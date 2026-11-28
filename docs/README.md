# terraform-worker documentation

The worker runs a pipeline of terraform root modules ("definitions") from one configuration file. It generates the provider, backend and remote state plumbing for each definition, passes outputs between them, and runs plan, apply or destroy across the whole set.

## Guides

- [Getting started](getting-started.md): install the worker and run a first deployment.
- [Configuration](configuration.md): templating, providers, definitions, variables and definitions sources.
- [Running the worker](running.md): the plan, apply and destroy phases, failure handling, limits and saved plans.
- [Hooks](hooks.md): scripts that run before and after each terraform action.
- [State locks](locking.md): waiting for held locks and detecting stale ones.
- [Run reporting](reporting.md): run records and per-definition status written beside each state.

## Reference

These pages are generated from the code with `make docs`; CI fails when they are out of date.

- [CLI reference](reference/cli.md): every command, option and environment variable.
- [Configuration reference](reference/config.md): every configuration key, including handler options.
