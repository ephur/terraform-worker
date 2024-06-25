import os

_CWD = os.getcwd()

DEFAULT_BACKEND_PREFIX = "terraform/state/{deployment}"
DEFAULT_CONFIG = f"{_CWD}/worker.yaml"
DEFAULT_REPOSITORY_PATH = _CWD
DEFAULT_AWS_REGION = "us-east-1"
DEFAULT_GCP_REGION = "us-east-1a"

TF_PROVIDER_DEFAULT_HOSTNAME = "registry.terraform.io"
TF_PROVIDER_DEFAULT_NAMESPACE = "hashicorp"
TF_PROVIDER_DEFAULT_LOCKFILE = ".terraform.lock.hcl"

# Items to refact from CLI / Logging output
REDACTED_ITEMS = ["aws_secret_access_key", "aws_session_token"]

# A map of supported backends, to the authenticators which they require
SUPPORTED_BACKENDS = {"s3": "aws", "gcs": "google"}

TF_STATE_CACHE_NAME = "worker_state_cache.json"
WORKER_LOCALS_FILENAME = "worker_generated_locals.tf"
WORKER_TF_FILENAME = "worker_generated_terraform.tf"
WORKER_TFVARS_FILENAME = "worker_generated.auto.tfvars"
RESERVED_FILES = [
    WORKER_LOCALS_FILENAME,
    WORKER_TF_FILENAME,
    WORKER_TFVARS_FILENAME,
    TF_STATE_CACHE_NAME,
]
