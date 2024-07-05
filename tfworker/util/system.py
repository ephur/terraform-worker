import os
import platform
import re
import shlex
import subprocess
from typing import Dict, List, Tuple, Union


def strip_ansi(line: str) -> str:
    """
    Strips ANSI escape sequences from a string.

    Args:
        line (str): The string to strip ANSI escape sequences from.

    Returns:
        str: The string with ANSI escape sequences stripped.
    """
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", line)


def pipe_exec(
    args: Union[str, List[str]],
    stdin: str = None,
    cwd: str = None,
    env: Dict[str, str] = None,
    stream_output: bool = False,
) -> Tuple[int, Union[bytes, None], Union[bytes, None]]:
    """
    A function to take one or more commands and execute them in a pipeline, returning the output of the last command.

    Args:
        args (str or list): A string or list of strings representing the command(s) to execute.
        stdin (str, optional): A string to pass as stdin to the first command
        cwd (str, optional): The working directory to execute the command in.
        env (dict, optional): A dictionary of environment variables to set for the command.
        stream_output (bool, optional): A boolean indicating if the output should be streamed back to the caller.

    Returns:
        tuple: A tuple containing the return code, stdout, and stderr of the last command in the pipeline.
    """
    commands = []  # listed used to hold all the popen objects
    # use the default environment if one is not specified
    if env is None:
        env = os.environ.copy()

    # if a single command was passed as a string, make it a list
    if not isinstance(args, list):
        args = [args]

    # setup various arguments for popen/popen.communicate, account for optional stdin
    popen_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": cwd,
        "env": env,
    }
    popen_stdin_kwargs = {}
    communicate_kwargs = {}

    if stream_output is True:
        popen_kwargs["bufsize"] = 1
        popen_kwargs["universal_newlines"] = True

    if stdin is not None:
        popen_stdin_kwargs["stdin"] = subprocess.PIPE
        communicate_kwargs["input"] = stdin.encode()

    if len(args) == 1 and stream_output is True:
        popen_kwargs["stderr"] = subprocess.STDOUT

    # handle the first command, requires distinct handling
    i = args.pop(0)
    commands.append(
        subprocess.Popen(shlex.split(i), **popen_kwargs, **popen_stdin_kwargs)
    )

    # handle any additional commands
    # every process now gets stdin as a pipe
    for i, cmd_str in enumerate(args):
        lastloop = True if len(args) - 1 == i else False
        popen_kwargs["stdin"] = commands[-1].stdout

        if lastloop and stream_output:
            popen_kwargs["stderr"] = subprocess.STDOUT

        commands.append(subprocess.Popen(shlex.split(cmd_str), **popen_kwargs))

        # close stdout on the command before we just added to allow recieving SIGPIPE
        commands[-2].stdout.close()

    if stream_output is True:
        # in order to stream the output, stderr and stdout streams must be combined to avoid
        # any potential blocking, for this reason the execution methods are different
        stdout = ""

        # if there is more than one command we need to use communicate on the first to send
        # in stdin and still allowing the pipeline to properly process
        if len(commands) > 1:
            # communicate in this instance needs a string type object, not bytes
            communicate_kwargs["input"] = stdin
            commands[0].communicate(**communicate_kwargs)

        else:
            # if it's just a single command we can not use communicate or we will not be able
            # to stream the output, so write directly to stdin
            if stdin is not None and len(commands) == 1:
                commands[0].stdin.write(stdin + "\n")
                commands[0].stdin.close()

        # for a single command this will be the only command, for a pipeline reading from the
        # last command will trigger all of the commands, communicating through their pipes
        for line in iter(commands[-1].stdout.readline, ""):
            print(line.rstrip())
            stdout += line

        # for streaming output stderr will be included with stdout, there's no way to make
        # a distinction, so stderr will always be an empty bytes object
        stderr = "".encode()
        stdout = stdout.encode()
        commands[-1].wait()
        returncode = commands[-1].poll()

    else:
        # if stdin is not None:
        if len(commands) > 1:
            # in this case communicate_kwargs must only be passed to the first
            # command in the pipe, and must NOT be passed to any other as the stdout/stdin
            # is chained between the piped commands
            commands[0].communicate(**communicate_kwargs)
            stdout, stderr = commands[-1].communicate()
            returncode = commands[-1].returncode
        else:
            stdout, stderr = commands[0].communicate(**communicate_kwargs)
            returncode = commands[0].returncode

    return (returncode, stdout, stderr)


def get_platform() -> Tuple[str, str]:
    """
    Returns a formatted operating system / architecture tuple that is consistent with common distribution creation tools.

    Returns:
        tuple: A tuple containing the operating system and architecture.
    """

    # strip off "2" which only appears on old linux kernels
    opsys = platform.system().rstrip("2").lower()

    # make sure machine uses consistent format
    machine = platform.machine()
    if machine == "x86_64":
        machine = "amd64"

    # some 64 bit arm extensions will `report aarch64, this is functionaly
    # equivalent to arm64 which is recognized and the pattern used by the TF
    # community
    if machine == "aarch64":
        machine = "arm64"
    return (opsys, machine)
