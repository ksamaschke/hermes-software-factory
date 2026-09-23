"""Pydantic v2 contracts exchanged across the factory control-plane boundary.

The models in this module deliberately contain no tracker, database, provider, or
agent implementation details. They are the values a controller can validate
before handing a bounded task to an executor and the values it can validate back
before applying a lifecycle transition.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        pattern=r"^[^\s\x00]+$",
        description="A non-empty stable control-plane identifier.",
    ),
]
Revision = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        pattern=r"^[^\s\x00]+$",
        description="A commit, revision, or other immutable source reference.",
    ),
]
NonEmptyText = Annotated[str, Field(min_length=1, max_length=16_384)]
RelativePathValue = Annotated[str, Field(min_length=1, max_length=1_024)]


class ContractModel(BaseModel):
    """Base class used for strict wire contracts."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


def _validate_relative_path(value: str, field_name: str = "path") -> str:
    """Reject paths that could escape a bound workspace or be ambiguous."""

    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    normalized = value.replace("\\", "/")
    if normalized.startswith(("/", "~/")):
        raise ValueError(f"{field_name} must be relative")
    parts = normalized.split("/")
    if ".." in parts:
        raise ValueError(f"{field_name} must be relative and stay within the workspace")
    if any(part in {"", "."} for part in parts):
        raise ValueError(f"{field_name} must not contain empty or '.' segments")
    return value


def _validate_workspace_root(value: str) -> str:
    if "\x00" in value or not value.strip():
        raise ValueError("workspace root must be a non-empty path without NUL")
    normalized = value.replace("\\", "/")
    if any(part == ".." for part in normalized.split("/")):
        raise ValueError("workspace root must not contain '..' segments")
    return value


def _validate_aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _unique(values: list[str], field_name: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


class TaskRole(StrEnum):
    """Roles that can receive a bounded typed task envelope."""

    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    CODE_REVIEWER = "code_reviewer"


class EvidenceKind(StrEnum):
    TEST = "test"
    COMMAND = "command"
    ARTIFACT = "artifact"
    COMMIT = "commit"
    RUN = "run"
    EXTERNAL = "external"


class RepositoryIdentity(ContractModel):
    """Provider-neutral identity for the source repository under execution."""

    provider: Identifier
    repository: Identifier
    project: Identifier | None = None
    url: HttpUrl | None = None


class WorkspaceIdentity(ContractModel):
    """A controller-bound workspace; agents receive this, not a database handle."""

    workspace_id: Identifier
    root: str
    kind: Literal["git_worktree", "directory"] = "git_worktree"

    @field_validator("root")
    @classmethod
    def root_is_bound_path(cls, value: str) -> str:
        return _validate_workspace_root(value)


class RunIdentity(ContractModel):
    """Stable identity shared by claims, events, leases, and terminal results."""

    task_id: Identifier
    run_id: Identifier
    executor_id: Identifier | None = None
    attempt: int = Field(default=1, ge=1)
    lease_id: Identifier | None = None


class EvidenceRef(ContractModel):
    """A reference to durable evidence; it never embeds secret material."""

    kind: EvidenceKind
    reference: Identifier
    description: str | None = Field(default=None, max_length=2_048)
    fingerprint: str | None = Field(default=None, min_length=1, max_length=256)
    revision: Revision | None = None


class AcceptanceCriterion(ContractModel):
    id: Identifier | None = None
    description: NonEmptyText
    required: bool = True


class CommandSpec(ContractModel):
    """A controller-approved command description, not an arbitrary shell handle."""

    command: NonEmptyText
    timeout_seconds: int = Field(default=120, ge=1, le=86_400)
    cwd: RelativePathValue | None = None
    network: Literal["denied", "bounded", "allowed"] = "denied"

    @field_validator("command")
    @classmethod
    def command_is_single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("command must be a single-line command specification")
        return value

    @field_validator("cwd")
    @classmethod
    def cwd_is_relative(cls, value: str | None) -> str | None:
        return None if value is None else _validate_relative_path(value, "cwd")


class Dependency(ContractModel):
    task_id: Identifier
    relation: Literal["depends_on", "blocks"] = "depends_on"


class TaskConstraints(ContractModel):
    allowed_paths: list[RelativePathValue] = Field(default_factory=list)
    protected_paths: list[RelativePathValue] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    network: Literal["denied", "bounded", "allowed"] = "denied"
    max_changed_files: int | None = Field(default=None, ge=1)

    @field_validator("allowed_paths", "protected_paths")
    @classmethod
    def paths_are_relative(cls, values: list[str], info) -> list[str]:
        field_name = info.field_name or "path"
        return [_validate_relative_path(value, field_name) for value in values]

    @field_validator("dependencies", mode="before")
    @classmethod
    def coerce_dependency_ids(cls, values):
        if values is None:
            return []
        return [
            {"task_id": value} if isinstance(value, str) else value for value in values
        ]

    @field_validator("dependencies")
    @classmethod
    def dependencies_are_unique(cls, values: list[Dependency]) -> list[Dependency]:
        _unique([value.task_id for value in values], "dependencies")
        return values


class TaskEnvelope(ContractModel):
    """The complete bounded input an executor receives for one run."""

    task_id: Identifier
    run_id: Identifier
    role: TaskRole
    repository: RepositoryIdentity
    workspace: WorkspaceIdentity
    base_revision: Revision
    candidate_revision: Revision | None = None
    objective: NonEmptyText
    acceptance: list[AcceptanceCriterion] = Field(min_length=1)
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    allowed_commands: list[CommandSpec] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    deadline: datetime

    @field_validator("deadline")
    @classmethod
    def deadline_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "deadline")

    @field_validator("acceptance")
    @classmethod
    def acceptance_ids_are_unique(
        cls, values: list[AcceptanceCriterion]
    ) -> list[AcceptanceCriterion]:
        ids = [criterion.id for criterion in values if criterion.id is not None]
        _unique(ids, "acceptance criterion ids")
        return values


class TestEvidence(ContractModel):
    """Deterministic test evidence recorded by a runner and read back by a controller."""

    command: NonEmptyText
    exit_code: int = Field(ge=0)
    status: Literal["passed", "failed", "skipped", "blocked"] = "passed"
    run_ref: Identifier | None = None
    revision: Revision | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    passed: bool | None = None

    @model_validator(mode="after")
    def status_matches_exit_code(self) -> TestEvidence:
        if self.status == "passed" and self.exit_code != 0:
            raise ValueError("passed test evidence must have exit_code 0")
        if self.status == "failed" and self.exit_code == 0:
            raise ValueError("failed test evidence must have a non-zero exit_code")
        if self.passed is not None:
            expected = self.status == "passed" and self.exit_code == 0
            if self.passed != expected:
                raise ValueError("passed must agree with status and exit_code")
        return self


class Blocker(ContractModel):
    code: Identifier
    summary: NonEmptyText
    owner: Identifier | None = None
    blocking: bool = True


class PlanTask(ContractModel):
    task_id: Identifier
    role: TaskRole
    objective: NonEmptyText
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list)
    dependencies: list[Identifier] = Field(default_factory=list)
    non_goals: list[NonEmptyText] = Field(default_factory=list)

    @field_validator("dependencies")
    @classmethod
    def plan_dependencies_are_unique(cls, values: list[str]) -> list[str]:
        return _unique(values, "plan task dependencies")


class DecisionRequest(ContractModel):
    key: Identifier
    question: NonEmptyText
    options: list[NonEmptyText] = Field(default_factory=list)


class PlanOutcome(ContractModel):
    """A planner proposal; it does not itself create tasks or mutate state."""

    status: Literal["planned", "ready", "needs_decision", "failed"]
    summary: NonEmptyText
    tasks: list[PlanTask] = Field(default_factory=list)
    decisions: list[DecisionRequest] = Field(default_factory=list)
    assumptions: list[NonEmptyText] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)
    next_gate: NonEmptyText


class ChangedPath(ContractModel):
    path: RelativePathValue
    status: Literal["added", "modified", "deleted", "renamed"] = "modified"
    old_path: RelativePathValue | None = None
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)

    @field_validator("path", "old_path")
    @classmethod
    def changed_paths_are_relative(cls, value: str | None, info) -> str | None:
        return (
            None if value is None else _validate_relative_path(value, info.field_name)
        )

    @model_validator(mode="after")
    def renamed_path_has_source(self) -> ChangedPath:
        if self.status == "renamed" and self.old_path is None:
            raise ValueError("renamed paths must include old_path")
        if self.status != "renamed" and self.old_path is not None:
            raise ValueError("old_path is only valid for renamed paths")
        return self


class ImplementationOutcome(ContractModel):
    """The implementer proposal validated before a lifecycle transition."""

    status: Literal["candidate_ready", "needs_decision", "failed"]
    summary: NonEmptyText
    changed_paths: list[RelativePathValue] = Field(default_factory=list)
    candidate_revision: Revision | None = None
    tests: list[TestEvidence] = Field(default_factory=list)
    assumptions: list[NonEmptyText] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)
    next_gate: NonEmptyText

    @field_validator("changed_paths")
    @classmethod
    def changed_paths_are_relative_and_unique(cls, values: list[str]) -> list[str]:
        checked = [_validate_relative_path(value, "changed_paths") for value in values]
        return _unique(checked, "changed_paths")

    @model_validator(mode="after")
    def candidate_status_is_consistent(self) -> ImplementationOutcome:
        if self.status == "candidate_ready" and self.candidate_revision is None:
            raise ValueError("candidate_ready requires candidate_revision")
        if self.status == "failed" and self.candidate_revision is not None:
            raise ValueError("failed outcomes must not publish candidate_revision")
        return self


class ReviewFinding(ContractModel):
    finding_id: Identifier
    severity: Literal["info", "warning", "error", "blocker"]
    summary: NonEmptyText
    path: RelativePathValue | None = None
    line: int | None = Field(default=None, ge=1)
    resolved: bool = False

    @field_validator("path")
    @classmethod
    def finding_path_is_relative(cls, value: str | None) -> str | None:
        return None if value is None else _validate_relative_path(value, "path")


class ReviewOutcome(ContractModel):
    """Independent review verdict for one immutable candidate and exact scope."""

    verdict: Literal["APPROVED", "CHANGES_REQUESTED", "REVIEW_INCOMPLETE"]
    candidate_revision: Revision
    reviewed_scope: list[ChangedPath] = Field(min_length=1)
    findings: list[ReviewFinding] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    mutation_detected: bool = False

    @field_validator("reviewed_scope", mode="before")
    @classmethod
    def coerce_scope_paths(cls, values):
        return [
            value if isinstance(value, dict) else {"path": value} for value in values
        ]

    @field_validator("reviewed_scope")
    @classmethod
    def scope_paths_are_unique(cls, values: list[ChangedPath]) -> list[ChangedPath]:
        _unique([value.path for value in values], "reviewed_scope")
        return values

    @model_validator(mode="after")
    def mutation_requires_incomplete_review(self) -> ReviewOutcome:
        if self.mutation_detected and self.verdict != "REVIEW_INCOMPLETE":
            raise ValueError("a source mutation must produce REVIEW_INCOMPLETE")
        return self


class FactoryEvent(ContractModel):
    """An append-only, provider-neutral event attached to a run."""

    event_type: Identifier
    run: RunIdentity
    occurred_at: datetime
    data: dict[str, object] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def event_time_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "occurred_at")


class Lease(ContractModel):
    run: RunIdentity
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def lease_expiry_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "expires_at")


class ClaimedRun(ContractModel):
    run: RunIdentity
    lease: Lease
    envelope: TaskEnvelope


ValidatedOutcome: TypeAlias = PlanOutcome | ImplementationOutcome | ReviewOutcome


class TaskState(ContractModel):
    task_id: Identifier
    state: Literal["queued", "claimed", "running", "completed", "blocked", "failed"]
    run: RunIdentity | None = None
    outcome: ValidatedOutcome | None = None


# Short aliases used by callers that treat these as the control-plane nouns.
Evidence: TypeAlias = EvidenceRef
Plan: TypeAlias = PlanOutcome
Implementation: TypeAlias = ImplementationOutcome
Review: TypeAlias = ReviewOutcome
Run: TypeAlias = RunIdentity

__all__ = [
    "AcceptanceCriterion",
    "Blocker",
    "ChangedPath",
    "ClaimedRun",
    "CommandSpec",
    "ContractModel",
    "DecisionRequest",
    "Dependency",
    "Evidence",
    "EvidenceKind",
    "EvidenceRef",
    "FactoryEvent",
    "Implementation",
    "ImplementationOutcome",
    "Lease",
    "Plan",
    "PlanOutcome",
    "PlanTask",
    "RepositoryIdentity",
    "Review",
    "ReviewFinding",
    "ReviewOutcome",
    "Run",
    "RunIdentity",
    "TaskConstraints",
    "TaskEnvelope",
    "TaskRole",
    "TaskState",
    "TestEvidence",
    "ValidatedOutcome",
    "WorkspaceIdentity",
]
