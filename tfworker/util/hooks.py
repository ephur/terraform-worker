# This contains utility functions for dealing with hooks
# in terraform definitions, it's primary purpose is to be
# used by the TerraformCommand class, while reducing the
# responsibility of the class itself.
import base64
import json
import os
import re
import shlex
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import hcl2
from lark.exceptions import UnexpectedToken

import tfworker.util.log as log
from tfworker.constants import WORKER_LOCALS_FILENAME, WORKER_TFVARS_FILENAME
from tfworker.custom_types.terraform import TerraformAction, TerraformStage
from tfworker.exceptions import HookError
from tfworker.util.system import pipe_exec


# --- Safe pipe_exec wrapper ---
def safe_pipe_exec(
    args: Union[str, List[str]],
    stdin: str = None,
    cwd: str = None,
    env: Dict[str, str] = None,
    stream_output: bool = False,
) -> Tuple[int, Union[bytes, None], Union[bytes, None]]:
    """
    A wrapper around pipe_exec that checks environment variables for unsafe types before calling pipe_exec.
    """
    if env is not None:
        bad_env = {k: v for k, v in env.items() if not isinstance(v, str)}
        if bad_env:
            for k, v in bad_env.items():
                log.error(
                    f"Invalid env var for pipe_exec: {k}={v!r} ({type(v).__name__})"
                )
            raise HookError(
                "Environment contains non-string values, aborting pipe_exec"
            )
    return pipe_exec(args, stdin=stdin, cwd=cwd, env=env, stream_output=stream_output)


if TYPE_CHECKING:
    from tfworker.backends.base import BaseBackend


class TFHookVarType(Enum):
    """
    Enum for the types of hook variables.
    """

    VAR = "TF_VAR"
    REMOTE = "TF_REMOTE"
    EXTRA = "TF_EXTRA"

    def __str__(self):
        return self.value.upper()


def _get_or_fetch_state(
    state: str,
    backend: "BaseBackend",
    state_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Get state data from cache or fetch from backend.

    Checks the cache first to avoid redundant backend calls. If the state
    is not cached, fetches it from the backend, validates it, and caches
    it for future use.

    Args:
        state: The state name to get.
        backend: The backend instance to fetch from.
        state_cache: Optional cache dict to store fetched state data.

    Returns:
        The state data as a dict.

    Raises:
        HookError: If the state data is invalid (not a dict).
    """
    # Check cache first to avoid multiple backend calls for the same state
    if state_cache is not None and state in state_cache:
        log.debug(f"Using cached state data for {state}")
        return state_cache[state]

    # Fetch from backend
    log.debug(f"Fetching state {state} from backend")
    state_data = backend.get_state(state)

    # Validate the state data before caching
    if not isinstance(state_data, dict):
        raise HookError(f"Invalid state data for {state}")

    # Cache the validated state data
    if state_cache is not None:
        state_cache[state] = state_data

    return state_data


def get_state_item(
    working_dir: str,
    env: Dict[str, str],
    terraform_bin: str,
    state: str,
    item: str,
    backend: "BaseBackend",
    state_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """
    Get a state item directly from the backend.

    Uses the backend's get_state() method to retrieve the full state,
    then extracts the requested output item. This is the most reliable
    method as it works in all contexts (pre_init hooks, --limit flag, etc.).

    Caching: If state_cache is provided, the state data will be cached by
    state name to avoid multiple backend calls for the same state.

    Args:
        working_dir: The working directory (unused, kept for API compat).
        env: The environment variables (unused, kept for API compat).
        terraform_bin: The path to terraform binary (unused, kept for API compat).
        state: The state name to get the item from.
        item: The output item name to extract from the state.
        backend: The backend instance to use for fetching state.
        state_cache: Optional dict to cache state data by state name (for performance).

    Returns:
        The state item as a JSON string: {"value": <actual_value>, "type": <type>}

    Raises:
        HookError: If the state or item cannot be found.
        NotImplementedError: If the backend doesn't support get_state().
    """
    try:
        # Get state data (from cache or backend)
        state_data = _get_or_fetch_state(state, backend, state_cache)

        # Extract the output from the state file's top-level outputs section
        # State file format from backend.get_state() is:
        # {
        #   "version": 4,
        #   "outputs": {
        #     "output_name": {
        #       "value": <actual_value>,
        #       "type": <type>
        #     }
        #   }
        # }

        outputs = state_data.get("outputs", {})
        if not outputs:
            raise HookError(f"No outputs found in state '{state}'")

        if item not in outputs:
            raise HookError(f"Output '{item}' not found in state '{state}'")

        output_data = outputs[item]

        # Return in Terraform output format for consistency
        # The output is already in the correct format: {"value": ..., "type": ...}
        return json.dumps(output_data, separators=(",", ":"))

    except NotImplementedError:
        raise HookError(
            f"Backend {type(backend).__name__} does not support get_state(). "
            "Cannot retrieve remote state variables."
        )
    except Exception as e:
        raise HookError(f"Error retrieving state item {state}.{item}: {str(e)}")


def check_hooks(
    phase: TerraformStage, working_dir: str, command: TerraformAction
) -> bool:
    """
    Check if a hook script exists for the given phase and command.

    Args:
        phase (TerraformStage): The phase of the terraform command.
        working_dir (str): The working directory of the terraform definition.
        command (TerraformAction): The terraform command to run.

    Returns:
        bool: True if the hook script exists and is executable, False otherwise.
    """
    hook_dir = f"{working_dir}/hooks"
    if not os.path.isdir(hook_dir):
        return False
    for f in os.listdir(hook_dir):
        if os.path.splitext(f)[0] == f"{phase}_{command}":
            if os.access(f"{hook_dir}/{f}", os.X_OK):
                return True
            else:
                raise HookError(f"{hook_dir}/{f} exists, but is not executable!")
    return False


def hook_exec(
    phase: TerraformStage,
    command: TerraformAction,
    working_dir: str,
    env: Dict[str, str],
    terraform_path: str,
    debug: bool = False,
    b64_encode: bool = False,
    extra_vars: Dict[str, str] = None,
    backend: "BaseBackend" = None,
    disable_remote_state_vars: bool = False,
) -> None:
    """
    Coordinates the execution of a hook script. This function is responsible for finding and executing
    the script, as well as setting up the proper environment variables with all of the terraform vars,
    and vars from remote data sources.

    Args:
        phase (TerraformPhase): The phase of the hook.
        command (TerraformAction): A poorly named variable, that is the terraform action being executed
        working_dir (str): The working directory of the Terraform definition.
        env (Dict[str, str]): The environment variables to pass to the hook.
        terraform_path (str): The path to the Terraform binary.
        debug (bool, optional): If True, debug information will be printed. Defaults to False.
        b64_encode (bool, optional): If True, variables will be base64 encoded. Defaults to False.
        extra_vars (Dict[str, str], optional): Additional environment variables to set. Defaults to None.

    Raises:
        HookError: If the hook script is missing or if execution fails.
    """
    if extra_vars is None:
        extra_vars = {}

    local_env = _prepare_environment(env, terraform_path)
    hook_script = _find_hook_script(working_dir, phase, command)
    _populate_environment_with_terraform_variables(
        local_env, working_dir, terraform_path, b64_encode
    )
    if not disable_remote_state_vars:
        _populate_environment_with_terraform_remote_vars(
            local_env, working_dir, terraform_path, b64_encode, backend
        )
    _populate_environment_with_extra_vars(local_env, extra_vars, b64_encode)
    _execute_hook_script(hook_script, phase, command, working_dir, local_env, debug)


def _find_hook_script(working_dir: str, phase: str, command: str) -> str:
    """
    Finds the hook script to execute.

    Args:
        working_dir (str): The working directory of the Terraform definition.
        phase (str): The phase of the hook.
        command (str): The command to execute.

    Returns:
        str: The path to the hook script.

    Raises:
        HookError: If the hook script is missing.
    """
    hook_dir = os.path.join(working_dir, "hooks")
    for f in os.listdir(hook_dir):
        if os.path.splitext(f)[0] == f"{phase}_{command}":
            return os.path.join(hook_dir, f)
    raise HookError(f"Hook script missing from {hook_dir}")


def _prepare_environment(env: Dict[str, str], terraform_path: str) -> Dict[str, str]:
    """
    Prepares the environment variables for the hook script execution.

    Args:
        env (Dict[str, str]): The initial environment variables.
        terraform_path (str): The path to the Terraform binary.

    Returns:
        Dict[str, str]: The prepared environment variables.
    """
    local_env = env.copy()
    local_env["TF_PATH"] = terraform_path
    return local_env


def _parse_tfvars_file(tfvars_path: str) -> Dict[str, Any]:
    """
    Parse a .tfvars file into a dictionary of native Python types.

    Uses python-hcl2 to properly parse HCL syntax, ensuring booleans,
    numbers, strings, lists, and maps are correctly typed.

    Args:
        tfvars_path: Path to the .tfvars file

    Returns:
        Dictionary mapping variable names to their parsed values

    Raises:
        FileNotFoundError: If the file doesn't exist
    """
    try:
        with open(tfvars_path, "r") as f:
            parsed = hcl2.load(f)
            return parsed
    except FileNotFoundError:
        raise
    except (UnexpectedToken, Exception) as e:
        # Fallback to simple parsing for robustness
        log.warn(
            f"Failed to parse {tfvars_path} as HCL2, falling back to simple parsing: {e}"
        )
        result = {}
        with open(tfvars_path, "r") as f:
            for line in f.read().splitlines():
                if "=" in line:
                    parts = line.split("=", 1)
                    key = parts[0].strip()
                    value = parts[1].strip()
                    result[key] = value
        return result


def _populate_environment_with_terraform_variables(
    local_env: Dict[str, str], working_dir: str, terraform_path: str, b64_encode: bool
) -> None:
    """
    Populates the environment with Terraform variables from .tfvars file.

    Variables are parsed using HCL2 to ensure proper type handling (booleans,
    numbers, maps, lists) before being passed to _set_hook_env_var().

    Args:
        local_env (Dict[str, str]): The environment variables.
        working_dir (str): The working directory of the Terraform definition.
        terraform_path (str): The path to the Terraform binary.
        b64_encode (bool): If True, variables will be base64 encoded.
    """
    tfvars_path = os.path.join(working_dir, WORKER_TFVARS_FILENAME)
    if not os.path.isfile(tfvars_path):
        return

    # Parse the .tfvars file into native Python types
    terraform_vars = _parse_tfvars_file(tfvars_path)

    # Set each variable in the environment
    for key, value in terraform_vars.items():
        _set_hook_env_var(local_env, TFHookVarType.VAR, key, value, b64_encode)


def _populate_environment_with_terraform_remote_vars(
    local_env: Dict[str, str],
    working_dir: str,
    terraform_path: str,
    b64_encode: bool,
    backend: "BaseBackend",
) -> None:
    """
    Populates the environment with Terraform variables.

    Args:
        local_env (Dict[str, str]): The environment variables.
        working_dir (str): The working directory of the Terraform definition.
        terraform_path (str): The path to the Terraform binary.
        b64_encode (bool): If True, variables will be base64 encoded.
    """
    if not os.path.isfile(os.path.join(working_dir, WORKER_LOCALS_FILENAME)):
        return

    with open(os.path.join(working_dir, WORKER_LOCALS_FILENAME)) as f:
        contents = f.read()

    # I'm sorry. :-)
    # this regex looks for variables in the form of:
    # <var_name, ITEM> = data.terraform_remote_state.<the name of a remote definition, STATE>.outputs.<the name of an output, STATE_ITEM>
    r = re.compile(
        r"\s*(?P<item>\w+)\s*\=.+data\.terraform_remote_state\.(?P<state>\w+)\.outputs\.(?P<state_item>\w+)\s*"
    )

    # Create a cache dict to avoid multiple backend calls for the same state
    state_cache: Dict[str, Dict[str, Any]] = {}

    for line in contents.splitlines():
        m = r.match(line)
        if m:
            item = m.group("item")
            state = m.group("state")
            state_item = m.group("state_item")
            state_value_json = get_state_item(
                working_dir,
                local_env,
                terraform_path,
                state,
                state_item,
                backend,
                state_cache,
            )

            # Parse the Terraform output JSON to extract just the value field
            # Terraform output format is: {"value": <actual_value>, "type": <type>, "sensitive": <bool>}
            try:
                state_output = json.loads(state_value_json)
                # Extract just the value field from the Terraform output structure
                if isinstance(state_output, dict) and "value" in state_output:
                    state_value = state_output["value"]
                else:
                    # Fallback to the raw value if it's not in the expected format
                    state_value = state_value_json
            except (json.JSONDecodeError, TypeError):
                # If parsing fails, use the raw value
                state_value = state_value_json

            _set_hook_env_var(
                local_env, TFHookVarType.REMOTE, item, state_value, b64_encode
            )


def _populate_environment_with_extra_vars(
    local_env: Dict[str, str], extra_vars: Dict[str, Any], b64_encode: bool
) -> None:
    """
    Populates the environment with extra variables.

    Args:
        local_env (Dict[str, str]): The environment variables.
        extra_vars (Dict[str, Any]): The extra variables to set.
        b64_encode (bool): If True, variables will be base64 encoded.
    """
    for k, v in extra_vars.items():
        _set_hook_env_var(local_env, TFHookVarType.EXTRA, k, v, b64_encode)


def _set_hook_env_var(
    local_env: Dict[str, str],
    var_type: TFHookVarType,
    key: str,
    value: Any,
    b64_encode: bool = False,
) -> None:
    """
    Sets a hook environment variable.

    Args:
        local_env (Dict[str, str]): The environment variables.
        var_type (TFHookVarType): The type of the variable.
        key (str): The key of the variable.
        value (Any): The value in native Python type (bool, str, int, dict, list, etc.).
        b64_encode (bool, optional): If True, the value will be base64 encoded. Defaults to False.
    """
    # Normalize the key into a safe env var suffix
    key_replace_items = {" ": "", '"': "", "-": "_", ".": "_"}
    for k, v in key_replace_items.items():
        key = key.replace(k, v)

    # If base64 encoding is requested, do not mutate the value except for encoding to str
    if b64_encode:
        if isinstance(value, bytes):
            raw_bytes = value
        elif isinstance(value, str):
            raw_bytes = value.encode()
        else:
            # Fall back to JSON/str then bytes to avoid lossy transforms
            try:
                raw_bytes = json.dumps(value).encode()
            except Exception:
                raw_bytes = str(value).encode()

        # Base64-encode and store as a string to satisfy env type requirements
        encoded = base64.b64encode(raw_bytes).decode()
        local_env[f"{var_type}_{key.upper()}"] = encoded
        return

    # For non-base64 values, ensure we end up with a string and shell-escape it
    # If the object is a complex type (e.g., dict or list), JSON-encode first
    if isinstance(value, bytes):
        value_str = value.decode()
    elif isinstance(value, bool):
        value_str = str(value).upper()
    elif isinstance(value, (dict, list, tuple)):
        try:
            # Use compact JSON formatting to match the rest of the codebase
            value_str = json.dumps(value, separators=(",", ":"))
        except Exception:
            value_str = str(value)
    else:
        value_str = str(value)

    # Use shlex.quote to safely escape values for shell consumption by hooks
    value_escaped = shlex.quote(value_str)

    local_env[f"{var_type}_{key.upper()}"] = value_escaped


def _execute_hook_script(
    hook_script: str,
    phase: str,
    command: str,
    working_dir: str,
    local_env: Dict[str, str],
    debug: bool,
    stream_output: bool = False,
) -> None:
    """
    Executes the hook script and handles its output.

    Args:
        hook_script (str): The path to the hook script.
        phase (str): The phase of the hook.
        command (str): The command to execute.
        working_dir (str): The working directory of the Terraform definition.
        local_env (Dict[str, str]): The environment variables.
        debug (bool): If True, debug information will be printed.

    Raises:
        HookError: If the hook script execution fails.
    """
    hook_dir = os.path.join(working_dir, "hooks")
    log.trace(
        f"Executing hook script: {hook_script} in {hook_dir} with params {phase} {command} "
    )
    exit_code, stdout, stderr = safe_pipe_exec(
        f"{hook_script} {phase} {command}",
        cwd=hook_dir,
        env=local_env,
        stream_output=stream_output,
    )

    if debug:
        log.debug(f"Results from hook script: {hook_script}")
        log.debug(f"exit code: {exit_code}")
        if not stream_output:
            for line in stdout.decode().splitlines():
                log.debug(f"stdout: {line}")
            for line in stderr.decode().splitlines():
                log.debug(f"stderr: {line}")

    if exit_code != 0:
        raise HookError(
            f"Hook script {hook_script} execution failed with exit code {exit_code}"
        )
