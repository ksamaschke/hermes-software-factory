"""Causal private-board regressions for the native review lifecycle boundary.

The subprocesses import an explicitly staged Factory runtime. They create only
private SQLite boards and disposable Git repositories; no live board, profile,
or service is mutated.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _runtime() -> Path:
    value = os.environ.get("FACTORY_NATIVE_RUNTIME")
    if not value:
        pytest.skip("FACTORY_NATIVE_RUNTIME is required for native lifecycle probes")
    runtime = Path(value).resolve()
    assert (runtime / "hermes_cli" / "kanban_db.py").is_file()
    assert (runtime / "hermes_cli" / "native_boundary.py").is_file()
    return runtime


def _run(command: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _scope_sha(repo: Path, base: str, candidate: str) -> str:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, candidate],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    paths = sorted(part for part in result.stdout.split(b"\0") if part)
    canonical = b"\0".join(paths) + (b"\0" if paths else b"")
    return hashlib.sha256(canonical).hexdigest()


def _git_fixture(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-b", "review-fixture"], cwd=repo)
    _run(["git", "config", "user.name", "Factory Review Test"], cwd=repo)
    _run(["git", "config", "user.email", "factory-review@example.invalid"], cwd=repo)
    _run(
        ["git", "remote", "add", "origin", "https://github.com/fixture/repository.git"],
        cwd=repo,
    )
    (repo / "fixture.txt").write_text("base\n", encoding="utf-8")
    _run(["git", "add", "fixture.txt"], cwd=repo)
    _run(["git", "commit", "-m", "base fixture"], cwd=repo)
    base = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    (repo / "fixture.txt").write_text("base\ncandidate\n", encoding="utf-8")
    _run(["git", "add", "fixture.txt"], cwd=repo)
    _run(["git", "commit", "-m", "candidate fixture"], cwd=repo)
    candidate = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    return repo, base, candidate, _scope_sha(repo, base, candidate), "review-fixture"


def _run_native(tmp_path: Path, script: str) -> dict:
    runtime = _runtime()
    repo, base, candidate, scope_sha, branch = _git_fixture(tmp_path)
    db = tmp_path / "private-kanban.db"
    home = tmp_path / "home"
    home.mkdir()
    profiles = home / ".hermes" / "profiles"
    for profile in ("orchestrator", "implementer", "reviewer", "coordinator2", "foreign"):
        (profiles / profile).mkdir(parents=True, exist_ok=True)
    environment = {
        "HOME": str(home),
        "HERMES_HOME": str(profiles / "orchestrator"),
        "HERMES_KANBAN_DB": str(db),
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(runtime),
        "PATH": "/usr/bin:/bin",
        "TARGET_REPO": str(repo),
        "TARGET_BASE": base,
        "TARGET_HEAD": candidate,
        "TARGET_SCOPE_SHA": scope_sha,
        "TARGET_BRANCH": branch,
        "RECOVERY_SCRIPT": str(ROOT / "scripts" / "kanban_review_successor_recovery.py"),
    }
    result = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(script)],
        cwd=runtime,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        numbered = "\n".join(
            f"{line_number:04d}|{line}"
            for line_number, line in enumerate(textwrap.dedent(script).splitlines(), 1)
        )
        pytest.fail((result.stderr or result.stdout) + "\nNative probe:\n" + numbered)
    return json.loads(result.stdout)


_COMMON = r'''
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import subprocess
from hermes_cli import kanban_db as kb


db = Path(os.environ["HERMES_KANBAN_DB"])
repo = Path(os.environ["TARGET_REPO"])
base = os.environ["TARGET_BASE"]
head = os.environ["TARGET_HEAD"]
initial_scope_sha = os.environ["TARGET_SCOPE_SHA"]
branch = os.environ["TARGET_BRANCH"]
profiles_root = Path(os.environ["HOME"]) / ".hermes" / "profiles"
kb.init_db(db)


@contextmanager
def active(profile):
    previous = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(profiles_root / profile)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous


def git(*args):
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def scope_sha(base_commit, candidate_commit):
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", base_commit, candidate_commit],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    paths = sorted(part for part in result.stdout.split(b"\0") if part)
    canonical = b"\0".join(paths) + (b"\0" if paths else b"")
    return hashlib.sha256(canonical).hexdigest()


def commit_remediation():
    marker = repo / "remediation.txt"
    marker.write_text("review remediation\n", encoding="utf-8")
    git("add", "remediation.txt")
    git("commit", "-m", "review remediation")
    candidate = git("rev-parse", "HEAD")
    return candidate, scope_sha(head, candidate)


def review_body(implementation):
    return "\n".join([
        "review_type: read-only adversarial code review leaf",
        f"implementation_task: {implementation}",
        "target_repository: fixture/repository",
        f"target_worktree: {repo}",
        f"branch: {branch}",
        f"base_commit: {base}",
        f"candidate_commit: {head}",
        f"scope_manifest_sha256: {initial_scope_sha}",
        "implementer_profile: implementer",
        "reviewer_profile: reviewer",
        "read_only_source: true",
        "review_kind: pre_commit",
        "review_scope: change_set",
    ])


def finish_plain_task(conn, task_id, profile="implementer"):
    claimed = kb.claim_task(conn, task_id, claimer=f"dispatcher:{profile}")
    assert claimed is not None
    with active(profile):
        assert kb.complete_task(
            conn,
            task_id,
            summary="fixture prerequisite complete",
            expected_run_id=claimed.current_run_id,
        )


def make_graph(conn, *, coordinator="orchestrator", extra_parent=False):
    implementation = kb.create_task(
        conn,
        title="Implementation",
        body="bounded implementation",
        assignee="implementer",
        created_by=coordinator,
        workspace_kind="dir",
        workspace_path=str(repo),
    )
    finish_plain_task(conn, implementation)
    parents = [implementation]
    gate = None
    if extra_parent:
        gate = kb.create_task(
            conn,
            title="Independent provenance gate",
            body="second direct parent",
            assignee="implementer",
            created_by=coordinator,
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        finish_plain_task(conn, gate)
        parents.append(gate)
    leaf = kb.create_task(
        conn,
        title=f"Standalone exact-head review by {coordinator}",
        body=review_body(implementation),
        assignee="reviewer",
        created_by=coordinator,
        workspace_kind="dir",
        workspace_path=str(repo),
        parents=tuple(parents),
    )
    child = kb.create_task(
        conn,
        title=f"Fan-in after review by {coordinator}",
        body="must remain gated",
        assignee=coordinator,
        created_by=coordinator,
        parents=(leaf,),
    )
    claimed = kb.claim_task(conn, leaf, claimer="dispatcher:reviewer")
    assert claimed is not None
    return {
        "implementation": implementation,
        "gate": gate,
        "leaf": leaf,
        "child": child,
        "review_run": claimed.current_run_id,
    }


def reject_leaf(conn, graph, finding="verified lifecycle defect"):
    with active("reviewer"):
        assert kb.block_task(
            conn,
            graph["leaf"],
            reason=f"STANDALONE_REVIEW_CHANGES_REQUESTED: {finding}",
            kind="dependency",
            expected_run_id=graph["review_run"],
        )
    receipt = conn.execute(
        "SELECT * FROM review_remediation_handoffs WHERE leaf_task_id = ?",
        (graph["leaf"],),
    ).fetchone()
    assert receipt is not None and receipt["status"] == "pending"
    return receipt
'''


def test_native_rejection_is_nonredispatching_and_remediates_once(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

with kb.connect_closing(db) as conn:
    graph = make_graph(conn)
    with active("reviewer"):
        ok, diagnostic = kb.request_changes(
            conn,
            graph["leaf"],
            reason="must not route as same-card review",
            expected_run_id=graph["review_run"],
        )
    assert ok is False and diagnostic == "active run was not claimed from review"
    receipt = reject_leaf(conn, graph)
    runs_before = conn.execute(
        "SELECT COUNT(*) AS n FROM task_runs WHERE task_id = ?", (graph["leaf"],)
    ).fetchone()["n"]
    kb.recompute_ready(conn)
    assert kb.get_task(conn, graph["leaf"]).status == "blocked"
    kb.dispatch_once(conn, dry_run=True, max_spawn=8, board="fixture")
    assert kb.get_task(conn, graph["leaf"]).status == "blocked"
    assert kb.claim_task(conn, graph["leaf"], claimer="dispatcher:reviewer") is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM task_runs WHERE task_id = ?", (graph["leaf"],)
    ).fetchone()["n"] == runs_before

barrier = Barrier(2)
def consume():
    with kb.connect_closing(db) as conn:
        barrier.wait()
        with active("orchestrator"):
            return kb.consume_standalone_review_handoffs(conn)

with ThreadPoolExecutor(max_workers=2) as pool:
    outcomes = list(pool.map(lambda _: consume(), range(2)))

with kb.connect_closing(db) as conn:
    receipt = conn.execute(
        "SELECT * FROM review_remediation_handoffs WHERE leaf_task_id = ?",
        (graph["leaf"],),
    ).fetchone()
    successor_id = receipt["successor_task_id"]
    assert receipt["status"] == "applied"
    assert kb.get_task(conn, graph["leaf"]).status == "archived"
    assert kb.get_task(conn, successor_id).status == "ready"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE idempotency_key = ?",
        (receipt["handoff_key"],),
    ).fetchone()["n"] == 1
    assert kb.get_task(conn, graph["child"]).status == "todo"

    implementation_claim = kb.claim_task(
        conn, successor_id, claimer="dispatcher:implementer"
    )
    assert implementation_claim is not None
    original_body = conn.execute(
        "SELECT body FROM tasks WHERE id = ?", (successor_id,)
    ).fetchone()["body"]
    try:
        conn.execute(
            "UPDATE tasks SET body = REPLACE(body, 'review_required_before_completion: true', '') "
            "WHERE id = ?",
            (successor_id,),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("ordinary writer mutated remediation body")
    with kb._review_native_mutation_authorized(conn):
        conn.execute(
            "UPDATE tasks SET body = REPLACE(body, 'review_required_before_completion: true', '') "
            "WHERE id = ?",
            (successor_id,),
        )
    with active("implementer"):
        assert not kb.complete_task(
            conn,
            successor_id,
            summary="body marker removed",
            expected_run_id=implementation_claim.current_run_id,
        )
        assert not kb.request_review(
            conn,
            successor_id,
            summary="tampered body",
            metadata={
                "candidate_commit": head,
                "review_remediation_handoff_key": receipt["handoff_key"],
                "scope_manifest_sha256": hashlib.sha256(b"").hexdigest(),
            },
            reviewer="reviewer",
            expected_run_id=implementation_claim.current_run_id,
        )
    with kb._review_native_mutation_authorized(conn):
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (original_body, successor_id))

    remediation_head, remediation_scope = commit_remediation()
    review_metadata = {
        "candidate_commit": remediation_head,
        "review_remediation_handoff_key": receipt["handoff_key"],
        "scope_manifest_sha256": remediation_scope,
    }

    class StringSubclass(str):
        pass

    class StringableLookalike:
        def __init__(self, value):
            self.value = value

        def __str__(self):
            return self.value

    class SneakyDict(dict):
        def __init__(self, raw, exposed):
            super().__init__(raw)
            self.exposed = exposed

        def items(self):
            return self.exposed.items()

        def get(self, key, default=None):
            return self.exposed.get(key, default)

        def __getitem__(self, key):
            return self.exposed[key]

    invalid_request_values = [
        ("candidate_commit", int("1" * 40)),
        ("candidate_commit", True),
        ("candidate_commit", 1.0),
        ("candidate_commit", b"a" * 40),
        ("candidate_commit", StringSubclass(remediation_head)),
        ("candidate_commit", StringableLookalike(remediation_head)),
        ("scope_manifest_sha256", int("1" * 64)),
        ("scope_manifest_sha256", b"b" * 64),
        ("scope_manifest_sha256", StringSubclass(remediation_scope)),
        ("review_remediation_handoff_key", True),
        ("review_remediation_handoff_key", b"opaque"),
        (
            "review_remediation_handoff_key",
            StringSubclass(receipt["handoff_key"]),
        ),
    ]
    for field, value in invalid_request_values:
        invalid_metadata = dict(review_metadata)
        invalid_metadata[field] = value
        before_receipts = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? "
            "AND kind = 'review_requested'",
            (successor_id,),
        ).fetchone()["n"]
        with active("implementer"):
            requested, request_reason = kb.request_review(
                conn,
                successor_id,
                summary="reject typed remediation lookalike",
                metadata=invalid_metadata,
                reviewer="reviewer",
                expected_run_id=implementation_claim.current_run_id,
                with_reason=True,
            )
        assert not requested, (field, value, request_reason)
        assert kb.get_task(conn, successor_id).status == "running"
        implementation_run = kb.latest_run(conn, successor_id)
        assert implementation_run.status == "running"
        assert implementation_run.outcome is None
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? "
            "AND kind = 'review_requested'",
            (successor_id,),
        ).fetchone()["n"] == before_receipts

    sneaky_request_metadata = SneakyDict(
        {
            "candidate_commit": int("1" * 40),
            "review_remediation_handoff_key": StringSubclass(
                receipt["handoff_key"]
            ),
            "scope_manifest_sha256": b"b" * 64,
        },
        review_metadata,
    )
    before_receipts = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? "
        "AND kind = 'review_requested'",
        (successor_id,),
    ).fetchone()["n"]
    with active("implementer"):
        requested, request_reason = kb.request_review(
            conn,
            successor_id,
            summary="reject dict subclass remediation metadata",
            metadata=sneaky_request_metadata,
            reviewer="reviewer",
            expected_run_id=implementation_claim.current_run_id,
            with_reason=True,
        )
    assert not requested, request_reason
    assert "exact built-in dict" in str(request_reason)
    assert kb.get_task(conn, successor_id).status == "running"
    implementation_run = kb.latest_run(conn, successor_id)
    assert implementation_run.status == "running"
    assert implementation_run.outcome is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? "
        "AND kind = 'review_requested'",
        (successor_id,),
    ).fetchone()["n"] == before_receipts

    subclass_key_request_metadata = {
        StringSubclass("candidate_commit"): remediation_head,
        "review_remediation_handoff_key": receipt["handoff_key"],
        "scope_manifest_sha256": remediation_scope,
    }
    with active("implementer"):
        requested, request_reason = kb.request_review(
            conn,
            successor_id,
            summary="reject string subclass metadata key",
            metadata=subclass_key_request_metadata,
            reviewer="reviewer",
            expected_run_id=implementation_claim.current_run_id,
            with_reason=True,
        )
    assert not requested, request_reason
    assert "metadata keys must use exact built-in strings" in str(request_reason)
    assert kb.get_task(conn, successor_id).status == "running"
    implementation_run = kb.latest_run(conn, successor_id)
    assert implementation_run.status == "running"
    assert implementation_run.outcome is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? "
        "AND kind = 'review_requested'",
        (successor_id,),
    ).fetchone()["n"] == before_receipts

    original_redact_review_value = kb.redact_review_value

    def redactor_that_masks_opaque_handoff(value):
        if isinstance(value, str) and value == receipt["handoff_key"]:
            return "***"
        if isinstance(value, dict):
            return {
                key: redactor_that_masks_opaque_handoff(item)
                for key, item in value.items()
            }
        return original_redact_review_value(value)
    kb.redact_review_value = redactor_that_masks_opaque_handoff
    with active("implementer"):
        assert not kb.complete_task(
            conn,
            successor_id,
            summary="implementation cannot self-approve",
            metadata={"review_outcome": "APPROVED", **review_metadata},
            expected_run_id=implementation_claim.current_run_id,
        )
        requested, request_reason = kb.request_review(
            conn,
            successor_id,
            summary="exact remediation candidate",
            metadata=review_metadata,
            reviewer="reviewer",
            expected_run_id=implementation_claim.current_run_id,
            with_reason=True,
        )
        assert requested, request_reason
    first_review = kb.claim_review_task(
        conn, successor_id, claimer="dispatcher:reviewer:first"
    )
    assert first_review is not None
    with active("reviewer"):
        same_card_ok, implementer = kb.request_changes(
            conn,
            successor_id,
            reason="correct the remediation",
            expected_run_id=first_review.current_run_id,
        )
    assert same_card_ok and implementer == "implementer"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM review_remediation_handoffs"
    ).fetchone()["n"] == 1

    second_impl = kb.claim_task(conn, successor_id, claimer="dispatcher:implementer:second")
    assert second_impl is not None
    with active("implementer"):
        assert kb.request_review(
            conn,
            successor_id,
            summary="corrected exact remediation candidate",
            metadata=review_metadata,
            reviewer="reviewer",
            expected_run_id=second_impl.current_run_id,
        )
    second_review = kb.claim_review_task(
        conn, successor_id, claimer="dispatcher:reviewer:second"
    )
    assert second_review is not None
    invalid_completion_values = [
        ("candidate_commit", StringSubclass(remediation_head)),
        ("candidate_commit", int("1" * 40)),
        ("review_outcome", StringSubclass("APPROVED")),
        ("scope_manifest_sha256", StringSubclass(remediation_scope)),
        (
            "review_remediation_handoff_key",
            StringSubclass(receipt["handoff_key"]),
        ),
    ]
    for field, value in invalid_completion_values:
        invalid_metadata = {"review_outcome": "APPROVED", **review_metadata}
        invalid_metadata[field] = value
        before_completed = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? "
            "AND kind = 'completed'",
            (successor_id,),
        ).fetchone()["n"]
        with active("reviewer"):
            assert not kb.complete_task(
                conn,
                successor_id,
                summary="reject typed completion lookalike",
                metadata=invalid_metadata,
                expected_run_id=second_review.current_run_id,
            )
        assert kb.get_task(conn, successor_id).status == "running"
        reviewer_run = kb.latest_run(conn, successor_id)
        assert reviewer_run.status == "running"
        assert reviewer_run.outcome is None
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? "
            "AND kind = 'completed'",
            (successor_id,),
        ).fetchone()["n"] == before_completed

    valid_completion_metadata = {"review_outcome": "APPROVED", **review_metadata}
    sneaky_completion_metadata = SneakyDict(
        {
            "review_outcome": StringSubclass("APPROVED"),
            "candidate_commit": int("1" * 40),
            "review_remediation_handoff_key": StringSubclass(
                receipt["handoff_key"]
            ),
            "scope_manifest_sha256": b"b" * 64,
        },
        valid_completion_metadata,
    )
    before_completed = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? "
        "AND kind = 'completed'",
        (successor_id,),
    ).fetchone()["n"]
    with active("reviewer"):
        assert not kb.complete_task(
            conn,
            successor_id,
            summary="reject dict subclass completion metadata",
            metadata=sneaky_completion_metadata,
            expected_run_id=second_review.current_run_id,
        )
    assert kb.get_task(conn, successor_id).status == "running"
    reviewer_run = kb.latest_run(conn, successor_id)
    assert reviewer_run.status == "running"
    assert reviewer_run.outcome is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? "
        "AND kind = 'completed'",
        (successor_id,),
    ).fetchone()["n"] == before_completed

    subclass_key_completion_metadata = {
        StringSubclass("review_outcome"): "APPROVED",
        **review_metadata,
    }
    with active("reviewer"):
        assert not kb.complete_task(
            conn,
            successor_id,
            summary="reject string subclass completion key",
            metadata=subclass_key_completion_metadata,
            expected_run_id=second_review.current_run_id,
        )
    assert kb.get_task(conn, successor_id).status == "running"
    reviewer_run = kb.latest_run(conn, successor_id)
    assert reviewer_run.status == "running"
    assert reviewer_run.outcome is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? "
        "AND kind = 'completed'",
        (successor_id,),
    ).fetchone()["n"] == before_completed

    with active("reviewer"):
        assert kb.complete_task(
            conn,
            successor_id,
            summary="APPROVED exact remediation",
            metadata={"review_outcome": "APPROVED", **review_metadata},
            expected_run_id=second_review.current_run_id,
        )
    assert kb.get_task(conn, graph["child"]).status == "ready"
    flattened = [entry for batch in outcomes for entry in batch]
    assert {entry["successor_task_id"] for entry in flattened} == {successor_id}
    print(json.dumps({
        "leaf": kb.get_task(conn, graph["leaf"]).status,
        "successor": kb.get_task(conn, successor_id).status,
        "child": kb.get_task(conn, graph["child"]).status,
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


def test_multi_parent_successor_preserves_reopen_invalidation(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
with kb.connect_closing(db) as conn:
    graph = make_graph(conn, extra_parent=True)
    receipt = reject_leaf(conn, graph, "preserve every direct parent")
    with active("orchestrator"):
        applied = kb.consume_standalone_review_handoffs(conn)
    assert len(applied) == 1
    successor_id = applied[0]["successor_task_id"]
    parents = {
        row["parent_id"]
        for row in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (successor_id,)
        ).fetchall()
    }
    assert parents == {graph["implementation"], graph["gate"]}
    conn.execute(
        "UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?",
        (graph["gate"],),
    )
    invalidation = kb.invalidate_descendants_for_parent_reopen(
        conn, graph["gate"], author="operator"
    )
    assert successor_id in {row["id"] for row in invalidation["invalidated"]}
    kb.recompute_ready(conn)
    assert kb.get_task(conn, successor_id).status == "todo"
    assert kb.claim_task(conn, successor_id, claimer="dispatcher:implementer") is None
    print(json.dumps({"parents": len(parents), "successor": "todo", "claimable": False}))
''',
    )
    assert probe == {"parents": 2, "successor": "todo", "claimable": False}


def test_changed_frontier_and_profile_spoof_fail_closed(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
with kb.connect_closing(db) as conn:
    graph = make_graph(conn)
    receipt = reject_leaf(conn, graph, "frontier must remain exact")
    with active("implementer"):
        assert kb.consume_standalone_review_handoffs(conn) == []
    try:
        kb.consume_standalone_review_handoffs(conn, actor="orchestrator")
    except TypeError:
        pass
    else:
        raise AssertionError("caller-supplied actor string was accepted")
    foreign_child = kb.create_task(
        conn,
        title="Foreign changed frontier",
        body="must not be absorbed by stale evidence",
        assignee="orchestrator",
        created_by="orchestrator",
    )
    try:
        kb.link_tasks(conn, graph["leaf"], foreign_child)
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("ordinary writer changed a frozen review frontier")
    with kb._review_native_mutation_authorized(conn):
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)",
            (graph["leaf"], foreign_child),
        )
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (foreign_child,))
    with active("orchestrator"):
        try:
            kb.consume_standalone_review_handoffs(conn)
        except RuntimeError as exc:
            assert (
                "provenance or frontier changed" in str(exc)
                or "packet receipt changed" in str(exc)
            )
        else:
            raise AssertionError("changed frontier was accepted")
    assert kb.get_task(conn, graph["leaf"]).status == "blocked"
    assert kb.get_task(conn, foreign_child).status == "todo"
    assert conn.execute(
        "SELECT status FROM review_remediation_handoffs WHERE handoff_key = ?",
        (receipt["handoff_key"],),
    ).fetchone()["status"] == "pending"
    print(json.dumps({"leaf": "blocked", "pending": 1, "successors": 0}))
''',
    )
    assert probe == {"leaf": "blocked", "pending": 1, "successors": 0}


def test_exact_run_and_independent_actor_fences(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
with kb.connect_closing(db) as conn:
    graph = make_graph(conn)
    review_run = graph["review_run"]
    for bad_run in (None, str(review_run), float(review_run), True, review_run + 1):
        with active("reviewer"):
            assert not kb.complete_task(
                conn,
                graph["leaf"],
                summary="bad fence",
                metadata={"review_outcome": "APPROVED", "candidate_commit": head},
                expected_run_id=bad_run,
            )
            ok, _ = kb.request_changes(
                conn,
                graph["leaf"],
                reason="bad fence",
                expected_run_id=bad_run,
            )
            assert not ok
    with active("implementer"):
        assert not kb.complete_task(
            conn,
            graph["leaf"],
            summary="implementer cannot approve",
            metadata={"review_outcome": "APPROVED", "candidate_commit": head},
            expected_run_id=review_run,
        )
    with active("reviewer"):
        assert kb.complete_task(
            conn,
            graph["leaf"],
            summary="independent exact approval",
            metadata={"review_outcome": "APPROVED", "candidate_commit": head},
            expected_run_id=review_run,
        )
    assert kb.get_task(conn, graph["child"]).status == "ready"
    print(json.dumps({"rejected_fences": 5, "foreign_actor": "rejected", "approved": True}))
''',
    )
    assert probe == {
        "rejected_fences": 5,
        "foreign_actor": "rejected",
        "approved": True,
    }


def test_role_collisions_and_duplicate_json_receipts_fail_closed(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
with kb.connect_closing(db) as conn:
    implementation = kb.create_task(
        conn,
        title="role prerequisite",
        body="plain",
        assignee="implementer",
        created_by="orchestrator",
        workspace_kind="dir",
        workspace_path=str(repo),
    )
    finish_plain_task(conn, implementation)
    same_role_body = review_body(implementation).replace(
        "implementer_profile: implementer", "implementer_profile: reviewer"
    )
    same_role = kb.create_task(
        conn,
        title="same role",
        body=same_role_body,
        assignee="reviewer",
        created_by="orchestrator",
        workspace_kind="dir",
        workspace_path=str(repo),
        parents=(implementation,),
    )
    assert kb.claim_task(conn, same_role, claimer="dispatcher:reviewer") is None
    creator_collision = kb.create_task(
        conn,
        title="creator collision",
        body=review_body(implementation),
        assignee="reviewer",
        created_by="implementer",
        workspace_kind="dir",
        workspace_path=str(repo),
        parents=(implementation,),
    )
    assert kb.claim_task(conn, creator_collision, claimer="dispatcher:reviewer") is None
    wrong_repository_body = review_body(implementation).replace(
        "target_repository: fixture/repository",
        "target_repository: foreign/wrong",
    ).replace(f"branch: {branch}", "branch: wrong-branch")
    wrong_repository = kb.create_task(
        conn,
        title="wrong repository and branch",
        body=wrong_repository_body,
        assignee="reviewer",
        created_by="orchestrator",
        workspace_kind="dir",
        workspace_path=str(repo),
        parents=(implementation,),
    )
    assert kb.claim_task(conn, wrong_repository, claimer="dispatcher:reviewer") is None
    incomplete_packet_body = "\n".join(
        line
        for line in review_body(implementation).splitlines()
        if not line.startswith(("base_commit:", "scope_manifest_sha256:"))
    )
    incomplete_packet = kb.create_task(
        conn,
        title="missing base and scope",
        body=incomplete_packet_body,
        assignee="reviewer",
        created_by="orchestrator",
        workspace_kind="dir",
        workspace_path=str(repo),
        parents=(implementation,),
    )
    assert kb.claim_task(conn, incomplete_packet, claimer="dispatcher:reviewer") is None

    ordinary = kb.create_task(
        conn,
        title="same-card role collision",
        body="ordinary implementation",
        assignee="implementer",
        created_by="orchestrator",
        workspace_kind="dir",
        workspace_path=str(repo),
    )
    ordinary_run = kb.claim_task(conn, ordinary, claimer="dispatcher:implementer")
    assert ordinary_run is not None
    with active("implementer"):
        assert not kb.request_review(
            conn,
            ordinary,
            summary="self review",
            metadata={"candidate_commit": head},
            reviewer="implementer",
            expected_run_id=ordinary_run.current_run_id,
        )

    graph = make_graph(conn)
    claimed_event = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind = 'claimed' ORDER BY id DESC LIMIT 1",
        (graph["leaf"], graph["review_run"]),
    ).fetchone()
    payload = json.loads(claimed_event["payload"])
    duplicate = (
        '{"lock":' + json.dumps(payload["lock"]) + ',"lock":"duplicate",'
        '"expires":' + str(payload["expires"]) + ',"run_id":' + str(payload["run_id"]) + '}'
    )
    conn.execute(
        "UPDATE task_events SET payload = ? WHERE id = ?", (duplicate, claimed_event["id"])
    )
    with active("reviewer"):
        assert kb.block_task(
            conn,
            graph["leaf"],
            reason="STANDALONE_REVIEW_CHANGES_REQUESTED: duplicate JSON must fail",
            kind="dependency",
            expected_run_id=graph["review_run"],
        )
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM review_remediation_handoffs WHERE leaf_task_id = ?",
        (graph["leaf"],),
    ).fetchone()["n"] == 0
    assert kb.get_task(conn, graph["leaf"]).status == "blocked"

    packet_graph = make_graph(conn)
    packet_event = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind = 'standalone_review_packet_claimed' ORDER BY id DESC LIMIT 1",
        (packet_graph["leaf"], packet_graph["review_run"]),
    ).fetchone()
    packet_payload = json.loads(packet_event["payload"])
    packet_items = ",".join(
        json.dumps(key) + ":" + json.dumps(value, separators=(",", ":"))
        for key, value in packet_payload.items()
    )
    duplicate_packet = (
        '{"packet_sha256":"' + ("0" * 64) + '",' + packet_items + '}'
    )
    try:
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (duplicate_packet, packet_event["id"]),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("ordinary writer changed immutable review evidence")
    with kb._review_native_mutation_authorized(conn):
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (duplicate_packet, packet_event["id"]),
        )
    with active("reviewer"):
        assert kb.block_task(
            conn,
            packet_graph["leaf"],
            reason="STANDALONE_REVIEW_CHANGES_REQUESTED: duplicate packet JSON must fail",
            kind="dependency",
            expected_run_id=packet_graph["review_run"],
        )
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM review_remediation_handoffs"
    ).fetchone()["n"] == 0
    assert kb.get_task(conn, packet_graph["leaf"]).status == "blocked"
    print(json.dumps({
        "role_or_packet_rejections": 5,
        "duplicate_json": "blocked_without_handoff",
    }))
''',
    )
    assert probe == {
        "role_or_packet_rejections": 5,
        "duplicate_json": "blocked_without_handoff",
    }


def test_remediation_idempotency_is_unique_under_a_writer_race(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

with kb.connect_closing(db) as conn:
    graph = make_graph(conn)
    receipt = reject_leaf(conn, graph, "idempotency race")
    handoff_key = receipt["handoff_key"]

barrier = Barrier(2)
def consume():
    with kb.connect_closing(db) as conn:
        barrier.wait()
        try:
            with active("orchestrator"):
                return ("consumer", kb.consume_standalone_review_handoffs(conn))
        except Exception as exc:
            return ("consumer_error", type(exc).__name__)

def compete():
    with kb.connect_closing(db) as conn:
        barrier.wait()
        try:
            task_id = kb.create_task(
                conn,
                title="competing duplicate",
                body="malformed competing task",
                assignee="implementer",
                created_by="orchestrator",
                workspace_kind="dir",
                workspace_path=str(repo),
                idempotency_key=handoff_key,
            )
            return ("competitor", task_id)
        except Exception as exc:
            return ("competitor_error", type(exc).__name__)

with ThreadPoolExecutor(max_workers=2) as pool:
    outcomes = list(pool.map(lambda fn: fn(), (consume, compete)))

with kb.connect_closing(db) as conn:
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE idempotency_key = ?", (handoff_key,)
    ).fetchone()["n"]
    assert count == 1
    receipt = conn.execute(
        "SELECT * FROM review_remediation_handoffs WHERE handoff_key = ?", (handoff_key,)
    ).fetchone()
    if receipt["status"] == "applied":
        assert receipt["successor_task_id"] is not None
        assert kb.get_task(conn, graph["leaf"]).status == "archived"
    else:
        assert receipt["status"] == "pending"
        assert kb.get_task(conn, graph["leaf"]).status == "blocked"
    print(json.dumps({"count": count, "receipt": receipt["status"], "outcomes": len(outcomes)}))
''',
    )
    assert probe["count"] == 1
    assert probe["receipt"] in {"pending", "applied"}
    assert probe["outcomes"] == 2


def test_recovery_adapter_filters_each_coordinator_and_verifies_runtime(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
import importlib.util

with kb.connect_closing(db) as conn:
    first = make_graph(conn, coordinator="orchestrator")
    reject_leaf(conn, first, "first coordinator")
    malformed = make_graph(conn, coordinator="orchestrator")
    reject_leaf(conn, malformed, "malformed same coordinator")
    try:
        conn.execute(
            "UPDATE tasks SET body = body || ? WHERE id = ?",
            ("\nreview_scope: tampered", malformed["leaf"]),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("ordinary writer mutated claimed review evidence")
    with kb._review_native_mutation_authorized(conn):
        conn.execute(
            "UPDATE tasks SET body = body || ? WHERE id = ?",
            ("\nreview_scope: tampered", malformed["leaf"]),
        )
    second = make_graph(conn, coordinator="coordinator2")
    reject_leaf(conn, second, "second coordinator")

script_path = Path(os.environ["RECOVERY_SCRIPT"])
spec = importlib.util.spec_from_file_location("review_recovery_adapter_probe", script_path)
assert spec is not None and spec.loader is not None
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
recovery._board_db_path = lambda board: db

with active("orchestrator"):
    planned_first = recovery._recover_native_review_remediation_handoffs("fixture", apply=False)
    applied_first = recovery._recover_native_review_remediation_handoffs("fixture", apply=True)
assert len(planned_first) == len(applied_first) == 2
assert sum(message.startswith("consumed ") for message in applied_first) == 1
assert sum(message.startswith("failed ") for message in applied_first) == 1
with kb.connect_closing(db) as conn:
    rows = conn.execute(
        "SELECT coordinator_profile, status, COUNT(*) AS n "
        "FROM review_remediation_handoffs GROUP BY coordinator_profile, status "
        "ORDER BY coordinator_profile, status"
    ).fetchall()
    status_after_first = {
        (row["coordinator_profile"], row["status"]): row["n"] for row in rows
    }
assert status_after_first == {
    ("coordinator2", "pending"): 1,
    ("orchestrator", "applied"): 1,
    ("orchestrator", "pending"): 1,
}

with active("coordinator2"):
    planned_second = recovery._recover_native_review_remediation_handoffs("fixture", apply=False)
    applied_second = recovery._recover_native_review_remediation_handoffs("fixture", apply=True)
assert len(planned_second) == len(applied_second) == 1
with kb.connect_closing(db) as conn:
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM review_remediation_handoffs WHERE status = 'pending'"
    ).fetchone()["n"]
    assert remaining == 1
    print(json.dumps({
        "first": len(applied_first),
        "second": len(applied_second),
        "pending": remaining,
        "failed": sum(message.startswith("failed ") for message in applied_first),
    }))
''',
    )
    assert probe == {"first": 2, "second": 1, "pending": 1, "failed": 1}


def test_terminal_review_receipts_block_every_generic_writer(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
with kb.connect_closing(db) as conn:
    graph = make_graph(conn)
    receipt = reject_leaf(conn, graph, "generic writers must not revive this leaf")
    promoted, _ = kb.promote_task(conn, graph["leaf"], actor="generic")
    assert promoted is False
    assert kb.schedule_task(conn, graph["leaf"], reason="generic") is False
    assert kb.unblock_task(conn, graph["leaf"]) is False
    assert kb.specify_triage_task(conn, graph["leaf"], author="generic") is False
    assert kb.respecify_idle_task(conn, graph["leaf"], author="generic", title="ordinary work") is None
    assert kb.archive_task(conn, graph["leaf"]) is False
    assert kb.delete_archived_task(conn, graph["leaf"]) is False
    assert kb.delete_task(conn, graph["leaf"]) is False
    for statement, params in (
        ("UPDATE tasks SET status='ready' WHERE id=?", (graph["leaf"],)),
        ("UPDATE tasks SET body='ordinary work' WHERE id=?", (graph["leaf"],)),
        ("DELETE FROM tasks WHERE id=?", (graph["leaf"],)),
        ("UPDATE review_remediation_handoffs SET successor_task_id='retargeted' WHERE handoff_key=?", (receipt["handoff_key"],)),
        ("DELETE FROM review_remediation_handoffs WHERE handoff_key=?", (receipt["handoff_key"],)),
    ):
        try:
            conn.execute(statement, params)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError(f"protected mutation succeeded: {statement}")
    result = kb.dispatch_once(conn, dry_run=True, max_spawn=8, board="fixture")
    assert graph["leaf"] not in {row[0] for row in result.spawned}
    assert kb.get_task(conn, graph["leaf"]).status == "blocked"
    print(json.dumps({"blocked": True, "protected_mutations": 13, "spawned": False}))
''',
    )
    assert probe == {"blocked": True, "protected_mutations": 13, "spawned": False}


def test_expired_worker_runs_and_aba_heartbeats_fail_closed(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
import time

with kb.connect_closing(db) as conn:
    standalone = make_graph(conn)
    plain = kb.create_task(
        conn, title="Lease-fenced implementation", assignee="implementer", created_by="orchestrator"
    )
    plain_run = kb.claim_task(conn, plain, claimer="dispatcher:implementer", ttl_seconds=1)
    assert plain_run is not None
    same_card = kb.create_task(
        conn, title="Lease-fenced same-card review", assignee="implementer", created_by="orchestrator"
    )
    implementation = kb.claim_task(
        conn, same_card, claimer="dispatcher:implementer:review", ttl_seconds=30
    )
    assert implementation is not None
    with active("implementer"):
        assert kb.request_review(
            conn,
            same_card,
            summary="lease-fenced review",
            reviewer="reviewer",
            expected_run_id=implementation.current_run_id,
        )
    review_run = kb.claim_review_task(
        conn, same_card, claimer="dispatcher:reviewer", ttl_seconds=1
    )
    assert review_run is not None
    expiry = int(time.time()) + 1
    with kb._review_native_mutation_authorized(conn):
        conn.execute(
            "UPDATE tasks SET claim_expires=? WHERE id=?",
            (expiry, standalone["leaf"]),
        )
    conn.execute(
        "UPDATE task_runs SET claim_expires=? WHERE id=?",
        (expiry, standalone["review_run"]),
    )
    time.sleep(2)
    with active("reviewer"):
        assert not kb.block_task(
            conn,
            standalone["leaf"],
            reason="STANDALONE_REVIEW_CHANGES_REQUESTED: expired reviewer",
            kind="dependency",
            expected_run_id=standalone["review_run"],
        )
        assert not kb.complete_task(
            conn,
            standalone["leaf"],
            summary="expired approval",
            metadata={"review_outcome": "APPROVED", "candidate_commit": head},
            expected_run_id=standalone["review_run"],
        )
        ok, _ = kb.request_changes(
            conn,
            same_card,
            reason="expired changes request",
            expected_run_id=review_run.current_run_id,
        )
        assert not ok
        assert not kb.complete_task(
            conn,
            same_card,
            summary="expired same-card approval",
            expected_run_id=review_run.current_run_id,
        )
    with active("implementer"):
        assert not kb.complete_task(
            conn,
            plain,
            summary="expired implementation",
            expected_run_id=plain_run.current_run_id,
        )
        for bad_run in (None, True, str(plain_run.current_run_id), float(plain_run.current_run_id), plain_run.current_run_id):
            assert not kb.heartbeat_worker(conn, plain, expected_run_id=bad_run)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM review_remediation_handoffs WHERE leaf_task_id=?",
        (standalone["leaf"],),
    ).fetchone()["n"] == 0
    assert kb.release_stale_claims(conn) >= 3
    second = kb.claim_task(conn, plain, claimer="dispatcher:implementer:second")
    assert second is not None and second.current_run_id != plain_run.current_run_id
    with active("implementer"):
        assert not kb.heartbeat_claim(conn, plain, claimer=second.claim_lock)
        assert not kb.heartbeat_claim(
            conn,
            plain,
            claimer=second.claim_lock,
            expected_run_id=plain_run.current_run_id,
        )
        assert kb.heartbeat_claim(
            conn,
            plain,
            claimer=second.claim_lock,
            expected_run_id=second.current_run_id,
        )
        assert not kb.heartbeat_worker(conn, plain, expected_run_id=None)
        assert not kb.heartbeat_worker(conn, plain, expected_run_id=plain_run.current_run_id)
        assert kb.heartbeat_worker(conn, plain, expected_run_id=second.current_run_id)
    print(json.dumps({"expired_transitions": "rejected", "aba": "rejected", "current": "accepted"}))
''',
    )
    assert probe == {
        "expired_transitions": "rejected",
        "aba": "rejected",
        "current": "accepted",
    }


def test_claimed_review_body_and_worktree_state_are_immutable(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
with kb.connect_closing(db) as conn:
    body_graph = make_graph(conn)
    try:
        conn.execute(
            "UPDATE tasks SET body = body || '\nuntrusted: instruction' WHERE id=?",
            (body_graph["leaf"],),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("post-claim body edit succeeded")
    with kb._review_native_mutation_authorized(conn):
        conn.execute(
            "UPDATE tasks SET body = body || '\nuntrusted: instruction' WHERE id=?",
            (body_graph["leaf"],),
        )
    with active("reviewer"):
        assert kb.block_task(
            conn,
            body_graph["leaf"],
            reason="STANDALONE_REVIEW_CHANGES_REQUESTED: mutated instructions",
            kind="dependency",
            expected_run_id=body_graph["review_run"],
        )
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM review_remediation_handoffs WHERE leaf_task_id=?",
        (body_graph["leaf"],),
    ).fetchone()["n"] == 0
    assert kb.get_task(conn, body_graph["leaf"]).status == "blocked"
    assert kb.respecify_idle_task(
        conn, body_graph["leaf"], author="generic", title="laundered work"
    ) is None

    dirty_graph = make_graph(conn)
    dirty = repo / "untracked-review-state.txt"
    dirty.write_text("not in candidate commit\n", encoding="utf-8")
    with active("reviewer"):
        assert kb.block_task(
            conn,
            dirty_graph["leaf"],
            reason="STANDALONE_REVIEW_CHANGES_REQUESTED: dirty source",
            kind="dependency",
            expected_run_id=dirty_graph["review_run"],
        )
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM review_remediation_handoffs WHERE leaf_task_id=?",
        (dirty_graph["leaf"],),
    ).fetchone()["n"] == 0
    dirty.unlink()
    print(json.dumps({"body_edit": "rejected", "receipt": "rejected", "dirty": "rejected"}))
''',
    )
    assert probe == {
        "body_edit": "rejected",
        "receipt": "rejected",
        "dirty": "rejected",
    }


def test_successor_receipt_pointer_is_mandatory_in_completion_and_dispatch(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
from hermes_cli.native_boundary import dispatcher_state_rejections

with kb.connect_closing(db) as conn:
    graph = make_graph(conn)
    receipt = reject_leaf(conn, graph, "successor receipt must be immutable")
    with active("orchestrator"):
        consumed = kb.consume_standalone_review_handoffs(conn)
    assert len(consumed) == 1
    row = conn.execute(
        "SELECT * FROM review_remediation_handoffs WHERE handoff_key=?",
        (receipt["handoff_key"],),
    ).fetchone()
    successor = row["successor_task_id"]
    assert kb.archive_task(conn, successor) is False
    assert kb.delete_task(conn, successor) is False
    assert kb.respecify_idle_task(
        conn, successor, author="generic", title="laundered successor"
    ) is None
    forged = kb.create_task(
        conn,
        title="Forged receipt target",
        body="ordinary task",
        assignee="implementer",
        created_by="orchestrator",
    )
    try:
        conn.execute(
            "UPDATE review_remediation_handoffs SET successor_task_id=? WHERE handoff_key=?",
            (forged, row["handoff_key"]),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("ordinary writer retargeted receipt")
    with kb._review_native_mutation_authorized(conn):
        conn.execute(
            "UPDATE review_remediation_handoffs SET successor_task_id=? WHERE handoff_key=?",
            (forged, row["handoff_key"]),
        )
    rejections = {task_id for task_id, reason in dispatcher_state_rejections(conn) if reason == "malformed_durable_state"}
    assert successor in rejections and forged in rejections
    claim = kb.claim_task(conn, successor, claimer="dispatcher:implementer")
    assert claim is None
    print(json.dumps({"writer": "rejected", "barrier": 2, "completion": "rejected"}))
''',
    )
    assert probe == {"writer": "rejected", "barrier": 2, "completion": "rejected"}


def test_legacy_pending_handoff_is_atomically_tombstoned_on_upgrade(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
import time

with kb.connect_closing(db) as conn:
    leaf = kb.create_task(
        conn,
        title="Legacy standalone review",
        body="legacy receipt",
        assignee="orchestrator",
        created_by="legacy-coordinator",
    )
    claimed = kb.claim_task(conn, leaf, claimer="legacy-worker")
    assert claimed is not None
    with active("orchestrator"):
        assert kb.block_task(
            conn,
            leaf,
            reason="legacy review rejection",
            kind="dependency",
            expected_run_id=claimed.current_run_id,
        )
    run_id = claimed.current_run_id

raw = sqlite3.connect(db)
raw.execute("PRAGMA foreign_keys=ON")
raw.executescript("""
DROP TABLE review_remediation_handoffs;
CREATE TABLE review_remediation_handoffs (
    handoff_key TEXT PRIMARY KEY,
    leaf_task_id TEXT NOT NULL UNIQUE,
    review_run_id INTEGER NOT NULL,
    coordinator_profile TEXT,
    implementation_task TEXT NOT NULL,
    packet_json TEXT NOT NULL,
    packet_sha256 TEXT NOT NULL,
    findings TEXT NOT NULL,
    graph_snapshot_json TEXT NOT NULL,
    frontier_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied')),
    successor_task_id TEXT,
    created_at INTEGER NOT NULL,
    consumed_at INTEGER
);
""")
raw.execute(
    "INSERT INTO review_remediation_handoffs "
    "(handoff_key, leaf_task_id, review_run_id, coordinator_profile, implementation_task, "
    "packet_json, packet_sha256, findings, graph_snapshot_json, frontier_json, status, created_at) "
    "VALUES (?, ?, ?, 'orchestrator', ?, '{}', ?, 'legacy', '{}', '{}', 'pending', ?)",
    (
        "review-remediation:v1:legacy",
        leaf,
        run_id,
        leaf,
        "0" * 64,
        int(time.time()),
    ),
)
raw.commit()
raw.close()
kb._INITIALIZED_PATHS.discard(str(db.resolve()))
kb.init_db(db)
with kb.connect_closing(db) as conn:
    row = conn.execute(
        "SELECT status, tombstone_reason, consumed_at FROM review_remediation_handoffs "
        "WHERE leaf_task_id=?",
        (leaf,),
    ).fetchone()
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? "
        "AND kind='standalone_review_handoff_tombstoned' ORDER BY id DESC LIMIT 1",
        (leaf,),
    ).fetchone()
    assert row["status"] == "applied"
    assert row["tombstone_reason"] == "legacy_receipt_missing_v2_provenance"
    assert type(row["consumed_at"]) is int and row["consumed_at"] > 0
    assert event is not None
    assert kb.consume_standalone_review_handoffs(conn) == []
    assert kb.get_task(conn, leaf).status == "blocked"
    print(json.dumps({"pending": 0, "tombstone": row["tombstone_reason"], "event": True}))
''',
    )
    assert probe == {
        "pending": 0,
        "tombstone": "legacy_receipt_missing_v2_provenance",
        "event": True,
    }


def test_successor_writers_and_terminal_shortcut_are_fail_closed(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
from hermes_cli.native_boundary import dispatcher_state_rejections

with kb.connect_closing(db) as conn:
    graph = make_graph(conn)
    reject_leaf(conn, graph, "successor must remain native")
    with active("orchestrator"):
        consumed = kb.consume_standalone_review_handoffs(conn)
    assert len(consumed) == 1
    successor = consumed[0]["successor_task_id"]
    assert kb.assign_task(conn, successor, "foreign") is False
    try:
        kb.set_branch_name(conn, successor, "foreign/retarget")
    except RuntimeError:
        pass
    else:
        raise AssertionError("branch retarget was accepted")
    try:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (successor,))
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("ordinary writer completed remediation successor")

    # Simulate corruption below the ordinary-writer boundary.  The dispatcher
    # barrier and dependency promotion must still fail closed.
    with kb._review_native_mutation_authorized(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (successor,))
    malformed = {
        str(task_id)
        for task_id, reason in dispatcher_state_rejections(conn)
        if reason == "malformed_durable_state"
    }
    assert successor in malformed
    kb.recompute_ready(conn)
    assert kb.get_task(conn, graph["child"]).status == "todo"
    dispatch = kb.dispatch_once(
        conn, dry_run=True, max_spawn=10, reconcile_orphans=False
    )
    assert graph["child"] not in {item[0] for item in dispatch.spawned}
    print(json.dumps({"successor": "rejected", "child": "gated"}))
''',
    )
    assert probe == {"successor": "rejected", "child": "gated"}


def test_review_claim_graph_body_and_gc_evidence_are_immutable(tmp_path):
    probe = _run_native(
        tmp_path,
        _COMMON
        + r'''
with kb.connect_closing(db) as conn:
    try:
        conn.execute(
            "INSERT INTO review_native_mutation_authorization(token) VALUES (1)"
        )
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("ordinary writer installed native mutation authorization")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM review_native_mutation_authorization"
    ).fetchone()["n"] == 0
    graph = make_graph(conn)
    try:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (graph["leaf"],))
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("claimed standalone review was directly completed")
    try:
        conn.execute(
            "DELETE FROM task_links WHERE parent_id=? AND child_id=?",
            (graph["leaf"], graph["child"]),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("claimed review frontier was detached")

    with active("reviewer"):
        assert kb.complete_task(
            conn,
            graph["leaf"],
            summary="exact packet approved",
            metadata={"review_outcome": "APPROVED", "candidate_commit": head},
            expected_run_id=graph["review_run"],
        )
    with kb._review_native_mutation_authorized(conn):
        conn.execute(
            "UPDATE task_events SET created_at=1 WHERE task_id=?",
            (graph["leaf"],),
        )
    kb.gc_events(conn, older_than_seconds=1)
    durable_kinds = {
        row["kind"]
        for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (graph["leaf"],)
        ).fetchall()
    }
    assert "standalone_review_packet_claimed" in durable_kinds
    assert "standalone_review_approved" in durable_kinds
    for sql, params in (
        ("UPDATE tasks SET title='laundered' WHERE id=?", (graph["leaf"],)),
        ("UPDATE tasks SET status='ready' WHERE id=?", (graph["leaf"],)),
    ):
        try:
            conn.execute(sql, params)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("retained approval evidence was bypassed")

    same_card = kb.create_task(
        conn,
        title="same-card immutable packet",
        body="review this exact body",
        assignee="implementer",
        created_by="orchestrator",
        workspace_kind="dir",
        workspace_path=str(repo),
    )
    implementation = kb.claim_task(conn, same_card, claimer="dispatcher:implementer")
    assert implementation is not None
    with active("implementer"):
        assert kb.request_review(
            conn,
            same_card,
            summary="review exact body",
            metadata={"candidate_commit": head},
            reviewer="reviewer",
            expected_run_id=implementation.current_run_id,
        )
    review = kb.claim_review_task(conn, same_card, claimer="dispatcher:reviewer")
    assert review is not None
    try:
        conn.execute(
            "UPDATE tasks SET title='dashboard edit', body='tampered' WHERE id=?",
            (same_card,),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("dashboard-equivalent edit bypassed review receipt")
    with kb._review_native_mutation_authorized(conn):
        conn.execute("UPDATE tasks SET body='tampered' WHERE id=?", (same_card,))
    with active("reviewer"):
        assert not kb.complete_task(
            conn,
            same_card,
            summary="must reject changed body",
            metadata={"candidate_commit": head},
            expected_run_id=review.current_run_id,
        )
    print(json.dumps({"standalone": "durable", "same_card": "bound"}))
''',
    )
    assert probe == {"standalone": "durable", "same_card": "bound"}
