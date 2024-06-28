import tfworker.util.log as log

from .base import Backends, BaseBackend  # noqa
from .gcs import GCSBackend  # noqa
from .s3 import S3Backend  # noqa


def select_backend(backend, deployment, authenticators, definitions):
    if backend == Backends.s3:
        log.trace("selected S3 backend")
        return S3Backend(authenticators, definitions, deployment=deployment)
    elif backend == Backends.gcs:
        log.trace("selected GCS backend")
        return GCSBackend(authenticators, definitions, deployment=deployment)
