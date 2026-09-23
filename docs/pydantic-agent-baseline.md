# Hermes baseline and synthetic replay contract

Status: strict synthetic test vectors plus one sanitized observed Hermes smoke; not a production load benchmark.

The eight fixtures in [`benchmarks/pydantic-agent-corpus.json`](../benchmarks/pydantic-agent-corpus.json)
are **synthetic test vectors**. Their numbers exercise evidence shape and
lifecycle contracts only. They are **not a measurement**. The separately
captured observed Hermes evidence is
[`benchmarks/hermes-baseline-observed.json`](../benchmarks/hermes-baseline-observed.json)
uses the machine-readable provenance kind `observed_benign_hermes_smoke`.

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
carries only fixed scenario codes. No objective/summary prose, live task ID,
repository URL, path, credential, or repository content is accepted. Event
providers, models, tool names, transition reasons, and review checks are also
fixed per scenario. Unknown fields are rejected recursively.

The validator is deliberately defensive at the JSON boundary. Wrong container
shapes, a list event kind, string latency, and malformed nested values return
deterministic validation errors rather than raising `TypeError` or `KeyError`.

## Deterministic replay

Replay is a local JSON operation. It validates the corpus and computes a stable
aggregate; it does not import Hermes, contact a provider, open a network
connection, invoke Git, write files, mutate Factory tasks, or access an
external repository.

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

Capture is deliberately separate, fixed, and explicitly opt-in. It cannot
receive profile or model overrides. The command requires an acknowledgement
because a real Hermes one-shot reads the profile-bound credential/config store
and is expected to persist its own profile SessionDB/session state and logs.
Those local Hermes side effects are real and are not represented as isolation
or as “no logs/sessions”.

```bash
python3 scripts/pydantic_agent_baseline.py capture \
  --ack-local-hermes-persistence \
  --output benchmarks/hermes-baseline-observed.json
```

The fixed capture uses:

- profile `implementer`;
- model `openai-codex:gpt-5.6-luna` through the Codex route;
- prompt `Do not use tools. Reply with exactly: Hermes baseline OK.`;
- the real profile-default prompt and tool overhead, with no safe-mode,
  ignore-rules, or reduced-toolset override;
- a fresh temporary non-git working directory;
- a minimal environment allowlist for `HOME`, `PATH`, locale, and TLS/proxy
  transport variables, with Hermes control-plane, Kanban, dispatcher, task,
  run, lease, profile, session, platform, cron, and gateway variables removed;
- a 120-second foreground bound and process-group termination on timeout.

Hermes stdout is held only in memory and must equal the exact fixed response;
stderr is not captured. The response, prompt beyond the fixed public phrase,
raw stderr/logs, raw usage failure strings, session identifiers, environment
values, authentication paths, and credentials are discarded. A fresh helper
process has no prior children; it measures only the Hermes child using
`RUSAGE_CHILDREN`. Its RSS value is **maximum direct-child RSS, not aggregate
process-tree RSS**.

The usage file is reduced to typed fields only. Capture fails closed unless the
Hermes exit code is zero, it did not time out, the exact response passed, usage
is complete and not failed, provider/model match the requested Codex route, and
`api_calls == 1`. That one-call exact-response result is the evidence basis for
zero tool roundtrips even though the profile-default tools remain available.
No failed or incomplete capture is written as an observed record.

Local credential-store reads and profile SessionDB/log writes are expected;
Factory task mutations and external repository mutations are not performed by
this benchmark contract. The checked-in record binds to the capture executable
source revision and source digest, factory base/head, profile/model, clean-before-
capture state, environment-scrub contract, and a profile contract fingerprint
covering only non-secret prompt/config metadata. The evidence fingerprint is a
self-consistency digest, not an attestation.

Metric availability is fail-closed. A null metric has exactly one enum reason;
a non-null metric has no reason; unknown reason keys and disagreement between
nested usage availability and top-level availability are rejected. Model calls
and input/output/cache counts come only from sanitized Hermes usage fields.

## Observed record fields

The observed record identifies the exact Hermes version and source revision,
capture source implementation commit, factory base/head revisions, profile and
qualified model, source/profile fingerprints, and the actual captured values:

| Field | Meaning |
| --- | --- |
| `metrics.model_calls` | sanitized `api_calls` from the Hermes usage file |
| `metrics.tool_calls` | zero, justified by one-call exact-response evidence |
| `metrics.input_tokens` | usage-file value or `null` with an enum reason |
| `metrics.output_tokens` | usage-file value or `null` with an enum reason |
| `metrics.cache_read_tokens` | usage-file value or `null` with an enum reason |
| `metrics.cache_write_tokens` | usage-file value or `null` with an enum reason |
| `metrics.wall_time_ms` | bounded parent-observed wall time |
| `metrics.peak_rss_bytes` | fresh-helper maximum child RSS |

The observed smoke is one bounded benign run, not a distribution, load test,
quality result, or throughput claim. Token fields remain unavailable when the
real usage file does not supply them; they are never inferred from prompt size
or response text.

## Architecture directional sample

The following is cited from the architecture document at commit
`5ad887c7c38ba1c52d092d5b0011a7a0ff2573c0`, not from the issue body. It is an
**architecture smoke sample**, not the checked-in observed record and not a
measurement of the synthetic corpus:

- the architecture's Hermes implementer sample took `18.955 seconds`, reached
  `267.6 MiB` maximum RSS, made two model requests, and carried `22,874` input
  tokens, including `20,990` cache reads;
- the stored Hermes system prompt for that sample was `40,260` characters and
  an unrelated `kanban_show` call occurred before the final answer;
- the minimal PydanticAI sample took `2.662 seconds` and reached `80.4 MiB`
  maximum RSS with one request and `34` input tokens; and
- the typed-tool/output PydanticAI probe took `5.174 seconds` and reached
  `80.7 MiB` maximum RSS.

Those values motivate a representative comparison. They do not establish a
throughput, quality, or production performance result.

## Limitations

- Synthetic vectors are not measurements and must never be reported as such.
- The checked-in observed smoke is one bounded run, not a distribution or load
  test.
- Real Hermes local credential-store, profile SessionDB/session, and log writes
  occur; the capture does not claim an isolated session or isolated logs.
- The fixed smoke prompt is benign and no tool roundtrip is expected; it does
  not represent an implementation, review, retry, timeout, or cancellation
  workload.
- Hermes usage availability depends on the fields emitted by that runtime;
  unavailable values stay `null` with strict enum reasons.
- System-prompt size and response content are intentionally not retained, so
  no hidden prompt or model text is exposed.
- The source/profile/evidence fingerprints are self-consistency checks, not
  independent provenance attestations.
