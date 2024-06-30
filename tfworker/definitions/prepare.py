from pathlib import Path
from typing import TYPE_CHECKING, Dict, Union

from tfworker.util.copier.factory import CopyFactory
from tfworker.constants import RESERVED_FILES
from tfworker.exceptions import ReservedFileError, TFWorkerException
import tfworker.util.log as log

# hack work around to force the copiers to be registered
# @TODO: find a better way to do this
from tfworker.util.copier.fs_copier import FileSystemCopier  # noqa
from tfworker.util.copier.git_copier import GitCopier  # noqa

if TYPE_CHECKING:
    from tfworker.types.definition import Definition
    from tfworker.backends.base import BaseBackend
    from tfworker.util.copier import Copier

def prepare(name:str, definition: "Definition", backend: "BaseBackend", working_path:str, repo_path: str) -> None:
    """
    Prepare is an orchestration function that prepares a definition to be ready for terraform init
    """
    log.trace(f"fetching copier for definition {name} with path {definition.path} and repo_path {repo_path}")
    try:
        c = _get_coppier(definition.path, repo_path)
    except NotImplementedError as e:
        raise TFWorkerException(f"could not handle source path {definition.path} for definition {name}, either file does not exist or could not handle remote URI") from e

    log.trace(f"putting definition {name} in {definition.get_target_path(working_path)} with copier {c.__class__.__name__}")
    try:
        _copy(copier=c, destination=definition.get_target_path(working_path), options=definition.remote_path_options)
    except FileNotFoundError as e:
        raise TFWorkerException(e) from e


    # get a copier // done
    # copy the base files into place // done
    # render the templates / how to deal with the template callback?
    # create local vars from remote data sources
    # create remote data sources
    # create .tfvars file
    # create providers
    # create the provider lockfile
    # ^^ it seems this method will require the app state, is it best here,
    #    or in the TerraformCommand? It seems we should not require app state
    #    here... we need access to much of the state to do it outside of
    #    a command


def _get_coppier(path: str, root_path: str) -> "Copier":
    """
    Returns an appropriate copier for the definition path
    """
    copier = CopyFactory.create(
        path,
        root_path=root_path,
        conflicts=RESERVED_FILES
    )
    return copier

def _copy(copier: "Copier", destination: str, options: Union[Dict[str, str], None]) -> None:
    """
    Copy the source to the destination
    """
    if options is None:
        options = {}

    try:
        copier.copy(destination=destination, **options)
    except FileNotFoundError:
        raise

#     def prep(self, backend):
#         """prepare the definitions for running"""

#         # prep the definitions
#         try:
#             c = CopyFactory.create(
#                 self.path,
#                 root_path=self._repository_path,
#                 conflicts=const.RESERVED_FILES,
#             )
#         except NotImplementedError:
#             click.secho(
#                 f"could not handle source path {self.path} for definition {self.tag}, either file does not exist or could not handle remote URI",
#                 fg="red",
#             )
#             raise SystemExit(1)

#         remote_options = dict(self.body.get("remote_path_options", {}))

#         try:
#             c.copy(destination=self._target, **remote_options)
#         except FileNotFoundError as e:
#             if remote_options.get("sub_path", False):
#                 click.secho(
#                     f"could not find sub_path {remote_options['sub_path']} for definition {self.tag}",
#                     fg="red",
#                 )
#                 raise SystemExit(1)
#             else:
#                 raise e
#         except FileExistsError as e:
#             raise ReservedFileError(e)
#         except RuntimeError as e:
#             click.secho(
#                 f"could not copy source path {self.path} for definition {self.tag}, error details:\n\n{e}",
#                 fg="red",
#             )
#             raise SystemExit(1)

#         # render the templates
#         if self._template_callback is not None:
#             self._template_callback(self._target, template_vars=self._template_vars)

#         # Create local vars from remote data sources
#         if len(list(self._remote_vars.keys())) > 0:
#             with open(f"{self._target}/{WORKER_LOCALS_FILENAME}", "w+") as tflocals:
#                 tflocals.write("locals {\n")
#                 for k, v in self._remote_vars.items():
#                     tflocals.write(f"  {k} = data.terraform_remote_state.{v}\n")
#                 tflocals.write("}\n\n")

#         # create remote data sources, and required providers
#         if self._use_backend_remotes:
#             remotes = backend.remotes()
#         else:
#             remotes = list(map(lambda x: x.split(".")[0], self._remote_vars.values()))

#         required_providers_content = (
#             ""
#             if self.provider_names is not None
#             else self._providers.required_hcl(self.provider_names)
#         )

#         with open(f"{self._target}/{WORKER_TF_FILENAME}", "w+") as tffile:
#             tffile.write(f"{self._providers.provider_hcl(self.provider_names)}\n\n")
#             tffile.write(
#                 TERRAFORM_TPL.format(
#                     f"{backend.hcl(self.tag)}",
#                     required_providers_content,
#                 )
#             )
#             tffile.write(backend.data_hcl(remotes))

#         # Create the variable definitions
#         with open(f"{self._target}/{WORKER_TFVARS_FILENAME}", "w+") as varfile:
#             for k, v in self._terraform_vars.items():
#                 varfile.write(f"{k} = {self.vars_typer(v)}\n")

#         self._prep_terraform_lockfile()

#     def _prep_terraform_lockfile(self):
#         """
#         Write a terraform lockfile in the definition directory
#         """
#         if self._provider_cache is None:
#             return

#         result = generate_terraform_lockfile(
#             providers=self._providers,
#             included_providers=self.provider_names,
#             cache_dir=self._provider_cache,
#         )

#         if result is not None:
#             with open(
#                 f"{self._target}/{TF_PROVIDER_DEFAULT_LOCKFILE}", "w"
#             ) as lockfile:
#                 lockfile.write(result)
