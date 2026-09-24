"""Causal regressions for the second exact-head #42 trust-boundary review."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError
from software_factory.api.contracts import (
    FailedOutcome,
    Failure,
    RunIdentity,
    TaskState,
)
from software_factory.control.policy import (
    FactoryPolicy,
    ImmutableMapping,
    PolicyError,
    load_policy,
)
from software_factory.execution import (
    DispatchRequest,
    ExecutorBinding,
    PolicyExecutorRouter,
    RetryRoutingError,
    RoutingError,
)


def policy_document(
    *,
    canary: object = (),
    retry_compatibility: object = (),
    agent_id: str = "implementer-v1",
    route_agent: str | None = None,
) -> dict[str, object]:
    return {
        "version": 1,
        "agents": {
            agent_id: {
                "prompt": "runtime/prompts/implementer-v1.md",
                "output_contract": "ImplementationOutcome",
            }
        },
        "roles": {
            "implementer": {
                "executor": "pydantic_agent",
                "agent": route_agent or agent_id,
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
            "allow_backend_change_on_retry": "explicit_policy_only",
            "retry_compatibility": retry_compatibility,
        },
    }


def rich_policy_document(*, retry_compatibility: object = ()) -> dict[str, object]:
    document = policy_document(
        canary=[{"task_id": "task-42", "role": "implementer"}],
        retry_compatibility=retry_compatibility,
    )
    document["providers"] = {
        "primary": {
            "kind": "openai",
            "models": ["openai:model-42"],
        }
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


def retry_rule(**updates: object) -> dict[str, object]:
    rule = {
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
    rule.update(updates)
    return rule


def router_with_backend_change() -> PolicyExecutorRouter:
    return PolicyExecutorRouter(
        load_policy(rich_policy_document(retry_compatibility=[retry_rule()]))
    )


def terminal_state(binding: ExecutorBinding) -> TaskState:
    return TaskState(
        task_id=binding.task_id,
        state="failed",
        run=binding.run,
        outcome=FailedOutcome(
            failure=Failure(code="runner", summary="runner stopped"),
            next_gate="retry_policy",
        ),
    )


def test_hook_free_snapshot_rejects_subclasses_without_calling_hooks():
    calls: list[str] = []

    class HostileDispatchRequest(DispatchRequest):
        def model_dump(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("dispatch.model_dump")
            raise AssertionError("serializer invoked")

        def model_dump_json(self, *args: object, **kwargs: object) -> str:
            calls.append("dispatch.model_dump_json")
            raise AssertionError("serializer invoked")

        def model_copy(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("dispatch.model_copy")
            raise AssertionError("copy hook invoked")

    class HostileFactoryPolicy(FactoryPolicy):
        def model_dump(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("policy.model_dump")
            raise AssertionError("serializer invoked")

        def model_dump_json(self, *args: object, **kwargs: object) -> str:
            calls.append("policy.model_dump_json")
            raise AssertionError("serializer invoked")

        def model_copy(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("policy.model_copy")
            raise AssertionError("copy hook invoked")

    class HostileBinding(ExecutorBinding):
        def model_dump(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("binding.model_dump")
            raise AssertionError("serializer invoked")

        def model_dump_json(self, *args: object, **kwargs: object) -> str:
            calls.append("binding.model_dump_json")
            raise AssertionError("serializer invoked")

        def model_copy(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("binding.model_copy")
            raise AssertionError("copy hook invoked")

    class HostileTaskState(TaskState):
        def model_dump(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("state.model_dump")
            raise AssertionError("serializer invoked")

        def model_dump_json(self, *args: object, **kwargs: object) -> str:
            calls.append("state.model_dump_json")
            raise AssertionError("serializer invoked")

        def model_copy(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("state.model_copy")
            raise AssertionError("copy hook invoked")

    class HostileRunIdentity(RunIdentity):
        def model_dump(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("run.model_dump")
            raise AssertionError("serializer invoked")

        def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
            calls.append("run.model_dump_json")
            raise AssertionError("serializer invoked")

        def model_copy(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("run.model_copy")
            raise AssertionError("copy hook invoked")

    policy = load_policy(policy_document())
    hostile_policy = HostileFactoryPolicy.model_validate(
        policy.model_dump(mode="python")
    )
    router = PolicyExecutorRouter(policy)
    hostile_request = HostileDispatchRequest(
        task_id="task-42", run_id="run-1", role="implementer"
    )
    with pytest.raises(RoutingError):
        router.select(hostile_request)
    with pytest.raises(RoutingError):
        PolicyExecutorRouter(hostile_policy)

    binding = router.select(
        {"task_id": "task-42", "run_id": "run-1", "role": "implementer"}
    )
    hostile_binding = HostileBinding.model_validate(binding.model_dump(mode="python"))
    with pytest.raises(RoutingError):
        router.select(
            {"task_id": "task-42", "run_id": "run-1", "role": "implementer"},
            existing_binding=hostile_binding,
        )
    hostile_state = HostileTaskState.model_validate(
        terminal_state(binding).model_dump(mode="python")
    )
    with pytest.raises(RoutingError):
        router.decide_retry(
            binding,
            router.select(
                {
                    "task_id": "task-42",
                    "run_id": "run-2",
                    "role": "implementer",
                    "attempt": 2,
                }
            ),
            prior_state=hostile_state,
        )
    hostile_nested_state = TaskState.model_construct(
        task_id=binding.task_id,
        state="failed",
        run=HostileRunIdentity(
            task_id=binding.task_id,
            run_id=binding.run_id,
            executor_id=binding.executor_id,
            attempt=binding.attempt,
        ),
        outcome=terminal_state(binding).outcome,
    )
    with pytest.raises(RoutingError):
        router.decide_retry(
            binding,
            router.select(
                {
                    "task_id": "task-42",
                    "run_id": "run-2",
                    "role": "implementer",
                    "attempt": 2,
                }
            ),
            prior_state=hostile_nested_state,
        )
    assert calls == []


class HostileMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise AssertionError("hostile mapping was iterated")

    def __iter__(self):
        raise AssertionError("hostile mapping was iterated")

    def __len__(self) -> int:
        return 1


class HostileDict(dict[str, object]):
    def items(self):
        raise AssertionError("hostile dict was iterated")


def test_raw_mappings_and_json_duplicate_aliases_are_fail_closed():
    with pytest.raises(PolicyError, match="approved container"):
        load_policy(HostileMapping())
    with pytest.raises(PolicyError, match="approved container"):
        load_policy(HostileDict())
    with pytest.raises(PolicyError, match="mapping keys"):
        load_policy({1: "version"})
    with pytest.raises(PolicyError, match="duplicate key"):
        load_policy('{"version":1,"version":1}')

    duplicate_alias = policy_document(
        retry_compatibility=[
            {
                **retry_rule(),
                "source_id": "implementer",
            }
        ]
    )
    with pytest.raises(PolicyError, match="both 'source_id' and 'from_id'"):
        load_policy(duplicate_alias)

    router = PolicyExecutorRouter(load_policy(policy_document()))
    with pytest.raises(RoutingError, match="approved mapping"):
        router.select(HostileDict())


def test_known_profiles_validate_shape_before_hashing_and_equality():
    class TrapString(str):
        def __hash__(self) -> int:
            raise AssertionError("hash trap invoked")

        def __eq__(self, other: object) -> bool:
            raise AssertionError("equality trap invoked")

    class TrapList(list[str]):
        pass

    policy = load_policy(policy_document())
    with pytest.raises(RoutingError, match="built-in identifier"):
        PolicyExecutorRouter(policy, known_profiles=[TrapString("implementer")])
    with pytest.raises(RoutingError, match="exact list, tuple"):
        PolicyExecutorRouter(policy, known_profiles=TrapList(["implementer"]))
    with pytest.raises(RoutingError, match="duplicate"):
        PolicyExecutorRouter(policy, known_profiles=["implementer", "implementer"])
    with pytest.raises(RoutingError, match="mapping keys"):
        PolicyExecutorRouter(policy, known_profiles={1: object()})

    # Mapping registries are accepted only as exact string-keyed containers.
    router = PolicyExecutorRouter(
        policy, known_profiles=ImmutableMapping({"implementer": object()})
    )
    assert (
        router.select(
            {"task_id": "task-42", "run_id": "run-1", "role": "implementer"}
        ).profile
        == "implementer"
    )


def test_wildcard_route_ids_never_authorize_literal_or_pattern_routes():
    for field in (
        "from_id",
        "to_id",
        "from_provider",
        "to_provider",
        "from_model",
        "to_model",
        "from_vendor_family",
        "to_vendor_family",
        "rule_id",
    ):
        with pytest.raises(PolicyError, match="exact literal"):
            load_policy(
                rich_policy_document(
                    retry_compatibility=[retry_rule(**{field: "agent@(wildcard)"})]
                )
            )

    with pytest.raises(PolicyError, match="exact literal"):
        load_policy(
            policy_document(canary=[{"task_id": "task-[42]", "role": "implementer"}])
        )
    with pytest.raises(PolicyError, match="exact literal"):
        load_policy(policy_document(agent_id="*", route_agent="*"))

    assert load_policy(policy_document(agent_id="implementer-1")).roles[
        "implementer"
    ].agent == ("implementer-1")


def test_stale_or_forged_prior_bindings_cannot_authorize_retry():
    router = router_with_backend_change()
    prior = router.select(
        {
            "task_id": "task-42",
            "run_id": "run-1",
            "role": "implementer",
            "admitted": False,
        }
    )
    retry = router.select(
        {
            "task_id": "task-42",
            "run_id": "run-2",
            "role": "implementer",
            "attempt": 2,
        }
    )
    stale = prior.model_copy(update={"policy_fingerprint": "sha256:" + "0" * 64})
    with pytest.raises(RetryRoutingError, match="cross-policy"):
        router.decide_retry(stale, retry, prior_state=terminal_state(prior))

    forged_payload = prior.model_dump(mode="python")
    forged_payload["selection_reason"] = "canary_exact_match"
    forged = ExecutorBinding.model_construct(**forged_payload)
    with pytest.raises(RetryRoutingError, match="trusted validation"):
        router.decide_retry(forged, retry, prior_state=terminal_state(prior))


def test_direct_retry_decision_requires_current_complete_proof():
    router = router_with_backend_change()
    prior = router.select(
        {
            "task_id": "task-42",
            "run_id": "run-1",
            "role": "implementer",
            "admitted": False,
        }
    )
    retry = router.select(
        {
            "task_id": "task-42",
            "run_id": "run-2",
            "role": "implementer",
            "attempt": 2,
        }
    )
    decision = router.decide_retry(prior, retry, prior_state=terminal_state(prior))
    assert decision.compatibility_proof is not None
    assert router.validate_retry_decision(decision) == decision
    assert (
        router.validate_retry_decision(decision.model_dump(mode="python")) == decision
    )

    incomplete = decision.model_dump(mode="python")
    del incomplete["compatibility_proof"]["rule_id"]
    with pytest.raises(RoutingError):
        router.validate_retry_decision(incomplete)

    forged = decision.model_dump(mode="python")
    forged["compatibility_proof"]["to_id"] = "other-agent"
    with pytest.raises(RoutingError):
        router.validate_retry_decision(forged)

    same_router = PolicyExecutorRouter(load_policy(policy_document()))
    same_prior = same_router.select(
        {"task_id": "task-42", "run_id": "run-1", "role": "implementer"}
    )
    same_retry = same_router.select(
        {
            "task_id": "task-42",
            "run_id": "run-2",
            "role": "implementer",
            "attempt": 2,
        }
    )
    same = same_router.decide_retry(
        same_prior, same_retry, prior_state=terminal_state(same_prior)
    )
    same_payload = same.model_dump(mode="python")
    same_payload["compatibility_proof"] = {
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
        "policy_version": 1,
        "policy_fingerprint": same_router.policy_fingerprint,
        "rule_id": "literal-rule",
    }
    with pytest.raises(RoutingError):
        same_router.validate_retry_decision(same_payload)


def test_nested_run_alias_is_complete_and_does_not_drop_lease_or_conflicts():
    base = PolicyExecutorRouter(load_policy(policy_document())).select(
        {"task_id": "task-42", "run_id": "run-1", "role": "implementer"}
    )
    values = base.model_dump(mode="python")
    values.pop("task_id")
    values.pop("run_id")
    values.pop("attempt")
    values.pop("executor_id")
    values["run"] = {
        "task_id": "task-42",
        "run_id": "run-1",
        "attempt": 1,
        "executor_id": base.executor_id,
    }
    positive = ExecutorBinding.model_validate(values)
    assert positive.run == base.run

    with pytest.raises(ValidationError, match="lease_id"):
        ExecutorBinding.model_validate(
            {**values, "run": {**values["run"], "lease_id": "lease-1"}}
        )
    with pytest.raises(ValidationError, match="executor_id"):
        ExecutorBinding.model_validate({**values, "executor_id": "other-agent"})


def test_constructed_invalid_state_is_revalidated_and_terminal_identity_is_exact():
    router = PolicyExecutorRouter(load_policy(policy_document()))
    prior = router.select(
        {"task_id": "task-42", "run_id": "run-1", "role": "implementer"}
    )
    retry = router.select(
        {
            "task_id": "task-42",
            "run_id": "run-2",
            "role": "implementer",
            "attempt": 2,
        }
    )
    invalid = TaskState.model_construct(
        task_id="task-42", state="failed", run=prior.run
    )
    with pytest.raises(RoutingError):
        router.decide_retry(prior, retry, prior_state=invalid)

    foreign = terminal_state(prior).model_copy(
        update={"run": prior.run.model_copy(update={"executor_id": "other-agent"})}
    )
    with pytest.raises(RetryRoutingError, match="does not match"):
        router.decide_retry(prior, retry, prior_state=foreign)


def test_fingerprint_is_deterministic_and_source_mutation_cannot_retroactively_route():
    document = policy_document()
    router = PolicyExecutorRouter(load_policy(document))
    fingerprint = router.policy_fingerprint
    document["roles"]["implementer"]["agent"] = "other-agent"
    document["compatibility"]["fallback_executors"]["implementer"]["profile"] = "other"
    assert router.policy_fingerprint == fingerprint
    assert (
        router.select(
            {"task_id": "task-42", "run_id": "run-1", "role": "implementer"}
        ).profile
        == "implementer"
    )
    assert (
        json.loads(json.dumps({"fingerprint": fingerprint}))["fingerprint"]
        == fingerprint
    )
