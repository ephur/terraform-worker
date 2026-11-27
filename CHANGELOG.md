# Unreleased

## Breaking changes
- Removed GCP support (#191). The worker is only used against AWS, and the GCP code had no users while still needing maintenance, tests and the `google-cloud-storage` dependency.
  - The `gcs` backend is gone; `--backend` now only accepts `s3`.
  - The Google authenticator and the special-cased `google` / `google-beta` providers are gone. Those providers can still be configured in `providers`; they are now handled like any other provider by the generic provider, without the worker injecting credentials, region or project.
  - The `--gcp-region`, `--gcp-creds-path` and `--gcp-project` root options (and the `GCP_REGION`, `GCP_CREDS_PATH`, `GCP_PROJECT` environment variables) were removed.
  - `google-cloud-storage` is no longer a dependency.
- `terraform_vars` are now written to `worker_generated.auto.tfvars.json` instead of `worker_generated.auto.tfvars` (#186). Values keep their YAML types and are no longer interpolated by terraform, so quotes, backslashes, `${` and `%{` are passed through as-is. Booleans and numbers inside lists and maps are now real values instead of strings, and a YAML null now leaves the variable unset instead of passing `"None"`. Hooks that parse `TF_VAR_*` lists or maps as JSON will see the same change.

## Features
- Apply and destroy failure policy (#195). `--apply-failures/--no-apply-failures` (`WORKER_APPLY_FAILURES`) and `--fail-on-apply-error/--no-fail-on-apply-error` (`WORKER_FAIL_ON_APPLY_ERROR`) work like the init and plan options. Both default to true, which keeps today's behavior: the first failed apply or destroy stops the phase and the run exits non-zero. With `--no-apply-failures`, a failed definition is recorded, its ERROR stage is dispatched, the remaining definitions are still applied, and every failure is reported at the end of the phase.
  - A failing PRE or POST apply/destroy hook, and a failing `always_apply` apply during the plan phase, count as that definition's apply failure and follow the same options.
  - Definitions carry `apply_failed` (attempted and failed) and `apply_skipped` (had a plan, but a halt stopped the phase before it). A definition with nothing to apply has neither.
  - A failed apply or destroy hook now exits with code 1, like every other phase failure, instead of 2.

- Without `--working-dir`, terraform init/plan/apply, plan JSON output and provider mirroring now run in the resolved temporary working directory instead of `<cwd>/None/...`. The temporary directory is created with `mkdtemp`, so it exists for the whole run, and it is removed at exit (#181).
- A `.tf` and `.tf.j2` file with the same name now raises the intended "already exists" error instead of `UnboundLocalError` (#182).
- A failing handler setup or teardown no longer stops the other handlers. Every handler is torn down, a teardown error no longer replaces the run's original failure, and a terminating `HandlerError` in setup or teardown exits with code 1 instead of a traceback. An unexpected (non-`HandlerError`) exception in setup is now logged and the run continues (#188).
- Hook remote vars (`TF_REMOTE_*`) are now resolved by the HCL2 path: python-hcl2 wraps references as `${...}`, which the parser rejected, and a call to the nonexistent `log.warning` crashed into the regex fallback. Simple references produce the same values as before; nested references in maps and lists are now resolved (#183).
- Removed `_check_handlers_ready`, which never removed anything. The handlers collection already filters on `is_ready()` (#184).
- The Dockerfile builds again (#201): Python 3.13-slim, the worker installed with uv from `uv.lock`, and Terraform as a `TERRAFORM_VERSION` build arg (default 1.17.2) whose checksum is verified. CI now builds the image and smoke-tests it.
- `--config-var` values containing `=` no longer crash definition preparation or get dropped from config templates (#187).
- `terraform get` now runs with `--terraform-bin` and the same authenticator environment as init, plan and apply, instead of the `terraform` on `PATH` with no credentials (#185).

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


- ... @TODO find time to update this :)
