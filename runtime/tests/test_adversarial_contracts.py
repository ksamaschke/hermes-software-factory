"""Adversarial regression coverage for the typed control-plane boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from software_factory import (
    BlockedOutcome,
    ClaimedRun,
    CommandSpec,
    FactoryEvent,
    FactoryPolicy,
    FailedOutcome,
    Failure,
    ImplementationOutcome,
    Lease,
    PlanOutcome,
    PlanTask,
    RepositoryIdentity,
    RunIdentity,
    TaskEnvelope,
    TaskRepository,
    TaskRepositoryFacade,
    TaskRole,
    TaskState,
    WorkspaceIdentity,
    build_agent_capabilities,
    load_policy,
)
from software_factory.agents.contracts import TaskEnvelope as AgentTaskEnvelope
from software_factory.control.policy import PolicyError, ProviderDefinition


def envelope(
    *, task_id: str = "task-1", run_id: str = "run-1", **overrides: object
) -> TaskEnvelope:
    values: dict[str, object] = {
        "task_id": task_id,
        "run_id": run_id,
        "role": TaskRole.IMPLEMENTER,
        "repository": {
            "provider": "github",
            "project": "owner",
            "repository": "repo",
        },
        "workspace": {"workspace_id": "workspace-1", "root": "/tmp/workspace-1"},
        "base_revision": "base-1",
        "objective": "run the bounded implementation task",
        "acceptance": [{"id": "one", "description": "one criterion"}],
        "deadline": datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return TaskEnvelope.model_validate(values)


def run_identity(*, task_id: str = "task-1", run_id: str = "run-1") -> RunIdentity:
    return RunIdentity(task_id=task_id, run_id=run_id)


def runtime_policy() -> dict:
    return {
        "version": 1,
        "providers": {
            "primary": {
                "kind": "openai",
                "models": ["openai:model-1"],
            }
        },
        "agents": {
            "agent-1": {
                "prompt": "runtime/prompts/agent.md",
                "output_contract": "ImplementationOutcome",
            }
        },
        "roles": {
            "implementer": {
                "executor": "pydantic_agent",
                "agent": "agent-1",
                "provider": "primary",
                "model": "openai:model-1",
            }
        },
    }


def test_agent_contract_import_is_the_canonical_api_object():
    from software_factory.api.contracts import TaskEnvelope as ApiTaskEnvelope

    assert AgentTaskEnvelope is ApiTaskEnvelope


def test_contract_models_are_frozen_and_sequences_are_immutable_json_arrays():
    value = envelope()

    with pytest.raises(ValidationError):
        value.task_id = "changed"  # type: ignore[misc]
    assert isinstance(value.acceptance, tuple)
    with pytest.raises(AttributeError):
        value.acceptance.append(None)  # type: ignore[union-attr]
    payload = json.loads(value.model_dump_json())
    assert isinstance(payload["acceptance"], list)


@pytest.mark.parametrize(
    "field,value",
    [
        ("acceptance", "criterion"),
        ("acceptance", {"id": "criterion"}),
        ("allowed_commands", "pytest"),
        ("allowed_commands", {"argv": ["pytest"]}),
        ("evidence", "evidence-ref"),
        ("evidence", {"kind": "test"}),
    ],
)
def test_sequence_fields_reject_scalars_and_mappings(field: str, value: object):
    with pytest.raises(ValidationError, match="sequence"):
        envelope(**{field: value})


def test_claimed_run_requires_matching_run_lease_and_envelope_identities():
    run = run_identity()
    valid_lease = Lease(
        run=run,
        expires_at=datetime(2026, 9, 23, 13, 0, tzinfo=UTC),
    )
    valid_envelope = envelope()
    ClaimedRun(run=run, lease=valid_lease, envelope=valid_envelope)

    with pytest.raises(ValidationError, match="task_id"):
        ClaimedRun(
            run=run,
            lease=Lease(
                run=run_identity(task_id="other-task"),
                expires_at=valid_lease.expires_at,
            ),
            envelope=valid_envelope,
        )
    with pytest.raises(ValidationError, match="run_id"):
        ClaimedRun(
            run=run,
            lease=valid_lease,
            envelope=envelope(run_id="other-run"),
        )


def test_task_state_is_fail_closed_and_has_typed_blocked_failed_outcomes():
    run = run_identity()
    outcome = ImplementationOutcome(
        status="candidate_ready",
        summary="candidate is ready",
        candidate_revision="candidate-1",
        next_gate="review",
    )

    TaskState(task_id="task-1", state="queued")
    TaskState(task_id="task-1", state="running", run=run)
    TaskState(task_id="task-1", state="completed", run=run, outcome=outcome)
    TaskState(
        task_id="task-1",
        state="blocked",
        run=run,
        outcome=BlockedOutcome(
            blocker={"code": "operator", "summary": "operator decision required"},
            next_gate="operator_decision",
        ),
    )
    TaskState(
        task_id="task-1",
        state="failed",
        run=run,
        outcome=FailedOutcome(
            failure=Failure(code="runner", summary="runner failed"),
            next_gate="retry_policy",
        ),
    )

    invalid = [
        {"task_id": "task-1", "state": "queued", "run": run},
        {"task_id": "task-1", "state": "claimed"},
        {"task_id": "task-1", "state": "running", "run": run, "outcome": outcome},
        {"task_id": "task-1", "state": "completed", "run": run},
        {"task_id": "task-1", "state": "blocked", "run": run},
        {"task_id": "task-1", "state": "failed", "run": run, "outcome": outcome},
    ]
    for values in invalid:
        with pytest.raises(ValidationError):
            TaskState.model_validate(values)


def test_workspace_roots_and_relative_paths_reject_escape_forms():
    for root in (
        "workspace",
        "~/workspace",
        "C:workspace",
        "\\workspace",
        "/tmp/../workspace",
        "C:\\tmp\\..\\workspace",
    ):
        with pytest.raises(ValidationError):
            WorkspaceIdentity(workspace_id="w", root=root)
    for root in ("/tmp/workspace", "C:/workspace", "C:\\workspace"):
        WorkspaceIdentity(workspace_id="w", root=root)

    for relative in (
        "/etc/passwd",
        "~/secret",
        "../outside",
        "nested/../outside",
        "nested/./file",
        "nested//file",
        "C:/outside",
        "C:\\outside",
        "\\outside",
        "",
    ):
        with pytest.raises(ValidationError):
            CommandSpec(argv=("pytest",), cwd=relative)


def test_command_spec_is_argv_only_and_shell_safe_by_shape():
    spec = CommandSpec(argv=["python", "-m", "pytest"], cwd="runtime")
    assert spec.argv == ("python", "-m", "pytest")
    assert "command" not in CommandSpec.model_fields
    for argv in ([], [""], "python", {"program": "python"}, ["python", ""]):
        with pytest.raises(ValidationError):
            CommandSpec(argv=argv)
    with pytest.raises(ValidationError):
        CommandSpec(command="python -m pytest")


def test_repository_urls_and_credential_references_cannot_carry_secrets():
    for url in (
        "https://user:password@example.invalid/repo",
        "https://example.invalid/repo?token=value",
        "https://example.invalid/repo#fragment",
    ):
        with pytest.raises(ValidationError):
            RepositoryIdentity(provider="github", repository="repo", url=url)

    ProviderDefinition(kind="openai", credential_reference="vault://factory/openai")
    ProviderDefinition(kind="openai", credential_reference="env://OPENAI_KEY")
    ProviderDefinition(kind="openai", credential_reference=None)
    for reference in (
        "ghp_inline_token",
        "https://example.invalid/secret",
        "vault://user:password@example.invalid/secret",
        "vault://factory/secret?version=1",
        "vault://factory/secret#fragment",
    ):
        with pytest.raises(ValidationError):
            ProviderDefinition(kind="openai", credential_reference=reference)


def test_event_attributes_are_typed_immutable_scalar_values_and_secret_free():
    event = FactoryEvent(
        event_type="heartbeat",
        run=run_identity(),
        occurred_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        attributes=[
            {"key": "status", "value": "running"},
            {"key": "attempt", "value": 1},
        ],
    )
    assert isinstance(event.attributes, tuple)
    assert json.loads(event.model_dump_json())["attributes"] == [
        {"key": "status", "value": "running"},
        {"key": "attempt", "value": 1},
    ]
    for attributes in (
        {"key": "status", "value": "running"},
        [{"key": "status", "value": {"nested": "object"}}],
        [{"key": "API-KEY", "value": "secret"}],
        [{"key": "access_token", "value": "secret"}],
        [{"key": "token_reference", "value": "not-a-token"}],
        [
            {"key": "status", "value": "one"},
            {"key": "STATUS", "value": "two"},
        ],
        [{"key": "status", "value": "Bearer abcdefghijklmnop"}],
    ):
        with pytest.raises(ValidationError):
            FactoryEvent(
                event_type="heartbeat",
                run=run_identity(),
                occurred_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
                attributes=attributes,
            )
    with pytest.raises(ValidationError):
        FactoryEvent(
            event_type="heartbeat",
            run=run_identity(),
            occurred_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
            data={"status": "running"},
        )


def test_plan_and_provider_fallback_graphs_reject_self_reference_and_cycles():
    with pytest.raises(ValidationError, match="self-reference"):
        PlanTask(
            task_id="a",
            role=TaskRole.PLANNER,
            objective="plan",
            dependencies=["a"],
        )

    first = PlanTask(
        task_id="a",
        role=TaskRole.PLANNER,
        objective="first",
        dependencies=["b"],
    )
    second = PlanTask(
        task_id="b",
        role=TaskRole.PLANNER,
        objective="second",
        dependencies=["a"],
    )
    with pytest.raises(ValidationError, match="cycle"):
        PlanOutcome(
            status="planned", summary="cycle", tasks=[first, second], next_gate="run"
        )

    self_reference = runtime_policy()
    self_reference["providers"]["primary"]["fallback_provider"] = "primary"
    with pytest.raises(PolicyError, match="cycle"):
        load_policy(self_reference)

    cycle = runtime_policy()
    cycle["providers"]["secondary"] = {"kind": "openai", "fallback_provider": "primary"}
    cycle["providers"]["primary"]["fallback_provider"] = "secondary"
    with pytest.raises(PolicyError, match="cycle"):
        load_policy(cycle)


def test_policy_is_top_level_strict_json_safe_and_requires_a_role():
    assert FactoryPolicy.model_config["extra"] == "forbid"
    document = runtime_policy()
    document["runtime"] = {"limits": {"max": 1}, "enabled": True}
    policy = load_policy(document)
    assert policy.runtime["limits"] == {"max": 1}

    for invalid in (
        {**runtime_policy(), "unknown_section": {}},
        {**runtime_policy(), "runtime": {"handle": object()}},
        {**runtime_policy(), "roles": {}},
    ):
        with pytest.raises(PolicyError):
            load_policy(invalid)


def test_policy_mappings_are_deeply_immutable_and_defensively_copied():
    document = runtime_policy()
    runtime = {"nested": {"items": [{"name": "before"}]}}
    document["runtime"] = runtime
    policy = load_policy(document)

    for field_name in ("providers", "agents", "handlers", "roles"):
        with pytest.raises(AttributeError):
            getattr(policy, field_name).clear()
    with pytest.raises(AttributeError):
        policy.compatibility.fallback_executors.clear()
    with pytest.raises(TypeError):
        policy.roles["implementer"] = policy.roles["implementer"]  # type: ignore[index]

    with pytest.raises(AttributeError):
        policy.runtime.clear()
    with pytest.raises(TypeError):
        policy.runtime["new"] = True  # type: ignore[index]
    with pytest.raises(AttributeError):
        policy.runtime["nested"].clear()  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        policy.runtime["nested"]["new"] = True  # type: ignore[index]
    with pytest.raises(AttributeError):
        policy.runtime["nested"]["items"].append({"name": "after"})  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        policy.runtime["nested"]["items"][0]["name"] = "changed"  # type: ignore[index]

    runtime["nested"]["items"].append({"name": "source-only"})
    runtime["nested"]["name"] = "source-only"
    document["providers"]["primary"]["models"].append("openai:source-only")
    document["roles"].clear()

    assert policy.runtime["nested"]["items"][0]["name"] == "before"
    assert len(policy.runtime["nested"]["items"]) == 1
    assert "name" not in policy.runtime["nested"]
    assert policy.providers["primary"].models == ("openai:model-1",)
    assert "implementer" in policy.roles

    payload = policy.model_dump(mode="json")
    assert isinstance(payload["runtime"], dict)
    assert isinstance(payload["runtime"]["nested"]["items"], list)
    assert json.loads(policy.model_dump_json()) == payload


def test_duplicate_yaml_keys_are_rejected_before_policy_validation():
    duplicate = "version: 1\nroles: {}\nroles: {}\n"
    with pytest.raises(PolicyError, match="duplicate"):
        load_policy(duplicate)


def test_legacy_policy_requires_profiles_and_preserves_typed_legacy_settings():
    legacy = {
        "version": 1,
        "profiles": {
            "orchestrator": "default",
            "implementer": "implementer",
            "code_reviewer": "reviewer",
            "code_reviewer_model_default": "openai/reviewer-strong",
            "code_reviewer_model_routine": "openai/reviewer-routine",
            "implementer_vendor_family": "openai",
            "code_reviewer_vendor_family": "anthropic",
        },
    }
    policy = load_policy(legacy)
    settings = policy.compatibility.legacy_settings
    assert settings is not None
    assert settings.implementer_vendor_family == "openai"
    assert settings.code_reviewer_vendor_family == "anthropic"
    assert settings.code_reviewer_model_default == "openai/reviewer-strong"
    assert policy.roles["code_reviewer"].profile == "reviewer"

    for role in ("orchestrator", "implementer", "code_reviewer"):
        invalid = {"version": 1, "profiles": dict(legacy["profiles"])}
        invalid["profiles"][role] = None
        with pytest.raises(PolicyError):
            load_policy(invalid)


def test_agent_repository_facade_exposes_only_the_four_operations():
    calls: list[str] = []
    sentinel = object()

    def claim(task_id: str, executor_id: str):
        calls.append(f"claim:{task_id}:{executor_id}")
        return sentinel

    def heartbeat(run):
        calls.append("heartbeat")
        return sentinel

    def append_event(run, event):
        calls.append("append_event")

    def finish(run, outcome):
        calls.append("finish")
        return sentinel

    facade = build_agent_capabilities(claim, heartbeat, append_event, finish)
    assert isinstance(facade, TaskRepositoryFacade)
    assert isinstance(facade, TaskRepository)
    assert {name for name in dir(facade) if not name.startswith("_")} == {
        "append_event",
        "claim",
        "finish",
        "heartbeat",
    }
    assert not hasattr(facade, "backend")
    assert not hasattr(facade, "database")
    assert facade.claim("task-1", "executor-1") is sentinel
    assert facade.heartbeat(run_identity()) is sentinel
    facade.append_event(run_identity(), object())
    assert facade.finish(run_identity(), object()) is sentinel
    assert calls == ["claim:task-1:executor-1", "heartbeat", "append_event", "finish"]

    class StructuralRepository:
        def claim(self, task_id: str, executor_id: str): ...

        def heartbeat(self, run): ...

        def append_event(self, run, event): ...

        def finish(self, run, outcome): ...

    assert isinstance(StructuralRepository(), TaskRepository)
