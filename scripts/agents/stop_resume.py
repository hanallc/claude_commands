"""Stop a session, or resume it on its account in its herdr tab."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import math
import os
import argparse
import json
import select
import shlex
import signal
import sys
import subprocess
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Collection, Generator, Mapping
from contextlib import ExitStack, contextmanager, redirect_stdout
from contextvars import ContextVar, copy_context
from io import StringIO, TextIOWrapper
from importlib import import_module
from hashlib import sha256
from functools import cached_property, partial
from threading import Event, Lock
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Never, Protocol, cast, final, overload, override
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]

from scripts.herdr import herdr
from scripts.herdr import herdr_types
from scripts.accounts import accounts, moves
from scripts.production.staff import STAFF_ROOT
from scripts.whoami import account as account_reader
from scripts.whoami import agent_accounts, note_file
from scripts.lib import agent_program, conf, models_registry, nix_env, operation_deadline
from scripts.lib.probe_session import PROBE_ENV
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.script_envelope import script_prompt
from scripts.lib.transcripts import open_transcript, transcript_paths
from scripts.message import session_records, session_send
from scripts.agents import member_account, pane_record, producer_creation, remote, shutdown_record
from scripts.agents.launch_names import LaunchNames, LaunchNamesNotRead
from scripts.agents.codex_trust import trust_arguments
from scripts.herdr import pane_launch
from scripts.production import staff
from scripts.lib.resume_message import (
    AccountMove, NO_ACCOUNT_MOVE, BusyPass, NO_OWN_PASS, WorkersHeld, launcher_prompt, worker_mode,
    ResumeWorkers as ResumeWorkers,
    NO_WORKERS as NO_WORKERS,
    resume_message as resume_message,
    resume_worker_command as resume_worker_command,
)
from scripts.agents.stop_results import (
    Stopped as Stopped, NotRunning as NotRunning, StopFailed as StopFailed,
    SESSION_WITHOUT_STAFF_ROW, SessionWithoutStaffRow,
    StopUnproved as StopUnproved,
    ClaudeUp as ClaudeUp, ClaudeStartState, ResumePending as ResumePending, MoveHandedOff as MoveHandedOff, ResumeFailed as ResumeFailed,
    SessionNotFound as SessionNotFound, MoveResult as MoveResult, claude_up_line as claude_up_line,
    ResumeResult as ResumeResult, StopResult, SourceRelease, SourceReleased, SourceUnresolved, SourceReleaseUnknown,
)
from scripts.message import sessions

from scripts.agents.stop_results import shutdown_state_root

if TYPE_CHECKING:
    from scripts.agents.restart import StopReadArguments
    from scripts.herdr import worker_panes
    from scripts.notify.push_texts import RenderedPush
    from scripts.production import unit_lookup
    from scripts.production.staff import StaffRow, StaffSnapshot, ClaudeBackend, CodexBackend, BackendUnavailable, StaffId


@dataclass(frozen=True)
class LiveAccountUnproved(ResumeFailed):
    """A live process has no readable account evidence."""


class RowStarter(Protocol):
    def start_row(self, row: StaffRow, record: StaffSnapshot, client: herdr.Herdr, *,
                  target_account: str | accounts.AccountSelection,
                  configuration: models_registry.LaunchModelConfiguration | None = None) -> str: ...
    def session_launch(self, identity: str, name: str) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]: ...
    def producer_resume(self, row: StaffRow, entry: pane_record.PaneLaunch, *,
                        configuration: models_registry.LaunchModelConfiguration | None = None
                        ) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]: ...
    def producer_arguments(self, name: str, settings: models_registry.LaunchSettings,
                           configuration: models_registry.LaunchModelConfiguration) -> list[str]: ...


@dataclass(frozen=True)
class ProducerFirstStart:
    """A configured producer that needs a fresh native conversation."""

    name: str
    folder: Path


@dataclass(frozen=True)
class PendingProducerOpening:
    """A creation request still needs proof of its opening prompt."""

    path: Path
    folder: Path
    since: datetime
    creation: dict[str, object]

    def confirm(self, identity: str) -> None:
        producer_creation.write_record(self.path, {**self.creation, "opening_confirmed": identity})


@dataclass(frozen=True)
class ConfirmedProducerOpening:
    identity: str


@dataclass(frozen=True)
class ProducerOpeningUnconfirmed:
    """The pane has no saved proof of the opening prompt."""


@dataclass(frozen=True)
class PendingProducerAdmission:
    """A reserved pane whose native conversation still needs staff admission."""

    pane: str
    terminal: str
    kind: Literal["claude", "codex"]
    home: Path
    opening: ConfirmedProducerOpening | ProducerOpeningUnconfirmed = field(default_factory=ProducerOpeningUnconfirmed)


@dataclass(frozen=True)
class ProducerFamilyChange(ProducerFirstStart):
    """The old conversation and its pane before a fresh producer start."""

    session: StaffId
    kind: Literal["claude", "codex"]
    client: herdr.Herdr
    pane: herdr.PaneIdentity | herdr.NoPane
    configuration: models_registry.LaunchModelConfiguration


def _producer_admission_path(name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("producer name must be one folder name")
    return pane_record.record_directory() / f".producer-admission-{name}"


def producer_admission_record(name: str) -> Path:
    """Locate the validated producer reservation for cleanup."""
    return _producer_admission_path(name)


def _pending_producer_opening(name: str, saved: dict[str, object]) -> PendingProducerOpening | herdr.NoOpeningPrompt:
    path = producer_creation.record_path(name)
    if not saved or isinstance(saved.get("opening_confirmed"), str):
        return herdr.NoOpeningPrompt()
    folder = Path(herdr.json_text(saved.get("folder"), "producer folder"))
    stamp = saved.get("opening_since")
    since = datetime.fromisoformat(stamp) if isinstance(stamp, str) else datetime.min.replace(tzinfo=timezone.utc)
    return PendingProducerOpening(path, folder, since, saved)


def producer_folder(name: str) -> Path:
    root = conf.read_or_repository("staff").text("PRODUCER_FOLDER_ROOT")
    return Path(root).expanduser() / name


def _producer_start_request(row: StaffRow) -> ProducerFirstStart:
    folder = row.worktree.path if isinstance(row.worktree, staff.PresentWorktree) else producer_folder(row.name)
    return ProducerFirstStart(row.name, folder)


def _reserve_producer_pane(request: ProducerFirstStart, kind: Literal["claude", "codex"], home: Path,
                           client: herdr.Herdr) -> PendingProducerAdmission:
    workspace = conf.read_or_repository("staff").text("PRODUCER_WORKSPACE")
    environment = account_reader.named_account_environment(home) if kind == "claude" else {"CODEX_HOME": str(home)}
    prepared = herdr.prepare_agent_environment(str(request.folder), environment)
    if isinstance(prepared, nix_env.Refused):
        raise OSError(str(prepared))
    pane = client.open_tab(workspace, request.name, str(request.folder), prepared.env)
    row = client.pane_row(pane)
    if isinstance(row, herdr.NoPane):
        raise ValueError("reserved producer pane is unavailable")
    return PendingProducerAdmission(row["pane_id"], herdr.json_text(row.get("terminal_id"), "producer terminal"),
                                    kind, home)


def _write_producer_reservation(request: ProducerFirstStart, pending: PendingProducerAdmission) -> None:
    write_text_atomic(_producer_admission_path(request.name), json.dumps({
        "pane": pending.pane, "terminal": pending.terminal, "kind": pending.kind,
        "home": str(pending.home), "folder": str(request.folder),
        **({"opening_confirmed": pending.opening.identity} if isinstance(pending.opening, ConfirmedProducerOpening) else {})}))


def _save_producer_reservation(request: ProducerFirstStart, pending: PendingProducerAdmission,
                               client: herdr.Herdr) -> None:
    try:
        _write_producer_reservation(request, pending)
    except OSError:
        _ = client.close_tab_confirmed(pending.pane, terminal=pending.terminal)
        raise


def producer_conflicts(name: str, client: herdr.Herdr) -> list[tuple[str, str]]:
    """Find producer conversations through their current native identities."""
    from scripts.message import codex_daemon

    panes = client.panes()
    membership = staff.initial_snapshot()
    member = next((row for row in membership.find(kind="producer") if row.name == name), None)
    entries = pane_record.read(client.session)
    references = pane_launch.named_references(name, membership, entries)
    evidence = pane_launch.NativePaneEvidence(panes, client.scan_row_panes(membership, [dict(pane) for pane in panes]))
    conflicts: list[tuple[str, str]] = []
    for reference in references:
        if isinstance(reference, pane_launch.StaffLaunchReference):
            owner = membership.by_id(reference.id)
            if owner is None or owner.kind != "producer":
                continue
            identity = str(owner.current_session)
        else:
            identity = reference.session
        for pane in pane_launch.reference_panes(reference, membership, client, entries, evidence=evidence):
            if pane_launch.native_sessions(client, pane):
                conflicts.append((identity, str(pane["pane_id"])))
    registry = session_records.enumerate_running_sessions()
    if isinstance(registry, session_records.RunningSessionDiscoveryFailed):
        raise ValueError(registry.reason)
    if registry.unreadable:
        raise ValueError(session_records.registry_unreadable_reason(registry.unreadable))
    named: list[tuple[str, tuple[int, ...]]] = [
        (row["sessionId"], (row["pid"],)) for row in registry.records
        if member is not None and row["sessionId"] == member.current_session]
    if member is not None:
        codex = herdr.codex_conversations()
        if member.current_session in codex.live:
            named.append((member.current_session, codex.live[member.current_session]))
    for daemon in codex_daemon.read_daemons(os.environ):
        if isinstance(daemon.sessions, codex_daemon.DaemonUnreadable):
            raise ValueError(f"producer thread registry is unreadable: {daemon.sessions.reason}")
        named.extend((row.thread_id, ()) for row in daemon.sessions
                     if (member := membership.by_current_session(row.thread_id)) is not None
                     and member.kind == "producer" and member.name == name)
    for identity, pids in named:
        places = [pane["pane_id"] for pane in panes if pane["session_id"] == identity]
        if not places:
            for pid in pids:
                place = herdr.herdr_place_of(pid)
                if isinstance(place, herdr.HerdrPlace):
                    places.append(place.pane if place.session == client.session else f"{place.session}:{place.pane}")
                elif isinstance(place, herdr.UnprovedHerdrPlacement):
                    places.append(f"placement unknown for pid {pid}")
        conflicts.append((identity, ",".join(places) or "outside herdr"))
    locations: dict[str, set[str]] = {}
    unresolved: list[tuple[str, str]] = []
    for identity, pane in conflicts:
        if identity == "unidentified":
            unresolved.append((identity, pane))
        else:
            locations.setdefault(identity, set()).update(pane.split(","))
    for identity, panes_for_session in locations.items():
        panes_for_session.discard("outside herdr")
        unresolved.append((identity, ",".join(sorted(panes_for_session)) or "outside herdr"))
    return sorted(set(unresolved))


def producer_adoption_launch(identity: str, pane: str, client: herdr.Herdr) -> pane_launch.CurrentLaunch:
    """Prove a named native conversation before staff admission."""
    pane = pane.removeprefix(client.session + ":")
    if any(row["pane_id"] == pane for row in client.panes()):
        info = herdr.json_object(client.result("pane", "process-info", "--pane", pane).get("process_info"),
                                 "producer adoption process info")
        owner = herdr.foreground_pane_owner(info)
        if isinstance(owner, herdr.UnprovedForegroundOwner):
            raise ValueError(f"producer has no unique native foreground process: {owner.reason}")
        if isinstance(owner, herdr.NoForegroundAgent):
            raise ValueError("producer has no unique native foreground process")
        entry = client.producer_admission_launch(pane, owner.kind)
        if entry["session_id"] != identity:
            raise ValueError("producer pane runs another conversation")
        return entry
    from scripts.message import codex_daemon

    observed = herdr.read_conversation(identity)
    if isinstance(observed, herdr.LiveConversation) and len(observed.pids) > 1:
        raise ValueError("producer conversation has several native processes")
    if isinstance(observed, herdr.LiveConversation) and len(observed.pids) == 1:
        pid = observed.pids[0]
        native = herdr.native_process(pid, observed.kind)
        if native.current_identity != identity:
            raise ValueError("producer process runs another conversation")
        if observed.kind == "claude":
            registry = session_records.enumerate_running_sessions()
            if isinstance(registry, session_records.RunningSessionDiscoveryFailed):
                raise ValueError(registry.reason)
            if registry.unreadable:
                raise ValueError(session_records.registry_unreadable_reason(registry.unreadable))
            records = [row for row in registry.records if row["sessionId"] == identity]
            if len(records) != 1 or records[0]["pid"] != pid:
                raise ValueError("producer registry has no unique native process")
            folder = records[0].get("cwd", "")
        else:
            saved = _unrecorded_codex_launch(identity)
            if isinstance(saved, SessionNotFound):
                raise ValueError("producer thread folder is unavailable")
            folder = pane_record.resume_launch(saved)["cwd"]
        if not Path(folder).is_absolute():
            raise ValueError("producer folder is unavailable")
        entry = pane_record.launch("", identity, folder, native.environment, [observed.kind], kind=observed.kind)
        key = "CLAUDE_CONFIG_DIR" if observed.kind == "claude" else "CODEX_HOME"
        entry["config_dir"] = native.environment.get(key) or str(Path.home() / (".claude" if observed.kind == "claude" else ".codex"))
        entry.update(pid=pid, proc_start=native.started)
        place = herdr.herdr_place_of(pid)
        if isinstance(place, herdr.HerdrPlace):
            herdr.forget_pane_evidence(place.session, place.pane)
        if herdr.native_process(pid, observed.kind) != native:
            raise ValueError("producer process changed before adoption")
        return entry
    for daemon in codex_daemon.read_daemons(os.environ):
        if isinstance(daemon.sessions, codex_daemon.DaemonUnreadable):
            raise ValueError(daemon.sessions.reason)
        for thread in daemon.sessions:
            if thread.thread_id == identity and Path(thread.cwd).is_absolute():
                entry = pane_record.launch("", identity, thread.cwd, {"CODEX_HOME": str(daemon.owner.home)}, ["codex"], kind="codex")
                entry["config_dir"] = str(daemon.owner.home)
                return entry
    raise ValueError("producer has no proved live native conversation")


def adopt_named_producer(request: ProducerFirstStart, identity: str, pane: str, client: herdr.Herdr) -> ClaudeUp:
    """Admit one proved live conversation without a launch."""
    try:
        entry = producer_adoption_launch(identity, pane, client)
        native = staff.native_id(entry["session_id"])
        home = pane_launch.config_home(entry, record=staff.StaffSnapshot(()))
        if not isinstance(home, staff.SessionHome):
            raise ValueError("producer account is unavailable")
        folder = Path(entry["cwd"])
        kind = pane_launch.agent_kind(entry)
        entry["name"] = request.name
        if entry["pane_id"]:
            pane_record.write(client.session, entry)
        row = staff.ensure("producer", name=request.name, runtime_id=native,
            worktree=staff.PresentWorktree(remote.local_machine(), folder),
            doc=staff.LiveDoc(remote.local_machine(), request.folder))
        if entry["pane_id"]:
            expectation = pane_launch.ProducerLaunchRequest(row.id, native, kind, folder, home.path)
            with pane_launch.producer_launch_request(expectation):
                row = pane_launch.staff_admitted(row)
            client.remember_row_tab(str(row.id), entry["pane_id"])
        else:
            row = staff.set_home(row.id, home)
        _producer_admission_path(request.name).unlink(missing_ok=True)
        label = accounts.claude_config_label(home.path) if kind == "claude" else agent_accounts.codex_home_role_label(home.path)
        return ClaudeUp(str(row.current_session), entry["pane_id"], label, start_state=ClaudeStartState.ALREADY_RUNNING)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        raise ValueError(f"session {identity}, pane {pane}: producer adoption is unproved: {error}") from error


def _locked_first_producer_resume(request: ProducerFirstStart, *, to: str, why: str = "resumed by name", requester: str = "",
                                  resend_opening: bool = False,
                                  client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> ResumeResult:
    """Serialize producer admission and replacement."""
    path = _producer_admission_path(request.name)
    with staff.slot_operation("producer-first-" + request.name, request.name):
        try:
            saved = producer_creation.read_record(producer_creation.record_path(request.name))
            marker = producer_creation.require_record(request.name, saved)
            opening = _pending_producer_opening(request.name, saved)
        except (OSError, ValueError, staff.StaffRefused) as error:
            return ResumeFailed(request.name, str(error))
        if isinstance(opening, PendingProducerOpening):
            if marker is None:
                return ResumeFailed(request.name, f"creation has no shared marker. Run {producer_creation.retry(request.name)}.")
            if isinstance(request, ProducerFamilyChange):
                return ResumeFailed(request.name, "complete producer creation before changing its family")
            request = replace(request, folder=opening.folder)
        if resend_opening and (not isinstance(opening, PendingProducerOpening) or not path.is_file()):
            return ResumeFailed(request.name, "there is no reserved pane with a pending opening prompt")
        record = staff.initial_snapshot()
        row = next((row for row in record.rows if row.kind == "producer" and row.name == request.name), None)
        if row is None:
            return _first_producer_resume(request, to=to, client=client,
                                          opening=opening, marker=marker, resend_opening=resend_opening)
        with staff.row_operation(str(row.id)):
            if not path.exists() and isinstance(opening, herdr.NoOpeningPrompt) and (not isinstance(request, ProducerFamilyChange)
                                      or row.current_session != request.session):
                return _resume(str(row.id), to=to, why=why, requester=requester, client=client)
            return _first_producer_resume(request, to=to, client=client, member=row,
                                          opening=opening, marker=marker, resend_opening=resend_opening)


@final
@dataclass(frozen=True)
class UnprovedCodexRollout(herdr.TranscriptUnreadable):
    reason: str
    malformed: bool = False


@final
@dataclass
class CodexProducerPromptCursor(herdr.TranscriptCursor):
    """Read Codex user turns for the opening prompt check."""

    _malformed: UnprovedCodexRollout | None = field(default=None, init=False)

    @override
    def allows_resend(self) -> bool:
        return isinstance(self.readability, UnprovedCodexRollout) and self.readability.malformed

    @override
    def read_turns(self) -> list[tuple[datetime, dict[str, object]]]:
        from scripts.production import worker_admission

        self.readability = self._malformed or herdr.TranscriptReadable()
        try:
            source = worker_admission.codex_transcript_source(self.transcript.session_id, self.transcript.config_dir)
        except ValueError:
            return []
        except OSError:
            self.readability = herdr.TranscriptUnreadable()
            return []
        try:
            with open_transcript(source) as stream:
                size = stream.seek(0, os.SEEK_END)
                offset = self.offsets.get(source, 0)
                if size < self.sizes.get(source, 0):
                    offset = 0
                    _ = self.turns.pop(source, None)
                self.sizes[source] = size
                self.offsets[source] = offset
                turns = self.turns.setdefault(source, [])
                _ = stream.seek(offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        self.readability = UnprovedCodexRollout("the first turn is still being written")
                        break
                    self.offsets[source] = stream.tell()
                    try:
                        value = cast(object, json.loads(line))
                        if not isinstance(value, dict):
                            continue
                        row = cast(dict[str, object], value)
                    except ValueError:
                        self._malformed = UnprovedCodexRollout(
                            "a malformed Codex rollout line may contain the opening prompt", malformed=True)
                        self.readability = self._malformed
                        continue
                    stamp = row.get("timestamp")
                    try:
                        at = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if isinstance(stamp, str) else None
                    except ValueError:
                        at = None
                    if at is not None and at.tzinfo is not None and at <= self.since:
                        continue
                    role, _, text = worker_admission.transcript_entry(row, set()).partition("\n")
                    if role == "user:":
                        turns.append((at if at is not None and at.tzinfo is not None else self.since,
                                      {"type": "user", "content": text, "isMeta": row.get("isMeta", False)}))
                return turns
        except (OSError, ValueError, subprocess.SubprocessError):
            self.readability = herdr.TranscriptUnreadable()
            return []


def _confirm_producer_opening(request: ProducerFirstStart, opening: PendingProducerOpening,
                              pending: PendingProducerAdmission, identity: str, client: herdr.Herdr, *,
                              resend: bool) -> None:
    if isinstance(pending.opening, ProducerOpeningUnconfirmed):
        transcript = herdr.SessionTranscript(pending.home, identity)
        cursor_type = CodexProducerPromptCursor if pending.kind == "codex" else herdr.TranscriptCursor
        cursor = cursor_type(transcript, opening.since)
        text = f"Read ~/rust/hanadocs/{request.name}/README.md, then run /producer:produce."

        def reason(state: UnprovedCodexRollout) -> str:
            if state.malformed:
                recovery = producer_creation.recovery_command("resume", request.name, "--resend-opening-prompt")
                return f"{state.reason}. Check the pane, then run {recovery} if the prompt is absent."
            return state.reason

        proof = cursor.submitted_prompt(script_prompt(text))
        if not isinstance(proof, herdr.PromptSubmitted):
            if resend and not cursor.allows_resend():
                raise ValueError("a resend requires a malformed Codex rollout line")
            if not resend and isinstance(cursor.readability, UnprovedCodexRollout):
                raise OSError(reason(cursor.readability))
            proof = client.submit_opening_prompt(pending.pane, herdr.OpeningPrompt(
                text, transcript, cursor=cursor, resend_once=resend),
                opening.since, int(conf.read("shutdown").number("START_SECONDS") * 1000))
            if isinstance(proof, herdr.PromptNotSubmitted):
                state = cursor.readability
                raise OSError(reason(state) if isinstance(state, UnprovedCodexRollout) and not resend else proof.reason)
        _write_producer_reservation(request, replace(pending, opening=ConfirmedProducerOpening(identity)))
    opening.confirm(identity)


def _check_producer_admission_account(pending: PendingProducerAdmission, to: str) -> None:
    if to:
        selected = member_account.producer_account(pending.kind, to=to)
        requested_home = selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home
        if requested_home.resolve() != pending.home.resolve():
            raise ValueError("pending producer admission uses another account; complete its admission before an account move")


def _first_producer_resume(request: ProducerFirstStart, *, opening: PendingProducerOpening | herdr.NoOpeningPrompt, to: str,
                           marker: producer_creation.Marker | None, resend_opening: bool,
                           client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW,
                           member: StaffRow | SessionWithoutStaffRow = SESSION_WITHOUT_STAFF_ROW) -> ResumeResult:
    """Retain the pane before launch and admit only its proved native identity."""
    began = monotonic()
    try:
        if not (request.folder / "README.md").is_file():
            if marker is not None:
                raise ValueError(f"producer README is unavailable. Run {producer_creation.retry(request.name)}.")
            raise ValueError(f"producer README is unavailable: {request.folder / 'README.md'}")
        starter = cast(RowStarter, cast(object, import_module("scripts.agents.restart")))
        configuration = (request.configuration if isinstance(request, ProducerFamilyChange)
                         else models_registry.launch_snapshot())
        settings = models_registry.launch_settings("producers", request.name, configuration=configuration)
        kind = settings.kind
        client = herdr.Herdr(herdr.shared_session()) if isinstance(client, herdr.LaunchClientSource) else client
        path = _producer_admission_path(request.name)
        recovering_admission = path.exists()
        if isinstance(request, ProducerFamilyChange) and not recovering_admission:
            _ = member_account.producer_account(kind, to=to)
            _stop_producer_for_family_change(request)
        conflicts = producer_conflicts(request.name, client)
        if len(conflicts) > 1:
            raise ValueError("producer sessions conflict: " + "; ".join(
                f"session {identity}, pane {pane}" for identity, pane in conflicts))
        if recovering_admission:
            saved = herdr.json_object(cast(object, json.loads(path.read_text(encoding="utf-8"))), "pending producer admission")
            if saved.get("kind") != kind or saved.get("folder") != str(request.folder):
                raise ValueError("pending producer settings differ; retain its pane and inspect the admission record")
            confirmed = saved.get("opening_confirmed")
            pending = PendingProducerAdmission(herdr.json_text(saved.get("pane"), "producer pane"),
                herdr.json_text(saved.get("terminal"), "producer terminal"), kind,
                Path(herdr.json_text(saved.get("home"), "producer account")),
                ConfirmedProducerOpening(confirmed) if isinstance(confirmed, str) else ProducerOpeningUnconfirmed())
            _check_producer_admission_account(pending, to)
            if conflicts:
                retained = pane_record.read_strict(client.session).get(pending.pane)
                if isinstance(opening, PendingProducerOpening) or isinstance(pending.opening, ConfirmedProducerOpening):
                    if conflicts[0][1] != pending.pane:
                        raise ValueError("the live producer is outside its reserved pane")
                elif retained is None or pane_record.resume_launch(retained)["session_id"] != conflicts[0][0]:
                    return adopt_named_producer(request, *conflicts[0], client)
        elif conflicts:
            identity, pane = conflicts[0]
            if isinstance(request, ProducerFamilyChange):
                raise ValueError(f"another live producer session remains: {identity}, pane {pane}")
            if isinstance(opening, herdr.NoOpeningPrompt):
                return adopt_named_producer(request, identity, pane, client)
            if not isinstance(member, staff.StaffRow) or identity != member.current_session:
                raise ValueError(f"another live producer session remains: {identity}, pane {pane}")
            entry = producer_adoption_launch(identity, pane, client)
            home = pane_launch.config_home(entry, record=staff.StaffSnapshot(()))
            pane_row = client.pane_row(pane)
            if not isinstance(home, staff.SessionHome) or isinstance(pane_row, herdr.NoPane):
                raise ValueError("cannot find the admitted producer's pane and account")
            pending = PendingProducerAdmission(pane, herdr.json_text(pane_row.get("terminal_id"), "producer terminal"),
                                              kind, home.path)
            _check_producer_admission_account(pending, to)
            _write_producer_reservation(request, pending)
        else:
            selected = member_account.producer_account(kind, to=to)
            home = selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home
            pending = _reserve_producer_pane(request, kind, home, client)
            _save_producer_reservation(request, pending, client)
        pane_row = client.pane_row(pending.pane)
        if isinstance(pane_row, herdr.NoPane) or pane_row.get("terminal_id") != pending.terminal:
            if resend_opening:
                raise ValueError("the reserved pane changed. Run agents resume without the resend option.")
            lost = pending
            retained = pane_record.read_strict(client.session).get(lost.pane)
            if retained is not None and not isinstance(herdr.read_conversation(pane_record.resume_launch(retained)["session_id"], kind), herdr.StoppedConversation):
                raise ValueError("retained producer conversation is not proved stopped; retain its admission record")
            pending = _reserve_producer_pane(request, kind, lost.home, client)
            pending = replace(pending, opening=lost.opening)
            if retained is not None:
                replacement = retained.copy()
                replacement["pane_id"] = pending.pane
                _ = replacement.pop("pid", None)
                _ = replacement.pop("proc_start", None)
                with pane_launch.locked(pane_record.record_path(client.session)):
                    entries = pane_record.read_strict(client.session)
                    current = entries.get(pending.pane)
                    if (current is not None and pane_record.resume_launch(current)["session_id"]
                            != pane_record.resume_launch(retained)["session_id"]):
                        raise ValueError("replacement producer pane holds another conversation; retain its launch record")
                    entries[pending.pane] = replacement
                    pane_launch.save(pane_record.record_path(client.session), entries)
            _save_producer_reservation(request, pending, client)
            if retained is not None:
                pane_launch.retire_launch(pane_record.record_path(client.session), lost.pane, retained)
        starting = isinstance(client.pane_activity(pending.pane), herdr.PaneAtShell)
        if resend_opening and starting:
            raise ValueError("the reserved pane is at a shell. Run agents resume without the resend option.")
        if starting:
            retained = pane_record.read_strict(client.session).get(pending.pane)
            if retained is not None:
                arguments = pane_record.configured_resume_arguments(retained, pane_record.resume_launch(retained)["session_id"], request.name,
                    settings, configuration=configuration)
                live = herdr.read_conversation(pane_record.resume_launch(retained)["session_id"], kind)
                if not isinstance(live, herdr.StoppedConversation):
                    raise ValueError("retained producer conversation is not proved stopped; retain its admission record")
            else:
                arguments = starter.producer_arguments(request.name, settings, configuration)
                if isinstance(member, staff.StaffRow) and isinstance(opening, PendingProducerOpening):
                    arguments[:0] = ["resume" if kind == "codex" else "--resume", str(member.current_session)]
                    if not isinstance(herdr.read_conversation(member.current_session, kind), herdr.StoppedConversation):
                        raise ValueError("the admitted producer conversation is not proved stopped")
            shell_setup = ":"
            if kind == "claude":
                if recovering_admission:
                    if not to and isinstance(opening, herdr.NoOpeningPrompt) and isinstance(pending.opening, ProducerOpeningUnconfirmed):
                        selected = member_account.producer_account(kind)
                        assert isinstance(selected, accounts.NamedAccount)
                        if selected.dir.resolve() != pending.home.resolve():
                            pending = replace(pending, home=selected.dir)
                            _write_producer_reservation(request, pending)
                    # A retry can follow an interruption between the write and the shell update.
                    exports = shlex.join([f"{key}={value}" for key, value in account_reader.named_account_environment(pending.home).items()])
                    shell_setup = f"export {exports}"
                herdr.trust_claude_launch(request.folder, pending.home)
                arguments = models_registry.claude_arguments(arguments,
                    configuration.environment(herdr.agent_base_environment(
                        account_reader.named_account_environment(pending.home), herdr.NO_ENVIRONMENT_REMOVALS)))
            client.run_in_pane(pending.pane, ["eval", herdr.agent_alias_script(kind) + shell_setup], terminal=pending.terminal)
            outcome = herdr.start_agent(client.session, pending.pane, kind,
                trust_arguments(kind, str(request.folder), arguments), herdr.NoOpeningPrompt(),
                int(conf.read("shutdown").number("START_SECONDS") * 1000), agent=request.name)
            if isinstance(outcome, herdr.PromptNotSubmitted):
                raise OSError(outcome.reason)
        entry = client.producer_admission_launch(pending.pane, kind)
        home = pane_launch.config_home(entry, record=staff.StaffSnapshot(()))
        if (not isinstance(home, staff.SessionHome) or home.path.resolve() != pending.home.resolve()
                or Path(entry["cwd"]).resolve() != request.folder.resolve()):
            raise ValueError("producer process differs from its reserved folder or account")
        identity = staff.native_id(entry["session_id"])
        if isinstance(pending.opening, ConfirmedProducerOpening) and identity != pending.opening.identity:
            raise ValueError("the opening prompt was confirmed for another conversation")
        if (isinstance(opening, PendingProducerOpening) and isinstance(member, staff.StaffRow)
                and identity != member.current_session):
            raise ValueError("the opening prompt belongs to another conversation")
        entry["name"] = request.name
        pane_record.write(client.session, entry)
        if isinstance(opening, PendingProducerOpening):
            _confirm_producer_opening(request, opening, pending, identity, client, resend=resend_opening)
        folder = staff.PresentWorktree(remote.local_machine(), request.folder)
        document: staff.LiveDoc | staff.WrappedDoc = staff.LiveDoc(folder.machine, request.folder)
        if isinstance(member, staff.StaffRow):
            if isinstance(member.worktree, staff.PresentWorktree):
                folder = member.worktree
            if not isinstance(member.doc, staff.DocUnfilled):
                document = member.doc
        row = staff.ensure("producer", name=request.name, runtime_id=identity, worktree=folder, doc=document)
        expectation = pane_launch.ProducerLaunchRequest(row.id, identity, kind, request.folder, pending.home)
        with (pane_launch.producer_launch_request(expectation),
              pane_launch.producer_publication_configuration(configuration)):
            row = pane_launch.staff_admitted(row)
        publication = pane_record.read_strict(client.session).get(pending.pane)
        if publication is None or not publication.get("producer_binding", False):
            raise ValueError("producer admission needs pane publication; retain its native conversation")
        client.remember_row_tab(str(row.id), pending.pane)
        if marker is not None:
            producer_creation.finish(request.name, marker)
        path.unlink(missing_ok=True)
        label = (accounts.claude_config_label(pending.home) if kind == "claude"
                 else agent_accounts.codex_home_role_label(pending.home))
        return ClaudeUp(str(row.current_session), pending.pane, label,
                        elapsed_seconds=monotonic() - began, start_state=ClaudeStartState.STARTED)
    except (OSError, ValueError, staff.StaffRefused, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        return ResumeFailed(request.name, str(error))


def _stop_producer_for_family_change(change: ProducerFamilyChange) -> None:
    with pane_launch.producer_publication_configuration(change.configuration):
        result = stop(change.session, change.kind)
    if isinstance(result, StopFailed):
        raise ValueError(result.reason)
    if isinstance(result, SessionNotFound):
        raise ValueError(f"{change.session}: the old producer session is unavailable")
    if isinstance(change.pane, herdr.NoPane):
        return
    pane = change.pane["pane_id"]
    pane_record.end(change.client.session, pane, change.session)
    terminal = herdr.json_text(change.pane.get("terminal_id"), "old producer terminal")
    if not terminal or not change.client.close_tab_confirmed(pane, terminal=terminal):
        raise ValueError(f"{change.session}: the old producer pane {pane} did not close")


@dataclass(frozen=True)
class ProducerFamilyUnavailable:
    """The observed session and its saved binding do not supply a family."""


def _producer_current_kind(row: StaffRow, entry: pane_record.PaneLaunch | None
                           ) -> Literal["claude", "codex"] | ProducerFamilyUnavailable:
    try:
        return pane_launch.current_kind(row)
    except ValueError:
        if entry is None or not entry.get("producer_binding", False):
            return ProducerFamilyUnavailable()
        # A Codex launch records its policy even when it has no policy arguments.
        return "codex" if "codex_policy" in entry else "claude"



_UNREAD_NAMES = LaunchNamesNotRead()
_resume_names: ContextVar[LaunchNames | LaunchNamesNotRead] = ContextVar("resume_names", default=_UNREAD_NAMES)
_NO_SOURCE_ACCOUNTS: Mapping[str, str] = MappingProxyType({})
_detached_moves: ContextVar[bool] = ContextVar("detached_moves", default=False)
_move_resume: ContextVar[bool] = ContextVar("move_resume", default=False)
_batch_process: ContextVar[bool] = ContextVar("batch_process", default=False)


@dataclass(frozen=True)
class BuildQueueUnreadable:
    reason: str


@dataclass(frozen=True)
class BuildMoveDeferred(StopFailed):
    """The director remains running until build ownership permits its automatic move."""


@dataclass(frozen=True)
class ResolvedMoveMember:
    owner: pane_launch.StaffLaunchReference | pane_launch.NativeLaunchReference
    name: str

    @property
    def reference(self) -> str:
        return self.owner.id if isinstance(self.owner, pane_launch.StaffLaunchReference) else self.owner.session

    @property
    def row_key(self) -> str:
        return self.owner.id if isinstance(self.owner, pane_launch.StaffLaunchReference) else moves.native_row_key(self.owner.session)

    def staff_row(self, record: StaffSnapshot) -> StaffRow | None:
        return record.by_id(self.owner.id) if isinstance(self.owner, pane_launch.StaffLaunchReference) else None


@dataclass
class LockedMoveRow:
    session: str
    member: ResolvedMoveMember
    identity: str
    lock: ExitStack
    marked_target: str = ""


@dataclass(frozen=True)
class LiveMoveSource:
    """The process that supplied the source account for the move."""

    session: ClaudeUp
    pid: int
    started: str


@dataclass(frozen=True)
class StoppedMoveSource:
    """The source read found no live process."""


@dataclass(frozen=True)
class MoveStopTarget:
    """The locked row identifies the conversation the stop may act on."""

    member: ResolvedMoveMember
    identity: str
    source: LiveMoveSource | StoppedMoveSource


@dataclass(frozen=True)
class MoveRequestNotRecorded:
    """There is no journal request for this stop."""


class MoveRequestRefused(ValueError):
    """The stop cannot proceed because it could not record a request."""


@dataclass
class MoveStopAttempt:
    target: MoveStopTarget
    build_request: Callable[[], moves.RequestedMove]
    request: moves.RequestedMove | MoveRequestNotRecorded = field(default_factory=MoveRequestNotRecorded)

    @cached_property
    def _prepared_request(self) -> moves.RequestedMove:
        return self.build_request()

    def record_request(self) -> None:
        try:
            request = self._prepared_request
            moves.append_move(request)
            self.request = request
        except Exception as error:
            raise MoveRequestRefused(f"could not record the move request: {error}") from error


class StopTargetAtUse:
    """The stop must resolve its own target."""


_move_stop_target: ContextVar[MoveStopAttempt | StopTargetAtUse] = ContextVar(
    "move_stop_target", default=StopTargetAtUse())


@dataclass(frozen=True)
class DetachedMoveRequest:
    request_id: str
    staff_members: frozenset[str]


class NewMoveRequest:
    """The caller requests a new account move."""


class LegacyMoveReport:
    """A legacy report supplies display data only."""


@dataclass
class MoveBatch:
    members: dict[str, ResolvedMoveMember]
    intent: moves.FollowGroupAccount | moves.ExplicitMoveTarget
    origin: NewMoveRequest | DetachedMoveRequest | moves.PendingMoveRequest
    target: str
    request_id: str = field(default_factory=lambda: uuid4().hex)
    results: dict[str, MoveResult] = field(default_factory=dict)
    native_sessions: dict[str, str] = field(default_factory=dict)


_NEW_MOVE_REQUEST = NewMoveRequest()
_move_batch: ContextVar[MoveBatch] = ContextVar("move_batch")


@contextmanager
def detached_moves() -> Generator[None]:
    """Keep move batches outside the maintenance part's kill boundary."""
    token = _detached_moves.set(True)
    try:
        yield
    finally:
        _detached_moves.reset(token)


def _tab_of(session: str, *, names: LaunchNames | LaunchNamesNotRead = _UNREAD_NAMES) -> herdr.TabSession | SessionNotFound:
    """Find an existing pane and retain its saved conversation when the herdr id is stale."""
    client = herdr.Herdr(herdr.shared_session())
    entries = pane_record.read(client.session)
    membership = (LaunchNames.read() if isinstance(names, LaunchNamesNotRead) else names).tab_record
    references = pane_launch.named_references(session, membership, entries)
    if not references:
        try:
            _ = staff.native_id(session)
        except staff.StaffRefused:
            return SessionNotFound(session)
        references.add(pane_launch.NativeLaunchReference(session))
    tabs: dict[str, herdr.TabSession] = {}
    for reference in references:
        owner = membership.by_id(reference.id) if isinstance(reference, pane_launch.StaffLaunchReference) else None
        identity = str(owner.current_session) if owner is not None else (
            reference.session if isinstance(reference, pane_launch.NativeLaunchReference) else "")
        for pane in pane_launch.reference_panes(reference, membership, client, entries):
            pane_id = str(pane["pane_id"])
            tabs[pane_id] = herdr.TabSession(pane_id, str(pane.get("label") or ""),
                str(pane.get("workspace_id", "")), identity)
    if len(tabs) > 1:
        raise ValueError(f"{session}: several tabs match")
    return next(iter(tabs.values())) if tabs else SessionNotFound(session)


def _same_process(pid: int, started: str) -> bool:
    """Check the saved process and start time after registry removal."""
    identity = sessions.process_liveness(pid, started)
    if isinstance(identity, sessions.SessionLivenessUnknown):
        raise OSError(identity.reason)
    return isinstance(identity, sessions.LiveSessionProcess)


def _signal_process(pid: int, number: int) -> None:
    """Refuse proof signals unless herdr places the process in the selected workspace."""
    if number != 0 and "SHUTDOWN_PROOF_WORKSPACE" not in os.environ and (
            "STAFF_FILE" in os.environ or "SHUTDOWN_STATE_DIR" in os.environ):
        raise PermissionError(
            f"signal {number} to pid {pid} refused: isolated records require SHUTDOWN_PROOF_WORKSPACE")
    if number != 0 and "SHUTDOWN_PROOF_WORKSPACE" in os.environ:
        workspace = os.environ["SHUTDOWN_PROOF_WORKSPACE"]
        refusal = f"proof workspace {workspace!s}: signal {number} to pid {pid} refused"
        try:
            if not workspace or pid <= 0:
                raise ValueError("expected a workspace id and a positive pid")
            client = herdr.Herdr(herdr.shared_session())
            for pane in client.panes():
                if pane["workspace_id"] != workspace:
                    continue
                info = herdr.json_object(client.result(
                    "pane", "process-info", "--pane", pane["pane_id"]).get("process_info"), "proof process info")
                if any(isinstance(observed := herdr.json_object(process, "proof pane process").get("pid"), int)
                       and not isinstance(observed, bool) and observed == pid
                       for process in herdr.json_items(info.get("foreground_processes"), "proof pane processes")):
                    break
            else:
                raise ValueError("herdr did not place the pid in a workspace pane")
        except (OSError, ValueError, herdr_types.HerdrFailure) as error:
            raise PermissionError(f"{refusal}: {error}") from error
    if number in (signal.SIGTERM, signal.SIGKILL):
        operation_deadline.check()
    os.kill(pid, number)


_launch_note_output = Lock()


def _record_launches(selected: list[str]) -> None:
    """Seed only selected panes while their Claude still exposes its launch flags."""
    if not selected:
        return
    try:
        client = herdr.Herdr(herdr.shared_session())
        entries = pane_record.read(client.session)
        record = staff.initial_snapshot()
        references = {reference for name in selected for reference in pane_launch.named_references(name, record, entries)}
        identities = set(selected)
        for reference in references:
            if isinstance(reference, pane_launch.StaffLaunchReference):
                owner = record.by_id(reference.id)
                if owner is not None:
                    identities.add(str(owner.current_session))
            else:
                identities.add(reference.session)
        locations = {pane for pane, entry in entries.items()
                     if pane_launch.launch_reference(entry, record) in references}
        retained_references = {pane_launch.launch_reference(entry, record) for entry in entries.values()}
        locations.update(str(pane["pane_id"]) for reference in references - retained_references
            for pane in pane_launch.reference_panes(reference, record, client, entries))
        def selected_pane(pane: herdr.PaneIdentity) -> bool:
            return pane["pane_id"] in locations or pane["session_id"] in identities
        with _launch_note_output, redirect_stdout(StringIO()):
            _ = pane_record.seed(client.session, select=selected_pane)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        print(f"stop: launch note anomaly: {error}", file=sys.stderr)

def release_source(row: StaffRow, backend: ClaudeBackend | CodexBackend | BackendUnavailable) -> SourceRelease:
    """Resolve native stop evidence before creating the destination."""
    kind = "claude" if isinstance(backend, staff.ClaudeBackend) else "codex" if isinstance(backend, staff.CodexBackend) else ""
    result = stop(row.id, kind)
    if isinstance(result, SessionNotFound):
        return SourceUnresolved(f"{row.id}: lifecycle could not resolve the staff reference; check the staff row and retry")
    if isinstance(result, StopFailed):
        return SourceReleaseUnknown(f"{result.reason}; inspect the source session and retry")
    return SourceReleased()


def stop(session: str, kind: str = "", *, deadline: float = math.inf,
         codex: herdr.CodexConversations | None = None) -> StopResult:
    try:
        with operation_deadline.within(deadline), staff.row_operation(session):
            shared: StopReadArguments = {"codex": codex} if codex is not None else {}
            return _stop_recorded(session, kind, **shared)
    except TimeoutError as error:
        reason = str(error)
        return StopFailed(session, reason if "deadline passed" in reason else f"deadline passed; {reason}")


def _stop_staff_row(record: StaffSnapshot, reference: str) -> StaffRow | None:
    row = record.by_member_reference(reference)
    if row is not None:
        return row
    matches = [item for item in record.rows if item.name == reference]
    return matches[0] if len(matches) == 1 else None


def stop_producer(identity: StaffId, why: str, *, deadline: float,
                  save_exit_after_stop: bool = False) -> StopResult:
    """Protect an intentional producer stop with a hold under the row lock."""
    with staff.row_operation(identity):
        row = staff.initial_snapshot().rows_by_id.get(identity)
        if row is None or row.kind != "producer":
            return SessionNotFound(identity)
        machine = member_machine(row)
        if machine and machine != remote.local_machine() and machine != remote.this_machine():
            return StopFailed(str(row.current_session), f"session is on {machine}")
        new_hold = not isinstance(row.exited, staff.Exited)
        if new_hold:
            if save_exit_after_stop:
                _ = staff.pause(identity, why)
            else:
                _ = staff.exit(identity, why)
        result = stop(identity, deadline=deadline)
        if isinstance(result, (Stopped, NotRunning)) and result.session_id != row.current_session:
            result = StopFailed(str(row.current_session), "the producer conversation changed before the stop")
        if isinstance(result, (Stopped, NotRunning)):
            shutdown_record.update(started=[], pending=[identity])
            if new_hold and save_exit_after_stop:
                _ = staff.exit(identity, why)
        elif new_hold:
            _ = staff.set_hold(identity, row.paused)
        return result


def _stop_recorded(session: str, kind: str = "", *,
                   codex: herdr.CodexConversations | None = None) -> StopResult:
    """Stop on purpose; retain its launch for an explicit resume, not crash recovery."""
    from scripts.herdr import worker_panes
    from scripts.production import session_staff

    try:
        record = staff.initial_snapshot()
    except (OSError, ValueError):
        record = staff.StaffSnapshot(())
    row = _stop_staff_row(record, session)
    before = None
    if row is not None and row.kind == "worker":
        worker = session_staff.worker_backend(record, row.id)
        if isinstance(worker, session_staff.RecordedWorker):
            kind = worker.backend.family
        if isinstance(row.parent, staff.ReportsTo):
            before = next((item for item in worker_panes.observe(row.parent.id, record) if item.row.id == row.id), None)
    if isinstance(before, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
        return StopFailed(str(before.row.current_session), before.reason)
    if isinstance(before, worker_panes.StoppedWorker) and isinstance(
            _worker_stopped(before, kind, codex=codex), NotRunning):
        result: StopResult = NotRunning(str(before.row.current_session))
    else:
        _record_launches([session])
        result = _stop(session, kind, **({"codex": codex} if codex is not None else {}))
        after = (None if codex is None else herdr.CodexConversations(
            {identity: pids for identity, pids in codex.live.items() if identity != result.session_id},
            codex.unknown)) if isinstance(result, (Stopped, NotRunning)) else codex
        if isinstance(before, (worker_panes.OpenWorker, worker_panes.StoppedWorker)) and isinstance(result, (Stopped, NotRunning)):
            proof = _worker_stopped(before, kind, codex=after)
            if isinstance(proof, StopFailed):
                return proof
    if isinstance(result, (Stopped, NotRunning)):
        _retire_prompt(result.session_id)
    if isinstance(result, Stopped):
        try:
            owner = herdr.shared_session()
            for pane, entry in pane_record.read(owner).items():
                member = record.by_member_reference(pane_launch.reference_id(entry))
                if pane_launch.reference_id(entry) == result.session_id or (member is not None and member.current_session == result.session_id):
                    pane_record.end(owner, pane, result.session_id, record=record)
        except (OSError, ValueError) as error:
            print(f"{result.session_id}: stop note anomaly: {error}", file=sys.stderr)
    return result


def _stop_backend(identity: str) -> Literal["claude", "codex", ""]:
    """Read the backend of the retained launch if it is available."""
    try:
        publication = pane_record.publication_for_session(identity)
        return pane_launch.agent_kind(publication.entry) if publication is not None else ""
    except (OSError, ValueError) as error:
        print(f"{identity}: launch record anomaly: {' '.join(str(error).splitlines())}", file=sys.stderr)
        return ""


def _worker_stopped(before: worker_panes.OpenWorker | worker_panes.StoppedWorker, kind: str, *,
                    codex: herdr.CodexConversations | None = None) -> NotRunning | StopFailed:
    """Prove the conversation stopped and check the retained pane."""
    identity = str(before.row.current_session)
    client = herdr.Herdr(herdr.shared_session())
    details = f"pane {before.pane}; foreground processes not read"
    try:
        panes = client.panes()
        closed = not any(pane["pane_id"] == before.pane or pane.get("terminal_id") == before.terminal for pane in panes)
        at_shell = False
        if any(pane["pane_id"] == before.pane and pane.get("terminal_id") == before.terminal for pane in panes):
            activity = client.pane_activity(before.pane)
            at_shell = isinstance(activity, herdr.PaneAtShell)
            details = (f"pane {before.pane}; foreground processes: {', '.join(activity.commands) or 'none'}"
                       if isinstance(activity, herdr.PaneRunning) else f"pane {before.pane}; at its shell")
        if closed or at_shell:
            observed = herdr.read_conversation(identity, kind or _stop_backend(identity), codex=codex)
            if isinstance(observed, herdr.StoppedConversation):
                return NotRunning(identity)
            if isinstance(observed, herdr.UnknownConversation):
                return StopUnproved(identity, observed.reason)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        details += f"; {error}"
    return StopFailed(identity, f"worker stop has no fresh shell or closed-pane proof; {details}")


def _retire_prompt(session_id: str) -> None:
    """Publish only the session id and stop time; any failure is separate from stop or move."""
    try:
        from scripts.production.stall_watch import retire_session_prompt

        retire_session_prompt(session_id)
    except Exception as error:
        print(f"{session_id}: prompt cancellation: {error}", file=sys.stderr)


def stop_grace_seconds() -> float:
    return float(conf.read("shutdown").text("STOP_GRACE_SECONDS"))


def stop_target(session: str, *, strict: bool = False) -> str | SessionNotFound | StopFailed:
    """Resolve the same local target for a stop and its preview."""
    try:
        try:
            record = staff.initial_snapshot()
        except (OSError, ValueError) as error:
            if strict:
                raise
            print(f"{session}: staff anomaly: {error}", file=sys.stderr)
            record = staff.StaffSnapshot(())
        member = record.by_current_session(session) if strict else _stop_staff_row(record, session)
        if member is not None:
            return member.current_session
        registry = sessions.read_registry()
        if strict:
            live = sessions.session_liveness(session, registry=registry)
            if isinstance(live, sessions.SessionLivenessUnknown):
                return StopUnproved(session, live.reason)
            if isinstance(live, sessions.LiveSessionProcess):
                return session
        elif any(session == row["sessionId"] for row in sessions.running_sessions(registry=registry)):
            return session
        read_panes = pane_record.read_strict if strict else pane_record.read
        entries = read_panes(herdr.shared_session())
        references = pane_launch.named_references(session, record, entries)
        if strict:
            references.intersection_update({pane_launch.NativeLaunchReference(session)})
        identities: set[str] = set()
        panes = herdr.Herdr(herdr.shared_session()).panes() if references else []
        for reference in references:
            if isinstance(reference, pane_launch.StaffLaunchReference):
                owner = record.by_id(reference.id)
                if owner is not None:
                    identities.add(str(owner.current_session))
                continue
            locations = {pane for pane, entry in entries.items()
                         if pane_launch.launch_reference(entry, record) == reference}
            if any(pane["pane_id"] in locations and pane["session_id"]
                   and pane["session_id"] != reference.session
                   and isinstance(herdr.read_conversation(pane["session_id"]), herdr.LiveConversation) for pane in panes):
                continue
            identities.add(reference.session)
        if len(identities) > 1:
            return StopFailed(session, f"{session}: several tabs match")
        if identities:
            return identities.pop()
        if registry.unreadable:
            return StopUnproved(session, "session registry unreadable")
        return SessionNotFound(session)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        return StopFailed(session, str(error))


def _stop(session: str, kind: str = "", *,
          codex: herdr.CodexConversations | None = None) -> StopResult:
    """Signal only the process that proves the current conversation."""
    identity = session
    last_signal = ""
    signal_pid = 0
    try:
        settings = conf.read("shutdown")
        stop_grace, kill_grace = stop_grace_seconds(), float(settings.text("KILL_GRACE_SECONDS"))
        attempt = _move_stop_target.get()
        pinned = attempt.target if isinstance(attempt, MoveStopAttempt) else attempt
        if isinstance(pinned, MoveStopTarget):
            target = (pinned.identity if isinstance(pinned.member.owner, pane_launch.StaffLaunchReference)
                      else stop_target(pinned.identity, strict=True))
        else:
            target = stop_target(session)
        if isinstance(target, (SessionNotFound, StopFailed)):
            return target
        if isinstance(pinned, MoveStopTarget):
            record = staff.initial_snapshot()
            row = _stop_staff_row(record, session)
            if (target != pinned.identity or
                    (isinstance(pinned.member.owner, pane_launch.StaffLaunchReference)
                     and (row is None or row.id != pinned.member.owner.id or row.current_session != target))
                    or (isinstance(pinned.member.owner, pane_launch.NativeLaunchReference) and row is not None)):
                return StopFailed(target, "the move owner changed before the stop")
        identity = target
        kind = kind or _stop_backend(identity)
        observed = herdr.read_conversation(identity, kind, **({"codex": codex} if codex is not None else {}))
        if isinstance(observed, herdr.StoppedConversation):
            if isinstance(pinned, MoveStopTarget) and isinstance(pinned.source, LiveMoveSource):
                return StopFailed(identity, "the move source exited before the stop")
            return NotRunning(identity)
        if isinstance(observed, herdr.UnknownConversation):
            return StopUnproved(identity, observed.reason)
        if not observed.pids:
            return StopUnproved(identity, "conversation has no unique signal target; no signal sent")
        if len(observed.pids) != 1:
            return StopFailed(identity, "conversation has no unique signal target; no signal sent")
        live = sessions.LiveSessionProcess(observed.pids[0])
        kind = observed.kind
        proved = herdr.native_process(live.pid, kind)
        if proved.current_identity != identity:
            return StopFailed(identity, "process runs another conversation; no signal sent")
        started = proved.started
        if isinstance(pinned, MoveStopTarget):
            source = pinned.source
            if not isinstance(source, LiveMoveSource) or (source.pid, source.started) != (live.pid, started):
                return StopFailed(identity, "the move source process changed before the stop; no signal sent")
        if not _same_process(live.pid, started):
            if isinstance(pinned, MoveStopTarget):
                return StopFailed(identity, "the move source exited before the stop")
            return NotRunning(identity)
        if isinstance(attempt, MoveStopAttempt):
            attempt.record_request()
        if not herdr.check_native_target(live.pid, kind, proved):
            return StopFailed(identity, "process identity changed; no signal sent")
        _signal_process(live.pid, signal.SIGTERM)
        last_signal = "SIGTERM"
        signal_pid = live.pid
        deadline = monotonic() + stop_grace
        while _same_process(live.pid, started):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            _ = Event().wait(operation_deadline.remaining(min(0.1, remaining)))
        else:
            return Stopped(identity, live.pid)
        if _same_process(live.pid, started):
            try:
                if not herdr.check_native_target(live.pid, kind, proved):
                    return StopFailed(identity, "process identity changed; no signal sent")
            except ProcessLookupError:
                return Stopped(identity, live.pid)
            except herdr.NativeRegistryUnavailable:
                pass
            except (OSError, ValueError, herdr_types.HerdrFailure) as error:
                print(f"{identity}: stop identity anomaly after SIGTERM: {error}; waiting for exit", file=sys.stderr)
            else:
                _signal_process(live.pid, signal.SIGKILL)
                last_signal = "SIGKILL"
            deadline = monotonic() + kill_grace
            while _same_process(live.pid, started):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return StopFailed(identity, f"pid {live.pid} is still running after {last_signal}")
                _ = Event().wait(operation_deadline.remaining(min(0.1, remaining)))
        return Stopped(identity, live.pid)
    except ProcessLookupError:
        if isinstance(_move_stop_target.get(), MoveStopAttempt) and not last_signal:
            return StopFailed(identity, "the move source exited before the stop")
        return NotRunning(identity)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        reason = str(error)
        if isinstance(error, TimeoutError) and last_signal:
            reason += f"; pid {signal_pid} received {last_signal} and still runs"
        return StopFailed(identity, reason)


NO_WORKERS_TO_KEEP_RUNNING: frozenset[StaffId] = frozenset()


def stop_unit_workers(unit: StaffId, record: StaffSnapshot, *,
                      codex: herdr.CodexConversations | None = None,
                      keep_running: frozenset[StaffId] = NO_WORKERS_TO_KEEP_RUNNING,
                      failures: dict[StaffId, str] | None = None) -> list[str]:
    """Stop the selected workers concurrently and retain every staff row."""
    from scripts.agents import worker_lifecycle
    from scripts.herdr import worker_panes
    from scripts.agents import restart

    members = [row for row in record.find(kind="worker", parent=unit) if row.id not in keep_running]
    if not members:
        return []
    try:
        deadline = worker_lifecycle.effective_deadline()
        states = [state for state in worker_panes.observe(unit, record) if state.row.id not in keep_running]
        failed = {item.row.id: item.reason for item in states
                  if isinstance(item, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker))}
        unresolved = [f"{item.row.name}: {failed[item.row.id]}" for item in states if item.row.id in failed]
        live = [item for item in states if isinstance(item, worker_panes.OpenWorker)]
        client = herdr.Herdr(herdr.shared_session())
        shared: StopReadArguments = {"codex": codex} if codex is not None else {}
        with ThreadPoolExecutor(max_workers=max(1, len(live))) as pool:
            futures = [(item, pool.submit(copy_context().run, restart.stop_row, item.row, client,
                deadline=deadline, **shared)) for item in live]
            for item, future in futures:
                if not future.result():
                    unresolved.append(f"{item.row.name}: not proved stopped")
                    failed[item.row.id] = "Could not confirm that the worker stopped"
    except (OSError, ValueError, herdr_types.HerdrFailure, SystemExit) as error:
        reason = " ".join(str(error).splitlines())
        unresolved = [f"{unit}: {reason}"]
        failed = {row.id: reason for row in members}
        print(f"anomaly: {unresolved[0]}", file=sys.stderr)
    if failures is not None:
        failures.update(failed)
    return unresolved


def resume_arguments(entry: pane_launch.NativePaneLaunch, *,
                     configuration: models_registry.LaunchModelConfiguration | None = None) -> list[str]:
    names = _resume_names.get()
    if isinstance(names, LaunchNamesNotRead):
        names = LaunchNames.read()
    row = names.tab_record.by_member_reference(pane_launch.reference_id(entry))
    current = names.resume(entry)
    if row is not None and row.kind == "producer":
        restart = cast(RowStarter, cast(object, import_module("scripts.agents.restart")))
        return restart.producer_resume(row, current, configuration=configuration)[2]
    if (row is not None and row.kind == "worker" and isinstance(row.role, staff.KnownWorkerRole)
            and pane_launch.saved_kind(current) == "claude"):
        role = row.role.value
        worker_configuration = models_registry.optional_launch_snapshot() if configuration is None else configuration

        def claude_role_fallback() -> pane_record.ClaudeResumeSettings:
            from scripts.herdr import worker_panes

            try:
                settings = worker_panes.role_launch_settings(role, "claude", worker_configuration)
            except (OSError, ValueError, subprocess.SubprocessError):
                return pane_record.NO_CLAUDE_RESUME_SETTINGS
            return pane_record.ClaudeResumeSettings(settings.model, settings.effort)

        return pane_record.resume_arguments(current, claude_role_fallback=claude_role_fallback, record=names.tab_record,
                                            configuration=worker_configuration)
    return pane_record.resume_arguments(current, record=names.tab_record, configuration=configuration)


def _pane_environment(tab: herdr.TabSession, entry: pane_launch.NativePaneLaunch) -> tuple[list[str], dict[str, str]]:
    """Read the pane environment and remove inherited launch proofs."""
    client = herdr.Herdr(herdr.shared_session())
    pane = next((pane for pane in client.panes() if pane["pane_id"] == tab.pane_id), None)
    if pane is None:
        raise herdr_types.HerdrFailure(f"no pane {tab.pane_id!r}")
    unset = ["HERDR_TAB_ID", "STAFF_ID", "STAFF_OPERATION_FD", "PYTHONUNBUFFERED", "REFUSAL_WATCH_INVENTORY",
             *sorted(key for key in os.environ.keys() | entry["environment"].keys() if key.startswith("STAFF_ROW_FD_"))]
    environment = {key: value for key, value in entry["environment"].items() if key not in unset}
    environment = {**environment, "HERDR_PANE_ID": tab.pane_id,
                   "HERDR_WORKSPACE_ID": pane["workspace_id"], "RELAUNCH_PROMPT_DELIVERY": "pane"}
    return unset, environment


def _launch_home(entry: pane_launch.NativePaneLaunch) -> staff.SessionHome:
    """Use the selected launch environment, retained home, or normal account."""
    kind = pane_launch.saved_kind(entry)
    directory = entry["environment"].get("CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR", "")
    home = staff.SessionHome(Path(directory)) if directory else pane_launch.config_home(entry)
    if isinstance(home, staff.HomeUnknown):
        print(f"anomaly: {entry['session_id']}: session home is unknown; select the normal account", file=sys.stderr)
        selected = member_account.resume_account(kind)
        return staff.SessionHome(selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home)
    return home


def _pane_command(tab: herdr.TabSession, entry: pane_launch.NativePaneLaunch, account: str, *,
                  configuration: models_registry.LaunchModelConfiguration | None = None) -> list[str]:
    """Build the account command for a shell in another pane."""
    unset, environment = _pane_environment(tab, entry)
    kind = pane_launch.saved_kind(entry)
    if kind == "claude":
        home = environment.get("CLAUDE_CONFIG_DIR") or str(_launch_home(entry).path)
        herdr.trust_claude_launch(Path(entry["cwd"]), Path(home))
    program = [sys.executable, str(SCRIPTS / "accounts/accounts.py"), "exec", account]
    if kind == "codex":
        environment["CODEX_HOME"] = environment.get("CODEX_HOME") or str(_launch_home(entry).path)
        program = ["codex"]
    if kind == "claude" and configuration is None:
        configuration = models_registry.optional_launch_snapshot()
    arguments = resume_arguments(entry, configuration=configuration)
    if configuration is not None:
        environment = configuration.environment(environment)
    return ["env", *(argument for key in unset for argument in ("-u", key)),
            *(f"{key}={value}" for key, value in environment.items()),
            "sh", "-c", 'cd -- "$1" && shift && exec "$@"', "resume", entry["cwd"],
            *program, *trust_arguments(kind, entry["cwd"], arguments)]


def _exec_in_own_pane(tab: herdr.TabSession, entry: pane_launch.NativePaneLaunch,
                      account: str, message: str, *,
                      configuration: models_registry.LaunchModelConfiguration | None = None) -> Never:
    """Replace the foreground command with the saved account launch."""
    kind = pane_launch.saved_kind(entry)
    environment = accounts.launch_shell_environment(Path(entry["cwd"]))
    unset, pane_environment = _pane_environment(tab, entry)
    environment.update(pane_environment)
    for key in unset:
        _ = environment.pop(key, None)
    if kind == "claude":
        prepared = accounts.prepare_claude_launch(account, resume=True)
        for key in prepared.remove:
            _ = environment.pop(key, None)
        environment.update(prepared.variables)
        home = environment.get("CLAUDE_CONFIG_DIR") or str(_launch_home(entry).path)
        herdr.trust_claude_launch(Path(entry["cwd"]), Path(home))
    else:
        environment["CODEX_HOME"] = environment.get("CODEX_HOME") or str(_launch_home(entry).path)
    program = agent_program.resolve_program(kind, environment=environment)
    if kind == "claude" and configuration is None:
        configuration = models_registry.optional_launch_snapshot()
    arguments = resume_arguments(entry, configuration=configuration)
    if configuration is not None:
        arguments = models_registry.claude_arguments(arguments, configuration.environment(environment))
    command = [program, *trust_arguments(kind, entry["cwd"], arguments)]
    if message and kind == "claude":
        command.append(message)
    os.chdir(entry["cwd"])
    row = staff.initial_snapshot().by_member_reference(entry["session_id"])
    if row is not None and isinstance(row.exited, staff.Exited):
        _ = staff.resume(str(row.id))
    selected_home = staff.SessionHome(Path(environment["CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR"]).absolute())
    if row is not None and row.home != selected_home:
        try:
            _ = staff.set_home(row.id, selected_home)
        except (staff.StaffUnreadable, staff.StaffNotFound) as error:
            print(f"anomaly: {row.id}: pane home is unavailable: {error}", file=sys.stderr)
    try:
        os.execvpe(program, command, staff.child_environment(environment))
    except OSError:
        if row is not None and row.home != selected_home and isinstance(row.home, staff.SessionHome):
            _ = staff.set_home(row.id, row.home)
        if row is not None and isinstance(row.exited, staff.Exited):
            _ = staff.set_hold(str(row.id), row.exited)
        raise


def _prompt_identity(client: herdr.Herdr, pane: str, deadline: float) -> str:
    """Wait for the pane to expose its native identity within the start deadline."""
    while True:
        try:
            return client.native_session(pane, "claude", operation_deadline.remaining(max(0.001, deadline - monotonic())))
        except (OSError, ValueError, subprocess.SubprocessError, herdr_types.HerdrFailure):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            time.sleep(operation_deadline.remaining(min(0.5, remaining)))


def _start_session(tab: herdr.TabSession, entry: pane_launch.NativePaneLaunch, account: str, message: str, *,
                   configuration: models_registry.LaunchModelConfiguration | None = None) -> sessions.LiveSessionProcess:
    """Run the account command in the session's own pane, with one resume message."""
    client = herdr.Herdr(herdr.shared_session())
    command = _pane_command(tab, entry, account, configuration=configuration)
    settings = conf.read("shutdown")
    start_seconds = settings.number("START_SECONDS")
    _ = settings.number("STAY_SECONDS")
    home = str(_launch_home(entry).path)
    transcript = herdr.SessionTranscript(Path(home), entry["session_id"])
    cursor: herdr.TranscriptCursor | herdr.UnspecifiedPromptTranscript = herdr.UnspecifiedPromptTranscript()
    if message:
        cursor = herdr.TranscriptCursor(transcript, datetime.now(timezone.utc))
        cursor.skip_existing_turns()
    launched_at = datetime.now(timezone.utc)
    if isinstance(cursor, herdr.TranscriptCursor):
        cursor.since = launched_at
    operation_deadline.check()
    client.run_in_pane(tab.pane_id, command)
    deadline = monotonic() + operation_deadline.remaining(start_seconds)
    live = sessions.session_liveness(entry["session_id"])
    while isinstance(live, sessions.SessionNotLive):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise herdr_types.HerdrFailure("never started")
        time.sleep(operation_deadline.remaining(min(0.5, remaining)))
        live = sessions.session_liveness(entry["session_id"])
    if isinstance(live, sessions.SessionLivenessUnknown):
        raise herdr_types.HerdrFailure(live.reason)
    if not message:
        return live
    try:
        identity = _prompt_identity(client, tab.pane_id, deadline)
    except (OSError, ValueError, subprocess.SubprocessError, herdr_types.HerdrFailure) as error:
        print(f"{entry['session_id']}: prompt identity anomaly: {error}", file=sys.stderr)
        return live
    if identity != entry["session_id"]:
        print(f"{entry['session_id']}: prompt identity anomaly: observed {identity}", file=sys.stderr)
        return live
    opening = herdr.OpeningPrompt(message, transcript, cursor=cursor)
    outcome = client.submit_opening_prompt(tab.pane_id, opening, launched_at, int(operation_deadline.remaining(start_seconds) * 1000))
    if isinstance(outcome, herdr.PromptNotSubmitted):
        print(f"{identity}: prompt anomaly: {outcome.reason}", file=sys.stderr)
    return live


def _observed_account(up: ClaudeUp, pid: int, started: str | None,
                      capture_generation: bool) -> ClaudeUp | LiveMoveSource | ResumeFailed:
    if not capture_generation:
        return up
    if not started or started != sessions.process_start(pid):
        return LiveAccountUnproved(up.session_id, "the source process changed during the account read")
    return LiveMoveSource(up, pid, started)


def _codex_running_on_account(identity: str, pane_id: str, pid: int,
                              capture_generation: bool) -> ClaudeUp | LiveMoveSource | ResumeFailed:
    """Read one process that the conversation reader proves hosts this thread."""
    started = sessions.process_start(pid) if capture_generation else None
    setting = account_reader.process_codex_home(pid)
    if isinstance(setting, account_reader.ProcessEnvironmentUnread):
        return LiveAccountUnproved(identity, setting.reason)
    home = setting.home if isinstance(setting, account_reader.ProcessCodexHome) else Path.home() / ".codex"
    up = ClaudeUp(identity, pane_id, accounts.codex_home_label(home), start_state=ClaudeStartState.ALREADY_RUNNING, pid=pid)
    return _observed_account(up, pid, started, capture_generation)


@overload
def running_on_account(identity: str, pane_id: str, *, kind: str = "claude", capture_generation: Literal[True]
                       ) -> LiveMoveSource | ResumeFailed | sessions.SessionNotLive: ...


@overload
def running_on_account(identity: str, pane_id: str, *, kind: str = "claude", capture_generation: Literal[False] = False
                       ) -> ClaudeUp | ResumeFailed | sessions.SessionNotLive: ...


def running_on_account(identity: str, pane_id: str, *, kind: str = "claude", capture_generation: bool = False
                       ) -> ClaudeUp | LiveMoveSource | ResumeFailed | sessions.SessionNotLive:
    """Keep a running conversation on its current account."""
    if kind == "codex":
        observed = herdr.read_conversation(identity, "codex")
        if isinstance(observed, herdr.StoppedConversation):
            return sessions.SessionNotLive()
        if isinstance(observed, herdr.UnknownConversation):
            if not capture_generation:
                herdr.conversation_note(observed)
            client = herdr.Herdr(herdr.shared_session())
            failure = ""
            try:
                for pane in client.panes():
                    if pane["session_id"] != identity and pane["pane_id"] != pane_id:
                        continue
                    candidate = pane["pane_id"]
                    herdr.forget_pane_evidence(client.session, candidate)
                    try:
                        info = client.result("pane", "process-info", "--pane", candidate).get("process_info")
                        owner = herdr.foreground_pane_conversation(info, pane)
                    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                        failure = str(error)
                        continue
                    if isinstance(owner, pane_launch.ProvedPaneConversation) and owner.kind == "codex" and owner.identity == identity:
                        return _codex_running_on_account(identity, candidate, owner.pid, capture_generation)
                    if pane["session_id"] == identity:
                        failure = f"pane {candidate}: cannot prove the reported Codex owner"
            except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                failure = str(error)
            if capture_generation:
                return ResumeFailed(identity, observed.reason)
            if failure or not observed.shared_codex:
                return ResumeFailed(identity, failure or observed.reason)
            return sessions.SessionNotLive()
        if not observed.pids:
            return ResumeFailed(identity, "live Codex thread has no process account proof")
        return _codex_running_on_account(identity, pane_id, observed.pids[0], capture_generation)
    live = sessions.session_liveness(identity)
    if isinstance(live, sessions.SessionNotLive):
        return live
    if isinstance(live, sessions.SessionLivenessUnknown):
        return ResumeFailed(identity, live.reason)

    started = sessions.process_start(live.pid) if capture_generation else None
    label = "unknown account"
    try:
        setting = account_reader.process_account_directory(account_reader.process_config_dir(live.pid))
        if isinstance(setting, account_reader.ConfiguredClaudeFolder):
            label = accounts.claude_config_label(setting.path)
    except (OSError, ValueError) as error:
        if capture_generation:
            return LiveAccountUnproved(identity, "live process account is unreadable")
        print(f"{identity}: account anomaly: {error}", file=sys.stderr)
    up = ClaudeUp(identity, pane_id, label, start_state=ClaudeStartState.ALREADY_RUNNING, pid=live.pid)
    return _observed_account(up, live.pid, started, capture_generation)


def member_machine(member: StaffRow) -> str:
    """Return the member's recorded machine, or an empty string when unknown."""
    location = member.worktree if isinstance(member.worktree, (staff.PresentWorktree, staff.RemovedWorktree)) else member.doc
    if isinstance(location, (staff.PresentWorktree, staff.RemovedWorktree, staff.LiveDoc, staff.WrappedDoc)):
        return location.machine
    return ""


def _nearby_resume_names(reference: str, names: LaunchNames) -> list[str]:
    """Select close names from local units and showrunners."""
    machine = remote.local_machine()
    return sorted({row.name for row in names.tab_record.rows
                   if row.kind in {"unit", "showrunner"} and member_machine(row) == machine
                   and reference.casefold() in row.name.casefold()})[:5]


def resume(session: str, *, why: str = "resumed by name", to: str | None = None, requester: str = "",
           client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> ResumeResult:
    with ExitStack() as locks:
        try:
            locks.enter_context(herdr.reboot_lock())
            settings = conf.read("shutdown")
            for key in ("START_SECONDS", "STAY_SECONDS"):
                _ = settings.number(key)
        except (conf.Unusable, herdr.RebootLockBusy) as error:
            return ResumeFailed(session, str(error))
        with staff.row_operation(session):
            return _resume(session, why=why, to=to, requester=requester, client=client)


@dataclass(frozen=True)
class StoppedOnPurpose:
    name: str


def resume_for_upkeep(identity: StaffId) -> ResumeResult | StoppedOnPurpose | None:
    """Check for a restart or an exit hold under the row lock."""
    with staff.row_operation(identity):
        if shutdown_record.active():
            return None
        row = staff.initial_snapshot().rows_by_id.get(identity)
        if row is None or row.kind != "producer":
            return SessionNotFound(identity)
        if isinstance(row.exited, staff.Exited) or isinstance(row.paused, staff.Paused):
            return StoppedOnPurpose(row.name)
        return resume(identity)


def resend_producer_opening(name: str, *, to: str = "") -> ResumeResult:
    """Try one explicit resend to the producer's existing reserved pane."""
    return _locked_first_producer_resume(ProducerFirstStart(name, producer_folder(name)), to=to, resend_opening=True)


def _codex_name_ids(name: str) -> list[str]:
    """Accept an explicit native ID without reading Codex display names."""
    try:
        _ = staff.native_id(name)
        return [name]
    except staff.StaffRefused:
        return []


def _unrecorded_codex_launch(identity: str) -> pane_record.PaneLaunch | SessionNotFound:
    """Read the native resume arguments and the folder from the rollout metadata."""
    path = next(iter(transcript_paths(agent_accounts.codex_home() / "sessions", f"**/rollout-*{identity}.jsonl")), None)
    if path is None:
        return SessionNotFound(identity)
    restart = cast(RowStarter, cast(object, import_module("scripts.agents.restart")))
    try:
        kind, environment, arguments = restart.session_launch(identity, identity)
    except OSError:
        return SessionNotFound(identity)
    if kind != "codex":
        return SessionNotFound(identity)
    with open_transcript(path) as stream, TextIOWrapper(stream, encoding="utf-8") as transcript:
        meta = herdr.json_object(cast(object, json.loads(transcript.readline())), "Codex rollout metadata")
    folder = herdr.json_text(herdr.json_object(meta.get("payload"), "Codex session metadata").get("cwd"), "Codex rollout cwd")
    return pane_record.launch("", identity, folder, environment, [kind, *arguments], kind=kind)


def _resume(session: str, *, why: str = "resumed by name", to: str | None = None, requester: str = "",
            client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> ResumeResult:
    names = LaunchNames.read()
    result = _resume_from_names(session, names, why=why, to=to, requester=requester, client=client)
    if isinstance(result, ClaudeUp):
        members = [row for row in names.tab_record.rows
                   if row.current_session == result.session_id or row.id == session or row.name == session]
        if len(members) == 1:
            member = members[0]
            with staff.row_operation(str(member.id)):
                if member.kind == "producer" and isinstance(member.exited, staff.Exited):
                    _ = staff.resume(str(member.id))
                shutdown_record.remove_started([str(member.id)])
    return result


def _resume_from_names(session: str, names: LaunchNames, *, why: str, to: str | None, requester: str,
                       client: herdr.Herdr | herdr.LaunchClientSource) -> ResumeResult:
    record = names.tab_record
    retired = next((row for row in record.retired_producers if session in (row.name, row.id, row.current_session)), None)
    if retired is not None:
        return ResumeFailed(session, f"{retired.name}: retired; use agents resume {retired.name} --retired")
    member = record.by_member_reference(session)
    matches = [row for row in record.rows if row.name == session or row == member]
    candidates = ", ".join(f"{row.name} ({row.id})" for row in matches[:5])
    if len(matches) > 1:
        return ResumeFailed(session, f"ambiguous name; candidates: {candidates}")
    if matches:
        member = matches[0]
        machine = member_machine(member)
        if machine and machine != remote.local_machine():
            return ResumeFailed(session, f"session is on {machine}")
        if member.kind == "producer":
            try:
                saved = producer_creation.read_record(producer_creation.record_path(member.name))
                if (_producer_admission_path(member.name).exists()
                        or isinstance(_pending_producer_opening(member.name, saved), PendingProducerOpening)):
                    return _locked_first_producer_resume(_producer_start_request(member), to=to or "", client=client)
            except (OSError, ValueError) as error:
                return ResumeFailed(session, str(error))
        session = member.current_session
    else:
        if session and Path(session).name == session and session not in {".", ".."}:
            try:
                saved = producer_creation.read_record(producer_creation.record_path(session))
                opening = _pending_producer_opening(session, saved)
                if isinstance(opening, PendingProducerOpening) or _producer_admission_path(session).exists():
                    folder = opening.folder if isinstance(opening, PendingProducerOpening) else producer_folder(session)
                    return _locked_first_producer_resume(ProducerFirstStart(session, folder), to=to or "", client=client)
            except (OSError, ValueError) as error:
                return ResumeFailed(session, str(error))
        try:
            configuration = (models_registry.launch_configuration(os.environ).text
                if models_registry.OPERATION_ENVIRONMENT[0] in os.environ else models_registry.registry_text())
            registry = models_registry.parse_registry(configuration)
        except (OSError, ValueError):
            registry = {}
        if session in registry.get("producers", {}):
            try:
                folder = producer_folder(session)
                if (session in registry.get(producer_creation.SECTION, {}) or (folder / "README.md").is_file()
                        or isinstance(_tab_of(session, names=names), SessionNotFound)):
                    return _locked_first_producer_resume(ProducerFirstStart(session, folder), to=to or "", client=client)
            except (OSError, ValueError) as error:
                return ResumeFailed(session, str(error))
        identities = _codex_name_ids(session)
        if len(identities) > 1:
            return ResumeFailed(session, "ambiguous name; candidates: " + ", ".join(identities))
        if identities:
            session = identities[0]
    result = _resume_once(session, why=why, to=to, requester=requester, names=names, client=client)
    if isinstance(result, SessionNotFound) and not matches:
        nearby = _nearby_resume_names(session, names)
        suffix = "; candidates: " + ", ".join(nearby) if nearby else ""
        return ResumeFailed(session, "unknown name" + suffix)
    return result


def heal_account_trust(
    folder: Path, target: Path, source: Path, name: str,
) -> herdr.Trusted | herdr.TrustCopied | herdr.Untrusted | herdr.UnreadableTrustFile:

    trust = herdr.heal_claude_trust(folder, target / ".claude.json",
        accounts.trust_sources(target, source), dry_run=False)
    if isinstance(trust, (herdr.Untrusted, herdr.UnreadableTrustFile)):
        reason = str(trust) if isinstance(trust, herdr.UnreadableTrustFile) else f"Claude has not trusted {folder}"
        print(f"{name}: trust anomaly: {reason}", file=sys.stderr)
    return trust


def _unit_resume_owner(production: str, unit: str, record: StaffSnapshot) -> unit_lookup.UnitOwner:
    """Read the unit again while only non-agent holders block its pane."""
    from scripts.production import unit_lookup

    owner = unit_lookup.checked_unit(production, unit, record=record)
    if not isinstance(owner, unit_lookup.NonAgentPaneHolders):
        return owner
    seconds = conf.read("shutdown").number("HOLDER_EXIT_SECONDS")
    deadline = monotonic() + operation_deadline.remaining(seconds)
    wait = Event()
    while isinstance(owner, unit_lookup.NonAgentPaneHolders):
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        _ = wait.wait(min(herdr.PANE_POLL_SECONDS, remaining))
        if monotonic() >= deadline:
            break
        with operation_deadline.within(deadline):
            observed = unit_lookup.checked_unit(production, unit, record=record)
        if monotonic() >= deadline:
            break
        owner = observed
    return owner


def _resume_once(session: str, *, why: str, to: str | None = None, requester: str = "",
                 names: LaunchNames,
                 client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> ResumeResult:
    began = monotonic()
    identity = session
    original: pane_launch.LaunchPublication | None = None
    attempt_pane = ""
    source_pane: pane_launch.ResumeSourcePane | pane_launch.ResumeSourceAbsent = pane_launch.ResumeSourceAbsent.CURRENT
    name_scope = _resume_names.set(names)
    start_scope = ExitStack()
    try:
        tab = _tab_of(session, names=names)
        client = herdr.Herdr(herdr.shared_session()) if isinstance(client, herdr.LaunchClientSource) else client
        row = names.tab_record.by_current_session(identity)
        if isinstance(tab, herdr.TabSession):
            identity = tab.session_id or identity
        original = pane_record.publication_for_session(identity, record=names.tab_record)
        if (isinstance(tab, SessionNotFound) and row is None and original is not None
                and original.path == pane_record.record_path(client.session)
                and any(pane["pane_id"] == original.entry["pane_id"] for pane in client.panes())):
            return tab
        if original is None and isinstance(tab, herdr.TabSession) and row is None:
            saved = pane_record.read_strict(client.session).get(tab.pane_id)
            if saved is not None:
                original = pane_launch.LaunchPublication(pane_record.record_path(client.session), saved)
        entry = original.entry if original is not None else None
        source_client = client
        if original is not None:
            source_client = client if original.path == pane_record.record_path(client.session) else herdr.Herdr(original.path.stem)
            locations = [dict(pane) for pane in source_client.panes()]
            evidence = pane_launch.NativePaneEvidence(locations, source_client.scan_row_panes(names.tab_record, locations))
            reference = pane_launch.launch_reference(original.entry, names.tab_record)
            source_matches = pane_launch.reference_panes(reference, names.tab_record, source_client,
                {original.entry["pane_id"]: original.entry}, evidence=evidence)
            if len(source_matches) == 1:
                source_pane = pane_launch.ResumeSourcePane(str(source_matches[0]["pane_id"]))
            elif not any(pane["pane_id"] == original.entry["pane_id"] for pane in locations):
                source_pane = pane_launch.ResumeSourcePane(original.entry["pane_id"])
            elif any(pane["pane_id"] == original.entry["pane_id"]
                     for owner_id, panes in evidence.scan.found.items()
                     if not isinstance(reference, pane_launch.StaffLaunchReference) or owner_id != reference.id
                     for pane in panes):
                source_pane = pane_launch.ResumeSourceAbsent.OTHER_OWNER
        if entry is None and row is None:
            recovered = _unrecorded_codex_launch(identity)
            if isinstance(recovered, SessionNotFound):
                if isinstance(tab, herdr.TabSession):
                    running = running_on_account(identity, tab.pane_id)
                    if not isinstance(running, sessions.SessionNotLive):
                        return running
                return recovered
            entry = recovered
        configuration = None
        if row is not None and row.kind == "producer":
            configuration = models_registry.launch_snapshot()
            kind = models_registry.launch_settings("producers", row.name, configuration=configuration).kind
            current_kind = _producer_current_kind(row, entry)
            if isinstance(current_kind, str) and current_kind != kind:
                request = _producer_start_request(row)
                old_pane: herdr.PaneIdentity | herdr.NoPane = herdr.NoPane(row.name)
                if isinstance(tab, herdr.TabSession):
                    source_client = client
                    old_pane = client.pane_row(tab.pane_id)
                elif isinstance(source_pane, pane_launch.ResumeSourcePane):
                    old_pane = source_client.pane_row(source_pane.pane)
                change = ProducerFamilyChange(request.name, request.folder, row.current_session, current_kind,
                                              source_client, old_pane, configuration)
                return _locked_first_producer_resume(change, to=to or "", why=why, requester=requester, client=client)
            if entry is not None:
                entry = names.resume(entry)
                entry["kind"] = kind
        elif entry is not None:
            entry = names.resume(entry)
            kind = pane_launch.saved_kind(entry)
        else:
            kind = pane_launch.current_kind(row) if row is not None else "claude"
        pane_id = tab.pane_id if isinstance(tab, herdr.TabSession) else ""
        running = running_on_account(identity, pane_id, kind=kind)
        if not isinstance(running, sessions.SessionNotLive):
            if row is not None and isinstance(running, ClaudeUp) and isinstance(running.pid, int) and running.account != "unknown account":
                member_account.repair_home(row, kind, running.pid)
            return running
        if row is not None and row.kind == "unit" and (parent := names.tab_record.parent_of(row)) is not None:
            from scripts.production import unit_lookup

            match _unit_resume_owner(parent.name, row.name, names.tab_record):
                case unit_lookup.UnknownUnit(reason=reason):
                    return ResumeFailed(identity, reason)
                case unit_lookup.ProvedLiveUnit():
                    return ResumeFailed(identity, "a unit owner is still live")
                case unit_lookup.ProvedAbsentUnit():
                    pass
        mode = worker_mode() if row is not None else NO_WORKERS
        if isinstance(mode, WorkersHeld) and row is not None and row.kind == "worker" and kind == "codex":
            return ResumeFailed(identity, "no-worker mode keeps Codex workers stopped")
        if entry is None:
            if row is None:
                return SessionNotFound(session)
            restart = cast(RowStarter, cast(object, import_module("scripts.agents.restart")))
            pane = restart.start_row(row, names.tab_record, client, target_account=to or "",
                                     **({"configuration": configuration} if configuration is not None else {}))
            result = running_on_account(identity, pane, kind=kind)
            if isinstance(result, ClaudeUp):
                return replace(result, elapsed_seconds=monotonic() - began, start_state=ClaudeStartState.STARTED)
            return ResumeFailed(identity, "session did not start") if isinstance(result, sessions.SessionNotLive) else result
        from scripts.worktree import worktrees

        entry = entry.copy()
        entry["session_id"] = identity
        entry["cwd"] = str(worktrees.moved_launch_folder(Path(entry["cwd"])))
        entry["environment"] = names.environment(entry)

        source = pane_launch.config_home(entry, record=names.tab_record)
        source_label = ""
        if isinstance(source, staff.SessionHome):
            source_label = agent_accounts.codex_home_role_label(source.path) if kind == "codex" else accounts.claude_config_label(source.path)
        else:
            print(f"anomaly: {identity}: session home is unknown; select the normal account", file=sys.stderr)
        selected = member_account.resume_account(kind, row if row is not None else SESSION_WITHOUT_STAFF_ROW, to=to or "")
        copied_from = ""
        if isinstance(selected, accounts.CodexWorkersAccount):
            target_dir, label = selected.home, selected.label
            account_environment = {"CODEX_HOME": str(target_dir)}
        else:
            target_dir, label = selected.dir, selected.label
            account_environment = account_reader.named_account_environment(target_dir)
        entry["environment"] = {**entry["environment"], **account_environment}
        if row is not None and row.kind == "producer":
            expectation = pane_launch.ProducerLaunchRequest(row.id, row.current_session, kind, Path(entry["cwd"]), target_dir)
            entry["environment"] = expectation.environment(entry["environment"])
        if row is None:
            entry["config_dir"] = str(target_dir)
            previous_home = pane_launch.config_home(original.entry, record=names.tab_record) if original is not None else staff.HomeUnknown()
            if original is not None and (not isinstance(previous_home, staff.SessionHome) or previous_home.path != target_dir):
                try:
                    pane_record.write(client.session, pane_launch.without_staff_labels(entry))
                except (OSError, ValueError) as error:
                    print(f"{identity}: account record anomaly: {error}", file=sys.stderr)
        else:
            _ = entry.pop("config_dir", None)
        if row is not None and row.kind == "producer" and not (
                isinstance(tab, herdr.TabSession) and os.environ.get("HERDR_SESSION") == client.session
                and os.environ.get("HERDR_PANE_ID") == tab.pane_id):
            space = herdr.row_workspace(row, names.tab_record, row.name)
            if isinstance(tab, SessionNotFound) or not client.producer_pane(tab.pane_id, str(row.id), space, record=names.tab_record):
                destination = client.producer_row_tab(str(row.id), row.name, space, record=names.tab_record)
                tab = (herdr.TabSession(destination.pane, row.name, "", identity)
                       if isinstance(destination, herdr.ExistingPane) else SessionNotFound(identity))
        elif row is not None and isinstance(tab, SessionNotFound):
            own_pane = client.saved_row_tab(str(row.id), row.name, herdr.row_workspace(row, names.tab_record, row.name),
                                             record=names.tab_record)
            if isinstance(own_pane, herdr.ExistingPane):
                tab = herdr.TabSession(own_pane.pane, row.name, "", identity)
        if isinstance(tab, SessionNotFound):
            space = herdr.row_workspace(row or STAFF_ROOT, names.tab_record, entry["name"])
            with herdr.restore_staff(names.tab_record, labels=names.labels):
                pane = client.open_tab(space,
                    entry["name"] or identity, entry["cwd"], entry["environment"])
            entry = entry.copy()
            entry["pane_id"] = pane
            attempt_pane = pane
            tab = next((item for item in client.tab_sessions() if item.pane_id == pane), None)
            if tab is None:
                raise herdr_types.HerdrFailure("new recovery pane is unavailable")
            tab = replace(tab, session_id=identity)
        elif entry["name"]:
            try:
                with herdr.restore_staff(names.tab_record, labels=names.labels):
                    _ = client.retitle(tab.pane_id, entry["name"])
            except (OSError, ValueError, herdr_types.HerdrFailure) as error:
                print(f"{identity}: title anomaly: {error}", file=sys.stderr)
        workers: ResumeWorkers | WorkersHeld = NO_WORKERS
        own = NO_OWN_PASS
        detached = ""
        from scripts.production import worker
        try:
            if row is not None and row.kind == "worker":
                from scripts.agents import detached_runs
                from scripts.herdr import worker_panes

                run = worker_panes.worker_run(row, record=names.tab_record)
                own = worker.own_pass(row, names.tab_record, run=worker_panes.DispatchRun(run))
                detached = detached_runs.resume_context(run / detached_runs.DETACHED_RUNS_DIR, row.name)
            elif isinstance(mode, WorkersHeld):
                workers = mode
            elif row is not None:
                workers = worker.resume_workers(row.id, names.tab_record)
        except (OSError, ValueError) as error:
            print(f"{identity}: workers anomaly: {error}", file=sys.stderr)
        if isinstance(own, worker.UnsubmittedAssignment):
            message = launcher_prompt(row.id, own.dispatch_id, own.command) if row is not None else ""
            if detached:
                message += "\n" + detached
        else:
            message = resume_message(why, move=AccountMove(source_label, label) if source_label != label else NO_ACCOUNT_MOVE,
                                     workers=workers, own_pass=own, unit=row, detached=detached) if (kind != "codex" or row is not None) and (
                                         row is None or row.kind != "worker" or isinstance(own, BusyPass) or detached) else ""
        completed = entry
        if row is not None and row.kind == "producer":
            expectation = pane_launch.ProducerLaunchRequest(row.id, row.current_session, kind, Path(entry["cwd"]), target_dir)
            start_scope.enter_context(pane_launch.producer_launch_request(expectation))
            completed = expectation.expected_launch(entry)
        if os.environ.get("HERDR_PANE_ID") == tab.pane_id:
            if row is None:
                with pane_record.locked(client.session):
                    entries = pane_record.read_strict(client.session)
                    pending = cast(pane_launch.CurrentLaunch, pane_launch.without_staff_labels(entry))
                    pending["session_id"] = pane_launch.reference_id(original.entry) if original is not None else identity
                    entries[tab.pane_id] = pending
                    pane_record.save(client.session, entries)
            attempt_pane = tab.pane_id
            _exec_in_own_pane(tab, entry, label, message,
                              **({"configuration": configuration} if configuration is not None and kind == "claude" else {}))
        started: herdr.AgentLaunched | sessions.LiveSessionProcess
        if kind == "codex":
            prepared = herdr.prepare_session_environment(identity, entry["cwd"], entry["environment"], herdr.NO_ENVIRONMENT_REMOVALS)
            if isinstance(prepared, nix_env.Refused):
                raise OSError(str(prepared))
            attempt_pane = tab.pane_id
            launched = herdr.launch_prepared_agent(client.session, herdr.ExistingPane(tab.pane_id, ""), prepared, kind,
                trust_arguments(kind, entry["cwd"], resume_arguments(entry)), herdr.NoOpeningPrompt(),
                int(operation_deadline.remaining(conf.read("shutdown").number("START_SECONDS")) * 1000),
                agent=entry["name"] or identity, client=client)
            if isinstance(launched.outcome, herdr.PromptNotSubmitted):
                raise herdr_types.HerdrFailure(launched.outcome.reason)
            if message:
                client.prompt(tab.pane_id, message, target=identity)
            started = launched
        else:
            live = sessions.session_liveness(identity)
            if isinstance(live, sessions.SessionLivenessUnknown):
                raise herdr_types.HerdrFailure(live.reason)
            if isinstance(live, sessions.SessionNotLive):
                attempt_pane = tab.pane_id
                live = _start_session(tab, entry, label, message,
                                      **({"configuration": configuration} if configuration is not None else {}))
            started = live
        selected_home = staff.SessionHome(target_dir.absolute())
        if row is not None and row.home != selected_home:
            try:
                # The launch hook can publish the new home before the start returns.
                current = staff.by_id(row.id)
                if current is None or current.home != selected_home:
                    _ = staff.set_home(row.id, selected_home)
            except (staff.StaffUnreadable, staff.StaffNotFound) as error:
                print(f"anomaly: {row.id}: pane home is unavailable: {error}", file=sys.stderr)
        if original is not None:
            try:
                destination_path = pane_record.record_path(client.session)
                attempt = pane_launch.resume_attempt(destination_path, tab.pane_id)
                proof = names.resume_proof(original.entry, completed)
                source_client = client if original.path == destination_path else herdr.Herdr(original.path.stem)
                if (isinstance(source_pane, pane_launch.ResumeSourceAbsent)
                        or (original.path == destination_path and source_pane.pane == tab.pane_id)
                        or not cast(Callable[[herdr.Herdr, str, str], bool],
                            import_module("scripts.agents.restart").pane_holds_other_work)(source_client, source_pane.pane, identity)):
                    pane_launch.finish_restore(original.path, original.entry, attempt, proof, close=client.close_tab_confirmed,
                                               destination=destination_path, close_source=source_client.close_tab_confirmed,
                                               source_pane=source_pane)
            except (OSError, ValueError, herdr_types.HerdrFailure) as error:
                print(f"{identity}: launch record anomaly: {error}", file=sys.stderr)
        attempt_pane = ""
        if not _move_resume.get():
            _watch_resume(identity, tab.pane_id, label, started, requester, why)
        return ClaudeUp(identity, tab.pane_id, label, monotonic() - began, copied_from)
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        if original is not None and attempt_pane:
            try:
                cleanup_client = herdr.Herdr(herdr.shared_session())
                destination_path = pane_record.record_path(cleanup_client.session)
                attempt = pane_launch.resume_attempt(destination_path, attempt_pane)
                pane_launch.finish_restore(original.path, original.entry, attempt, pane_launch.FailedResume(),
                                           close=cleanup_client.close_tab_confirmed, destination=destination_path, source_pane=source_pane)
            except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as cleanup:
                return ResumeFailed(identity, f"{error}; cleanup failed: {cleanup}")
        return ResumeFailed(identity, str(error))
    finally:
        _resume_names.reset(name_scope)
        start_scope.close()


def _watch_resume(identity: str, pane: str, account: str,
                  started: herdr.AgentLaunched | sessions.LiveSessionProcess, requester: str, why: str) -> None:
    try:
        if isinstance(started, herdr.AgentLaunched):
            observed = running_on_account(identity, pane, kind="codex")
            if not isinstance(observed, ClaudeUp) or not isinstance(observed.pid, int):
                raise OSError(observed.reason if isinstance(observed, ResumeFailed) else "process id is unavailable")
            pid = observed.pid
        else:
            pid = started.pid
        generation = sessions.process_start(pid)
        if generation:
            _ = _detach(["stay", "--session", identity, "--pane", pane,
                     "--account", account, "--pid", str(pid), "--generation", generation, "--why", why,
                     *(["--requester", requester] if requester else [])], "stay")
        else:
            print(f"{identity}: observer anomaly: process generation is unavailable", file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"{identity}: observer anomaly: {error}", file=sys.stderr)


_STOP_RESUME_PROGRAM = Path(__file__).resolve()
_DETACHED_START_SECONDS = 15


def _launch_mac_with_log(command: list[str], environment: dict[str, str], log_fd: int) -> None:
    read_fd, ready_fd = os.pipe()
    try:
        try:
            _ = subprocess.Popen([*command, "--ready-fd", str(ready_fd)], env=environment, start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=log_fd, pass_fds=(ready_fd,))
        finally:
            os.close(ready_fd)
        if not select.select([read_fd], [], [], _DETACHED_START_SECONDS)[0]:
            raise subprocess.TimeoutExpired(command, _DETACHED_START_SECONDS)
        if not os.read(read_fd, 1):
            raise OSError("The detached process exited before it confirmed the log claim")
    finally:
        os.close(read_fd)


def _detach(arguments: list[str], task: str, *, program: Path = _STOP_RESUME_PROGRAM, log: Path | None = None,
            log_fd: int | None = None) -> Path:
    """Run lifecycle work outside the caller's stop boundary."""
    from scripts.production import showrunners

    # Capture the registry identity before the child loses the caller's session id.
    if "--requester" not in arguments:
        requester = showrunners.own_name()
        options = ["--requester", requester]
        boundary = arguments.index("--") if "--" in arguments else len(arguments)
        arguments = [*arguments[:boundary], *options, *arguments[boundary:]]
    if log is None:
        logs = shutdown_state_root() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / f"{task}-{uuid4().hex}.log"
    environment = dict(os.environ)
    _ = environment.pop("CLAUDE_CODE_SESSION_ID", None)
    log_options = ["--log", str(log)]
    if log_fd is not None:
        log_options.extend(("--log-fd", "1"))
    command = [sys.executable, str(program), arguments[0], *log_options, *arguments[1:]]
    if log_fd is not None:
        if sys.platform == "darwin":
            _launch_mac_with_log(command, environment, log_fd)
        else:
            # Keep the descriptor open until the child claims the log.
            source = f"/proc/{os.getpid()}/fd/{log_fd}"
            _ = subprocess.run(["systemd-run", "--user", "--collect", "--quiet", "--service-type=notify",
                                "--expand-environment=no", f"--property=StandardOutput=append:{source}",
                                "--property=StandardError=journal", "--property=UnsetEnvironment=CLAUDE_CODE_SESSION_ID",
                                *command, "--notify-systemd"], env=environment, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, check=True, timeout=_DETACHED_START_SECONDS)
    elif sys.platform == "darwin":
        _ = subprocess.Popen(command, env=environment, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        _ = subprocess.run(["systemd-run", "--user", "--collect", "--quiet", "--no-block",
                            "env", "-u", "CLAUDE_CODE_SESSION_ID", *command],
                           env=environment, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, check=True, timeout=15)
    return log


detach = _detach


def _handoff_batch(selected: list[str], target: str, why: str, warn_seconds: int, cause: moves.MoveCause,
                   source_accounts: Mapping[str, str]) -> None:
    batch = _move_batch.get()
    identities = {session: batch.native_sessions[session] for session in selected}
    group = ("--group", batch.intent.group) if isinstance(batch.intent, moves.FollowGroupAccount) else ()
    group_scope = ("--explicit-group-scope",) if batch.intent is moves.ExplicitMoveTarget.GROUP_ACCOUNT else ()
    _ = _detach(["batch", "--target", target, *group, *group_scope, "--why", why, "--cause", cause, "--warn-seconds", str(warn_seconds),
                 "--request-id", batch.request_id,
                 *(arg for session, identity in identities.items() if (label := source_accounts.get(session))
                   for arg in ("--source-account", f"{identity}={label}")),
                 "--", *identities.values()], "move")


def _handoff_locked_moves(selected: Mapping[str, str], locks: Mapping[str, LockedMoveRow], target: str,
                          why: str, warn_seconds: int, cause: moves.MoveCause,
                          sources: Mapping[str, str]) -> dict[str, MoveResult]:
    for session in selected:
        locks[session].lock.close()
    try:
        _handoff_batch(list(selected), target, why, warn_seconds, cause, sources)
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        return {session: StopFailed(identity, str(error)) for session, identity in selected.items()}
    return {session: MoveHandedOff(identity) for session, identity in selected.items()}


def _check_stay(identity: str, pane: str, account: str, pid: int, generation: str,
                requester: str = "", why: str = "") -> str:
    """Observe the started generation after the command returns; recovery belongs to the watchdog."""
    settings = conf.read("shutdown")
    stay_seconds = settings.number("STAY_SECONDS")
    began = monotonic()
    while _same_process(pid, generation):
        remaining = stay_seconds - (monotonic() - began)
        if remaining <= 0:
            return ""
        time.sleep(operation_deadline.remaining(min(0.1, remaining)))
    return _early_exit(identity, pane, account, monotonic() - began, requester=requester, why=why,
                       generation=generation)


def _send_move_push(push: RenderedPush, key: str) -> None:
    from scripts.message import send_budget, send_result
    from scripts.notify import push_texts, review

    try:
        window = conf.read("shutdown").whole("MOVE_ALERT_REPEAT_MINUTES")
        limit = review.user_send_seconds()
        command = push_texts.user_send_argv(push, key=key, timeout=limit)
        command.extend(("--repeat-minutes", str(window)))
        done = subprocess.run(command, capture_output=True, text=True, check=False,
                              timeout=send_budget.user_seconds(limit))
    except (OSError, ValueError, subprocess.SubprocessError):
        print("shutdown: could not send the move result", file=sys.stderr)
        return
    result = send_result.send_outcome(done.returncode, done.stdout)
    if not (isinstance(result, send_result.ConfirmedSendOutcome) and
            result.header.outcome in ("sent", "skipped", "kept", "replaced")):
        print("shutdown: the sender did not confirm the move result", file=sys.stderr)


def _early_exit(identity: str, pane: str, account: str, elapsed: float, *, requester: str = "",
                why: str = "", failure: str = "", generation: str = "") -> str:
    """Build one actionable outcome for the showrunner, user push and stay log."""
    from scripts.production import showrunners, staff_sources
    from scripts.accounts import unit_watchdog
    from scripts.notify.push_texts import render

    try:
        record = staff.snapshot()
    except (staff.StaffUnreadable, ValueError) as error:
        print(f"{identity}: staff anomaly: {error}", file=sys.stderr)
        record = staff.StaffSnapshot(())
    row = record.by_current_session(identity)
    entry = pane_record.read(herdr.shared_session()).get(pane)
    entry = entry if entry and (pane_launch.reference_id(entry) == identity
        or row is not None and record.by_member_reference(pane_launch.reference_id(entry)) == row) else None
    environment = entry["environment"] if entry else {}
    name = row.name if row is not None else str(entry.get("name", "")) if entry else ""
    name = name or identity
    unit = row.name if row is not None and row.kind == "unit" else ""
    if unit:
        try:
            recovery = unit_watchdog.run(datetime.now(timezone.utc), dry_run=False)
            next_step = "; ".join(recovery) if recovery else "watchdog reported no recovery action"
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            next_step = f"watchdog could not recover it: {error}"
    else:
        command = "agents resume " + shlex.join([name, "--to", account, "--why", why or "retry after early exit"])
        next_step = f"resume it: {command}"
    cause = (f"{name}: resume on {account} failed: {failure}; {next_step}" if failure else
             f"{name} stopped {elapsed:.1f} s after its move or resume to {account}; {next_step}")
    try:
        name = staff_sources.unit_parent(unit, identity).name if unit else requester
        if name:
            print(showrunners.tell(name, f"resume-ended-{identity}-{uuid4().hex}", cause, sender="shutdown"))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"{identity}: could not tell showrunner: {error}")
    if not _move_resume.get() and environment.get(PROBE_ENV) != "1":
        key = sha256(f"{identity}\0{account}\0{generation}".encode()).hexdigest()
        _send_move_push(render("shutdown.restart_failed", account=account, machine=remote.local_machine(),
                               stopped=(row.name if row is not None else str(entry.get("name", "")) if entry else "") or "one session",
                               cause=cause), f"resume-ended:{key}")
    return cause


def _warn_session(session: str, target: str, why: str, seconds: int) -> None:
    tab = _tab_of(session)
    if isinstance(tab, SessionNotFound):
        return
    _ = subprocess.run(session_send.send_argv("--to", f"session:{tab.session_id}", "--from", "shutdown",
        "--text", f"Account move in {seconds} seconds: {why}. You will resume on {target}; continue your work and workers."),
        capture_output=True, timeout=15, check=False)


def _prepare_move_request(reference: str, plan: MoveStopTarget, target: str, why: str, cause: moves.MoveCause,
                          fallback_source: str, names: LaunchNames, *,
                          intent: moves.FollowGroupAccount | moves.ExplicitMoveTarget) -> moves.RequestedMove:
    """Build a request from the proved live source or the retained source."""
    source = plan.source.session.account if isinstance(plan.source, LiveMoveSource) else fallback_source
    row = plan.member.staff_row(names.tab_record)
    identity = plan.identity
    label = note_file.parse_account_label(target)
    if not isinstance(label, note_file.NumberedAccountLabel):
        raise ValueError(f"invalid move target: {target}")
    tool = label.tool
    previous: moves.HistoricalAccountLabel = source or moves.HomeAccountUnresolved(Path.home() / f".{tool}")
    if not source:
        entries = [entry for entry in names.history if pane_launch.reference_id(entry) in
                   (identity, row.id if row is not None else identity) or entry.get("name") == reference]
        saved = max(entries, key=lambda entry: entry["written_at"], default=None)
        directory = pane_launch.config_home(saved if saved is not None else {"session_id": identity}, record=names.tab_record)
        if isinstance(directory, staff.SessionHome):
            home = directory.path
            label = note_file.parse_account_label(home.name.removeprefix("."))
            previous = label.label if isinstance(label, note_file.NumberedAccountLabel) else moves.HomeAccountUnresolved(home)
        else:
            print(f"anomaly: {identity}: session home is unknown; retain the normal source account", file=sys.stderr)
    return {"kind": "requested", "at": datetime.now(timezone.utc).isoformat(),
        "move_id": uuid4().hex, "tool": tool, "subject": f"{identity}: {row.name}" if row is not None else reference,
        "from": previous, "to": target, "why": why, "window": "unknown", "cause": cause,
        "scope": "session" if intent is moves.ExplicitMoveTarget.REQUESTED_ACCOUNT else "group"}


def _record_move_outcome(request: moves.RequestedMove, result: ClaudeUp | StopFailed | SessionNotFound | ResumePending) -> None:
    """Record one final outcome without losing the batch results."""
    try:
        entry: moves.ConfirmedMove | moves.FailedMove
        if isinstance(result, ClaudeUp):
            entry = moves.ConfirmedMove(kind="confirmed", at=datetime.now(timezone.utc).isoformat(),
                move_id=request["move_id"], tool=request["tool"], subject=request["subject"],
                worker=request["subject"].split(": ", 1)[-1])
        else:
            reason = f"{result.name}: session not found" if isinstance(result, SessionNotFound) else result.reason
            entry = moves.FailedMove(kind="failed", at=datetime.now(timezone.utc).isoformat(),
                move_id=request["move_id"], tool=request["tool"], subject=request["subject"], reason=reason)
        moves.append_move(entry)
    except Exception as error:
        identity = result.name if isinstance(result, SessionNotFound) else result.session_id
        print(f"{identity}: move log anomaly: {error}", file=sys.stderr)


def _confirm_move_start(result: ClaudeUp, requester: str, why: str) -> ClaudeUp | ResumeFailed:
    """Keep the existing stay observation inside the move's outcome."""
    identity = result.session_id
    account = note_file.parse_account_label(result.account)
    if isinstance(account, note_file.NumberedAccountLabel) and account.tool == "codex":
        observed = running_on_account(identity, result.pane_id, kind="codex")
        if isinstance(observed, ClaudeUp) and observed.account == result.account and isinstance(observed.pid, int):
            failure = _check_stay(identity, result.pane_id, result.account, observed.pid,
                                  sessions.process_start(observed.pid) or "", requester, why)
            return ResumeFailed(identity, failure) if failure else result
        reason = (observed.reason if isinstance(observed, ResumeFailed) else
                  f"Codex did not start on {result.account}")
        failure = _early_exit(identity, result.pane_id, result.account, 0.0,
                              requester=requester, why=why, failure=reason)
        return ResumeFailed(identity, failure)
    live = sessions.session_liveness(identity)
    failure = (_check_stay(identity, result.pane_id, result.account, live.pid,
                           sessions.process_start(live.pid) or "", requester, why)
               if isinstance(live, sessions.LiveSessionProcess) else
               _early_exit(identity, result.pane_id, result.account, 0.0, requester=requester, why=why))
    return ResumeFailed(identity, failure) if failure else result


def _resume_move(identity: str, target: str, requester: str, why: str, began: float,
                 client: herdr.Herdr) -> ResumeResult:
    token = _move_resume.set(True)
    try:
        try:
            result = (resume(identity, why=why, to=target, requester=requester, client=client) if requester else
                      resume(identity, why=why, to=target, client=client))
        except Exception as error:
            result = ResumeFailed(identity, str(error))
        if isinstance(result, ClaudeUp):
            return _confirm_move_start(replace(result, elapsed_seconds=monotonic() - began), requester, why)
        reason = "session has no tab after stop" if isinstance(result, SessionNotFound) else result.reason
        _ = _early_exit(identity, "", target, 0.0, requester=requester, why=why, failure=reason)
        return result
    finally:
        _move_resume.reset(token)


def _verify_build_holders(selected: list[str]) -> dict[str, tuple[str, ...]] | BuildQueueUnreadable:
    """Keep live verify calls, including calls that have not entered the admission queue."""
    from scripts.build_hold.owning_session import OwningSession, RunNotInStaff, RunOwner, owning_session, run_resolution
    from scripts.direct import step_record
    from scripts.lib import process_start

    directory = step_record.record_directory()
    try:
        paths = sorted(path for path in directory.iterdir() if path.suffix == ".json")
    except FileNotFoundError:
        return {}
    except OSError as error:
        return BuildQueueUnreadable(f"verify records are unreadable: {error}")
    owned: dict[str, list[str]] = {}
    for path in paths:
        record = step_record.read(path.stem)
        if isinstance(record, (step_record.MissingRecord, step_record.StaleRecord)):
            continue
        if isinstance(record, step_record.UnreadableRecord):
            return BuildQueueUnreadable(f"verify call {record.call_id} is unreadable: {record.reason}")
        try:
            if sys.platform == "darwin":
                result = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(record.pid)],
                    capture_output=True, text=True, timeout=5, check=False)
                if result.returncode or "HOME=" not in result.stdout:
                    raise OSError("process environment is unreadable")
                entries = result.stdout.split()
            else:
                proc = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc"))
                entries = (proc / str(record.pid) / "environ").read_bytes().decode(errors="replace").split("\0")
            environment = dict(entry.split("=", 1) for entry in entries if "=" in entry)
            resolution = run_resolution(environment)
            if isinstance(resolution, RunNotInStaff):
                owner, reason = "", f"named run owner cannot be resolved: {resolution.run}"
            else:
                resolved = (OwningSession(resolution.session_id) if isinstance(resolution, RunOwner)
                            else owning_session(environment))
                owner = resolved.session_id if isinstance(resolved, OwningSession) else ""
                reason = "verify call has no readable session identity" if not owner else ""
        except (OSError, subprocess.SubprocessError) as error:
            owner, reason = "", str(error)
        # The environment belongs to this call only while its saved process identity still matches.
        current = process_start.process_start(record.pid)
        if isinstance(current, (process_start.Gone, process_start.Zombie)):
            continue
        if isinstance(current, process_start.Started) and current.start != record.proc_start:
            continue
        if isinstance(current, process_start.Unreadable):
            return BuildQueueUnreadable(f"verify call {record.call_id} identity is unreadable: {current.reason}")
        if reason:
            return BuildQueueUnreadable(f"verify call {record.call_id}: {reason}")
        if owner in selected:
            owned.setdefault(owner, []).append(f"verify.sh {record.kind} in {record.repo}")
    return {owner: tuple(steps) for owner, steps in owned.items()}


def _build_holders(sessions: list[str]) -> dict[str, tuple[str, ...]] | BuildQueueUnreadable:
    """Name the running, waiting and cloud build steps that each session owns in the admission queue."""
    from scripts.build_queue import admission_client, admission_protocol as wire

    try:
        reading = admission_client.view()
    except wire.AdmissionUnavailable as error:
        return BuildQueueUnreadable(str(error) or "no answer")
    owned: dict[str, list[str]] = {}
    steps = [*((step.session_id, step.step, step.folder) for step in reading.view.running),
             *((step.session_id, step.step, step.folder) for step in reading.view.waiting),
             *((row.identity.session_id, row.identity.step, row.identity.folder)
               for row in (reading.cloud if isinstance(reading, wire.CloudQueueRead) else ()))]
    for session, step, folder in steps:
        if session in sessions:
            owned.setdefault(session, []).append(f"{step} in {folder}" if folder else step)
    verify = _verify_build_holders(sessions)
    if isinstance(verify, BuildQueueUnreadable):
        return verify
    for session, names in verify.items():
        owned.setdefault(session, []).extend(names)
    return {session: tuple(names) for session, names in owned.items()}


def _owns_detached_request(identity: str, request_id: str) -> bool:
    current = moves.read_latest_request(identity)
    return isinstance(current, moves.LatestMoveRequest) and current.request_id == request_id


def _move_lock_command(batch: MoveBatch, identity: str, target: str) -> str:
    if isinstance(batch.origin, NewMoveRequest):
        destination = batch.intent if isinstance(batch.intent, moves.FollowGroupAccount) else target
        return moves.batch_move_command(identity, destination)
    return moves.RETRY_COMMAND


def _lock_move_row(session: str, target: str, cleanup: ExitStack, admitted: Mapping[str, LockedMoveRow],
                   ) -> LockedMoveRow | moves.MoveInProgress | StopFailed | SessionNotFound:
    batch = _move_batch.get()
    member = batch.members[session]
    if member.row_key in admitted:
        previous = admitted[member.row_key]
        batch.members[session] = previous.member
        return previous
    lock = cleanup.enter_context(ExitStack())
    identity = member.reference
    detached = batch.origin
    try:
        owner = None
        if isinstance(member.owner, pane_launch.StaffLaunchReference):
            owner = member.staff_row(staff.initial_snapshot())
            if owner is None:
                return StopFailed(member.reference, "staff member is unavailable")
            identity = str(owner.current_session)
        if isinstance(detached, DetachedMoveRequest) and not _owns_detached_request(identity, detached.request_id):
            return moves.MoveSuperseded(identity)
        record = lock.enter_context(staff.session_operation(identity, native_key=moves.native_row_key(identity)))
        current = record.by_current_session(identity)
        if owner is not None and (current is None or current.id != owner.id):
            lock.close()
            return moves.MoveWaitingForRow(session, "the move owner changed while acquiring its row",
                                           _move_lock_command(batch, identity, target))
        if current is not None:
            member = _staff_move_member(current.id, record)
        batch.members[session] = member
        batch.native_sessions[session] = identity
        return LockedMoveRow(session, member, identity, lock)
    except staff.StaffRefused as error:
        lock.close()
        if isinstance(detached, DetachedMoveRequest):
            try:
                if not _owns_detached_request(identity, detached.request_id):
                    return moves.MoveSuperseded(identity)
            except (OSError, ValueError) as read_error:
                return StopFailed(identity, str(read_error))
        command = _move_lock_command(batch, identity, target)
        active = staff.row_activity(member.row_key)
        if isinstance(active, staff.RowMoving):
            try:
                account = (accounts.current_move_account(batch.intent.group)
                           if isinstance(batch.intent, moves.FollowGroupAccount) else target)
            except (OSError, ValueError, subprocess.SubprocessError):
                return moves.MoveWaitingForRow(session, str(error), command)
            if active.target == account:
                return moves.MoveInProgress(session, account, command)
        reason = str(error) if isinstance(active, staff.RowAvailable) else "another operation holds this row"
        return moves.MoveWaitingForRow(session, reason, command)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        lock.close()
        return StopFailed(session, str(error))


def _native_move_member(reference: str, entries: Mapping[str, pane_record.PaneLaunch]) -> ResolvedMoveMember:
    saved = max((entry for entry in entries.values() if pane_launch.reference_id(entry) == reference),
                key=lambda entry: entry["written_at"], default=None)
    name: object = saved.get("name") if saved is not None else None
    return ResolvedMoveMember(pane_launch.NativeLaunchReference(reference), name if isinstance(name, str) else "")


def _staff_move_member(identity: staff.StaffId, record: StaffSnapshot) -> ResolvedMoveMember:
    row = record.by_id(identity)
    return ResolvedMoveMember(pane_launch.StaffLaunchReference(identity), record.qualified_name(row) if row else "")


def _batch_member(reference: str, record: StaffSnapshot,
                  entries: Mapping[str, pane_record.PaneLaunch]) -> ResolvedMoveMember:
    if (row := record.by_member_reference(reference)) is None:
        named = [row for row in record.rows if row.name == reference]
        row = named[0] if len(named) == 1 else None
    if row is not None:
        return ResolvedMoveMember(pane_launch.StaffLaunchReference(row.id), record.qualified_name(row))
    named = pane_launch.named_references(reference, record, entries)
    if len(named) == 1 and isinstance(native := next(iter(named)), pane_launch.NativeLaunchReference):
        return _native_move_member(native.session, entries)
    return ResolvedMoveMember(pane_launch.NativeLaunchReference(reference), "")


def _alert_pending_moves(batch: MoveBatch, entries: Mapping[str, pane_record.PaneLaunch]) -> None:
    from scripts.notify import pushover
    from scripts.notify.push_texts import render

    probes = {pane_launch.reference_id(entry) for entry in entries.values() if entry["environment"].get(PROBE_ENV) == "1"}
    names: list[str] = []
    for session, result in batch.results.items():
        if isinstance(result, (ClaudeUp, MoveHandedOff, BuildMoveDeferred, moves.MoveSuperseded, moves.MoveWaitingForRow)):
            continue
        member = batch.members[session]
        identity = batch.native_sessions.get(session)
        if identity is None or member.reference in probes or identity in probes or session in probes:
            continue
        try:
            current = moves.read_latest_request(identity)
        except (OSError, ValueError):
            continue
        if isinstance(current, moves.LatestMoveRequest) and current.request_id == batch.request_id:
            names.append(member.name or "unnamed session")
    if not names:
        return
    machine = remote.local_machine()
    group = batch.intent.group if isinstance(batch.intent, moves.FollowGroupAccount) else ""
    destination = f"for {group}" if group else f"to {batch.target}"
    push = render("alert.decision", sender="shutdown", title="Some sessions still need to move",
                  action=f"Retry pending moves on {machine} with `{moves.RETRY_COMMAND}`.",
                  body=f"These sessions did not finish their move {destination}: " + ", ".join(names) + ".")
    if len(push.message) > pushover.phone_message_limit(push.sender, push.id):
        push = render(push.id, **(push.values | {"body":
            f"{len(names)} sessions did not finish their move {destination}. "
            + f"Read the pending list on {machine} with `{moves.MOVE_COMMAND} result`."}))
    _send_move_push(push, f"account-move:{group}:{batch.target}")


def move_sessions(sessions: list[str], target: str, *, why: str, warn_seconds: int = 0,
                  requester: str = "", cause: moves.MoveCause = "by hand",
                  source_accounts: Mapping[str, str] = _NO_SOURCE_ACCOUNTS,
                  request: DetachedMoveRequest | NewMoveRequest | moves.PendingMoveRequest = _NEW_MOVE_REQUEST,
                  intent: moves.FollowGroupAccount | moves.ExplicitMoveTarget =
                  moves.ExplicitMoveTarget.REQUESTED_ACCOUNT,
                  staff_members: Collection[staff.StaffId] = ()) -> list[MoveResult]:
    """Move available sessions and report busy rows for a manual retry."""
    selected = list(dict.fromkeys(sessions))
    if not selected:
        return []
    members: dict[str, ResolvedMoveMember] = {}
    batch = MoveBatch(members, intent, request, target)
    if isinstance(request, DetachedMoveRequest):
        batch.request_id = request.request_id
    try:
        record = staff.initial_snapshot()
        entries = pane_record.read_strict(herdr.shared_session())
        settings = conf.read("shutdown")
        by_owner: dict[pane_launch.StaffLaunchReference | pane_launch.NativeLaunchReference, str] = {}
        aliases: dict[str, str] = {}
        sources: dict[str, str] = {}
        for session in selected:
            fixed_member = (session in staff_members
                            or isinstance(request, DetachedMoveRequest) and session in request.staff_members)
            if isinstance(request, (moves.PendingMoveRequest, DetachedMoveRequest)) and not fixed_member:
                member = _native_move_member(session, entries)
            else:
                member = (_staff_move_member(staff.StaffId(session), record)
                          if fixed_member else _batch_member(session, record, entries))
            primary = by_owner.setdefault(member.owner, session)
            aliases[session] = primary
            _ = members.setdefault(primary, member)
            if label := source_accounts.get(session):
                _ = sources.setdefault(primary, label)
        selected = list(members)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        if isinstance(error, staff.StaffUnreadable) and cause != "by hand":
            reason = f"move deferred: build ownership is unreadable ({error})"
            if not isinstance(request, DetachedMoveRequest):
                print(f"build wait: {reason}")
            failure = BuildMoveDeferred
        else:
            reason = str(error)
            failure = StopFailed
        results: list[MoveResult] = []
        for session in sessions:
            if isinstance(request, DetachedMoveRequest) and session not in request.staff_members:
                try:
                    if not _owns_detached_request(session, request.request_id):
                        results.append(moves.MoveSuperseded(session))
                        continue
                except (OSError, ValueError) as read_error:
                    results.append(StopFailed(session, str(read_error)))
                    continue
            results.append(failure(session, reason))
        return results
    token = _move_batch.set(batch)
    try:
        try:
            results = _run_moves(selected, target, why=why, warn_seconds=max(0, warn_seconds),
                                 requester=requester, cause=cause, source_accounts=sources, settings=settings)
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
            results = [batch.results.get(session, StopFailed(session, str(error))) for session in selected]
        batch.results.update(zip(selected, results))
        return [batch.results[aliases[session]] for session in sessions]
    finally:
        _move_batch.reset(token)
        _alert_pending_moves(batch, entries)


def _read_move_source(member: ResolvedMoveMember, identity: str, record: StaffSnapshot, *, check_presence: bool
                      ) -> LiveMoveSource | StoppedMoveSource | StopFailed | SessionNotFound:
    owner = member.staff_row(record)
    if owner is None and isinstance(member.owner, pane_launch.StaffLaunchReference):
        return StopFailed(member.reference, "staff member is unavailable")
    if owner is not None and owner.current_session != identity:
        return moves.MoveWaitingForRow(identity, "the move owner changed while holding its row", moves.RETRY_COMMAND)
    try:
        publication = pane_record.publication_for_session(identity, record=record)
        if owner is not None and owner.kind == "producer":
            kind = models_registry.launch_settings("producers", owner.name).kind
        elif publication is not None:
            kind = pane_launch.agent_kind(publication.entry, record=record)
        else:
            kind = "claude"
            if owner is not None:
                try:
                    kind = pane_launch.current_kind(owner)
                except ValueError:
                    pass  # The stop still requires exact native process proof.
        tab = _tab_of(member.reference)
        running = running_on_account(identity, tab.pane_id if isinstance(tab, herdr.TabSession) else "",
                                     kind=kind, capture_generation=True)
        if isinstance(running, ResumeFailed):
            return StopFailed(identity, running.reason)
        if isinstance(running, LiveMoveSource):
            if running.session.account == "unknown account":
                return StopFailed(identity, "live process account is unreadable")
            return running
        if check_presence and owner is None:
            target = stop_target(identity, strict=True)
            if isinstance(target, StopFailed):
                return target
            if isinstance(target, SessionNotFound) and publication is None and isinstance(tab, SessionNotFound):
                return target
        return StoppedMoveSource()
    except (OSError, ValueError, staff.StaffRefused, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        return StopFailed(identity, str(error))


def _prepare_move(row: LockedMoveRow, account: str, record: StaffSnapshot, *, check_presence: bool
                  ) -> MoveStopTarget | ClaudeUp | StopFailed | SessionNotFound:
    source = _read_move_source(row.member, row.identity, record, check_presence=check_presence)
    if isinstance(source, (StopFailed, SessionNotFound)):
        return source
    try:
        if isinstance(source, LiveMoveSource) and source.session.account == account:
            if (owner := row.member.staff_row(record)) is not None:
                kind = "codex" if account.startswith("codex") else "claude"
                member_account.repair_home(owner, kind, source.pid)
            return source.session
        if row.marked_target != account:
            _ = row.lock.enter_context(staff.mark_row_move(row.member.row_key, account))
            row.marked_target = account
        return MoveStopTarget(row.member, row.identity, source)
    except (OSError, ValueError, staff.StaffRefused, herdr_types.HerdrFailure) as error:
        return StopFailed(row.identity, str(error))


def _warn_moves(plans: Mapping[str, MoveStopTarget], target: str, why: str, seconds: int) -> None:
    warning_until = monotonic() + seconds
    with ThreadPoolExecutor(max_workers=len(plans)) as pool:
        warnings = [pool.submit(_warn_session, plan.member.reference, target, why, seconds) for plan in plans.values()]
        for warning in warnings:
            try:
                warning.result()
            except Exception:
                pass  # A failed warning does not prevent the requested stop.
    _ = Event().wait(max(0.0, warning_until - monotonic()))


def _remove_latest_move(identity: str, request_id: str) -> None:
    try:
        moves.remove_latest_request(identity, request_id)
    except (OSError, ValueError) as error:
        print(f"shutdown: could not remove the latest move request: {error}", file=sys.stderr)


def _claim_move_request(row: LockedMoveRow, batch: MoveBatch, target: str, *, new: bool) -> bool:
    destination = f"group:{batch.intent.group}" if isinstance(batch.intent, moves.FollowGroupAccount) else target
    claim = moves.LatestMoveRequest(batch.request_id, destination,
                                   explicit_group_scope=batch.intent is moves.ExplicitMoveTarget.GROUP_ACCOUNT)
    try:
        current = moves.read_latest_request(row.identity)
        retry = batch.origin
        if ((not new and current != claim)
                or (new and isinstance(retry, moves.PendingMoveRequest)
                    and (retry.member.reference != row.identity or current != retry.request))):
            batch.results[row.session] = moves.MoveSuperseded(row.identity)
            row.lock.close()
            return False
        if new:
            name = retry.member.name if isinstance(retry, moves.PendingMoveRequest) else row.member.name
            moves.write_latest_request(row.identity, claim, name=name)
    except (OSError, ValueError) as error:
        batch.results[row.session] = StopFailed(row.identity, f"cannot record or read the latest move request: {error}")
        row.lock.close()
        return False
    return True


def _run_moves(selected: list[str], target: str, *, why: str, warn_seconds: int, requester: str,
               cause: moves.MoveCause, source_accounts: Mapping[str, str], settings: conf.ConfigRead) -> list[MoveResult]:
    batch = _move_batch.get()
    results = batch.results
    locks: dict[str, LockedMoveRow] = {}
    admitted: dict[str, LockedMoveRow] = {}
    aliases: dict[str, str] = {}
    held: dict[str, tuple[str, ...]] = {}
    warned: set[str] = set()
    build_deadline = math.inf
    check_presence = isinstance(batch.origin, moves.PendingMoveRequest)
    with ExitStack() as cleanup:
        for session in selected:
            row = _lock_move_row(session, target, cleanup, admitted)
            if not isinstance(row, LockedMoveRow):
                results[session] = row
                continue
            if row.session != session:
                aliases[session] = row.session
                continue
            admitted[row.member.row_key] = row
            admitted[moves.native_row_key(row.identity)] = row
            if _claim_move_request(row, batch, target, new=not isinstance(batch.origin, DetachedMoveRequest)):
                locks[session] = row
        while locks:
            with herdr.process_action():
                account = (accounts.current_move_account(batch.intent.group)
                           if isinstance(batch.intent, moves.FollowGroupAccount) else target)
                batch.target = account
                record = staff.initial_snapshot()
                ready: dict[str, MoveStopTarget] = {}
                for session, row in tuple(locks.items()):
                    prepared = _prepare_move(row, account, record, check_presence=check_presence)
                    if isinstance(prepared, MoveStopTarget):
                        ready[session] = prepared
                    else:
                        results[session] = prepared
                        if isinstance(prepared, (ClaudeUp, SessionNotFound)):
                            _remove_latest_move(row.identity, batch.request_id)
                        row.lock.close()
                if _detached_moves.get():
                    if ready:
                        results.update(_handoff_locked_moves({session: session for session in ready}, locks, account,
                                                            why, warn_seconds, cause, source_accounts))
                    break
                reading = (_build_holders([plan.identity for plan in ready.values()])
                           if ready and cause != "by hand" else {})
                next_held: dict[str, tuple[str, ...]] = {}
                for session, plan in tuple(ready.items()):
                    if isinstance(reading, BuildQueueUnreadable):
                        steps = held.get(session, ())
                        if not steps:
                            reason = f"move deferred: build ownership is unreadable ({reading.reason})"
                            print(f"build wait: {reason}")
                            results[session] = BuildMoveDeferred(session, reason)
                            locks[session].lock.close()
                            del ready[session]
                            continue
                    else:
                        steps = reading.get(plan.identity, ())
                    if steps:
                        if session not in held:
                            print(f"build wait: {session} owns {'; '.join(steps)}; the move waits for its steps to end")
                        next_held[session] = steps
                        del ready[session]
                held = next_held
                if ready:
                    caller = os.environ.get("CLAUDE_CODE_SESSION_ID")
                    if any(plan.identity == caller for plan in ready.values()):
                        _record_launches([plan.member.reference for plan in ready.values()])
                        chosen = {session: plan.identity for session, plan in ready.items()}
                        chosen.update((session, session) for session in held)
                        sources = dict(source_accounts) | {session: plan.source.session.account for session, plan in ready.items()
                                                          if isinstance(plan.source, LiveMoveSource)}
                        results.update(_handoff_locked_moves(chosen, locks, account, why, warn_seconds, cause, sources))
                        break
                    unwarned = {session: plan for session, plan in ready.items() if session not in warned}
                    if warn_seconds and unwarned:
                        _warn_moves(unwarned, account, why, warn_seconds)
                        warned.update(unwarned)
                        locks = {session: locks[session] for session in (*held, *ready)}
                        continue
                    ready = {session: plan for session, plan in ready.items()
                             if _claim_move_request(locks[session], batch, target, new=False)}
                    if ready:
                        moved = _move_now(list(ready), account, why=why, requester=requester,
                                          cause=cause, source_accounts=source_accounts, plans=ready, locks=locks,
                                          admitted_record=record)
                        results.update(zip(ready, moved))
            if not held:
                break
            if not _batch_process.get():
                results.update(_handoff_locked_moves({session: session for session in held}, locks, target,
                                                     why, warn_seconds, cause, source_accounts))
                break
            now = monotonic()
            if build_deadline == math.inf:
                build_deadline = now + settings.whole("MOVE_BUILD_WAIT_MINUTES") * 60
            if now >= build_deadline:
                for session, steps in held.items():
                    reason = f"move deferred: build ownership remains: {'; '.join(steps)}"
                    print(f"build wait limit: {session}: {reason}")
                    results[session] = BuildMoveDeferred(session, reason)
                break
            locks = {session: locks[session] for session in held}
            time.sleep(min(settings.whole("MOVE_BUILD_POLL_SECONDS"), build_deadline - now))
    return [results[aliases.get(session, session)] for session in selected]


def _stop_move(attempt: MoveStopAttempt) -> StopResult:
    token = _move_stop_target.set(attempt)
    try:
        return _stop(attempt.target.member.reference)
    finally:
        _move_stop_target.reset(token)


def _move_now(selected: list[str], target: str, *, why: str, requester: str,
              cause: moves.MoveCause, source_accounts: Mapping[str, str], plans: Mapping[str, MoveStopTarget],
              locks: Mapping[str, LockedMoveRow], admitted_record: StaffSnapshot) -> list[MoveResult]:
    """Stop the selected rows before resuming any of them."""
    from scripts.herdr import worker_panes

    began = monotonic()
    batch = _move_batch.get()
    label = note_file.parse_account_label(target)
    if not isinstance(label, note_file.NumberedAccountLabel) or label.tool == "claude":
        checked = accounts.check_move_target(target, datetime.now(timezone.utc))
        if isinstance(checked, accounts.MoveRefused):
            for session in selected:
                locks[session].lock.close()
            return [StopFailed(plans[session].identity, checked.reason) for session in selected]
    _record_launches([plan.member.reference for plan in plans.values()])
    try:
        names = LaunchNames.read()
    except Exception as error:
        for session in selected:
            locks[session].lock.close()
        return [StopFailed(plans[session].identity, f"could not record the move request: {error}") for session in selected]
    attempts: dict[str, MoveStopAttempt] = {}
    failures: dict[str, StopFailed] = {}
    for session, plan in plans.items():
        attempt = MoveStopAttempt(plan, partial(_prepare_move_request, session, plan, target, why, cause,
                                                source_accounts.get(session, ""), names, intent=batch.intent))
        attempts[session] = attempt
        if isinstance(plan.source, StoppedMoveSource):
            try:
                attempt.record_request()
            except MoveRequestRefused as error:
                failures[session] = StopFailed(plan.identity, str(error))
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        role_failures: dict[str, StopFailed] = {}
        worker_policies: dict[StaffId, worker_panes.CheckedRolePolicy] = {}
        needs_role: dict[str, MoveStopTarget] = {}
        for session, plan in plans.items():
            row = plan.member.staff_row(admitted_record)
            if row is None or row.kind != "worker":
                continue
            try:
                if pane_record.publication_for_session(plan.identity, record=admitted_record) is None:
                    needs_role[session] = plan
            except (OSError, ValueError) as error:
                role_failures[session] = StopFailed(plan.identity, str(error))
        if needs_role:
            try:
                record = staff.initial_snapshot()
            except (OSError, ValueError) as error:
                for session, plan in needs_role.items():
                    role_failures[session] = StopFailed(plan.identity, f"anomaly: cannot read worker role: {error}")
            else:
                for session, plan in needs_role.items():
                    row = plan.member.staff_row(record)
                    if row is None or row.kind != "worker":
                        continue
                    policy = worker_panes.checked_role_policy(worker_panes.dispatch_role(row))
                    if isinstance(policy, worker_panes.StartRefused):
                        role_failures[session] = StopFailed(plan.identity, f"{policy.reason.value}: {policy.detail}")
                    else:
                        worker_policies[row.current_session] = policy
        for session, failure in role_failures.items():
            attempt = attempts[session]
            if isinstance(attempt.request, MoveRequestNotRecorded):
                try:
                    attempt.record_request()
                except MoveRequestRefused as error:
                    print(f"{failure.session_id}: {error}", file=sys.stderr)
            failures[session] = failure
        stops = {session: pool.submit(_stop_move, attempt)
                 for session, attempt in attempts.items() if session not in failures}
        stopped: list[StopResult] = []
        for session in selected:
            if session in failures:
                stopped.append(failures[session])
                continue
            try:
                result = stops[session].result()
                if isinstance(result, (Stopped, NotRunning)):
                    if isinstance(attempts[session].request, MoveRequestNotRecorded):
                        result = StopFailed(plans[session].identity, "the move request was not recorded")
                    else:
                        _retire_prompt(result.session_id)
                stopped.append(result)
            except Exception as error:
                stopped.append(StopFailed(session, str(error)))

        client = herdr.Herdr(herdr.shared_session())

        def resume_stopped(identity: str) -> ResumeResult:
            with worker_panes.use_checked_resume_policies(worker_policies):
                return _resume_move(identity, target, requester, why, began, client)

        resumes = {pool.submit(resume_stopped, result.session_id): (index, result.session_id)
                   for index, result in enumerate(stopped) if isinstance(result, (Stopped, NotRunning))}
        outcomes: dict[int, ClaudeUp | StopFailed | SessionNotFound | ResumePending] = {}
        for index, result in enumerate(stopped):
            if isinstance(result, (StopFailed, SessionNotFound)):
                outcomes[index] = result
                session = selected[index]
                if (attempt := attempts.get(session)) is not None and not isinstance(attempt.request, MoveRequestNotRecorded):
                    _record_move_outcome(attempt.request, result)
                locks[session].lock.close()
        for future in as_completed(resumes):
            index, identity = resumes[future]
            try:
                resumed = future.result()
                if isinstance(resumed, ClaudeUp):
                    outcome = resumed
                elif isinstance(resumed, SessionNotFound):
                    outcome = ResumePending(identity, target, "session has no tab after stop")
                else:
                    outcome = ResumePending(identity, target, resumed.reason)
            except Exception as error:
                outcome = ResumePending(identity, target, str(error))
            outcomes[index] = outcome
            session = selected[index]
            request = attempts[session].request
            if not isinstance(request, MoveRequestNotRecorded):
                _record_move_outcome(request, outcome)
            if isinstance(outcome, ClaudeUp):
                _remove_latest_move(locks[session].identity, batch.request_id)
            locks[session].lock.close()
    return [outcomes[index] for index in range(len(selected))]


def move(session: str, target: str, *, why: str) -> MoveResult:
    return move_sessions([session], target, why=why)[0]


def _load_batch_modules() -> None:
    """Load deferred move modules before the batch can wait."""
    for name in (
        # Restart imports this module. Worker restore also uses deferred imports.
        "scripts.agents.restart",
        "scripts.production.worker",
        "scripts.agents.inventory",
        "scripts.astra.design_roles",
        "scripts.herdr.turn_end",
        "scripts.direct.assign",
        "scripts.direct.remove_run_scratch",
        "scripts.lint.tmp_sweep",
        "scripts.message.failed_lookup",
        "scripts.message.send",
        "scripts.message.roster",
        # Stops retire prompts. Failed starts can enter the unit watchdog.
        "scripts.production.unit_lookup",
        "scripts.production.stall_watch",
        "scripts.production.staff_sources",
        "scripts.accounts.unit_watchdog",
        "scripts.maintenance.settings",
        "scripts.whoami.quota_alert",
        "scripts.message.process_table",
        "scripts.production.unit_names",
        # Launches can need these modules to check the target environment.
        "scripts.lib.copy_record",
        "scripts.lib.slot_root",
        # Automatic moves use the admission queue and verify process records.
        "scripts.build_queue.admission_client",
        "scripts.build_queue.admission_protocol",
        "scripts.build_hold.owning_session",
        "scripts.build_hold.override_guard",
        "scripts.direct.step_record",
    ):
        _ = import_module(name)


def _print_retry_result(result: MoveResult) -> int:
    if isinstance(result, SessionNotFound):
        print("Removed the pending move for a session that no longer exists.")
        return 0
    if isinstance(result, moves.MoveSuperseded):
        print(f"not moved: {result.reason}")
        return 0
    if isinstance(result, (moves.MoveWaitingForRow, moves.MoveInProgress)) and result.command:
        print(f"Move pending. Run again: {result.command}")
        return 1
    if isinstance(result, (ClaudeUp, MoveHandedOff)):
        return 0
    print(f"Move failed: {result.reason}", file=sys.stderr)
    return 1


def _pending_target(target: str) -> tuple[str, moves.FollowGroupAccount | moves.ExplicitMoveTarget]:
    if target.startswith("group:"):
        group = target.removeprefix("group:")
        if group not in accounts.CLAUDE_GROUPS:
            raise ValueError("invalid pending move group")
        return "", moves.FollowGroupAccount(group)
    return target, moves.ExplicitMoveTarget.REQUESTED_ACCOUNT


@dataclass(frozen=True)
class MoveStillPending:
    account: str
    intent: moves.FollowGroupAccount | moves.ExplicitMoveTarget


def _reconcile_move(identity: str, name: str, destination: str, request: moves.LatestMoveRequest | LegacyMoveReport
                     ) -> MoveStillPending | ClaudeUp | SessionNotFound | StopFailed:
    try:
        with staff.session_operation(identity, native_key=moves.native_row_key(identity)) as record:
            if isinstance(request, moves.LatestMoveRequest) and moves.read_latest_request(identity) != request:
                return moves.MoveSuperseded(identity)
            target, intent = _pending_target(destination)
            if isinstance(intent, moves.FollowGroupAccount):
                target = accounts.current_move_account(intent.group)
            elif isinstance(request, moves.LatestMoveRequest) and request.explicit_group_scope:
                intent = moves.ExplicitMoveTarget.GROUP_ACCOUNT
            owner = record.by_current_session(identity)
            member = (_staff_move_member(owner.id, record) if owner is not None
                      else ResolvedMoveMember(pane_launch.NativeLaunchReference(identity), name))
            source = _read_move_source(member, identity, record, check_presence=True)
            if isinstance(source, StopFailed):
                return source
            if isinstance(source, SessionNotFound) or isinstance(source, LiveMoveSource) and source.session.account == target:
                if isinstance(request, moves.LatestMoveRequest):
                    moves.remove_latest_request(identity, request.request_id)
                return source if isinstance(source, SessionNotFound) else source.session
            return MoveStillPending(target, intent)
    except staff.StaffRefused as error:
        command = moves.RETRY_COMMAND if isinstance(request, moves.LatestMoveRequest) else ""
        return moves.MoveWaitingForRow(identity, str(error), command)
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        return StopFailed(identity, str(error))


def _pending_move_command(command: str, request_id: str) -> int:
    code = 0
    found = False
    announced = False
    seen: set[str] = set()

    def reconcile(member: moves.MoveMember, destination: str,
                  request: moves.PendingMoveRequest | LegacyMoveReport) -> None:
        nonlocal code, found, announced
        identity = member.reference
        if identity in seen:
            return
        seen.add(identity)
        current = request.request if isinstance(request, moves.PendingMoveRequest) else request
        state = _reconcile_move(identity, member.name, destination, current)
        if isinstance(state, (ClaudeUp, moves.MoveSuperseded)):
            return
        found = True
        if isinstance(state, SessionNotFound):
            action = "Removed the pending move" if isinstance(request, moves.PendingMoveRequest) else "Skipped a legacy move"
            print(f"{member.name}: {action} for a session that no longer exists.")
        elif isinstance(state, StopFailed):
            reason = " ".join(state.reason.splitlines())
            advice = f" Run again: {state.command}" if isinstance(state, moves.MoveWaitingForRow) and state.command else ""
            print(f"{member.name}: could not check the pending move: {reason}.{advice}", file=sys.stderr if not advice else sys.stdout)
            code = 1
        elif isinstance(request, LegacyMoveReport):
            target = state.intent if isinstance(state.intent, moves.FollowGroupAccount) else state.account
            print(f"{member.name}: start a new move with {moves.batch_move_command(identity, target)}")
        elif command == "result":
            print(f"{member.name}: {destination}")
        else:
            if not announced:
                print("The retry starts a new move request.")
                announced = True
            result, = move_sessions([identity], state.account, why="retry account move", request=request, intent=state.intent)
            code |= _print_retry_result(result)

    try:
        for item in sorted(moves.pending_move_requests(), key=lambda item: item.member.name):
            if not request_id or item.request.request_id == request_id:
                reconcile(item.member, item.request.target, item)
        for batch_id in (request_id,) if request_id else moves.legacy_batch_ids():
            try:
                saved = moves.read_batch_result(batch_id)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, RecursionError):
                print(f"Could not read legacy move report {batch_id}.json.", file=sys.stderr)
                code = 1
                continue
            if not saved.pending:
                continue
            record = staff.initial_snapshot()
            entries = pane_record.read_strict(herdr.shared_session())
            destination = f"group:{saved.target.group}" if isinstance(saved.target, moves.FollowGroupAccount) else saved.target
            for member in saved.pending:
                resolved = _batch_member(member.reference, record, entries)
                owner = resolved.staff_row(record)
                identity = str(owner.current_session) if owner is not None else resolved.reference
                reconcile(moves.MoveMember(identity, member.name), destination, LegacyMoveReport())
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        print(f"shutdown: could not read the move requests: {error}", file=sys.stderr)
        return 1
    if not found and not code:
        print("No sessions need a move.")
    return code


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    batch = commands.add_parser("batch")
    _ = batch.add_argument("--requester", default="")
    _ = batch.add_argument("--target", default="")
    _ = batch.add_argument("--why", default="account move")
    _ = batch.add_argument("--cause", choices=("by hand", "refusal", "auto", "credits off"), default="by hand")
    _ = batch.add_argument("--warn-seconds", type=int, default=0)
    _ = batch.add_argument("--source-account", action="append", default=[])
    _ = batch.add_argument("--request-id", default="", help=argparse.SUPPRESS)
    _ = batch.add_argument("--group", choices=accounts.CLAUDE_GROUPS, help=argparse.SUPPRESS)
    _ = batch.add_argument("--explicit-group-scope", action="store_true", help=argparse.SUPPRESS)
    _ = batch.add_argument("--staff-member", action="append", default=[], help=argparse.SUPPRESS)
    _ = batch.add_argument("--log", type=Path)
    _ = batch.add_argument("--preload-restarted", action="store_true", help=argparse.SUPPRESS)
    _ = batch.add_argument("sessions", nargs="+")
    retry = commands.add_parser("retry")
    _ = retry.add_argument("id", nargs="?", default="")
    result_command = commands.add_parser("result")
    _ = result_command.add_argument("id", nargs="?", default="")
    stay = commands.add_parser("stay")
    for option in ("session", "pane", "account", "generation"):
        _ = stay.add_argument("--" + option, required=True)
    _ = stay.add_argument("--requester", default="")
    _ = stay.add_argument("--why", default="")
    _ = stay.add_argument("--pid", type=int, required=True)
    _ = stay.add_argument("--log", type=Path, default=shutdown_state_root() / "stay.log")
    args = parser.parse_args(argv)
    if cast(str, args.command) in ("retry", "result"):
        return _pending_move_command(cast(str, args.command), cast(str, args.id))
    # Even a directly invoked batch must not hand itself off a second time.
    _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    if cast(str, args.command) == "stay":
        log = cast(Path, args.log)
        with log.open("a", encoding="utf-8") as output, redirect_stdout(output):
            cause = _check_stay(cast(str, args.session), cast(str, args.pane), cast(str, args.account),
                                cast(int, args.pid), cast(str, args.generation), cast(str, args.requester), cast(str, args.why))
            _ = output.write(cause + "\n")
        return int(bool(cause))
    # The batch is the one process that may wait for build steps; its output goes to the move log.
    group = cast(accounts.ClaudeGroup | None, args.group)
    target = cast(str, args.target)
    if not (target or group):
        batch.error("provide --target for an explicit move or --group for a group move")
    token = _batch_process.set(True)
    intent: moves.FollowGroupAccount | moves.ExplicitMoveTarget
    if group is not None:
        intent = moves.FollowGroupAccount(group)
    elif cast(bool, args.explicit_group_scope):
        intent = moves.ExplicitMoveTarget.GROUP_ACCOUNT
    else:
        intent = moves.ExplicitMoveTarget.REQUESTED_ACCOUNT
    request_id = cast(str, args.request_id)
    request = (DetachedMoveRequest(request_id, frozenset(cast(list[str], args.staff_member)))
               if request_id else _NEW_MOVE_REQUEST)
    try:
        log = cast(Path | None, args.log)
        if log is None:
            log = shutdown_state_root() / "move.log"
            log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as output, redirect_stdout(output):
            try:
                _load_batch_modules()
            except (ImportError, AttributeError) as error:
                if cast(bool, args.preload_restarted):
                    raise
                print(f"batch startup: module preload failed ({type(error).__name__}); restarting in a new interpreter",
                      flush=True)
                os.execv(sys.executable, [sys.executable, __file__, "batch", "--preload-restarted", *argv[1:]])
            results = move_sessions(cast(list[str], args.sessions), target,
                                    why=cast(str, args.why), warn_seconds=cast(int, args.warn_seconds),
                                    requester=cast(str, args.requester), cause=cast(moves.MoveCause, args.cause),
                                    request=request, intent=intent,
                                    source_accounts={identity: label for item in cast(list[str], args.source_account)
                                                     for identity, _, label in (item.partition("="),)})
            for result in results:
                fields = asdict(result)
                if isinstance(result, moves.MoveSuperseded):
                    fields["reason"] = f"not moved: {result.reason}"
                elif isinstance(result, moves.MoveWaitingForRow) and result.command:
                    fields["reason"] = f"Move pending: {result.reason}. Run again: {result.command}"
                elif isinstance(result, moves.MoveInProgress) and result.command:
                    fields["reason"] = f"Move pending. Run again: {result.command}"
                _ = output.write(json.dumps({"result": type(result).__name__, **fields}) + "\n")
    finally:
        _batch_process.reset(token)
    return int(any(not isinstance(result, (ClaudeUp, MoveHandedOff)) for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
