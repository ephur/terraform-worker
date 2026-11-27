from typing import Optional

from fastapi import FastAPI
from tfworker.report import PLAN_SCHEMA, RUN_SCHEMA, STATUS_SCHEMA

from . import version
from .settings import ConsoleSettings


def create_app(settings: Optional[ConsoleSettings] = None) -> FastAPI:
    settings = settings or ConsoleSettings()
    app = FastAPI(title="tfworker console", version=version())
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "status": "ok",
            "version": version(),
            "schemas": [RUN_SCHEMA, STATUS_SCHEMA, PLAN_SCHEMA],
        }

    return app
