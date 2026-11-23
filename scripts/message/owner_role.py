"""The role of the owner for whom a session runs a step."""
from enum import StrEnum


class OwnerRole(StrEnum):
    """The role of the owner for whom a session runs a step."""

    UNKNOWN = "unknown"
    SHOWRUNNER = "showrunner"
    UNIT = "unit"
