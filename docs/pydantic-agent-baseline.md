# Hermes baseline and synthetic replay contract

Status: checked-in observed smoke evidence plus a strict synthetic future-comparison corpus; not a production load benchmark.

The eight fixtures in [`benchmarks/pydantic-agent-corpus.json`](../benchmarks/pydantic-agent-corpus.json)
are **synthetic test vectors**. Their numbers exercise the evidence shape and
lifecycle contracts only. They are not measurements. The separately captured,
sanitized observed Hermes evidence record
[`benchmarks/hermes-baseline-observed.json`](../benchmarks/hermes-baseline-observed.json).

## Synthetic corpus

The corpus covers one fixture for each required lifecycle scenario:

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

Every fixture is marked `synthetic: true`, uses a `fixture-<scenario>` ID, and
has a task, repository, and workspace name tied to that fixture. No live task ID,
repository URL, path, prompt, credential, or repository content is accepted.
Timeout and cancellation contain explicit causal events and terminal transitions.
Review fixtures contain immutable-packet evidence, checks, mutation status, and
finding counts.

The validator uses explicit allowlisted object schemas recursively. It rejects
unknown fields, normalized secret-key variants such as `apiKey`, `access_token`,
and `authorization`, arbitrary URI schemes, absolute/home/parent paths,
credential-like values, external repository references, live-looking IDs, event
count/latency drift, and inconsistent terminal reason/state pairs.

## Deterministic replay

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

Replay output is explicitly marked:

```json
{
  "provenance": {
    "kind": "synthetic_test_vectors",
    "synthetic": true,
    "observed": false,
    "metrics_are_measurements": false
  },
  "side_effects": false
}
```

## Observed Hermes smoke capture

Capture is deliberately separate and opt-in. It runs one bounded foreground
Hermes one-shot with the `implementer` profile, the qualified model below, the
fixed benign phrase `Reply with exactly: Hermes baseline OK.`, and the empty
built-in `bot_room` toolset. Hermes runs in a temporary non-repository directory;
standard output and error are discarded; the selected `--usage-file` counters
are read and then discarded except for sanitized numeric values. The command
has a 120-second wall-clock bound and writes the observed record as its only
intentional side effect:

```bash
python3 scripts/pydantic_agent_baseline.py capture \
  --profile implementer \
  --model openai-codex:gpt-5.6-luna \
  --output benchmarks/hermes-baseline-observed.json
```

The checked-in record was produced by that command. It records no response,
raw log, session identifier, environment value, credential path, or prompt other
than the fixed benign phrase.
Its machine-readable provenance kind is `observed_benign_hermes_smoke`.

### Exact observed identity and evidence

| Field | Observed value |
| --- | --- |
| Hermes profile | `implementer` |
| Qualified model | `openai-codex:gpt-5.6-luna` |
| Hermes runtime version | `0.21.2` |
| Hermes source revision | `044a77b3b6af4ce16138d42762f812a20b9f7a89` |
| Factory runtime revision at capture | `f6f68269bee26dbb33c49b21c01db871a901804e` |
| Capture timestamp (UTC) | `2026-09-23T18:00:32Z` |
| Evidence fingerprint | `sha256:57d039e2997c785437667ed38dcd26070784d107c41f1bcbc8ebb132a0e47a80` |

The observed process measurements and available usage evidence were:

| Metric | Value |
| --- | --- |
| Process exit code | `0` |
| Wall time | `3019.588 ms` |
| Peak RSS | `170717184 bytes` |
| Model calls | `1` (`api_calls` from the sanitized Hermes usage file) |
| Tool calls | `0` (the empty toolset did not permit tools) |
| Input tokens | `null` — Hermes usage evidence did not supply `input_tokens` |
| Output tokens | `null` — Hermes usage evidence did not supply `output_tokens` |
| Cache-read tokens | `null` — Hermes usage evidence did not supply `cache_read_tokens` |
| Cache-write tokens | `null` — Hermes usage evidence did not supply `cache_write_tokens` |

Unavailable values remain `null`; they are not inferred from prompt length,
model defaults, or the process response.

## Architecture directional sample

The following is cited from the architecture document at commit
`5ad887c7c38ba1c52d092d5b0011a7a0ff2573c0`, not from the issue body. It is an
**architecture smoke sample**, not the checked-in observed record. It is not a measurement of the synthetic corpus:

- the Hermes implementer sample took `18.955 seconds`, reached `267.6 MiB`
  maximum RSS, made two model requests, and carried `22,874` input tokens,
  including `20,990` cache reads;
- the stored Hermes system prompt for that sample was `40,260` characters and
  an unrelated `kanban_show` call occurred before the final answer;
- the minimal PydanticAI sample took `2.662 seconds` and reached `80.4 MiB`
  maximum RSS with one request and `34` input tokens; and
- the typed-tool/output PydanticAI probe took `5.174 seconds` and reached
  `80.7 MiB` maximum RSS.

Those values motivate a representative comparison. They do not establish a
throughput, quality, or production performance result.

## Limitations

- The checked-in observed smoke is one bounded benign run, not a distribution
  or load test.
- Hermes supplied only `api_calls` in the available usage evidence for this run;
  token fields are intentionally `null` with reasons.
- System-prompt size and response content are intentionally not captured, so no
  hidden prompt or model text is exposed.
- Synthetic vectors are future comparison fixtures and must never be reported
  as observed measurements. The replay summary carries `synthetic: true` and
  `observed: false` to make that distinction machine-readable.
