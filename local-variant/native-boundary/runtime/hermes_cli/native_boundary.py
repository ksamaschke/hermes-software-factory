"""Fail-closed native boundary decisions for the versioned Factory artifact.

This module is added to a disposable copied Hermes runtime by the artifact
patch.  It deliberately contains only pure decision helpers and narrow SQL
reads; callers own the surrounding native write transaction and guard order.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

# These are lifecycle transitions that can establish an explicit re-admission.
# Comments are intentionally absent: text is evidence, never authority.
_REQUEUE_EVENT_KINDS = frozenset(
    {"unblocked", "promoted", "reclaimed", "specified", "status"}
)
_OBSERVATION_EVENT_KINDS = ("commented", "respawn_guarded")
# SQLite stores a bound Python ``True`` as integer ``1``.  No real Hermes
# lifecycle row predates 2000-01-01, so a plausibility floor keeps that lossy
# coercion from becoming durable timestamp authority.
_MIN_LIFECYCLE_TIMESTAMP = 946_684_800
_MAX_TRANSITION_PAYLOAD_CHARS = 65_536
_PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")

# Canonical terminal task-run state pairs emitted by the native kernel.  A
# synthetic terminal row uses the outcome as its status; claimed runs use the
# task landing phase for completion/review handoffs.  This explicit matrix
# prevents a non-empty but impossible status/outcome combination from becoming
# durable re-admission authority.
_TERMINAL_RUN_STATE_PAIRS = frozenset(
    {
        ("blocked", "blocked"),
        ("completed", "completed"),
        ("done", "completed"),
        ("review_requested", "review_requested"),
        ("review", "review_requested"),
        ("ready", "changes_requested"),
        ("todo", "changes_requested"),
        ("reclaimed", "reclaimed"),
        ("todo", "reclaimed"),
        ("scheduled", "scheduled"),
        ("timed_out", "timed_out"),
        ("stale", "stale"),
        ("crashed", "crashed"),
        ("rate_limited", "rate_limited"),
        ("gave_up", "gave_up"),
        ("spawn_failed", "spawn_failed"),
    }
)


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
    recurrences = _row_value(existing, "block_recurrences")
    limit = recurrence_limit
    if (
        type(recurrences) is not int
        or recurrences < 0
        or type(limit) is not int
        or limit < 1
    ):
        return "invalid quarantine state"
    if recurrences < limit:
        return None

    # A free-form title/body from a specifier is not authority that the
    # blocker was resolved.  Rephrasing the same request, or inventing a
    # lexically novel request, must not launder the quarantine.  Only a
    # separate durable lifecycle resolution/requeue may clear this boundary.
    return "repeated blocker remains quarantined pending explicit resolution"


def _json_object(payload: str | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    if type(payload) is not str:
        return None
    if len(payload) > _MAX_TRANSITION_PAYLOAD_CHARS:
        return None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON object member")
            value[key] = item
        return value

    try:
        value = json.loads(payload, object_pairs_hook=unique_object)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def terminal_run_state_is_authorized(status: Any, outcome: Any) -> bool:
    """Return whether durable run status/outcome form a native terminal pair."""
    return (
        type(status) is str
        and type(outcome) is str
        and (status, outcome) in _TERMINAL_RUN_STATE_PAIRS
    )


def _canonical_profile_id(value: Any) -> str | None:
    if type(value) is not str or _PROFILE_ID_RE.fullmatch(value) is None:
        return None
    return value


def _optional_reclaim_metadata_is_canonical(value: dict[str, Any]) -> bool:
    """Validate optional fields emitted by native reclaim implementations."""
    if (
        "reason" in value
        and value["reason"] is not None
        and type(value["reason"]) is not str
    ):
        return False
    for key in ("worker_pid", "prev_pid"):
        item = value.get(key)
        if (
            key in value
            and item is not None
            and (type(item) is not int or item <= 0)
        ):
            return False
    if "last_heartbeat_at" in value:
        heartbeat = value["last_heartbeat_at"]
        if (
            heartbeat is not None
            and validated_lifecycle_timestamp(heartbeat) is None
        ):
            return False
    for key in (
        "host_local",
        "heartbeat_stale",
        "termination_attempted",
        "terminated",
        "sigkill",
    ):
        if key in value and type(value[key]) is not bool:
            return False
    return True


def validated_lifecycle_timestamp(
    value: Any,
    *,
    maximum: int | None = None,
) -> int | None:
    """Return a bounded positive SQLite integer timestamp, else fail closed."""
    if (
        type(value) is not int
        or value < _MIN_LIFECYCLE_TIMESTAMP
    ):
        return None
    if maximum is not None:
        if (
            type(maximum) is not int
            or maximum < _MIN_LIFECYCLE_TIMESTAMP
            or value > maximum
        ):
            return None
    return value


def requeue_transition_is_authorized(kind: Any, payload: Any) -> bool:
    """Validate durable transition evidence without trusting SQLite types."""
    if type(kind) is not str or kind not in _REQUEUE_EVENT_KINDS:
        return False
    if kind == "status":
        value = _json_object(payload)
        status = value.get("status") if value is not None else None
        return (
            value is not None
            and frozenset(value) == {"status"}
            and type(status) is str
            and status in {"ready", "todo"}
        )
    if kind == "promoted":
        if payload is None:
            return True
        value = _json_object(payload)
        status = value.get("status") if value is not None else None
        return (
            value is not None
            and frozenset(value) == {"status"}
            and type(status) is str
            and status in {"ready", "todo"}
        )
    if kind == "unblocked":
        # The native direct blocked -> ready event deliberately stores SQL
        # NULL.  Object/scalar payloads describe another landing phase or are
        # malformed and cannot authorize this ready-lane continuation.
        return payload is None
    if kind == "reclaimed":
        value = _json_object(payload)
        if value is None:
            return False
        retry_status = value.get("retry_status")
        if type(retry_status) is not str or retry_status not in {"ready", "todo"}:
            return False
        if value.get("manual") is True:
            if frozenset(value) != frozenset(
                {
                    "manual",
                    "reason",
                    "prev_lock",
                    "retry_status",
                    "prev_pid",
                    "host_local",
                    "termination_attempted",
                    "terminated",
                    "sigkill",
                }
            ):
                return False
            previous_lock = value.get("prev_lock")
            return (
                previous_lock is None
                or (
                    type(previous_lock) is str
                    and bool(previous_lock.strip())
                )
            ) and _optional_reclaim_metadata_is_canonical(value)
        if frozenset(value) != frozenset(
            {
                "stale_lock",
                "worker_pid",
                "claim_expires",
                "last_heartbeat_at",
                "now",
                "host_local",
                "heartbeat_stale",
                "retry_status",
                "prev_pid",
                "termination_attempted",
                "terminated",
                "sigkill",
            }
        ):
            return False
        if not _optional_reclaim_metadata_is_canonical(value):
            return False
        if value["worker_pid"] != value["prev_pid"]:
            return False
        stale_lock = value.get("stale_lock")
        observed_now = validated_lifecycle_timestamp(
            value.get("now"), maximum=int(time.time())
        )
        claim_expires = validated_lifecycle_timestamp(
            value.get("claim_expires"), maximum=observed_now
        )
        heartbeat = value["last_heartbeat_at"]
        if (
            heartbeat is not None
            and validated_lifecycle_timestamp(
                heartbeat, maximum=observed_now
            )
            is None
        ):
            return False
        return (
            type(stale_lock) is str
            and bool(stale_lock.strip())
            and observed_now is not None
            and claim_expires is not None
            and claim_expires < observed_now
        )
    if kind == "specified":
        value = _json_object(payload)
        if value is None:
            return False
        if frozenset(value) != frozenset(
            {
                "changed_fields",
                "previous_status",
                "status",
                "block_recurrences_reset",
            }
        ):
            return False
        changed_fields = value.get("changed_fields")
        previous_status = value.get("previous_status")
        status = value.get("status")
        return (
            type(previous_status) is str
            and previous_status in {"blocked", "triage"}
            and type(status) is str
            and status in {"ready", "todo"}
            and type(changed_fields) is list
            and bool(changed_fields)
            and all(
                type(field) is str and bool(field.strip())
                for field in changed_fields
            )
            and len(set(changed_fields)) == len(changed_fields)
            and set(changed_fields).issubset({"title", "body", "assignee"})
            and "body" in changed_fields
            and value["block_recurrences_reset"] is True
        )
    return False


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
    consecutive_failures = row["consecutive_failures"]
    if type(consecutive_failures) is not int or consecutive_failures != 0:
        return False

    owner = _canonical_profile_id(row["assignee"])
    if owner is None:
        return False

    prior_run = conn.execute(
        "SELECT id, profile, status, outcome, ended_at FROM task_runs "
        "WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if prior_run is None:
        return False
    profile = _canonical_profile_id(prior_run["profile"])
    if profile is None or profile != owner:
        return False
    if not terminal_run_state_is_authorized(
        prior_run["status"], prior_run["outcome"]
    ) or prior_run["outcome"] != "blocked":
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
    if not requeue_transition_is_authorized(
        transition["kind"], transition["payload"]
    ):
        return False
    now = int(time.time())
    transition_created_at = validated_lifecycle_timestamp(
        transition["created_at"], maximum=now
    )
    prior_run_ended_at = validated_lifecycle_timestamp(
        prior_run["ended_at"], maximum=now
    )
    if transition_created_at is None or prior_run_ended_at is None:
        return False
    if transition_created_at < prior_run_ended_at:
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
