"""Causal tests for the exact executor-selection boundary."""

from __future__ import annotations

import json

import pytest
from software_factory.api.contracts import FailedOutcome, Failure, TaskState
from software_factory.control.policy import FactoryPolicy, PolicyError, load_policy
from software_factory.execution import (
    DispatchRequest,
    ExecutorBinding,
    ExecutorSelectionError,
    PolicyExecutorRouter,
    RetryDecision,
    RetryDecisionKind,
    RetryDecisionReason,
    RetryRoutingError,
    RoutingError,
    SelectionReason,
)


def policy_document(
    *,
    canary: object = (),
    retry_compatibility: object = (),
    allow_backend_change: object = "explicit_policy_only",
) -> dict[str, object]:
    return {
        "version": 1,
        "agents": {
            "implementer-v1": {
                "prompt": "runtime/prompts/implementer-v1.md",
                "output_contract": "ImplementationOutcome",
            }
        },
        "roles": {
            "implementer": {
                "executor": "pydantic_agent",
                "agent": "implementer-v1",
            }
        },
        "compatibility": {
            "canary_rules": canary,
            "fallback_executors": {
                "implementer": {
                    "executor": "hermes_profile",
                    "profile": "implementer",
                }
            },
            "allow_backend_change_on_retry": allow_backend_change,
            "retry_compatibility": retry_compatibility,
        },
    }


def router(*, canary: object = (), **kwargs: object) -> PolicyExecutorRouter:
    return PolicyExecutorRouter(load_policy(policy_document(canary=canary, **kwargs)))


def rich_policy_document(
    *,
    canary: object = (),
    retry_compatibility: object = (),
    allow_backend_change: object = "explicit_policy_only",
) -> dict[str, object]:
    document = policy_document(
        canary=canary,
        retry_compatibility=retry_compatibility,
        allow_backend_change=allow_backend_change,
    )
    document["providers"] = {
        "primary": {
            "kind": "openai",
            "models": ["openai:model-42", "openai:alternate-model"],
        },
        "alternate": {
            "kind": "openai",
            "models": ["openai:alternate-model"],
        },
    }
    document["agents"]["other-agent"] = {
        "prompt": "runtime/prompts/other-agent.md",
        "output_contract": "ImplementationOutcome",
    }
    document["roles"]["implementer"].update(
        {
            "provider": "primary",
            "model": "openai:model-42",
            "vendor_family": "openai",
            "read_only_source": False,
        }
    )
    return document


def terminal_state(binding: ExecutorBinding, *, state: str = "failed") -> TaskState:
    return TaskState(
        task_id=binding.task_id,
        state=state,  # type: ignore[arg-type]
        run=binding.run,
        outcome=FailedOutcome(
            failure=Failure(code="runner", summary="runner stopped"),
            next_gate="retry_policy",
        ),
    )


def exact_retry_rule() -> dict[str, object]:
    return {
        "from_executor": "hermes_profile",
        "to_executor": "pydantic_agent",
        "from_id": "implementer",
        "to_id": "implementer-v1",
        "from_provider": None,
        "to_provider": "primary",
        "from_model": None,
        "to_model": "openai:model-42",
        "from_vendor_family": None,
        "to_vendor_family": "openai",
        "from_read_only_source": None,
        "to_read_only_source": False,
        "role": "implementer",
    }


def request(
    *,
    task_id: str = "task-42",
    run_id: str = "run-1",
    attempt: int = 1,
    admitted: bool = True,
) -> DispatchRequest:
    return DispatchRequest(
        task_id=task_id,
        run_id=run_id,
        role="implementer",
        attempt=attempt,
        admitted=admitted,
    )


def test_exact_canary_pair_selects_pydantic_and_every_other_task_falls_back():
    selected = router(canary=[{"task_id": "task-42", "role": "implementer"}])

    canary = selected.select(request())
    other_task = selected.select(request(task_id="task-43", run_id="run-2"))
    not_admitted = selected.select(request(run_id="run-3", admitted=False))

    assert canary.executor.value == "pydantic_agent"
    assert canary.agent == "implementer-v1"
    assert canary.selection_reason is SelectionReason.CANARY_EXACT_MATCH
    assert other_task.executor.value == "hermes_profile"
    assert other_task.profile == "implementer"
    assert other_task.selection_reason is SelectionReason.COMPATIBILITY_FALLBACK
    assert not_admitted.executor.value == "hermes_profile"


def test_canary_rules_are_exact_not_probabilistic_or_overlapping():
    with pytest.raises(PolicyError, match="overlapping"):
        load_policy(
            policy_document(
                canary=[
                    {"task_id": "task-42", "role": "implementer"},
                    {"task_id": "task-42", "role": "implementer"},
                ]
            )
        )

    with pytest.raises(PolicyError, match="exact"):
        load_policy(
            policy_document(canary=[{"task_id": "task-*", "role": "implementer"}])
        )


def test_malformed_pydantic_route_never_silently_falls_back():
    document = policy_document(canary=[{"task_id": "task-42", "role": "implementer"}])
    document["roles"] = {
        "implementer": {"executor": "pydantic_agent", "agent": "missing-agent"}
    }

    with pytest.raises(PolicyError, match="unknown agent"):
        load_policy(document)


def test_unknown_profile_fails_when_dispatcher_profile_registry_is_bound():
    with pytest.raises(RoutingError, match="unknown Hermes profile"):
        router().__class__(load_policy(policy_document()), known_profiles={"reviewer"})


def test_reselecting_a_bound_active_run_cannot_change_its_executor():
    hermes_router = router()
    prior = hermes_router.select(request())
    pydantic_router = router(canary=[{"task_id": "task-42", "role": "implementer"}])

    with pytest.raises(ExecutorSelectionError, match="cannot change executor"):
        pydantic_router.select(request(), existing_binding=prior)

    same = hermes_router.select(request(), existing_binding=prior)
    assert same == prior
    assert same.policy_fingerprint == prior.policy_fingerprint


@pytest.mark.parametrize(
    "terminal_state",
    ["success", "failure", "timeout", "cancellation", "stale_lease", "crash"],
)
def test_terminal_lifecycle_paths_preserve_the_bound_route(terminal_state: str):
    del (
        terminal_state
    )  # The lifecycle owner records this state; routing only re-reads it.
    selected = router()
    bound = selected.select(request())

    reread = selected.select(request(), existing_binding=bound)

    assert reread == bound
    assert reread.backend_key() == bound.backend_key()


def test_same_backend_retry_is_new_identity_and_does_not_mutate_prior_evidence():
    selected = router()
    prior = selected.select(request())
    retry = selected.select(request(run_id="run-2", attempt=2))
    prior_payload = prior.model_dump(mode="json")

    decision = selected.decide_retry(prior, retry, prior_state=terminal_state(prior))

    assert decision.allowed is True
    assert decision.decision is RetryDecisionKind.SAME_BACKEND
    assert decision.reason is RetryDecisionReason.SAME_BACKEND
    assert decision.retry_run.run_id == "run-2"
    assert prior.model_dump(mode="json") == prior_payload


def test_backend_change_on_retry_is_rejected_without_explicit_policy():
    prior = router().select(request())
    selected = router(canary=[{"task_id": "task-42", "role": "implementer"}])
    retry = selected.select(request(run_id="run-2", attempt=2))

    with pytest.raises(RetryRoutingError, match="cross-policy"):
        selected.decide_retry(prior, retry, prior_state=terminal_state(prior))


def test_backend_change_requires_concrete_comparison_and_explicit_compatibility():
    compatibility = [
        {
            "from_executor": "hermes_profile",
            "to_executor": "pydantic_agent",
            "from_id": "implementer",
            "to_id": "implementer-v1",
            "from_provider": None,
            "to_provider": None,
            "from_model": None,
            "to_model": None,
            "from_vendor_family": None,
            "to_vendor_family": None,
            "from_read_only_source": None,
            "to_read_only_source": None,
            "role": "implementer",
        }
    ]
    selected = router(
        canary=[{"task_id": "task-42", "role": "implementer"}],
        retry_compatibility=compatibility,
    )
    prior = selected.select(request(admitted=False))
    retry = selected.select(request(run_id="run-2", attempt=2))

    decision = selected.decide_retry(prior, retry, prior_state=terminal_state(prior))

    assert decision.allowed is True
    assert decision.decision is RetryDecisionKind.BACKEND_CHANGE_ALLOWED
    assert decision.reason is RetryDecisionReason.EXPLICIT_COMPATIBILITY
    assert decision.prior_selection == prior
    assert decision.new_selection == retry

    wrong_identity = retry.model_copy(
        update={"executor_id": "another-agent", "agent": "another-agent"}
    )
    with pytest.raises(RetryRoutingError):
        selected.decide_retry(prior, wrong_identity, prior_state=terminal_state(prior))


def test_active_run_cannot_be_replaced_by_a_retry_even_when_policy_allows_change():
    compatibility = [
        {
            "from_executor": "hermes_profile",
            "to_executor": "pydantic_agent",
            "from_id": "implementer",
            "to_id": "implementer-v1",
            "from_provider": None,
            "to_provider": None,
            "from_model": None,
            "to_model": None,
            "from_vendor_family": None,
            "to_vendor_family": None,
            "from_read_only_source": None,
            "to_read_only_source": None,
            "role": "implementer",
        }
    ]
    selected = router(
        canary=[{"task_id": "task-42", "role": "implementer"}],
        retry_compatibility=compatibility,
    )
    prior = selected.select(request(admitted=False))
    retry = selected.select(request(run_id="run-2", attempt=2))
    running = TaskState(task_id=prior.task_id, state="running", run=prior.run)

    decision = selected.decide_retry(prior, retry, prior_state=running)

    assert decision.allowed is False
    assert decision.reason is RetryDecisionReason.ACTIVE_RUN


def test_retry_same_run_id_is_rejected_before_backend_policy_is_considered():
    selected = router()
    prior = selected.select(request())
    same_run = selected.select(request(attempt=2))

    with pytest.raises(ValueError, match="distinct run_id"):
        selected.decide_retry(prior, same_run)


def test_selection_is_a_small_json_readback_record_without_opaque_policy_data():
    selected = router(canary=[{"task_id": "task-42", "role": "implementer"}])
    binding = selected.select(request())

    payload = json.loads(binding.model_dump_json())

    assert payload["role"] == "implementer"
    assert payload["executor"] == "pydantic_agent"
    assert payload["agent"] == "implementer-v1"
    assert payload["selection_reason"] == "canary_exact_match"
    assert payload["policy_fingerprint"].startswith("sha256:")
    assert "credentials" not in payload
    assert "runtime" not in payload


def test_retry_compatibility_requires_the_complete_backend_identity():
    with pytest.raises(PolicyError, match="from_provider"):
        load_policy(
            policy_document(
                canary=[{"task_id": "task-42", "role": "implementer"}],
                retry_compatibility=[
                    {
                        "from_executor": "hermes_profile",
                        "to_executor": "pydantic_agent",
                        "from_id": "implementer",
                        "to_id": "implementer-v1",
                        "role": "implementer",
                    }
                ],
            )
        )

    selected = PolicyExecutorRouter(
        load_policy(
            rich_policy_document(
                canary=[{"task_id": "task-42", "role": "implementer"}],
                retry_compatibility=[exact_retry_rule()],
            )
        )
    )
    prior = selected.select(request(admitted=False))
    retry = selected.select(request(run_id="run-2", attempt=2))
    evidence = terminal_state(prior)

    decision = selected.decide_retry(prior, retry, prior_state=evidence)
    assert decision.allowed is True
    assert decision.decision is RetryDecisionKind.BACKEND_CHANGE_ALLOWED

    for update in (
        {"agent": "other-agent", "executor_id": "other-agent"},
        {"provider": "alternate", "model": "openai:alternate-model"},
        {"model": "openai:alternate-model"},
        {"vendor_family": "other-vendor"},
        {"read_only_source": True},
    ):
        changed = retry.model_copy(update=update)
        with pytest.raises(RetryRoutingError):
            selected.decide_retry(prior, changed, prior_state=evidence)

    arbitrary = router(
        canary=[{"task_id": "task-42", "role": "implementer"}],
        allow_backend_change=True,
    )
    arbitrary_prior = arbitrary.select(request(admitted=False))
    arbitrary_retry = arbitrary.select(request(run_id="run-2", attempt=2))
    arbitrary_decision = arbitrary.decide_retry(
        arbitrary_prior,
        arbitrary_retry,
        prior_state=terminal_state(arbitrary_prior),
        allow_backend_change=True,
    )
    assert arbitrary_decision.allowed is False


def test_controller_revalidates_forged_policy_and_binding_snapshots():
    valid_policy = load_policy(
        policy_document(canary=[{"task_id": "task-42", "role": "implementer"}])
    )
    forged_policy_payload = valid_policy.model_dump(mode="python")
    forged_policy_payload["roles"]["implementer"]["agent"] = "unregistered-agent"
    forged_policy = FactoryPolicy.model_construct(**forged_policy_payload)

    with pytest.raises(RoutingError):
        PolicyExecutorRouter(forged_policy)

    selected = PolicyExecutorRouter(valid_policy)
    binding = selected.select(request())
    forged_binding_payload = binding.model_dump(mode="python")
    forged_binding_payload["selection_reason"] = "compatibility_fallback"
    forged_binding = ExecutorBinding.model_construct(**forged_binding_payload)
    with pytest.raises(ExecutorSelectionError):
        selected.select(request(), existing_binding=forged_binding)

    with pytest.raises(ValueError, match="fallback reason"):
        binding.model_copy(update={"selection_reason": "compatibility_fallback"})

    source = policy_document(canary=[{"task_id": "task-42", "role": "implementer"}])
    isolated = PolicyExecutorRouter(load_policy(source))
    source["roles"]["implementer"]["agent"] = "unregistered-agent"
    source["compatibility"]["canary_rules"].clear()
    assert isolated.select(request()).agent == "implementer-v1"


def test_canary_admission_requires_a_builtin_bool():
    selected = router(canary=[{"task_id": "task-42", "role": "implementer"}])
    assert (
        selected.select(request(admitted=False)).selection_reason
        is SelectionReason.COMPATIBILITY_FALLBACK
    )

    class TruthyString(str):
        pass

    for admitted in (0, "false", TruthyString("false")):
        forged_request = DispatchRequest.model_construct(
            task_id="task-42",
            run_id="run-forged",
            role="implementer",
            attempt=1,
            admitted=admitted,
        )
        with pytest.raises(RoutingError):
            selected.select(forged_request)

    for admitted in (0, "false", TruthyString("false")):
        with pytest.raises(RoutingError):
            selected.resolve_route(
                task_id="task-42",
                role="implementer",
                admitted=admitted,  # type: ignore[arg-type]
            )


def test_retry_requires_authoritative_terminal_lifecycle_evidence():
    selected = router()
    prior = selected.select(request())
    retry = selected.select(request(run_id="run-2", attempt=2))

    with pytest.raises(RetryRoutingError, match="terminal lifecycle"):
        selected.decide_retry(prior, retry)
    with pytest.raises(RetryRoutingError, match="terminal lifecycle"):
        selected.decide_retry(prior, retry, prior_active=False)
    with pytest.raises(RetryRoutingError, match="terminal lifecycle"):
        selected.decide_retry(prior, retry, prior_state=prior.run)

    running = TaskState(task_id=prior.task_id, state="running", run=prior.run)
    active_decision = selected.decide_retry(prior, retry, prior_state=running)
    assert active_decision.allowed is False
    assert active_decision.reason is RetryDecisionReason.ACTIVE_RUN

    terminal = terminal_state(prior)
    terminal_decision = selected.decide_retry(prior, retry, prior_state=terminal)
    assert terminal_decision.allowed is True
    assert terminal_decision.decision is RetryDecisionKind.SAME_BACKEND

    mismatched = TaskState(
        task_id="task-other",
        state="failed",
        run=prior.run.model_copy(update={"task_id": "task-other"}),
        outcome=FailedOutcome(
            failure=Failure(code="runner", summary="runner stopped"),
            next_gate="retry_policy",
        ),
    )
    with pytest.raises(RetryRoutingError, match="lifecycle evidence"):
        selected.decide_retry(prior, retry, prior_state=mismatched)


def test_retry_decision_serialized_validation_rejects_role_and_backend_corruption():
    selected = router()
    prior = selected.select(request())
    retry = selected.select(request(run_id="run-2", attempt=2))
    decision = selected.decide_retry(prior, retry, prior_state=terminal_state(prior))

    role_corruption = decision.model_dump(mode="json")
    role_corruption["new_selection"]["role"] = "reviewer"
    with pytest.raises(ValueError, match="role"):
        RetryDecision.model_validate(role_corruption)

    backend_corruption = decision.model_dump(mode="json")
    backend_corruption["new_selection"]["executor"] = "hermes_profile"
    backend_corruption["new_selection"]["executor_id"] = "other-profile"
    backend_corruption["new_selection"]["agent"] = None
    backend_corruption["new_selection"]["profile"] = "other-profile"
    backend_corruption["new_selection"]["selection_reason"] = "compatibility_fallback"
    backend_corruption["retry_run"]["executor_id"] = "other-profile"
    with pytest.raises(ValueError, match="same-backend"):
        RetryDecision.model_validate(backend_corruption)
