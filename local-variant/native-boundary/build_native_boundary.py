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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(raw: str) -> str:
    value = str(raw).replace("\\", "/")
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not value
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise ValueError(f"unsafe artifact path: {raw!r}")
    return path.as_posix()


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
    """Yield every entry and reject symlinked source/output entries."""
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                item = Path(entry.path)
                if entry.is_symlink():
                    raise ValueError(f"symlinked runtime entry is unsafe: {item}")
                yield item
                if entry.is_dir(follow_symlinks=False):
                    stack.append(item)


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


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    excluded = {
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
    return {
        name for name in names if name in excluded or name.endswith((".pyc", ".pyo"))
    }


def _apply_patches(staging: Path, patches: list[tuple[Path, str]]) -> None:
    for patch_path, _relative in patches:
        command = [
            "patch",
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
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stdout or "").strip()[-2000:]
            raise RuntimeError(f"patch application failed for {patch_path}: {detail}")


def _verify_tree(root: Path, targets: list[str]) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"staged runtime is not a safe directory: {root}")
    _reject_symlink_components(root)
    for item in _walk_without_symlinks(root):
        if item.is_file() and item.name.endswith((".orig", ".rej")):
            raise ValueError(f"partial patch artifact remains: {item}")
    output_files: dict[str, str] = {}
    for relative in targets:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"patched output is missing or unsafe: {path}")
        output_files[relative] = _sha256(path)
    return output_files


def _import_probe(runtime: Path) -> dict[str, str]:
    probe = (
        "import json\n"
        "from pathlib import Path\n"
        "from hermes_cli import kanban_db, kanban_specify\n"
        "from hermes_cli import native_boundary\n"
        "print(json.dumps({\n"
        "  'kanban_db': str(Path(kanban_db.__file__).resolve()),\n"
        "  'kanban_specify': str(Path(kanban_specify.__file__).resolve()),\n"
        "  'native_boundary': str(Path(native_boundary.__file__).resolve()),\n"
        "}))\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(runtime)
    environment["PYTHONNOUSERSITE"] = "1"
    for key in tuple(environment):
        if key.startswith("HERMES_"):
            environment.pop(key, None)
    completed = subprocess.run(
        [sys.executable, "-B", "-c", probe],
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
    root = runtime.resolve()
    for name in ("kanban_db", "kanban_specify", "native_boundary"):
        imported = Path(str(paths.get(name, ""))).resolve()
        if not imported.is_relative_to(root):
            raise RuntimeError(f"{name} imported outside staged runtime: {imported}")
    return {str(key): str(value) for key, value in paths.items()}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _reject_symlink_components(path.parent)
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def stage(
    source_arg: str, output_arg: str, manifest_output_arg: str | None
) -> dict[str, Any]:
    manifest = _static_manifest()
    patches = _patch_entries(manifest)
    targets = _patch_targets(manifest)
    source = _resolve_directory(source_arg, label="source runtime")
    output = Path(output_arg).expanduser()
    _reject_symlink_components(output)
    if output.exists() or output.is_symlink():
        raise ValueError(f"output must be a new non-symlink path: {output}")
    output_parent = output.parent.resolve(strict=False)
    if output.resolve(strict=False).is_relative_to(source):
        raise ValueError("output runtime may not be inside the source runtime")
    manifest_output = (
        Path(manifest_output_arg).expanduser()
        if manifest_output_arg
        else output.parent / f"{output.name}.native-boundary-manifest.json"
    )
    _reject_symlink_components(manifest_output)
    if manifest_output.exists() or manifest_output.is_symlink():
        raise ValueError(
            f"manifest output must be a new non-symlink path: {manifest_output}"
        )
    if manifest_output.resolve(strict=False).is_relative_to(output):
        raise ValueError("output manifest must be outside the staged runtime")
    for _entry in _walk_without_symlinks(source):
        pass
    observed_source = _check_pins(source, manifest)

    output_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output_parent)
    )
    try:
        # symlinks=True preserves the safety check's fail-closed behavior: a
        # source link is rejected rather than silently followed into user data.
        shutil.copytree(
            source, staging, symlinks=True, dirs_exist_ok=True, ignore=_copy_ignore
        )
        for _entry in _walk_without_symlinks(staging):
            pass
        _apply_patches(staging, patches)
        output_files = _verify_tree(staging, targets)
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
        "artifact_root": str(ROOT),
        "source_runtime": str(source),
        "source_files": observed_source,
        "patches": {
            relative: digest
            for _path, relative in patches
            for digest in [_sha256(ROOT / relative)]
        },
        "patched_paths": output_files,
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
        "verification": {"patches_applied": True, "symlinks_rejected": True},
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
    expected_source_pins = {
        _safe_relative(entry["path"]): str(entry["sha256"])
        for entry in static["source_files"]
    }
    if output_manifest.get("source_files") != expected_source_pins:
        raise ValueError("output manifest source pins do not match the artifact")
    if output_manifest.get("patches") != {
        relative: _sha256(path) for path, relative in patches
    }:
        raise ValueError("output manifest patch pins do not match the artifact")
    runtime = _resolve_directory(
        str(output_manifest.get("output_runtime", "")), label="output runtime"
    )
    targets = _patch_targets(static)
    observed = _verify_tree(runtime, targets)
    expected = output_manifest.get("patched_paths")
    if expected != observed:
        raise ValueError("staged output hashes do not match its manifest")
    source = source_arg or output_manifest.get("source_runtime")
    if source:
        source_path = _resolve_directory(str(source), label="source runtime")
        _check_pins(source_path, static)
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
    verify_parser.add_argument("--source")
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
