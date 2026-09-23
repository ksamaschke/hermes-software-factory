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
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
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
    "ProviderLoopError",
    "ProviderOperationError",
    "ProviderPressure",
    "ProviderQueueFullError",
)


_MAX_CREDENTIAL_FIELD_LENGTH = 128 * 1024
_DEFAULT_MAX_FILE_BYTES = 512 * 1024
_DEFAULT_LOCK_TIMEOUT = 5.0
_MAX_LOCK_TIMEOUT = 300.0
_DEFAULT_REFRESH_TIMEOUT = 30.0
_MAX_REFRESH_TIMEOUT = 300.0
_DEFAULT_ADMISSION_TIMEOUT = 30.0
_MAX_ADMISSION_TIMEOUT = 3_600.0
_DEFAULT_MAX_BACKOFF = 60.0
_DEFAULT_MAX_QUEUE = 128
_DEFAULT_MAX_RETAINED_GENERATIONS = 128

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
    [OpenAICodexCredentials],
    OpenAICodexCredentials | Awaitable[OpenAICodexCredentials],
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
    if type(resolved) not in {int, float} or not math.isfinite(float(resolved)):
        raise ValueError(code)
    if float(resolved) < 0 or float(resolved) > maximum:
        raise ValueError(code)
    return float(resolved)


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

    def _open_trusted_anchor(self) -> int:
        if not self._supported():
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

    def _validate_anchor(self, descriptor: int) -> CodexCredentialError | None:
        try:
            info = os.fstat(descriptor)
        except OSError:
            return CredentialPermissionError("parent_stat_failed")
        if (info.st_dev, info.st_ino) != self._parent_identity:
            return CredentialPermissionError("parent_changed_identity")
        return _directory_error(info)

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

    def _open_operation_parent(self) -> tuple[int | None, CodexCredentialError | None]:
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
        anchor_fd = getattr(self, "_anchor_fd", None)
        if type(anchor_fd) is not int:
            return None, CredentialPermissionError("backend_closed")
        try:
            descriptor = os.open(".", flags, dir_fd=anchor_fd)
        except OSError:
            return None, CredentialPersistenceError("parent_open_failed")
        failure = self._validate_anchor(descriptor) or self._lock_marker_error(
            descriptor
        )
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
        failure = self._lock_marker_error(descriptor)
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
                descriptor, _failure = await asyncio.shield(task)
                if descriptor is not None:
                    await asyncio.to_thread(self._release_fd, descriptor)
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
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if read_failure is not None:
            raise read_failure
        try:
            decoded = bytes(data).decode("utf-8")
            payload = json.loads(decoded, object_pairs_hook=_strict_object_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
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
        try:
            for _ in range(32):
                candidate = f".{self.path.name}.{os.getpid()}.{time.monotonic_ns()}.{next(_TEMP_COUNTER)}.tmp"
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
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
            with contextlib.suppress(OSError):
                os.close(descriptor)
            descriptor = None

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
            try:
                os.rename(
                    temporary_name,
                    self.path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_name = None
                os.fsync(parent_fd)
            except OSError:
                write_failure = CredentialPersistenceError("atomic_replace_failed")
                raise write_failure

            published, failure = self._stat_target(parent_fd, require_mode=True)
            if failure is not None:
                write_failure = failure
                raise write_failure
            if published is None or temporary_info is None:
                write_failure = CredentialPersistenceError("published_missing")
                raise write_failure
            if (published.st_dev, published.st_ino) != (
                temporary_info.st_dev,
                temporary_info.st_ino,
            ):
                write_failure = CredentialPermissionError("published_changed_identity")
                raise write_failure
            if published.st_nlink != 1:
                write_failure = CredentialPermissionError("published_hardlink")
        except OSError:
            write_failure = CredentialPersistenceError("atomic_replace_failed")
        finally:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            if temporary_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary_name, dir_fd=parent_fd)
        if write_failure is not None:
            raise write_failure

    def _save_unlocked(
        self, parent_fd: int, credentials: OpenAICodexCredentials
    ) -> None:
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

    async def _await_blocking(self, function: Callable[..., Any], *args: Any) -> Any:
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Drain bounded local I/O before the caller closes the descriptor.
            await asyncio.shield(task)
            raise

    async def _read_locked_async(self, descriptor: int) -> OpenAICodexCredentials:
        return cast(
            OpenAICodexCredentials,
            await self._await_blocking(self._read_unlocked, descriptor),
        )

    async def _save_locked_async(
        self, descriptor: int, value: OpenAICodexCredentials
    ) -> None:
        await self._await_blocking(self._save_unlocked, descriptor, value)

    async def _recover_locked_async(
        self, descriptor: int, value: OpenAICodexCredentials
    ) -> None:
        await self._await_blocking(self._recover_unlocked, descriptor, value)


class FileCodexCredentialBackend(_CredentialPathMixin):
    """Secure JSON backend using a trusted-directory flock and atomic replacement.

    Supported semantics are POSIX local filesystems that implement descriptor-
    relative open/stat/replace/unlink, ``O_NOFOLLOW``/``O_NONBLOCK``, directory
    fsync, and directory flocking.  The constructor fails closed otherwise.
    """

    enforces_refresh_timeout = True

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
        if (
            type(max_file_bytes) is not int
            or not 256 <= max_file_bytes <= 16 * 1024 * 1024
        ):
            raise ValueError("max_file_bytes must be bounded")
        self.max_file_bytes = max_file_bytes
        self.lock_timeout = _validate_duration(
            lock_timeout,
            default=_DEFAULT_LOCK_TIMEOUT,
            maximum=_MAX_LOCK_TIMEOUT,
            code="lock_timeout must be finite and bounded",
        )
        self.refresh_timeout = _validate_duration(
            refresh_timeout,
            default=_DEFAULT_REFRESH_TIMEOUT,
            maximum=_MAX_REFRESH_TIMEOUT,
            code="refresh_timeout must be finite and bounded",
        )
        self.lock_path = Path(f"{self.path}.lock")
        self._anchor_fd = self._open_trusted_anchor()
        try:
            info = os.fstat(self._anchor_fd)
        except OSError:
            self._release_fd(self._anchor_fd)
            info = None
        if info is None:
            raise CredentialPermissionError("parent_stat_failed")
        self._parent_identity = (info.st_dev, info.st_ino)
        self._last_fingerprint: bytes | None = None
        self._callback_tasks: set[asyncio.Task[Any]] = set()
        self._callback_lock = threading.Lock()

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
            value = await asyncio.to_thread(self._load_sync)
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
            await asyncio.to_thread(self._save_sync, credentials)
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
            await asyncio.to_thread(self._recover_sync, credentials)
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
            await asyncio.to_thread(self._release_fd, descriptor)

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
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CredentialRefreshError("refresh_timeout")
        callback_callable = inspect.iscoroutinefunction(
            callback
        ) or inspect.iscoroutinefunction(type(callback).__call__)
        if callback_callable:
            result = callback(current)
        else:
            task = asyncio.create_task(asyncio.to_thread(callback, current))
            with self._callback_lock:
                self._callback_tasks.add(task)
            task.add_done_callback(self._consume_callback_task)
            callback_failure: str | None = None
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except TimeoutError:
                callback_failure = "refresh_callback_timeout"
                result = None
            if callback_failure is not None:
                raise CredentialRefreshError(callback_failure)
        if inspect.isawaitable(result):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CredentialRefreshError("refresh_timeout")
            callback_failure = None
            try:
                awaited_result = await asyncio.wait_for(
                    cast(Awaitable[Any], result), timeout=remaining
                )
            except TimeoutError:
                callback_failure = "refresh_callback_timeout"
                awaited_result = None
            if callback_failure is not None:
                raise CredentialRefreshError(callback_failure)
            return awaited_result
        return result

    def _consume_callback_task(self, task: asyncio.Task[Any]) -> None:
        with self._callback_lock:
            self._callback_tasks.discard(task)
        if not task.cancelled():
            with contextlib.suppress(BaseException):
                task.exception()

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
        try:
            current = await self._read_locked_async(descriptor)
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
                await self._save_locked_async(descriptor, rotated)
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
            self._last_fingerprint = _fingerprint(rotated)
            return rotated
        finally:
            await asyncio.to_thread(self._release_fd, descriptor)

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
        if not callable(refresh_callback):
            raise CredentialRefreshError("refresh_callback_invalid")
        duration = _validate_duration(
            timeout,
            default=self.refresh_timeout,
            maximum=_MAX_REFRESH_TIMEOUT,
            code="refresh_timeout must be finite and bounded",
        )
        deadline = time.monotonic() + duration
        descriptor = self._acquire_sync(max(0.0, deadline - time.monotonic()))
        try:
            current = self._read_unlocked(descriptor)
            if expected is not None and not _same_credentials(current, expected):
                self._last_fingerprint = _fingerprint(current)
                return current
            try:
                candidate = refresh_callback(current)
                if inspect.isawaitable(candidate):
                    raise CredentialRefreshError("refresh_callback_async")
            except CredentialRefreshError as error:
                callback_code = error.code
                candidate = None
            except BaseException:  # noqa: BLE001 - callback details are untrusted
                callback_code = "refresh_callback_failed"
                candidate = None
            else:
                callback_code = None
            if callback_code is not None:
                raise CredentialRefreshError(callback_code)
            invalid_result = False
            try:
                rotated = _coerce_credentials(candidate, path=self.path)
            except BaseException:  # noqa: BLE001 - callback result is untrusted
                invalid_result = True
                rotated = None
            if invalid_result:
                raise CredentialRefreshError("refresh_result_invalid")
            assert rotated is not None
            if time.monotonic() > deadline:
                raise CredentialRefreshError("refresh_timeout")
            self._save_unlocked(descriptor, rotated)
            self._last_fingerprint = _fingerprint(rotated)
            return rotated
        finally:
            self._release_fd(descriptor)


class OpenAICodexCredentialSource:
    """PydanticAI-shaped source with an explicit atomic refresh operation."""

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
        if backend is None:
            backend = FileCodexCredentialBackend(
                cast(os.PathLike[str] | str, path),
                max_file_bytes=max_file_bytes,
                lock_timeout=lock_timeout,
                refresh_timeout=refresh_timeout,
            )
        self._backend = backend
        self.path = getattr(backend, "path", None)
        self.lock_path = getattr(backend, "lock_path", None)
        self.refresh_timeout = _validate_duration(
            refresh_timeout,
            default=_DEFAULT_REFRESH_TIMEOUT,
            maximum=_MAX_REFRESH_TIMEOUT,
            code="refresh_timeout must be finite and bounded",
        )

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
        duration = _validate_duration(
            timeout,
            default=self.refresh_timeout,
            maximum=_MAX_REFRESH_TIMEOUT,
            code="refresh_timeout must be finite and bounded",
        )
        failure: CodexCredentialError | None = None
        value: object | None = None
        try:
            if getattr(self._backend, "enforces_refresh_timeout", False):
                value = await self._backend.refresh(
                    refresh_callback, expected=expected, timeout=duration
                )
            else:
                async with asyncio.timeout(duration):
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


class ProviderAdmission:
    """A capacity lease owned by :class:`ProviderPressure`."""

    __slots__ = ("_identity", "_pressure", "generation")

    def __init__(
        self, pressure: ProviderPressure, generation: Hashable | None, identity: int
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
    """Finite provider concurrency with immediate wakeups and single-flight.

    The queue, active operations, retained generations, and rate-limit
    observations are all bounded.  A logical generation is retained in a small
    LRU of completed safe futures, so repeated retry requests cannot re-run a
    completed operation merely because another generation was observed in
    between.  Futures may contain a successful provider result internally, but
    they are never serialized or rendered as telemetry.
    """

    def __init__(
        self,
        capacity: int = 1,
        *,
        admission_timeout: float = _DEFAULT_ADMISSION_TIMEOUT,
        max_backoff: float = _DEFAULT_MAX_BACKOFF,
        max_queue: int = _DEFAULT_MAX_QUEUE,
        max_retained_generations: int = _DEFAULT_MAX_RETAINED_GENERATIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if type(max_queue) is not int or max_queue < 1:
            raise ValueError("max_queue must be a positive integer")
        if type(max_retained_generations) is not int or max_retained_generations < 1:
            raise ValueError("max_retained_generations must be a positive integer")
        self.capacity = capacity
        self.max_queue = max_queue
        self.max_retained_generations = max_retained_generations
        self.admission_timeout = _validate_duration(
            admission_timeout,
            default=_DEFAULT_ADMISSION_TIMEOUT,
            maximum=_MAX_ADMISSION_TIMEOUT,
            code="admission_timeout must be finite and bounded",
        )
        self.max_backoff = _validate_duration(
            max_backoff,
            default=_DEFAULT_MAX_BACKOFF,
            maximum=_MAX_ADMISSION_TIMEOUT,
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
        self._active: dict[Hashable, asyncio.Future[Any]] = {}
        self._completed: OrderedDict[Hashable, asyncio.Future[Any]] = OrderedDict()
        self._rate_limit_generations: OrderedDict[Hashable, None] = OrderedDict()
        self._rate_limit_deadlines: OrderedDict[Hashable, float] = OrderedDict()
        self._anonymous_generation = 0

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

    def _assert_generation(self, generation: Hashable) -> None:
        if not isinstance(generation, Hashable):
            raise TypeError("generation must be hashable")

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

    def observe_rate_limit(
        self, retry_after: float | None, *, generation: Hashable | None = None
    ) -> None:
        self._require_sync_loop()
        if retry_after is None:
            return
        if type(retry_after) not in {int, float} or not math.isfinite(
            float(retry_after)
        ):
            return
        if generation is not None:
            self._assert_generation(generation)
            key = generation
            if key in self._rate_limit_generations:
                return
            self._rate_limit_generations[key] = None
            self._rate_limit_generations.move_to_end(key)
        else:
            self._anonymous_generation += 1
            key = ("anonymous", self._anonymous_generation)
            self._rate_limit_generations[key] = None
        delay = max(0.0, min(float(retry_after), self.max_backoff))
        self._rate_limit_deadlines[key] = self._clock() + delay
        self._rate_limit_deadlines.move_to_end(key)
        while len(self._rate_limit_generations) > self.max_retained_generations:
            old, _ = self._rate_limit_generations.popitem(last=False)
            self._rate_limit_deadlines.pop(old, None)
        self._blocked_until = max(
            self._rate_limit_deadlines.values(), default=self._clock()
        )
        self._wake_waiters()

    record_rate_limit = observe_rate_limit

    def clear_pressure(self, *, generation: Hashable | None = None) -> None:
        self._require_sync_loop()
        if generation is not None:
            self._assert_generation(generation)
            self._rate_limit_deadlines.pop(generation, None)
            self._rate_limit_generations.pop(generation, None)
        else:
            self._rate_limit_deadlines.clear()
            self._rate_limit_generations.clear()
        self._blocked_until = max(
            self._rate_limit_deadlines.values(), default=self._clock()
        )
        self._wake_waiters()

    def _release(self, lease: ProviderAdmission) -> None:
        self._require_sync_loop()
        current = self._leases.pop(lease._identity, None)
        if current is None:
            return
        self._in_flight = max(0, self._in_flight - 1)
        self._wake_waiters()

    async def acquire(
        self, *, timeout: float | None = None, generation: Hashable | None = None
    ) -> ProviderAdmission:
        self._bind_loop()
        if generation is not None:
            self._assert_generation(generation)
        duration = _validate_duration(
            timeout,
            default=self.admission_timeout,
            maximum=_MAX_ADMISSION_TIMEOUT,
            code="admission_timeout must be finite and bounded",
        )
        assert self._state_lock is not None
        assert self._wake_event is not None
        deadline = self._clock() + duration
        ticket = object()
        async with self._state_lock:
            if len(self._queue) >= self.max_queue:
                raise ProviderQueueFullError()
            self._queue.append(ticket)
        while True:
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
                    lease = ProviderAdmission(self, generation, id(ticket))
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
                wait_timeout = min(remaining, pressure_wait if pressure_wait else 0.05)
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=wait_timeout)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                async with self._state_lock:
                    with contextlib.suppress(ValueError):
                        self._queue.remove(ticket)
                    self._wake_event.set()
                raise

    @asynccontextmanager
    async def slot(
        self, *, timeout: float | None = None, generation: Hashable | None = None
    ) -> AsyncIterator[ProviderAdmission]:
        lease = await self.acquire(timeout=timeout, generation=generation)
        try:
            yield lease
        finally:
            lease.release()

    async def run(
        self,
        generation: Hashable,
        operation: Callable[[], Any | Awaitable[Any]],
        *,
        timeout: float | None = None,
    ) -> Any:
        self._bind_loop()
        self._assert_generation(generation)
        if not callable(operation):
            raise TypeError("operation must be callable")
        assert self._state_lock is not None
        async with self._state_lock:
            shared = self._completed.get(generation)
            owner = False
            if shared is not None:
                self._completed.move_to_end(generation)
            else:
                shared = self._active.get(generation)
                if shared is None:
                    shared = asyncio.get_running_loop().create_future()
                    self._active[generation] = shared
                    owner = True
        if not owner:
            return await asyncio.shield(shared)

        failure_code: str | None = None
        cancelled = False
        result: Any = None
        try:
            duration = _validate_duration(
                timeout,
                default=self.admission_timeout,
                maximum=_MAX_ADMISSION_TIMEOUT,
                code="operation_timeout must be finite and bounded",
            )
            async with asyncio.timeout(duration):
                async with self.slot(timeout=duration, generation=generation):
                    result = operation()
                    if inspect.isawaitable(result):
                        result = await result
        except asyncio.CancelledError:
            failure_code = "operation_cancelled"
            cancelled = True
        except PressureAdmissionTimeoutError:
            failure_code = "admission_timeout"
        except ProviderQueueFullError:
            failure_code = "queue_full"
        except TimeoutError:
            failure_code = "operation_timeout"
        except BaseException:  # noqa: BLE001 - every waiter receives a safe error
            # This includes provider BaseException paths.  The original object
            # is never placed in the shared future or re-raised to another waiter.
            failure_code = "operation_aborted"

        safe_failure: (
            ProviderOperationError
            | PressureAdmissionTimeoutError
            | ProviderQueueFullError
            | None
        ) = None
        if failure_code == "admission_timeout":
            safe_failure = PressureAdmissionTimeoutError()
        elif failure_code == "queue_full":
            safe_failure = ProviderQueueFullError()
        elif failure_code is not None:
            safe_failure = ProviderOperationError(failure_code)

        if safe_failure is None:
            if not shared.done():
                shared.set_result(result)
        else:
            if not shared.done():
                shared.set_exception(safe_failure)
                with contextlib.suppress(BaseException):
                    shared.exception()

        async with self._state_lock:
            self._active.pop(generation, None)
            self._completed[generation] = shared
            self._completed.move_to_end(generation)
            while len(self._completed) > self.max_retained_generations:
                self._completed.popitem(last=False)

        if cancelled:
            raise asyncio.CancelledError
        if safe_failure is not None:
            raise safe_failure
        return result


CodexProviderPressure = ProviderPressure
