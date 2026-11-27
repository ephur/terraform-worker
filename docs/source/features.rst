Features
========

Orchestration
-------------
Orchestration  of complex terraform deployments. Configuration can be expressed in YAML, JSON, or HCL.

Remote git sources
------------------
Pulling terraform modules from remote git sources.

Shared state
------------
Passing state variables betwween separate terraform operations.

Hooks
-----
Execution hooks for handling operations outside of terraform.

.. _state-locks:

State locks
-----------
With a top-level ``locking`` block in the config file, the worker waits for a held state lock instead of
failing at once. Locking is off when the block is absent. Right before each plan, apply and destroy the
worker reads the definition's lock (the ``.tflock`` object with ``use_lockfile``, or the DynamoDB item
before Terraform 1.12):

- No lock: terraform runs, with whatever is left of ``wait`` passed as ``-lock-timeout``.
- A live lock: the worker polls, backing off from ``poll_interval`` up to four times it, and logs the
  holder, operation and age on each poll until the lock is released. If it is still held when ``wait``
  runs out, that definition fails, and the phase's failure options (such as ``--apply-failures``) decide
  whether the run continues. ``on_timeout: fail_run`` stops the run instead.
- A stale lock follows ``on_stale``: ``fail`` (the default) fails the definition and names the lock ID to
  force-unlock, ``wait`` treats it as live, and ``force_unlock`` runs ``terraform force-unlock -force``
  and carries on.

While terraform runs, the worker writes a lease next to the state
(``<prefix>/<definition>/terraform.tfstate.tfworker-lease.<host>``) and refreshes it every ``heartbeat``.
The lease decides whether a lock is stale:

- A lock held by a worker whose lease is fresh is live at any age, so a long apply is never mistaken for a
  dead one. A lease is fresh while it was refreshed within three heartbeats plus one minute.
- A lock held by a worker whose lease stopped being refreshed is stale once that window passes, so a
  killed pod's lock goes stale within a few minutes.
- A lock with no lease from its holder, such as one taken by a person running terraform, is stale once it
  is older than ``stale_after``.

The lease is found by the host in terraform's lock (the part of ``Who`` after ``@``, which is the pod name
in Kubernetes). Each host writes and removes only its own lease, and it is removed when the action
finishes. The freshness window compares the reader's clock with the time the holder wrote, so hosts need
clocks within about a minute of each other. ``force_unlock`` relies on this; it also only removes a lock
that looked stale on two polls, and never the same lock twice.

If terraform itself gives up on the lock (someone took it between the check and the command), the failure
is reported as a lock failure, and ``on_timeout: fail_run`` still stops the run. The ``-lock-timeout``
passed to terraform is whatever is left of ``wait`` when terraform starts. The wait begins after the PRE
hooks and handlers have run, so they do not count against it.

On a backend that cannot read locks, the worker logs a warning once and only passes ``wait`` as
``-lock-timeout``.

.. code-block:: yaml

    locking:
      wait: 10m
      poll_interval: 15s
      stale_after: 2h
      on_stale: fail                # fail | wait | force_unlock
      on_timeout: fail_definition   # or fail_run
      heartbeat: 30s                # 0 turns the lease off
      actions: [plan, apply, destroy]

All fields are optional and the values above are the defaults, so ``locking: {}`` turns locking on with
them. Reading locks needs the S3 backend. Besides
what terraform already needs, the backend credentials need:

- ``s3:GetObject`` on ``<state key>.tflock`` (Terraform 1.12 and later), or ``dynamodb:GetItem`` on the
  ``terraform-<deployment>`` table (earlier versions).
- ``s3:GetObject``, ``s3:PutObject`` and ``s3:DeleteObject`` on ``<state key>.tfworker-lease.*``.
- With ``on_stale: force_unlock``, terraform's own unlock rights: ``s3:DeleteObject`` on the
  ``.tflock`` object, or ``dynamodb:DeleteItem`` on the table.
