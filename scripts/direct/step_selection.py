"""Select a target through routing or a fixed target name."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Routed:
    """The step uses the normal target routing rules."""


@dataclass(frozen=True)
class Pinned:
    """The step must use this target."""
    target: str


TargetSelection = Routed | Pinned
ROUTED = Routed()
