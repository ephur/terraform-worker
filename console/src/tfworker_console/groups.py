"""
Deployment groups, such as Production, Non-prod and Sandbox.

Groups live in a YAML file named by ``TFWORKER_CONSOLE_GROUPS_FILE``, beside
the stores file, so they survive a rebuild of the projection:

.. code-block:: yaml

    groups:                 # listed in fleet sort order
      - name: Production
        plan_ttl: 1d        # pending saved plans older than this are flagged
        force_unlock: admins
      - name: Sandbox
        force_unlock: operators
    rules:                  # first match wins
      - match: "prod-*"     # a case-sensitive glob (*, ?, [...])
        group: Production
      - store: dev          # optionally only in one store
        match: "*"
        group: Sandbox
    deployments:            # by hand; wins over every rule
      prod/legacy-app: Production

A deployment belongs to exactly one group. One that no rule or assignment
names is in Unassigned, which comes last and has the default policy.
"""

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Dict, Iterable, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from tfworker.util.duration import parse_duration

UNASSIGNED = "Unassigned"


GLOB = set("*?[")


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # seconds; a pending saved plan older than this is flagged for cleanup
    plan_ttl: Optional[float] = None
    # who may force-unlock a state in the group (enforced with roles)
    force_unlock: Literal["operators", "admins"] = "admins"

    @field_validator("plan_ttl", mode="before")
    @classmethod
    def _duration(cls, value):
        if value is None:
            return None
        seconds = parse_duration(value)
        if seconds <= 0:
            raise ValueError("plan_ttl must be longer than zero")
        return seconds


class Group(Policy):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("group names cannot start or end with spaces")
        return value


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # a glob (fnmatch: *, ? and [...]) on the deployment, case-sensitive
    match: str = Field(min_length=1)
    store: Optional[str] = None
    group: str

    @field_validator("match")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("patterns cannot start or end with spaces")
        return value

    def matches(self, store: str, deployment: str) -> bool:
        return (self.store is None or self.store == store) and fnmatchcase(
            deployment, self.match
        )


class GroupsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: List[Group] = Field(default_factory=list)
    rules: List[Rule] = Field(default_factory=list)
    # "store/deployment": group
    deployments: Dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> "GroupsFile":
        names = [group.name for group in self.groups]
        folded = [name.casefold() for name in names]
        if UNASSIGNED.casefold() in folded:
            raise ValueError(f"{UNASSIGNED} is built in and cannot be defined")
        duplicates = sorted({n for n in names if folded.count(n.casefold()) > 1})
        if duplicates:
            raise ValueError(
                f"duplicate groups (names ignore case): {', '.join(duplicates)}"
            )
        known = set(names)
        for rule in self.rules:
            if rule.group not in known:
                raise ValueError(
                    f"rule {rule.match!r} names unknown group {rule.group}"
                )
        for key, group in self.deployments.items():
            if key.count("/") != 1 or not all(key.split("/")):
                raise ValueError(f"{key!r} must be store/deployment")
            if GLOB & set(key):
                raise ValueError(f"{key!r} is a name, not a pattern; use a rule")
            if group not in known:
                raise ValueError(f"{key} names unknown group {group}")
        return self

    def stores(self) -> set:
        """Every store a rule or assignment names."""
        named = {rule.store for rule in self.rules if rule.store}
        return named | {key.split("/")[0] for key in self.deployments}


class Groups:
    """Resolves deployments to groups, and groups to policy and sort order."""

    def __init__(self, config: Optional[GroupsFile] = None):
        self.config = config or GroupsFile()
        self._by_name = {group.name: group for group in self.config.groups}
        self._unassigned = Group(name=UNASSIGNED)

    @property
    def names(self) -> List[str]:
        """Every group in sort order, Unassigned last."""
        return [group.name for group in self.config.groups] + [UNASSIGNED]

    def rank(self, name: str) -> int:
        names = self.names
        return names.index(name) if name in names else len(names)

    def of(self, store: str, deployment: str) -> str:
        manual = self.config.deployments.get(f"{store}/{deployment}")
        if manual:
            return manual
        for rule in self.config.rules:
            if rule.matches(store, deployment):
                return rule.group
        return UNASSIGNED

    def group(self, name: str) -> Group:
        return self._by_name.get(name, self._unassigned)

    def policy(self, store: str, deployment: str) -> Group:
        return self.group(self.of(store, deployment))

    def assign(self, session) -> None:
        """Store every deployment's group, so scopes can filter in SQL."""
        from sqlalchemy import select

        from . import db

        for row in session.scalars(select(db.Deployment)):
            row.group_name = self.of(row.store, row.name)
        session.commit()

    def unknown_stores(self, configured: Iterable[str]) -> List[str]:
        return sorted(self.config.stores() - set(configured))


def load_groups(path: Optional[str]) -> Groups:
    if not path:
        return Groups()
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Groups(GroupsFile.model_validate(data))
