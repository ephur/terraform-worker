from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tfworker.definitions.model import DefinitionRemoteOptions
from tfworker.util.duration import parse_duration

from .freezable_basemodel import FreezableBaseModel
from .terraform import TerraformAction

# a faster lease refresh only adds load on the state bucket
MIN_HEARTBEAT = 5.0
LOCKED_ACTIONS = (TerraformAction.PLAN, TerraformAction.APPLY, TerraformAction.DESTROY)


class ParallelOptions(FreezableBaseModel):
    """
    Configuration options for parallel processing of definitions.
    """

    model_config = ConfigDict(extra="forbid")

    max_preparation_workers: int = Field(
        8,
        description="Maximum number of parallel workers for file preparation operations",
    )
    max_init_workers: int = Field(
        4,
        description="Maximum number of parallel workers for terraform init operations",
    )


class OnStale(str, Enum):
    FAIL = "fail"
    WAIT = "wait"
    FORCE_UNLOCK = "force_unlock"


class OnTimeout(str, Enum):
    FAIL_DEFINITION = "fail_definition"
    FAIL_RUN = "fail_run"


class LockingOptions(FreezableBaseModel):
    """
    How the worker waits for state locks. Durations accept seconds or 1h30m.
    """

    model_config = ConfigDict(extra="forbid")

    wait: float = Field(600.0, description="How long to wait for a live lock")
    poll_interval: float = Field(15.0, description="The first delay between polls")
    stale_after: float = Field(
        7200.0, description="A lock older than this, with no lease, is stale"
    )
    on_stale: OnStale = Field(OnStale.FAIL, description="What to do with a stale lock")
    heartbeat: float = Field(
        30.0, description="How often the worker lease is refreshed; 0 turns it off"
    )
    on_timeout: OnTimeout = Field(
        OnTimeout.FAIL_DEFINITION,
        description="Fail the definition or the whole run when the wait runs out",
    )
    actions: List[TerraformAction] = Field(
        default_factory=lambda: list(LOCKED_ACTIONS),
        description="The terraform actions that wait for the lock",
    )

    @field_validator("wait", "poll_interval", "stale_after", "heartbeat", mode="before")
    @classmethod
    def _duration(cls, value):
        return parse_duration(value)

    @field_validator("poll_interval")
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("heartbeat")
    @classmethod
    def _heartbeat(cls, value: float) -> float:
        if 0 < value < MIN_HEARTBEAT:
            raise ValueError(f"must be 0 (off) or at least {MIN_HEARTBEAT:g}s")
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


class GlobalVars(FreezableBaseModel):
    """
    Global Variables can be defined inside of the configuration file, this is a model for those variables.
    """

    model_config = ConfigDict(extra="forbid")

    terraform_vars: Dict[str, str | bool | list | dict] = Field(
        {}, description="Variables to pass to terraform via a generated .tfvars file."
    )
    remote_vars: Dict[str, str | bool | list | dict] = Field(
        {},
        description="Variables which are used to generate local references to remote state vars.",
    )
    template_vars: Dict[str, str | bool | list | dict] = Field(
        {}, description="Variables which are suppled to any jinja templates."
    )


class DefinitionsSource(BaseModel):
    """
    A source fetched like a definition whose command prints more definitions.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="The local path or git URL of the source.")
    remote_path_options: Optional[DefinitionRemoteOptions] = Field(
        None, description="Options for fetching a remote source."
    )
    command: str = Field(
        description="Command run in the fetched copy; prints definitions as YAML or JSON."
    )
    after: Optional[str] = Field(
        None,
        description="Insert generated definitions after this definition; appended when unset.",
    )


class ConfigFile(FreezableBaseModel):
    """
    This model is used to validate and deserialize the configuration file.
    """

    model_config = ConfigDict(extra="forbid")

    definitions: Dict[str, Any] = Field(
        {}, description="The terraform root modules to run, in order, keyed by name."
    )
    definitions_sources: Dict[str, DefinitionsSource] = Field(
        {}, description="Sources whose commands generate definitions."
    )
    global_vars: Optional[GlobalVars] = Field(
        default_factory=GlobalVars,
        description="Variables merged into every definition.",
    )
    providers: Dict[str, Any] = Field(
        {}, description="Provider requirements and blocks, keyed by provider name."
    )
    worker_options: Dict[str, str | bool] = Field(
        {},
        description="Defaults for command line options, keyed by option name with underscores.",
    )
    handlers: Dict[str, Any] = Field(
        {}, description="Handlers to turn on, keyed by handler name."
    )
    parallel_options: Optional[ParallelOptions] = Field(
        default_factory=ParallelOptions,
        description="How many definitions are prepared and initialized at once.",
    )
    locking: Optional[LockingOptions] = Field(
        None,
        description="Wait for held state locks and keep a worker lease; off when unset.",
    )

    def freeze(self):
        super().freeze()
        if self.global_vars:
            self.global_vars.freeze()
        if self.parallel_options:
            self.parallel_options.freeze()
        if self.locking:
            self.locking.freeze()
        return self
