from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ARTIFACT = Path(__file__).parents[1] / "local-variant" / "review-runtime"
BUILDER_PATH = ARTIFACT / "build_review_runtime.py"
MANIFEST_PATH = ARTIFACT / "manifest.json"
PATCH_PATH = ARTIFACT / "patches" / "002-review-runtime.patch"


def _builder_module():
    spec = importlib.util.spec_from_file_location(
        "review_runtime_builder", BUILDER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_review_manifest_pins_policy_patch_and_targets():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    expected_hash = hashlib.sha256(PATCH_PATH.read_bytes()).hexdigest()

    assert manifest["schema"] == "factory.review-runtime.v1"
    assert manifest["native_boundary_schema"] == "factory.native-boundary.v1"
    assert (
        manifest["native_manifest_sha256"]
        == "c15a9c10e499c5cbda6cbbe523162502b8875720f639a42e4a0a48b2ef2c4a01"
    )
    assert manifest["native_artifact_version"] == "1.0.0"
    assert manifest["policy"]["review_dispatch_hard_cap_seconds"] == 1200
    assert manifest["policy"]["review"] == {
        "hard_worker_cap_seconds": 1200,
        "evidence_budget_seconds": 600,
        "per_command_timeout_seconds": 120,
    }
    assert manifest["policy"]["evidence_recovery"] == {
        "hard_worker_cap_seconds": 600,
        "evidence_budget_seconds": 300,
        "per_command_timeout_seconds": 60,
    }
    assert manifest["patches"]["patches/002-review-runtime.patch"] == expected_hash
    assert manifest["patched_paths"] == [
        "hermes_cli/kanban_db.py",
        "gateway/kanban_watchers.py",
        "tui_gateway/server.py",
        "tools/terminal_tool.py",
        "hermes_cli/kanban.py",
    ]


def test_review_patch_has_exact_allowlist_and_truthful_status_logic():
    builder = _builder_module()
    patch_text = PATCH_PATH.read_text(encoding="utf-8")

    assert builder._patch_targets(PATCH_PATH) == tuple(builder.TARGET_PATHS)
    assert "REVIEW_DISPATCH_HARD_CAP_SECONDS = 1200" in patch_text
    assert "EVIDENCE_RECOVERY_RUNTIME_POLICY" in patch_text
    assert "claim_evidence_recovery_task" in patch_text
    assert '"HERMES_KANBAN_TOOL_TIMEOUT_SECONDS"' in patch_text
    assert "HERMES_KANBAN_EVIDENCE_DEADLINE" in patch_text
    assert "_review_worker_env(" in patch_text
    assert "retry queued" in patch_text
    assert "task is {retry_status}, no retry" in patch_text
    assert '"final_status": "blocked"' in patch_text


def test_builder_rejects_non_native_boundary_input(tmp_path):
    builder = _builder_module()
    native_runtime = tmp_path / "native"
    native_runtime.mkdir()
    manifest = tmp_path / "native.json"
    manifest.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")

    with pytest.raises(SystemExit, match="native input"):
        builder._validate_native_input(native_runtime, manifest)


def test_builder_stages_prerequisite_when_paths_are_provided(tmp_path):
    """Exercise the real layered build when the prerequisite artifact is present."""
    native_runtime_value = os.environ.get("FACTORY_NATIVE_BOUNDARY_RUNTIME")
    native_manifest_value = os.environ.get("FACTORY_NATIVE_BOUNDARY_MANIFEST")
    if not native_runtime_value or not native_manifest_value:
        pytest.skip(
            "set FACTORY_NATIVE_BOUNDARY_RUNTIME and FACTORY_NATIVE_BOUNDARY_MANIFEST"
        )

    builder = _builder_module()
    output = tmp_path / "review-runtime"
    generated = tmp_path / "review-runtime.manifest.json"
    manifest_path = builder.build(
        Path(native_runtime_value),
        Path(native_manifest_value),
        output,
        generated,
        force=False,
    )

    assert manifest_path == generated
    builder.verify(output, generated)
    result = json.loads(generated.read_text(encoding="utf-8"))
    assert result["policy"]["review_dispatch_hard_cap_seconds"] == 1200
    assert result["policy"]["review"]["evidence_budget_seconds"] == 600
    assert result["policy"]["evidence_recovery"] == {
        "hard_worker_cap_seconds": 600,
        "evidence_budget_seconds": 300,
        "per_command_timeout_seconds": 60,
    }
    assert (
        result["native_manifest_sha256"]
        == "c15a9c10e499c5cbda6cbbe523162502b8875720f639a42e4a0a48b2ef2c4a01"
    )
    assert result["native_source_tree_sha256"]
    assert result["native_staged_tree_sha256"]
    assert result["syntax_probe"] == list(builder.TARGET_PATHS)


def test_review_claim_caps_active_run_without_rewriting_task_row(tmp_path, monkeypatch):
    runtime_value = os.environ.get("FACTORY_REVIEW_RUNTIME")
    source_value = os.environ.get("FACTORY_SOURCE_RUNTIME")
    if not runtime_value or not source_value:
        pytest.skip("set FACTORY_REVIEW_RUNTIME and FACTORY_SOURCE_RUNTIME")

    sys.path.insert(0, runtime_value)
    sys.path.insert(1, source_value)
    try:
        spec = importlib.util.spec_from_file_location(
            "hermes_cli.kanban_db", Path(runtime_value) / "hermes_cli/kanban_db.py"
        )
        assert spec is not None and spec.loader is not None
        import hermes_cli  # noqa: F401

        sys.modules.pop("hermes_cli.kanban_db", None)
        module = importlib.util.module_from_spec(spec)
        sys.modules["hermes_cli.kanban_db"] = module
        spec.loader.exec_module(module)

        db_path = tmp_path / "review-cap.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        module.init_db()
        conn = module.connect()
        task_id = module.create_task(
            conn,
            title="review cap",
            assignee="reviewer",
            max_runtime_seconds=2400,
        )
        with module.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        claimed = module.claim_review_task(conn, task_id, claimer="fixture-reviewer")
        assert claimed is not None
        task_row = conn.execute(
            "SELECT max_runtime_seconds, current_run_id FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT max_runtime_seconds FROM task_runs WHERE id=?",
            (task_row["current_run_id"],),
        ).fetchone()
        assert task_row["max_runtime_seconds"] == 2400
        assert run_row["max_runtime_seconds"] == 1200
        assert claimed.max_runtime_seconds == 1200
        assert claimed.runtime_class == module.RUNTIME_CLASS_REVIEW
        assert claimed.evidence_budget_seconds == 600
        assert claimed.command_timeout_seconds == 120
    finally:
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)


def test_review_requeue_resets_runtime_lane_and_evidence_dispatch_isolated(
    tmp_path, monkeypatch
):
    runtime_value = os.environ.get("FACTORY_REVIEW_RUNTIME")
    source_value = os.environ.get("FACTORY_SOURCE_RUNTIME")
    if not runtime_value or not source_value:
        pytest.skip("set FACTORY_REVIEW_RUNTIME and FACTORY_SOURCE_RUNTIME")

    sys.path.insert(0, runtime_value)
    sys.path.insert(1, source_value)
    try:
        spec = importlib.util.spec_from_file_location(
            "hermes_cli.kanban_db", Path(runtime_value) / "hermes_cli/kanban_db.py"
        )
        assert spec is not None and spec.loader is not None
        import hermes_cli  # noqa: F401

        sys.modules.pop("hermes_cli.kanban_db", None)
        module = importlib.util.module_from_spec(spec)
        sys.modules["hermes_cli.kanban_db"] = module
        spec.loader.exec_module(module)

        db_path = tmp_path / "lane.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        module.init_db()
        conn = module.connect()
        task_id = module.create_task(conn, title="review lane", assignee="reviewer")
        claimed = module.claim_task(conn, task_id, claimer="implementer")
        assert claimed is not None and claimed.current_run_id is not None
        assert module.request_review(
            conn,
            task_id,
            summary="ready for review",
            expected_run_id=claimed.current_run_id,
        )
        review = module.claim_review_task(conn, task_id, claimer="reviewer")
        assert review is not None and review.current_run_id is not None
        assert module.request_changes(
            conn,
            task_id,
            reason="needs a fix",
            expected_run_id=review.current_run_id,
        )
        row = conn.execute(
            "SELECT status, runtime_class FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert tuple(row) == ("ready", module.RUNTIME_CLASS_IMPLEMENTATION)

        normal = module.create_task(conn, title="normal", assignee="reviewer")
        evidence = module.create_task(
            conn,
            title="evidence",
            assignee="reviewer",
            runtime_class=module.RUNTIME_CLASS_EVIDENCE_RECOVERY,
        )
        from hermes_cli import profiles

        monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
        result = module.dispatch_evidence_recovery_once(
            conn, dry_run=True, max_spawn=10
        )
        assert [item[0] for item in result.spawned] == [evidence]
        assert normal not in [item[0] for item in result.spawned]
    finally:
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)
