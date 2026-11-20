#!/usr/bin/env python3
"""Fast-forward a main checkout to origin/main without deleting local work.

Changes that match origin/main can come from an interrupted update. Prove every
candidate's content, mode, and index state before changing any file. Remove proved
untracked files before restoring tracked paths to HEAD. Then finish the update
under the journal lock. The move keeps live configuration rows. A blocked report names every path that fails the proof or collides
with an incoming path.

Each run appends a JSON line to SYNC_LOG. Each blocked run with --notify calls
send.py with a key for the host and state. send.py limits delivery to one message
per main_sync_repeat_minutes window from send.conf. A successful run changes no
notification state. Fetch failures advise github-warmup only when
github-warm-status fails, times out, or is missing.

Exit 0 means the checkout is at origin/main. Exit 1 means the run is blocked.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.lib import conf, waiting_rows
from scripts.message import session_send
from scripts.lib.process_failure import failure_detail
from scripts.production.production_doc import (
    LiveFileConflict as LiveFileConflict, ProductionPathsError, live_file_conflicts as live_file_conflicts,
)

STATE_ENV = "MAIN_SYNC_STATE_DIR"
DEFAULT_STATE_DIR = os.path.join("~", ".local", "state", "main-sync")
SYNC_LOG = "sync.jsonl"
FETCH_TIMEOUT_SECONDS = 120
IN_PROGRESS = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply")
# A job has no terminal: git and ssh must fail rather than ask.
GIT_ENVIRONMENT = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=20"}

CURRENT = "current"
ADVANCED = "advanced"


@dataclass(frozen=True)
class GitFileState:
    mode: str
    object_id: str


ABSENT_FILE = GitFileState("000000", "")


@dataclass(frozen=True)
class Outcome:
    state: str
    detail: str

    @property
    def blocked(self) -> bool:
        return self.state not in (CURRENT, ADVANCED)


def _git(checkout: Path, *arguments: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(checkout), *arguments], capture_output=True, text=True,
                              errors="surrogateescape", check=False,
                              timeout=timeout, env={**os.environ, **GIT_ENVIRONMENT})
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git"], 124, "", f"git {arguments[0]} took over {timeout:.0f} s")


def _first_line(result: subprocess.CompletedProcess[str]) -> str:
    return failure_detail(result, lines=1).replace("\n", "; ")


def _matches_file(checkout: Path, name: str, entry: GitFileState) -> bool:
    path = checkout / name
    if any((checkout / parent).is_symlink() for parent in Path(name).parents):
        return False
    try:
        mode = path.lstat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return entry == ABSENT_FILE
    if stat.S_ISLNK(mode):
        git_mode = "120000"
        content = os.fsencode(os.readlink(path))
    elif stat.S_ISREG(mode):
        git_mode = "100755" if mode & stat.S_IXUSR else "100644"
        if entry.mode != git_mode:
            return False
        content = path.read_bytes()
    else:
        return False
    if entry.mode != git_mode:
        return False
    hashed = subprocess.run(["git", "-C", str(checkout), "hash-object", "--no-filters", "--stdin"],
                            input=content, capture_output=True, check=False, timeout=30,
                            env={**os.environ, **GIT_ENVIRONMENT})
    return hashed.returncode == 0 and hashed.stdout.decode("ascii").strip() == entry.object_id


def _finish_interrupted_update(checkout: Path, move: waiting_rows.CheckoutMove,
                               blocking_details: list[str], waiting: set[str]) -> bool:
    """Restore candidates only after every changed working file matches origin/main.

    Files in `waiting` hold waiting rows; the move keeps them.
    """
    queries = [
        _git(checkout, "ls-tree", "-r", "-z", move.head),
        _git(checkout, "ls-tree", "-r", "-z", move.tip),
        _git(checkout, "-c", "core.filemode=true", "diff", "--name-only", "--no-renames", "-z", "HEAD", "--"),
        _git(checkout, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD", "--"),
        _git(checkout, "ls-files", "--others", "-z"),
        _git(checkout, "ls-files", "--stage", "-z"),
    ]
    for result in queries:
        if result.returncode:
            blocking_details.append(_first_line(result))
            return False
    trees: list[dict[str, GitFileState]] = []
    for result in queries[:2]:
        files: dict[str, GitFileState] = {}
        for record in result.stdout.split("\0"):
            if record:
                metadata, name = record.split("\t", 1)
                mode, _, object_id = metadata.split()
                files[name] = GitFileState(mode, object_id)
        trees.append(files)
    head_files, target_files = trees
    index_files: dict[str, GitFileState] = {}
    for record in queries[5].stdout.split("\0"):
        if record:
            metadata, name = record.split("\t", 1)
            mode, object_id, _ = metadata.split()
            index_files[name] = GitFileState(mode, object_id)
    tracked = set(queries[2].stdout.split("\0") + queries[3].stdout.split("\0")) - {""} - waiting
    added = target_files.keys() - head_files.keys()
    untracked = set(queries[4].stdout.split("\0")) - waiting - {""}
    blocking_details.extend(sorted(name for name in untracked if any(
        name.startswith(path + "/") or path.startswith(name + "/") for path in added)))
    restore: list[str] = []
    remove: list[str] = []
    for name in sorted(tracked | (untracked & added)):
        try:
            if name in tracked and index_files.get(name, ABSENT_FILE) not in (
                    head_files.get(name, ABSENT_FILE), target_files.get(name, ABSENT_FILE)):
                blocking_details.append(name)
                continue
            if _matches_file(checkout, name, head_files.get(name, ABSENT_FILE)):
                continue
            if not _matches_file(checkout, name, target_files.get(name, ABSENT_FILE)):
                blocking_details.append(name)
                continue
            (restore if name in tracked else remove).append(name)
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            blocking_details.append(name)
    if blocking_details:
        return False
    try:
        for name in remove:
            (checkout / name).unlink()
        if restore:
            restored = _git(checkout, "--literal-pathspecs", "restore", "--source=HEAD", "--staged", "--worktree",
                            "--", *restore)
            if restored.returncode:
                blocking_details.append(_first_line(restored))
                return False
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        blocking_details.append(str(error) or type(error).__name__)
        return False
    return bool(restore or remove)


def sync(checkout: Path) -> Outcome:
    """Fast-forward checkout to origin/main when that is safe, else say why not."""
    branch = _git(checkout, "branch", "--show-current")
    if branch.returncode:
        return Outcome("unreadable", f"git cannot read {checkout}: {_first_line(branch)}")
    if branch.stdout.strip() != "main":
        return Outcome("not-main", f"{checkout} is on {branch.stdout.strip() or 'a detached HEAD'}, not main")
    git_dir = Path(_git(checkout, "rev-parse", "--absolute-git-dir").stdout.strip() or checkout / ".git")
    busy = [name for name in IN_PROGRESS if (git_dir / name).exists()]
    if busy:
        return Outcome("in-progress", f"{checkout} has a git operation in progress ({busy[0]})")
    try:
        waiting_rows.restore_live(checkout)
        fetched = _git(checkout, "fetch", "-q", "--no-tags", "--refmap=", "origin", "refs/heads/main",
                       timeout=FETCH_TIMEOUT_SECONDS)
        if fetched.returncode:
            try:
                warm = subprocess.run(["github-warm-status"], capture_output=True, check=False, timeout=5).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                warm = False
            advice = ("GitHub credentials are warm. GitHub refused the fetch. This is likely a GitHub incident; "
                      + "the job retries" if warm else f"run github-warmup at a terminal on {socket.gethostname()}")
            return Outcome("fetch-failed", f"git fetch origin main failed in {checkout}: {_first_line(fetched)}; {advice}")
        with waiting_rows.locked_journal(waiting_rows.journal_path()) as pending:
            fetched_tip = _git(checkout, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
            if fetched_tip.returncode:
                return Outcome("fetch-failed", _first_line(fetched_tip))
            tip = fetched_tip.stdout.strip()
            conflicts = live_file_conflicts(checkout, tip)
            if conflicts:
                return Outcome("live-file-conflict", "\n".join(str(conflict) for conflict in conflicts))
            tracked = _git(checkout, "update-ref", "refs/remotes/origin/main", tip)
            if tracked.returncode:
                return Outcome("fetch-failed", _first_line(tracked))
            move = waiting_rows.move_target(checkout, tip, pending, fast_forward=True)
            return _advance(checkout, move, pending)
    except waiting_rows.Diverged:
        return Outcome("diverged", f"{checkout} main holds commits not on origin/main, so it cannot fast-forward")
    except (OSError, UnicodeError, ValueError, ProductionPathsError) as error:
        return Outcome("ff-failed", f"the move to origin/main failed in {checkout}: {' '.join(str(error).split())}")


def _advance(checkout: Path, move: waiting_rows.CheckoutMove, pending: waiting_rows.LockedJournal) -> Outcome:
    if move.head == move.tip:
        return Outcome(CURRENT, move.head[:8])
    blocking_details: list[str] = []
    waiting = set(waiting_rows.waiting_files(checkout)) | waiting_rows.pending_files(checkout, pending).keys()
    interrupted = _finish_interrupted_update(checkout, move, blocking_details, waiting)
    if blocking_details:
        return Outcome("dirty", f"{checkout} has local changes that block the update ({', '.join(blocking_details)})")
    waiting_rows.apply_move(checkout, waiting_rows.check_move(checkout, move, pending), pending)
    suffix = "; finished an interrupted update" if interrupted else ""
    return Outcome(ADVANCED, f"{move.head[:8]}..{move.tip[:8]}{suffix}")


def state_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get(STATE_ENV, DEFAULT_STATE_DIR)))


def record(outcome: Outcome, checkout: Path, now: float) -> None:
    """Append the run to the log."""
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / SYNC_LOG).open("a", encoding="utf-8") as log:
        _ = log.write(json.dumps({"at": now, "checkout": str(checkout), "state": outcome.state,
                                  "detail": outcome.detail}) + "\n")


def notify(name: str, outcome: Outcome) -> None:
    minutes = conf.read("send").number("main_sync_repeat_minutes")
    host = socket.gethostname()
    text = (f"main sync on {host}: {outcome.detail}. Commands there stay old until this is "
            + "cleared; the job retries every 15 minutes.")
    _ = subprocess.run(session_send.send_argv("--to", name, "--from", "main-sync", "--text", text,
                        "--key", f"main-sync-{host}-{outcome.state}", "--repeat-minutes", f"{minutes:g}"),
                       check=False, capture_output=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="main_sync.py", description="Fast-forward a main checkout to origin/main.")
    _ = parser.add_argument("--checkout", default="~/.claude")
    _ = parser.add_argument("--notify", default="")
    arguments = parser.parse_args(argv)
    checkout = Path(os.path.expanduser(cast(str, arguments.checkout)))
    outcome = sync(checkout)
    print(f"main sync: {outcome.state}: {outcome.detail}")
    notify_name = cast(str, arguments.notify)
    record(outcome, checkout, time.time())
    if outcome.blocked and notify_name:
        notify(notify_name, outcome)
    return 1 if outcome.blocked else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
