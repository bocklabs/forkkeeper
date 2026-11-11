from dataclasses import dataclass


@dataclass(frozen=True)
class Fork:
    full_name: str
    base: str
    upstream: str
    upstream_branch: str


@dataclass(frozen=True)
class Installation:
    id: int
    account: str
    workflows: bool = False
