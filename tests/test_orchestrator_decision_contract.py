"""Behavioral tests for the generic typed orchestrator decision contract."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator, Mapping
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
            existing.get("status") in {"blocked", "completed"}
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

        key = tools["read_action_key"](action, target)["idempotency_key"]
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
        credentials_verified=True,
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
    key = contract.action_idempotency_key(context, "hold", None)
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


def test_admission_requires_explicit_credential_verification_evidence():
    context = _context(
        "omitted-credential-verification",
        {
            "blocker": {
                "fingerprint": "contract:v2",
                "previous_fingerprint": "contract:v1",
                "resolved": True,
            }
        },
    )
    execution = contract.ExecutionIdentity(
        mode=context.execution.mode,
        profile_name=context.execution.profile_name,
        task_id=context.execution.task_id,
    )
    assert execution.credentials_verified is False
    context = replace(context, execution=execution)
    state = _fixture_state(context)

    with pytest.raises(
        contract.ContractViolation, match="admission requires verified credentials"
    ):
        contract._validate_admission_guards(
            context,
            state["live"],
            state["source"],
        )


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

    current_target = context.execution.task_id
    current_key = contract.action_idempotency_key(context, "admit", current_target)
    lane_key = contract.action_idempotency_key(
        lane_variant, "admit", lane_variant.execution.task_id
    )
    task_key = contract.action_idempotency_key(
        task_variant, "admit", task_variant.execution.task_id
    )
    assert current_key != lane_key
    assert current_key != task_key
    assert current_key != contract.action_idempotency_key(context, "quarantine", None)
    assert current_key != contract.action_idempotency_key(
        context, "admit", "other-task"
    )

    delimiter_first = replace(context, phase="a\x1fb", input_identity="c")
    delimiter_second = replace(context, phase="a", input_identity="b\x1fc")
    assert contract.action_idempotency_key(
        delimiter_first, "hold", None
    ) != contract.action_idempotency_key(delimiter_second, "hold", None)


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
    with pytest.raises(contract.ContractViolation, match="bounded mapping or sequence"):
        contract.prepare_prompt(
            _context("iterator-skills", {}),
            skills=((f"skill-{index}", "safe") for index in range(3)),
        )
    with pytest.raises(contract.ContractViolation, match="bounded mapping or sequence"):
        contract.prepare_prompt(
            _context("iterator-tools", {}),
            tool_catalog=(f"tool-{index}" for index in range(3)),
        )
    shared = {"value": "safe"}
    with pytest.raises(contract.ContractViolation, match="shared"):
        contract._safe_value({"first": shared, "second": shared})
    with pytest.raises(contract.ContractViolation, match="trace exceeds"):
        contract._validate_observation_trace(
            [{"tool": "read_live_state"}] * (contract._MAX_NATIVE_TRACE_ENTRIES + 1)
        )


def test_fixture_adapter_bounds_hostile_mapping_before_materialization():
    class HostileMapping(Mapping[str, Any]):
        def __init__(self, item_count: int) -> None:
            self.item_count = item_count
            self.yielded = 0
            self.lookups = 0

        def __getitem__(self, key: str) -> Any:
            self.lookups += 1
            return key

        def __iter__(self) -> Iterator[str]:
            for index in range(self.item_count):
                self.yielded += 1
                yield f"key-{index}"

        def __len__(self) -> int:
            return self.item_count

    state = HostileMapping(contract._MAX_SAFE_VALUE_ITEMS * 4)
    with pytest.raises(contract.ContractViolation, match="exceeds the contract bound"):
        contract.NoSideEffectFixtureAdapter(state)
    expected_consumption = contract._MAX_SAFE_VALUE_ITEMS + 1
    assert state.yielded == expected_consumption
    assert state.lookups == expected_consumption


@pytest.mark.parametrize(
    "ingress", ["typed_evidence", "proposal", "trace_entry", "trace_receipt"]
)
def test_all_untrusted_mapping_ingresses_are_bounded_before_materialization(ingress):
    class HostileMapping(Mapping[str, Any]):
        def __init__(self) -> None:
            self.yielded = 0
            self.lookups = 0

        def __getitem__(self, key: str) -> Any:
            self.lookups += 1
            return key

        def __iter__(self) -> Iterator[str]:
            for index in range(contract._MAX_SAFE_VALUE_ITEMS + 4):
                self.yielded += 1
                yield f"key-{index}"

        def __len__(self) -> int:
            return contract._MAX_SAFE_VALUE_ITEMS + 4

    hostile = HostileMapping()
    if ingress == "typed_evidence":
        operation = lambda: contract.TypedEvidence(
            kind="scheduler",
            subject="subject",
            status="observed",
            reference="reference",
            attributes=hostile,
        ).as_dict()
    elif ingress == "proposal":
        operation = lambda: contract.DecisionProposal.from_response(hostile)
    elif ingress == "trace_entry":
        operation = lambda: contract._validate_observation_trace([hostile])
    else:
        key = "fixture-key"
        trace = [
            {"tool": name}
            for name in (
                "read_live_state",
                "read_parent_completion",
                "read_source_state",
                "read_ready_lanes",
                "read_capabilities",
            )
        ]
        trace.extend(
            [
                {
                    "tool": "read_action_key",
                    "action": "hold",
                    "target_task_id": None,
                    "idempotency_key": key,
                },
                {
                    "tool": "propose_action",
                    "action": "hold",
                    "idempotency_key": key,
                    "target_task_id": None,
                },
                {
                    "tool": "read_action_readback",
                    "idempotency_key": key,
                    "receipt": hostile,
                },
            ]
        )
        operation = lambda: contract._validate_observation_trace(trace)

    with pytest.raises(contract.ContractViolation):
        operation()
    assert hostile.yielded <= contract._MAX_SAFE_VALUE_ITEMS + 1
    assert hostile.lookups <= contract._MAX_SAFE_VALUE_ITEMS + 1


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
        target_by_action = {
            "admit": context.execution.task_id,
            "reuse_existing": "existing-card",
            "select_independent_lane": "independent-ready",
            "repair_artifact": "artifact-repair",
        }
        key = contract.action_idempotency_key(
            context, expected_action, target_by_action.get(expected_action)
        )
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
    key = contract.action_idempotency_key(context, "hold", None)
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


def test_identity_bound_completed_action_is_reused_without_admission():
    context = _context(
        "completed-terminal",
        {
            "blocker": {
                "fingerprint": "contract:v2",
                "previous_fingerprint": "contract:v1",
                "resolved": True,
            },
            "live": {
                "blocker": {},
                "existing_action": {
                    "status": "completed",
                    "task_id": "completed-card",
                    "current_run_id": None,
                },
            },
        },
    )
    adapter = contract.NoSideEffectFixtureAdapter(
        _fixture_state(
            context,
            live={
                "blocker": {},
                "existing_action": {
                    "status": "completed",
                    "task_id": "completed-card",
                    "current_run_id": None,
                },
            },
        )
    )

    result = contract.evaluate_decision(SyntheticDecisionModel(), context, adapter)

    assert result.action == "reuse_existing"
    assert result.readback["current_run_id"] is None
    assert result.readback["admission_count"] == 0
    assert adapter.postproposal_receipt_reads == 1


def test_completed_action_rejects_admit_before_fixture_readback_allocation():
    context = _context(
        "completed-admit-attack",
        {
            "blocker": {
                "fingerprint": "contract:v2",
                "previous_fingerprint": "contract:v1",
                "resolved": True,
            },
            "live": {
                "blocker": {},
                "existing_action": {
                    "status": "completed",
                    "task_id": "completed-card",
                    "current_run_id": None,
                },
            },
        },
    )
    adapter = contract.NoSideEffectFixtureAdapter(
        _fixture_state(
            context,
            live={
                "blocker": {},
                "existing_action": {
                    "status": "completed",
                    "task_id": "completed-card",
                    "current_run_id": None,
                },
            },
        )
    )

    class AdmitModel:
        def complete(self, prompt: str, tools: dict[str, Any]) -> dict[str, Any]:
            del prompt
            for name in (
                "read_live_state",
                "read_parent_completion",
                "read_source_state",
                "read_ready_lanes",
                "read_capabilities",
            ):
                tools[name]()
            key = tools["read_action_key"]("admit", context.execution.task_id)[
                "idempotency_key"
            ]
            tools["propose_action"]("admit", key, context.execution.task_id)
            raise AssertionError("completed action reached an impossible readback")

    with pytest.raises(
        contract.ContractViolation, match="completed existing action is terminal"
    ):
        contract.evaluate_decision(AdmitModel(), context, adapter)
    assert adapter.proposal is None
    assert adapter.proposal_attempts == 1
    assert adapter.postproposal_receipt_reads == 0


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
            key = context_json["decision_identity_key"]
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


def test_action_identity_includes_full_execution_binding():
    context = _context("execution-bound", {})
    variants = (
        replace(context, execution=replace(context.execution, run_id="run-b")),
        replace(context, execution=replace(context.execution, branch="branch-b")),
        replace(context, execution=replace(context.execution, tenant="tenant-b")),
        replace(
            context,
            execution=replace(context.execution, profile_name="profile-b"),
        ),
        replace(context, execution=replace(context.execution, mode="interactive")),
    )
    keys = {
        contract.action_idempotency_key(item, "hold") for item in (context, *variants)
    }
    assert len(keys) == 6


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
    key = contract.action_idempotency_key(context, "hold", None)
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
            for name in (
                "read_live_state",
                "read_parent_completion",
                "read_source_state",
                "read_ready_lanes",
                "read_capabilities",
            ):
                tools[name]()
            key = tools["read_action_key"]("admit", "foreign-task")["idempotency_key"]
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


def test_admission_rejects_an_omitted_target_task():
    context = _context(
        "omitted-admit-target",
        {
            "blocker": {
                "fingerprint": "contract:v2",
                "previous_fingerprint": "contract:v1",
                "resolved": True,
            }
        },
    )
    adapter = contract.NoSideEffectFixtureAdapter(_fixture_state(context))

    class OmittedTargetModel:
        def complete(self, prompt: str, tools: dict[str, Any]) -> dict[str, Any]:
            for name in (
                "read_live_state",
                "read_parent_completion",
                "read_source_state",
                "read_ready_lanes",
                "read_capabilities",
            ):
                tools[name]()
            key = tools["read_action_key"]("admit", None)["idempotency_key"]
            tools["propose_action"]("admit", key, None)
            readback = tools["read_action_readback"](key)
            return {
                "diagnose": {"summary": "resolved"},
                "choose": {"action": "admit"},
                "act": {"action": "admit", "idempotency_key": key},
                "read_back": readback,
                "advance": {"next_phase": "implementation"},
            }

    with pytest.raises(contract.ContractViolation, match="admit target"):
        contract.evaluate_decision(OmittedTargetModel(), context, adapter)


def test_running_action_without_a_current_run_fails_closed():
    context = _context(
        "malformed-running-action",
        {
            "blocker": {
                "fingerprint": "contract:v2",
                "previous_fingerprint": "contract:v1",
                "resolved": True,
            }
        },
    )
    adapter = contract.NoSideEffectFixtureAdapter(
        _fixture_state(
            context,
            live={
                "existing_action": {
                    "status": "running",
                    "task_id": "running-card",
                    "current_run_id": None,
                }
            },
        )
    )
    with pytest.raises(contract.ContractViolation, match="running action"):
        contract.evaluate_decision(SyntheticDecisionModel(), context, adapter)


def test_compound_secret_keys_are_redacted_case_insensitively():
    safe = contract._safe_value(
        {
            "secretValue": "secret-one",
            "passwordHash": "secret-two",
            "tokenValue": "secret-three",
            "API_KEY": "secret-four",
            "mySecretValue": "secret-five",
            "credentials_verified": "safe-status",
        }
    )
    for key in (
        "secretValue",
        "passwordHash",
        "tokenValue",
        "API_KEY",
        "mySecretValue",
    ):
        assert safe[key] == "[REDACTED]"
    assert safe["credentials_verified"] == "[REDACTED]"
    assert (
        contract._safe_value({"credentials_verified": True})["credentials_verified"]
        is True
    )
    encoded = contract._redact_text(
        '{"apiKey":"API-SECRET","passwordHash":"HASH-SECRET"}'
    )
    assert "API-SECRET" not in encoded
    assert "HASH-SECRET" not in encoded
    assert "[REDACTED]" in encoded


def test_context_and_lying_sequences_are_bounded_without_len_trust():
    class LyingSequence(list):
        def __len__(self):
            return 1

    with pytest.raises(contract.ContractViolation, match="skill input"):
        contract.prepare_prompt(
            _context("lying-skills", {}),
            LyingSequence([(f"skill-{index}", "safe") for index in range(1_025)]),
        )
    oversized = replace(
        _context("oversized-context", {}),
        prior_decision={f"field-{index}": "x" * 80 for index in range(1_024)},
    )
    with pytest.raises(contract.ContractViolation, match="context"):
        contract.validate_context(oversized)
    policy = replace(
        oversized.policy,
        allowed_execution_modes=LyingSequence(
            ["scheduled"] + [f"mode-{index}" for index in range(100)]
        ),
    )
    with pytest.raises(contract.ContractViolation, match="execution_modes"):
        contract.validate_context(replace(oversized, policy=policy))
    with pytest.raises(
        contract.ContractViolation, match="parent_completion.parent_ids"
    ):
        contract.ParentCompletion(
            state="complete",
            verified=True,
            parent_ids=LyingSequence(
                [f"parent-{index}" for index in range(contract._MAX_PARENT_IDS + 1)]
            ),
        ).as_dict()
    evidence = contract.TypedEvidence(
        kind="scheduler",
        subject="subject",
        status="observed",
        reference="reference",
    )
    with pytest.raises(contract.ContractViolation, match="evidence.scheduler"):
        contract.EvidenceBundle(
            scheduler=tuple(evidence for _ in range(contract._MAX_EVIDENCE_ENTRIES + 1))
        ).as_dict()
    with pytest.raises(contract.ContractViolation, match="native fixture trace"):
        contract._validate_observation_trace(
            LyingSequence(
                [{"tool": "read_live_state"}] * (contract._MAX_NATIVE_TRACE_ENTRIES + 1)
            )
        )


def test_foreign_running_action_is_rejected_before_decision():
    context = _context(
        "foreign-running-action",
        {
            "run_id": "current-run",
            "blocker": {
                "fingerprint": "provider:capacity",
                "previous_fingerprint": "provider:capacity",
                "occurrences": 3,
            },
        },
    )
    state = _fixture_state(
        context,
        live={
            "existing_action": {
                "status": "running",
                "current_run_id": "foreign-run",
            }
        },
    )
    with pytest.raises(contract.ContractViolation, match="foreign current run"):
        contract.evaluate_decision(
            SyntheticDecisionModel(),
            context,
            contract.NoSideEffectFixtureAdapter(state),
        )
