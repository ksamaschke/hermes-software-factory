"""Synthetic security and concurrency coverage for the Codex provider boundary."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import inspect
import json
import multiprocessing as mp
import os
import pickle
import stat
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
from software_factory.providers.openai_codex import (
    CredentialCleanupError,
    CredentialCorruptionError,
    CredentialLockError,
    CredentialLockTimeoutError,
    CredentialPermissionError,
    CredentialPersistenceError,
    CredentialRefreshError,
    CredentialValidationError,
    FileCodexCredentialBackend,
    OpenAICodexCredentials,
    OpenAICodexCredentialSource,
    PressureAdmissionTimeoutError,
    ProviderCompletion,
    ProviderGeneration,
    ProviderLoopError,
    ProviderOperationError,
    ProviderPressure,
    ProviderQueueFullError,
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

    async def rotate(current: OpenAICodexCredentials) -> OpenAICodexCredentials:
        assert current == old
        with counter_lock:
            counter.value += 1
        await asyncio.sleep(0.08)
        return new

    try:
        result = asyncio.run(source.refresh(rotate, expected=old, timeout=2.0))
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

                async def callback(_current: OpenAICodexCredentials):
                    return credentials("new")

                await source.refresh(
                    callback,
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
        original_replace = module.os.rename

        def replace_then_crash(source: str, destination: str, **kwargs) -> None:
            original_replace(source, destination, **kwargs)
            os._exit(0)

        module.os.rename = replace_then_crash
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

    first = asyncio.create_task(pressure.run(1, operation))
    await entered.wait()
    second = asyncio.create_task(pressure.run(2, operation))
    await asyncio.sleep(0.02)
    assert pressure.queued == 1
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert max_active == 1
    assert pressure.in_flight == 0


def test_provider_capacity_queues_without_duplicate_concurrency():
    run(_pressure_capacity_probe())


def test_provider_admission_tokens_survive_forced_id_reuse_and_churn(monkeypatch):
    import software_factory.providers.openai_codex as module

    async def probe() -> None:
        pressure = ProviderPressure(2, admission_timeout=0.5)
        monkeypatch.setattr(module, "id", lambda _value: 7, raising=False)

        first = await pressure.acquire()
        second = await pressure.acquire()
        assert pressure.in_flight == 2
        first.release()
        first.release()
        assert pressure.in_flight == 1
        second.release()
        second.release()
        assert pressure.in_flight == 0

        for _ in range(128):
            leases = [await pressure.acquire(), await pressure.acquire()]
            leases[0].release()
            leases[1].release()
        assert pressure.in_flight == 0
        assert pressure.queued == 0
        assert pressure._leases == {}

    run(probe())


def test_provider_admission_token_overflow_fails_closed():
    import software_factory.providers.openai_codex as module

    async def probe() -> None:
        pressure = ProviderPressure(1, admission_timeout=0.1)
        pressure._next_lease_token = module._MAX_LEASE_TOKEN + 1
        with pytest.raises(ProviderOperationError) as raised:
            await pressure.acquire()
        assert raised.value.code == "lease_token_exhausted"
        assert pressure.in_flight == 0
        assert pressure.queued == 0
        assert pressure._leases == {}

    run(probe())


def test_scope_release_rejects_pending_admission_and_reuse_has_no_stale_entry():
    async def probe() -> None:
        pressure = ProviderPressure(1, admission_timeout=0.5)
        scope = pressure.new_scope()
        generation = ProviderGeneration(scope, 1)
        pressure._bind_loop()
        assert pressure._state_lock is not None
        await pressure._state_lock.acquire()
        pending = asyncio.create_task(
            pressure.acquire(generation=generation, timeout=0.5)
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if pressure._pending_admissions.get(scope) == 1:
                break
        assert pressure._pending_admissions.get(scope) == 1
        with pytest.raises(ProviderOperationError) as raised:
            pressure.release_scope(scope)
        assert raised.value.code == "generation_scope_active"
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert pressure._pending_admissions == {}
        pressure._state_lock.release()
        pressure.release_scope(scope)
        assert pressure.new_scope() == scope

        held = await pressure.acquire(generation=ProviderGeneration(scope, 2))
        waiter = asyncio.create_task(
            pressure.acquire(generation=ProviderGeneration(scope, 3), timeout=0.5)
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if pressure._pending_admissions.get(scope) == 1:
                break
        assert pressure.queued == 1
        with pytest.raises(ProviderOperationError):
            pressure.release_scope(scope)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        held.release()
        pressure.release_scope(scope)
        assert pressure._pending_admissions == {}
        assert pressure._leases == {}
        assert pressure.queued == 0
        assert pressure.in_flight == 0

    run(probe())


def test_rate_limit_backoff_is_bounded_and_updates_admission_pressure():
    async def probe() -> None:
        pressure = ProviderPressure(1, admission_timeout=0.03, max_backoff=0.5)
        pressure.observe_rate_limit(0.5, generation=1)
        assert pressure.pressure_state()["queued"] == 0
        with pytest.raises(PressureAdmissionTimeoutError):
            await pressure.acquire(timeout=0.02)
        pressure.clear_pressure(generation=1)
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

        tasks = [asyncio.create_task(pressure.run(1, operation)) for _ in range(5)]
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
            await pressure.run(1, operation)
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

    async def rotate(_current: OpenAICodexCredentials) -> OpenAICodexCredentials:
        return new

    assert run(source.load()) == credentials("old")
    assert run(source.refresh(rotate, expected=credentials("old"))) == new
    assert run(source.save(new)) is None
    assert backend.saves == 2
    assert run(source.load()) == new


def test_credential_serialization_and_caller_paths_fail_closed(tmp_path: Path):
    marker = "secret-marker.synthetic"
    value = OpenAICodexCredentials(
        access_token=f"access-{marker}",
        refresh_token=f"refresh-{marker}",
        account_id=f"account-{marker}",
    )
    source = OpenAICodexCredentialSource(tmp_path / f"{marker}.json")
    representations = (
        repr(value),
        str(value),
        format(value),
        repr([value]),
        repr(source),
    )
    assert all(marker not in representation for representation in representations)
    assert not hasattr(value, "as_mapping")
    with pytest.raises(TypeError):
        pickle.dumps(value)
    with pytest.raises(TypeError):
        dataclasses.asdict(value)
    with pytest.raises(TypeError):
        dict(value)
    error = CredentialValidationError(path=tmp_path / marker)
    assert marker not in str(error)
    assert marker not in repr(error)


def test_callback_failure_has_no_secret_bearing_exception_chain(tmp_path: Path):
    async def probe() -> None:
        source = OpenAICodexCredentialSource(tmp_path / "credentials.json")
        current = credentials("old")
        await source.save(current)

        async def callback(_current: OpenAICodexCredentials):
            raise RuntimeError("refresh-secret-marker.synthetic")

        with pytest.raises(CredentialRefreshError) as raised:
            await source.refresh(callback, expected=current, timeout=1.0)
        assert raised.value.__context__ is None
        assert raised.value.__cause__ is None
        assert "synthetic" not in str(raised.value)
        assert "synthetic" not in repr(raised.value)

    run(probe())


def test_hung_async_callback_times_out_and_releases_process_lock(tmp_path: Path):
    async def probe() -> None:
        source = OpenAICodexCredentialSource(tmp_path / "credentials.json")
        current = credentials("old")
        replacement = credentials("new")
        await source.save(current)
        callback_started = asyncio.Event()
        callback_never_finishes = asyncio.Event()

        async def callback(_current: OpenAICodexCredentials):
            callback_started.set()
            await callback_never_finishes.wait()
            return replacement

        task = asyncio.create_task(
            source.refresh(callback, expected=current, timeout=0.05)
        )
        await callback_started.wait()
        with pytest.raises(CredentialRefreshError) as raised:
            await task
        assert raised.value.code in {"refresh_callback_timeout", "refresh_timeout"}
        await source.save(replacement)
        assert await source.load() == replacement

    run(probe())


def test_sync_callback_is_rejected_before_invocation(tmp_path: Path):
    async def probe() -> None:
        source = OpenAICodexCredentialSource(tmp_path / "credentials.json")
        current = credentials("old")
        await source.save(current)
        called = False

        def callback(_current: OpenAICodexCredentials):
            nonlocal called
            called = True
            return credentials("new")

        with pytest.raises(CredentialRefreshError) as raised:
            await source.refresh(callback, expected=current, timeout=0.03)
        assert raised.value.code == "refresh_callback_async_required"
        assert not called
        assert await source.load() == current

    run(probe())


def test_sync_refresh_entrypoint_rejects_before_lock_or_invocation(tmp_path: Path):
    backend = FileCodexCredentialBackend(tmp_path / "credentials.json")
    current = credentials("old")
    backend.save_sync(current)
    called = False

    def callback(_current: OpenAICodexCredentials):
        nonlocal called
        called = True
        return credentials("new")

    with pytest.raises(CredentialRefreshError) as raised:
        backend.refresh_sync(cast(Any, callback), expected=current, timeout=0.03)
    assert raised.value.code == "refresh_callback_async_required"
    assert not called
    assert backend.load_sync() == current


def test_descriptor_anchor_does_not_follow_parent_replacement(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    parent.chmod(0o700)
    path = parent / "credentials.json"
    backend = FileCodexCredentialBackend(path)
    moved = tmp_path / "moved-parent"
    os.rename(parent, moved)
    redirected = tmp_path / "redirected-parent"
    redirected.mkdir()
    parent.symlink_to(redirected, target_is_directory=True)
    with pytest.raises(CredentialPermissionError):
        backend.save_sync(credentials("anchored"))
    assert not (redirected / "credentials.json").exists()
    assert not (moved / "credentials.json").exists()
    with pytest.raises(CredentialPermissionError):
        backend.load_sync()


def test_descriptor_anchor_rejects_parent_metadata_change(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    parent.chmod(0o700)
    backend = FileCodexCredentialBackend(parent / "credentials.json")
    parent.chmod(0o750)
    try:
        with pytest.raises(CredentialPermissionError):
            backend.save_sync(credentials("new"))
    finally:
        parent.chmod(0o700)


def test_hardlink_retention_is_observed_before_publishing(tmp_path: Path, monkeypatch):
    import software_factory.providers.openai_codex as module

    path = tmp_path / "credentials.json"
    backend = FileCodexCredentialBackend(path)
    backend.save_sync(credentials("old"))
    retained = tmp_path / "retained-hardlink"
    original_fsync = module.os.fsync

    def link_during_temp_sync(descriptor: int) -> None:
        original_fsync(descriptor)
        temporary = next(tmp_path.glob(".credentials.json.*.tmp"))
        os.link(temporary, retained)

    monkeypatch.setattr(module.os, "fsync", link_during_temp_sync)
    with pytest.raises(CredentialPermissionError):
        backend.save_sync(credentials("new"))
    retained.unlink()
    assert backend.load_sync() == credentials("old")


def test_post_publication_hardlink_is_scrubbed_before_ftruncate_failure(
    tmp_path: Path, monkeypatch
):
    import software_factory.providers.openai_codex as module

    path = tmp_path / "credentials.json"
    retained = tmp_path / "retained-published-hardlink"
    backend = FileCodexCredentialBackend(path)
    backend.save_sync(credentials("old"))
    original_rename = module.os.rename

    def publish_and_retain(source: str, destination: str, **kwargs: Any) -> None:
        original_rename(source, destination, **kwargs)
        os.link(path, retained)

    monkeypatch.setattr(module.os, "rename", publish_and_retain)

    def fail_truncate(_descriptor: int, _size: int) -> None:
        raise OSError(errno.EIO, "ftruncate failure")

    monkeypatch.setattr(module.os, "ftruncate", fail_truncate)
    with pytest.raises(CredentialCleanupError):
        backend.save_sync(credentials("new"))

    observed = retained.read_bytes()
    assert observed
    assert not any(observed)
    retained.unlink()


def test_post_publication_hardlink_is_scrubbed_before_fsync_failure(
    tmp_path: Path, monkeypatch
):
    import software_factory.providers.openai_codex as module

    path = tmp_path / "credentials.json"
    retained = tmp_path / "retained-published-hardlink"
    backend = FileCodexCredentialBackend(path)
    backend.save_sync(credentials("old"))
    original_rename = module.os.rename
    published = False

    def publish_and_retain(source: str, destination: str, **kwargs: Any) -> None:
        nonlocal published
        original_rename(source, destination, **kwargs)
        os.link(path, retained)
        published = True

    def fail_after_publication(_descriptor: int) -> None:
        if published:
            raise OSError(errno.EIO, "fsync failure")

    monkeypatch.setattr(module.os, "rename", publish_and_retain)
    monkeypatch.setattr(module.os, "fsync", fail_after_publication)
    with pytest.raises(CredentialCleanupError):
        backend.save_sync(credentials("new"))

    assert not any(retained.read_bytes())
    retained.unlink()


def test_post_publication_scrub_retries_partial_writes_and_eintr(
    tmp_path: Path, monkeypatch
):
    import software_factory.providers.openai_codex as module

    path = tmp_path / "credentials.json"
    retained = tmp_path / "retained-published-hardlink"
    backend = FileCodexCredentialBackend(path)
    backend.save_sync(credentials("old"))
    original_rename = module.os.rename
    original_write = module.os.write
    published = False
    interrupted = False
    shortened = False

    def publish_and_retain(source: str, destination: str, **kwargs: Any) -> None:
        nonlocal published
        original_rename(source, destination, **kwargs)
        os.link(path, retained)
        published = True

    def partial_and_interrupted(descriptor: int, data: bytes) -> int:
        nonlocal interrupted, shortened
        if published and not interrupted:
            interrupted = True
            raise OSError(errno.EINTR, "interrupted scrub write")
        if published and not shortened:
            shortened = True
            return original_write(descriptor, data[:1])
        return original_write(descriptor, data)

    monkeypatch.setattr(module.os, "rename", publish_and_retain)
    monkeypatch.setattr(module.os, "write", partial_and_interrupted)
    with pytest.raises(CredentialPermissionError):
        backend.save_sync(credentials("new"))

    observed = retained.read_bytes()
    assert interrupted
    assert shortened
    assert not any(observed)
    retained.unlink()


def test_backend_io_failures_are_rethrown_without_exception_chains(
    tmp_path, monkeypatch
):
    import software_factory.providers.openai_codex as module

    path = tmp_path / "credentials.json"
    backend = FileCodexCredentialBackend(path)
    backend.save_sync(credentials("old"))
    marker = "filesystem-secret-marker.synthetic"

    def fail_sync(_descriptor: int) -> None:
        raise OSError(marker)

    monkeypatch.setattr(module.os, "fsync", fail_sync)
    with pytest.raises(CredentialPersistenceError) as raised:
        backend.save_sync(credentials("new"))
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert marker not in str(raised.value)
    assert backend.load_sync() == credentials("old")


def test_lock_marker_replacement_fails_before_refresh(tmp_path: Path):
    path = tmp_path / "credentials.json"
    source = OpenAICodexCredentialSource(path)
    run(source.save(credentials("old")))
    marker = Path(f"{path}.lock")
    outside = tmp_path / "outside-lock"
    outside.write_text("not-a-lock", encoding="utf-8")
    marker.symlink_to(outside)

    async def callback(_current: OpenAICodexCredentials):
        return credentials("new")

    with pytest.raises(CredentialLockError):
        run(source.refresh(callback, expected=credentials("old")))
    marker.unlink()
    assert run(source.load()) == credentials("old")


def test_provider_pressure_wakes_clear_immediately_and_bounds_queue():
    async def probe() -> None:
        pressure = ProviderPressure(1, admission_timeout=1.0, max_queue=1)
        pressure.observe_rate_limit(10.0, generation=1)
        waiter = asyncio.create_task(pressure.acquire(timeout=0.5))
        await asyncio.sleep(0.02)
        assert not waiter.done()
        pressure.clear_pressure(generation=1)
        lease = await asyncio.wait_for(waiter, timeout=0.2)
        lease.release()

        held = await pressure.acquire(timeout=0.1)
        queued = asyncio.create_task(pressure.acquire(timeout=0.5))
        await asyncio.sleep(0.02)
        with pytest.raises(ProviderQueueFullError):
            await pressure.acquire(timeout=0.1)
        held.release()
        next_lease = await queued
        next_lease.release()
        assert pressure.in_flight == 0
        assert pressure.queued == 0

    run(probe())


def test_provider_pressure_settles_waiters_on_base_exception_and_deduplicates_retained_generations():
    class SyntheticAbort(BaseException):
        pass

    async def probe() -> None:
        pressure = ProviderPressure(1, admission_timeout=0.5)
        started = asyncio.Event()
        release = asyncio.Event()

        async def aborting_operation():
            started.set()
            await release.wait()
            raise SyntheticAbort()

        owner = asyncio.create_task(pressure.run(1, aborting_operation))
        await started.wait()
        waiter = asyncio.create_task(pressure.run(1, aborting_operation))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(ProviderOperationError):
            await owner
        with pytest.raises(ProviderOperationError):
            await waiter
        assert pressure.in_flight == 0
        assert pressure.queued == 0

        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            return calls

        assert await pressure.run(2, operation) == 1
        assert await pressure.run(3, operation) == 2
        assert isinstance(await pressure.run(2, operation), ProviderCompletion)
        assert calls == 2

    run(probe())


def test_provider_pressure_bounds_operation_timeout_and_enforces_loop_affinity():
    async def timeout_probe() -> None:
        pressure = ProviderPressure(1, admission_timeout=0.5)
        never = asyncio.Event()

        async def hanging_operation():
            await never.wait()

        with pytest.raises(ProviderOperationError) as raised:
            await pressure.run(1, hanging_operation, timeout=0.03)
        assert raised.value.code == "operation_timeout"
        assert pressure.in_flight == 0
        assert pressure.queued == 0
        assert isinstance(
            await pressure.run(1, lambda: "not-called", timeout=0.03),
            ProviderCompletion,
        )

    run(timeout_probe())
    pressure = ProviderPressure()

    async def bind_probe() -> None:
        lease = await pressure.acquire(timeout=0.0)
        lease.release()

    run(bind_probe())
    with pytest.raises(ProviderLoopError) as raised:
        run(bind_probe())
    assert raised.value.__context__ is None


def test_rate_limit_generation_deduplication_is_bounded():
    async def probe() -> None:
        pressure = ProviderPressure(max_retained_generations=2)
        pressure.observe_rate_limit(0.5, generation=1)
        first_deadline = pressure.blocked_until
        pressure.observe_rate_limit(0.5, generation=1)
        assert pressure.blocked_until == first_deadline
        pressure.observe_rate_limit(0.5, generation=2)
        pressure.observe_rate_limit(0.5, generation=3)
        assert pressure._rate_limit_high_water[0] == 3
        deadline = pressure.blocked_until
        pressure.observe_rate_limit(0.5, generation=1)
        assert pressure.blocked_until == deadline

    run(probe())


def test_provider_pressure_owner_cancellation_settles_shared_waiters():
    async def probe() -> None:
        pressure = ProviderPressure(1, admission_timeout=0.5)
        started = asyncio.Event()
        never = asyncio.Event()

        async def operation() -> None:
            started.set()
            await never.wait()

        owner = asyncio.create_task(pressure.run(1, operation))
        await started.wait()
        waiter = asyncio.create_task(pressure.run(1, operation))
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        with pytest.raises(ProviderOperationError) as raised:
            await asyncio.wait_for(waiter, timeout=0.2)
        assert raised.value.code == "operation_cancelled"
        assert raised.value.__context__ is None
        assert pressure.in_flight == 0
        assert pressure.queued == 0

    run(probe())


def test_provider_pressure_scrubs_successful_results_and_rejects_old_replays():
    async def probe() -> None:
        pressure = ProviderPressure(max_retained_generations=1)
        started = asyncio.Event()
        release = asyncio.Event()
        secret = credentials("pressure-secret")
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return secret

        owner = asyncio.create_task(pressure.run(1, operation))
        await started.wait()
        shared = pressure._active[next(iter(pressure._active))]
        future = shared.future
        assert future is not None
        waiter = asyncio.create_task(pressure.run(1, operation))
        await asyncio.sleep(0)
        release.set()
        assert await owner == secret
        assert await waiter == secret
        assert future._result is None  # type: ignore[attr-defined]
        assert future._exception is None  # type: ignore[attr-defined]
        assert getattr(future, "_delivery", None) is None
        assert not pressure._active
        assert all(
            type(value).__name__ == "_CompletionMetadata"
            for value in pressure._completed.values()
        )

        for generation in range(2, 8):
            assert (
                await pressure.run(generation, lambda generation=generation: generation)
                == generation
            )
        replay = await pressure.run(1, operation)
        assert isinstance(replay, ProviderCompletion)
        assert calls == 1

    run(probe())


def test_refresh_deadline_rejects_a_save_that_finishes_late(
    tmp_path: Path, monkeypatch
):
    async def probe() -> None:
        backend = FileCodexCredentialBackend(tmp_path / "credentials.json")
        old = credentials("old")
        new = credentials("new")
        await backend.save(old)
        original = backend._save_unlocked

        def delayed_save(descriptor: int, value: OpenAICodexCredentials) -> None:
            time.sleep(0.05)
            original(descriptor, value)

        monkeypatch.setattr(backend, "_save_unlocked", delayed_save)

        async def callback(_current: OpenAICodexCredentials):
            return new

        with pytest.raises(CredentialRefreshError) as raised:
            await backend.refresh(callback, expected=old, timeout=0.02)
        assert raised.value.code == "refresh_timeout"
        assert await backend.load() == new
        await backend.save(credentials("after-timeout"))

    run(probe())


@pytest.mark.parametrize("operation_name", ["load", "save", "recover"])
def test_cancelled_file_operation_drains_its_worker(
    tmp_path: Path, monkeypatch, operation_name: str
):
    async def probe() -> None:
        backend = FileCodexCredentialBackend(tmp_path / f"{operation_name}.json")
        old = credentials("old")
        new = credentials("new")
        await backend.save(old)
        original = getattr(backend, f"_{operation_name}_sync")
        started = threading.Event()

        def delayed(*args: object) -> object:
            started.set()
            time.sleep(0.05)
            return original(*args)

        monkeypatch.setattr(backend, f"_{operation_name}_sync", delayed)
        if operation_name == "load":
            task = asyncio.create_task(backend.load())
        elif operation_name == "save":
            task = asyncio.create_task(backend.save(new))
        else:
            task = asyncio.create_task(backend.recover(new))
        await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if operation_name == "load":
            assert await backend.load() == old
        else:
            assert await backend.load() == new
        await backend.save(credentials("after-drain"))

    run(probe())
