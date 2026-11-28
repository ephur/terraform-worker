# tfworker-console

A web console for the runs, plans and locks that terraform-worker records
beside each deployment's state. It reads and curates that data; it never runs
terraform. It lives in the terraform-worker repository as its own package, so
installing `terraform-worker` never pulls in web dependencies. It ships on its
own `console-v*` tags, but depends on the worker from the same commit.

Develop from the repository root:

    uv sync --all-packages
    uv run --package tfworker-console tfworker-console serve --reload

Settings come from `TFWORKER_CONSOLE_*` environment variables, for example
`TFWORKER_CONSOLE_PORT=8080`.

## Stores and the projection

The console reads the run records, status snapshots, plan manifests and
state locks the worker writes beside each deployment's state. List the
backend stores in a YAML file and point `TFWORKER_CONSOLE_STORES_FILE` at it:

    stores:
      - name: prod
        region: us-east-1
        bucket: prod-terraform-state
        prefix: terraform/state/{deployment}
        credentials:
          role_arn: arn:aws:iam::123456789012:role/tfworker-console-read
      - name: dev
        region: us-west-2
        bucket: dev-terraform-state
        prefix: terraform/state/{deployment}
        credentials:
          profile: dev          # or leave credentials out for the ambient ones

What the console knows lives in Postgres (`TFWORKER_CONSOLE_DATABASE_URL`,
for example `postgresql+psycopg://console@db/console`). It is a projection:
every row can be rebuilt from the stores.

    tfworker-console db upgrade              # create or migrate the schema
    tfworker-console reconcile               # project every store now
    tfworker-console reconcile --store prod  # one store
    tfworker-console reconcile --rebuild     # empty the projection, then reconcile

While serving, the console reconciles every `TFWORKER_CONSOLE_RECONCILE_INTERVAL`
seconds (300 by default, 0 turns it off). Deployments are found by listing the
segment where `{deployment}` sits, and that segment is the deployment's
identity; the name its newest run record gives is shown beside it. Objects
whose ETag has not changed are skipped. A deployment whose prefix is gone or
holds nothing the worker wrote, a state that was deleted, and a store removed
from the stores file all leave the projection on the next reconcile.

Each store reconciles in one transaction that holds its row locked, so the
schedule, the CLI and other replicas take turns. An object or deployment that
cannot be read or stored is reported in the store's `last_error` and the rest
still land.

The reconciler never reads state files. It needs `s3:ListBucket` on the
prefix, and `s3:GetObject` on `<prefix>/_tfworker/*`, `*.tflock` and
`*.tfworker-lease.*`.

## Screens and the read API

The console serves a screen for each view, and the same data as JSON under
`/api/v1` (OpenAPI at `/docs`). Every view takes `store` and `deployment`
query parameters, repeatable, to narrow it; the picker at the top of each
screen sets them, and htmx swaps the content in place.

| Screen | API | Shows |
|---|---|---|
| `/` | `GET /api/v1/fleet` | health counts, then every definition that is failing, drifted, pending or locked |
| `/deployments` | `GET /api/v1/deployments` | each deployment, its record name and its health counts |
| `/definitions/{name}` | `GET /api/v1/definitions/{name}` and `.../timeline` | one definition across deployments: health, pending plan, previews, lock, recent runs, module versions and skew, and a timeline of phases and plan changes |
| `/plans` | `GET /api/v1/plans` | plan manifests, filtered by `status` and `definition` |
| `/locks` | `GET /api/v1/locks` | held locks; `live` when the holder's lease is fresh, `stale` when it stopped or, without a lease, after `TFWORKER_CONSOLE_LOCK_STALE_AFTER` seconds (7200) |
| `/runs` | `GET /api/v1/runs`, `GET /api/v1/runs/{store}/{record_id}` | run records, and the full record |
| `/tracking/local-modules` | `GET /api/v1/tracking/local-modules` | the weekly share of applies that used a module from a local path |

Health is worst first. A definition is *failing* when its newest failure is
newer than its newest successful plan or apply, *drifted* while a drift check
found changes, and *pending* while a saved plan is pending and unexpired. Lock
state is judged as of the reconcile that saw it, and each lock shows how long
ago that was. Times are stored and returned in UTC on every database.

`POST /api/v1/reconcile[?store=NAME]` reconciles now instead of waiting for
the schedule and returns each store's summary. Until roles arrive (#237) the
console has no access control, so serve it only where everyone who can reach
it may see every store.

