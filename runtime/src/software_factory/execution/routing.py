"""Deterministic executor selection for the existing factory dispatcher.

This module is deliberately a routing boundary, not a scheduler or worker.  The
current dispatcher can select and durably record a binding before it admits a
run, then continue owning claims, leases, worktrees, retries, reclaim, and
terminal readback exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Collection, Mapping
from datetime import date, datetime, time, timezone
from enum import Enum, StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .._safety import TraversalBudget, TraversalBudgetError
from ..api.contracts import (
    AcceptanceCriterion,
    BlockedOutcome,
    Blocker,
    ChangedPath,
    ClaimedRun,
    CommandSpec,
    ContractModel,
    DecisionRequest,
    Dependency,
    EventAttribute,
    FactoryEvent,
    FailedOutcome,
    Failure,
    Identifier,
    ImplementationOutcome,
    Lease,
    PlanOutcome,
    PlanTask,
    RepositoryIdentity,
    ReviewFinding,
    ReviewOutcome,
    Revision,
    RunIdentity,
    TaskConstraints,
    TaskEnvelope,
    TaskRole,
    TaskState,
    TestEvidence,
    WorkspaceIdentity,
)
from ..control.policy import (
    ROLE_KEYS,
    AgentDefinition,
    CanaryRule,
    CompatibilityPolicy,
    ExecutorKind,
    FactoryPolicy,
    HandlerDefinition,
    ImmutableMapping,
    LegacySettings,
    ProviderDefinition,
    ProviderKind,
    RetryCompatibilityRule,
    RoleRoute,
    _immutable_mapping_entries,
    _validate_exact_route_identifier,
    _validate_model_family,
)


class RoutingError(ValueError):
    """Raised when a route or immutable binding cannot be selected safely."""


class ExecutorSelectionError(RoutingError):
    """Raised when an existing run would be routed to a different backend."""


class RetryRoutingError(RoutingError):
    """Raised when a retry request is not a distinct, policy-valid run."""


class SelectionReason(StrEnum):
    """Stable reasons persisted with a route selection."""

    CANARY_EXACT_MATCH = "canary_exact_match"
    COMPATIBILITY_FALLBACK = "compatibility_fallback"
    CONFIGURED_ROUTE = "configured_route"


class RetryDecisionKind(StrEnum):
    """Stable retry decisions suitable for durable readback."""

    SAME_BACKEND = "same_backend"
    BACKEND_CHANGE_ALLOWED = "backend_change_allowed"
    BACKEND_CHANGE_REJECTED = "backend_change_rejected"


class RetryDecisionReason(StrEnum):
    """Stable retry decision causes without exception text or credentials."""

    SAME_BACKEND = "same_backend"
    EXPLICIT_COMPATIBILITY = "explicit_compatibility"
    ACTIVE_RUN = "active_run"
    BACKEND_CHANGE_NOT_ALLOWED = "backend_change_not_allowed"


class DispatchRequest(ContractModel):
    """The small input the dispatcher gives the routing adapter.

    ``admitted`` means admitted to the explicit canary allow-list, not claimed
    or leased.  Claim and lease admission remain owned by the existing
    dispatcher after it records the returned binding.
    """

    task_id: Identifier
    run_id: Identifier
    role: Identifier
    attempt: StrictInt = Field(default=1, ge=1)
    admitted: StrictBool = True

    @model_validator(mode="before")
    @classmethod
    def request_mapping_is_approved(cls, value: object) -> object:
        if type(value) not in _APPROVED_MAPPING_TYPES:
            _reject_unapproved_mapping(value, "dispatch request")
            return value
        return _approved_mapping_copy(value, "dispatch request")

    @field_validator("task_id", "run_id", "role")
    @classmethod
    def route_request_identities_are_exact(cls, value: str, info) -> str:
        return _validate_exact_route_identifier(value, f"dispatch {info.field_name}")

    @property
    def run(self) -> RunIdentity:
        """Return the run identity represented by this pre-admission request."""

        return RunIdentity(
            task_id=self.task_id, run_id=self.run_id, attempt=self.attempt
        )


class ExecutorBinding(ContractModel):
    """Immutable, serializable executor choice for one run attempt."""

    task_id: Identifier
    run_id: Identifier
    attempt: StrictInt = Field(ge=1)
    role: Identifier
    executor: ExecutorKind
    executor_id: Identifier
    agent: Identifier | None = None
    profile: Identifier | None = None
    handler: Identifier | None = None
    provider: Identifier | None = None
    model: Revision | None = None
    vendor_family: Identifier | None = None
    read_only_source: StrictBool | None = None
    policy_version: StrictInt = Field(ge=1)
    policy_fingerprint: StrictStr = Field(min_length=71, max_length=71)
    selection_reason: SelectionReason

    @field_validator(
        "task_id",
        "run_id",
        "role",
        "executor_id",
        "agent",
        "profile",
        "handler",
        "provider",
        "model",
        "vendor_family",
    )
    @classmethod
    def binding_identities_are_exact(cls, value: str | None, info) -> str | None:
        return (
            None
            if value is None
            else _validate_exact_route_identifier(value, f"binding {info.field_name}")
        )

    @model_validator(mode="before")
    @classmethod
    def normalize_routing_names(cls, value: object) -> object:
        if type(value) not in _APPROVED_MAPPING_TYPES:
            _reject_unapproved_mapping(value, "executor binding")
            return value
        data = _approved_mapping_copy(value, "executor binding")
        for field_name in ("task_id", "run_id", "attempt", "executor_id"):
            if field_name in data:
                data[field_name] = _canonical_run_identity_scalar(
                    data[field_name],
                    field_name,
                    f"executor binding {field_name}",
                )
        if "run" in data:
            run = data.pop("run")
            run_data = _binding_run_payload(run)
            for field_name in ("task_id", "run_id", "attempt"):
                if field_name in data and field_name in run_data:
                    if data[field_name] != run_data[field_name]:
                        raise ValueError(
                            f"executor binding {field_name} disagrees with run"
                        )
                elif field_name in run_data:
                    data[field_name] = run_data[field_name]
            if "executor_id" in data and "executor_id" in run_data:
                if data["executor_id"] != run_data["executor_id"]:
                    raise ValueError("executor binding executor_id disagrees with run")
            elif "executor_id" not in data and run_data.get("executor_id") is not None:
                data["executor_id"] = run_data["executor_id"]
        aliases = {
            "logical_role": "role",
            "executor_kind": "executor",
            "agent_id": "agent",
            "profile_id": "profile",
            "handler_id": "handler",
            "reason": "selection_reason",
        }
        for alias, canonical in aliases.items():
            if alias not in data:
                continue
            if canonical in data:
                raise ValueError(
                    f"executor binding defines both {alias!r} and {canonical!r}"
                )
            data[canonical] = data.pop(alias)
        return data

    @field_validator("role")
    @classmethod
    def role_is_known(cls, value: str) -> str:
        _validate_exact_route_identifier(value, "binding role")
        if value not in ROLE_KEYS:
            raise ValueError(f"unknown logical role: {value!r}")
        return value

    @field_validator("policy_fingerprint")
    @classmethod
    def fingerprint_is_stable_sha256(cls, value: str) -> str:
        if not value.startswith("sha256:"):
            raise ValueError("policy_fingerprint must use the sha256:<hex> form")
        digest = value[7:]
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(
                "policy_fingerprint must contain a lowercase SHA-256 digest"
            )
        return value

    @model_validator(mode="after")
    def route_identity_is_coherent(self) -> ExecutorBinding:
        if self.executor is ExecutorKind.PYDANTIC_AGENT:
            if self.agent is None:
                raise ValueError("pydantic_agent binding requires agent")
            if self.executor_id != self.agent:
                raise ValueError("pydantic_agent executor_id must equal agent")
            if self.profile is not None or self.handler is not None:
                raise ValueError(
                    "pydantic_agent binding cannot include profile or handler"
                )
        elif self.executor is ExecutorKind.HERMES_PROFILE:
            if self.profile is None:
                raise ValueError("hermes_profile binding requires profile")
            if self.executor_id != self.profile:
                raise ValueError("hermes_profile executor_id must equal profile")
            if self.agent is not None or self.handler is not None:
                raise ValueError(
                    "hermes_profile binding cannot include agent or handler"
                )
            if self.provider is not None or self.model is not None:
                raise ValueError(
                    "hermes_profile binding cannot include provider or model"
                )
        elif self.executor is ExecutorKind.DETERMINISTIC:
            if self.handler is None:
                raise ValueError("deterministic binding requires handler")
            if self.executor_id != self.handler:
                raise ValueError("deterministic executor_id must equal handler")
            if self.agent is not None or self.profile is not None:
                raise ValueError(
                    "deterministic binding cannot include agent or profile"
                )
            if self.provider is not None or self.model is not None:
                raise ValueError(
                    "deterministic binding cannot include provider or model"
                )
        if self.model is not None and self.provider is None:
            raise ValueError("a binding model requires a provider")
        if (
            self.selection_reason is SelectionReason.CANARY_EXACT_MATCH
            and self.executor is not ExecutorKind.PYDANTIC_AGENT
        ):
            raise ValueError("canary selection reason requires pydantic_agent")
        if (
            self.selection_reason is SelectionReason.COMPATIBILITY_FALLBACK
            and self.executor is not ExecutorKind.HERMES_PROFILE
        ):
            raise ValueError("compatibility fallback reason requires hermes_profile")
        return self

    @property
    def logical_role(self) -> str:
        """Descriptive alias for the serialized logical ``role`` field."""

        return self.role

    @property
    def executor_kind(self) -> ExecutorKind:
        """Descriptive alias for the serialized ``executor`` field."""

        return self.executor

    @property
    def concrete_identity(self) -> str:
        """Return the selected agent, profile, or handler identity."""

        return self.executor_id

    @property
    def is_canary(self) -> bool:
        return self.selection_reason is SelectionReason.CANARY_EXACT_MATCH

    @property
    def run(self) -> RunIdentity:
        """Return this binding's run identity without a mutable lease."""

        return RunIdentity(
            task_id=self.task_id,
            run_id=self.run_id,
            executor_id=self.executor_id,
            attempt=self.attempt,
        )

    def backend_key(
        self,
    ) -> tuple[
        str,
        str,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        bool | None,
    ]:
        """Return the complete concrete backend/capability selection."""

        return (
            self.executor.value,
            self.executor_id,
            self.agent,
            self.profile,
            self.handler,
            self.provider,
            self.model,
            self.vendor_family,
            self.read_only_source,
        )


class ExecutorSelectionRecord(ContractModel):
    """Readback record containing exactly one run binding and no opaque payload."""

    event_type: str = "executor_selection"
    run: RunIdentity
    binding: ExecutorBinding

    @model_validator(mode="before")
    @classmethod
    def normalize_selection_alias(cls, value: object) -> object:
        if type(value) not in _APPROVED_MAPPING_TYPES:
            _reject_unapproved_mapping(value, "selection record")
            return value
        data = _approved_mapping_copy(value, "selection record")
        if "selection" in data:
            if "binding" in data:
                raise ValueError("selection record defines both selection and binding")
            data["binding"] = data.pop("selection")
        return data

    @field_validator("event_type")
    @classmethod
    def event_type_is_stable(cls, value: str) -> str:
        if value != "executor_selection":
            raise ValueError("executor selection event_type is fixed")
        return value

    @model_validator(mode="after")
    def identities_match(self) -> ExecutorSelectionRecord:
        fields = ("task_id", "run_id", "attempt")
        if any(
            getattr(self.run, field) != getattr(self.binding, field) for field in fields
        ):
            raise ValueError("selection record run and binding identities must match")
        if self.run.executor_id != self.binding.executor_id:
            raise ValueError("selection record run executor_id must match binding")
        return self

    @property
    def selection(self) -> ExecutorBinding:
        return self.binding

    @property
    def executor_binding(self) -> ExecutorBinding:
        return self.binding


class RetryCompatibilityProof(ContractModel):
    """Complete authorization proof for one exact backend transition."""

    from_executor: ExecutorKind
    to_executor: ExecutorKind
    from_id: Identifier
    to_id: Identifier
    from_provider: Identifier | None
    to_provider: Identifier | None
    from_model: Revision | None
    to_model: Revision | None
    from_vendor_family: Identifier | None
    to_vendor_family: Identifier | None
    from_read_only_source: StrictBool | None
    to_read_only_source: StrictBool | None
    role: Identifier
    policy_version: StrictInt = Field(ge=1)
    policy_fingerprint: StrictStr = Field(min_length=71, max_length=71)
    rule_id: Identifier

    @model_validator(mode="before")
    @classmethod
    def normalize_proof_names(cls, value: object) -> object:
        if type(value) not in _APPROVED_MAPPING_TYPES:
            _reject_unapproved_mapping(value, "retry compatibility proof")
            return value
        data = _approved_mapping_copy(value, "retry compatibility proof")
        aliases = {
            "source_executor": "from_executor",
            "source_kind": "from_executor",
            "from_kind": "from_executor",
            "target_executor": "to_executor",
            "target_kind": "to_executor",
            "to_kind": "to_executor",
            "source_id": "from_id",
            "source_executor_id": "from_id",
            "from_executor_id": "from_id",
            "target_id": "to_id",
            "target_executor_id": "to_id",
            "to_executor_id": "to_id",
            "source_provider": "from_provider",
            "target_provider": "to_provider",
            "source_model": "from_model",
            "target_model": "to_model",
            "source_vendor_family": "from_vendor_family",
            "target_vendor_family": "to_vendor_family",
            "source_read_only_source": "from_read_only_source",
            "target_read_only_source": "to_read_only_source",
            "id": "rule_id",
            "rule": "rule_id",
        }
        for alias, canonical in aliases.items():
            if alias not in data:
                continue
            if canonical in data:
                raise ValueError(
                    f"retry compatibility proof defines both {alias!r} and {canonical!r}"
                )
            data[canonical] = data.pop(alias)
        return data

    @field_validator("policy_fingerprint")
    @classmethod
    def fingerprint_is_stable_sha256(cls, value: str) -> str:
        return _validate_sha256_fingerprint(value, "policy_fingerprint")

    @field_validator("rule_id")
    @classmethod
    def rule_id_is_exact(cls, value: str) -> str:
        return _validate_exact_route_identifier(value, "retry proof rule_id")

    @field_validator(
        "from_id",
        "to_id",
        "from_provider",
        "to_provider",
        "from_model",
        "to_model",
        "from_vendor_family",
        "to_vendor_family",
    )
    @classmethod
    def proof_identities_are_exact(cls, value: str | None, info) -> str | None:
        return (
            None
            if value is None
            else _validate_exact_route_identifier(
                value, f"retry proof {info.field_name}"
            )
        )

    @model_validator(mode="after")
    def proof_is_complete_and_stable(self) -> RetryCompatibilityProof:
        source = (
            self.from_executor.value,
            self.from_id,
            self.from_provider,
            self.from_model,
            self.from_vendor_family,
            self.from_read_only_source,
        )
        target = (
            self.to_executor.value,
            self.to_id,
            self.to_provider,
            self.to_model,
            self.to_vendor_family,
            self.to_read_only_source,
        )
        if source == target:
            raise ValueError(
                "retry compatibility proof must authorize a backend change"
            )
        return self


class RetryDecision(ContractModel):
    """Immutable comparison of one terminal run and one distinct retry run."""

    task_id: Identifier
    prior_run: RunIdentity
    retry_run: RunIdentity
    prior_selection: ExecutorBinding
    new_selection: ExecutorBinding
    allowed: StrictBool
    decision: RetryDecisionKind
    reason: RetryDecisionReason
    compatibility_proof: RetryCompatibilityProof | None = None

    @field_validator("task_id")
    @classmethod
    def decision_task_id_is_exact(cls, value: str) -> str:
        return _validate_exact_route_identifier(value, "retry decision task_id")

    @model_validator(mode="before")
    @classmethod
    def normalize_retry_names(cls, value: object) -> object:
        if type(value) not in _APPROVED_MAPPING_TYPES:
            _reject_unapproved_mapping(value, "retry decision")
            return value
        data = _approved_mapping_copy(value, "retry decision")
        aliases = {
            "previous_selection": "prior_selection",
            "prior_binding": "prior_selection",
            "retry_selection": "new_selection",
            "retry_binding": "new_selection",
            "accepted": "allowed",
            "compatibility": "compatibility_proof",
            "authorization": "compatibility_proof",
            "capability": "compatibility_proof",
            "proof": "compatibility_proof",
        }
        for alias, canonical in aliases.items():
            if alias not in data:
                continue
            if canonical in data:
                raise ValueError(
                    f"retry decision defines both {alias!r} and {canonical!r}"
                )
            data[canonical] = data.pop(alias)
        return data

    @model_validator(mode="after")
    def retry_identities_are_distinct_and_coherent(self) -> RetryDecision:
        if (
            self.prior_run.task_id != self.task_id
            or self.retry_run.task_id != self.task_id
        ):
            raise ValueError("retry decision task_id must match both run identities")
        if self.prior_run.run_id == self.retry_run.run_id:
            raise ValueError("a retry must use a distinct run_id")
        if self.retry_run.attempt <= self.prior_run.attempt:
            raise ValueError("a retry must use a greater attempt number")
        for run, selection, label in (
            (self.prior_run, self.prior_selection, "prior"),
            (self.retry_run, self.new_selection, "retry"),
        ):
            if run != selection.run:
                raise ValueError(f"{label} run and selection identities must match")
        if self.prior_selection.role != self.new_selection.role:
            raise ValueError(
                "retry decision selections must keep the same logical role"
            )

        backend_changed = (
            self.prior_selection.backend_key() != self.new_selection.backend_key()
        )
        if self.compatibility_proof is not None:
            proof = self.compatibility_proof
            if (
                proof.role != self.prior_selection.role
                or proof.from_executor is not self.prior_selection.executor
                or proof.to_executor is not self.new_selection.executor
                or proof.from_id != self.prior_selection.executor_id
                or proof.to_id != self.new_selection.executor_id
                or proof.from_provider != self.prior_selection.provider
                or proof.to_provider != self.new_selection.provider
                or proof.from_model != self.prior_selection.model
                or proof.to_model != self.new_selection.model
                or proof.from_vendor_family != self.prior_selection.vendor_family
                or proof.to_vendor_family != self.new_selection.vendor_family
                or proof.from_read_only_source != self.prior_selection.read_only_source
                or proof.to_read_only_source != self.new_selection.read_only_source
                or proof.policy_version != self.prior_selection.policy_version
                or proof.policy_version != self.new_selection.policy_version
                or proof.policy_fingerprint != self.prior_selection.policy_fingerprint
                or proof.policy_fingerprint != self.new_selection.policy_fingerprint
            ):
                raise ValueError("retry compatibility proof does not match selections")
        if self.reason is RetryDecisionReason.ACTIVE_RUN:
            if (
                self.allowed
                or self.decision is not RetryDecisionKind.BACKEND_CHANGE_REJECTED
                or self.compatibility_proof is not None
            ):
                raise ValueError("active-run retry decisions must reject the retry")
            return self
        if backend_changed:
            if self.decision is RetryDecisionKind.SAME_BACKEND:
                raise ValueError("same-backend retry decision has changed selections")
            if self.allowed:
                if (
                    self.decision is not RetryDecisionKind.BACKEND_CHANGE_ALLOWED
                    or self.reason is not RetryDecisionReason.EXPLICIT_COMPATIBILITY
                    or self.compatibility_proof is None
                ):
                    raise ValueError(
                        "allowed backend changes require complete compatibility proof"
                    )
            elif (
                self.decision is not RetryDecisionKind.BACKEND_CHANGE_REJECTED
                or self.reason is not RetryDecisionReason.BACKEND_CHANGE_NOT_ALLOWED
                or self.compatibility_proof is not None
            ):
                raise ValueError("rejected backend changes require a rejection reason")
            return self
        if (
            not self.allowed
            or self.decision is not RetryDecisionKind.SAME_BACKEND
            or self.reason is not RetryDecisionReason.SAME_BACKEND
            or self.compatibility_proof is not None
        ):
            raise ValueError("same-backend retry decisions must be allowed")
        return self

    @property
    def prior_binding(self) -> ExecutorBinding:
        return self.prior_selection

    @property
    def retry_binding(self) -> ExecutorBinding:
        return self.new_selection

    @property
    def previous_selection(self) -> ExecutorBinding:
        return self.prior_selection

    @property
    def retry_selection(self) -> ExecutorBinding:
        return self.new_selection


_APPROVED_MAPPING_TYPES = frozenset({dict, ImmutableMapping})
_APPROVED_SEQUENCE_TYPES = frozenset({list, tuple})

_TRUSTED_SNAPSHOT_MODELS = frozenset(
    {
        AcceptanceCriterion,
        AgentDefinition,
        BlockedOutcome,
        Blocker,
        CanaryRule,
        ChangedPath,
        ClaimedRun,
        CommandSpec,
        CompatibilityPolicy,
        DecisionRequest,
        Dependency,
        EventAttribute,
        ExecutorBinding,
        ExecutorSelectionRecord,
        FactoryEvent,
        FactoryPolicy,
        FailedOutcome,
        Failure,
        HandlerDefinition,
        ImplementationOutcome,
        Lease,
        LegacySettings,
        PlanOutcome,
        PlanTask,
        ProviderDefinition,
        RetryCompatibilityProof,
        RetryCompatibilityRule,
        RetryDecision,
        RepositoryIdentity,
        ReviewFinding,
        ReviewOutcome,
        RoleRoute,
        RunIdentity,
        TaskConstraints,
        TaskEnvelope,
        TaskState,
        TestEvidence,
        WorkspaceIdentity,
        DispatchRequest,
    }
)
_TRUSTED_SNAPSHOT_ENUMS = frozenset(
    {
        ExecutorKind,
        ProviderKind,
        RetryDecisionKind,
        RetryDecisionReason,
        SelectionReason,
        TaskRole,
    }
)


def _reject_unapproved_mapping(value: object, label: str) -> None:
    """Reject mapping subclasses/proxies before Pydantic can iterate them."""

    if type(value) in _APPROVED_MAPPING_TYPES:
        return
    if isinstance(value, Mapping):
        raise ValueError(  # noqa: TRY004 - Pydantic validators require ValueError
            f"{label} must use an approved exact mapping container"
        )


def _validate_public_route_identifier(value: object, label: str) -> str:
    """Validate route lookup identities before any map/hash/equality operation."""

    try:
        return _validate_exact_route_identifier(value, label)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RoutingError(
            "route identifier is not an exact built-in literal"
        ) from None


def _canonical_run_identity_scalar(
    value: object,
    field_name: str,
    label: str,
    *,
    allow_none: bool = False,
) -> object:
    """Admit RunIdentity scalars only as exact built-in values."""

    if allow_none and value is None:
        return None
    expected_type = int if field_name == "attempt" else str
    if type(value) is not expected_type:
        expected_name = "int" if expected_type is int else "str"
        raise ValueError(f"{label} must be a built-in {expected_name}")
    return value


def _approved_mapping_copy(value: object, label: str) -> dict[str, object]:
    """Materialize one approved mapping without invoking candidate methods."""

    if type(value) not in _APPROVED_MAPPING_TYPES:
        raise ValueError(f"{label} must use an approved exact mapping container")
    result: dict[str, object] = {}
    try:
        items = _mapping_items(value)
    except RoutingError:
        raise ValueError(
            "approved immutable mapping could not be safely inspected"
        ) from None
    for key, nested in items:
        if type(key) is not str:
            raise ValueError(f"{label} mapping keys must be built-in strings")
        if key in result:
            raise ValueError(f"{label} contains duplicate key {key!r}")
        result[key] = nested
    return result


def _exact_dict_snapshot(value: object, label: str) -> dict[object, object]:
    """Copy one exact built-in dict before inspecting any descendant."""

    if type(value) is not dict:
        raise RoutingError("snapshot requires an exact built-in dict")
    try:
        return dict.copy(value)
    except (MemoryError, RecursionError, RuntimeError):
        raise RoutingError("snapshot could not be safely copied") from None


def _exact_sequence_snapshot(
    value: object, label: str
) -> list[object] | tuple[object, ...]:
    """Copy one exact built-in sequence before inspecting any descendant."""

    if type(value) is list:
        try:
            return list.copy(value)
        except (MemoryError, RecursionError, RuntimeError):
            raise RoutingError("snapshot could not be safely copied") from None
    if type(value) is tuple:
        return value
    raise RoutingError("snapshot requires an exact built-in sequence")


def _snapshot_datetime(value: datetime, label: str) -> datetime:
    """Rebuild an exact datetime without calling candidate timezone hooks."""

    if type(value) is not datetime:
        raise RoutingError("datetime must use the exact built-in datetime type")
    try:
        tz_value = object.__getattribute__(value, "tzinfo")
        if tz_value is None:
            trusted_tz = None
        elif type(tz_value) is timezone:
            trusted_tz = tz_value
        else:
            raise RoutingError("datetime uses an unsupported timezone")
        return datetime(
            object.__getattribute__(value, "year"),
            object.__getattribute__(value, "month"),
            object.__getattribute__(value, "day"),
            object.__getattribute__(value, "hour"),
            object.__getattribute__(value, "minute"),
            object.__getattribute__(value, "second"),
            object.__getattribute__(value, "microsecond"),
            tzinfo=trusted_tz,
            fold=object.__getattribute__(value, "fold"),
        )
    except RoutingError:
        raise
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise RoutingError("datetime is not a safe built-in value") from None


def _snapshot_date(value: date, label: str) -> date:
    if type(value) is not date:
        raise RoutingError("date must use the exact built-in date type")
    try:
        return date(
            object.__getattribute__(value, "year"),
            object.__getattribute__(value, "month"),
            object.__getattribute__(value, "day"),
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise RoutingError("date is not a safe built-in value") from None


def _snapshot_time(value: time, label: str) -> time:
    """Rebuild an exact time with only naive or exact built-in timezone state."""

    if type(value) is not time:
        raise RoutingError("time must use the exact built-in time type")
    try:
        tz_value = object.__getattribute__(value, "tzinfo")
        if tz_value is None:
            trusted_tz = None
        elif type(tz_value) is timezone:
            trusted_tz = tz_value
        else:
            raise RoutingError("datetime uses an unsupported timezone")
        return time(
            object.__getattribute__(value, "hour"),
            object.__getattribute__(value, "minute"),
            object.__getattribute__(value, "second"),
            object.__getattribute__(value, "microsecond"),
            tzinfo=trusted_tz,
            fold=object.__getattribute__(value, "fold"),
        )
    except RoutingError:
        raise
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise RoutingError("time is not a safe built-in value") from None


def _mapping_items(value: object):
    """Iterate only an exact mapping admitted by the routing boundary."""

    if type(value) is dict:
        snapshot = _exact_dict_snapshot(value, "routing mapping")
        return tuple(dict.items(snapshot))
    if type(value) is ImmutableMapping:
        try:
            return _immutable_mapping_entries(value, "routing immutable mapping")
        except (TypeError, ValueError):
            raise RoutingError("routing immutable mapping is not valid") from None
    raise TypeError("mapping was not exact-admitted")


def _snapshot_value(
    value: object,
    label: str,
    depth: int = 0,
    *,
    budget: TraversalBudget | None = None,
) -> object:
    """Copy exact values with one aggregate, identity-aware budget."""

    budget = budget or TraversalBudget()
    if value is None or type(value) in {bool, int, str}:
        budget.charge_scalar(value, label)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise RoutingError("snapshot contains a non-finite float")
        budget.charge_scalar(value, label)
        return value
    if isinstance(value, Enum):
        if type(value) not in _TRUSTED_SNAPSHOT_ENUMS:
            raise RoutingError("snapshot contains an untrusted enum")
        enum_value = value.value
        if type(enum_value) not in {bool, int, float, str}:
            raise RoutingError("snapshot contains an unsupported enum value")
        budget.charge_scalar(enum_value, label)
        return enum_value
    if type(value) in {datetime, date, time}:
        if type(value) is datetime:
            trusted_value = _snapshot_datetime(value, label)
        elif type(value) is date:
            trusted_value = _snapshot_date(value, label)
        else:
            trusted_value = _snapshot_time(value, label)
        budget.charge_encoded(32, label)
        return trusted_value
    if type(value) is HttpUrl:
        converted = str(value)
        if type(converted) is not str:
            raise RoutingError("snapshot URL did not produce a built-in string")
        budget.charge_string(converted, label)
        return converted
    if isinstance(value, BaseModel):
        if type(value) not in _TRUSTED_SNAPSHOT_MODELS:
            raise RoutingError("snapshot contains an unexpected Pydantic model")
        return _snapshot_model(value, type(value), label, depth + 1, budget=budget)
    if type(value) in _APPROVED_MAPPING_TYPES:
        items = _mapping_items(value)
        length = len(items)
        budget.enter(value, depth=depth, label=label, length=length)
        result: dict[str, object] = {}
        for key, nested in items:
            if type(key) is not str:
                raise RoutingError("snapshot mapping keys must be built-in strings")
            budget.charge_string(key, "snapshot mapping key")
            if key in result:
                raise RoutingError("snapshot contains duplicate mapping keys")
            result[key] = _snapshot_value(
                nested, "nested snapshot value", depth + 1, budget=budget
            )
        return result
    if type(value) in _APPROVED_SEQUENCE_TYPES:
        exact_sequence = _exact_sequence_snapshot(value, label)
        length = len(exact_sequence)
        budget.enter(value, depth=depth, label=label, length=length)
        return tuple(
            _snapshot_value(item, f"{label}[{index}]", depth + 1, budget=budget)
            for index, item in enumerate(exact_sequence)
        )
    if isinstance(value, Mapping):
        raise RoutingError("snapshot must use an approved exact mapping container")
    raise RoutingError("snapshot contains an unsupported or hostile value")


def _snapshot_model(
    value: BaseModel,
    model_type: type[BaseModel],
    label: str,
    depth: int = 0,
    *,
    budget: TraversalBudget | None = None,
) -> dict[str, object]:
    """Read one exact model through a stable raw-state snapshot."""

    if type(value) is not model_type or model_type not in _TRUSTED_SNAPSHOT_MODELS:
        raise RoutingError("snapshot model type is not an exact trusted model")
    budget = budget or TraversalBudget()
    identity = id(value)
    cached = budget.model_snapshots.get(identity)
    if cached is not None:
        if type(cached) is not dict:
            raise RoutingError("snapshot cache contains an invalid model snapshot")
        return cached
    if identity in budget.active_models:
        raise RoutingError("snapshot contains a recursive model reference")
    budget.active_models.add(identity)
    try:
        budget.enter(value, depth=depth, label=label, length=1)
        try:
            raw_state = object.__getattribute__(value, "__dict__")
        except (AttributeError, TypeError):
            raise RoutingError("snapshot model has no trusted raw state") from None
        if type(raw_state) is not dict:
            raise RoutingError("snapshot model raw state is not an exact dict")
        state_snapshot = _exact_dict_snapshot(raw_state, f"{label}.__dict__")
        budget.enter(
            raw_state,
            depth=depth + 1,
            label=f"{label}.__dict__",
            length=len(state_snapshot),
        )
        try:
            extra = object.__getattribute__(value, "__pydantic_extra__")
        except AttributeError:
            extra = None
        try:
            private = object.__getattribute__(value, "__pydantic_private__")
        except AttributeError:
            private = None
        if extra is not None:
            if type(extra) is not dict:
                raise RoutingError("snapshot model contains unknown extra fields")
            extra_snapshot = _exact_dict_snapshot(extra, f"{label} extras")
            if extra_snapshot:
                raise RoutingError("snapshot model contains unknown extra fields")
        if private is not None:
            if type(private) is not dict:
                raise RoutingError("snapshot model contains private state")
            private_snapshot = _exact_dict_snapshot(private, f"{label} private state")
            if private_snapshot:
                raise RoutingError("snapshot model contains private state")
        declared = model_type.model_fields
        expected_names = tuple(declared)
        actual_names = tuple(state_snapshot)
        if any(type(name) is not str for name in actual_names):
            raise RoutingError("snapshot model field names must be built-in strings")
        if set(actual_names) != set(expected_names):
            raise RoutingError("snapshot model field state is not exact")
        result: dict[str, object] = {}
        for name in expected_names:
            result[name] = _snapshot_value(
                state_snapshot[name], f"{label}.{name}", depth + 2, budget=budget
            )
        budget.model_snapshots[identity] = result
        return result
    finally:
        budget.active_models.discard(identity)


def _validated_payload(
    model_type: type[BaseModel], payload: dict[str, object], label: str
) -> Any:
    try:
        return model_type.model_validate(payload)
    except (RecursionError, MemoryError, TraversalBudgetError):
        raise RoutingError(f"{label} exceeded the bounded safety limits") from None
    except Exception:  # noqa: BLE001 - normalize all validator failures safely
        raise RoutingError(f"{label} failed trusted validation") from None


def _validated_snapshot(model_type: type[BaseModel], value: object, label: str) -> Any:
    """Take a hook-free immutable snapshot, then validate a fresh model."""

    try:
        budget = TraversalBudget()
        if type(value) is model_type:
            payload = _snapshot_model(value, model_type, label, budget=budget)
        elif type(value) in _APPROVED_MAPPING_TYPES:
            payload = _snapshot_value(value, label, budget=budget)
            assert isinstance(payload, dict)
        else:
            if isinstance(value, Mapping):
                raise RoutingError(
                    f"{label} must use an approved exact mapping container"
                )
            raise RoutingError(
                f"{label} must be an exact trusted model or approved mapping"
            )
        return _validated_payload(model_type, payload, label)
    except RoutingError:
        raise
    except (RecursionError, MemoryError, TraversalBudgetError):
        raise RoutingError(f"{label} exceeded the bounded safety limits") from None
    except Exception:  # noqa: BLE001 - normalize all snapshot failures safely
        raise RoutingError(f"{label} failed trusted snapshot") from None


def _binding_run_payload(value: object) -> dict[str, object]:
    """Validate the binding's limited run alias without dropping fields."""

    if type(value) is RunIdentity:
        payload = _snapshot_model(value, RunIdentity, "executor binding run")
        if payload["lease_id"] is not None:
            raise ValueError("executor binding run must not contain lease_id")
        payload.pop("lease_id")
    elif type(value) in _APPROVED_MAPPING_TYPES:
        snapshot = _snapshot_value(value, "executor binding run")
        assert isinstance(snapshot, dict)
        payload = snapshot
    elif isinstance(value, Mapping):
        raise ValueError("executor binding run must use an approved exact mapping")
    else:
        raise ValueError("executor binding run must be an exact RunIdentity mapping")
    for field_name in ("task_id", "run_id", "attempt", "executor_id"):
        if field_name in payload:
            payload[field_name] = _canonical_run_identity_scalar(
                payload[field_name],
                field_name,
                f"executor binding run {field_name}",
                allow_none=field_name == "executor_id",
            )
    allowed = {"task_id", "run_id", "attempt", "executor_id"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(
            "executor binding run contains unknown field(s): "
            + ", ".join(sorted(unknown))
        )
    required = {"task_id", "run_id", "attempt", "executor_id"}
    missing = required - set(payload)
    if missing:
        raise ValueError(
            "executor binding run is missing field(s): " + ", ".join(sorted(missing))
        )
    canonical = _validated_payload(RunIdentity, payload, "executor binding run")
    return {
        field_name: object.__getattribute__(canonical, field_name)
        for field_name in allowed
        if field_name in payload
    }


def _snapshot_jsonable(
    value: object,
    *,
    budget: TraversalBudget | None = None,
    depth: int = 0,
) -> object:
    """Convert an already-safe snapshot with the same bounded traversal."""

    budget = budget or TraversalBudget()
    if value is None or type(value) in {bool, int, float, str}:
        if type(value) is float and not math.isfinite(value):
            raise RoutingError("trusted snapshot contains a non-finite float")
        budget.charge_scalar(value, "trusted snapshot")
        return value
    if isinstance(value, Enum):
        if type(value) not in _TRUSTED_SNAPSHOT_ENUMS:
            raise RoutingError("trusted snapshot contains an untrusted enum")
        enum_value = value.value
        budget.charge_scalar(enum_value, "trusted snapshot enum")
        return enum_value
    if type(value) in {datetime, date, time}:
        if type(value) is datetime:
            converted = _snapshot_datetime(
                value, "trusted snapshot datetime"
            ).isoformat()
        elif type(value) is date:
            converted = _snapshot_date(value, "trusted snapshot date").isoformat()
        else:
            converted = _snapshot_time(value, "trusted snapshot time").isoformat()
        budget.charge_string(converted, "trusted snapshot datetime")
        return converted
    if type(value) is dict:
        snapshot = _exact_dict_snapshot(value, "trusted snapshot")
        budget.enter(value, depth=depth, label="trusted snapshot", length=len(snapshot))
        result: dict[str, object] = {}
        for key, nested in dict.items(snapshot):
            if type(key) is not str:
                raise RoutingError("trusted snapshot mapping keys must be strings")
            budget.charge_string(key, "trusted snapshot key")
            result[key] = _snapshot_jsonable(nested, budget=budget, depth=depth + 1)
        return result
    if type(value) is tuple:
        snapshot = _exact_sequence_snapshot(value, "trusted snapshot")
        budget.enter(value, depth=depth, label="trusted snapshot", length=len(snapshot))
        return [
            _snapshot_jsonable(item, budget=budget, depth=depth + 1)
            for item in snapshot
        ]
    if isinstance(value, Mapping):
        raise RoutingError("trusted snapshot must use exact mapping containers")
    raise RoutingError("trusted snapshot is not JSON-safe")


def _validate_sha256_fingerprint(value: str, label: str) -> str:
    if not value.startswith("sha256:"):
        raise ValueError(f"{label} must use the sha256:<hex> form")
    digest = value[7:]
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"{label} must contain a lowercase SHA-256 digest")
    return value


def _require_exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise RoutingError(f"{label} must be a built-in bool")
    return value


_MAX_KNOWN_PROFILES = 4096
_APPROVED_PROFILE_CONTAINERS = frozenset(
    {list, tuple, set, frozenset, dict, ImmutableMapping}
)


def _materialize_profile_registry(value: object) -> frozenset[str]:
    """Validate profile identifiers from one exact-container snapshot."""

    if type(value) not in _APPROVED_PROFILE_CONTAINERS:
        raise RoutingError(
            "profile registry must be an exact list, tuple, set, frozenset, "
            "or approved immutable mapping"
        )
    budget = TraversalBudget()
    try:
        if type(value) in {dict, ImmutableMapping}:
            mapping_items = _mapping_items(value)
            for key, _ in mapping_items:
                if type(key) is not str:
                    raise RoutingError(
                        "profile registry mapping keys must be built-in strings"
                    )
            names = tuple(key for key, _ in mapping_items)
        elif type(value) is list:
            names = tuple(list.copy(value))
        elif type(value) is tuple:
            names = value
        elif type(value) is set:
            names = tuple(set.copy(value))
        else:
            names = tuple(frozenset(value))
        if len(names) > _MAX_KNOWN_PROFILES:
            raise RoutingError("profile registry exceeds its bounded size")
        budget.enter(names, depth=0, label="profile registry", length=len(names))
        checked: list[str] = []
        seen: set[str] = set()
        for index, name in enumerate(names):
            if type(name) is not str:
                raise RoutingError(
                    f"profile registry entry {index} must be a built-in identifier string"
                )
            budget.charge_string(name, f"profile registry entry {index}")
            if (
                not name
                or len(name) > 256
                or any(character.isspace() for character in name)
            ):
                raise RoutingError(
                    f"profile registry entry {index} must be a non-empty identifier"
                )
            if "\x00" in name:
                raise RoutingError(
                    f"profile registry entry {index} must not contain NUL"
                )
            if name in seen:
                raise RoutingError("profile registry contains duplicate identifiers")
            seen.add(name)
            checked.append(name)
        return frozenset(checked)
    except TraversalBudgetError:
        raise RoutingError(
            "profile registry exceeds the bounded safety limits"
        ) from None
    except (MemoryError, RecursionError, RuntimeError):
        raise RoutingError("profile registry could not be safely snapshotted") from None


@runtime_checkable
class ExecutorSelectionAdapter(Protocol):
    """Protocol the current dispatcher can call before its existing admission."""

    def select(
        self,
        request: DispatchRequest,
        *,
        existing_binding: ExecutorBinding | None = None,
    ) -> ExecutorBinding: ...

    def decide_retry(
        self,
        prior_selection: ExecutorBinding,
        new_selection: ExecutorBinding,
        *,
        prior_state: TaskState | ClaimedRun | Mapping[str, Any] | None = None,
        prior_lifecycle: TaskState | ClaimedRun | Mapping[str, Any] | None = None,
        lifecycle_evidence: TaskState | ClaimedRun | Mapping[str, Any] | None = None,
        prior_active: bool | None = None,
    ) -> RetryDecision: ...


@runtime_checkable
class ExecutorAdapter(Protocol):
    """Minimal execution adapter contract; this package does not implement workers."""

    kind: ExecutorKind
    identity: str

    def execute(self, envelope: TaskEnvelope, binding: ExecutorBinding) -> object:
        """Execute a previously selected binding without selecting a new route."""
        ...


class PolicyExecutorRouter:
    """Select immutable routes from one validated policy.

    The class has no queue, task claim, lease, process, thread, or retry loop.
    It is safe for a dispatcher to call it once before admission and again for
    durable readback; a bound active run cannot be silently re-routed.
    """

    def __init__(
        self,
        policy: FactoryPolicy | Mapping[str, Any],
        *,
        known_profiles: Collection[str] | Mapping[str, object] | None = None,
        profiles: Collection[str] | Mapping[str, object] | None = None,
        profile_registry: Collection[str] | Mapping[str, object] | None = None,
    ) -> None:
        if (
            sum(
                value is not None
                for value in (known_profiles, profiles, profile_registry)
            )
            > 1
        ):
            raise RoutingError(
                "provide at most one of known_profiles, profiles, or profile_registry"
            )
        configured_profiles = next(
            (
                value
                for value in (known_profiles, profiles, profile_registry)
                if value is not None
            ),
            None,
        )
        try:
            self._policy = _validated_snapshot(FactoryPolicy, policy, "policy")
            self._policy_fingerprint = policy_fingerprint(self._policy)
            self._known_profiles = (
                None
                if configured_profiles is None
                else _materialize_profile_registry(configured_profiles)
            )
            self._validate_known_profiles()
        except RoutingError:
            raise
        except (RecursionError, MemoryError, TraversalBudgetError):
            raise RoutingError("policy exceeds the bounded safety limits") from None

    @property
    def policy(self) -> FactoryPolicy:
        return self._policy

    @property
    def policy_fingerprint(self) -> str:
        return self._policy_fingerprint

    def _validate_known_profiles(self) -> None:
        if self._known_profiles is None:
            return
        routes = [
            *self._policy.roles.values(),
            *self._policy.compatibility.fallback_executors.values(),
        ]
        unknown = sorted(
            {
                route.profile
                for route in routes
                if route.executor is ExecutorKind.HERMES_PROFILE
                and route.profile is not None
                and route.profile not in self._known_profiles
            }
        )
        if unknown:
            raise RoutingError("unknown Hermes profile route(s)")

    def _route_for(
        self, task_id: str, role: str, admitted: bool
    ) -> tuple[RoleRoute, SelectionReason]:
        task_id = _validate_public_route_identifier(task_id, "route task_id")
        role = _validate_public_route_identifier(role, "route role")
        _require_exact_bool(admitted, "admitted")
        if role not in self._policy.roles:
            raise RoutingError("unknown logical role in policy routing")
        route = self._policy.roles[role]
        if route.executor is not ExecutorKind.PYDANTIC_AGENT:
            return route, SelectionReason.CONFIGURED_ROUTE
        if not self._policy.compatibility.canary_only:
            return route, SelectionReason.CONFIGURED_ROUTE
        canary_match = admitted and any(
            rule.task_id == task_id and rule.role == role
            for rule in self._policy.compatibility.canary_rules
        )
        if canary_match:
            return route, SelectionReason.CANARY_EXACT_MATCH
        fallback = self._policy.compatibility.fallback_executors.get(role)
        if fallback is None:
            raise RoutingError("no Hermes compatibility fallback is configured")
        if fallback.executor is not ExecutorKind.HERMES_PROFILE:
            raise RoutingError("compatibility fallback is not hermes_profile")
        return fallback, SelectionReason.COMPATIBILITY_FALLBACK

    def resolve_route(
        self, *, task_id: str, role: str, admitted: bool = True
    ) -> tuple[RoleRoute, SelectionReason]:
        """Resolve a route without creating a run binding."""

        return self._route_for(task_id, role, admitted)

    def select(
        self,
        request: DispatchRequest
        | RunIdentity
        | TaskEnvelope
        | Mapping[str, Any]
        | None = None,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        role: str | None = None,
        attempt: int | None = None,
        admitted: bool | None = None,
        run: RunIdentity | None = None,
        existing_binding: ExecutorBinding | None = None,
        active_binding: ExecutorBinding | None = None,
        bound_binding: ExecutorBinding | None = None,
    ) -> ExecutorBinding:
        """Select and return one immutable binding before dispatcher admission."""

        for label, value in (("task_id", task_id), ("run_id", run_id), ("role", role)):
            if value is not None:
                _validate_public_route_identifier(value, f"selection {label}")
        if admitted is not None:
            _require_exact_bool(admitted, "admitted")
        provided_bindings = tuple(
            binding
            for binding in (existing_binding, active_binding, bound_binding)
            if binding is not None
        )
        if len(provided_bindings) > 1:
            raise RoutingError("provide only one existing active binding")
        if run is not None:
            run = _validated_snapshot(RunIdentity, run, "run")
            if request is not None:
                raise RoutingError("provide request or run, not both")
            if any(value is not None for value in (task_id, run_id, attempt)):
                raise RoutingError("run cannot be combined with explicit run fields")
            if role is None:
                request = run
            else:
                request = DispatchRequest(
                    task_id=run.task_id,
                    run_id=run.run_id,
                    role=role,
                    attempt=run.attempt,
                    admitted=True if admitted is None else admitted,
                )
            role = None
        elif isinstance(request, RunIdentity) and role is not None:
            request = _validated_snapshot(RunIdentity, request, "run")
            request = DispatchRequest(
                task_id=request.task_id,
                run_id=request.run_id,
                role=role,
                attempt=request.attempt,
                admitted=True if admitted is None else admitted,
            )
            role = None
        request = self._coerce_request(
            request,
            task_id=task_id,
            run_id=run_id,
            role=role,
            attempt=attempt,
            admitted=admitted,
        )
        existing = None
        if provided_bindings:
            try:
                existing = _validated_snapshot(
                    ExecutorBinding, provided_bindings[0], "existing binding"
                )
            except RoutingError:
                raise ExecutorSelectionError(
                    "existing binding failed trusted validation"
                ) from None
        route, reason = self._route_for(request.task_id, request.role, request.admitted)
        binding = self._binding_for(request, route, reason)
        if existing is None:
            return binding
        self._validate_existing_identity(existing, request)
        self._validate_binding_registries(existing, require_current=False)
        if existing != binding:
            if existing.backend_key() != binding.backend_key():
                raise ExecutorSelectionError(
                    "bound active run cannot change executor route"
                )
            raise ExecutorSelectionError(
                "bound active run binding is not canonical for the active policy"
            )
        # Return the trusted snapshot, never the caller's unvalidated object.
        return existing

    def route(
        self,
        request: DispatchRequest
        | RunIdentity
        | TaskEnvelope
        | Mapping[str, Any]
        | None = None,
        **kwargs: Any,
    ) -> ExecutorBinding:
        """Adapter alias used by dispatchers that call their boundary ``route``."""

        return self.select(request, **kwargs)

    def select_executor(self, *args: Any, **kwargs: Any) -> ExecutorBinding:
        """Explicitly named adapter call for existing dispatcher code."""

        return self.select(*args, **kwargs)

    def _coerce_request(
        self,
        request: DispatchRequest
        | RunIdentity
        | TaskEnvelope
        | Mapping[str, Any]
        | None,
        *,
        task_id: str | None,
        run_id: str | None,
        role: str | None,
        attempt: int | None,
        admitted: bool | None,
    ) -> DispatchRequest:
        if admitted is not None:
            _require_exact_bool(admitted, "admitted")
        supplied = (task_id, run_id, role, attempt)
        if request is not None and any(value is not None for value in supplied):
            raise RoutingError(
                "request and explicit task/run fields cannot be combined"
            )
        if isinstance(request, DispatchRequest):
            canonical = _validated_snapshot(
                DispatchRequest, request, "dispatch request"
            )
            if admitted is not None and admitted != canonical.admitted:
                raise RoutingError("request admitted flag was specified twice")
            return canonical
        if isinstance(request, TaskEnvelope):
            canonical = _validated_snapshot(TaskEnvelope, request, "task envelope")
            return DispatchRequest(
                task_id=canonical.task_id,
                run_id=canonical.run_id,
                role=canonical.role.value,
                admitted=True if admitted is None else admitted,
            )
        if isinstance(request, RunIdentity):
            canonical = _validated_snapshot(RunIdentity, request, "run")
            if role is None:
                raise RoutingError("a logical role is required with RunIdentity")
            return DispatchRequest(
                task_id=canonical.task_id,
                run_id=canonical.run_id,
                role=role,
                attempt=canonical.attempt,
                admitted=True if admitted is None else admitted,
            )
        if request is not None:
            if type(request) not in _APPROVED_MAPPING_TYPES:
                raise RoutingError("dispatch request must use an approved mapping")
            try:
                request_snapshot = _snapshot_value(request, "dispatch request")
            except (RecursionError, MemoryError, TraversalBudgetError):
                raise RoutingError(
                    "dispatch request exceeds the bounded safety limits"
                ) from None
            assert isinstance(request_snapshot, dict)
            if admitted is not None:
                if "admitted" in request_snapshot:
                    request_admitted = _require_exact_bool(
                        request_snapshot["admitted"], "request admitted"
                    )
                    if request_admitted is not admitted:
                        raise RoutingError("request admitted flag was specified twice")
                request_snapshot["admitted"] = admitted
            return _validated_payload(
                DispatchRequest, request_snapshot, "dispatch request"
            )
        if task_id is None or run_id is None or role is None:
            raise RoutingError("task_id, run_id, and role are required for selection")
        return DispatchRequest(
            task_id=task_id,
            run_id=run_id,
            role=role,
            attempt=1 if attempt is None else attempt,
            admitted=True if admitted is None else admitted,
        )

    def _binding_for(
        self, request: DispatchRequest, route: RoleRoute, reason: SelectionReason
    ) -> ExecutorBinding:
        if route.executor is ExecutorKind.PYDANTIC_AGENT:
            assert route.agent is not None
            executor_id = route.agent
        elif route.executor is ExecutorKind.HERMES_PROFILE:
            assert route.profile is not None
            executor_id = route.profile
        else:
            assert route.handler is not None
            executor_id = route.handler
        return ExecutorBinding(
            task_id=request.task_id,
            run_id=request.run_id,
            attempt=request.attempt,
            role=request.role,
            executor=route.executor,
            executor_id=executor_id,
            agent=route.agent,
            profile=route.profile,
            handler=route.handler,
            provider=route.provider,
            model=route.model,
            vendor_family=route.vendor_family,
            read_only_source=route.read_only_source,
            policy_version=self._policy.version,
            policy_fingerprint=self._policy_fingerprint,
            selection_reason=reason,
        )

    @staticmethod
    def _validate_existing_identity(
        existing: ExecutorBinding, request: DispatchRequest
    ) -> None:
        if (
            existing.task_id != request.task_id
            or existing.run_id != request.run_id
            or existing.attempt != request.attempt
            or existing.role != request.role
        ):
            raise ExecutorSelectionError(
                "existing binding identity does not match the selected run"
            )

    def _validate_binding_registries(
        self, binding: ExecutorBinding, *, require_current: bool
    ) -> None:
        """Check serialized binding authority against the active registries."""

        if binding.policy_version != self._policy.version:
            raise RoutingError("binding policy_version is not active")
        if require_current and binding.policy_fingerprint != self._policy_fingerprint:
            raise RoutingError("binding policy_fingerprint is not active")
        if binding.executor is ExecutorKind.PYDANTIC_AGENT:
            if binding.agent not in self._policy.agents:
                raise RoutingError("unknown bound agent")
            if binding.provider is not None:
                provider = self._policy.providers.get(binding.provider)
                if provider is None:
                    raise RoutingError("unknown bound provider")
                if binding.model is not None:
                    if provider.models and binding.model not in provider.models:
                        raise RoutingError("unknown bound model for provider")
                    try:
                        _validate_model_family(
                            provider.kind, binding.model, "binding.model"
                        )
                    except ValueError:
                        raise RoutingError(
                            "bound model does not match provider"
                        ) from None
        elif binding.executor is ExecutorKind.HERMES_PROFILE:
            if (
                self._known_profiles is not None
                and binding.profile not in self._known_profiles
            ):
                raise RoutingError("unknown bound Hermes profile")
        elif binding.handler not in (
            set(self._policy.handlers) | set(self._builtin_handlers)
        ):
            raise RoutingError("unknown bound deterministic handler")

    @property
    def _builtin_handlers(self) -> frozenset[str]:
        # Keep the registry check local to the router without making lifecycle
        # or handler execution part of this module's responsibilities.
        from ..control.policy import BUILTIN_DETERMINISTIC_HANDLERS

        return BUILTIN_DETERMINISTIC_HANDLERS

    def _validate_active_binding(self, binding: ExecutorBinding) -> None:
        self._validate_binding_registries(binding, require_current=True)
        admitted_values: tuple[bool, ...]
        if binding.selection_reason is SelectionReason.CANARY_EXACT_MATCH:
            admitted_values = (True,)
        elif binding.selection_reason is SelectionReason.COMPATIBILITY_FALLBACK:
            admitted_values = (False,)
        else:
            admitted_values = (False, True)
        for admitted in admitted_values:
            request = DispatchRequest(
                task_id=binding.task_id,
                run_id=binding.run_id,
                role=binding.role,
                attempt=binding.attempt,
                admitted=admitted,
            )
            route, reason = self._route_for(
                request.task_id, request.role, request.admitted
            )
            if reason is not binding.selection_reason:
                continue
            if self._binding_for(request, route, reason) == binding:
                return
        raise RoutingError("binding is not canonical for the active policy")

    @staticmethod
    def _canonical_lifecycle_evidence(
        evidence: TaskState | ClaimedRun | Mapping[str, Any],
    ) -> tuple[str, RunIdentity]:
        if isinstance(evidence, TaskState):
            state = _validated_snapshot(TaskState, evidence, "task state evidence")
            run = state.run
            if run is None:
                raise RetryRoutingError("lifecycle evidence has no prior run identity")
            if state.state in {"claimed", "running"}:
                return "active", run
            if state.state in {"completed", "blocked", "failed"}:
                return "terminal", run
            raise RetryRoutingError("lifecycle evidence is not authoritative")
        if isinstance(evidence, ClaimedRun):
            claimed = _validated_snapshot(ClaimedRun, evidence, "claimed run evidence")
            return "active", claimed.run
        if isinstance(evidence, RunIdentity):
            raise RetryRoutingError(
                "authoritative terminal lifecycle evidence is required, not RunIdentity"
            )
        if type(evidence) in _APPROVED_MAPPING_TYPES:
            try:
                snapshot = _snapshot_value(evidence, "lifecycle evidence")
            except (RecursionError, MemoryError, TraversalBudgetError):
                raise RetryRoutingError(
                    "lifecycle evidence exceeds the bounded safety limits"
                ) from None
            assert isinstance(snapshot, dict)
            if "state" in snapshot:
                return PolicyExecutorRouter._canonical_lifecycle_evidence(
                    _validated_payload(TaskState, snapshot, "task state evidence")
                )
            if "lease" in snapshot or "envelope" in snapshot:
                return PolicyExecutorRouter._canonical_lifecycle_evidence(
                    _validated_payload(ClaimedRun, snapshot, "claimed run evidence")
                )
        elif isinstance(evidence, Mapping):
            raise RetryRoutingError("lifecycle evidence must use an approved mapping")
        raise RetryRoutingError("lifecycle evidence is not authoritative")

    @staticmethod
    def _lifecycle_inputs(
        *,
        prior_state: TaskState | ClaimedRun | Mapping[str, Any] | None,
        prior_lifecycle: TaskState | ClaimedRun | Mapping[str, Any] | None,
        lifecycle_evidence: TaskState | ClaimedRun | Mapping[str, Any] | None,
    ) -> TaskState | ClaimedRun | Mapping[str, Any]:
        supplied = tuple(
            value
            for value in (prior_state, prior_lifecycle, lifecycle_evidence)
            if value is not None
        )
        if not supplied:
            raise RetryRoutingError(
                "authoritative terminal lifecycle evidence is required"
            )
        if len(supplied) > 1:
            raise RetryRoutingError("provide only one lifecycle evidence value")
        return supplied[0]

    @staticmethod
    def _lifecycle_identity_matches(
        binding_run: RunIdentity, evidence_run: RunIdentity
    ) -> bool:
        """Match the complete logical run while permitting an authoritative lease."""

        logical_fields = ("task_id", "run_id", "executor_id", "attempt")
        if any(
            object.__getattribute__(binding_run, field_name)
            != object.__getattribute__(evidence_run, field_name)
            for field_name in logical_fields
        ):
            return False
        binding_lease = object.__getattribute__(binding_run, "lease_id")
        evidence_lease = object.__getattribute__(evidence_run, "lease_id")
        return binding_lease is None or binding_lease == evidence_lease

    @staticmethod
    def _retry_rule_matches(
        rule: RetryCompatibilityRule,
        prior: ExecutorBinding,
        new: ExecutorBinding,
    ) -> bool:
        return (
            rule.role == prior.role == new.role
            and rule.from_executor is prior.executor
            and rule.to_executor is new.executor
            and rule.from_id == prior.executor_id
            and rule.to_id == new.executor_id
            and rule.from_provider == prior.provider
            and rule.to_provider == new.provider
            and rule.from_model == prior.model
            and rule.to_model == new.model
            and rule.from_vendor_family == prior.vendor_family
            and rule.to_vendor_family == new.vendor_family
            and rule.from_read_only_source == prior.read_only_source
            and rule.to_read_only_source == new.read_only_source
        )

    def _matching_retry_rule(
        self, prior: ExecutorBinding, new: ExecutorBinding
    ) -> RetryCompatibilityRule | None:
        if self._policy.compatibility.allow_backend_change_on_retry != (
            "explicit_policy_only"
        ):
            return None
        matches = tuple(
            rule
            for rule in self._policy.compatibility.retry_compatibility
            if self._retry_rule_matches(rule, prior, new)
        )
        if len(matches) > 1:
            raise RetryRoutingError(
                "retry compatibility proof must correspond to exactly one active policy rule"
            )
        return matches[0] if matches else None

    def _compatibility_proof(
        self,
        prior: ExecutorBinding,
        new: ExecutorBinding,
        rule: RetryCompatibilityRule,
    ) -> RetryCompatibilityProof:
        return RetryCompatibilityProof(
            from_executor=prior.executor,
            to_executor=new.executor,
            from_id=prior.executor_id,
            to_id=new.executor_id,
            from_provider=prior.provider,
            to_provider=new.provider,
            from_model=prior.model,
            to_model=new.model,
            from_vendor_family=prior.vendor_family,
            to_vendor_family=new.vendor_family,
            from_read_only_source=prior.read_only_source,
            to_read_only_source=new.read_only_source,
            role=prior.role,
            policy_version=self._policy.version,
            policy_fingerprint=self._policy_fingerprint,
            rule_id=rule.rule_id,
        )

    def _validate_retry_decision_authorization(
        self, decision: RetryDecision, *, allow_lifecycle_override: bool = False
    ) -> None:
        """Recompute backend-change authority from the active policy."""

        self._validate_active_binding(decision.prior_selection)
        self._validate_active_binding(decision.new_selection)
        backend_changed = (
            decision.prior_selection.backend_key()
            != decision.new_selection.backend_key()
        )
        if not backend_changed:
            if decision.compatibility_proof is not None:
                raise RetryRoutingError(
                    "same-backend retry decisions must not carry compatibility proof"
                )
            return

        if decision.reason is RetryDecisionReason.ACTIVE_RUN:
            if not allow_lifecycle_override:
                raise RetryRoutingError(
                    "active-run retry decisions require lifecycle evidence"
                )
            return

        rule = self._matching_retry_rule(
            decision.prior_selection, decision.new_selection
        )
        if rule is None:
            if decision.allowed:
                raise RetryRoutingError(
                    "allowed backend change has no exact active policy rule"
                )
            if decision.compatibility_proof is not None:
                raise RetryRoutingError(
                    "rejected backend changes must not carry compatibility proof"
                )
            return
        if not decision.allowed:
            raise RetryRoutingError(
                "rejected backend change is authorized by an exact active policy rule"
            )
        expected = self._compatibility_proof(
            decision.prior_selection, decision.new_selection, rule
        )
        if decision.compatibility_proof != expected:
            raise RetryRoutingError(
                "retry compatibility proof is forged or not canonical for the active policy"
            )

    def validate_retry_decision(
        self, decision: RetryDecision | Mapping[str, Any]
    ) -> RetryDecision:
        """Validate direct retry-decision readback against this active policy."""

        try:
            canonical = _validated_snapshot(RetryDecision, decision, "retry decision")
        except RoutingError:
            raise RetryRoutingError(
                "retry decision failed trusted validation"
            ) from None
        try:
            self._validate_retry_decision_authorization(canonical)
        except RetryRoutingError:
            raise
        except RoutingError:
            raise RetryRoutingError("retry decision authorization failed") from None
        return canonical

    def _finalize_retry_decision(
        self, decision: RetryDecision, *, allow_lifecycle_override: bool = False
    ) -> RetryDecision:
        try:
            canonical = _validated_snapshot(RetryDecision, decision, "retry decision")
        except RoutingError:
            raise RetryRoutingError(
                "retry decision failed trusted validation"
            ) from None
        try:
            self._validate_retry_decision_authorization(
                canonical, allow_lifecycle_override=allow_lifecycle_override
            )
        except RetryRoutingError:
            raise
        except RoutingError:
            raise RetryRoutingError("retry decision authorization failed") from None
        return canonical

    def decide_retry(
        self,
        prior_selection: ExecutorBinding,
        new_selection: ExecutorBinding,
        *,
        prior_state: TaskState | ClaimedRun | Mapping[str, Any] | None = None,
        prior_lifecycle: TaskState | ClaimedRun | Mapping[str, Any] | None = None,
        lifecycle_evidence: TaskState | ClaimedRun | Mapping[str, Any] | None = None,
        prior_active: bool | None = None,
        active: bool | None = None,
        allow_backend_change: bool | None = None,
        explicit_policy: bool | None = None,
    ) -> RetryDecision:
        """Compare one terminal run with one distinct, policy-valid retry.

        Lifecycle state is evidence owned by the dispatcher.  This adapter only
        validates and compares a supplied snapshot; it never queries or mutates
        the lifecycle repository.
        """

        bool_hints = (
            ("prior_active", prior_active),
            ("active", active),
            ("allow_backend_change", allow_backend_change),
            ("explicit_policy", explicit_policy),
        )
        for label, value in bool_hints:
            if value is not None:
                _require_exact_bool(value, label)
        if prior_active is not None and active is not None and prior_active != active:
            raise RetryRoutingError("prior_active and active disagree")
        if (
            allow_backend_change is not None
            and explicit_policy is not None
            and allow_backend_change != explicit_policy
        ):
            raise RetryRoutingError("allow_backend_change and explicit_policy disagree")

        try:
            prior = _validated_snapshot(
                ExecutorBinding, prior_selection, "prior binding"
            )
            new = _validated_snapshot(ExecutorBinding, new_selection, "retry binding")
        except RoutingError:
            raise RetryRoutingError("retry binding failed trusted validation") from None
        try:
            if (
                prior.policy_version != self._policy.version
                or prior.policy_fingerprint != self._policy_fingerprint
                or new.policy_version != self._policy.version
                or new.policy_fingerprint != self._policy_fingerprint
            ):
                raise RetryRoutingError(
                    "cross-policy retries are unsupported; both bindings must be current"
                )
            self._validate_binding_registries(prior, require_current=True)
            self._validate_binding_registries(new, require_current=True)
            self._validate_active_binding(prior)
            self._validate_active_binding(new)
        except RetryRoutingError:
            raise
        except RoutingError:
            raise RetryRoutingError(
                "retry bindings failed active-policy validation"
            ) from None
        self._validate_retry_candidates(prior, new)

        evidence = self._lifecycle_inputs(
            prior_state=prior_state,
            prior_lifecycle=prior_lifecycle,
            lifecycle_evidence=lifecycle_evidence,
        )
        try:
            lifecycle_status, lifecycle_run = self._canonical_lifecycle_evidence(
                evidence
            )
        except RetryRoutingError:
            raise
        except RoutingError:
            raise RetryRoutingError(
                "lifecycle evidence failed trusted validation"
            ) from None
        if not self._lifecycle_identity_matches(prior.run, lifecycle_run):
            raise RetryRoutingError("lifecycle evidence does not match the prior run")
        lifecycle_active = lifecycle_status == "active"
        for label, value in (("prior_active", prior_active), ("active", active)):
            if value is not None and value != lifecycle_active:
                raise RetryRoutingError(f"{label} contradicts lifecycle evidence")

        if lifecycle_active:
            return self._finalize_retry_decision(
                RetryDecision(
                    task_id=prior.task_id,
                    prior_run=prior.run,
                    retry_run=new.run,
                    prior_selection=prior,
                    new_selection=new,
                    allowed=False,
                    decision=RetryDecisionKind.BACKEND_CHANGE_REJECTED,
                    reason=RetryDecisionReason.ACTIVE_RUN,
                ),
                allow_lifecycle_override=True,
            )
        if prior.backend_key() == new.backend_key():
            return self._finalize_retry_decision(
                RetryDecision(
                    task_id=prior.task_id,
                    prior_run=prior.run,
                    retry_run=new.run,
                    prior_selection=prior,
                    new_selection=new,
                    allowed=True,
                    decision=RetryDecisionKind.SAME_BACKEND,
                    reason=RetryDecisionReason.SAME_BACKEND,
                )
            )
        rule = self._matching_retry_rule(prior, new)
        if rule is not None:
            return self._finalize_retry_decision(
                RetryDecision(
                    task_id=prior.task_id,
                    prior_run=prior.run,
                    retry_run=new.run,
                    prior_selection=prior,
                    new_selection=new,
                    allowed=True,
                    decision=RetryDecisionKind.BACKEND_CHANGE_ALLOWED,
                    reason=RetryDecisionReason.EXPLICIT_COMPATIBILITY,
                    compatibility_proof=self._compatibility_proof(prior, new, rule),
                )
            )
        return self._finalize_retry_decision(
            RetryDecision(
                task_id=prior.task_id,
                prior_run=prior.run,
                retry_run=new.run,
                prior_selection=prior,
                new_selection=new,
                allowed=False,
                decision=RetryDecisionKind.BACKEND_CHANGE_REJECTED,
                reason=RetryDecisionReason.BACKEND_CHANGE_NOT_ALLOWED,
            )
        )

    def retry(
        self,
        prior_selection: ExecutorBinding,
        new_selection: ExecutorBinding | None = None,
        *,
        request: DispatchRequest
        | RunIdentity
        | TaskEnvelope
        | Mapping[str, Any]
        | None = None,
        retry_request: DispatchRequest
        | RunIdentity
        | TaskEnvelope
        | Mapping[str, Any]
        | None = None,
        prior_state: TaskState | ClaimedRun | Mapping[str, Any] | None = None,
        prior_lifecycle: TaskState | ClaimedRun | Mapping[str, Any] | None = None,
        lifecycle_evidence: TaskState | ClaimedRun | Mapping[str, Any] | None = None,
        prior_active: bool | None = None,
        active: bool | None = None,
        allow_backend_change: bool | None = None,
        explicit_policy: bool | None = None,
    ) -> RetryDecision:
        """Convenience adapter for a retry selection followed by comparison."""

        if request is not None and retry_request is not None:
            raise RetryRoutingError("provide request or retry_request, not both")
        request = request if request is not None else retry_request
        if new_selection is not None and request is not None:
            raise RetryRoutingError("provide new_selection or request, not both")
        if new_selection is None:
            if request is None:
                raise RetryRoutingError("a new retry selection or request is required")
            new_selection = self.select(request)
        return self.decide_retry(
            prior_selection,
            new_selection,
            prior_state=prior_state,
            prior_lifecycle=prior_lifecycle,
            lifecycle_evidence=lifecycle_evidence,
            prior_active=prior_active,
            active=active,
            allow_backend_change=allow_backend_change,
            explicit_policy=explicit_policy,
        )

    evaluate_retry = decide_retry
    retry_decision = decide_retry

    def _explicit_backend_change_allowed(
        self, prior: ExecutorBinding, new: ExecutorBinding
    ) -> bool:
        """Compatibility shim that never grants authority by itself."""

        return self._matching_retry_rule(prior, new) is not None

    @staticmethod
    def _validate_retry_candidates(
        prior: ExecutorBinding, new: ExecutorBinding
    ) -> None:
        if prior.task_id != new.task_id:
            raise RetryRoutingError("a retry must keep the same task_id")
        if prior.role != new.role:
            raise RetryRoutingError("a retry must keep the same logical role")
        if prior.run_id == new.run_id:
            raise RetryRoutingError("a retry must use a distinct run_id")
        if new.attempt <= prior.attempt:
            raise RetryRoutingError("a retry must use a greater attempt number")


def policy_fingerprint(policy: FactoryPolicy | Mapping[str, Any]) -> str:
    """Return a deterministic credential-free fingerprint of validated policy."""

    try:
        canonical = _validated_snapshot(FactoryPolicy, policy, "policy")
        budget = TraversalBudget()
        snapshot = _snapshot_model(canonical, FactoryPolicy, "policy", budget=budget)
        jsonable = _snapshot_jsonable(snapshot, budget=budget)
        payload = json.dumps(
            jsonable,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        budget.charge_encoded(len(payload), "policy fingerprint")
        return "sha256:" + hashlib.sha256(payload).hexdigest()
    except RoutingError:
        raise
    except (RecursionError, MemoryError, TraversalBudgetError):
        raise RoutingError(
            "policy fingerprint exceeds the bounded safety limits"
        ) from None


# Focused aliases keep the boundary discoverable for dispatcher integrations.
ExecutorRouter = PolicyExecutorRouter
PluggableExecutorRouter = PolicyExecutorRouter
ExecutorSelectionRouter = PolicyExecutorRouter
ExecutorSelection = ExecutorBinding
ExecutionBinding = ExecutorBinding
RunBinding = ExecutorBinding
RouteSelection = ExecutorBinding
ExecutorBindingEvent = ExecutorSelectionRecord
SelectionEvent = ExecutorSelectionRecord
RetryRoutingDecision = RetryDecision
RetryPolicyDecision = RetryDecision

__all__ = [
    "DispatchRequest",
    "ExecutionBinding",
    "ExecutorAdapter",
    "ExecutorBinding",
    "ExecutorBindingEvent",
    "ExecutorRouter",
    "ExecutorSelection",
    "ExecutorSelectionAdapter",
    "ExecutorSelectionError",
    "ExecutorSelectionRecord",
    "ExecutorSelectionRouter",
    "PluggableExecutorRouter",
    "PolicyExecutorRouter",
    "RetryCompatibilityProof",
    "RetryDecision",
    "RetryDecisionKind",
    "RetryDecisionReason",
    "RetryPolicyDecision",
    "RetryRoutingDecision",
    "RetryRoutingError",
    "RouteSelection",
    "RoutingError",
    "RunBinding",
    "SelectionEvent",
    "SelectionReason",
    "policy_fingerprint",
]
