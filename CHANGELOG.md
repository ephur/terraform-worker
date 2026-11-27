# Unreleased

## Breaking changes
- Removed GCP support (#191). The worker is only used against AWS, and the GCP code had no users while still needing maintenance, tests and the `google-cloud-storage` dependency.
  - The `gcs` backend is gone; `--backend` now only accepts `s3`.
  - The Google authenticator and the special-cased `google` / `google-beta` providers are gone. Those providers can still be configured in `providers`; they are now handled like any other provider by the generic provider, without the worker injecting credentials, region or project.
  - The `--gcp-region`, `--gcp-creds-path` and `--gcp-project` root options (and the `GCP_REGION`, `GCP_CREDS_PATH`, `GCP_PROJECT` environment variables) were removed.
  - `google-cloud-storage` is no longer a dependency.
- `terraform_vars` are now written to `worker_generated.auto.tfvars.json` instead of `worker_generated.auto.tfvars` (#186). Values keep their YAML types and are no longer interpolated by terraform, so quotes, backslashes, `${` and `%{` are passed through as-is. Booleans and numbers inside lists and maps are now real values instead of strings, and a YAML null now leaves the variable unset instead of passing `"None"`. Hooks that parse `TF_VAR_*` lists or maps as JSON will see the same change.

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
- Packaging and dev tooling moved from Poetry to uv; the version now comes from git tags via hatch-vcs (#202).
- CI runs lint (black, isort, flake8) and tests on Python 3.11 to 3.14 (#157), and builds and smoke-tests the Docker image (#212).
- The coverage gate is raised from 60% to 93%, the measured level, and is set once in `pyproject.toml` for CI and `make` (#194).

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
