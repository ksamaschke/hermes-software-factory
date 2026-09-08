import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "examples" / "project-policy.yaml"
PROFILE_ROLES = ROOT / "docs" / "profile-roles.md"
SOUL_TEMPLATE = ROOT / "docs" / "orchestrator-soul-template.md"


def _text(*relative_paths: str) -> str:
    return "\n".join(
        (ROOT / relative_path).read_text(encoding="utf-8")
        for relative_path in relative_paths
    )


def _compact(text: str) -> str:
    return " ".join(text.lower().split())


def test_policy_declares_bounded_orchestrator_authority():
    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    authority = policy["decision_authority"]

    assert authority["mode"] in {"delegated", "approval_required"}
    assert authority["default"] == "smallest_adequate_policy_compliant"

    delegated = set(authority["delegated_scope"])
    non_delegable = set(authority["non_delegable"])
    assert delegated
    assert non_delegable
    assert delegated.isdisjoint(non_delegable)
    assert {
        "decision",
        "rationale",
        "owner",
        "acceptance_evidence",
        "rollback_or_fallback",
        "next_gate",
    } <= set(authority["required_decision_record"])

    bridge = policy["operator_bridge"]
    assert bridge["mode"] == "central_transport_only"
    assert bridge["enabled"] is True
    assert bridge["request_marker"] == "OPERATOR_INPUT_REQUIRED"
    assert bridge["response_marker"] == "DECISION"
    assert set(bridge["dedupe_on"]) >= {"source_item", "kanban_task"}


def test_policy_defines_safety_hold_and_approval_required_mode():
    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    non_delegable = set(policy["decision_authority"]["non_delegable"])
    assert "undefined_safety_critical_value" in non_delegable

    text = _compact(
        (ROOT / "docs" / "policy-resolution.md").read_text(encoding="utf-8")
    )
    assert "safety-critical value is unspecified" in text
    assert "hold only the affected action" in text
    assert "approval_required" in text
    assert "planning-only" in text


def test_orchestrator_contract_is_operating_and_architecture_authority():
    text = _compact(
        _text(
            "docs/profile-roles.md",
            "docs/kanban-factory-runtime.md",
            "README.md",
        )
    )

    for phrase in (
        "operating and architecture authority",
        "architecture and cross-component interface",
        "decomposition",
        "remediation",
        "recovery",
        "review",
        "wip",
        "next safe phase",
        "operator approval for routine",
    ):
        assert phrase in text

    runtime = _compact(
        (ROOT / "docs" / "kanban-factory-runtime.md").read_text(encoding="utf-8")
    )
    assert "mechanical" in runtime
    assert "gateway" in runtime


def test_shared_skills_require_the_common_decision_ladder():
    profile = _compact(PROFILE_ROLES.read_text(encoding="utf-8"))
    assert "## decision ladder" in profile
    for phrase in (
        "binds the canonical source item",
        "diagnoses the observed cause",
        "chooses the next phase",
        "assigns ownership",
        "reads back the exact mutation",
        "preserves the prior completed decision",
    ):
        assert phrase in profile

    for relative_path in (
        "skills/kanban-factory-operations/SKILL.md",
        "skills/kanban-implementation-workflow/SKILL.md",
        "skills/kanban-progress-evidence/SKILL.md",
    ):
        text = _compact((ROOT / relative_path).read_text(encoding="utf-8"))
        assert "shared decision ladder" in text
        assert "selected issue or locked lane" in text


@pytest.mark.parametrize(
    "skill",
    [
        "kanban-factory-operations",
        "kanban-implementation-workflow",
        "kanban-progress-evidence",
    ],
)
def test_installed_skill_links_to_canonical_decision_ladder(tmp_path, skill):
    # Raw SKILL.md installs must work without the collection checkout or siblings.
    installed = tmp_path / skill / "SKILL.md"
    installed.parent.mkdir()
    installed.write_text(
        (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    text = installed.read_text(encoding="utf-8")
    links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
    contract_links = [urlsplit(link) for link in links if "profile-roles.md" in link]
    assert contract_links, "Installed skill needs a fetchable canonical contract link"
    for link in contract_links:
        assert link.scheme == "https"
        assert link.netloc == "github.com"
        assert link.path == (
            "/ksamaschke/hermes-software-factory/blob/main/docs/profile-roles.md"
        )
        assert link.fragment == "decision-ladder"
        assert not link.query
        # Verify the target and anchor offline; live availability is not a CI gate.
        assert "### Decision ladder" in PROFILE_ROLES.read_text(encoding="utf-8")
    assert "`docs/profile-roles.md`" not in text
    assert "web_extract" in text
    assert "cannot be fetched" in text


def test_central_bridge_requires_recommendation_and_keeps_workers_off_human_channels():
    text = _compact(
        (ROOT / "docs" / "central-kanban-reporting.md").read_text(encoding="utf-8")
    )
    for phrase in (
        "workers and task cards do not contact the user directly",
        "one deduplicated clarification packet",
        "recommended default",
        "non-delegable reason",
        "impact of waiting",
        "operator response",
        "transport handoff",
        "unanswered requests are not repeated unless evidence materially changes",
    ):
        assert phrase in text


def test_orchestrator_soul_template_is_generic_and_complete():
    text = SOUL_TEMPLATE.read_text(encoding="utf-8")
    lowered = text.lower()
    for heading in (
        "## mission",
        "## ownership",
        "## standing delegated authority",
        "## decision ladder",
        "## operator clarification boundary",
        "## evidence and reporting",
        "## boundaries",
    ):
        assert heading in lowered

    for project_specific in (
        "sustainical",
        "esg-",
        "grace",
        "notion",
        "slack",
        "argocd",
    ):
        assert project_specific not in lowered


def test_authority_contract_keeps_runtime_safety_boundaries():
    text = _compact(
        _text(
            "docs/profile-roles.md",
            "docs/central-kanban-reporting.md",
            "skills/kanban-factory-operations/SKILL.md",
        )
    )
    for phrase in (
        "second dispatcher",
        "do not bypass independent review",
        "destructive or irreversible",
        "production",
        "preserve useful",
    ):
        assert phrase in text


def test_public_authority_contract_stays_product_agnostic():
    text = _compact(
        _text(
            "README.md",
            "docs/profile-roles.md",
            "docs/kanban-factory-runtime.md",
            "docs/policy-resolution.md",
            "docs/central-kanban-reporting.md",
            "docs/orchestrator-soul-template.md",
            "examples/project-policy.yaml",
            "skills/kanban-factory-operations/SKILL.md",
            "skills/kanban-implementation-workflow/SKILL.md",
            "skills/kanban-progress-evidence/SKILL.md",
        )
    )
    for forbidden_pattern in (
        r"\bsustainical\b",
        r"\besg[-_]\w*",
        r"\bgrace\b",
        r"\bnotion\b",
        r"\bslack\b",
        r"192\.168\.",
        r"/users/",
        r"/home/",
    ):
        assert not re.search(forbidden_pattern, text)
