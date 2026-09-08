# Central Kanban Reporting

## Policy

Kanban workers and individual task cards do not contact Matrix or any other
human channel directly. They write task state, runs, comments, and events to
the board. Workers and task cards do not contact the user directly. A project may expose
an `operator-observer` profile as a read-only evidence/transport boundary, but
that observer is not a second orchestrator and cannot mutate delivery state. One
central orchestrator/reporting path reads that state and reports the aggregate
result through the configured progress digest. The gateway dispatcher remains
the mechanical lifecycle owner; it is not the decision-making bridge.

Task-level `kanban_notify_subs` rows are not part of this policy and should stay
empty. Do not use `notify-subscribe` as a substitute for dispatcher
observability.

## Agent-supervised cron topology

Recurring factory supervision has two distinct layers. The deterministic
controller jobs are evidence producers; one project-level LLM supervisor is the
only scheduled job that interprets the evidence and decides whether an internal
factory action is needed.

Deterministic controllers must be explicit `no_agent: true` jobs with
`deliver: local`. Their stdout is persisted as local job output for later
inspection. It is not a human report, and it must not be sent directly to
Matrix, email, or another human channel. A controller may observe board,
tracker, repository, scheduler-attempt, or scheduler-incident state, but
**Controller output is an observation, never proof**.

The single project-level LLM supervisor consumes every controller output through
`context_from` and keeps its own prior result with `continuity: true`. It must
also have the project workdir, the `terminal`, `file`, and `code_execution`
toolsets, and the `kanban-factory-operations`, `software-factory-recovery`,
`factory-reporting`, and `kanban-progress-evidence` skills. The generic shape is
kept in [`examples/factory-cron-supervision.yaml`](../examples/factory-cron-supervision.yaml).

On every run the supervisor reads all injected controller outputs, including
scheduler attempts and incidents, the prior supervisor result, and fresh live
board, tracker, repository, dispatcher, and scheduler state. It reconciles
those sources before classifying the factory as exactly `ACTIVE`,
`IDLE-BY-GATING`, or `STALLED`. If the live state is `STALLED` and a bounded
safe internal recovery is available, the supervisor performs it and reads back
every mutation before reporting. It must not emit a passive stalled report while
an internal action remains available. Controller output or a successful cron
exit is never a substitute for live readback.

Human delivery is secondary. The supervisor sends only concise verified
progress, completed recovery, or one genuine non-delegable decision. If no
human action is required, it emits `[SILENT]`; it never forwards raw controller
output or raw cron logs. Set one project-approved human delivery target and use
`attach_to_session: true` when that target is conversational.

For a standalone scheduled output that needs one receiving-agent turn,
`deliver: bot-chat` or `deliver: bot-chat:<profile>` is the supported
alternative. Do not double-route the same controller through `bot-chat` and an
existing central supervisor unless that duplication is deliberate and its
different responsibilities are documented.

## Operator clarification bridge

Workers and task cards write state and evidence only. They never ask the user
directly. When a decision crosses a policy-declared non-delegable boundary, the
central orchestrator emits one deduplicated clarification packet containing:

- source item;
- pull request or change request, when applicable;
- Kanban task;
- owner;
- one concrete question;
- recommended default;
- materially different alternatives;
- exact non-delegable reason;
- impact of waiting;
- current evidence;
- next gate;
- response format.

An operator response is a transport handoff only. The transport layer never
changes code, ownership, status, dependencies, or deployment state. The next
orchestrator cycle verifies the response, records the resulting decision, and
then performs the allowed mutation. Unanswered requests are not repeated
unless evidence materially changes. Independent work continues while only the
dependent lane is held.

## Runtime configuration

The user-local Hermes configuration must disable automatic per-task
subscriptions:

```yaml
kanban:
  auto_subscribe_on_create: false
```

Apply it with the Hermes CLI rather than editing secrets or copying the entire
user config into Git:

```bash
hermes config set kanban.auto_subscribe_on_create false
hermes config get kanban.auto_subscribe_on_create
```

The gateway must be restarted from a shell outside the running gateway process
so the watcher reads the new value:

```bash
hermes gateway restart
hermes gateway status
```

The exact `~/.hermes/config.yaml` remains user-local runtime state. This
repository stores the reproducible configuration contract, not credentials or
machine-specific full config files.

## Central digest contract

The project-level supervisor is the central factory progress digest and the
human-facing reporting path. Each run reads the live board and dispatcher
evidence, applies the human-impact filter below, and then reports:

Lead every digest with these sections:

- **Decision**;
- **Durable action**;
- **Progress**;
- **Not progressing**;
- **Why**;
- **Boundary:** internal or external;
- **Owner**;
- **Evidence**;
- **Next gate**.

Keep counts as supporting context. Distinguish the last completed decision from
a newer decision currently in flight, and distinguish both from queued/running
Kanban state, durable tracker/Git state, and independently verified progress.

- board counts and completed work since the previous digest;
- running, review, ready, and todo work;
- verified progress and active work;
- genuine human decisions only: product/design/priority, explicit external
  authorization that the factory cannot obtain, security/payment/credential
  approval, deployment/release approval, or deliberate parked-task steering;
- gateway/dispatcher health and whether `IDLE-BY-GATING` is intentional.

Internal execution failures are not human blockers. Gateway/dispatcher
recovery, worker timeouts, reviewer/tool capability gaps, CuaDriver/TCC state,
provider failures, stale diagnostics, and routine board repair are owned by the
factory. The digest may state that factory recovery is handling internal state,
but must not ask Karsten to restart, respawn, unblock, grant permissions, or
inspect logs.

Parked backlog is not user-blocked work. Never enumerate parked task IDs, ages,
or `stuck_in_blocked` diagnostics in the human digest. At most report
`Parked backlog unchanged; no action required.` Never unblock or dispatch a
parked task.

The external recovery add-on writes one idempotent machine acknowledgement to
each imported parked card. This is durable board evidence that the blocked state
is intentional and prevents the generic stale-blocked diagnostic from treating
the card as abandoned. The acknowledgement never changes status, assignee, or
dispatchability.

When no genuine human decision exists, the digest must say `No human action
required` and report concise verified progress. The digest must not create task
subscriptions or ask a worker to message the user.

## Verification

From the active Hermes profile, verify:

```bash
hermes config get kanban.auto_subscribe_on_create
hermes kanban --board <board> stats --json
hermes kanban --board <board> list --status blocked --json
hermes kanban --board <board> notify-list --json
hermes gateway status
```

Expected state for this policy:

- `auto_subscribe_on_create` is `false`;
- the central digest job is enabled and delivers to its configured origin;
- blocked tasks are described in the digest;
- task-level notification subscriptions are empty;
- one supervised gateway owns dispatcher and digest delivery.
