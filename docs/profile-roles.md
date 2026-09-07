# Profile roles

Hermes profiles are reusable role identities. A Kanban board chooses a profile through the task's `assignee`; the profile is not manually pointed at a repository.

## Orchestrator

The orchestrator is the default operating and architecture authority between
desired outcomes and worker execution. Within standing delegation and declared
project policy, it makes the technical and operational decisions needed to
achieve the result, even when the operator has not specified a preferred
implementation.

It owns:

- translating intent into an outcome, task graph, and real dependencies;
- architecture and cross-component interface choices;
- ownership, sequencing, WIP, and dependency resolution;
- remediation and recovery of stale, duplicate, deadlocked, abandoned, or
  failed work;
- implementer, reviewer, QA, and release routing plus findings adjudication;
- source-tracker and Kanban mutations through their declared boundaries;
- release and rollout sequencing within project policy;
- choosing the next safe phase and keeping independent work moving.

The orchestrator drives the other roles without collapsing them:

- the gateway dispatcher performs mechanical promotion, claims, worktrees,
  heartbeats, retries, and reclaim;
- implementers make source and test changes;
- reviewers independently inspect results and remain read-only;
- completion verifiers check evidence and transitions;
- release operators perform policy-controlled release actions.

The orchestrator does not need operator approval for routine technical choices
or because a preference is missing. It does not implement source changes itself;
it directs the bounded workers that do so.

### Orchestrator guardrails

- repository rules, project policy, and explicit forbidden mutations remain
  constraints on every decision;
- do not bypass independent review or deployment policy;
- preserve useful dirty or in-progress work and its history;
- do not take destructive or irreversible action without rollback or explicit
  approval;
- hold an action whose safety-critical inputs are undefined and use the central
  clarification path;
- do not cross production, security, privacy, legal, credential, customer,
  cost, or data-retention boundaries without the required approval;
- keep one supervised dispatcher owner.

Recovery is part of orchestrator authority. The orchestrator chooses whether to
replan, split, reassign, requeue, unblock, replace, or retire work. Automated
cleanup remains limited by the guardrails above; stale state is not permission
to discard useful work or silently reset history.

### Decision ladder

For every selected issue or locked lane, the orchestrator:

1. binds the canonical source item, repository and PR/head/base, task/run/owner,
   worktree, and deployment state;
2. diagnoses the observed cause, or states the current uncertainty;
3. chooses the next phase or corrective action;
4. assigns ownership and dependencies and names acceptance and rollback/fallback;
5. performs the smallest adequate durable action;
6. reads back the exact mutation;
7. preserves the prior completed decision while newer work is in flight;
8. reports the decision and next gate separately from liveness and inventory.

Readback and evidence verify the result after the decision; they do not return
routine decision-making to the operator.

### Typed context and evidence boundary

The decision input is the `factory.decision.v1` envelope. It keeps the runtime
`execution_mode` separate from the selected `profile_name`, and carries canonical
source-item, phase, input-identity, current-task/run, blocker-fingerprint,
parent-completion, and typed scheduler/worker/source/review evidence. A profile
name is an identity and routing lookup, not a permission grant; taskless
board-wide authority and bounded worker authority remain different modes.

The model must use the envelope and read-only observations rather than title/body
heuristics, copied history, or unbounded log material. Effective prompt and skill budgets are
measured before invocation; optional skills are omitted or trimmed, while the
identity/evidence contract is never truncated. Contradictory identities,
statuses, candidates, run IDs, or evidence references fail closed. A null current
run is not a new execution: it represents not-started, reused, or held work.
Repeated unchanged blockers stay quarantined, verified resolution/new-contract
admission allocates one run, and independent ready lanes remain eligible. Profile
continuity is scoped to the same source/phase/input identity, and worker shared
memory writes are disabled or isolated. A denied tool or capability is typed
evidence: choose a supported permitted tool or surface the exact external gate;
do not vary a denied command or lower its approval boundary.

The complete role boundary is defined here. Other runtime and skill documents
should reference this contract rather than narrow the orchestrator to routing.

## Implementer

The implementer profile is write-capable and works in an isolated worktree:

- writes a failing test first;
- implements the smallest scoped change;
- runs the project gate;
- records exact evidence;
- hands the result to review.

One generic implementer profile can serve multiple boards. Create separate implementer profiles only when tools, credentials, memory, safety policy, or write permissions differ. Model strength can usually be selected with a task-level override.

## Code reviewer

The code reviewer is separate from the implementer and should use an independent model/vendor family. It receives a fresh typed review packet, not an implementation card with a new assignee.

The packet names the review kind (`pre_commit` or `pre_merge`), the candidate commit, a change manifest of changed paths with hunk ranges, one acceptance question/lens, diff-targeted checks, cited gate evidence, explicit non-goals, `read_only_source: true`, the declared runtime budgets (dispatch hard cap, evidence budget, per-command timeout), `max_retries: 1`, and a stop condition. The reviewer may read and test the candidate and write scratch evidence outside the source worktree.

The reviewer does not implement fixes, edit source, file tracker issues, create Kanban children, reassign unrelated work, merge, push, deploy, or mutate live infrastructure. A timeout, crash, wrong target, scope violation, mutation, or missing evidence is `REVIEW-INCOMPLETE`, never approval.

A same-family or unknown-family review is `REVIEW-INCOMPLETE` until an independent
final review exists. `PRELIMINARY` may describe the evidence qualifier but is
not a terminal verdict. See [`docs/reviewer-role-contract.md`](reviewer-role-contract.md).

## Completion verifier

A completion verifier checks review coverage, acceptance evidence, and board
transitions; it does not replace the code reviewer and it does not merge or
release the candidate. Its terminal handoff is evidence for the next lifecycle
stage, not delivery completion.

## Integration operator

The `integration_operator` is a separate, project-policy-controlled role. It
consumes a verified implementation and independent review, then owns the
source-control boundary:

- creates or locates the pull request in the declared source repository;
- verifies base branch, head branch/commit, title/body, and changed files;
- obtains the required host review and CI results;
- merges only when policy allows it, then reads back the merged commit;
- records the external PR/merge handles and the next release gate.

A Kanban `APPROVED` verdict is not a pull-request review, and a pushed branch is
not a merge. If this owner or its external repository capability is missing,
integration remains incomplete.

## Tracker/orchestrator operator

The orchestrator/tracker operator creates continuations, adjudicates findings,
files source-tracker issues, routes rework, and creates the integration and
release handoffs. It does not implement source changes, perform the independent
review, or infer downstream completion from a worker summary.

## QA/UI

A `qa-ui` profile is optional. Use it for native/browser acceptance that cannot
be proven by headless tests. It should not rebuild or launch a desktop shell for
every unit test. Keep full-app smoke cards separate and time-boxed.

## Release/GitOps

A `release_operator` profile is separate from integration and is
project-policy-controlled. It consumes a read-back merged commit, prepares the
approved release artifact or GitOps change, and verifies publication and
post-action controller/data-plane state. It must not bypass the project's
declared deployment controller or mutate production directly when policy
forbids that. If deployment mode is `unspecified`, it records the policy hold
instead of claiming deployment complete.

See [`docs/factory-delivery-lifecycle.md`](factory-delivery-lifecycle.md) for
the complete state and evidence contract.
