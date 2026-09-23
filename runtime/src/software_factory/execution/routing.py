"""Deterministic executor selection for the existing factory dispatcher.

This module is deliberately a routing boundary, not a scheduler or worker.  The
current dispatcher can select and durably record a binding before it admits a
run, then continue owning claims, leases, worktrees, retries, reclaim, and
terminal readback exactly as before.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ..api.contracts import (
    ClaimedRun,
    ContractModel,
    Identifier,
    Revision,
    RunIdentity,
    TaskEnvelope,
    TaskState,
)
from ..control.policy import (
    ROLE_KEYS,
    ExecutorKind,
    FactoryPolicy,
    RoleRoute,
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

    @model_validator(mode="before")
    @classmethod
    def normalize_routing_names(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if "run" in data:
            run = data.pop("run")
            if isinstance(run, RunIdentity):
                run_data = run.model_dump(mode="python")
            elif isinstance(run, Mapping):
                run_data = dict(run)
            else:
                raise ValueError("executor binding run must be a RunIdentity mapping")
            for field_name in ("task_id", "run_id", "attempt"):
                if field_name in data and field_name in run_data:
                    if data[field_name] != run_data[field_name]:
                        raise ValueError(
                            f"executor binding {field_name} disagrees with run"
                        )
                elif field_name in run_data:
                    data[field_name] = run_data[field_name]
            if "executor_id" not in data and run_data.get("executor_id") is not None:
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
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
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

    @model_validator(mode="before")
    @classmethod
    def normalize_retry_names(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        aliases = {
            "previous_selection": "prior_selection",
            "prior_binding": "prior_selection",
            "retry_selection": "new_selection",
            "retry_binding": "new_selection",
            "accepted": "allowed",
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
        if self.reason is RetryDecisionReason.ACTIVE_RUN:
            if (
                self.allowed
                or self.decision is not RetryDecisionKind.BACKEND_CHANGE_REJECTED
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
                ):
                    raise ValueError(
                        "allowed backend changes require explicit compatibility"
                    )
            elif (
                self.decision is not RetryDecisionKind.BACKEND_CHANGE_REJECTED
                or self.reason is not RetryDecisionReason.BACKEND_CHANGE_NOT_ALLOWED
            ):
                raise ValueError("rejected backend changes require a rejection reason")
            return self
        if (
            not self.allowed
            or self.decision is not RetryDecisionKind.SAME_BACKEND
            or self.reason is not RetryDecisionReason.SAME_BACKEND
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


def _snapshot_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return json.loads(value.model_dump_json())
    raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")


def _snapshot_json(value: object, label: str) -> str:
    try:
        if isinstance(value, BaseModel):
            return value.model_dump_json()
        return json.dumps(
            value,
            default=_snapshot_default,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise RoutingError(f"{label} has no trusted JSON snapshot") from exc


def _validated_snapshot(model_type: type[BaseModel], value: object, label: str) -> Any:
    try:
        return model_type.model_validate_json(_snapshot_json(value, label))
    except Exception as exc:
        raise RoutingError(f"{label} failed trusted validation") from exc


def _require_exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise RoutingError(f"{label} must be a built-in bool")
    return value


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
        self._policy = _validated_snapshot(FactoryPolicy, policy, "policy")
        self._policy_fingerprint = policy_fingerprint(self._policy)
        try:
            self._known_profiles = (
                None if configured_profiles is None else frozenset(configured_profiles)
            )
        except (TypeError, ValueError) as exc:
            raise RoutingError(
                "profile registry must be a stable identifier collection"
            ) from exc
        self._validate_known_profiles()

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
            raise RoutingError("unknown Hermes profile route(s): " + ", ".join(unknown))

    def _route_for(
        self, task_id: str, role: str, admitted: bool
    ) -> tuple[RoleRoute, SelectionReason]:
        _require_exact_bool(admitted, "admitted")
        if role not in self._policy.roles:
            raise RoutingError(f"unknown logical role: {role!r}")
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
            raise RoutingError(
                f"no Hermes compatibility fallback is configured for role {role!r}"
            )
        if fallback.executor is not ExecutorKind.HERMES_PROFILE:
            raise RoutingError(
                f"compatibility fallback for role {role!r} is not hermes_profile"
            )
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
            except RoutingError as exc:
                raise ExecutorSelectionError(
                    "existing binding failed trusted validation"
                ) from exc
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

    def route(self, request: DispatchRequest, **kwargs: Any) -> ExecutorBinding:
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
            if admitted is not None:
                request_data = dict(request)
                if "admitted" in request_data and request_data["admitted"] != admitted:
                    raise RoutingError("request admitted flag was specified twice")
                request_data["admitted"] = admitted
            else:
                request_data = request
            return _validated_snapshot(
                DispatchRequest, request_data, "dispatch request"
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
                raise RoutingError(f"unknown bound agent: {binding.agent!r}")
            if binding.provider is not None:
                provider = self._policy.providers.get(binding.provider)
                if provider is None:
                    raise RoutingError(f"unknown bound provider: {binding.provider!r}")
                if binding.model is not None:
                    if provider.models and binding.model not in provider.models:
                        raise RoutingError(
                            f"unknown bound model {binding.model!r} for provider "
                            f"{binding.provider!r}"
                        )
                    try:
                        _validate_model_family(
                            provider.kind, binding.model, "binding.model"
                        )
                    except ValueError as exc:
                        raise RoutingError(str(exc)) from exc
        elif binding.executor is ExecutorKind.HERMES_PROFILE:
            if (
                self._known_profiles is not None
                and binding.profile not in self._known_profiles
            ):
                raise RoutingError(f"unknown bound Hermes profile: {binding.profile!r}")
        elif binding.handler not in (
            set(self._policy.handlers) | set(self._builtin_handlers)
        ):
            raise RoutingError(
                f"unknown bound deterministic handler: {binding.handler!r}"
            )

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
        if isinstance(evidence, Mapping):
            if "state" in evidence:
                return PolicyExecutorRouter._canonical_lifecycle_evidence(
                    _validated_snapshot(TaskState, evidence, "task state evidence")
                )
            if "lease" in evidence or "envelope" in evidence:
                return PolicyExecutorRouter._canonical_lifecycle_evidence(
                    _validated_snapshot(ClaimedRun, evidence, "claimed run evidence")
                )
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

        prior = _validated_snapshot(ExecutorBinding, prior_selection, "prior binding")
        new = _validated_snapshot(ExecutorBinding, new_selection, "retry binding")
        try:
            self._validate_binding_registries(prior, require_current=False)
            self._validate_binding_registries(new, require_current=False)
            if new.policy_fingerprint != self._policy_fingerprint:
                raise RetryRoutingError(
                    "retry binding is not canonical for the active policy"
                )
            self._validate_active_binding(new)
            if prior.policy_fingerprint == self._policy_fingerprint:
                self._validate_active_binding(prior)
        except RetryRoutingError:
            raise
        except RoutingError as exc:
            raise RetryRoutingError(str(exc)) from exc
        self._validate_retry_candidates(prior, new)

        evidence = self._lifecycle_inputs(
            prior_state=prior_state,
            prior_lifecycle=prior_lifecycle,
            lifecycle_evidence=lifecycle_evidence,
        )
        lifecycle_status, lifecycle_run = self._canonical_lifecycle_evidence(evidence)
        if lifecycle_run != prior.run:
            raise RetryRoutingError("lifecycle evidence does not match the prior run")
        lifecycle_active = lifecycle_status == "active"
        for label, value in (("prior_active", prior_active), ("active", active)):
            if value is not None and value != lifecycle_active:
                raise RetryRoutingError(f"{label} contradicts lifecycle evidence")

        if lifecycle_active:
            return RetryDecision(
                task_id=prior.task_id,
                prior_run=prior.run,
                retry_run=new.run,
                prior_selection=prior,
                new_selection=new,
                allowed=False,
                decision=RetryDecisionKind.BACKEND_CHANGE_REJECTED,
                reason=RetryDecisionReason.ACTIVE_RUN,
            )
        if prior.backend_key() == new.backend_key():
            return RetryDecision(
                task_id=prior.task_id,
                prior_run=prior.run,
                retry_run=new.run,
                prior_selection=prior,
                new_selection=new,
                allowed=True,
                decision=RetryDecisionKind.SAME_BACKEND,
                reason=RetryDecisionReason.SAME_BACKEND,
            )
        if self._explicit_backend_change_allowed(prior, new):
            return RetryDecision(
                task_id=prior.task_id,
                prior_run=prior.run,
                retry_run=new.run,
                prior_selection=prior,
                new_selection=new,
                allowed=True,
                decision=RetryDecisionKind.BACKEND_CHANGE_ALLOWED,
                reason=RetryDecisionReason.EXPLICIT_COMPATIBILITY,
            )
        return RetryDecision(
            task_id=prior.task_id,
            prior_run=prior.run,
            retry_run=new.run,
            prior_selection=prior,
            new_selection=new,
            allowed=False,
            decision=RetryDecisionKind.BACKEND_CHANGE_REJECTED,
            reason=RetryDecisionReason.BACKEND_CHANGE_NOT_ALLOWED,
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
        # A caller boolean is only a request and never grants authority.  A
        # boolean policy value is likewise non-authorizing; only the explicit
        # exact-rule mode can reach the rule comparison below.
        if self._policy.compatibility.allow_backend_change_on_retry != (
            "explicit_policy_only"
        ):
            return False
        for rule in self._policy.compatibility.retry_compatibility:
            if (
                rule.role == prior.role
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
            ):
                return True
        return False

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

    canonical = _validated_snapshot(FactoryPolicy, policy, "policy")
    payload = json.dumps(
        canonical.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


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
