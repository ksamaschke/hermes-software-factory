"""Strict project-policy loading and executor route validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping
from enum import Enum, StrEnum
from pathlib import Path
from typing import Annotated, Any, Generic, Literal, Self, TypeAlias, TypeVar, cast
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from yaml.constructor import ConstructorError

from .._safety import TraversalBudget, TraversalBudgetError
from ..api.contracts import MAX_IDENTIFIER_LENGTH, Identifier, Revision


class PolicyError(ValueError):
    """Raised when a project policy cannot be loaded fail-closed."""


_MappingKey = TypeVar("_MappingKey")
_MappingValue = TypeVar("_MappingValue")


class ImmutableMapping(
    Mapping[_MappingKey, _MappingValue], Generic[_MappingKey, _MappingValue]
):
    """A read-only mapping backed only by exact immutable entry tuples.

    This class is also an authority-bearing input container.  Its state is
    therefore validated through ``object.__getattribute__`` by the helpers
    below instead of trusting normal attribute access or a mapping proxy.
    """

    __slots__ = ("_entries",)

    def __init__(self, values: Mapping[_MappingKey, _MappingValue]) -> None:
        object.__setattr__(self, "_entries", _build_immutable_entries(values))

    def __getitem__(self, key: _MappingKey) -> _MappingValue:
        entries = _immutable_mapping_entries(self)
        if type(key) is not str:
            raise KeyError("ImmutableMapping keys must be built-in strings")
        for entry_key, value in entries:
            if entry_key == key:
                return value  # type: ignore[return-value]
        raise KeyError(key)

    def __iter__(self) -> Iterator[_MappingKey]:
        entries = _immutable_mapping_entries(self)
        return iter(tuple(entry[0] for entry in entries))  # type: ignore[return-value]

    def __len__(self) -> int:
        return len(_immutable_mapping_entries(self))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(size={len(self)})"

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")


def _validate_immutable_entries(
    entries: object,
    label: str = "immutable mapping",
    *,
    budget: TraversalBudget | None = None,
    depth: int = 0,
    keepalive: list[object] | None = None,
) -> tuple[tuple[str, object], ...]:
    """Validate one immutable mapping's complete structural representation."""

    if type(entries) is not tuple:
        raise ValueError(f"{label} entries must be an exact tuple")
    budget = budget or TraversalBudget()
    keepalive = keepalive if keepalive is not None else []
    keepalive.append(entries)
    try:
        budget.enter(
            entries,
            depth=depth,
            label=f"{label} entries",
            length=len(entries),
        )
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            if type(entry) is not tuple:
                raise ValueError(f"{label} entry {index} must be an exact tuple")
            if len(entry) != 2:
                raise ValueError(f"{label} entry {index} must contain two values")
            budget.enter(
                entry,
                depth=depth + 1,
                label=f"{label} entry {index}",
                length=2,
            )
            key, nested = entry
            if type(key) is not str:
                raise ValueError(f"{label} entry {index} key must be a built-in string")
            budget.charge_string(key, f"{label} entry {index} key")
            if key in seen:
                raise ValueError(f"{label} contains duplicate key {key!r}")
            seen.add(key)
            _validate_immutable_value(
                nested,
                budget=budget,
                depth=depth + 2,
                label=f"{label}.{key}",
                keepalive=keepalive,
            )
    except TraversalBudgetError as exc:
        raise ValueError(str(exc)) from exc
    return entries  # type: ignore[return-value]


def _immutable_mapping_entries(
    value: object,
    label: str = "immutable mapping",
    *,
    budget: TraversalBudget | None = None,
    depth: int = 0,
    keepalive: list[object] | None = None,
) -> tuple[tuple[str, object], ...]:
    """Read and validate exact immutable state without invoking value hooks."""

    if type(value) is not ImmutableMapping:
        raise ValueError(f"{label} must be an exact ImmutableMapping")
    try:
        entries = object.__getattribute__(value, "_entries")
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{label} has no initialized entries") from exc
    try:
        extra = object.__getattribute__(value, "__dict__")
    except AttributeError:
        extra = None
    if extra is not None and (type(extra) is not dict or extra):
        raise ValueError(f"{label} contains unexpected extra state")
    return _validate_immutable_entries(
        entries,
        label,
        budget=budget,
        depth=depth,
        keepalive=keepalive,
    )


def _validate_immutable_value(
    value: object,
    *,
    budget: TraversalBudget,
    depth: int,
    label: str,
    keepalive: list[object],
) -> None:
    """Recursively validate exact nested state without reading hostile values."""

    if value is None or type(value) in {bool, int, str}:
        budget.charge_scalar(value, label)
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} must contain finite JSON numbers")
        budget.charge_scalar(value, label)
        return
    if type(value) is dict:
        budget.enter(value, depth=depth, label=label, length=len(value))
        try:
            entries = tuple((key, nested) for key, nested in dict.items(value))
        except (MemoryError, RuntimeError) as exc:
            raise ValueError(f"{label} could not be safely traversed") from exc
        keepalive.append(entries)
        _validate_immutable_entries(
            entries,
            label,
            budget=budget,
            depth=depth + 1,
            keepalive=keepalive,
        )
        return
    if type(value) in {list, tuple}:
        exact_sequence = cast(list[object] | tuple[object, ...], value)
        budget.enter(
            exact_sequence,
            depth=depth,
            label=label,
            length=len(exact_sequence),
        )
        for index, nested in enumerate(exact_sequence):
            _validate_immutable_value(
                nested,
                budget=budget,
                depth=depth + 1,
                label=f"{label}[{index}]",
                keepalive=keepalive,
            )
        return
    if type(value) is ImmutableMapping:
        _immutable_mapping_entries(
            value,
            label,
            budget=budget,
            depth=depth,
            keepalive=keepalive,
        )
        return
    if type(value) in _TRUSTED_IMMUTABLE_ENUM_TYPES:
        enum_value = object.__getattribute__(value, "_value_")
        if enum_value is None or type(enum_value) in {bool, int, str}:
            budget.charge_scalar(enum_value, label)
            return
        if type(enum_value) is float and math.isfinite(enum_value):
            budget.charge_scalar(enum_value, label)
            return
        raise ValueError(f"{label} contains an unsupported enum value")
    if type(value) in _TRUSTED_IMMUTABLE_MODEL_TYPES:
        _validate_immutable_model(
            value,
            budget=budget,
            depth=depth,
            label=label,
            keepalive=keepalive,
        )
        return
    if type(value) is object:
        budget.charge_encoded(1, label)
        return
    raise ValueError(f"{label} contains an unsupported or hostile value")


def _validate_immutable_model(
    value: object,
    *,
    budget: TraversalBudget,
    depth: int,
    label: str,
    keepalive: list[object],
) -> None:
    """Validate exact trusted model state without model or serializer hooks."""

    model_type = type(value)
    if model_type not in _TRUSTED_IMMUTABLE_MODEL_TYPES:
        raise ValueError(f"{label} contains an untrusted model")
    budget.enter(value, depth=depth, label=label, length=1)
    try:
        raw_state = object.__getattribute__(value, "__dict__")
        extra = object.__getattribute__(value, "__pydantic_extra__")
        private = object.__getattribute__(value, "__pydantic_private__")
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{label} has no trusted raw model state") from exc
    if type(raw_state) is not dict:
        raise ValueError(f"{label} raw model state is not an exact dict")
    if extra is not None and (type(extra) is not dict or extra):
        raise ValueError(f"{label} contains unknown extra fields")
    if private is not None and (type(private) is not dict or private):
        raise ValueError(f"{label} contains private model state")
    budget.enter(
        raw_state,
        depth=depth + 1,
        label=f"{label}.__dict__",
        length=len(raw_state),
    )
    expected_names = tuple(model_type.model_fields)
    actual_names = tuple(dict.keys(raw_state))
    if any(type(name) is not str for name in actual_names):
        raise ValueError(f"{label} contains a non-string field name")
    if set(actual_names) != set(expected_names):
        raise ValueError(f"{label} model state is not exact")
    keepalive.append(raw_state)
    for field_name in expected_names:
        _validate_immutable_value(
            raw_state[field_name],
            budget=budget,
            depth=depth + 2,
            label=f"{label}.{field_name}",
            keepalive=keepalive,
        )


def _new_immutable_mapping(
    entries: tuple[tuple[str, object], ...],
) -> ImmutableMapping[object, object]:
    """Create an instance only after its exact entries have been frozen."""

    result = object.__new__(ImmutableMapping)
    object.__setattr__(result, "_entries", entries)
    return result


def _freeze_immutable_value(
    value: object,
    *,
    budget: TraversalBudget,
    depth: int,
    label: str,
    keepalive: list[object],
) -> object:
    """Deep-copy exact mutable containers without observing hostile ones."""

    if value is None or type(value) in {bool, int, str}:
        budget.charge_scalar(value, label)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} must contain finite JSON numbers")
        budget.charge_scalar(value, label)
        return value
    if type(value) is dict:
        length = len(value)
        budget.enter(value, depth=depth, label=label, length=length)
        entries = tuple((key, nested) for key, nested in dict.items(value))
        return _new_immutable_mapping(
            _freeze_immutable_entries(
                entries,
                budget=budget,
                depth=depth + 1,
                label=label,
                keepalive=keepalive,
            )
        )
    if type(value) in {list, tuple}:
        length = len(value)
        budget.enter(value, depth=depth, label=label, length=length)
        return tuple(
            _freeze_immutable_value(
                item,
                budget=budget,
                depth=depth + 1,
                label=f"{label}[{index}]",
                keepalive=keepalive,
            )
            for index, item in enumerate(value)
        )
    if type(value) is ImmutableMapping:
        entries = _immutable_mapping_entries(value, label)
        return _new_immutable_mapping(
            _freeze_immutable_entries(
                entries,
                budget=budget,
                depth=depth + 1,
                label=label,
                keepalive=keepalive,
            )
        )
    if type(value) in _TRUSTED_IMMUTABLE_ENUM_TYPES:
        enum_value = object.__getattribute__(value, "_value_")
        if enum_value is None or type(enum_value) in {bool, int, str}:
            budget.charge_scalar(enum_value, label)
            return value
        if type(enum_value) is float and math.isfinite(enum_value):
            budget.charge_scalar(enum_value, label)
            return value
        raise ValueError(f"{label} contains an unsupported enum value")
    if type(value) in _TRUSTED_IMMUTABLE_MODEL_TYPES:
        _validate_immutable_model(
            value,
            budget=budget,
            depth=depth,
            label=label,
            keepalive=keepalive,
        )
        return value
    if type(value) is object:
        budget.charge_encoded(1, label)
        return value
    raise ValueError(f"{label} contains an unsupported or hostile value")


def _freeze_immutable_entries(
    entries: object,
    *,
    budget: TraversalBudget,
    depth: int,
    label: str,
    keepalive: list[object],
) -> tuple[tuple[str, object], ...]:
    """Freeze exact entry tuples and recursively snapshot their values."""

    if type(entries) is not tuple:
        raise ValueError(f"{label} entries must be an exact tuple")
    exact_entries = cast(tuple[object, ...], entries)
    keepalive.append(exact_entries)
    budget.enter(
        exact_entries,
        depth=depth,
        label=f"{label} entries",
        length=len(exact_entries),
    )
    result: list[tuple[str, object]] = []
    seen: set[str] = set()
    for index, entry in enumerate(exact_entries):
        if type(entry) is not tuple:
            raise ValueError(f"{label} entry {index} must be an exact tuple")
        if len(entry) != 2:
            raise ValueError(f"{label} entry {index} must contain two values")
        budget.enter(
            entry,
            depth=depth + 1,
            label=f"{label} entry {index}",
            length=2,
        )
        key, nested = entry
        if type(key) is not str:
            raise ValueError(f"{label} entry {index} key must be a built-in string")
        budget.charge_string(key, f"{label} entry {index} key")
        if key in seen:
            raise ValueError(f"{label} contains duplicate key {key!r}")
        seen.add(key)
        result.append(
            (
                key,
                _freeze_immutable_value(
                    nested,
                    budget=budget,
                    depth=depth + 2,
                    label=f"{label}.{key}",
                    keepalive=keepalive,
                ),
            )
        )
    return tuple(result)


def _build_immutable_entries(
    values: Mapping[_MappingKey, _MappingValue],
) -> tuple[tuple[str, object], ...]:
    """Copy only exact mapping structure into immutable entry tuples."""

    if type(values) is dict:
        try:
            entries = tuple((key, nested) for key, nested in dict.items(values))
        except (MemoryError, RuntimeError) as exc:
            raise TypeError(
                "ImmutableMapping could not snapshot the exact dict"
            ) from exc
    elif type(values) is ImmutableMapping:
        entries = _immutable_mapping_entries(values)
    else:
        raise TypeError("ImmutableMapping requires an exact dict snapshot")
    try:
        keepalive: list[object] = []
        return _freeze_immutable_entries(
            entries,
            budget=TraversalBudget(),
            depth=0,
            label="immutable mapping",
            keepalive=keepalive,
        )
    except TraversalBudgetError as exc:
        raise TypeError(str(exc)) from exc


_APPROVED_MAPPING_TYPES = frozenset({dict, ImmutableMapping})


def _reject_unapproved_mapping(value: object, field_name: str) -> None:
    """Reject mapping subclasses/proxies before Pydantic can iterate them."""

    if type(value) in _APPROVED_MAPPING_TYPES:
        return
    if isinstance(value, Mapping):
        raise ValueError(  # noqa: TRY004 - Pydantic validators require ValueError
            f"{field_name} must use an approved exact mapping container"
        )


def _mapping_items(value: object):
    """Iterate only an already exact-admitted mapping."""

    if type(value) is dict:
        return dict.items(value)
    if type(value) is ImmutableMapping:
        return _immutable_mapping_entries(value)
    raise TypeError("mapping was not exact-admitted")


def _deep_freeze(
    value: object,
    *,
    budget: TraversalBudget | None = None,
    depth: int = 0,
    path: str = "policy",
) -> object:
    """Recursively freeze a bounded, already-validated policy graph."""

    budget = budget or TraversalBudget()
    if value is None or type(value) in {bool, int, str}:
        budget.charge_scalar(value, path)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain finite JSON numbers")
        budget.charge_scalar(value, path)
        return value
    if type(value) in _APPROVED_MAPPING_TYPES:
        items = _mapping_items(value)
        length = len(items)
        budget.enter(value, depth=depth, label=path, length=length)
        result: dict[str, object] = {}
        for key, nested in items:
            if type(key) is not str:
                raise ValueError(f"{path} mapping keys must be built-in strings")
            budget.charge_string(key, f"{path}.{key}")
            if key in result:
                raise ValueError(f"{path} contains duplicate mapping key {key!r}")
            result[key] = _deep_freeze(
                nested, budget=budget, depth=depth + 1, path=f"{path}.{key}"
            )
        return ImmutableMapping(result)
    if type(value) in {list, tuple}:
        length = len(value)  # exact type is checked before calling len
        budget.enter(value, depth=depth, label=path, length=length)
        return tuple(
            _deep_freeze(item, budget=budget, depth=depth + 1, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if isinstance(value, BaseModel):
        # Nested policy models have already passed their typed validators.  We
        # still account for their identity so aliases cannot bypass the graph
        # budget during the surrounding mapping freeze.
        if type(value).__module__.split(".")[0] != "software_factory":
            raise ValueError(f"{path} contains an untrusted model")
        budget.enter(value, depth=depth, label=path, length=1)
        return value
    _reject_unapproved_mapping(value, path)
    raise ValueError(f"{path} contains an unsupported policy value")


def _approved_mapping_copy(value: object, field_name: str) -> dict[str, object]:
    """Materialize only exact mappings with exact built-in string keys."""

    _reject_unapproved_mapping(value, field_name)
    if type(value) not in _APPROVED_MAPPING_TYPES:
        raise ValueError(f"{field_name} must use an approved exact mapping container")
    result: dict[str, object] = {}
    for key, nested in _mapping_items(value):
        if type(key) is not str:
            raise ValueError(f"{field_name} mapping keys must be built-in strings")
        if key in result:
            raise ValueError(f"{field_name} contains duplicate key {key!r}")
        result[key] = nested
    return result


def _policy_dump_value(value: object, mode: str) -> object:
    """Turn immutable policy values back into ordinary Pydantic output values."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode=mode)
    if type(value) in _APPROVED_MAPPING_TYPES:
        return {
            key: _policy_dump_value(nested, mode)
            for key, nested in _mapping_items(value)
        }
    if type(value) in {list, tuple}:
        values = [_policy_dump_value(item, mode) for item in value]
        return values if mode == "json" else tuple(values)
    return value


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

    def validated_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        """Return a defensive copy after re-running the complete policy model."""

        del deep  # validation creates a fresh object graph regardless of this hint
        values = self.model_dump(mode="python")
        if update is not None:
            values.update(_approved_mapping_copy(update, "policy model update"))
        return type(self).model_validate(values)

    def model_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        """Return only a validated copy; never bypass policy validators."""

        return self.validated_copy(update=update, deep=deep)


def _as_tuple_input(value: object, field_name: str) -> tuple[object, ...]:
    if type(value) not in {list, tuple}:
        raise ValueError(f"{field_name} must be an exact list or tuple")
    return tuple(value)


def _json_safe_value(
    value: object,
    path: str,
    *,
    budget: TraversalBudget | None = None,
    depth: int = 0,
) -> object:
    """Copy only JSON values with one aggregate graph budget."""

    budget = budget or TraversalBudget()
    if value is None or type(value) in {bool, int, str}:
        budget.charge_scalar(value, path)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain finite JSON numbers")
        budget.charge_scalar(value, path)
        return value
    if isinstance(value, Enum):
        if type(value) not in {ExecutorKind, ProviderKind}:
            raise ValueError(f"{path} contains an untrusted enum")
        enum_value = value.value
        if type(enum_value) not in {bool, int, float, str}:
            raise ValueError(f"{path} contains an unsupported enum value")
        budget.charge_scalar(enum_value, path)
        return enum_value
    if type(value) in _APPROVED_MAPPING_TYPES:
        items = _mapping_items(value)
        length = len(items)
        budget.enter(value, depth=depth, label=path, length=length)
        result: dict[str, object] = {}
        for key, nested in items:
            if type(key) is not str:
                raise ValueError(f"{path} mapping keys must be strings")
            budget.charge_string(key, f"{path}.{key}")
            if key in result:
                raise ValueError(f"{path} contains duplicate mapping key {key!r}")
            result[key] = _json_safe_value(
                nested, f"{path}.{key}", budget=budget, depth=depth + 1
            )
        return result
    if type(value) in {list, tuple}:
        length = len(value)  # exact type is checked before calling len
        budget.enter(value, depth=depth, label=path, length=length)
        return [
            _json_safe_value(item, f"{path}[{index}]", budget=budget, depth=depth + 1)
            for index, item in enumerate(value)
        ]
    _reject_unapproved_mapping(value, path)
    raise ValueError(
        f"{path} must contain only JSON-safe scalar, array, or mapping values"
    )


def _json_safe_mapping(value: object, field_name: str) -> dict[str, object]:
    _reject_unapproved_mapping(value, field_name)
    if type(value) not in _APPROVED_MAPPING_TYPES:
        raise ValueError(f"{field_name} must be a JSON-safe mapping")
    checked = _json_safe_value(value, field_name, budget=TraversalBudget())
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

SUPPORTED_OUTCOME_CONTRACTS = frozenset(
    {
        "PlanOutcome",
        "ImplementationOutcome",
        "ReviewOutcome",
        "BlockedOutcome",
        "FailedOutcome",
    }
)
OutputContractName: TypeAlias = Literal[
    "PlanOutcome",
    "ImplementationOutcome",
    "ReviewOutcome",
    "BlockedOutcome",
    "FailedOutcome",
]


class AgentDefinition(PolicyModel):
    framework: Literal["pydantic_ai"] = "pydantic_ai"
    prompt: StrictStr
    output_contract: OutputContractName
    static_prompt_token_target: StrictInt | None = Field(default=None, ge=1)
    visible_tool_limit: StrictInt | None = Field(default=None, ge=1)
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


_PROVIDER_MODEL_PREFIXES: dict[ProviderKind, str | None] = {
    ProviderKind.OPENAI_CODEX: "openai-codex",
    ProviderKind.OPENAI: "openai",
    ProviderKind.ANTHROPIC: "anthropic",
    ProviderKind.GEMINI: "gemini",
    ProviderKind.GOOGLE: "google",
    ProviderKind.OLLAMA: "ollama",
    ProviderKind.MISTRAL: "mistral",
    ProviderKind.XAI: "xai",
    ProviderKind.OPENROUTER: "openrouter",
    ProviderKind.AZURE_OPENAI: "azure-openai",
    ProviderKind.VERTEX_AI: "vertex-ai",
    ProviderKind.BEDROCK: "bedrock",
    ProviderKind.LOCAL: "local",
    ProviderKind.CUSTOM: None,
}


def _validate_model_family(kind: ProviderKind, model: str, field_name: str) -> str:
    if ":" not in model:
        raise ValueError(
            f"{field_name} must use a provider-qualified model route such as "
            "openai-codex:model-id"
        )
    model_provider, _, model_id = model.partition(":")
    if not model_provider:
        raise ValueError(f"{field_name} must include a non-empty provider prefix")
    if not model_id:
        raise ValueError(f"{field_name} must include a non-empty model id")
    expected_prefix = _PROVIDER_MODEL_PREFIXES[kind]
    if expected_prefix is not None and model_provider != expected_prefix:
        raise ValueError(
            f"{field_name} route {model!r} does not belong to provider kind {kind.value!r}"
        )
    return model


_GLOB_SYNTAX = frozenset("*?[]{}\\()!+@^~")


def _validate_exact_route_identifier(value: str, field_name: str) -> str:
    """Require a literal route identity, never a glob or escaped pattern."""

    if type(value) is not str:
        raise ValueError(f"{field_name} must be a built-in identifier string")
    if not value or len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"{field_name} must be a non-empty identifier no longer than "
            f"{MAX_IDENTIFIER_LENGTH} characters"
        )
    if "\x00" in value or any(character.isspace() for character in value):
        raise ValueError(f"{field_name} must not contain whitespace or NUL")
    if any(character in _GLOB_SYNTAX for character in value):
        raise ValueError(f"{field_name} must be an exact literal identifier")
    return value


class ProviderDefinition(PolicyModel):
    kind: ProviderKind
    credential_source: Identifier | None = None
    credential_reference: CredentialReference | None = None
    max_in_progress: StrictInt | None = Field(default=None, ge=1)
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
        for model in values:
            if type(model) is not str:
                raise ValueError("provider models must contain built-in strings")
            _validate_exact_route_identifier(model, "provider model")
        if len(values) != len(set(values)):
            raise ValueError("provider models must not contain duplicates")
        return values

    @model_validator(mode="after")
    def models_belong_to_provider(self) -> ProviderDefinition:
        for model in self.models:
            _validate_model_family(self.kind, model, "provider.models")
        return self


class RoleRoute(PolicyModel):
    """One logical role's explicitly selected execution backend."""

    executor: ExecutorKind
    agent: Identifier | None = None
    profile: Identifier | None = None
    provider: Identifier | None = None
    model: Revision | None = None
    handler: Identifier | None = None
    vendor_family: Identifier | None = None
    max_in_progress: StrictInt | None = Field(default=None, ge=1)
    max_runtime_seconds: StrictInt | None = Field(default=None, ge=1)
    read_only_source: StrictBool | None = None

    @field_validator(
        "agent",
        "profile",
        "provider",
        "model",
        "handler",
        "vendor_family",
    )
    @classmethod
    def route_identities_are_exact(cls, value: str | None, info) -> str | None:
        return (
            None
            if value is None
            else _validate_exact_route_identifier(value, f"route {info.field_name}")
        )

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


class CanaryRule(PolicyModel):
    """One exact task/role pair admitted to a Pydantic route."""

    task_id: Identifier
    role: Identifier

    @field_validator("task_id")
    @classmethod
    def task_id_is_exact(cls, value: str) -> str:
        return _validate_exact_route_identifier(value, "canary task_id")

    @field_validator("role")
    @classmethod
    def role_is_known(cls, value: str) -> str:
        _validate_exact_route_identifier(value, "canary role")
        if value not in ROLE_KEYS:
            raise ValueError(f"unknown canary role: {value!r}")
        return value


class RetryCompatibilityRule(PolicyModel):
    """One exact source/target backend selection allowed on a retry.

    Every field is part of the authority decision.  Optional values are still
    required in the input so that ``None`` means the concrete route has no
    value, never that the field is a wildcard.
    """

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
    rule_identity: Identifier | None = Field(default=None, alias="rule_id")

    @model_validator(mode="before")
    @classmethod
    def normalize_executor_aliases(cls, value: object) -> object:
        if type(value) not in _APPROVED_MAPPING_TYPES:
            _reject_unapproved_mapping(value, "retry compatibility rule")
            return value
        data = _approved_mapping_copy(value, "retry compatibility rule")
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
        if "rule_identity" in data:
            if "rule_id" in data:
                raise ValueError(
                    "retry compatibility rule defines both 'rule_identity' and 'rule_id'"
                )
            data["rule_id"] = data.pop("rule_identity")
        for alias, canonical in aliases.items():
            if alias not in data:
                continue
            if canonical in data:
                raise ValueError(
                    f"retry compatibility rule defines both {alias!r} and {canonical!r}"
                )
            data[canonical] = data.pop(alias)
        return data

    @field_validator("role")
    @classmethod
    def role_is_known(cls, value: str) -> str:
        _validate_exact_route_identifier(value, "unknown retry compatibility role")
        if value not in ROLE_KEYS:
            raise ValueError(f"unknown retry compatibility role: {value!r}")
        return value

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
    def backend_identities_are_exact(cls, value: str | None, info) -> str | None:
        return (
            None
            if value is None
            else _validate_exact_route_identifier(value, f"retry {info.field_name}")
        )

    @field_validator("rule_identity")
    @classmethod
    def rule_identity_is_exact(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _validate_exact_route_identifier(value, "retry rule_id")
        )

    @property
    def rule_id(self) -> str:
        """Return the stable identity of this complete literal rule."""

        return self.rule_identity or retry_compatibility_rule_identity(self)


def retry_compatibility_rule_identity(rule: RetryCompatibilityRule) -> str:
    """Hash only the exact authorization fields of one retry rule."""

    return retry_compatibility_identity(
        from_executor=rule.from_executor,
        to_executor=rule.to_executor,
        from_id=rule.from_id,
        to_id=rule.to_id,
        from_provider=rule.from_provider,
        to_provider=rule.to_provider,
        from_model=rule.from_model,
        to_model=rule.to_model,
        from_vendor_family=rule.from_vendor_family,
        to_vendor_family=rule.to_vendor_family,
        from_read_only_source=rule.from_read_only_source,
        to_read_only_source=rule.to_read_only_source,
        role=rule.role,
    )


def retry_compatibility_identity(
    *,
    from_executor: ExecutorKind,
    to_executor: ExecutorKind,
    from_id: str,
    to_id: str,
    from_provider: str | None,
    to_provider: str | None,
    from_model: str | None,
    to_model: str | None,
    from_vendor_family: str | None,
    to_vendor_family: str | None,
    from_read_only_source: bool | None,
    to_read_only_source: bool | None,
    role: str,
) -> str:
    values = (
        from_executor.value,
        to_executor.value,
        from_id,
        to_id,
        from_provider,
        to_provider,
        from_model,
        to_model,
        from_vendor_family,
        to_vendor_family,
        from_read_only_source,
        to_read_only_source,
        role,
    )
    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    canary_only: StrictBool = True
    default_executor: ExecutorKind = ExecutorKind.HERMES_PROFILE
    fallback_executors: Mapping[str, RoleRoute] = Field(
        default_factory=dict, validate_default=True
    )
    allow_backend_change_on_retry: Literal["explicit_policy_only"] | StrictBool = (
        "explicit_policy_only"
    )
    canary_rules: tuple[CanaryRule, ...] = Field(default_factory=tuple)
    retry_compatibility: tuple[RetryCompatibilityRule, ...] = Field(
        default_factory=tuple
    )

    @field_validator("fallback_executors", mode="before")
    @classmethod
    def fallback_mapping_is_approved(cls, value: object) -> dict[str, object]:
        return _approved_mapping_copy(value, "fallback_executors")

    legacy_settings: LegacySettings | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_routing_aliases(cls, value: object) -> object:
        """Accept descriptive aliases, but reject ambiguous policy sources."""

        if type(value) not in _APPROVED_MAPPING_TYPES:
            _reject_unapproved_mapping(value, "compatibility policy")
            return value
        data = _approved_mapping_copy(value, "compatibility policy")

        canary_aliases = (
            "canary",
            "canary_tasks",
            "canary_routes",
            "canary_allowlist",
        )
        present_canary = [
            name for name in ("canary_rules", *canary_aliases) if name in data
        ]
        if len(present_canary) > 1:
            raise ValueError(
                "compatibility canary policy must use exactly one of "
                "canary_rules, canary, canary_tasks, or canary_routes"
            )
        if present_canary and any(
            name in data for name in ("canary_task_ids", "canary_roles")
        ):
            raise ValueError("canary rule forms must not be combined")
        if present_canary and present_canary[0] != "canary_rules":
            data["canary_rules"] = data.pop(present_canary[0])
        if type(data.get("canary_rules")) in _APPROVED_MAPPING_TYPES:
            canary_mapping = _approved_mapping_copy(
                data["canary_rules"], "compatibility canary_rules"
            )
            if "admitted" in canary_mapping and len(canary_mapping) == 1:
                data["canary_rules"] = canary_mapping["admitted"]

        for alias, canonical in (
            ("admitted_task_ids", "canary_task_ids"),
            ("admitted_roles", "canary_roles"),
        ):
            if alias not in data:
                continue
            if canonical in data:
                raise ValueError(
                    f"canary policy defines both {alias!r} and {canonical!r}"
                )
            data[canonical] = data.pop(alias)
        has_task_ids = "canary_task_ids" in data
        has_roles = "canary_roles" in data
        if has_task_ids != has_roles:
            raise ValueError("canary_task_ids and canary_roles must be paired")
        if has_task_ids:
            if "canary_rules" in data:
                raise ValueError("canary rule forms must not be combined")
            task_ids = _as_tuple_input(data.pop("canary_task_ids"), "canary_task_ids")
            roles = _as_tuple_input(data.pop("canary_roles"), "canary_roles")
            if len(task_ids) != len(roles):
                if len(task_ids) == 1:
                    task_ids = task_ids * len(roles)
                elif len(roles) == 1:
                    roles = roles * len(task_ids)
                else:
                    raise ValueError(
                        "canary_task_ids and canary_roles must have equal lengths "
                        "or one must contain exactly one value"
                    )
            data["canary_rules"] = tuple(
                {"task_id": task_id, "role": role}
                for task_id, role in zip(task_ids, roles, strict=True)
            )

        retry_aliases = (
            "retry_backend_compatibility",
            "backend_change_compatibility",
            "backend_change_rules",
            "retry_backend_rules",
        )
        present_retry = [
            name for name in ("retry_compatibility", *retry_aliases) if name in data
        ]
        if len(present_retry) > 1:
            raise ValueError(
                "compatibility retry policy must use exactly one retry rule field"
            )
        if present_retry and present_retry[0] != "retry_compatibility":
            data["retry_compatibility"] = data.pop(present_retry[0])
        return data

    @field_validator("fallback_executors")
    @classmethod
    def fallback_role_names_are_known(
        cls, values: Mapping[str, RoleRoute]
    ) -> Mapping[str, RoleRoute]:
        unknown = sorted(set(values) - ROLE_KEYS)
        if unknown:
            raise ValueError(
                f"unknown compatibility fallback role(s): {', '.join(unknown)}"
            )
        return values

    @field_validator("fallback_executors")
    @classmethod
    def fallback_routes_are_hermes_profiles(
        cls, values: Mapping[str, RoleRoute]
    ) -> Mapping[str, RoleRoute]:
        invalid = sorted(
            role
            for role, route in values.items()
            if route.executor is not ExecutorKind.HERMES_PROFILE
        )
        if invalid:
            raise ValueError(
                "compatibility fallback routes must use hermes_profile: "
                + ", ".join(invalid)
            )
        return values

    @field_validator("canary_rules", mode="before")
    @classmethod
    def canary_rules_are_explicit(
        cls, value: object
    ) -> tuple[Mapping[str, object], ...]:
        if value is None:
            return ()
        if type(value) in _APPROVED_MAPPING_TYPES:
            data = _approved_mapping_copy(value, "canary_rules")
            if "task_id" in data or "role" in data:
                raw_rules: tuple[object, ...] = (data,)
            else:
                raw_rules = tuple(
                    {"task_id": task_id, "role": role} for task_id, role in data.items()
                )
        else:
            raw_rules = _as_tuple_input(value, "canary_rules")

        expanded: list[Mapping[str, object]] = []
        for raw_rule in raw_rules:
            data = _approved_mapping_copy(raw_rule, "canary rule")
            if "task_ids" in data or "roles" in data:
                if "task_id" in data or "role" in data:
                    raise ValueError(
                        "canary rules must use singular or plural fields, not both"
                    )
                task_ids = _as_tuple_input(data.get("task_ids", ()), "task_ids")
                roles = _as_tuple_input(data.get("roles", ()), "roles")
                if not task_ids or not roles:
                    raise ValueError("plural canary rules require task_ids and roles")
                for task_id in task_ids:
                    for role in roles:
                        expanded.append({"task_id": task_id, "role": role})
                continue
            expanded.append(data)
        return tuple(expanded)

    @field_validator("retry_compatibility", mode="before")
    @classmethod
    def retry_rules_are_a_sequence(cls, value: object) -> tuple[object, ...]:
        if value is None:
            return ()
        if type(value) in _APPROVED_MAPPING_TYPES:
            return (_approved_mapping_copy(value, "retry_compatibility"),)
        return _as_tuple_input(value, "retry_compatibility")

    @model_validator(mode="after")
    def routing_rules_are_unambiguous(self) -> CompatibilityPolicy:
        seen_task_ids: set[str] = set()
        for rule in self.canary_rules:
            if rule.task_id in seen_task_ids:
                raise ValueError(
                    f"overlapping canary rule for exact task_id {rule.task_id!r}"
                )
            seen_task_ids.add(rule.task_id)

        seen_retry_rules: set[tuple[object, ...]] = set()
        seen_retry_rule_ids: set[str] = set()
        for rule in self.retry_compatibility:
            key = (
                rule.from_executor,
                rule.to_executor,
                rule.from_id,
                rule.to_id,
                rule.from_provider,
                rule.to_provider,
                rule.from_model,
                rule.to_model,
                rule.from_vendor_family,
                rule.to_vendor_family,
                rule.from_read_only_source,
                rule.to_read_only_source,
                rule.role,
            )
            if key in seen_retry_rules:
                raise ValueError("duplicate retry compatibility rule")
            seen_retry_rules.add(key)
            if rule.rule_id in seen_retry_rule_ids:
                raise ValueError("duplicate retry compatibility rule identity")
            seen_retry_rule_ids.add(rule.rule_id)
        return self

    @model_validator(mode="after")
    def mapping_fields_are_immutable(self) -> CompatibilityPolicy:
        object.__setattr__(
            self,
            "fallback_executors",
            _deep_freeze(
                self.fallback_executors,
                budget=TraversalBudget(),
                path="compatibility.fallback_executors",
            ),
        )
        return self

    @field_serializer("fallback_executors")
    def serialize_fallback_executors(self, value: Mapping[str, RoleRoute], info):
        return _policy_dump_value(value, info.mode)

    @property
    def canary(self) -> tuple[CanaryRule, ...]:
        """Compatibility alias for callers using the short policy name."""

        return self.canary_rules

    @property
    def canary_task_ids(self) -> tuple[str, ...]:
        return tuple(rule.task_id for rule in self.canary_rules)

    @property
    def canary_roles(self) -> tuple[str, ...]:
        return tuple(rule.role for rule in self.canary_rules)

    @property
    def retry_backend_compatibility(self) -> tuple[RetryCompatibilityRule, ...]:
        return self.retry_compatibility


class FactoryPolicy(PolicyModel):
    """Validated runtime policy with strict route references.

    Architecture and legacy operational sections are explicitly declared as
    opaque JSON-safe mappings. Their semantics belong to later control-plane
    components, but unknown top-level sections are never silently accepted.
    """

    version: StrictInt = 1
    runtime: Mapping[str, object] = Field(default_factory=dict, validate_default=True)
    transport: Mapping[str, object] = Field(default_factory=dict, validate_default=True)
    state: Mapping[str, object] = Field(default_factory=dict, validate_default=True)
    workspace: Mapping[str, object] = Field(default_factory=dict, validate_default=True)
    review: Mapping[str, object] = Field(default_factory=dict, validate_default=True)
    credentials: Mapping[str, object] = Field(
        default_factory=dict, validate_default=True
    )
    observability: Mapping[str, object] = Field(
        default_factory=dict, validate_default=True
    )
    migration_gates: Mapping[str, object] = Field(
        default_factory=dict, validate_default=True
    )
    tracker: Mapping[str, object] = Field(default_factory=dict, validate_default=True)
    decision_authority: Mapping[str, object] = Field(
        default_factory=dict, validate_default=True
    )
    operator_bridge: Mapping[str, object] = Field(
        default_factory=dict, validate_default=True
    )
    kanban: Mapping[str, object] = Field(default_factory=dict, validate_default=True)
    verification: Mapping[str, object] = Field(
        default_factory=dict, validate_default=True
    )
    safety: Mapping[str, object] = Field(default_factory=dict, validate_default=True)
    delivery: Mapping[str, object] = Field(default_factory=dict, validate_default=True)
    deployment: Mapping[str, object] = Field(
        default_factory=dict, validate_default=True
    )
    notifications: Mapping[str, object] = Field(
        default_factory=dict, validate_default=True
    )
    providers: Mapping[str, ProviderDefinition] = Field(
        default_factory=dict, validate_default=True
    )
    agents: Mapping[str, AgentDefinition] = Field(
        default_factory=dict, validate_default=True
    )
    handlers: Mapping[str, HandlerDefinition] = Field(
        default_factory=dict, validate_default=True
    )
    roles: Mapping[str, RoleRoute] = Field(min_length=1)
    compatibility: CompatibilityPolicy = Field(default_factory=CompatibilityPolicy)

    @model_validator(mode="before")
    @classmethod
    def canonicalize_policy_graph(cls, value: object) -> object:
        """Reject untrusted maps and bound the complete raw policy graph."""

        if type(value) not in _APPROVED_MAPPING_TYPES:
            _reject_unapproved_mapping(value, "project policy")
            return value
        return _json_safe_mapping(value, "project policy")

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

    @field_validator("version")
    @classmethod
    def version_is_supported(cls, value: int) -> int:
        if value != 1:
            raise ValueError("policy version must be 1")
        return value

    @field_validator("providers", "agents", "handlers")
    @classmethod
    def registries_are_approved_mappings(
        cls, values: object, info
    ) -> dict[str, object]:
        return _approved_mapping_copy(values, info.field_name)

    @field_validator("providers", "agents", "handlers")
    @classmethod
    def registry_keys_are_non_empty(
        cls, values: Mapping[str, object], info
    ) -> Mapping[str, object]:
        for key in values:
            if not key or any(character.isspace() for character in key):
                raise ValueError(
                    f"{info.field_name} contains an invalid registry key: {key!r}"
                )
            _validate_exact_route_identifier(key, f"{info.field_name} registry key")
        return values

    @field_validator("roles", mode="before")
    @classmethod
    def roles_are_approved_mapping(cls, values: object) -> dict[str, object]:
        return _approved_mapping_copy(values, "roles")

    @field_validator("roles")
    @classmethod
    def role_names_are_known(
        cls, values: Mapping[str, RoleRoute]
    ) -> Mapping[str, RoleRoute]:
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
        if self.compatibility.canary_rules and not self.compatibility.canary_only:
            raise ValueError("canary rules require compatibility.canary_only=true")
        for rule in self.compatibility.canary_rules:
            if rule.role not in self.roles:
                raise ValueError(f"canary rule references unknown role {rule.role!r}")
            if self.roles[rule.role].executor is not ExecutorKind.PYDANTIC_AGENT:
                raise ValueError(
                    f"canary rule role {rule.role!r} does not select a pydantic_agent route"
                )
        _validate_provider_fallback_graph(self.providers)
        return self

    @model_validator(mode="after")
    def mapping_fields_are_immutable(self) -> FactoryPolicy:
        budget = TraversalBudget()
        for field_name in (
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
            "providers",
            "agents",
            "handlers",
            "roles",
        ):
            object.__setattr__(
                self,
                field_name,
                _deep_freeze(
                    getattr(self, field_name),
                    budget=budget,
                    path=f"policy.{field_name}",
                ),
            )
        object.__setattr__(
            self.compatibility,
            "fallback_executors",
            _deep_freeze(
                self.compatibility.fallback_executors,
                budget=budget,
                path="policy.compatibility.fallback_executors",
            ),
        )
        return self

    @field_serializer(
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
        "providers",
        "agents",
        "handlers",
        "roles",
    )
    def serialize_mapping_fields(self, value: Mapping[str, object], info):
        return _policy_dump_value(value, info.mode)


_TRUSTED_IMMUTABLE_MODEL_TYPES = frozenset(
    {
        AgentDefinition,
        CanaryRule,
        CompatibilityPolicy,
        FactoryPolicy,
        HandlerDefinition,
        LegacySettings,
        ProviderDefinition,
        RetryCompatibilityRule,
        RoleRoute,
    }
)
_TRUSTED_IMMUTABLE_ENUM_TYPES = frozenset({ExecutorKind, ProviderKind})


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
    if route.agent is not None:
        output_contract = agents[route.agent].output_contract
        if output_contract not in SUPPORTED_OUTCOME_CONTRACTS:
            raise ValueError(
                f"{prefix}.agent references unsupported output contract "
                f"{output_contract!r}"
            )
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
    _validate_model_family(provider.kind, route.model, f"{prefix}.model")


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


class _DuplicateJSONKey(ValueError):
    """Raised before a JSON object can silently overwrite a member."""


def _construct_unique_json_mapping(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(f"found duplicate key {key!r}")
        result[key] = value
    return result


def _load_json(text: str) -> object:
    return json.loads(text, object_pairs_hook=_construct_unique_json_mapping)


def _load_text_document(text: str) -> object:
    if type(text) is not str:
        raise PolicyError("policy text must be a built-in string")
    try:
        TraversalBudget().charge_string(text, "policy text")
    except TraversalBudgetError as exc:
        raise PolicyError(str(exc)) from exc
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return _load_json(text)
        except _DuplicateJSONKey:
            raise
        except json.JSONDecodeError:
            pass
    return _load_yaml(text)


def _read_document(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if type(source) in _APPROVED_MAPPING_TYPES:
        try:
            document = _json_safe_mapping(source, "project policy")
        except ValueError as exc:
            raise PolicyError(str(exc)) from exc
    elif isinstance(source, Mapping):
        raise PolicyError("project policy mapping must use an approved container")
    else:
        if isinstance(source, str):
            if type(source) is not str:
                raise PolicyError("policy text must be a built-in string")
            raw_source = source
            looks_like_yaml = "\n" in source or source.lstrip().startswith(
                ("{", "[", "version:")
            )
        else:
            raw_source = str(source)
            looks_like_yaml = False
        if looks_like_yaml:
            try:
                document = _load_text_document(raw_source)
            except _DuplicateJSONKey as exc:
                raise PolicyError(
                    f"policy JSON contains a duplicate key: {exc}"
                ) from exc
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
                    document = _load_text_document(path.read_text(encoding="utf-8"))
                except OSError as exc:
                    raise PolicyError(f"cannot read policy {path}: {exc}") from exc
                except _DuplicateJSONKey as exc:
                    raise PolicyError(
                        f"policy {path} JSON contains a duplicate key: {exc}"
                    ) from exc
                except yaml.YAMLError as exc:
                    raise PolicyError(
                        f"policy {path} is not valid YAML: {exc}"
                    ) from exc
            else:
                try:
                    document = _load_text_document(raw_source)
                except _DuplicateJSONKey as exc:
                    raise PolicyError(
                        f"policy JSON contains a duplicate key: {exc}"
                    ) from exc
                except yaml.YAMLError as exc:
                    raise PolicyError(
                        f"policy path does not exist and text is invalid: {source}: {exc}"
                    ) from exc
    if type(document) not in _APPROVED_MAPPING_TYPES:
        raise PolicyError("project policy must be a YAML mapping")
    try:
        return _json_safe_mapping(document, "project policy")
    except ValueError as exc:
        raise PolicyError(str(exc)) from exc


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

    try:
        document = _read_document(source)
        if "roles" not in document:
            if "profiles" not in document:
                raise PolicyError("policy must define runtime roles or legacy profiles")
            document = _legacy_policy(document)
        elif "profiles" in document:
            raise PolicyError(
                "policy cannot define both runtime roles and legacy profiles"
            )
        return FactoryPolicy.model_validate(document)
    except (RecursionError, MemoryError) as exc:
        raise PolicyError("policy exceeds the bounded safety limits") from exc
    except ValidationError as exc:
        raise PolicyError(f"invalid project policy: {exc}") from exc


def load_project_policy(source: str | Path | Mapping[str, Any]) -> FactoryPolicy:
    """Backward-compatible name for :func:`load_policy`."""

    return load_policy(source)


__all__ = [
    "BUILTIN_DETERMINISTIC_HANDLERS",
    "SUPPORTED_OUTCOME_CONTRACTS",
    "AgentDefinition",
    "AgentSpec",
    "CanaryRule",
    "CompatibilityPolicy",
    "CredentialReference",
    "ExecutorKind",
    "ExecutorRoute",
    "FactoryPolicy",
    "HandlerDefinition",
    "ImmutableMapping",
    "LegacySettings",
    "OutputContractName",
    "Policy",
    "PolicyError",
    "ProjectPolicy",
    "ProviderDefinition",
    "ProviderKind",
    "ProviderSpec",
    "RetryCompatibilityRule",
    "RoleRoute",
    "load_policy",
    "load_project_policy",
]
