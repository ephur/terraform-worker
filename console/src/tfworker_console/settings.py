from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class ConsoleSettings(BaseSettings):
    """Console settings, read from `TFWORKER_CONSOLE_*` environment variables."""

    model_config = SettingsConfigDict(env_prefix="TFWORKER_CONSOLE_")

    host: str = "127.0.0.1"
    port: int = 8000
    database_url: Optional[str] = None
    # a YAML file listing the backend stores to read (see tfworker_console.stores)
    stores_file: Optional[str] = None
    # a YAML file defining deployment groups (see tfworker_console.groups)
    groups_file: Optional[str] = None
    # seconds between scheduled reconciles while serving; 0 turns them off
    reconcile_interval: float = 300.0
    # a lock with no fresh worker lease is shown as stale once older than this
    lock_stale_after: float = 7200.0
