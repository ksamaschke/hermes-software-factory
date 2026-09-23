"""Optional exact-version PydanticAI integration regressions.

The module is skipped in the default runtime environment.  The focused proof is
run with ``uv run --no-project --with pydantic-ai==2.48.0`` and uses only
synthetic credentials plus an in-memory HTTP transport.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
from pathlib import Path

import pytest

pydantic_ai = pytest.importorskip("pydantic_ai")
httpx2 = pytest.importorskip("httpx2")

import pydantic_ai.providers.openai_codex as pydantic_codex
from software_factory.providers import (
    OpenAICodexCredentials,
    OpenAICodexCredentialSource,
    ProviderPressure,
    PydanticAIIntegrationError,
    create_pydantic_ai_codex_provider,
    get_pydantic_ai_codex_provider_class,
)


def _credentials(generation: str) -> OpenAICodexCredentials:
    return OpenAICodexCredentials(
        access_token=f"access-{generation}.synthetic",
        refresh_token=f"refresh-{generation}.synthetic",
        account_id="account.synthetic",
    )


def test_guarded_class_is_the_pinned_real_provider():
    provider_class = get_pydantic_ai_codex_provider_class()
    assert issubclass(provider_class, pydantic_codex.OpenAICodexProvider)
    assert getattr(pydantic_ai, "__version__", "2.48.0") == "2.48.0"


@pytest.mark.asyncio
async def test_real_provider_path_uses_atomic_source_refresh_and_mock_transport(
    tmp_path: Path,
):
    source = OpenAICodexCredentialSource(tmp_path / "credentials.json")
    await source.save(_credentials("old"))
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return httpx2.Response(
            200,
            json={
                "access_token": "access-new.synthetic",
                "refresh_token": "refresh-new.synthetic",
                "account_id": "account.synthetic",
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    pressure = ProviderPressure(1, admission_timeout=1.0)
    provider = create_pydantic_ai_codex_provider(
        source, pressure=pressure, http_client=client
    )
    try:
        await provider._load_if_needed()
        await provider._refresh_for_401(
            provider._revision,
            refresh_failures=provider._refresh_failures,
        )
        persisted = await source.load()
        assert calls == 1
        assert persisted == _credentials("new")
        assert provider.credentials == persisted
        assert pressure.in_flight == 0
        assert pressure.queued == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_actual_pydantic_ai_http_path_refreshes_once_and_retries_without_network(
    tmp_path: Path,
):
    source = OpenAICodexCredentialSource(tmp_path / "credentials.json")
    await source.save(_credentials("old"))
    token_calls = 0
    provider_calls = 0

    async def handler(request):
        nonlocal token_calls, provider_calls
        if "token" in request.url.path:
            token_calls += 1
            return httpx2.Response(
                200,
                json={
                    "access_token": "access-new.synthetic",
                    "refresh_token": "refresh-new.synthetic",
                    "account_id": "account.synthetic",
                },
            )
        provider_calls += 1
        if provider_calls == 1:
            return httpx2.Response(401, json={"error": "expired"})
        return httpx2.Response(
            200,
            json={
                "id": "response.synthetic",
                "object": "response",
                "created_at": 0,
                "model": "gpt-5.6-luna",
                "output": [],
                "usage": None,
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    pressure = ProviderPressure(1, admission_timeout=1.0)
    provider = create_pydantic_ai_codex_provider(
        source, pressure=pressure, http_client=client
    )
    try:
        result = await provider.client.responses.create(
            model="gpt-5.6-luna", input="synthetic request"
        )
        assert type(result).__name__ == "Response"
        assert token_calls == 1
        assert provider_calls == 2
        assert await source.load() == _credentials("new")
        assert pressure.in_flight == 0
        assert pressure.queued == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pressure_wraps_actual_pydantic_ai_refresh_path(tmp_path: Path):
    source_one = OpenAICodexCredentialSource(tmp_path / "one.json")
    source_two = OpenAICodexCredentialSource(tmp_path / "two.json")
    await source_one.save(_credentials("old-one"))
    await source_two.save(_credentials("old-two"))
    active = 0
    maximum_active = 0
    token_calls = 0

    async def handler(request):
        nonlocal active, maximum_active, token_calls
        if "token" not in request.url.path:
            raise AssertionError("unexpected provider request")
        token_calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            await asyncio.sleep(0.05)
            return httpx2.Response(
                200,
                json={
                    "access_token": "access-rotated.synthetic",
                    "refresh_token": "refresh-rotated.synthetic",
                    "account_id": "account.synthetic",
                },
            )
        finally:
            active -= 1

    pressure = ProviderPressure(1, admission_timeout=1.0)
    clients = [
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ]
    providers = [
        create_pydantic_ai_codex_provider(
            source_one, pressure=pressure, http_client=clients[0]
        ),
        create_pydantic_ai_codex_provider(
            source_two, pressure=pressure, http_client=clients[1]
        ),
    ]
    try:
        await asyncio.gather(*(provider._load_if_needed() for provider in providers))
        await asyncio.gather(
            providers[0]._refresh_for_401(
                providers[0]._revision,
                refresh_failures=providers[0]._refresh_failures,
            ),
            providers[1]._refresh_for_401(
                providers[1]._revision,
                refresh_failures=providers[1]._refresh_failures,
            ),
        )
        assert token_calls == 2
        assert maximum_active == 1
        assert pressure.in_flight == 0
        assert pressure.queued == 0
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))


def _provider_process_worker(path: str, calls, calls_lock, ready, results) -> None:
    async def fake_refresh(credentials, *, http_client=None):
        del credentials, http_client
        with calls_lock:
            calls.value += 1
        await asyncio.sleep(0.05)
        return pydantic_codex.OpenAICodexCredentials(
            access_token="access-new.synthetic",
            refresh_token="refresh-new.synthetic",
            account_id="account.synthetic",
        )

    async def run() -> None:
        original = pydantic_codex._refresh_credentials
        pydantic_codex._refresh_credentials = fake_refresh
        try:
            source = OpenAICodexCredentialSource(path)
            provider = create_pydantic_ai_codex_provider(source)
            await provider._load_if_needed()
            await asyncio.to_thread(ready.wait)
            await provider._refresh_for_401(
                provider._revision,
                refresh_failures=provider._refresh_failures,
            )
            results.put(True)
        except Exception:  # noqa: BLE001 - child reports only a boolean
            results.put(False)
        finally:
            pydantic_codex._refresh_credentials = original

    asyncio.run(run())


@pytest.mark.skipif(
    mp.get_start_method(allow_none=True) not in {None, "fork"},
    reason="POSIX fork probe",
)
def test_two_real_provider_processes_consume_one_synthetic_generation(tmp_path: Path):
    path = tmp_path / "credentials.json"
    asyncio.run(OpenAICodexCredentialSource(path).save(_credentials("old")))
    context = mp.get_context("fork")
    calls = context.Value("i", 0)
    calls_lock = context.Lock()
    ready = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_provider_process_worker,
            args=(str(path), calls, calls_lock, ready, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
    assert all(process.exitcode == 0 for process in processes)
    assert [results.get(timeout=1) for _ in processes] == [True, True]
    assert calls.value == 1
    assert asyncio.run(OpenAICodexCredentialSource(path).load()) == _credentials("new")


def test_wrong_pydantic_ai_version_fails_closed(monkeypatch):
    import software_factory.providers.pydantic_ai_codex as integration

    monkeypatch.setattr(integration, "_installed_version", lambda: "2.48.1")
    with pytest.raises(PydanticAIIntegrationError):
        integration.get_pydantic_ai_codex_provider_class()
