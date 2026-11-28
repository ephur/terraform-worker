# Configuration

The worker reads one or more configuration files (`--config-file`, default `./worker.yaml`). Everything lives under a top-level `terraform` key. Files ending in `.hcl` are parsed as HCL; anything else is parsed as YAML, which includes JSON. When several files are given, they are merged in order and later files win.

Every key is listed in the [configuration reference](reference/config.md). This page explains how they work together.

## Templating

Each file is rendered with [Jinja](https://jinja.palletsprojects.com/) before it is parsed. These names are available:

| Name | Value |
| --- | --- |
| `deployment` | The deployment being run, the `DEPLOYMENT` argument of `terraform`, `clean` and `env`. |
| Root option names, such as `aws_region` or `backend_bucket` | The value of each root CLI option that is a string. |
| `var.<key>` | Each `--config-var key=value`. The key is also available on its own, as `<key>`. |
| `env.<NAME>` | The worker's environment variables. |

An undefined name is an error, so guard optional values with `is defined` or `default`:

```yaml
terraform:
  definitions:
    blue:
      path: /definitions/charts
      terraform_vars:
        name: {{ env.NAME_VAR | default("alpha") }}
        {% if var.live_data is defined and var.live_data == "true" %}
        data_source: mysql
        {% else %}
        data_source: sqlite
        {% endif %}
```

`deployment` always wins over a `--config-var deployment=...`.

## Worker options

`worker_options` sets defaults for CLI options, using the option name with underscores. An option given on the command line or in its environment variable overrides the file.

```yaml
terraform:
  worker_options:
    backend: s3
    backend_bucket: my-terraform-state
    backend_prefix: terraform/state/{deployment}
    terraform_bin: /usr/local/bin/terraform
```

With the options in the file and credentials in the environment, `worker terraform my-deploy --plan` is all that is needed.

## Providers

Each entry under `providers` describes one provider: `requirements` for the `required_providers` block, and `vars` and `config_blocks` for its `provider` block.

```yaml
terraform:
  providers:
    aws:
      requirements:
        version: ">= 5.0"
      vars:
        region: {{ aws_region }}
      config_blocks:
        default_tags:
          tags:
            managed_by: terraform-worker
      aliases:
        east:
          vars:
            region: us-east-1
    kubectl:
      requirements:
        source: gavinbunney/kubectl
        version: "~> 1.14"
```

`source` defaults to `hashicorp/<name>` on the public registry. Each alias becomes a second `provider` block with `alias` set, starting from the provider's own `vars` and `config_blocks` and overriding them with the alias's values. The worker downloads each provider once into a provider cache (a directory in the working directory, or `--provider-cache`) and writes a lock file for each definition.

## Definitions

A definition is a terraform root module that runs as its own state, at `<backend prefix>/<definition name>`. Definitions run in the order they are listed, for apply and destroy alike.

```yaml
terraform:
  definitions:
    network:
      path: /definitions/aws/network
    database:
      path: git@github.com:example/terraform-modules.git
      remote_path_options:
        branch: v2.1.0
        sub_path: rds
      remote_vars:
        subnet_ids: network.outputs.private_subnet_ids
```

`path` is either a git URL or a local directory. A local path is looked up under `--repository-path` (the current directory by default) first, even when it starts with `/`, and used as given when it is not there. For git, `branch` picks the branch or tag to clone (`master` when unset) and `sub_path` the directory inside the repository.

The worker copies each definition into its working directory and adds the files it generates:

- `worker_generated_terraform.tf`: the backend, a `provider` block for each configured provider the definition uses, and a `terraform_remote_state` data source for each state it reads. When the definition has no `required_providers` of its own, the worker adds one listing every configured provider.
- `worker_generated.auto.tfvars.json`: the definition's `terraform_vars`.
- `worker_generated_locals.tf`: a `locals` block with the definition's `remote_vars`.

Files ending in `.tf.j2` are rendered with Jinja and saved without the `.j2`. In them, `var.<name>` holds the definition's `template_vars` (merged with the global ones) and the `--config-var` values, and `env.<NAME>` the environment. A `hooks` directory holds [hook scripts](hooks.md).

### Terraform variables

`terraform_vars` become terraform input variables through `worker_generated.auto.tfvars.json`. A definition's values keep their YAML types (global ones must be strings, booleans, lists or maps), and strings are written as they are: quotes, backslashes, `${` and `%{` need no escaping. Each definition still declares the variables it uses.

### Remote variables

`remote_vars` read another definition's outputs from its state and expose them as locals. The value is `<definition>.outputs`, optionally followed by a key path:

```yaml
remote_vars:
  vpc_id: network.outputs.vpc_id               # one output
  db_host: database.outputs.endpoint.host      # a nested key
  all_network: network.outputs                 # every output, as a map
  ids: [network.outputs.vpc_id, network.outputs.subnet_id]
  by_name:
    vpc: network.outputs.vpc_id
```

The definition uses them as `local.vpc_id`, `local.ids` and so on. The referenced definition's state is read from the backend, so it does not need to be part of the current run. `worker terraform --backend-use-all-remotes` adds a data source for every state in the deployment, not only the referenced ones.

### Global variables

`global_vars` holds `terraform_vars`, `remote_vars` and `template_vars` that are merged into every definition. A definition's own value wins over a global with the same name. A definition can opt out with `ignore_global_vars: true`, leave out specific names with the `ignored_global_*_vars` lists, or take only the names listed in `use_global_*_vars`.

```yaml
terraform:
  global_vars:
    terraform_vars:
      environment: dev
      region: {{ aws_region }}
  definitions:
    audit:
      path: /definitions/audit
      ignored_global_terraform_vars: [region]
```

## Definitions sources

A definitions source generates definitions when the configuration loads, instead of listing them by hand.

```yaml
terraform:
  definitions_sources:
    catalog:
      path: git@github.com:example/service-catalog.git
      remote_path_options:
        branch: main
      command: bin/list-definitions
      after: network

  definitions:
    network:
      path: /definitions/aws/network
    database:
      path: /definitions/aws/rds
```

If `bin/list-definitions` prints:

```yaml
queue: {}
api:
  remote_vars:
    subnet: network.outputs.subnet_id
```

the definitions run in the order `network`, `queue`, `api`, `database`, and `queue` and `api` are fetched from the catalog repository on branch `main`.

Sources are processed for every command that loads the configuration (`terraform`, `clean` and `env`), after all files are rendered and merged and before the configuration is validated. Generated definitions are treated like configured ones everywhere, including by `--limit` and handlers.

For each source, in configuration order:

1. The source is copied to a temporary directory, which is removed once its command has run, whether it succeeds or fails. Nothing is written to the working directory. With `sub_path`, only that subdirectory is copied.
2. `command` runs in the copy. It is rendered with Jinja like the rest of the configuration, then split into arguments shell-style and run without a shell, so pipes and redirection are not available. A relative executable such as `bin/list-definitions` resolves against the copy and must be executable. The command inherits the worker's environment, plus `WORKER_DEPLOYMENT` set to the deployment.
3. Standard output is parsed as YAML (or JSON): a mapping of definition name to definition body. Empty output generates no definitions, and a null body is treated as `{}`. Standard error is logged at debug level.
4. A generated definition without its own `path` gets the source's `path` and, if it has no `remote_path_options` of its own, the options the source sets.
5. The definitions are inserted, in the order printed, right after the `after` definition, or at the end when `after` is not set. `after` may name a definition generated by an earlier source.

Deciding what counts as an error is the command's job; the worker only checks its exit code. The worker stops with exit code 1, naming the source, when:

- the source cannot be fetched (clone failure, unknown path type, missing local path);
- the command cannot be run, or exits non-zero (the error includes its exit code and standard error);
- the output is not YAML or JSON, or is not a mapping of names to mappings;
- `after` names a definition that does not exist;
- a generated name matches a configured definition, or is generated by two sources;
- a source entry has an unknown key, or is missing `path` or `command`.

The command runs with the worker's environment and credentials. Only point a definitions source at repositories you trust as much as the configuration itself.

## Handlers

`handlers` turns on integrations that run around each terraform action: reporting to GitHub, Slack, Datadog, Bitbucket or SQS, plan summaries from OpenAI, and Snyk or Trivy scans of each plan. Each key is a handler name, and its value holds that handler's options; see [handlers in the configuration reference](reference/config.md#handlers). The `s3` handler is always on and stores plans in the state bucket when `--backend-plans` is set.
