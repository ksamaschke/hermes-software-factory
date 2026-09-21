---
name: kanban-review-orchestration
description: "Use for Kanban review orchestration and evidence gates."
version: 1.0.1
author: HEX
license: MIT
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [kanban, review, orchestration, evidence, routing]
    related_skills:
      - kanban-reviewer-contract
      - kanban-factory-operations
      - kanban-progress-evidence
      - kanban-orchestrator
---

# Kanban Review Orchestration

Use this skill when HEX or another parent agent is routing implementation work
through independent Kanban reviews. It defines the orchestrator boundary around
the reviewer contract: route, validate, and reconcile; do not become the
reviewer.

## Trigger

Use when an implementation handoff needs adversarial code/config review, when a
review leaf times out or returns an invalid packet, when fan-in coverage is
missing, when a board shows review work that may be dependency-gated,
misrouted, or represented by stale history, or when a progress digest reports
a board as idle/stalled and "waiting on review" — verify the candidate gate
before accepting that conclusion.

## Core role boundary

HEX/the orchestrator owns decomposition, profile selection, packet construction,
dependency links, WIP/capacity, timeout recovery, tracker mutations, and
lifecycle readback. HEX does **not** perform the adversarial review, replay the
candidate to supply an independent finding, or turn its own code inspection
into review evidence.

Only the configured reviewer profile may emit the review verdict. A completion
verifier may check that the reviewer reached the intended target, stayed within
scope, kept the source read-only, and emitted one terminal outcome. That check
is lifecycle evidence, not a replacement review.

## Required review graph

Build the graph explicitly:

```text
implementation handoff
        |
        v
fresh exact-scope reviewer leaf(s)
        |
        v
bounded fan-in / coverage reconciliation
```

A reviewer leaf is a separate Kanban task. Reassigning an implementation card
to a reviewer, or using a same-card `request-review` run as the final
independent review, is invalid for closure. If a worker puts an implementation
card back into the review lane, reclaim or reclassify that card and create a
fresh reviewer task.

Create true dependencies in the original task creation call. A fan-in task
must depend on every required leaf. Do not create an apparently-ready child and
repair its graph later when the dispatcher could race the mutation.

## Candidate gate before review dispatch

Run this BEFORE building a packet. Routing a reviewer at a candidate that
cannot pass CI wastes a review lane and, worse, can produce an `APPROVED`
verdict on unmergeable code.

1. **Run the repository gate yourself, from a fresh clone.** Not from the
   candidate worktree — a worktree carries untracked files, stale build
   artifacts, and local state that mask packaging defects:

   ```bash
   git clone --no-hardlinks <worktree> "$T/c" && cd "$T/c"
   make test; echo "exit=$?"
   ```

   Check the exit code explicitly. Piping through `tail`/`grep` reports the
   pipeline's status, not the gate's. Run the gate twice to separate a
   deterministic failure from a flake.

2. **Verify the packet's candidate revision still exists.** Compare
   `candidate_commit` in every open review leaf against the branch tip and
   `origin/<branch>`. A packet that names `<sha> plus the uncommitted delta in
   the worktree` is void the moment that delta is committed: the reviewed
   artifact is gone and the leaf can never be satisfied.

3. **Compare timestamps.** If the newest approval predates the newest commit
   on the candidate branch, that approval does not cover the current code, no
   matter what the board status says.

If the gate is red, do not dispatch a review. File a focused implementer card
for the gate failure and let it complete first. Report the red gate as the
real blocker, replacing whatever "waiting for review" story the board tells.

### Gates decide, not digests

A progress digest — including one this factory generated — is a claim, not
evidence. When a digest concludes "idle, waiting on review" or "no human
action required", verify the underlying gate before acting on that
conclusion. A digest can be internally consistent, cite real run ids, and
still be wrong about the next gate because it never executed the build.
Prefer one executed command over any amount of well-formatted board prose.

## Packet gate before dispatch

Reject the packet until it explicitly contains all of these fields:

- `implementation_task` and source issue/PR;
- target repository, absolute worktree, branch, and candidate revision;
- implementer profile and reviewer profile, with vendor-family comparison;
- `review_kind`: `pre_commit` (working-tree delta vs. the base commit) or
  `pre_merge` (merge-base delta vs. the target branch);
- a change manifest: base reference, candidate reference, and changed paths with
  hunk ranges. Never a whole file, directory, glob, module, or topic label;
- one review lens and one bounded acceptance question;
- original acceptance criteria and diff-targeted commands;
- cited implementer/CI gate evidence: command, exit code, run reference, and
  commit. The reviewer never re-runs the full project gate;
- explicit non-goals and live-system boundaries;
- `read_only_source: true`;
- the declared two-tier budget for each adversarial leaf: dispatch hard cap
  (reference 1800s), evidence budget (reference 900s) at which the reviewer must
  return a verdict, and per-command timeout (reference 120s);
- `max_retries: 1` for each adversarial leaf;
- an explicit stop condition, evidence format, and allowed verdicts:
  `APPROVED`, `CHANGES_REQUESTED`, and `REVIEW-INCOMPLETE`.

The packet must not contain implementation instructions such as “TDD first”,
“write the fix”, or “make the production change”. Reviewers may write scratch
artifacts only outside the candidate worktree.

## Durable board gate

Validate the task record as well as the prose. Read back the created task and
require:

- the configured reviewer profile is the assignee;
- the board row has `max_runtime_seconds` equal to the declared dispatch hard
  cap and `max_retries=1`;
- the task has the intended exact scope and candidate;
- the parent/fan-in links are correct;
- the task is not an implementation card in `review`;
- the idempotency key includes the implementation task and review round, not
  only a reusable title.

A prose claim that a retry cap or read-only boundary exists does not override a
missing or contradictory board field.

## Dispatch and recovery

1. Preflight the actual reviewer profile and its resolved skills/tools.
2. Dispatch only through the supervised Kanban reviewer lane.
3. Treat a timeout, crash, wrong target, missing field, missing report, source
   mutation, or scope violation as `REVIEW-INCOMPLETE`, never as approval or a
   product finding.
4. Preserve the failed run and evidence. Create a strict-subset review
   continuation with a new idempotency key; never retry the unchanged packet.
5. Keep review failures on the review path. Never convert a timed-out reviewer
   into an implementation continuation.
6. If implementation rework is required, create a new focused implementer task
   from the reviewer finding. Do not silently edit the review card into an
   implementation card.
7. Run fan-in only after every required leaf is terminal. Fan-in reads the leaf
   handoffs and reconciles coverage; it does not rescan the candidate or issue a
   substitute verdict.

## Closure loop after requested changes

`CHANGES_REQUESTED` is a routing result, not a reason to stop the factory or
hand internal repair coordination to the operator. Continue the bounded loop:

1. Convert each reviewer finding into a narrow implementation acceptance slice
   and a new implementer card. Start from the current remote upstream `main`,
   not a stale local tracking ref, and use a fresh clean clone/worktree so a
   user-owned dirty checkout is never mutated.
2. Verify the implementer commit, exact file scope, focused/full gates, and
   clean worktree. Mark that implementation handoff terminal; do not call a
   same-card `request-review` path when it can route the implementation card to
   a generic reviewer or reuse its run history.
3. Create a fresh reviewer child with the implementation card as a completed
   parent. Revalidate the exact packet, durable budget/one-retry fields,
   read-only boundary, and the configured independent reviewer route before
   dispatch. Only that fresh reviewer verdict can reopen the loop or authorize
   integration.
4. After a fresh `APPROVED`, hand the candidate to a separate integration owner
   for upstream PR creation, CI, branch-policy checks, merge, and merged-commit
   readback. A local branch or worker summary is not upstream delivery.
5. Hand only the verified merged commit to a separate release owner. Back up the
   installed artifact, install atomically from the merged source, and read back
   checksum, mode, compilation/help, and non-mutating smoke evidence. Do not
   install an unmerged worktree.

Pause only for a genuine product decision, external authorization, security or
release approval, or an actual unavailable dependency. Internal task status,
retry bookkeeping, stale refs, worker transport errors, and routine gateway or
board repair remain factory-owned; report them as evidence or blockers, not as
questions for the operator to coordinate.

## Reporting boundary

Report lifecycle facts separately from reviewer findings:

- packet/graph status and configured reviewer lane;
- run outcome, runtime cap, retry field, and source mutation readback;
- reviewer verdicts exactly as emitted;
- fan-in coverage and gaps;
- implementation/rework routing;
- genuine external or human blockers only.

Do not say “independently verified” because HEX inspected a file or because a
worker summary looked plausible. Use `APPROVED`, `CHANGES_REQUESTED`, and
`REVIEW-INCOMPLETE` only for the corresponding reviewer evidence.

## References

Use `references/review-packet-checklist.md` for the compact preflight/readback
checklist and failure-pattern examples.

Use `references/candidate-gate-verification.md` when a board looks idle and
"waiting for review": it records the fresh-clone gate run, the stale
`<sha> + uncommitted delta` candidate-identity trap, clean-checkout fixture
rot after those files get committed, and how to route the repair.

## Leaf lifecycle gap

A review leaf claimed from `source_status=ready` has no `review_requested`
handoff, so it is a **standalone review leaf**, not a same-card review. It must
record `APPROVED`, `CHANGES_REQUESTED`, or `REVIEW-INCOMPLETE` in its completion
summary/metadata and call `kanban_complete` for the leaf itself. The
orchestrator then creates or reuses one bounded remediation/continuation from
that terminal handoff. Only a review with a valid `review_requested` handoff
claimed from `source_status=review` may call `kanban_request_changes`.

If a standalone leaf is already blocked only because
`kanban_request_changes` rejected `source_status=ready`, and its exact-head
verdict is still current, complete the blocked leaf from the existing evidence;
do not re-specify, requeue, or re-dispatch it. Treat the rejected transition as
a factory contract defect, not as `REVIEW-INCOMPLETE`. If the review evidence
itself is incomplete, create one bounded diagnostic successor; do not retry the
same prompt unchanged.

## Pitfalls

- Performing the adversarial review in the parent because the leaf is slow;
- pinning leaves to `<sha> + uncommitted delta`: once that delta is committed
  the reviewed artifact no longer exists and no continuation can ever be
  satisfied. Re-derive the change set from the live merge-base delta rather
  than narrowing the dead packet;
- passing a category-qualified skill path (`software-development/<skill>`) to
  `--skill`: workers resolve bare names, and the qualified form kills the run
  with `Unknown skill(s)` before any review happens;
- putting a full-gate command such as `CI=true make validate` in
  `focused_checks`. A correct reviewer rejects that packet as
  `REVIEW-INCOMPLETE`; cite the gate result instead;
- omitting hunk ranges from the change manifest, or emitting the manifest under
  a key the pre-dispatch guard does not read (it reads `exact_scope`);
- dispatching a reviewer without first running the repository gate from a
  fresh clone, so the lane burns on a candidate that cannot pass CI;
- trusting a progress digest's "next gate" conclusion instead of executing
  the gate;
- reading a gate's result through a pipe and mistaking the pipe's exit code
  for the gate's;
- raising a runtime cap or deleting a failing test to reach green;
- using a same-card implementation review to save a task creation;
- checking only packet prose while the durable board row has a null retry cap;
- reusing review leaves by title across implementation rounds;
- letting a timeout-created continuation inherit the full broad prompt;
- routing a timed-out review to an implementer;
- treating fan-in as permission to rescan or approve uncovered scope;
- creating a second dispatcher to accelerate review pickup;
- treating a reviewer heartbeat, PID, or green focused test as a verdict.

## Completion criteria

This orchestration task is complete only when every requested review scope has a
fresh contract-complete leaf or an explicit preserved gap, every leaf has a
terminal reviewer outcome or an honest `REVIEW-INCOMPLETE`, fan-in coverage is
reconciled, and no HEX-authored inspection is being counted as independent
review evidence.
