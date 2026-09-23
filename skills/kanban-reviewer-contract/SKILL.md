---
name: kanban-reviewer-contract
description: Define bounded, read-only Kanban review work.
version: 0.5.0
author: Karsten Samaschke, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, reviewer, review, evidence, read-only]
    related_skills: [kanban-factory-operations, kanban-progress-evidence, scoped-subagent-audits]
---

# Kanban Reviewer Contract Skill

Use this skill to create or operate an independent Kanban review. A reviewer
checks an implementation or a declared artifact; it does not implement fixes,
operate the source tracker, deploy infrastructure, or decide that missing
evidence is approval. The review packet is a separate contract from the
implementation card.

## When to Use

Use when:

- an implementation task moves to review;
- a security, reliability, test-adequacy, or release-readiness review is needed;
- a previous review timed out, crashed, or returned an unverifiable claim;
- a factory needs a completion verdict backed by exact evidence.

Do not use this skill to implement a review protocol, file tracker issues, merge
code, or perform a deployment. Those are separate implementation, orchestrator,
or release tasks.

## Role split

Keep these responsibilities distinct:

- **Implementer:** changes code in an isolated worktree and requests review.
- **Code reviewer:** independently inspects and exercises the candidate, read-only.
- **Completion verifier:** checks that required review coverage and board evidence
  exist; it does not replace the code review.
- **Orchestrator/tracker operator:** is the operating and architecture authority
  within project policy; decomposes work, creates continuations, adjudicates
  findings, files tracker issues, chooses recovery, and routes rework. It does
  not implement source changes or bypass the independent review contract.
- **Release operator:** performs only the project-policy-approved release or
  deployment action.

A profile name does not establish a role. The task packet must state the role.
If a configured profile named `reviewer` is used for adversarial code review,
apply the code-reviewer contract below rather than relying on the profile name.

## Review kinds

The factory dispatches exactly two kinds of adversarial code review. Both review
a **change set**; neither reviews a code base.

- **`pre_commit`** - the working-tree change set an implementer proposes to
  commit. Scope basis: `git diff <base>` plus `git diff --cached` plus declared
  untracked files, against the base commit named in the packet.
- **`pre_merge`** - the change set a pull/merge request would introduce into the
  target branch. Scope basis: `git diff $(git merge-base <target> <source>)..<source>`.

A packet that is neither kind is invalid. Neither kind may grow into a
repository-wide audit: if a change genuinely needs whole-codebase judgement, the
orchestrator files a separate architecture-review task with its own card and
budget. That is never an escalation of a commit or merge review.

## Scope rule: the diff is the boundary

The reviewed unit is the set of changed hunks - not the changed files in full,
not their modules, not the repository. Scope is declared as a **change
manifest**: base reference, candidate reference, and the enumerated changed
paths with hunk ranges.

A scope expressed as a directory, glob, module name, topic label, or "the
repository" is invalid; return `REVIEW-INCOMPLETE: invalid review packet`.

Reading outside the diff is allowed only as context for a specific changed hunk
- the definition a changed line calls, the test covering it, the declaration it
overrides. That reading must be traceable to a named hunk. Unchanged code is not
part of the finding surface; a defect visible only there is a `gaps` note for
the orchestrator, not a finding against this change set.

## Runtime budget

Reviews carry two distinct limits, because one wall clock covering model
latency, provider backoff, and command execution produces kills rather than
verdicts. Projects declare the values; the reference budget is:

- `dispatch_hard_cap_seconds: 1800` - the dispatcher's `--max-runtime`
  SIGTERM/SIGKILL backstop against a hung worker. Not a target.
- `evidence_budget_seconds: 900` - the reviewer's own mandatory return point.
- `command_timeout_seconds: 120` - hard cap on any single command.

The reviewer records its start time, tracks elapsed time, and **returns a
verdict at or before the evidence budget** with whatever evidence it holds,
listing everything unchecked under `gaps`. Checkpoints: at 50% stop opening new
context, at 70% stop starting new commands, at 100% emit the verdict.

Returning at the evidence budget is correct behavior. Being killed at the
dispatch hard cap is an anomaly - a broken reviewer, a broken route, or an
invalid packet - and is always `REVIEW-INCOMPLETE`. Provider backoff counts
against the evidence budget; a review consumed by `429`/cooldown returns
`REVIEW-INCOMPLETE` naming the provider condition, never approval.

## Execution boundary

A change-scoped reviewer runs **diff-targeted checks only**: tests covering the
changed hunks selected by node id/file/pattern, linters or type checks scoped to
changed paths, focused probes and scratch harnesses outside the source worktree,
and read-only inspection commands.

It must not run the full project gate - `make test`, `make validate`, a full
suite, or a full build - nor any command expected to exceed the per-command
timeout.

**The full gate is the implementer's and CI's evidence; the reviewer cites it
and never re-runs it.** The packet carries the gate command, exit code, run
reference, and the commit it ran against. The reviewer confirms that evidence
exists and corresponds to the candidate commit. Missing, stale, or
unverifiable gate evidence is a `CHANGES_REQUESTED` finding against the
implementation card, not a reason to run the gate.

## Review packet

Do not dispatch a review until the packet contains every field below:

- implementation task ID and source issue/PR, if any;
- `review_kind`: `pre_commit` or `pre_merge`;
- target repository, worktree path, branch, and candidate commit;
- implementer profile and reviewer profile, with vendor-family comparison;
- a change manifest: base reference, candidate reference, and changed paths with
  hunk ranges. No directories, globs, modules, or topic labels;
- one review lens and the single acceptance question it must answer;
- original acceptance criteria and diff-targeted commands to run;
- cited gate evidence: command, exit code, run reference, and commit;
- explicit non-goals and live-system boundaries;
- `read_only_source: true`;
- the declared runtime budgets for every adversarial code-review leaf: dispatch
  hard cap (reference 1800s), evidence budget (reference 900s), per-command
  timeout (reference 120s);
- `max_retries: 1`;
- stop condition and required evidence format;
- environment provenance: profile-scoped runtime, effective `cwd`, interpreter,
  required command paths/versions, dependency activation, and preflight result.

A review card must not inherit an implementation prompt unchanged. Reject the
packet if it contains implementation language such as "TDD first", "write the
fix", or "make the production change"; if it asks the reviewer to create or
edit tracker issues; if its scope is not a change manifest; or if its checks
include a full-gate command. The orchestrator creates a fresh review task with
the review packet.

## Profile environment preflight

Hermes profile isolation is a runtime boundary. The controller's interpreter,
`HERMES_HOME`, skills, toolsets, `cwd`, credentials, and dependency cache do not
necessarily propagate to the worker profile. Run the smallest project-declared
probe through the same profile/runtime that will execute the review.

Verify the profile identity, worktree/commit, effective `cwd`, interpreter and
version, required command paths/versions, dependency activation, and external
capability names without secrets. A controller-side passing test is supplementary
only; it cannot satisfy reviewer-side preflight.

Missing commands, packages, interpreters, skills, tools, or target paths are a
capability gap and produce `REVIEW-INCOMPLETE`, not a product finding. The
orchestrator repairs the profile/project environment or creates a narrower
continuation. A timeout, crash, or provider failure follows the same incomplete
path. After changing the profile environment, run preflight again instead of
retrying the unchanged prompt. See `docs/profile-environment-contract.md`.

## Mutation boundary

The reviewer may:

- read the named worktree, diff, task context, and project instructions;
- run focused tests, linters, renderers, fake services, or read-only probes;
- write scratch harnesses outside the source worktree when needed;
- emit heartbeats and one structured review report;
- use the worker protocol's single terminal transition for its own review run.

Scratch files and test caches are not source changes. Verify the worktree status
before the verdict and report any unexpected change.

The reviewer must not:

- edit source, tests, project configuration, or documentation in the candidate;
- commit, push, merge, rebase, or change branches;
- create, edit, label, close, or comment on tracker issues or pull requests;
- create child Kanban tasks, reassign work, unblock unrelated cards, or dispatch workers;
- deploy, mutate a cluster, rotate credentials, or change live infrastructure;
- expand the review to another repository or runtime layer without a new packet.

A reviewer may append its report to its own review record when the worker
protocol requires it. Tracker mutations and child-task creation belong to the
orchestrator/tracker lane, never inside the review.

## Procedure

1. **Validate the packet.** Check `review_kind`, the change manifest, candidate
   commit, profiles, read-only boundary, lens, diff-targeted commands, cited
   gate evidence, runtime budgets, retry limit, and stop condition. Completion
   criterion: every required field is present, the scope is a change manifest,
   no full-gate command is requested, and no prohibited mutation is requested.
2. **Preflight the target.** Confirm the worktree is completely clean (including
   untracked files), branch, candidate commit, base reference, project
   instructions, and named changed paths. The native claim receipt binds the
   complete task title/body and resolved Git/worktree identity; any later task
   edit or dirty worktree makes the verdict incomplete. Record the review start
   time and compute the evidence-budget deadline. If the target is missing or
   the profile cannot resolve its required review capability, return
   `REVIEW-INCOMPLETE`; do not improvise a repository-wide search.
3. **Read the change set cold.** Inspect the diff for the manifest hunks and the
   acceptance criteria before reading any surrounding code. Keep the working set
   inside the manifest; context reads must trace to a named hunk.
4. **Verify the cited gate evidence.** Confirm the packet's gate command, exit
   code, run reference, and commit match the candidate. Do not run the gate.
   Missing or stale evidence is a `CHANGES_REQUESTED` finding.
5. **Run the diff-targeted checks.** Exercise the changed behavior and its error
   paths with checks scoped to the changed hunks, each within the per-command
   timeout. Record command, exit code, fixture, and result.
6. **Honor the evidence budget.** At 50% stop opening new context; at 70% stop
   starting new commands; at 100% emit the verdict with recorded gaps. Do not
   run past the evidence budget waiting to be killed at the dispatch cap.
7. **Check mutation and provenance.** Verify the candidate commit, changed-file
   set, worktree status, and that scratch artifacts stayed outside the source.
8. **Emit one verdict.** Use exactly one terminal outcome for the review run.
   Do not call multiple competing terminal actions after the run is already
   terminal.

## Verdicts

Use only these outcomes:

- `APPROVED` — every required question is answered with sufficient evidence,
  the exact scope is covered, and no contract violation occurred;
- `CHANGES_REQUESTED` — a reproducible defect or acceptance gap exists; record
  file/line, evidence, impact, and the smallest required correction;
- `REVIEW-INCOMPLETE` — timeout, crash, spawn failure, wrong target, missing
  capability, provider failure, scope violation, or missing evidence prevented
  a valid verdict.

A timeout or crash is never a finding, approval, or clean result. A reviewer
that writes source or tracker state has violated the contract; its result is
`REVIEW-INCOMPLETE` even if it also reports a plausible finding.

Before the one terminal Kanban call, classify the review lifecycle from native
task/run/events rather than task prose:

- A **same-card review** has an exact `review_requested` handoff and a fresh
  reviewer run claimed from `source_status=review`. `APPROVED` uses
  `kanban_complete` once with structured metadata containing exactly
  `review_outcome: APPROVED` as the sole verdict field and the exact 40-hex
  `candidate_commit` copied from the implementation handoff. Do not include
  `verdict`, `overall_verdict`, `terminal_verdict`, `review_verdict`, or any
  other `*_verdict` key;
  `CHANGES_REQUESTED` uses `kanban_request_changes`, whose native rework
  transition on the original task is the only implementation lane. The
  orchestrator must not create a second implementer task for that verdict. The
  implementer's earlier request-review run is not review evidence.
- A **standalone review leaf** is a separate review card claimed from
  `source_status=ready` with no `review_requested` handoff. Its exact body
  schema names `review_type: read-only adversarial code review leaf`, direct
  `implementation_task`, canonical `owner/repository`, real worktree, branch,
  40-hex base and candidate commits, a SHA-256 of the NUL-delimited sorted
  changed-path manifest, distinct implementer/reviewer profiles,
  `read_only_source: true`, supported review kind, and
  `review_scope: change_set`. The native claim persists resolved worktree and
  Git-dir identities and rejects wrong repos, branches, symlinks, scope, or
  role collisions. Exact `APPROVED` calls `kanban_complete` once and includes
  structured metadata `review_outcome: APPROVED` plus the exact 40-hex
  `candidate_commit`; the native boundary re-reads the exact positive run ID,
  active reviewer profile, packet, worktree identity and HEAD, and candidate
  before release. `CHANGES_REQUESTED` never calls `kanban_request_changes`:
  call `kanban_block` once with `kind=dependency` and a reason beginning exactly
  `STANDALONE_REVIEW_CHANGES_REQUESTED:`, followed by the bounded structured
  findings. Within Hermes-managed SQLite connections, Native Boundary `1.0.25`
  terminally closes that exact run and
  writes one immutable remediation outbox receipt; the reviewer creates no
  product task. `REVIEW-INCOMPLETE` calls `kanban_block` once without the
  changes-requested prefix and remains gated for coordinator adjudication.

All immutable and fail-closed claims in this standalone-leaf contract are
scoped to Hermes-managed SQLite connections. A process running as the board
database's owning OS user or holding raw file write access is trusted and can
replace the file, drop triggers, or register lookalike functions. Isolate
hostile workers behind a broker or a distinct OS identity without database
write access.

Never call `kanban_request_changes` for a standalone leaf. Never attempt a
second terminal action after rejection. Do not manufacture the native prefix
for incomplete evidence. A task body cannot override native lifecycle
preconditions, and stale, foreign, contradictory, changed-frontier, wrong-HEAD,
or newer run evidence fails closed without downstream release.

## Lifecycle qualifiers

The three leaf verdicts are `APPROVED`, `CHANGES_REQUESTED`, and
`REVIEW-INCOMPLETE`. `PRELIMINARY` is only a non-terminal evidence qualifier
for same-family, unknown-family, or partial coverage; it maps to
`REVIEW-INCOMPLETE` when an independent final review is required. `BLOCKED` is
an orchestrator/board state for a genuine external or human decision, not a
reviewer verdict. A reviewer records incomplete evidence and the orchestrator
owns any board block or continuation.
Return a compact structured report containing:

- `implementation_task` and `candidate_commit`;
- `reviewer_profile` and `implementer_profile`;
- `scope_checked` with exact paths;
- `acceptance_coverage` for every criterion;
- `checks` with commands, exit codes, and evidence;
- `findings` with severity, file/line, reproduction, and impact;
- `gaps` and `mutations`;
- `environment_provenance` separating worker-side checks from parent-side supplementary checks;
- `verdict` exactly as defined above;
- `next_action`, limited to rework, continuation, fan-in, or approval.

The parent/orchestrator independently reads the diff, task run, and decisive
evidence before treating the report as true.

## Continuations and fan-in

A timed-out leaf is preserved as `REVIEW-INCOMPLETE`. Do not retry the same
prompt unchanged. **Classify the cause before choosing a remedy:**

- provider backoff/`429`, a forbidden full-gate command, or an invalid packet -
  the scope was never the problem. Fix the packet or the route and re-dispatch
  the same slice. Do not narrow it;
- genuine change-set size - the orchestrator creates a continuation whose change
  manifest is a strict subset of the previous one, carrying prior evidence
  forward.

Raising the runtime budget is not a recovery step. A continuation chain that
keeps timing out at shrinking scope is evidence of a budget or
execution-boundary defect, not of a change set needing more splitting; escalate
it as a factory fault.

When a change set exceeds one leaf (more than five changed files or more than
one acceptance question), the orchestrator splits it before dispatch: group
changed hunks into coupled-concern slices, give each its own strict-subset
manifest and single question, and create one bounded fan-in. The fan-in reads
only the leaf reports and the coverage matrix; it never rescans the repository
and never re-runs checks. Every hunk in the parent change set must appear in
exactly one leaf manifest. Any uncovered hunk, incomplete leaf,
changes-requested leaf, or contract violation blocks final approval.

Fail closed only when a single hunk cannot fit a leaf budget: report the change
set as unreviewable and require the implementer to split the commit or PR.

Do not combine code review with source-tracker filing. If a finding should become
an issue, the orchestrator creates a separate tracker task after adjudicating
the finding. If a review protocol itself needs implementation, route that work
to an implementer and review the resulting code separately.

## Profile and capacity rules

Before dispatch:

- verify the exact reviewer profile and its resolved skills/tools;
- verify the reviewer is independent from the implementer vendor family;
- use the observed per-profile concurrency cap, not an optimistic default;
- set `--max-runtime` to the declared dispatch hard cap and carry the evidence
  budget and per-command timeout in the packet body; keep `max_retries=1`;
- do not infer read-only capability from a skill name; skills do not provision
  tools or revoke terminal/file write capability.

If the runtime cannot enforce read-only tools, enforce the boundary through the
packet, source-status readback, worker lifecycle, and parent verification. A
profile description alone is not a security boundary.

The shared contract defines reviewer mechanics and safety invariants. If
factories on the same host need different tools, permissions, memory, or
routing, create custom profiles and map them in project policy. Do not weaken or
fork the shared reviewer lifecycle to fit one product.

Project-specific behavior belongs in a project policy file, review packet, or
external factory add-on. Shared skills and Hermes core are not extension points
for one product's review protocol.

## Pitfalls

- Reassigning an implementation card to a reviewer without replacing its body.
- Asking a reviewer to file tracker findings while it is reviewing.
- Declaring scope as file paths, a module, or a directory instead of a change
  manifest - a whole file is not a change set, and it lets the review drift into
  surrounding code.
- Running the project gate inside a review. It is the single largest consumer of
  the budget and the evidence already exists; cite it.
- Letting one wall clock cover model latency, provider backoff, and command
  execution, so the worker is killed instead of returning a verdict.
- Treating a dispatch-cap kill as the reviewer's normal stop condition rather
  than as a factory fault.
- Narrowing scope in response to a timeout that was actually caused by provider
  backoff or a forbidden gate command.
- Treating a reviewer summary, heartbeat, or successful test command as approval.
- Letting a reviewer expand from one repository to a private overlay or live
  cluster without a new packet.
- Retrying a crashed or timed-out review with the same scope and prompt.
- Treating a same-family reviewer as independent without recording the exception.

## Verification

The review contract is satisfied only when:

- the packet is complete and read back from the Kanban task;
- `review_kind` is exactly `pre_commit` or `pre_merge`, and scope is a change
  manifest with base reference, candidate reference, and hunk ranges;
- `--max-runtime` equals the declared dispatch hard cap, and the evidence budget
  and per-command timeout are present in the packet;
- no full-gate command appears in the checks, and gate evidence is cited with a
  run reference matching the candidate commit;
- the reviewer reached a terminal outcome exactly once, inside its evidence
  budget;
- exact scope and candidate commit are recorded;
- source and live-system mutation checks are clean;
- every acceptance criterion has evidence or an explicit gap;
- incomplete slices remain gated and no downstream work was released;
- any tracker or implementation mutation is owned by a separate task.
