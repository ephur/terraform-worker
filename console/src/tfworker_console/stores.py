"""
Backend stores the console reads, and how it reaches them.

A store is one bucket and a prefix template, such as
``terraform/state/{deployment}``, with the credentials to read it. Stores are
listed in a YAML file:

.. code-block:: yaml

    stores:
      - name: prod
        region: us-east-1
        bucket: prod-terraform-state
        prefix: terraform/state/{deployment}
        credentials:
          role_arn: arn:aws:iam::123456789012:role/tfworker-console-read
      - name: dev
        region: us-west-2
        bucket: dev-terraform-state
        prefix: terraform/state/{deployment}
        credentials:
          profile: dev
"""

from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import boto3
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLACEHOLDER = "{deployment}"


class Credentials(BaseModel):
    """An assumed role, a named profile, or the ambient credentials when both are unset."""

    model_config = ConfigDict(extra="forbid")

    role_arn: Optional[str] = None
    external_id: Optional[str] = None
    session_name: str = "tfworker-console"
    profile: Optional[str] = None

    @model_validator(mode="after")
    def _one_source(self) -> "Credentials":
        if self.role_arn and self.profile:
            raise ValueError("set role_arn or profile, not both")
        if self.external_id and not self.role_arn:
            raise ValueError("external_id needs role_arn")
        return self

    @property
    def kind(self) -> str:
        if self.role_arn:
            return "role"
        if self.profile:
            return "profile"
        return "ambient"


class StoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    region: str
    bucket: str
    prefix: str = Field(description="The backend prefix, with {deployment} in it")
    credentials: Credentials = Field(default_factory=Credentials)

    @field_validator("prefix")
    @classmethod
    def _template(cls, value: str) -> str:
        value = value.strip("/")
        if value.count(PLACEHOLDER) != 1:
            raise ValueError(f"must contain {PLACEHOLDER} exactly once")
        head, tail = value.split(PLACEHOLDER)
        if head and not head.endswith("/") or tail and not tail.startswith("/"):
            raise ValueError(f"{PLACEHOLDER} must be a whole path segment")
        return value

    @property
    def listing_prefix(self) -> str:
        """The part of the template before the deployment segment."""
        return self.prefix.split(PLACEHOLDER)[0]

    def render(self, deployment: str) -> str:
        return self.prefix.replace(PLACEHOLDER, deployment)

    def session(self) -> boto3.Session:
        """A boto3 session with this store's credentials."""
        creds = self.credentials
        if creds.profile:
            return boto3.Session(profile_name=creds.profile, region_name=self.region)
        if creds.role_arn:
            params = {"RoleArn": creds.role_arn, "RoleSessionName": creds.session_name}
            if creds.external_id:
                params["ExternalId"] = creds.external_id
            sts = boto3.client("sts", region_name=self.region)
            assumed = sts.assume_role(**params)["Credentials"]
            return boto3.Session(
                aws_access_key_id=assumed["AccessKeyId"],
                aws_secret_access_key=assumed["SecretAccessKey"],
                aws_session_token=assumed["SessionToken"],
                region_name=self.region,
            )
        return boto3.Session(region_name=self.region)


class StoresFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stores: List[StoreConfig] = Field(default_factory=list)

    @field_validator("stores")
    @classmethod
    def _unique(cls, stores: List[StoreConfig]) -> List[StoreConfig]:
        names = [store.name for store in stores]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate store names: {', '.join(duplicates)}")
        return stores


def load_stores(path: Optional[str]) -> List[StoreConfig]:
    if not path:
        return []
    data = yaml.safe_load(Path(path).read_text()) or {}
    return StoresFile.model_validate(data).stores


def list_keys(client, bucket: str, prefix: str) -> Iterator[Tuple[str, str]]:
    """Yield (key, etag) for every object under a prefix."""
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            yield item["Key"], item["ETag"].strip('"')


def list_segments(client, bucket: str, prefix: str) -> Iterator[str]:
    """Yield the next path segment of every key under a prefix."""
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for item in page.get("CommonPrefixes", []):
            segment = item["Prefix"][len(prefix) :].rstrip("/")
            if segment:
                yield segment
