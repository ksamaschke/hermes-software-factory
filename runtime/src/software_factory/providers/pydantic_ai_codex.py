"""Explicit PydanticAI 2.48.0 Codex integration.

The runtime keeps ``pydantic-ai`` optional.  When this module is used, it
fail-closes unless the installed distribution is exactly the architecture-pinned
2.48.0 release and its private refresh seam still has the audited signature.
The adapter replaces PydanticAI's separate load → network refresh → save path
with one call to the application source's locked ``refresh`` transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import inspect
import textwrap
import types
from collections.abc import Callable
from typing import Any, TypeAlias, cast

from .openai_codex import (
    CodexCredentialError,
    CredentialPersistenceError,
    CredentialRefreshError,
    OpenAICodexCredentialSource,
    ProviderCompletion,
    ProviderGeneration,
    ProviderOperationError,
    ProviderPressure,
)

__all__ = (
    "PINNED_PYDANTIC_AI_VERSION",
    "PydanticAIIntegrationError",
    "create_pydantic_ai_codex_provider",
    "get_pydantic_ai_codex_provider_class",
)

PINNED_PYDANTIC_AI_VERSION = "2.48.0"

# These are reviewed constants for the private PydanticAI seam.  A version
# upgrade is intentionally blocked until this exact table is reviewed and
# replaced; signatures alone do not protect refresh, stale, or persistence
# behavior from a same-signature upstream change.
_REVIEWED_SEAM_FINGERPRINTS: dict[str, tuple[str, str]] = {
    "_refresh_credentials": (
        "0fb368d1c38f991f409ee28bdf65f6c209942d73acd3b0cef4334e696483dad7",
        "a0fb49a5958f2ac87e7b5e2e856b2a1362d2605d6f7b6db0f76d1fc45a735e11",
    ),
    "OpenAICodexProvider.__init__": (
        "34d6bba70131fb7a4d5cde843258ed2be9facbb73734e00fc661b4f6a0aee754",
        "599b0001ccd41ec68c8a7e2d064aa3c34aa3133f7fed383d1a567e6686897157",
    ),
    "OpenAICodexProvider._refresh_locked": (
        "59e281ab33601f2560ad7ec8903efef3170eb13e28096bff387c1d164418337b",
        "eafda9143a966bf76d299fd2256ba3a43f2c78af77a118ac15e5737bc02a8ae8",
    ),
    "OpenAICodexProvider._replace": (
        "9dcea17bd30eb610164708daa6b84407862eefa35d4ede2b89ad06734971b055",
        "cc2c1d1a66c1c4803d15baded24611825f2caa60e256e1303d279dd5583d1821",
    ),
    "OpenAICodexProvider._load_if_needed": (
        "59936598e919f530bbbd4c19414fef7136ee1096fe868c1320fa8112d57b8ea1",
        "b0ae77650578dc2c5eea872f5f173f87fb4b81573d9c6624403895990e727fdb",
    ),
    "OpenAICodexProvider._refresh_if_stale": (
        "febdc67fe1c213ef015dc68f330f4ac69a26f56f039b7f5e52067d610914a61c",
        "5a68b7b83f56776765657706debf1d795ae7565d4b5f32ceec1bc07c7395f785",
    ),
    "OpenAICodexProvider._refresh_for_401": (
        "c3c450bdf40aa3af022adde64a64d666ebd583be18b347fde670b925cfd9a16b",
        "08140c3e4a3536d532e863a116cd345d91295e15c85411549e14f01ecb185155",
    ),
    "OpenAICodexProvider._prepare_request_credentials": (
        "21e476b52998b723a6261b5ef17d7262b7e9e40851c4d2170d7c04594e79e961",
        "dd8ca0b0ee8b31d0f157ca6074d46680955e46bbc0c931588c823cc6355a443a",
    ),
    "OpenAICodexProvider._is_stale": (
        "122628764fafa56f2284dc06cadccd41b4b8b281ef236b23a8c1bfc9b53b930d",
        "4e44935759e794d8d92c430f306a704564efddb786ede21daa41f00980b66525",
    ),
    "CredentialsPersistenceError.__init__": (
        "56f8da9195188d7b411fd40e8d8bb37992275eb2bc5d5ea9e54af71bdb5a28a1",
        "b529089e8c08a56392d60c4bcdff155f0dfa874d51f5bfbaf13b9aeae5bfb66c",
    ),
}
_REVIEWED_CLASS_SOURCES = {
    "CredentialsPersistenceError": "a6337c4dd33e445fbe14c96b7bd36363d8c0d33d20afe9cdc251d80e4b43a03f"
}
_SAFE_PERSISTENCE_MESSAGE = (
    "Application-owned credential persistence failed after refresh."
)


def _source_fingerprint(value: object) -> str | None:
    try:
        source = textwrap.dedent(inspect.getsource(cast(Any, value))).strip()
    except (OSError, TypeError):
        return None
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _code_shape(value: object) -> tuple[object, ...] | None:
    code = getattr(value, "__code__", None)
    if not isinstance(code, types.CodeType):
        return None

    def constant_shape(constant: object) -> object:
        if isinstance(constant, types.CodeType):
            return (
                "code",
                constant.co_argcount,
                constant.co_kwonlyargcount,
                constant.co_nlocals,
                constant.co_flags,
                constant.co_code.hex(),
                tuple(constant_shape(item) for item in constant.co_consts),
                constant.co_names,
                constant.co_varnames,
                constant.co_freevars,
                constant.co_cellvars,
            )
        if type(constant) in {str, int, float, bytes, bool, type(None)}:
            return (type(constant).__name__, repr(constant))
        return (type(constant).__name__,)

    shape = (
        code.co_argcount,
        code.co_kwonlyargcount,
        code.co_nlocals,
        code.co_flags,
        code.co_code.hex(),
        tuple(constant_shape(item) for item in code.co_consts),
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
    )
    return shape


def _bytecode_fingerprint(value: object) -> str | None:
    shape = _code_shape(value)
    if shape is None:
        return None
    return hashlib.sha256(repr(shape).encode("utf-8")).hexdigest()


def _guarded_callable(name: str, value: object) -> bool:
    expected = _REVIEWED_SEAM_FINGERPRINTS.get(name)
    if expected is None:
        return False
    return (
        _source_fingerprint(value) == expected[0]
        and _bytecode_fingerprint(value) == expected[1]
    )


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
    base = getattr(module, "OpenAICodexProvider", None)
    refresh = getattr(module, "_refresh_credentials", None)
    if base is None or not callable(refresh):
        raise PydanticAIIntegrationError("pydantic_ai_refresh_seam_missing")
    persistence_error = getattr(module, "CredentialsPersistenceError", None)
    if (
        not inspect.isclass(persistence_error)
        or persistence_error.__module__ != module.__name__
        or not issubclass(persistence_error, Exception)
        or _source_fingerprint(persistence_error)
        != _REVIEWED_CLASS_SOURCES["CredentialsPersistenceError"]
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
    guarded_members: dict[str, object] = {
        "_refresh_credentials": refresh,
        "OpenAICodexProvider.__init__": base.__init__,
        "OpenAICodexProvider._refresh_locked": base._refresh_locked,
        "OpenAICodexProvider._replace": base._replace,
        "OpenAICodexProvider._load_if_needed": base._load_if_needed,
        "OpenAICodexProvider._refresh_if_stale": base._refresh_if_stale,
        "OpenAICodexProvider._refresh_for_401": base._refresh_for_401,
        "OpenAICodexProvider._prepare_request_credentials": base._prepare_request_credentials,
        "OpenAICodexProvider._is_stale": base._is_stale,
        "CredentialsPersistenceError.__init__": persistence_error.__init__,
    }
    if any(
        not _guarded_callable(name, member) for name, member in guarded_members.items()
    ):
        raise PydanticAIIntegrationError("pydantic_ai_reviewed_seam_changed")
    return module


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
            refresh_method = getattr(credential_source, "refresh", None)
            if not callable(refresh_method):
                raise PydanticAIIntegrationError("source_refresh_transaction_required")
            self._application_pressure = (
                pressure if pressure is not None else ProviderPressure()
            )
            self._application_scope = self._application_pressure.new_scope()
            self._application_refresh_timeout = refresh_timeout
            super().__init__(credential_source=credential_source, **kwargs)

        async def _refresh_locked(self) -> None:
            source = getattr(self, "_credential_source", None)
            if source is None:
                raise PydanticAIIntegrationError("source_refresh_transaction_required")
            rejected = self.credentials

            async def network_refresh(current: Any) -> Any:
                callback_failure = False
                result: Any = None
                try:
                    result = await module._refresh_credentials(
                        current,
                        http_client=self._http_client,
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException:  # noqa: BLE001 - provider text is untrusted
                    callback_failure = True
                if callback_failure:
                    raise CredentialRefreshError("provider_refresh_failed")
                return result

            generation = ProviderGeneration(self._application_scope, self._revision + 1)
            pressure_failure: CodexCredentialError | None = None
            persistence_failure: BaseException | None = None
            rotated: Any = None
            try:
                rotated = await self._application_pressure.run(
                    generation,
                    lambda: source.refresh(
                        network_refresh,
                        expected=rejected,
                        timeout=self._application_refresh_timeout,
                    ),
                    timeout=self._application_refresh_timeout,
                )
            except ProviderOperationError as error:
                if error.code == "credential_persistence":
                    persistence_failure = _new_upstream_persistence_error(module)
                else:
                    pressure_failure = CredentialRefreshError("provider_refresh_failed")
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 - source/provider text is untrusted
                pressure_failure = CredentialRefreshError("provider_refresh_failed")
            if isinstance(rotated, ProviderCompletion):
                if rotated.status == "credential_persistence":
                    persistence_failure = _new_upstream_persistence_error(module)
                elif rotated.status in {"completed", "replayed"}:
                    try:
                        persisted = await source.load()
                    except CredentialPersistenceError:
                        persistence_failure = _new_upstream_persistence_error(module)
                    except asyncio.CancelledError:
                        raise
                    except BaseException:  # noqa: BLE001 - source details are untrusted
                        pressure_failure = CredentialRefreshError(
                            "provider_refresh_failed"
                        )
                    else:
                        if rotated.status == "replayed" and persisted == rejected:
                            pressure_failure = CredentialRefreshError(
                                "provider_refresh_failed"
                            )
                        else:
                            rotated = persisted
                else:
                    pressure_failure = CredentialRefreshError("provider_refresh_failed")
            if persistence_failure is not None:
                raise persistence_failure
            if pressure_failure is not None:
                raise pressure_failure
            # The application source returns its redacted local credential value.
            # The pinned provider only requires the three structural attributes.
            self._replace(rotated)

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

    ``credential_source`` must be an application-owned source implementing the
    atomic ``refresh`` operation.  A caller may pass an ``httpx2.AsyncClient``
    with a test transport; no client request occurs during construction.
    """

    provider_class = get_pydantic_ai_codex_provider_class()
    kwargs: dict[str, Any] = {
        "credential_source": credential_source,
        "pressure": pressure,
        "refresh_timeout": refresh_timeout,
    }
    if http_client is not None:
        kwargs["http_client"] = http_client
    return provider_class(**kwargs)
