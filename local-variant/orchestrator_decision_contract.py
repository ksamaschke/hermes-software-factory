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
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

CONTRACT_SCHEMA = "factory.decision.v1"
DECISION_LADDER = ("diagnose", "choose", "act", "read_back", "advance")
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

_SECRET_KEY = re.compile(
    r"(?:token|password|passwd|secret|cookie|authorization|raw[_ -]?(?:log|output))",
    re.IGNORECASE,
)


class ContractViolation(ValueError):
    """Raised when a context, proposal, budget, or readback is unsafe."""


class DecisionModel(Protocol):
    """Minimal model adapter used by the evaluator."""

    def complete(
        self, prompt: str, tools: Mapping[str, Callable[..., Any]]
    ) -> Mapping[str, Any]: ...


def _required(value: Any, field_name: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ContractViolation(f"missing required field: {field_name}")
    return value


def _safe_value(value: Any, field_name: str = "value") -> Any:
    """Copy JSON-like values while rejecting secret/unbounded-log fields."""

    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY.search(key_text):
                raise ContractViolation(f"unsafe field in {field_name}: {key_text}")
            result[key_text] = _safe_value(item, f"{field_name}.{key_text}")
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, field_name) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ContractViolation(f"unsupported value in {field_name}")


def _compact(text: str) -> str:
    return " ".join(str(text).split())


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
        return ":".join(
            (
                _required(self.tracker, "source_item.tracker"),
                _required(self.project, "source_item.project"),
                _required(self.kind, "source_item.kind"),
                _required(self.item_key, "source_item.item_key"),
            )
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "tracker": _required(self.tracker, "source_item.tracker"),
            "project": _required(self.project, "source_item.project"),
            "kind": _required(self.kind, "source_item.kind"),
            "item_key": _required(self.item_key, "source_item.item_key"),
            "canonical_key": self.canonical_key,
        }
        if self.url:
            result["url"] = str(self.url)
        return result


def build_input_identity(
    source_item: SourceIdentity,
    phase: str,
    input_payload: Mapping[str, Any],
) -> str:
    """Return a stable identity for one source/phase/input observation."""

    document = {
        "source_item": source_item.as_dict(),
        "phase": _required(phase, "phase"),
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": _required(self.mode, "execution_mode"),
            "profile_name": _required(self.profile_name, "profile_name"),
            "current_task": {"id": _required(self.task_id, "current_task.id")},
            "current_run": {"id": self.run_id} if self.run_id else None,
        }


@dataclass(frozen=True)
class BlockerState:
    """Evidence-derived blocker state, not a title/body heuristic."""

    fingerprint: str | None = None
    previous_fingerprint: str | None = None
    occurrences: int = 0
    resolved: bool = False

    def as_dict(self) -> dict[str, Any]:
        if self.occurrences < 0:
            raise ContractViolation("blocker occurrences cannot be negative")
        return {
            "fingerprint": self.fingerprint,
            "previous_fingerprint": self.previous_fingerprint,
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
        state = _required(self.state, "parent_completion.state")
        if state not in {"complete", "incomplete", "none", "unknown"}:
            raise ContractViolation(f"invalid parent completion state: {state}")
        result: dict[str, Any] = {
            "state": state,
            "verified": bool(self.verified),
            "parent_ids": list(self.parent_ids),
        }
        if self.evidence_reference:
            result["evidence_reference"] = str(self.evidence_reference)
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

    def as_dict(self) -> dict[str, Any]:
        kind = _required(self.kind, "evidence.kind")
        if kind not in EVIDENCE_KINDS:
            raise ContractViolation(f"invalid evidence kind: {kind}")
        result: dict[str, Any] = {
            "kind": kind,
            "subject": _required(self.subject, f"evidence.{kind}.subject"),
            "status": _required(self.status, f"evidence.{kind}.status"),
            "reference": _required(self.reference, f"evidence.{kind}.reference"),
        }
        if self.run_id is not None:
            result["run_id"] = str(self.run_id)
        if self.candidate is not None:
            result["candidate"] = str(self.candidate)
        if self.attributes:
            result["attributes"] = _safe_value(
                dict(self.attributes), f"evidence.{kind}.attributes"
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

    def as_dict(self) -> dict[str, Any]:
        if self.max_prompt_chars <= 0 or self.max_skill_chars < 0:
            raise ContractViolation("prompt and skill budgets must be non-negative")
        if self.max_skills < 0 or self.repeated_blocker_threshold < 1:
            raise ContractViolation("invalid skill or blocker policy bound")
        if self.conflict_mode != "fail_closed":
            raise ContractViolation("conflict mode must be fail_closed")
        allowed = list(self.allowed_actions)
        if not allowed or not set(allowed) <= set(ALLOWED_ACTIONS):
            raise ContractViolation("policy contains an unsupported action")
        return {
            "budgets": {
                "max_prompt_chars": self.max_prompt_chars,
                "max_skill_chars": self.max_skill_chars,
                "max_skills": self.max_skills,
            },
            "repeated_blocker_threshold": self.repeated_blocker_threshold,
            "conflict_mode": self.conflict_mode,
            "allowed_actions": allowed,
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

    def as_dict(self) -> dict[str, Any]:
        execution = self.execution.as_dict()
        result: dict[str, Any] = {
            "schema": CONTRACT_SCHEMA,
            "execution_mode": execution["mode"],
            "profile_name": execution["profile_name"],
            "current_task": execution["current_task"],
            "current_run": execution["current_run"],
            "source_item": self.source_item.as_dict(),
            "phase": _required(self.phase, "phase"),
            "input_identity": _required(self.input_identity, "input_identity"),
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
            _required(context.phase, "phase"),
            _required(context.input_identity, "input_identity"),
        )
    )
    return "action-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def conflict_checks(context: DecisionContext) -> tuple[str, ...]:
    """Return contradictions that must prevent a model call."""

    conflicts: list[str] = []
    try:
        context.execution.as_dict()
        context.source_item.as_dict()
        context.parent_completion.as_dict()
        context.evidence.as_dict()
        context.policy.as_dict()
    except ContractViolation as exc:
        return (str(exc),)

    if context.blocker.occurrences and not context.blocker.fingerprint:
        conflicts.append("blocker fingerprint is missing for an observed blocker")
    if (
        context.parent_completion.state == "complete"
        and not context.parent_completion.verified
    ):
        conflicts.append("parent completion is marked complete but is not verified")

    worker_runs = {
        evidence.run_id for evidence in context.evidence.worker if evidence.run_id
    }
    if context.execution.run_id and worker_runs - {context.execution.run_id}:
        conflicts.append("worker evidence run does not match current run")

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

    _required(context.phase, "phase")
    _required(context.input_identity, "input_identity")
    conflicts = conflict_checks(context)
    if conflicts:
        raise ContractViolation("context conflict: " + "; ".join(conflicts))


@dataclass(frozen=True)
class PromptEnvelope:
    prompt: str
    prompt_chars: int
    skill_chars: int
    selected_skills: tuple[str, ...]
    omitted_skills: tuple[str, ...]
    shrunk: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_chars": self.prompt_chars,
            "skill_chars": self.skill_chars,
            "selected_skills": list(self.selected_skills),
            "omitted_skills": list(self.omitted_skills),
            "shrunk": self.shrunk,
        }


def _skill_entries(
    skills: Mapping[str, str] | Sequence[tuple[str, str]] | None,
) -> list[tuple[str, str]]:
    if skills is None:
        return []
    if isinstance(skills, Mapping):
        entries = list(skills.items())
    else:
        entries = list(skills)
    normalized = []
    for name, text in entries:
        normalized.append((_required(name, "skill.name"), str(text)))
    return normalized


def prepare_prompt(
    context: DecisionContext,
    skills: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
) -> PromptEnvelope:
    """Build an effective bounded prompt and report what was omitted/trimmed."""

    validate_context(context)
    policy = context.policy
    document = json.dumps(context.as_dict(), sort_keys=True, separators=(",", ":"))
    instructions = (
        "You are the bounded factory decision model. Use only the typed context "
        "and read-only tools. Do not infer identity from title/body text or unbounded "
        "logs. Return exactly the five ladder objects diagnose, choose, act, "
        "read_back, advance. Diagnose before choosing; choose before acting; "
        "read back the exact idempotent result before advancing. A null current "
        "run means not started/reused/held, never a new run. Conflicts fail closed."
    )
    prefix = f"{instructions}\n\nCONTEXT_JSON\n{document}\nEND_CONTEXT\n"
    entries = _skill_entries(skills)
    names = [name for name, _ in entries]
    omitted: list[str] = names[policy.max_skills :]
    entries = entries[: policy.max_skills]

    # Reserve room for the fixed contract and context. Skills are optional
    # context, so shrinking them never weakens the typed contract itself.
    skill_budget = policy.max_skill_chars
    prompt_budget = policy.max_prompt_chars
    remaining_prompt = prompt_budget - len(prefix) - len("\nSKILLS\n")
    if remaining_prompt < 0:
        raise ContractViolation("prompt budget is smaller than required context")
    effective_skill_budget = min(skill_budget, remaining_prompt)

    skill_lines: list[str] = []
    selected: list[str] = []
    shrunk = False
    remaining = effective_skill_budget
    for index, (name, text) in enumerate(entries):
        compacted = _compact(text)
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
    prompt = prefix + "\nSKILLS\n" + skill_text
    if len(prompt) > prompt_budget:
        # Defensive correction for separators/name accounting. Never silently
        # exceed the effective limit.
        overflow = len(prompt) - prompt_budget
        skill_text, was_shrunk = _bounded_text(
            skill_text, max(0, len(skill_text) - overflow)
        )
        prompt = prefix + "\nSKILLS\n" + skill_text
        shrunk = True
    if len(prompt) > prompt_budget:
        raise ContractViolation("effective prompt budget exceeded")

    return PromptEnvelope(
        prompt=prompt,
        prompt_chars=len(prompt),
        skill_chars=len(skill_text),
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


class NoSideEffectFixtureAdapter:
    """Read-only fixture adapter for behavioral evaluations.

    The adapter returns copies, records reads, and has no mutation method in the
    tool registry. Direct calls to common mutation names fail loudly and count an
    attempted write, making accidental side effects observable in tests.
    """

    def __init__(self, state: Mapping[str, Any]) -> None:
        self._state = copy.deepcopy(dict(state))
        self._baseline = copy.deepcopy(self._state)
        self._reads: list[str] = []
        self.mutation_attempts = 0

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    @property
    def reads(self) -> tuple[str, ...]:
        return tuple(self._reads)

    def _read(self, name: str, key: str, default: Any) -> Any:
        self._reads.append(name)
        return copy.deepcopy(self._state.get(key, default))

    def read_live_state(self) -> dict[str, Any]:
        return self._read("live", "live", {})

    def read_parent_completion(self) -> dict[str, Any]:
        return self._read("parent_completion", "parent", {})

    def read_source_state(self) -> dict[str, Any]:
        return self._read("source", "source", {})

    def read_ready_lanes(self) -> list[dict[str, Any]]:
        return self._read("ready_lanes", "ready", [])

    def read_capabilities(self) -> dict[str, Any]:
        return self._read("capabilities", "capabilities", {})

    def read_action_readback(self, idempotency_key: str) -> dict[str, Any]:
        self._reads.append("action_readback")
        readbacks = self._state.get("readbacks", {})
        result = readbacks.get(idempotency_key)
        if result is None:
            return {
                "status": "not_started",
                "idempotency_key": idempotency_key,
                "current_run_id": None,
            }
        return copy.deepcopy(result)

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

    claimed_status = _required(proposal.read_back.get("status"), "read_back.status")
    actual_status = _required(readback.get("status"), "adapter.readback.status")
    if claimed_status != actual_status:
        raise ContractViolation("readback conflict: model status differs from adapter")

    claimed_run = proposal.read_back.get("current_run_id")
    actual_run = readback.get("current_run_id")
    if claimed_run != actual_run:
        raise ContractViolation("readback conflict: model run differs from adapter")

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
        if (
            context.parent_completion.state != "complete"
            or not context.parent_completion.verified
        ):
            raise ContractViolation("admission requires verified parent completion")
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
        blocker = adapter.read_live_state().get("blocker", {})
        if not (
            blocker.get("occurrences", 0) >= context.policy.repeated_blocker_threshold
            and blocker.get("fingerprint")
            and blocker.get("fingerprint") == blocker.get("previous_fingerprint")
            and not blocker.get("resolved")
        ):
            raise ContractViolation("quarantine lacks an unchanged repeated blocker")

    if action == "reuse_existing":
        existing = adapter.read_live_state().get("existing_action") or {}
        if not (
            existing.get("status") == "blocked"
            and existing.get("current_run_id") is None
        ):
            raise ContractViolation(
                "reuse requires a blocked existing action with null run"
            )

    if action == "select_independent_lane":
        ready = adapter.read_ready_lanes()
        target = proposal.choose.get("target_task_id")
        if (
            not ready
            or not target
            or target not in {row.get("task_id") for row in ready}
        ):
            raise ContractViolation("independent-lane selection lacks a ready target")

    if action == "repair_artifact":
        source = adapter.read_source_state()
        if not (
            source.get("source_state") == "merged"
            and source.get("artifact_state") == "failed"
        ):
            raise ContractViolation("artifact remediation lacks merged/failed evidence")

    if action == "hold_missing_capability" and not adapter.read_capabilities().get(
        "missing"
    ):
        raise ContractViolation("capability hold lacks a missing capability")

    next_phase = _required(proposal.advance.get("next_phase"), "advance.next_phase")
    if action == "repair_artifact" and next_phase == "release":
        raise ContractViolation("failed artifact cannot advance to release")
    return action


def evaluate_decision(
    model: DecisionModel,
    context: DecisionContext,
    adapter: NoSideEffectFixtureAdapter,
    skills: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
) -> DecisionResult:
    """Run one bounded model/tool decision and verify its read-only readback."""

    validate_context(context)
    prompt = prepare_prompt(context, skills)
    tools: dict[str, Callable[..., Any]] = {
        "read_live_state": adapter.read_live_state,
        "read_parent_completion": adapter.read_parent_completion,
        "read_source_state": adapter.read_source_state,
        "read_ready_lanes": adapter.read_ready_lanes,
        "read_capabilities": adapter.read_capabilities,
        "read_action_readback": adapter.read_action_readback,
    }
    response = model.complete(prompt.prompt, tools)
    proposal = DecisionProposal.from_response(response)
    key = action_idempotency_key(context)
    actual_readback = adapter.read_action_readback(key)
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
    "evaluate_decision",
    "prepare_prompt",
    "validate_context",
]
