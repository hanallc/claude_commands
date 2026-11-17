#!/usr/bin/env python3
"""Make an approved settings change live at once; the commit, push and Mac pull follow."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
import tempfile
from typing import cast

from scripts.lib import conf, mac_wake, main_publish, main_push_lock, models_registry
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.waiting_rows import git, replace_value, show


@dataclass(frozen=True)
class WaitingChange:
    """One approved change that is live and that main does not have yet."""

    stem: str
    key: str
    value: str
    message: str


def write_live(stem: str, key: str, value: str) -> str:
    """Write one value into the live file now; return the value it replaced."""
    source = conf.path(stem)
    index, old = conf.editable_line(stem, key)
    conf.check(stem, key, value)
    if old != value:
        write_text_atomic(source, replace_value(source.read_text(encoding="utf-8"), index, value))
    return old


def journal_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(state) / "live-settings" / "waiting.json"


@contextmanager
def _journal() -> Generator[list[WaitingChange]]:
    """Hold the journal lock; the caller changes the list in place."""
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            rows = cast(list[dict[str, str]], json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            rows = []
        changes = [WaitingChange(**row) for row in rows]
        before = list(changes)
        yield changes
        if changes != before:
            write_text_atomic(path, json.dumps([asdict(change) for change in changes], indent=2) + "\n")


def stage(change: WaitingChange) -> str:
    """Make the change live and record it for main; return the replaced value."""
    with _journal() as changes:
        old = write_live(change.stem, change.key, change.value)
        changes.append(change)
    return old


def _commit_row(checkout: Path, relative: str, change: WaitingChange) -> None:
    """Commit only this row onto HEAD; other waiting rows stay live and uncommitted."""
    head = show(checkout, "HEAD", relative)
    text = replace_value(head, conf.settings(head)[change.key].line, change.value)
    mode = git(checkout, "ls-tree", "HEAD", "--", relative).split()[0]
    blob = git(checkout, "hash-object", "-w", "--stdin", input_text=text).strip()
    parent = git(checkout, "rev-parse", "HEAD").strip()
    with tempfile.TemporaryDirectory(prefix="live-settings-index-") as directory:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        _ = git(checkout, "read-tree", parent, environment=environment)
        _ = git(checkout, "update-index", "--cacheinfo", f"{mode},{blob},{relative}", environment=environment)
        tree = git(checkout, "write-tree", environment=environment).strip()
    commit = git(checkout, "commit-tree", tree, "-p", parent, input_text=change.message).strip()
    _ = git(checkout, "update-ref", "-m", change.message.splitlines()[0], "HEAD", commit, parent)
    _ = git(checkout, "update-index", "--cacheinfo", f"{mode},{blob},{relative}")


def _publish_rows(checkout: Path, changes: list[WaitingChange], *, wait: bool) -> list[str] | None:
    """Commit the rows main lacks and push; None means that another process holds main."""
    lines: list[str] = []
    with ExitStack() as locks:
        try:
            locks.enter_context(main_push_lock.main_push_lock(checkout, wait=wait))
        except BlockingIOError:
            return None
        main_publish.ready(checkout, fetch_limit=main_publish.CommandTimeLimit(_seconds("fetch_seconds")))
        for change in changes:
            source = conf.path(change.stem)
            relative = str(source.resolve().relative_to(checkout.resolve()))
            live = conf.settings(source.read_text(encoding="utf-8")).get(change.key)
            head = conf.settings(show(checkout, "HEAD", relative)).get(change.key)
            if live is None or live.value != change.value:
                lines.append(f"main: {change.key}={change.value} has a newer live value; no commit")
            elif head is None or head.value != change.value:
                _commit_row(checkout, relative, change)
        pushed, detail = main_publish.push(checkout, time_limit=main_publish.CommandTimeLimit(_seconds("push_seconds")))
        if not pushed:
            raise main_publish.Refusal(f"main: {detail}")
        lines.append(f"main: {detail}")
    return lines


def publish(checkout: Path, *, wait: bool) -> list[str] | None:
    """Commit and push every waiting change, then send or queue the Mac pull.

    With wait=False, a busy main returns None at once; the next notes pass tries again.
    """
    with _journal() as journal:
        changes = list(journal)
    lines = _publish_rows(checkout, changes, wait=wait)
    if lines is None:
        return None
    with _journal() as journal:
        journal[:] = [change for change in journal if change not in changes]
    label = " ".join(f"{change.stem}.{change.key}" for change in changes) or "main"
    return [*lines, pull_mac(checkout, label)]


def publish_waiting() -> list[str]:
    """Publish waiting /tune changes and registry rows when main is free; never wait for main."""
    lines: list[str] = []
    try:
        with _journal() as journal:
            first = journal[0] if journal else None
        if first is not None:
            lines += publish(main_publish.checkout_of(conf.path(first.stem).resolve()), wait=False) or []
    except (OSError, main_publish.Refusal, conf.Unusable) as error:
        lines.append(f"settings not on main yet: {' '.join(str(error).split())}")
    try:
        if models_registry.publish_pending(models_registry.registry_path()):
            lines.append("registry rows published")
    except OSError as error:
        lines.append(f"registry rows not on main yet: {' '.join(str(error).split())}")
    return lines


def pull_mac(checkout: Path, label: str) -> str:
    outcome = main_publish.pull_mac(checkout, ssh_program=os.environ.get("ZONE_SSH") or "ssh",
                                    time_limit=main_publish.CommandTimeLimit(_seconds("mac_pull_seconds")))
    if isinstance(outcome, main_publish.MacPulled):
        main_publish.clear_pull_only()
        return "mac: pulled"
    main_publish.queue_pull_only(label, outcome.reason,
                                 pulls=(main_publish.MacCheckoutPull(PurePosixPath("~/.claude"), checkout),))
    _ = mac_wake.start()
    return f"mac: queued ({outcome.reason}); it goes out as soon as the Mac answers"


def write_mac(stem: str, key: str, value: str) -> str:
    """Write the value live on the Mac now; the Mac pull brings the commit later."""
    command = ('"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/live_settings.py" '
               + shlex.join([stem, key, value]))
    done = main_publish.ssh(command, ssh_program=os.environ.get("ZONE_SSH") or "ssh",
                            time_limit=main_publish.CommandTimeLimit(_seconds("mac_write_seconds")))
    if isinstance(done, str):
        return f"mac: not live yet ({done}); the Mac pull brings it"
    if done.returncode:
        return f"mac: not live yet ({main_publish.first_line(done)}); the Mac pull brings it"
    return f"mac: live now: {key}={value}"


def start_readers(stem: str) -> list[str]:
    """Run each notes part that reads this file now, in its own session; do not wait for it."""
    pairs = (pair.partition(":") for pair in conf.read_or_repository("live_settings").words("readers"))
    job = Path(__file__).resolve().parents[1] / "maintenance/agent_notes_job.py"
    lines: list[str] = []
    for part in (part for file, _, part in pairs if file == stem):
        _ = subprocess.Popen([str(Path(__file__).resolve().parent / "py"), str(job), "--now", part],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        lines.append(f"started now: the {part} pass")
    return lines


def _seconds(key: str) -> int:
    return conf.read_or_repository("live_settings").whole(key)


def main(arguments: list[str] | None = None) -> int:
    """Write one value live on this machine: `live_settings.py <stem> <key> <value>`."""
    argv = sys.argv[1:] if arguments is None else arguments
    if len(argv) != 3:
        print("usage: live_settings.py <stem> <key> <value>", file=sys.stderr)
        return 2
    try:
        _ = write_live(*argv)
    except (OSError, UnicodeError, conf.Unusable) as error:
        print(f"live_settings: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
