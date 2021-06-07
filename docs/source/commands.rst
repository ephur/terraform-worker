Commands
========

This section provides an explanation of the **version**, **terraform**, and **clean** commands
to **terraform-worker**.

.. contents:: On this page
   :depth: 3

Root command options
--------------------

The following commands are all available on the root **terraform-worker** command.

\\-\\-aws-access-key-id
+++++++++++++++++++++++

The **\\-\\-aws-access-key-id** option specifies the **AWS_ACCESS_KEY_ID** configuration value
that is to be used for the underlying terraform operations.

.. index::
   triple: worker; options; --aws-access-key-id

\\-\\-aws-secret-access-key
+++++++++++++++++++++++++++

The **\\-\\-aws-secret-access-key** option specifies the **AWS_SECRET_ACCESS_KEY** configuration
value that is to be used for the underlying terraform operations.

.. index::
   triple: worker; options; --aws-secret-access-key

\\-\\-aws-session-token
+++++++++++++++++++++++

The **\\-\\-aws-session-token** option specifies the **AWS_SESSION_TOKEN** configuration value
that is to be used for the underlying terraform operations.

.. index::
   triple: worker; options; --aws-session-token

\\-\\-aws-role-arn
++++++++++++++++++

The **\\-\\-aws-role-arn** option specifies the **AWS_ROLE_ARN** configuration value that is
to be used for the underlying terraform operations.

.. index::
   triple: worker; options; --aws-role-arn

\\-\\-aws-region
++++++++++++++++

The **\\-\\-aws-region** option specifies the **AWS_DEFAULT_REGION** configuration value that
is to be used for the underlying terraform operations.

.. index::
   triple: worker; options; --aws-region

\\-\\-aws-profile
+++++++++++++++++

The **\\-\\-aws-profile** option specifies the **AWS_PROFILE** configuration value that is to
be used for the underlying terraform operations.

.. index::
   triple: worker; options; --aws-profile

\\-\\-gcp-region
++++++++++++++++

The **\\-\\-gcp-region** option specifies the **REGION** configuration value for the GCP
region that is to be used for the underlying terraform operations.

.. index::
   triple: worker; options; --gcp-region

\\-\\-gcp-creds-path
++++++++++++++++++++

The **\\-\\-gcp-creds-path** option specifies the local filesystem path for the credentials
that are to be used for the underlying terraform operations.

.. index::
   triple: worker; options; --gcp-creds-path

\\-\\-gcp-poject
++++++++++++++++

The **\\-\\-gcp-project** option sepcifies the google project id that is to be used for the
underlying terraform operations.

.. index::
   triple: worker; options; --gcp-project

\\-\\-config-file
+++++++++++++++++

The **\\-\\-config-file** option specifies the local filesystem path of the configuration
file for the current operation.

.. index::
   triple: worker; options; --config-file

\\-\\-repository-path
+++++++++++++++++++++

The **\\-\\-repository-path** option specifies the local filesystem path of the repository
containing terraform modules.

By default, this value is the current working directory.

.. index::
   triple: worker; options; --repository-path

\\-\\-backend
+++++++++++++

The **\\-\\-backend** option specifies which type of terraform backend should be used in
the current operation.  Acceptable values are: ``gcs`` or ``s3``.

.. index::
   triple: worker; options; --backend

.. _backend-prefix:

\\-\\-backend-prefix
++++++++++++++++++++

The **\\-\\-backend-prefix** option specifies the prefix under which terraform state values
will be stored for the current operation.

By default, this value is ``terraform/state/<deployment>``.

.. seealso::
   | The terraform command's :ref:`deployment <terraform_deployment>` option.
   | The clean command's :ref:`deployment <clean_deployment>` option.

.. index::
   triple: worker; options; --backend-prefix

\\-\\-backend-region
++++++++++++++++++++

The **\\-\\-backend-region** option specifies the region where the backend lock file
exists.

.. index::
   triple: worker; options; --backend-region

.. _config-var:

\\-\\-config-var
++++++++++++++++

The **\\-\\-config-var** option specifies the key=value to be supplied as jinja variables when
rendering a **terraform-worker** configuration. Key/value pairs specified in this way are
namedspaced in a **var** dictionary when they are referenced from a Jinja expression.

This option can be specified multiple times.

.. note::

    Following is an example using a **\\-\\-config-var** option.

    .. code-block:: bash

        % worker --config-file ./worker.yaml --config-var live_data=true terraform --apply

    Following is an example of accessing the **\\-\\-config-var** within a Jinja expression.

    .. code-block:: jinja
       :emphasize-lines: 5-9

       definitions:
         blue:
           path: /definitions/charts
           terraform_vars:
             {% if var.live_data is defined and var.live_data %}
             data_source: mysql
             {% else %}
             data_source: sqlite
             {% endif %}
 
.. index::
   triple: worker; options; --config-var

version
-------

.. index::
   pair: commands; version

The **version** command provides the semantic version information for **terraform-worker**.

.. code-block:: bash

   % worker version
   terraform-worker version 0.10.1

terraform
---------

.. index::
   pair: commands; terraform

The **terraform** command is used to initialize the terraform definition calls expressed in the
configuration.  The **terraform** command supports the following arguments.

\\-\\-clean / \\-\\-no-clean
++++++++++++++++++++++++++++

.. index::
   triple: terraform; options; --no-clean
.. index::
   triple: terraform; options; --clean

The **\\-\\-no-clean** flag will prevent the temporary directory where terraform operations are executed
from being deleted when **terraform-worker** command completes.  The **\\-\\-clean** option will cause
the temporary directory to be deleted.

By default, the **\\-\\-clean** option is active.

.. _terraform-apply-no-apply:

\\-\\-apply / \\-\\-no-apply
++++++++++++++++++++++++++++

.. index::
   triple: terraform; options; --no-apply
.. index::
   triple: terraform; options; --apply

The **\\-\\-no-apply** flag will cause the operations for each terraform definition to only execute
``terraform plan``.  The **\\-\\-apply** flag will cause ``terraform apply`` to be executed.

By default, the **\\-\\-no-apply** option is active.

\\-\\-force / \\-\\-no-force
++++++++++++++++++++++++++++

.. index::
   triple: terraform; options; --no-force
.. index::
   triple: terraform; options; --force

The **\\-\\-no-force** flag will omit the ``-force`` option from a ``terraform apply`` or ``terraform destroy`` operation.
``terraform plan``.  The **\\-\\-force** flag will cause the ``-force`` option to be included in ``terraform apply`` and 
``terraform destory`` operations.

\\-\\-destroy / \\-\\-no-destroy
++++++++++++++++++++++++++++++++

.. index::
   triple: terraform commands; options; --no-destroy
.. index::
   triple: terraform commands; options; --destroy

The **\\-\\-no-destroy** flag will prevent each terraform definition from executing ``terraform destroy``.  The **\\-\\-destroy**
flag will cause ``terraform destroy`` to be executed. ``destroy`` will only be called when ``--destroy`` is passed, so
``--no-destroy`` has no effect.

\\-\\-show-output / \\-\\-no-show-output
++++++++++++++++++++++++++++++++++++++++

.. index::
   triple: terraform commands; options; --no-show-output
.. index::
   triple: terraform commands; options; --show-output

The **\\-\\-show-output** flag will cause verbose output from the underlying terraform operations to be written to standard out
of the **terraform-worker** process.

\\-\\-terraform-bin
+++++++++++++++++++

.. index::
   triple: terraform commands; options; --terraform-bin

The **\\-\\-terraform-bin** option allows a user to specify a specific terraform binary.

.. code-block:: bash

   % worker terraform --apply --terraform--bin ~/apps/terraform

.. _base-64-option:

\\-\\-b64-encode-hook-values / \\-\\-no-b64-encode-hook-values
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

.. index::
   triple: terraform commands; options; --no-b64-encode-hook-values
.. index::
   triple: terraform commands; options; --b64-encode-hook-values

The **\\-\\-b64-encode-hook-values** flag will cause variable and output values that are made available to **terraform-worker**
hooks to be base64 encoded.  This is useful since these values can be complex data structures that are not easily escaped
in an environment variable.

.. seealso::
   :doc:`./hooks`

.. _terraform-modules-dir:

\\-\\-terraform-modules-dir
+++++++++++++++++++++++++++

.. index::
   triple: terraform commands; options; --terraform-modules-dir

The **\\-\\-terraform-modules--dir** option allows a user to specify a local directory where terraform-modules can be found.
If this value is not set, the location is assumed to be ``./terraform-modules``.

.. seealso::
   :ref:`terraform-modules`

.. _terraform-limit:

\\-\\-limit
+++++++++++

.. index::
   triple: terraform commands; options; --limit

The **\\-\\-limit** option is a repeatable option which allows a user to limit terraform operations to only specific
configuration definitions.

This option can be specified multiple times.

.. code-block:: bash

   % worker terraform --apply --limit alpha --limit omega

.. _terraform_deployment:

deployment
++++++++++

The **deployment** argument specifies the name of the deployment to be used for the current operation. This value is used
in as a part of the :ref:`backend-prefix` bucket key. A valid deployment value is no more than 16 characters.

clean
-----

.. index::
   pair: commands; clean

The **clean** command is used to initiate operations related to removing artifacts left over
from previous runs of **terraform-worker**.  For example, for a **terraform-worker** configuration
that uses an AWS/S3 backend store, the **clean** command will remove the DynamoDB tables associated
with the backend's locking mechanism.

\\-\\-limit
+++++++++++

.. index::
   triple: clean commands; options; --limit

The **\\-\\-limit** option is a repeatable option which allows a user to limit clean operations to only specific
configuration definitions.

This option can be specified multiple times.

.. code-block:: bash

   % worker --config-file ./worker.yaml clean --apply --limit alpha --limit omega

.. _clean_deployment:

deployment
++++++++++

The **deployment** argument specifies the name of the deployment to be used for the current operation. This value is used
in as a part of the :ref:`backend-prefix` bucket key. A valid deployment value is no more than 16 characters.
