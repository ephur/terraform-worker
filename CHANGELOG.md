# Unreleased

## Breaking changes
- Removed GCP support (#191). The worker is only used against AWS, and the GCP code had no users while still needing maintenance, tests and the `google-cloud-storage` dependency.
  - The `gcs` backend is gone; `--backend` now only accepts `s3`.
  - The Google authenticator and the special-cased `google` / `google-beta` providers are gone. Those providers can still be configured in `providers`; they are now handled like any other provider by the generic provider, without the worker injecting credentials, region or project.
  - The `--gcp-region`, `--gcp-creds-path` and `--gcp-project` root options (and the `GCP_REGION`, `GCP_CREDS_PATH`, `GCP_PROJECT` environment variables) were removed.
  - `google-cloud-storage` is no longer a dependency.

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
