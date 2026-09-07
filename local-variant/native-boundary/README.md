# Factory native-boundary compatibility artifact

This is the versioned `factory.native-boundary.v1` prerequisite for the
Factory runtime. It repairs two native SQLite boundaries in a disposable copy
of the pinned Hermes runtime:

- repeated-blocker triage admission is rejected inside the existing native
  write transaction before any task fields are changed;
- a same-owner continuation is admitted only when the task has durable,
  post-run lifecycle requeue evidence and the current assignee still matches
  the prior native run profile. Comments and PR URLs are evidence only.

The artifact is fail-closed. It pins both input source files and the patch,
rejects symlinks, hardlinks, special files, path escapes, patch mode/rename
metadata, and partial patch output, and authenticates the complete regular-file
copy tree. It never installs, restarts, launches a worker, or edits a live
board.

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

The builder validates the source pins before copying, records a SHA-256 entry
for every copied regular source file, parses the unified patch against the
exact target allowlist, applies it with zero fuzz, and records the complete
staged regular-file tree. Verification independently re-hashes the explicit
source, applies the pinned patch to a disposable target-only copy to derive the
expected patched hashes, and compares the entire staged tree. A mutable output
manifest cannot turn an unlisted file mutation, metadata change, or patched
file replacement into `verified=true`.

The import probe is also fail-closed. It rejects `sitecustomize.py`,
`usercustomize.py`, and `.pth` import-hook files, runs Python with `-B -S`,
uses only a minimal explicit environment, adds only the interpreter's known
virtualenv site-packages path for required dependencies, and requires
`kanban_db`, `kanban_specify`, and `native_boundary` to resolve to their exact
files under `STAGE`. Hosted package checks without `FACTORY_NATIVE_RUNTIME`
are package evidence only; they are not native activation proof.

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

The repository artifact tests cover manifest pins, complete-tree tamper
detection, patch-target/path safety, hardlink/symlink rejection, isolated
import provenance, and the native decision helpers. When
`FACTORY_NATIVE_RUNTIME` is set, they additionally exercise staged import
provenance, old-runtime-to-candidate migration, the real PR-URL guard seam,
and one-admission concurrency in private temporary databases.

## Controlled activation boundary

Activation is not part of implementation or hosted validation. It is allowed
only after two independent exact-head reviews approve the same PR head, base,
complete manifest, and generated native diff, and after an operator has
recorded a deterministic rollback contract. Do not use an activation wrapper
that is not supplied by this repository.

The current user-systemd unit uses `KillMode=mixed`, so a plain
`systemctl --user stop` is not a safe admission boundary: it can terminate
children before an operator knows whether a writer is still active. A safe
host procedure must first freeze the dispatcher main process, let existing
children drain, and prove that the service cgroup contains only that stopped
main process. If the cgroup cannot be read, the main PID changes, a child
remains, or the drain times out, abort and resume the main process. Never
install the reviewed drop-in or restart the service after a failed gate.

The following is the required shape of that host-only procedure. It is
intentionally parameterized and does not execute here:

```bash
#!/usr/bin/env bash
set -euo pipefail

SERVICE=hermes-gateway-orchestrator.service
DROPIN=/home/ksamaschke/.config/systemd/user/hermes-gateway-orchestrator.service.d/10-runtime-hotfix.conf
STAGE=/absolute/path/to/.native-boundary-stage/runtime
MANIFEST=/absolute/path/to/.native-boundary-stage/manifest.json
SOURCE=/absolute/path/to/runtime-hotfix-source
REVIEWED_DROPIN=/absolute/path/to/reviewed-10-runtime-hotfix.conf

python3 -B local-variant/native-boundary/build_native_boundary.py verify \
  --manifest "$MANIFEST" --source "$SOURCE"
test -r "$REVIEWED_DROPIN"
grep -F "PYTHONPATH=$STAGE" "$REVIEWED_DROPIN"

# Freeze admission before inspecting or stopping a KillMode=mixed service.
MAIN_PID=$(systemctl --user show "$SERVICE" -p MainPID --value)
test "$MAIN_PID" -gt 0
test "$(systemctl --user show "$SERVICE" -p KillMode --value)" = mixed
CONTROL_GROUP=$(systemctl --user show "$SERVICE" -p ControlGroup --value)
test -n "$CONTROL_GROUP"
CGROUP_ROOT="/sys/fs/cgroup$CONTROL_GROUP"
test -d "$CGROUP_ROOT"
resume_main() {
  systemctl --user kill --kill-who=main --signal=SIGCONT "$SERVICE" 2>/dev/null || true
}
trap resume_main EXIT
systemctl --user kill --kill-who=main --signal=SIGSTOP "$SERVICE"
test "$(systemctl --user show "$SERVICE" -p MainPID --value)" = "$MAIN_PID"

# Existing workers may drain while the dispatcher is stopped. No new worker
# can be admitted. Fail closed after the bounded drain window.
deadline=$((SECONDS + 90))
while :; do
  EXTRA_PIDS=$(find "$CGROUP_ROOT" -type f -name cgroup.procs -print0 \
    | xargs -0r cat | sort -nu | grep -vx "$MAIN_PID" || true)
  test -z "$EXTRA_PIDS" && break
  test "$SECONDS" -lt "$deadline"
  sleep 1
done

# Create a unique backup without overwriting an earlier rollback generation.
BACKUP="$DROPIN.native-boundary.backup.$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_SHA256="$BACKUP.sha256"
test ! -e "$BACKUP" -a ! -e "$BACKUP_SHA256"
( umask 077; set -C; cat "$DROPIN" > "$BACKUP" )
( umask 077; set -C; sha256sum "$BACKUP" > "$BACKUP_SHA256" )
chmod 0444 "$BACKUP" "$BACKUP_SHA256"

# Install only after the stopped-main/zero-child gate. The service remains
# stopped until a separately authenticated MainPID import readback exists.
install -D -m 0644 "$REVIEWED_DROPIN" "$DROPIN"
systemctl --user daemon-reload
systemctl --user kill --kill-who=main --signal=SIGTERM "$SERVICE"
systemctl --user kill --kill-who=main --signal=SIGCONT "$SERVICE"
systemctl --user stop "$SERVICE"
test "$(systemctl --user is-active "$SERVICE" 2>/dev/null || true)" = inactive
```

This repository does not currently expose an authenticated diagnostic that
can inspect `sys.modules` inside the launched gateway MainPID. Reading
`PYTHONPATH` from `/proc/$MAIN_PID/environ`, or importing the modules in a new
shell, is not equivalent proof. Therefore the procedure above must stop before
restart unless the host supplies an independently reviewed service-side
MainPID-scoped provenance diagnostic that prints and authenticates every
reviewed module path against `STAGE` and the manifest. Do not claim activation
from a green package check, a new-shell import, or a matching drop-in alone.

## Rollback contract

Before any activation, create one read-only legacy provenance record for the
currently active drop-in/runtime if no prior v1 manifest exists. The record
must contain the current drop-in SHA-256, the exact runtime path, and a full
regular-file tree digest produced by a reviewed read-only procedure. Keep it
under a unique name; never overwrite it. A legacy runtime without that record
is not a rollback target and activation must stop.

The following defines the legacy record format and digest operation. It rejects
symlinks and hardlinks, sorts relative names deterministically, and writes the
record with `noclobber`; run it once before activation with the real current
paths:

```bash
set -euo pipefail
DROPIN=/home/ksamaschke/.config/systemd/user/hermes-gateway-orchestrator.service.d/10-runtime-hotfix.conf
PRIOR_RUNTIME=/home/ksamaschke/.hermes/profiles/orchestrator/runtime-hotfix-20260906
PRIOR_PROVENANCE=/absolute/path/to/unique/prior-runtime-provenance.txt

tree_digest() {
  local root="$1" relative
  test -d "$root"
  test -z "$(find "$root" -type l -print -quit)"
  test -z "$(find "$root" -type f -links +1 -print -quit)"
  find "$root" -type f -printf '%P\0' | sort -z |
    while IFS= read -r -d '' relative; do
      printf '%s  %s\n' \
        "$(sha256sum "$root/$relative" | awk '{print $1}')" "$relative"
    done | sha256sum | awk '{print $1}'
}

test ! -e "$PRIOR_PROVENANCE"
PRIOR_DROPIN_SHA256=$(sha256sum "$DROPIN" | awk '{print $1}')
PRIOR_TREE_SHA256=$(tree_digest "$PRIOR_RUNTIME")
(
  umask 077
  set -C
  printf 'schema=legacy-native-provenance.v1\ndropin=%s\nruntime=%s\ntree=%s\n' \
    "$PRIOR_DROPIN_SHA256" "$PRIOR_RUNTIME" "$PRIOR_TREE_SHA256" \
    > "$PRIOR_PROVENANCE"
)
chmod 0444 "$PRIOR_PROVENANCE"
```

For every reviewed activation, retain the unique backup and its hash. A later
activation creates another unique backup; it never reuses or overwrites an old
one. Before rollback, authenticate the backup hash and the legacy provenance
record, then repeat the same stopped-main/zero-child gate above. Restore only
through the profile-scoped user-systemd boundary:

```bash
set -euo pipefail
SERVICE=hermes-gateway-orchestrator.service
DROPIN=/home/ksamaschke/.config/systemd/user/hermes-gateway-orchestrator.service.d/10-runtime-hotfix.conf
BACKUP=/absolute/path/to/unique/10-runtime-hotfix.conf.native-boundary.backup.UTC
BACKUP_SHA256=/absolute/path/to/unique/backup.sha256
PRIOR_PROVENANCE=/absolute/path/to/unique/prior-runtime-provenance.txt

# Read and authenticate the legacy record against the backup and the
# still-available runtime before proceeding.
test -r "$BACKUP" -a -r "$BACKUP_SHA256" -a -r "$PRIOR_PROVENANCE"
sha256sum --check "$BACKUP_SHA256"
record_value() {
  awk -F= -v key="$1" '$1 == key {print substr($0, index($0, "=") + 1); exit}' "$PRIOR_PROVENANCE"
}
test "$(record_value schema)" = legacy-native-provenance.v1
test "$(record_value dropin)" = "$(sha256sum "$BACKUP" | awk '{print $1}')"
PRIOR_RUNTIME=$(record_value runtime)
PRIOR_TREE=$(record_value tree)
test -n "$PRIOR_RUNTIME" -a -n "$PRIOR_TREE"

tree_digest() {
  local root="$1" relative
  test -d "$root"
  test -z "$(find "$root" -type l -print -quit)"
  test -z "$(find "$root" -type f -links +1 -print -quit)"
  find "$root" -type f -printf '%P\0' | sort -z |
    while IFS= read -r -d '' relative; do
      printf '%s  %s\n' \
        "$(sha256sum "$root/$relative" | awk '{print $1}')" "$relative"
    done | sha256sum | awk '{print $1}'
}
test "$(tree_digest "$PRIOR_RUNTIME")" = "$PRIOR_TREE"

# Repeat the freeze, bounded drain, and zero-child cgroup gate from above.
# Then restore; install never overwrites BACKUP.
install -D -m 0644 "$BACKUP" "$DROPIN"
systemctl --user daemon-reload
systemctl --user restart "$SERVICE"
systemctl --user is-active "$SERVICE"
```

Rollback is complete only after the same service-side MainPID provenance
readback is available, the restored drop-in hash matches the authenticated
backup, and the legacy runtime/tree digest matches `PRIOR_PROVENANCE`. Keep
all backup generations and records until the next reviewed activation has
passed its independent exact-head and host gates.
