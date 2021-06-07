Quick Start
===========

This section provides a quick sample of getting **terraform-worker** up and running.
The configuration from this section references remote :ref:`definitions <definitions>`
hosted on github.

.. warning::

   As with any code pulled from the public internet it is probably a good idea to 
   review the terraform that will be executed ahead of time.

Following is a sample configuration file.  Copy the contents of the code block below and
save to a local file named ``tfw.yaml``.

.. code-block:: yaml
   :linenos:

    terraform:
      # global level variables, these are supplied to all definitions
      terraform_vars:
        # a variable named: `deployment` is populated always, matching the name of the deployment passed on CLI
        region: {{ aws_region }}
        environment: dev

      definitions:
        tags:
          path: git@github.com:ephur/terraform-worker-examples.git
          remote_path_options:
            sub_path: definitions/quickstart/misc-tags

        # network creates a VPC and required resources to launch instances
        network:
          path: git@github.com:ephur/terraform-worker-examples.git
          remote_path_options:
            sub_path: definitions/quickstart/vpc-new
          remote_vars:
            tags: tags.outputs.tag_map

        # deploy the application gateway
        gateway:
          path: git@github.com:ephur/terraform-worker-examples.git
          remote_path_options:
            sub_path: definitions/quickstart/service
          terraform_vars:
            name: new-app-gateway
            public_ip: true
          remote_vars:
            tags: tags.outputs.tag_map
            subnets: network.outputs.public_subnets

        # deploy the application
        backend:
          path: git@github.com:ephur/terraform-worker-examples.git
          remote_path_options:
            sub_path: definitions/quickstart/service
          terraform_vars:
            name: new-app-backend
          remote_vars:
            tags: tags.outputs.tag_map
            subnets: network.outputs.public_subnets

      providers:
        aws:
          vars:
            version: ">= 3.16.0"
            region: {{ aws_region }}
        'null':
          vars:
            version: ">= 3.0.0"

Next, run the following from a \*nix shell session.

.. code-block:: sh

    % worker --config-file ./tfw.yaml --aws-profile <YOUR_AWS_PROFILE> --backend s3 \
             --backend-region <YOUR_AWS_REGION> --backend-bucket <YOUR_BACKEND_BUCKET> \
             --aws-region <YOUR_AWS_REGION> terraform tfw-qs --show-output --limit tags

.. note::
   Be sure to replace ``<YOUR_AWS_PROFILE>``, ``<YOUR_BACKEND_BUCKET>``, and ``<YOUR_AWS_REGION>`` with the
   the appropriate values.

Once the operation is complete, the console should contain text similar to the following:

.. code-block:: sh

    cmd: /usr/local/bin/terraform plan -input=false -detailed-exitcode -no-color
    exit code: 2
    stdout: Acquiring state lock. This may take a few moments...
    stdout: Refreshing Terraform state in-memory prior to plan...
    stdout: The refreshed state will be used to calculate this plan, but will not be
    stdout: persisted to local or remote state storage.
    stdout:
    stdout:
    stdout: ------------------------------------------------------------------------
    stdout:
    stdout: An execution plan has been generated and is shown below.
    stdout: Resource actions are indicated with the following symbols:
    stdout:   + create
    stdout:
    stdout: Terraform will perform the following actions:
    stdout:
    stdout:   # null_resource.null will be created
    stdout:   + resource "null_resource" "null" {
    stdout:       + id       = (known after apply)
    stdout:       + triggers = {
    stdout:           + "tagmap_hash" = "c4dbb1cad9d913b24e0cd288100fbef8"
    stdout:         }
    stdout:     }
    stdout:
    stdout: Plan: 1 to add, 0 to change, 0 to destroy.
    stdout:
    stdout: Changes to Outputs:
    stdout:   + tag_map = {
    stdout:       + deparment   = "TheFunGroup"
    stdout:       + deployment  = "tfw-qs"
    stdout:       + environment = "dev"
    stdout:       + product     = "A Little Demo"
    stdout:       + region      = "us-west-2"
    stdout:     }
    stdout:
    stdout: ------------------------------------------------------------------------
    stdout:
    stdout: Note: You didn't specify an "-out" parameter to save this plan, so Terraform
    stdout: can't guarantee that exactly these actions will be performed if
    stdout: "terraform apply" is subsequently run.
    stdout:
    stdout: Releasing state lock. This may take a few moments...
    plan changes for apply tags

.. note::

    Because the :ref:`terraform-limit` option was passed and the
    :ref:`\\\\-\\\\-apply <terraform-apply-no-apply>` option was NOT passed, the previous operation only
    executed a terraform plan on the first :ref:`definition <definitions>`.

Next, remove the ``--limit`` option and add the ``--apply`` option execute apply on all of the
terraform operations. Also add the ``--no-clean`` option to prevent the terraform operations files
from being cleaned up when **terraform-worker** completes.

.. code-block:: sh

    % worker --config-file ./tfw.yaml --aws-profile <YOUR_AWS_PROFILE> --backend s3 \
             --backend-region <YOUR_AWS_REGION> --backend-bucket <YOUR_BACKEND_BUCKET> \
             --aws-region <YOUR_AWS_REGION> terraform tfw-qs --show-output --apply --no-clean

In the output from the preceding command, look for a line similar to the following:

.. code-block:: sh

    using temporary Directory: /var/folders/8v/vjwlxjbn0q3d_vc52d97ndf93gf7kg/T/tmphm40uuat

If the command is not being run on Mac OS X, the line might look more like:

.. code-block:: sh

   using temporary Directory: /tmp/tmphm40uuat

Navigate to the temporary directory.  The working directory for each of the terraform operations is included
in the ``definitions`` directory.  When troubleshooting, it can be useful to navigate to the working
directory and run terraform commands directly.

Next, in the output from the preceding command, note the lines which similar to the following:

.. code-block:: sh

    stdout: module.service.aws_instance.this[0]: Creation complete after 38s [id=i-0c7189853e1c2addf]
    stdout: module.service.aws_instance.this[2]: Creation complete after 38s [id=i-0d29c8e56f0627ade]
    stdout: module.service.aws_instance.this[1]: Creation complete after 38s [id=i-0a6abfddce557ba29]

In AWS, navigate to the EC2 service and search for each of the instance ids to verify they were created
successfully.

Finally, to clean up the resources provisioned by **terraform-worker**, run the following:

.. code-block:: sh

    % worker --config-file ./tfw.yaml --aws-profile <YOUR_AWS_PROFILE> --backend s3 \
             --backend-region <YOUR_AWS_REGION> --backend-bucket <YOUR_BACKEND_BUCKET> \
             --aws-region <YOUR_AWS_REGION> terraform tfw-qs --show-output --destroy
