# Review-runtime overlay

This artifact is the second, non-colliding layer after the native-boundary
prerequisite. It never edits the prerequisite worktree or the orchestrator
checkout. `build_review_runtime.py` copies a verified native-boundary runtime,
applies `patches/002-review-runtime.patch` with zero fuzz, and writes a
content-addressed manifest for the resulting runtime.

The overlay has three responsibilities:

- Resolve every ordinary reviewer claim to the 1,200-second hard worker cap,
  600-second evidence budget, and 120-second per-command timeout at claim time.
  The persisted task row is not rewritten; the active `task_runs` row and the
  worker-facing task object carry the effective cap.
- Keep evidence-only recovery distinct at the actual dispatch boundary: its
  hard worker cap is 600 seconds, its evidence budget is 300 seconds, and its
  per-command timeout is 60 seconds. Unrelated implementation tasks retain
  their requested runtime budget.
- Measure timeout enforcement from the active run cap and account timeout
  failures through the same breaker as other worker failures. Durable timeout
  and gave-up payloads use the final status, and event order remains
  `timed_out` followed by `gave_up`. Passive and wake notifications say that a
  retryable timeout is queued/eligible, while exhausted timeouts say
  blocked/no retry. Legacy `gave_up` rows with a NULL run id are associated
  with the nearest preceding identified timeout; a later identified timeout
  starts a new notification boundary.
- Persist one absolute evidence deadline on the claimed `task_runs` row. A
  reconnect or event-compaction gap rehydrates that same value; missing or
  malformed, non-finite, inconsistent, or expired deadline state produces a
  typed `REVIEW-INCOMPLETE` block instead of silently minting more time.
- Provide the symmetric recovery transition `kanban_supersede`: an
  orchestrator may replace one blocked/triaged card only when its exact latest
  run is a terminal failure. One immutable lane key has one successor leaf,
  recovery forks are rejected, and downstream dependencies follow that leaf
  until it reaches a terminal state. The failed predecessor remains archived
  audit history rather than an eternal dependency gate.

Inputs and commands:

```text
python3 local-variant/review-runtime/build_review_runtime.py stage \
  --native-runtime /path/to/native-boundary-stage/runtime8 \
  --native-manifest /path/to/native-boundary-stage/manifest8.json \
  --output /path/to/durable/review-runtime/runtime8 \
  --manifest-output /path/to/durable/review-runtime/runtime8.manifest.json

python3 local-variant/review-runtime/build_review_runtime.py verify \
  --runtime /path/to/durable/review-runtime/runtime8 \
  --manifest /path/to/durable/review-runtime/runtime8.manifest.json
```

`manifest.json` pins the patch hash, target allowlist, native-boundary schema,
review policy, and a path-independent native artifact identity. The identity
covers every content-bearing native-manifest field while allowing the same
reviewed tree to be rebuilt at a different absolute output path. The generated
manifest still records the exact native input-manifest hash, patched output
hashes, syntax probe, and complete runtime tree hash.

The manifest destination must be new, outside every source, artifact, and
staged-runtime tree, and contain no symlink in any existing path component.
Publication traverses and pins the parent with directory descriptors plus
`O_NOFOLLOW`, then performs an exclusive atomic create relative to that pinned
directory. `--force` builds and verifies in a private sibling before swapping;
publication failure restores the prior runtime and never overwrites evidence.

The builder invokes only root-owned `/usr/bin/git` with a scrubbed environment.
The staged worker launcher uses the active reviewed Python interpreter rather
than an executable selected from ambient `PATH`; ambient configuration roots
such as `XDG_CONFIG_HOME` are not inherited by worker children.

The native-boundary stage is a prerequisite dependency; do not edit it in this
worktree.
