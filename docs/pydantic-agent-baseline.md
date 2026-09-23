# Reproducible Hermes baseline for the Pydantic-agent migration

Status: baseline contract and synthetic replay corpus, not a production load test.

This report freezes the identity and replay rules for the Phase 0 comparison
required by the Pydantic-agent factory architecture. The checked-in corpus is
at [`benchmarks/pydantic-agent-corpus.json`](../benchmarks/pydantic-agent-corpus.json).
Every fixture is explicitly synthetic; its numbers exercise the evidence shape
and lifecycle outcomes but are **not observed production measurements**.

## Exact baseline identity

The directional Hermes sample and the migration baseline use this exact route
and source revision:

| Field | Value |
| --- | --- |
| `hermes_profile` | `implementer` |
| `model` | `openai-codex:gpt-5.6-luna` |
| `runtime_revision` | `0c32430ab1243e060f21bab98c109e4f21d0a402` |
| corpus | `pydantic-agent-factory-migration-v1` |
| corpus provenance | `synthetic`, `observed_production_data: false` |

`runtime_revision` is the exact Hermes software-factory revision on which this
baseline was established. A future benchmark run must record a new revision
rather than silently reusing this label.

## Corpus coverage

The corpus has one sanitized fixture for each required lifecycle sample:

| Fixture | Role | Durable outcome |
| --- | --- | --- |
| `fixture-implementation-success` | implementer | `candidate_ready` |
| `fixture-test-failure` | implementer | `failed` |
| `fixture-provider-retry` | implementer | `candidate_ready` after one retry |
| `fixture-timeout` | implementer | `timed_out` |
| `fixture-cancellation` | implementer | `cancelled` |
| `fixture-review-approval` | code reviewer | `APPROVED` |
| `fixture-review-changes-requested` | code reviewer | `CHANGES_REQUESTED` |
| `fixture-review-incomplete` | code reviewer | `REVIEW_INCOMPLETE` |

Each fixture records the following under `metrics`:

- prompt size in characters and estimated tokens;
- model requests and tool-call counts;
- input, output, and cache-read tokens (`cache_tokens` is retained as the
  architecture-compatible alias);
- queue wait, startup, first-model-request, model, tool, and total latency in
  milliseconds;
- peak resident set size in bytes; and
- retry count.

The `durable_outcome` object records a terminal status, task state, reason, and
sanitized summary. Model and tool events are names and counts only; prompts,
credentials, repository contents, live task identifiers, and absolute paths are
not included.

## Side-effect-free replay

Replay is a local JSON operation. It validates the corpus and computes a stable
aggregate; it does not import Hermes, contact a provider, open a network
connection, invoke Git, write files, mutate Factory tasks, or access an external
repository.

```bash
python3 scripts/pydantic_agent_baseline.py \
  validate benchmarks/pydantic-agent-corpus.json

python3 scripts/pydantic_agent_baseline.py \
  replay benchmarks/pydantic-agent-corpus.json
```

The validator fails closed if a fixture is not synthetic, if required evidence
is missing, if event counts disagree with metrics, or if live/external
identifiers appear. Replay output includes the fixture count, outcome counts,
metric totals, maximum peak RSS, and `side_effects: false`.

## Directional smoke evidence

The following values are the known issue-provided smoke samples. They are kept
here for context only. They are not a corpus result, distribution, or
production/load benchmark and must not be read as one:

| Sample | Result |
| --- | --- |
| Hermes `implementer`, `gpt-5.6-luna` | 18.955 s; 274052 KiB maximum RSS; 2 model requests; 22874 input tokens; 20990 cache-read tokens; 40260-character system prompt |
| Hermes behavior note | The one-shot made an unnecessary `kanban_show` call before answering. |
| Minimal PydanticAI 2.48.0, `openai-codex:gpt-5.6-luna` | 2.662 s; 82320 KiB maximum RSS; 1 request; 34 input tokens; 14 output tokens |
| Typed tool/output PydanticAI probe | 5.174 s; 82608 KiB maximum RSS; 2 requests; 214 input tokens; 92 output tokens |

These samples establish why representative replayable evidence is needed; they
do not establish a production throughput or quality improvement.
