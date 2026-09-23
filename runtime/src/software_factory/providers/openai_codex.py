"""Application-owned Codex credentials and bounded provider admission.

This module intentionally does not import Hermes authentication code or the optional
``pydantic-ai`` package.  ``OpenAICodexCredentialSource`` implements the small
``load``/``save`` protocol consumed by Pydantic AI's ``openai-codex`` provider,
while also exposing an explicit, locked ``refresh`` operation for applications
that need cross-process refresh-token rotation.

The file backend stores the same flat three-field shape as
``OpenAICodexCredentials``.  It is not compatible with, and never reads or
writes, the Codex CLI's ``auth.json`` format.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import math
import os
import stat
import tempfile
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Self, TypeAlias, cast

try:  # POSIX is the supported production target for the filesystem lock.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts.
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
    "ProviderOperationError",
    "ProviderPressure",
)


_MAX_CREDENTIAL_FIELD_LENGTH = 128 * 1024
_DEFAULT_MAX_FILE_BYTES = 512 * 1024
_DEFAULT_LOCK_TIMEOUT = 5.0
_MAX_LOCK_TIMEOUT = 300.0
_DEFAULT_ADMISSION_TIMEOUT = 30.0
_MAX_ADMISSION_TIMEOUT = 3_600.0
_DEFAULT_MAX_BACKOFF = 60.0

_SCHEMA_FIELDS = ("access_token", "refresh_token", "account_id")
_SCHEMA_FIELD_SET = frozenset(_SCHEMA_FIELDS)

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class CodexCredentialError(RuntimeError):
    """Base class whose message contains only a safe path and reason code."""

    __slots__ = ("category", "code", "safe_path")

    def __init__(
        self,
        code: str,
        *,
        category: str = "credential",
        path: Path | None = None,
    ) -> None:
        self.category = category
        self.code = code
        self.safe_path = None if path is None else str(path)
        # Do not include an errno, exception text, field value, or credential
        # object.  A stable code is sufficient for callers and telemetry.
        location = f" path={self.safe_path}" if self.safe_path is not None else ""
        super().__init__(f"{category} error code={code}{location}")

    def __repr__(self) -> str:
        location = f", path={self.safe_path!r}" if self.safe_path is not None else ""
        return f"{type(self).__name__}(code={self.code!r}{location})"


class CredentialValidationError(CodexCredentialError):
    """Credential values or their serialized schema are invalid."""

    def __init__(
        self, code: str = "invalid_credentials", *, path: Path | None = None
    ) -> None:
        super().__init__(code, category="schema", path=path)


class CredentialCorruptionError(CodexCredentialError):
    """The stored file is present but cannot be trusted or decoded."""

    def __init__(
        self, code: str = "corrupt_store", *, path: Path | None = None
    ) -> None:
        super().__init__(code, category="corruption", path=path)


class CredentialPermissionError(CodexCredentialError):
    """The credential file, lock, or parent directory is unsafe."""

    def __init__(
        self, code: str = "insecure_permissions", *, path: Path | None = None
    ) -> None:
        super().__init__(code, category="permission", path=path)


class CredentialPersistenceError(CodexCredentialError):
    """A durable file operation failed without exposing its underlying detail."""

    def __init__(
        self, code: str = "persistence_failed", *, path: Path | None = None
    ) -> None:
        super().__init__(code, category="persistence", path=path)


class CredentialNotFoundError(CodexCredentialError):
    """No credential generation exists at the configured path."""

    def __init__(
        self, code: str = "missing_store", *, path: Path | None = None
    ) -> None:
        super().__init__(code, category="not_found", path=path)


class CredentialLockError(CodexCredentialError):
    """The refresh lock cannot be used safely."""

    def __init__(
        self, code: str = "lock_unavailable", *, path: Path | None = None
    ) -> None:
        super().__init__(code, category="lock", path=path)


class CredentialLockTimeoutError(CredentialLockError):
    """The bounded refresh-lock wait expired."""

    def __init__(self, code: str = "lock_timeout", *, path: Path | None = None) -> None:
        super().__init__(code, path=path)


class CredentialRefreshError(CodexCredentialError):
    """A refresh callback failed or returned an invalid credential generation."""

    def __init__(
        self, code: str = "refresh_failed", *, path: Path | None = None
    ) -> None:
        super().__init__(code, category="refresh", path=path)


class PressureAdmissionTimeoutError(CodexCredentialError):
    """Provider admission could not be obtained within its finite deadline."""

    def __init__(self, code: str = "admission_timeout") -> None:
        super().__init__(code, category="provider_pressure")


class ProviderOperationError(CodexCredentialError):
    """A shared provider operation failed without retaining unsafe exception text."""

    def __init__(self, code: str = "operation_failed") -> None:
        super().__init__(code, category="provider_pressure")


def _validate_secret_text(value: object, *, code: str) -> str:
    """Validate a credential field without putting the value in an exception."""

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


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class OpenAICodexCredentials:
    """The three values required by Pydantic AI's Codex provider.

    All three fields are treated as secret-bearing.  The custom representation is
    deliberately constant so accidental logging, exception rendering, and test
    failure output cannot disclose a token or account identifier.
    """

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    account_id: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_secret_text(self.access_token, code="invalid_access_token")
        _validate_secret_text(self.refresh_token, code="invalid_refresh_token")
        _validate_secret_text(self.account_id, code="invalid_account_id")

    def __repr__(self) -> str:
        return "<OpenAICodexCredentials redacted>"

    def __str__(self) -> str:
        return "<OpenAICodexCredentials redacted>"

    def __format__(self, _format_spec: str) -> str:
        return "<OpenAICodexCredentials redacted>"

    def __eq__(self, other: object) -> bool:
        """Compare structurally with PydanticAI's optional credentials class."""

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
        except Exception:  # noqa: BLE001 - foreign comparison must fail closed
            return False

    def __hash__(self) -> int:
        return hash((self.access_token, self.refresh_token, self.account_id))

    def as_mapping(self) -> dict[str, str]:
        """Return the exact persistence shape for the trusted file writer.

        This method is intentionally explicit rather than relying on a generic
        dataclass serializer at arbitrary call sites.
        """

        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "account_id": self.account_id,
        }


CredentialRefreshCallback: TypeAlias = Callable[
    [OpenAICodexCredentials],
    OpenAICodexCredentials | Awaitable[OpenAICodexCredentials],
]


class CodexCredentialBackend(Protocol):
    """Backend contract for application-owned Codex credential storage.

    A future secret manager can implement this protocol without changing the
    PydanticAI-facing source.  ``refresh`` is part of the contract because the
    backend owns the atomicity and cross-process single-flight boundary; a plain
    read/write adapter is not sufficient for single-use refresh tokens.
    """

    async def load(self) -> OpenAICodexCredentials:
        """Load one complete, validated credential generation."""
        ...

    async def save(self, credentials: OpenAICodexCredentials) -> None:
        """Persist one complete credential generation atomically."""
        ...

    async def refresh(
        self,
        refresh_callback: CredentialRefreshCallback,
        *,
        expected: OpenAICodexCredentials | None = None,
        timeout: float | None = None,
    ) -> OpenAICodexCredentials:
        """Reload under the backend's refresh lock, then rotate at most once."""
        ...


# Store-oriented spelling for callers that do not care whether the backend is
# file-backed or provided by a future secret manager.
CodexCredentialStore = CodexCredentialBackend


class _CredentialPathMixin:
    """Shared safe-path and descriptor helpers for the file backend."""

    path: Path
    lock_path: Path
    max_file_bytes: int

    def _check_parent_directory(self) -> None:
        parent = self.path.parent
        current = Path(parent.anchor)
        # ``Path.parts`` is lexical here on purpose: resolving a symlink would
        # turn an unsafe parent into an apparently safe path.
        for component in parent.parts:
            if component in {parent.anchor, ""}:
                continue
            current /= component
            try:
                info = os.lstat(current)
            except OSError:
                raise CredentialPermissionError(
                    "parent_missing", path=self.path
                ) from None
            if stat.S_ISLNK(info.st_mode):
                raise CredentialPermissionError("parent_symlink", path=self.path)
            if not stat.S_ISDIR(info.st_mode):
                raise CredentialPermissionError("parent_not_directory", path=self.path)
            if info.st_uid not in {os.geteuid(), 0}:
                raise CredentialPermissionError("parent_owner", path=self.path)
            mode = stat.S_IMODE(info.st_mode)
            # A root-owned sticky directory such as /tmp is a safe rendezvous
            # ancestor; arbitrary group/world-writable parents are not.
            sticky_shared = bool(mode & stat.S_ISVTX) and info.st_uid == 0
            if mode & 0o022 and not sticky_shared:
                raise CredentialPermissionError("parent_writable", path=self.path)

    def _validate_target(self, *, allow_missing: bool) -> bool:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            if allow_missing:
                return False
            raise CredentialNotFoundError(path=self.path) from None
        except OSError:
            raise CredentialPersistenceError(
                "target_stat_failed", path=self.path
            ) from None
        if stat.S_ISLNK(info.st_mode):
            raise CredentialPermissionError("target_symlink", path=self.path)
        if not stat.S_ISREG(info.st_mode):
            raise CredentialPermissionError("target_not_regular", path=self.path)
        if info.st_nlink != 1:
            raise CredentialPermissionError("target_hardlink", path=self.path)
        if info.st_uid not in {os.geteuid(), 0}:
            raise CredentialPermissionError("target_owner", path=self.path)
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise CredentialPermissionError("target_mode", path=self.path)
        return True

    def _open_parent_fd(self) -> int:
        self._check_parent_directory()
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
        try:
            descriptor = os.open(self.path.parent, flags)
        except OSError:
            raise CredentialPersistenceError(
                "parent_open_failed", path=self.path
            ) from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode):
                raise CredentialPermissionError("parent_not_directory", path=self.path)
            if info.st_uid not in {os.geteuid(), 0}:
                raise CredentialPermissionError("parent_owner", path=self.path)
            mode = stat.S_IMODE(info.st_mode)
            sticky_shared = bool(mode & stat.S_ISVTX) and info.st_uid == 0
            if mode & 0o022 and not sticky_shared:
                raise CredentialPermissionError("parent_writable", path=self.path)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _read_unlocked(self) -> OpenAICodexCredentials:
        self._validate_target(allow_missing=False)
        try:
            expected_info = os.lstat(self.path)
        except OSError:
            raise CredentialPersistenceError(
                "target_stat_failed", path=self.path
            ) from None
        flags = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            raise CredentialNotFoundError(path=self.path) from None
        except OSError:
            raise CredentialPersistenceError(
                "target_open_failed", path=self.path
            ) from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CredentialPermissionError("target_changed_type", path=self.path)
            if (info.st_dev, info.st_ino) != (
                expected_info.st_dev,
                expected_info.st_ino,
            ):
                raise CredentialPermissionError(
                    "target_changed_identity", path=self.path
                )
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise CredentialPermissionError("target_changed_mode", path=self.path)
            if info.st_uid not in {os.geteuid(), 0}:
                raise CredentialPermissionError("target_changed_owner", path=self.path)
            if info.st_size > self.max_file_bytes:
                raise CredentialCorruptionError("store_too_large", path=self.path)
            data = bytearray()
            while len(data) <= self.max_file_bytes:
                chunk = os.read(
                    descriptor, min(65_536, self.max_file_bytes + 1 - len(data))
                )
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > self.max_file_bytes:
                raise CredentialCorruptionError("store_too_large", path=self.path)
        except (
            CredentialCorruptionError,
            CredentialPermissionError,
            CredentialPersistenceError,
            CredentialNotFoundError,
            CredentialValidationError,
        ):
            raise
        except OSError:
            raise CredentialPersistenceError(
                "target_read_failed", path=self.path
            ) from None
        finally:
            os.close(descriptor)
        try:
            decoded = bytes(data).decode("utf-8")
            payload = json.loads(decoded, object_pairs_hook=_strict_object_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise CredentialCorruptionError("invalid_json", path=self.path) from None
        return _credentials_from_payload(payload, path=self.path)


def _credentials_from_payload(
    payload: object, *, path: Path | None = None
) -> OpenAICodexCredentials:
    if type(payload) is not dict:
        raise CredentialCorruptionError("schema_not_object", path=path)
    if frozenset(payload) != _SCHEMA_FIELD_SET or len(payload) != len(_SCHEMA_FIELDS):
        raise CredentialCorruptionError("schema_fields", path=path)
    try:
        return OpenAICodexCredentials(
            access_token=payload["access_token"],  # type: ignore[arg-type]
            refresh_token=payload["refresh_token"],  # type: ignore[arg-type]
            account_id=payload["account_id"],  # type: ignore[arg-type]
        )
    except CredentialValidationError:
        raise CredentialCorruptionError("schema_values", path=path) from None


def _coerce_credentials(
    credentials: object, *, path: Path | None = None
) -> OpenAICodexCredentials:
    """Accept PydanticAI's structurally identical credential dataclass.

    The runtime package keeps PydanticAI optional.  Its provider passes its own
    ``OpenAICodexCredentials`` instance back to ``save`` after rotation, so the
    backend validates the three attributes into its redacted local value rather
    than requiring an optional dependency or trusting an arbitrary object.
    """

    if isinstance(credentials, OpenAICodexCredentials):
        return credentials
    foreign = cast(Any, credentials)
    try:
        return OpenAICodexCredentials(
            access_token=foreign.access_token,
            refresh_token=foreign.refresh_token,
            account_id=foreign.account_id,
        )
    except CredentialValidationError:
        raise CredentialValidationError("credential_type", path=path) from None
    except Exception:  # noqa: BLE001 - do not expose foreign credential errors
        raise CredentialValidationError("credential_type", path=path) from None


def _strict_object_pairs(pairs: list[tuple[object, object]]) -> dict[object, object]:
    """Reject duplicate JSON keys instead of silently selecting one value."""

    result: dict[object, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _serialized_credentials(credentials: object, *, path: Path) -> bytes:
    credentials = _coerce_credentials(credentials, path=path)
    try:
        return (
            json.dumps(
                credentials.as_mapping(),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError):
        raise CredentialValidationError("credential_serialization", path=path) from None


def _validate_duration(
    value: float | None, *, default: float, maximum: float, code: str
) -> float:
    resolved = default if value is None else value
    if type(resolved) not in {int, float} or not math.isfinite(float(resolved)):
        raise ValueError(code)
    if float(resolved) < 0 or float(resolved) > maximum:
        raise ValueError(code)
    return float(resolved)


class FileCodexCredentialBackend(_CredentialPathMixin):
    """Secure JSON backend with stable POSIX lock and crash-safe replacement."""

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
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
        self.lock_path = Path(f"{self.path}.lock")
        self._last_loaded: OpenAICodexCredentials | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(path={str(self.path)!r})"

    def _lock_fd(self) -> int:
        if fcntl is None:
            raise CredentialLockError("posix_lock_unavailable", path=self.lock_path)
        self._check_parent_directory()
        try:
            existed = os.path.lexists(self.lock_path)
            descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC,
                0o600,
            )
        except OSError:
            raise CredentialLockError("lock_open_failed", path=self.lock_path) from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CredentialPermissionError("lock_not_regular", path=self.lock_path)
            if info.st_uid not in {os.geteuid(), 0}:
                raise CredentialPermissionError("lock_owner", path=self.lock_path)
            if not existed:
                os.fchmod(descriptor, 0o600)
                info = os.fstat(descriptor)
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise CredentialPermissionError("lock_mode", path=self.lock_path)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _unlock_and_close(descriptor: int) -> None:
        try:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)

    async def _acquire_async(self, timeout: float | None) -> int:
        duration = _validate_duration(
            timeout,
            default=self.lock_timeout,
            maximum=_MAX_LOCK_TIMEOUT,
            code="lock_timeout must be finite and bounded",
        )
        descriptor = self._lock_fd()
        deadline = time.monotonic() + duration
        try:
            assert fcntl is not None
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return descriptor
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CredentialLockTimeoutError(path=self.lock_path) from None
                    await asyncio.sleep(min(0.05, remaining))
        except BaseException:
            self._unlock_and_close(descriptor)
            raise

    def _acquire_sync(self, timeout: float | None) -> int:
        duration = _validate_duration(
            timeout,
            default=self.lock_timeout,
            maximum=_MAX_LOCK_TIMEOUT,
            code="lock_timeout must be finite and bounded",
        )
        descriptor = self._lock_fd()
        deadline = time.monotonic() + duration
        try:
            assert fcntl is not None
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return descriptor
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CredentialLockTimeoutError(path=self.lock_path) from None
                    time.sleep(min(0.05, remaining))
        except BaseException:
            self._unlock_and_close(descriptor)
            raise

    @asynccontextmanager
    async def refresh_lock(
        self, *, timeout: float | None = None
    ) -> AsyncIterator[None]:
        """Hold the stable same-domain exclusive lock for one refresh transaction."""

        descriptor = await self._acquire_async(timeout)
        try:
            yield None
        finally:
            self._unlock_and_close(descriptor)

    @contextlib.contextmanager
    def refresh_lock_sync(self, *, timeout: float | None = None):
        """Synchronous counterpart used by deterministic crash/process probes."""

        descriptor = self._acquire_sync(timeout)
        try:
            yield None
        finally:
            self._unlock_and_close(descriptor)

    def _write_atomic_unlocked(
        self,
        credentials: OpenAICodexCredentials,
        *,
        allow_insecure_existing: bool,
    ) -> None:
        data = _serialized_credentials(credentials, path=self.path)
        self._check_parent_directory()
        try:
            existing = os.lstat(self.path)
        except FileNotFoundError:
            existing = None
        except OSError:
            raise CredentialPersistenceError(
                "target_stat_failed", path=self.path
            ) from None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise CredentialPermissionError("target_symlink", path=self.path)
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                raise CredentialPermissionError("target_not_regular", path=self.path)
            if existing.st_uid not in {os.geteuid(), 0}:
                raise CredentialPermissionError("target_owner", path=self.path)
            if not allow_insecure_existing and stat.S_IMODE(existing.st_mode) != 0o600:
                raise CredentialPermissionError("target_mode", path=self.path)

        descriptor: int | None = None
        temporary_path: str | None = None
        parent_descriptor: int | None = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=str(self.path.parent),
            )
            written = 0
            while written < len(data):
                count = os.write(descriptor, data[written:])
                if count <= 0:
                    raise OSError("short write")
                written += count
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            temporary_info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(temporary_info.st_mode)
                or stat.S_IMODE(temporary_info.st_mode) != 0o600
            ):
                raise CredentialPermissionError("temporary_mode", path=self.path)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary_path, self.path)
            temporary_path = None
            parent_descriptor = self._open_parent_fd()
            os.fsync(parent_descriptor)
            # Read-back checks the published inode's type and mode without
            # parsing the secret payload a second time.
            self._validate_target(allow_missing=False)
        except (
            CredentialCorruptionError,
            CredentialPermissionError,
            CredentialPersistenceError,
        ):
            raise
        except OSError:
            raise CredentialPersistenceError(
                "atomic_replace_failed", path=self.path
            ) from None
        finally:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            if parent_descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(parent_descriptor)
            if temporary_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary_path)

    def _save_unlocked(self, credentials: OpenAICodexCredentials) -> None:
        # A normal save must prove that it is not silently replacing a
        # corrupted generation.  Missing is the explicit initial-write case.
        if self._validate_target(allow_missing=True):
            self._read_unlocked()
        self._write_atomic_unlocked(credentials, allow_insecure_existing=False)

    def _recover_unlocked(self, credentials: OpenAICodexCredentials) -> None:
        # Recovery is explicit and can replace malformed JSON or an insecure
        # mode, but it still refuses links, non-files, hardlinks, and foreign
        # owners.  It never follows or deletes a link.
        self._write_atomic_unlocked(credentials, allow_insecure_existing=True)

    async def load(self) -> OpenAICodexCredentials:
        async with self.refresh_lock():
            current = self._read_unlocked()
            self._last_loaded = current
            return current

    async def save(self, credentials: OpenAICodexCredentials) -> None:
        async with self.refresh_lock():
            if self._last_loaded is not None:
                current = self._read_unlocked()
                if not _same_credentials(current, self._last_loaded):
                    raise CredentialPersistenceError("stale_generation", path=self.path)
            self._save_unlocked(credentials)
            self._last_loaded = _coerce_credentials(credentials, path=self.path)

    async def recover(self, credentials: OpenAICodexCredentials) -> None:
        """Explicitly replace a corrupt or incomplete regular-file generation."""

        async with self.refresh_lock():
            self._recover_unlocked(credentials)
            self._last_loaded = _coerce_credentials(credentials, path=self.path)

    async def refresh(
        self,
        refresh_callback: CredentialRefreshCallback,
        *,
        expected: OpenAICodexCredentials | None = None,
        timeout: float | None = None,
    ) -> OpenAICodexCredentials:
        """Reload under the lock, reuse a fresh generation, or rotate exactly once.

        ``expected`` is the generation the caller used for a failed/stale
        request.  A waiter that observes a different generation returns it
        without invoking the callback, which prevents duplicate use of a
        single-use refresh token across processes.
        """

        if not callable(refresh_callback):
            raise CredentialRefreshError("refresh_callback_invalid", path=self.path)
        async with self.refresh_lock(timeout=timeout):
            current = self._read_unlocked()
            if expected is not None and not _same_credentials(current, expected):
                return current
            try:
                candidate = refresh_callback(current)
                if inspect.isawaitable(candidate):
                    candidate = await candidate
            except CredentialRefreshError:
                raise
            except Exception:  # noqa: BLE001 - redact arbitrary provider callback text
                # Never preserve arbitrary callback text: provider exceptions
                # can contain request bodies or bearer values.
                raise CredentialRefreshError(
                    "refresh_callback_failed", path=self.path
                ) from None
            try:
                candidate = _coerce_credentials(candidate, path=self.path)
            except CredentialValidationError:
                raise CredentialRefreshError(
                    "refresh_result_invalid", path=self.path
                ) from None
            try:
                self._save_unlocked(candidate)
            except CodexCredentialError:
                raise
            except Exception:  # noqa: BLE001 - redact arbitrary persistence text
                raise CredentialPersistenceError(
                    "rotation_failed", path=self.path
                ) from None
            self._last_loaded = candidate
            return candidate

    def load_sync(self) -> OpenAICodexCredentials:
        with self.refresh_lock_sync():
            current = self._read_unlocked()
            self._last_loaded = current
            return current

    def save_sync(self, credentials: OpenAICodexCredentials) -> None:
        with self.refresh_lock_sync():
            if self._last_loaded is not None:
                current = self._read_unlocked()
                if not _same_credentials(current, self._last_loaded):
                    raise CredentialPersistenceError("stale_generation", path=self.path)
            self._save_unlocked(credentials)
            self._last_loaded = _coerce_credentials(credentials, path=self.path)

    def recover_sync(self, credentials: OpenAICodexCredentials) -> None:
        with self.refresh_lock_sync():
            self._recover_unlocked(credentials)
            self._last_loaded = _coerce_credentials(credentials, path=self.path)

    def refresh_sync(
        self,
        refresh_callback: CredentialRefreshCallback,
        *,
        expected: OpenAICodexCredentials | None = None,
        timeout: float | None = None,
    ) -> OpenAICodexCredentials:
        """Synchronous refresh helper for process workers and test harnesses."""

        if not callable(refresh_callback):
            raise CredentialRefreshError("refresh_callback_invalid", path=self.path)
        with self.refresh_lock_sync(timeout=timeout):
            current = self._read_unlocked()
            if expected is not None and not _same_credentials(current, expected):
                return current
            try:
                candidate = refresh_callback(current)
                if inspect.isawaitable(candidate):
                    raise CredentialRefreshError(
                        "refresh_callback_async", path=self.path
                    )
            except CredentialRefreshError:
                raise
            except Exception:  # noqa: BLE001 - redact arbitrary provider callback text
                raise CredentialRefreshError(
                    "refresh_callback_failed", path=self.path
                ) from None
            try:
                candidate = _coerce_credentials(candidate, path=self.path)
            except CredentialValidationError:
                raise CredentialRefreshError(
                    "refresh_result_invalid", path=self.path
                ) from None
            self._save_unlocked(candidate)
            self._last_loaded = candidate
            return candidate


def _same_credentials(left: OpenAICodexCredentials, right: object) -> bool:
    try:
        right = _coerce_credentials(right)
        return (
            left.access_token == right.access_token
            and left.refresh_token == right.refresh_token
            and left.account_id == right.account_id
        )
    except Exception:  # noqa: BLE001 - malformed comparison must fail closed
        return False


class OpenAICodexCredentialSource:
    """PydanticAI-compatible source backed by a file or a future secret store.

    With a path, this constructs ``FileCodexCredentialBackend``.  A custom
    backend may instead be passed for a secret manager; it must implement the
    ``CodexCredentialBackend`` protocol, so the application/provider wiring does
    not change when storage is replaced.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str | None = None,
        *,
        backend: CodexCredentialBackend | None = None,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        lock_timeout: float = _DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        if (path is None) == (backend is None):
            raise ValueError("provide exactly one credential path or backend")
        if backend is None:
            backend = FileCodexCredentialBackend(
                cast(os.PathLike[str] | str, path),
                max_file_bytes=max_file_bytes,
                lock_timeout=lock_timeout,
            )
        self._backend = backend
        self.path = getattr(backend, "path", None)
        self.lock_path = getattr(backend, "lock_path", None)

    def __repr__(self) -> str:
        if self.path is not None:
            return f"{type(self).__name__}(path={str(self.path)!r})"
        return f"{type(self).__name__}(backend={type(self._backend).__name__!r})"

    async def load(self) -> OpenAICodexCredentials:
        """Load the current generation for PydanticAI's provider."""

        try:
            return _coerce_credentials(await self._backend.load(), path=self.path)
        except CodexCredentialError:
            raise
        except Exception:  # noqa: BLE001 - sanitize future backend failures
            raise CredentialPersistenceError(
                "backend_load_failed", path=self.path
            ) from None

    async def save(self, credentials: OpenAICodexCredentials) -> None:
        """Persist PydanticAI's rotated generation atomically."""

        try:
            await self._backend.save(credentials)
        except CodexCredentialError:
            raise
        except Exception:  # noqa: BLE001 - sanitize future backend failures
            raise CredentialPersistenceError(
                "backend_save_failed", path=self.path
            ) from None

    async def refresh(
        self,
        refresh_callback: CredentialRefreshCallback,
        *,
        expected: OpenAICodexCredentials | None = None,
        timeout: float | None = None,
    ) -> OpenAICodexCredentials:
        """Run a locked reload/refresh/save transaction on the selected backend."""

        try:
            result = await self._backend.refresh(
                refresh_callback, expected=expected, timeout=timeout
            )
            return _coerce_credentials(result, path=self.path)
        except CodexCredentialError:
            raise
        except Exception:  # noqa: BLE001 - sanitize future backend failures
            raise CredentialRefreshError(
                "backend_refresh_failed", path=self.path
            ) from None

    async def recover(self, credentials: OpenAICodexCredentials) -> None:
        """Use the backend's explicit recovery path; never auto-discard corruption."""

        recover = getattr(self._backend, "recover", None)
        if recover is None or not callable(recover):
            raise CredentialPersistenceError(
                "backend_recovery_unsupported", path=self.path
            )
        recover = cast(Callable[[OpenAICodexCredentials], Awaitable[None]], recover)
        try:
            await recover(credentials)
        except CodexCredentialError:
            raise
        except Exception:  # noqa: BLE001 - sanitize future backend failures
            raise CredentialPersistenceError(
                "backend_recovery_failed", path=self.path
            ) from None

    @asynccontextmanager
    async def refresh_lock(
        self, *, timeout: float | None = None
    ) -> AsyncIterator[None]:
        """Expose the backend's bounded lock for application-level request flows."""

        lock = getattr(self._backend, "refresh_lock", None)
        if lock is None or not callable(lock):
            raise CredentialLockError("backend_lock_unsupported", path=self.path)
        lock = cast(Callable[..., Any], lock)
        try:
            async with lock(timeout=timeout):
                yield None
        except CodexCredentialError:
            raise
        except Exception:  # noqa: BLE001 - sanitize future backend failures
            raise CredentialLockError("backend_lock_failed", path=self.path) from None


# Compatibility spellings kept in the focused provider package, not at the
# root runtime API.  They all implement the same PydanticAI load/save shape.
FileCredentialSource = OpenAICodexCredentialSource


@dataclass(frozen=True, slots=True)
class ProviderAdmission:
    """A bounded provider-capacity lease."""

    _pressure: ProviderPressure = field(repr=False, compare=False)
    generation: Hashable | None = field(default=None, repr=False, compare=False)
    _released: bool = field(default=False, repr=False, compare=False)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.release()

    def release(self) -> None:
        # Dataclass frozen state is not mutated; the pressure object owns the
        # idempotent release set.
        self._pressure._release(self)


class ProviderPressure:
    """Finite provider concurrency with queueing, backoff, and single-flight.

    Admission never spawns a retry worker and never sleeps without a finite
    deadline.  A rate-limit observation moves a monotone ``blocked_until``
    pressure fence; queued callers are awakened when capacity or pressure
    changes.  ``run`` deduplicates one logical generation so concurrent retry
    callers await the same bounded operation.
    """

    def __init__(
        self,
        capacity: int = 1,
        *,
        admission_timeout: float = _DEFAULT_ADMISSION_TIMEOUT,
        max_backoff: float = _DEFAULT_MAX_BACKOFF,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity
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
        self._condition = asyncio.Condition()
        self._queue: deque[object] = deque()
        self._in_flight = 0
        self._blocked_until = 0.0
        self._rate_limit_generation: Hashable | None = None
        self._leases: set[int] = set()
        self._single_flight: dict[Hashable, asyncio.Future[Any]] = {}

    @property
    def queued(self) -> int:
        return len(self._queue)

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def blocked_until(self) -> float:
        return self._blocked_until

    def pressure_state(self) -> dict[str, int | float | None]:
        """Return safe numeric state suitable for bounded telemetry."""

        return {
            "capacity": self.capacity,
            "in_flight": self._in_flight,
            "queued": len(self._queue),
            "blocked_until": self._blocked_until,
        }

    def observe_rate_limit(
        self,
        retry_after: float | None,
        *,
        generation: Hashable | None = None,
    ) -> None:
        """Update pressure from a bounded provider retry-after value.

        The value is clamped to ``max_backoff`` and never decreases an existing
        pressure fence.  ``generation`` is an opaque logical retry generation;
        repeating the same observation cannot extend pressure indefinitely.
        """

        if retry_after is None:
            return
        if type(retry_after) not in {int, float} or not math.isfinite(
            float(retry_after)
        ):
            return
        delay = max(0.0, min(float(retry_after), self.max_backoff))
        now = self._clock()
        if generation is not None and generation == self._rate_limit_generation:
            return
        if generation is not None:
            self._rate_limit_generation = generation
        self._blocked_until = max(self._blocked_until, now + delay)
        self._wake_waiters()

    # Common provider-facing spelling.
    record_rate_limit = observe_rate_limit

    def clear_pressure(self, *, generation: Hashable | None = None) -> None:
        """Clear only a matching logical pressure generation."""

        if generation is not None and generation != self._rate_limit_generation:
            return
        self._blocked_until = self._clock()
        self._rate_limit_generation = None
        self._wake_waiters()

    def _wake_waiters(self) -> None:
        # Admission polls with a finite condition-wait interval.  Keeping this
        # method task-free avoids an unowned background notifier when a provider
        # callback updates pressure from synchronous cleanup code.
        return

    def _release(self, lease: ProviderAdmission) -> None:
        identity = id(lease)
        if identity not in self._leases:
            return
        self._leases.remove(identity)
        self._in_flight = max(0, self._in_flight - 1)
        self._wake_waiters()

    async def acquire(
        self,
        *,
        timeout: float | None = None,
        generation: Hashable | None = None,
    ) -> ProviderAdmission:
        """Queue for one capacity lease until the finite admission deadline."""

        duration = _validate_duration(
            timeout,
            default=self.admission_timeout,
            maximum=_MAX_ADMISSION_TIMEOUT,
            code="admission_timeout must be finite and bounded",
        )
        ticket = object()
        deadline = self._clock() + duration
        async with self._condition:
            self._queue.append(ticket)
            try:
                while True:
                    now = self._clock()
                    if (
                        self._queue[0] is ticket
                        and self._in_flight < self.capacity
                        and now >= self._blocked_until
                    ):
                        self._queue.popleft()
                        self._in_flight += 1
                        lease = ProviderAdmission(self, generation)
                        self._leases.add(id(lease))
                        return lease
                    remaining = deadline - now
                    if remaining <= 0:
                        self._queue.remove(ticket)
                        self._condition.notify_all()
                        raise PressureAdmissionTimeoutError()
                    pressure_wait = max(0.0, self._blocked_until - now)
                    try:
                        await asyncio.wait_for(
                            self._condition.wait(),
                            timeout=min(
                                remaining, pressure_wait if pressure_wait else 0.05
                            ),
                        )
                    except TimeoutError:
                        # The timed condition wait is only a bounded poll for
                        # a monotonic backoff deadline.  Keep the ticket in
                        # place so FIFO admission is preserved.
                        if self._clock() >= deadline:
                            with contextlib.suppress(ValueError):
                                self._queue.remove(ticket)
                            self._condition.notify_all()
                            raise PressureAdmissionTimeoutError() from None
                        continue
            except BaseException:
                with contextlib.suppress(ValueError):
                    self._queue.remove(ticket)
                self._condition.notify_all()
                raise

    @asynccontextmanager
    async def slot(
        self,
        *,
        timeout: float | None = None,
        generation: Hashable | None = None,
    ) -> AsyncIterator[ProviderAdmission]:
        """Acquire and always release one provider-capacity lease."""

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
        """Run one bounded operation per logical generation.

        A second caller for the same generation awaits the first operation
        rather than acquiring another slot or spawning a duplicate retry.  Its
        cancellation cannot cancel the shared result.
        """

        if not isinstance(generation, Hashable) or not callable(operation):
            raise TypeError("generation and operation must be valid")
        loop = asyncio.get_running_loop()
        async with self._condition:
            existing = self._single_flight.get(generation)
            if existing is None:
                shared: asyncio.Future[Any] = loop.create_future()
                self._single_flight[generation] = shared
                owner = True
            else:
                shared = existing
                owner = False
        if not owner:
            return await asyncio.shield(shared)

        try:
            async with self.slot(timeout=timeout, generation=generation):
                result = operation()
                if inspect.isawaitable(result):
                    result = await result
            if not shared.done():
                shared.set_result(result)
            return result
        except asyncio.CancelledError:
            if not shared.done():
                shared.cancel()
            raise
        except PressureAdmissionTimeoutError as error:
            if not shared.done():
                shared.set_exception(error)
                shared.exception()
            raise
        except Exception:  # noqa: BLE001 - redact arbitrary provider operation text
            if not shared.done():
                # Do not store an arbitrary provider exception as a shared
                # exception object that a caller might render with secrets.
                shared.set_exception(ProviderOperationError())
                shared.exception()
            raise ProviderOperationError() from None
        finally:
            async with self._condition:
                self._single_flight.pop(generation, None)


# Architecture-facing spelling: both names describe the same bounded pressure
# primitive and remain local to the provider package.
CodexProviderPressure = ProviderPressure
