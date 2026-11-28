# Getting started

## Install

The worker is published on PyPI as `terraform-worker` and installs a `worker` command.

```sh
uv tool install terraform-worker
# or
pip install --user terraform-worker
```

It needs Python 3.11 or later and a `terraform` binary, either on `PATH` or passed with `--terraform-bin`. State is stored in S3, so the worker also needs AWS credentials that can read and write the state bucket.

## A first deployment

A deployment is a set of definitions run against one state prefix. Each definition is a directory holding a terraform root module. The worker writes the provider, backend and remote state blocks itself, so a definition only holds its resources, variables and outputs.

Create two definitions:

```hcl
# definitions/tags/main.tf
variable "environment" {}

output "tag_map" {
  value = {
    environment = var.environment
    managed_by  = "terraform-worker"
  }
}
```

```hcl
# definitions/bucket/main.tf
variable "name" {}

resource "aws_s3_bucket" "this" {
  bucket = var.name
  tags   = local.tags
}
```

`local.tags` in the bucket definition is not declared anywhere: the worker generates it from `remote_vars` in the configuration below.

Then a `worker.yaml` next to them:

```yaml
terraform:
  worker_options:
    backend: s3
    backend_bucket: my-terraform-state
    backend_region: us-west-2

  providers:
    aws:
      requirements:
        version: ">= 5.0"
      vars:
        region: {{ aws_region }}

  global_vars:
    terraform_vars:
      environment: dev

  definitions:
    tags:
      path: ./definitions/tags
    bucket:
      path: ./definitions/bucket
      terraform_vars:
        name: {{ deployment }}-example-bucket
      remote_vars:
        tags: tags.outputs.tag_map
```

Plan the deployment, named `example`:

```sh
worker --aws-profile my-profile --aws-region us-west-2 terraform example --plan
```

Each definition is initialized and planned in order. Nothing is applied until you ask for it:

```sh
worker --aws-profile my-profile --aws-region us-west-2 terraform example --plan --apply
```

To tear it down again, plan a destroy and run it. Definitions are destroyed in reverse order:

```sh
worker --aws-profile my-profile --aws-region us-west-2 terraform example --plan-destroy --destroy
```

## Looking at the generated files

The worker builds each definition in a temporary directory that is removed when it exits. To keep the files, pass an empty directory with `--working-dir`. Each definition is under `definitions/<name>/` there, and you can run terraform in it directly, for example to import or remove state. `worker env <deployment>` prints the same credentials the worker uses, ready to `eval` in that shell.

Next: [Configuration](configuration.md) and [Running the worker](running.md).
