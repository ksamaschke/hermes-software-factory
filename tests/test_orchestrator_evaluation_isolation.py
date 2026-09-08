"""Causal isolation tests for the native orchestrator evaluation harness."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "local-variant" / "orchestrator_decision_contract.py"
contract_spec = importlib.util.spec_from_file_location(
    "orchestrator_decision_contract", CONTRACT_PATH
)
assert contract_spec is not None and contract_spec.loader is not None
contract = importlib.util.module_from_spec(contract_spec)
sys.modules[contract_spec.name] = contract
contract_spec.loader.exec_module(contract)

MODULE_PATH = ROOT / "local-variant" / "orchestrator_llm_evaluation.py"
spec = importlib.util.spec_from_file_location(
    "orchestrator_llm_evaluation", MODULE_PATH
)
assert spec is not None and spec.loader is not None
evaluation = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = evaluation
spec.loader.exec_module(evaluation)


def _contaminated_environment(live_board: Path) -> dict[str, str]:
    parent = dict(os.environ)
    parent.update(
        {
            "HERMES_HOME": "/operator/hermes",
            "HOME": "/operator",
            "XDG_CONFIG_HOME": "/operator/config",
            "HERMES_PROFILE": "implementer",
            "HERMES_PROFILE_NAME": "implementer",
            "HERMES_KANBAN_TASK": "t_live",
            "HERMES_KANBAN_RUN_ID": "run-live",
            "HERMES_KANBAN_DB": str(live_board),
            "HERMES_KANBAN_BOARD": "production",
            "HERMES_KANBAN_HOME": str(live_board.parent),
            "HERMES_KANBAN_WORKSPACES_ROOT": "/operator/workspaces",
            "HERMES_KANBAN_ATTACHMENTS_ROOT": "/operator/attachments",
            "HERMES_KANBAN_CLAIM_LOCK": "live-lock",
            "HERMES_KANBAN_BRANCH": "live-branch",
            "HERMES_KANBAN_GOAL_MODE": "1",
            "HERMES_KANBAN_GOAL_MAX_TURNS": "10",
            "HERMES_SESSION_ID": "session-live",
            "HERMES_SESSION_SOURCE": "kanban",
            "HERMES_SESSION_PROFILE": "implementer",
            "HERMES_CRON_SESSION": "1",
            "HERMES_CRON_AUTO_DELIVER_CHAT_ID": "live-chat",
            "HERMES_YOLO_MODE": "1",
            "HERMES_ACCEPT_HOOKS": "1",
            "FACTORY_EVAL_AUTH_FILE": "/operator/auth.json",
        }
    )
    return parent


def test_contaminated_parent_is_rewritten_to_a_private_board_and_profile(
    tmp_path: Path,
):
    live_board = tmp_path / "live" / "live-kanban.db"
    live_board.parent.mkdir(parents=True)
    live_board.write_bytes(b"live-board")
    profile = tmp_path / "eval" / "profile"
    profile.mkdir(parents=True)
    isolated_board = tmp_path / "eval" / "isolated-kanban.db"

    child = evaluation.build_isolated_environment(
        _contaminated_environment(live_board), profile, isolated_board
    )

    assert child["HERMES_HOME"] == str(profile.resolve())
    assert child["HOME"] == str(profile.resolve())
    assert child["XDG_CONFIG_HOME"] == str((profile / "xdg").resolve())
    assert child["HERMES_KANBAN_HOME"] == str(isolated_board.parent.resolve())
    assert child["HERMES_KANBAN_DB"] == str(isolated_board.resolve())
    assert child["HERMES_KANBAN_DB"] != str(live_board.resolve())
    assert child["HERMES_KANBAN_BOARD"] == "default"

    for key in (
        "HERMES_PROFILE",
        "HERMES_PROFILE_NAME",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_BRANCH",
        "HERMES_KANBAN_GOAL_MODE",
        "HERMES_KANBAN_GOAL_MAX_TURNS",
        "HERMES_SESSION_ID",
        "HERMES_SESSION_SOURCE",
        "HERMES_SESSION_PROFILE",
        "HERMES_CRON_SESSION",
        "HERMES_CRON_AUTO_DELIVER_CHAT_ID",
        "HERMES_YOLO_MODE",
        "HERMES_ACCEPT_HOOKS",
        "FACTORY_EVAL_AUTH_FILE",
    ):
        assert key not in child


def test_board_path_inside_inherited_authority_is_rejected(tmp_path: Path):
    live_board = tmp_path / "live" / "live-kanban.db"
    live_board.parent.mkdir(parents=True)
    profile = tmp_path / "eval" / "profile"
    profile.mkdir(parents=True)

    with pytest.raises(
        evaluation.NativeEvaluationUnavailable,
        match="overlaps inherited board authority",
    ):
        evaluation.build_isolated_environment(
            _contaminated_environment(live_board),
            profile,
            live_board.parent / "child" / "isolated-kanban.db",
        )


def test_board_path_inside_inherited_workspace_or_attachment_is_rejected(
    tmp_path: Path,
):
    live_board = tmp_path / "live" / "live-kanban.db"
    live_board.parent.mkdir(parents=True)
    profile = tmp_path / "eval" / "profile"
    profile.mkdir(parents=True)

    for key in ("HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ATTACHMENTS_ROOT"):
        parent = _contaminated_environment(live_board)
        inherited_root = tmp_path / key.removeprefix("HERMES_KANBAN_").lower()
        inherited_root.mkdir()
        parent[key] = str(inherited_root)
        with pytest.raises(
            evaluation.NativeEvaluationUnavailable,
            match="overlaps inherited board authority",
        ):
            evaluation.build_isolated_environment(
                parent,
                profile,
                inherited_root / "child" / "isolated-kanban.db",
            )


def test_native_child_write_cannot_reach_contaminated_live_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    live_board = tmp_path / "live" / "live-kanban.db"
    live_board.parent.mkdir(parents=True)
    live_board.write_bytes(b"live-board")
    profile = tmp_path / "profile"
    profile.mkdir()
    isolated_board = tmp_path / "isolated" / "kanban.db"
    captured: dict[str, Any] = {}

    def fake_bounded(command, *, cwd, env, timeout):
        captured.update(command=command, cwd=cwd, env=env, timeout=timeout)
        # A faulty child would write its event through this path. The assertion
        # below proves that path is isolated before any native model is used.
        Path(env["HERMES_KANBAN_DB"]).parent.mkdir(parents=True, exist_ok=True)
        Path(env["HERMES_KANBAN_DB"]).write_bytes(b"child-board")
        return 0, '{"diagnose": {}}', ""

    monkeypatch.setattr(os, "environ", _contaminated_environment(live_board))
    monkeypatch.setattr(evaluation, "_run_bounded_process", fake_bounded)

    model = evaluation.HermesSubprocessModel(
        profile=profile,
        state_path=tmp_path / "state.json",
        trace_path=tmp_path / "trace.jsonl",
        board_path=isolated_board,
        hermes="hermes",
        model="test-model",
        provider="test-provider",
        run_budget=30,
    )
    assert model.complete("typed prompt", {}) == {"diagnose": {}}

    assert live_board.read_bytes() == b"live-board"
    assert captured["cwd"] == profile
    assert captured["env"]["HERMES_KANBAN_DB"] == str(isolated_board.resolve())
    assert captured["env"].get("HERMES_KANBAN_TASK") is None
    assert captured["env"].get("HERMES_KANBAN_RUN_ID") is None
    assert "--yolo" not in captured["command"]
    assert "--accept-hooks" not in captured["command"]
    assert captured["command"][captured["command"].index("--toolsets") + 1] == "fixture"


def test_profile_catalog_is_explicitly_fixture_only(tmp_path: Path):
    root = tmp_path / "evaluation"
    root.mkdir()
    profile = evaluation._write_profile(
        root,
        model="test-model",
        provider="test-provider",
        state_path=root / "state.json",
        trace_path=root / "trace.jsonl",
    )

    config = json.loads((profile / "config.yaml").read_text(encoding="utf-8"))
    assert config["platform_toolsets"] == {"cli": ["fixture"]}
    assert set(config["mcp_servers"]) == {"fixture"}
    assert config["include_default_mcp_servers"] is False
    evaluation.verify_fixture_only_profile(profile)

    config["mcp_servers"]["unexpected"] = {"enabled": True}
    (profile / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(evaluation.NativeEvaluationUnavailable, match="fixture-only"):
        evaluation.verify_fixture_only_profile(profile)


def test_native_summary_redacts_model_and_provider_values():
    summary = evaluation.NativeEvaluation(
        "https://user:pw@example.invalid", "token=SECRET", ()
    ).as_dict()
    encoded = json.dumps(summary)
    assert "user:pw" not in encoded
    assert "token=SECRET" not in encoded
    assert "[REDACTED]" in encoded


def test_directory_snapshot_detects_new_nested_files(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = {"HERMES_KANBAN_WORKSPACES_ROOT": str(workspace)}
    snapshot = evaluation.snapshot_board_state(parent)
    (workspace / "foreign-write.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="touched inherited board state"
    ):
        evaluation.verify_board_state_unchanged(snapshot)


def test_bounded_process_rejects_large_output_before_return(tmp_path: Path):
    with pytest.raises(contract.ContractViolation, match="output exceeds"):
        evaluation._run_bounded_process(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * 300000)",
            ],
            cwd=tmp_path,
            env={},
            timeout=10,
        )


def test_malformed_trace_entry_is_a_contract_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    profile = tmp_path / "profile"
    profile.mkdir()
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text("[]\n", encoding="utf-8")

    def fake_bounded(*_args: Any, **_kwargs: Any):
        return 0, "{}", ""

    monkeypatch.setattr(evaluation, "_run_bounded_process", fake_bounded)
    model = evaluation.HermesSubprocessModel(
        profile=profile,
        state_path=tmp_path / "state.json",
        trace_path=trace_path,
        board_path=tmp_path / "isolated" / "kanban.db",
        hermes="hermes",
        model="test-model",
        provider="test-provider",
        run_budget=30,
        parent_env={},
    )

    with pytest.raises(contract.ContractViolation, match="not an object"):
        model.complete("typed prompt", {})


def test_snapshot_detects_mutation_of_inherited_home_root(tmp_path: Path):
    home = tmp_path / "kanban-home"
    home.mkdir()
    snapshot = evaluation.snapshot_board_state({"HERMES_KANBAN_HOME": str(home)})
    (home / "foreign-root-write").write_text("unexpected", encoding="utf-8")
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="touched inherited board state"
    ):
        evaluation.verify_board_state_unchanged(snapshot)


def test_snapshot_detects_mutation_through_nested_symlink_target(tmp_path: Path):
    workspace = tmp_path / "workspace"
    target = tmp_path / "foreign-target"
    workspace.mkdir()
    target.mkdir()
    (workspace / "linked").symlink_to(target, target_is_directory=True)
    snapshot = evaluation.snapshot_board_state(
        {"HERMES_KANBAN_WORKSPACES_ROOT": str(workspace)}
    )
    (target / "foreign-write.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="touched inherited board state"
    ):
        evaluation.verify_board_state_unchanged(snapshot)


def test_profile_overlap_is_rejected_before_prompt_write(tmp_path: Path):
    live_board = tmp_path / "live" / "kanban.db"
    live_board.parent.mkdir(parents=True)
    profile = live_board.parent / "profile"
    profile.mkdir()
    model = evaluation.HermesSubprocessModel(
        profile=profile,
        state_path=tmp_path / "state.json",
        trace_path=tmp_path / "trace.jsonl",
        board_path=tmp_path / "isolated" / "kanban.db",
        hermes="hermes",
        model="test-model",
        provider="test-provider",
        run_budget=30,
        parent_env=_contaminated_environment(live_board),
    )
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable,
        match="overlaps inherited board authority",
    ):
        model.complete("typed prompt", {})
    assert not (profile / "decision-prompt.txt").exists()


def test_trace_output_inside_inherited_root_is_rejected_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    live_board = tmp_path / "live" / "kanban.db"
    live_board.parent.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(live_board.parent))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live_board))
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="trace output overlaps"
    ):
        evaluation.run_native_evaluation(
            hermes=sys.executable,
            trace_output=str(live_board),
        )
    assert not live_board.exists()


def test_bounded_process_closes_streams_without_parent_stderr_noise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    result = evaluation._run_bounded_process(
        [sys.executable, "-c", "print(123)"],
        cwd=tmp_path,
        env={},
        timeout=10,
    )
    assert result[:2] == (0, "123\n")


def test_native_summary_redacts_case_payloads_and_compound_json_keys():
    summary = evaluation.NativeEvaluation(
        "safe-model",
        "safe-provider",
        ({"apiKey": "CASE-SECRET", "nested": {"passwordHash": "PW-SECRET"}},),
    ).as_dict()
    encoded = json.dumps(summary)
    assert "CASE-SECRET" not in encoded
    assert "PW-SECRET" not in encoded
    assert summary["case_count"] == 1


def test_response_size_is_bounded_before_json_decode():
    with pytest.raises(contract.ContractViolation, match="response exceeds"):
        evaluation._parse_json_response("{" + "x" * evaluation._MAX_NATIVE_RESPONSE_CHARS)


def test_root_symlink_retarget_is_detected(tmp_path: Path):
    root_link = tmp_path / "workspace-link"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    root_link.symlink_to(first, target_is_directory=True)
    snapshot = evaluation.snapshot_board_state(
        {"HERMES_KANBAN_WORKSPACES_ROOT": str(root_link)}
    )
    root_link.unlink()
    root_link.symlink_to(second, target_is_directory=True)
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="touched inherited board state"
    ):
        evaluation.verify_board_state_unchanged(snapshot)


def test_snapshot_enforces_cumulative_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one").write_bytes(b"123456")
    (workspace / "two").write_bytes(b"abcdef")
    monkeypatch.setattr(evaluation, "_MAX_SNAPSHOT_TOTAL_BYTES", 10)
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="bytes exceed snapshot bound"
    ):
        evaluation.snapshot_board_state(
            {"HERMES_KANBAN_WORKSPACES_ROOT": str(workspace)}
        )


def test_prompt_atomic_replace_does_not_mutate_hardlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    profile = tmp_path / "profile"
    profile.mkdir()
    protected = tmp_path / "protected.txt"
    protected.write_text("protected", encoding="utf-8")
    query = profile / "decision-prompt.txt"
    query.hardlink_to(protected)

    def fake_bounded(*_args: Any, **_kwargs: Any):
        return 0, "{}", ""

    monkeypatch.setattr(evaluation, "_run_bounded_process", fake_bounded)
    model = evaluation.HermesSubprocessModel(
        profile=profile,
        state_path=tmp_path / "state.json",
        trace_path=tmp_path / "trace.jsonl",
        board_path=tmp_path / "isolated" / "kanban.db",
        hermes="hermes",
        model="test-model",
        provider="test-provider",
        run_budget=30,
        parent_env={},
    )
    assert model.complete("typed prompt", {}) == {}
    assert protected.read_text(encoding="utf-8") == "protected"
    assert query.read_text(encoding="utf-8").startswith("typed prompt")


def test_combined_process_output_is_bounded(tmp_path: Path):
    code = "import sys; sys.stdout.write('o' * 180000); sys.stderr.write('e' * 100000)"
    with pytest.raises(contract.ContractViolation, match="combined native Hermes output"):
        evaluation._run_bounded_process(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env={},
            timeout=10,
        )


def test_bounded_process_kills_descendants_on_timeout(tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    code = (
        "import pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
    )
    with pytest.raises(Exception):
        evaluation._run_bounded_process(
            [sys.executable, "-c", code, str(pid_file)],
            cwd=tmp_path,
            env={},
            timeout=1,
        )
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(20):
        try:
            state = Path(f"/proc/{child_pid}/stat").read_text(encoding="utf-8").split()[2]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        import time

        time.sleep(0.05)
    else:
        pytest.fail("bounded process left a descendant alive")


def test_native_failure_has_bounded_secret_safe_recovery_metadata():
    code, retryable, action, diagnostic = evaluation._classify_native_failure(
        1,
        "Unauthorized token=TOP-SECRET at /home/ksamaschke/private/auth.json",
    )
    assert code == "authentication"
    assert retryable is False
    assert "refresh" in action
    assert "TOP-SECRET" not in diagnostic
    assert "/home/ksamaschke" not in diagnostic
    code, retryable, _action, diagnostic = evaluation._classify_native_failure(
        1, "", "authentication failed token=TOP-SECRET"
    )
    assert code == "authentication"
    assert not retryable
    assert "TOP-SECRET" not in diagnostic
    failure = evaluation.NativeEvaluationUnavailable(
        "child token=SECRET",
        failure_code="native_exit_1",
        recovery_action="inspect password=PW",
        diagnostic="apiKey=KEY",
        attempts=2,
    )
    rendered = failure.as_dict()
    assert "SECRET" not in str(rendered)
    assert "PW" not in str(rendered)
    assert "KEY" not in str(rendered)


def test_native_subprocess_retries_only_retryable_failure(monkeypatch, tmp_path):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            return 1, "", "provider temporarily unavailable"
        return 0, '{"decision":"hold"}', ""

    monkeypatch.setattr(
        evaluation,
        "build_isolated_environment",
        lambda parent_env, profile, board_path: {},
    )
    monkeypatch.setattr(evaluation, "_run_bounded_process", fake_run)
    profile = tmp_path / "profile"
    profile.mkdir()
    model = evaluation.HermesSubprocessModel(
        profile,
        tmp_path / "state.json",
        tmp_path / "missing-trace.jsonl",
        tmp_path / "board.db",
        "/bin/hermes",
        "model",
        "provider",
        1,
        parent_env={},
    )

    assert model.complete("prompt", {}) == {"decision": "hold"}
    assert len(calls) == 2
