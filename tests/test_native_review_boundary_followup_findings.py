"""Causal regressions for the rejected native-boundary 1.0.21 candidate.

Every test imports the generated runtime selected by ``FACTORY_NATIVE_RUNTIME``
and uses only disposable databases and repositories.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

_RUNTIME_SETTING = os.environ.get("FACTORY_NATIVE_RUNTIME")
if not _RUNTIME_SETTING:
    pytest.skip(
        "set FACTORY_NATIVE_RUNTIME to the generated candidate",
        allow_module_level=True,
    )

_RUNTIME = Path(_RUNTIME_SETTING)
if not _RUNTIME.is_dir():
    pytest.skip(
        "set FACTORY_NATIVE_RUNTIME to the generated candidate",
        allow_module_level=True,
    )

sys.path.insert(0, str(_RUNTIME))


@pytest.fixture
def kanban_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    sys.modules.pop("hermes_cli.kanban_db", None)
    module = importlib.import_module("hermes_cli.kanban_db")
    db_path = tmp_path / "kanban.db"
    conn = module.connect(db_path)
    try:
        yield module, conn, db_path
    finally:
        conn.close()


def _claim(module, conn, title: str = "fixture"):
    task_id = module.create_task(conn, title=title, assignee="implementer")
    claimed = module.claim_task(conn, task_id, claimer=module._claimer_id())
    assert claimed is not None
    assert claimed.current_run_id is not None
    return task_id, claimed


def _set_running_claim(
    module,
    conn,
    task_id: str,
    *,
    run_id: int,
    lock: str,
    expires: int,
    worker_pid: int,
    started_at: int | None = None,
    max_runtime_seconds: int | None = None,
) -> None:
    with module._review_native_mutation_authorized(), module.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'running', current_run_id = ?, "
            "claim_lock = ?, claim_expires = ?, worker_pid = ?, "
            "started_at = COALESCE(?, started_at), "
            "max_runtime_seconds = COALESCE(?, max_runtime_seconds) WHERE id = ?",
            (
                run_id,
                lock,
                expires,
                worker_pid,
                started_at,
                max_runtime_seconds,
                task_id,
            ),
        )
        conn.execute(
            "UPDATE task_runs SET status = 'running', outcome = NULL, ended_at = NULL, "
            "claim_lock = ?, claim_expires = ?, worker_pid = ?, "
            "started_at = COALESCE(?, started_at) WHERE id = ? AND task_id = ?",
            (lock, expires, worker_pid, started_at, run_id, task_id),
        )
        if worker_pid is not None:
            module._append_event(
                conn,
                task_id,
                "spawned",
                {"pid": worker_pid, "process_identity": "fixture-process"},
                run_id=run_id,
            )


def _install_replacement(
    module,
    conn,
    task_id: str,
    *,
    old_run_id: int,
    lock: str,
    expires: int,
    worker_pid: int,
) -> int:
    with module._review_native_mutation_authorized(), module.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed', "
            "ended_at = ? WHERE id = ? AND task_id = ?",
            (int(time.time()), old_run_id, task_id),
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL WHERE id = ?",
            (task_id,),
        )
    replacement = module.claim_task(conn, task_id, claimer="replacement")
    assert replacement is not None and replacement.current_run_id is not None
    replacement_id = int(replacement.current_run_id)
    _set_running_claim(
        module,
        conn,
        task_id,
        run_id=replacement_id,
        lock=lock,
        expires=expires,
        worker_pid=worker_pid,
        started_at=int(time.time()),
    )
    return replacement_id


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _scope_sha(repo: Path, base: str, candidate: str) -> str:
    output = subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repo),
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            base,
            candidate,
        ],
        check=True,
        capture_output=True,
    ).stdout
    paths = sorted(item for item in output.split(b"\0") if item)
    scope = b"\0".join(paths) + (b"\0" if paths else b"")
    return hashlib.sha256(scope).hexdigest()


def _review_repo(
    tmp_path: Path, *, ignored: bool = False
) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "review-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "review-fixture")
    _git(repo, "config", "user.name", "Factory Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "remote", "add", "origin", "https://github.com/fixture/repository.git")
    if ignored:
        (repo / ".gitignore").write_text("ignored.bin\n", encoding="utf-8")
    (repo / "fixture.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "fixture.txt").write_text("base\ncandidate\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    if ignored:
        (repo / "ignored.bin").write_bytes(b"ignored but present\n")
    packet = {
        "target_worktree": str(repo),
        "target_repository": "fixture/repository",
        "branch": "review-fixture",
        "base_commit": base,
        "candidate_commit": candidate,
        "scope_manifest_sha256": _scope_sha(repo, base, candidate),
    }
    return repo, packet


def test_raw_sqlite_connection_cannot_forge_native_review_evidence(kanban_db):
    module, conn, db_path = kanban_db
    task_id, claimed = _claim(module, conn, "raw review forgery")
    run = conn.execute(
        "SELECT claim_lock, claim_expires FROM task_runs WHERE id = ? AND task_id = ?",
        (claimed.current_run_id, task_id),
    ).fetchone()
    assert run is not None

    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        raw.execute(
            "INSERT OR IGNORE INTO review_native_mutation_authorization(token) VALUES (1)"
        )
        raw.execute(
            "INSERT INTO review_native_run_capabilities "
            "(task_id, run_id, claim_lock, claim_expires, nonce, source_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'review', ?)",
            (
                task_id,
                int(claimed.current_run_id),
                run[0],
                int(run[1]),
                "forged-nonce",
                int(time.time()),
            ),
        )
        with pytest.raises(sqlite3.Error):
            raw.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, 'same_card_review_approved', '{}', ?)",
                (task_id, int(claimed.current_run_id), int(time.time())),
            )
    finally:
        raw.close()


def test_same_name_noop_trigger_is_repaired_before_trusted_use(kanban_db):
    module, _conn, db_path = kanban_db
    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        raw.execute("DROP TRIGGER trg_review_event_insert_authorized")
        raw.execute(
            "CREATE TRIGGER trg_review_event_insert_authorized "
            "BEFORE INSERT ON task_events WHEN 0 BEGIN SELECT 1; END"
        )
    finally:
        raw.close()

    module._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    repaired = module.connect(db_path)
    repaired.close()

    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        sql = raw.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_review_event_insert_authorized'"
        ).fetchone()[0]
        assert "review_native_connection_authorized" in sql
        with pytest.raises(sqlite3.Error):
            raw.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES ('missing', 'same_card_review_approved', '{}', ?)",
                (int(time.time()),),
            )
    finally:
        raw.close()


def test_missing_installed_protected_table_is_not_lossily_recreated(kanban_db):
    module, _conn, db_path = kanban_db
    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        raw.execute("DROP TABLE review_native_run_capabilities")
    finally:
        raw.close()
    module._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with pytest.raises(sqlite3.DatabaseError, match="lossy|protected review schema"):
        module.connect(db_path)


def test_raw_lifecycle_authority_event_is_rejected(kanban_db):
    module, conn, db_path = kanban_db
    task_id = module.create_task(conn, title="event authority", assignee="implementer")
    raw = sqlite3.connect(db_path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.Error):
            raw.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'unblocked', '{}', ?)",
                (task_id, int(time.time())),
            )
    finally:
        raw.close()


def test_request_review_force_requires_operator_capability_and_live_lease(kanban_db):
    module, conn, _ = kanban_db
    task_id, _claimed = _claim(module, conn, "force review")
    ok, reason = module.request_review(
        conn,
        task_id,
        summary="verified",
        reviewer="reviewer",
        force=True,
        with_reason=True,
    )
    assert not ok
    assert "capability" in str(reason).casefold()

    with module._operator_review_override_authorized() as capability:
        ok, reason = module.request_review(
            conn,
            task_id,
            summary="verified",
            reviewer="reviewer",
            force=True,
            operator_capability=capability,
            with_reason=True,
        )
    assert ok, reason

    other_id, other = _claim(module, conn, "expired force review")
    expired = int(time.time()) - 1
    _set_running_claim(
        module,
        conn,
        other_id,
        run_id=int(other.current_run_id),
        lock=str(other.claim_lock),
        expires=expired,
        worker_pid=123,
    )
    with module._operator_review_override_authorized() as capability:
        ok, reason = module.request_review(
            conn,
            other_id,
            summary="must fail closed",
            reviewer="reviewer",
            force=True,
            operator_capability=capability,
            with_reason=True,
        )
    assert not ok
    assert "expired" in str(reason).casefold() or "live" in str(reason).casefold()


def test_ready_task_cannot_close_foreign_run(kanban_db):
    module, conn, _ = kanban_db
    task_a = module.create_task(conn, title="malformed ready", assignee="implementer")
    task_b, claimed_b = _claim(module, conn, "foreign running")
    with module._review_native_mutation_authorized(), module.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = ?, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL WHERE id = ?",
            (int(claimed_b.current_run_id), task_a),
        )
    ok, reason = module.request_review(
        conn,
        task_a,
        summary="must not cross task boundary",
        reviewer="reviewer",
        with_reason=True,
    )
    assert not ok
    assert "inconsistent" in str(reason).casefold() or "ready" in str(reason).casefold()
    foreign = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE id = ? AND task_id = ?",
        (int(claimed_b.current_run_id), task_b),
    ).fetchone()
    assert tuple(foreign) == ("running", None, None)


def test_process_identity_mismatch_prevents_signal(kanban_db):
    module, _conn, _ = kanban_db
    signals: list[tuple[int, int]] = []
    result = module._terminate_reclaimed_worker(
        111,
        module._claimer_id(),
        expected_process_identity="old-process",
        process_identity_fn=lambda _pid: "replacement-process",
        signal_fn=lambda pid, sig: signals.append((pid, sig)),
    )
    assert signals == []
    assert result["process_identity_mismatch"] is True
    assert result["termination_attempted"] is False


def test_max_runtime_settlement_is_bound_to_captured_run(kanban_db, monkeypatch):
    module, conn, _ = kanban_db
    task_id, claimed = _claim(module, conn, "max runtime ABA")
    old_run = int(claimed.current_run_id)
    lock = str(claimed.claim_lock)
    expires = int(claimed.claim_expires)
    _set_running_claim(
        module,
        conn,
        task_id,
        run_id=old_run,
        lock=lock,
        expires=expires,
        worker_pid=111,
        started_at=int(time.time()) - 100,
        max_runtime_seconds=1,
    )
    replacement: list[int] = []

    def race_signal(_pid: int, _sig: int) -> None:
        if not replacement:
            replacement.append(
                _install_replacement(
                    module,
                    conn,
                    task_id,
                    old_run_id=old_run,
                    lock=lock,
                    expires=expires,
                    worker_pid=111,
                )
            )

    monkeypatch.setattr(module, "_pid_alive", lambda _pid: False)
    timed_out = module.enforce_max_runtime(
        conn,
        signal_fn=race_signal,
        process_identity_fn=lambda _pid: "fixture-process",
    )
    assert timed_out == []
    assert replacement
    row = conn.execute(
        "SELECT status, current_run_id, claim_lock, claim_expires, worker_pid "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert tuple(row) == ("running", replacement[0], lock, expires, 111)
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
        (replacement[0],),
    ).fetchone()
    assert tuple(run) == ("running", None, None)


def test_spawn_failure_is_fenced_to_originating_run(kanban_db):
    module, conn, _ = kanban_db
    task_id, claimed = _claim(module, conn, "spawn failure ABA")
    old_run = int(claimed.current_run_id)
    lock = str(claimed.claim_lock)
    expires = int(claimed.claim_expires)
    replacement = _install_replacement(
        module,
        conn,
        task_id,
        old_run_id=old_run,
        lock=lock,
        expires=expires,
        worker_pid=222,
    )
    assert not module._record_spawn_failure(
        conn,
        task_id,
        "old attempt failed",
        failure_limit=1,
        expected_run_id=old_run,
        expected_claim_lock=lock,
        expected_claim_expires=expires,
    )
    row = conn.execute(
        "SELECT status, current_run_id, consecutive_failures FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert tuple(row) == ("running", replacement, 0)
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
        (replacement,),
    ).fetchone()
    assert tuple(run) == ("running", None, None)


def test_remediation_metadata_survives_tool_boundary_exactly(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-remediation")
    monkeypatch.setenv("HERMES_SESSION_ID", "worker-session")
    sys.modules.pop("tools.kanban_tools", None)
    tools = importlib.import_module("tools.kanban_tools")
    metadata = {
        "candidate_commit": "a" * 40,
        "review_remediation_handoff_key": "review-remediation:v2:opaque-value",
        "scope_manifest_sha256": "b" * 64,
    }
    assert (
        tools._prepare_request_review_metadata("task-remediation", metadata) == metadata
    )


def test_ignored_untracked_content_is_not_a_clean_review_tree(kanban_db, tmp_path):
    module, _conn, _ = kanban_db
    _repo, packet = _review_repo(tmp_path, ignored=True)
    identity, error = module._review_workspace_identity(packet)
    assert identity is None
    assert "clean" in str(error).casefold() or "untracked" in str(error).casefold()


def test_invalid_byte_git_path_fails_closed_without_decode_exception(
    kanban_db, tmp_path
):
    module, _conn, _ = kanban_db
    repo, packet = _review_repo(tmp_path)
    bad_name = b"invalid-\xff.bin"
    bad_path = os.fsencode(repo) + b"/" + bad_name
    fd = os.open(bad_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, b"bad path\n")
    finally:
        os.close(fd)
    subprocess.run(
        [b"/usr/bin/git", b"-C", os.fsencode(repo), b"add", b"--", bad_name],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [b"/usr/bin/git", b"-C", os.fsencode(repo), b"commit", b"-qm", b"bad path"],
        check=True,
        capture_output=True,
    )
    packet["candidate_commit"] = _git(repo, "rev-parse", "HEAD")
    packet["scope_manifest_sha256"] = "0" * 64
    identity, error = module._review_workspace_identity(packet)
    assert identity is None
    assert error


def test_oversized_tracked_file_is_rejected_before_materialization(kanban_db, tmp_path):
    module, _conn, _ = kanban_db
    repo, packet = _review_repo(tmp_path)
    base = packet["candidate_commit"]
    limit = int(module._REVIEW_MAX_TRACKED_FILE_BYTES)
    with (repo / "oversized.bin").open("wb") as handle:
        handle.truncate(limit + 1)
    _git(repo, "add", "oversized.bin")
    _git(repo, "commit", "-qm", "oversized")
    candidate = _git(repo, "rev-parse", "HEAD")
    packet["base_commit"] = base
    packet["candidate_commit"] = candidate
    packet["scope_manifest_sha256"] = _scope_sha(repo, base, candidate)
    identity, error = module._review_workspace_identity(packet)
    assert identity is None
    assert isinstance(error, str) and error
