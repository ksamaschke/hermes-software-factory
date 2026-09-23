#!/usr/bin/env python3
"""Validate, replay, and explicitly capture the Pydantic-agent baseline.

``validate`` and ``replay`` only read a checked-in synthetic corpus.  They are
standard-library-only, deterministic, and side-effect free.  ``capture`` is a
separate opt-in operation: it runs one bounded, benign Hermes one-shot with an
empty toolset in a temporary non-repository directory and writes a sanitized
observed record.  It never stores the response, prompts other than the fixed
benign phrase, environment values, credential paths, or raw logs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
OBSERVED_SCHEMA_VERSION = 1
CORPUS_ID = "pydantic-agent-factory-migration-v1"
CAPTURE_PROFILE = "implementer"
CAPTURE_MODEL = "openai-codex:gpt-5.6-luna"
CAPTURE_PROMPT = "Reply with exactly: Hermes baseline OK."
CAPTURE_TOOLSET = "bot_room"
CAPTURE_TIMEOUT_SECONDS = 120

REQUIRED_SCENARIOS = (
    "implementation_success",
    "test_failure",
    "provider_retry",
    "timeout",
    "cancellation",
    "review_approval",
    "review_changes_requested",
    "review_incomplete",
)
REQUIRED_SCENARIO_SET = frozenset(REQUIRED_SCENARIOS)
REVIEW_SCENARIOS = frozenset(
    {"review_approval", "review_changes_requested", "review_incomplete"}
)
EXPECTED_ROLES = {
    "implementation_success": "implementer",
    "test_failure": "implementer",
    "provider_retry": "implementer",
    "timeout": "implementer",
    "cancellation": "implementer",
    "review_approval": "code_reviewer",
    "review_changes_requested": "code_reviewer",
    "review_incomplete": "code_reviewer",
}
EXPECTED_OUTCOMES = {
    "implementation_success": ("candidate_ready", "done", "tests_passed"),
    "test_failure": ("failed", "blocked", "test_failure"),
    "provider_retry": (
        "candidate_ready",
        "done",
        "provider_retry_recovered",
    ),
    "timeout": ("timed_out", "timed_out", "deadline_exceeded"),
    "cancellation": ("cancelled", "cancelled", "operator_requested"),
    "review_approval": ("APPROVED", "review_approved", "review_passed"),
    "review_changes_requested": (
        "CHANGES_REQUESTED",
        "rework_required",
        "review_finding",
    ),
    "review_incomplete": (
        "REVIEW_INCOMPLETE",
        "blocked",
        "insufficient_review_evidence",
    ),
}
EXPECTED_TRANSITIONS = {
    "implementation_success": (
        ("queued", "running", "dispatch_started"),
        ("running", "done", "tests_passed"),
    ),
    "test_failure": (
        ("queued", "running", "dispatch_started"),
        ("running", "blocked", "test_failure"),
    ),
    "provider_retry": (
        ("queued", "running", "dispatch_started"),
        ("running", "done", "provider_retry_recovered"),
    ),
    "timeout": (
        ("queued", "running", "dispatch_started"),
        ("running", "timed_out", "deadline_exceeded"),
    ),
    "cancellation": (
        ("queued", "running", "dispatch_started"),
        ("running", "cancelled", "operator_requested"),
    ),
    "review_approval": (
        ("queued", "reviewing", "dispatch_started"),
        ("reviewing", "review_approved", "review_passed"),
    ),
    "review_changes_requested": (
        ("queued", "reviewing", "dispatch_started"),
        ("reviewing", "rework_required", "review_finding"),
    ),
    "review_incomplete": (
        ("queued", "reviewing", "dispatch_started"),
        ("reviewing", "blocked", "insufficient_review_evidence"),
    ),
}
EXPECTED_EVENT_KINDS = {
    "implementation_success": ("model_call", "tool_call", "tool_call"),
    "test_failure": ("model_call", "tool_call", "tool_call", "tool_call"),
    "provider_retry": (
        "model_call",
        "provider_retry",
        "model_call",
        "tool_call",
        "tool_call",
    ),
    "timeout": ("model_call", "tool_call", "timeout"),
    "cancellation": (
        "model_call",
        "tool_call",
        "cancellation_requested",
    ),
    "review_approval": ("model_call", "tool_call", "tool_call"),
    "review_changes_requested": ("model_call", "tool_call", "tool_call"),
    "review_incomplete": ("model_call", "tool_call"),
}
EXPECTED_TOOL_NAMES = {
    "implementation_success": ("workspace_read", "test_runner"),
    "test_failure": ("workspace_read", "workspace_patch", "test_runner"),
    "provider_retry": ("workspace_read", "test_runner"),
    "timeout": ("workspace_read",),
    "cancellation": ("workspace_read",),
    "review_approval": ("review_packet_read", "gate_evidence_read"),
    "review_changes_requested": ("review_packet_read", "exact_diff_read"),
    "review_incomplete": ("review_packet_read",),
}

INTEGER_METRICS = frozenset(
    {
        "prompt_size_chars",
        "prompt_size_tokens",
        "model_calls",
        "model_requests",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_tokens",
        "retry_count",
        "timeout_events",
        "cancellation_events",
        "peak_rss_bytes",
    }
)
REQUIRED_METRICS = frozenset(
    {
        *INTEGER_METRICS,
        "queue_wait_ms",
        "startup_ms",
        "first_model_request_ms",
        "model_latency_ms",
        "tool_latency_ms",
        "retry_latency_ms",
        "lifecycle_latency_ms",
        "event_latency_ms",
        "total_latency_ms",
    }
)
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
    "timeout_events",
    "cancellation_events",
)

# Normalize separators and case before checking.  This catches apiKey,
# api_key, API-KEY, access_token, and authorization without rejecting normal
# metric names such as input_tokens.
SECRET_KEY_NAMES = frozenset(
    {
        "apikey",
        "accesstoken",
        "authorization",
        "auth",
        "bearer",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "privatekey",
        "refresh token",
        "refreshtoken",
        "secret",
        "sessiontoken",
        "token",
        "url",
        "uri",
        "remote",
        "workspacepath",
        "livetaskid",
    }
)
CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)(?:bearer|basic)\s+\S+"
    r"|(?:api[_-]?key|access[_-]?token|authorization|password|secret|credential)\s*[:=]\s*\S+"
    r"|(?:sk|pk|gh[pousr]|xox[baprs])[-_][A-Za-z0-9_-]{8,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN\s+[^-]+-----"
)
ENV_REFERENCE_RE = re.compile(r"^\$\{?[A-Z_][A-Z0-9_]*\}?$")
URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?://)?")
QUALIFIED_MODEL_RE = re.compile(r"^[a-z][a-z0-9_-]*:[a-z0-9][a-z0-9._-]*$")
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
FIXTURE_ID_RE = re.compile(r"^fixture-[a-z0-9]+(?:-[a-z0-9]+)*$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
LIVE_LOOKING_ID_RE = re.compile(
    r"(?i)(?:^|[-_ ])(?:prod(?:uction)?|live|task|issue|run|pr|kanban)(?:[-_ ]?\d+|$)"
)
PATH_PARENT_RE = re.compile(r"(?:^|[\s/\\])\.\.?(?:[/\\]|$)")
ABSOLUTE_PATH_RE = re.compile(r"^(?:[/\\]|~[/\\]|[A-Za-z]:[/\\])")


class CorpusValidationError(ValueError):
    """Raised when a corpus or observed record cannot be safely consumed."""

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class CaptureError(RuntimeError):
    """Raised when the opt-in observed capture cannot produce a record."""


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


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _check_keys(
    value: Any,
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    path: str,
    errors: list[str],
) -> Mapping[str, Any] | None:
    if not _is_mapping(value):
        errors.append(f"{path} must be an object")
        return None
    for key in value:
        if not isinstance(key, str):
            errors.append(f"{path} contains a non-string field name")
            continue
        if key not in allowed:
            errors.append(f"{path}.{key} is an unknown field")
    for key in sorted(required - set(value)):
        errors.append(f"{path}.{key} is required")
    return value


def _check_nonempty_string(value: Any, path: str, errors: list[str]) -> bool:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path} must be a non-empty string")
        return False
    return True


def _check_identifier(value: Any, path: str, errors: list[str]) -> bool:
    if not _check_nonempty_string(value, path, errors):
        return False
    if not IDENTIFIER_RE.fullmatch(value):
        errors.append(f"{path} must be a safe identifier")
        return False
    return True


def _check_revision(value: Any, path: str, errors: list[str]) -> bool:
    if not _check_nonempty_string(value, path, errors):
        return False
    if not REVISION_RE.fullmatch(value):
        errors.append(f"{path} must be a 40-character hexadecimal revision")
        return False
    return True


def _check_safe_string(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str):
        return
    normalized_path = path.casefold()
    normalized_key = _normalized_key(path.rsplit(".", 1)[-1])
    if normalized_key in SECRET_KEY_NAMES:
        errors.append(f"{path} is a forbidden secret-like field")
    if CREDENTIAL_VALUE_RE.search(value) or ENV_REFERENCE_RE.fullmatch(value.strip()):
        errors.append(f"{path} contains a credential-like value")
    if "git@" in value.casefold() or "://" in value:
        errors.append(f"{path} contains an external repository or URL reference")
    if ABSOLUTE_PATH_RE.match(value) or PATH_PARENT_RE.search(value):
        errors.append(f"{path} contains an absolute, home, or parent path")
    # The corpus permits exactly one qualified model spelling.  Any other
    # scheme-like value is rejected rather than treated as harmless text.
    if URI_SCHEME_RE.match(value) and not (
        (
            normalized_path.endswith(
                (
                    ".baseline.model",
                    ".identity.qualified_model",
                    ".evidence_fingerprint",
                )
            )
        )
        and (
            QUALIFIED_MODEL_RE.fullmatch(value)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value)
        )
    ):
        errors.append(f"{path} contains an arbitrary URI scheme")
    if LIVE_LOOKING_ID_RE.search(value):
        errors.append(f"{path} contains a live-looking identifier")


def _check_forbidden_values(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            normalized = _normalized_key(key_text)
            if normalized in SECRET_KEY_NAMES:
                errors.append(f"{path}.{key_text} is a forbidden secret-like field")
            _check_forbidden_values(child, f"{path}.{key_text}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_forbidden_values(child, f"{path}[{index}]", errors)
    elif isinstance(value, str):
        _check_safe_string(value, path, errors)


def _check_event(event: Any, path: str, errors: list[str]) -> tuple[str, int]:
    if not _is_mapping(event):
        errors.append(f"{path} must be an object")
        return "", 0
    kind = event.get("kind")
    common = {"kind", "latency_ms"}
    if kind == "model_call":
        allowed = frozenset({*common, "provider", "model", "attempt"})
        required = frozenset({*allowed})
    elif kind == "tool_call":
        allowed = frozenset({*common, "name"})
        required = frozenset({*allowed})
    elif kind == "provider_retry" or kind in {"timeout", "cancellation_requested"}:
        allowed = frozenset({*common, "reason"})
        required = frozenset({*allowed})
    else:
        errors.append(f"{path}.kind is unsupported")
        allowed = frozenset(common)
        required = frozenset(common)
    checked = _check_keys(
        event, allowed=allowed, required=required, path=path, errors=errors
    )
    if checked is None:
        return str(kind or ""), 0
    latency = checked.get("latency_ms")
    if not _is_nonnegative_number(latency):
        errors.append(f"{path}.latency_ms must be non-negative")
    if kind == "model_call":
        _check_identifier(checked.get("provider"), f"{path}.provider", errors)
        _check_nonempty_string(checked.get("model"), f"{path}.model", errors)
        if not _is_nonnegative_int(checked.get("attempt")) or checked["attempt"] < 1:
            errors.append(f"{path}.attempt must be a positive integer")
    elif kind == "tool_call":
        _check_identifier(checked.get("name"), f"{path}.name", errors)
    elif kind in {"provider_retry", "timeout", "cancellation_requested"}:
        _check_identifier(checked.get("reason"), f"{path}.reason", errors)
    latency_value = (
        int(latency)
        if isinstance(latency, (int, float))
        and not isinstance(latency, bool)
        and math.isfinite(float(latency))
        and latency >= 0
        else 0
    )
    return str(kind or ""), latency_value


def _check_review_evidence(
    evidence: Any, scenario: str, path: str, errors: list[str]
) -> None:
    checked = _check_keys(
        evidence,
        allowed=frozenset(
            {
                "packet_state",
                "evidence_state",
                "mutation_detected",
                "checks",
                "findings",
            }
        ),
        required=frozenset(
            {
                "packet_state",
                "evidence_state",
                "mutation_detected",
                "checks",
                "findings",
            }
        ),
        path=path,
        errors=errors,
    )
    if checked is None:
        return
    if checked.get("packet_state") != "immutable":
        errors.append(f"{path}.packet_state must be immutable")
    expected_evidence_state = (
        "incomplete" if scenario == "review_incomplete" else "complete"
    )
    if checked.get("evidence_state") != expected_evidence_state:
        errors.append(
            f"{path}.evidence_state must be {expected_evidence_state} for {scenario}"
        )
    if checked.get("mutation_detected") is not False:
        errors.append(f"{path}.mutation_detected must be false")
    findings = checked.get("findings")
    findings_int = (
        findings
        if isinstance(findings, int)
        and not isinstance(findings, bool)
        and findings >= 0
        else None
    )
    if findings_int is None:
        errors.append(f"{path}.findings must be a non-negative integer")
    elif scenario == "review_approval" and findings_int != 0:
        errors.append(f"{path}.findings must be zero for review_approval")
    elif scenario == "review_changes_requested" and findings_int < 1:
        errors.append(f"{path}.findings must be positive for review_changes_requested")
    elif scenario == "review_incomplete" and findings_int != 0:
        errors.append(f"{path}.findings must be zero for review_incomplete")

    checks = checked.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append(f"{path}.checks must be a non-empty array")
        checks = []
    check_names: list[str] = []
    for index, check in enumerate(checks):
        check_path = f"{path}.checks[{index}]"
        check_obj = _check_keys(
            check,
            allowed=frozenset({"name", "status"}),
            required=frozenset({"name", "status"}),
            path=check_path,
            errors=errors,
        )
        if check_obj is None:
            continue
        name = check_obj.get("name")
        status = check_obj.get("status")
        _check_identifier(name, f"{check_path}.name", errors)
        if status not in {"passed", "missing"}:
            errors.append(f"{check_path}.status is unsupported")
        if isinstance(name, str):
            if name in check_names:
                errors.append(f"{check_path}.name is duplicated")
            check_names.append(name)
    expected_checks = {
        "review_approval": {"review_packet_read", "gate_evidence_read"},
        "review_changes_requested": {"review_packet_read", "exact_diff_read"},
        "review_incomplete": {"review_packet_read"},
    }[scenario]
    if set(check_names) != expected_checks:
        errors.append(
            f"{path}.checks must exactly cover {sorted(expected_checks)} for {scenario}"
        )
    for check in checks:
        if (
            isinstance(check, Mapping)
            and check.get("name") in expected_checks
            and check.get("status") != "passed"
        ):
            errors.append(
                f"{path}.checks.{check.get('name')} must be passed in review evidence"
            )


def _check_metrics(
    metrics: Any,
    events: Sequence[Mapping[str, Any]],
    scenario: str,
    path: str,
    errors: list[str],
) -> None:
    checked = _check_keys(
        metrics,
        allowed=frozenset(REQUIRED_METRICS),
        required=frozenset(REQUIRED_METRICS),
        path=path,
        errors=errors,
    )
    if checked is None:
        return
    for key in REQUIRED_METRICS & set(checked):
        valid = (
            _is_nonnegative_int(checked[key])
            if key in INTEGER_METRICS
            else _is_nonnegative_number(checked[key])
        )
        if not valid:
            errors.append(f"{path}.{key} must be non-negative")

    model_events = [event for event in events if event.get("kind") == "model_call"]
    tool_events = [event for event in events if event.get("kind") == "tool_call"]
    retry_events = [event for event in events if event.get("kind") == "provider_retry"]
    timeout_events = [event for event in events if event.get("kind") == "timeout"]
    cancellation_events = [
        event for event in events if event.get("kind") == "cancellation_requested"
    ]
    event_latency = sum(
        event.get("latency_ms", 0)
        for event in events
        if _is_nonnegative_number(event.get("latency_ms"))
    )
    model_latency = sum(event.get("latency_ms", 0) for event in model_events)
    tool_latency = sum(event.get("latency_ms", 0) for event in tool_events)
    retry_latency = sum(event.get("latency_ms", 0) for event in retry_events)
    lifecycle_latency = sum(
        event.get("latency_ms", 0) for event in (*timeout_events, *cancellation_events)
    )
    expected_pairs = (
        ("model_calls", len(model_events)),
        ("model_requests", len(model_events)),
        ("tool_calls", len(tool_events)),
        ("retry_count", len(retry_events)),
        ("timeout_events", len(timeout_events)),
        ("cancellation_events", len(cancellation_events)),
        ("model_latency_ms", model_latency),
        ("tool_latency_ms", tool_latency),
        ("retry_latency_ms", retry_latency),
        ("lifecycle_latency_ms", lifecycle_latency),
        ("event_latency_ms", event_latency),
    )
    for key, expected in expected_pairs:
        if checked.get(key) != expected:
            errors.append(f"{path}.{key} does not match event aggregate")
    if checked.get("cache_tokens") != checked.get("cache_read_tokens"):
        errors.append(f"{path}.cache_tokens must equal cache_read_tokens")
    if (
        _is_nonnegative_int(checked.get("cache_tokens"))
        and _is_nonnegative_int(checked.get("input_tokens"))
        and checked["cache_tokens"] > checked["input_tokens"]
    ):
        errors.append(f"{path}.cache_tokens cannot exceed input_tokens")
    if (
        _is_nonnegative_number(checked.get("queue_wait_ms"))
        and _is_nonnegative_number(checked.get("startup_ms"))
        and _is_nonnegative_number(checked.get("event_latency_ms"))
        and checked.get("total_latency_ms")
        != checked["queue_wait_ms"]
        + checked["startup_ms"]
        + checked["event_latency_ms"]
    ):
        errors.append(f"{path}.total_latency_ms does not match queue/startup/events")
    if model_events and _is_nonnegative_number(model_events[0].get("latency_ms")):
        expected_first = (
            checked.get("queue_wait_ms", 0)
            + checked.get("startup_ms", 0)
            + model_events[0]["latency_ms"]
        )
        if checked.get("first_model_request_ms") != expected_first:
            errors.append(
                f"{path}.first_model_request_ms does not match first model event"
            )
    if not _is_nonnegative_int(checked.get("peak_rss_bytes")) or not checked.get(
        "peak_rss_bytes"
    ):
        errors.append(f"{path}.peak_rss_bytes must be positive")
    if (
        scenario in {"timeout", "cancellation"}
        and checked.get("lifecycle_latency_ms", 0) <= 0
    ):
        errors.append(f"{path}.lifecycle_latency_ms must include the causal event")


def validate_corpus(document: Any) -> list[str]:
    """Return deterministic errors for the strict synthetic corpus schema."""

    errors: list[str] = []
    checked_document = _check_keys(
        document,
        allowed=frozenset(
            {
                "schema_version",
                "corpus_id",
                "baseline",
                "provenance",
                "replay",
                "fixtures",
            }
        ),
        required=frozenset(
            {
                "schema_version",
                "corpus_id",
                "baseline",
                "provenance",
                "replay",
                "fixtures",
            }
        ),
        path="corpus",
        errors=errors,
    )
    if checked_document is None:
        return errors
    if checked_document.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"corpus.schema_version must be {SCHEMA_VERSION}")
    if checked_document.get("corpus_id") != CORPUS_ID:
        errors.append(f"corpus.corpus_id must be {CORPUS_ID}")

    baseline = _check_keys(
        checked_document.get("baseline"),
        allowed=frozenset({"hermes_profile", "model", "runtime_revision"}),
        required=frozenset({"hermes_profile", "model", "runtime_revision"}),
        path="corpus.baseline",
        errors=errors,
    )
    if baseline is not None:
        _check_identifier(
            baseline.get("hermes_profile"), "corpus.baseline.hermes_profile", errors
        )
        model = baseline.get("model")
        if not isinstance(model, str) or not QUALIFIED_MODEL_RE.fullmatch(model):
            errors.append("corpus.baseline.model must be a qualified model")
        _check_revision(
            baseline.get("runtime_revision"), "corpus.baseline.runtime_revision", errors
        )

    provenance = _check_keys(
        checked_document.get("provenance"),
        allowed=frozenset(
            {"kind", "synthetic", "observed", "observed_production_data", "redaction"}
        ),
        required=frozenset(
            {"kind", "synthetic", "observed", "observed_production_data", "redaction"}
        ),
        path="corpus.provenance",
        errors=errors,
    )
    if provenance is not None:
        if provenance.get("kind") != "synthetic_test_vectors":
            errors.append("corpus.provenance.kind must be synthetic_test_vectors")
        if provenance.get("synthetic") is not True:
            errors.append("corpus.provenance.synthetic must be true")
        if provenance.get("observed") is not False:
            errors.append("corpus.provenance.observed must be false")
        if provenance.get("observed_production_data") is not False:
            errors.append("corpus.provenance.observed_production_data must be false")
        _check_nonempty_string(
            provenance.get("redaction"), "corpus.provenance.redaction", errors
        )

    replay = _check_keys(
        checked_document.get("replay"),
        allowed=frozenset(
            {
                "deterministic",
                "network",
                "writes_files",
                "mutates_factory_tasks",
                "accesses_external_repositories",
                "requires_credentials",
            }
        ),
        required=frozenset(
            {
                "deterministic",
                "network",
                "writes_files",
                "mutates_factory_tasks",
                "accesses_external_repositories",
                "requires_credentials",
            }
        ),
        path="corpus.replay",
        errors=errors,
    )
    if replay is not None:
        if replay.get("deterministic") is not True:
            errors.append("corpus.replay.deterministic must be true")
        for key in (
            "network",
            "writes_files",
            "mutates_factory_tasks",
            "accesses_external_repositories",
            "requires_credentials",
        ):
            if replay.get(key) is not False:
                errors.append(f"corpus.replay.{key} must be false")

    fixtures = checked_document.get("fixtures")
    if not isinstance(fixtures, list):
        errors.append("corpus.fixtures must be an array")
        fixtures = []
    if len(fixtures) != len(REQUIRED_SCENARIOS):
        errors.append("corpus.fixtures must contain exactly eight entries")

    seen_ids: set[str] = set()
    seen_scenarios: set[str] = set()
    for index, fixture in enumerate(fixtures):
        prefix = f"corpus.fixtures[{index}]"
        checked_fixture = _check_keys(
            fixture,
            allowed=frozenset(
                {
                    "id",
                    "scenario",
                    "role",
                    "synthetic",
                    "replay_safe",
                    "task",
                    "events",
                    "transitions",
                    "metrics",
                    "durable_outcome",
                    "review_evidence",
                }
            ),
            required=frozenset(
                {
                    "id",
                    "scenario",
                    "role",
                    "synthetic",
                    "replay_safe",
                    "task",
                    "events",
                    "transitions",
                    "metrics",
                    "durable_outcome",
                }
            ),
            path=prefix,
            errors=errors,
        )
        if checked_fixture is None:
            continue
        fixture_id = checked_fixture.get("id")
        scenario = checked_fixture.get("scenario")
        if not isinstance(fixture_id, str) or not FIXTURE_ID_RE.fullmatch(fixture_id):
            errors.append(f"{prefix}.id must use the fixture- prefix")
        elif fixture_id in seen_ids:
            errors.append(f"{prefix}.id is duplicated")
        else:
            seen_ids.add(fixture_id)
        if not isinstance(scenario, str) or scenario not in REQUIRED_SCENARIO_SET:
            errors.append(f"{prefix}.scenario is not a required scenario")
        elif scenario in seen_scenarios:
            errors.append(f"{prefix}.scenario is duplicated")
        else:
            seen_scenarios.add(scenario)
        if isinstance(scenario, str) and scenario in REQUIRED_SCENARIO_SET:
            expected_id = f"fixture-{scenario.replace('_', '-')}"
            if fixture_id != expected_id:
                errors.append(f"{prefix}.id must be {expected_id}")
            if checked_fixture.get("role") != EXPECTED_ROLES[scenario]:
                errors.append(f"{prefix}.role must be {EXPECTED_ROLES[scenario]}")
        if checked_fixture.get("synthetic") is not True:
            errors.append(f"{prefix}.synthetic must be true")
        if checked_fixture.get("replay_safe") is not True:
            errors.append(f"{prefix}.replay_safe must be true")

        task = _check_keys(
            checked_fixture.get("task"),
            allowed=frozenset(
                {"task_id", "objective", "repository", "workspace", "external_task_id"}
            ),
            required=frozenset(
                {"task_id", "objective", "repository", "workspace", "external_task_id"}
            ),
            path=f"{prefix}.task",
            errors=errors,
        )
        if task is not None:
            if task.get("task_id") != fixture_id:
                errors.append(f"{prefix}.task.task_id must equal the fixture id")
            _check_nonempty_string(
                task.get("objective"), f"{prefix}.task.objective", errors
            )
            suffix = (
                fixture_id.removeprefix("fixture-")
                if isinstance(fixture_id, str)
                else ""
            )
            repository = _check_keys(
                task.get("repository"),
                allowed=frozenset({"kind", "name"}),
                required=frozenset({"kind", "name"}),
                path=f"{prefix}.task.repository",
                errors=errors,
            )
            if repository is not None:
                if repository.get("kind") != "synthetic_fixture":
                    errors.append(
                        f"{prefix}.task.repository.kind must be synthetic_fixture"
                    )
                if repository.get("name") != f"repo-{suffix}":
                    errors.append(
                        f"{prefix}.task.repository.name must be tied to the fixture"
                    )
            workspace = _check_keys(
                task.get("workspace"),
                allowed=frozenset({"kind", "name"}),
                required=frozenset({"kind", "name"}),
                path=f"{prefix}.task.workspace",
                errors=errors,
            )
            if workspace is not None:
                if workspace.get("kind") != "ephemeral_fixture":
                    errors.append(
                        f"{prefix}.task.workspace.kind must be ephemeral_fixture"
                    )
                if workspace.get("name") != f"workspace-{suffix}":
                    errors.append(
                        f"{prefix}.task.workspace.name must be tied to the fixture"
                    )
            if task.get("external_task_id") is not None:
                errors.append(f"{prefix}.task.external_task_id must be null")

        events_value = checked_fixture.get("events")
        if not isinstance(events_value, list) or not events_value:
            errors.append(f"{prefix}.events must be a non-empty array")
            events_value = []
        event_kinds: list[str] = []
        event_rows: list[Mapping[str, Any]] = []
        for event_index, event in enumerate(events_value):
            kind, _ = _check_event(event, f"{prefix}.events[{event_index}]", errors)
            event_kinds.append(kind)
            if isinstance(event, Mapping):
                event_rows.append(event)
        if isinstance(scenario, str) and scenario in EXPECTED_EVENT_KINDS:
            if tuple(event_kinds) != EXPECTED_EVENT_KINDS[scenario]:
                errors.append(f"{prefix}.events are not causal for {scenario}")
            tool_names = tuple(
                event.get("name")
                for event in event_rows
                if event.get("kind") == "tool_call"
            )
            if tool_names != EXPECTED_TOOL_NAMES[scenario]:
                errors.append(
                    f"{prefix}.events tool evidence is not valid for {scenario}"
                )
            model_attempts = [
                event.get("attempt")
                for event in event_rows
                if event.get("kind") == "model_call"
            ]
            if model_attempts != list(range(1, len(model_attempts) + 1)):
                errors.append(f"{prefix}.events model attempts must be consecutive")
            if scenario == "timeout" and "timeout" not in event_kinds:
                errors.append(
                    f"{prefix}.events requires a scenario-causal timeout event"
                )
            if (
                scenario == "cancellation"
                and "cancellation_requested" not in event_kinds
            ):
                errors.append(
                    f"{prefix}.events requires a scenario-causal cancellation event"
                )

        transitions_value = checked_fixture.get("transitions")
        transition_rows: list[Mapping[str, Any]] = []
        if not isinstance(transitions_value, list) or not transitions_value:
            errors.append(f"{prefix}.transitions must be a non-empty array")
            transitions_value = []
        for transition_index, transition in enumerate(transitions_value):
            transition_path = f"{prefix}.transitions[{transition_index}]"
            checked_transition = _check_keys(
                transition,
                allowed=frozenset({"from", "to", "reason"}),
                required=frozenset({"from", "to", "reason"}),
                path=transition_path,
                errors=errors,
            )
            if checked_transition is not None:
                for key in ("from", "to", "reason"):
                    _check_identifier(
                        checked_transition.get(key), f"{transition_path}.{key}", errors
                    )
                transition_rows.append(checked_transition)
        if isinstance(scenario, str) and scenario in EXPECTED_TRANSITIONS:
            actual_transitions = tuple(
                (row.get("from"), row.get("to"), row.get("reason"))
                for row in transition_rows
            )
            if actual_transitions != EXPECTED_TRANSITIONS[scenario]:
                errors.append(
                    f"{prefix}.transitions do not match durable outcome for {scenario}"
                )

        _check_metrics(
            checked_fixture.get("metrics"),
            event_rows,
            str(scenario),
            f"{prefix}.metrics",
            errors,
        )

        outcome = _check_keys(
            checked_fixture.get("durable_outcome"),
            allowed=frozenset(
                {"status", "terminal", "task_state", "reason", "summary"}
            ),
            required=frozenset(
                {"status", "terminal", "task_state", "reason", "summary"}
            ),
            path=f"{prefix}.durable_outcome",
            errors=errors,
        )
        if (
            outcome is not None
            and isinstance(scenario, str)
            and scenario in EXPECTED_OUTCOMES
        ):
            expected_status, expected_state, expected_reason = EXPECTED_OUTCOMES[
                scenario
            ]
            if outcome.get("status") != expected_status:
                errors.append(
                    f"{prefix}.durable_outcome.status must be {expected_status}"
                )
            if outcome.get("terminal") is not True:
                errors.append(f"{prefix}.durable_outcome.terminal must be true")
            if outcome.get("task_state") != expected_state:
                errors.append(
                    f"{prefix}.durable_outcome.task_state must be {expected_state}"
                )
            if outcome.get("reason") != expected_reason:
                errors.append(
                    f"{prefix}.durable_outcome.reason must be {expected_reason}"
                )
            summary = outcome.get("summary")
            if not _check_nonempty_string(
                summary, f"{prefix}.durable_outcome.summary", errors
            ):
                pass
            elif isinstance(summary, str) and not summary.startswith(
                "Synthetic test vector:"
            ):
                errors.append(
                    f"{prefix}.durable_outcome.summary must be labeled synthetic"
                )

        if scenario in REVIEW_SCENARIOS:
            if "review_evidence" not in checked_fixture:
                errors.append(
                    f"{prefix}.review_evidence is required for review scenarios"
                )
            else:
                _check_review_evidence(
                    checked_fixture.get("review_evidence"),
                    str(scenario),
                    f"{prefix}.review_evidence",
                    errors,
                )
        elif "review_evidence" in checked_fixture:
            errors.append(
                f"{prefix}.review_evidence is only valid for review scenarios"
            )

    missing_scenarios = sorted(REQUIRED_SCENARIO_SET - seen_scenarios)
    errors.extend(
        f"missing required scenario: {scenario}" for scenario in missing_scenarios
    )
    _check_forbidden_values(document, "corpus", errors)
    return errors


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _evidence_fingerprint(record: Mapping[str, Any]) -> str:
    payload = dict(record)
    payload.pop("evidence_fingerprint", None)
    return "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()


def validate_observed_record(document: Any) -> list[str]:
    """Validate the sanitized observed-record contract and its fingerprint."""

    errors: list[str] = []
    checked = _check_keys(
        document,
        allowed=frozenset(
            {
                "schema_version",
                "record_type",
                "provenance",
                "identity",
                "command_contract",
                "captured_at_utc",
                "execution",
                "usage_evidence",
                "metrics",
                "metric_unavailable_reasons",
                "evidence_fingerprint",
            }
        ),
        required=frozenset(
            {
                "schema_version",
                "record_type",
                "provenance",
                "identity",
                "command_contract",
                "captured_at_utc",
                "execution",
                "usage_evidence",
                "metrics",
                "metric_unavailable_reasons",
                "evidence_fingerprint",
            }
        ),
        path="observed",
        errors=errors,
    )
    if checked is None:
        return errors
    if checked.get("schema_version") != OBSERVED_SCHEMA_VERSION:
        errors.append("observed.schema_version is unsupported")
    if checked.get("record_type") != "observed_hermes_baseline":
        errors.append("observed.record_type is unsupported")

    provenance = _check_keys(
        checked.get("provenance"),
        allowed=frozenset(
            {
                "kind",
                "synthetic",
                "observed",
                "sanitized",
                "raw_prompts_recorded",
                "raw_logs_recorded",
                "redaction",
            }
        ),
        required=frozenset(
            {
                "kind",
                "synthetic",
                "observed",
                "sanitized",
                "raw_prompts_recorded",
                "raw_logs_recorded",
                "redaction",
            }
        ),
        path="observed.provenance",
        errors=errors,
    )
    if provenance is not None:
        if provenance.get("kind") != "observed_benign_hermes_smoke":
            errors.append("observed.provenance.kind is unsupported")
        for key in ("synthetic", "raw_prompts_recorded", "raw_logs_recorded"):
            if provenance.get(key) is not False:
                errors.append(f"observed.provenance.{key} must be false")
        for key in ("observed", "sanitized"):
            if provenance.get(key) is not True:
                errors.append(f"observed.provenance.{key} must be true")
        _check_nonempty_string(
            provenance.get("redaction"), "observed.provenance.redaction", errors
        )

    identity = _check_keys(
        checked.get("identity"),
        allowed=frozenset(
            {
                "hermes_profile",
                "qualified_model",
                "hermes_version",
                "hermes_source_revision",
                "factory_runtime_revision",
            }
        ),
        required=frozenset(
            {
                "hermes_profile",
                "qualified_model",
                "hermes_version",
                "hermes_source_revision",
                "factory_runtime_revision",
            }
        ),
        path="observed.identity",
        errors=errors,
    )
    if identity is not None:
        _check_identifier(
            identity.get("hermes_profile"), "observed.identity.hermes_profile", errors
        )
        if not isinstance(
            identity.get("qualified_model"), str
        ) or not QUALIFIED_MODEL_RE.fullmatch(identity.get("qualified_model", "")):
            errors.append("observed.identity.qualified_model must be qualified")
        for key in (
            "hermes_version",
            "hermes_source_revision",
            "factory_runtime_revision",
        ):
            value = identity.get(key)
            if value is not None and not isinstance(value, str):
                errors.append(f"observed.identity.{key} must be a string or null")
        for key in ("hermes_source_revision", "factory_runtime_revision"):
            value = identity.get(key)
            if value is not None and not REVISION_RE.fullmatch(value):
                errors.append(
                    f"observed.identity.{key} must be a full hexadecimal revision or null"
                )

    command = _check_keys(
        checked.get("command_contract"),
        allowed=frozenset(
            {
                "mode",
                "fixed_prompt",
                "toolsets",
                "tools_allowed",
                "repository_access",
                "working_directory",
                "timeout_seconds",
                "stdout_stderr",
                "usage_evidence",
            }
        ),
        required=frozenset(
            {
                "mode",
                "fixed_prompt",
                "toolsets",
                "tools_allowed",
                "repository_access",
                "working_directory",
                "timeout_seconds",
                "stdout_stderr",
                "usage_evidence",
            }
        ),
        path="observed.command_contract",
        errors=errors,
    )
    if command is not None:
        expected_command = {
            "mode": "bounded_one_shot",
            "fixed_prompt": CAPTURE_PROMPT,
            "toolsets": [CAPTURE_TOOLSET],
            "tools_allowed": False,
            "repository_access": "none",
            "working_directory": "temporary_directory",
            "timeout_seconds": CAPTURE_TIMEOUT_SECONDS,
            "stdout_stderr": "discarded",
            "usage_evidence": "sanitized_usage_file",
        }
        for key, expected in expected_command.items():
            if command.get(key) != expected:
                errors.append(
                    f"observed.command_contract.{key} does not match capture contract"
                )

    execution = _check_keys(
        checked.get("execution"),
        allowed=frozenset(
            {
                "exit_code",
                "timed_out",
                "wall_time_ms",
                "peak_rss_bytes",
                "response_recorded",
                "raw_output_recorded",
            }
        ),
        required=frozenset(
            {
                "exit_code",
                "timed_out",
                "wall_time_ms",
                "peak_rss_bytes",
                "response_recorded",
                "raw_output_recorded",
            }
        ),
        path="observed.execution",
        errors=errors,
    )
    if execution is not None:
        if execution.get("exit_code") is not None and not isinstance(
            execution.get("exit_code"), int
        ):
            errors.append("observed.execution.exit_code must be an integer or null")
        if not _is_nonnegative_number(execution.get("wall_time_ms")):
            errors.append("observed.execution.wall_time_ms must be non-negative")
        if execution.get("peak_rss_bytes") is not None and not _is_nonnegative_int(
            execution.get("peak_rss_bytes")
        ):
            errors.append(
                "observed.execution.peak_rss_bytes must be an integer or null"
            )
        if (
            execution.get("response_recorded") is not False
            or execution.get("raw_output_recorded") is not False
        ):
            errors.append("observed execution must not record response or raw output")

    usage = _check_keys(
        checked.get("usage_evidence"),
        allowed=frozenset(
            {
                "source",
                "read",
                "available_fields",
                "unavailable_reasons",
                "session_identifier_recorded",
                "raw_logs_recorded",
                "prompts_recorded",
            }
        ),
        required=frozenset(
            {
                "source",
                "read",
                "available_fields",
                "unavailable_reasons",
                "session_identifier_recorded",
                "raw_logs_recorded",
                "prompts_recorded",
            }
        ),
        path="observed.usage_evidence",
        errors=errors,
    )
    if usage is not None:
        _check_nonempty_string(
            usage.get("source"), "observed.usage_evidence.source", errors
        )
        available_fields = usage.get("available_fields")
        allowed_usage_fields = frozenset(
            {
                "api_calls",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            }
        )
        if not isinstance(available_fields, list) or any(
            not isinstance(field, str) or field not in allowed_usage_fields
            for field in (
                available_fields if isinstance(available_fields, list) else []
            )
        ):
            errors.append(
                "observed.usage_evidence.available_fields must be a string array"
            )
        elif len(available_fields) != len(set(available_fields)):
            errors.append("observed.usage_evidence.available_fields must be unique")
        unavailable_usage = usage.get("unavailable_reasons")
        if not isinstance(unavailable_usage, Mapping):
            errors.append(
                "observed.usage_evidence.unavailable_reasons must be an object"
            )
        else:
            allowed_unavailable = frozenset({*allowed_usage_fields, "process"})
            for key, value in unavailable_usage.items():
                if key not in allowed_unavailable:
                    errors.append(
                        f"observed.usage_evidence.unavailable_reasons.{key} is unknown"
                    )
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"observed.usage_evidence.unavailable_reasons.{key} must be non-empty text"
                    )
        for key in (
            "session_identifier_recorded",
            "raw_logs_recorded",
            "prompts_recorded",
        ):
            if usage.get(key) is not False:
                errors.append(f"observed.usage_evidence.{key} must be false")

    metrics = _check_keys(
        checked.get("metrics"),
        allowed=frozenset(
            {
                "model_calls",
                "tool_calls",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            }
        ),
        required=frozenset(
            {
                "model_calls",
                "tool_calls",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            }
        ),
        path="observed.metrics",
        errors=errors,
    )
    if metrics is not None:
        for key, value in metrics.items():
            if value is not None and not _is_nonnegative_int(value):
                errors.append(
                    f"observed.metrics.{key} must be a non-negative integer or null"
                )
        if metrics.get("tool_calls") != 0:
            errors.append(
                "observed.metrics.tool_calls must be zero for the empty toolset"
            )
    reasons = checked.get("metric_unavailable_reasons")
    if not isinstance(reasons, Mapping):
        errors.append("observed.metric_unavailable_reasons must be an object")
    else:
        allowed_reason_keys = frozenset(
            {
                "model_calls",
                "tool_calls",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "hermes_version",
                "hermes_source_revision",
                "factory_runtime_revision",
                "peak_rss_bytes",
                "process",
            }
        )
        for key, value in reasons.items():
            if key not in allowed_reason_keys:
                errors.append(f"observed.metric_unavailable_reasons.{key} is unknown")
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"observed.metric_unavailable_reasons.{key} must contain text"
                )

    fingerprint = checked.get("evidence_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", fingerprint
    ):
        errors.append("observed.evidence_fingerprint must be a sha256 fingerprint")
    elif fingerprint != _evidence_fingerprint(checked):
        errors.append(
            "observed.evidence_fingerprint does not match the sanitized record"
        )
    _check_forbidden_values(document, "observed", errors)
    return errors


def load_corpus(path: str | Path) -> dict[str, Any]:
    """Load one corpus file and fail closed if it is not a strict corpus."""

    corpus_path = Path(path)
    try:
        document = json.loads(corpus_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusValidationError(
            (f"cannot read corpus: {type(exc).__name__}",)
        ) from exc
    errors = validate_corpus(document)
    if errors:
        raise CorpusValidationError(errors)
    return document


def replay_corpus(source: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Compute a deterministic synthetic replay summary without side effects."""

    document = load_corpus(source) if isinstance(source, (str, Path)) else source
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
        "provenance": {
            "kind": "synthetic_test_vectors",
            "synthetic": True,
            "observed": False,
            "metrics_are_measurements": False,
        },
        "synthetic": True,
        "observed": False,
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


def _read_revision(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision if REVISION_RE.fullmatch(revision) else None


def _hermes_identity(executable: str) -> tuple[str | None, str | None]:
    """Read only version/revision identity; never persist the install path."""

    try:
        result = subprocess.run(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    output = result.stdout
    version_match = re.search(r"Hermes Agent v([^\s·]+)", output)
    upstream_match = re.search(r"upstream\s+([0-9a-f]{7,40})", output)
    version = version_match.group(1) if version_match else None
    revision = None
    install_match = re.search(r"^Install directory:\s*(\S+)\s*$", output, re.MULTILINE)
    if install_match:
        revision = _read_revision(Path(install_match.group(1)))
    if revision is None and upstream_match and len(upstream_match.group(1)) == 40:
        revision = upstream_match.group(1)
    return version, revision


def _children_peak_rss_bytes() -> int | None:
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    except (ImportError, AttributeError, OSError, ValueError):
        return None
    if not _is_nonnegative_number(value) or value <= 0:
        return None
    # Linux reports KiB; macOS reports bytes.  The observed environment is
    # Linux, but retaining the platform branch keeps the command repeatable.
    return int(value * 1024) if sys.platform.startswith("linux") else int(value)


def _safe_usage_value(usage: Mapping[str, Any], key: str) -> int | None:
    value = usage.get(key)
    return value if _is_nonnegative_int(value) else None


def _capture_record(profile: str, model: str) -> dict[str, Any]:
    if not _check_nonempty_string(
        profile, "profile", []
    ) or not IDENTIFIER_RE.fullmatch(profile):
        raise CaptureError("profile must be a safe identifier")
    if not isinstance(model, str) or not QUALIFIED_MODEL_RE.fullmatch(model):
        raise CaptureError("model must be a qualified model")

    started_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    hermes_version, hermes_source_revision = _hermes_identity("hermes")
    factory_revision = _read_revision(Path(__file__).resolve().parents[1])
    usage_data: Mapping[str, Any] = {}
    return_code: int | None = None
    timed_out = False
    usage_read = False
    launch_reason: str | None = None
    start = time.perf_counter()
    rss_before = _children_peak_rss_bytes()
    with tempfile.TemporaryDirectory(prefix="pydantic-baseline-") as temp_root:
        root = Path(temp_root)
        workspace = root / "workspace"
        workspace.mkdir()
        usage_path = root / "usage.json"
        command = [
            "hermes",
            "--profile",
            profile,
            "--ignore-rules",
            "-z",
            CAPTURE_PROMPT,
            "--usage-file",
            str(usage_path),
            "-m",
            model,
            "-t",
            CAPTURE_TOOLSET,
            "--in",
            str(workspace),
        ]
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=(os.name == "posix"),
            )
            try:
                process.communicate(timeout=CAPTURE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        process.kill()
                else:
                    process.kill()
                process.communicate()
            return_code = process.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            launch_reason = f"Hermes process unavailable ({type(exc).__name__})."
        finished = time.perf_counter()
        if usage_path.exists():
            try:
                candidate = json.loads(usage_path.read_text(encoding="utf-8"))
                if isinstance(candidate, Mapping):
                    usage_data = candidate
                    usage_read = True
            except (OSError, json.JSONDecodeError):
                usage_read = False

    wall_time_ms = round((finished - start) * 1000, 3)
    rss_after = _children_peak_rss_bytes()
    peak_rss_bytes = (
        rss_after
        if rss_after is not None and (rss_before is None or rss_after > rss_before)
        else None
    )
    metric_keys = (
        "model_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    )
    metrics = {
        "model_calls": _safe_usage_value(usage_data, "api_calls"),
        "tool_calls": 0,
        "input_tokens": _safe_usage_value(usage_data, "input_tokens"),
        "output_tokens": _safe_usage_value(usage_data, "output_tokens"),
        "cache_read_tokens": _safe_usage_value(usage_data, "cache_read_tokens"),
        "cache_write_tokens": _safe_usage_value(usage_data, "cache_write_tokens"),
    }
    unavailable_reasons: dict[str, str] = {}
    usage_reason = (
        "The sanitized Hermes --usage-file was not produced or was unreadable."
        if not usage_read
        else "Hermes --usage-file did not supply this metric."
    )
    for key in metric_keys:
        if metrics[key] is None:
            unavailable_reasons[key] = usage_reason
    unavailable_reasons["tool_calls"] = (
        (
            "The capture supplied an empty built-in toolset, so tool calls were not permitted."
        )
        if metrics["tool_calls"] == 0
        else ""
    )
    if hermes_version is None:
        unavailable_reasons["hermes_version"] = (
            "Hermes --version did not expose a parseable version."
        )
    if hermes_source_revision is None:
        unavailable_reasons["hermes_source_revision"] = (
            "The Hermes installation did not expose a full source revision."
        )
    if factory_revision is None:
        unavailable_reasons["factory_runtime_revision"] = (
            "The factory worktree revision was not readable."
        )
    if peak_rss_bytes is None:
        unavailable_reasons["peak_rss_bytes"] = (
            "The platform did not provide an isolated child-process peak RSS measurement."
        )
    if launch_reason:
        unavailable_reasons["process"] = launch_reason
    if timed_out:
        unavailable_reasons["process"] = (
            "The bounded Hermes process exceeded the capture timeout."
        )

    available_fields = sorted(
        key
        for key in (
            "api_calls",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
        if _safe_usage_value(usage_data, key) is not None
    )
    record: dict[str, Any] = {
        "schema_version": OBSERVED_SCHEMA_VERSION,
        "record_type": "observed_hermes_baseline",
        "provenance": {
            "kind": "observed_benign_hermes_smoke",
            "synthetic": False,
            "observed": True,
            "sanitized": True,
            "raw_prompts_recorded": False,
            "raw_logs_recorded": False,
            "redaction": (
                "Only bounded process measurements and selected usage counters are retained; "
                "the response, raw prompts, environment, credential paths, session identifiers, and logs are discarded."
            ),
        },
        "identity": {
            "hermes_profile": profile,
            "qualified_model": model,
            "hermes_version": hermes_version,
            "hermes_source_revision": hermes_source_revision,
            "factory_runtime_revision": factory_revision,
        },
        "command_contract": {
            "mode": "bounded_one_shot",
            "fixed_prompt": CAPTURE_PROMPT,
            "toolsets": [CAPTURE_TOOLSET],
            "tools_allowed": False,
            "repository_access": "none",
            "working_directory": "temporary_directory",
            "timeout_seconds": CAPTURE_TIMEOUT_SECONDS,
            "stdout_stderr": "discarded",
            "usage_evidence": "sanitized_usage_file",
        },
        "captured_at_utc": started_at,
        "execution": {
            "exit_code": return_code,
            "timed_out": timed_out,
            "wall_time_ms": wall_time_ms,
            "peak_rss_bytes": peak_rss_bytes,
            "response_recorded": False,
            "raw_output_recorded": False,
        },
        "usage_evidence": {
            "source": "Hermes one-shot --usage-file",
            "read": usage_read,
            "available_fields": available_fields,
            "unavailable_reasons": {
                key: value
                for key, value in unavailable_reasons.items()
                if key in metric_keys or key in {"process"}
            },
            "session_identifier_recorded": False,
            "raw_logs_recorded": False,
            "prompts_recorded": False,
        },
        "metrics": metrics,
        "metric_unavailable_reasons": unavailable_reasons,
    }
    record["evidence_fingerprint"] = _evidence_fingerprint(record)
    errors = validate_observed_record(record)
    if errors:
        raise CaptureError("captured record failed its sanitized schema")
    return record


def capture_baseline(
    output: str | Path, *, profile: str = CAPTURE_PROFILE, model: str = CAPTURE_MODEL
) -> dict[str, Any]:
    """Run the opt-in bounded smoke and write only its sanitized evidence."""

    record = _capture_record(profile, model)
    output_path = Path(output)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise CaptureError("could not write the sanitized observed record") from exc
    return record


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("validate", "validate the strict synthetic corpus without replaying it"),
        ("replay", "validate and print a deterministic synthetic replay summary"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("corpus", type=Path)
    capture = subparsers.add_parser(
        "capture",
        help="opt-in: run one benign Hermes smoke and write sanitized observed evidence",
    )
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--profile", default=CAPTURE_PROFILE)
    capture.add_argument("--model", default=CAPTURE_MODEL)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture":
        try:
            record = capture_baseline(
                args.output, profile=args.profile, model=args.model
            )
        except CaptureError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "captured": True,
                    "record_type": record["record_type"],
                    "observed": record["provenance"]["observed"],
                    "synthetic": record["provenance"]["synthetic"],
                    "evidence_fingerprint": record["evidence_fingerprint"],
                },
                sort_keys=True,
            )
        )
        return 0

    try:
        corpus = load_corpus(args.corpus)
    except CorpusValidationError as exc:
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    if args.command == "validate":
        print(
            f"valid synthetic test-vector corpus: {corpus['corpus_id']} "
            f"({len(corpus['fixtures'])} fixtures)"
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
