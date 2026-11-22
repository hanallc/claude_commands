"""Read claim records and lock the target coordination state."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
import fcntl
from hashlib import sha256
import json
from pathlib import Path
from subprocess import SubprocessError
import sys
from typing import TYPE_CHECKING, Literal, NotRequired, Self, TypedDict, cast, override
from uuid import uuid4

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.buildlog import store
from scripts.build_targets import target_config
from scripts.lib import process_start as process_identity
from scripts.lib.atomic_file import write_text_atomic

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
    mac_repository: NotRequired[MacRepositoryRecord]
    transfers: NotRequired[list[TransferBytes]]


class ProcessBoundClaim(ClaimOwnerRecord):
    """The claim holds Mac capacity while its process lives."""


class ReplacedMacReservation(TypedDict):
    """Identify the exact local reservation to remove."""

    file: str
    fingerprint: str


def replaced_reservation(record: ProcessBoundClaim, path: Path, paths: StatePaths) -> ReplacedMacReservation:
    return {"file": str(path.relative_to(paths.directory)), "fingerprint": _reservation_fingerprint(
        record["claim_id"], record["episode"], record["attempt"], record["pid"], record["proc_start"], record["since"])}


def _reservation_fingerprint(claim_id: str, episode: str, attempt: str, pid: int, start: str, since: str) -> str:
    return sha256(json.dumps((claim_id, episode, attempt, pid, start, since)).encode()).hexdigest()


class RemoteJobClaim(ClaimOwnerRecord):
    """The claim holds Mac capacity until the named host confirms that its job ends."""

    remote_host: str
    cleanup_pending: NotRequired[bool]
    cleanup_request_id: NotRequired[str]
    instance_generation: NotRequired[str]
    replaced_reservation: NotRequired[ReplacedMacReservation]


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


class ResolveMacRepository:
    """Read the repository at the claim caller boundary."""


RESOLVE_MAC_REPOSITORY = ResolveMacRepository()


class MacRepositoryRecord(TypedDict):
    key: str
    mirror_name: str
    claim_file: str
    fence_directory: str
    fence_generation: NotRequired[str]
    history_key: NotRequired[str]


@dataclass(frozen=True)
class MacMirrorIdentity:
    """The repository key, mirror name, and claim paths."""

    key: str
    mirror_name: str
    claim_file: str
    fence_directory: str
    fence_generation: str = field(default="", kw_only=True)

    def for_slot(self, slot: int) -> Self:
        fence = mac_claim_stem(self.key, slot)
        return replace(self, claim_file=f"{fence}.json", fence_directory=fence, fence_generation="")

    def for_remote_claim(self) -> Self:
        return replace(self, fence_generation=uuid4().hex)

    def fence_scope_key(self) -> str:
        """Include the generation in the key for a remote claim."""
        return f"{self.fence_directory}/{self.fence_generation}" if self.fence_generation else self.fence_directory

    def record(self) -> MacRepositoryRecord:
        record: MacRepositoryRecord = {"key": self.key, "mirror_name": self.mirror_name,
                                      "claim_file": self.claim_file, "fence_directory": self.fence_directory}
        if self.fence_generation:
            record["fence_generation"] = self.fence_generation
        return record


@dataclass(frozen=True)
class CleanupOnlyMacRepository(MacMirrorIdentity):
    """The saved mirror identifies work for cleanup."""


@dataclass(frozen=True)
class ResolvedMacMirror(MacMirrorIdentity):
    """The mirror and the checkout origin used to find it."""

    worktree: str
    checkout_origin: str = field(repr=False)

    def claim_repository(self) -> MacRepositoryIdentity | CleanupOnlyMacRepository:
        from scripts.buildlog import repository

        history_key = (repository.remote_identity(self.checkout_origin, self.worktree) if self.checkout_origin
                       else repository.git_root_key(self.worktree))
        if not history_key or history_key == store.UNKNOWN_IDENTITY:
            return CleanupOnlyMacRepository(self.key, self.mirror_name, self.claim_file, self.fence_directory)
        return MacRepositoryIdentity(self.key, self.mirror_name, self.claim_file, self.fence_directory, history_key)


@dataclass(frozen=True)
class MacRepositoryIdentity(MacMirrorIdentity):
    """The saved mirror and history key required to execute work."""

    history_key: str

    def __post_init__(self) -> None:
        if not self.history_key or self.history_key == store.UNKNOWN_IDENTITY:
            raise ValueError("the Mac repository identity needs a history key")

    @override
    def record(self) -> MacRepositoryRecord:
        return {**super().record(), "history_key": self.history_key}


def mac_claim_stem(key: str, slot: int) -> str:
    digest = sha256(key.encode()).hexdigest()
    return f"{digest}!{slot}" if slot else digest


def valid_mac_path_component(value: object) -> bool:
    return (isinstance(value, str) and bool(value) and value not in (".", "..")
            and not any(character in value for character in "/\0\r\n\t"))


def pending_start_key(identity: ClaimIdentity) -> str:
    """Name the fence for one exact provider claim."""
    return sha256(json.dumps([identity.claim_id, identity.episode, identity.attempt]).encode()).hexdigest()


def start_fence_name(identity: ClaimIdentity, scope_key: str) -> str:
    _, _, generation = scope_key.partition("/")
    key = pending_start_key(identity)
    return f"{key}-{generation}" if generation else key


def pending_start_path(paths: StatePaths, identity: ClaimIdentity, *, scope_key: str = "run") -> Path:
    return paths.directory / "pending-starts" / start_fence_name(identity, scope_key)


def pending_start_exists(paths: StatePaths, identity: ClaimIdentity) -> bool:
    """Check all generations when only the claim identity is known."""
    legacy = pending_start_path(paths, identity)
    if legacy.exists():
        return True
    if paths.platform != "macos":
        return False
    try:
        for path in legacy.parent.iterdir():
            key, _, generation = path.name.partition("-")
            if key == legacy.name and valid_fence_generation(generation):
                return True
    except FileNotFoundError:
        pass
    return False


def claim_pending_start(paths: StatePaths, path: Path, record: RunRecord) -> Path:
    return pending_start_path(paths, claim_identity(record), scope_key=claim_fence_scope_key(path, record))


def valid_fence_generation(generation: object) -> bool:
    return (isinstance(generation, str) and len(generation) == 32
            and all(character in "0123456789abcdef" for character in generation))


def mac_repository_record(value: object) -> MacRepositoryIdentity | CleanupOnlyMacRepository:
    if not isinstance(value, dict):
        raise StateReadError("invalid Mac repository identity")
    fields = cast(dict[str, object], value)
    key, mirror, file, fence = (fields.get(field) for field in
                               ("key", "mirror_name", "claim_file", "fence_directory"))
    if not isinstance(key, str) or not key:
        raise StateReadError("invalid Mac repository key")
    if not all(valid_mac_path_component(field) for field in (mirror, file, fence)):
        raise StateReadError("invalid Mac repository path")
    assert isinstance(mirror, str) and isinstance(file, str) and isinstance(fence, str)
    if file != f"{fence}.json":
        raise StateReadError("the Mac claim file and fence directory differ")
    generation = fields.get("fence_generation", "")
    if not isinstance(generation, str) or ("fence_generation" in fields and not valid_fence_generation(generation)):
        raise StateReadError("invalid Mac fence generation")
    history_key = fields.get("history_key", store.UNKNOWN_IDENTITY)
    if not isinstance(history_key, str) or not history_key:
        raise StateReadError("invalid Mac history key")
    if history_key == store.UNKNOWN_IDENTITY:
        return CleanupOnlyMacRepository(key, mirror, file, fence, fence_generation=generation)
    return MacRepositoryIdentity(key, mirror, file, fence, history_key, fence_generation=generation)


class NoSavedMacRepository:
    """The claim has no proven mirror. A legacy scope name does not identify a mirror."""


NO_SAVED_MAC_REPOSITORY = NoSavedMacRepository()
LEGACY_WAIT_CLAIM_RELEASED = 14


SavedMacRepository = MacRepositoryIdentity | CleanupOnlyMacRepository | NoSavedMacRepository


def saved_mac_repository(record: RunRecord) -> SavedMacRepository:
    return mac_repository_record(record["mac_repository"]) if "mac_repository" in record else NO_SAVED_MAC_REPOSITORY


def claim_fence_scope_key(path: Path, record: RunRecord) -> str:
    saved = saved_mac_repository(record)
    return saved.fence_scope_key() if isinstance(saved, MacMirrorIdentity) else path.stem


@dataclass(frozen=True)
class HeldClaim:
    """One claim record and the file that holds it."""

    scope: ClaimScope
    path: Path
    record: RunRecord

    def matches_mac_assignment_scope(self, scope: ClaimScope) -> bool:
        """Match the scope from an assignment with the same claim identity."""
        if self.scope == scope:
            return True
        if isinstance(scope, RepositoryScope) and self.record.get("slot", 0) == scope.slot:
            if scope.key:
                return self.record.get("repo_key") == scope.key
            return self.record.get("repo_name") == scope.name
        return False


class ReservationRetirementError(OSError):
    """A file for the interrupted replacement could not be removed."""

    def __init__(self, path: Path, error: OSError) -> None:
        super().__init__(f"cannot retire Mac reservation {path}: {error}")
        self.path: Path = path


def retire_replaced_reservation(paths: StatePaths, replacement: Path, record: RunRecord) -> tuple[Path, ...]:
    """Remove the exact local reservation under the provider lock."""
    if (paths.platform != "macos" or "remote_host" not in record or "replaced_reservation" not in record
            or not isinstance(saved_mac_repository(record), MacRepositoryIdentity)):
        return ()
    replaced = record["replaced_reservation"]
    path = paths.directory / replaced["file"]
    if path == replacement:
        return ()
    old = read_run(path)
    if (isinstance(old, NoRun) or "remote_host" in old
            or isinstance(saved_mac_repository(old), MacRepositoryIdentity)
            or replaced_reservation(old, path, paths) != replaced):
        return ()
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise ReservationRetirementError(path, error) from error
    return (path,)


def sweep_mac_replacements(paths: StatePaths, claims: tuple[HeldClaim, ...]) -> tuple[tuple[HeldClaim, ...], set[Path]]:
    """Remove incomplete replacements under the provider lock."""
    if paths.platform != "macos":
        return claims, set()
    retired: set[Path] = set()
    retained: set[Path] = set()
    current: list[HeldClaim] = []
    for claim in claims:
        record = claim.record
        if "remote_host" not in record or "replaced_reservation" not in record:
            current.append(claim)
            continue
        pending = claim_pending_start(paths, claim.path, record).exists()
        legacy = "/" not in claim_fence_scope_key(claim.path, record)
        if legacy and not pending:
            # Older runners kept this marker after launch.
            owner = process_identity.process_start(record["pid"])
            if isinstance(owner, process_identity.Unreadable) or owner == process_identity.Started(record["proc_start"]):
                current.append(claim)
                continue
        try:
            retired.update(retire_replaced_reservation(paths, claim.path, record))
            if pending:
                if legacy:
                    completed = record.copy()
                    del completed["replaced_reservation"]
                    write_text_atomic(claim.path, json.dumps(completed) + "\n")
                    claim = HeldClaim(claim.scope, claim.path, completed)
                current.append(claim)
                continue
            claim.path.unlink(missing_ok=True)
            retired.add(claim.path)
        except ReservationRetirementError as error:
            print(str(error), file=sys.stderr)
            retained.add(error.path)
            current.append(claim)
        except OSError as error:
            print(str(ReservationRetirementError(claim.path, error)), file=sys.stderr)
            current.append(claim)
    return tuple(claim for claim in current if claim.path not in retired), retained


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
    if "repo_key" in values:
        record["repo_key"] = required_string(values, "repo_key", path)
    if "repo_name" in values or "repo_key" in values:
        record["repo_name"] = required_string(values, "repo_name", path)
    if "slot" in values:
        slot = values["slot"]
        if type(slot) is not int or slot < 1:
            raise StateReadError(f"{path} has an invalid slot")
        record["slot"] = slot
    if "mac_repository" in values:
        saved = mac_repository_record(values["mac_repository"])
        if saved.claim_file != path.name or saved.key != record.get("repo_key"):
            raise StateReadError(f"{path} has a mismatched Mac repository identity")
        record["mac_repository"] = saved.record()
    if "remote_host" not in values:
        return record
    host = values["remote_host"]
    if not isinstance(host, str) or not host.strip():
        raise StateReadError(f"{path} has an invalid remote_host")
    remote: RemoteJobClaim = {**record, "remote_host": host}
    if "replaced_reservation" in values:
        remote["replaced_reservation"] = read_replaced_reservation(values["replaced_reservation"], path)
    if 'instance_generation' in values:
        generation = required_string(values, 'instance_generation', path)
        if not generation:
            raise StateReadError(f'{path} has an invalid instance_generation')
        remote['instance_generation'] = generation
    if "cleanup_pending" in values:
        if values["cleanup_pending"] is not True:
            raise StateReadError(f"{path} has an invalid cleanup_pending")
        remote["cleanup_pending"] = True
        if "cleanup_request_id" in values:
            request_id = required_string(values, "cleanup_request_id", path)
            if not request_id:
                raise StateReadError(f"{path} has an invalid cleanup_request_id")
            remote["cleanup_request_id"] = request_id
    return remote


def read_replaced_reservation(value: object, path: Path) -> ReplacedMacReservation:
    if not isinstance(value, dict):
        raise StateReadError(f"{path} has an invalid replaced reservation")
    fields = cast(dict[str, object], value)
    file = required_string(fields, "file", path)
    relative = Path(file)
    if file != "run.json" and (len(relative.parts) != 2 or relative.parts[0] != "claims"
                               or relative.suffix != ".json" or str(relative) != file):
        raise StateReadError(f"{path} has an invalid replaced reservation file")
    if "fingerprint" in fields:
        fingerprint = required_string(fields, "fingerprint", path)
        if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
            raise StateReadError(f"{path} has an invalid replaced reservation fingerprint")
    else:
        pid = fields.get("pid")
        if type(pid) is not int:
            raise StateReadError(f"{path} has an invalid replaced reservation pid")
        fingerprint = _reservation_fingerprint(
            required_string(fields, "claim_id", path), required_string(fields, "episode", path),
            required_string(fields, "attempt", path), pid, required_string(fields, "proc_start", path),
            required_string(fields, "since", path))
    return {"file": file, "fingerprint": fingerprint}


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


def mac_repository_identity(worktree: str) -> ResolvedMacMirror | WholeMacScope:
    from scripts.buildlog import repository

    common = repository.git_common_dir(worktree)
    if common is None:
        return WHOLE_MAC
    try:
        source = repository.mac_source_repository(worktree, common)
    except (OSError, SubprocessError, ValueError):
        return WHOLE_MAC
    mirror = Path(source.git_dir).parent.name
    if not valid_mac_path_component(mirror):
        return WHOLE_MAC
    fence = mac_claim_stem(source.key, 0)
    return ResolvedMacMirror(source.key, mirror, f"{fence}.json", fence, worktree, source.checkout_origin)


def target_claim_scope(paths: StatePaths, worktree: str) -> ClaimScope:
    from scripts.buildlog import repository, store

    if paths.platform == "macos":
        identity = mac_repository_identity(worktree)
        return RepositoryScope(identity.mirror_name, identity.key) if isinstance(identity, MacMirrorIdentity) else WHOLE_MAC
    common = repository.git_common_dir(worktree)
    if common is None:
        return WHOLE_MAC
    source = Path(repository.source_git_dir(common))
    name = source.parent.name
    if not name:
        return WHOLE_MAC
    if not (source / "config").is_file():
        return RepositoryScope(name)
    key = repository.repository_key(str(source.parent))
    if key == store.UNKNOWN_IDENTITY:
        return RepositoryScope(name)
    policy_name = key.rsplit("/", 1)[-1] if key and not key.startswith("git-root:") else name
    return RepositoryScope(policy_name, key)


def claim_files(paths: StatePaths) -> tuple[Path, ...]:
    try:
        repository_files = sorted(paths.claims.glob("*.json"))
    except OSError as error:
        raise ClaimStateUnreadable(paths.claims, error) from error
    return (paths.run, *repository_files)


def read_claims(paths: StatePaths) -> tuple[HeldClaim, ...]:
    """Read saved claim scopes from oldest to newest."""
    held: list[HeldClaim] = []
    for path in claim_files(paths):
        try:
            record = read_run(path)
        except StateReadError as error:
            raise ClaimStateUnreadable(path, error) from error
        if isinstance(record, NoRun):
            continue
        key = record.get("repo_key", "")
        if paths.platform == "macos" and not key:
            scope: ClaimScope = WHOLE_MAC
        elif "repo_name" in record:
            name = (record["mac_repository"]["mirror_name"] if "mac_repository" in record else record["repo_name"])
            scope = RepositoryScope(name, key, record.get("slot", 0))
        else:
            scope = WHOLE_MAC if path == paths.run else RepositoryScope(path.stem)
        held.append(HeldClaim(scope, path, record))
    return tuple(sorted(held, key=lambda claim: datetime.fromisoformat(claim.record["since"])))


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


def claim_worktree_path(worktree: str) -> Path:
    """Resolve a worktree path supplied by a claim caller."""
    return Path(worktree).resolve()


def recorded_worktree_matches(saved: str, worktree: Path) -> bool:
    """Compare an absolute saved path with a resolved caller path."""
    return Path(saved).is_absolute() and claim_worktree_path(saved) == worktree


WAIT_CLAIM_CHECK_PREFIX = "cannot adopt the Mac claim: "


class WaitClaimCheck(StrEnum):
    RUNNER_START = "runner pid start"
    NO_RESERVATION = "no reservation"
    RESERVATION_COUNT = "reservation count"
    SCOPE = "scope"
    PID = "pid"
    IDENTITY = "identity"
    SESSION = "submitting session"
    REMOTE_HOST = "remote host"
    OWNER_START = "proc_start"
    WORKTREE = "worktree"
    REMOTE_MISSING = "remote missing"
    BLOCK_STATE = "block state"
    LEASED_SLOT = "leased slot"

    @property
    def diagnostic(self) -> str:
        return WAIT_CLAIM_CHECK_PREFIX + self.value
