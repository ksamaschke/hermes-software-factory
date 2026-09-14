"""Causal regression for native dependency-wait admission.

Staged-runtime tests compare the pinned legacy runtime with the exact Factory
candidate. They use private temporary SQLite databases only and start no
service, listener, or worker.
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
NATIVE_PATH = (
    ROOT
    / "local-variant"
    / "native-boundary"
    / "runtime"
    / "hermes_cli"
    / "native_boundary.py"
)
LEGACY_RUNTIME_DEFAULT = Path(
    "/home/ksamaschke/.hermes/profiles/orchestrator/runtime-hotfix-20260906"
)
PARENT_ERROR = "dependency block requires at least one unfinished direct parent"


def _load_native_boundary():
    spec = importlib.util.spec_from_file_location(
        "native_boundary_dependency_wait_test", NATIVE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


native_boundary = _load_native_boundary()


def _decision_connection(parent_statuses: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE task_links (
            parent_id TEXT NOT NULL,
            child_id TEXT NOT NULL
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT
        );
        INSERT INTO tasks (id, status) VALUES ('child', 'running');
        """
    )
    for index, status in enumerate(parent_statuses):
        parent_id = f"parent-{index}"
        conn.execute(
            "INSERT INTO tasks (id, status) VALUES (?, ?)", (parent_id, status)
        )
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, 'child')",
            (parent_id,),
        )
    conn.commit()
    return conn


def _record_wait(
    conn: sqlite3.Connection,
    parent_ids: object,
    *,
    schema: object = native_boundary.DEPENDENCY_WAIT_SCHEMA,
) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload) "
        "VALUES ('child', 'dependency_wait', ?)",
        (json.dumps({"schema": schema, "waiting_parent_ids": parent_ids}),),
    )
    conn.commit()


@pytest.mark.parametrize(
    "parent_statuses",
    [[], ["done"], ["archived"], ["done", "archived"]],
)
def test_dependency_wait_rejects_absent_or_terminal_parents(parent_statuses):
    conn = _decision_connection(parent_statuses)
    try:
        with pytest.raises(ValueError, match=PARENT_ERROR):
            native_boundary.dependency_wait_parent_ids(conn, "child")
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("parent_statuses", "expected"),
    [
        (["ready"], ("parent-0",)),
        (["running"], ("parent-0",)),
        (["done", "todo"], ("parent-1",)),
        (["archived", "review"], ("parent-1",)),
    ],
)
def test_dependency_wait_snapshots_only_unfinished_parents(parent_statuses, expected):
    conn = _decision_connection(parent_statuses)
    try:
        assert native_boundary.dependency_wait_parent_ids(conn, "child") == expected
    finally:
        conn.close()


def test_dependency_wait_promotion_accepts_authenticated_terminal_snapshot():
    conn = _decision_connection(["done", "archived"])
    try:
        _record_wait(conn, ["parent-0", "parent-1"])
        assert native_boundary.dependency_wait_promotion_rejection(conn, "child") is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (None, "lacks an authenticated parent snapshot"),
        ({}, "lacks an authenticated parent snapshot"),
        (
            {"schema": "legacy", "waiting_parent_ids": ["parent-0"]},
            "lacks an authenticated parent snapshot",
        ),
        (
            {"schema": native_boundary.DEPENDENCY_WAIT_SCHEMA},
            "invalid parent snapshot",
        ),
        (
            {
                "schema": native_boundary.DEPENDENCY_WAIT_SCHEMA,
                "waiting_parent_ids": [],
            },
            "invalid parent snapshot",
        ),
        (
            {
                "schema": native_boundary.DEPENDENCY_WAIT_SCHEMA,
                "waiting_parent_ids": ["parent-0", "parent-0"],
            },
            "duplicate parent identities",
        ),
        (
            {
                "schema": native_boundary.DEPENDENCY_WAIT_SCHEMA,
                "waiting_parent_ids": ["missing-parent"],
            },
            "missing from the current graph",
        ),
    ],
)
def test_dependency_wait_promotion_rejects_ambiguous_snapshots(event, reason):
    conn = _decision_connection(["done"])
    try:
        if event is not None:
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload) "
                "VALUES ('child', 'dependency_wait', ?)",
                (json.dumps(event),),
            )
            conn.commit()
        assert reason in native_boundary.dependency_wait_promotion_rejection(
            conn, "child"
        )
    finally:
        conn.close()


def test_dependency_wait_promotion_rejects_parent_that_is_still_open():
    conn = _decision_connection(["running"])
    try:
        _record_wait(conn, ["parent-0"])
        assert native_boundary.dependency_wait_promotion_rejection(
            conn, "child"
        ) == "dependency wait parent is not terminal"
    finally:
        conn.close()


def test_dependency_wait_validation_tracks_the_current_lifecycle_phase():
    conn = _decision_connection(["done"])
    try:
        assert not native_boundary.dependency_wait_requires_validation(
            conn, "child", None
        )
        assert native_boundary.dependency_wait_requires_validation(
            conn, "child", "dependency"
        )
        _record_wait(conn, ["parent-0"])
        assert native_boundary.dependency_wait_requires_validation(
            conn, "child", None
        )
        # Promotion/claim telemetry does not erase the proof obligation.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload) "
            "VALUES ('child', 'promoted', ?)",
            (json.dumps({"status": "review"}),),
        )
        conn.commit()
        assert native_boundary.dependency_wait_requires_validation(
            conn, "child", None
        )
        # Explicit lifecycle resolution does.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload) "
            "VALUES ('child', 'unblocked', ?)",
            (json.dumps({"status": "review"}),),
        )
        conn.commit()
        assert not native_boundary.dependency_wait_requires_validation(
            conn, "child", "dependency"
        )
    finally:
        conn.close()


def _private_environment(runtime: Path, home: Path, db: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "HERMES_KANBAN_DB": str(db),
        "PATH": "/usr/bin:/bin",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(runtime),
    }


def _run_script(
    runtime: Path,
    home: Path,
    db: Path,
    script: str,
    **extra_environment: str,
) -> dict[str, object]:
    environment = _private_environment(runtime, home, db)
    environment.update(extra_environment)
    result = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(script)],
        cwd=runtime,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def _staged_and_legacy_runtimes() -> tuple[Path, Path]:
    staged_value = os.environ.get("FACTORY_NATIVE_RUNTIME")
    if not staged_value:
        pytest.skip("FACTORY_NATIVE_RUNTIME is required for staged-runtime test")
    staged = Path(staged_value).resolve(strict=True)
    legacy = Path(
        os.environ.get("FACTORY_NATIVE_LEGACY_RUNTIME", str(LEGACY_RUNTIME_DEFAULT))
    ).resolve(strict=True)
    return staged, legacy


_DIRECT_BLOCK_PROBE = """
import json
import os
from pathlib import Path
from hermes_cli import kanban_db as kb

db = Path(os.environ["HERMES_KANBAN_DB"])
kb.init_db(db)
with kb.connect_closing(db) as conn:
    parent = kb.create_task(conn, title="terminal parent", assignee="fixture")
    assert kb.complete_task(conn, parent, result="already complete")
    child = kb.create_task(
        conn, title="must not spin", assignee="fixture", parents=[parent]
    )
    run = kb.claim_task(conn, child, claimer="fixture-owner:1")
    assert run is not None
    error = None
    blocked = None
    try:
        blocked = kb.block_task(
            conn,
            child,
            reason="external prerequisite was not modeled",
            kind="dependency",
            expected_run_id=run.current_run_id,
        )
    except ValueError as exc:
        error = str(exc)
    promoted = kb.recompute_ready(conn)
    task = kb.get_task(conn, child)
    dependency_events = [
        event for event in kb.list_events(conn, child)
        if event.kind == "dependency_wait"
    ]
    latest_run = kb.latest_run(conn, child)
    print(json.dumps({
        "runtime": str(Path(kb.__file__).resolve()),
        "blocked": blocked,
        "error": error,
        "promoted": promoted,
        "status": task.status,
        "current_run_id": task.current_run_id,
        "run_status": latest_run.status,
        "dependency_events": len(dependency_events),
    }))
"""


def test_staged_runtime_stops_terminal_parent_dependency_retry_loop(tmp_path):
    staged_runtime, legacy_runtime = _staged_and_legacy_runtimes()
    legacy = _run_script(
        legacy_runtime,
        tmp_path / "legacy-home",
        tmp_path / "legacy.db",
        _DIRECT_BLOCK_PROBE,
    )
    assert Path(str(legacy["runtime"])).is_relative_to(legacy_runtime)
    assert legacy == {
        "runtime": legacy["runtime"],
        "blocked": True,
        "error": None,
        "promoted": 1,
        "status": "ready",
        "current_run_id": None,
        "run_status": "blocked",
        "dependency_events": 1,
    }

    candidate = _run_script(
        staged_runtime,
        tmp_path / "candidate-home",
        tmp_path / "candidate.db",
        _DIRECT_BLOCK_PROBE,
    )
    assert Path(str(candidate["runtime"])).is_relative_to(staged_runtime)
    assert candidate == {
        "runtime": candidate["runtime"],
        "blocked": None,
        "error": PARENT_ERROR,
        "promoted": 0,
        "status": "running",
        "current_run_id": candidate["current_run_id"],
        "run_status": "running",
        "dependency_events": 0,
    }
    assert isinstance(candidate["current_run_id"], int)


def _create_legacy_wait(
    runtime: Path,
    home: Path,
    db: Path,
    *,
    promote: bool,
) -> dict[str, object]:
    return _run_script(
        runtime,
        home,
        db,
        """
        import json
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        kb.init_db(db)
        with kb.connect_closing(db) as conn:
            parent = kb.create_task(conn, title="legacy terminal parent", assignee="fixture")
            assert kb.complete_task(conn, parent, result="already complete")
            child = kb.create_task(
                conn, title="legacy ambiguous wait", assignee="fixture", parents=[parent]
            )
            run = kb.claim_task(conn, child, claimer="legacy-owner")
            assert run is not None
            assert kb.block_task(
                conn,
                child,
                reason="legacy dependency without snapshot",
                kind="dependency",
                expected_run_id=run.current_run_id,
            )
            promoted = kb.recompute_ready(conn) if os.environ["PROMOTE"] == "1" else 0
            print(json.dumps({
                "task_id": child,
                "status": kb.get_task(conn, child).status,
                "promoted": promoted,
            }))
        """,
        PROMOTE="1" if promote else "0",
    )


def _assert_quarantine_event(payload: object, source_status: str) -> None:
    assert isinstance(payload, dict)
    assert payload["schema"] == "factory.dependency-wait-quarantine.v1"
    assert payload["kind"] == "dependency"
    assert payload["source_status"] == source_status
    assert payload["quarantine"] is True
    assert "lacks an authenticated parent snapshot" in payload["reason"]


def test_candidate_dispatch_quarantines_legacy_todo_wait_before_spawn(tmp_path):
    staged_runtime, legacy_runtime = _staged_and_legacy_runtimes()
    db = tmp_path / "legacy-todo.db"
    legacy = _create_legacy_wait(
        legacy_runtime, tmp_path / "legacy-todo-home", db, promote=False
    )
    assert legacy["status"] == "todo"

    candidate = _run_script(
        staged_runtime,
        tmp_path / "candidate-dispatch-home",
        db,
        """
        import json
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        with kb.connect_closing(db) as conn:
            result = kb.dispatch_once(
                conn, dry_run=True, max_spawn=1, reconcile_orphans=False
            )
            task = kb.get_task(conn, os.environ["TASK_ID"])
            claimed = kb.claim_task(conn, task.id, claimer="must-not-claim")
            blocked = [
                event for event in kb.list_events(conn, task.id)
                if event.kind == "blocked"
            ]
            print(json.dumps({
                "status": task.status,
                "spawned": [item[0] for item in result.spawned],
                "claimed": claimed is not None,
                "blocked_events": len(blocked),
                "payload": blocked[-1].payload,
            }))
        """,
        TASK_ID=str(legacy["task_id"]),
    )
    assert candidate["status"] == "blocked"
    assert candidate["spawned"] == []
    assert candidate["claimed"] is False
    assert candidate["blocked_events"] == 1
    _assert_quarantine_event(candidate["payload"], "todo")


def test_candidate_direct_claim_quarantines_legacy_ready_wait(tmp_path):
    staged_runtime, legacy_runtime = _staged_and_legacy_runtimes()
    db = tmp_path / "legacy-ready.db"
    legacy = _create_legacy_wait(
        legacy_runtime, tmp_path / "legacy-ready-home", db, promote=True
    )
    assert legacy["status"] == "ready"
    assert legacy["promoted"] == 1

    candidate = _run_script(
        staged_runtime,
        tmp_path / "candidate-claim-home",
        db,
        """
        import json
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        with kb.connect_closing(db) as conn:
            claimed = kb.claim_task(conn, os.environ["TASK_ID"], claimer="must-not-claim")
            task = kb.get_task(conn, os.environ["TASK_ID"])
            blocked = [
                event for event in kb.list_events(conn, task.id)
                if event.kind == "blocked"
            ]
            print(json.dumps({
                "status": task.status,
                "claimed": claimed is not None,
                "blocked_events": len(blocked),
                "payload": blocked[-1].payload,
            }))
        """,
        TASK_ID=str(legacy["task_id"]),
    )
    assert candidate["status"] == "blocked"
    assert candidate["claimed"] is False
    assert candidate["blocked_events"] == 1
    _assert_quarantine_event(candidate["payload"], "ready")


def test_staged_runtime_still_resumes_after_real_parent_completion(tmp_path):
    runtime, _legacy = _staged_and_legacy_runtimes()
    probe = _run_script(
        runtime,
        tmp_path / "positive-home",
        tmp_path / "positive.db",
        """
        import json
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        kb.init_db(db)
        with kb.connect_closing(db) as conn:
            parent = kb.create_task(conn, title="real prerequisite", assignee="fixture")
            assert kb.complete_task(conn, parent, result="initially complete")
            child = kb.create_task(
                conn, title="waits exactly once", assignee="fixture", parents=[parent]
            )
            run = kb.claim_task(conn, child, claimer="fixture-owner:1")
            assert run is not None
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
            assert kb.block_task(
                conn,
                child,
                reason="parent contract is being refreshed",
                kind="dependency",
                expected_run_id=run.current_run_id,
            )
            wait_event = [
                event for event in kb.list_events(conn, child)
                if event.kind == "dependency_wait"
            ][-1]
            waiting = kb.get_task(conn, child).status
            before = kb.recompute_ready(conn)
            still_waiting = kb.get_task(conn, child).status
            assert kb.complete_task(conn, parent, result="refresh complete")
            after = kb.recompute_ready(conn)
            resumed = kb.get_task(conn, child).status
            print(json.dumps({
                "runtime": str(Path(kb.__file__).resolve()),
                "waiting": waiting,
                "promoted_before": before,
                "still_waiting": still_waiting,
                "promoted_after": after,
                "resumed": resumed,
                "wait_payload": wait_event.payload,
            }))
        """,
    )
    assert Path(str(probe["runtime"])).is_relative_to(runtime)
    assert probe["waiting"] == "todo"
    assert probe["promoted_before"] == 0
    assert probe["still_waiting"] == "todo"
    assert probe["promoted_after"] in {0, 1}
    assert probe["resumed"] == "ready"
    wait_payload = probe["wait_payload"]
    assert isinstance(wait_payload, dict)
    assert wait_payload["schema"] == native_boundary.DEPENDENCY_WAIT_SCHEMA
    assert len(wait_payload["waiting_parent_ids"]) == 1


def test_staged_review_wait_records_snapshot_and_resumes_in_review(tmp_path):
    runtime, _legacy = _staged_and_legacy_runtimes()
    probe = _run_script(
        runtime,
        tmp_path / "review-home",
        tmp_path / "review.db",
        """
        import json
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        kb.init_db(db)
        with kb.connect_closing(db) as conn:
            parent = kb.create_task(conn, title="review prerequisite", assignee="fixture")
            assert kb.complete_task(conn, parent, result="initially complete")
            child = kb.create_task(
                conn, title="review waits", assignee="fixture", parents=[parent]
            )
            implementation = kb.claim_task(conn, child, claimer="implementer")
            assert implementation is not None
            assert kb.request_review(
                conn,
                child,
                summary="source handoff",
                reviewer="reviewer",
                expected_run_id=implementation.current_run_id,
            )
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
            assert kb.claim_review_task(conn, child, claimer="must-wait") is None
            wait_event = [
                event for event in kb.list_events(conn, child)
                if event.kind == "dependency_wait"
            ][-1]
            waiting = kb.get_task(conn, child).status
            assert kb.complete_task(conn, parent, result="refresh complete")
            kb.recompute_ready(conn)
            resumed = kb.get_task(conn, child).status
            review = kb.claim_review_task(conn, child, claimer="reviewer")
            print(json.dumps({
                "runtime": str(Path(kb.__file__).resolve()),
                "waiting": waiting,
                "resumed": resumed,
                "review_claimed": review is not None,
                "wait_payload": wait_event.payload,
            }))
        """,
    )
    assert Path(str(probe["runtime"])).is_relative_to(runtime)
    assert probe["waiting"] == "todo"
    assert probe["resumed"] == "review"
    assert probe["review_claimed"] is True
    wait_payload = probe["wait_payload"]
    assert isinstance(wait_payload, dict)
    assert wait_payload["schema"] == native_boundary.DEPENDENCY_WAIT_SCHEMA
    assert len(wait_payload["waiting_parent_ids"]) == 1


def _create_legacy_review_wait(
    runtime: Path,
    home: Path,
    db: Path,
    *,
    promote: bool,
) -> dict[str, object]:
    return _run_script(
        runtime,
        home,
        db,
        """
        import json
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        kb.init_db(db)
        with kb.connect_closing(db) as conn:
            parent = kb.create_task(conn, title="legacy review parent", assignee="fixture")
            assert kb.complete_task(conn, parent, result="initially complete")
            child = kb.create_task(
                conn, title="legacy review wait", assignee="fixture", parents=[parent]
            )
            implementation = kb.claim_task(conn, child, claimer="legacy-implementer")
            assert implementation is not None
            assert kb.request_review(
                conn,
                child,
                summary="legacy review handoff",
                reviewer="reviewer",
                expected_run_id=implementation.current_run_id,
            )
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
            assert kb.claim_review_task(conn, child, claimer="legacy-must-wait") is None
            waiting = kb.get_task(conn, child)
            assert waiting.status == "todo"
            if os.environ["PROMOTE"] == "1":
                assert kb.complete_task(conn, parent, result="legacy parent complete")
                kb.recompute_ready(conn)
            current = kb.get_task(conn, child)
            wait_event = [
                event for event in kb.list_events(conn, child)
                if event.kind == "dependency_wait"
            ][-1]
            print(json.dumps({
                "parent_id": parent,
                "task_id": child,
                "status": current.status,
                "block_kind": current.block_kind,
                "wait_payload": wait_event.payload,
            }))
        """,
        PROMOTE="1" if promote else "0",
    )


def test_candidate_recompute_quarantines_legacy_review_todo_wait(tmp_path):
    staged_runtime, legacy_runtime = _staged_and_legacy_runtimes()
    db = tmp_path / "legacy-review-todo.db"
    legacy = _create_legacy_review_wait(
        legacy_runtime, tmp_path / "legacy-review-todo-home", db, promote=False
    )
    assert legacy["status"] == "todo"
    assert legacy["block_kind"] is None

    candidate = _run_script(
        staged_runtime,
        tmp_path / "candidate-review-todo-home",
        db,
        """
        import json
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        with kb.connect_closing(db) as conn:
            assert kb.complete_task(conn, os.environ["PARENT_ID"], result="now complete")
            promoted = kb.recompute_ready(conn)
            task = kb.get_task(conn, os.environ["TASK_ID"])
            review = kb.claim_review_task(conn, task.id, claimer="must-not-claim")
            blocked = [
                event for event in kb.list_events(conn, task.id)
                if event.kind == "blocked"
            ]
            print(json.dumps({
                "promoted": promoted,
                "status": task.status,
                "block_kind": task.block_kind,
                "claimed": review is not None,
                "blocked_events": len(blocked),
                "payload": blocked[-1].payload,
            }))
        """,
        PARENT_ID=str(legacy["parent_id"]),
        TASK_ID=str(legacy["task_id"]),
    )
    assert candidate["promoted"] == 0
    assert candidate["status"] == "blocked"
    assert candidate["block_kind"] == "dependency"
    assert candidate["claimed"] is False
    assert candidate["blocked_events"] == 1
    _assert_quarantine_event(candidate["payload"], "todo")


def test_candidate_claim_quarantines_legacy_promoted_review_wait(tmp_path):
    staged_runtime, legacy_runtime = _staged_and_legacy_runtimes()
    db = tmp_path / "legacy-review-promoted.db"
    legacy = _create_legacy_review_wait(
        legacy_runtime, tmp_path / "legacy-review-promoted-home", db, promote=True
    )
    assert legacy["status"] == "review"
    assert legacy["block_kind"] is None

    candidate = _run_script(
        staged_runtime,
        tmp_path / "candidate-review-claim-home",
        db,
        """
        import json
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        with kb.connect_closing(db) as conn:
            review = kb.claim_review_task(conn, os.environ["TASK_ID"], claimer="must-not-claim")
            task = kb.get_task(conn, os.environ["TASK_ID"])
            blocked = [
                event for event in kb.list_events(conn, task.id)
                if event.kind == "blocked"
            ]
            print(json.dumps({
                "status": task.status,
                "block_kind": task.block_kind,
                "claimed": review is not None,
                "blocked_events": len(blocked),
                "payload": blocked[-1].payload,
            }))
        """,
        TASK_ID=str(legacy["task_id"]),
    )
    assert candidate["status"] == "blocked"
    assert candidate["block_kind"] == "dependency"
    assert candidate["claimed"] is False
    assert candidate["blocked_events"] == 1
    _assert_quarantine_event(candidate["payload"], "review")


@pytest.mark.parametrize("replace_parent", [False, True])
def test_candidate_quarantines_review_wait_after_parent_graph_change(
    tmp_path, replace_parent
):
    runtime, _legacy = _staged_and_legacy_runtimes()
    probe = _run_script(
        runtime,
        tmp_path / "review-unlink-home",
        tmp_path / "review-unlink.db",
        """
        import json
        import os
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        db = Path(os.environ["HERMES_KANBAN_DB"])
        kb.init_db(db)
        with kb.connect_closing(db) as conn:
            parent = kb.create_task(conn, title="unlink review parent", assignee="fixture")
            assert kb.complete_task(conn, parent, result="initially complete")
            child = kb.create_task(
                conn, title="unlink review wait", assignee="fixture", parents=[parent]
            )
            implementation = kb.claim_task(conn, child, claimer="implementer")
            assert implementation is not None
            assert kb.request_review(
                conn,
                child,
                summary="review handoff",
                reviewer="reviewer",
                expected_run_id=implementation.current_run_id,
            )
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
            assert kb.claim_review_task(conn, child, claimer="must-wait") is None
            waiting = kb.get_task(conn, child)
            assert waiting.status == "todo"
            assert waiting.block_kind == "dependency"
            assert kb.complete_task(conn, parent, result="parent complete")
            kb.recompute_ready(conn)
            assert kb.get_task(conn, child).status == "review"
            assert kb.unlink_tasks(conn, parent, child)
            if os.environ["REPLACE_PARENT"] == "1":
                replacement = kb.create_task(
                    conn, title="replacement parent", assignee="fixture"
                )
                assert kb.complete_task(
                    conn, replacement, result="replacement complete"
                )
                kb.link_tasks(conn, replacement, child)
            review = kb.claim_review_task(conn, child, claimer="must-not-claim")
            task = kb.get_task(conn, child)
            blocked = [
                event for event in kb.list_events(conn, child)
                if event.kind == "blocked"
            ]
            print(json.dumps({
                "runtime": str(Path(kb.__file__).resolve()),
                "status": task.status,
                "block_kind": task.block_kind,
                "claimed": review is not None,
                "blocked_events": len(blocked),
                "payload": blocked[-1].payload,
            }))
        """,
        REPLACE_PARENT="1" if replace_parent else "0",
    )
    assert Path(str(probe["runtime"])).is_relative_to(runtime)
    assert probe["status"] == "blocked"
    assert probe["block_kind"] == "dependency"
    assert probe["claimed"] is False
    assert probe["blocked_events"] == 1
    payload = probe["payload"]
    assert isinstance(payload, dict)
    assert "missing from the current graph" in payload["reason"]
