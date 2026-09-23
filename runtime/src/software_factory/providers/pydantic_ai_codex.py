"""Explicit PydanticAI 2.48.0 Codex integration.

The runtime keeps ``pydantic-ai`` optional.  When this module is used, it
fail-closes unless the installed distribution is exactly the architecture-pinned
2.48.0 release and its private refresh seam still has the audited signature.
The adapter replaces PydanticAI's separate load → network refresh → save path
with one call to the application source's locked ``refresh`` transaction.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import inspect
from collections.abc import Callable
from typing import Any, TypeAlias

from .openai_codex import (
    CodexCredentialError,
    CredentialRefreshError,
    OpenAICodexCredentialSource,
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
    signature_failure = False
    try:
        refresh_signature = inspect.signature(refresh)
        locked_signature = inspect.signature(base._refresh_locked)
        provider_signature = inspect.signature(base.__init__)
    except (AttributeError, TypeError, ValueError):
        signature_failure = True
        refresh_signature = locked_signature = provider_signature = None
    if signature_failure:
        raise PydanticAIIntegrationError("pydantic_ai_signature_unavailable")
    assert refresh_signature is not None
    assert locked_signature is not None
    assert provider_signature is not None
    refresh_parameters = tuple(refresh_signature.parameters)
    locked_parameters = tuple(locked_signature.parameters)
    provider_parameters = tuple(provider_signature.parameters)
    if refresh_parameters != ("credentials", "http_client"):
        raise PydanticAIIntegrationError("pydantic_ai_refresh_signature_changed")
    if locked_parameters != ("self",):
        raise PydanticAIIntegrationError("pydantic_ai_locked_signature_changed")
    if "credential_source" not in provider_parameters:
        raise PydanticAIIntegrationError("pydantic_ai_source_parameter_missing")
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

            generation = ("pydantic-ai-2.48.0", id(self), self._revision)
            pressure_failure: CodexCredentialError | None = None
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
            except ProviderOperationError:
                pressure_failure = CredentialRefreshError("provider_refresh_failed")
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 - source/provider text is untrusted
                pressure_failure = CredentialRefreshError("provider_refresh_failed")
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
