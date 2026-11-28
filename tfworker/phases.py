"""
Internal phase dispatch for core components.

The terraform command tells core components, such as the run recorder, when
a run starts and ends and when each terraform phase of a definition starts,
finishes, fails or is skipped. Handlers are not involved: they are plugins
with their own dispatch in ``tfworker.handlers``, and nothing here reaches
them.

A listener observes the run; it never changes its outcome. Any error a
listener raises is logged and the run carries on.
"""

from typing import TYPE_CHECKING, Iterable, List, Optional

import tfworker.util.log as log
from tfworker.custom_types.terraform import TerraformAction, TerraformStage

if TYPE_CHECKING:  # pragma: no cover
    from tfworker.commands.terraform import TerraformResult
    from tfworker.definitions.model import Definition


class PhaseListener:
    """A core component told about the phases of a run. Every method is optional."""

    name = "listener"

    def setup(self, deployment: str) -> None:
        """The run is starting."""

    def phase(
        self,
        action: TerraformAction,
        stage: TerraformStage,
        definition: "Definition",
        working_dir: str,
        result: Optional["TerraformResult"] = None,
        lock_wait: Optional[float] = None,
    ) -> None:
        """
        A phase of a definition started (PRE), succeeded (POST) or failed (ERROR).

        Args:
            result: terraform's result, at POST and ERROR
            lock_wait: seconds spent waiting for the state lock, at POST and
                ERROR when locking covered the phase
        """

    def skip(
        self, action: TerraformAction, definition: "Definition", reason: str
    ) -> None:
        """A phase passed over a definition without running terraform."""

    def teardown(self, deployment: str) -> None:
        """The run has ended, whether it succeeded or not."""


class PhaseDispatcher:
    """Calls each listener in turn; a listener's error never reaches the run."""

    def __init__(self, listeners: Iterable[PhaseListener] = ()):
        self.listeners: List[PhaseListener] = list(listeners)

    def __bool__(self) -> bool:
        return bool(self.listeners)

    def setup(self, deployment: str) -> None:
        self._each("setup", deployment)

    def phase(
        self,
        action: TerraformAction,
        stage: TerraformStage,
        definition: "Definition",
        working_dir: str,
        result: Optional["TerraformResult"] = None,
        lock_wait: Optional[float] = None,
    ) -> None:
        self._each(
            "phase",
            action,
            stage,
            definition,
            working_dir,
            result=result,
            lock_wait=lock_wait,
        )

    def skip(
        self, action: TerraformAction, definition: "Definition", reason: str
    ) -> None:
        self._each("skip", action, definition, reason)

    def teardown(self, deployment: str) -> None:
        self._each("teardown", deployment)

    def _each(self, method: str, *args, **kwargs) -> None:
        for listener in self.listeners:
            try:
                getattr(listener, method)(*args, **kwargs)
            except Exception as e:
                log.warn(
                    f"{listener.name} failed during {method}: "
                    f"{type(e).__name__}: {e}"
                )
