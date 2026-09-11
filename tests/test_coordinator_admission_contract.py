from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_VARIANT = ROOT / "local-variant"
if str(LOCAL_VARIANT) not in sys.path:
    sys.path.insert(0, str(LOCAL_VARIANT))

from coordinator_admission_contract import (
    build_progress_contract,
    no_progress_decision_required,
    resolve_action_mode,
)


def _handoff(*, action_required: bool = True) -> dict[str, object]:
    return {
        "available": True,
        "action_required": action_required,
        "signals": ["no_durable_progress_since_previous_heartbeat"],
    }


def test_no_progress_wake_does_not_override_actionable_product_admission():
    result = resolve_action_mode(
        "fill_parallel_lanes",
        gate_allows_work=True,
        probe_errors=[],
        heartbeat_handoff=_handoff(),
    )

    assert result == "fill_parallel_lanes"


def test_genuine_watchdog_summary_remains_watchdog_reconciliation():
    result = resolve_action_mode(
        "reconcile_kanban_watchdog",
        gate_allows_work=True,
        probe_errors=[],
        heartbeat_handoff=_handoff(),
    )

    assert result == "reconcile_kanban_watchdog"


def test_probe_and_gate_fail_closed_before_summary_action():
    assert (
        resolve_action_mode(
            "fill_parallel_lanes",
            gate_allows_work=True,
            probe_errors=[{"error": "read failed"}],
            heartbeat_handoff=_handoff(),
        )
        == "probe_error"
    )
    assert (
        resolve_action_mode(
            "fill_parallel_lanes",
            gate_allows_work=False,
            probe_errors=[],
            heartbeat_handoff=_handoff(),
        )
        == "coordinator_gate_suppressed"
    )


def test_no_progress_contract_requires_verified_transition_or_all_gated_proof():
    contract = build_progress_contract(
        _handoff(),
        action_mode="fill_parallel_lanes",
        can_start_next=True,
        candidate_counts={"ready": 0, "backlog": 10, "triage": 3, "unmapped_active": 2},
    )

    assert no_progress_decision_required(_handoff()) is True
    assert contract == {
        "no_progress_decision_required": True,
        "watchdog_reconciliation_preempts_product_admission": False,
        "action_mode": "fill_parallel_lanes",
        "can_start_next": True,
        "candidate_counts": {
            "ready": 0,
            "backlog": 10,
            "triage": 3,
            "unmapped_active": 2,
        },
        "has_projected_candidates": True,
        "required_outcome": "one_verified_transition_or_proof_every_candidate_is_gated",
        "retry_unverified_noop_on_next_tick": True,
    }


def test_no_progress_without_projected_candidates_still_requires_frontier_scan():
    contract = build_progress_contract(
        _handoff(),
        action_mode="idle",
        can_start_next=False,
        candidate_counts={},
    )

    assert contract["required_outcome"] == (
        "complete_frontier_scan_then_transition_or_all_gated_proof"
    )
    assert contract["retry_unverified_noop_on_next_tick"] is True


def test_non_actionable_handoff_does_not_invent_work():
    contract = build_progress_contract(
        _handoff(action_required=False),
        action_mode="idle",
        can_start_next=False,
        candidate_counts={"backlog": 4},
    )

    assert contract["no_progress_decision_required"] is False
    assert contract["required_outcome"] == "follow_selected_gate"
    assert contract["retry_unverified_noop_on_next_tick"] is False
