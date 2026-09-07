"""Regression tests for the versioned native-boundary artifact.

The owner contracts under the ESG audit report exercise the complete native
specify and guard entry points. These tests add artifact integrity, path
safety, and optional staged-runtime concurrency coverage without contacting a
provider or touching a live board.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "local-variant" / "native-boundary"
BUILDER_PATH = ARTIFACT / "build_native_boundary.py"
NATIVE_PATH = ARTIFACT / "runtime" / "hermes_cli" / "native_boundary.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load_module("native_boundary_builder_for_tests", BUILDER_PATH)
native_boundary = _load_module("native_boundary_for_tests", NATIVE_PATH)


def test_static_manifest_and_patch_are_pinned():
    manifest = builder._static_manifest()
    assert manifest["schema"] == "factory.native-boundary.v1"
    assert manifest["artifact_version"] == "1.0.0"
    assert manifest["copy_policy"] == {
        "fresh_copy_required": True,
        "reject_symlinks": True,
        "exclude_vcs_and_caches": True,
        "no_live_install_or_restart": True,
    }
    assert {entry["path"]: entry["sha256"] for entry in manifest["source_files"]} == {
        "hermes_cli/kanban_db.py": "3d225442d9aae60ae05f659b1e3a10bc5833b1b8332bce81fc83a77bc791473f",
        "hermes_cli/kanban_specify.py": "67bdf407fc4ce626677c8aae7ee3a2a0da893c9015ad56410e0de3704ed19f13",
    }
    for entry in manifest["patches"]:
        assert builder._sha256(ARTIFACT / entry["path"]) == entry["sha256"]


def test_unsafe_relative_and_symlink_paths_fail_closed(tmp_path):
    for value in ("", ".", "../escape", "/absolute", "a/../b", "a//b"):
        with pytest.raises(ValueError, match="unsafe artifact path"):
            builder._safe_relative(value)

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        builder._resolve_directory(str(linked), label="source runtime")

    source = tmp_path / "source"
    source.mkdir()
    output_parent = tmp_path / "output-parent"
    output_parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        builder.stage(str(source), str(output_parent / "staged"), None)


def test_tampered_manifest_pins_are_rejected(tmp_path):
    manifest = builder._static_manifest()
    patch_entries = builder._patch_entries(manifest)
    output = {
        "schema": manifest["schema"],
        "artifact_version": manifest["artifact_version"],
        "source_files": {"hermes_cli/kanban_db.py": "0" * 64},
        "patches": {
            relative: builder._sha256(path) for path, relative in patch_entries
        },
        "output_runtime": str(tmp_path / "missing-runtime"),
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(output), encoding="utf-8")

    with pytest.raises(ValueError, match="source pins"):
        builder.verify(str(path))


def test_tampered_patch_pin_is_rejected(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    manifest = builder._static_manifest()
    manifest["patches"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(builder, "STATIC_MANIFEST", manifest_path)

    with pytest.raises(ValueError, match="patch hash mismatch"):
        builder._patch_entries(builder._static_manifest())


def test_manifest_symlink_is_rejected(tmp_path):
    target = tmp_path / "manifest.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "manifest-link.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        builder.verify(str(link))


def test_native_quarantine_decision_is_conservative():
    row = {
        "title": "Capability unavailable",
        "body": "The required provider cannot be reached.",
        "block_recurrences": 2,
    }
    assert (
        native_boundary.triage_admission_rejection(
            row,
            title=row["title"],
            body=row["body"],
            recurrence_limit=2,
        )
        == "repeated blocker remains quarantined pending explicit resolution"
    )
    assert (
        native_boundary.triage_admission_rejection(
            row,
            title="New independently actionable design",
            body="Use a local deterministic implementation with a bounded fixture.",
            recurrence_limit=2,
        )
        == "repeated blocker remains quarantined pending explicit resolution"
    )
    assert (
        native_boundary.triage_admission_rejection(
            {**row, "block_recurrences": 0},
            title=row["title"],
            body=row["body"],
            recurrence_limit=2,
        )
        is None
    )


def _requeue_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            status TEXT,
            assignee TEXT,
            claim_lock TEXT,
            current_run_id INTEGER,
            consecutive_failures INTEGER
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT,
            profile TEXT,
            outcome TEXT,
            ended_at INTEGER
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT,
            kind TEXT,
            payload TEXT,
            created_at INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?, 'ready', ?, NULL, NULL, 0)",
        ("task-1", "fixture-worker"),
    )
    conn.execute(
        "INSERT INTO task_runs VALUES (1, ?, ?, 'blocked', 100)",
        ("task-1", "fixture-worker"),
    )
    conn.execute(
        "INSERT INTO task_events VALUES (1, ?, 'blocked', NULL, 99)",
        ("task-1",),
    )
    conn.execute(
        "INSERT INTO task_events VALUES (2, ?, 'unblocked', NULL, 101)",
        ("task-1",),
    )
    conn.commit()
    return conn


def test_same_owner_requeue_ignores_later_comment_but_not_new_lifecycle_state():
    conn = _requeue_connection()
    conn.execute(
        "INSERT INTO task_events VALUES (3, ?, 'commented', ?, 102)",
        ("task-1", "existing pull request"),
    )
    conn.execute(
        "INSERT INTO task_events VALUES (4, ?, 'respawn_guarded', ?, 103)",
        ("task-1", json.dumps({"reason": "active_pr"})),
    )
    conn.commit()
    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is True

    conn.execute(
        "INSERT INTO task_events VALUES (5, ?, 'status', ?, 104)",
        ("task-1", json.dumps({"status": "running"})),
    )
    conn.commit()
    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is False

    conn.close()


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE tasks SET assignee = 'other-worker' WHERE id = 'task-1'",
        "UPDATE tasks SET current_run_id = 99 WHERE id = 'task-1'",
        "UPDATE tasks SET consecutive_failures = 1 WHERE id = 'task-1'",
        "UPDATE task_runs SET profile = 'other-worker' WHERE id = 1",
        "UPDATE task_runs SET outcome = 'completed' WHERE id = 1",
        "UPDATE task_events SET created_at = 99 WHERE id = 2",
    ],
)
def test_same_owner_requeue_fail_closed_for_ambiguous_history(change):
    conn = _requeue_connection()
    conn.execute(change)
    conn.commit()
    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is False
    conn.close()


def _staged_runtime() -> Path:
    value = os.environ.get("FACTORY_NATIVE_RUNTIME")
    if not value:
        pytest.skip("FACTORY_NATIVE_RUNTIME is required for staged-runtime tests")
    runtime = Path(value).resolve(strict=True)
    if not runtime.is_dir():
        pytest.fail(f"staged runtime is not a directory: {runtime}")
    return runtime


def test_staged_imports_are_private_and_provenanced():
    runtime = _staged_runtime()
    script = textwrap.dedent(
        """
        import json
        from pathlib import Path
        from hermes_cli import kanban_db, kanban_specify
        from hermes_cli import native_boundary
        print(json.dumps({
            "db": str(Path(kanban_db.__file__).resolve()),
            "specify": str(Path(kanban_specify.__file__).resolve()),
            "boundary": str(Path(native_boundary.__file__).resolve()),
        }))
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(runtime)
    environment["PYTHONNOUSERSITE"] = "1"
    for key in tuple(environment):
        if key.startswith("HERMES_"):
            environment.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=runtime,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    imported = json.loads(result.stdout)
    assert all(
        Path(value).resolve().is_relative_to(runtime) for value in imported.values()
    )


def test_staged_native_direct_callers_have_one_admission():
    runtime = _staged_runtime()
    script = textwrap.dedent(
        """
        import json
        import os
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        kb.init_db(db)
        with kb.connect_closing(db) as conn:
            task_id = kb.create_task(
                conn,
                title="Concurrent native specification",
                body="An independently actionable task.",
                assignee="fixture-worker",
                created_by="fixture",
                triage=True,
            )

        def admit():
            with kb.connect_closing(db) as conn:
                return kb.specify_triage_task(
                    conn,
                    task_id,
                    title="Concurrent native specification",
                    body="An independently actionable task.",
                    assignee="fixture-worker",
                    author="fixture",
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: admit(), range(2)))
        assert sorted(outcomes) == [False, True], outcomes
        with kb.connect_closing(db) as conn:
            task = kb.get_task(conn, task_id)
            assert task.status == "ready"
        print(json.dumps({"runtime": str(Path(kb.__file__).resolve()), "outcomes": outcomes}))
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(runtime)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["HOME"] = str(runtime / ".test-home")
    environment["HERMES_KANBAN_DB"] = str(runtime / ".native-concurrency.db")
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=runtime,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
