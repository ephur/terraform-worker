from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from tfworker.report import PLAN_SCHEMA, RUN_SCHEMA, STATUS_SCHEMA

from . import api, version, web
from .db import engine_for, session_factory
from .groups import load_groups
from .reconcile import Reconciler
from .scheduler import ReconcileScheduler
from .settings import ConsoleSettings
from .stores import load_stores


def create_app(settings: Optional[ConsoleSettings] = None) -> FastAPI:
    settings = settings or ConsoleSettings()
    app_state = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = app_state.get("scheduler")
        if scheduler is not None:
            scheduler.start()
        yield
        if scheduler is not None:
            scheduler.stop()

    app = FastAPI(title="tfworker console", version=version(), lifespan=lifespan)
    app.state.settings = settings
    app.state.sessions = None
    app.state.reconciler = None
    app.state.groups = load_groups(settings.groups_file)
    if settings.database_url:
        app.state.sessions = session_factory(engine_for(settings.database_url))
        app.state.reconciler = Reconciler(
            load_stores(settings.stores_file), app.state.sessions
        )
        if settings.reconcile_interval > 0 and app.state.reconciler.stores:
            app_state["scheduler"] = ReconcileScheduler(
                app.state.reconciler, settings.reconcile_interval
            )

    app.include_router(api.router)
    app.include_router(web.router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "status": "ok",
            "version": version(),
            "schemas": [RUN_SCHEMA, STATUS_SCHEMA, PLAN_SCHEMA],
        }

    return app
