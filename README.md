# terraform-worker

**terraform-worker** is a command-line tool purpose-built for orchestrating large-scale, modular Terraform deployments. It enables precise control over provider versions, remote state scaffolding, and shared configuration logic — ideal for enterprise infrastructure teams managing complex environments across multiple workspaces and deployment stages.

Unlike tools such as `terragrunt`, this is not a drop-in wrapper — `terraform-worker` introduces a new structure and workflow model, optimized for maintainability, scalability, and declarative control, and integration with CI/CD pipelines.

---

## 🚀 Features

- 🔧 Modularized state management across nested or sequential Terraform operations
- 🧱 Automated scaffolding of provider blocks, remote state configs, and input variables
- ✨ Jinja2 templating for flexible, environment-specific rendering
- 🔒 Enforces version consistency of modules and providers
- 🧰 Supports YAML, JSON, and HCL2 for configuration definitions
- 📦 Clean CLI with pluggable backend and runtime overrides
- 🔍 Designed from the ground up for enterprise environments

---

## 🔄 Example Workflow

Here's a minimal example using `worker.yaml` to scaffold a VPC and RDS database with shared state:

```yaml
terraform:
  providers:
    aws:
      vars:
        region: {{ aws_region }}
        version: "~> 4.0"

  terraform_vars:
    region: {{ aws_region }}
    environment: dev

  definitions:
    network:
      path: /definitions/aws/network-existing

    database:
      path: /definitions/aws/rds
      remote_vars:
        subnet: network.outputs.subnet_id
```

Run the deployment:

```sh
% worker --aws-profile default --config-var aws_region=us-west-2 terraform deploy
```

Use custom worker options for flexibility:

```yaml
terraform:
  worker_options:
    backend: s3
    backend_prefix: tfstate
    terraform_bin: /usr/local/bin/terraform
```

---

## 📌 Configuration Notes

- Config files may be written in YAML, JSON, or HCL2
- Configs support templating via [Jinja2](https://jinja.palletsprojects.com/)
- `terraform-worker` reads `worker.yaml` by default from the current directory
- Variables like `{{ aws_region }}` are injected via `--config-var` CLI args

---

## 🧠 How It Works

1. Parses your configuration into **definitions** (state modules)
2. Injects provider/version/variable blocks automatically
3. Handles remote state linking across module outputs
4. Creates ephemeral execution directories (unless `--no-clean` is used)
5. Delegates to native Terraform for execution (`init`, `plan`, `apply`, etc.)

---

## 🧪 Development & Testing

Initialize the environment:

```bash
poetry install
make init
```

Run tests:

```bash
pytest
```

---

## 📤 Releasing

Update the version and publish to PyPI:

```bash
poetry version <semver>
poetry publish --build
```

Configure credentials for Poetry using [these instructions](https://python-poetry.org/docs/repositories/#configuring-credentials).

---

## 🛠 Troubleshooting

Use `--no-clean` to retain generated terraform files:

```sh
worker --no-clean terraform deploy
```

The tool will print the temporary directory it used, e.g.:

```text
using temporary Directory: /tmp/tmpew44uopp
```

You can `cd` into the generated definition to run manual Terraform commands for debugging.

---

## 🪝 Hook Scripts

Each Terraform action (`init`, `plan`, `apply`) can be extended via optional lifecycle hook scripts, enabling advanced custom logic around deployments.

### Supported Hook Types

For every definition, the following scripts are recognized and executed if present:

- `pre_init`, `post_init`
- `pre_plan`, `post_plan`
- `pre_apply`, `post_apply`

Scripts must be placed within the corresponding definition directory.

### Language & Execution

Hook scripts can be written in **any language** — they are executed as standalone shell commands. Ensure the script has execution permissions (e.g., `chmod +x`).

Example:
```bash
#!/usr/bin/env python3
import os
print("Pre-apply hook running for", os.environ.get("TF_WORKER_DEFINITION"))
```

### Environment Variable Access

The following environment variables are automatically injected into all hook scripts:

- `TF_`-prefixed variables for:
  - Terraform input variables (`TF_REGION`, `TF_ENVIRONMENT`, etc.)
  - Rendered template variables (e.g., Jinja context)
  - Remote state outputs
- Authentication credentials (e.g., `AWS_ACCESS_KEY_ID`, `GOOGLE_APPLICATION_CREDENTIALS`, etc.)

This allows hook scripts to dynamically read and interact with Terraform configuration and authentication context.

### Use Cases

- Pre-checks or assertions before deploys
- Dynamic secrets injection
- Notifications or logging
- External system integration (e.g., Slack, monitoring)

Hook scripts provide a powerful extension point for customizing behavior around each Terraform stage without modifying core logic.

---

## 🔒 Assuming a Role (AWS)

To execute with a specific IAM role:

```sh
worker --aws-role-arn arn:aws:iam::1234567890:role/example \
       --aws-external-id my-id \
       terraform deploy
```

Configure the role and trust relationship per [AWS IAM guidelines](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-user.html).

---

## 📚 Documentation

Full documentation is built using [Sphinx](https://www.sphinx-doc.org/en/master/):

```bash
cd docs
make html
```

Open the local docs via:

```sh
open ./docs/build/index.html
```

---

## 🧭 Design Philosophy

**terraform-worker** was created from the ground up for production-first use in enterprise CI/CD systems, including:
- Large deployments with complex dependency graphs
- Strict version controls and reproducibility
- Integration with GitHub or API-driven configuration management

While it excels at scale, it’s also a powerful CLI for individuals who need structure without sacrificing flexibility.

---
## 🔗 License

Apache License 2.0 — see [LICENSE](./LICENSE) for details.
