"""PydanticAI provider lifecycle regressions for pressure scopes and cleanup."""

from __future__ import annotations

import asyncio
from functools import wraps
from typing import Any, cast

import pytest

pytest.importorskip("pydantic_ai")

from software_factory.providers import (
    OpenAICodexCredentials,
    OpenAICodexCredentialSource,
    ProviderPressure,
    PydanticAIIntegrationError,
    create_pydantic_ai_codex_provider,
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


def test_provider_rejects_unreviewed_backend_before_any_operation():
    class ClosingBackend:
        def __init__(self) -> None:
            self.calls = 0

        async def load(self) -> OpenAICodexCredentials:
            self.calls += 1
            return _credentials("old")

        async def save(self, _value: OpenAICodexCredentials) -> None:
            self.calls += 1

        async def refresh(
            self,
            _callback: object,
            *,
            expected: OpenAICodexCredentials | None = None,
            timeout: float | None = None,
        ) -> OpenAICodexCredentials:
            del expected, timeout
            self.calls += 1
            return _credentials("old")

        def close(self) -> None:
            self.calls += 1

    backend = ClosingBackend()
    source = OpenAICodexCredentialSource(backend=cast(Any, backend))
    pressure = ProviderPressure(1)
    with pytest.raises(PydanticAIIntegrationError) as raised:
        create_pydantic_ai_codex_provider(source, pressure=pressure)
    assert raised.value.code == "audited_file_source_required"
    assert backend.calls == 0
    assert pressure._scopes == {0}
    assert pressure._free_scopes == set()
    assert pressure._pending_admissions == {}
    assert pressure._active == {}
    assert pressure._leases == {}
    assert pressure.in_flight == 0
    assert pressure.queued == 0


def test_provider_rejects_subclass_and_uninitialized_facades(tmp_path):
    class SourceSubclass(OpenAICodexCredentialSource):
        pass

    subclass = SourceSubclass(tmp_path / "subclass.json")
    try:
        with pytest.raises(PydanticAIIntegrationError) as raised:
            create_pydantic_ai_codex_provider(subclass)
        assert raised.value.code == "audited_file_source_required"
    finally:
        cast(Any, subclass._backend).close()

    uninitialized = object.__new__(OpenAICodexCredentialSource)
    with pytest.raises(PydanticAIIntegrationError) as raised:
        create_pydantic_ai_codex_provider(uninitialized)
    assert raised.value.code == "audited_file_source_required"


@_asyncio_test
async def test_provider_close_is_idempotent_and_releases_all_scope_state(tmp_path):
    source = OpenAICodexCredentialSource(tmp_path / "credentials.json")
    pressure = ProviderPressure(1)
    provider = create_pydantic_ai_codex_provider(source, pressure=pressure)
    await asyncio.gather(provider.close(), provider.close(), provider.aclose())
    await provider.close()

    assert cast(Any, source._backend)._anchor_fd is None
    assert pressure._scopes == {0}
    assert pressure._pending_admissions == {}
    assert pressure._active == {}
    assert pressure._leases == {}
    assert pressure.in_flight == 0
    assert pressure.queued == 0


@_asyncio_test
async def test_provider_close_rejects_backend_replacement_before_invocation(tmp_path):
    class ReplacementBackend:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    source = OpenAICodexCredentialSource(tmp_path / "credentials.json")
    original_backend = source._backend
    provider = create_pydantic_ai_codex_provider(source)
    replacement = ReplacementBackend()
    source._backend = cast(Any, replacement)
    try:
        with pytest.raises(PydanticAIIntegrationError) as raised:
            await provider.close()
        assert raised.value.code == "audited_file_source_required"
        assert replacement.close_calls == 0
    finally:
        source._backend = original_backend
        await provider.close()
