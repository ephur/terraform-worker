Commands
========

This section provides an explanation of the **version**, **terraform**, **clean** and **plans** commands
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

\\-\\-config-file
+++++++++++++++++

The **\\-\\-config-file** option specifies the local filesystem path of the configuration
file for the current operation.

Any :ref:`definitions-sources` in the configuration are fetched and run while it is loaded, before the
subcommand's options (such as ``--limit``) are validated.

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
the current operation.  The only acceptable value is ``s3``.

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

\\-\\-apply-failures / \\-\\-no-apply-failures
++++++++++++++++++++++++++++++++++++++++++++++

.. index::
   triple: terraform; options; --no-apply-failures
.. index::
   triple: terraform; options; --apply-failures

The **\\-\\-apply-failures** flag stops the apply or destroy phase at the first definition that fails,
and the definitions after it are not attempted. With **\\-\\-no-apply-failures** every definition with a
plan is attempted, and all failures are reported together when the phase ends. A failing apply or destroy
hook counts as a failure of its definition.

By default, the **\\-\\-apply-failures** option is active.

\\-\\-fail-on-apply-error / \\-\\-no-fail-on-apply-error
++++++++++++++++++++++++++++++++++++++++++++++++++++++++

.. index::
   triple: terraform; options; --no-fail-on-apply-error
.. index::
   triple: terraform; options; --fail-on-apply-error

The **\\-\\-fail-on-apply-error** flag makes the worker exit non-zero when any definition failed to apply
or destroy. **\\-\\-no-fail-on-apply-error** reports the failures and lets the run finish successfully.

By default, the **\\-\\-fail-on-apply-error** option is active.

\\-\\-lock-timeout
++++++++++++++++++

.. index::
   triple: terraform; options; --lock-timeout

The **\\-\\-lock-timeout** option (``WORKER_LOCK_TIMEOUT``) sets how long terraform retries a state lock that
another process holds. It takes seconds or a duration such as ``90s``, ``10m`` or ``1h30m``, and is passed as
``-lock-timeout`` to plan, apply and destroy. Without it, terraform fails at once on a held lock. When the
``lock`` handler is configured, it sets the timeout instead; see :ref:`state-locks`.

\\-\\-plan-ttl
++++++++++++++

.. index::
   triple: terraform; options; --plan-ttl

The **\\-\\-plan-ttl** option (``WORKER_PLAN_TTL``, default ``7d``) sets how long a saved plan may be
applied after it was made, as seconds or a duration such as ``12h`` or ``7d``. An older plan is marked
``expired`` and refused. See :ref:`saved-plans`.

\\-\\-require-plan-manifest
+++++++++++++++++++++++++++

.. index::
   triple: terraform; options; --require-plan-manifest

The **\\-\\-require-plan-manifest** flag (``WORKER_REQUIRE_PLAN_MANIFEST``) refuses to apply a saved plan
that has no manifest, such as one saved by an older worker. Without it, such a plan is applied with a
warning. See :ref:`saved-plans`.

\\-\\-intent
++++++++++++

.. index::
   triple: terraform; options; --intent

The **\\-\\-intent** option (``WORKER_INTENT``) optionally says what the run's plans are for: ``deploy``,
``preview`` or ``drift``. The report handler records it, and a ``preview`` or ``drift`` plan never becomes
a pending plan. With ``--backend-plans``, ``preview`` plans are saved apart under
``<prefix>/_tfworker/previews/`` and never applied by a later run. See :ref:`reporting`.

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

plans
-----

.. index::
   pair: commands; plans

The **plans** command group works on the saved plans of a deployment through their manifests (see
"Saved plans and the apply gate" under :doc:`reporting`). It needs the ``s3`` backend. Each subcommand
takes the **deployment** argument.

plans list
++++++++++

.. index::
   triple: plans commands; options; list

Lists every saved plan record, oldest first: its id, definition, status, change counts, when it was
made and expires, and the run that made it. ``--status`` (repeatable) and ``--definition`` narrow the
list. ``--json`` prints the ``tfworker.plan/v1`` manifests instead.

.. code-block:: bash

   % worker --config-file ./worker.yaml plans list prod --status pending --json

plans discard
+++++++++++++

.. index::
   triple: plans commands; options; discard

Marks one or more pending plans ``discarded`` and removes their plan files, so no run can apply them,
even a run that has already downloaded one. ``--reason`` is required and is kept in each plan's
history. Only a pending plan can be discarded; the command exits non-zero if any plan was not found or
was not pending.

.. code-block:: bash

   % worker --config-file ./worker.yaml plans discard prod 01JD3Z... --reason "superseded by a hotfix"

plans gc
++++++++

.. index::
   triple: plans commands; options; gc

Retires the pending plans that can no longer be applied. It is meant to run as a scheduled job. Each
pending plan is checked in turn:

- **Expired.** The plan is past ``expires_at``. It is marked ``expired``.
- **Orphaned.** The plan was made against a state that no longer exists, or its definition is missing
  from the configuration and has no state. The plan is marked ``discarded``. A definition that is
  missing from the configuration but still has state is only treated as orphaned with
  ``--discard-unconfigured``, so a job run with the wrong configuration does not throw away good plans.
- **Stale.** The state moved since the plan was made. When the bucket is versioned and the state's
  version id still matches the one recorded at plan time, nothing more is read; otherwise the state's
  lineage and serial are compared. A stale plan is marked ``stale``. The reason names the run that
  moved the state when the definition's last recorded apply succeeded and left the state at its current
  serial, and otherwise says the state changed outside a recorded run.

Pending plans are found through the pending index. ``--all-records`` reads every plan record instead,
which also finds preview plans and any plan whose index entry was lost, at the cost of reading the
whole history.

``--applying-timeout`` (for example ``6h``, and more than zero) also marks plans that have been
``applying`` for longer than that ``failed``, which recovers plans left behind by a worker that was
killed mid apply. This pass reads every plan record that has not changed for that long.

Every change is appended to the plan's history, and the plan file is removed. ``--dry-run`` reports
what would be retired and changes nothing. ``--json`` prints a list of objects with ``plan_id``,
``definition``, ``plan_key``, ``record_key``, ``check`` (``orphaned``, ``expired``, ``stale`` or
``applying``), ``status``, ``reason``, ``outcome`` (``planned`` with ``--dry-run``, ``retired``,
``changed`` when another run changed the plan first, or ``error``) and ``error``. gc exits non-zero
when any plan could not be checked or retired.

The ``plans`` commands log to stderr, so their output can be piped, and they never create the backend
bucket or lock table.

.. code-block:: bash

   % worker --config-file ./worker.yaml plans gc prod --applying-timeout 6h
