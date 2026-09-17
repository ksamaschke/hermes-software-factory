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
_REQUEUE_EVENT_KINDS = frozenset(
    {"unblocked", "promoted", "reclaimed", "specified", "status"}
)
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


def _json_object(payload: str | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


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
        "WHERE task_id = ? AND kind IN "
        "('unblocked', 'promoted', 'reclaimed', 'specified', 'status') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if transition is None:
        return False
    if transition["kind"] not in _REQUEUE_EVENT_KINDS:
        return False
    transition_created_at = transition["created_at"]
    prior_run_ended_at = prior_run["ended_at"]
    if type(transition_created_at) is not int or type(prior_run_ended_at) is not int:
        return False
    if transition_created_at <= 0 or prior_run_ended_at <= 0:
        return False
    if transition_created_at < prior_run_ended_at:
        return False

    if transition["kind"] == "status":
        payload = _json_object(transition["payload"])
        if payload is None or payload.get("status") not in {"ready", "todo"}:
            return False
    elif transition["kind"] == "promoted":
        raw_payload = transition["payload"]
        if raw_payload is not None:
            payload = _json_object(raw_payload)
            if payload is None or payload.get("status") not in {"ready", "todo"}:
                return False
    elif transition["kind"] == "specified":
        payload = _json_object(transition["payload"])
        if payload is None:
            return False
        changed_fields = payload.get("changed_fields")
        if payload.get("previous_status") not in {"blocked", "triage"}:
            return False
        if payload.get("status") not in {"ready", "todo"}:
            return False
        if not isinstance(changed_fields, list) or "body" not in changed_fields:
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
