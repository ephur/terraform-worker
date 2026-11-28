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

Each saved plan gets a `tfworker.plan/v1` manifest, and a plan is only applied while it is pending, unexpired, unaltered and still matches the state; see [Saved plans and the apply gate](reporting.md#saved-plans-and-the-apply-gate). `--plan-ttl` (`WORKER_PLAN_TTL`, default `7d`) sets how long a saved plan may be applied after it was made, and `--require-plan-manifest` (`WORKER_REQUIRE_PLAN_MANIFEST`) refuses plans saved by older workers without a manifest, which are otherwise applied with a warning.

`--intent deploy|preview|drift` (`WORKER_INTENT`) says what a run's plans are for. [Run reporting](reporting.md) records it, and a `preview` or `drift` plan never becomes a pending plan. With `--backend-plans`, such plans are saved apart under `<prefix>/_tfworker/previews/` and never applied by a later run.

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

## Saved plan commands

The **plans** command group works on the saved plans of a deployment through their manifests (see [Saved plans and the apply gate](reporting.md#saved-plans-and-the-apply-gate)). It needs the `s3` backend. Each subcommand takes the **deployment** argument.

### `worker plans list`

Lists every saved plan record, oldest first: its id, definition, status, change counts, when it was made and expires, and the run that made it. `--status` (repeatable) and `--definition` narrow the list. `--json` prints the `tfworker.plan/v1` manifests instead.

```sh
worker --config-file ./worker.yaml plans list prod --status pending --json
```

### `worker plans discard`

Marks one or more pending plans `discarded` and removes their plan files, so no run can apply them, even a run that has already downloaded one. `--reason` is required and is kept in each plan's history. Only a pending plan can be discarded; the command exits non-zero if any plan was not found or was not pending.

```sh
worker --config-file ./worker.yaml plans discard prod 01JD3Z... --reason "superseded by a hotfix"
```

### `worker plans gc`

Retires the pending plans that can no longer be applied. It is meant to run as a scheduled job. Each pending plan is checked in turn:

- **Expired.** The plan is past `expires_at`. It is marked `expired`.
- **Orphaned.** The plan was made against a state that no longer exists, or its definition is missing from the configuration and has no state. The plan is marked `discarded`. A definition that is missing from the configuration but still has state is only treated as orphaned with `--discard-unconfigured`, so a job run with the wrong configuration does not throw away good plans.
- **Stale.** The state moved since the plan was made. When the bucket is versioned and the state's version id still matches the one recorded at plan time, nothing more is read; otherwise the state's lineage and serial are compared. A stale plan is marked `stale`. The reason names the run that moved the state when the definition's last recorded apply succeeded and left the state at its current serial, and otherwise says the state changed outside a recorded run.

Pending plans are found through the pending index. `--all-records` reads every plan record instead, which also finds preview plans and any plan whose index entry was lost, at the cost of reading the whole history.

`--applying-timeout` (for example `6h`, and more than zero) also marks plans that have been `applying` for longer than that `failed`, which recovers plans left behind by a worker that was killed mid apply. This pass reads every plan record that has not changed for that long.

Every change is appended to the plan's history, and the plan file is removed. `--dry-run` reports what would be retired and changes nothing. `--json` prints a list of objects with `plan_id`, `definition`, `plan_key`, `record_key`, `check` (`orphaned`, `expired`, `stale` or `applying`), `status`, `reason`, `outcome` (`planned` with `--dry-run`, `retired`, `changed` when another run changed the plan first, or `error`) and `error`. gc exits non-zero when any plan could not be checked or retired.

The `plans` commands log to stderr, so their output can be piped, and they never create the backend bucket or lock table.

```sh
worker --config-file ./worker.yaml plans gc prod --applying-timeout 6h
```

## Other commands

- `worker env DEPLOYMENT` prints `export` lines for the credentials the worker would use.
- `worker clean DEPLOYMENT` removes what the backend leaves behind for a destroyed deployment, after checking its states are empty. `--limit` restricts it to some definitions.
- `worker plans list|discard|gc DEPLOYMENT` works on saved plans; see [Saved plan commands](#saved-plan-commands).
- `worker --version` prints the version.
