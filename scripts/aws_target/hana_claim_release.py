"""Name the outcome of an exact Hana claim release."""
from dataclasses import dataclass


@dataclass(frozen=True)
class HanaClaimReleased:
    """The exact Hana claim is free."""


@dataclass(frozen=True)
class HanaClaimReleaseRefused:
    reason: str


HanaClaimRelease = HanaClaimReleased | HanaClaimReleaseRefused
