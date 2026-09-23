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

    The facade stores only private callable references. It deliberately accepts
    no backend, connection, transaction, or database object and offers no method
    other than the four operations in :class:`TaskRepository`.
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
        self._claim = claim
        self._heartbeat = heartbeat
        self._append_event = append_event
        self._finish = finish

    def claim(self, task_id: str, executor_id: str) -> ClaimedRun:
        return self._claim(task_id, executor_id)

    def heartbeat(self, run: RunIdentity) -> Lease:
        return self._heartbeat(run)

    def append_event(self, run: RunIdentity, event: FactoryEvent) -> None:
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
