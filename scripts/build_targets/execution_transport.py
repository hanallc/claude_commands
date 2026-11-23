"""Name the transport selected by a configured remote target."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SshCommandTransport:
    settings: Path


@dataclass(frozen=True)
class HanaControlTransport:
    settings: Path
    host: str


@dataclass(frozen=True)
class HanaGpuNextestTransport:
    settings: Path
    host: str


ExecutionTransport = SshCommandTransport | HanaControlTransport | HanaGpuNextestTransport
