"""Keep one ledger record for each Go that nate presses on the List page."""
from __future__ import annotations

import errno
import ipaddress
import json
import re
import secrets
import socket
import threading
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict, cast

from scripts.lib import atomic_file, conf, file_lock
from scripts.production import staff, the_list
from scripts.review_links import change_page

GO_PATH = f"/{change_page.CONSOLE_FOLDER}/list-go"
GO_CHECK_PATH = f"/{change_page.CONSOLE_FOLDER}/go-check"
LEDGER_FOLDER = "go-ledger"
CHECK_COMMAND = "~/.claude/scripts/lib/py ~/.claude/scripts/review_links/go_check.py"
GO_ID = re.compile(r"[0-9a-f]{32}")
# The file lock orders the server processes. A file lock is not reentrant across opens, so the thread lock orders
# the threads of one process and lets a holder take the ledger lock again.
_thread_lock = threading.RLock()
_holders: list[Path] = []


class GoRecord(TypedDict):
    go_id: str
    ask: str
    item: str
    target: str
    what: str
    time: str
    client: str
    page: str
    used: bool
    sent: dict[str, bool]
    reported: dict[str, bool]


class GoCheckAnswer(TypedDict):
    ok: bool
    text: str


def token_prefix(ask_id: str) -> str:
    """Sign the Go form of one ask apart from the other List forms."""
    return "go:" + ask_id


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | str:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def host_owns(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Find an address that this host owns: a bind of a socket to an owned address works.

    Only EADDRNOTAVAIL means that the host does not own the address. Each other error counts as owned, so the Go is refused.
    """
    family = socket.AF_INET6 if isinstance(address, ipaddress.IPv6Address) else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((str(address), 0))
    except OSError as error:
        return error.errno != errno.EADDRNOTAVAIL
    return True


def natedev_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Find natedev's own address: each loopback address and each address that this host owns."""
    return address.is_loopback or host_owns(address)


def refused_address(client: str) -> bool:
    """Refuse a Go from natedev or from an extra address in the conf; read the conf at each click."""
    address = _address(client)
    if isinstance(address, str) or natedev_address(address):
        return True
    extra = conf.read("review_links").words("go_refused_addresses")
    return address in {_address(text) for text in extra}


def _path(root: Path, go_id: str) -> Path:
    if GO_ID.fullmatch(go_id) is None:
        raise ValueError("unknown go id")
    return root / LEDGER_FOLDER / f"{go_id}.json"


def _read(path: Path) -> GoRecord:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    states = [fields.get(name) for name in ("sent", "reported")]
    if (fields.keys() != GoRecord.__annotations__.keys() or not isinstance(fields["used"], bool)
            or not all(isinstance(text, str) for name, text in fields.items() if name not in ("used", "sent", "reported"))
            or not all(isinstance(state, dict) and all(isinstance(name, str) and isinstance(done, bool)
                                                       for name, done in cast(dict[object, object], state).items())
                       for state in states)):
        raise ValueError(f"invalid go ledger record: {path.name}")
    return cast(GoRecord, cast(object, fields))


@contextmanager
def locked(root: Path) -> Generator[None]:
    """Hold the ledger lock of `root` for each process and thread; a holder can take it again."""
    with _thread_lock:
        if _holders:
            yield
            return
        folder = root / LEDGER_FOLDER
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with file_lock.exclusive(folder / ".lock"):
            _holders.append(root)
            try:
                yield
            finally:
                _ = _holders.pop()


def read(root: Path, go_id: str) -> GoRecord:
    """Read one record; each write replaces the whole file, so a read needs no lock."""
    return _read(_path(root, go_id))


def closed_ask(entry: GoRecord) -> tuple[staff.StaffId, the_list.ListItem, the_list.Ask] | None:
    """Find the closed List ask that holds this go id with the same target and What."""
    keeper, _, identity = entry["item"].partition("/")
    try:
        item = the_list.load(staff.StaffId(keeper), identity)
    except (OSError, ValueError):
        return None
    ask = next((ask for ask in item.asks if ask.go_id == entry["go_id"] and not ask.is_open), None)
    if ask is None or (ask.ask_id, ask.target, ask.what) != (entry["ask"], entry["target"], entry["what"]):
        return None
    return staff.StaffId(keeper), item, ask


def record(root: Path, *, ask: the_list.Ask, item: str, recipients: Iterable[staff.StaffId], client: str, page: str,
           moment: datetime) -> GoRecord:
    """Write the unused, unsent record before the List saves the Go."""
    entry: GoRecord = {"go_id": secrets.token_hex(16), "ask": ask.ask_id, "item": item, "target": ask.target,
                       "what": ask.what, "time": moment.isoformat(), "client": client, "page": page, "used": False,
                       "sent": dict.fromkeys(recipients, False), "reported": dict.fromkeys(recipients, False)}
    path = _path(root, entry["go_id"])
    with locked(root):
        if not atomic_file.create_text_atomic(path, json.dumps(entry, ensure_ascii=False)):
            raise OSError("the go id already exists")
    return entry


def remove(root: Path, go_id: str) -> None:
    """Remove a record whose ask did not close."""
    with locked(root):
        _path(root, go_id).unlink(missing_ok=True)


def mark(root: Path, go_id: str, state: Literal["sent", "reported"], recipients: Iterable[staff.StaffId]) -> None:
    """Keep the delivery state of each recipient: sent, or reported to nate as not delivered."""
    with locked(root):
        path = _path(root, go_id)
        entry = _read(path)
        entry[state].update(dict.fromkeys(recipients, True))
        atomic_file.write_text_atomic(path, json.dumps(entry, ensure_ascii=False))


def unsent(root: Path) -> tuple[str, ...]:
    """Find each Go whose message did not reach every recipient."""
    found: list[str] = []
    for path in sorted((root / LEDGER_FOLDER).glob("*.json")):
        try:
            entry = read(root, path.stem)
        except (OSError, ValueError):
            continue
        if not all(entry["sent"].values()):
            found.append(entry["go_id"])
    return tuple(found)


def check(root: Path, go_id: str, caller: str) -> GoCheckAnswer:
    """Accept one use by the target session and mark the record used."""
    with locked(root):
        try:
            path = _path(root, go_id)
            entry = _read(path)
        except (FileNotFoundError, ValueError):
            return {"ok": False, "text": "no Go has this id."}
        if closed_ask(entry) is None:
            return {"ok": False, "text": "the List has no closed ask for this Go."}
        if entry["target"] != caller:
            return {"ok": False, "text": "this Go is for another session."}
        if entry["used"]:
            return {"ok": False, "text": "this Go was already used."}
        entry["used"] = True
        try:
            atomic_file.write_text_atomic(path, json.dumps(entry, ensure_ascii=False))
        except atomic_file.AtomicDurabilityUnconfirmed:
            pass  # atomic_file raises this only after os.replace, so the used mark is on disk.
        return {"ok": True, "text": entry["what"]}


def message(item: the_list.ListItem, ask: the_list.Ask, go_id: str, agent: str) -> str:
    """Name what nate approved; carry only the go id and the check command."""
    button = "Go" if ask.kind is the_list.AskKind.GO else "Approved"
    return (f"List page: nate pressed {button} for item {item.number}.\n"
            + f"What: {ask.what}\n"
            + f"For: {agent}. Only {agent} can use it, once.\n"
            + f"Go id: {go_id}\n"
            + f"Run this check before you act: {CHECK_COMMAND} {go_id}")
