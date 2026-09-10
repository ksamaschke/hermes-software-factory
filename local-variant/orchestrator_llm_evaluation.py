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
import importlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from stat import S_IMODE, S_ISDIR, S_ISLNK, S_ISREG, S_ISVTX
from typing import Any

try:  # Running as a package is useful to downstream installers.
    from .orchestrator_decision_contract import (
        _MAX_NATIVE_OUTPUT_CHARS,
        _MAX_NATIVE_RESPONSE_CHARS,
        _MAX_NATIVE_TRACE_BYTES,
        _MAX_NATIVE_TRACE_ENTRIES,
        AtomicActionReservationStore,
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
        _action_key_from_decision_identity,
        _bounded_iterable,
        _receipt_digest,
        _redact_text,
        _reject_existing_admission,
        _safe_identifier,
        _safe_value,
        _validate_observation_trace,
        action_idempotency_key,
        build_input_identity,
        decision_identity_key,
        decision_response_requirements_text,
        evaluate_decision,
        simulated_action_readback,
    )
except ImportError:  # Running this file directly is the supported CLI path.
    from orchestrator_decision_contract import (  # type: ignore[no-redef]
        _MAX_NATIVE_OUTPUT_CHARS,
        _MAX_NATIVE_RESPONSE_CHARS,
        _MAX_NATIVE_TRACE_BYTES,
        _MAX_NATIVE_TRACE_ENTRIES,
        AtomicActionReservationStore,
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
        _action_key_from_decision_identity,
        _bounded_iterable,
        _receipt_digest,
        _redact_text,
        _reject_existing_admission,
        _safe_identifier,
        _safe_value,
        _validate_observation_trace,
        action_idempotency_key,
        build_input_identity,
        decision_identity_key,
        decision_response_requirements_text,
        evaluate_decision,
        simulated_action_readback,  # noqa: F401
    )


READ_TOOL_NAMES = (
    "read_live_state",
    "read_parent_completion",
    "read_source_state",
    "read_ready_lanes",
    "read_capabilities",
    "read_action_key",
    "read_action_readback",
)
FIXTURE_TOOL_NAMES = (*READ_TOOL_NAMES, "propose_action")
_NATIVE_SCANNER_NOTICE = (
    "⚠ tirith security scanner enabled but not available — "
    "command scanning will use pattern matching only"
)
_MAX_NATIVE_CASES = 64
_MAX_NATIVE_COMBINED_OUTPUT_CHARS = 256 * 1024

NATIVE_QUERY_SUFFIX = f"""
This is a native integration evaluation. Use the read-only MCP fixture tools,
not assumptions or prose. Call every read_* tool at least once. First diagnose
and choose, then call read_action_key with the chosen action and exact target
(or null when the action has no target). Use its returned idempotency_key in
propose_action exactly once; this records a fixture-only proposal and is not a
live mutation. Do not call read_action_readback before that proposal. Only
after the proposal, call read_action_readback with the same returned key and
copy the complete returned receipt payload, including action, receipt,
receipt_digest, admission_count, and any selected_task_id. Never call a
mutation, shell, file, memory, or network tool.
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
hold_missing_capability; otherwise a bound blocked or completed action selects reuse_existing; a
completed existing action is terminal and a no-op, never admit;
otherwise an unchanged blocker at or above repeated_blocker_threshold remains
quarantined; otherwise a resolved or newly changed blocker may be admitted; and
otherwise hold. These are contract rules, not case labels or an answer oracle.
The allowed actions are quarantine, admit, reuse_existing, select_independent_lane,
repair_artifact, hold_missing_capability, or hold. The choose and act actions
must match the one proposal and the read_back fields must match the post-proposal
fixture read. Keep current_run_id null unless the fixture explicitly returns a
newly admitted run. For select_independent_lane, include choose.target_task_id
copied exactly from read_ready_lanes. For admit, choose and act.target_task_id
MUST equal CONTEXT_JSON.current_task.id; never omit it. For reuse_existing, copy the bound
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


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _resolve_path(path: Path, label: str) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError as exc:
        raise NativeEvaluationUnavailable(
            f"{label} cannot be normalized safely"
        ) from exc


def _inherited_protected_roots(parent_env: Mapping[str, str]) -> tuple[Path, ...]:
    roots: list[Path] = []
    raw_database = str(parent_env.get("HERMES_KANBAN_DB", "")).strip()
    if raw_database:
        database = _resolve_path(Path(raw_database), "inherited board path")
        roots.append(database.parent)
    raw_home = str(parent_env.get("HERMES_KANBAN_HOME", "")).strip()
    if raw_home:
        roots.append(_resolve_path(Path(raw_home), "inherited board path"))
    for key in (
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_ATTACHMENTS_ROOT",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "TERMINAL_CWD",
    ):
        raw_root = str(parent_env.get(key, "")).strip()
        if raw_root:
            roots.append(_resolve_path(Path(raw_root), "inherited board path"))
    return tuple(dict.fromkeys(roots))


def _ensure_path_not_inherited(
    path: Path, parent_env: Mapping[str, str], label: str
) -> Path:
    candidate = _resolve_path(path, label)
    if any(
        _paths_overlap(candidate, root)
        for root in _inherited_protected_roots(parent_env)
    ):
        raise NativeEvaluationUnavailable(f"{label} overlaps inherited board authority")
    return candidate


def _real_directory_without_symlinks(path: Path, label: str) -> Path:
    """Require an existing absolute directory with no symlinked component."""

    try:
        absolute = Path(os.path.abspath(os.path.expanduser(str(path))))
    except OSError as exc:
        raise NativeEvaluationUnavailable(
            f"{label} cannot be normalized safely"
        ) from exc
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except OSError as exc:
            raise NativeEvaluationUnavailable(
                f"{label} is missing or unreadable"
            ) from exc
        if S_ISLNK(info.st_mode):
            raise NativeEvaluationUnavailable(f"{label} contains a symlink")
    try:
        info = absolute.lstat()
    except OSError as exc:
        raise NativeEvaluationUnavailable(f"{label} is missing or unreadable") from exc
    if not S_ISDIR(info.st_mode):
        raise NativeEvaluationUnavailable(f"{label} is not a directory")
    return absolute


def _validated_temporary_parent(parent_env: Mapping[str, str]) -> Path:
    """Resolve and validate the parent before creating any evaluation artifact."""

    configured = next(
        (
            str(parent_env[key]).strip()
            for key in ("TMPDIR", "TEMP", "TMP")
            if str(parent_env.get(key, "")).strip()
        ),
        None,
    )
    if configured is None:
        if os.name != "posix":
            raise NativeEvaluationUnavailable(
                "native evaluation requires an explicit temporary parent"
            )
        configured = "/tmp"
    parent = _real_directory_without_symlinks(
        Path(configured), "native evaluation temporary parent"
    )
    parent = _ensure_path_not_inherited(
        parent, parent_env, "native evaluation temporary parent"
    )
    info = parent.lstat()
    mode = S_IMODE(info.st_mode)
    if info.st_uid not in {0, os.geteuid()}:
        raise NativeEvaluationUnavailable(
            "native evaluation temporary parent has an untrusted owner"
        )
    if mode & 0o022 and not mode & S_ISVTX:
        raise NativeEvaluationUnavailable(
            "native evaluation temporary parent is writable without sticky isolation"
        )
    return parent


def _validate_evaluation_layout(root: Path, parent_env: Mapping[str, str]) -> None:
    """Validate every derived setup path before profile/state/auth writes."""

    derived_paths = (
        (root, "native evaluation root"),
        (root / "profile", "native evaluation profile"),
        (root / "profile" / "auth.json", "native evaluation credential copy"),
        (root / "profile" / "decision-prompt.txt", "native evaluation prompt"),
        (root / "fixture-state.json", "native evaluation fixture state"),
        (root / "fixture-trace.jsonl", "native evaluation fixture trace"),
        (root / "isolated-kanban.db", "native evaluation board"),
        (root / "tmp", "native evaluation child temporary root"),
        (root / "workspaces", "native evaluation child workspace root"),
        (root / "attachments", "native evaluation child attachment root"),
    )
    for path, label in derived_paths:
        _ensure_path_not_inherited(path, parent_env, label)


@contextmanager
def _private_evaluation_root(parent_env: Mapping[str, str]) -> Iterator[Path]:
    """Create one identity-checked private root under a validated parent."""

    parent = _validated_temporary_parent(parent_env)
    root: Path | None = None
    for _ in range(128):
        candidate = parent / f"factory-decision-eval-{uuid.uuid4().hex}"
        _ensure_path_not_inherited(candidate, parent_env, "native evaluation root")
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        except OSError as exc:
            raise NativeEvaluationUnavailable(
                "native evaluation root cannot be created safely"
            ) from exc
        root = candidate
        break
    if root is None:
        raise NativeEvaluationUnavailable("native evaluation root cannot be reserved")
    opened = root.lstat()
    identity = (opened.st_dev, opened.st_ino)
    if S_ISLNK(opened.st_mode) or not S_ISDIR(opened.st_mode):
        raise NativeEvaluationUnavailable(
            "native evaluation root is not a real directory"
        )
    try:
        _validate_evaluation_layout(root, parent_env)
        yield root
    finally:
        try:
            current = root.lstat()
        except OSError as exc:
            raise NativeEvaluationUnavailable(
                "native evaluation root changed before cleanup"
            ) from exc
        if (
            S_ISLNK(current.st_mode)
            or not S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != identity
        ):
            raise NativeEvaluationUnavailable(
                "native evaluation root changed before cleanup"
            )
        shutil.rmtree(root)


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

    profile = _resolve_path(profile, "isolated Hermes profile")
    board_path = _resolve_path(board_path, "isolated board path")
    if not profile.is_dir():
        raise NativeEvaluationUnavailable(
            "isolated Hermes profile directory is missing"
        )

    protected_roots = list(_inherited_protected_roots(parent_env))
    isolated_root = board_path.parent
    isolated_paths = (
        profile,
        isolated_root,
        board_path,
        isolated_root / "tmp",
        isolated_root / "workspaces",
        isolated_root / "attachments",
    )
    if any(
        _paths_overlap(candidate, root)
        for candidate in isolated_paths
        for root in protected_roots
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

    def __init__(
        self,
        message: str,
        *,
        failure_code: str = "native_unavailable",
        retryable: bool = False,
        recovery_action: str = "inspect the bounded diagnostic and hold",
        exit_code: int | None = None,
        diagnostic: str | None = None,
        attempts: int = 1,
    ) -> None:
        if not isinstance(retryable, bool):
            raise ContractViolation("native failure retryability is malformed")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            raise ContractViolation("native failure attempts are malformed")
        if exit_code is not None and (
            not isinstance(exit_code, int) or isinstance(exit_code, bool)
        ):
            raise ContractViolation("native failure exit code is malformed")
        self.failure_code = _redact_text(failure_code)
        self.retryable = retryable
        self.recovery_action = _redact_text(recovery_action)
        self.exit_code = exit_code
        self.diagnostic = _redact_text(
            diagnostic or "no sanitized diagnostic available"
        )
        self.attempts = attempts
        super().__init__(_redact_text(message))

    def as_dict(self) -> dict[str, Any]:
        return {
            "failure_code": self.failure_code,
            "retryable": self.retryable,
            "recovery_action": self.recovery_action,
            "exit_code": self.exit_code,
            "attempts": self.attempts,
            "diagnostic": self.diagnostic,
        }


def _sanitize_native_diagnostic(stderr: str) -> str:
    text = " ".join(str(stderr).split())
    if not text:
        return "no sanitized child diagnostic available"
    if len(text) > 2_048:
        text = text[:1_024] + " ...[trimmed]... " + text[-1_024:]
    try:
        return _redact_text(text)
    except ContractViolation:
        return "child diagnostic was rejected by the redaction boundary"


def _classify_native_failure(
    exit_code: int, stderr: str, stdout: str = ""
) -> tuple[str, bool, str, str]:
    diagnostic = _sanitize_native_diagnostic(
        "\n".join(part for part in (stderr, stdout) if str(part).strip())
    )
    lowered = diagnostic.casefold()
    if any(token in lowered for token in ("timeout", "timed out", "deadline")):
        return (
            "timeout",
            True,
            "retry once with the bounded budget; otherwise hold",
            diagnostic,
        )
    if any(
        token in lowered
        for token in (
            "unauthorized",
            "authentication",
            "auth.json",
            "credential",
            "login required",
            "token expired",
        )
    ):
        return (
            "authentication",
            False,
            "refresh the scoped native auth source and rerun; do not guess credentials",
            diagnostic,
        )
    if any(
        token in lowered
        for token in (
            "rate limit",
            "temporarily unavailable",
            "service unavailable",
            "429",
        )
    ):
        return (
            "provider_unavailable",
            True,
            "retry once with the same exact fixture and identity",
            diagnostic,
        )
    if any(
        token in lowered
        for token in (
            "no module named",
            "configuration",
            "invalid provider",
            "not found",
        )
    ):
        return (
            "runtime_configuration",
            False,
            "repair the isolated runtime configuration, then rerun",
            diagnostic,
        )
    return (
        f"native_exit_{exit_code}",
        False,
        "preserve the diagnostic, hold the lane, and route a bounded runtime repair",
        diagnostic,
    )


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
        safe_cases = _safe_value(
            {
                "cases": _bounded_iterable(
                    self.cases, "native.evaluation.cases", _MAX_NATIVE_CASES
                )
            },
            "native.evaluation",
        )["cases"]
        return {
            "model": _redact_text(self.model),
            "provider": _redact_text(self.provider),
            "case_count": len(safe_cases),
            "cases": safe_cases,
        }


def _fixture_context(
    item_key: str,
    *,
    credentials_verified: bool = False,
    blocker: Mapping[str, Any] | None = None,
    source_state: str = "open",
    artifact_state: str = "ready",
    review_state: str = "approved",
) -> DecisionContext:
    if type(credentials_verified) is not bool:
        raise ContractViolation("credential verification guard is malformed")
    source = SourceIdentity(
        tracker="synthetic-tracker",
        project="synthetic-project",
        item_key=item_key,
        kind="issue",
    )
    if blocker is None:
        blocker_snapshot: dict[str, Any] = {}
    else:
        if not isinstance(blocker, Mapping):
            raise ContractViolation("fixture blocker must be an object")
        bounded_blocker = _safe_value(blocker, "fixture blocker")
        if type(bounded_blocker) is not dict:
            raise ContractViolation("fixture blocker must be an object")
        blocker_snapshot = bounded_blocker
    occurrences = blocker_snapshot.get("occurrences", 0)
    if type(occurrences) is not int or occurrences < 0:
        raise ContractViolation("fixture blocker occurrences are malformed")
    resolved = blocker_snapshot.get("resolved", False)
    if type(resolved) is not bool:
        raise ContractViolation("fixture blocker resolved state is malformed")
    phase = "triage"
    execution = ExecutionIdentity(
        mode="scheduled",
        profile_name="factory-orchestrator",
        task_id=f"task-{item_key}",
        run_id=None,
        credentials_verified=credentials_verified,
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
            fingerprint=blocker_snapshot.get("fingerprint"),
            previous_fingerprint=blocker_snapshot.get("previous_fingerprint"),
            occurrences=occurrences,
            resolved=resolved,
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
    if type(context) is not DecisionContext:
        raise ContractViolation("fixture context must be an exact DecisionContext")

    if existing_action is None:
        existing_snapshot = None
    else:
        if not isinstance(existing_action, Mapping):
            raise ContractViolation("fixture existing action must be an object")
        existing_snapshot = _safe_value(existing_action, "fixture existing action")
        if type(existing_snapshot) is not dict:
            raise ContractViolation("fixture existing action must be an object")

    if source is None:
        source_snapshot: dict[str, Any] = {
            "source_state": "open",
            "artifact_state": "ready",
        }
    else:
        if not isinstance(source, Mapping):
            raise ContractViolation("fixture source state must be an object")
        bounded_source = _safe_value(source, "fixture source state")
        if type(bounded_source) is not dict:
            raise ContractViolation("fixture source state must be an object")
        allowed_source_fields = {
            "source_state",
            "artifact_state",
            "artifact_task_id",
        }
        unexpected_source_fields = sorted(set(bounded_source) - allowed_source_fields)
        if unexpected_source_fields:
            raise ContractViolation(
                "fixture source state contains reserved or unexpected fields"
            )
        source_snapshot = bounded_source

    ready_rows = []
    if ready is not None:
        for index, row in enumerate(
            _bounded_iterable(ready, "fixture ready lanes", _MAX_NATIVE_CASES)
        ):
            if not isinstance(row, Mapping):
                raise ContractViolation("fixture ready lane must be an object")
            bounded_row = _safe_value(row, f"fixture ready lane {index}")
            if type(bounded_row) is not dict:
                raise ContractViolation("fixture ready lane must be an object")
            ready_rows.append(bounded_row)

    missing_capabilities = []
    if missing is not None:
        missing_capabilities = [
            _safe_identifier(value, "fixture missing capability")
            for value in _bounded_iterable(
                missing, "fixture missing capabilities", _MAX_NATIVE_CASES
            )
        ]

    title = _redact_text(title)
    body = _redact_text(body)
    return {
        "decision_identity_key": decision_identity_key(context),
        "live": {
            "blocker": context.blocker.as_dict(),
            "existing_action": copy.deepcopy(existing_snapshot),
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
            **source_snapshot,
        },
        "ready": copy.deepcopy(ready_rows),
        "capabilities": {"missing": missing_capabilities},
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


def build_synthetic_cases(
    seed: str | None = None, *, credentials_verified: bool = False
) -> tuple[EvaluationCase, ...]:
    """Build six unseen, opaque synthetic cases without an action oracle."""

    if seed is None:
        suffix = uuid.uuid4().hex[:12]
    else:
        if type(seed) is not str:
            raise ValueError("seed must be a built-in string")
        suffix = seed.strip()
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
        credentials_verified=credentials_verified,
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
        credentials_verified=credentials_verified,
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
        credentials_verified=credentials_verified,
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
    context = _fixture_context(
        item_key,
        credentials_verified=credentials_verified,
        blocker={"fingerprint": "signer:held"},
    )
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
    context = _fixture_context(
        item_key,
        credentials_verified=credentials_verified,
        source_state="merged",
        artifact_state="failed",
    )
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
    context = _fixture_context(item_key, credentials_verified=credentials_verified)
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
    *,
    credentials_verified: bool = False,
) -> tuple[tuple[EvaluationCase, EvaluationCase], ...]:
    """Return baseline/paraphrase pairs sharing the exact canonical identity."""

    pairs: list[tuple[EvaluationCase, EvaluationCase]] = []
    for index, baseline in enumerate(
        build_synthetic_cases(seed, credentials_verified=credentials_verified)
    ):
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
    model: DecisionModel,
    *,
    seed: str | None = None,
    credentials_verified: bool = False,
) -> StatefulEvaluation:
    """Exercise three unchanged ticks, resolution, reuse, and ready work."""

    if seed is None:
        suffix = uuid.uuid4().hex[:12]
    else:
        if type(seed) is not str:
            raise ValueError("seed must be a built-in string")
        suffix = seed.strip()
    if not suffix:
        raise ValueError("seed must not be empty")
    item_key = _opaque_item_key(suffix, 100)
    base = _fixture_context(
        item_key,
        credentials_verified=credentials_verified,
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
        try:
            initial_state = json.loads(state_path.read_text(encoding="utf-8"))
            initial_live = initial_state.get("live", {})
            initial_run_id = (
                initial_live.get("current_run_id")
                if isinstance(initial_live, dict)
                else None
            )
        except (OSError, TypeError, ValueError):
            initial_run_id = None
        self._reservation_store = AtomicActionReservationStore(
            current_run_id=initial_run_id,
            state_path=state_path.with_name(state_path.name + ".reservation.json"),
        )

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
        serialized = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
        try:
            current_size = (
                self.trace_path.stat().st_size if self.trace_path.exists() else 0
            )
        except OSError as exc:
            raise ContractViolation("fixture trace cannot be inspected") from exc
        if current_size + len(serialized) > _MAX_NATIVE_TRACE_BYTES:
            raise ContractViolation("fixture trace exceeds the contract bound")
        with self.trace_path.open("ab") as stream:
            stream.write(serialized)

    def read(self, name: str, key: str, default: Any) -> Any:
        key = _safe_identifier(key, "fixture.state_key")
        self._record(name)
        value = self._state().get(key, default)
        return copy.deepcopy(_safe_value(value, f"fixture.{key}"))

    def read_action_key(
        self, action: str, target_task_id: str | None = None
    ) -> dict[str, Any]:
        action = _safe_identifier(action, "action_key.action")
        target = (
            None
            if target_task_id is None
            else _safe_identifier(target_task_id, "action_key.target_task_id")
        )
        state = self._state()
        decision_identity = _safe_identifier(
            state.get("decision_identity_key"), "decision_identity_key"
        )
        result = {
            "action": action,
            "target_task_id": target,
            "idempotency_key": _action_key_from_decision_identity(
                decision_identity, action, target
            ),
        }
        self._record("read_action_key", **result)
        return result

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
        state = self._state()
        decision_identity = _safe_identifier(
            state.get("decision_identity_key"), "decision_identity_key"
        )
        if idempotency_key != _action_key_from_decision_identity(
            decision_identity, action, target_task_id
        ):
            raise ContractViolation(
                "fixture proposal key is not bound to action and target"
            )
        _reject_existing_admission(action, state.get("live"))
        self._reservation_store.reserve(action, idempotency_key, target_task_id)
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
            live = self._state().get("live")
            _reject_existing_admission(self._proposal["action"], live)
            result = self._reservation_store.read(key)
            if result.get("status") == "not_started":
                raise ContractViolation("fixture reservation readback is missing")
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
        name="read_action_key",
        description="Read the exact key bound to one action and target.",
    )
    def read_action_key(
        action: str, target_task_id: str | None = None
    ) -> dict[str, Any]:
        return store.read_action_key(action, target_task_id)

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


_MAX_AUTH_FILE_BYTES = 4 * 1024 * 1024
_NON_OAUTH_AUTH_TYPES = frozenset({"api_key", "external_process"})


def _load_authoritative_provider_layer() -> Any:
    try:
        return importlib.import_module("hermes_cli.providers")
    except Exception as exc:
        raise NativeEvaluationUnavailable(
            "native Hermes provider registry/layer is unavailable"
        ) from exc


def _provider_resolution(
    provider: str,
    *,
    user_providers: dict[str, Any] | None = None,
    custom_providers: list[dict[str, Any]] | None = None,
) -> tuple[str, tuple[str, ...], Any]:
    if type(provider) is not str or not provider.strip():
        raise NativeEvaluationUnavailable("native provider identity is malformed")
    requested = provider.strip().casefold()
    try:
        layer = _load_authoritative_provider_layer()
    except NativeEvaluationUnavailable:
        raise
    except Exception as exc:
        raise NativeEvaluationUnavailable(
            "native Hermes provider registry/layer is unavailable"
        ) from exc
    normalize = getattr(layer, "normalize_provider", None)
    get_provider = getattr(layer, "get_provider", None)
    if not callable(normalize) or not callable(get_provider):
        raise NativeEvaluationUnavailable("native Hermes provider layer is malformed")
    try:
        canonical = normalize(provider)
    except Exception as exc:
        raise NativeEvaluationUnavailable(
            "native Hermes provider identity cannot be normalized"
        ) from exc
    if type(canonical) is not str or not canonical.strip():
        raise NativeEvaluationUnavailable(
            "native Hermes provider identity is malformed"
        )
    canonical = canonical.strip().casefold()
    config = None
    raw = provider.strip().casefold()
    if user_providers is not None:
        if type(user_providers) is not dict:
            raise NativeEvaluationUnavailable(
                "configured native providers are malformed"
            )
        configured = dict.get(user_providers, raw)
        if configured is not None:
            if type(configured) is not dict:
                raise NativeEvaluationUnavailable(
                    "configured native provider metadata is malformed"
                )
            local_resolver = getattr(layer, "resolve_user_provider", None)
            full_resolver = getattr(layer, "resolve_provider_full", None)
            if not callable(local_resolver) or not callable(full_resolver):
                raise NativeEvaluationUnavailable(
                    "configured native provider resolver is unavailable"
                )
            try:
                locally_resolved = local_resolver(raw, user_providers)
                config = (
                    full_resolver(provider, user_providers, None)
                    if locally_resolved is not None
                    else None
                )
            except Exception as exc:
                raise NativeEvaluationUnavailable(
                    "configured native provider metadata is unavailable"
                ) from exc
            if config is None:
                raise NativeEvaluationUnavailable(
                    "configured native provider metadata is incomplete"
                )
    if config is None:
        try:
            config = get_provider(canonical, allow_network=False)
        except TypeError:
            raise NativeEvaluationUnavailable(
                "native Hermes provider resolver cannot disable network access"
            ) from None
        except Exception as exc:
            raise NativeEvaluationUnavailable(
                "native Hermes provider metadata is unavailable"
            ) from exc
    if config is None and custom_providers is not None:
        if type(custom_providers) is not list or any(
            type(entry) is not dict for entry in custom_providers
        ):
            raise NativeEvaluationUnavailable("custom native providers are malformed")
        custom_resolver = getattr(layer, "resolve_custom_provider", None)
        if not callable(custom_resolver):
            raise NativeEvaluationUnavailable(
                "custom native provider resolver is unavailable"
            )
        try:
            config = custom_resolver(provider, custom_providers)
        except Exception as exc:
            raise NativeEvaluationUnavailable(
                "custom native provider metadata is unavailable"
            ) from exc
    if config is None:
        raise NativeEvaluationUnavailable("unsupported native provider identity")
    try:
        config_id = getattr(config, "id", canonical)
        auth_type = getattr(config, "auth_type", None)
    except Exception as exc:
        raise NativeEvaluationUnavailable(
            "native Hermes provider metadata is malformed"
        ) from exc
    if type(config_id) is not str or not config_id.strip():
        config_id = canonical
    resolved_id: str | None = None
    try:
        auth_module = importlib.import_module("hermes_cli.auth")
        resolver = getattr(auth_module, "resolve_provider", None)
        if callable(resolver):
            candidate_id = resolver(provider)
            if type(candidate_id) is str and candidate_id.strip():
                resolved_id = candidate_id
    except Exception:  # noqa: BLE001 - optional runtime identity lookup
        resolved_id = None
    candidates = tuple(
        dict.fromkeys(
            candidate.strip().casefold()
            for candidate in (requested, config_id, resolved_id, canonical)
            if type(candidate) is str and candidate.strip()
        )
    )
    if type(auth_type) is not str or not auth_type.strip():
        # The provider definition is authoritative for identity; the installed
        # auth registry is only a credential resolver fallback for old provider
        # definitions that do not carry auth_type themselves.
        try:
            auth_module = importlib.import_module("hermes_cli.auth")
            registry = getattr(auth_module, "PROVIDER_REGISTRY", None)
            auth_config = (
                registry.get(next((key for key in candidates if key in registry), ""))
                if isinstance(registry, Mapping)
                else None
            )
            auth_type = getattr(auth_config, "auth_type", None)
        except Exception as exc:
            raise NativeEvaluationUnavailable(
                "native Hermes provider auth metadata is unavailable"
            ) from exc
    if type(auth_type) is not str:
        raise NativeEvaluationUnavailable(
            "native Hermes provider metadata is malformed"
        )
    auth_type = auth_type.strip().casefold()
    if auth_type in {"aws_sdk", "vertex"}:
        raise NativeEvaluationUnavailable(
            "native provider authentication type is explicitly unsupported"
        )
    if (
        auth_type != "oauth"
        and not auth_type.startswith("oauth_")
        and auth_type not in _NON_OAUTH_AUTH_TYPES
    ):
        raise NativeEvaluationUnavailable(
            "native Hermes provider metadata is malformed"
        )
    return auth_type, candidates, config


def _auth_provider_keys(provider: str) -> tuple[str, ...]:
    return _provider_resolution(provider)[1]


def _provider_metadata(provider: str) -> tuple[str, Any]:
    auth_type, _keys, config = _provider_resolution(provider)
    return auth_type, config


def _provider_requires_refresh(provider: str) -> bool:
    auth_type, _config = _provider_metadata(provider)
    return auth_type == "oauth" or auth_type.startswith("oauth_")


def _nonempty_auth_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _auth_state_has_credential(state: Any, *, requires_refresh: bool) -> bool:
    if not isinstance(state, Mapping):
        return False
    tokens = state.get("tokens")
    if (
        isinstance(tokens, Mapping)
        and _nonempty_auth_string(tokens.get("access_token"))
        and (not requires_refresh or _nonempty_auth_string(tokens.get("refresh_token")))
    ):
        return True
    access = next(
        (
            state.get(field_name)
            for field_name in ("access_token", "api_key", "token", "agent_key")
            if _nonempty_auth_string(state.get(field_name))
        ),
        None,
    )
    if not _nonempty_auth_string(access):
        return False
    return not requires_refresh or _nonempty_auth_string(state.get("refresh_token"))


def _auth_pool_has_credential(entries: Any, *, requires_refresh: bool) -> bool:
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        auth_type = entry.get("auth_type")
        entry_requires_refresh = requires_refresh or (
            isinstance(auth_type, str) and auth_type.casefold() == "oauth"
        )
        access = next(
            (
                entry.get(field_name)
                for field_name in ("access_token", "api_key", "token", "agent_key")
                if _nonempty_auth_string(entry.get(field_name))
            ),
            None,
        )
        if _nonempty_auth_string(access) and (
            not entry_requires_refresh
            or _nonempty_auth_string(entry.get("refresh_token"))
        ):
            return True
    return False


def _open_directory_without_symlinks(path: Path, label: str) -> tuple[Path, int]:
    """Open one directory through component-wise no-follow descriptors."""

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory_flag:
        raise NativeEvaluationUnavailable(
            f"{label} cannot be opened without symbolic links"
        )
    try:
        absolute = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    except (OSError, TypeError, ValueError):
        raise NativeEvaluationUnavailable(f"{label} path is malformed") from None
    flags = os.O_RDONLY | no_follow | directory_flag | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute.anchor, flags)
        for component in absolute.parts[1:]:
            if component in {"", ".", ".."}:
                raise NativeEvaluationUnavailable(f"{label} path is malformed")
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except NativeEvaluationUnavailable:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, TypeError, ValueError):
        if descriptor is not None:
            os.close(descriptor)
        raise NativeEvaluationUnavailable(
            f"{label} cannot be opened without symbolic links"
        ) from None
    if descriptor is None:  # pragma: no cover - an absolute path always has an anchor
        raise NativeEvaluationUnavailable(f"{label} path is malformed")
    return absolute, descriptor


def _validate_auth_file(
    source: Path | None,
    provider: str,
    resolution: tuple[str, tuple[str, ...], Any] | None = None,
) -> bytes:
    """Return the exact bounded bytes validated from one no-follow file open."""

    if source is None:
        raise NativeEvaluationUnavailable(
            "set FACTORY_EVAL_AUTH_FILE to an authenticated native Hermes auth.json"
        )
    if type(provider) is not str or not provider.strip():
        raise NativeEvaluationUnavailable("native provider identity is malformed")
    if resolution is None:
        resolution = _provider_resolution(provider)
    if type(resolution) is not tuple or tuple.__len__(resolution) != 3:
        raise NativeEvaluationUnavailable("native provider resolution is malformed")
    auth_type, provider_keys, _config = resolution
    if (
        type(auth_type) is not str
        or type(provider_keys) is not tuple
        or not provider_keys
    ):
        raise NativeEvaluationUnavailable("native provider resolution is malformed")
    requires_refresh = auth_type == "oauth" or auth_type.startswith("oauth_")
    try:
        source_path = Path(source)
    except (TypeError, ValueError):
        raise NativeEvaluationUnavailable(
            "native Hermes auth file path is malformed"
        ) from None
    absolute_parent, parent_descriptor = _open_directory_without_symlinks(
        source_path.parent, "native Hermes auth file parent"
    )
    absolute_source = absolute_parent / source_path.name
    if not source_path.name or source_path.name in {".", ".."}:
        os.close(parent_descriptor)
        raise NativeEvaluationUnavailable("native Hermes auth file path is malformed")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    for flag_name in ("O_CLOEXEC", "O_NONBLOCK"):
        flags |= getattr(os, flag_name, 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(source_path.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not S_ISREG(opened.st_mode):
            raise NativeEvaluationUnavailable(
                "native Hermes auth file is not a regular readable file"
            )
        if opened.st_size > _MAX_AUTH_FILE_BYTES:
            raise NativeEvaluationUnavailable(
                "native Hermes auth file exceeds its bound"
            )
        chunks: list[bytes] = []
        copied = 0
        while True:
            chunk = os.read(
                descriptor, min(64 * 1024, _MAX_AUTH_FILE_BYTES + 1 - copied)
            )
            if not chunk:
                break
            chunks.append(chunk)
            copied += len(chunk)
            if copied > _MAX_AUTH_FILE_BYTES:
                raise NativeEvaluationUnavailable(
                    "native Hermes auth file exceeds its bound"
                )
        closed = os.fstat(descriptor)
        identity_before = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        identity_after = (
            closed.st_dev,
            closed.st_ino,
            closed.st_size,
            closed.st_mtime_ns,
            closed.st_ctime_ns,
        )
        raw = b"".join(chunks)
        if identity_before != identity_after or len(raw) != opened.st_size:
            raise NativeEvaluationUnavailable(
                "native Hermes auth file changed during validation"
            )
    except NativeEvaluationUnavailable:
        raise
    except (OSError, TypeError, ValueError):
        raise NativeEvaluationUnavailable(
            "native Hermes auth file is not a regular readable file"
        ) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.close(parent_descriptor)
        except OSError:
            pass
    if absolute_source.name != source_path.name:
        raise NativeEvaluationUnavailable("native Hermes auth file path is malformed")
    try:
        text = raw.decode("utf-8-sig")
        payload = json.loads(text)
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        raise NativeEvaluationUnavailable(
            "native Hermes auth JSON is malformed"
        ) from None
    if type(payload) is not dict:
        raise NativeEvaluationUnavailable("native Hermes auth JSON is not an object")
    try:
        bounded_payload = _safe_value(payload, "native auth payload")
    except ContractViolation:
        raise NativeEvaluationUnavailable(
            "native Hermes auth JSON exceeds its structural bounds"
        ) from None
    if type(bounded_payload) is not dict:
        raise NativeEvaluationUnavailable("native Hermes auth JSON is not an object")

    providers = bounded_payload.get("providers")
    pool = bounded_payload.get("credential_pool")
    if providers is not None and type(providers) is not dict:
        raise NativeEvaluationUnavailable("native Hermes auth providers are malformed")
    if pool is not None and type(pool) is not dict:
        raise NativeEvaluationUnavailable(
            "native Hermes auth credential pool is malformed"
        )
    if providers is None and pool is None:
        raise NativeEvaluationUnavailable(
            "native Hermes auth JSON has no provider credential structure"
        )

    state_valid = bool(
        type(providers) is dict
        and any(
            _auth_state_has_credential(
                providers.get(provider_key), requires_refresh=requires_refresh
            )
            for provider_key in provider_keys
        )
    )
    pool_valid = bool(
        type(pool) is dict
        and any(
            _auth_pool_has_credential(
                pool.get(provider_key), requires_refresh=requires_refresh
            )
            for provider_key in provider_keys
        )
    )
    if not (state_valid or pool_valid):
        raise NativeEvaluationUnavailable(
            "native Hermes auth JSON has no provider-compatible credential"
        )
    return raw


def _atomic_copy_auth(payload: bytes, destination: Path) -> None:
    """Publish only the exact bytes validated from the original file descriptor."""

    if type(payload) is not bytes or len(payload) > _MAX_AUTH_FILE_BYTES:
        raise NativeEvaluationUnavailable("validated native auth payload is malformed")
    try:
        destination = Path(destination)
    except (TypeError, ValueError):
        raise NativeEvaluationUnavailable(
            "native Hermes auth destination is malformed"
        ) from None
    _absolute_parent, parent_descriptor = _open_directory_without_symlinks(
        destination.parent, "native Hermes auth destination parent"
    )
    if not destination.name or destination.name in {".", ".."}:
        os.close(parent_descriptor)
        raise NativeEvaluationUnavailable("native Hermes auth destination is malformed")
    temporary_name = f".{destination.name}.{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor: int | None = None
    published = False
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output_stream:
            descriptor = None
            output_stream.write(payload)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        published = True
        info = os.stat(
            destination.name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if not S_ISREG(info.st_mode) or S_IMODE(info.st_mode) & 0o077:
            raise NativeEvaluationUnavailable(
                "native Hermes auth destination is not a private regular file"
            )
        os.fsync(parent_descriptor)
    except NativeEvaluationUnavailable:
        if published:
            try:
                os.unlink(destination.name, dir_fd=parent_descriptor)
            except OSError:
                pass
        raise
    except (OSError, TypeError, ValueError):
        if published:
            try:
                os.unlink(destination.name, dir_fd=parent_descriptor)
            except OSError:
                pass
        raise NativeEvaluationUnavailable(
            "native Hermes auth file is not copyable"
        ) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        try:
            os.close(parent_descriptor)
        except OSError:
            pass


def _remove_auth_file(destination: Path) -> None:
    """Remove the isolated copy through the same no-follow directory boundary."""

    _parent, parent_descriptor = _open_directory_without_symlinks(
        destination.parent, "native Hermes auth destination parent"
    )
    try:
        os.unlink(destination.name, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise NativeEvaluationUnavailable(
            "isolated native auth copy could not be removed"
        ) from exc
    finally:
        os.close(parent_descriptor)


def _resolve_runtime_credentials(
    provider: str,
    profile: Path,
    resolution: tuple[str, tuple[str, ...], Any] | None = None,
) -> bool:
    """Resolve/refresh credentials through installed Hermes without returning them."""

    try:
        auth_module = importlib.import_module("hermes_cli.auth")
        if resolution is None:
            resolution = _provider_resolution(provider)
        if type(resolution) is not tuple or tuple.__len__(resolution) != 3:
            raise NativeEvaluationUnavailable("native provider resolution is malformed")
        auth_type, provider_keys, _config = resolution
        if (
            type(auth_type) is not str
            or type(provider_keys) is not tuple
            or not provider_keys
        ):
            raise NativeEvaluationUnavailable("native provider resolution is malformed")
        provider_id = provider_keys[-1]
    except NativeEvaluationUnavailable:
        raise
    except Exception as exc:
        raise NativeEvaluationUnavailable(
            "native provider runtime credential layer is unavailable"
        ) from exc
    resolver_name = {
        "oauth_pkce": "resolve_codex_runtime_credentials",
        "oauth_device_code": "resolve_nous_runtime_credentials",
        "oauth_qwen": "resolve_qwen_runtime_credentials",
        "oauth_xai": "resolve_xai_oauth_runtime_credentials",
        "oauth_minimax": "resolve_minimax_oauth_runtime_credentials",
        "oauth_spotify": "resolve_spotify_runtime_credentials",
        "api_key": "resolve_api_key_provider_credentials",
        "external_process": "resolve_external_process_provider_credentials",
    }.get(auth_type)
    if auth_type == "oauth_external":
        resolver_name = {
            "openai-codex": "resolve_codex_runtime_credentials",
            "xai-oauth": "resolve_xai_oauth_runtime_credentials",
            "qwen-oauth": "resolve_qwen_runtime_credentials",
            "minimax-oauth": "resolve_minimax_oauth_runtime_credentials",
        }.get(provider_id)
    if resolver_name is None:
        raise NativeEvaluationUnavailable(
            "native provider has no authenticated runtime credential resolver"
        )
    resolver = getattr(auth_module, resolver_name, None)
    if not callable(resolver):
        raise NativeEvaluationUnavailable(
            "native provider runtime credential resolver is unavailable"
        )
    credential_root = profile.parent / "credential-runtime"
    for directory in (
        credential_root,
        credential_root / "tmp",
        credential_root / "workspaces",
        credential_root / "attachments",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    board_path = credential_root / "kanban.db"
    environment = build_isolated_environment(dict(os.environ), profile, board_path)
    verifier = """
import importlib
import sys
from collections.abc import Mapping

resolver_name, auth_type, provider_id = sys.argv[1:4]
try:
    resolver = getattr(importlib.import_module("hermes_cli.auth"), resolver_name)
    result = resolver() if auth_type.startswith("oauth_") else resolver(provider_id)
except BaseException:
    raise SystemExit(3)
if not isinstance(result, Mapping) or not result:
    raise SystemExit(4)
sys.stdout.write("credential-ok")
""".strip()
    try:
        return_code, stdout, _stderr = _run_bounded_process(
            [
                sys.executable,
                "-c",
                verifier,
                resolver_name,
                auth_type,
                provider_id,
            ],
            cwd=profile,
            env=environment,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        raise NativeEvaluationUnavailable(
            "native provider runtime credential resolution did not complete"
        ) from exc
    if return_code != 0 or stdout != "credential-ok":
        raise NativeEvaluationUnavailable(
            "native provider runtime credential resolution returned no usable lease"
        )
    return True


def _provider_preflight_unavailable(_provider: str, _profile: Path) -> None:
    raise NativeEvaluationUnavailable(
        "authenticated provider endpoint/native inference preflight is unavailable"
    )


def _copy_auth(
    source: Path | None,
    profile: Path,
    *,
    provider: str = "openai-codex",
    preflight: Callable[[str, Path], None] | None = None,
) -> bool:
    """Copy auth only after runtime resolution and authenticated preflight succeed."""

    resolution = _provider_resolution(provider)
    payload = _validate_auth_file(source, provider, resolution)
    destination = profile / "auth.json"
    try:
        _atomic_copy_auth(payload, destination)
        _resolve_runtime_credentials(provider, profile, resolution)
        (preflight or _provider_preflight_unavailable)(provider, profile)
    except Exception:
        try:
            _remove_auth_file(destination)
        except NativeEvaluationUnavailable as cleanup_exc:
            raise NativeEvaluationUnavailable(
                "native provider verification failed and the isolated auth copy "
                "could not be removed"
            ) from cleanup_exc
        raise
    return True


def _auth_source(explicit: str | None) -> Path | None:
    candidates = []
    if explicit is not None and type(explicit) is not str:
        raise NativeEvaluationUnavailable("explicit auth path is malformed")
    configured_explicit = (
        explicit
        if explicit is not None and explicit.strip()
        else os.environ.get("FACTORY_EVAL_AUTH_FILE", "")
    )
    if type(configured_explicit) is not str:
        raise NativeEvaluationUnavailable("configured auth path is malformed")
    if configured_explicit.strip():
        candidates.append(Path(configured_explicit).expanduser())
    configured_home = os.environ.get("HERMES_HOME", "").strip()
    if configured_home:
        candidates.append(Path(configured_home) / "auth.json")
    for candidate in candidates:
        try:
            os.lstat(os.fspath(candidate))
        except FileNotFoundError:
            continue
        except OSError:
            return candidate
        else:
            return candidate
    return None


def _lexical_absolute_path(path: Path, label: str) -> Path:
    """Normalize spelling without following symlinks, for root snapshots."""

    try:
        return Path(os.path.abspath(os.path.expanduser(str(path))))
    except OSError as exc:
        raise NativeEvaluationUnavailable(
            f"{label} cannot be normalized safely"
        ) from exc


def _inherited_board_paths(parent_env: Mapping[str, str]) -> tuple[Path, ...]:
    """Return raw inherited roots so root symlink retargets remain observable."""

    paths: list[Path] = []
    raw_database = str(parent_env.get("HERMES_KANBAN_DB", "")).strip()
    if raw_database:
        database = _lexical_absolute_path(Path(raw_database), "inherited board path")
        paths.extend(
            (
                database,
                database.parent,
                Path(f"{database}-wal"),
                Path(f"{database}-shm"),
                Path(f"{database}-journal"),
            )
        )
    raw_home = str(parent_env.get("HERMES_KANBAN_HOME", "")).strip()
    if raw_home:
        paths.append(_lexical_absolute_path(Path(raw_home), "inherited board path"))
    for key in (
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_ATTACHMENTS_ROOT",
    ):
        raw_root = str(parent_env.get(key, "")).strip()
        if raw_root:
            paths.append(_lexical_absolute_path(Path(raw_root), "inherited board path"))
    return tuple(dict.fromkeys(paths))


_MAX_SNAPSHOT_ENTRIES = 8_192
_MAX_SNAPSHOT_FILE_BYTES = 64 * 1024 * 1024
_MAX_SNAPSHOT_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_SNAPSHOT_SYMLINK_TARGETS = 256


def _new_snapshot_budget() -> dict[str, int]:
    return {"entries": 0, "bytes": 0, "symlink_targets": 0}


def _count_snapshot_entry(budget: dict[str, int]) -> None:
    budget["entries"] += 1
    if budget["entries"] > _MAX_SNAPSHOT_ENTRIES:
        raise NativeEvaluationUnavailable("inherited board tree exceeds snapshot bound")


def _file_digest(path: Path, size: int, budget: dict[str, int]) -> str:
    if size > _MAX_SNAPSHOT_FILE_BYTES:
        raise NativeEvaluationUnavailable("inherited board file exceeds snapshot bound")
    budget["bytes"] += size
    if budget["bytes"] > _MAX_SNAPSHOT_TOTAL_BYTES:
        raise NativeEvaluationUnavailable("inherited board bytes exceed snapshot bound")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_signature(
    path: Path,
    _symlink_seen: frozenset[Path] | None = None,
    _budget: dict[str, int] | None = None,
) -> tuple[Any, ...]:
    """Fingerprint a file or bounded directory tree, including link targets."""

    symlink_seen = _symlink_seen or frozenset()
    budget = _budget if _budget is not None else _new_snapshot_budget()
    stat = path.lstat()
    if path.is_symlink():
        target = Path(os.path.realpath(path))
        if target in symlink_seen:
            target_signature: tuple[Any, ...] = ("cycle",)
        elif not target.exists():
            target_signature = ("missing",)
        else:
            budget["symlink_targets"] += 1
            if budget["symlink_targets"] > _MAX_SNAPSHOT_SYMLINK_TARGETS:
                raise NativeEvaluationUnavailable(
                    "inherited board symlink targets exceed snapshot bound"
                )
            target_signature = _path_signature(target, symlink_seen | {target}, budget)
        return (
            "symlink",
            stat.st_ino,
            stat.st_mode,
            os.readlink(path),
            str(target),
            target_signature,
        )
    if not path.is_dir():
        digest = _file_digest(path, stat.st_size, budget) if path.is_file() else None
        return (
            "file",
            stat.st_ino,
            stat.st_mode,
            stat.st_size,
            stat.st_mtime_ns,
            digest,
        )
    records: list[tuple[Any, ...]] = []
    pending = [path]
    while pending:
        current = pending.pop()
        entries = []
        with os.scandir(current) as iterator:
            for entry in iterator:
                _count_snapshot_entry(budget)
                entries.append(entry)
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            entry_path = Path(entry.path)
            entry_stat = entry.stat(follow_symlinks=False)
            relative = entry_path.relative_to(path).as_posix()
            if entry.is_symlink():
                record = (
                    relative,
                    "symlink",
                    _path_signature(entry_path, symlink_seen, budget),
                )
            elif entry.is_dir(follow_symlinks=False):
                record = (
                    relative,
                    "directory",
                    entry_stat.st_ino,
                    entry_stat.st_mode,
                    entry_stat.st_mtime_ns,
                )
                pending.append(entry_path)
            elif entry.is_file(follow_symlinks=False):
                record = (
                    relative,
                    "file",
                    entry_stat.st_ino,
                    entry_stat.st_mode,
                    entry_stat.st_size,
                    entry_stat.st_mtime_ns,
                    _file_digest(entry_path, entry_stat.st_size, budget),
                )
            else:
                record = (
                    relative,
                    "other",
                    entry_stat.st_ino,
                    entry_stat.st_mode,
                    entry_stat.st_size,
                    entry_stat.st_mtime_ns,
                )
            records.append(record)
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return (
        "directory",
        stat.st_ino,
        stat.st_mode,
        stat.st_mtime_ns,
        hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def snapshot_board_state(parent_env: Mapping[str, str]) -> dict[Path, tuple[Any, ...]]:
    """Capture bounded content fingerprints for inherited board paths."""

    snapshot: dict[Path, tuple[Any, ...]] = {}
    budget = _new_snapshot_budget()
    for path in _inherited_board_paths(parent_env):
        try:
            snapshot[path] = _path_signature(path, _budget=budget)
        except FileNotFoundError:
            snapshot[path] = (False,)
        except OSError as exc:
            raise NativeEvaluationUnavailable(
                "inherited board state cannot be snapshotted safely"
            ) from exc
    return snapshot


def verify_board_state_unchanged(snapshot: Mapping[Path, tuple[Any, ...]]) -> None:
    """Raise if the native child touched inherited board or tree contents."""

    if type(snapshot) is not dict:
        raise NativeEvaluationUnavailable(
            "board snapshot is not a trusted built-in map"
        )
    budget = _new_snapshot_budget()
    for path, before in dict.items(snapshot):
        if not isinstance(path, Path) or type(before) is not tuple:
            raise NativeEvaluationUnavailable(
                "board snapshot is not a trusted built-in map"
            )
        try:
            current = _path_signature(path, _budget=budget)
        except FileNotFoundError:
            current = (False,)
        except OSError as exc:
            raise NativeEvaluationUnavailable(
                "inherited board state cannot be verified safely"
            ) from exc
        if current != before:
            raise NativeEvaluationUnavailable(
                "native evaluation touched inherited board state"
            )


def _hermes_binary(explicit: str | None) -> str:
    if explicit is not None:
        if type(explicit) is not str or not explicit.strip():
            raise NativeEvaluationUnavailable("native Hermes executable is malformed")
        return explicit
    binary = shutil.which("hermes")
    if binary:
        return binary
    raise NativeEvaluationUnavailable("native Hermes executable is not on PATH")


def _native_provider_preflight(
    provider: str,
    profile: Path,
    *,
    hermes: str,
    model: str,
    parent_env: Mapping[str, str],
    run_budget: int,
) -> None:
    """Prove authenticated native inference with no configured tool surface."""

    query_path = _ensure_path_not_inherited(
        profile / "provider-preflight.txt", parent_env, "provider preflight query"
    )
    _atomic_write_text(
        query_path,
        "Return one short provider health response. Do not call tools or mutate files.",
        label="provider preflight query",
    )
    board_path = _ensure_path_not_inherited(
        profile / "provider-preflight-board.db", parent_env, "provider preflight board"
    )
    env = build_isolated_environment(parent_env, profile, board_path)
    command = [
        hermes,
        "chat",
        "--query-file",
        str(query_path),
        "--oneshot",
        "--quiet",
        "--model",
        model,
        "--provider",
        provider,
        "--run-budget",
        str(min(run_budget, 60)),
    ]

    config_path = profile / "config.yaml"
    try:
        original_config_text = config_path.read_text(encoding="utf-8")
        original_config = json.loads(original_config_text)
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise NativeEvaluationUnavailable(
            "provider preflight profile configuration is unavailable"
        ) from exc
    if type(original_config) is not dict:
        raise NativeEvaluationUnavailable(
            "provider preflight profile configuration is malformed"
        )
    preflight_config = copy.deepcopy(original_config)
    preflight_config["platform_toolsets"] = {"cli": []}
    preflight_config["include_default_mcp_servers"] = False
    preflight_config["mcp_servers"] = {}
    agent_config = preflight_config.get("agent")
    if type(agent_config) is not dict:
        agent_config = {}
    else:
        agent_config = dict(agent_config)
    agent_config["max_turns"] = 1
    preflight_config["agent"] = agent_config
    preflight_config_text = (
        json.dumps(preflight_config, indent=2, sort_keys=True) + "\n"
    )

    _atomic_write_text(
        config_path,
        preflight_config_text,
        label="provider preflight profile configuration",
    )
    try:
        try:
            installed = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise NativeEvaluationUnavailable(
                "provider preflight profile configuration cannot be verified"
            ) from exc
        if (
            type(installed) is not dict
            or installed.get("platform_toolsets") != {"cli": []}
            or installed.get("include_default_mcp_servers") is not False
            or installed.get("mcp_servers") != {}
            or not isinstance(installed.get("agent"), Mapping)
            or installed["agent"].get("max_turns") != 1
        ):
            raise NativeEvaluationUnavailable(
                "provider preflight profile retained a tool surface"
            )
        try:
            return_code, _stdout, stderr = _run_bounded_process(
                command,
                cwd=profile,
                env=env,
                timeout=min(run_budget, 60),
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
            raise NativeEvaluationUnavailable(
                "authenticated provider preflight did not complete"
            ) from exc
    finally:
        _atomic_write_text(
            config_path,
            original_config_text,
            label="provider profile restoration",
        )
        try:
            restored_text = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise NativeEvaluationUnavailable(
                "provider profile restoration cannot be verified"
            ) from exc
        if restored_text != original_config_text:
            raise NativeEvaluationUnavailable(
                "provider profile restoration did not preserve the fixture profile"
            )

    if return_code != 0:
        raise NativeEvaluationUnavailable(
            "authenticated provider preflight failed",
            diagnostic=_sanitize_native_diagnostic(stderr),
            exit_code=return_code,
        )


def _parse_json_response(text: str) -> Mapping[str, Any]:
    """Parse exactly one top-level JSON object and reject framing ambiguity."""

    if not isinstance(text, str) or not text.strip():
        raise ContractViolation("native model returned an empty response")
    if len(text) > _MAX_NATIVE_RESPONSE_CHARS:
        raise ContractViolation("native model response exceeds the contract bound")

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


def _kill_process_group(process: subprocess.Popen[Any]) -> None:
    """Kill the bounded child and descendants without touching the parent."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass


def _run_bounded_process(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
) -> tuple[int, str, str]:
    """Run a child while bounding stdout/stderr before buffering them."""

    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    buffers: dict[int, bytearray] = {}
    try:
        assert process.stdout is not None and process.stderr is not None
        stream_fds = (process.stdout.fileno(), process.stderr.fileno())
        streams = {
            stream.fileno(): stream for stream in (process.stdout, process.stderr)
        }
        for stream in streams.values():
            selector.register(stream, selectors.EVENT_READ)
            buffers[stream.fileno()] = bytearray()
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            events = selector.select(remaining)
            if not events:
                continue
            for selected, _ in events:
                chunk = os.read(selected.fd, 8192)
                if not chunk:
                    selector.unregister(selected.fileobj)
                    streams[selected.fd].close()
                    continue
                buffer = buffers[selected.fd]
                buffer.extend(chunk)
                if len(buffer) > _MAX_NATIVE_OUTPUT_CHARS:
                    raise ContractViolation(
                        "native Hermes output exceeds the contract bound"
                    )
                if (
                    sum(len(item) for item in buffers.values())
                    > _MAX_NATIVE_COMBINED_OUTPUT_CHARS
                ):
                    raise ContractViolation(
                        "combined native Hermes output exceeds the contract bound"
                    )
        return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        output = [bytes(buffers[fd]).decode("utf-8") for fd in stream_fds]
        return return_code, output[0], output[1]
    except (UnicodeDecodeError, subprocess.TimeoutExpired):
        raise
    except BaseException:
        _kill_process_group(process)
        raise
    finally:
        try:
            selector.close()
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            _kill_process_group(process)
            try:
                process.wait(timeout=1)
            except (subprocess.TimeoutExpired, ChildProcessError):
                pass


def _atomic_write_text(path: Path, text: str, *, label: str) -> None:
    """Replace a generated file without ever truncating a caller-controlled inode."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise NativeEvaluationUnavailable(f"{label} cannot be written safely") from exc


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
        env = build_isolated_environment(self.parent_env, self.profile, self.board_path)
        query_path = _ensure_path_not_inherited(
            self.profile / "decision-prompt.txt",
            self.parent_env,
            "native prompt output",
        )
        query = prompt
        if not query.rstrip().endswith(NATIVE_QUERY_SUFFIX):
            query += "\n\n" + NATIVE_QUERY_SUFFIX
        _atomic_write_text(query_path, query, label="native prompt output")
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

        def reset_retry_artifacts() -> None:
            reservation_path = self.state_path.with_name(
                self.state_path.name + ".reservation.json"
            )
            for path in (
                self.trace_path,
                reservation_path,
                Path(f"{reservation_path}.lock"),
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise NativeEvaluationUnavailable(
                        "native retry artifacts could not be reset"
                    ) from exc
            self.native_fixture_receipt = None
            self.native_trace = ()

        attempts = 0
        while True:
            try:
                return_code, stdout, stderr = _run_bounded_process(
                    command,
                    cwd=self.profile,
                    env=env,
                    timeout=self.run_budget + 30,
                )
            except subprocess.TimeoutExpired as exc:
                if attempts == 0:
                    attempts += 1
                    reset_retry_artifacts()
                    continue
                raise NativeEvaluationUnavailable(
                    "native Hermes one-shot did not finish",
                    failure_code="timeout",
                    retryable=True,
                    recovery_action="retry exhausted; hold for runtime repair",
                    attempts=attempts + 1,
                ) from exc
            except OSError as exc:
                raise NativeEvaluationUnavailable(
                    "native Hermes one-shot could not be launched",
                    failure_code="launch_error",
                    recovery_action="repair the isolated executable/runtime and rerun",
                    diagnostic=_sanitize_native_diagnostic(str(exc)),
                    attempts=attempts + 1,
                ) from exc
            if return_code == 0:
                break
            failure_code, retryable, recovery_action, diagnostic = (
                _classify_native_failure(return_code, stderr, stdout)
            )
            if retryable and attempts == 0:
                attempts += 1
                reset_retry_artifacts()
                continue
            raise NativeEvaluationUnavailable(
                f"native Hermes one-shot failed with exit code {return_code}",
                failure_code=failure_code,
                retryable=retryable,
                recovery_action=(
                    f"retry exhausted; {recovery_action}"
                    if attempts
                    else recovery_action
                ),
                exit_code=return_code,
                diagnostic=diagnostic,
                attempts=attempts + 1,
            )
        if not self.trace_path.exists():
            return _parse_json_response(stdout)
        try:
            trace_size = self.trace_path.stat().st_size
        except OSError as exc:
            raise NativeEvaluationUnavailable(
                "native fixture trace is unreadable"
            ) from exc
        if trace_size > _MAX_NATIVE_TRACE_BYTES:
            raise ContractViolation("native fixture trace exceeds the contract bound")
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
            with self.trace_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    if len(trace) >= _MAX_NATIVE_TRACE_ENTRIES:
                        raise ContractViolation(
                            "native fixture trace exceeds the contract bound"
                        )
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
        return _parse_json_response(stdout)


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

    if type(run_budget) is not int or run_budget < 30:
        raise ValueError("run_budget must be an integer of at least 30 seconds")
    parent_env = dict(os.environ)
    source_auth = _auth_source(auth_file)
    if trace_output:
        _ensure_path_not_inherited(
            Path(trace_output), parent_env, "native trace output"
        )
    with _private_evaluation_root(parent_env) as root:
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
        binary = _hermes_binary(hermes)
        credentials_verified = _copy_auth(
            source_auth,
            profile,
            provider=provider,
            preflight=lambda selected_provider, selected_profile: (
                _native_provider_preflight(
                    selected_provider,
                    selected_profile,
                    hermes=binary,
                    model=model,
                    parent_env=parent_env,
                    run_budget=run_budget,
                )
            ),
        )
        if credentials_verified is not True:
            raise NativeEvaluationUnavailable(
                "native Hermes credentials were not verified"
            )
        cases = build_synthetic_cases(
            seed,
            credentials_verified=credentials_verified,
        )
        result_rows: list[dict[str, Any]] = []
        board_snapshot: dict[Path, tuple[Any, ...]] | None = None
        for case in cases:
            state_path.write_text(
                json.dumps(case.state, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            reservation_path = state_path.with_name(
                state_path.name + ".reservation.json"
            )
            try:
                reservation_path.unlink()
            except FileNotFoundError:
                pass
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
            chosen_action = _safe_identifier(
                result.proposal.choose.get("action"), "choose.action"
            )
            chosen_target = result.proposal.choose.get("target_task_id")
            if chosen_target is not None:
                chosen_target = _safe_identifier(chosen_target, "choose.target_task_id")
            expected_key = action_idempotency_key(
                case.context, chosen_action, chosen_target
            )
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
            output = _ensure_path_not_inherited(
                Path(trace_output), parent_env, "native trace output"
            )
            _atomic_write_text(
                output,
                json.dumps(evaluation.as_dict(), indent=2, sort_keys=True) + "\n",
                label="native trace output",
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
        if isinstance(exc, NativeEvaluationUnavailable):
            detail = json.dumps(exc.as_dict(), sort_keys=True)
        else:
            detail = str(exc)
        print(f"native evaluation unavailable: {detail}", file=sys.stderr)
        return 2
    print(json.dumps(evaluation.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by native Hermes
    raise SystemExit(main())
