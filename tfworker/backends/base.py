from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING, Optional, Tuple

from tfworker.custom_types import JSONType
from tfworker.exceptions import BackendError

from .lock import LockInfo, WorkerLease

if TYPE_CHECKING:
    from tfworker.authenticators import (  # pragma: no cover  # noqa
        AuthenticatorsCollection,
    )
    from tfworker.report import BackendLocation  # pragma: no cover  # noqa


class BaseBackend(metaclass=ABCMeta):
    """
    The base backend is an abrastract class that defines the interface for backends

    A backend provides the mechanisms and functions for interacting with a Terraform backend,

    Attributes:
        plan_storage (bool): A flag to indicate whether the backend supports plan storage
        lock_inspection (bool): A flag to indicate whether the backend can read state
            locks and keep worker leases (get_lock, get_lease, put_lease, delete_lease)
        auth_tag(str): The tag of the authenticator that is required for this backend
        tag (str): A unique identifier for the backend
    """

    auth_tag: str
    tag: str
    plan_storage: bool = False
    lock_inspection: bool = False
    report_storage: bool = False

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
        pass  # pragma: no cover

    @abstractmethod
    def hcl(self, deployment: str) -> str:
        """
        The HCL method returns the configuration that belongs in the "terraform" configuration block

        Args:
            deployment (str): The deployment name

        Returns:
            str: The HCL configuration
        """
        pass  # pragma: no cover

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
        pass  # pragma: no cover

    @abstractmethod
    def clean(self, deployment: str, limit: tuple) -> None:
        """
        Clean is called to remove any resources that are no longer needed

        Args:
            deployment (str): The deployment name
            limit (tuple): A tuple with a list of resources to limit execution to
        """
        pass  # pragma: no cover

    @property
    @abstractmethod
    def remotes(self) -> list:
        """
        Remotes returns a list of the remote data sources that may be configured for a deployment

        Returns:
            list: A list of remote data sources
        """
        pass  # pragma: no cover

    @abstractmethod
    def get_state(self, remote: str) -> JSONType:
        """
        Get the state file from the backend, for a remote.

        Args:
            deployment (str): The deployment name

        Returns:
            JSONType: The state from the backend
        """
        pass

    def get_lock(self, definition: str) -> Optional[LockInfo]:
        """
        Return the lock terraform holds on a definition's state, or None.

        Raises:
            BackendError: if a lock exists but cannot be read
        """
        raise NotImplementedError

    def get_lease(self, definition: str, host: str) -> Optional[WorkerLease]:
        """Return the lease a worker on `host` keeps beside a definition's state, or None."""
        raise NotImplementedError

    def put_lease(self, lease: WorkerLease) -> None:
        """Write or refresh this worker's lease for `lease.definition`."""
        raise NotImplementedError

    def location(self, definition: str) -> "BackendLocation":
        """Return where a definition's state lives."""
        raise NotImplementedError

    def state_version(self, definition: str) -> Optional[Tuple[str, int]]:
        """Return the lineage and serial of a definition's state, or None if it has none."""
        raise NotImplementedError

    def read_report(self, path: str) -> Optional[Tuple[str, str]]:
        """
        Return the text and version tag of a report object stored beside the
        state under `_tfworker/<path>`, or None if it does not exist.
        """
        raise NotImplementedError

    def write_report(
        self,
        path: str,
        body: str,
        if_match: Optional[str] = None,
        create_only: bool = False,
    ) -> None:
        """
        Write a report object under `_tfworker/<path>`.

        Args:
            if_match: only replace the object if it still has this version tag
            create_only: only write if the object does not exist yet

        Raises:
            ReportConflictError: when a condition did not hold
        """
        raise NotImplementedError

    def delete_lease(self, definition: str, host: str) -> None:
        """Remove the lease a worker on `host` keeps beside a definition's state, if any."""
        raise NotImplementedError


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
        raise BackendError("resources key does not exist in state")
    except TypeError:
        raise BackendError("state is not valid JSON")
