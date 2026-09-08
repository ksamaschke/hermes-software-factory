"""Native Hermes evaluation for the generic orchestrator decision contract.

The unit tests use a deterministic model double.  This module is the separate
integration surface for the real model path: it installs the generic role
prompt into an ephemeral profile, exposes only read-only fixture tools over
MCP, invokes native Hermes in one-shot mode, and validates the returned ladder
with the same contract used by the unit tests.  The fixture server records
proposals locally for causal validation, but exposes no live mutation method;
the ephemeral profile is removed when the run ends.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

try:  # Running as a package is useful to downstream installers.
    from .orchestrator_decision_contract import (
        BlockerState,
        ContractViolation,
        DecisionContext,
        DecisionModel,
        DecisionPolicy,
        EvidenceBundle,
        ExecutionIdentity,
        NoSideEffectFixtureAdapter,
        ParentCompletion,
        SourceIdentity,
        TypedEvidence,
        _receipt_digest,
        _safe_identifier,
        _safe_value,
        _validate_observation_trace,
        action_idempotency_key,
        build_input_identity,
        decision_response_requirements_text,
        evaluate_decision,
        simulated_action_readback,
    )
except ImportError:  # Running this file directly is the supported CLI path.
    from orchestrator_decision_contract import (  # type: ignore[no-redef]
        BlockerState,
        ContractViolation,
        DecisionContext,
        DecisionModel,
        DecisionPolicy,
        EvidenceBundle,
        ExecutionIdentity,
        NoSideEffectFixtureAdapter,
        ParentCompletion,
        SourceIdentity,
        TypedEvidence,
        _receipt_digest,
        _safe_identifier,
        _safe_value,
        _validate_observation_trace,
        action_idempotency_key,
        build_input_identity,
        decision_response_requirements_text,
        evaluate_decision,
        simulated_action_readback,
    )


READ_TOOL_NAMES = (
    "read_live_state",
    "read_parent_completion",
    "read_source_state",
    "read_ready_lanes",
    "read_capabilities",
    "read_action_readback",
)
FIXTURE_TOOL_NAMES = (*READ_TOOL_NAMES, "propose_action")
_NATIVE_SCANNER_NOTICE = (
    "⚠ tirith security scanner enabled but not available — "
    "command scanning will use pattern matching only"
)

NATIVE_QUERY_SUFFIX = f"""
This is a native integration evaluation. Use the read-only MCP fixture tools,
not assumptions or prose. Call every read_* tool at least once. First diagnose
and choose, then call propose_action exactly once with the chosen action and the
idempotency_key from CONTEXT_JSON; this records a fixture-only proposal and is
not a live mutation. Do not call read_action_readback before that proposal. Only
after the proposal, call read_action_readback with the same key and copy the
complete returned receipt payload, including action, receipt, receipt_digest,
admission_count, and any selected_task_id. Never call a mutation, shell, file,
memory, or network tool.
Return exactly one JSON object matching this minimum shape; the required fields
are: {decision_response_requirements_text()}.
The read_back status and current_run_id must be copied from the post-proposal
fixture read. The advance.next_phase is derived from the typed transition
policy and current phase/action (it is not fixture-observed):
{{"diagnose":{{"summary":"..."}},"choose":{{"action":"..."}},
"act":{{"action":"...","idempotency_key":"..."}},
"read_back":{{"idempotency_key":"...","status":"...","current_run_id":null}},
"advance":{{"next_phase":"..."}}}}
The gate precedence is typed and must be applied before any proposal: a merged
source with a failed artifact selects repair_artifact; otherwise an unrelated
ready lane selects select_independent_lane; otherwise missing capabilities select
hold_missing_capability; otherwise a bound blocked action selects reuse_existing;
otherwise an unchanged blocker at or above repeated_blocker_threshold remains
quarantined; otherwise a resolved or newly changed blocker may be admitted; and
otherwise hold. These are contract rules, not case labels or an answer oracle.
The allowed actions are quarantine, admit, reuse_existing, select_independent_lane,
repair_artifact, hold_missing_capability, or hold. The choose and act actions
must match the one proposal and the read_back fields must match the post-proposal
fixture read. Keep current_run_id null unless the fixture explicitly returns a
newly admitted run. For select_independent_lane, include choose.target_task_id
copied exactly from read_ready_lanes. For reuse_existing, copy the bound
existing_action.task_id; for repair_artifact, copy source.artifact_task_id.
The act target must match choose.target_task_id. A denied tool or capability is
evidence for a bounded hold; do not retry a denied command with altered syntax
or weaken its approval boundary. Return JSON only, with no markdown or
explanatory text.
""".strip()


_ISOLATION_EXACT_ENV_KEYS = frozenset(
    {
        "HERMES_HOME",
        "HERMES_CONFIG",
        "HERMES_ENV",
        "HERMES_PROFILE",
        "HERMES_PROFILE_NAME",
        "HERMES_YOLO_MODE",
        "HERMES_ACCEPT_HOOKS",
        "HERMES_INTERACTIVE",
        "HERMES_TUI",
        "HERMES_SAFE_MODE",
        "HERMES_IGNORE_USER_CONFIG",
        "HERMES_IGNORE_RULES",
        "HERMES_TENANT",
        "HERMES_PROJECT",
        "TERMINAL_CWD",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "HOME",
        "TMPDIR",
    }
)
_ISOLATION_ENV_PREFIXES = (
    "HERMES_KANBAN_",
    "HERMES_SESSION_",
    "HERMES_CRON_",
    "FACTORY_EVAL_",
)
_SAFE_CHILD_ENV_KEYS = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NUMERIC",
        "LC_TIME",
        "NO_COLOR",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        "TERM",
        "TZ",
    }
)
_SAFE_CHILD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def build_isolated_environment(
    parent_env: Mapping[str, str], profile: Path, board_path: Path
) -> dict[str, str]:
    """Return a child environment with no inherited factory authority.

    The native child intentionally receives a temporary board *path* so any
    accidental lifecycle lookup remains local.  Parent task/run/claim/session
    variables are removed rather than copied, and the profile is a direct
    ``HERMES_HOME`` rather than a named profile selected through the operator's
    Hermes root.  The function is pure apart from path normalization, which
    makes its isolation contract directly testable before a model is launched.
    """

    profile = Path(profile).expanduser().resolve()
    board_path = Path(board_path).expanduser().resolve()
    if not profile.is_dir():
        raise NativeEvaluationUnavailable(
            "isolated Hermes profile directory is missing"
        )

    protected_roots: list[Path] = []
    inherited_board = str(parent_env.get("HERMES_KANBAN_DB", "")).strip()
    if inherited_board:
        try:
            protected_roots.append(Path(inherited_board).expanduser().resolve().parent)
        except OSError:
            # An invalid inherited path is still discarded below; it must not
            # prevent creation of a valid isolated child environment.
            pass
    inherited_home = str(parent_env.get("HERMES_KANBAN_HOME", "")).strip()
    if inherited_home:
        try:
            protected_roots.append(Path(inherited_home).expanduser().resolve())
        except OSError:
            pass
    if any(
        board_path == root or root in board_path.parents for root in protected_roots
    ):
        raise NativeEvaluationUnavailable(
            "isolated board path overlaps inherited board authority"
        )

    # Use an allow-list rather than a deny-list.  Provider, SCM, cloud,
    # credential, board, session, and private-path variables are all excluded
    # unless explicitly reconstructed below with an isolated value.
    child: dict[str, str] = {
        key: str(parent_env[key]) for key in _SAFE_CHILD_ENV_KEYS if key in parent_env
    }
    child["PATH"] = _SAFE_CHILD_PATH

    isolated_root = board_path.parent
    child.update(
        {
            "HERMES_HOME": str(profile),
            "HOME": str(profile),
            "XDG_CONFIG_HOME": str(profile / "xdg"),
            "XDG_CACHE_HOME": str(profile / "cache"),
            "XDG_DATA_HOME": str(profile / "data"),
            "XDG_STATE_HOME": str(profile / "state"),
            "TMPDIR": str(isolated_root / "tmp"),
            "TERMINAL_CWD": str(profile),
            "HERMES_KANBAN_HOME": str(isolated_root),
            "HERMES_KANBAN_DB": str(board_path),
            "HERMES_KANBAN_BOARD": "default",
            "HERMES_KANBAN_WORKSPACES_ROOT": str(isolated_root / "workspaces"),
            "HERMES_KANBAN_ATTACHMENTS_ROOT": str(isolated_root / "attachments"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return child


class NativeEvaluationUnavailable(RuntimeError):
    """Raised when the requested native model path cannot be launched."""


@dataclass(frozen=True)
class EvaluationCase:
    """One synthetic case and its expected fixture-backed action."""

    item_key: str
    expected_action: str
    context: DecisionContext
    state: Mapping[str, Any]
    title: str = ""
    body: str = ""
    variant: str = "baseline"


@dataclass(frozen=True)
class NativeEvaluation:
    """Secret-safe summary of one native evaluation run."""

    model: str
    provider: str
    cases: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "case_count": len(self.cases),
            "cases": [dict(case) for case in self.cases],
        }


def _fixture_context(
    item_key: str,
    *,
    blocker: Mapping[str, Any] | None = None,
    source_state: str = "open",
    artifact_state: str = "ready",
    review_state: str = "approved",
) -> DecisionContext:
    source = SourceIdentity(
        tracker="synthetic-tracker",
        project="synthetic-project",
        item_key=item_key,
        kind="issue",
    )
    blocker = dict(blocker or {})
    phase = "triage"
    execution = ExecutionIdentity(
        mode="scheduled",
        profile_name="factory-orchestrator",
        task_id=f"task-{item_key}",
        run_id=None,
    )
    evidence = EvidenceBundle(
        scheduler=(
            TypedEvidence(
                kind="scheduler",
                subject=f"tick-{item_key}",
                status="observed",
                reference=f"scheduler-{item_key}",
            ),
        ),
        worker=(
            TypedEvidence(
                kind="worker",
                subject=execution.task_id,
                status="not_started",
                reference=f"worker-{item_key}",
            ),
        ),
        source=(
            TypedEvidence(
                kind="source",
                subject=source.canonical_key,
                status=source_state,
                reference=f"source-{item_key}",
                attributes={"artifact_state": artifact_state},
            ),
        ),
        review=(
            TypedEvidence(
                kind="review",
                subject=source.canonical_key,
                status=review_state,
                reference=f"review-{item_key}",
            ),
        ),
    )
    return DecisionContext(
        execution=execution,
        source_item=source,
        phase=phase,
        input_identity=build_input_identity(
            source,
            phase,
            {"fixture": "native-evaluation", "case": item_key},
        ),
        blocker=BlockerState(
            fingerprint=blocker.get("fingerprint"),
            previous_fingerprint=blocker.get("previous_fingerprint"),
            occurrences=int(blocker.get("occurrences", 0)),
            resolved=bool(blocker.get("resolved", False)),
        ),
        parent_completion=ParentCompletion(
            state="complete",
            verified=True,
            parent_ids=(f"parent-{item_key}",),
        ),
        evidence=evidence,
        policy=DecisionPolicy(
            max_prompt_chars=8_000,
            max_skill_chars=600,
            max_skills=2,
            repeated_blocker_threshold=3,
        ),
    )


def _case_state(
    context: DecisionContext,
    _expected_action: str,
    *,
    existing_action: Mapping[str, Any] | None = None,
    ready: list[Mapping[str, Any]] | None = None,
    source: Mapping[str, Any] | None = None,
    missing: list[str] | None = None,
    title: str = "Fixture work item",
    body: str = "The current typed state is supplied by the fixture.",
) -> dict[str, Any]:
    del _expected_action  # The oracle remains outside the model-visible state.
    return {
        "live": {
            "blocker": context.blocker.as_dict(),
            "existing_action": copy.deepcopy(existing_action),
            "current_run_id": None,
            "source_key": context.source_item.canonical_key,
            "phase": context.phase,
            "input_identity": context.input_identity,
            "semantic_lane": context.semantic_lane,
            "branch": context.execution.branch,
            "tenant": context.execution.tenant,
            "singleton_key": context.execution.singleton_key,
            "credentials_verified": context.execution.credentials_verified,
            "production": context.execution.production,
            "production_approved": context.execution.production_approved,
            "retry_count": context.execution.retry_count,
            "title": title,
            "body": body,
        },
        "parent": context.parent_completion.as_dict(),
        "source": {
            "source_key": context.source_item.canonical_key,
            "phase": context.phase,
            "input_identity": context.input_identity,
            "semantic_lane": context.semantic_lane,
            "current_run_id": None,
            **dict(source or {"source_state": "open", "artifact_state": "ready"}),
        },
        "ready": copy.deepcopy(ready or []),
        "capabilities": {"missing": list(missing or [])},
        "readbacks": {},
    }


def _opaque_item_key(seed: str, index: int) -> str:
    """Create an identity that contains no expected action label."""

    digest = hashlib.sha256(f"{seed}:item:{index}".encode()).hexdigest()
    return f"fixture-{digest[:24]}"


def _counterfactual_text(index: int, variant: str) -> tuple[str, str]:
    """Return action-neutral title/body text for a counterfactual pair."""

    if variant == "paraphrase":
        return (
            f"Work item {index}: typed state observation",
            "Review the canonical state and use the bounded decision contract.",
        )
    return (
        f"Work item {index}: operational observation",
        "Use the canonical evidence and preserve the declared lane identity.",
    )


def build_synthetic_cases(seed: str | None = None) -> tuple[EvaluationCase, ...]:
    """Build six unseen, opaque synthetic cases without an action oracle."""

    suffix = (seed or uuid.uuid4().hex[:12]).strip()
    if not suffix:
        raise ValueError("seed must not be empty")

    cases: list[EvaluationCase] = []

    def add_case(
        index: int,
        expected_action: str,
        context: DecisionContext,
        state: Mapping[str, Any],
    ) -> None:
        title, body = _counterfactual_text(index, "baseline")
        cases.append(
            EvaluationCase(
                context.source_item.item_key,
                expected_action,
                context,
                state,
                title,
                body,
                "baseline",
            )
        )

    item_key = _opaque_item_key(suffix, 0)
    context = _fixture_context(
        item_key,
        blocker={
            "fingerprint": "provider:capacity",
            "previous_fingerprint": "provider:capacity",
            "occurrences": 3,
        },
    )
    add_case(0, "quarantine", context, _case_state(context, "quarantine"))

    item_key = _opaque_item_key(suffix, 1)
    context = _fixture_context(
        item_key,
        blocker={
            "fingerprint": "contract:v2",
            "previous_fingerprint": "contract:v1",
            "resolved": True,
        },
    )
    add_case(1, "admit", context, _case_state(context, "admit"))

    item_key = _opaque_item_key(suffix, 2)
    context = _fixture_context(
        item_key,
        blocker={
            "fingerprint": "existing:blocked",
            "previous_fingerprint": "existing:blocked",
            "occurrences": 1,
        },
    )
    existing = {
        "status": "blocked",
        "task_id": f"existing-{suffix}",
        "current_run_id": None,
        "source_key": context.source_item.canonical_key,
        "phase": context.phase,
        "input_identity": context.input_identity,
        "blocker_fingerprint": context.blocker.fingerprint,
        "semantic_lane": context.semantic_lane,
    }
    add_case(
        2,
        "reuse_existing",
        context,
        _case_state(context, "reuse_existing", existing_action=existing),
    )

    item_key = _opaque_item_key(suffix, 3)
    context = _fixture_context(item_key, blocker={"fingerprint": "signer:held"})
    add_case(
        3,
        "select_independent_lane",
        context,
        _case_state(
            context,
            "select_independent_lane",
            ready=[{"task_id": f"independent-{suffix}"}],
        ),
    )

    item_key = _opaque_item_key(suffix, 4)
    context = _fixture_context(item_key, source_state="merged", artifact_state="failed")
    add_case(
        4,
        "repair_artifact",
        context,
        _case_state(
            context,
            "repair_artifact",
            source={
                "source_state": "merged",
                "artifact_state": "failed",
                "artifact_task_id": f"artifact-{suffix}",
            },
        ),
    )

    item_key = _opaque_item_key(suffix, 5)
    context = _fixture_context(item_key)
    add_case(
        5,
        "hold_missing_capability",
        context,
        _case_state(
            context,
            "hold_missing_capability",
            missing=[f"required-capability-{suffix}"],
        ),
    )
    return tuple(cases)


def build_counterfactual_cases(
    seed: str | None = None,
) -> tuple[tuple[EvaluationCase, EvaluationCase], ...]:
    """Return baseline/paraphrase pairs sharing the exact canonical identity."""

    pairs: list[tuple[EvaluationCase, EvaluationCase]] = []
    for index, baseline in enumerate(build_synthetic_cases(seed)):
        title, body = _counterfactual_text(index, "paraphrase")
        variant = EvaluationCase(
            baseline.item_key,
            baseline.expected_action,
            baseline.context,
            copy.deepcopy(baseline.state),
            title,
            body,
            "paraphrase",
        )
        variant_state = dict(variant.state)
        live = dict(variant_state.get("live", {}))
        live["title"] = title
        live["body"] = body
        variant_state["live"] = live
        variant = EvaluationCase(
            variant.item_key,
            variant.expected_action,
            variant.context,
            variant_state,
            variant.title,
            variant.body,
            variant.variant,
        )
        pairs.append((baseline, variant))
    return tuple(pairs)


@dataclass(frozen=True)
class StatefulEvaluation:
    """Causal multi-tick acceptance evidence for one opaque identity."""

    item_key: str
    ticks: tuple[Mapping[str, Any], ...]
    canonical_identity_stable: bool
    unchanged_blocker_fenced: bool
    exactly_one_admission: bool
    reused_run_is_null: bool
    independent_lane_selected: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_key": self.item_key,
            "tick_count": len(self.ticks),
            "ticks": [dict(tick) for tick in self.ticks],
            "canonical_identity_stable": self.canonical_identity_stable,
            "unchanged_blocker_fenced": self.unchanged_blocker_fenced,
            "exactly_one_admission": self.exactly_one_admission,
            "reused_run_is_null": self.reused_run_is_null,
            "independent_lane_selected": self.independent_lane_selected,
        }


def run_stateful_fixture_evaluation(
    model: DecisionModel, *, seed: str | None = None
) -> StatefulEvaluation:
    """Exercise three unchanged ticks, resolution, reuse, and ready work."""

    suffix = (seed or uuid.uuid4().hex[:12]).strip()
    if not suffix:
        raise ValueError("seed must not be empty")
    item_key = _opaque_item_key(suffix, 100)
    base = _fixture_context(
        item_key,
        blocker={
            "fingerprint": "provider:capacity",
            "previous_fingerprint": "provider:capacity",
            "occurrences": 3,
        },
    )
    observations: list[tuple[DecisionContext, Mapping[str, Any], str, str, str]] = []
    for index in range(3):
        variant = "baseline" if index % 2 == 0 else "paraphrase"
        title, body = _counterfactual_text(index, variant)
        observations.append(
            (
                base,
                _case_state(base, "quarantine", title=title, body=body),
                "quarantine",
                title,
                body,
            )
        )

    resolved = replace(
        base,
        blocker=BlockerState(
            fingerprint="contract:v2",
            previous_fingerprint="contract:v1",
            resolved=True,
        ),
    )
    observations.append(
        (
            resolved,
            _case_state(resolved, "admit"),
            "admit",
            "resolved contract",
            "The typed contract changed and resolution evidence is present.",
        )
    )

    reused = _case_state(
        resolved,
        "reuse_existing",
        existing_action={
            "status": "blocked",
            "task_id": f"existing-{suffix}",
            "current_run_id": None,
            "source_key": resolved.source_item.canonical_key,
            "phase": resolved.phase,
            "input_identity": resolved.input_identity,
            "blocker_fingerprint": resolved.blocker.fingerprint,
            "semantic_lane": resolved.semantic_lane,
        },
    )
    observations.append(
        (
            resolved,
            reused,
            "reuse_existing",
            "reused identity",
            "The same canonical lane is being observed again.",
        )
    )

    independent = _case_state(
        base,
        "select_independent_lane",
        ready=[{"task_id": f"independent-{suffix}"}],
    )
    observations.append(
        (
            base,
            independent,
            "select_independent_lane",
            "independent lane",
            "An unrelated ready lane is available for selection.",
        )
    )

    rows: list[Mapping[str, Any]] = []
    identities = {context.input_identity for context, _, _, _, _ in observations[:3]}
    for tick, (context, state, expected, title, body) in enumerate(observations):
        adapter = NoSideEffectFixtureAdapter(state)
        before = adapter.snapshot()
        result = evaluate_decision(
            model,
            context,
            adapter,
            skills={
                "role": "Use typed role boundaries and keep independent lanes moving.",
                "evidence": "Require exact post-proposal receipts and fail closed.",
            },
        )
        if result.action != expected:
            raise NativeEvaluationUnavailable(
                f"stateful tick {tick} chose {result.action!r}; expected {expected!r}"
            )
        if adapter.snapshot() != before or adapter.mutation_attempts:
            raise NativeEvaluationUnavailable("stateful fixture observed a mutation")
        rows.append(
            {
                "tick": tick,
                "title": title,
                "body": body,
                "action": result.action,
                "status": result.readback.get("status"),
                "current_run_id": result.readback.get("current_run_id"),
                "input_identity": context.input_identity,
                "new_current_run": result.new_current_run,
            }
        )

    return StatefulEvaluation(
        item_key=item_key,
        ticks=tuple(rows),
        canonical_identity_stable=len(identities) == 1,
        unchanged_blocker_fenced=all(
            row["action"] == "quarantine" and row["current_run_id"] is None
            for row in rows[:3]
        ),
        exactly_one_admission=sum(row["action"] == "admit" for row in rows) == 1
        and rows[3]["current_run_id"] is not None,
        reused_run_is_null=rows[4]["action"] == "reuse_existing"
        and rows[4]["current_run_id"] is None,
        independent_lane_selected=rows[5]["action"] == "select_independent_lane",
    )


evaluate_stateful_cases = run_stateful_fixture_evaluation
run_stateful_evaluation = run_stateful_fixture_evaluation


class _FixtureStore:
    """Read-only state file and secret-free tool trace writer."""

    def __init__(self, state_path: Path, trace_path: Path) -> None:
        self.state_path = state_path
        self.trace_path = trace_path
        self._proposal: dict[str, Any] | None = None

    def _state(self) -> dict[str, Any]:
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError("fixture state must be a JSON object")
        return value

    def _record(self, name: str, **details: Any) -> None:
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {"tool": name}
        safe_details = _safe_value(details, f"fixture.trace.{name}")
        if not isinstance(safe_details, dict):
            raise ContractViolation("fixture trace details are malformed")
        entry.update(safe_details)
        with self.trace_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")

    def read(self, name: str, key: str, default: Any) -> Any:
        key = _safe_identifier(key, "fixture.state_key")
        self._record(name)
        value = self._state().get(key, default)
        return copy.deepcopy(_safe_value(value, f"fixture.{key}"))

    def propose_action(
        self,
        action: str,
        idempotency_key: str,
        target_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one proposal and derive its receipt without changing state."""

        if self._proposal is not None:
            raise ContractViolation("fixture accepts only one action proposal")
        # Validate the proposal before recording it; the return value is not
        # exposed as a future outcome and is only an acknowledgement.
        action = _safe_identifier(action, "proposal.action")
        idempotency_key = _safe_identifier(idempotency_key, "proposal.idempotency_key")
        if target_task_id is not None:
            target_task_id = _safe_identifier(target_task_id, "proposal.target_task_id")
        simulated_action_readback(action, idempotency_key, target_task_id)
        self._proposal = {
            "action": action,
            "idempotency_key": idempotency_key,
            "target_task_id": target_task_id,
        }
        self._record(
            "propose_action",
            action=str(action),
            idempotency_key=str(idempotency_key),
            target_task_id=target_task_id,
        )
        return {
            "status": "proposal_recorded",
            "action": action,
            "idempotency_key": idempotency_key,
        }

    def read_action(self, key: str) -> dict[str, Any]:
        key = _safe_identifier(key, "readback.idempotency_key")
        if self._proposal is None or self._proposal["idempotency_key"] != key:
            result = {
                "status": "not_started",
                "idempotency_key": key,
                "current_run_id": None,
            }
        else:
            result = simulated_action_readback(
                self._proposal["action"],
                key,
                self._proposal.get("target_task_id"),
            )
        self._record(
            "read_action_readback",
            idempotency_key=key,
            receipt=copy.deepcopy(result),
        )
        return result


def serve_fixture(state_path: str, trace_path: str) -> None:
    """Run the fixture MCP server; only read tools are registered."""

    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:  # pragma: no cover - depends on native Hermes env
        raise NativeEvaluationUnavailable(
            "native Hermes MCP SDK is unavailable"
        ) from exc

    store = _FixtureStore(Path(state_path), Path(trace_path))
    server = MCPServer(
        "factory-decision-fixture",
        instructions="Read-only synthetic state for a bounded decision evaluation.",
    )

    @server.tool(
        name="read_live_state", description="Read the current synthetic live state."
    )
    def read_live_state() -> dict[str, Any]:
        return store.read("read_live_state", "live", {})

    @server.tool(
        name="read_parent_completion",
        description="Read typed parent completion evidence.",
    )
    def read_parent_completion() -> dict[str, Any]:
        return store.read("read_parent_completion", "parent", {})

    @server.tool(
        name="read_source_state", description="Read typed source and artifact state."
    )
    def read_source_state() -> dict[str, Any]:
        return store.read("read_source_state", "source", {})

    @server.tool(
        name="read_ready_lanes", description="Read independent ready lane identities."
    )
    def read_ready_lanes() -> list[dict[str, Any]]:
        return store.read("read_ready_lanes", "ready", [])

    @server.tool(
        name="read_capabilities", description="Read missing capability evidence."
    )
    def read_capabilities() -> dict[str, Any]:
        return store.read("read_capabilities", "capabilities", {})

    @server.tool(
        name="propose_action",
        description="Record one fixture-only action proposal before readback.",
    )
    def propose_action(
        action: str,
        idempotency_key: str,
        target_task_id: str | None = None,
    ) -> dict[str, Any]:
        return store.propose_action(action, idempotency_key, target_task_id)

    @server.tool(
        name="read_action_readback",
        description="Read the exact idempotent action result.",
    )
    def read_action_readback(idempotency_key: str) -> dict[str, Any]:
        return store.read_action(idempotency_key)

    server.run("stdio")


def _write_profile(
    root: Path,
    *,
    model: str,
    provider: str,
    state_path: Path,
    trace_path: Path,
) -> Path:
    """Install the source template and a minimal isolated native config."""

    profile = root / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    for directory in (
        profile / "xdg",
        profile / "cache",
        profile / "data",
        profile / "state",
        state_path.parent / "tmp",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    source_template = (
        Path(__file__).resolve().parents[1] / "docs" / "orchestrator-soul-template.md"
    )
    (profile / "SOUL.md").write_text(
        source_template.read_text(encoding="utf-8"), encoding="utf-8"
    )

    config = {
        "model": {"provider": provider, "default": model},
        "providers": {provider: {"request_timeout_seconds": 300}},
        "platform_toolsets": {"cli": ["fixture"]},
        "include_default_mcp_servers": False,
        "agent": {"max_turns": 12, "reasoning_effort": "max"},
        "terminal": {"backend": "local", "cwd": str(profile)},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "skills": {"external_dirs": []},
        "display": {"streaming": False},
        "approvals": {"mode": "manual"},
        "mcp_servers": {
            "fixture": {
                "enabled": True,
                "command": sys.executable,
                "args": [
                    str(Path(__file__).resolve()),
                    "--serve",
                    "--state",
                    str(state_path),
                    "--trace",
                    str(trace_path),
                ],
                "env": {},
                "connect_timeout": 30,
                "timeout": 120,
            }
        },
    }
    # JSON is valid YAML and avoids making the generic harness depend on PyYAML.
    (profile / "config.yaml").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return profile


def verify_fixture_only_profile(profile: Path) -> None:
    """Fail closed unless the native profile can expose only the fixture."""

    try:
        config = json.loads((profile / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeEvaluationUnavailable(
            "fixture-only profile config cannot be read"
        ) from exc
    if not isinstance(config, Mapping):
        raise NativeEvaluationUnavailable(
            "fixture-only profile config is not an object"
        )

    if config.get("platform_toolsets") != {"cli": ["fixture"]}:
        raise NativeEvaluationUnavailable(
            "fixture-only profile has a broader effective toolset selection"
        )
    if config.get("include_default_mcp_servers") is not False:
        raise NativeEvaluationUnavailable(
            "fixture-only profile did not disable default MCP servers"
        )
    servers = config.get("mcp_servers")
    if not isinstance(servers, Mapping) or set(servers) != {"fixture"}:
        raise NativeEvaluationUnavailable(
            "fixture-only profile contains a non-fixture MCP server"
        )
    fixture = servers.get("fixture")
    if not isinstance(fixture, Mapping) or fixture.get("enabled") is not True:
        raise NativeEvaluationUnavailable("fixture-only MCP server is not enabled")
    if fixture.get("command") != sys.executable:
        raise NativeEvaluationUnavailable(
            "fixture-only MCP server command is unexpected"
        )
    args = fixture.get("args")
    if not isinstance(args, list) or "--serve" not in args:
        raise NativeEvaluationUnavailable(
            "fixture-only MCP server does not use the fixture server"
        )
    if str(Path(__file__).resolve()) not in {str(value) for value in args}:
        raise NativeEvaluationUnavailable(
            "fixture-only MCP server points outside this harness"
        )
    memory = config.get("memory")
    if not isinstance(memory, Mapping) or memory.get("memory_enabled") is not False:
        raise NativeEvaluationUnavailable("fixture-only profile permits shared memory")


def _copy_auth(source: Path | None, profile: Path) -> None:
    """Copy credentials only into the ephemeral profile, never into the source tree."""

    if source is None:
        raise NativeEvaluationUnavailable(
            "set FACTORY_EVAL_AUTH_FILE to an authenticated native Hermes auth.json"
        )
    if not source.is_file():
        raise NativeEvaluationUnavailable("native Hermes auth file is not present")
    destination = profile / "auth.json"
    shutil.copy2(source, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass


def _auth_source(explicit: str | None) -> Path | None:
    candidates = []
    configured_explicit = explicit or os.environ.get("FACTORY_EVAL_AUTH_FILE", "")
    if configured_explicit:
        candidates.append(Path(configured_explicit).expanduser())
    configured_home = os.environ.get("HERMES_HOME", "").strip()
    if configured_home:
        candidates.append(Path(configured_home) / "auth.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _inherited_board_paths(parent_env: Mapping[str, str]) -> tuple[Path, ...]:
    """Return inherited board, event, database, and SQLite sidecar paths."""

    paths: list[Path] = []
    raw_database = str(parent_env.get("HERMES_KANBAN_DB", "")).strip()
    if raw_database:
        database = Path(raw_database).expanduser()
        paths.extend(
            (
                database,
                Path(f"{database}-wal"),
                Path(f"{database}-shm"),
                Path(f"{database}-journal"),
            )
        )
    raw_home = str(parent_env.get("HERMES_KANBAN_HOME", "")).strip()
    if raw_home:
        home = Path(raw_home).expanduser()
        paths.extend(
            home / name for name in ("events", "event-log", "attachments", "workspaces")
        )
    return tuple(dict.fromkeys(paths))


def snapshot_board_state(parent_env: Mapping[str, str]) -> dict[Path, tuple[Any, ...]]:
    """Capture metadata for an inherited board without opening or mutating it."""

    def signature(path: Path) -> tuple[Any, ...]:
        stat = path.stat()
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return (True, stat.st_ino, stat.st_size, digest.hexdigest())
        return (True, stat.st_ino, stat.st_mode)

    snapshot: dict[Path, tuple[Any, ...]] = {}
    for path in _inherited_board_paths(parent_env):
        try:
            snapshot[path] = signature(path)
        except FileNotFoundError:
            snapshot[path] = (False,)
        except OSError as exc:
            snapshot[path] = ("unreadable", type(exc).__name__)
    return snapshot


def verify_board_state_unchanged(snapshot: Mapping[Path, tuple[Any, ...]]) -> None:
    """Raise if the native child touched an inherited board or SQLite sidecar."""

    def signature(path: Path) -> tuple[Any, ...]:
        stat = path.stat()
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return (True, stat.st_ino, stat.st_size, digest.hexdigest())
        return (True, stat.st_ino, stat.st_mode)

    current: dict[Path, tuple[Any, ...]] = {}
    for path, before in snapshot.items():
        try:
            current[path] = signature(path)
        except FileNotFoundError:
            current[path] = (False,)
        except OSError as exc:
            current[path] = ("unreadable", type(exc).__name__)
        if current[path] != before:
            raise NativeEvaluationUnavailable(
                "native evaluation touched inherited board state"
            )


def _hermes_binary(explicit: str | None) -> str:
    if explicit:
        return explicit
    binary = shutil.which("hermes")
    if binary:
        return binary
    raise NativeEvaluationUnavailable("native Hermes executable is not on PATH")


def _parse_json_response(text: str) -> Mapping[str, Any]:
    """Parse exactly one top-level JSON object and reject framing ambiguity."""

    if not isinstance(text, str) or not text.strip():
        raise ContractViolation("native model returned an empty response")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContractViolation(
                    f"native model response contains duplicate field: {key}"
                )
            result[key] = value
        return result

    decoder = json.JSONDecoder(object_pairs_hook=unique_object)
    # Hermes may emit this fixed runtime diagnostic on stdout when the optional
    # scanner is absent.  Remove only that exact diagnostic; arbitrary prose,
    # partial JSON, arrays, and multiple objects remain contract failures.
    lines = text.splitlines()
    unexpected_runtime_output = [
        line
        for line in lines
        if line.strip() and line.strip() != _NATIVE_SCANNER_NOTICE
    ]
    payload = "\n".join(unexpected_runtime_output).strip()
    try:
        value, end = decoder.raw_decode(payload)
    except json.JSONDecodeError as exc:
        raise ContractViolation("native model response is not one JSON object") from exc
    if not isinstance(value, Mapping):
        raise ContractViolation(
            "native model response must be one top-level JSON object"
        )
    if payload[end:].strip():
        raise ContractViolation("native model response contains trailing output")
    return value


class HermesSubprocessModel(DecisionModel):
    """DecisionModel adapter backed by a fresh native Hermes one-shot."""

    def __init__(
        self,
        profile: Path,
        state_path: Path,
        trace_path: Path,
        board_path: Path,
        hermes: str,
        model: str,
        provider: str,
        run_budget: int,
        parent_env: Mapping[str, str] | None = None,
    ) -> None:
        self.profile = profile
        self.state_path = state_path
        self.trace_path = trace_path
        self.board_path = board_path
        self.hermes = hermes
        self.model = model
        self.provider = provider
        self.run_budget = run_budget
        self.parent_env = dict(parent_env if parent_env is not None else os.environ)
        self.native_fixture_receipt: (
            tuple[Mapping[str, Any], Mapping[str, Any]] | None
        ) = None
        self.native_trace: tuple[Mapping[str, Any], ...] = ()

    def complete(
        self, prompt: str, tools: Mapping[str, Callable[..., Any]]
    ) -> Mapping[str, Any]:
        del tools  # Native Hermes receives the equivalent tools through MCP.
        self.native_fixture_receipt = None
        self.native_trace = ()
        query_path = self.profile / "decision-prompt.txt"
        query = prompt
        if not query.rstrip().endswith(NATIVE_QUERY_SUFFIX):
            query += "\n\n" + NATIVE_QUERY_SUFFIX
        query_path.write_text(query, encoding="utf-8")
        env = build_isolated_environment(self.parent_env, self.profile, self.board_path)
        command = [
            self.hermes,
            "chat",
            "--query-file",
            str(query_path),
            "--oneshot",
            "--quiet",
            "--model",
            self.model,
            "--provider",
            self.provider,
            "--toolsets",
            "fixture",
            "--in",
            str(self.profile),
            "--run-budget",
            str(self.run_budget),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.profile,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.run_budget + 30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NativeEvaluationUnavailable(
                "native Hermes one-shot did not finish"
            ) from exc
        if completed.returncode != 0:
            raise NativeEvaluationUnavailable(
                f"native Hermes one-shot failed with exit code {completed.returncode}"
            )
        if not self.trace_path.exists():
            return _parse_json_response(completed.stdout)
        trace: list[Mapping[str, Any]] = []

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ContractViolation(
                        f"native fixture trace contains duplicate field: {key}"
                    )
                result[key] = value
            return result

        try:
            for line in self.trace_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line, object_pairs_hook=unique_object)
                    if not isinstance(entry, Mapping):
                        raise ContractViolation("fixture trace entry is not an object")
                    trace.append(entry)
        except ContractViolation:
            raise
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise NativeEvaluationUnavailable(
                "native fixture trace is missing or malformed"
            ) from exc
        self.native_trace = tuple(trace)
        proposal_index, readback_index = _validate_observation_trace(trace)
        proposal = trace[proposal_index]
        readback = trace[readback_index]
        receipt = readback.get("receipt")
        if not isinstance(receipt, Mapping):
            raise ContractViolation("native fixture readback omitted its payload")
        self.native_fixture_receipt = (
            {
                "action": proposal.get("action"),
                "idempotency_key": proposal.get("idempotency_key"),
                "target_task_id": proposal.get("target_task_id"),
            },
            dict(receipt),
        )
        return _parse_json_response(completed.stdout)


def run_native_evaluation(
    *,
    model: str = "gpt-5.6-luna",
    provider: str = "openai-codex",
    auth_file: str | None = None,
    hermes: str | None = None,
    seed: str | None = None,
    trace_output: str | None = None,
    run_budget: int = 300,
) -> NativeEvaluation:
    """Run all synthetic cases through native Hermes and return safe evidence."""

    if run_budget < 30:
        raise ValueError("run_budget must be at least 30 seconds")
    binary = _hermes_binary(hermes)
    source_auth = _auth_source(auth_file)
    cases = build_synthetic_cases(seed)
    parent_env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="factory-decision-eval-") as directory:
        root = Path(directory)
        state_path = root / "fixture-state.json"
        trace_path = root / "fixture-trace.jsonl"
        board_path = root / "isolated-kanban.db"
        profile = _write_profile(
            root,
            model=model,
            provider=provider,
            state_path=state_path,
            trace_path=trace_path,
        )
        verify_fixture_only_profile(profile)
        _copy_auth(source_auth, profile)
        result_rows: list[dict[str, Any]] = []
        board_snapshot: dict[Path, tuple[Any, ...]] | None = None
        for case in cases:
            state_path.write_text(
                json.dumps(case.state, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            trace_path.write_text("", encoding="utf-8")
            case_snapshot = snapshot_board_state(parent_env)
            board_snapshot = case_snapshot
            adapter = NoSideEffectFixtureAdapter(case.state)
            before = adapter.snapshot()
            model_adapter = HermesSubprocessModel(
                profile=profile,
                state_path=state_path,
                trace_path=trace_path,
                board_path=board_path,
                hermes=binary,
                model=model,
                provider=provider,
                run_budget=run_budget,
                parent_env=parent_env,
            )
            result = evaluate_decision(
                model_adapter,
                case.context,
                adapter,
                skills={
                    "role": "Use the typed role boundary and preserve independent lanes.",
                    "evidence": "Require exact readback and fail closed on conflicts.",
                },
                prompt_suffix="\n\n" + NATIVE_QUERY_SUFFIX,
            )
            verify_board_state_unchanged(case_snapshot)
            trace = [dict(entry) for entry in model_adapter.native_trace]
            tools = [entry.get("tool") for entry in trace]
            unexpected = sorted(set(tools) - set(FIXTURE_TOOL_NAMES))
            if unexpected:
                raise NativeEvaluationUnavailable(
                    "native model exercised a non-fixture tool: "
                    + ", ".join(unexpected)
                )
            missing = sorted(set(READ_TOOL_NAMES) - set(tools))
            if missing:
                raise NativeEvaluationUnavailable(
                    "native model did not exercise required fixture reads: "
                    + ", ".join(missing)
                )
            proposal_indices = [
                index for index, name in enumerate(tools) if name == "propose_action"
            ]
            if len(proposal_indices) != 1:
                raise ContractViolation(
                    "native model must commit exactly one fixture proposal"
                )
            readback_indices = [
                index
                for index, name in enumerate(tools)
                if name == "read_action_readback"
            ]
            if len(readback_indices) != 1 or proposal_indices[0] > readback_indices[0]:
                raise ContractViolation(
                    "native model must issue exactly one post-proposal readback"
                )
            required_reads = set(READ_TOOL_NAMES) - {"read_action_readback"}
            if any(
                next(index for index, name in enumerate(tools) if name == required_name)
                > proposal_indices[0]
                for required_name in required_reads
            ):
                raise ContractViolation(
                    "native model proposed before completing fixture observations"
                )
            proposal_entry = trace[proposal_indices[0]]
            readback_entry = trace[readback_indices[0]]
            expected_key = action_idempotency_key(case.context)
            if proposal_entry.get("action") != result.proposal.choose.get("action"):
                raise ContractViolation(
                    "native fixture proposal does not match the chosen action"
                )
            if proposal_entry.get("idempotency_key") != expected_key:
                raise ContractViolation(
                    "native fixture proposal does not match the current identity"
                )
            if proposal_entry.get("target_task_id") != result.proposal.choose.get(
                "target_task_id"
            ):
                raise ContractViolation(
                    "native fixture proposal target does not match the decision"
                )
            if any(
                entry.get("idempotency_key") != expected_key
                for entry in (trace[index] for index in readback_indices)
            ):
                raise ContractViolation(
                    "native fixture readback used a stale or foreign identity"
                )
            receipt = readback_entry.get("receipt")
            if not isinstance(receipt, Mapping):
                raise ContractViolation("native fixture readback omitted its payload")
            if dict(receipt) != dict(result.readback):
                raise ContractViolation(
                    "native fixture receipt differs from verified readback"
                )
            if receipt.get("receipt_digest") != _receipt_digest(receipt):
                raise ContractViolation(
                    "native fixture receipt payload is unauthenticated"
                )
            if adapter.snapshot() != before or adapter.mutation_attempts:
                raise NativeEvaluationUnavailable(
                    "fixture adapter observed an unexpected mutation"
                )
            if result.action != case.expected_action:
                raise NativeEvaluationUnavailable(
                    f"native model chose {result.action!r} for {case.item_key!r}; "
                    f"expected {case.expected_action!r}"
                )
            result_rows.append(
                {
                    "item_key": case.item_key,
                    "action": result.action,
                    "tool_calls": len(tools),
                    "tools": tools,
                    "proposal_before_readback": proposal_indices[0]
                    < readback_indices[0],
                    "prompt_chars": result.prompt.prompt_chars,
                    "skill_chars": result.prompt.skill_chars,
                    "tool_chars": result.prompt.tool_chars,
                    "tool_count": result.prompt.tool_count,
                    "effective_prompt_chars": result.prompt.effective_prompt_chars,
                    "new_current_run": result.new_current_run,
                }
            )
        if board_snapshot is not None:
            verify_board_state_unchanged(board_snapshot)
        evaluation = NativeEvaluation(
            model=model, provider=provider, cases=tuple(result_rows)
        )
        if trace_output:
            output = Path(trace_output).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(evaluation.as_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return evaluation


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state", help=argparse.SUPPRESS)
    parser.add_argument("--trace", help=argparse.SUPPRESS)
    parser.add_argument(
        "--model", default=os.environ.get("FACTORY_EVAL_MODEL", "gpt-5.6-luna")
    )
    parser.add_argument(
        "--provider", default=os.environ.get("FACTORY_EVAL_PROVIDER", "openai-codex")
    )
    parser.add_argument("--auth-file", default=os.environ.get("FACTORY_EVAL_AUTH_FILE"))
    parser.add_argument("--hermes", default=os.environ.get("FACTORY_EVAL_HERMES"))
    parser.add_argument("--seed", default=None)
    parser.add_argument("--trace-output", default=None)
    parser.add_argument("--run-budget", type=int, default=300)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.serve:
        if not args.state or not args.trace:
            raise SystemExit("--serve requires --state and --trace")
        serve_fixture(args.state, args.trace)
        return 0
    try:
        evaluation = run_native_evaluation(
            model=args.model,
            provider=args.provider,
            auth_file=args.auth_file,
            hermes=args.hermes,
            seed=args.seed,
            trace_output=args.trace_output,
            run_budget=args.run_budget,
        )
    except ContractViolation as exc:
        print(f"native evaluation contract violation: {exc}", file=sys.stderr)
        return 3
    except (NativeEvaluationUnavailable, ValueError) as exc:
        print(f"native evaluation unavailable: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(evaluation.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by native Hermes
    raise SystemExit(main())
