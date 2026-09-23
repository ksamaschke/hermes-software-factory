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
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ..api.contracts import (
    ContractModel,
    Identifier,
    Revision,
    RunIdentity,
    TaskEnvelope,
)
from ..control.policy import ROLE_KEYS, ExecutorKind, FactoryPolicy, RoleRoute


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
    ) -> tuple[str, str, str | None, str | None, str | None, str | None, str | None]:
        """Return only route identity fields used for retry comparison."""

        return (
            self.executor.value,
            self.executor_id,
            self.agent,
            self.profile,
            self.handler,
            self.provider,
            self.model,
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
        if (
            self.run.executor_id is not None
            and self.run.executor_id != self.binding.executor_id
        ):
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
            if (
                run.task_id != selection.task_id
                or run.run_id != selection.run_id
                or run.attempt != selection.attempt
            ):
                raise ValueError(f"{label} run and selection identities must match")
            if run.executor_id is not None and run.executor_id != selection.executor_id:
                raise ValueError(f"{label} run executor_id must match selection")
        if self.decision is RetryDecisionKind.SAME_BACKEND:
            if not self.allowed or self.reason is not RetryDecisionReason.SAME_BACKEND:
                raise ValueError("same-backend retry decisions must be allowed")
        elif self.decision is RetryDecisionKind.BACKEND_CHANGE_ALLOWED:
            if (
                not self.allowed
                or self.reason is not RetryDecisionReason.EXPLICIT_COMPATIBILITY
            ):
                raise ValueError(
                    "allowed backend changes require explicit compatibility"
                )
        elif (
            self.decision is RetryDecisionKind.BACKEND_CHANGE_REJECTED and self.allowed
        ):
            raise ValueError("rejected backend changes must not be allowed")
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
        prior_active: bool = False,
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
        policy: FactoryPolicy,
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
        self._policy = policy
        self._policy_fingerprint = policy_fingerprint(policy)
        self._known_profiles = (
            None if configured_profiles is None else frozenset(configured_profiles)
        )
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

        provided_bindings = tuple(
            binding
            for binding in (existing_binding, active_binding, bound_binding)
            if binding is not None
        )
        if len(provided_bindings) > 1:
            raise RoutingError("provide only one existing active binding")
        if run is not None:
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
            request = DispatchRequest(
                task_id=request.task_id,
                run_id=request.run_id,
                role=role,
                attempt=request.attempt,
                admitted=True if admitted is None else admitted,
            )
            role = None
        existing = provided_bindings[0] if provided_bindings else None
        request = self._coerce_request(
            request,
            task_id=task_id,
            run_id=run_id,
            role=role,
            attempt=attempt,
            admitted=admitted,
        )
        route, reason = self._route_for(request.task_id, request.role, request.admitted)
        binding = self._binding_for(request, route, reason)
        if existing is None:
            return binding
        self._validate_existing_identity(existing, request)
        if existing.backend_key() != binding.backend_key():
            raise ExecutorSelectionError(
                "bound active run cannot change executor route"
            )
        # Preserve the first durable reason/fingerprint rather than rewriting
        # evidence when a dispatcher re-reads the same active run.
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
        supplied = (task_id, run_id, role, attempt)
        if request is not None and any(value is not None for value in supplied):
            raise RoutingError(
                "request and explicit task/run fields cannot be combined"
            )
        if isinstance(request, DispatchRequest):
            if admitted is not None and admitted != request.admitted:
                raise RoutingError("request admitted flag was specified twice")
            return request
        if isinstance(request, TaskEnvelope):
            if admitted is None:
                admitted = True
            return DispatchRequest(
                task_id=request.task_id,
                run_id=request.run_id,
                role=request.role.value,
                admitted=admitted,
            )
        if isinstance(request, RunIdentity):
            if role is None:
                raise RoutingError("a logical role is required with RunIdentity")
            return DispatchRequest(
                task_id=request.task_id,
                run_id=request.run_id,
                role=role,
                attempt=request.attempt,
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
            return DispatchRequest.model_validate(request_data)
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

    def decide_retry(
        self,
        prior_selection: ExecutorBinding,
        new_selection: ExecutorBinding,
        *,
        prior_active: bool = False,
        active: bool | None = None,
        allow_backend_change: bool = False,
        explicit_policy: bool | None = None,
    ) -> RetryDecision:
        """Compare a terminal selection with a distinct retry selection.

        This method only returns a durable decision. It never mutates either
        selection and never creates, claims, or reclaims a task.
        """

        if active is not None:
            if prior_active and active is not prior_active:
                raise RetryRoutingError("prior_active and active disagree")
            prior_active = active
        if explicit_policy is not None:
            if allow_backend_change and explicit_policy is not allow_backend_change:
                raise RetryRoutingError(
                    "allow_backend_change and explicit_policy disagree"
                )
            allow_backend_change = explicit_policy
        self._validate_retry_candidates(prior_selection, new_selection)
        prior_run = prior_selection.run
        retry_run = new_selection.run
        if prior_active:
            return RetryDecision(
                task_id=prior_selection.task_id,
                prior_run=prior_run,
                retry_run=retry_run,
                prior_selection=prior_selection,
                new_selection=new_selection,
                allowed=False,
                decision=RetryDecisionKind.BACKEND_CHANGE_REJECTED,
                reason=RetryDecisionReason.ACTIVE_RUN,
            )
        if prior_selection.backend_key() == new_selection.backend_key():
            return RetryDecision(
                task_id=prior_selection.task_id,
                prior_run=prior_run,
                retry_run=retry_run,
                prior_selection=prior_selection,
                new_selection=new_selection,
                allowed=True,
                decision=RetryDecisionKind.SAME_BACKEND,
                reason=RetryDecisionReason.SAME_BACKEND,
            )
        if self._explicit_backend_change_allowed(
            prior_selection, new_selection, allow_backend_change
        ):
            return RetryDecision(
                task_id=prior_selection.task_id,
                prior_run=prior_run,
                retry_run=retry_run,
                prior_selection=prior_selection,
                new_selection=new_selection,
                allowed=True,
                decision=RetryDecisionKind.BACKEND_CHANGE_ALLOWED,
                reason=RetryDecisionReason.EXPLICIT_COMPATIBILITY,
            )
        return RetryDecision(
            task_id=prior_selection.task_id,
            prior_run=prior_run,
            retry_run=retry_run,
            prior_selection=prior_selection,
            new_selection=new_selection,
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
        prior_active: bool = False,
        active: bool | None = None,
        allow_backend_change: bool = False,
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
            prior_active=prior_active,
            active=active,
            allow_backend_change=allow_backend_change,
            explicit_policy=explicit_policy,
        )

    evaluate_retry = decide_retry
    retry_decision = decide_retry

    def _explicit_backend_change_allowed(
        self, prior: ExecutorBinding, new: ExecutorBinding, explicitly_requested: bool
    ) -> bool:
        policy_allows = self._policy.compatibility.allow_backend_change_on_retry
        if policy_allows is False:
            return False
        if explicitly_requested and policy_allows not in {True, "explicit_policy_only"}:
            return False
        for rule in self._policy.compatibility.retry_compatibility:
            if rule.role is not None and rule.role != prior.role:
                continue
            if rule.from_executor is not prior.executor:
                continue
            if rule.to_executor is not new.executor:
                continue
            if rule.from_id is not None and rule.from_id != prior.executor_id:
                continue
            if rule.to_id is not None and rule.to_id != new.executor_id:
                continue
            return True
        return explicitly_requested and policy_allows is True

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


def policy_fingerprint(policy: FactoryPolicy) -> str:
    """Return a deterministic credential-free fingerprint of validated policy."""

    payload = json.dumps(
        policy.model_dump(mode="json"),
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
