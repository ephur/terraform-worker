import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Dict, Union

import tfworker.util.log as log
from tfworker.exceptions import FrozenInstanceError, HandlerError, UnknownHandler

if TYPE_CHECKING:
    from tfworker.commands.terraform import TerraformResult
    from tfworker.custom_types import TerraformAction, TerraformStage
    from tfworker.definitions.model import Definition

    from .base import BaseHandler  # noqa: F401


class HandlersCollection(Mapping):
    """
    The HandlersCollection class is a collection of handlers that are active in a various execution.
    """

    _instance = None
    _lock = threading.Lock()
    _frozen: bool = False

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, handlers: Dict[str, Union["BaseHandler", None]] = None):
        """
        Initialize the HandlersCollection object, only add handlers which have a provider key in the handlers_config dict.
        """
        if not hasattr(self, "_initialized"):
            self._handlers = dict()
            self._results = []
            if handlers:
                for k, v in handlers.items():
                    log.trace(f"Adding handler {k} to handlers collection")
                    log.trace(f"Handler cls: {v}")
                    self._handlers[k] = v
            self._initialized = True

    def __len__(self):
        return len(self._handlers)

    def __getitem__(self, value):
        if isinstance(value, int):
            return self._handlers[list(self._handlers.keys())[value]]
        return self._handlers[value]

    def __iter__(self):
        return iter(self._handlers.keys())

    def __setitem__(self, key, value):
        if self._frozen:
            raise FrozenInstanceError("Cannot modify a frozen instance.")
        self._handlers[key] = value

    # result store helpers
    @property
    def results(self):
        return self._results

    def add_result(self, result):
        self._results.append(result)

    def get_results(self, handler_name=None, action=None, stage=None):
        res = []
        for r in self._results:
            if handler_name is not None and r.handler != handler_name:
                continue
            if action is not None and r.action != action:
                continue
            if stage is not None and r.stage != stage:
                continue
            res.append(r)
        return res

    def find_results(self, field, value):
        return [r for r in self._results if getattr(r, field, None) == value]

    def freeze(self):
        """
        freeze is used to prevent further modification of the handlers collection.
        """
        self._frozen = True

    def update(self, handlers_config):
        """
        update is used to update the handlers collection with new handlers.
        """
        for k in handlers_config:
            if k in self._handlers.keys():
                raise TypeError(f"Duplicate handler: {k}")
            self._handlers[k] = handlers_config[k]

    def get(self, value):
        try:
            return self[value]
        except KeyError:
            raise UnknownHandler(provider=value)

    def _ordered_handlers(
        self, action: "TerraformAction", stage: "TerraformStage"
    ) -> list[tuple[str, "BaseHandler"]]:
        """Return handlers ordered by dependencies and priority."""
        # Filter handlers that are ready for this action
        handlers = {
            name: h
            for name, h in self._handlers.items()
            if h is not None and action in h.actions and h.is_ready()
        }

        if not handlers:
            return []

        # Build priority map
        priorities = {
            name: getattr(h, "default_priority", {}).get(action, 100)
            for name, h in handlers.items()
        }

        # Build dependency edges
        edges: dict[str, set[str]] = {name: set() for name in handlers}
        indegree = {n: 0 for n in handlers}
        for name, h in handlers.items():
            deps = getattr(h, "dependencies", {}).get(action, {}).get(stage, [])
            for dep in deps:
                if dep in handlers:
                    edges[dep].add(name)
                    indegree[name] += 1

        # Kahn's algorithm with priority ordering

        ready = [n for n, d in indegree.items() if d == 0]
        ready.sort(key=lambda n: priorities.get(n, 100))

        ordered: list[str] = []
        while ready:
            n = ready.pop(0)
            ordered.append(n)
            for m in edges[n]:
                indegree[m] -= 1
                if indegree[m] == 0:
                    # insert maintaining priority order
                    index = 0
                    while (
                        index < len(ready) and priorities[ready[index]] <= priorities[m]
                    ):
                        index += 1
                    ready.insert(index, m)

        if len(ordered) != len(handlers):
            log.error("Cycle detected in handler dependencies; using priority order")
            ordered = sorted(handlers, key=lambda n: priorities.get(n, 100))

        return [(name, handlers[name]) for name in ordered]

    def exec_handlers(
        self,
        action: "TerraformAction",
        stage: "TerraformStage",
        deployment: str,
        definition: "Definition",
        working_dir: str,
        result: Union["TerraformResult", None] = None,
    ):
        """
        exec_handlers is used to execute a specific action on all handlers.
        """
        from tfworker.custom_types import TerraformAction, TerraformStage

        handler: BaseHandler

        if action not in TerraformAction:
            raise HandlerError(f"Invalid action {action}")
        if stage not in TerraformStage:
            raise HandlerError(f"Invalid stage {stage}")
        for name, handler in self._ordered_handlers(action, stage):
            log.trace(
                f"Executing handler {name} for {definition.name} action {action} and stage {stage}"
            )
            ret = handler.execute(
                action=action,
                stage=stage,
                deployment=deployment,
                definition=definition,
                working_dir=working_dir,
                result=result,
            )
            if ret is not None:
                if isinstance(ret, list):
                    for r in ret:
                        self.add_result(r)
                else:
                    self.add_result(ret)
