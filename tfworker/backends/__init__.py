# from .base import BackendError, Backends, BaseBackend  # noqa
# from .gcs import GCSBackend  # noqa
# from .s3 import S3Backend  # noqa


# def select_backend(backend, deployment, authenticators, definitions):
#     if backend == Backends.s3:
#         return S3Backend(authenticators, definitions, deployment=deployment)
#     elif backend == Backends.gcs:
#         return GCSBackend(authenticators, definitions, deployment=deployment)
