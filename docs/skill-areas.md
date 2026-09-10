# Skill Areas and Synchronization Boundaries

This repository is the maintained source for reusable software-factory skills and
their supporting references. It is not a mirror of every skill installed in a
Hermes profile.

## Area: Software factory

These skills form the software-factory operating set:

### `operator-observer`

**Primary concern:** read-only human-facing evidence and transport.

Use for:

- exact tracker, Kanban, repository, scheduler, deployment, and sanitized runtime readback;
- concise owner/boundary/evidence/next-gate reporting;
- one sanitized response handoff for a policy-declared non-delegable decision;
- keeping all routine product and factory mutations with the orchestrator.

The observer is not a second orchestrator and is never a substitute for a
native implementer, reviewer, integration, or release profile.

### `kanban-implementation-workflow`

**Primary concern:** end-to-end implementation workflow.

Use for:

- tracker issue import and dependency mapping;
- task decomposition and WIP limits;
- TDD-first implementation in isolated worktrees;
- independent review and verification;
- headless-first testing and project-policy-controlled release work.

### `kanban-factory-operations`

**Primary concern:** live factory operation.

Use for:

- dispatcher and gateway ownership;
- board health classification;
- reviewer capacity and backend routing;
- infrastructure-caused requeue decisions;
- recovery verification and factory status reporting.

### `kanban-progress-evidence`

**Primary concern:** evidence and closure accounting.

Use for:

- reconciling complete board inventories;
- mapping concerns to durable tasks, comments, runs, commits, and artifacts;
- distinguishing queued, dependency-gated, blocked, fixed, verified, and closed;
- preserving timeout and environment gaps;
- producing closure matrices instead of vague “handled” summaries.

### `software-factory-recovery`

**Primary concern:** autonomous recovery around the dispatcher.

Use when:

- a factory stops behind internal worker or dispatcher failures;
- a legacy cron job has creation-time inference snapshots but no explicit pin;
- a blocked task failed only because a clean managed Git worktree already owns
  its branch;
- a bounded recovery and readback is possible without changing product,
  review, deployment, authorization, or human-decision state.

The companion `scripts/kanban_factory_recovery.py` is deterministic and runs as
a silent `no_agent` cron job. It is an add-on around Hermes, not a replacement
for Hermes runtime behavior.

## Area: Audit and review support

### `scoped-subagent-audits`

**Primary concern:** bounded independent audits.

Use for:

- explicit repository/live-state scope;
- time-budgeted audit workers;
- checkpoint and timeout handling;
- parent-side verification of worker claims.

It supports the factory but is not a dispatcher or implementation workflow by
itself.

### `kanban-reviewer-contract`

**Primary concern:** typed, bounded, read-only review work.

Use for:

- fresh review packets instead of assignee-only handoffs;
- exact-scope, one-lens review leaves;
- profile/worktree environment preflight;
- two-tier adversarial review budgets (dispatch hard cap plus evidence budget)
  and one retry;
- `APPROVED`, `CHANGES_REQUESTED`, and `REVIEW-INCOMPLETE` outcomes;
- keeping tracker mutation, implementation, and deployment outside the review.

### `tracker-kanban-reconciliation`

**Primary concern:** project-specific tracker-to-Kanban source reconciliation.

Use for:

- canonical issue identity and idempotent intake tasks;
- configurable tracker adapters, actionability labels, states, and dependency fields;
- conservative source-state reconciliation;
- project overlays and external poller/add-on design;
- preserving the supervised gateway as the only dispatcher.

The skill defines the reusable adapter contract. A concrete product must provide
its own overlay or add-on rather than modifying the shared skill.

## Repository-owned supporting files

- `scripts/kanban_factory_recovery.py` — deterministic recovery add-on;
- `scripts/kanban_factory_recovery_cron.py` — regular in-directory cron shim;
- `tests/test_kanban_factory_recovery.py` — regression tests for collision parsing;
- `skills/kanban-factory-operations/references/` — runtime drift and stall recovery;
- `skills/kanban-progress-evidence/references/` — closure-matrix template;
- `skills/software-factory-recovery/references/` — worker-budget and recovery evidence contract;
- `docs/policy-resolution.md` and `examples/project-policy.yaml` — project policy adaptation.

## Explicit Hermes synchronization allowlist

Only these installed skill directories are symlinked to this repository:

- `scoped-subagent-audits`;
- `operator-observer`;
- `kanban-implementation-workflow`;
- `kanban-factory-operations`;
- `kanban-progress-evidence`;
- `kanban-reviewer-contract`;
- `tracker-kanban-reconciliation`;
- `software-factory-recovery`.

The symlink destinations are category-specific under `~/.hermes/skills/`. The
repository path is authoritative for the linked directories; changes in the
checkout are immediately visible to Hermes after the normal skill reload.

The category-specific links are established by the local installation and may
use `<HERMES_HOME>` or profile-scoped `HERMES_HOME` roots. A generic local
installation can map:

- `<HERMES_HOME>/skills/.../scoped-subagent-audits` → `skills/scoped-subagent-audits`;
- `<HERMES_HOME>/skills/.../operator-observer` → `skills/operator-observer`;
- `<HERMES_HOME>/skills/.../kanban-implementation-workflow` → `skills/kanban-implementation-workflow`;
- `<HERMES_HOME>/skills/.../kanban-factory-operations` → `skills/kanban-factory-operations`;
- `<HERMES_HOME>/skills/.../kanban-progress-evidence` → `skills/kanban-progress-evidence`;
- `<HERMES_HOME>/skills/.../kanban-reviewer-contract` → `skills/kanban-reviewer-contract`;
- `<HERMES_HOME>/skills/.../tracker-kanban-reconciliation` → `skills/tracker-kanban-reconciliation`;
- `<HERMES_HOME>/skills/.../software-factory-recovery` → `skills/software-factory-recovery`.

The external-dir entry must be explicitly trusted by each profile that loads
these links. Profile-specific paths, tracker values, and credentials stay in
local profile or project configuration and are never copied into this public
repository.

Adding another symlink requires an explicit allowlist change. A skill being
related to the factory does not implicitly grant synchronization ownership.

## Installation and verification

From this checkout:

```bash
hermes skills install \
  https://raw.githubusercontent.com/ksamaschke/hermes-software-factory/main/skills/operator-observer/SKILL.md

hermes skills install \
  https://raw.githubusercontent.com/ksamaschke/hermes-software-factory/main/skills/kanban-implementation-workflow/SKILL.md

install -m 755 scripts/kanban_factory_recovery.py \
  ~/.hermes/scripts/kanban_factory_recovery.py
```

For the local development installation, verify the five allowlisted paths with
`readlink` and verify the source checkout with:

```bash
git status --short --branch
git remote -v
```
