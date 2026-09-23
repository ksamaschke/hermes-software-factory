"""Causal regressions for issue #41 runtime-contract review findings."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from software_factory import (
    MAX_EVENT_ATTRIBUTE_KEY_LENGTH,
    MAX_EVENT_ATTRIBUTE_VALUE_LENGTH,
    MAX_FACTORY_EVENT_ATTRIBUTES,
    AcceptanceCriterion,
    BlockedOutcome,
    ClaimedRun,
    EventAttribute,
    EvidenceRef,
    FactoryEvent,
    FailedOutcome,
    Failure,
    ImplementationOutcome,
    Lease,
    PlanOutcome,
    ReviewOutcome,
    RoleRoute,
    RunIdentity,
    TaskEnvelope,
    TaskRole,
    TaskState,
    WorkspaceIdentity,
    build_agent_capabilities,
    load_policy,
)
from software_factory import TestEvidence as ContractTestEvidence
from software_factory.control.policy import PolicyError, ProviderDefinition


def envelope(task_id: str = "task-41", run_id: str = "run-41") -> TaskEnvelope:
    return TaskEnvelope.model_validate(
        {
            "task_id": task_id,
            "run_id": run_id,
            "role": TaskRole.IMPLEMENTER,
            "repository": {
                "provider": "github",
                "project": "owner",
                "repository": "repo",
            },
            "workspace": {"workspace_id": "workspace-41", "root": "/tmp/workspace-41"},
            "base_revision": "base-41",
            "objective": "exercise the runtime contract",
            "acceptance": [{"id": "contract", "description": "contract holds"}],
            "deadline": datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        }
    )


def policy_document(
    *,
    kind: str = "openai",
    model: str = "openai:model-41",
    output_contract: str = "ImplementationOutcome",
) -> dict[str, object]:
    return {
        "version": 1,
        "providers": {"primary": {"kind": kind, "models": [model]}},
        "agents": {
            "agent-41": {
                "prompt": "runtime/prompts/agent-41.md",
                "output_contract": output_contract,
            }
        },
        "roles": {
            "implementer": {
                "executor": "pydantic_agent",
                "agent": "agent-41",
                "provider": "primary",
                "model": model,
            }
        },
    }


def test_claimed_run_fences_every_lease_ownership_field():
    run = RunIdentity(
        task_id="task-41",
        run_id="run-41",
        executor_id="executor-41",
        attempt=2,
        lease_id="lease-41",
    )
    valid_lease = Lease(
        run=run,
        expires_at=datetime(2026, 9, 23, 13, 0, tzinfo=UTC),
    )
    ClaimedRun(run=run, lease=valid_lease, envelope=envelope())

    for field_name, foreign_value in (
        ("task_id", "task-other"),
        ("run_id", "run-other"),
        ("executor_id", "executor-other"),
        ("attempt", 3),
        ("lease_id", "lease-other"),
    ):
        foreign_values = {
            "task_id": run.task_id,
            "run_id": run.run_id,
            "executor_id": run.executor_id,
            "attempt": run.attempt,
            "lease_id": run.lease_id,
        }
        foreign_values[field_name] = foreign_value
        with pytest.raises(ValidationError, match=field_name):
            ClaimedRun(
                run=run,
                lease=Lease(
                    run=RunIdentity(**foreign_values),
                    expires_at=valid_lease.expires_at,
                ),
                envelope=envelope(),
            )


@pytest.mark.parametrize(
    "outcome",
    [
        PlanOutcome(status="failed", summary="plan failed", next_gate="retry"),
        PlanOutcome(
            status="needs_decision", summary="decision required", next_gate="operator"
        ),
        ImplementationOutcome(
            status="needs_decision", summary="decision required", next_gate="operator"
        ),
        ImplementationOutcome(
            status="failed", summary="implementation failed", next_gate="retry"
        ),
        ReviewOutcome(
            verdict="CHANGES_REQUESTED",
            candidate_revision="candidate-41",
            reviewed_scope=["runtime/src/software_factory/api/contracts.py"],
        ),
        ReviewOutcome(
            verdict="REVIEW_INCOMPLETE",
            candidate_revision="candidate-41",
            reviewed_scope=["runtime/src/software_factory/api/contracts.py"],
        ),
        BlockedOutcome(
            blocker={"code": "operator", "summary": "operator decision required"},
            next_gate="operator",
        ),
        FailedOutcome(
            failure=Failure(code="runner", summary="runner failed"),
            next_gate="retry",
        ),
    ],
)
def test_completed_state_accepts_only_successful_outcomes(outcome: object):
    with pytest.raises(ValidationError):
        TaskState(
            task_id="task-41",
            state="completed",
            run=RunIdentity(task_id="task-41", run_id="run-41"),
            outcome=outcome,
        )  # type: ignore[arg-type]

    successful = [
        PlanOutcome(status="planned", summary="plan complete", next_gate="run"),
        PlanOutcome(status="ready", summary="plan ready", next_gate="run"),
        ImplementationOutcome(
            status="candidate_ready",
            summary="candidate ready",
            candidate_revision="candidate-41",
            next_gate="review",
        ),
        ReviewOutcome(
            verdict="APPROVED",
            candidate_revision="candidate-41",
            reviewed_scope=["runtime/src/software_factory/api/contracts.py"],
        ),
    ]
    for valid_outcome in successful:
        TaskState(
            task_id="task-41",
            state="completed",
            run=RunIdentity(task_id="task-41", run_id="run-41"),
            outcome=valid_outcome,
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RunIdentity(task_id="task-41", run_id="run-41", attempt="2"),
        lambda: AcceptanceCriterion(description="required", required="false"),
        lambda: ContractTestEvidence(command="pytest", exit_code="0"),
        lambda: ContractTestEvidence(command="pytest", exit_code=0, passed=1),
        lambda: RoleRoute(
            executor="deterministic", handler="handler-41", read_only_source="false"
        ),
    ],
)
def test_contract_scalars_reject_string_and_bool_coercion(factory):
    with pytest.raises(ValidationError):
        factory()


def test_policy_scalars_are_strict_but_native_yaml_values_remain_valid():
    load_policy(policy_document())
    with pytest.raises(PolicyError):
        load_policy({**policy_document(), "version": True})
    with pytest.raises(PolicyError):
        load_policy(
            {
                **policy_document(),
                "roles": {
                    "implementer": {
                        "executor": "pydantic_agent",
                        "agent": "agent-41",
                        "provider": "primary",
                        "model": "openai:model-41",
                        "max_runtime_seconds": "120",
                    }
                },
            }
        )


def test_workspace_root_is_canonical_before_binding_comparisons():
    assert (
        WorkspaceIdentity(workspace_id="workspace-41", root="/tmp//workspace-41/").root
        == "/tmp/workspace-41"
    )
    assert (
        WorkspaceIdentity(workspace_id="workspace-41", root="c:\\workspace-41").root
        == "C:/workspace-41"
    )
    with pytest.raises(ValidationError):
        WorkspaceIdentity(workspace_id="workspace-41", root="/tmp/./workspace-41")


def test_validated_copy_does_not_bypass_frozen_policy_validation():
    policy = load_policy(policy_document())
    with pytest.raises(ValidationError):
        policy.model_copy(update={"version": True})
    with pytest.raises(ValidationError):
        policy.model_copy(update={"roles": {}})
    copied = policy.validated_copy(update={"runtime": {"enabled": True}})
    assert copied.runtime["enabled"] is True


@pytest.mark.parametrize(
    ("kind", "prefix"),
    [
        ("openai_codex", "openai-codex"),
        ("openai", "openai"),
        ("anthropic", "anthropic"),
        ("gemini", "gemini"),
        ("google", "google"),
        ("ollama", "ollama"),
        ("mistral", "mistral"),
        ("xai", "xai"),
        ("openrouter", "openrouter"),
        ("azure_openai", "azure-openai"),
        ("vertex_ai", "vertex-ai"),
        ("bedrock", "bedrock"),
        ("local", "local"),
    ],
)
def test_every_known_provider_family_accepts_only_its_model_prefix(
    kind: str, prefix: str
):
    model = f"{prefix}:model-41"
    load_policy(policy_document(kind=kind, model=model))
    with pytest.raises(PolicyError):
        mismatched_prefix = "anthropic" if prefix != "anthropic" else "openai"
        load_policy(policy_document(kind=kind, model=f"{mismatched_prefix}:model-41"))


def test_custom_provider_preserves_provider_neutral_model_forms():
    load_policy(policy_document(kind="custom", model="vendor:model-41"))


def test_agent_output_contract_is_a_closed_routing_allowlist():
    with pytest.raises(PolicyError):
        load_policy(policy_document(output_contract="UnknownOutcome"))


def test_event_resource_caps_are_explicit_and_enforced():
    EventAttribute(
        key="k" * MAX_EVENT_ATTRIBUTE_KEY_LENGTH,
        value="v" * MAX_EVENT_ATTRIBUTE_VALUE_LENGTH,
    )
    with pytest.raises(ValidationError):
        EventAttribute(key="k" * (MAX_EVENT_ATTRIBUTE_KEY_LENGTH + 1), value="value")
    with pytest.raises(ValidationError):
        EventAttribute(key="key", value="v" * (MAX_EVENT_ATTRIBUTE_VALUE_LENGTH + 1))

    attributes = [
        {"key": f"key-{index}", "value": index}
        for index in range(MAX_FACTORY_EVENT_ATTRIBUTES)
    ]
    FactoryEvent(
        event_type="bounded",
        run=RunIdentity(task_id="task-41", run_id="run-41"),
        occurred_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        attributes=attributes,
    )
    with pytest.raises(ValidationError):
        FactoryEvent(
            event_type="bounded",
            run=RunIdentity(task_id="task-41", run_id="run-41"),
            occurred_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
            attributes=[*attributes, {"key": "one-too-many", "value": 1}],
        )


def test_event_and_evidence_text_use_conservative_credential_exclusions():
    EventAttribute(key="status", value="candidate-ready")
    EvidenceRef(
        kind="artifact",
        reference="https://example.invalid/reports/run-41",
        description="HTML report for the bounded test run",
    )
    synthetic_opaque_value = "AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPp"
    for value in (
        "Bearer synthetic-placeholder-value",
        "Authorization: synthetic-placeholder-value",
        synthetic_opaque_value,
    ):
        with pytest.raises(ValidationError):
            EventAttribute(key="status", value=value)
    for reference, description in (
        ("https://example.invalid/reports?token=synthetic-placeholder", None),
        ("vault://factory/provider", None),
        ("run-41", "Authorization: synthetic-placeholder-value"),
        ("run-41", synthetic_opaque_value),
    ):
        with pytest.raises(ValidationError):
            EvidenceRef(kind="artifact", reference=reference, description=description)


def test_facade_wraps_operations_without_backend_bound_method_leaks_and_binds_once():
    calls: list[str] = []

    class Backend:
        def claim(self, task_id: str, executor_id: str):
            calls.append("claim")
            return "claimed"

        def heartbeat(self, run):
            calls.append("heartbeat")
            return "lease"

        def append_event(self, run, event):
            calls.append("append")

        def finish(self, run, outcome):
            calls.append("finish")
            return "finished"

    backend = Backend()
    facade = build_agent_capabilities(
        backend.claim, backend.heartbeat, backend.append_event, backend.finish
    )
    for name in ("_claim", "_heartbeat", "_append_event", "_finish"):
        assert not hasattr(getattr(facade, name), "__self__")
    with pytest.raises(AttributeError):
        facade._claim = backend.claim  # type: ignore[misc]

    run = RunIdentity(task_id="task-41", run_id="run-41")
    foreign_event = FactoryEvent(
        event_type="heartbeat",
        run=RunIdentity(task_id="task-other", run_id="run-other"),
        occurred_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="run identity"):
        facade.append_event(run, foreign_event)
    assert calls == []

    own_event = FactoryEvent(
        event_type="heartbeat",
        run=run,
        occurred_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )
    facade.append_event(run, own_event)
    assert calls == ["append"]


def test_provider_definition_rejects_mismatched_declared_models():
    with pytest.raises(ValidationError):
        ProviderDefinition(kind="mistral", models=["openai:model-41"])
