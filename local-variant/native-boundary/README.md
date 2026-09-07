# Factory native-boundary compatibility artifact

This is the versioned `factory.native-boundary.v1` prerequisite for the
Factory runtime. It repairs two native SQLite boundaries in a disposable copy
of the pinned Hermes runtime:

- repeated-blocker triage admission is rejected inside the existing native
  write transaction before any task fields are changed;
- a same-owner continuation is admitted only when the task has a durable,
  post-run lifecycle requeue and the current assignee still matches the
  prior native run profile. Comments and PR URLs are evidence only.

The artifact is fail-closed. It pins both input source files and the patch,
rejects symlinks and path escapes, excludes VCS/cache material from the copy,
and refuses partial patch output. It never installs, restarts, launches a
worker, or edits a live board.

## Build and verify a disposable runtime

Run these commands from the repository root after reviewing the pinned source
runtime. The output directory must be new and must be on a filesystem with
sufficient free space; do not stage under `/tmp` for the full runtime.

```sh
SOURCE=/home/ksamaschke/.hermes/profiles/orchestrator/runtime-hotfix-20260906
STAGE="$PWD/.native-boundary-stage/runtime"
MANIFEST="$PWD/.native-boundary-stage/manifest.json"

python3 -B local-variant/native-boundary/build_native_boundary.py stage \
  --source "$SOURCE" \
  --output "$STAGE" \
  --manifest-output "$MANIFEST"

python3 -B local-variant/native-boundary/build_native_boundary.py verify \
  --manifest "$MANIFEST" \
  --source "$SOURCE"
```

The builder validates the source hashes before copying, applies the pinned
unified patch with zero fuzz, checks every staged target, imports the staged
modules in a fresh subprocess, and records import provenance in the generated
manifest. A failed check is an unsuccessful build; do not activate a partial
output.

## Native contract verification

The owner contracts are kept outside this repository and must remain
unchanged. Run them against the exact staged runtime, never against the live
runtime or a monkeypatched import:

```sh
OWNER_TEST_DIR=/home/ksamaschke/.hermes/profiles/esg/reports/factory-audit-20260907
FACTORY_NATIVE_RUNTIME="$STAGE" \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
/opt/hermes-agent/venv/bin/python -B -m pytest \
  "$OWNER_TEST_DIR/test_native_specify_task_guard.py" \
  "$OWNER_TEST_DIR/test_native_same_writer_readmission.py" \
  "$OWNER_TEST_DIR/test_native_boundary_supplemental.py" \
  -q -s -p no:cacheprovider -o addopts=''
```

The repository artifact tests cover manifest pins, tamper detection, path and
symlink rejection, and the native decision helpers. When
`FACTORY_NATIVE_RUNTIME` is set, they additionally exercise staged import
provenance and the public native entry points in an isolated temporary DB.

## Controlled activation (separate reviewed host step)

Activation is intentionally not part of implementation. After the artifact
has passed exact-head functional/security review and the host operator has
approved the source and generated manifest, perform the following as a
separate change-controlled step. Substitute the host's documented runtime
slot and service unit; never edit `kanban.db` or start a worker manually.

```sh
# Re-verify the immutable artifact immediately before activation.
python3 -B local-variant/native-boundary/build_native_boundary.py verify \
  --manifest "$MANIFEST" \
  --source "$SOURCE"

# The host's activation wrapper must atomically select the verified staged
# runtime, retain the prior slot as BACKUP, and record MANIFEST as the active
# artifact. Use the host-approved wrapper rather than ad-hoc cp/symlink edits.
hermes-factory-runtime activate \
  --runtime "$STAGE" \
  --manifest "$MANIFEST" \
  --backup "$BACKUP" \
  --service "$FACTORY_GATEWAY_SERVICE"
```

The activation wrapper is a host control-plane command, not supplied or run by
this source artifact. It must verify the manifest again, stop/restart only the
approved gateway service, wait for the service health check, and leave the
normal dispatcher enabled. Once active, the existing B task resumes through
its ordinary native dispatch tick; no direct worker launch or manual live-DB
mutation is valid evidence.

## Rollback

If the post-activation health check or the unchanged native contracts fail,
use the same reviewed host wrapper to restore the recorded prior runtime and
restart only the approved gateway service. Verify the restored manifest and
health before declaring rollback complete:

```sh
hermes-factory-runtime rollback \
  --backup "$BACKUP" \
  --manifest "$BACKUP_MANIFEST" \
  --service "$FACTORY_GATEWAY_SERVICE"

python3 -B local-variant/native-boundary/build_native_boundary.py verify \
  --manifest "$BACKUP_MANIFEST"
```

Do not delete the backup until the next reviewed activation is accepted.
