class UnknownHandler(Exception):
    """
    This is an excpetion that indicates configuration was attempted for a handler that is not supported.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def __str__(self) -> str:
        return f"Unknown handler: {self.provider}"


class HandlerError(Exception):
    """
    This is an exception that indicates an error occurred while attempting to execute a handler.
    """

    def __init__(self, message: str, terminate: bool = True) -> None:
        self.message = message
        self.terminate = terminate

    def __str__(self) -> str:
        return f"Handler error: {self.message}"
