"""Causal private-board regressions for review lifecycle routing.

These tests exercise the staged native Kanban runtime. They never touch the live
board or a service. Set FACTORY_NATIVE_RUNTIME to a freshly staged artifact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _runtime() -> Path:
    value = os.environ.get("FACTORY_NATIVE_RUNTIME", "").strip()
    if not value:
        pytest.skip("FACTORY_NATIVE_RUNTIME is required for native lifecycle tests")
    runtime = Path(value).resolve(strict=True)
    if not (runtime / "hermes_cli" / "kanban_db.py").is_file():
        pytest.fail(f"staged native runtime is incomplete: {runtime}")
    return runtime


def _git_fixture(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "review-target"
    repo.mkdir()
    commands = (
        ["/usr/bin/git", "init", "-q"],
        ["/usr/bin/git", "config", "user.name", "Factory Test"],
        ["/usr/bin/git", "config", "user.email", "factory@example.invalid"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=repo, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr or result.stdout
    (repo / "fixture.txt").write_text("review fixture\n", encoding="utf-8")
    for command in (["/usr/bin/git", "add", "fixture.txt"], ["/usr/bin/git", "commit", "-qm", "fixture"]):
        result = subprocess.run(command, cwd=repo, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr or result.stdout
    head = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert len(head) == 40
    return repo, head


def _run_native(tmp_path: Path, script: str) -> dict:
    runtime = _runtime()
    repo, head = _git_fixture(tmp_path)
    db = tmp_path / "private-kanban.db"
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes-home"
    home.mkdir()
    hermes_home.mkdir()
    environment = {
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "HERMES_KANBAN_DB": str(db),
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(runtime),
        "PATH": "/usr/bin:/bin",
        "TARGET_REPO": str(repo),
        "TARGET_HEAD": head,
        "RECOVERY_SCRIPT": str(ROOT / "scripts" / "kanban_review_successor_recovery.py"),
    }
    result = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(script)],
        cwd=runtime,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        numbered = "\n".join(
            f"{line_number:04d}|{line}"
            for line_number, line in enumerate(textwrap.dedent(script).splitlines(), 1)
        )
        pytest.fail((result.stderr or result.stdout) + "\nNative probe:\n" + numbered)
    return json.loads(result.stdout)


_COMMON = r'''
import json
import os
from pathlib import Path
from hermes_cli import kanban_db as kb

db = Path(os.environ["HERMES_KANBAN_DB"])
repo = os.environ["TARGET_REPO"]
head = os.environ["TARGET_HEAD"]
kb.init_db(db)


def review_body():
    return "\n".join([
        "review_type: read-only adversarial code review leaf",
        "implementation_task: t_placeholder",
        "target_repository: fixture/repository",
        f"target_worktree: {repo}",
        "branch: master",
        f"candidate_commit: {head}",
        "implementer_profile: implementer",
        "reviewer_profile: reviewer",
        "read_only_source: true",
        "review_kind: pre_commit",
        "review_scope: change_set",
    ])


def make_graph(conn):
    implementation = kb.create_task(
        conn,
        title="Implementation",
        body="bounded implementation",
        assignee="implementer",
        created_by="orchestrator",
        workspace_kind="dir",
        workspace_path=repo,
    )
    implementation_run = kb.claim_task(conn, implementation, claimer="implementer:fixture")
    assert implementation_run is not None
    assert kb.complete_task(
        conn,
        implementation,
        summary="implementation complete",
        expected_run_id=implementation_run.current_run_id,
    )
    body = review_body().replace("t_placeholder", implementation)
    leaf = kb.create_task(
        conn,
        title="Standalone exact-head review",
        body=body,
        assignee="reviewer",
        created_by="orchestrator",
        workspace_kind="dir",
        workspace_path=repo,
        parents=(implementation,),
    )
    child = kb.create_task(
        conn,
        title="Fan-in after review",
        body="must remain gated",
        assignee="orchestrator",
        created_by="orchestrator",
        parents=(leaf,),
    )
    claimed = kb.claim_task(conn, leaf, claimer="reviewer:fixture")
    assert claimed is not None
    return implementation, leaf, child, int(claimed.current_run_id)
'''


def test_native_standalone_rejection_creates_one_coordinator_owned_successor(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

with kb.connect_closing(db) as conn:
    implementation, leaf, child, review_run = make_graph(conn)
    # A standalone leaf cannot use the same-card rework transition.
    ok, diagnostic = kb.request_changes(
        conn, leaf, reason="must not route", expected_run_id=review_run
    )
    assert ok is False
    assert diagnostic == "active run was not claimed from review"
    assert kb.get_task(conn, leaf).status == "running"
    assert kb.block_task(
        conn,
        leaf,
        reason="STANDALONE_REVIEW_CHANGES_REQUESTED: fix the verified lifecycle defect",
        kind="dependency",
        expected_run_id=review_run,
    )
    assert kb.get_task(conn, leaf).status == "blocked"
    receipt = conn.execute("SELECT * FROM review_remediation_handoffs").fetchone()
    assert receipt is not None and receipt["status"] == "pending"
    assert conn.execute(
        "SELECT outcome FROM task_runs WHERE id = ?", (review_run,)
    ).fetchone()["outcome"] == "changes_requested"
    assert kb.get_task(conn, child).status == "todo"

barrier = Barrier(2)
def consume():
    with kb.connect_closing(db) as conn:
        barrier.wait()
        return kb.consume_standalone_review_handoffs(conn, actor="orchestrator")

with ThreadPoolExecutor(max_workers=2) as pool:
    outcomes = list(pool.map(lambda _: consume(), range(2)))

with kb.connect_closing(db) as conn:
    receipt = conn.execute("SELECT * FROM review_remediation_handoffs").fetchone()
    successor_id = receipt["successor_task_id"]
    assert receipt["status"] == "applied"
    assert kb.get_task(conn, leaf).status == "archived"
    successor = kb.get_task(conn, successor_id)
    assert successor.status == "ready"
    assert successor.assignee == "implementer"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE idempotency_key = ?",
        (receipt["handoff_key"],),
    ).fetchone()["n"] == 1
    child_parents = {
        row["parent_id"]
        for row in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (child,)
        ).fetchall()
    }
    assert successor_id in child_parents and leaf not in child_parents
    assert kb.get_task(conn, child).status == "todo"

    implementation_claim = kb.claim_task(conn, successor_id, claimer="implementer:remediation")
    assert implementation_claim is not None
    # Direct implementation completion cannot bypass the mandatory same-card review.
    assert not kb.complete_task(
        conn,
        successor_id,
        summary="attempted direct completion",
        metadata={"review_outcome": "APPROVED", "candidate_commit": head},
        expected_run_id=implementation_claim.current_run_id,
    )
    assert kb.get_task(conn, child).status == "todo"
    assert kb.request_review(
        conn,
        successor_id,
        summary="exact remediation candidate",
        metadata={"candidate_commit": head},
        reviewer="reviewer",
        expected_run_id=implementation_claim.current_run_id,
    )
    first_review = kb.claim_review_task(conn, successor_id, claimer="reviewer:first")
    assert first_review is not None
    # Same-card CHANGES_REQUESTED uses only native rework and creates no successor.
    same_card_ok, implementer = kb.request_changes(
        conn,
        successor_id,
        reason="correct the remediation",
        expected_run_id=first_review.current_run_id,
    )
    assert same_card_ok and implementer == "implementer"
    assert kb.get_task(conn, successor_id).status == "ready"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM review_remediation_handoffs"
    ).fetchone()["n"] == 1

    second_impl = kb.claim_task(conn, successor_id, claimer="implementer:second")
    assert second_impl is not None
    assert kb.request_review(
        conn,
        successor_id,
        summary="corrected exact remediation candidate",
        metadata={"candidate_commit": head},
        reviewer="reviewer",
        expected_run_id=second_impl.current_run_id,
    )
    second_review = kb.claim_review_task(conn, successor_id, claimer="reviewer:second")
    assert second_review is not None
    assert kb.complete_task(
        conn,
        successor_id,
        summary="APPROVED exact remediation",
        metadata={"review_outcome": "APPROVED", "candidate_commit": head},
        expected_run_id=second_review.current_run_id,
    )
    assert kb.get_task(conn, child).status == "ready"
    flattened = [entry for batch in outcomes for entry in batch]
    assert {entry["successor_task_id"] for entry in flattened} == {successor_id}
    print(json.dumps({
        "leaf": kb.get_task(conn, leaf).status,
        "successor": kb.get_task(conn, successor_id).status,
        "child": kb.get_task(conn, child).status,
        "successor_count": conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE idempotency_key = ?",
            (receipt["handoff_key"],),
        ).fetchone()["n"],
    }))
''',
    )
    assert probe == {
        "leaf": "archived",
        "successor": "done",
        "child": "ready",
        "successor_count": 1,
    }


def test_native_changed_or_foreign_frontier_fails_closed(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
with kb.connect_closing(db) as conn:
    implementation, leaf, child, review_run = make_graph(conn)
    assert kb.block_task(
        conn,
        leaf,
        reason="STANDALONE_REVIEW_CHANGES_REQUESTED: exact finding",
        kind="dependency",
        expected_run_id=review_run,
    )
    receipt = conn.execute("SELECT * FROM review_remediation_handoffs").fetchone()
    assert kb.consume_standalone_review_handoffs(conn, actor="foreign") == []
    foreign_child = kb.create_task(
        conn,
        title="Foreign changed frontier",
        body="must not be absorbed by stale evidence",
        assignee="orchestrator",
        created_by="orchestrator",
        parents=(leaf,),
    )
    try:
        kb.consume_standalone_review_handoffs(conn, actor="orchestrator")
    except RuntimeError as exc:
        assert "provenance or frontier changed" in str(exc)
    else:
        raise AssertionError("changed frontier was accepted")
    assert kb.get_task(conn, leaf).status == "blocked"
    assert kb.get_task(conn, child).status == "todo"
    assert kb.get_task(conn, foreign_child).status == "todo"
    assert conn.execute(
        "SELECT status FROM review_remediation_handoffs WHERE handoff_key = ?",
        (receipt["handoff_key"],),
    ).fetchone()["status"] == "pending"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE idempotency_key = ?",
        (receipt["handoff_key"],),
    ).fetchone()["n"] == 0
    print(json.dumps({"leaf": "blocked", "pending": 1, "successors": 0}))
''',
    )
    assert probe == {"leaf": "blocked", "pending": 1, "successors": 0}


def test_native_stale_run_foreign_reviewer_and_forged_approval_do_not_release(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
with kb.connect_closing(db) as conn:
    implementation, leaf, child, review_run = make_graph(conn)
    assert not kb.block_task(
        conn,
        leaf,
        reason="STANDALONE_REVIEW_CHANGES_REQUESTED: stale run",
        kind="dependency",
        expected_run_id=review_run + 1,
    )
    assert kb.get_task(conn, leaf).status == "running"
    assert conn.execute("SELECT COUNT(*) AS n FROM review_remediation_handoffs").fetchone()["n"] == 0

    assert not kb.complete_task(
        conn,
        leaf,
        summary="forged approval",
        metadata={"review_outcome": "APPROVED", "candidate_commit": "0" * 40},
        expected_run_id=review_run,
    )
    assert kb.get_task(conn, leaf).status == "running"
    assert kb.get_task(conn, child).status == "todo"
    original_body = conn.execute(
        "SELECT body FROM tasks WHERE id = ?", (leaf,)
    ).fetchone()["body"]
    conn.execute(
        "UPDATE tasks SET body = 'review_type: standalone review leaf' WHERE id = ?",
        (leaf,),
    )
    assert not kb.complete_task(
        conn,
        leaf,
        summary="approval after packet mutation",
        metadata={"review_outcome": "APPROVED", "candidate_commit": head},
        expected_run_id=review_run,
    )
    conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (original_body, leaf))
    conn.execute(
        "UPDATE task_runs SET profile = 'foreign-reviewer' WHERE id = ?",
        (review_run,),
    )
    assert not kb.complete_task(
        conn,
        leaf,
        summary="foreign approval",
        metadata={"review_outcome": "APPROVED", "candidate_commit": head},
        expected_run_id=review_run,
    )
    assert kb.get_task(conn, child).status == "todo"
    blocked_events = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events "
        "WHERE task_id = ? AND kind = 'completion_blocked_review_evidence'",
        (leaf,),
    ).fetchone()["n"]
    assert blocked_events == 3
    print(json.dumps({"leaf": "running", "child": "todo", "blocked_events": blocked_events}))
''',
    )
    assert probe == {"leaf": "running", "child": "todo", "blocked_events": 3}


def test_recovery_adapter_consumes_the_native_outbox_path(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
import importlib.util

with kb.connect_closing(db) as conn:
    implementation, leaf, child, review_run = make_graph(conn)
    assert kb.block_task(
        conn,
        leaf,
        reason="STANDALONE_REVIEW_CHANGES_REQUESTED: adapter-consumed finding",
        kind="dependency",
        expected_run_id=review_run,
    )

script_path = Path(os.environ["RECOVERY_SCRIPT"])
spec = importlib.util.spec_from_file_location("review_recovery_adapter_probe", script_path)
assert spec is not None and spec.loader is not None
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
recovery._board_db_path = lambda board: db
recovery._active_profile_name = lambda: "orchestrator"
planned = recovery._recover_native_review_remediation_handoffs("fixture", apply=False)
assert len(planned) == 1 and planned[0].startswith("would consume native review remediation handoff")
changes = recovery._recover_native_review_remediation_handoffs("fixture", apply=True)
assert len(changes) == 1 and changes[0].startswith("consumed native review remediation handoff")
with kb.connect_closing(db) as conn:
    receipt = conn.execute("SELECT * FROM review_remediation_handoffs").fetchone()
    assert receipt["status"] == "applied"
    assert kb.get_task(conn, leaf).status == "archived"
    assert kb.get_task(conn, receipt["successor_task_id"]).status == "ready"
    assert kb.get_task(conn, child).status == "todo"
    print(json.dumps({
        "planned": len(planned),
        "consumed": len(changes),
        "receipt": receipt["status"],
    }))
''',
    )
    assert probe == {"planned": 1, "consumed": 1, "receipt": "applied"}
