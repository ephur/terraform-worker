# Copyright 2020-2023 Richard Maynard (richard.maynard@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import re
import shlex
import subprocess

import click
from pkg_resources import DistributionNotFound, get_distribution


def strip_ansi(line):
    """
    Strips ANSI escape sequences from a string.
    """
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", line)


def pipe_exec(args, stdin=None, cwd=None, env=None, stream_output=False):
    """
    A function to accept a list of commands and pipe them together.

    Takes optional stdin to give to the first item in the pipe chain.
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

        commands.append(subprocess.Popen(shlex.split(args[i]), **popen_kwargs))

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
            click.secho(line.rstrip())
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


def which(program):
    """From stack overflow"""

    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file
    return None


def get_version() -> str:
    """
    Get the version of the current package
    """
    try:
        pkg_info = get_distribution("terraform-worker")
        return pkg_info.version
    except DistributionNotFound:
        return "unknown"
