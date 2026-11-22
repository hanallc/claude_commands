"""Keep start paths available to sessions that still use them."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.lib.transcripts import config_dirs


def link_transcripts(old: Path, new: Path) -> None:
    """Give the new start path access to each existing Claude transcript folder."""
    old_name = re.sub(r"[^A-Za-z0-9]", "-", str(old))
    new_name = re.sub(r"[^A-Za-z0-9]", "-", str(new))
    if old_name == new_name:
        return
    for projects in sorted({(folder / "projects").resolve() for folder in config_dirs()}):
        source, destination = projects / old_name, projects / new_name
        if not source.is_dir():
            continue
        try:
            destination.symlink_to(source.name, target_is_directory=True)
        except FileExistsError:
            if destination.resolve() != source.resolve():
                raise ValueError(f"{destination}: existing transcript folder differs from {source}") from None


@dataclass(frozen=True)
class SessionStart:
    identity: str
    pid: int
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class SessionStarts:
    sessions: tuple[SessionStart, ...]
    unreadable: tuple[str, ...]


def _linux_agents(root: Path) -> dict[int, dict[str, str] | OSError]:
    from scripts.herdr import herdr

    found: dict[int, dict[str, str] | OSError] = {}
    for process in root.iterdir():
        if not process.name.isdecimal():
            continue
        pid = int(process.name)
        try:
            if process.stat().st_uid != os.getuid():
                continue
            arguments = os.fsdecode((process / "cmdline").read_bytes()).split("\0")
            if not (herdr.is_claude_process(arguments) or herdr.is_codex_process(arguments)):
                continue
            fields = os.fsdecode((process / "environ").read_bytes()).split("\0")
            found[pid] = dict(field.split("=", 1) for field in fields if "=" in field)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            found[pid] = error
    return found


def _darwin_agents() -> dict[int, dict[str, str] | OSError]:
    from scripts.herdr import herdr

    listed = subprocess.run(["ps", "-axo", "uid=,pid=,comm="], check=True, capture_output=True, text=True)
    found: dict[int, dict[str, str] | OSError] = {}
    for line in listed.stdout.splitlines():
        uid, pid_text, executable = line.split(maxsplit=2)
        if int(uid) != os.getuid() or Path(executable).name not in {"claude", "codex", "codex-aarch64-apple-darwin"}:
            continue
        pid = int(pid_text)
        try:
            found[pid] = herdr.darwin_process_environment(pid)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            found[pid] = OSError(str(error))
    return found


def live_session_starts() -> SessionStarts:
    """Read lexical start paths. Unreadable evidence protects every old link."""
    from scripts.herdr import herdr
    from scripts.message import session_records

    directories = {session_records.sessions_dir().resolve()}
    directories.update((account / "sessions").resolve() for account in config_dirs()
                       if (account / "sessions").exists())
    records: dict[int, list[session_records.SessionRecord]] = {}
    unreadable: list[str] = []
    for directory in sorted(directories):
        registry = session_records.read_registry(directory=directory)
        unreadable.extend(str(path) for path in registry.unreadable)
        for observation in registry.entries:
            record = observation.record
            if session_records.running_session(record):
                records.setdefault(record["pid"], []).append(record)
    agents = _darwin_agents() if sys.platform == "darwin" else _linux_agents(herdr.proc_root())
    sessions: list[SessionStart] = []
    for pid in sorted(agents.keys() | records.keys()):
        environment = agents.get(pid, {})
        entries = records.get(pid, [])
        paths = {Path(cwd) for entry in entries if (cwd := entry.get("cwd", "")) and Path(cwd).is_absolute()}
        identity = next((entry["sessionId"] for entry in entries if entry["sessionId"]), f"pid {pid}")
        if isinstance(environment, OSError):
            unreadable.append(f"pid {pid}: {environment}")
        else:
            pwd = Path(environment.get("PWD", ""))
            if pwd.is_absolute():
                paths.add(pwd)
            identity = environment.get("CODEX_THREAD_ID") or environment.get("CLAUDE_CODE_SESSION_ID") or identity
        if not paths:
            unreadable.append(f"{identity}: start path is unavailable")
        sessions.append(SessionStart(identity, pid, tuple(sorted(paths))))
    return SessionStarts(tuple(sessions), tuple(unreadable))
