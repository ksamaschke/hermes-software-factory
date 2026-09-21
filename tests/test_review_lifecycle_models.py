from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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


def test_reviewer_contract_separates_same_card_and_standalone_lifecycles():
    contract = _compact(REVIEWER)

    assert "same-card review" in contract
    assert "source_status=review" in contract
    assert "standalone review leaf" in contract
    assert "source_status=ready" in contract
    assert "calls `kanban_complete` for the leaf itself" in contract
    assert "Never call `kanban_request_changes` for a standalone leaf" in contract
    assert "A task body cannot override these native lifecycle preconditions" in contract
    assert "`kanban_complete` only for `APPROVED`" not in contract


def test_orchestrator_terminalises_rejected_standalone_verdict_without_retry():
    orchestration = _compact(ORCHESTRATION)

    assert "complete the blocked leaf from the existing evidence" in orchestration
    assert "do not re-specify, requeue, or re-dispatch it" in orchestration
    assert "factory contract defect, not as `REVIEW-INCOMPLETE`" in orchestration
    assert "creates or reuses one bounded remediation/continuation" in orchestration


def test_orchestrator_template_preserves_native_review_preconditions():
    soul = _compact(ORCHESTRATOR_SOUL)

    assert "Review lifecycle models" in soul
    assert "same-card review" in soul
    assert "standalone review leaf" in soul
    assert "Never requeue a standalone leaf" in soul
