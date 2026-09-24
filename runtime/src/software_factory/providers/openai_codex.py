"""Application-owned OpenAI Codex credentials and bounded provider pressure.

The default runtime has no dependency on ``pydantic-ai``.  This module owns the
credential value, a replaceable backend protocol, and a POSIX file backend whose
refresh transaction is the only supported place to consume a refresh token.  The
optional pinned PydanticAI adapter lives in ``pydantic_ai_codex``.

The file backend deliberately uses an application-owned flat JSON shape.  It is
not the Codex CLI ``auth.json`` format and this module never imports Hermes or
Codex CLI storage code.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import inspect
import itertools
import json
import math
import os
import stat
import threading
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, Self, TypeAlias, cast

try:  # POSIX is the supported production target for the filesystem backend.
    import fcntl
except ImportError:  # pragma: no cover - the constructor fails closed elsewhere.
    fcntl = None  # type: ignore[assignment]

__all__ = (
    "CodexCredentialBackend",
    "CodexCredentialError",
    "CodexCredentialStore",
    "CodexProviderPressure",
    "CredentialCorruptionError",
    "CredentialLockError",
    "CredentialLockTimeoutError",
    "CredentialNotFoundError",
    "CredentialPermissionError",
    "CredentialPersistenceError",
    "CredentialRefreshError",
    "CredentialValidationError",
    "FileCodexCredentialBackend",
    "FileCredentialSource",
    "OpenAICodexCredentialSource",
    "OpenAICodexCredentials",
    "PressureAdmissionTimeoutError",
    "ProviderAdmission",
    "ProviderCompletion",
    "ProviderGeneration",
    "ProviderLoopError",
    "ProviderOperationError",
    "ProviderPressure",
    "ProviderQueueFullError",
)


_MAX_CREDENTIAL_FIELD_LENGTH = 128 * 1024
_DEFAULT_MAX_FILE_BYTES = 512 * 1024
_MAX_FILE_BYTES = 16 * 1024 * 1024
_DEFAULT_LOCK_TIMEOUT = 5.0
_MAX_LOCK_TIMEOUT = 300.0
_DEFAULT_REFRESH_TIMEOUT = 30.0
_MAX_REFRESH_TIMEOUT = 300.0
_DEFAULT_ADMISSION_TIMEOUT = 30.0
_MAX_ADMISSION_TIMEOUT = 3_600.0
_DEFAULT_MAX_BACKOFF = 60.0
_DEFAULT_MAX_QUEUE = 128
_DEFAULT_MAX_RETAINED_GENERATIONS = 128
_DEFAULT_MAX_GENERATION_SCOPES = 64
_MAX_CAPACITY = 1_024
_MAX_QUEUE = 4_096
_MAX_RETAINED_GENERATIONS = 4_096
_MAX_GENERATION_SCOPES = 1_024
_MAX_BACKOFF = 3_600.0
_MAX_GENERATION_VALUE = (1 << 31) - 1

_SCHEMA_FIELDS = ("access_token", "refresh_token", "account_id")
_SCHEMA_FIELD_SET = frozenset(_SCHEMA_FIELDS)
_SAFE_CODE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_TEMP_COUNTER = itertools.count()


# ---------------------------------------------------------------------------
# Secret-safe values and errors


def _safe_code(value: object, fallback: str) -> str:
    if type(value) is str and 0 < len(value) <= 64 and set(value) <= _SAFE_CODE_CHARS:
        return value
    return fallback


class CodexCredentialError(RuntimeError):
    """Base error with only a bounded category and reason code.

    Caller paths, provider exception text, credential values, and errno details
    are intentionally not retained in the public exception object.  ``path`` is
    accepted for source compatibility but is ignored.
    """

    __slots__ = ("category", "code")

    def __init__(
        self,
        code: str,
        *,
        category: str = "credential",
        path: object | None = None,
    ) -> None:
        del path
        self.category = _safe_code(category, "credential")
        self.code = _safe_code(code, "internal_error")
        super().__init__(f"{self.category} error code={self.code}")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class CredentialValidationError(CodexCredentialError):
    """Credential values or their serialized schema are invalid."""

    def __init__(
        self, code: str = "invalid_credentials", *, path: object | None = None
    ) -> None:
        super().__init__(code, category="schema", path=path)


class CredentialCorruptionError(CodexCredentialError):
    """The stored file is present but cannot be trusted or decoded."""

    def __init__(
        self, code: str = "corrupt_store", *, path: object | None = None
    ) -> None:
        super().__init__(code, category="corruption", path=path)


class CredentialPermissionError(CodexCredentialError):
    """The credential file, parent directory, or filesystem is unsafe."""

    def __init__(
        self, code: str = "insecure_permissions", *, path: object | None = None
    ) -> None:
        super().__init__(code, category="permission", path=path)


class CredentialPersistenceError(CodexCredentialError):
    """A durable file operation failed without exposing its underlying detail."""

    def __init__(
        self, code: str = "persistence_failed", *, path: object | None = None
    ) -> None:
        super().__init__(code, category="persistence", path=path)


class CredentialNotFoundError(CodexCredentialError):
    """No credential generation exists at the configured path."""

    def __init__(
        self, code: str = "missing_store", *, path: object | None = None
    ) -> None:
        super().__init__(code, category="not_found", path=path)


class CredentialLockError(CodexCredentialError):
    """The refresh lock cannot be used safely."""

    def __init__(
        self, code: str = "lock_unavailable", *, path: object | None = None
    ) -> None:
        super().__init__(code, category="lock", path=path)


class CredentialLockTimeoutError(CredentialLockError):
    """The bounded refresh-lock wait expired."""

    def __init__(
        self, code: str = "lock_timeout", *, path: object | None = None
    ) -> None:
        super().__init__(code, path=path)


class CredentialRefreshError(CodexCredentialError):
    """A refresh callback failed or returned an invalid credential generation."""

    def __init__(
        self, code: str = "refresh_failed", *, path: object | None = None
    ) -> None:
        super().__init__(code, category="refresh", path=path)


class ProviderLoopError(CodexCredentialError):
    """A provider-pressure object was used from a different event loop."""

    def __init__(self, code: str = "wrong_event_loop") -> None:
        super().__init__(code, category="provider_pressure")


class ProviderQueueFullError(CodexCredentialError):
    """The bounded provider-pressure queue is full."""

    def __init__(self, code: str = "queue_full") -> None:
        super().__init__(code, category="provider_pressure")


_STORAGE_ERROR_CATEGORIES = frozenset(
    {"corruption", "permission", "persistence", "not_found", "lock", "schema"}
)
_STORAGE_ERROR_TYPES = (
    CredentialCorruptionError,
    CredentialPermissionError,
    CredentialPersistenceError,
    CredentialNotFoundError,
    CredentialLockError,
    CredentialValidationError,
)


def _is_credential_storage_error(error: BaseException) -> bool:
    """Classify only local storage failures; refresh/network errors stay transient."""

    return isinstance(error, _STORAGE_ERROR_TYPES) or (
        isinstance(error, CodexCredentialError)
        and getattr(error, "category", None) in _STORAGE_ERROR_CATEGORIES
    )


class PressureAdmissionTimeoutError(CodexCredentialError):
    """Provider admission could not be obtained within its finite deadline."""

    def __init__(self, code: str = "admission_timeout") -> None:
        super().__init__(code, category="provider_pressure")


class ProviderOperationError(CodexCredentialError):
    """A shared provider operation failed without retaining unsafe exception text."""

    def __init__(self, code: str = "operation_failed") -> None:
        super().__init__(code, category="provider_pressure")


def _clone_credential_error(
    error: CodexCredentialError, fallback: CodexCredentialError
) -> CodexCredentialError:
    """Copy only a safe reason code, outside the source exception context."""

    code = _safe_code(getattr(error, "code", None), fallback.code)
    if isinstance(error, CredentialCorruptionError):
        return CredentialCorruptionError(code)
    if isinstance(error, CredentialPermissionError):
        return CredentialPermissionError(code)
    if isinstance(error, CredentialNotFoundError):
        return CredentialNotFoundError(code)
    if isinstance(error, CredentialLockTimeoutError):
        return CredentialLockTimeoutError(code)
    if isinstance(error, CredentialLockError):
        return CredentialLockError(code)
    if isinstance(error, CredentialRefreshError):
        return CredentialRefreshError(code)
    if isinstance(error, CredentialValidationError):
        return CredentialValidationError(code)
    if isinstance(error, CredentialPersistenceError):
        return CredentialPersistenceError(code)
    return type(fallback)(fallback.code)


def _validate_secret_text(value: object, *, code: str) -> str:
    """Validate one secret-bearing field without rendering its value."""

    if type(value) is not str:
        raise CredentialValidationError(code)
    if not value or len(value) > _MAX_CREDENTIAL_FIELD_LENGTH:
        raise CredentialValidationError(code)
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise CredentialValidationError(code)
    return value


class OpenAICodexCredentials:
    """The three values required by the Codex provider.

    This is deliberately not a dataclass or mapping.  Generic serializers and
    pickle are rejected so accidental telemetry/artifact serialization cannot
    turn the credential object into a secret-bearing payload.
    """

    __slots__ = ("_sealed", "access_token", "account_id", "refresh_token")

    def __init__(
        self, *, access_token: str, refresh_token: str, account_id: str
    ) -> None:
        access = _validate_secret_text(access_token, code="invalid_access_token")
        refresh = _validate_secret_text(refresh_token, code="invalid_refresh_token")
        account = _validate_secret_text(account_id, code="invalid_account_id")
        object.__setattr__(self, "access_token", access)
        object.__setattr__(self, "refresh_token", refresh)
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False) and name in {
            "access_token",
            "refresh_token",
            "account_id",
        }:
            raise AttributeError("credential values are immutable")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return "<OpenAICodexCredentials redacted>"

    def __str__(self) -> str:
        return "<OpenAICodexCredentials redacted>"

    def __format__(self, _format_spec: str) -> str:
        return "<OpenAICodexCredentials redacted>"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, OpenAICodexCredentials):
            return (
                self.access_token == other.access_token
                and self.refresh_token == other.refresh_token
                and self.account_id == other.account_id
            )
        foreign = cast(Any, other)
        try:
            return (
                self.access_token == foreign.access_token
                and self.refresh_token == foreign.refresh_token
                and self.account_id == foreign.account_id
            )
        except Exception:  # noqa: BLE001 - foreign comparison fails closed
            return False

    def __hash__(self) -> int:
        return hash((self.access_token, self.refresh_token, self.account_id))

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("credential values are not serializable")


CredentialRefreshCallback: TypeAlias = Callable[
    [OpenAICodexCredentials], Awaitable[OpenAICodexCredentials]
]


class CodexCredentialBackend(Protocol):
    """Replaceable storage contract for application-owned credentials."""

    async def load(self) -> OpenAICodexCredentials:
        """Load one complete, validated generation."""
        ...

    async def save(self, credentials: OpenAICodexCredentials) -> None:
        """Persist one complete generation atomically."""
        ...

    async def refresh(
        self,
        refresh_callback: CredentialRefreshCallback,
        *,
        expected: OpenAICodexCredentials | None = None,
        timeout: float | None = None,
    ) -> OpenAICodexCredentials:
        """Reload, consume, and persist one rotation under one backend lock."""
        ...


CodexCredentialStore = CodexCredentialBackend


def _credential_mapping(
    credentials: object, *, path: object | None = None
) -> dict[str, str]:
    """Private serialization helper used only by the trusted file writer."""

    value = _coerce_credentials(credentials, path=path)
    return {
        "access_token": value.access_token,
        "refresh_token": value.refresh_token,
        "account_id": value.account_id,
    }


def _credentials_from_payload(
    payload: object, *, path: object | None = None
) -> OpenAICodexCredentials:
    if type(payload) is not dict:
        raise CredentialCorruptionError("schema_not_object", path=path)
    if frozenset(payload) != _SCHEMA_FIELD_SET or len(payload) != len(_SCHEMA_FIELDS):
        raise CredentialCorruptionError("schema_fields", path=path)
    failure: CredentialCorruptionError | None = None
    try:
        value = OpenAICodexCredentials(
            access_token=payload["access_token"],  # type: ignore[arg-type]
            refresh_token=payload["refresh_token"],  # type: ignore[arg-type]
            account_id=payload["account_id"],  # type: ignore[arg-type]
        )
    except CredentialValidationError:
        failure = CredentialCorruptionError("schema_values", path=path)
        value = None
    if failure is not None:
        raise failure
    assert value is not None
    return value


def _coerce_credentials(
    credentials: object, *, path: object | None = None
) -> OpenAICodexCredentials:
    """Copy only the three structural fields from a provider credential value."""

    if isinstance(credentials, OpenAICodexCredentials):
        return credentials
    foreign = cast(Any, credentials)
    failure: CredentialValidationError | None = None
    try:
        value = OpenAICodexCredentials(
            access_token=foreign.access_token,
            refresh_token=foreign.refresh_token,
            account_id=foreign.account_id,
        )
    except CredentialValidationError:
        failure = CredentialValidationError("credential_type", path=path)
        value = None
    except Exception:  # noqa: BLE001 - foreign provider details are untrusted
        failure = CredentialValidationError("credential_type", path=path)
        value = None
    if failure is not None:
        raise failure
    assert value is not None
    return value


def _strict_object_pairs(pairs: list[tuple[object, object]]) -> dict[object, object]:
    """Reject duplicate JSON keys instead of silently selecting one value."""

    result: dict[object, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _serialized_credentials(credentials: object, *, path: object) -> bytes:
    mapping = _credential_mapping(credentials, path=path)
    failure: CredentialValidationError | None = None
    try:
        data = (
            json.dumps(
                mapping, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError):
        failure = CredentialValidationError("credential_serialization", path=path)
        data = b""
    if failure is not None:
        raise failure
    return data


def _validate_duration(
    value: float | None, *, default: float, maximum: float, code: str
) -> float:
    resolved = default if value is None else value
    if type(resolved) not in {int, float}:
        raise ValueError(code)
    try:
        numeric = float(resolved)
    except (OverflowError, ValueError):
        raise ValueError(code) from None
    if not math.isfinite(numeric) or numeric < 0 or numeric > maximum:
        raise ValueError(code)
    return numeric


def _validate_int(value: object, *, minimum: int, maximum: int, code: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(code)
    return value


_DRAIN_FAILED = object()


async def _drain_cancelled_task(task: asyncio.Task[Any]) -> object:
    """Drain a shielded task while preserving every caller cancellation."""

    current = asyncio.current_task()
    cancellations = current.cancelling() if current is not None else 0
    if current is not None:
        for _ in range(cancellations):
            current.uncancel()
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if current is not None:
                    newly_cancelled = current.cancelling()
                    for _ in range(newly_cancelled):
                        current.uncancel()
                    cancellations += newly_cancelled
                continue
            except BaseException:  # noqa: BLE001 - drain before sanitizing
                break
        if task.cancelled():
            return _DRAIN_FAILED
        try:
            return task.result()
        except BaseException:  # noqa: BLE001 - caller receives a safe failure
            return _DRAIN_FAILED
    finally:
        if current is not None:
            for _ in range(cancellations):
                current.cancel()


async def _await_task_drained(awaitable: Awaitable[Any]) -> Any:
    """Await an operation without orphaning it when the caller is cancelled."""

    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _drain_cancelled_task(task)
        raise


async def _await_before_deadline(
    awaitable: Awaitable[Any], deadline: float, *, timeout_code: str
) -> Any:
    """Await one operation and drain it before reporting timeout/cancellation."""

    task = asyncio.ensure_future(awaitable)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        task.cancel()
        await _drain_cancelled_task(task)
        raise CredentialRefreshError(timeout_code)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except TimeoutError:
        task.cancel()
        await _drain_cancelled_task(task)
        raise CredentialRefreshError(timeout_code)
    except asyncio.CancelledError:
        await _drain_cancelled_task(task)
        raise


def _is_async_refresh_callback(callback: object) -> bool:
    return inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        type(callback).__call__
    )


def _fingerprint(credentials: OpenAICodexCredentials) -> bytes:
    digest = hashlib.sha256()
    for value in (
        credentials.access_token,
        credentials.refresh_token,
        credentials.account_id,
    ):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.digest()


def _same_credentials(left: OpenAICodexCredentials, right: object) -> bool:
    try:
        return _fingerprint(left) == _fingerprint(_coerce_credentials(right))
    except Exception:  # noqa: BLE001 - malformed comparison fails closed
        return False


def _is_safe_owner(info: os.stat_result) -> bool:
    return info.st_uid in {os.geteuid(), 0}


def _directory_error(info: os.stat_result) -> CodexCredentialError | None:
    if not stat.S_ISDIR(info.st_mode):
        return CredentialPermissionError("parent_not_directory")
    if not _is_safe_owner(info):
        return CredentialPermissionError("parent_owner")
    mode = stat.S_IMODE(info.st_mode)
    sticky_shared = bool(mode & stat.S_ISVTX) and info.st_uid == 0
    if mode & 0o022 and not sticky_shared:
        return CredentialPermissionError("parent_writable")
    return None


def _target_error(
    info: os.stat_result, *, require_mode: bool
) -> CodexCredentialError | None:
    if stat.S_ISLNK(info.st_mode):
        return CredentialPermissionError("target_symlink")
    if not stat.S_ISREG(info.st_mode):
        return CredentialPermissionError("target_not_regular")
    if info.st_nlink != 1:
        return CredentialPermissionError("target_hardlink")
    if not _is_safe_owner(info):
        return CredentialPermissionError("target_owner")
    if require_mode and stat.S_IMODE(info.st_mode) != 0o600:
        return CredentialPermissionError("target_mode")
    return None


class _CredentialPathMixin:
    """Descriptor-anchored local-filesystem operations."""

    path: Path
    lock_path: Path
    max_file_bytes: int
    _anchor_fd: int
    _parent_identity: tuple[int, int]
    _parent_metadata: tuple[int, int, int, int]
    _filesystem_supported: bool

    @staticmethod
    def _supported() -> bool:
        required = (
            os.name == "posix",
            fcntl is not None,
            bool(_O_CLOEXEC),
            bool(_O_NOFOLLOW),
            bool(_O_NONBLOCK),
            bool(_O_DIRECTORY),
            os.open in getattr(os, "supports_dir_fd", set()),
            os.stat in getattr(os, "supports_dir_fd", set()),
            os.rename in getattr(os, "supports_dir_fd", set()),
            os.unlink in getattr(os, "supports_dir_fd", set()),
        )
        return all(required)

    def _open_configured_parent(self) -> int:
        supported = getattr(self, "_filesystem_supported", None)
        if supported is None:
            supported = self._supported()
        if not supported:
            raise CredentialPermissionError("unsupported_filesystem")
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
        current: int | None = None
        failure: CodexCredentialError | None = None
        try:
            current = os.open(os.path.sep, flags)
        except OSError:
            failure = CredentialPermissionError("parent_open_failed")
        if failure is not None:
            raise failure
        assert current is not None
        root = current
        for component in self.path.parent.parts:
            if component in {self.path.parent.anchor, ""}:
                continue
            next_fd: int | None = None
            try:
                next_fd = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                failure = CredentialPermissionError("parent_missing")
            except OSError:
                failure = CredentialPermissionError("parent_open_failed")
            if failure is not None:
                break
            assert next_fd is not None
            try:
                info = os.fstat(next_fd)
            except OSError:
                failure = CredentialPermissionError("parent_stat_failed")
            else:
                failure = _directory_error(info)
            if failure is not None:
                with contextlib.suppress(OSError):
                    os.close(next_fd)
                break
            if current != root:
                with contextlib.suppress(OSError):
                    os.close(current)
            current = next_fd
        if failure is not None:
            if current is not None:
                with contextlib.suppress(OSError):
                    os.close(current)
            raise failure
        if current is None:
            raise CredentialPermissionError("parent_open_failed")
        if current == root and self.path.parent == Path(os.path.sep):
            return current
        if current != root:
            with contextlib.suppress(OSError):
                os.close(root)
        return current

    def _open_trusted_anchor(self) -> int:
        return self._open_configured_parent()

    def _validate_anchor(self, descriptor: int) -> CodexCredentialError | None:
        try:
            info = os.fstat(descriptor)
        except OSError:
            return CredentialPermissionError("parent_stat_failed")
        if (info.st_dev, info.st_ino) != self._parent_identity:
            return CredentialPermissionError("parent_changed_identity")
        if (
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
        ) != self._parent_metadata[1:]:
            return CredentialPermissionError("parent_changed_metadata")
        return _directory_error(info)

    def _validate_configured_parent(self) -> CodexCredentialError | None:
        try:
            descriptor = self._open_configured_parent()
        except CodexCredentialError as error:
            return error
        try:
            return self._validate_anchor(descriptor)
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def _lock_marker_error(self, parent_fd: int) -> CodexCredentialError | None:
        try:
            info = os.stat(self.lock_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            return CredentialLockError("lock_marker_stat_failed")
        if stat.S_ISLNK(info.st_mode):
            return CredentialLockError("lock_marker_symlink")
        # The directory itself is the immutable lock identity.  A pathname lock
        # is never created by this backend; any marker is treated as tampering.
        return CredentialLockError("lock_marker_present")

    def _assert_current_parent(self, parent_fd: int) -> None:
        failure = self._validate_anchor(parent_fd) or self._validate_configured_parent()
        if failure is not None:
            raise failure

    def _open_operation_parent(self) -> tuple[int | None, CodexCredentialError | None]:
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
        anchor_fd = getattr(self, "_anchor_fd", None)
        if type(anchor_fd) is not int:
            return None, CredentialPermissionError("backend_closed")
        failure = self._validate_configured_parent()
        if failure is not None:
            return None, failure
        try:
            descriptor = os.open(".", flags, dir_fd=anchor_fd)
        except OSError:
            return None, CredentialPersistenceError("parent_open_failed")
        failure = self._validate_anchor(descriptor) or self._lock_marker_error(
            descriptor
        )
        if failure is None:
            # Re-open the configured path after opening the lock descriptor.  The
            # descriptor-relative operation remains anchored, but a recreated
            # pathname must never silently acquire the old directory's lock.
            failure = self._validate_configured_parent()
        if failure is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            return None, failure
        return descriptor, None

    def _try_lock_once(self) -> tuple[int | None, CodexCredentialError | None]:
        descriptor, failure = self._open_operation_parent()
        if failure is not None:
            return None, failure
        assert descriptor is not None
        try:
            assert fcntl is not None
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            return None, None
        except OSError:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            return None, CredentialLockError("lock_acquire_failed")
        failure = (
            self._lock_marker_error(descriptor) or self._validate_configured_parent()
        )
        if failure is not None:
            self._release_fd(descriptor)
            return None, failure
        return descriptor, None

    @staticmethod
    def _release_fd(descriptor: int) -> None:
        try:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def _acquire_sync(self, timeout: float | None) -> int:
        duration = _validate_duration(
            timeout,
            default=self.lock_timeout,
            maximum=_MAX_LOCK_TIMEOUT,
            code="lock_timeout must be finite and bounded",
        )
        deadline = time.monotonic() + duration
        while True:
            descriptor, failure = self._try_lock_once()
            if failure is not None:
                raise failure
            if descriptor is not None:
                return descriptor
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CredentialLockTimeoutError()
            time.sleep(min(0.05, remaining))

    async def _acquire_async(self, timeout: float | None) -> int:
        duration = _validate_duration(
            timeout,
            default=self.lock_timeout,
            maximum=_MAX_LOCK_TIMEOUT,
            code="lock_timeout must be finite and bounded",
        )
        deadline = time.monotonic() + duration
        while True:
            task = asyncio.create_task(asyncio.to_thread(self._try_lock_once))
            try:
                descriptor, failure = await asyncio.shield(task)
            except asyncio.CancelledError:
                # The attempt is non-blocking.  Own and drain it before closing
                # a descriptor so cancellation cannot strand a process lock.
                outcome = await _drain_cancelled_task(task)
                if outcome is not _DRAIN_FAILED:
                    descriptor, _failure = cast(
                        tuple[int | None, CodexCredentialError | None], outcome
                    )
                    if descriptor is not None:
                        await _await_task_drained(
                            asyncio.to_thread(self._release_fd, descriptor)
                        )
                raise
            if failure is not None:
                raise failure
            if descriptor is not None:
                return descriptor
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CredentialLockTimeoutError()
            await asyncio.sleep(min(0.05, remaining))

    def _stat_target(
        self, parent_fd: int, *, require_mode: bool
    ) -> tuple[os.stat_result | None, CodexCredentialError | None]:
        try:
            info = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None, None
        except OSError:
            return None, CredentialPersistenceError("target_stat_failed")
        return info, _target_error(info, require_mode=require_mode)

    def _open_target(
        self, parent_fd: int, expected: os.stat_result
    ) -> tuple[int | None, CodexCredentialError | None]:
        flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC
        try:
            descriptor = os.open(self.path.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            return None, CredentialNotFoundError()
        except OSError as error:
            if error.errno == errno.ELOOP:
                return None, CredentialPermissionError("target_symlink")
            return None, CredentialPersistenceError("target_open_failed")
        try:
            info = os.fstat(descriptor)
        except OSError:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            return None, CredentialPersistenceError("target_stat_failed")
        failure = _target_error(info, require_mode=True)
        if failure is None and (info.st_dev, info.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            failure = CredentialPermissionError("target_changed_identity")
        if failure is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            return None, failure
        return descriptor, None

    def _read_unlocked(self, parent_fd: int) -> OpenAICodexCredentials:
        self._assert_current_parent(parent_fd)
        expected, failure = self._stat_target(parent_fd, require_mode=True)
        if failure is not None:
            raise failure
        if expected is None:
            raise CredentialNotFoundError()
        descriptor, failure = self._open_target(parent_fd, expected)
        if failure is not None:
            raise failure
        assert descriptor is not None
        data = bytearray()
        read_failure: CodexCredentialError | None = None
        try:
            while len(data) <= self.max_file_bytes:
                try:
                    chunk = os.read(
                        descriptor, min(65_536, self.max_file_bytes + 1 - len(data))
                    )
                except OSError:
                    read_failure = CredentialPersistenceError("target_read_failed")
                    break
                if not chunk:
                    break
                data.extend(chunk)
            if read_failure is None and len(data) > self.max_file_bytes:
                read_failure = CredentialCorruptionError("store_too_large")
            if read_failure is None:
                try:
                    final_info = os.fstat(descriptor)
                except OSError:
                    read_failure = CredentialPersistenceError("target_stat_failed")
                else:
                    read_failure = _target_error(final_info, require_mode=True)
                    if read_failure is None and (
                        final_info.st_dev,
                        final_info.st_ino,
                    ) != (expected.st_dev, expected.st_ino):
                        read_failure = CredentialPermissionError(
                            "target_changed_identity"
                        )
            if read_failure is None:
                current, path_failure = self._stat_target(parent_fd, require_mode=True)
                if path_failure is not None:
                    read_failure = path_failure
                elif current is None or (
                    current.st_dev,
                    current.st_ino,
                ) != (expected.st_dev, expected.st_ino):
                    read_failure = CredentialPermissionError("target_changed_identity")
                else:
                    parent_failure = self._validate_configured_parent()
                    if parent_failure is not None:
                        read_failure = parent_failure
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if read_failure is not None:
            raise read_failure
        try:
            decoded = bytes(data).decode("utf-8")
            payload = json.loads(decoded, object_pairs_hook=_strict_object_pairs)
        except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            payload = None
            read_failure = CredentialCorruptionError("invalid_json")
        if read_failure is not None:
            raise read_failure
        return _credentials_from_payload(payload)

    def _write_atomic_unlocked(
        self,
        parent_fd: int,
        credentials: OpenAICodexCredentials,
        *,
        allow_insecure_existing: bool,
    ) -> None:
        self._assert_current_parent(parent_fd)
        data = _serialized_credentials(credentials, path=self.path)
        existing, failure = self._stat_target(
            parent_fd, require_mode=not allow_insecure_existing
        )
        if failure is not None:
            raise failure
        temporary_name: str | None = None
        descriptor: int | None = None
        temporary_info: os.stat_result | None = None
        write_failure: CodexCredentialError | None = None
        published_by_us = False
        directory_dirty = False

        def scrub_inode() -> None:
            """Erase a staged inode while its original descriptor remains open."""

            if descriptor is None:
                return
            with contextlib.suppress(BaseException):
                os.ftruncate(descriptor, 0)
            with contextlib.suppress(BaseException):
                os.fsync(descriptor)

        def unlink_published_inode() -> None:
            """Remove our target name only when it still names the staged inode."""

            nonlocal directory_dirty
            if descriptor is None:
                return
            try:
                fd_info = os.fstat(descriptor)
                path_info = os.stat(
                    self.path.name, dir_fd=parent_fd, follow_symlinks=False
                )
            except OSError:
                return
            if (fd_info.st_dev, fd_info.st_ino) != (
                path_info.st_dev,
                path_info.st_ino,
            ):
                return
            try:
                os.unlink(self.path.name, dir_fd=parent_fd)
            except OSError:
                return
            directory_dirty = True

        try:
            for _ in range(32):
                candidate = f".{self.path.name}.{os.getpid()}.{time.monotonic_ns()}.{next(_TEMP_COUNTER)}.tmp"
                try:
                    # Keep this exact descriptor open through rename and the
                    # post-publish link-count validation.  O_RDWR is required
                    # to scrub the inode if a hardlink race is observed.
                    descriptor = os.open(
                        candidate,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                        0o600,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    continue
                except OSError:
                    write_failure = CredentialPersistenceError("temporary_open_failed")
                else:
                    temporary_name = candidate
                break
            if write_failure is not None:
                raise write_failure
            if descriptor is None or temporary_name is None:
                write_failure = CredentialPersistenceError("temporary_name_failed")
                raise write_failure
            try:
                os.fchmod(descriptor, 0o600)
                temporary_info = os.fstat(descriptor)
            except OSError:
                write_failure = CredentialPersistenceError("temporary_prepare_failed")
                raise write_failure
            failure = _target_error(temporary_info, require_mode=True)
            if failure is not None:
                write_failure = CredentialPermissionError("temporary_unsafe")
                raise write_failure
            written = 0
            while written < len(data):
                try:
                    count = os.write(descriptor, data[written:])
                except OSError:
                    write_failure = CredentialPersistenceError("temporary_write_failed")
                    raise write_failure
                if count <= 0:
                    write_failure = CredentialPersistenceError("temporary_write_failed")
                    raise write_failure
                written += count
            try:
                os.fsync(descriptor)
            except OSError:
                write_failure = CredentialPersistenceError("temporary_sync_failed")
                raise write_failure
            try:
                final_temporary_info = os.fstat(descriptor)
            except OSError:
                write_failure = CredentialPersistenceError("temporary_stat_failed")
                raise write_failure
            if (
                not stat.S_ISREG(final_temporary_info.st_mode)
                or final_temporary_info.st_nlink != 1
                or stat.S_IMODE(final_temporary_info.st_mode) != 0o600
            ):
                write_failure = CredentialPermissionError("temporary_hardlink")
                raise write_failure
            temporary_info = final_temporary_info

            current, failure = self._stat_target(
                parent_fd, require_mode=not allow_insecure_existing
            )
            if failure is not None:
                write_failure = failure
                raise write_failure
            if (existing is None) != (current is None):
                write_failure = CredentialPermissionError("target_changed_identity")
                raise write_failure
            if (
                existing is not None
                and current is not None
                and (existing.st_dev, existing.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                write_failure = CredentialPermissionError("target_changed_identity")
                raise write_failure
            self._assert_current_parent(parent_fd)
            try:
                os.rename(
                    temporary_name,
                    self.path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_name = None
                published_by_us = True
                directory_dirty = True
                os.fsync(parent_fd)
            except OSError:
                write_failure = CredentialPersistenceError("atomic_replace_failed")
                raise write_failure
            self._assert_current_parent(parent_fd)

            # Validate the inode through the retained descriptor, not a path
            # lookup alone.  A same-UID race can add a hardlink after rename;
            # every link must lose the bytes before we report failure.
            try:
                published_fd_info = os.fstat(descriptor)
            except OSError:
                write_failure = CredentialPersistenceError("published_stat_failed")
                raise write_failure
            if temporary_info is None or (
                published_fd_info.st_dev,
                published_fd_info.st_ino,
            ) != (temporary_info.st_dev, temporary_info.st_ino):
                write_failure = CredentialPermissionError("published_changed_identity")
                raise write_failure
            if (
                not stat.S_ISREG(published_fd_info.st_mode)
                or stat.S_IMODE(published_fd_info.st_mode) != 0o600
                or published_fd_info.st_nlink != 1
            ):
                write_failure = CredentialPermissionError("published_hardlink")
                raise write_failure
            published, failure = self._stat_target(parent_fd, require_mode=True)
            if failure is not None:
                write_failure = failure
                raise write_failure
            if published is None or (
                published.st_dev,
                published.st_ino,
            ) != (published_fd_info.st_dev, published_fd_info.st_ino):
                write_failure = CredentialPermissionError("published_changed_identity")
                raise write_failure
        except CodexCredentialError as error:
            write_failure = error
        except OSError:
            write_failure = CredentialPersistenceError("atomic_replace_failed")
        except BaseException:  # noqa: BLE001 - filesystem details are untrusted
            write_failure = CredentialPersistenceError("atomic_replace_failed")
        finally:
            if write_failure is not None:
                # Do not leave staged or newly published bytes behind on any
                # failed atomicity check.  The descriptor still names the
                # inode even when a race removed both visible names.
                scrub_inode()
                if published_by_us:
                    with contextlib.suppress(BaseException):
                        unlink_published_inode()
            if descriptor is not None:
                with contextlib.suppress(BaseException):
                    os.close(descriptor)
            if temporary_name is not None:
                with contextlib.suppress(BaseException):
                    os.unlink(temporary_name, dir_fd=parent_fd)
                directory_dirty = True
            if directory_dirty and write_failure is not None:
                with contextlib.suppress(BaseException):
                    os.fsync(parent_fd)
        if write_failure is not None:
            raise write_failure

    def _save_unlocked(
        self, parent_fd: int, credentials: OpenAICodexCredentials
    ) -> None:
        self._assert_current_parent(parent_fd)
        existing, failure = self._stat_target(parent_fd, require_mode=True)
        if failure is not None:
            raise failure
        if existing is not None:
            # A normal save never repairs corruption or permissions implicitly.
            self._read_unlocked(parent_fd)
        self._write_atomic_unlocked(
            parent_fd, credentials, allow_insecure_existing=False
        )

    def _recover_unlocked(
        self, parent_fd: int, credentials: OpenAICodexCredentials
    ) -> None:
        # Recovery still rejects symlinks, non-files, hardlinks, and foreign owners.
        self._assert_current_parent(parent_fd)
        self._write_atomic_unlocked(
            parent_fd, credentials, allow_insecure_existing=True
        )

    def _load_sync(self) -> OpenAICodexCredentials:
        descriptor = self._acquire_sync(None)
        try:
            current = self._read_unlocked(descriptor)
            self._last_fingerprint = _fingerprint(current)
            return current
        finally:
            self._release_fd(descriptor)

    def _save_sync(self, credentials: OpenAICodexCredentials) -> None:
        value = _coerce_credentials(credentials, path=self.path)
        descriptor = self._acquire_sync(None)
        try:
            if self._last_fingerprint is not None:
                current = self._read_unlocked(descriptor)
                if _fingerprint(current) != self._last_fingerprint:
                    raise CredentialPersistenceError("stale_generation")
            self._save_unlocked(descriptor, value)
            self._last_fingerprint = _fingerprint(value)
        finally:
            self._release_fd(descriptor)

    def _recover_sync(self, credentials: OpenAICodexCredentials) -> None:
        value = _coerce_credentials(credentials, path=self.path)
        descriptor = self._acquire_sync(None)
        try:
            self._recover_unlocked(descriptor, value)
            self._last_fingerprint = _fingerprint(value)
        finally:
            self._release_fd(descriptor)

    async def _await_blocking(
        self,
        function: Callable[..., Any],
        *args: Any,
        deadline: float | None = None,
    ) -> Any:
        if deadline is not None and time.monotonic() > deadline:
            raise CredentialRefreshError("refresh_timeout")
        try:
            result = await _await_task_drained(asyncio.to_thread(function, *args))
        except asyncio.CancelledError:
            raise
        except BaseException:
            if deadline is not None and time.monotonic() > deadline:
                raise CredentialRefreshError("refresh_timeout")
            raise
        if deadline is not None and time.monotonic() > deadline:
            raise CredentialRefreshError("refresh_timeout")
        return result

    async def _read_locked_async(
        self, descriptor: int, *, deadline: float | None = None
    ) -> OpenAICodexCredentials:
        return cast(
            OpenAICodexCredentials,
            await self._await_blocking(
                self._read_unlocked, descriptor, deadline=deadline
            ),
        )

    async def _save_locked_async(
        self,
        descriptor: int,
        value: OpenAICodexCredentials,
        *,
        deadline: float | None = None,
    ) -> None:
        await self._await_blocking(
            self._save_unlocked, descriptor, value, deadline=deadline
        )

    async def _recover_locked_async(
        self,
        descriptor: int,
        value: OpenAICodexCredentials,
        *,
        deadline: float | None = None,
    ) -> None:
        await self._await_blocking(
            self._recover_unlocked, descriptor, value, deadline=deadline
        )


class FileCodexCredentialBackend(_CredentialPathMixin):
    """Secure JSON backend using a trusted-directory flock and atomic replacement.

    Supported semantics are POSIX local filesystems that implement descriptor-
    relative open/stat/replace/unlink, ``O_NOFOLLOW``/``O_NONBLOCK``, directory
    fsync, and directory flocking.  The constructor fails closed otherwise.
    The security boundary assumes a local, non-hostile same-UID environment:
    that principal can read a target it is authorized to access by design, so
    the backend does not claim protection from that reader.  Kernel liveness is
    also assumed; cooperative cancellation drains worker threads but cannot
    forcibly interrupt a permanently hung kernel filesystem syscall.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
        refresh_timeout: float = _DEFAULT_REFRESH_TIMEOUT,
    ) -> None:
        raw_path = os.fspath(path)
        if isinstance(raw_path, bytes):
            raise TypeError("credential path must be text")
        self.path = Path(os.path.abspath(raw_path))
        if self.path.name in {"", ".", ".."}:
            raise ValueError("credential path must name a file")
        normalized_max_file_bytes = _validate_int(
            max_file_bytes,
            minimum=256,
            maximum=_MAX_FILE_BYTES,
            code="max_file_bytes must be bounded",
        )
        normalized_lock_timeout = _validate_duration(
            lock_timeout,
            default=_DEFAULT_LOCK_TIMEOUT,
            maximum=_MAX_LOCK_TIMEOUT,
            code="lock_timeout must be finite and bounded",
        )
        normalized_refresh_timeout = _validate_duration(
            refresh_timeout,
            default=_DEFAULT_REFRESH_TIMEOUT,
            maximum=_MAX_REFRESH_TIMEOUT,
            code="refresh_timeout must be finite and bounded",
        )
        self.max_file_bytes = normalized_max_file_bytes
        self.lock_timeout = normalized_lock_timeout
        self.refresh_timeout = normalized_refresh_timeout
        self.lock_path = Path(f"{self.path}.lock")
        self._filesystem_supported = self._supported()
        self._anchor_fd = self._open_trusted_anchor()
        try:
            info = os.fstat(self._anchor_fd)
        except OSError:
            self._release_fd(self._anchor_fd)
            info = None
        if info is None:
            raise CredentialPermissionError("parent_stat_failed")
        self._parent_identity = (info.st_dev, info.st_ino)
        self._parent_metadata = (
            info.st_dev,
            info.st_uid,
            info.st_gid,
            stat.S_IMODE(info.st_mode),
        )
        self._last_fingerprint: bytes | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(application_owned=True)"

    def close(self) -> None:
        descriptor = getattr(self, "_anchor_fd", None)
        if descriptor is not None:
            object.__setattr__(self, "_anchor_fd", None)
            self._release_fd(descriptor)

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup path
        with contextlib.suppress(Exception):
            self.close()

    async def load(self) -> OpenAICodexCredentials:
        value: object | None = None
        failure: CodexCredentialError | None = None
        try:
            value = await _await_task_drained(asyncio.to_thread(self._load_sync))
        except asyncio.CancelledError:
            raise
        except CodexCredentialError as error:
            failure = _clone_credential_error(
                error, CredentialPersistenceError("backend_load_failed")
            )
        except BaseException:  # noqa: BLE001 - backend details are untrusted
            failure = CredentialPersistenceError("backend_load_failed")
        if failure is not None:
            raise failure
        return cast(OpenAICodexCredentials, value)

    async def save(self, credentials: OpenAICodexCredentials) -> None:
        failure: CodexCredentialError | None = None
        try:
            await _await_task_drained(asyncio.to_thread(self._save_sync, credentials))
        except asyncio.CancelledError:
            raise
        except CodexCredentialError as error:
            failure = _clone_credential_error(
                error, CredentialPersistenceError("backend_save_failed")
            )
        except BaseException:  # noqa: BLE001 - backend details are untrusted
            failure = CredentialPersistenceError("backend_save_failed")
        if failure is not None:
            raise failure

    async def recover(self, credentials: OpenAICodexCredentials) -> None:
        failure: CodexCredentialError | None = None
        try:
            await _await_task_drained(
                asyncio.to_thread(self._recover_sync, credentials)
            )
        except asyncio.CancelledError:
            raise
        except CodexCredentialError as error:
            failure = _clone_credential_error(
                error, CredentialPersistenceError("backend_recovery_failed")
            )
        except BaseException:  # noqa: BLE001 - backend details are untrusted
            failure = CredentialPersistenceError("backend_recovery_failed")
        if failure is not None:
            raise failure

    @asynccontextmanager
    async def refresh_lock(
        self, *, timeout: float | None = None
    ) -> AsyncIterator[None]:
        descriptor = await self._acquire_async(timeout)
        try:
            yield None
        finally:
            await _await_task_drained(asyncio.to_thread(self._release_fd, descriptor))

    @contextlib.contextmanager
    def refresh_lock_sync(self, *, timeout: float | None = None):
        descriptor = self._acquire_sync(timeout)
        try:
            yield None
        finally:
            self._release_fd(descriptor)

    async def _invoke_callback(
        self,
        callback: CredentialRefreshCallback,
        current: OpenAICodexCredentials,
        deadline: float,
    ) -> object:
        if not _is_async_refresh_callback(callback):
            raise CredentialRefreshError("refresh_callback_async_required")
        if time.monotonic() > deadline:
            raise CredentialRefreshError("refresh_timeout")
        result = callback(current)
        if not inspect.isawaitable(result):
            raise CredentialRefreshError("refresh_callback_async_required")
        return await _await_before_deadline(
            cast(Awaitable[Any], result),
            deadline,
            timeout_code="refresh_callback_timeout",
        )

    async def refresh(
        self,
        refresh_callback: CredentialRefreshCallback,
        *,
        expected: OpenAICodexCredentials | None = None,
        timeout: float | None = None,
    ) -> OpenAICodexCredentials:
        failure: CodexCredentialError | None = None
        value: object | None = None
        try:
            value = await self._refresh_async_impl(
                refresh_callback, expected=expected, timeout=timeout
            )
        except asyncio.CancelledError:
            raise
        except CodexCredentialError as error:
            failure = _clone_credential_error(
                error, CredentialRefreshError("backend_refresh_failed")
            )
        except BaseException:  # noqa: BLE001 - callback/provider details are untrusted
            failure = CredentialRefreshError("backend_refresh_failed")
        if failure is not None:
            raise failure
        return cast(OpenAICodexCredentials, value)

    async def _refresh_async_impl(
        self,
        refresh_callback: CredentialRefreshCallback,
        *,
        expected: OpenAICodexCredentials | None = None,
        timeout: float | None = None,
    ) -> OpenAICodexCredentials:
        if not callable(refresh_callback):
            raise CredentialRefreshError("refresh_callback_invalid")
        duration = _validate_duration(
            timeout,
            default=self.refresh_timeout,
            maximum=_MAX_REFRESH_TIMEOUT,
            code="refresh_timeout must be finite and bounded",
        )
        deadline = time.monotonic() + duration
        descriptor = await self._acquire_async(max(0.0, deadline - time.monotonic()))
        if time.monotonic() > deadline:
            await _await_task_drained(asyncio.to_thread(self._release_fd, descriptor))
            raise CredentialRefreshError("refresh_timeout")
        try:
            current = await self._read_locked_async(descriptor, deadline=deadline)
            if expected is not None and not _same_credentials(current, expected):
                self._last_fingerprint = _fingerprint(current)
                return current
            callback_failure: str | None = None
            try:
                candidate = await self._invoke_callback(
                    refresh_callback, current, deadline
                )
            except asyncio.CancelledError:
                raise
            except CredentialRefreshError as error:
                callback_failure = error.code
                candidate = None
            except BaseException:  # noqa: BLE001 - provider callback text is untrusted
                callback_failure = "refresh_callback_failed"
                candidate = None
            if callback_failure is not None:
                raise CredentialRefreshError(callback_failure)
            invalid_result = False
            try:
                rotated = _coerce_credentials(candidate, path=self.path)
            except BaseException:  # noqa: BLE001 - foreign callback result is untrusted
                invalid_result = True
                rotated = None
            if invalid_result:
                raise CredentialRefreshError("refresh_result_invalid")
            assert rotated is not None
            if time.monotonic() > deadline:
                raise CredentialRefreshError("refresh_timeout")
            try:
                await self._save_locked_async(descriptor, rotated, deadline=deadline)
            except CredentialRefreshError:
                raise
            except CredentialPersistenceError as error:
                persistence_code = error.code
                persistence_failure = CredentialPersistenceError(persistence_code)
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 - persistence details are untrusted
                persistence_failure = CredentialPersistenceError("rotation_failed")
            else:
                persistence_failure = None
            if persistence_failure is not None:
                raise persistence_failure
            if time.monotonic() > deadline:
                raise CredentialRefreshError("refresh_timeout")
            self._last_fingerprint = _fingerprint(rotated)
            return rotated
        finally:
            await _await_task_drained(asyncio.to_thread(self._release_fd, descriptor))

    def load_sync(self) -> OpenAICodexCredentials:
        failure: CodexCredentialError | None = None
        try:
            return self._load_sync()
        except CodexCredentialError as error:
            failure = _clone_credential_error(
                error, CredentialPersistenceError("backend_load_failed")
            )
        except BaseException:  # noqa: BLE001 - backend details are untrusted
            failure = CredentialPersistenceError("backend_load_failed")
        assert failure is not None
        raise failure

    def save_sync(self, credentials: OpenAICodexCredentials) -> None:
        failure: CodexCredentialError | None = None
        try:
            self._save_sync(credentials)
            return
        except CodexCredentialError as error:
            failure = _clone_credential_error(
                error, CredentialPersistenceError("backend_save_failed")
            )
        except BaseException:  # noqa: BLE001 - backend details are untrusted
            failure = CredentialPersistenceError("backend_save_failed")
        assert failure is not None
        raise failure

    def recover_sync(self, credentials: OpenAICodexCredentials) -> None:
        failure: CodexCredentialError | None = None
        try:
            self._recover_sync(credentials)
            return
        except CodexCredentialError as error:
            failure = _clone_credential_error(
                error, CredentialPersistenceError("backend_recovery_failed")
            )
        except BaseException:  # noqa: BLE001 - backend details are untrusted
            failure = CredentialPersistenceError("backend_recovery_failed")
        assert failure is not None
        raise failure

    def refresh_sync(
        self,
        refresh_callback: CredentialRefreshCallback,
        *,
        expected: OpenAICodexCredentials | None = None,
        timeout: float | None = None,
    ) -> OpenAICodexCredentials:
        failure: CodexCredentialError | None = None
        value: object | None = None
        try:
            value = self._refresh_sync_impl(
                refresh_callback, expected=expected, timeout=timeout
            )
        except CodexCredentialError as error:
            failure = _clone_credential_error(
                error, CredentialRefreshError("backend_refresh_failed")
            )
        except BaseException:  # noqa: BLE001 - callback/provider details are untrusted
            failure = CredentialRefreshError("backend_refresh_failed")
        if failure is not None:
            raise failure
        return cast(OpenAICodexCredentials, value)

    def _refresh_sync_impl(
        self,
        refresh_callback: CredentialRefreshCallback,
        *,
        expected: OpenAICodexCredentials | None,
        timeout: float | None,
    ) -> OpenAICodexCredentials:
        del expected, timeout
        if not callable(refresh_callback):
            raise CredentialRefreshError("refresh_callback_invalid")
        # There is no killable, portable way to bound an arbitrary synchronous
        # callback while this method owns the refresh lock.  Keep the legacy
        # entry point fail-closed rather than invoking one.
        raise CredentialRefreshError("refresh_callback_async_required")


class OpenAICodexCredentialSource:
    """Replaceable storage facade with an explicit atomic refresh operation.

    The broader backend protocol remains available to applications that review
    another secret store separately.  The pinned PydanticAI adapter accepts this
    concrete facade; only its audited file backend carries the local POSIX
    timeout and cancellation contract described by that adapter.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str | None = None,
        *,
        backend: CodexCredentialBackend | None = None,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
        refresh_timeout: float = _DEFAULT_REFRESH_TIMEOUT,
    ) -> None:
        if (path is None) == (backend is None):
            raise ValueError("provide exactly one credential path or backend")
        normalized_max_file_bytes = _validate_int(
            max_file_bytes,
            minimum=256,
            maximum=_MAX_FILE_BYTES,
            code="max_file_bytes must be bounded",
        )
        normalized_lock_timeout = _validate_duration(
            lock_timeout,
            default=_DEFAULT_LOCK_TIMEOUT,
            maximum=_MAX_LOCK_TIMEOUT,
            code="lock_timeout must be finite and bounded",
        )
        normalized_refresh_timeout = _validate_duration(
            refresh_timeout,
            default=_DEFAULT_REFRESH_TIMEOUT,
            maximum=_MAX_REFRESH_TIMEOUT,
            code="refresh_timeout must be finite and bounded",
        )
        if backend is None:
            backend = FileCodexCredentialBackend(
                cast(os.PathLike[str] | str, path),
                max_file_bytes=normalized_max_file_bytes,
                lock_timeout=normalized_lock_timeout,
                refresh_timeout=normalized_refresh_timeout,
            )
        self._backend = backend
        self.path = getattr(backend, "path", None)
        self.lock_path = getattr(backend, "lock_path", None)
        self.refresh_timeout = normalized_refresh_timeout

    def __repr__(self) -> str:
        return f"{type(self).__name__}(application_owned=True)"

    @staticmethod
    def _clone_error(
        error: CodexCredentialError, fallback: CodexCredentialError
    ) -> CodexCredentialError:
        return _clone_credential_error(error, fallback)

    async def load(self) -> OpenAICodexCredentials:
        failure: CodexCredentialError | None = None
        try:
            value = await self._backend.load()
        except CodexCredentialError as error:
            failure = self._clone_error(
                error, CredentialPersistenceError("backend_load_failed")
            )
            value = None
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - replaceable backend text is untrusted
            failure = CredentialPersistenceError("backend_load_failed")
            value = None
        if failure is not None:
            raise failure
        invalid = False
        try:
            result = _coerce_credentials(value, path=self.path)
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - backend values are untrusted
            invalid = True
            result = None
        if invalid:
            raise CredentialCorruptionError("backend_value_invalid")
        assert result is not None
        return result

    async def save(self, credentials: OpenAICodexCredentials) -> None:
        value = _coerce_credentials(credentials, path=self.path)
        failure: CodexCredentialError | None = None
        try:
            await self._backend.save(value)
        except CodexCredentialError as error:
            failure = self._clone_error(
                error, CredentialPersistenceError("backend_save_failed")
            )
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - replaceable backend text is untrusted
            failure = CredentialPersistenceError("backend_save_failed")
        if failure is not None:
            raise failure

    async def refresh(
        self,
        refresh_callback: CredentialRefreshCallback,
        *,
        expected: OpenAICodexCredentials | None = None,
        timeout: float | None = None,
    ) -> OpenAICodexCredentials:
        if not callable(refresh_callback):
            raise CredentialRefreshError("refresh_callback_invalid")
        if not _is_async_refresh_callback(refresh_callback):
            raise CredentialRefreshError("refresh_callback_async_required")
        duration = _validate_duration(
            timeout,
            default=self.refresh_timeout,
            maximum=_MAX_REFRESH_TIMEOUT,
            code="refresh_timeout must be finite and bounded",
        )
        failure: CodexCredentialError | None = None
        value: object | None = None
        try:
            # Do not duck-type a timeout marker here.  The file backend owns
            # the cooperative deadline/drain implementation; arbitrary
            # replaceable backends make no finite-time guarantee.
            value = await self._backend.refresh(
                refresh_callback, expected=expected, timeout=duration
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            failure = CredentialRefreshError("refresh_timeout")
        except CodexCredentialError as error:
            failure = self._clone_error(
                error, CredentialRefreshError("backend_refresh_failed")
            )
        except BaseException:  # noqa: BLE001 - replaceable backend text is untrusted
            failure = CredentialRefreshError("backend_refresh_failed")
        if failure is not None:
            raise failure
        invalid = False
        try:
            result = _coerce_credentials(value, path=self.path)
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - backend result is untrusted
            invalid = True
            result = None
        if invalid:
            raise CredentialRefreshError("backend_result_invalid")
        assert result is not None
        return result

    async def recover(self, credentials: OpenAICodexCredentials) -> None:
        recover = getattr(self._backend, "recover", None)
        if recover is None or not callable(recover):
            raise CredentialPersistenceError("backend_recovery_unsupported")
        value = _coerce_credentials(credentials, path=self.path)
        failure: CodexCredentialError | None = None
        try:
            await recover(value)
        except CodexCredentialError as error:
            failure = self._clone_error(
                error, CredentialPersistenceError("backend_recovery_failed")
            )
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - replaceable backend text is untrusted
            failure = CredentialPersistenceError("backend_recovery_failed")
        if failure is not None:
            raise failure

    @asynccontextmanager
    async def refresh_lock(
        self, *, timeout: float | None = None
    ) -> AsyncIterator[None]:
        lock = getattr(self._backend, "refresh_lock", None)
        if lock is None or not callable(lock):
            raise CredentialLockError("backend_lock_unsupported")
        failure: CodexCredentialError | None = None
        context = None
        try:
            context = lock(timeout=timeout)
            await context.__aenter__()
        except CodexCredentialError as error:
            failure = self._clone_error(
                error, CredentialLockError("backend_lock_failed")
            )
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - replaceable backend text is untrusted
            failure = CredentialLockError("backend_lock_failed")
        if failure is not None:
            raise failure
        assert context is not None
        try:
            yield None
        finally:
            exit_failure: CodexCredentialError | None = None
            try:
                await context.__aexit__(None, None, None)
            except CodexCredentialError as error:
                exit_failure = self._clone_error(
                    error, CredentialLockError("backend_unlock_failed")
                )
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 - backend unlock details are untrusted
                exit_failure = CredentialLockError("backend_unlock_failed")
            if exit_failure is not None:
                raise exit_failure


FileCredentialSource = OpenAICodexCredentialSource


# ---------------------------------------------------------------------------
# Bounded provider pressure


class ProviderGeneration:
    """Validated, bounded sequence identity for one pressure scope."""

    __slots__ = ("scope", "sequence")

    def __init__(self, scope: int, sequence: int) -> None:
        if type(scope) is not int or type(sequence) is not int:
            raise TypeError("generation scope and sequence must be integers")
        if not 0 <= scope <= _MAX_GENERATION_VALUE:
            raise ValueError("generation scope is out of bounds")
        if not 1 <= sequence <= _MAX_GENERATION_VALUE:
            raise ValueError("generation sequence is out of bounds")
        self.scope = scope
        self.sequence = sequence

    def __repr__(self) -> str:
        return f"<ProviderGeneration scope={self.scope} sequence={self.sequence}>"

    def __hash__(self) -> int:
        return hash((self.scope, self.sequence))

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is ProviderGeneration
            and self.scope == other.scope
            and self.sequence == other.sequence
        )


class ProviderCompletion:
    """Opaque outcome returned when a generation was already settled."""

    __slots__ = ("status",)

    def __init__(self, status: str = "completed") -> None:
        self.status = _safe_code(status, "completed")

    def __repr__(self) -> str:
        return f"<ProviderCompletion status={self.status!r}>"


class _CompletionMetadata:
    __slots__ = ("status",)

    def __init__(self, status: str) -> None:
        self.status = _safe_code(status, "completed")


class _ScrubbableFuture(asyncio.Future[None]):
    """Signal completion without storing the operation result in the Future."""

    __slots__ = ("_delivery",)

    def __init__(self) -> None:
        super().__init__()
        self._delivery: Any = None

    def set_result(self, result: Any) -> None:
        # asyncio.Future keeps its result for the lifetime of the object.  Keep
        # the credential in a private delivery slot only while participants can
        # still observe it, and resolve the actual Future with a null signal.
        self._delivery = result
        super().set_result(None)

    def delivered_result(self) -> Any:
        return self._delivery

    def scrub(self) -> None:
        self._delivery = None


class _SharedOperation:
    __slots__ = ("future", "participants", "published")

    def __init__(self, future: asyncio.Future[Any]) -> None:
        self.future: asyncio.Future[Any] | None = future
        self.participants = 1
        self.published = False


class ProviderAdmission:
    """A capacity lease owned by :class:`ProviderPressure`."""

    __slots__ = ("_identity", "_pressure", "generation")

    def __init__(
        self,
        pressure: ProviderPressure,
        generation: ProviderGeneration | None,
        identity: int,
    ) -> None:
        self._pressure = pressure
        self.generation = generation
        self._identity = identity

    def __repr__(self) -> str:
        return "<ProviderAdmission active>"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.release()

    def release(self) -> None:
        self._pressure._release(self)


class ProviderPressure:
    """Finite provider concurrency with secret-free, bounded single-flight state.

    An active future can deliver an operation's result to its current owner and
    waiters.  It is scrubbed as soon as those participants settle.  Completed
    state contains only a safe status code; a replayed generation therefore
    cannot recover a credential or re-run a refresh after bounded metadata is
    evicted.
    """

    def __init__(
        self,
        capacity: int = 1,
        *,
        admission_timeout: float = _DEFAULT_ADMISSION_TIMEOUT,
        max_backoff: float = _DEFAULT_MAX_BACKOFF,
        max_queue: int = _DEFAULT_MAX_QUEUE,
        max_retained_generations: int = _DEFAULT_MAX_RETAINED_GENERATIONS,
        max_generation_scopes: int = _DEFAULT_MAX_GENERATION_SCOPES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = _validate_int(
            capacity,
            minimum=1,
            maximum=_MAX_CAPACITY,
            code="capacity must be a bounded positive integer",
        )
        self.max_queue = _validate_int(
            max_queue,
            minimum=1,
            maximum=_MAX_QUEUE,
            code="max_queue must be a bounded positive integer",
        )
        self.max_retained_generations = _validate_int(
            max_retained_generations,
            minimum=1,
            maximum=_MAX_RETAINED_GENERATIONS,
            code="max_retained_generations must be a bounded positive integer",
        )
        self.max_generation_scopes = _validate_int(
            max_generation_scopes,
            minimum=1,
            maximum=_MAX_GENERATION_SCOPES,
            code="max_generation_scopes must be a bounded positive integer",
        )
        self.admission_timeout = _validate_duration(
            admission_timeout,
            default=_DEFAULT_ADMISSION_TIMEOUT,
            maximum=_MAX_ADMISSION_TIMEOUT,
            code="admission_timeout must be finite and bounded",
        )
        self.max_backoff = _validate_duration(
            max_backoff,
            default=_DEFAULT_MAX_BACKOFF,
            maximum=_MAX_BACKOFF,
            code="max_backoff must be finite and bounded",
        )
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._state_lock: asyncio.Lock | None = None
        self._wake_event: asyncio.Event | None = None
        self._queue: deque[object] = deque()
        self._in_flight = 0
        self._blocked_until = 0.0
        self._leases: dict[int, ProviderAdmission] = {}
        self._scope_lock = threading.RLock()
        self._scopes = {0}
        self._free_scopes: set[int] = set()
        self._next_scope = 1
        self._generation_high_water: dict[int, int] = {0: 0}
        self._active: dict[ProviderGeneration, _SharedOperation] = {}
        self._completed: OrderedDict[ProviderGeneration, _CompletionMetadata] = (
            OrderedDict()
        )
        self._rate_limit_high_water: dict[int, int] = {0: 0}
        self._rate_limit_deadlines: dict[int, float] = {}

    def __repr__(self) -> str:
        return "<ProviderPressure bounded application state>"

    def new_scope(self) -> int:
        """Reserve one bounded provider scope for a shared pressure object."""

        with self._scope_lock:
            if self._free_scopes:
                scope = min(self._free_scopes)
                self._free_scopes.remove(scope)
            else:
                if len(self._scopes) >= self.max_generation_scopes:
                    raise ProviderOperationError("generation_scope_limit")
                if self._next_scope > _MAX_GENERATION_VALUE:
                    raise ProviderOperationError("generation_scope_exhausted")
                scope = self._next_scope
                self._next_scope += 1
            self._scopes.add(scope)
            self._generation_high_water[scope] = 0
            self._rate_limit_high_water[scope] = 0
            return scope

    def release_scope(self, scope: int) -> None:
        """Release an idle non-default scope so it can be safely reused."""

        if type(scope) is not int or scope <= 0 or scope > _MAX_GENERATION_VALUE:
            raise ProviderOperationError("generation_scope_invalid")
        with self._scope_lock:
            if scope not in self._scopes:
                return
            if any(generation.scope == scope for generation in self._active) or any(
                lease.generation is not None and lease.generation.scope == scope
                for lease in self._leases.values()
            ):
                raise ProviderOperationError("generation_scope_active")
            self._scopes.remove(scope)
            self._free_scopes.add(scope)
            self._generation_high_water.pop(scope, None)
            self._rate_limit_high_water.pop(scope, None)
            self._rate_limit_deadlines.pop(scope, None)
            for generation in tuple(self._completed):
                if generation.scope == scope:
                    del self._completed[generation]
            self._recompute_pressure()
            self._wake_waiters()

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            raise ProviderLoopError("event_loop_required")
        if self._loop is None:
            self._loop = loop
            self._state_lock = asyncio.Lock()
            self._wake_event = asyncio.Event()
        elif self._loop is not loop:
            raise ProviderLoopError()
        return loop

    def _normalize_generation(
        self, generation: object | None, *, allow_none: bool = True
    ) -> ProviderGeneration | None:
        if generation is None:
            if allow_none:
                return None
            raise TypeError("generation is required")
        if type(generation) is int:
            scope = 0
            sequence = generation
        elif type(generation) is ProviderGeneration:
            scope = generation.scope
            sequence = generation.sequence
        else:
            raise TypeError("generation must be a bounded integer sequence")
        if type(scope) is not int or type(sequence) is not int:
            raise TypeError("generation scope and sequence must be integers")
        if not 0 <= scope <= _MAX_GENERATION_VALUE:
            raise ValueError("generation scope is out of bounds")
        if not 1 <= sequence <= _MAX_GENERATION_VALUE:
            raise ValueError("generation sequence is out of bounds")
        with self._scope_lock:
            registered = scope in self._scopes
        if not registered:
            raise ValueError("generation scope is not registered")
        return ProviderGeneration(scope, sequence)

    def _assert_generation(self, generation: object) -> ProviderGeneration:
        normalized = self._normalize_generation(generation, allow_none=False)
        assert normalized is not None
        return normalized

    @property
    def queued(self) -> int:
        return len(self._queue)

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def blocked_until(self) -> float:
        return self._blocked_until

    def pressure_state(self) -> dict[str, int | float]:
        return {
            "capacity": self.capacity,
            "in_flight": self._in_flight,
            "queued": len(self._queue),
            "blocked_until": self._blocked_until,
        }

    def _wake_waiters(self) -> None:
        loop = self._loop
        event = self._wake_event
        if loop is None or event is None:
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            event.set()
        elif loop.is_closed():
            return
        else:
            loop.call_soon_threadsafe(event.set)

    def _require_sync_loop(self) -> None:
        loop = self._bind_loop()
        if loop.is_closed():
            raise ProviderLoopError("event_loop_closed")

    def _recompute_pressure(self) -> None:
        self._blocked_until = max(
            self._rate_limit_deadlines.values(), default=self._clock()
        )

    def observe_rate_limit(
        self, retry_after: float | None, *, generation: object | None = None
    ) -> None:
        self._require_sync_loop()
        if retry_after is None:
            return
        if type(retry_after) not in {int, float}:
            return
        try:
            retry_value = float(retry_after)
        except (OverflowError, ValueError):
            return
        if not math.isfinite(retry_value):
            return
        try:
            normalized = self._normalize_generation(generation)
        except (OverflowError, TypeError, ValueError):
            raise ProviderOperationError("generation_invalid") from None
        if normalized is None:
            current = self._rate_limit_high_water[0]
            if current >= _MAX_GENERATION_VALUE:
                raise ProviderOperationError("generation_exhausted")
            normalized = ProviderGeneration(0, current + 1)
        high_water = self._rate_limit_high_water[normalized.scope]
        if normalized.sequence <= high_water:
            return
        self._rate_limit_high_water[normalized.scope] = normalized.sequence
        delay = max(0.0, min(retry_value, self.max_backoff))
        self._rate_limit_deadlines[normalized.scope] = self._clock() + delay
        self._recompute_pressure()
        self._wake_waiters()

    record_rate_limit = observe_rate_limit

    def clear_pressure(self, *, generation: object | None = None) -> None:
        self._require_sync_loop()
        try:
            normalized = self._normalize_generation(generation)
        except (OverflowError, TypeError, ValueError):
            raise ProviderOperationError("generation_invalid") from None
        if normalized is None:
            self._rate_limit_deadlines.clear()
        elif normalized.sequence == self._rate_limit_high_water[normalized.scope]:
            self._rate_limit_deadlines.pop(normalized.scope, None)
        self._recompute_pressure()
        self._wake_waiters()

    def _release(self, lease: ProviderAdmission) -> None:
        self._require_sync_loop()
        current = self._leases.pop(lease._identity, None)
        if current is None:
            return
        self._in_flight = max(0, self._in_flight - 1)
        self._wake_waiters()

    async def acquire(
        self, *, timeout: float | None = None, generation: object | None = None
    ) -> ProviderAdmission:
        self._bind_loop()
        try:
            normalized = self._normalize_generation(generation)
        except (OverflowError, TypeError, ValueError):
            raise ProviderOperationError("generation_invalid") from None
        try:
            duration = _validate_duration(
                timeout,
                default=self.admission_timeout,
                maximum=_MAX_ADMISSION_TIMEOUT,
                code="admission_timeout must be finite and bounded",
            )
        except (OverflowError, TypeError, ValueError):
            raise ProviderOperationError("admission_timeout_invalid") from None
        assert self._state_lock is not None
        assert self._wake_event is not None
        deadline = self._clock() + duration
        ticket = object()
        async with self._state_lock:
            if len(self._queue) >= self.max_queue:
                raise ProviderQueueFullError()
            self._queue.append(ticket)
        while True:
            try:
                async with self._state_lock:
                    now = self._clock()
                    if (
                        self._queue
                        and self._queue[0] is ticket
                        and self._in_flight < self.capacity
                        and now >= self._blocked_until
                    ):
                        self._queue.popleft()
                        self._in_flight += 1
                        lease = ProviderAdmission(self, normalized, id(ticket))
                        self._leases[id(ticket)] = lease
                        return lease
                    remaining = deadline - now
                    if remaining <= 0:
                        with contextlib.suppress(ValueError):
                            self._queue.remove(ticket)
                        self._wake_event.set()
                        raise PressureAdmissionTimeoutError()
                    pressure_wait = max(0.0, self._blocked_until - now)
                    self._wake_event.clear()
                    wait_timeout = min(
                        remaining, pressure_wait if pressure_wait else 0.05
                    )
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(self._remove_ticket(ticket))
                await _await_task_drained(cleanup)
                raise
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=wait_timeout)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(self._remove_ticket(ticket))
                await _await_task_drained(cleanup)
                raise

    async def _remove_ticket(self, ticket: object) -> None:
        """Remove a queued admission even when cancellation hit the state lock."""

        assert self._state_lock is not None
        async with self._state_lock:
            with contextlib.suppress(ValueError):
                self._queue.remove(ticket)
            assert self._wake_event is not None
            self._wake_event.set()

    @asynccontextmanager
    async def slot(
        self, *, timeout: float | None = None, generation: object | None = None
    ) -> AsyncIterator[ProviderAdmission]:
        lease = await self.acquire(timeout=timeout, generation=generation)
        try:
            yield lease
        finally:
            lease.release()

    @staticmethod
    def _scrub_future(future: asyncio.Future[Any]) -> None:
        # asyncio.Future has no public clear-result API.  This is performed only
        # after every participant has settled, never while a waiter can observe
        # the delivery.
        if isinstance(future, _ScrubbableFuture):
            future.scrub()
        with contextlib.suppress(AttributeError):
            future._result = None  # type: ignore[attr-defined]
        with contextlib.suppress(AttributeError):
            future._exception = None  # type: ignore[attr-defined]
        with contextlib.suppress(AttributeError):
            future._log_traceback = False  # type: ignore[attr-defined]
        with contextlib.suppress(AttributeError):
            future._callbacks = None  # type: ignore[attr-defined]

    async def _settle_participant(self, shared: _SharedOperation) -> None:
        """Drop one waiter under the state lock, then scrub its delivery."""

        assert self._state_lock is not None
        future_to_scrub: asyncio.Future[Any] | None = None
        async with self._state_lock:
            if shared.participants > 0:
                shared.participants -= 1
            if shared.participants == 0 and shared.future is not None:
                future_to_scrub = shared.future
                shared.future = None
        if future_to_scrub is not None:
            self._scrub_future(future_to_scrub)

    async def _settle_owner(
        self,
        normalized: ProviderGeneration,
        shared: _SharedOperation,
        result: Any,
        safe_failure: CodexCredentialError | None,
        completion_status: str,
    ) -> None:
        """Commit pressure state before publishing a result to any waiter.

        This coroutine is always shielded and drained by its caller.  The state
        lock transaction removes the active entry, records only categorical
        completion metadata, and accounts for the owner before the future can
        expose a value.  A successful value is therefore never published while
        the operation is still active or before its owner bookkeeping is done.
        """

        assert self._state_lock is not None
        future_to_publish: asyncio.Future[Any] | None = None
        future_to_scrub: asyncio.Future[Any] | None = None
        async with self._state_lock:
            if self._active.get(normalized) is shared:
                self._active.pop(normalized, None)
            self._completed[normalized] = _CompletionMetadata(completion_status)
            self._completed.move_to_end(normalized)
            while len(self._completed) > self.max_retained_generations:
                self._completed.popitem(last=False)
            if shared.participants > 0:
                shared.participants -= 1
            future = shared.future
            if shared.participants == 0:
                if future is not None:
                    future_to_scrub = future
                shared.future = None
            elif future is not None and not shared.published:
                shared.published = True
                future_to_publish = future

        if future_to_publish is not None:
            if safe_failure is None:
                future_to_publish.set_result(result)
            else:
                future_to_publish.set_exception(safe_failure)
                with contextlib.suppress(BaseException):
                    future_to_publish.exception()
        if future_to_scrub is not None:
            self._scrub_future(future_to_scrub)

    async def run(
        self,
        generation: object,
        operation: Callable[[], Any | Awaitable[Any]],
        *,
        timeout: float | None = None,
    ) -> Any:
        self._bind_loop()
        try:
            normalized = self._assert_generation(generation)
        except (OverflowError, TypeError, ValueError):
            raise ProviderOperationError("generation_invalid") from None
        if not callable(operation):
            raise ProviderOperationError("operation_invalid")
        assert self._state_lock is not None
        owner = False
        shared: _SharedOperation | None = None
        async with self._state_lock:
            shared = self._active.get(normalized)
            if shared is not None:
                shared.participants += 1
            else:
                high_water = self._generation_high_water[normalized.scope]
                if normalized.sequence <= high_water or normalized in self._completed:
                    metadata = self._completed.get(normalized)
                    return ProviderCompletion(
                        metadata.status if metadata is not None else "replayed"
                    )
                if len(self._active) >= self.capacity + self.max_queue:
                    raise ProviderQueueFullError()
                self._generation_high_water[normalized.scope] = normalized.sequence
                future = _ScrubbableFuture()
                shared = _SharedOperation(future)
                self._active[normalized] = shared
                owner = True
        assert shared is not None
        if not owner:
            assert shared.future is not None
            future = shared.future
            try:
                await asyncio.shield(future)
                if isinstance(future, _ScrubbableFuture):
                    return future.delivered_result()
                return future.result()
            finally:
                cleanup = asyncio.create_task(self._settle_participant(shared))
                await _await_task_drained(cleanup)

        failure_code: str | None = None
        cancelled = False
        result: Any = None
        try:
            try:
                duration = _validate_duration(
                    timeout,
                    default=self.admission_timeout,
                    maximum=_MAX_ADMISSION_TIMEOUT,
                    code="operation_timeout must be finite and bounded",
                )
            except (OverflowError, TypeError, ValueError):
                raise ProviderOperationError("operation_timeout_invalid") from None
            async with asyncio.timeout(duration):
                async with self.slot(timeout=duration, generation=normalized):
                    result = operation()
                    if inspect.isawaitable(result):
                        result = await result
        except asyncio.CancelledError:
            failure_code = "operation_cancelled"
            cancelled = True
        except CodexCredentialError as error:
            if _is_credential_storage_error(error):
                failure_code = "credential_persistence"
            elif isinstance(error, PressureAdmissionTimeoutError):
                failure_code = "admission_timeout"
            elif isinstance(error, ProviderQueueFullError):
                failure_code = "queue_full"
            elif isinstance(error, CredentialRefreshError):
                failure_code = "provider_refresh_failed"
            elif isinstance(error, ProviderOperationError):
                failure_code = _safe_code(error.code, "operation_aborted")
            else:
                failure_code = "operation_aborted"
        except TimeoutError:
            failure_code = "operation_timeout"
        except BaseException:  # noqa: BLE001 - every waiter receives a safe error
            failure_code = "operation_aborted"

        safe_failure: CodexCredentialError | None = None
        if failure_code == "admission_timeout":
            safe_failure = PressureAdmissionTimeoutError()
        elif failure_code == "queue_full":
            safe_failure = ProviderQueueFullError()
        elif failure_code is not None:
            safe_failure = ProviderOperationError(failure_code)
        completion_status = "completed" if safe_failure is None else safe_failure.code

        settlement = asyncio.create_task(
            self._settle_owner(
                normalized,
                shared,
                result,
                safe_failure,
                completion_status,
            )
        )
        await _await_task_drained(settlement)

        if cancelled:
            raise asyncio.CancelledError
        if safe_failure is not None:
            raise safe_failure
        return result


CodexProviderPressure = ProviderPressure
