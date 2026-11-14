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

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from scripts.lib import conf, git_index, registry_rows
from scripts.lib.atomic_file import write_text_atomic

WAITING_GLOB = "config/*.conf"
WAITING_PATHS = f":(glob){WAITING_GLOB}"
REGISTRY_PATH = "config/models.conf"


def replace_value(source: str, index: int, value: str) -> str:
    """Keep the selected line's key, spacing, comment, and line ending."""
    lines = source.splitlines(keepends=True)
    line = lines[index]
    body = line.rstrip("\r\n")
    ending = line[len(body):]
    key, _, right = body.partition("=")
    old, marker, comment = right.partition("#")
    leading = old[:len(old) - len(old.lstrip())]
    trailing = old[len(old.rstrip()):] if old.strip() else ""
    lines[index] = f"{key}={leading}{value}{trailing}{marker}{comment}{ending}"
    return "".join(lines)


def replay(path: str, head: str, local: str, target: str) -> str:
    """Put the rows that local changed from head onto target."""
    if path == REGISTRY_PATH:
        return registry_rows.replay_pending(head, local, target)
    before, wanted, base = conf.settings(head), conf.settings(local), conf.settings(target)
    text = target
    for key, setting in wanted.items():
        if key in base and (key not in before or before[key].value != setting.value):
            text = replace_value(text, base[key].line, setting.value)
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
    """The waiting files that a move to target changes, with their texts before the move."""

    target: str
    entries: str
    head: dict[str, str]
    local: dict[str, str]


def hold(checkout: Path, target: str, *, skip: tuple[str, ...] = ()) -> WaitingRows:
    """Read the waiting rows that a move to target must keep; refuse other local edits in them."""
    changed = set(git(checkout, "diff", "--name-only", "-z", "HEAD", target, "--").split("\0"))
    touched = [path for path in waiting_files(checkout) if path in changed and path not in skip]
    entries = git(checkout, "ls-tree", "-z", target, "--", *touched).replace("\0", "\n") if touched else ""
    if len(entries.splitlines()) != len(touched):
        raise OSError(f"{target} removes a file with waiting rows: {', '.join(touched)}")
    head = {path: show(checkout, "HEAD", path) for path in touched}
    local = {path: (checkout / path).read_text(encoding="utf-8") for path in touched}
    for path in touched:
        if path != REGISTRY_PATH and replay(path, head[path], local[path], head[path]) != local[path]:
            raise OSError(f"{path} has local edits other than setting values")
    return WaitingRows(target, entries, head, local)


def give(checkout: Path, rows: WaitingRows, environment: dict[str, str] | None = None) -> None:
    """Give git the target's blob for each waiting file, so the move keeps the live file."""
    if rows.entries:
        _ = git(checkout, "update-index", "--index-info", input_text=rows.entries, environment=environment)


def restore(checkout: Path, rows: WaitingRows) -> None:
    """Put the waiting rows onto the target's text of each file after the move."""
    for path in rows.head:
        text = replay(path, rows.head[path], rows.local[path], show(checkout, rows.target, path))
        if (checkout / path).read_text(encoding="utf-8") != text:
            write_text_atomic(checkout / path, text)


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
