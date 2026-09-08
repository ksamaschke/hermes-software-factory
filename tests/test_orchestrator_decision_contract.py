"""Behavioral tests for the generic typed orchestrator decision contract."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "local-variant" / "orchestrator_decision_contract.py"
spec = importlib.util.spec_from_file_location(
    "orchestrator_decision_contract", MODULE_PATH
)
assert spec is not None and spec.loader is not None
contract = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contract
spec.loader.exec_module(contract)


class SyntheticDecisionModel:
    """Offline model double that uses only typed prompt/tool observations."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, tools: dict[str, Any]) -> dict[str, Any]:
        marker = "CONTEXT_JSON\n"
        context = json.loads(prompt.split(marker, 1)[1].split("\nEND_CONTEXT", 1)[0])
        live = tools["read_live_state"]()
        parent = tools["read_parent_completion"]()
        source = tools["read_source_state"]()
        ready = tools["read_ready_lanes"]()
        capabilities = tools["read_capabilities"]()
        key = context["idempotency_key"]

        self.calls.append(
            {
                "source_item": context["source_item"]["item_key"],
                "profile_name": context["profile_name"],
                "execution_mode": context["execution_mode"],
            }
        )

        blocker = live.get("blocker", {})
        existing = live.get("existing_action") or {}
        if (
            source.get("artifact_state") == "failed"
            and source.get("source_state") == "merged"
        ):
            action = "repair_artifact"
            next_phase = "artifact"
            target = source.get("artifact_task_id")
        elif ready:
            action = "select_independent_lane"
            next_phase = "implementation"
            target = ready[0]["task_id"]
        elif capabilities.get("missing"):
            action = "hold_missing_capability"
            next_phase = context["phase"]
            target = None
        elif (
            existing.get("status") == "blocked"
            and existing.get("current_run_id") is None
        ):
            action = "reuse_existing"
            next_phase = context["phase"]
            target = existing.get("task_id")
        elif (
            blocker.get("occurrences", 0) >= 3
            and not blocker.get("resolved")
            and blocker.get("fingerprint") == blocker.get("previous_fingerprint")
        ):
            action = "quarantine"
            next_phase = context["phase"]
            target = None
        elif blocker.get("resolved") or (
            blocker.get("fingerprint")
            and blocker.get("fingerprint") != blocker.get("previous_fingerprint")
        ):
            action = "admit"
            next_phase = "implementation"
            target = context["current_task"]["id"]
        else:
            action = "hold"
            next_phase = context["phase"]
            target = None

        tools["propose_action"](action, key, target)
        readback = tools["read_action_readback"](key)
        return {
            "diagnose": {
                "cause": action,
                "source_state": source.get("source_state"),
                "parent_state": parent["state"],
            },
            "choose": {
                "action": action,
                "target_task_id": target,
                "independent_lane": bool(ready),
            },
            "act": {
                "action": action,
                "idempotency_key": key,
                "target_task_id": target,
            },
            "read_back": readback,
            "advance": {
                "next_phase": next_phase,
                "status": readback["status"],
            },
        }


def _context(item_key: str, state: dict[str, Any]):
    source = contract.SourceIdentity(
        tracker="synthetic-tracker",
        project="synthetic-project",
        item_key=item_key,
        kind="issue",
    )
    live_state = state.get("live", {})
    source_fixture = state.get("source", {})
    phase = state.get("phase", live_state.get("phase", "triage"))
    blocker = state.get("blocker", live_state.get("blocker", {}))
    execution = contract.ExecutionIdentity(
        mode="scheduled",
        profile_name="orchestrator-profile",
        task_id=f"task-{item_key}",
        run_id=state.get("run_id"),
    )
    evidence = contract.EvidenceBundle(
        scheduler=(
            contract.TypedEvidence(
                kind="scheduler",
                subject=f"tick-{item_key}",
                status="observed",
                reference=f"scheduler-ref-{item_key}",
            ),
        ),
        worker=(
            contract.TypedEvidence(
                kind="worker",
                subject=execution.task_id,
                status="not_started" if execution.run_id is None else "running",
                reference=f"worker-ref-{item_key}",
                run_id=execution.run_id,
            ),
        ),
        source=(
            contract.TypedEvidence(
                kind="source",
                subject=source.canonical_key,
                status=state.get(
                    "source_state", source_fixture.get("source_state", "open")
                ),
                reference=f"source-ref-{item_key}",
                attributes={
                    "artifact_state": source_fixture.get("artifact_state", "ready")
                },
            ),
        ),
        review=(
            contract.TypedEvidence(
                kind="review",
                subject=source.canonical_key,
                status=state.get("review_state", "approved"),
                reference=f"review-ref-{item_key}",
                candidate=state.get("candidate"),
            ),
        ),
    )
    context = contract.DecisionContext(
        execution=execution,
        source_item=source,
        phase=phase,
        input_identity=contract.build_input_identity(
            source,
            phase,
            {"fixture": "synthetic", "case": item_key},
        ),
        blocker=contract.BlockerState(
            fingerprint=blocker.get("fingerprint"),
            previous_fingerprint=blocker.get("previous_fingerprint"),
            occurrences=blocker.get("occurrences", 0),
            resolved=blocker.get("resolved", False),
        ),
        parent_completion=contract.ParentCompletion(
            state=state.get("parent_state", "complete"),
            verified=state.get("parent_verified", True),
            parent_ids=tuple(state.get("parent_ids", ())),
        ),
        evidence=evidence,
        policy=contract.DecisionPolicy(
            max_prompt_chars=5000,
            max_skill_chars=300,
            max_skills=2,
            repeated_blocker_threshold=3,
        ),
    )
    return context


def _fixture_state(context, **overrides: Any) -> dict[str, Any]:
    source_evidence = context.evidence.source[0]
    artifact_state = source_evidence.attributes.get("artifact_state", "ready")
    key = contract.action_idempotency_key(context)
    state: dict[str, Any] = {
        "live": {
            "blocker": {
                "fingerprint": context.blocker.fingerprint,
                "previous_fingerprint": context.blocker.previous_fingerprint,
                "occurrences": context.blocker.occurrences,
                "resolved": context.blocker.resolved,
            },
            "existing_action": None,
            "current_run_id": context.execution.run_id,
            "source_key": context.source_item.canonical_key,
            "phase": context.phase,
            "input_identity": context.input_identity,
            "semantic_lane": context.semantic_lane,
            "branch": context.execution.branch,
            "tenant": context.execution.tenant,
            "singleton_key": context.execution.singleton_key,
            "credentials_verified": context.execution.credentials_verified,
            "production": context.execution.production,
            "production_approved": context.execution.production_approved,
            "retry_count": context.execution.retry_count,
        },
        "parent": context.parent_completion.as_dict(),
        "source": {
            "source_key": context.source_item.canonical_key,
            "phase": context.phase,
            "input_identity": context.input_identity,
            "semantic_lane": context.semantic_lane,
            "current_run_id": context.execution.run_id,
            "source_state": source_evidence.status,
            "artifact_state": artifact_state,
        },
        "ready": [],
        "capabilities": {"missing": []},
        "readbacks": {
            key: {
                "status": "held",
                "idempotency_key": key,
                "current_run_id": None,
            }
        },
    }

    def merge(base: dict[str, Any], update: dict[str, Any]) -> None:
        for name, value in update.items():
            if isinstance(base.get(name), dict) and isinstance(value, dict):
                merge(base[name], value)
            else:
                base[name] = value

    merge(state, overrides)
    existing = state["live"].get("existing_action")
    if isinstance(existing, dict):
        bound = {
            "source_key": context.source_item.canonical_key,
            "phase": context.phase,
            "input_identity": context.input_identity,
            "semantic_lane": context.semantic_lane,
            "blocker_fingerprint": context.blocker.fingerprint,
            "current_run_id": context.execution.run_id,
        }
        bound.update(existing)
        state["live"]["existing_action"] = bound
    return state


def test_context_separates_mode_profile_and_preserves_typed_evidence():
    context = _context(
        "unseen-identity-a",
        {
            "blocker": {
                "fingerprint": "capability:runner",
                "previous_fingerprint": "capability:runner",
                "occurrences": 3,
            }
        },
    )

    document = context.as_dict()
    assert document["schema"] == contract.CONTRACT_SCHEMA
    assert document["execution_mode"] == "scheduled"
    assert document["profile_name"] == "orchestrator-profile"
    assert document["execution_mode"] != document["profile_name"]
    assert document["current_task"]["id"] == "task-unseen-identity-a"
    assert document["current_run"] is None
    assert document["source_item"]["item_key"] == "unseen-identity-a"
    assert document["phase"] == "triage"
    assert document["input_identity"]
    assert document["blocker"]["fingerprint"] == "capability:runner"
    assert document["parent_completion"]["verified"] is True
    assert set(document["evidence"]) == {"scheduler", "worker", "source", "review"}
    assert all(document["evidence"][kind] for kind in document["evidence"])


def test_prompt_and_skill_budgets_are_effective_and_conflicts_fail_closed():
    context = _context(
        "unseen-identity-b",
        {"blocker": {"fingerprint": "new-contract", "previous_fingerprint": "old"}},
    )
    envelope = contract.prepare_prompt(
        context,
        [
            ("large-role", "role instruction " * 500),
            ("second-role", "secondary instruction " * 500),
            ("ignored-role", "should not be selected " * 500),
        ],
    )
    assert envelope.prompt_chars <= context.policy.max_prompt_chars
    assert envelope.skill_chars <= context.policy.max_skill_chars
    assert len(envelope.selected_skills) <= context.policy.max_skills
    assert envelope.shrunk or envelope.omitted_skills

    conflicting_worker = contract.TypedEvidence(
        kind="worker",
        subject=context.execution.task_id,
        status="running",
        reference="worker-conflict",
        run_id="historical-run",
    )
    conflicting = replace(
        context,
        execution=replace(context.execution, run_id="current-run"),
        evidence=replace(context.evidence, worker=(conflicting_worker,)),
    )
    with pytest.raises(contract.ContractViolation, match="conflict"):
        contract.validate_context(conflicting)


def test_action_key_binds_semantic_lane_and_task_identity():
    context = _context("key-bindings", {})
    lane_variant = replace(context, semantic_lane="other-lane")
    task_variant = replace(
        context,
        execution=replace(context.execution, task_id="other-task"),
    )

    assert contract.action_idempotency_key(context) != contract.action_idempotency_key(
        lane_variant
    )
    assert contract.action_idempotency_key(context) != contract.action_idempotency_key(
        task_variant
    )


def test_secret_private_path_and_resource_bounds_fail_closed():
    redacted = contract._redact_text(
        "Authorization: Basic SENSITIVE ssh://user:password@example.invalid "
        "path=/opt/private/file"
    )
    assert "SENSITIVE" not in redacted
    assert "password" not in redacted
    assert "/opt/private/file" not in redacted

    safe = contract._safe_value(
        {"private_path=/srv/secret.txt": "SENSITIVE", "token": "SENSITIVE"}
    )
    encoded = json.dumps(safe)
    assert "SENSITIVE" not in encoded
    assert "/srv/secret.txt" not in encoded

    with pytest.raises(contract.ContractViolation, match="text value"):
        contract._safe_value("x" * (contract._MAX_SAFE_TEXT_CHARS + 1))
    with pytest.raises(contract.ContractViolation, match="skill input"):
        contract.prepare_prompt(
            _context("too-many-skills", {}),
            [
                (f"skill-{index}", "safe")
                for index in range(contract._MAX_INPUT_ITEMS + 1)
            ],
        )
    with pytest.raises(contract.ContractViolation, match="tool input"):
        contract.prepare_prompt(
            _context("too-many-tools", {}),
            tool_catalog=[
                f"tool-{index}" for index in range(contract._MAX_INPUT_ITEMS + 1)
            ],
        )
    with pytest.raises(contract.ContractViolation, match="trace exceeds"):
        contract._validate_observation_trace(
            [{"tool": "read_live_state"}] * (contract._MAX_NATIVE_TRACE_ENTRIES + 1)
        )


def test_model_fixture_path_makes_safe_decisions_for_unseen_ids_without_writes():
    cases = [
        (
            "unseen-case-quarantine",
            {
                "blocker": {
                    "fingerprint": "provider:capacity",
                    "previous_fingerprint": "provider:capacity",
                    "occurrences": 3,
                }
            },
            "quarantine",
        ),
        (
            "unseen-case-admit",
            {
                "blocker": {
                    "fingerprint": "contract:v2",
                    "previous_fingerprint": "contract:v1",
                    "resolved": True,
                },
                "readbacks": {},
            },
            "admit",
        ),
        (
            "unseen-case-reuse",
            {
                "blocker": {
                    "fingerprint": "existing:blocked",
                    "previous_fingerprint": "other:blocker",
                    "occurrences": 1,
                },
                "live": {
                    "blocker": {},
                    "existing_action": {
                        "status": "blocked",
                        "task_id": "existing-card",
                        "current_run_id": None,
                    },
                },
                "readbacks": {},
            },
            "reuse_existing",
        ),
        (
            "unseen-case-independent",
            {
                "blocker": {
                    "fingerprint": "signer:held",
                    "previous_fingerprint": "signer:old",
                },
                "ready": [{"task_id": "independent-ready"}],
                "live": {"blocker": {"fingerprint": "signer:held"}},
            },
            "select_independent_lane",
        ),
        (
            "unseen-case-artifact",
            {
                "source": {
                    "source_state": "merged",
                    "artifact_state": "failed",
                    "artifact_task_id": "artifact-repair",
                },
            },
            "repair_artifact",
        ),
        (
            "unseen-case-capability",
            {"capabilities": {"missing": ["required-tool"]}},
            "hold_missing_capability",
        ),
    ]

    for item_key, overrides, expected_action in cases:
        context = _context(item_key, overrides)
        key = contract.action_idempotency_key(context)
        if expected_action == "admit":
            overrides.setdefault("readbacks", {})[key] = {
                "status": "admitted",
                "idempotency_key": key,
                "current_run_id": f"run-preview-{item_key}",
                "admission_count": 1,
            }
        elif expected_action == "reuse_existing":
            overrides.setdefault("readbacks", {})[key] = {
                "status": "reused",
                "idempotency_key": key,
                "current_run_id": None,
                "admission_count": 0,
            }
        elif expected_action == "quarantine":
            overrides.setdefault("readbacks", {})[key] = {
                "status": "quarantined",
                "idempotency_key": key,
                "current_run_id": None,
                "admission_count": 0,
            }
        elif expected_action == "select_independent_lane":
            overrides.setdefault("readbacks", {})[key] = {
                "status": "selected",
                "idempotency_key": key,
                "current_run_id": None,
                "selected_task_id": "independent-ready",
            }
        elif expected_action == "repair_artifact":
            overrides.setdefault("readbacks", {})[key] = {
                "status": "artifact-remediation",
                "idempotency_key": key,
                "current_run_id": None,
            }
        elif expected_action == "hold_missing_capability":
            overrides.setdefault("readbacks", {})[key] = {
                "status": "held",
                "idempotency_key": key,
                "current_run_id": None,
            }

        fixture = _fixture_state(context, **overrides)
        adapter = contract.NoSideEffectFixtureAdapter(fixture)
        before = adapter.snapshot()
        model = SyntheticDecisionModel()
        result = contract.evaluate_decision(
            model,
            context,
            adapter,
            skills=[("contract", "short bounded role contract"), ("extra", "extra")],
        )

        assert result.action == expected_action
        assert result.readback["idempotency_key"] == key
        assert adapter.snapshot() == before
        assert adapter.mutation_attempts == 0
        assert model.calls[-1]["source_item"] == item_key
        assert result.steps == contract.DECISION_LADDER

    assert len(cases) == 6


def test_reused_and_current_run_null_are_not_claimed_as_new_execution():
    context = _context(
        "unseen-case-null-run",
        {
            "blocker": {
                "fingerprint": "reuse:blocked",
                "previous_fingerprint": "reuse:old",
                "occurrences": 1,
            },
            "live": {
                "blocker": {},
                "existing_action": {
                    "status": "blocked",
                    "task_id": "reused-card",
                    "current_run_id": None,
                },
            },
        },
    )
    key = contract.action_idempotency_key(context)
    adapter = contract.NoSideEffectFixtureAdapter(
        _fixture_state(
            context,
            live={
                "blocker": {},
                "existing_action": {
                    "status": "blocked",
                    "task_id": "reused-card",
                    "current_run_id": None,
                },
            },
            readbacks={
                key: {
                    "status": "reused",
                    "idempotency_key": key,
                    "current_run_id": None,
                    "admission_count": 0,
                }
            },
        )
    )
    result = contract.evaluate_decision(
        SyntheticDecisionModel(),
        context,
        adapter,
    )
    assert result.action == "reuse_existing"
    assert result.readback["status"] == "reused"
    assert result.readback["current_run_id"] is None
    assert result.new_current_run is False


def test_controller_does_not_auto_propose_a_response_without_a_model_commit():
    context = _context(
        "causal-no-proposal",
        {
            "blocker": {
                "fingerprint": "provider:capacity",
                "previous_fingerprint": "provider:capacity",
                "occurrences": 3,
            }
        },
    )
    adapter = contract.NoSideEffectFixtureAdapter(_fixture_state(context))

    class ResponseOnlyModel:
        def complete(self, prompt: str, tools: dict[str, Any]) -> dict[str, Any]:
            context_json = json.loads(
                prompt.split("CONTEXT_JSON\n", 1)[1].split("\nEND_CONTEXT", 1)[0]
            )
            for name in (
                "read_live_state",
                "read_parent_completion",
                "read_source_state",
                "read_ready_lanes",
                "read_capabilities",
            ):
                tools[name]()
            key = context_json["idempotency_key"]
            return {
                "diagnose": {"summary": "observed"},
                "choose": {"action": "quarantine"},
                "act": {"action": "quarantine", "idempotency_key": key},
                "read_back": {
                    "idempotency_key": key,
                    "status": "not_started",
                    "current_run_id": None,
                },
                "advance": {"next_phase": context_json["phase"]},
            }

    with pytest.raises(
        contract.ContractViolation, match="controller will not auto-propose"
    ):
        contract.evaluate_decision(ResponseOnlyModel(), context, adapter)
    assert adapter.proposal is None
    assert adapter.proposal_attempts == 0


def test_contract_surface_is_generic_and_does_not_embed_private_identifiers():
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "docs" / "orchestrator-soul-template.md",
            ROOT / "docs" / "profile-roles.md",
            ROOT / "docs" / "profile-environment-contract.md",
            ROOT / "examples" / "project-policy.yaml",
            MODULE_PATH,
        )
    ).lower()
    for forbidden in ("/home/", "192.168.", "sustainical", "notion", "slack"):
        assert forbidden not in text
    assert "raw logs" not in text
    assert "current_run" in text
    assert "blocker" in text
    assert "idempot" in text
    assert "execution_mode" in text
    assert "profile_name" in text


def test_source_canonical_identity_is_injective_for_delimiter_values():
    first = contract.SourceIdentity(
        tracker="tracker:one",
        project="two",
        kind="issue",
        item_key="42",
    )
    second = contract.SourceIdentity(
        tracker="tracker",
        project="one:two",
        kind="issue",
        item_key="42",
    )

    assert first.canonical_key != second.canonical_key
    assert first.canonical_key == first.canonical_key


def test_policy_rejects_duplicate_transition_actions():
    policy = contract.DecisionPolicy(
        transition_policy=(
            ("hold", "current_phase"),
            ("hold", "artifact"),
        )
    )

    with pytest.raises(contract.ContractViolation, match="transitions are duplicated"):
        policy.as_dict()


def test_failed_merged_artifact_requires_a_bound_repair_task():
    context = _context(
        "artifact-without-task",
        {"source": {"source_state": "merged", "artifact_state": "failed"}},
    )
    key = contract.action_idempotency_key(context)
    adapter = contract.NoSideEffectFixtureAdapter(
        _fixture_state(
            context,
            source={"source_state": "merged", "artifact_state": "failed"},
            readbacks={
                key: {
                    "status": "artifact-remediation",
                    "idempotency_key": key,
                    "current_run_id": None,
                }
            },
        )
    )

    with pytest.raises(contract.ContractViolation, match="repair task identity"):
        contract.evaluate_decision(SyntheticDecisionModel(), context, adapter)


def test_admission_rejects_a_foreign_target_task():
    context = _context(
        "foreign-admit-target",
        {
            "blocker": {
                "fingerprint": "contract:v2",
                "previous_fingerprint": "contract:v1",
                "resolved": True,
            }
        },
    )
    adapter = contract.NoSideEffectFixtureAdapter(_fixture_state(context))

    class ForeignTargetModel:
        def complete(self, prompt: str, tools: dict[str, Any]) -> dict[str, Any]:
            context_json = json.loads(
                prompt.split("CONTEXT_JSON\n", 1)[1].split("\nEND_CONTEXT", 1)[0]
            )
            for name in (
                "read_live_state",
                "read_parent_completion",
                "read_source_state",
                "read_ready_lanes",
                "read_capabilities",
            ):
                tools[name]()
            key = context_json["idempotency_key"]
            tools["propose_action"]("admit", key, "foreign-task")
            readback = tools["read_action_readback"](key)
            return {
                "diagnose": {"summary": "resolved"},
                "choose": {"action": "admit", "target_task_id": "foreign-task"},
                "act": {
                    "action": "admit",
                    "idempotency_key": key,
                    "target_task_id": "foreign-task",
                },
                "read_back": readback,
                "advance": {"next_phase": "implementation"},
            }

    with pytest.raises(
        contract.ContractViolation, match="target is not bound to the current task"
    ):
        contract.evaluate_decision(ForeignTargetModel(), context, adapter)
