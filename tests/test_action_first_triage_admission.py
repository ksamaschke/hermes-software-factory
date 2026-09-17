"""Causal contract tests for zero-ready action-first triage admission."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = (
    ROOT
    / "skills"
    / "kanban-factory-operations"
    / "references"
    / "action-first-triage-admission.md"
)
OPERATIONS = ROOT / "skills" / "kanban-factory-operations" / "SKILL.md"


def _compact(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").lower().split())


def test_zero_ready_contract_requires_triage_and_one_audited_action_before_idle():
    text = _compact(REFERENCE)

    for phrase in (
        "zero-ready tick",
        "current triage frontier",
        "ready, running, review, todo, blocked, and triage",
        "parent-complete",
        "at most one existing canonical",
        "one bounded audited admission/remediation action",
        "status-only no-progress report",
        "safe independent triage action",
        "idempotency key",
        "read back",
        "wake-up input",
        "must never rewrite",
        "unverified no-op is retried on the next tick",
        "coordinator_admission_contract.py",
        "fail closed before iteration",
        "reserved hard dispositions",
        "never caller-selectable product actions",
    ):
        assert phrase in text

    scan = text.index("### 1. read the live frontier")
    parent = text.index("### 2. reconcile parent completion")
    selection = text.index("### 4. select at most one lane")
    action = text.index("### 5. perform one bounded audited action")
    idle = text.index("## idle-by-gating")
    assert scan < parent < selection < action < idle


def test_candidate_selection_is_existing_parent_complete_and_wip_bounded():
    text = _compact(REFERENCE)

    for phrase in (
        "canonical lane",
        "existing canonical parent-complete lane",
        "every declared parent",
        "terminal completion state",
        "worker summary alone is not parent completion",
        "global and per-profile wip",
        "never bypass wip limits",
        "do not create a new lane",
        "deterministic order",
    ):
        assert phrase in text


def test_gate_dispositions_preserve_parked_external_and_invalid_work():
    text = _compact(REFERENCE)

    for phrase in (
        "parked tasks remain untouched",
        "never unblock or dispatch parked tasks",
        "genuine external/human gate",
        "malformed or stale contract",
        "fail closed",
        "duplicate or idempotency conflict",
        "hold only that lane",
        "continue independent work",
        "protected signer/credential boundary",
        "never print or copy credentials",
    ):
        assert phrase in text


def test_internal_factory_defects_use_keyed_remediation_not_human_escalation():
    text = _compact(REFERENCE)

    assert "internal factory defect" in text
    assert "routine internal publication" in text
    assert "route a keyed remediation" in text
    assert "reuse the existing remediation task" in text
    assert "not a reason to ask a human" in text
    protected_section = text.index("### protected signer/credential boundaries")
    protected = text.index("protected signer/ credential boundary", protected_section)
    assert text.index("route a keyed remediation") < protected


def test_worker_blocker_claims_are_projected_and_reclassified_by_coordinator():
    text = _compact(REFERENCE)

    for phrase in (
        "worker blocker ownership is not authoritative",
        "worker-selected `block_kind`",
        "evidence only",
        "must project bounded idle blocked/triage candidates",
        "must not discard a candidate solely because the worker selected",
        "an internal prerequisite",
        "repair or route that prerequisite",
        "worker's label alone never supplies that proof",
    ):
        assert phrase in text

    internal = text.index("an internal prerequisite")
    external = text.index("preserve a worker block as external")
    assert internal < external


def test_parent_completion_preserves_parent_gates_and_child_release_conditions():
    text = _compact(REFERENCE)

    for phrase in (
        "parent completion is phase-causal",
        "a pre-created child does not make its parent complete",
        "failed, skipped, missing, stale, or mismatched required check",
        "a pushed artifact or terminal worker summary does not override",
        "child's explicit release condition",
        "do not move a red parent-owned gate into the child handoff",
        "keep downstream children dependency-gated",
        "is not progress around the red gate",
    ):
        assert phrase in text

    completion = text.index("a pre-created child does not make its parent complete")
    repair = text.index("re-specify the blocked canonical lane", completion)
    release = text.index("keep downstream children dependency-gated", completion)
    assert completion < repair < release


def test_terminal_prerequisite_phase_is_encoded_in_the_graph():
    text = _compact(REFERENCE)
    skill = _compact(OPERATIONS)

    for phrase in (
        "link the terminal prerequisite phase",
        "approval of that pr is not base availability",
        "prerequisite source/review -> guarded merge/migration -> downstream validation/review",
        "do not release a consumer merely because",
        "live base state and fresh exact-artifact checks",
    ):
        assert phrase in text

    assert "link downstream work to the terminal prerequisite phase" in skill
    assert "review approval is not enough" in skill
    assert "coordinator-owned rather than operator gates" in skill


def test_idle_by_gating_requires_complete_scans_and_no_safe_action():
    text = _compact(REFERENCE)
    idle_section = text[text.index("idle-by-gating") :]

    for phrase in (
        "complete ready, running, review, todo, blocked, and triage scans",
        "no existing canonical parent-complete lane is safe",
        "no safe independent lane exists",
        "every remaining candidate has a recorded gate",
        "zero-ready observation alone never justifies",
    ):
        assert phrase in idle_section

    assert "status-only no-progress" in idle_section


def test_operations_surface_references_the_action_first_contract():
    operations = _compact(OPERATIONS)

    assert "action-first-triage-admission.md" in operations
    for phrase in (
        "zero-ready",
        "current triage frontier",
        "at most one existing canonical parent-complete lane",
        "one bounded audited admission/remediation action",
        "hold only that lane",
        "continue independent work",
        "wake-up input only",
        "must not replace an already derived product admission action",
        "retry an unverified no-op on the next tick",
        "coordinator_admission_contract.py",
        "bounded built-in mapping",
        "reserved outputs derived from causal fields",
        "worker-selected blocker kinds are non-authoritative claims",
        "a worker label alone is never that proof",
        "a pre-created downstream child does not by itself prove parent completion",
        "never convert a pushed artifact or terminal worker summary into completion",
    ):
        assert phrase in operations


def test_action_first_public_contract_stays_project_agnostic():
    text = "\n".join(_compact(path) for path in (REFERENCE, OPERATIONS))

    for forbidden_pattern in (
        r"\bsustainical\b",
        r"\besg[-_]\w*",
        r"\bgrace\b",
        r"\bnotion\b",
        r"\bslack\b",
        r"/home/",
        r"/users/",
    ):
        assert not re.search(forbidden_pattern, text)
