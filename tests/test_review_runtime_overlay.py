from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

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


def _clear_delegated_context(monkeypatch) -> None:
    delegated_keys = {
        "HERMES_DELEGATION_PARENT_ID",
        "HERMES_DELEGATION_DEPTH",
        "HERMES_PARENT_SESSION_ID",
    }
    for key in tuple(os.environ):
        if key.startswith("HERMES_DELEGATED_") or key in delegated_keys:
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _private_fixture_is_not_a_delegated_worker(monkeypatch) -> None:
    _clear_delegated_context(monkeypatch)


def test_review_manifest_pins_policy_patch_and_targets():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    expected_hash = hashlib.sha256(PATCH_PATH.read_bytes()).hexdigest()

    assert manifest["schema"] == "factory.review-runtime.v1"
    assert manifest["native_boundary_schema"] == "factory.native-boundary.v1"
    assert "native_manifest_sha256" not in manifest
    assert (
        manifest["native_artifact_identity_sha256"]
        == "feaffd82904b7cce43285ec0a038a3e7c8f85f467cb8c99ae07075a6f59918ab"
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
    assert manifest["policy"]["canonical_recovery"] == {
        "terminal_failure_run_fence": True,
        "one_leaf_per_lane": True,
        "dependency_resolution": "canonical_leaf",
    }
    assert manifest["patches"]["patches/002-review-runtime.patch"] == expected_hash
    assert manifest["patched_paths"] == [
        "hermes_cli/kanban_db.py",
        "gateway/kanban_watchers.py",
        "tui_gateway/server.py",
        "tools/terminal_tool.py",
        "hermes_cli/kanban.py",
        "tools/kanban_tools.py",
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
    assert "CREATE TABLE IF NOT EXISTS canonical_task_lanes" in patch_text
    assert "CREATE TABLE IF NOT EXISTS task_supersessions" in patch_text
    assert "def supersede_task(" in patch_text
    assert 'name="kanban_supersede"' in patch_text
    assert "retry queued" in patch_text
    assert "task is {retry_status}, no retry" in patch_text
    assert '"final_status": "blocked"' in patch_text
    assert '\n+    hermes_bin = _safe_which_no_cwd("hermes")' not in patch_text
    assert 'safe_prefixes = ("LC_",)' in patch_text
    assert all(
        not line.startswith("+") or not line.rstrip("\r\n").endswith((" ", "\t"))
        for line in patch_text.splitlines(keepends=True)
    )


def _synthetic_native_manifest(root: Path) -> dict[str, Any]:
    output = root / "native-runtime"
    return {
        "artifact_root": str(root / "artifact"),
        "artifact_version": "1.0.0",
        "excluded_copy_entries": [".git", "*.pyc"],
        "import_probe": {
            "kanban_db": str(output / "hermes_cli/kanban_db.py"),
            "kanban_specify": str(output / "hermes_cli/kanban_specify.py"),
            "native_boundary": str(output / "hermes_cli/native_boundary.py"),
        },
        "output_runtime": str(output),
        "patched_paths": {"hermes_cli/kanban_db.py": "b" * 64},
        "patches": {"patches/001-native-boundary.patch": "c" * 64},
        "schema": "factory.native-boundary.v1",
        "source_files": {"hermes_cli/kanban_db.py": "a" * 64},
        "source_runtime": str(root / "source-runtime"),
        "source_tree": {"hermes_cli/kanban_db.py": "a" * 64},
        "staged_tree": {"hermes_cli/kanban_db.py": "b" * 64},
        "verification": {"checks": ["syntax", "import_provenance"]},
    }


def test_native_artifact_identity_is_relocatable_but_content_bound(tmp_path):
    builder = _builder_module()
    first = _synthetic_native_manifest(tmp_path / "first")
    relocated = _synthetic_native_manifest(tmp_path / "relocated")

    assert builder._native_artifact_identity_sha256(first) == (
        builder._native_artifact_identity_sha256(relocated)
    )

    tampered = copy.deepcopy(relocated)
    tampered["staged_tree"]["hermes_cli/kanban_db.py"] = "d" * 64
    assert builder._native_artifact_identity_sha256(first) != (
        builder._native_artifact_identity_sha256(tampered)
    )


def test_manifest_destination_rejects_overlap_existing_and_symlink(tmp_path):
    builder = _builder_module()
    protected = tmp_path / "native"
    protected.mkdir()

    with pytest.raises(SystemExit, match="overlaps protected input/output"):
        builder._validate_manifest_destination(
            protected / "review.manifest.json", (protected,)
        )

    existing = tmp_path / "existing.manifest.json"
    existing.write_text("sentinel", encoding="utf-8")
    with pytest.raises(SystemExit, match="already exists"):
        builder._validate_manifest_destination(existing, (protected,))
    assert existing.read_text(encoding="utf-8") == "sentinel"

    link = tmp_path / "link.manifest.json"
    link.symlink_to(existing)
    with pytest.raises(SystemExit, match="already exists"):
        builder._validate_manifest_destination(link, (protected,))

    outside = tmp_path / "outside"
    outside.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit, match="parent contains a symlink"):
        builder._validate_manifest_destination(
            parent_link / "review.manifest.json", (protected,)
        )
    assert not (outside / "review.manifest.json").exists()

    allowed = tmp_path / "new.manifest.json"
    assert builder._validate_manifest_destination(allowed, (protected,)) == allowed


def test_exclusive_manifest_write_never_overwrites_and_is_private(tmp_path):
    builder = _builder_module()
    destination = tmp_path / "review.manifest.json"
    builder._write_json_exclusive(destination, {"schema": "fixture"})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"schema": "fixture"}
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert destination.stat().st_nlink == 1

    with pytest.raises(SystemExit, match="already exists"):
        builder._write_json_exclusive(destination, {"schema": "overwritten"})
    assert json.loads(destination.read_text(encoding="utf-8")) == {"schema": "fixture"}


def test_exclusive_manifest_write_is_not_redirected_by_parent_replacement(
    tmp_path, monkeypatch
):
    builder = _builder_module()
    parent = tmp_path / "manifest-parent"
    parent.mkdir()
    moved_parent = tmp_path / "original-parent"
    attacker_target = tmp_path / "attacker-target"
    attacker_target.mkdir()
    destination = parent / "review.manifest.json"
    real_link = builder.os.link

    def replace_parent_before_publish(source, target, **kwargs):
        parent.rename(moved_parent)
        parent.symlink_to(attacker_target, target_is_directory=True)
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(builder.os, "link", replace_parent_before_publish)

    with pytest.raises(SystemExit, match="parent changed or became unsafe"):
        builder._write_json_exclusive(destination, {"schema": "fixture"})

    assert not (attacker_target / destination.name).exists()
    assert not (moved_parent / destination.name).exists()


def test_patch_application_uses_trusted_git_and_scrubbed_environment(
    tmp_path, monkeypatch
):
    builder = _builder_module()
    output = tmp_path / "output"
    output.mkdir()
    patch_path = tmp_path / "review.patch"
    patch_path.write_text("not executed by the test", encoding="utf-8")
    captured = []

    monkeypatch.setattr(
        builder, "_patch_targets", lambda _path: tuple(builder.TARGET_PATHS)
    )
    monkeypatch.setattr(
        builder, "_trusted_git_executable", lambda: Path("/usr/bin/git")
    )

    def fake_run(command, **kwargs):
        captured.append((list(command), kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    builder._apply_patch(output, patch_path)

    assert len(captured) == 2
    for command, kwargs in captured:
        assert command[0] == "/usr/bin/git"
        assert kwargs["cwd"] == output
        assert kwargs["env"] == builder._GIT_ENVIRONMENT


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
        result["native_artifact_identity_sha256"]
        == (
            json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))[
                "native_artifact_identity_sha256"
            ]
        )
    )
    assert result["native_source_tree_sha256"]
    assert result["native_staged_tree_sha256"]
    assert result["syntax_probe"] == list(builder.TARGET_PATHS)


@pytest.mark.parametrize("failure_phase", ["patch", "manifest"])
def test_force_build_restores_prior_runtime_on_failure(
    tmp_path, monkeypatch, failure_phase
):
    native_runtime_value = os.environ.get("FACTORY_NATIVE_BOUNDARY_RUNTIME")
    native_manifest_value = os.environ.get("FACTORY_NATIVE_BOUNDARY_MANIFEST")
    if not native_runtime_value or not native_manifest_value:
        pytest.skip(
            "set FACTORY_NATIVE_BOUNDARY_RUNTIME and FACTORY_NATIVE_BOUNDARY_MANIFEST"
        )

    builder = _builder_module()
    output = tmp_path / "review-runtime"
    output.mkdir()
    sentinel = output / "prior-runtime.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    prior_identity = builder._directory_identity(output)
    generated = tmp_path / "review-runtime.manifest.json"

    if failure_phase == "patch":
        monkeypatch.setattr(
            builder,
            "_apply_patch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                SystemExit("synthetic patch failure")
            ),
        )
    else:
        monkeypatch.setattr(
            builder,
            "_write_json_exclusive",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                SystemExit("synthetic manifest failure")
            ),
        )

    with pytest.raises(SystemExit, match=f"synthetic {failure_phase} failure"):
        builder.build(
            Path(native_runtime_value),
            Path(native_manifest_value),
            output,
            generated,
            force=True,
        )

    assert builder._directory_identity(output) == prior_identity
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert not generated.exists()
    assert not list(tmp_path.glob(".review-runtime.stage.*"))
    assert not list(tmp_path.glob(".review-runtime.rollback.*"))


@pytest.mark.parametrize("symlink_kind", ["output", "parent"])
def test_force_build_rejects_symlinked_runtime_destination_before_staging(
    tmp_path, monkeypatch, symlink_kind
):
    builder = _builder_module()
    native_runtime = tmp_path / "native"
    native_runtime.mkdir()
    source_runtime = tmp_path / "source"
    source_runtime.mkdir()
    native_manifest = tmp_path / "native.manifest.json"
    native_manifest.write_text("{}", encoding="utf-8")
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    real_output = real_parent / "review-runtime"
    real_output.mkdir()
    sentinel = real_output / "sentinel"
    sentinel.write_text("preserve-me", encoding="utf-8")

    if symlink_kind == "output":
        output = tmp_path / "output-alias"
        output.symlink_to(real_output, target_is_directory=True)
    else:
        parent_alias = tmp_path / "parent-alias"
        parent_alias.symlink_to(real_parent, target_is_directory=True)
        output = parent_alias / "review-runtime"

    monkeypatch.setattr(
        builder,
        "_validate_native_input",
        lambda *_args, **_kwargs: {
            "source_runtime": str(source_runtime),
            "verification": {},
        },
    )

    with pytest.raises(SystemExit, match="runtime output path contains a symlink"):
        builder.build(
            native_runtime,
            native_manifest,
            output,
            tmp_path / "review.manifest.json",
            force=True,
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert not list(real_parent.glob(".review-runtime.stage.*"))
    assert not list(real_parent.glob(".review-runtime.rollback.*"))
    assert not (tmp_path / "review.manifest.json").exists()


def test_runtime_output_parent_replacement_cannot_redirect_sibling_reservation(
    tmp_path,
):
    builder = _builder_module()
    parent = tmp_path / "runtime-parent"
    parent.mkdir()
    output = parent / "review-runtime"
    output, directory_fd, output_existed = builder._validate_runtime_output_destination(
        output, force=False
    )
    assert output_existed is False

    moved_parent = tmp_path / "original-runtime-parent"
    attacker_target = tmp_path / "attacker-target"
    attacker_target.mkdir()
    parent.rename(moved_parent)
    parent.symlink_to(attacker_target, target_is_directory=True)
    try:
        with pytest.raises(SystemExit, match="runtime output parent changed"):
            builder._reserve_private_sibling_directory(
                output, "stage", directory_fd=directory_fd
            )
    finally:
        os.close(directory_fd)

    assert not list(attacker_target.iterdir())
    assert not list(moved_parent.iterdir())


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


def test_worker_entrypoint_and_environment_ignore_ambient_injection(
    tmp_path, monkeypatch
):
    runtime_value = os.environ.get("FACTORY_REVIEW_RUNTIME")
    source_value = os.environ.get("FACTORY_SOURCE_RUNTIME")
    if not runtime_value or not source_value:
        pytest.skip("set FACTORY_REVIEW_RUNTIME and FACTORY_SOURCE_RUNTIME")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_hermes = fake_bin / "hermes"
    fake_hermes.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    fake_hermes.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.delenv("HERMES_BIN", raising=False)

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

        argv = module._resolve_hermes_argv()
        assert argv == [sys.executable, "-m", "hermes_cli.main"]
        child = module._review_worker_env(
            {
                "PATH": str(fake_bin),
                "XDG_CONFIG_HOME": str(tmp_path / "attacker-config"),
                "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
                "GH_REPO": "attacker/example",
                "NATIVE_SECRET_SENTINEL": "must-not-survive",
            },
            runtime_root=tmp_path,
            hermes_argv=argv,
        )
        assert child["PATH"] != str(fake_bin)
        assert child["XDG_RUNTIME_DIR"] == str(tmp_path / "runtime")
        assert "XDG_CONFIG_HOME" not in child
        assert "GH_REPO" not in child
        assert "NATIVE_SECRET_SENTINEL" not in child
    finally:
        sys.modules.pop("hermes_cli.kanban_db", None)
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)


def test_review_timeout_retries_same_card_then_blocks_without_successor(
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

        db_path = tmp_path / "timeout.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        monkeypatch.setattr(module, "_pid_alive", lambda _pid: False)
        module.init_db()
        conn = module.connect()
        task_id = module.create_task(
            conn,
            title="bounded review timeout",
            assignee="reviewer",
            max_runtime_seconds=2400,
        )
        with module.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))

        observed_statuses = []
        for _attempt in range(3):
            claimed = module.claim_review_task(conn, task_id)
            assert claimed is not None and claimed.current_run_id is not None
            with module.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET worker_pid=? WHERE id=?",
                    (999999, task_id),
                )
                conn.execute(
                    "UPDATE task_runs SET started_at=? WHERE id=?",
                    (int(module.time.time()) - 1301, claimed.current_run_id),
                )
            assert module.enforce_max_runtime(
                conn, signal_fn=lambda _pid, _signal: None, failure_limit=3
            ) == [task_id]
            observed_statuses.append(module.get_task(conn, task_id).status)

        assert observed_statuses == ["review", "review", "blocked"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        runs = conn.execute(
            "SELECT outcome, max_runtime_seconds FROM task_runs ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in runs] == [
            ("timed_out", 1200),
            ("timed_out", 1200),
            ("timed_out", 1200),
        ]
        timeout_payloads = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload FROM task_events "
                "WHERE task_id=? AND kind='timed_out' ORDER BY id",
                (task_id,),
            ).fetchall()
        ]
        assert timeout_payloads[-1]["final_status"] == "blocked"
        assert timeout_payloads[-1]["retry_eligible"] is False
    finally:
        sys.modules.pop("hermes_cli.kanban_db", None)
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)


def test_canonical_successor_replaces_obsolete_gate_without_fork(tmp_path, monkeypatch):
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

        db_path = tmp_path / "canonical-recovery.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        module.init_db()
        conn = module.connect()

        predecessor = module.create_task(
            conn, title="timed-out review", assignee="reviewer"
        )
        predecessor_claim = module.claim_task(conn, predecessor, claimer="old-reviewer")
        assert predecessor_claim is not None
        predecessor_run_id = predecessor_claim.current_run_id
        assert predecessor_run_id is not None
        assert module.block_task(
            conn,
            predecessor,
            reason="review timed out",
            expected_run_id=predecessor_run_id,
        )
        activation = module.create_task(
            conn,
            title="activation",
            assignee="orchestrator",
            parents=(predecessor,),
        )
        successor = module.create_task(
            conn,
            title="exact-head successor review",
            assignee="reviewer",
            parents=(predecessor,),
        )

        relation = module.supersede_task(
            conn,
            predecessor,
            successor,
            lane_key="factory:24:pr28:review:exact-head",
            expected_predecessor_run_id=predecessor_run_id,
            reason="new exact-head review replaces terminal timeout",
            evidence={"candidate_sha": "a" * 40, "verdict": "pending"},
        )
        assert relation["changed"] is True
        assert relation["removed_obsolete_parent_edge"] is True
        assert module.canonical_task_chain(conn, predecessor) == (
            predecessor,
            successor,
        )
        assert module.get_task(conn, predecessor).status == "archived"
        assert module.get_task(conn, successor).status == "ready"
        assert module.get_task(conn, activation).status == "todo"
        assert module.parent_ids(conn, activation) == [predecessor]

        fork = module.create_task(conn, title="duplicate recovery", assignee="reviewer")
        with pytest.raises(RuntimeError, match="different canonical successor"):
            module.supersede_task(
                conn,
                predecessor,
                fork,
                lane_key="factory:24:pr28:review:exact-head",
                expected_predecessor_run_id=predecessor_run_id,
                reason="attempted fork",
                evidence={"candidate_sha": "b" * 40},
            )

        successor_claim = module.claim_task(
            conn, successor, claimer="canonical-reviewer"
        )
        assert successor_claim is not None
        assert successor_claim.current_run_id is not None
        assert module.complete_task(
            conn,
            successor,
            summary="APPROVE exact head",
            metadata={"candidate_sha": "a" * 40},
            expected_run_id=successor_claim.current_run_id,
        )
        assert module.get_task(conn, activation).status == "ready"
        assert module.parent_results(conn, activation) == [
            (successor, "APPROVE exact head")
        ]
        context = module.build_worker_context(conn, activation)
        assert successor in context
        assert f"canonical successor for {predecessor}" in context
        assert "APPROVE exact head" in context
        persisted = conn.execute(
            "SELECT lane_key, generation FROM canonical_task_lanes"
        ).fetchone()
        assert tuple(persisted) == (
            "factory:24:pr28:review:exact-head",
            1,
        )
    finally:
        sys.modules.pop("hermes_cli.kanban_db", None)
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)


def test_review_evidence_deadline_survives_event_loss_and_reconnect(
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

        db_path = tmp_path / "deadline.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        module.init_db()
        conn = module.connect()
        task_id = module.create_task(
            conn, title="persistent review deadline", assignee="reviewer"
        )
        with module.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        claimed = module.claim_review_task(conn, task_id, claimer="reviewer")
        assert claimed is not None and claimed.current_run_id is not None
        expected_deadline = claimed.evidence_deadline
        assert expected_deadline is not None

        with module.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload='{}' "
                "WHERE task_id=? AND run_id=? AND kind='claimed'",
                (task_id, claimed.current_run_id),
            )
        conn.close()
        conn = module.connect()
        rehydrated = module.get_task(conn, task_id)
        assert rehydrated is not None
        assert rehydrated.evidence_deadline == expected_deadline
        assert (
            module._claimed_evidence_deadline(conn, task_id, claimed.current_run_id)
            == expected_deadline
        )
    finally:
        sys.modules.pop("hermes_cli.kanban_db", None)
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)


@pytest.mark.parametrize(
    "deadline_kind",
    ["missing", "malformed", "nan", "infinite", "extended"],
)
def test_review_completion_fails_closed_for_invalid_persisted_deadline(
    tmp_path, monkeypatch, deadline_kind
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

        db_path = tmp_path / f"deadline-{deadline_kind}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        module.init_db()
        conn = module.connect()
        task_id = module.create_task(
            conn, title="invalid review deadline", assignee="reviewer"
        )
        with module.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        claimed = module.claim_review_task(conn, task_id, claimer="reviewer")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        started_at = conn.execute(
            "SELECT started_at FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()["started_at"]
        invalid_deadline = {
            "missing": None,
            "malformed": "not-a-deadline",
            "nan": "nan",
            "infinite": float("inf"),
            "extended": float(started_at + 3600),
        }[deadline_kind]
        with module.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET evidence_deadline=? WHERE id=?",
                (invalid_deadline, run_id),
            )

        with pytest.raises(module.ReviewIncompleteError, match="REVIEW-INCOMPLETE"):
            module.complete_task(
                conn,
                task_id,
                summary="APPROVE exact head",
                expected_run_id=run_id,
                fire_lifecycle_hook=False,
            )

        task_row = conn.execute(
            "SELECT status, block_kind, current_run_id FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        assert tuple(task_row) == ("blocked", "needs_input", None)
        run_row = conn.execute(
            "SELECT status, outcome, error FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert run_row["status"] == "blocked"
        assert run_row["outcome"] == "blocked"
        assert str(run_row["error"]).startswith("REVIEW-INCOMPLETE:")
        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id=? AND kind='review_incomplete' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
        assert json.loads(event["payload"])["code"] == "REVIEW-INCOMPLETE"
    finally:
        sys.modules.pop("hermes_cli.kanban_db", None)
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)


@pytest.mark.parametrize(
    "deadline_kind",
    ["missing", "malformed", "nan", "infinite", "extended"],
)
def test_worker_boundary_rejects_invalid_persisted_deadline_before_spawn(
    tmp_path, monkeypatch, deadline_kind
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

        db_path = tmp_path / f"worker-deadline-{deadline_kind}.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        module.init_db()
        conn = module.connect()
        task_id = module.create_task(
            conn, title="worker deadline gate", assignee="reviewer"
        )
        with module.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        claimed = module.claim_review_task(conn, task_id, claimer="reviewer")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        started_at = conn.execute(
            "SELECT started_at FROM task_runs WHERE id=?", (run_id,)
        ).fetchone()["started_at"]
        invalid_deadline = {
            "missing": None,
            "malformed": "not-a-deadline",
            "nan": "nan",
            "infinite": float("inf"),
            "extended": float(started_at + 3600),
        }[deadline_kind]
        with module.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET evidence_deadline=? WHERE id=?",
                (invalid_deadline, run_id),
            )

        rehydrated = module.get_task(conn, task_id)
        assert rehydrated is not None
        policy = module.resolve_runtime_policy(module.RUNTIME_CLASS_REVIEW)
        with pytest.raises(module.ReviewIncompleteError, match="REVIEW-INCOMPLETE"):
            module._validated_worker_evidence_deadline(rehydrated, policy)
    finally:
        sys.modules.pop("hermes_cli.kanban_db", None)
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)


@pytest.mark.parametrize(
    ("deadline", "started_at", "budget"),
    [
        ("nan", "1000", "600"),
        ("inf", "1000", "600"),
        ("4600", "1000", "600"),
        ("1600.5", None, "600"),
        ("1600.5", "not-a-time", "600"),
        ("1600.5", "1000", "0"),
    ],
)
def test_terminal_boundary_rejects_invalid_or_extended_evidence_window(
    monkeypatch, deadline, started_at, budget
):
    runtime_value = os.environ.get("FACTORY_REVIEW_RUNTIME")
    source_value = os.environ.get("FACTORY_SOURCE_RUNTIME")
    if not runtime_value or not source_value:
        pytest.skip("set FACTORY_REVIEW_RUNTIME and FACTORY_SOURCE_RUNTIME")

    sys.path.insert(0, runtime_value)
    sys.path.insert(1, source_value)
    module_name = "factory_review_terminal_tool_test"
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, Path(runtime_value) / "tools/terminal_tool.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        monkeypatch.setenv("HERMES_KANBAN_EVIDENCE_DEADLINE", deadline)
        if started_at is None:
            monkeypatch.delenv("HERMES_KANBAN_EVIDENCE_STARTED_AT", raising=False)
        else:
            monkeypatch.setenv("HERMES_KANBAN_EVIDENCE_STARTED_AT", started_at)
        monkeypatch.setenv("HERMES_KANBAN_EVIDENCE_BUDGET_SECONDS", budget)
        monkeypatch.setattr(module.time, "time", lambda: 1200.0)

        assert module._kanban_evidence_remaining() == 0.0
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)


def test_terminal_boundary_accepts_exact_valid_evidence_window(monkeypatch):
    runtime_value = os.environ.get("FACTORY_REVIEW_RUNTIME")
    source_value = os.environ.get("FACTORY_SOURCE_RUNTIME")
    if not runtime_value or not source_value:
        pytest.skip("set FACTORY_REVIEW_RUNTIME and FACTORY_SOURCE_RUNTIME")

    sys.path.insert(0, runtime_value)
    sys.path.insert(1, source_value)
    module_name = "factory_review_terminal_tool_valid_test"
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, Path(runtime_value) / "tools/terminal_tool.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        monkeypatch.setenv("HERMES_KANBAN_EVIDENCE_DEADLINE", "1600.5")
        monkeypatch.setenv("HERMES_KANBAN_EVIDENCE_STARTED_AT", "1000")
        monkeypatch.setenv("HERMES_KANBAN_EVIDENCE_BUDGET_SECONDS", "600")
        monkeypatch.setattr(module.time, "time", lambda: 1200.0)

        assert module._kanban_evidence_remaining() == pytest.approx(400.5)
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)


def test_evidence_recovery_claim_persists_class_and_expiry_blocks_completion(
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

        db_path = tmp_path / "evidence-recovery-class.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        module.init_db()
        conn = module.connect()
        task_id = module.create_task(
            conn,
            title="explicit evidence recovery",
            assignee="reviewer",
            runtime_class=module.RUNTIME_CLASS_IMPLEMENTATION,
        )
        claimed = module.claim_evidence_recovery_task(
            conn, task_id, claimer="recovery-reviewer"
        )
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        row = conn.execute(
            "SELECT runtime_class FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert row["runtime_class"] == module.RUNTIME_CLASS_EVIDENCE_RECOVERY

        with module.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET evidence_deadline=0 WHERE id=?", (run_id,)
            )

        with pytest.raises(module.ReviewIncompleteError, match="REVIEW-INCOMPLETE"):
            module.complete_task(
                conn,
                task_id,
                summary="APPROVE stale evidence",
                expected_run_id=run_id,
                fire_lifecycle_hook=False,
            )
        final = conn.execute(
            "SELECT status, block_kind, current_run_id FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        assert tuple(final) == ("blocked", "needs_input", None)
    finally:
        sys.modules.pop("hermes_cli.kanban_db", None)
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)


def test_completion_without_explicit_run_id_is_fenced_to_preflight_run(
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

        db_path = tmp_path / "completion-run-fence.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        module.init_db()
        conn = module.connect()
        task_id = module.create_task(conn, title="review race", assignee="reviewer")
        with module.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        claimed = module.claim_review_task(conn, task_id, claimer="old-reviewer")
        assert claimed is not None and claimed.current_run_id is not None
        old_run_id = claimed.current_run_id
        worker_pid = 987654
        with module.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid=? WHERE id=?", (worker_pid, task_id)
            )
        owner = conn.execute(
            "SELECT claim_lock FROM tasks WHERE id=?", (task_id,)
        ).fetchone()["claim_lock"]

        fresh_run_ids: list[int] = []
        real_merge = module._merge_completion_prose_artifacts

        def interleave_timeout_and_reclaim(*args, **kwargs):
            other = module.connect()
            try:
                assert module._settle_timed_out_run(
                    other,
                    task_id=task_id,
                    pid=worker_pid,
                    claim_lock=owner,
                    expected_run_id=old_run_id,
                    elapsed=1301,
                    limit_seconds=1200,
                    killed=True,
                    failure_limit=3,
                )
                fresh = module.claim_review_task(
                    other, task_id, claimer="fresh-reviewer"
                )
                assert fresh is not None and fresh.current_run_id is not None
                fresh_run_ids.append(fresh.current_run_id)
            finally:
                other.close()
            return real_merge(*args, **kwargs)

        monkeypatch.setattr(
            module, "_merge_completion_prose_artifacts", interleave_timeout_and_reclaim
        )
        assert (
            module.complete_task(
                conn,
                task_id,
                summary="APPROVE from stale run",
                fire_lifecycle_hook=False,
            )
            is False
        )

        assert len(fresh_run_ids) == 1
        current = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert tuple(current) == ("running", fresh_run_ids[0])
        fresh_run = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id=?",
            (fresh_run_ids[0],),
        ).fetchone()
        assert tuple(fresh_run) == ("running", None, None)
    finally:
        sys.modules.pop("hermes_cli.kanban_db", None)
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)


def test_terminal_rechecks_deadline_after_environment_creation_before_execute(
    tmp_path, monkeypatch
):
    runtime_value = os.environ.get("FACTORY_REVIEW_RUNTIME")
    source_value = os.environ.get("FACTORY_SOURCE_RUNTIME")
    if not runtime_value or not source_value:
        pytest.skip("set FACTORY_REVIEW_RUNTIME and FACTORY_SOURCE_RUNTIME")

    sys.path.insert(0, runtime_value)
    sys.path.insert(1, source_value)
    module_name = "factory_review_terminal_creation_deadline_test"
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, Path(runtime_value) / "tools/terminal_tool.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        remaining = iter((5.0, 5.0, 0.0))
        executed: list[tuple[str, dict[str, Any]]] = []

        class FakeEnvironment:
            cwd = str(tmp_path)

            def execute(self, command, **kwargs):
                executed.append((command, kwargs))
                return {"output": "ran", "returncode": 0, "cwd_observed": False}

        monkeypatch.setattr(
            module, "_kanban_evidence_remaining", lambda: next(remaining)
        )
        monkeypatch.setattr(module, "_start_cleanup_thread", lambda: None)
        monkeypatch.setattr(module, "resolve_task_overrides", lambda _task_id: {})
        monkeypatch.setattr(
            module,
            "_get_env_config",
            lambda: {
                "env_type": "local",
                "cwd": str(tmp_path),
                "timeout": 30,
                "local_persistent": False,
            },
        )
        created: list[bool] = []

        def create_environment(**_kwargs):
            created.append(True)
            return FakeEnvironment()

        monkeypatch.setattr(module, "_create_environment", create_environment)
        module._active_environments.clear()
        module._creation_locks.clear()

        result = json.loads(
            module.terminal_tool(
                "true",
                timeout=5,
                task_id="deadline-after-create-fixture",
                force=True,
                _host_local=True,
            )
        )
        assert created == [True]
        assert result["code"] == "REVIEW-INCOMPLETE"
        assert executed == []
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(source_value)
        sys.path.remove(runtime_value)
