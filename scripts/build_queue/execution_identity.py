"""Identify assigned source and report execution evidence."""
from dataclasses import dataclass
from typing import Literal
import uuid


@dataclass(frozen=True)
class SourceKeyKnown:
    """The key identifies the source tree for placement."""
    key: str


@dataclass(frozen=True)
class SourceKeyUnavailable:
    """The reason explains why the source tree key is unavailable."""
    reason: str


SourceIdentity = SourceKeyKnown | SourceKeyUnavailable


@dataclass(frozen=True)
class ProcessExecution:
    """The execution identity holds a host, generation, process identifier, and start time."""
    host: str
    generation: str
    pid: int
    start_time: str


@dataclass(frozen=True)
class CloudExecution:
    """The cloud execution identity holds a target, generation, instance, and attempt."""
    target: str
    generation: str
    instance: str
    attempt: str


@dataclass(frozen=True)
class GuestExecution:
    """The guest start report names the assigned attempt and its execution folder."""
    target: Literal["mac-linux"]
    attempt: str
    folder: str
    home: str

    def __post_init__(self) -> None:
        if any(not path.startswith("/") or "\0" in path for path in (self.folder, self.home)):
            raise ValueError("The guest start report needs absolute folder and home paths.")


ExecutionIdentity = ProcessExecution | CloudExecution | GuestExecution


@dataclass(frozen=True)
class ExecutionNotStarted:
    """Execution has not started."""


@dataclass(frozen=True)
class ObservedExecution:
    """The evidence identifies an observed execution."""
    identity: ExecutionIdentity


@dataclass(frozen=True)
class ExecutionEvidenceUnavailable:
    """The reason and time describe unavailable execution evidence."""
    reason: str
    observed_at: float


ExecutionEvidence = ExecutionNotStarted | ObservedExecution | ExecutionEvidenceUnavailable


@dataclass(frozen=True)
class HanaCommandAdmissionRefused:
    """The Hana command admission check refused the command."""
    reason: str


def attempt_key(episode: str, attempt: str) -> str:
    """Length framing keeps different episode and attempt pairs distinct."""
    return f"{len(episode)}:{episode}{attempt}"


def assigned_claim_id(episode: str, attempt: str) -> str:
    """The provider claim id of one assigned attempt."""
    return uuid.uuid5(uuid.NAMESPACE_URL, attempt_key(episode, attempt)).hex
