"""Fork a live conversation into a separate staff session."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import subprocess
from time import monotonic
from typing import Literal, cast

from scripts.agents import pane_record, producer_creation, producer_lifecycle, remote, stop_resume
from scripts.herdr import herdr, pane_launch
from scripts.production import staff, worker_admission
from scripts.whoami.account import named_account_environment


@dataclass(frozen=True)
class CloneSource:
    name: str
    pane: str
    kind: Literal["claude", "codex"]
    launch: herdr.ForegroundLaunch

    @property
    def identity(self) -> str:
        return self.launch.process.current_identity

    @property
    def account_home(self) -> Path:
        key = "CLAUDE_CONFIG_DIR" if self.kind == "claude" else "CODEX_HOME"
        return Path(self.launch.process.environment.get(key) or Path.home() / f".{self.kind}").expanduser()


def _source(reference: str, record: staff.StaffSnapshot, client: herdr.Herdr) -> CloneSource:
    selected = stop_resume.stop_target(reference)
    if isinstance(selected, stop_resume.StopFailed):
        raise ValueError(selected.reason)
    tabs = client.tab_sessions()
    if isinstance(selected, stop_resume.SessionNotFound):
        matches = {session for tab in tabs if tab.label == reference and (session := tab.session_id)}
        if len(matches) > 1:
            raise ValueError(f"{reference}: several tabs match")
        if matches:
            selected = matches.pop()
        else:
            try:
                selected = staff.native_id(reference)
            except staff.StaffRefused as error:
                raise ValueError(f"{reference}: no live session on this machine") from error
    live = herdr.read_conversation(selected)
    if not isinstance(live, herdr.LiveConversation):
        reason = live.reason if isinstance(live, herdr.UnknownConversation) else "session is not live"
        raise ValueError(f"{reference}: {reason}")
    panes = {place.pane for pid in live.pids if isinstance(place := herdr.herdr_place_of(pid), herdr.HerdrPlace)
             and place.session == client.session}
    if not panes:
        panes = {tab.pane_id for tab in tabs if tab.session_id == selected}
    if len(panes) != 1:
        raise ValueError(f"{reference}: expected one live Herdr pane on this machine")
    pane = panes.pop()
    launch = client.foreground_launch(pane, live.kind)
    if launch.process.current_identity != selected:
        raise ValueError(f"{reference}: the source pane changed sessions")
    member = record.by_current_session(selected)
    name = member.name if member is not None else next((tab.label for tab in tabs if tab.pane_id == pane and tab.label), reference)
    return CloneSource(name, pane, live.kind, launch)


@dataclass(frozen=True)
class CodexForkSettings:
    model: str = ""
    effort: str = ""

    def arguments(self) -> list[str]:
        arguments: list[str] = []
        if self.model:
            arguments.extend(("--model", self.model))
        if self.effort:
            arguments.extend(("-c", f"model_reasoning_effort={json.dumps(self.effort)}"))
        return arguments


def _codex_settings(source: CloneSource) -> CodexForkSettings:
    model = effort = ""
    arguments = iter(source.launch.arguments[1:])
    for flag in arguments:
        option, equals, value = flag.partition("=")
        if option == "--":
            break
        if option in ("--model", "-m"):
            model = value if equals else next(arguments, "")
        elif option in ("--config", "-c"):
            key, _, setting = (value if equals else next(arguments, "")).partition("=")
            if key == "model_reasoning_effort":
                effort = setting.strip("\"'")
            elif key == "model":
                model = setting.strip("\"'")
    try:
        transcript = worker_admission.codex_transcript_source(source.identity, source.account_home)
    except ValueError:
        return CodexForkSettings(model, effort)
    for row in worker_admission.transcript_records(
            transcript, boundary=worker_admission.TranscriptRecordBoundary.COMPLETE_LINES):
        if isinstance(row, worker_admission.TornTranscriptRecord):
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        settings = cast(dict[str, object], payload)
        if row.get("type") != "turn_context":
            if (settings.get("type") != "thread_settings_applied"
                    or settings.get("thread_id") != source.identity
                    or not isinstance(snapshot := settings.get("thread_settings"), dict)):
                continue
            settings = cast(dict[str, object], snapshot)
        if isinstance(current := settings.get("model"), str) and current:
            model = current
        if isinstance(current := settings.get("reasoning_effort", settings.get("effort")), str) and current:
            effort = current
    return CodexForkSettings(model, effort)


def _command(source: CloneSource, name: str) -> tuple[dict[str, str], list[str]]:
    prompt = (f"You are {name}, a clone of {source.name}. Use {name} as your session name. "
              f"Messages to {source.name} are for the source session. "
              "Wait for a request addressed to you before you continue the work.")
    home = source.account_home
    if source.kind == "codex":
        return {"CODEX_HOME": str(home)}, ["fork", source.identity, *_codex_settings(source).arguments(), "--", prompt]
    launched = pane_launch.parse_claude_arguments(source.launch.arguments)
    turn = pane_record.claude_turn_settings(source.identity, home)
    settings = pane_record.claude_resume_settings(turn, fallback=pane_record.ClaudeResumeSettings(
        launched.value(pane_launch.MODEL_FLAGS), launched.value(frozenset({"--effort"}))))
    arguments = pane_record.claude_conversation_arguments(source.identity, name, settings, launched.plugin_directories,
                                                         remote_control=True, fork=True)
    return named_account_environment(home), [*arguments, "--", prompt]


def run(reference: str, name: str) -> int:
    producer_lifecycle.check_name(name)
    with producer_creation.operation(name):
        record = staff.initial_snapshot()
        producer_lifecycle.require_free_name(name, record)
        if producer_creation.record_path(name).exists():
            raise ValueError(f"{name}: producer creation is pending")
        client = herdr.Herdr(herdr.shared_session())
        source = _source(reference, record, client)
        environment, arguments = _command(source, name)
        prepared = herdr.prepare_agent_environment(source.launch.cwd, environment)
        if isinstance(prepared, herdr.nix_env.Refused):
            raise ValueError(prepared.reason)
        if client.foreground_launch(source.pane, source.kind) != source.launch:
            raise ValueError(f"{reference}: source changed before the fork")
        launched = herdr.launch_prepared_agent(client.session, herdr.ForkTab(source.pane, name), prepared,
            source.kind, arguments, herdr.NoOpeningPrompt(), 90_000, agent=name, client=client)
        if isinstance(launched.outcome, herdr.PromptNotSubmitted):
            raise ValueError(f"{name}: {launched.outcome.reason}")
        try:
            native = stop_resume.wait_for_native_session(client, launched.pane,
                monotonic() + herdr.PROGRAM_TIMEOUT_SECONDS, kind=source.kind)
        except subprocess.SubprocessError as error:
            raise ValueError(f"{name}: cannot prove the clone in pane {launched.pane}: {error}") from error
        fresh = staff.initial_snapshot()
        if native == source.identity or fresh.by_member_reference(native) is not None:
            raise ValueError(f"{name}: pane {launched.pane} did not start a separate session")
        if any(row.name.casefold() == name.casefold() for row in fresh.all_rows):
            raise ValueError(f"{name}: a staff row claimed this name during launch")
        row = staff.ensure("producer", name=name, runtime_id=native,
                           worktree=staff.PresentWorktree(remote.local_machine(), prepared.directory))
        _ = staff.set_home(row.id, staff.SessionHome(source.account_home))
        client.remember_row_tab(row.id, launched.pane)
    print(f"{name}: cloned {source.name} as {native} in pane {launched.pane}")
    return 0
