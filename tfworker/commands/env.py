import click

from .base import BaseCommand


class EnvCommand(BaseCommand):
    """
    The env command translates the environment configuration that is used for the worker
    into an output that can be `eval`'d by a shell. This will allow one to maintain the
    same authentication options that the worker will use when running terraform when
    executing commands against the rendered terraform definitions such as `terraform import`
    """

    def exec(self):
        for auth in self.app_state.authenticators:
            for k, v in auth.env().items():
                click.secho(f"export {k}={v}")
