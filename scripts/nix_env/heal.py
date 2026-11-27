#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Restart idle Claude sessions that missed their worktree's nix environment.

The sweep reserves each restart in the journal before shutdown.
It reports failed recovery to the owning showrunner.
If the showrunner cannot receive the alert, the sweep sends Nate a push.
Use retry after a repair, or escalate if the repair fails.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
from fnmatch import fnmatchcase
import math
import os
import shlex
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Generator, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
from scripts.lib import atomic_file, conf, nix_env
from scripts.herdr import herdr, pane_launch
from scripts.production import staff
from scripts.message import send as message_send, send_result, session_records, session_send, sessions
from scripts.message import send_budget
if TYPE_CHECKING:
    from scripts.notify import push_texts, review
else:
    from scripts.notify import push_texts
    from scripts.notify import review


def state_directory() -> Path:
    return Path(conf.read("nix_env").text("heal_state_directory")).expanduser()


@dataclass(frozen=True)
class HealLimits:
    restart_seconds: float
    relaunch_seconds: float
    alert_seconds: float

    @classmethod
    def read(cls) -> HealLimits:
        settings = conf.read("nix_env")
        return cls(settings.number("restart_interval_seconds"),
                   2 * settings.number("command_timeout_seconds")
                   + settings.number("relaunch_launch_seconds") + settings.number("relaunch_confirmation_seconds"),
                   settings.number("alert_timeout_seconds"))


@dataclass(frozen=True)
class HealSession:
    session_id: str
    pid: int
    proc_start: str
    name: str
    cwd: Path
    status: str
    config_dir: Path


@dataclass(frozen=True)
class PendingSession:
    session_id: str
    name: str
    cwd: Path


@dataclass(frozen=True)
class Healthy:
    pass


@dataclass(frozen=True)
class Broken:
    pass


@dataclass(frozen=True)
class Unavailable:
    reason: str


@dataclass(frozen=True)
class Gone:
    pass


class NoPendingRestart(TypedDict):
    kind: Literal["none"]


class PendingRestart(TypedDict):
    kind: Literal["stopping", "launching"]
    login: str
    name: str
    cwd: str
    config_dir: NotRequired[str]
    pid: int
    proc_start: str
    stop_request_sent: bool


@dataclass(frozen=True)
class StopComplete:
    """The exact process has exited."""


@dataclass(frozen=True)
class StopPending:
    """The original process still runs."""


@dataclass(frozen=True)
class StopRefused:
    reason: str


class HealHistory(TypedDict):
    restart_at: float
    restart_pid: int
    restart_proc_start: str
    healthy_since_restart: bool
    notified: bool
    action: str
    reason: str
    alert_reason: str
    showrunner_alert_key: str
    pending: NoPendingRestart | PendingRestart


class HealLog(TypedDict):
    time: str
    machine: str
    session_id: str
    name: str
    cwd: str
    action: str
    reason: str


@dataclass(frozen=True)
class HealSessionRead:
    sessions: tuple[HealSession, ...]
    unreadable: tuple[Path, ...] = ()


def registry_sessions() -> HealSessionRead:
    """Decode the configured registry and any other discovered registries."""
    configured = sessions.sessions_dir()
    directories = {Path.home()}
    unreadable: list[Path] = []

    def recursive_directories(root: Path) -> Iterator[Path]:
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = directory / entry.name
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(path)
                        except OSError:
                            unreadable.append(path)
            except NotADirectoryError:
                continue
            except OSError:
                unreadable.append(directory)
                continue
            yield directory

    pattern = conf.read("nix_env").text("session_directory_glob")
    for part in Path(pattern).parts:
        if part == "**":
            directories = {child for root in directories for child in recursive_directories(root)}
            continue
        matches: set[Path] = set()
        wildcard = any(character in part for character in "*?[")
        for directory in directories:
            if wildcard:
                try:
                    matches.update(child for child in directory.iterdir() if fnmatchcase(child.name, part))
                except OSError:
                    unreadable.append(directory)
                continue
            path = directory / part
            try:
                _ = path.stat()
                matches.add(path)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                unreadable.append(path)
        directories = matches

    found: dict[tuple[str, int, str], HealSession] = {}
    for directory in sorted({configured, *directories}):
        registry = session_records.read_registry(directory=directory)
        unreadable.extend(registry.unreadable)
        for observation in registry.entries:
            record, details = observation.record, observation.details
            if sessions.headless(record):
                continue
            cwd, status = details.get("cwd"), details.get("status")
            if (not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute()
                    or not isinstance(status, str) or not record["sessionId"]):
                continue
            started = record.get("procStart", "")
            if not started:
                continue
            found[(record["sessionId"], record["pid"], started)] = HealSession(record["sessionId"], record["pid"], started,
                record["name"], Path(cwd), status, directory.parent)
    return HealSessionRead(tuple(found.values()), tuple(dict.fromkeys(unreadable)))


def list_sessions() -> HealSessionRead:
    """List confirmed live processes and keep unreadable registry paths."""
    reading = registry_sessions()
    return HealSessionRead(
        tuple(item for item in reading.sessions if sessions.process_start(item.pid) == item.proc_start),
        reading.unreadable,
    )


def find_session(session_id: str) -> HealSession | Gone | Unavailable:
    reading = list_sessions()
    for item in reading.sessions:
        if item.session_id == session_id:
            return item
    if reading.unreadable:
        return Unavailable(session_records.registry_unreadable_reason(reading.unreadable))
    return Gone()


def inspect(session: HealSession) -> Healthy | Broken | Unavailable | Gone:
    """Only a confirmed process with readable environment can be broken."""
    try:
        if nix_env.nearest_envrc(session.cwd) is None:
            return Healthy()
        if not session.proc_start or sessions.process_start(session.pid) != session.proc_start:
            return Gone()
        if sys.platform == "darwin":
            from scripts.whoami import account

            parts = account.procargs2_parts(account.kern_procargs2(session.pid), lambda name: name == "IN_NIX_SHELL")
            loaded = bool(parts.environment.get("IN_NIX_SHELL"))
        else:
            process = Path(f"/proc/{session.pid}")
            if process.stat().st_uid != os.getuid():
                return Unavailable("process belongs to another user")
            environment = (process / "environ").read_bytes().split(b"\0")
            loaded = any(value.startswith(b"IN_NIX_SHELL=") and value != b"IN_NIX_SHELL=" for value in environment)
        if sessions.process_start(session.pid) != session.proc_start:
            return Gone()
        return Healthy() if loaded else Broken()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return Unavailable(str(error))


def broken(session: HealSession) -> bool:
    return isinstance(inspect(session), Broken)


def hold_reason() -> str:
    """Return a local hold or unreadable-state reason, without reading hold files."""
    try:
        from scripts.build_targets import targets

        local_platform = "macos" if sys.platform == "darwin" else "linux"
        matched = False
        for target in targets.read_targets():
            if target.platform != local_platform:
                continue
            matched = True
            state = targets.target_state(target)
            if isinstance(state, targets.Held):
                return f"build hold: {state.holder}: {state.reason}"
            if isinstance(state, targets.StateUnreadable):
                return f"build hold state unavailable: {state.reason}"
        return "" if matched else "build hold state unavailable: this machine has no configured target"
    except (OSError, ValueError, ImportError) as error:
        return f"build hold state unavailable: {error}"


def new_history() -> HealHistory:
    return HealHistory(restart_at=0.0, restart_pid=0, restart_proc_start="", healthy_since_restart=False,
                       notified=False, action="", reason="", alert_reason="", showrunner_alert_key="",
                       pending=NoPendingRestart(kind="none"))


def read_pending(value: object) -> NoPendingRestart | PendingRestart:
    if not isinstance(value, dict):
        raise ValueError("invalid nix-heal pending restart")
    fields = cast(dict[str, object], value)
    if fields.get("kind") == "none":
        return NoPendingRestart(kind="none")
    login, name, cwd = fields.get("login"), fields.get("name"), fields.get("cwd")
    config_dir, pid, proc_start = fields.get("config_dir"), fields.get("pid"), fields.get("proc_start")
    kind = fields.get("kind")
    stop_request_sent = fields.get("stop_request_sent", True)
    if (kind not in ("stopping", "launching") or not isinstance(login, str) or not login
            or not isinstance(name, str) or not isinstance(cwd, str) or not Path(cwd).is_absolute()
            or ("config_dir" in fields and (not isinstance(config_dir, str) or not Path(config_dir).is_absolute()))
            or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(proc_start, str) or not proc_start
            or not isinstance(stop_request_sent, bool)):
        raise ValueError("invalid nix-heal pending restart; refusing an unproven relaunch")
    pending = PendingRestart(kind="launching" if kind == "launching" else "stopping", login=login, name=name, cwd=cwd,
                             pid=pid, proc_start=proc_start, stop_request_sent=stop_request_sent)
    if isinstance(config_dir, str):
        pending["config_dir"] = config_dir
    return pending


def read_history(directory: Path) -> dict[str, HealHistory]:
    try:
        value = cast(object, json.loads((directory / "state.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("nix-heal state is not an object; refusing to lose the restart guard")
    histories: dict[str, HealHistory] = {}
    for key, raw in cast(dict[str, object], value).items():
        if not isinstance(raw, dict):
            raise ValueError("invalid nix-heal session state")
        fields = cast(dict[str, object], raw)
        restarted, pid = fields.get("restart_at"), fields.get("restart_pid")
        start, notified = fields.get("restart_proc_start"), fields.get("notified")
        healthy = fields.get("healthy_since_restart")
        action, reason = fields.get("action"), fields.get("reason")
        alert_reason = fields.get("alert_reason", "")
        showrunner_alert_key = fields.get("showrunner_alert_key", "")
        if (not isinstance(restarted, (int, float)) or isinstance(restarted, bool) or not math.isfinite(restarted)
                or not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(start, str)
                or not isinstance(healthy, bool) or not isinstance(notified, bool)
                or not isinstance(action, str) or not isinstance(reason, str) or not isinstance(alert_reason, str)
                or not isinstance(showrunner_alert_key, str)):
            raise ValueError("invalid nix-heal session state; refusing to lose the restart guard")
        histories[key] = HealHistory(restart_at=float(restarted), restart_pid=pid,
            restart_proc_start=start, healthy_since_restart=healthy, notified=notified, action=action, reason=reason,
            alert_reason=alert_reason, showrunner_alert_key=showrunner_alert_key,
            pending=read_pending(fields.get("pending", {"kind": "none"})))
    return histories


def save_history(directory: Path, histories: dict[str, HealHistory]) -> None:
    atomic_file.write_text_atomic(directory / "state.json", json.dumps(histories))


@contextmanager
def state_lock(directory: Path) -> Generator[bool]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "state.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def log(directory: Path, session: HealSession | PendingSession, history: HealHistory, action: str, reason: str) -> None:
    """Retain every restart and refusal; suppress unchanged skip reasons."""
    if action in {"skip", "pending"} and history["action"] == action and history["reason"] == reason:
        return
    entry = HealLog(time=datetime.now(timezone.utc).isoformat(), machine=socket.gethostname(),
        session_id=session.session_id, name=session.name, cwd=str(session.cwd), action=action, reason=reason)
    with (directory / "log.jsonl").open("a", encoding="utf-8") as stream:
        _ = stream.write(json.dumps(entry) + "\n")
    history["action"], history["reason"] = action, reason


NO_RESTART_REASON = "session has no nix shell; the healer does not restart sessions"


def alert_cause(reason: str) -> str:
    if reason == NO_RESTART_REASON:
        return "The healer does not restart sessions. A restart of the session loads its nix shell."
    if reason.endswith(".envrc loaded but no nix shell (IN_NIX_SHELL unset)"):
        return "The automatic restart could not fix it. The .envrc file loaded, but no nix shell started."
    if reason == "session still has no nix shell after its heal restart":
        return "An automatic restart did not fix it."
    return "The automatic restart could not fix it. The nix shell setup refused to load."


def heal_command(command: str, session_id: str, *options: str) -> str:
    return shlex.join([str(SCRIPTS / "lib/py"), str(Path(__file__).resolve()), command, "--only", session_id, *options])


def send_alert(command: list[str], limits: HealLimits) -> tuple[send_result.OutcomeHeader | None, str]:
    try:
        timeout = max(limits.alert_seconds, send_budget.user_seconds(review.user_send_seconds()))
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return None, str(error)
    outcome = send_result.send_outcome(result.returncode, result.stdout)
    detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
    return (outcome.header if isinstance(outcome, send_result.ConfirmedSendOutcome) else None), detail


def once_accepted(header: send_result.OutcomeHeader) -> bool:
    """A --once-per-key send that the sender holds durably: it never delivers the key again."""
    if header.outcome == "sent":
        return True
    if header.outcome in ("kept", "queued"):
        return header.detail.startswith(message_send.DURABLE_ACCEPTANCE)
    return header.outcome == "skipped" and message_send.is_once_accepted_skip(header.detail)


def send_push(directory: Path, limits: HealLimits, session: HealSession | PendingSession, history: HealHistory,
              reason: str) -> tuple[bool, str]:
    push = push_texts.render("nix_heal.failed", session=session.name,
                             machine=socket.gethostname(), cwd=str(session.cwd), cause=alert_cause(reason))
    command = push_texts.user_send_argv(push, key=f"nix-heal-{session.session_id}")
    command.extend(["--clears-when", heal_command("sweep", session.session_id, "--check")])
    header, detail = send_alert(command, limits)
    if header is not None and header.outcome in {"sent", "skipped", "replaced", "queued", "kept"}:
        history["notified"] = True
        return True, header.outcome.upper()
    log(directory, session, history, "push_failed", detail)
    return False, " ".join(detail.split())


def showrunner_owner(session_id: str) -> staff.StaffRow | None:
    record = staff.snapshot()
    member = record.by_member_reference(session_id)
    if member is None:
        return None
    visited = {member.id}
    parent = record.parent_of(member)
    while parent is not None and parent.id not in visited:
        if parent.kind == "showrunner":
            return parent
        visited.add(parent.id)
        parent = record.parent_of(parent)
    return None


def alert(directory: Path, limits: HealLimits, session: HealSession, history: HealHistory, reason: str, *,
          histories: dict[str, HealHistory]) -> None:
    if history["notified"]:
        return
    history["alert_reason"] = reason
    prefix = f"nix-heal-showrunner-{session.session_id}-"
    new_key = not history["showrunner_alert_key"].startswith(prefix)
    if new_key:
        history["showrunner_alert_key"] = prefix + uuid.uuid4().hex
    try:
        owner = showrunner_owner(session.session_id)
    except (OSError, ValueError, staff.StaffRefused, staff.StaffNotFound) as error:
        fallback = f"Could not read the staff record. {error}"
    else:
        fallback = "The session has no owning showrunner."
        if owner is not None:
            machine = socket.gethostname()
            escalate = heal_command("escalate", session.session_id)
            if reason == NO_RESTART_REASON:
                instructions = (
                    "2. Restart the session after the repair. After that, the healer sends no more alerts for it.\n"
                    f"3. Run this command on {machine} to send Nate a push if you cannot repair the setup or restart the session.\n"
                    f"   {escalate}"
                )
            elif history["pending"]["kind"] == "none":
                instructions = (
                    f"2. Run this command on {machine} after the repair.\n"
                    f"   {heal_command('retry', session.session_id)}\n"
                    f"3. Run this command on {machine} to send Nate a push if you cannot repair the setup.\n"
                    f"   {escalate}"
                )
            else:
                instructions = (
                    "Retry refuses while a restart is pending.\n"
                    f"2. Run this command on {machine} after the repair or if you cannot repair the setup.\n"
                    f"   {escalate}\n"
                    "Nate gets a push to restart the session."
                )
            text = (
                f"The nix shell for {session.name} did not load on {machine}.\n"
                f"The worktree is {session.cwd}.\n"
                f"{alert_cause(reason)}\n"
                f"The healer recorded this reason.\n{reason}\n\n"
                "1. Repair the nix setup for this worktree. Get Nate's approval before you change /etc/nixos.\n"
                f"{instructions}"
            )
            if new_key:
                # Save the new episode key before the send: a sweep stopped after the
                # send resends with the same key, which the sender answers SKIPPED.
                try:
                    save_history(directory, histories)
                except OSError as error:
                    print(f"nix-heal: {session.session_id}: cannot save the alert key; sending anyway. {error}",
                          file=sys.stderr)
            command = session_send.send_argv("--to", str(owner.id), "--from", "nix-heal",
                                             "--key", history["showrunner_alert_key"], "--once-per-key", "--text", text)
            header, detail = send_alert(command, limits)
            if header is not None and once_accepted(header):
                history["notified"] = True
                log(directory, session, history, "showrunner_alerted", owner.name)
                return
            if header is not None and header.outcome == "retry":
                # An earlier sweep's send still holds the key; the next sweep asks again.
                log(directory, session, history, "pending", f"the alert to {owner.name} is still in delivery")
                return
            fallback = f"Could not confirm the alert to {owner.name}. {detail}"
    print(f"nix-heal: {session.session_id}: {fallback}", file=sys.stderr)
    _ = send_push(directory, limits, session, history, reason)


def stop_completion(session: HealSession, login: str) -> StopComplete | StopPending | StopRefused:
    del login
    observed = sessions.process_liveness(session.pid, session.proc_start)
    if isinstance(observed, sessions.SessionLivenessUnknown):
        return StopRefused(observed.reason)
    return StopPending() if isinstance(observed, sessions.LiveSessionProcess) else StopComplete()


def confirm_relaunch(directory: Path, session: HealSession, history: HealHistory,
                     replacement: HealSession | Gone) -> bool:
    """Confirm a live, healthy replacement before clearing the pending restart."""
    if (isinstance(replacement, HealSession) and (replacement.pid, replacement.proc_start) != (session.pid, session.proc_start)
            and replacement.cwd.resolve() == session.cwd.resolve()
            and replacement.config_dir.resolve() == session.config_dir.resolve()
            and isinstance(inspect(replacement), Healthy)):
        history["pending"] = NoPendingRestart(kind="none")
        history.update(healthy_since_restart=True, notified=False, showrunner_alert_key="")
        log(directory, session, history, "restart", "nix shell missing; stopped session relaunched")
        return True
    return False


def request_stop(directory: Path, limits: HealLimits, session: HealSession, pending: PendingRestart, history: HealHistory,
                 histories: dict[str, HealHistory]) -> bool:
    from scripts.agents import stop_resume

    result = stop_resume.stop(session.session_id)
    if not isinstance(result, (stop_resume.Stopped, stop_resume.NotRunning)):
        reason = result.reason if isinstance(result, stop_resume.StopFailed) else "no local session tab"
        log(directory, session, history, "refused", reason)
        alert(directory, limits, session, history, reason, histories=histories)
        return False
    pending["stop_request_sent"] = True
    save_history(directory, histories)
    return True


def resume_pending(directory: Path, limits: HealLimits, session: HealSession, pending: PendingRestart, history: HealHistory,
                   histories: dict[str, HealHistory]) -> None:
    if pending["kind"] == "launching":
        replacement = find_session(session.session_id)
        if isinstance(replacement, Unavailable):
            log(directory, session, history, "pending", replacement.reason)
            return
        if not confirm_relaunch(directory, session, history, replacement):
            still_broken = isinstance(replacement, HealSession) and isinstance(inspect(replacement), Broken)
            reason = ("session still has no nix shell after its heal restart" if still_broken else
                      "waiting for a live replacement with a nix shell; relaunch will not be repeated")
            log(directory, session, history, "pending", reason)
            if still_broken or time.time() - history["restart_at"] >= limits.relaunch_seconds:
                alert(directory, limits, session, history, reason, histories=histories)
        return
    held = hold_reason()
    if held:
        log(directory, session, history, "pending", held)
        return
    completion = stop_completion(session, pending["login"])
    if isinstance(completion, StopPending):
        if not pending["stop_request_sent"]:
            if sessions.process_start(session.pid) != session.proc_start:
                history["pending"] = NoPendingRestart(kind="none")
                reason = "unsent shutdown request lost its original session process; restart cancelled"
                log(directory, session, history, "refused", reason)
                alert(directory, limits, session, history, reason, histories=histories)
                return
            if not request_stop(directory, limits, session, pending, history, histories):
                return
        log(directory, session, history, "pending", "waiting for shutdown stop completion")
        return
    if isinstance(completion, StopRefused):
        log(directory, session, history, "refused", completion.reason)
        alert(directory, limits, session, history, completion.reason, histories=histories)
        return
    loaded = nix_env.ensure(session.cwd)
    if isinstance(loaded, nix_env.Refused):
        log(directory, session, history, "refused", loaded.reason)
        alert(directory, limits, session, history, loaded.reason, histories=histories)
        return
    held = hold_reason()
    if held:
        log(directory, session, history, "pending", held)
        return
    from scripts.agents import stop_resume

    with ExitStack() as start:
        try:
            start.enter_context(herdr.reboot_lock())
        except herdr.RebootLockBusy as error:
            log(directory, session, history, "pending", str(error))
            return
        # Save the launch reservation before the command can start a session.
        pending["kind"] = "launching"
        history["restart_at"] = time.time()
        save_history(directory, histories)
        try:
            result = stop_resume.resume(session.session_id, why="nix shell heal")
        except (OSError, subprocess.TimeoutExpired) as error:
            log(directory, session, history, "refused", str(error))
            alert(directory, limits, session, history, str(error), histories=histories)
            return
    if not isinstance(result, stop_resume.ClaudeUp):
        reason = (result.reason
                  if isinstance(result, stop_resume.ResumeFailed)
                  else "no local session tab")
        log(directory, session, history, "refused", reason)
        alert(directory, limits, session, history, reason, histories=histories)
        return
    replacement = find_session(session.session_id)
    if isinstance(replacement, Unavailable):
        log(directory, session, history, "pending", replacement.reason)
    elif not confirm_relaunch(directory, session, history, replacement):
        log(directory, session, history, "pending", "restart command completed; waiting for a live replacement with a nix shell")


def heal_one(directory: Path, limits: HealLimits, session: HealSession, history: HealHistory, histories: dict[str, HealHistory]) -> None:
    observation = inspect(session)
    if isinstance(observation, (Healthy, Gone)):
        if isinstance(observation, Healthy):
            history.update(healthy_since_restart=True, notified=False, showrunner_alert_key="")
        return
    if isinstance(observation, Unavailable):
        log(directory, session, history, "skip", observation.reason)
        return
    held = hold_reason()
    if held:
        log(directory, session, history, "skip", held)
        return
    if session.status not in sessions.INPUT_STATUSES:
        log(directory, session, history, "skip", "session is not idle")
        return
    now = time.time()
    interval = limits.restart_seconds
    if history["restart_at"] and not history["healthy_since_restart"]:
        replaced = (session.pid, session.proc_start) != (history["restart_pid"], history["restart_proc_start"])
        if replaced or now - history["restart_at"] >= interval:
            reason = "session still has no nix shell after its heal restart"
            log(directory, session, history, "refused", reason)
            alert(directory, limits, session, history, reason, histories=histories)
            return
        log(directory, session, history, "skip", f"heal restart already requested within {interval / 60:g} minutes")
        return
    if history["restart_at"] and now - history["restart_at"] < interval:
        log(directory, session, history, "skip", f"heal restart already requested within {interval / 60:g} minutes")
        return
    loaded = nix_env.ensure(session.cwd)
    if isinstance(loaded, nix_env.Refused):
        log(directory, session, history, "refused", loaded.reason)
        alert(directory, limits, session, history, loaded.reason, histories=histories)
        return
    # Loading can take minutes. Recheck both coordination and identity before
    # shutdown gets authority to stop a process.
    held = hold_reason()
    if held:
        log(directory, session, history, "skip", held)
        return
    fresh = find_session(session.session_id)
    if isinstance(fresh, Unavailable):
        log(directory, session, history, "skip", fresh.reason)
        return
    if (isinstance(fresh, Gone) or fresh.pid != session.pid or fresh.proc_start != session.proc_start
            or fresh.status not in sessions.INPUT_STATUSES or not broken(fresh)):
        log(directory, session, history, "skip", "session changed during environment load")
        return
    from scripts.whoami import account

    selected = account.claude_account(session.config_dir)
    if selected is None:
        reason = "cannot identify the session's Claude account"
        log(directory, session, history, "refused", reason)
        alert(directory, limits, session, history, reason, histories=histories)
        return
    held = hold_reason()
    if held:
        log(directory, session, history, "skip", held)
        return
    history["restart_at"], history["restart_pid"] = time.time(), session.pid
    history["restart_proc_start"] = session.proc_start
    history["healthy_since_restart"] = False
    pending = PendingRestart(kind="stopping", login=selected.login, name=session.name, cwd=str(session.cwd),
                             pid=session.pid, proc_start=session.proc_start,
                             stop_request_sent=False)
    if staff.initial_snapshot().by_member_reference(session.session_id) is None:
        pending["config_dir"] = str(session.config_dir)
    history["pending"] = pending
    save_history(directory, histories)
    if not request_stop(directory, limits, session, pending, history, histories):
        return
    resume_pending(directory, limits, session, pending, history, histories)


def report_one(directory: Path, limits: HealLimits, session: HealSession, history: HealHistory,
               histories: dict[str, HealHistory]) -> None:
    """Alert the owning showrunner once about a broken session; never stop or start it."""
    observation = inspect(session)
    if isinstance(observation, Healthy):
        history.update(healthy_since_restart=True, notified=False, showrunner_alert_key="")
        return
    if isinstance(observation, Gone):
        return
    if isinstance(observation, Unavailable):
        log(directory, session, history, "skip", observation.reason)
        return
    if history["notified"]:
        return
    log(directory, session, history, "refused", NO_RESTART_REASON)
    alert(directory, limits, session, history, NO_RESTART_REASON, histories=histories)


def sweep(only: str = "", check: bool = False, no_restart: bool = False) -> int:
    """Keep one bad record or launch from preventing other sessions' recovery."""
    try:
        reading = list_sessions()
        selected = [item for item in reading.sessions if not only or item.session_id == only]
        if reading.unreadable:
            print(f"nix-heal: {session_records.registry_unreadable_reason(reading.unreadable)}", file=sys.stderr)
            if check:
                return 1
        if check:
            # Absence is not healthy proof: a registry write may be in progress
            # during restart. Keep the push until a live replacement proves it.
            if only and not selected:
                return 1
            return 0 if all(isinstance(inspect(item), Healthy) for item in selected) else 1
        directory = state_directory()
        limits = HealLimits.read()
        needs_attention = [item for item in selected if not isinstance(inspect(item), (Healthy, Gone))]
        if not needs_attention and not (directory / "state.json").exists():
            return 0
        with state_lock(directory) as locked:
            if not locked:
                return 0
            histories = read_history(directory)
            pending_ids: set[str] = set()
            # A stopped session has left the registry. Recover its saved request
            # before considering any new live session, including after a hold.
            for session_id, history in histories.items():
                pending = history["pending"]
                if no_restart or pending["kind"] == "none" or (only and only != session_id):
                    continue
                pending_ids.add(session_id)
                home = pane_launch.config_home({**pending, "session_id": session_id})
                if isinstance(home, staff.HomeUnknown):
                    from scripts.accounts import accounts

                    print(f"anomaly: {session_id}: session home is unknown; select the normal account", file=sys.stderr)
                    selected_account = accounts.role_account("unit")
                    home = staff.SessionHome(selected_account.dir)
                session = HealSession(session_id, pending["pid"], pending["proc_start"], pending["name"],
                                      Path(pending["cwd"]), "idle", home.path)
                try:
                    resume_pending(directory, limits, session, pending, history, histories)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    log(directory, session, history, "refused", str(error))
                finally:
                    save_history(directory, histories)
            for session in selected:
                if session.session_id in pending_ids:
                    continue
                if session not in needs_attention and session.session_id not in histories:
                    continue
                history = histories.setdefault(session.session_id, new_history())
                try:
                    if no_restart:
                        report_one(directory, limits, session, history, histories)
                    else:
                        heal_one(directory, limits, session, history, histories)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    log(directory, session, history, "refused", str(error))
                finally:
                    save_history(directory, histories)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"nix-heal: {error}", file=sys.stderr)
        return 1 if check else 0


def update_recovery(command: str, session_id: str) -> int:
    try:
        directory = state_directory()
        with state_lock(directory) as locked:
            if not locked:
                print(f"The nix-heal state is busy. Run {command} again after the sweep ends.")
                return 1
            histories = read_history(directory)
            history = histories.get(session_id)
            if command == "retry":
                if history is not None:
                    if history["pending"]["kind"] != "none":
                        print(f"A restart is pending for {session_id}. Run {heal_command('escalate', session_id)} to send Nate a push.")
                        return 1
                    history.update(restart_at=0.0, restart_pid=0, restart_proc_start="",
                                   healthy_since_restart=False, notified=False, showrunner_alert_key="")
                    save_history(directory, histories)
                print(f"The next sweep can retry {session_id}.")
                return 0
            session = find_session(session_id)
            if isinstance(session, Unavailable):
                print(f"Could not read the live session. {' '.join(session.reason.split())}")
                return 1
            pending = history["pending"] if history is not None else NoPendingRestart(kind="none")
            if isinstance(session, Gone):
                if pending["kind"] == "none":
                    print(f"No live session matches {session_id}.")
                    return 1
                target = PendingSession(session_id, pending["name"], Path(pending["cwd"]))
            else:
                target = session
            if history is None or not history["alert_reason"]:
                print(f"No failure reason is saved for {session_id}.")
                return 1
            confirmed, outcome = send_push(directory, HealLimits.read(), target, history, history["alert_reason"])
            save_history(directory, histories)
            print(f"{outcome} push for {session_id}." if confirmed else f"Push failed. {outcome}")
            return 0 if confirmed else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"{command} failed. {' '.join(str(error).split())}")
        return 1


class HealArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = ""
        self.only: str = ""
        self.check: bool = False
        self.no_restart: bool = False


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("sweep", "retry", "escalate"))
    _ = parser.add_argument("--only", default="")
    _ = parser.add_argument("--check", action="store_true")
    _ = parser.add_argument("--no-restart", action="store_true",
                            help="alert the owning showrunner about a broken session; never stop or start it")
    parsed = parser.parse_args(arguments, namespace=HealArguments())
    if parsed.command == "sweep":
        return sweep(parsed.only, parsed.check, parsed.no_restart)
    if not parsed.only or parsed.check or parsed.no_restart:
        parser.error(f"{parsed.command} requires --only and does not accept --check or --no-restart")
    return update_recovery(parsed.command, parsed.only)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
