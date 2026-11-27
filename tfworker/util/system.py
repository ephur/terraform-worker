import os
import platform
import re
import shlex
import subprocess
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import tfworker.util.log as log


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
    stream_log_level: log.LogLevel | None = None,
) -> Tuple[int, Union[bytes, None], Union[bytes, None]]:
    """
    A function to take one or more commands and execute them in a pipeline, returning the output of the last command.

    Args:
        args (str or list): A string or list of strings representing the command(s) to execute.
        stdin (str, optional): A string to pass as stdin to the first command
        cwd (str, optional): The working directory to execute the command in.
        env (dict, optional): A dictionary of environment variables to set for the command.
        stream_output (bool, optional): A boolean indicating if the output should be streamed back to the caller.
        stream_log_level (LogLevel, optional): The logger level to use when
            streaming output through the application logger.

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
            rendered_line = line.rstrip()
            if log.json_logging_enabled():
                stdout += line
                continue
            if stream_log_level is not None:
                log.log(rendered_line, level=stream_log_level)
            else:
                print(rendered_line)
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


def pipe_exec_logged(
    args: Union[str, List[str]],
    label: str,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    stream_output: bool = True,
    stream_log_level: Optional[log.LogLevel] = None,
    success_level: Optional[log.LogLevel] = None,
    ok_exit_codes: Iterable[int] = (0,),
    extra: Optional[Dict[str, Any]] = None,
    message: Optional[str] = None,
    debug_when_not_streaming: bool = False,
    exec_fn: Optional[Callable] = None,
) -> Tuple[int, Union[bytes, None], Union[bytes, None]]:
    """
    Run a command and deliver its output the way the active log format needs.

    Streaming and aggregating are mutually exclusive: a text run streams the
    child's output line by line as it happens, while a JSON run captures it and
    emits one structured record, because streamed lines would otherwise become
    a series of records with no context attached. Every caller needs that same
    decision, so it lives here rather than at each call site.

    Args:
        args: the command to run, passed through to the exec function
        label: command name recorded in the structured result (e.g. "terraform init")
        cwd: working directory for the command
        env: environment for the command; the exec default is used when None
        stream_output: whether the caller wants live output at all
        stream_log_level: level for streamed lines (default INFO)
        success_level: level for the result record when the command succeeded
            (default INFO)
        ok_exit_codes: exit codes that are not failures; `terraform plan`
            returns 2 for "changes present"
        extra: fields to attach to the structured result record
        message: message for the structured result record
        debug_when_not_streaming: emit the captured output at debug level when
            nothing was streamed and no structured record was produced, so the
            output is not lost entirely
        exec_fn: the exec function to use, defaults to pipe_exec

    Returns:
        tuple: the exit code, stdout, and stderr of the command
    """
    if exec_fn is None:
        exec_fn = pipe_exec
    if stream_log_level is None:
        stream_log_level = log.LogLevel.INFO
    if success_level is None:
        success_level = log.LogLevel.INFO

    aggregate_output = log.json_logging_enabled()
    effective_stream_output = stream_output and not aggregate_output

    exec_kwargs: Dict[str, Any] = {
        "cwd": cwd,
        "stream_output": effective_stream_output,
    }
    if env is not None:
        exec_kwargs["env"] = env
    if effective_stream_output:
        exec_kwargs["stream_log_level"] = stream_log_level

    exit_code, stdout, stderr = exec_fn(args, **exec_kwargs)

    if aggregate_output:
        log.log_subprocess_result(
            command=label,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            level=(success_level if exit_code in ok_exit_codes else log.LogLevel.ERROR),
            extra=extra,
            message=message,
        )
    elif debug_when_not_streaming and not effective_stream_output:
        log.debug(f"{label} result: {(stdout or b'').decode(errors='replace')}")
        log.debug(f"{label} error: {(stderr or b'').decode(errors='replace')}")

    return (exit_code, stdout, stderr)


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
