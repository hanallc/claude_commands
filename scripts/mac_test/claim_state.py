"""Read claim records and lock the target coordination state."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import fcntl
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, NotRequired, TypedDict, cast

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_targets import target_config

if TYPE_CHECKING:
    from scripts.buildlog.transfer_bytes import TransferBytes


SubmittingSession = OwningSession | NoOwningSession


CiSwitch = Literal[
    "off_by_this_block",
    "off_before",
    "not_configured",
    "still_on",
    "off_unconfirmed",
]


class NoMacBlock:
    """No session excludes work from the Mac."""


@dataclass(frozen=True)
class RecordedSession:
    """The session recorded when a block was requested."""

    name: str
    holder: str


@dataclass(frozen=True)
class HolderQueue:
    """The holder's named message queue."""

    name: str


MessageDelivery = RecordedSession | HolderQueue


@dataclass(frozen=True)
class NamedShowrunner:
    """A showrunner that also receives expiry messages."""

    name: str


class NoShowrunner:
    """A block with no showrunner recipient."""


Showrunner = NamedShowrunner | NoShowrunner


@dataclass(frozen=True)
class WaitingOnLocalRun:
    """A block waiting for a local Mac run."""

    what: str


class WaitingOnCiJob:
    """A block waiting for CI's Mac job to start."""


class WaitingOnCiMacJob:
    """A block waiting for CI's running Mac job to finish."""


class WaitingOnUnknownCi:
    """A block waiting until CI can be checked."""


WaitingOn = WaitingOnLocalRun | WaitingOnCiJob | WaitingOnCiMacJob | WaitingOnUnknownCi


class FreeMessageNotNeeded:
    """An active block that did not wait for Mac work."""


@dataclass(frozen=True)
class FreeMessageOwed:
    """An active block whose availability message still needs delivery."""

    what: str
    ended: datetime


class FreeMessageSent:
    """An active block whose availability message was delivered."""


FreeMessage = FreeMessageNotNeeded | FreeMessageOwed | FreeMessageSent


@dataclass(frozen=True)
class PendingMacBlock:
    """A block that is waiting for existing Mac work to finish."""

    holder: str
    reason: str
    since: datetime
    expires: datetime
    delivery: MessageDelivery
    showrunner: Showrunner
    ci: CiSwitch
    waiting_on: WaitingOn


@dataclass(frozen=True)
class ActiveMacBlock:
    """A block that currently has exclusive control of the Mac."""

    holder: str
    reason: str
    since: datetime
    expires: datetime
    delivery: MessageDelivery
    showrunner: Showrunner
    ci: CiSwitch
    free_message: FreeMessage


MacBlockState = NoMacBlock | PendingMacBlock | ActiveMacBlock


@dataclass(frozen=True)
class MacBlockEvent:
    """The event holds a block start or end, its time, and its target."""
    kind: Literal["began", "ended"]
    block: PendingMacBlock | ActiveMacBlock
    at: datetime
    target: str = "mac"


class StateReadError(ValueError):
    """A Mac coordination record cannot be read or decoded."""


class ClaimStateUnreadable(StateReadError):
    """A claim file, or the claims folder, that cannot be read or decoded."""

    def __init__(self, path: Path, error: Exception) -> None:
        super().__init__(str(error))
        self.path: Path = path


@dataclass(frozen=True)
class ClaimIdentity:
    """The identity retained through adoption and exact release."""

    claim_id: str
    episode: str
    attempt: str


@dataclass(frozen=True)
class NoClaimIdentity:
    """No successful claim response supplied an identity."""


NO_CLAIM_IDENTITY = NoClaimIdentity()


@dataclass(frozen=True)
class ClaimRecordLocation:
    """The path identifies a claim record within the target state directory."""

    relative: Path

    @classmethod
    def parse(cls, value: str) -> ClaimRecordLocation:
        path = Path(value)
        if path == Path("run.json") or (len(path.parts) == 2 and path.parts[0] == "claims" and path.suffix == ".json"):
            return cls(path)
        raise ValueError("invalid claim record path")


@dataclass(frozen=True)
class UnlocatedClaimRecord:
    """The claim reply did not identify the record path."""


UNLOCATED_CLAIM_RECORD = UnlocatedClaimRecord()


def reply_claim_location(output: str) -> ClaimRecordLocation | UnlocatedClaimRecord:
    locations = [line.removeprefix("claim_record=") for line in output.splitlines() if line.startswith("claim_record=")]
    if len(locations) == 1:
        try:
            return ClaimRecordLocation.parse(locations[0])
        except ValueError:
            pass
    return UNLOCATED_CLAIM_RECORD


@dataclass(frozen=True)
class CallingSession:
    """Resolve the submitting session at the existing caller boundary."""


CALLING_SESSION = CallingSession()


def submitting_session(session: SubmittingSession | CallingSession) -> SubmittingSession:
    from scripts.build_hold import owning_session

    return owning_session.owning_session() if isinstance(session, CallingSession) else session


def session_id(session: SubmittingSession) -> str:
    return session.session_id if isinstance(session, OwningSession) else ""


def claim_identity(record: ClaimOwnerRecord) -> ClaimIdentity:
    return ClaimIdentity(record["claim_id"], record["episode"], record["attempt"])


def reply_claim_identity(output: str) -> ClaimIdentity | NoClaimIdentity:
    lines = [line.removeprefix("claim_identity=") for line in output.splitlines()
             if line.startswith("claim_identity=")]
    if len(lines) != 1:
        return NO_CLAIM_IDENTITY
    try:
        value = cast(object, json.loads(lines[0]))
    except json.JSONDecodeError:
        return NO_CLAIM_IDENTITY
    if not isinstance(value, dict):
        return NO_CLAIM_IDENTITY
    fields = cast(dict[str, object], value)
    claim_id, episode, attempt = (fields.get(field) for field in ("claim_id", "episode", "attempt"))
    if not isinstance(claim_id, str) or not isinstance(episode, str) or not isinstance(attempt, str):
        return NO_CLAIM_IDENTITY
    if not claim_id or not episode or not attempt:
        return NO_CLAIM_IDENTITY
    return ClaimIdentity(claim_id, episode, attempt)


class ClaimOwnerRecord(TypedDict):
    """Both claim types retain the process identity and job details."""

    claim_id: str
    episode: str
    attempt: str
    submitting_session: str
    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str
    holder_session: NotRequired[str]
    idle_since: NotRequired[str]
    work_idle_since: NotRequired[str]
    copy: NotRequired[bool]
    repo_key: NotRequired[str]
    repo_name: NotRequired[str]
    slot: NotRequired[int]
    transfers: NotRequired[list[TransferBytes]]


class ProcessBoundClaim(ClaimOwnerRecord):
    """The claim holds Mac capacity while its process lives."""


class RemoteJobClaim(ClaimOwnerRecord):
    """The claim holds Mac capacity until the named host confirms that its job ends."""

    remote_host: str
    cleanup_pending: NotRequired[bool]
    instance_generation: NotRequired[str]


RunRecord = ProcessBoundClaim | RemoteJobClaim


@dataclass(frozen=True)
class RepositoryScope:
    """A claim that excludes only other claims for the same repository."""

    name: str
    key: str = ""
    slot: int = 0


@dataclass(frozen=True)
class WholeMacScope:
    """This claim names no repository and excludes every other claim."""


ClaimScope = RepositoryScope | WholeMacScope


WHOLE_MAC = WholeMacScope()


def remote_claim_destination(target: str, platform: target_config.Platform) -> tuple[Literal["mac", "ssh"], str]:
    """Identify the provider and target for an SSH claim."""
    return ("mac" if platform == "macos" else "ssh"), target or "mac"


@dataclass(frozen=True)
class ClaimKey:
    """Identify one claim across provider records, dispatch, and release."""

    provider: Literal["local", "mac", "ssh", "scaler"]
    target: str
    identity: ClaimIdentity

    @classmethod
    def for_target(cls, target: str | StatePaths, identity: ClaimIdentity) -> ClaimKey:
        paths = state_paths(target) if isinstance(target, str) else target
        provider, name = remote_claim_destination(paths.target, paths.platform)
        return cls(provider, name, identity)


@dataclass(frozen=True)
class FenceHostIdentity:
    """Identify the remote machine, its boot, and its state directory."""

    machine: str
    boot: str
    home: str

    @classmethod
    def parse(cls, value: object) -> FenceHostIdentity:
        if not isinstance(value, list) or len(cast(list[object], value)) != 3:
            raise ValueError("The fence host identity is invalid.")
        parts = cast(list[object], value)
        machine, boot, home = parts
        if not isinstance(machine, str) or not machine or not isinstance(boot, str) or not boot:
            raise ValueError("The fence host identity is incomplete.")
        if not isinstance(home, str) or not Path(home).is_absolute():
            raise ValueError("The fence host home is invalid.")
        return cls(machine, boot, home)


@dataclass(frozen=True)
class FenceHostUnrecorded:
    """The journal predates the recorded host identity."""


@dataclass(frozen=True)
class FenceRef:
    """Identify the original remote start fence."""

    key: ClaimKey
    host: str
    generation: str
    path: Path
    responder: FenceHostIdentity | FenceHostUnrecorded = FenceHostUnrecorded()

    def journal_text(self) -> str:
        identity = self.key.identity
        address: list[object] = [self.key.provider, self.key.target, identity.claim_id, identity.episode,
                                 identity.attempt, self.host, self.generation]
        if isinstance(self.responder, FenceHostIdentity):
            address.append([self.responder.machine, self.responder.boot, self.responder.home])
        address.append(self.path.as_posix())
        return json.dumps(address) + "\n"

    def acknowledgement(self, outcome: Literal["settled", "pending"]) -> str:
        token = hashlib.sha256(self.journal_text().encode()).hexdigest()
        return f"dispatch_start_{outcome}={token}"


class FencePreparation(StrEnum):
    """Record what is known about the remote preparation request."""

    NOT_SENT = "not-sent"
    UNKNOWN = "unknown"
    PREPARED = "prepared"


@dataclass(frozen=True)
class StartFenceJournal:
    """Record local preparation evidence apart from the remote identity."""

    VERSION: ClassVar[int] = 1

    fence: FenceRef
    preparation: FencePreparation

    def text(self) -> str:
        return json.dumps({"version": self.VERSION, "fence": self.fence.journal_text(),
                           "preparation": self.preparation}) + "\n"


@dataclass(frozen=True)
class UnresolvedFenceRef:
    """The original fence address is unavailable."""


UNRESOLVED_FENCE = UnresolvedFenceRef()


@dataclass(frozen=True)
class Reserved:
    """The wait owner still holds the claim for transfer."""


@dataclass(frozen=True)
class Held:
    """The provider adopted or retained the claim."""


@dataclass(frozen=True)
class Released:
    """Exact evidence proves release and fence settlement."""


@dataclass(frozen=True)
class Unknown:
    """The claim obligation remains while its disposition is unknown."""


@dataclass(frozen=True)
class NoClaimAcquired:
    """The provider acquired no claim for this attempt."""


@dataclass(frozen=True)
class ReleaseReceipt:
    """The provider confirmed completion of the exact release."""

    key: ClaimKey

    def line(self) -> str:
        ident = self.key.identity
        return "claim_released=" + json.dumps([self.key.provider, self.key.target,
                                               ident.claim_id, ident.episode, ident.attempt])


@dataclass(frozen=True)
class AuthoritativeClaimAbsence:
    """A full provider read proves the exact claim and its journal are absent."""

    key: ClaimKey


@dataclass(frozen=True)
class ClaimAbsentFromInventory:
    """The exact key is absent from the provider records that were read."""

    key: ClaimKey


@dataclass(frozen=True)
class ClaimLifecycle:
    """Store one exact claim and the evidence that governs its exclusion."""

    key: ClaimKey
    scope: ClaimScope
    state: Reserved | Held | Released | Unknown = Unknown()
    location: ClaimRecordLocation | UnlocatedClaimRecord = UNLOCATED_CLAIM_RECORD
    fence: FenceRef | UnresolvedFenceRef = UNRESOLVED_FENCE

    @property
    def excludes(self) -> bool:
        return not isinstance(self.state, Released)


def transition_claim(claim: ClaimLifecycle,
                     evidence: Reserved | Held | Unknown | ReleaseReceipt | AuthoritativeClaimAbsence,
                     *, location: ClaimRecordLocation | UnlocatedClaimRecord = UNLOCATED_CLAIM_RECORD,
                     fence: FenceRef | UnresolvedFenceRef = UNRESOLVED_FENCE) -> ClaimLifecycle:
    """Update one claim with evidence for the same key."""
    if isinstance(claim.state, Released):
        return claim
    if isinstance(evidence, (ReleaseReceipt, AuthoritativeClaimAbsence)):
        return replace(claim, state=Released()) if evidence.key == claim.key else claim
    if isinstance(location, UnlocatedClaimRecord):
        location = claim.location
    if isinstance(claim.fence, FenceRef) or isinstance(fence, FenceRef) and fence.key != claim.key:
        fence = claim.fence
    return replace(claim, state=evidence, location=location, fence=fence)


@dataclass(frozen=True)
class CompleteClaimInventory:
    """A provider read covered every record for this target."""

    claims: tuple[ClaimLifecycle, ...]

    def evidence(self, key: ClaimKey) -> ClaimLifecycle | ClaimAbsentFromInventory:
        """Return the observed claim or authority for its absence from provider records."""
        return next((claim for claim in self.claims if claim.key == key), ClaimAbsentFromInventory(key))


@dataclass(frozen=True)
class IncompleteClaimInventory:
    """The observation cannot prove that a claim is absent."""


@dataclass(frozen=True)
class HeldClaim:
    """One claim record, the repository it covers and the file that holds it."""

    scope: ClaimScope
    path: Path
    record: RunRecord


class NoRun:
    """No process owns the Mac."""


RunState = RunRecord | NoRun


NO_RUN = NoRun()


class StatePaths:
    """Files used by mac-test coordination."""

    def __init__(self, directory: Path, target: str = "mac", *,
                 registry_selected: bool = False, platform: Literal["linux", "macos"] = "macos") -> None:
        self.target: str = target
        self.registry_selected: bool = registry_selected
        self.platform: Literal["linux", "macos"] = platform
        self.directory: Path = directory
        self.lock: Path = directory / "state.lock"
        self.control_lock: Path = directory / "control.lock"
        self.watch_lock: Path = directory / "watch.lock"
        self.claim_watch_lock: Path = directory / "claim_watch.lock"
        self.run: Path = directory / "run.json"
        self.claims: Path = directory / "claims"
        self.block: Path = directory / "block.json"
        self.affinity: Path = directory / "affinity"


def state_paths(target: str = "") -> StatePaths:
    if not target:
        return StatePaths(target_config.ssh_target_state_directory("mac"))
    configured = target_config.configured_ssh_target(target)
    return StatePaths(target_config.ssh_target_state_directory(target), target,
                      registry_selected=True, platform=configured.platform)


@contextmanager
def locked(path: Path) -> Generator[None, None, None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with record_after_unlock(), path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_json_object(path: Path) -> dict[str, object] | None:
    try:
        decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StateReadError(f"{path}: {error}") from error
    if not isinstance(decoded, dict):
        raise StateReadError(f"{path} does not contain a JSON object")
    return cast(dict[str, object], decoded)


def read_run(path: Path) -> RunState:
    values = read_json_object(path)
    if values is None:
        return NO_RUN
    pid = values.get("pid")
    proc_start = values.get("proc_start")
    what = values.get("what")
    worktree = values.get("worktree")
    since = values.get("since")
    if (
        type(pid) is not int
        or not isinstance(proc_start, str)
        or not isinstance(what, str)
        or not isinstance(worktree, str)
        or not isinstance(since, str)
    ):
        raise StateReadError(f"{path} has an invalid run record")
    _ = aware_timestamp(since, path, "since")
    identity = ClaimIdentity(*(required_string(values, field, path) for field in ("claim_id", "episode", "attempt")))
    if not identity.claim_id or not identity.episode or not identity.attempt:
        raise StateReadError(f"{path} has an invalid claim identity")
    record: ProcessBoundClaim = {
        "claim_id": identity.claim_id,
        "episode": identity.episode,
        "attempt": identity.attempt,
        "submitting_session": required_string(values, "submitting_session", path),
        "pid": pid,
        "proc_start": proc_start,
        "what": what,
        "worktree": worktree,
        "since": since,
    }
    if "transfers" in values:
        record["transfers"] = cast("list[TransferBytes]", values["transfers"])
    if "holder_session" in values:
        holder_session = required_string(values, "holder_session", path)
        if not holder_session.strip():
            raise StateReadError(f"{path} has an invalid holder_session")
        record["holder_session"] = holder_session
    for field in ("idle_since", "work_idle_since"):
        if field in values:
            idle_since = values[field]
            _ = aware_timestamp(idle_since, path, field)
            if field == "idle_since":
                record["idle_since"] = cast(str, idle_since)
            else:
                record["work_idle_since"] = cast(str, idle_since)
    if "copy" in values:
        if values["copy"] is not True:
            raise StateReadError(f"{path} has an invalid copy")
        record["copy"] = True
    if "repo_key" in values or "repo_name" in values:
        record["repo_key"] = required_string(values, "repo_key", path)
        record["repo_name"] = required_string(values, "repo_name", path)
    if "slot" in values:
        slot = values["slot"]
        if type(slot) is not int or slot < 1:
            raise StateReadError(f"{path} has an invalid slot")
        record["slot"] = slot
    if "remote_host" not in values:
        return record
    host = values["remote_host"]
    if not isinstance(host, str) or not host.strip():
        raise StateReadError(f"{path} has an invalid remote_host")
    remote: RemoteJobClaim = {**record, "remote_host": host}
    if 'instance_generation' in values:
        generation = required_string(values, 'instance_generation', path)
        if not generation:
            raise StateReadError(f'{path} has an invalid instance_generation')
        remote['instance_generation'] = generation
    if "cleanup_pending" in values:
        if values["cleanup_pending"] is not True:
            raise StateReadError(f"{path} has an invalid cleanup_pending")
        remote["cleanup_pending"] = True
    return remote


def aware_timestamp(value: object, path: Path, field: str) -> datetime:
    if not isinstance(value, str):
        raise StateReadError(f"{path} has an invalid {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise StateReadError(f"{path} has an invalid {field}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StateReadError(f"{path} has an invalid {field}")
    return parsed


def required_string(values: dict[str, object], key: str, path: Path) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise StateReadError(f"{path} has an invalid block record")
    return value


def target_claim_scope(paths: StatePaths, worktree: str) -> ClaimScope:
    from scripts.buildlog import repository, store

    name = repository.repo_name(worktree)
    common = repository.git_common_dir(worktree)
    if name is None or common is None:
        return WHOLE_MAC
    if target_config.uses_legacy_mac_layout(paths.platform):
        return RepositoryScope(name)
    source = Path(repository.source_git_dir(common))
    if not (source / "config").is_file():
        return RepositoryScope(name)
    key = repository.repository_key(str(source.parent))
    if key == store.UNKNOWN_IDENTITY:
        return RepositoryScope(name)
    policy_name = key.rsplit("/", 1)[-1] if key and not key.startswith("git-root:") else name
    return RepositoryScope(policy_name, key)


def claim_files(paths: StatePaths) -> tuple[Path, ...]:
    try:
        repository_files = sorted(path for path in paths.claims.iterdir() if path.name.endswith(".json"))
    except FileNotFoundError:
        repository_files = []
    except OSError as error:
        raise ClaimStateUnreadable(paths.claims, error) from error
    return (paths.run, *repository_files)


def read_claims(paths: StatePaths) -> tuple[HeldClaim, ...]:
    """Read every claim record in order from the oldest record.

    Each claims/<repo>.json file holds one repository claim.
    The run.json file holds a claim without a repository name or an older claim.
    An older claim uses its worktree to select its repository scope.
    A worktree outside a repository gives the claim the whole Mac scope.
    """
    held: list[HeldClaim] = []
    for path in claim_files(paths):
        try:
            record = read_run(path)
        except StateReadError as error:
            raise ClaimStateUnreadable(path, error) from error
        if isinstance(record, NoRun):
            continue
        if "repo_name" in record:
            scope: ClaimScope = RepositoryScope(record["repo_name"], record.get("repo_key", ""), record.get("slot", 0))
        else:
            scope = target_claim_scope(paths, record["worktree"]) if path == paths.run else RepositoryScope(path.stem)
        held.append(HeldClaim(scope, path, record))
    return tuple(sorted(held, key=lambda claim: datetime.fromisoformat(claim.record["since"])))


def claim_inventory(paths: StatePaths, claims: tuple[HeldClaim, ...]) -> CompleteClaimInventory:
    """Project a complete provider record scan into claim states."""
    return CompleteClaimInventory(tuple(transition_claim(
        ClaimLifecycle(ClaimKey.for_target(paths, claim_identity(held.record)), held.scope),
        Held() if "remote_host" in held.record else Reserved(),
        location=ClaimRecordLocation(held.path.relative_to(paths.directory))) for held in claims))


@contextmanager
def record_after_unlock() -> Generator[None, None, None]:
    depth = coordination_depth.get()
    token = coordination_depth.set(depth + 1)
    try:
        yield
    finally:
        coordination_depth.reset(token)
        if depth == 0:
            events = pending_hold_events.get()
            _ = pending_hold_events.set(())
            for event in events:
                from importlib import import_module

                recorder = cast(Callable[[MacBlockEvent], None],
                                import_module("scripts.mac_test.mac_test").start_hold_recorder)
                recorder(event)


pending_hold_events: ContextVar[tuple[MacBlockEvent, ...]] = ContextVar("pending_hold_events", default=())


coordination_depth: ContextVar[int] = ContextVar("coordination_depth", default=0)
