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
- A live lock: the handler polls, backing off from ``poll_interval`` up to four times it, until the lock
  is released. If it is still held when ``wait`` runs out, that definition fails, and the phase's failure
  options (such as ``--apply-failures``) decide whether the run continues.
- A stale lock, older than ``stale_after`` with no fresh worker lease beside it, follows ``on_stale``:
  ``fail`` (the default) fails the definition and names the lock ID to force-unlock, ``wait`` treats it
  as live, and ``force_unlock`` runs ``terraform force-unlock -force`` and carries on.

While terraform runs, the handler writes a lease next to the state
(``<prefix>/<definition>/terraform.tfstate.tfworker-lease``) and refreshes it every ``heartbeat``. A lease
refreshed within two heartbeats, from the host that holds the lock, marks an old lock as a long-running
command rather than one left behind by a process that died. The lease is removed when the action finishes.

.. code-block:: yaml

    handlers:
      lock:
        wait: 10m
        poll_interval: 15s
        stale_after: 2h
        on_stale: fail        # fail | wait | force_unlock
        heartbeat: 30s
        actions: [plan, apply, destroy]

All fields are optional and the values above are the defaults. The handler needs the S3 backend.
