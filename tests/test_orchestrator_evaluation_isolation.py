"""Causal isolation tests for the native orchestrator evaluation harness."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
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
    live_board = tmp_path / "live-kanban.db"
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


def test_native_child_write_cannot_reach_contaminated_live_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    live_board = tmp_path / "live-kanban.db"
    live_board.write_bytes(b"live-board")
    profile = tmp_path / "profile"
    profile.mkdir()
    isolated_board = tmp_path / "isolated" / "kanban.db"
    captured: dict[str, Any] = {}

    def fake_run(command, *, cwd, env, capture_output, text, timeout, check):
        captured.update(
            command=command,
            cwd=cwd,
            env=env,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
        )
        # A faulty child would write its event through this path. The assertion
        # below proves that path is isolated before any native model is used.
        Path(env["HERMES_KANBAN_DB"]).parent.mkdir(parents=True, exist_ok=True)
        Path(env["HERMES_KANBAN_DB"]).write_bytes(b"child-board")
        return SimpleNamespace(
            returncode=0,
            stdout='{"diagnose": {}}',
            stderr="",
        )

    monkeypatch.setattr(os, "environ", _contaminated_environment(live_board))
    monkeypatch.setattr(evaluation.subprocess, "run", fake_run)

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
