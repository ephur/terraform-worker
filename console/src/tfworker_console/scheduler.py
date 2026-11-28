"""Reconcile every store on an interval while the console serves."""

import logging
import threading
from typing import Optional

from .reconcile import Reconciler

log = logging.getLogger(__name__)


class ReconcileScheduler:
    def __init__(self, reconciler: Reconciler, interval: float):
        self.reconciler = reconciler
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="tfworker-console-reconcile", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def run_once(self) -> None:
        try:
            for summary in self.reconciler.reconcile_all():
                log.info(
                    f"reconciled {summary.store}: {summary.deployments} deployments, "
                    f"{summary.fetched} fetched, {summary.skipped} unchanged, "
                    f"{summary.removed} removed, {summary.locks} locks held"
                )
        except Exception as e:  # the schedule must keep running
            log.exception(f"scheduled reconcile failed: {e}")

    def _loop(self) -> None:
        while True:
            self.run_once()
            if self._stop.wait(self.interval):
                return
