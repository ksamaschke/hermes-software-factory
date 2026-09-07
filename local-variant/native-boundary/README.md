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
approved the source and generated manifest, use the existing user-systemd
`PYTHONPATH` drop-in. The procedure below is parameterized for the current
profile-scoped gateway; it does not invent an activation wrapper, edit
`kanban.db`, or start a worker manually.

```sh
SERVICE=hermes-gateway-orchestrator.service
DROPIN=/home/ksamaschke/.config/systemd/user/hermes-gateway-orchestrator.service.d/10-runtime-hotfix.conf
DROPIN_BACKUP="${DROPIN}.native-boundary.backup"
STAGE=/absolute/path/to/.native-boundary-stage/runtime
MANIFEST=/absolute/path/to/.native-boundary-stage/manifest.json
SOURCE=/absolute/path/to/runtime-hotfix-source
REVIEWED_DROPIN=/absolute/path/to/reviewed-10-runtime-hotfix.conf

# Re-verify the immutable artifact immediately before activation.
python3 -B local-variant/native-boundary/build_native_boundary.py verify \
  --manifest "$MANIFEST" \
  --source "$SOURCE"

# Preserve and then select the reviewed staged runtime through the exact
# existing drop-in. REVIEWED_DROPIN must contain PYTHONPATH=$STAGE and no
# unrelated service changes; inspect both files before proceeding.
install -D -m 0644 "$DROPIN" "$DROPIN_BACKUP"
install -D -m 0644 "$REVIEWED_DROPIN" "$DROPIN"
grep -F "PYTHONPATH=$STAGE" "$DROPIN"
systemctl --user daemon-reload
test "$(systemctl --user show "$SERVICE" -p KillMode --value)" = mixed

# KillMode=mixed requires an empty worker boundary before the restart. Stop
# the profile-scoped service, wait for inactive, and inspect its cgroup; do
# not continue while systemd-cgls shows a live worker below this unit.
systemctl --user stop "$SERVICE"
test "$(systemctl --user is-active "$SERVICE" 2>/dev/null || true)" = inactive
systemd-cgls --user-unit "$SERVICE" --no-pager

# Restart only the supported user-systemd gateway boundary.
systemctl --user restart "$SERVICE"
systemctl --user is-active "$SERVICE"
systemctl --user show "$SERVICE" -p KillMode -p ControlGroup -p DropInPaths

# Read back the active service/lock boundary, full manifest, and import paths;
# all imported modules must point at STAGE.
STAGE="$STAGE" MANIFEST="$MANIFEST" PYTHONPATH="$STAGE" python3 -B -c 'import json, os; from pathlib import Path; from hermes_cli import kanban_db, native_boundary; stage=Path(os.environ["STAGE"]).resolve(); manifest_path=Path(os.environ["MANIFEST"]); manifest=json.loads(manifest_path.read_text()); print(manifest_path.read_text()); print(manifest["schema"], Path(kanban_db.__file__).resolve(), Path(native_boundary.__file__).resolve()); assert Path(kanban_db.__file__).resolve().is_relative_to(stage); assert Path(native_boundary.__file__).resolve().is_relative_to(stage)' \
  </dev/null
```

The one-liner passes `STAGE` and `MANIFEST` explicitly. Once active, the existing B task
resumes through its ordinary native dispatch tick; no direct worker launch or
manual live-DB mutation is valid evidence.

## Rollback

If the post-activation health check or unchanged native contracts fail, restore
the exact backed-up drop-in through the same profile-scoped user-systemd
boundary. Keep the service stopped until the cgroup has no live workers:

```sh
SERVICE=hermes-gateway-orchestrator.service
DROPIN=/home/ksamaschke/.config/systemd/user/hermes-gateway-orchestrator.service.d/10-runtime-hotfix.conf
DROPIN_BACKUP="${DROPIN}.native-boundary.backup"
BACKUP_MANIFEST=/absolute/path/to/prior-runtime/manifest.json
BACKUP_SOURCE=/absolute/path/to/prior-runtime-source

systemctl --user stop "$SERVICE"
test "$(systemctl --user is-active "$SERVICE" 2>/dev/null || true)" = inactive
systemd-cgls --user-unit "$SERVICE" --no-pager
install -D -m 0644 "$DROPIN_BACKUP" "$DROPIN"
systemctl --user daemon-reload
systemctl --user restart "$SERVICE"
systemctl --user is-active "$SERVICE"
systemctl --user show "$SERVICE" -p KillMode -p ControlGroup -p DropInPaths
python3 -B local-variant/native-boundary/build_native_boundary.py verify \
  --manifest "$BACKUP_MANIFEST" \
  --source "$BACKUP_SOURCE"
```

Read back the restored import paths and service state before declaring
rollback complete. Do not delete `DROPIN_BACKUP` until the next reviewed
activation is accepted.
