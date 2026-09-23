"""Pydantic v2 contracts exchanged across the factory control-plane boundary.

The models in this module deliberately contain no tracker, database, provider, or
agent implementation details. They are the values a controller can validate
before handing a bounded task to an executor and the values it can validate back
before applying a lifecycle transition.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self, TypeAlias
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

MAX_IDENTIFIER_LENGTH = 256
MAX_EVENT_ATTRIBUTE_KEY_LENGTH = 128
MAX_EVENT_ATTRIBUTE_VALUE_LENGTH = 4_096
MAX_FACTORY_EVENT_ATTRIBUTES = 64
MAX_EVIDENCE_DESCRIPTION_LENGTH = 2_048

Identifier = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=MAX_IDENTIFIER_LENGTH,
        pattern=r"^[^\s\x00]+$",
        description="A non-empty stable control-plane identifier.",
    ),
]
Revision = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=256,
        pattern=r"^[^\s\x00]+$",
        description="A commit, revision, or other immutable source reference.",
    ),
]
NonEmptyText = Annotated[StrictStr, Field(min_length=1, max_length=16_384)]
RelativePathValue = Annotated[StrictStr, Field(min_length=1, max_length=1_024)]
EventString = Annotated[StrictStr, Field(max_length=MAX_EVENT_ATTRIBUTE_VALUE_LENGTH)]
EventScalar: TypeAlias = EventString | StrictInt | StrictFloat | StrictBool | None
EventKey = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=MAX_EVENT_ATTRIBUTE_KEY_LENGTH,
        pattern=r"^[^\s\x00]+$",
    ),
]


class ContractModel(BaseModel):
    """Base class used for strict, immutable wire contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    def validated_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        """Return a defensive, freshly validated copy of this contract.

        Pydantic's default ``model_copy(update=...)`` intentionally skips
        validation.  Runtime callers must use this validated behavior instead;
        the override below keeps the familiar API from creating an unvalidated
        frozen object.
        """

        del deep  # validation creates a fresh object graph regardless of this hint
        values = self.model_dump(mode="python")
        if update:
            values.update(dict(update))
        return type(self).model_validate(values)

    def model_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        """Return only a validated copy; never bypass contract validators."""

        return self.validated_copy(update=update, deep=deep)


def _as_tuple_input(value: object, field_name: str) -> tuple[object, ...]:
    """Accept only real sequences for tuple-valued contract fields."""

    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(
        value, Sequence
    ):
        raise ValueError(  # noqa: TRY004 - Pydantic v2 escapes TypeError from validators
            f"{field_name} must be a sequence, not a scalar or mapping"
        )
    return tuple(value)


def _validate_relative_path(value: str, field_name: str = "path") -> str:
    """Reject paths that could escape a bound workspace or be ambiguous."""

    if "\x00" in value or not value:
        raise ValueError(f"{field_name} must be a non-empty relative path")
    normalized = value.replace("\\", "/")
    if normalized.startswith(("/", "~")):
        raise ValueError(f"{field_name} must be relative")
    # This rejects drive-relative (``C:foo``) as well as drive-rooted paths.
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"{field_name} must be relative")
    parts = normalized.split("/")
    if ".." in parts:
        raise ValueError(f"{field_name} must be relative and stay within the workspace")
    if any(part in {"", "."} for part in parts):
        raise ValueError(f"{field_name} must not contain empty or '.' segments")
    return value


def _validate_workspace_root(value: str) -> str:
    """Require and canonically represent an absolute workspace root.

    This is lexical canonicalization only.  It removes separator aliases and
    dot segments but cannot resolve filesystem symlinks; a controller must still
    bind the resulting root to the real workspace before authorization checks.
    """

    if "\x00" in value or not value.strip():
        raise ValueError("workspace root must be a non-empty path without NUL")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("workspace root must not contain control characters")
    normalized = value.replace("\\", "/")
    if value.startswith("\\") and not value.startswith("\\\\"):
        raise ValueError("workspace root must be an absolute POSIX or Windows path")

    def canonical_parts(parts: list[str]) -> list[str]:
        if any(part in {".", ".."} for part in parts):
            raise ValueError("workspace root must not contain '.' or '..' segments")
        return [part for part in parts if part]

    if normalized.startswith("//"):
        parts = canonical_parts(normalized[2:].split("/"))
        if len(parts) < 2:
            raise ValueError("UNC workspace root must include a server and share")
        return "//" + "/".join(parts)
    if normalized.startswith("/"):
        parts = canonical_parts(normalized[1:].split("/"))
        return "/" if not parts else "/" + "/".join(parts)

    drive_match = re.match(r"^([A-Za-z]):/(.*)$", normalized)
    if drive_match is None:
        raise ValueError("workspace root must be an absolute POSIX or Windows path")
    parts = canonical_parts(drive_match.group(2).split("/"))
    suffix = "/".join(parts)
    return f"{drive_match.group(1).upper()}:/" + suffix


def _validate_aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _unique(values: Sequence[str], field_name: str) -> Sequence[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


def _validate_repository_url(value: HttpUrl | None) -> HttpUrl | None:
    if value is None:
        return None
    text = str(value)
    parsed = urlsplit(text)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("repository URL must not contain userinfo")
    if parsed.query or "?" in text:
        raise ValueError("repository URL must not contain a query")
    if parsed.fragment or "#" in text:
        raise ValueError("repository URL must not contain a fragment")
    return value


_SECRET_EVENT_KEY_MARKERS = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "authorization",
        "bearer",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "privatekey",
        "refreshtoken",
        "secret",
        "setcookie",
        "token",
    }
)


def _normalize_event_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _validate_event_key(value: str) -> str:
    normalized = _normalize_event_key(value)
    if not normalized:
        raise ValueError("event attribute key must contain an alphanumeric character")
    if any(marker in normalized for marker in _SECRET_EVENT_KEY_MARKERS):
        raise ValueError("event attribute keys must not identify credential material")
    return value


def _looks_like_inline_credential(value: str) -> bool:
    """Reject common credential encodings even when a safe-looking key is used.

    This is a conservative exclusion list, not a proof that arbitrary text is
    secret-free.  Unknown encodings remain the responsibility of the caller and
    must not be treated as safe merely because they evade these checks.
    """

    lower = value.casefold()
    if "-----begin " in lower and " key-----" in lower:
        return True
    if re.search(r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}", value, re.IGNORECASE):
        return True
    if re.search(
        r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_-]{16,}|glpat-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{12,})",
        value,
    ):
        return True
    if re.search(r"(?:AKIA|ASIA)[0-9A-Z]{16}", value):
        return True
    if re.search(r"(?:github_pat|npm_|pypi-)[A-Za-z0-9_-]{16,}", value):
        return True
    if re.search(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", value):
        return True
    if re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value):
        return True
    if re.search(
        r"(?:^|[-_ ])(?:token|secret|password|credential)(?:$|[-_ ])",
        value,
        re.IGNORECASE,
    ):
        return True
    return bool(
        re.search(
            r"(?:authorization|cookie|token|secret|password|api[ _-]?key|credential)\s*[:=]\s*\S+",
            value,
            re.IGNORECASE,
        )
        or re.search(
            r"(?:^|[?&\s])(?:access[_-]?token|client[_-]?secret|refresh[_-]?token|private[_-]?key)=[^\s&]+",
            value,
            re.IGNORECASE,
        )
    )


_SAFE_DIGEST_OR_UUID = re.compile(
    r"^(?:[0-9a-f]{7,128}|(?:md5|sha(?:1|224|256|384|512)):[0-9a-f]{7,128}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def _shannon_entropy(value: str) -> float:
    frequencies = {character: value.count(character) for character in set(value)}
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length) for count in frequencies.values()
    )


def _contains_high_entropy_token(value: str) -> bool:
    """Reject opaque high-entropy tokens unless shaped as durable fingerprints."""

    for token in re.findall(r"[A-Za-z0-9+/=_-]{20,}", value):
        if _SAFE_DIGEST_OR_UUID.fullmatch(token):
            continue
        if len(set(token)) >= 8 and _shannon_entropy(token) >= 4.0:
            return True
    return False


def _validate_secret_free_text(value: str, field_name: str) -> str:
    """Apply bounded, fail-closed checks to text crossing an evidence boundary."""

    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} must not contain control characters")
    if _looks_like_inline_credential(value) or _contains_high_entropy_token(value):
        raise ValueError(f"{field_name} must not contain credential-like material")
    return value


_CREDENTIAL_REFERENCE_SCHEMES = frozenset(
    {
        "env",
        "secret",
        "secrets",
        "vault",
        "aws-secretsmanager",
        "gcp-secretmanager",
        "azure-keyvault",
    }
)


def _validate_evidence_reference(value: str) -> str:
    value = _validate_secret_free_text(value, "evidence reference")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() in _CREDENTIAL_REFERENCE_SCHEMES:
        raise ValueError("evidence references must not point to credential stores")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("evidence references must not contain userinfo")
    if parsed.query or "?" in value or parsed.fragment or "#" in value:
        raise ValueError("evidence references must not contain query or fragment data")
    return value


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

    _url_is_credential_free = field_validator("url")(_validate_repository_url)


class WorkspaceIdentity(ContractModel):
    """A controller-bound workspace; agents receive this, not a database handle."""

    workspace_id: Identifier
    root: StrictStr = Field(min_length=1, max_length=4_096)
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
    attempt: StrictInt = Field(default=1, ge=1)
    lease_id: Identifier | None = None


class EvidenceRef(ContractModel):
    """A reference to durable evidence; it never embeds secret material."""

    kind: EvidenceKind
    reference: Identifier
    description: StrictStr | None = Field(
        default=None, max_length=MAX_EVIDENCE_DESCRIPTION_LENGTH
    )
    fingerprint: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    revision: Revision | None = None

    @field_validator("reference")
    @classmethod
    def reference_is_credential_free(cls, value: str) -> str:
        return _validate_evidence_reference(value)

    @field_validator("description")
    @classmethod
    def description_is_credential_free(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _validate_secret_free_text(value, "evidence description")
        )

    @field_validator("fingerprint")
    @classmethod
    def fingerprint_is_credential_free(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _validate_secret_free_text(value, "evidence fingerprint")
        )


class AcceptanceCriterion(ContractModel):
    id: Identifier | None = None
    description: NonEmptyText
    required: StrictBool = True


class CommandSpec(ContractModel):
    """A controller-approved argv, executed by a caller with ``shell=False``."""

    argv: tuple[NonEmptyText, ...] = Field(min_length=1)
    timeout_seconds: StrictInt = Field(default=120, ge=1, le=86_400)
    cwd: RelativePathValue | None = None
    network: Literal["denied", "bounded", "allowed"] = "denied"

    @field_validator("argv", mode="before")
    @classmethod
    def argv_is_a_sequence(cls, value: object) -> tuple[object, ...]:
        return _as_tuple_input(value, "argv")

    @field_validator("argv")
    @classmethod
    def argv_elements_are_non_empty(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for element in values:
            if "\x00" in element or "\n" in element or "\r" in element:
                raise ValueError("argv elements must not contain NUL or newlines")
        return values

    @field_validator("cwd")
    @classmethod
    def cwd_is_relative(cls, value: str | None) -> str | None:
        return None if value is None else _validate_relative_path(value, "cwd")


class Dependency(ContractModel):
    task_id: Identifier
    relation: Literal["depends_on", "blocks"] = "depends_on"


class TaskConstraints(ContractModel):
    allowed_paths: tuple[RelativePathValue, ...] = Field(default_factory=tuple)
    protected_paths: tuple[RelativePathValue, ...] = Field(default_factory=tuple)
    dependencies: tuple[Dependency, ...] = Field(default_factory=tuple)
    network: Literal["denied", "bounded", "allowed"] = "denied"
    max_changed_files: StrictInt | None = Field(default=None, ge=1)

    @field_validator("allowed_paths", "protected_paths", mode="before")
    @classmethod
    def paths_are_sequences(cls, value: object, info) -> tuple[object, ...]:
        return _as_tuple_input(value, info.field_name)

    @field_validator("allowed_paths", "protected_paths")
    @classmethod
    def paths_are_relative(cls, values: tuple[str, ...], info) -> tuple[str, ...]:
        return tuple(
            _validate_relative_path(value, info.field_name) for value in values
        )

    @field_validator("dependencies", mode="before")
    @classmethod
    def dependencies_are_sequence(cls, value: object) -> tuple[object, ...]:
        values = _as_tuple_input(value, "dependencies")
        return tuple(
            {"task_id": item} if isinstance(item, str) else item for item in values
        )

    @field_validator("dependencies")
    @classmethod
    def dependencies_are_unique(
        cls, values: tuple[Dependency, ...]
    ) -> tuple[Dependency, ...]:
        _unique(tuple(value.task_id for value in values), "dependencies")
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
    acceptance: tuple[AcceptanceCriterion, ...] = Field(min_length=1)
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    allowed_commands: tuple[CommandSpec, ...] = Field(default_factory=tuple)
    evidence: tuple[EvidenceRef, ...] = Field(default_factory=tuple)
    deadline: datetime

    @field_validator("acceptance", "allowed_commands", "evidence", mode="before")
    @classmethod
    def nested_fields_are_sequences(cls, value: object, info) -> tuple[object, ...]:
        return _as_tuple_input(value, info.field_name)

    @field_validator("deadline")
    @classmethod
    def deadline_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "deadline")

    @field_validator("acceptance")
    @classmethod
    def acceptance_ids_are_unique(
        cls, values: tuple[AcceptanceCriterion, ...]
    ) -> tuple[AcceptanceCriterion, ...]:
        ids = tuple(criterion.id for criterion in values if criterion.id is not None)
        _unique(ids, "acceptance criterion ids")
        return values


class TestEvidence(ContractModel):
    """Deterministic test evidence recorded by a runner and read back by a controller."""

    command: NonEmptyText
    exit_code: StrictInt = Field(ge=0)
    status: Literal["passed", "failed", "skipped", "blocked"] = "passed"
    run_ref: Identifier | None = None
    revision: Revision | None = None
    duration_seconds: StrictFloat | None = Field(default=None, ge=0)
    passed: StrictBool | None = None

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
    blocking: StrictBool = True


class Failure(ContractModel):
    code: Identifier
    summary: NonEmptyText
    owner: Identifier | None = None
    retryable: StrictBool = False


class PlanTask(ContractModel):
    task_id: Identifier
    role: TaskRole
    objective: NonEmptyText
    acceptance: tuple[AcceptanceCriterion, ...] = Field(default_factory=tuple)
    dependencies: tuple[Identifier, ...] = Field(default_factory=tuple)
    non_goals: tuple[NonEmptyText, ...] = Field(default_factory=tuple)

    @field_validator("acceptance", "dependencies", "non_goals", mode="before")
    @classmethod
    def plan_fields_are_sequences(cls, value: object, info) -> tuple[object, ...]:
        return _as_tuple_input(value, info.field_name)

    @field_validator("dependencies")
    @classmethod
    def plan_dependencies_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        _unique(values, "plan task dependencies")
        return values

    @model_validator(mode="after")
    def task_does_not_depend_on_itself(self) -> PlanTask:
        if self.task_id in self.dependencies:
            raise ValueError("plan task dependency must not self-reference")
        return self


class DecisionRequest(ContractModel):
    key: Identifier
    question: NonEmptyText
    options: tuple[NonEmptyText, ...] = Field(default_factory=tuple)

    @field_validator("options", mode="before")
    @classmethod
    def options_are_sequence(cls, value: object) -> tuple[object, ...]:
        return _as_tuple_input(value, "options")


class PlanOutcome(ContractModel):
    """A planner proposal; it does not itself create tasks or mutate state."""

    status: Literal["planned", "ready", "needs_decision", "failed"]
    summary: NonEmptyText
    tasks: tuple[PlanTask, ...] = Field(default_factory=tuple)
    decisions: tuple[DecisionRequest, ...] = Field(default_factory=tuple)
    assumptions: tuple[NonEmptyText, ...] = Field(default_factory=tuple)
    blockers: tuple[Blocker, ...] = Field(default_factory=tuple)
    next_gate: NonEmptyText

    @field_validator("tasks", "decisions", "assumptions", "blockers", mode="before")
    @classmethod
    def plan_outcome_fields_are_sequences(
        cls, value: object, info
    ) -> tuple[object, ...]:
        return _as_tuple_input(value, info.field_name)

    @model_validator(mode="after")
    def dependency_graph_is_acyclic(self) -> PlanOutcome:
        task_ids = tuple(task.task_id for task in self.tasks)
        _unique(task_ids, "plan task ids")
        known = set(task_ids)
        graph: dict[str, tuple[str, ...]] = {}
        for task in self.tasks:
            for dependency in task.dependencies:
                if dependency not in known:
                    raise ValueError(
                        f"plan task {task.task_id!r} references unknown dependency "
                        f"{dependency!r}"
                    )
            graph[task.task_id] = task.dependencies

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str, path: tuple[str, ...] = ()) -> None:
            if task_id in visiting:
                cycle = " -> ".join((*path, task_id))
                raise ValueError(f"plan dependency cycle detected: {cycle}")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in graph[task_id]:
                visit(dependency, (*path, task_id))
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in graph:
            visit(task_id)
        return self


class ChangedPath(ContractModel):
    path: RelativePathValue
    status: Literal["added", "modified", "deleted", "renamed"] = "modified"
    old_path: RelativePathValue | None = None
    additions: StrictInt = Field(default=0, ge=0)
    deletions: StrictInt = Field(default=0, ge=0)

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
    changed_paths: tuple[RelativePathValue, ...] = Field(default_factory=tuple)
    candidate_revision: Revision | None = None
    tests: tuple[TestEvidence, ...] = Field(default_factory=tuple)
    assumptions: tuple[NonEmptyText, ...] = Field(default_factory=tuple)
    blockers: tuple[Blocker, ...] = Field(default_factory=tuple)
    next_gate: NonEmptyText

    @field_validator("changed_paths", "tests", "assumptions", "blockers", mode="before")
    @classmethod
    def implementation_fields_are_sequences(
        cls, value: object, info
    ) -> tuple[object, ...]:
        return _as_tuple_input(value, info.field_name)

    @field_validator("changed_paths")
    @classmethod
    def changed_paths_are_relative_and_unique(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        checked = tuple(
            _validate_relative_path(value, "changed_paths") for value in values
        )
        _unique(checked, "changed_paths")
        return checked

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
    line: StrictInt | None = Field(default=None, ge=1)
    resolved: StrictBool = False

    @field_validator("path")
    @classmethod
    def finding_path_is_relative(cls, value: str | None) -> str | None:
        return None if value is None else _validate_relative_path(value, "path")


class ReviewOutcome(ContractModel):
    """Independent review verdict for one immutable candidate and exact scope."""

    verdict: Literal["APPROVED", "CHANGES_REQUESTED", "REVIEW_INCOMPLETE"]
    candidate_revision: Revision
    reviewed_scope: tuple[ChangedPath, ...] = Field(min_length=1)
    findings: tuple[ReviewFinding, ...] = Field(default_factory=tuple)
    evidence: tuple[EvidenceRef, ...] = Field(default_factory=tuple)
    mutation_detected: StrictBool = False

    @field_validator("reviewed_scope", mode="before")
    @classmethod
    def scope_is_a_sequence(cls, value: object) -> tuple[object, ...]:
        values = _as_tuple_input(value, "reviewed_scope")
        return tuple(
            item if isinstance(item, Mapping) else {"path": item} for item in values
        )

    @field_validator("findings", "evidence", mode="before")
    @classmethod
    def review_fields_are_sequences(cls, value: object, info) -> tuple[object, ...]:
        return _as_tuple_input(value, info.field_name)

    @field_validator("reviewed_scope")
    @classmethod
    def scope_paths_are_unique(
        cls, values: tuple[ChangedPath, ...]
    ) -> tuple[ChangedPath, ...]:
        _unique(tuple(value.path for value in values), "reviewed_scope")
        return values

    @model_validator(mode="after")
    def mutation_requires_incomplete_review(self) -> ReviewOutcome:
        if self.mutation_detected and self.verdict != "REVIEW_INCOMPLETE":
            raise ValueError("a source mutation must produce REVIEW_INCOMPLETE")
        return self


class EventAttribute(ContractModel):
    """One scalar event attribute; compound payloads are intentionally forbidden."""

    key: EventKey
    value: EventScalar

    @field_validator("key")
    @classmethod
    def key_is_not_secret_bearing(cls, value: str) -> str:
        return _validate_event_key(value)

    @field_validator("value")
    @classmethod
    def value_is_safe_scalar(cls, value: EventScalar) -> EventScalar:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("event attribute values must be JSON-safe finite scalars")
        if isinstance(value, str) and _looks_like_inline_credential(value):
            raise ValueError(
                "event attributes must not contain inline credential material"
            )
        if isinstance(value, str) and _contains_high_entropy_token(value):
            raise ValueError(
                "event attributes must not contain opaque high-entropy material"
            )
        return value


class FactoryEvent(ContractModel):
    """An append-only event with immutable typed scalar attributes."""

    event_type: Identifier
    run: RunIdentity
    occurred_at: datetime
    attributes: tuple[EventAttribute, ...] = Field(
        default_factory=tuple, max_length=MAX_FACTORY_EVENT_ATTRIBUTES
    )

    @field_validator("attributes", mode="before")
    @classmethod
    def attributes_are_a_sequence(cls, value: object) -> tuple[object, ...]:
        return _as_tuple_input(value, "attributes")

    @field_validator("occurred_at")
    @classmethod
    def event_time_is_aware(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value, "occurred_at")

    @model_validator(mode="after")
    def normalized_attribute_keys_are_unique(self) -> FactoryEvent:
        keys = tuple(
            _normalize_event_key(attribute.key) for attribute in self.attributes
        )
        _unique(keys, "event attribute keys")
        return self


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

    @model_validator(mode="after")
    def identities_agree(self) -> ClaimedRun:
        ownership_fields = (
            "task_id",
            "run_id",
            "executor_id",
            "attempt",
            "lease_id",
        )
        mismatched_fields = tuple(
            field_name
            for field_name in ownership_fields
            if getattr(self.lease.run, field_name) != getattr(self.run, field_name)
        )
        if mismatched_fields:
            raise ValueError(
                "claimed run lease identity must exactly match run for: "
                + ", ".join(mismatched_fields)
            )
        if self.envelope.task_id != self.run.task_id:
            raise ValueError("claimed run envelope task_id must agree with run")
        if self.envelope.run_id != self.run.run_id:
            raise ValueError("claimed run envelope run_id must agree with run")
        return self


class BlockedOutcome(ContractModel):
    """Explicit terminal representation for a policy/controller blocker."""

    status: Literal["blocked"] = "blocked"
    blocker: Blocker
    next_gate: NonEmptyText


class FailedOutcome(ContractModel):
    """Explicit terminal representation for a failed run."""

    status: Literal["failed"] = "failed"
    failure: Failure
    next_gate: NonEmptyText


ValidatedOutcome: TypeAlias = (
    PlanOutcome | ImplementationOutcome | ReviewOutcome | BlockedOutcome | FailedOutcome
)


def _is_successful_completion_outcome(outcome: ValidatedOutcome) -> bool:
    if isinstance(outcome, PlanOutcome):
        return outcome.status in {"planned", "ready"}
    if isinstance(outcome, ImplementationOutcome):
        return outcome.status == "candidate_ready"
    if isinstance(outcome, ReviewOutcome):
        return outcome.verdict == "APPROVED"
    return False


class TaskState(ContractModel):
    task_id: Identifier
    state: Literal["queued", "claimed", "running", "completed", "blocked", "failed"]
    run: RunIdentity | None = None
    outcome: ValidatedOutcome | None = None

    @model_validator(mode="after")
    def lifecycle_fields_are_coherent(self) -> TaskState:
        if self.run is not None and self.run.task_id != self.task_id:
            raise ValueError("task state run.task_id must agree with task_id")
        if self.state == "queued":
            if self.run is not None or self.outcome is not None:
                raise ValueError("queued task state must not include a run or outcome")
            return self
        if self.state in {"claimed", "running"}:
            if self.run is None:
                raise ValueError(f"{self.state} task state requires a run")
            if self.outcome is not None:
                raise ValueError(f"{self.state} task state must not include an outcome")
            return self
        if self.state == "completed":
            if self.run is None or self.outcome is None:
                raise ValueError("completed task state requires a run and outcome")
            if not _is_successful_completion_outcome(self.outcome):
                raise ValueError(
                    "completed task state requires a successful plan, implementation, "
                    "or approved review outcome"
                )
            return self
        if self.run is None:
            raise ValueError(f"{self.state} task state requires a run")
        if self.state == "blocked" and not isinstance(self.outcome, BlockedOutcome):
            raise ValueError("blocked task state requires an explicit BlockedOutcome")
        if self.state == "failed" and not isinstance(self.outcome, FailedOutcome):
            raise ValueError("failed task state requires an explicit FailedOutcome")
        return self


# Short aliases used by callers that treat these as the control-plane nouns.
Evidence: TypeAlias = EvidenceRef
Plan: TypeAlias = PlanOutcome
Implementation: TypeAlias = ImplementationOutcome
Review: TypeAlias = ReviewOutcome
Run: TypeAlias = RunIdentity
BlockerOutcome: TypeAlias = BlockedOutcome
FailureOutcome: TypeAlias = FailedOutcome

__all__ = [
    "MAX_EVENT_ATTRIBUTE_KEY_LENGTH",
    "MAX_EVENT_ATTRIBUTE_VALUE_LENGTH",
    "MAX_EVIDENCE_DESCRIPTION_LENGTH",
    "MAX_FACTORY_EVENT_ATTRIBUTES",
    "MAX_IDENTIFIER_LENGTH",
    "AcceptanceCriterion",
    "BlockedOutcome",
    "Blocker",
    "BlockerOutcome",
    "ChangedPath",
    "ClaimedRun",
    "CommandSpec",
    "ContractModel",
    "DecisionRequest",
    "Dependency",
    "EventAttribute",
    "EventScalar",
    "EventString",
    "Evidence",
    "EvidenceKind",
    "EvidenceRef",
    "FactoryEvent",
    "FailedOutcome",
    "Failure",
    "FailureOutcome",
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
