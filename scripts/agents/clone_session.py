"""Fork a live conversation into a separate staff session."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import Literal, cast

from scripts.agents import pane_record, producer_creation, producer_lifecycle, remote, set_model, stop_resume
from scripts.herdr import herdr, herdr_types, pane_launch
from scripts.lib import codex_models_cache
from scripts.message import sessions
from scripts.production import staff
from scripts.whoami.account import NAMED_ACCOUNT_VARIABLES


AgentKind = Literal["claude", "codex"]
_ACCOUNT_VARIABLE = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
_ACCOUNT_KEYS = {
    "claude": frozenset(("HOME", *NAMED_ACCOUNT_VARIABLES, "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                         "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")),
    "codex": frozenset(("HOME", "CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")),
}


def _account_environment(kind: AgentKind, process: herdr.NativeAgentProcess) -> dict[str, str]:
    return {key: process.environment[key] for key in _ACCOUNT_KEYS[kind] if key in process.environment}


def _account_home(kind: AgentKind, process: herdr.NativeAgentProcess) -> Path:
    configured = process.environment.get(_ACCOUNT_VARIABLE[kind])
    path = Path(configured) if configured else Path(process.environment.get("HOME", "")) / f".{kind}"
    if not path.is_absolute():
        raise ValueError(f"{process.current_identity}: the {kind} account home is unknown")
    return path


@dataclass(frozen=True)
class CloneSource:
    name: str
    pane: str
    kind: AgentKind
    launch: herdr.ForegroundLaunch

    @property
    def identity(self) -> str:
        return self.launch.process.current_identity


def _source_id(reference: str, record: staff.StaffSnapshot) -> str:
    member = record.by_member_reference(reference)
    if member is None and "/" in reference:
        member = record.by_path(reference)
    if member is not None:
        return member.current_session
    named = [row for row in record.rows if row.name == reference]
    if len(named) > 1:
        raise ValueError(f"{reference}: several staff rows match; use the staff ID")
    if named:
        return named[0].current_session
    try:
        return staff.native_id(reference)
    except staff.StaffRefused as error:
        raise ValueError(f"{reference}: no staff name matches. Use the native session ID.") from error


def _source(reference: str, record: staff.StaffSnapshot) -> tuple[CloneSource, herdr.Herdr]:
    identity = _source_id(reference, record)
    claude = sessions.session_processes(identity)
    if isinstance(claude, sessions.SessionLivenessUnknown):
        raise ValueError(f"{reference}: {claude.reason}")
    if claude.pids:
        live = herdr.LiveConversation("claude", claude.pids)
    else:
        live = herdr.read_conversation(identity, "codex")
        if not isinstance(live, herdr.LiveConversation):
            reason = live.reason if isinstance(live, herdr.UnknownConversation) else "session is not live"
            raise ValueError(f"{reference}: {reason}")
    places: set[herdr.HerdrPlace] = set()
    for pid in live.pids:
        place = herdr.herdr_place_of(pid)
        if isinstance(place, herdr.UnprovedHerdrPlacement):
            raise ValueError(f"{reference}: {place.reason}")
        if isinstance(place, herdr.HerdrPlace):
            places.add(place)
    if len(places) != 1:
        raise ValueError(f"{reference}: expected one proved live Herdr pane, found {len(places)}")
    place = places.pop()
    client = herdr.Herdr(place.session)
    tabs = client.tab_sessions()
    if any(tab.session_id == identity and tab.pane_id != place.pane for tab in tabs):
        raise ValueError(f"{reference}: several tabs match")
    launch = client.foreground_launch(place.pane, live.kind)
    if launch.process.current_identity != identity or launch.pid not in live.pids:
        raise ValueError(f"{reference}: the source pane changed sessions")
    member = record.by_current_session(identity)
    label = next((tab.label for tab in tabs if tab.pane_id == place.pane and tab.label), reference)
    return CloneSource(member.name if member is not None else label, place.pane, live.kind, launch), client


_CODEX_MODEL = r"gpt-[a-z0-9][a-z0-9.-]*"
_CODEX_MODEL_AND_EFFORT = re.compile(rf"(.+?) (none|minimal|{set_model.DISPLAY_EFFORT})(?: (?:fast|standard|flex))?")


@dataclass(frozen=True)
class LiveForkSettings:
    model: str
    effort: str


def _codex_models(home: Path) -> dict[str, set[str]]:
    try:
        cache = codex_models_cache.read_models_cache(home / "models_cache.json", retry_delays=())
    except codex_models_cache.ModelsCacheUnreadable:
        return {}
    names: dict[str, set[str]] = {}
    for value in cast(list[object], cache["models"]):
        if not isinstance(value, dict):
            continue
        row = cast(dict[str, object], value)
        slug = row.get("slug")
        label = row.get("display_name")
        if isinstance(slug, str) and slug:
            names.setdefault(slug, set()).add(slug)
            if isinstance(label, str) and label:
                names.setdefault(label, set()).add(slug)
    return names


def _codex_model(name: str, catalog: dict[str, set[str]]) -> str:
    if name in catalog:
        slugs = catalog[name]
        return next(iter(slugs)) if len(slugs) == 1 else ""
    return name if re.fullmatch(_CODEX_MODEL, name) else ""


def _live_settings(source: CloneSource, client: herdr.Herdr, home: Path) -> LiveForkSettings:
    terminal = client.command_terminal(source.pane)
    footer = client.command_footer(source.pane, terminal, target=source.identity, codex=source.kind == "codex")
    if source.kind == "claude":
        current = set_model.current_claude_model(footer)
        model = f"claude-{current.model}" if current.model else ""
        effort = current.effort
    else:
        fields = {part.strip() for line in footer for part in line.split(" · ")}
        catalog = _codex_models(home)
        pairs = {(model, match[2]) for part in fields if (match := _CODEX_MODEL_AND_EFFORT.fullmatch(part))
                 and (model := _codex_model(match[1], catalog))}
        if len(pairs) > 1:
            raise ValueError(f"{source.name}: the live pane reports conflicting model and effort")
        model, effort = next(iter(pairs), ("", ""))
    if not model or not effort:
        missing = "model and effort" if not (model or effort) else "effort" if model else "model"
        raise ValueError(f"{source.name}: cannot prove the current {missing} from the live pane")
    return LiveForkSettings(model, effort)


def _arguments(source: CloneSource, settings: LiveForkSettings, name: str) -> list[str]:
    prompt = (f"You are {name}, a clone of {source.name}. Use {name} as your session name. "
              f"Messages to {source.name} are for the source session. "
              "Wait for a request addressed to you.")
    if source.kind == "codex":
        return ["fork", source.identity, "--model", settings.model,
                "-c", f"model_reasoning_effort={json.dumps(settings.effort)}", "--", prompt]
    launched = pane_launch.parse_claude_arguments(source.launch.arguments)
    arguments = pane_record.claude_conversation_arguments(source.identity, name,
        pane_record.ClaudeResumeSettings(settings.model, settings.effort),
        launched.plugin_directories, remote_control=True, fork=True)
    return [*arguments, "--", prompt]


def _admit_clone(name: str, source: CloneSource, client: herdr.Herdr, launched: herdr.ForkLaunched,
                 prepared: herdr.PreparedAgentEnvironment, environment: dict[str, str], home: Path) -> str:
    pane, terminal = launched.pane, launched.terminal
    try:
        native = stop_resume.wait_for_native_session(client, pane,
            monotonic() + herdr.PROGRAM_TIMEOUT_SECONDS, kind=source.kind)
        clone = client.foreground_launch(pane, source.kind)
        if native == source.identity or native != clone.process.current_identity:
            raise ValueError(f"{name}: pane {pane} did not start a separate session")
        if _account_environment(source.kind, clone.process) != environment:
            raise ValueError(f"{name}: pane {pane} did not use the source account environment")
        if Path(clone.cwd).resolve() != prepared.directory:
            raise ValueError(f"{name}: pane {pane} did not start in the source folder")
        if client.command_terminal(pane) != terminal:
            raise ValueError(f"{name}: pane {pane} changed terminals")
        record = staff.initial_snapshot()
        if any(row.name.casefold() == name.casefold() for row in record.all_rows):
            raise ValueError(f"{name}: another staff row has this name")
        if any(native in (row.id, row.current_session) for row in record.all_rows):
            raise ValueError(f"{name}: the fork identity already belongs to another session")
        entry = pane_record.launch(pane, native, clone.cwd, clone.process.environment, clone.arguments, kind=source.kind)
        entry.update(name=name, pid=clone.pid, proc_start=clone.process.started, config_dir=str(home))
        try:
            pane_record.write(client.session, entry)
            try:
                row = staff.create_session(name, native, staff.SessionLaunchFolder(remote.local_machine(), prepared.directory),
                                           staff.SessionHome(home))
            except staff.StaffUnreadable as error:
                # A lost reply can follow a committed staff write.
                raise _AdmissionUnconfirmed(
                    f"{name}: staff admission is unconfirmed. Pane {pane} and its launch are retained."
                ) from error
        except (OSError, ValueError, staff.StaffRefused):
            pane_launch.retire_launch(pane_record.record_path(client.session), pane, entry)
            raise
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError, staff.StaffRefused) as error:
        if not client.close_terminal_pane(pane, terminal):
            raise ValueError(f"{name}: fork pane {pane} closure is unconfirmed ({error})") from error
        raise ValueError(f"{name}: fork pane {pane} closed after refusal ({error})") from error
    try:
        client.remember_row_tab(row.id, pane)
    except (herdr_types.HerdrFailure, OSError, ValueError) as error:
        print(f"{name}: clone admitted. Cannot save the tab position ({error}).", file=sys.stderr)
    return native


class _AdmissionUnconfirmed(RuntimeError):
    """Staff may have committed the row before its reply failed."""


def run(reference: str, name: str) -> int:
    staff.validate_name("session", name)
    producer_lifecycle.check_name(name)
    with producer_creation.operation(name):
        record = staff.initial_snapshot()
        producer_lifecycle.require_free_name(name, record)
        if producer_creation.record_path(name).exists():
            raise ValueError(f"{name}: producer creation is pending")
        source, client = _source(reference, record)
        home = _account_home(source.kind, source.launch.process)
        environment = _account_environment(source.kind, source.launch.process)
        unset = _ACCOUNT_KEYS[source.kind].difference(environment)
        prepared = herdr.prepare_agent_environment(source.launch.cwd, environment, unset)
        if isinstance(prepared, herdr.nix_env.Refused):
            raise ValueError(prepared.reason)
        prepared = replace(prepared, env={**{key: value for key, value in prepared.env.items() if key not in unset},
                                          **environment},
                           unset_environment=(prepared.unset_environment | unset).difference(environment))
        settings = _live_settings(source, client, home)
        if client.foreground_launch(source.pane, source.kind) != source.launch:
            raise ValueError(f"{reference}: source changed before the fork")
        arguments = _arguments(source, settings, name)
        launched = herdr.launch_prepared_agent(client.session, herdr.ForkTab(source.pane, name), prepared,
            source.kind, arguments, herdr.NoOpeningPrompt(), 90_000, agent=name, client=client)
        if isinstance(launched.outcome, herdr.PromptNotSubmitted):
            raise ValueError(f"{name}: {launched.outcome.reason}")
        if not isinstance(launched, herdr.ForkLaunched):
            raise ValueError(f"{name}: cannot prove the fork terminal")
        try:
            native = _admit_clone(name, source, client, launched, prepared, environment, home)
        except _AdmissionUnconfirmed as error:
            raise ValueError(str(error)) from error
    print(f"{name}: cloned {source.name} as {native} in pane {launched.pane}")
    return 0
