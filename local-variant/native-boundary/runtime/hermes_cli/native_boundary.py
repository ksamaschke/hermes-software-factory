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
# These transition producers deliberately call ``_append_event`` without a
# run id. A non-NULL value is forged provenance, even if it names a real
# historical run for the same task.
_RUNLESS_REQUEUE_EVENT_KINDS = frozenset(
    {"status", "promoted", "unblocked", "specified"}
)
_OBSERVATION_EVENT_KINDS = (
    "commented",
    "descendant_invalidation_recorded",
    "respawn_guarded",
)
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
    """Return a bounded positive SQLite integer timestamp, else fail closed.

    Real wall-clock validation retains the plausible epoch floor.  A caller
    supplying a bounded synthetic clock below that floor (as native deterministic
    tests do) establishes a separate positive monotonic domain; values from that
    domain remain bounded by the supplied maximum and cannot become live-board
    authority under the real clock.
    """
    if type(value) is not int or value <= 0:
        return None
    if maximum is None:
        return value if value >= _MIN_LIFECYCLE_TIMESTAMP else None
    if type(maximum) is not int or maximum <= 0 or value > maximum:
        return None
    if maximum >= _MIN_LIFECYCLE_TIMESTAMP and value < _MIN_LIFECYCLE_TIMESTAMP:
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


def _ancestor_reopened_status_payload(payload: Any) -> dict[str, Any] | None:
    """Validate the legacy status event emitted for descendant invalidation."""
    value = _json_object(payload)
    if value is None or frozenset(value) != {
        "status",
        "reason",
        "parent",
        "previous_status",
        "resume_status",
    }:
        return None
    previous_status = value["previous_status"]
    resume_status = value["resume_status"]
    if (
        value["status"] != "todo"
        or value["reason"] != "ancestor_reopened"
        or type(value["parent"]) is not str
        or not value["parent"].strip()
        or type(previous_status) is not str
        or previous_status not in {"ready", "review", "running", "done"}
        or type(resume_status) is not str
        or resume_status not in {"ready", "review"}
    ):
        return None
    if previous_status == "review" and resume_status != "review":
        return None
    if previous_status in {"ready", "done"} and resume_status != "ready":
        return None
    return value


def _descendant_invalidated_payload(payload: Any) -> dict[str, Any] | None:
    """Validate the native companion event emitted for ancestor reopening."""
    value = _json_object(payload)
    if value is None or frozenset(value) != {
        "ancestor",
        "prior_status",
        "new_status",
        "resume_status",
    }:
        return None
    prior_status = value["prior_status"]
    resume_status = value["resume_status"]
    if (
        value["new_status"] != "todo"
        or type(value["ancestor"]) is not str
        or not value["ancestor"].strip()
        or type(prior_status) is not str
        or prior_status not in {"ready", "review", "running", "done"}
        or type(resume_status) is not str
        or resume_status not in {"ready", "review"}
    ):
        return None
    if prior_status == "review" and resume_status != "review":
        return None
    if prior_status in {"ready", "done"} and resume_status != "ready":
        return None
    return value


def _descendant_invalidation_marker_payload(
    payload: Any,
) -> dict[str, Any] | None:
    """Validate redundant parent-side descendant invalidation evidence."""
    value = _json_object(payload)
    if value is None or frozenset(value) != {
        "descendant",
        "prior_status",
        "new_status",
        "resume_status",
    }:
        return None
    descendant = value["descendant"]
    prior_status = value["prior_status"]
    resume_status = value["resume_status"]
    if (
        type(descendant) is not str
        or not descendant.strip()
        or value["new_status"] != "todo"
        or type(prior_status) is not str
        or prior_status not in {"ready", "review", "running", "done"}
        or type(resume_status) is not str
        or resume_status not in {"ready", "review"}
    ):
        return None
    if prior_status == "review" and resume_status != "review":
        return None
    if prior_status in {"ready", "done"} and resume_status != "ready":
        return None
    return value


def _durable_requeue_payload_is_canonical(kind: str, payload: Any) -> bool:
    """Accept native storage forms, including non-authorizing legacy forms."""
    if requeue_transition_is_authorized(kind, payload):
        return True
    if kind == "specified":
        return _native_specified_payload_is_canonical(payload)
    value = _json_object(payload)
    if kind == "status":
        return _ancestor_reopened_status_payload(payload) is not None
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


def requeue_transition_requires_null_run_id(kind: Any, payload: Any) -> bool:
    """Return whether the native producer emits this transition without a run."""
    if kind in {"promoted", "unblocked", "specified"}:
        return True
    return kind == "status" and requeue_transition_is_authorized(kind, payload)


def requeue_transition_provenance_is_authorized(
    kind: Any,
    payload: Any,
    run_id: Any,
) -> bool:
    """Validate payload plus the native run/runless producer contract."""
    if not requeue_transition_is_authorized(kind, payload):
        return False
    if requeue_transition_requires_null_run_id(kind, payload):
        return run_id is None
    return _positive_row_id(run_id) is not None


def requeue_transition_event_provenance_is_authorized(
    conn: sqlite3.Connection,
    task_id: Any,
    kind: Any,
    payload: Any,
    run_id: Any,
) -> bool:
    """Validate a requeue event's producer contract and same-task run link."""
    if type(task_id) is not str or not task_id.strip():
        return False
    if not requeue_transition_provenance_is_authorized(kind, payload, run_id):
        return False
    if run_id is None:
        return True
    canonical_run_id = _positive_row_id(run_id)
    run = conn.execute(
        "SELECT task_id FROM task_runs WHERE id = ?",
        (canonical_run_id,),
    ).fetchone()
    return (
        run is not None
        and type(run["task_id"]) is str
        and run["task_id"] == task_id
    )


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
        "SELECT id, run_id, kind, payload, created_at FROM task_events "
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
    transition_kind = transition["kind"]
    transition_run_id = transition["run_id"]
    if (
        transition_kind in _RUNLESS_REQUEUE_EVENT_KINDS
        and transition_run_id is not None
    ):
        return False
    if (
        transition_kind == "reclaimed"
        and _positive_row_id(transition_run_id) != prior_run_id
    ):
        return False
    if not requeue_transition_is_authorized(
        transition_kind, transition["payload"]
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


def _later_descendant_invalidation_marker_exists(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    review_event_id: int,
    review_created_at: int,
) -> bool:
    """Detect parent-side evidence that consumed an older review handoff."""
    now = int(time.time())
    rows = conn.execute(
        "SELECT e.id, e.run_id, e.payload, e.created_at FROM task_events e "
        "JOIN task_links l ON l.parent_id = e.task_id "
        "WHERE l.child_id = ? "
        "AND e.kind = 'descendant_invalidation_recorded' ORDER BY e.id ASC",
        (task_id,),
    ).fetchall()
    for row in rows:
        marker_id = _positive_row_id(row["id"])
        marker_created_at = validated_lifecycle_timestamp(
            row["created_at"], maximum=now
        )
        later_than_handoff = marker_id is None or marker_id > review_event_id
        if (
            not later_than_handoff
            and marker_created_at is not None
            and marker_created_at > review_created_at
            and marker_id is not None
            and marker_id >= review_event_id
        ):
            later_than_handoff = True
        if not later_than_handoff:
            continue

        # A marker on a direct parent is durable evidence that some descendant
        # invalidation happened after this handoff.  If its payload cannot be
        # parsed and validated we cannot safely prove that it belonged to a
        # different sibling, so the old handoff must be consumed fail-closed.
        payload = _descendant_invalidation_marker_payload(row["payload"])
        if (
            payload is None
            or row["run_id"] is not None
            or marker_id is None
            or marker_created_at is None
        ):
            return True
        if payload["descendant"] == task_id:
            return True
    return False


def _review_handoff_after_ancestor_reopen_is_authorized(
    conn: sqlite3.Connection,
    task_id: str,
    reviewer: str,
) -> bool:
    """Validate the exact native suffix that resumes a review descendant."""
    placeholders = ", ".join("?" for _ in _OBSERVATION_EVENT_KINDS)
    suffix = conn.execute(
        "SELECT id, run_id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? "
        f"AND kind NOT IN ({placeholders}) ORDER BY id DESC LIMIT 3",
        (task_id, *_OBSERVATION_EVENT_KINDS),
    ).fetchall()
    if len(suffix) != 3:
        return False
    promoted, status_event, invalidated = suffix
    if (
        promoted["kind"] != "promoted"
        or status_event["kind"] != "status"
        or invalidated["kind"] != "descendant_invalidated"
    ):
        return False
    promoted_id = _positive_row_id(promoted["id"])
    status_id = _positive_row_id(status_event["id"])
    invalidated_id = _positive_row_id(invalidated["id"])
    promoted_payload = _json_object(promoted["payload"])
    status_payload = _ancestor_reopened_status_payload(status_event["payload"])
    invalidated_payload = _descendant_invalidated_payload(invalidated["payload"])
    if (
        promoted_id is None
        or status_id is None
        or invalidated_id is None
        or not (invalidated_id < status_id < promoted_id)
        or promoted["run_id"] is not None
        or promoted_payload != {"status": "review"}
        or status_payload is None
        or invalidated_payload is None
        or status_payload["resume_status"] != "review"
        or invalidated_payload["resume_status"] != "review"
        or status_payload["parent"] != invalidated_payload["ancestor"]
        or status_payload["previous_status"] != invalidated_payload["prior_status"]
    ):
        return False

    parent = conn.execute(
        "SELECT p.status FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND l.parent_id = ?",
        (task_id, status_payload["parent"]),
    ).fetchone()
    if parent is None or parent["status"] not in {"done", "archived"}:
        return False

    now = int(time.time())
    invalidated_at = validated_lifecycle_timestamp(
        invalidated["created_at"], maximum=now
    )
    status_at = validated_lifecycle_timestamp(status_event["created_at"], maximum=now)
    promoted_at = validated_lifecycle_timestamp(promoted["created_at"], maximum=now)
    if (
        invalidated_at is None
        or status_at is None
        or promoted_at is None
        or not (invalidated_at <= status_at <= promoted_at)
    ):
        return False

    latest_run = conn.execute(
        "SELECT id, profile, status, outcome, started_at, ended_at "
        "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest_run is None:
        return False
    previous_status = status_payload["previous_status"]
    if previous_status == "review":
        if status_event["run_id"] is not None or invalidated["run_id"] is not None:
            return False
        implementation_run = latest_run
    elif previous_status == "running":
        reviewer_run_id = _positive_row_id(status_event["run_id"])
        if (
            reviewer_run_id is None
            or _positive_row_id(invalidated["run_id"]) != reviewer_run_id
            or _positive_row_id(latest_run["id"]) != reviewer_run_id
            or _canonical_profile_id(latest_run["profile"]) != reviewer
            or latest_run["outcome"] != "reclaimed"
            or not terminal_run_state_is_authorized(
                latest_run["status"], latest_run["outcome"]
            )
        ):
            return False
        reviewer_started_at = validated_lifecycle_timestamp(
            latest_run["started_at"], maximum=invalidated_at
        )
        reviewer_ended_at = validated_lifecycle_timestamp(
            latest_run["ended_at"], maximum=invalidated_at
        )
        if (
            reviewer_started_at is None
            or reviewer_ended_at is None
            or reviewer_ended_at < reviewer_started_at
        ):
            return False
        implementation_run = conn.execute(
            "SELECT id, profile, status, outcome, started_at, ended_at "
            "FROM task_runs WHERE task_id = ? AND id < ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id, reviewer_run_id),
        ).fetchone()
        if implementation_run is None:
            return False
    else:
        return False

    implementation_run_id = _positive_row_id(implementation_run["id"])
    implementer = _canonical_profile_id(implementation_run["profile"])
    implementation_started_at = validated_lifecycle_timestamp(
        implementation_run["started_at"], maximum=now
    )
    implementation_ended_at = validated_lifecycle_timestamp(
        implementation_run["ended_at"], maximum=now
    )
    if (
        implementation_run_id is None
        or implementer is None
        or implementation_run["outcome"] != "review_requested"
        or implementation_run["status"] not in {"review", "completed"}
        or not terminal_run_state_is_authorized(
            implementation_run["status"], implementation_run["outcome"]
        )
        or implementation_started_at is None
        or implementation_ended_at is None
        or implementation_ended_at < implementation_started_at
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
    review_payload = _review_requested_payload(review_event["payload"])
    review_created_at = validated_lifecycle_timestamp(
        review_event["created_at"], maximum=invalidated_at
    )
    if (
        review_event_id is None
        or review_event_id >= invalidated_id
        or _positive_row_id(review_event["run_id"]) != implementation_run_id
        or review_payload is None
        or review_payload["implementer"] != implementer
        or review_payload["reviewer"] != reviewer
        or review_created_at is None
        or review_created_at < implementation_ended_at
    ):
        return False
    if previous_status == "running":
        reviewer_started_at = validated_lifecycle_timestamp(
            latest_run["started_at"], maximum=invalidated_at
        )
        if reviewer_started_at is None or reviewer_started_at < review_created_at:
            return False
    return True


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
    if _review_handoff_after_ancestor_reopen_is_authorized(
        conn, task_id, reviewer
    ):
        return True

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
    if _later_descendant_invalidation_marker_exists(
        conn,
        task_id,
        review_event_id=review_event_id,
        review_created_at=created_at,
    ):
        return False

    placeholders = ", ".join("?" for _ in _OBSERVATION_EVENT_KINDS)
    latest = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? "
        f"AND kind NOT IN ({placeholders}) ORDER BY id DESC LIMIT 1",
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
) -> list[tuple[Any, str]]:
    """Find non-terminal durable corruption before any dispatcher mutation.

    The dispatcher performs reclaim and promotion before its per-task respawn
    guard. This read barrier keeps malformed SQLite storage from reaching
    permissive ``int(...)``/string operations in those native paths. Returned
    identities remain the raw SQLite values so callers can quarantine exactly
    the offending task while allowing unrelated canonical work to continue.
    """
    now = int(time.time())
    rows = conn.execute(
        "SELECT id, CAST(id AS BLOB) AS storage_identity, "
        "status, assignee, priority, created_at, started_at, "
        "claim_lock, claim_expires, worker_pid, last_heartbeat_at, "
        "current_run_id, consecutive_failures, block_recurrences, "
        "max_retries, max_runtime_seconds, last_failure_error, block_kind "
        "FROM tasks WHERE status IN "
        "('todo', 'ready', 'running', 'blocked', 'review')"
    ).fetchall()
    rejected: list[tuple[Any, str]] = []

    task_ids_by_storage_identity: dict[Any, list[Any]] = {}
    for task in rows:
        task_ids_by_storage_identity.setdefault(task["storage_identity"], []).append(
            task["id"]
        )
    malformed_child_task_ids: set[Any] = set()
    for table in ("task_runs", "task_events", "task_comments"):
        for child in conn.execute(
            f"SELECT CAST(task_id AS BLOB) AS storage_identity FROM {table} "
            "WHERE typeof(task_id) != 'text'"
        ).fetchall():
            malformed_child_task_ids.update(
                task_ids_by_storage_identity.get(child["storage_identity"], ())
            )

    for task in rows:
        raw_task_id = task["id"]
        malformed = raw_task_id in malformed_child_task_ids

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
            "SELECT task_id, id, profile, status, outcome, summary, error, metadata, "
            "started_at, ended_at, claim_lock, claim_expires, worker_pid "
            "FROM task_runs WHERE task_id = ?",
            (raw_task_id,),
        ).fetchall()
        active_runs: list[Any] = []
        seen_run_ids: set[int] = set()
        for run in runs:
            if type(run["task_id"]) is not str or run["task_id"] != raw_task_id:
                reject()
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
                active_runs.append(run)
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

        if len(active_runs) > 1:
            reject()
        if active_runs:
            active_run = active_runs[0]
            active_run_id = _positive_row_id(active_run["id"])
            if (
                task["status"] != "running"
                or current_run_id != active_run_id
                or task["claim_lock"] is None
                or task["claim_expires"] is None
                or task["claim_lock"] != active_run["claim_lock"]
                or task["claim_expires"] != active_run["claim_expires"]
                or task["worker_pid"] != active_run["worker_pid"]
            ):
                reject()
        elif current_run_id is not None:
            reject()
        if task["status"] != "running" and current_run_id is not None:
            reject()

        events = conn.execute(
            "SELECT task_id, id, run_id, kind, payload, created_at "
            "FROM task_events WHERE task_id = ? ORDER BY id ASC",
            (raw_task_id,),
        ).fetchall()
        seen_event_ids: set[int] = set()
        for event in events:
            if type(event["task_id"]) is not str or event["task_id"] != raw_task_id:
                reject()
            event_id = _positive_row_id(event["id"])
            if event_id is None or event_id in seen_event_ids:
                reject()
            else:
                seen_event_ids.add(event_id)
            event_run_id = event["run_id"]
            kind = event["kind"]
            if type(kind) is not str or not kind.strip():
                reject()
            if event_run_id is not None:
                canonical_event_run_id = _positive_row_id(event_run_id)
                if (
                    canonical_event_run_id is None
                    or canonical_event_run_id not in seen_run_ids
                ):
                    reject()
            payload = event["payload"]
            if payload is not None and _json_object(payload) is None:
                reject()
            if (
                requeue_transition_requires_null_run_id(kind, payload)
                and event_run_id is not None
            ):
                reject()
            if (
                requeue_transition_is_authorized(kind, payload)
                and not requeue_transition_event_provenance_is_authorized(
                    conn,
                    raw_task_id,
                    kind,
                    payload,
                    event_run_id,
                )
            ):
                reject()
            ancestor_reopened = (
                _ancestor_reopened_status_payload(payload)
                if kind == "status"
                else None
            )
            if ancestor_reopened is not None and (
                (ancestor_reopened["previous_status"] == "running")
                is not (event_run_id is not None)
            ):
                reject()
            if validated_lifecycle_timestamp(
                event["created_at"], maximum=now
            ) is None:
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
            "SELECT task_id, id, body, created_at FROM task_comments "
            "WHERE task_id = ?",
            (raw_task_id,),
        ).fetchall()
        seen_comment_ids: set[int] = set()
        for comment in comments:
            if (
                type(comment["task_id"]) is not str
                or comment["task_id"] != raw_task_id
            ):
                reject()
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
            rejected.append((raw_task_id, "malformed_durable_state"))

    return rejected
