# hermes-software-factory

Reusable workflow skills for [Hermes Agent](https://hermes-agent.nousresearch.com/), focused on durable multi-agent engineering, review, and operations.

This repository is intentionally **policy-aware rather than policy-hardcoded**. The skills define safe defaults and require each project to declare its tracker (Forgejo/Gitea, GitHub, GitLab, Bitbucket, or a custom adapter), test gate, deployment policy, profiles, and operational constraints.

## Shared factory versus project add-ons

The shared factory provides the mechanics and invariants: source identity,
Kanban state, isolated worktrees, claims, retries, review gates, evidence, and
one supervised dispatcher. Project overlays and external add-ons provide tracker
hosts, repository mappings, labels, acceptance gates, model routes, and product-
specific automation.

If multiple factories share a host but their tools, permissions, memory, or
routing differ, create custom profiles and map them in project policy. Do not
modify these shared skills or Hermes core to fit one project or product. Product
adapters are separate deliverables.

## Included skills

### `scoped-subagent-audits`

A bounded audit/review procedure that requires explicit working scope,
appropriate time budgets, checkpoints, timeout recovery, and parent-side
verification of worker claims.

### `kanban-reviewer-contract`

A typed reviewer role contract: fresh exact-scope packets, read-only source
boundaries, one-lens 600-second leaves, one retry, structured verdicts, and
separate orchestrator/tracker mutation.

### `tracker-kanban-reconciliation`

A project-agnostic tracker-to-Kanban adapter contract for Forgejo/Gitea, GitHub,
GitLab, Bitbucket, and custom REST/CLI/webhook sources. It covers canonical
identity, idempotent intake tasks, actionability/dependency mapping, conservative
source-state reconciliation, and supervised-dispatch separation. Concrete
projects provide overlays or external add-ons.

### `kanban-implementation-workflow`

A tracker-agnostic Hermes Kanban workflow for:

- importing actionable issues while excluding tracking epics;
- turning `Depends on:` into real task dependencies;
- decomposing work into many small cards without unbounded fan-out;
- TDD-first implementation in isolated worktrees;
- independent adversarial review;
- headless-first testing with explicit UI-smoke exceptions;
- project-defined GitOps and rollout controls;
- durable monitoring, notifications, and progress digests.

### `kanban-factory-operations`

Live operation and recovery for stalled factories: dispatcher ownership, gateway/runtime drift, reviewer capacity, narrow requeueing, and ACTIVE/IDLE-BY-GATING/STALLED health classification.

### `kanban-progress-evidence`

Evidence and closure accounting for digests and reviews: complete inventories, durable records, dependency gates, timeout attribution, independent verification, and explicit queued/blocked/fixed/verified/closed language.

### `software-factory-recovery`

Adaptive recovery for autonomous factories. The skill uses the external
`scripts/kanban_factory_recovery.py` add-on to repair legacy cron inference
snapshots and deterministic duplicate clean worktree failures without changing
Hermes core or discarding dirty work.

The core skill does not select a deployment controller. Each project declares its own rollout policy; the repository includes an illustrative GitOps/Argo policy example, but Argo is not a requirement of the workflow.

See [`docs/skill-areas.md`](docs/skill-areas.md) for the area-of-interest map,
the software-factory skill set, supporting files, and the explicit Hermes
synchronization allowlist.

Central Kanban reporting, the versioned runtime configuration contract, and
the dispatcher/digest boundary are documented in
[`docs/central-kanban-reporting.md`](docs/central-kanban-reporting.md).

The broader runtime, profile-routing, decomposer, and explicit-profile
contract is documented in
[`docs/kanban-factory-runtime.md`](docs/kanban-factory-runtime.md).

## Tracker adapters

The workflow supports multiple code and issue trackers through declared
adapters. The project policy selects the adapter and source-of-truth conventions:

- Forgejo/Gitea: use `tea` or the Forgejo REST API;
- GitHub: use `gh` or the GitHub REST/GraphQL API;
- GitLab: use the GitLab CLI/API or a project-owned client;
- Bitbucket: use the Bitbucket CLI/API or a project-owned client;
- custom systems: use a deterministic REST, GraphQL, CLI, or webhook adapter;
- preserve each provider’s native issue URLs, labels, comments, and dependency conventions;
- do not assume that a GitHub milestone, Forgejo epic label, GitLab issue type, Bitbucket pull request, or custom field has the same meaning on another tracker.

The Kanban card format is tracker-neutral: retain the source kind, project,
item key, URL, labels/fields, body, and parent dependencies in imported metadata.

## Install

Install the Skill directly from GitHub:

```bash
hermes skills install \
  https://raw.githubusercontent.com/ksamaschke/hermes-software-factory/main/skills/kanban-implementation-workflow/SKILL.md

hermes skills install \
  https://raw.githubusercontent.com/ksamaschke/hermes-software-factory/main/skills/kanban-factory-operations/SKILL.md

hermes skills install \
  https://raw.githubusercontent.com/ksamaschke/hermes-software-factory/main/skills/kanban-progress-evidence/SKILL.md

hermes skills install \
  https://raw.githubusercontent.com/ksamaschke/hermes-software-factory/main/skills/kanban-reviewer-contract/SKILL.md

hermes skills install \
  https://raw.githubusercontent.com/ksamaschke/hermes-software-factory/main/skills/tracker-kanban-reconciliation/SKILL.md

hermes skills install \
  https://raw.githubusercontent.com/ksamaschke/hermes-software-factory/main/skills/software-factory-recovery/SKILL.md
```

Once installed, load the set explicitly for a session:

```bash
hermes --skills kanban-implementation-workflow,kanban-factory-operations,kanban-progress-evidence,kanban-reviewer-contract,tracker-kanban-reconciliation,software-factory-recovery
```

The deterministic recovery add-ons are installed separately from the skill
documents:

```bash
install -m 755 scripts/kanban_worker_reaper.py ~/.hermes/scripts/kanban_worker_reaper.py
install -m 755 scripts/kanban_factory_recovery.py ~/.hermes/scripts/kanban_factory_recovery.py
install -m 755 scripts/kanban_review_successor_recovery.py ~/.hermes/scripts/kanban_review_successor_recovery.py
install -m 755 scripts/kanban_review_successor_recovery_cron.py ~/.hermes/scripts/kanban_review_successor_recovery_cron.py
```

The terminal-worker recovery pass is deliberately bounded. It reads the
minimal task status from the query-only Kanban SQLite database when the Hermes
CLI is locked or slow, caps each CLI call at five seconds by default, and caps
optional full-detail repairs at 30 seconds per tick. A timeout is reported as
unavailable; it never becomes permission to mutate or kill an unverified task.
The worker reaper matches the exact task and board identity inherited by the
worker, revalidates the terminal status immediately before signalling, and
signals only a fully task-owned process group. It never uses process names,
shell kill commands, or a broad PID sweep.

Forgejo-backed projects can also install the generic read-only delivery
observer and copy its non-secret overlay template:

```bash
install -m 755 scripts/forgejo_delivery_controller.py ~/.hermes/scripts/forgejo_delivery_controller.py
install -m 644 examples/forgejo-delivery-overlay.json ~/.hermes/scripts/<project>-forgejo-delivery.json
python3 ~/.hermes/scripts/forgejo_delivery_controller.py \
  --config ~/.hermes/scripts/<project>-forgejo-delivery.json
```

The observer paginates open and closed pull requests, live repository branches,
and configured runner scopes, reads combined commit status, and emits bounded
`ACTIVE`, `IDLE-BY-GATING`, or `STALLED` JSON. Branch prefixes, exact exclusions,
and inventory limits stay in the overlay; matching newly pushed branches appear
as `pushed/no-PR` on the next tick without editing it. The observer performs
GETs only. Schedule a project wrapper as a local `no_agent` controller and feed
that job into the one project supervisor; see
[`examples/factory-cron-supervision.yaml`](examples/factory-cron-supervision.yaml)
and [`docs/factory-delivery-lifecycle.md`](docs/factory-delivery-lifecycle.md).

The repository also carries the deterministic Minna outbound PR controller used
as a concrete project add-on. Install the controller, its packet validator, and
the silent cron wrapper together, then copy and adapt the example configuration:

```bash
install -m 644 scripts/review_packet_integrity.py ~/.hermes/scripts/review_packet_integrity.py
install -m 755 scripts/minna_review_cycle.py ~/.hermes/scripts/minna_review_cycle.py
install -m 755 scripts/minna_review_cycle_cron.py ~/.hermes/scripts/minna_review_cycle_cron.py
install -m 600 examples/minna-review-cycle.json ~/.hermes/scripts/minna-review-cycle.json
```

The project configuration must pin a reviewer provider/model whose vendor family
is independent from the implementer, define the full fresh-clone gate, and list
product source issues in dependency order. Run the controller without `--apply`
first; only schedule the wrapper after that dry run and one controlled apply tick
have been read back.

The recovery add-on validates the durable packet before dispatch, keeps authored
acceptance questions verbatim, creates at most eight lossless two-file successor
leaves, and preserves an incomplete packet when that bound cannot cover the
whole scope. Successor creation is read back and rolled back to archived state
if any card in the batch fails validation; failed create responses are
rediscovered by exact idempotency key before cleanup. Dry-run performs only
read-only inspection and planning.

Run the review add-on in dry-run mode before scheduling it:

```bash
python3 scripts/kanban_review_successor_recovery.py \
  --board <project-board> --dry-run
```

The project wrapper is suitable for a silent `no_agent` cron job. Set
`HERMES_FACTORY_BOARD` explicitly; it runs the tracked sibling implementation
with `--apply --quiet`. The wrapper remains outside Hermes core and never
creates a second dispatcher.

Existing profiles need to load or install the skill explicitly. Newly cloned profiles inherit the skill set available at clone time.

## Policy resolution

The workflow resolves policy in this order:

1. explicit user instructions or standing delegation;
2. repository `AGENTS.md`, `CLAUDE.md`, or equivalent project rules;
3. the project policy file;
4. Kanban/task-specific constraints;
5. safe defaults;
6. operator clarification for genuinely non-delegable decisions only.

This precedence does not remove repository safety rules, project policy,
protected paths, or forbidden mutations. Within delegated scope, the
orchestrator chooses the simplest adequate policy-compliant option rather than
asking for every routine technical preference.

If a safety-critical value is unspecified, hold only the affected action and
use the central clarification path; independent work continues. A routine
missing preference is not a reason to stop.

Use [`examples/project-policy.yaml`](examples/project-policy.yaml) as a starting point. See [`docs/policy-resolution.md`](docs/policy-resolution.md) for how to adapt it.

## Role model

Profiles represent **roles, permissions, and model routing**, not repositories:

```text
orchestrator  →  implementer  →  code-reviewer  →  completion-verifier
                                                    ↓
                           integration-operator  →  release-operator
                           PR/host review/merge      artifact/deployment
```

Implementation, independent review, integration, and deployment are separate
states. A worktree or Kanban verdict never implies a pull-request review, merge,
or rollout.

Recommended reusable profile roles:

- `orchestrator` — operating and architecture authority for decomposition,
  architecture, ownership, sequencing, remediation, recovery, routing, WIP,
  adjudication, and tracker writes; no source-code writes;
- `implementer` — TDD-first code changes in isolated worktrees;
- `code-reviewer` — independent read-only review from a fresh typed packet;
- `completion-verifier` — checks review coverage, acceptance evidence, and board transitions;
- `integration-operator` — creates/verifies the source pull request, host review, CI, and policy-controlled merge;
- `qa-ui` — optional native/browser verification lane for UI-only acceptance;
- `release-operator` — consumes the merged revision for project-policy-controlled release/GitOps/deployment work.

In `project-policy.yaml`, role keys use snake_case (`qa_ui`, `integration_operator`, `release_operator`) while profile names may use hyphens. The mapping is intentional: policy keys are stable schema names; profile values are user-selected identities.

The orchestrator drives routine technical and operational decisions between
desired outcomes and worker execution. The Hermes gateway dispatcher handles
mechanical Kanban lifecycle work: promotion, claims, worktrees, heartbeats,
retries, and reclaim. The orchestrator chooses the next safe phase; workers
execute bounded implementation, review, verification, and release tasks.

See [`docs/profile-roles.md`](docs/profile-roles.md#orchestrator) for the
authority, guardrails, and decision ladder.

Use task-level model overrides for strength tiers where possible. Create another profile when behavior, tools, credentials, or memory isolation differ, not merely because a task needs a stronger model.

## Testing principle

Headless-first is a default, not a project-specific mandate:

- unit, integration, HTTP, jsdom, and headless-browser tests run without a desktop shell;
- full desktop/WebView smoke tests are reserved for native-window, sidecar, updater, visual-editor, or other acceptance criteria that genuinely require them;
- any process started by a smoke test is tracked and cleaned up before handoff.

## Deployment principle

Deployment is always project policy, never a hidden default. A project policy declares its rollout mode and controller:

- `gitops_only` with Argo CD, Flux, or another named controller;
- a release pipeline or explicitly approved direct mode;
- `unspecified`, which means no production mutation is allowed until clarified.

The example policy demonstrates one GitOps/Argo arrangement without baking any user's infrastructure paths, rooms, tokens, or repository names into the reusable skill.

The required distinction between implementation, review, integration, merge, and
deployment is documented in [`docs/factory-delivery-lifecycle.md`](docs/factory-delivery-lifecycle.md).

## Monitoring

Typical board commands:

```bash
hermes kanban --board <board> stats
hermes kanban --board <board> list --status running
hermes kanban --board <board> diagnostics
hermes kanban --board <board> watch \
  --kinds completed,blocked,gave_up,crashed,timed_out
```

Check factory control-plane health separately:

```bash
hermes gateway status
~/.hermes/scripts/kanban_factory_recovery.py --board <board> --dry-run
HERMES_FACTORY_BOARD=<board> ~/.hermes/scripts/kanban_review_successor_recovery_cron.py
```

For recurring updates, use a continuity-enabled cron digest and deliver it to a configured gateway home channel. A local CLI/Desktop chat may not have a live cron delivery target.

## Repository layout

```text
skills/scoped-subagent-audits/SKILL.md       bounded audit/review procedure
skills/kanban-reviewer-contract/SKILL.md     typed reviewer role contract
skills/tracker-kanban-reconciliation/SKILL.md  source reconciliation contract
skills/kanban-implementation-workflow/SKILL.md  reusable agent procedure
skills/kanban-factory-operations/SKILL.md       live factory operation and recovery
skills/kanban-factory-operations/references/    runtime drift and stall recovery
skills/kanban-progress-evidence/SKILL.md        evidence and closure accounting
skills/kanban-progress-evidence/references/     closure matrix template
skills/software-factory-recovery/SKILL.md      autonomous recovery procedure
scripts/kanban_factory_recovery.py              deterministic recovery add-on
scripts/kanban_worker_reaper.py                  task-owned terminal worker cleanup
scripts/kanban_review_successor_recovery.py     review packet guard and recursive successor bridge
scripts/kanban_review_successor_recovery_cron.py installed no-agent wrapper
scripts/forgejo_delivery_controller.py          bounded read-only Forgejo delivery observer
examples/project-policy.yaml                    adaptable tracker policy template
examples/forgejo-delivery-overlay.json           anonymized observer overlay template
docs/profile-roles.md                            reusable profile role model
docs/reviewer-role-contract.md                   project-agnostic reviewer boundary
docs/profile-environment-contract.md             profile/worktree environment preflight
docs/tracker-kanban-reconciliation.md            project-agnostic source adapter contract
docs/policy-resolution.md                        project-specific adaptation guide
docs/reviewer-reliability.md                     bounded review and failure recovery
docs/factory-delivery-lifecycle.md               implementation-to-deployment state contract
docs/tracker-adapters.md                         multi-provider tracker adapter guidance
tests/test_skill_frontmatter.py                 lightweight package validation
```

## License

MIT. See [`LICENSE`](LICENSE).
