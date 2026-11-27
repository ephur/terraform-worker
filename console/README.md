# tfworker-console

A web console for the runs, plans and locks that terraform-worker records
beside each deployment's state. It reads and curates that data; it never runs
terraform. It lives in the terraform-worker repository as its own package, so
installing `terraform-worker` never pulls in web dependencies. It ships on its
own `console-v*` tags, but depends on the worker from the same commit.

Develop from the repository root, without an identity provider:

    uv sync --all-packages
    TFWORKER_CONSOLE_AUTH=none uv run --package tfworker-console tfworker-console serve --reload

`TFWORKER_CONSOLE_AUTH=none` is development mode: everyone is an admin and
every screen shows a banner saying so. See [Sign-in and roles](#sign-in-and-roles).

Settings come from `TFWORKER_CONSOLE_*` environment variables, for example
`TFWORKER_CONSOLE_PORT=8080`.

## Try it with Docker

The image runs on its own: it keeps its database in SQLite on the `/data`
volume (`TFWORKER_CONSOLE_DATABASE_URL=sqlite:////data/console.db`) and
applies migrations at startup (`TFWORKER_CONSOLE_AUTO_MIGRATE=true`). Build it
from the repository root:

    docker build -f console/Dockerfile -t tfworker-console .

**With sample data**, no AWS needed. The demo seeds two stores, a week of
runs, saved plans and locks, so every screen has something on it:

    docker run --rm -p 127.0.0.1:8000:8000 \
      -e TFWORKER_CONSOLE_AUTH=none \
      -e TFWORKER_CONSOLE_AUTH_ALLOW_REMOTE_NONE=true \
      -e TFWORKER_CONSOLE_DEMO=true \
      tfworker-console

Then open http://127.0.0.1:8000. Development mode only listens on loopback,
and inside a container the console listens on `0.0.0.0`, so
`TFWORKER_CONSOLE_AUTH_ALLOW_REMOTE_NONE=true` allows that. Publishing the port
on `127.0.0.1` keeps it to your machine. The demo seeds only an empty
database and refuses one with other data in it. It has no stores to read, so
it doesn't reconcile, and it can't be combined with a stores file. Outside
Docker, `tfworker-console serve --demo` does the same with any database URL.

**With your own stores**, mount a stores file (and optionally a groups file)
and give the container AWS credentials. The schedule reconciles them:

    docker run --rm -p 127.0.0.1:8000:8000 \
      -e TFWORKER_CONSOLE_AUTH=none \
      -e TFWORKER_CONSOLE_AUTH_ALLOW_REMOTE_NONE=true \
      -e TFWORKER_CONSOLE_STORES_FILE=/config/stores.yaml \
      -v "$PWD/stores.yaml:/config/stores.yaml:ro" \
      -v "$HOME/.aws:/home/console/.aws:ro" -e AWS_PROFILE=readonly \
      -v tfworker-console:/data \
      tfworker-console

For anything shared, turn sign-in on (see [Sign-in and roles](#sign-in-and-roles))
and point `TFWORKER_CONSOLE_DATABASE_URL` at Postgres.

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
the schedule and returns each store's summary. It needs the admin role on
every group.

## Deployment groups

Groups such as Production, Non-prod and Sandbox are defined in a YAML file
named by `TFWORKER_CONSOLE_GROUPS_FILE`. It lives beside the stores file
rather than in the database, so a rebuild of the projection keeps it.

```yaml
groups:                 # listed in fleet sort order
  - name: Production
    plan_ttl: 1d        # pending saved plans older than this are flagged
    force_unlock: admins
  - name: Sandbox
    force_unlock: operators
rules:                  # first match wins
  - match: "prod-*"     # a case-sensitive glob: *, ? and [...]
    group: Production
  - store: dev          # optionally only in one store
    match: "*"
    group: Sandbox
deployments:            # by hand; wins over every rule
  prod/legacy-app: Production
```

Group names are compared without regard to case. Each deployment belongs to exactly one group, and its group is stored
with it at startup and on every reconcile, so scoping by group filters in the
database. A deployment that no
assignment or rule names goes into **Unassigned**, which sorts last and gets
the default policy: no plan TTL, and only admins may force-unlock.

Groups affect the console in these ways:
- Every view can be scoped with `group`, and the screens have a group picker.
- Within each health level, the fleet sorts by group order.
- `GET /api/v1/groups` and `/groups` list each group's policy, members and
  health counts.
- A group's plan TTL flags older pending saved plans as `over_ttl`, with their
  `ttl_expires_at`, for cleanup. The worker still applies them, so they still
  count as pending. A plan's own `expires_at` and `expired` are the
  manifest's. Previews have no TTL.
- Each lock shows who may force-unlock it, and roles can be granted on
  only some groups (see below).


## Sign-in and roles

There are three roles. Viewers read everything they can see, operators also
act on plans, and admins also manage locks, the projection, accounts and
tokens. A role is granted on every group or only on named groups, so a person
can be an operator in Sandbox and a viewer everywhere else; they see only the
groups some grant names. `GET /api/v1/me` shows the caller and their grants.

`TFWORKER_CONSOLE_AUTH` picks how people sign in:

| Mode | Who signs in |
|---|---|
| `local` (default) | accounts in the console's database, with a password and an optional one-time code |
| `oidc` | single sign-on through an OpenID Connect provider such as Okta, Entra ID, Keycloak or Dex; local accounts still work unless SSO is enforced |
| `none` | nobody: everyone is an admin. For development only |

Every mode but `none` needs `TFWORKER_CONSOLE_SECRET_KEY`, a long random
string that signs the session cookie, and a database for the accounts. Sessions last
`TFWORKER_CONSOLE_SESSION_MAX_AGE` seconds (8 hours). The cookie is sent only
over HTTPS unless `TFWORKER_CONSOLE_SECURE_COOKIES=false`, which plain-HTTP
testing needs.

Every request checks the account behind its session, so disabling an account
ends its sessions at once. Signing out, `users set-password` and
`users sign-out` end every session of that account, on every browser. Forms
and API calls that change something are refused when a browser sends them
from another site; requests with a bearer token are not affected.

### Development mode

With `TFWORKER_CONSOLE_AUTH=none` the console refuses to start unless it
listens on loopback (`127.0.0.1`, `::1` or `localhost`). In a container that
must listen on `0.0.0.0`, set `TFWORKER_CONSOLE_AUTH_ALLOW_REMOTE_NONE=true`
and publish the port on loopback only, such as `-p 127.0.0.1:8000:8000`.

### Local accounts

Create the first admin from the command line, then sign in at `/login`:

    tfworker-console users create alice --role admin           # prompts for a password
    tfworker-console users create bob --role operator --group Sandbox --totp
    tfworker-console users grant bob --role viewer             # add a grant
    tfworker-console users grant bob --role admin --replace    # replace every grant
    tfworker-console users set-password bob     # also lifts a lockout
    tfworker-console users sign-out bob         # end bob's open sessions
    tfworker-console users disable bob
    tfworker-console users list

Passwords are hashed with argon2id. `--totp` prints a secret and an
`otpauth://` URI for an authenticator app, and sign-in then needs the code;
each code is accepted once. A wrong password and a wrong code give the same
message, and five failures in a row lock the account for 15 minutes.
`--group` takes the names in the groups file, in any case. Role changes take
effect on the account's next request.

### Single sign-on

Register the console with the provider as a web application using the
authorization code flow, with the redirect URI `<public URL>/auth/callback`
(`TFWORKER_CONSOLE_PUBLIC_URL` is required),
and have it put the user's groups in the ID token. Then:

    TFWORKER_CONSOLE_AUTH=oidc
    TFWORKER_CONSOLE_PUBLIC_URL=https://console.example.com
    TFWORKER_CONSOLE_OIDC_ISSUER=https://example.okta.com/oauth2/default
    TFWORKER_CONSOLE_OIDC_CLIENT_ID=...
    TFWORKER_CONSOLE_OIDC_CLIENT_SECRET=...     # leave out for a public client
    TFWORKER_CONSOLE_ROLES_FILE=/etc/tfworker-console/roles.yaml

The console uses PKCE, and checks the ID token's signature against the
provider's published keys, its issuer, audience, expiry and nonce. The name
comes from `TFWORKER_CONSOLE_OIDC_USERNAME_CLAIM` (`preferred_username`,
falling back to `sub`) and the groups from `TFWORKER_CONSOLE_OIDC_GROUPS_CLAIM`
(`groups`). The scopes requested are `TFWORKER_CONSOLE_OIDC_SCOPES`
(`openid profile email groups`).

The roles file maps values of the groups claim to roles:

```yaml
role_mappings:
  - claim: tf-admins
    role: admin
  - claim: tf-sandbox
    role: operator
    groups: [Sandbox]   # only these deployment groups; leave out for every group
  - claim: engineering
    role: viewer
```

Someone whose groups map to no role cannot sign in. Roles are read from the
groups claim at each sign-in, so after changing someone's groups at the
provider, run `users sign-out` to apply it now. A person's account belongs to
the provider's subject (`iss` and `sub`) and keeps the name it was first
recorded under, even if the username claim changes later. An SSO sign-in
cannot take a name another account already has. Group names in the roles
file must be groups from the groups file.

With `TFWORKER_CONSOLE_SSO_ENFORCED=true`, local passwords work only for
accounts created with `--break-glass`, for when the provider is down. Give
those accounts a one-time code too.

### API tokens

Integrations send `Authorization: Bearer <token>` to `/api/v1`:

    tfworker-console tokens create backstage --role viewer --expires-in 90d
    tfworker-console tokens create ci --role operator --group Sandbox
    tfworker-console tokens list
    tfworker-console tokens revoke backstage

The token is printed once; the console keeps only its SHA-256 hash.
Rebuilding the projection keeps accounts and tokens.
