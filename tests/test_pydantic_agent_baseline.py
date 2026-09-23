"""Adversarial contracts for the synthetic corpus and observed baseline."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import pydantic_agent_baseline as baseline

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "benchmarks" / "pydantic-agent-corpus.json"
OBSERVED_PATH = ROOT / "benchmarks" / "hermes-baseline-observed.json"
SCRIPT_PATH = ROOT / "scripts" / "pydantic_agent_baseline.py"
REPORT_PATH = ROOT / "docs" / "pydantic-agent-baseline.md"


def _corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def _errors(document: dict) -> list[str]:
    return baseline.validate_corpus(document)


def test_checked_in_corpus_is_explicitly_synthetic_and_complete():
    document = _corpus()

    assert _errors(document) == []
    assert document["provenance"] == {
        "kind": "synthetic_test_vectors",
        "synthetic": True,
        "observed": False,
        "observed_production_data": False,
        "redaction": document["provenance"]["redaction"],
    }
    assert {
        fixture["scenario"] for fixture in document["fixtures"]
    } == baseline.REQUIRED_SCENARIO_SET
    assert all(fixture["synthetic"] for fixture in document["fixtures"])
    assert all(fixture["replay_safe"] for fixture in document["fixtures"])


def test_every_fixture_has_event_derived_counts_latencies_and_terminal_transition():
    document = _corpus()

    for fixture in document["fixtures"]:
        metrics = fixture["metrics"]
        events = fixture["events"]
        assert metrics["model_calls"] == sum(
            event["kind"] == "model_call" for event in events
        )
        assert metrics["tool_calls"] == sum(
            event["kind"] == "tool_call" for event in events
        )
        assert metrics["event_latency_ms"] == sum(
            event["latency_ms"] for event in events
        )
        assert (
            fixture["transitions"][-1]["to"] == fixture["durable_outcome"]["task_state"]
        )
        assert fixture["durable_outcome"]["terminal"] is True


def test_replay_is_deterministic_and_carries_synthetic_provenance():
    document = _corpus()
    before = copy.deepcopy(document)

    first = baseline.replay_corpus(document)
    second = baseline.replay_corpus(document)

    assert first == second
    assert document == before
    assert first["provenance"] == {
        "kind": "synthetic_test_vectors",
        "synthetic": True,
        "observed": False,
        "metrics_are_measurements": False,
    }
    assert first["synthetic"] is True
    assert first["observed"] is False
    assert first["side_effects"] is False
    assert first["fixtures_replayed"] == 8
    assert first["all_terminal"] is True


def test_replay_summary_contains_all_outcomes_and_event_totals():
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
    assert summary["totals"]["timeout_events"] == 1
    assert summary["totals"]["cancellation_events"] == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update({"unexpected": True}),
        lambda d: d["baseline"].update({"unexpected": True}),
        lambda d: d["fixtures"][0].update({"unexpected": True}),
        lambda d: d["fixtures"][0]["task"].update({"unexpected": True}),
        lambda d: d["fixtures"][0]["task"]["repository"].update({"unexpected": True}),
        lambda d: d["fixtures"][0]["events"][0].update({"unexpected": True}),
        lambda d: d["fixtures"][0]["transitions"][0].update({"unexpected": True}),
        lambda d: d["fixtures"][0]["metrics"].update({"unexpected": True}),
        lambda d: d["fixtures"][0]["durable_outcome"].update({"unexpected": True}),
        lambda d: d["fixtures"][5]["review_evidence"].update({"unexpected": True}),
        lambda d: d["fixtures"][5]["review_evidence"]["checks"][0].update(
            {"unexpected": True}
        ),
    ],
)
def test_validator_rejects_unknown_fields_recursively(mutate):
    document = _corpus()
    mutate(document)

    errors = _errors(document)

    assert any("unknown field" in error for error in errors)


@pytest.mark.parametrize("secret_key", ["apiKey", "access_token", "authorization"])
def test_validator_normalizes_secret_key_variants(secret_key):
    document = _corpus()
    document["fixtures"][0]["task"]["repository"][secret_key] = "redacted-value"

    errors = _errors(document)

    assert any("forbidden secret-like field" in error for error in errors)


def test_validator_rejects_credential_values_and_arbitrary_uri_schemes():
    document = _corpus()
    document["fixtures"][0]["task"]["objective"] = "custom+scheme:opaque-value"
    document["fixtures"][1]["task"]["objective"] = "Bearer redacted-value"

    errors = _errors(document)

    assert any("arbitrary URI scheme" in error for error in errors)
    assert any("credential-like value" in error for error in errors)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["fixtures"][0]["task"].update({"task_id": "prod-task-42"}),
        lambda d: d["fixtures"][0]["task"]["repository"].update(
            {"name": "https://example.invalid/repository"}
        ),
        lambda d: d["fixtures"][0]["task"]["workspace"].update(
            {"name": "~/.credentials"}
        ),
        lambda d: d["fixtures"][0]["task"].update({"objective": "read ../parent data"}),
    ],
)
def test_validator_rejects_live_ids_external_repositories_and_paths(mutate):
    document = _corpus()
    mutate(document)

    errors = _errors(document)

    assert errors
    assert any(
        any(
            marker in error
            for marker in ("fixture id", "live-looking", "external", "path", "tied")
        )
        for error in errors
    )


def test_reviewed_bypass_combination_is_rejected():
    document = _corpus()
    document["fixtures"][0]["task"]["apiKey"] = "redacted-value"
    document["fixtures"][0]["task"]["task_id"] = "prod-task-42"
    for key in (
        "model_calls",
        "model_requests",
        "tool_calls",
        "model_latency_ms",
        "tool_latency_ms",
        "event_latency_ms",
        "total_latency_ms",
    ):
        document["fixtures"][0]["metrics"][key] = 0

    errors = _errors(document)

    assert errors
    assert any("forbidden secret-like field" in error for error in errors)
    assert any("fixture id" in error for error in errors)
    assert any("event aggregate" in error for error in errors)


def test_timeout_requires_causal_event_and_transition():
    document = _corpus()
    fixture = document["fixtures"][3]
    fixture["events"] = fixture["events"][:-1]
    fixture["transitions"] = fixture["transitions"][:-1]

    errors = _errors(document)

    assert any("causal" in error or "transitions" in error for error in errors)


def test_cancellation_requires_causal_event_and_durable_semantics():
    document = _corpus()
    fixture = document["fixtures"][4]
    fixture["events"] = fixture["events"][:-1]
    fixture["durable_outcome"]["reason"] = "deadline_exceeded"
    fixture["durable_outcome"]["task_state"] = "blocked"

    errors = _errors(document)

    assert any("cancellation" in error or "causal" in error for error in errors)
    assert any("durable_outcome.reason" in error for error in errors)
    assert any("durable_outcome.task_state" in error for error in errors)


def test_review_evidence_is_fail_closed():
    document = _corpus()
    fixture = document["fixtures"][5]
    fixture["review_evidence"]["mutation_detected"] = True
    fixture["review_evidence"]["checks"][1]["status"] = "missing"

    errors = _errors(document)

    assert any("mutation_detected" in error for error in errors)
    assert any("must be passed" in error for error in errors)


def test_review_evidence_is_required_and_incomplete_is_distinct():
    document = _corpus()
    del document["fixtures"][5]["review_evidence"]
    document["fixtures"][7]["review_evidence"]["evidence_state"] = "complete"

    errors = _errors(document)

    assert any("review_evidence is required" in error for error in errors)
    assert any("evidence_state must be incomplete" in error for error in errors)


def test_cli_validate_and_replay_are_side_effect_free(tmp_path):
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
    assert "valid synthetic test-vector corpus" in validate.stdout
    assert replay.returncode == 0
    replay_summary = json.loads(replay.stdout)
    assert replay_summary["side_effects"] is False
    assert replay_summary["synthetic"] is True
    assert replay_summary["observed"] is False
    assert list(tmp_path.iterdir()) == []


def test_checked_in_observed_record_is_sanitized_and_fingerprinted():
    record = json.loads(OBSERVED_PATH.read_text(encoding="utf-8"))

    assert baseline.validate_observed_record(record) == []
    assert record["provenance"]["observed"] is True
    assert record["provenance"]["synthetic"] is False
    assert record["provenance"]["raw_logs_recorded"] is False
    assert record["command_contract"]["fixed_prompt"] == baseline.CAPTURE_PROMPT
    assert record["identity"]["hermes_profile"] == "implementer"
    assert record["identity"]["qualified_model"] == baseline.CAPTURE_MODEL
    assert '"session_id"' not in json.dumps(record)
    assert "credential_path" not in json.dumps(record)


def test_baseline_report_cites_architecture_sample_and_separates_observation():
    report = REPORT_PATH.read_text(encoding="utf-8")

    for required_text in (
        "synthetic test vectors",
        "hermes-baseline-observed.json",
        "observed_benign_hermes_smoke",
        "5ad887c7c38ba1c52d092d5b0011a7a0ff2573c0",
        "architecture smoke sample",
        "not a measurement",
        "capture",
        "input_tokens",
        "cache_read_tokens",
    ):
        assert required_text in report
    assert "issue-provided smoke samples" not in report
