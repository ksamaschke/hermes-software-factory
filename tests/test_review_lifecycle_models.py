import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "review_lifecycle_contract.py"
spec = importlib.util.spec_from_file_location("review_lifecycle_contract", SCRIPT)
assert spec is not None and spec.loader is not None
contract = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contract
spec.loader.exec_module(contract)

REVIEWER = ROOT / "skills" / "kanban-reviewer-contract" / "SKILL.md"
ORCHESTRATION = (
    ROOT
    / "local-variant"
    / "skills"
    / "kanban-review-orchestration"
    / "SKILL.md"
)
ORCHESTRATOR_SOUL = ROOT / "docs" / "orchestrator-soul-template.md"


def _compact(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _evidence(**overrides):
    values = {
        "task_id": "t_review",
        "implementation_task": "t_impl",
        "run_id": "42",
        "latest_run_id": "42",
        "current_run_id": None,
        "reviewer_identity": "reviewer-a",
        "expected_reviewer_identity": "reviewer-a",
        "source_status": "ready",
        "review_requested_identity": None,
        "verdict": "CHANGES_REQUESTED",
        "candidate": "candidate-sha",
        "base": "base-sha",
        "scope_manifest": ("a.py:10-20",),
        "review_round": "3",
        "finding_identity": "finding-1",
        "child_ids": (),
        "rejection": None,
    }
    values.update(overrides)
    return contract.ReviewEvidence(**values)


def _with_handoff(evidence):
    return replace(
        evidence,
        review_requested_identity=contract.handoff_identity(evidence),
    )


def _with_native_rejection(evidence):
    rejection = contract.TransitionRejection(
        task_id=evidence.task_id,
        run_id=evidence.run_id,
        reviewer_identity=evidence.reviewer_identity,
        action="kanban_request_changes",
        code=contract.LIFECYCLE_REJECTION_CODE,
        source_status="ready",
        identity=contract.rejection_identity(evidence),
    )
    return replace(evidence, rejection=rejection)


def _scheduled_sets(plan):
    created = {plan.successor_key} if plan.successor_count else set()
    spawned = set() if plan.redispatch_leaf else set(created)
    return created, spawned


def test_same_card_changes_requested_uses_only_native_rework():
    evidence = _with_handoff(
        _evidence(source_status="review", verdict="CHANGES_REQUESTED")
    )

    plan = contract.plan_review(evidence)

    assert plan.lifecycle == "same-card"
    assert plan.worker_action == "kanban_request_changes"
    assert plan.orchestrator_action == "native_rework_only"
    assert plan.successor_count == 0
    assert plan.successor_key is None
    assert plan.release_allowed is False
    assert _scheduled_sets(plan) == (set(), set())


def test_standalone_changes_requested_blocks_once_and_routes_one_successor():
    evidence = _evidence()

    first = contract.plan_review(evidence)
    second = contract.plan_review(evidence)

    assert first.lifecycle == "standalone"
    assert first.worker_action == "kanban_block"
    assert first.orchestrator_action == "route_remediation_then_archive"
    assert first.successor_kind == "implementation-remediation"
    assert first.successor_count == 1
    assert first.successor_key == second.successor_key
    assert first.release_allowed is False
    assert first.archive_leaf_after_successor is True
    assert first.redispatch_leaf is False
    created, spawned = _scheduled_sets(first)
    assert created == {first.successor_key}
    assert spawned == created


def test_legacy_ready_request_changes_rejection_reuses_same_plan_without_review_retry():
    evidence = _with_native_rejection(_evidence())

    plan = contract.plan_review(evidence)

    assert plan.worker_action == "kanban_block"
    assert plan.orchestrator_action == "route_remediation_then_archive"
    assert plan.redispatch_leaf is False
    assert plan.successor_key == contract.successor_key(
        evidence, "implementation-remediation"
    )


def test_standalone_incomplete_with_children_requires_frontier_replacement():
    evidence = _evidence(
        verdict="REVIEW-INCOMPLETE",
        finding_identity="provider-timeout",
        child_ids=("t_fanin",),
    )

    plan = contract.plan_review(evidence)

    assert plan.worker_action == "kanban_block"
    assert plan.orchestrator_action == "route_continuation_then_archive"
    assert plan.successor_kind == "review-continuation"
    assert plan.release_allowed is False
    assert plan.replace_downstream_frontier is True
    assert plan.successor_count == 1


def test_only_current_approved_standalone_leaf_can_release_downstream():
    evidence = _evidence(verdict="APPROVED", finding_identity="none")

    plan = contract.plan_review(evidence)

    assert plan.worker_action == "kanban_complete"
    assert plan.release_allowed is True
    assert plan.successor_count == 0


@pytest.mark.parametrize(
    "evidence, message",
    [
        (
            _evidence(latest_run_id="43", verdict="APPROVED", finding_identity="none"),
            "not the latest run",
        ),
        (
            _evidence(current_run_id="43"),
            "newer or foreign run",
        ),
        (
            _evidence(reviewer_identity="reviewer-b"),
            "reviewer identity",
        ),
        (
            _evidence(source_status="review"),
            "lacks the exact review_requested handoff",
        ),
    ],
)
def test_stale_foreign_or_missing_provenance_fails_closed(evidence, message):
    with pytest.raises(contract.ContractViolation, match=message):
        contract.plan_review(evidence)


def test_forged_or_foreign_transition_rejection_fails_closed():
    evidence = _with_native_rejection(_evidence())
    forged = replace(
        evidence,
        rejection=replace(evidence.rejection, run_id="foreign-run"),
    )

    with pytest.raises(contract.ContractViolation, match="stale, foreign, or ambiguous"):
        contract.plan_review(forged)


def test_successor_identity_changes_with_review_round_even_on_same_head():
    first = contract.plan_review(_evidence(review_round="3"))
    second = contract.plan_review(_evidence(review_round="4"))

    assert first.successor_key != second.successor_key


def test_reviewer_contract_separates_same_card_and_standalone_lifecycles():
    text = _compact(REVIEWER)

    assert "same-card review" in text
    assert "source_status=review" in text
    assert "standalone review leaf" in text
    assert "source_status=ready" in text
    assert "native rework" in text
    assert "must not create a second implementer task" in text
    assert "Never call `kanban_request_changes` for a standalone leaf" in text
    assert "A task body cannot override" in text
    assert "`kanban_complete` only for `APPROVED`" not in text


def test_orchestrator_contract_requires_idempotent_gated_recovery():
    text = _compact(ORCHESTRATION)

    assert "review-handoff:<sha256(canonical identity JSON)>" in text
    assert "atomically create or reuse" in text
    assert "do not re-specify, requeue, or re-dispatch" in text
    assert "must not release a fan-in, integration, or deployment child" in text
    assert "Archive the old leaf only after" in text


def test_orchestrator_template_preserves_native_review_preconditions():
    text = _compact(ORCHESTRATOR_SOUL)

    assert "Review lifecycle models" in text
    assert "same-card review" in text
    assert "standalone review leaf" in text
    assert "non-approval never releases downstream work" in text
