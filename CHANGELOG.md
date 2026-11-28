# Unreleased

## Breaking changes
- Removed GCP support (#191). The worker is only used against AWS, and the GCP code had no users while still needing maintenance, tests and the `google-cloud-storage` dependency.
  - The `gcs` backend is gone; `--backend` now only accepts `s3`.
  - The Google authenticator and the special-cased `google` / `google-beta` providers are gone. Those providers can still be configured in `providers`; they are now handled like any other provider by the generic provider, without the worker injecting credentials, region or project.
  - The `--gcp-region`, `--gcp-creds-path` and `--gcp-project` root options (and the `GCP_REGION`, `GCP_CREDS_PATH`, `GCP_PROJECT` environment variables) were removed.
  - `google-cloud-storage` is no longer a dependency.
- `terraform_vars` are now written to `worker_generated.auto.tfvars.json` instead of `worker_generated.auto.tfvars` (#186). Values keep their YAML types and are no longer interpolated by terraform, so quotes, backslashes, `${` and `%{` are passed through as-is. Booleans and numbers inside lists and maps are now real values instead of strings, and a YAML null now leaves the variable unset instead of passing `"None"`. Hooks that parse `TF_VAR_*` lists or maps as JSON will see the same change.
- Definition names may no longer start with `_` (#219). Those names are reserved for worker metadata stored beside state, such as `<prefix>/_tfworker/`.
- `--backend-use-all-remotes` now only counts `<prefix>/<definition>/terraform.tfstate` as a definition (#219). Before, any key under the prefix added its parent directory, so saved plans under a `--run-id` directory and anything else in the bucket produced bogus remotes.

## Features
- `worker plans list|discard|gc` (#216). `list` shows saved plan records with `--status`, `--definition` and `--json` (the `tfworker.plan/v1` manifests). `discard` marks pending plans discarded with a required `--reason` and removes their files. `gc` retires expired, orphaned and stale pending plans, checking the state's S3 version id before reading it and naming the recorded run that moved the state, or saying it moved outside one. `--applying-timeout` also fails plans stuck `applying`, `--all-records` checks records missing from the pending index, `--discard-unconfigured` discards plans for definitions missing from the configuration, and `--dry-run` changes nothing. gc exits non-zero when a plan could not be checked or retired. These commands log to stderr and never create the backend bucket or lock table.
- Saved plans have a manifest and an apply gate (#215). Every plan saved with `--backend-plans` or `--plan-file-path` gets a `tfworker.plan/v1` manifest (in the backend, a record under `<prefix>/_tfworker/plans/` with a pointer in the plan's slot) recording the run and provenance that made it, the state it was made against, its sha256, change counts, expiry and a status history. A plan is applied only while it is pending, unexpired, unaltered, of the right kind (destroy or not) and still matches the state, and the run applying it first takes it with a conditional write, so two runs never apply the same plan at once. A plan whose apply never reached terraform, such as after a failed PRE hook, is released back to pending. A successful apply marks the definition's other pending plans stale, `--force-plan` marks the replaced plan superseded, and `--intent preview` and `--intent drift` plans are saved under `<prefix>/_tfworker/previews/` and never applied by a later run; such runs cannot apply or destroy. A refused plan counts as that definition's apply failure. See "Saved plans and the apply gate".
- `--plan-ttl` (`WORKER_PLAN_TTL`, default `7d`) and `--require-plan-manifest` (`WORKER_REQUIRE_PLAN_MANIFEST`) (#215). Plans saved by older workers have no manifest and are applied with a warning unless `--require-plan-manifest` is set.
- Run records and per-definition status (#214). With a top-level `reporting:` block in the config file (off by default), the worker writes an immutable `tfworker.run/v1` record per invocation and a `tfworker.status/v1` snapshot per definition under `<prefix>/_tfworker/`, beside each run's own state. Records carry phase timings, lock wait, exit codes, change counts and addresses (never attribute values), state lineage and serial, module sources and run provenance; status tracks the last plan, apply and failure, the pending plan and drift. Status updates are conditional writes retried on conflict. Reporting is core, not a handler: the terraform command tells it about each phase through an internal phase dispatcher (`tfworker.phases`) that handlers are not part of (#253). The models live in `tfworker.report` with JSON Schema exports, and are documented under "Run reporting".
- `--run-label key=value` (`WORKER_RUN_LABELS`) and `--intent deploy|preview|drift` (`WORKER_INTENT`) are recorded by run reporting (#214). Without `--run-id`, run reporting records a generated ULID.
- `worker clean` keeps `_tfworker/` run history (#214).
- State lock waiting (#213). With a top-level `locking:` block in the config file (off by default), the worker reads the definition's state lock right before each plan, apply and destroy, waits out a live lock with backoff, and fails the definition, not the run, when the wait runs out (`on_timeout: fail_run` stops the run instead). While terraform runs, each worker keeps a heartbeat lease at `<state key>.tfworker-lease.<host>`: a lock whose lease is fresh is live at any age, a lock whose lease missed three heartbeats (plus a minute of skew) is stale, and a lock with no lease is stale after `stale_after`. A stale lock fails with the lock ID by default; `on_stale: wait` or `force_unlock` change that, and `force_unlock` only removes a lock that looked stale on two polls. `worker clean` removes leases left by killed workers.
- `--lock-timeout` (`WORKER_LOCK_TIMEOUT`) passes `-lock-timeout` to plan, apply and destroy, so terraform retries a held lock instead of failing at once (#213). With `locking` configured, the time left of its wait is used instead.
- Handlers can implement `skip(action, deployment, definition, reason)` to hear about definitions a phase passed over without running terraform: a plan that was not needed, a definition that did not initialize, an apply-only run with no saved plan, a destroy outside `--limit`, or a definition left behind when a failure halted the phase (#219). Each definition is reported at most once per action. It is a no-op by default, and a handler that raises from it is logged without stopping the run.
- `TerraformResult` carries `started_at`, `finished_at` (UTC) and `duration` for each terraform command (#219). `duration` is measured with a monotonic clock. Results that stand in for a failure outside terraform, such as a failed hook, are stamped with the moment of failure.
- Apply and destroy failure policy (#195). `--apply-failures/--no-apply-failures` (`WORKER_APPLY_FAILURES`) and `--fail-on-apply-error/--no-fail-on-apply-error` (`WORKER_FAIL_ON_APPLY_ERROR`) work like the init and plan options. Both default to true, which keeps today's behavior: the first failed apply or destroy stops the phase and the run exits non-zero. With `--no-apply-failures`, a failed definition is recorded, its ERROR stage is dispatched, the remaining definitions are still applied, and every failure is reported at the end of the phase.
  - A failing PRE or POST apply/destroy hook, and a failing `always_apply` apply during the plan phase, count as that definition's apply failure and follow the same options.
  - Definitions carry `apply_failed` (attempted and failed) and `apply_skipped` (had a plan, but a halt stopped the phase before it). A definition with nothing to apply has neither.
  - A failed apply or destroy hook now exits with code 1, like every other phase failure, instead of 2.

## Bug fixes
- Without `--working-dir`, terraform init/plan/apply, plan JSON output and provider mirroring now run in the resolved temporary working directory instead of `<cwd>/None/...`. The temporary directory is created with `mkdtemp`, so it exists for the whole run, and it is removed at exit (#181).
- A `.tf` and `.tf.j2` file with the same name now raises the intended "already exists" error instead of `UnboundLocalError` (#182).
- A failing handler setup or teardown no longer stops the other handlers. Every handler is torn down, a teardown error no longer replaces the run's original failure, and a terminating `HandlerError` in setup or teardown exits with code 1 instead of a traceback. An unexpected (non-`HandlerError`) exception in setup is now logged and the run continues (#188).
- Hook remote vars (`TF_REMOTE_*`) are now resolved by the HCL2 path: python-hcl2 wraps references as `${...}`, which the parser rejected, and a call to the nonexistent `log.warning` crashed into the regex fallback. Simple references produce the same values as before; nested references in maps and lists are now resolved (#183).
- Removed `_check_handlers_ready`, which never removed anything. The handlers collection already filters on `is_ready()` (#184).
- The Dockerfile builds again (#201): Python 3.13-slim, the worker installed with uv from `uv.lock`, and Terraform as a `TERRAFORM_VERSION` build arg (default 1.17.2) whose checksum is verified. CI now builds the image and smoke-tests it.
- `--config-var` values containing `=` no longer crash definition preparation or get dropped from config templates (#187).
- `terraform get` now runs with `--terraform-bin` and the same authenticator environment as init, plan and apply, instead of the `terraform` on `PATH` with no credentials (#185).

## Features and changes
These landed after 0.13.0 and are summarized here; see the linked PRs and the docs for details.
- **Handlers** gained ordering (priority plus declared dependencies) and a shared results registry, so one handler can use another's output.
  - **GitHub** (new, #180): per-definition check runs with a rollup, a deployment-scoped PR status comment, and plan/apply reporting.
  - **Slack** (#169, reworked in #180): a single live-updating Block Kit status board per run, with a threaded per-definition table.
  - **Datadog** (new, #180): change events for plan and apply.
  - **SQS** (queues validated at startup), **OpenAI** (plan summaries, embedded by the GitHub handler), **Snyk** and **Trivy** scanning, and **Bitbucket** (comments only when a plan has changes).
  - The **S3** handler stores saved plans in the backend bucket; `--run-id` prefixes those plan files.
- **Definitions sources** (#180): `definitions_sources` in the config fetch a path or git repo, run a command, and insert the definitions it prints (YAML or JSON) after a named definition. `{{ deployment }}` is available in config templates.
- **Saved-plan applies**: apply uses a saved plan when one exists and skips definitions without one, consumed plans are removed, and init is skipped for definitions with nothing to apply. `--force-plan` replans over a saved plan (it replaces the unused `--force`).
- **Init phase options**: `--init-failures` / `--fail-on-init-error` control whether a prepare or init failure stops the run and whether it exits non-zero; `--plan-failures` / `--fail-on-plan-error` do the same for plan errors (#170). `parallel_options` sets the worker pools for preparation and init.
- **Destroy**: `--plan-destroy` creates a destroy plan, and the `clean` command removes empty state files and DynamoDB lock tables from the backend.
- **Terraform**: `--target` (validated, applied at plan time), pessimistic (`~>`) provider version constraints, provider aliases, provider locking limited to modules terraform loads, and ANSI codes stripped from captured output.
- **Remote and hook vars**: one code path builds `TF_VAR_*`, `TF_REMOTE_*` and `TF_EXTRA_*` for hooks, supports nested `remote_vars` and bracket notation, and does not cache failed lookups. `hooks_disable_remotes` on a definition skips remote state lookups for its hooks. `--backend-use-all-remotes` builds remote data sources for every definition in the backend.
- **AWS**: refreshable boto credentials, and `AWS_PROFILE` is ignored when another credential source is given.
- **Logging**: `--log-format JSON` for structured logs, and cleaner subprocess output.

## Tooling
- Coverage gate raised to 94% (#214).
- Packaging and dev tooling moved from Poetry to uv; the version now comes from git tags via hatch-vcs (#202).
- CI runs lint (black, isort, flake8) and tests on Python 3.11 to 3.14 (#157), and builds and smoke-tests the Docker image (#212).
- The coverage gate is raised from 60% to 93%, the measured level, and is set once in `pyproject.toml` for CI and `make` (#194).
- The Sphinx docs are replaced by Markdown under `docs/`, checked against the code and corrected where they had drifted. The CLI and configuration references (including every handler's options) are generated by `scripts/gen_reference.py` (`make docs`), and CI fails when they are out of date (#251).
- Pull requests to this repository are reviewed by Claude through `anthropics/claude-code-action`, posting as `claude[bot]`. The workflow runs only in `ephur/terraform-worker` and authenticates with workload identity federation (GitHub OIDC, no stored API key); it skips when the federation variables are not set, so forks are unaffected (#222).

# Changes 0.12.0 -> 0.13.0 (2024-06...)

## Notes
This version reconsiders many of the core elements of the worker application.

The application now relies on pydantic to define the models for what is consumed from the configuration file and the command line. Previously there was a heinous mix of classes which just had attributes and methods seemingly hung on
them at random. This was a nightmare to maintain and understand.

- the CopierFactory was reorganized to be more modular
- providers became a package, with a model used to validation all provider configurations
- all of the configuration file logic was moved into the `commands` module, the logic to handle managing the cofig is all contained there instead of being spread out many places
- added tfworker.util.log to handle interaction with the user, via the CLI or via a logger in the future
- made significant strides towards consolidating all exceptions / error handling
- made significant strides in only having the primary cli.py and commands/ actually cause the program to terminate
- validation of options and inputs is now handled centrally in the pydantic models instead of being spread out *everywhere*
