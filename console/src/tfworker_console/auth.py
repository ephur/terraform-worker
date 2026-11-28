"""
Who is asking, and what they may do.

Roles rank viewer < operator < admin: viewers read, operators also act on
plans, and admins also manage locks, retention, stores, groups and auth. A
grant gives a role on every deployment group or only on some, so a person can
be an operator in Sandbox and a viewer everywhere else.

People sign in with SSO (OIDC, authorization code with PKCE) or a local
password with optional TOTP. Integrations use API tokens. With
``TFWORKER_CONSOLE_AUTH=none`` (development only) everyone is an admin.
"""

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Literal, Optional, Set

import httpx
import jwt
import pyotp
import yaml
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db

Role = Literal["viewer", "operator", "admin"]
RANKS: Dict[str, int] = {"viewer": 0, "operator": 1, "admin": 2}
TOKEN_PREFIX = "tfw_"
# seconds of clock skew allowed on ID token times
LEEWAY = 60

# failed sign-ins in a row before an account is locked, and for how long
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
SIGN_IN_FAILED = "unknown user, wrong password or wrong one-time code"

_hasher = PasswordHasher()


class AuthError(Exception):
    """Sign-in failed; the message is safe to show."""


@dataclass(frozen=True)
class Grant:
    role: str
    # None means every group
    groups: Optional[frozenset] = None

    def covers(self, role: str, group: Optional[str]) -> bool:
        if RANKS[self.role] < RANKS[role]:
            return False
        return self.groups is None or (group is not None and group in self.groups)

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "groups": sorted(self.groups) if self.groups is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Grant":
        groups = data.get("groups")
        return cls(data["role"], frozenset(groups) if groups is not None else None)


@dataclass
class Principal:
    name: str
    # "user", "token" or "dev"
    kind: str
    grants: List[Grant] = field(default_factory=list)

    def allows(self, role: str, group: Optional[str] = None) -> bool:
        """Whether a grant gives `role` on `group`; with no group, on every group."""
        return any(grant.covers(role, group) for grant in self.grants)

    def allows_somewhere(self, role: str) -> bool:
        return any(RANKS[grant.role] >= RANKS[role] for grant in self.grants)

    def visible_groups(self) -> Optional[Set[str]]:
        """The groups this principal may see, or None for every group."""
        groups: Set[str] = set()
        for grant in self.grants:
            if grant.groups is None:
                return None
            groups |= grant.groups
        return groups

    @property
    def highest_role(self) -> Optional[str]:
        roles = [grant.role for grant in self.grants]
        return max(roles, key=RANKS.__getitem__) if roles else None


DEV_PRINCIPAL = Principal("local-dev", "dev", [Grant("admin")])


# passwords and TOTP


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def check_password(password_hash: Optional[str], password: str) -> bool:
    if not password_hash:
        # spend the same time either way, so unknown users are not revealed
        _hasher.hash(password)
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_step(secret: str, code: str, now: float) -> Optional[int]:
    """The time step `code` is valid for, one step either side of now, or None."""
    totp = pyotp.TOTP(secret)
    code = code.strip()
    current = int(now // totp.interval)
    for step in (current - 1, current, current + 1):
        if hmac.compare_digest(totp.at(step * totp.interval), code):
            return step
    return None


def user_grants(session: Session, username: str) -> List[Grant]:
    rows = session.scalars(
        select(db.UserGrant).where(db.UserGrant.username == username)
    )
    return [Grant.from_dict({"role": row.role, "groups": row.groups}) for row in rows]


def local_login(
    session: Session,
    username: str,
    password: str,
    totp: str,
    sso_enforced: bool,
    now: datetime,
) -> Principal:
    """
    Check a local sign-in. Every wrong password or code counts toward a
    lockout, and all of them give the same message, so the password and the
    code cannot be guessed separately. A code is accepted only once.
    """
    user = session.get(db.User, username)
    valid = check_password(user.password_hash if user is not None else None, password)
    if user is None or user.source != "local":
        raise AuthError(SIGN_IN_FAILED)
    if user.locked_until is not None and now < user.locked_until:
        raise AuthError(SIGN_IN_FAILED)
    step = None
    if valid and user.totp_secret:
        step = totp_step(user.totp_secret, totp or "", now.timestamp())
        if step is None or (
            user.totp_last_step is not None and step <= user.totp_last_step
        ):
            valid = False
    if not valid:
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = now + LOCKOUT
            user.failed_logins = 0
        session.commit()
        raise AuthError(SIGN_IN_FAILED)
    user.failed_logins = 0
    user.locked_until = None
    if step is not None:
        user.totp_last_step = step
    session.commit()
    if user.disabled:
        raise AuthError(SIGN_IN_FAILED)
    if sso_enforced and not user.break_glass:
        raise AuthError("sign in with SSO; local sign-in is for break-glass accounts")
    grants = user_grants(session, username)
    if not grants:
        raise AuthError("this account has no role")
    user.last_login = now
    session.commit()
    return Principal(username, "user", grants)


# API tokens


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# a token that may only scrape /metrics; it signs in to nothing else
METRICS_ROLE = "metrics"
TOKEN_ROLES = [*sorted(RANKS, key=RANKS.__getitem__), METRICS_ROLE]


LAST_USED_EVERY = timedelta(minutes=1)


def _live_token(session: Session, token: str, now: datetime) -> Optional[db.ApiToken]:
    """The unexpired token row for a bearer token, marked used."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    row = session.scalar(
        select(db.ApiToken).where(db.ApiToken.token_hash == token_hash(token))
    )
    if row is None or (row.expires_at is not None and now >= row.expires_at):
        return None
    # scrapes come every few seconds; last_used needn't be written each time
    if row.last_used is None or now - row.last_used >= LAST_USED_EVERY:
        row.last_used = now
        session.commit()
    return row


def metrics_token(session: Session, token: str, now: datetime) -> bool:
    """Whether a bearer token may scrape /metrics."""
    row = _live_token(session, token, now)
    return row is not None and row.role == METRICS_ROLE


def token_principal(session: Session, token: str, now: datetime) -> Optional[Principal]:
    row = _live_token(session, token, now)
    if row is None or row.role == METRICS_ROLE:
        return None
    return Principal(
        f"token:{row.name}",
        "token",
        [Grant.from_dict({"role": row.role, "groups": row.groups})],
    )


# SSO role mappings


class RoleMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # a value of the groups claim, such as an Okta group name
    claim: str = Field(min_length=1)
    role: Role
    # deployment groups the role applies to; unset means every group
    groups: Optional[List[str]] = None


class RolesFile(BaseModel):
    """Maps SSO groups claims to console roles; read from TFWORKER_CONSOLE_ROLES_FILE."""

    model_config = ConfigDict(extra="forbid")

    role_mappings: List[RoleMapping] = Field(default_factory=list)

    def grants(self, claims: Iterable[str]) -> List[Grant]:
        held = set(claims)
        return [
            Grant(m.role, frozenset(m.groups) if m.groups is not None else None)
            for m in self.role_mappings
            if m.claim in held
        ]


def load_roles(path: Optional[str]) -> RolesFile:
    if not path:
        return RolesFile()
    return RolesFile.model_validate(yaml.safe_load(Path(path).read_text()) or {})


# OIDC


def pkce_pair() -> tuple:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


class OIDCClient:
    """
    The authorization code flow with PKCE against one issuer, such as Okta,
    Entra ID, Keycloak or Dex. Discovery and keys are fetched once and cached.
    """

    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: Optional[str],
        scopes: str,
        http: Callable[[], httpx.Client] = httpx.Client,
        clock: Callable[[], float] = time.time,
    ):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self._http = http
        self._clock = clock
        self._metadata: Optional[dict] = None
        self._keys: Optional[jwt.PyJWKSet] = None

    def _get(self, url: str) -> dict:
        try:
            with self._http() as client:
                response = client.get(url, timeout=10)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise AuthError(f"could not reach the identity provider: {e}")

    @property
    def metadata(self) -> dict:
        if self._metadata is None:
            metadata = self._get(f"{self.issuer}/.well-known/openid-configuration")
            if metadata.get("issuer", "").rstrip("/") != self.issuer:
                raise AuthError("the identity provider's issuer does not match")
            self._metadata = metadata
        return self._metadata

    def _key(self, kid: Optional[str], refresh: bool = False):
        if self._keys is None or refresh:
            self._keys = jwt.PyJWKSet.from_dict(self._get(self.metadata["jwks_uri"]))
        for key in self._keys.keys:
            if kid is None or key.key_id == kid:
                return key
        if not refresh:
            return self._key(kid, refresh=True)  # the provider rotated its keys
        raise AuthError("the sign-in token was signed with an unknown key")

    def authorize_url(
        self, redirect_uri: str, state: str, nonce: str, challenge: str
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return str(
            httpx.URL(self.metadata["authorization_endpoint"]).copy_merge_params(params)
        )

    def exchange(self, code: str, verifier: str, redirect_uri: str, nonce: str) -> dict:
        """Trade the code for tokens and return the verified ID token's claims."""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "client_id": self.client_id,
        }
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        endpoint = self.metadata["token_endpoint"]
        try:
            with self._http() as client:
                response = client.post(endpoint, data=data, auth=auth, timeout=10)
            if response.status_code != 200:
                raise AuthError("the identity provider refused the sign-in")
            id_token = response.json().get("id_token")
        except (httpx.HTTPError, ValueError) as e:
            raise AuthError(f"could not reach the identity provider: {e}")
        if not id_token:
            raise AuthError("the identity provider returned no ID token")
        try:
            header = jwt.get_unverified_header(id_token)
            key = self._key(header.get("kid"))
            claims = jwt.decode(
                id_token,
                key=key.key,
                algorithms=["RS256", "PS256", "ES256"],
                audience=self.client_id,
                issuer=self.metadata["issuer"],
                # times are checked below against the injected clock
                options={
                    "require": ["exp", "iat", "sub"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError as e:
            raise AuthError(f"the sign-in token is not valid: {e}")
        now = self._clock()
        if not isinstance(claims["exp"], (int, float)) or now >= claims["exp"] + LEEWAY:
            raise AuthError("the sign-in token is not valid: it has expired")
        if not isinstance(claims["iat"], (int, float)) or claims["iat"] > now + LEEWAY:
            raise AuthError(
                "the sign-in token is not valid: it was issued in the future"
            )
        if claims.get("nonce") != nonce:
            raise AuthError("the sign-in token does not belong to this sign-in")
        return claims
