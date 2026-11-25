"""File user actions without executing their commands."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

from scripts.lib import atomic_file, file_lock

ActionStatus = Literal["open", "done", "withdrawn"]
REVIEW_PATH = "/console/review.html"


class ActionStoreError(ValueError):
    """An action cannot be read or changed safely."""


def actions_folder() -> Path:
    """Return the action folder selected by FOR_YOU_ACTIONS_DIR."""
    folder = Path(os.environ.get("FOR_YOU_ACTIONS_DIR", "~/.local/state/for-you/actions"))
    try:
        return folder.expanduser()
    except RuntimeError as error:
        raise ActionStoreError(f"Cannot expand the action folder {folder}: {error}") from error


def action_id(key: str) -> str:
    """Accept a canonical UUID or action key without accepting a file path."""
    identifier = key.removeprefix("action/")
    try:
        if str(UUID(identifier)) == identifier:
            return identifier
    except ValueError:
        pass
    raise ActionStoreError("Use an action key or a canonical UUID.")


@dataclass(frozen=True)
class UserAction:
    """One user action with its requester, command, and retained completion evidence."""

    key: str
    version: int
    title: str
    command: str
    machine: str
    cwd: str
    requested_by: str
    reason: str
    waits: str
    concerns: str
    dedupe_key: str
    status: ActionStatus
    created_at: str
    updated_at: str
    completed_at: str = ""
    completion: Literal["manual"] = "manual"
    evidence: str = ""
    withdraw_reason: str = ""

    def __post_init__(self) -> None:
        identifier = action_id(self.key)
        if self.key != f"action/{identifier}" or type(self.version) is not int or self.version != 1:
            raise ActionStoreError("Unsupported action key or version.")
        for name, value in cast(dict[str, object], asdict(self)).items():
            if name != "version" and not isinstance(value, str):
                raise ActionStoreError(f"{name}: expected text.")
        for name in ("title", "command", "machine", "cwd", "requested_by", "reason", "waits", "concerns", "dedupe_key"):
            if not cast(str, getattr(self, name)).strip():
                raise ActionStoreError(f"{name}: provide a value.")
        if not Path(self.cwd).is_absolute():
            raise ActionStoreError("cwd: provide an absolute path on the target machine.")
        if self.status not in ("open", "done", "withdrawn") or self.completion != "manual":
            raise ActionStoreError("Unsupported action status or completion method.")
        created, updated = self._time(self.created_at), self._time(self.updated_at)
        if updated < created:
            raise ActionStoreError("The update time precedes the creation time.")
        if self.status == "done":
            if self._time(self.completed_at) < created or not self.evidence.strip() or self.withdraw_reason:
                raise ActionStoreError("A done action requires a completion time and evidence.")
        elif self.completed_at or self.evidence:
            raise ActionStoreError("Only a done action can hold completion evidence.")
        if (self.status == "withdrawn") != bool(self.withdraw_reason.strip()):
            raise ActionStoreError("Only a withdrawn action requires a withdrawal reason.")

    @staticmethod
    def _time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ActionStoreError("Action times require a time zone.")
        return parsed

    @classmethod
    def decode(cls, text: str) -> UserAction:
        """Validate a stored record before it enters the action model."""
        value = cast(object, json.loads(text))
        if not isinstance(value, dict):
            raise ActionStoreError("An action record must be an object.")
        fields = cast(dict[str, object], value)
        version = fields.get("version")
        if type(version) is not int:
            raise ActionStoreError("version: expected an integer.")
        text_fields: dict[str, str] = {}
        for name in ("key", "title", "command", "machine", "cwd", "requested_by", "reason", "waits", "concerns",
                     "dedupe_key", "status", "created_at", "updated_at", "completed_at", "completion", "evidence",
                     "withdraw_reason"):
            item = fields.get(name)
            if not isinstance(item, str):
                raise ActionStoreError(f"{name}: expected text.")
            text_fields[name] = item
        return cls(text_fields["key"], version, text_fields["title"], text_fields["command"], text_fields["machine"],
                   text_fields["cwd"], text_fields["requested_by"], text_fields["reason"], text_fields["waits"],
                   text_fields["concerns"], text_fields["dedupe_key"], cast(ActionStatus, text_fields["status"]),
                   text_fields["created_at"], text_fields["updated_at"], text_fields["completed_at"],
                   cast(Literal["manual"], text_fields["completion"]), text_fields["evidence"], text_fields["withdraw_reason"])


@dataclass(frozen=True)
class ActionStore:
    """Own all action writes under one folder lock."""

    folder: Path = field(default_factory=actions_folder)

    def list(self, *, watch: Callable[[Path], None] | None = None) -> tuple[UserAction, ...]:
        """Read every retained action or report the damaged source."""
        if watch is not None:
            watch(self.folder)
        try:
            paths = sorted(path for path in self.folder.iterdir() if path.suffix == ".json")
        except FileNotFoundError:
            if watch is not None and (self.folder.exists() or self.folder.is_symlink()):
                raise
            return ()
        records: list[UserAction] = []
        for path in paths:
            try:
                if watch is not None:
                    watch(path)
                record = UserAction.decode(path.read_text(encoding="utf-8"))
                if path.stem != action_id(record.key):
                    raise ActionStoreError("The filename does not match the action key.")
                records.append(record)
            except (OSError, ValueError, TypeError) as error:
                raise ActionStoreError(f"{path.name}: {error}") from error
        return tuple(sorted(records, key=lambda record: (record.created_at, record.key)))

    def _write(self, record: UserAction) -> UserAction:
        path = self.folder / f"{action_id(record.key)}.json"
        atomic_file.write_text_atomic(path, json.dumps(asdict(record), indent=2) + "\n", new_file_mode=0o600)
        return record

    def add(self, *, title: str, command: str, machine: str, cwd: str, requested_by: str,
            reason: str, waits: str, concerns: str, dedupe_key: str, key: str = "") -> UserAction:
        """Create an action or return the same key or open request."""
        now = datetime.now(timezone.utc).isoformat()
        identifier = action_id(key) if key else str(uuid4())
        record = UserAction(f"action/{identifier}", 1, title, command, machine, cwd, requested_by,
                            reason, waits, concerns, dedupe_key, "open", now, now)
        self.folder.mkdir(parents=True, exist_ok=True)
        with file_lock.exclusive(self.folder / ".lock"):
            records = self.list()
            for existing in records:
                if existing.key == record.key:
                    if (existing.requested_by, existing.dedupe_key) != (requested_by, dedupe_key):
                        raise ActionStoreError("Another request uses this action key.")
                    return existing
            for existing in records:
                if (existing.status == "open" and existing.requested_by == requested_by
                        and existing.dedupe_key == dedupe_key):
                    return existing
            return self._write(record)

    def get(self, key: str) -> UserAction:
        """Read the action with this key."""
        canonical = f"action/{action_id(key)}"
        for record in self.list():
            if record.key == canonical:
                return record
        raise ActionStoreError(f"Unknown action: {canonical}")

    def withdraw(self, key: str, *, requested_by: str, reason: str) -> UserAction:
        """Withdraw an open action only for its original requester."""
        if not reason.strip():
            raise ActionStoreError("Provide a withdrawal reason.")
        self.folder.mkdir(parents=True, exist_ok=True)
        with file_lock.exclusive(self.folder / ".lock"):
            record = self.get(key)
            if record.requested_by != requested_by:
                raise ActionStoreError("Another requester owns this action.")
            if record.status != "open":
                raise ActionStoreError("Only an open action can be withdrawn.")
            return self._write(replace(record, status="withdrawn", withdraw_reason=reason,
                                       updated_at=datetime.now(timezone.utc).isoformat()))

    def mark_done(self, key: str, *, evidence: str) -> UserAction:
        """Record manual completion after the signed route authorizes the user."""
        if not evidence.strip():
            raise ActionStoreError("Provide completion evidence.")
        self.folder.mkdir(parents=True, exist_ok=True)
        with file_lock.exclusive(self.folder / ".lock"):
            record = self.get(key)
            if record.status == "done":
                return record
            if record.status != "open":
                raise ActionStoreError("Only an open action can be completed.")
            now = datetime.now(timezone.utc).isoformat()
            return self._write(replace(record, status="done", updated_at=now, completed_at=now, evidence=evidence))


def review_place(key: str = "") -> str:
    """Link to the open actions or select one action."""
    fields = {"type": "for-you", "show": "open"}
    if key:
        fields["item"] = key
    return REVIEW_PATH + "?" + urlencode(fields)


def card_url(key: str) -> str:
    """Use the existing Review origin and path without creating a console page."""
    from scripts.review_links import review_link

    root = review_link.review_root()
    url = review_link.snapshot_url(root, root / REVIEW_PATH.lstrip("/"))
    return url + review_place(key).removeprefix(REVIEW_PATH)


class Arguments(argparse.Namespace):
    """The fields accepted by the action filer."""

    operation: str = ""
    title: str = ""
    command: str = ""
    machine: str = ""
    cwd: str = ""
    requested_by: str = ""
    reason: str = ""
    waits: str = ""
    concerns: str = ""
    dedupe_key: str = ""
    id: str = ""
    json: bool = False


def main(argv: list[str]) -> int:
    """File, list, or withdraw an action without running its command."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    add = commands.add_parser("add")
    for name in ("title", "command", "machine", "cwd", "requested-by", "reason", "waits", "concerns", "dedupe-key"):
        _ = add.add_argument("--" + name, required=True)
    listing = commands.add_parser("list")
    _ = listing.add_argument("--json", action="store_true")
    withdraw = commands.add_parser("withdraw")
    for name in ("id", "reason", "requested-by"):
        _ = withdraw.add_argument("--" + name, required=True)
    args = parser.parse_args(argv, namespace=Arguments())
    store = ActionStore()
    try:
        if args.operation == "add":
            record = store.add(title=args.title, command=args.command, machine=args.machine, cwd=args.cwd,
                               requested_by=args.requested_by, reason=args.reason, waits=args.waits,
                               concerns=args.concerns, dedupe_key=args.dedupe_key)
            print(record.key)
            print(card_url(record.key))
        elif args.operation == "withdraw":
            print(store.withdraw(args.id, requested_by=args.requested_by, reason=args.reason).key)
        else:
            records = store.list()
            if args.json:
                print(json.dumps([asdict(record) for record in records]))
            else:
                for record in records:
                    print(f"{record.key} {record.status} {record.title}")
    except (OSError, ValueError) as error:
        print(f"for-you: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
