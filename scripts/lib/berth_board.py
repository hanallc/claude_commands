"""Read reservation availability without acquiring or changing a reservation."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, cast


class BoardSection(TypedDict):
    journal_position: dict[str, object]
    entries: list[object]


class BoardModel(TypedDict):
    targets: list[object]
    ready_now: BoardSection
    waiting: BoardSection
    unresolved_overlaps: BoardSection
    unconstrained_reservations: BoardSection
    resolved: BoardSection


@dataclass(frozen=True)
class MergeNotDerived:
    protection: tuple[str, ...]


@dataclass(frozen=True)
class MergeEmpty:
    pass


@dataclass(frozen=True)
class MergeProtected:
    scopes: tuple[str, ...]


type RetainedMergeEvidence = MergeNotDerived | MergeEmpty | MergeProtected


@dataclass(frozen=True)
class MergeUnavailable:
    retained_evidence: RetainedMergeEvidence
    failure: str


type LiveMergeExtent = RetainedMergeEvidence | MergeUnavailable


@dataclass(frozen=True)
class MergeReleased:
    at_release: LiveMergeExtent


type MergeExtent = LiveMergeExtent | MergeReleased


def _blocks(scopes: tuple[str, ...], path: str | Path) -> bool:
    candidate = PurePosixPath(path).as_posix()
    for scope in scopes:
        if scope.startswith("tree:"):
            root = scope.removeprefix("tree:").rstrip("/")
            if root == "." or candidate == root or candidate.startswith(root + "/"):
                return True
        elif candidate == scope.removeprefix("file:"):
            return True
    return False


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    target: str
    holder_worktree_root: Path
    holder_branch: str
    scopes: tuple[str, ...]
    lifecycle: str
    edit_blocking_status: Literal["blocking", "clear"]
    # The commit an outstanding reservation protects; empty in every other stage.
    protected_tip: str = ""
    merge_extent: MergeExtent = MergeEmpty()
    source_kind: str = "explicit"

    def blocks(self, path: str | Path) -> bool:
        return self.edit_blocking_status == "blocking" and _blocks(self.scopes, path)

    def blocks_foreign(self, path: str | Path) -> bool:
        """Use cargo-berth's Reservation::protection_in_worktree for a foreign holder."""
        if self.edit_blocking_status != "blocking":
            return False
        if self.source_kind == "plan_shrink" and self.lifecycle == "active":
            return _blocks(self.scopes, path)
        extent = self.merge_extent
        # MergeExtent::protection uses the last evidence when observation fails.
        if isinstance(extent, MergeUnavailable):
            extent = extent.retained_evidence
        if isinstance(extent, MergeProtected):
            return _blocks(extent.scopes, path)
        if isinstance(extent, MergeNotDerived):
            return _blocks(extent.protection, path)
        return False

    def names_file(self, path: str | Path) -> bool:
        """Whether a blocking file scope names this exact path; a tree scope does not."""
        if self.edit_blocking_status != "blocking":
            return False
        candidate = PurePosixPath(path).as_posix()
        return any(not scope.startswith("tree:") and candidate == scope.removeprefix("file:")
                   for scope in self.scopes)


@dataclass(frozen=True)
class Board:
    """The reservations; configured is False for a repository that berth does not configure."""

    entries: tuple[Reservation, ...]
    configured: bool = True

    def reservations(self) -> tuple[Reservation, ...]:
        return self.entries


# cargo-berth's message for a repository where `cargo-berth init` never ran,
# such as a fresh clone. Its envelope has no other field for this case.
LEDGER_NOT_INITIALIZED = "the cargo-berth ledger is not initialized"


@dataclass(frozen=True)
class BoardUnavailable:
    """No board; ledger_missing is True when the repository has no ledger yet."""

    reason: str
    ledger_missing: bool = False


class LedgerMissing(ValueError):
    """The repository has no cargo-berth ledger, so it has no reservations."""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("expected a list")
    return cast(list[object], value)


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("expected a nonempty string")
    return value


def _section(value: object) -> BoardSection:
    fields = _object(value)
    return {"journal_position": _object(fields["journal_position"]), "entries": _list(fields["entries"])}


def _merge_path(value: object) -> str:
    path = _string(value)
    if ("\\" in path or (len(path) > 1 and path[0].isascii() and path[0].isalpha() and path[1] == ":")
            or any(part.lower() in ("", ".", "..", ".git") for part in path.split("/"))):
        raise ValueError("invalid merge extent path")
    return path


def _scopes(value: object, *, declared: bool = False) -> tuple[str, ...]:
    scopes: list[str] = []
    for scope in _list(value):
        if declared and isinstance(scope, str):
            scopes.append(_string(scope))
            continue
        fields = _object(scope)
        kind = fields["kind"]
        if kind not in ("tree", "file"):
            raise ValueError("unknown scope kind")
        if declared:
            path = _string(fields["path"])
            scopes.append("tree:" + path if kind == "tree" else path)
        else:
            scopes.append(f"{kind}:{_merge_path(fields['path'])}")
    if not scopes:
        raise ValueError("reservation has no scopes")
    return tuple(scopes)


def _validate_merge_key(value: object) -> None:
    key = _object(value)
    _ = _string(key["trunk"]), _string(key["head"])
    working_tree = _object(key["working_tree"])
    for field in ("tracked_paths", "untracked_paths"):
        for path in _list(working_tree[field]):
            _ = _merge_path(path)
    if "reservation_group" in key:
        group = _object(key["reservation_group"])
        if group["kind"] == "reservation_shares":
            for identity in _list(group["reservation_ids"]):
                _ = _string(identity)
        elif group["kind"] != "full_holder":
            raise ValueError("unknown merge extent reservation group")


def _merge_evidence(fields: dict[str, object]) -> RetainedMergeEvidence:
    match fields["status"]:
        case "not_derived":
            return MergeNotDerived(_scopes(fields["protection"]))
        case "empty":
            _validate_merge_key(fields["key"])
            return MergeEmpty()
        case "protected":
            _validate_merge_key(fields["key"])
            return MergeProtected(_scopes(fields["scopes"]))
        case _:
            raise ValueError("unknown merge extent evidence status")


def _live_merge_extent(fields: dict[str, object]) -> LiveMergeExtent:
    if fields["status"] == "unavailable":
        return MergeUnavailable(_merge_evidence(_object(fields["retained_evidence"])), _string(fields["failure"]))
    return _merge_evidence(fields)


def _merge_extent(value: object) -> MergeExtent:
    fields = _object(value)
    if fields["status"] == "released":
        return MergeReleased(_live_merge_extent(_object(fields["at_release"])))
    return _live_merge_extent(fields)


def _reservation(value: dict[str, object]) -> Reservation:
    holder = _object(value["holder"])
    branch = holder["branch"]
    if isinstance(branch, dict):
        branch_fields = cast(dict[str, object], branch)
        kind = branch_fields["kind"]
        if kind not in ("attached", "detached"):
            raise ValueError("unknown holder branch kind")
        branch = branch_fields["reference" if kind == "attached" else "head"]
    target = value["target"]
    if isinstance(target, dict):
        target = cast(dict[str, object], target)["ref"]
    lifecycle = value["lifecycle"]
    protected_tip = ""
    if isinstance(lifecycle, dict):
        lifecycle_fields = cast(dict[str, object], lifecycle)
        lifecycle = lifecycle_fields["stage"]
        tip = lifecycle_fields.get("protected_tip")
        protected_tip = tip if isinstance(tip, str) else ""
    status = value["edit_blocking_status"]
    if status not in ("blocking", "clear"):
        raise ValueError("unknown edit blocking status")
    return Reservation(
        _string(value["reservation_id"]), _string(target), Path(_string(holder["worktree_root"])),
        _string(branch), _scopes(value["scopes"], declared=True), _string(lifecycle), status, protected_tip,
        merge_extent=_merge_extent(value["merge_extent"]), source_kind=_string(_object(value["source"])["kind"]),
    )


def _parse_board(value: object) -> Board:
    envelope = _object(value)
    if not isinstance(envelope.get("output_contract_version"), int) or envelope.get("verb") != "board":
        raise ValueError("not a cargo-berth board envelope")
    payload = _object(envelope["payload"])
    if envelope.get("status") == "ledger_unreadable":
        message = str(envelope.get("message") or "cargo-berth ledger unreadable")
        raise (LedgerMissing if LEDGER_NOT_INITIALIZED in message else ValueError)(message)
    if envelope.get("status") == "unconfigured" and payload.get("kind") == "no_facts":
        return Board((), configured=False)
    if payload.get("kind") != "board":
        raise ValueError("envelope contains no board facts")
    fields = _object(payload["data"])
    data: BoardModel = {
        "targets": _list(fields["targets"]),
        "ready_now": _section(fields["ready_now"]),
        "waiting": _section(fields["waiting"]),
        "unresolved_overlaps": _section(fields["unresolved_overlaps"]),
        "unconstrained_reservations": _section(fields["unconstrained_reservations"]),
        "resolved": _section(fields["resolved"]),
    }
    reservations: dict[str, Reservation] = {}
    referenced: set[str] = set()

    def collect(entry: object) -> None:
        if isinstance(entry, str):
            referenced.add(entry)
            return
        fields = _object(entry)
        if "reservation_id" in fields:
            reservation = _reservation(fields)
            previous = reservations.get(reservation.reservation_id)
            if previous is not None and previous != reservation:
                raise ValueError("conflicting reservation snapshots")
            reservations[reservation.reservation_id] = reservation
        elif "reservation" in fields:
            collect(fields["reservation"])
        else:
            # Overlap entries name the two snapshots carried by the other sections.
            referenced.add(_string(fields["deferred"]))
            referenced.add(_string(fields["blocker"]))

    for target in _list(data["targets"]):
        for entry in _list(_object(target)["reservations"]):
            collect(entry)
    for section in (data["ready_now"], data["waiting"], data["unresolved_overlaps"], data["unconstrained_reservations"], data["resolved"]):
        for entry in section["entries"]:
            collect(entry)
    if referenced.difference(reservations):
        raise ValueError("board references reservations without snapshots")
    return Board(tuple(reservations.values()))


def read_board(repo: Path) -> Board | BoardUnavailable:
    try:
        result = subprocess.run(
            ["cargo-berth", "board", "--json"], cwd=repo, capture_output=True, text=True, check=False,
        )
        if not result.stdout.strip():
            return BoardUnavailable(result.stderr.strip() or f"cargo-berth returned no board (exit {result.returncode})")
        value = cast(object, json.loads(result.stdout))
        return _parse_board(value)
    except LedgerMissing as error:
        return BoardUnavailable(str(error), ledger_missing=True)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return BoardUnavailable(str(error))
