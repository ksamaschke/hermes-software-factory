"""Final exact-head #42 regressions for policy and routing boundaries."""

from __future__ import annotations

import copy
import logging
import threading
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from software_factory._safety import MAX_STRING_BYTES
from software_factory.api.contracts import (
    ClaimedRun,
    FailedOutcome,
    Failure,
    Lease,
    RunIdentity,
    TaskEnvelope,
    TaskRole,
    TaskState,
)
from software_factory.control.policy import (
    ImmutableMapping,
    PolicyError,
    load_policy,
    load_project_policy,
)
from software_factory.execution import (
    ExecutorBinding,
    PolicyExecutorRouter,
    RetryRoutingError,
    RoutingError,
)


def policy_document(*, retry_compatibility: object = ()) -> dict[str, object]:
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
            "canary_rules": [{"task_id": "task-42", "role": "implementer"}],
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
    document = policy_document(retry_compatibility=retry_compatibility)
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


def retry_rule() -> dict[str, object]:
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


def router(*, with_rule: bool = False) -> PolicyExecutorRouter:
    return PolicyExecutorRouter(
        load_policy(
            rich_policy_document(
                retry_compatibility=[retry_rule()] if with_rule else ()
            )
        )
    )


def request_payload() -> dict[str, object]:
    return {
        "task_id": "task-42",
        "run_id": "run-1",
        "role": "implementer",
    }


def envelope(*, task_id: str = "task-42", run_id: str = "run-1") -> TaskEnvelope:
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
            "workspace": {
                "workspace_id": "workspace-42",
                "root": "/tmp/workspace-42",
            },
            "base_revision": "base-42",
            "objective": "run the bounded implementation task",
            "acceptance": [{"id": "criterion", "description": "criterion"}],
            "deadline": datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
        }
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


def forge_model(model: Any, **updates: object) -> Any:
    """Make an exact model type with deliberately bypassed construction."""

    forged = object.__new__(type(model))
    raw_state = dict(object.__getattribute__(model, "__dict__"))
    raw_state.update(updates)
    object.__setattr__(forged, "__dict__", raw_state)
    return forged


class TrapTimezone(tzinfo):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def utcoffset(self, value: datetime | None):
        del value
        self.calls.append("utcoffset")
        raise AssertionError("hostile utcoffset hook executed")

    def dst(self, value: datetime | None):
        del value
        self.calls.append("dst")
        raise AssertionError("hostile dst hook executed")

    def tzname(self, value: datetime | None):
        del value
        self.calls.append("tzname")
        raise AssertionError("hostile tzname hook executed")


class UnsupportedPolicySource:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __str__(self) -> str:
        self.calls.append("str")
        raise AssertionError("source __str__ hook executed")

    def __repr__(self) -> str:
        self.calls.append("repr")
        raise AssertionError("source __repr__ hook executed")

    def __fspath__(self) -> str:
        self.calls.append("fspath")
        raise AssertionError("source __fspath__ hook executed")

    def __bytes__(self) -> bytes:
        self.calls.append("bytes")
        raise AssertionError("source __bytes__ hook executed")


def _assert_safe_policy_error(error: PolicyError, marker: str) -> None:
    assert marker not in str(error)
    assert marker not in repr(error)
    assert marker not in repr(error.args)
    assert error.__cause__ is None
    assert error.__context__ is None
    traceback = error.__traceback__
    while traceback is not None:
        module_name = traceback.tb_frame.f_globals.get("__name__")
        if module_name == "software_factory.control.policy":
            for value in traceback.tb_frame.f_locals.values():
                try:
                    rendered = repr(value)
                except BaseException:  # noqa: BLE001, S112 - inspect hostile traceback locals
                    continue
                assert marker not in rendered
        traceback = traceback.tb_next


def test_policy_errors_are_bounded_and_never_retain_caller_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "FINAL42_POLICY_SECRET_MARKER"
    cases: list[object] = [
        f"version: [\n  {marker}",
        copy.deepcopy(
            {
                **policy_document(),
                "roles": {
                    "implementer": {
                        "executor": "pydantic_agent",
                        "agent": marker,
                    }
                },
            }
        ),
        {
            **policy_document(),
            "credentials": {f"credential-{marker}": object()},
        },
        {
            **policy_document(),
            "runtime": {f"control-{marker}\x01": object()},
        },
        {**policy_document(), "roles": marker},
        {
            **policy_document(),
            "runtime": {"overlong": marker + ("x" * MAX_STRING_BYTES)},
        },
    ]
    caplog.set_level(logging.DEBUG)
    for loader in (load_policy, load_project_policy):
        for source in cases:
            with pytest.raises(PolicyError) as caught:
                loader(source)  # type: ignore[arg-type]
            _assert_safe_policy_error(caught.value, marker)
    assert marker not in caplog.text


def test_unsupported_policy_source_is_rejected_before_any_source_hook() -> None:
    source = UnsupportedPolicySource()
    with pytest.raises(PolicyError):
        load_policy(source)  # type: ignore[arg-type]
    assert source.calls == []


def test_forged_exact_datetime_is_rejected_without_timezone_hooks() -> None:
    selected = router()
    trap = TrapTimezone()
    forged = forge_model(
        envelope(),
        deadline=datetime(2026, 9, 24, 12, 0, tzinfo=trap),
    )
    with pytest.raises(RoutingError):
        selected.select(forged)
    assert trap.calls == []


def test_exact_fixed_offset_datetime_is_snapshotted_safely() -> None:
    fixed_offset = timezone(timedelta(hours=5, minutes=30))
    request = envelope().model_copy(
        update={"deadline": datetime(2026, 9, 24, 12, 0, tzinfo=fixed_offset)}
    )

    binding = router().select(request)

    assert binding.task_id == "task-42"


def test_forged_lifecycle_datetime_is_rejected_without_timezone_hooks() -> None:
    selected = router()
    prior = selected.select(request_payload())
    retry = selected.select({**request_payload(), "run_id": "run-2", "attempt": 2})
    run = prior.run
    trap = TrapTimezone()
    forged_claim = ClaimedRun(
        run=run,
        lease=Lease(
            run=run,
            expires_at=datetime(2026, 9, 24, 13, 0, tzinfo=UTC),
        ),
        envelope=envelope(),
    )
    forged_claim = forge_model(
        forged_claim,
        envelope=forge_model(
            forged_claim.envelope,
            deadline=datetime(2026, 9, 24, 12, 0, tzinfo=trap),
        ),
    )
    with pytest.raises(RetryRoutingError):
        selected.decide_retry(prior, retry, prior_state=forged_claim)
    assert trap.calls == []


def test_matching_authoritative_lease_reaches_active_run_denial() -> None:
    selected = router(with_rule=True)
    prior = selected.select({**request_payload(), "admitted": False})
    retry = selected.select({**request_payload(), "run_id": "run-2", "attempt": 2})
    run = RunIdentity(
        task_id=prior.task_id,
        run_id=prior.run_id,
        executor_id=prior.executor_id,
        attempt=prior.attempt,
        lease_id="lease-authoritative-42",
    )
    claimed = ClaimedRun(
        run=run,
        lease=Lease(
            run=run,
            expires_at=datetime(2026, 9, 24, 13, 0, tzinfo=UTC),
        ),
        envelope=envelope(),
    )
    for evidence in (
        claimed,
        TaskState(task_id=prior.task_id, state="running", run=run),
    ):
        decision = selected.decide_retry(prior, retry, prior_state=evidence)
        assert decision.allowed is False
        assert decision.reason.value == "active_run"


def test_mismatched_or_malformed_lifecycle_identity_fails_closed() -> None:
    selected = router(with_rule=True)
    prior = selected.select({**request_payload(), "admitted": False})
    retry = selected.select({**request_payload(), "run_id": "run-2", "attempt": 2})
    for run in (
        prior.run.model_copy(update={"run_id": "other-run"}),
        prior.run.model_copy(update={"executor_id": "other-executor"}),
        prior.run.model_copy(update={"attempt": 2}),
    ):
        evidence = TaskState(task_id=prior.task_id, state="running", run=run)
        with pytest.raises(RetryRoutingError):
            selected.decide_retry(prior, retry, prior_state=evidence)
    malformed = {
        "task_id": prior.task_id,
        "state": "running",
        "run": {
            "task_id": prior.task_id,
            "run_id": prior.run_id,
            "executor_id": prior.executor_id,
            "attempt": prior.attempt,
            "lease_id": object(),
        },
    }
    with pytest.raises((RetryRoutingError, RoutingError)):
        selected.decide_retry(prior, retry, prior_state=malformed)


def _run_with_mutator(operation, source: dict[str, object]) -> None:
    nested = source.setdefault("runtime", {})
    assert isinstance(nested, dict)
    for index in range(2_000):
        nested[f"seed-{index}"] = index
    stop = threading.Event()

    def mutate() -> None:
        index = 0
        while not stop.is_set():
            key = f"mutating-{index % 32}"
            nested[key] = index
            if index % 2:
                nested.pop(key, None)
            index += 1

    worker = threading.Thread(target=mutate)
    worker.start()
    try:
        for _ in range(12):
            try:
                operation(source)
            except (
                PolicyError,
                RetryRoutingError,
                RoutingError,
                ValidationError,
            ):
                pass
    finally:
        stop.set()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_mutating_exact_dicts_only_yield_coherent_or_safe_results() -> None:
    _run_with_mutator(load_policy, policy_document())
    _run_with_mutator(lambda value: ImmutableMapping(value), policy_document())

    selected = router()
    _run_with_mutator(selected.select, request_payload())
    binding = selected.select(request_payload()).model_dump(mode="python")
    _run_with_mutator(
        lambda value: ExecutorBinding.model_validate(value),
        binding,
    )
    lifecycle = terminal_state(selected.select(request_payload())).model_dump(
        mode="python"
    )
    _run_with_mutator(
        lambda value: selected.decide_retry(
            selected.select(request_payload()),
            selected.select({**request_payload(), "run_id": "run-2", "attempt": 2}),
            prior_state=value,
        ),
        lifecycle,
    )

    profiles = {"implementer": object()}
    for index in range(2_000):
        profiles[f"profile-{index}"] = object()
    stop = threading.Event()

    def mutate_profiles() -> None:
        index = 0
        while not stop.is_set():
            key = f"profile-mutating-{index % 32}"
            profiles[key] = object()
            if index % 2:
                profiles.pop(key, None)
            index += 1

    worker = threading.Thread(target=mutate_profiles)
    worker.start()
    try:
        for _ in range(12):
            try:
                PolicyExecutorRouter(
                    load_policy(policy_document()), known_profiles=profiles
                )
            except (RoutingError, PolicyError, ValidationError):
                pass
    finally:
        stop.set()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_pathlike_and_str_subclasses_are_not_policy_sources() -> None:
    class TrapString(str):
        def __str__(self) -> str:
            raise AssertionError("string subclass hook executed")

    class TrapPath(Path):
        _flavour = type(Path())._flavour

        def __fspath__(self) -> str:
            raise AssertionError("path subclass hook executed")

    for source in (TrapString("policy"), TrapPath("policy")):
        with pytest.raises(PolicyError):
            load_policy(source)  # type: ignore[arg-type]
