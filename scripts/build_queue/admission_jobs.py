"""Job records shared by command readers and the admission wire."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StepJob:
    """The job that the command runs.

    For integrate, unit names the checkpoint's unit. Other jobs test the caller's unit.
    """
    step: str
    unit: str = ""
    branch: str = ""
    modules: int = 0


@dataclass(frozen=True)
class GateJobNotRecorded:
    """The step has no recorded gate job."""


GATE_JOB_NOT_RECORDED = GateJobNotRecorded()

# A hand command that passed its model check (scripts/production/hand_made.py).
HAND_MADE = StepJob("hand-made")


VerifyVerb = Literal["check", "test", "lint", "doc", "example", "build", "final"]
VERIFY_VERBS: tuple[VerifyVerb, ...] = ("check", "test", "lint", "doc", "example", "build", "final")


@dataclass(frozen=True)
class VerifyCall:
    """The verify call identifies one step in its plan."""
    verb: VerifyVerb
    call: str
    position: int
    total: int


@dataclass(frozen=True)
class NotVerifyCall:
    """This step does not belong to a verify call."""


VerifyStep = VerifyCall | NotVerifyCall
