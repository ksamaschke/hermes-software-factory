"""PydanticAI provider lifecycle regressions for pressure scopes and cleanup."""

from __future__ import annotations

import asyncio
from functools import wraps
from typing import Any, cast

import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.providers.openai_codex as pydantic_codex
from software_factory.providers import (
    OpenAICodexCredentials,
    OpenAICodexCredentialSource,
    ProviderPressure,
    create_pydantic_ai_codex_provider,
    get_pydantic_ai_codex_provider_class,
)


def _credentials(generation: str) -> OpenAICodexCredentials:
    return OpenAICodexCredentials(
        access_token=f"access-{generation}.synthetic",
        refresh_token=f"refresh-{generation}.synthetic",
        account_id="account.synthetic",
    )


def _asyncio_test(test):
    """Run an async regression without an undeclared pytest plugin."""

    @wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


def test_provider_constructor_failure_closes_backend_and_releases_scope(monkeypatch):
    class ClosingBackend:
        def __init__(self) -> None:
            self.close_calls = 0

        async def load(self) -> OpenAICodexCredentials:
            return _credentials("old")

        async def save(self, _value: OpenAICodexCredentials) -> None:
            return None

        async def refresh(
            self,
            _callback: object,
            *,
            expected: OpenAICodexCredentials | None = None,
            timeout: float | None = None,
        ) -> OpenAICodexCredentials:
            del expected, timeout
            return _credentials("old")

        def close(self) -> None:
            self.close_calls += 1

    backend = ClosingBackend()
    source = OpenAICodexCredentialSource(backend=cast(Any, backend))
    pressure = ProviderPressure(1)
    provider_class = get_pydantic_ai_codex_provider_class()

    def failing_init(self, **kwargs: Any) -> None:
        del self, kwargs
        raise RuntimeError("synthetic constructor failure")

    monkeypatch.setattr(pydantic_codex.OpenAICodexProvider, "__init__", failing_init)
    with pytest.raises(RuntimeError):
        provider_class(credential_source=source, pressure=pressure)
    assert backend.close_calls == 1
    assert pressure._scopes == {0}
    assert pressure._free_scopes == {1}
    assert pressure._pending_admissions == {}
    assert pressure._active == {}
    assert pressure._leases == {}
    assert pressure.in_flight == 0
    assert pressure.queued == 0


@_asyncio_test
async def test_provider_close_is_idempotent_and_releases_all_scope_state():
    class ClosingBackend:
        def __init__(self) -> None:
            self.close_calls = 0

        async def load(self) -> OpenAICodexCredentials:
            return _credentials("old")

        async def save(self, _value: OpenAICodexCredentials) -> None:
            return None

        async def refresh(
            self,
            _callback: object,
            *,
            expected: OpenAICodexCredentials | None = None,
            timeout: float | None = None,
        ) -> OpenAICodexCredentials:
            del expected, timeout
            return _credentials("old")

        def close(self) -> None:
            self.close_calls += 1

    backend = ClosingBackend()
    source = OpenAICodexCredentialSource(backend=cast(Any, backend))
    pressure = ProviderPressure(1)
    provider = create_pydantic_ai_codex_provider(source, pressure=pressure)
    await asyncio.gather(provider.close(), provider.close(), provider.aclose())
    await provider.close()

    assert backend.close_calls == 1
    assert pressure._scopes == {0}
    assert pressure._pending_admissions == {}
    assert pressure._active == {}
    assert pressure._leases == {}
    assert pressure.in_flight == 0
    assert pressure.queued == 0
