class TFWorkerException(Exception):
    pass


class MissingDependencyException(Exception):
    pass


class BackendError(Exception):
    # add custom "help" parameter to the exception
    def __init__(self, message, help=None):
        super().__init__(message)
        self._help = help

    @property
    def help(self):
        if self._help is None:
            return "No help available"
        return self._help


class HookError(Exception):
    """
    Exception is raised when a hook fails, or has execution issues.
    """

    pass


class PlanChange(Exception):
    """
    Exception is raised when a terraform plan has changes.
    """

    pass


# class PluginSourceParseException(Exception):
#     """
#     Exception is raised when a plugin source cannot be parsed.
#     """

#     pass


class UnknownProvider(Exception):
    def __init__(self, provider):
        super().__init__(f"{provider} is not a known value.")


class ReservedFileError(Exception):
    """
    Exception is raised when a reserved file is found in the repository;

    Reserved files are files that are used by tfworker, and should not be
    present in the repository.
    """

    pass


class TerraformError(Exception):
    """
    Exception is raised when a terraform command fails.
    """

    pass
