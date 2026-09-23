"""Repository protocols and agent-safe capability facades."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ..api.contracts import (
    ClaimedRun,
    FactoryEvent,
    Lease,
    RunIdentity,
    TaskState,
    ValidatedOutcome,
)

ClaimOperation = Callable[[str, str], ClaimedRun]
HeartbeatOperation = Callable[[RunIdentity], Lease]
AppendEventOperation = Callable[[RunIdentity, FactoryEvent], None]
FinishOperation = Callable[[RunIdentity, ValidatedOutcome], TaskState]


def _wrap_operation(operation: Callable[..., object]) -> Callable[..., object]:
    """Adapt a trusted operation without storing its bound-method descriptor."""

    def invoke(*args: object, **kwargs: object) -> object:
        return operation(*args, **kwargs)

    return invoke


@runtime_checkable
class TaskRepository(Protocol):
    """Narrow task-store boundary; implementations remain outside agent code.

    The protocol intentionally exposes task operations only. It has no
    storage-specific handle, transaction, schema, or tracker-specific method, so
    an agent dependency can be constructed from this interface without
    granting access to the underlying state implementation.
    """

    def claim(self, task_id: str, executor_id: str) -> ClaimedRun:
        """Atomically claim one task and return its validated run envelope."""
        ...

    def heartbeat(self, run: RunIdentity) -> Lease:
        """Renew the controller-owned lease for a claimed run."""
        ...

    def append_event(self, run: RunIdentity, event: FactoryEvent) -> None:
        """Append one immutable run event."""
        ...

    def finish(self, run: RunIdentity, outcome: ValidatedOutcome) -> TaskState:
        """Submit a validated terminal proposal for controller readback."""
        ...


class TaskRepositoryFacade:
    """Agent-facing capability object built from exactly four operations.

    This is an API/capability-narrowing boundary, not a Python sandbox. The
    constructor receives trusted operation capabilities and keeps only wrapper
    functions, so a readable facade slot never exposes a bound method's
    ``__self__`` backend. Slots are write-protected after construction, and the
    facade accepts no backend, connection, transaction, or database object. The
    supplied operations still retain whatever authority their owner granted;
    callers must not pass an over-privileged callable.
    """

    __slots__ = ("_append_event", "_claim", "_finish", "_heartbeat")

    def __init__(
        self,
        claim: ClaimOperation,
        heartbeat: HeartbeatOperation,
        append_event: AppendEventOperation,
        finish: FinishOperation,
    ) -> None:
        operations = (claim, heartbeat, append_event, finish)
        if not all(callable(operation) for operation in operations):
            raise TypeError("all repository facade operations must be callable")
        object.__setattr__(self, "_claim", _wrap_operation(claim))
        object.__setattr__(self, "_heartbeat", _wrap_operation(heartbeat))
        object.__setattr__(self, "_append_event", _wrap_operation(append_event))
        object.__setattr__(self, "_finish", _wrap_operation(finish))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} operation bindings are immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} operation bindings are immutable")

    def claim(self, task_id: str, executor_id: str) -> ClaimedRun:
        return self._claim(task_id, executor_id)

    def heartbeat(self, run: RunIdentity) -> Lease:
        return self._heartbeat(run)

    def append_event(self, run: RunIdentity, event: FactoryEvent) -> None:
        if isinstance(event, FactoryEvent) and event.run != run:
            raise ValueError("factory event run identity must exactly match append run")
        self._append_event(run, event)

    def finish(self, run: RunIdentity, outcome: ValidatedOutcome) -> TaskState:
        return self._finish(run, outcome)


def build_agent_capabilities(
    claim: ClaimOperation,
    heartbeat: HeartbeatOperation,
    append_event: AppendEventOperation,
    finish: FinishOperation,
) -> TaskRepositoryFacade:
    """Construct the only repository capability set exposed to an agent."""

    return TaskRepositoryFacade(claim, heartbeat, append_event, finish)


# Explicit aliases make the boundary discoverable without adding capabilities.
AgentRepositoryFacade = TaskRepositoryFacade
AgentRepositoryCapabilities = TaskRepositoryFacade
AgentCapabilityFacade = TaskRepositoryFacade


__all__ = [
    "AgentCapabilityFacade",
    "AgentRepositoryCapabilities",
    "AgentRepositoryFacade",
    "AppendEventOperation",
    "ClaimOperation",
    "FinishOperation",
    "HeartbeatOperation",
    "TaskRepository",
    "TaskRepositoryFacade",
    "build_agent_capabilities",
]
