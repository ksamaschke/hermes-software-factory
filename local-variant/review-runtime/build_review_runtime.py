#!/usr/bin/env python3
"""Build and verify the review-runtime layer over a native-boundary stage.

The native-boundary artifact is an input, not a file-edit target.  This layer
copies that exact stage, applies one allowlisted patch, and records enough
hashes to make the resulting runtime reproducible and auditable.
"""

from __future__ import annotations

import argparse
import ast
import errno
import fnmatch
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

ARTIFACT_ROOT = Path(__file__).resolve().parent
STATIC_MANIFEST_PATH = ARTIFACT_ROOT / "manifest.json"
PATCH_RELATIVE = "patches/002-review-runtime.patch"
STATIC_SCHEMA = "factory.review-runtime.v1"
NATIVE_SCHEMA = "factory.native-boundary.v1"
REVIEW_CAP_SECONDS = 1200
EVIDENCE_RECOVERY_CAP_SECONDS = 600
REVIEW_EVIDENCE_BUDGET_SECONDS = 600
EVIDENCE_RECOVERY_EVIDENCE_BUDGET_SECONDS = 300
REVIEW_COMMAND_TIMEOUT_SECONDS = 120
EVIDENCE_RECOVERY_COMMAND_TIMEOUT_SECONDS = 60
TARGET_PATHS = (
    "gateway/kanban_watchers.py",
    "hermes_cli/kanban.py",
    "hermes_cli/kanban_db.py",
    "tools/code_execution_tool.py",
    "tools/code_kernel_remote.py",
    "tools/environments/base.py",
    "tools/evidence_window.py",
    "tools/file_operations.py",
    "tools/image_source.py",
    "tools/kanban_tools.py",
    "tools/process_registry.py",
    "tools/terminal_tool.py",
    "tools/tool_result_storage.py",
    "tui_gateway/server.py",
)
_GIT_EXECUTABLE = Path("/usr/bin/git")
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
}
NATIVE_ARTIFACT_IDENTITY_SHA256 = (
    "feaffd82904b7cce43285ec0a038a3e7c8f85f467cb8c99ae07075a6f59918ab"
)
_NATIVE_IDENTITY_FIELDS = (
    "artifact_version",
    "excluded_copy_entries",
    "patched_paths",
    "patches",
    "schema",
    "source_files",
    "source_tree",
    "staged_tree",
    "verification",
)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"manifest must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _native_artifact_identity_sha256(manifest: dict[str, Any]) -> str:
    """Return a path-independent identity for one complete native artifact.

    Native manifests contain absolute build and output paths. Those locations
    are provenance, not artifact identity: rebuilding the same reviewed tree
    elsewhere must remain valid. Every content-bearing field stays in the
    digest, while import-probe paths are normalized relative to output_runtime.
    """
    missing = [key for key in _NATIVE_IDENTITY_FIELDS if key not in manifest]
    if missing:
        raise SystemExit(
            "native manifest is missing identity fields: " + ", ".join(missing)
        )
    output_value = manifest.get("output_runtime")
    probes = manifest.get("import_probe")
    if not isinstance(output_value, str) or not output_value.strip():
        raise SystemExit("native manifest has no output_runtime for identity")
    if not isinstance(probes, dict):
        raise SystemExit("native manifest has no import_probe map for identity")
    output = Path(output_value).expanduser().resolve(strict=False)
    normalized_probes: dict[str, str] = {}
    for raw_name, raw_path in sorted(probes.items()):
        if not isinstance(raw_name, str) or not isinstance(raw_path, str):
            raise SystemExit("native manifest import_probe identity is invalid")
        probe = Path(raw_path).expanduser().resolve(strict=False)
        try:
            normalized_probes[raw_name] = probe.relative_to(output).as_posix()
        except ValueError as exc:
            raise SystemExit(
                f"native manifest import_probe escapes output_runtime: {raw_name}"
            ) from exc
    payload = {key: manifest[key] for key in _NATIVE_IDENTITY_FIELDS}
    payload["import_probe"] = normalized_probes
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _trusted_git_executable() -> Path:
    """Return the fixed system Git only when its metadata is trustworthy."""
    try:
        info = _GIT_EXECUTABLE.lstat()
    except OSError as exc:
        raise SystemExit(f"trusted git executable is unavailable: {exc}") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not bool(info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    ):
        raise SystemExit(
            "trusted git executable must be root-owned, regular, executable, "
            "and not group/world writable"
        )
    return _GIT_EXECUTABLE


def _excluded(relative: Path, patterns: list[str]) -> bool:
    return any(
        part in patterns or any(fnmatch.fnmatch(part, pattern) for pattern in patterns)
        for part in relative.parts
    )


def _tree_hashes(root: Path, patterns: list[str]) -> dict[str, str]:
    """Hash every regular file in *root* and reject unsafe filesystem entries."""
    if not root.is_dir() or root.is_symlink():
        raise SystemExit(f"runtime tree is not a real directory: {root}")
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _excluded(relative, patterns):
            continue
        relative_key = relative.as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit(
                f"symlink is not allowed in runtime artifact: {relative_key}"
            )
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(f"unsupported native runtime entry: {relative_key}")
        if info.st_nlink != 1:
            raise SystemExit(
                f"hard-linked runtime entry is not allowed: {relative_key} "
                f"(nlink={info.st_nlink})"
            )
        if relative.name in {
            "sitecustomize.py",
            "usercustomize.py",
        } or relative.suffix in {
            ".pth",
            ".egg-link",
        }:
            raise SystemExit(
                f"Python import hook is not allowed in runtime artifact: {relative_key}"
            )
        hashes[relative_key] = _sha256(path)
    return hashes


def _tree_digest(root: Path, patterns: list[str]) -> str:
    digest = hashlib.sha256()
    for relative, value in sorted(_tree_hashes(root, patterns).items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(value))
    return digest.hexdigest()


def _validate_relative(path: str) -> Path:
    if not isinstance(path, str) or not path or "\\" in path:
        raise SystemExit(f"unsafe relative artifact path: {path!r}")
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or str(relative) == ".":
        raise SystemExit(f"unsafe relative artifact path: {path!r}")
    return relative


def _static_manifest() -> dict[str, Any]:
    manifest = _load_json(STATIC_MANIFEST_PATH)
    if manifest.get("schema") != STATIC_SCHEMA:
        raise SystemExit(
            f"unexpected review manifest schema: {manifest.get('schema')!r}"
        )
    if manifest.get("native_boundary_schema") != NATIVE_SCHEMA:
        raise SystemExit("review manifest native boundary schema changed unexpectedly")
    if "native_manifest_sha256" in manifest or (
        manifest.get("native_artifact_identity_sha256")
        != NATIVE_ARTIFACT_IDENTITY_SHA256
    ):
        raise SystemExit(
            "review manifest is not pinned to the reviewed native artifact identity"
        )
    if manifest.get("native_artifact_version") != "1.0.0":
        raise SystemExit("review manifest native artifact version changed unexpectedly")
    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        raise SystemExit("review manifest has no policy object")
    expected_policy = {
        "review_dispatch_hard_cap_seconds": REVIEW_CAP_SECONDS,
        "review": {
            "hard_worker_cap_seconds": REVIEW_CAP_SECONDS,
            "evidence_budget_seconds": REVIEW_EVIDENCE_BUDGET_SECONDS,
            "per_command_timeout_seconds": REVIEW_COMMAND_TIMEOUT_SECONDS,
        },
        "evidence_recovery": {
            "hard_worker_cap_seconds": EVIDENCE_RECOVERY_CAP_SECONDS,
            "evidence_budget_seconds": EVIDENCE_RECOVERY_EVIDENCE_BUDGET_SECONDS,
            "per_command_timeout_seconds": EVIDENCE_RECOVERY_COMMAND_TIMEOUT_SECONDS,
        },
        "canonical_recovery": {
            "terminal_failure_run_fence": True,
            "one_leaf_per_lane": True,
            "dependency_resolution": "canonical_leaf",
        },
        "retryable_statuses": ["ready", "review"],
        "terminal_statuses": ["blocked", "triage", "done", "archived"],
    }
    if policy != expected_policy:
        raise SystemExit("review manifest policy does not match the declared contract")
    patches = manifest.get("patches")
    if not isinstance(patches, dict) or patches.get(PATCH_RELATIVE) != _sha256(
        ARTIFACT_ROOT / PATCH_RELATIVE
    ):
        raise SystemExit("review patch hash does not match manifest.json")
    if tuple(manifest.get("patched_paths", ())) != TARGET_PATHS:
        raise SystemExit("review patch target allowlist changed unexpectedly")
    return manifest


def _patch_targets(patch_path: Path) -> tuple[str, ...]:
    """Read one canonical target header per modified or added file."""
    targets: list[str] = []
    lines = patch_path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("--- a/"):
            target = line[6:].split("\t", 1)[0]
            targets.append(target)
        elif line == "--- /dev/null" and index + 1 < len(lines):
            added = lines[index + 1]
            if added.startswith("+++ b/"):
                targets.append(added[6:].split("\t", 1)[0])
    return tuple(targets)


def _validate_native_input(
    native_runtime: Path,
    native_manifest_path: Path,
    *,
    expected_artifact_identity_sha256: str | None = None,
    expected_exclusions: list[str] | None = None,
    expected_artifact_version: str | None = None,
) -> dict[str, Any]:
    """Authenticate the complete native boundary before any copy occurs."""
    manifest_info = native_manifest_path.lstat()
    if not stat.S_ISREG(manifest_info.st_mode) or manifest_info.st_nlink != 1:
        raise SystemExit("native manifest must be a regular, non-hard-linked file")
    native_manifest = _load_json(native_manifest_path)
    if native_manifest.get("schema") != NATIVE_SCHEMA:
        raise SystemExit(
            f"native input is not the required boundary artifact: {native_manifest.get('schema')!r}"
        )
    if (
        expected_artifact_version
        and native_manifest.get("artifact_version") != expected_artifact_version
    ):
        raise SystemExit("native artifact version does not match the reviewed boundary")
    exclusions = native_manifest.get("excluded_copy_entries")
    if not isinstance(exclusions, list) or not all(
        isinstance(item, str) for item in exclusions
    ):
        raise SystemExit("native manifest has no valid excluded_copy_entries list")
    if expected_exclusions is not None and exclusions != expected_exclusions:
        raise SystemExit(
            "native manifest exclusions do not match the reviewed boundary"
        )
    source_runtime_value = native_manifest.get("source_runtime")
    if not isinstance(source_runtime_value, str) or not source_runtime_value.strip():
        raise SystemExit("native manifest has no source_runtime")
    source_runtime = Path(source_runtime_value).expanduser()
    if not source_runtime.is_absolute() or source_runtime.is_symlink():
        raise SystemExit("native source_runtime must be an absolute real directory")
    if not native_runtime.is_absolute() or native_runtime.is_symlink():
        raise SystemExit("native runtime must be an absolute real directory")
    source_runtime = source_runtime.resolve(strict=False)
    native_runtime = native_runtime.resolve(strict=False)
    if not source_runtime.is_dir() or not native_runtime.is_dir():
        raise SystemExit("native source_runtime and staged runtime must be directories")
    if source_runtime == native_runtime:
        raise SystemExit("native source_runtime and staged runtime must be distinct")

    source_tree = native_manifest.get("source_tree")
    staged_tree = native_manifest.get("staged_tree")
    if not isinstance(source_tree, dict) or not isinstance(staged_tree, dict):
        raise SystemExit(
            "native manifest must pin complete source_tree and staged_tree maps"
        )
    expected_source = {
        _validate_relative(str(relative)).as_posix(): value
        for relative, value in source_tree.items()
        if isinstance(relative, str) and isinstance(value, str)
    }
    expected_staged = {
        _validate_relative(str(relative)).as_posix(): value
        for relative, value in staged_tree.items()
        if isinstance(relative, str) and isinstance(value, str)
    }
    if len(expected_source) != len(source_tree) or len(expected_staged) != len(
        staged_tree
    ):
        raise SystemExit("native manifest tree maps contain invalid or duplicate paths")
    actual_source = _tree_hashes(source_runtime, exclusions)
    actual_staged = _tree_hashes(native_runtime, exclusions)
    if actual_source != expected_source:
        raise SystemExit("native source tree hash map does not match source_runtime")
    if actual_staged != expected_staged:
        raise SystemExit("native staged tree hash map does not match native_runtime")

    patched_paths = native_manifest.get("patched_paths")
    if not isinstance(patched_paths, dict):
        raise SystemExit("native manifest has no patched_paths hash map")
    for required in ("hermes_cli/kanban_db.py", "hermes_cli/native_boundary.py"):
        if required not in patched_paths or not isinstance(
            patched_paths[required], str
        ):
            raise SystemExit(f"native manifest is missing required path: {required}")
        if expected_staged.get(required) != patched_paths[required]:
            raise SystemExit(
                f"native manifest patched path is not in staged_tree: {required}"
            )

    import_probe = native_manifest.get("import_probe")
    if not isinstance(import_probe, dict) or set(import_probe) != {
        "kanban_db",
        "kanban_specify",
        "native_boundary",
    }:
        raise SystemExit("native manifest import_probe keys are incomplete")
    for name, raw_path in import_probe.items():
        if not isinstance(raw_path, str):
            raise SystemExit(f"native import probe path is invalid: {name}")
        probe = Path(raw_path).expanduser()
        if not probe.is_absolute() or probe.is_symlink():
            raise SystemExit(
                f"native import probe is not an isolated real file: {name}"
            )
        try:
            relative = probe.resolve(strict=True).relative_to(native_runtime)
        except (OSError, ValueError) as exc:
            raise SystemExit(
                f"native import probe escapes staged runtime: {name}"
            ) from exc
        key = relative.as_posix()
        if key not in expected_staged or not probe.is_file():
            raise SystemExit(f"native import probe is not in staged_tree: {name}")
        if name == "kanban_db" and key != "hermes_cli/kanban_db.py":
            raise SystemExit("kanban_db import probe points at an unexpected file")
        if name == "native_boundary" and key != "hermes_cli/native_boundary.py":
            raise SystemExit(
                "native_boundary import probe points at an unexpected file"
            )
        if name == "kanban_specify" and key != "hermes_cli/kanban_specify.py":
            raise SystemExit("kanban_specify import probe points at an unexpected file")
    verification = native_manifest.get("verification")
    if not isinstance(verification, dict) or verification != {
        "complete_tree_verified": True,
        "import_probe_isolated": True,
        "patches_applied": True,
        "symlinks_rejected": True,
    }:
        raise SystemExit("native manifest verification flags are incomplete")
    if (
        not isinstance(native_manifest.get("patches"), dict)
        or not native_manifest["patches"]
    ):
        raise SystemExit("native manifest has no applied patch hashes")
    artifact_root_value = native_manifest.get("artifact_root")
    if not isinstance(artifact_root_value, str) or not artifact_root_value:
        raise SystemExit("native manifest has no artifact_root")
    artifact_root = Path(artifact_root_value).expanduser()
    if (
        not artifact_root.is_absolute()
        or not artifact_root.is_dir()
        or artifact_root.is_symlink()
    ):
        raise SystemExit("native artifact_root is not a real directory")
    for raw_relative, expected_hash in native_manifest["patches"].items():
        relative = _validate_relative(raw_relative)
        patch_path = artifact_root / relative
        info = patch_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SystemExit(f"native patch is not a regular file: {raw_relative}")
        if _sha256(patch_path) != expected_hash:
            raise SystemExit(f"native patch hash mismatch: {raw_relative}")
    source_files = native_manifest.get("source_files")
    if not isinstance(source_files, dict) or set(source_files) != {
        "hermes_cli/kanban_db.py",
        "hermes_cli/kanban_specify.py",
    }:
        raise SystemExit("native manifest source_files are incomplete")
    for raw_relative, expected_hash in source_files.items():
        relative = _validate_relative(raw_relative).as_posix()
        if (
            not isinstance(expected_hash, str)
            or expected_source.get(relative) != expected_hash
        ):
            raise SystemExit(f"native source_files hash mismatch: {raw_relative}")
    output_runtime = native_manifest.get("output_runtime")
    if (
        not isinstance(output_runtime, str)
        or Path(output_runtime).expanduser().resolve(strict=False) != native_runtime
    ):
        raise SystemExit("native manifest output_runtime does not match staged runtime")
    identity = _native_artifact_identity_sha256(native_manifest)
    if (
        expected_artifact_identity_sha256
        and identity != expected_artifact_identity_sha256
    ):
        raise SystemExit(
            "native artifact identity does not match the reviewed boundary"
        )
    return native_manifest


def _copy_runtime(source: Path, output: Path, patterns: list[str]) -> None:
    if not source.is_dir() or source.is_symlink():
        raise SystemExit(
            f"native runtime directory does not exist as a real directory: {source}"
        )
    source_hashes = _tree_hashes(source, patterns)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if _excluded(relative, patterns):
            continue
        destination = output / relative
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit(f"symlink is not allowed in native runtime: {relative}")
        if stat.S_ISDIR(info.st_mode):
            destination.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise SystemExit(f"hard-linked native runtime entry: {relative}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination, follow_symlinks=False)
        else:
            raise SystemExit(f"unsupported native runtime entry: {relative}")
    if _tree_hashes(output, patterns) != source_hashes:
        raise SystemExit(
            "copied runtime tree does not match the authenticated native stage"
        )


def _assert_overlay_equivalence(
    native_runtime: Path, output: Path, patterns: list[str]
) -> None:
    """Ensure the overlay changed only the declared target files."""
    native_hashes = _tree_hashes(native_runtime, patterns)
    output_hashes = _tree_hashes(output, patterns)
    native_paths = set(native_hashes)
    output_paths = set(output_hashes)
    added = output_paths - native_paths
    removed = native_paths - output_paths
    if removed or added - set(TARGET_PATHS):
        unexpected = sorted(removed | (added - set(TARGET_PATHS)))
        raise SystemExit(
            "review overlay changed the native runtime file set outside the allowlist: "
            + ", ".join(unexpected)
        )
    changed = {
        relative
        for relative in native_paths & output_paths
        if native_hashes[relative] != output_hashes[relative]
    }
    if changed - set(TARGET_PATHS):
        raise SystemExit(
            "review overlay changed files outside the target allowlist: "
            + ", ".join(sorted(changed - set(TARGET_PATHS)))
        )
    effective = added | changed
    expected = set(TARGET_PATHS)
    if effective != expected:
        missing = sorted(expected - effective)
        unexpected = sorted(effective - expected)
        detail = []
        if missing:
            detail.append("missing/unmodified=" + ", ".join(missing))
        if unexpected:
            detail.append("unexpected=" + ", ".join(unexpected))
        raise SystemExit(
            "review overlay did not change every declared target: " + "; ".join(detail)
        )
    if not set(TARGET_PATHS).issubset(output_paths):
        raise SystemExit("review overlay target file is missing from the output")


def _apply_patch(output: Path, patch_path: Path) -> None:
    targets = _patch_targets(patch_path)
    if targets != TARGET_PATHS:
        raise SystemExit(
            f"patch target set {targets!r} is outside the review allowlist"
        )
    git = str(_trusted_git_executable())
    apply_environment = dict(_GIT_ENVIRONMENT)
    # The builder may run inside an enclosing worktree.  Git discovery must
    # stop at the private output parent so patch paths stay relative to output.
    apply_environment["GIT_CEILING_DIRECTORIES"] = str(output.parent.resolve())
    apply_environment["GIT_DISCOVERY_ACROSS_FILESYSTEM"] = "0"
    discovery = subprocess.run(
        [git, "rev-parse", "--show-toplevel"],
        cwd=output,
        check=False,
        capture_output=True,
        text=True,
        env=apply_environment,
    )
    if discovery.returncode == 0:
        raise SystemExit(
            "review patch output unexpectedly discovered an enclosing Git repository"
        )
    command = [git, "apply", "--whitespace=error", str(patch_path)]
    check = subprocess.run(
        [git, "apply", "--check", "--whitespace=error", str(patch_path)],
        cwd=output,
        check=False,
        capture_output=True,
        text=True,
        env=apply_environment,
    )
    if check.returncode:
        detail = (check.stdout + check.stderr).strip()
        raise SystemExit(f"review patch failed exact git apply check: {detail}")
    result = subprocess.run(
        command,
        cwd=output,
        check=False,
        capture_output=True,
        text=True,
        env=apply_environment,
    )
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise SystemExit(f"review patch did not apply after exact check: {detail}")


def _syntax_probe(output: Path) -> list[str]:
    probed: list[str] = []
    for relative in TARGET_PATHS:
        path = output / relative
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise SystemExit(f"syntax probe failed for {relative}: {exc}") from exc
        probed.append(relative)
    return probed


def _manifest_output(output: Path, explicit: str | None) -> Path:
    return (
        Path(explicit).expanduser()
        if explicit
        else output.parent / f"{output.name}.manifest.json"
    )


def _path_overlaps(left: Path, right: Path) -> bool:
    """Return true when either path contains the other."""
    left = left.resolve(strict=False)
    right = right.resolve(strict=False)
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _absolute_lexical_path(path: Path) -> Path:
    """Return an absolute normalized path without resolving symlinks."""
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.abspath(os.fspath(expanded)))


def _manifest_directory_flags() -> int:
    """Return the fail-closed flags required for directory traversal."""
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    missing = [name for name in required if not hasattr(os, name)]
    if missing:
        raise SystemExit(
            "secure manifest publication is unsupported: missing " + ", ".join(missing)
        )
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_chain(path: Path, *, create: bool) -> int:
    """Open an absolute directory one non-symlink component at a time."""
    absolute = _absolute_lexical_path(path)
    flags = _manifest_directory_flags()
    current_fd = os.open(os.sep, flags)
    completed = False
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise SystemExit(
                        f"manifest destination parent disappeared: {absolute}"
                    ) from None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    # A concurrent creator won. The no-follow open below must
                    # authenticate the winner as a real directory.
                    pass
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except OSError as exc:
                    raise SystemExit(
                        "manifest destination parent contains a symlink or "
                        f"non-directory component: {absolute}"
                    ) from exc
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise SystemExit(
                        "manifest destination parent contains a symlink or "
                        f"non-directory component: {absolute}"
                    ) from exc
                raise SystemExit(
                    f"cannot open manifest destination parent {absolute}: {exc}"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        completed = True
        return current_fd
    finally:
        if not completed:
            os.close(current_fd)


def _manifest_entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _assert_manifest_parent_identity(parent: Path, expected_fd: int) -> None:
    """Require the pathname to still identify the opened directory."""
    try:
        current_fd = _open_directory_chain(parent, create=False)
    except SystemExit as exc:
        raise SystemExit(
            "manifest destination parent changed or became unsafe during publication"
        ) from exc
    try:
        expected = os.fstat(expected_fd)
        current = os.fstat(current_fd)
        if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
            raise SystemExit("manifest destination parent changed during publication")
    finally:
        os.close(current_fd)


def _validate_manifest_destination(
    destination: Path,
    protected_roots: tuple[Path, ...],
) -> Path:
    """Authenticate a new, separate manifest destination before any write.

    Every parent component is opened with ``O_NOFOLLOW`` before the final name
    is checked. The lexical absolute path is retained so validation never
    silently converts a caller-supplied symlink into an accepted destination.
    """
    absolute = _absolute_lexical_path(destination)
    if not absolute.name:
        raise SystemExit("manifest destination must name a file")
    directory_fd = _open_directory_chain(absolute.parent, create=True)
    try:
        _assert_manifest_parent_identity(absolute.parent, directory_fd)
        if _manifest_entry_exists(directory_fd, absolute.name):
            raise SystemExit(f"manifest destination already exists: {absolute}")
        for protected in protected_roots:
            if _path_overlaps(absolute, protected):
                raise SystemExit(
                    f"manifest destination overlaps protected input/output: {absolute}"
                )
        _assert_manifest_parent_identity(absolute.parent, directory_fd)
        return absolute
    finally:
        os.close(directory_fd)


def _write_json_exclusive(
    destination: Path,
    value: dict[str, Any],
    protected_roots: tuple[Path, ...] = (),
) -> None:
    """Publish one private JSON file atomically without clobbering a path.

    Directory-descriptor-relative operations keep publication anchored to the
    exact parent inode authenticated above. A hard-link publishes the final
    name with create-if-absent semantics; an existing file, directory, or
    symlink therefore wins and the builder fails closed instead of replacing
    it. Parent replacement never redirects the write.
    """
    destination = _validate_manifest_destination(destination, protected_roots)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    directory_fd = _open_directory_chain(destination.parent, create=False)
    temporary_name: str | None = None
    payload_identity: tuple[int, int] | None = None
    published = False
    fd = -1
    try:
        _assert_manifest_parent_identity(destination.parent, directory_fd)
        if _manifest_entry_exists(directory_fd, destination.name):
            raise SystemExit(f"manifest destination already exists: {destination}")

        for _ in range(128):
            candidate = f".{destination.name}.{secrets.token_hex(16)}.tmp"
            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if fd < 0 or temporary_name is None:
            raise SystemExit("cannot reserve a private manifest staging file")

        os.fchmod(fd, 0o600)
        payload_stat = os.fstat(fd)
        payload_identity = (payload_stat.st_dev, payload_stat.st_ino)
        payload_fd = fd
        fd = -1
        with os.fdopen(payload_fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_manifest_parent_identity(destination.parent, directory_fd)
        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise SystemExit(
                f"manifest destination already exists: {destination}"
            ) from exc
        published = True
        final_stat = os.stat(
            destination.name, dir_fd=directory_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(final_stat.st_mode)
            or (final_stat.st_dev, final_stat.st_ino) != payload_identity
        ):
            raise SystemExit("manifest publication identity check failed")
        _assert_manifest_parent_identity(destination.parent, directory_fd)
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_name = None
        final_stat = os.stat(
            destination.name, dir_fd=directory_fd, follow_symlinks=False
        )
        if final_stat.st_nlink != 1:
            raise SystemExit("published manifest must have exactly one hard link")
        os.fsync(directory_fd)
        _assert_manifest_parent_identity(destination.parent, directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if published and payload_identity is not None:
            try:
                final_stat = os.stat(
                    destination.name, dir_fd=directory_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                final_stat = None
            if (
                sys.exc_info()[0] is not None
                and final_stat is not None
                and (final_stat.st_dev, final_stat.st_ino) == payload_identity
            ):
                # Keep the successfully published file only when no exception
                # is active. On an identity/race failure, remove only our inode.
                os.unlink(destination.name, dir_fd=directory_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _runtime_entry_exists(directory_fd: int, relative: str) -> bool:
    try:
        os.stat(relative, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _runtime_directory_identity_at(directory_fd: int, relative: str) -> tuple[int, int]:
    info = os.stat(relative, dir_fd=directory_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SystemExit(f"expected a real runtime directory: {relative}")
    return info.st_dev, info.st_ino


def _assert_runtime_parent_identity(output: Path, directory_fd: int) -> None:
    try:
        _assert_manifest_parent_identity(output.parent, directory_fd)
    except SystemExit as exc:
        raise SystemExit(
            "runtime output parent changed or became unsafe during publication"
        ) from exc


def _validate_runtime_output_destination(
    output: Path, *, force: bool
) -> tuple[Path, int, bool]:
    """Open and retain the lexical output parent without following symlinks."""
    absolute = _absolute_lexical_path(output)
    if not absolute.name:
        raise SystemExit("runtime output must name a directory")
    try:
        directory_fd = _open_directory_chain(absolute.parent, create=True)
    except SystemExit as exc:
        raise SystemExit(
            f"runtime output path contains a symlink or unsafe parent: {absolute}"
        ) from exc
    try:
        _assert_runtime_parent_identity(absolute, directory_fd)
        try:
            info = os.stat(absolute.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return absolute, directory_fd, False
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit(f"runtime output path contains a symlink: {absolute}")
        if not stat.S_ISDIR(info.st_mode):
            raise SystemExit(f"runtime output is not a directory: {absolute}")
        if not force:
            raise SystemExit(
                f"output already exists; use --force to replace it: {absolute}"
            )
        _assert_runtime_parent_identity(absolute, directory_fd)
        return absolute, directory_fd, True
    except (Exception, SystemExit, KeyboardInterrupt):
        os.close(directory_fd)
        raise


def _reserve_private_sibling_directory(
    output: Path, purpose: str, *, directory_fd: int
) -> Path:
    """Reserve a private same-filesystem directory beside ``output``."""
    _assert_runtime_parent_identity(output, directory_fd)
    for _ in range(128):
        name = f".{output.name}.{purpose}.{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=directory_fd)
        except FileExistsError:
            continue
        candidate = output.parent / name
        try:
            _assert_runtime_parent_identity(output, directory_fd)
            identity = _runtime_directory_identity_at(directory_fd, name)
            if _directory_identity(candidate) != identity:
                raise SystemExit(
                    f"private {purpose} directory path changed during reservation"
                )
        except (Exception, SystemExit, KeyboardInterrupt):
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except OSError:
                pass
            raise
        return candidate
    raise SystemExit(f"cannot reserve private {purpose} directory beside {output}")


def _directory_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SystemExit(f"expected a real private directory: {path}")
    return info.st_dev, info.st_ino


def _remove_owned_directory(
    path: Path,
    identity: tuple[int, int],
    *,
    output: Path | None = None,
    directory_fd: int | None = None,
) -> None:
    """Remove only the exact directory inode created by this build."""
    if directory_fd is not None:
        if output is None:
            raise SystemExit("anchored runtime cleanup requires the output path")
        _assert_runtime_parent_identity(output, directory_fd)
        if not _runtime_entry_exists(directory_fd, path.name):
            return
        if _runtime_directory_identity_at(directory_fd, path.name) != identity:
            raise SystemExit(f"refusing to remove replaced build directory: {path}")
    elif not os.path.lexists(path):
        return
    if _directory_identity(path) != identity:
        raise SystemExit(f"refusing to remove replaced build directory: {path}")
    shutil.rmtree(path)
    if directory_fd is not None:
        if output is None:
            raise SystemExit("anchored runtime cleanup requires the output path")
        _assert_runtime_parent_identity(output, directory_fd)
        if _runtime_entry_exists(directory_fd, path.name):
            raise SystemExit(f"owned build directory survived cleanup: {path}")


def build(
    native_runtime: Path,
    native_manifest_path: Path,
    output: Path,
    manifest_output: Path,
    force: bool,
) -> Path:
    static = _static_manifest()
    patterns = list(static["excluded_copy_entries"])
    if native_runtime.is_symlink() or native_manifest_path.is_symlink():
        raise SystemExit("native inputs must not be symlink paths")
    native_runtime = native_runtime.resolve(strict=False)
    native_manifest_path = native_manifest_path.resolve(strict=False)
    native_manifest = _validate_native_input(
        native_runtime,
        native_manifest_path,
        expected_artifact_identity_sha256=static["native_artifact_identity_sha256"],
        expected_exclusions=patterns,
        expected_artifact_version=static["native_artifact_version"],
    )
    output, output_parent_fd, output_existed = _validate_runtime_output_destination(
        output, force=force
    )
    try:
        source_runtime = (
            Path(native_manifest["source_runtime"]).expanduser().resolve(strict=False)
        )
        for protected in (native_runtime, source_runtime, ARTIFACT_ROOT):
            if _path_overlaps(output, protected):
                raise SystemExit(
                    "review runtime output collides with protected "
                    f"input/artifact path: {output}"
                )
        manifest_protected_roots = (
            native_runtime,
            source_runtime,
            ARTIFACT_ROOT,
            output,
        )
        manifest_output = _validate_manifest_destination(
            manifest_output, manifest_protected_roots
        )
        staging = _reserve_private_sibling_directory(
            output, "stage", directory_fd=output_parent_fd
        )
        staging_identity = _runtime_directory_identity_at(
            output_parent_fd, staging.name
        )
    except (Exception, SystemExit, KeyboardInterrupt):
        os.close(output_parent_fd)
        raise
    backup_root: Path | None = None
    backup_root_identity: tuple[int, int] | None = None
    backup_output: Path | None = None
    prior_identity: tuple[int, int] | None = None
    runtime_published = False
    try:
        _copy_runtime(native_runtime, staging, patterns)
        _apply_patch(staging, ARTIFACT_ROOT / PATCH_RELATIVE)
        _assert_overlay_equivalence(native_runtime, staging, patterns)
        probed = _syntax_probe(staging)
        generated = {
            "schema": STATIC_SCHEMA,
            "artifact_version": static["artifact_version"],
            "native_boundary_schema": NATIVE_SCHEMA,
            "native_manifest": str(native_manifest_path),
            "native_manifest_sha256": _sha256(native_manifest_path),
            "native_artifact_identity_sha256": _native_artifact_identity_sha256(
                native_manifest
            ),
            "native_source_runtime": native_manifest.get("source_runtime"),
            "native_source_tree_sha256": _tree_digest(source_runtime, patterns),
            "native_staged_tree_sha256": _tree_digest(native_runtime, patterns),
            "native_verification": native_manifest["verification"],
            "output_runtime": str(output),
            "patches": {PATCH_RELATIVE: _sha256(ARTIFACT_ROOT / PATCH_RELATIVE)},
            "patched_paths": {
                relative: _sha256(staging / relative) for relative in TARGET_PATHS
            },
            "policy": static["policy"],
            "syntax_probe": probed,
            "runtime_tree_sha256": _tree_digest(staging, patterns),
        }

        if output_existed:
            # Keep the prior runtime intact until the complete replacement has
            # passed copy, patch, equivalence, and syntax verification.
            _assert_runtime_parent_identity(output, output_parent_fd)
            prior_identity = _runtime_directory_identity_at(
                output_parent_fd, output.name
            )
            if _directory_identity(output) != prior_identity:
                raise SystemExit("prior runtime path changed before backup")
            backup_root = _reserve_private_sibling_directory(
                output, "rollback", directory_fd=output_parent_fd
            )
            backup_root_identity = _runtime_directory_identity_at(
                output_parent_fd, backup_root.name
            )
            backup_output = backup_root / "previous-runtime"
            backup_relative = f"{backup_root.name}/{backup_output.name}"
            try:
                os.rename(
                    output.name,
                    backup_relative,
                    src_dir_fd=output_parent_fd,
                    dst_dir_fd=output_parent_fd,
                )
                _assert_runtime_parent_identity(output, output_parent_fd)
                if (
                    _runtime_directory_identity_at(output_parent_fd, backup_relative)
                    != prior_identity
                    or _directory_identity(backup_output) != prior_identity
                ):
                    raise SystemExit("prior runtime identity changed during backup")
            except (Exception, SystemExit, KeyboardInterrupt):
                if _runtime_entry_exists(
                    output_parent_fd, backup_relative
                ) and not _runtime_entry_exists(output_parent_fd, output.name):
                    os.rename(
                        backup_relative,
                        output.name,
                        src_dir_fd=output_parent_fd,
                        dst_dir_fd=output_parent_fd,
                    )
                raise

        try:
            _assert_runtime_parent_identity(output, output_parent_fd)
            if _runtime_entry_exists(output_parent_fd, output.name):
                raise SystemExit(
                    f"runtime output appeared during publication: {output}"
                )
            os.rename(
                staging.name,
                output.name,
                src_dir_fd=output_parent_fd,
                dst_dir_fd=output_parent_fd,
            )
            runtime_published = True
            _assert_runtime_parent_identity(output, output_parent_fd)
            if (
                _runtime_directory_identity_at(output_parent_fd, output.name)
                != staging_identity
                or _directory_identity(output) != staging_identity
            ):
                raise SystemExit("published runtime identity check failed")
            _write_json_exclusive(
                manifest_output,
                generated,
                protected_roots=manifest_protected_roots + (staging,),
            )
        except (Exception, SystemExit, KeyboardInterrupt) as publish_error:
            rollback_errors: list[str] = []
            if runtime_published:
                try:
                    _remove_owned_directory(
                        output,
                        staging_identity,
                        output=output,
                        directory_fd=output_parent_fd,
                    )
                    runtime_published = False
                except (OSError, SystemExit) as exc:
                    rollback_errors.append(f"cannot remove failed replacement: {exc}")
            if backup_output is not None and backup_root is not None:
                backup_relative = f"{backup_root.name}/{backup_output.name}"
                if _runtime_entry_exists(output_parent_fd, backup_relative):
                    try:
                        _assert_runtime_parent_identity(output, output_parent_fd)
                        if _runtime_entry_exists(output_parent_fd, output.name):
                            raise SystemExit(
                                "replacement path is occupied; refusing unsafe rollback"
                            )
                        os.rename(
                            backup_relative,
                            output.name,
                            src_dir_fd=output_parent_fd,
                            dst_dir_fd=output_parent_fd,
                        )
                        _assert_runtime_parent_identity(output, output_parent_fd)
                        if (
                            prior_identity is None
                            or _runtime_directory_identity_at(
                                output_parent_fd, output.name
                            )
                            != prior_identity
                            or _directory_identity(output) != prior_identity
                        ):
                            raise SystemExit("restored runtime identity check failed")
                    except (OSError, SystemExit) as exc:
                        rollback_errors.append(f"cannot restore prior runtime: {exc}")
            if rollback_errors:
                location = str(backup_output) if backup_output else "none"
                raise SystemExit(
                    "runtime publication failed and rollback was incomplete; "
                    f"backup={location}; {'; '.join(rollback_errors)}"
                ) from publish_error
            raise

        if backup_root is not None and backup_root_identity is not None:
            try:
                _assert_runtime_parent_identity(output, output_parent_fd)
                if (
                    _runtime_directory_identity_at(output_parent_fd, backup_root.name)
                    != backup_root_identity
                    or _directory_identity(backup_root) != backup_root_identity
                ):
                    raise SystemExit("prior-runtime backup identity changed")
                shutil.rmtree(backup_root)
                _assert_runtime_parent_identity(output, output_parent_fd)
            except (OSError, SystemExit) as exc:
                print(
                    f"warning: verified prior-runtime backup remains at {backup_root}: {exc}",
                    file=sys.stderr,
                )
        return manifest_output
    finally:
        try:
            if _runtime_entry_exists(output_parent_fd, staging.name):
                _remove_owned_directory(
                    staging,
                    staging_identity,
                    output=output,
                    directory_fd=output_parent_fd,
                )
            if (
                backup_root is not None
                and backup_root_identity is not None
                and _runtime_entry_exists(output_parent_fd, backup_root.name)
            ):
                _assert_runtime_parent_identity(output, output_parent_fd)
                if (
                    _runtime_directory_identity_at(output_parent_fd, backup_root.name)
                    != backup_root_identity
                ):
                    raise SystemExit("rollback directory identity changed")
                if not any(backup_root.iterdir()):
                    os.rmdir(backup_root.name, dir_fd=output_parent_fd)
        finally:
            os.close(output_parent_fd)


def verify(output: Path, manifest_path: Path) -> None:
    static = _static_manifest()
    manifest_info = manifest_path.lstat()
    if not stat.S_ISREG(manifest_info.st_mode) or manifest_info.st_nlink != 1:
        raise SystemExit("generated manifest must be a regular, non-hard-linked file")
    manifest = _load_json(manifest_path)
    if manifest.get("schema") != STATIC_SCHEMA:
        raise SystemExit("generated manifest has an unexpected schema")
    if (
        manifest.get("native_artifact_identity_sha256")
        != static["native_artifact_identity_sha256"]
    ):
        raise SystemExit(
            "generated manifest is not bound to the reviewed native artifact identity"
        )
    native_manifest_path_value = manifest.get("native_manifest")
    if not isinstance(native_manifest_path_value, str):
        raise SystemExit("generated manifest has no native manifest path")
    native_manifest_path = Path(native_manifest_path_value).resolve(strict=False)
    native_manifest = _load_json(native_manifest_path)
    native_runtime_value = native_manifest.get("output_runtime")
    if not isinstance(native_runtime_value, str):
        raise SystemExit("native manifest has no staged output_runtime")
    native_runtime = Path(native_runtime_value).resolve(strict=False)
    patterns = list(static["excluded_copy_entries"])
    checked_native = _validate_native_input(
        native_runtime,
        native_manifest_path,
        expected_artifact_identity_sha256=static["native_artifact_identity_sha256"],
        expected_exclusions=patterns,
        expected_artifact_version=static["native_artifact_version"],
    )
    if manifest.get("native_manifest_sha256") != _sha256(native_manifest_path):
        raise SystemExit("generated manifest native manifest hash mismatch")
    if checked_native.get("verification") != manifest.get("native_verification"):
        raise SystemExit(
            "generated manifest native verification does not match the boundary"
        )
    source_runtime = (
        Path(checked_native["source_runtime"]).expanduser().resolve(strict=False)
    )
    if manifest.get("native_source_tree_sha256") != _tree_digest(
        source_runtime, patterns
    ):
        raise SystemExit("generated manifest native source tree hash mismatch")
    if manifest.get("native_staged_tree_sha256") != _tree_digest(
        native_runtime, patterns
    ):
        raise SystemExit("generated manifest native staged tree hash mismatch")
    if manifest.get("output_runtime") != str(output):
        raise SystemExit(
            "generated manifest output_runtime does not match requested runtime"
        )
    if (
        manifest.get("patches", {}).get(PATCH_RELATIVE)
        != static["patches"][PATCH_RELATIVE]
    ):
        raise SystemExit("generated manifest patch hash is not the reviewed patch")
    if manifest.get("policy") != static.get("policy"):
        raise SystemExit(
            "generated manifest policy does not match the reviewed contract"
        )
    expected_paths = manifest.get("patched_paths")
    if not isinstance(expected_paths, dict):
        raise SystemExit("generated manifest has no patched_paths")
    if set(expected_paths) != set(TARGET_PATHS) or len(expected_paths) != len(
        TARGET_PATHS
    ):
        raise SystemExit(
            "generated manifest target set does not match the reviewed allowlist"
        )
    for relative in TARGET_PATHS:
        path = output / _validate_relative(relative)
        if not path.is_file() or _sha256(path) != expected_paths.get(relative):
            raise SystemExit(f"generated runtime hash mismatch: {relative}")
    probed = _syntax_probe(output)
    if probed != manifest.get("syntax_probe"):
        raise SystemExit(
            "generated syntax probe does not match the reviewed target set"
        )
    patterns = list(static["excluded_copy_entries"])
    if _tree_digest(output, patterns) != manifest.get("runtime_tree_sha256"):
        raise SystemExit("generated runtime tree hash mismatch")
    print(
        json.dumps(
            {
                "status": "verified",
                "runtime": str(output),
                "manifest": str(manifest_path),
            }
        )
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage = subparsers.add_parser(
        "stage", help="copy a native stage and apply the review overlay"
    )
    stage.add_argument("--native-runtime", required=True, type=Path)
    stage.add_argument("--native-manifest", required=True, type=Path)
    stage.add_argument("--output", required=True, type=Path)
    stage.add_argument("--manifest-output", type=Path)
    stage.add_argument("--force", action="store_true")
    check = subparsers.add_parser(
        "verify", help="verify a previously built review runtime"
    )
    check.add_argument("--runtime", required=True, type=Path)
    check.add_argument("--manifest", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.command == "stage":
        output = _absolute_lexical_path(args.output)
        manifest = build(
            args.native_runtime,
            args.native_manifest,
            output,
            _manifest_output(
                output,
                str(args.manifest_output) if args.manifest_output else None,
            ),
            args.force,
        )
        print(
            json.dumps(
                {
                    "status": "staged",
                    "runtime": str(output),
                    "manifest": str(manifest),
                }
            )
        )
    else:
        verify(args.runtime.resolve(), args.manifest.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
