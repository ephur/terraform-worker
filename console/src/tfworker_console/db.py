"""
The console's projection of what the worker writes beside each run's state.

Every row can be rebuilt from the stores, so the projection can be dropped at
any time. A state's identity is store + deployment + definition.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKeyConstraint, String, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True), dict: JSON}


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
    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(1024))
    # "template" when found by listing the prefix, "record" when a run record named it
    discovered_from: Mapped[str] = mapped_column(String(16))
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
    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), index=True)
    deployment: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(index=True)
    finished_at: Mapped[Optional[datetime]]
    body: Mapped[dict]


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
    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    who: Mapped[str] = mapped_column(String(512))
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


def engine_for(url: str) -> Engine:
    return create_engine(url, future=True)


def session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
