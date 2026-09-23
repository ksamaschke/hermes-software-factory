"""Tests for the agent-safe repository boundary."""

from __future__ import annotations

import inspect
from typing import Protocol

from software_factory import TaskRepository


def test_task_repository_is_a_narrow_runtime_checkable_protocol():
    assert issubclass(TaskRepository, Protocol)
    for method in ("claim", "heartbeat", "append_event", "finish"):
        assert callable(getattr(TaskRepository, method))

    source = inspect.getsource(TaskRepository).lower()
    assert "sqlite" not in source
    assert "connection" not in source


def test_structural_repository_implementation_needs_only_typed_operations():
    class FakeRepository:
        def claim(self, task_id: str, executor_id: str):
            raise NotImplementedError

        def heartbeat(self, run):
            raise NotImplementedError

        def append_event(self, run, event):
            raise NotImplementedError

        def finish(self, run, outcome):
            raise NotImplementedError

    assert isinstance(FakeRepository(), TaskRepository)
