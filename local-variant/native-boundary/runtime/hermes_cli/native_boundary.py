"""Fail-closed native boundary decisions for the versioned Factory artifact.

This module is added to a disposable copied Hermes runtime by the artifact
patch.  It deliberately contains only pure decision helpers and narrow SQL
reads; callers own the surrounding native write transaction and guard order.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import time
from typing import Any

# These are lifecycle transitions that can establish an explicit re-admission.
# Comments are intentionally absent: text is evidence, never authority.
_REQUEUE_EVENT_KINDS = frozenset(
    {
        "changes_requested",
        "unblocked",
        "promoted",
        "reclaimed",
        "specified",
        "status",
    }
)
_OBSERVATION_EVENT_KINDS = ("commented", "respawn_guarded")
# SQLite stores a bound Python ``True`` as integer ``1``.  No real Hermes
# lifecycle row predates 2000-01-01, so a plausibility floor keeps that lossy
# coercion from becoming durable timestamp authority.
_MIN_LIFECYCLE_TIMESTAMP = 946_684_800
_MAX_TRANSITION_PAYLOAD_CHARS = 65_536
_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 60 * 60
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
        ("completed", "review_requested"),
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


def _positive_row_id(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _changes_requested_payload(payload: Any) -> dict[str, Any] | None:
    value = _json_object(payload)
    if value is None or frozenset(value) != frozenset(
        {"reason", "implementer", "reviewer", "status"}
    ):
        return None
    reason = value["reason"]
    implementer = _canonical_profile_id(value["implementer"])
    reviewer = _canonical_profile_id(value["reviewer"])
    status = value["status"]
    if (
        type(reason) is not str
        or not reason.strip()
        or reason != reason.strip()
        or implementer is None
        or reviewer is None
        or type(status) is not str
        or status not in {"ready", "todo"}
    ):
        return None
    return value


def _review_requested_payload(payload: Any) -> dict[str, Any] | None:
    value = _json_object(payload)
    if value is None or frozenset(value) != frozenset(
        {"summary", "implementer", "reviewer"}
    ):
        return None
    summary = value["summary"]
    if summary is not None and (
        type(summary) is not str
        or not summary.strip()
        or summary != summary.strip()
        or len(summary) > 400
    ):
        return None
    if _canonical_profile_id(value["implementer"]) is None:
        return None
    if _canonical_profile_id(value["reviewer"]) is None:
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


def _termination_metadata_is_consistent(
    value: dict[str, Any],
    *,
    lock: Any,
) -> bool:
    host_local = value["host_local"]
    attempted = value["termination_attempted"]
    terminated = value["terminated"]
    sigkill = value["sigkill"]
    previous_pid = value["prev_pid"]
    if previous_pid is not None and (
        type(previous_pid) is not int or previous_pid <= 0
    ):
        return False
    local_prefix = f"{socket.gethostname() or 'unknown-host'}:"
    lock_is_local = type(lock) is str and lock.startswith(local_prefix)
    expected_host_local = previous_pid is not None and lock_is_local
    if host_local is not expected_host_local:
        return False
    if attempted is not host_local:
        return False
    if terminated and not attempted:
        return False
    if sigkill and not attempted:
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
    if kind == "changes_requested":
        return _changes_requested_payload(payload) is not None
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
            ) and _optional_reclaim_metadata_is_canonical(
                value
            ) and _termination_metadata_is_consistent(
                value, lock=previous_lock
            )
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
        heartbeat_at = (
            validated_lifecycle_timestamp(heartbeat, maximum=observed_now)
            if heartbeat is not None
            else None
        )
        if heartbeat is not None and heartbeat_at is None:
            return False
        if observed_now is None or claim_expires is None:
            return False
        if type(stale_lock) is not str or not stale_lock.strip():
            return False
        expected_heartbeat_stale = (
            heartbeat_at is not None
            and (observed_now - heartbeat_at)
            > _CLAIM_HEARTBEAT_MAX_STALE_SECONDS
        )
        if value["heartbeat_stale"] is not expected_heartbeat_stale:
            return False
        return (
            type(stale_lock) is str
            and bool(stale_lock.strip())
            and claim_expires < observed_now
            and _termination_metadata_is_consistent(
                value, lock=stale_lock
            )
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
        changed_fields = _canonical_specified_fields(value.get("changed_fields"))
        previous_status = value.get("previous_status")
        status = value.get("status")
        return (
            type(previous_status) is str
            and previous_status in {"blocked", "triage"}
            and type(status) is str
            and status in {"ready", "todo"}
            and changed_fields is not None
            and "body" in changed_fields
            and value["block_recurrences_reset"] is True
        )
    return False


def _canonical_specified_fields(value: Any) -> list[str] | None:
    """Return an exact native changed-fields list, otherwise ``None``."""
    if type(value) is not list or not value:
        return None
    if not all(type(field) is str for field in value):
        return None
    native_order = [
        field for field in ("title", "body", "assignee") if field in value
    ]
    if value != native_order:
        return None
    return value


def _native_specified_payload_is_canonical(payload: Any) -> bool:
    """Validate both native ``specified`` producer storage forms.

    ``specify_triage_task`` stores SQL NULL when no material field changes,
    otherwise only ``changed_fields``. ``respecify_idle_task`` stores the full
    transition envelope. Only a full envelope that materially changes ``body``
    is re-admission authority; the other native forms are valid durable history
    but grant no active-PR bypass.
    """
    if payload is None:
        return True
    value = _json_object(payload)
    if value is None:
        return False
    changed_fields = _canonical_specified_fields(value.get("changed_fields"))
    if changed_fields is None:
        return False
    keys = frozenset(value)
    if keys == {"changed_fields"}:
        return True
    if keys != {
        "changed_fields",
        "previous_status",
        "status",
        "block_recurrences_reset",
    }:
        return False
    return (
        type(value["previous_status"]) is str
        and value["previous_status"] in {"blocked", "triage"}
        and type(value["status"]) is str
        and value["status"] in {"ready", "todo"}
        and value["block_recurrences_reset"] is True
    )


def _durable_requeue_payload_is_canonical(kind: str, payload: Any) -> bool:
    """Accept native storage forms, including non-authorizing legacy forms."""
    if requeue_transition_is_authorized(kind, payload):
        return True
    if kind == "specified":
        return _native_specified_payload_is_canonical(payload)
    value = _json_object(payload)
    if kind == "status":
        return (
            value is not None
            and frozenset(value)
            == {"status", "reason", "parent", "previous_status"}
            and value["status"] == "todo"
            and value["reason"] == "ancestor_reopened"
            and type(value["parent"]) is str
            and bool(value["parent"].strip())
            and type(value["previous_status"]) is str
            and bool(value["previous_status"].strip())
        )
    if kind == "promoted":
        return (
            value is not None
            and frozenset(value) == {"status"}
            and value["status"] == "review"
        )
    if kind == "unblocked":
        return (
            value is not None
            and frozenset(value) == {"status", "resume_status"}
            and type(value["status"]) is str
            and value["status"] in {"ready", "todo", "review"}
            and type(value["resume_status"]) is str
            and value["resume_status"] in {"ready", "review"}
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
    prior_run_id = _positive_row_id(prior_run["id"])
    if prior_run_id is None:
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
    transition_id = _positive_row_id(transition["id"])
    if transition_id is None:
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
    if latest is None:
        return False
    latest_id = _positive_row_id(latest["id"])
    return latest_id is not None and latest_id == transition_id


def review_handoff_is_authorized(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return whether a review-lane task has exact native handoff provenance."""
    task = conn.execute(
        "SELECT status, assignee, claim_lock, current_run_id, "
        "consecutive_failures FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if task is None:
        return False
    if (
        task["status"] != "review"
        or task["claim_lock"] is not None
        or task["current_run_id"] is not None
        or type(task["consecutive_failures"]) is not int
        or task["consecutive_failures"] < 0
    ):
        return False
    reviewer = _canonical_profile_id(task["assignee"])
    if reviewer is None:
        return False

    implementation_run = conn.execute(
        "SELECT id, profile, status, outcome, started_at, ended_at "
        "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if implementation_run is None:
        return False
    implementation_run_id = _positive_row_id(implementation_run["id"])
    implementer = _canonical_profile_id(implementation_run["profile"])
    if implementation_run_id is None or implementer is None:
        return False
    if (
        implementation_run["outcome"] != "review_requested"
        or implementation_run["status"] not in {"review", "completed"}
        or not terminal_run_state_is_authorized(
            implementation_run["status"], implementation_run["outcome"]
        )
    ):
        return False

    now = int(time.time())
    started_at = validated_lifecycle_timestamp(
        implementation_run["started_at"], maximum=now
    )
    ended_at = validated_lifecycle_timestamp(
        implementation_run["ended_at"], maximum=now
    )
    if started_at is None or ended_at is None or ended_at < started_at:
        return False

    review_event = conn.execute(
        "SELECT id, run_id, payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = 'review_requested' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if review_event is None:
        return False
    review_event_id = _positive_row_id(review_event["id"])
    review_event_run_id = _positive_row_id(review_event["run_id"])
    payload = _review_requested_payload(review_event["payload"])
    created_at = validated_lifecycle_timestamp(
        review_event["created_at"], maximum=now
    )
    if (
        review_event_id is None
        or review_event_run_id != implementation_run_id
        or payload is None
        or payload["implementer"] != implementer
        or payload["reviewer"] != reviewer
        or created_at is None
        or created_at < ended_at
    ):
        return False

    latest = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? "
        "AND kind NOT IN (?, ?) ORDER BY id DESC LIMIT 1",
        (task_id, *_OBSERVATION_EVENT_KINDS),
    ).fetchone()
    return (
        latest is not None
        and _positive_row_id(latest["id"]) == review_event_id
    )


def review_rework_is_authorized(
    conn: sqlite3.Connection,
    task_id: str,
) -> bool:
    """Validate a reviewer-owned changes-requested handoff back to implementer."""
    row = conn.execute(
        "SELECT status, assignee, claim_lock, current_run_id, "
        "       consecutive_failures "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None or row["status"] != "ready":
        return False
    if row["claim_lock"] is not None or row["current_run_id"] is not None:
        return False
    failures = row["consecutive_failures"]
    if type(failures) is not int or failures < 0:
        return False
    implementer = _canonical_profile_id(row["assignee"])
    if implementer is None:
        return False

    prior_run = conn.execute(
        "SELECT id, profile, status, outcome, started_at, ended_at "
        "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if prior_run is None:
        return False
    prior_run_id = _positive_row_id(prior_run["id"])
    reviewer = _canonical_profile_id(prior_run["profile"])
    run_status = prior_run["status"]
    run_outcome = prior_run["outcome"]
    if (
        prior_run_id is None
        or reviewer is None
        or not terminal_run_state_is_authorized(run_status, run_outcome)
        or run_outcome != "changes_requested"
    ):
        return False

    change = conn.execute(
        "SELECT id, run_id, payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if change is None:
        return False
    change_id = _positive_row_id(change["id"])
    change_run_id = _positive_row_id(change["run_id"])
    payload = _changes_requested_payload(change["payload"])
    if (
        change_id is None
        or change_run_id != prior_run_id
        or payload is None
        or payload["implementer"] != implementer
        or payload["reviewer"] != reviewer
        or payload["status"] != run_status
    ):
        return False

    now = int(time.time())
    run_started_at = validated_lifecycle_timestamp(
        prior_run["started_at"], maximum=now
    )
    run_ended_at = validated_lifecycle_timestamp(
        prior_run["ended_at"], maximum=now
    )
    change_created_at = validated_lifecycle_timestamp(
        change["created_at"], maximum=now
    )
    if (
        run_started_at is None
        or run_ended_at is None
        or run_ended_at < run_started_at
        or change_created_at is None
        or change_created_at < run_ended_at
    ):
        return False

    implementation_run = conn.execute(
        "SELECT id, profile, status, outcome, ended_at FROM task_runs "
        "WHERE task_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
        (task_id, prior_run_id),
    ).fetchone()
    if implementation_run is None:
        return False
    implementation_run_id = _positive_row_id(implementation_run["id"])
    implementation_profile = _canonical_profile_id(
        implementation_run["profile"]
    )
    implementation_ended_at = validated_lifecycle_timestamp(
        implementation_run["ended_at"], maximum=run_started_at
    )
    if (
        implementation_run_id is None
        or implementation_profile != implementer
        or not terminal_run_state_is_authorized(
            implementation_run["status"], implementation_run["outcome"]
        )
        or implementation_run["outcome"] != "review_requested"
        or implementation_ended_at is None
    ):
        return False

    review_event = conn.execute(
        "SELECT id, run_id, payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = 'review_requested' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if review_event is None:
        return False
    review_event_id = _positive_row_id(review_event["id"])
    review_event_run_id = _positive_row_id(review_event["run_id"])
    review_payload = _review_requested_payload(review_event["payload"])
    review_created_at = validated_lifecycle_timestamp(
        review_event["created_at"], maximum=run_started_at
    )
    if (
        review_event_id is None
        or review_event_id >= change_id
        or review_event_run_id != implementation_run_id
        or review_payload is None
        or review_payload["implementer"] != implementer
        or review_payload["reviewer"] != reviewer
        or review_created_at is None
        or review_created_at < implementation_ended_at
    ):
        return False

    placeholders = ", ".join("?" for _ in _OBSERVATION_EVENT_KINDS)
    latest = conn.execute(
        "SELECT id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? "
        f"AND kind NOT IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (task_id, *_OBSERVATION_EVENT_KINDS),
    ).fetchone()
    if latest is None:
        return False
    latest_id = _positive_row_id(latest["id"])
    if latest_id is None:
        return False
    if run_status == "ready":
        return latest_id == change_id
    if (
        run_status != "todo"
        or latest_id <= change_id
        or latest["kind"] != "promoted"
        or latest["payload"] is not None
    ):
        return False
    promoted_at = validated_lifecycle_timestamp(
        latest["created_at"], maximum=now
    )
    return promoted_at is not None and promoted_at >= change_created_at


def dispatcher_state_rejections(
    conn: sqlite3.Connection,
) -> list[tuple[str, str]]:
    """Find non-terminal durable corruption before any dispatcher mutation.

    The dispatcher performs reclaim and promotion before its per-task respawn
    guard.  This board-wide read barrier keeps malformed SQLite storage from
    reaching permissive ``int(...)``/string operations in those native paths.
    A malformed task therefore defers the entire tick rather than partially
    mutating state and then admitting work from a corrupted history.
    """
    now = int(time.time())
    rows = conn.execute(
        "SELECT id, status, assignee, priority, created_at, started_at, "
        "claim_lock, claim_expires, worker_pid, last_heartbeat_at, "
        "current_run_id, consecutive_failures, block_recurrences, "
        "max_retries, max_runtime_seconds, last_failure_error, block_kind "
        "FROM tasks WHERE status IN "
        "('todo', 'ready', 'running', 'blocked', 'review')"
    ).fetchall()
    rejected: list[tuple[str, str]] = []

    for task in rows:
        raw_task_id = task["id"]
        task_id = raw_task_id if type(raw_task_id) is str else "<malformed>"
        malformed = False

        def reject() -> None:
            nonlocal malformed
            malformed = True

        if type(raw_task_id) is not str or not raw_task_id.strip():
            reject()
        if type(task["status"]) is not str:
            reject()
        assignee = task["assignee"]
        if assignee is not None and _canonical_profile_id(assignee) is None:
            reject()
        for field in ("priority", "consecutive_failures", "block_recurrences"):
            value = task[field]
            if type(value) is not int or (
                field != "priority" and value < 0
            ):
                reject()
        for field in ("created_at", "started_at", "last_heartbeat_at"):
            value = task[field]
            if value is not None and validated_lifecycle_timestamp(
                value, maximum=now
            ) is None:
                reject()
        claim_expires = task["claim_expires"]
        if claim_expires is not None and (
            type(claim_expires) is not int
            or claim_expires < _MIN_LIFECYCLE_TIMESTAMP
        ):
            reject()
        claim_lock = task["claim_lock"]
        if claim_lock is not None and (
            type(claim_lock) is not str or not claim_lock.strip()
        ):
            reject()
        worker_pid = task["worker_pid"]
        if worker_pid is not None and (
            type(worker_pid) is not int or worker_pid <= 0
        ):
            reject()
        current_run_id = task["current_run_id"]
        if current_run_id is not None and _positive_row_id(current_run_id) is None:
            reject()
        max_retries = task["max_retries"]
        if max_retries is not None and (
            type(max_retries) is not int or max_retries < 0
        ):
            reject()
        max_runtime = task["max_runtime_seconds"]
        if max_runtime is not None and (
            type(max_runtime) is not int or max_runtime <= 0
        ):
            reject()
        for field in ("last_failure_error", "block_kind"):
            value = task[field]
            if value is not None and type(value) is not str:
                reject()

        runs = conn.execute(
            "SELECT id, profile, status, outcome, summary, error, metadata, "
            "started_at, ended_at, claim_lock, claim_expires, worker_pid "
            "FROM task_runs WHERE task_id = ?",
            (raw_task_id,),
        ).fetchall()
        active_ids: list[int] = []
        seen_run_ids: set[int] = set()
        for run in runs:
            run_id = _positive_row_id(run["id"])
            if run_id is None or run_id in seen_run_ids:
                reject()
                continue
            seen_run_ids.add(run_id)
            profile = run["profile"]
            if profile is not None and _canonical_profile_id(profile) is None:
                reject()
            started_at = validated_lifecycle_timestamp(
                run["started_at"], maximum=now
            )
            if started_at is None:
                reject()
            status = run["status"]
            outcome = run["outcome"]
            ended_at = run["ended_at"]
            if status == "running" and outcome is None and ended_at is None:
                active_ids.append(run_id)
            else:
                terminal_at = validated_lifecycle_timestamp(
                    ended_at, maximum=now
                )
                if (
                    not terminal_run_state_is_authorized(status, outcome)
                    or terminal_at is None
                    or (started_at is not None and terminal_at < started_at)
                ):
                    reject()
            for field in ("summary", "error"):
                value = run[field]
                if value is not None and type(value) is not str:
                    reject()
            metadata = run["metadata"]
            if metadata is not None and _json_object(metadata) is None:
                reject()
            run_lock = run["claim_lock"]
            if run_lock is not None and (
                type(run_lock) is not str or not run_lock.strip()
            ):
                reject()
            run_expires = run["claim_expires"]
            if run_expires is not None and (
                type(run_expires) is not int
                or run_expires < _MIN_LIFECYCLE_TIMESTAMP
            ):
                reject()
            run_pid = run["worker_pid"]
            if run_pid is not None and (
                type(run_pid) is not int or run_pid <= 0
            ):
                reject()

        if len(active_ids) > 1:
            reject()
        if active_ids:
            if task["status"] != "running" or current_run_id != active_ids[0]:
                reject()
        elif current_run_id is not None:
            reject()
        if task["status"] != "running" and current_run_id is not None:
            reject()

        events = conn.execute(
            "SELECT id, run_id, kind, payload, created_at FROM task_events "
            "WHERE task_id = ?",
            (raw_task_id,),
        ).fetchall()
        seen_event_ids: set[int] = set()
        for event in events:
            event_id = _positive_row_id(event["id"])
            if event_id is None or event_id in seen_event_ids:
                reject()
            else:
                seen_event_ids.add(event_id)
            event_run_id = event["run_id"]
            if event_run_id is not None and _positive_row_id(event_run_id) is None:
                reject()
            kind = event["kind"]
            if type(kind) is not str or not kind.strip():
                reject()
            if validated_lifecycle_timestamp(
                event["created_at"], maximum=now
            ) is None:
                reject()
            payload = event["payload"]
            if payload is not None and _json_object(payload) is None:
                reject()

        latest_lifecycle_event = next(
            (
                event
                for event in reversed(events)
                if event["kind"] not in _OBSERVATION_EVENT_KINDS
            ),
            None,
        )
        if latest_lifecycle_event is not None:
            latest_kind = latest_lifecycle_event["kind"]
            latest_payload = latest_lifecycle_event["payload"]
            if (
                latest_kind in _REQUEUE_EVENT_KINDS
                and not _durable_requeue_payload_is_canonical(
                    latest_kind, latest_payload
                )
            ):
                reject()
            if (
                latest_kind == "review_requested"
                and _review_requested_payload(latest_payload) is None
            ):
                reject()

        comments = conn.execute(
            "SELECT id, body, created_at FROM task_comments WHERE task_id = ?",
            (raw_task_id,),
        ).fetchall()
        seen_comment_ids: set[int] = set()
        for comment in comments:
            comment_id = _positive_row_id(comment["id"])
            if comment_id is None or comment_id in seen_comment_ids:
                reject()
            else:
                seen_comment_ids.add(comment_id)
            if type(comment["body"]) is not str:
                reject()
            if validated_lifecycle_timestamp(
                comment["created_at"], maximum=now
            ) is None:
                reject()

        if malformed:
            rejected.append((task_id, "malformed_durable_state"))

    return rejected
