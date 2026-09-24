"""Optional exact-version PydanticAI integration regressions.

The module is skipped in the default runtime environment.  From ``runtime/``,
the focused proof runs with ``uv run --extra pydantic-ai --with pytest>=8
pytest tests/test_pydantic_ai_codex_integration.py -q`` and uses only synthetic
credentials plus an in-memory HTTP transport.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
from functools import wraps
from pathlib import Path

import pytest

pydantic_ai = pytest.importorskip("pydantic_ai")
httpx2 = pytest.importorskip("httpx2")

import pydantic_ai.providers.openai_codex as pydantic_codex
from software_factory.providers import (
    CredentialCleanupError,
    CredentialPersistenceError,
    CredentialRefreshError,
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


def _asyncio_test(test):
    """Run an async regression without an undeclared pytest plugin."""

    @wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


def test_guarded_class_is_the_pinned_real_provider():
    provider_class = get_pydantic_ai_codex_provider_class()
    assert issubclass(provider_class, pydantic_codex.OpenAICodexProvider)
    assert getattr(pydantic_ai, "__version__", "2.48.0") == "2.48.0"


def test_same_signature_refresh_mutation_fails_closed(monkeypatch):
    async def replacement(credentials, *, http_client=None):
        del http_client
        return credentials

    monkeypatch.setattr(pydantic_codex, "_refresh_credentials", replacement)
    with pytest.raises(PydanticAIIntegrationError) as raised:
        get_pydantic_ai_codex_provider_class()
    assert raised.value.code == "pydantic_ai_reviewed_seam_changed"


@_asyncio_test
async def test_persistence_failure_uses_the_guarded_upstream_exception(
    monkeypatch, tmp_path: Path
):
    current = _credentials("old")
    source = OpenAICodexCredentialSource(tmp_path / "credentials.json")
    await source.save(current)

    async def failing_refresh(
        _backend,
        _callback,
        *,
        expected=None,
        timeout=None,
    ):
        del expected, timeout
        raise CredentialPersistenceError("synthetic_backend_failure")

    monkeypatch.setattr(
        "software_factory.providers.openai_codex.FileCodexCredentialBackend.refresh",
        failing_refresh,
    )
    provider = create_pydantic_ai_codex_provider(source)
    await provider._load_if_needed()
    monkeypatch.setattr(provider, "_is_stale", lambda: True)
    with pytest.raises(pydantic_codex.CredentialsPersistenceError) as raised:
        await provider._refresh_if_stale()
    assert str(raised.value) == (
        "Application-owned credential persistence failed after refresh."
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@_asyncio_test
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


@_asyncio_test
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


@_asyncio_test
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
    async def handler(request):
        if "token" not in request.url.path:
            return httpx2.Response(500, json={"error": "unexpected_request"})
        with calls_lock:
            calls.value += 1
        await asyncio.sleep(0.05)
        return httpx2.Response(
            200,
            json={
                "access_token": "access-new.synthetic",
                "refresh_token": "refresh-new.synthetic",
                "account_id": "account.synthetic",
            },
        )

    async def run() -> None:
        source = OpenAICodexCredentialSource(path)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        provider = create_pydantic_ai_codex_provider(source, http_client=client)
        try:
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
            await client.aclose()

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


@_asyncio_test
async def test_guard_failure_aborts_proactive_and_direct_refresh_without_network(
    monkeypatch, tmp_path: Path
):
    import software_factory.providers.pydantic_ai_codex as integration

    source = OpenAICodexCredentialSource(tmp_path / "credentials.json")
    await source.save(_credentials("old"))
    network_calls = 0

    async def handler(_request):
        nonlocal network_calls
        network_calls += 1
        return httpx2.Response(500, json={"error": "unexpected"})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    provider = create_pydantic_ai_codex_provider(
        source, pressure=ProviderPressure(1), http_client=client
    )
    try:
        await provider._load_if_needed()
        revision = provider._revision

        def broken_guard():
            raise RuntimeError("guard failure")

        monkeypatch.setattr(integration, "_load_pinned_provider_module", broken_guard)
        monkeypatch.setattr(provider, "_is_stale", lambda: True)
        with pytest.raises(CredentialRefreshError) as proactive:
            await provider._refresh_if_stale()
        assert proactive.value.code == "reviewed_seam_changed"
        assert provider._revision == revision
        assert network_calls == 0

        with pytest.raises(CredentialRefreshError) as direct:
            await provider._refresh_for_401(
                revision, refresh_failures=provider._refresh_failures
            )
        assert direct.value.code == "reviewed_seam_changed"
        assert direct.value.__context__ is None
        assert provider._revision == revision
        assert network_calls == 0
    finally:
        monkeypatch.undo()
        await provider.close()
        await client.aclose()


@_asyncio_test
async def test_cleanup_failure_is_exact_categorical_and_not_retried(
    monkeypatch, tmp_path: Path
):
    import software_factory.providers.openai_codex as codex

    path = tmp_path / "credentials.json"
    source = OpenAICodexCredentialSource(path)
    await source.save(_credentials("old"))
    source_two = OpenAICodexCredentialSource(path)
    network_calls = 0

    async def handler(_request):
        nonlocal network_calls
        network_calls += 1
        return httpx2.Response(
            200,
            json={
                "access_token": "access-new.synthetic",
                "refresh_token": "refresh-new.synthetic",
                "account_id": "account.synthetic",
            },
        )

    def fail_save(_backend, _parent_fd, _value):
        raise CredentialCleanupError("synthetic_cleanup")

    monkeypatch.setattr(codex.FileCodexCredentialBackend, "_save_unlocked", fail_save)
    clients = [
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ]
    pressure = ProviderPressure(2)
    provider = create_pydantic_ai_codex_provider(
        source, pressure=pressure, http_client=clients[0]
    )
    provider_two = create_pydantic_ai_codex_provider(
        source_two, pressure=pressure, http_client=clients[1]
    )
    try:
        await asyncio.gather(provider._load_if_needed(), provider_two._load_if_needed())
        revision = provider._revision
        results = await asyncio.gather(
            provider._refresh_for_401(revision, refresh_failures=0),
            provider._refresh_for_401(revision, refresh_failures=0),
            return_exceptions=True,
        )
        assert all(isinstance(result, CredentialCleanupError) for result in results)
        assert all(result.__context__ is None for result in results)
        assert provider._last_refresh_error == (revision, "credential_cleanup")
        assert network_calls == 1
        assert await source.load() == _credentials("old")

        await provider_two._load_if_needed()
        provider_two._is_stale = lambda: True
        with pytest.raises(CredentialCleanupError) as proactive:
            await provider_two._refresh_if_stale()
        assert proactive.value.__context__ is None
        assert provider_two._revision == 0
        assert await source.load() == _credentials("old")
    finally:
        await provider.close()
        await provider_two.close()
        await asyncio.gather(*(client.aclose() for client in clients))


@_asyncio_test
async def test_context_delegates_owned_and_external_http_client_lifecycle(
    tmp_path: Path,
):
    owned_source = OpenAICodexCredentialSource(tmp_path / "owned.json")
    external_source = OpenAICodexCredentialSource(tmp_path / "external.json")
    await owned_source.save(_credentials("owned"))
    await external_source.save(_credentials("external"))

    owned_provider = create_pydantic_ai_codex_provider(owned_source)
    owned_client = owned_provider._http_client
    async with owned_provider as entered:
        assert entered is owned_provider
        assert owned_client.is_closed is False
    assert owned_client.is_closed is True
    assert owned_provider._auth is None
    assert owned_provider._http_client is None
    assert owned_provider._client is None
    assert owned_provider._credentials is None

    external_client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(500, json={"error": "unused"})
        )
    )
    external_provider = create_pydantic_ai_codex_provider(
        external_source, http_client=external_client
    )
    try:
        async with external_provider:
            assert external_client.is_closed is False
        assert external_client.is_closed is False
        assert external_client.auth is None
        assert external_provider._auth is None
        assert external_provider._http_client is None
        assert external_provider._client is None
        assert external_provider._credentials is None
    finally:
        await external_client.aclose()


@_asyncio_test
async def test_partial_context_enter_drains_owned_client_and_scope(
    monkeypatch, tmp_path: Path
):
    from pydantic_ai.providers import Provider

    source = OpenAICodexCredentialSource(tmp_path / "partial.json")
    await source.save(_credentials("partial"))
    provider = create_pydantic_ai_codex_provider(source)
    client = provider._http_client

    async def fail_enter(_provider):
        raise RuntimeError("synthetic partial enter")

    monkeypatch.setattr(Provider, "__aenter__", fail_enter)
    with pytest.raises(RuntimeError, match="synthetic partial enter"):
        await provider.__aenter__()
    assert client.is_closed is True
    assert provider._auth is None
    assert provider._http_client is None
    assert provider._client is None
    assert provider._credentials is None


@_asyncio_test
async def test_context_cancellation_drains_owned_client_and_scope(tmp_path: Path):
    source = OpenAICodexCredentialSource(tmp_path / "cancelled.json")
    await source.save(_credentials("cancelled"))
    pressure = ProviderPressure(1)
    provider = create_pydantic_ai_codex_provider(source, pressure=pressure)
    client = provider._http_client
    entered = asyncio.Event()

    async def use_provider():
        async with provider:
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(use_provider())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.is_closed is True
    assert provider._auth is None
    assert provider._http_client is None
    assert provider._client is None
    assert provider._credentials is None
    assert pressure._scopes == {0}
    assert pressure._pending_admissions == {}
    assert pressure._active == {}
    await provider.close()
