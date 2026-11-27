"""Give numbered copy tests private roots."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import os
import signal
import stat
import subprocess
import sys

from scripts.buildlog.store import host_name
from scripts.lib import conf
from scripts.lib.git_test_support import write_fixture_git_config
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import config_environment, write_config_fixture


@dataclass(frozen=True)
class CopyTestConfiguration:
    settings: Path
    machine: str
    environment: Mapping[str, str]


def write_private_build_roots(settings: Path, root: Path, machine: str) -> None:
    """Set a private build root for the fixture."""
    if not settings.exists():
        _ = write_config_fixture(settings)
    lines = [line for line in settings.read_text().splitlines() if not line.startswith("roots.build")]
    lines.append(f"roots.build={root / 'build'}")
    for host in sorted({"natedev", conf.host_key_name(machine)}):
        lines.append(f"roots.build.{host}={root / 'build'}")
    _ = settings.write_text("\n".join(lines) + "\n")


def private_copy_config(root: Path) -> CopyTestConfiguration:
    """Give direct calls and child commands private roots without a caller session."""
    environment = config_environment(root / "config")
    settings = Path(environment["CONF_DIR"]) / "build_folders.conf"
    machine = host_name()
    host = conf.host_key_name(machine)
    fixed = f"""# whole: 1..
names.suffix_length=8
# whole: 1..64
names.repository_key_length=8
names.suffix_characters=abcdefghijklmnopqrstuvwxyz0123456789_
roots.system=/tmp
roots.gate={root / 'gate'}
roots.temporary={root / 'temporary'}
roots.build={root / 'build'}
roots.build.natedev={root / 'build'}
roots.workspace={root / 'workspaces'}
roots.session={root / 'session'}
roots.scratch={root / 'scratch'}
roots.copies={root / 'workspaces'}
patterns.tool={{purpose}}{{suffix}}
patterns.named={{showrunner}}-{{unit}}/{{work}}-{{purpose}}
patterns.legacy-unique={{purpose}}{{suffix}}
patterns.legacy-stable={{purpose}}
patterns.editable={{repository}}-{{owner}}/{{purpose}}
patterns.temporary={{repository}}/{{owner}}-{{purpose}}-{{suffix}}
patterns.gate={{purpose}}-{{suffix}}
patterns.standing={{repository}}/{{owner}}-gate{{suffix}}
patterns.target={{purpose}}
patterns.scratch={{purpose}}-{{suffix}}
prefixes.queue-place=queue-place-
# range: 0.001..60
locks.probe_wait_seconds=0.25
# range: 0.001..1
locks.probe_retry_seconds=0.005
# whole: 1..
folders.unused_days=2"""
    fixed_lines = fixed.splitlines()
    if host != "natedev":
        fixed_lines.append(f"roots.build.{host}={root / 'build'}")
    keys = {line.partition("=")[0] for line in fixed_lines if "=" in line}
    replaced_prefixes = tuple(f"{key}." for key in keys)
    lines = [line for line in settings.read_text().splitlines()
             if (key := line.partition("=")[0].strip()) not in keys and not key.startswith(replaced_prefixes)]
    lines.extend(fixed_lines)
    _ = settings.write_text("\n".join(lines) + "\n")
    copies = settings.with_name("copies.conf")
    _ = write_config_fixture(copies, f"remote_registry={root / 'remote-mirrors'}\ngit_timeout_seconds=10\n")
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
    return CopyTestConfiguration(settings, machine, environment)


def fixed_registration_snapshot(
    build: Path, source: Path, *records: Path,
) -> dict[Path, bytes | tuple[str, ...]]:
    """Record the fixed folders, registrations, and recovery records."""
    git = source / ".git"
    roots = (build, git / "worktrees", git / "mac-linux-worktree", git / "mac-smoke-worktree", *records)
    entries: dict[Path, bytes | tuple[str, ...]] = {}
    for root in roots:
        for entry in (root, *sorted(root.rglob("*"))):
            try:
                mode = entry.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                entries[entry] = ("link", str(entry.readlink()))
            elif stat.S_ISDIR(mode):
                entries[entry] = ("directory",)
            else:
                entries[entry] = entry.read_bytes()
    return entries


class FixedRegistrationKillPoint(Enum):
    """Select where to stop the first registration with KILL."""

    BEFORE_GIT = "before-git"
    DURING_RESCUE = "during-rescue"


def kill_fixed_registration(
    source: Path, checkout: Path, commit: str, environment: Mapping[str, str], point: FixedRegistrationKillPoint,
) -> None:
    """Leave the registration mark after KILL at the selected point."""
    program = """
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch

from scripts.lib import build_copies, build_folders, git_rescue

source, checkout = map(Path, sys.argv[1:3])
commit, point = sys.argv[3:5]
registration = (build_folders.FixedCheckoutRegistration.MAC_SMOKE if checkout.name == "mac"
                else build_folders.FixedCheckoutRegistration.MAC_LINUX)
checkout_source = build_folders.FixedCheckoutSource(source, registration)
run_rescue_git = git_rescue._git

def kill_before_git(*args):
    os.kill(os.getpid(), signal.SIGKILL)

def kill_during_rescue(folder, *words, **options):
    if words[0] == "update-ref":
        os.kill(os.getpid(), signal.SIGKILL)
    return run_rescue_git(folder, *words, **options)

stop = (patch.object(build_folders, "_register_fixed_build_checkout", side_effect=kill_before_git)
        if point == "before-git" else patch.object(git_rescue, "_git", side_effect=kill_during_rescue))
with build_folders.fixed_repository_build_folder(
    checkout.parent.name, machine=checkout.name, unit="first", checkout_source=checkout_source,
) as folder, stop:
    assert folder == checkout, (folder, checkout)
    (folder / "user-work").write_bytes(b"work before registration")
    (folder / "target").mkdir()
    (folder / "target/cache").write_bytes(b"Cargo output")
    build_copies.prepare_fixed_checkout(source, folder, commit, reserved=("target",), registration=registration)
"""
    tree = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", program, str(source), str(checkout), commit, point.value],
                            cwd=tree, env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
    if result.returncode != -signal.SIGKILL:
        raise AssertionError(f"The registration worker did not die by KILL: {result.returncode}\n{result.stderr}")
    git_entry = checkout / ".git"
    if point is FixedRegistrationKillPoint.BEFORE_GIT:
        unexpected_entry = os.path.lexists(git_entry)
    else:
        unexpected_entry = not git_entry.is_file()
    if unexpected_entry:
        raise AssertionError(f"{checkout}: unexpected Git pointer after KILL at {point.value}")
