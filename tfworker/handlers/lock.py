"""Lock handler for terraform-worker.

Before terraform plans, applies or destroys a definition, the handler reads
the definition's state lock. A live lock is waited out, polling until it is
released or the wait runs out. A stale lock, one older than ``stale_after``
with no fresh worker lease beside it, follows ``on_stale``.

While terraform runs, the handler keeps a worker lease beside the state and
refreshes it every ``heartbeat``. Another worker that finds the lock can then
tell a long-running command from one whose process died with the lock held.

Example configuration::

    handlers:
      lock:
        wait: 10m             # how long to wait for a live lock
        poll_interval: 15s
        stale_after: 2h       # a lock older than this, with no fresh lease, is stale
        on_stale: fail        # fail | wait | force_unlock
        heartbeat: 30s
        actions: [plan, apply, destroy]

A lock still held when the wait runs out fails that definition, and the phase's
failure options decide whether the run continues. Whatever is left of the wait
is passed to terraform as ``-lock-timeout``, which covers the gap between the
check and terraform taking the lock.
"""

import getpass
import os
import random
import socket
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Union

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator

import tfworker.util.log as log
from tfworker.backends.lock import LockInfo, WorkerLease
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import BackendError, LockUnavailableError
from tfworker.util.duration import format_duration, parse_duration
from tfworker.util.system import pipe_exec

from .base import BaseHandler
from .registry import HandlerRegistry

if TYPE_CHECKING:  # pragma: no cover
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition

LOCKED_ACTIONS = (TerraformAction.PLAN, TerraformAction.APPLY, TerraformAction.DESTROY)


class OnStale(str, Enum):
    FAIL = "fail"
    WAIT = "wait"
    FORCE_UNLOCK = "force_unlock"


class LockConfig(BaseModel):
    """Configuration for the lock handler. Durations accept seconds or 1h30m."""

    model_config = ConfigDict(extra="forbid")

    wait: float = 600.0
    poll_interval: float = 15.0
    stale_after: float = 7200.0
    on_stale: OnStale = OnStale.FAIL
    heartbeat: float = 30.0
    actions: List[TerraformAction] = Field(default_factory=lambda: list(LOCKED_ACTIONS))

    @field_validator("wait", "poll_interval", "stale_after", "heartbeat", mode="before")
    @classmethod
    def _duration(cls, value):
        return parse_duration(value)

    @field_validator("poll_interval", "heartbeat")
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("actions", mode="before")
    @classmethod
    def _actions(cls, value):
        if isinstance(value, str):
            value = [value]
        return value

    @field_validator("actions")
    @classmethod
    def _locked_actions(cls, value: List[TerraformAction]) -> List[TerraformAction]:
        for action in value:
            if action not in LOCKED_ACTIONS:
                raise ValueError(f"{action.value} does not take a state lock")
        return value


class _Heartbeat:
    """Keeps one definition's worker lease fresh until stopped."""

    def __init__(self, backend, lease: WorkerLease, now: Callable[[], datetime]):
        self._backend = backend
        self._lease = lease
        self._now = now
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._beat,
            name=f"tfworker-lease-{lease.definition}",
            daemon=True,
        )

    def start(self) -> None:
        self._write()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._lease.heartbeat_seconds)

    def _beat(self) -> None:
        while not self._stop.wait(self._lease.heartbeat_seconds):
            self._lease = self._lease.model_copy(update={"heartbeat_at": self._now()})
            self._write()

    def _write(self) -> None:
        try:
            self._backend.put_lease(self._lease)
        except BackendError as e:
            log.warn(
                f"unable to refresh the worker lease for {self._lease.definition}: {e}"
            )


@HandlerRegistry.register("lock")
class LockHandler(BaseHandler):
    """Waits out live state locks and keeps a worker lease while terraform runs."""

    actions = list(LOCKED_ACTIONS)
    config_model = LockConfig
    default_priority = {action: 5 for action in LOCKED_ACTIONS}

    def __init__(self, config: LockConfig):
        self.config = config
        self._app_state = None
        self._heartbeats: Dict[str, _Heartbeat] = {}
        self._host = socket.gethostname()
        self._now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        self._sleep: Callable[[float], None] = time.sleep
        self._monotonic: Callable[[], float] = time.monotonic

    @property
    def app_state(self):
        if self._app_state is None:
            self._app_state = click.get_current_context().obj
        return self._app_state

    @property
    def backend(self):
        return self.app_state.backend

    def is_ready(self) -> bool:
        backend = getattr(self.app_state, "backend", None)
        return bool(getattr(backend, "lock_inspection", False))

    def execute(
        self,
        action: TerraformAction,
        stage: TerraformStage,
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None] = None,
    ) -> None:
        if action not in self.config.actions:
            return
        if stage == TerraformStage.PRE:
            self._acquire(action, definition, working_dir)
        else:
            definition.lock_timeout = None
            self._release(definition.name)

    def skip(self, action, deployment, definition, reason) -> None:
        definition.lock_timeout = None
        self._release(definition.name)

    def teardown(self, deployment: str, working_dir: str) -> None:
        for name in list(self._heartbeats):
            self._release(name)

    def _acquire(
        self, action: TerraformAction, definition: "Definition", working_dir: str
    ) -> None:
        self._release(definition.name)
        deadline = self._monotonic() + self.config.wait
        attempt = 0
        while True:
            lock = self._read_lock(definition.name)
            if lock is None:
                break
            now = self._now()
            if self._is_stale(definition.name, lock, now):
                if self.config.on_stale == OnStale.FAIL:
                    raise LockUnavailableError(
                        f"{definition.name} state is locked by a stale lock "
                        f"{self._describe(lock, now)}; clear it with "
                        f"'terraform force-unlock {lock.id}' once nothing holds it"
                    )
                if self.config.on_stale == OnStale.FORCE_UNLOCK:
                    self._force_unlock(definition, working_dir, lock, now)
                    continue
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise LockUnavailableError(
                    f"{definition.name} state is still locked after waiting "
                    f"{format_duration(self.config.wait)}: {self._describe(lock, now)}"
                )
            if attempt == 0:
                log.info(
                    f"{definition.name} state is locked {self._describe(lock, now)}; "
                    f"waiting up to {format_duration(self.config.wait)}"
                )
            delay = min(self._poll_delay(attempt), remaining)
            attempt += 1
            self._sleep(delay)

        definition.lock_timeout = max(deadline - self._monotonic(), 0.0)
        self._start_heartbeat(action, definition)

    def _read_lock(self, name: str) -> Optional[LockInfo]:
        try:
            return self.backend.get_lock(name)
        except BackendError as e:
            log.warn(
                f"unable to read the state lock for {name}, leaving it to terraform: {e}"
            )
            return None

    def _is_stale(self, name: str, lock: LockInfo, now: datetime) -> bool:
        if lock.age(now) <= self.config.stale_after:
            return False
        try:
            lease = self.backend.get_lease(name)
        except BackendError as e:
            log.warn(f"unable to read the worker lease for {name}: {e}")
            return False
        return not (lease and lease.holds(lock) and lease.is_fresh(now))

    def _poll_delay(self, attempt: int) -> float:
        base = self.config.poll_interval * min(2**attempt, 4)
        return base * random.uniform(0.8, 1.2)

    def _force_unlock(
        self, definition: "Definition", working_dir: str, lock: LockInfo, now: datetime
    ) -> None:
        log.warn(
            f"force unlocking the stale {definition.name} state lock "
            f"{self._describe(lock, now)}"
        )
        from tfworker.commands.terraform import terraform_env

        exit_code, _, stderr = pipe_exec(
            f"{self.app_state.terraform_options.terraform_bin} force-unlock -force {lock.id}",
            cwd=definition.get_target_path(working_dir),
            env=terraform_env(self.app_state),
        )
        if exit_code:
            message = (stderr or b"").decode().strip()
            raise LockUnavailableError(
                f"unable to force unlock the stale {definition.name} state lock "
                f"{lock.id}: {message}"
            )

    def _start_heartbeat(
        self, action: TerraformAction, definition: "Definition"
    ) -> None:
        now = self._now()
        lease = WorkerLease(
            definition=definition.name,
            action=action.value,
            who=f"{self._user()}@{self._host}",
            host=self._host,
            pid=os.getpid(),
            run_id=getattr(self.app_state.root_options, "run_id", None),
            started_at=now,
            heartbeat_at=now,
            heartbeat_seconds=self.config.heartbeat,
        )
        heartbeat = _Heartbeat(self.backend, lease, self._now)
        heartbeat.start()
        self._heartbeats[definition.name] = heartbeat

    def _release(self, name: str) -> None:
        heartbeat = self._heartbeats.pop(name, None)
        if heartbeat is None:
            return
        heartbeat.stop()
        try:
            self.backend.delete_lease(name)
        except BackendError as e:
            log.warn(f"unable to remove the worker lease for {name}: {e}")

    @staticmethod
    def _user() -> str:
        try:
            return getpass.getuser()
        except (KeyError, OSError):
            return "unknown"

    @staticmethod
    def _describe(lock: LockInfo, now: datetime) -> str:
        parts = [f"held by {lock.who or 'unknown'}"]
        if lock.operation:
            parts.append(f"for {lock.operation}")
        parts.append(f"taken {format_duration(max(lock.age(now), 0))} ago")
        parts.append(f"(ID {lock.id})")
        return " ".join(parts)
