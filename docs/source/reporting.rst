.. _reporting:

Run reporting
=============

The ``report`` handler leaves a durable record of every run next to the state it touched, so a dashboard,
Backstage or a script can answer questions such as when a definition was last applied, how long a plan
took, what changes are pending and which runs failed. The worker only writes these records; it does not
serve them.

.. code-block:: yaml

    handlers:
      report:
        resources: true           # record changed resource addresses in run records
        state_serials: true       # read state lineage and serial around plan and apply
        tracked_ref: origin/main  # default: the remote's default branch
        status_retries: 5

All fields are optional. The handler needs a backend that can store reports (S3).

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

.. _saved-plans:

Saved plans and the apply gate
------------------------------

Every plan the worker saves, in the backend with ``--backend-plans`` or on disk with ``--plan-file-path``,
gets a ``tfworker.plan/v1`` manifest. The manifest records the run that made the plan and its
provenance, the definition, whether it is a destroy plan, the lineage, serial and S3 version id of the
state it was made against, the plan's sha256 and size, its change counts and addresses, when it was made
and when it expires (``--plan-ttl``, default ``7d``; ``0`` means it never expires), and its status with a
history of every change.

In the backend each manifest is a record of its own at ``<prefix>/_tfworker/plans/<definition>/<plan_id>.json``,
so a plan's history survives a newer plan made for the same definition. The plan's slot holds a small
pointer, ``terraform.tfplan.manifest.json``, naming the record of the plan currently in the slot. The
record is written before the plan is uploaded, so a plan file never sits in the bucket without one. On
disk the manifest sits beside the plan as ``<definition>.tfplan.manifest.json``.

A plan starts out ``pending``. From there it becomes ``applying`` and then ``applied`` or ``failed``, or it
is retired as ``stale`` (the state changed), ``superseded`` (a newer plan replaced it), ``expired`` or
``discarded`` (its file no longer matches the manifest). When terraform never started, for example
because a PRE hook failed, the plan is released from ``applying`` back to ``pending`` so a later run can
still apply it. A plan is only applied when:

1. it is ``pending``
2. it has not expired
3. its file matches the manifest's sha256
4. it is a destroy plan exactly when the run is destroying
5. its lineage and serial still match the state
6. this run won the conditional write that moves it from ``pending`` to ``applying``

The last step means two runs never apply the same plan at the same time; the one that loses is refused.
A refused plan counts as that definition's apply failure and follows ``--apply-failures``. Expiry is
judged by the clock of the machine applying the plan, so keep worker clocks in sync. The state version id
in the manifest is advisory; the lineage and serial check is what refuses an out of date plan.

After a successful apply or destroy, every other pending backend plan for that definition is marked
``stale`` straight away, rather than waiting for a later run to find its lineage out of date, and its
plan file is removed. Pending backend plans are found through small index entries under
``<prefix>/_tfworker/pending/<definition>/``, so this never lists the whole bucket. ``--force-plan``
marks the plan it replaces ``superseded``. Records stay behind for reporting, and ``worker plans``
lists, discards and sweeps them (see the **plans** command).

Plans on disk are never pending fleet wide, since only the holder of the file can apply them, but they
are gated and taken the same way, and applying one still marks the definition's pending backend plans
stale.

A saved plan with no manifest, made by an older worker, is still applied with a warning.
``--require-plan-manifest`` (``WORKER_REQUIRE_PLAN_MANIFEST``) refuses it instead.

Previews
~~~~~~~~

A run with ``--intent preview`` or ``--intent drift`` saves its backend plans under
``<prefix>/_tfworker/previews/<run_id>/``. They are never offered to a later apply run and do not count
as pending, and such a run cannot apply or destroy. Expire them with a bucket
lifecycle rule, for example:

.. code-block:: json

    {
      "Rules": [
        {
          "ID": "expire-tfworker-previews",
          "Filter": {"Prefix": "<prefix>/_tfworker/previews/"},
          "Status": "Enabled",
          "Expiration": {"Days": 14}
        }
      ]
    }

Streaming events
----------------

The records can also be pushed as `CloudEvents <https://cloudevents.io>`_ 1.0, for dashboards, Argo Events
sensors or alerting that should not poll the bucket. Set ``format: cloudevents`` on the ``sqs`` handler and
give it queues, an EventBridge bus, or both:

.. code-block:: yaml

    handlers:
      sqs:
        format: cloudevents
        queues:
          https://sqs.us-east-1.amazonaws.com/123456789012/all-events:
            types: ["*"]
          https://sqs.us-east-1.amazonaws.com/123456789012/plans:
            types: ["io.tfworker.plan.*"]
        event_bus: tfworker
        types: ["io.tfworker.run.v1", "io.tfworker.status.v1"]

Each event's ``data`` is exactly what was written, and an event is only sent after that write succeeded,
so the bucket stays the source of truth and a consumer that missed events can reconcile from it. Plan
events also cover manifests kept on disk with ``--plan-file-path``, which only the run's own machine has.
Run and status events come from the ``report`` handler, and lock events from the ``lock`` handler, so
enable those handlers to get them. The event types are:

- ``io.tfworker.run.v1``: a ``tfworker.run/v1`` record, once per run.
- ``io.tfworker.status.v1``: a ``tfworker.status/v1`` snapshot, after each update. The ``subject`` is the
  definition.
- ``io.tfworker.plan.v1``: a ``tfworker.plan/v1`` manifest, each time it is written or changes status.
- ``io.tfworker.lock.waiting``: the ``lock`` handler started waiting for a held state lock. ``data`` has
  the deployment, definition, the lock's details and how long the handler will wait.

Every event has ``source`` ``urn:tfworker:deployment:<deployment>`` and a ``tfworkerrunid`` extension
attribute naming the run that caused it. ``types`` (shell-style patterns) filters which events are sent; the
top-level ``types`` applies to the bus and to every queue without its own ``types``, and a queue rule's own
``types`` replaces it for that queue. An empty list is rejected. SQS messages carry a ``content-type``
message attribute of ``application/cloudevents+json``. On the bus,
events are sent with ``Source`` ``tfworker`` (``event_source`` changes it) and the event type as
``DetailType``, with the whole envelope as ``Detail``.

A failure to publish is logged and never fails the run. Events are sent as they happen, so the clients
give up after a 5 second connect timeout, a 10 second read timeout and two attempts. SQS and EventBridge both limit a message to 256 KiB,
so a very large run record may be rejected; it is still in the bucket. Without ``format: cloudevents``,
the ``sqs`` handler keeps sending its original per-stage messages. Publishing needs ``sqs:SendMessage``
on the queues, ``sqs:ListQueues``, and ``events:PutEvents`` and ``events:DescribeEventBus`` on the bus.

IAM
---

On top of what terraform needs, the backend credentials need ``s3:GetObject`` and ``s3:PutObject`` on
``<prefix>/_tfworker/*`` (plus ``s3:DeleteObject`` and ``s3:ListBucket`` there for the pending plan index),
``s3:GetObject``, ``s3:PutObject`` and ``s3:DeleteObject`` on saved plans and their pointers,
``s3:GetObject`` on the state (for lineage and serial), and ``sts:GetCallerIdentity`` for the account id.
Applying a plan from ``--plan-file-path`` with an S3 backend needs the ``_tfworker`` permissions too,
since it marks the definition's pending backend plans stale.
