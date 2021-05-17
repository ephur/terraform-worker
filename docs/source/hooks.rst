.. _hooks:

Hooks
=====

.. index::
   single: hooks

Hooks are executable "cut-in" points that are available to a user during the execution a **terraform-worker**
configuration.  The hooks mechanism allows a user to insert an execuatable script in a ``hooks`` directory in a
**terraform-worker** :ref:`filesystem definition <filesystem-definition>`.  The name of the hook should correspond
to the ``pre`` or ``post`` event that the script should target.

For routing a hook script to the proper interpreter, **terraform-worker** relies on the filesystem's underlying
mechanism (`the shebang, for example, on \*nix systems`). A hook script should be named for the event that it
targets, ignoring the extension. So, a python hook script running on a \*nix system targeting the ``post_apply``
event might be named: ``post_apply.py`` and might include ``#!/usr/bin/env python3`` on the first line.

.. note:: Definition file structure with hooks.

   .. code-block:: bash

      .
      ├── README.md
      ├── gcs.tf
      ├── hooks
      │  └── post_apply.py
      ├── inputs.tf
      └── outputs.tf

Events
------

The **terraform-worker** provides event-based entry points for executing operations outside of terraform.  The following
list shows each of the hook events that **terraform-worker** supports.

 * ``pre_plan``
 * ``post_plan``
 * ``pre_apply``
 * ``post_apply``
 * ``pre_destroy``
 * ``post_destroy``

Environment Variables
---------------------

.. index::
   pair: hooks; environment variables

The execution environment of a hook script is populated with the :ref:`terraform_vars <terraform-vars>` and :ref:`remote_vars <remote-vars>` values from the definition.
The naming convention for terraform_vars environment variables is ``TF_VAR_variable_name``.  The naming convention for 
remote_vars environment variables is ``TF_REMOTE_variable_name``.

If using complex data types (objects, lists) in the terraform variables, consider passing :ref:`\-\-b64-encode-hook-values <base-64-option>` to have the
variable values base64 encoded.
