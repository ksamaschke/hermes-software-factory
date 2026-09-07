# Local variant snapshot

These files are the installed-variant counterparts of the generic factory
contracts. They existed only on one machine's disk under `~/.hermes/`, which is
not version controlled, so a disk loss would have destroyed them. This directory
is a durable backup, not the live copy.

## What is here

`skills/` — three review skills that have no generic counterpart in `skills/`:

- `kanban-review-integrity` — honest closure, timeout handling, and the
  reviewer timeout policy reference.
- `kanban-review-orchestration` — dispatching bounded review leaves and fan-in,
  including the packet failure modes and candidate-gate verification references.
- `factory-review-routing` — reviewer model and effort routing.

`profiles/` — the reviewer profile role text (`SOUL.md`) for the `reviewer` and
`vanillacore-reviewer` profiles. Both carry the change-scoped review contract:
two review kinds, the diff as the scope boundary, and the two-tier budget.

`orchestrator_decision_contract.py` — the provider-neutral typed context,
decision ladder, readback validation, and no-side-effect fixture adapter. It is
not a scheduler or a tracker client.

`orchestrator_llm_evaluation.py` — the named native integration harness. It
installs `docs/orchestrator-soul-template.md` into a temporary profile, disables
profile memory, exposes only read-only synthetic MCP tools, invokes the selected
native Hermes model, and validates the returned decision against the fixture.
The harness never writes source or fixture state; credentials, when explicitly
provided for a native run, are copied only to the temporary profile and removed
with it.

## Relationship to the live installation

The live copies live under `~/.hermes/skills/software-development/<skill>/` and
`~/.hermes/profiles/<profile>/SOUL.md`. Skills that DO have a generic
counterpart are symlinked into `skills/` instead of copied, so they track this
repository automatically:

    ~/.hermes/skills/software-development/kanban-reviewer-contract
      -> <repo>/skills/kanban-reviewer-contract

Prefer that symlink arrangement. A real directory copy silently drifts: the
installed `kanban-reviewer-contract` was once 70 diff lines behind the generic
contract while appearing installed and healthy.

## Installing the generic orchestrator role

Copy the generic soul template into the selected orchestrator profile and copy
the `decision_contract` section from `examples/project-policy.yaml` into the
project policy. The profile configuration must disable worker shared-memory
writes (or select an isolated store), enable only the tools declared by policy,
and set the effective prompt/skill budgets. Verify the installed `SOUL.md`,
config, tool catalog, loaded skills, and memory mode from the launched profile;
a controller-side profile name is not installation evidence.

For a native, read-only behavioral evaluation from this checkout, provide an
authenticated profile-scoped Hermes auth file without putting it in the source
tree:

    FACTORY_EVAL_AUTH_FILE=<path-to-auth-json> \
      python3 local-variant/orchestrator_llm_evaluation.py \
      --trace-output <scratch-trace.json>

The command creates six fresh synthetic tracker item identities, runs each
through native Hermes plus the fixture MCP server, and prints secret-safe
per-case actions, tool calls, and effective prompt/skill sizes. A missing native
provider or denied capability is an explicit unavailable result, not a passing
fixture or a reason to weaken command approvals.

## Refreshing this snapshot

    for s in kanban-review-integrity kanban-review-orchestration \
             factory-review-routing; do
      cp -R ~/.hermes/skills/software-development/$s local-variant/skills/
    done
    cp ~/.hermes/profiles/reviewer/SOUL.md \
       local-variant/profiles/reviewer-SOUL.md
    cp ~/.hermes/profiles/vanillacore-reviewer/SOUL.md \
       local-variant/profiles/vanillacore-reviewer-SOUL.md

## Scope boundary

This snapshot is deliberately free of credentials, tokens, and internal
hostnames. Project-specific operational scripts that name private hosts or
issue trackers do not belong here; keep them with the private material for the
project they serve.
