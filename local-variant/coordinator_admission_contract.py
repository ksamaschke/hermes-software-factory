"""Project-agnostic coordinator admission precedence.

A liveness/no-progress handoff wakes the decision owner.  It must not replace an
already-derived product action with watchdog reconciliation.  Genuine watchdog
work remains represented by the source summary's own action mode.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

NO_PROGRESS_SIGNAL = "no_durable_progress_since_previous_heartbeat"


def no_progress_decision_required(heartbeat_handoff: Mapping[str, Any] | None) -> bool:
    """Return whether a fresh handoff requires one bounded decision attempt."""
    if not isinstance(heartbeat_handoff, Mapping):
        return False
    signals = heartbeat_handoff.get("signals")
    return (
        heartbeat_handoff.get("available") is True
        and heartbeat_handoff.get("action_required") is True
        and isinstance(signals, list)
        and NO_PROGRESS_SIGNAL in signals
    )


def resolve_action_mode(
    summary_action: Any,
    *,
    gate_allows_work: bool,
    probe_errors: Sequence[Any],
    heartbeat_handoff: Mapping[str, Any] | None = None,
) -> str:
    """Preserve product selection while applying only hard safety precedence.

    ``heartbeat_handoff`` is accepted deliberately: callers must project the
    wake-up evidence, but it is context rather than a competing action source.
    """
    _ = heartbeat_handoff
    if probe_errors:
        return "probe_error"
    if not gate_allows_work:
        return "coordinator_gate_suppressed"
    if isinstance(summary_action, str) and summary_action.strip():
        return summary_action.strip()
    return "idle"


def build_progress_contract(
    heartbeat_handoff: Mapping[str, Any] | None,
    *,
    action_mode: str,
    can_start_next: bool,
    candidate_counts: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a bounded, non-selecting directive for an LLM coordinator."""
    decision_required = no_progress_decision_required(heartbeat_handoff)
    normalized_counts: dict[str, int] = {}
    for key in ("ready", "backlog", "triage", "unmapped_active"):
        value = candidate_counts.get(key, 0)
        normalized_counts[key] = (
            min(value, 1_000_000_000)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else 0
        )
    has_projected_candidates = any(normalized_counts.values())

    if decision_required and has_projected_candidates:
        required_outcome = "one_verified_transition_or_proof_every_candidate_is_gated"
    elif decision_required:
        required_outcome = "complete_frontier_scan_then_transition_or_all_gated_proof"
    else:
        required_outcome = "follow_selected_gate"

    return {
        "no_progress_decision_required": decision_required,
        "watchdog_reconciliation_preempts_product_admission": False,
        "action_mode": action_mode,
        "can_start_next": bool(can_start_next),
        "candidate_counts": normalized_counts,
        "has_projected_candidates": has_projected_candidates,
        "required_outcome": required_outcome,
        "retry_unverified_noop_on_next_tick": decision_required,
    }
