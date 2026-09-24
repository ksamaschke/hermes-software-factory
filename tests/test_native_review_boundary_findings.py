"""Causal regressions for the current native review-boundary findings.

These tests deliberately load the generated runtime named by
``FACTORY_NATIVE_RUNTIME``.  They skip in an ordinary source-only test run so
that no test silently exercises an installed/live runtime.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_RUNTIME_SETTING = os.environ.get("FACTORY_NATIVE_RUNTIME")
if not _RUNTIME_SETTING:
    pytest.skip(
        "set FACTORY_NATIVE_RUNTIME to the generated candidate for native-boundary regressions",
        allow_module_level=True,
    )

_RUNTIME = Path(_RUNTIME_SETTING)
if not _RUNTIME.is_dir():
    pytest.skip(
        "set FACTORY_NATIVE_RUNTIME to the generated candidate for native-boundary regressions",
        allow_module_level=True,
    )

sys.path.insert(0, str(_RUNTIME))


@pytest.fixture
def kanban_db(monkeypatch, tmp_path):
    """Load the exact candidate module and provide an isolated SQLite board."""
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    for name in ("hermes_cli.kanban_db",):
        sys.modules.pop(name, None)
    module = importlib.import_module("hermes_cli.kanban_db")
    db_path = tmp_path / "kanban.db"
    conn = module.connect(db_path)
    try:
        yield module, conn, db_path
    finally:
        conn.close()


def _native_status(module, conn, task_id: str, status: str) -> None:
    with module._review_native_mutation_authorized(), module.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _review_like_claim(module, conn):
    """Create a native run and protected evidence row for boundary tests."""
    task_id = module.create_task(
        conn,
        title="review boundary fixture",
        body="review boundary fixture",
        assignee="reviewer",
        created_by="implementer",
        initial_status="blocked",
    )
    _native_status(module, conn, task_id, "ready")
    claimed = module.claim_task(conn, task_id, claimer="reviewer")
    assert claimed is not None
    run_id = claimed.current_run_id
    assert isinstance(run_id, int) and run_id > 0
    assert isinstance(claimed.claim_lock, str) and claimed.claim_lock
    assert isinstance(claimed.claim_expires, int) and claimed.claim_expires > 0
    with module._review_native_mutation_authorized(), module.write_txn(conn):
        if hasattr(module, "_review_run_capability_create"):
            module._review_run_capability_create(
                conn,
                task_id=task_id,
                run_id=run_id,
                claim_lock=claimed.claim_lock,
                claim_expires=claimed.claim_expires,
                source_status="ready",
            )
        conn.execute(
            "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, run_id, "standalone_review_packet_claimed", "{}", 1),
        )
    return task_id, claimed


def test_claim_to_spawn_updates_require_the_exact_dispatcher_claim(kanban_db):
    module, conn, _db_path = kanban_db
    task_id, claimed = _review_like_claim(module, conn)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        module.set_workspace_path(conn, task_id, "/tmp/generic-writer")

    module.set_workspace_path(
        conn,
        task_id,
        "/tmp/review-workspace",
        expected_run_id=claimed.current_run_id,
        expected_claim_lock=claimed.claim_lock,
        expected_claim_expires=claimed.claim_expires,
    )
    module._set_worker_pid(
        conn,
        task_id,
        4242,
        expected_run_id=claimed.current_run_id,
        expected_claim_lock=claimed.claim_lock,
        expected_claim_expires=claimed.claim_expires,
    )
    row = conn.execute(
        "SELECT workspace_path, worker_pid FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["workspace_path"] == "/tmp/review-workspace"
    assert row["worker_pid"] == 4242

    with pytest.raises(RuntimeError, match="dispatcher claim changed"):
        module.set_workspace_path(
            conn,
            task_id,
            "/tmp/wrong-run",
            expected_run_id=claimed.current_run_id + 1,
            expected_claim_lock=claimed.claim_lock,
            expected_claim_expires=claimed.claim_expires,
        )


def test_real_non_dry_review_dispatch_finishes_claim_to_spawn(
    kanban_db, tmp_path, monkeypatch
):
    module, conn, _db_path = kanban_db
    home = tmp_path / "home"
    (home / ".hermes" / "profiles" / "implementer").mkdir(parents=True)
    (home / ".hermes" / "profiles" / "reviewer").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes" / "profiles" / "implementer"))
    repo = tmp_path / "review-repo"
    repo.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["/usr/bin/git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-q", "-b", "review-fixture")
    git("config", "user.name", "Factory Test")
    git("config", "user.email", "test@example.invalid")
    git("remote", "add", "origin", "https://github.com/fixture/repository.git")
    (repo / "fixture.txt").write_text("base\n")
    git("add", "fixture.txt")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "fixture.txt").write_text("base\ncandidate\n")
    git("commit", "-qam", "candidate")
    candidate = git("rev-parse", "HEAD")
    scope_sha = hashlib.sha256(b"fixture.txt\0").hexdigest()

    implementation = module.create_task(
        conn,
        title="implementation prerequisite",
        body="done implementation",
        assignee="implementer",
        created_by="coordinator",
        initial_status="blocked",
    )
    _native_status(module, conn, implementation, "ready")
    implementation_claim = module.claim_task(conn, implementation, claimer="implementer")
    assert implementation_claim is not None
    assert module.complete_task(
        conn,
        implementation,
        summary="implementation complete",
        expected_run_id=implementation_claim.current_run_id,
    )
    body = "\n".join(
        [
            "review_type: read-only adversarial code review leaf",
            f"implementation_task: {implementation}",
            "target_repository: fixture/repository",
            f"target_worktree: {repo}",
            "branch: review-fixture",
            f"base_commit: {base}",
            f"candidate_commit: {candidate}",
            f"scope_manifest_sha256: {scope_sha}",
            "implementer_profile: implementer",
            "reviewer_profile: reviewer",
            "read_only_source: true",
            "review_kind: pre_commit",
            "review_scope: change_set",
        ]
    )
    review_task = module.create_task(
        conn,
        title="standalone review",
        body=body,
        assignee="reviewer",
        created_by="coordinator",
        workspace_kind="dir",
        workspace_path=str(repo),
        parents=(implementation,),
        initial_status="blocked",
    )
    _native_status(module, conn, review_task, "ready")
    result = module.dispatch_once(
        conn,
        dry_run=False,
        max_spawn=1,
        board="fixture",
        spawn_fn=lambda _task, _workspace: 9911,
    )
    assert review_task in [task_id for task_id, _profile, _workspace in result.spawned]
    row = conn.execute(
        "SELECT status, workspace_path, worker_pid, current_run_id "
        "FROM tasks WHERE id = ?",
        (review_task,),
    ).fetchone()
    assert row["status"] == "running"
    assert row["workspace_path"] == str(repo)
    assert row["worker_pid"] == 9911
    assert row["current_run_id"] is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
        "AND kind = 'standalone_review_packet_claimed'",
        (review_task,),
    ).fetchone()[0] == 1


def test_raw_connection_cannot_forge_review_evidence_or_handoff(kanban_db):
    module, conn, db_path = kanban_db
    task_id, claimed = _review_like_claim(module, conn)
    conn.commit()
    raw = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.Error, match="native review evidence|review_native_connection_authorized"):
            raw.execute(
                "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, claimed.current_run_id, "standalone_review_packet_claimed", "{}", 2),
            )
        raw.rollback()
        with pytest.raises(sqlite3.Error, match="native review handoff|review_native_connection_authorized"):
            raw.execute(
                """INSERT INTO review_remediation_handoffs(
                    handoff_key, leaf_task_id, review_run_id, reviewer_profile,
                    implementer_profile, coordinator_profile, implementation_task,
                    candidate_commit, workspace_identity_json, packet_sha256,
                    finding_sha256, frontier_sha256, frontier_json, reason,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "forged",
                    task_id,
                    claimed.current_run_id,
                    "reviewer",
                    "implementer",
                    "coordinator",
                    "implementation",
                    "a" * 40,
                    "{}",
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                    "{}",
                    "forged",
                    2,
                ),
            )
    finally:
        raw.close()
    assert conn.execute("SELECT COUNT(*) FROM review_remediation_handoffs").fetchone()[0] == 0


def test_ordinary_connection_cannot_change_protected_trigger_ddl(kanban_db):
    _module, conn, db_path = kanban_db
    conn.commit()
    raw = _module.connect(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            raw.execute(
                "CREATE TRIGGER forged AFTER INSERT ON tasks "
                "BEGIN SELECT 1; END"
            )
        raw.rollback()
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            raw.execute("DROP TRIGGER trg_review_event_insert_authorized")
    finally:
        raw.close()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    trigger_names = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    assert "trg_review_event_insert_authorized" in trigger_names
    assert "trg_review_handoff_insert_authorized" in trigger_names


def test_reclaim_cas_captures_run_lease_and_worker_identity(kanban_db):
    module, conn, db_path = kanban_db
    task_id = module.create_task(
        conn,
        title="reclaim ABA fixture",
        body="ordinary task",
        assignee="worker",
        created_by="coordinator",
        initial_status="blocked",
    )
    _native_status(module, conn, task_id, "ready")
    first = module.claim_task(conn, task_id, claimer=None)
    assert first is not None
    first_run = first.current_run_id
    first_lock = first.claim_lock
    first_expiry = first.claim_expires
    assert isinstance(first_run, int) and first_run > 0
    first_run_id = int(first_run)
    assert isinstance(first_lock, str) and first_lock
    assert isinstance(first_expiry, int) and first_expiry > 0
    with module._review_native_mutation_authorized(), module.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid = 4242 WHERE id = ?", (task_id,))
        conn.execute("UPDATE task_runs SET worker_pid = 4242 WHERE id = ?", (first_run_id,))
        module._append_event(
            conn,
            task_id,
            "spawned",
            {"pid": 4242, "process_identity": "fixture-process"},
            run_id=first_run_id,
        )

    def replace_after_snapshot(_pid, _signal):
        replacement = module._sqlite_connect(db_path)
        try:
            with module._review_native_mutation_authorized(), module.write_txn(replacement):
                cur = replacement.execute(
                    """INSERT INTO task_runs(
                        task_id, profile, status, claim_lock, claim_expires,
                        worker_pid, started_at
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?)""",
                    (task_id, "worker", first_lock, first_expiry, 4242, 3),
                )
                replacement_run = int(cur.lastrowid)
                replacement.execute(
                    """UPDATE tasks SET status = 'running', current_run_id = ?,
                        claim_lock = ?, claim_expires = ?, worker_pid = ?
                        WHERE id = ?""",
                    (replacement_run, first_lock, first_expiry, 4242, task_id),
                )
        finally:
            replacement.close()
        raise ProcessLookupError()

    assert module.reclaim_task(
        conn,
        task_id,
        signal_fn=replace_after_snapshot,
        process_identity_fn=lambda _pid: "fixture-process",
    ) is False
    live_task = conn.execute(
        "SELECT status, current_run_id, claim_lock, claim_expires, worker_pid "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert live_task["status"] == "running"
    assert live_task["current_run_id"] != first_run
    live_run = conn.execute(
        "SELECT status, outcome FROM task_runs WHERE id = ?",
        (live_task["current_run_id"],),
    ).fetchone()
    assert live_run["status"] == "running"
    assert live_run["outcome"] is None


def test_git_provenance_binds_origin_identity_ancestry_and_disables_callbacks(
    kanban_db, tmp_path
):
    module, _conn, _db_path = kanban_db
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["/usr/bin/git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Factory Test")
    (repo / "a.txt").write_text("base\n")
    git("add", "a.txt")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("candidate\n")
    git("commit", "-qam", "candidate")
    candidate = git("rev-parse", "HEAD").stdout.strip()
    git("remote", "add", "origin", "https://github.com/fixture/repository.git")
    marker = tmp_path / "fsmonitor-ran"
    git("config", "core.fsmonitor", f"!touch {marker}")
    packet = {
        "target_worktree": str(repo),
        "target_repository": "fixture/repository",
        "branch": "main",
        "base_commit": base,
        "candidate_commit": candidate,
        "scope_manifest_sha256": hashlib.sha256(b"a.txt\0").hexdigest(),
    }
    identity, error = module._review_workspace_identity(packet)
    assert error is None
    assert identity["origin_identity"] == "github.com/fixture/repository"
    assert not marker.exists()

    git("remote", "set-url", "origin", "https://github.com/prefix/fixture/repository.git")
    _identity, path_error = module._review_workspace_identity(packet)
    assert path_error and "origin identity" in path_error
    git("remote", "set-url", "origin", "https://evil.example/fixture/repository.git")
    _identity, host_error = module._review_workspace_identity(packet)
    assert host_error and "origin identity" in host_error

    git("remote", "set-url", "origin", "https://github.com/fixture/repository.git")
    git("checkout", "-q", "--orphan", "unrelated")
    (repo / "a.txt").unlink()
    (repo / "other.txt").write_text("unrelated\n")
    git("add", "-A")
    git("commit", "-qm", "unrelated")
    unrelated = git("rev-parse", "HEAD").stdout.strip()
    git("checkout", "-q", "main")
    reverse_packet = dict(packet, base_commit=unrelated, candidate_commit=candidate)
    _identity, ancestry_error = module._review_workspace_identity(reverse_packet)
    assert ancestry_error and "ancestor" in ancestry_error


def test_verdict_requires_one_literal_canonical_field(kanban_db):
    module, conn, _db_path = kanban_db
    task_id = module.create_task(
        conn,
        title="same-card verdict fixture",
        body="review_required_before_completion: true",
        assignee="reviewer",
        created_by="implementer",
        initial_status="blocked",
    )
    _native_status(module, conn, task_id, "review")
    claimed = module.claim_review_task(conn, task_id, claimer="reviewer")
    assert claimed is not None
    run_id = claimed.current_run_id
    assert isinstance(run_id, int)
    invalid_metadata = (
        {"review_outcome": " approved ", "candidate_commit": "a" * 40},
        {"overall_verdict": "APPROVED", "candidate_commit": "a" * 40},
        {
            "review_outcome": "APPROVED",
            "verdict": "APPROVED",
            "candidate_commit": "a" * 40,
        },
        {
            "review_outcome": "APPROVED",
            "terminal_verdict": "CHANGES_REQUESTED",
            "candidate_commit": "a" * 40,
        },
        {
            "review_outcome": "APPROVED",
            "review_verdict": "CHANGES_REQUESTED",
            "candidate_commit": "a" * 40,
        },
        {
            "review_outcome": "APPROVED",
            "extra_verdict": "CHANGES_REQUESTED",
            "candidate_commit": "a" * 40,
        },
        {"review_outcome": "APPROVED-", "candidate_commit": "a" * 40},
    )
    for metadata in invalid_metadata:
        error = module._review_completion_rejection(
            conn,
            task_id,
            metadata=metadata,
            expected_run_id=run_id,
        )
        assert error == "only exact APPROVED evidence may complete a review-gated task"
