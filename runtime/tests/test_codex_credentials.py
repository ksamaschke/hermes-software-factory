"""Synthetic security and concurrency coverage for the Codex provider boundary."""

from __future__ import annotations

import asyncio
import inspect
import json
import multiprocessing as mp
import os
import stat
import time
from pathlib import Path

import pytest
from software_factory.providers.openai_codex import (
    CredentialCorruptionError,
    CredentialLockTimeoutError,
    CredentialPermissionError,
    CredentialPersistenceError,
    CredentialValidationError,
    FileCodexCredentialBackend,
    OpenAICodexCredentials,
    OpenAICodexCredentialSource,
    PressureAdmissionTimeoutError,
    ProviderOperationError,
    ProviderPressure,
)


def run(awaitable):
    return asyncio.run(awaitable)


def credentials(generation: str = "one") -> OpenAICodexCredentials:
    return OpenAICodexCredentials(
        access_token=f"access-{generation}.synthetic",
        refresh_token=f"refresh-{generation}.synthetic",
        account_id="account.synthetic",
    )


def write_payload(path: Path, payload: object, *, mode: int = 0o600) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)


def test_credentials_and_errors_have_redacted_representations():
    value = credentials()
    representations = (repr(value), str(value), f"{value}", repr([value]))
    assert not any(
        getattr(value, field) in representation
        for field in ("access_token", "refresh_token", "account_id")
        for representation in representations
    )

    with pytest.raises(CredentialValidationError) as error:
        OpenAICodexCredentials(
            access_token="", refresh_token="unused", account_id="unused"
        )
    assert str(error.value) == "schema error code=invalid_access_token"


def test_pydantic_ai_source_shape_is_async_and_application_owned(tmp_path: Path):
    source = OpenAICodexCredentialSource(tmp_path / "application-credentials.json")
    assert inspect.iscoroutinefunction(source.load)
    assert inspect.iscoroutinefunction(source.save)
    assert source.path is not None
    assert ".codex" not in str(source.path)
    assert source.path.name == "application-credentials.json"


def test_atomic_save_rotation_and_strict_mode(tmp_path: Path):
    path = tmp_path / "credentials.json"
    source = OpenAICodexCredentialSource(path)
    first = credentials("first")
    second = credentials("second")

    run(source.save(first))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert run(source.load()) == first

    run(source.save(second))
    assert run(source.load()) == second
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {
        "access_token",
        "refresh_token",
        "account_id",
    }
    assert not list(tmp_path.glob(".credentials.json.*.tmp"))


def test_refresh_reloads_inside_lock_and_persists_both_rotated_tokens(tmp_path: Path):
    source = OpenAICodexCredentialSource(tmp_path / "credentials.json")
    old = credentials("old")
    new = credentials("new")
    run(source.save(old))
    calls = 0

    async def rotate(current: OpenAICodexCredentials) -> OpenAICodexCredentials:
        nonlocal calls
        calls += 1
        assert current == old
        return new

    assert run(source.refresh(rotate, expected=old)) == new
    assert run(source.load()) == new
    assert calls == 1

    # A stale waiter re-reads the fresh generation and does not spend the
    # single-use refresh token a second time.
    assert run(source.refresh(rotate, expected=old)) == new
    assert calls == 1


def test_stale_save_cannot_overwrite_a_fresh_generation(tmp_path: Path):
    path = tmp_path / "credentials.json"
    first = credentials("first")
    second = credentials("second")
    stale_writer = FileCodexCredentialBackend(path)
    fresh_writer = FileCodexCredentialBackend(path)
    run(stale_writer.save(first))
    assert run(stale_writer.load()) == first
    run(fresh_writer.save(second))

    with pytest.raises(CredentialPersistenceError):
        run(stale_writer.save(credentials("third")))
    assert run(fresh_writer.load()) == second


def _cross_process_refresh_worker(
    path: str, counter, counter_lock, result_queue
) -> None:
    source = FileCodexCredentialBackend(path, lock_timeout=2.0)
    old = credentials("old")
    new = credentials("new")

    def rotate(current: OpenAICodexCredentials) -> OpenAICodexCredentials:
        assert current == old
        with counter_lock:
            counter.value += 1
        time.sleep(0.08)
        return new

    try:
        result = source.refresh_sync(rotate, expected=old, timeout=2.0)
        result_queue.put(result == new)
    except Exception:  # noqa: BLE001 - child reports only a safe boolean
        result_queue.put(False)


@pytest.mark.skipif(os.name != "posix", reason="POSIX flock is the production target")
def test_cross_process_refresh_has_one_rotator_and_fresh_waiter(tmp_path: Path):
    path = tmp_path / "credentials.json"
    source = FileCodexCredentialBackend(path, lock_timeout=2.0)
    run(source.save(credentials("old")))
    context = mp.get_context("fork")
    counter = context.Value("i", 0)
    counter_lock = context.Lock()
    results = context.Queue()
    processes = [
        context.Process(
            target=_cross_process_refresh_worker,
            args=(str(path), counter, counter_lock, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(3)
    assert all(process.exitcode == 0 for process in processes)
    assert [results.get(timeout=1) for _ in processes] == [True, True]
    assert counter.value == 1
    assert run(source.load()) == credentials("new")


def test_lock_timeout_is_bounded_and_cancellation_releases_lock(tmp_path: Path):
    source = OpenAICodexCredentialSource(
        tmp_path / "credentials.json", lock_timeout=0.05
    )
    run(source.save(credentials("old")))

    async def probe() -> None:
        async with source.refresh_lock():
            with pytest.raises(CredentialLockTimeoutError):
                await source.refresh(
                    lambda _current: credentials("new"),
                    expected=credentials("old"),
                    timeout=0.02,
                )

    run(probe())
    run(source.save(credentials("new")))
    assert run(source.load()) == credentials("new")


def test_corruption_is_not_discarded_and_recovery_is_explicit(tmp_path: Path):
    path = tmp_path / "credentials.json"
    source = OpenAICodexCredentialSource(path)
    first = credentials("first")
    replacement = credentials("replacement")
    run(source.save(first))
    path.write_bytes(b"{partial")
    path.chmod(0o600)

    with pytest.raises(CredentialCorruptionError):
        run(source.load())
    with pytest.raises(CredentialCorruptionError):
        run(source.save(replacement))
    assert path.read_bytes() == b"{partial"

    run(source.recover(replacement))
    assert run(source.load()) == replacement


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "a", "refresh_token": "r"},
        {
            "access_token": "a",
            "refresh_token": "r",
            "account_id": "i",
            "unexpected": "field",
        },
        {
            "tokens": {
                "access_token": "a",
                "refresh_token": "r",
                "account_id": "i",
            }
        },
        {"access_token": 1, "refresh_token": "r", "account_id": "i"},
        {"access_token": "a", "refresh_token": "r", "account_id": "i", "x": None},
    ],
)
def test_wrong_schema_and_unexpected_fields_fail_closed(
    tmp_path: Path, payload: object
):
    path = tmp_path / "credentials.json"
    source = OpenAICodexCredentialSource(path)
    write_payload(path, payload)
    with pytest.raises(CredentialCorruptionError):
        run(source.load())


def test_duplicate_json_fields_fail_closed(tmp_path: Path):
    path = tmp_path / "credentials.json"
    path.write_text(
        '{"access_token":"a","access_token":"b","refresh_token":"r","account_id":"i"}',
        encoding="utf-8",
    )
    path.chmod(0o600)
    with pytest.raises(CredentialCorruptionError):
        run(OpenAICodexCredentialSource(path).load())


def test_insecure_file_mode_requires_explicit_recovery(tmp_path: Path):
    path = tmp_path / "credentials.json"
    source = OpenAICodexCredentialSource(path)
    run(source.save(credentials("old")))
    path.chmod(0o640)
    with pytest.raises(CredentialPermissionError):
        run(source.load())
    with pytest.raises(CredentialPermissionError):
        run(source.save(credentials("new")))
    run(source.recover(credentials("new")))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert run(source.load()) == credentials("new")


def test_symlink_fifo_and_symlink_parent_are_rejected(tmp_path: Path):
    target = tmp_path / "outside.json"
    target.write_text("not-a-credential", encoding="utf-8")
    link = tmp_path / "credentials.json"
    link.symlink_to(target)
    source = OpenAICodexCredentialSource(link)
    with pytest.raises(CredentialPermissionError):
        run(source.load())
    with pytest.raises(CredentialPermissionError):
        run(source.recover(credentials("new")))
    assert target.read_text(encoding="utf-8") == "not-a-credential"

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(CredentialPermissionError):
        run(OpenAICodexCredentialSource(fifo).load())

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(CredentialPermissionError):
        run(
            OpenAICodexCredentialSource(parent_link / "credentials.json").save(
                credentials("new")
            )
        )


def test_group_or_world_writable_parent_is_rejected(tmp_path: Path):
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir()
    unsafe_parent.chmod(0o777)
    try:
        with pytest.raises(CredentialPermissionError):
            run(
                OpenAICodexCredentialSource(unsafe_parent / "credentials.json").save(
                    credentials("new")
                )
            )
    finally:
        unsafe_parent.chmod(0o700)


def test_partial_temp_file_does_not_replace_published_generation(tmp_path: Path):
    path = tmp_path / "credentials.json"
    source = OpenAICodexCredentialSource(path)
    run(source.save(credentials("old")))
    interrupted = tmp_path / ".credentials.json.interrupted.tmp"
    interrupted.write_bytes(b"partial")
    interrupted.chmod(0o600)

    assert run(source.load()) == credentials("old")
    run(source.save(credentials("new")))
    assert run(source.load()) == credentials("new")
    assert interrupted.read_bytes() == b"partial"


def _crash_during_atomic_save(path: str, phase: str) -> None:
    import software_factory.providers.openai_codex as module

    backend = module.FileCodexCredentialBackend(path, lock_timeout=2.0)
    replacement = credentials("new")
    if phase == "before_replace":
        original_fsync = module.os.fsync
        calls = 0

        def crash_after_first_fsync(descriptor: int) -> None:
            nonlocal calls
            original_fsync(descriptor)
            calls += 1
            if calls == 1:
                os._exit(0)

        module.os.fsync = crash_after_first_fsync
    else:
        original_replace = module.os.replace

        def replace_then_crash(source: str, destination: str) -> None:
            original_replace(source, destination)
            os._exit(0)

        module.os.replace = replace_then_crash
    backend.save_sync(replacement)


@pytest.mark.skipif(os.name != "posix", reason="POSIX crash probe")
@pytest.mark.parametrize("phase", ["before_replace", "after_replace"])
def test_crash_window_leaves_old_or_new_complete_file(tmp_path: Path, phase: str):
    path = tmp_path / "credentials.json"
    source = FileCodexCredentialBackend(path, lock_timeout=2.0)
    run(source.save(credentials("old")))
    context = mp.get_context("fork")
    process = context.Process(target=_crash_during_atomic_save, args=(str(path), phase))
    process.start()
    process.join(3)
    assert process.exitcode == 0
    observed = run(source.load())
    assert observed in {credentials("old"), credentials("new")}
    if phase == "before_replace":
        assert observed == credentials("old")
    else:
        assert observed == credentials("new")


async def _pressure_capacity_probe() -> None:
    pressure = ProviderPressure(1, admission_timeout=0.2)
    entered = asyncio.Event()
    release = asyncio.Event()
    max_active = 0
    active = 0

    async def operation() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        entered.set()
        await release.wait()
        active -= 1

    first = asyncio.create_task(pressure.run("generation-one", operation))
    await entered.wait()
    second = asyncio.create_task(pressure.run("generation-two", operation))
    await asyncio.sleep(0.02)
    assert pressure.queued == 1
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert max_active == 1
    assert pressure.in_flight == 0


def test_provider_capacity_queues_without_duplicate_concurrency():
    run(_pressure_capacity_probe())


def test_rate_limit_backoff_is_bounded_and_updates_admission_pressure():
    async def probe() -> None:
        pressure = ProviderPressure(1, admission_timeout=0.03, max_backoff=0.5)
        pressure.observe_rate_limit(0.5, generation="retry-one")
        assert pressure.pressure_state()["queued"] == 0
        with pytest.raises(PressureAdmissionTimeoutError):
            await pressure.acquire(timeout=0.02)
        pressure.clear_pressure(generation="retry-one")
        async with pressure.slot(timeout=0.1):
            assert pressure.in_flight == 1

    run(probe())


def test_provider_single_flight_deduplicates_one_logical_retry_generation():
    async def probe() -> None:
        pressure = ProviderPressure(1, admission_timeout=0.2)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "safe-result"

        tasks = [
            asyncio.create_task(pressure.run("retry-generation", operation))
            for _ in range(5)
        ]
        await started.wait()
        await asyncio.sleep(0.01)
        assert calls == 1
        release.set()
        assert await asyncio.gather(*tasks) == ["safe-result"] * 5
        assert calls == 1

    run(probe())


def test_provider_operation_failures_are_redacted_for_shared_waiters():
    async def probe() -> None:
        pressure = ProviderPressure(1, admission_timeout=0.2)

        async def operation() -> None:
            raise RuntimeError("access-one.synthetic refresh-one.synthetic")

        with pytest.raises(ProviderOperationError) as error:
            await pressure.run("failure-generation", operation)
        assert "synthetic" not in str(error.value)

    run(probe())


class MemoryBackend:
    """Synthetic backend showing the future secret-store substitution seam."""

    def __init__(self, value: OpenAICodexCredentials):
        self.value = value
        self.saves = 0

    async def load(self) -> OpenAICodexCredentials:
        return self.value

    async def save(self, value: OpenAICodexCredentials) -> None:
        self.value = value
        self.saves += 1

    async def refresh(self, callback, *, expected=None, timeout=None):
        del timeout
        current = await self.load()
        if expected is not None and current != expected:
            return current
        result = callback(current)
        if inspect.isawaitable(result):
            result = await result
        await self.save(result)
        return result


def test_secret_store_backend_protocol_keeps_source_api_stable():
    backend = MemoryBackend(credentials("old"))
    source = OpenAICodexCredentialSource(backend=backend)
    new = credentials("new")
    assert run(source.load()) == credentials("old")
    assert run(source.refresh(lambda _current: new, expected=credentials("old"))) == new
    assert run(source.save(new)) is None
    assert backend.saves == 2
    assert run(source.load()) == new
