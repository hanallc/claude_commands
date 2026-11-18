#!/usr/bin/env python3
"""Keep waiting configuration rows live across every fast-forward.

A waiting row is a configuration line that the live file holds and HEAD does
not. A fast-forward gives git the target's blob in the index, so the live file
keeps its rows; then the rows replay onto the target's text.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import NotRequired, TypedDict, cast

from scripts.lib import conf, conf_reads, git_index, registry_rows
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.transcripts import config_dirs

WAITING_GLOB = "config/*.conf"
WAITING_PATHS = f":(glob){WAITING_GLOB}"
REGISTRY_PATH = "config/models.conf"
STATUSLINE_PATH = "config/statusline.conf"


def model_color_host(key: str) -> str:
    """Return the reserved host suffix of a model color key, if present."""
    base = key.rpartition(".")[0]
    if conf.full_model_color_key("statusline", base):
        return conf.host_suffix(key, conf.known_host_names())
    return ""


def model_color_key(stem: str, key: str) -> bool:
    """Allow a new color only for a full model ID in its catalog."""
    if not conf.full_model_color_key(stem, key):
        return False
    model = key.removeprefix("model.")
    if model.startswith("gpt-"):
        override = os.environ.get("MODELS_CONFIG_FILE")
        source = Path(override) if override else conf.path("models")
        try:
            if not override and not source.exists():
                source = Path(__file__).resolve().parents[2] / REGISTRY_PATH
            registry = registry_rows.parse_registry(conf_reads.read_text(source))
        except (OSError, UnicodeError):
            return False
        return model in registry.get("codex.agents", {}) and model not in registry.get("codex.families", {})
    return _claude_model_listed(model)


def _claude_model_listed(model: str) -> bool:
    for directory in config_dirs():
        for path in (directory / "cache/model-catalog").glob("*-cc.json"):
            try:
                value = cast(object, json.loads(path.read_bytes()))
            except (OSError, ValueError):
                continue
            for key in ("catalog", "config", "models"):
                value = cast(dict[str, object], value).get(key) if isinstance(value, dict) else None
            if isinstance(value, list) and any(isinstance(entry, dict)
                    and cast(dict[str, object], entry).get("id") == model for entry in cast(list[object], value)):
                return True
    return False


def model_color_value(value: str) -> bool:
    """Check the hex format that tune accepts for a new model color."""
    return re.fullmatch(r"#[0-9a-f]{6}", value) is not None


def append_model_color(source: str, key: str, value: str) -> str:
    """Append one validated model color to the config text."""
    ending = "\n" if source and not source.endswith("\n") else ""
    return f"{source}{ending}{key}={value}\n"


@dataclass(frozen=True)
class WaitingChange:
    """An accepted live change that waits for publication or the Mac pull."""

    stem: str
    key: str
    value: str
    message: str
    publish_on_main: bool = True


class WaitingChangeRecord(TypedDict):
    stem: str
    key: str
    value: str
    message: str
    publish_on_main: NotRequired[bool]


def journal_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(state) / "live-settings" / "waiting.json"


def _read_journal(path: Path) -> list[WaitingChange]:
    try:
        records = cast(list[WaitingChangeRecord], json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    return [WaitingChange(**record) for record in records]


@contextmanager
def journal(path: Path | None = None) -> Generator[list[WaitingChange]]:
    """Hold the journal lock while a caller changes the accepted rows."""
    path = journal_path() if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        changes = _read_journal(path)
        before = list(changes)
        yield changes
        if changes != before:
            write_text_atomic(path, json.dumps([asdict(change) for change in changes], indent=2) + "\n")


def replace_value(source: str, index: int, value: str) -> str:
    """Keep the selected line's key, spacing, comment, and line ending."""
    lines = source.splitlines(keepends=True)
    line = lines[index]
    body = line.rstrip("\r\n")
    ending = line[len(body):]
    key, _, right = body.partition("=")
    # The equals sign precedes the value, so only whitespace can start its comment.
    parts = re.split(r"(?<=\s)#", right, maxsplit=1)
    old = parts[0]
    comment = "#" + parts[1] if len(parts) == 2 else ""
    leading = old[:len(old) - len(old.lstrip())]
    trailing = old[len(old.rstrip()):] if old.strip() else old if comment else ""
    if value.startswith("#"):
        # A hash value must touch the equals sign to avoid a comment.
        leading = ""
    lines[index] = f"{key}={leading}{value}{trailing}{comment}{ending}"
    return "".join(lines)


def replay(path: str, head: str, local: str, target: str, *,
           accepted_colors: dict[str, str] | None = None) -> str:
    """Put the rows that local changed from head onto target."""
    if path == REGISTRY_PATH:
        return registry_rows.replay_pending(head, local, target)
    before, wanted, base = conf.settings(head), conf.settings(local), conf.settings(target)
    text = target
    for key, setting in wanted.items():
        if key in before and before[key].value == setting.value:
            continue
        if key in base:
            text = replace_value(text, base[key].line, setting.value)
        elif key not in before and accepted_colors is not None and accepted_colors.get(key) == setting.value:
            text = append_model_color(text, key, setting.value)
    return text


def git(checkout: Path, *arguments: str, input_text: str | None = None,
        environment: dict[str, str] | None = None) -> str:
    done = subprocess.run(["git", "-C", str(checkout), *arguments], input=input_text, text=True,
                          capture_output=True, env=environment, check=False)
    if done.returncode:
        detail = (done.stderr or done.stdout).strip() or f"exit {done.returncode}"
        raise OSError(f"git {' '.join(arguments)}: {detail}")
    return done.stdout


def show(checkout: Path, commit: str, path: str) -> str:
    return git(checkout, "show", f"{commit}:{path}")


def waiting_files(checkout: Path) -> list[str]:
    """Tracked configuration files whose live text differs from HEAD."""
    listed = git(checkout, "diff", "--name-only", "-z", "HEAD", "--", WAITING_PATHS)
    return [path for path in listed.split("\0") if path]


@dataclass(frozen=True)
class WaitingRows:
    """The checked index entries and file text for a move to the target."""

    entries: str
    restored: dict[str, str]
    mac_pulls: tuple[WaitingChange, ...]


def _accepted_colors(head: str, local: str, target: str,
                     changes: Sequence[WaitingChange]) -> dict[str, str]:
    before, incoming = conf.settings(head), conf.settings(target)
    pending = {change.key: change.value for change in changes if change.stem == "statusline"}
    accepted: dict[str, str] = {}
    for key, setting in conf.settings(local).items():
        if key in before or not conf.full_model_color_key("statusline", key) or not model_color_value(setting.value):
            continue
        published = incoming.get(key)
        if pending.get(key) == setting.value or (published is not None and published.value == setting.value):
            accepted[key] = setting.value
    return accepted


def hold(checkout: Path, target: str, *, skip: tuple[str, ...] = ()) -> WaitingRows:
    """Read the waiting rows that a move to target must keep; refuse other local edits in them."""
    changed = set(git(checkout, "diff", "--name-only", "-z", "HEAD", target, "--").split("\0"))
    touched = [path for path in waiting_files(checkout) if path in changed and path not in skip]
    entries = git(checkout, "ls-tree", "-z", target, "--", *touched).replace("\0", "\n") if touched else ""
    if len(entries.splitlines()) != len(touched):
        raise OSError(f"{target} removes a file with waiting rows: {', '.join(touched)}")
    restored: dict[str, str] = {}
    mac_pulls: tuple[WaitingChange, ...] = ()
    for path in touched:
        head = show(checkout, "HEAD", path)
        local = (checkout / path).read_text(encoding="utf-8")
        incoming = show(checkout, target, path)
        colors = None
        if path == STATUSLINE_PATH:
            try:
                changes = _read_journal(journal_path())
            except (ValueError, TypeError) as error:
                raise OSError(f"cannot read accepted colors: {error}") from error
            colors = _accepted_colors(head, local, incoming, changes)
            published = conf.settings(incoming)
            mac_pulls = tuple(change for change in changes if not change.publish_on_main
                              and change.key in published and published[change.key].value == change.value)
        if path != REGISTRY_PATH and replay(path, head, local, head, accepted_colors=colors) != local:
            raise OSError(f"{path} has local edits other than setting values")
        restored[path] = replay(path, head, local, incoming, accepted_colors=colors)
    return WaitingRows(entries, restored, mac_pulls)


def give(checkout: Path, rows: WaitingRows, environment: dict[str, str] | None = None) -> None:
    """Give git the target's blob for each waiting file, so the move keeps the live file."""
    if rows.entries:
        _ = git(checkout, "update-index", "--index-info", input_text=rows.entries, environment=environment)


def restore(checkout: Path, rows: WaitingRows) -> None:
    """Put the waiting rows onto the target's text of each file after the move."""
    for path, text in rows.restored.items():
        if (checkout / path).read_text(encoding="utf-8") != text:
            write_text_atomic(checkout / path, text)
    if rows.mac_pulls:
        with journal() as changes:
            changes[:] = [change for change in changes if change not in rows.mac_pulls]


def move_to(checkout: Path, target: str) -> None:
    """Move the checkout to target, as a fast-forward does, and keep every waiting row live.

    Target need not descend from HEAD: a publisher replaces its own local commits with copies on main.

    The check runs on a private index before any write. Another local edit on
    a path that target changes refuses the move, as git does.
    """
    if Path(git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").strip()).exists():
        raise OSError("an unfinished merge must be completed first")
    if git(checkout, "ls-files", "--unmerged").strip():
        raise OSError("unmerged index entries must be resolved first")
    rows = hold(checkout, target)
    index = Path(git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
    with tempfile.TemporaryDirectory(prefix="waiting-rows-index-") as directory:
        trial = Path(directory) / "index"
        git_index.private_copy(index, trial)
        environment = {**os.environ, "GIT_INDEX_FILE": str(trial)}
        give(checkout, rows, environment)
        _refresh(checkout, environment)
        _ = git(checkout, "read-tree", "--dry-run", "-m", "-u", "HEAD", target, environment=environment)
    head = git(checkout, "rev-parse", "HEAD").strip()
    tip = git(checkout, "rev-parse", f"{target}^{{commit}}").strip()
    give(checkout, rows)
    _refresh(checkout)
    # The two-tree merge that `git merge --ff-only` runs; it keeps staged work on paths target does not change.
    _ = git(checkout, "read-tree", "-m", "-u", "HEAD", tip)
    _ = git(checkout, "update-ref", "-m", f"waiting_rows: move to {target}", "HEAD", tip, head)
    restore(checkout, rows)


def _refresh(checkout: Path, environment: dict[str, str] | None = None) -> None:
    """Refresh file times in the index, as git merge does first; a stale time reads as an edit."""
    # It exits 1 for each modified file; the merge judges those.
    _ = subprocess.run(["git", "-C", str(checkout), "update-index", "-q", "--refresh"], env=environment,
                       capture_output=True, check=False)


def main(arguments: list[str] | None = None) -> int:
    """Fast-forward the checkout in the current folder: `waiting_rows.py fast-forward <tip>`."""
    argv = sys.argv[1:] if arguments is None else arguments
    if len(argv) != 2 or argv[0] != "fast-forward":
        print("usage: waiting_rows.py fast-forward <tip>", file=sys.stderr)
        return 2
    checkout = Path.cwd()
    try:
        if subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", "HEAD", argv[1]],
                          check=False).returncode:
            raise OSError(f"{argv[1]} is not a fast-forward of HEAD")
        move_to(checkout, argv[1])
    except (OSError, UnicodeError) as error:
        print(f"waiting_rows: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
