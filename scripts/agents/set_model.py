"""Set a staff role's model default and verify the change in each live pane."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from scripts.agents import remote, worker_lifecycle
from scripts.herdr import herdr
from scripts.lib import conf, models_registry, operation_deadline, registry_rows
from scripts.lib.transcripts import open_transcript
from scripts.message import codex_daemon
from scripts.production import session_staff, staff, unit_lookup, worker_admission
from scripts.production.session_pane import HerdrFailure

COMMAND = "set-model"
LOCAL_COMMAND = "_" + COMMAND + "-local"
ARGUMENTS = "ROLE MODEL [EFFORT]"
TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]*")
ROLE_KINDS: dict[str, staff.Kind] = {
    "producer": "producer",
    "unit": "unit",
    "showrunner": "showrunner",
    "worker": "worker",
}
DESIGN_TASKS: dict[staff.WorkerRole, str] = {"astra-designer": "designer", "astra-reviewer": "reviewer"}


@dataclass(frozen=True)
class Role:
    kind: staff.Kind
    worker_roles: tuple[staff.WorkerRole, ...] = ()


@dataclass(frozen=True)
class ScreenChecks:
    delay: float
    reads: int
    pane_timeout: float


@dataclass(frozen=True)
class ScreenModel:
    model: str = ""
    effort: str = ""


def select_role(word: str) -> Role:
    singular = word.removesuffix("s")
    if singular == "director":
        singular = "unit"
    if singular in ROLE_KINDS:
        return Role(ROLE_KINDS[singular])
    for role in staff.WORKER_ROLES:
        if role == singular:
            return Role("worker", (role,))
    raise ValueError(f"Unknown role {word}. Valid roles: {', '.join((*ROLE_KINDS, *staff.WORKER_ROLES))}")


def role_rows(record: staff.StaffSnapshot, role: Role) -> list[staff.StaffRow]:
    return sorted(
        (row for row in staff.active(record, role.kind)
         if isinstance(row.retirement, staff.Active)
         and (not role.worker_roles
              or isinstance(row.role, staff.KnownWorkerRole) and row.role.value in role.worker_roles)),
        key=lambda row: (row.name, row.id),
    )


def _change(section: str, key: str, value: str) -> registry_rows.RowChange:
    return registry_rows.RowChange(section, key, f"{key}={value}")


def default_changes(role: Role, word: str, target: models_registry.LaunchSettings) -> list[registry_rows.RowChange]:
    pair = f"{word}:{target.effort}"
    profile = f"{target.kind}:{pair}"
    if role.kind == "producer":
        return [_change("producer_defaults", "model", profile)]
    if role.kind in ("unit", "showrunner"):
        key = "director" if role.kind == "unit" else "showrunner"
        return [_change(f"production.{target.kind}", key, pair)]
    changes: list[registry_rows.RowChange] = []
    for task in role.worker_roles or staff.WORKER_ROLES:
        if task in DESIGN_TASKS:
            if target.kind == "codex":
                changes.append(_change("design_roles.codex", DESIGN_TASKS[task], pair))
            elif role.worker_roles:
                raise ValueError(f"{task} sessions start on Codex. Choose a Codex model.")
        else:
            changes.append(_change(f"direct.{target.kind}", task, pair))
            changes.append(_change("assignments", f"direct.{task}", target.kind))
    return changes


_CLAUDE_MODEL = r"(?:claude-)?[a-zA-Z]+[ -]\d+(?:[.-]\d+)*"
DISPLAY_EFFORT = r"low|medium|high|xhigh|max|ultra"


class CodexRolloutResult(StrEnum):
    MATCH = "match"
    PENDING = "pending"
    SETTINGS_CONFLICT = "settings conflict"
    UNCONFIRMED = "unconfirmed"
    MIXED_INPUT = "input mixed with user text"


@dataclass(frozen=True)
class CodexRolloutOffset:
    complete_bytes: int
    partial_line: bool = False


@dataclass(frozen=True)
class CodexTranscriptCursor:
    thread: str
    home: Path
    offsets: dict[Path, CodexRolloutOffset]

    @classmethod
    def before_send(cls, client: herdr.Herdr, pane: str, identity: str) -> CodexTranscriptCursor:
        process = client.native_pane_process(pane, "codex", operation_deadline.remaining(herdr.PROGRAM_TIMEOUT_SECONDS))
        if process.current_identity != identity:
            raise HerdrFailure(f"{pane}: the conversation changed", "wrong_target")
        directory = process.environment.get("CODEX_HOME")
        home = Path(directory) if directory else Path(process.environment.get("HOME", "")) / ".codex"
        if not home.is_absolute():
            raise ValueError("the selected pane's Codex account is unresolved")
        try:
            source = worker_admission.codex_transcript_source(identity, home)
        except ValueError:
            return cls(identity, home, {})
        with open_transcript(source) as stream:
            offset = herdr.complete_transcript_offset(stream)
            position = CodexRolloutOffset(offset, stream.seek(0, io.SEEK_END) != offset)
            return cls(identity, home, {source: position})

    def check_settings(self, command: str, target: models_registry.LaunchSettings) -> CodexRolloutResult:
        try:
            source = worker_admission.codex_transcript_source(self.thread, self.home)
        except ValueError:
            return CodexRolloutResult.PENDING
        position = self.offsets.get(source, CodexRolloutOffset(0))
        if self.offsets and (source not in self.offsets or
                             (source.suffix != ".zst" and source.stat().st_size < position.complete_bytes)):
            raise ValueError("unconfirmed: the Codex rollout changed during submission")
        result = CodexRolloutResult.PENDING
        saw_user_prompt = False
        started_before_send = position.partial_line
        for row in worker_admission.transcript_records(
                source, offset=position.complete_bytes,
                boundary=worker_admission.TranscriptRecordBoundary.COMPLETE_LINES_WITH_TAIL):
            if isinstance(row, worker_admission.IncompleteTranscriptTail):
                return CodexRolloutResult.UNCONFIRMED if saw_user_prompt else CodexRolloutResult.PENDING
            if isinstance(row, worker_admission.TornTranscriptRecord):
                raise ValueError("unconfirmed: the Codex rollout has a malformed record")
            if started_before_send:
                started_before_send = False
                continue
            raw = row.get("payload")
            if not isinstance(raw, dict):
                continue
            payload = cast(dict[str, object], raw)
            record_type = row.get("type")
            payload_type = payload.get("type")
            if ((record_type == "event_msg" and payload_type == "user_message")
                    or (record_type == "response_item" and payload_type == "message"
                        and payload.get("role") == "user")):
                if command in worker_admission.transcript_entry(row, set()):
                    return CodexRolloutResult.MIXED_INPUT
                saw_user_prompt = True
                continue
            if (record_type != "event_msg" or payload_type != "thread_settings_applied"
                    or payload.get("thread_id") != self.thread):
                continue
            snapshot = payload.get("thread_settings")
            settings = cast(dict[str, object], snapshot) if isinstance(snapshot, dict) else {}
            matches = settings.get("model") == target.model and settings.get("reasoning_effort") == target.effort
            result = CodexRolloutResult.MATCH if matches else CodexRolloutResult.SETTINGS_CONFLICT
        return CodexRolloutResult.UNCONFIRMED if saw_user_prompt else result


def _claude_model_key(model: str) -> str:
    return re.sub(r"[ .]", "-", model.lower().removeprefix("claude-"))


def current_claude_model(footer: tuple[str, ...]) -> ScreenModel:
    """Read the model and effort that Claude displays now."""
    for line in footer:
        for field in line.split("|"):
            if status := re.fullmatch(rf"\s*({_CLAUDE_MODEL})(?:\s+({DISPLAY_EFFORT}))?(?:\s+·.*)?\s*", field):
                return ScreenModel(_claude_model_key(status[1]), status[2] or "")
    return ScreenModel()


def _new_output(before: tuple[str, ...], after: tuple[str, ...]) -> tuple[str, ...]:
    if not before:
        return after
    if not after:
        return ()
    # Keep the longest unchanged suffix of the earlier screen.
    for start in range(len(before)):
        common = len(before) - start
        if before[start:] == after[:common]:
            return after[common:]
    return ()


def _claude_receipt(lines: tuple[str, ...], previous_model: str) -> ScreenModel:
    current = ScreenModel(previous_model)
    for line in lines:
        line = line.lstrip(" │└╰⎿●•").rstrip()
        if result := re.match(rf"(?:Set model to |Model set to |Kept model as )({_CLAUDE_MODEL})(?=\s|\(|\[|$)", line):
            effort = re.search(r"\(effort: (\w+)\)|with (\w+) effort", line)
            current = ScreenModel(_claude_model_key(result[1]), "" if effort is None else effort[1] or effort[2])
        if result := re.match(r"Set effort level to ([\w-]+)(?=\s|:|$)", line):
            current = ScreenModel(current.model, result[1].lower())
    return current


def _wait_for_claude_model(client: herdr.Herdr, pane: str, terminal: str, identity: str, before: tuple[str, ...],
                           command: str, target: models_registry.LaunchSettings, checks: ScreenChecks, *,
                           require_effort: bool, previous_model: str = "") -> str:
    wanted = _claude_model_key(target.model)
    prompt_pattern = re.compile(r"^ {0,2}[❯>][ \xa0](.*)$")
    saw_command = False
    for _ in range(checks.reads):
        time.sleep(operation_deadline.remaining(checks.delay))
        after = client.command_screen(pane, terminal, target=identity)
        lines = _new_output(before, after.history)
        for index, line in enumerate(lines):
            if prompt := prompt_pattern.fullmatch(line):
                lines = lines[index + 1:]
                following = next((row for row in lines if row.strip()), "")
                continuation = following.startswith("  ") and not following.lstrip().startswith("⎿")
                if prompt[1] != command or continuation:
                    return "input mixed with user text"
                saw_command = True
                break
        else:
            continue
        current = current_claude_model(after.footer)
        if current.model and current.model != wanted:
            continue
        if require_effort and current.effort and current.effort != target.effort:
            continue
        if current.model == wanted and (not require_effort or current.effort):
            return ""
        receipt = _claude_receipt(lines, previous_model)
        if receipt.model == wanted and (not require_effort or receipt.effort == target.effort):
            return ""
    if not saw_command:
        return "the screen did not confirm the command prompt"
    return "the screen did not confirm the model and effort" if require_effort else "the screen did not confirm the model"


def _update_codex(client: herdr.Herdr, pane: str, terminal: str, identity: str,
                  target: models_registry.LaunchSettings, checks: ScreenChecks) -> str:
    deadline = worker_lifecycle.effective_deadline(time.monotonic() + checks.pane_timeout)
    interval = checks.delay or herdr.PANE_POLL_SECONDS
    command = f"/model {target.model} --reasoning={target.effort}"
    transcript = CodexTranscriptCursor.before_send(client, pane, identity)
    _ = client.idle_command(pane, terminal, command, target=identity, codex=True)
    while time.monotonic() < deadline:
        result = transcript.check_settings(command, target)
        if result in (CodexRolloutResult.MIXED_INPUT, CodexRolloutResult.UNCONFIRMED):
            return result.value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
        if result is not CodexRolloutResult.MATCH:
            continue
        if time.monotonic() >= deadline:
            break
        try:
            client.check_command_target(pane, terminal, target=identity, codex=True)
        except TimeoutError:
            break
        if time.monotonic() >= deadline:
            break
        result = transcript.check_settings(command, target)
        if result is CodexRolloutResult.MATCH:
            return "" if time.monotonic() < deadline else CodexRolloutResult.UNCONFIRMED.value
        if result is CodexRolloutResult.SETTINGS_CONFLICT:
            break
        if result is not CodexRolloutResult.PENDING:
            return result.value
    return CodexRolloutResult.UNCONFIRMED.value


def update_pane(place: unit_lookup.Place, identity: str, target: models_registry.LaunchSettings,
                checks: ScreenChecks) -> str:
    client = herdr.Herdr(place.session)
    pane = place.pane
    terminal = client.command_terminal(pane)
    if target.kind == "codex":
        return _update_codex(client, pane, terminal, identity, target, checks)
    screen = client.command_screen(pane, terminal, target=identity)
    current = current_claude_model(screen.footer)
    wanted = _claude_model_key(target.model)
    if current == ScreenModel(wanted, target.effort):
        return ""
    if current.model != wanted:
        command = f"/model {target.model}"
        before = client.idle_command(pane, terminal, command, target=identity, codex=False)
        problem = _wait_for_claude_model(client, pane, terminal, identity, before, command, target, checks,
                                        require_effort=False)
        if problem:
            return problem
    command = f"/effort {target.effort}"
    before = client.idle_command(pane, terminal, command, target=identity, codex=False)
    return _wait_for_claude_model(client, pane, terminal, identity, before, command, target, checks,
                                 require_effort=True, previous_model=wanted)


class SessionState(StrEnum):
    PANE = "pane"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    NO_PANE = "no pane"
    CONFIRMED = "confirmed"
    FAILED = "failed"


@dataclass(frozen=True)
class SessionResult:
    state: SessionState
    reason: str = ""


def _observe(rows: list[staff.StaffRow]) -> dict[staff.StaffId, unit_lookup.LabeledSession | SessionResult]:
    if not rows:
        return {}
    try:
        observed = unit_lookup.observed_session_labels(probe_address=False)
        daemons = codex_daemon.read_daemons(os.environ)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {row.id: SessionResult(SessionState.UNKNOWN, str(error)) for row in rows}
    daemon_ids: set[str] = set()
    daemon_problems: list[str] = []
    for daemon in daemons:
        if isinstance(daemon.sessions, codex_daemon.DaemonUnreadable):
            daemon_problems.append(f"could not read the Codex daemon at {daemon.owner.socket}: {daemon.sessions.reason}")
        else:
            daemon_ids.update(session.thread_id for session in daemon.sessions)
    results: dict[staff.StaffId, unit_lookup.LabeledSession | SessionResult] = {}
    for row in rows:
        owner = observed.checked_conversation(row.current_session)
        if isinstance(owner, unit_lookup.ProvedLiveUnit) and isinstance(owner.owner, unit_lookup.LabeledSession):
            results[row.id] = owner.owner
        elif row.current_session in daemon_ids or isinstance(owner, unit_lookup.ProvedLiveUnit):
            results[row.id] = SessionResult(SessionState.NO_PANE, "the live session has no herdr pane")
        elif isinstance(owner, unit_lookup.UnknownUnit):
            results[row.id] = SessionResult(SessionState.UNKNOWN, owner.reason)
        elif daemon_problems:
            results[row.id] = SessionResult(SessionState.UNKNOWN, daemon_problems[0])
        else:
            results[row.id] = SessionResult(SessionState.ABSENT)
    return results


def _claude_selection(pane: unit_lookup.LabeledSession, owner: unit_lookup.LiveClaude,
                      target: models_registry.LaunchSettings) -> models_registry.LaunchSettings:
    try:
        process = herdr.Herdr(pane.place.session).native_pane_process(
            pane.place.pane, "claude", operation_deadline.remaining(herdr.PROGRAM_TIMEOUT_SECONDS))
        if process.current_identity != owner.session_id:
            raise ValueError("the process changed conversations")
        if isinstance(pane.generation, session_staff.ProvedProcessGeneration) and (
                pane.generation.pid != owner.pid or pane.generation.started != process.started):
            raise ValueError("the pane process changed")
        config_dir = process.environment.get("CLAUDE_CONFIG_DIR")
        if not config_dir:
            home = Path(process.environment.get("HOME", ""))
            if not home.is_absolute():
                raise ValueError("the process home is unknown")
            config_dir = str(home / ".claude")
        if not Path(config_dir).is_absolute():
            raise ValueError("the process configuration directory is unknown")
    except (OSError, ValueError, HerdrFailure, subprocess.SubprocessError) as error:
        operation_deadline.check()
        raise ValueError("the selected pane's Claude account is unresolved") from error
    environment = {**os.environ, models_registry.OPERATION_ENVIRONMENT[0]: "", "CLAUDE_CONFIG_DIR": config_dir}
    model = models_registry.claude_model(target.model, environment, timeout=operation_deadline.remaining())
    operation_deadline.check()
    if not TOKEN.fullmatch(model) or not re.fullmatch(_CLAUDE_MODEL, model):
        raise ValueError(f"could not resolve {target.model} to one Claude model version")
    return target._replace(model=model)


def _apply(row: staff.StaffRow, pane: unit_lookup.LabeledSession, target: models_registry.LaunchSettings,
           checks: ScreenChecks) -> SessionResult:
    owner = pane.claude
    kind = "claude" if isinstance(owner, unit_lookup.LiveClaude) else "codex"
    if kind != target.kind:
        return SessionResult(SessionState.FAILED, f"the session runs {kind.title()}. {target.model} requires {target.kind.title()}.")
    try:
        with worker_lifecycle.operation_deadline(timeout=checks.pane_timeout):
            if isinstance(owner, unit_lookup.LiveClaude):
                target = _claude_selection(pane, owner, target)
            problem = update_pane(pane.place, row.current_session, target, checks)
            if problem:
                return SessionResult(SessionState.FAILED, problem)
            operation_deadline.check()
    except TimeoutError:
        return SessionResult(SessionState.FAILED, "the pane exceeded its update deadline")
    except (OSError, ValueError, HerdrFailure, subprocess.SubprocessError) as error:
        return SessionResult(SessionState.FAILED, str(error))
    return SessionResult(SessionState.CONFIRMED)


def _checks(settings: conf.ConfigRead) -> ScreenChecks:
    return ScreenChecks(settings.number("screen_wait_seconds"), settings.whole("screen_reads"),
                        settings.number("pane_timeout_seconds"))


def _peer_results(rows: list[staff.StaffRow], command: list[str], allowed: tuple[SessionState, ...], *,
                  limit: remote.RemoteCallLimit = remote.STANDARD_TIME_LIMIT) -> dict[staff.StaffId, SessionResult]:
    if not rows:
        return {}
    identities = [value for row in rows for value in (row.id, row.current_session)]
    status, reply = remote.run_remote([LOCAL_COMMAND, *command, *identities], limit=limit)
    host = remote.other_machine()
    payload: dict[str, object] = {}
    error = ""
    for line in reply.splitlines():
        try:
            fields = cast(object, json.loads(line))
            if not isinstance(fields, dict):
                raise ValueError("expected session results")
            payload.update(cast(dict[str, object], fields))
        except ValueError as problem:
            error = f"invalid reply from {host}: {problem}"
    if status and not error:
        error = f"could not check {host}: exit {status}"
    results: dict[staff.StaffId, SessionResult] = {}
    for row in rows:
        match payload.get(row.id):
            case [str() as state, str() as reason] if state in allowed:
                results[row.id] = SessionResult(SessionState(state), reason)
            case _:
                results[row.id] = SessionResult(
                    SessionState.UNKNOWN, error or f"{host} did not report a result for the session")
    return results


def on_remote(arguments: list[str]) -> int:
    """Observe or update the specified conversations on this host."""
    match arguments:
        case ["observe", *identities]:
            target = None
        case ["apply", "claude" | "codex" as family, model, effort, *identities] if TOKEN.fullmatch(model) and TOKEN.fullmatch(effort):
            target = models_registry.LaunchSettings(family, model, effort, "")
        case _:
            raise ValueError("invalid remote model request")
    record = staff.initial_snapshot()
    rows: list[staff.StaffRow] = []
    results: dict[staff.StaffId, SessionResult] = {}
    for staff_id, conversation in zip(identities[::2], identities[1::2], strict=True):
        identity = staff.StaffId(staff_id)
        row = record.by_id(identity)
        if row is None or not isinstance(row.retirement, staff.Active) or row.current_session != conversation:
            results[identity] = SessionResult(SessionState.UNKNOWN, "the staff conversation changed or is unavailable")
        else:
            rows.append(row)
    observed = _observe(rows)
    checks = _checks(conf.read("set_model"))
    for row in rows:
        owner = observed[row.id]
        if isinstance(owner, unit_lookup.LabeledSession):
            results[row.id] = SessionResult(SessionState.PANE) if target is None else _apply(row, owner, target, checks)
        elif target is not None and owner.state == SessionState.ABSENT:
            results[row.id] = SessionResult(SessionState.FAILED, "the pane stopped before the update")
        else:
            results[row.id] = owner
        if target is not None:
            result = results.pop(row.id)
            print(json.dumps({row.id: [result.state, result.reason]}), flush=True)
    if results:
        print(json.dumps({identity: [result.state, result.reason] for identity, result in results.items()}), flush=True)
    return 0


def _update_sessions(rows: list[staff.StaffRow], target: models_registry.LaunchSettings, checks: ScreenChecks
                     ) -> dict[staff.StaffId, SessionResult]:
    local = _observe(rows)
    peer = _peer_results(rows, ["observe"],
                         (SessionState.PANE, SessionState.NO_PANE, SessionState.ABSENT, SessionState.UNKNOWN))
    results: dict[staff.StaffId, SessionResult] = {}
    remote_rows: list[staff.StaffRow] = []
    for row in rows:
        here, there = local[row.id], peer[row.id]
        if isinstance(here, unit_lookup.LabeledSession):
            if there.state == SessionState.PANE:
                results[row.id] = SessionResult(SessionState.FAILED, "both hosts report a pane for the conversation")
            else:
                results[row.id] = _apply(row, here, target, checks)
        elif there.state == SessionState.PANE:
            remote_rows.append(row)
        elif here.state == SessionState.NO_PANE:
            results[row.id] = here
        elif here.state == SessionState.UNKNOWN and there.state != SessionState.NO_PANE:
            results[row.id] = here
        else:
            results[row.id] = there
    return {**results, **_peer_results(remote_rows, ["apply", target.kind, target.model, target.effort],
                                       (SessionState.CONFIRMED, SessionState.FAILED, SessionState.UNKNOWN,
                                        SessionState.NO_PANE), limit={"kind": "while link alive"})}


def _save(path: Path, before: str, after: str, description: str, label: str, *,
          preserve_producer_rows: bool = False) -> tuple[str, bool]:
    try:
        saved = models_registry.save_registry(path, before, after, description,
                                             preserve_producer_rows=preserve_producer_rows)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"{label}: local save failed: {error}", file=sys.stderr)
        print(f"{label}: publication skipped", file=sys.stderr)
        return before, False
    local = "saved locally" if saved.changed else "local rows are current"
    print(f"{label}: {local}", file=sys.stderr)
    detail = f": {saved.detail}" if saved.detail else ""
    print(f"{label}: publication {saved.publication}{detail}", file=sys.stderr)
    return saved.text, saved.publication != models_registry.RegistryPublication.UNCONFIRMED


def run(role_name: str, model: str, effort: str) -> int:
    role = select_role(role_name)
    settings = conf.read("set_model")
    effort = effort or settings.text("default_effort")
    if not TOKEN.fullmatch(model) or not TOKEN.fullmatch(effort):
        raise ValueError("model and effort must be single names")
    configuration = models_registry.edit_base({**os.environ, models_registry.OPERATION_ENVIRONMENT[1]: ""})
    path, before = configuration.path, configuration.text
    target = models_registry.requested_model(model, effort, configuration)
    rows = role_rows(staff.initial_snapshot(), role)
    after = registry_rows.replay(before, default_changes(role, model, target), False)
    description = f"set {role_name} to {model} {effort}"
    registry_ok = True
    if registry_rows.values(before) != registry_rows.values(after):
        after, registry_ok = _save(path, before, after, description, "role default")
    results = _update_sessions(rows, target, _checks(settings))
    if role.kind == "producer":
        profiles = models_registry.parse_registry(after).get("producers", {})
        missing = [_change("producers", row.name, f"{target.kind}:{model}:{target.effort}") for row in rows
                   if row.name not in profiles and results[row.id].state == SessionState.CONFIRMED]
        if missing:
            _, profiles_ok = _save(path, after, registry_rows.replay(after, missing, False),
                                   description, "producer profiles", preserve_producer_rows=True)
            registry_ok = registry_ok and profiles_ok
    failed = False
    for row in rows:
        result = results[row.id]
        if result.state not in (SessionState.CONFIRMED, SessionState.ABSENT):
            failed = True
            print(f"{row.name}: {result.reason}")
    if failed or not registry_ok:
        return 1
    print("100 percent")
    return 0


def report_error(error: Exception) -> int:
    print(f"{COMMAND}: {error}", file=sys.stderr)
    return 2
