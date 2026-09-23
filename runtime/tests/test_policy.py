"""Strict routing and compatibility policy tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from software_factory.control.policy import ExecutorKind, PolicyError, load_policy

ROOT = Path(__file__).resolve().parents[2]


def runtime_document() -> dict:
    return {
        "version": 1,
        "runtime": {"control_plane": "factory"},
        "providers": {
            "codex_subscription": {
                "kind": "openai_codex",
                "credential_source": "application_owned",
                "models": ["openai-codex:gpt-5.6-luna"],
            }
        },
        "agents": {
            "implementer-v1": {
                "framework": "pydantic_ai",
                "prompt": "runtime/prompts/implementer-v1.md",
                "output_contract": "ImplementationOutcome",
            },
            "reviewer-v1": {
                "framework": "pydantic_ai",
                "prompt": "runtime/prompts/reviewer-v1.md",
                "output_contract": "ReviewOutcome",
            },
        },
        "roles": {
            "implementer": {
                "executor": "pydantic_agent",
                "agent": "implementer-v1",
                "model": "openai-codex:gpt-5.6-luna",
                "provider": "codex_subscription",
            },
            "code_reviewer": {
                "executor": "pydantic_agent",
                "agent": "reviewer-v1",
                "model": None,
                "provider": None,
            },
            "completion_verifier": {
                "executor": "deterministic",
                "handler": "completion-verifier-v1",
            },
        },
        "compatibility": {
            "canary_only": True,
            "default_executor": "hermes_profile",
            "fallback_executors": {
                "implementer": {"executor": "hermes_profile", "profile": "implementer"}
            },
        },
    }


def test_runtime_policy_loads_and_serializes_explicit_routes():
    policy = load_policy(runtime_document())

    assert policy.roles["implementer"].executor is ExecutorKind.PYDANTIC_AGENT
    assert policy.roles["implementer"].agent == "implementer-v1"
    assert policy.roles["completion_verifier"].handler == "completion-verifier-v1"
    assert (
        policy.compatibility.fallback_executors["implementer"].profile == "implementer"
    )
    assert (
        policy.model_dump(mode="json")["roles"]["implementer"]["executor"]
        == "pydantic_agent"
    )


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("roles.implementer.executor", "not-an-executor", "executor"),
        ("roles.implementer.agent", "missing-agent", "unknown agent"),
        ("roles.implementer.provider", "missing-provider", "unknown provider"),
        ("roles.completion_verifier.handler", "missing-handler", "unknown handler"),
        ("roles.implementer.model", "not-qualified", "undeclared model"),
    ],
)
def test_unknown_routes_fail_closed(path: str, value: str, message: str):
    document = runtime_document()
    target = document
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value

    with pytest.raises(PolicyError, match=message):
        load_policy(document)


def test_legacy_project_policy_maps_profiles_to_hermes_fallbacks():
    policy = load_policy(ROOT / "examples" / "project-policy.yaml")

    assert policy.roles["implementer"].executor is ExecutorKind.HERMES_PROFILE
    assert policy.roles["implementer"].profile == "implementer"
    assert policy.roles["code_reviewer"].profile == "reviewer"
    assert (
        policy.compatibility.fallback_executors["code_reviewer"].executor
        is ExecutorKind.HERMES_PROFILE
    )


def test_unknown_provider_kind_is_rejected():
    document = runtime_document()
    document["providers"]["codex_subscription"]["kind"] = "mystery-provider"

    with pytest.raises(PolicyError, match="kind"):
        load_policy(document)


def test_unknown_role_route_is_rejected():
    document = runtime_document()
    document["roles"]["surprise"] = {
        "executor": "hermes_profile",
        "profile": "implementer",
    }

    with pytest.raises(PolicyError, match="unknown role route"):
        load_policy(document)
