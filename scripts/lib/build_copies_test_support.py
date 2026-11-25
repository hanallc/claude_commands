"""Give numbered copy tests private roots."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.buildlog.store import host_name
from scripts.lib import conf
from scripts.lib.git_test_support import write_fixture_git_config
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
roots.gate=/dev/shm
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
