# Action-first triage admission

This reference defines the reusable admission contract for a coordinator that
observes a live Kanban board with no ready workers. It supplements the normal
supervised dispatcher and project policy; it does not replace either one. A
project maps its own tracker states, terminal outcomes, parent relation, WIP
limits, and permitted mutations.

## Contract

A zero-ready observation is an **action trigger**, not a no-progress result. On
every tick that sees zero ready workers, the coordinator must inspect the
current triage frontier and parent completion before classifying the factory as
idle. It may choose **at most one existing canonical parent-complete lane** and,
when the safety gates below pass, perform **one bounded audited
admission/remediation action**. The action is logical and atomic from the
coordinator's point of view: its durable audit record and readback belong to
that one action, not to a batch.

The coordinator must not emit a status-only no-progress report when a safe
independent triage action exists. A zero-ready count, a stale digest, a
supervised PID, or an empty worker list is not evidence that no action exists.

This contract is project-agnostic. The project policy supplies the state
mapping, canonical identity fields, parent semantics, role/profile mapping,
WIP limits, allowed action types, and external/human gate definitions. If a
mapping or contract is missing, the coordinator fails closed and preserves the
work rather than guessing.

## Terms

- **Zero-ready:** a fresh live-board read contains no task that is both
  dispatchable and eligible for a worker claim. It does not mean that the
  board has no actionable work.
- **Triage frontier:** the existing, non-terminal work that could become
  admissible after parent, contract, capacity, or gate reconciliation. The
  frontier includes the declared `triage` state and any mapped candidates in
  `todo`, `blocked`, or another project-defined holding state.
- **Canonical lane:** an existing task with a stable source/canonical identity
  and an idempotency key. A title similarity, worker summary, or duplicate
  source mention is not canonical identity.
- **Parent-complete:** every declared parent is read back in its
  policy-defined terminal completion state with the required acceptance
  evidence. A missing, ambiguous, stale, or prose-only parent link is not
  completion. A worker summary alone is not parent completion.
- **Safe independent action:** an action that does not depend on a held lane,
  does not cross a protected boundary, fits effective WIP, and can be
  performed with an existing valid contract and a durable readback.

## Mandatory zero-ready tick

### 1. Read the live frontier

Use one bounded, fresh snapshot of the board and effective runtime policy.
Reconcile the complete `ready`, `running`, `review`, `todo`, `blocked`, and
`triage` scans, plus task details needed for parent/child links, latest runs and
events, claims, diagnostics, archived identity records, and global/per-profile
WIP. If the tracker uses different names, use its declared mapping; do not
invent a `triage` state or infer readiness from a title.

The snapshot must distinguish:

- work already claimed or running;
- review work that is a gate rather than implementation work;
- todo/triage candidates and their declared parents;
- parked work and genuine external/human gates;
- malformed or stale contracts, duplicate identities, and internal factory
  defects;
- effective capacity and WIP remaining for the candidate's role.

A digest may identify what to inspect, but it cannot substitute for this live
read.

A liveness or no-progress heartbeat is a **wake-up input**, not a replacement
action. It must never rewrite an already derived `fill`/admission decision into
generic watchdog reconciliation. Only an independently observed watchdog
failure may select watchdog repair. When a fresh no-progress handoff arrives,
the coordinator must preserve the source decision, attempt one bounded product
transition, or prove from the complete frontier that every candidate is gated.
An unverified no-op is retried on the next tick; it is not converted to
`[SILENT]` or a status-only comment.

Project overlays can reuse
`local-variant/coordinator_admission_contract.py` to keep this precedence and
emit a bounded non-selecting progress contract. The helper never chooses an
issue, changes the board, or spawns a worker; those remain LLM-owned decisions
through the supported coordinator path.

### 2. Reconcile parent completion

For each existing canonical candidate, read every declared parent and its
latest relevant run/evidence. Treat a lane as parent-complete only when all
parents satisfy the policy-defined completion condition. Do not promote a
child because a parent title, comment, worker narrative, or old digest says
that it is complete. Preserve a candidate with an unfinished or ambiguous
parent in its current state.

A root lane with no parents is eligible only when the task contract explicitly
identifies it as a root and all other admission checks pass. A missing parent
record or malformed dependency edge is a contract/reconciliation problem, not
implicit parent completion.

### 3. Apply fail-closed filters

Before selecting a lane, exclude candidates that are:

- parked, intentionally held, or explicitly out of dispatch;
- behind an unfinished, missing, or ambiguous parent;
- already claimed, running, in review, or covered by an active equivalent;
- malformed, stale, missing required identity/role/acceptance fields, or
  otherwise not safe to interpret;
- subject to a duplicate or idempotency conflict;
- over the effective global or per-profile WIP limit;
- blocked by a genuine external/human decision or a protected
  signer/credential boundary.

These filters preserve state. They do not authorize an ad-hoc status change,
manual dispatch, direct database mutation, credential workaround, or second
dispatcher.

### 4. Select at most one lane

If more than one candidate survives, use the project's declared deterministic
order (for example, priority followed by canonical key). Select at most one
existing canonical parent-complete lane. Do not create a new lane merely to
fill capacity, do not fan out a batch, and do not mutate the unselected
frontier.

Before acting, re-check that the selected lane is still canonical, parent-
complete, unclaimed, contract-valid, and within global and per-profile WIP.
A concurrent change between the snapshot and action cancels the action; it does
not justify a forced transition.

### 5. Perform one bounded audited action

The permitted logical action is exactly one of the following, as selected by
policy and the observed cause:

- **Admission:** move the selected canonical lane through the supported
  admission transition for the normal dispatcher, preserving parents,
  assignee, retry policy, and WIP accounting. Admission is not a manual worker
  spawn.
- **Internal remediation:** route a factory-owned defect to a keyed remediation
  action, preserving the original lane and its history. Reuse the existing
  remediation task for the exact key when one exists; create at most one
  idempotent remediation record when policy permits and no record exists.

A remediation action is for an internal factory defect such as a missing or
invalid handoff, stale runtime/task contract, routine internal publication or
worker-routing defect, or dispatcher-owned reconciliation failure. It is not a
reason to ask a human to restart, respawn, unblock, or repair routine factory
state. The remediation key must be stable, for example
`triage-remediation:<canonical-lane-key>:<defect-class>`, with no secret or
machine-specific value.

The action record must include, at minimum:

- contract/version and action kind;
- canonical lane key and idempotency/action key;
- parent completion evidence or the remediation cause;
- effective global/per-profile WIP snapshot;
- selected owner, bounded operation, result, and next gate;
- a secret-safe evidence fingerprint.

Use the supported board/coordinator API or command. If a create or mutation
times out, rediscover by the exact idempotency key before retrying. Never retry
blindly, create a duplicate, or turn a readback failure into a second action.
Read back the exact task, status, parents, assignee, retry state, action key,
and audit event before claiming progress. A failed action is still reported as
an attempted bounded action with its cause; it is not a status-only report.

## Gate-specific dispositions

### Parked work

Parked tasks remain untouched. Never unblock or dispatch parked tasks, and do
not treat parked backlog as a human blocker. Leave its status, assignee,
parents, comments, and history intact; report only an aggregate parked-state
observation through the central reporting path.

### Genuine external or human gates

A genuine external/human gate includes an unavailable external authorization,
explicit product/design/priority decision, required release approval, or a
protected signer/credential boundary that the factory is not allowed to cross.
Do not mislabel that lane as an internal factory defect and do not silently
rewrite its contract. Hold only that lane, record the non-secret reason and
next gate, and continue independent work. If no independent lane is safe, use
the central clarification path once; workers and task cards do not request
credentials or contact a human channel directly.

### Malformed or stale contracts

A malformed or stale contract is not admissible. Fail closed, preserve the
original task, worktree/artifact references, claims, evidence, and history, and
do not silently repair identity, parents, role, credentials, or acceptance
criteria. If the defect is owned by the factory and a safe bounded repair is
known, route a keyed remediation and read it back. If identity or ownership is
ambiguous, hold that lane for reconciliation and keep independent lanes
moving; never create a replacement merely because parsing failed.

### Duplicate and idempotency conflicts

Index active and archived canonical identities before any create. An exact
existing key is reused, not duplicated. If two records claim the same canonical
identity or the key cannot be resolved unambiguously, preserve both audit
trails, take no admission action for that lane, and route only the declared
reconciliation/remediation path. A timeout is not proof that a create failed.

### WIP and capacity

Use effective, not merely persisted, global and per-profile WIP. Never bypass
WIP limits, borrow capacity from another profile, or dispatch a second worker
to make the board look active. Leave over-capacity candidates queued and
select no action from that candidate until capacity is genuinely available.

### Protected signer/credential boundaries

A missing signer, credential, or protected capability is a protected signer/
credential boundary, not an internal publication defect. Never print or copy
credentials, inspect secret material, substitute an unapproved identity, or
weaken the boundary. Hold only that lane, emit a secret-safe genuine-gate
record when policy requires it, and continue independent work. The existence
of an unrelated safe lane is not permission to cross this boundary.

## IDLE-BY-GATING and reporting

`IDLE-BY-GATING` is truthful only after **complete ready, running, review, todo,
blocked, and triage scans** prove that no safe action remains. The coordinator
must also record that no existing canonical parent-complete lane is safe, every
remaining candidate has a recorded gate, and no safe independent lane exists.
A zero-ready observation alone never justifies `IDLE-BY-GATING`.

If a safe independent triage action exists, the report must name the selected
canonical lane, the one bounded audited admission/remediation action, its
readback result, and the next gate. A status-only no-progress report is
prohibited in that case. If an internal factory defect remains actionable,
route its keyed remediation when safe; do not disguise it as a genuine human
gate. If no safe remediation is possible because the contract is ambiguous,
report the contract defect explicitly rather than claiming that the factory is
quiet.

Every report separates:

- decision and durable action actually attempted;
- verified progress and exact readback;
- remaining queued, parked, dependency, external/human, protected, malformed,
  duplicate, WIP, or internal-defect state;
- internal versus external boundary, owner, and next observable gate.

## Verification checklist

- [ ] The zero-ready tick read a fresh complete `ready`, `running`, `review`,
      `todo`, `blocked`, and `triage` frontier.
- [ ] Parent links and completion evidence were read back, not inferred from
      prose or a stale digest.
- [ ] At most one existing canonical parent-complete lane was selected.
- [ ] Parked work, genuine gates, malformed/stale contracts, duplicates, WIP
      limits, and protected credentials were preserved.
- [ ] Exactly one bounded admission/remediation action was attempted when safe.
- [ ] Internal factory defects used a keyed, idempotent remediation path.
- [ ] Protected signer/credential lanes were held alone while independent work
      continued.
- [ ] The action, idempotency key, evidence, and exact board mutation were read
      back without secrets.
- [ ] `IDLE-BY-GATING` was used only after all scans proved no safe action and
      no status-only no-progress report replaced an available action.
