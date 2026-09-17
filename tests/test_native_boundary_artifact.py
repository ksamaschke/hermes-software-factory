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
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "local-variant" / "native-boundary"
BUILDER_PATH = ARTIFACT / "build_native_boundary.py"
NATIVE_PATH = ARTIFACT / "runtime" / "hermes_cli" / "native_boundary.py"
# This URL is an opaque, non-routable fixture value. Tests never contact GitHub;
# the old guard only needs the same URL shape as its production PR detector.
FIXTURE_PR_URL = "https://github.com/fixture-owner/fixture-repository/pull/123"
_TEST_LIFECYCLE_BASE = 1_700_000_000


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
    assert manifest["artifact_version"] == "1.0.6"
    assert manifest["copy_policy"] == {
        "fresh_copy_required": True,
        "reject_symlinks": True,
        "exclude_vcs_and_caches": True,
        "no_live_install_or_restart": True,
    }
    assert {entry["path"]: entry["sha256"] for entry in manifest["source_files"]} == {
        "hermes_cli/kanban_db.py": "9262365420d875736fbc90927b353cd854241ae85fb80ffda2e622789fc6ed9e",
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


@pytest.mark.parametrize("destination", ["output", "manifest"])
@pytest.mark.parametrize("boundary", ["source", "artifact"])
def test_stage_rejects_source_and_artifact_destinations_before_side_effects(
    tmp_path, destination, boundary
):
    source = tmp_path / "source"
    source.mkdir()
    token = f".native-boundary-test-{tmp_path.name}-{destination}-{boundary}"
    boundary_root = source if boundary == "source" else ARTIFACT
    output = tmp_path / "staged-output"
    manifest = tmp_path / "manifest.json"
    if destination == "output":
        output = boundary_root / f"{token}-runtime"
    else:
        manifest = boundary_root / f"{token}.json"

    try:
        with pytest.raises(ValueError, match="inside"):
            builder.stage(str(source), str(output), str(manifest))
        assert not output.exists()
        assert not manifest.exists()
        assert not any(source.iterdir())
    finally:
        if output.is_dir():
            shutil.rmtree(output)
        elif output.exists() or output.is_symlink():
            output.unlink()
        if manifest.exists() or manifest.is_symlink():
            manifest.unlink()


def test_patch_application_uses_fixed_tool_and_scrubbed_environment(
    tmp_path, monkeypatch
):
    if not builder._PATCH_EXECUTABLE.is_file():
        pytest.skip("fixed patch executable is not available")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "ambient-path-used"
    fake_patch = fake_bin / "patch"
    fake_patch.write_text(
        "#!/bin/sh\n"
        f'test -z "${{NATIVE_SECRET_SENTINEL-}}" || printf \'%s\' "$NATIVE_SECRET_SENTINEL" > {marker}\n'
        "exit 99\n",
        encoding="utf-8",
    )
    fake_patch.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("NATIVE_SECRET_SENTINEL", "review-secret")

    staging = tmp_path / "runtime"
    staging.mkdir()
    target = staging / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    patch = tmp_path / "change.patch"
    patch.write_text(
        "--- a/target.txt\n+++ b/target.txt\n@@ -1 +1 @@\n-old\n+new\n",
        encoding="utf-8",
    )

    builder._apply_patches(
        staging,
        [(patch, "patches/change.patch")],
        ["target.txt"],
    )

    assert target.read_text(encoding="utf-8") == "new\n"
    assert not marker.exists()


def test_import_probe_uses_fixed_path_and_scrubbed_environment(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    package = runtime / "hermes_cli"
    package.mkdir(parents=True)
    imported = {
        "kanban_db": str(package / "kanban_db.py"),
        "kanban_specify": str(package / "kanban_specify.py"),
        "native_boundary": str(package / "native_boundary.py"),
    }
    for path in imported.values():
        Path(path).write_text("# fixture\n", encoding="utf-8")

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["environment"] = dict(kwargs["env"])
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(imported),
            stderr="",
        )

    monkeypatch.setenv("PATH", str(tmp_path / "attacker-controlled"))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "attacker-python-home"))
    monkeypatch.setenv("PYTHONSTARTUP", str(tmp_path / "attacker-startup.py"))
    monkeypatch.setenv("NATIVE_SECRET_SENTINEL", "review-secret")
    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    assert builder._import_probe(runtime) == imported
    assert captured["command"][:4] == [sys.executable, "-B", "-S", "-c"]
    assert captured["environment"] == {
        "HERMES_HOME": str(runtime / ".probe-home"),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(runtime),
        "PYTHONNOUSERSITE": "1",
        "PYTHONHASHSEED": "0",
    }


def test_documented_cgroup_reader_fails_closed_on_find_error(tmp_path):
    readme = (ARTIFACT / "README.md").read_text(encoding="utf-8")
    start = readme.index("read_extra_pids() {")
    end = readme.index("\n}", start) + 2
    reader = readme[start:end]
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_find = fake_bin / "find"
    fake_find.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_find.chmod(0o755)
    script = (
        "set -euo pipefail\n"
        f"{reader}\n"
        "if read_extra_pids /does-not-exist 123; then\n"
        "  printf 'read-error-treated-as-empty\\n'\n"
        "  exit 1\n"
        "fi\n"
        "printf 'read-error-failed-closed\\n'\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout == "read-error-failed-closed\n"


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
        "INSERT INTO task_runs VALUES (1, ?, ?, 'blocked', ?)",
        ("task-1", "fixture-worker", _TEST_LIFECYCLE_BASE),
    )
    conn.execute(
        "INSERT INTO task_events VALUES (1, ?, 'blocked', NULL, ?)",
        ("task-1", _TEST_LIFECYCLE_BASE - 1),
    )
    conn.execute(
        "INSERT INTO task_events VALUES (2, ?, 'unblocked', NULL, ?)",
        ("task-1", _TEST_LIFECYCLE_BASE + 1),
    )
    conn.commit()
    return conn


def test_same_owner_requeue_ignores_later_comment_but_not_new_lifecycle_state():
    conn = _requeue_connection()
    conn.execute(
        "INSERT INTO task_events VALUES (3, ?, 'commented', ?, ?)",
        ("task-1", "existing pull request", _TEST_LIFECYCLE_BASE + 2),
    )
    conn.execute(
        "INSERT INTO task_events VALUES (4, ?, 'respawn_guarded', ?, ?)",
        (
            "task-1",
            json.dumps({"reason": "active_pr"}),
            _TEST_LIFECYCLE_BASE + 3,
        ),
    )
    conn.commit()
    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is True

    conn.execute(
        "INSERT INTO task_events VALUES (5, ?, 'status', ?, ?)",
        (
            "task-1",
            json.dumps({"status": "running"}),
            _TEST_LIFECYCLE_BASE + 4,
        ),
    )
    conn.commit()
    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is False

    conn.close()


def test_material_specification_is_same_owner_readmission_authority():
    conn = _requeue_connection()
    conn.execute("DELETE FROM task_events WHERE id = 2")
    payload = {
        "changed_fields": ["title", "body"],
        "previous_status": "blocked",
        "status": "ready",
    }
    conn.execute(
        "INSERT INTO task_events VALUES (2, ?, 'specified', ?, ?)",
        ("task-1", json.dumps(payload), _TEST_LIFECYCLE_BASE + 1),
    )
    conn.commit()
    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is True

    for invalid in (
        {**payload, "changed_fields": ["title"]},
        {**payload, "previous_status": "ready"},
        {**payload, "status": "running"},
        {**payload, "changed_fields": "body"},
    ):
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = 2",
            (json.dumps(invalid),),
        )
        conn.commit()
        assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is False

    conn.close()


@pytest.mark.parametrize(
    ("table", "column", "row_id"),
    [
        ("task_events", "created_at", 2),
        ("task_runs", "ended_at", 1),
    ],
)
def test_same_owner_requeue_rejects_malformed_lifecycle_timestamps(
    table, column, row_id
):
    conn = _requeue_connection()
    conn.execute(
        f"UPDATE {table} SET {column} = 'not-a-timestamp' WHERE id = ?",
        (row_id,),
    )
    conn.commit()

    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is False
    conn.close()


@pytest.mark.parametrize("outcome", ["blocked", "completed"])
def test_same_owner_requeue_rejects_newer_null_ended_run(outcome):
    conn = _requeue_connection()
    conn.execute(
        "INSERT INTO task_runs "
        "(id, task_id, profile, outcome, ended_at) VALUES (2, ?, ?, ?, NULL)",
        ("task-1", "fixture-worker", outcome),
    )
    conn.commit()

    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is False
    conn.close()


def test_same_owner_requeue_rejects_future_lifecycle_timestamps():
    conn = _requeue_connection()
    future = int(time.time()) + 1_000_000
    conn.execute("UPDATE task_runs SET ended_at = ? WHERE id = 1", (future,))
    conn.execute("UPDATE task_events SET created_at = ? WHERE id = 2", (future + 1,))
    conn.commit()

    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is False
    conn.close()


def test_same_owner_requeue_rejects_sqlite_boolean_timestamps():
    conn = _requeue_connection()
    conn.execute("UPDATE task_runs SET ended_at = ? WHERE id = 1", (True,))
    conn.execute("UPDATE task_events SET created_at = ? WHERE id = 2", (True,))
    conn.commit()

    assert conn.execute(
        "SELECT ended_at FROM task_runs WHERE id = 1"
    ).fetchone()["ended_at"] == 1
    assert conn.execute(
        "SELECT created_at FROM task_events WHERE id = 2"
    ).fetchone()["created_at"] == 1
    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is False
    conn.close()


@pytest.mark.parametrize(
    "malformed_payload",
    [
        "",
        "{",
        "null",
        "[]",
        '"ready"',
        "{}",
        json.dumps({"status": []}),
        json.dumps({"status": {}}),
    ],
)
def test_same_owner_requeue_rejects_malformed_promoted_payload(malformed_payload):
    conn = _requeue_connection()
    conn.execute(
        "UPDATE task_events SET kind = 'promoted', payload = ? WHERE id = 2",
        (malformed_payload,),
    )
    conn.commit()

    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is False
    conn.close()


@pytest.mark.parametrize("payload", [None, json.dumps({"status": "ready"})])
def test_same_owner_requeue_accepts_well_formed_ready_promotion(payload):
    conn = _requeue_connection()
    conn.execute(
        "UPDATE task_events SET kind = 'promoted', payload = ? WHERE id = 2",
        (payload,),
    )
    conn.commit()

    assert native_boundary.same_owner_requeue_is_authorized(conn, "task-1") is True
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


def test_staged_material_respecification_bypasses_active_pr_guard(tmp_path):
    runtime = _staged_runtime()
    script = textwrap.dedent(
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
                title="Canonical review continuation",
                body="Review the exact existing pull request.",
                assignee="default",
                created_by="fixture",
            )
            running = kb.claim_task(conn, task_id, claimer="fixture-owner:1")
            assert running is not None
            assert kb.block_task(
                conn,
                task_id,
                reason="worker classified an internal gate as capability",
                kind="capability",
                expected_run_id=running.current_run_id,
            )
            landing = kb.respecify_idle_task(
                conn,
                task_id,
                body="Materially corrected exact-head review contract.",
                author="fixture-controller",
            )
            kb.add_comment(conn, task_id, "fixture-controller", __FIXTURE_PR_URL__)
            guard = kb.check_respawn_guard(conn, task_id, lane="ready")
            result = kb.dispatch_once(
                conn,
                dry_run=True,
                max_spawn=1,
                reconcile_orphans=False,
            )
            print(json.dumps({
                "landing": landing,
                "guard": guard,
                "spawned": [item[0] for item in result.spawned],
            }))
        """
    ).replace("__FIXTURE_PR_URL__", repr(FIXTURE_PR_URL))
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=runtime,
        env=_private_environment(
            runtime,
            tmp_path,
            db=tmp_path / "specified-requeue.db",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    probe = json.loads(result.stdout)
    assert probe["landing"] == "ready"
    assert probe["guard"] is None
    assert len(probe["spawned"]) == 1


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


@pytest.mark.parametrize(
    ("outcome", "ended_at", "expected_guard"),
    [
        ("rate_limited", "not-a-timestamp", "rate_limit_cooldown"),
        ("rate_limited", None, "rate_limit_cooldown"),
        ("completed", "not-a-timestamp", "recent_success"),
        ("completed", None, "recent_success"),
        ("completed", True, "recent_success"),
        ("blocked", "not-a-timestamp", "recent_success"),
        ("blocked", None, "recent_success"),
        ("failed", "not-a-timestamp", "recent_success"),
        ("failed", None, "recent_success"),
    ],
)
def test_staged_dispatch_fails_closed_on_malformed_run_timestamp(
    tmp_path, outcome, ended_at, expected_guard
):
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
        with kb.connect_closing(db) as conn:
            task_id = kb.create_task(
                conn,
                title="Malformed run timestamp",
                body="The production guard must fail closed without raising.",
                assignee="default",
                created_by="fixture",
            )
            now = int(time.time())
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, ?, 'done', ?, ?, ?)",
                (task_id, "default", now, __ENDED_AT__, __OUTCOME__),
            )
            conn.commit()
            guard = kb.check_respawn_guard(conn, task_id, lane="ready")
            result = kb.dispatch_once(
                conn,
                dry_run=True,
                max_spawn=1,
                reconcile_orphans=False,
            )
            print(json.dumps({
                "task_id": task_id,
                "guard": guard,
                "guarded": result.respawn_guarded,
                "spawned": [item[0] for item in result.spawned],
            }))
        """
    ).replace("__OUTCOME__", repr(outcome)).replace("__ENDED_AT__", repr(ended_at))
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=runtime,
        env=_private_environment(
            runtime,
            tmp_path,
            db=tmp_path / f"malformed-{outcome}.db",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    probe = json.loads(result.stdout)
    assert probe["guard"] == expected_guard
    assert probe["guarded"] == [[probe["task_id"], expected_guard]]
    assert probe["spawned"] == []


@pytest.mark.parametrize(
    ("kind", "payload", "expected_guard"),
    [
        ("promoted", None, None),
        ("promoted", json.dumps({"status": "ready"}), None),
        ("status", json.dumps({"status": "ready"}), None),
        ("unblocked", None, None),
        (
            "reclaimed",
            json.dumps(
                {
                    "manual": True,
                    "prev_lock": "fixture-lock",
                    "retry_status": "ready",
                }
            ),
            None,
        ),
        (
            "reclaimed",
            json.dumps(
                {
                    "stale_lock": "fixture-lock",
                    "claim_expires": _TEST_LIFECYCLE_BASE,
                    "now": _TEST_LIFECYCLE_BASE + 1,
                    "retry_status": "ready",
                }
            ),
            None,
        ),
        ("promoted", "", "recent_success"),
        ("promoted", "{", "recent_success"),
        ("promoted", "null", "recent_success"),
        ("promoted", "[]", "recent_success"),
        ("promoted", '"ready"', "recent_success"),
        ("promoted", "{}", "recent_success"),
        ("promoted", json.dumps({"status": "running"}), "recent_success"),
        ("promoted", json.dumps({"status": []}), "recent_success"),
        ("promoted", json.dumps({"status": {}}), "recent_success"),
        ("status", json.dumps({"status": []}), "recent_success"),
        ("status", json.dumps({"status": {}}), "recent_success"),
        ("unblocked", "{", "recent_success"),
        ("unblocked", "null", "recent_success"),
        ("unblocked", "[]", "recent_success"),
        ("unblocked", '"ready"', "recent_success"),
        ("unblocked", "{}", "recent_success"),
        ("unblocked", json.dumps({"status": "ready"}), "recent_success"),
        ("reclaimed", "{", "recent_success"),
        ("reclaimed", "null", "recent_success"),
        ("reclaimed", "[]", "recent_success"),
        ("reclaimed", '"ready"', "recent_success"),
        ("reclaimed", "{}", "recent_success"),
        (
            "reclaimed",
            json.dumps({"retry_status": "ready"}),
            "recent_success",
        ),
    ],
)
def test_staged_dispatch_validates_requeue_payload(
    tmp_path, kind, payload, expected_guard
):
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
        with kb.connect_closing(db) as conn:
            task_id = kb.create_task(
                conn,
                title="Requeue payload validation",
                body="Only valid native requeue evidence may bypass recent success.",
                assignee="default",
                created_by="fixture",
            )
            now = int(time.time())
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, ?, 'done', ?, ?, 'completed')",
                (task_id, "default", now, now),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, __KIND__, ?, ?)",
                (task_id, __PAYLOAD__, now),
            )
            conn.commit()
            guard = kb.check_respawn_guard(conn, task_id, lane="ready")
            result = kb.dispatch_once(
                conn,
                dry_run=True,
                max_spawn=1,
                reconcile_orphans=False,
            )
            print(json.dumps({
                "task_id": task_id,
                "guard": guard,
                "guarded": result.respawn_guarded,
                "spawned": [item[0] for item in result.spawned],
            }))
        """
    ).replace("__KIND__", repr(kind)).replace("__PAYLOAD__", repr(payload))
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=runtime,
        env=_private_environment(
            runtime,
            tmp_path,
            db=tmp_path / f"{kind}-payload.db",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    probe = json.loads(result.stdout)
    assert probe["guard"] == expected_guard
    if expected_guard is None:
        assert probe["guarded"] == []
        assert probe["spawned"] == [probe["task_id"]]
    else:
        assert probe["guarded"] == [[probe["task_id"], expected_guard]]
        assert probe["spawned"] == []


def test_staged_dispatch_fails_closed_on_malformed_requeue_timestamp(tmp_path):
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
        with kb.connect_closing(db) as conn:
            def completed_task(title):
                task_id = kb.create_task(
                    conn,
                    title=title,
                    body="Malformed requeue evidence must not bypass recent success.",
                    assignee="default",
                    created_by="fixture",
                )
                now = int(time.time())
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, profile, status, started_at, ended_at, outcome) "
                    "VALUES (?, ?, 'done', ?, ?, 'completed')",
                    (task_id, "default", now, now),
                )
                return task_id

            malformed = completed_task("Malformed lifecycle requeue")
            kb._append_event(conn, malformed, "status", {"status": "ready"})
            conn.execute(
                "UPDATE task_events SET created_at = 'not-a-timestamp' "
                "WHERE task_id = ? AND kind = 'status'",
                (malformed,),
            )

            observation = completed_task("Malformed observation timestamp")
            kb._append_event(
                conn,
                observation,
                "respawn_guarded",
                {"reason": "active_pr"},
            )
            conn.execute(
                "UPDATE task_events SET created_at = 'not-a-timestamp' "
                "WHERE task_id = ? AND kind = 'respawn_guarded'",
                (observation,),
            )
            conn.commit()

            guards = {
                "malformed": kb.check_respawn_guard(conn, malformed, lane="ready"),
                "observation": kb.check_respawn_guard(conn, observation, lane="ready"),
            }
            result = kb.dispatch_once(
                conn,
                dry_run=True,
                max_spawn=2,
                reconcile_orphans=False,
            )
            print(json.dumps({
                "guards": guards,
                "guarded": result.respawn_guarded,
                "spawned": [item[0] for item in result.spawned],
            }))
        """
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=runtime,
        env=_private_environment(
            runtime,
            tmp_path,
            db=tmp_path / "malformed-requeue.db",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    probe = json.loads(result.stdout)
    assert probe["guards"] == {
        "malformed": "recent_success",
        "observation": "recent_success",
    }
    assert sorted(reason for _task_id, reason in probe["guarded"]) == [
        "recent_success",
        "recent_success",
    ]
    assert probe["spawned"] == []


def test_staged_dispatch_fails_closed_on_future_lifecycle_timestamps(tmp_path):
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
        with kb.connect_closing(db) as conn:
            now = int(time.time())
            future = now + 1_000_000

            future_terminal = kb.create_task(
                conn,
                title="Future terminal timestamp",
                body="Future terminal evidence must fail closed.",
                assignee="default",
                created_by="fixture",
            )
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, ?, 'done', ?, ?, 'completed')",
                (future_terminal, "default", now, future),
            )

            future_transition = kb.create_task(
                conn,
                title="Future transition timestamp",
                body="Future requeue evidence must not bypass recent success.",
                assignee="default",
                created_by="fixture",
            )
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, ?, 'done', ?, ?, 'completed')",
                (future_transition, "default", now, now),
            )
            kb._append_event(
                conn,
                future_transition,
                "status",
                {"status": "ready"},
            )
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = 'status'",
                (future, future_transition),
            )
            conn.commit()

            guards = {
                "future_terminal": kb.check_respawn_guard(
                    conn, future_terminal, lane="ready"
                ),
                "future_transition": kb.check_respawn_guard(
                    conn, future_transition, lane="ready"
                ),
            }
            result = kb.dispatch_once(
                conn,
                dry_run=True,
                max_spawn=2,
                reconcile_orphans=False,
            )
            print(json.dumps({
                "guards": guards,
                "guarded": result.respawn_guarded,
                "spawned": [item[0] for item in result.spawned],
            }))
        """
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=runtime,
        env=_private_environment(
            runtime,
            tmp_path,
            db=tmp_path / "future-lifecycle.db",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    probe = json.loads(result.stdout)
    assert probe["guards"] == {
        "future_terminal": "recent_success",
        "future_transition": "recent_success",
    }
    assert sorted(reason for _task_id, reason in probe["guarded"]) == [
        "recent_success",
        "recent_success",
    ]
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
