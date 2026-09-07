"""Regression tests for the versioned native-boundary artifact.

The owner contracts under the ESG audit report exercise the complete native
specify and guard entry points. These tests add artifact integrity, path
safety, and optional staged-runtime dispatch, concurrency, and migration
coverage without contacting a provider or touching a live board.
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
# This URL is an opaque, non-routable fixture value. Tests never contact GitHub;
# the old guard only needs the same URL shape as its production PR detector.
FIXTURE_PR_URL = "https://github.com/fixture-owner/fixture-repository/pull/123"


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


def test_complete_tree_manifest_rejects_unlisted_imported_module(tmp_path):
    runtime = tmp_path / "runtime"
    package = runtime / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("ORIGINAL = True\n", encoding="utf-8")
    (package / "kanban_db.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "kanban_specify.py").write_text("VALUE = 2\n", encoding="utf-8")
    expected = builder._tree_files(runtime, allow_excluded=False)

    # __init__.py is intentionally not a patched target, but it is imported
    # as part of the same staged package and therefore must be authenticated.
    (package / "__init__.py").write_text("TAMPERED = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="staged runtime tree"):
        builder._verify_tree(
            runtime,
            ["hermes_cli/kanban_db.py"],
            expected=expected,
        )


def test_patch_target_allowlist_rejects_unlisted_file(tmp_path):
    patch_path = tmp_path / "unlisted.patch"
    patch_path.write_text(
        "--- a/hermes_cli/kanban_db.py\n"
        "+++ b/hermes_cli/kanban_db.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "--- a/hermes_cli/unlisted.py\n"
        "+++ b/hermes_cli/unlisted.py\n"
        "@@ -0,0 +1 @@\n"
        "+unexpected\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside the exact allowlist"):
        builder._validate_patch_targets(
            [(patch_path, "patches/unlisted.patch")],
            ["hermes_cli/kanban_db.py"],
        )


def test_hardlinked_runtime_entries_fail_closed(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    original = runtime / "original.py"
    original.write_text("value = 1\n", encoding="utf-8")
    hardlink = runtime / "hardlink.py"
    hardlink.hardlink_to(original)
    with pytest.raises(ValueError, match="hard-linked"):
        builder._tree_files(runtime, allow_excluded=False)


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


def _private_environment(
    runtime: Path,
    tmp_path: Path,
    *,
    db: Path | None = None,
    task_file: Path | None = None,
) -> dict[str, str]:
    """Build a fresh child environment with no inherited identity or secrets."""
    environment = {
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(runtime),
        "TMPDIR": str(tmp_path),
    }
    if db is not None:
        environment["HERMES_KANBAN_DB"] = str(db)
    if task_file is not None:
        environment["TASK_ID_FILE"] = str(task_file)
    return environment


def test_staged_imports_are_private_and_provenanced(tmp_path):
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
    environment = _private_environment(runtime, tmp_path)
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


def test_staged_native_direct_callers_have_one_admission(tmp_path):
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
    environment = _private_environment(
        runtime, tmp_path, db=tmp_path / ".native-concurrency.db"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=runtime,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_staged_dispatch_tick_uses_guard_before_dry_run_admission(tmp_path):
    runtime = _staged_runtime()
    old_runtime = Path(
        os.environ.get(
            "FACTORY_NATIVE_LEGACY_RUNTIME",
            "/home/ksamaschke/.hermes/profiles/orchestrator/runtime-hotfix-20260906",
        )
    ).resolve()
    if not old_runtime.is_dir():
        pytest.skip("FACTORY_NATIVE_LEGACY_RUNTIME is not available")
    db = tmp_path / "native-dispatch-migration.db"
    old_script = textwrap.dedent(
        """
        import json
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        kb.init_db(db)
        with kb.connect_closing(db) as conn:
            task_id = kb.create_task(
                conn,
                title="Existing source continuation",
                body="Continue the bounded source work after explicit resolution.",
                assignee="default",
                created_by="fixture",
            )
            running = kb.claim_task(conn, task_id, claimer="fixture-owner:1")
            assert running is not None
            assert kb.block_task(
                conn,
                task_id,
                reason="explicit fixture resolution",
                kind="needs_input",
                expected_run_id=running.current_run_id,
            )
            assert kb.unblock_task(conn, task_id)
            kb.add_comment(conn, task_id, "fixture-controller", __FIXTURE_PR_URL__)
            guard = kb.check_respawn_guard(conn, task_id, lane="ready")
            result = kb.dispatch_once(
                conn,
                dry_run=True,
                max_spawn=1,
                reconcile_orphans=False,
            )
            print(json.dumps({
                "runtime": str(Path(kb.__file__).resolve()),
                "task_id": task_id,
                "guard": guard,
                "spawned": [item[0] for item in result.spawned],
            }))
        """
    ).replace("__FIXTURE_PR_URL__", repr(FIXTURE_PR_URL))
    old_result = subprocess.run(
        [sys.executable, "-B", "-c", old_script],
        cwd=old_runtime,
        env=_private_environment(old_runtime, tmp_path, db=db),
        capture_output=True,
        text=True,
        check=False,
    )
    assert old_result.returncode == 0, old_result.stderr or old_result.stdout
    old_probe = json.loads(old_result.stdout)
    assert old_probe["guard"] == "active_pr"
    assert old_probe["spawned"] == []

    staged_script = textwrap.dedent(
        """
        import json
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(__import__("os").environ["HERMES_KANBAN_DB"])
        with kb.connect_closing(db) as conn:
            task_id = __TASK_ID__
            guard = kb.check_respawn_guard(conn, task_id, lane="ready")
            result = kb.dispatch_once(
                conn,
                dry_run=True,
                max_spawn=1,
                reconcile_orphans=False,
            )
            resumed = kb.claim_task(conn, task_id, claimer="fixture-owner:2")
            second = kb.claim_task(conn, task_id, claimer="fixture-owner:3")
            print(json.dumps({
                "runtime": str(Path(kb.__file__).resolve()),
                "guard": guard,
                "spawned": [item[0] for item in result.spawned],
                "resumed": resumed is not None,
                "second_claim": second is not None,
            }))
        """
    ).replace("__TASK_ID__", repr(old_probe["task_id"]))
    staged_result = subprocess.run(
        [sys.executable, "-B", "-c", staged_script],
        cwd=runtime,
        env=_private_environment(runtime, tmp_path, db=db),
        capture_output=True,
        text=True,
        check=False,
    )
    assert staged_result.returncode == 0, staged_result.stderr or staged_result.stdout
    staged_probe = json.loads(staged_result.stdout)
    assert staged_probe["guard"] is None
    assert staged_probe["spawned"] == [old_probe["task_id"]]
    assert staged_probe["resumed"] is True
    assert staged_probe["second_claim"] is False


def test_staged_dispatch_negative_controls(tmp_path):
    runtime = _staged_runtime()
    script = textwrap.dedent(
        """
        import json
        import os
        import time
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        kb.init_db(db)
        negative = {}
        with kb.connect_closing(db) as conn:
            def create_lifecycle(name, *, explicit, comment=True):
                task_id = kb.create_task(
                    conn,
                    title=name,
                    body="Private negative-control fixture",
                    assignee="default",
                    created_by="fixture",
                )
                run = kb.claim_task(conn, task_id, claimer="fixture-owner:1")
                assert run is not None
                assert kb.block_task(
                    conn,
                    task_id,
                    reason="fixture resolution boundary",
                    kind="needs_input",
                    expected_run_id=run.current_run_id,
                )
                if explicit:
                    assert kb.unblock_task(conn, task_id)
                if comment:
                    kb.add_comment(conn, task_id, "fixture-controller", __FIXTURE_PR_URL__)
                return task_id

            def ready_without_requeue(task_id):
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))

            absent = create_lifecycle("absent explicit requeue", explicit=False)
            ready_without_requeue(absent)
            negative["absent_requeue"] = absent

            comment_only = kb.create_task(
                conn,
                title="comment-only resolution",
                body="No durable lifecycle transition",
                assignee="default",
                created_by="fixture",
            )
            kb.add_comment(conn, comment_only, "fixture-controller", __FIXTURE_PR_URL__)
            negative["comment_only"] = comment_only

            wrong_owner = create_lifecycle("wrong owner", explicit=True)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET assignee = 'other-owner' WHERE id = ?", (wrong_owner,))
            negative["wrong_owner"] = wrong_owner

            unknown_owner = create_lifecycle("unknown owner", explicit=True)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET assignee = NULL WHERE id = ?", (unknown_owner,))
            negative["unknown_owner"] = unknown_owner

            live_run = create_lifecycle("live run", explicit=True)
            assert kb.claim_task(conn, live_run, claimer="fixture-owner:2") is not None
            negative["live_run"] = live_run

            quota = kb.create_task(
                conn,
                title="quota auth blocker",
                body="Private quota fixture",
                assignee="default",
                created_by="fixture",
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                    ("401 unauthorized provider token", quota),
                )
            negative["quota_auth"] = quota

            retry = kb.create_task(
                conn,
                title="retry quarantine",
                body="Private retry fixture",
                assignee="default",
                created_by="fixture",
            )
            now = int(time.time())
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, ?, 'done', ?, ?, 'rate_limited')",
                (retry, "default", now, now),
            )
            conn.commit()
            negative["retry_quarantine"] = retry

            guards = {name: kb.check_respawn_guard(conn, task_id, lane="ready")
                      for name, task_id in negative.items()}
            result = kb.dispatch_once(
                conn,
                dry_run=True,
                max_spawn=1,
                reconcile_orphans=False,
            )
            print(json.dumps({
                "guards": guards,
                "spawned": [item[0] for item in result.spawned],
                "runtime": str(Path(kb.__file__).resolve()),
            }))
        """
    ).replace("__FIXTURE_PR_URL__", repr(FIXTURE_PR_URL))
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=runtime,
        env=_private_environment(
            runtime, tmp_path, db=tmp_path / "negative-controls.db"
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    probe = json.loads(result.stdout)
    assert probe["guards"]["absent_requeue"] == "active_pr"
    assert probe["guards"]["comment_only"] == "active_pr"
    assert probe["guards"]["wrong_owner"] == "active_pr"
    assert probe["guards"]["unknown_owner"] == "active_pr"
    assert probe["guards"]["live_run"] == "active_pr"
    assert probe["guards"]["quota_auth"] == "blocker_auth"
    assert probe["guards"]["retry_quarantine"] == "rate_limit_cooldown"
    assert probe["spawned"] == []


def test_old_runtime_lifecycle_state_is_admitted_by_staged_runtime(tmp_path):
    runtime = _staged_runtime()
    old_runtime = Path(
        os.environ.get(
            "FACTORY_NATIVE_LEGACY_RUNTIME",
            "/home/ksamaschke/.hermes/profiles/orchestrator/runtime-hotfix-20260906",
        )
    ).resolve()
    if not old_runtime.is_dir():
        pytest.skip("FACTORY_NATIVE_LEGACY_RUNTIME is not available")
    db = tmp_path / "legacy-native.db"
    task_file = tmp_path / "task-id.txt"
    old_script = textwrap.dedent(
        """
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        kb.init_db(db)
        with kb.connect_closing(db) as conn:
            task_id = kb.create_task(
                conn,
                title="Legacy source continuation",
                body="Continue the same source after an explicit old-runtime unblock.",
                assignee="default",
                created_by="fixture",
            )
            running = kb.claim_task(conn, task_id, claimer="fixture-owner:1")
            assert running is not None
            assert kb.block_task(
                conn,
                task_id,
                reason="legacy explicit resolution",
                kind="needs_input",
                expected_run_id=running.current_run_id,
            )
            assert kb.unblock_task(conn, task_id)
            with kb.write_txn(conn):
                for _ in range(3):
                    kb._append_event(conn, task_id, "respawn_guarded", {"reason": "active_pr"})
            kb.add_comment(conn, task_id, "fixture-controller", __FIXTURE_PR_URL__)
        Path(os.environ["TASK_ID_FILE"]).write_text(task_id, encoding="utf-8")
        """
    ).replace("__FIXTURE_PR_URL__", repr(FIXTURE_PR_URL))
    old_environment = _private_environment(
        old_runtime, tmp_path, db=db, task_file=task_file
    )
    old_result = subprocess.run(
        [sys.executable, "-B", "-c", old_script],
        cwd=old_runtime,
        env=old_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert old_result.returncode == 0, old_result.stderr or old_result.stdout
    staged_script = textwrap.dedent(
        """
        import json
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        with kb.connect_closing(db) as conn:
            task_id = Path(os.environ["TASK_ID_FILE"]).read_text(encoding="utf-8")
            assert kb.check_respawn_guard(conn, task_id, lane="ready") is None
            resumed = kb.claim_task(conn, task_id, claimer="fixture-owner:2")
            assert resumed is not None
            assert resumed.assignee == "default"
            assert kb.claim_task(conn, task_id, claimer="fixture-owner:3") is None
        print(json.dumps({"runtime": str(Path(kb.__file__).resolve()), "task_id": task_id}))
        """
    )
    staged_environment = _private_environment(
        runtime, tmp_path, db=db, task_file=task_file
    )
    staged_result = subprocess.run(
        [sys.executable, "-B", "-c", staged_script],
        cwd=runtime,
        env=staged_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert staged_result.returncode == 0, staged_result.stderr or staged_result.stdout
