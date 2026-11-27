from typing import Literal, Optional

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

    # local: passwords only; oidc: SSO plus local accounts; none: no sign-in,
    # everyone is an admin (development only, loopback addresses only)
    auth: Literal["local", "oidc", "none"] = "local"
    # signs session cookies; required unless auth is none
    secret_key: Optional[str] = None
    session_max_age: int = 8 * 3600
    # send the session cookie over HTTPS only
    secure_cookies: bool = True
    # with oidc, only break-glass accounts may sign in with a password
    sso_enforced: bool = False
    # the URL people reach the console at, for the OIDC redirect
    public_url: Optional[str] = None
    oidc_issuer: Optional[str] = None
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None
    oidc_scopes: str = "openid profile email groups"
    oidc_groups_claim: str = "groups"
    oidc_username_claim: str = "preferred_username"
    # a YAML file mapping SSO groups to roles (see tfworker_console.auth)
    roles_file: Optional[str] = None
    # let auth=none listen beyond loopback, as it must inside a container
    auth_allow_remote_none: bool = False
