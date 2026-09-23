#!/usr/bin/env python3
"""Validate, replay, and explicitly capture the Pydantic-agent baseline.

``validate`` and ``replay`` consume only the checked-in synthetic corpus.  They
are deterministic, standard-library-only operations and never contact Hermes,
Git, a provider, a Factory task, or an external repository.  ``capture`` is a
separate, explicitly acknowledged operation.  It measures one real Hermes
implementer-profile one-shot and writes only a sanitized observation after all
fail-closed gates pass.
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
from urllib.parse import urlsplit

SCHEMA_VERSION = 3
OBSERVED_SCHEMA_VERSION = 2
CORPUS_ID = "pydantic-agent-factory-migration-v1"
BASE_REVISION = "0c32430ab1243e060f21bab98c109e4f21d0a402"

# These are deliberately constants.  The capture CLI does not expose profile or
# model overrides: changing either would make the checked-in baseline a
# different experiment.
CAPTURE_PROFILE = "implementer"
CAPTURE_MODEL = "openai-codex:gpt-5.6-luna"
CAPTURE_PROVIDER = "openai-codex"
CAPTURE_BARE_MODEL = "gpt-5.6-luna"
CAPTURE_PROMPT = "Do not use tools. Reply with exactly: Hermes baseline OK."
CAPTURE_RESPONSE = "Hermes baseline OK."
CAPTURE_TIMEOUT_SECONDS = 120
CAPTURE_HELPER_GRACE_SECONDS = 10

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
EXPECTED_MODEL_EVENT_ROUTES = {
    "implementation_success": ("openai_codex", "gpt-5.6-luna"),
    "test_failure": ("openai_codex", "gpt-5.6-luna"),
    "provider_retry": ("openai_codex", "gpt-5.6-luna"),
    "timeout": ("openai_codex", "gpt-5.6-luna"),
    "cancellation": ("openai_codex", "gpt-5.6-luna"),
    "review_approval": ("independent_review_provider", "gpt-5.6-luna"),
    "review_changes_requested": (
        "independent_review_provider",
        "gpt-5.6-luna",
    ),
    "review_incomplete": ("independent_review_provider", "gpt-5.6-luna"),
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
EXPECTED_RETRY_REASONS = {"provider_retry": "synthetic_rate_limit"}
EXPECTED_LIFECYCLE_REASONS = {
    "timeout": "deadline_exceeded",
    "cancellation": "operator_requested",
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

# Normalize separators and case before checking.  The synthetic corpus has no
# arbitrary text fields, but this recursive guard remains defense in depth for
# future schema edits and for observed-record redaction.
SECRET_KEY_NAMES = frozenset(
    {
        "apikey",
        "accesstoken",
        "authorization",
        "auth",
        "authpath",
        "bearer",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "environment",
        "env",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "session",
        "sessionid",
        "sessionidentifier",
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
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
LIVE_LOOKING_ID_RE = re.compile(
    r"(?i)(?:^|[-_ ])(?:prod(?:uction)?|live|task|issue|run|pr|kanban)(?:[-_ ]?\d+|$)"
)
PATH_PARENT_RE = re.compile(r"(?:^|[\s/\\])\.\.?(?:[/\\]|$)")
ABSOLUTE_PATH_RE = re.compile(r"^(?:[/\\]|~[/\\]|[A-Za-z]:[/\\])")

OBSERVED_METRIC_KEYS = (
    "model_calls",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "wall_time_ms",
    "peak_rss_bytes",
)
OBSERVED_INTEGER_METRICS = frozenset(
    {
        "model_calls",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "peak_rss_bytes",
    }
)
USAGE_METRIC_TO_FIELD = {
    "model_calls": "api_calls",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "cache_write_tokens": "cache_write_tokens",
}
USAGE_FIELDS = frozenset(
    {
        "api_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    }
)
UNAVAILABLE_REASON_CODES = frozenset(
    {"usage_field_absent", "usage_file_unreadable", "measurement_unavailable"}
)

PROFILE_CONTRACT_FILES = (
    "SOUL.md",
    "CAPABILITIES.md",
    "profile.yaml",
    "config.yaml",
)
NON_SECRET_PROFILE_PROMPTS = frozenset({"SOUL.md", "CAPABILITIES.md"})
ALLOWED_ENV_NAMES = frozenset(
    {
        "HOME",
        "PATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)
PROXY_ENV_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)


class CorpusValidationError(ValueError):
    """Raised when a corpus cannot be safely consumed."""

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class CaptureError(RuntimeError):
    """Raised when the opt-in observed capture cannot pass its gates."""


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
    try:
        keys = list(value)
    except (TypeError, ValueError, AttributeError):
        errors.append(f"{path} contains malformed fields")
        return None
    for key in keys:
        if not isinstance(key, str):
            errors.append(f"{path} contains a non-string field name")
            continue
        if key not in allowed:
            errors.append(f"{path}.{key} is an unknown field")
    try:
        present = set(keys)
    except (TypeError, ValueError, AttributeError):
        errors.append(f"{path} contains malformed fields")
        return None
    for key in sorted(required - present):
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


def _check_sha256(value: Any, path: str, errors: list[str]) -> bool:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        errors.append(f"{path} must be a sha256 fingerprint")
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
    if URI_SCHEME_RE.match(value) and not (
        normalized_path.endswith(
            (
                ".baseline.model",
                ".identity.qualified_model",
                ".command_contract.model",
                ".evidence_fingerprint",
                ".source_sha256",
                ".profile_contract_fingerprint",
                ".profile_contract.fingerprint",
            )
        )
        and (QUALIFIED_MODEL_RE.fullmatch(value) or SHA256_RE.fullmatch(value))
    ):
        errors.append(f"{path} contains an arbitrary URI scheme")
    if LIVE_LOOKING_ID_RE.search(value):
        errors.append(f"{path} contains a live-looking identifier")


def _check_forbidden_values(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        try:
            items = list(value.items())
        except (TypeError, ValueError, AttributeError):
            errors.append(f"{path} contains malformed fields")
            return
        for key, child in items:
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


def _event_spec(kind: str | None) -> tuple[frozenset[str], frozenset[str]]:
    common = {"kind", "latency_ms"}
    if kind == "model_call":
        return frozenset({*common, "provider", "model", "attempt"}), frozenset(
            {*common, "provider", "model", "attempt"}
        )
    if kind == "tool_call":
        return frozenset({*common, "name"}), frozenset({*common, "name"})
    if kind in {"provider_retry", "timeout", "cancellation_requested"}:
        return frozenset({*common, "reason"}), frozenset({*common, "reason"})
    return frozenset(common), frozenset(common)


def _check_event(event: Any, path: str, errors: list[str]) -> tuple[str, int]:
    if not _is_mapping(event):
        errors.append(f"{path} must be an object")
        return "", 0
    kind_value = event.get("kind")
    kind = kind_value if isinstance(kind_value, str) else None
    if kind not in {
        "model_call",
        "tool_call",
        "provider_retry",
        "timeout",
        "cancellation_requested",
    }:
        errors.append(f"{path}.kind is unsupported")
    allowed, required = _event_spec(kind)
    checked = _check_keys(
        event, allowed=allowed, required=required, path=path, errors=errors
    )
    if checked is None:
        return kind or "", 0
    latency = checked.get("latency_ms")
    if not _is_nonnegative_number(latency):
        errors.append(f"{path}.latency_ms must be non-negative")
    if kind == "model_call":
        _check_identifier(checked.get("provider"), f"{path}.provider", errors)
        _check_nonempty_string(checked.get("model"), f"{path}.model", errors)
        attempt = checked.get("attempt")
        if not _is_nonnegative_int(attempt) or attempt < 1:
            errors.append(f"{path}.attempt must be a positive integer")
    elif kind == "tool_call":
        _check_identifier(checked.get("name"), f"{path}.name", errors)
    elif kind in {"provider_retry", "timeout", "cancellation_requested"}:
        _check_identifier(checked.get("reason"), f"{path}.reason", errors)
    latency_value = int(latency) if _is_nonnegative_number(latency) else 0
    return kind or "", latency_value


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
    if not _is_nonnegative_int(findings):
        errors.append(f"{path}.findings must be a non-negative integer")
    elif scenario == "review_approval" and findings != 0:
        errors.append(f"{path}.findings must be zero for review_approval")
    elif scenario == "review_changes_requested" and findings < 1:
        errors.append(f"{path}.findings must be positive for review_changes_requested")
    elif scenario == "review_incomplete" and findings != 0:
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


def _safe_event_latency(event: Mapping[str, Any]) -> float | int:
    value = event.get("latency_ms")
    return value if _is_nonnegative_number(value) else 0


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
            _is_nonnegative_int(checked.get(key))
            if key in INTEGER_METRICS
            else _is_nonnegative_number(checked.get(key))
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
    event_latency = sum(_safe_event_latency(event) for event in events)
    model_latency = sum(_safe_event_latency(event) for event in model_events)
    tool_latency = sum(_safe_event_latency(event) for event in tool_events)
    retry_latency = sum(_safe_event_latency(event) for event in retry_events)
    lifecycle_latency = sum(
        _safe_event_latency(event) for event in (*timeout_events, *cancellation_events)
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
    if model_events:
        expected_first = (
            checked.get("queue_wait_ms", 0)
            + checked.get("startup_ms", 0)
            + _safe_event_latency(model_events[0])
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


def _validate_corpus(document: Any, errors: list[str]) -> None:
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
        return
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
        if baseline.get("hermes_profile") != CAPTURE_PROFILE:
            errors.append("corpus.baseline.hermes_profile must be implementer")
        if baseline.get("model") != CAPTURE_MODEL:
            errors.append(f"corpus.baseline.model must be {CAPTURE_MODEL}")
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
        if provenance.get("redaction") != (
            "Synthetic vectors only; no observed task data or arbitrary text is retained."
        ):
            errors.append(
                "corpus.provenance.redaction is not the fixed synthetic literal"
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
                {
                    "task_id",
                    "scenario_code",
                    "repository",
                    "workspace",
                    "external_task_id",
                }
            ),
            required=frozenset(
                {
                    "task_id",
                    "scenario_code",
                    "repository",
                    "workspace",
                    "external_task_id",
                }
            ),
            path=f"{prefix}.task",
            errors=errors,
        )
        if task is not None:
            if task.get("task_id") != fixture_id:
                errors.append(f"{prefix}.task.task_id must equal the fixture id")
            if task.get("scenario_code") != scenario:
                errors.append(f"{prefix}.task.scenario_code must equal the scenario")
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
            expected_provider, expected_model = EXPECTED_MODEL_EVENT_ROUTES[scenario]
            for event_index, event in enumerate(event_rows):
                event_path = f"{prefix}.events[{event_index}]"
                if event.get("kind") == "model_call":
                    if event.get("provider") != expected_provider:
                        errors.append(
                            f"{event_path}.provider is not the fixed scenario provider"
                        )
                    if event.get("model") != expected_model:
                        errors.append(
                            f"{event_path}.model is not the fixed scenario model"
                        )
                elif event.get("kind") == "provider_retry":
                    if event.get("reason") != EXPECTED_RETRY_REASONS["provider_retry"]:
                        errors.append(
                            f"{event_path}.reason is not the fixed retry code"
                        )
                elif event.get("kind") in {"timeout", "cancellation_requested"}:
                    if event.get("reason") != EXPECTED_LIFECYCLE_REASONS[scenario]:
                        errors.append(
                            f"{event_path}.reason is not the fixed lifecycle code"
                        )
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
            scenario if isinstance(scenario, str) else "",
            f"{prefix}.metrics",
            errors,
        )

        outcome = _check_keys(
            checked_fixture.get("durable_outcome"),
            allowed=frozenset(
                {"status", "terminal", "task_state", "reason", "outcome_code"}
            ),
            required=frozenset(
                {"status", "terminal", "task_state", "reason", "outcome_code"}
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
            if outcome.get("outcome_code") != scenario:
                errors.append(
                    f"{prefix}.durable_outcome.outcome_code must equal the scenario"
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


def validate_corpus(document: Any) -> list[str]:
    """Return deterministic errors and never raise for malformed JSON values."""

    errors: list[str] = []
    try:
        _validate_corpus(document, errors)
    except (TypeError, KeyError, IndexError, AttributeError, ValueError):
        errors.append("corpus contains malformed JSON values")
    return errors


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _evidence_fingerprint(record: Mapping[str, Any]) -> str:
    payload = dict(record)
    payload.pop("evidence_fingerprint", None)
    return "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()


def _validate_observed_record(document: Any, errors: list[str]) -> None:
    checked = _check_keys(
        document,
        allowed=frozenset(
            {
                "schema_version",
                "record_type",
                "provenance",
                "identity",
                "source_binding",
                "profile_contract",
                "environment_contract",
                "command_contract",
                "local_side_effects",
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
                "source_binding",
                "profile_contract",
                "environment_contract",
                "command_contract",
                "local_side_effects",
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
        return
    if checked.get("schema_version") != OBSERVED_SCHEMA_VERSION:
        errors.append(f"observed.schema_version must be {OBSERVED_SCHEMA_VERSION}")
    if checked.get("record_type") != "observed_hermes_baseline":
        errors.append("observed.record_type is unsupported")
    captured_at = checked.get("captured_at_utc")
    if not isinstance(captured_at, str) or not TIMESTAMP_RE.fullmatch(captured_at):
        errors.append("observed.captured_at_utc must be a UTC timestamp")

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
                "raw_output_recorded",
                "fingerprint_semantics",
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
                "raw_output_recorded",
                "fingerprint_semantics",
                "redaction",
            }
        ),
        path="observed.provenance",
        errors=errors,
    )
    if provenance is not None:
        if provenance.get("kind") != "observed_benign_hermes_smoke":
            errors.append("observed.provenance.kind is unsupported")
        for key in (
            "synthetic",
            "raw_prompts_recorded",
            "raw_logs_recorded",
            "raw_output_recorded",
        ):
            if provenance.get(key) is not False:
                errors.append(f"observed.provenance.{key} must be false")
        for key in ("observed", "sanitized"):
            if provenance.get(key) is not True:
                errors.append(f"observed.provenance.{key} must be true")
        if (
            provenance.get("fingerprint_semantics")
            != "self_consistency_digest_not_attestation"
        ):
            errors.append("observed.provenance.fingerprint_semantics is unsupported")
        if provenance.get("redaction") != (
            "Response, stderr, raw usage failures, prompts beyond the fixed public phrase, environment values, credentials, session identifiers, and logs are not retained."
        ):
            errors.append("observed.provenance.redaction is not the fixed literal")

    identity = _check_keys(
        checked.get("identity"),
        allowed=frozenset(
            {
                "hermes_profile",
                "qualified_model",
                "hermes_version",
                "hermes_source_revision",
                "capture_source_revision",
                "factory_base_revision",
                "factory_head_revision",
                "clean_before_capture",
            }
        ),
        required=frozenset(
            {
                "hermes_profile",
                "qualified_model",
                "hermes_version",
                "hermes_source_revision",
                "capture_source_revision",
                "factory_base_revision",
                "factory_head_revision",
                "clean_before_capture",
            }
        ),
        path="observed.identity",
        errors=errors,
    )
    if identity is not None:
        if identity.get("hermes_profile") != CAPTURE_PROFILE:
            errors.append("observed.identity.hermes_profile must be implementer")
        if identity.get("qualified_model") != CAPTURE_MODEL:
            errors.append(f"observed.identity.qualified_model must be {CAPTURE_MODEL}")
        version = identity.get("hermes_version")
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            errors.append("observed.identity.hermes_version must be a version")
        for key in (
            "hermes_source_revision",
            "capture_source_revision",
            "factory_base_revision",
            "factory_head_revision",
        ):
            _check_revision(identity.get(key), f"observed.identity.{key}", errors)
        if identity.get("factory_base_revision") != BASE_REVISION:
            errors.append(
                "observed.identity.factory_base_revision does not match the admitted base"
            )
        if identity.get("factory_head_revision") != identity.get(
            "capture_source_revision"
        ):
            errors.append(
                "observed identity head and capture source revision must agree"
            )
        if identity.get("clean_before_capture") is not True:
            errors.append("observed.identity.clean_before_capture must be true")

    source_binding = _check_keys(
        checked.get("source_binding"),
        allowed=frozenset(
            {"capture_source_revision", "source_sha256", "fingerprint_scope"}
        ),
        required=frozenset(
            {"capture_source_revision", "source_sha256", "fingerprint_scope"}
        ),
        path="observed.source_binding",
        errors=errors,
    )
    if source_binding is not None:
        _check_revision(
            source_binding.get("capture_source_revision"),
            "observed.source_binding.capture_source_revision",
            errors,
        )
        _check_sha256(
            source_binding.get("source_sha256"),
            "observed.source_binding.source_sha256",
            errors,
        )
        if source_binding.get("fingerprint_scope") != "capture_executable_source_bytes":
            errors.append("observed.source_binding.fingerprint_scope is unsupported")
        if identity is not None and source_binding.get(
            "capture_source_revision"
        ) != identity.get("capture_source_revision"):
            errors.append(
                "observed.source_binding.capture_source_revision does not match identity"
            )
        current_source = _capture_source_sha256()
        if (
            current_source is None
            or source_binding.get("source_sha256") != current_source
        ):
            errors.append(
                "observed.source_binding.source_sha256 does not match capture source"
            )

    profile_contract = _check_keys(
        checked.get("profile_contract"),
        allowed=frozenset(
            {
                "fingerprint",
                "metadata_only",
                "contents_recorded",
                "auth_material_recorded",
            }
        ),
        required=frozenset(
            {
                "fingerprint",
                "metadata_only",
                "contents_recorded",
                "auth_material_recorded",
            }
        ),
        path="observed.profile_contract",
        errors=errors,
    )
    if profile_contract is not None:
        _check_sha256(
            profile_contract.get("fingerprint"),
            "observed.profile_contract.fingerprint",
            errors,
        )
        for key in ("metadata_only",):
            if profile_contract.get(key) is not True:
                errors.append(f"observed.profile_contract.{key} must be true")
        for key in ("contents_recorded", "auth_material_recorded"):
            if profile_contract.get(key) is not False:
                errors.append(f"observed.profile_contract.{key} must be false")
        # This digest describes the capture-time profile contract. Offline
        # validation must not depend on the validator having that local Hermes
        # profile installed; capture generation itself requires and hashes it.

    environment_contract = _check_keys(
        checked.get("environment_contract"),
        allowed=frozenset(
            {
                "allowlist_categories",
                "control_plane_variables_removed",
                "kanban_variables_removed",
                "secret_environment_passed",
                "values_recorded",
            }
        ),
        required=frozenset(
            {
                "allowlist_categories",
                "control_plane_variables_removed",
                "kanban_variables_removed",
                "secret_environment_passed",
                "values_recorded",
            }
        ),
        path="observed.environment_contract",
        errors=errors,
    )
    if environment_contract is not None:
        if environment_contract.get("allowlist_categories") != [
            "HOME",
            "PATH",
            "LOCALE",
            "TLS_PROXY",
        ]:
            errors.append(
                "observed.environment_contract.allowlist_categories is unsupported"
            )
        for key in ("control_plane_variables_removed", "kanban_variables_removed"):
            if environment_contract.get(key) is not True:
                errors.append(f"observed.environment_contract.{key} must be true")
        for key in ("secret_environment_passed", "values_recorded"):
            if environment_contract.get(key) is not False:
                errors.append(f"observed.environment_contract.{key} must be false")

    command = _check_keys(
        checked.get("command_contract"),
        allowed=frozenset(
            {
                "mode",
                "profile",
                "model",
                "fixed_prompt",
                "expected_response",
                "tools",
                "tool_override",
                "repository_access",
                "working_directory",
                "timeout_seconds",
                "stdout",
                "stderr",
                "usage_evidence",
                "acknowledgement",
            }
        ),
        required=frozenset(
            {
                "mode",
                "profile",
                "model",
                "fixed_prompt",
                "expected_response",
                "tools",
                "tool_override",
                "repository_access",
                "working_directory",
                "timeout_seconds",
                "stdout",
                "stderr",
                "usage_evidence",
                "acknowledgement",
            }
        ),
        path="observed.command_contract",
        errors=errors,
    )
    if command is not None:
        expected_command = {
            "mode": "bounded_one_shot",
            "profile": CAPTURE_PROFILE,
            "model": CAPTURE_MODEL,
            "fixed_prompt": CAPTURE_PROMPT,
            "expected_response": CAPTURE_RESPONSE,
            "tools": "profile_default",
            "tool_override": "none",
            "repository_access": "none",
            "working_directory": "fresh_temporary_non_git",
            "timeout_seconds": CAPTURE_TIMEOUT_SECONDS,
            "stdout": "captured_in_memory_then_discarded",
            "stderr": "not_captured",
            "usage_evidence": "sanitized_usage_file",
            "acknowledgement": "ack_local_hermes_persistence_required",
        }
        for key, expected in expected_command.items():
            if command.get(key) != expected:
                errors.append(
                    f"observed.command_contract.{key} does not match capture contract"
                )

    side_effects = _check_keys(
        checked.get("local_side_effects"),
        allowed=frozenset(
            {
                "credential_store_read",
                "profile_session_db_writes",
                "profile_log_writes",
                "factory_task_mutations",
                "external_repository_mutations",
                "observed_record_write",
            }
        ),
        required=frozenset(
            {
                "credential_store_read",
                "profile_session_db_writes",
                "profile_log_writes",
                "factory_task_mutations",
                "external_repository_mutations",
                "observed_record_write",
            }
        ),
        path="observed.local_side_effects",
        errors=errors,
    )
    if side_effects is not None:
        expected_side_effects = {
            "credential_store_read": "expected",
            "profile_session_db_writes": "expected",
            "profile_log_writes": "expected",
            "factory_task_mutations": "not_performed_by_contract",
            "external_repository_mutations": "not_performed_by_contract",
            "observed_record_write": "checked_in_record_only",
        }
        for key, expected in expected_side_effects.items():
            if side_effects.get(key) != expected:
                errors.append(f"observed.local_side_effects.{key} is not truthful")

    execution = _check_keys(
        checked.get("execution"),
        allowed=frozenset(
            {
                "exit_code",
                "timed_out",
                "response_contract_satisfied",
                "wall_time_ms",
                "peak_rss_bytes",
                "rss_scope",
                "measurement_helper",
                "stdout_recorded",
                "stderr_recorded",
                "cwd_non_git",
            }
        ),
        required=frozenset(
            {
                "exit_code",
                "timed_out",
                "response_contract_satisfied",
                "wall_time_ms",
                "peak_rss_bytes",
                "rss_scope",
                "measurement_helper",
                "stdout_recorded",
                "stderr_recorded",
                "cwd_non_git",
            }
        ),
        path="observed.execution",
        errors=errors,
    )
    if execution is not None:
        if execution.get("exit_code") != 0:
            errors.append("observed.execution.exit_code must be zero")
        if execution.get("timed_out") is not False:
            errors.append("observed.execution.timed_out must be false")
        if execution.get("response_contract_satisfied") is not True:
            errors.append("observed.execution.response_contract_satisfied must be true")
        if (
            not _is_nonnegative_number(execution.get("wall_time_ms"))
            or execution.get("wall_time_ms") <= 0
        ):
            errors.append("observed.execution.wall_time_ms must be positive")
        if (
            not _is_nonnegative_int(execution.get("peak_rss_bytes"))
            or execution.get("peak_rss_bytes") <= 0
        ):
            errors.append("observed.execution.peak_rss_bytes must be positive")
        if execution.get("rss_scope") != "max_child_rss_not_aggregate_process_tree":
            errors.append("observed.execution.rss_scope is unsupported")
        if execution.get("measurement_helper") != "fresh_helper_only_child_hermes":
            errors.append("observed.execution.measurement_helper is unsupported")
        for key in ("stdout_recorded", "stderr_recorded"):
            if execution.get(key) is not False:
                errors.append(f"observed.execution.{key} must be false")
        if execution.get("cwd_non_git") is not True:
            errors.append("observed.execution.cwd_non_git must be true")

    usage = _check_keys(
        checked.get("usage_evidence"),
        allowed=frozenset(
            {
                "source",
                "read",
                "provider",
                "model",
                "available_fields",
                "unavailable_reasons",
                "completed",
                "failed",
                "session_identifier_recorded",
                "raw_logs_recorded",
                "prompts_recorded",
            }
        ),
        required=frozenset(
            {
                "source",
                "read",
                "provider",
                "model",
                "available_fields",
                "unavailable_reasons",
                "completed",
                "failed",
                "session_identifier_recorded",
                "raw_logs_recorded",
                "prompts_recorded",
            }
        ),
        path="observed.usage_evidence",
        errors=errors,
    )
    usage_unavailable: Mapping[str, Any] = {}
    if usage is not None:
        if usage.get("source") != "Hermes one-shot --usage-file":
            errors.append("observed.usage_evidence.source is unsupported")
        if usage.get("read") is not True:
            errors.append("observed.usage_evidence.read must be true")
        if usage.get("provider") != CAPTURE_PROVIDER:
            errors.append("observed.usage_evidence.provider does not match Codex route")
        if usage.get("model") != CAPTURE_BARE_MODEL:
            errors.append("observed.usage_evidence.model does not match Codex route")
        if usage.get("completed") is not True:
            errors.append("observed.usage_evidence.completed must be true")
        if usage.get("failed") is not False:
            errors.append("observed.usage_evidence.failed must be false")
        available_fields = usage.get("available_fields")
        if not isinstance(available_fields, list) or any(
            not isinstance(field, str) or field not in USAGE_FIELDS
            for field in available_fields
        ):
            errors.append(
                "observed.usage_evidence.available_fields must be a usage-field array"
            )
            available_fields = []
        elif len(available_fields) != len(set(available_fields)):
            errors.append("observed.usage_evidence.available_fields must be unique")
        usage_unavailable_value = usage.get("unavailable_reasons")
        if not isinstance(usage_unavailable_value, Mapping):
            errors.append(
                "observed.usage_evidence.unavailable_reasons must be an object"
            )
        else:
            usage_unavailable = usage_unavailable_value
            for key, value in usage_unavailable.items():
                if key not in USAGE_FIELDS - {"api_calls"}:
                    errors.append(
                        f"observed.usage_evidence.unavailable_reasons.{key} is unknown"
                    )
                if value not in UNAVAILABLE_REASON_CODES:
                    errors.append(
                        f"observed.usage_evidence.unavailable_reasons.{key} is not an enum code"
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
        allowed=frozenset(OBSERVED_METRIC_KEYS),
        required=frozenset(OBSERVED_METRIC_KEYS),
        path="observed.metrics",
        errors=errors,
    )
    null_metrics: set[str] = set()
    if metrics is not None:
        for key in OBSERVED_METRIC_KEYS:
            value = metrics.get(key)
            if value is None:
                null_metrics.add(key)
            elif key in OBSERVED_INTEGER_METRICS and not _is_nonnegative_int(value):
                errors.append(
                    f"observed.metrics.{key} must be a non-negative integer or null"
                )
            elif key == "wall_time_ms" and not _is_nonnegative_number(value):
                errors.append(
                    "observed.metrics.wall_time_ms must be a non-negative number or null"
                )
        if metrics.get("model_calls") != 1:
            errors.append(
                "observed.metrics.model_calls must equal one usage-file api call"
            )
        if metrics.get("tool_calls") != 0:
            errors.append("observed.metrics.tool_calls must be zero")
        if execution is not None:
            if metrics.get("wall_time_ms") != execution.get("wall_time_ms"):
                errors.append("observed.metrics.wall_time_ms must match execution")
            if metrics.get("peak_rss_bytes") != execution.get("peak_rss_bytes"):
                errors.append("observed.metrics.peak_rss_bytes must match execution")

    reasons = checked.get("metric_unavailable_reasons")
    if not isinstance(reasons, Mapping):
        errors.append("observed.metric_unavailable_reasons must be an object")
        reasons = {}
    reason_keys = set(reasons)
    unknown_reason_keys = reason_keys - set(OBSERVED_METRIC_KEYS)
    for key in sorted(unknown_reason_keys):
        errors.append(f"observed.metric_unavailable_reasons.{key} is unknown")
    if reason_keys != null_metrics:
        errors.append(
            "observed.metric_unavailable_reasons must exactly cover null metrics"
        )
    for key in sorted(reason_keys & set(OBSERVED_METRIC_KEYS)):
        if reasons.get(key) not in UNAVAILABLE_REASON_CODES:
            errors.append(
                f"observed.metric_unavailable_reasons.{key} is not an enum code"
            )
    expected_nested = {
        field: reasons[metric]
        for metric, field in USAGE_METRIC_TO_FIELD.items()
        if metric in null_metrics and field != "api_calls"
    }
    if dict(usage_unavailable) != expected_nested:
        errors.append("nested and top-level metric availability reasons disagree")
    if usage is not None:
        available_fields = usage.get("available_fields")
        if isinstance(available_fields, list):
            expected_available = {
                field
                for metric, field in USAGE_METRIC_TO_FIELD.items()
                if metric not in null_metrics
            }
            if set(available_fields) != expected_available:
                errors.append("usage available_fields do not match non-null metrics")

    fingerprint = checked.get("evidence_fingerprint")
    if not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint):
        errors.append("observed.evidence_fingerprint must be a sha256 fingerprint")
    elif fingerprint != _evidence_fingerprint(checked):
        errors.append(
            "observed.evidence_fingerprint does not match the sanitized record"
        )
    _check_forbidden_values(document, "observed", errors)


def validate_observed_record(document: Any) -> list[str]:
    """Return strict observed-record errors without raising on malformed values."""

    errors: list[str] = []
    try:
        _validate_observed_record(document, errors)
    except (TypeError, KeyError, IndexError, AttributeError, ValueError):
        errors.append("observed record contains malformed JSON values")
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
    if not isinstance(document, dict):
        raise CorpusValidationError(("corpus must be an object",))
    return document


def replay_corpus(source: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Compute a deterministic synthetic replay summary without side effects."""

    document = load_corpus(source) if isinstance(source, (str, Path)) else source
    errors = validate_corpus(document)
    if errors:
        raise CorpusValidationError(errors)
    if not isinstance(document, Mapping):
        raise CorpusValidationError(("corpus must be an object",))
    fixtures = document.get("fixtures")
    if not isinstance(fixtures, list):
        raise CorpusValidationError(("corpus.fixtures must be an array",))
    try:
        outcomes = Counter(fixture["durable_outcome"]["status"] for fixture in fixtures)
        totals = {
            key: sum(fixture["metrics"][key] for fixture in fixtures)
            for key in REPLAY_SUM_METRICS
        }
        result = {
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
    except (TypeError, KeyError, IndexError, AttributeError, ValueError) as exc:
        raise CorpusValidationError(("corpus contains malformed JSON values",)) from exc
    return result


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


def _git_worktree_is_clean(cwd: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "status", "--porcelain", "--untracked-files=all"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout == ""


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
    version = version_match.group(1) if version_match else None
    revision = None
    install_match = re.search(r"^Install directory:\s*(\S+)\s*$", output, re.MULTILINE)
    if install_match:
        revision = _read_revision(Path(install_match.group(1)))
    return version, revision


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except (OSError, ValueError):
        return None
    return "sha256:" + digest.hexdigest()


def _config_key_metadata(path: Path) -> list[str]:
    """Return key/indent metadata only; never include config values."""

    keys: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key = stripped.split(":", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z0-9_.-]+", key):
                keys.append(f"{len(line) - len(stripped)}:{key}")
    except (OSError, UnicodeError):
        return []
    return keys


def _profile_contract_fingerprint(profile: str) -> str | None:
    """Hash stable, non-secret profile contract metadata without storing contents."""

    home = os.environ.get("HOME")
    if not isinstance(home, str) or not home:
        return None
    root = Path(home) / ".hermes" / "profiles" / profile
    metadata: dict[str, Any] = {"profile": profile, "files": []}
    for relative in PROFILE_CONTRACT_FILES:
        path = root / relative
        item: dict[str, Any] = {"path": relative, "present": path.is_file()}
        if path.is_file():
            try:
                item["size"] = path.stat().st_size
            except OSError:
                item["size"] = None
            if relative in NON_SECRET_PROFILE_PROMPTS:
                item["sha256"] = _sha256_file(path)
            elif relative == "config.yaml":
                item["key_metadata"] = _config_key_metadata(path)
        metadata["files"].append(item)
    try:
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError):
        return None
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _capture_source_sha256() -> str | None:
    return _sha256_file(Path(__file__).resolve())


def _proxy_value_is_non_secret(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.username is None and parsed.password is None


def _scrubbed_environment() -> dict[str, str]:
    """Build the narrow environment allowlist used by the real Hermes child."""

    scrubbed: dict[str, str] = {}
    for key, value in os.environ.items():
        if key not in ALLOWED_ENV_NAMES and not key.startswith("LC_"):
            continue
        if key in PROXY_ENV_NAMES and not _proxy_value_is_non_secret(value):
            continue
        scrubbed[key] = value
    return scrubbed


def _usage_int(candidate: Mapping[str, Any], key: str) -> int | None:
    value = candidate.get(key)
    return value if _is_nonnegative_int(value) else None


def _children_peak_rss_bytes() -> int | None:
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    except (ImportError, AttributeError, OSError, ValueError):
        return None
    if not _is_nonnegative_number(value) or value <= 0:
        return None
    # Linux reports KiB; macOS reports bytes.  The record states that this is
    # max direct-child RSS, not aggregate process-tree RSS.
    return int(value * 1024) if sys.platform.startswith("linux") else int(value)


def _kill_process_group(process: subprocess.Popen[Any]) -> None:
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _sanitized_usage(candidate: Any) -> dict[str, Any] | None:
    """Extract only typed, non-secret usage evidence from Hermes' report."""

    if not isinstance(candidate, Mapping):
        return None
    completed = candidate.get("completed")
    failed = candidate.get("failed")
    provider = candidate.get("provider")
    model = candidate.get("model")
    api_calls = candidate.get("api_calls")
    if (
        not isinstance(completed, bool)
        or not isinstance(failed, bool)
        or not isinstance(provider, str)
        or not isinstance(model, str)
        or not _is_nonnegative_int(api_calls)
    ):
        return None
    values: dict[str, int] = {"api_calls": api_calls}
    for field in USAGE_FIELDS - {"api_calls"}:
        if field in candidate:
            value = candidate.get(field)
            if value is None:
                continue
            if not _is_nonnegative_int(value):
                return None
            values[field] = value
    return {
        "completed": completed,
        "failed": failed,
        "route_matches": provider == CAPTURE_PROVIDER and model == CAPTURE_BARE_MODEL,
        "api_calls": api_calls,
        "values": values,
    }


def _hermes_capture_command(usage_path: Path) -> list[str]:
    """Return the fixed provider/model route used by the observed capture."""

    return [
        "hermes",
        "--profile",
        CAPTURE_PROFILE,
        "-z",
        CAPTURE_PROMPT,
        "--usage-file",
        str(usage_path),
        "--provider",
        CAPTURE_PROVIDER,
        "-m",
        CAPTURE_BARE_MODEL,
    ]


def _measurement_helper(usage_path: Path, workspace: Path) -> dict[str, Any]:
    """Run exactly one measured Hermes child and emit only sanitized metadata."""

    result: dict[str, Any] = {
        "exit_code": None,
        "timed_out": False,
        "response_exact": False,
        "usage_read": False,
        "usage_contract": None,
        "peak_rss_bytes": None,
        "helper_error": None,
    }
    process: subprocess.Popen[bytes] | None = None
    try:
        before_rss = _children_peak_rss_bytes()
        process = subprocess.Popen(
            _hermes_capture_command(usage_path),
            cwd=workspace,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=False,
        )
        try:
            stdout, _ = process.communicate(timeout=CAPTURE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            _kill_process_group(process)
            stdout, _ = process.communicate()
        result["exit_code"] = process.returncode
        # Hermes oneshot writes the final response followed by one newline.  No
        # trim or normalization is allowed: this is an exact response gate.
        result["response_exact"] = stdout == (CAPTURE_RESPONSE + "\n").encode("utf-8")
        if usage_path.is_file():
            result["usage_read"] = True
            try:
                candidate = json.loads(usage_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                candidate = None
            result["usage_contract"] = _sanitized_usage(candidate)
        after_rss = _children_peak_rss_bytes()
        if after_rss is not None and (before_rss is None or after_rss > before_rss):
            result["peak_rss_bytes"] = after_rss
    except (OSError, subprocess.SubprocessError):
        result["helper_error"] = "process_unavailable"
    except (TypeError, ValueError, UnicodeError):
        result["helper_error"] = "measurement_invalid"
    return result


def _measurement_helper_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--usage-path", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args(list(argv))
    try:
        result = _measurement_helper(args.usage_path, args.workspace)
        sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
        sys.stdout.flush()
        return 0
    except (
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        RuntimeError,
        KeyError,
        IndexError,
        AttributeError,
    ):
        # Never expose an exception, usage failure string, environment, or
        # session identifier through the helper boundary.
        sys.stdout.write(json.dumps({"helper_error": "measurement_invalid"}))
        sys.stdout.flush()
        return 0


def _run_measurement_helper(
    usage_path: Path, workspace: Path, environment: Mapping[str, str]
) -> dict[str, Any] | None:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_measure-hermes",
        "--usage-path",
        str(usage_path),
        "--workspace",
        str(workspace),
    ]
    helper: subprocess.Popen[Any] | None = None
    try:
        helper = subprocess.Popen(
            command,
            cwd=workspace,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=(os.name == "posix"),
        )
        try:
            stdout, _ = helper.communicate(
                timeout=CAPTURE_TIMEOUT_SECONDS + CAPTURE_HELPER_GRACE_SECONDS
            )
        except subprocess.TimeoutExpired:
            _kill_process_group(helper)
            stdout, _ = helper.communicate()
        if helper.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        if helper is not None:
            _kill_process_group(helper)
        return None
    try:
        result = json.loads(stdout)
    except (TypeError, UnicodeError, json.JSONDecodeError):
        return None
    return result if isinstance(result, dict) else None


def _capture_record() -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[1]
    if not _git_worktree_is_clean(repository):
        raise CaptureError("capture requires a clean factory worktree")
    factory_head = _read_revision(repository)
    hermes_version, hermes_source_revision = _hermes_identity("hermes")
    source_sha256 = _capture_source_sha256()
    profile_fingerprint = _profile_contract_fingerprint(CAPTURE_PROFILE)
    if not factory_head or not hermes_version or not hermes_source_revision:
        raise CaptureError("capture identity probes were incomplete")
    if not source_sha256 or not profile_fingerprint:
        raise CaptureError("capture source or profile fingerprint was unavailable")

    started_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    environment = _scrubbed_environment()
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="pydantic-baseline-") as temp_root:
        root = Path(temp_root)
        workspace = root / "workspace"
        workspace.mkdir()
        usage_path = root / "usage.json"
        if (workspace / ".git").exists():
            raise CaptureError("capture workspace was not non-git")
        helper_result = _run_measurement_helper(usage_path, workspace, environment)
    wall_time_ms = round((time.perf_counter() - started) * 1000, 3)

    if not isinstance(helper_result, Mapping):
        raise CaptureError("capture measurement helper did not return evidence")
    usage_contract = helper_result.get("usage_contract")
    gates = (
        helper_result.get("exit_code") == 0,
        helper_result.get("timed_out") is False,
        helper_result.get("response_exact") is True,
        helper_result.get("usage_read") is True,
        isinstance(usage_contract, Mapping),
        usage_contract.get("completed") is True
        if isinstance(usage_contract, Mapping)
        else False,
        usage_contract.get("failed") is False
        if isinstance(usage_contract, Mapping)
        else False,
        usage_contract.get("route_matches") is True
        if isinstance(usage_contract, Mapping)
        else False,
        usage_contract.get("api_calls") == 1
        if isinstance(usage_contract, Mapping)
        else False,
        _is_nonnegative_int(helper_result.get("peak_rss_bytes"))
        and helper_result.get("peak_rss_bytes") > 0,
    )
    if not all(gates):
        raise CaptureError("capture failed a required Hermes evidence gate")

    values = usage_contract["values"]
    metrics: dict[str, Any] = {
        "model_calls": values["api_calls"],
        # One exact response from one provider call is the evidence basis for
        # zero tool roundtrips.  The full profile-default tools remain enabled.
        "tool_calls": 0,
        "input_tokens": values.get("input_tokens"),
        "output_tokens": values.get("output_tokens"),
        "cache_read_tokens": values.get("cache_read_tokens"),
        "cache_write_tokens": values.get("cache_write_tokens"),
        "wall_time_ms": wall_time_ms,
        "peak_rss_bytes": helper_result["peak_rss_bytes"],
    }
    unavailable_reasons = {
        metric: "usage_field_absent"
        for metric, field in USAGE_METRIC_TO_FIELD.items()
        if metrics[metric] is None and field != "api_calls"
    }
    available_fields = sorted(
        field for field in USAGE_FIELDS if field in values and field != "api_calls"
    )
    available_fields.insert(0, "api_calls")
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
            "raw_output_recorded": False,
            "fingerprint_semantics": "self_consistency_digest_not_attestation",
            "redaction": (
                "Response, stderr, raw usage failures, prompts beyond the fixed public phrase, environment values, credentials, session identifiers, and logs are not retained."
            ),
        },
        "identity": {
            "hermes_profile": CAPTURE_PROFILE,
            "qualified_model": CAPTURE_MODEL,
            "hermes_version": hermes_version,
            "hermes_source_revision": hermes_source_revision,
            "capture_source_revision": factory_head,
            "factory_base_revision": BASE_REVISION,
            "factory_head_revision": factory_head,
            "clean_before_capture": True,
        },
        "source_binding": {
            "capture_source_revision": factory_head,
            "source_sha256": source_sha256,
            "fingerprint_scope": "capture_executable_source_bytes",
        },
        "profile_contract": {
            "fingerprint": profile_fingerprint,
            "metadata_only": True,
            "contents_recorded": False,
            "auth_material_recorded": False,
        },
        "environment_contract": {
            "allowlist_categories": ["HOME", "PATH", "LOCALE", "TLS_PROXY"],
            "control_plane_variables_removed": True,
            "kanban_variables_removed": True,
            "secret_environment_passed": False,
            "values_recorded": False,
        },
        "command_contract": {
            "mode": "bounded_one_shot",
            "profile": CAPTURE_PROFILE,
            "model": CAPTURE_MODEL,
            "fixed_prompt": CAPTURE_PROMPT,
            "expected_response": CAPTURE_RESPONSE,
            "tools": "profile_default",
            "tool_override": "none",
            "repository_access": "none",
            "working_directory": "fresh_temporary_non_git",
            "timeout_seconds": CAPTURE_TIMEOUT_SECONDS,
            "stdout": "captured_in_memory_then_discarded",
            "stderr": "not_captured",
            "usage_evidence": "sanitized_usage_file",
            "acknowledgement": "ack_local_hermes_persistence_required",
        },
        "local_side_effects": {
            "credential_store_read": "expected",
            "profile_session_db_writes": "expected",
            "profile_log_writes": "expected",
            "factory_task_mutations": "not_performed_by_contract",
            "external_repository_mutations": "not_performed_by_contract",
            "observed_record_write": "checked_in_record_only",
        },
        "captured_at_utc": started_at,
        "execution": {
            "exit_code": helper_result["exit_code"],
            "timed_out": helper_result["timed_out"],
            "response_contract_satisfied": helper_result["response_exact"],
            "wall_time_ms": wall_time_ms,
            "peak_rss_bytes": helper_result["peak_rss_bytes"],
            "rss_scope": "max_child_rss_not_aggregate_process_tree",
            "measurement_helper": "fresh_helper_only_child_hermes",
            "stdout_recorded": False,
            "stderr_recorded": False,
            "cwd_non_git": True,
        },
        "usage_evidence": {
            "source": "Hermes one-shot --usage-file",
            "read": True,
            "provider": CAPTURE_PROVIDER,
            "model": CAPTURE_BARE_MODEL,
            "available_fields": available_fields,
            "unavailable_reasons": {
                field: unavailable_reasons[metric]
                for metric, field in USAGE_METRIC_TO_FIELD.items()
                if metric in unavailable_reasons
            },
            "completed": True,
            "failed": False,
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
    output: str | Path, *, ack_local_hermes_persistence: bool = False
) -> dict[str, Any]:
    """Run the fixed real-profile capture and atomically write sanitized evidence."""

    if not ack_local_hermes_persistence:
        raise CaptureError("capture requires --ack-local-hermes-persistence")
    record = _capture_record()
    output_path = Path(output)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_path, output_path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except (UnboundLocalError, OSError):
            pass
        raise CaptureError("could not write the sanitized observed record") from exc
    return record


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    internal = subparsers.add_parser("_measure-hermes", help=argparse.SUPPRESS)
    internal.add_argument("--usage-path", required=True, type=Path)
    internal.add_argument("--workspace", required=True, type=Path)
    for command, help_text in (
        ("validate", "validate the strict synthetic corpus without replaying it"),
        ("replay", "validate and print a deterministic synthetic replay summary"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("corpus", type=Path)
    capture = subparsers.add_parser(
        "capture",
        help="opt-in: run the fixed real Hermes implementer smoke and write sanitized evidence",
    )
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument(
        "--ack-local-hermes-persistence",
        action="store_true",
        help="acknowledge expected profile credential-store, SessionDB, and log writes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "_measure-hermes":
        return _measurement_helper_main(
            ("--usage-path", str(args.usage_path), "--workspace", str(args.workspace))
        )
    if args.command == "capture":
        try:
            record = capture_baseline(
                args.output,
                ack_local_hermes_persistence=args.ack_local_hermes_persistence,
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
    except CorpusValidationError as exc:
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
