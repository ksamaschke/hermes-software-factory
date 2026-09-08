"""Small, provider-neutral decision/evidence contract for factory supervisors.

This module is deliberately independent of a scheduler, tracker, model vendor, or
project.  ``evaluate_decision`` accepts the same narrow ``complete(prompt,
tools)`` shape used by a model adapter and exposes read-only tools.  A project
can replace the model and fixture adapter without changing the contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

CONTRACT_SCHEMA = "factory.decision.v1"
DECISION_LADDER = ("diagnose", "choose", "act", "read_back", "advance")
# Each inner tuple is a required field group.  A group with one field requires
# that field; a group with several fields accepts any one of the alternatives.
# The compact schema is shared by prompt construction and response ingress.
DECISION_REQUIRED_FIELDS = {
    "diagnose": (("summary", "cause"),),
    "choose": (("action",),),
    "act": (("action",), ("idempotency_key",)),
    "read_back": (
        ("idempotency_key",),
        ("status",),
        ("current_run_id",),
    ),
    "advance": (("next_phase",),),
}
EVIDENCE_KINDS = ("scheduler", "worker", "source", "review")
ALLOWED_ACTIONS = (
    "quarantine",
    "admit",
    "reuse_existing",
    "select_independent_lane",
    "repair_artifact",
    "hold_missing_capability",
    "hold",
)

_REDACTED = "[REDACTED]"
_PRIVATE_PATH = "[PRIVATE_PATH]"
_MAX_SAFE_VALUE_DEPTH = 32
_MAX_SAFE_VALUE_ITEMS = 1_024
_MAX_SAFE_TEXT_CHARS = 32_000
_MAX_INPUT_ITEMS = 1_024
_MAX_NATIVE_TRACE_ENTRIES = 512
_MAX_NATIVE_TRACE_BYTES = 512 * 1024
_MAX_NATIVE_OUTPUT_CHARS = 256 * 1024
_SECRET_KEY = re.compile(
    r"(?<![A-Za-z0-9])(?:token|password|passwd|secret|cookie|authorization|api[_ -]?key|access[_ -]?key|private[_ -]?(?:key|path)|credentials?(?!_verified\b)|raw[_ -]?(?:log|output))(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_UNSAFE_LOG_KEY = re.compile(r"raw[_ -]?(?:log|output)", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?P<name>\b(?:[A-Z0-9_ -]*(?:api[_ -]?key|access[_ -]?key|auth(?:orization)?|cookie|passwd|password|private[_ -]?key|secret|token|credential)s?)\b)"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;?&#]+)",
    re.IGNORECASE,
)
_AUTH_HEADER_VALUE = re.compile(
    r"(\bauthorization\s*[:=]\s*(?:basic|bearer|token|digest)\s+)[^\s,;?&#]+",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(\bbearer\s+)[^\s,;]+", re.IGNORECASE)
_PRIVATE_PATH_VALUE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:home|root|operator|Users|private|var/lib|etc/ssh|srv|opt|tmp|mnt|workspace(?:s)?|app)(?:/[^\s\"']*)?",
    re.IGNORECASE,
)
_SECRET_QUERY_NAME = re.compile(
    r"(?:token|password|passwd|secret|authorization|cookie|api[_-]?key|access[_-]?key|private[_-]?key|credential)",
    re.IGNORECASE,
)
_URI_CREDENTIALS = re.compile(
    r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE
)
_EVIDENCE_STATUSES = {
    "scheduler": frozenset(
        {
            "observed",
            "ready",
            "blocked",
            "held",
            "pending",
            "running",
            "completed",
            "failed",
        }
    ),
    "worker": frozenset(
        {
            "not_started",
            "running",
            "blocked",
            "held",
            "reused",
            "admitted",
            "completed",
            "failed",
        }
    ),
    "source": frozenset({"open", "changed", "merged", "closed", "ready", "failed"}),
    "review": frozenset(
        {
            "not_required",
            "pending",
            "approved",
            "changes_requested",
            "rejected",
            "complete",
            "failed",
        }
    ),
}


def _redact_text(text: str) -> str:
    """Redact secret-bearing values while preserving safe identity text."""

    value = str(text)
    if len(value) > _MAX_SAFE_TEXT_CHARS:
        raise ContractViolation("text value exceeds the contract bound")
    value = _AUTH_HEADER_VALUE.sub(rf"\1{_REDACTED}", value)
    value = _BEARER_VALUE.sub(rf"\1{_REDACTED}", value)
    value = _URI_CREDENTIALS.sub(rf"\1{_REDACTED}@", value)
    value = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}{_REDACTED}",
        value,
    )

    # Query values are untrusted evidence.  Keep ordinary URL structure but
    # remove credentials and values for explicitly sensitive parameters.
    def redact_query(match: re.Match[str]) -> str:
        prefix, query = match.group(1), match.group(2)
        pairs = []
        for pair in query.split("&"):
            key, separator, _item = pair.partition("=")
            if separator and _SECRET_QUERY_NAME.search(key):
                pairs.append(f"{key}={_REDACTED}")
            else:
                pairs.append(pair)
        return prefix + "?" + "&".join(pairs)

    value = re.sub(r"(https?://[^\s?#]+)\?([^\s#]+)", redact_query, value)

    def redact_fragment(match: re.Match[str]) -> str:
        prefix, fragment = match.group(1), match.group(2)
        pairs = []
        for pair in fragment.split("&"):
            key, separator, _item = pair.partition("=")
            if separator and _SECRET_QUERY_NAME.search(key):
                pairs.append(f"{key}={_REDACTED}")
            else:
                pairs.append(pair)
        return prefix + "#" + "&".join(pairs)

    value = re.sub(r"(https?://[^\s#]+)#([^\s]+)", redact_fragment, value)
    value = _PRIVATE_PATH_VALUE.sub(_PRIVATE_PATH, value)
    value = re.sub(
        r"(?<![A-Za-z0-9_])(?:[A-Za-z]:)?\\(?:Users|home|root|private)\\[^\s\"']+",
        _PRIVATE_PATH,
        value,
        flags=re.IGNORECASE,
    )
    return value


def _safe_identifier(value: Any, field_name: str) -> str:
    """Validate an identity and keep secrets/private paths out of context."""

    result = _required(value, field_name)
    redacted = _redact_text(result)
    if redacted != result:
        raise ContractViolation(f"unsafe value in {field_name}")
    return result


class ContractViolation(ValueError):
    """Raised when a context, proposal, budget, or readback is unsafe."""


class DecisionModel(Protocol):
    """Minimal model adapter used by the evaluator."""

    def complete(
        self, prompt: str, tools: Mapping[str, Callable[..., Any]]
    ) -> Mapping[str, Any]: ...


def _required(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ContractViolation(f"missing or malformed field: {field_name}")
    value = value.strip()
    if not value:
        raise ContractViolation(f"missing required field: {field_name}")
    if len(value) > _MAX_SAFE_TEXT_CHARS:
        raise ContractViolation(f"text value exceeds the contract bound: {field_name}")
    return value


def _safe_value(value: Any, field_name: str = "value", *, _depth: int = 0) -> Any:
    """Copy bounded JSON-like values with value-level secret redaction."""

    if _depth > _MAX_SAFE_VALUE_DEPTH:
        raise ContractViolation(
            f"nested value exceeds the contract bound: {field_name}"
        )
    if isinstance(value, Mapping):
        if len(value) > _MAX_SAFE_VALUE_ITEMS:
            raise ContractViolation(f"mapping exceeds the contract bound: {field_name}")
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if len(key_text) > _MAX_SAFE_TEXT_CHARS:
                raise ContractViolation(
                    f"field name exceeds the contract bound: {field_name}"
                )
            if _UNSAFE_LOG_KEY.search(key_text):
                raise ContractViolation(f"unsafe field in {field_name}: {key_text}")
            safe_key = _redact_text(key_text)
            if _SECRET_KEY.search(key_text):
                safe_item = _REDACTED
            else:
                safe_item = _safe_value(
                    item, f"{field_name}.{safe_key}", _depth=_depth + 1
                )
            if safe_key in result:
                raise ContractViolation(f"redacted field collision in {field_name}")
            result[safe_key] = safe_item
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_SAFE_VALUE_ITEMS:
            raise ContractViolation(
                f"sequence exceeds the contract bound: {field_name}"
            )
        return [_safe_value(item, field_name, _depth=_depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ContractViolation(f"non-finite value in {field_name}")
        return value
    if isinstance(value, str):
        return _redact_text(value)
    raise ContractViolation(f"unsupported value in {field_name}")


def _compact(text: str) -> str:
    return " ".join(str(text).split())


def decision_response_requirements_text() -> str:
    """Render the one response schema used by prompts and ingress checks."""

    rendered = []
    for step in DECISION_LADDER:
        groups = DECISION_REQUIRED_FIELDS[step]
        fields = []
        for alternatives in groups:
            fields.append(" or ".join(f"{step}.{field}" for field in alternatives))
        rendered.append(f"{step} requires " + " and ".join(fields))
    return "; ".join(rendered)


def _bounded_text(text: str, limit: int) -> tuple[str, bool]:
    """Keep both ends of a skill while making the bound visible."""

    if limit <= 0:
        return "", bool(text)
    text = _compact(text)
    if len(text) <= limit:
        return text, False
    marker = " ...[trimmed]... "
    if limit <= len(marker):
        return text[:limit], True
    remaining = limit - len(marker)
    left = remaining // 2
    right = remaining - left
    return f"{text[:left]}{marker}{text[-right:]}", True


@dataclass(frozen=True)
class SourceIdentity:
    """Stable tracker identity; titles and bodies are intentionally excluded."""

    tracker: str
    project: str
    item_key: str
    kind: str = "issue"
    url: str | None = None

    @property
    def canonical_key(self) -> str:
        # JSON string escaping makes the four ordered identity components
        # injective even when an identifier contains the historical delimiter.
        return "source.v1:" + json.dumps(
            [
                _safe_identifier(self.tracker, "source_item.tracker"),
                _safe_identifier(self.project, "source_item.project"),
                _safe_identifier(self.kind, "source_item.kind"),
                _safe_identifier(self.item_key, "source_item.item_key"),
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "tracker": _safe_identifier(self.tracker, "source_item.tracker"),
            "project": _safe_identifier(self.project, "source_item.project"),
            "kind": _safe_identifier(self.kind, "source_item.kind"),
            "item_key": _safe_identifier(self.item_key, "source_item.item_key"),
            "canonical_key": self.canonical_key,
        }
        if self.url is not None:
            if not isinstance(self.url, str):
                raise ContractViolation("source_item.url is malformed")
            result["url"] = _redact_text(self.url)
        return result


def build_input_identity(
    source_item: SourceIdentity,
    phase: str,
    input_payload: Mapping[str, Any],
) -> str:
    """Return a stable identity for one source/phase/input observation."""

    document = {
        "source_item": source_item.as_dict(),
        "phase": _safe_identifier(phase, "phase"),
        "input": _safe_value(dict(input_payload), "input"),
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return "input-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ExecutionIdentity:
    """Runtime route and current execution identity, kept separate by design."""

    mode: str
    profile_name: str
    task_id: str
    run_id: str | None = None
    branch: str | None = None
    tenant: str | None = None
    credentials_verified: bool = True
    production: bool = False
    production_approved: bool = False
    retry_count: int = 0
    singleton_key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.run_id is not None and not isinstance(self.run_id, str):
            raise ContractViolation("current_run.id is malformed")
        if (
            not isinstance(self.retry_count, int)
            or isinstance(self.retry_count, bool)
            or self.retry_count < 0
        ):
            raise ContractViolation("execution retry count is invalid")
        if not isinstance(self.credentials_verified, bool):
            raise ContractViolation("credential verification guard is malformed")
        if not isinstance(self.production, bool) or not isinstance(
            self.production_approved, bool
        ):
            raise ContractViolation("production guard is malformed")
        return {
            "mode": _safe_identifier(self.mode, "execution_mode"),
            "profile_name": _safe_identifier(self.profile_name, "profile_name"),
            "current_task": {"id": _safe_identifier(self.task_id, "current_task.id")},
            "current_run": {"id": _safe_identifier(self.run_id, "current_run.id")}
            if self.run_id is not None
            else None,
            "guards": {
                "branch": _safe_identifier(self.branch, "execution.branch")
                if self.branch is not None
                else None,
                "tenant": _safe_identifier(self.tenant, "execution.tenant")
                if self.tenant is not None
                else None,
                "credentials_verified": self.credentials_verified,
                "production": self.production,
                "production_approved": self.production_approved,
                "retry_count": self.retry_count,
                "singleton_key": _safe_identifier(
                    self.singleton_key, "execution.singleton_key"
                )
                if self.singleton_key is not None
                else None,
            },
        }


@dataclass(frozen=True)
class BlockerState:
    """Evidence-derived blocker state, not a title/body heuristic."""

    fingerprint: str | None = None
    previous_fingerprint: str | None = None
    occurrences: int = 0
    resolved: bool = False

    def as_dict(self) -> dict[str, Any]:
        if (
            not isinstance(self.occurrences, int)
            or isinstance(self.occurrences, bool)
            or self.occurrences < 0
        ):
            raise ContractViolation("blocker occurrences are malformed")
        if not isinstance(self.resolved, bool):
            raise ContractViolation("blocker resolution is malformed")
        for field_name, value in (
            ("blocker.fingerprint", self.fingerprint),
            ("blocker.previous_fingerprint", self.previous_fingerprint),
        ):
            if value is not None:
                _safe_identifier(value, field_name)
        return {
            "fingerprint": _safe_identifier(self.fingerprint, "blocker.fingerprint")
            if self.fingerprint is not None
            else None,
            "previous_fingerprint": _safe_identifier(
                self.previous_fingerprint, "blocker.previous_fingerprint"
            )
            if self.previous_fingerprint is not None
            else None,
            "occurrences": self.occurrences,
            "resolved": bool(self.resolved),
        }


@dataclass(frozen=True)
class ParentCompletion:
    state: str
    verified: bool
    parent_ids: tuple[str, ...] = ()
    evidence_reference: str | None = None

    def as_dict(self) -> dict[str, Any]:
        state = _safe_identifier(self.state, "parent_completion.state")
        if state not in {"complete", "incomplete", "none", "unknown"}:
            raise ContractViolation(f"invalid parent completion state: {state}")
        if not isinstance(self.verified, bool):
            raise ContractViolation("parent completion verification is malformed")
        result: dict[str, Any] = {
            "state": state,
            "verified": self.verified,
            "parent_ids": [
                _safe_identifier(parent_id, "parent_completion.parent_id")
                for parent_id in self.parent_ids
            ],
        }
        if self.evidence_reference is not None:
            if not isinstance(self.evidence_reference, str):
                raise ContractViolation(
                    "parent completion evidence reference is malformed"
                )
            result["evidence_reference"] = _redact_text(self.evidence_reference)
        return result


@dataclass(frozen=True)
class TypedEvidence:
    """One typed, bounded evidence observation."""

    kind: str
    subject: str
    status: str
    reference: str
    run_id: str | None = None
    candidate: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    phase: str | None = None
    input_identity: str | None = None
    task_id: str | None = None
    source_key: str | None = None
    semantic_lane: str | None = None

    def as_dict(self) -> dict[str, Any]:
        kind = _safe_identifier(self.kind, "evidence.kind")
        if kind not in EVIDENCE_KINDS:
            raise ContractViolation(f"invalid evidence kind: {kind}")
        status = _safe_identifier(self.status, f"evidence.{kind}.status")
        if status not in _EVIDENCE_STATUSES[kind]:
            raise ContractViolation(f"invalid evidence status for {kind}: {status}")
        result: dict[str, Any] = {
            "kind": kind,
            "subject": _redact_text(
                _required(self.subject, f"evidence.{kind}.subject")
            ),
            "status": status,
            "reference": _redact_text(
                _required(self.reference, f"evidence.{kind}.reference")
            ),
        }
        if self.run_id is not None:
            result["run_id"] = _safe_identifier(self.run_id, f"evidence.{kind}.run_id")
        if self.candidate is not None:
            result["candidate"] = _safe_identifier(
                self.candidate, f"evidence.{kind}.candidate"
            )
        if not isinstance(self.attributes, Mapping):
            raise ContractViolation(f"evidence.{kind}.attributes are malformed")
        if self.attributes:
            result["attributes"] = _safe_value(
                dict(self.attributes), f"evidence.{kind}.attributes"
            )
        for field_name in (
            "phase",
            "input_identity",
            "task_id",
            "source_key",
            "semantic_lane",
        ):
            value = getattr(self, field_name)
            if value is not None:
                result[field_name] = _safe_identifier(
                    value, f"evidence.{kind}.{field_name}"
                )
        return result


@dataclass(frozen=True)
class EvidenceBundle:
    scheduler: tuple[TypedEvidence, ...] = ()
    worker: tuple[TypedEvidence, ...] = ()
    source: tuple[TypedEvidence, ...] = ()
    review: tuple[TypedEvidence, ...] = ()

    def as_dict(self) -> dict[str, list[dict[str, Any]]]:
        rows = {
            "scheduler": self.scheduler,
            "worker": self.worker,
            "source": self.source,
            "review": self.review,
        }
        result: dict[str, list[dict[str, Any]]] = {}
        for expected_kind, entries in rows.items():
            rendered = []
            for entry in entries:
                if not isinstance(entry, TypedEvidence):
                    raise ContractViolation(
                        f"evidence.{expected_kind} contains a malformed entry"
                    )
                item = entry.as_dict()
                if item["kind"] != expected_kind:
                    raise ContractViolation(
                        f"evidence kind {item['kind']} is in {expected_kind} bundle"
                    )
                rendered.append(item)
            result[expected_kind] = rendered
        return result


@dataclass(frozen=True)
class DecisionPolicy:
    max_prompt_chars: int = 12_000
    max_skill_chars: int = 6_000
    max_skills: int = 4
    repeated_blocker_threshold: int = 3
    conflict_mode: str = "fail_closed"
    allowed_actions: tuple[str, ...] = ALLOWED_ACTIONS
    transition_policy: tuple[tuple[str, str], ...] = (
        ("quarantine", "current_phase"),
        ("admit", "implementation"),
        ("reuse_existing", "current_phase"),
        ("select_independent_lane", "implementation"),
        ("repair_artifact", "artifact"),
        ("hold_missing_capability", "current_phase"),
        ("hold", "current_phase"),
    )
    max_tool_chars: int = 4_000
    max_tools: int = 16
    require_independent_review: bool = True
    require_exact_reuse_binding: bool = True
    allowed_execution_modes: tuple[str, ...] = ("scheduled", "interactive")

    def as_dict(self) -> dict[str, Any]:
        integer_fields = (
            self.max_prompt_chars,
            self.max_skill_chars,
            self.max_skills,
            self.repeated_blocker_threshold,
            self.max_tool_chars,
            self.max_tools,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in integer_fields
        ):
            raise ContractViolation("policy budgets and thresholds are malformed")
        if (
            self.max_prompt_chars <= 0
            or self.max_skill_chars < 0
            or self.max_tool_chars < 0
        ):
            raise ContractViolation("prompt and skill budgets must be non-negative")
        if (
            self.max_skills < 0
            or self.max_tools < 1
            or self.repeated_blocker_threshold < 1
        ):
            raise ContractViolation("invalid skill or blocker policy bound")
        if not isinstance(self.require_independent_review, bool) or not isinstance(
            self.require_exact_reuse_binding, bool
        ):
            raise ContractViolation("policy guard flags are malformed")
        if self.conflict_mode != "fail_closed":
            raise ContractViolation("conflict mode must be fail_closed")
        if isinstance(self.allowed_execution_modes, (str, bytes)):
            raise ContractViolation("policy execution modes are malformed")
        try:
            execution_modes = [
                _safe_identifier(mode, "policy.execution_mode")
                for mode in self.allowed_execution_modes
            ]
        except TypeError as exc:
            raise ContractViolation("policy execution modes are malformed") from exc
        if not execution_modes:
            raise ContractViolation("policy has no valid execution mode")
        if len(set(execution_modes)) != len(execution_modes):
            raise ContractViolation("policy execution modes are duplicated")
        if isinstance(self.allowed_actions, (str, bytes)):
            raise ContractViolation("policy actions are malformed")
        try:
            allowed = [
                _safe_identifier(action, "policy.action")
                for action in self.allowed_actions
            ]
        except TypeError as exc:
            raise ContractViolation("policy actions are malformed") from exc
        if not allowed or not set(allowed) <= set(ALLOWED_ACTIONS):
            raise ContractViolation("policy contains an unsupported action")
        if len(set(allowed)) != len(allowed):
            raise ContractViolation("policy actions are duplicated")
        try:
            transition_entries = list(self.transition_policy)
            transition_pairs = []
            for entry in transition_entries:
                if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                    raise ValueError("transition entry must contain action and phase")
                transition_pairs.append((entry[0], entry[1]))
        except (TypeError, ValueError) as exc:
            raise ContractViolation("policy transition table is malformed") from exc
        transition_pairs = [
            (
                _safe_identifier(action, "policy.transition.action"),
                _safe_identifier(next_phase, "policy.transition.next_phase"),
            )
            for action, next_phase in transition_pairs
        ]
        transition_actions = [action for action, _next_phase in transition_pairs]
        if len(set(transition_actions)) != len(transition_actions):
            raise ContractViolation("policy transitions are duplicated")
        transition = dict(transition_pairs)
        if set(allowed) - set(transition):
            raise ContractViolation("policy has no transition for an allowed action")
        if any(
            action not in ALLOWED_ACTIONS
            or not isinstance(next_phase, str)
            or not _compact(next_phase)
            for action, next_phase in transition.items()
        ):
            raise ContractViolation("policy contains an invalid transition")
        transition = {
            _safe_identifier(action, "policy.transition.action"): _safe_identifier(
                next_phase, "policy.transition.next_phase"
            )
            for action, next_phase in transition.items()
        }
        return {
            "budgets": {
                "max_prompt_chars": self.max_prompt_chars,
                "max_skill_chars": self.max_skill_chars,
                "max_skills": self.max_skills,
                "max_tool_chars": self.max_tool_chars,
                "max_tools": self.max_tools,
            },
            "repeated_blocker_threshold": self.repeated_blocker_threshold,
            "conflict_mode": self.conflict_mode,
            "require_independent_review": self.require_independent_review,
            "require_exact_reuse_binding": self.require_exact_reuse_binding,
            "allowed_execution_modes": execution_modes,
            "allowed_actions": allowed,
            "transition_policy": transition,
            "current_run_null": {
                "before_spawn": True,
                "reused_or_held": True,
                "new_admission_requires_run": True,
            },
        }


@dataclass(frozen=True)
class DecisionContext:
    execution: ExecutionIdentity
    source_item: SourceIdentity
    phase: str
    input_identity: str
    blocker: BlockerState
    parent_completion: ParentCompletion
    evidence: EvidenceBundle
    policy: DecisionPolicy = field(default_factory=DecisionPolicy)
    prior_decision: Mapping[str, Any] | None = None
    semantic_lane: str = "current"

    def as_dict(self) -> dict[str, Any]:
        execution = self.execution.as_dict()
        result: dict[str, Any] = {
            "schema": CONTRACT_SCHEMA,
            "execution_mode": execution["mode"],
            "profile_name": execution["profile_name"],
            "current_task": execution["current_task"],
            "current_run": execution["current_run"],
            "source_item": self.source_item.as_dict(),
            "phase": _safe_identifier(self.phase, "phase"),
            "input_identity": _safe_identifier(self.input_identity, "input_identity"),
            "semantic_lane": _safe_identifier(self.semantic_lane, "semantic_lane"),
            "idempotency_key": action_idempotency_key(self),
            "blocker": self.blocker.as_dict(),
            "parent_completion": self.parent_completion.as_dict(),
            "evidence": self.evidence.as_dict(),
            "policy": self.policy.as_dict(),
        }
        if self.prior_decision is not None:
            result["prior_decision"] = _safe_value(
                dict(self.prior_decision), "prior_decision"
            )
        return result


def action_idempotency_key(context: DecisionContext) -> str:
    """Derive one stable action key from canonical identity, never title text."""

    value = "\x1f".join(
        (
            context.source_item.canonical_key,
            _safe_identifier(context.phase, "phase"),
            _safe_identifier(context.input_identity, "input_identity"),
            _safe_identifier(context.semantic_lane, "semantic_lane"),
            _safe_identifier(context.execution.task_id, "current_task.id"),
        )
    )
    return "action-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _evidence_identity(item: Mapping[str, Any], name: str) -> Any:
    """Read an optional identity field from its canonical or attributes slot."""

    if name in item:
        return item[name]
    attributes = item.get("attributes")
    if isinstance(attributes, Mapping):
        return attributes.get(name)
    return None


def _reference_binds_context(
    reference: str,
    *,
    kind: str,
    item_key: str,
    task_id: str,
    input_identity: str,
    source_key: str,
) -> bool:
    """Require a reference to carry one exact current-lane identity.

    References are opaque handles, but a fixture/reference adapter must still
    bind them to the current lane.  Substring matching is unsafe: a foreign
    handle can contain a valid item ID as a prefix or embedded fragment.  The
    repository's compact fixture form is ``<kind>-ref-<item_key>``; the other
    exact identities are accepted for provider adapters that use them directly.
    """

    if not isinstance(reference, str) or not reference.startswith(f"{kind}-"):
        return False
    suffix = reference[len(kind) + 1 :]
    accepted = {
        item_key,
        f"ref-{item_key}",
        task_id,
        f"ref-{task_id}",
        input_identity,
        f"ref-{input_identity}",
        source_key,
        f"ref-{source_key}",
    }
    return suffix in accepted


def _validate_evidence_for_context(context: DecisionContext) -> tuple[str, ...]:
    """Validate presence, vocabulary, and canonical identity of all evidence."""

    try:
        evidence = context.evidence.as_dict()
    except (AttributeError, KeyError, TypeError, ValueError, ContractViolation) as exc:
        return (str(exc),)

    conflicts: list[str] = []
    if any(not evidence[kind] for kind in EVIDENCE_KINDS):
        conflicts.append("all typed evidence bundles are required")

    source_key = context.source_item.canonical_key
    item_key = _safe_identifier(context.source_item.item_key, "source_item.item_key")
    task_id = _safe_identifier(context.execution.task_id, "current_task.id")
    scheduler_subjects = {
        f"tick-{item_key}",
        f"scheduler-{item_key}",
        f"tick-{context.input_identity}",
        context.input_identity,
        task_id,
    }
    for kind in EVIDENCE_KINDS:
        for item in evidence[kind]:
            subject = item["subject"]
            expected_subjects = {
                "scheduler": scheduler_subjects,
                "worker": {task_id},
                "source": {source_key},
                "review": {source_key},
            }[kind]
            if subject not in expected_subjects:
                conflicts.append(f"{kind} evidence subject is not bound to context")
            if not _reference_binds_context(
                item["reference"],
                kind=kind,
                item_key=item_key,
                task_id=task_id,
                input_identity=context.input_identity,
                source_key=source_key,
            ):
                conflicts.append(f"{kind} evidence reference is not bound to context")

            for field_name, expected in (
                ("phase", context.phase),
                ("input_identity", context.input_identity),
                ("source_key", source_key),
                ("semantic_lane", context.semantic_lane),
            ):
                observed = _evidence_identity(item, field_name)
                if observed is not None and observed != expected:
                    conflicts.append(
                        f"{kind} evidence {field_name} does not match context"
                    )
            observed_task = _evidence_identity(item, "task_id")
            if observed_task is not None and observed_task != task_id:
                conflicts.append(
                    f"{kind} evidence task identity does not match context"
                )

            source_observation = _evidence_identity(item, "source_item")
            if source_observation is not None:
                if isinstance(source_observation, Mapping):
                    observed_canonical = source_observation.get("canonical_key")
                else:
                    observed_canonical = source_observation
                if observed_canonical != source_key:
                    conflicts.append(
                        f"{kind} evidence source identity does not match context"
                    )

            if kind == "worker":
                run_id = item.get("run_id")
                if run_id != context.execution.run_id:
                    conflicts.append(
                        "worker evidence run does not match current execution"
                    )

    return tuple(dict.fromkeys(conflicts))


def conflict_checks(context: DecisionContext) -> tuple[str, ...]:
    """Return contradictions that must prevent a model call."""

    conflicts: list[str] = []
    try:
        context.execution.as_dict()
        context.source_item.as_dict()
        context.blocker.as_dict()
        context.parent_completion.as_dict()
        context.evidence.as_dict()
        context.policy.as_dict()
    except (AttributeError, KeyError, TypeError, ValueError, ContractViolation) as exc:
        return (f"malformed decision context: {exc}",)

    if context.blocker.occurrences and not context.blocker.fingerprint:
        conflicts.append("blocker fingerprint is missing for an observed blocker")
    if (
        context.parent_completion.state == "complete"
        and not context.parent_completion.verified
    ):
        conflicts.append("parent completion is marked complete but is not verified")
    if context.parent_completion.state == "unknown":
        conflicts.append("parent completion is unknown")

    conflicts.extend(_validate_evidence_for_context(context))

    by_reference: dict[str, set[str]] = {}
    for entries in (
        context.evidence.scheduler,
        context.evidence.worker,
        context.evidence.source,
        context.evidence.review,
    ):
        for evidence in entries:
            by_reference.setdefault(evidence.reference, set()).add(evidence.status)
    if any(len(statuses) > 1 for statuses in by_reference.values()):
        conflicts.append("same evidence reference has conflicting statuses")

    candidates = {
        evidence.candidate
        for evidence in (*context.evidence.source, *context.evidence.review)
        if evidence.candidate
    }
    if len(candidates) > 1:
        conflicts.append("source and review evidence name conflicting candidates")
    return tuple(conflicts)


def validate_context(context: DecisionContext) -> None:
    """Validate all required context and fail closed on contradictions."""

    try:
        _safe_identifier(context.phase, "phase")
        _safe_identifier(context.input_identity, "input_identity")
        _safe_identifier(context.semantic_lane, "semantic_lane")
        conflicts = conflict_checks(context)
    except ContractViolation:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ContractViolation(f"malformed decision context: {exc}") from exc
    if conflicts:
        raise ContractViolation("context conflict: " + "; ".join(conflicts))


@dataclass(frozen=True)
class PromptEnvelope:
    prompt: str
    prompt_chars: int
    skill_chars: int
    tool_chars: int
    tool_count: int
    effective_prompt_chars: int
    selected_skills: tuple[str, ...]
    omitted_skills: tuple[str, ...]
    shrunk: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_chars": self.prompt_chars,
            "skill_chars": self.skill_chars,
            "tool_chars": self.tool_chars,
            "tool_count": self.tool_count,
            "effective_prompt_chars": self.effective_prompt_chars,
            "selected_skills": list(self.selected_skills),
            "omitted_skills": list(self.omitted_skills),
            "shrunk": self.shrunk,
        }


def _check_input_items(value: Any, field_name: str) -> None:
    if (
        isinstance(value, (Mapping, Sequence))
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) > _MAX_INPUT_ITEMS
    ):
        raise ContractViolation(f"{field_name} input exceeds the contract bound")


def _skill_entries(
    skills: Mapping[str, str] | Sequence[tuple[str, str]] | None,
) -> list[tuple[str, str]]:
    if skills is None:
        return []
    _check_input_items(skills, "skill")
    if isinstance(skills, Mapping):
        entries = list(skills.items())
    else:
        entries = list(skills)
    normalized = []
    for name, text in entries:
        normalized.append((_safe_identifier(name, "skill.name"), str(text)))
    return normalized


def prepare_prompt(
    context: DecisionContext,
    skills: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
    *,
    prompt_suffix: str = "",
    tool_catalog: Mapping[str, Any] | Sequence[str] | None = None,
) -> PromptEnvelope:
    """Build an effective bounded prompt and report what was omitted/trimmed."""

    validate_context(context)
    policy = context.policy
    safe_suffix = _redact_text(str(prompt_suffix))
    document = json.dumps(context.as_dict(), sort_keys=True, separators=(",", ":"))
    _check_input_items(tool_catalog, "tool")
    if tool_catalog is None:
        tool_entries: list[str] = []
    elif isinstance(tool_catalog, Mapping):
        tool_entries = [str(name) for name in tool_catalog]
    else:
        tool_entries = [str(name) for name in tool_catalog]
    if len(tool_entries) > policy.max_tools:
        raise ContractViolation("effective tool catalog exceeds tool-count budget")
    if any(not _safe_identifier(name, "tool.name") for name in tool_entries):
        raise ContractViolation("effective tool catalog contains an empty name")
    tool_text = "\n".join(f"[{name}]" for name in sorted(set(tool_entries)))
    if len(tool_text) > policy.max_tool_chars:
        raise ContractViolation("effective tool catalog exceeds character budget")
    instructions = (
        "You are the bounded factory decision model. Use only the typed context "
        "and read-only tools. Do not infer identity from title/body text or unbounded "
        "logs. Return exactly the five ladder objects diagnose, choose, act, "
        "read_back, advance. Diagnose before choosing; choose before acting; "
        "read back the exact idempotent result before advancing. A null current "
        "run means not started/reused/held, never a new run. Conflicts fail closed. "
        f"Required response fields: {decision_response_requirements_text()}. "
        "The next phase is derived from the typed transition policy, not inferred "
        "from an unbounded log."
    )
    prefix = (
        f"{instructions}\n\nCONTEXT_JSON\n{document}\nEND_CONTEXT\n\nTOOLS\n{tool_text}"
    )
    entries = _skill_entries(skills)
    names = [name for name, _ in entries]
    omitted: list[str] = names[policy.max_skills :]
    entries = entries[: policy.max_skills]

    # Reserve room for the fixed contract and context. Skills are optional
    # context, so shrinking them never weakens the typed contract itself.
    skill_budget = policy.max_skill_chars
    prompt_budget = policy.max_prompt_chars
    remaining_prompt = (
        prompt_budget - len(prefix) - len("\nSKILLS\n") - len(safe_suffix)
    )
    if remaining_prompt < 0:
        raise ContractViolation("prompt budget is smaller than required context")
    effective_skill_budget = min(skill_budget, remaining_prompt)

    skill_lines: list[str] = []
    selected: list[str] = []
    shrunk = False
    remaining = effective_skill_budget
    for index, (name, text) in enumerate(entries):
        compacted = _compact(_redact_text(text))
        # Include the name in the bounded accounting as it is part of the model
        # input, while preserving a complete typed context outside this section.
        available = max(0, remaining - len(name) - 4)
        bounded, was_shrunk = _bounded_text(compacted, available)
        if not bounded:
            omitted.append(name)
            shrunk = shrunk or bool(compacted)
            continue
        skill_lines.append(f"[{name}] {bounded}")
        selected.append(name)
        consumed = len(skill_lines[-1]) + (1 if skill_lines else 0)
        remaining -= consumed
        shrunk = shrunk or was_shrunk or compacted != text
        if remaining <= 0 and index + 1 < len(entries):
            omitted.extend(name for name, _ in entries[index + 1 :])
            break

    skill_text = "\n".join(skill_lines)
    prompt = prefix + "\nSKILLS\n" + skill_text + safe_suffix
    if len(prompt) > prompt_budget:
        # Defensive correction for separators/name accounting. Never silently
        # exceed the effective limit.
        overflow = len(prompt) - prompt_budget
        skill_text, was_shrunk = _bounded_text(
            skill_text, max(0, len(skill_text) - overflow)
        )
        prompt = prefix + "\nSKILLS\n" + skill_text + safe_suffix
        shrunk = True
    if len(prompt) > prompt_budget:
        raise ContractViolation("effective prompt budget exceeded")

    return PromptEnvelope(
        prompt=prompt,
        prompt_chars=len(prompt),
        skill_chars=len(skill_text),
        tool_chars=len(tool_text),
        tool_count=len(set(tool_entries)),
        effective_prompt_chars=len(prompt),
        selected_skills=tuple(selected),
        omitted_skills=tuple(dict.fromkeys(omitted)),
        shrunk=shrunk,
    )


def build_prompt(
    context: DecisionContext,
    skills: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
) -> str:
    """Compatibility helper returning only the bounded prompt text."""

    return prepare_prompt(context, skills).prompt


def _receipt_digest(receipt: Mapping[str, Any]) -> str:
    """Authenticate the complete sanitized post-proposal receipt payload."""

    document = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    return (
        "receipt-"
        + hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32]
    )


def _validate_observation_trace(
    trace: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    """Require one ordered proposal and one authenticated receipt payload."""

    if isinstance(trace, (str, bytes)) or not isinstance(trace, Sequence):
        raise ContractViolation("native fixture trace must be a sequence")
    if len(trace) > _MAX_NATIVE_TRACE_ENTRIES:
        raise ContractViolation("native fixture trace exceeds the contract bound")
    names: list[str] = []
    allowed = {
        "read_live_state",
        "read_parent_completion",
        "read_source_state",
        "read_ready_lanes",
        "read_capabilities",
        "propose_action",
        "read_action_readback",
    }
    for entry in trace:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("tool"), str):
            raise ContractViolation("native fixture trace contains a malformed entry")
        if any(
            key
            not in {"tool", "action", "idempotency_key", "target_task_id", "receipt"}
            for key in entry
        ):
            raise ContractViolation("native fixture trace contains an unexpected field")
        names.append(entry["tool"])
    unexpected = sorted(set(names) - allowed)
    if unexpected:
        raise ContractViolation(
            "native fixture trace contains an unexpected tool: " + ", ".join(unexpected)
        )
    proposals = [index for index, name in enumerate(names) if name == "propose_action"]
    readbacks = [
        index for index, name in enumerate(names) if name == "read_action_readback"
    ]
    if len(proposals) != 1:
        raise ContractViolation("native fixture trace must contain one proposal")
    if len(readbacks) != 1 or proposals[0] >= readbacks[0]:
        raise ContractViolation(
            "native fixture trace must contain one post-proposal readback"
        )
    required_reads = {
        "read_live_state",
        "read_parent_completion",
        "read_source_state",
        "read_ready_lanes",
        "read_capabilities",
    }
    missing = sorted(required_reads - set(names))
    if missing:
        raise ContractViolation(
            "native fixture trace is missing required reads: " + ", ".join(missing)
        )
    if any(
        names.index(required_name) > proposals[0] for required_name in required_reads
    ):
        raise ContractViolation(
            "native fixture proposal precedes a required state observation"
        )

    proposal = trace[proposals[0]]
    readback = trace[readbacks[0]]
    proposal_fields = {"tool", "action", "idempotency_key"}
    if "target_task_id" in proposal:
        proposal_fields.add("target_task_id")
    if set(proposal) != proposal_fields:
        raise ContractViolation("native fixture proposal payload is malformed")
    if set(readback) != {"tool", "idempotency_key", "receipt"}:
        raise ContractViolation("native fixture readback payload is malformed")
    action = _safe_identifier(proposal.get("action"), "trace.proposal.action")
    if action not in ALLOWED_ACTIONS:
        raise ContractViolation("native fixture proposal action is unsupported")
    key = _safe_identifier(
        proposal.get("idempotency_key"), "trace.proposal.idempotency_key"
    )
    target = proposal.get("target_task_id")
    if target is not None:
        target = _safe_identifier(target, "trace.proposal.target_task_id")
    readback_key = _safe_identifier(
        readback.get("idempotency_key"), "trace.readback.idempotency_key"
    )
    receipt = readback.get("receipt")
    if not isinstance(receipt, Mapping):
        raise ContractViolation("native fixture trace omitted the receipt payload")
    try:
        receipt_document = dict(_safe_value(dict(receipt), "trace.receipt"))
    except (TypeError, ValueError, ContractViolation) as exc:
        raise ContractViolation("native fixture trace receipt is malformed") from exc
    if readback_key != key or receipt_document.get("idempotency_key") != key:
        raise ContractViolation("native fixture trace receipt identity is stale")
    if receipt_document.get("action") != action:
        raise ContractViolation("native fixture trace receipt action differs")
    if receipt_document.get("receipt") != "post_proposal":
        raise ContractViolation("native fixture trace receipt is not post-proposal")
    if receipt_document.get("receipt_digest") != _receipt_digest(receipt_document):
        raise ContractViolation("native fixture trace receipt digest is invalid")
    selected = receipt_document.get("selected_task_id")
    if selected != target and (selected is not None or target is not None):
        raise ContractViolation("native fixture trace receipt target differs")
    return proposals[0], readbacks[0]


def simulated_action_readback(
    action: str, idempotency_key: str, target_task_id: str | None = None
) -> dict[str, Any]:
    """Return a deterministic receipt only after a fixture proposal."""

    action = _safe_identifier(action, "proposal.action")
    idempotency_key = _safe_identifier(idempotency_key, "proposal.idempotency_key")
    statuses = {
        "quarantine": "quarantined",
        "admit": "admitted",
        "reuse_existing": "reused",
        "select_independent_lane": "selected",
        "repair_artifact": "artifact-remediation",
        "hold_missing_capability": "held",
        "hold": "held",
    }
    if action not in statuses:
        raise ContractViolation(f"unsupported fixture proposal action: {action}")
    result: dict[str, Any] = {
        "action": action,
        "status": statuses[action],
        "idempotency_key": idempotency_key,
        "current_run_id": (
            "run-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
            if action == "admit"
            else None
        ),
        "admission_count": 1 if action == "admit" else 0,
    }
    if target_task_id is not None:
        result["selected_task_id"] = _safe_identifier(
            target_task_id, "readback.selected_task_id"
        )
    result["receipt"] = "post_proposal"
    result["receipt_digest"] = _receipt_digest(result)
    return result


class NoSideEffectFixtureAdapter:
    """Read-only fixture adapter for behavioral evaluations.

    The adapter returns copies, records reads, and has no mutation method in the
    tool registry. Direct calls to common mutation names fail loudly and count an
    attempted write, making accidental side effects observable in tests.
    """

    def __init__(self, state: Mapping[str, Any]) -> None:
        if not isinstance(state, Mapping):
            raise ContractViolation("fixture state must be a JSON object")
        self._state = copy.deepcopy(dict(state))
        self._baseline = copy.deepcopy(self._state)
        self._reads: list[str] = []
        self._proposal: dict[str, Any] | None = None
        self._external_readback: dict[str, Any] | None = None
        self._context: DecisionContext | None = None
        self._preproposal_receipt_reads = 0
        self._postproposal_receipt_reads = 0
        self._last_readback: dict[str, Any] | None = None
        self._proposal_attempts = 0
        self.mutation_attempts = 0

    def bind_context(self, context: DecisionContext) -> None:
        """Bind the adapter boundary to one execution identity."""

        validate_context(context)
        self._context = context

    @staticmethod
    def _can_propose(context: DecisionContext) -> bool:
        mode = context.execution.mode.casefold()
        profile = context.execution.profile_name.casefold()
        if mode not in {
            item.casefold() for item in context.policy.allowed_execution_modes
        }:
            return False
        if mode in {"review", "read_only", "readonly", "worker"}:
            return False
        if any(marker in profile for marker in ("review", "qa", "release")):
            return False
        return "orchestrator" in profile or profile in {"default", "factory"}

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    @property
    def reads(self) -> tuple[str, ...]:
        return tuple(self._reads)

    @property
    def proposal(self) -> Mapping[str, Any] | None:
        return copy.deepcopy(self._proposal)

    @property
    def preproposal_receipt_reads(self) -> int:
        return self._preproposal_receipt_reads

    @property
    def postproposal_receipt_reads(self) -> int:
        return self._postproposal_receipt_reads

    @property
    def last_readback(self) -> Mapping[str, Any] | None:
        return copy.deepcopy(self._last_readback)

    @property
    def proposal_attempts(self) -> int:
        return self._proposal_attempts

    def _read(self, name: str, key: str, default: Any) -> Any:
        self._reads.append(name)
        value = self._state.get(key, default)
        return copy.deepcopy(_safe_value(value, f"fixture.{key}"))

    def read_live_state(self) -> dict[str, Any]:
        value = self._read("live", "live", {})
        if not isinstance(value, Mapping):
            raise ContractViolation("fixture live state is malformed")
        return dict(value)

    def read_parent_completion(self) -> dict[str, Any]:
        value = self._read("parent_completion", "parent", {})
        if not isinstance(value, Mapping):
            raise ContractViolation("fixture parent state is malformed")
        return dict(value)

    def read_source_state(self) -> dict[str, Any]:
        value = self._read("source", "source", {})
        if not isinstance(value, Mapping):
            raise ContractViolation("fixture source state is malformed")
        return dict(value)

    def read_ready_lanes(self) -> list[dict[str, Any]]:
        value = self._read("ready_lanes", "ready", [])
        if not isinstance(value, list) or any(
            not isinstance(row, Mapping) for row in value
        ):
            raise ContractViolation("fixture ready-lane state is malformed")
        return [dict(row) for row in value]

    def read_capabilities(self) -> dict[str, Any]:
        value = self._read("capabilities", "capabilities", {})
        if not isinstance(value, Mapping):
            raise ContractViolation("fixture capability state is malformed")
        return dict(value)

    def propose_action(
        self,
        action: str,
        idempotency_key: str,
        target_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one model proposal without changing fixture state."""

        self._proposal_attempts += 1
        if self._proposal is not None:
            raise ContractViolation("fixture accepts only one action proposal")
        action = _safe_identifier(action, "proposal.action")
        idempotency_key = _safe_identifier(idempotency_key, "proposal.idempotency_key")
        if action not in ALLOWED_ACTIONS:
            raise ContractViolation(f"unsupported fixture proposal action: {action}")
        if not {
            "live",
            "parent_completion",
            "source",
            "ready_lanes",
            "capabilities",
        }.issubset(self._reads):
            raise ContractViolation(
                "fixture proposal requires complete pre-proposal observations"
            )
        if self._context is not None and not self._can_propose(self._context):
            raise ContractViolation(
                "execution mode/profile is not permitted to propose an action"
            )
        if self._context is not None and idempotency_key != action_idempotency_key(
            self._context
        ):
            raise ContractViolation(
                "proposal idempotency key is not bound to the current context"
            )
        if self._context is not None:
            allowed_actions = self._context.policy.allowed_actions
            if action not in allowed_actions:
                raise ContractViolation(
                    "proposal action is not allowed by the current policy"
                )
        self._proposal = {
            "action": action,
            "idempotency_key": idempotency_key,
        }
        if target_task_id is not None:
            self._proposal["target_task_id"] = _safe_identifier(
                target_task_id, "proposal.target_task_id"
            )
        self._reads.append("propose_action")
        return {
            "status": "proposal_recorded",
            "action": action,
            "idempotency_key": idempotency_key,
        }

    def accept_external_proposal(
        self,
        proposal: Mapping[str, Any],
        readback: Mapping[str, Any],
        *,
        trace: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        """Reconcile a proposal and receipt emitted by the native child."""

        if not isinstance(proposal, Mapping) or not isinstance(readback, Mapping):
            raise ContractViolation("native fixture proposal/receipt is malformed")
        if self._context is None:
            raise ContractViolation("native fixture proposal is not context-bound")
        if not self._can_propose(self._context):
            raise ContractViolation(
                "execution mode/profile is not permitted to propose an action"
            )
        if trace is None:
            raise ContractViolation(
                "native fixture proposal has no ordered trace proof"
            )
        proposal_index, readback_index = _validate_observation_trace(trace)
        trace_proposal = trace[proposal_index]
        trace_readback = trace[readback_index]
        action = _safe_identifier(proposal.get("action"), "proposal.action")
        if action not in self._context.policy.allowed_actions:
            raise ContractViolation(
                "native fixture proposal action is not allowed by the current policy"
            )
        key = _safe_identifier(
            proposal.get("idempotency_key"), "proposal.idempotency_key"
        )
        if key != action_idempotency_key(self._context):
            raise ContractViolation(
                "native fixture proposal key is not bound to the current context"
            )
        target = proposal.get("target_task_id")
        if trace_proposal.get("action") != action:
            raise ContractViolation("native fixture proposal differs from its trace")
        if trace_proposal.get("idempotency_key") != key:
            raise ContractViolation(
                "native fixture proposal key differs from its trace"
            )
        if trace_proposal.get("target_task_id") != target:
            raise ContractViolation(
                "native fixture proposal target differs from its trace"
            )
        if self._proposal is not None:
            raise ContractViolation("fixture accepts only one action proposal")
        sanitized = _safe_value(dict(readback), "native.readback")
        if not isinstance(sanitized, dict):
            raise ContractViolation("native fixture receipt is malformed")
        trace_receipt = trace_readback.get("receipt")
        if not isinstance(trace_receipt, Mapping) or dict(trace_receipt) != sanitized:
            raise ContractViolation("native fixture receipt differs from its trace")
        if sanitized.get("idempotency_key") != key:
            raise ContractViolation("native fixture receipt identity is stale")
        if sanitized.get("receipt") != "post_proposal":
            raise ContractViolation("native fixture receipt is not post-proposal")
        if sanitized.get("receipt_digest") != _receipt_digest(sanitized):
            raise ContractViolation("native fixture receipt digest is invalid")
        self._proposal_attempts += 1
        self._proposal = {"action": action, "idempotency_key": key}
        if target is not None:
            self._proposal["target_task_id"] = _safe_identifier(
                target, "proposal.target_task_id"
            )
        self._reads.append("propose_action")
        self._external_readback = sanitized

    def read_action_readback(self, idempotency_key: str) -> dict[str, Any]:
        self._reads.append("action_readback")
        idempotency_key = _safe_identifier(idempotency_key, "readback.idempotency_key")
        if self._proposal is None:
            self._preproposal_receipt_reads += 1
            result = {
                "status": "not_started",
                "idempotency_key": idempotency_key,
                "current_run_id": None,
            }
        else:
            if self._proposal["idempotency_key"] != idempotency_key:
                raise ContractViolation("readback requested for a foreign proposal")
            self._postproposal_receipt_reads += 1
            if self._external_readback is not None:
                result = copy.deepcopy(self._external_readback)
            else:
                result = simulated_action_readback(
                    self._proposal["action"],
                    idempotency_key,
                    self._proposal.get("target_task_id"),
                )
        self._last_readback = copy.deepcopy(result)
        return result

    def _forbid(self, operation: str) -> None:
        self.mutation_attempts += 1
        raise ContractViolation(f"fixture adapter forbids mutation: {operation}")

    def create_task(self, *_args: Any, **_kwargs: Any) -> None:
        self._forbid("create_task")

    def update_task(self, *_args: Any, **_kwargs: Any) -> None:
        self._forbid("update_task")

    def write_source(self, *_args: Any, **_kwargs: Any) -> None:
        self._forbid("write_source")

    def set_status(self, *_args: Any, **_kwargs: Any) -> None:
        self._forbid("set_status")

    def unchanged(self) -> bool:
        return self._state == self._baseline


@dataclass(frozen=True)
class DecisionProposal:
    diagnose: Mapping[str, Any]
    choose: Mapping[str, Any]
    act: Mapping[str, Any]
    read_back: Mapping[str, Any]
    advance: Mapping[str, Any]

    @classmethod
    def from_response(cls, response: Mapping[str, Any]) -> DecisionProposal:
        if not isinstance(response, Mapping):
            raise ContractViolation("model response must be an object")
        if set(response) != set(DECISION_LADDER):
            raise ContractViolation(
                "model response must contain exactly the bounded decision ladder"
            )
        values = {}
        for step in DECISION_LADDER:
            value = response.get(step)
            if not isinstance(value, Mapping) or not value:
                raise ContractViolation(
                    f"decision step {step} must be a non-empty object"
                )
            for alternatives in DECISION_REQUIRED_FIELDS[step]:
                valid_fields = [
                    field
                    for field in alternatives
                    if field in value
                    and (
                        (
                            step == "read_back"
                            and field == "current_run_id"
                            and (
                                value[field] is None
                                or (
                                    isinstance(value[field], str)
                                    and bool(value[field].strip())
                                )
                            )
                        )
                        or (
                            isinstance(value[field], str) and bool(value[field].strip())
                        )
                    )
                ]
                if not valid_fields:
                    expected = " or ".join(f"{step}.{field}" for field in alternatives)
                    raise ContractViolation(
                        f"decision step {step} missing required field: {expected}"
                    )
            values[step] = _safe_value(dict(value), step)
        return cls(**values)


@dataclass(frozen=True)
class DecisionResult:
    action: str
    readback: Mapping[str, Any]
    steps: tuple[str, ...]
    prompt: PromptEnvelope
    proposal: DecisionProposal
    new_current_run: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTRACT_SCHEMA,
            "action": self.action,
            "readback": copy.deepcopy(dict(self.readback)),
            "steps": list(self.steps),
            "prompt": self.prompt.as_dict(),
            "new_current_run": self.new_current_run,
        }


def _state_identity_matches(
    state: Mapping[str, Any], context: DecisionContext, *, label: str
) -> None:
    """Reject identity-bearing fixture state that names another lane."""

    source_key = context.source_item.canonical_key
    for field_name, expected in (
        ("source_key", source_key),
        ("canonical_key", source_key),
        ("phase", context.phase),
        ("input_identity", context.input_identity),
        ("semantic_lane", context.semantic_lane),
    ):
        if field_name in state and state[field_name] != expected:
            raise ContractViolation(f"{label} {field_name} does not match context")
    source_item = state.get("source_item")
    if source_item is not None:
        if not isinstance(source_item, Mapping):
            raise ContractViolation(f"{label} source identity is malformed")
        if source_item.get("canonical_key") != source_key:
            raise ContractViolation(f"{label} source identity does not match context")


def _validate_parent_state(context: DecisionContext, parent: Mapping[str, Any]) -> None:
    """Reconcile the post-model parent read with the bound context."""

    if not isinstance(parent, Mapping):
        raise ContractViolation("fixture parent state is malformed")
    expected = context.parent_completion.as_dict()
    for field_name in ("state", "verified", "parent_ids"):
        if parent.get(field_name) != expected[field_name]:
            raise ContractViolation(
                f"parent completion {field_name} does not match context"
            )
    if (
        "evidence_reference" in expected
        and parent.get("evidence_reference") != expected["evidence_reference"]
    ):
        raise ContractViolation("parent completion evidence does not match context")
    _state_identity_matches(parent, context, label="parent completion")


def _validate_fixture_state(
    context: DecisionContext,
    live: Mapping[str, Any],
    source: Mapping[str, Any],
    ready: list[Mapping[str, Any]],
    capabilities: Mapping[str, Any],
) -> None:
    """Validate every state shape used to derive gate precedence."""

    for field_name in ("source_key", "phase", "input_identity"):
        if field_name not in source:
            raise ContractViolation(f"fixture source state omits {field_name}")
    _state_identity_matches(source, context, label="source state")
    if "source_state" not in source or "artifact_state" not in source:
        raise ContractViolation("fixture source state is incomplete")
    source_state = source["source_state"]
    artifact_state = source["artifact_state"]
    if source_state not in _EVIDENCE_STATUSES["source"]:
        raise ContractViolation("fixture source state has an unknown status")
    if artifact_state not in {"ready", "failed"}:
        raise ContractViolation("fixture artifact state is unknown or malformed")
    artifact_task_id = source.get("artifact_task_id")
    if artifact_task_id is not None:
        if not isinstance(artifact_task_id, str) or not artifact_task_id.strip():
            raise ContractViolation("fixture artifact task identity is malformed")
        _safe_identifier(artifact_task_id, "fixture source.artifact_task_id")
    if (
        source_state == "merged"
        and artifact_state == "failed"
        and artifact_task_id is None
    ):
        raise ContractViolation("failed merged artifact has no repair task identity")
    source_evidence = context.evidence.source
    if not any(entry.status == source_state for entry in source_evidence):
        raise ContractViolation("fixture source state is not backed by source evidence")
    for entry in source_evidence:
        attributes = entry.as_dict().get("attributes", {})
        observed_artifact = (
            attributes.get("artifact_state")
            if isinstance(attributes, Mapping)
            else None
        )
        if observed_artifact != artifact_state:
            raise ContractViolation(
                "fixture artifact state is not backed by source evidence"
            )
    identity_fields = (
        ("source_key", context.source_item.canonical_key),
        ("phase", context.phase),
        ("input_identity", context.input_identity),
        ("semantic_lane", context.semantic_lane),
    )
    for state, label in ((live, "lane"), (source, "source")):
        for field_name, expected in identity_fields:
            if field_name not in state or state[field_name] != expected:
                raise ContractViolation(
                    f"{label} state {field_name} does not match context"
                )
    if "current_run_id" not in live:
        raise ContractViolation("fixture lane state has no current-run field")
    if live["current_run_id"] is not None and not isinstance(
        live["current_run_id"], str
    ):
        raise ContractViolation("fixture lane current-run field is malformed")
    if live["current_run_id"] is not None:
        _safe_identifier(live["current_run_id"], "fixture lane current_run_id")
    if live["current_run_id"] != context.execution.run_id:
        raise ContractViolation("fixture lane current-run does not match context")
    if "current_run_id" not in source:
        raise ContractViolation("fixture source state has no current-run field")
    if source["current_run_id"] != context.execution.run_id:
        raise ContractViolation("fixture source current-run does not match context")
    _state_identity_matches(live, context, label="lane state")
    blocker = live.get("blocker")
    if not isinstance(blocker, Mapping):
        raise ContractViolation("fixture blocker state is malformed")
    if any(
        field_name not in blocker
        for field_name in (
            "occurrences",
            "fingerprint",
            "previous_fingerprint",
            "resolved",
        )
    ):
        raise ContractViolation("fixture blocker state is incomplete")
    if (
        not isinstance(blocker["occurrences"], int)
        or isinstance(blocker["occurrences"], bool)
        or blocker["occurrences"] < 0
    ):
        raise ContractViolation("fixture blocker occurrences are malformed")
    if not isinstance(blocker["resolved"], bool):
        raise ContractViolation("fixture blocker resolution is malformed")
    for field_name in ("fingerprint", "previous_fingerprint"):
        value = blocker[field_name]
        if value is not None:
            _safe_identifier(value, f"fixture blocker.{field_name}")
    expected_blocker = context.blocker.as_dict()
    for field_name, expected in expected_blocker.items():
        if blocker.get(field_name) != expected:
            raise ContractViolation(
                f"fixture blocker {field_name} does not match context"
            )
    existing = live.get("existing_action")
    if existing is not None:
        if not isinstance(existing, Mapping):
            raise ContractViolation("fixture existing action is malformed")
        _state_identity_matches(existing, context, label="existing action")
        if existing.get("status") not in {"blocked", "held", "running", "completed"}:
            raise ContractViolation("fixture existing action has an unknown status")
        if "current_run_id" not in existing:
            raise ContractViolation("fixture existing action has no current-run field")
        if existing["current_run_id"] is not None and not isinstance(
            existing["current_run_id"], str
        ):
            raise ContractViolation("fixture existing action current-run is malformed")
        if existing["current_run_id"] is not None:
            _safe_identifier(
                existing["current_run_id"], "fixture existing action current_run_id"
            )
        if "task_id" not in existing or not isinstance(existing["task_id"], str):
            raise ContractViolation("fixture existing action has no task identity")
        _safe_identifier(existing["task_id"], "fixture existing action.task_id")

    seen_tasks: set[str] = set()
    for row in ready:
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ContractViolation("fixture ready lane has no task identity")
        _safe_identifier(task_id, "fixture ready lane.task_id")
        if task_id in seen_tasks or task_id == context.execution.task_id:
            raise ContractViolation(
                "fixture ready lane has a duplicate or current task"
            )
        seen_tasks.add(task_id)
        for field_name, expected in (
            ("phase", context.phase),
            ("input_identity", context.input_identity),
            ("semantic_lane", context.semantic_lane),
        ):
            if field_name in row and row[field_name] != expected:
                raise ContractViolation(
                    f"ready lane {field_name} does not match context"
                )

    if "missing" not in capabilities:
        raise ContractViolation("fixture capability evidence is incomplete")
    missing = capabilities["missing"]
    if not isinstance(missing, list) or any(
        not isinstance(item, str) or not item.strip() for item in missing
    ):
        raise ContractViolation("fixture capability evidence is malformed")
    for item in missing:
        _safe_identifier(item, "fixture missing capability")


def _expected_action(
    context: DecisionContext,
    live: Mapping[str, Any],
    source: Mapping[str, Any],
    ready: list[Mapping[str, Any]],
    capabilities: Mapping[str, Any],
) -> str:
    """Derive the one safe gate from typed state, with explicit precedence."""

    # Artifact provenance is the first gate: a merged source with a failed
    # artifact cannot be hidden by a hold, implementation, or release choice.
    if (
        source.get("source_state") == "merged"
        and source.get("artifact_state") == "failed"
    ):
        return "repair_artifact"
    # An unrelated ready lane keeps moving even while this lane is blocked.
    if ready:
        return "select_independent_lane"
    if capabilities.get("missing"):
        return "hold_missing_capability"

    existing = live.get("existing_action")
    if isinstance(existing, Mapping) and existing.get("status") == "blocked":
        if not context.blocker.fingerprint:
            raise ContractViolation(
                "existing blocked action has no blocker fingerprint"
            )
        bound_fields = {
            "source_key": context.source_item.canonical_key,
            "phase": context.phase,
            "input_identity": context.input_identity,
            "blocker_fingerprint": context.blocker.fingerprint,
            "semantic_lane": context.semantic_lane,
        }
        if all(
            field_name in existing and existing.get(field_name) == expected
            for field_name, expected in bound_fields.items()
        ):
            return "reuse_existing"
        if context.policy.require_exact_reuse_binding:
            raise ContractViolation("existing blocked action is not bound to this lane")

    if (
        context.blocker.occurrences >= context.policy.repeated_blocker_threshold
        and context.blocker.fingerprint
        and context.blocker.fingerprint == context.blocker.previous_fingerprint
        and not context.blocker.resolved
    ):
        return "quarantine"
    if context.blocker.resolved or (
        context.blocker.fingerprint
        and context.blocker.fingerprint != context.blocker.previous_fingerprint
    ):
        return "admit"
    return "hold"


def _validate_admission_guards(
    context: DecisionContext,
    live: Mapping[str, Any],
    source: Mapping[str, Any],
) -> None:
    """Fail closed before allocating a current run."""

    if context.execution.run_id is not None:
        raise ContractViolation("admission requires current_run_id=null before spawn")
    if (
        context.parent_completion.state != "complete"
        or not context.parent_completion.verified
    ):
        raise ContractViolation("admission requires verified parent completion")
    if context.prior_decision:
        raise ContractViolation("admission cannot reuse historical decision progress")
    for evidence in context.evidence.worker:
        if evidence.status != "not_started" or evidence.run_id is not None:
            raise ContractViolation(
                "admission has historical or active worker progress"
            )
    if context.policy.require_independent_review and (
        not context.evidence.review
        or any(evidence.status != "approved" for evidence in context.evidence.review)
    ):
        raise ContractViolation(
            "admission requires independent approved review evidence"
        )
    if not context.execution.credentials_verified:
        raise ContractViolation("admission requires verified credentials")
    if context.execution.production and not context.execution.production_approved:
        raise ContractViolation("production admission requires explicit approval")
    if context.execution.retry_count < 0:
        raise ContractViolation("admission retry count is invalid")

    for state, label in ((live, "lane"), (source, "source")):
        if "current_run_id" not in state or state["current_run_id"] is not None:
            raise ContractViolation(
                f"admission {label} current_run_id must be explicitly null"
            )
    identity_fields = (
        ("source_key", context.source_item.canonical_key),
        ("phase", context.phase),
        ("input_identity", context.input_identity),
        ("semantic_lane", context.semantic_lane),
    )
    for state, label in ((live, "lane"), (source, "source")):
        for field_name, expected in identity_fields:
            if field_name not in state or state[field_name] != expected:
                raise ContractViolation(
                    f"admission {label} {field_name} does not match context"
                )
    existing = live.get("existing_action")
    if isinstance(existing, Mapping) and (
        "current_run_id" not in existing or existing.get("current_run_id") is not None
    ):
        raise ContractViolation("admission cannot bypass an existing current run")
    guard_fields = (
        ("singleton_key", context.execution.singleton_key),
        ("tenant", context.execution.tenant),
        ("branch", context.execution.branch),
        ("credentials_verified", context.execution.credentials_verified),
        ("production", context.execution.production),
        ("production_approved", context.execution.production_approved),
        ("retry_count", context.execution.retry_count),
    )
    for field_name, expected in guard_fields:
        if field_name not in live or live[field_name] != expected:
            raise ContractViolation(f"admission {field_name} does not match context")


def _validate_action_semantics(
    context: DecisionContext,
    proposal: DecisionProposal,
    readback: Mapping[str, Any],
    adapter: NoSideEffectFixtureAdapter,
) -> str:
    choice_action = _required(proposal.choose.get("action"), "choose.action")
    act_action = _required(proposal.act.get("action"), "act.action")
    action = choice_action
    if action != act_action:
        raise ContractViolation(
            "decision conflict: choose and act name different actions"
        )
    if action not in context.policy.allowed_actions:
        raise ContractViolation(f"unsupported action: {action}")

    recorded_proposal = adapter.proposal
    if recorded_proposal is None:
        raise ContractViolation("decision has no recorded fixture proposal")
    if recorded_proposal.get("action") != action:
        raise ContractViolation("decision conflict: recorded proposal action differs")
    if recorded_proposal.get("idempotency_key") != proposal.act.get("idempotency_key"):
        raise ContractViolation(
            "decision conflict: recorded proposal key differs from the response"
        )
    if recorded_proposal.get("target_task_id") != proposal.act.get("target_task_id"):
        raise ContractViolation(
            "decision conflict: recorded proposal target differs from the response"
        )

    parent = adapter.read_parent_completion()
    _validate_parent_state(context, parent)
    live = adapter.read_live_state()
    source = adapter.read_source_state()
    ready = adapter.read_ready_lanes()
    capabilities = adapter.read_capabilities()
    _validate_fixture_state(context, live, source, ready, capabilities)
    expected_action = _expected_action(context, live, source, ready, capabilities)
    if action != expected_action:
        raise ContractViolation(
            "decision action does not match the required gate "
            f"(expected={expected_action!r}, observed={action!r})"
        )
    choose_target = proposal.choose.get("target_task_id")
    act_target = proposal.act.get("target_task_id")
    if choose_target != act_target:
        raise ContractViolation("decision conflict: choose and act targets differ")
    if (
        action in {"admit", "quarantine", "hold_missing_capability", "hold"}
        and choose_target is not None
        and choose_target != context.execution.task_id
    ):
        raise ContractViolation(f"{action} target is not bound to the current task")
    if action == "select_independent_lane" and (
        not isinstance(choose_target, str) or not choose_target.strip()
    ):
        raise ContractViolation("independent-lane selection needs a target task")
    if action == "reuse_existing":
        existing_target = (live.get("existing_action") or {}).get("task_id")
        if choose_target != existing_target:
            raise ContractViolation("reuse target is not the bound existing task")
    if action == "repair_artifact":
        artifact_target = source.get("artifact_task_id")
        if not isinstance(artifact_target, str) or not artifact_target.strip():
            raise ContractViolation("artifact repair lacks a failed artifact task")
        if choose_target != artifact_target:
            raise ContractViolation(
                "artifact repair target is not the failed artifact task"
            )

    expected_key = action_idempotency_key(context)
    if proposal.act.get("idempotency_key") != expected_key:
        raise ContractViolation(
            "decision conflict: idempotency key does not match context "
            f"(expected={expected_key!r}, observed={proposal.act.get('idempotency_key')!r})"
        )
    if proposal.read_back.get("idempotency_key") != expected_key:
        raise ContractViolation(
            "decision conflict: model readback key is not current "
            f"(expected={expected_key!r}, observed={proposal.read_back.get('idempotency_key')!r})"
        )
    if readback.get("idempotency_key") != expected_key:
        raise ContractViolation("readback conflict: adapter returned another action")
    if readback.get("action") != action:
        raise ContractViolation(
            "readback conflict: adapter action differs from proposal"
        )
    if readback.get("receipt") != "post_proposal":
        raise ContractViolation(
            "readback is not an authenticated post-proposal receipt"
        )
    if readback.get("receipt_digest") != _receipt_digest(readback):
        raise ContractViolation("readback receipt digest is invalid")

    if dict(proposal.read_back) != dict(readback):
        raise ContractViolation(
            "readback conflict: model did not copy the exact receipt payload"
        )

    claimed_status = _required(proposal.read_back.get("status"), "read_back.status")
    actual_status = _required(readback.get("status"), "adapter.readback.status")
    if claimed_status != actual_status:
        raise ContractViolation("readback conflict: model status differs from adapter")

    claimed_run = proposal.read_back.get("current_run_id")
    actual_run = readback.get("current_run_id")
    if claimed_run != actual_run:
        raise ContractViolation("readback conflict: model run differs from adapter")
    for field_name in (
        "action",
        "status",
        "idempotency_key",
        "current_run_id",
        "admission_count",
        "selected_task_id",
        "receipt",
        "receipt_digest",
    ):
        if (
            field_name in readback
            and proposal.read_back.get(field_name) != readback[field_name]
        ):
            raise ContractViolation(
                f"readback conflict: model did not copy exact {field_name} payload"
            )

    expected_statuses = {
        "admit": {"admitted", "created"},
        "quarantine": {"quarantined"},
        "reuse_existing": {"reused"},
        "select_independent_lane": {"selected"},
        "repair_artifact": {"artifact-remediation"},
        "hold_missing_capability": {"held"},
        "hold": {"held"},
    }
    if actual_status not in expected_statuses[action]:
        raise ContractViolation(
            f"readback status {actual_status!r} is invalid for action {action!r}"
        )

    if action == "admit":
        _validate_admission_guards(context, live, source)
        if not (
            context.blocker.resolved
            or (
                context.blocker.fingerprint
                and context.blocker.fingerprint != context.blocker.previous_fingerprint
            )
        ):
            raise ContractViolation(
                "admission requires blocker resolution or a new contract"
            )
        if not actual_run:
            raise ContractViolation("new admission must return a current run")
        if readback.get("admission_count", 1) != 1:
            raise ContractViolation(
                "new admission must allocate exactly one current run"
            )
    else:
        if actual_run is not None:
            raise ContractViolation(f"{action} cannot claim a current run")

    if action == "quarantine":
        blocker = live.get("blocker", {})
        if not (
            blocker.get("occurrences", 0) >= context.policy.repeated_blocker_threshold
            and blocker.get("fingerprint")
            and blocker.get("fingerprint") == blocker.get("previous_fingerprint")
            and not blocker.get("resolved")
        ):
            raise ContractViolation("quarantine lacks an unchanged repeated blocker")

    if action == "reuse_existing":
        existing = live.get("existing_action") or {}
        if not (
            existing.get("status") == "blocked"
            and existing.get("current_run_id") is None
        ):
            raise ContractViolation(
                "reuse requires a blocked existing action with null run"
            )

    if action == "select_independent_lane":
        target = proposal.choose.get("target_task_id")
        if (
            not ready
            or not target
            or target not in {row.get("task_id") for row in ready}
        ):
            raise ContractViolation("independent-lane selection lacks a ready target")
        if readback.get("selected_task_id") != target:
            raise ContractViolation(
                "independent-lane readback target differs from proposal"
            )

    if action == "repair_artifact" and not (
        source.get("source_state") == "merged"
        and source.get("artifact_state") == "failed"
    ):
        raise ContractViolation("artifact remediation lacks merged/failed evidence")

    if action == "hold_missing_capability" and not capabilities.get("missing"):
        raise ContractViolation("capability hold lacks a missing capability")

    next_phase = _required(proposal.advance.get("next_phase"), "advance.next_phase")
    transition = dict(context.policy.transition_policy)
    configured_next_phase = transition.get(action)
    if configured_next_phase is None:
        raise ContractViolation(f"policy has no transition for action {action!r}")
    expected_next_phase = (
        context.phase
        if configured_next_phase == "current_phase"
        else configured_next_phase
    )
    if next_phase != expected_next_phase:
        raise ContractViolation(
            "advance.next_phase does not match transition policy "
            f"(expected={expected_next_phase!r}, observed={next_phase!r})"
        )
    if action == "repair_artifact" and next_phase == "release":
        raise ContractViolation("failed artifact cannot advance to release")
    return action


def evaluate_decision(
    model: DecisionModel,
    context: DecisionContext,
    adapter: NoSideEffectFixtureAdapter,
    skills: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
    *,
    prompt_suffix: str = "",
) -> DecisionResult:
    """Run one bounded model/tool decision and verify its read-only readback."""

    validate_context(context)
    adapter.bind_context(context)
    tools: dict[str, Callable[..., Any]] = {
        "read_live_state": adapter.read_live_state,
        "read_parent_completion": adapter.read_parent_completion,
        "read_source_state": adapter.read_source_state,
        "read_ready_lanes": adapter.read_ready_lanes,
        "read_capabilities": adapter.read_capabilities,
        "propose_action": adapter.propose_action,
        "read_action_readback": adapter.read_action_readback,
    }
    prompt = prepare_prompt(
        context,
        skills,
        prompt_suffix=prompt_suffix,
        tool_catalog=tools,
    )
    response = model.complete(prompt.prompt, tools)
    proposal = DecisionProposal.from_response(response)
    external_proposal = False
    native_receipt = getattr(model, "native_fixture_receipt", None)
    if adapter.proposal is None:
        if native_receipt is None:
            raise ContractViolation(
                "model did not commit exactly one fixture proposal; controller will not auto-propose"
            )
        if (
            not isinstance(native_receipt, tuple)
            or len(native_receipt) != 2
            or not isinstance(native_receipt[0], Mapping)
            or not isinstance(native_receipt[1], Mapping)
        ):
            raise ContractViolation(
                "native model proposal/receipt handoff is malformed"
            )
        native_trace = getattr(model, "native_trace", None)
        if not isinstance(native_trace, tuple):
            raise ContractViolation("native model proposal has no ordered trace proof")
        adapter.accept_external_proposal(
            native_receipt[0], native_receipt[1], trace=native_trace
        )
        external_proposal = True
    elif native_receipt is not None:
        raise ContractViolation(
            "model supplied both a local and an external fixture proposal"
        )
    if adapter.proposal_attempts != 1 or adapter.proposal is None:
        raise ContractViolation("model must commit exactly one fixture proposal")
    if adapter.preproposal_receipt_reads:
        raise ContractViolation("model read action state before proposing")
    key = action_idempotency_key(context)
    if external_proposal:
        # The child trace has already proven its one post-proposal read.  The
        # parent adapter performs one corresponding exact receipt readback.
        actual_readback = adapter.read_action_readback(key)
    else:
        if adapter.postproposal_receipt_reads != 1 or adapter.last_readback is None:
            raise ContractViolation(
                "model must perform exactly one post-proposal action readback"
            )
        actual_readback = adapter.last_readback
    action = _validate_action_semantics(context, proposal, actual_readback, adapter)
    return DecisionResult(
        action=action,
        readback=copy.deepcopy(dict(actual_readback)),
        steps=DECISION_LADDER,
        prompt=prompt,
        proposal=proposal,
        new_current_run=bool(
            action == "admit" and actual_readback.get("current_run_id")
        ),
    )


__all__ = [
    "ALLOWED_ACTIONS",
    "CONTRACT_SCHEMA",
    "DECISION_LADDER",
    "DECISION_REQUIRED_FIELDS",
    "BlockerState",
    "ContractViolation",
    "DecisionContext",
    "DecisionModel",
    "DecisionPolicy",
    "DecisionProposal",
    "DecisionResult",
    "EvidenceBundle",
    "ExecutionIdentity",
    "NoSideEffectFixtureAdapter",
    "ParentCompletion",
    "PromptEnvelope",
    "SourceIdentity",
    "TypedEvidence",
    "action_idempotency_key",
    "build_input_identity",
    "build_prompt",
    "conflict_checks",
    "decision_response_requirements_text",
    "evaluate_decision",
    "prepare_prompt",
    "simulated_action_readback",
    "validate_context",
]
