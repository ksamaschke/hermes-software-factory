"""Contract validation and JSON serialization tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from software_factory import (
    ImplementationOutcome,
    RepositoryIdentity,
    ReviewOutcome,
    TaskEnvelope,
    TaskRole,
    WorkspaceIdentity,
)


def make_envelope(**overrides) -> TaskEnvelope:
    values = {
        "task_id": "task-41",
        "run_id": "run-1",
        "role": TaskRole.IMPLEMENTER,
        "repository": {
            "provider": "github",
            "project": "ksamaschke",
            "repository": "hermes-software-factory",
        },
        "workspace": {
            "workspace_id": "worktree-1",
            "root": "/tmp/factory-worktree-1",
        },
        "base_revision": "0c32430ab1243e060f21bab98c109e4f21d0a402",
        "objective": "Implement the typed runtime contracts",
        "acceptance": [
            {"id": "contracts", "description": "Contracts validate and serialize"}
        ],
        "deadline": datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return TaskEnvelope.model_validate(values)


def test_task_envelope_serializes_typed_nested_contracts():
    envelope = make_envelope()

    payload = json.loads(envelope.model_dump_json())

    assert payload["task_id"] == "task-41"
    assert payload["role"] == "implementer"
    assert payload["repository"]["repository"] == "hermes-software-factory"
    assert payload["deadline"].endswith("Z")


def test_task_envelope_rejects_escape_paths_and_naive_deadlines():
    with pytest.raises(ValidationError, match="must be relative"):
        make_envelope(constraints={"allowed_paths": ["../outside"]})

    with pytest.raises(ValidationError, match="timezone-aware"):
        make_envelope(deadline=datetime(2026, 9, 23, 12, 0))  # noqa: DTZ001


def test_implementation_terminal_statuses_are_consistent():
    ready = ImplementationOutcome(
        status="candidate_ready",
        summary="Candidate is ready for review",
        changed_paths=["runtime/src/software_factory/api/contracts.py"],
        candidate_revision="abc123",
        next_gate="independent_review",
    )
    assert ready.candidate_revision == "abc123"

    with pytest.raises(ValidationError, match="candidate_revision"):
        ImplementationOutcome(
            status="candidate_ready",
            summary="Missing revision",
            next_gate="independent_review",
        )


def test_review_mutation_cannot_be_approved():
    with pytest.raises(ValidationError, match="REVIEW_INCOMPLETE"):
        ReviewOutcome(
            verdict="APPROVED",
            candidate_revision="abc123",
            reviewed_scope=["runtime/src/software_factory/api/contracts.py"],
            mutation_detected=True,
        )


def test_repository_and_workspace_identities_are_provider_neutral():
    assert (
        RepositoryIdentity(
            provider="forgejo", project="owner", repository="repo"
        ).provider
        == "forgejo"
    )
    assert (
        WorkspaceIdentity(workspace_id="w1", root="/worktrees/w1").kind
        == "git_worktree"
    )
