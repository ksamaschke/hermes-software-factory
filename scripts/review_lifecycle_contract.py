#!/usr/bin/env python3
"""Plan fail-closed Kanban review lifecycle actions from durable evidence.

The module is deliberately pure: it never mutates a board.  A coordinator reads
native task/run/events, constructs ``ReviewEvidence``, calls ``plan_review``, and
then executes at most the returned action with native idempotency/readback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


VALID_VERDICTS = {"APPROVED", "CHANGES_REQUESTED", "REVIEW-INCOMPLETE"}
VALID_SOURCE_STATUSES = {"review", "ready"}
LIFECYCLE_REJECTION_CODE = "invalid_source_status"


class ContractViolation(ValueError):
    """Durable evidence is incomplete, stale, foreign, or contradictory."""


@dataclass(frozen=True)
class TransitionRejection:
    task_id: str
    run_id: str
    reviewer_identity: str
    action: str
    code: str
    source_status: str
    identity: str


@dataclass(frozen=True)
class ReviewEvidence:
    task_id: str
    implementation_task: str
    run_id: str
    latest_run_id: str
    current_run_id: str | None
    reviewer_identity: str
    expected_reviewer_identity: str
    source_status: str
    review_requested_identity: str | None
    verdict: str
    candidate: str
    base: str
    scope_manifest: tuple[str, ...]
    review_round: str
    finding_identity: str
    child_ids: tuple[str, ...] = ()
    rejection: TransitionRejection | None = None


@dataclass(frozen=True)
class ReviewPlan:
    lifecycle: str
    worker_action: str
    orchestrator_action: str
    successor_kind: str | None
    successor_key: str | None
    release_allowed: bool
    archive_leaf_after_successor: bool
    replace_downstream_frontier: bool
    redispatch_leaf: bool
    successor_count: int


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_text(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractViolation(f"missing {name}")
    return text


def _normalise_scope(scope: Sequence[Any]) -> tuple[str, ...]:
    values = tuple(sorted({_require_text("scope entry", item) for item in scope}))
    if not values:
        raise ContractViolation("missing scope_manifest")
    return values


def handoff_identity(evidence: ReviewEvidence) -> str:
    """Bind a review-request handoff to the candidate and expected reviewer."""
    return _canonical_hash(
        {
            "task_id": evidence.task_id,
            "implementation_task": evidence.implementation_task,
            "expected_reviewer_identity": evidence.expected_reviewer_identity,
            "candidate": evidence.candidate,
            "base": evidence.base,
            "scope_manifest": list(_normalise_scope(evidence.scope_manifest)),
            "review_round": evidence.review_round,
        }
    )


def rejection_identity(evidence: ReviewEvidence) -> str:
    """Bind a native transition rejection to the exact claimed run."""
    return _canonical_hash(
        {
            "task_id": evidence.task_id,
            "run_id": evidence.run_id,
            "reviewer_identity": evidence.reviewer_identity,
            "source_status": evidence.source_status,
            "action": "kanban_request_changes",
            "code": LIFECYCLE_REJECTION_CODE,
        }
    )


def successor_key(evidence: ReviewEvidence, successor_kind: str) -> str:
    """Return one stable key for one terminal handoff and successor kind."""
    digest = _canonical_hash(
        {
            "implementation_task": evidence.implementation_task,
            "leaf_task": evidence.task_id,
            "source_run_id": evidence.run_id,
            "reviewer_identity": evidence.reviewer_identity,
            "review_round": evidence.review_round,
            "candidate": evidence.candidate,
            "base": evidence.base,
            "scope_manifest": list(_normalise_scope(evidence.scope_manifest)),
            "verdict": evidence.verdict,
            "finding_identity": evidence.finding_identity,
            "successor_kind": successor_kind,
        }
    )
    return f"review-handoff:{digest}"


def _validate_evidence(evidence: ReviewEvidence) -> None:
    for name in (
        "task_id",
        "implementation_task",
        "run_id",
        "latest_run_id",
        "reviewer_identity",
        "expected_reviewer_identity",
        "candidate",
        "base",
        "review_round",
        "finding_identity",
    ):
        _require_text(name, getattr(evidence, name))
    _normalise_scope(evidence.scope_manifest)

    if evidence.run_id != evidence.latest_run_id:
        raise ContractViolation("review run is not the latest run")
    if evidence.current_run_id not in {None, "", evidence.run_id}:
        raise ContractViolation("a newer or foreign run is current")
    if evidence.reviewer_identity != evidence.expected_reviewer_identity:
        raise ContractViolation("reviewer identity does not match the durable route")
    if evidence.source_status not in VALID_SOURCE_STATUSES:
        raise ContractViolation("unsupported claim source_status")
    if evidence.verdict not in VALID_VERDICTS:
        raise ContractViolation("unsupported verdict")

    if evidence.source_status == "review":
        if evidence.review_requested_identity != handoff_identity(evidence):
            raise ContractViolation("same-card review lacks the exact review_requested handoff")
    elif evidence.review_requested_identity:
        raise ContractViolation("standalone review has contradictory review_requested evidence")

    rejection = evidence.rejection
    if rejection is not None:
        if evidence.source_status != "ready":
            raise ContractViolation("transition rejection is not from a standalone leaf")
        expected = {
            "task_id": evidence.task_id,
            "run_id": evidence.run_id,
            "reviewer_identity": evidence.reviewer_identity,
            "action": "kanban_request_changes",
            "code": LIFECYCLE_REJECTION_CODE,
            "source_status": "ready",
            "identity": rejection_identity(evidence),
        }
        actual = asdict(rejection)
        if actual != expected:
            raise ContractViolation("transition rejection is stale, foreign, or ambiguous")


def plan_review(evidence: ReviewEvidence) -> ReviewPlan:
    """Return the only lifecycle action allowed by the durable evidence."""
    _validate_evidence(evidence)

    if evidence.source_status == "review":
        if evidence.verdict == "APPROVED":
            return ReviewPlan(
                "same-card",
                "kanban_complete",
                "none",
                None,
                None,
                True,
                False,
                False,
                False,
                0,
            )
        if evidence.verdict == "CHANGES_REQUESTED":
            return ReviewPlan(
                "same-card",
                "kanban_request_changes",
                "native_rework_only",
                None,
                None,
                False,
                False,
                False,
                False,
                0,
            )
        kind = "review-continuation"
        return ReviewPlan(
            "same-card",
            "kanban_block",
            "route_review_continuation",
            kind,
            successor_key(evidence, kind),
            False,
            False,
            False,
            False,
            1,
        )

    if evidence.verdict == "APPROVED":
        return ReviewPlan(
            "standalone",
            "kanban_complete",
            "none",
            None,
            None,
            True,
            False,
            False,
            False,
            0,
        )

    kind = "implementation-remediation" if evidence.verdict == "CHANGES_REQUESTED" else "review-continuation"
    action = "route_remediation_then_archive" if evidence.verdict == "CHANGES_REQUESTED" else "route_continuation_then_archive"
    return ReviewPlan(
        "standalone",
        "kanban_block",
        action,
        kind,
        successor_key(evidence, kind),
        False,
        True,
        bool(evidence.child_ids),
        False,
        1,
    )


def evidence_from_mapping(value: Mapping[str, Any]) -> ReviewEvidence:
    rejection_value = value.get("rejection")
    rejection = TransitionRejection(**rejection_value) if isinstance(rejection_value, Mapping) else None
    return ReviewEvidence(
        task_id=_require_text("task_id", value.get("task_id")),
        implementation_task=_require_text("implementation_task", value.get("implementation_task")),
        run_id=_require_text("run_id", value.get("run_id")),
        latest_run_id=_require_text("latest_run_id", value.get("latest_run_id")),
        current_run_id=str(value["current_run_id"]) if value.get("current_run_id") not in {None, ""} else None,
        reviewer_identity=_require_text("reviewer_identity", value.get("reviewer_identity")),
        expected_reviewer_identity=_require_text("expected_reviewer_identity", value.get("expected_reviewer_identity")),
        source_status=_require_text("source_status", value.get("source_status")),
        review_requested_identity=(str(value["review_requested_identity"]) if value.get("review_requested_identity") else None),
        verdict=_require_text("verdict", value.get("verdict")),
        candidate=_require_text("candidate", value.get("candidate")),
        base=_require_text("base", value.get("base")),
        scope_manifest=_normalise_scope(value.get("scope_manifest") or ()),
        review_round=_require_text("review_round", value.get("review_round")),
        finding_identity=_require_text("finding_identity", value.get("finding_identity")),
        child_ids=tuple(sorted({_require_text("child_id", child) for child in value.get("child_ids") or ()})),
        rejection=rejection,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON evidence file")
    args = parser.parse_args(argv)
    evidence = evidence_from_mapping(json.loads(args.input.read_text(encoding="utf-8")))
    print(json.dumps(asdict(plan_review(evidence)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
