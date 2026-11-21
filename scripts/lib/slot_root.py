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


def configured_slot_root() -> Path:
    """Read the machine's slot root without creating a folder."""
    settings = conf.read_or_repository("build_folders")
    machine = host_name()
    root = slot_root(settings.values(), machine)
    if not root:
        raise SlotsNotConfigured(machine)
    return Path(os.path.expandvars(root)).expanduser().resolve()
