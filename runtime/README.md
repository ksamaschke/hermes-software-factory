# Software Factory Runtime

`software-factory-runtime` contains the provider-neutral Pydantic control-plane
contracts for the Hermes software factory. It is intentionally limited to
models, policy validation, and the `TaskRepository` protocol; it does not start
a dispatcher, connect to a tracker, or execute PydanticAI agents.

## Install

```bash
python -m pip install ./runtime
```

The compatibility loader accepts the existing `examples/project-policy.yaml`
profile schema and maps its profile routes to the explicit `hermes_profile`
executor. Runtime policies use explicit `roles`, `agents`, `providers`, and
executor routes.

## Runtime boundary and credential semantics

`TaskRepositoryFacade` narrows an agent's API to claim, heartbeat, event, and
finish operations. It is a capability boundary, not a Python sandbox: the
trusted callables supplied by the controller retain their own authority. The
facade stores wrapper functions rather than readable bound methods (so a slot
does not expose a backend through `__self__`) and rejects operation replacement
after construction. It never accepts or stores a raw database/backend handle.

Workspace roots are lexically canonicalized before they are used for binding
comparisons. This normalizes separator and trailing-separator aliases and rejects
ambiguous dot/parent segments; filesystem symlink resolution remains the
controller's responsibility.

Event attributes and evidence references/descriptions use bounded, fail-closed
credential exclusions: strict scalar types, length/cardinality caps, control-
character rejection, known credential-shape rejection, and an entropy check for
opaque tokens. These checks cannot prove arbitrary text is non-secret, so callers
must still avoid submitting secrets and must treat an accepted value as a
bounded evidence identifier/description rather than a secret scanner result.
