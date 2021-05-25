# Copyright 2020 Richard Maynard (richard.maynard@gmail.com)
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
import shlex
import subprocess


def pipe_exec(args, stdin=None, cwd=None, env=None):
    """
    A function to accept a list of commands and pipe them together.

    Takes optional stdin to give to the first item in the pipe chain.
    """
    count = 0  # int used to manage communication between processes
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
    if stdin is not None:
        popen_stdin_kwargs["stdin"] = subprocess.PIPE
        communicate_kwargs["input"] = stdin.encode()

    # handle the first process
    i = args.pop(0)
    commands.append(
        subprocess.Popen(shlex.split(i), **popen_kwargs, **popen_stdin_kwargs)
    )

    # handle any additional arguments
    for i in args:
        popen_kwargs["stdin"] = commands[count].stdout
        commands.append(subprocess.Popen(shlex.split(i), **popen_kwargs))
        commands[count].stdout.close()
        count = count + 1

    # communicate with first command, ensure it gets any optional input
    commands[0].communicate(**communicate_kwargs)

    # communicate with final command, which will trigger the entire pipeline
    stdout, stderr = commands[-1].communicate()
    returncode = commands[-1].returncode

    return (returncode, stdout, stderr)


def which(program):
    """ From stack overflow """

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
