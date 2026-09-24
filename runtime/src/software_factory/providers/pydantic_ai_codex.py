"""Explicit PydanticAI 2.48.0 Codex integration.

The runtime keeps ``pydantic-ai`` optional.  When this module is used, it
fail-closes unless the installed distribution is exactly the architecture-pinned
2.48.0 release and its private refresh seam still has the audited signature.
The adapter replaces PydanticAI's separate load → network refresh → save path
with one call to the application source's locked ``refresh`` transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import importlib.metadata
import inspect
import textwrap
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeAlias, cast

from .openai_codex import (
    _MAX_REFRESH_TIMEOUT,
    CodexCredentialError,
    CredentialRefreshError,
    OpenAICodexCredentialSource,
    ProviderCompletion,
    ProviderGeneration,
    ProviderOperationError,
    ProviderPressure,
    _await_task_drained,
    _is_credential_storage_error,
    _validate_duration,
)

__all__ = (
    "PINNED_PYDANTIC_AI_VERSION",
    "PydanticAIIntegrationError",
    "create_pydantic_ai_codex_provider",
    "get_pydantic_ai_codex_provider_class",
)

PINNED_PYDANTIC_AI_VERSION = "2.48.0"

# These are reviewed source/artifact-semantic guards for the private
# PydanticAI seam.  A version upgrade is blocked until this manifest and the
# exact module artifact are reviewed together.  The guard intentionally uses
# source, identity, and constants rather than CPython bytecode so it is stable
# across the declared Python 3.11+ minor versions.
_REVIEWED_MODULE_SOURCE_SHA256 = (
    "bff0d91fbcf9dc0334c9d2981df56dbcc2e3602c1baecef37d4001a0f769960c"
)
_REVIEWED_DEPENDENCY_MODULES = {
    "pydantic_ai.providers._openai_compatible": (
        "2a65e3e3e3ccd5352de327d458f40f393805532f0c4117e6b10141d827ae2957"
    ),
    "pydantic_ai._http": (
        "e2aedeb774f502826a7cf0d7ace2249001ea525b03732a5e89a835f317408fa1"
    ),
}
_REVIEWED_DEPENDENCY_SOURCES: dict[str, tuple[str, str, str, str]] = {
    "OpenAICompatibleProvider": (
        "class",
        "pydantic_ai.providers._openai_compatible",
        "OpenAICompatibleProvider",
        "622833df4c9bf3f373929453175e7a0a18cff747974020c12ea79a8d69505637",
    ),
    "create_async_httpx2_client": (
        "function",
        "pydantic_ai._http",
        "create_async_httpx2_client",
        "2934907ac8128a26b3d3f65006bc504536ab5bdfbd0609ee4ff53d3cc66e85aa",
    ),
}
_REVIEWED_NAMESPACE_SOURCES: dict[str, tuple[str, str, str, str]] = {
    "_jwt_payload": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "_jwt_payload",
        "ea5e87c898865b75037791f468e40848f5e4f72756b55103eb68eaa276c8548c",
    ),
    "_jwt_expires_at": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "_jwt_expires_at",
        "4271d15af434733385a3713fd8a3e3097dbe631ced02ca33c04d78784199eca9",
    ),
    "_account_id_from_id_token": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "_account_id_from_id_token",
        "ff61cc1be6a5701b82fc9399a0b2934760bb347fba84d647b57d42dbdace136e",
    ),
    "_JwtAuthClaim": (
        "class",
        "pydantic_ai.providers.openai_codex",
        "_JwtAuthClaim",
        "70e8772bf8b29ea51fca3890e4c293607ddfe32604caacee5d46519cba924a3e",
    ),
    "_JwtPayload": (
        "class",
        "pydantic_ai.providers.openai_codex",
        "_JwtPayload",
        "46a39993f3d316c863dc75cdf10d3cf9453b654d029be676f6e129cdb25fe7cb",
    ),
    "_credentials_from_token_response": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "_credentials_from_token_response",
        "33287944b6409aceb6da16cc46aa6a90e49e0035e5b3f85fecd790b9e05d6b64",
    ),
    "OpenAICodexCredentials": (
        "class",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexCredentials",
        "6bbc59d7f3e67308b13e52f0f69474cedca7798478c119291fce8c0648486709",
    ),
    "_post_token_request": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "_post_token_request",
        "f69f7642316bed911f687e629a94e9025bf5fb32d045a2663e038aa2ac6f608c",
    ),
    "_refresh_credentials": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "_refresh_credentials",
        "0fb368d1c38f991f409ee28bdf65f6c209942d73acd3b0cef4334e696483dad7",
    ),
    "_CredentialsError": (
        "class",
        "pydantic_ai.providers.openai_codex",
        "_CredentialsError",
        "4a1b764654d9b95f9c8592821d3f063eb632cee60df99e24498c5389b68a6f57",
    ),
    "_CredentialsError.__init__": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "_CredentialsError.__init__",
        "56f8da9195188d7b411fd40e8d8bb37992275eb2bc5d5ea9e54af71bdb5a28a1",
    ),
    "CredentialsRefreshError": (
        "class",
        "pydantic_ai.providers.openai_codex",
        "CredentialsRefreshError",
        "3fe8d437dcc4da5ac0fac96c6d21bd2cd3c4107a2b7b18926c739b79e91b700e",
    ),
    "CredentialsPersistenceError": (
        "class",
        "pydantic_ai.providers.openai_codex",
        "CredentialsPersistenceError",
        "a6337c4dd33e445fbe14c96b7bd36363d8c0d33d20afe9cdc251d80e4b43a03f",
    ),
    "_TokenResponse": (
        "class",
        "pydantic_ai.providers.openai_codex",
        "_TokenResponse",
        "b3c101afeab7975188c97483817bb5315c8fc588308db2e28f9752e0e7e74c8c",
    ),
    "_TokenErrorResponse": (
        "class",
        "pydantic_ai.providers.openai_codex",
        "_TokenErrorResponse",
        "eb5447224a1c9b8e7dc89a5579d4de6f746a855cba40af301a27906c07342aaa",
    ),
    "_OpenAICodexAuth": (
        "class",
        "pydantic_ai.providers.openai_codex",
        "_OpenAICodexAuth",
        "0d272c4dc16bc533a1bfbcc880d8ea99e0e7f4ec24c3f3a8fc9ba19815ba1de0",
    ),
    "_OpenAICodexAuth.__init__": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "_OpenAICodexAuth.__init__",
        "b45daed78e23dbd9274dc912ac9ee03ff6f31a7427fd2eb5ed5b5319c952e9e0",
    ),
    "_OpenAICodexAuth._apply_headers": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "_OpenAICodexAuth._apply_headers",
        "5ca602e0de0bf6bebf151043017cc190f723953e1d68deb4d6a2f720f684d0e5",
    ),
    "_OpenAICodexAuth.sync_auth_flow": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "_OpenAICodexAuth.sync_auth_flow",
        "fd0e81b880a660e1062f19ac47ef9abc26b0ca23d729937f32abd247de571ef4",
    ),
    "_OpenAICodexAuth.async_auth_flow": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "_OpenAICodexAuth.async_auth_flow",
        "21cb4707c0010c02bbb33cec2d7a6128d979f533230a9a8106ac4f0fef09b216",
    ),
    "OpenAICodexProvider": (
        "class",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider",
        "e415593d8fd2fbdfadf654fc478cb56b06bd9e34ed3d0e87d6a03b7313b81b7d",
    ),
    "OpenAICodexProvider.__init__": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider.__init__",
        "34d6bba70131fb7a4d5cde843258ed2be9facbb73734e00fc661b4f6a0aee754",
    ),
    "OpenAICodexProvider._set_http_client": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider._set_http_client",
        "637e5ecc678e88d4af08eb52267dba3966d96aa9455ae5385b5989259e17379f",
    ),
    "OpenAICodexProvider._create_http_client": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider._create_http_client",
        "0636f17f676c50cd04bb161c3f02c3ff05314f7de1aef4e7ab3667d16889dfc7",
    ),
    "OpenAICodexProvider.name": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider.name",
        "0f6516bc9797747c21ea61345575535651495aa46e3216bfe07a912786c7b07c",
    ),
    "OpenAICodexProvider.base_url": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider.base_url",
        "429a183b33028a004cd79a4c861d57d8218ac548d78e6fdd122cb76e5c5ded81",
    ),
    "OpenAICodexProvider.client": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider.client",
        "bf6115355829236f537d4db5e863dc17a3fb8ca00e748e52571b50d736a4387b",
    ),
    "OpenAICodexProvider._prepare_request_credentials": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider._prepare_request_credentials",
        "21e476b52998b723a6261b5ef17d7262b7e9e40851c4d2170d7c04594e79e961",
    ),
    "OpenAICodexProvider._load_if_needed": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider._load_if_needed",
        "59936598e919f530bbbd4c19414fef7136ee1096fe868c1320fa8112d57b8ea1",
    ),
    "OpenAICodexProvider._refresh_if_stale": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider._refresh_if_stale",
        "febdc67fe1c213ef015dc68f330f4ac69a26f56f039b7f5e52067d610914a61c",
    ),
    "OpenAICodexProvider._refresh_for_401": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider._refresh_for_401",
        "c3c450bdf40aa3af022adde64a64d666ebd583be18b347fde670b925cfd9a16b",
    ),
    "OpenAICodexProvider._refresh_locked": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider._refresh_locked",
        "59e281ab33601f2560ad7ec8903efef3170eb13e28096bff387c1d164418337b",
    ),
    "OpenAICodexProvider._refresh_lock": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider._refresh_lock",
        "1daa1007fd977f1e1eaf80c73e05eb40e186c495b16e6d79c704eb22ff6a15c4",
    ),
    "OpenAICodexProvider._replace": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider._replace",
        "9dcea17bd30eb610164708daa6b84407862eefa35d4ede2b89ad06734971b055",
    ),
    "OpenAICodexProvider._is_stale": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider._is_stale",
        "122628764fafa56f2284dc06cadccd41b4b8b281ef236b23a8c1bfc9b53b930d",
    ),
    "OpenAICodexProvider.credentials": (
        "function",
        "pydantic_ai.providers.openai_codex",
        "OpenAICodexProvider.credentials",
        "d58fb613b9847f5fd9b0b9b31cf92380a8ca289b83751d21ef52e969f8bc9e8a",
    ),
}
_REVIEWED_CONSTANTS: dict[str, object] = {
    "_CODEX_BASE_URL": "https://chatgpt.com/backend-api/codex",
    "_CODEX_HOST": "chatgpt.com",
    "_TOKEN_URL": "https://auth.openai.com/oauth/token",
    "_PUBLIC_CLIENT_ID": "app_EMoamEEZ73f0CkXaXp7hrann",
    "_REDIRECT_URI": "http://localhost:1455/auth/callback",
    "_DEFAULT_SCOPE": "openid profile email offline_access",
    "_ORIGINATOR": "pydantic-ai",
    "_TOKEN_EXPIRY_BUFFER": timedelta(seconds=30),
}
_SAFE_PERSISTENCE_MESSAGE = (
    "Application-owned credential persistence failed after refresh."
)


def _source_fingerprint(value: object) -> str | None:
    try:
        source = textwrap.dedent(inspect.getsource(cast(Any, value))).strip()
    except Exception:  # noqa: BLE001 - source availability is a fail-closed guard
        return None
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _guarded_callable(name: str, value: object) -> bool:
    expected = _REVIEWED_NAMESPACE_SOURCES.get(name)
    if expected is None:
        return False
    return _guarded_source(value, expected)


def _guarded_source(value: object, expected: tuple[str, str, str, str]) -> bool:
    kind, module_name, qualname, fingerprint = expected
    if kind == "class":
        if not inspect.isclass(value):
            return False
    elif not inspect.isfunction(value):
        return False
    return (
        getattr(value, "__module__", None) == module_name
        and getattr(value, "__name__", None) == qualname.rsplit(".", 1)[-1]
        and getattr(value, "__qualname__", None) == qualname
        and _source_fingerprint(value) == fingerprint
    )


def _module_source_sha256(module: Any) -> str | None:
    path = getattr(module, "__file__", None)
    if type(path) is not str or not path.endswith(".py"):
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001 - source/artifact availability fails closed
        return None


def _static_member(owner: type[Any], name: str) -> object | None:
    try:
        member = inspect.getattr_static(owner, name)
    except (AttributeError, TypeError):
        return None
    if isinstance(member, (staticmethod, classmethod)):
        return member.__func__
    if isinstance(member, property):
        return member.fget
    if (
        type(member).__module__ == "functools"
        and type(member).__name__ == "cached_property"
    ):
        return getattr(member, "func", None)
    return member


def _new_upstream_persistence_error(module: Any) -> BaseException:
    """Build the reviewed upstream error without retaining local context."""

    error = module.CredentialsPersistenceError(_SAFE_PERSISTENCE_MESSAGE)
    error.__cause__ = None
    error.__context__ = None
    return error


class PydanticAIIntegrationError(CodexCredentialError):
    """The optional PydanticAI integration is unavailable or unsupported."""

    def __init__(self, code: str = "integration_unavailable") -> None:
        super().__init__(code, category="pydantic_ai")


def _installed_version() -> str | None:
    try:
        return importlib.metadata.version("pydantic-ai")
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - environment metadata is untrusted
        return None


def _load_pinned_provider_module() -> Any:
    version = _installed_version()
    if version != PINNED_PYDANTIC_AI_VERSION:
        raise PydanticAIIntegrationError("unsupported_pydantic_ai_version")
    failure: PydanticAIIntegrationError | None = None
    try:
        module = importlib.import_module("pydantic_ai.providers.openai_codex")
    except Exception:  # noqa: BLE001 - optional dependency import text is untrusted
        failure = PydanticAIIntegrationError("pydantic_ai_import_failed")
        module = None
    if failure is not None:
        raise failure
    assert module is not None
    if (
        module.__name__ != "pydantic_ai.providers.openai_codex"
        or _module_source_sha256(module) != _REVIEWED_MODULE_SOURCE_SHA256
    ):
        raise PydanticAIIntegrationError("pydantic_ai_module_artifact_changed")
    dependencies: dict[str, Any] = {}
    for dependency_name, expected_sha256 in _REVIEWED_DEPENDENCY_MODULES.items():
        try:
            dependency = importlib.import_module(dependency_name)
        except BaseException:  # noqa: BLE001 - dependency identity is untrusted
            raise PydanticAIIntegrationError(
                "pydantic_ai_dependency_artifact_changed"
            ) from None
        if (
            dependency.__name__ != dependency_name
            or _module_source_sha256(dependency) != expected_sha256
        ):
            raise PydanticAIIntegrationError("pydantic_ai_dependency_artifact_changed")
        dependencies[dependency_name] = dependency
    for constant_name, expected in _REVIEWED_CONSTANTS.items():
        try:
            actual = getattr(module, constant_name)
        except AttributeError:
            raise PydanticAIIntegrationError(
                "pydantic_ai_reviewed_constant_missing"
            ) from None
        if type(actual) is not type(expected) or actual != expected:
            raise PydanticAIIntegrationError("pydantic_ai_reviewed_constant_changed")
    base = getattr(module, "OpenAICodexProvider", None)
    refresh = getattr(module, "_refresh_credentials", None)
    if base is None or not callable(refresh):
        raise PydanticAIIntegrationError("pydantic_ai_refresh_seam_missing")
    compatible_provider = getattr(
        dependencies["pydantic_ai.providers._openai_compatible"],
        "OpenAICompatibleProvider",
        None,
    )
    if not _guarded_source(
        compatible_provider, _REVIEWED_DEPENDENCY_SOURCES["OpenAICompatibleProvider"]
    ) or getattr(base, "__bases__", None) != (compatible_provider,):
        raise PydanticAIIntegrationError("pydantic_ai_inherited_provider_changed")
    if not _guarded_source(
        getattr(module, "create_async_httpx2_client", None),
        _REVIEWED_DEPENDENCY_SOURCES["create_async_httpx2_client"],
    ):
        raise PydanticAIIntegrationError("pydantic_ai_dependency_seam_changed")
    persistence_error = getattr(module, "CredentialsPersistenceError", None)
    credentials_error = getattr(module, "_CredentialsError", None)
    if (
        not inspect.isclass(persistence_error)
        or persistence_error.__module__ != module.__name__
        or not issubclass(persistence_error, Exception)
        or not _guarded_callable("CredentialsPersistenceError", persistence_error)
        or not inspect.isclass(credentials_error)
        or "__init__" in vars(persistence_error)
        or getattr(persistence_error, "__init__", None)
        is not getattr(credentials_error, "__init__", None)
    ):
        raise PydanticAIIntegrationError("pydantic_ai_persistence_error_changed")
    try:
        persistence_probe = persistence_error("reviewed")
    except Exception:  # noqa: BLE001 - the upstream class is untrusted until guarded
        raise PydanticAIIntegrationError("pydantic_ai_persistence_error_changed")
    if str(persistence_probe) != "reviewed" or persistence_probe.args != ("reviewed",):
        raise PydanticAIIntegrationError("pydantic_ai_persistence_error_changed")
    signature_failure = False
    try:
        refresh_signature = inspect.signature(refresh)
        locked_signature = inspect.signature(base._refresh_locked)
        provider_signature = inspect.signature(base.__init__)
        persistence_signature = inspect.signature(persistence_error)
    except (AttributeError, TypeError, ValueError):
        signature_failure = True
        refresh_signature = locked_signature = provider_signature = None
        persistence_signature = None
    if signature_failure:
        raise PydanticAIIntegrationError("pydantic_ai_signature_unavailable")
    assert refresh_signature is not None
    assert locked_signature is not None
    assert provider_signature is not None
    assert persistence_signature is not None
    refresh_parameters = tuple(refresh_signature.parameters)
    locked_parameters = tuple(locked_signature.parameters)
    provider_parameters = tuple(provider_signature.parameters)
    persistence_parameters = tuple(persistence_signature.parameters)
    if refresh_parameters != ("credentials", "http_client"):
        raise PydanticAIIntegrationError("pydantic_ai_refresh_signature_changed")
    if locked_parameters != ("self",):
        raise PydanticAIIntegrationError("pydantic_ai_locked_signature_changed")
    if provider_parameters != (
        "self",
        "credentials",
        "credential_source",
        "openai_client",
        "http_client",
    ):
        raise PydanticAIIntegrationError("pydantic_ai_constructor_signature_changed")
    if persistence_parameters != ("message",):
        raise PydanticAIIntegrationError("pydantic_ai_persistence_signature_changed")
    for name in _REVIEWED_NAMESPACE_SOURCES:
        if "." not in name:
            try:
                member = getattr(module, name)
            except AttributeError:
                member = None
        else:
            owner_name, member_name = name.rsplit(".", 1)
            try:
                owner = getattr(module, owner_name)
            except AttributeError:
                owner = None
            if owner is None or not inspect.isclass(owner):
                member = None
            elif member_name not in vars(owner):
                # An inherited same-signature replacement is not an audited
                # implementation of the named runtime seam.
                member = None
            else:
                member = _static_member(owner, member_name)
        if not _guarded_callable(name, member):
            raise PydanticAIIntegrationError("pydantic_ai_reviewed_seam_changed")
    return module


_MAX_ADAPTER_ATTEMPT_SEQUENCE = (1 << 31) - 1


def _safe_integration_code(value: object, fallback: str) -> str:
    code = getattr(value, "code", value)
    if (
        type(code) is str
        and 0 < len(code) <= 64
        and set(code)
        <= frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    ):
        return code
    return fallback


def _validate_adapter_timeout(value: object) -> float | None:
    """Validate adapter deadlines without exposing raw numeric failures."""

    if value is None:
        return None
    try:
        return _validate_duration(
            cast(float, value),
            default=30.0,
            maximum=_MAX_REFRESH_TIMEOUT,
            code="refresh_timeout must be finite and bounded",
        )
    except (OverflowError, TypeError, ValueError):
        raise PydanticAIIntegrationError("refresh_timeout_invalid") from None


def _require_audited_file_source(value: object) -> OpenAICodexCredentialSource:
    """Require the exact reviewed application source facade.

    The adapter's reviewed integration seam is the concrete source facade,
    not an arbitrary object satisfying the backend protocol.  The facade may
    wrap a separately reviewed backend; only the audited file backend carries
    the local POSIX deadline/drain guarantees documented by this adapter.
    """

    if type(value) is not OpenAICodexCredentialSource:
        raise PydanticAIIntegrationError("audited_file_source_required")
    return value


ProviderClassFactory: TypeAlias = Callable[..., Any]


def get_pydantic_ai_codex_provider_class() -> type[Any]:
    """Return a guarded subclass of the pinned PydanticAI provider.

    The subclass is created only after the exact version and private signatures
    have been checked.  Using the private refresh helper is intentional and is
    bounded by this guard plus the optional-integration tests.
    """

    module = _load_pinned_provider_module()
    base = module.OpenAICodexProvider

    class ApplicationOwnedCodexProvider(base):  # type: ignore[misc,valid-type]
        """PydanticAI 2.48.0 provider with application-owned refresh atomicity."""

        def __init__(
            self,
            *,
            credential_source: OpenAICodexCredentialSource,
            pressure: ProviderPressure | None = None,
            refresh_timeout: float | None = None,
            **kwargs: Any,
        ) -> None:
            source = _require_audited_file_source(credential_source)
            validated_refresh_timeout = _validate_adapter_timeout(refresh_timeout)
            self._application_pressure = (
                pressure if pressure is not None else ProviderPressure()
            )
            if type(self._application_pressure) is not ProviderPressure:
                raise PydanticAIIntegrationError("provider_pressure_required")
            try:
                scope = self._application_pressure.new_scope()
            except CodexCredentialError as error:
                raise PydanticAIIntegrationError(
                    _safe_integration_code(error, "generation_scope_limit")
                ) from None
            except BaseException:  # noqa: BLE001 - pressure state is untrusted
                raise PydanticAIIntegrationError(
                    "generation_scope_unavailable"
                ) from None
            self._application_scope = scope
            self._application_scope_released = False
            self._application_closing = False
            self._application_attempt_sequence = 0
            self._application_attempt_task: asyncio.Task[str] | None = None
            self._application_attempt_lock = asyncio.Lock()
            self._application_idle_event: asyncio.Event | None = None
            self._application_active_operations = 0
            self._application_close_task: asyncio.Task[None] | None = None
            self._application_refresh_timeout = validated_refresh_timeout
            try:
                super().__init__(credential_source=source, **kwargs)
            except BaseException:
                with contextlib.suppress(BaseException):
                    self._application_pressure.release_scope(scope)
                raise

        def _bind_application_activity(self) -> None:
            if self._application_idle_event is None:
                self._application_idle_event = asyncio.Event()
                self._application_idle_event.set()

        def _begin_application_operation(self) -> None:
            self._bind_application_activity()
            if self._application_closing:
                raise CredentialRefreshError("provider_closed")
            self._application_active_operations += 1
            if self._application_active_operations == 1:
                assert self._application_idle_event is not None
                self._application_idle_event.clear()

        def _end_application_operation(self) -> None:
            self._application_active_operations = max(
                0, self._application_active_operations - 1
            )
            if self._application_active_operations == 0:
                event = self._application_idle_event
                if event is not None:
                    event.set()

        async def _run_application_attempt(
            self,
            generation: ProviderGeneration,
            source: OpenAICodexCredentialSource,
            rejected: Any,
        ) -> str:
            """Run one pressure attempt and retain only a categorical status."""

            try:
                reviewed_module = _load_pinned_provider_module()
            except BaseException:  # noqa: BLE001 - guard failures are categorical
                return "reviewed_seam_changed"

            async def network_refresh(current: Any) -> Any:
                try:
                    return await reviewed_module._refresh_credentials(
                        current,
                        http_client=self._http_client,
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException:  # noqa: BLE001 - provider text is untrusted
                    raise CredentialRefreshError("provider_refresh_failed") from None

            try:
                outcome = await self._application_pressure.run(
                    generation,
                    lambda: source.refresh(
                        network_refresh,
                        expected=rejected,
                        timeout=self._application_refresh_timeout,
                    ),
                    timeout=self._application_refresh_timeout,
                )
            except ProviderOperationError as error:
                return _safe_integration_code(error, "provider_refresh_failed")
            except asyncio.CancelledError:
                return "provider_refresh_cancelled"
            except BaseException:  # noqa: BLE001 - pressure details are untrusted
                return "provider_refresh_failed"
            if isinstance(outcome, ProviderCompletion):
                return _safe_integration_code(outcome.status, "provider_refresh_failed")
            # The credential returned by the pressure operation is deliberately
            # discarded.  The task result is only a status; the caller reloads
            # the durable file after settlement, so no task retains a token.
            return "completed"

        async def _application_refresh_status(
            self,
            source: OpenAICodexCredentialSource,
            rejected: Any,
        ) -> str:
            async with self._application_attempt_lock:
                task = self._application_attempt_task
                if task is None or task.done():
                    if (
                        self._application_attempt_sequence
                        >= _MAX_ADAPTER_ATTEMPT_SEQUENCE
                    ):
                        raise CredentialRefreshError("refresh_generation_exhausted")
                    self._application_attempt_sequence += 1
                    try:
                        generation = ProviderGeneration(
                            self._application_scope,
                            self._application_attempt_sequence,
                        )
                    except (OverflowError, TypeError, ValueError):
                        raise CredentialRefreshError(
                            "refresh_generation_exhausted"
                        ) from None
                    task = asyncio.create_task(
                        self._run_application_attempt(generation, source, rejected)
                    )
                    self._application_attempt_task = task
            try:
                return cast(str, await asyncio.shield(task))
            except asyncio.CancelledError:
                await _await_task_drained(task)
                raise
            except BaseException:  # noqa: BLE001 - task details are untrusted
                return "provider_refresh_failed"
            finally:
                if self._application_attempt_task is task and task.done():
                    self._application_attempt_task = None

        async def _refresh_locked(self) -> None:
            self._begin_application_operation()
            try:
                try:
                    reviewed_module = _load_pinned_provider_module()
                except BaseException:  # noqa: BLE001 - guard failures are categorical
                    raise CredentialRefreshError("reviewed_seam_changed") from None
                source = getattr(self, "_credential_source", None)
                if type(source) is not OpenAICodexCredentialSource:
                    raise CredentialRefreshError("audited_file_source_required")
                rejected = self.credentials
                status = await self._application_refresh_status(source, rejected)
                if status == "credential_persistence":
                    raise _new_upstream_persistence_error(reviewed_module)
                if status not in {"completed", "replayed"}:
                    raise CredentialRefreshError("provider_refresh_failed")
                storage_failure = False
                try:
                    persisted = await source.load()
                except asyncio.CancelledError:
                    raise
                except CodexCredentialError as error:
                    if _is_credential_storage_error(error):
                        storage_failure = True
                        persisted = None
                    else:
                        raise CredentialRefreshError(
                            "provider_refresh_failed"
                        ) from None
                except BaseException:  # noqa: BLE001 - source details are untrusted
                    raise CredentialRefreshError("provider_refresh_failed") from None
                if storage_failure:
                    raise _new_upstream_persistence_error(reviewed_module)
                assert persisted is not None
                if status == "replayed" and persisted == rejected:
                    raise CredentialRefreshError("provider_refresh_failed")
                self._replace(persisted)
            finally:
                self._end_application_operation()

        async def _close_application(self) -> None:
            self._application_closing = True
            event = self._application_idle_event
            if event is not None and not event.is_set():
                await event.wait()
            task = self._application_attempt_task
            if task is not None and not task.done():
                await asyncio.shield(task)
            if not self._application_scope_released:
                source = getattr(self, "_credential_source", None)
                if type(source) is OpenAICodexCredentialSource:
                    with contextlib.suppress(BaseException):
                        source._backend.close()  # type: ignore[attr-defined]
                try:
                    self._application_pressure.release_scope(self._application_scope)
                except ProviderOperationError:
                    raise PydanticAIIntegrationError(
                        "generation_scope_release_failed"
                    ) from None
                except BaseException:  # noqa: BLE001 - pressure details are untrusted
                    raise PydanticAIIntegrationError(
                        "generation_scope_release_failed"
                    ) from None
                self._application_scope_released = True

        async def close(self) -> None:
            """Drain active refresh work, then release this provider's scope once."""

            task = self._application_close_task
            if task is None:
                task = asyncio.create_task(self._close_application())
                self._application_close_task = task
            await _await_task_drained(task)

        async def aclose(self) -> None:
            await self.close()

        async def __aenter__(self) -> Any:
            if self._application_closing:
                raise PydanticAIIntegrationError("provider_closed")
            return self

        async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
            await self.close()

    ApplicationOwnedCodexProvider.__name__ = "ApplicationOwnedCodexProvider"
    ApplicationOwnedCodexProvider.__qualname__ = "ApplicationOwnedCodexProvider"
    return ApplicationOwnedCodexProvider


def create_pydantic_ai_codex_provider(
    credential_source: OpenAICodexCredentialSource,
    *,
    pressure: ProviderPressure | None = None,
    refresh_timeout: float | None = None,
    http_client: Any | None = None,
) -> Any:
    """Construct the guarded provider without making a network request.

    The pinned adapter accepts only the exact audited
    :class:`OpenAICodexCredentialSource` facade.  Its secure file backend is
    the reviewed finite-deadline/cancellation path; a separately reviewed
    backend remains replaceable but receives no stronger liveness claim.  A
    caller may pass an ``httpx2.AsyncClient`` with a test transport; no client
    request occurs during construction.
    """

    audited_source = _require_audited_file_source(credential_source)
    provider_class = get_pydantic_ai_codex_provider_class()
    kwargs: dict[str, Any] = {
        "credential_source": audited_source,
        "pressure": pressure,
        "refresh_timeout": refresh_timeout,
    }
    if http_client is not None:
        kwargs["http_client"] = http_client
    return provider_class(**kwargs)
