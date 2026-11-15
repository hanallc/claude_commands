"""Select a target through routing or a fixed target name."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Routed:
    pass


@dataclass(frozen=True)
class Pinned:
    target: str


TargetSelection = Routed | Pinned
ROUTED = Routed()


@dataclass(frozen=True)
class MacFlagGiven:
    """The caller gave --mac."""


@dataclass(frozen=True)
class MacFlagAbsent:
    """The caller did not give --mac."""


MacFlag = MacFlagGiven | MacFlagAbsent
MAC_FLAG_GIVEN = MacFlagGiven()
MAC_FLAG_ABSENT = MacFlagAbsent()
