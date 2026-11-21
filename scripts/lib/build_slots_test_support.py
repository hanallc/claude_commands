"""Give slot tests private roots and explicit pool limits."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.buildlog.store import host_name
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
    """Set private slot and cargo roots for the host and the default."""
    seed_config_fixture_folder(settings.parent, {settings.name: workspace_folder_settings(root)})
    lines = [line for line in settings.read_text().splitlines()
             if not line.startswith(("roots.slots", "roots.slot_cargo"))]
    lines.extend((f"roots.slots={root / 'default-slots'}", f"roots.slots.{machine}={root / 'slots'}",
                  f"roots.slot_cargo={root / 'default-cargo'}", f"roots.slot_cargo.{machine}={root / 'cargo'}"))
    _ = settings.write_text("\n".join(lines) + "\n")


def private_slot_config(root: Path, *, repository: str, maximum: int | UseDefaultMaximum = 2,
                        default_maximum: int = 1, unused_days: int = 7) -> SlotTestConfiguration:
    """Give direct calls and child commands private roots without a caller session."""
    configuration = root / "config"
    settings = configuration / "build_folders.conf"
    write_scratch_settings(settings.parent, root)
    slots = root / "slots"
    cargo = root / "cargo"
    machine = host_name()
    write_private_slot_roots(settings, root, machine)
    environment = config_environment(configuration)
    lines = [line for line in settings.read_text().splitlines()
             if not line.startswith(("roots.workspace", "roots.session", "slots.", "patterns.slot="))]
    lines.extend((f"roots.workspace={root / 'workspaces'}", f"roots.session={root / 'session'}",
                  "patterns.slot={repository}-slot{slot}", "# whole: 1..",
                  f"slots.{machine}.default_max={default_maximum}", "# whole: 1..",
                  f"slots.unused_days={unused_days}", "# range: 5e-324..", "slots.wait_seconds=30"))
    if isinstance(maximum, int):
        lines.extend(("# whole: 1..", f"slots.{machine}.{repository}.max={maximum}"))
    _ = settings.write_text("\n".join(lines) + "\n")
    copies = settings.with_name("copies.conf")
    _ = write_config_fixture(copies, f"remote_registry={root / 'remote-mirrors'}\n",
                             defaults=copy_fixture_settings(root))
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
