"""Give slot tests private roots and explicit pool limits."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.buildlog.store import host_name
from scripts.lib.git_test_support import write_fixture_git_config
from scripts.lint.memory_test_support import config_environment


@dataclass(frozen=True)
class UseDefaultMaximum:
    """Leave the repository limit unset to test the default limit."""


@dataclass(frozen=True)
class SlotTestConfiguration:
    settings: Path
    slots: Path
    cargo: Path
    machine: str
    environment: Mapping[str, str]


def private_slot_config(root: Path, *, repository: str, maximum: int | UseDefaultMaximum = 2,
                        default_maximum: int = 1, unused_days: int = 7) -> SlotTestConfiguration:
    """Write a complete private configuration for direct calls and child commands."""
    environment = config_environment(root / "config")
    settings = Path(environment["CONF_DIR"]) / "build_folders.conf"
    slots = root / "slots"
    cargo = root / "cargo"
    machine = host_name()
    lines = [line for line in settings.read_text().splitlines()
             if not line.startswith(("roots.slots", "roots.slot_cargo", "roots.workspace", "slots.", "patterns.slot="))]
    lines.extend((f"roots.slots={root / 'default-slots'}", f"roots.slots.{machine}={slots}",
                  f"roots.slot_cargo={root / 'default-cargo'}", f"roots.slot_cargo.{machine}={cargo}",
                  f"roots.workspace={root / 'workspaces'}",
                  "patterns.slot={repository}-slot{slot}", "# whole: 1..",
                  f"slots.{machine}.default_max={default_maximum}", "# whole: 1..",
                  f"slots.unused_days={unused_days}", "# range: 5e-324..", "slots.wait_seconds=30"))
    if isinstance(maximum, int):
        lines.extend(("# whole: 1..", f"slots.{machine}.{repository}.max={maximum}"))
    _ = settings.write_text("\n".join(lines) + "\n")
    copies = settings.with_name("copies.conf")
    copy_lines = [line for line in copies.read_text().splitlines()
                  if not line.startswith("remote_registry=")]
    copy_lines.append(f"remote_registry={root / 'remote-mirrors'}")
    _ = copies.write_text("\n".join(copy_lines) + "\n")
    git_config = write_fixture_git_config(root)
    environment.update({
        "COPIES_CONFIG": str(copies), "SCRATCH_ROOT": str(root / "scratch"),
        "SCRATCH_COPIES_ROOT": str(root / "workspaces"), "SCRATCH_HOLDS": str(root / "holds.json"),
        "GIT_CONFIG_GLOBAL": str(git_config), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    })
    return SlotTestConfiguration(settings, slots, cargo, machine, environment)
