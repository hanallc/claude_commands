"""Give slot tests private roots and explicit pool limits."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.buildlog.store import host_name
from scripts.lib import conf
from scripts.lib.build_folders_test_support import workspace_folder_settings
from scripts.lib.git_test_support import write_fixture_git_config
from scripts.lib.scratch_test_support import write_scratch_settings
from scripts.lint.memory_test_support import config_environment, seed_config_fixture_folder, write_config_fixture


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


def copy_fixture_settings(root: Path, *, git_timeout_seconds: int = 600) -> str:
    """Set the private mirror directory and the limits for copy commands."""
    return f"""\
remote_registry={root / 'remote-mirrors'}
# range: 0.001..
git_timeout_seconds={git_timeout_seconds}
# range: 0.001..
guard_timeout_seconds=10
# whole: 1..
guard_choice_limit=32
owner.nightly=nightly
owner.style=style
owner.verify=verify
"""


def write_private_slot_roots(settings: Path, root: Path, machine: str) -> None:
    """Set private slot, cargo, and build roots for the fixture."""
    removed = {"roots.mirror", "roots.shell", "patterns.mirror", "patterns.shell-cache"}
    defaults = "\n".join(line for line in workspace_folder_settings(root).splitlines()
                         if line.partition("=")[0] not in removed)
    seed_config_fixture_folder(settings.parent, {settings.name: defaults})
    lines = [line for line in settings.read_text().splitlines()
             if not line.startswith(("roots.slots", "roots.slot_cargo", "roots.build"))]
    lines.extend((f"roots.slots={root / 'default-slots'}", f"roots.slots.{machine}={root / 'slots'}",
                  f"roots.slot_cargo={root / 'default-cargo'}", f"roots.slot_cargo.{machine}={root / 'cargo'}",
                  f"roots.build={root / 'build'}"))
    for host in sorted({"natedev", machine}):
        lines.append(f"roots.build.{host}={root / 'build'}")
    _ = settings.write_text("\n".join(lines) + "\n")


def private_slot_config(root: Path, *, repository: str, maximum: int | UseDefaultMaximum = 2,
                        default_maximum: int = 1) -> SlotTestConfiguration:
    """Give direct calls and child commands private roots without a caller session."""
    configuration = root / "config"
    settings = configuration / "build_folders.conf"
    write_scratch_settings(settings.parent, root)
    slots = root / "slots"
    cargo = root / "cargo"
    machine = host_name()
    write_private_slot_roots(settings, root, machine)
    environment = config_environment(configuration)
    limits = f"# whole: 1..\nslots.{machine}.default_max={default_maximum}\n"
    machines = settings.with_name("machines.conf")
    mac_hosts = conf.read(machines).words("mac.hosts") if machines.exists() else ()
    if mac_hosts and mac_hosts[0] != machine:
        limits += f"# whole: 1..\nslots.{mac_hosts[0]}.default_max=1\n"
    if isinstance(maximum, int):
        limits += f"# whole: 1..\nslots.{machine}.{repository}.max={maximum}\n"
    _ = write_config_fixture(settings, limits)
    copies = settings.with_name("copies.conf")
    _ = write_config_fixture(copies, f"remote_registry={root / 'remote-mirrors'}\n",
                             defaults=copy_fixture_settings(root, git_timeout_seconds=10))
    seed_config_fixture_folder(configuration, {"build_steps.conf": "names.integrate=integrate\n"})
    git_config = write_fixture_git_config(root)
    environment.update({
        "COPIES_CONFIG": str(copies), "SCRATCH_ROOT": str(root / "scratch"),
        "SCRATCH_COPIES_ROOT": str(root / "workspaces"), "SCRATCH_HOLDS": str(root / "holds.json"),
        "SESSION_TEMP_ROOT": str(root / "session"), "SESSION_FORMER_ROOT": str(root / "former-session"),
        "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_FOLDER": "",
        "GIT_CONFIG_GLOBAL": str(git_config), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    })
    return SlotTestConfiguration(settings, slots, cargo, machine, environment)
