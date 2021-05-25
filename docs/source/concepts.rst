Configuration Concepts
======================

The **terraform-worker** is a terraform wrapper which emphasizes configuration simplicity for 
complex orchestrations.  The **terraform-worker** works by reading a configuration of terraform
provider, variable and module :ref:`definitions`, gathering provider plugins and remote terraform
sources, and then serially executing the terraform operations in a local temporary directory. The
**terraform-worker** supports passing orchestration values through a pipeline of terraform operations
via the `data\.terraform_remote_state <https://www.terraform.io/docs/language/state/remote-state-data.html>`_
data source. This section examines **terraform-worker** concepts of :ref:`rendering`, :ref:`definitions`,
:ref:`terraform-vars`, :ref:`remote-vars`, and :ref:`provider-configurations`.

.. contents:: On this page
   :depth: 3

.. index::
   single: jinja templates

.. _rendering:

Rendering with Jinja templates
-------------------------------

**terraform-worker** configurations are pre-processed using the `Jinja <https://jinja.palletsprojects.com/en/2.11.x/>`_
templating language. This allows interpolation of values passed in from the command line.

.. note:: 

   Expansion of a variable passed in from the CLI:

   .. code-block:: yaml
      :emphasize-lines: 5

      providers:
        aws:
          vars:
            version: ">= 3.16.0"
            region: {{ aws_region }}

**terraform-worker** supports a special ``env`` object for interpolating values passed in the environment.

.. note::

   Expansion of a variable passed in from the environment:

   .. code-block:: yaml
      :emphasize-lines: 4-5

      definitions:
        blue:
          terraform_vars:
            name: {{ env.NAME_VAR|default("alpha") }}
            tag: {{ env.TAG_VAR|default("beta") }}

Pre-processing with Jinja allows for the use of conditional blocks. Conditional blocks might be keyed on a
:ref:`config-var` passed via the CLI.

.. note::

   Following is an example of Jinaja conditional blocks applied to terraform variable configuration.

   .. code-block:: jinja
      :emphasize-lines: 4-8

      definitions:
        blue:
          path: /definitions/charts
          terraform_vars:
            {% if env.CHART_HOME is defined and env.CHART_HOME %}
            chart_base_path: "{{ env.CHART_HOME }}/helm-charts"
            {% else %}
            chart_base_path: "{{ env.HOME }}/helm-charts"
            {% endif %}
            homedir: {{ env.HOME }}

.. index::
   single: worker options

.. _worker-options:

Stipulating options in the configuration file
---------------------------------------------

In addition to using command line options, worker configuration can be specified using a ``worker_options`` section in
the worker configuration.

.. code-block:: yaml

    terraform:
      worker_options:
        backend: s3
        backend_prefix: tfstate
        terraform_bin: /home/user/bin/terraform

      providers:
      ...

**terraform-worker** requires a configuration file.  By default, it will looks for a file named "worker.yaml" in the
current working directory.  Together with the ``worker_options`` listed above, it's possible to specify all options 
either in the environment or in the configuration file and simply call the worker command by itself.

.. code-block:: bash

    % env | grep AWS
    AWS_PROFILE=a-great-profile

    % head ./worker.yaml
    terraform:
      worker_options:
        backend: s3
        backend_prefix: tfstate
        terraform_bin: /home/user/bin/terraform

    # The following command does not pass apply, so tf operations are only planned.
    % worker terraform my-deploy --no-clean

.. index::
   single: provider configurations

.. _provider-configurations:

Provider Configurations
-----------------------

A **terraform-worker** configuration must include information about the providers that are used by the
definitions. The **terraform-worker** uses this information to download all plugins locally and then
passes the local path to each terraform operation.

Provider configurations typically include the version and any other configuration variables the provider
may require. These values should be declared in a ``vars`` dictionary as an immediate child of provider-named
dictionary.

.. note::

    Following is a ``providers`` snippet from a configuration.

    .. code-block:: yaml
       :emphasize-lines: 2-9

       terraform:
         providers:
           aws:
             vars:
               version: ">= 3.16.0"
               region: {{ aws_region }}
           'null':
             vars:
               version: ">= 3.0.0"

    Following is how the ``region`` variable from the **aws** provider configuration listed above is rendered
    in the terraform ``provider`` block.

    .. code-block:: terraform

        provider "aws" {
          region = "us-west-2"
        }

If the provider is not available from the hashicorp registry, it is also possible to explicitly stipulate
the provider download location using a ``baseURL`` field in the provider dictionary.

.. note::

   Following is an example of a ``baseURL`` configuration.

   .. code-block:: yaml
      :emphasize-lines: 4

      terraform:
        providers:
          kubectl:
            baseURL: https://github.com/gavinbunney/terraform-provider-kubectl/releases/download/v1.9.4
            vars:
              version: "1.9.4"

.. index::
   single: definition

.. _definitions:

Definitions
-----------

A **terraform-worker** configuration is comprised of one or more definition statements. Conceptually, a 
**definition** may refer to either the statement in the configuration, or a collection of terraform and 
supporting files on a file system, or in a git repository. In general, these latter **definitions** are
lightweight.  They are mainly involved aggregating the parameters that will be supplied to an underlying
terraform module as inputs.

.. _definition-statements:

Definition Statements
+++++++++++++++++++++

A **definition statement** is `key` in a :ref:`definitions` object in a **terraform-worker** configuration.
A **definition statement** must include a `key` which defines either a locally relative :ref:`filesystem-definition`
or a path to a git repository.

.. note:: Following is an example of a definitions statement, "ami". 

   .. code-block:: yaml

      definitions:
        ami:
          path: /definitions/new-ami
          terraform_vars:
            name: {{ env.NAME_VAR|default("alpha") }}
            tag: {{ env.TAG_VAR|default("beta") }}

.. _filesystem-definition:

Filesystem Definition
+++++++++++++++++++++

A **filesystem definition** refers to a directory which includes a terraform root module.  Optionally, it may also
include a :ref:`hooks` directory.

.. note::

   Following is the directory tree of a sample definition.

   .. code-block:: bash

      definitions/new-ami
      ├── README.md
      ├── hooks
      │   ├── images
      │   │   └── image.pkr.hcl
      │   └── scripts
      │       └── setup.sh
      ├── main.tf
      └── outputs.tf

.. index::
   single: terraform_vars

.. _terraform-vars:

Terraform Variables
-------------------

The ``terraform_vars`` field  in a **terraform-worker** configuration is used to express an input
variables or local variables for a terraform module. Values which appear in this block are passed to
the underlying terraform operation in a ``worker.auto.tfvars`` file.

.. note::

   Following is a ``terraform_vars`` snippet from a configuration.

   .. code-block:: yaml
      :emphasize-lines: 5-7

      terraform:
        ...
        definitions:
          blue:
            terraform_vars:
              name: alpha
              tag: beta
      ...

   Following is how this value appears in the terraform execution environment.

   .. code-block:: bash

      % pwd
      /tmp/fhgwjxkt/definitions/blue
      % cat worker.auto.tfvars
      name = "alpha"
      tag = "beta"

.. index::
   single: remote_vars

.. _remote-vars:

Remote Variables
----------------

A ``remote_vars`` field in a **terraform-worker** configuration is used to express input or local
variables that will be supplied from terraform's backend state store.

.. note::

   Following is a ``remote_vars`` snippet from a configuration.

   .. code-block:: yaml
      :emphasize-lines: 10,11

      ...
      terraform:
        ...
        definitions:
          tagging:
            # This definition includes an output value for tagmap
            path: /definitions/tagging

          blue:
            remote_vars:
              tags: tagging.output.tagmap
      ...

   Following is how this value appears in the terraform execution environment.

   .. code-block:: bash

      % pwd
      /tmp/tsgsdh6t/definitions/blue
      % cat worker-locals.tf
      locals {
        tags = data.terraform_remote_state.tagging.output.tagmap
      }

.. index::
   single: terraform modules

.. _terraform-modules:

Terraform Modules
-----------------

The **terraform-worker** can be made aware of a local repository of terraform-modules.  Locally defined
terraform modules are copied into the same directory as a **terraform-worker** definition, so that they
are available within a definition's terraform code at the path: ``./terraform-modules``.

The location of a local repository of terraform-modules can be specified using the
:ref:`terraform-modules-dir` command line option.

