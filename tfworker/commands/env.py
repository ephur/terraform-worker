import click

from tfworker.authenticators import AuthenticatorsCollection


class EnvCommand:
    """
    The env command translates the environment configuration that is used for the worker
    into an output that can be `eval`'d by a shell. This will allow one to maintain the
    same authentication options that the worker will use when running terraform when
    executing commands against the rendered terraform definitions such as `terraform import`
    """

    def __init__(self, rootc, **kwargs):
        # parse the configuration
        rootc.load_config()

        # initialize any authenticators
        self._authenticators = AuthenticatorsCollection(rootc.args, deployment=None)

    def exec(self):
        for auth in self._authenticators:
            for k, v in auth.env().items():
                click.secho(f"export {k}={v}")
