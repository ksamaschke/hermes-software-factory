#!/usr/bin/env python3
"""Build and verify the review-runtime layer over a native-boundary stage.

The native-boundary artifact is an input, not a file-edit target.  This layer
copies that exact stage, applies one allowlisted patch, and records enough
hashes to make the resulting runtime reproducible and auditable.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
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
    "hermes_cli/kanban_db.py",
    "gateway/kanban_watchers.py",
    "tui_gateway/server.py",
    "tools/terminal_tool.py",
    "hermes_cli/kanban.py",
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
    if (
        manifest.get("native_manifest_sha256")
        != "c15a9c10e499c5cbda6cbbe523162502b8875720f639a42e4a0a48b2ef2c4a01"
    ):
        raise SystemExit(
            "review manifest is not pinned to the reviewed native manifest"
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
    targets: list[str] = []
    for line in patch_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("--- a/"):
            target = line[6:].split("\t", 1)[0]
            targets.append(target)
    return tuple(targets)


def _validate_native_input(
    native_runtime: Path,
    native_manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    expected_exclusions: list[str] | None = None,
    expected_artifact_version: str | None = None,
) -> dict[str, Any]:
    """Authenticate the complete native boundary before any copy occurs."""
    manifest_info = native_manifest_path.lstat()
    if not stat.S_ISREG(manifest_info.st_mode) or manifest_info.st_nlink != 1:
        raise SystemExit("native manifest must be a regular, non-hard-linked file")
    if (
        expected_manifest_sha256
        and _sha256(native_manifest_path) != expected_manifest_sha256
    ):
        raise SystemExit("native manifest hash does not match the reviewed boundary")
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
    if set(native_hashes) != set(output_hashes):
        raise SystemExit("review overlay changed the native runtime file set")
    changed = {
        relative
        for relative in native_hashes
        if native_hashes[relative] != output_hashes[relative]
    }
    if changed - set(TARGET_PATHS):
        raise SystemExit(
            "review overlay changed files outside the target allowlist: "
            + ", ".join(sorted(changed - set(TARGET_PATHS)))
        )
    if not set(TARGET_PATHS).issubset(output_hashes):
        raise SystemExit("review overlay target file is missing from the output")


def _apply_patch(output: Path, patch_path: Path) -> None:
    targets = _patch_targets(patch_path)
    if targets != TARGET_PATHS:
        raise SystemExit(
            f"patch target set {targets!r} is outside the review allowlist"
        )
    command = ["git", "apply", "--whitespace=nowarn", str(patch_path)]
    check = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn", str(patch_path)],
        cwd=output,
        check=False,
        capture_output=True,
        text=True,
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
        Path(explicit).resolve()
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
        expected_manifest_sha256=static["native_manifest_sha256"],
        expected_exclusions=patterns,
        expected_artifact_version=static["native_artifact_version"],
    )
    output = output.resolve(strict=False)
    manifest_output = manifest_output.resolve(strict=False)
    source_runtime = (
        Path(native_manifest["source_runtime"]).expanduser().resolve(strict=False)
    )
    for protected in (native_runtime, source_runtime, ARTIFACT_ROOT):
        if _path_overlaps(output, protected):
            raise SystemExit(
                f"review runtime output collides with protected input/artifact path: {output}"
            )
    if _path_overlaps(manifest_output, output):
        raise SystemExit(
            "generated manifest must not be inside the runtime output tree"
        )
    if output.exists():
        if not force:
            raise SystemExit(
                f"output already exists; use --force to replace it: {output}"
            )
        if output.is_symlink() or not output.is_dir():
            raise SystemExit(
                "existing output must be a real directory when --force is used"
            )
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    _copy_runtime(native_runtime, output, patterns)
    _apply_patch(output, ARTIFACT_ROOT / PATCH_RELATIVE)
    _assert_overlay_equivalence(native_runtime, output, patterns)
    probed = _syntax_probe(output)
    generated = {
        "schema": STATIC_SCHEMA,
        "artifact_version": static["artifact_version"],
        "native_boundary_schema": NATIVE_SCHEMA,
        "native_manifest": str(native_manifest_path),
        "native_manifest_sha256": _sha256(native_manifest_path),
        "native_source_runtime": native_manifest.get("source_runtime"),
        "native_source_tree_sha256": _tree_digest(source_runtime, patterns),
        "native_staged_tree_sha256": _tree_digest(native_runtime, patterns),
        "native_verification": native_manifest["verification"],
        "output_runtime": str(output),
        "patches": {PATCH_RELATIVE: _sha256(ARTIFACT_ROOT / PATCH_RELATIVE)},
        "patched_paths": {
            relative: _sha256(output / relative) for relative in TARGET_PATHS
        },
        "policy": static["policy"],
        "syntax_probe": probed,
        "runtime_tree_sha256": _tree_digest(output, patterns),
    }
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_text(
        json.dumps(generated, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_output


def verify(output: Path, manifest_path: Path) -> None:
    static = _static_manifest()
    manifest = _load_json(manifest_path)
    if manifest.get("schema") != STATIC_SCHEMA:
        raise SystemExit("generated manifest has an unexpected schema")
    if manifest.get("native_manifest_sha256") != static["native_manifest_sha256"]:
        raise SystemExit(
            "generated manifest is not bound to the reviewed native manifest"
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
        expected_manifest_sha256=static["native_manifest_sha256"],
        expected_exclusions=patterns,
        expected_artifact_version=static["native_artifact_version"],
    )
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
        manifest = build(
            args.native_runtime.resolve(),
            args.native_manifest.resolve(),
            args.output.resolve(),
            _manifest_output(
                args.output.resolve(),
                str(args.manifest_output) if args.manifest_output else None,
            ),
            args.force,
        )
        print(
            json.dumps(
                {
                    "status": "staged",
                    "runtime": str(args.output.resolve()),
                    "manifest": str(manifest),
                }
            )
        )
    else:
        verify(args.runtime.resolve(), args.manifest.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
