import os

_CWD = os.getcwd()

DEFAULT_BACKEND_PREFIX = "terraform/state/{deployment}"
DEFAULT_REPOSITORY_PATH = _CWD
DEFAULT_CONFIG = f"{DEFAULT_REPOSITORY_PATH}/worker.yaml"
DEFAULT_AWS_REGION = "us-east-1"

TF_PROVIDER_DEFAULT_HOSTNAME = "registry.terraform.io"
TF_PROVIDER_DEFAULT_NAMESPACE = "hashicorp"
TF_PROVIDER_DEFAULT_LOCKFILE = ".terraform.lock.hcl"

# Items to refact from CLI / Logging output
REDACTED_ITEMS = ["aws_secret_access_key", "aws_session_token"]

WORKER_LOCALS_FILENAME = "worker_generated_locals.tf"
WORKER_TF_FILENAME = "worker_generated_terraform.tf"
# terraform reads *.auto.tfvars.json natively; JSON needs no HCL escaping and
# its strings are never treated as templates
WORKER_TFVARS_FILENAME = "worker_generated.auto.tfvars.json"
# the HCL file written by earlier versions; removed if found and still reserved
LEGACY_WORKER_TFVARS_FILENAME = "worker_generated.auto.tfvars"
RESERVED_FILES = [
    WORKER_LOCALS_FILENAME,
    WORKER_TF_FILENAME,
    WORKER_TFVARS_FILENAME,
    LEGACY_WORKER_TFVARS_FILENAME,
]
