---
name: kanban-implementation-workflow
description: "Tracker-agnostic Kanban work with TDD and review."
version: 1.1.0
author: Karsten Samaschke (ksamaschke), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [forgejo, github, kanban, orchestration, tdd, review, testing, gitops]
    related_skills: [kanban-factory-operations, kanban-progress-evidence, software-factory-recovery, kanban-reviewer-contract, tracker-kanban-reconciliation]
---

# Tracker + Hermes Kanban Implementation Workflow

Use this skill when a code or issue-tracking backlog should be implemented through Hermes Kanban workers while an orchestrator owns decomposition, verification, review, and delivery. The workflow is intentionally policy-driven: it does not assume a tracker, repository, branch, model vendor, deployment controller, or profile roster.

## Shared factory versus project customization

The shared factory owns durable mechanics and safety invariants: source identity,
Kanban state, isolated worktrees, claims, retries, review gates, evidence, and
one supervised dispatcher. Project policy and external add-ons own tracker
hosts, repository mappings, labels, acceptance gates, model routes, and
project-specific automation.

If multiple factories on one host need different tools, permissions, memory, or
routing, create custom profiles and map them in project policy. Do not modify the
shared skills or Hermes core to fit one product, and do not treat a project
adapter as a new universal factory rule.

## When to use

Use when:

- work spans multiple implementation/review steps;
- dependencies and durable handoffs matter;
- tasks should survive agent restarts;
- a human may need to intervene;
- test evidence and review history must remain auditable.

Do not use it to bypass project rules, deploy without an approved rollout policy, or replace a small direct edit with unnecessary orchestration.

## Prerequisites

Resolve these before creating ready work:

- tracker kind and project identity;
- source adapter and dependency convention;
- base/integration branch;
- project-local instructions (`AGENTS.md`, `CLAUDE.md`, or equivalent);
- headless test/build commands;
- UI-smoke command, if applicable;
- deployment mode and rollout owner;
- implementer and independent reviewer profiles;
- completion verifier, integration operator, and release operator profiles;
- WIP and concurrency limits.
- dispatcher owner, effective runtime policy, and the supported reload path.

If deployment policy is absent, do not mutate production state. Use the public policy template at https://raw.githubusercontent.com/ksamaschke/hermes-software-factory/main/examples/project-policy.yaml as a starting point, not as a universal policy.

Requires: `git`, `hermes`, and the project-declared tracker adapter.

## Roles and dedicated profiles

Profiles represent reusable roles, permissions, and model routing rather than repositories:

- `orchestrator` — operating and architecture authority for decomposition,
  architecture, ownership, sequencing, remediation, recovery, routing, WIP,
  adjudication, and the next safe phase;
- `implementer` — TDD-first writes in isolated worktrees;
- `code-reviewer` — independent read-only review using a different model/vendor family;
- `completion-verifier` — checks review coverage, acceptance evidence, and board transitions;
- `integration-operator` — creates/verifies the source pull request, host review, CI, and policy-controlled merge;
- optional `qa-ui` — native/browser verification for UI-only acceptance;
- `release-operator` — consumes the merged revision for project-policy-controlled release or GitOps/deployment work.

Use task-level model overrides for strength tiers when behavior and permissions are unchanged. Create a separate profile when tools, credentials, memory, safety policy, or write permissions differ. The gateway dispatcher handles mechanical lifecycle work; the orchestrator drives the technical and operational decisions about what should run next.

Use the shared decision ladder in
[the canonical role contract](https://github.com/ksamaschke/hermes-software-factory/blob/main/docs/profile-roles.md#decision-ladder) for every selected
issue or locked lane. Bind canonical identity and current execution state,
diagnose the observed cause or uncertainty, choose the next phase, assign
ownership/dependencies/acceptance/fallback, perform the durable action, read it
back, preserve the prior decision while newer work is in flight, and report the
decision separately from liveness. Routine missing preferences are not reasons
to stop; project policy's non-delegable boundaries are.

Fetch the linked contract with `web_extract` when running from an installed
skill; no collection checkout is required. If it cannot be fetched, report
the access gap and hold decisions that need the missing authority or guardrails
rather than inventing them.

## Quick reference

Inspect the repository:

```text
terminal(command="git status --short --branch && git branch -vv --all")
terminal(command="git diff --stat && git diff --cached --stat")
```

Inspect the board:

```text
terminal(command="hermes kanban --board <board> stats --json")
terminal(command="hermes kanban --board <board> diagnostics")
terminal(command="hermes kanban --board <board> list --status running --json")
```

Monitor events:

```text
terminal(command="hermes kanban --board <board> watch --kinds completed,blocked,gave_up,crashed,timed_out")
```

Command examples use POSIX shell syntax. On Windows, translate them to PowerShell or use the platform-specific commands declared by the project policy.

## Procedure

### 1. Discover and disposition the checkout

1. Read project rules, build files, and tracker configuration with `read_file`/`search_files`.
2. Inspect branch, staged/unstaged diff, worktrees, and stash list with `terminal`.
3. Classify dirty changes as current work, partial useful work, superseded work, intentional docs, or noise.
4. Run targeted tests on meaningful dirty work before discarding it.
5. Preserve partial/superseded work in a named stash.
6. Return the shared checkout to the latest valid integration ref and verify local/remote ref parity.
7. Keep intentional planning documents; do not silently stash user context.
8. Keep Kanban worktree containers out of project Git policy through local excludes unless the repository explicitly wants them tracked.

Completion criterion: the shared checkout is explainably clean, preserved work is recoverable, and the selected base commit is verified.

### 2. Import the live tracker backlog

Fetch the complete live work-item set with the project's declared adapter. The
adapter may be a provider CLI, REST/GraphQL client, webhook consumer, or a
project-owned script. Normalize the result into tracker-neutral fields:

- stable provider item key and exact URL;
- kind, state, title, body, labels/fields, comments, timestamps;
- declared or native dependencies;
- target repository and project mapping.

Provider examples include paginated `tea` issue reads for Forgejo/Gitea and
`gh api --paginate` for GitHub. GitLab, Bitbucket, and custom systems must use
their declared adapter rather than being forced through either command. Do not
rely only on stale local mapping documents.

Fetch comments/activity for each retained item with the adapter, then compare the
Kanban import count against the complete paginated source set, not one page. The
project policy declares how dependencies and tracking-only work are identified;
do not infer equivalence between tracker features.

Import actionable items, not tracking epics. Preserve the source key, URL, kind,
title, body, labels/fields, comments, and dependency metadata. Keep parked or
human-only work out of ready dispatch.

Create or match canonical intake tasks before dependent children and pass real
parent IDs during child creation. Use an idempotency key per source item. Validate
the imported count programmatically before proceeding.

Completion criterion: every requested actionable item is represented exactly once
or has an explicit one-to-many decomposition, and every declared execution
dependency is encoded as a board link or recorded as deferred.

### 3. Decompose into many small cards

Prefer many small, independently verifiable cards. Limit **execution**, not useful decomposition.

Use project-appropriate limits such as:

```yaml
kanban:
  auto_decompose: true
  auto_decompose_per_tick: 3
  max_in_progress: 8 # total cap; tune for aggregate backend capacity
  max_in_progress_per_profile: 2 # conservative start; raise after a capacity probe
```

These are examples. Tune them to the host, model/API quota, and file-conflict risk.

Good cards include one of:

- reproduce/isolate a defect;
- write one failing regression test;
- implement one route or transport group;
- implement one UI behavior;
- verify one lifecycle;
- perform one independent review;
- validate one release artifact.

Never allow a generated review card to land on the implementer profile. Use real dependency links rather than prose-only “wait for X”.

Completion criterion: cards are small enough to verify, dependencies are real, and active-worker limits are explicit.

### 4. Implement TDD-first in a worktree

Each code-changing worker:

1. reads its board card, parent handoffs, and project rules;
2. writes a failing test or executable regression check;
3. runs it and records the real failure;
4. makes the smallest production change;
5. reruns focused and regression tests;
6. runs project lint/type-check/build gates;
7. checks diff scope and `git diff --check`;
8. requests independent review instead of self-approving.

Do not weaken, skip, delete, or loosen tests to make a gate green.

Completion criterion: the worktree contains only scoped changes, tests exercise the acceptance behavior, and the handoff names exact commands/results.

### 5. Test headless first

Use headless checks whenever they prove the behavior:

```text
terminal(command="<project Rust test command>")
terminal(command="<project frontend test command>")
terminal(command="<project frontend build command>")
terminal(command="<project frontend lint command>")
```

Use in-process HTTP tests or a temporary headless server for server behavior. Use jsdom/component tests or headless browser tests for browser behavior.

Launch the full desktop/WebView shell only when acceptance requires native windows, WebView navigation, sidecars, dialogs, trays, visual editor behavior, signed updater installation, or other native evidence. Do not open Terminal with `osascript` or start a long-lived Vite/Tauri process for routine tests.

When a full smoke test is required, make it a separate time-boxed card, capture actual screenshots/logs, track every process started, and clean all app/Vite/helper processes before handoff. If display/accessibility support is unavailable, block with the exact limitation rather than retrying blindly.

Completion criterion: headless evidence exists before any GUI attempt; GUI evidence is limited to genuinely native acceptance criteria.

For native UI acceptance, separate capture from review. HEX/the orchestrator or
an explicitly tool-capable `qa-ui` lane performs desktop input and records the
app/window, build and process provenance, fixture hashes, screenshots, and
live protocol checks. The independent reviewer consumes attached artifacts
read-only. Do not dispatch a reviewer with a `macos-computer-use` skill and
assume that the native tool exists in its schema; skills do not provision tools.
Treat macOS TCC permission dialogs as a human-only capability boundary.

### 6. Review independently

Create a fresh review task from a typed packet; do not reuse the implementation
card body with a new assignee. Use `kanban-reviewer-contract` and assign a
profile from a different vendor family when possible. The packet names the
candidate commit, the review kind (`pre_commit` or `pre_merge`), a change
manifest of changed paths with hunk ranges, one review lens, diff-targeted
commands, cited implementer/CI gate evidence, `read_only_source=true`, the
declared runtime budgets (dispatch hard cap, evidence budget, per-command
timeout), `max_retries=1`, and a stop condition. The reviewer reads the original acceptance criteria, diff, tests,
error paths, security boundaries, and deployment constraints. It reruns
relevant checks where possible and reports file/line evidence.

A reviewer does not implement fixes, file source-tracker issues, create Kanban
children, reassign work, or deploy. The orchestrator owns findings adjudication,
source-tracker writes, continuations, and rework routing. A review protocol that
needs code is an implementation task, not reviewer work.

Create adversarial reviews as change-scoped slices before dispatch. Each slice
must carry the declared two-tier budget and an explicit stop condition, and must
run diff-targeted checks only - the full project gate is the implementer's/CI's
evidence and is cited, never re-run inside a review. Do not put a full
repository scan, baseline reproduction, native E2E, and security review in one
card. Split at least the transport/bootstrap/security, frontend parity, and
packaging/lifecycle concerns when they are all in scope. Require a heartbeat at
phase boundaries and a partial evidence handoff before 70% of the budget.

Set `max_retries=1` on every review leaf and preflight the assigned profile's
skill resolution before dispatch. A timed-out leaf is `REVIEW-INCOMPLETE` and
must be replaced by a narrower continuation; never let the dispatcher retry
the unchanged prompt automatically.

Use hierarchical chunking rather than a fixed three-card split. Build a review
manifest and make each leaf answer one acceptance question/control-flow path
with no more than five primary production files plus directly referenced tests
or config. Split a leaf again when it crosses two runtime layers or contains
multiple independent verdicts. After the leaves complete, use a bounded
fan-in/synthesis card to reconcile reports and acceptance coverage; it must not
repeat a whole-repository scan.

If a slice times out, mark it `REVIEW-INCOMPLETE`, preserve its evidence, and
create narrower leaf continuation tasks at the last verified boundary. Never
retry the unchanged broad prompt and never treat the timeout as a product
blocker or approval. Rerun only the affected leaf after a finding is fixed,
then rerun synthesis.

Use these canonical outcomes:

- `APPROVED`: independent evidence is sufficient;
- `CHANGES_REQUESTED`: a reproducible defect or acceptance gap routes to focused implementation work;
- `REVIEW-INCOMPLETE`: the reviewer did not reach or finish the target, or the packet/capability was invalid.

`PRELIMINARY` is a non-terminal evidence qualifier for same-family, unknown-family,
or partial coverage; it maps to `REVIEW-INCOMPLETE` when final independent review
is required. `BLOCKED` is an orchestrator/board state for a genuine external or
human decision; a reviewer does not emit it as a leaf verdict.

Completion criterion: no code card is accepted as final solely from its implementer’s report.

### 7. Integrate through source control

After completion verification and an `APPROVED` independent review, create and
assign a separate integration task to the configured `integration-operator`.
The integration operator must create or verify the pull request in the declared
source repository, read back its base branch, head revision, title/body, and
changed files, then obtain the required host review and CI results. It merges
only under project policy and reads back the merged commit. A pushed branch,
worker summary, or Kanban review verdict is not a merge.

Completion criterion: the external PR handle, required host-review/CI evidence,
merge decision, and merged revision are durable and read back. If the
integration owner, repository capability, PR, host review, CI, or merge policy
is missing, keep integration incomplete and do not close the source work.

### 8. Apply the project deployment policy

Resolve deployment mode from project instructions or policy config.

For a `gitops_only`/`argocd` project, for example:

1. change desired state in the declared GitOps repository;
2. render and validate before state changes;
3. commit/push the desired state;
4. sync through the declared controller;
5. verify controller revision/health and the real data plane.

For a `release_only` project, build and sign the artifact, publish it through the declared release system, and verify artifact discovery/application there; do not treat artifact creation as rollout success.

Do not run direct `kubectl apply`, Helm mutation, or ad-hoc production rollout when the project policy forbids it. For a project with another explicit rollout contract, follow that contract instead. If no contract exists, stop before production mutation.

Completion criterion: rollout evidence matches the declared project policy, not a generic assumption.

## Agent-supervised cron topology

Recurring factory supervision has two separate layers. Deterministic controller
jobs are evidence producers and one project-level LLM supervisor is the only
scheduled interpreter. Keep this topology generic and project-owned; the
reference shape is [`examples/factory-cron-supervision.yaml`](../../examples/factory-cron-supervision.yaml).

Each deterministic controller is an explicit `no_agent: true` job using
`deliver: local`. Its output is persisted locally and consumed as evidence; it
does not report to a human channel. Controller output is an observation, never
proof. A controller may collect live board, tracker, repository, scheduler
attempt, or scheduler incident state, but the supervisor must verify those
observations itself.

The one supervisor job uses `context_from` for every controller output and
`continuity: true` for its prior result. It has the project `workdir`, the
`terminal`, `file`, and `code_execution` toolsets, and the
`kanban-factory-operations`, `software-factory-recovery`, `factory-reporting`,
and `kanban-progress-evidence` skills. Every run reads all injected outputs,
scheduler attempts/incidents, the prior result, and fresh live board, tracker,
repository, dispatcher, and scheduler state before classifying `ACTIVE`,
`IDLE-BY-GATING`, or `STALLED`.

If the live state is `STALLED` and bounded safe internal recovery is available,
the supervisor performs it and reads back every mutation before reporting. It
must not emit a passive stalled report while an internal action remains
available. Human delivery is secondary: send concise verified progress,
completed recovery, or a genuine non-delegable decision; otherwise emit
`[SILENT]`. Never forward raw controller output or raw cron logs. Use one
project-approved human delivery target and `attach_to_session: true` when the
target is conversational.

For a standalone scheduled output needing one receiving-agent turn, use
`deliver: bot-chat` or `deliver: bot-chat:<profile>` instead. Do not double-route
the same controller through `bot-chat` and an existing central supervisor unless
that duplication is deliberate and documented.

### 9. Monitor and report

The gateway dispatcher mechanically promotes, claims, spawns, heartbeats, reclaims, retries, and caps work. The orchestrator/HEX drives adaptive routing, architecture, WIP, reviewer assignment, remediation, recovery, and human decisions. Factory-specific mechanical repairs belong in the external add-on `scripts/kanban_factory_recovery.py`, not Hermes core: pin legacy cron snapshots, repair duplicate clean managed worktrees, verify readback, and leave dirty/product/review/human blockers untouched.

Use `terminal` to inspect:

```text
hermes kanban --board <board> stats
hermes kanban --board <board> list --status running
hermes kanban --board <board> diagnostics
hermes kanban --board <board> show <id>
hermes kanban --board <board> runs <id>
hermes kanban --board <board> log <id>
```

For recurring human updates, use the single supervisor topology above: its
controller inputs remain local, while only the supervisor has the configured
human delivery target. A local CLI/Desktop chat may not accept scheduled
delivery.

Every report leads with Decision, Durable action, Progress, Not progressing,
Why, Boundary, Owner, Evidence, and Next gate. Counts and liveness remain
supporting context. Distinguish the last completed decision from a newer
decision currently in flight.

## Factory health and recovery

When a digest reports no progress, classify the live factory instead of treating the digest as the source of truth. Collect current board JSON, `hermes gateway status`, effective `kanban` configuration, and the latest dispatcher evidence from the supervised gateway. A supervised PID is not proof that the dispatcher is ticking.

Use these classifications:

- `ACTIVE` — workers are running and dispatcher ticks are recent;
- `IDLE-BY-GATING` — no runnable work exists because named dependencies or human decisions hold it;
- `STALLED` — actionable todo/review work exists but dispatcher liveness, runtime policy, profile routing, or backend capacity prevents execution.

Persisted configuration is not necessarily effective configuration. Gateway watchers may capture `max_in_progress`, `max_in_progress_per_profile`, and review-dispatch settings at startup. After changing them, use only the supervised lifecycle, then verify the new gateway PID, startup policy lines, board claims, worker PIDs, and heartbeats. Never start a second gateway or standalone dispatcher against the same database.

A successful tiny model probe proves route reachability, not worker-sized capacity. Test the exact profile/provider/model route and observe a representative request before increasing fan-out. Treat `Overloaded` as `REVIEW-INCOMPLETE` until route capacity is repaired, reduce the per-profile cap, and queue excess reviews rather than repeatedly retrying the same backend.

Requeue only cards whose failure is attributable to the repaired infrastructure. Record the cause and replacement route in a durable comment, use the Kanban unblock/requeue operation, read back status/assignee/retry state, and verify a new run, PID, and heartbeat. Preserve genuine product timeouts, missing UI evidence, dependency gates, and human decisions.

## Reviewer reliability and failure handling

A reviewer is only valid when it reached the intended target. Before reviewing, verify the absolute repository/worktree path, Git commit/branch, named files, read-only intent, bounded turns/runtime, and minimal reviewer toolset. Put the target path in the prompt; do not assume the controller's outer `cwd` propagates.

Use `CI=1` and `TERM=dumb` for non-interactive Hermes CLI review sessions when supported. These are POSIX shell prefixes; on PowerShell set `$env:CI = "1"` and `$env:TERM = "dumb"` before launching the reviewer. A target-not-found error, wrong-cwd, terminal-clear failure, timeout, iteration exhaustion, missing credential, or process crash is `REVIEW-INCOMPLETE`, never approval.

Also record the exact provider/model route and effective reviewer concurrency cap. A catalog entry or one successful completion is not evidence that concurrent review workers can run safely.

On incomplete review: inspect logs/processes, verify no source files changed, retry once with a smaller explicit scope or clean profile, then block with the exact limitation. Do not repeat the same failed launch indefinitely. The final handoff names the target commit, files inspected, checks run, findings, verdict, and limitations. See https://github.com/ksamaschke/hermes-software-factory/blob/main/docs/reviewer-reliability.md.

## Policy adaptation

Do not copy project-specific assumptions into this skill. Put them in a project policy file or repository instructions:

- branch and tracker;
- gate commands;
- profiles and model tiers;
- UI-smoke availability;
- deployment mode/controller;
- notification destination;
- protected paths and secrets policy.

See https://raw.githubusercontent.com/ksamaschke/hermes-software-factory/main/examples/project-policy.yaml and https://github.com/ksamaschke/hermes-software-factory/blob/main/docs/policy-resolution.md in the public collection repository.

## Pitfalls

- Many cards are not a problem; unbounded active workers are.
- Auto-decomposition before profiles, review, and checkout policy are ready creates noise and unsafe claims.
- A same-profile review is preliminary, not independent.
- A named custom provider must preserve the endpoint’s exact model ID; verify the provider route with a harmless completion before assigning it to workers.
- A tiny model probe can pass while a worker-sized or concurrent request returns `Overloaded`; verify observed capacity before fan-out.
- A persisted WIP/config edit can differ from the policy loaded by the running gateway; reload through the supervised owner and read the effective setting back.
- A stale supervised PID or a digest with unchanged counts does not distinguish healthy gating from a stalled dispatcher.
- Use `key_env`; never inline or print credentials. Rotate a key if a command expanded it into config or output.
- A terminal tab can outlive its process, and a process can outlive the tab. Verify and clean both.
- Never claim a full UI flow from unit tests or a worker summary alone.
