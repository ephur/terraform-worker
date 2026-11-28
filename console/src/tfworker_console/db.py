"""
The console's projection of what the worker writes beside each run's state.

Every row can be rebuilt from the stores, so the projection can be dropped at
any time. A state's identity is store + deployment + definition, where the
deployment is the prefix segment its state lives under.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .sqltypes import Timestamp


class Base(DeclarativeBase):
    type_annotation_map = {datetime: Timestamp(), dict: JSON}


class Store(Base):
    __tablename__ = "stores"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    region: Mapped[str] = mapped_column(String(64))
    bucket: Mapped[str] = mapped_column(String(255))
    prefix: Mapped[str] = mapped_column(String(1024))
    credentials: Mapped[str] = mapped_column(String(16))
    reconciled_at: Mapped[Optional[datetime]]
    last_error: Mapped[Optional[str]] = mapped_column(String(2048))


class Deployment(Base):
    __tablename__ = "deployments"

    store: Mapped[str] = mapped_column(String(128), primary_key=True)
    # the prefix segment, which stays stable whatever the records call it
    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(1024))
    # the name the newest run record gives, when it differs from the prefix segment
    record_name: Mapped[Optional[str]] = mapped_column(String(255))
    # "record" when run records name the deployment differently, else "template"
    discovered_from: Mapped[str] = mapped_column(String(16))
    # the deployment group it is in, kept current from the groups file
    group_name: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    first_seen: Mapped[datetime]
    last_seen: Mapped[datetime]


class Definition(Base):
    __tablename__ = "definitions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["store", "deployment"],
            ["deployments.store", "deployments.name"],
            ondelete="CASCADE",
        ),
    )

    store: Mapped[str] = mapped_column(String(128), primary_key=True)
    deployment: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    state_key: Mapped[Optional[str]] = mapped_column(String(1024))
    last_seen: Mapped[datetime]


class Run(Base):
    __tablename__ = "runs"

    store: Mapped[str] = mapped_column(String(128), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), index=True)
    deployment: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(index=True)
    finished_at: Mapped[Optional[datetime]]
    body: Mapped[dict]


class RunDefinition(Base):
    """One definition's part of a run record, so views can filter in SQL."""

    __tablename__ = "run_definitions"
    __table_args__ = (
        Index(
            "ix_run_definitions_lookup",
            "store",
            "definition",
            "deployment",
            "started_at",
        ),
    )

    store: Mapped[str] = mapped_column(String(128), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    definition: Mapped[str] = mapped_column(String(255), primary_key=True)
    deployment: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(index=True)
    # the DefinitionResult's phases, module_sources and tags
    phases: Mapped[dict]
    module_sources: Mapped[Optional[list]] = mapped_column(JSON)
    tags: Mapped[Optional[list]] = mapped_column(JSON)
    local_modules: Mapped[bool]


class Status(Base):
    __tablename__ = "statuses"

    store: Mapped[str] = mapped_column(String(128), primary_key=True)
    deployment: Mapped[str] = mapped_column(String(255), primary_key=True)
    definition: Mapped[str] = mapped_column(String(255), primary_key=True)
    updated_at: Mapped[datetime]
    body: Mapped[dict]


class Plan(Base):
    __tablename__ = "plans"

    store: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    deployment: Mapped[str] = mapped_column(String(255), index=True)
    definition: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime]
    body: Mapped[dict]


class Lock(Base):
    """A state lock seen held on the last reconcile, with its holder's lease if any."""

    __tablename__ = "locks"

    store: Mapped[str] = mapped_column(String(128), primary_key=True)
    deployment: Mapped[str] = mapped_column(String(255), primary_key=True)
    definition: Mapped[str] = mapped_column(String(255), primary_key=True)
    lock_id: Mapped[str] = mapped_column(String(255))
    who: Mapped[str] = mapped_column(String(1024))
    operation: Mapped[str] = mapped_column(String(64))
    created: Mapped[datetime]
    lease: Mapped[Optional[dict]]
    observed_at: Mapped[datetime]


class SeenObject(Base):
    """The ETag of every object last projected, so unchanged objects are skipped."""

    __tablename__ = "seen_objects"

    store: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(1024), primary_key=True)
    etag: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    deployment: Mapped[str] = mapped_column(String(255))
    # the projected row's id within its kind (record id, plan id or definition)
    ref: Mapped[str] = mapped_column(String(255))


class User(Base):
    """
    A person who signs in. Local users have a password; SSO users are
    recorded at sign-in so actions can be attributed to them.
    """

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    # "local" or "oidc"
    source: Mapped[str] = mapped_column(String(16))
    password_hash: Mapped[Optional[str]] = mapped_column(String(255))
    totp_secret: Mapped[Optional[str]] = mapped_column(String(64))
    # may sign in with a password even while SSO is enforced
    break_glass: Mapped[bool] = mapped_column(default=False)
    disabled: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime]
    last_login: Mapped[Optional[datetime]]
    # SSO users: "<issuer>#<sub>", the identity the row belongs to
    subject: Mapped[Optional[str]] = mapped_column(String(1024), unique=True)
    # sessions carry this; bumping it signs the account out everywhere
    session_version: Mapped[int] = mapped_column(default=0)
    failed_logins: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[Optional[datetime]]
    # the newest one-time code step accepted, so a code works only once
    totp_last_step: Mapped[Optional[int]] = mapped_column(BigInteger)


class UserGrant(Base):
    """A role a local user holds, on every group or only on some."""

    __tablename__ = "user_grants"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        String(255), ForeignKey("users.username", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    # None means every group
    groups: Mapped[Optional[list]] = mapped_column(JSON)


class ApiToken(Base):
    """A bearer token for an integration, stored only as its SHA-256."""

    __tablename__ = "api_tokens"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16))
    groups: Mapped[Optional[list]] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime]
    expires_at: Mapped[Optional[datetime]]
    last_used: Mapped[Optional[datetime]]


# tables a rebuild empties; users and tokens are not part of the projection
PROJECTION = (
    "stores",
    "deployments",
    "definitions",
    "runs",
    "run_definitions",
    "statuses",
    "plans",
    "locks",
    "seen_objects",
)


def engine_for(url: str) -> Engine:
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":
        _sqlite_savepoints(engine)
    return engine


def _sqlite_savepoints(engine: Engine) -> None:
    """Let pysqlite run SAVEPOINTs, which the reconciler uses per object."""

    @event.listens_for(engine, "connect")
    def _connect(connection, _record):
        connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(connection):
        connection.exec_driver_sql("BEGIN")


def session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
