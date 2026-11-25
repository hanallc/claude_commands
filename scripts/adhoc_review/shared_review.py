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


class ReviewProcess(TypedDict):
    pid: int
    started: str


class ActiveReview(TypedDict):
    id: str
    started: str
    source: str
    items: list[str]
    order: list[str]
    claims: dict[str, str]
    decisions: dict[str, Decision]
    participants: dict[str, ReviewProcess]


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


class AppendItems:
    """Place new items at the end of the list."""


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
    for index in review["order"]:
        if (decision := review["decisions"].get(index)) is not None:
            item = review["items"][int(index) - 1]
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
    review = cast(ActiveReview, json.loads(first[len(RECORD_PREFIX):-len(RECORD_SUFFIX)]))
    _ = review.setdefault("order", [str(index) for index in range(1, len(review["items"]) + 1)])
    _ = review.setdefault("participants", {})
    return review


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
            exists = path.exists()
            if exists:
                review = _read(path)
                if (review["source"], review["items"]) != (source, items):
                    raise ReviewError("This session already has an open review. Resume it or end it first.")
            else:
                review = ActiveReview(id=uuid4().hex, started=date.today().isoformat(), source=source,
                                      items=items, order=[str(index) for index in range(1, len(items) + 1)],
                                      claims={}, decisions={}, participants={})
            if self.remember_process(review, owner) or not exists:
                _save(path, review)
            self.prune_claims(path, review, owner)
            return self.handle(owner, path, review)

    def handle(self, owner: str, path: Path, review: ActiveReview) -> ReviewHandle:
        return ReviewHandle(owner=owner, review=review["id"], document=str(path), remaining=remaining(review))

    def session_state(self, review: ActiveReview, identity: str) -> SessionLiveness:
        observed = self.liveness(identity)
        if isinstance(observed, sessions.LiveSessionProcess):
            return observed
        saved = review["participants"].get(identity)
        if saved is None:
            return (observed if isinstance(observed, sessions.SessionLivenessUnknown)
                    else sessions.SessionLivenessUnknown("No saved process identity."))
        # The registry can lose a record before the process exits.
        process = sessions.process_liveness(saved["pid"], saved["started"])
        if isinstance(process, sessions.LiveSessionProcess) or isinstance(observed, sessions.SessionNotLive):
            return process
        return observed

    def remember_process(self, review: ActiveReview, identity: str) -> bool:
        observed = self.session_state(review, identity)
        if not isinstance(observed, sessions.LiveSessionProcess) or not (started := sessions.process_start(observed.pid)):
            raise ReviewError("Cannot confirm your process. Try again when your session is live.")
        process = ReviewProcess(pid=observed.pid, started=started)
        if review["participants"].get(identity) == process:
            return False
        review["participants"][identity] = process
        return True

    def prune_claims(self, path: Path, review: ActiveReview, actor: str) -> None:
        ended = [index for index, holder in review["claims"].items()
                 if holder != actor and isinstance(self.session_state(review, holder), sessions.SessionNotLive)]
        if ended:
            for index in ended:
                del review["claims"][index]
            _save(path, review)

    def check_owner(self, actor: str, owner: str, path: Path, review: ActiveReview) -> None:
        if actor == owner:
            return
        observed = self.session_state(review, owner)
        if isinstance(observed, sessions.SessionNotLive):
            _remove(path)
            raise ReviewError("The review owner has ended their session.")
        if isinstance(observed, sessions.SessionLivenessUnknown):
            raise ReviewError(f"Cannot confirm the review owner's session: {observed.reason}")

    def join(self, actor: str, owner: str) -> ReviewHandle:
        with self.locked_path(owner) as path:
            review = _read(path)
            self.check_owner(actor, owner, path, review)
            if self.remember_process(review, actor):
                _save(path, review)
            self.prune_claims(path, review, actor)
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
            self.check_owner(actor, owner, path, review)
            if actor != owner and actor not in review["participants"]:
                raise ReviewError("Join this review before you use its items.")
            self.prune_claims(path, review, actor)
            yield path, review

    def claim(self, actor: str, owner: str, review_id: str, requested: int | NextItem) -> ClaimedItem | NoAvailableItem:
        with self.opened(actor, owner, review_id) as (path, review):
            return self._claim(path, review, actor, requested)

    def _claim(self, path: Path, review: ActiveReview, actor: str, requested: int | NextItem) -> ClaimedItem | NoAvailableItem:
        claims = review["claims"]
        current = next((index for index, holder in claims.items() if holder == actor), "")
        if isinstance(requested, int):
            if not 1 <= requested <= len(review["items"]):
                raise ReviewError(f"No item {requested}.")
            index = str(requested)
            if index in claims and claims[index] != actor:
                raise ReviewError(f"Another session holds item {requested}.")
        else:
            index = current or next((number for number in review["order"]
                                     if number not in claims and number not in review["decisions"]), "")
        if not index:
            count = remaining(review)
            return NoAvailableItem(state="busy" if count else "complete", remaining=count)
        _ = self.remember_process(review, actor)
        if current and current != index:
            del claims[current]
        claims[index] = actor
        _save(path, review)
        return ClaimedItem(state="claimed", item=int(index), text=review["items"][int(index) - 1],
                           remaining=remaining(review))


def add_items(review: ActiveReview, items: list[str], after: int | AppendItems) -> None:
    order = review["order"]
    if isinstance(after, int) and str(after) not in order:
        raise ReviewError(f"No item {after}.")
    position = order.index(str(after)) + 1 if isinstance(after, int) else len(order)
    first = len(review["items"]) + 1
    order[position:position] = [str(index) for index in range(first, first + len(items))]
    review["items"].extend(items)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    _ = start.add_argument("--source", required=True)
    _ = start.add_argument("--items", required=True, type=Path, help="Read a JSON array from a file or from stdin (-).")
    _ = commands.add_parser("join").add_argument("staff_name")
    _ = commands.add_parser("resume")
    for verb in ("claim", "decide", "next", "record", "split", "add", "show", "leave", "end"):
        command = commands.add_parser(verb)
        _ = command.add_argument("owner")
        _ = command.add_argument("review")
        if verb in ("claim", "next"):
            _ = command.add_argument("--item", type=int, default=NextItem())
        elif verb in ("decide", "record"):
            _ = command.add_argument("item", type=int)
            _ = command.add_argument("decision")
            _ = command.add_argument("--actions", default="")
        elif verb == "add":
            _ = command.add_argument("text")
            _ = command.add_argument("--after", type=int, default=AppendItems())
        elif verb == "split":
            _ = command.add_argument("item", type=int)
            _ = command.add_argument("subitems", nargs="+")
    return parser.parse_args(argv)


def run(store: ReviewStore, actor: str, args: argparse.Namespace) -> None:
    verb = cast(str, args.command)
    if verb == "start":
        source = cast(str, args.source).strip()
        input_path = cast(Path, args.items)
        text = sys.stdin.read() if input_path == Path("-") else input_path.read_text(encoding="utf-8")
        items = item_list(cast(object, json.loads(text)))
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
    review_id = cast(str, args.review)
    if verb in ("claim", "decide") and actor != owner_id:
        raise ReviewError("Use next and record after you join a review.")
    if verb in ("next", "record", "leave") and actor == owner_id:
        raise ReviewError("The owner uses claim, decide, and end.")
    if verb in ("claim", "next"):
        print(json.dumps(store.claim(actor, owner_id, review_id, cast(int | NextItem, args.item))))
        return

    with store.opened(actor, owner_id, review_id) as (path, review):
        if verb in ("show", "end"):
            if verb == "end" and actor != owner_id:
                raise ReviewError("Only the owner can end the review. Use leave to release your claim.")
            print(decision_document(review), end="", flush=True)
            if verb == "end":
                _remove(path)
            return
        if verb in ("decide", "record"):
            index = str(cast(int, args.item))
            words = cast(str, args.decision).strip()
            if review["claims"].get(index) != actor or not words:
                raise ReviewError("Recording a decision requires your current claim and a nonempty decision.")
            review["decisions"][index] = Decision(words=words, actions=cast(str, args.actions).strip())
            del review["claims"][index]
        elif verb == "add":
            add_items(review, item_list([cast(str, args.text)]), cast(int | AppendItems, args.after))
        elif verb == "split":
            item = cast(int, args.item)
            parts = item_list(cast(object, args.subitems))
            if review["claims"].get(str(item)) != actor or len(parts) < 2:
                raise ReviewError("Split your claimed item into at least two subitems.")
            if str(item) in review["decisions"]:
                raise ReviewError("A decided item cannot be split. Add the new items after it.")
            review["items"][item - 1] = parts[0]
            add_items(review, parts[1:], item)
        else:
            review["claims"] = {index: holder for index, holder in review["claims"].items() if holder != actor}
            del review["participants"][actor]
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
