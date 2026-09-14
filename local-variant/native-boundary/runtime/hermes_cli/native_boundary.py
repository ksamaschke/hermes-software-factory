"""Fail-closed native boundary decisions for the versioned Factory artifact.

This module is added to a disposable copied Hermes runtime by the artifact
patch.  It deliberately contains only pure decision helpers and narrow SQL
reads; callers own the surrounding native write transaction and guard order.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

# These are lifecycle transitions that can establish an explicit re-admission.
# Comments are intentionally absent: text is evidence, never authority.
_REQUEUE_EVENT_KINDS = frozenset({"unblocked", "promoted", "reclaimed", "status"})
_OBSERVATION_EVENT_KINDS = ("commented", "respawn_guarded")


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    """Read a sqlite row or mapping without requiring a particular row class."""
    try:
        keys = row.keys()
    except AttributeError:
        return default
    return row[key] if key in keys else default


def triage_admission_rejection(
    existing: Any,
    *,
    title: str | None,
    body: str | None,
    recurrence_limit: int,
) -> str | None:
    """Return a rejection reason for a repeated-blocker triage admission.

    ``existing`` must be the row read inside the caller's active native write
    transaction.  The helper does not write and therefore cannot be used as an
    out-of-transaction preflight.  Fresh triage rows (zero recurrences) are
    always left to the native implementation.
    """
    try:
        recurrences = int(_row_value(existing, "block_recurrences", 0) or 0)
        limit = int(recurrence_limit)
    except (TypeError, ValueError):
        return "invalid quarantine state"
    if recurrences < max(1, limit):
        return None

    # A free-form title/body from a specifier is not authority that the
    # blocker was resolved.  Rephrasing the same request, or inventing a
    # lexically novel request, must not launder the quarantine.  Only a
    # separate durable lifecycle resolution/requeue may clear this boundary.
    return "repeated blocker remains quarantined pending explicit resolution"


MAX_DURABLE_EVENT_PAYLOAD_CHARS = 16_384
MAX_DURABLE_EVENT_NESTING_DEPTH = 64


def _json_nesting_within_bound(payload: str) -> bool:
    """Preflight JSON structure without recursively materializing it."""
    depth = 0
    in_string = False
    escaped = False
    for char in payload:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DURABLE_EVENT_NESTING_DEPTH:
                return False
        elif char in "]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _json_object(payload: Any) -> dict[str, Any]:
    if type(payload) is not str:
        return {}
    if len(payload) > MAX_DURABLE_EVENT_PAYLOAD_CHARS:
        return {}
    if not _json_nesting_within_bound(payload):
        return {}
    try:
        value = json.loads(payload) if payload else {}
    except (
        TypeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
        MemoryError,
        OverflowError,
    ):
        return {}
    return value if isinstance(value, dict) else {}


DEPENDENCY_WAIT_SCHEMA = "factory.dependency-wait.v1"
DEPENDENCY_WAIT_QUARANTINE_SCHEMA = "factory.dependency-wait-quarantine.v1"
MAX_DEPENDENCY_WAIT_PARENTS = 256
_DEPENDENCY_WAIT_PARENT_ERROR = (
    "dependency block requires at least one unfinished direct parent"
)


def _valid_parent_id(value: Any) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 128
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def dependency_wait_parent_ids(
    conn: sqlite3.Connection,
    task_id: str,
) -> tuple[str, ...]:
    """Return a bounded snapshot of unfinished direct parents or fail closed."""
    rows = conn.execute(
        "SELECT p.id AS parent_id FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? "
        "AND p.status NOT IN ('done', 'archived') "
        "ORDER BY p.id LIMIT ?",
        (task_id, MAX_DEPENDENCY_WAIT_PARENTS + 1),
    ).fetchall()
    if not rows:
        raise ValueError(_DEPENDENCY_WAIT_PARENT_ERROR)
    if len(rows) > MAX_DEPENDENCY_WAIT_PARENTS:
        raise ValueError("dependency block has too many unfinished direct parents")
    parent_ids = tuple(_row_value(row, "parent_id") for row in rows)
    if any(not _valid_parent_id(parent_id) for parent_id in parent_ids):
        raise ValueError("dependency block has an invalid direct parent identity")
    if len(set(parent_ids)) != len(parent_ids):
        raise ValueError("dependency block has duplicate direct parent identities")
    return parent_ids


def dependency_wait_promotion_rejection(
    conn: sqlite3.Connection,
    task_id: str,
) -> str | None:
    """Validate durable parent evidence before re-admission.

    Legacy dependency waits did not record which parents were unfinished when
    the task parked.  Their state is ambiguous and must be quarantined instead
    of being promoted or claimed after activation.
    """
    event = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'dependency_wait' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    payload = _json_object(_row_value(event, "payload")) if event else {}
    if payload.get("schema") != DEPENDENCY_WAIT_SCHEMA:
        return "dependency wait lacks an authenticated parent snapshot"
    parent_ids = payload.get("waiting_parent_ids")
    if type(parent_ids) is not list or not parent_ids:
        return "dependency wait has an invalid parent snapshot"
    if len(parent_ids) > MAX_DEPENDENCY_WAIT_PARENTS:
        return "dependency wait parent snapshot exceeds the configured bound"
    if any(not _valid_parent_id(parent_id) for parent_id in parent_ids):
        return "dependency wait has an invalid parent identity"
    if len(set(parent_ids)) != len(parent_ids):
        return "dependency wait has duplicate parent identities"

    for parent_id in parent_ids:
        parent = conn.execute(
            "SELECT p.status FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.id = ?",
            (task_id, parent_id),
        ).fetchone()
        if parent is None:
            return "dependency wait parent is missing from the current graph"
        if _row_value(parent, "status") not in {"done", "archived"}:
            return "dependency wait parent is not terminal"
    return None


_DEPENDENCY_WAIT_PHASE_EVENTS = (
    "dependency_wait",
    "review_requested",
    "changes_requested",
    "review_reopened",
    "completed",
    "archived",
    "blocked",
    "unblocked",
    "status",
    "deleted",
)


def dependency_wait_requires_validation(
    conn: sqlite3.Connection,
    task_id: str,
    block_kind: Any,
) -> bool:
    """Return whether admission must authenticate a dependency wait.

    Promotion and claim/retry events deliberately do not supersede a wait: a
    resumed task must retain the parent proof until a new review, rework,
    terminal, explicit-unblock, or manual-status phase begins.  If no relevant
    event exists, a durable dependency marker is treated as malformed and
    therefore still requires validation.
    """
    placeholders = ", ".join("?" for _ in _DEPENDENCY_WAIT_PHASE_EVENTS)
    event = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? "
        f"AND kind IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (task_id, *_DEPENDENCY_WAIT_PHASE_EVENTS),
    ).fetchone()
    if event is None:
        return block_kind == "dependency"
    return _row_value(event, "kind") == "dependency_wait"


def dependency_wait_quarantine_is_sticky(
    conn: sqlite3.Connection,
    task_id: str,
) -> bool:
    """Return whether a dependency quarantine lacks an explicit unblock."""
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('blocked', 'unblocked') ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if event is None or _row_value(event, "kind") != "blocked":
        return False
    payload = _json_object(_row_value(event, "payload"))
    return payload.get("schema") == DEPENDENCY_WAIT_QUARANTINE_SCHEMA


def same_owner_requeue_is_authorized(
    conn: sqlite3.Connection,
    task_id: str,
) -> bool:
    """Return whether a ready task has durable same-owner re-admission proof.

    The evidence is intentionally independent of comments: a prior native run
    ended as ``blocked``, the task was explicitly requeued by a lifecycle
    transition, and the current assignee is the same durable run profile. The
    latest non-observation lifecycle event must still be that transition, so a
    later PR URL comment or guard telemetry cannot revoke it while a
    comment-only claim cannot create it.
    """
    row = conn.execute(
        "SELECT status, assignee, claim_lock, current_run_id, "
        "       consecutive_failures "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    if row["status"] != "ready":
        return False
    if row["claim_lock"] is not None or row["current_run_id"] is not None:
        return False

    # A task still carrying a retry-failure streak is not converted into a
    # same-writer continuation by a PR comment or a stale transition.
    try:
        if int(row["consecutive_failures"] or 0) > 0:
            return False
    except (TypeError, ValueError):
        return False

    owner = str(row["assignee"] or "").strip().casefold()
    if not owner:
        return False

    prior_run = conn.execute(
        "SELECT id, profile, outcome, ended_at FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if prior_run is None:
        return False
    if str(prior_run["profile"] or "").strip().casefold() != owner:
        return False
    if prior_run["outcome"] != "blocked":
        return False
    if prior_run["ended_at"] is None:
        return False

    transition = conn.execute(
        "SELECT id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind IN ('unblocked', 'promoted', 'reclaimed', 'status') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if transition is None:
        return False
    if transition["kind"] not in _REQUEUE_EVENT_KINDS:
        return False
    if int(transition["created_at"] or 0) < int(prior_run["ended_at"] or 0):
        return False

    if transition["kind"] == "status":
        payload = _json_object(transition["payload"])
        if payload.get("status") not in {"ready", "todo"}:
            return False
    elif transition["kind"] == "promoted":
        payload = _json_object(transition["payload"])
        if payload and payload.get("status") not in {"ready", "todo"}:
            return False

    # Comments and guard telemetry are deliberately ignored here. They are
    # observations, not lifecycle changes: the native dispatcher emits a
    # ``respawn_guarded`` event on every suppressed tick, and those events
    # must not consume the legacy explicit requeue proof.
    placeholders = ", ".join("?" for _ in _OBSERVATION_EVENT_KINDS)
    latest = conn.execute(
        "SELECT id, kind FROM task_events WHERE task_id = ? "
        f"AND kind NOT IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (task_id, *_OBSERVATION_EVENT_KINDS),
    ).fetchone()
    return latest is not None and int(latest["id"]) == int(transition["id"])
