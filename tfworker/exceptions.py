class TFWorkerException(Exception):
    """
    All exceptions raised by tfworker should inherit from this class.
    """

    pass


class MissingDependencyException(TFWorkerException):
    pass


class BackendError(TFWorkerException):
    # add custom "help" parameter to the exception
    def __init__(self, message, help=None):
        super().__init__(message)
        self._help = help

    @property
    def help(self):
        if self._help is None:
            return "No help available"
        return self._help


class HookError(TFWorkerException):
    """
    Exception is raised when a hook fails, or has execution issues.
    """

    pass


class PlanChange(TFWorkerException):
    """
    Exception is raised when a terraform plan has changes.
    """

    pass


# class PluginSourceParseException(Exception):
#     """
#     Exception is raised when a plugin source cannot be parsed.
#     """

#     pass


class UnknownProvider(TFWorkerException):
    def __init__(self, provider):
        super().__init__(f"{provider} is not a known value.")


class ReservedFileError(TFWorkerException):
    """
    Exception is raised when a reserved file is found in the repository;

    Reserved files are files that are used by tfworker, and should not be
    present in the repository.
    """

    pass


class TerraformError(TFWorkerException):
    """
    Exception is raised when a terraform command fails.
    """

    pass


class UnknownHandler(TFWorkerException):
    """
    This is an excpetion that indicates configuration was attempted for a handler that is not supported.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def __str__(self) -> str:
        return f"Unknown handler: {self.provider}"


class HandlerError(TFWorkerException):
    """
    This is an exception that indicates an error occurred while attempting to execute a handler.
    """

    def __init__(self, message: str, terminate: bool = True) -> None:
        self.message = message
        self.terminate = terminate

    def __str__(self) -> str:
        return f"Handler error: {self.message}"


class UnknownAuthenticator(Exception):
    def __init__(self, provider):
        super().__init__(f"{provider} is not a known authenticator.")
