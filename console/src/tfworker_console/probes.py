"""
Liveness and readiness probes.

``/livez`` does no I/O: the process is up
and answering. ``/readyz`` checks what serving needs, the database and its
schema, and answers 503 naming the failing check otherwise. Neither reads the
stores or reconciles, so a slow or broken store never takes pods out of
rotation, and neither needs a session or a token.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

# how long a readiness answer is reused, so frequent probes do not load the
# database, and how long the database may take to answer
CACHE_SECONDS = 1.0
DATABASE_TIMEOUT = 2.0

Checks = Dict[str, str]


@router.get("/livez")
def livez() -> dict:
    return {
        "status": "ok",
        "version": version(),
        "schemas": [RUN_SCHEMA, STATUS_SCHEMA, PLAN_SCHEMA],
    }


class Readiness:
    """Checks the database and its schema, caching the answer briefly."""

    def __init__(
        self,
        engine: Optional[Engine],
        timeout: float = DATABASE_TIMEOUT,
        cache_seconds: float = CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.engine = engine
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Optional[Tuple[float, bool, Checks]] = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="readyz")
        self.head = (
            ScriptDirectory.from_config(alembic_config(engine)).get_current_head()
            if engine is not None
            else None
        )

    def check(self) -> Tuple[bool, Checks]:
        with self._lock:
            now = self._clock()
            if self._cached and now - self._cached[0] < self.cache_seconds:
                return self._cached[1], self._cached[2]
            ready, checks = self._check()
            self._cached = (now, ready, checks)
            return ready, checks

    def _check(self) -> Tuple[bool, Checks]:
        if self.engine is None:
            # auth=none without a database: nothing to wait for
            return True, {"database": "not configured"}
        try:
            revision = self._pool.submit(self._revision).result(self.timeout)
        except FutureTimeout:
            return False, {"database": "did not answer in time"}
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
def readyz(request: Request) -> JSONResponse:
    ready, checks = request.app.state.readiness.check()
    return JSONResponse(
        {"status": "ok" if ready else "unavailable", "checks": checks},
        status_code=200 if ready else 503,
    )
