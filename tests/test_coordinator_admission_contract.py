from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOCAL_VARIANT = ROOT / "local-variant"
if str(LOCAL_VARIANT) not in sys.path:
    sys.path.insert(0, str(LOCAL_VARIANT))

from coordinator_admission_contract import build_coordinator_directive

ALLOWED_ACTIONS = (
    "probe_error",
    "coordinator_gate_suppressed",
    "fill_parallel_lanes",
    "reconcile_kanban_watchdog",
    "idle",
)
CANDIDATE_KEYS = ("ready", "backlog", "triage", "unmapped_active")


def _handoff(**overrides: object) -> dict[str, object]:
    handoff: dict[str, object] = {
        "status": "available",
        "available": True,
        "event_id": "heartbeat-20260911T120000Z-1",
        "age_seconds": 30,
        "action_required": True,
        "signals": ["no_durable_progress_since_previous_heartbeat"],
    }
    handoff.update(overrides)
    return handoff


def _directive(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "summary_action": "fill_parallel_lanes",
        "allowed_action_modes": ALLOWED_ACTIONS,
        "gate_allows_work": True,
        "probe_errors": [],
        "heartbeat_handoff": _handoff(),
        "max_handoff_age_seconds": 7200,
        "can_start_next": True,
        "candidate_counts": {
            "ready": 0,
            "backlog": 10,
            "triage": 3,
            "unmapped_active": 2,
        },
        "expected_candidate_keys": CANDIDATE_KEYS,
    }
    arguments.update(overrides)
    return build_coordinator_directive(**arguments)


def _legacy_overlay_action(summary_action: str, heartbeat_handoff: object) -> str:
    if (
        isinstance(heartbeat_handoff, dict)
        and heartbeat_handoff.get("available") is True
        and heartbeat_handoff.get("action_required") is True
        and "no_durable_progress_since_previous_heartbeat"
        in heartbeat_handoff.get("signals", [])
    ):
        return "reconcile_kanban_watchdog"
    return summary_action


def test_no_progress_wake_does_not_override_actionable_product_admission():
    heartbeat = _handoff()
    legacy_action = _legacy_overlay_action("fill_parallel_lanes", heartbeat)
    result = _directive(heartbeat_handoff=heartbeat)

    assert legacy_action == "reconcile_kanban_watchdog"
    assert result["action_mode"] == "fill_parallel_lanes"
    assert result["progress_contract"] == {
        "schema_version": 1,
        "handoff_disposition": "available",
        "no_progress_decision_required": True,
        "hard_disposition": "none",
        "watchdog_reconciliation_preempts_product_admission": False,
        "can_start_next": True,
        "candidate_projection_valid": True,
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


def test_genuine_watchdog_summary_remains_watchdog_reconciliation():
    result = _directive(summary_action="reconcile_kanban_watchdog")

    assert result["action_mode"] == "reconcile_kanban_watchdog"


def test_hard_dispositions_override_progress_and_prohibit_retry():
    probe = _directive(probe_errors=[{"source": "probe", "error": "read failed"}])
    assert probe["action_mode"] == "probe_error"
    assert probe["progress_contract"]["hard_disposition"] == "probe_error"
    assert probe["progress_contract"]["can_start_next"] is False
    assert probe["progress_contract"]["retry_unverified_noop_on_next_tick"] is False
    assert probe["progress_contract"]["required_outcome"] == (
        "surface_probe_error_and_hold_admission"
    )

    suppressed = _directive(gate_allows_work=False)
    assert suppressed["action_mode"] == "coordinator_gate_suppressed"
    assert suppressed["progress_contract"]["hard_disposition"] == (
        "coordinator_gate_suppressed"
    )
    assert suppressed["progress_contract"]["can_start_next"] is False
    assert suppressed["progress_contract"]["required_outcome"] == (
        "surface_gate_disposition_and_hold_admission"
    )


def test_malformed_booleans_and_error_shape_fail_closed():
    for arguments in (
        {"gate_allows_work": "false"},
        {"can_start_next": "false"},
        {"probe_errors": None},
        {"probe_errors": ["raw error"]},
    ):
        result = _directive(**arguments)
        assert result["action_mode"] == "probe_error"
        assert result["progress_contract"]["hard_disposition"] == (
            "input_contract_error"
        )
        assert result["progress_contract"]["can_start_next"] is False


def test_action_mode_is_typed_bounded_and_control_free():
    for action in (
        "x" * 65,
        "fill_parallel_lanes\nsecret",
        "fill_parallel_lanes\u202e",
        "secret_like_value",
    ):
        result = _directive(summary_action=action)
        assert result["action_mode"] == "probe_error"
        assert action not in json.dumps(result)

    result = _directive(allowed_action_modes=("fill_parallel_lanes", "idle"))
    assert result["action_mode"] == "probe_error"


def test_reserved_hard_modes_cannot_spoof_a_product_action():
    for action in ("probe_error", "coordinator_gate_suppressed"):
        result = _directive(summary_action=action)

        assert result["action_mode"] == "probe_error"
        assert result["progress_contract"]["hard_disposition"] == (
            "input_contract_error"
        )
        assert result["progress_contract"]["can_start_next"] is False
        assert result["progress_contract"]["no_progress_decision_required"] is False
        assert result["progress_contract"]["retry_unverified_noop_on_next_tick"] is (
            False
        )


class _ExplodingMapping(Mapping[object, object]):
    def __getitem__(self, key: object) -> object:
        raise AssertionError("candidate mapping item access must not occur")

    def __iter__(self):
        raise AssertionError("candidate mapping iteration must not occur")

    def __len__(self) -> int:
        raise AssertionError("candidate mapping length access must not occur")


class _UnhashableKeyMapping(Mapping[object, object]):
    def __getitem__(self, key: object) -> object:
        return 0

    def __iter__(self):
        return iter((["ready"],))

    def __len__(self) -> int:
        return 1


class _ExplodingDict(dict[Any, Any]):
    def __iter__(self):
        raise AssertionError("dict subclass iteration must not occur")

    def __len__(self) -> int:
        raise AssertionError("dict subclass length access must not occur")

    def get(self, key: object, default: object = None) -> object:
        raise AssertionError("dict subclass item access must not occur")


def test_candidate_schema_is_explicit_and_malformed_counts_fail_closed():
    invalid_counts = (
        {"ready": 0, "backlog": "1", "triage": 0, "unmapped_active": 0},
        {"ready": 0, "backlog": -1, "triage": 0, "unmapped_active": 0},
        {"ready": 0, "backlog": True, "triage": 0, "unmapped_active": 0},
        {"ready": 0, "backlog": 1, "triage": 0},
        {
            "ready": 0,
            "backlog": 1,
            "triage": 0,
            "unmapped_active": 0,
            "todo": 1,
        },
    )
    for counts in invalid_counts:
        result = _directive(candidate_counts=counts)
        assert result["action_mode"] == "probe_error"
        assert result["progress_contract"]["candidate_projection_valid"] is False
        assert result["progress_contract"]["candidate_counts"] == {}
        assert result["progress_contract"]["has_projected_candidates"] is False

    parameterized = _directive(
        candidate_counts={"todo": 2, "review": 1},
        expected_candidate_keys=("todo", "review"),
    )
    assert parameterized["action_mode"] == "fill_parallel_lanes"
    assert parameterized["progress_contract"]["candidate_counts"] == {
        "todo": 2,
        "review": 1,
    }


def test_candidate_mapping_ingress_is_bounded_and_fail_closed():
    oversized = {f"candidate_{index}": 0 for index in range(17)}
    hostile_inputs = (
        _ExplodingMapping(),
        _UnhashableKeyMapping(),
        _ExplodingDict(),
        oversized,
    )

    for counts in hostile_inputs:
        result = _directive(candidate_counts=counts)

        assert result["action_mode"] == "probe_error"
        assert result["progress_contract"]["hard_disposition"] == (
            "input_contract_error"
        )
        assert result["progress_contract"]["candidate_projection_valid"] is False
        assert result["progress_contract"]["candidate_counts"] == {}
        assert len(json.dumps(result)) < 2000


def test_handoff_requires_fresh_bounded_identity_and_signal_schema():
    malformed_handoffs = (
        _handoff(event_id="x" * 161),
        _handoff(age_seconds=-1),
        _handoff(action_required="true"),
        _handoff(signals=("no_durable_progress_since_previous_heartbeat",)),
        _handoff(signals=["ok"] * 17),
        _handoff(signals=["bad\nvalue"]),
        _handoff(status={"not": "a token"}, available=False),
        {"status": "available", "available": True},
    )
    for handoff in malformed_handoffs:
        result = _directive(heartbeat_handoff=handoff)
        assert result["action_mode"] == "probe_error"
        assert result["progress_contract"]["handoff_disposition"] == "malformed"
        assert result["progress_contract"]["hard_disposition"] == (
            "input_contract_error"
        )

    stale = _directive(heartbeat_handoff=_handoff(age_seconds=7201))
    assert stale["action_mode"] == "fill_parallel_lanes"
    assert stale["progress_contract"]["handoff_disposition"] == "stale"
    assert stale["progress_contract"]["no_progress_decision_required"] is False


def test_no_progress_without_projected_candidates_requires_complete_frontier_scan():
    result = _directive(
        summary_action="idle",
        can_start_next=False,
        candidate_counts={
            "ready": 0,
            "backlog": 0,
            "triage": 0,
            "unmapped_active": 0,
        },
    )

    assert result["progress_contract"]["required_outcome"] == (
        "complete_frontier_scan_then_transition_or_all_gated_proof"
    )
    assert result["progress_contract"]["retry_unverified_noop_on_next_tick"] is True


def test_non_actionable_handoff_does_not_invent_work():
    result = _directive(heartbeat_handoff=_handoff(action_required=False))

    assert result["progress_contract"]["no_progress_decision_required"] is False
    assert result["progress_contract"]["required_outcome"] == "follow_selected_gate"
    assert result["progress_contract"]["retry_unverified_noop_on_next_tick"] is False


def test_inputs_are_not_mutated_and_output_remains_bounded():
    handoff = _handoff()
    counts = {"ready": 0, "backlog": 10, "triage": 3, "unmapped_active": 2}
    original_handoff = deepcopy(handoff)
    original_counts = deepcopy(counts)

    result = _directive(heartbeat_handoff=handoff, candidate_counts=counts)

    assert handoff == original_handoff
    assert counts == original_counts
    assert len(json.dumps(result)) < 2000
