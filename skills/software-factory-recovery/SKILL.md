---
name: software-factory-recovery
description: "Use when a software factory stalls; repair and resume work."
version: 1.0.0
author: HEX
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [software-factory, kanban, recovery, continuation, orchestration]
    related_skills:
      - kanban-factory-operations
      - kanban-progress-evidence
---

# Software Factory Recovery

Recover an autonomous software factory when work stops behind worker failures, stale diagnostics, oversized cards, incomplete handoffs, or dependency gates. This skill is the adaptive recovery layer around a Kanban dispatcher. It does not authorize weakening tests, bypassing review, or changing deployment policy.

## Trigger

Use when a factory digest reports stalled work, zero workers, repeated worker timeouts, circuit-breaker blocks, stale capability claims, missing reviewer handoffs, or a ready queue that is not advancing.

## User expectation

Treat internal execution failures as factory-owned. Do not leave work human-blocked because of a worker iteration ceiling, stale capability note, missing handoff, dirty but useful partial worktree, dispatcher observability gap, test-environment mismatch, or a task that needs decomposition. Leave `blocked` only for a genuine human decision, external authorization/capability, or explicit operator disposition. A project policy may make a `parked` backlog item human-gated; verify that policy instead of inferring it.

## Recovery invariants

The factory is an add-on around Hermes, not a replacement for Hermes runtime behavior. Keep deterministic repairs in `~/.hermes/scripts/kanban_factory_recovery.py` and user-owned cron/board operations. Do not modify Hermes source for per-job pinning or routine worktree recovery.

Before escalating, run the recovery layer. It promotes legacy cron snapshots with `hermes cron edit`, repairs only duplicate clean managed linked worktrees after resolving the shared Git common directory and verifying the occupied worktree is checked out to the reported branch, reconciles task-owned worker process groups after a terminal Kanban handoff, preserves dirty work, unblocks only after readback, and leaves product/review/auth/capability/deployment decisions for explicit handling. Worker cleanup requires exact `HERMES_KANBAN_TASK` plus board/database identity and a terminal task with no `current_run_id`; it never falls back to process-name matching or a broad `pkill`.

The orchestrator owns the recovery decision: it chooses whether to replan, split,
reassign, requeue, unblock, replace, or retire work. The recovery layer and
dispatcher carry out only the permitted mechanical action. Preserve useful work;
do not bypass review or policy, start a second dispatcher, or take destructive
or irreversible action without rollback or approval.

Review workers have an additional contract: adversarial code review is
change-scoped (`pre_commit` or `pre_merge`) and split into focused read-only
slices under a two-tier budget - a dispatcher hard cap plus a reviewer evidence
budget at which a verdict must be returned. A timed-out slice is
`REVIEW-INCOMPLETE`, not a finding and not a human blocker.

Preserve the run and classify the cause before choosing a remedy. Provider
backoff, a forbidden full-gate command, or an invalid packet is a budget or
execution-boundary fault: fix the packet or route and re-dispatch the same
slice rather than narrowing it. Narrow only for genuine change-set size, and
continue automatically until every required review question has a verdict or a
genuine external/human limitation exists. A continuation chain that keeps
timing out at shrinking scope is a factory fault, not a scope problem.

Review decomposition is hierarchical. Split by acceptance question or
control-flow path, then split again when a chunk crosses two runtime layers,
contains more than five primary production files, or asks for multiple verdicts.
Use a bounded fan-in task only after the leaf reports exist; it reconciles
evidence and gaps without redoing the full review. A timeout creates narrower
leaf continuations, not an unchanged retry.

Leaf cards use `max_retries=1`; the dispatcher must not consume a second attempt
on the same timed-out review prompt. Verify reviewer skill resolution before
dispatch and record any startup capability failure separately from review
evidence.

For UI evidence, do not retry a reviewer that lacks native desktop tools. Route
the approved capture/input to HEX/the orchestrator, attach the resulting
artifacts, and create a read-only evidence-review continuation. TCC permission
prompts and other OS authorization remain human-only; a worker must report the
capability gap rather than looping or asking the user to repair the board.

1. The live board, process state, runs, events, workspace, and tests outrank a digest or worker summary.
2. One supervised dispatcher owns a board. Never start a second long-lived dispatcher to compensate for uncertainty.
3. Preserve partial work before requeueing. Read the worktree status and prior run handoff; do not discard dirty implementation state.
4. A new worker PID is not recovery. Require a run, heartbeat or useful progress, and a valid terminal transition/review handoff.
5. A failed review or timed-out reviewer is incomplete evidence, not human approval or a product blocker.
6. Requeue internal failures only after changing the cause or task shape. Do not reset counters merely to improve dashboard numbers.

## Agent-supervised cron topology

Keep deterministic controllers and the LLM supervisor separate. A controller is
an explicit `no_agent: true` evidence job with `deliver: local`; its output is
persisted locally for supervision and is never a human report. Controller output
is an observation, never proof. The recovery add-on, board inspector, scheduler
observer, and repository/tracker observers may all be controllers, but their
results do not replace live verification.

Use one project-level LLM supervisor with `context_from` containing every
controller job and `continuity: true` for the previous supervisor result. Give
it the project `workdir`, `terminal`, `file`, and `code_execution` toolsets, and
load `kanban-factory-operations`, `software-factory-recovery`,
`factory-reporting`, and `kanban-progress-evidence`. The generic configuration
shape is `examples/factory-cron-supervision.yaml`.

Every run reads all injected outputs, including scheduler attempts/incidents,
the prior result, and fresh live board, tracker, repository, dispatcher, and
scheduler state. Reconcile them before classifying `ACTIVE`,
`IDLE-BY-GATING`, or `STALLED`. When `STALLED` has a bounded safe internal
recovery, act first, read back every mutation, and classify again. Never emit a
passive stalled report while an internal recovery action remains available.

Supervision is delta-first, not a periodic full audit. Use continuity to limit
each tick to state that changed or currently gates progress. The default ceiling
is ten minutes, eight read-only command batches, and one recovery mutation plus
readback. Do not scan the complete repository/board history, run the product's
full gate, implement product code, or perform independent review inside the
supervisor. A larger repair becomes one exact-scope owned factory task whose
creation is read back before the tick stops.

Human delivery is secondary. Send only concise verified progress, completed
recovery, or a genuine non-delegable decision; otherwise emit `[SILENT]`. Never
forward raw controller output or raw cron logs. Configure one human delivery
target and `attach_to_session: true` when it is conversational.

For a standalone scheduled output that needs one receiving-agent turn,
`deliver: bot-chat` or `deliver: bot-chat:<profile>` is supported. Do not
double-route the same controller through bot-chat and an existing central
supervisor unless the duplication is deliberate and documented.

## Live recovery procedure

### 1. Establish a baseline

Collect in one bounded pass:

```bash
hermes gateway status
hermes kanban --board <board> stats --json
hermes kanban --board <board> list --status ready --json
hermes kanban --board <board> list --status running --json
hermes kanban --board <board> list --status review --json
hermes kanban --board <board> list --status blocked --json
hermes kanban --board <board> diagnostics --json
hermes kanban --board <board> notify-list --json
```

Reconcile counts programmatically. Inspect every root blocker and every task that appears ready. Classify each as internal execution, dependency, review, external capability/authorization, or explicit operator disposition.

### 2. Reconcile terminal worker processes

Run the installed recovery add-on before classifying a blocked lane as external or
before reporting a worker as active. For each blocked task, it reads the exact
task detail and, only when the card is terminal with `current_run_id=null`, scans
task-owned process environments and isolated process groups. The controller
revalidates the task and process start-times immediately before signalling,
sends bounded `SIGTERM` followed by `SIGKILL` when necessary, and emits only
bounded task/PID/group status. A board mismatch, PID reuse, unreadable member,
or non-isolated group is `unsafe` and is preserved for operator/orchestrator
disposition; it is never a reason to kill by command name.

The recovery result is evidence, not delivery proof. Read back the task status,
`current_run_id`, process inventory, and cleanup result. A task that is still
`running` or has a current run is not eligible for cleanup.

### 3. Repair the worker execution contract

Interactive profile limits must not silently cap durable Kanban jobs. Add a recognized `kanban.worker_max_turns` config key with a bounded default and pass it after the `chat` subcommand:

```text
hermes ... chat --max-turns <kanban.worker_max_turns> -q "work kanban task <id>"
```

Register the key in the canonical config defaults/schema, not only in a local YAML file. Keep the interactive profile's `agent.max_turns` unchanged.

Use the project-managed test environment, not a mixed system interpreter:

```bash
uv sync --extra dev --locked
uv run pytest -q <focused-kanban-tests>
```

The bounded-budget change is a reusable first layer. It is not proof that the entire factory is recovered.

### 3. Preserve and resume internal cards

For each internal breaker-blocked card:

1. read the full task, comments, runs, events, and workspace status;
2. add a durable recovery comment naming the changed execution contract;
3. explicitly unblock/reclaim the card, preserving prior run history and the workspace;
4. verify the card's new status and current run;
5. verify the actual spawned command contains the worker budget;
6. verify PID, heartbeat/progress, and eventual terminal handoff.

Respect global and per-profile WIP caps. Cards that cannot run immediately should remain `ready`, not be mislabeled human-blocked.

### 4. Use semantic continuation, not infinite retry

A full factory should distinguish these outcomes:

- `completed`: worker called the terminal completion protocol and evidence is present;
- `blocked_human`: genuine human decision, external authorization, or unavailable capability;
- `continuation_pending`: worker reached a bounded segment boundary with a valid checkpoint and no failure;
- `failed`: crash, provider failure, real timeout, protocol violation, or no-progress segment.

`continuation_pending` must preserve workspace, session/run context, summary, completed criteria, remaining criteria, and next action. The dispatcher may start a bounded continuation segment without incrementing the failure breaker. Set a maximum segment count and wall-clock budget; repeated no-progress segments become `failed` and escalate. Do not implement continuation by silently resetting counters or endlessly respawning the same prompt.

### 5. Escalate failures durably

On a real failure episode, emit one actionable event containing task id/title,
profile, run sequence, budget, failure threshold, last error, dependencies,
workspace, and next action. Deduplicate by task plus failure episode. Deliver
it through the central dispatcher/orchestrator reporting path; workers and
individual task cards must not contact Matrix directly. The central HEX digest
reads the current board snapshot, so it does not depend on replaying old task
subscriptions.

Do not create or retain per-task Matrix subscriptions under the
central-reporting policy. `IDLE-BY-GATING` is valid when the central digest
reports verified progress, genuine human decisions if any, and explicitly says
`No human action required` when internal or parked state is the only reason work
is not spawnable. Internal failures and stale diagnostics are never a human
repair task. Parked tasks are never unblocked or dispatched. A missing central
report is an observability failure owned by the orchestrator, not a reason to
route a worker directly to the human.

The recovery add-on acknowledges imported parked cards once, idempotently, with
durable machine evidence that their blocked state is intentional. It does not
change their status, assignee, or dispatchability. This repairs stale generic
blocked diagnostics without hiding or advancing parked work.

### 6. Verify the recovered factory

Do not report `ACTIVE` or `RECOVERED` until all are true:

- at least one intended task was claimed after the repair;
- its command used the worker-specific budget;
- a live PID and run record exist;
- a heartbeat or useful progress event exists;
- a terminal Kanban transition or independent review handoff exists;
- the board counts and remaining blockers were read back;
- no claim relies only on worker prose.

## Reporting

Use these sections:

- **Live state:** exact board counts and gateway owner;
- **Internal repairs:** code/config/task transitions and readbacks;
- **Verified progress:** task/run/PID/heartbeat/terminal evidence;
- **Human blockers only:** explicit decisions or external authorizations;
- **Remaining work:** queued, dependency-gated, review-gated, or failed;
- **Decision:** continue automatically, queue behind capacity, or request a human decision.

Never call a card handled when it is only mentioned, running, or queued. Use `fixed`, `verified`, and `closed` only with evidence.

## References

For the validated bounded-worker-budget implementation and test-environment procedure, see `references/kanban-worker-budget-recovery.md`. The continuation outcome remains an architectural requirement until covered by implementation and integration tests.

## Pitfalls

- Treating the absence of an action-only dispatcher log as proof that the dispatcher is dead;
- blindly unblocking the same oversized task without changing its execution contract;
- raising the global interactive budget instead of setting a worker-specific bound;
- mixing the system pytest interpreter with the Hermes environment;
- treating stale CuaDriver or capability comments as current live state;
- treating a worker summary, PID, or green focused test as final completion;
- converting every failure into a human blocker instead of repairing internal logic;
- starting a duplicate dispatcher or writer against the same SQLite board.
