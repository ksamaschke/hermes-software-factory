---
name: kanban-factory-operations
description: "Use when Kanban work stalls; recover dispatch and review."
version: 0.2.0
author: Karsten Samaschke, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, software-factory, orchestration, dispatch, review, evidence]
    related_skills: [kanban-implementation-workflow, kanban-progress-evidence, software-factory-recovery, kanban-reviewer-contract, tracker-kanban-reconciliation]
---

# Kanban Factory Operations

Operate a durable Kanban board as a software factory: tasks are decomposed, implemented, independently reviewed, verified, and released through evidence-backed state transitions. This skill owns live factory operations and recovery. It does not replace project acceptance criteria, deployment policy, or independent code review.

## When to Use

Use when:

- a Kanban digest reports no progress for hours;
- the board has todo/review work but zero or unexpectedly few running workers;
- review handoffs accumulate without independent verdicts;
- workers time out, crash, exit without a terminal Kanban call, or are reported done without evidence;
- the dispatcher, gateway, model route, or worker capacity is suspected;
- a user asks whether the software factory is actually operating.

Do not use a digest alone as the source of truth. Do not use this skill to bypass acceptance gates, silence diagnostics, weaken tests, or mark work done because a worker produced a plausible summary.

## Operating Invariants

1. **The live board is authoritative.** A digest is a historical observation. Current JSON, task details, runs, events, diagnostics, and process state decide what is happening now.
2. **The factory is more than a queue.** A task is not handled until its implementation, independent review, verification evidence, and board transition are all accounted for. A source issue is not execution state until the reconciliation contract has matched it.
3. **Review is a separate role contract.** A reviewer receives a fresh packet with a change manifest (never a whole-file, module, or repository scope), the review kind `pre_commit` or `pre_merge`, candidate commit, read-only source boundary, one lens, the declared two-tier review budget, and one retry. Reviewers run diff-targeted checks only and cite the implementer/CI gate rather than re-running it. Never reuse an implementation card, ask a reviewer to file tracker issues, or let a reviewer implement the review protocol. A timeout, crash, wrong target, or mutation is `REVIEW-INCOMPLETE`, never approval.
4. **One dispatcher owner.** If dispatch is embedded in a supervised gateway, that gateway is the owner. Never launch a second long-lived gateway or standalone dispatcher against the same board database.
5. **Review is a first-class lane.** A review handoff is not completion. A review task assigned to a real reviewer can be automatically dispatched only when the review-dispatch gate and reviewer profile are enabled.
6. **Infrastructure recovery is narrow.** Requeue a card after fixing a dispatcher/backend problem only when its failure is attributable to that problem. Preserve genuine product timeouts, missing evidence, dependency gates, and human decisions.
7. **Worker claims are untrusted.** Verify from task runs, task events, PIDs, heartbeats, diffs, tests, and the exact board state.
8. **Capacity is part of correctness.** A backend that accepts one probe but overloads under fan-out is not a healthy factory route. Bound per-profile concurrency to observed capacity.
9. **Delivery is staged.** Implementation completion and an independent `APPROVED`
   review do not imply integration, pull-request review, merge, or deployment.
   Create and dispatch the policy-selected integration and release handoffs, then
   read back each external result before calling the source work delivered.

## Orchestrator authority and decision ladder

The orchestrator is the default operating and architecture authority between
desired outcomes and worker execution. It chooses and drives the technical or
operational path, including routine architecture, sequencing, remediation,
recovery, routing, WIP, and the next safe phase. It does not ask the operator
because a routine preference is missing.

Use the shared decision ladder in `docs/profile-roles.md` for every selected
issue or locked lane: bind canonical identity and current execution state;
diagnose the observed cause or uncertainty; choose the next phase; assign
ownership, dependencies, acceptance, and fallback; act; read back the exact
mutation; preserve the prior decision while newer work is in flight; and report
the decision separately from liveness.

Guardrails constrain the action, not the orchestrator's decision ownership:
preserve useful work, do not bypass independent review or deployment policy,
do not take destructive or irreversible action without rollback or approval,
and do not start a second dispatcher.

## Action-first triage admission

A zero-ready live snapshot is an action trigger, not a no-progress conclusion.
Before reporting `IDLE-BY-GATING`, the orchestrator must inspect the current
triage frontier and parent completion using the procedure in
`references/action-first-triage-admission.md`. Reconcile the complete `ready`,
`running`, `review`, `todo`, `blocked`, and `triage` scans, current claims and
runs, canonical identities, parent links, diagnostics, archived duplicates,
and effective global/per-profile WIP.

When safe, select **at most one existing canonical parent-complete lane** and
perform **one bounded audited admission/remediation action** through the
supported board/coordinator path. Do not create a new lane merely to fill
capacity, manually spawn a worker, batch admissions, or emit a status-only
no-progress report when a safe independent triage action exists. Read back the
exact action, idempotency key, parent evidence, status, owner, retry state, WIP
accounting, and audit event before claiming progress.

Parked work, unfinished or ambiguous parents, malformed/stale contracts,
duplicate identities, and WIP-full lanes remain unchanged. Genuine external or
human gates, including protected signer/credential boundaries, hold only that
lane; continue independent work without printing or copying credentials. An
internal factory defect, including a routine internal publication or handoff
defect, is not a human gate: route a keyed remediation, reuse its canonical
idempotency key, and verify the readback. Use `IDLE-BY-GATING` only after the
complete status scans prove no safe action remains.

## Add-on recovery layer

Keep factory recovery outside Hermes core. The repository's
`scripts/kanban_factory_recovery.py` is installed as a silent `no_agent` cron
job and repairs only:

- legacy LLM cron jobs whose creation-time snapshots are not durable
  `provider`/`model` fields, using `hermes cron edit`;
- blocked tasks whose latest spawn failure is a duplicate clean managed Git
  worktree, preserving the branch, removing only the clean worktree, unblocking
  the task, and reading the status back.
- blocked/terminal tasks whose `current_run_id` is null but whose task-bound
  Hermes worker process group or descendants remain live; cleanup matches exact
  task plus board/database identity and reaps only the isolated group after a
  final task readback.

Dirty or non-managed worktrees, product failures, provider authorization,
review findings, and deployment decisions remain explicit blockers. Do not
modify Hermes source for these repairs.

## Agent-supervised cron topology

Use two layers for recurring factory supervision. Deterministic controller jobs
are evidence producers, not human reports. They run as explicit `no_agent: true`
jobs with `deliver: local`, which persists their output locally for the central
supervisor; they do not send raw stdout to a human channel. Controller output is
an observation, never proof.

One project-level LLM supervisor consumes every controller through
`context_from` and retains its prior result with `continuity: true`. The
supervisor must run with the project workdir, `terminal`, `file`, and
`code_execution` toolsets, and the `kanban-factory-operations`,
`software-factory-recovery`, `factory-reporting`, and `kanban-progress-evidence`
skills. The generic job shape is in
`examples/factory-cron-supervision.yaml`.

Every supervisor run reads all injected controller outputs, scheduler attempts
and incidents, the prior supervisor result, and fresh live board, tracker,
repository, dispatcher, and scheduler state. It classifies exactly `ACTIVE`,
`IDLE-BY-GATING`, or `STALLED`. If `STALLED` has a bounded safe internal repair,
the supervisor performs it and reads back every mutation; it must not emit a
passive stalled report while an internal action remains available. A controller
exit code, PID, or digest is not proof of recovery.

Keep each tick delta-first and bounded. Reuse continuity, inspect only state that
changed or currently gates progress, and finish within ten minutes by default.
Allow at most eight read-only command batches plus one recovery mutation and its
readback. A supervisor must not scan complete repository or board history, run
the product's full gate, implement product code, or perform the independent
review itself. If a repair exceeds that budget, create one exact-scope factory
task with an owner and acceptance check, read it back, and stop the tick.

Human delivery is secondary: send only concise verified progress, completed
recovery, or a genuine non-delegable decision. Emit `[SILENT]` when no human
action is required, and never forward raw controller output or cron logs. Use
one human delivery target and `attach_to_session: true` when the target is
conversational. `deliver: bot-chat` or `deliver: bot-chat:<profile>` is the
supported alternative for a standalone scheduled output needing one receiving-
agent turn; do not double-route a controller through bot-chat and an existing
central supervisor unless deliberate.

## Review budget protocol

Adversarial code reviews are dispatched as focused, fresh review cards, not as
implementation cards with a new assignee. Each slice uses
`max_runtime_seconds` set to the declared dispatch hard cap, `max_retries=1`, names the change manifest and one review
lens, runs only focused checks, declares `read_only_source=true`, heartbeats at
phase boundaries, and stops discovery at roughly 70% of its budget. Reviewers
may not implement fixes, file source-tracker issues, create Kanban children, or
deploy. Use `kanban-reviewer-contract` for the packet and verdict schema.

Create each leaf with `max_retries=1` and preflight the assigned reviewer with
`hermes -p <reviewer-profile> skills list`. A timed-out leaf must not be
automatically respawned with the same prompt; preserve it as
`REVIEW-INCOMPLETE`, then create a narrower continuation after confirming the
profile can resolve the required skills.

If a review times out, classify it as `REVIEW-INCOMPLETE`. Preserve the run and
its evidence, do not retry the same prompt, and create a narrower continuation
slice. A review timeout is an internal orchestration problem, not a product
blocker and not approval. The implementation card remains gated only on a
completed review verdict, not on the failed worker attempt.

Chunking is hierarchical: split by acceptance question/control-flow path, then
split again whenever a chunk crosses two runtime layers, contains more than
five primary production files, or asks for multiple independent verdicts. Leaf
chunks run independently; a bounded fan-in task reconciles their reports and
acceptance coverage without rescanning the repository. Findings rerun only the
affected leaf after a fix.

Native UI evidence is a separate lane. A skill reference does not provision
`computer_use` to a reviewer worker. Preflight the actual worker schema; if the
tool is absent, HEX/the orchestrator performs the approved capture/input and
attaches the screenshot, process/build provenance, fixture hash, and protocol
checks. The reviewer then validates those artifacts read-only. TCC permission
dialogs remain a human-only boundary and are never delegated to a worker.

## Prerequisites

Resolve before mutating the board:

- board slug and repository identity;
- the dispatcher owner and supervised service lifecycle;
- project-local instructions and canonical verification commands;
- implementer and independent reviewer profiles;
- review-dispatch policy and concurrency caps;
- reviewer packet contract, exact-scope rule, terminal verdicts, and continuation policy;
- backend/model route and its credential ownership without printing secrets;
- deployment policy and release owner.

If any prerequisite is missing, inspect the live state and record the gap. Do not invent a worker, model, repository, or deployment contract.

## Live Stall Procedure

### 1. Establish a current baseline

Use the terminal tool to collect, in one bounded pass:

```text
hermes kanban --board <board> stats --json
hermes kanban --board <board> list --status running --json
hermes kanban --board <board> list --status review --json
hermes kanban --board <board> list --status todo --json
hermes kanban --board <board> diagnostics --json
hermes gateway status
```

Reconcile counts programmatically. Enumerate every active review, blocker, and runnable-looking task. A count that does not reconcile is a failed inspection, not a minor formatting issue.

**Completion criterion:** the report names the exact current counts, every running/review/blocked task, and whether the gateway is supervised by the expected owner.

Only genuine human decisions belong in the central dispatcher/digest report.
Internal mechanical failures, stale diagnostics, worker/tool capability gaps,
provider errors, gateway recovery, and routine board repair remain factory-owned
and must not be presented as work for Karsten. Parked tasks are not human
blockers: never enumerate their IDs or ages in the user-facing digest; never unblock or dispatch them, and at most report that parked backlog is unchanged.
Do **not** create a Matrix subscription for the individual task. Under the
central-reporting policy, task-level `notify-list` entries should remain empty:
workers write board state and events, while the dispatcher or HEX digest reports
only verified progress and genuine decisions. If the central reporting path is
unavailable, escalate that observability failure to the orchestrator rather than
routing a worker directly to Matrix.

### 2. Classify why work is not moving

For each task, distinguish:

- **dependency-gated:** todo with an unfinished parent;
- **review-gated:** review handoff waiting for an enabled reviewer lane;
- **dispatch-stalled:** ready/runnable work with no claim or spawn despite a healthy assignee;
- **backend-failed:** worker starts but model/auth/provider requests fail;
- **capacity-limited:** workers are healthy individually but concurrent fan-out overloads the backend;
- **product-blocked:** repeated implementation timeout, reproducible defect, missing environment capability, or human decision;
- **verified/closed:** independent evidence and board state agree.

Inspect parent links and latest events rather than inferring readiness from the title or priority. A todo card behind a blocked parent is expected gating, not proof that the dispatcher is broken.

**Completion criterion:** every apparent stall has one named cause with task/run/event evidence.

### 3. Check dispatcher ownership and service state

Inspect the supported gateway/dispatcher status. If the service definition is stale, use the documented supervised lifecycle. Do not run a second gateway from a shell to "unstick" the board; concurrent writers can race on the Kanban database and create misleading state.

After any supervised lifecycle action, do not stop at the command's success message. Continue with the live board and worker checks below.

**Completion criterion:** exactly one dispatcher owner is identified, and its service definition/process state is known.

### 4. Inspect gates and profiles

Read the effective configuration without printing secrets. Pay particular attention to:

- `kanban.dispatch_in_gateway`;
- `kanban.review_dispatch`;
- `kanban.failure_limit`;
- `kanban.max_in_progress`;
- `kanban.max_in_progress_per_profile`;
- the assigned profile's existence and model/provider route.

A review profile can exist while review dispatch is explicitly disabled. Conversely, enabling review dispatch without a working reviewer route merely converts a quiet gate into a retry storm.

**Completion criterion:** every review handoff has an explicit decision: dispatchable now, intentionally human-only, or blocked by a named backend/profile issue.

### 5. Verify model routes before rerouting work

Use a bounded non-secret probe:

1. query the authenticated model catalog if supported;
2. select an exact model ID returned by that catalog;
3. send one minimal non-streaming completion with a tiny output budget;
4. record only status, model ID, latency/error class, and whether authentication succeeded;
5. never print API keys, auth-file contents, or full provider error pages.

A model appearing in configuration is not proof that it authenticates, is authorized for the selected provider, or tolerates concurrent workers. Test the actual route the worker profile will use, not a different shell default.

**Completion criterion:** the replacement route has a successful bounded probe, or the task remains explicitly blocked on external provider recovery.

### 6. Recover only infrastructure-caused failures

When a reviewer/backend outage is fixed:

1. update the owning profile or task route to the verified model/provider;
2. read the exact setting back;
3. use the Kanban unblock/requeue operation for only the affected cards;
4. include the reason in the durable comment/event;
5. read every target task back and confirm its resumed status, assignee, and reset retry state;
6. leave genuine product timeouts and human blockers untouched.

Do not blindly retry a task with the same failed backend. Do not reset a failure counter merely to make the dashboard look healthy.

**Completion criterion:** each requeued card has a recorded infrastructure cause and a verified replacement route; unrelated blockers remain intact.

### 7. Bound concurrency and account for reload semantics

Set a per-profile cap based on observed backend capacity. A single successful probe does not validate four simultaneous agents. Prefer a stable review lane with fewer workers over repeated overload/crash cycles.

Some gateway watchers capture concurrency settings at startup. A persisted config edit may therefore be durable but not active in the current process. If reload is needed, use only the supervised lifecycle, and expect to re-check running workers and claims afterward. Never start an unmanaged duplicate to apply a setting.

**Completion criterion:** the effective running cap is known, and the active worker count cannot continue an observed overload pattern.

### 8. Verify real recovery

Require all of the following before calling the factory recovered:

- a task was claimed in the intended source lane;
- a worker PID/run was created;
- the worker is alive or produced a terminal run outcome;
- a heartbeat or equivalent liveness event exists for long work;
- the board recorded a status/event delta;
- independent review or implementation completion is not confused with a self-report;
- remaining blockers are explicitly named.

For completed work, read the task/run back and verify the summary, tests, diff/review evidence, and final board status. A service-start response, a worker process alone, or a digest with new counts is insufficient.

**Completion criterion:** live task/run/event evidence supports the exact recovery claim.

## References

When working from a repository checkout, use the companion references for the detailed runtime-drift symptom matrix, action-first triage admission, and stall-recovery contract:

- `references/action-first-triage-admission.md`
- `references/dispatcher-runtime-drift.md`
- `references/stall-recovery.md`

The core procedure above remains self-contained for direct `SKILL.md` installation.

## Reporting Shape

Use direct decision-oriented sections, not a vague success paragraph:

- **Decision:** what the orchestrator chose;
- **Durable action:** what changed and was read back;
- **Progress:** independently verified result;
- **Not progressing:** work not advancing;
- **Why:** the current cause;
- **Boundary:** internal or external;
- **Owner:** responsible role or lane;
- **Evidence:** exact board, run, diff, test, or artifact evidence;
- **Next gate:** the next condition for progress.

Keep exact counts, board slug, dispatcher health, and worker state as supporting
context. Separate the last completed decision from a newer decision in flight.

Never say "handled" when the state is only mentioned, queued, or running. Use `fixed`, `verified`, and `closed` only when their evidence criteria are satisfied.

## Pitfalls

- Replying to a no-change digest with a restatement instead of checking the live factory.
- Treating a digest baseline as the start of the unchanged interval.
- Assuming todo means ready without reading parent links.
- Assuming review tasks run automatically when `review_dispatch` is false.
- Treating a model catalog entry or one successful request as proof of auth and fan-out capacity.
- Requeueing genuine product timeouts under the label of infrastructure recovery.
- Launching a second gateway or dispatcher to compensate for a suspected stall.
- Assuming a config write changed a watcher that captured settings at startup.
- Claiming recovery from a worker self-report or a successful service command without board events and heartbeats.
- Reporting counts from memory or prose when current JSON disagrees.

## Verification Checklist

- [ ] Current board JSON/stats reconciled.
- [ ] Every review, running, todo, and diagnostic task enumerated.
- [ ] Parent dependencies inspected for apparent todo stalls.
- [ ] Single dispatcher owner and supervised service state confirmed.
- [ ] Review-dispatch policy and profile existence checked.
- [ ] Every dispatched review has a fresh change-manifest packet, read-only boundary, the declared two-tier budget, and one retry.
- [ ] Replacement backend route probed without exposing secrets.
- [ ] Requeues limited to infrastructure-caused failures and read back.
- [ ] Per-profile concurrency cap selected for actual backend capacity.
- [ ] Worker PID/run and heartbeat or terminal event observed.
- [ ] Independent review and deployment evidence kept separate from implementation claims.
- [ ] Final report distinguishes queued, gated, blocked, fixed, verified, and closed.
