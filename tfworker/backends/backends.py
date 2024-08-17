from enum import Enum
from typing import List

from tfworker.backends.gcs import GCSBackend
from tfworker.backends.s3 import S3Backend


class Backends(Enum):
    S3 = S3Backend
    GCS = GCSBackend

    @classmethod
    def names(cls) -> List[str]:
        """
        List of the names of the available backends

        Returns:
            List[str]: List of the names of the available back
        """
        return [i.name for i in cls]
