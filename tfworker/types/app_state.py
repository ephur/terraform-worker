from typing import Any, Dict

from pydantic import BaseModel, ConfigDict

import tfworker.types.cli_options as cli_types
from tfworker.commands.root import RootCommand


class AppState(BaseModel):
    model_config = ConfigDict(
        {
            "extra": "forbid",
            "arbitrary_types_allowed": True,
        }
    )

    root_options: cli_types.CLIOptionsRoot | None = None
    root_command: RootCommand | None = None
    clean_options: cli_types.CLIOptionsClean | None = None
    terraform_options: cli_types.CLIOptionsTerraform | None = None
    loaded_config: Dict[str, Any] | None = {}
