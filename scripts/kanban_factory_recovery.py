#!/usr/bin/env python3
"""Deterministic recovery layer around Hermes Kanban.

This is intentionally outside Hermes core. It repairs only mechanical factory
state that the supervised Hermes dispatcher cannot infer safely by itself:

* legacy LLM cron jobs that have creation-time provider/model snapshots but no
  durable fields;
* Kanban spawn failures caused solely by a duplicate clean managed Git
  worktree.

Dirty or non-managed worktrees are preserved and reported as genuine operator
disposition cases. Product failures, provider failures, review findings, and
human decisions are never auto-unblocked here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from kanban_worker_reaper import reap_terminal_task_workers


_COLLISION_RE = re.compile(
    r"(?:['\"](?P<branch>[^'\"]+)['\"]\s+is already used by worktree at\s+['\"](?P<path>[^'\"]+)['\"]|"
    r"branch\s+(?P<branch2>[^\s]+)\s+is already checked out at\s+(?P<path2>[^\s]+))",
    re.IGNORECASE,
)

_PARKED_ACK_MARKER = "[factory] parked backlog acknowledged"
_DEFAULT_CLI_TIMEOUT_SECONDS = 5.0
_DEFAULT_RECOVERY_BUDGET_SECONDS = 30.0


def _factory_cli_timeout_seconds() -> float:
    raw = os.environ.get("HERMES_FACTORY_CLI_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_CLI_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_CLI_TIMEOUT_SECONDS
    if not 1 <= value <= 30:
        return _DEFAULT_CLI_TIMEOUT_SECONDS
    return value


def _recovery_budget_seconds() -> float:
    raw = os.environ.get("HERMES_FACTORY_RECOVERY_BUDGET_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_RECOVERY_BUDGET_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_RECOVERY_BUDGET_SECONDS
    if not 5 <= value <= 120:
        return _DEFAULT_RECOVERY_BUDGET_SECONDS
    return value


def _kanban_db_path(board: str | None = None) -> Path | None:
    raw = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if raw:
        path = Path(raw).expanduser()
    else:
        real_home = os.environ.get("HERMES_REAL_HOME", "").strip()
        hermes_root = (
            Path(real_home).expanduser() if real_home else Path.home()
        ) / ".hermes"
        board_name = str(board or "").strip()
        if board_name:
            if Path(board_name).name != board_name or board_name in {".", ".."}:
                return None
            path = hermes_root / "kanban" / "boards" / board_name / "kanban.db"
        else:
            path = hermes_root / "kanban.db"
    if not path.exists():
        return None
    return path.resolve(strict=False)


def _readonly_task_detail(task_id: str, board: str | None = None) -> dict[str, Any] | None:
    path = _kanban_db_path(board)
    if path is None:
        return None
    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=2
        )
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute(
            "SELECT id, status, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass
    if row is None:
        return None
    return {
        "task": {
            "id": str(row[0]),
            "status": str(row[1]),
            "current_run_id": row[2],
        }
    }


def _readonly_blocked_tasks(board: str | None = None) -> list[dict[str, Any]] | None:
    path = _kanban_db_path(board)
    if path is None:
        return None
    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=2
        )
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT id, title, status, current_run_id "
            "FROM tasks WHERE status = 'blocked' "
            "ORDER BY priority DESC, created_at ASC"
        ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass
    return [
        {
            "id": str(row[0]),
            "title": str(row[1] or ""),
            "status": str(row[2]),
            "current_run_id": row[3],
        }
        for row in rows
    ]


def _run(argv: list[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    timeout = _factory_cli_timeout_seconds()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout:g}s"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _hermes(*args: str) -> tuple[int, str, str]:
    return _run(["hermes", *args])


def _json_command(*args: str) -> Any:
    code, stdout, stderr = _hermes(*args)
    if code != 0:
        raise RuntimeError(stderr or stdout or f"hermes {' '.join(args)} failed")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from hermes {' '.join(args)}: {exc}") from exc


def _jobs_path() -> Path:
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    return (Path(hermes_home).expanduser() if hermes_home else Path.home() / ".hermes") / "cron" / "jobs.json"


def _repair_cron_pins(*, dry_run: bool) -> list[str]:
    path = _jobs_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    repairs: list[str] = []
    for job in data.get("jobs", []):
        if not isinstance(job, dict) or job.get("no_agent") or not job.get("enabled", True):
            continue
        provider = str(job.get("provider") or "").strip()
        model = str(job.get("model") or "").strip()
        snapshot_provider = str(job.get("provider_snapshot") or "").strip()
        snapshot_model = str(job.get("model_snapshot") or "").strip()
        if provider or model or not (snapshot_provider or snapshot_model):
            continue
        # A partial snapshot is not enough to invent the missing axis. Leave it
        # alone for the normal job failure/preflight path.
        if not (snapshot_provider and snapshot_model):
            continue
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            continue
        if dry_run:
            repairs.append(
                f"would pin cron {job_id} to {snapshot_provider}/{snapshot_model}"
            )
            continue
        code, stdout, stderr = _run(
            [
                "hermes",
                "cron",
                "edit",
                job_id,
                "--provider",
                snapshot_provider,
                "--model",
                snapshot_model,
            ]
        )
        if code != 0:
            repairs.append(f"cron {job_id} pin failed: {stderr or stdout}")
            continue
        repairs.append(f"pinned cron {job_id} to {snapshot_provider}/{snapshot_model}")

    return repairs


def _task_detail(board: str, task_id: str) -> dict[str, Any] | None:
    try:
        value = _json_command("kanban", "--board", board, "show", "--json", task_id)
    except RuntimeError:
        fallback = _readonly_task_detail(task_id, board)
        if fallback is not None:
            fallback["_readback"] = "sqlite"
        return fallback
    return value if isinstance(value, dict) else None


def _is_parked(task: dict[str, Any]) -> bool:
    labels = task.get("labels") or []
    if isinstance(labels, list) and any(
        str(label).strip().lower() == "parked" for label in labels
    ):
        return True
    body = str(task.get("body") or "")
    return bool(re.search(r"(?im)^\s*-\s*Labels:\s*.*\bparked\b", body))


def _parked_acknowledged(detail: dict[str, Any]) -> bool:
    comments = detail.get("comments") or []
    return any(
        _PARKED_ACK_MARKER in str(comment.get("body") or "").lower()
        for comment in comments
        if isinstance(comment, dict)
    )


def _acknowledge_parked(
    board: str, task: dict[str, Any], *, dry_run: bool
) -> str | None:
    task_id = str(task.get("id") or "")
    if not task_id or task.get("status") != "blocked" or not _is_parked(task):
        return None
    detail = _task_detail(board, task_id)
    if not detail or detail.get("_readback") == "sqlite" or _parked_acknowledged(detail):
        return None
    message = (
        f"{_PARKED_ACK_MARKER}: this task is intentionally parked by policy; "
        "preserve blocked state and do not dispatch or auto-unblock."
    )
    if dry_run:
        return f"would acknowledge intentionally parked task {task_id}"
    code, stdout, stderr = _hermes(
        "kanban", "--board", board, "comment", task_id, message
    )
    if code != 0:
        return f"{task_id}: parked acknowledgement failed: {stderr or stdout}"
    verify = _task_detail(board, task_id)
    if not verify or not _parked_acknowledged(verify):
        return f"{task_id}: parked acknowledgement write was not verified"
    return f"acknowledged intentionally parked task {task_id}; preserved blocked state"


def _latest_spawn_error(detail: dict[str, Any]) -> str:
    runs = detail.get("runs") or []
    for run in reversed(runs):
        if not isinstance(run, dict):
            continue
        if run.get("status") in {"spawn_failed", "gave_up"}:
            return str(run.get("error") or "")
    return ""


def _worker_reap_grace_seconds() -> float:
    raw = os.environ.get("HERMES_FACTORY_WORKER_REAP_GRACE_SECONDS", "2").strip()
    try:
        value = float(raw)
    except ValueError:
        return 2.0
    if not 0 <= value <= 30:
        return 2.0
    return value


def _reconcile_terminal_worker(
    board: str, task: dict[str, Any], *, dry_run: bool
) -> str | None:
    """Remove only a task-owned worker left behind after terminal handoff."""

    raw_task_id = task.get("id")
    if (
        not isinstance(raw_task_id, str)
        or not raw_task_id
        or raw_task_id != raw_task_id.strip()
        or task.get("status") != "blocked"
    ):
        return None
    task_id = raw_task_id
    # The reaper needs only the current status/run identity. Prefer the
    # query-only row so a locked/hung CLI cannot serialize every blocked task.
    detail = _readonly_task_detail(task_id, board) or _task_detail(board, task_id)
    if not detail:
        return f"{task_id}: terminal worker readback unavailable"
    row = detail.get("task") or {}
    if row.get("status") != "blocked" or row.get("current_run_id") is not None:
        return None

    report = reap_terminal_task_workers(
        detail,
        task_id=task_id,
        board=board,
        kanban_db=_kanban_db_path(board),
        refresh=lambda: _task_detail(board, task_id),
        dry_run=dry_run,
        grace_seconds=_worker_reap_grace_seconds(),
    )
    status = str(report.get("status") or "unknown")
    if status in {"none", "not_applicable"}:
        return None
    fields = []
    if report.get("pids"):
        fields.append(f"pids={','.join(str(pid) for pid in report['pids'])}")
    if report.get("survivors"):
        fields.append(
            f"survivors={','.join(str(pid) for pid in report['survivors'])}"
        )
    if report.get("reason"):
        fields.append(f"reason={report['reason']}")
    suffix = f" ({'; '.join(fields)})" if fields else ""
    return f"{task_id}: terminal worker reconciliation={status}{suffix}"


def _collision(error: str) -> tuple[str, Path] | None:
    match = _COLLISION_RE.search(error)
    if not match:
        return None
    branch = match.group("branch") or match.group("branch2")
    path = match.group("path") or match.group("path2")
    if not branch or not path:
        return None
    return branch, Path(path).expanduser().resolve(strict=False)


def _git_status(path: Path) -> tuple[bool, str]:
    code, stdout, stderr = _run(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"]
    )
    if code != 0:
        return False, stderr or stdout
    return True, stdout


def _repo_root(path: Path) -> Path | None:
    code, stdout, _ = _run(["git", "-C", str(path), "rev-parse", "--git-common-dir"])
    if code != 0 or not stdout:
        return None
    common = Path(stdout)
    if not common.is_absolute():
        common = path / common
    common = common.expanduser().resolve(strict=False)
    if common.name != ".git":
        return None
    return common.parent


def _worktree_branch(path: Path) -> str | None:
    code, stdout, _ = _run(
        ["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"]
    )
    if code != 0 or not stdout:
        return None
    return stdout.strip()


def _repair_collision(board: str, task: dict[str, Any], *, dry_run: bool) -> str | None:
    task_id = str(task.get("id") or "")
    detail = _task_detail(board, task_id)
    if not detail or detail.get("_readback") == "sqlite":
        return None
    task_row = detail.get("task") or {}
    if task_row.get("status") != "blocked":
        return None
    if _is_parked(task) or _is_parked(task_row):
        return None
    error = _latest_spawn_error(detail)
    collision = _collision(error)
    if collision is None:
        return None
    branch, occupied = collision
    repo = _repo_root(occupied)
    if repo is None or occupied == repo or not occupied.is_dir():
        return f"{task_id}: collision path is not a managed linked worktree"
    actual_branch = _worktree_branch(occupied)
    if actual_branch != branch:
        return (
            f"{task_id}: preserved worktree {occupied}; collision branch {branch!r} "
            f"does not match checked-out branch {actual_branch!r}"
        )
    owner_id = occupied.name
    if not owner_id.startswith("t_"):
        return f"{task_id}: preserved worktree with non-task owner {occupied}"
    owner_detail = _task_detail(board, owner_id)
    if owner_detail and owner_detail.get("_readback") == "sqlite":
        owner_detail = None
    owner_status = ((owner_detail or {}).get("task") or {}).get("status")
    if owner_status not in {"done", "archived", "failed", "cancelled"}:
        return f"{task_id}: preserved worktree {occupied} owned by status={owner_status!r}"
    ok, dirty = _git_status(occupied)
    if not ok:
        return f"{task_id}: could not inspect {occupied}: {dirty}"
    if dirty:
        return f"{task_id}: preserved dirty worktree {occupied}"
    if dry_run:
        return f"would remove clean worktree {occupied} for branch {branch} and unblock {task_id}"

    code, stdout, stderr = _run(
        ["git", "-C", str(repo), "worktree", "remove", str(occupied)]
    )
    if code != 0 or occupied.exists():
        return f"{task_id}: clean worktree removal failed: {stderr or stdout}"

    code, stdout, stderr = _hermes(
        "kanban",
        "--board",
        board,
        "unblock",
        task_id,
        "factory recovery: removed clean duplicate worktree; preserved branch and retried dispatch",
    )
    if code != 0:
        return f"{task_id}: unblock failed after cleanup: {stderr or stdout}"

    verify = _task_detail(board, task_id)
    status = ((verify or {}).get("task") or {}).get("status")
    if status not in {"ready", "todo"}:
        return f"{task_id}: cleanup succeeded but readback status is {status!r}"
    return f"recovered {task_id}: removed clean {occupied} and read back status={status}"


def recover(board: str, *, dry_run: bool = False) -> list[str]:
    changes = _repair_cron_pins(dry_run=dry_run)
    try:
        tasks = _json_command("kanban", "--board", board, "list", "--status", "blocked", "--json")
    except RuntimeError:
        tasks = _readonly_blocked_tasks(board)
    if not isinstance(tasks, list):
        return changes
    blocked_tasks = [task for task in tasks if isinstance(task, dict)]
    deadline = time.monotonic() + _recovery_budget_seconds()
    budget_reported = False
    for task in blocked_tasks:
        if time.monotonic() >= deadline:
            changes.append("recovery budget exhausted; skipped remaining blocked-task repairs")
            budget_reported = True
            break
        change = _reconcile_terminal_worker(board, task, dry_run=dry_run)
        if change:
            changes.append(change)

    for task in blocked_tasks:
        if time.monotonic() >= deadline:
            if not budget_reported:
                changes.append("recovery budget exhausted; skipped remaining blocked-task repairs")
                budget_reported = True
            break
        change = _acknowledge_parked(board, task, dry_run=dry_run)
        if change:
            changes.append(change)
        if time.monotonic() >= deadline:
            if not budget_reported:
                changes.append("recovery budget exhausted; skipped remaining blocked-task repairs")
                budget_reported = True
            break
        change = _repair_collision(board, task, dry_run=dry_run)
        if change:
            changes.append(change)
    return changes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", default=os.environ.get("HERMES_FACTORY_BOARD"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.board:
        parser.error("--board or HERMES_FACTORY_BOARD is required")
    try:
        changes = recover(args.board, dry_run=args.dry_run)
    except Exception as exc:
        print(f"factory recovery failed: {exc}", file=sys.stderr)
        return 1
    if changes:
        print("\n".join(changes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
