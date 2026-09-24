"""Third-round #42 routing and policy trust-boundary regressions."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from pydantic import ValidationError
from software_factory._safety import (
    MAX_CONTAINER_ITEMS,
    MAX_STRING_BYTES,
    MAX_TRAVERSAL_DEPTH,
)
from software_factory.api.contracts import FailedOutcome, Failure, TaskState
from software_factory.control.policy import (
    ImmutableMapping,
    PolicyError,
    load_policy,
)
from software_factory.execution import (
    DispatchRequest,
    ExecutorBinding,
    PolicyExecutorRouter,
    RetryDecisionKind,
    RetryDecisionReason,
    RetryRoutingError,
    RoutingError,
)


def policy_document(
    *, retry_compatibility: object = (), canary: object = ()
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
        "primary": {"kind": "openai", "models": ["openai:model-42"]}
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
    rule: dict[str, object] = {
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


def router(*, with_rule: bool = False) -> PolicyExecutorRouter:
    rule = [retry_rule()] if with_rule else ()
    return PolicyExecutorRouter(
        load_policy(rich_policy_document(retry_compatibility=rule))
    )


def backend_change_candidates(
    selected: PolicyExecutorRouter,
) -> tuple[ExecutorBinding, ExecutorBinding]:
    prior = selected.select(
        {
            "task_id": "task-42",
            "run_id": "run-1",
            "role": "implementer",
            "admitted": False,
        }
    )
    retry = selected.select(
        {
            "task_id": "task-42",
            "run_id": "run-2",
            "role": "implementer",
            "attempt": 2,
        }
    )
    return prior, retry


class HostileDict(dict[str, object]):
    """A dict subclass whose every normal observation is an alarm."""

    def __iter__(self):
        raise AssertionError("hostile dict iterated")

    def items(self):
        raise AssertionError("hostile dict items hook executed")

    def keys(self):
        raise AssertionError("hostile dict keys hook executed")

    def __len__(self) -> int:
        raise AssertionError("hostile dict len hook executed")


class TrapString(str):
    def __hash__(self) -> int:
        raise AssertionError("hash trap executed")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("equality trap executed")

    def __len__(self) -> int:
        raise AssertionError("len trap executed")


def hostile_proxy(payload: dict[str, object]) -> MappingProxyType:
    return MappingProxyType(HostileDict(payload))


def test_mapping_proxy_hostile_dict_is_rejected_at_every_authority_ingress():
    calls: list[str] = []
    policy_payload = policy_document()
    selected = router()
    prior, retry = backend_change_candidates(selected)
    binding_payload = prior.model_dump(mode="python")
    lifecycle_payload = terminal_state(prior).model_dump(mode="python")

    probes = (
        ("policy", lambda: load_policy(hostile_proxy(policy_payload))),
        (
            "request",
            lambda: selected.select(
                hostile_proxy(
                    {
                        "task_id": "task-42",
                        "run_id": "run-1",
                        "role": "implementer",
                    }
                )
            ),
        ),
        (
            "binding",
            lambda: ExecutorBinding.model_validate(hostile_proxy(binding_payload)),
        ),
        (
            "lifecycle",
            lambda: selected.decide_retry(
                prior, retry, prior_state=hostile_proxy(lifecycle_payload)
            ),
        ),
        (
            "known_profiles",
            lambda: PolicyExecutorRouter(
                load_policy(policy_document()),
                known_profiles=hostile_proxy({"implementer": object()}),
            ),
        ),
    )
    for label, probe in probes:
        with pytest.raises(
            (PolicyError, RoutingError, RetryRoutingError, ValidationError)
        ):
            probe()
        calls.append(label)
    assert calls == [
        "policy",
        "request",
        "binding",
        "lifecycle",
        "known_profiles",
    ]
    with pytest.raises(ValidationError):
        DispatchRequest.model_validate(
            hostile_proxy(
                {
                    "task_id": "task-42",
                    "run_id": "run-1",
                    "role": "implementer",
                }
            )
        )

    # Exact built-in dicts and the runtime's own immutable snapshots remain
    # valid positive controls; a proxy is not a trust signal.
    exact_policy = load_policy(policy_document())
    exact_router = PolicyExecutorRouter(
        exact_policy, known_profiles={"implementer": object()}
    )
    immutable_router = PolicyExecutorRouter(
        load_policy(ImmutableMapping(policy_document())),
        known_profiles=ImmutableMapping({"implementer": object()}),
    )
    assert (
        exact_router.select(
            {"task_id": "task-42", "run_id": "run-1", "role": "implementer"}
        ).role
        == "implementer"
    )
    assert immutable_router.policy.roles["implementer"].executor.value == (
        "pydantic_agent"
    )
    assert ExecutorBinding.model_validate(binding_payload) == prior
    assert (
        selected.decide_retry(prior, retry, prior_state=lifecycle_payload).reason
        is RetryDecisionReason.BACKEND_CHANGE_NOT_ALLOWED
    )


def _forged_immutable_mapping(payload: dict[str, object]) -> ImmutableMapping:
    """Build the exact-type forged states used by the trust-boundary regression."""

    forged = object.__new__(ImmutableMapping)
    hostile_data = MappingProxyType(HostileDict(payload))
    for field_name in ("_entries", "_data"):
        try:
            object.__setattr__(forged, field_name, hostile_data)
        except AttributeError:
            # Each representation intentionally exposes only one of these slots.
            pass
    return forged


def test_forged_immutable_mapping_state_is_rejected_before_hostile_hooks():
    forged = _forged_immutable_mapping(policy_document())
    with pytest.raises(PolicyError):
        load_policy(forged)

    nested_policy = policy_document()
    nested_policy["runtime"] = {"forged": forged}
    with pytest.raises(PolicyError):
        load_policy(nested_policy)

    selected = router()
    with pytest.raises(RoutingError):
        selected.select(forged)
    with pytest.raises(RoutingError):
        selected.select(
            {
                "task_id": "task-42",
                "run_id": "run-1",
                "role": "implementer",
                "opaque": {"forged": forged},
            }
        )


def test_immutable_mapping_forgery_shapes_fail_closed():
    uninitialized = object.__new__(ImmutableMapping)
    duplicate = object.__new__(ImmutableMapping)
    object.__setattr__(duplicate, "_entries", (("key", 1), ("key", 2)))
    cyclic = object.__new__(ImmutableMapping)
    object.__setattr__(cyclic, "_entries", (("self", cyclic),))
    hostile_value = object.__new__(ImmutableMapping)
    object.__setattr__(
        hostile_value,
        "_entries",
        (("value", HostileDict({"nested": "hostile"})),),
    )
    hostile_scalar = object.__new__(ImmutableMapping)
    object.__setattr__(
        hostile_scalar,
        "_entries",
        (("value", BindingTrapString("hostile")),),
    )
    hostile_integer = object.__new__(ImmutableMapping)
    object.__setattr__(
        hostile_integer,
        "_entries",
        (("value", BindingTrapInt(1)),),
    )

    class ForgedImmutableMapping(ImmutableMapping):
        def __iter__(self):
            raise AssertionError("subclass iterator executed")

    subclass = object.__new__(ForgedImmutableMapping)
    for candidate in (
        uninitialized,
        duplicate,
        cyclic,
        hostile_value,
        hostile_scalar,
        hostile_integer,
        subclass,
    ):
        with pytest.raises(PolicyError):
            load_policy(candidate)


def test_immutable_mapping_uses_exact_structural_snapshot_without_mutable_backing():
    source = policy_document()
    snapshot = ImmutableMapping(source)
    source["new_field"] = "must not appear"
    nested_source = {"nested": {"values": ["original"]}}
    nested_snapshot = ImmutableMapping(nested_source)
    nested_source["nested"]["values"].append("mutated")

    entries = object.__getattribute__(snapshot, "_entries")
    assert type(entries) is tuple
    assert all(type(entry) is tuple and len(entry) == 2 for entry in entries)
    assert "new_field" not in snapshot
    assert nested_snapshot["nested"]["values"] == ("original",)
    with pytest.raises((AttributeError, TypeError)):
        snapshot["version"] = 2  # type: ignore[index]
    with pytest.raises(AttributeError):
        object.__getattribute__(snapshot, "_data")


class BindingTrapString(str):
    def __eq__(self, other: object) -> bool:
        del other
        raise AssertionError("binding string equality trap executed")

    def __ne__(self, other: object) -> bool:
        del other
        raise AssertionError("binding string inequality trap executed")

    def __hash__(self) -> int:
        raise AssertionError("binding string hash trap executed")

    def __len__(self) -> int:
        raise AssertionError("binding string len trap executed")


class BindingTrapInt(int):
    def __eq__(self, other: object) -> bool:
        del other
        raise AssertionError("binding int equality trap executed")

    def __ne__(self, other: object) -> bool:
        del other
        raise AssertionError("binding int inequality trap executed")

    def __hash__(self) -> int:
        raise AssertionError("binding int hash trap executed")


def test_binding_run_alias_validates_exact_scalars_before_conflict_comparison():
    selected = router()
    binding = selected.select(
        {"task_id": "task-42", "run_id": "run-1", "role": "implementer"}
    )
    payload = binding.model_dump(mode="python")
    payload["run"] = {
        "task_id": binding.task_id,
        "run_id": binding.run_id,
        "executor_id": binding.executor_id,
        "attempt": binding.attempt,
    }

    assert ExecutorBinding.model_validate(payload) == binding
    conflicting = {**payload, "task_id": "other-task"}
    with pytest.raises(ValidationError, match="disagrees with run"):
        ExecutorBinding.model_validate(conflicting)

    top_level_traps = {
        "task_id": BindingTrapString(binding.task_id),
        "run_id": BindingTrapString(binding.run_id),
        "executor_id": BindingTrapString(binding.executor_id),
        "attempt": BindingTrapInt(binding.attempt),
    }
    for field_name, trap in top_level_traps.items():
        candidate = dict(payload)
        candidate[field_name] = trap
        with pytest.raises(ValidationError):
            ExecutorBinding.model_validate(candidate)

    for field_name, trap in top_level_traps.items():
        nested_run = dict(payload["run"])
        nested_run[field_name] = trap
        candidate = {**payload, "run": nested_run}
        with pytest.raises(ValidationError):
            ExecutorBinding.model_validate(candidate)


def test_route_lookup_validates_exact_identifier_before_any_hook_or_lookup():
    selected = router()
    trap = TrapString("task-42")

    probes = (
        lambda: selected.resolve_route(task_id=trap, role="implementer"),
        lambda: selected.resolve_route(
            task_id="task-42", role=TrapString("implementer")
        ),
        lambda: selected.select(task_id=trap, run_id="run-1", role="implementer"),
        lambda: selected.route(task_id=trap, run_id="run-1", role="implementer"),
        lambda: selected.select_executor(
            task_id=trap, run_id="run-1", role="implementer"
        ),
    )
    for probe in probes:
        with pytest.raises(RoutingError):
            probe()

    for bad_task in (None, 0, False, b"task-42", "task-*", ""):
        with pytest.raises(RoutingError):
            selected.resolve_route(task_id=bad_task, role="implementer")  # type: ignore[arg-type]
    for bad_role in (None, 0, False, b"implementer", "implementer*", ""):
        with pytest.raises(RoutingError):
            selected.resolve_route(task_id="task-42", role=bad_role)  # type: ignore[arg-type]

    route, reason = selected.resolve_route(task_id="task-42", role="implementer")
    assert route.executor.value == "pydantic_agent"
    assert reason.value == "canary_exact_match"


def test_policy_and_routing_graphs_reject_cycles_aliases_and_aggregate_overflow():
    deep: object = "leaf"
    for _ in range(MAX_TRAVERSAL_DEPTH + 2):
        deep = {"next": deep}
    deep_document = policy_document()
    deep_document["runtime"] = {"deep": deep}
    with pytest.raises(PolicyError, match="nesting|safety"):
        load_policy(deep_document)

    oversized = policy_document()
    oversized["runtime"] = {"items": [0] * (MAX_CONTAINER_ITEMS + 1)}
    with pytest.raises(PolicyError, match="container length|safety"):
        load_policy(oversized)

    large_string = policy_document()
    large_string["runtime"] = {"text": "x" * (MAX_STRING_BYTES + 1)}
    with pytest.raises(PolicyError, match="string-byte|safety"):
        load_policy(large_string)

    shared: dict[str, object] = {"leaf": "value"}
    aliased = policy_document()
    aliased["runtime"] = {"first": shared, "second": shared}
    with pytest.raises(PolicyError, match="repeated|cycle"):
        load_policy(aliased)

    cycle: list[object] = []
    cycle.append(cycle)
    cyclic = policy_document()
    cyclic["runtime"] = {"cycle": cycle}
    with pytest.raises(PolicyError, match="repeated|cycle"):
        load_policy(cyclic)

    yaml_cycle = """\
version: 1
runtime: &loop
  self: *loop
agents:
  implementer-v1:
    prompt: runtime/prompts/implementer-v1.md
    output_contract: ImplementationOutcome
roles:
  implementer:
    executor: pydantic_agent
    agent: implementer-v1
compatibility:
  fallback_executors:
    implementer:
      executor: hermes_profile
      profile: implementer
"""
    with pytest.raises(PolicyError, match="recursive|repeated|cycle|YAML"):
        load_policy(yaml_cycle)

    request_alias: dict[str, object] = {}
    request_alias["self"] = request_alias
    with pytest.raises(RoutingError, match="repeated|cycle|safety"):
        router().select(
            {
                "task_id": "task-42",
                "run_id": "run-1",
                "role": "implementer",
                "opaque": request_alias,
            }
        )


def test_recursive_failures_are_converted_to_safe_public_errors(monkeypatch):
    import software_factory.control.policy as policy_module
    import software_factory.execution.routing as routing_module

    selected = router()

    def policy_failure(*args: Any, **kwargs: Any) -> object:
        del args, kwargs
        raise RecursionError("synthetic recursion")

    monkeypatch.setattr(policy_module, "_json_safe_value", policy_failure)
    with pytest.raises(PolicyError, match="safety"):
        load_policy(policy_document())
    monkeypatch.undo()

    def routing_failure(*args: Any, **kwargs: Any) -> object:
        del args, kwargs
        raise MemoryError("synthetic memory")

    monkeypatch.setattr(routing_module, "_snapshot_value", routing_failure)
    with pytest.raises(RoutingError, match="safety"):
        selected.select(
            {"task_id": "task-42", "run_id": "run-1", "role": "implementer"}
        )


def test_retry_readback_recomputes_zero_one_and_multiple_rule_authority():
    zero = router()
    zero_prior, zero_retry = backend_change_candidates(zero)
    denied = zero.decide_retry(
        zero_prior, zero_retry, prior_state=terminal_state(zero_prior)
    )
    assert denied.allowed is False
    assert denied.decision is RetryDecisionKind.BACKEND_CHANGE_REJECTED
    assert denied.reason is RetryDecisionReason.BACKEND_CHANGE_NOT_ALLOWED
    assert zero.validate_retry_decision(denied) == denied

    one = router(with_rule=True)
    prior, retry = backend_change_candidates(one)
    allowed = one.decide_retry(prior, retry, prior_state=terminal_state(prior))
    assert allowed.allowed is True
    assert allowed.compatibility_proof is not None
    assert one.validate_retry_decision(allowed) == allowed

    active = one.decide_retry(
        prior,
        retry,
        prior_state=TaskState(task_id=prior.task_id, state="running", run=prior.run),
    )
    with pytest.raises(RetryRoutingError, match="lifecycle"):
        one.validate_retry_decision(active)

    forged_denial = allowed.model_dump(mode="python")
    forged_denial.update(
        {
            "allowed": False,
            "decision": "backend_change_rejected",
            "reason": "backend_change_not_allowed",
            "compatibility_proof": None,
        }
    )
    with pytest.raises(RetryRoutingError, match="authorized"):
        one.validate_retry_decision(forged_denial)

    forged_allowed = allowed.model_dump(mode="python")
    forged_allowed["compatibility_proof"]["rule_id"] = "forged-rule"
    with pytest.raises(RetryRoutingError, match="forged|canonical"):
        one.validate_retry_decision(forged_allowed)

    missing_proof = allowed.model_dump(mode="python")
    missing_proof["compatibility_proof"] = None
    with pytest.raises(RoutingError, match="trusted validation"):
        one.validate_retry_decision(missing_proof)

    rule = one.policy.compatibility.retry_compatibility[0]
    duplicate = type(rule).model_construct(
        **{**rule.model_dump(mode="python"), "rule_identity": "second-rule"}
    )
    object.__setattr__(
        one.policy.compatibility,
        "retry_compatibility",
        (rule, duplicate),
    )
    with pytest.raises(RetryRoutingError, match="exactly one"):
        one.validate_retry_decision(allowed)

    # The zero-rule denial remains valid only because no active rule matches;
    # an allowed change without that exact policy proof is never accepted.
    forged_zero = denied.model_dump(mode="python")
    forged_zero.update(
        {
            "allowed": True,
            "decision": "backend_change_allowed",
            "reason": "explicit_compatibility",
            "compatibility_proof": {
                **{
                    key: value
                    for key, value in retry_rule().items()
                    if key not in {"role"}
                },
                "role": "implementer",
                "policy_version": zero.policy.version,
                "policy_fingerprint": zero.policy_fingerprint,
                "rule_id": "forged-rule",
            },
        }
    )
    with pytest.raises(RetryRoutingError, match="no exact active policy rule"):
        zero.validate_retry_decision(forged_zero)
