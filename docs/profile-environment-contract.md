# Profile environment contract

Hermes profile isolation is a runtime boundary, not just a model or personality
selection. A worker profile may have its own `HERMES_HOME`, configuration,
skills, toolsets, credentials, interpreter, virtual environment, shell startup,
working directory, and dependency cache. The controller environment must not be
assumed to propagate into it.

## Runtime layers

Keep these layers separate:

1. **Factory controller:** the profile that creates/reconciles tasks and reports
   state.
2. **Worker profile:** the exact Hermes profile launched for the task, including
   its `HERMES_HOME`, config, skills, tools, provider route, and shell setup.
3. **Project worktree:** the candidate checkout, branch, project instructions,
   dependency manager, virtual environment, and declared test/build commands.
4. **External runtime:** live services, clusters, credentials, browsers, or
   provider endpoints required by a check.

A check passing in layer 1 does not prove that the same check is runnable in
layers 2–4.

## Environment provenance packet

Every implementation or review task that runs project commands records:

- profile name and profile-scoped `HERMES_HOME` identity;
- target repository, worktree, branch, and candidate commit;
- effective `cwd` and project instruction files;
- interpreter/runtime path and version;
- required command paths and versions;
- dependency activation command or project runner;
- relevant non-secret environment flags;
- external service/credential capability names, never secret values;
- the exact preflight command and result.

Project policy supplies the commands and dependency manager. The shared factory
does not assume `python`, `pytest`, `cargo`, `pnpm`, `npm`, `uv`, or any other
binary exists in every profile.

## Preflight procedure

Before dispatching a worker or reviewer:

1. Resolve the exact profile and verify its effective role prompt (`SOUL.md` or
   equivalent), config, skills, actual toolsets, and memory mode. A profile name
   does not provision a prompt, tool, or package.
2. Launch the bounded environment probe through the same profile/runtime that
   will run the task, not only from the controller shell.
3. Verify the worktree, branch, candidate commit, effective `cwd`, project
   instructions, runtime/interpreter, required commands, and dependency activation.
4. Run the smallest project-declared discovery check, such as test collection,
   a build-tool version check, or a project runner dry probe. Do not run the full
   gate during preflight.
5. Measure the effective prompt and loaded skill inventory against the project
   decision-contract budgets. Optional skills may be omitted or trimmed, but the
   typed identity/evidence context may not be truncated.
6. Record the result in the task/run evidence without secrets.
7. Dispatch the task only when the required capability is present.

After changing profile configuration, skills, tools, credentials, or project
dependencies, repeat the preflight. Do not retry an unchanged worker prompt.

## Failure classification

- **Missing command, package, interpreter, tool, skill, or target path:**
  `REVIEW-INCOMPLETE` or a factory capability diagnostic. The orchestrator
  repairs the profile/project environment or creates a narrower continuation.
- **Provider authorization or external permission unavailable:** an explicit
  external-authorization blocker; do not invent a product finding.
- **Command runs and reproduces a product defect:** normal implementation or
  review evidence.
- **Timeout, crash, or process loss before the target:** `REVIEW-INCOMPLETE`,
  never approval and never an unchanged retry.
- **Controller-side command passes while the worker-side probe is missing:**
  supplementary evidence only, not reviewer approval.

Routine environment repair is factory-owned. Do not send the human to install a
package, switch interpreters, restart a worker, or inspect routine logs unless
an external authorization or product decision is genuinely required.

## Reviewer-specific rule

A reviewer may not substitute the controller's test result for its own
profile/worktree preflight. Its final report includes `environment_provenance`
and distinguishes:

- the worker-side checks it actually ran;
- parent-side supplementary checks;
- capabilities that were unavailable;
- acceptance questions left incomplete.

A missing profile environment is a capability gap, not a clean review and not a
reason to broaden the review scope.

## Project add-ons

If a project needs a special environment, keep it in project policy, a
project-local setup script, or an external factory add-on. Do not add a project
interpreter path, package name, shell initialization rule, or provider-specific
environment assumption to the shared skills or Hermes core.
