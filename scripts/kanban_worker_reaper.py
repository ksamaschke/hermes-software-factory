#!/usr/bin/env python3
"""Safely reconcile task-owned Hermes workers after terminal Kanban handoff.

Kanban workers are launched in a new POSIX session and inherit a small set of
identity environment variables.  A worker can therefore outlive the board run
that launched it (for example after ``kanban_block``), while a descendant such
as ``sleep`` remains alive.  This module only reaps a process group when the
current task readback is terminal, the process identity is exact, and every
member of the target group carries the same task/board identity.

The implementation is deliberately dependency-free and Linux/POSIX-focused.
On platforms without ``/proc`` it returns an explicit unsupported result rather
than falling back to broad process-name matching.
"""

from __future__ import annotations

import math
import os
import signal
import sqlite3
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote


PROC_ROOT = Path("/proc")
TASK_ENV = "HERMES_KANBAN_TASK"
RUN_ENV = "HERMES_KANBAN_RUN_ID"
BOARD_ENV = "HERMES_KANBAN_BOARD"
DB_ENV = "HERMES_KANBAN_DB"
IDENTITY_ENV_KEYS = frozenset({TASK_ENV, RUN_ENV, BOARD_ENV, DB_ENV})
# A caller may request a shorter grace period, but cleanup never waits longer
# than this fixed bound before force-authorization is considered.
MAX_GRACE_SECONDS = 30.0
# Survivor readback is independently bounded after SIGKILL.
MAX_SURVIVOR_READBACK_SECONDS = 2.0
MAX_DB_RESERVATION_SECONDS = 2.0
TERMINAL_TASK_STATES = frozenset(
    {"archived", "blocked", "cancelled", "done", "failed", "review"}
)


@dataclass(frozen=True)
class ProcessRecord:
    """Small, non-sensitive process snapshot used for identity checks."""

    pid: int
    ppid: int | None
    pgrp: int | None
    session: int | None
    start_time: int | None
    state: str | None
    env: Mapping[str, str]
    env_readable: bool = True
    readable: bool = True


@dataclass(frozen=True)
class ProcessGroup:
    """A validated task-owned process group ready for bounded termination."""

    session: int
    pgrp: int
    pids: tuple[int, ...]
    start_times: Mapping[int, int]


@dataclass(frozen=True)
class ProcessHandle:
    """A stable kernel handle plus the identity captured for its process."""

    pid: int
    fd: int
    start_time: int
    session: int
    pgrp: int


@dataclass(frozen=True)
class MembershipSnapshot:
    """Current target membership and any uncertainty in its enumeration."""

    live_pids: tuple[int, ...]
    uncertain_pids: tuple[int, ...]
    errors: tuple[str, ...]


class ReservationError(RuntimeError):
    """The exact board database could not be reserved or verified."""


def _database_file_identity(path: Path) -> tuple[int, int]:
    try:
        info = path.stat()
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise ReservationError(f"board database is unreadable: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ReservationError(f"board database is not a regular file: {path}")
    return info.st_dev, info.st_ino


def _sqlite_uri(path: Path) -> str:
    return f"file:{quote(str(path), safe='/')}?mode=rw"


class SQLiteWriteReservation:
    """Hold a bounded write reservation on one resolved Kanban database.

    The reservation deliberately performs no task mutation.  Its purpose is to
    prevent another SQLite writer (the dispatcher) from changing the exact task
    between the terminal readback and process signalling.
    """

    _REQUIRED_TASK_COLUMNS = frozenset({"id", "status", "current_run_id"})

    def __init__(
        self,
        path: str | os.PathLike[str] | None,
        task_id: str,
        *,
        timeout_seconds: float = MAX_DB_RESERVATION_SECONDS,
    ) -> None:
        if path is None:
            raise ReservationError("exact board database path is unavailable")
        text = _normalise_path(path)
        if text is None:
            raise ReservationError("exact board database path is unavailable")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReservationError("database reservation timeout is invalid") from exc
        if not math.isfinite(timeout) or timeout < 0:
            raise ReservationError("database reservation timeout is invalid")
        self.path = Path(text)
        self.task_id = task_id
        self.timeout_seconds = min(timeout, MAX_DB_RESERVATION_SECONDS)
        self._connection: sqlite3.Connection | None = None
        self._identity: tuple[int, int] | None = None

    def __enter__(self) -> "SQLiteWriteReservation":
        identity = _database_file_identity(self.path)
        try:
            connection = sqlite3.connect(
                _sqlite_uri(self.path),
                uri=True,
                timeout=self.timeout_seconds,
                isolation_level=None,
            )
            self._connection = connection
            connection.execute("BEGIN IMMEDIATE")
            self._identity = identity
            self._verify_connection()
            return self
        except ReservationError:
            self._close_after_failed_open()
            raise
        except (OSError, sqlite3.Error) as exc:
            self._close_after_failed_open()
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise ReservationError(
                    "board database write reservation timed out"
                ) from exc
            raise ReservationError(
                f"board database write reservation failed: {exc}"
            ) from exc

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        connection = self._connection
        self._connection = None
        self._identity = None
        if connection is None:
            return
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        try:
            connection.close()
        except sqlite3.Error:
            pass

    def _close_after_failed_open(self) -> None:
        connection = self._connection
        self._connection = None
        self._identity = None
        if connection is None:
            return
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        try:
            connection.close()
        except sqlite3.Error:
            pass

    def _verify_connection(self) -> None:
        connection = self._connection
        identity = self._identity
        if connection is None or identity is None:
            raise ReservationError("board database reservation is not active")
        if _database_file_identity(self.path) != identity:
            raise ReservationError("board database was replaced during reservation")
        try:
            rows = connection.execute("PRAGMA database_list").fetchall()
            main_path = next(
                (str(row[2]) for row in rows if len(row) >= 3 and row[1] == "main"),
                "",
            )
        except sqlite3.Error as exc:
            raise ReservationError(
                "board database identity readback failed"
            ) from exc
        if not main_path:
            raise ReservationError("board database identity is unreadable")
        try:
            resolved_main = str(Path(main_path).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError) as exc:
            raise ReservationError("board database identity is unreadable") from exc
        if resolved_main != str(self.path):
            raise ReservationError("SQLite opened a different board database")
        try:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
                if len(row) > 1 and isinstance(row[1], str)
            }
        except sqlite3.Error as exc:
            raise ReservationError("board database schema readback failed") from exc
        if not self._REQUIRED_TASK_COLUMNS.issubset(columns):
            raise ReservationError(
                "board database schema is missing the task identity columns"
            )

    def assert_healthy(self) -> None:
        """Recheck path identity and schema while the write lock is held."""

        self._verify_connection()

    def read_task(self, task_id: str) -> dict[str, Any] | None:
        """Read the exact minimal task identity under the write reservation."""

        self.assert_healthy()
        connection = self._connection
        if connection is None:
            raise ReservationError("board database reservation is not active")
        try:
            row = connection.execute(
                "SELECT id, status, current_run_id FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ReservationError("task readback failed under reservation") from exc
        if row is None:
            return None
        if not isinstance(row[0], str) or not isinstance(row[1], str):
            raise ReservationError("task readback identity is malformed")
        return {
            "task": {
                "id": row[0],
                "status": row[1],
                "current_run_id": row[2],
            }
        }



def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, OSError):
        return None



def _read_environ(path: Path) -> dict[str, str] | None:
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    values: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key_bytes, value_bytes = item.split(b"=", 1)
        try:
            key = key_bytes.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive for unusual procfs
            continue
        if key not in IDENTITY_ENV_KEYS:
            continue
        values[key] = value_bytes.decode("utf-8", errors="replace")
    return values



def _parse_stat(raw: str) -> tuple[int, int, int, int, str] | None:
    """Parse selected ``/proc/<pid>/stat`` fields.

    The command name is parenthesized and may itself contain ``)``; using the
    final closing parenthesis keeps the field offsets stable for such names.
    Fields after ``comm`` start at field 3, so starttime (field 22) is index 19.
    """

    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 2 :].split()
    if len(fields) <= 19:
        return None
    try:
        state = fields[0]
        ppid = int(fields[1])
        pgrp = int(fields[2])
        session = int(fields[3])
        start_time = int(fields[19])
    except (TypeError, ValueError):
        return None
    return ppid, pgrp, session, start_time, state



def read_process_record(pid: int, *, proc_root: Path = PROC_ROOT) -> ProcessRecord | None:
    """Read one process without exposing its command line or unrelated env."""

    if pid <= 0:
        return None
    process_dir = proc_root / str(pid)
    stat = _read_text(process_dir / "stat")
    if stat is None:
        return None
    parsed = _parse_stat(stat)
    if parsed is None:
        return None
    env = _read_environ(process_dir / "environ")
    env_readable = env is not None
    ppid, pgrp, session, start_time, state = parsed
    return ProcessRecord(
        pid=pid,
        ppid=ppid,
        pgrp=pgrp,
        session=session,
        start_time=start_time,
        state=state,
        env=env or {},
        env_readable=env_readable,
    )



def _unknown_process_record(pid: int) -> ProcessRecord:
    """Represent a numeric procfs entry whose identity could not be read."""

    return ProcessRecord(
        pid=pid,
        ppid=None,
        pgrp=None,
        session=None,
        start_time=None,
        state=None,
        env={},
        env_readable=False,
        readable=False,
    )


def iter_process_records(*, proc_root: Path = PROC_ROOT) -> list[ProcessRecord]:
    """Return a snapshot, retaining unreadable numeric entries as unknowns."""

    if not proc_root.is_dir():
        return [_unknown_process_record(0)]
    records: list[ProcessRecord] = []
    try:
        entries = list(proc_root.iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return [_unknown_process_record(0)]
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            records.append(_unknown_process_record(0))
            continue
        record = read_process_record(pid, proc_root=proc_root)
        records.append(record if record is not None else _unknown_process_record(pid))
    return records



def _normalise_path(value: str | os.PathLike[str] | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return text


def _canonical_identity_reason(
    value: Any, *, label: str, allow_empty: bool = False
) -> str | None:
    if not isinstance(value, str):
        return f"{label} is not a string"
    if not value:
        return None if allow_empty else f"empty {label}"
    if value != value.strip():
        return f"{label} has leading or trailing whitespace"
    return None


def process_identity_matches(
    record: ProcessRecord,
    *,
    task_id: str,
    board: str,
    kanban_db: str | os.PathLike[str] | None = None,
) -> bool:
    """Require task identity plus at least one exact board binding."""

    if _canonical_identity_reason(task_id, label="task id"):
        return False
    if _canonical_identity_reason(board, label="board", allow_empty=True):
        return False
    if not record.env_readable:
        return False
    actual_task = record.env.get(TASK_ENV, "")
    if not isinstance(actual_task, str) or actual_task != task_id:
        return False
    if actual_task != actual_task.strip():
        return False
    expected_board = board
    actual_board = record.env.get(BOARD_ENV, "")
    if not isinstance(actual_board, str):
        return False
    if actual_board != actual_board.strip():
        return False
    expected_db = _normalise_path(kanban_db)
    actual_db = _normalise_path(record.env.get(DB_ENV))

    # A configured board must be present and exact.  If callers do not have a
    # board slug, an exact shared DB path is the alternative binding.
    if expected_board:
        if actual_board != expected_board:
            return False
    elif expected_db is None or actual_db != expected_db:
        return False

    if expected_db is not None and actual_db != expected_db:
        return False
    return bool(actual_board or actual_db)



def _task_row(detail: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(detail, Mapping):
        return {}
    if "task" not in detail:
        return detail
    nested = detail["task"]
    return nested if isinstance(nested, Mapping) else {}



def _task_identity_reason(detail: Mapping[str, Any], task_id: str) -> str | None:
    row = _task_row(detail)
    value = row.get("id")
    if not isinstance(value, str) or not value:
        return "task readback identity is missing"
    if value != task_id:
        return "task readback identity does not match requested task"
    return None


def _terminal_reason_for_task(
    detail: Mapping[str, Any], *, task_id: str | None
) -> str | None:
    if task_id is not None:
        identity_reason = _task_identity_reason(detail, task_id)
        if identity_reason:
            return identity_reason
    row = _task_row(detail)
    status = str(row.get("status") or "").strip().lower()
    if status not in TERMINAL_TASK_STATES:
        return f"task status={status or 'unknown'} is not terminal"
    if "current_run_id" not in row:
        return "task current run state is missing"
    current_run = row["current_run_id"]
    if current_run is not None:
        return "task still has a current run"
    return None



def _terminal_reason(detail: Mapping[str, Any]) -> str | None:
    return _terminal_reason_for_task(detail, task_id=None)


def _validated_grace_seconds(value: Any) -> tuple[float | None, str | None]:
    """Return a finite non-negative grace period bounded by the fixed cap."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "grace_seconds must be a finite non-negative number"
    try:
        grace_seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, "grace_seconds must be a finite non-negative number"
    if not math.isfinite(grace_seconds):
        return None, "grace_seconds must be a finite non-negative number"
    if grace_seconds < 0:
        return None, "grace_seconds must be non-negative"
    return min(grace_seconds, MAX_GRACE_SECONDS), None


def _record_is_readable(record: ProcessRecord) -> bool:
    return (
        isinstance(record, ProcessRecord)
        and record.readable
        and isinstance(record.pid, int)
        and record.pid > 0
        and isinstance(record.ppid, int)
        and isinstance(record.pgrp, int)
        and isinstance(record.session, int)
        and isinstance(record.start_time, int)
        and isinstance(record.state, str)
        and isinstance(record.env, Mapping)
    )


def _pidfd_open(pid: int) -> int:
    """Open a Linux pidfd, or fail explicitly when the primitive is absent."""

    if not sys.platform.startswith("linux"):
        raise OSError("stable process handles are unavailable on this platform")
    opener = getattr(os, "pidfd_open", None)
    if not callable(opener):
        raise OSError("pidfd_open is unavailable")
    return opener(pid, 0)


def _pidfd_send_signal(fd: int, signum: int) -> None:
    """Signal one process through its stable pidfd."""

    if not sys.platform.startswith("linux"):
        raise OSError("stable process handles are unavailable on this platform")
    sender = getattr(signal, "pidfd_send_signal", None)
    if not callable(sender):
        raise OSError("pidfd_send_signal is unavailable")
    sender(fd, signum, None, 0)


def _close_process_handles(
    handles: Mapping[int, ProcessHandle], close_handle: Callable[[int], None]
) -> None:
    for handle in handles.values():
        try:
            close_handle(handle.fd)
        except (OSError, ValueError):
            pass


def _open_process_handles(
    groups: Iterable[ProcessGroup],
    *,
    open_handle: Callable[[int], int],
    close_handle: Callable[[int], None],
) -> tuple[dict[int, ProcessHandle], str | None]:
    """Open one stable handle per captured member before any signal."""

    handles: dict[int, ProcessHandle] = {}
    fds: set[int] = set()
    for group in groups:
        for pid in group.pids:
            try:
                fd = open_handle(pid)
            except Exception as exc:
                _close_process_handles(handles, close_handle)
                return {}, f"stable process handle unavailable for pid {pid}: {exc}"
            if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
                _close_process_handles(handles, close_handle)
                return {}, f"stable process handle for pid {pid} is unreadable"
            if fd in fds:
                _close_process_handles(handles, close_handle)
                return {}, f"stable process handle for pid {pid} was duplicated"
            fds.add(fd)
            handles[pid] = ProcessHandle(
                pid=pid,
                fd=fd,
                start_time=group.start_times[pid],
                session=group.session,
                pgrp=group.pgrp,
            )
    return handles, None


def _validated_groups(
    records: Iterable[ProcessRecord],
    *,
    task_id: str,
    board: str,
    kanban_db: str | os.PathLike[str] | None,
    current_pid: int,
) -> tuple[list[ProcessGroup], list[str]]:
    all_records = list(records)
    unreadable = [
        record
        for record in all_records
        if not isinstance(record, ProcessRecord) or not _record_is_readable(record)
    ]
    if unreadable:
        first = unreadable[0]
        pid = getattr(first, "pid", "unknown")
        return [], [f"process enumeration contains unreadable record {pid}"]
    matching = [
        record
        for record in all_records
        if process_identity_matches(
            record, task_id=task_id, board=board, kanban_db=kanban_db
        )
    ]
    if not matching:
        return [], []

    by_session: dict[int, list[ProcessRecord]] = {}
    for record in all_records:
        if any(record.session == match.session for match in matching):
            by_session.setdefault(record.session, []).append(record)

    groups: list[ProcessGroup] = []
    unsafe: list[str] = []
    try:
        own_group = os.getpgrp()
    except (ProcessLookupError, PermissionError, OSError):
        return [], ["could not determine reaper process group"]
    try:
        own_session = os.getsid(current_pid)
    except (ProcessLookupError, PermissionError, OSError):
        return [], ["could not determine reaper session"]
    for session, members in by_session.items():
        if session <= 1 or session == own_session:
            unsafe.append(f"session {session} is not an isolated worker session")
            continue
        if any(
            not process_identity_matches(
                member, task_id=task_id, board=board, kanban_db=kanban_db
            )
            for member in members
        ):
            unsafe.append(f"session {session} contains an unbound process")
            continue

        by_group: dict[int, list[ProcessRecord]] = {}
        for member in members:
            by_group.setdefault(member.pgrp, []).append(member)
        for pgrp, group_members in by_group.items():
            if pgrp <= 1 or pgrp == own_group:
                unsafe.append(f"process group {pgrp} is not safely killable")
                continue
            groups.append(
                ProcessGroup(
                    session=session,
                    pgrp=pgrp,
                    pids=tuple(sorted(member.pid for member in group_members)),
                    start_times={member.pid: member.start_time for member in group_members},
                )
            )
    return groups, unsafe



def _snapshot_still_bound(
    group: ProcessGroup,
    *,
    task_id: str,
    board: str,
    kanban_db: str | os.PathLike[str] | None,
    proc_root: Path,
    handles: Mapping[int, ProcessHandle] | None = None,
) -> tuple[bool, str | None]:
    """Reject PID reuse or a changed process identity before signalling.

    When handles are supplied this is specifically the post-acquisition
    validation required before any pidfd signal is sent.
    """

    if len(group.pids) != len(set(group.pids)):
        return False, f"process group {group.pgrp} has duplicate captured pids"
    if set(group.pids) != set(group.start_times):
        return False, f"process group {group.pgrp} has an incomplete identity snapshot"
    if any(
        not isinstance(pid, int)
        or pid <= 0
        or not isinstance(group.start_times[pid], int)
        or group.start_times[pid] < 0
        for pid in group.pids
    ):
        return False, f"process group {group.pgrp} has an invalid identity snapshot"
    if handles is not None:
        for pid in group.pids:
            handle = handles.get(pid)
            if handle is None:
                return False, f"pid {pid} has no stable process handle"
            if (
                handle.pid != pid
                or handle.start_time != group.start_times[pid]
                or handle.session != group.session
                or handle.pgrp != group.pgrp
                or isinstance(handle.fd, bool)
                or not isinstance(handle.fd, int)
                or handle.fd < 0
            ):
                return False, f"pid {pid} has an invalid stable process handle"
    try:
        current = {
            pid: read_process_record(pid, proc_root=proc_root) for pid in group.pids
        }
    except Exception:
        return False, "process identity readback failed during revalidation"
    for pid in group.pids:
        expected_start = group.start_times[pid]
        record = current.get(pid)
        if record is None:
            return False, f"pid {pid} could not be read during revalidation"
        if not _record_is_readable(record) or record.pid != pid:
            return False, f"pid {pid} is unreadable during revalidation"
        if record.start_time != expected_start:
            return False, f"pid {pid} changed start time"
        if record.session != group.session:
            return False, f"pid {pid} changed session"
        if record.pgrp != group.pgrp:
            return False, f"pid {pid} changed process group"
        if not process_identity_matches(
            record, task_id=task_id, board=board, kanban_db=kanban_db
        ):
            return False, f"pid {pid} changed task identity"
    # A new, unbound member in the same session/group is a fail-closed race.
    try:
        enumerated = list(iter_process_records(proc_root=proc_root))
    except Exception:
        return False, "process enumeration failed during revalidation"
    if any(not _record_is_readable(record) for record in enumerated):
        return False, "process enumeration is incomplete during revalidation"
    enumerated_by_pid: dict[int, ProcessRecord] = {}
    for record in enumerated:
        if record.pid in enumerated_by_pid:
            return False, f"process enumeration contains duplicate pid {record.pid}"
        enumerated_by_pid[record.pid] = record
    missing = [pid for pid in group.pids if pid not in enumerated_by_pid]
    if missing:
        return False, f"process enumeration missed captured pid {missing[0]}"
    for pid in group.pids:
        record = enumerated_by_pid[pid]
        expected_start = group.start_times[pid]
        if record.start_time != expected_start:
            return False, f"pid {pid} changed start time"
        if record.session != group.session:
            return False, f"pid {pid} changed session"
        if record.pgrp != group.pgrp:
            return False, f"pid {pid} changed process group"
        if not process_identity_matches(
            record, task_id=task_id, board=board, kanban_db=kanban_db
        ):
            return False, f"pid {pid} changed task identity"
    captured_pids = set(group.pids)
    for record in enumerated:
        if record.session == group.session and record.pgrp == group.pgrp:
            if record.pid not in captured_pids:
                return False, f"process group {group.pgrp} gained an unexpected member"
    return True, None



def _proc_entry_present(pid: int, *, proc_root: Path) -> bool:
    try:
        (proc_root / str(pid)).stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _membership_snapshot(
    group: ProcessGroup,
    *,
    proc_root: Path,
    task_id: str,
    board: str,
    kanban_db: str | os.PathLike[str] | None,
) -> MembershipSnapshot:
    """Enumerate the current session/group, not only captured process IDs."""

    live: set[int] = set()
    uncertain: set[int] = set()
    errors: list[str] = []
    captured = set(group.pids)
    try:
        enumerated = list(iter_process_records(proc_root=proc_root))
    except Exception:
        return MembershipSnapshot(
            live_pids=(),
            uncertain_pids=(),
            errors=("process enumeration failed during survivor readback",),
        )

    by_pid: dict[int, ProcessRecord] = {}
    for record in enumerated:
        if not isinstance(record, ProcessRecord):
            errors.append("process enumeration contains a malformed record")
            continue
        if record.pid in by_pid:
            errors.append(f"process enumeration contains duplicate pid {record.pid}")
            continue
        by_pid[record.pid] = record
        if not _record_is_readable(record):
            if record.pid > 0:
                uncertain.add(record.pid)
            errors.append(f"process enumeration contains unreadable record {record.pid}")

    for pid, expected_start in group.start_times.items():
        record = by_pid.get(pid)
        if record is None:
            # A missing proc entry is an exited process.  If the entry still
            # exists but cannot be read, retain it as an uncertain survivor.
            if not _proc_entry_present(pid, proc_root=proc_root):
                continue
            current = read_process_record(pid, proc_root=proc_root)
            if current is None or not _record_is_readable(current):
                live.add(pid)
                uncertain.add(pid)
                errors.append(f"pid {pid} became unreadable during survivor readback")
            elif current.state != "Z":
                live.add(pid)
                errors.append(f"pid {pid} left the captured process group")
            continue
        if not _record_is_readable(record) or record.pid != pid:
            live.add(pid)
            uncertain.add(pid)
            errors.append(f"pid {pid} is unreadable during survivor readback")
            continue
        if record.start_time != expected_start:
            live.add(pid)
            errors.append(f"pid {pid} changed start time during survivor readback")
            continue
        if record.session != group.session:
            live.add(pid)
            errors.append(f"pid {pid} changed session during survivor readback")
            continue
        if record.pgrp != group.pgrp:
            live.add(pid)
            errors.append(f"pid {pid} changed process group during survivor readback")
            continue
        if record.state != "Z":
            # A process with an unreadable environment is still a live
            # survivor.  Identity is required for a future signal, not for
            # conservative survivor reporting.
            live.add(pid)

    for record in enumerated:
        if not _record_is_readable(record):
            continue
        if record.session == group.session and record.pgrp == group.pgrp:
            if record.pid not in captured:
                live.add(record.pid)
                errors.append(f"process group {group.pgrp} gained an unexpected member")
    return MembershipSnapshot(
        live_pids=tuple(sorted(live)),
        uncertain_pids=tuple(sorted(uncertain)),
        errors=tuple(dict.fromkeys(errors)),
    )


def _live_pids(
    group: ProcessGroup,
    *,
    proc_root: Path,
    task_id: str,
    board: str,
    kanban_db: str | os.PathLike[str] | None,
) -> list[int]:
    """Return all known and uncertain current survivors for compatibility."""

    state = _membership_snapshot(
        group,
        proc_root=proc_root,
        task_id=task_id,
        board=board,
        kanban_db=kanban_db,
    )
    survivors = set(state.live_pids) | set(state.uncertain_pids)
    if not survivors:
        # Preserve the historical helper's direct-read behavior for callers
        # that provide a synthetic process reader without a procfs entry.
        for pid in group.pids:
            current = read_process_record(pid, proc_root=proc_root)
            if current is not None and current.state != "Z":
                survivors.add(pid)
    return sorted(survivors)



def _refresh_task_reason(
    refresh: Callable[[], Mapping[str, Any] | None] | None,
    *,
    task_id: str,
    signal_name: str,
    reservation: Any | None = None,
) -> str | None:
    try:
        if reservation is not None:
            reservation.assert_healthy()
            latest = reservation.read_task(task_id)
        elif refresh is not None:
            latest = refresh()
        else:
            return f"task readback unavailable before {signal_name}"
    except Exception:
        return f"task readback failed before {signal_name}"
    if latest is None:
        return f"task readback disappeared before {signal_name}"
    return _terminal_reason_for_task(latest, task_id=task_id)


def _terminate_groups(
    groups: Iterable[ProcessGroup],
    *,
    task_id: str,
    board: str,
    kanban_db: str | os.PathLike[str] | None,
    proc_root: Path,
    grace_seconds: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    refresh: Callable[[], Mapping[str, Any] | None] | None,
    reservation: Any | None = None,
    pidfd_open: Callable[[int], int] | None = None,
    pidfd_send_signal: Callable[[int, int], None] | None = None,
    close_handle: Callable[[int], None] | None = None,
) -> tuple[list[int], list[int], list[str]]:
    """Signal exact members through stable handles under one reservation."""

    validated_grace, grace_reason = _validated_grace_seconds(grace_seconds)
    if grace_reason:
        return [], [], [grace_reason]
    assert validated_grace is not None
    grace_seconds = validated_grace
    group_list = list(groups)
    errors: list[str] = []
    signalled_pids: list[int] = []
    signalled_group_ids: set[int] = set()
    term_authorized: list[ProcessGroup] = []
    final_states: dict[int, MembershipSnapshot] = {}

    def add_error(reason: str | None) -> None:
        if reason and reason not in errors:
            errors.append(reason)

    if reservation is not None:
        try:
            reservation.assert_healthy()
        except Exception:
            return [], [], ["board database reservation became unsafe before signalling"]

    open_fn = pidfd_open or _pidfd_open
    send_fn = pidfd_send_signal or _pidfd_send_signal
    close_fn = close_handle or os.close
    handles, handle_reason = _open_process_handles(
        group_list, open_handle=open_fn, close_handle=close_fn
    )
    if handle_reason:
        return [], [], [handle_reason]

    def read_membership(group: ProcessGroup) -> MembershipSnapshot:
        state = _membership_snapshot(
            group,
            proc_root=proc_root,
            task_id=task_id,
            board=board,
            kanban_db=kanban_db,
        )
        final_states[group.pgrp] = state
        for reason in state.errors:
            add_error(reason)
        return state

    try:
        # Handle acquisition is followed by a full identity and membership
        # revalidation.  The second validation after task readback closes the
        # gap between the two independent readbacks.
        for group in group_list:
            safe, reason = _snapshot_still_bound(
                group,
                task_id=task_id,
                board=board,
                kanban_db=kanban_db,
                proc_root=proc_root,
                handles=handles,
            )
            if not safe:
                add_error(reason or f"process group {group.pgrp} failed revalidation")
                continue
            task_reason = _refresh_task_reason(
                refresh,
                task_id=task_id,
                signal_name="SIGTERM",
                reservation=reservation,
            )
            if task_reason:
                add_error(task_reason)
                continue
            safe, reason = _snapshot_still_bound(
                group,
                task_id=task_id,
                board=board,
                kanban_db=kanban_db,
                proc_root=proc_root,
                handles=handles,
            )
            if not safe:
                add_error(reason or f"process group {group.pgrp} changed before SIGTERM")
                continue

            group_safe = True
            for pid in group.pids:
                handle = handles[pid]
                safe, reason = _snapshot_still_bound(
                    group,
                    task_id=task_id,
                    board=board,
                    kanban_db=kanban_db,
                    proc_root=proc_root,
                    handles=handles,
                )
                if not safe:
                    add_error(
                        reason or f"process group {group.pgrp} changed before SIGTERM"
                    )
                    group_safe = False
                    break
                try:
                    send_fn(handle.fd, signal.SIGTERM)
                    signalled_pids.append(pid)
                    signalled_group_ids.add(group.pgrp)
                except ProcessLookupError:
                    # The exact process ended between validation and the
                    # pidfd syscall; no other process is broadened by this.
                    pass
                except PermissionError:
                    add_error(f"permission denied for stable process handle {pid}")
                    group_safe = False
                    break
                except OSError as exc:
                    add_error(f"signal failed for stable process handle {pid}: {exc.errno}")
                    group_safe = False
                    break
                state = read_membership(group)
                if state.errors or state.uncertain_pids:
                    group_safe = False
                    break
            if group_safe:
                term_authorized.append(group)

        # Only groups that remained continuously clean after SIGTERM can reach
        # the force phase.  Poll count is capped in addition to the injected
        # monotonic deadline so a broken test clock cannot spin forever.
        deadline = monotonic() + max(0.0, grace_seconds)
        max_polls = max(1, int(grace_seconds / 0.05) + 1)
        poll_count = 0
        remaining_groups = list(term_authorized)
        while remaining_groups and monotonic() < deadline and poll_count < max_polls:
            poll_count += 1
            next_groups: list[ProcessGroup] = []
            for group in remaining_groups:
                state = read_membership(group)
                if state.errors or state.uncertain_pids:
                    continue
                if state.live_pids:
                    next_groups.append(group)
            remaining_groups = next_groups
            if remaining_groups:
                wait_for = min(0.05, max(0.0, deadline - monotonic()))
                if wait_for > 0:
                    sleep(wait_for)

        kill_sent: list[int] = []
        for group in remaining_groups:
            safe, reason = _snapshot_still_bound(
                group,
                task_id=task_id,
                board=board,
                kanban_db=kanban_db,
                proc_root=proc_root,
                handles=handles,
            )
            if not safe:
                add_error(reason or f"process group {group.pgrp} failed kill revalidation")
                continue
            task_reason = _refresh_task_reason(
                refresh,
                task_id=task_id,
                signal_name="SIGKILL",
                reservation=reservation,
            )
            if task_reason:
                add_error(task_reason)
                continue
            safe, reason = _snapshot_still_bound(
                group,
                task_id=task_id,
                board=board,
                kanban_db=kanban_db,
                proc_root=proc_root,
                handles=handles,
            )
            if not safe:
                add_error(reason or f"process group {group.pgrp} changed before SIGKILL")
                continue

            group_safe = True
            for pid in group.pids:
                handle = handles[pid]
                safe, reason = _snapshot_still_bound(
                    group,
                    task_id=task_id,
                    board=board,
                    kanban_db=kanban_db,
                    proc_root=proc_root,
                    handles=handles,
                )
                if not safe:
                    add_error(
                        reason or f"process group {group.pgrp} changed before SIGKILL"
                    )
                    group_safe = False
                    break
                try:
                    send_fn(handle.fd, signal.SIGKILL)
                    kill_sent.append(pid)
                    signalled_pids.append(pid)
                    signalled_group_ids.add(group.pgrp)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    add_error(f"permission denied for stable process handle {pid}")
                    group_safe = False
                    break
                except OSError as exc:
                    add_error(f"kill failed for stable process handle {pid}: {exc.errno}")
                    group_safe = False
                    break
                state = read_membership(group)
                if state.errors or state.uncertain_pids:
                    group_safe = False
                    break
            if not group_safe:
                continue

        # Always perform a final bounded membership readback for any group that
        # received a signal.  An unsafe/new member is retained in the result;
        # it is never converted into a false "reaped" outcome.
        signalled_groups = [
            group for group in group_list if group.pgrp in signalled_group_ids
        ]
        final_deadline = monotonic() + min(
            MAX_SURVIVOR_READBACK_SECONDS, max(0.2, grace_seconds)
        )
        final_polls = 0
        final_max_polls = max(1, int(min(
            MAX_SURVIVOR_READBACK_SECONDS, max(0.2, grace_seconds)
        ) / 0.05) + 1)
        while signalled_groups and final_polls < final_max_polls:
            final_polls += 1
            states = [read_membership(group) for group in signalled_groups]
            if any(state.errors or state.uncertain_pids for state in states):
                break
            if not any(state.live_pids for state in states):
                break
            if monotonic() >= final_deadline:
                break
            wait_for = min(0.05, max(0.0, final_deadline - monotonic()))
            if wait_for <= 0:
                break
            sleep(wait_for)

        survivors: set[int] = set()
        for state in final_states.values():
            survivors.update(state.live_pids)
            survivors.update(state.uncertain_pids)
        return signalled_pids, sorted(survivors), errors
    finally:
        _close_process_handles(handles, close_fn)



def _reap_with_reservation(
    detail: Mapping[str, Any],
    *,
    task_id: str,
    board: str,
    kanban_db: str | os.PathLike[str] | None,
    reservation: Any,
    refresh: Callable[[], Mapping[str, Any] | None] | None,
    grace_seconds: float,
    proc_root: Path,
    current_pid: int,
    unsafe: list[str],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    pidfd_open: Callable[[int], int] | None,
    pidfd_send_signal: Callable[[int, int], None] | None,
    close_handle: Callable[[int], None] | None,
) -> dict[str, Any]:
    """Perform the final readback and cleanup while ``reservation`` is held."""

    try:
        reservation.assert_healthy()
        latest = reservation.read_task(task_id)
    except Exception as exc:
        return {
            "status": "unsafe",
            "task_id": task_id,
            "reason": f"board database reservation/readback failed: {exc}",
        }
    if latest is None:
        return {
            "status": "unsafe",
            "task_id": task_id,
            "reason": "task readback disappeared under board database reservation",
        }
    latest_reason = _terminal_reason_for_task(latest, task_id=task_id)
    if latest_reason:
        status = "skipped" if "identity" in latest_reason else "not_applicable"
        return {"status": status, "task_id": task_id, "reason": latest_reason}

    # Re-scan after the reserved task readback.  This catches a worker that
    # ended naturally and establishes the exact member set for handle opening.
    records = iter_process_records(proc_root=proc_root)
    groups, unsafe_after_refresh = _validated_groups(
        records,
        task_id=task_id,
        board=board,
        kanban_db=kanban_db,
        current_pid=current_pid,
    )
    unsafe.extend(unsafe_after_refresh)
    if not groups:
        result: dict[str, Any] = {"status": "none", "task_id": task_id}
        if unsafe:
            result.update({"status": "unsafe", "reason": "; ".join(unsafe[:3])})
        return result

    pids = sorted({pid for group in groups for pid in group.pids})
    groups_payload = [group.pgrp for group in groups]
    signalled, survivors, errors = _terminate_groups(
        groups,
        task_id=task_id,
        board=board,
        kanban_db=kanban_db,
        proc_root=proc_root,
        grace_seconds=grace_seconds,
        sleep=sleep,
        monotonic=monotonic,
        refresh=refresh,
        reservation=reservation,
        pidfd_open=pidfd_open,
        pidfd_send_signal=pidfd_send_signal,
        close_handle=close_handle,
    )
    signalled_set = set(signalled)
    signalled_groups = [
        group.pgrp
        for group in groups
        if signalled_set.intersection(group.pids)
    ]
    all_errors = unsafe + errors
    handle_unavailable = any(
        "stable process handle" in reason for reason in errors
    )
    result = {
        "status": (
            "unsafe"
            if handle_unavailable and not signalled and not survivors
            else "reaped"
            if not survivors and not all_errors
            else "partial"
        ),
        "task_id": task_id,
        "pids": pids,
        "process_groups": groups_payload,
        "signalled_groups": signalled_groups,
        "signalled_pids": signalled,
        "survivors": survivors,
    }
    if all_errors:
        result["reason"] = "; ".join(all_errors[:3])
    return result


def reap_terminal_task_workers(
    detail: Mapping[str, Any],
    *,
    task_id: str,
    board: str,
    kanban_db: str | os.PathLike[str] | None = None,
    refresh: Callable[[], Mapping[str, Any] | None] | None = None,
    dry_run: bool = False,
    grace_seconds: float = 2.0,
    proc_root: Path = PROC_ROOT,
    current_pid: int | None = None,
    reservation: Any | None = None,
    reservation_timeout_seconds: float = MAX_DB_RESERVATION_SECONDS,
    pidfd_open: Callable[[int], int] | None = None,
    pidfd_send_signal: Callable[[int, int], None] | None = None,
    close_handle: Callable[[int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Reap terminal workers only under exact task/database ownership.

    Non-dry cleanup requires a bounded SQLite write reservation and Linux
    pidfds.  There is deliberately no numeric process-group signalling path.
    """

    task_identity_reason = _canonical_identity_reason(task_id, label="task id")
    if task_identity_reason:
        return {"status": "skipped", "task_id": task_id, "reason": task_identity_reason}
    board_identity_reason = _canonical_identity_reason(
        board, label="board", allow_empty=True
    )
    if board_identity_reason:
        return {"status": "skipped", "task_id": task_id, "reason": board_identity_reason}
    validated_grace, grace_reason = _validated_grace_seconds(grace_seconds)
    if grace_reason:
        return {"status": "skipped", "task_id": task_id, "reason": grace_reason}
    assert validated_grace is not None
    grace_seconds = validated_grace
    identity_reason = _task_identity_reason(detail, task_id)
    if identity_reason:
        return {"status": "skipped", "task_id": task_id, "reason": identity_reason}
    initial_reason = _terminal_reason_for_task(detail, task_id=task_id)
    if initial_reason:
        return {"status": "not_applicable", "task_id": task_id, "reason": initial_reason}
    if not proc_root.is_dir():
        return {
            "status": "unsupported",
            "task_id": task_id,
            "reason": "procfs is unavailable; no process-name fallback is permitted",
        }

    pid = current_pid if current_pid is not None else os.getpid()
    records = iter_process_records(proc_root=proc_root)
    groups, unsafe = _validated_groups(
        records,
        task_id=task_id,
        board=board,
        kanban_db=kanban_db,
        current_pid=pid,
    )
    if not groups:
        result: dict[str, Any] = {"status": "none", "task_id": task_id}
        if unsafe:
            result.update({"status": "unsafe", "reason": "; ".join(unsafe[:3])})
        return result

    pids = sorted({member for group in groups for member in group.pids})
    groups_payload = [group.pgrp for group in groups]
    if dry_run:
        return {
            "status": "would_reap",
            "task_id": task_id,
            "pids": pids,
            "process_groups": groups_payload,
        }

    if reservation is not None:
        expected_path = _normalise_path(kanban_db)
        actual_path = _normalise_path(getattr(reservation, "path", None))
        if expected_path is not None and actual_path is not None and expected_path != actual_path:
            return {
                "status": "unsafe",
                "task_id": task_id,
                "reason": "board database reservation does not match resolved database",
            }
        return _reap_with_reservation(
            detail,
            task_id=task_id,
            board=board,
            kanban_db=kanban_db,
            reservation=reservation,
            refresh=refresh,
            grace_seconds=grace_seconds,
            proc_root=proc_root,
            current_pid=pid,
            unsafe=unsafe,
            sleep=sleep,
            monotonic=monotonic,
            pidfd_open=pidfd_open,
            pidfd_send_signal=pidfd_send_signal,
            close_handle=close_handle,
        )

    if kanban_db is None:
        return {
            "status": "unsafe",
            "task_id": task_id,
            "reason": "exact board database is required for destructive cleanup",
        }
    try:
        with SQLiteWriteReservation(
            kanban_db,
            task_id,
            timeout_seconds=reservation_timeout_seconds,
        ) as acquired:
            return _reap_with_reservation(
                detail,
                task_id=task_id,
                board=board,
                kanban_db=kanban_db,
                reservation=acquired,
                refresh=refresh,
                grace_seconds=grace_seconds,
                proc_root=proc_root,
                current_pid=pid,
                unsafe=unsafe,
                sleep=sleep,
                monotonic=monotonic,
                pidfd_open=pidfd_open,
                pidfd_send_signal=pidfd_send_signal,
                close_handle=close_handle,
            )
    except ReservationError as exc:
        return {"status": "unsafe", "task_id": task_id, "reason": str(exc)}
