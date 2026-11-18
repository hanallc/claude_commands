"""Read the machine's repository slot root.

This module imports only configuration and the host name, so a low-level module can test for a slot
without the slot pool and its queue imports.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path

from scripts.buildlog.store import host_name
from scripts.lib import conf


class SlotsNotConfigured(ValueError):
    """The user has not activated repository slots on this machine."""

    def __init__(self, machine: str) -> None:
        super().__init__(f"slots: roots.slots.{machine} is not configured; slots are not active")


def slot_root(values: Mapping[str, str], machine: str) -> str:
    """Select the machine's slot root, with the common root as the default."""
    return values.get(f"roots.slots.{machine}", values.get("roots.slots", ""))


def configured_maximum(settings: conf.ConfigRead, machine: str, repository: str) -> int:
    """Read the repository limit or the machine's default limit."""
    key = f"slots.{machine}.{repository}.max"
    return settings.whole(key if key in settings.values(key, validate=False) else f"slots.{machine}.default_max")


def configured_slot_root(
    settings: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
) -> Path:
    """Read the machine's slot root without creating a folder."""
    if isinstance(settings, conf.UseConfiguredValue):
        settings = conf.read_or_repository("build_folders")
    machine = host_name()
    key = f"roots.slots.{machine}"
    if key not in settings.values(key, validate=False):
        key = "roots.slots"
        if key not in settings.values(key, validate=False):
            raise SlotsNotConfigured(machine)
    root = settings.value(key, str, allow_blank=True)
    if not root:
        raise SlotsNotConfigured(machine)
    return Path(os.path.expandvars(root)).expanduser().resolve()
