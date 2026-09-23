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
