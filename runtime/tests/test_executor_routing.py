"""Causal tests for the exact executor-selection boundary."""

from __future__ import annotations

import json

import pytest
from software_factory.control.policy import PolicyError, load_policy
from software_factory.execution import (
    DispatchRequest,
    ExecutorSelectionError,
    PolicyExecutorRouter,
    RetryDecisionKind,
    RetryDecisionReason,
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

    decision = selected.decide_retry(prior, retry)

    assert decision.allowed is True
    assert decision.decision is RetryDecisionKind.SAME_BACKEND
    assert decision.reason is RetryDecisionReason.SAME_BACKEND
    assert decision.retry_run.run_id == "run-2"
    assert prior.model_dump(mode="json") == prior_payload


def test_backend_change_on_retry_is_rejected_without_explicit_policy():
    prior = router().select(request())
    retry = router(canary=[{"task_id": "task-42", "role": "implementer"}]).select(
        request(run_id="run-2", attempt=2)
    )

    decision = router().decide_retry(prior, retry)

    assert decision.allowed is False
    assert decision.decision is RetryDecisionKind.BACKEND_CHANGE_REJECTED
    assert decision.reason is RetryDecisionReason.BACKEND_CHANGE_NOT_ALLOWED
    assert decision.new_selection == retry


def test_backend_change_requires_concrete_comparison_and_explicit_compatibility():
    compatibility = [
        {
            "from_executor": "hermes_profile",
            "to_executor": "pydantic_agent",
            "from_id": "implementer",
            "to_id": "implementer-v1",
            "role": "implementer",
        }
    ]
    prior = router().select(request())
    selected = router(
        canary=[{"task_id": "task-42", "role": "implementer"}],
        retry_compatibility=compatibility,
    )
    retry = selected.select(request(run_id="run-2", attempt=2))

    decision = selected.decide_retry(prior, retry)

    assert decision.allowed is True
    assert decision.decision is RetryDecisionKind.BACKEND_CHANGE_ALLOWED
    assert decision.reason is RetryDecisionReason.EXPLICIT_COMPATIBILITY
    assert decision.prior_selection is prior
    assert decision.new_selection is retry

    wrong_identity = retry.model_copy(
        update={"executor_id": "another-agent", "agent": "another-agent"}
    )
    wrong_decision = selected.decide_retry(prior, wrong_identity)
    assert wrong_decision.allowed is False
    assert wrong_decision.reason is RetryDecisionReason.BACKEND_CHANGE_NOT_ALLOWED


def test_active_run_cannot_be_replaced_by_a_retry_even_when_policy_allows_change():
    compatibility = [
        {"from_executor": "hermes_profile", "to_executor": "pydantic_agent"}
    ]
    prior = router().select(request())
    selected = router(
        canary=[{"task_id": "task-42", "role": "implementer"}],
        retry_compatibility=compatibility,
    )
    retry = selected.select(request(run_id="run-2", attempt=2))

    decision = selected.decide_retry(prior, retry, prior_active=True)

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
