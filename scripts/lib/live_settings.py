#!/usr/bin/env python3
"""Make an approved settings change live at once; the commit, push and Mac pull follow."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from enum import StrEnum
import json
import os
import platform
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
import tempfile

from scripts.lib import conf, mac_wake, main_publish, main_push_lock, models_registry
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.waiting_rows import (
    NAMED_ROWS, ORDER_PATH, AcceptedSave, LockedJournal, SaveOrders, WaitingChange as WaitingChange,
    append_setting, apply_move, check_move, git,
    journal_path as journal_path, locked_journal, model_color_host, model_color_key, model_color_value, move_target,
    named_row, published_orders, replace_value, show, tree_value,
)
from scripts.production import producer_map_rows

SAVE_ORDER_ENV = "LIVE_SETTINGS_SAVE_ORDER"


@dataclass(frozen=True)
class ExistingSetting:
    """The line and current value of an editable setting."""

    line: int
    value: str


@dataclass(frozen=True)
class NewModelColor:
    """The model color has no line yet and can be added."""


@dataclass(frozen=True)
class NamedRow:
    """A producer_map row: its current value, None when new, and the name comment at the end of its line."""

    value: str | None
    comment: str


@dataclass(frozen=True)
class StagedSave:
    """The old value and the order recorded for the new value."""

    replaced_value: str
    order: int


@dataclass(frozen=True)
class UnchangedSave:
    """The requested value is already live under the journal lock."""


def editable_setting(stem: str, key: str, value: str, *, accepted: bool = False,
                     comment: str | None = None) -> ExistingSetting | NewModelColor | NamedRow:
    """Validate the change and identify the current setting; only the Mac takes natedev's name comment unchecked."""
    if stem == NAMED_ROWS:
        current = conf.settings(conf.path(stem).read_text(encoding="utf-8")).get(key)
        trusted = comment if comment is not None and platform.system() == "Darwin" else None
        return NamedRow(current.value if current else None,
                        trusted if trusted is not None else producer_map_rows.checked_comment(key, value))
    if stem == "statusline" and key.startswith("model."):
        if host := model_color_host(key):
            raise conf.Unusable(f"{stem}: {key}: .{host} is a host override, not a model color")
        settings = conf.settings(conf.path(stem).read_text(encoding="utf-8"))
        if (key not in settings and conf.host_override(stem, settings, key) is None
                and conf.full_model_color_key(stem, key) and (accepted or model_color_key(stem, key))):
            if not model_color_value(value):
                raise conf.Unusable(f"{stem}: {key}: use a color in #rrggbb format")
            return NewModelColor()
    index, old = conf.editable_line(stem, key)
    conf.check(stem, key, value)
    return ExistingSetting(index, old)


def write_live(stem: str, key: str, value: str, *, accepted: bool = False) -> str:
    """Write one value now and return the value it replaced, if any."""
    setting = editable_setting(stem, key, value, accepted=accepted)
    return _write_setting(stem, key, value, setting)


def _write_setting(stem: str, key: str, value: str, setting: ExistingSetting | NewModelColor | NamedRow) -> str:
    source = conf.path(stem)
    if isinstance(setting, NewModelColor):
        write_text_atomic(source, append_setting(source.read_text(encoding="utf-8"), key, value))
        return ""
    if isinstance(setting, NamedRow):
        write_text_atomic(source, named_row(source.read_text(encoding="utf-8"), key, value, setting.comment))
        return setting.value or ""
    if setting.value != value:
        write_text_atomic(source, replace_value(source.read_text(encoding="utf-8"), setting.line, value))
    return setting.value


def stage_save(change: WaitingChange, *, describe: Callable[[str], str] | None = None) -> StagedSave | UnchangedSave:
    """Validate the requested value, then decide and issue its order under the lock."""
    stem, key, value = change.stem, change.key, change.value
    checkout = main_publish.checkout_of(conf.path(stem))
    with locked_journal(journal_path()) as pending:
        pending.restore(checkout, "HEAD")
        setting = editable_setting(stem, key, value, accepted=False)
        current = None if isinstance(setting, NewModelColor) else setting.value
        if current == value:
            return UnchangedSave()
        old = "<missing>" if current is None else current
        order = pending.last_order + 1
        _ = pending.accept(stem, key, AcceptedSave(order, value))
        message = change.message if describe is None else describe(old)
        pending.changes.append(replace(change, message=message, save_order=order))
        pending.save()
        return StagedSave(_write_setting(stem, key, value, setting), order)


def _commit_row(checkout: Path, relative: str, change: WaitingChange, orders: SaveOrders, pending: LockedJournal) -> None:
    """Commit only this row onto HEAD; other waiting rows stay live and uncommitted."""
    entry = git(checkout, "ls-tree", "HEAD", "--", relative)
    head = show(checkout, "HEAD", relative) if entry else ""
    setting = conf.settings(head).get(change.key)
    if change.stem == NAMED_ROWS:
        text = named_row(head, change.key, change.value, producer_map_rows.name_comment(change.key, change.value))
    else:
        text = (replace_value(head, setting.line, change.value) if setting
                else append_setting(head, change.key, change.value))
    mode = entry.split()[0] if entry else "100644"
    orders.setdefault(change.stem, {})[change.key] = change.save_order
    parent = git(checkout, "rev-parse", "HEAD").strip()
    with tempfile.TemporaryDirectory(prefix="live-settings-index-") as directory:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        _ = git(checkout, "read-tree", parent, environment=environment)
        for path, permissions, contents in (
                (relative, mode, text), (ORDER_PATH, "100644", json.dumps(orders, indent=2, sort_keys=True) + "\n")):
            blob = git(checkout, "hash-object", "-w", "--stdin", input_text=contents).strip()
            _ = git(checkout, "update-index", "--add", "--cacheinfo", f"{permissions},{blob},{path}", environment=environment)
        tree = git(checkout, "write-tree", environment=environment).strip()
    commit = git(checkout, "commit-tree", tree, "-p", parent, input_text=change.message).strip()
    move = move_target(checkout, commit, pending, fast_forward=True)
    apply_move(checkout, check_move(checkout, move, pending), pending)


def _publish_rows(checkout: Path, pending: LockedJournal, changes: list[WaitingChange]) -> list[str]:
    """Commit each Save whose order is still current and has not reached HEAD."""
    lines: list[str] = []
    orders = published_orders(checkout, "HEAD")
    for change in changes:
        committed = orders.get(change.stem, {}).get(change.key, 0)
        if change.save_order < pending.order(change.stem, change.key):
            lines.append(f"main: {change.key}={change.value} has a newer live value; no commit")
        elif change.save_order > committed:
            relative = str(conf.path(change.stem).resolve().relative_to(checkout.resolve()))
            _commit_row(checkout, relative, change, orders, pending)
    return lines


def publish(checkout: Path, *, wait: bool) -> list[str] | None:
    """Publish waiting changes and request a Mac pull. Return None if the lock is busy and wait is False."""
    with ExitStack() as locks:
        try:
            locks.enter_context(main_push_lock.main_push_lock(checkout, wait=wait))
        except BlockingIOError:
            return None
        main_publish.ready(checkout, fetch_limit=main_publish.CommandTimeLimit(_seconds("fetch_seconds")))
        with locked_journal(journal_path()) as pending:
            pending.restore(checkout, "HEAD")
            changes = list(pending.changes)
            lines = _publish_rows(checkout, pending, changes)
        pushed, detail = main_publish.push(checkout, time_limit=main_publish.CommandTimeLimit(_seconds("push_seconds")))
        if not pushed:
            raise main_publish.Refusal(f"main: {detail}")
        lines.append(f"main: {detail}")
        with locked_journal(journal_path()) as pending:
            retained = [change for change in pending.changes if change not in changes]
            if retained != pending.changes:
                pending.changes = retained
                pending.save()
    label = " ".join(f"{change.stem}.{change.key}" for change in changes) or "main"
    return [*lines, pull_mac(checkout, label)]


def publish_waiting() -> list[str]:
    """Publish waiting /tune changes and registry rows when main is free; never wait for main."""
    lines: list[str] = []
    try:
        with locked_journal(journal_path()) as pending:
            first = next((change for change in pending.changes if change.publish_on_main), None)
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


class MacWriteSuperseded(StrEnum):
    NEWER_SAVE = "mac: a newer Save is already live"


def write_mac(stem: str, key: str, value: str, *, order: int) -> str | MacWriteSuperseded:
    """Write the value live on the Mac now; the Mac pull brings the commit later."""
    arguments = [stem, key, value]
    if conf.full_model_color_key(stem, key):
        arguments.insert(0, "--accepted-model-color")
    if stem == NAMED_ROWS:
        arguments[:0] = ["--name-comment", producer_map_rows.name_comment(key, value)]
    command = (f'{SAVE_ORDER_ENV}={order} "$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/live_settings.py" '
               + shlex.join(arguments))
    done = main_publish.ssh(command, ssh_program=os.environ.get("ZONE_SSH") or "ssh",
                            time_limit=main_publish.CommandTimeLimit(_seconds("mac_write_seconds")))
    if isinstance(done, str) or done.returncode:
        reason = done if isinstance(done, str) else main_publish.first_line(done)
        return f"mac: not live yet ({reason}); the Mac pull brings it"
    if done.stdout.strip() == MacWriteSuperseded.NEWER_SAVE:
        return MacWriteSuperseded.NEWER_SAVE
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
    argv = sys.argv[1:] if arguments is None else arguments
    accepted = argv[:1] == ["--accepted-model-color"]
    if accepted:
        argv = argv[1:]
    comment = argv[1] if argv[:1] == ["--name-comment"] and len(argv) > 1 else None
    if comment is not None:
        argv = argv[2:]
    if len(argv) != 3:
        print("usage: live_settings.py [--accepted-model-color | --name-comment <comment>] <stem> <key> <value>",
              file=sys.stderr)
        return 2
    try:
        stem, key, value = argv
        order = int(os.environ.get(SAVE_ORDER_ENV, "0"))
        if order < 0 or (order == 0 and platform.system() == "Darwin"):
            raise ValueError("the Mac needs a positive Save order from natedev")
        checkout = main_publish.checkout_of(conf.path(stem))
        with locked_journal(journal_path()) as pending:
            pending.restore(checkout, "HEAD")
            current = pending.order(stem, key)
            if order < current:
                print(MacWriteSuperseded.NEWER_SAVE)
            elif not current or order > current:
                setting = editable_setting(stem, key, value, accepted=accepted, comment=comment)
                if not order and not isinstance(setting, NewModelColor) and setting.value == value:
                    return 0
                base = tree_value(checkout, "HEAD", stem, key) if not order else None
                _ = pending.accept(stem, key, AcceptedSave(order, value, base))
                pending.save()
                _ = _write_setting(stem, key, value, setting)
    except (OSError, ValueError, main_publish.Refusal) as error:
        print(f"live_settings: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
