# Running the worker

```sh
worker [ROOT OPTIONS] terraform DEPLOYMENT [OPTIONS]
```

Root options (credentials, backend, configuration file) come before the command, and command options after it. Most options have an environment variable and can be set in `worker_options`; see the [CLI reference](reference/cli.md).

`DEPLOYMENT` names the set of states being managed. It goes into the backend prefix (`terraform/state/{deployment}` by default) and is available to templates as `{{ deployment }}`. It must be 32 characters or fewer, with no spaces.

## Phases

`worker terraform` runs every definition through up to three phases, in configuration order:

1. **Init.** Each definition is copied into the working directory, its generated files are written, its templates are rendered, and `terraform init` runs.
2. **Plan**, with `--plan` (or `--plan-destroy` for a destroy plan). Definitions with no changes are skipped by the next phase. A definition with `always_apply: true` is applied as soon as it is planned, even without `--apply` and even with no changes, so the definitions after it plan against its new outputs.
3. **Apply** with `--apply`, or **destroy** with `--destroy`.

Every definition is planned before any is applied, and a plan reads the states named in its `remote_vars`. On a new deployment, apply the definitions others read from first (for example with `--limit`), or give them `always_apply`.

The phases are independent switches, so a run can plan only, plan and apply, or apply plans that were saved earlier:

| Goal | Options |
| --- | --- |
| Review changes | `--plan` |
| Plan and apply in one run | `--plan --apply` |
| Tear down | `--plan-destroy --destroy` |
| Apply a reviewed plan later | `--plan` with `--plan-file-path` or `--backend-plans`, then `--apply` with the same option in a second run |

`--apply` and `--destroy` cannot be combined, and neither can `--plan` and `--plan-destroy`.

## Saved plans

With `--plan-file-path DIR`, plans are written to `DIR/<deployment>/<definition>.tfplan`. With `--backend-plans`, they are stored in the state bucket next to each state, and `--run-id` prefixes the plan file names so concurrent runs do not collide.

When saved plans are on, a definition that already has a saved plan is not planned again; the plan phase reuses it and the apply phase applies it. `--force-plan` plans anyway and replaces the saved plan. A run with `--apply` and no `--plan` fetches the saved plans and applies only the definitions that have one; it needs the same `--plan-file-path` or `--backend-plans` as the planning run.

`--intent deploy|preview|drift` (`WORKER_INTENT`) says what a run's plans are for. [Run reporting](reporting.md) records it, and a `preview` or `drift` plan never becomes a pending plan.

## Choosing definitions

`--limit NAME` restricts the run to the named definitions. It can be repeated or given a comma-separated list. A definition with `always_include: true` is planned and applied even when it is not in the list, but a destroy only touches the listed definitions.

`--target ADDRESS` is passed to terraform plan as `-target`, so the saved plan, and the apply or destroy of it, covers only those resources. It is repeatable as well.

## When a definition fails

Each phase has two options. The first decides whether the phase stops at the first failing definition; the second decides whether the worker exits non-zero at the end.

| Phase | Stop at first failure (default on) | Exit non-zero at the end (default on) |
| --- | --- | --- |
| Init | `--init-failures` | `--fail-on-init-error` |
| Plan | `--plan-failures` | `--fail-on-plan-error` |
| Apply or destroy | `--apply-failures` | `--fail-on-apply-error` |

With `--no-<phase>-failures`, every definition is attempted and all failures are reported together when the phase ends. Definitions that failed in one phase are not run in the next. A failing hook counts as a failure of its definition. With the `--no-fail-on-*` forms, failures are reported but the run exits 0.

## Output

`--stream-output` (on by default) streams terraform's output as it runs. `--show-output` is accepted but currently has no effect. A definition can turn off streaming for its plan or apply with `squelch_plan_output` or `squelch_apply_output`. `--color` keeps terraform's colors. `--log-level` and `--log-format` (`TEXT` or `JSON`) control the worker's own logging.

## Working directory

Each run builds its definitions in a temporary directory that is removed on exit. `--working-dir DIR` uses an existing empty directory instead and leaves it in place. To investigate a definition, run with `--working-dir`, `cd` into `DIR/definitions/<name>`, and `eval "$(worker env DEPLOYMENT)"` first so terraform has the same credentials the worker used.

## Other commands

- `worker env DEPLOYMENT` prints `export` lines for the credentials the worker would use.
- `worker clean DEPLOYMENT` removes what the backend leaves behind for a destroyed deployment, after checking its states are empty. `--limit` restricts it to some definitions.
- `worker --version` prints the version.
