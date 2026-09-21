#!/usr/bin/env python3
"""Guard and recover bounded Kanban review work outside Hermes core.

The supervised Hermes gateway remains the only dispatcher.  This add-on is a
small, deterministic board guard/recovery layer:

* invalid review leaves are blocked before they can be dispatched;
* timed-out leaves are preserved as REVIEW-INCOMPLETE and replaced only by
  narrower, contract-complete successors;
* replacement fan-in follows the current successor frontier recursively;
* every created card is read back before the old card/fan-in is archived.

It never edits a source worktree, tracker issue, Git history, cluster, or live
service.  It does not retry a timed-out prompt unchanged.
"""

from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from functools import lru_cache
import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl.
    fcntl = None  # type: ignore[assignment]


BOARD_DEFAULT = "default"
DEFAULT_REVIEWER_SKILLS = ("kanban-reviewer-contract", "scoped-subagent-audits")
VALID_VERDICTS = {"APPROVED", "CHANGES_REQUESTED", "REVIEW-INCOMPLETE"}
TIMEOUT_OUTCOMES = {"timed_out", "gave_up"}
INCOMPLETE_OUTCOMES = TIMEOUT_OUTCOMES | {
    "crashed",
    "failed",
    "spawn_failed",
    "cancelled",
    "reclaimed",
    "blocked",
}
DISPATCHABLE_STATUSES = {"ready", "todo", "review"}
FRONTIER_STATUSES = {"ready", "todo", "running", "review", "done"}
MAX_FILES_PER_LEAF = 5
MAX_FILES_PER_SUCCESSOR = 2
# Eight leaves cover the expected seven-file/two-question recovery packet;
# larger decompositions are preserved as incomplete instead of partially
# creating an unbounded or lossy successor set.
MAX_SUCCESSOR_SPECS = 8

# Change-scoped review budgets. See docs/change-scoped-review.md.
#
# Two tiers, because a single wall clock covering model latency, provider
# backoff, and command execution produces kills instead of verdicts. Observed
# failure mode: adversarial review leaves were killed at 602s/608s/610s against
# a single 600s cap -- one of them a depth-3 continuation of a single file and a
# single question. Narrowing scope could not fix a budget defect.
#
# - dispatch hard cap: the dispatcher's --max-runtime SIGTERM/SIGKILL backstop
#   against a hung worker. Not a target and not a budget.
# - evidence budget: the point at which the reviewer must itself return a
#   verdict with recorded gaps. Reaching it is correct behavior.
# - command timeout: hard cap on any single command, so a full project gate
#   cannot consume the review.
REVIEW_DISPATCH_HARD_CAP_SECONDS = int(
    os.environ.get("REVIEW_DISPATCH_HARD_CAP_SECONDS", "1800")
)
REVIEW_EVIDENCE_BUDGET_SECONDS = int(
    os.environ.get("REVIEW_EVIDENCE_BUDGET_SECONDS", "900")
)
REVIEW_COMMAND_TIMEOUT_SECONDS = int(
    os.environ.get("REVIEW_COMMAND_TIMEOUT_SECONDS", "120")
)
# Bounded fan-in reads leaf reports only; it never rescans or re-runs checks.
FANIN_DISPATCH_HARD_CAP_SECONDS = int(
    os.environ.get("FANIN_DISPATCH_HARD_CAP_SECONDS", "1800")
)
FANIN_EVIDENCE_BUDGET_SECONDS = int(
    os.environ.get("FANIN_EVIDENCE_BUDGET_SECONDS", "900")
)

_REVIEW_LEAF_RE = re.compile(
    r"review[_ ]type\s*:\s*(?:fresh,\s*)?read-only\s+adversarial\s+"
    r"(?:code(?:/config)?|config)\s+review\s+leaf",
    re.IGNORECASE,
)
_FANIN_RE = re.compile(
    r"review[_ ]type\s*:\s*bounded\s+fan-in|"
    r"review\s+synthesis\s*/\s*bounded\s+fan-in\s+task",
    re.IGNORECASE,
)
_PROHIBITED_REVIEW_INSTRUCTIONS = (
    re.compile(r"\btdd\s+first\b", re.IGNORECASE),
    re.compile(r"\bwrite\s+(?:the\s+)?(?:fix|production\s+code)\b", re.IGNORECASE),
    re.compile(r"\bmake\s+(?:the\s+)?production\s+change\b", re.IGNORECASE),
    re.compile(
        r"\bimplement\s+(?:the|a|this)\s+(?:fix|change|feature)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bfile\s+(?:the|a)\s+(?:forgejo|github)\s+"
        r"(?:issue|pull\s+request)\b",
        re.IGNORECASE,
    ),
)
_FIELD_STOP_RE = re.compile(
    r"\.\s+(?=(?:source|target|repository|branch|candidate|implementer|reviewer|"
    r"lens|exact|acceptance|focused|stop|read[_ -]?only|max[_ -]?runtime|max[_ -]?retries)\b)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Process and board helpers


def _run(argv: Sequence[str], *, timeout: int = 120) -> tuple[int, str, str]:
    proc = subprocess.run(
        list(argv),
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
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


def _show(board: str, task_id: str) -> dict[str, Any]:
    value = _json_command("kanban", "--board", board, "show", "--json", task_id)
    if not isinstance(value, dict):
        raise RuntimeError(f"task {task_id} readback was not an object")
    return value


def _list(board: str) -> list[dict[str, Any]]:
    value = _json_command("kanban", "--board", board, "list", "--archived", "--json")
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


@lru_cache(maxsize=32)
def _board_db_path(board: str) -> Path:
    """Resolve a board database through Hermes' own board registry."""
    value = _json_command("kanban", "boards", "list", "--json")
    if isinstance(value, list):
        for entry in value:
            if not isinstance(entry, dict) or entry.get("slug") != board:
                continue
            path = _clean(entry.get("db_path"))
            if path:
                return Path(path).expanduser().resolve()
    raise RuntimeError(f"board database path is unavailable for {board!r}")


def _durable_task_fields(board: str, task_id: str) -> dict[str, Any]:
    """Read stored budget fields without opening the database for writing."""
    path = _board_db_path(board)
    if not path.is_file():
        raise RuntimeError(f"board database is unavailable: {path}")
    uri = f"{path.as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                "SELECT max_runtime_seconds, max_retries FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"could not read durable fields for {task_id}: {exc}") from exc
    if row is None:
        raise RuntimeError(f"task {task_id} is absent from the board database")
    return {"max_runtime_seconds": row[0], "max_retries": row[1]}


def _enrich_task_with_durable_fields(board: str, task: dict[str, Any]) -> dict[str, Any]:
    task_id = _clean(task.get("id"))
    if not task_id:
        raise RuntimeError("task has no id for durable-field readback")
    enriched = dict(task)
    enriched.update(_durable_task_fields(board, task_id))
    return enriched


def _active_profile_name() -> str:
    try:
        profiles = importlib.import_module("hermes_cli.profiles")
        resolver = getattr(profiles, "get_active_profile_name", None)
        value = resolver() if callable(resolver) else None
    except Exception as exc:
        raise RuntimeError("active coordinator profile is unavailable") from exc
    actor = str(value or "").strip()
    if not actor:
        raise RuntimeError("active coordinator profile is unavailable")
    return actor


def _recover_native_review_remediation_handoffs(
    board: str,
    *,
    apply: bool,
) -> list[str]:
    """Consume native standalone-review outbox rows through the active runtime."""
    path = _board_db_path(board)
    uri = f"{path.as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'review_remediation_handoffs'"
            ).fetchone()
            if table is None:
                return []
            pending = [
                str(row[0])
                for row in connection.execute(
                    "SELECT handoff_key FROM review_remediation_handoffs "
                    "WHERE status = 'pending' ORDER BY created_at, handoff_key"
                ).fetchall()
            ]
    except sqlite3.Error as exc:
        raise RuntimeError(f"could not read native review handoffs: {exc}") from exc
    if not pending:
        return []
    if not apply:
        return [f"would consume native review remediation handoff {key}" for key in pending]

    try:
        native_db = importlib.import_module("hermes_cli.kanban_db")
    except Exception as exc:
        raise RuntimeError("active native Kanban runtime is unavailable") from exc
    consumer = getattr(native_db, "consume_standalone_review_handoffs", None)
    if not callable(consumer):
        raise RuntimeError(
            "pending review remediation handoffs exist but the active runtime has no native consumer"
        )
    actor = _active_profile_name()
    with native_db.connect(path) as connection:
        raw_results = consumer(connection, actor=actor, limit=max(len(pending), 1))
    if not isinstance(raw_results, list) or not all(
        isinstance(result, dict) for result in raw_results
    ):
        raise RuntimeError("native review handoff consumer returned malformed readback")
    results: list[dict[str, Any]] = raw_results
    consumed = {str(result.get("handoff_key")) for result in results if isinstance(result, dict)}
    missing = [key for key in pending if key not in consumed]
    if missing:
        raise RuntimeError(
            "native review handoff consumption returned incomplete readback: " + ", ".join(missing)
        )
    return [
        "consumed native review remediation handoff "
        f"{result['handoff_key']} -> {result['successor_task_id']}"
        for result in results
    ]


def _task_id(value: Any) -> str:
    if isinstance(value, dict):
        task = value.get("task")
        if isinstance(task, dict) and task.get("id"):
            return str(task["id"])
        if value.get("id"):
            return str(value["id"])
    return ""


def _task_from_detail(detail: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    task = detail.get("task")
    return task if isinstance(task, dict) else fallback


# ---------------------------------------------------------------------------
# Packet parsing and validation


def _clean(value: Any) -> str:
    cleaned = str(value or "").strip().strip("`").strip().strip(".,")
    if cleaned.lower() in {
        "not declared",
        "unknown",
        "unresolved",
        "unresolved-candidate-requires-preflight",
    }:
        return ""
    return cleaned


def _field(body: str, name: str) -> str:
    """Read a canonical field or a same-line human-readable field."""
    escaped = re.escape(name).replace(r"\ ", r"[ _]")
    line_match = re.search(rf"(?im)^\s*{escaped}\s*:\s*(.*?)\s*$", body)
    if line_match:
        value = _FIELD_STOP_RE.split(line_match.group(1), maxsplit=1)[0]
        return _clean(value)

    inline_match = re.search(rf"(?i){escaped}\s*:\s*([^\n]+)", body)
    if not inline_match:
        return ""
    value = _FIELD_STOP_RE.split(inline_match.group(1), maxsplit=1)[0]
    return _clean(value)


def _first_field(body: str, *names: str) -> str:
    for name in names:
        value = _field(body, name)
        if value:
            return value
    return ""


def _int_value(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _bool_value(value: str) -> Optional[bool]:
    lowered = value.strip().lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    return None


def _section_lines(body: str, section: str) -> list[str]:
    """Collect list entries after a canonical or Markdown section heading."""
    lines = body.splitlines()
    start: Optional[int] = None
    pattern = re.compile(rf"^\s*(?:#+\s*)?{re.escape(section)}\b.*$", re.IGNORECASE)
    for index, line in enumerate(lines):
        if pattern.match(line):
            start = index + 1
            break
    if start is None:
        return []

    result: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            break
        if stripped.startswith("-"):
            value = stripped[1:].strip().strip("`")
        else:
            number = re.match(r"^\d+[.)]\s+(.*)$", stripped)
            if not number:
                break
            value = number.group(1).strip().strip("`")
        if value:
            result.append(value)
    return result


def _find_natural_section(body: str, expression: str) -> list[str]:
    lines = body.splitlines()
    header = re.compile(expression, re.IGNORECASE)
    for index, line in enumerate(lines):
        if not header.search(line):
            continue
        result: list[str] = []
        for candidate in lines[index + 1 :]:
            stripped = candidate.strip()
            if not stripped or stripped.startswith("#"):
                break
            if stripped.startswith("-"):
                value = stripped[1:].strip().strip("`")
            else:
                number = re.match(r"^\d+[.)]\s+(.*)$", stripped)
                if not number:
                    break
                value = number.group(1).strip().strip("`")
            if value:
                result.append(value)
        return result
    return []


def _parse_inline_numbered_questions(value: str) -> Optional[list[str]]:
    """Split only a genuine sequential ``1) A? 2) B?`` list."""
    markers = list(re.finditer(r"(?<!\w)(\d+)[.)]\s+", value))
    if len(markers) < 2 or markers[0].start() != 0:
        return None
    numbers = [int(marker.group(1)) for marker in markers]
    if numbers != list(range(1, len(numbers) + 1)):
        return None
    questions = [
        value[marker.end() : next_marker.start()].strip()
        for marker, next_marker in zip(markers, markers[1:])
    ]
    questions.append(value[markers[-1].end() :].strip())
    return questions if all(questions) else None


def _parse_questions(body: str) -> list[str]:
    explicit = _first_field(body, "one_acceptance_question", "one acceptance question")
    if explicit:
        numbered = _parse_inline_numbered_questions(explicit)
        return numbered or [explicit]

    questions = _section_lines(body, "acceptance questions")
    if not questions:
        lines = body.splitlines()
        for index, line in enumerate(lines):
            if not re.search(r"acceptance\s+questions?\b", line, re.IGNORECASE):
                continue
            parsed: list[str] = []
            for candidate in lines[index + 1 :]:
                stripped = candidate.strip()
                if not stripped:
                    if parsed:
                        break
                    continue
                if stripped.lower().startswith("lens:"):
                    continue
                if stripped.startswith("-"):
                    value = stripped[1:].strip().strip("`")
                else:
                    number = re.match(r"^\d+[.)]\s+(.*)$", stripped)
                    if not number:
                        break
                    value = number.group(1).strip().strip("`")
                if value:
                    parsed.append(value)
            if parsed:
                return parsed
            break
    if questions:
        return questions

    # Markdown packets sometimes use a singular heading with the answer on the
    # following paragraph rather than a key/value field.
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^\s*#+\s*one\s+acceptance\s+question\b", line, re.IGNORECASE):
            for candidate in lines[index + 1 :]:
                stripped = candidate.strip()
                if not stripped or stripped.startswith("#"):
                    break
                if stripped.lower().startswith(("lens:", "focused checks:", "non-goals:")):
                    break
                return [_clean(stripped)]
    return []


def _parse_lens(body: str) -> str:
    value = _first_field(body, "review_lens", "review lens", "lens")
    if value:
        return value
    match = re.search(r"(?im)^\s*lens\s*:\s*(.*?)\s*$", body)
    return _clean(match.group(1)) if match else ""


def _parse_candidate(body: str) -> str:
    value = _first_field(body, "candidate_commit", "candidate ref", "candidate")
    if not value:
        match = re.search(
            r"(?i)\bcandidate(?:\s+is)?\s*:?\s*(?:head\s+)?([^\n]+)",
            body,
        )
        value = _clean(match.group(1)) if match else ""
    sha = re.search(r"\b[0-9a-f]{40}\b", value, re.IGNORECASE)
    return sha.group(0) if sha else _clean(value)


def _repository_from_worktree(worktree: str) -> str:
    parts = Path(worktree).parts
    if ".worktrees" in parts:
        index = parts.index(".worktrees")
        if index > 0:
            return parts[index - 1]
    return Path(worktree).name


def _parse_target_worktree(task: dict[str, Any], body: str) -> str:
    value = _first_field(body, "target_worktree", "target worktree", "repository/worktree", "target repository/worktree")
    if not value:
        value = _clean(task.get("workspace_path"))
    if not value:
        match = re.search(r"(/[^\s`]+(?:/\.worktrees/|/worktrees/)[^\s`]+)", body)
        value = _clean(match.group(1)) if match else ""
    return value.rstrip(".,")


def _profile_value(body: str, *names: str) -> str:
    value = _first_field(body, *names)
    match = re.match(r"[A-Za-z0-9._-]+", value)
    return match.group(0) if match else value


def parse_review_packet(task: dict[str, Any]) -> dict[str, Any]:
    body = str(task.get("body") or "")
    implementation_raw = _first_field(body, "implementation_task", "implementation task")
    implementation_match = re.search(r"\bt_[a-z0-9_-]+\b", implementation_raw, re.IGNORECASE)
    implementation_task = implementation_match.group(0) if implementation_match else implementation_raw
    reviewer = _profile_value(body, "reviewer_profile", "reviewer profile") or _clean(task.get("assignee"))
    implementer = _profile_value(body, "implementer_profile", "implementer profile")
    vendor_independence = _first_field(body, "vendor_family_independent", "vendor family independent")
    raw_depth = _first_field(body, "continuation_depth", "continuation depth")
    continuation = _first_field(body, "continuation_of", "continuation of")
    allowed = _first_field(body, "allowed_verdicts", "allowed verdicts")
    allowed_verdicts = []
    for item in re.split(r"[,;|]", allowed):
        if not item.strip():
            continue
        token = item.strip().upper().replace(" ", "_")
        if token == "REVIEW_INCOMPLETE":
            token = "REVIEW-INCOMPLETE"
        elif token == "CHANGES-REQUESTED":
            token = "CHANGES_REQUESTED"
        allowed_verdicts.append(token)
    files = _section_lines(body, "exact_scope")
    if not files:
        files = _find_natural_section(body, r"exact\s+scope\b")
    checks = _section_lines(body, "focused_checks")
    if not checks:
        checks = _find_natural_section(body, r"focused\s+checks\b")
    packet: dict[str, Any] = {
        "task_id": _clean(task.get("id")),
        "title": _clean(task.get("title")),
        "implementation_task": implementation_task,
        "source_issue": _first_field(
            body,
            "source_issue",
            "source issue",
            "source_pr",
            "source PR",
            "forgejo_issue",
            "forgejo issue",
            "original forgejo issue",
        ),
        "target_repository": _first_field(body, "target_repository", "target repository", "repository"),
        "target_worktree": _parse_target_worktree(task, body),
        "branch": _first_field(body, "branch"),
        "candidate_commit": _parse_candidate(body),
        "implementer_profile": _profile_value(
            body, "implementer_profile", "implementer profile", "implementer"
        ),
        "reviewer_profile": _profile_value(
            body, "reviewer_profile", "reviewer profile", "reviewer"
        ),
        "vendor_family_independent": (
            _bool_value(vendor_independence)
            if vendor_independence
            else (True if re.search(r"independent\s+vendor[- ]family", body, re.IGNORECASE) else None)
        ),
        "read_only_source": _bool_value(_first_field(body, "read_only_source", "read-only source")),
        "max_runtime_seconds": _int_value(
            _first_field(body, "max_runtime_seconds", "max runtime seconds")
        ),
        "max_retries": _int_value(_first_field(body, "max_retries", "max retries")),
        "allowed_verdicts": allowed_verdicts,
        "review_lens": _parse_lens(body),
        "files": files,
        "questions": _parse_questions(body),
        "checks": checks,
        "stop_condition": _first_field(body, "stop_condition", "stop condition"),
        "continuation_of": continuation,
        "continuation_depth": _int_value(raw_depth) or (1 if continuation else 0),
        "body": body,
    }
    if not packet["target_repository"] and packet["target_worktree"]:
        packet["target_repository"] = _repository_from_worktree(packet["target_worktree"])
    return packet


def is_review_fanin(task: dict[str, Any]) -> bool:
    body = str(task.get("body") or "")
    title = str(task.get("title") or "").lower()
    return bool(_FANIN_RE.search(body) or ("review synthesis" in title and "leaf_tasks" in body))


def is_review_leaf(task: dict[str, Any]) -> bool:
    body = str(task.get("body") or "")
    title = str(task.get("title") or "").lower()
    if is_review_fanin(task):
        return False
    if _REVIEW_LEAF_RE.search(body):
        return True
    if not any(
        title.startswith(prefix)
        for prefix in ("review leaf ", "review continuation", "fresh review", "fresh independent review", "independent review")
    ):
        return False
    packet = parse_review_packet(task)
    return bool(
        packet["implementation_task"]
        and packet["reviewer_profile"]
        and re.search(r"read[- ]only", body, re.IGNORECASE)
        and re.search(r"review", body, re.IGNORECASE)
    )


_HUNK_RANGE_RE = re.compile(r"^\d+-\d+(,\d+-\d+)*$")


def _split_hunk_ranges(value: str) -> tuple[str, str | None]:
    """Split `path:1-20,40-55` into (path, ranges).

    A change manifest entry may carry hunk ranges. Only a trailing
    `:<start>-<end>[,<start>-<end>...]` suffix is treated as ranges; any other
    colon still makes the entry invalid.
    """
    path = _clean(value)
    if ":" not in path:
        return path, None
    head, _, tail = path.rpartition(":")
    if head and _HUNK_RANGE_RE.match(tail):
        return head, tail
    return path, None


def _exact_file_path(value: str) -> bool:
    path, ranges = _split_hunk_ranges(value)
    if not path or path.startswith("/") or path.endswith("/"):
        return False
    if any(char in path for char in "*?[]{}") or "\x00" in path:
        return False
    if any(part in {"", ".", ".."} for part in path.split("/")):
        return False
    if ":" in path:
        return False
    if ranges is not None:
        for chunk in ranges.split(","):
            start, _, end = chunk.partition("-")
            if int(start) < 1 or int(end) < int(start):
                return False
    return True


def validate_review_packet(task: dict[str, Any]) -> list[str]:
    """Return all reasons a review leaf must not be dispatched."""
    if not is_review_leaf(task):
        return ["task is not a read-only review leaf"]
    packet = parse_review_packet(task)
    errors: list[str] = []

    required = {
        "implementation_task": packet["implementation_task"],
        "target_repository": packet["target_repository"],
        "target_worktree": packet["target_worktree"],
        "branch": packet["branch"],
        "candidate_commit": packet["candidate_commit"],
        "implementer_profile": packet["implementer_profile"],
        "reviewer_profile": packet["reviewer_profile"],
        "review_lens": packet["review_lens"],
        "stop_condition": packet["stop_condition"],
    }
    for name, value in required.items():
        if not value:
            errors.append(f"missing {name}")

    worktree = packet["target_worktree"]
    if worktree and not Path(worktree).is_absolute():
        errors.append("target_worktree must be absolute")
    row_worktree = _clean(task.get("workspace_path"))
    if row_worktree and worktree and row_worktree != worktree:
        errors.append("task workspace_path does not match target_worktree")
    assignee = _clean(task.get("assignee"))
    if packet["reviewer_profile"] and assignee != packet["reviewer_profile"]:
        errors.append("reviewer_profile does not match the task assignee")
    if packet["vendor_family_independent"] is not True:
        errors.append("vendor-family independence is not explicitly true")
    if packet["read_only_source"] is not True:
        errors.append("read_only_source must be true")

    files = packet["files"]
    if not files:
        errors.append("exact_scope must contain file paths")
    elif len(files) > MAX_FILES_PER_LEAF:
        errors.append(f"exact_scope must contain at most {MAX_FILES_PER_LEAF} files")
    bad_paths = [path for path in files if not _exact_file_path(path)]
    if bad_paths:
        errors.append(f"exact_scope contains non-file paths: {', '.join(bad_paths)}")

    questions = packet["questions"]
    if len(questions) != 1:
        errors.append(f"exactly one acceptance question is required (found {len(questions)})")
    if not packet["review_lens"]:
        errors.append("one review lens is required")
    if not packet["checks"]:
        errors.append("focused_checks must contain at least one bounded check")

    if packet["max_runtime_seconds"] != REVIEW_DISPATCH_HARD_CAP_SECONDS:
        errors.append(
            f"packet max_runtime_seconds must be {REVIEW_DISPATCH_HARD_CAP_SECONDS}"
        )
    if _int_value(task.get("max_runtime_seconds")) != REVIEW_DISPATCH_HARD_CAP_SECONDS:
        errors.append(
            f"durable task max_runtime_seconds must be {REVIEW_DISPATCH_HARD_CAP_SECONDS}"
        )
    if packet["max_retries"] != 1:
        errors.append("packet max_retries must be 1")
    if _int_value(task.get("max_retries")) != 1:
        errors.append("durable task max_retries must be 1")
    if set(packet["allowed_verdicts"]) != VALID_VERDICTS:
        errors.append("allowed_verdicts must be APPROVED, CHANGES_REQUESTED, REVIEW-INCOMPLETE")

    for pattern in _PROHIBITED_REVIEW_INSTRUCTIONS:
        if pattern.search(packet["body"]):
            errors.append("packet contains implementation instructions")
            break
    return list(dict.fromkeys(errors))


def dispatchable_review_packets(
    rows: Iterable[dict[str, Any]],
    durable_loader: Optional[Callable[[str], dict[str, Any]]] = None,
) -> list[tuple[dict[str, Any], list[str]]]:
    """Return invalid review leaves that could otherwise be dispatched."""
    result: list[tuple[dict[str, Any], list[str]]] = []
    for row in rows:
        if str(row.get("status") or "").lower() not in DISPATCHABLE_STATUSES:
            continue
        if not is_review_leaf(row):
            continue
        candidate = dict(row)
        task_id = _clean(row.get("id"))
        if durable_loader and task_id:
            try:
                candidate.update(durable_loader(task_id))
            except Exception as exc:
                result.append((row, [f"durable task fields unavailable: {exc}"]))
                continue
        errors = validate_review_packet(candidate)
        if errors:
            result.append((row, errors))
    return result


# ---------------------------------------------------------------------------
# Terminal run/verdict handling


def _latest_run(detail: dict[str, Any]) -> Optional[dict[str, Any]]:
    runs = [run for run in detail.get("runs") or [] if isinstance(run, dict)]
    if not runs:
        return None
    # task_runs.id is an always-present INTEGER PRIMARY KEY AUTOINCREMENT. It
    # remains monotonic while a run is active or has just been killed, when
    # ended_at is NULL. Fall back to API order only for synthetic/non-numeric
    # fixtures.
    identified = [run for run in runs if _int_value(run.get("id")) is not None]
    if identified:
        return max(identified, key=lambda run: _int_value(run.get("id")) or 0)
    return runs[-1]


def _run_state(run: Optional[dict[str, Any]]) -> str:
    if not run:
        return ""
    values = {
        str(run.get("status") or "").strip().lower(),
        str(run.get("outcome") or "").strip().lower(),
    }
    # Timeout state is authoritative even if another field reports a generic
    # failure or a partial completion.
    for state in ("timed_out", "gave_up"):
        if state in values:
            return state
    for state in ("crashed", "failed", "spawn_failed", "cancelled", "reclaimed", "blocked"):
        if state in values:
            return state
    if "changes_requested" in values:
        return "changes_requested"
    for state in ("done", "completed", "review_requested", "running", "ready"):
        if state in values:
            return state
    return next((value for value in values if value), "")


def latest_timeout(detail: dict[str, Any]) -> Optional[dict[str, Any]]:
    latest = _latest_run(detail)
    return latest if _run_state(latest) in TIMEOUT_OUTCOMES else None


def _metadata(run: dict[str, Any]) -> dict[str, Any]:
    value = run.get("metadata")
    if isinstance(value, dict):
        return value
    if value:
        try:
            parsed = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalise_verdict(value: Any) -> Optional[str]:
    text = re.sub(r"[\s-]+", "_", str(value or "").strip().upper())
    if text == "REVIEW_INCOMPLETE":
        return "REVIEW-INCOMPLETE"
    if text in {"APPROVED", "CHANGES_REQUESTED"}:
        return text
    return None


def _text_verdict(text: str) -> Optional[str]:
    if re.search(r"\bCHANGES[- _]REQUESTED\b", text, re.IGNORECASE):
        return "CHANGES_REQUESTED"
    if re.search(r"\bREVIEW[- _]INCOMPLETE\b", text, re.IGNORECASE):
        return "REVIEW-INCOMPLETE"
    if re.search(
        r"\b(?:no|not|never|without|un)\s+approval|\bnot\s+approved\b|\bunapproved\b",
        text,
        re.IGNORECASE,
    ):
        return None
    if re.search(r"\bAPPROVED\b", text, re.IGNORECASE):
        return "APPROVED"
    return None


def review_verdict(detail: dict[str, Any]) -> str:
    """Return one verdict, with the latest terminal run state authoritative.

    In particular, a timed-out/crashed latest run is incomplete even when its
    partial summary or an old reviewer comment contains APPROVED or
    CHANGES_REQUESTED.
    """
    latest = _latest_run(detail)
    recognised_states = (
        TIMEOUT_OUTCOMES
        | {"crashed", "failed", "spawn_failed", "cancelled", "reclaimed", "blocked"}
        | {"changes_requested", "done", "completed", "review_requested", "running", "ready"}
    )
    raw_states = {
        str(latest.get(field) or "").strip().lower()
        for field in ("status", "outcome")
    } if latest else set()
    if any(value and value not in recognised_states for value in raw_states):
        return "REVIEW-INCOMPLETE"
    state = _run_state(latest)
    if state in INCOMPLETE_OUTCOMES:
        return "REVIEW-INCOMPLETE"

    if state in {"running", "ready", "review_requested"}:
        return ""

    # A latest run with an unrecognised non-empty state is not evidence that an
    # older comment is still authoritative.  Keep the review fail-closed.
    if latest and state and state not in recognised_states:
        return "REVIEW-INCOMPLETE"

    if latest and state in {"done", "completed", "changes_requested"}:
        metadata = _metadata(latest)
        for key in ("review_outcome", "overall_verdict", "verdict"):
            verdict = _normalise_verdict(metadata.get(key))
            if verdict:
                return verdict
        verdict = _text_verdict(str(latest.get("summary") or ""))
        if verdict:
            return verdict

    # Comments are fallback evidence only when the current run did not produce
    # a terminal failure or explicit verdict.
    for comment in reversed(detail.get("comments") or []):
        if not isinstance(comment, dict):
            continue
        verdict = _text_verdict(str(comment.get("body") or ""))
        if verdict:
            return verdict
    return ""


# ---------------------------------------------------------------------------
# Successor decomposition and recursive fan-in frontier


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result or "scope"


def _question_variants(packet: dict[str, Any]) -> list[str]:
    """Return authored questions without inventing acceptance criteria."""
    return [str(question).strip() for question in packet.get("questions") or [] if str(question).strip()]


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _checks_for(packet: dict[str, Any]) -> list[str]:
    """Carry every authored bounded check into the successor packet."""
    return list(dict.fromkeys(
        str(check).strip() for check in packet.get("checks") or [] if str(check).strip()
    ))


def successor_specs(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """Split a broad/oversized packet into one-question, small-file leaves.

    An already atomic one-question leaf deliberately returns no successor: a
    timeout there is preserved as REVIEW-INCOMPLETE rather than converted into
    an identical retry.  Authored questions are retained verbatim; this helper
    never invents acceptance criteria.  Decompositions above
    MAX_SUCCESSOR_SPECS are rejected as incomplete instead of being partially
    created.  There is no arbitrary successor-depth ceiling.
    """
    files = [str(path).strip().strip("`") for path in packet.get("files") or [] if str(path).strip()]
    questions = _question_variants(packet)
    if not files or not questions:
        return []
    file_groups = _chunks(files, MAX_FILES_PER_SUCCESSOR)
    original_question = str((packet.get("questions") or [""])[0]).strip()
    specs: list[dict[str, Any]] = []
    for question_index, question in enumerate(questions, 1):
        for file_index, group in enumerate(file_groups, 1):
            if len(questions) == 1 and len(file_groups) == 1 and question == original_question:
                continue
            key = f"q-{question_index:02d}-files-{file_index:02d}-{_slug(question)[:64]}"
            specs.append(
                {
                    "key": key,
                    "files": group,
                    "question": question,
                    "checks": _checks_for(packet),
                    "review_lens": packet.get("review_lens") or "bounded acceptance review",
                }
            )
    if len(specs) > MAX_SUCCESSOR_SPECS:
        return []
    return specs


def is_strict_successor(packet: dict[str, Any], spec: dict[str, Any]) -> bool:
    original_files = set(packet.get("files") or [])
    successor_files = set(spec.get("files") or [])
    original_questions = [str(value).strip() for value in packet.get("questions") or []]
    successor_question = str(spec.get("question") or "").strip()
    if successor_question not in original_questions:
        return False
    return successor_files < original_files or len(original_questions) > 1


def successor_key(original_task_id: str, run_id: Any, spec: dict[str, Any]) -> str:
    return f"review-successor:{original_task_id}:{run_id or 'packet'}:{spec['key']}"


def successor_body(
    packet: dict[str, Any],
    failure: dict[str, Any],
    spec: dict[str, Any],
    *,
    original_task_id: str,
) -> str:
    failure_state = str(failure.get("outcome") or failure.get("status") or "incomplete")
    scope = "\n".join(f"- `{path}`" for path in spec["files"])
    checks = "\n".join(f"- `{check}`" for check in spec.get("checks") or [])
    lines = [
        "review_type: read-only adversarial code/config review leaf",
        f"continuation_of: {original_task_id}",
        f"failure_run: {failure.get('id') or 'unknown'}",
        f"failure_outcome: {failure_state}",
        f"continuation_depth: {int(packet.get('continuation_depth') or 0) + 1}",
        f"implementation_task: {packet['implementation_task']}",
        f"source_issue: {packet.get('source_issue') or 'not declared'}",
        f"target_repository: {packet['target_repository']}",
        f"target_worktree: {packet['target_worktree']}",
        f"branch: {packet.get('branch') or 'not declared'}",
        f"candidate_commit: {packet['candidate_commit']}",
        f"implementer_profile: {packet['implementer_profile']}",
        f"reviewer_profile: {packet['reviewer_profile']}",
        "vendor_family_independent: true",
        "read_only_source: true",
        "review_kind: pre_commit",
        "review_scope: change_set",
        f"max_runtime_seconds: {REVIEW_DISPATCH_HARD_CAP_SECONDS}",
        f"dispatch_hard_cap_seconds: {REVIEW_DISPATCH_HARD_CAP_SECONDS}",
        f"evidence_budget_seconds: {REVIEW_EVIDENCE_BUDGET_SECONDS}",
        f"command_timeout_seconds: {REVIEW_COMMAND_TIMEOUT_SECONDS}",
        "max_retries: 1",
        "allowed_verdicts: APPROVED, CHANGES_REQUESTED, REVIEW-INCOMPLETE",
        "review_lens: " + str(spec.get("review_lens") or packet.get("review_lens") or "bounded acceptance review"),
        "",
        "This is a strict-subset continuation, not an identical retry.",
        "The prior run is incomplete evidence, never a finding or approval.",
        "Review the changed hunks in the exact scope below, not the whole files and not the repository.",
        "Inspect only the exact scope below. Do not edit source, tracker state, Git history, or live systems.",
        "Do not create child tasks or perform deployment/release actions.",
        (
            "Run diff-targeted checks only; each command must stay within "
            f"{REVIEW_COMMAND_TIMEOUT_SECONDS}s. Do not run the full project gate "
            "(make test, make validate, a full suite or build): that evidence is the "
            "implementer's/CI's and is cited here, never re-run."
        ),
        (
            f"Return a verdict at or before {REVIEW_EVIDENCE_BUDGET_SECONDS}s with the "
            "evidence you hold, listing anything unchecked under gaps. At 50% of that "
            "budget stop opening new context; at 70% stop starting new commands. Being "
            "killed at the dispatch cap is a factory fault, not a stop condition."
        ),
        "Return one verdict with exact evidence, gaps, mutations, and next_action.",
        "",
        "exact_scope:",
        scope,
        "",
        f"one_acceptance_question: {spec['question']}",
        "",
        "focused_checks:",
        checks,
        "",
        "stop_condition: stop after the named files, one acceptance question, focused checks, and source mutation readback; do not expand scope.",
    ]
    return "\n".join(lines)


def create_command(
    *,
    board: str,
    packet: dict[str, Any],
    spec: dict[str, Any],
    body: str,
    key: str,
) -> list[str]:
    source_number_match = re.search(r"(?:issues|pulls)/(\d+)|\b#(\d+)\b", str(packet.get("source_issue") or ""))
    source_label = f"#{source_number_match.group(1) or source_number_match.group(2)}" if source_number_match else "review"
    command = [
        "kanban",
        "--board",
        board,
        "create",
        f"Review continuation {source_label}: {spec['key']}",
        "--body",
        body,
        "--assignee",
        str(packet["reviewer_profile"]),
        "--parent",
        str(packet["implementation_task"]),
        "--workspace",
        f"dir:{packet['target_worktree']}",
        "--priority",
        str(packet.get("priority") or 90),
        "--max-runtime",
        str(REVIEW_DISPATCH_HARD_CAP_SECONDS),
        "--max-retries",
        "1",
        "--created-by",
        "review-successor-recovery",
        "--idempotency-key",
        key,
    ]
    for skill in DEFAULT_REVIEWER_SKILLS:
        command.extend(["--skill", skill])
    command.append("--json")
    return command


def _successor_rows(
    rows: Iterable[dict[str, Any]],
    original_task_id: str,
    run_id: Any = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        body = str(row.get("body") or "")
        if _field(body, "continuation_of") != original_task_id:
            continue
        if run_id is not None and _field(body, "failure_run") != str(run_id):
            continue
        if is_review_leaf(row):
            result.append(row)
    return sorted(result, key=lambda row: (int(row.get("created_at") or 0), str(row.get("id") or "")))


def successor_frontier(
    task_id: str,
    rows: Iterable[dict[str, Any]],
    failure_runs: Optional[dict[str, Any]] = None,
    _seen: Optional[set[str]] = None,
    failure_run_loader: Optional[Callable[[str], Any]] = None,
) -> list[str]:
    """Return the active/current frontier after recursively following successors."""
    rows_list = list(rows)
    by_id = {str(row.get("id")): row for row in rows_list if row.get("id")}
    known_failure_runs = failure_runs if failure_runs is not None else {}
    seen = set(_seen or set())
    if task_id in seen:
        return []
    seen.add(task_id)
    run_known = task_id in known_failure_runs
    run_id = known_failure_runs.get(task_id)
    if run_id is not None and not str(run_id).strip():
        run_id = None
    successors = _successor_rows(rows_list, task_id, run_id)
    if run_id is None and not run_known and failure_run_loader is not None and successors:
        # A terminal frontier leaf needs no run lookup.  Only read back a
        # descendant's latest timeout when there are children to disambiguate.
        run_id = failure_run_loader(task_id)
        if run_id is not None and not str(run_id).strip():
            run_id = None
        known_failure_runs[task_id] = run_id
        successors = _successor_rows(rows_list, task_id, run_id) if run_id is not None else []
    if successors:
        # A successor is valid only for the exact failure run that generated
        # its parent.  Never let a missing descendant readback turn into an
        # unrestricted match that can admit a stale/superseded branch.
        if run_id is None:
            return []
        frontier: list[str] = []
        for successor in successors:
            successor_id = str(successor.get("id") or "")
            if successor_id:
                frontier.extend(
                    successor_frontier(
                        successor_id,
                        rows_list,
                        known_failure_runs,
                        _seen=seen,
                        failure_run_loader=failure_run_loader,
                    )
                )
        return list(dict.fromkeys(frontier))

    row = by_id.get(task_id)
    status = str((row or {}).get("status") or "").lower()
    if status in FRONTIER_STATUSES:
        return [task_id]
    # Archived or blocked nodes with no live successor are historical/incomplete
    # evidence and must not remain a replacement fan-in parent.
    return []


def _clean_id_list(value: str) -> list[str]:
    return [item.strip().strip("`") for item in value.split(",") if item.strip()]


def replacement_fanin_parents(
    fanin: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    failure_runs: Optional[dict[str, Any]] = None,
    failure_run_loader: Optional[Callable[[str], Any]] = None,
) -> Optional[list[str]]:
    """Replace blocked ancestors with the recursively resolved successor frontier."""
    leaf_ids = _clean_id_list(_field(str(fanin.get("body") or ""), "leaf_tasks"))
    if not leaf_ids:
        return None
    parents: list[str] = []
    changed = False
    for leaf_id in leaf_ids:
        frontier = successor_frontier(
            leaf_id,
            rows,
            failure_runs,
            failure_run_loader=failure_run_loader,
        )
        if not frontier:
            return None
        if set(frontier) != {leaf_id}:
            changed = True
        parents.extend(frontier)
    if not changed:
        return None
    return list(dict.fromkeys(parents))


def fanin_key(fanin_id: str, parent_ids: Iterable[str]) -> str:
    return f"review-fan-in-successor:{fanin_id}:{','.join(parent_ids)}"


def fanin_body(fanin: dict[str, Any], parent_ids: list[str], *, key: str) -> str:
    original_body = str(fanin.get("body") or "")
    return "\n".join(
        [
            "review_type: bounded fan-in",
            f"replaces_fan_in: {fanin.get('id')}",
            f"review_successor_idempotency_key: {key}",
            f"implementation_task: {_first_field(original_body, 'implementation_task', 'implementation task')}",
            f"source_issue: {_first_field(original_body, 'source_issue', 'source issue')}",
            f"leaf_tasks: {', '.join(parent_ids)}",
            f"target_worktree: {_clean(fanin.get('workspace_path'))}",
            "read_only_source: true",
            f"max_runtime_seconds: {FANIN_DISPATCH_HARD_CAP_SECONDS}",
            f"dispatch_hard_cap_seconds: {FANIN_DISPATCH_HARD_CAP_SECONDS}",
            f"evidence_budget_seconds: {FANIN_EVIDENCE_BUDGET_SECONDS}",
            "max_retries: 1",
            "allowed_verdicts: APPROVED, CHANGES_REQUESTED, REVIEW-INCOMPLETE",
            "",
            "Read only the completed parent leaf handoffs and attached evidence.",
            "Do not rescan the repository, edit source/tracker/Git, create tasks, or deploy.",
            "Do not re-run leaf checks or the project gate; reconcile the reported evidence only.",
            "Reconcile every replacement leaf against the original acceptance criteria.",
            "Verify that every changed hunk of the parent change set is covered by exactly one leaf manifest.",
            "Any missing, timed-out, changes-requested, mutated, or uncovered leaf prevents approval.",
            "A replacement fan-in is not approval; it only restores the dependency graph.",
            "stop_condition: fan-in only; do not repeat leaf review or repository-wide discovery.",
        ]
    )


def fanin_command(board: str, fanin: dict[str, Any], parent_ids: list[str], body: str, key: str) -> list[str]:
    command = [
        "kanban",
        "--board",
        board,
        "create",
        f"Review synthesis continuation: {fanin.get('title') or fanin.get('id')}",
        "--body",
        body,
        "--assignee",
        str(fanin.get("assignee") or "default"),
        "--workspace",
        f"dir:{_clean(fanin.get('workspace_path'))}",
        "--priority",
        str(fanin.get("priority") or 90),
        "--max-runtime",
        str(FANIN_DISPATCH_HARD_CAP_SECONDS),
        "--max-retries",
        "1",
        "--created-by",
        "review-successor-recovery",
        "--idempotency-key",
        key,
    ]
    for parent_id in parent_ids:
        command.extend(["--parent", parent_id])
    command.append("--json")
    return command


# ---------------------------------------------------------------------------
# Guard/recovery mutations with readback


def _preflight_profile(profile: str) -> None:
    code, stdout, stderr = _run(
        ["hermes", "-p", profile, "skills", "list", "--enabled-only"], timeout=90
    )
    if code != 0:
        raise RuntimeError(f"reviewer profile {profile!r} preflight failed: {stderr or stdout}")


def _comment_once(board: str, task_id: str, marker: str, message: str) -> None:
    detail = _show(board, task_id)
    comments = detail.get("comments") or []
    if any(marker in str(comment.get("body") or "") for comment in comments if isinstance(comment, dict)):
        return
    code, stdout, stderr = _hermes("kanban", "--board", board, "comment", task_id, message)
    if code != 0:
        raise RuntimeError(f"comment {task_id} failed: {stderr or stdout}")
    verify = _show(board, task_id)
    if not any(
        message in str(comment.get("body") or "")
        for comment in verify.get("comments") or []
        if isinstance(comment, dict)
    ):
        raise RuntimeError(f"comment {task_id} readback failed")


def _block_invalid(board: str, row: dict[str, Any], errors: list[str]) -> str:
    task_id = str(row.get("id") or "")
    marker = "[review-packet-guard]"
    reason = f"{marker} blocked before dispatch: " + "; ".join(errors)
    last_error = ""

    # The list/read and quarantine write race with review dispatch. `block`
    # accepts ready/running tasks, but review/todo/scheduled packets must be
    # archived to leave the dispatch frontier. Re-read before every bounded
    # attempt and accept only a verified blocked/archived terminal state.
    for _attempt in range(3):
        detail = _show(board, task_id)
        task = _task_from_detail(detail, row)
        status = str(task.get("status") or "").lower()
        comments = detail.get("comments") or []

        if status in {"blocked", "archived"}:
            if not any(
                marker in str(comment.get("body") or "")
                for comment in comments
                if isinstance(comment, dict)
            ):
                _comment_once(board, task_id, marker, reason)
            return f"quarantined invalid review packet {task_id} as {status}"

        if status in {"review", "todo", "scheduled"}:
            _comment_once(board, task_id, marker, reason)
            action = "archive"
            expected = "archived"
            command = ("kanban", "--board", board, "archive", task_id)
        elif status in {"ready", "running"}:
            action = "block"
            expected = "blocked"
            command = ("kanban", "--board", board, "block", task_id, reason)
        else:
            raise RuntimeError(
                f"invalid review {task_id} reached non-quarantinable status {status!r}"
            )

        code, stdout, stderr = _hermes(*command)
        last_error = stderr or stdout
        verify = _show(board, task_id)
        verified_task = _task_from_detail(verify, row)
        verified_status = str(verified_task.get("status") or "").lower()
        if verified_status == expected:
            if not any(
                marker in str(comment.get("body") or "")
                for comment in verify.get("comments") or []
                if isinstance(comment, dict)
            ):
                _comment_once(board, task_id, marker, reason)
            return f"quarantined invalid review packet {task_id} as {expected}"
        if verified_status in {"blocked", "archived"}:
            if not any(
                marker in str(comment.get("body") or "")
                for comment in verify.get("comments") or []
                if isinstance(comment, dict)
            ):
                _comment_once(board, task_id, marker, reason)
            return f"quarantined invalid review packet {task_id} as {verified_status}"
        if code == 0 or verified_status != status:
            continue
        raise RuntimeError(
            f"{action} invalid review {task_id} failed: {last_error}"
        )

    raise RuntimeError(
        f"invalid review {task_id} quarantine did not converge: {last_error}"
    )


def guard(board: str, rows: list[dict[str, Any]], *, apply: bool) -> list[str]:
    changes: list[str] = []
    for row, errors in dispatchable_review_packets(
        rows,
        durable_loader=lambda task_id: _durable_task_fields(board, task_id),
    ):
        task_id = str(row.get("id") or "unknown")
        if not apply:
            changes.append(f"would block invalid review packet {task_id}: {'; '.join(errors)}")
        else:
            changes.append(_block_invalid(board, row, errors))
    return changes


def _archive_created_task(board: str, task_id: str) -> None:
    """Remove a failed create from the dispatchable frontier and verify it."""
    try:
        status = _task_from_detail(_show(board, task_id), {}).get("status")
    except Exception:
        status = None
    if status != "archived":
        code, stdout, stderr = _hermes("kanban", "--board", board, "archive", task_id)
        if code != 0:
            raise RuntimeError(f"archive failed created task {task_id}: {stderr or stdout}")
    final_status = _task_from_detail(_show(board, task_id), {}).get("status")
    if final_status != "archived":
        raise RuntimeError(f"failed created task {task_id} is not archived: {final_status!r}")


def _create_successor(
    board: str,
    packet: dict[str, Any],
    failure: dict[str, Any],
    spec: dict[str, Any],
    key: str,
) -> str:
    body = successor_body(packet, failure, spec, original_task_id=packet["task_id"])
    body += f"\nreview_successor_idempotency_key: {key}\n"
    task_id: Optional[str] = None
    try:
        result = _json_command(*create_command(board=board, packet=packet, spec=spec, body=body, key=key))
        task_id = _task_id(result) or None
        if not task_id:
            raise RuntimeError(f"review successor create returned no task id for {key}")
        detail = _show(board, task_id)
        task = _task_from_detail(detail, {})
        durable_task = _enrich_task_with_durable_fields(board, task)
        if task.get("assignee") != packet["reviewer_profile"]:
            raise RuntimeError(f"successor {task_id} assignee readback failed")
        if _int_value(durable_task.get("max_runtime_seconds")) != REVIEW_DISPATCH_HARD_CAP_SECONDS or _int_value(durable_task.get("max_retries")) != 1:
            raise RuntimeError(f"successor {task_id} durable budget readback failed")
        if str(task.get("workspace_path") or "") != packet["target_worktree"]:
            raise RuntimeError(f"successor {task_id} workspace readback failed")
        parent_ids = [str(parent) for parent in detail.get("parents") or []]
        if packet["implementation_task"] not in parent_ids:
            raise RuntimeError(f"successor {task_id} dependency readback failed")
        verified_packet = parse_review_packet(task)
        if verified_packet["files"] != spec["files"] or verified_packet["questions"] != [spec["question"]]:
            raise RuntimeError(f"successor {task_id} exact packet readback failed")
        if validate_review_packet(durable_task):
            raise RuntimeError(f"successor {task_id} contract readback failed")
        return task_id
    except Exception as exc:
        cleanup_ids: list[str] = []
        if task_id:
            cleanup_ids.append(task_id)
        else:
            try:
                existing = _existing_key(_list(board), key)
            except Exception:
                existing = None
            if existing and existing.get("id"):
                cleanup_ids.append(str(existing["id"]))
        for cleanup_id in dict.fromkeys(cleanup_ids):
            try:
                _archive_created_task(board, cleanup_id)
            except Exception as cleanup_exc:
                raise RuntimeError(
                    f"successor {cleanup_id} failed and cleanup failed: {cleanup_exc}"
                ) from exc
        raise


def _field_present(body: str, name: str) -> bool:
    escaped = re.escape(name).replace(r"\ ", r"[ _]")
    return re.search(rf"(?im)^\s*{escaped}\s*:", body) is not None


def _fanin_provenance_errors(
    fanin: dict[str, Any],
    task: dict[str, Any],
    parent_ids: list[str],
    key: str,
) -> list[str]:
    """Validate the replacement body against its source handoff."""
    body = str(task.get("body") or "")
    source_body = str(fanin.get("body") or "")
    expected = {
        "review_type": "bounded fan-in",
        "replaces_fan_in": _clean(fanin.get("id")),
        "review_successor_idempotency_key": key,
        "implementation_task": _first_field(source_body, "implementation_task", "implementation task"),
        "source_issue": _first_field(source_body, "source_issue", "source issue"),
        "target_worktree": _clean(fanin.get("workspace_path")),
        "read_only_source": "true",
        "max_runtime_seconds": str(FANIN_DISPATCH_HARD_CAP_SECONDS),
        "max_retries": "1",
        "allowed_verdicts": "APPROVED, CHANGES_REQUESTED, REVIEW-INCOMPLETE",
        "stop_condition": "fan-in only; do not repeat leaf review or repository-wide discovery",
    }
    errors: list[str] = []
    required_nonempty = {
        "review_type",
        "replaces_fan_in",
        "review_successor_idempotency_key",
        "implementation_task",
        "target_worktree",
        "read_only_source",
        "max_runtime_seconds",
        "max_retries",
        "allowed_verdicts",
        "stop_condition",
    }
    for field, expected_value in expected.items():
        if not _field_present(body, field):
            errors.append(f"missing {field}")
            continue
        actual_value = _field(body, field)
        if field in required_nonempty and not expected_value:
            errors.append(f"source {field} is missing")
            continue
        if field == "read_only_source":
            matches = _bool_value(actual_value) is True
        elif field in {"max_runtime_seconds", "max_retries"}:
            matches = _int_value(actual_value) == int(expected_value)
        else:
            matches = actual_value == expected_value
        if not matches:
            errors.append(f"{field} does not match the source handoff")

    actual_parent_ids = _clean_id_list(_field(body, "leaf_tasks"))
    if not _field_present(body, "leaf_tasks"):
        errors.append("missing leaf_tasks")
    elif (
        len(actual_parent_ids) != len(parent_ids)
        or len(set(actual_parent_ids)) != len(actual_parent_ids)
        or set(actual_parent_ids) != set(parent_ids)
    ):
        errors.append("leaf_tasks does not match the exact frontier parent set")
    return errors


def _create_fanin(board: str, fanin: dict[str, Any], parent_ids: list[str], key: str) -> str:
    body = fanin_body(fanin, parent_ids, key=key)
    task_id: Optional[str] = None
    try:
        result = _json_command(*fanin_command(board, fanin, parent_ids, body, key))
        task_id = _task_id(result) or None
        if not task_id:
            raise RuntimeError(f"replacement fan-in create returned no task id for {key}")
        detail = _show(board, task_id)
        task = _task_from_detail(detail, {})
        durable_task = _enrich_task_with_durable_fields(board, task)
        if str(task.get("status") or "") not in {"todo", "ready", "running"}:
            raise RuntimeError(f"replacement fan-in {task_id} status readback failed")
        actual = [str(parent) for parent in detail.get("parents") or []]
        if len(actual) != len(parent_ids) or set(actual) != set(parent_ids):
            raise RuntimeError(f"replacement fan-in {task_id} dependency readback failed")
        if (
            _int_value(durable_task.get("max_runtime_seconds")) != FANIN_DISPATCH_HARD_CAP_SECONDS
            or _int_value(durable_task.get("max_retries")) != 1
        ):
            raise RuntimeError(f"replacement fan-in {task_id} durable budget readback failed")
        expected_assignee = str(fanin.get("assignee") or "default")
        if str(task.get("assignee") or "") != expected_assignee:
            raise RuntimeError(f"replacement fan-in {task_id} assignee readback failed")
        if str(task.get("workspace_path") or "") != _clean(fanin.get("workspace_path")):
            raise RuntimeError(f"replacement fan-in {task_id} workspace readback failed")
        provenance_errors = _fanin_provenance_errors(fanin, task, parent_ids, key)
        if provenance_errors:
            raise RuntimeError(
                f"replacement fan-in {task_id} provenance readback failed: "
                + "; ".join(provenance_errors)
            )
        return task_id
    except Exception as exc:
        cleanup_ids: list[str] = []
        if task_id:
            cleanup_ids.append(task_id)
        else:
            try:
                existing = _existing_key(_list(board), key)
            except Exception:
                existing = None
            if existing and existing.get("id"):
                cleanup_ids.append(str(existing["id"]))
        cleanup_errors: list[str] = []
        for cleanup_id in dict.fromkeys(cleanup_ids):
            try:
                _archive_created_task(board, cleanup_id)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"{cleanup_id}: {cleanup_exc}")
        if cleanup_errors:
            raise RuntimeError(f"{exc}; cleanup failures: {'; '.join(cleanup_errors)}") from exc
        raise


def _settle_old_fanin(board: str, fanin_id: str, replacement_id: str) -> None:
    marker = f"[review-recovery] Superseded fan-in: {replacement_id}"
    message = (
        f"{marker} carries the current successor frontier and is the dispatchable fan-in. "
        "The historical fan-in is preserved only as evidence; no old timed-out prompt is retried."
    )
    _comment_once(board, fanin_id, marker, message)
    code, stdout, stderr = _hermes("kanban", "--board", board, "archive", fanin_id)
    if code != 0:
        raise RuntimeError(f"archive old fan-in {fanin_id} failed: {stderr or stdout}")
    status = _task_from_detail(_show(board, fanin_id), {}).get("status")
    if status != "archived":
        raise RuntimeError(f"old fan-in {fanin_id} archive readback failed: {status!r}")


def _existing_key(rows: Iterable[dict[str, Any]], key: str) -> Optional[dict[str, Any]]:
    return next(
        (
            row
            for row in rows
            if _field(str(row.get("body") or ""), "review_successor_idempotency_key") == key
        ),
        None,
    )


def _recover_review_leaves(
    board: str,
    rows: list[dict[str, Any]],
    *,
    apply: bool,
    changes: list[str],
) -> list[dict[str, Any]]:
    working_rows = rows if apply else copy.deepcopy(rows)
    profile_preflighted: set[str] = set()
    # The queue includes pre-existing blocked chains.  New successors are ready,
    # not failed, so they are intentionally not recursively retried in this pass.
    for row in list(working_rows):
        if str(row.get("status") or "") != "blocked" or not is_review_leaf(row):
            continue
        task_id = str(row.get("id") or "")
        detail = _show(board, task_id)
        failure = latest_timeout(detail)
        if failure is None:
            continue
        packet = parse_review_packet(_task_from_detail(detail, row))
        required_for_successor = (
            "implementation_task",
            "target_repository",
            "target_worktree",
            "branch",
            "candidate_commit",
            "implementer_profile",
            "reviewer_profile",
            "review_lens",
            "checks",
            "stop_condition",
        )
        missing_for_successor = [name for name in required_for_successor if not packet.get(name)]
        if missing_for_successor:
            changes.append(
                f"{task_id}: missing {', '.join(missing_for_successor)}; preserved REVIEW-INCOMPLETE"
            )
            continue
        specs = [spec for spec in successor_specs(packet) if is_strict_successor(packet, spec)]
        if not specs:
            changes.append(f"{task_id}: no safe narrower successor; preserved REVIEW-INCOMPLETE")
            continue
        if apply and not Path(packet["target_worktree"]).is_dir():
            changes.append(f"{task_id}: target worktree unavailable; preserved REVIEW-INCOMPLETE")
            continue
        if apply and packet["reviewer_profile"] not in profile_preflighted:
            _preflight_profile(packet["reviewer_profile"])
            profile_preflighted.add(packet["reviewer_profile"])

        created_ids: list[str] = []
        newly_created: list[tuple[str, str]] = []
        try:
            for spec in specs:
                key = successor_key(task_id, failure.get("id"), spec)
                existing = _existing_key(working_rows, key)
                if existing is not None:
                    if str(existing.get("status") or "") in FRONTIER_STATUSES:
                        created_ids.append(str(existing.get("id")))
                        changes.append(f"{task_id}: existing successor {existing.get('id')} satisfies {key}")
                    else:
                        changes.append(f"{task_id}: archived successor {existing.get('id')} preserves {key}; no duplicate")
                    continue
                if not apply:
                    planned_id = f"planned-{_slug(key)}"
                    planned_body = successor_body(packet, failure, spec, original_task_id=task_id)
                    planned_body += f"\nreview_successor_idempotency_key: {key}\n"
                    working_rows.append({
                        "id": planned_id,
                        "status": "ready",
                        "body": planned_body,
                        "created_at": 0,
                    })
                    created_ids.append(planned_id)
                    changes.append(f"would create review successor for {task_id}: {key}")
                    continue
                successor_id = _create_successor(board, packet, failure, spec, key)
                working_rows.append({
                    "id": successor_id,
                    "status": "ready",
                    "assignee": packet["reviewer_profile"],
                    "workspace_path": packet["target_worktree"],
                    "max_runtime_seconds": REVIEW_DISPATCH_HARD_CAP_SECONDS,
                    "max_retries": 1,
                    "body": successor_body(packet, failure, spec, original_task_id=task_id)
                    + f"\nreview_successor_idempotency_key: {key}\n",
                    "created_at": 0,
                })
                created_ids.append(successor_id)
                newly_created.append((successor_id, key))
        except Exception as exc:
            rollback_errors: list[str] = []
            for created_id, _key in reversed(newly_created):
                try:
                    _archive_created_task(board, created_id)
                except Exception as cleanup_exc:
                    rollback_errors.append(f"{created_id}: {cleanup_exc}")
            created_set = {created_id for created_id, _key in newly_created}
            working_rows[:] = [
                row for row in working_rows if str(row.get("id") or "") not in created_set
            ]
            detail = f"{task_id}: successor batch rolled back: {exc}"
            if rollback_errors:
                detail += "; cleanup failures: " + "; ".join(rollback_errors)
            changes.append(detail)
            if rollback_errors:
                raise RuntimeError(detail) from exc
            continue
        if len(created_ids) != len(specs):
            changes.append(f"{task_id}: successor set incomplete; fan-in remains gated")
        if apply and newly_created:
            successor_ids = ", ".join(created_id for created_id, _key in newly_created)
            _comment_once(
                board,
                task_id,
                f"[review-recovery] Successors for {task_id}",
                f"[review-recovery] Successors for {task_id}: {successor_ids}. "
                f"Created for timeout run {failure.get('id')}; original review remains preserved as REVIEW-INCOMPLETE.",
            )
            for successor_id, key in newly_created:
                changes.append(f"created and verified review successor {successor_id} for {task_id}: {key}")
    return working_rows


def _recover_fanins(
    board: str,
    rows: list[dict[str, Any]],
    *,
    apply: bool,
    changes: list[str],
) -> list[dict[str, Any]]:
    working_rows = rows if apply else copy.deepcopy(rows)
    for row in list(working_rows):
        if str(row.get("status") or "") != "todo" or not is_review_fanin(row):
            continue
        fanin_id = str(row.get("id") or "")
        fanin_detail = _show(board, fanin_id)
        fanin = _task_from_detail(fanin_detail, row)
        leaf_ids = _clean_id_list(_field(str(fanin.get("body") or ""), "leaf_tasks"))
        failure_runs: dict[str, Any] = {}
        for leaf_id in leaf_ids:
            leaf_detail = _show(board, leaf_id)
            failure = latest_timeout(leaf_detail)
            failure_id = failure.get("id") if failure is not None else None
            if failure_id is not None and not str(failure_id).strip():
                failure_id = None
            failure_runs[leaf_id] = failure_id

        def load_failure_run(task_id: str) -> Any:
            try:
                detail = _show(board, task_id)
            except Exception:
                return None
            failure = latest_timeout(detail)
            if failure is None:
                return None
            failure_id = failure.get("id")
            if failure_id is not None and not str(failure_id).strip():
                return None
            return failure_id

        parent_ids = replacement_fanin_parents(
            fanin,
            working_rows,
            failure_runs,
            failure_run_loader=load_failure_run,
        )
        if not parent_ids:
            continue
        key = fanin_key(fanin_id, parent_ids)
        existing = next(
            (
                candidate
                for candidate in working_rows
                if _field(str(candidate.get("body") or ""), "replaces_fan_in") == fanin_id
                and _field(str(candidate.get("body") or ""), "review_successor_idempotency_key") == key
            ),
            None,
        )
        if existing is not None:
            replacement_status = str(existing.get("status") or "").lower()
            if replacement_status not in FRONTIER_STATUSES:
                changes.append(
                    f"archived replacement fan-in {existing.get('id')} preserves {key}; no duplicate dispatch"
                )
                continue
            replacement_id = str(existing.get("id"))
            changes.append(f"existing replacement fan-in {replacement_id} satisfies {key}")
        elif not apply:
            changes.append(f"would create replacement fan-in for {fanin_id} with parents: {', '.join(parent_ids)}")
            continue
        else:
            replacement_id = _create_fanin(board, fanin, parent_ids, key)
            working_rows.append({
                "id": replacement_id,
                "status": "todo",
                "body": fanin_body(fanin, parent_ids, key=key),
                "created_at": 0,
            })
            changes.append(f"created and verified replacement fan-in {replacement_id} for {fanin_id}")
        if apply:
            _settle_old_fanin(board, fanin_id, replacement_id)
    return working_rows


def recover(board: str, *, apply: bool = False, run_guard: bool = True) -> list[str]:
    changes: list[str] = []
    changes.extend(_recover_native_review_remediation_handoffs(board, apply=apply))
    rows = _list(board)
    if run_guard:
        changes.extend(guard(board, rows, apply=apply))
        if apply:
            rows = _list(board)
    rows = _recover_review_leaves(board, rows, apply=apply, changes=changes)
    if apply:
        rows = _list(board)
    rows = _recover_fanins(board, rows, apply=apply, changes=changes)
    return changes


@contextmanager
def board_lock(board: str):
    if fcntl is None:
        yield True
        return
    lock_dir = Path.home() / ".hermes" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"review-successor-{_slug(board)}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def recover_locked(board: str, *, apply: bool = False, run_guard: bool = True) -> list[str]:
    with board_lock(board) as acquired:
        if not acquired:
            return [f"skipped overlapping review successor recovery for {board}"]
        return recover(board, apply=apply, run_guard=run_guard)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default=os.environ.get("HERMES_FACTORY_BOARD") or BOARD_DEFAULT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--guard-only", action="store_true")
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    if args.guard_only and args.recover_only:
        parser.error("--guard-only and --recover-only are mutually exclusive")
    try:
        if args.guard_only:
            rows = _list(args.board)
            with board_lock(args.board) as acquired:
                changes = ["skipped overlapping review packet guard"] if not acquired else guard(args.board, rows, apply=args.apply)
        else:
            changes = recover_locked(args.board, apply=args.apply, run_guard=not args.recover_only)
    except Exception as exc:
        print(f"review successor recovery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if changes and not args.quiet:
        print("\n".join(changes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
