"""Give slot tests private roots and explicit pool limits."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.buildlog.store import host_name
from scripts.lib.git_test_support import write_fixture_git_config
from scripts.lint.memory_test_support import config_environment, write_config_fixture


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


def write_private_slot_roots(settings: Path, root: Path, machine: str) -> None:
    """Set private slot and cargo roots for the host and the default."""
    if not settings.exists():
        _ = write_config_fixture(settings)
    lines = [line for line in settings.read_text().splitlines()
             if not line.startswith(("roots.slots", "roots.slot_cargo"))]
    lines.extend((f"roots.slots={root / 'default-slots'}", f"roots.slots.{machine}={root / 'slots'}",
                  f"roots.slot_cargo={root / 'default-cargo'}", f"roots.slot_cargo.{machine}={root / 'cargo'}"))
    _ = settings.write_text("\n".join(lines) + "\n")


def private_slot_config(root: Path, *, repository: str, maximum: int | UseDefaultMaximum = 2,
                        default_maximum: int = 1, unused_days: int = 7) -> SlotTestConfiguration:
    """Give direct calls and child commands private roots without a caller session."""
    environment = config_environment(root / "config")
    settings = Path(environment["CONF_DIR"]) / "build_folders.conf"
    slots = root / "slots"
    cargo = root / "cargo"
    machine = host_name()
    fixed = f"""# whole: 1..
names.suffix_length=8
# whole: 1..64
names.repository_key_length=8
names.suffix_characters=abcdefghijklmnopqrstuvwxyz0123456789_
roots.system=/tmp
roots.gate=/dev/shm
roots.temporary={root / 'temporary'}
roots.slots={root / 'default-slots'}
roots.slots.{machine}={slots}
roots.slot_cargo={root / 'default-cargo'}
roots.slot_cargo.{machine}={cargo}
roots.workspace={root / 'workspaces'}
roots.session={root / 'session'}
roots.scratch={root / 'scratch'}
roots.copies={root / 'workspaces'}
roots.mirror={root / 'mirrors'}
roots.shell={root / 'shells'}
patterns.slot={{repository}}-slot{{slot}}
patterns.tool={{purpose}}{{suffix}}
patterns.named={{showrunner}}-{{unit}}/{{work}}-{{purpose}}
patterns.legacy-unique={{purpose}}{{suffix}}
patterns.legacy-stable={{purpose}}
patterns.editable={{repository}}-{{owner}}/{{purpose}}
patterns.temporary={{repository}}/{{owner}}-{{purpose}}-{{suffix}}
patterns.gate={{purpose}}-{{suffix}}
patterns.standing={{repository}}/{{owner}}-gate{{suffix}}
patterns.mirror={{repository}}/{{owner}}-s{{slot}}
patterns.target={{purpose}}
patterns.shell-cache={{repository}}
patterns.scratch={{purpose}}-{{suffix}}
prefixes.queue-place=queue-place-
# range: 0.001..60
locks.probe_wait_seconds=0.25
# range: 0.001..1
locks.probe_retry_seconds=0.005
# whole: 1..
slots.{machine}.default_max={default_maximum}
# whole: 1..
slots.unused_days={unused_days}
# range: 5e-324..
slots.wait_seconds=30"""
    fixed_lines = fixed.splitlines()
    keys = {line.partition("=")[0] for line in fixed_lines if "=" in line}
    replaced_prefixes = ("slots.", *(f"{key}." for key in keys))
    lines = [line for line in settings.read_text().splitlines()
             if (key := line.partition("=")[0].strip()) not in keys and not key.startswith(replaced_prefixes)]
    lines.extend(fixed_lines)
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
        "SESSION_TEMP_ROOT": str(root / "session"), "SESSION_FORMER_ROOT": str(root / "former-session"),
        "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_FOLDER": "",
        "GIT_CONFIG_GLOBAL": str(git_config), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    })
    return SlotTestConfiguration(settings, slots, cargo, machine, environment)
