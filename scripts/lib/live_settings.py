#!/usr/bin/env python3
"""Make an approved settings change live at once; the commit, push and Mac pull follow."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
import tempfile

from scripts.lib import conf, mac_wake, main_publish, main_push_lock, models_registry
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.waiting_rows import (
    WaitingChange as WaitingChange, append_model_color, git, journal, journal_path as journal_path,
    model_color_host, model_color_key, model_color_value, replace_value, show,
)


@dataclass(frozen=True)
class ExistingSetting:
    """The line and current value of an editable setting."""

    line: int
    value: str


@dataclass(frozen=True)
class NewModelColor:
    """The model color has no line yet and can be added."""


def _new_model_color(stem: str, key: str, value: str, *, accepted: bool) -> bool:
    if not conf.full_model_color_key(stem, key):
        return False
    if not accepted and not model_color_key(stem, key):
        return False
    if not model_color_value(value):
        raise conf.Unusable(f"{stem}: {key}: use a color in #rrggbb format")
    return True


def editable_setting(stem: str, key: str, value: str, *, accepted: bool = False) -> ExistingSetting | NewModelColor:
    """Validate the change and identify the current setting."""
    if stem == "statusline" and key.startswith("model."):
        if host := model_color_host(key):
            raise conf.Unusable(f"{stem}: {key}: .{host} is a host override, not a model color")
        settings = conf.settings(conf.path(stem).read_text(encoding="utf-8"))
        if (key not in settings and conf.host_override(stem, settings, key) is None
                and _new_model_color(stem, key, value, accepted=accepted)):
            return NewModelColor()
    index, old = conf.editable_line(stem, key)
    conf.check(stem, key, value)
    return ExistingSetting(index, old)


def write_live(stem: str, key: str, value: str, *, accepted: bool = False) -> str:
    """Write one value now and return the value it replaced, if any."""
    source = conf.path(stem)
    setting = editable_setting(stem, key, value, accepted=accepted)
    if isinstance(setting, NewModelColor):
        write_text_atomic(source, append_model_color(source.read_text(encoding="utf-8"), key, value))
        return ""
    if setting.value != value:
        write_text_atomic(source, replace_value(source.read_text(encoding="utf-8"), setting.line, value))
    return setting.value


def _journal() -> AbstractContextManager[list[WaitingChange]]:
    return journal(journal_path())


def stage(change: WaitingChange, *, accepted: bool = False) -> str:
    """Make the change live and record it for main; return the replaced value."""
    with _journal() as changes:
        old = write_live(change.stem, change.key, change.value, accepted=accepted)
        changes.append(change)
    return old


def _commit_row(checkout: Path, relative: str, change: WaitingChange) -> None:
    """Commit only this row onto HEAD; other waiting rows stay live and uncommitted."""
    head = show(checkout, "HEAD", relative)
    settings = conf.settings(head)
    if setting := settings.get(change.key):
        text = replace_value(head, setting.line, change.value)
    elif _new_model_color(change.stem, change.key, change.value, accepted=True):
        text = append_model_color(head, change.key, change.value)
    else:
        raise main_publish.Refusal(f"{relative}: {change.key}: exact stored key is missing")
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


def _publish_rows(checkout: Path, changes: list[WaitingChange]) -> list[str]:
    """Commit the valid rows main lacks and push them."""
    lines: list[str] = []
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
    """Commit and push waiting changes, then send or queue the Mac pull.

    With wait=False, a busy main returns None at once; the next notes pass tries again.
    """
    with ExitStack() as locks:
        try:
            locks.enter_context(main_push_lock.main_push_lock(checkout, wait=wait))
        except BlockingIOError:
            return None
        main_publish.ready(checkout, fetch_limit=main_publish.CommandTimeLimit(_seconds("fetch_seconds")))
        with _journal() as journal:
            changes = [change for change in journal if change.publish_on_main]
        lines = _publish_rows(checkout, changes)
        with _journal() as journal:
            journal[:] = [change for change in journal if change not in changes]
    label = " ".join(f"{change.stem}.{change.key}" for change in changes) or "main"
    return [*lines, pull_mac(checkout, label)]


def publish_waiting() -> list[str]:
    """Publish waiting /tune changes and registry rows when main is free; never wait for main."""
    lines: list[str] = []
    try:
        with _journal() as journal:
            first = next((change for change in journal if change.publish_on_main), None)
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
    arguments = [stem, key, value]
    if conf.full_model_color_key(stem, key):
        arguments.insert(0, "--accepted-model-color")
    command = ('"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/live_settings.py" '
               + shlex.join(arguments))
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
    accepted = argv[:1] == ["--accepted-model-color"]
    if accepted:
        argv = argv[1:]
    if len(argv) != 3:
        print("usage: live_settings.py <stem> <key> <value>", file=sys.stderr)
        return 2
    try:
        stem, key, value = argv
        with _journal() as changes:
            old = write_live(stem, key, value, accepted=accepted)
            if conf.full_model_color_key(stem, key):
                def same_pending_key(change: WaitingChange) -> bool:
                    return not change.publish_on_main and (change.stem, change.key) == (stem, key)

                if not old or any(same_pending_key(change) for change in changes):
                    changes[:] = [change for change in changes if not same_pending_key(change)]
                    changes.append(WaitingChange(stem, key, value, "", publish_on_main=False))
    except (OSError, UnicodeError, conf.Unusable) as error:
        print(f"live_settings: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
