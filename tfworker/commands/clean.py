import click

from tfworker.commands.base import BaseCommand
from tfworker.exceptions import BackendError


class CleanCommand(BaseCommand):
    """
    The CleanCommand class is called by the top level CLI
    as part of the `clean` sub-command. It inherits from
    BaseCommand which sets up the application state.

    This command cleans the backend by removing empty state files
    and optionally DynamoDB lock tables for a deployment.
    """

    def exec(self):
        """
        Execute the clean command to remove backend state and locks
        """
        deployment = self._app_state.deployment
        limit = self._app_state.clean_options.limit

        # Convert limit list to tuple if provided (backend expects tuple)
        limit_tuple = tuple(limit) if limit else None

        try:
            self._app_state.backend.clean(deployment=deployment, limit=limit_tuple)
        except BackendError as e:
            click.secho(f"error while cleaning: {e}", fg="red")
            raise SystemExit(1)

        click.secho("backend cleaning completed", fg="green")
