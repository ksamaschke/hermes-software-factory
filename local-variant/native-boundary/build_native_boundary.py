#!/usr/bin/env python3
"""Build and verify the Factory native-boundary compatibility artifact.

The command only prepares a fresh copied runtime.  It never installs the
candidate, edits a live board, restarts a gateway, or launches a worker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

ARTIFACT_SCHEMA = "factory.native-boundary.v1"
ARTIFACT_VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent
STATIC_MANIFEST = ROOT / "manifest.json"
_PATCH_EXECUTABLE = Path("/usr/bin/patch")
_FIXED_EXECUTABLE_PATH = "/usr/bin:/bin"
_PATCH_ENVIRONMENT = {
    "HOME": "/nonexistent",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": _FIXED_EXECUTABLE_PATH,
}

_COPY_IGNORE_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "node_modules",
        "dist",
        "build",
    }
)
_COPY_IGNORE_SUFFIXES = (".pyc", ".pyo")
_FORBIDDEN_IMPORT_NAMES = frozenset({"sitecustomize.py", "usercustomize.py"})
_FORBIDDEN_IMPORT_SUFFIXES = (".pth",)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(raw: str) -> str:
    """Accept only an unambiguous relative POSIX artifact path."""
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"unsafe artifact path: {raw!r}")
    if "\\" in raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError(f"unsafe artifact path: {raw!r}")
    raw_parts = raw.split("/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or path.as_posix() != raw
    ):
        raise ValueError(f"unsafe artifact path: {raw!r}")
    return raw


def _resolve_directory(raw: str, *, label: str, must_exist: bool = True) -> Path:
    path = Path(raw).expanduser()
    _reject_symlink_components(path)
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    if must_exist and not path.is_dir():
        raise ValueError(f"{label} is not a directory: {path}")
    return path.resolve(strict=must_exist)


def _reject_symlink_components(path: Path) -> None:
    """Reject symlinks in an existing path's components."""
    current = path
    while True:
        if current.is_symlink():
            raise ValueError(f"symlinked path component is unsafe: {current}")
        if current.parent == current:
            return
        current = current.parent


def _walk_without_symlinks(root: Path) -> Iterable[Path]:
    """Yield every entry and reject links, hardlinks, and special files."""
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                item = Path(entry.path)
                item_stat = entry.stat(follow_symlinks=False)
                if entry.is_symlink():
                    raise ValueError(f"symlinked runtime entry is unsafe: {item}")
                if stat.S_ISREG(item_stat.st_mode) and item_stat.st_nlink > 1:
                    raise ValueError(f"hard-linked runtime entry is unsafe: {item}")
                if not (
                    stat.S_ISREG(item_stat.st_mode) or stat.S_ISDIR(item_stat.st_mode)
                ):
                    raise ValueError(f"special runtime entry is unsafe: {item}")
                yield item
                if stat.S_ISDIR(item_stat.st_mode):
                    stack.append(item)


def _is_copy_ignored(name: str) -> bool:
    return name in _COPY_IGNORE_NAMES or name.endswith(_COPY_IGNORE_SUFFIXES)


def _tree_files(root: Path, *, allow_excluded: bool) -> dict[str, str]:
    """Hash every copied regular file and reject anything not copy-safe.

    Source trees may contain the explicitly excluded cache/VCS entries used by
    ``copytree(ignore=...)``.  A staged tree may not contain them: an excluded
    entry in output would otherwise be an unpinned file outside the digest.
    """
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"runtime is not a safe directory: {root}")
    _reject_symlink_components(root)
    output: dict[str, str] = {}
    stack = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        for entry in entries:
            item = Path(entry.path)
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            item_stat = entry.stat(follow_symlinks=False)
            if entry.is_symlink():
                raise ValueError(f"symlinked runtime entry is unsafe: {item}")
            if stat.S_ISDIR(item_stat.st_mode):
                if _is_copy_ignored(entry.name):
                    if not allow_excluded:
                        raise ValueError(
                            f"excluded runtime entry in staged output: {item}"
                        )
                    continue
                stack.append((item, relative))
                continue
            if not stat.S_ISREG(item_stat.st_mode):
                raise ValueError(f"special runtime entry is unsafe: {item}")
            if _is_copy_ignored(entry.name):
                if not allow_excluded:
                    raise ValueError(f"excluded runtime entry in staged output: {item}")
                continue
            if item_stat.st_nlink > 1:
                raise ValueError(f"hard-linked runtime entry is unsafe: {item}")
            output[_safe_relative(relative)] = _sha256(item)
    return dict(sorted(output.items()))


def _reject_forbidden_import_entries(root: Path) -> None:
    """Reject files that can run before the requested probe imports."""
    for item in _walk_without_symlinks(root):
        if item.name in _FORBIDDEN_IMPORT_NAMES or item.name.endswith(
            _FORBIDDEN_IMPORT_SUFFIXES
        ):
            raise ValueError(f"import-hook runtime entry is unsafe: {item}")


def _load_json(path: Path) -> dict[str, Any]:
    _reject_symlink_components(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"manifest must be a JSON object: {path}")
    return value


def _static_manifest() -> dict[str, Any]:
    manifest = _load_json(STATIC_MANIFEST)
    if manifest.get("schema") != ARTIFACT_SCHEMA:
        raise ValueError("static manifest schema mismatch")
    if manifest.get("artifact_version") != ARTIFACT_VERSION:
        raise ValueError("static manifest artifact version mismatch")
    for entry in manifest.get("source_files", []):
        _safe_relative(entry["path"])
        if len(str(entry.get("sha256", ""))) != 64:
            raise ValueError(f"invalid source pin for {entry.get('path')!r}")
    for entry in manifest.get("patches", []):
        _safe_relative(entry["path"])
        if len(str(entry.get("sha256", ""))) != 64:
            raise ValueError(f"invalid patch pin for {entry.get('path')!r}")
    return manifest


def _check_pins(source: Path, manifest: dict[str, Any]) -> dict[str, str]:
    observed: dict[str, str] = {}
    for entry in manifest["source_files"]:
        relative = _safe_relative(entry["path"])
        path = source / relative
        _reject_symlink_components(path)
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"pinned source file is missing or unsafe: {path}")
        digest = _sha256(path)
        observed[relative] = digest
        if digest != entry["sha256"]:
            raise ValueError(
                f"source hash mismatch for {relative}: expected {entry['sha256']}, observed {digest}"
            )
    return observed


def _patch_entries(manifest: dict[str, Any]) -> list[tuple[Path, str]]:
    entries: list[tuple[Path, str]] = []
    for item in manifest["patches"]:
        relative = _safe_relative(item["path"])
        patch_path = ROOT / relative
        _reject_symlink_components(patch_path)
        if not patch_path.is_file() or patch_path.is_symlink():
            raise ValueError(f"artifact patch is missing or unsafe: {patch_path}")
        digest = _sha256(patch_path)
        if digest != item["sha256"]:
            raise ValueError(
                f"patch hash mismatch for {relative}: expected {item['sha256']}, observed {digest}"
            )
        entries.append((patch_path, relative))
    return entries


def _patch_targets(manifest: dict[str, Any]) -> list[str]:
    targets = [_safe_relative(item) for item in manifest["patched_paths"]]
    allowed = set(targets)
    if len(allowed) != len(targets):
        raise ValueError("duplicate patched path in manifest")
    return targets


def _patch_header_path(raw: str, prefix: str) -> str | None:
    token = raw.rstrip("\n").split("\t", 1)[0].split(" ", 1)[0]
    if token == "/dev/null":
        return None
    if not token.startswith(f"{prefix}/"):
        raise ValueError(f"patch header is not rooted under {prefix}/: {token!r}")
    return _safe_relative(token[len(prefix) + 1 :])


def _patch_header_targets(patch_path: Path) -> set[str]:
    """Parse unified headers and reject mode/path escape syntax."""
    lines = patch_path.read_text(encoding="utf-8").splitlines()
    pairs: list[tuple[str | None, str | None]] = []
    forbidden_prefixes = (
        "diff --git ",
        "old mode ",
        "new mode ",
        "deleted file mode ",
        "new file mode ",
        "rename from ",
        "rename to ",
        "copy from ",
        "copy to ",
        "Index: ",
    )
    for line in lines:
        if line.startswith(forbidden_prefixes):
            raise ValueError(f"unsupported patch metadata in {patch_path}: {line!r}")
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("--- "):
            continue
        next_line = lines[index + 1]
        if not next_line.startswith("+++ "):
            raise ValueError(f"malformed unified patch header in {patch_path}")
        old_path = _patch_header_path(line[4:], "a")
        new_path = _patch_header_path(next_line[4:], "b")
        if old_path is not None and new_path is not None and old_path != new_path:
            raise ValueError(
                f"patch rename/copy is outside the exact target contract: {old_path!r} -> {new_path!r}"
            )
        if old_path is None and new_path is None:
            raise ValueError(f"patch header has no target in {patch_path}")
        pairs.append((old_path, new_path))
    if not pairs:
        raise ValueError(f"patch has no unified file headers: {patch_path}")
    targets: set[str] = set()
    for old_path, new_path in pairs:
        target = new_path if new_path is not None else old_path
        if target is None:  # pragma: no cover - guarded above
            raise ValueError(f"patch header has no target in {patch_path}")
        targets.add(target)
    return targets


def _validate_patch_targets(
    patches: list[tuple[Path, str]], targets: list[str]
) -> None:
    allowed = set(targets)
    observed: set[str] = set()
    for patch_path, _relative in patches:
        patch_targets = _patch_header_targets(patch_path)
        outside = patch_targets - allowed
        if outside:
            raise ValueError(
                f"patch target is outside the exact allowlist: {sorted(outside)}"
            )
        observed.update(patch_targets)
    if not observed:
        raise ValueError("patch has no exact allowlisted targets")


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if _is_copy_ignored(name)}


def _authenticated_patch_command() -> tuple[list[str], dict[str, str]]:
    """Return the fixed patch tool and a scrubbed execution environment."""
    executable = _PATCH_EXECUTABLE
    if not executable.is_absolute():
        raise RuntimeError(f"patch executable must be absolute: {executable}")
    _reject_symlink_components(executable)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError(
            f"patch executable is missing or not executable: {executable}"
        )
    if executable.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError(f"patch executable is group/world writable: {executable}")
    return [str(executable)], dict(_PATCH_ENVIRONMENT)


def _apply_patches(
    staging: Path,
    patches: list[tuple[Path, str]],
    targets: list[str] | None = None,
) -> None:
    if targets is None:
        targets = _patch_targets(_static_manifest())
    _validate_patch_targets(patches, targets)
    for patch_path, _relative in patches:
        executable, environment = _authenticated_patch_command()
        command = [
            *executable,
            "--batch",
            "--forward",
            "--fuzz=0",
            "-p1",
            "--input",
            str(patch_path),
        ]
        completed = subprocess.run(
            command,
            cwd=staging,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stdout or "").strip()[-2000:]
            raise RuntimeError(f"patch application failed for {patch_path}: {detail}")


def _expected_patched_tree(
    source: Path,
    patches: list[tuple[Path, str]],
    targets: list[str],
) -> dict[str, str]:
    """Apply the pinned patch to only its exact inputs for independent proof."""
    with tempfile.TemporaryDirectory(
        prefix=".native-boundary-expected-", dir=str(source.parent)
    ) as temporary:
        expected_root = Path(temporary)
        for relative in targets:
            source_path = source / relative
            if not source_path.exists():
                continue
            _reject_symlink_components(source_path)
            if not source_path.is_file() or source_path.is_symlink():
                raise ValueError(f"patch input is missing or unsafe: {source_path}")
            destination = expected_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, destination)
        _apply_patches(expected_root, patches, targets)
        expected = _tree_files(expected_root, allow_excluded=False)
    if set(expected) != set(targets):
        raise ValueError(
            "pinned patch did not produce the complete exact target set: "
            f"expected={sorted(targets)}, observed={sorted(expected)}"
        )
    return expected


def _verify_tree(
    root: Path,
    targets: list[str] | None = None,
    *,
    expected: dict[str, str] | None = None,
) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"staged runtime is not a safe directory: {root}")
    _reject_symlink_components(root)
    for item in _walk_without_symlinks(root):
        if item.is_file() and item.name.endswith((".orig", ".rej")):
            raise ValueError(f"partial patch artifact remains: {item}")
    if expected is not None:
        observed_tree = _tree_files(root, allow_excluded=False)
        if observed_tree != expected:
            raise ValueError(
                "staged runtime tree does not match the pinned source and patch"
            )
        return {relative: observed_tree[relative] for relative in targets or expected}
    if targets is None:
        raise ValueError("tree verification requires targets or an expected tree")
    output_files: dict[str, str] = {}
    for relative in targets:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"patched output is missing or unsafe: {path}")
        output_files[relative] = _sha256(path)
    return output_files


def _import_probe(runtime: Path) -> dict[str, str]:
    _reject_forbidden_import_entries(runtime)
    probe = (
        "import json\n"
        "import sysconfig\n"
        "import sys\n"
        "from pathlib import Path\n"
        "venv_site = (Path(sys.executable).parent.parent / 'lib' /\n"
        "            f'python{sys.version_info.major}.{sys.version_info.minor}' /\n"
        "            'site-packages')\n"
        "sys.path.insert(0, str(venv_site))\n"
        "sys.path.insert(0, sysconfig.get_paths()['purelib'])\n"
        "from hermes_cli import kanban_db, kanban_specify\n"
        "from hermes_cli import native_boundary\n"
        "print(json.dumps({\n"
        "  'kanban_db': str(Path(kanban_db.__file__).resolve()),\n"
        "  'kanban_specify': str(Path(kanban_specify.__file__).resolve()),\n"
        "  'native_boundary': str(Path(native_boundary.__file__).resolve()),\n"
        "}))\n"
    )
    environment = {
        "HERMES_HOME": str(runtime / ".probe-home"),
        "PATH": _FIXED_EXECUTABLE_PATH,
        "PYTHONPATH": str(runtime),
        "PYTHONNOUSERSITE": "1",
        "PYTHONHASHSEED": "0",
    }
    completed = subprocess.run(
        [sys.executable, "-B", "-S", "-c", probe],
        cwd=runtime,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "staged runtime import probe failed: "
            + (completed.stderr or completed.stdout or "unknown error")
        )
    try:
        paths = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"staged import probe returned invalid JSON: {completed.stdout!r}"
        ) from exc
    if not isinstance(paths, dict):
        raise TypeError("staged import probe returned a non-object")
    expected_paths = {
        "kanban_db": runtime / "hermes_cli" / "kanban_db.py",
        "kanban_specify": runtime / "hermes_cli" / "kanban_specify.py",
        "native_boundary": runtime / "hermes_cli" / "native_boundary.py",
    }
    for name, expected in expected_paths.items():
        imported = Path(str(paths.get(name, ""))).resolve()
        if imported != expected.resolve():
            raise RuntimeError(
                f"{name} imported outside its exact staged path: {imported}"
            )
    return {str(key): str(value) for key, value in paths.items()}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _reject_symlink_components(path.parent)
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _expected_tree(
    source_tree: dict[str, str], patched_tree: dict[str, str]
) -> dict[str, str]:
    expected = dict(source_tree)
    expected.update(patched_tree)
    return dict(sorted(expected.items()))


def _verification_metadata() -> dict[str, bool]:
    return {
        "complete_tree_verified": True,
        "import_probe_isolated": True,
        "patches_applied": True,
        "symlinks_rejected": True,
    }


def _new_destination(raw: str | Path, *, label: str) -> Path:
    path = Path(raw).expanduser()
    _reject_symlink_components(path)
    if path.exists() or path.is_symlink():
        raise ValueError(f"{label} must be a new non-symlink path: {path}")
    return path.resolve(strict=False)


def _reject_destination_boundary(
    path: Path, *, label: str, source: Path, artifact_root: Path
) -> None:
    for boundary, boundary_label in (
        (source, "source runtime"),
        (artifact_root, "artifact root"),
    ):
        if path.is_relative_to(boundary):
            raise ValueError(f"{label} may not be inside the {boundary_label}")


def stage(
    source_arg: str, output_arg: str, manifest_output_arg: str | None
) -> dict[str, Any]:
    manifest = _static_manifest()
    patches = _patch_entries(manifest)
    targets = _patch_targets(manifest)
    _validate_patch_targets(patches, targets)
    source = _resolve_directory(source_arg, label="source runtime")
    artifact_root = ROOT.resolve(strict=True)
    output = _new_destination(output_arg, label="output")
    _reject_destination_boundary(
        output, label="output runtime", source=source, artifact_root=artifact_root
    )
    output_parent = output.parent
    manifest_output = (
        _new_destination(manifest_output_arg, label="manifest output")
        if manifest_output_arg
        else output.parent / f"{output.name}.native-boundary-manifest.json"
    )
    if not manifest_output_arg:
        manifest_output = _new_destination(manifest_output, label="manifest output")
    _reject_destination_boundary(
        manifest_output,
        label="manifest output",
        source=source,
        artifact_root=artifact_root,
    )
    if manifest_output.is_relative_to(output):
        raise ValueError("output manifest must be outside the staged runtime")
    for _entry in _walk_without_symlinks(source):
        pass
    observed_source = _check_pins(source, manifest)
    source_tree = _tree_files(source, allow_excluded=True)
    expected_patched = _expected_patched_tree(source, patches, targets)
    expected_tree = _expected_tree(source_tree, expected_patched)

    output_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output_parent)
    )
    try:
        shutil.copytree(
            source, staging, symlinks=True, dirs_exist_ok=True, ignore=_copy_ignore
        )
        _apply_patches(staging, patches, targets)
        _verify_tree(staging, targets, expected=expected_tree)
        _reject_forbidden_import_entries(staging)
        staging.rename(output)
        try:
            imports = _import_probe(output)
        except Exception:
            shutil.rmtree(output, ignore_errors=True)
            raise
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    output_manifest: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA,
        "artifact_version": ARTIFACT_VERSION,
        "artifact_root": str(ROOT.resolve()),
        "source_runtime": str(source),
        "source_files": observed_source,
        "source_tree": source_tree,
        "patches": {
            relative: digest
            for _path, relative in patches
            for digest in [_sha256(ROOT / relative)]
        },
        "patched_paths": expected_patched,
        "staged_tree": expected_tree,
        "output_runtime": str(output.resolve()),
        "import_probe": imports,
        "excluded_copy_entries": [
            ".git",
            ".hg",
            ".svn",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".tox",
            ".venv",
            "node_modules",
            "dist",
            "build",
            "*.pyc",
            "*.pyo",
        ],
        "verification": _verification_metadata(),
    }
    try:
        _write_json(manifest_output, output_manifest)
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    print(
        json.dumps(
            {"output_runtime": str(output), "manifest": str(manifest_output)},
            sort_keys=True,
        )
    )
    return output_manifest


def verify(manifest_arg: str, source_arg: str | None = None) -> dict[str, Any]:
    output_manifest_path = Path(manifest_arg).expanduser()
    _reject_symlink_components(output_manifest_path)
    if output_manifest_path.is_symlink() or not output_manifest_path.is_file():
        raise ValueError(
            f"output manifest is missing or symlinked: {output_manifest_path}"
        )
    output_manifest = _load_json(output_manifest_path)
    if output_manifest.get("schema") != ARTIFACT_SCHEMA:
        raise ValueError("output manifest schema mismatch")
    if output_manifest.get("artifact_version") != ARTIFACT_VERSION:
        raise ValueError("output manifest artifact version mismatch")
    static = _static_manifest()
    patches = _patch_entries(static)
    targets = _patch_targets(static)
    _validate_patch_targets(patches, targets)
    expected_source_pins = {
        _safe_relative(entry["path"]): str(entry["sha256"])
        for entry in static["source_files"]
    }
    if output_manifest.get("source_files") != expected_source_pins:
        raise ValueError("output manifest source pins do not match the artifact")
    if output_manifest.get("artifact_root") != str(ROOT.resolve()):
        raise ValueError("output manifest artifact root is not this artifact")
    expected_patch_pins = {relative: _sha256(path) for path, relative in patches}
    if output_manifest.get("patches") != expected_patch_pins:
        raise ValueError("output manifest patch pins do not match the artifact")
    if output_manifest.get("excluded_copy_entries") != [
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "node_modules",
        "dist",
        "build",
        "*.pyc",
        "*.pyo",
    ]:
        raise ValueError("output manifest copy policy is not authenticated")
    if output_manifest.get("verification") != _verification_metadata():
        raise ValueError("output manifest verification metadata is not authenticated")
    if not source_arg:
        raise ValueError("explicit source runtime is required for verification")
    source = _resolve_directory(source_arg, label="source runtime")
    if output_manifest.get("source_runtime") != str(source):
        raise ValueError("output manifest source runtime does not match --source")
    _check_pins(source, static)
    source_tree = _tree_files(source, allow_excluded=True)
    if output_manifest.get("source_tree") != source_tree:
        raise ValueError("output manifest source tree does not match --source")
    expected_patched = _expected_patched_tree(source, patches, targets)
    expected_tree = _expected_tree(source_tree, expected_patched)
    if output_manifest.get("patched_paths") != expected_patched:
        raise ValueError("output manifest patched output is not authenticated")
    if output_manifest.get("staged_tree") != expected_tree:
        raise ValueError("output manifest staged tree is not authenticated")
    runtime = _resolve_directory(
        str(output_manifest.get("output_runtime", "")), label="output runtime"
    )
    if output_manifest.get("output_runtime") != str(runtime):
        raise ValueError("output manifest output runtime is not canonical")
    _verify_tree(runtime, targets, expected=expected_tree)
    imports = _import_probe(runtime)
    if imports != output_manifest.get("import_probe"):
        raise ValueError("staged import provenance changed since staging")
    result = {
        "verified": True,
        "output_runtime": str(runtime),
        "manifest": str(output_manifest_path),
    }
    print(json.dumps(result, sort_keys=True))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage_parser = subparsers.add_parser(
        "stage", help="copy, patch, and verify a runtime"
    )
    stage_parser.add_argument("--source", required=True)
    stage_parser.add_argument("--output", required=True)
    stage_parser.add_argument("--manifest-output")
    verify_parser = subparsers.add_parser(
        "verify", help="verify a staged runtime manifest"
    )
    verify_parser.add_argument("--manifest", required=True)
    verify_parser.add_argument("--source", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "stage":
            stage(args.source, args.output, args.manifest_output)
        else:
            verify(args.manifest, args.source)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        print(f"native-boundary: ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
