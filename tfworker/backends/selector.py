from typing import TYPE_CHECKING

import tfworker.util.log as log
from tfworker.exceptions import BackendError

from .backends import Backends  # noqa
from .gcs import GCSBackend  # noqa
from .s3 import S3Backend  # noqa

if TYPE_CHECKING:
    from tfworker.authenticators import AuthenticatorsCollection


def select_backend(
    backend: Backends, deployment: str, authenticators: "AuthenticatorsCollection"
):
    if backend == Backends.S3:
        log.trace("selected S3 backend")
        return S3Backend(authenticators, deployment=deployment)
    elif backend == Backends.GCS:
        log.trace("selected GCS backend")
        return GCSBackend(authenticators, deployment=deployment)
    else:
        raise BackendError(f"Unsupported backend: {backend}")
