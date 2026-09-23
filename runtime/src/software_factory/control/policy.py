"""Strict project-policy loading and executor route validation."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)
from yaml.constructor import ConstructorError

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
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


def _as_tuple_input(value: object, field_name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(
        value, Sequence
    ):
        raise ValueError(  # noqa: TRY004 - Pydantic v2 escapes TypeError from validators
            f"{field_name} must be a sequence, not a scalar or mapping"
        )
    return tuple(value)


def _json_safe_value(value: object, path: str) -> object:
    """Copy only JSON values, rejecting handles and provider objects."""

    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, nested in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} mapping keys must be strings")
            result[key] = _json_safe_value(nested, f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item, f"{path}[]") for item in value]
    raise ValueError(
        f"{path} must contain only JSON-safe scalar, array, or mapping values"
    )


def _json_safe_mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(  # noqa: TRY004 - Pydantic v2 escapes TypeError from validators
            f"{field_name} must be a JSON-safe mapping"
        )
    checked = _json_safe_value(value, field_name)
    assert isinstance(checked, dict)
    return checked


_ALLOWED_CREDENTIAL_SCHEMES = frozenset(
    {
        "env",
        "secret",
        "secrets",
        "vault",
        "aws-secretsmanager",
        "gcp-secretmanager",
        "azure-keyvault",
        "keyring",
    }
)


def _validate_credential_reference(value: str) -> str:
    if "\x00" in value or any(character.isspace() for character in value):
        raise ValueError("credential references must not contain whitespace or NUL")
    parsed = urlsplit(value)
    scheme = parsed.scheme.casefold()
    if scheme not in _ALLOWED_CREDENTIAL_SCHEMES or not re.match(
        r"^[A-Za-z][A-Za-z0-9+.-]*://", value
    ):
        raise ValueError("credential reference must use an allowlisted explicit scheme")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credential reference must not contain userinfo")
    if parsed.query or "?" in value:
        raise ValueError("credential reference must not contain a query")
    if parsed.fragment or "#" in value:
        raise ValueError("credential reference must not contain a fragment")
    if not parsed.netloc and not parsed.path:
        raise ValueError("credential reference must identify a secret-store reference")
    return value


CredentialReference: TypeAlias = Annotated[
    StrictStr,
    Field(min_length=1, max_length=1_024),
    AfterValidator(_validate_credential_reference),
]


class AgentDefinition(PolicyModel):
    framework: Literal["pydantic_ai"] = "pydantic_ai"
    prompt: StrictStr
    output_contract: Identifier
    static_prompt_token_target: int | None = Field(default=None, ge=1)
    visible_tool_limit: int | None = Field(default=None, ge=1)
    capabilities: tuple[Identifier, ...] = Field(default_factory=tuple)

    @field_validator("prompt")
    @classmethod
    def prompt_is_relative(cls, value: str) -> str:
        if not value or "\x00" in value or value.startswith(("/", "~")):
            raise ValueError("agent prompt must be a non-empty relative path")
        if any(part in {"", ".", ".."} for part in value.replace("\\", "/").split("/")):
            raise ValueError("agent prompt must not contain ambiguous path segments")
        return value

    @field_validator("capabilities", mode="before")
    @classmethod
    def capabilities_are_sequence(cls, value: object) -> tuple[object, ...]:
        return _as_tuple_input(value, "capabilities")

    @field_validator("capabilities")
    @classmethod
    def capabilities_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
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
    credential_reference: CredentialReference | None = None
    max_in_progress: int | None = Field(default=None, ge=1)
    refresh_lock: Literal["required", "optional", "disabled"] | None = None
    fallback_provider: Identifier | None = None
    models: tuple[Revision, ...] = Field(default_factory=tuple)

    @field_validator("models", mode="before")
    @classmethod
    def models_are_sequence(cls, value: object) -> tuple[object, ...]:
        return _as_tuple_input(value, "models")

    @field_validator("models")
    @classmethod
    def models_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
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


class LegacySettings(PolicyModel):
    """Typed preservation of the old ``profiles`` compatibility section."""

    orchestrator: Identifier
    planner: Identifier | None = None
    implementer: Identifier
    reviewer: Identifier | None = None
    code_reviewer: Identifier
    completion_verifier: Identifier | None = None
    integration_operator: Identifier | None = None
    qa_ui: Identifier | None = None
    release_operator: Identifier | None = None
    code_reviewer_model_default: Revision | None = None
    code_reviewer_model_routine: Revision | None = None
    implementer_vendor_family: Identifier | None = None
    code_reviewer_vendor_family: Identifier | None = None


class CompatibilityPolicy(PolicyModel):
    canary_only: bool = True
    default_executor: ExecutorKind = ExecutorKind.HERMES_PROFILE
    fallback_executors: dict[str, RoleRoute] = Field(default_factory=dict)
    allow_backend_change_on_retry: Literal["explicit_policy_only"] = (
        "explicit_policy_only"
    )
    legacy_settings: LegacySettings | None = None

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


class FactoryPolicy(PolicyModel):
    """Validated runtime policy with strict route references.

    Architecture and legacy operational sections are explicitly declared as
    opaque JSON-safe mappings. Their semantics belong to later control-plane
    components, but unknown top-level sections are never silently accepted.
    """

    version: Literal[1] = 1
    runtime: dict[str, object] = Field(default_factory=dict)
    transport: dict[str, object] = Field(default_factory=dict)
    state: dict[str, object] = Field(default_factory=dict)
    workspace: dict[str, object] = Field(default_factory=dict)
    review: dict[str, object] = Field(default_factory=dict)
    credentials: dict[str, object] = Field(default_factory=dict)
    observability: dict[str, object] = Field(default_factory=dict)
    migration_gates: dict[str, object] = Field(default_factory=dict)
    tracker: dict[str, object] = Field(default_factory=dict)
    decision_authority: dict[str, object] = Field(default_factory=dict)
    operator_bridge: dict[str, object] = Field(default_factory=dict)
    kanban: dict[str, object] = Field(default_factory=dict)
    verification: dict[str, object] = Field(default_factory=dict)
    safety: dict[str, object] = Field(default_factory=dict)
    delivery: dict[str, object] = Field(default_factory=dict)
    deployment: dict[str, object] = Field(default_factory=dict)
    notifications: dict[str, object] = Field(default_factory=dict)
    providers: dict[str, ProviderDefinition] = Field(default_factory=dict)
    agents: dict[str, AgentDefinition] = Field(default_factory=dict)
    handlers: dict[str, HandlerDefinition] = Field(default_factory=dict)
    roles: dict[str, RoleRoute] = Field(min_length=1)
    compatibility: CompatibilityPolicy = Field(default_factory=CompatibilityPolicy)

    @field_validator(
        "runtime",
        "transport",
        "state",
        "workspace",
        "review",
        "credentials",
        "observability",
        "migration_gates",
        "tracker",
        "decision_authority",
        "operator_bridge",
        "kanban",
        "verification",
        "safety",
        "delivery",
        "deployment",
        "notifications",
        mode="before",
    )
    @classmethod
    def sections_are_json_safe(cls, value: object, info) -> dict[str, object]:
        return _json_safe_mapping(value, info.field_name)

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
        handlers = set(BUILTIN_DETERMINISTIC_HANDLERS) | set(self.handlers)
        for role, route in self.roles.items():
            _validate_route_references(
                role,
                route,
                agents=self.agents,
                providers=self.providers,
                handlers=handlers,
            )
        for role, route in self.compatibility.fallback_executors.items():
            _validate_route_references(
                f"compatibility.fallback_executors.{role}",
                route,
                agents=self.agents,
                providers=self.providers,
                handlers=handlers,
            )
        _validate_provider_fallback_graph(self.providers)
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
    if route.provider is None or route.provider not in providers:
        return
    provider = providers[route.provider]
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


def _validate_provider_fallback_graph(
    providers: Mapping[str, ProviderDefinition],
) -> None:
    """Reject fallback self-links and cycles before routing can use them."""

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(provider_name: str, path: tuple[str, ...] = ()) -> None:
        if provider_name in visiting:
            cycle = " -> ".join((*path, provider_name))
            raise ValueError(f"provider fallback cycle detected: {cycle}")
        if provider_name in visited:
            return
        visiting.add(provider_name)
        fallback = providers[provider_name].fallback_provider
        if fallback is not None:
            if fallback not in providers:
                raise ValueError(
                    f"providers.{provider_name}.fallback_provider references unknown provider "
                    f"{fallback!r}"
                )
            visit(fallback, (*path, provider_name))
        visiting.remove(provider_name)
        visited.add(provider_name)

    for provider_name in providers:
        visit(provider_name)


# Compatibility names used by callers that prefer generic policy terminology.
ProjectPolicy = FactoryPolicy
Policy = FactoryPolicy
ExecutorRoute = RoleRoute
AgentSpec = AgentDefinition
ProviderSpec = ProviderDefinition


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(loader: _UniqueKeySafeLoader, node, deep: bool = False):
    if not isinstance(node, yaml.MappingNode):
        raise ConstructorError(None, None, "expected a mapping node", node.start_mark)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "mapping keys must be hashable",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(text: str) -> object:
    return yaml.load(text, Loader=_UniqueKeySafeLoader)


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
                document = _load_yaml(raw_source)
            except yaml.YAMLError as exc:
                raise PolicyError(f"policy text is not valid YAML: {exc}") from exc
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
                    document = _load_yaml(path.read_text(encoding="utf-8"))
                except OSError as exc:
                    raise PolicyError(f"cannot read policy {path}: {exc}") from exc
                except yaml.YAMLError as exc:
                    raise PolicyError(
                        f"policy {path} is not valid YAML: {exc}"
                    ) from exc
            else:
                try:
                    document = _load_yaml(raw_source)
                except yaml.YAMLError as exc:
                    raise PolicyError(
                        f"policy path does not exist and text is invalid: {source}: {exc}"
                    ) from exc
    if not isinstance(document, dict):
        raise PolicyError("project policy must be a YAML mapping")
    return document


_LEGACY_ROUTE_ROLES = (
    "orchestrator",
    "planner",
    "implementer",
    "reviewer",
    "code_reviewer",
    "completion_verifier",
    "integration_operator",
    "qa_ui",
    "release_operator",
)


def _legacy_policy(document: Mapping[str, Any]) -> dict[str, Any]:
    profiles = document.get("profiles")
    if not isinstance(profiles, Mapping):
        raise PolicyError("legacy project policy requires a profiles mapping")
    try:
        legacy_settings = LegacySettings.model_validate(profiles)
    except ValidationError as exc:
        raise PolicyError(f"invalid legacy profile settings: {exc}") from exc

    roles: dict[str, dict[str, str]] = {}
    fallbacks: dict[str, dict[str, str]] = {}
    for role in _LEGACY_ROUTE_ROLES:
        profile = getattr(legacy_settings, role)
        if profile is None:
            continue
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
        "legacy_settings": legacy_settings.model_dump(mode="python"),
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
    "CredentialReference",
    "ExecutorKind",
    "ExecutorRoute",
    "FactoryPolicy",
    "HandlerDefinition",
    "LegacySettings",
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
