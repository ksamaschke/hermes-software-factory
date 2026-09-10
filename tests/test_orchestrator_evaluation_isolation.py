"""Causal isolation tests for the native orchestrator evaluation harness."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
REAL_RESOLVE_RUNTIME_CREDENTIALS = evaluation._resolve_runtime_credentials


@pytest.fixture(autouse=True)
def _native_provider_registry(monkeypatch: pytest.MonkeyPatch):
    auth_module = ModuleType("hermes_cli.auth")
    auth_module.PROVIDER_REGISTRY = {
        "openai-codex": SimpleNamespace(auth_type="oauth_pkce"),
        "api-provider": SimpleNamespace(auth_type="api_key"),
        "external-provider": SimpleNamespace(auth_type="external_process"),
    }
    hermes_module = ModuleType("hermes_cli")
    hermes_module.__path__ = []
    hermes_module.auth = auth_module
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth_module)
    providers_module = ModuleType("hermes_cli.providers")
    provider_definitions = {
        "openai-codex": SimpleNamespace(id="openai-codex", auth_type="oauth_pkce"),
        "api-provider": SimpleNamespace(id="api-provider", auth_type="api_key"),
        "external-provider": SimpleNamespace(
            id="external-provider", auth_type="external_process"
        ),
    }
    provider_aliases = {"codex": "openai-codex", "openai_codex": "openai-codex"}

    def normalize_provider(name: str) -> str:
        return provider_aliases.get(name.casefold(), name.casefold())

    def get_provider(name: str, *, allow_network: bool = True):
        assert allow_network is False
        return provider_definitions.get(name)

    providers_module.normalize_provider = normalize_provider
    providers_module.get_provider = get_provider
    hermes_module.providers = providers_module
    monkeypatch.setitem(sys.modules, "hermes_cli.providers", providers_module)
    monkeypatch.setattr(evaluation, "_resolve_runtime_credentials", lambda *_args: True)


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


@pytest.mark.parametrize(
    "document, leaked_marker",
    [
        ("{not-json", None),
        ({"providers": {"openai-codex": {}}}, None),
        (
            {
                "providers": {
                    "openai-codex": {"tokens": {"access_token": "fixture-access"}}
                }
            },
            "fixture-access",
        ),
    ],
)
def test_auth_validation_rejects_malformed_or_unsupported_input_before_copy(
    tmp_path: Path, document: Any, leaked_marker: str | None
):
    source = tmp_path / "source-auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    source.write_text(
        document if isinstance(document, str) else json.dumps(document),
        encoding="utf-8",
    )

    with pytest.raises(evaluation.NativeEvaluationUnavailable) as failure:
        evaluation._copy_auth(source, profile, provider="openai-codex")

    assert not (profile / "auth.json").exists()
    if leaked_marker is not None:
        assert leaked_marker not in str(failure.value)


def test_auth_validation_returns_verified_status_only_for_provider_shape(
    tmp_path: Path,
):
    document = {
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": {
                    "access_token": "fixture-access",
                    "refresh_token": "fixture-refresh",
                }
            }
        },
    }
    source = tmp_path / "source-auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    source.write_text(json.dumps(document), encoding="utf-8")

    assert (
        evaluation._copy_auth(
            source,
            profile,
            provider="openai-codex",
            preflight=lambda *_args: None,
        )
        is True
    )
    assert json.loads((profile / "auth.json").read_text(encoding="utf-8")) == document
    assert (profile / "auth.json").stat().st_mode & 0o077 == 0


def test_auth_copy_uses_the_exact_bytes_from_the_validated_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original = {
        "providers": {
            "openai-codex": {
                "tokens": {
                    "access_token": "original-access",
                    "refresh_token": "original-refresh",
                }
            }
        }
    }
    replacement = {
        "providers": {
            "openai-codex": {
                "tokens": {
                    "access_token": "replacement-access",
                    "refresh_token": "replacement-refresh",
                }
            }
        }
    }
    source = tmp_path / "source-auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    source.write_text(json.dumps(original), encoding="utf-8")
    real_validate = evaluation._validate_auth_file

    def validate_then_replace(*args: Any, **kwargs: Any):
        validated = real_validate(*args, **kwargs)
        source.write_text(json.dumps(replacement), encoding="utf-8")
        return validated

    monkeypatch.setattr(evaluation, "_validate_auth_file", validate_then_replace)
    assert (
        evaluation._copy_auth(
            source,
            profile,
            provider="openai-codex",
            preflight=lambda *_args: None,
        )
        is True
    )
    assert json.loads((profile / "auth.json").read_text(encoding="utf-8")) == original


def test_auth_source_rejects_a_symlinked_parent_component(tmp_path: Path):
    source_parent = tmp_path / "real-source"
    source_parent.mkdir()
    source = source_parent / "auth.json"
    source.write_text(
        json.dumps(
            {
                "providers": {
                    "openai-codex": {
                        "tokens": {
                            "access_token": "fixture-access",
                            "refresh_token": "fixture-refresh",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    linked_parent = tmp_path / "linked-source"
    linked_parent.symlink_to(source_parent, target_is_directory=True)
    profile = tmp_path / "profile"
    profile.mkdir()

    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="symlink|without symbolic links"
    ):
        evaluation._copy_auth(
            linked_parent / "auth.json",
            profile,
            provider="openai-codex",
            preflight=lambda *_args: None,
        )
    assert not (profile / "auth.json").exists()


def test_auth_gate_resolves_provider_metadata_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source-auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    source.write_text(
        json.dumps({"providers": {"api-provider": {"api_key": "fixture-key"}}}),
        encoding="utf-8",
    )
    calls = 0
    real_resolution = evaluation._provider_resolution

    def counted_resolution(provider: str):
        nonlocal calls
        calls += 1
        return real_resolution(provider)

    monkeypatch.setattr(evaluation, "_provider_resolution", counted_resolution)
    assert (
        evaluation._copy_auth(
            source,
            profile,
            provider="api-provider",
            preflight=lambda *_args: None,
        )
        is True
    )
    assert calls == 1


def test_provider_preflight_temporarily_removes_all_fixture_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "evaluation"
    root.mkdir()
    state_path = root / "fixture-state.json"
    trace_path = root / "fixture-trace.jsonl"
    profile = evaluation._write_profile(
        root,
        model="unit-model",
        provider="openai-codex",
        state_path=state_path,
        trace_path=trace_path,
    )
    original_text = (profile / "config.yaml").read_text(encoding="utf-8")
    observed = {}

    def fake_runner(command, *, cwd, env, timeout):
        del command, cwd, env, timeout
        observed.update(
            json.loads((profile / "config.yaml").read_text(encoding="utf-8"))
        )
        return 0, "healthy", ""

    monkeypatch.setattr(evaluation, "_run_bounded_process", fake_runner)
    evaluation._native_provider_preflight(
        "openai-codex",
        profile,
        hermes="hermes",
        model="unit-model",
        parent_env={},
        run_budget=30,
    )

    assert observed["platform_toolsets"] == {"cli": []}
    assert observed["include_default_mcp_servers"] is False
    assert observed["mcp_servers"] == {}
    assert observed["agent"]["max_turns"] == 1
    assert (profile / "config.yaml").read_text(encoding="utf-8") == original_text


def test_auth_provider_alias_preserves_oauth_refresh_requirement(tmp_path: Path):
    document = {
        "providers": {
            "openai-codex": {
                "tokens": {
                    "access_token": "fixture-access",
                    "refresh_token": "fixture-refresh",
                }
            }
        }
    }
    source = tmp_path / "source-auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    source.write_text(json.dumps(document), encoding="utf-8")

    assert (
        evaluation._copy_auth(
            source,
            profile,
            provider="codex",
            preflight=lambda *_args: None,
        )
        is True
    )


def test_auth_provider_registry_accepts_authoritative_api_key_provider(
    tmp_path: Path,
):
    document = {
        "providers": {"api-provider": {"api_key": "fixture-key"}},
    }
    source = tmp_path / "source-auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    source.write_text(json.dumps(document), encoding="utf-8")

    assert (
        evaluation._copy_auth(
            source,
            profile,
            provider="api-provider",
            preflight=lambda *_args: None,
        )
        is True
    )


def test_auth_provider_registry_rejects_unknown_provider_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source-auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    source.write_text(
        json.dumps({"providers": {"not-a-provider": {"api_key": "fixture-key"}}}),
        encoding="utf-8",
    )
    opens: list[tuple[Any, ...]] = []
    real_open = evaluation.os.open

    def observed_open(*args: Any, **kwargs: Any):
        opens.append((args, kwargs))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(evaluation.os, "open", observed_open)
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="unsupported native provider"
    ):
        evaluation._copy_auth(source, profile, provider="not-a-provider")
    assert opens == []
    assert not (profile / "auth.json").exists()


def test_auth_provider_registry_unavailability_fails_closed_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source-auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    source.write_text(
        json.dumps(
            {
                "providers": {
                    "openai-codex": {
                        "tokens": {
                            "access_token": "fixture-access",
                            "refresh_token": "fixture-refresh",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    def unavailable():
        raise ImportError("provider registry unavailable")

    monkeypatch.setattr(evaluation, "_load_authoritative_provider_layer", unavailable)
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="provider registry"
    ):
        evaluation._copy_auth(source, profile, provider="openai-codex")
    assert not (profile / "auth.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_auth_source_rejects_links_and_special_files_before_copy_or_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
):
    target = tmp_path / "target-auth.json"
    target.write_text(
        json.dumps(
            {
                "providers": {
                    "openai-codex": {
                        "tokens": {
                            "access_token": "fixture-access",
                            "refresh_token": "fixture-refresh",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "source-auth.json"
    if kind == "symlink":
        source.symlink_to(target)
    else:
        os.mkfifo(source)

    copy_calls: list[tuple[Any, ...]] = []
    case_calls: list[tuple[Any, ...]] = []

    def forbidden_copy(*args: Any, **kwargs: Any) -> None:
        copy_calls.append((args, kwargs))

    def forbidden_cases(*args: Any, **kwargs: Any):
        case_calls.append((args, kwargs))
        raise AssertionError("invalid auth reached case construction")

    monkeypatch.setattr(evaluation, "_atomic_write_text", forbidden_copy)
    monkeypatch.setattr(evaluation, "build_synthetic_cases", forbidden_cases)
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="regular readable file"
    ):
        evaluation.run_native_evaluation(
            hermes=sys.executable,
            auth_file=str(source),
            run_budget=30,
        )
    assert copy_calls == []
    assert case_calls == []


def test_auth_regular_file_bound_rejects_before_copy(tmp_path: Path):
    source = tmp_path / "oversized-auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    source.write_bytes(b"x" * (evaluation._MAX_AUTH_FILE_BYTES + 1))

    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="auth file exceeds its bound"
    ):
        evaluation._copy_auth(source, profile, provider="api-provider")
    assert not (profile / "auth.json").exists()


def test_invalid_auth_aborts_before_case_construction_or_model_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "invalid-auth.json"
    source.write_text("{not-json", encoding="utf-8")
    calls: list[tuple[Any, ...]] = []

    def forbidden_case_builder(*args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        raise AssertionError("invalid auth reached case construction")

    monkeypatch.setattr(evaluation, "build_synthetic_cases", forbidden_case_builder)
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="auth JSON is malformed"
    ):
        evaluation.run_native_evaluation(
            hermes=sys.executable,
            auth_file=str(source),
            run_budget=30,
        )
    assert calls == []


def test_fixture_builders_do_not_forge_credentials_verified():
    unverified = evaluation.build_synthetic_cases(
        "unverified-builder", credentials_verified=False
    )
    admission_case = unverified[1]
    assert admission_case.context.execution.credentials_verified is False
    with pytest.raises(
        contract.ContractViolation, match="admission requires verified credentials"
    ):
        contract._validate_admission_guards(
            admission_case.context,
            admission_case.state["live"],
            admission_case.state["source"],
        )

    verified = evaluation.build_synthetic_cases(
        "verified-builder", credentials_verified=True
    )
    assert verified[1].context.execution.credentials_verified is True


def test_fixture_context_materializes_blocker_once_without_truthiness():
    class SinglePassBlocker(Mapping):
        def __init__(self):
            self.iterations = 0
            self._values = {
                "fingerprint": "stable:blocker",
                "previous_fingerprint": "stable:blocker",
                "occurrences": 3,
                "resolved": False,
            }

        def __bool__(self):
            raise AssertionError("blocker truthiness must not be observed")

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("blocker mapping was consumed twice")
            return iter(self._values)

        def __len__(self):
            return len(self._values)

        def __getitem__(self, key):
            return self._values[key]

    blocker = SinglePassBlocker()
    with pytest.raises(contract.ContractViolation, match="unsupported value"):
        evaluation._fixture_context("single-pass-blocker", blocker=blocker)
    assert blocker.iterations == 0


@pytest.mark.parametrize(
    ("blocker", "message"),
    [
        ({"occurrences": True}, "occurrences"),
        ({"occurrences": "3"}, "occurrences"),
        ({"resolved": "false"}, "resolved"),
        ({"resolved": 0}, "resolved"),
    ],
)
def test_fixture_context_rejects_coerced_blocker_scalars(blocker, message):
    with pytest.raises(contract.ContractViolation, match=message):
        evaluation._fixture_context("typed-blocker", blocker=blocker)


def test_case_state_snapshots_optional_containers_without_truthiness():
    class SinglePassSource(Mapping):
        def __init__(self):
            self.iterations = 0
            self._values = {"source_state": "open", "artifact_state": "ready"}

        def __bool__(self):
            raise AssertionError("source truthiness must not be observed")

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("source mapping was consumed twice")
            return iter(self._values)

        def __len__(self):
            return len(self._values)

        def __getitem__(self, key):
            return self._values[key]

    class HostileList(list):
        def __bool__(self):
            raise AssertionError("optional list truthiness must not be observed")

        def __iter__(self):
            raise AssertionError("subclass iterator must not be used")

    context = evaluation._fixture_context("single-pass-state")
    source = SinglePassSource()
    with pytest.raises(contract.ContractViolation, match="unsupported value"):
        evaluation._case_state(
            context,
            "hold_missing_capability",
            source=source,
            ready=[{"task_id": "ready-task"}],
            missing=["missing-capability"],
        )
    assert source.iterations == 0

    with pytest.raises(contract.ContractViolation, match="exact dict, list, or tuple"):
        evaluation._case_state(
            context,
            "hold_missing_capability",
            source={"source_state": "open", "artifact_state": "ready"},
            ready=HostileList([{"task_id": "ready-task"}]),
            missing=["missing-capability"],
        )

    with pytest.raises(contract.ContractViolation, match="reserved or unexpected"):
        evaluation._case_state(
            context,
            "hold",
            source={
                "source_state": "open",
                "artifact_state": "ready",
                "source_key": "foreign-source",
            },
        )


def test_fixture_store_rejects_completed_admit_before_readback_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    case = evaluation.build_synthetic_cases(
        "completed-store", credentials_verified=True
    )[1]
    state = json.loads(json.dumps(case.state))
    state["live"]["existing_action"] = {
        "status": "completed",
        "task_id": "completed-store-task",
        "current_run_id": None,
        "source_key": case.context.source_item.canonical_key,
        "phase": case.context.phase,
        "input_identity": case.context.input_identity,
        "semantic_lane": case.context.semantic_lane,
    }
    state_path = tmp_path / "state.json"
    trace_path = tmp_path / "trace.jsonl"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    store = evaluation._FixtureStore(state_path, trace_path)
    key = store.read_action_key("admit", case.context.execution.task_id)[
        "idempotency_key"
    ]
    readback_calls: list[tuple[Any, ...]] = []

    def unexpected_readback(*args: Any, **kwargs: Any):
        readback_calls.append((args, kwargs))
        raise AssertionError("completed admit allocated a fixture readback")

    monkeypatch.setattr(evaluation, "simulated_action_readback", unexpected_readback)
    with pytest.raises(
        contract.ContractViolation, match="completed existing action is terminal"
    ):
        store.propose_action("admit", key, case.context.execution.task_id)

    assert readback_calls == []
    trace = trace_path.read_text(encoding="utf-8")
    assert "propose_action" not in trace
    assert "read_action_readback" not in trace


def test_fixture_store_rechecks_live_state_after_proposal_before_admission_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    case = evaluation.build_synthetic_cases(
        "completed-store-race", credentials_verified=True
    )[1]
    state = json.loads(json.dumps(case.state))
    state_path = tmp_path / "state.json"
    trace_path = tmp_path / "trace.jsonl"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    store = evaluation._FixtureStore(state_path, trace_path)
    key = store.read_action_key("admit", case.context.execution.task_id)[
        "idempotency_key"
    ]
    store.propose_action("admit", key, case.context.execution.task_id)
    state["live"]["existing_action"] = {
        "status": "completed",
        "task_id": "completed-store-race-task",
        "current_run_id": None,
        "source_key": case.context.source_item.canonical_key,
        "phase": case.context.phase,
        "input_identity": case.context.input_identity,
        "semantic_lane": case.context.semantic_lane,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    readback_calls: list[tuple[Any, ...]] = []

    def unexpected_readback(*args: Any, **kwargs: Any):
        readback_calls.append((args, kwargs))
        raise AssertionError("completed transition allocated a fixture readback")

    monkeypatch.setattr(evaluation, "simulated_action_readback", unexpected_readback)
    with pytest.raises(
        contract.ContractViolation, match="completed existing action is terminal"
    ):
        store.read_action(key)
    assert readback_calls == []
    trace = trace_path.read_text(encoding="utf-8")
    assert "read_action_readback" not in trace


def test_no_side_effect_adapter_rechecks_live_state_after_proposal_before_readback(
    monkeypatch: pytest.MonkeyPatch,
):
    case = evaluation.build_synthetic_cases(
        "completed-adapter-race", credentials_verified=True
    )[1]
    adapter = contract.NoSideEffectFixtureAdapter(json.loads(json.dumps(case.state)))
    adapter.bind_context(case.context)
    for name in (
        "read_live_state",
        "read_parent_completion",
        "read_source_state",
        "read_ready_lanes",
        "read_capabilities",
    ):
        getattr(adapter, name)()
    key = adapter.read_action_key("admit", case.context.execution.task_id)[
        "idempotency_key"
    ]
    adapter.propose_action("admit", key, case.context.execution.task_id)
    adapter._state["live"]["existing_action"] = {
        "status": "completed",
        "task_id": "completed-adapter-race-task",
        "current_run_id": None,
        "source_key": case.context.source_item.canonical_key,
        "phase": case.context.phase,
        "input_identity": case.context.input_identity,
        "semantic_lane": case.context.semantic_lane,
    }
    readback_calls: list[tuple[Any, ...]] = []

    def unexpected_readback(*args: Any, **kwargs: Any):
        readback_calls.append((args, kwargs))
        raise AssertionError("completed transition allocated an adapter readback")

    monkeypatch.setattr(evaluation, "simulated_action_readback", unexpected_readback)
    with pytest.raises(
        contract.ContractViolation, match="completed existing action is terminal"
    ):
        adapter.read_action_readback(key)
    assert readback_calls == []
    assert adapter.last_readback is None
    assert adapter.postproposal_receipt_reads == 1


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


def test_tmpdir_overlap_is_rejected_before_any_evaluation_setup_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    inherited_root = tmp_path / "live"
    inherited_root.mkdir()
    live_board = inherited_root / "kanban.db"
    live_board.write_bytes(b"live-board")
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    parent = _contaminated_environment(live_board)
    parent["TMPDIR"] = str(inherited_root)
    monkeypatch.setattr(os, "environ", parent)
    monkeypatch.setattr(tempfile, "tempdir", None)
    setup_roots: list[Path] = []

    def setup_write_observer(root: Path, **_: Any) -> Path:
        setup_roots.append(root)
        raise AssertionError("evaluation setup wrote before isolation validation")

    monkeypatch.setattr(evaluation, "_write_profile", setup_write_observer)

    with pytest.raises(
        evaluation.NativeEvaluationUnavailable,
        match="temporary parent overlaps inherited board authority",
    ):
        evaluation.run_native_evaluation(
            hermes=sys.executable,
            auth_file=str(auth_file),
            run_budget=30,
        )

    assert setup_roots == []
    assert list(inherited_root.iterdir()) == [live_board]


@pytest.mark.parametrize(
    "authority_key",
    [
        "HERMES_HOME",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_ATTACHMENTS_ROOT",
    ],
)
def test_temporary_parent_rejects_each_inherited_private_authority(
    tmp_path: Path, authority_key: str
):
    inherited_root = tmp_path / authority_key.lower()
    inherited_root.mkdir()
    parent = {"TMPDIR": str(inherited_root), authority_key: str(inherited_root)}

    with pytest.raises(
        evaluation.NativeEvaluationUnavailable,
        match="temporary parent overlaps inherited board authority",
    ):
        evaluation._validated_temporary_parent(parent)
    assert list(inherited_root.iterdir()) == []


def test_private_evaluation_root_is_mode_private_and_removed(tmp_path: Path):
    temporary_parent = tmp_path / "temporary-parent"
    temporary_parent.mkdir(mode=0o700)
    parent = {"TMPDIR": str(temporary_parent)}

    with evaluation._private_evaluation_root(parent) as root:
        created_root = root
        assert root.parent == temporary_parent
        assert root.stat().st_mode & 0o077 == 0
        (root / "bounded-fixture").write_text("fixture", encoding="utf-8")

    assert not created_root.exists()


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
        evaluation._parse_json_response(
            "{" + "x" * evaluation._MAX_NATIVE_RESPONSE_CHARS
        )


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


def test_snapshot_enforces_cumulative_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
    with pytest.raises(
        contract.ContractViolation, match="combined native Hermes output"
    ):
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
    with pytest.raises(subprocess.TimeoutExpired):
        evaluation._run_bounded_process(
            [sys.executable, "-c", code, str(pid_file)],
            cwd=tmp_path,
            env={},
            timeout=1,
        )
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(20):
        try:
            state = (
                Path(f"/proc/{child_pid}/stat").read_text(encoding="utf-8").split()[2]
            )
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


def test_native_subprocess_retry_discards_partial_trace_and_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    trace_path = tmp_path / "trace.jsonl"
    state_path = tmp_path / "state.json"
    reservation_path = state_path.with_name(state_path.name + ".reservation.json")
    calls = 0

    def fake_run(*_args: Any, **_kwargs: Any):
        nonlocal calls
        calls += 1
        if calls == 1:
            trace_path.write_text('{"tool":"propose_action"}\n', encoding="utf-8")
            reservation_path.write_text('{"records":{"poison":{}}}', encoding="utf-8")
            return 1, "", "provider temporarily unavailable"
        assert not trace_path.exists()
        assert not reservation_path.exists()
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
        state_path,
        trace_path,
        tmp_path / "board.db",
        "/bin/hermes",
        "model",
        "provider",
        1,
        parent_env={},
    )

    assert model.complete("prompt", {}) == {"decision": "hold"}
    assert calls == 2


def test_runtime_credential_resolution_uses_isolated_child_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    auth_module = sys.modules["hermes_cli.auth"]
    auth_module.resolve_api_key_provider_credentials = lambda _provider: {
        "api_key": "not-observed"
    }
    observed: dict[str, Any] = {}

    def fake_run(command, *, cwd, env, timeout):
        observed.update(command=command, cwd=cwd, env=env, timeout=timeout)
        return 0, "credential-ok", ""

    monkeypatch.setattr(evaluation, "_run_bounded_process", fake_run)
    profile = tmp_path / "profile"
    profile.mkdir()
    before = dict(os.environ)

    assert REAL_RESOLVE_RUNTIME_CREDENTIALS(
        "api-provider",
        profile,
        ("api_key", ("api-provider",), SimpleNamespace(id="api-provider")),
    )
    assert dict(os.environ) == before
    assert observed["env"]["HERMES_HOME"] == str(profile.resolve())
    assert observed["env"]["HOME"] == str(profile.resolve())
    assert "HERMES_KANBAN_TASK" not in observed["env"]
    assert observed["command"][0] == sys.executable


def test_configured_provider_uses_installed_full_resolver_without_network(
    monkeypatch: pytest.MonkeyPatch,
):
    providers_module = sys.modules["hermes_cli.providers"]
    calls: list[tuple[Any, ...]] = []
    metadata = SimpleNamespace(id="custom-provider", auth_type="api_key")

    def resolve_user_provider(name, providers):
        calls.append(("local", name, providers))
        return metadata

    def resolve_provider_full(name, providers, custom):
        calls.append(("full", name, providers, custom))
        return metadata

    monkeypatch.setattr(
        providers_module, "resolve_user_provider", resolve_user_provider, raising=False
    )
    monkeypatch.setattr(
        providers_module, "resolve_provider_full", resolve_provider_full, raising=False
    )
    configured = {
        "custom-provider": {
            "base_url": "https://provider.invalid/v1",
            "key_env": "CUSTOM_PROVIDER_KEY",
        }
    }

    auth_type, keys, resolved = evaluation._provider_resolution(
        "custom-provider", user_providers=configured
    )
    assert auth_type == "api_key"
    assert keys == ("custom-provider",)
    assert resolved is metadata
    assert [call[0] for call in calls] == ["local", "full"]


def test_authoritative_provider_aliases_resolve_without_network(
    monkeypatch: pytest.MonkeyPatch,
):
    providers_module = ModuleType("hermes_cli.providers")
    canonical = {
        "glm": "zai",
        "claude": "anthropic",
        "github": "github-copilot",
        "grok-oauth": "xai-oauth",
        "aws": "bedrock",
    }

    def normalize_provider(name):
        return canonical.get(name.strip().lower(), name.strip().lower())

    def get_provider(name, *, allow_network=True):
        assert allow_network is False
        return SimpleNamespace(
            id=name,
            name=name,
            auth_type="aws_sdk" if name == "bedrock" else "api_key",
            transport="openai_chat",
            base_url="https://provider.invalid/v1",
            api_key_env_vars=(),
        )

    providers_module.normalize_provider = normalize_provider
    providers_module.get_provider = get_provider
    providers_module.ALIASES = canonical
    hermes_module = sys.modules["hermes_cli"]
    hermes_module.providers = providers_module
    monkeypatch.setitem(sys.modules, "hermes_cli.providers", providers_module)

    for alias, provider_id in canonical.items():
        if alias == "aws":
            continue
        auth_type, metadata = evaluation._provider_metadata(alias)
        assert metadata.id == provider_id
        assert auth_type == "api_key"
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="explicitly unsupported"
    ):
        evaluation._provider_metadata("aws")


def test_credential_shaped_json_never_sets_verified_without_runtime_preflight(
    tmp_path: Path,
):
    source = tmp_path / "source-auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    source.write_text(
        json.dumps(
            {
                "providers": {
                    "openai-codex": {
                        "tokens": {
                            "access_token": "fixture-access",
                            "refresh_token": "fixture-refresh",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(evaluation.NativeEvaluationUnavailable, match="preflight"):
        evaluation._copy_auth(source, profile, provider="openai-codex")
    assert not (profile / "auth.json").exists()


def test_provider_registry_failure_is_injectable_and_fails_before_auth_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source-auth.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    source.write_text(
        json.dumps({"providers": {"openai-codex": {"api_key": "fixture-key"}}}),
        encoding="utf-8",
    )

    def unavailable():
        raise ImportError("provider registry unavailable")

    monkeypatch.setattr(
        evaluation, "_load_authoritative_provider_layer", unavailable, raising=False
    )
    opens: list[tuple[Any, ...]] = []
    real_open = evaluation.os.open

    def observed_open(*args: Any, **kwargs: Any):
        opens.append((args, kwargs))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(evaluation.os, "open", observed_open)
    with pytest.raises(
        evaluation.NativeEvaluationUnavailable, match="provider registry"
    ):
        evaluation._copy_auth(source, profile, provider="openai-codex")
    assert opens == []


def test_board_verification_rejects_untrusted_items_mapping(tmp_path: Path):
    class RaisingItemsMapping(Mapping):
        def __iter__(self):
            return iter((tmp_path / "board",))

        def __getitem__(self, key):
            return (False,)

        def __len__(self):
            return 1

        def items(self):
            raise RuntimeError("unbounded board snapshot items")

    with pytest.raises(evaluation.NativeEvaluationUnavailable):
        evaluation.verify_board_state_unchanged(RaisingItemsMapping())
