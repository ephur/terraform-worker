"""
Prometheus metrics.

``GET /metrics`` serves the Prometheus text format, which Prometheus, the
Datadog agent's ``openmetrics`` check and the OpenTelemetry collector scrape.

Fleet metrics are computed from the projection when scraped, so every replica
reports the same values; scrape one replica or deduplicate. They are cached
for a few seconds, so frequent scrapes don't load the database. Console
metrics (HTTP requests, reconciles and events this replica handled) are this
replica's own.

Labels are store, deployment, group and definition only, never run ids, plan
ids, users or resource addresses, so cardinality stays bounded by the fleet.
``TFWORKER_CONSOLE_METRICS_DEFINITION_LABEL=false`` drops the definition label
for very large fleets.

The endpoint never reconciles or reads the stores. It is off unless
``TFWORKER_CONSOLE_METRICS`` is ``token`` (a bearer API token with the
``metrics`` role, on the console's own port) or ``port`` (no auth, on a
separate listener that should stay inside the cluster).
"""

import logging
import socket
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily, Metric
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from tfworker.report.events import EVENT_TYPES

from . import db, queries
from .auth import metrics_token
from .groups import Groups
from .reconcile import StoreSummary

log = logging.getLogger(__name__)
# counters' *_created series double the output and nothing here reads them
disable_created_metrics()

PREFIX = "tfworker_console"
CACHE_SECONDS = 30.0
# the window the local-module apply counts cover
APPLY_WINDOW = timedelta(days=7)
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
HEALTHS = list(queries.HEALTH_ORDER)
LOCK_STATES = ["live", "stale"]
# HTTP latency buckets, in seconds
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class Metrics:
    """One replica's metrics registry."""

    def __init__(
        self,
        sessions: Optional[sessionmaker],
        groups: Groups,
        stale_after: float,
        definition_label: bool = True,
        cache_seconds: float = CACHE_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        clock: Callable[[], float] = time.monotonic,
    ):
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter(
            f"{PREFIX}_http_requests",
            "HTTP requests this replica answered, by route template and status",
            ["route", "method", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            f"{PREFIX}_http_request_duration_seconds",
            "How long this replica took to answer, by route template",
            ["route", "method"],
            buckets=BUCKETS,
            registry=self.registry,
        )
        self.reconcile_duration = Gauge(
            f"{PREFIX}_reconcile_last_duration_seconds",
            "How long this replica's last reconcile of the store took",
            ["store"],
            registry=self.registry,
        )
        self.reconcile_objects = Counter(
            f"{PREFIX}_reconcile_objects_read",
            "Objects this replica read from the store while reconciling",
            ["store"],
            registry=self.registry,
        )
        self.reconcile_errors = Counter(
            f"{PREFIX}_reconcile_errors",
            "Objects, deployments or whole reconciles of the store that failed",
            ["store"],
            registry=self.registry,
        )
        self.events = Counter(
            f"{PREFIX}_events",
            "Events this replica took from the queue, by type and what became of them",
            ["type", "outcome"],
            registry=self.registry,
        )
        self.fleet: Optional["FleetCollector"] = None
        if sessions is not None:
            self.fleet = FleetCollector(
                sessions,
                groups,
                stale_after,
                definition_label,
                cache_seconds,
                now,
                clock,
            )
            self.registry.register(self.fleet)

    def observe_request(
        self, route: str, method: str, status: int, seconds: float
    ) -> None:
        # the method comes from the client, so only known ones become labels
        method = method if method in METHODS else "other"
        self.requests.labels(route, method, str(status)).inc()
        self.latency.labels(route, method).observe(seconds)

    def observe_reconcile(self, summary: StoreSummary, seconds: float) -> None:
        self.reconcile_duration.labels(summary.store).set(seconds)
        self.reconcile_objects.labels(summary.store).inc(summary.fetched)
        self.reconcile_errors.labels(summary.store).inc(len(summary.errors))

    def observe_event(self, kind: str, outcome: str) -> None:
        # the type comes from the message, so only known types become labels
        self.events.labels(kind if kind in EVENT_TYPES else "other", outcome).inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)


def _gauge(name: str, documentation: str, labels: List[str]) -> GaugeMetricFamily:
    return GaugeMetricFamily(f"{PREFIX}_{name}", documentation, labels=labels)


class FleetCollector:
    """Fleet metrics read from the projection when scraped."""

    def __init__(
        self,
        sessions: sessionmaker,
        groups: Groups,
        stale_after: float,
        definition_label: bool,
        cache_seconds: float,
        now: Callable[[], datetime],
        clock: Callable[[], float],
    ):
        self.sessions = sessions
        self.groups = groups
        self.stale_after = stale_after
        self.definition_label = definition_label
        self.cache_seconds = cache_seconds
        self.now = now
        self.clock = clock
        self._lock = threading.Lock()
        self._cached: Optional[Tuple[float, List[Metric]]] = None

    def describe(self) -> Iterable[Metric]:
        # nothing to check up front; the families depend on the projection
        return []

    def collect(self) -> Iterable[Metric]:
        with self._lock:
            started = self.clock()
            if self._cached and started - self._cached[0] < self.cache_seconds:
                return list(self._cached[1])
            try:
                families = self._families()
                up = 1.0
            except Exception as e:  # the scrape still answers
                log.warning(f"unable to read the projection for metrics: {e}")
                families, up = [], 0.0
            gauge = _gauge("projection_up", "1 when the projection could be read", [])
            gauge.add_metric([], up)
            families.append(gauge)
            self._cached = (self.clock(), families)
            return list(families)

    def _families(self) -> List[Metric]:
        now = self.now()
        scope = queries.Scope(resolver=self.groups)
        place = ["store", "deployment", "group"]
        with self.sessions() as session:
            healths = queries._healths(session, scope, now)
            locks = queries.locks(session, scope, now, self.stale_after)
            plans = list(
                session.scalars(select(db.Plan).where(db.Plan.status == "pending"))
            )
            stores = list(session.scalars(select(db.Store)))
            applies = self._applies(session, now)
            deployments = [
                (row.store, row.name, self.groups.of(row.store, row.name))
                for row in session.scalars(select(db.Deployment))
            ]
            successes = self._last_successes(session, healths)

        definitions = _gauge("definitions", "Definitions by health", place + ["health"])
        # every deployment reports every health, so a recovery reads as 0
        # rather than as no data
        counts: Dict[Tuple[str, ...], int] = {
            place + (health,): 0 for place in deployments for health in HEALTHS
        }
        for health in healths:
            key = (health.store, health.deployment, health.group, health.health)
            counts[key] = counts.get(key, 0) + 1
        for labels, count in sorted(counts.items()):
            definitions.add_metric(list(labels), count)

        pending = _gauge("pending_plans", "Saved plans waiting to be applied", place)
        oldest = _gauge(
            "oldest_pending_plan_age_seconds",
            "Age of the oldest saved plan waiting to be applied",
            place,
        )
        waiting: Dict[Tuple[str, ...], List[datetime]] = {
            place: [] for place in deployments
        }
        for row in plans:
            if row.body.get("storage") == "preview" or queries._plan_expired(row, now):
                continue
            labels = (
                row.store,
                row.deployment,
                self.groups.of(row.store, row.deployment),
            )
            waiting.setdefault(labels, []).append(row.created_at)
        for labels, created in sorted(waiting.items()):
            pending.add_metric(list(labels), len(created))
            if created:  # an age of nothing is no sample, not 0
                oldest.add_metric(list(labels), (now - min(created)).total_seconds())

        held = _gauge("locks", "Held state locks, live or stale", place + ["state"])
        lock_counts: Dict[Tuple[str, ...], int] = {
            place + (state,): 0 for place in deployments for state in LOCK_STATES
        }
        for lock in locks:
            key = (lock.store, lock.deployment, lock.group, lock.state)
            lock_counts[key] = lock_counts.get(key, 0) + 1
        for labels, count in sorted(lock_counts.items()):
            held.add_metric(list(labels), count)

        since_labels = place + (["definition"] if self.definition_label else [])
        since = _gauge(
            "last_apply_age_seconds",
            "Seconds since the last successful apply"
            + ("" if self.definition_label else "; the oldest in the deployment"),
            since_labels,
        )
        ages: Dict[Tuple[str, ...], float] = {}
        for health in healths:
            at = successes.get((health.store, health.deployment, health.definition))
            if at is None:
                continue
            labels = (health.store, health.deployment, health.group)
            if self.definition_label:
                labels += (health.definition,)
            age = (now - at).total_seconds()
            ages[labels] = max(age, ages.get(labels, age))
        for labels, age in sorted(ages.items()):
            since.add_metric(list(labels), age)

        window = int(APPLY_WINDOW.total_seconds() / 86400)
        total = _gauge(
            "applies",
            f"Definition applies in the last {window} days",
            ["store"],
        )
        local = _gauge(
            "local_module_applies",
            f"Definition applies in the last {window} days that used a module "
            "from a local path",
            ["store"],
        )
        for row in stores:
            applies.setdefault(row.name, (0, 0))
        for store, (count, local_count) in sorted(applies.items()):
            total.add_metric([store], count)
            local.add_metric([store], local_count)

        reconciled = _gauge(
            "store_last_reconcile_timestamp_seconds",
            "When the store last finished reconciling, on any replica",
            ["store"],
        )
        healthy = _gauge(
            "store_reconcile_ok",
            "1 when the store's last reconcile had no errors",
            ["store"],
        )
        for row in stores:
            if row.reconciled_at is None:
                continue
            reconciled.add_metric([row.name], row.reconciled_at.timestamp())
            healthy.add_metric([row.name], 0.0 if row.last_error else 1.0)

        return [
            definitions,
            pending,
            oldest,
            held,
            since,
            total,
            local,
            reconciled,
            healthy,
        ]

    @staticmethod
    def _last_successes(session, healths) -> Dict[Tuple[str, str, str], datetime]:
        """
        When each definition last applied successfully.

        The status snapshot keeps only the last apply, so when that one
        failed, the run records the console still holds are searched for the
        last that succeeded.
        """
        found: Dict[Tuple[str, str, str], datetime] = {}
        for health in healths:
            key = (health.store, health.deployment, health.definition)
            apply = health.last_apply
            if apply is not None and apply.status == "succeeded":
                found[key] = apply.at
                continue
            if apply is None:
                continue
            query = (
                select(db.RunDefinition.started_at, db.RunDefinition.phases)
                .where(
                    db.RunDefinition.store == health.store,
                    db.RunDefinition.deployment == health.deployment,
                    db.RunDefinition.definition == health.definition,
                )
                .order_by(db.RunDefinition.started_at.desc())
            )
            for started_at, phases in session.execute(query):
                if (phases.get("apply") or {}).get("status") == "succeeded":
                    found[key] = started_at
                    break
        return found

    @staticmethod
    def _applies(session, now: datetime) -> Dict[str, Tuple[int, int]]:
        query = select(
            db.RunDefinition.store,
            db.RunDefinition.phases,
            db.RunDefinition.local_modules,
        ).where(db.RunDefinition.started_at >= now - APPLY_WINDOW)
        counts: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
        for store, phases, local in session.execute(query):
            phase = phases.get("apply")
            if not phase or phase.get("status") == "skipped":
                continue
            counts[store][0] += 1
            if local:
                counts[store][1] += 1
        return {store: (c[0], c[1]) for store, c in counts.items()}


def response(metrics: "Metrics") -> Response:
    return Response(metrics.render(), media_type=CONTENT_TYPE_LATEST)


router = APIRouter()


@router.get("/metrics", include_in_schema=False)
def scrape(request: Request) -> Response:
    """The console's own port serves metrics only with TFWORKER_CONSOLE_METRICS=token."""
    state = request.app.state
    if state.metrics is None or state.settings.metrics != "token":
        return PlainTextResponse("not found", status_code=404)
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else ""
    try:
        with state.sessions() as session:
            allowed = bool(token) and metrics_token(
                session, token, datetime.now(timezone.utc)
            )
    except Exception as e:  # tokens live in the database
        log.warning(f"unable to check a metrics token: {e}")
        return PlainTextResponse("unable to check the token", status_code=503)
    if not allowed:
        return PlainTextResponse(
            "send a bearer token with the metrics role",
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return response(state.metrics)


def listener_app(metrics: "Metrics") -> FastAPI:
    """The app on the separate metrics port: /metrics and nothing else."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/metrics")
    def scrape() -> Response:
        return response(metrics)

    return app


class MetricsListener:
    """Serves the metrics app on its own port while the console serves."""

    def __init__(self, metrics: "Metrics", host: str, port: int):
        import uvicorn

        self.host = host
        self.port = port
        self.server = uvicorn.Server(
            uvicorn.Config(
                listener_app(metrics),
                host=host,
                port=port,
                lifespan="off",
                # uvicorn's logging is the console's own; leave it as it is
                log_config=None,
                access_log=False,
            )
        )
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Bind now, so a port in use stops the console instead of failing quietly."""
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as e:
            sock.close()
            raise RuntimeError(
                f"unable to serve metrics on {self.host}:{self.port}: {e}"
            ) from e
        self._thread = threading.Thread(
            target=self.server.run,
            kwargs={"sockets": [sock]},
            name="tfworker-console-metrics",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self.server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
