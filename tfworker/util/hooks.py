# This contains utility functions for dealing with hooks
# in terraform definitions, it's primary purpose is to be
# used by the TerraformCommand class, while reducing the
# responsibility of the class itself.
import base64
import json
import os
import re
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict

import tfworker.util.log as log
from tfworker.constants import (
    TF_STATE_CACHE_NAME,
    WORKER_LOCALS_FILENAME,
    WORKER_TFVARS_FILENAME,
)
from tfworker.exceptions import HookError
from tfworker.types.terraform import TerraformAction, TerraformStage
from tfworker.util.system import pipe_exec

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


def get_state_item(
    working_dir: str,
    env: Dict[str, str],
    terraform_bin: str,
    state: str,
    item: str,
    backend: "BaseBackend" = None,
) -> str:
    """
    General handler function for getting a state item. First tries to get the item from another definition's output,
    and if the other definition is not set up, falls back to getting the item from the remote state.

    Args:
        working_dir (str): The working directory of the terraform definition.
        env (dict[str, str]): The environment variables to pass to the terraform command.
        terraform_bin (str): The path to the terraform binary.
        state (str): The state name to get the item from.
        item (str): The item to get from the state.

    Returns:
        str: The state item, key: value as a JSON string.

    Raises:
        HookError: If the state item is not found in the remote state.
    """

    try:
        log.trace(f"Getting state item {state}.{item} from output")
        return _get_state_item_from_output(working_dir, env, terraform_bin, state, item)
    except FileNotFoundError:
        log.trace(
            "Remote state not setup, falling back to getting state item from remote"
        )
        return _get_state_item_from_remote(working_dir, env, terraform_bin, state, item)


def _get_state_item_from_output(
    working_dir: str, env: Dict[str, str], terraform_bin: str, state: str, item: str
) -> str:
    """
    Get a single item from the terraform output. This is the preferred
    mechanism as items will be more guaranteed to be up to date, but it
    creates problems when the remote state is not set up, like when using
    --limit.

    Args:
        working_dir (str): The working directory of the terraform definition.
        env (Dict[str, str]): The environment variables to pass to the terraform command.
        terraform_bin (str): The path to the terraform binary.
        state (str): The state name to get the item from.
        item (str): The item to get from the state.

    Returns:
        str: The item from the terraform output in JSON format.

    Raises:
        HookError: If there is an error reading the remote state item or if the output is empty or not in JSON format.
    """
    base_dir, _ = os.path.split(working_dir)
    try:
        (exit_code, stdout, stderr) = pipe_exec(
            f"{terraform_bin} output -json -no-color {item}",
            cwd=f"{base_dir}/{state}",
            env=env,
        )
    except FileNotFoundError:
        # the remote state is not setup, likely do to use of --limit
        # this is acceptable, and is the responsibility of the hook
        # to ensure it has all values needed for safe execution
        raise

    if exit_code != 0:
        raise HookError(
            f"Error reading remote state item {state}.{item}, details: {stderr.decode()}"
        )

    if stdout is None:
        raise HookError(
            f"Remote state item {state}.{item} is empty; This is completely"
            " unexpected, failing..."
        )

    try:
        json_output = json.loads(stdout)
    except json.JSONDecodeError:
        raise HookError(
            f"Error parsing remote state item {state}.{item}; output is not in JSON format"
        )

    return json.dumps(json_output, indent=None, separators=(",", ":"))


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
    _populate_environment_with_terraform_remote_vars(
        local_env, working_dir, terraform_path, b64_encode
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


def _populate_environment_with_terraform_variables(
    local_env: Dict[str, str], working_dir: str, terraform_path: str, b64_encode: bool
) -> None:
    """
    Populates the environment with Terraform variables.

    Args:
        local_env (Dict[str, str]): The environment variables.
        working_dir (str): The working directory of the Terraform definition.
        terraform_path (str): The path to the Terraform binary.
        b64_encode (bool): If True, variables will be base64 encoded.
    """
    if not os.path.isfile(os.path.join(working_dir, WORKER_TFVARS_FILENAME)):
        return

    with open(os.path.join(working_dir, WORKER_TFVARS_FILENAME)) as f:
        contents = f.read()

    for line in contents.splitlines():
        tf_var = line.split("=")
        _set_hook_env_var(
            local_env, TFHookVarType.VAR, tf_var[0], tf_var[1], b64_encode
        )


def _populate_environment_with_terraform_remote_vars(
    local_env: Dict[str, str], working_dir: str, terraform_path: str, b64_encode: bool
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

    for line in contents.splitlines():
        m = r.match(line)
        if m:
            item = m.group("item")
            state = m.group("state")
            state_item = m.group("state_item")
            state_value = get_state_item(
                working_dir, local_env, terraform_path, state, state_item
            )
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
    value: str,
    b64_encode: bool = False,
) -> None:
    """
    Sets a hook environment variable.

    Args:
        local_env (Dict[str, str]): The environment variables.
        var_type (TFHookVarType): The type of the variable.
        key (str): The key of the variable.
        value (str): The value of the variable.
        b64_encode (bool, optional): If True, the value will be base64 encoded. Defaults to False.
    """
    key_replace_items = {" ": "", '"': "", "-": "_", ".": "_"}
    val_replace_items = {" ": "", '"': "", "\n": ""}

    for k, v in key_replace_items.items():
        key = key.replace(k, v)

    for k, v in val_replace_items.items():
        if isinstance(value, str):
            value = value.replace(k, v)
        if isinstance(value, bytes):
            value = value.decode().replace(k, v)
        if isinstance(value, bool):
            value = str(value).upper()

    if b64_encode:
        value = base64.b64encode(value.encode())

    local_env[f"{var_type}_{key.upper()}"] = value


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
    exit_code, stdout, stderr = pipe_exec(
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


def _get_state_item_from_remote(
    working_dir: str, env: Dict[str, str], terraform_bin: str, state: str, item: str
) -> str:
    """
    Retrieve a state item from terraform remote state.

    Args:
        working_dir: The working directory of the terraform definition.
        env: The environment variables to pass to the terraform command.
        terraform_bin: The path to the terraform binary.
        state: The state name to get the item from.
        item: The item to get from the state.

    Returns:
        A JSON string of the state item.

    Raises:
        HookError: If the state item cannot be found or read.
    """
    cache_file = _get_state_cache_name(working_dir)
    _make_state_cache(working_dir, env, terraform_bin)

    state_cache = _read_state_cache(cache_file)
    remote_state = _find_remote_state(state_cache, state)

    return _get_item_from_remote_state(remote_state, state, item)


def _get_state_cache_name(working_dir: str) -> str:
    """
    Get the name of the state cache file.

    Args:
        working_dir (str): The working directory of the terraform definition.

    Returns:
        str: The name of the state cache file.
    """
    return f"{working_dir}/{TF_STATE_CACHE_NAME}"


def _make_state_cache(
    working_dir: str, env: Dict[str, str], terraform_bin: str, refresh: bool = False
) -> None:
    """
    Create a cache of the terraform state file.

    Args:
        working_dir (str): The working directory of the terraform definition.
        env ({str, str}): The environment variables to pass to the terraform command.
        terraform_bin (str): The path to the terraform binary.
        refresh (bool, optional): If true, the cache will be refreshed. Defaults to False.

    Raises:
        HookError: If there is an error reading or writing the state cache.
    """
    state_cache = _get_state_cache_name(working_dir)
    if not refresh and os.path.exists(state_cache):
        return

    _run_terraform_refresh(terraform_bin, working_dir, env)
    state_json = _run_terraform_show(terraform_bin, working_dir, env)
    _write_state_cache(state_cache, state_json)


def _read_state_cache(cache_file: str) -> Dict[str, Any]:
    """
    Read the state cache from a file.

    Args:
        cache_file (str): The path to the state cache file.

    Returns:
        Dict[str, Any]: The state cache JSON.
    """
    with open(cache_file, "r") as f:
        return json.load(f)


def _find_remote_state(state_cache: Dict[str, Any], state: str) -> Dict[str, Any]:
    """
    Find the remote state in the state cache.

    Args:
        state_cache (Dict[str, Any]): The state cache JSON.
        state (str): The state name to find.

    Returns:
        Dict[str, Any]: The remote state JSON.

    Raises:
        HookError: If the remote state is not found in the state cache.
    """
    resources = state_cache["values"]["root_module"]["resources"]
    for resource in resources:
        if resource["type"] == "terraform_remote_state" and resource["name"] == state:
            return resource
    raise HookError(f"Remote state item {state} not found")


def _get_item_from_remote_state(
    remote_state: Dict[str, Any], state: str, item: str
) -> str:
    """
    Get an item from the remote state JSON

    Args:
        remote_state (Dict[str, Any]): The remote state JSON.
        state (str): The state name.
        item (str): The item to get from the state.

    Returns:
        str: The item from the remote state in JSON format.

    Raises:
        HookError: If the item is not found in the remote state.
    """
    if item in remote_state["values"]["outputs"]:
        return json.dumps(
            remote_state["values"]["outputs"][item],
            indent=None,
            separators=(",", ":"),
        )
    raise HookError(f"Remote state item {state}.{item} not found in state cache")


def _run_terraform_refresh(
    terraform_bin: str, working_dir: str, env: Dict[str, str]
) -> None:
    """
    Run `terraform apply -refresh-only` to ensure the state is refreshed.

    Args:
        terraform_bin (str): The path to the terraform binary.
        working_dir (str): The working directory of the terraform definition.
        env (Dict[str, str]): The environment variables to pass to the terraform command.

    Raises:
        HookError: If there is an error refreshing the terraform state.
    """
    exit_code, _, stderr = pipe_exec(
        f"{terraform_bin} apply -auto-approve -refresh-only",
        cwd=working_dir,
        env=env,
    )
    if exit_code != 0:
        raise HookError(f"Error applying terraform state, details: {stderr}")


def _run_terraform_show(
    terraform_bin: str, working_dir: str, env: Dict[str, str]
) -> str:
    """
    Run `terraform show -json` to get the state in JSON format.

    Args:
        terraform_bin (str): The path to the terraform binary.
        working_dir (str): The working directory of the terraform definition.
        env (Dict[str, str]): The environment variables to pass to the terraform command.

    Returns:
        str: The state in JSON format.

    Raises:
        HookError: If there is an error reading the terraform state.
    """
    exit_code, stdout, stderr = pipe_exec(
        f"{terraform_bin} show -json",
        cwd=working_dir,
        env=env,
    )
    if exit_code != 0:
        raise HookError(f"Error reading terraform state, details: {stderr}")
    try:
        json.loads(stdout)
    except json.JSONDecodeError:
        raise HookError("Error parsing terraform state; output is not in JSON format")
    return stdout.decode()


def _write_state_cache(state_cache: str, state_json: str) -> None:
    """
    Write the state JSON to the cache file.

    Args:
        state_cache (str): The path to the state cache file.
        state_json (str): The state JSON to write.

    Raises:
        HookError: If there is an error writing the state cache.
    """
    try:
        with open(state_cache, "w") as f:
            f.write(state_json)
    except Exception as e:
        raise HookError(f"Error writing state cache to {state_cache}, details: {e}")
