---
name: kanban-review-orchestration
description: "Use for Kanban review orchestration and evidence gates."
version: 1.3.0
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

The factory supports two review lifecycle models and must never mix them:

- **same-card review** — the implementation task has a durable
  `review_requested` handoff, then a fresh independent reviewer run/profile
  claims that same task from `source_status=review`. The implementer's
  request-review run is not review evidence; the later reviewer run is.
- **standalone review leaf** — a separate review task is claimed from
  `source_status=ready` and carries no same-card handoff.

Choose one model before dispatch. Do not create a redundant standalone leaf for
a valid same-card review, and do not treat a standalone leaf as same-card merely
because its body says `request_changes`.

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
6. If implementation rework is required from a standalone verdict, consume the
   Native Boundary remediation receipt through the scheduled recovery path. Do
   not silently edit the review card or manually create a second successor.
7. Run fan-in only after every required leaf is terminal. Fan-in reads the leaf
   handoffs and reconciles coverage; it does not rescan the candidate or issue a
   substitute verdict.

## Closure loop after requested changes

`CHANGES_REQUESTED` is a routing result, not a reason to stop the factory or
hand internal repair coordination to the operator. Continue the bounded loop:

1. Classify the durable claim. For a same-card `CHANGES_REQUESTED`, use only the
   native `kanban_request_changes` rework transition on the original task; do
   not create a successor implementer task.
2. For a standalone `CHANGES_REQUESTED`, require the current terminal native
   remediation receipt and let `kanban_review_successor_recovery.py --apply`
   consume only the active coordinator's receipt, preserve every direct parent,
   replace only the unchanged child frontier, and archive the old leaf after
   readback. Do not create a parallel manual card. The implementer then starts
   from current remote upstream `main`, not a stale local tracking ref, in a
   fresh clean clone/worktree so user-owned dirty state is never mutated.
3. The immutable successor receipt remains review-gated even if its body is
   edited. Its exact implementation run requests a distinct reviewer with only
   `candidate_commit`, `review_remediation_handoff_key`, and
   `scope_manifest_sha256`; the native boundary validates these against the
   current repository, branch, base, candidate, and changed-path manifest.
4. Verify the implementer commit, exact file scope, focused/full gates, and
   clean worktree. Route exactly one fresh independent review for the new
   candidate; never reuse the old leaf or its verdict.
5. After a fresh `APPROVED`, hand the candidate to a separate integration owner
   for upstream PR creation, CI, branch-policy checks, merge, and merged-commit
   readback. A local branch or worker summary is not upstream delivery.
6. Hand only the verified merged commit to a separate release owner. Back up the
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

## Native standalone-leaf lifecycle

A standalone leaf claimed from `source_status=ready` must never call
`kanban_request_changes`. Exact `APPROVED` calls `kanban_complete` once with
`review_outcome: APPROVED` and the exact `candidate_commit`; Native Boundary
`1.0.19` requires an exact positive current run ID from the native active
reviewer profile, re-reads the strict packet, immutable claim receipt, resolved
worktree/Git-dir identity, repository, branch, base, candidate, and changed-path
manifest before release. A missing/string/float/stale run ID, role collision,
self-attested, foreign, malformed, wrong-repository, or wrong-head approval
remains blocked and cannot release children.

For exact `CHANGES_REQUESTED`, the reviewer calls `kanban_block` once with
`kind=dependency` and a reason beginning
`STANDALONE_REVIEW_CHANGES_REQUESTED:`. The native transaction verifies the
current ready-claimed run, independent reviewer, immutable packet, exact local
HEAD, finding, coordinator identity, and current direct dependency frontier;
it terminally closes the run and inserts one immutable SQLite outbox receipt.
`REVIEW-INCOMPLETE` never uses that prefix and remains gated for coordinator
adjudication.

The scheduled recovery add-on verifies that the imported runtime carries the
same Native Boundary contract and consumes only receipts owned by its native
active profile. Each receipt has its own `BEGIN IMMEDIATE` transaction and
readback, so one malformed receipt does not suppress unrelated current work. It
revalidates the immutable packet/worktree receipt and complete direct-parent
and child frontier, uses a full-digest reserved idempotency key, creates or
reuses exactly one successor with the complete parent set, moves only the
unchanged child frontier, records an immutable successor-body digest and review
gate, reads the graph back, archives the old leaf, and marks the receipt applied.
The rejected leaf remains in the canonical sticky
`blocked/changes_requested` terminal state and cannot be recomputed or claimed
again. A remediation successor cannot complete until its exact implementation
run requests a distinct reviewer with candidate, handoff, and changed-path
manifest metadata and that exact review run returns `APPROVED`. Concurrent or
repeated consumption is idempotent. Any frontier change, identity collision,
missing function, or malformed state fails closed without duplicate successor,
archival, or downstream release. The coordinator never manually duplicates
this native path.

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
