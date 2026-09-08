---
name: kanban-progress-evidence
description: "Use when auditing Kanban progress and closure evidence."
version: 0.1.0
author: Karsten Samaschke, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, evidence, verification, orchestration, audit]
    related_skills: [kanban-implementation-workflow, kanban-factory-operations, software-factory-recovery, kanban-reviewer-contract, tracker-kanban-reconciliation]
---

# Kanban Progress Evidence

## When to Use

Use when auditing a Kanban digest, reviewing progress or risk, reconciling worker claims with the live board, deciding whether work is safe to close, or checking that every stated concern has durable evidence.

This is the evidence and closure layer around Kanban orchestration: the
orchestrator decides and routes work; this skill verifies that every stated
concern has a durable record and that “done” means independently verified.

## Core rule

Never summarize an inventory with “most,” “the main items,” or “nothing else notable” unless the full set was enumerated and reconciled. Every concern, acceptance gap, blocker, and residual risk must appear in an explicit closure matrix.

Separate these states:

- **mentioned** — appears in prose or a worker summary only;
- **recorded** — present in a durable task comment, task body, issue, or artifact;
- **queued** — owned by a task but not started;
- **dependency-gated** — intentionally waiting on named parents;
- **blocked** — cannot proceed and has a diagnostic or explicit decision request;
- **fixed** — implementation changed;
- **verified** — an independent check exercised the result with evidence;
- **closed** — the acceptance gate is satisfied and the board state reflects it.

A worker saying “done” is not proof of fixed, verified, or closed. A verified
implementation/review is not integration or deployment; closure additionally
requires the policy-selected pull-request, merge, release, and post-action
readbacks.

## Decision authority and ladder

The orchestrator owns routine technical and operational decisions. Evidence
verifies the effect after an action; it does not return routine decisions to the
operator. Use the shared decision ladder in
[the canonical role contract](https://github.com/ksamaschke/hermes-software-factory/blob/main/docs/profile-roles.md#decision-ladder) for every
selected issue or locked lane: bind identity and current state, diagnose cause
or uncertainty, choose the next phase, assign ownership and dependencies, act,
read back the mutation, preserve the prior decision while newer work is in
flight, and report the decision separately from liveness.

Fetch the linked contract with `web_extract` when running from an installed
skill; no collection checkout is required. If it cannot be fetched, report
the access gap and hold decisions that need the missing authority or guardrails
rather than inventing them.

A review verdict is valid only when a fresh review packet records the candidate
commit, exact scope, independent profile, read-only boundary, focused checks,
terminal outcome, and mutation check. A timeout, crash, missing scope, tracker
mutation, or absent verdict is `REVIEW-INCOMPLETE`, never approval.

## Human-impact filter

The live board may contain internal failures and intentionally parked cards.
Those are evidence for the factory, not chores for Karsten. Human-facing
reports expose only genuine product/design/priority decisions, external
authorization the factory cannot obtain, security/payment/credential approval,
deployment/release approval, or explicit steering of deliberately parked work.
They do not enumerate parked task IDs or stale `stuck_in_blocked` diagnostics,
and they do not ask the user to restart services, grant routine permissions,
respawn workers, unblock cards, or inspect logs. When no genuine decision is
needed, report `No human action required`.

## Workflow

1. **Extract the complete inventory.** Copy every finding and acceptance gap from the source review, digest, issue, or user message into a numbered list. Include residuals such as placeholder URLs, incomplete tests, environment timeouts, and repository hygiene warnings.
2. **Map each item to durable evidence.** For every entry, record the exact task ID, issue, comment, test artifact, commit, or file. If several findings share a remediation card, keep the individual entries in the matrix; a grouped card does not erase their identities.
3. **Inspect live state.** Use the board's JSON/list/stats and task detail views, not only a previous digest. Reconcile counts, status, assignee, parents, children, diagnostics, latest comments, and run outcomes programmatically where possible.
4. **Check dependency gates.** Confirm that a remediation card cannot start before required review or reproduction evidence, and that a final sign-off task depends on the remediation. A `todo` child behind an unfinished parent is healthy gating; a `ready` task missing required evidence is a process defect.
5. **Verify every write.** After creating a task, adding a comment, blocking/reassigning, or changing a status, read the exact target back. Do not claim an external write succeeded from the write response alone.
6. **Handle timeouts conservatively.** If a create/comment command times out, inspect by idempotency key and title before retrying. It may have succeeded. If an intended blocked task races into `ready`, block or reclaim it immediately, then read it back again. Inspect repository state after any worker may have started.
7. **Report with explicit closure language.** Say which items are recorded, queued, gated, blocked, fixed, independently verified, and closed. Do not collapse these into “handled.” State untracked items plainly and either file them or explain why they are intentionally outside Kanban.

## Autonomous mechanical recovery

Before escalating a task to a human, run the repository's deterministic
recovery add-on and inspect its readback:

```bash
~/.hermes/scripts/kanban_factory_recovery.py --board <board> --dry-run
```

The real watchdog may promote legacy cron snapshots through `hermes cron edit`
and remove only a clean managed worktree whose branch collision blocked a task.
It must preserve dirty or active worktrees and never bypass review, product
acceptance, or deployment policy. Hermes core remains unchanged; this is an
add-on layer.

## Review and remediation pattern

For an adversarial review:

- The review task should preserve the full finding list in a durable comment.
- A remediation task may group related fixes, but its body or a linked comment must enumerate each finding and its acceptance test.
- A re-verification task must depend on remediation and must confirm every individual finding is fixed, tested, or explicitly accepted as residual risk with an owner/reference.
- A production or release task must not be marked done while a high-severity finding, broken stock path, missing E2E evidence, or mandatory test gate remains unresolved.

## Test and environment gaps

A timeout is not a pass and not automatically a product failure. Record:

- the exact command;
- the timeout ceiling;
- whether the process produced a failure or merely no result;
- focused suites that did pass;
- the task that owns the replacement run;
- the condition for closure.

If a full-suite run is unverified, attach that gap to the relevant parent acceptance task instead of leaving it only in a worker summary. A later reviewer must either produce the missing run or record a bounded, explicit environmental decision with substitute evidence.

## Repository-state concerns

Untracked planning files, generated artifacts, or dirty checkouts are not automatically bugs. They are still concerns if release cleanliness or Git-first workflow is part of acceptance. File an operator-disposition task that does not authorize automatic deletion or modification. Keep it blocked until the operator decides whether to commit, move, or explicitly accept the exception. Verify `git status` after any worker could have touched the repository.

## Reporting template

Use the closure-matrix fields below. In a repository checkout, `references/closure-matrix.md` provides the same template as a reusable file; the procedure does not depend on that file being separately installed. The final report should lead with:

- **Decision:** what the orchestrator chose;
- **Durable action:** what changed and was read back;
- **Progress:** independently verified result;
- **Not progressing:** work not advancing;
- **Why:** current cause;
- **Boundary:** internal or external;
- **Owner:** responsible role or lane;
- **Evidence:** exact task/comment/issue/artifact;
- **Next gate:** condition for progress.

Then include the complete **Inventory**, **State**, and **Gaps** sections. Keep
counts as supporting context and distinguish the last completed decision from a
newer decision in flight.

## Closure matrix template

Keep one row per finding or acceptance gap:

- ID / concern and source;
- severity or release impact;
- exact evidence target;
- durable record: task, issue, comment, artifact, or file;
- current state: mentioned / recorded / queued / dependency-gated / blocked / fixed / verified / closed;
- parent or dependency and owner;
- acceptance evidence still required;
- residual-risk decision or reference.

For every timeout, also record the command or worker action, time limit, result, focused checks that passed, replacement owner/task, and closure condition.

## Delivery is not board state

A card's `done` status says nothing about whether its code exists anywhere but a
worktree. Before auditing a completed card, resolve and record these placeholders
from the card and project policy; do not infer them from local defaults:

- `<worktree>` — the absolute candidate worktree path;
- `<base-ref>` — the immutable base commit SHA recorded when the candidate was
  created;
- `<candidate-sha>` — the immutable candidate commit SHA under review;
- `<remote>` — the declared source-control remote name or URL;
- `<source-branch>` — the declared candidate branch;
- `<target-branch>` — the declared integration target branch;
- `<merged-sha>` — the merged commit returned by the hosting service after
  integration.

Before merge, inspect the worktree and prove that the candidate contains work
and is the revision published on the source branch:

```text
terminal(command='git -C "<worktree>" status --porcelain')
terminal(command='git -C "<worktree>" rev-list --count "<base-ref>..<candidate-sha>"')
terminal(command='git -C "<worktree>" ls-remote --heads "<remote>" "refs/heads/<source-branch>"')
```

The immutable pre-merge commit count must be greater than zero. The remote
source-branch SHA must equal `<candidate-sha>`; branch existence alone is not
delivery evidence. A successful
`git merge-base --is-ancestor "<candidate-sha>" "<base-ref>"` result proves
nothing useful when both names resolve to the same zero-change commit.

After merge, use integration evidence rather than the pre-merge change count. Do
not require `<target-branch>..<source-branch>` to remain non-zero after merge: a
valid integration can make that count zero or delete the source branch. Require
the hosting service's merged-commit readback, including the
exact `<merged-sha>` and target branch, then fetch `<target-branch>` from
`<remote>` and compare against that freshly fetched remote target. Apply the
project policy's integration strategy using exact SHAs: fast-forward integration
requires candidate/merge equality; an ancestry-preserving merge requires
`<candidate-sha>` to be an ancestor of `<merged-sha>`; a permitted squash or
rebase requires the hosting service's explicit candidate-to-merge linkage rather
than invented ancestry. In every strategy, `<merged-sha>` must equal or be an
ancestor of the freshly fetched remote target. A stale local branch or a board
transition is not a substitute for either readback.

Preserve dirty work narrowly before any checkout, reset, prune, or branch
replacement. First inventory every dirty and untracked path, then select only
the intended files for the card. Exclude generated and build artifacts and
unrelated changes; leave them untouched and record their disposition. Review
the exact selected diff and scan the selected patch for secrets with the
project-approved scanner before staging only those paths. If the selection is
safe, record the original revision and create a reversible recovery branch and
commit containing only that patch. Treat the recovery commit as a new candidate:
rerun the candidate gate, create a fresh independent review, and continue through
the normal integration gates. Preservation never authorizes broad staging,
destructive cleanup, or review bypass.

A review verdict that lands after the card was closed still counts. Check
comment timestamps against `completed_at`; `CHANGES_REQUESTED` invalidates the
earlier closure. Record every finding and create a new gated candidate/rework path
that must pass implementation, candidate gates, and independent review before
integration can resume.

## Pitfalls

- Treating a digest baseline as the start of the unchanged interval.
- Trusting one `git merge-base --is-ancestor` result as proof of delivery; equal zero-change commits pass, and rewritten integrations need policy-specific hosting-service evidence.
- Preserving a dirty tree with broad staging or without secret scanning, candidate revalidation, and fresh independent review.
- Auditing only blocked cards. Silent failure hides in `done`, where nothing raises a diagnostic.
- Importing tracker items whose label marks them non-actionable; they become permanent blocked cards that farm stale diagnostics.
- Treating a grouped remediation task as proof that each finding has an acceptance test.
- Calling a task “done” because it passed focused tests while the required workspace-wide or E2E gate timed out.
- Retrying a timed-out task creation and silently creating a duplicate.
- Assuming `initial-status=blocked` survived a dispatcher race without reading the task back.
- Reporting board counts from memory instead of the current JSON/list output.
- Hiding untracked or residual items under “non-blocking” without naming their owner and disposition.

## Verification Checklist

- [ ] Complete source inventory reconciled against live board JSON.
- [ ] Every concern has a durable evidence target or is explicitly marked untracked.
- [ ] Parent and child dependency states were inspected.
- [ ] Every review task has a fresh packet and exact-scope read-only evidence.
- [ ] Comments, status changes, and task creation were read back after writes.
- [ ] Worker handoffs are separated from independent verification.
- [ ] Timeouts record command, ceiling, result, owner, and closure condition.
- [ ] Final report uses explicit queued/gated/blocked/fixed/verified/closed language.
