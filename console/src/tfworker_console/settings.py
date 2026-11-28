from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class ConsoleSettings(BaseSettings):
    """Console settings, read from `TFWORKER_CONSOLE_*` environment variables."""

    model_config = SettingsConfigDict(env_prefix="TFWORKER_CONSOLE_")

    host: str = "127.0.0.1"
    port: int = 8000
    database_url: Optional[str] = None
