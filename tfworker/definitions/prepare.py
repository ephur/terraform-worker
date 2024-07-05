import json
from os import environ
from typing import TYPE_CHECKING, Dict, Union

import jinja2

import tfworker.util.log as log
from tfworker.constants import (
    RESERVED_FILES,
    TF_PROVIDER_DEFAULT_LOCKFILE,
    WORKER_LOCALS_FILENAME,
    WORKER_TF_FILENAME,
    WORKER_TFVARS_FILENAME,
)
from tfworker.copier import Copier, CopyFactory
from tfworker.exceptions import ReservedFileError, TFWorkerException
from tfworker.util.terraform import generate_terraform_lockfile

if TYPE_CHECKING:
    from click import Context

    from tfworker.app_state import AppState
    from tfworker.types.definition import Definition


TERRAFORM_TPL = """\
terraform {{
{0}
{1}
}}

"""


class DefinitionPrepare:
    """
    TerraformPrepare is a class that prepares a definition to be ready for terraform init
    """

    def __init__(self, ctx: "Context", app_state: "AppState"):
        self._ctx: "Context" = ctx
        self._app_state: "AppState" = app_state

    def copy_files(self, name: str) -> None:
        """
        Prepare is an orchestration function that prepares a definition to be ready for terraform init
        """
        definition = self._app_state.definitions[name]

        log.trace(
            f"fetching copier for definition {name} with path {definition.path} and repo_path {self._app_state.root_options.repository_path}"
        )
        try:
            c = get_coppier(
                definition.path, self._app_state.root_options.repository_path
            )
        except NotImplementedError as e:
            raise TFWorkerException(
                f"could not handle source path {definition.path} for definition {name}, either file does not exist or could not handle remote URI"
            ) from e

        log.trace(
            f"putting definition {name} in {definition.get_target_path(self._app_state.working_dir)} with copier {c.__class__.__name__}"
        )
        try:
            copy(
                copier=c,
                destination=definition.get_target_path(self._app_state.working_dir),
                options=definition.remote_path_options,
            )
        except (FileNotFoundError, ReservedFileError) as e:
            raise TFWorkerException(e) from e

    def render_templates(self, name: str) -> None:
        """render all the .tf.j2 files in a path, and rename them to .tf"""
        definition = self._app_state.definitions[name]
        template_path = definition.get_target_path(self._app_state.working_dir)
        jinja_env = get_jinja_env(
            template_path=template_path, jinja_globals=self._get_template_vars(name)
        )
        for template_file in jinja_env.list_templates(filter_func=filter_templates):
            write_template_file(jinja_env, template_path, template_file)

    def create_local_vars(self, name: str) -> None:
        """Create local vars from remote data sources"""
        definition = self._app_state.definitions[name]
        log.trace(f"creating local vars for definition {name}")
        with open(
            f"{definition.get_target_path(self._app_state.working_dir)}/{WORKER_LOCALS_FILENAME}",
            "w+",
        ) as tflocals:
            tflocals.write("locals {\n")
            for k, v in definition.get_remote_vars(
                global_vars=self._app_state.loaded_config.global_vars.remote_vars
            ).items():
                tflocals.write(f"  {k} = data.terraform_remote_state.{v}\n")
            tflocals.write("}\n\n")

    def create_worker_tf(self, name: str) -> None:
        """Create remote data sources, and required providers"""
        log.trace(f"creating remote data sources for definition {name}")
        remotes = self._get_remotes(name)
        provider_content = self._get_provider_content(name)
        self._write_worker_tf(name, remotes, provider_content)

    def create_terraform_vars(self, name: str) -> None:
        """Create the variable definitions"""
        definition = self._app_state.definitions[name]
        log.trace(f"creating terraform vars for definition {name}")
        with open(
            f"{definition.get_target_path(self._app_state.working_dir)}/{WORKER_TFVARS_FILENAME}",
            "w+",
        ) as varfile:
            for k, v in definition.get_terraform_vars(
                global_vars=self._app_state.loaded_config.global_vars.terraform_vars
            ).items():
                varfile.write(f"{k} = {vars_typer(v)}\n")

    def create_terraform_lockfile(self, name: str) -> None:
        """Create the terraform lockfile"""
        if (
            self._app_state.providers is None
            or self._app_state.terraform_options.provider_cache is None
        ):
            log.trace(
                f"no providers or provider cache, skipping lockfile creation for {name}"
            )
            return

        definition = self._app_state.definitions[name]
        log.trace(f"creating terraform lockfile for definition {name}")
        result = generate_terraform_lockfile(
            providers=self._app_state.providers,
            included_providers=definition.get_used_providers(
                self._app_state.working_dir
            ),
            cache_dir=self._app_state.terraform_options.provider_cache,
        )

        if result is not None:
            with open(
                f"{definition.get_target_path(self._app_state.working_dir)}/{TF_PROVIDER_DEFAULT_LOCKFILE}",
                "w",
            ) as lockfile:
                lockfile.write(result)

    def _get_provider_content(self, name: str) -> str:
        """Get the provider content"""
        definition = self._app_state.definitions[name]
        provider_names = definition.get_used_providers(self._app_state.working_dir)

        if provider_names is not None:
            return ""
        return self._app_state.providers.required_hcl(provider_names)

    def _get_remotes(self, name: str) -> list:
        """Get the remote data sources"""
        definition = self._app_state.definitions[name]
        log.trace(f"getting remotes for definition {name}")
        if self._app_state.terraform_options.backend_use_all_remotes:
            log.trace(f"using all remotes for definition {name}")
            remotes = self._app_state.backend.remotes
        else:
            remotes = list(
                map(lambda x: x.split(".")[0], definition.remote_vars.values())
            )
            log.trace(f"using remotes {remotes} for definition {name}")
        return remotes

    def _write_worker_tf(self, name: str, remotes: list, provider_content: str) -> None:
        """Write the worker.tf file"""
        definition = self._app_state.definitions[name]

        with open(
            f"{definition.get_target_path(self._app_state.working_dir)}/{WORKER_TF_FILENAME}",
            "w+",
        ) as tffile:
            # Write out the provider configurations for each provider
            tffile.write(
                f"{self._app_state.providers.provider_hcl(includes=definition.get_used_providers(self._app_state.working_dir))}\n\n"
            )
            tffile.write(
                TERRAFORM_TPL.format(
                    # the backend configuration
                    f"{self._app_state.backend.hcl(name)}",
                    # the required providers
                    provider_content,
                )
            )
            tffile.write(self._app_state.backend.data_hcl(remotes))

    def _get_template_vars(self, name: str) -> Dict[str, str]:
        """
        Prepares the vars for rendering in a jinja template

        Creates a dictionary of vars from the following sources:
        - definition vars
        - root_command config-vars
        - OS environment vars
        - root_command template-vars

        Args:
            name (str): the name of the definition

        Returns:
            Dict[str, str]: the template vars
        """
        definition: "Definition" = self._app_state.definitions[name]
        template_vars = definition.get_template_vars(
            self._app_state.loaded_config.global_vars.template_vars
        ).copy()

        for item in self._app_state.root_options.config_var:
            k, v = item.split("=")
            template_vars[k] = v

        return {
            "var": template_vars,
            "env": dict(environ),
        }


def get_coppier(path: str, root_path: str) -> Copier:
    """
    Returns an appropriate copier for the definition path

    Args:
        path (str): the path to the definition
        root_path (str): the root path of the repository

    Returns:
        Copier: the copier to use

    Raises:
        NotImplementedError: if there is no copier to handle the path
    """
    copier = CopyFactory.create(path, root_path=root_path, conflicts=RESERVED_FILES)
    return copier


def copy(
    copier: Copier, destination: str, options: Union[Dict[str, str], None]
) -> None:
    """
    Copy the source to the destination

    Args:
        copier (Copier): the copier to use
        destination (str): the destination to copy to
        options (Dict[str, str]): the options to pass to the copier

    Raises:
        FileNotFoundError: if the source file does not exist
    """
    if options is None:
        options = {}

    try:
        copier.copy(destination=destination, **options)
    except FileNotFoundError:
        raise


def get_jinja_env(
    template_path: str, jinja_globals: Dict[str, str]
) -> jinja2.Environment:
    """
    Get a jinja environment

    Args:
        jinja_globals (Dict[str, str]): the globals to add to the environment

    Returns:
        jinja2.Environment: the jinja environment
    """
    jinja_env = jinja2.Environment(
        undefined=jinja2.StrictUndefined,
        loader=jinja2.FileSystemLoader(template_path),
    )
    jinja_env.globals = jinja_globals
    return jinja_env


def write_template_file(
    jinja_env: jinja2.Environment, template_path: str, template_file: str
) -> None:
    """
    Write a template file to disk

    Args:
        jinja_env (jinja2.Environment): the jinja environment
        template_path (str): the path to the template
        template_file (str): the file to render

    Raises:
        TFWorkerException: if the file already exists, or contains invalid template substitutions
    """
    template_target = f"{template_path}/{'.'.join(template_file.split('.')[:-1])}"

    try:
        f = open(template_target, "x")
        try:
            f.writelines(jinja_env.get_template(template_file).generate())
            log.debug(f"rendered {template_file} into {template_target}")
        except (
            jinja2.exceptions.UndefinedError,
            jinja2.exceptions.TemplateSyntaxError,
        ) as e:
            raise TFWorkerException(
                f"{template_path}/{template_file} could not be rendered: {e}"
            ) from e
    except FileExistsError as e:
        raise TFWorkerException(
            f"{template_target} already exists! Make sure there's not a .tf and .tf.j2 copy of this file"
        ) from e
    finally:
        f.close()


def filter_templates(filename):
    """a small function to filter the list of files down to only j2 templates"""
    return filename.endswith(".tf.j2")


def vars_typer(v, inner=False):
    """
    vars_typer is used to assemble variables as they are parsed from the yaml configuration
    into the required format to be used in terraform
    """
    if v is True:
        return "true"
    elif v is False:
        return "false"
    elif isinstance(v, list):
        rval = []
        for val in v:
            result = Definition.vars_typer(val, inner=True)
            try:
                rval.append(result.strip('"').strip("'"))
            except AttributeError:
                rval.append(result)
        if inner:
            return rval
        else:
            return json.dumps(rval)
    elif isinstance(v, dict):
        rval = {}
        for k, val in v.items():
            result = Definition.vars_typer(val, inner=True)
            try:
                rval[k] = result.strip('"').strip("'")
            except AttributeError:
                rval[k] = result
        if inner:
            return rval
        else:
            return json.dumps(rval)
    return f'"{v}"'
