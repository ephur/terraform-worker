import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import click
import pyotp
import uvicorn
from sqlalchemy import select
from tfworker.util.duration import parse_duration

from . import db, version
from .auth import RANKS, hash_password, new_token, new_totp_secret, token_hash
from .db import engine_for, session_factory
from .groups import load_groups
from .migrate import upgrade
from .reconcile import Reconciler
from .security import sign_out_everywhere
from .settings import ConsoleSettings
from .stores import load_stores


@click.group()
@click.version_option(version(), prog_name="tfworker-console")
def main() -> None:
    """Run the terraform-worker console."""


@main.command()
@click.option("--host", default=None, help="Interface to listen on")
@click.option("--port", default=None, type=int, help="Port to listen on")
@click.option("--reload", is_flag=True, help="Reload on code changes (development)")
def serve(host, port, reload) -> None:
    """Serve the console over HTTP."""
    # the app reads its settings from the environment, so the options go
    # there too; otherwise its checks would judge the default host
    if host:
        os.environ["TFWORKER_CONSOLE_HOST"] = host
    if port:
        os.environ["TFWORKER_CONSOLE_PORT"] = str(port)
    settings = ConsoleSettings()
    uvicorn.run(
        "tfworker_console.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=reload,
    )


def _database_url(settings: ConsoleSettings) -> str:
    if not settings.database_url:
        raise click.UsageError("set TFWORKER_CONSOLE_DATABASE_URL")
    return settings.database_url


@main.group("db")
def database() -> None:
    """Manage the console's database."""


@database.command("upgrade")
def db_upgrade() -> None:
    """Create or migrate the database schema."""
    upgrade(engine_for(_database_url(ConsoleSettings())))
    click.echo("database is up to date")


@main.command()
@click.option("--store", "stores", multiple=True, help="Only this store (repeatable)")
@click.option(
    "--rebuild", is_flag=True, help="Empty the projection first, then reconcile all"
)
def reconcile(stores, rebuild) -> None:
    """Project the stores' run records, status, plans and locks into the database."""
    settings = ConsoleSettings()
    configured = load_stores(settings.stores_file)
    unknown = sorted(set(stores) - {store.name for store in configured})
    if unknown:
        raise click.UsageError(f"unknown store: {', '.join(unknown)}")
    reconciler = Reconciler(
        configured,
        session_factory(engine_for(_database_url(settings))),
        groups=load_groups(settings.groups_file),
    )
    if rebuild:
        if stores:
            raise click.UsageError("--rebuild reconciles every store")
        summaries = reconciler.rebuild()
    else:
        summaries = [reconciler.reconcile(name) for name in stores or reconciler.stores]
    click.echo(json.dumps([asdict(summary) for summary in summaries], indent=2))
    if any(summary.errors for summary in summaries):
        raise SystemExit(1)


def _sessions(settings: ConsoleSettings):
    return session_factory(engine_for(_database_url(settings)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


ROLE = click.Choice(sorted(RANKS, key=RANKS.__getitem__))


def _groups(settings: ConsoleSettings, groups) -> Optional[list]:
    """Grant groups by their configured names, or None for every group."""
    if not groups:
        return None
    known = load_groups(settings.groups_file)
    try:
        return [known.canonical(group) for group in groups]
    except ValueError as e:
        raise click.UsageError(str(e))


@main.group()
def users() -> None:
    """Manage local accounts. The first admin is created here."""


@users.command("create")
@click.argument("username")
@click.option("--role", type=ROLE, required=True)
@click.option(
    "--group",
    "groups",
    multiple=True,
    help="Only in this deployment group (repeatable)",
)
@click.option(
    "--break-glass",
    is_flag=True,
    help="May sign in with a password while SSO is enforced",
)
@click.option("--totp", is_flag=True, help="Require a one-time code; prints its secret")
@click.password_option(prompt="Password", help="Prompted when not given")
def users_create(username, role, groups, break_glass, totp, password) -> None:
    """Create a local account."""
    settings = ConsoleSettings()
    groups = _groups(settings, groups)
    with _sessions(settings)() as session:
        if session.get(db.User, username) is not None:
            raise click.UsageError(f"{username} already exists")
        secret = new_totp_secret() if totp else None
        session.add(
            db.User(
                username=username,
                source="local",
                password_hash=hash_password(password),
                totp_secret=secret,
                break_glass=break_glass,
                disabled=False,
                created_at=_now(),
            )
        )
        session.flush()
        session.add(db.UserGrant(username=username, role=role, groups=groups))
        session.commit()
    click.echo(f"created {username} ({role})")
    if secret:
        uri = pyotp.TOTP(secret).provisioning_uri(
            name=username, issuer_name="tfworker console"
        )
        click.echo(f"one-time code secret: {secret}\n{uri}")


@users.command("grant")
@click.argument("username")
@click.option("--role", type=ROLE, required=True)
@click.option("--group", "groups", multiple=True)
@click.option("--replace", is_flag=True, help="Drop the account's other roles first")
def users_grant(username, role, groups, replace) -> None:
    """Give a local account a role, on every group or only some."""
    settings = ConsoleSettings()
    groups = _groups(settings, groups)
    with _sessions(settings)() as session:
        if session.get(db.User, username) is None:
            raise click.UsageError(f"no user named {username}")
        if replace:
            for row in session.scalars(
                select(db.UserGrant).where(db.UserGrant.username == username)
            ):
                session.delete(row)
        session.add(db.UserGrant(username=username, role=role, groups=groups))
        session.commit()
    click.echo(f"granted {role} to {username}")


@users.command("set-password")
@click.argument("username")
@click.password_option(prompt="New password")
def users_set_password(username, password) -> None:
    """Set a password; this also lifts a lockout and ends open sessions."""
    with _sessions(ConsoleSettings())() as session:
        user = session.get(db.User, username)
        if user is None or user.source != "local":
            raise click.UsageError(f"no local user named {username}")
        user.password_hash = hash_password(password)
        user.failed_logins = 0
        user.locked_until = None
        sign_out_everywhere(session, username)
        session.commit()
    click.echo(f"password changed for {username}")


@users.command("disable")
@click.argument("username")
def users_disable(username) -> None:
    """Stop an account signing in; its open sessions end on their next request."""
    with _sessions(ConsoleSettings())() as session:
        user = session.get(db.User, username)
        if user is None:
            raise click.UsageError(f"no user named {username}")
        user.disabled = True
        session.commit()
    click.echo(f"disabled {username}")


@users.command("sign-out")
@click.argument("username")
def users_sign_out(username) -> None:
    """
    End an account's open sessions. An SSO user's roles are read at sign-in,
    so do this after changing their groups at the identity provider.
    """
    with _sessions(ConsoleSettings())() as session:
        if session.get(db.User, username) is None:
            raise click.UsageError(f"no user named {username}")
        sign_out_everywhere(session, username)
        session.commit()
    click.echo(f"signed out {username}")


@users.command("list")
def users_list() -> None:
    with _sessions(ConsoleSettings())() as session:
        for user in session.scalars(select(db.User).order_by(db.User.username)):
            grants = ", ".join(
                grant.role + (f" in {'/'.join(grant.groups)}" if grant.groups else "")
                for grant in session.scalars(
                    select(db.UserGrant).where(db.UserGrant.username == user.username)
                )
            )
            flags = [user.source]
            flags += ["break-glass"] if user.break_glass else []
            flags += ["totp"] if user.totp_secret else []
            flags += ["disabled"] if user.disabled else []
            locked = user.locked_until is not None and _now() < user.locked_until
            flags += ["locked"] if locked else []
            click.echo(f"{user.username}\t{grants or '-'}\t{' '.join(flags)}")


@main.group()
def tokens() -> None:
    """Manage API tokens for integrations."""


@tokens.command("create")
@click.argument("name")
@click.option("--role", type=ROLE, required=True)
@click.option("--group", "groups", multiple=True)
@click.option("--expires-in", default=None, help="A duration such as 90d")
@click.option("--created-by", default="cli")
def tokens_create(name, role, groups, expires_in, created_by) -> None:
    """Create a token; it is shown once."""
    settings = ConsoleSettings()
    groups = _groups(settings, groups)
    now = _now()
    expires = None
    if expires_in is not None:
        try:
            seconds = parse_duration(expires_in)
        except ValueError as e:
            raise click.UsageError(f"--expires-in: {e}")
        if seconds <= 0:
            raise click.UsageError("--expires-in must be longer than zero")
        expires = now + timedelta(seconds=seconds)
    token = new_token()
    with _sessions(settings)() as session:
        if session.get(db.ApiToken, name) is not None:
            raise click.UsageError(f"a token named {name} already exists")
        session.add(
            db.ApiToken(
                name=name,
                token_hash=token_hash(token),
                role=role,
                groups=groups,
                created_by=created_by,
                created_at=now,
                expires_at=expires,
            )
        )
        session.commit()
    click.echo(token)


@tokens.command("revoke")
@click.argument("name")
def tokens_revoke(name) -> None:
    with _sessions(ConsoleSettings())() as session:
        row = session.get(db.ApiToken, name)
        if row is None:
            raise click.UsageError(f"no token named {name}")
        session.delete(row)
        session.commit()
    click.echo(f"revoked {name}")


@tokens.command("list")
def tokens_list() -> None:
    with _sessions(ConsoleSettings())() as session:
        for row in session.scalars(select(db.ApiToken).order_by(db.ApiToken.name)):
            expires = row.expires_at.isoformat() if row.expires_at else "never"
            used = row.last_used.isoformat() if row.last_used else "never"
            click.echo(f"{row.name}\t{row.role}\texpires {expires}\tlast used {used}")
