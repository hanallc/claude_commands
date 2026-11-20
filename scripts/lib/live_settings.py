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
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

from scripts.lib import conf, land_main, mac_wake, main_publish, main_push_lock, models_registry
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.main_push_lock import request_publication as request_publication
from scripts.production import land_queue, land_runs
from scripts.lib.waiting_rows import (
    ORDER_PATH, AcceptedSave, LockedJournal, WaitingChange as WaitingChange,
    append_setting, apply_accepted_values, apply_move, check_move, checkout_orders, git,
    journal_path as journal_path, live_text, locked_checkout, locked_journal, model_color_host, model_color_key, model_color_value,
    move_target, published_orders, read_journal, recover_abandoned_move, replace_value,
    restore_live, tree_text,
    tree_value,
)

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
class StagedSave:
    """The old value and the order recorded for the new value."""

    replaced_value: str
    order: int


@dataclass(frozen=True)
class UnchangedSave:
    """The requested value is already live."""


def editable_setting(stem: str, key: str, value: str, *, accepted: bool = False) -> ExistingSetting | NewModelColor:
    """Validate the change and identify the current setting."""
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


def _write_setting(stem: str, key: str, value: str, setting: ExistingSetting | NewModelColor) -> str:
    source = conf.path(stem)
    if isinstance(setting, NewModelColor):
        write_text_atomic(source, append_setting(source.read_text(encoding="utf-8"), key, value))
        return ""
    if setting.value != value:
        write_text_atomic(source, replace_value(source.read_text(encoding="utf-8"), setting.line, value))
    return setting.value


def stage_save(change: WaitingChange, *, describe: Callable[[str], str] | None = None) -> StagedSave | UnchangedSave:
    """Validate the requested value, then decide and issue its order under the lock."""
    stem, key, value = change.stem, change.key, change.value
    checkout = main_publish.checkout_of(conf.path(stem))
    recover_abandoned_move(checkout)
    head = git(checkout, "rev-parse", "HEAD").strip()
    orders, legacy = checkout_orders(checkout, head)
    with locked_journal(journal_path()) as pending:
        pending.settle(orders, legacy)
        path = conf.path(stem)
        source = live_text(path)
        if source is not None:
            restored = apply_accepted_values(source, pending.accepted.get(stem, {}))
            if restored != source:
                write_text_atomic(path, restored)
        setting = editable_setting(stem, key, value, accepted=False)
        if isinstance(setting, ExistingSetting) and setting.value == value:
            return UnchangedSave()
        old = setting.value if isinstance(setting, ExistingSetting) else "<missing>"
        message = change.message if describe is None else describe(old)
        order = pending.last_order + 1
        _ = pending.accept(stem, key, AcceptedSave(order, value))
        pending.changes.append(replace(change, message=message, save_order=order))
        pending.save()
        return StagedSave(_write_setting(stem, key, value, setting), order)


def _settings_commit(checkout: Path, parent: str, files: dict[str, str], message: str) -> str:
    """Create a commit without changing the live files or the checkout index."""
    with tempfile.TemporaryDirectory(prefix="live-settings-index-") as directory:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        _ = git(checkout, "read-tree", parent, environment=environment)
        for path, contents in files.items():
            entry = git(checkout, "ls-tree", parent, "--", path)
            mode = entry.split()[0] if entry else "100644"
            blob = git(checkout, "hash-object", "-w", "--stdin", input_text=contents).strip()
            _ = git(checkout, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", environment=environment)
        tree = git(checkout, "write-tree", environment=environment).strip()
    return git(checkout, "commit-tree", tree, "-p", parent, input_text=message).strip()


def _prepare_rows(checkout: Path, parent: str, pending: LockedJournal) -> tuple[str, list[str]]:
    """Prepare commits for the latest Saves without moving HEAD."""
    lines: list[str] = []
    orders = published_orders(checkout, parent)
    for change in pending.changes:
        committed = orders.get(change.stem, {}).get(change.key, 0)
        if not change.publish_on_main:
            continue
        if change.save_order < pending.order(change.stem, change.key):
            lines.append(f"main: {change.key}={change.value} has a newer live value; no commit")
        elif change.save_order > committed:
            relative = str(conf.path(change.stem).resolve().relative_to(checkout.resolve()))
            head = tree_text(checkout, parent, relative) or ""
            setting = conf.settings(head).get(change.key)
            text = (replace_value(head, setting.line, change.value) if setting
                    else append_setting(head, change.key, change.value))
            orders.setdefault(change.stem, {})[change.key] = change.save_order
            files = {relative: text, ORDER_PATH: json.dumps(orders, indent=2, sort_keys=True) + "\n"}
            parent = _settings_commit(checkout, parent, files, change.message)
    return parent, lines


class PublishedSettings(NamedTuple):
    checkout: Path
    label: str
    lines: list[str]

    def with_mac_pull(self) -> list[str]:
        return [*self.lines, pull_mac(self.checkout, self.label)]


def _publish_main(checkout: Path) -> PublishedSettings:
    """Move the waiting Saves to main and return the label for the Mac pull."""
    restore_live(checkout)
    main_publish.ready(checkout, fetch_limit=main_publish.CommandTimeLimit(_seconds("fetch_seconds")))
    pending = read_journal(journal_path())
    changes = pending.changes
    head = git(checkout, "rev-parse", "HEAD").strip()
    tip, lines = _prepare_rows(checkout, head, pending)
    if tip != head:
        _ = check_move(checkout, move_target(checkout, tip, fast_forward=True, expected_head=head))
    try:
        landed = land_main.land(checkout, tip, time_limit=land_main.CommandTimeLimit(_seconds("push_seconds")))
    except (OSError, land_main.LandError) as error:
        raise main_publish.Refusal(f"main: not pushed: {error}") from error
    detail = "pushed" if isinstance(landed.push, land_main.Pushed) else "origin has it"
    if isinstance(landed.hook, (land_main.HookDone, land_main.HookFailed)):
        detail += "; " + land_main.hook_report(landed.hook)
    if tip != head:
        apply_move(checkout, check_move(checkout, move_target(checkout, tip, fast_forward=True, expected_head=head)))
    names = [f"{change.stem}.{change.key}" for change in changes]
    label = " ".join(names) or "main"
    if names:
        main_publish.queue_pull_only(label, "main has the Save",
                                     pulls=(main_publish.MacCheckoutPull(main_publish.DEFAULT_MAC_CHECKOUT, checkout, tip),))
    lines.append(f"main: {detail}")
    with locked_journal(journal_path()) as pending:
        retained = [change for change in pending.changes if change not in changes]
        if retained != pending.changes:
            pending.changes = retained
            pending.save()
    return PublishedSettings(checkout, label, lines)


def publish(checkout: Path) -> list[str]:
    """Publish in a confirmed gap or start the detached waiter."""
    result = None
    with main_push_lock.settings_push(checkout) as gap, ExitStack() as locks:
        if isinstance(gap, land_queue.SettingsGap):
            try:
                locks.enter_context(locked_checkout(checkout, wait=False))
            except BlockingIOError:
                pass
            else:
                result = _publish_main(checkout)
    if result is not None:
        return result.with_mac_pull()
    return [request_publication()]


def _waiting_checkout() -> Path | None:
    pending = read_journal(journal_path())
    first = next((change for change in pending.changes if change.publish_on_main), None)
    if first is not None:
        return main_publish.checkout_of(conf.path(first.stem).resolve())
    return None


def publish_waiting() -> list[str]:
    """Start the waiter if a settings or registry change needs publication."""
    try:
        if _publication_pending():
            return [request_publication()]
    except OSError as error:
        return [f"settings not on main yet: {' '.join(str(error).split())}"]
    return []


def _publication_pending() -> bool:
    return waiting_rows_for_main() or models_registry.has_pending(models_registry.registry_path())


def waiting_rows_for_main() -> bool:
    pending = read_journal(journal_path())
    return any(change.publish_on_main for change in pending.changes)


def _publish_waiting(descriptor: int) -> int:
    """Wait for a gap in this process and publish the saved values."""
    try:
        if _publication_pending():
            while True:
                with land_queue.gap_lock() as gap:
                    if isinstance(gap, land_queue.SettingsGap) and land_runs.has_gap():
                        result = _publish_in_gap()
                        break
                time.sleep(0.25)
            if result is not None:
                _ = result.with_mac_pull()
    except (OSError, ValueError, main_publish.Refusal) as error:
        print(f"live_settings: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        os.close(descriptor)
    if _publication_pending():
        _ = request_publication()
    return 0


def _publish_in_gap() -> PublishedSettings | None:
    result = None
    try:
        if (checkout := _waiting_checkout()) is not None:
            with main_push_lock.main_push_lock(checkout), locked_checkout(checkout):
                result = _publish_main(checkout)
    finally:
        _ = models_registry.publish_pending(models_registry.registry_path())
    return result


def pull_mac(checkout: Path, label: str) -> str:
    pending = main_publish.read_queue()
    request = next((pull for pull in pending.pulls if pull.mac == main_publish.DEFAULT_MAC_CHECKOUT
                    and (pull.source == checkout or isinstance(pull.source, main_publish.MacPullSourceUnrecorded))),
                   None) if isinstance(pending, main_publish.PendingMacPull) else None
    outcome = main_publish.pull_mac(checkout, ssh_program=os.environ.get("ZONE_SSH") or "ssh",
                                    time_limit=main_publish.CommandTimeLimit(_seconds("mac_pull_seconds")))
    if isinstance(outcome, main_publish.MacPulled):
        if request is not None:
            main_publish.clear_pull_only(confirmed=request)
        return "mac: pulled"
    main_publish.queue_pull_only(label, outcome.reason,
                                 pulls=(main_publish.MacCheckoutPull(main_publish.DEFAULT_MAC_CHECKOUT, checkout),))
    _ = mac_wake.start()
    return f"mac: queued ({outcome.reason}); it goes out as soon as the Mac answers"


class MacWriteSuperseded(StrEnum):
    NEWER_SAVE = "mac: a newer Save is already live"


def write_mac(stem: str, key: str, value: str, *, order: int) -> str | MacWriteSuperseded:
    """Write the value live on the Mac now; the Mac pull brings the commit later."""
    arguments = [stem, key, value]
    if conf.full_model_color_key(stem, key):
        arguments.insert(0, "--accepted-model-color")
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
    try:
        if len(argv) == 2 and argv[0] == "publish-waiting":
            return _publish_waiting(int(argv[1]))
    except (OSError, ValueError, main_publish.Refusal) as error:
        print(f"live_settings: {error}", file=sys.stderr)
        return 1
    accepted = argv[:1] == ["--accepted-model-color"]
    if accepted:
        argv = argv[1:]
    if len(argv) != 3:
        print("usage: live_settings.py <stem> <key> <value>", file=sys.stderr)
        return 2
    try:
        stem, key, value = argv
        order = int(os.environ.get(SAVE_ORDER_ENV, "0"))
        if order < 0 or (order == 0 and platform.system() == "Darwin"):
            raise ValueError("the Mac needs a positive Save order from natedev")
        checkout = main_publish.checkout_of(conf.path(stem))
        recover_abandoned_move(checkout)
        head = git(checkout, "rev-parse", "HEAD").strip()
        orders, legacy = checkout_orders(checkout, head)
        base = tree_value(checkout, head, stem, key) if not order else None
        with locked_journal(journal_path()) as pending:
            pending.settle(orders, legacy)
            current = pending.order(stem, key)
            if order < current:
                print(MacWriteSuperseded.NEWER_SAVE)
            elif not current or order > current:
                setting = editable_setting(stem, key, value, accepted=accepted)
                if not order and isinstance(setting, ExistingSetting) and setting.value == value:
                    return 0
                _ = pending.accept(stem, key, AcceptedSave(order, value, base))
                pending.save()
                _ = _write_setting(stem, key, value, setting)
            elif isinstance(save := pending.accepted.get(stem, {}).get(key), AcceptedSave):
                _ = write_live(stem, key, save.value, accepted=accepted)
    except (OSError, ValueError, main_publish.Refusal) as error:
        print(f"live_settings: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
