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


class PluginSourceParseException(Exception):
    """
    Exception is raised when a plugin source cannot be parsed.
    """

    pass


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
