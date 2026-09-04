# Kanban Factory Runtime and Routing Contract

This document defines reusable runtime mechanics for a Hermes Kanban software
factory. It is not a complete project configuration and does not select a
repository, tracker host, model, profile name, deployment controller, or
notification room.

Project-specific values belong in a project policy file or external add-on.
When several factories share a host but need different tools, permissions,
memory, or routing, create custom profiles and map them in those overlays. Keep
the lifecycle and safety invariants below unchanged.

## Required runtime settings

A project policy should declare values equivalent to these, tuned from live
capacity rather than copied blindly:

```yaml
kanban:
  dispatch_in_gateway: true
  review_dispatch: true
  dispatch_interval_seconds: 60
  failure_limit: 2
  dispatch_stale_timeout_seconds: 14400
  worker_max_turns: 250
  max_in_progress: 8
  max_in_progress_per_profile: 2
  auto_decompose: true
  auto_decompose_per_tick: 3
  auto_subscribe_on_create: false
```

One supervised gateway owns dispatcher promotion, claims, worktrees,
heartbeats, retries, and recovery. A source reconciler or progress digest does
not start another dispatcher.

### Worker/process reconciliation

Dispatcher workers are started in an isolated POSIX session and carry the
following non-secret identity values in their environment:

- `HERMES_KANBAN_TASK`;
- `HERMES_KANBAN_RUN_ID`;
- `HERMES_KANBAN_BOARD` and/or `HERMES_KANBAN_DB`.

The factory recovery add-on may reconcile a worker only after an exact task
readback shows a terminal handoff (`blocked`, `review`, `done`, `failed`,
`cancelled`, or `archived`) and `current_run_id=null`. It must revalidate the
task and process start-time before signalling, require an isolated process
group whose members all carry the same task/board identity, use bounded
`SIGTERM`/`SIGKILL`, and read the process state back. Active tasks, board
mismatches, PID reuse, unreadable group members, and non-isolated sessions are
preserved as explicit `unsafe` evidence. Process names, shell command text,
and broad `pkill`/`killall` matching are never valid identity or cleanup
mechanisms.

`failure_limit` is a recovery breaker, not a review verdict. Review leaves set
`max_retries: 1` individually and use the declared two-tier review budget. A
timeout, crash, or
spawn failure is `REVIEW-INCOMPLETE`, never approval.

## Role routing

Profiles represent reusable roles, not repositories:

- `orchestrator`: operating and architecture authority for decomposition,
  architecture, routing, WIP, adjudication, recovery, and tracker writes;
- `implementer`: TDD-first changes in an isolated worktree;
- `code-reviewer`: independent, read-only review from a fresh packet;
- `completion-verifier`: review coverage and board evidence, not code review;
- `integration-operator`: source pull request, host review, CI, and policy-controlled merge;
- optional `qa-ui`: native/browser evidence;
- `release-operator`: consumes the merged revision for policy-controlled release or GitOps/deployment work.

A profile name does not establish these permissions. The project overlay maps
logical roles to exact existing profile names and verifies them before dispatch.

The orchestrator is the default decision-maker between desired outcomes and
worker execution. It drives routine architecture, sequencing, remediation,
recovery, routing, and next-phase decisions within project policy. The gateway
dispatcher remains a mechanical lifecycle owner. See
[`docs/profile-roles.md`](profile-roles.md#orchestrator) for the shared
authority and guardrail contract.

## Profile environment preflight

The controller and worker profile are separate runtime layers. Verify the
profile-scoped `HERMES_HOME`, config, skills, actual tools, `cwd`, interpreter,
project dependency activation, command paths, and non-secret external capability
names through the same profile that will execute the task.

A controller-side test pass is supplementary only. Missing commands, packages,
interpreters, tools, skills, or target paths produce `REVIEW-INCOMPLETE` for
review work and a factory capability diagnostic for implementation work. Repair
the profile/project environment or create a narrower continuation; do not retry
an unchanged prompt. See `docs/profile-environment-contract.md`.

## Review dispatch contract

Every adversarial review leaf is a fresh task reviewing a **change set**, never
a code base. Its kind is exactly one of `pre_commit` (working-tree delta against
the base commit) or `pre_merge` (merge-base delta against the target branch).
Each leaf carries:

- `review_kind` and a change manifest: base reference, candidate reference, and
  changed paths with hunk ranges;
- candidate commit and exact worktree;
- one acceptance question and one review lens;
- diff-targeted commands and explicit non-goals;
- cited gate evidence - command, exit code, run reference, and commit - from the
  implementer/CI. The reviewer never re-runs the full project gate;
- `read_only_source: true`;
- `max_runtime_seconds` set to the declared dispatch hard cap (reference 1800s),
  with an evidence budget (reference 900s) and per-command timeout (120s);
- `max_retries: 1`;
- a structured verdict and stop condition.

A leaf reviews at most five changed files and one acceptance question. Larger
change sets are split into strict-subset slices with a bounded fan-in before
dispatch. A worker killed at the dispatch hard cap is a factory fault to be
diagnosed - provider backoff and forbidden gate commands are not fixed by
narrowing scope.

Do not reuse an implementation card with a new assignee. Reviewers do not
implement fixes, file tracker issues, create Kanban children, deploy, or mutate
live infrastructure. Those actions belong to the orchestrator or a separate
implementer/release task. See `docs/reviewer-role-contract.md` and
`docs/change-scoped-review.md`.

## Delivery handoff

A completed implementation and an `APPROVED` review are not integrated or
released. The orchestrator creates a separate integration task for the
`integration-operator` after completion verification. That task creates or
verifies the pull request, reads back base/head and changed files, waits for
host review and CI, performs a policy-controlled merge, and reads back the
merged revision. A separate `release-operator` then handles approved artifact
or GitOps publication and post-action verification. Missing owners or external
handles are lifecycle gaps, not successful completion. See
`docs/factory-delivery-lifecycle.md`.

## Source reconciliation

A source tracker reconciliation adapter is an external boundary:

- source tracker owns issue identity, labels, source state, and declared
  dependencies;
- the reconciler projects actionable work into Kanban using stable identities;
- Kanban owns claims, worktrees, retries, reviews, and evidence;
- the gateway owns dispatch.

The adapter is configuration-driven. It must not hardcode project hosts,
repositories, checkout paths, labels, profile names, or model routes into the
shared skills. See `docs/tracker-kanban-reconciliation.md`.

## Decomposition and auxiliary models

If a project enables automatic decomposition, it declares the auxiliary provider
and model in project policy. The decomposer creates a task graph; it does not
become an implicit worker profile and does not override explicit assignees.

Unknown profile routes must be surfaced as configuration errors or mapped by the
project's declared policy. Do not silently send malformed review work to an
implementer or malformed implementation work to a reviewer.

## Capacity and reload semantics

A successful small model probe proves route reachability, not worker-sized
capacity. Observe representative requests and concurrent behavior before
raising per-profile fan-out. Provider overload is an infrastructure condition,
not a product verdict.

Some gateway watchers snapshot concurrency and review settings at startup.
After changing them, use only the supervised lifecycle and read back the new
running policy, gateway process, board claims, worker PIDs, and heartbeats.
Never start an unmanaged gateway to apply a setting.

## Verification

From the active profile, verify the effective policy and live owner:

```text
terminal(command="hermes config get kanban.dispatch_in_gateway")
terminal(command="hermes config get kanban.review_dispatch")
terminal(command="hermes config get kanban.max_in_progress")
terminal(command="hermes config get kanban.max_in_progress_per_profile")
terminal(command="hermes kanban assignees --json")
terminal(command="hermes gateway status")
```

For each review task, read back the packet, candidate commit, exact scope,
assignee, runtime/retry fields, run outcome, and mutation check. For each source
adapter, perform two stable dry runs before enabling recurring writes.

Do not report the factory as healthy from a PID, digest, model catalog entry,
or worker summary alone. Require board events, run outcomes, evidence, and the
correct role transition.
