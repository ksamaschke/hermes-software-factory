"""Causal tests for the generic Factory admission/lifecycle seam."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "kanban_packet_repair_guard.py"
spec = importlib.util.spec_from_file_location("packet_repair_guard_admission", SCRIPT)
assert spec is not None and spec.loader is not None
admission = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = admission
spec.loader.exec_module(admission)


IDENTITY = {
    "source_repository": "forgejo.example/factory",
    "source_item": "issue-24",
    "phase": "implementation",
    "input_artifact_revision": "sha-abc123",
}


VALID_EVIDENCE = {
    "kind": "blocker-resolution",
    "source": "controller-readback",
    "reference": "run-42",
    "contract_revision": "sha-def456",
    "observed_at": 100,
}


@pytest.fixture
def native_runtime(tmp_path, monkeypatch):
    runtime_value = os.environ.get("FACTORY_NATIVE_RUNTIME")
    if not runtime_value:
        pytest.skip(
            "FACTORY_NATIVE_RUNTIME is required for native-path adapter coverage"
        )
    runtime = Path(runtime_value).resolve(strict=True)
    for key in tuple(os.environ):
        if key.startswith("HERMES_"):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    root = home / ".hermes"
    root.mkdir(parents=True)
    db = root / "isolated-kanban.db"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_ROOT_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(Path, "home", lambda: home)
    for module_name in tuple(sys.modules):
        if module_name == "hermes_cli" or module_name.startswith("hermes_cli."):
            sys.modules.pop(module_name, None)
    monkeypatch.syspath_prepend(str(runtime))
    from agent import auxiliary_client
    from hermes_cli import kanban_db as db_module
    from hermes_cli import kanban_specify as native_module

    assert Path(db_module.__file__).resolve().parent.parent == runtime
    assert Path(native_module.__file__).resolve().parent.parent == runtime
    assert db_module.kanban_db_path().resolve() == db.resolve()
    db_module.init_db()
    return db_module, native_module, auxiliary_client


def _native_spec_response(task):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps({"title": task.title, "body": task.body})
                )
            )
        ]
    )


def test_identity_and_blocker_fingerprint_ignore_title_body_paraphrase():
    first = {
        **IDENTITY,
        "title": "Signer unavailable",
        "body": "The artifact signer is unavailable.",
        "blocker_class": "artifact_signer",
    }
    paraphrase = {
        **IDENTITY,
        "title": "Provenance publication is waiting",
        "body": "Publication cannot proceed until the signer returns.",
        "blocker_class": "artifact_signer",
    }

    assert admission.canonical_identity(first) == admission.canonical_identity(
        paraphrase
    )
    assert admission.blocker_fingerprint(first) == admission.blocker_fingerprint(
        paraphrase
    )


def test_repeated_blocker_stays_fenced_for_three_ticks_until_evidence_resolution():
    ledger = admission.AdmissionLedger()
    task = {**IDENTITY, "blocker_class": "artifact_signer"}

    for tick in range(1, 4):
        record = ledger.observe_blocker(task, tick=tick)
        assert record.observations == tick
        assert record.fenced is (tick >= 3)

    assert ledger.is_fenced(task)
    assert ledger.observe_blocker(
        {**task, "title": "different wording", "body": "same gate"}, tick=4
    ).fenced

    assert ledger.resolve_blocker(task, VALID_EVIDENCE, new_contract=True).accepted
    admission_result = ledger.admit(task)
    assert admission_result.created is True
    assert admission_result.current_run_id is not None
    assert ledger.admit(task).created is False
    assert ledger.admit(task).current_run_id == admission_result.current_run_id


def test_malformed_or_unrelated_resolution_evidence_fails_closed():
    ledger = admission.AdmissionLedger()
    task = {**IDENTITY, "blocker_class": "artifact_signer"}
    for tick in range(1, 4):
        ledger.observe_blocker(task, tick=tick)

    missing_reference = {**VALID_EVIDENCE, "reference": ""}
    assert not ledger.resolve_blocker(
        task, missing_reference, new_contract=True
    ).accepted
    unrelated = {**VALID_EVIDENCE, "source": "other-controller"}
    assert not ledger.resolve_blocker(task, unrelated, new_contract=True).accepted
    assert ledger.is_fenced(task)


def test_parentless_dependency_wait_does_not_spin_and_parent_resumes_once():
    gate = admission.DependencyGate()
    assert gate.reconcile("child", [], tick=1).action == "hold"
    assert gate.reconcile("child", [], tick=2).action == "hold"
    assert gate.resume_count("child") == 0

    unfinished = [{"id": "parent", "status": "running", "current_run_id": 7}]
    assert gate.reconcile("child", unfinished, tick=3).action == "wait"
    assert (
        gate.reconcile("child", [{**unfinished[0], "status": "done"}], tick=4).action
        == "resume"
    )
    assert (
        gate.reconcile("child", [{"id": "parent", "status": "done"}], tick=5).action
        == "hold"
    )
    assert gate.resume_count("child") == 1


def test_capability_preflight_rejects_before_spawn(tmp_path, monkeypatch):
    calls = []
    request = admission.CapabilityRequest(
        target_profile="reviewer",
        skill="review-contract",
        category="review",
        workspace=str(tmp_path),
        interpreter="/definitely/missing/interpreter",
        tools=("git",),
    )

    result = admission.spawn_after_preflight(
        request,
        lambda: calls.append("spawn"),
        profiles={"reviewer"},
        skills={"review-contract"},
        categories={"review"},
    )

    assert result.spawned is False
    assert calls == []
    assert any("interpreter" in error for error in result.errors)


def test_capability_preflight_rejects_unknown_profile_skill_category_and_tool(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(admission.shutil, "which", lambda name: None)
    request = admission.CapabilityRequest(
        target_profile="bad profile",
        skill="missing-skill",
        category="missing-category",
        workspace=str(tmp_path),
        interpreter="python3",
        tools=("missing-tool",),
    )

    result = admission.preflight_capability(
        request,
        profiles={"reviewer"},
        skills={"review-contract"},
        categories={"review"},
    )

    assert result.ok is False
    assert {"profile", "skill", "category", "tool"} <= {
        error.split(":", 1)[0] for error in result.errors
    }


def test_idempotent_create_readback_distinguishes_not_started_and_current_run():
    ledger = admission.AdmissionLedger()
    not_started = ledger.create_or_reuse(
        IDENTITY,
        idempotency_key="guess-a",
        existing={
            **IDENTITY,
            "id": "task-1",
            "status": "todo",
            "current_run_id": None,
            "historical_run_ids": [3],
        },
    )
    assert not_started.reused is True
    assert not_started.created is False
    assert not_started.current_run_id is None
    assert not_started.state == "reused_not_started"

    created = ledger.create_or_reuse(IDENTITY, idempotency_key="guess-b")
    assert created.created is True
    assert created.current_run_id is not None
    assert (
        ledger.create_or_reuse(IDENTITY, idempotency_key="guess-c").current_run_id
        == created.current_run_id
    )


def test_malformed_current_run_readback_fails_closed():
    ledger = admission.AdmissionLedger()
    result = ledger.create_or_reuse(
        IDENTITY,
        idempotency_key="readback",
        existing={
            **IDENTITY,
            "id": "task-1",
            "status": "running",
            "current_run_id": "not-an-integer",
        },
    )

    assert result.accepted is False
    assert result.state == "rejected"
    assert "current run" in result.reason


def test_inconsistent_active_or_historical_readback_fails_closed():
    ledger = admission.AdmissionLedger()
    active_without_run = ledger.create_or_reuse(
        IDENTITY,
        idempotency_key="active-without-run",
        existing={
            **IDENTITY,
            "id": "task-active",
            "status": "running",
            "current_run_id": None,
            "historical_run_ids": [],
        },
    )
    malformed_history = ledger.create_or_reuse(
        IDENTITY,
        idempotency_key="history-string",
        existing={
            **IDENTITY,
            "id": "task-history",
            "status": "todo",
            "current_run_id": None,
            "historical_run_ids": "42",
        },
    )
    assert active_without_run.accepted is False
    assert "active task" in active_without_run.reason
    assert malformed_history.accepted is False
    assert "historical run" in malformed_history.reason


def test_historical_run_id_is_rejected_and_current_identity_is_required():
    ledger = admission.AdmissionLedger()
    created = ledger.create_or_reuse(IDENTITY, idempotency_key="key")
    rejected = ledger.create_or_reuse(
        IDENTITY,
        idempotency_key="another-key",
        requested_run_id=created.current_run_id - 1,
    )
    assert rejected.accepted is False
    assert "historical" in rejected.reason

    foreign = ledger.create_or_reuse(
        {**IDENTITY, "source_item": "issue-25"}, idempotency_key="foreign"
    )
    assert foreign.created is True
    assert foreign.current_run_id != created.current_run_id


def test_exact_run_bound_report_rejects_stale_identity_and_preserves_current_run():
    ledger = admission.AdmissionLedger()
    created = ledger.create_or_reuse(IDENTITY, idempotency_key="key")
    report = {
        "source_repository": IDENTITY["source_repository"],
        "source_item": IDENTITY["source_item"],
        "phase": IDENTITY["phase"],
        "input_artifact_revision": IDENTITY["input_artifact_revision"],
        "current_run_id": created.current_run_id,
        "outcome": "completed",
    }
    assert admission.validate_run_bound_report(
        report, IDENTITY, created.current_run_id
    ).ok
    stale = {**report, "current_run_id": created.current_run_id - 1}
    assert not admission.validate_run_bound_report(
        stale, IDENTITY, created.current_run_id
    ).ok


def test_failed_merged_source_signer_routes_to_artifact_gate_without_retry_or_bypass():
    route = admission.route_merged_source_pr(
        source_pr_state="merged",
        artifact_signer_state="failed",
        implementation_retry_requested=True,
        bypass_requested=True,
    )

    assert route.route == "artifact/provenance"
    assert route.implementation_retry is False
    assert route.bypass is False


def test_failed_artifact_gate_does_not_hide_independent_ready_work():
    controller = admission.AdmissionController(admission.AdmissionLedger())
    rows = [
        {
            **IDENTITY,
            "source_item": "merged-source",
            "status": "ready",
            "source_pr_state": "merged",
            "artifact_signer_state": "failed",
            "priority": 1,
        },
        {
            **IDENTITY,
            "source_item": "independent-ready",
            "status": "ready",
            "priority": 2,
        },
    ]

    decision = controller.tick(rows, tick=1, now=1)

    assert decision.action == "admit"
    assert decision.identity is not None
    assert decision.identity.source_item == "independent-ready"


def test_controller_fences_repeated_blocker_paraphrases_at_three_ticks():
    ledger = admission.AdmissionLedger()
    controller = admission.AdmissionController(ledger)
    blocker = {
        **IDENTITY,
        "status": "triage",
        "blocker_class": "dependency",
        "body": "first wording",
    }
    decision = None
    for tick, wording in enumerate(
        ("first wording", "different wording", "third wording"), 1
    ):
        current = {**blocker, "body": wording}
        decision = controller.tick([current], tick=tick)
    assert ledger.blockers[admission.blocker_fingerprint(blocker)].fenced is True
    assert decision is not None
    assert decision.action == "hold"
    assert "fenced" in decision.reason


def test_semantic_phase_reservation_converges_guessed_idempotency_keys():
    ledger = admission.AdmissionLedger()
    first = ledger.reserve_phase(
        IDENTITY,
        owner="orchestrator",
        phase="recovery",
        tick=1,
        freshness=10,
        idempotency_key="guess-a",
    )
    second = ledger.reserve_phase(
        IDENTITY,
        owner="orchestrator",
        phase="recovery",
        tick=1,
        freshness=10,
        idempotency_key="guess-b",
    )

    assert first.accepted and first.created
    assert second.accepted and second.created is False
    assert second.reservation_id == first.reservation_id
    assert ledger.reservation_count(IDENTITY, "recovery") == 1


def test_fresh_live_owner_prevents_taskless_recovery_descendant_creation():
    ledger = admission.AdmissionLedger()
    controller = admission.AdmissionController(ledger)
    task = {
        **IDENTITY,
        "status": "todo",
        "phase_owner": "orchestrator",
        "phase": "implementation",
    }
    ledger.reserve_phase(
        task,
        owner="orchestrator",
        phase="implementation",
        tick=1,
        freshness=10,
        idempotency_key="owner",
    )

    decision = controller.tick([task], tick=2, now=2)

    assert decision.action == "hold"
    assert decision.created_descendant is False
    assert "fresh" in decision.reason


def test_native_guard_fences_recurrence_threshold_without_title_body_match():
    task = {
        **IDENTITY,
        "status": "triage",
        "block_recurrences": 2,
        "title": "old wording",
        "body": "old body",
    }

    result = admission.native_specification_guard(
        task,
        proposed_title="new wording",
        proposed_body="new contract evidence",
    )

    assert result.accepted is False
    assert result.state == "fenced"


def test_controller_selects_at_most_one_canonical_lane_and_reads_back_reservation():
    controller = admission.AdmissionController(admission.AdmissionLedger())
    rows = [
        {**IDENTITY, "source_item": "issue-2", "status": "todo", "priority": 2},
        {**IDENTITY, "source_item": "issue-1", "status": "todo", "priority": 1},
    ]

    decision = controller.tick(rows, tick=1, now=1)

    assert decision.action == "admit"
    assert decision.identity.source_item == "issue-1"
    assert decision.created_descendant is False
    assert decision.reservation_id


def test_guarded_adapter_exercises_native_transition_and_fences_quarantined_task(
    native_runtime, monkeypatch
):
    db_module, native_module, auxiliary = native_runtime
    with db_module.connect_closing() as conn:
        task_id = db_module.create_task(
            conn,
            title="Fixture specification",
            body="The required capability is unavailable.",
            assignee="fixture-worker",
            created_by="fixture",
        )
        for index in range(db_module.BLOCK_RECURRENCE_LIMIT):
            assert db_module.block_task(
                conn, task_id, reason="same unresolved capability", kind="capability"
            )
            if index < db_module.BLOCK_RECURRENCE_LIMIT - 1:
                assert db_module.unblock_task(conn, task_id)
        before = db_module.get_task(conn, task_id)
        assert before.status == "triage"

    calls = []
    monkeypatch.setattr(
        auxiliary,
        "call_llm",
        lambda **kwargs: calls.append(kwargs) or _native_spec_response(before),
    )
    result = admission.run_guarded_native_specify(
        task_id,
        native_module=native_module,
        db_module=db_module,
        author="fixture",
    )

    with db_module.connect_closing() as conn:
        after = db_module.get_task(conn, task_id)
    assert result.typed_outcome == "QUARANTINED"
    assert result.accepted is False
    assert after.status == "triage"
    assert calls == []

    with db_module.connect_closing() as conn:
        fresh_id = db_module.create_task(
            conn,
            title="Fresh specification",
            body="A new independently actionable fixture.",
            assignee="fixture-worker",
            created_by="fixture",
            triage=True,
        )
        fresh = db_module.get_task(conn, fresh_id)
    monkeypatch.setattr(
        auxiliary, "call_llm", lambda **kwargs: _native_spec_response(fresh)
    )
    fresh_result = admission.run_guarded_native_specify(
        fresh_id,
        native_module=native_module,
        db_module=db_module,
        author="fixture",
    )
    with db_module.connect_closing() as conn:
        fresh_after = db_module.get_task(conn, fresh_id)
    assert fresh_result.typed_outcome == "ADMITTED"
    assert fresh_after.status == "ready"
