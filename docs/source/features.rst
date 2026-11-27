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
The ``lock`` handler waits for a held state lock instead of failing at once. Before each plan, apply and
destroy it reads the definition's lock (the ``.tflock`` object with ``use_lockfile``, or the DynamoDB item
before Terraform 1.12):

- No lock: terraform runs, with whatever is left of ``wait`` passed as ``-lock-timeout``.
- A live lock: the handler polls, backing off from ``poll_interval`` up to four times it, and logs the
  holder, operation and age on each poll until the lock is released. If it is still held when ``wait``
  runs out, that definition fails, and the phase's failure options (such as ``--apply-failures``) decide
  whether the run continues. ``on_timeout: fail_run`` stops the run instead.
- A stale lock follows ``on_stale``: ``fail`` (the default) fails the definition and names the lock ID to
  force-unlock, ``wait`` treats it as live, and ``force_unlock`` runs ``terraform force-unlock -force``
  and carries on.

While terraform runs, the handler writes a lease next to the state
(``<prefix>/<definition>/terraform.tfstate.tfworker-lease``) and refreshes it every ``heartbeat``. The
lease decides whether a lock is stale:

- A lock held by a worker whose lease is fresh (refreshed within two heartbeats) is live at any age, so a
  long apply is never mistaken for a dead one.
- A lock held by a worker whose lease stopped being refreshed is stale at once, so a killed pod's lock
  goes stale within a minute or two.
- A lock with no lease from its holder, such as one taken by a person running terraform, is stale once it
  is older than ``stale_after``.

The lease is matched to the lock by host (the part of terraform's ``Who`` after ``@``, which is the pod
name in Kubernetes), and it is removed when the action finishes.

.. code-block:: yaml

    handlers:
      lock:
        wait: 10m
        poll_interval: 15s
        stale_after: 2h
        on_stale: fail                # fail | wait | force_unlock
        on_timeout: fail_definition   # or fail_run
        heartbeat: 30s                # 0 turns the lease off
        actions: [plan, apply, destroy]

All fields are optional and the values above are the defaults. The handler needs the S3 backend. Besides
what terraform already needs, the backend credentials need:

- ``s3:GetObject`` on ``<state key>.tflock`` (Terraform 1.12 and later), or ``dynamodb:GetItem`` on the
  ``terraform-<deployment>`` table (earlier versions).
- ``s3:GetObject``, ``s3:PutObject`` and ``s3:DeleteObject`` on ``<state key>.tfworker-lease``.
