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

`coordinator_admission_contract.py` — a project-agnostic pure helper for
scheduled coordinator overlays. It preserves a queue-derived product action
when a liveness/no-progress handoff wakes the coordinator, emits a bounded
progress contract, and leaves issue selection and mutation to the LLM-owned
coordinator path.

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
