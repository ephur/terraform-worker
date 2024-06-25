from abc import ABCMeta, abstractmethod

from tfworker.types.json import JSONType


class BaseBackend(metaclass=ABCMeta):
    plan_storage = False
    tag = "base"

    @abstractmethod
    def hcl(self, name: str) -> str:
        pass

    @abstractmethod
    def data_hcl(self, exclude: list) -> str:
        pass

    @abstractmethod
    def clean(self, deployment: str, limit: tuple) -> str:
        pass

    @abstractmethod
    def remotes(self) -> list:
        pass

    @property
    def handlers(self) -> dict:
        return {}


class Backends:
    s3 = "s3"
    gcs = "gcs"


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
