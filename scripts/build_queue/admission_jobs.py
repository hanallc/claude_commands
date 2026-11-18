"""Job records shared by command readers and the admission wire."""

from dataclasses import dataclass


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
