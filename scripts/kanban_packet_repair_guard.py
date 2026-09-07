#!/usr/bin/env python3
"""Stop invalid review packets from being fanned out, and quarantine the leaves.

This is the external add-on for factory defect 2. It is deliberately outside
Hermes core: the core auto-decomposer is a generic triage-to-workgraph LLM step
with no concept of a review packet, and it must not grow one.

The failure it prevents, observed end to end on a live board:

    review card (invalid packet: 17 paths, no hunk ranges,
                 gate cited as the literal "all gate commands green")
      -> reviewer returns REVIEW-INCOMPLETE  (correct)
      -> reviewer returns REVIEW-INCOMPLETE  (correct, second run)
      -> block-loop detection escalates the card to `triage`
      -> core auto-decomposer fans `triage` out into 4 children
      -> all 4 inherit the same invalid packet
      -> all 4 return REVIEW-INCOMPLETE

Six worker runs, zero source inspection. Splitting cannot repair a validity
failure; it multiplies it.

Two guards, run in order:

``quarantine``
    A review card sitting in ``triage`` is one dispatcher tick away from being
    fanned out. If its packet is invalid, archive it before that happens and
    file a single packet-repair card. ``triage`` cards cannot be blocked
    (``hermes kanban block`` rejects the transition), so archive is the only
    durable stop — verified against the live CLI.

``sweep``
    For leaves already created from an invalid parent, block any that still
    carry the parent's defect, so an inherited-invalid packet cannot be
    dispatched a second time.

Neither guard touches a source worktree, a tracker, or Git history.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_packet_integrity as rpi  # noqa: E402

MARKER = "[packet-repair-guard]"
REVIEW_HINTS = (
    "review_kind:",
    "read_only_source:",
    "adversarial review",
    "adversarial read-only",
    "review leaf",
    "candidate_commit:",
)


def _hermes(*args: str, timeout: int = 120) -> tuple[int, str, str]:
    exe = shutil.which("hermes") or "hermes"
    env = os.environ.copy()
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    proc = subprocess.run(
        [exe, *args], text=True, capture_output=True, timeout=timeout, env=env, check=False
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _hermes_json(*args: str) -> Any:
    code, out, err = _hermes(*args, "--json")
    if code != 0:
        raise RuntimeError(f"hermes {' '.join(args[:4])} failed: {err or out}")
    return json.loads(out)


def board_db(board: str) -> Path:
    for entry in _hermes_json("kanban", "boards", "list") or []:
        if isinstance(entry, dict) and entry.get("slug") == board:
            return Path(str(entry.get("db_path"))).expanduser()
    raise RuntimeError(f"no board database for {board}")


def read_tasks(board: str) -> list[dict]:
    """Read task rows directly.

    `PRAGMA query_only` rather than a `mode=ro` URI: the board is WAL, and a
    read-only handle cannot create the `-shm` file it needs when no writer holds
    the database open ("unable to open database file"). Verified on this host.
    """
    conn = sqlite3.connect(str(board_db(board)), timeout=10)
    try:
        conn.execute("PRAGMA query_only=1")
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT id, title, body, assignee, status, created_by, workspace_path, "
            "max_runtime_seconds, max_retries FROM tasks"
        )]
    finally:
        conn.close()


def looks_like_review(task: dict) -> bool:
    body = str(task.get("body") or "").lower()
    title = str(task.get("title") or "").lower()
    if any(hint in body for hint in REVIEW_HINTS):
        return True
    return title.startswith("review ") or " review " in title


# --------------------------------------------------------------------------
# Packet validation focused on the two defects that actually caused the burn.


def packet_errors(task: dict) -> list[str]:
    """Return the packet defects that make this review card undispatchable.

    Intentionally narrow: it checks the things whose absence provably wastes a
    review lane, not the full reviewer contract. A card that passes here can
    still be rejected by the stricter contract validator downstream.
    """
    body = str(task.get("body") or "")
    errors: list[str] = []

    scope = _scope_entries(body)
    if not scope:
        errors.append("exact_scope must contain file paths")
    else:
        unranged = [p for p in scope if not re.search(r":\d+-\d+", p)]
        if unranged:
            errors.append(
                f"exact_scope entries carry no hunk range ({len(unranged)}/{len(scope)} paths): "
                + ", ".join(unranged[:3])
                + ("..." if len(unranged) > 3 else "")
            )

    gate = _gate_section(body)
    if rpi.is_unverifiable_gate_citation(gate):
        shown = " ".join(gate.split())[:80] or "(absent)"
        errors.append(f"gate evidence is unverifiable: '{shown}'")

    if not re.search(r"\b[0-9a-f]{40}\b", body):
        errors.append("missing candidate_commit")
    return errors


def _scope_entries(body: str) -> list[str]:
    """Collect the change manifest entries from a packet body."""
    lines = body.splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^\s*(?:#+\s*)?(?:exact[_ ]scope|change manifest)\b", line, re.I):
            capturing = True
            continue
        if capturing:
            if stripped.startswith("#"):
                break
            if stripped.startswith("-"):
                out.append(stripped[1:].strip().strip("`"))
                continue
            if not stripped:
                if out:
                    break
                continue
            if not out:
                continue
            break
    return [o for o in out if o and not o.lower().startswith("(+")]


def _gate_section(body: str) -> str:
    """Extract the gate-evidence prose from a packet body."""
    match = re.search(
        r"(?:#+\s*)?gate evidence[^\n]*\n(.*?)(?=\n\s*#|\Z)", body, re.I | re.S
    )
    if match:
        return match.group(1).strip()
    match = re.search(r"(?im)^\s*gate(?:_note|_evidence)?\s*:\s*(.+)$", body)
    return match.group(1).strip() if match else ""


# --------------------------------------------------------------------------
# Guard 1: quarantine invalid review packets sitting in triage.


def repair_card_body(task: dict, errors: list[str], decision) -> str:
    return f"""{MARKER} A review packet failed validation and was stopped before fan-out.

original_review_card: {task.get('id')}
original_title: {task.get('title')}

## Why this is a repair job, not a split

{decision.reason}

Splitting an invalid packet does not repair it: every child inherits the same
defect and returns REVIEW-INCOMPLETE for the same reason. A strict-subset
manifest is the remedy for genuine change-set *size*, never for invalidity.

## Defects to fix

{chr(10).join(f'- {e}' for e in errors)}

## Definition of done

- The change manifest lists each changed path WITH per-path hunk ranges in the
  form `path:1-20,40-55`. `git diff -U0 <base>..<candidate>` emits these directly.
- Gate evidence cites a real command, its exit code, and the commit it ran
  against — or explicitly declares the gate absent. Never assert a bare green.
- The packet names its candidate commit.

Once repaired, re-dispatch a fresh review card. Do not re-run the original.
"""


def quarantine(board: str, *, apply: bool) -> list[str]:
    """Archive invalid review packets in `triage` before the decomposer sees them.

    `triage` is the pre-fan-out state the core auto-decomposer polls. Cards there
    cannot be blocked — `hermes kanban block` rejects the transition — so archive
    is the only durable stop.
    """
    changes: list[str] = []
    for task in read_tasks(board):
        if str(task.get("status") or "") != "triage":
            continue
        if not looks_like_review(task):
            continue
        errors = packet_errors(task)
        if not errors:
            continue
        decision = rpi.split_decision(errors)
        if decision.allowed:
            changes.append(f"{task['id']}: size-only defect, split permitted; left in triage")
            continue

        tid = task["id"]
        if not apply:
            changes.append(f"would quarantine {tid} (invalid packet): {'; '.join(errors)}")
            continue

        code, out, err = _hermes("kanban", "--board", board, "archive", tid)
        if code != 0:
            changes.append(f"{tid}: archive FAILED: {err or out}")
            continue
        after = _task_status(board, tid)
        if after != "archived":
            changes.append(f"{tid}: archive readback FAILED (status={after!r})")
            continue

        rc, rout, rerr = _hermes(
            "kanban", "--board", board, "create",
            f"Repair review packet: {str(task.get('title') or tid)[:60]}",
            "--body", repair_card_body(task, errors, decision),
            "--assignee", os.environ.get("PACKET_REPAIR_ASSIGNEE", "default"),
            "--priority", "80",
            "--idempotency-key", f"packet-repair:{tid}",
            "--created-by", "packet-repair-guard",
            "--json",
        )
        repair_id = ""
        if rc == 0:
            try:
                payload = json.loads(rout)
                repair_id = str((payload.get("task") or payload).get("id") or "")
            except Exception:
                repair_id = ""
        changes.append(
            f"quarantined {tid} (invalid packet, {len(errors)} defect(s)); "
            f"repair card {repair_id or 'CREATE FAILED: ' + (rerr or rout)[:120]}"
        )
    return changes


def _task_status(board: str, task_id: str) -> str:
    try:
        detail = _hermes_json("kanban", "--board", board, "show", task_id)
    except Exception:
        return ""
    task = detail.get("task") if isinstance(detail, dict) else None
    return str((task or detail or {}).get("status") or "")


# --------------------------------------------------------------------------
# Guard 2: block leaves that inherited a parent's invalid packet.


DISPATCHABLE = {"ready", "todo", "review"}


def sweep(board: str, *, apply: bool) -> list[str]:
    """Block already-created leaves that still carry an invalid packet."""
    changes: list[str] = []
    for task in read_tasks(board):
        if str(task.get("status") or "") not in DISPATCHABLE:
            continue
        if not looks_like_review(task):
            continue
        errors = packet_errors(task)
        if not errors:
            continue
        if rpi.split_decision(errors).allowed:
            continue
        tid = task["id"]
        reason = f"{MARKER} not dispatched: " + "; ".join(errors)
        if not apply:
            changes.append(f"would block {tid}: {'; '.join(errors)}")
            continue
        code, out, err = _hermes(
            "kanban", "--board", board, "block", tid, reason, "--kind", "needs_input"
        )
        if code != 0:
            changes.append(f"{tid}: block FAILED: {err or out}")
            continue
        after = _task_status(board, tid)
        changes.append(
            f"blocked {tid} (inherited-invalid packet)"
            if after == "blocked"
            else f"{tid}: block readback FAILED (status={after!r})"
        )
    return changes


# ---------------------------------------------------------------------------
# Generic admission and lifecycle safety
#
# This section is deliberately a small state-machine seam.  It does not spawn
# workers, choose a scheduler, or mutate Hermes' board database directly.  The
# controller owns only canonical identity/fencing and prepares a single
# auditable action for the supported Kanban API.  Callers must persist the
# returned audit data and read back the mutation before treating it as progress.

ADMISSION_CONTRACT_VERSION = "factory.admission.v1"
BLOCKER_FENCE_TICKS = 3
# Hermes' native loop breaker enters triage after two repeated block/unblock
# cycles.  A Factory adapter must fence that quarantined triage row at the
# native boundary; waiting for a third write is the bypass this seam exists to
# prevent.
NATIVE_QUARANTINE_RECURRENCE = 2
_TERMINAL_PARENT_STATUSES = {"done", "archived"}
_SAFE_CAPABILITY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_REFERENCE = re.compile(r"^[^\s\x00]{1,256}$")
_ALLOWED_RESOLUTION_SOURCES = {
    "controller-readback",
    "factory-controller",
    "native-readback",
    "orchestrator-readback",
}
_NATIVE_ADAPTER_LOCK = threading.RLock()


def _normalise_identity_value(value: Any, field: str) -> str:
    text = " ".join(str(value or "").split()).strip().casefold()
    if not text:
        raise ValueError(f"missing canonical identity field: {field}")
    if any(char in text for char in "\x00\r\n"):
        raise ValueError(f"malformed canonical identity field: {field}")
    return text


def _nested_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _identity_mapping(value: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = dict(value)
        metadata = _nested_mapping(result.get("metadata"))
        for key, item in metadata.items():
            result.setdefault(key, item)
        body = str(result.get("body") or "")
    else:
        result = {}
        body = str(value or "")
    for line in body.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9 _/-]*)\s*:\s*(.*?)\s*$", line)
        if match:
            result.setdefault(match.group(1).strip().casefold().replace(" ", "_"), match.group(2))
    return result


def _identity_field(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        for candidate in (name, name.replace("_", " "), name.replace("_", "-")):
            if candidate in mapping and mapping[candidate] not in (None, ""):
                return mapping[candidate]
            lowered = candidate.casefold()
            for key, value in mapping.items():
                if str(key).casefold() == lowered and value not in (None, ""):
                    return value
    return None


@dataclass(frozen=True)
class CanonicalIdentity:
    """The semantic lane identity used for admission and recovery."""

    source_repository: str
    source_item: str
    phase: str
    input_artifact_revision: str

    def __post_init__(self) -> None:
        for name in (
            "source_repository",
            "source_item",
            "phase",
            "input_artifact_revision",
        ):
            object.__setattr__(
                self,
                name,
                _normalise_identity_value(getattr(self, name), name),
            )

    @property
    def key(self) -> str:
        return json.dumps(
            [
                self.source_repository,
                self.source_item,
                self.phase,
                self.input_artifact_revision,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()


def canonical_identity(value: Mapping[str, Any] | CanonicalIdentity | str) -> CanonicalIdentity:
    """Parse all four identity coordinates and fail closed when one is absent."""
    if isinstance(value, CanonicalIdentity):
        return value
    mapping = _identity_mapping(value)
    fields = {
        "source_repository": _identity_field(
            mapping, "source_repository", "repository", "source_repo"
        ),
        "source_item": _identity_field(
            mapping, "source_item", "source_issue", "source_pr", "item"
        ),
        "phase": _identity_field(mapping, "phase", "current_phase"),
        "input_artifact_revision": _identity_field(
            mapping,
            "input_artifact_revision",
            "input_artifact",
            "artifact_revision",
            "revision",
            "candidate_commit",
        ),
    }
    return CanonicalIdentity(**fields)


def blocker_fingerprint(
    value: Mapping[str, Any] | CanonicalIdentity | str,
    blocker_class: Optional[str] = None,
) -> str:
    """Return a secret-safe blocker identity, independent of prose wording."""
    identity = canonical_identity(value)
    if blocker_class is None and isinstance(value, Mapping):
        blocker_class = _identity_field(
            _identity_mapping(value), "blocker_class", "block_kind", "reason_code"
        )
    blocker = _normalise_identity_value(blocker_class, "blocker_class")
    if not _SAFE_CAPABILITY_NAME.fullmatch(blocker.replace("/", "-")):
        raise ValueError("malformed blocker_class")
    payload = f"{identity.digest}:{blocker}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass
class BlockerRecord:
    fingerprint: str
    identity: CanonicalIdentity
    blocker_class: str
    observations: int = 0
    fenced: bool = False
    resolved: bool = False
    resolution_reference: Optional[str] = None
    last_tick: Optional[int] = None


@dataclass(frozen=True)
class ResolutionResult:
    accepted: bool
    reason: str = ""
    fingerprint: Optional[str] = None


@dataclass(frozen=True)
class AdmissionResult:
    accepted: bool
    created: bool
    reused: bool
    state: str
    current_run_id: Optional[int]
    reason: str = ""
    task_id: Optional[str] = None


@dataclass
class LaneRecord:
    identity: CanonicalIdentity
    task_id: Optional[str] = None
    idempotency_keys: set[str] = field(default_factory=set)
    historical_run_ids: set[int] = field(default_factory=set)
    current_run_id: Optional[int] = None
    status: str = "not_started"


@dataclass(frozen=True)
class PhaseReservation:
    reservation_id: str
    identity: CanonicalIdentity
    phase: str
    owner: str
    idempotency_key: str
    created_tick: int
    fresh_until: int


@dataclass(frozen=True)
class ReservationResult:
    accepted: bool
    created: bool
    reservation_id: Optional[str] = None
    reason: str = ""


class AdmissionLedger:
    """Small in-process ledger for one controller's durable read/reconcile loop.

    The board event/task record is the durable source of truth.  This ledger is
    intentionally serialisable by callers and never substitutes a board
    readback.  Keeping the semantic index separate from caller-supplied
    idempotency keys is what makes two concurrent guesses converge on one lane.
    """

    def __init__(self) -> None:
        self.blockers: dict[str, BlockerRecord] = {}
        self.lanes: dict[str, LaneRecord] = {}
        self.reservations: dict[tuple[str, str], PhaseReservation] = {}
        self._next_run = 1

    def _lane(self, identity: CanonicalIdentity) -> LaneRecord:
        return self.lanes.setdefault(identity.key, LaneRecord(identity=identity))

    def observe_blocker(self, task: Mapping[str, Any], *, tick: int) -> BlockerRecord:
        identity = canonical_identity(task)
        blocker = _identity_field(_identity_mapping(task), "blocker_class", "block_kind", "reason_code")
        fingerprint = blocker_fingerprint(identity, str(blocker or "unknown"))
        try:
            durable_recurrence = int(task.get("block_recurrences") or 0)
        except (TypeError, ValueError):
            raise ValueError("block recurrence is malformed") from None
        if durable_recurrence < 0:
            raise ValueError("block recurrence is negative")
        record = self.blockers.get(fingerprint)
        if record is None:
            record = BlockerRecord(
                fingerprint=fingerprint,
                identity=identity,
                blocker_class=_normalise_identity_value(blocker or "unknown", "blocker_class"),
            )
            self.blockers[fingerprint] = record
        if not record.resolved and record.last_tick != tick:
            record.observations = max(record.observations + 1, durable_recurrence)
            record.last_tick = tick
            record.fenced = record.fenced or (
                record.observations >= BLOCKER_FENCE_TICKS
                or durable_recurrence >= NATIVE_QUARANTINE_RECURRENCE
            )
        return record

    def is_fenced(self, task: Mapping[str, Any] | CanonicalIdentity) -> bool:
        identity = canonical_identity(task)
        for record in self.blockers.values():
            if record.identity == identity and record.fenced and not record.resolved:
                return True
        return False

    def resolve_blocker(
        self,
        task: Mapping[str, Any],
        evidence: Mapping[str, Any],
        *,
        new_contract: bool,
    ) -> ResolutionResult:
        identity = canonical_identity(task)
        blocker = _identity_field(_identity_mapping(task), "blocker_class", "block_kind", "reason_code")
        fingerprint = blocker_fingerprint(identity, str(blocker or "unknown"))
        record = self.blockers.get(fingerprint)
        if record is None or not record.fenced:
            return ResolutionResult(False, "blocker is not durably fenced", fingerprint)
        source = str(evidence.get("source") or "").strip()
        reference = str(evidence.get("reference") or "").strip()
        contract_revision = str(evidence.get("contract_revision") or "").strip()
        kind = str(evidence.get("kind") or "").strip()
        if source not in _ALLOWED_RESOLUTION_SOURCES:
            return ResolutionResult(False, "resolution evidence source is not approved", fingerprint)
        if kind != "blocker-resolution" or not _SAFE_REFERENCE.fullmatch(reference):
            return ResolutionResult(False, "resolution evidence is malformed", fingerprint)
        if not _SAFE_REFERENCE.fullmatch(contract_revision):
            return ResolutionResult(False, "new contract revision is missing or malformed", fingerprint)
        if not new_contract:
            return ResolutionResult(False, "resolution must identify a new contract", fingerprint)
        record.resolved = True
        record.fenced = False
        record.resolution_reference = reference
        return ResolutionResult(True, "evidence-backed resolution accepted", fingerprint)

    def admit(self, task: Mapping[str, Any]) -> AdmissionResult:
        identity = canonical_identity(task)
        if self.is_fenced(identity):
            return AdmissionResult(False, False, False, "fenced", None, "blocker remains fenced")
        key = f"admission:{identity.digest}"
        return self.create_or_reuse(identity, idempotency_key=key)

    def create_or_reuse(
        self,
        value: Mapping[str, Any] | CanonicalIdentity,
        *,
        idempotency_key: str,
        existing: Optional[Mapping[str, Any]] = None,
        requested_run_id: Optional[int] = None,
    ) -> AdmissionResult:
        identity = canonical_identity(value)
        key = str(idempotency_key or "").strip()
        if not key or not _SAFE_REFERENCE.fullmatch(key):
            return AdmissionResult(False, False, False, "rejected", None, "idempotency key is malformed")

        if existing is not None:
            try:
                existing_identity = canonical_identity(existing)
            except ValueError:
                return AdmissionResult(False, False, False, "rejected", None, "existing identity is malformed")
            if existing_identity != identity:
                return AdmissionResult(False, False, False, "rejected", None, "semantic identity conflict")
            raw_history = existing.get("historical_run_ids") or existing.get("run_ids") or []
            if isinstance(raw_history, (str, bytes)) or not isinstance(raw_history, Sequence):
                return AdmissionResult(
                    False,
                    False,
                    False,
                    "rejected",
                    None,
                    "historical run record is malformed",
                    str(existing.get("id") or "") or None,
                )
            try:
                history = {int(item) for item in raw_history}
            except (TypeError, ValueError):
                return AdmissionResult(False, False, False, "rejected", None, "historical run record is malformed")
            if any(item <= 0 for item in history):
                return AdmissionResult(False, False, False, "rejected", None, "historical run record is malformed")
            current = existing.get("current_run_id")
            try:
                current_id = int(current) if current is not None else None
            except (TypeError, ValueError):
                return AdmissionResult(
                    False,
                    False,
                    False,
                    "rejected",
                    None,
                    "current run record is malformed",
                    str(existing.get("id") or "") or None,
                )
            if current_id is not None and current_id <= 0:
                return AdmissionResult(
                    False,
                    False,
                    False,
                    "rejected",
                    None,
                    "current run record is malformed",
                    str(existing.get("id") or "") or None,
                )
            if current_id is not None and current_id in history:
                return AdmissionResult(
                    False,
                    False,
                    False,
                    "rejected",
                    current_id,
                    "current run is also recorded as historical",
                    str(existing.get("id") or "") or None,
                )
            status = str(existing.get("status") or "").casefold()
            if current_id is None and status in {"active", "in_progress", "review", "running"}:
                return AdmissionResult(False, False, False, "rejected", None, "active task has no current run")
            if current_id is not None and status in {"done", "archived"}:
                return AdmissionResult(
                    False,
                    False,
                    False,
                    "rejected",
                    current_id,
                    "terminal task carries a current run",
                    str(existing.get("id") or "") or None,
                )
            lane = self._lane(identity)
            lane.task_id = str(existing.get("id") or lane.task_id or "") or None
            lane.historical_run_ids.update(history)
            lane.current_run_id = current_id
            lane.status = str(existing.get("status") or lane.status)
            lane.idempotency_keys.add(key)
        else:
            lane = self._lane(identity)

        if requested_run_id is not None:
            if requested_run_id in lane.historical_run_ids:
                return AdmissionResult(False, False, True, "rejected", lane.current_run_id, "historical run id rejected", lane.task_id)
            if lane.current_run_id != requested_run_id:
                return AdmissionResult(False, False, True, "rejected", lane.current_run_id, "historical or foreign run id is not the current run", lane.task_id)

        if lane.current_run_id is not None:
            lane.idempotency_keys.add(key)
            return AdmissionResult(True, False, True, "reused_current_run", lane.current_run_id, "current run reused", lane.task_id)
        if existing is not None:
            lane.idempotency_keys.add(key)
            return AdmissionResult(True, False, True, "reused_not_started", None, "existing task has no current run", lane.task_id)

        lane.current_run_id = self._next_run
        self._next_run += 1
        lane.status = "running"
        lane.idempotency_keys.add(key)
        return AdmissionResult(True, True, False, "new_current_run", lane.current_run_id, "new semantic lane admitted", lane.task_id)

    def reserve_phase(
        self,
        value: Mapping[str, Any] | CanonicalIdentity,
        *,
        owner: str,
        phase: str,
        tick: int,
        freshness: int,
        idempotency_key: str,
    ) -> ReservationResult:
        identity = canonical_identity(value)
        owner_name = _normalise_identity_value(owner, "owner")
        phase_name = _normalise_identity_value(phase, "phase")
        key = str(idempotency_key or "").strip()
        if not _SAFE_REFERENCE.fullmatch(key):
            return ReservationResult(False, False, reason="reservation idempotency key is malformed")
        if freshness < 0:
            return ReservationResult(False, False, reason="reservation freshness is negative")
        slot = (identity.key, phase_name)
        current = self.reservations.get(slot)
        if current is not None and tick <= current.fresh_until:
            if current.owner != owner_name:
                return ReservationResult(False, False, current.reservation_id, "fresh phase is owned by another controller")
            return ReservationResult(True, False, current.reservation_id, "fresh semantic reservation reused")
        reservation_id = f"phase:{identity.digest}:{hashlib.sha256(phase_name.encode()).hexdigest()[:16]}"
        self.reservations[slot] = PhaseReservation(
            reservation_id=reservation_id,
            identity=identity,
            phase=phase_name,
            owner=owner_name,
            idempotency_key=key,
            created_tick=int(tick),
            fresh_until=int(tick) + int(freshness),
        )
        return ReservationResult(True, True, reservation_id, "semantic phase reserved")

    def reservation_count(self, value: Mapping[str, Any] | CanonicalIdentity, phase: str) -> int:
        identity = canonical_identity(value)
        return int((identity.key, _normalise_identity_value(phase, "phase")) in self.reservations)


@dataclass(frozen=True)
class DependencyDecision:
    action: str
    reason: str


class DependencyGate:
    """Reconcile dependency waits without an unblock/resume spin loop."""

    def __init__(self) -> None:
        self._resumed: set[str] = set()

    def reconcile(
        self,
        task_id: str,
        parents: Optional[Sequence[Mapping[str, Any]]],
        *,
        tick: int,
    ) -> DependencyDecision:
        del tick  # the durable resume set, not time, is the idempotency guard
        if not parents:
            return DependencyDecision("hold", "dependency wait has no declared parent")
        if any(not parent.get("id") for parent in parents):
            return DependencyDecision("hold", "dependency parent identity is missing")
        if any(str(parent.get("status") or "").lower() not in _TERMINAL_PARENT_STATUSES for parent in parents):
            return DependencyDecision("wait", "a declared parent is unfinished")
        if task_id in self._resumed:
            return DependencyDecision("hold", "parent-complete resume already consumed")
        self._resumed.add(task_id)
        return DependencyDecision("resume", "all declared parents are terminal; resume exactly once")

    def resume_count(self, task_id: str) -> int:
        return int(task_id in self._resumed)


@dataclass(frozen=True)
class CapabilityRequest:
    target_profile: str
    skill: str
    category: str
    workspace: str
    interpreter: str
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpawnResult:
    spawned: bool
    errors: tuple[str, ...] = ()
    value: Any = None


def _capability_name(value: str, field_name: str) -> Optional[str]:
    text = str(value or "").strip()
    if not text or not _SAFE_CAPABILITY_NAME.fullmatch(text):
        return f"{field_name}: malformed capability name"
    return None


def preflight_capability(
    request: CapabilityRequest,
    *,
    profiles: Optional[Iterable[str]] = None,
    skills: Optional[Iterable[str]] = None,
    categories: Optional[Iterable[str]] = None,
) -> PreflightResult:
    errors: list[str] = []
    for field_name, value in (
        ("profile", request.target_profile),
        ("skill", request.skill),
        ("category", request.category),
    ):
        error = _capability_name(value, field_name)
        if error:
            errors.append(error)
    catalogues = {
        "profile": profiles,
        "skill": skills,
        "category": categories,
    }
    for field_name, catalogue in catalogues.items():
        if catalogue is None:
            continue
        value = getattr(request, {"profile": "target_profile", "skill": "skill", "category": "category"}[field_name])
        if value not in {str(item).strip() for item in catalogue}:
            errors.append(f"{field_name}: not present in effective catalog")

    workspace = Path(str(request.workspace or "")).expanduser()
    if not workspace.is_absolute() or not workspace.is_dir():
        errors.append("workspace: must be an existing absolute directory")

    interpreter = str(request.interpreter or "").strip()
    resolved_interpreter = Path(interpreter).expanduser() if "/" in interpreter else None
    if resolved_interpreter is not None:
        if not resolved_interpreter.is_file() or not os.access(resolved_interpreter, os.X_OK):
            errors.append("interpreter: executable is unavailable")
    elif not interpreter or shutil.which(interpreter) is None:
        errors.append("interpreter: executable is unavailable")

    for tool in request.tools:
        tool_name = str(tool or "").strip()
        if _capability_name(tool_name, "tool") or shutil.which(tool_name) is None:
            errors.append("tool: executable is unavailable or malformed")
    return PreflightResult(not errors, tuple(dict.fromkeys(errors)))


def spawn_after_preflight(
    request: CapabilityRequest,
    spawn: Callable[[], Any],
    **catalogues: Optional[Iterable[str]],
) -> SpawnResult:
    """Run a spawn callback only after all capability checks pass."""
    result = preflight_capability(request, **catalogues)
    if not result.ok:
        return SpawnResult(False, result.errors)
    try:
        return SpawnResult(True, value=spawn())
    except Exception as exc:
        return SpawnResult(False, (f"spawn: {type(exc).__name__}: {exc}",))


@dataclass(frozen=True)
class ReportValidation:
    ok: bool
    reason: str = ""


def validate_run_bound_report(
    report: Mapping[str, Any],
    identity: Mapping[str, Any] | CanonicalIdentity,
    current_run_id: Optional[int],
    *,
    historical_run_ids: Iterable[int] = (),
) -> ReportValidation:
    if current_run_id is None:
        return ReportValidation(False, "there is no current run")
    try:
        reported_identity = canonical_identity(report)
        expected_identity = canonical_identity(identity)
    except ValueError as exc:
        return ReportValidation(False, str(exc))
    if reported_identity != expected_identity:
        return ReportValidation(False, "report identity does not match current lane")
    reported_run = report.get("current_run_id")
    if reported_run is None:
        return ReportValidation(False, "report is missing current_run_id")
    try:
        reported_run_id = int(reported_run)
    except (TypeError, ValueError):
        return ReportValidation(False, "report current_run_id is malformed")
    if reported_run_id in {int(item) for item in historical_run_ids}:
        return ReportValidation(False, "historical run id is not reportable")
    if reported_run_id != int(current_run_id):
        return ReportValidation(False, "report is bound to a historical or foreign run")
    return ReportValidation(True, "report is bound to the current semantic run")


@dataclass(frozen=True)
class GateRoute:
    route: str
    implementation_retry: bool
    bypass: bool
    reason: str


def route_merged_source_pr(
    *,
    source_pr_state: str,
    artifact_signer_state: str,
    implementation_retry_requested: bool = False,
    bypass_requested: bool = False,
) -> GateRoute:
    merged = str(source_pr_state or "").strip().casefold()
    signer = str(artifact_signer_state or "").strip().casefold()
    if merged == "merged" and signer not in {"passed", "success", "verified"}:
        return GateRoute(
            "artifact/provenance",
            False,
            False,
            "merged source is held at the failed artifact/provenance gate",
        )
    return GateRoute(
        "implementation" if implementation_retry_requested else "next-phase",
        bool(implementation_retry_requested),
        bool(bypass_requested),
        "no protected signer failure was observed",
    )


@dataclass(frozen=True)
class AdmissionDecision:
    action: str
    identity: Optional[CanonicalIdentity]
    reason: str
    reservation_id: Optional[str] = None
    created_descendant: bool = False


class AdmissionController:
    """Select one existing semantic lane after fresh reservation readback."""

    def __init__(self, ledger: AdmissionLedger) -> None:
        self.ledger = ledger
        self.dependencies = DependencyGate()

    def tick(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        tick: int,
        now: Optional[int] = None,
        global_wip: int = 0,
        global_wip_limit: Optional[int] = None,
        profile_wip: Optional[Mapping[str, int]] = None,
        profile_limits: Optional[Mapping[str, int]] = None,
    ) -> AdmissionDecision:
        del now
        candidates: list[tuple[CanonicalIdentity, Mapping[str, Any]]] = []
        holds: list[str] = []
        for row in rows:
            status = str(row.get("status") or "").casefold()
            if status not in {"blocked", "ready", "todo", "triage"}:
                continue
            try:
                identity = canonical_identity(row)
            except ValueError:
                holds.append("malformed canonical identity")
                continue
            recurrence_value = row.get("block_recurrences")
            is_blocker = status == "blocked" or any(
                row.get(name) not in (None, "", 0)
                for name in ("block_kind", "blocker_class", "reason_code")
            ) or recurrence_value not in (None, "", 0)
            if is_blocker:
                try:
                    self.ledger.observe_blocker(row, tick=tick)
                except ValueError:
                    holds.append("malformed blocker evidence")
                    continue
            if status == "blocked":
                holds.append("blocker remains blocked")
                continue
            if self.ledger.is_fenced(identity):
                holds.append("canonical blocker remains fenced")
                continue
            try:
                durable_recurrence = int(row.get("block_recurrences") or 0)
            except (TypeError, ValueError):
                holds.append("malformed blocker recurrence")
                continue
            if durable_recurrence < 0:
                holds.append("negative blocker recurrence")
                continue
            if status == "triage" and durable_recurrence >= NATIVE_QUARANTINE_RECURRENCE:
                holds.append("quarantined blocker remains fenced")
                continue
            route = route_merged_source_pr(
                source_pr_state=str(row.get("source_pr_state") or ""),
                artifact_signer_state=str(row.get("artifact_signer_state") or ""),
            )
            if route.route == "artifact/provenance":
                holds.append("merged source is held at the artifact/provenance gate")
                continue
            if str(row.get("block_kind") or "").casefold() == "dependency":
                parents = row.get("parents")
                decision = self.dependencies.reconcile(str(row.get("id") or identity.key), parents, tick=tick)
                if decision.action != "resume":
                    holds.append(decision.reason)
                    continue
            owner = str(row.get("phase_owner") or row.get("owner") or "controller").strip()
            phase = str(row.get("phase") or identity.phase).strip()
            existing = self.ledger.reservations.get((identity.key, phase.casefold()))
            if existing is not None and tick <= existing.fresh_until:
                holds.append("fresh live owner reservation prevents recovery descendant")
                continue
            profile = str(row.get("target_profile") or row.get("assignee") or owner).strip()
            if global_wip_limit is not None and global_wip >= int(global_wip_limit):
                holds.append("global WIP limit is full")
                continue
            if (
                profile_limits is not None
                and profile_wip is not None
                and int(profile_wip.get(profile, 0)) >= int(profile_limits.get(profile, 0))
            ):
                holds.append("profile WIP limit is full")
                continue
            candidates.append((identity, row))
        if not candidates:
            reason = holds[0] if holds else "no safe canonical parent-complete lane"
            return AdmissionDecision("hold", None, reason)
        candidates.sort(key=lambda pair: (int(pair[1].get("priority") or 0), pair[0].key))
        identity, row = candidates[0]
        phase = str(row.get("phase") or identity.phase)
        reservation = self.ledger.reserve_phase(
            identity,
            owner=str(row.get("phase_owner") or row.get("owner") or "controller"),
            phase=phase,
            tick=tick,
            freshness=max(1, int(row.get("reservation_freshness") or 1)),
            idempotency_key=f"phase-reservation:{identity.digest}:{phase.casefold()}",
        )
        if not reservation.accepted:
            return AdmissionDecision("hold", identity, reservation.reason)
        return AdmissionDecision(
            "admit",
            identity,
            "one existing canonical lane reserved and read back",
            reservation.reservation_id,
            False,
        )


@dataclass(frozen=True)
class NativeAdapterResult:
    """Read-back result from the Factory-owned native specify adapter."""

    task_id: str
    accepted: bool
    typed_outcome: str
    reason: str
    native_outcome: Any = None


def _native_task_mapping(task: Any) -> dict[str, Any]:
    if isinstance(task, Mapping):
        return dict(task)
    fields = getattr(task, "__dataclass_fields__", {})
    if isinstance(fields, Mapping):
        return {
            name: getattr(task, name)
            for name in fields
            if hasattr(task, name)
        }
    return {
        name: getattr(task, name)
        for name in (
            "id",
            "title",
            "body",
            "status",
            "block_recurrences",
            "block_kind",
            "source_repository",
            "source_item",
            "phase",
            "input_artifact_revision",
        )
        if hasattr(task, name)
    }


def run_guarded_native_specify(
    task_id: str,
    *,
    native_module: Any = None,
    db_module: Any = None,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
    resolution_evidence: Optional[Mapping[str, Any]] = None,
    ledger: Optional[AdmissionLedger] = None,
) -> NativeAdapterResult:
    """Invoke the supported native transition through the Factory guard.

    The installed native runtime currently has no admission callback.  This
    adapter therefore wraps the exact imported ``specify_task`` path and
    re-checks immediately at its write boundary.  Calls made directly to the
    unmodified native CLI remain outside this adapter and are reported as an
    upstream hook limitation rather than being claimed as protected.
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        return NativeAdapterResult(
            task_id,
            False,
            "UPSTREAM_HANDOFF_REQUIRED",
            "native task id is missing",
        )
    try:
        runtime = os.environ.get("FACTORY_NATIVE_RUNTIME", "").strip()
        if runtime and Path(runtime).is_dir() and runtime not in sys.path:
            sys.path.insert(0, runtime)
        native = native_module or importlib.import_module("hermes_cli.kanban_specify")
        db = db_module or importlib.import_module("hermes_cli.kanban_db")
    except (AttributeError, ImportError, OSError, RuntimeError) as exc:
        return NativeAdapterResult(
            task_id,
            False,
            "UPSTREAM_HANDOFF_REQUIRED",
            f"native admission hook is unavailable: {type(exc).__name__}",
        )
    runtime_root = os.environ.get("FACTORY_NATIVE_RUNTIME", "").strip()
    if runtime_root:
        root = Path(runtime_root).resolve()
        module_paths = [
            Path(str(getattr(native, "__file__", ""))).resolve(),
            Path(str(getattr(db, "__file__", ""))).resolve(),
        ]
        if any(not path.is_relative_to(root) for path in module_paths):
            return NativeAdapterResult(
                task_id,
                False,
                "UPSTREAM_HANDOFF_REQUIRED",
                "native modules are outside the declared runtime",
            )

    try:
        with db.connect_closing() as conn:
            current = db.get_task(conn, task_id)
    except (AttributeError, OSError, RuntimeError, sqlite3.Error) as exc:
        return NativeAdapterResult(
            task_id,
            False,
            "UPSTREAM_HANDOFF_REQUIRED",
            f"native task readback failed: {type(exc).__name__}",
        )
    if current is None:
        return NativeAdapterResult(task_id, False, "NOT_ADMITTED", "unknown task id")
    current_mapping = _native_task_mapping(current)
    if str(current_mapping.get("status") or "") != "triage":
        return NativeAdapterResult(
            task_id,
            False,
            "NOT_ADMITTED",
            f"task is not in triage (status={current_mapping.get('status')!r})",
        )

    active_ledger = ledger or AdmissionLedger()
    precheck = native_specification_guard(
        current_mapping,
        proposed_title=None,
        proposed_body=None,
        resolution_evidence=resolution_evidence,
        ledger=active_ledger,
    )
    if not precheck.accepted:
        return NativeAdapterResult(task_id, False, "QUARANTINED", precheck.reason)

    original_writer: Any = getattr(db, "specify_triage_task", None)
    original_write_txn: Any = getattr(db, "write_txn", None)
    if not callable(original_writer) or not callable(original_write_txn):
        return NativeAdapterResult(
            task_id,
            False,
            "UPSTREAM_HANDOFF_REQUIRED",
            "native specify write boundary or transaction primitive is unavailable",
        )

    def guarded_writer(conn: Any, guarded_task_id: str, **kwargs: Any) -> bool:
        if str(guarded_task_id) != task_id:
            return False
        latest = db.get_task(conn, task_id)
        if latest is None:
            return False
        latest_mapping = _native_task_mapping(latest)
        decision = native_specification_guard(
            latest_mapping,
            proposed_title=kwargs.get("title"),
            proposed_body=kwargs.get("body"),
            resolution_evidence=resolution_evidence,
            ledger=active_ledger,
        )
        if not decision.accepted:
            return False
        # The native writer opens its own write_txn.  Run it under this adapter's
        # outer IMMEDIATE transaction and make that inner call a savepoint, so
        # the guard read and the native status transition share one atomic
        # boundary.  Direct native callers still bypass this adapter and are
        # intentionally reported as an upstream-hook limitation.
        with original_write_txn(conn):
            latest = db.get_task(conn, task_id)
            if latest is None:
                return False
            latest_mapping = _native_task_mapping(latest)
            decision = native_specification_guard(
                latest_mapping,
                proposed_title=kwargs.get("title"),
                proposed_body=kwargs.get("body"),
                resolution_evidence=resolution_evidence,
                ledger=active_ledger,
            )
            if not decision.accepted:
                return False

            def nested_write_txn(connection: Any, **_options: Any) -> Any:
                return original_write_txn(connection, allow_nested=True)

            db.__dict__["write_txn"] = nested_write_txn
            try:
                return bool(original_writer(conn, guarded_task_id, **kwargs))
            finally:
                db.__dict__["write_txn"] = original_write_txn

    with _NATIVE_ADAPTER_LOCK:
        db.__dict__["specify_triage_task"] = guarded_writer
        try:
            outcome = native.specify_task(task_id, author=author, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - type all provider/runtime failures
            return NativeAdapterResult(
                task_id,
                False,
                "UPSTREAM_HANDOFF_REQUIRED",
                f"native adapter invocation failed: {type(exc).__name__}",
            )
        finally:
            db.__dict__["specify_triage_task"] = original_writer

    try:
        with db.connect_closing() as conn:
            after = db.get_task(conn, task_id)
    except (AttributeError, OSError, RuntimeError, sqlite3.Error) as exc:
        return NativeAdapterResult(
            task_id,
            False,
            "UPSTREAM_HANDOFF_REQUIRED",
            f"native post-write readback failed: {type(exc).__name__}",
            outcome,
        )
    after_mapping = _native_task_mapping(after) if after is not None else {}
    accepted = bool(getattr(outcome, "ok", False)) and str(after_mapping.get("status") or "") != "triage"
    return NativeAdapterResult(
        task_id,
        accepted,
        "ADMITTED" if accepted else "NOT_ADMITTED",
        str(getattr(outcome, "reason", "native transition did not commit")),
        outcome,
    )


def native_specification_guard(
    task: Mapping[str, Any],
    *,
    proposed_title: Optional[str],
    proposed_body: Optional[str],
    resolution_evidence: Optional[Mapping[str, Any]] = None,
    ledger: Optional[AdmissionLedger] = None,
) -> AdmissionResult:
    """Compatibility decision for a native triage adapter.

    A native integration must call this decision inside its atomic triage write
    boundary.  The standalone cron guard can quarantine before that boundary,
    but a post-transition sweep cannot prove the invariant.
    """
    del proposed_title, proposed_body
    try:
        recurrence = int(task.get("block_recurrences") or 0)
    except (TypeError, ValueError):
        return AdmissionResult(False, False, False, "rejected", None, "block recurrence is malformed")
    if recurrence < 0:
        return AdmissionResult(False, False, False, "rejected", None, "block recurrence is negative")
    if recurrence >= NATIVE_QUARANTINE_RECURRENCE:
        if resolution_evidence is None or ledger is None:
            return AdmissionResult(
                False,
                False,
                False,
                "fenced",
                None,
                "native triage promotion is fenced pending evidence-backed resolution",
            )
        resolution = ledger.resolve_blocker(task, resolution_evidence, new_contract=True)
        if not resolution.accepted:
            return AdmissionResult(False, False, False, "fenced", None, resolution.reason)
    return AdmissionResult(True, False, False, "admissible", None, "native triage promotion requires adapter readback")


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", default=os.environ.get("HERMES_FACTORY_BOARD", "minna"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--native-specify",
        metavar="TASK_ID",
        help="invoke the imported native triage transition through the guard",
    )
    ap.add_argument("--native-author")
    ap.add_argument("--native-timeout", type=int)
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.native_specify:
        if not args.apply:
            print("--native-specify requires --apply", file=sys.stderr)
            return 2
        result = run_guarded_native_specify(
            args.native_specify,
            author=args.native_author,
            timeout=args.native_timeout,
        )
        print(
            json.dumps(
                {
                    "task_id": result.task_id,
                    "accepted": result.accepted,
                    "typed_outcome": result.typed_outcome,
                    "reason": result.reason,
                },
                sort_keys=True,
            )
        )
        return 2 if result.typed_outcome == "UPSTREAM_HANDOFF_REQUIRED" else 0
    try:
        changes = quarantine(args.board, apply=args.apply)
        changes += sweep(args.board, apply=args.apply)
    except Exception as exc:
        print(f"packet guard failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if changes and not args.quiet:
        print("\n".join(changes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
