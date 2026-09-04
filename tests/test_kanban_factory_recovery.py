"""Unit tests for the deterministic factory recovery add-on."""

from __future__ import annotations

import os
import errno
import importlib.util
import inspect
import math
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import kanban_worker_reaper as reaper


SCRIPT = Path(__file__).parents[1] / "scripts" / "kanban_factory_recovery.py"
CRON_SHIM = Path(__file__).parents[1] / "scripts" / "kanban_factory_recovery_cron.py"


spec = importlib.util.spec_from_file_location("kanban_factory_recovery", SCRIPT)
assert spec is not None and spec.loader is not None
factory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(factory)


def test_collision_parser_matches_git_worktree_error():
    value = factory._collision(
        "fatal: 'kanban/project-231' is already used by worktree at "
        "'/repo/.worktrees/t_parent'"
    )
    assert value is not None
    branch, path = value
    assert branch == "kanban/project-231"
    assert path == Path("/repo/.worktrees/t_parent")


def test_collision_parser_matches_alternate_git_wording():
    value = factory._collision(
        "branch kanban/project-231 is already checked out at /repo/.worktrees/t_parent"
    )
    assert value is not None
    assert value[0] == "kanban/project-231"
    assert value[1] == Path("/repo/.worktrees/t_parent")


def test_collision_parser_rejects_unrelated_errors():
    assert factory._collision("cargo test failed") is None


def test_parked_detection_uses_imported_label_metadata():
    assert factory._is_parked({"labels": ["slice-11", "parked"]})
    assert not factory._is_parked({"labels": ["slice-11"]})


def test_parked_detection_supports_imported_body_metadata():
    assert factory._is_parked({"body": "- Labels: enhancement, parked, slice-3"})
    assert not factory._is_parked({"body": "- Labels: enhancement, slice-3"})


def test_parked_acknowledgement_is_idempotent():
    assert not factory._parked_acknowledged({"comments": []})
    assert factory._parked_acknowledged(
        {"comments": [{"body": "[factory] parked backlog acknowledged: preserve state"}]}
    )


def test_cron_shim_requires_explicit_board_and_script():
    text = CRON_SHIM.read_text(encoding="utf-8")
    assert 'os.environ.get("HERMES_FACTORY_RECOVERY_SCRIPT")' in text
    assert 'os.environ.get("HERMES_FACTORY_BOARD")' in text
    assert "HERMES_FACTORY_RECOVERY_SCRIPT is required" in text
    assert "HERMES_FACTORY_BOARD is required" in text


def test_repo_root_resolves_git_common_dir_for_linked_worktree(monkeypatch):
    monkeypatch.setattr(
        factory,
        "_run",
        lambda argv, cwd=None: (0, "/repo/.git", "")
        if argv[-1] == "--git-common-dir"
        else (1, "", ""),
    )
    assert factory._repo_root(Path("/repo/.worktrees/t_done")) == Path("/repo")


def test_real_linked_worktree_reports_common_root_and_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "factory@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Factory Test"],
        check=True,
    )
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "test fixture"], check=True)
    linked = repo / ".worktrees" / "t_done"
    linked.parent.mkdir()
    subprocess.run(
        [
            "git", "-C", str(repo), "worktree", "add", "-q", "-b",
            "kanban/project-231", str(linked), "HEAD",
        ],
        check=True,
    )
    assert factory._repo_root(linked) == repo.resolve()
    assert factory._worktree_branch(linked) == "kanban/project-231"


def test_repair_preserves_collision_when_branch_does_not_match(monkeypatch, tmp_path):
    occupied = tmp_path / "t_owner"
    occupied.mkdir()
    task = {"id": "t_blocked", "status": "blocked"}
    detail = {
        "task": task,
        "runs": [{
            "status": "spawn_failed",
            "error": "fatal: 'kanban/project-231' is already used by worktree at "
            f"'{occupied}'",
        }],
    }
    monkeypatch.setattr(factory, "_task_detail", lambda board, task_id: detail)
    monkeypatch.setattr(factory, "_repo_root", lambda path: Path("/repo"))
    monkeypatch.setattr(factory, "_worktree_branch", lambda path: "kanban/other-231")
    result = factory._repair_collision("generic-board", task, dry_run=False)
    assert result is not None
    assert "does not match" in result


def _terminal_detail(task_id: str, *, current_run_id=None):
    return {
        "task": {
            "id": task_id,
            "status": "blocked",
            "current_run_id": current_run_id,
        }
    }


def _create_reaper_test_database(path: Path, task_id: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE tasks (id TEXT, status TEXT, current_run_id INTEGER)"
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?)",
            (task_id, "blocked", None),
        )
        connection.commit()
    finally:
        connection.close()


def _start_task_worker(task_id: str, board: str, kanban_db: Path):
    # The child deliberately stays alive so the test proves process-group
    # cleanup, rather than merely observing the Hermes parent exit.
    script = (
        "import subprocess, time; "
        "subprocess.Popen(['sleep', '60']); "
        "time.sleep(60)"
    )
    env = os.environ.copy()
    env.update(
        {
            "HERMES_KANBAN_TASK": task_id,
            "HERMES_KANBAN_RUN_ID": "41",
            "HERMES_KANBAN_BOARD": board,
            "HERMES_KANBAN_DB": str(kanban_db),
        }
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_for_task_process(task_id: str, board: str, kanban_db: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        records = reaper.iter_process_records()
        if any(
            reaper.process_identity_matches(
                record,
                task_id=task_id,
                board=board,
                kanban_db=kanban_db,
            )
            for record in records
        ):
            return
        time.sleep(0.05)
    raise AssertionError(f"task worker {task_id} did not become visible in procfs")


def _kill_task_worker(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, 9)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux procfs")
def test_reaper_terminal_cleanup_reaps_worker_and_live_descendant(tmp_path):
    task_id = "t_reaper_terminal"
    board = "factory-reaper-test"
    kanban_db = tmp_path / "kanban.db"
    _create_reaper_test_database(kanban_db, task_id)
    process = _start_task_worker(task_id, board, kanban_db)
    try:
        _wait_for_task_process(task_id, board, kanban_db)
        detail = _terminal_detail(task_id)
        report = factory.reap_terminal_task_workers(
            detail,
            task_id=task_id,
            board=board,
            kanban_db=kanban_db,
            refresh=lambda: detail,
            grace_seconds=1,
        )
        assert report["status"] == "reaped"
        assert report["survivors"] == []
        process.wait(timeout=5)
    finally:
        _kill_task_worker(process)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux procfs")
def test_reaper_does_not_kill_worker_for_active_run_or_board_mismatch(tmp_path):
    kanban_db = tmp_path / "kanban.db"
    active_id = "t_reaper_active"
    active = _start_task_worker(active_id, "factory-reaper-test", kanban_db)
    mismatched_id = "t_reaper_mismatch"
    mismatched = _start_task_worker(mismatched_id, "other-board", kanban_db)
    try:
        _wait_for_task_process(active_id, "factory-reaper-test", kanban_db)
        _wait_for_task_process(mismatched_id, "other-board", kanban_db)

        active_detail = _terminal_detail(active_id, current_run_id=41)
        active_report = factory.reap_terminal_task_workers(
            active_detail,
            task_id=active_id,
            board="factory-reaper-test",
            kanban_db=kanban_db,
            refresh=lambda: active_detail,
        )
        assert active_report["status"] == "not_applicable"
        assert active.poll() is None

        mismatch_detail = _terminal_detail(mismatched_id)
        mismatch_report = factory.reap_terminal_task_workers(
            mismatch_detail,
            task_id=mismatched_id,
            board="factory-reaper-test",
            kanban_db=kanban_db,
            refresh=lambda: mismatch_detail,
        )
        assert mismatch_report["status"] == "none"
        assert mismatched.poll() is None
    finally:
        _kill_task_worker(active)
        _kill_task_worker(mismatched)


def test_recover_runs_terminal_worker_reconciliation_for_blocked_tasks(monkeypatch):
    task = {"id": "t_blocked", "status": "blocked"}
    monkeypatch.setattr(factory, "_json_command", lambda *args: [task])
    monkeypatch.setattr(
        factory,
        "_reconcile_terminal_worker",
        lambda board, task, dry_run: "t_blocked: terminal worker reconciliation=reaped",
    )
    monkeypatch.setattr(factory, "_acknowledge_parked", lambda *args, **kwargs: None)
    monkeypatch.setattr(factory, "_repair_collision", lambda *args, **kwargs: None)
    assert factory.recover("factory-reaper-test", dry_run=False) == [
        "t_blocked: terminal worker reconciliation=reaped"
    ]


@pytest.mark.parametrize("raw_task_id", [" t ", 123, None, ""])
def test_reconcile_rejects_noncanonical_id_before_canonical_lookup(
    monkeypatch, raw_task_id
):
    task = {"id": raw_task_id, "status": "blocked"}
    monkeypatch.setattr(
        factory,
        "_readonly_task_detail",
        lambda *args, **kwargs: pytest.fail("malformed id reached task lookup"),
    )
    monkeypatch.setattr(
        factory,
        "_task_detail",
        lambda *args, **kwargs: pytest.fail("malformed id reached CLI lookup"),
    )

    assert factory._reconcile_terminal_worker(
        "factory-reaper-test", task, dry_run=False
    ) is None


def test_recovery_budget_applies_before_terminal_worker_scans(monkeypatch):
    task = {"id": "t_blocked", "status": "blocked"}
    monkeypatch.setattr(factory, "_json_command", lambda *args: [task])
    monkeypatch.setattr(factory, "_repair_cron_pins", lambda dry_run: [])
    monkeypatch.setattr(factory, "_recovery_budget_seconds", lambda: 0)
    monkeypatch.setattr(
        factory,
        "_reconcile_terminal_worker",
        lambda *args, **kwargs: pytest.fail("worker scan ran after budget expiry"),
    )

    assert factory.recover("factory-reaper-test", dry_run=False) == [
        "recovery budget exhausted; skipped remaining blocked-task repairs"
    ]


def test_cli_timeout_is_converted_to_bounded_failure(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(kwargs.get("args", args[0]), kwargs["timeout"])

    monkeypatch.setattr(factory.subprocess, "run", timeout)
    monkeypatch.setenv("HERMES_FACTORY_CLI_TIMEOUT_SECONDS", "7")

    assert factory._run(["hermes", "kanban"])[0:2] == (124, "")
    assert "7s" in factory._run(["hermes", "kanban"])[2]


def test_readonly_task_detail_fallback_uses_only_terminal_identity(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id TEXT, status TEXT, current_run_id INTEGER, "
        "title TEXT, priority INTEGER, created_at INTEGER)"
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
        ("t_sqlite", "blocked", None, "blocked task", 1, 1),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setattr(
        factory,
        "_json_command",
        lambda *args: (_ for _ in ()).throw(RuntimeError("CLI unavailable")),
    )

    assert factory._task_detail("factory-reaper-test", "t_sqlite") == {
        "_readback": "sqlite",
        "task": {"id": "t_sqlite", "status": "blocked", "current_run_id": None},
    }


def test_default_db_path_resolves_the_named_board(tmp_path, monkeypatch):
    db = tmp_path / ".hermes" / "kanban" / "boards" / "factory-reaper-test" / "kanban.db"
    db.parent.mkdir(parents=True)
    db.touch()
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_REAL_HOME", str(tmp_path))

    assert factory._kanban_db_path("factory-reaper-test") == db
    assert factory._kanban_db_path("../other-board") is None


def _write_proc_record(
    proc_root: Path,
    *,
    pid: int,
    ppid: int,
    pgrp: int,
    session: int,
    start_time: int,
    env: dict[str, str] | None,
) -> None:
    process_dir = proc_root / str(pid)
    process_dir.mkdir()
    fields = ["S", str(ppid), str(pgrp), str(session), *("0" for _ in range(15)), str(start_time)]
    (process_dir / "stat").write_text(
        f"{pid} (worker) {' '.join(fields)}", encoding="utf-8"
    )
    if env is not None:
        payload = b"\0".join(f"{key}={value}".encode() for key, value in env.items())
        (process_dir / "environ").write_bytes(payload + b"\0")


def test_unreadable_process_group_member_fails_closed(tmp_path):
    identity = {
        reaper.TASK_ENV: "t_unreadable",
        reaper.BOARD_ENV: "factory-reaper-test",
    }
    _write_proc_record(
        tmp_path,
        pid=42000,
        ppid=1,
        pgrp=42000,
        session=42000,
        start_time=10,
        env=identity,
    )
    _write_proc_record(
        tmp_path,
        pid=42001,
        ppid=42000,
        pgrp=42000,
        session=42000,
        start_time=11,
        env=None,
    )

    records = reaper.iter_process_records(proc_root=tmp_path)
    groups, unsafe = reaper._validated_groups(
        records,
        task_id="t_unreadable",
        board="factory-reaper-test",
        kanban_db=None,
        current_pid=os.getpid(),
    )

    assert {record.pid for record in records} == {42000, 42001}
    assert groups == []
    assert unsafe == ["session 42000 contains an unbound process"]


def test_reaper_identity_changing_process_remains_a_reported_survivor(monkeypatch):
    group = reaper.ProcessGroup(
        session=43000,
        pgrp=43000,
        pids=(43000,),
        start_times={43000: 12},
    )
    survivor = reaper.ProcessRecord(
        pid=43000,
        ppid=1,
        pgrp=43000,
        session=43000,
        start_time=12,
        state="S",
        env={},
        env_readable=False,
    )
    monkeypatch.setattr(reaper, "read_process_record", lambda *args, **kwargs: survivor)

    assert reaper._live_pids(
        group,
        proc_root=Path("/proc"),
        task_id="t_unreadable",
        board="factory-reaper-test",
        kanban_db=None,
    ) == [43000]


def _synthetic_record(
    task_id: str,
    *,
    pid: int = 50000,
    pgrp: int = 50000,
    session: int = 50000,
    start_time: int = 12,
    board: str = "factory-reaper-test",
) -> reaper.ProcessRecord:
    return reaper.ProcessRecord(
        pid=pid,
        ppid=1,
        pgrp=pgrp,
        session=session,
        start_time=start_time,
        state="S",
        env={reaper.TASK_ENV: task_id, reaper.BOARD_ENV: board},
    )


def _patch_synthetic_process_view(monkeypatch, record):
    monkeypatch.setattr(reaper, "iter_process_records", lambda **kwargs: [record])
    monkeypatch.setattr(
        reaper,
        "read_process_record",
        lambda pid, **kwargs: record if pid == record.pid else None,
    )
    monkeypatch.setattr(reaper.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(reaper.os, "getsid", lambda pid: 2)


def test_f1_reaper_rejects_missing_initial_task_identity(tmp_path):
    signals = []
    report = reaper.reap_terminal_task_workers(
        {"task": {"status": "blocked", "current_run_id": None}},
        task_id="t_f1_missing",
        board="factory-reaper-test",
        proc_root=tmp_path,
        refresh=lambda: _terminal_detail("t_f1_missing"),
    )

    assert report["status"] == "skipped"
    assert "identity is missing" in report["reason"]
    assert signals == []


def test_reaper_rejects_empty_current_run_before_procfs(monkeypatch, tmp_path):
    task_id = "t_empty_run_marker"
    detail = _terminal_detail(task_id, current_run_id="")
    monkeypatch.setattr(
        reaper,
        "iter_process_records",
        lambda **kwargs: pytest.fail("empty run marker reached procfs"),
    )
    signals = []

    report = reaper.reap_terminal_task_workers(
        detail,
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        refresh=lambda: detail,
    )

    assert report["status"] == "not_applicable"
    assert "current run" in report["reason"]
    assert signals == []


def test_f1_reaper_revalidates_task_before_sigterm(monkeypatch, tmp_path):
    task_id = "t_f1_race"
    record = _synthetic_record(task_id)
    _patch_synthetic_process_view(monkeypatch, record)
    signals = []
    report = reaper.reap_terminal_task_workers(
        _terminal_detail(task_id),
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        reservation=_ReservationFixture(
            _terminal_detail(task_id),
            _terminal_detail(task_id, current_run_id=41),
        ),
        pidfd_open=lambda pid: pid,
        pidfd_send_signal=lambda fd, signum: signals.append((fd, signum)),
        close_handle=lambda fd: None,
        grace_seconds=0,
        monotonic=lambda: 0,
    )

    assert report["status"] == "partial"
    assert "current run" in report["reason"]
    assert signals == []


def test_f1_reaper_rejects_mismatched_refresh_identity(monkeypatch, tmp_path):
    task_id = "t_f1_mismatch"
    record = _synthetic_record(task_id)
    _patch_synthetic_process_view(monkeypatch, record)
    signals = []
    report = reaper.reap_terminal_task_workers(
        _terminal_detail(task_id),
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        reservation=_ReservationFixture(_terminal_detail("t_other")),
    )

    assert report["status"] == "skipped"
    assert "does not match" in report["reason"]
    assert signals == []


def test_f2_reaper_rejects_malformed_numeric_proc_entry(tmp_path):
    task_id = "t_f2_malformed"
    identity = {
        reaper.TASK_ENV: task_id,
        reaper.BOARD_ENV: "factory-reaper-test",
    }
    _write_proc_record(
        tmp_path,
        pid=52000,
        ppid=1,
        pgrp=52000,
        session=52000,
        start_time=12,
        env=identity,
    )
    malformed = tmp_path / "52001"
    malformed.mkdir()
    (malformed / "stat").write_text("not a proc stat", encoding="utf-8")
    signals = []
    detail = _terminal_detail(task_id)
    report = reaper.reap_terminal_task_workers(
        detail,
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        current_pid=os.getpid(),
        refresh=lambda: detail,
    )

    assert report["status"] == "unsafe"
    assert "unreadable record 52001" in report["reason"]
    assert signals == []


def test_f2_reaper_rejects_unreadable_captured_member(monkeypatch, tmp_path):
    task_id = "t_f2_captured"
    record = _synthetic_record(task_id, pid=53000, pgrp=53000, session=53000)
    _patch_synthetic_process_view(monkeypatch, record)
    monkeypatch.setattr(reaper, "read_process_record", lambda *args, **kwargs: None)
    signals = []
    detail = _terminal_detail(task_id)
    report = reaper.reap_terminal_task_workers(
        detail,
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        reservation=_ReservationFixture(detail),
        pidfd_open=lambda pid: pid,
        pidfd_send_signal=lambda fd, signum: signals.append((fd, signum)),
        close_handle=lambda fd: None,
    )

    assert report["status"] == "partial"
    assert "could not be read" in report["reason"]
    assert signals == []


def test_f3_reaper_rejects_moved_captured_member(monkeypatch, tmp_path):
    task_id = "t_f3_moved"
    captured = _synthetic_record(task_id, pid=54000, pgrp=54000, session=54000)
    moved = _synthetic_record(task_id, pid=54000, pgrp=54001, session=54000)
    monkeypatch.setattr(reaper, "iter_process_records", lambda **kwargs: [captured])
    monkeypatch.setattr(
        reaper, "read_process_record", lambda *args, **kwargs: moved
    )
    monkeypatch.setattr(reaper.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(reaper.os, "getsid", lambda pid: 2)
    signals = []
    detail = _terminal_detail(task_id)
    report = reaper.reap_terminal_task_workers(
        detail,
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        reservation=_ReservationFixture(detail),
        pidfd_open=lambda pid: pid,
        pidfd_send_signal=lambda fd, signum: signals.append((fd, signum)),
        close_handle=lambda fd: None,
    )

    assert report["status"] == "partial"
    assert "changed process group" in report["reason"]
    assert signals == []


def test_f4_reaper_rejects_unknown_caller_session(monkeypatch, tmp_path):
    task_id = "t_f4_session"
    record = _synthetic_record(task_id, pid=55000, pgrp=55000, session=55000)
    _patch_synthetic_process_view(monkeypatch, record)
    monkeypatch.setattr(
        reaper.os, "getsid", lambda pid: (_ for _ in ()).throw(PermissionError())
    )
    signals = []
    detail = _terminal_detail(task_id)
    report = reaper.reap_terminal_task_workers(
        detail,
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        refresh=lambda: detail,
    )

    assert report["status"] == "unsafe"
    assert "could not determine reaper session" in report["reason"]
    assert signals == []


def test_reaper_f5_force_authorization_never_escalates_to_sigkill(monkeypatch, tmp_path):
    task_id = "t_f5_authorization"
    group = reaper.ProcessGroup(
        session=56000,
        pgrp=56000,
        pids=(56000,),
        start_times={56000: 12},
    )
    snapshot_calls = []

    def snapshot(*args, **kwargs):
        snapshot_calls.append(args[0])
        if len(snapshot_calls) == 1:
            return False, "initial validation failed"
        return True, None

    monkeypatch.setattr(
        reaper,
        "_snapshot_still_bound",
        snapshot,
    )
    signals = []
    signalled, survivors, errors = reaper._terminate_groups(
        [group],
        task_id=task_id,
        board="factory-reaper-test",
        kanban_db=None,
        proc_root=tmp_path,
        grace_seconds=0,
        pidfd_open=lambda pid: pid,
        pidfd_send_signal=lambda fd, signum: signals.append((fd, signum)),
        close_handle=lambda fd: None,
        sleep=lambda seconds: None,
        monotonic=lambda: 0,
        refresh=lambda: _terminal_detail(task_id),
    )

    assert signalled == []
    assert signals == []
    assert survivors == []
    assert errors == ["initial validation failed"]
    assert len(snapshot_calls) == 1


def test_reaper_rejects_final_enumeration_pid_reuse_before_signal(monkeypatch, tmp_path):
    task_id = "t_r1_pid_reuse"
    group = reaper.ProcessGroup(
        session=57000,
        pgrp=57000,
        pids=(57000,),
        start_times={57000: 12},
    )
    captured = _synthetic_record(
        task_id,
        pid=57000,
        pgrp=57000,
        session=57000,
        start_time=12,
    )
    replacement = _synthetic_record(
        task_id,
        pid=57000,
        pgrp=57000,
        session=57000,
        start_time=99,
    )
    monkeypatch.setattr(reaper, "read_process_record", lambda *args, **kwargs: captured)
    monkeypatch.setattr(
        reaper, "iter_process_records", lambda **kwargs: [replacement]
    )
    monkeypatch.setattr(reaper, "_live_pids", lambda *args, **kwargs: [])
    signals = []

    signalled, survivors, errors = reaper._terminate_groups(
        [group],
        task_id=task_id,
        board="factory-reaper-test",
        kanban_db=None,
        proc_root=tmp_path,
        grace_seconds=0,
        pidfd_open=lambda pid: pid,
        pidfd_send_signal=lambda fd, signum: signals.append((fd, signum)),
        close_handle=lambda fd: None,
        sleep=lambda seconds: None,
        monotonic=lambda: 0,
        refresh=lambda: _terminal_detail(task_id),
    )

    assert signalled == []
    assert survivors == []
    assert signals == []
    assert errors == ["pid 57000 changed start time"]


def test_reaper_does_not_sigterm_later_member_after_identity_loss(
    monkeypatch, tmp_path
):
    task_id = "t_r1_term_member_identity_loss"
    first = _synthetic_record(task_id, pid=64000, pgrp=64000, session=64000)
    second = _synthetic_record(task_id, pid=64001, pgrp=64000, session=64000)
    current = {first.pid: first, second.pid: second}
    signals = []

    monkeypatch.setattr(
        reaper, "iter_process_records", lambda **kwargs: list(current.values())
    )
    monkeypatch.setattr(
        reaper,
        "read_process_record",
        lambda pid, **kwargs: current.get(pid),
    )

    def send(fd, signum):
        signals.append((fd, signum))
        if fd == first.pid and signum == signal.SIGTERM:
            current[second.pid] = reaper.ProcessRecord(
                pid=second.pid,
                ppid=second.ppid,
                pgrp=second.pgrp,
                session=second.session,
                start_time=second.start_time,
                state=second.state,
                env={},
            )

    signalled, survivors, errors = reaper._terminate_groups(
        [
            reaper.ProcessGroup(
                session=first.session,
                pgrp=first.pgrp,
                pids=(first.pid, second.pid),
                start_times={first.pid: first.start_time, second.pid: second.start_time},
            )
        ],
        task_id=task_id,
        board="factory-reaper-test",
        kanban_db=None,
        proc_root=tmp_path,
        grace_seconds=0,
        pidfd_open=lambda pid: pid,
        pidfd_send_signal=send,
        close_handle=lambda fd: None,
        refresh=lambda: _terminal_detail(task_id),
        sleep=lambda seconds: None,
        monotonic=lambda: 0,
    )

    assert signalled == [first.pid]
    assert signals == [(first.pid, signal.SIGTERM)]
    assert second.pid in survivors
    assert errors == [f"pid {second.pid} changed task identity"]


def test_reaper_does_not_sigkill_later_member_after_identity_rebind(
    monkeypatch, tmp_path
):
    task_id = "t_r1_kill_member_identity_rebind"
    first = _synthetic_record(task_id, pid=65000, pgrp=65000, session=65000)
    second = _synthetic_record(task_id, pid=65001, pgrp=65000, session=65000)
    current = {first.pid: first, second.pid: second}
    signals = []

    monkeypatch.setattr(
        reaper, "iter_process_records", lambda **kwargs: list(current.values())
    )
    monkeypatch.setattr(
        reaper,
        "read_process_record",
        lambda pid, **kwargs: current.get(pid),
    )

    def send(fd, signum):
        signals.append((fd, signum))
        if fd == first.pid and signum == signal.SIGKILL:
            current[second.pid] = _synthetic_record(
                "t_other_worker",
                pid=second.pid,
                pgrp=second.pgrp,
                session=second.session,
                start_time=second.start_time,
            )

    signalled, survivors, errors = reaper._terminate_groups(
        [
            reaper.ProcessGroup(
                session=first.session,
                pgrp=first.pgrp,
                pids=(first.pid, second.pid),
                start_times={first.pid: first.start_time, second.pid: second.start_time},
            )
        ],
        task_id=task_id,
        board="factory-reaper-test",
        kanban_db=None,
        proc_root=tmp_path,
        grace_seconds=0,
        pidfd_open=lambda pid: pid,
        pidfd_send_signal=send,
        close_handle=lambda fd: None,
        refresh=lambda: _terminal_detail(task_id),
        sleep=lambda seconds: None,
        monotonic=lambda: 0,
    )

    assert signalled == [first.pid, second.pid, first.pid]
    assert signals == [
        (first.pid, signal.SIGTERM),
        (second.pid, signal.SIGTERM),
        (first.pid, signal.SIGKILL),
    ]
    assert second.pid in survivors
    assert errors == [f"pid {second.pid} changed task identity"]


def test_reaper_rejects_malformed_task_envelope_before_procfs(monkeypatch, tmp_path):
    task_id = "t_r2_malformed_task"
    monkeypatch.setattr(
        reaper,
        "iter_process_records",
        lambda **kwargs: pytest.fail("malformed task reached procfs"),
    )
    signals = []

    report = reaper.reap_terminal_task_workers(
        {
            "task": "not-a-row",
            "id": task_id,
            "status": "blocked",
            "current_run_id": None,
        },
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        refresh=lambda: _terminal_detail(task_id),
    )

    assert report["status"] == "skipped"
    assert "identity is missing" in report["reason"]
    assert signals == []


def test_reaper_accepts_valid_nested_task_row_without_outer_fallback():
    task_id = "t_r2_nested"
    detail = {
        "task": {"id": task_id, "status": "blocked", "current_run_id": None},
        "id": "t_wrong_outer_row",
        "status": "running",
        "current_run_id": 41,
    }

    assert reaper._task_row(detail) == detail["task"]
    assert reaper._terminal_reason_for_task(detail, task_id=task_id) is None


def test_reaper_accepts_valid_direct_task_row_when_task_is_absent():
    task_id = "t_r2_direct"
    detail = {"id": task_id, "status": "blocked", "current_run_id": None}

    assert reaper._task_row(detail) == detail
    assert reaper._terminal_reason_for_task(detail, task_id=task_id) is None


@pytest.mark.parametrize(
    "grace_seconds",
    [math.inf, math.nan, "not-a-number", None, -1.0],
)
def test_reaper_rejects_invalid_grace_seconds_before_procfs(
    monkeypatch, tmp_path, grace_seconds
):
    task_id = "t_r3_invalid_grace"
    monkeypatch.setattr(
        reaper,
        "iter_process_records",
        lambda **kwargs: pytest.fail("invalid grace reached procfs"),
    )
    signals = []

    report = reaper.reap_terminal_task_workers(
        _terminal_detail(task_id),
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        refresh=lambda: _terminal_detail(task_id),
        grace_seconds=grace_seconds,
    )

    assert report["status"] == "skipped"
    assert "grace_seconds" in report["reason"]
    assert signals == []


@pytest.mark.parametrize(
    ("grace_seconds", "expected"),
    [
        (0, 0.0),
        (2.5, 2.5),
        (reaper.MAX_GRACE_SECONDS + 1, reaper.MAX_GRACE_SECONDS),
    ],
)
def test_reaper_validates_and_caps_finite_grace_seconds(grace_seconds, expected):
    assert reaper._validated_grace_seconds(grace_seconds) == (expected, None)


def test_reaper_caps_oversized_grace_before_force_kill(monkeypatch, tmp_path):
    task_id = "t_r3_oversized_grace"
    group = reaper.ProcessGroup(
        session=58000,
        pgrp=58000,
        pids=(58000,),
        start_times={58000: 12},
    )
    clock = {"now": 0.0}
    phase = {"killed": False}
    sleeps = []
    signals = []

    monkeypatch.setattr(reaper, "_snapshot_still_bound", lambda *args, **kwargs: (True, None))
    monkeypatch.setattr(
        reaper,
        "_membership_snapshot",
        lambda *args, **kwargs: reaper.MembershipSnapshot(
            live_pids=() if phase["killed"] else (58000,),
            uncertain_pids=(),
            errors=(),
        ),
    )

    def send(fd, signum):
        signals.append((fd, signum))
        if signum == signal.SIGKILL:
            phase["killed"] = True

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    signalled, survivors, errors = reaper._terminate_groups(
        [group],
        task_id=task_id,
        board="factory-reaper-test",
        kanban_db=None,
        proc_root=tmp_path,
        grace_seconds=reaper.MAX_GRACE_SECONDS + 1,
        pidfd_open=lambda pid: pid,
        pidfd_send_signal=send,
        close_handle=lambda fd: None,
        sleep=sleep,
        monotonic=lambda: clock["now"],
        refresh=lambda: _terminal_detail(task_id),
    )

    assert signalled == [58000, 58000]
    assert signals == [(58000, signal.SIGTERM), (58000, signal.SIGKILL)]
    assert survivors == []
    assert errors == []
    assert sum(sleeps) <= reaper.MAX_GRACE_SECONDS + 1e-6


@pytest.mark.parametrize(
    ("task_id", "board", "reason_fragment"),
    [
        ("t_r4_task ", "factory-reaper-test", "task id"),
        ("t_r4_board", "factory-reaper-test ", "board"),
    ],
)
def test_reaper_rejects_noncanonical_caller_identity_before_procfs(
    monkeypatch, tmp_path, task_id, board, reason_fragment
):
    canonical_task_id = "t_r4_task" if reason_fragment == "task id" else "t_r4_board"
    monkeypatch.setattr(
        reaper,
        "iter_process_records",
        lambda **kwargs: pytest.fail("noncanonical identity reached procfs"),
    )

    report = reaper.reap_terminal_task_workers(
        _terminal_detail(canonical_task_id),
        task_id=task_id,
        board=board,
        proc_root=tmp_path,
        refresh=lambda: _terminal_detail(canonical_task_id),
    )

    assert report["status"] == "skipped"
    assert reason_fragment in report["reason"]


@pytest.mark.parametrize(
    ("env_key", "env_value"),
    [
        (reaper.TASK_ENV, "t_r4_process "),
        (reaper.BOARD_ENV, "factory-reaper-test "),
    ],
)
def test_reaper_rejects_whitespace_normalized_process_identity(env_key, env_value):
    task_id = "t_r4_process"
    board = "factory-reaper-test"
    env = {reaper.TASK_ENV: task_id, reaper.BOARD_ENV: board}
    env[env_key] = env_value
    record = _synthetic_record(task_id, board=board)
    record = reaper.ProcessRecord(
        pid=record.pid,
        ppid=record.ppid,
        pgrp=record.pgrp,
        session=record.session,
        start_time=record.start_time,
        state=record.state,
        env=env,
    )

    assert not reaper.process_identity_matches(
        record,
        task_id=task_id,
        board=board,
        kanban_db=None,
    )


class _ReservationFixture:
    def __init__(self, *details):
        self.details = iter(details)
        self.reads = 0

    def assert_healthy(self):
        return None

    def read_task(self, task_id):
        self.reads += 1
        return next(self.details)


def test_r1_pidfd_unavailable_fails_closed_without_group_fallback(monkeypatch, tmp_path):
    task_id = "t_r1_no_pidfd"
    record = _synthetic_record(task_id)
    _patch_synthetic_process_view(monkeypatch, record)
    sends = []

    def unavailable(pid):
        raise OSError(errno.ENOSYS, "pidfd unavailable")

    report = reaper.reap_terminal_task_workers(
        _terminal_detail(task_id),
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        reservation=_ReservationFixture(_terminal_detail(task_id)),
        pidfd_open=unavailable,
        pidfd_send_signal=lambda fd, signum: sends.append((fd, signum)),
    )

    assert report["status"] == "unsafe"
    assert "stable process handle" in report["reason"]
    assert sends == []


def test_r1_pid_reuse_after_handle_acquisition_is_not_signalled(monkeypatch, tmp_path):
    task_id = "t_r1_handle_pid_reuse"
    captured = _synthetic_record(task_id, pid=61000, pgrp=61000, session=61000)
    replacement = _synthetic_record(
        task_id, pid=61000, pgrp=61000, session=61000, start_time=99
    )
    acquired = {"value": False}
    monkeypatch.setattr(
        reaper,
        "iter_process_records",
        lambda **kwargs: [replacement if acquired["value"] else captured],
    )
    monkeypatch.setattr(
        reaper,
        "read_process_record",
        lambda pid, **kwargs: replacement if acquired["value"] else captured,
    )
    monkeypatch.setattr(reaper.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(reaper.os, "getsid", lambda pid: 2)
    sends = []

    def open_handle(pid):
        acquired["value"] = True
        return 31

    report = reaper.reap_terminal_task_workers(
        _terminal_detail(task_id),
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        reservation=_ReservationFixture(_terminal_detail(task_id)),
        pidfd_open=open_handle,
        pidfd_send_signal=lambda fd, signum: sends.append((fd, signum)),
    )

    assert report["status"] == "partial"
    assert "changed start time" in report["reason"]
    assert sends == []


def test_r1_membership_change_between_validation_and_signal_fails_closed(
    monkeypatch, tmp_path
):
    task_id = "t_r1_membership_change"
    captured = _synthetic_record(task_id, pid=62000, pgrp=62000, session=62000)
    new_member = _synthetic_record(
        task_id, pid=62001, pgrp=62000, session=62000, start_time=13
    )
    acquired = {"value": False}
    monkeypatch.setattr(
        reaper,
        "iter_process_records",
        lambda **kwargs: [captured, new_member] if acquired["value"] else [captured],
    )
    monkeypatch.setattr(
        reaper,
        "read_process_record",
        lambda pid, **kwargs: captured if pid == captured.pid else new_member,
    )
    monkeypatch.setattr(reaper.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(reaper.os, "getsid", lambda pid: 2)
    sends = []

    def open_handle(pid):
        acquired["value"] = True
        return pid

    report = reaper.reap_terminal_task_workers(
        _terminal_detail(task_id),
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        reservation=_ReservationFixture(_terminal_detail(task_id)),
        pidfd_open=open_handle,
        pidfd_send_signal=lambda fd, signum: sends.append((fd, signum)),
    )

    assert report["status"] == "partial"
    assert "unexpected member" in report["reason"]
    assert sends == []


def test_r2_redispatch_readback_under_reservation_blocks_signal(monkeypatch, tmp_path):
    task_id = "t_r2_redispatch"
    record = _synthetic_record(task_id)
    _patch_synthetic_process_view(monkeypatch, record)
    reservation = _ReservationFixture(
        _terminal_detail(task_id), _terminal_detail(task_id, current_run_id=42)
    )
    sends = []

    report = reaper.reap_terminal_task_workers(
        _terminal_detail(task_id),
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        reservation=reservation,
        pidfd_open=lambda pid: 32,
        pidfd_send_signal=lambda fd, signum: sends.append((fd, signum)),
        grace_seconds=0,
    )

    assert report["status"] == "partial"
    assert "current run" in report["reason"]
    assert sends == []
    assert reservation.reads >= 2


def test_r3_new_member_after_sigterm_is_reported_as_survivor(monkeypatch, tmp_path):
    task_id = "t_r3_spawned_survivor"
    captured = _synthetic_record(task_id, pid=63000, pgrp=63000, session=63000)
    new_member = _synthetic_record(
        task_id, pid=63001, pgrp=63000, session=63000, start_time=13
    )
    phase = {"term_sent": False}
    monkeypatch.setattr(
        reaper,
        "iter_process_records",
        lambda **kwargs: [new_member] if phase["term_sent"] else [captured],
    )
    monkeypatch.setattr(
        reaper,
        "read_process_record",
        lambda pid, **kwargs: captured if pid == captured.pid else new_member,
    )
    monkeypatch.setattr(reaper.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(reaper.os, "getsid", lambda pid: 2)
    sends = []

    def send(fd, signum):
        sends.append((fd, signum))
        if signum == signal.SIGTERM:
            phase["term_sent"] = True

    report = reaper.reap_terminal_task_workers(
        _terminal_detail(task_id),
        task_id=task_id,
        board="factory-reaper-test",
        proc_root=tmp_path,
        reservation=_ReservationFixture(
            _terminal_detail(task_id), _terminal_detail(task_id),
            _terminal_detail(task_id), _terminal_detail(task_id),
        ),
        pidfd_open=lambda pid: 33,
        pidfd_send_signal=send,
        grace_seconds=0,
    )

    assert report["status"] == "partial"
    assert report["survivors"] == [63001]
    assert sends == [(33, signal.SIGTERM)]
    assert "unexpected member" in report["reason"]


def test_r2_lock_timeout_is_unsafe_and_sends_nothing(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(
        "CREATE TABLE tasks (id TEXT, status TEXT, current_run_id INTEGER)"
    )
    conn.execute("INSERT INTO tasks VALUES ('t_r2_locked', 'blocked', NULL)")
    conn.execute("BEGIN IMMEDIATE")
    base_record = _synthetic_record("t_r2_locked")
    record = reaper.ProcessRecord(
        pid=base_record.pid,
        ppid=base_record.ppid,
        pgrp=base_record.pgrp,
        session=base_record.session,
        start_time=base_record.start_time,
        state=base_record.state,
        env={**base_record.env, reaper.DB_ENV: str(db)},
    )
    monkeypatch.setattr(reaper, "iter_process_records", lambda **kwargs: [record])
    monkeypatch.setattr(reaper.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(reaper.os, "getsid", lambda pid: 2)
    sends = []

    report = reaper.reap_terminal_task_workers(
        _terminal_detail("t_r2_locked"),
        task_id="t_r2_locked",
        board="factory-reaper-test",
        kanban_db=db,
        proc_root=tmp_path,
        pidfd_open=lambda pid: 34,
        pidfd_send_signal=lambda fd, signum: sends.append((fd, signum)),
        reservation_timeout_seconds=0.01,
    )

    conn.rollback()
    conn.close()
    assert report["status"] == "unsafe"
    assert "reservation" in report["reason"] or "locked" in report["reason"]
    assert sends == []


def test_reaper_contains_no_killpg_fallback():
    assert "killpg" not in inspect.getsource(reaper)
