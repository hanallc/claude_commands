#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Load and prove a worktree's nix environment before starting a process.

Envrc files below ~/rust, ~/worktrees or ~/.claude* are allowed automatically.
Linked worktrees registered by repositories below those roots also qualify.
Numbered copies require a regular envrc that matches their recorded source commit.
Legacy AWS slots require a regular envrc that matches their Git HEAD.
Registered scratch copies qualify when their envrc matches their recorded source commit in a trusted repository.
Other folders require explicit direnv approval.
Without an envrc, the caller keeps its environment without running direnv.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
from enum import Enum, auto
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

if TYPE_CHECKING:
    from collections.abc import Mapping
    from scripts.lib.conf import ConfigRead

# Internal, one-session handoff from the healer to shutdown restart. These are
# consumed before opening a pane and never exported to the resumed agent.
HEAL_SESSION_ENV = "NIX_HEAL_PREPARED_SESSION"
HEAL_DIRECTORY_ENV = "NIX_HEAL_PREPARED_DIRECTORY"


class Loaded(NamedTuple):
    env: dict[str, str]
    envrc: Path


class NoEnvrc(NamedTuple):
    env: dict[str, str]


class Refused(NamedTuple):
    reason: str


def _remaining_timeout(cap: float) -> float:
    """Load deadline support only when a subprocess needs a timeout."""
    from scripts.lib import operation_deadline

    return operation_deadline.remaining(cap)


def _settings() -> ConfigRead:
    from scripts.lib import conf

    return conf.read("nix_env")


def nearest_envrc(directory: Path) -> Path | None:
    """Find the nearest envrc, including the directory itself."""
    directory = directory.expanduser().resolve()
    return next((parent / ".envrc" for parent in (directory, *directory.parents)
                 if (parent / ".envrc").is_file()), None)


def trusted_root(path: Path) -> bool:
    """Whether the resolved path belongs to an automatically approved root."""
    try:
        relative = path.resolve().relative_to(Path.home().resolve())
    except (OSError, ValueError, RuntimeError):
        return False
    settings = _settings()
    return bool(relative.parts and (relative.parts[0] in settings.words("trusted_home_folders")
                                  or relative.parts[0].startswith(settings.text("trusted_home_prefix"))))


def registered_worktree(folder: Path) -> bool:
    """Prove registration by a trusted repository using only git's pointer files."""
    # Imported here: the no-envrc path keeps its import budget (no dataclasses).
    from scripts.lib import git_files
    try:
        folder = folder.resolve(strict=True)
        git_file = folder / ".git"
        if not stat.S_ISREG(git_file.lstat().st_mode):
            return False
        checkout = git_files.at(folder)
        entry, common = checkout.git_dir, checkout.common_dir
        if entry.parent.name != "worktrees" or common != entry.parent.parent:
            return False
        back_text = (entry / "gitdir").read_text(encoding="utf-8").strip()
        if not back_text or len(back_text.splitlines()) != 1:
            return False
        return trusted_root(common) and (entry / back_text).resolve(strict=True) == git_file
    except (OSError, ValueError, RuntimeError):
        return False


def _git_environment() -> dict[str, str]:
    """Keep caller overrides from choosing git's repository or object store."""
    return {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}


def _envrc_matches_commit(envrc: Path, git_directory: Path, commit: str) -> bool:
    """Compare the file bytes with one commit in the proven repository."""
    import subprocess

    timeout = _settings().number("command_timeout_seconds")
    environment = _git_environment()
    command = ["git", "--git-dir", str(git_directory)]
    expected = subprocess.run(
        [*command, "rev-parse", "--verify", "--end-of-options", f"{commit}:.envrc"],
        cwd=git_directory, env=environment, capture_output=True, text=True,
        timeout=_remaining_timeout(timeout), check=True)
    actual = subprocess.run(
        [*command, "hash-object", "--no-filters", "--", str(envrc)],
        cwd=git_directory, env=environment, capture_output=True, text=True,
        timeout=_remaining_timeout(timeout), check=True)
    return bool(expected.stdout.strip()) and expected.stdout.strip() == actual.stdout.strip()


class SlotEnvrcTrust(Enum):
    """A slot requires byte proof before any broader trust rule."""

    NOT_A_SLOT = auto()
    TRUSTED = auto()
    NEEDS_APPROVAL = auto()


def slot_envrc_trust(envrc: Path) -> SlotEnvrcTrust:
    """Prove a slot's envrc against its checked-out source commit."""
    import subprocess
    from scripts.lib import git_files, slot_root

    try:
        root = slot_root.configured_slot_root()
    except slot_root.SlotsNotConfigured:
        return SlotEnvrcTrust.NOT_A_SLOT
    try:
        folder = envrc.parent.resolve(strict=True)
        if not folder.is_relative_to(root):
            return SlotEnvrcTrust.NOT_A_SLOT
        if (folder.parent != root or envrc.name != ".envrc"
                or not stat.S_ISREG(envrc.lstat().st_mode) or not registered_worktree(folder)):
            return SlotEnvrcTrust.NEEDS_APPROVAL
        checkout = git_files.at(folder)
        return (SlotEnvrcTrust.TRUSTED if _envrc_matches_commit(envrc.absolute(), checkout.git_dir, "HEAD")
                else SlotEnvrcTrust.NEEDS_APPROVAL)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return SlotEnvrcTrust.NEEDS_APPROVAL


def _legacy_copy_envrc(envrc: Path) -> bool:
    """Compare a legacy copy's envrc with its recorded source commit."""
    import subprocess
    from scripts.lib import copy_record, git_files

    try:
        if envrc.name != ".envrc" or not stat.S_ISREG(envrc.lstat().st_mode):
            return False
        folder = envrc.parent.resolve(strict=True)
        record = copy_record.source_of(folder)
        if not isinstance(record, copy_record.RecordedCopySource):
            return False
        source = record.checkout.resolve(strict=True)
        if not source.is_dir() or not trusted_root(source):
            return False
        return _envrc_matches_commit(folder / ".envrc", git_files.at(source).git_dir, record.commit)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False


def registered_copy(envrc: Path) -> bool:
    """Prove a copy's envrc against its source commit."""
    from scripts.lib import build_slots

    try:
        numbered = build_slots.numbered_copy_envrc(envrc, trusted_source=trusted_root)
        if numbered is not build_slots.NumberedCopyEnvrc.OUTSIDE_COPY:
            return numbered is build_slots.NumberedCopyEnvrc.TRUSTED
        slot = slot_envrc_trust(envrc)
        if slot is not SlotEnvrcTrust.NOT_A_SLOT:
            return slot is SlotEnvrcTrust.TRUSTED
        return _legacy_copy_envrc(envrc)
    except (OSError, ValueError, RuntimeError):
        return False


def generated_source(root: Path) -> bool:
    """Accept a scratch record that marks files written by our code."""
    try:
        folder = root.resolve(strict=True)
        from scripts.lib import build_slots, copy_record

        if folder.parent != copy_record.root().resolve():
            return False
        numbered = build_slots.numbered_copy_envrc(folder / ".envrc", trusted_source=trusted_root)
        if numbered is not build_slots.NumberedCopyEnvrc.OUTSIDE_COPY:
            return False
        record = copy_record.read_record(folder / copy_record.RECORD)
        return isinstance(record, copy_record.Registered) and record.fields.get("generated_source") is True
    except (OSError, ValueError, RuntimeError):
        return False


def trusted_envrc(envrc: Path) -> bool:
    """Check numbered copy proof before checking general approval."""
    from scripts.lib import build_slots

    try:
        numbered = build_slots.numbered_copy_envrc(envrc, trusted_source=trusted_root)
        if numbered is not build_slots.NumberedCopyEnvrc.OUTSIDE_COPY:
            return numbered is build_slots.NumberedCopyEnvrc.TRUSTED
        resolved = envrc.resolve(strict=True)
        if resolved != envrc.absolute() and build_slots.numbered_copy_envrc(
                resolved, trusted_source=trusted_root) is not build_slots.NumberedCopyEnvrc.OUTSIDE_COPY:
            return False
        slot = slot_envrc_trust(envrc)
        if slot is not SlotEnvrcTrust.NOT_A_SLOT:
            return slot is SlotEnvrcTrust.TRUSTED
        return trusted_root(resolved) or registered_worktree(resolved.parent) or _legacy_copy_envrc(envrc)
    except (OSError, ValueError, RuntimeError):
        return False


def last_line(text: str, fallback: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1].strip() if lines else fallback


def ensure(directory: Path, base_env: Mapping[str, str] = os.environ) -> Loaded | NoEnvrc | Refused:
    """Apply direnv's export diff and require proof that a nix shell loaded."""
    try:
        directory = directory.expanduser().resolve()
        if not directory.is_dir():
            return Refused(f"nix_env: {directory}: directory does not exist")
        envrc = nearest_envrc(directory)
        environment = dict(base_env)
        if envrc is None:
            return NoEnvrc(environment)
        from scripts.lib import build_folders, copy_record

        numbered = copy_record.numbered_copy_checkout(directory)
        if isinstance(numbered, copy_record.OutsideNumberedCopy):
            numbered = copy_record.numbered_copy_checkout(envrc.resolve())
        if isinstance(numbered, copy_record.OutsideNumberedCopy):
            return _load_envrc(directory, envrc, environment, environment)
        with build_folders.numbered_copy_use_lease(numbered) as lease:
            if isinstance(lease, build_folders.NumberedCopyUseRefused):
                return Refused(f"nix_env: {directory}: {lease.reason}")
            envrc = nearest_envrc(directory)
            if envrc is None:
                return NoEnvrc(environment)
            command_env = {**environment, **lease.environment}
            return _load_envrc(directory, envrc, environment, command_env, pass_fds=lease.pass_fds)
    except (OSError, ValueError, RuntimeError) as error:
        return Refused(f"nix_env: {directory}: " + last_line(str(error), type(error).__name__))


def _load_envrc(directory: Path, envrc: Path, environment: dict[str, str], command_env: Mapping[str, str], *,
                pass_fds: tuple[int, ...] = ()) -> Loaded | Refused:
    """Prove the selected envrc and apply direnv's export."""
    import subprocess

    timeout = _settings().number("command_timeout_seconds")
    trusted = trusted_envrc(envrc)
    commands = (["direnv", "allow", str(envrc)], ["direnv", "export", "json"]) if trusted else (
        ["direnv", "export", "json"],)
    exported = ""
    try:
        for command in commands:
            result = subprocess.run(command, cwd=directory, env=command_env, capture_output=True,
                                    text=True, timeout=_remaining_timeout(timeout), check=False, pass_fds=pass_fds)
            if result.returncode:
                reason = last_line(result.stderr, f"direnv exit {result.returncode}")
                if not trusted and any(word in result.stderr.lower() for word in ("not allowed", "blocked", "denied")):
                    # Pay shlex's startup cost only for an explicit approval hint.
                    import shlex

                    reason += "; run " + shlex.join(["direnv", "allow", str(envrc)])
                return Refused(f"nix_env: {directory}: {reason}")
            exported = result.stdout
    except subprocess.TimeoutExpired as error:
        detail = error.stderr
        stderr = detail.decode(errors="replace") if isinstance(detail, bytes) else detail or ""
        return Refused(f"nix_env: {directory}: " + last_line(stderr, f"direnv timed out after {timeout:g} s"))
    # Pay JSON's startup cost only when decoding an envrc export.
    import json

    # direnv's no-update path succeeds without emitting an export object.
    diff: object = json.loads(exported) if exported.strip() else {}
    if not isinstance(diff, dict):
        return Refused(f"nix_env: {directory}: direnv export is not a JSON object")
    for key, value in cast(dict[str, object], diff).items():
        if not key or "=" in key or "\0" in key:
            return Refused(f"nix_env: {directory}: direnv export has an invalid variable name")
        if value is None:
            if key in environment:
                del environment[key]
        elif isinstance(value, str) and "\0" not in value:
            environment[key] = value
        else:
            return Refused(f"nix_env: {directory}: direnv export has an invalid value for {key}")
    if not environment.get("IN_NIX_SHELL"):
        return Refused(f"nix_env: {directory}: .envrc loaded but no nix shell (IN_NIX_SHELL unset)")
    return Loaded(environment, envrc)


def main(arguments: list[str]) -> int:
    if len(arguments) == 2 and arguments[0] == "ensure" and arguments[1]:
        outcome = ensure(Path(arguments[1]))
        if isinstance(outcome, Refused):
            print(outcome.reason, file=sys.stderr)
            return 1
        # Pay JSON's startup cost only when the CLI serializes an environment.
        import json

        print(json.dumps(outcome.env))
        return 0
    if len(arguments) >= 2 and arguments[0] == "exec":
        from scripts.lib.models_registry import agent_environment

        program = arguments[1]
        if not Path(program).is_absolute():
            print("nix_env: exec requires an absolute program path", file=sys.stderr)
            return 2
        outcome = ensure(Path.cwd())
        if isinstance(outcome, Refused):
            print(outcome.reason, file=sys.stderr)
            return 1
        if isinstance(outcome, Loaded):
            outcome.env["NIX_ENV_ENVRC"] = str(outcome.envrc.resolve())
        else:
            _ = outcome.env.pop("NIX_ENV_ENVRC", None)
        try:
            os.execvpe(program, arguments[1:], agent_environment(outcome.env))
        except OSError as error:
            print("nix_env: " + last_line(str(error), type(error).__name__), file=sys.stderr)
            return 1
    print("nix_env: usage: nix_env.py ensure DIR | exec ABSOLUTE_PROGRAM [ARGS...]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
