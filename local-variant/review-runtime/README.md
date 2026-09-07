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

Inputs and commands:

```text
python3 local-variant/review-runtime/build_review_runtime.py stage \
  --native-runtime /path/to/native-boundary-stage/runtime8 \
  --native-manifest /path/to/native-boundary-stage/manifest8.json \
  --output /tmp/review-runtime/runtime8 \
  --manifest-output /tmp/review-runtime/runtime8.manifest.json

python3 local-variant/review-runtime/build_review_runtime.py verify \
  --runtime /tmp/review-runtime/runtime8 \
  --manifest /tmp/review-runtime/runtime8.manifest.json
```

`manifest.json` pins the patch hash, target allowlist, native-boundary schema,
and review policy. The generated manifest records the native input manifest
hash, patched output hashes, syntax probe, and complete runtime tree hash.
The native-boundary stage is a prerequisite dependency; do not edit it in this
worktree.
