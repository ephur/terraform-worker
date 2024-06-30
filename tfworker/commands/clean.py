from tfworker.commands.base import BaseCommand


class CleanCommand(BaseCommand):
    def __init__(self, rootc, **kwargs):
        # super(CleanCommand, self).__init__(rootc, **kwargs)
        # self._deployment = self._resolve_arg("deployment")
        # self._limit = self._resolve_arg("limit")
        pass

    def exec(self):
        # try:
        #     self._backend.clean(deployment=self._deployment, limit=self._limit)
        # except BackendError as e:
        #     click.secho(f"error while cleaning: {e}", fg="red")
        #     raise SystemExit(1)
        # click.secho("backend cleaning completed", fg="green")
        pass
