from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "examples" / "project-policy.yaml"
ROLE_DOC = ROOT / "docs" / "profile-roles.md"
RUNTIME_DOC = ROOT / "docs" / "kanban-factory-runtime.md"
SOUL_TEMPLATE = ROOT / "docs" / "orchestrator-soul-template.md"
OBSERVER_SKILL = ROOT / "skills" / "operator-observer" / "SKILL.md"
README = ROOT / "README.md"


def _compact(text: str) -> str:
    return " ".join(text.lower().split())


def test_policy_declares_a_read_only_operator_observer():
    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    profiles = policy["profiles"]
    assert "operator_observer" in profiles
    bridge = policy["operator_bridge"]
    assert bridge["observer_profile"] == "operator_observer"
    assert bridge["orchestrator_profile"] == "orchestrator"
    assert bridge["mutation_owner"] == "orchestrator"
    assert set(bridge["observer_actions"]) == {
        "read_evidence",
        "transport_decision_response",
    }


def test_observer_role_is_explicit_and_product_agnostic():
    role = _compact(ROLE_DOC.read_text(encoding="utf-8"))
    runtime = _compact(RUNTIME_DOC.read_text(encoding="utf-8"))
    soul = _compact(SOUL_TEMPLATE.read_text(encoding="utf-8"))
    skill = _compact(OBSERVER_SKILL.read_text(encoding="utf-8"))

    for text in (role, runtime, soul, skill):
        assert "operator observer" in text or "operator-observer" in text
        assert "read-only" in text
        assert "orchestrator" in text
        assert "dispatch" in text

    assert "read-only" in skill
    assert "roll out" in skill or "rollout" in skill

    for forbidden in (
        "sustainical",
        "grace",
        "notion",
        "slack",
        "argocd",
        "esg-",
        "/home/",
    ):
        assert forbidden not in skill


def test_observer_skill_is_installed_in_public_index():
    readme = _compact(README.read_text(encoding="utf-8"))
    assert "operator-observer" in readme
    assert "skills/operator-observer/skill.md" in readme
