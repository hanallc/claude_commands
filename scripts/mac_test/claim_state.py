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
from scripts.build_targets import target_config
from scripts.direct.runner_result import (
    MAC_HISTORY_KEY_UNAVAILABLE as MAC_HISTORY_KEY_UNAVAILABLE,
    MAC_HISTORY_KEY_UNAVAILABLE_STATUS as MAC_HISTORY_KEY_UNAVAILABLE_STATUS,
)
from scripts.lib import process_start as process_identity
from scripts.lib.atomic_file import write_text_atomic

if TYPE_CHECKING:
    from scripts.buildlog.repository import MacSourceRepository
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


@dataclass(frozen=True)
class StateFileReadFailed:
    """An OS error prevented a read of the state file."""

    path: Path
    error: OSError

    @property
    def diagnostic(self) -> str:
        return f"{self.path}: {self.error}"


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
    """The local process holds capacity until it releases the claim or ends."""

    wait_released: NotRequired[Literal[True]]


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


class UnknownMacHistoryKeyError(ValueError):
    """The history reader returned its unknown identity."""


@dataclass(frozen=True)
class KnownMacHistoryKey:
    """The submitting checkout has this history key."""

    key: str

    def __post_init__(self) -> None:
        from scripts.buildlog import store

        if not self.key:
            raise ValueError("the Mac history key is empty")
        if self.key == store.UNKNOWN_IDENTITY:
            raise UnknownMacHistoryKeyError("the Mac history key is unknown")


class UnavailableMacHistoryKey:
    """The submitting checkout has no known history key."""


class ReadCheckoutHistoryKey:
    """Read the history key from the submitting checkout."""


READ_CHECKOUT_HISTORY_KEY = ReadCheckoutHistoryKey()


def mac_history_key(key: str) -> KnownMacHistoryKey | UnavailableMacHistoryKey:
    """Convert the result of the build history reader."""
    try:
        return KnownMacHistoryKey(key)
    except ValueError:
        return UnavailableMacHistoryKey()


@dataclass(frozen=True)
class ResolvedMacMirror(MacMirrorIdentity):
    """The mirror and the checkout that needs it."""

    worktree: str

    def claim_repository(
        self, *, history_key: KnownMacHistoryKey | UnavailableMacHistoryKey | ReadCheckoutHistoryKey = READ_CHECKOUT_HISTORY_KEY,
    ) -> MacRepositoryIdentity | CleanupOnlyMacRepository:
        if isinstance(history_key, ReadCheckoutHistoryKey):
            from scripts.buildlog import repository

            history_key = mac_history_key(repository.repository_key(self.worktree))
        if isinstance(history_key, UnavailableMacHistoryKey):
            return CleanupOnlyMacRepository(self.key, self.mirror_name, self.claim_file, self.fence_directory)
        return MacRepositoryIdentity(self.key, self.mirror_name, self.claim_file, self.fence_directory, history_key)


@dataclass(frozen=True)
class MacRepositoryIdentity(MacMirrorIdentity):
    """The saved mirror and history key required to execute work."""

    history_key: KnownMacHistoryKey

    @override
    def record(self) -> MacRepositoryRecord:
        return {**super().record(), "history_key": self.history_key.key}


def mac_claim_key(key: str) -> str:
    """Fold the path of a GitHub key for Mac claims."""
    prefix, separator, path = key.removeprefix("https://").partition("/")
    if key.startswith("https://") and prefix.partition(":")[0] == "github.com":
        return f"https://{prefix}{separator}{path.lower()}"
    return key


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
    from scripts.buildlog import store

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
    raw_history_key = fields.get("history_key", store.UNKNOWN_IDENTITY)
    if not isinstance(raw_history_key, str):
        raise StateReadError("invalid Mac history key")
    try:
        history_key = KnownMacHistoryKey(raw_history_key)
    except UnknownMacHistoryKeyError:
        return CleanupOnlyMacRepository(key, mirror, file, fence, fence_generation=generation)
    except ValueError as exc:
        raise StateReadError("invalid Mac history key") from exc
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


def claim_uses_legacy_fence(record: RunRecord) -> bool:
    saved = saved_mac_repository(record)
    return not isinstance(saved, MacMirrorIdentity) or not saved.fence_generation


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
                return mac_claim_key(self.record.get("repo_key", "")) == mac_claim_key(scope.key)
            return self.record.get("repo_name") == scope.name
        return False


class ReservationRetirementError(OSError):
    """A file for the interrupted replacement could not be removed."""

    def __init__(self, path: Path, error: OSError) -> None:
        super().__init__(f"cannot retire Mac reservation {path}: {error}")
        self.path: Path = path


def retired_mac_reservation_path(paths: StatePaths, identity: ClaimIdentity) -> Path:
    return paths.retired_reservations / pending_start_key(identity)


class UnrecordedReservationPath:
    """The waiter or an older proof has no saved reservation path."""


@dataclass(frozen=True)
class RetiredMacReservation:
    """The saved permission for the original waiter to retry a retired reservation."""

    path: Path
    pid: int
    proc_start: str
    submitting_session: str
    worktree: str
    reservation: Path | UnrecordedReservationPath


class NoSavedRetryPermission:
    """No saved permission authorizes this retry."""


NO_SAVED_RETRY_PERMISSION = NoSavedRetryPermission()


def read_retired_mac_reservation(
    paths: StatePaths, identity: ClaimIdentity,
) -> RetiredMacReservation | NoSavedRetryPermission:
    """Read the saved retry permission for this claim identity."""
    path = retired_mac_reservation_path(paths, identity)
    values = read_json_object(path)
    if values is None:
        return NO_SAVED_RETRY_PERMISSION
    record = decode_run(values, path)
    if "remote_host" in record or "wait_released" in record or claim_identity(record) != identity:
        return NO_SAVED_RETRY_PERMISSION
    reservation = (paths.directory / reservation_file(values, "reservation_file", path)
                   if "reservation_file" in values else UnrecordedReservationPath())
    return RetiredMacReservation(path, record["pid"], record["proc_start"], record["submitting_session"],
                                 record["worktree"], reservation)


def claim_owner_ended(record: RunRecord) -> bool:
    """Confirm that the saved process no longer owns the claim."""
    owner = process_identity.process_start(record["pid"])
    return not isinstance(owner, process_identity.Unreadable) and owner != process_identity.Started(record["proc_start"])


def _sweep_retired_mac_reservations(paths: StatePaths) -> None:
    """Remove retry permissions for ended waiters under the provider lock."""
    for path in paths.retired_reservations.glob("[0-9a-f]" * 64):
        try:
            record = read_run(path)
            if (not isinstance(record, NoRun) and "remote_host" not in record
                    and path == retired_mac_reservation_path(paths, claim_identity(record))
                    and claim_owner_ended(record)):
                path.unlink(missing_ok=True)
        except (StateReadError, OSError) as error:
            print(str(error), file=sys.stderr)


def retire_replaced_reservation(paths: StatePaths, replacement: Path, record: RunRecord, *,
                                allow_retry: bool = False) -> tuple[Path, ...]:
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
        if "wait_released" in old:
            retired_mac_reservation_path(paths, claim_identity(old)).unlink(missing_ok=True)
        elif allow_retry:
            proof = retired_mac_reservation_path(paths, claim_identity(old))
            proof.parent.mkdir(parents=True, exist_ok=True)
            # Keep the reservation identity if the writer stops before the replacement commits.
            _ = old.pop("mac_repository", None)
            write_text_atomic(proof, json.dumps({**old, "reservation_file": replaced["file"]}) + "\n")
        path.unlink(missing_ok=True)
    except OSError as error:
        raise ReservationRetirementError(path, error) from error
    return (path,)


def sweep_mac_replacements(paths: StatePaths, claims: tuple[HeldClaim, ...]) -> tuple[tuple[HeldClaim, ...], set[Path]]:
    """Remove incomplete replacements and expired retry permissions under the provider lock."""
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
        legacy = claim_uses_legacy_fence(record)
        if legacy and not pending and not claim_owner_ended(record):
            current.append(claim)
            continue
        try:
            retired.update(retire_replaced_reservation(paths, claim.path, record, allow_retry=not legacy and not pending))
            if legacy or pending:
                # Keep the claim for remote cleanup. Remote work can still run.
                completed = record.copy()
                del completed["replaced_reservation"]
                write_text_atomic(claim.path, json.dumps(completed) + "\n")
                current.append(HeldClaim(claim.scope, claim.path, completed))
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
    _sweep_retired_mac_reservations(paths)
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
        self.retired_reservations: Path = directory / "retired-reservations"
        self.wait_locations: Path = directory / "wait-locations"
        self.block: Path = directory / "block.json"
        self.affinity: Path = directory / "affinity"


def state_paths(target: str = "") -> StatePaths:
    if not target:
        return StatePaths(target_config.ssh_target_state_directory("mac"))
    configured = target_config.configured_ssh_target(target)
    return StatePaths(target_config.ssh_target_state_directory(target), target,
                      registry_selected=True, platform=configured.platform)


@contextmanager
def locked(path: Path, *, wait: bool = True) -> Generator[None, None, None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with record_after_unlock(), path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_json_object(path: Path) -> dict[str, object] | None:
    values = _read_json_object(path)
    if isinstance(values, StateFileReadFailed):
        raise StateReadError(values.diagnostic) from values.error
    return values


def _read_json_object(path: Path) -> dict[str, object] | None | StateFileReadFailed:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        return StateFileReadFailed(path, error)
    except UnicodeError as error:
        raise StateReadError(f"{path}: {error}") from error
    try:
        decoded = cast(object, json.loads(contents))
    except (ValueError, RecursionError) as error:
        raise StateReadError(f"{path}: {error}") from error
    if not isinstance(decoded, dict):
        raise StateReadError(f"{path} does not contain a JSON object")
    return cast(dict[str, object], decoded)


def read_run(path: Path) -> RunState:
    values = read_json_object(path)
    return NO_RUN if values is None else decode_run(values, path)


def decode_run(values: dict[str, object], path: Path, *, moved_from_claim: bool = False) -> RunRecord:
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
        try:
            saved = mac_repository_record(values["mac_repository"])
        except StateReadError as error:
            raise StateReadError(f"{path}: {error}") from error
        if (not moved_from_claim and saved.claim_file != path.name) or saved.key != record.get("repo_key"):
            raise StateReadError(f"{path} has a mismatched Mac repository identity")
        record["mac_repository"] = saved.record()
    if "remote_host" not in values:
        if "wait_released" in values:
            if values["wait_released"] is not True:
                raise StateReadError(f"{path} has an invalid wait_released flag")
            record["wait_released"] = True
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
    file = reservation_file(fields, "file", path)
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


def reservation_file(fields: dict[str, object], field: str, path: Path) -> str:
    file = required_string(fields, field, path)
    relative = Path(file)
    if file != "run.json" and (len(relative.parts) != 2 or relative.parts[0] != "claims"
                               or relative.suffix != ".json" or str(relative) != file):
        raise StateReadError(f"{path} has an invalid reservation file")
    return file


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
    if isinstance(source, repository.UnrecordedSourceCheckout):
        return WHOLE_MAC
    mirror = _mac_mirror_name(source)
    if not valid_mac_path_component(mirror):
        return WHOLE_MAC
    key = mac_claim_key(source.key)
    fence = mac_claim_stem(key, 0)
    return ResolvedMacMirror(key, mirror, f"{fence}.json", fence, worktree)


def _mac_mirror_name(source: "MacSourceRepository") -> str:
    """Name the mirror after the checkout that lends its objects when it has the same origin.

    A prepared or merged copy is a shared clone in a folder named for the copy.
    Its own folder name is not the repository name; the lending checkout's name is.
    """
    from scripts.buildlog import repository

    lender = repository.borrowed_git_dir(source.git_dir)
    if lender is not None:
        try:
            lent = repository.mac_source_repository(str(Path(lender).parent), lender)
        except (OSError, SubprocessError, ValueError):
            lent = None
        if isinstance(lent, repository.MacSourceRepository) and lent.key == source.key:
            return Path(lent.git_dir).parent.name
    return Path(source.git_dir).parent.name


def has_mac_worktree(worktree: str) -> bool:
    """Recognize a Git checkout even when its metadata is unreadable."""
    from scripts.lib import git_files

    try:
        _ = git_files.locate(Path(worktree))
    except git_files.NotACheckout:
        return False
    except git_files.UnreadableGitFiles:
        pass
    return True


def scopes_overlap(first: ClaimScope, second: ClaimScope, *, mac: bool = False) -> bool:
    if isinstance(first, WholeMacScope) or isinstance(second, WholeMacScope):
        return True
    if first.key and second.key:
        same_repository = mac_claim_key(first.key) == mac_claim_key(second.key) if mac else first.key == second.key
    else:
        same_repository = first.name == second.name
    return same_repository and (not first.slot or not second.slot or first.slot == second.slot)


def cleanup_scope_is_known(paths: StatePaths, claim: HeldClaim) -> bool:
    """Require a repository identity or an absolute path for a legacy machine claim."""
    return (claim.path != paths.run or "repo_name" in claim.record
            or Path(claim.record["worktree"]).is_absolute())


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


@dataclass(frozen=True)
class WaitReservationLocation:
    """The claim file that a particular waiter reserved."""

    path: Path
    pid: int
    proc_start: str
    submitting_session: str

    @classmethod
    def for_claim(cls, path: Path, claim: ProcessBoundClaim) -> WaitReservationLocation:
        return cls(path, claim["pid"], claim["proc_start"], claim["submitting_session"])


def released_wait_path(paths: StatePaths, identity: ClaimIdentity) -> Path:
    return paths.wait_locations / f"{pending_start_key(identity)}.released"


def record_wait_removal(paths: StatePaths, identity: ClaimIdentity, path: Path) -> None:
    """Remove the reservation and record its release in one operation under the provider lock."""
    released = released_wait_path(paths, identity)
    released.parent.mkdir(parents=True, exist_ok=True)
    _ = path.replace(released)


def save_wait_location(paths: StatePaths, identity: ClaimIdentity, reservation: WaitReservationLocation) -> None:
    """Save the claim path before release needs to read the claim."""
    location = paths.wait_locations / pending_start_key(identity)
    location.parent.mkdir(parents=True, exist_ok=True)
    values = {"reservation_file": str(reservation.path.relative_to(paths.directory)), "pid": reservation.pid,
              "proc_start": reservation.proc_start, "submitting_session": reservation.submitting_session}
    write_text_atomic(location, json.dumps(values) + "\n")
    released_wait_path(paths, identity).unlink(missing_ok=True)


@dataclass(frozen=True)
class WaiterProcessIdentity:
    """The process that owned the wait reservation."""

    pid: int
    proc_start: str

    def ended(self) -> bool:
        owner = process_identity.process_start(self.pid)
        return not isinstance(owner, process_identity.Unreadable) and owner != process_identity.Started(self.proc_start)


def _wait_location_owner(values: dict[str, object], location: Path) -> WaiterProcessIdentity:
    pid = values.get("pid")
    if type(pid) is not int or pid < 1:
        raise StateReadError(f"{location} has an invalid waiter pid")
    start = values.get("proc_start")
    if not isinstance(start, str) or not start:
        raise StateReadError(f"{location} has an invalid waiter proc_start")
    return WaiterProcessIdentity(pid, start)


class NoWaitRemoval:
    """Release has no saved removal record for this reservation."""


def read_wait_removal(path: Path) -> WaiterProcessIdentity | NoWaitRemoval | StateFileReadFailed:
    """Read the owner from the reservation that release moved to this path."""
    values = _read_json_object(path)
    if isinstance(values, StateFileReadFailed):
        return values
    if values is None:
        return NoWaitRemoval()
    record = decode_run(values, path, moved_from_claim=True)
    if "remote_host" in record or path.stem != pending_start_key(claim_identity(record)):
        raise StateReadError(f"{path} has an invalid wait removal record")
    if "reservation_file" in values:
        _ = reservation_file(values, "reservation_file", path)
    return _wait_location_owner(values, path)


def read_wait_location(paths: StatePaths, identity: ClaimIdentity) -> WaitReservationLocation | UnrecordedReservationPath:
    return _read_wait_location(paths, paths.wait_locations / pending_start_key(identity))


def _read_wait_location(paths: StatePaths, location: Path) -> WaitReservationLocation | UnrecordedReservationPath:
    values = read_json_object(location)
    if values is None:
        return UnrecordedReservationPath()
    owner = _wait_location_owner(values, location)
    return WaitReservationLocation(paths.directory / reservation_file(values, "reservation_file", location), owner.pid,
                                   owner.proc_start, required_string(values, "submitting_session", location))


def _wait_slot_has_claim(paths: StatePaths, location: Path, live_paths: set[Path]) -> bool:
    if not live_paths:
        return False
    # Keep the removal record while an unknown slot can still hold a claim.
    try:
        saved = _read_wait_location(paths, location)
    except StateReadError:
        return True
    return isinstance(saved, UnrecordedReservationPath) or saved.path in live_paths


_reported_wait_removal_reads: dict[Path, set[Path]] = {}


def sweep_wait_locations(paths: StatePaths, live_paths: set[Path]) -> None:
    """Sweep wait records after the sweep of live claims under the provider lock."""
    key = "[0-9a-f]" * 64
    retained_locations: set[Path] = set()
    reported_reads = _reported_wait_removal_reads.get(paths.wait_locations, set())
    failed_reads: set[Path] = set()
    for path in paths.wait_locations.glob(f"{key}.released"):
        try:
            try:
                owner = read_wait_removal(path)
            except StateReadError as error:
                location = path.with_suffix("")
                if _wait_slot_has_claim(paths, location, live_paths):
                    retained_locations.add(location)
                else:
                    path.unlink(missing_ok=True)
                    print(str(error), file=sys.stderr)
                continue
            if isinstance(owner, StateFileReadFailed):
                failed_reads.add(path)
                # Keep the saved slot path until the sweep can read the removal record.
                retained_locations.add(path.with_suffix(""))
                if path not in reported_reads:
                    print(owner.diagnostic, file=sys.stderr)
            elif isinstance(owner, WaiterProcessIdentity) and owner.ended():
                path.unlink(missing_ok=True)
        except OSError as error:
            print(str(error), file=sys.stderr)
    if failed_reads:
        _reported_wait_removal_reads[paths.wait_locations] = failed_reads
    else:
        _ = _reported_wait_removal_reads.pop(paths.wait_locations, None)
    for path in paths.wait_locations.glob(key):
        if path in retained_locations:
            continue
        try:
            values = read_json_object(path)
            if values is not None and _wait_location_owner(values, path).ended():
                path.unlink(missing_ok=True)
        except (StateReadError, OSError) as error:
            print(str(error), file=sys.stderr)


@dataclass(frozen=True)
class WaitReservation:
    """The local claim and its original fields for a release marker."""

    path: Path
    record: ProcessBoundClaim
    original_fields: dict[str, object]


class UnlocatedWaitReservation(StateReadError):
    """An unreadable claim could be a reservation with no recorded path."""


def read_wait_reservations(
    paths: StatePaths, identity: ClaimIdentity, reservation: Path | UnrecordedReservationPath,
) -> Generator[WaitReservation, None, None]:
    """Read the exact local reservation for release."""
    expected = (identity.claim_id, identity.episode, identity.attempt)
    unreadable: StateReadError | None = None
    found = False
    for path in (reservation,) if isinstance(reservation, Path) else claim_files(paths):
        try:
            values = read_json_object(path)
            if values is None:
                continue
            fields = (values.get("claim_id"), values.get("episode"), values.get("attempt"))
            if fields != expected:
                if not any(
                    isinstance(value, str) and value and value != name for value, name in zip(fields, expected)
                ):
                    raise StateReadError(f"{path} has an incomplete reservation identity")
                continue
        except StateReadError as error:
            if isinstance(reservation, Path):
                raise
            if unreadable is None:
                unreadable = error
            continue
        record = decode_run(values, path)
        if "remote_host" in record:
            continue
        found = True
        yield WaitReservation(path, record, values)
    if not found and unreadable is not None:
        raise UnlocatedWaitReservation(str(unreadable)) from unreadable


def mark_wait_released(path: Path, values: dict[str, object]) -> None:
    """Record release of the exact local reservation under the provider lock."""
    write_text_atomic(path, json.dumps({**values, "wait_released": True}) + "\n")


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
