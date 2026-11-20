#!/usr/bin/env python3
"""Make an approved settings change live at once; the commit, push and Mac pull follow."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
import argparse
import json
import os
import platform
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
import tempfile
from typing import NamedTuple, cast

from scripts.lib import conf, land_main, mac_wake, main_publish, main_push_lock, models_registry
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import fast_track, land_queue
from scripts.lib.waiting_rows import (
    ORDER_PATH, SETTINGS_JSON, AcceptedSave, LockedJournal, MacSettingsBaseline, MacSettingsUntracked, NoSettingsJsonSave, SaveOrders, SettingsJsonEdits, SettingsJsonSave,
    WaitingChange as WaitingChange, apply_settings_json_edits, clean_settings_json, settings_json_edits, smudge_settings_json,
    append_setting, apply_move, check_move, git, has_settings_json_edits,
    journal_path as journal_path, locked_journal, model_color_host, model_color_key, model_color_value, move_target,
    published_orders, read_journal, replace_value, tree_text, tree_value, waiting_paths,
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
    """The requested value is already live under the journal lock."""


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
    with locked_journal(journal_path()) as pending:
        pending.restore(checkout, "HEAD")
        setting = editable_setting(stem, key, value, accepted=False)
        if isinstance(setting, ExistingSetting) and setting.value == value:
            return UnchangedSave()
        old = setting.value if isinstance(setting, ExistingSetting) else "<missing>"
        order = pending.last_order + 1
        _ = pending.accept(stem, key, AcceptedSave(order, value))
        message = change.message if describe is None else describe(old)
        pending.changes.append(replace(change, message=message, save_order=order))
        pending.save()
        return StagedSave(_write_setting(stem, key, value, setting), order)


def _commit_settings(checkout: Path, files: dict[str, str], message: str, pending: LockedJournal) -> None:
    """Commit only these files while the other live values stay in place."""
    parent = git(checkout, "rev-parse", "HEAD").strip()
    with tempfile.TemporaryDirectory(prefix="live-settings-index-") as directory:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        _ = git(checkout, "read-tree", parent, environment=environment)
        for path, contents in files.items():
            entry = git(checkout, "ls-tree", parent, "--", path)
            mode = entry.split()[0] if entry else "100644"
            blob = git(checkout, "hash-object", "-w", "--stdin", input_text=contents).strip()
            _ = git(checkout, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", environment=environment)
        tree = git(checkout, "write-tree", environment=environment).strip()
    commit = git(checkout, "commit-tree", tree, "-p", parent, input_text=message).strip()
    move = move_target(checkout, commit, pending, fast_forward=True)
    apply_move(checkout, check_move(checkout, move, pending), pending)


def _commit_row(checkout: Path, relative: str, change: WaitingChange, orders: SaveOrders, pending: LockedJournal) -> None:
    """Commit one waiting configuration row onto HEAD."""
    head = tree_text(checkout, "HEAD", relative) or ""
    setting = conf.settings(head).get(change.key)
    text = (replace_value(head, setting.line, change.value) if setting
            else append_setting(head, change.key, change.value))
    orders.setdefault(change.stem, {})[change.key] = change.save_order
    _commit_settings(checkout, {relative: text, ORDER_PATH: json.dumps(orders, indent=2, sort_keys=True) + "\n"},
                     change.message, pending)


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


class PublishedSettings(NamedTuple):
    checkout: Path
    label: str
    lines: list[str]

    def with_mac_pull(self) -> list[str]:
        return [*self.lines, pull_mac(self.checkout, self.label)]


def _publish_main(checkout: Path) -> PublishedSettings:
    """Move the waiting Saves to main and return the label for the Mac pull."""
    main_publish.ready(checkout, fetch_limit=main_publish.CommandTimeLimit(_seconds("fetch_seconds")))
    with locked_journal(journal_path()) as pending:
        pending.restore(checkout, "HEAD")
        changes = list(pending.changes)
        lines = _publish_rows(checkout, pending, changes)
        settings_json = pending.settings_json
        saved_through = pending.last_order
        if not isinstance(settings_json, NoSettingsJsonSave):
            head = tree_text(checkout, "HEAD", SETTINGS_JSON) or "{}\n"
            settings = apply_settings_json_edits(head, settings_json["edits"])
            if has_settings_json_edits(settings_json_edits(head, settings)):
                _commit_settings(checkout, {SETTINGS_JSON: settings}, settings_json["message"], pending)
    pushed, detail = main_publish.push(checkout, time_limit=main_publish.CommandTimeLimit(_seconds("push_seconds")))
    if not pushed:
        raise main_publish.Refusal(f"main: {detail}")
    lines.append(f"main: {detail}")
    _record_journal_publication(checkout, saved_through, detail)
    with locked_journal(journal_path()) as pending:
        retained = [change for change in pending.changes if change not in changes]
        finished_json = not isinstance(settings_json, NoSettingsJsonSave) and settings_json == pending.settings_json
        if finished_json:
            pending.settings_json = NoSettingsJsonSave()
        if retained != pending.changes or finished_json:
            pending.changes = retained
            pending.save()
    names = [f"{change.stem}.{change.key}" for change in changes]
    if not isinstance(settings_json, NoSettingsJsonSave):
        names.append(SETTINGS_JSON)
    label = " ".join(names) or "main"
    return PublishedSettings(checkout, label, lines)


def _record_journal_publication(checkout: Path, saved_through: int, detail: str) -> None:
    selected = [request for request in land_queue.deferred_pushes()
                if request["kind"] == "direct" and Path(request["checkout"]) == checkout
                and "journal_order" in request and request["journal_order"] <= saved_through]
    if not selected:
        return
    tip = git(checkout, "rev-parse", "HEAD").strip()
    receipt: land_queue.PublicationReceipt = {
        "commit": tip, "state": "contained" if detail.startswith("origin has it") else "pushed",
    }
    for request in selected:
        land_queue.record_publication(checkout, request["commit"], receipt)
        land_queue.finish_push(request)


def _publish(checkout: Path, *, wait: bool, gap: land_queue.SettingsGap | main_push_lock.CheckLandLine
             ) -> PublishedSettings | land_queue.SettingsWaiting | main_push_lock.MainPushBusy:
    with main_push_lock.settings_push(checkout, wait=wait, gap=gap) as admission:
        if isinstance(admission, main_push_lock.MainPushReady):
            return _publish_main(checkout)
        if isinstance(admission, land_queue.SettingsWaiting):
            return admission
        return main_push_lock.MainPushBusy()


def publish(checkout: Path, *, wait: bool) -> list[str] | None:
    """Publish now in a gap or report the wait."""
    published = _publish(checkout, wait=False, gap=main_push_lock.CHECK_LAND_LINE)
    if isinstance(published, PublishedSettings):
        return published.with_mac_pull()
    if isinstance(published, land_queue.SettingsWaiting):
        return [published.report()]
    return [land_queue.enqueue_settings().report()] if wait else None


def _waiting_checkout(*, wait: bool) -> Path | None:
    with locked_journal(journal_path(), wait=wait) as pending:
        first = next((change for change in pending.changes if change.publish_on_main), None)
        has_settings_json = not isinstance(pending.settings_json, NoSettingsJsonSave)
    if first is not None:
        return main_publish.checkout_of(conf.path(first.stem).resolve())
    if has_settings_json:
        return _settings_checkout()
    for request in land_queue.deferred_pushes():
        if request["kind"] == "direct" and "journal_order" in request:
            return Path(request["checkout"])
    return None


def _publish_registry(lines: list[str], *, gap: land_queue.SettingsGap | main_push_lock.CheckLandLine) -> None:
    try:
        if models_registry.publish_pending(models_registry.registry_path(), gap=gap):
            lines.append("registry rows published")
    except OSError as error:
        lines.append(f"registry rows not on main yet: {' '.join(str(error).split())}")


def _publish_waiting_settings(lines: list[str], *, gap: land_queue.SettingsGap | main_push_lock.CheckLandLine
                              ) -> PublishedSettings | None:
    try:
        wait = isinstance(gap, land_queue.SettingsGap)
        if (checkout := _waiting_checkout(wait=wait)) is not None:
            published = _publish(checkout, wait=wait, gap=gap)
            if isinstance(published, PublishedSettings):
                return published
            if isinstance(published, land_queue.SettingsWaiting):
                lines.append(published.report())
    except BlockingIOError:
        pass
    except (OSError, main_publish.Refusal, conf.Unusable) as error:
        lines.append(f"settings not on main yet: {' '.join(str(error).split())}")
    return None


def _publish_direct(request: land_queue.DirectSettingsPush, gap: land_queue.SettingsGap, *, wait: bool) -> bool:
    checkout = Path(request["checkout"])
    if "journal_order" in request:
        return False
    with main_push_lock.settings_push(checkout, wait=wait, gap=gap) as admission:
        if not isinstance(admission, main_push_lock.MainPushReady):
            return False
        commit, _ = main_publish.prepare_deferred_commit(checkout, request["commit"], request["base"], request["branch"],
                                                         validated_base=request["validated_base"])
        landed = land_main.land(checkout, commit, request["branch"])
        print(land_main.hook_report(landed.hook), flush=True)
        land_queue.record_publication(checkout, request["commit"], {
            "commit": landed.commit,
            "state": "pushed" if isinstance(landed.push, land_main.Pushed) else "contained",
        })
    return True


def _publish_deferred(lines: list[str], gap: land_queue.SettingsGap, *, wait: bool) -> None:
    for request in land_queue.deferred_pushes():
        try:
            if request["kind"] == "direct":
                published = _publish_direct(request, gap, wait=wait)
            else:
                published = fast_track.resume_settings_push(Path(request["attempt"]), gap, wait=wait)
            if published:
                land_queue.finish_push(request)
        except (OSError, ValueError, main_publish.Refusal, land_main.LandError,
                fast_track.PublishedFailure, fast_track.Refused) as error:
            lines.append(f"settings push remains pending: {' '.join(str(error).split())}")


def publish_waiting() -> list[str]:
    """Notify the runner of pending work without taking a publication lock."""
    try:
        if (waiting_rows_for_main() or land_queue.deferred_pushes()
                or models_registry.has_pending(models_registry.registry_path())):
            return [land_queue.enqueue_settings().report()]
    except OSError as error:
        return [f"settings not on main yet: {' '.join(str(error).split())}"]
    return []


def waiting_rows_for_main() -> bool:
    pending = read_journal(journal_path())
    return (any(change.publish_on_main for change in pending.changes)
            or not isinstance(pending.settings_json, NoSettingsJsonSave))


def publish_queued(gap: land_queue.SettingsGap) -> list[str]:
    """Use the gap that the runner holds for this publisher."""
    lines: list[str] = []
    _publish_deferred(lines, gap, wait=True)
    published = _publish_waiting_settings(lines, gap=gap)
    _publish_registry(lines, gap=gap)
    if published is None:
        return lines
    mac = _queue_mac_pull(published.checkout, published.label, "settings published")
    return [*published.lines, *lines, mac]


def pull_mac(checkout: Path, label: str) -> str:
    outcome = main_publish.pull_mac(checkout, ssh_program=os.environ.get("ZONE_SSH") or "ssh",
                                    time_limit=main_publish.CommandTimeLimit(_seconds("mac_pull_seconds")))
    if isinstance(outcome, main_publish.MacPulled):
        main_publish.clear_pull_only()
        return "mac: pulled"
    return _queue_mac_pull(checkout, label, outcome.reason)


def _queue_mac_pull(checkout: Path, label: str, reason: str) -> str:
    main_publish.queue_pull_only(label, reason,
                                 pulls=(main_publish.MacCheckoutPull(PurePosixPath("~/.claude"), checkout),))
    _ = mac_wake.start()
    return f"mac: queued ({reason}); it goes out as soon as the Mac answers"


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


def _settings_checkout() -> Path:
    return main_publish.checkout_of(conf.directory().parent / SETTINGS_JSON)


def _accept_settings_json(checkout: Path, order: int, edits: SettingsJsonEdits) -> None:
    with locked_journal(journal_path()) as pending:
        if order <= pending.settings_json_order:
            return
        shared = apply_settings_json_edits(clean_settings_json(checkout), edits)
        write_text_atomic(checkout / SETTINGS_JSON, smudge_settings_json(checkout, shared))
        pending.settings_json_order = order
        pending.save()


class MacSettingsDelivery(NamedTuple):
    order: int
    edits: SettingsJsonEdits
    text: str


def _write_settings_json_mac(saved: MacSettingsDelivery) -> str:
    command = ('"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/live_settings.py" '
               + shlex.join(["apply-json", str(saved.order), json.dumps(saved.edits)]))
    done = main_publish.ssh(command, ssh_program=os.environ.get("ZONE_SSH") or "ssh",
                            time_limit=main_publish.CommandTimeLimit(_seconds("mac_write_seconds")))
    if isinstance(done, str) or done.returncode:
        reason = done if isinstance(done, str) else main_publish.first_line(done)
        return f"mac: not live yet ({reason}); the Mac pull brings it"
    with locked_journal(journal_path()) as pending:
        baseline = pending.mac_settings
        if isinstance(baseline, MacSettingsUntracked) or baseline["order"] < saved.order:
            pending.mac_settings = {"order": saved.order, "text": saved.text}
            pending.save()
    return f"mac: live now: {SETTINGS_JSON}"


def _stage_settings_json(checkout: Path, message: str, forwarded: str) -> MacSettingsDelivery | NoSettingsJsonSave:
    with locked_journal(journal_path()) as pending:
        pending.restore(checkout, "HEAD")
        head = tree_text(checkout, "HEAD", SETTINGS_JSON) or "{}\n"
        shared = clean_settings_json(checkout)
        if forwarded:
            shared = apply_settings_json_edits(shared, cast(SettingsJsonEdits, json.loads(forwarded)))
            write_text_atomic(checkout / SETTINGS_JSON, smudge_settings_json(checkout, shared))
        return _record_settings_json(pending, message, head, shared)


def _record_settings_json(pending: LockedJournal, message: str, head: str,
                          shared: str) -> MacSettingsDelivery | NoSettingsJsonSave:
    edits = settings_json_edits(head, shared)
    baseline: MacSettingsBaseline | MacSettingsUntracked = pending.mac_settings
    if isinstance(baseline, MacSettingsUntracked):
        baseline = {"order": 0, "text": head}
    mac_edits = settings_json_edits(baseline["text"], shared)
    waits = has_settings_json_edits(edits)
    if not waits and not has_settings_json_edits(mac_edits) and isinstance(pending.settings_json, NoSettingsJsonSave):
        return NoSettingsJsonSave()
    pending.last_order += 1
    saved: SettingsJsonSave = {"order": pending.last_order, "message": f"tune({SETTINGS_JSON}): {message}",
                               "edits": edits}
    pending.settings_json = saved if waits else NoSettingsJsonSave()
    pending.mac_settings = baseline
    pending.save()
    return MacSettingsDelivery(saved["order"], mac_edits, shared)


def _stage_hand_commit(request: land_queue.DirectSettingsPush) -> land_queue.DirectSettingsPush:
    checkout = Path(request["checkout"])
    if not request.get("settings_json_only") or SETTINGS_JSON not in waiting_paths(checkout):
        return request
    try:
        with main_push_lock.main_push_lock(checkout, wait=False), locked_journal(journal_path(), wait=False) as pending:
            if (git(checkout, "rev-parse", "HEAD").strip() != request["commit"]
                    or git(checkout, "branch", "--show-current").strip() != "main"):
                return request
            baseline = tree_text(checkout, request["base"], SETTINGS_JSON) or "{}\n"
            approved = tree_text(checkout, request["commit"], SETTINGS_JSON) or "{}\n"
            message = git(checkout, "show", "-s", "--format=%B", request["commit"]).strip()
            delivery = _record_settings_json(pending, message, baseline, approved)
            move = move_target(checkout, request["base"], pending, fast_forward=False, expected_head=request["commit"])
            apply_move(checkout, check_move(checkout, move, pending, keep_live_json=True), pending)
    except BlockingIOError:
        return request
    if not isinstance(delivery, NoSettingsJsonSave):
        print(_write_settings_json_mac(delivery))
    return {**request, "journal_order": pending.last_order}


def _queue_direct(request: land_queue.DirectSettingsPush) -> int:
    staged = _stage_hand_commit(request)
    admission = land_queue.enqueue_settings(staged)
    print(admission.report())
    print(f"{request['commit']} queued")
    return 0


def _forward_settings_json(checkout: Path, message: str) -> int:
    with locked_journal(journal_path()) as pending:
        pending.restore(checkout, "HEAD")
        head = tree_text(checkout, "HEAD", SETTINGS_JSON) or "{}\n"
        edits = settings_json_edits(head, clean_settings_json(checkout))
    command = ('"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/live_settings.py" '
               + shlex.join(["publish-json", "--message", message, "--forwarded", json.dumps(edits)]))
    done = main_publish.ssh(command, ssh_program=os.environ.get("ZONE_SSH") or "ssh", host="natedev",
                            time_limit=main_publish.CommandTimeLimit(_seconds("mac_write_seconds")))
    if isinstance(done, str):
        raise OSError(f"natedev: {done}")
    print(done.stdout, end="")
    print(done.stderr, end="", file=sys.stderr)
    return done.returncode


class PublishSettingsJsonArgs(argparse.Namespace):
    message: str = ""
    forwarded: str = ""


def _publish_settings_json(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="live_settings.py publish-json")
    _ = parser.add_argument("--message", required=True)
    _ = parser.add_argument("--forwarded", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(arguments, namespace=PublishSettingsJsonArgs())
    checkout = _settings_checkout()
    if platform.system() == "Darwin":
        return _forward_settings_json(checkout, args.message)
    saved = _stage_settings_json(checkout, args.message, args.forwarded)
    if isinstance(saved, NoSettingsJsonSave):
        print(f"main: {SETTINGS_JSON} has no shared changes")
        return 0
    print(f"live now: {SETTINGS_JSON}")
    print(_write_settings_json_mac(saved))
    for line in publish(checkout, wait=True) or []:
        print(line)
    return 0


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
        match argv:
            case ["queue-direct", payload]:
                return _queue_direct(cast(land_queue.DirectSettingsPush, json.loads(payload)))
            case ["publish-queued", descriptor]:
                for line in publish_queued(land_queue.inherited_gap(int(descriptor))):
                    print(line)
                return 0
            case ["publish-json", *options]:
                return _publish_settings_json(options)
            case ["apply-json", order, text]:
                _accept_settings_json(_settings_checkout(), int(order), cast(SettingsJsonEdits, json.loads(text)))
                return 0
            case _:
                pass
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
        with locked_journal(journal_path()) as pending:
            pending.restore(checkout, "HEAD")
            current = pending.order(stem, key)
            if order < current:
                print(MacWriteSuperseded.NEWER_SAVE)
            elif not current or order > current:
                setting = editable_setting(stem, key, value, accepted=accepted)
                if not order and isinstance(setting, ExistingSetting) and setting.value == value:
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
