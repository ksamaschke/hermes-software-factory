"""Project-agnostic coordinator admission precedence.

A liveness/no-progress handoff wakes the decision owner. It must not replace an
already-derived product action with watchdog reconciliation. Genuine watchdog
work remains represented by the source summary's own typed action mode.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

NO_PROGRESS_SIGNAL = "no_durable_progress_since_previous_heartbeat"
MAX_ACTION_MODES = 64
MAX_ACTION_TOKEN_LENGTH = 64
MAX_CANDIDATE_KEYS = 16
MAX_COUNT = 1_000_000_000
MAX_EVENT_ID_LENGTH = 160
MAX_PROBE_ERRORS = 32
MAX_SIGNALS = 16
MAX_SIGNAL_LENGTH = 120
_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")
_HANDOFF_TOKEN = re.compile(r"^[A-Za-z0-9_.:+-]+$")
_UNAVAILABLE_HANDOFF_STATES = {"invalid", "missing", "stale", "unavailable"}


def _is_token(value: Any, *, limit: int, pattern: re.Pattern[str]) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and pattern.fullmatch(value) is not None
    )


def _validate_token_sequence(
    values: Any,
    *,
    maximum_items: int,
    maximum_token_length: int,
    pattern: re.Pattern[str],
) -> tuple[str, ...] | None:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= maximum_items:
        return None
    normalized = tuple(values)
    if not all(
        _is_token(value, limit=maximum_token_length, pattern=pattern)
        for value in normalized
    ):
        return None
    if len(set(normalized)) != len(normalized):
        return None
    return normalized


def _classify_handoff(
    heartbeat_handoff: Mapping[str, Any] | None,
    *,
    max_handoff_age_seconds: int,
) -> tuple[str, bool]:
    if heartbeat_handoff is None:
        return "unavailable", False
    if not isinstance(heartbeat_handoff, Mapping):
        return "malformed", False

    available = heartbeat_handoff.get("available")
    status = heartbeat_handoff.get("status")
    if (
        available is False
        and isinstance(status, str)
        and status in _UNAVAILABLE_HANDOFF_STATES
    ):
        return str(status), False
    if available is not True or status != "available":
        return "malformed", False

    event_id = heartbeat_handoff.get("event_id")
    age_seconds = heartbeat_handoff.get("age_seconds")
    action_required = heartbeat_handoff.get("action_required")
    signals = heartbeat_handoff.get("signals")
    if not _is_token(event_id, limit=MAX_EVENT_ID_LENGTH, pattern=_HANDOFF_TOKEN):
        return "malformed", False
    if (
        not isinstance(age_seconds, int)
        or isinstance(age_seconds, bool)
        or age_seconds < 0
    ):
        return "malformed", False
    if age_seconds > max_handoff_age_seconds:
        return "stale", False
    if not isinstance(action_required, bool):
        return "malformed", False
    if not isinstance(signals, list) or len(signals) > MAX_SIGNALS:
        return "malformed", False
    if not all(
        _is_token(signal, limit=MAX_SIGNAL_LENGTH, pattern=_HANDOFF_TOKEN)
        for signal in signals
    ):
        return "malformed", False
    return "available", action_required and NO_PROGRESS_SIGNAL in signals


def _normalize_candidate_counts(
    candidate_counts: Any,
    expected_candidate_keys: Any,
) -> tuple[dict[str, int], bool]:
    keys = _validate_token_sequence(
        expected_candidate_keys,
        maximum_items=MAX_CANDIDATE_KEYS,
        maximum_token_length=MAX_ACTION_TOKEN_LENGTH,
        pattern=_TOKEN,
    )
    if keys is None or not isinstance(candidate_counts, Mapping):
        return {}, False
    if set(candidate_counts) != set(keys):
        return {}, False

    normalized: dict[str, int] = {}
    for key in keys:
        value = candidate_counts.get(key)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > MAX_COUNT
        ):
            return {}, False
        normalized[key] = value
    return normalized, True


def build_coordinator_directive(
    *,
    summary_action: Any,
    allowed_action_modes: Sequence[str],
    gate_allows_work: Any,
    probe_errors: Any,
    heartbeat_handoff: Mapping[str, Any] | None,
    max_handoff_age_seconds: Any,
    can_start_next: Any,
    candidate_counts: Any,
    expected_candidate_keys: Sequence[str],
) -> dict[str, Any]:
    """Resolve hard precedence and emit one bounded, non-selecting directive."""
    allowed = _validate_token_sequence(
        allowed_action_modes,
        maximum_items=MAX_ACTION_MODES,
        maximum_token_length=MAX_ACTION_TOKEN_LENGTH,
        pattern=_TOKEN,
    )
    expected_keys = _validate_token_sequence(
        expected_candidate_keys,
        maximum_items=MAX_CANDIDATE_KEYS,
        maximum_token_length=MAX_ACTION_TOKEN_LENGTH,
        pattern=_TOKEN,
    )
    counts, counts_valid = _normalize_candidate_counts(
        candidate_counts, expected_candidate_keys
    )
    booleans_valid = isinstance(gate_allows_work, bool) and isinstance(
        can_start_next, bool
    )
    errors_valid = (
        isinstance(probe_errors, list)
        and len(probe_errors) <= MAX_PROBE_ERRORS
        and all(isinstance(error, Mapping) for error in probe_errors)
    )
    age_limit_valid = (
        isinstance(max_handoff_age_seconds, int)
        and not isinstance(max_handoff_age_seconds, bool)
        and 0 < max_handoff_age_seconds <= 86_400
    )
    action_valid = (
        allowed is not None
        and _is_token(summary_action, limit=MAX_ACTION_TOKEN_LENGTH, pattern=_TOKEN)
        and summary_action in allowed
        and {"probe_error", "coordinator_gate_suppressed"}.issubset(allowed)
    )
    handoff_disposition, decision_required = (
        _classify_handoff(
            heartbeat_handoff,
            max_handoff_age_seconds=max_handoff_age_seconds,
        )
        if age_limit_valid
        else ("malformed", False)
    )
    input_valid = all(
        (
            allowed is not None,
            expected_keys is not None,
            counts_valid,
            booleans_valid,
            errors_valid,
            age_limit_valid,
            action_valid,
            handoff_disposition != "malformed",
        )
    )

    if not input_valid:
        action_mode = "probe_error"
        hard_disposition = "input_contract_error"
        required_outcome = "surface_input_contract_error_and_hold_admission"
    elif probe_errors:
        action_mode = "probe_error"
        hard_disposition = "probe_error"
        required_outcome = "surface_probe_error_and_hold_admission"
    elif gate_allows_work is False:
        action_mode = "coordinator_gate_suppressed"
        hard_disposition = "coordinator_gate_suppressed"
        required_outcome = "surface_gate_disposition_and_hold_admission"
    else:
        action_mode = summary_action
        hard_disposition = "none"
        if decision_required and any(counts.values()):
            required_outcome = (
                "one_verified_transition_or_proof_every_candidate_is_gated"
            )
        elif decision_required:
            required_outcome = (
                "complete_frontier_scan_then_transition_or_all_gated_proof"
            )
        else:
            required_outcome = "follow_selected_gate"

    admission_allowed = input_valid and not probe_errors and gate_allows_work is True
    return {
        "action_mode": action_mode,
        "progress_contract": {
            "schema_version": 1,
            "handoff_disposition": handoff_disposition,
            "no_progress_decision_required": bool(
                decision_required and admission_allowed
            ),
            "hard_disposition": hard_disposition,
            "watchdog_reconciliation_preempts_product_admission": False,
            "can_start_next": bool(can_start_next is True and admission_allowed),
            "candidate_projection_valid": counts_valid,
            "candidate_counts": counts if counts_valid else {},
            "has_projected_candidates": bool(counts_valid and any(counts.values())),
            "required_outcome": required_outcome,
            "retry_unverified_noop_on_next_tick": bool(
                decision_required and admission_allowed
            ),
        },
    }
