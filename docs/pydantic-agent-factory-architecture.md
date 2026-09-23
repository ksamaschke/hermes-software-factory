# Pydantic-agent factory architecture

Status: proposed

## Decision

The factory will stop using a general-purpose Hermes profile as the execution
runtime for every implementation, review, verification, integration, and
release task.

One deliberately small Hermes `factory-orchestrator` profile remains the
operator-facing architecture and decision authority for Slack and other
messaging applications. It communicates with a separate factory control plane
through a narrow typed tool boundary. The factory control plane owns durable
state, policy enforcement, scheduling, claims, leases, workspaces, retries,
evidence, and lifecycle transitions. PydanticAI agents perform only the bounded
reasoning work for which an LLM is useful.

The initial migration preserves the current Kanban task identities, dependency
graph, worktree isolation, run/claim locks, evidence requirements, and delivery
lifecycle. A task route selects either the current `hermes_profile` executor or
a new `pydantic_agent` executor. This per-role switch is the rollback boundary.
There must still be exactly one dispatcher and one claim owner.

## Why change

A Hermes profile is a good interactive general-purpose agent, but it is an
expensive default worker runtime for a software factory. Every fresh worker
inherits a broad system prompt, profile policy, skill catalog, generic tool
schemas, session machinery, and task protocol. Those facilities are useful at
the messaging boundary but most of them are irrelevant to a narrowly scoped
implementation or review leaf.

A local directional smoke test illustrates the size of the opportunity. It is
not a load benchmark and must not be treated as one:

- a trivial current Hermes implementer run took 18.955 seconds, reached 267.6
  MiB maximum RSS, made two model requests, and carried 22,874 input tokens,
  20,990 of which were cache reads;
- the stored Hermes system prompt for that run was 40,260 characters and the
  generic task protocol caused an unrelated `kanban_show` tool call before the
  final answer;
- a minimal PydanticAI 2.48.0 agent using the same Codex subscription path and
  `gpt-5.6-luna` took 2.662 seconds, reached 80.4 MiB maximum RSS, made one
  request, and used 34 input tokens;
- a second PydanticAI probe used one custom function tool and a typed Pydantic
  result successfully in 5.174 seconds with 80.7 MiB maximum RSS.

The measurements prove feasibility and identify avoidable startup/context
cost. Representative implementation and review cards are still required before
claiming a production throughput improvement.

## Goals

1. Keep one Hermes profile as the human-facing messaging and decision bridge.
2. Remove Hermes profile startup, generic prompt, broad tools, and profile skill
   discovery from routine worker tasks.
3. Define each agent in code with short versioned instructions, typed
   dependencies, typed results, and the minimum role-specific tools.
4. Preserve the current fail-closed lifecycle: implementation, independent
   review, completion verification, integration, and release remain distinct.
5. Preserve one dispatcher, atomic claims, leases, heartbeats, retries,
   worktree isolation, immutable run evidence, and readback after mutations.
6. Make model/provider routing configurable per role, including direct
   ChatGPT/Codex subscription access through PydanticAI.
7. Measure prompt size, model calls, latency, memory, retries, and outcome
   quality per run.
8. Support a role-by-role rollback to the existing Hermes executor while the
   migration is being evaluated.

## Non-goals

- Replacing the messaging gateway or its Slack/Telegram/etc. integrations.
- Letting an LLM own task claims, leases, retries, or status transitions.
- Replacing deterministic tests, Git operations, CI, merge policy, or rollout
  controllers with model judgement.
- Collapsing implementation, review, integration, and release into one agent.
- Giving every agent a generic shell, all repository credentials, or direct
  database access.
- Building a public multi-tenant service on one person's subscription
  credentials.
- Migrating the board database before the executor boundary has been proven.

## Target topology

```text
Messaging applications
        |
        v
Hermes gateway + one `factory-orchestrator` profile
        |
        |  six to eight narrow Factory tools over MCP/stdio
        v
Factory control plane
  - policy and decision records
  - task graph and state store
  - scheduler, claims, leases, retries
  - workspace/worktree manager
  - event outbox and evidence store
  - executor routing and capacity
        |
        +-------------------+-------------------+-------------------+
        |                   |                   |                   |
        v                   v                   v                   v
 Pydantic planner    Pydantic implementer  Pydantic reviewer  deterministic
 (read-only DAG)     (write worktree)       (read-only)         verifier/
                                                              integrator/release
        |
        v
Model providers
  - `openai-codex:*` through ChatGPT/Codex subscription
  - ordinary API providers when policy requires them
  - optional official CLI/SDK adapters behind the same executor contract
```

The Hermes profile is not the queue worker and does not need the repository,
shell, GitHub, Kanban, deployment, skill-management, or delegation toolsets. It
owns outcome interpretation, architecture choices, routine adjudication within
policy, and the operator conversation. It expresses those decisions as typed
factory commands, reports verified factory state, and carries genuinely
non-delegable decisions back to the control plane.

## Boundary of the Hermes profile

The profile is `factory-orchestrator`, never an implementation or review role.
Its system prompt contains only:

- the operator communication contract;
- the distinction between durable evidence and liveness;
- which decisions may be delegated and which require an operator;
- how to use the Factory tools;
- the rule that it never edits source, claims a task, or infers completion.

The profile receives a dedicated toolset exposing only:

- `factory_submit` — create an idempotent work request;
- `factory_status` — read aggregate or task-specific state;
- `factory_evidence` — read the exact run, revision, checks, and artifacts;
- `factory_decisions` — list pending non-delegable decisions;
- `factory_record_decision` — record one validated operator decision;
- `factory_cancel` — request safe cancellation with a reason;
- `factory_retry` — request a policy-checked retry or continuation;
- `factory_explain` — return the causal boundary and next owner.

The first transport should be an MCP server launched over stdio. Hermes already
supports profile-scoped MCP servers, stdio avoids another network listener, and
the server can expose stable JSON schemas without granting the profile direct
access to the state database. A later remote deployment may place the same API
behind authenticated transport without changing the tool contract.

Every mutating command carries an idempotency key and returns a readback object,
not only an acknowledgement. The bridge never reads or writes the factory
SQLite/PostgreSQL store directly.

## Factory control plane

The control plane is deterministic Python code. It owns:

- canonical source item and repository identity;
- task DAG and dependency promotion;
- role and executor selection from project policy;
- atomic claim, run ID, claim lock, lease, heartbeat, timeout, retry, and
  cancellation;
- worktree creation, path binding, cleanliness checks, and cleanup policy;
- model/provider concurrency and backpressure;
- validation of typed agent results;
- immutable evidence and event append;
- lifecycle transitions and fan-in;
- tracker, pull-request, CI, merge, artifact, and release adapters;
- an outbox for operator notifications and decision requests.

An agent cannot call `complete`, `block`, `reassign`, `create_child`, `merge`,
or `deploy`. It returns a proposal/result object. The controller validates that
object against the active run, current lease, policy, workspace diff, test
results, and external readback before applying a transition.

### Initial state backend

Phase 1 reuses the current Kanban database and semantics through a dedicated
repository interface. No Pydantic agent receives SQL access. This minimizes the
migration surface and makes executor A/B routing possible on the same task
model.

The interface must not expose Hermes implementation details to agent code:

```python
class TaskRepository(Protocol):
    def claim(self, task_id: str, executor_id: str) -> ClaimedRun: ...
    def heartbeat(self, run: RunIdentity) -> Lease: ...
    def append_event(self, run: RunIdentity, event: FactoryEvent) -> None: ...
    def finish(self, run: RunIdentity, outcome: ValidatedOutcome) -> TaskState: ...
```

A later phase may move the state to a factory-owned SQLite or PostgreSQL schema.
That is a separate migration with dual-read verification; it is not required to
remove Hermes worker overhead.

## Executor contract

The existing dispatcher currently treats `assignee` as a Hermes profile and
spawns a fresh Hermes process. The new dispatcher route must separate logical
role from execution backend:

```yaml
roles:
  implementer:
    executor: pydantic_agent
    agent: implementer-v1
  code_reviewer:
    executor: pydantic_agent
    agent: reviewer-v1
  integration_operator:
    executor: deterministic
    handler: source-integration-v1
```

A Pydantic executor receives only a validated task envelope and a bound
workspace. It does not rediscover the board or profile configuration:

```python
class TaskEnvelope(BaseModel):
    task_id: str
    run_id: str
    role: Literal['planner', 'implementer', 'reviewer']
    repository: RepositoryIdentity
    workspace: WorkspaceIdentity
    base_revision: str
    candidate_revision: str | None
    objective: str
    acceptance: list[AcceptanceCriterion]
    constraints: TaskConstraints
    allowed_commands: list[CommandSpec]
    evidence: list[EvidenceRef]
    deadline: datetime
```

The executor emits events while running and exactly one terminal result. The
result is role-specific and validated before any state transition:

```python
class ImplementationOutcome(BaseModel):
    status: Literal['candidate_ready', 'needs_decision', 'failed']
    summary: str
    changed_paths: list[str]
    candidate_revision: str | None
    tests: list[TestEvidence]
    assumptions: list[str]
    blockers: list[Blocker]
    next_gate: str

class ReviewOutcome(BaseModel):
    verdict: Literal['APPROVED', 'CHANGES_REQUESTED', 'REVIEW_INCOMPLETE']
    candidate_revision: str
    reviewed_scope: list[ChangedPath]
    findings: list[ReviewFinding]
    evidence: list[EvidenceRef]
    mutation_detected: bool
```

The controller independently recomputes changed paths and repository state. An
agent's claimed commit, test, or clean worktree is never accepted without
readback.

## Agent composition

PydanticAI agents are long-lived Python objects reused by a worker process, but
each run receives fresh dependencies and no prior task message history. This
amortizes imports, provider setup, and tool schema construction without leaking
conversation state between repositories or tenants. Workers are recycled after
a configured number of runs or resource threshold.

Use direct PydanticAI capabilities and narrowly selected Pydantic AI Harness
components. Do not start with the complete generic `Coder()` capability, because
that would recreate the broad-tool problem in another framework.

### Planner

Purpose: produce a bounded typed task DAG and identify missing decisions.

Visible capabilities:

- read-only source item and repository metadata;
- bounded file tree/search/read;
- project policy and prior decision records;
- no shell, write, tracker mutation, or task creation tool.

Output: a `PlanOutcome` containing tasks, real dependencies, role, acceptance,
non-goals, and decision requests. The planner proposes; it does not acquire the
orchestrator's architecture authority. The controller validates limits and may
accept a routine plan only when an existing project policy or recorded
orchestrator decision already delegates that exact choice. Otherwise it holds
the proposal for `factory-orchestrator` adjudication before creating the graph
transactionally.

### Implementer

Purpose: produce the smallest tested candidate in one isolated worktree.

Visible capabilities:

- workspace-rooted read/search;
- patch/write/delete only below the bound worktree and outside protected paths;
- deterministic Git status/diff helpers;
- an allowlisted test runner with command and wall-clock budgets;
- optional on-demand documentation lookup when project policy enables it.

It does not receive board, messaging, merge, release, credential-management, or
arbitrary infrastructure tools. Network access is denied unless an explicit
project tool provides a bounded operation.

### Reviewer

Purpose: independently answer one acceptance question for an immutable change
packet.

Visible capabilities:

- read-only worktree and exact diff/hunk access;
- implementer/CI evidence lookup;
- bounded diff-targeted checks;
- scratch evidence outside the source tree.

It has no source-write, patch, commit, task, tracker, merge, or deployment
capability. The controller snapshots source state before and after the run;
mutation makes the result `REVIEW_INCOMPLETE`.

The reviewer route remains independently configurable by provider/vendor
family. Moving to PydanticAI must not silently remove independent review.

### Verifier, integration, and release

Prefer deterministic code for:

- test execution and exit-code capture;
- changed-path and commit verification;
- required-check evaluation;
- pull-request creation/readback;
- policy-gated merge;
- artifact/GitOps publication and rollout readback.

An LLM may diagnose a failed gate or prepare a proposal, but it does not decide
that a deterministic gate passed.

## Prompt and tool budget

Each agent prompt is a short versioned artifact reviewed like source code. It
contains role invariants, stop conditions, and output semantics, not generic
Hermes operation manuals. Repository instructions and specialist knowledge are
loaded only when relevant.

Initial design targets, to be validated rather than presented as guarantees:

- fewer than 2,500 static input tokens per worker role before task data;
- no more than 12 model-visible tools per role;
- no model request for board orientation, profile discovery, or status lookup;
- tool results are bounded and summarized before re-entering model context;
- stable prompt/tool prefixes for provider prompt caching;
- every model and tool call emits usage, latency, and outcome telemetry.

If a role needs a larger catalog, use PydanticAI Tool Search or a purpose-built
on-demand capability rather than injecting every schema on each request. Code
Mode may be evaluated later for high-volume read-only batching, but it is not a
Phase 1 dependency.

## Codex subscription authentication

PydanticAI provides the `openai-codex` provider specifically for using a
ChatGPT/Codex subscription. A development setup can run `codex login` once and
then construct:

```python
agent = Agent('openai-codex:gpt-5.6-luna')
```

The default provider reads `~/.codex/auth.json` or `$CODEX_HOME/auth.json`.
Because that source is read-only and refreshes otherwise live only in process,
the factory must not let many ephemeral workers race on one stale token file.
Production uses one application-owned `OpenAICodexCredentialSource` per account
with atomic load/save, mode `0600` or a secret manager, and a cross-process lock
around refresh. Credentials never enter task envelopes, prompts, events, logs,
telemetry, or artifacts.

Subscription use is restricted to trusted internal workers and remains subject
to the account's limits and OpenAI agreement. The controller enforces a small
provider concurrency cap, records rate-limit/backoff events, and queues work
instead of spawning unbounded retries. An ordinary API provider and the
official Codex SDK remain configurable fallbacks.

The Pydantic integration is maintained by Pydantic; OpenAI officially documents
ChatGPT authentication for Codex clients and recommends API-key authentication
for general programmatic CI/CD. This distinction must remain visible in the
risk register.

## Runtime and isolation

A worker process may keep several immutable agent definitions and provider
clients warm. Runs remain isolated through:

- fresh dependency objects and empty message history;
- a unique worktree and scratch directory;
- path-safe filesystem tools rooted at that worktree;
- command allowlists, sanitized environments, process-group termination, and
  per-command timeouts;
- per-run cancellation and lease checks;
- separate credentials and process pools for roles with different authority;
- worker recycling after configurable run count, memory growth, or failure.

The model does not receive raw environment variables. Command tools strip
credentials from output and reject background processes, listeners, service
stacks, containers, and other actions forbidden by the active project policy.
Execution that requires a service, container, deployment, or authenticated
environment runs through the project's approved CI or remote sandbox adapter.

## Scheduling and concurrency

The controller schedules by logical role and provider capacity, not by Hermes
profile process count. Capacity keys include:

- executor kind and agent version;
- provider/account/model;
- repository/worktree write conflict domain;
- role authority;
- external CI or sandbox capacity.

A claimed task stays owned by one run until terminal transition or lease expiry.
A provider timeout does not release the claim immediately; the controller first
cancels the model/tool work, records the cause, and then applies retry policy.
Rate limiting creates backpressure, not duplicate workers.

The initial Codex-subscription route should start at one concurrent implementer
and one concurrent read-only agent, then be raised only from representative
measurements. Subscription limits are not equivalent to an API throughput SLA.

## Observability

Use OpenTelemetry-compatible PydanticAI instrumentation plus factory-owned
structured events. Every run records:

- task, run, role, agent version, prompt revision, and executor kind;
- provider/model route without credentials;
- queue wait, startup, model, tool, and total latency;
- model request count, input/output/cache tokens when supplied;
- tool name, duration, bounded outcome, and exit code;
- peak worker RSS and worker recycle reason;
- retries, rate limits, cancellation, and lease events;
- candidate revision, changed paths, tests, review verdict, and next gate.

Raw prompts, source content, tokens, cookies, and command environments are not
exported by default. Tracing must keep HTTP body capture disabled unless an
explicit scrubbed diagnostic mode is approved.

The human-facing Hermes profile reads summarized, verified factory state. It
must not infer progress from worker PIDs, model responses, or telemetry alone.

## Project policy changes

Project policy moves from profile names to logical roles plus executor routes.
A compatibility section retains old profile mappings during migration:

```yaml
runtime:
  control_plane: factory
  transport_profile: factory-orchestrator
  tool_transport: mcp_stdio

roles:
  planner:
    executor: pydantic_agent
    agent: planner-v1
    model: openai-codex:gpt-5.6-luna
    max_in_progress: 1
  implementer:
    executor: pydantic_agent
    agent: implementer-v1
    model: openai-codex:gpt-5.6-luna
    max_in_progress: 1
  code_reviewer:
    executor: pydantic_agent
    agent: reviewer-v1
    model: null
    vendor_family: null
    max_in_progress: 1
  completion_verifier:
    executor: deterministic
    handler: completion-verifier-v1
  integration_operator:
    executor: deterministic
    handler: source-integration-v1
  release_operator:
    executor: deterministic
    handler: release-v1

compatibility:
  fallback_executors:
    implementer:
      executor: hermes_profile
      profile: implementer
    code_reviewer:
      executor: hermes_profile
      profile: reviewer
```

Unknown agents, handlers, models, or executor kinds fail configuration loading.
They are never silently routed to the messaging profile.

## Package layout

The new runtime should be an installable package, separate from reusable skill
documents:

```text
runtime/
  pyproject.toml
  src/software_factory/
    api/mcp_server.py
    api/contracts.py
    control/controller.py
    control/policy.py
    control/events.py
    state/repository.py
    state/hermes_kanban.py
    dispatch/scheduler.py
    dispatch/executors.py
    dispatch/pydantic_executor.py
    agents/contracts.py
    agents/planner.py
    agents/implementer.py
    agents/reviewer.py
    capabilities/filesystem.py
    capabilities/git.py
    capabilities/test_runner.py
    auth/codex.py
    integrations/tracker.py
    integrations/source_control.py
    integrations/ci.py
  prompts/
    planner-v1.md
    implementer-v1.md
    reviewer-v1.md
  tests/
```

The runtime has no dependency on a Hermes profile directory. Only the MCP
bridge configuration belongs to the `factory-orchestrator` profile.

## Migration plan

### Phase 0 — freeze contracts and establish a baseline

- Capture representative implementation and review cards, including success,
  test failure, provider retry, changes requested, timeout, and cancellation.
- Record current Hermes prompt size, model/tool calls, wall time, RSS, retries,
  and durable outcome for each card.
- Version the current task envelope, evidence, and lifecycle contracts.

Exit gate: the benchmark corpus and current results are reproducible without
changing live task state.

### Phase 1 — vertical implementer slice

- Add the runtime package, task/result models, provider factory, safe filesystem,
  Git, and test capabilities.
- Add `pydantic_agent` as a dispatcher executor while retaining one dispatcher,
  existing claims, leases, worktrees, and Kanban transitions.
- Implement `implementer-v1` with direct Codex subscription authentication.
- Route only explicitly opted-in canary tasks; all others remain on the Hermes
  executor.
- Validate the result independently before transition.

Exit gate: canary implementation cards survive success, failure, timeout,
cancellation, restart, and stale-lease scenarios without duplicate execution or
state divergence.

### Phase 2 — independent review and completion verification

- Add immutable `ReviewPacket` and `ReviewOutcome` contracts.
- Add a read-only reviewer process/tool boundary and source mutation check.
- Preserve provider-family policy and bounded fan-out/fan-in.
- Move completion verification to deterministic code.

Exit gate: approval, changes requested, incomplete review, crash, and mutation
cases produce the same or stricter durable gates as the existing workflow.

### Phase 3 — messaging bridge and planner

- Create the minimal `factory-orchestrator` Hermes profile.
- Expose the typed Factory MCP tools over stdio.
- Move operator submissions, status, evidence, cancellation, and decisions to
  that API.
- Add the read-only planner only after graph validation is deterministic.

Exit gate: the messaging profile can operate and explain the factory without
repository, shell, direct Kanban, or worker-profile tools.

### Phase 4 — deterministic integration/release and cutover

- Move integration, CI/host review, merge, artifact, and release transitions to
  deterministic adapters with readback.
- Run a defined dual-route comparison window.
- Make Pydantic executors the default only after acceptance gates pass.
- Retain the Hermes executor fallback for a bounded rollback period, then remove
  worker profile requirements from the primary policy schema.

Exit gate: no routine task launches a Hermes worker profile; the sole Hermes
profile is the messaging/operator bridge.

## Performance evaluation

Compare the two executors on the same task classes and model strength. Report
distributions, not one favorable sample:

- queue-to-first-model-request latency;
- total wall time and p50/p95;
- model request and tool-call counts;
- static and total input tokens, including cache reads;
- output tokens and context compactions;
- peak RSS and CPU time per completed task;
- retry/rate-limit/timeout frequency;
- candidate acceptance, test success, review findings, rework count, and escaped
  defects;
- tasks completed per subscription window and per worker-hour.

A performance win is accepted only if lifecycle correctness and review quality
are not worse. Initial acceptance targets:

- at least 80% lower static worker prompt than the measured Hermes baseline;
- no generic board/profile orientation model call;
- at least 50% lower worker-process peak RSS on trivial and bounded tasks;
- materially lower queue-to-first-useful-action latency;
- no increase in duplicate runs, invalid transitions, review escapes, or
  unrecoverable tasks.

These are migration gates, not promised production results.

## Failure and rollback design

- Executor selection is recorded on each run and never changes mid-run.
- Retrying may select a different backend only through a new run and an explicit
  policy decision.
- If Pydantic provider authentication fails, the controller blocks only that
  route, preserves the task, and applies configured fallback policy; it never
  copies credentials into a prompt or silently bills an API key.
- If the MCP bridge is unavailable, workers continue and events accumulate in
  the outbox; only operator commands are temporarily unavailable.
- If the Hermes messaging profile is unavailable, the factory continues all
  delegated work and holds only genuinely non-delegable decisions.
- If the new executor is disabled, existing tasks remain in the same state store
  and new runs route to the compatibility Hermes profile.
- Dirty or valuable worktrees are quarantined, never deleted as retry cleanup.

## Risks and mitigations

### Rebuilding a generic agent in PydanticAI

Mitigation: enforce per-role prompt and visible-tool budgets; introduce
capabilities only from measured need; review prompt/tool diffs like source code.

### Credential refresh races

Mitigation: one credential source per account, atomic persistence, process lock,
no secret-bearing telemetry, and bounded provider concurrency.

### Long-lived process state leakage

Mitigation: immutable agent definitions, fresh dependencies and history per run,
role-separated pools, workspace-rooted tools, and scheduled worker recycling.

### Loss of Hermes lifecycle safety

Mitigation: keep lifecycle mechanics in deterministic control-plane code and
reuse current Kanban semantics first. Replace only the executor at Phase 1.

### Provider-specific coupling

Mitigation: model/provider factories and an executor protocol; keep ordinary API
and official SDK adapters available. Typed task/result contracts remain provider
neutral.

### Faster but lower-quality output

Mitigation: compare real acceptance, review, and rework outcomes; preserve
independent review; do not accept latency/RSS gains alone.

### Two dispatchers claim the same work

Mitigation: extend the one existing dispatcher or replace it atomically. Never
run a parallel polling claimant during migration.

## Required decisions before implementation

The proposal intentionally leaves only these project-level choices open:

1. Which repository and deployment unit will own the runtime package?
2. Is the first state adapter the current Hermes Kanban database, or a copied
   compatibility database used only for canaries?
3. Which independent reviewer provider is available during the Phase 2 canary?
4. Where will the application-owned Codex credential source be stored and
   locked in the approved runtime environment?
5. Which representative cards constitute the benchmark corpus and what quality
   regression threshold is acceptable?

The recommended defaults are: this repository owns `runtime/`; Phase 1 uses the
current Kanban repository interface with explicit canary routing; Codex is the
implementer provider; the independent reviewer remains on the currently
approved non-OpenAI route; credentials use the environment's secret store; and
cutover requires no lifecycle correctness regression.

## References

- PydanticAI agents: <https://pydantic.dev/docs/ai/agents/>
- PydanticAI OpenAI Codex provider: <https://pydantic.dev/docs/ai/models/openai-codex/>
- Pydantic AI Harness: <https://pydantic.dev/docs/ai/harness/>
- PydanticAI durable execution: <https://pydantic.dev/docs/ai/durable_execution/>
- OpenAI Codex authentication: <https://developers.openai.com/codex/auth>
- OpenAI Codex SDK: <https://developers.openai.com/codex/sdk>
- Hermes MCP: <https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp>
