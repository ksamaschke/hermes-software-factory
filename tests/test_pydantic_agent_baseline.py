"""Contract tests for the side-effect-free Pydantic-agent baseline corpus."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

from scripts import pydantic_agent_baseline as baseline

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "benchmarks" / "pydantic-agent-corpus.json"
SCRIPT_PATH = ROOT / "scripts" / "pydantic_agent_baseline.py"
REPORT_PATH = ROOT / "docs" / "pydantic-agent-baseline.md"


def _corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def test_checked_in_corpus_has_all_required_synthetic_scenarios():
    document = _corpus()

    assert baseline.validate_corpus(document) == []
    assert document["baseline"] == {
        "hermes_profile": "implementer",
        "model": "openai-codex:gpt-5.6-luna",
        "runtime_revision": "0c32430ab1243e060f21bab98c109e4f21d0a402",
    }
    assert {
        fixture["scenario"] for fixture in document["fixtures"]
    } == baseline.REQUIRED_SCENARIOS
    assert all(fixture["synthetic"] for fixture in document["fixtures"])
    assert all(fixture["replay_safe"] for fixture in document["fixtures"])


def test_every_fixture_records_the_complete_observability_contract():
    document = _corpus()
    required = baseline.REQUIRED_METRICS

    for fixture in document["fixtures"]:
        metrics = fixture["metrics"]
        assert required <= set(metrics)
        assert fixture["durable_outcome"]["terminal"] is True
        assert metrics["model_calls"] == metrics["model_requests"]
        assert metrics["cache_tokens"] == metrics["cache_read_tokens"]
        assert metrics["peak_rss_bytes"] > 0
        assert metrics["retry_count"] >= 0


def test_replay_is_deterministic_and_does_not_mutate_the_document():
    document = _corpus()
    before = copy.deepcopy(document)

    first = baseline.replay_corpus(document)
    second = baseline.replay_corpus(document)

    assert first == second
    assert document == before
    assert first["deterministic"] is True
    assert first["side_effects"] is False
    assert first["baseline"]["hermes_profile"] == "implementer"
    assert first["fixtures_replayed"] == len(document["fixtures"])
    assert first["all_terminal"] is True


def test_replay_summary_contains_all_durable_outcome_classes():
    summary = baseline.replay_corpus(CORPUS_PATH)

    assert summary["outcome_counts"] == {
        "APPROVED": 1,
        "CHANGES_REQUESTED": 1,
        "REVIEW_INCOMPLETE": 1,
        "cancelled": 1,
        "candidate_ready": 2,
        "failed": 1,
        "timed_out": 1,
    }
    assert summary["totals"]["model_requests"] == 9
    assert summary["totals"]["tool_calls"] == 14
    assert summary["totals"]["retry_count"] == 1


def test_validator_rejects_live_repository_references():
    document = _corpus()
    document["fixtures"][0]["task"]["repository"] = {
        "kind": "external",
        "name": "live-repository",
        "url": "https://example.invalid/repository",
    }

    errors = baseline.validate_corpus(document)

    assert any("repository.kind must be synthetic_fixture" in error for error in errors)
    assert any("external reference" in error for error in errors)


def test_validator_rejects_metric_and_event_drift():
    document = _corpus()
    fixture = document["fixtures"][0]
    fixture["metrics"]["tool_calls"] = 99
    fixture["metrics"]["total_latency_ms"] = 1

    errors = baseline.validate_corpus(document)

    assert any("tool_calls does not match tool events" in error for error in errors)
    assert any("total_latency_ms is shorter" in error for error in errors)


def test_cli_validate_and_replay_are_local_operations(tmp_path):
    validate = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "validate", str(CORPUS_PATH)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    replay = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "replay", str(CORPUS_PATH)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert validate.returncode == 0
    assert "valid synthetic corpus" in validate.stdout
    assert replay.returncode == 0
    replay_summary = json.loads(replay.stdout)
    assert replay_summary["side_effects"] is False
    assert replay_summary["fixtures_replayed"] == 8
    assert list(tmp_path.iterdir()) == []


def test_baseline_report_freezes_identity_and_smoke_limitations():
    report = REPORT_PATH.read_text(encoding="utf-8")

    for required_text in (
        "hermes_profile",
        "`implementer`",
        "openai-codex:gpt-5.6-luna",
        "0c32430ab1243e060f21bab98c109e4f21d0a402",
        "synthetic",
        "not observed production measurements",
        "not a corpus result",
        "274052 KiB",
        "40260-character system prompt",
    ):
        assert required_text in report
