import click

from tfworker.authenticators.collection import AuthenticatorsCollection
from tfworker.commands.base import BaseCommand


class EnvCommand(BaseCommand):
    """
    The env command translates the environment configuration that is used for the worker
    into an output that can be `eval`'d by a shell. This will allow one to maintain the
    same authentication options that the worker will use when running terraform when
    executing commands against the rendered terraform definitions such as `terraform import`
    """

    def __init__(self, rootc, **kwargs):
        # Initialize the base command
        self._rootc = rootc
        self._args_dict = dict(kwargs)
        self._args_dict.update(self._rootc.args.__dict__)

        # parse the configuration
        rootc.add_arg("deployment", "env")
        rootc.load_config()

        # initialize any authenticators
        self._authenticators = AuthenticatorsCollection(rootc.args, deployment=None)

    def exec(self):
        for auth in self._authenticators:
            for k, v in auth.env().items():
                click.secho(f"export {k}={v}")
