from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING

from tfworker.exceptions import BackendError
from tfworker.types import JSONType

if TYPE_CHECKING:
    from tfworker.authenticators import (  # pragma: no cover  # noqa
        AuthenticatorsCollection,
    )


class BaseBackend(metaclass=ABCMeta):
    """
    The base backend is an abrastract class that defines the interface for backends

    A backend provides the mechanisms and functions for interacting with a Terraform backend,

    Attributes:
        plan_storage (bool): A flag to indicate whether the backend supports plan storage
        auth_tag(str): The tag of the authenticator that is required for this backend
        tag (str): A unique identifier for the backend
    """

    auth_tag: str
    tag: str
    plan_storage: bool = False

    @abstractmethod
    def __init__(
        self, authenticators: "AuthenticatorsCollection", deployment: str = None
    ):
        """
        The __init__ method initializes the backend

        Args:
            authenticators (AuthenticatorsCollection): The collection of authenticators
            deployment (str): The deployment name

        Raises:
            BackendError: If there is an error during initialization
        """
        ...

    @abstractmethod
    def hcl(self, deployment: str) -> str:
        """
        The HCL method returns the configuration that belongs in the "terraform" configuration block

        Args:
            deployment (str): The deployment name

        Returns:
            str: The HCL configuration
        """
        pass

    @abstractmethod
    def data_hcl(self, remotes: list) -> str:
        """
        The data_hcl method returns the configuration that is used to configure this backend as a remote
        data source.

        Args:
            remotes (list): A list of remote sources to provide a configuration for

        Returns:
            str: The HCL configuration for the remote data source
        """
        pass

    @abstractmethod
    def clean(self, deployment: str, limit: tuple) -> None:
        """
        Clean is called to remove any resources that are no longer needed

        Args:
            deployment (str): The deployment name
            limit (tuple): A tuple with a list of resources to limit execution to
        """
        pass

    @property
    @abstractmethod
    def remotes(self) -> list:
        """
        Remotes returns a list of the remote data sources that may be configured for a deployment

        Returns:
            list: A list of remote data sources
        """
        pass


def validate_backend_empty(state: JSONType) -> bool:
    """
    validate_backend_empty ensures that the provided state file
    is empty
    """

    try:
        if len(state["resources"]) > 0:
            return False
        else:
            return True
    except KeyError:
        raise BackendError("resources key does not exist in state!")
