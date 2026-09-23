"""Repository protocol exposed to agents and control-plane executors."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..api.contracts import (
    ClaimedRun,
    FactoryEvent,
    Lease,
    RunIdentity,
    TaskState,
    ValidatedOutcome,
)


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


__all__ = ["TaskRepository"]
