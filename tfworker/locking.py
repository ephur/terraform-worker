"""
Wait for terraform state locks, and keep a worker lease while terraform runs.

Before terraform plans, applies or destroys a definition, the worker reads the
definition's state lock. A live lock is waited out, polling until it is
released or the wait runs out. A stale lock follows ``on_stale``.

While terraform runs, the worker keeps a lease beside the state and refreshes
it every ``heartbeat``. Another worker that finds the lock can then tell a
long-running command from one whose process died with the lock held: a lock
whose lease is fresh is live at any age, a lock whose lease missed three
heartbeats (plus a minute for clock skew) is stale, and a lock with no lease
(taken outside the worker, or with the heartbeat off) is stale once older than
``stale_after``. Each host keeps its own lease, so workers never overwrite or
remove each other's.

``force_unlock`` removes a lock only after it looked stale on two polls, and
never removes the same lock twice.
"""

import getpass
import os
import random
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Set

import tfworker.util.log as log
from tfworker.backends.lock import LockInfo, WorkerLease
from tfworker.custom_types.config_file import LockingOptions, OnStale, OnTimeout
from tfworker.custom_types.terraform import TerraformAction
from tfworker.exceptions import BackendError, LockUnavailableError
from tfworker.util.duration import format_duration
from tfworker.util.system import pipe_exec

# seconds to wait for a heartbeat thread to finish an in-flight write
STOP_TIMEOUT = 10.0


class _Heartbeat:
    """Keeps one definition's worker lease fresh until stopped."""

    def __init__(self, backend, lease: WorkerLease, now: Callable[[], datetime]):
        self._backend = backend
        self._lease = lease
        self._now = now
        self._stop = threading.Event()
        # held around each write, so no write can land after stop() returns
        self._write_lock = threading.Lock()
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
            self._thread.join(timeout=STOP_TIMEOUT)
        if self._write_lock.acquire(timeout=STOP_TIMEOUT):
            self._write_lock.release()

    def _beat(self) -> None:
        while not self._stop.wait(self._lease.heartbeat_seconds):
            self._lease = self._lease.model_copy(update={"heartbeat_at": self._now()})
            if self._stop.is_set():
                return
            self._write()

    def _write(self) -> None:
        with self._write_lock:
            if self._stop.is_set():
                return
            # the thread must outlive any error: a lease that stops being
            # refreshed makes the live lock it covers look stale to other workers
            try:
                self._backend.put_lease(self._lease)
            except Exception as e:
                log.warn(
                    f"unable to refresh the worker lease for {self._lease.definition}: {e}"
                )


class StateLocker:
    """Waits out live state locks and keeps a worker lease while terraform runs."""

    def __init__(
        self,
        options: LockingOptions,
        backend,
        terraform_bin: str,
        env: Callable[[], Dict[str, str]],
        run_id: Optional[str] = None,
    ):
        self.options = options
        self.backend = backend
        self._terraform_bin = terraform_bin
        self._env = env
        self._run_id = run_id
        self._heartbeats: Dict[str, _Heartbeat] = {}
        self._host = socket.gethostname()
        self._now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        self._sleep: Callable[[float], None] = time.sleep
        self._monotonic: Callable[[], float] = time.monotonic
        self._warned_no_inspection = False
        # lock ids seen stale on an earlier poll, and ids already force-unlocked
        self._stale_seen: Set[str] = set()
        self._forced: Set[str] = set()

    @property
    def fail_run(self) -> bool:
        """True when a lock still held at the deadline stops the whole run."""
        return self.options.on_timeout == OnTimeout.FAIL_RUN

    def covers(self, action: TerraformAction) -> bool:
        return action in self.options.actions

    @property
    def _inspects_locks(self) -> bool:
        return bool(getattr(self.backend, "lock_inspection", False))

    def acquire(self, action: TerraformAction, name: str, target_dir) -> float:
        """
        Wait until the definition's state is free to lock.

        Returns:
            float: the time.monotonic() deadline left for terraform's -lock-timeout

        Raises:
            LockUnavailableError: the lock is stale and the policy is to fail,
                or it was still held when the wait ran out
        """
        self.release(name)
        deadline = self._monotonic() + self.options.wait
        if not self._inspects_locks:
            if not self._warned_no_inspection:
                log.warn(
                    "the backend cannot read state locks; locking only passes "
                    f"-lock-timeout={format_duration(self.options.wait)}"
                )
                self._warned_no_inspection = True
            return deadline
        attempt = 0
        while True:
            lock = self._read_lock(name)
            if lock is None:
                break
            now = self._now()
            if self._is_stale(name, lock, now):
                if self.options.on_stale == OnStale.FAIL:
                    raise LockUnavailableError(
                        f"{name} state is locked by a stale lock "
                        f"{self._describe(lock, now)}; clear it with "
                        f"'terraform force-unlock {lock.id}' once nothing holds it",
                        fail_run=self.fail_run,
                    )
                if self.options.on_stale == OnStale.FORCE_UNLOCK:
                    # only unlock a lock seen stale on two polls, and only once
                    if lock.id in self._stale_seen and lock.id not in self._forced:
                        self._forced.add(lock.id)
                        self._force_unlock(name, target_dir, lock, now)
                        continue
                    self._stale_seen.add(lock.id)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise LockUnavailableError(
                    f"{name} state is still locked after waiting "
                    f"{format_duration(self.options.wait)}: {self._describe(lock, now)}",
                    fail_run=self.fail_run,
                )
            log.info(
                f"{name} state is locked {self._describe(lock, now)}; "
                f"waiting up to {format_duration(remaining)} more"
            )
            delay = min(self._poll_delay(attempt), remaining)
            attempt += 1
            self._sleep(delay)

        if self.options.heartbeat > 0:
            self._start_heartbeat(action, name)
        return deadline

    def release(self, name: str) -> None:
        """Stop the definition's heartbeat and remove its lease, if it has one."""
        heartbeat = self._heartbeats.pop(name, None)
        if heartbeat is None:
            return
        heartbeat.stop()
        try:
            # another process on this host may have taken the same lease key
            lease = self.backend.get_lease(name, self._host)
            if lease is not None and not self._owns(lease):
                log.debug(
                    f"leaving the {name} lease another process on this host holds"
                )
                return
        except BackendError:
            pass
        try:
            self.backend.delete_lease(name, self._host)
        except BackendError as e:
            log.warn(f"unable to remove the worker lease for {name}: {e}")

    def _owns(self, lease: WorkerLease) -> bool:
        return lease.pid == os.getpid() and lease.run_id == self._run_id

    def _read_lock(self, name: str) -> Optional[LockInfo]:
        try:
            return self.backend.get_lock(name)
        except BackendError as e:
            log.warn(
                f"unable to read the state lock for {name}, leaving it to terraform: {e}"
            )
            return None

    def _is_stale(self, name: str, lock: LockInfo, now: datetime) -> bool:
        """
        A lock held by a worker is stale once its lease stops being refreshed,
        whatever its age. Any other lock is stale once older than stale_after.
        """
        if not lock.host:
            return lock.age(now) > self.options.stale_after
        try:
            lease = self.backend.get_lease(name, lock.host)
        except BackendError as e:
            log.warn(f"unable to read the worker lease for {name}: {e}")
            return False
        if lease is not None and lease.holds(lock):
            return not lease.is_fresh(now)
        return lock.age(now) > self.options.stale_after

    def _poll_delay(self, attempt: int) -> float:
        base = self.options.poll_interval * min(2**attempt, 4)
        return base * random.uniform(0.8, 1.2)

    def _force_unlock(
        self, name: str, target_dir, lock: LockInfo, now: datetime
    ) -> None:
        log.warn(
            f"force unlocking the stale {name} state lock {self._describe(lock, now)}"
        )
        try:
            exit_code, _, stderr = pipe_exec(
                f"{self._terraform_bin} force-unlock -force {lock.id}",
                cwd=target_dir,
                env=self._env(),
            )
            message = (stderr or b"").decode().strip()
        except Exception as e:
            exit_code, message = 1, str(e)
        if not exit_code:
            return
        # another worker may have removed it first, and maybe taken a new lock
        current = self._read_lock(name)
        if current is None or current.id != lock.id:
            log.info(f"the stale {name} state lock {lock.id} was already removed")
            return
        raise LockUnavailableError(
            f"unable to force unlock the stale {name} state lock "
            f"{lock.id}: {message}",
            fail_run=self.fail_run,
        )

    def _start_heartbeat(self, action: TerraformAction, name: str) -> None:
        now = self._now()
        lease = WorkerLease(
            definition=name,
            action=action.value,
            who=f"{self._user()}@{self._host}",
            host=self._host,
            pid=os.getpid(),
            run_id=self._run_id,
            started_at=now,
            heartbeat_at=now,
            heartbeat_seconds=self.options.heartbeat,
        )
        heartbeat = _Heartbeat(self.backend, lease, self._now)
        heartbeat.start()
        self._heartbeats[name] = heartbeat

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
