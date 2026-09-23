#!/usr/bin/env python3
"""Validate and replay the synthetic Pydantic-agent migration corpus.

The replay path is intentionally boring: it parses checked-in JSON, validates
its contracts, and computes a deterministic summary. It does not import Hermes,
open a network connection, invoke Git, write a file, or contact a Factory task.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
REQUIRED_SCENARIOS = {
    "implementation_success",
    "test_failure",
    "provider_retry",
    "timeout",
    "cancellation",
    "review_approval",
    "review_changes_requested",
    "review_incomplete",
}
EXPECTED_OUTCOMES = {
    "implementation_success": "candidate_ready",
    "test_failure": "failed",
    "provider_retry": "candidate_ready",
    "timeout": "timed_out",
    "cancellation": "cancelled",
    "review_approval": "APPROVED",
    "review_changes_requested": "CHANGES_REQUESTED",
    "review_incomplete": "REVIEW_INCOMPLETE",
}
REQUIRED_METRICS = {
    "prompt_size_chars",
    "prompt_size_tokens",
    "model_calls",
    "model_requests",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_tokens",
    "queue_wait_ms",
    "startup_ms",
    "first_model_request_ms",
    "model_latency_ms",
    "tool_latency_ms",
    "total_latency_ms",
    "peak_rss_bytes",
    "retry_count",
}
COUNT_METRICS = {
    "model_calls",
    "model_requests",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_tokens",
    "retry_count",
}
LATENCY_METRICS = {
    "queue_wait_ms",
    "startup_ms",
    "first_model_request_ms",
    "model_latency_ms",
    "tool_latency_ms",
    "total_latency_ms",
}
REPLAY_SUM_METRICS = (
    "model_requests",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "cache_tokens",
    "queue_wait_ms",
    "startup_ms",
    "total_latency_ms",
    "retry_count",
)
SENSITIVE_KEYS = {
    "api_key",
    "credential",
    "password",
    "secret",
    "token",
    "url",
    "remote",
    "workspace_path",
    "live_task_id",
}


class CorpusValidationError(ValueError):
    """Raised when a corpus cannot be safely replayed."""

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _is_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value >= 0
    )


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_forbidden_keys(value: Any, path: str, errors: list[str]) -> None:
    """Reject fields that could accidentally turn a fixture into live data."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if key_text.lower() in SENSITIVE_KEYS:
                errors.append(f"{path}.{key_text} is forbidden in a fixture")
            _check_forbidden_keys(child, f"{path}.{key_text}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_forbidden_keys(child, f"{path}[{index}]", errors)
    elif isinstance(value, str):
        lowered = value.lower()
        if "http://" in lowered or "https://" in lowered or "git@" in lowered:
            errors.append(f"{path} contains an external reference")
        if value.startswith(("/", "\\")) or ":\\" in value:
            errors.append(f"{path} contains an absolute path")


def _mapping_or_error(
    value: Any, label: str, errors: list[str]
) -> Mapping[str, Any] | None:
    if not _is_mapping(value):
        errors.append(f"{label} must be an object")
        return None
    return value


def _require_mapping(
    document: Mapping[str, Any], key: str, errors: list[str]
) -> Mapping[str, Any] | None:
    return _mapping_or_error(document.get(key), key, errors)


def validate_corpus(document: Any) -> list[str]:
    """Return deterministic validation errors for a corpus document.

    Validation is deliberately strict because the replay command is intended
    for sanitized, checked-in fixtures rather than arbitrary task exports.
    """

    errors: list[str] = []
    if not _is_mapping(document):
        return ["corpus must be an object"]

    if document.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if (
        not isinstance(document.get("corpus_id"), str)
        or not document["corpus_id"].strip()
    ):
        errors.append("corpus_id must be a non-empty string")

    baseline = _require_mapping(document, "baseline", errors)
    if baseline is not None:
        for key in ("hermes_profile", "model", "runtime_revision"):
            value = baseline.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"baseline.{key} must be a non-empty string")
        revision = baseline.get("runtime_revision")
        if isinstance(revision, str) and (
            len(revision) != 40
            or any(
                character not in "0123456789abcdef" for character in revision.lower()
            )
        ):
            errors.append(
                "baseline.runtime_revision must be a 40-character hexadecimal revision"
            )

    provenance = _require_mapping(document, "provenance", errors)
    if provenance is not None:
        if provenance.get("kind") != "synthetic":
            errors.append("provenance.kind must be synthetic")
        if provenance.get("observed_production_data") is not False:
            errors.append("provenance.observed_production_data must be false")

    replay = _require_mapping(document, "replay", errors)
    if replay is not None:
        required_false = (
            "network",
            "writes_files",
            "mutates_factory_tasks",
            "accesses_external_repositories",
            "requires_credentials",
        )
        if replay.get("deterministic") is not True:
            errors.append("replay.deterministic must be true")
        for key in required_false:
            if replay.get(key) is not False:
                errors.append(f"replay.{key} must be false")

    fixtures = document.get("fixtures")
    if not isinstance(fixtures, list):
        errors.append("fixtures must be an array")
        fixtures = []

    seen_ids: set[str] = set()
    seen_scenarios: set[str] = set()
    for index, fixture in enumerate(fixtures):
        prefix = f"fixtures[{index}]"
        if not _is_mapping(fixture):
            errors.append(f"{prefix} must be an object")
            continue

        fixture_id = fixture.get("id")
        scenario = fixture.get("scenario")
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            errors.append(f"{prefix}.id must be a non-empty string")
        elif fixture_id in seen_ids:
            errors.append(f"{prefix}.id is duplicated: {fixture_id}")
        else:
            seen_ids.add(fixture_id)
        if not isinstance(scenario, str) or scenario not in REQUIRED_SCENARIOS:
            errors.append(f"{prefix}.scenario is not a required scenario")
        elif scenario in seen_scenarios:
            errors.append(f"{prefix}.scenario is duplicated: {scenario}")
        else:
            seen_scenarios.add(scenario)

        if fixture.get("synthetic") is not True:
            errors.append(f"{prefix}.synthetic must be true")
        if fixture.get("replay_safe") is not True:
            errors.append(f"{prefix}.replay_safe must be true")

        task = _mapping_or_error(fixture.get("task"), f"{prefix}.task", errors)
        if task is not None:
            if not isinstance(task.get("task_id"), str) or not task["task_id"].strip():
                errors.append(f"{prefix}.task.task_id must be a non-empty string")
            if (
                not isinstance(task.get("objective"), str)
                or not task["objective"].strip()
            ):
                errors.append(f"{prefix}.task.objective must be a non-empty string")
            repository = task.get("repository")
            repository_kind = (
                repository.get("kind") if isinstance(repository, Mapping) else None
            )
            if repository_kind != "synthetic_fixture":
                errors.append(
                    f"{prefix}.task.repository.kind must be synthetic_fixture"
                )
            workspace = task.get("workspace")
            workspace_kind = (
                workspace.get("kind") if isinstance(workspace, Mapping) else None
            )
            if workspace_kind != "ephemeral_fixture":
                errors.append(f"{prefix}.task.workspace.kind must be ephemeral_fixture")
            if task.get("external_task_id") is not None:
                errors.append(f"{prefix}.task.external_task_id must be null")

        events = fixture.get("events")
        if not isinstance(events, list) or not events:
            errors.append(f"{prefix}.events must be a non-empty array")
            events = []
        model_event_count = 0
        tool_event_count = 0
        retry_event_count = 0
        for event_index, event in enumerate(events):
            event_prefix = f"{prefix}.events[{event_index}]"
            if not _is_mapping(event):
                errors.append(f"{event_prefix} must be an object")
                continue
            kind = event.get("kind")
            if kind == "model_call":
                model_event_count += 1
                if (
                    not isinstance(event.get("model"), str)
                    or not event["model"].strip()
                ):
                    errors.append(f"{event_prefix}.model must be a non-empty string")
            elif kind == "tool_call":
                tool_event_count += 1
                if not isinstance(event.get("name"), str) or not event["name"].strip():
                    errors.append(f"{event_prefix}.name must be a non-empty string")
            elif kind == "provider_retry":
                retry_event_count += 1
            else:
                errors.append(f"{event_prefix}.kind is unsupported")
            if not _is_nonnegative_number(event.get("latency_ms")):
                errors.append(f"{event_prefix}.latency_ms must be non-negative")

        metrics = _mapping_or_error(fixture.get("metrics"), f"{prefix}.metrics", errors)
        if metrics is not None:
            missing = sorted(REQUIRED_METRICS - set(metrics))
            errors.extend(f"{prefix}.metrics.{key} is required" for key in missing)
            for key in REQUIRED_METRICS & set(metrics):
                valid = (
                    _is_nonnegative_int(metrics[key])
                    if key in COUNT_METRICS
                    else _is_nonnegative_number(metrics[key])
                )
                if not valid:
                    errors.append(f"{prefix}.metrics.{key} must be non-negative")
            if metrics.get("model_calls") != metrics.get("model_requests"):
                errors.append(
                    f"{prefix}.metrics model_calls and model_requests must match"
                )
            if metrics.get("model_calls") != model_event_count:
                errors.append(
                    f"{prefix}.metrics.model_calls does not match model events"
                )
            if metrics.get("tool_calls") != tool_event_count:
                errors.append(f"{prefix}.metrics.tool_calls does not match tool events")
            if metrics.get("retry_count") != retry_event_count:
                errors.append(
                    f"{prefix}.metrics.retry_count does not match retry events"
                )
            if metrics.get("cache_tokens") != metrics.get("cache_read_tokens"):
                errors.append(
                    f"{prefix}.metrics cache_tokens and cache_read_tokens must match"
                )
            if (
                _is_nonnegative_number(metrics.get("input_tokens"))
                and _is_nonnegative_number(metrics.get("cache_tokens"))
                and metrics["cache_tokens"] > metrics["input_tokens"]
            ):
                errors.append(
                    f"{prefix}.metrics.cache_tokens cannot exceed input_tokens"
                )
            if (
                _is_nonnegative_number(metrics.get("queue_wait_ms"))
                and _is_nonnegative_number(metrics.get("startup_ms"))
                and _is_nonnegative_number(metrics.get("total_latency_ms"))
                and metrics["total_latency_ms"]
                < metrics["queue_wait_ms"] + metrics["startup_ms"]
            ):
                errors.append(
                    f"{prefix}.metrics.total_latency_ms is shorter than queue plus startup"
                )

        outcome = _mapping_or_error(
            fixture.get("durable_outcome"), f"{prefix}.durable_outcome", errors
        )
        if outcome is not None:
            status = outcome.get("status")
            if scenario in EXPECTED_OUTCOMES and status != EXPECTED_OUTCOMES[scenario]:
                errors.append(
                    f"{prefix}.durable_outcome.status must be {EXPECTED_OUTCOMES[scenario]}"
                )
            if outcome.get("terminal") is not True:
                errors.append(f"{prefix}.durable_outcome.terminal must be true")
            if (
                not isinstance(outcome.get("task_state"), str)
                or not outcome["task_state"].strip()
            ):
                errors.append(
                    f"{prefix}.durable_outcome.task_state must be a non-empty string"
                )
            if (
                not isinstance(outcome.get("summary"), str)
                or not outcome["summary"].strip()
            ):
                errors.append(
                    f"{prefix}.durable_outcome.summary must be a non-empty string"
                )

    missing_scenarios = sorted(REQUIRED_SCENARIOS - seen_scenarios)
    errors.extend(
        f"missing required scenario: {scenario}" for scenario in missing_scenarios
    )
    if len(fixtures) < len(REQUIRED_SCENARIOS):
        errors.append(
            f"fixtures must contain at least {len(REQUIRED_SCENARIOS)} entries"
        )

    _check_forbidden_keys(document, "corpus", errors)
    return errors


def load_corpus(path: str | Path) -> dict[str, Any]:
    """Load and validate one checked-in corpus file."""

    corpus_path = Path(path)
    try:
        document = json.loads(corpus_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusValidationError(
            (f"cannot read corpus {corpus_path}: {exc}",)
        ) from exc
    errors = validate_corpus(document)
    if errors:
        raise CorpusValidationError(errors)
    return document


def replay_corpus(source: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Compute a deterministic, side-effect-free summary for ``source``."""

    document = load_corpus(source) if isinstance(source, (str, Path)) else dict(source)
    errors = validate_corpus(document)
    if errors:
        raise CorpusValidationError(errors)

    fixtures = document["fixtures"]
    outcomes = Counter(fixture["durable_outcome"]["status"] for fixture in fixtures)
    totals = {
        key: sum(fixture["metrics"][key] for fixture in fixtures)
        for key in REPLAY_SUM_METRICS
    }
    return {
        "corpus_id": document["corpus_id"],
        "schema_version": document["schema_version"],
        "baseline": dict(document["baseline"]),
        "deterministic": True,
        "side_effects": False,
        "fixtures_replayed": len(fixtures),
        "fixture_ids": [fixture["id"] for fixture in fixtures],
        "scenario_ids": sorted(fixture["scenario"] for fixture in fixtures),
        "outcome_counts": dict(sorted(outcomes.items())),
        "totals": totals,
        "max_peak_rss_bytes": max(
            fixture["metrics"]["peak_rss_bytes"] for fixture in fixtures
        ),
        "all_terminal": all(
            fixture["durable_outcome"]["terminal"] for fixture in fixtures
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("validate", "validate a synthetic corpus without replaying it"),
        ("replay", "validate and print a deterministic replay summary"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("corpus", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        corpus = load_corpus(args.corpus)
    except CorpusValidationError as exc:
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    if args.command == "validate":
        print(
            f"valid synthetic corpus: {corpus['corpus_id']} ({len(corpus['fixtures'])} fixtures)"
        )
        return 0

    try:
        summary = replay_corpus(corpus)
    except CorpusValidationError as exc:  # Defensive: load_corpus already validated.
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
