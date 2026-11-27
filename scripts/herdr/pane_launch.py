"""Read pane launch options and resolve current staff and native facts."""
from __future__ import annotations

import fcntl
import json
import os
import sys
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, cast, overload
from enum import Enum
from importlib import import_module

if TYPE_CHECKING:
    from scripts.production.session_labels import StaffLabels

from scripts.message import sessions
from scripts.herdr.place import HerdrPlace
from scripts.herdr import herdr_types
from scripts.lib.probe_session import PROBE_ENV
from scripts.lib import atomic_file, legacy_identity, models_registry
from scripts.production import staff

# The launch environment a resume must give back. Credentials never enter the record.
BERTH_SESSION_ENV = "CARGO_BERTH_SESSION_ID"
KEPT_NAMES = frozenset({"ENABLE_TOOL_SEARCH", BERTH_SESSION_ENV, PROBE_ENV})
# Claude flags whose value the resume repeats; every other argument stays out of the record.
NAME_FLAGS = frozenset({"-n", "--name"})
MODEL_FLAGS = frozenset({"--model"})
PRODUCER_LAUNCH_REQUEST_ENV = "HERDR_PRODUCER_LAUNCH_REQUEST"
CLAUDE_FORK_NAME_ENV = "CLAUDE_DEFER_FORK_PUBLICATION"


class LaunchOptions(TypedDict):
    """Retain pane placement, launch options, and process generation evidence."""

    pane_id: str
    model: str
    codex_policy: NotRequired[list[str]]
    plugin_directories: NotRequired[list[str]]
    remote_control: bool
    environment: dict[str, str]
    # False when the launch flags could not be read: a restore refuses it.
    flags_read: bool
    ended: NotRequired[bool]
    written_at: str
    pid: NotRequired[int]
    proc_start: NotRequired[str]
    producer_binding: NotRequired[bool]
    implicit_claude_home: NotRequired[str]


class StaffPaneLaunch(LaunchOptions):
    """Staff supplies every fact for this retained member reference."""

    staff_id: str


class NativePaneLaunch(LaunchOptions):
    """An unstaffed conversation retains its native identity and launch facts."""

    session_id: str
    name: str
    cwd: str
    kind: NotRequired[Literal["claude", "codex"]]
    config_dir: NotRequired[str]


class CurrentLaunch(NativePaneLaunch):
    """Current launch facts combine staff ownership with retained launch options."""

    staff_id: NotRequired[str]


PaneLaunch = StaffPaneLaunch | NativePaneLaunch


@dataclass(frozen=True)
class ImplicitClaudeHome:
    """Claude uses this HOME with no CLAUDE_CONFIG_DIR."""

    home: Path

    @property
    def config_dir(self) -> Path:
        return self.home / ".claude"

    @property
    def settings_file(self) -> Path:
        return self.home / ".claude.json"

    def environment(self, base: Mapping[str, str]) -> dict[str, str]:
        environment = {**base, "HOME": str(self.home)}
        _ = environment.pop("CLAUDE_CONFIG_DIR", None)
        return environment


class SelectedClaudeAccount(Enum):
    USE_SELECTION = "use the selected account"


def implicit_claude_home(entry: PaneLaunch) -> ImplicitClaudeHome | SelectedClaudeAccount:
    if home := entry.get("implicit_claude_home"):
        return ImplicitClaudeHome(Path(home))
    return SelectedClaudeAccount.USE_SELECTION


def retain_implicit_claude_home(entry: CurrentLaunch, environment: Mapping[str, str]) -> None:
    """Keep the home of a standalone Claude session without storing credentials."""
    home = Path(environment.get("HOME", ""))
    if saved_kind(entry) == "claude" and "CLAUDE_CONFIG_DIR" not in environment and home.is_absolute():
        entry["implicit_claude_home"] = str(home)
        entry["config_dir"] = str(ImplicitClaudeHome(home).config_dir)


class CurrentProcessEvidence(Protocol):
    """The current conversation, process generation, and environment for a producer publication."""

    @property
    def current_identity(self) -> str: ...
    started: str
    environment: dict[str, str]


class NativeProcessReader(Protocol):
    def native_process(self, pid: int, kind: str) -> CurrentProcessEvidence: ...


@dataclass(frozen=True)
class ProducerLaunchRequest:
    """The parent operation's selected conversation, backend, folder, and account."""

    member: staff.StaffId
    session: staff.StaffId
    kind: Literal["claude", "codex"]
    folder: Path
    home: Path

    @property
    def environment_key(self) -> str:
        return PRODUCER_LAUNCH_REQUEST_ENV + self.member.encode().hex()

    def environment(self, base: Mapping[str, str]) -> dict[str, str]:
        """Give the child process the selected launch request."""
        return {**base, self.environment_key: json.dumps({
            "member": self.member, "session": self.session, "kind": self.kind,
            "folder": str(self.folder), "home": str(self.home)})}

    def expected_launch(self, entry: NativePaneLaunch) -> CurrentLaunch:
        expected = cast(CurrentLaunch, entry.copy())
        expected.update(staff_id=self.member, session_id=self.session, kind=self.kind, cwd=str(self.folder), config_dir=str(self.home))
        return expected


class ProducerLaunchFromStaff(Enum):
    CURRENT = "publication outside a parent launch operation"


def process_producer_launch_request(member: staff.StaffId) -> ProducerLaunchRequest | ProducerLaunchFromStaff:
    """Read the launch request from the publisher's process environment."""
    encoded = os.environ.get(PRODUCER_LAUNCH_REQUEST_ENV + member.encode().hex())
    if encoded is None:
        return ProducerLaunchFromStaff.CURRENT
    request = cast(dict[str, str], json.loads(encoded))
    return ProducerLaunchRequest(staff.StaffId(request["member"]), staff.StaffId(request["session"]),
        cast(Literal["claude", "codex"], request["kind"]), Path(request["folder"]), Path(request["home"]))


@contextmanager
def producer_launch_request(request: ProducerLaunchRequest) -> Generator[None]:
    """Give direct publishers the same request as the child process."""
    previous = os.environ.get(request.environment_key)
    os.environ.update(request.environment({}))
    try:
        yield
    finally:
        if previous is None:
            del os.environ[request.environment_key]
        else:
            os.environ[request.environment_key] = previous


_producer_publication_configuration = ContextVar[models_registry.LaunchModelConfiguration | models_registry.FreshLaunchConfiguration](
    "producer_publication_model", default=models_registry.FreshLaunchConfiguration()
)


@contextmanager
def producer_publication_configuration(configuration: models_registry.LaunchModelConfiguration) -> Generator[None]:
    """Use the launch configuration when publishing a producer pane."""
    scope = _producer_publication_configuration.set(configuration)
    try:
        yield
    finally:
        _producer_publication_configuration.reset(scope)


@dataclass(frozen=True)
class StaffLaunchReference:
    """The fixed staff owner supplies the current launch facts."""

    id: staff.StaffId


@dataclass(frozen=True)
class NativeLaunchReference:
    """An unstaffed native session retains its saved launch facts."""

    session: str


def reference_id(entry: Mapping[str, object]) -> str:
    """Read the retained staff or native reference without projecting staff facts."""
    return str(entry.get("staff_id", entry.get("session_id", "")))


def launch_reference(entry: Mapping[str, object], record: staff.StaffSnapshot) -> StaffLaunchReference | NativeLaunchReference:
    if isinstance(identity := entry.get("staff_id"), str):
        return StaffLaunchReference(staff.StaffId(identity))
    reference = entry.get("session_id", "")
    owner = record.by_member_reference(reference) if isinstance(reference, str) else None
    return StaffLaunchReference(owner.id) if owner is not None else NativeLaunchReference(str(entry.get("session_id", "")))


def named_references(name: str, record: staff.StaffSnapshot,
                     entries: Mapping[str, PaneLaunch]) -> set[StaffLaunchReference | NativeLaunchReference]:
    """Resolve a staff name or a retained native name without reading display labels."""
    references: set[StaffLaunchReference | NativeLaunchReference] = {
        StaffLaunchReference(row.id) for row in record.rows
        if name in (row.name, row.id, row.current_session)}
    references.update(reference for entry in entries.values()
        if isinstance(reference := launch_reference(entry, record), NativeLaunchReference)
        and name in (reference.session, entry.get("name")))
    return references


class UnknownNativePane(Protocol):
    @property
    def pane(self) -> str: ...


class NativePaneScan(Protocol):
    @property
    def found(self) -> Mapping[staff.StaffId, Sequence[Mapping[str, object]]]: ...
    @property
    def unknown(self) -> Sequence[UnknownNativePane]: ...


class NativePaneServer(Protocol):
    session: str
    def panes(self) -> Sequence[Mapping[str, object]]: ...
    def scan_row_panes(self, record: staff.StaffSnapshot, panes: list[dict[str, object]]) -> NativePaneScan: ...
    def pane_activity(self, pane: str) -> object: ...
    def result(self, *arguments: str) -> dict[str, object]: ...


@dataclass(frozen=True)
class NativePaneEvidence:
    """The current server panes and their decoded native owners."""

    panes: Sequence[Mapping[str, object]]
    scan: NativePaneScan


class ReadNativePanes(Enum):
    CURRENT = "read the current native processes"


class PaneProcessUnreadable(ValueError):
    """The retained pane's current process could not be read."""


def reference_panes(reference: StaffLaunchReference | NativeLaunchReference,
                    record: staff.StaffSnapshot, client: NativePaneServer,
                    entries: Mapping[str, PaneLaunch], *,
                    evidence: NativePaneEvidence | ReadNativePanes = ReadNativePanes.CURRENT,
                    strict_retained_reads: bool = False) -> list[dict[str, object]]:
    """Select the current native pane, or its newest retained shell after the process stops."""
    owner = record.by_id(reference.id) if isinstance(reference, StaffLaunchReference) else None
    if isinstance(reference, StaffLaunchReference) and owner is None:
        return []
    panes: Sequence[Mapping[str, object]]
    found: Mapping[staff.StaffId, Sequence[Mapping[str, object]]]
    if isinstance(evidence, ReadNativePanes):
        panes = [dict(pane) for pane in client.panes()]
        scan = client.scan_row_panes(record, [dict(pane) for pane in panes])
        found = scan.found
        unknown = {item.pane for item in scan.unknown}
    else:
        panes = evidence.panes
        found = evidence.scan.found
        unknown = {item.pane for item in evidence.scan.unknown}
    decoded: dict[str, set[str]] = {}
    if isinstance(reference, StaffLaunchReference):
        matches = found.get(reference.id, ())
    else:
        decoded = {str(pane["pane_id"]): native_sessions(client, pane) for pane in panes
                   if str(pane["pane_id"]) not in unknown}
        matches = [pane for pane in panes if reference.session in decoded.get(str(pane["pane_id"]), set())]
    if matches:
        return [dict(pane) for pane in matches]
    retained = {pane for pane, entry in entries.items() if launch_reference(entry, record) == reference
                or isinstance(reference, NativeLaunchReference) and entry.get("session_id") == reference.session}
    occupied = {str(pane["pane_id"]) for matches in found.values() for pane in matches}
    candidates = [pane for pane in panes
                  if str(pane["pane_id"]) in retained and str(pane["pane_id"]) not in occupied
                  and (owner is None or owner.kind != "worker" or str(pane["pane_id"]) not in unknown)]
    retained_matches: list[dict[str, object]] = []
    for pane in candidates:
        identity = str(pane["pane_id"])
        try:
            if identity in unknown:
                matches_retained = _retained_shell(client, pane, strict=strict_retained_reads)
            elif owner is not None:
                matches_retained = _retained_native_owner(client, pane, owner, record)
            else:
                matches_retained = not decoded[identity]
        except (herdr_types.HerdrFailure, OSError, ValueError) as error:
            if strict_retained_reads:
                raise PaneProcessUnreadable(f"{identity}: {error}") from error
            continue
        if matches_retained:
            retained_matches.append(dict(pane))
    return sorted(retained_matches, key=lambda pane: entries[str(pane["pane_id"])]["written_at"], reverse=True)[:1]


@dataclass(frozen=True)
class ProvedPaneConversation:
    identity: str
    kind: Literal["claude", "codex"]
    pid: int


class NoForegroundConversation(Enum):
    FOUND = "no foreground conversation"


class NativePaneReader(Protocol):
    def foreground_pane_conversation(
        self, info: object, pane: Mapping[str, object],
    ) -> ProvedPaneConversation | NoForegroundConversation: ...
    def shared_session(self) -> str: ...
    def Herdr(self, session: str) -> NativePaneServer: ...
    def codex_resume_thread(self, pid: int) -> str: ...


def native_sessions(client: NativePaneServer, pane: Mapping[str, object]) -> set[str]:
    return _native_sessions(_process_info(client, pane), pane)


def _process_info(client: NativePaneServer, pane: Mapping[str, object]) -> dict[str, object]:
    info = client.result("pane", "process-info", "--pane", str(pane["pane_id"])).get("process_info")
    if not isinstance(info, dict):
        raise ValueError("expected pane process information")
    process = cast(dict[str, object], info)
    pids = (process.get("shell_pid"), process.get("foreground_process_group_id"))
    if any(isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 for pid in pids):
        raise ValueError("expected positive pane process ids")
    return process


def _native_sessions(info: dict[str, object], pane: Mapping[str, object]) -> set[str]:
    reader = cast(NativePaneReader, cast(object, import_module("scripts.herdr.herdr")))
    conversation = reader.foreground_pane_conversation(info, pane)
    return {conversation.identity} if isinstance(conversation, ProvedPaneConversation) else set()


def _retained_shell(client: NativePaneServer, pane: Mapping[str, object], *, strict: bool) -> bool:
    """Refresh a retained unknown pane once after its agent can exit."""
    info = _process_info(client, pane)
    at_shell = info["foreground_process_group_id"] == info["shell_pid"]
    if strict or at_shell:
        return not _native_sessions(info, pane) and at_shell
    return False


def _retained_native_owner(client: NativePaneServer, pane: Mapping[str, object],
                           owner: staff.StaffRow, record: staff.StaffSnapshot) -> bool:
    """Keep readable retained panes unless another staffed owner runs there."""
    return all(current is None or current.id == owner.id
               for identity in native_sessions(client, pane)
               for current in (record.by_member_reference(identity),))


def current_launch(entry: PaneLaunch, record: staff.StaffSnapshot) -> CurrentLaunch:
    """Combine retained launch options with the current staff facts."""
    reference = launch_reference(entry, record)
    if isinstance(reference, NativeLaunchReference):
        if "session_id" not in entry:
            raise ValueError(f"{reference.session}: staff member is unavailable")
        return cast(CurrentLaunch, entry.copy())
    owner = record.by_id(reference.id)
    if owner is None:
        raise ValueError(f"{reference.id}: staff member is unavailable")
    row_folder = cast(Callable[[staff.StaffRow, staff.StaffSnapshot], tuple[str, Path]],
        import_module("scripts.agents.shutdown_record").row_folder)
    try:
        folder = row_folder(owner, record)[1]
    except staff.StaffUnreadable as error:
        raise ValueError(str(error)) from error
    current = cast(CurrentLaunch, entry.copy())
    current.update(staff_id=owner.id, session_id=owner.current_session, name=owner.name,
                   cwd=str(folder), kind=agent_kind(entry, record=record))
    if isinstance(owner.home, staff.SessionHome):
        current["config_dir"] = str(owner.home.path)
    else:
        _ = current.pop("config_dir", None)
    return current


def retained_launch(entry: PaneLaunch, record: staff.StaffSnapshot) -> PaneLaunch:
    """Keep one staff reference or the unstaffed native launch facts."""
    reference = launch_reference(entry, record)
    if isinstance(reference, NativeLaunchReference) and "staff_id" not in entry:
        return entry.copy()
    retained = {field: value for field, value in entry.items()
                if field not in {"session_id", "name", "cwd", "kind", "config_dir"}}
    retained["staff_id"] = reference.id if isinstance(reference, StaffLaunchReference) else reference_id(entry)
    return cast(StaffPaneLaunch, cast(object, retained))


def native_kind(row: staff.StaffRow) -> str:
    """Read the current backend from its proved pane owner or process registry."""
    reader = cast(NativePaneReader, cast(object, import_module("scripts.herdr.herdr")))
    try:
        client = reader.Herdr(reader.shared_session())
        for pane in client.panes():
            report = pane.get("agent_session")
            identity = pane.get("session_id", cast(dict[str, object], report).get("value") if isinstance(report, dict) else "")
            if identity != row.current_session:
                continue
            reply = client.result("pane", "process-info", "--pane", str(pane["pane_id"]))
            conversation = reader.foreground_pane_conversation(reply.get("process_info"), pane)
            if isinstance(conversation, ProvedPaneConversation) and conversation.identity == row.current_session:
                return conversation.kind
    except (OSError, ValueError, herdr_types.HerdrFailure):
        pass
    observed = sessions.session_liveness(row.current_session)
    if isinstance(observed, sessions.LiveSessionProcess):
        try:
            if reader.codex_resume_thread(observed.pid) == row.current_session:
                return "codex"
        except (OSError, ValueError):
            pass
        return "claude"
    return ""


def current_kind(row: staff.StaffRow) -> Literal["claude", "codex"]:
    """Read the live backend first, then the current session's transcript."""
    family = native_kind(row)
    if family in ("claude", "codex"):
        return family
    from scripts.lib.transcripts import codex_transcript_homes, transcript_paths

    extra_homes = (row.home.path,) if isinstance(row.home, staff.SessionHome) else ()
    if any(transcript_paths(home / folder, f"**/*{row.current_session}.jsonl")
           for home in codex_transcript_homes(extra_homes) for folder in ("sessions", "archived_sessions")):
        return "codex"
    if (isinstance(row.home, staff.SessionHome) and
            transcript_paths(row.home.path / "projects", f"*/{row.current_session}.jsonl")) or sessions.transcript_locations(row.current_session):
        return "claude"
    reader = cast(Callable[[str], object], import_module("scripts.herdr.herdr").read_conversation)
    family = cast(str, getattr(reader(str(row.current_session)), "kind", ""))
    if family in ("claude", "codex"):
        return family
    raise ValueError(f"{row.name}: current backend is unavailable")


def config_home(entry: Mapping[str, object], *, record: staff.StaffSnapshot | None = None) -> staff.SessionHome | staff.HomeUnknown:
    """Read a staffed session's home, or an unstaffed launch's config folder."""
    if record is None:
        try:
            record = staff.local_snapshot()
        except staff.StaffAbsent:
            record = staff.StaffSnapshot(())
        except staff.StaffUnreadable:
            return staff.HomeUnknown()
    reference = launch_reference(entry, record)
    if isinstance(reference, StaffLaunchReference):
        owner = record.by_id(reference.id)
        if owner is not None:
            return owner.home
    directory = entry.get("config_dir", "")
    return staff.SessionHome(Path(directory)) if isinstance(directory, str) and directory else staff.HomeUnknown()


def _record_home(entry: CurrentLaunch, environment: Mapping[str, str]) -> CurrentLaunch:
    """Publish the pane's home through the staff writer when membership exists."""
    try:
        record = staff.initial_snapshot()
        reference = launch_reference(entry, record)
        row = record.by_id(reference.id) if isinstance(reference, StaffLaunchReference) else None
        if row is None:
            return entry
        current = entry.copy()
        current["staff_id"] = row.id
        if row.kind == "session":
            retain_implicit_claude_home(current, environment)
        directory = current.pop("config_dir", "")
        if directory:
            home = staff.SessionHome(Path(directory).absolute())
            if row.home != home:
                _ = staff.set_home(row.id, home)
        return current
    except (staff.StaffUnreadable, staff.StaffNotFound) as error:
        print(f"anomaly: {entry.get('session_id', entry.get('staff_id', ''))}: pane home is unavailable: {error}", file=sys.stderr)
        return entry


def staff_admitted(row: staff.StaffRow) -> staff.StaffRow:
    """Complete pane publication after the first native identity admission."""
    try:
        publication = newest_publication(record_directory(), (row.current_session,))
        if publication is not None and "config_dir" in publication.entry:
            entry = cast(CurrentLaunch, publication.entry)
            write(publication.path, _record_home(entry, entry["environment"]), sources=tuple(publication.path.parent.glob("*.json")))
            return staff.by_id(row.id) or row
    except (OSError, ValueError, staff.StaffNotFound) as error:
        print(f"anomaly: {row.id}: pane publication is unavailable: {error}", file=sys.stderr)
    return row


def record_directory() -> Path:
    return Path(os.environ.get("HERDR_PANE_RECORD_DIR") or Path.home() / ".local/state/herdr-panes")


def read_entries(path: Path) -> dict[str, object]:
    if not path.parent.exists():
        return {}
    with _publication_lock(path.parent):
        return _read_entries(path)


def _read_entries(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path}: pane records must be an object")
    return cast(dict[str, object], value)


def read(path: Path) -> dict[str, PaneLaunch]:
    try:
        return cast(dict[str, PaneLaunch], read_entries(path))
    except (OSError, ValueError):
        return {}


def _validated_launch(path: Path, pane_id: str, value: object, *, required_strings: frozenset[str]) -> PaneLaunch:
    location = f"{path}: pane {pane_id}"
    if not isinstance(value, dict):
        raise ValueError(f"{location}: launch must be an object")
    launch = cast(dict[str, object], value)
    staffed = "staff_id" in launch
    if staffed:
        if any(field in launch for field in ("session_id", "name", "cwd", "kind", "config_dir")):
            raise ValueError(f"{location}: a staffed launch keeps only its staff reference")
        required_strings = (required_strings - {"session_id", "name", "cwd", "kind", "config_dir"}) | {"staff_id"}
    else:
        _ = saved_kind(cast(NativePaneLaunch, cast(object, launch)))
    for field in ("pane_id", "session_id", "staff_id", "config_dir", "cwd", "name", "model", "written_at"):
        if field not in launch and field not in required_strings:
            continue
        if not isinstance(launch.get(field), str):
            raise ValueError(f"{location}: {field} must be a string")
    for field in ("remote_control", "flags_read", "ended", "producer_binding"):
        if field not in launch:
            continue
        if not isinstance(launch.get(field), bool):
            raise ValueError(f"{location}: {field} must be a boolean")
    if "implicit_claude_home" in launch:
        home = launch["implicit_claude_home"]
        if not isinstance(home, str) or not Path(home).is_absolute():
            raise ValueError(f"{location}: implicit_claude_home must be an absolute path")
    environment = launch.get("environment")
    if (not isinstance(environment, dict)
            or not all(isinstance(name, str) and isinstance(setting, str)
                       for name, setting in cast(dict[object, object], environment).items())):
        raise ValueError(f"{location}: environment must contain string names and settings")
    if "plugin_directories" in launch:
        directories = launch["plugin_directories"]
        if (not isinstance(directories, list)
                or not all(isinstance(directory, str) and directory
                           for directory in cast(list[object], directories))):
            raise ValueError(f"{location}: plugin_directories must contain nonempty paths")
    if "codex_policy" in launch:
        policy = launch["codex_policy"]
        if not isinstance(policy, list) or not all(isinstance(value, str) for value in cast(list[object], policy)):
            raise ValueError(f"{location}: codex_policy must contain strings")
    return cast(PaneLaunch, cast(object, launch))


class StaffSnapshotRead(Enum):
    CURRENT = "read the current staff snapshot"


def agent_kind(entry: PaneLaunch, *, record: staff.StaffSnapshot | StaffSnapshotRead = StaffSnapshotRead.CURRENT) -> Literal["claude", "codex"]:
    """Read the current backend for staff or the saved backend for an unstaffed session."""
    record = staff.initial_snapshot() if isinstance(record, StaffSnapshotRead) else record
    reference = launch_reference(entry, record)
    if isinstance(reference, StaffLaunchReference):
        owner = record.by_id(reference.id)
        if owner is None:
            raise ValueError(f"{reference.id}: staff member is unavailable")
        return current_kind(owner)
    return saved_kind(cast(NativePaneLaunch, entry))


def saved_kind(entry: NativePaneLaunch) -> Literal["claude", "codex"]:
    """Read the backend field in unstaffed or newly observed launch evidence."""
    kind = entry.get("kind", "claude")
    if kind not in ("claude", "codex"):
        raise ValueError(f"invalid retained agent kind: {kind!r}")
    return kind


def read_strict(path: Path) -> dict[str, PaneLaunch]:
    """Require identity evidence; refuse damage in any present launch field."""
    required_strings = frozenset({"session_id", "written_at"})
    return {pane: _validated_launch(path, pane, value, required_strings=required_strings)
            for pane, value in read_entries(path).items()}


def read_valid_entries(path: Path, *, record: staff.StaffSnapshot | None = None) -> dict[str, PaneLaunch]:
    """Keep usable evidence for a readout without losing a damaged entry's siblings."""
    valid: dict[str, PaneLaunch] = {}
    required_strings = frozenset({"session_id", "written_at"})
    if record is None:
        try:
            record = staff.local_snapshot()
        except staff.StaffUnreadable:
            record = staff.StaffSnapshot(())
    for pane, value in read_entries(path).items():
        try:
            entry = _validated_launch(path, pane, value, required_strings=required_strings)
            if "config_dir" in entry or isinstance(launch_reference(entry, record), StaffLaunchReference):
                valid[pane] = entry
        except ValueError:
            continue
    return valid


def matching_launch(entries: Mapping[str, PaneLaunch], place: HerdrPlace, session_id: str) -> PaneLaunch | None:
    record = staff.initial_snapshot()
    owner = record.by_member_reference(session_id)
    entry = entries.get(place.pane)
    if owner is not None:
        reference = StaffLaunchReference(owner.id)
        if entry is not None and launch_reference(entry, record) != reference:
            return None
        return max((entry for entry in entries.values() if launch_reference(entry, record) == reference),
                   key=lambda entry: entry["written_at"], default=None)
    if entry is None or entry.get("pane_id") != place.pane or entry.get("session_id") != session_id:
        return None
    return entry


class StaffLabelsRead(Enum):
    PENDING = "pending"
    UNAVAILABLE = "unavailable"


class RecordedLabels:
    """Resolve process labels from the staff record without pane records."""

    def __init__(self, membership: StaffLabels | StaffLabelsRead = StaffLabelsRead.PENDING) -> None:
        self.membership: StaffLabels | StaffLabelsRead = membership

    def resolve(self, pid: int, environment: Mapping[str, str]) -> dict[str, str]:
        values = legacy_identity.without_legacy_identity(environment)
        record = sessions.read_session(sessions.sessions_dir() / f"{pid}.json")
        if (isinstance(record, sessions.UnreadableSessionRecord) or record["pid"] != pid
                or not sessions.running_session(record)):
            return values
        if self.membership is StaffLabelsRead.PENDING:
            from scripts.production.session_labels import StaffLabels

            try:
                self.membership = StaffLabels.read()
            except (staff.StaffUnreadable, OSError, ValueError):
                self.membership = StaffLabelsRead.UNAVAILABLE
        if not isinstance(self.membership, StaffLabelsRead):
            values = self.membership.environment(values, record["sessionId"])
        return values


@contextmanager
def locked(path: Path) -> Generator[None]:
    """Hold the lock for one retained launch file."""
    with _publication_lock(path.parent):
        with open(path.parent / f".{path.stem}.lock", "w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


_publication_directories: ContextVar[frozenset[Path]] = ContextVar("publication_directories", default=frozenset())


@contextmanager
def _publication_lock(directory: Path) -> Generator[None]:
    """Serialize record access and complete an interrupted producer publication before access."""
    directory = directory.absolute()
    held = _publication_directories.get()
    if directory in held:
        yield
        return
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".publication.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        scope = _publication_directories.set(held | {directory})
        try:
            journal = directory / ".producer-publication"
            if journal.exists():
                pending = cast(dict[str, dict[str, PaneLaunch]], json.loads(journal.read_text(encoding="utf-8")))
                for filename, entries in pending.items():
                    if Path(filename).name != filename or not filename.endswith(".json"):
                        raise ValueError("invalid producer publication recovery path")
                    atomic_file.write_text_atomic(directory / filename, json.dumps(entries, indent=2, sort_keys=True) + "\n")
                journal.unlink()
            yield
        finally:
            _publication_directories.reset(scope)


def without_staff_labels(entry: PaneLaunch) -> PaneLaunch:
    """Keep pane observations without saved staff labels."""
    current = entry.copy()
    current["environment"] = {key: value for key, value in
        legacy_identity.without_legacy_identity(entry["environment"]).items()
        if key not in {"CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"}}
    return current


def save(path: Path, entries: dict[str, PaneLaunch], *,
         record: staff.StaffSnapshot | StaffSnapshotRead = StaffSnapshotRead.CURRENT) -> None:
    """Publish one retained launch file through the atomic writer."""
    if isinstance(record, StaffSnapshotRead) and any("staff_id" not in entry for entry in entries.values()):
        record = staff.initial_snapshot()
    if isinstance(record, StaffSnapshotRead):
        record = staff.StaffSnapshot(())
    entries = {pane: retained_launch(entry, record) for pane, entry in entries.items()}
    with _publication_lock(path.parent):
        atomic_file.write_text_atomic(path, json.dumps(entries, indent=2, sort_keys=True) + "\n")


def write(path: Path, launch: PaneLaunch, *, sources: Sequence[Path] = ()) -> None:
    """Persist pane observations with the fixed staff reference."""
    native_session = launch.get("session_id", "")
    try:
        membership = staff.initial_snapshot()
        reference = launch_reference(launch, membership)
        owner = membership.by_id(reference.id) if isinstance(reference, StaffLaunchReference) else None
    except (staff.StaffUnreadable, OSError, ValueError):
        membership = staff.StaffSnapshot(())
        owner = None
    with locked(path):
        if owner is not None and owner.kind == "producer":
            request = process_producer_launch_request(owner.id)
            if isinstance(request, ProducerLaunchRequest) and request.session != native_session:
                request = ProducerLaunchFromStaff.CURRENT
            _publish_producer(path, launch, owner, membership, sources, request)
            return
        launch = retained_launch(launch, membership)
        entries = read(path)
        if entries.get(launch["pane_id"]) == launch:
            return
        entries[launch["pane_id"]] = launch
        save(path, entries, record=membership)


def _publish_producer(path: Path, launch: PaneLaunch, owner: staff.StaffRow,
                      membership: staff.StaffSnapshot, sources: Sequence[Path],
                      request: ProducerLaunchRequest | ProducerLaunchFromStaff) -> None:
    """Commit the producer binding. Keep open prior bindings unless another native owner holds the pane."""
    paths = sorted({path.absolute(), *(source.absolute() for source in sources)})
    if any(source.parent != path.absolute().parent for source in paths):
        raise ValueError("producer publication paths must share the record directory")
    records = {source: read_strict(source) for source in paths}
    previous = [entry for entries in records.values() for entry in entries.values()
                if launch_reference(entry, membership) == StaffLaunchReference(owner.id)]
    newest = max(previous, key=lambda entry: entry["written_at"], default=None)
    if newest is not None and newest.get("producer_binding", False):
        if newest == retained_launch(launch, membership) or launch["written_at"] < newest["written_at"]:
            return
        if (newest["pane_id"] != launch["pane_id"] and launch["written_at"] == newest["written_at"]):
            raise ValueError("producer publication is older than its current binding")
    pid, generation = launch.get("pid"), launch.get("proc_start", "")
    if not isinstance(pid, int) or isinstance(pid, bool) or not generation:
        if previous:
            current = records[path.absolute()].get(launch["pane_id"])
            if current is None or current.get("producer_binding", False):
                return
            if launch_reference(current, membership) != StaffLaunchReference(owner.id):
                return
        records[path.absolute()][launch["pane_id"]] = launch
        save(path, records[path.absolute()], record=membership)
        return
    process_reader = cast(NativeProcessReader, cast(object, import_module("scripts.herdr.herdr")))
    observed_launch = (current_launch(launch, membership) if "session_id" not in launch
                       else cast(CurrentLaunch, launch))
    observed = process_reader.native_process(pid, saved_kind(observed_launch))
    if (observed.current_identity != owner.current_session or observed.started != generation
            or observed.environment.get("HERDR_PANE_ID") != launch["pane_id"]
            or observed.environment.get("HERDR_SESSION", "default") != path.stem):
        raise ValueError("producer publication does not match the current process")
    expected = observed_launch.copy()
    expected["session_id"] = owner.id
    configuration = _producer_publication_configuration.get()
    expected["kind"] = models_registry.launch_settings(
        "producers", owner.name,
        configuration=configuration if isinstance(configuration, models_registry.LaunchModelConfiguration) else None,
    ).kind
    if isinstance(request, ProducerLaunchRequest):
        if request.member != owner.id or request.session != owner.current_session:
            raise ValueError("producer start expectation names another conversation")
        expected["cwd"] = str(request.folder)
        if request.kind != saved_kind(observed_launch):
            raise ValueError("producer publication uses another backend")
        variable = "CODEX_HOME" if saved_kind(observed_launch) == "codex" else "CLAUDE_CONFIG_DIR"
        process_home = Path(observed.environment.get(variable) or Path.home() / (".codex" if saved_kind(observed_launch) == "codex" else ".claude"))
        if process_home.resolve() != request.home.resolve():
            raise ValueError("producer publication uses another account")
    elif isinstance(owner.worktree, staff.PresentWorktree):
        expected["cwd"] = str(owner.worktree.path)
    elif newest is not None:
        expected["cwd"] = current_launch(newest, membership)["cwd"]
    _usable_replacement(path, ResumeAttempt(launch["pane_id"], launch), observed_launch,
                        VerifiedResume(expected, frozenset({owner.id, owner.current_session}), membership), same_pane=False)
    place = cast(Callable[[int], object], import_module("scripts.herdr.herdr").herdr_place_of)(pid)
    if isinstance(place, HerdrPlace):
        cast(Callable[[str, str], None], import_module("scripts.herdr.herdr").forget_pane_evidence)(place.session, place.pane)
    current = process_reader.native_process(pid, saved_kind(observed_launch))
    if current.current_identity != observed.current_identity or current.started != generation:
        raise ValueError("producer process changed before publication")
    committed = retained_launch(launch, membership)
    committed["producer_binding"] = True
    updates = {source: entries.copy() for source, entries in records.items()}
    for source, entries in records.items():
        prior = {pane for pane, entry in entries.items()
                 if launch_reference(entry, membership) == StaffLaunchReference(owner.id)
                 and (source != path.absolute() or pane != launch["pane_id"])}
        if not prior:
            continue
        try:
            client = cast(Callable[[str], NativePaneServer],
                          import_module("scripts.herdr.herdr").Herdr)(source.stem)
            present = {str(pane["pane_id"]): pane for pane in client.panes()}
        except (OSError, ValueError, herdr_types.HerdrFailure):
            continue
        dropped = prior - present.keys()
        for pane in prior & present.keys():
            try:
                identities = native_sessions(client, present[pane])
                if identities and all(current is not None and current.id != owner.id
                                      for identity in identities
                                      for current in (membership.by_member_reference(identity),)):
                    dropped.add(pane)
            except (OSError, ValueError, herdr_types.HerdrFailure):
                continue
        for pane in dropped:
            del updates[source][pane]
    updates[path.absolute()][launch["pane_id"]] = committed
    changed = {source: entries for source, entries in updates.items() if entries != records[source]}
    if len(changed) > 1:
        journal = path.parent / ".producer-publication"
        atomic_file.write_text_atomic(journal, json.dumps({source.name: entries for source, entries in changed.items()}))
        for source, entries in changed.items():
            save(source, entries, record=membership)
        journal.unlink()
    elif changed:
        source, entries = next(iter(changed.items()))
        save(source, entries, record=membership)


@dataclass(frozen=True)
class LaunchPublication:
    """The source file and exact retained launch before a replacement."""

    path: Path
    entry: PaneLaunch


def newest_publication(directory: Path, references: Sequence[str]) -> LaunchPublication | None:
    """Read the newest retained publication for the supplied session references."""
    matches = [LaunchPublication(path, entry)
               for path in sorted(directory.glob("*.json"))
               for entry in read_strict(path).values()
               if entry.get("staff_id") in references or entry.get("session_id") in references]
    return max(matches, key=lambda publication: publication.entry["written_at"], default=None)


@dataclass(frozen=True)
class UnpublishedResume:
    """The resume attempt has no retained launch record."""


UNPUBLISHED_RESUME = UnpublishedResume()


@dataclass(frozen=True)
class ResumeAttempt:
    """The attempt's pane and its exact publication before completion."""

    pane_id: str
    publication: PaneLaunch | UnpublishedResume


@dataclass(frozen=True)
class VerifiedResume:
    """The caller proved the expected conversation in the attempt's pane."""

    expected: NativePaneLaunch
    references: frozenset[str]
    record: staff.StaffSnapshot | StaffSnapshotRead = StaffSnapshotRead.CURRENT


@dataclass(frozen=True)
class FailedResume:
    """The attempt did not prove the expected conversation."""


@dataclass(frozen=True)
class PreparedAccountMove:
    """The target account has folder trust and must survive a failed start."""

    config_dir: Path
    account_environment: Mapping[str, str]


class ResumeRecord(Enum):
    SOURCE = "source record"


@dataclass(frozen=True)
class ResumeSourcePane:
    """The operation located this source pane before the replacement launch."""

    pane: str


class ResumeSourceAbsent(Enum):
    CURRENT = "the operation found no source pane"
    OTHER_OWNER = "another native owner holds the recorded pane"


def resume_attempt(path: Path, pane_id: str) -> ResumeAttempt:
    """Capture the exact publication before identity checks or cleanup."""
    entry = read_strict(path).get(pane_id)
    return ResumeAttempt(pane_id, entry if entry is not None else UnpublishedResume())


def _usable_replacement(path: Path, attempt: ResumeAttempt, entry: PaneLaunch,
                        proof: VerifiedResume, *, same_pane: bool) -> None:
    required = frozenset({"pane_id", "session_id", "cwd", "name", "model", "written_at"})
    validation = {field: value for field, value in entry.items() if field != "staff_id"} if "session_id" in entry else entry
    _ = _validated_launch(path, attempt.pane_id, validation, required_strings=required)
    expected = proof.expected
    if "session_id" not in entry:
        record = staff.initial_snapshot() if isinstance(proof.record, StaffSnapshotRead) else proof.record
        entry = current_launch(entry, record)
    else:
        entry = cast(CurrentLaunch, entry)
    home, expected_home = config_home(entry), config_home(expected)
    home_changed = False
    if isinstance(home, staff.SessionHome) and isinstance(expected_home, staff.SessionHome):
        home_changed = home.path.resolve() != expected_home.path.resolve()
    else:
        print(f"anomaly: {attempt.pane_id}: session home is unknown; keep the selected account", file=sys.stderr)
    if (entry["pane_id"] != attempt.pane_id or entry["session_id"] not in proof.references
            or saved_kind(entry) != saved_kind(expected)
            or home_changed
            or Path(entry["cwd"]).resolve() != Path(expected["cwd"]).resolve()
            or not entry.get("flags_read", False)
            or (not same_pane and entry.get("ended", False))):
        raise ValueError(f"{attempt.pane_id}: replacement launch does not match the proved resume")


def retire_launch(path: Path, pane_id: str, original: PaneLaunch, *,
                  replacement: ResumeAttempt | UnpublishedResume = UNPUBLISHED_RESUME) -> None:
    """Remove the exact old publication after confirmed closure."""
    with locked(path):
        entries = read_strict(path)
        current = entries.get(pane_id)
        if current is None:
            return
        if current != original:
            raise ValueError(f"{pane_id}: retained launch changed before retirement")
        if (isinstance(replacement, ResumeAttempt) and not isinstance(replacement.publication, UnpublishedResume)
                and entries.get(replacement.pane_id) != replacement.publication):
            raise ValueError(f"{replacement.pane_id}: replacement launch changed before retirement")
        del entries[pane_id]
        save(path, entries, record=staff.StaffSnapshot(()))


@overload
def finish_restore(path: Path, original: PaneLaunch, attempt: ResumeAttempt,
                   outcome: PreparedAccountMove, *, close: Callable[[str], bool],
                   destination: Path | ResumeRecord = ResumeRecord.SOURCE,
                   close_source: Callable[[str], bool] | ResumeRecord = ResumeRecord.SOURCE,
                   source_pane: ResumeSourcePane | ResumeSourceAbsent | ResumeRecord = ResumeRecord.SOURCE) -> LaunchPublication: ...


@overload
def finish_restore(path: Path, original: PaneLaunch, attempt: ResumeAttempt,
                   outcome: VerifiedResume | FailedResume, *, close: Callable[[str], bool],
                   destination: Path | ResumeRecord = ResumeRecord.SOURCE,
                   close_source: Callable[[str], bool] | ResumeRecord = ResumeRecord.SOURCE,
                   source_pane: ResumeSourcePane | ResumeSourceAbsent | ResumeRecord = ResumeRecord.SOURCE) -> None: ...


def finish_restore(path: Path, original: PaneLaunch, attempt: ResumeAttempt,
                   outcome: VerifiedResume | FailedResume | PreparedAccountMove, *,
                   close: Callable[[str], bool],
                   destination: Path | ResumeRecord = ResumeRecord.SOURCE,
                   close_source: Callable[[str], bool] | ResumeRecord = ResumeRecord.SOURCE,
                   source_pane: ResumeSourcePane | ResumeSourceAbsent | ResumeRecord = ResumeRecord.SOURCE) -> LaunchPublication | None:
    """Keep one proved launch, or retire a failed attempt after confirmed closure.

    The close callback must confirm that the selected tab is closed.
    A same-pane resume keeps its pane on success and failure.
    """
    destination_path = destination if isinstance(destination, Path) else path
    source = source_pane.pane if isinstance(source_pane, ResumeSourcePane) else (
        original["pane_id"] if isinstance(source_pane, ResumeRecord) else "")
    same_pane = path == destination_path and source == attempt.pane_id
    if isinstance(outcome, PreparedAccountMove):
        if not same_pane:
            raise ValueError("an account move requires its original retained pane")
        with locked(path):
            entries = read_strict(path)
            if entries.get(original["pane_id"]) != original or attempt.publication != original:
                raise ValueError(f"{original['pane_id']}: retained launch changed before the account move")
            prepared = original.copy()
            if "config_dir" in original:
                prepared = cast(NativePaneLaunch, prepared)
                prepared["config_dir"] = str(outcome.config_dir)
                prepared["environment"] = {**original["environment"], **outcome.account_environment}
            if prepared != original:
                entries[original["pane_id"]] = prepared
                save(path, entries)
            return LaunchPublication(path, prepared)
    if isinstance(outcome, FailedResume):
        if same_pane:
            return
        committed = read(destination_path).get(attempt.pane_id)
        if committed is not None and committed.get("producer_binding", False):
            return
        if close(attempt.pane_id) is not True:
            return
        if not isinstance(attempt.publication, UnpublishedResume):
            retire_launch(destination_path, attempt.pane_id, attempt.publication,
                           replacement=ResumeAttempt(attempt.pane_id, UnpublishedResume()))
        return
    try:
        with locked(destination_path):
            entries = read_strict(destination_path)
            replacement = entries.get(attempt.pane_id)
            if replacement is None:
                raise ValueError(f"{attempt.pane_id}: replacement launch was not published; would delete the only resume record")
            _usable_replacement(destination_path, attempt, replacement, outcome, same_pane=same_pane)
            if same_pane:
                if replacement.get("ended", False):
                    replacement["ended"] = False
                    save(destination_path, entries)
                return
            source_entry = read_strict(path).get(original["pane_id"])
            if source_entry is None and replacement.get("producer_binding", False):
                retained = ResumeAttempt(attempt.pane_id, replacement.copy())
            elif source_entry != original:
                raise ValueError(f"{original['pane_id']}: retained launch changed before replacement")
            else:
                retained = ResumeAttempt(attempt.pane_id, replacement.copy())
    except (OSError, ValueError):
        committed = read(destination_path).get(attempt.pane_id)
        if committed is None or not committed.get("producer_binding", False):
            finish_restore(path, original, attempt, FailedResume(), close=close, destination=destination_path,
                           source_pane=source_pane)
        raise
    source_close = close if isinstance(close_source, ResumeRecord) else close_source
    if source_pane is not ResumeSourceAbsent.OTHER_OWNER and (not source or source_close(source) is not True):
        return
    if destination_path == path:
        retire_launch(path, original["pane_id"], original, replacement=retained)
    else:
        retire_launch(path, original["pane_id"], original)


@dataclass(frozen=True)
class ClaudeArguments:
    values: dict[str, str]
    has_prompt: bool
    plugin_directories: tuple[str, ...] = ()

    def value(self, flags: frozenset[str]) -> str:
        return next((value for name, value in reversed(tuple(self.values.items())) if name in flags), "")


# Claude options used by launch commands; all consumers share this parse.
CLAUDE_VALUE_FLAGS = frozenset({"--resume", "-r", "--remote-control", "--settings",
    "--model", "--name", "-n", "--effort", "--autocompact", "--permission-mode", "--session-id", "--plugin-dir"})
RESUME_FLAGS = frozenset({"--resume", "-r"})


LaunchArguments = list[str] | tuple[str, ...]


def parse_claude_arguments(arguments: LaunchArguments) -> ClaudeArguments:
    return parse_claude_flags(arguments[1:])


def parse_claude_flags(arguments: Sequence[str]) -> ClaudeArguments:
    values: dict[str, str] = {}
    prompt = False
    plugin_directories: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            prompt = prompt or index < len(arguments)
            break
        option, equals, value = argument.partition("=")
        if option in CLAUDE_VALUE_FLAGS:
            if not equals and index < len(arguments) and not arguments[index].startswith("-"):
                value = arguments[index]
                index += 1
            _ = values.pop(option, None)
            values[option] = value
            if option == "--plugin-dir" and value:
                plugin_directories.append(value)
        elif not argument.startswith("-"):
            prompt = True
    return ClaudeArguments(values, prompt, tuple(plugin_directories))


def launch(pane_id: str, session_id: str, cwd: str, environment: Mapping[str, str],
           arguments: LaunchArguments, *, kind: Literal["claude", "codex"] = "claude") -> CurrentLaunch:
    config_dir = (environment.get("CODEX_HOME") or str(Path.home() / ".codex") if kind == "codex"
                  else environment.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"))
    parsed = parse_claude_arguments(arguments)
    entry: CurrentLaunch = {
        "kind": kind,
        "pane_id": pane_id,
        "session_id": session_id,
        "config_dir": config_dir,
        "cwd": cwd,
        "name": parsed.value(NAME_FLAGS),
        "model": parsed.value(MODEL_FLAGS),
        "remote_control": any(argument.partition("=")[0] == "--remote-control" for argument in arguments),
        "environment": {key: value for key, value in sorted(environment.items())
                        if key in KEPT_NAMES},
        "flags_read": bool(arguments),
        "ended": False,
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    if kind == "codex":
        policy: list[str] = []
        options = iter(arguments[1:])
        for argument in options:
            if argument == "--":
                break
            option, equals, value = argument.partition("=")
            if option in {"--sandbox", "--ask-for-approval", "-c"}:
                value = value if equals else next(options, "")
                if option != "-c" or not value.startswith("projects="):
                    policy.extend((option, value))
        entry["codex_policy"] = policy
    return _record_home(entry, environment)
