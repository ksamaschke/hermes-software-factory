"""Strict project-policy loading and executor route validation."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ..api.contracts import Identifier, Revision


class PolicyError(ValueError):
    """Raised when a project policy cannot be loaded fail-closed."""


class ExecutorKind(StrEnum):
    PYDANTIC_AGENT = "pydantic_agent"
    HERMES_PROFILE = "hermes_profile"
    DETERMINISTIC = "deterministic"


class PolicyModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class AgentDefinition(PolicyModel):
    framework: Literal["pydantic_ai"] = "pydantic_ai"
    prompt: str
    output_contract: Identifier
    static_prompt_token_target: int | None = Field(default=None, ge=1)
    visible_tool_limit: int | None = Field(default=None, ge=1)
    capabilities: list[Identifier] = Field(default_factory=list)

    @field_validator("prompt")
    @classmethod
    def prompt_is_relative(cls, value: str) -> str:
        if not value or "\x00" in value or value.startswith(("/", "~")):
            raise ValueError("agent prompt must be a non-empty relative path")
        if any(part in {"", ".", ".."} for part in value.replace("\\", "/").split("/")):
            raise ValueError("agent prompt must not contain ambiguous path segments")
        return value

    @field_validator("capabilities")
    @classmethod
    def capabilities_are_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("agent capabilities must not contain duplicates")
        return values


class ProviderKind(StrEnum):
    """Supported provider families; ``custom`` keeps the route provider-neutral."""

    OPENAI_CODEX = "openai_codex"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    GOOGLE = "google"
    OLLAMA = "ollama"
    MISTRAL = "mistral"
    XAI = "xai"
    OPENROUTER = "openrouter"
    AZURE_OPENAI = "azure_openai"
    VERTEX_AI = "vertex_ai"
    BEDROCK = "bedrock"
    LOCAL = "local"
    CUSTOM = "custom"


class ProviderDefinition(PolicyModel):
    kind: ProviderKind
    credential_source: Identifier | None = None
    credential_reference: str | None = None
    max_in_progress: int | None = Field(default=None, ge=1)
    refresh_lock: Literal["required", "optional", "disabled"] | None = None
    fallback_provider: Identifier | None = None
    models: list[Revision] = Field(default_factory=list)

    @field_validator("models")
    @classmethod
    def models_are_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("provider models must not contain duplicates")
        return values


class RoleRoute(PolicyModel):
    """One logical role's explicitly selected execution backend."""

    executor: ExecutorKind
    agent: Identifier | None = None
    profile: Identifier | None = None
    provider: Identifier | None = None
    model: Revision | None = None
    handler: Identifier | None = None
    vendor_family: Identifier | None = None
    max_in_progress: int | None = Field(default=None, ge=1)
    max_runtime_seconds: int | None = Field(default=None, ge=1)
    read_only_source: bool | None = None

    @model_validator(mode="after")
    def route_shape_is_strict(self) -> RoleRoute:
        if self.executor == ExecutorKind.PYDANTIC_AGENT:
            if self.agent is None:
                raise ValueError("pydantic_agent routes require agent")
            if self.profile is not None or self.handler is not None:
                raise ValueError("pydantic_agent routes cannot set profile or handler")
        elif self.executor == ExecutorKind.HERMES_PROFILE:
            if self.profile is None:
                raise ValueError("hermes_profile routes require profile")
            if any(
                value is not None
                for value in (self.agent, self.provider, self.model, self.handler)
            ):
                raise ValueError(
                    "hermes_profile routes cannot set agent, provider, model, or handler"
                )
        elif self.executor == ExecutorKind.DETERMINISTIC:
            if self.handler is None:
                raise ValueError("deterministic routes require handler")
            if any(
                value is not None
                for value in (self.agent, self.profile, self.provider, self.model)
            ):
                raise ValueError(
                    "deterministic routes cannot set agent, profile, provider, or model"
                )
        if self.model is not None and self.provider is None:
            raise ValueError("a model route requires a provider")
        return self


class CompatibilityPolicy(PolicyModel):
    canary_only: bool = True
    default_executor: ExecutorKind = ExecutorKind.HERMES_PROFILE
    fallback_executors: dict[str, RoleRoute] = Field(default_factory=dict)
    allow_backend_change_on_retry: Literal["explicit_policy_only"] = (
        "explicit_policy_only"
    )

    @field_validator("fallback_executors")
    @classmethod
    def fallback_role_names_are_known(
        cls, values: dict[str, RoleRoute]
    ) -> dict[str, RoleRoute]:
        unknown = sorted(set(values) - ROLE_KEYS)
        if unknown:
            raise ValueError(
                f"unknown compatibility fallback role(s): {', '.join(unknown)}"
            )
        return values


class HandlerDefinition(PolicyModel):
    """Optional declaration for a project-owned deterministic handler."""

    kind: Literal["deterministic"] = "deterministic"
    entrypoint: Identifier | None = None


ROLE_KEYS = frozenset(
    {
        "orchestrator",
        "planner",
        "implementer",
        "reviewer",
        "code_reviewer",
        "completion_verifier",
        "integration_operator",
        "qa_ui",
        "release_operator",
    }
)

# These are the deterministic lifecycle handlers named by the architecture
# example. Project-owned additions must be declared under ``handlers``.
BUILTIN_DETERMINISTIC_HANDLERS = frozenset(
    {
        "completion-verifier-v1",
        "source-integration-v1",
        "release-v1",
    }
)


class FactoryPolicy(BaseModel):
    """Validated runtime policy with strict route references.

    The architecture example contains additional operational sections (state,
    workspace, review, credentials, and observability). They are intentionally
    retained as opaque data here: this issue validates routing and compatibility
    without pretending to implement the live dispatcher.
    """

    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    version: Literal[1] = 1
    runtime: dict[str, object] = Field(default_factory=dict)
    transport: dict[str, object] = Field(default_factory=dict)
    providers: dict[str, ProviderDefinition] = Field(default_factory=dict)
    agents: dict[str, AgentDefinition] = Field(default_factory=dict)
    handlers: dict[str, HandlerDefinition] = Field(default_factory=dict)
    roles: dict[str, RoleRoute]
    compatibility: CompatibilityPolicy = Field(default_factory=CompatibilityPolicy)

    @field_validator("providers", "agents", "handlers")
    @classmethod
    def registry_keys_are_non_empty(
        cls, values: dict[str, object], info
    ) -> dict[str, object]:
        for key in values:
            if not key or any(character.isspace() for character in key):
                raise ValueError(
                    f"{info.field_name} contains an invalid registry key: {key!r}"
                )
        return values

    @field_validator("roles")
    @classmethod
    def role_names_are_known(cls, values: dict[str, RoleRoute]) -> dict[str, RoleRoute]:
        unknown = sorted(set(values) - ROLE_KEYS)
        if unknown:
            raise ValueError(f"unknown role route(s): {', '.join(unknown)}")
        return values

    @model_validator(mode="after")
    def all_routes_resolve(self) -> FactoryPolicy:
        for role, route in self.roles.items():
            _validate_route_references(
                role,
                route,
                agents=self.agents,
                providers=self.providers,
                handlers=set(BUILTIN_DETERMINISTIC_HANDLERS) | set(self.handlers),
            )
        for role, route in self.compatibility.fallback_executors.items():
            _validate_route_references(
                f"compatibility.fallback_executors.{role}",
                route,
                agents=self.agents,
                providers=self.providers,
                handlers=set(BUILTIN_DETERMINISTIC_HANDLERS) | set(self.handlers),
            )
        for name, provider in self.providers.items():
            if (
                provider.fallback_provider is not None
                and provider.fallback_provider not in self.providers
            ):
                raise ValueError(
                    f"providers.{name}.fallback_provider references unknown provider "
                    f"{provider.fallback_provider!r}"
                )
        return self


def _validate_route_references(
    role: str,
    route: RoleRoute,
    *,
    agents: Mapping[str, AgentDefinition],
    providers: Mapping[str, ProviderDefinition],
    handlers: set[str],
) -> None:
    prefix = f"roles.{role}"
    if route.agent is not None and route.agent not in agents:
        raise ValueError(f"{prefix}.agent references unknown agent {route.agent!r}")
    if route.provider is not None and route.provider not in providers:
        raise ValueError(
            f"{prefix}.provider references unknown provider {route.provider!r}"
        )
    if route.handler is not None and route.handler not in handlers:
        raise ValueError(
            f"{prefix}.handler references unknown handler {route.handler!r}"
        )
    if route.model is None:
        return
    provider = providers[route.provider]  # route shape guarantees provider is present
    if provider.models and route.model not in provider.models:
        raise ValueError(
            f"{prefix}.model references an undeclared model {route.model!r} for "
            f"provider {route.provider!r}"
        )
    if ":" not in route.model:
        raise ValueError(
            f"{prefix}.model must use a provider-qualified model route such as "
            "openai-codex:model-id"
        )
    model_provider, _, model_id = route.model.partition(":")
    if not model_id:
        raise ValueError(f"{prefix}.model must include a non-empty model id")
    expected_prefix = {
        "openai_codex": "openai-codex",
        "openai": "openai",
        "anthropic": "anthropic",
        "gemini": "gemini",
        "google": "google",
        "ollama": "ollama",
    }.get(provider.kind.value)
    if expected_prefix is not None and model_provider != expected_prefix:
        raise ValueError(
            f"{prefix}.model route {route.model!r} does not belong to provider "
            f"{route.provider!r}"
        )


# Compatibility names used by callers that prefer generic policy terminology.
ProjectPolicy = FactoryPolicy
Policy = FactoryPolicy
ExecutorRoute = RoleRoute
AgentSpec = AgentDefinition
ProviderSpec = ProviderDefinition


def _read_document(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(source, Mapping):
        document = dict(source)
    else:
        raw_source = str(source)
        looks_like_yaml = isinstance(source, str) and (
            "\n" in source or source.lstrip().startswith(("{", "[", "version:"))
        )
        if looks_like_yaml:
            try:
                document = yaml.safe_load(raw_source)
            except yaml.YAMLError as exc:
                raise PolicyError("policy text is not valid YAML") from exc
        else:
            path = Path(source)
            try:
                exists = path.exists()
            except OSError as exc:
                raise PolicyError(
                    f"cannot inspect policy source {source!r}: {exc}"
                ) from exc
            if exists:
                try:
                    document = yaml.safe_load(path.read_text(encoding="utf-8"))
                except OSError as exc:
                    raise PolicyError(f"cannot read policy {path}: {exc}") from exc
                except yaml.YAMLError as exc:
                    raise PolicyError(f"policy {path} is not valid YAML") from exc
            else:
                try:
                    document = yaml.safe_load(raw_source)
                except yaml.YAMLError as exc:
                    raise PolicyError(
                        f"policy path does not exist and text is invalid: {source}"
                    ) from exc
    if not isinstance(document, dict):
        raise PolicyError("project policy must be a YAML mapping")
    return document


def _legacy_policy(document: Mapping[str, Any]) -> dict[str, Any]:
    profiles = document.get("profiles")
    if not isinstance(profiles, Mapping):
        raise PolicyError("legacy project policy requires a profiles mapping")
    required = ("orchestrator", "implementer", "code_reviewer")
    missing = [key for key in required if key not in profiles]
    if missing:
        raise PolicyError(
            "legacy project policy is missing required profile(s): "
            + ", ".join(missing)
        )

    roles: dict[str, dict[str, str]] = {}
    fallbacks: dict[str, dict[str, str]] = {}
    for role in ROLE_KEYS:
        if role not in profiles or profiles[role] is None:
            continue
        profile = profiles[role]
        if (
            not isinstance(profile, str)
            or not profile.strip()
            or any(character.isspace() for character in profile)
        ):
            raise PolicyError(f"profiles.{role} must be a non-empty profile name")
        route = {"executor": ExecutorKind.HERMES_PROFILE.value, "profile": profile}
        roles[role] = route
        fallbacks[role] = dict(route)

    mapped = dict(document)
    mapped.pop("profiles", None)
    mapped["roles"] = roles
    mapped["providers"] = {}
    mapped["agents"] = {}
    mapped["compatibility"] = {
        "canary_only": True,
        "default_executor": ExecutorKind.HERMES_PROFILE.value,
        "fallback_executors": fallbacks,
        "allow_backend_change_on_retry": "explicit_policy_only",
    }
    return mapped


def load_policy(source: str | Path | Mapping[str, Any]) -> FactoryPolicy:
    """Load runtime policy or explicitly map the historical profile schema.

    Runtime policies must contain ``roles`` and are validated as-is. Documents
    containing only the existing ``profiles`` section go through the dedicated
    compatibility mapper; they are never silently treated as runtime routes.
    """

    document = _read_document(source)
    if "roles" not in document:
        if "profiles" not in document:
            raise PolicyError("policy must define runtime roles or legacy profiles")
        document = _legacy_policy(document)
    elif "profiles" in document:
        raise PolicyError("policy cannot define both runtime roles and legacy profiles")

    try:
        return FactoryPolicy.model_validate(document)
    except ValidationError as exc:
        raise PolicyError(f"invalid project policy: {exc}") from exc


def load_project_policy(source: str | Path | Mapping[str, Any]) -> FactoryPolicy:
    """Backward-compatible name for :func:`load_policy`."""

    return load_policy(source)


__all__ = [
    "BUILTIN_DETERMINISTIC_HANDLERS",
    "AgentDefinition",
    "AgentSpec",
    "CompatibilityPolicy",
    "ExecutorKind",
    "ExecutorRoute",
    "FactoryPolicy",
    "HandlerDefinition",
    "Policy",
    "PolicyError",
    "ProjectPolicy",
    "ProviderDefinition",
    "ProviderKind",
    "ProviderSpec",
    "RoleRoute",
    "load_policy",
    "load_project_policy",
]
