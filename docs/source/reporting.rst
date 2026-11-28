.. _reporting:

Run reporting
=============

Run reporting leaves a durable record of every run next to the state it touched, so a dashboard,
Backstage or a script can answer questions such as when a definition was last applied, how long a plan
took, what changes are pending and which runs failed. The worker only writes these records; it does not
serve them.

.. code-block:: yaml

    reporting:
      resources: true           # record changed resource addresses in run records
      state_serials: true       # read state lineage and serial around plan and apply
      tracked_ref: origin/main  # default: the remote's default branch
      status_retries: 5

Reporting is part of the worker, configured by the top-level ``reporting`` block like ``locking``, and off
when the block is absent. All fields are optional; ``reporting: {}`` turns it on with the defaults. It needs
a backend that can store reports (S3); with another backend the worker warns and runs without it. Handlers
are not involved, and a failure to record never changes the outcome of a run.

Where records live
------------------

Each run writes beside its own state, using its own credentials, under the rendered backend prefix:

.. code-block:: text

    <prefix>/_tfworker/runs/YYYY/MM/DD/<record_id>.json   # one immutable record per invocation
    <prefix>/_tfworker/status/<definition>.json         # latest snapshot per definition

Nothing assumes a single bucket. Fleets often keep state in many accounts and buckets, so every record
names its full location (backend, account id, region, bucket and state key), and that location, not the
definition name, identifies a state. Definition names starting with ``_`` are reserved, and
``worker clean`` keeps the ``_tfworker/`` history.

The run record is written at teardown, which runs whether the run succeeded or failed, and is never
replaced. Each invocation gets its own ``record_id``, so a plan job and its apply job that share one
``--run-id`` each leave a record. A phase that started but never finished (the run stopped under it) is
recorded as failed. A run that stops before any definition starts writes no record.

Each status file is updated with a conditional write; when two runs update the same definition at once,
the one that loses re-reads, merges and retries with backoff, so neither update is lost. A status field
never moves back to an older event, and a plan made before the last recorded apply never comes back as
pending or as drift. A status written by a newer schema version is left alone, and fields this worker
does not know are kept.

What is recorded
----------------

Records hold resource addresses, actions, counts and timings. They never hold attribute values from plans
or state.

``tfworker.run/v1``
    The run id (``--run-id``, or a generated ULID), deployment, start and finish, overall status, the
    requested actions, provenance, and per definition:

    - where its state lives
    - each phase (init, plan, apply, destroy) with status, start, finish, duration, exit code, time spent
      waiting for the state lock, and the first error line on failure; phases the worker passed over are
      recorded as skipped with the reason. The failure reason is only the summary line of terraform's
      first error; the detail lines, which can carry provider values, are left out
    - plan change counts and changed addresses (``resources: false`` keeps counts only)
    - the state lineage and serial the plan was based on, and the serial after apply
    - the modules terraform installed, with their source kind, ref and commit

``tfworker.status/v1``
    The latest plan, apply or destroy, and failure for a definition, plus:

    - ``pending_plan``: a successful plan with changes, stored in the backend (``--backend-plans``), from a
      run not marked ``--intent preview`` or ``drift``. A later successful apply or destroy, or a later plan
      with no changes, clears it. A plan whose changes could not be read leaves it alone.
    - ``drift``: set by a plan with changes from a run marked ``--intent drift``, and cleared by a drift
      check with no changes or by a successful apply.

Provenance
----------

The worker can't reliably know why a plan was made: people validate locally with module sources pointing
at local paths, and then open pull requests from that same work. Runs therefore record observable facts
instead of intent:

- labels from ``--run-label key=value`` (repeatable or comma separated; ``WORKER_RUN_LABELS``), such as
  ``origin=ci`` or ``origin=laptop``
- where plans are kept: ``backend``, a ``local`` plan file path, or only for the ``run``
- the git commit and branch of the checkout holding the config, whether it has local changes (untracked
  files included), and whether the commit is on the tracked ref (local git data only; nothing is fetched)
- each module's source: ``git`` (with the ref asked for and the commit fetched; nothing is fetched to
  compare it with a branch), ``registry`` (with version), ``bundled`` (a path
  inside the definition or inside a fetched module), or ``local`` (a path outside the definition, such as
  a mounted module checkout)
- the worker and terraform versions, host and user

``--intent deploy|preview|drift`` (``WORKER_INTENT``) is an optional override. A definition applied while
any of its modules came from a ``local`` path is tagged ``local-module-paths``. The tag is meant for
tracking a trend over time, not for alerting.

JSON Schema
-----------

The models live in ``tfworker.report`` and have no web or cloud dependencies, so other tools can import
the same contract. JSON Schema exports are published with these docs:

- :download:`run-v1.schema.json <_static/schemas/run-v1.schema.json>`
- :download:`status-v1.schema.json <_static/schemas/status-v1.schema.json>`

Regenerate them with ``python -m tfworker.report.schema docs/source/_static/schemas``; a test fails when
they are out of date.

IAM
---

On top of what terraform needs, the backend credentials need ``s3:GetObject`` and ``s3:PutObject`` on
``<prefix>/_tfworker/*``, ``s3:GetObject`` on the state (for lineage and serial), and
``sts:GetCallerIdentity`` for the account id.
