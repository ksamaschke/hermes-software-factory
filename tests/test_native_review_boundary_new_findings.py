"""Isolated causal regressions for rejected native-boundary 1.0.20 findings.

Every test loads the generated runtime named by ``FACTORY_NATIVE_RUNTIME`` and
uses only a temporary database or repository.  The tests are intentionally
separate from the compatibility contract suite so each rejected bypass has an
independent failure signal.
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


def _set_claim(
    module,
    conn,
    task_id: str,
    *,
    status: str,
    run_id: int | None,
    lock: str | None,
    expires: int | None,
    worker_pid: int | None,
    started_at: int | None = None,
) -> None:
    with module._review_native_mutation_authorized(), module.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = ?, claim_lock = ?, "
            "claim_expires = ?, worker_pid = ?, started_at = COALESCE(?, started_at) "
            "WHERE id = ?",
            (status, run_id, lock, expires, worker_pid, started_at, task_id),
        )
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET status = 'running', outcome = NULL, ended_at = NULL, "
                "claim_lock = ?, claim_expires = ?, worker_pid = ?, started_at = COALESCE(?, started_at) "
                "WHERE id = ? AND task_id = ?",
                (lock, expires, worker_pid, started_at, run_id, task_id),
            )


def _replace_with_same_lease(module, conn, task_id: str, captured: dict[str, object]) -> int:
    """Install a replacement run that deliberately reuses the old lease values."""
    old_run_id = captured["run_id"]
    with module._review_native_mutation_authorized(), module.write_txn(conn):
        if old_run_id is not None:
            conn.execute(
                "UPDATE task_runs SET status = 'released', outcome = 'reclaimed', "
                "ended_at = ? WHERE id = ? AND task_id = ?",
                (int(time.time()), old_run_id, task_id),
            )
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = NULL, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
            (task_id,),
        )
    replacement = module.claim_task(conn, task_id, claimer="aba-worker", ttl_seconds=60)
    assert replacement is not None
    assert replacement.current_run_id is not None
    _set_claim(
        module,
        conn,
        task_id,
        status="running",
        run_id=replacement.current_run_id,
        lock=str(captured["lock"]),
        expires=int(captured["expires"]),
        worker_pid=222,
        started_at=int(time.time()),
    )
    return int(replacement.current_run_id)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _review_packet(repo: Path, base: str, candidate: str, target: str = "fixture/repository") -> dict[str, str]:
    scope = _git(repo, "diff", "--name-only", "--no-renames", base, candidate) + "\n"
    scope_bytes = b"\x00".join(
        item.encode("utf-8") for item in sorted(scope.splitlines()) if item
    ) + b"\x00"
    return {
        "target_worktree": str(repo),
        "target_repository": target,
        "branch": _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD"),
        "base_commit": base,
        "candidate_commit": candidate,
        "scope_manifest_sha256": hashlib.sha256(scope_bytes).hexdigest(),
    }


def _make_git_review_repo(tmp_path: Path, *, origins: tuple[str, ...] = ()) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "review-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "review-fixture")
    _git(repo, "config", "user.name", "Factory Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "remote", "add", "origin", origins[0] if origins else "https://github.com/fixture/repository.git")
    for origin in origins[1:]:
        _git(repo, "config", "--add", "remote.origin.url", origin)
    (repo / ".gitattributes").write_text("*.txt filter=evil\n")
    (repo / "fixture.txt").write_text("base\n")
    _git(repo, "add", ".gitattributes", "fixture.txt")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "fixture.txt").write_text("base\ncandidate\n")
    _git(repo, "commit", "-qam", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    return repo, _review_packet(repo, base, candidate)


def test_dispatch_post_claim_rejects_expired_claim_at_write(kanban_db):
    module, conn, _ = kanban_db
    task_id = module.create_task(conn, title="expired dispatch")
    claimed = module.claim_task(conn, task_id, claimer="dispatcher", ttl_seconds=1)
    assert claimed is not None
    stale_expires = int(time.time()) - 1
    _set_claim(
        module,
        conn,
        task_id,
        status="running",
        run_id=claimed.current_run_id,
        lock=claimed.claim_lock,
        expires=stale_expires,
        worker_pid=None,
    )

    with pytest.raises(RuntimeError, match="expired|live claim"):
        module.set_workspace_path(
            conn,
            task_id,
            "/tmp/expired-workspace",
            expected_run_id=claimed.current_run_id,
            expected_claim_lock=claimed.claim_lock,
            expected_claim_expires=stale_expires,
        )
    with pytest.raises(RuntimeError, match="expired|live claim"):
        module._set_worker_pid(
            conn,
            task_id,
            1234,
            expected_run_id=claimed.current_run_id,
            expected_claim_lock=claimed.claim_lock,
            expected_claim_expires=stale_expires,
        )


def test_release_stale_claims_cas_captures_run_pid_lock_and_expiry(kanban_db, monkeypatch):
    module, conn, _ = kanban_db
    task_id = module.create_task(conn, title="release ABA")
    claimed = module.claim_task(conn, task_id, claimer="aba-worker", ttl_seconds=1)
    assert claimed is not None and claimed.current_run_id is not None
    captured = {
        "run_id": int(claimed.current_run_id),
        "lock": str(claimed.claim_lock),
        "expires": int(time.time()) - 10,
    }
    _set_claim(
        module,
        conn,
        task_id,
        status="running",
        run_id=int(captured["run_id"]),
        lock=str(captured["lock"]),
        expires=int(captured["expires"]),
        worker_pid=111,
    )

    replacement_run: list[int] = []

    def replace_before_release(pid, lock, *, signal_fn=None, **_kwargs):
        replacement_run.append(_replace_with_same_lease(module, conn, task_id, captured))
        return {"termination_attempted": False, "terminated": False, "host_local": False}

    monkeypatch.setattr(module, "_terminate_reclaimed_worker", replace_before_release)
    assert module.release_stale_claims(conn) == 0
    row = conn.execute(
        "SELECT status, current_run_id, worker_pid, claim_lock, claim_expires "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert replacement_run and row["current_run_id"] == replacement_run[0]
    assert row["status"] == "running"
    assert row["worker_pid"] == 222
    assert row["claim_lock"] == captured["lock"]
    assert row["claim_expires"] == captured["expires"]


def test_detect_stale_running_cas_captures_run_pid_lock_and_expiry(kanban_db, monkeypatch):
    module, conn, _ = kanban_db
    task_id = module.create_task(conn, title="detect ABA")
    claimed = module.claim_task(conn, task_id, claimer="aba-worker", ttl_seconds=60)
    assert claimed is not None and claimed.current_run_id is not None
    captured = {
        "run_id": int(claimed.current_run_id),
        "lock": str(claimed.claim_lock),
        "expires": int(claimed.claim_expires),
    }
    old = int(time.time()) - 7200
    _set_claim(
        module,
        conn,
        task_id,
        status="running",
        run_id=int(captured["run_id"]),
        lock=str(captured["lock"]),
        expires=int(captured["expires"]),
        worker_pid=111,
        started_at=old,
    )

    replacement_run: list[int] = []

    def replace_before_detect(pid, lock, *, signal_fn=None, **_kwargs):
        replacement_run.append(_replace_with_same_lease(module, conn, task_id, captured))
        return {"termination_attempted": False, "terminated": False, "host_local": False}

    monkeypatch.setattr(module, "_terminate_reclaimed_worker", replace_before_detect)
    assert module.detect_stale_running(conn, stale_timeout_seconds=1) == []
    row = conn.execute(
        "SELECT status, current_run_id, worker_pid, claim_lock, claim_expires "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert replacement_run and row["current_run_id"] == replacement_run[0]
    assert row["status"] == "running"
    assert row["worker_pid"] == 222
    assert row["claim_lock"] == captured["lock"]
    assert row["claim_expires"] == captured["expires"]


def test_request_review_restores_ready_and_force_paths_without_generic_bypass(kanban_db):
    module, conn, _ = kanban_db
    ready_id = module.create_task(conn, title="ready review", assignee="worker")
    assert module.request_review(conn, ready_id, summary="manual handoff") is True
    assert module.get_task(conn, ready_id).status == "review"

    forced_id = module.create_task(conn, title="forced review", assignee="worker")
    forced_claim = module.claim_task(conn, forced_id, claimer="worker")
    assert forced_claim is not None
    with module._operator_review_override_authorized() as capability:
        assert module.request_review(
            conn,
            forced_id,
            summary="operator override",
            force=True,
            operator_capability=capability,
        ) is True
    assert module.get_task(conn, forced_id).status == "review"

    live_id = module.create_task(conn, title="live claim", assignee="worker")
    live_claim = module.claim_task(conn, live_id, claimer="worker")
    assert live_claim is not None
    assert module.request_review(conn, live_id) is False
    live_row = conn.execute(
        "SELECT status, claim_lock, current_run_id FROM tasks WHERE id = ?", (live_id,)
    ).fetchone()
    assert live_row["status"] == "running"
    assert live_row["claim_lock"] is not None
    assert live_row["current_run_id"] == live_claim.current_run_id

    done_id = module.create_task(conn, title="done review", assignee="worker")
    assert module.complete_task(conn, done_id) is True
    with module._operator_review_override_authorized() as capability:
        assert module.request_review(
            conn,
            done_id,
            force=True,
            operator_capability=capability,
        ) is False


def test_protected_event_update_blocks_ordinary_to_protected(kanban_db):
    module, conn, _ = kanban_db
    task_id = module.create_task(conn, title="ordinary event")
    with module.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'ordinary', '{}', 1)",
            (task_id,),
        )
        event_id = int(cur.lastrowid)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE task_events SET kind = ?, run_id = ?, payload = ? WHERE id = ?",
            ("same_card_review_approved", 1, "{}", event_id),
        )


def test_drift_rebuild_reinstalls_protected_triggers_before_connect_returns(kanban_db):
    module, conn, db_path = kanban_db
    conn.close()
    raw = sqlite3.connect(db_path)
    raw.execute("DROP TABLE task_events")
    raw.execute(
        "CREATE TABLE task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL)"
    )
    raw.commit()
    raw.close()

    repaired = module.connect(db_path)
    try:
        names = {
            row["name"]
            for row in repaired.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert {
            "trg_review_event_insert_authorized",
            "trg_review_event_update_immutable",
            "trg_review_event_delete_immutable",
        } <= names
    finally:
        repaired.close()


@pytest.mark.parametrize(
    "table",
    [
        "tasks",
        "task_events",
        "task_runs",
        "task_links",
        "review_remediation_handoffs",
        "review_native_mutation_authorization",
        "review_native_run_capabilities",
        "review_native_dispatch_authorization",
        "review_native_schema_migration_authorization",
    ],
)
def test_authorizer_denies_ordinary_drop_of_every_protected_base_table(kanban_db, table):
    _module, conn, _ = kanban_db
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(f"DROP TABLE {table}")


def test_steady_state_connect_refuses_lossy_protected_schema_repair(kanban_db):
    module, conn, db_path = kanban_db
    conn.close()
    raw = sqlite3.connect(db_path)
    raw.execute("DROP TABLE task_runs")
    raw.commit()
    raw.close()

    with pytest.raises(sqlite3.DatabaseError, match="refusing lossy repair"):
        module.connect(db_path)
    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'task_runs'"
        ).fetchone()[0] == 0
    finally:
        raw.close()


def test_git_provenance_never_executes_repository_filter_callbacks(kanban_db, tmp_path):
    module, _conn, _ = kanban_db
    repo, packet = _make_git_review_repo(tmp_path)
    marker = tmp_path / "filter-ran.marker"
    _git(repo, "config", "filter.evil.clean", f"/bin/sh -c 'touch {marker}; cat'")
    _git(repo, "config", "filter.evil.smudge", f"/bin/sh -c 'touch {marker}; cat'")
    _git(repo, "config", "filter.evil.process", f"/bin/sh -c 'touch {marker}; cat'")
    identity, error = module._review_workspace_identity(packet)
    assert error is None, error
    assert identity is not None
    assert not marker.exists()


def test_git_provenance_rejects_non_identical_remote_origin_values(kanban_db, tmp_path):
    module, _conn, _ = kanban_db
    _repo, packet = _make_git_review_repo(
        tmp_path,
        origins=(
            "https://github.com/other/repository.git",
            "https://github.com/fixture/repository.git",
        ),
    )
    identity, error = module._review_workspace_identity(packet)
    assert identity is None
    assert error is not None and "origin" in error
