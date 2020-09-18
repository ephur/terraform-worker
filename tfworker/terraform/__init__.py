class Terraform:
    def __init__(self):
        self._backend = None

    def render_providers(self.providers, args):
        """Return a string that provides the provider configuration."""
        # prov_string as a list is funny sounding, but it gets joined and returned as a string
        prov_string = []
        for provider in providers:
            provider_vars = {}
            try:
                for k, v in providers[provider]["vars"].items():
                    provider_vars[k] = v
            except (KeyError, TypeError):
                """No provider vars were set."""
                pass
            prov_string.append('provider "{}" {{'.format(provider))
            for k, v in provider_vars.items():
                prov_string.append('  {} = "{}"'.format(k, v))
            prov_string.append("}")
        return "\n".join(prov_string)

    def run(
        name,
        temp_dir,
        terraform_path,
        command,
        key_id=None,
        key_secret=None,
        key_token=None,
        debug=False,
        plan_action="apply",
        b64_encode=False,
    ):
        """Run terraform."""
        params = {
            "init": "-input=false -no-color",
            "plan": "-input=false -detailed-exitcode -no-color",
            "apply": "-input=false -no-color -auto-approve",
            "destroy": "-input=false -no-color -force",
        }

        if plan_action == "destroy":
            params["plan"] += " -destroy"

        env = os.environ.copy()
        env["AWS_ACCESS_KEY_ID"] = key_id
        env["AWS_SECRET_ACCESS_KEY"] = key_secret
        if key_token is not None:
            env["AWS_SESSION_TOKEN"] = key_token
        env["TF_PLUGIN_CACHE_DIR"] = "{}/terraform-plugins".format(temp_dir)

        working_dir = "{}/definitions/{}".format(temp_dir, name)
        command_params = params.get(command)
        if not command_params:
            raise ValueError(
                "invalid command passed to terraform, {} has no defined params!".format(
                    command
                )
            )

        # only execute hooks for apply/destroy
        try:
            if check_hooks("pre", working_dir, command) and command in ["apply", "destroy"]:
                # pre exec hooks
                # want to pass remotes
                # want to pass tf_vars
                click.secho(
                    "found pre-{} hook script for definition {}, executing ".format(
                        command, name
                    ),
                    fg="yellow",
                )
                hook_exec(
                    "pre",
                    command,
                    name,
                    working_dir,
                    env,
                    terraform_path,
                    debug=debug,
                    b64_encode=b64_encode,
                )
        except HookError as e:
            click.secho(
                "hook execution error on definition {}: {}".format(name, e), fg="red"
            )
            raise SystemExit(1)

        (exit_code, stdout, stderr) = pipe_exec(
            "{} {} {}".format(terraform_path, command, command_params),
            cwd=working_dir,
            env=env,
        )
        if debug:
            click.secho("exit code: {}".format(exit_code), fg="blue")
            for line in stdout.decode().splitlines():
                click.secho("stdout: {}".format(line), fg="blue")
            for line in stderr.decode().splitlines():
                click.secho("stderr: {}".format(line), fg="red")

        # special handling of the exit codes for "plan" operations
        if command == "plan":
            if exit_code == 0:
                return True
            if exit_code == 1:
                raise TerraformError
            if exit_code == 2:
                raise PlanChange

        if exit_code:
            raise TerraformError

        # only execute hooks for plan/destroy
        try:
            if check_hooks("post", working_dir, command) and command in [
                "apply",
                "destroy",
            ]:
                # post exec hooks
                # want to pass remotes
                # want to pass tf_vars
                click.secho(
                    "found post-{} hook script for definition {}, executing ".format(
                        command, name
                    ),
                    fg="yellow",
                )
                # hook_exec("what", "args", "are", "needed")
        except HookError as e:
            click.secho(
                "hook execution error on definition {}: {}".format(name, e), fg="red"
            )
            raise SystemExit(1)
        return True


    def check_hooks(phase, working_dir, command):
        """
        check_hooks determines if a hook exists for a given operation/definition
        """
        hook_dir = "{}/hooks".format(working_dir)
        if not os.path.isdir(hook_dir):
            # there is no hooks dir
            return False
        for f in os.listdir(hook_dir):
            if os.path.splitext(f)[0] == "{}_{}".format(phase, command):
                if os.access("{}/{}".format(hook_dir, f), os.X_OK):
                    return True
                else:
                    raise HookError(
                        "{}/{} exists, but is not executable!".format(hook_dir, f)
                    )
        return False


    def hook_exec(
        phase,
        command,
        name,
        working_dir,
        env,
        terraform_path,
        debug=False,
        b64_encode=False,
    ):
        """
        hook_exec executes a hook script.

        Before execution it sets up the environment to make all terraform and remote
        state variables available to the hook via environment vars
        """

        key_replace_items = {
            " ": "",
            '"': "",
            "-": "_",
            ".": "_",
        }
        val_replace_items = {
            " ": "",
            '"': "",
            "\n": "",
        }
        local_env = env.copy()
        hook_dir = "{}/hooks".format(working_dir)
        hook_script = None

        for f in os.listdir(hook_dir):
            # this file format is specifically structured by the prep_def function
            if os.path.splitext(f)[0] == "{}_{}".format(phase, command):
                hook_script = "{}/{}".format(hook_dir, f)
        # this should never have been called if the hook script didn't exist...
        if hook_script is None:
            raise HookError("hook script missing from {}".format(hook_dir))

        # populate environment with terraform vars
        if os.path.isfile("{}/worker-locals.tf".format(working_dir)):
            # I'm sorry.
            r = re.compile(
                r"\s*(?P<item>\w+)\s*\=.+data\.terraform_remote_state\.(?P<state>\w+)\.outputs\.(?P<state_item>\w+)\s*"
            )

            with open("{}/worker-locals.tf".format(working_dir)) as f:
                for line in f:
                    m = r.match(line)
                    if m:
                        item = m.group("item")
                        state = m.group("state")
                        state_item = m.group("state_item")
                    else:
                        continue

                    state_value = get_state_item(
                        working_dir, env, terraform_path, state, state_item
                    )

                    if state_value is not None:
                        if b64_encode:
                            state_value = base64.b64encode(
                                state_value.encode("utf-8")
                            ).decode()
                        local_env[
                            "TF_REMOTE_{}_{}".format(state, item).upper()
                        ] = state_value

        # populate environment with terraform remotes
        if os.path.isfile("{}/worker.auto.tfvars".format(working_dir)):
            with open("{}/worker.auto.tfvars".format(working_dir)) as f:
                for line in f:
                    tf_var = line.split("=")

                    # strip bad names out for env var settings
                    for k, v in key_replace_items.items():
                        tf_var[0] = tf_var[0].replace(k, v)

                    for k, v in val_replace_items.items():
                        tf_var[1] = tf_var[1].replace(k, v)

                    if b64_encode:
                        tf_var[1] = base64.b64encode(tf_var[1].encode("utf-8")).decode()

                    local_env["TF_VAR_{}".format(tf_var[0].upper())] = tf_var[1]
        else:
            click.secho("{}/worker.auto.tfvars not found!".format(working_dir), fg="red")

        # execute the hook
        (exit_code, stdout, stderr) = pipe_exec(
            "{} {} {}".format(hook_script, phase, command), cwd=hook_dir, env=local_env,
        )

        # handle output from hook_script
        if debug:
            click.secho("exit code: {}".format(exit_code), fg="blue")
            for line in stdout.decode().splitlines():
                click.secho("stdout: {}".format(line), fg="blue")
            for line in stderr.decode().splitlines():
                click.secho("stderr: {}".format(line), fg="red")

        if exit_code != 0:
            raise HookError("hook script {}")


    def get_state_item(working_dir, env, terraform_path, state, item):
        """
        get_state_item returns json encoded output from a terraform remote state
        """
        base_dir, _ = os.path.split(working_dir)
        try:
            (exit_code, stdout, stderr) = pipe_exec(
                "{} output -json -no-color {}".format(terraform_path, item),
                cwd="{}/{}".format(base_dir, state),
                env=env,
            )
        except FileNotFoundError:
            # the remote state is not setup, likely do to use of --limit
            # this is acceptable, and is the responsibility of the hook
            # to ensure it has all values needed for safe execution
            return None

        if exit_code != 0:
            raise HookError(
                "Error reading remote state item {}.{}, details: {}".format(
                    state, item, stderr
                )
            )

        if stdout is None:
            raise HookError(
                "Remote state item {}.{} is empty; This is completely unexpected, failing...".format(
                    state, item
                )
            )
        json_output = json.loads(stdout)
        return json.dumps(json_output, indent=None, separators=(",", ":"))
