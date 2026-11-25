"""Share one ad hoc review between Claude sessions on this machine."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import date
from functools import cache
from pathlib import Path
from typing import Literal, TypedDict, cast, final
from uuid import UUID, uuid4

from scripts.lib import atomic_file, file_lock
from scripts.message import sessions
from scripts.production import staff


class ReviewError(ValueError):
    """The requested review operation cannot proceed."""


class NoLocalReview(ReviewError):
    """This session has no shared document on this machine."""


class Decision(TypedDict):
    words: str
    actions: str


class ActiveReview(TypedDict):
    id: str
    started: str
    source: str
    items: list[str]
    claims: dict[str, str]
    decisions: dict[str, Decision]


class ReviewHandle(TypedDict):
    owner: str
    review: str
    document: str
    remaining: int


class ClaimedItem(TypedDict):
    state: Literal["claimed"]
    item: int
    text: str
    remaining: int


class NoAvailableItem(TypedDict):
    state: Literal["busy", "complete"]
    remaining: int


class NextItem:
    """Select the next unclaimed item in the review's order."""


type SessionLiveness = sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown
RECORD_PREFIX = "<!-- adhoc-review "
RECORD_SUFFIX = " -->"


def session_id(text: str) -> str:
    try:
        if str(UUID(text)) == text:
            return text
    except ValueError:
        pass
    raise ReviewError("A Claude session ID is required.")


def item_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ReviewError("The item list must contain one text string per item.")
    items = cast(list[object], value)
    if not items or not all(isinstance(item, str) and item.strip() for item in items):
        raise ReviewError("The item list must contain one nonempty text string per item.")
    return cast(list[str], items)


def remaining(review: ActiveReview) -> int:
    return len(review["items"]) - len(review["decisions"].keys() - review["claims"].keys())


def decision_document(review: ActiveReview) -> str:
    lines = [f'# Ad hoc review {review["started"]}', f'Source: {review["source"]}', '']
    for index, item in enumerate(review["items"], 1):
        if (decision := review["decisions"].get(str(index))) is not None:
            lines.append(f'{index}. {item.strip().splitlines()[0]} → {decision["words"]}')
            if decision["actions"]:
                lines.extend(f'   {line}' for line in decision["actions"].splitlines())
    return "\n".join(lines) + "\n"


def _save(path: Path, review: ActiveReview) -> None:
    metadata = json.dumps(review).replace("<", r"\u003c").replace(">", r"\u003e")
    atomic_file.write_text_atomic(path, RECORD_PREFIX + metadata + RECORD_SUFFIX + "\n" + decision_document(review),
                                  new_file_mode=0o600)


def _read(path: Path) -> ActiveReview:
    try:
        first = path.read_text(encoding="utf-8").split("\n", 1)[0]
    except FileNotFoundError as error:
        raise NoLocalReview("No open review for this session on this machine.") from error
    if not first.startswith(RECORD_PREFIX) or not first.endswith(RECORD_SUFFIX):
        raise ReviewError(f"Cannot read review record {path}.")
    return cast(ActiveReview, json.loads(first[len(RECORD_PREFIX):-len(RECORD_SUFFIX)]))


def _remove(path: Path) -> None:
    atomic_file.remove_dead_temporary_files(path)
    path.unlink()


@final
class ReviewStore:
    def __init__(self, root: Path, liveness: Callable[[str], SessionLiveness]) -> None:
        self.root = root
        self.liveness = liveness

    @contextmanager
    def locked_path(self, owner: str) -> Generator[Path]:
        path = self.root / f"{session_id(owner)}.md"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Keep the same lock inode when an owner ends a review and starts another.
        with file_lock.exclusive(self.root / ".lock"):
            yield path

    def start(self, owner: str, source: str, items: list[str]) -> ReviewHandle:
        with self.locked_path(owner) as path:
            if path.exists():
                review = _read(path)
                if (review["source"], review["items"]) != (source, items):
                    raise ReviewError("This session already has an open review. Resume it or end it first.")
            else:
                review = ActiveReview(id=uuid4().hex, started=date.today().isoformat(), source=source,
                                      items=items, claims={}, decisions={})
                _save(path, review)
            return self.handle(owner, path, review)

    def handle(self, owner: str, path: Path, review: ActiveReview) -> ReviewHandle:
        return ReviewHandle(owner=owner, review=review["id"], document=str(path), remaining=remaining(review))

    def check_owner(self, actor: str, owner: str, path: Path) -> None:
        if actor == owner:
            return
        observed = self.liveness(owner)
        if isinstance(observed, sessions.SessionNotLive):
            _remove(path)
            raise ReviewError("The review owner has ended their session.")
        if isinstance(observed, sessions.SessionLivenessUnknown):
            raise ReviewError(f"Cannot confirm the review owner's session: {observed.reason}")

    def join(self, actor: str, owner: str) -> ReviewHandle:
        with self.locked_path(owner) as path:
            review = _read(path)
            self.check_owner(actor, owner, path)
            return self.handle(owner, path, review)

    @contextmanager
    def opened(self, actor: str, owner: str, review_id: str) -> Generator[tuple[Path, ActiveReview]]:
        with self.locked_path(owner) as path:
            try:
                review = _read(path)
            except NoLocalReview as error:
                raise ReviewError("This review has ended.") from error
            if review["id"] != review_id:
                raise ReviewError("This review has ended. Join the new review to work on its items.")
            self.check_owner(actor, owner, path)
            yield path, review

    def claim(self, path: Path, review: ActiveReview, actor: str, requested: int | NextItem) -> ClaimedItem | NoAvailableItem:
        claims = review["claims"]
        ended = [index for index, holder in claims.items()
                 if holder != actor and isinstance(self.liveness(holder), sessions.SessionNotLive)]
        for index in ended:
            del claims[index]
        current = next((index for index, holder in claims.items() if holder == actor), "")
        if isinstance(requested, int):
            if not 1 <= requested <= len(review["items"]):
                raise ReviewError(f"No item {requested}.")
            index = str(requested)
            if index in claims and claims[index] != actor:
                raise ReviewError(f"Another session holds item {requested}.")
        else:
            index = current or next((str(number) for number in range(1, len(review["items"]) + 1)
                                     if str(number) not in claims and str(number) not in review["decisions"]), "")
        if not index:
            if ended:
                _save(path, review)
            count = remaining(review)
            return NoAvailableItem(state="busy" if count else "complete", remaining=count)
        if current and current != index:
            del claims[current]
        claims[index] = actor
        _save(path, review)
        return ClaimedItem(state="claimed", item=int(index), text=review["items"][int(index) - 1],
                           remaining=remaining(review))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    _ = start.add_argument("--source", required=True)
    _ = start.add_argument("--items", required=True, type=Path, help="A JSON array of item texts.")
    _ = commands.add_parser("join").add_argument("staff_name")
    _ = commands.add_parser("resume")
    for verb in ("next", "record", "add", "show", "leave", "end"):
        command = commands.add_parser(verb)
        _ = command.add_argument("owner")
        _ = command.add_argument("review")
        if verb == "next":
            _ = command.add_argument("--item", type=int, default=NextItem())
        elif verb == "record":
            _ = command.add_argument("item", type=int)
            _ = command.add_argument("decision")
            _ = command.add_argument("--actions", default="")
        elif verb == "add":
            _ = command.add_argument("text")
    return parser.parse_args(argv)


def run(store: ReviewStore, actor: str, args: argparse.Namespace) -> None:
    verb = cast(str, args.command)
    if verb == "start":
        source = cast(str, args.source).strip()
        items = item_list(cast(object, json.loads(cast(Path, args.items).read_text(encoding="utf-8"))))
        print(json.dumps(store.start(actor, source, items)))
        return
    if verb == "resume":
        print(json.dumps(store.join(actor, actor)))
        return
    if verb == "join":
        name = cast(str, args.staff_name)
        owner = sessions.staff_recipient(name, staff.snapshot())
        if isinstance(owner, sessions.StaffNameMissing):
            raise ReviewError(f"No staff member named {name!r}.")
        print(json.dumps(store.join(actor, owner.current_session)))
        return

    owner_id = cast(str, args.owner)
    with store.opened(actor, owner_id, cast(str, args.review)) as (path, review):
        if verb == "next":
            print(json.dumps(store.claim(path, review, actor, cast(int | NextItem, args.item))))
            return
        if verb in ("show", "end"):
            if verb == "end" and actor != owner_id:
                raise ReviewError("Only the owner can end the review. Use leave to release your claim.")
            print(decision_document(review), end="", flush=True)
            if verb == "end":
                _remove(path)
            return
        if verb == "record":
            index = str(cast(int, args.item))
            words = cast(str, args.decision).strip()
            if review["claims"].get(index) != actor or not words:
                raise ReviewError("Recording a decision requires your current claim and a nonempty decision.")
            review["decisions"][index] = Decision(words=words, actions=cast(str, args.actions).strip())
            del review["claims"][index]
        elif verb == "add":
            review["items"].extend(item_list([cast(str, args.text)]))
        else:
            review["claims"] = {index: holder for index, holder in review["claims"].items() if holder != actor}
        _save(path, review)
        print(json.dumps({"remaining": remaining(review)}))


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    read_registry = cache(sessions.read_registry)

    def observe(session: str) -> SessionLiveness:
        return sessions.session_liveness(session, read_registry())

    try:
        caller = session_id(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
        store = ReviewStore(Path.home() / ".local/state/adhoc_review/.shared", observe)
        run(store, caller, args)
    except (OSError, ValueError, KeyError, TypeError, staff.StaffRefused) as error:
        print(f"adhoc review: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
