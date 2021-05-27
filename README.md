# terraform-worker

`terraform-worker` is a command line tool for pipelining terraform operations while sharing state between them. The worker consumese a yaml configuration file which is broken up into two sections, definitions (which were really just top level modules) and sub-modules. The definitions are put into a worker config in order, with the terraform variables, and remote state variables.  Following is a sample configuration file and command:

*./worker.yaml*
```yaml
terraform:
  providers:
    aws:
      vars:
        region: {{ aws_region }}
        version: "~> 2.61.0"

  # global level variables
  terraform_vars:
    region: {{ aws_region }}
    environment: dev

  definitions:
    # Either setup a VPC and resources, or deploy into an existing one
    network:
      path: /definitions/aws/network-existing

    database:
      path: /definitions/aws/rds
```

```sh
% worker --aws-profile default --backend s3 terraform example1
```
**NOTE:** When adding a provider from a non-hashicorp source, use a `source` field, as follows
(_the `source` field is only valid for terraform 13+ and is not emitted when using 12_):

```yaml
providers:
...
  kubectl:
    vars:
      version: "~> 1.9"
    source: "gavinbunney/kubectl"
```

In addition to using command line options, worker configuration can be specified using a `worker_options` section in
the worker configuration.

```yaml
terraform:
  worker_options:
    backend: s3
    backend_prefix: tfstate
    terraform_bin: /home/user/bin/terraform

  providers:
...
```

**terraform-worker** requires a configuration file.  By default, it will looks for a file named "worker.yaml" in the
current working directory.  Together with the `worker_options` listed above, it's possible to specify all options 
either in the environment or in the configuration file and simply call the worker command by itself.

```sh
 % env | grep AWS
 AWS_ACCESS_KEY_ID=somekey
 AWS_SECRET_ACCESS_KEY=somesecret
 % head ./worker.yaml
terraform:
  worker_options:
    backend: s3
    backend_prefix: tfstate
    terraform_bin: /home/user/bin/terraform
 % worker terraform my-deploy
```

## Assuming a Role

The first step in assuming a role is to create the role to be assumed as documented in [Creating a role to delegate permissions to an IAM user ](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-user.html) and then granting permissions to assume the role as documented in [Granting a user permissions to switch roles ](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_permissions-to-switch.html).

To have the worker assume a role once the role and permissions are configured the `--aws-role-arn` and `--aws-external-id` flags need to be provided to the worker along with the credentials for the trusted account.  Since neither the role ARN nor the ExternalId are secret, this allows running under another set of credentials without providing any additional secrets.

## Development

```sh
 # virtualenv setup stuff... and then:
 % pip install poetry && make init
```

## Releasing

Publishing a release to PYPI is done locally through poetry. Instructions on how to configure credentials for poetry can be found [here](https://python-poetry.org/docs/repositories/#configuring-credentials).

Bump the version of the worker and commit the change:
```sh
 % poetry version <semver_version_number>
```

Build and publish the package to PYPI:
```sh
 % poetry publish --build
```

## Configuration

A project is configured through a worker config, a yaml, json, or hcl2 file that specifies the definitions, inputs, outputs, providers and all other necessary configuration. The worker config is what specifies how state is shared among your definitions. The config support jinja templating that can be used to conditionally pass state or pass in env variables through the command line via the `--config-var` option.

*./worker.yaml*
```yaml
terraform:
  providers:
    aws:
      vars:
        region: {{ aws_region }}
        version: "~> 2.61.1"

  # global level variables
  terraform_vars:
    region: {{ aws_region }}
    environment: dev

  definitions:
    # Either setup a VPC and resources, or deploy into an existing one
    network:
      path: /definitions/aws/network-existing

    database:
      path: /definitions/aws/rds
      remote_vars:
        subnet: network.outputs.subnet_id
```

```json
{
    "terraform": {
        "providers": {
            "aws": {
                "vars": {
                    "region": "{{ aws_region }}",
                    "version": "~> 2.61"
                }
            }
        },
        "terraform_vars": {
            "region": "{{ aws_region }}",
            "environment": "dev"
        },
        "definitions": {
            "network": {
                "path": "/definitions/aws/network-existing"
            },
            "database": {
                "path": "/definitions/aws/rds",
                "remote_vars": {
                    "subnet": "network.outputs.subnet_id"
                }
            }
        }
    }
}
```

```hcl
terraform {
  providers {
    aws = {
      vars = {
        region = "{{ aws_region }}"
        version = "2.63.0"
      }
    }
  }

  terraform_vars {
    environment = "dev"
    region = "{{ aws_region }}"
  }

  definitions {
    network = {
      path = "/definitions/aws/network-existing"
    }

    database = {
      path = "/definitions/aws/rds"

      remote_vars = {
        subnet = "network.outputs.subnet_id"
      }
    }
  }
}
```

In this config, the worker manages two separate terraform modules, a `network` and a `database` definition, and shares an output from the network definition with the database definition. This is made available inside of the `database` definition through the `local.subnet` value.

`aws_region` is substituted at runtime for the value of `--aws-region` passed through the command line.

## Troubleshooting

Running the worker with the `--no-clean` option will keep around the terraform files that the worker generates. You can use these generated files to directly run terraform commands for that definition. This is useful for when you need to do things like troubleshoot or delete items from the remote state. After running the worker with --no-clean, cd into the definition directory where the terraform-worker generates it's tf files. The worker should tell you where it's putting these for example:

```
...
building deployment mfaitest
using temporary Directory: /tmp/tmpew44uopp
...
```

In order to troubleshoot this definition, you would cd /tmp/tmpew44uopp/definitions/my_definition/ and then perform any terraform commands from there.

## Background

The terraform worker was a weekend project to run terraform against a series of definitions (modules). The idea was the configuration vars, provider configuration, remote state, and variables from remote state would all be dynamically generated. The purpose was for building kubernetes deployments, and allowing all of the configuration information to be stored as either yamnl files in github, or have the worker configuration generated by an API which stored all of the deployment configurations in a database.

## Documentation

Documentation uses the [Sphinx](https://www.sphinx-doc.org/en/master/index.html) documentation fromework.

To build HTML documentation:

```bash
% cd docs
% make clean && make html
```

The documentation can be viewed locally by open `./docs/build/index.html` in a browser.
