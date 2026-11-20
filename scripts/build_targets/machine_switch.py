"""Read the switch for placed work on one machine."""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from pathlib import Path

from scripts.lib import conf


class Machine(Enum):
    MAC = "mac"
    AWS_GATE = "aws-gate"
    HANA_AWS = "hana-aws"
    HANA_GPU = "hana-gpu"


class MachineSwitch(Enum):
    ON = "on"
    OFF = "off"


class MachineSwitchError(conf.Unusable):
    """The machine switch has no valid value."""

    def __init__(self, source: Path, detail: str) -> None:
        super().__init__(f"{source}: {detail}")
        self.detail = detail


def read(machine: Machine, values: Mapping[str, str] | None = None) -> MachineSwitch:
    """Read the exact switch key from the registry or supplied values."""
    key = f"enabled.{machine.value}"
    source = conf.path("build_targets")
    if values is None:
        settings = conf.read(source).settings
        if not isinstance(settings, dict):
            raise MachineSwitchError(source, f"{key}: {settings.reason}")
        setting = settings.get(key)
        value = None if setting is None else setting.value
    else:
        value = values.get(key)
    if value is None:
        raise MachineSwitchError(source, f"{key}: key is missing")
    try:
        return MachineSwitch(value)
    except ValueError:
        raise MachineSwitchError(source, f"{key}={value!r}: expected on or off") from None
