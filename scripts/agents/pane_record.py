"""Retain each pane's launch so restore can use its account and conversation.

Each herdr session has one file under `~/.local/state/herdr-panes`.
The file keeps the account, flags, and environment for each pane. Staff supplies each unit folder.
The Claude SessionStart hook records Claude launches.
The normal herdr start path records Codex launches after it proves the foreground process.
The seed command records the proved native owner of each pane.

Stored member references use fixed staff ids.
Runtime observations prove the current conversation before a stop or resume.
An intentional exit marks its launch ended, so restore keeps that session stopped.
A proved resume clears the ended state.

Linux reads process evidence from /proc.
The Mac reads parents through `ps` and arguments through KERN_PROCARGS2.
Environment readers retain only the names below.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, cast

if TYPE_CHECKING:
    from scripts.herdr import herdr
    from scripts.production import staff


from scripts.herdr import herdr_types, pane_launch  # noqa: E402
from scripts.lib import legacy_identity, models_registry
from scripts.lib.transcripts import open_transcript, transcript_paths, transcript_source
from scripts.message import sessions
from scripts.herdr.pane_launch import PaneLaunch as PaneLaunch
from scripts.herdr.pane_launch import (
    CLAUDE_VALUE_FLAGS as CLAUDE_VALUE_FLAGS,
    KEPT_NAMES as KEPT_NAMES,
    MODEL_FLAGS as MODEL_FLAGS,
    NAME_FLAGS as NAME_FLAGS,
    RESUME_FLAGS as RESUME_FLAGS,
    ClaudeArguments as ClaudeArguments,
    parse_claude_arguments as parse_claude_arguments,
    parse_claude_flags as parse_claude_flags,
)

RECORD_DIR = pane_launch.record_directory()
ANCESTOR_LIMIT = 16
PROC = Path(os.environ.get("HERDR_PROC", "/proc"))


def record_directory() -> Path:
    """Use the configured store, including an explicit binding by an embedded caller."""
    return RECORD_DIR


def record_path(session: str) -> Path:
    return record_directory() / f"{session}.json"


def read_strict(session: str, *, dependencies: Callable[[Path], None] | None = None) -> dict[str, PaneLaunch]:
    """Read validated launch evidence; only an absent file means there are no entries."""
    path = record_path(session)
    if dependencies is not None:
        dependencies(path)
    return pane_launch.read_strict(path)


def read(session: str) -> dict[str, PaneLaunch]:
    """The session's entries by pane id; an absent or unreadable file reads as none."""
    return pane_launch.read(record_path(session))


def launches() -> list[PaneLaunch]:
    """Read retained launches through the same per-session reader as restore."""
    return [entry for path in record_directory().glob("*.json") for entry in read(path.stem).values()]


def launch_for_session(session_id: str) -> PaneLaunch | None:
    """Read a conversation's newest launch, including account moves and intentional exits."""
    publication = publication_for_session(session_id)
    return publication.entry if publication is not None else None


def publication_for_session(session_id: str, *, record: staff.StaffSnapshot | None = None) -> pane_launch.LaunchPublication | None:
    """Keep the source file and exact publication for a conversation."""
    from scripts.production import staff

    owner = (staff.initial_snapshot() if record is None else record).by_current_session(session_id)
    reference = owner.id if owner is not None else session_id
    return pane_launch.newest_publication(record_directory(), (reference, session_id))


def resume_launch(entry: PaneLaunch) -> pane_launch.CurrentLaunch:
    """Resolve a retained member reference to its current conversation for resume."""
    from scripts.production import staff

    return pane_launch.current_launch(entry, staff.initial_snapshot())


def launch(pane_id: str, session_id: str, cwd: str, environment: Mapping[str, str],
           arguments: Sequence[str], *, kind: Literal["claude", "codex"] = "claude") -> pane_launch.CurrentLaunch:
    """Retain plugin paths only in the shutdown restore record."""
    entry = pane_launch.launch(pane_id, session_id, cwd, legacy_identity.without_legacy_identity(environment), arguments, kind=kind)
    if kind == "claude":
        plugins = parse_claude_arguments(arguments).plugin_directories
        if plugins:
            entry["plugin_directories"] = [str((Path(cwd) / directory).resolve()) for directory in plugins]
    return entry


class ClaudeTurnSettings(NamedTuple):
    """The model and optional effort from one real assistant turn."""

    model: str
    effort: str


class NoClaudeTurn(NamedTuple):
    """No readable assistant turn records a model for this session."""


class ClaudeResumeSettings(NamedTuple):
    """The model and effort flags for a Claude resume. Empty values omit flags."""

    model: str = ""
    effort: str = ""

    def arguments(self) -> list[str]:
        arguments: list[str] = []
        if self.model:
            arguments.extend(("--model", self.model))
        if self.effort:
            arguments.extend(("--effort", self.effort))
        return arguments


NO_CLAUDE_RESUME_SETTINGS = ClaudeResumeSettings()


def claude_resume_settings(preferred: ClaudeTurnSettings | NoClaudeTurn | ClaudeResumeSettings, *,
                           fallback: ClaudeResumeSettings = NO_CLAUDE_RESUME_SETTINGS,
                           explicit_model: str = "") -> ClaudeResumeSettings:
    """Resolve each Claude resume flag from its own available settings."""
    if isinstance(preferred, NoClaudeTurn):
        preferred = NO_CLAUDE_RESUME_SETTINGS
    return ClaudeResumeSettings(explicit_model or preferred.model or fallback.model,
                                preferred.effort or fallback.effort)


def _transcript_lines_from_end(path: Path) -> Iterator[bytes]:
    """Read newest entries first without loading the whole conversation."""
    with open_transcript(path) as stream:
        offset = stream.seek(0, os.SEEK_END)
        pending = b""
        while offset:
            size = min(offset, 65536)
            offset -= size
            _ = stream.seek(offset)
            lines = (stream.read(size) + pending).split(b"\n")
            pending = lines[0]
            yield from reversed(lines[1:])
        if pending:
            yield pending


def claude_turn_settings(session_id: str, *config_dirs: Path) -> ClaudeTurnSettings | NoClaudeTurn:
    """Read the latest real assistant turn across the session's Claude homes.

    A dated turn wins over an undated turn. Use file mtime, then path, for ties.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return NoClaudeTurn()
    matches = set(sessions.transcript_locations(session_id))
    for directory in config_dirs:
        matches.update(path.resolve() for path in transcript_paths(directory / "projects", f"*/{session_id}.jsonl"))
    turns: dict[tuple[bool, datetime, int, str], ClaudeTurnSettings] = {}
    for path in matches:
        try:
            modified = transcript_source(path).stat().st_mtime_ns
            for line in _transcript_lines_from_end(path):
                if b'"assistant"' not in line:
                    continue
                try:
                    value = cast(object, json.loads(line))
                except (ValueError, RecursionError):
                    continue
                if not isinstance(value, dict):
                    continue
                record = cast(dict[str, object], value)
                message = record.get("message")
                if record.get("type") != "assistant" or not isinstance(message, dict):
                    continue
                model = cast(dict[str, object], message).get("model")
                if isinstance(model, str) and model and model != "<synthetic>":
                    effort = record.get("effort")
                    timestamp = record.get("timestamp")
                    stamp = datetime.min.replace(tzinfo=UTC)
                    dated = False
                    if isinstance(timestamp, str):
                        try:
                            parsed = datetime.fromisoformat(timestamp)
                            if parsed.tzinfo is not None:
                                stamp = parsed.astimezone(UTC)
                                dated = True
                        except (ValueError, OverflowError):
                            pass
                    turns[dated, stamp, modified, str(path)] = ClaudeTurnSettings(
                        model, effort if isinstance(effort, str) else "")
                    break
        except (OSError, subprocess.SubprocessError):
            continue
    return turns[max(turns)] if turns else NoClaudeTurn()


def _no_claude_role_fallback() -> ClaudeResumeSettings:
    return NO_CLAUDE_RESUME_SETTINGS


def resume_arguments(entry: PaneLaunch, *,
                     claude_role_fallback: Callable[[], ClaudeResumeSettings] = _no_claude_role_fallback) -> list[str]:
    """Build resume arguments with the latest Claude settings when available."""
    current = resume_launch(entry) if "session_id" not in entry else entry
    if pane_launch.saved_kind(current) == "codex":
        return _resume_arguments(current)
    homes = tuple(Path(directory) for directory in (current.get("config_dir", ""),
        current["environment"].get("CLAUDE_CONFIG_DIR", "")) if directory)
    turn = claude_turn_settings(current["session_id"], *homes)
    settings = claude_resume_settings(turn, fallback=ClaudeResumeSettings(model=current["model"]))
    if not settings.model or not settings.effort:
        settings = claude_resume_settings(settings, fallback=claude_role_fallback())
    return _resume_arguments(current, claude_settings=settings)


def _resume_arguments(entry: pane_launch.NativePaneLaunch, *,
                      claude_settings: ClaudeResumeSettings = NO_CLAUDE_RESUME_SETTINGS) -> list[str]:
    if pane_launch.saved_kind(entry) == "codex":
        arguments = ["resume", entry["session_id"]]
        if entry["model"]:
            arguments.extend(("--model", entry["model"]))
        arguments.extend(entry.get("codex_policy", []))
        return arguments
    arguments = ["--resume", entry["session_id"]]
    if entry["name"]:
        arguments.extend(("-n", entry["name"]))
    if entry["remote_control"]:
        arguments.extend(("--remote-control", entry["name"]) if entry["name"] else ("--remote-control",))
    arguments.extend(claude_settings.arguments())
    for directory in entry.get("plugin_directories", []):
        arguments.extend(("--plugin-dir", directory))
    arguments.extend(("--settings", '{"disableAgentView": true}'))
    return arguments


def configured_resume_arguments(entry: PaneLaunch, identity: str, name: str,
                                settings: models_registry.LaunchSettings) -> list[str]:
    """Apply current model settings and retain the saved resume options."""
    current = resume_launch(entry) if "session_id" not in entry else cast(pane_launch.CurrentLaunch, entry.copy())
    current.update(kind=settings.kind, session_id=identity, name=name, model=settings.model)
    if settings.kind == "codex":
        policy = ["-c", f'model_reasoning_effort="{settings.effort}"']
        if settings.service_tier:
            policy.extend(("-c", f'service_tier="{settings.service_tier}"'))
        saved = iter(entry.get("codex_policy", []))
        for option in saved:
            value = next(saved, "")
            if option == "-c" and value.partition("=")[0].strip() in ("model_reasoning_effort", "service_tier"):
                continue
            policy.extend((option, value))
        current["codex_policy"] = policy
    arguments = _resume_arguments(current, claude_settings=ClaudeResumeSettings(model=settings.model))
    if settings.kind == "claude":
        arguments.extend(("--effort", settings.effort))
    return arguments


def locked(session: str) -> AbstractContextManager[None]:
    return pane_launch.locked(record_path(session))


def without_legacy_identity(entry: PaneLaunch) -> PaneLaunch:
    """Remove copied staff identity from a retained launch."""
    current = entry.copy()
    current["environment"] = legacy_identity.without_legacy_identity(entry["environment"])
    return current


def save(session: str, entries: dict[str, PaneLaunch], *,
         record: staff.StaffSnapshot | pane_launch.StaffSnapshotRead = pane_launch.StaffSnapshotRead.CURRENT) -> None:
    pane_launch.save(record_path(session), {pane: without_legacy_identity(entry) for pane, entry in entries.items()}, record=record)


def write(session: str, launch: PaneLaunch) -> None:
    """Delegate retained launch publication to the shared writer."""
    pane_launch.write(record_path(session), without_legacy_identity(launch), sources=tuple(record_directory().glob("*.json")))


def retained_environment(launch: PaneLaunch, history: Sequence[PaneLaunch]) -> dict[str, str]:
    """Keep native runtime labels and account settings from retained launches."""
    environment: dict[str, str] = {}
    for previous in sorted(history, key=lambda item: item["written_at"]):
        same_conversation = pane_launch.reference_id(previous) == pane_launch.reference_id(launch)
        if "session_id" in previous and "session_id" in launch:
            same_conversation = previous["session_id"] == launch["session_id"]
        if same_conversation:
            environment.update({key: value for key, value in previous["environment"].items()
                                if key in herdr_types.NATIVE_ID_MARKS or _needed(key)})
    environment.update(launch["environment"])
    current = launch.copy()
    current["environment"] = environment
    return without_legacy_identity(current)["environment"]


def matching_launch(place: herdr.HerdrPlace, session_id: str) -> PaneLaunch | None:
    """A pane's retained launch is evidence only for that exact conversation."""
    entries = read(place.session)
    retained = pane_launch.matching_launch(entries, place, session_id)
    if retained is None:
        return None
    from scripts.production import staff

    owner = staff.initial_snapshot().by_current_session(session_id)
    if owner is None:
        return retained
    return pane_launch.current_launch(retained, staff.initial_snapshot())


def end(session: str, pane_id: str, session_id: str, *, record: "staff.StaffSnapshot | None" = None) -> None:
    """Mark the pane's entry ended when it is still this conversation's."""
    _set_ended(session, pane_id, session_id, True, record)


def _set_ended(session: str, pane_id: str, session_id: str, ended: bool,
               record: "staff.StaffSnapshot | None") -> None:
    from scripts.production import staff

    record = staff.initial_snapshot() if record is None else record
    owner = record.by_current_session(session_id)
    stale = record.by_id(staff.StaffId(session_id))
    if owner is None and stale is not None:
        return
    reference = owner.id if owner is not None else session_id
    with locked(session):
        entries = read(session)
        entry = entries.get(pane_id)
        if entry is None or pane_launch.reference_id(entry) not in (reference, session_id):
            if not ended:
                raise ValueError(f"{session_id}: retained launch changed before resume publication")
            return
        if entry.get("ended", False) != ended:
            entry["ended"] = ended
            save(session, entries)


def forget_unit(unit_id: str) -> list[str]:
    """Drop every entry of a unit's director, in each session's record; a rename relaunches it in a new pane."""
    from scripts.production import staff

    record = staff.snapshot()
    owner = record.by_member_reference(unit_id)
    if owner is None or owner.kind != "unit":
        print(f"{unit_id}: no unit staff row; launch records retained", file=sys.stderr)
        return []
    dropped: list[str] = []
    for path in sorted(record_directory().glob("*.json")):
        for pane, entry in pane_launch.read_strict(path).items():
            if record.by_member_reference(pane_launch.reference_id(entry)) == owner:
                pane_launch.retire_launch(path, pane, entry)
                dropped.append(f"{path.stem}:{pane}")
    return dropped


def _needed(name: str) -> bool:
    """An environment name a record reads; the value of any other name is dropped as it is read."""
    return name in {"CLAUDE_CONFIG_DIR", "CODEX_HOME"} or name in KEPT_NAMES


def _environment(parts: Sequence[bytes]) -> dict[str, str]:
    pairs = (os.fsdecode(part).partition("=") for part in parts if b"=" in part)
    return {key: value for key, _, value in pairs if _needed(key)}


def _mac_record(pid: int) -> tuple[list[str], dict[str, str]]:
    # Imported here: the Linux hook never needs the account module.
    from scripts.whoami.account import kern_procargs2, procargs2_parts

    parts = procargs2_parts(kern_procargs2(pid), _needed)
    return list(parts.arguments), parts.environment


def _process_stat(process: Path) -> list[str]:
    return (process / "stat").read_text().rsplit(")", 1)[1].split()


def process_parent(pid: int) -> int:
    """Read the OS parent; callers decide how unavailable evidence is handled."""
    if (os.environ.get("HERDR_PLATFORM") or sys.platform) == "darwin":
        result = subprocess.run([os.environ.get("HERDR_PS", "ps"), "-o", "ppid=", "-p", str(pid)],
                                capture_output=True, text=True, check=True, timeout=5)
        return int(result.stdout)
    return int(_process_stat(Path(os.environ.get("HERDR_PROC") or PROC) / str(pid))[1])


def pane_shell_pid(place: herdr.HerdrPlace) -> int:
    from scripts.herdr import herdr

    info = herdr.json_object(herdr.Herdr(place.session).result(
        "pane", "process-info", "--pane", place.pane).get("process_info"), "pane process info")
    shell = info.get("shell_pid")
    if isinstance(shell, bool) or not isinstance(shell, int) or shell <= 0:
        raise ValueError("pane process info: expected a positive shell_pid")
    return shell


def _arguments(pid: int) -> list[str]:
    if sys.platform == "darwin":
        return _mac_record(pid)[0]
    raw = (PROC / str(pid) / "cmdline").read_bytes()
    return [os.fsdecode(part) for part in raw.split(b"\0") if part]


def process_arguments(pid: int) -> list[str]:
    """Read arguments for an exact process without selecting an ancestor."""
    return _arguments(pid)


def claude_arguments(pid: int) -> list[str]:
    """The argv of the nearest Claude process above `pid`; empty when none is found."""
    from scripts.herdr import herdr

    for _ in range(ANCESTOR_LIMIT):
        try:
            pid = process_parent(pid)
            arguments = _arguments(pid)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return []
        if herdr.is_claude_process(arguments):
            return arguments
        if pid <= 1:
            return []
    return []


def _process_environment(pid: int) -> dict[str, str]:
    """The needed part of `pid`'s environment."""
    if sys.platform == "darwin":
        return _mac_record(pid)[1]
    return _environment((PROC / str(pid) / "environ").read_bytes().split(b"\0"))


def running_launch(record: sessions.SessionRecord, place: herdr.HerdrPlace, cwd: str) -> pane_launch.CurrentLaunch:
    """Capture one verified live Claude using the existing argv and environment readers."""
    from scripts.herdr import herdr

    if not sessions.running_session(record):
        raise ValueError(f"{record['sessionId']}: Claude is no longer running")
    environment = _process_environment(record["pid"])
    arguments = _arguments(record["pid"])
    if not herdr.is_claude_process(arguments):
        raise ValueError(f"{record['sessionId']}: process is not Claude")
    entry = launch(place.pane, record["sessionId"], cwd, environment, arguments)
    entry["name"] = record["name"]
    entry["pid"] = record["pid"]
    started = sessions.process_start(record["pid"])
    if not started:
        raise ValueError(f"{record['sessionId']}: process start is unavailable")
    entry["proc_start"] = started
    return entry


def _account_local_conversation(config_dir: str, pid: int) -> str:
    """Read the account-local registry entry for the proved Claude PID."""
    value = cast(object, json.loads((Path(config_dir) / "sessions" / f"{pid}.json").read_text(encoding="utf-8")))
    identity = cast(dict[str, object], value).get("sessionId") if isinstance(value, dict) else None
    if not isinstance(identity, str) or not identity:
        raise ValueError(f"no conversation id for {pid}")
    return identity


def seed(session: str, *, select: Callable[[herdr.PaneIdentity], bool] | None = None) -> int:
    """Record Claude and Codex panes; name each pane whose conversation cannot be read."""
    from scripts.herdr import herdr

    client = herdr.Herdr(session)
    for pane in client.panes():
        pane_id = pane["pane_id"]
        if select is not None and not select(pane):
            continue
        context = f"herdr {session}:{pane_id} process info"
        info = herdr.json_object(client.result("pane", "process-info", "--pane", pane_id).get("process_info"), context)
        owner = herdr.foreground_pane_owner(info)
        if isinstance(owner, herdr.NoForegroundAgent):
            continue
        if isinstance(owner, herdr.UnprovedForegroundOwner):
            print(f"{pane_id}: {owner.reason}")
            continue
        if isinstance(owner, herdr.ProvedClaudeForeground):
            pid = owner.pid
            started = sessions.process_start(pid)
            if not started:
                print(f"{pane_id}: process start is unavailable for {pid}")
                continue
            try:
                environment = _process_environment(pid)
                config_dir = environment.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
                session_id = _account_local_conversation(config_dir, pid)
            except (OSError, ValueError):
                print(f"{pane_id}: no conversation id for {pid}")
                continue
            if sessions.process_start(pid) != started:
                print(f"{pane_id}: process start changed for {pid}")
                continue
        else:
            try:
                native = herdr.read_foreground_pane_owner(owner, pane)
                session_id = native.current_identity
                started, environment = native.started, native.environment
            except (OSError, ValueError, herdr_types.HerdrFailure):
                print(f"{pane_id}: no conversation id for {owner.pid}")
                continue
        cwd = owner.cwd if isinstance(owner.cwd, str) else ""
        entry = launch(pane_id, session_id, cwd, environment, owner.arguments, kind=owner.kind)
        entry.update(pid=owner.pid, proc_start=started)
        write(session, entry)
        print(f"{pane_id}: {pane.get('label') or ''} {session_id}")
    return 0


if __name__ == "__main__":
    from scripts.herdr import herdr

    parser = argparse.ArgumentParser(description="Record the Claude and Codex panes running now.")
    _ = parser.add_argument("command", choices=["seed"])
    _ = parser.add_argument("--session", default="")
    options = parser.parse_args()
    sys.exit(seed(cast(str, options.session) or herdr.shared_session() or "default"))
