# tfworker-console

A web console for the runs, plans and locks that terraform-worker records
beside each deployment's state. It reads and curates that data; it never runs
terraform. It lives in the terraform-worker repository as its own package, so
installing `terraform-worker` never pulls in web dependencies.

Develop from the repository root:

    uv sync --package tfworker-console
    uv run --package tfworker-console tfworker-console serve --reload

Settings come from `TFWORKER_CONSOLE_*` environment variables, for example
`TFWORKER_CONSOLE_PORT=8080`.
