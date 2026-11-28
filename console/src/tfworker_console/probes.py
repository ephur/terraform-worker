"""
Liveness and readiness probes.

``/livez`` does no I/O: the process is up
and answering. ``/readyz`` checks what serving needs, the database and its
schema, and answers 503 naming the failing check otherwise. Neither reads the
stores or reconciles, so a slow or broken store never takes pods out of
rotation, and neither needs a session or a token.
"""

import asyncio
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable, Dict, Optional, Tuple

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import Engine
from tfworker.report import PLAN_SCHEMA, RUN_SCHEMA, STATUS_SCHEMA

from . import version
from .migrate import alembic_config

router = APIRouter()

# How long a readiness answer is reused, so frequent probes do not load the
# database. How long the database may take to answer; it fits inside the
# kubelet's default one second probe timeout. How long a check that has not
# returned is waited on before a new one is started beside it.
CACHE_SECONDS = 1.0
DATABASE_TIMEOUT = 0.8
STUCK_SECONDS = 30.0

Checks = Dict[str, str]
Answer = Tuple[bool, Checks]
TIMED_OUT: Answer = (False, {"database": "did not answer in time"})


@router.get("/livez")
async def livez() -> dict:
    # async, so it runs on the event loop and never waits for a worker thread
    # that a hung database may be holding
    return {
        "status": "ok",
        "version": version(),
        "schemas": [RUN_SCHEMA, STATUS_SCHEMA, PLAN_SCHEMA],
    }


class Readiness:
    """
    Checks the database and its schema, caching the answer briefly.

    The check runs on its own daemon thread, so a database that never answers
    holds neither a request thread nor shutdown. Callers that arrive while a
    check is running share it rather than starting another, and a check that
    has not returned after ``stuck_seconds`` is left behind and a new one
    started, so the console turns ready once the database is back.
    """

    def __init__(
        self,
        engine: Optional[Engine],
        timeout: float = DATABASE_TIMEOUT,
        cache_seconds: float = CACHE_SECONDS,
        stuck_seconds: float = STUCK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.engine = engine
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.stuck_seconds = stuck_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Optional[Tuple[float, Answer]] = None
        self._running: Optional[Tuple["Future[Answer]", float]] = None
        self.head = (
            ScriptDirectory.from_config(alembic_config(engine)).get_current_head()
            if engine is not None
            else None
        )

    def check(self) -> Answer:
        answer, future, remaining = self._begin()
        if answer is not None:
            return answer
        try:
            return self._finish(future.result(remaining))
        except FutureTimeout:
            return self._finish(TIMED_OUT)

    async def check_async(self) -> Answer:
        answer, future, remaining = self._begin()
        if answer is not None:
            return answer
        try:
            # shield: a timeout must not cancel the check other callers share
            waited = asyncio.shield(asyncio.wrap_future(future))
            return self._finish(await asyncio.wait_for(waited, remaining))
        except asyncio.TimeoutError:
            return self._finish(TIMED_OUT)

    def _begin(self) -> Tuple[Optional[Answer], Optional["Future[Answer]"], float]:
        """A fresh cached answer, or the running check and how long to wait for it."""
        if self.engine is None:
            # auth=none without a database: nothing to wait for
            return (True, {"database": "not configured"}), None, 0.0
        with self._lock:
            now = self._clock()
            if self._cached and now - self._cached[0] < self.cache_seconds:
                return self._cached[1], None, 0.0
            if self._running is None or now - self._running[1] >= self.stuck_seconds:
                self._running = (self._start(), now)
            future, started = self._running
        return None, future, max(started + self.timeout - now, 0.0)

    def _finish(self, answer: Answer) -> Answer:
        # stamped when the answer is known, so a slow answer is still reused
        with self._lock:
            self._cached = (self._clock(), answer)
        return answer

    def _start(self) -> "Future[Answer]":
        future: "Future[Answer]" = Future()
        future.set_running_or_notify_cancel()

        def run() -> None:
            try:
                future.set_result(self._check())
            finally:
                with self._lock:
                    if self._running and self._running[0] is future:
                        self._running = None

        threading.Thread(target=run, name="readyz", daemon=True).start()
        return future

    def _check(self) -> Answer:
        try:
            revision = self._revision()
        except Exception as e:
            # the reason names the error type only: messages can carry hosts
            return False, {"database": f"unreachable ({type(e).__name__})"}
        checks = {"database": "ok"}
        if revision == self.head:
            checks["schema"] = "ok"
            return True, checks
        checks["schema"] = (
            "not migrated"
            if revision is None
            else f"at {revision}, this console expects {self.head}"
        )
        return False, checks

    def _revision(self) -> Optional[str]:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            return MigrationContext.configure(connection).get_current_revision()


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    ready, checks = await request.app.state.readiness.check_async()
    return JSONResponse(
        {"status": "ok" if ready else "unavailable", "checks": checks},
        status_code=200 if ready else 503,
    )
