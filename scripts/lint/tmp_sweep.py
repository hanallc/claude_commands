#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""List idle scratch for manual removal.

Agents leave shots, traces, gate copies and scratch builds in /tmp/claude and in
Claude Code's own /tmp/claude-<uid>/<project>/ folders, and nothing removed
them: on 2026-10-25 the two held 122 GiB and 86 GiB. sweep.py --floor-only, on
disk-floor.nix's 2-minute timer, calls run_if_due, which runs this sweep when
its last run started RUN_INTERVAL_SECONDS or more ago, and before the floor
takes any build cache. On the Mac, disk-floor does not run, and the tmp-sweep job
in nixos modules/darwin/tmp-sweep.nix runs `tmp_sweep.py` every hour.
`tmp_sweep.py [--dry-run]` runs it at once.
For hand cleanup use `tmp_sweep.py --plan <new-plan.json>` first; it lists every
candidate with keep/remove and a reason, writes the plan, and removes nothing.
This diagnostic plan requires evidence that the work ended.
Use removal_guard.py to remove entries from a reviewed removal list.
The timed sweep keeps top-level /tmp/claude entries and direct unit/delegate run
folders without that end evidence until nothing in them changed for 7 days.
Files and links at the top level also wait 7 days. Tempfile-named entries and
invoke/verify logs keep their existing rules. Every process and ownership check
still applies; the log distinguishes an ended owner from the 7-day fallback.
The timed sweep also prunes the dataclass code cache of the startup hook
(scripts/lib/python_startup/sitecustomize.py): an entry goes when no process
read it in lint.conf `dataclass_code_days` days, and an emptied cache tag folder goes with it.

An entry is one top-level file, folder or link under a root: /tmp/claude,
/tmp/claude/delegate, /tmp/claude/unit, or a /tmp/claude-<uid>/-<project>/
folder of each /tmp/claude-<uid> this user owns, or a scratch root of scripts/lib/scratch.py
(build_folders.conf roots.scratch, and /tmp/claude/scratch, which natedev used before /build),
where each task's folder is an entry: it stays while its task is open, goes
when its until: time passes, and goes after TASK_IDLE_SECONDS whatever its task. Each running
session's temp root (SESSION_TEMP_ROOT/<session id>, its TMPDIR) stays, as does each run's tmp/ (its TMPDIR).
Run keep reasons keep the run folder. Its tmp/ entries use the 4 h and process-use rules.
A director or session worktree guard permits the same checks for closed phases. Open runs and open phases stay.
Unreadable ownership or phase state keeps the affected entries. Each session temp entry also uses the 4 h rule.
In a temp folder a folder's ctime is no change, since
systemd-tmpfiles-clean moves it each day when it sets the folder's times back. In a temp folder (/tmp, $TMPDIR
and the Mac's per-user temp folder) only a tempfile folder is an entry, the
name a tempfile maker gives (TEMPFILE_NAME: Rust's tempfile crate, mktemp and
Python's tempfile), which test runs leave behind, and a lint/invoke.sh log
file (INVOKE_TEMP_NAME). A direct/verify.sh run folder (verify_temp_name()) is an entry
too: it stays while the run whose pid it names runs, and goes at once after. In /tmp alone (the
wide root) every other empty folder this user owns, also one that holds only folders, is an entry
too, whatever its name, and uses the configured folder removal age instead. A folder a system tool owns (SYSTEM_TEMP_NAMES,
SYSTEM_TEMP_PREFIXES; an empty EMPTY_PREFIXES one aside), another root and a
folder that holds one stay. A folder in /tmp that holds something is
folder_audit.py's, which checks it for unsaved git work first. Roots are real paths on both machines. An entry is deleted when
both are true:

  * nothing in it changed for IDLE_SECONDS. A change is the newer of mtime and
    ctime (a `cp -a` copy keeps its mtime but sets ctime), over every file,
    folder and link inside it; the entry's own times do not count, except for a
    file, a link, or a folder with nothing in it;
  * no process has its working folder, an open file or a mapped file inside it
    (one /proc scan per run; where no /proc exists, one lsof scan).

These stay too: runs named by staff rows, runs with any worker not ended (including
paused workers), and runs using a live or paused session worktree. These runs also keep folders named by cwd in their
detached_runs/*.json records. Empty detached_runs files are not records. Pending files are not read.
A worker thread not ended keeps its session temp parent, but its old temp entries can go.
An unreadable worker record keeps scratch entries and top-level /tmp/claude entries, and names
the record in the log. Before an idle entry can go, a git check searches
three folder levels without following links. Uncommitted work, a stash, commits absent from remote
and clone-source refs, or a failed git check keep it. Git commands have a timeout and never fetch.
Timed and manual cleanup use the same checks. The roots stay.
Staff records each director's open run.
A run also stays when its ownership record identifies a running director.
The `unit_runs.director` reader resolves that owner through staff.
A session folder stays when its name identifies a running session.
An entry of SESSION_ENTRY_MIN_BYTES or more in
its folders (scratchpad/, tasks/) is judged like any other: a live session
keeps small files there that it only reads, and a read does not move mtime;
each of those when a session record cannot be read; a scratch folder of a change review
row, which its verdict removes (the user can take days to check a row);
and an entry that cannot be read. Links are never followed, a read-only folder
is made writable and deleted, and a project folder left empty goes. With
neither /proc nor lsof nothing goes.

Each run appends one JSON line to LOG_NAME in the lint-sweep state folder: the
entries deleted, the GiB freed, the count kept for each reason, and a sample of
the paths kept for each reason except UNLISTED_REASONS, which hold most entries.
lint.conf `tmp_sweep_log_sample` sets the sample size. Lines older than
LOG_KEEP_SECONDS drop off, and the oldest lines also drop off while the log is
larger than lint.conf `tmp_sweep_log_max_mib`. This replaced the 24-hour session scratch step (2026-10-21), whose
folders it also covers.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass


import json
from importlib import import_module
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict, cast
from urllib.parse import unquote, urlparse

from scripts.lib import build_folders, conf, copy_record, removal_guard, scratch, unit_runs
from scripts.lib.conf import UseConfiguredValue  # noqa: E402
from scripts.lib.scratch import delete_sweep_tree
from scripts.lib.python_startup.sitecustomize import dataclass_code_root, dataclass_code_usable  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.agents import remote
from scripts.mac_test import nextest_log
from scripts.direct import phase_reservation, recorder_state

if TYPE_CHECKING:
    from scripts.production import staff

delete_tree = delete_sweep_tree

GIB = 1 << 30
HOUR_SECONDS = 3600
IDLE_SECONDS = 4 * HOUR_SECONDS
# A scratch folder whose task is open goes after this long anyway: an ender can fail.
TASK_IDLE_SECONDS = 7 * 24 * HOUR_SECONDS
# Inside a running session's folders only an entry this large can go (showrunner, 2026-10-25).
SESSION_ENTRY_MIN_BYTES = GIB
RUN_INTERVAL_SECONDS = HOUR_SECONDS
LOG_KEEP_SECONDS = 7 * 24 * HOUR_SECONDS
TMP_ROOT = "/tmp/claude"
SYSTEM_TEMP_ROOT = "/tmp"
# Each Claude session's TMPDIR is SESSION_TEMP_ROOT/<session id> (hooks/session-start-temp-root.py),
# and each run's is <run folder>/RUN_TEMP_NAME; the entries in both are judged with the 4 h rule.
class SessionTemporaryRoot(Protocol):
    ROOT: str

    def temp_parent(self) -> Path: ...


_session_temp = cast(SessionTemporaryRoot, cast(object, import_module("scripts.hooks.session-start-temp-root")))
SESSION_TEMP_ROOT = _session_temp.ROOT
RUN_TEMP_NAME = unit_runs.RUN_TEMP_NAME
# Claude Code keeps each session's scratch in /tmp/claude-<uid>/<project>/<session id>/.
SESSION_SCRATCH_PREFIX = "claude-"
PROC_DIR = "/proc"
# Where no /proc exists (the Mac), one lsof scan lists what each process uses.
LSOF = ("lsof", "-n", "-P", "-w", "-F", "pfan")
LSOF_TIMEOUT_SECONDS = 120
# lsof's fd names for a working folder and for mapped files (program text, memory maps).
LSOF_CWD = "cwd"
LSOF_MAPPED = ("txt", "mem")
WORKS_IN = "works in it"
MAPS_FILE = "maps a file in it"
HOLDS_FILE = "holds a file open in it"
WRITES_FILE = "holds a file open for writing in it"
# The kept reason when the Cargo output guard keeps a folder's external output.
# The folder names tempfile makers give by default: Rust's tempfile crate ".tmp" and 6 letters or
# digits, mktemp "tmp." and 8 to 10, Python's tempfile "tmp" and 8 lower-case letters, digits or "_".
# Test runs leave them behind in the temp folders (3,058 Rust ones in natedev /tmp since 2026-09-06;
# 3,917 mktemp and Python ones on 2026-10-26).
# Folders in /tmp that system tools own; the 7-day rule never judges them, except an empty one with
# an EMPTY_PREFIXES name: nix-shell and nix develop leave theirs (1,321 on natedev, 2026-10-27).
SYSTEM_TEMP_NAMES = frozenset({"claude", ".X11-unix", ".ICE-unix", ".XIM-unix", ".font-unix", ".Test-unix"})
SYSTEM_TEMP_PREFIXES = ("systemd-private-", "tmux-", "nix-", "ssh-", "claude-", "com.apple.", "powerlog")
EMPTY_PREFIXES = ("nix-",)
# lint/invoke.sh's own logs in $TMPDIR or /tmp; a killed run leaves them (1,308 lint_invoke and
# 1,438 buildlog files in natedev /tmp on 2026-10-27).
INVOKE_TEMP_NAME = re.compile(r"lint_invoke\.\d+\.\d+\.log|buildlog\.\d+\.\d+\.(?:log|peak|peak\.cgroup)")
TEMPFILE_NAME = re.compile(r"\.tmp[A-Za-z0-9]{6}|tmp\.[A-Za-z0-9]{8,10}|tmp" + build_folders.MKDTEMP_SUFFIX_PATTERN)
# direct/verify.sh's run folder (its copy folders sit inside it), named with the run's pid. Its EXIT
# trap removes it, but a killed run leaves it (47 hold and 6 copy folders in natedev /tmp, 2026-10-27).
# A named pid that is no verify.sh run started before the folder ends the folder at once; an old name
# with no pid waits out the 4 h rule.
def verify_temp_name() -> re.Pattern[str]:
    """Read the current prefixes for verify run folders."""
    return re.compile(
        "(?:" + "|".join(re.escape(build_folders.FOLDER_PREFIXES[kind].split("{owner}")[0])
                        for kind in ("verify-hold", "verify-copy"))
        + r")(?:(\d+)\.)?(?:" + build_folders.LEGACY_MKTEMP_SUFFIX_PATTERN
        + "|" + build_folders.MKDTEMP_SUFFIX_PATTERN + ")")


VERIFY_SCRIPT = "verify.sh"
# ps prints elapsed time to the second, so a run's start can read up to this late.
START_SLACK_SECONDS = 2.0
# A folder in /tmp that holds only folders, however deep, is empty; a tree of more folders than this
# counts as holding something (155 nix-shell folders of empty folders on natedev, 2026-10-27).
EMPTY_TREE_FOLDERS = 1000
DARWIN_TEMP_NAME = "CS_DARWIN_USER_TEMP_DIR"
DELETED_SUFFIX = " (deleted)"
STATE_ENV = "LINT_SWEEP_STATE_DIR"
DEFAULT_STATE_DIR = os.path.join("~", ".local", "state", "lint-sweep")
LOG_NAME = "tmp-sweep.jsonl"
STAMP_NAME = "tmp-sweep.stamp"
TARGET_DELETES_NAME = "target-deletes.json"
DRY_RUN_FLAG = "--dry-run"

ROOT = "a root"
SESSION_RUNS = "its session runs"
DIRECTOR_RUNS = "its unit director runs"
ROW_WAITS = "its change review row waits for a verdict"
NO_SESSIONS = "a session record cannot be read"
IN_USE = "a process uses it"
VERIFY_RUNS = "its verify.sh run is running"
RECENT = "changed in the last 4 h"
RECENT_WIDE = "changed in the last 7 days"
NO_END_RECENT = "no record proves its owner ended, and it changed in the last 7 days"
NO_END_OLD = "no record proves its owner ended; unchanged for 7 days and unused"
TASK_OPEN = "its task is open and it changed in the last 7 days"
SMALL_IN_LIVE_SESSION = "under 1 GiB in a running session's folder"
UNLISTED_REASONS = (RECENT, RECENT_WIDE, SMALL_IN_LIVE_SESSION, TASK_OPEN)
UNREADABLE = "it cannot be read"
PHASE_OPEN = "its phase is open"
WORKER_USES = "a live worker uses it"
UNSAVED_WORK = "it holds unsaved git work"
GIT_DEPTH = 3
GIT_TIMEOUT_SECONDS = 10
NO_PROC = "no /proc or lsof to show which processes use it"


class DeletedEntry(TypedDict):
    path: str
    gib: float
    last_change: str
    reason: str


class FailedEntry(TypedDict):
    path: str
    error: str


class RunRecord(TypedDict):
    at: str
    dry_run: bool
    freed_gib: float
    deleted: list[DeletedEntry]
    failed: list[FailedEntry]
    kept: dict[str, int]
    kept_paths: dict[str, list[str]]


@dataclass(frozen=True)
class Limit:
    """An entry goes when nothing in it changed at or after `since`; `recent` is why it stays when
    something did. Without `folder_ctime`, a folder's ctime is no change."""
    since: float
    recent: str
    folder_ctime: bool = True


def report_unreadable_copy_folder(path: Path, error: OSError) -> None:
    print(f"tmp sweep: cannot read {path}: {error}", file=sys.stderr)


@dataclass(frozen=True)
class Roots:
    """The folders whose top-level entries the sweep judges, by kind. In a temp folder only a
    tempfile folder (TEMPFILE_NAME) and an invoke.sh log (INVOKE_TEMP_NAME) are judged, and in a
    wide one an empty folder too."""
    plain: tuple[str, ...]
    runs: tuple[str, ...]
    projects: tuple[str, ...]
    temp: tuple[str, ...] = ()
    scratch: tuple[str, ...] = ()
    wide: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    copies: tuple[str, ...] = ()
    # Dataclass code cache roots; prune_dataclass_code judges each entry of each cache tag folder.
    dataclass_code: tuple[str, ...] = ()
    verify_temp_name: re.Pattern[str] = field(default_factory=verify_temp_name, kw_only=True)

    def all(self) -> tuple[str, ...]:
        return self.plain + self.runs + self.projects + self.temp + self.scratch + self.sessions + self.copies

    def entries(self, root: str, *,
        unreadable_copies: Callable[[Path, OSError], None] = report_unreadable_copy_folder,
    ) -> Iterator[os.DirEntry[str]]:
        """The entries of root the sweep judges, without retaining the directory."""
        if root in self.copies:
            for pair in scratch.legacy_copy_pairs(Path(root), unreadable=unreadable_copies):
                for entry in listing(str(pair), unreadable=unreadable_copies):
                    path = Path(entry.path)
                    copy = scratch.removal_target(path)
                    if copy is not None:
                        removal = scratch.checked_removal(copy)
                        if isinstance(removal, (copy_record.Registered, scratch.StaleRemoval)) or (
                                isinstance(removal, copy_record.NotRegistered) and not os.path.lexists(entry.path)):
                            continue
                    if not scratch.protected_build_path(path):
                        yield entry
            return
        in_build_root = Path(root).resolve().is_relative_to(copy_record.build_root())
        for entry in listing(root):
            if in_build_root and scratch.protected_build_path(Path(entry.path)):
                continue
            if root not in self.temp or self.judged_temp(root, entry):
                yield entry

    def judged_temp(self, root: str, entry: os.DirEntry[str]) -> bool:
        try:
            if entry.is_symlink():
                return False
            if entry.is_file():
                return INVOKE_TEMP_NAME.fullmatch(entry.name) is not None
            return entry.is_dir() and (TEMPFILE_NAME.fullmatch(entry.name) is not None
                                       or self.verify_temp_name.fullmatch(entry.name) is not None
                                       or self.judged_empty(root, entry))
        except OSError:
            return False

    def is_wide(self, root: str, entry: os.DirEntry[str]) -> bool:
        """Whether entry uses the configured folder removal age: an empty folder in a wide root that is
        no tempfile folder."""
        name = entry.name
        return root in self.wide and not any(pattern.fullmatch(name) for pattern in (TEMPFILE_NAME, INVOKE_TEMP_NAME, self.verify_temp_name))

    def limit(self, root: str, entry: os.DirEntry[str], now: float, proc: str = PROC_DIR, *,
        lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
        scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    ) -> Limit:
        """How long entry must be idle to go, and how a change in it counts."""
        if isinstance(lint_settings, UseConfiguredValue):
            lint_settings = conf.read("lint")
        if isinstance(scratch_settings, UseConfiguredValue):
            scratch_settings = conf.read("scratch")
        verify = self.verify_temp_name.fullmatch(entry.name) if root in self.temp else None
        if verify is not None and verify.group(1):
            try:
                made = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                made = now
            running = verify_running(int(verify.group(1)), made, now, proc)
            return Limit(float("-inf"), VERIFY_RUNS) if running else Limit(float("inf"), RECENT)
        if root in self.scratch:
            record = scratch.task_of(Path(entry.path), scratch_settings=scratch_settings,
                                     intent=scratch.RemovalIntentAccess.READ_ONLY)
            if isinstance(record, copy_record.Registered) and scratch.ended(record.task, now):
                return Limit(float("inf"), RECENT)
            if isinstance(record, copy_record.Registered):
                return Limit(now - TASK_IDLE_SECONDS, TASK_OPEN)
        if root in self.copies:
            if isinstance(scratch.checked_removal(Path(entry.path)), copy_record.Registered):
                return Limit(float("inf"), TASK_OPEN)
            record = scratch.task_of(Path(entry.path), scratch_settings=scratch_settings,
                                     intent=scratch.RemovalIntentAccess.READ_ONLY)
            landed = isinstance(record, copy_record.Registered) and record.task.startswith("landed:")
            return Limit(now - wide_idle_seconds(lint_settings=lint_settings) if landed else float("inf"), TASK_OPEN)
        # systemd-tmpfiles-clean sets each folder's times back after it reads it, which moves the
        # folder's ctime, so in a temp folder only a folder's mtime counts.
        if self.is_wide(root, entry):
            return Limit(now - wide_idle_seconds(lint_settings=lint_settings), RECENT_WIDE, folder_ctime=False)
        return Limit(now - IDLE_SECONDS, RECENT, folder_ctime=root not in self.temp)

    def judged_empty(self, root: str, entry: os.DirEntry[str]) -> bool:
        """An empty folder of a wide root that this user owns, that no system tool owns (an
        EMPTY_PREFIXES name aside), and that is no other root."""
        name = entry.name
        if root not in self.wide or name in SYSTEM_TEMP_NAMES or (
                name.startswith(SYSTEM_TEMP_PREFIXES) and not name.startswith(EMPTY_PREFIXES)):
            return False
        if not holds_no_file(entry.path):
            return False
        inside = os.path.join(entry.path, "")
        if any(other == entry.path or other.startswith(inside) for other in self.all()):
            return False
        try:
            return entry.stat(follow_symlinks=False).st_uid == os.getuid()
        except OSError:
            return False


@dataclass
class Sweep:
    deleted: list[DeletedEntry] = field(default_factory=list)
    failed: list[FailedEntry] = field(default_factory=list)
    kept: dict[str, list[str]] = field(default_factory=dict)
    freed: int = 0
    dataclass_code_removed: int = 0

    def keep(self, path: str, reason: str) -> None:
        self.kept.setdefault(reason, []).append(path)


def default_roots(tmp_root: str = TMP_ROOT, run_roots: Sequence[str] | None = None,
                  session_roots: Sequence[str] | None = None, temp_roots: Sequence[str] | None = None,
                  scratch_roots: Sequence[str] | None = None, wide_roots: Sequence[str] | None = None,
                  session_temp_roots: Sequence[str] | None = None) -> Roots:
    """Each root by its real path; only the scratch and copies roots differ by host. Only the live /tmp root
    adds the dataclass code cache root. The Mac's /tmp is a link to
    /private/tmp, and lsof prints real paths. The wide root is /tmp alone: $TMPDIR and the Mac's
    temp folder hold app state, and macOS clears its own."""
    runs = tuple(str(root) for root in unit_runs.run_roots()) if run_roots is None else run_roots
    sessions_at = session_scratch_roots() if session_roots is None else session_roots
    projects = tuple(entry.path for root in sessions_at for entry in listing(os.path.realpath(root))
                     if entry.name.startswith("-") and entry.is_dir(follow_symlinks=False))
    temp = temp_folders() if temp_roots is None else temp_roots
    live = os.path.realpath(tmp_root) == os.path.realpath(TMP_ROOT)
    task_roots = ((str(copy_record.root()), str(copy_record.FORMER_ROOT)) if live else (str(copy_record.root()),)
                  ) if scratch_roots is None else scratch_roots
    temp_real = tuple(os.path.realpath(root) for root in temp)
    wide = (SYSTEM_TEMP_ROOT,) if wide_roots is None else wide_roots
    if session_temp_roots is None:
        session_parent = str(_session_temp.temp_parent()) if SESSION_TEMP_ROOT == _session_temp.ROOT else SESSION_TEMP_ROOT
        session_temp_roots = (session_parent,)
    copies = (copy_record.copies_root(), copy_record.FORMER_COPIES_ROOT) if live else (Path(tmp_root) / "copies",)
    return Roots((os.path.realpath(tmp_root),), tuple(os.path.realpath(root) for root in runs), projects,
                 temp_real, tuple(dict.fromkeys(os.path.realpath(root) for root in task_roots if os.path.isdir(root))),
                 tuple(real for root in wide if (real := os.path.realpath(root)) in temp_real),
                 tuple(os.path.realpath(root) for root in session_temp_roots if os.path.isdir(root)),
                 tuple(dict.fromkeys(os.path.realpath(root) for root in copies)),
                 (dataclass_code_root(),) if live else ())


def session_scratch_roots() -> tuple[str, ...]:
    """Each /tmp/claude-<uid> folder this user owns."""
    owned: list[str] = []
    for entry in listing(SYSTEM_TEMP_ROOT):
        if not entry.name.startswith(SESSION_SCRATCH_PREFIX) or not entry.name[len(SESSION_SCRATCH_PREFIX):].isdigit():
            continue
        try:
            if entry.is_dir(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_uid == os.getuid():
                owned.append(entry.path)
        except OSError:
            continue
    return tuple(owned)


def temp_folders() -> tuple[str, ...]:
    """/tmp, $TMPDIR and the Mac's per-user temp folder, each by its real path, once."""
    try:
        darwin = os.confstr(DARWIN_TEMP_NAME) or ""
    except (ValueError, OSError):
        darwin = ""
    candidates = (SYSTEM_TEMP_ROOT, os.environ.get("TMPDIR", ""), darwin)
    return tuple(dict.fromkeys(os.path.realpath(path) for path in candidates if path and os.path.isdir(path)))


def holds_no_file(folder: str) -> bool:
    """Whether folder holds only folders, however deep (or nothing). A link, a socket or a tree
    of more than EMPTY_TREE_FOLDERS folders holds something; links are never followed."""
    frontier = [folder]
    walked = 0
    while frontier:
        walked += 1
        if walked > EMPTY_TREE_FOLDERS:
            return False
        try:
            with os.scandir(frontier.pop()) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        return False
                    frontier.append(entry.path)
        except OSError:
            return False
    return True


def process_start(pid: int, now: float, proc: str = PROC_DIR) -> tuple[float, list[str]] | None:
    """When pid started and its arguments, from proc, else from ps (the Mac); None when it is gone
    or cannot be read."""
    if os.path.isdir(proc):
        try:
            with open(os.path.join(proc, str(pid), "cmdline"), "rb") as handle:
                arguments = handle.read().decode(errors="replace").split("\0")
            with open(os.path.join(proc, str(pid), "stat"), encoding="utf-8") as handle:
                fields = handle.read().rpartition(")")[2].split()
            with open(os.path.join(proc, "stat"), encoding="utf-8") as handle:
                boot = next(int(line.split()[1]) for line in handle if line.startswith("btime "))
            return boot + int(fields[19]) / os.sysconf("SC_CLK_TCK"), arguments
        except (OSError, ValueError, IndexError, StopIteration):
            return None
    try:
        completed = subprocess.run(["ps", "-o", "etime=", "-o", "command=", "-p", str(pid)],
                                   capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    elapsed, _, command = completed.stdout.strip().partition(" ")
    seconds = elapsed_seconds(elapsed)
    return None if seconds is None else (now - seconds, command.split())


def elapsed_seconds(elapsed: str) -> int | None:
    """ps etime, [[dd-]hh:]mm:ss, in seconds."""
    days, _, clock = elapsed.rpartition("-")
    parts = clock.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts) or (days and not days.isdigit()):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds + int(days or 0) * 86400


def verify_running(pid: int, made: float, now: float, proc: str = PROC_DIR) -> bool:
    """Whether pid is a verify.sh run that started before made, the folder's mtime: a pid reused by
    another process, or by a later run, is not the run that made the folder."""
    started = process_start(pid, now, proc)
    if started is None:
        return False
    start, arguments = started
    return any(os.path.basename(argument) == VERIFY_SCRIPT for argument in arguments) and start <= made + START_SLACK_SECONDS


def listing(directory: str, *, unreadable: Callable[[Path, OSError], None] | None = None) -> Iterator[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            yield from entries
    except FileNotFoundError:
        return
    except OSError as error:
        if unreadable is not None:
            unreadable(Path(directory), error)


def running_session_ids() -> frozenset[str] | None:
    """The id of every session whose process runs; None when a session record cannot be read, since
    that session can be live."""
    registry = sessions.read_registry()
    if registry.unreadable:
        return None
    return frozenset(entry.record["sessionId"] for entry in registry.entries
                     if sessions.running_session(entry.record))


@dataclass(frozen=True)
class ProcessUsesKnown:
    """A completed scan of process paths, including a scan with no matches."""
    paths: list[tuple[str, int, str]]
    unreadable_cwd_pids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ProcessUsesUnavailable:
    """Process use cannot be checked; candidates must stay."""


def process_paths(proc: str, prefix: str | tuple[str, ...]) -> ProcessUsesKnown | ProcessUsesUnavailable:
    """Read matching process paths and report processes with unreadable working directories.

    Ignore processes that end during the scan.
    """
    if not os.path.isdir(proc):
        return ProcessUsesUnavailable()
    found: list[tuple[str, int, str]] = []
    unreadable: list[int] = []
    for entry in listing(proc):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cwd = os.readlink(os.path.join(entry.path, "cwd"))
        except PermissionError:
            unreadable.append(pid)
            continue
        except OSError:
            continue
        if cwd.startswith(prefix):
            found.append((cwd, pid, WORKS_IN))
        fds = os.path.join(entry.path, "fd")
        for fd in listing(fds):
            try:
                target = os.readlink(fd.path)
            except OSError:
                continue
            if target.startswith(prefix):
                how = HOLDS_FILE
                try:
                    with open(os.path.join(entry.path, "fdinfo", fd.name), encoding="utf-8") as info:
                        for line in info:
                            if line.startswith("flags:") and int(line.split()[1], 8) & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR):
                                how = WRITES_FILE
                except (OSError, ValueError):
                    pass
                found.append((target.removesuffix(DELETED_SUFFIX), pid, how))
        try:
            with open(os.path.join(entry.path, "maps"), encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    parts = line.rstrip("\n").split(maxsplit=5)
                    if len(parts) == 6 and parts[5].startswith(prefix):
                        found.append((parts[5].removesuffix(DELETED_SUFFIX), pid, MAPS_FILE))
        except OSError:
            continue
    return ProcessUsesKnown(found, tuple(unreadable))


def lsof_paths(prefix: str | tuple[str, ...], *, require_complete: bool = False) -> ProcessUsesKnown | ProcessUsesUnavailable:
    """Process paths from one lsof scan; unavailable when lsof cannot run or prints nothing."""
    try:
        completed = subprocess.run(LSOF, capture_output=True, text=True, errors="replace",
                                   timeout=LSOF_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ProcessUsesUnavailable()
    # lsof exits 1 when it cannot read some process; the lines it printed still hold.
    if completed.returncode != 0 and (require_complete or completed.returncode != 1):
        return ProcessUsesUnavailable()
    return (ProcessUsesKnown(parse_lsof(completed.stdout, prefix))
            if completed.stdout else ProcessUsesUnavailable())


def parse_lsof(text: str, prefix: str | tuple[str, ...]) -> list[tuple[str, int, str]]:
    """Read each process path and its access from `lsof -F pfan` output."""
    found: list[tuple[str, int, str]] = []
    pid = 0
    how = ""
    for line in text.splitlines():
        field, value = line[:1], line[1:]
        if field == "p" and value.isdigit():
            pid = int(value)
        elif field == "f":
            how = WORKS_IN if value == LSOF_CWD else MAPS_FILE if value in LSOF_MAPPED else HOLDS_FILE
        elif field == "a" and value in ("w", "u") and how == HOLDS_FILE:
            how = WRITES_FILE
        elif field == "n" and pid and how and value.startswith(prefix):
            found.append((value, pid, how))
    return found


def entry_uses(paths: Sequence[tuple[str, int, str]], roots: Sequence[str]) -> dict[str, str]:
    """For each entry a process uses, under any root, how the first such process uses it."""
    uses: dict[str, str] = {}
    for path, pid, how in paths:
        if how == WRITES_FILE:
            how = HOLDS_FILE
        for root in roots:
            if path.startswith(root + os.sep):
                name = path[len(root) + 1:].split(os.sep, 1)[0]
                _ = uses.setdefault(os.path.join(root, name), f"pid {pid} {how}")
    return uses


def last_change(path: str, limit: Limit) -> tuple[float, int] | str:
    """The newest change inside path and its disk blocks, or why it stays: limit.recent when
    something changed at or after limit.since, UNREADABLE when a part of it could not be read. A
    folder's own times count only when it holds nothing. Symbolic links are never followed."""
    since, recent = limit.since, limit.recent
    try:
        top = os.lstat(path)
    except OSError:
        return UNREADABLE
    folder = stat.S_ISDIR(top.st_mode)
    own = max(top.st_mtime, top.st_ctime if limit.folder_ctime or not folder else 0.0)
    if not folder:
        return recent if own >= since else (own, top.st_blocks * 512)
    newest = 0.0
    total = 0
    seen: set[tuple[int, int]] = set()
    frontier = [path]
    while frontier:
        try:
            with os.scandir(frontier.pop()) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    folder = stat.S_ISDIR(info.st_mode)
                    newest = max(newest, info.st_mtime, info.st_ctime if limit.folder_ctime or not folder else 0.0)
                    if newest >= since:
                        return recent
                    if folder:
                        frontier.append(entry.path)
                    elif info.st_nlink <= 1:
                        total += info.st_blocks * 512
                    elif (info.st_dev, info.st_ino) not in seen:
                        # Only hard links need an identity retained across the scan.
                        seen.add((info.st_dev, info.st_ino))
                        total += info.st_blocks * 512
        except OSError:
            return UNREADABLE
    if not newest:
        return recent if own >= since else (own, 0)
    return newest, total


class RunKeepReason(Enum):
    OPEN_RUN = "a staff run pointer"
    UNENDED_WORKER = "a worker has not ended"
    SESSION_WORKTREE = "a live or paused session uses its worktree"
    UNREADABLE_RECORD = "a run ownership record cannot be read"
    DIRECTOR = DIRECTOR_RUNS


class MissingRecord(Enum):
    ABSENT = "record absent"


def record_object(path: Path) -> dict[str, object] | MissingRecord:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError(f"{path}: broken record link") from None
        return MissingRecord.ABSENT
    raw = cast(object, json.loads(text))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected an object")
    return cast(dict[str, object], raw)


def record_entries(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except FileNotFoundError:
        if directory.is_symlink():
            raise ValueError(f"{directory}: broken record link") from None
        return []


@dataclass(frozen=True)
class SessionRunUse:
    worktrees: frozenset[str]


def session_run_use(live: frozenset[str]) -> SessionRunUse:
    """Read live registry working directories without consulting stale shutdown state."""
    registry = sessions.read_registry()
    if registry.unreadable:
        raise ValueError("session registry unreadable")
    worktrees: set[str] = set()
    for item in registry.entries:
        cwd = cast(object, item.details.get("cwd", ""))
        if not isinstance(cwd, str) or (cwd and not os.path.isabs(cwd)):
            raise ValueError("session cwd unreadable")
        if item.record["sessionId"] in live and cwd:
            worktrees.add(os.path.realpath(cwd))
    return SessionRunUse(frozenset(worktrees))


@dataclass(frozen=True)
class PaneRunUse:
    record: staff.StaffSnapshot
    parents: frozenset[staff.StaffId]
    active: frozenset[staff.StaffId]
    threads: dict[staff.StaffId, frozenset[str]]
    ended: frozenset[staff.StaffId] = frozenset()


@dataclass(frozen=True)
class PaneRunsUnreadable:
    reason: str


class ReadPaneRuns(Enum):
    CURRENT = "read the current pane workers"


def read_pane_runs() -> PaneRunUse | PaneRunsUnreadable:
    from scripts.production import staff

    try:
        return pane_run_use()
    except (staff.StaffUnreadable, OSError, ValueError) as error:
        return PaneRunsUnreadable(str(error))


def pane_run_use() -> PaneRunUse:
    from scripts.production import staff, session_staff

    record = staff.snapshot()
    parents = session_staff.pane_parents(record)
    active: set[staff.StaffId] = set()
    ended: set[staff.StaffId] = set()
    threads: dict[staff.StaffId, frozenset[str]] = {}
    if parents:
        from scripts.herdr import worker_panes

        for parent, observed in worker_panes.observe_parents(parents, record).items():
            identities: set[str] = set()
            if observed and all(isinstance(worker, (worker_panes.StoppedWorker, worker_panes.ClosedWorker)) for worker in observed):
                ended.add(parent)
            for worker in observed:
                if isinstance(worker, (worker_panes.OpenWorker, worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                    active.add(parent)
                    identities.add(worker.row.current_session)
                if isinstance(worker, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                    state = "unknown" if isinstance(worker, worker_panes.ProcessUnknownWorker) else "incomplete"
                    print(f"tmp sweep: {worker.row.name}: {state}: {worker.reason}", file=sys.stderr)
            threads[parent] = frozenset(identities)
    return PaneRunUse(record, frozenset(row.id for row in parents), frozenset(active), threads, frozenset(ended))


def run_pane_owner(folder: Path, use: PaneRunUse) -> staff.StaffRow | unit_runs.RunOwnerUnrecorded:
    from scripts.production import staff

    state = record_object(folder / unit_runs.STATE_FILE)
    owner = unit_runs.run_owner(state, use.record) if isinstance(state, dict) else unit_runs.RunOwnerUnrecorded("run has no unit staff owner")
    if isinstance(owner, staff.StaffRow):
        location = owner.worktree
        if isinstance(location, (staff.PresentWorktree, staff.RemovedWorktree)) and location.machine != remote.local_machine():
            raise staff.StaffUnreadable(f"{folder}: {owner.name}: pane owner is on {location.machine}")
    return owner


def run_folder_guards(roots: Roots, live: frozenset[str] | None,
                      workers: PaneRunUse) -> dict[str, RunKeepReason]:
    from scripts.production import staff

    folders = [entry.path for root in roots.runs for entry in listing(root)
               if entry.is_dir(follow_symlinks=False)]
    try:
        if live is None:
            raise ValueError("session registry unreadable")
        use = session_run_use(live)
        opened = {os.path.realpath(row.run.path) for row in workers.record.rows
                  if isinstance(row.run, staff.OpenRun)}
    except (OSError, UnicodeError, ValueError):
        return dict.fromkeys(folders, RunKeepReason.UNREADABLE_RECORD)
    kept: dict[str, RunKeepReason] = {}
    for folder in folders:
        try:
            pane_owner = run_pane_owner(Path(folder), workers)
            if isinstance(pane_owner, staff.StaffRow) and pane_owner.id in workers.active:
                kept[folder] = RunKeepReason.UNENDED_WORKER
            # Read even when another reason keeps the folder, so errors are visible.
            state = record_object(Path(folder) / unit_runs.STATE_FILE)
            if not isinstance(state, MissingRecord):
                if state.get(unit_runs.OWNER_FIELD):
                    owner = unit_runs.run_owner(state, workers.record)
                    if isinstance(owner, staff.StaffRow) and isinstance(owner.worktree, staff.PresentWorktree):
                        state = {**state, "working_dir": str(owner.worktree.path)}
                working = state.get("working_dir", "")
                if not isinstance(working, str):
                    raise ValueError("run worktree unreadable")
                if (working and os.path.realpath(working) in use.worktrees
                        and kept.get(folder) is not RunKeepReason.UNENDED_WORKER):
                    kept[folder] = RunKeepReason.SESSION_WORKTREE
            if os.path.realpath(folder) in opened:
                kept[folder] = RunKeepReason.OPEN_RUN
            if folder not in kept:
                director = unit_runs.director(Path(folder), record=workers.record)
                if director in live:
                    kept[folder] = RunKeepReason.DIRECTOR
            if not isinstance(state, MissingRecord) and state.get("history_file"):
                history = state["history_file"]
                if not isinstance(history, str):
                    raise ValueError("run history path unreadable")
                with Path(history).open(encoding="utf-8") as handle:
                    event = cast(object, json.loads(handle.readline()))
                if not isinstance(event, dict):
                    raise ValueError("run history unreadable")
        except (staff.StaffUnreadable, OSError, UnicodeError, ValueError) as error:
            print(f"tmp sweep: {folder}: {error}", file=sys.stderr)
            kept[folder] = RunKeepReason.UNREADABLE_RECORD
    return kept


@dataclass(frozen=True)
class WorkerUse:
    folders: frozenset[str]
    threads: frozenset[str]
    unreadable_records: tuple[str, ...]


def worker_use(guarded: dict[str, RunKeepReason], use: PaneRunUse) -> WorkerUse:
    """Read working folders and live thread ids only from runs that must stay."""
    from scripts.production import staff

    folders: set[str] = set()
    threads: set[str] = set()
    unreadable: list[str] = []

    for folder in guarded:
        state_path = Path(folder) / unit_runs.STATE_FILE
        try:
            owner = run_pane_owner(Path(folder), use)
            if isinstance(owner, staff.StaffRow):
                threads.update(use.threads.get(owner.id, ()))
        except (staff.StaffUnreadable, OSError, UnicodeError, ValueError) as error:
            print(f"tmp sweep: {state_path}: {error}", file=sys.stderr)
            unreadable.append(str(state_path))
        runs_path = Path(folder) / "detached_runs"
        try:
            for path in record_entries(runs_path):
                if path.suffix != ".json":
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                    if not text:
                        continue
                    record = cast(object, json.loads(text))
                    if not isinstance(record, dict):
                        continue
                    fields = cast(dict[str, object], record)
                    if fields.get("status") != "running":
                        continue
                    cwd = fields.get("cwd", "")
                    if not isinstance(cwd, str) or (cwd and not os.path.isabs(cwd)):
                        raise ValueError("worker working folder unreadable")
                    if cwd:
                        folders.add(os.path.realpath(cwd))
                except (OSError, UnicodeError, ValueError):
                    unreadable.append(str(path))
        except (OSError, UnicodeError, ValueError):
            unreadable.append(str(runs_path))
    return WorkerUse(frozenset(folders), frozenset(threads), tuple(unreadable))


class GitWork(Enum):
    SAVED = "all git work is saved"
    UNSAVED = UNSAVED_WORK
    UNREADABLE = UNREADABLE


def checked_git(repo: Path, *arguments: str) -> str:
    """Read git state without optional locks or a network request."""
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True,
                            check=False, timeout=GIT_TIMEOUT_SECONDS,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"})
    if result.returncode:
        raise ValueError(f"{repo}: git {arguments[0]} failed")
    return result.stdout


def repo_git_work(repo: Path) -> GitWork:
    if checked_git(repo, "status", "--porcelain", "--untracked-files=all").strip():
        return GitWork.UNSAVED
    refs = checked_git(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()
    linked = (repo / ".git").is_file()
    if not linked and any(line.split(" ", 1)[0] == "refs/stash" for line in refs):
        return GitWork.UNSAVED
    # An empty repository has no commit to lose. A broken HEAD fails closed below.
    head = checked_git(repo, "rev-parse", "--git-path", "HEAD").strip()
    head_path = Path(head) if os.path.isabs(head) else repo / head
    head_text = head_path.read_text(encoding="utf-8").strip()
    if not refs and head_text.startswith("ref: "):
        return GitWork.SAVED
    sources: list[str] = []
    source_tags: set[str] = set()
    for remote in checked_git(repo, "remote").splitlines():
        url = checked_git(repo, "remote", "get-url", remote).strip()
        if url.startswith("file://"):
            parsed = urlparse(url)
            if parsed.netloc not in ("", "localhost"):
                continue
            url = unquote(parsed.path)
        elif ":" in url and not os.path.isabs(url):
            continue
        source = Path(url) if os.path.isabs(url) else repo / url
        sources.extend(checked_git(source, "for-each-ref", "--format=%(objectname)", "refs/heads/").splitlines())
        source_tags.update(checked_git(source, "for-each-ref", "--format=%(refname) %(objectname)", "refs/tags/").splitlines())
    # A tag that a local source holds with the same object is the source's copy, not work of the clone.
    own_tags = [line.split(" ", 1)[0] for line in refs
                if line.startswith("refs/tags/") and line not in source_tags]
    tips = ["HEAD"] if linked else ["HEAD", "--branches", *own_tags]
    remaining = checked_git(repo, "-c", "core.alternateRefsPrefixes=refs/heads/", "rev-list", "-n1",
                            *tips, "--not", "--remotes", "--alternate-refs", *sources)
    return GitWork.UNSAVED if remaining.strip() else GitWork.SAVED


def git_work(path: Path, depth: int = GIT_DEPTH, *, verified_repos: tuple[Path, ...] = ()) -> GitWork:
    """Check all worktrees down to a fixed depth. Never follow links."""
    try:
        if path.is_symlink() or not path.is_dir():
            return GitWork.SAVED
        if (path / ".git").is_symlink():
            return GitWork.UNREADABLE
        if os.path.lexists(path / ".git") and path not in verified_repos:
            result = repo_git_work(path)
            if result is not GitWork.SAVED:
                return result
        if depth:
            for entry in path.iterdir():
                if entry.name == ".git":
                    continue
                result = git_work(entry, depth - 1, verified_repos=verified_repos)
                if result is not GitWork.SAVED:
                    return result
        return GitWork.SAVED
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        return GitWork.UNREADABLE


def when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, user_zone()).strftime("%Y-%m-%d %H:%M")


def owner_reason(entry: os.DirEntry[str], root: str, roots: Roots, live: frozenset[str] | None,
                 *,
    record: staff.StaffSnapshot | PaneRunsUnreadable,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str | None:
    """Why an entry stays whatever its age: it is a root, or a running session
    owns it by the entry's own record."""
    if entry.path in roots.all():
        return ROOT
    if root in roots.scratch:
        task = scratch.task_of(Path(entry.path), scratch_settings=scratch_settings,
                               intent=scratch.RemovalIntentAccess.READ_ONLY)
        if isinstance(task, copy_record.UnreadableRecord):
            return task.reason
        return ROW_WAITS if isinstance(task, copy_record.Registered) and task.task.startswith("row:") else None
    if root in roots.projects or root in roots.sessions:
        if live is None:
            return NO_SESSIONS
        return SESSION_RUNS if entry.name in live else None
    if root in roots.runs and entry.is_dir(follow_symlinks=False):
        if isinstance(record, PaneRunsUnreadable):
            return UNREADABLE
        director = unit_runs.director(Path(entry.path), record=record)
        if director and live is None:
            return NO_SESSIONS
        return DIRECTOR_RUNS if live is not None and director in live else None
    return None


def session_folders(roots: Roots, live: frozenset[str] | None) -> tuple[str, ...]:
    """Each folder inside a running session's folder (scratchpad/, tasks/): the session folder stays,
    and each entry of SESSION_ENTRY_MIN_BYTES or more in these is judged like any other."""
    if live is None:
        return ()
    return tuple(inner.path for project in roots.projects for session in listing(project)
                 if session.name in live and session.is_dir(follow_symlinks=False)
                 for inner in listing(session.path) if inner.is_dir(follow_symlinks=False))


def temp_parents(roots: Roots, live: frozenset[str] | None) -> tuple[str, ...]:
    """Each running session's temp root and each run folder's temp folder: the parent stays, and
    every entry in it is judged with the 4 h rule. Sessions run for weeks, so these entries are what
    goes; the parent goes only once its session or director stops."""
    sessions = () if live is None else tuple(
        entry.path for root in roots.sessions for entry in listing(root)
        if entry.name in live and entry.is_dir(follow_symlinks=False))
    runs = tuple(temp for root in roots.runs for entry in listing(root)
                 if entry.is_dir(follow_symlinks=False) and os.path.isdir(temp := os.path.join(entry.path, RUN_TEMP_NAME))
                 and not os.path.islink(temp))
    return sessions + runs


@dataclass(frozen=True)
class KeptEntry:
    path: str
    reason: str
    use: str = ""


@dataclass(frozen=True)
class RemovableEntry:
    path: str
    root: str
    newest: float
    size: int
    reason: str = "idle and unused"
    maker: removal_guard.ManualReviewEvidence = removal_guard.UnknownMaker.NO_EVIDENCE


class ClosedPhaseEntry(RemovableEntry):
    """The phase is closed under a director or session worktree guard."""


def judge_entries(roots: Roots, now: float, proc: str, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    pane_use: PaneRunUse | PaneRunsUnreadable | ReadPaneRuns = ReadPaneRuns.CURRENT,
    inspection: scratch.RemoteInspection = scratch.RemoteInspection.LOCAL_ONLY,
    unreadable_copies: Callable[[Path, OSError], None] = report_unreadable_copy_folder,
) -> Iterator[KeptEntry | RemovableEntry]:
    """Apply the ownership, use and idle checks shared by timed and hand cleanup."""
    from scripts.production import staff

    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    if not roots.all():
        return
    # The scan keeps only paths under the roots' common folder (/tmp), which entry_uses then sorts.
    prefix = os.path.join(os.path.commonpath(roots.all()), "")
    paths = process_paths(proc, prefix)
    if isinstance(paths, ProcessUsesUnavailable):
        paths = lsof_paths(prefix)
    if isinstance(paths, ProcessUsesUnavailable):
        for root in roots.all():
            for entry in roots.entries(root, unreadable_copies=unreadable_copies):
                yield KeptEntry(entry.path, NO_PROC)
        return
    live = running_session_ids()
    inner = session_folders(roots, live)
    try:
        pane_use = read_pane_runs() if isinstance(pane_use, ReadPaneRuns) else pane_use
        if isinstance(pane_use, PaneRunsUnreadable):
            raise staff.StaffUnreadable(pane_use.reason)
        guarded = run_folder_guards(roots, live, pane_use)
        workers = worker_use(guarded, pane_use)
    except (staff.StaffUnreadable, OSError, UnicodeError, ValueError) as error:
        print(f"tmp sweep: {error}", file=sys.stderr)
        pane_use = PaneRunsUnreadable(str(error))
        guarded = {entry.path: RunKeepReason.UNREADABLE_RECORD for root in roots.runs
                   for entry in listing(root)}
        workers = WorkerUse(frozenset(), frozenset(), (str(staff.record_path()),))
    for path in workers.unreadable_records:
        print(f"tmp sweep: {path}: {UNREADABLE}; keeping scratch and top-level /tmp/claude entries", file=sys.stderr)
    temp_live = None if live is None else live | workers.threads
    phase_parents: dict[str, list[str]] = {}
    unreadable_phases: set[str] = set()
    if live is not None:
        for folder, guard in guarded.items():
            if guard not in (RunKeepReason.SESSION_WORKTREE, RunKeepReason.DIRECTOR):
                continue
            phases = Path(folder) / "phases"
            if not phases.is_dir() or phases.is_symlink():
                continue
            try:
                phase_parents[str(phases)] = [phase_reservation.phase_directory_name(phase)
                                             for phase in recorder_state.read_open_phase_ids(Path(folder))]
            except (OSError, ValueError):
                phase_parents[str(phases)] = []
                unreadable_phases.add(str(phases))
    judged = roots.all() + inner + temp_parents(roots, temp_live) + tuple(phase_parents)
    pairs = tuple(str(pair) for root in roots.copies
                  for pair in scratch.legacy_copy_pairs(Path(root), unreadable=unreadable_copies))
    uses = entry_uses(paths.paths, judged + pairs)
    for root in judged:
        for entry in roots.entries(root, unreadable_copies=unreadable_copies):
            reason = owner_reason(entry, root, roots, temp_live,
                                  record=pane_use.record if isinstance(pane_use, PaneRunUse) else pane_use,
                                  scratch_settings=scratch_settings)
            landed_remote_check = False
            if root in roots.copies and reason is None:
                state = scratch.lifetime(Path(entry.path), now, scratch_settings=scratch_settings,
                                         intent=scratch.RemovalIntentAccess.READ_ONLY)
                landed_remote_check = isinstance(state, scratch.LandedRemoteCheckNeeded) and inspection is scratch.RemoteInspection.ADVERTISE
                if isinstance(state, scratch.Live) and not landed_remote_check:
                    reason = state.why
                elif isinstance(state, scratch.Unknown):
                    reason = state.reason
            if root in unreadable_phases:
                reason = UNREADABLE
            elif entry.name in phase_parents.get(root, []):
                reason = PHASE_OPEN
            # Eligible run entries still use the shared age, process and git checks.
            if reason != ROOT:
                # Resolve only the parents: a link inside a protected run is judged where it lies.
                path = os.path.join(os.path.realpath(os.path.dirname(entry.path)), entry.name)
                for folder, guard in guarded.items():
                    if (live is not None and (
                            root == os.path.join(folder, RUN_TEMP_NAME) and guard is not RunKeepReason.UNREADABLE_RECORD
                            or root == os.path.join(folder, "phases") and root in phase_parents)):
                        continue
                    if path == folder or path.startswith(folder + os.sep) or folder.startswith(path + os.sep):
                        reason = NO_SESSIONS if live is None else guard.value
                        break
                if any(path == folder or folder.startswith(path + os.sep) for folder in workers.folders):
                    reason = WORKER_USES
                elif workers.unreadable_records and root in roots.scratch + roots.plain and reason is None:
                    reason = UNREADABLE
            if reason is None and entry.path in uses:
                yield KeptEntry(entry.path, IN_USE, uses[entry.path])
                continue
            change = last_change(entry.path, roots.limit(root, entry, now, proc, lint_settings=lint_settings, scratch_settings=scratch_settings)) if reason is None else reason
            if isinstance(change, str):
                yield KeptEntry(entry.path, change)
                continue
            newest, size = change
            if root in inner and size < SESSION_ENTRY_MIN_BYTES:
                yield KeptEntry(entry.path, SMALL_IN_LIVE_SESSION)
                continue
            path = Path(entry.path)
            if landed_remote_check:
                with scratch.judgment_lock(path) as idle:
                    if not idle:
                        yield KeptEntry(entry.path, IN_USE)
                        continue
                    work = scratch.check_removal_work(
                        path, scratch.ExpiredFolderRemoval(now), scratch_settings,
                        publication=scratch.RemovalAdvertisements(path, scratch_settings),
                        check=scratch.RemovalWorkCheck.LANDED,
                    )
                if isinstance(work, scratch.KeptFolder):
                    yield KeptEntry(entry.path, work.reason)
                    continue
            pending = root in roots.copies and isinstance(scratch.checked_removal(path), copy_record.Registered)
            git = (GitWork.SAVED if landed_remote_check or pending
                   or scratch.is_throwaway(path, intent=scratch.RemovalIntentAccess.READ_ONLY)
                   else git_work(path))
            if git is not GitWork.SAVED:
                yield KeptEntry(entry.path, git.value)
                continue
            if root in phase_parents:
                yield ClosedPhaseEntry(entry.path, root, newest, size, CleanupEndReason.CLOSED_PHASE.value)
                continue
            yield RemovableEntry(entry.path, root, newest, size)


def remove_entry(entry: RemovableEntry, result: Sweep, dry_run: bool, *,
    plan: removal_guard.RemovalList | removal_guard.ListMode = removal_guard.ListMode.PRINT_ONLY,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> bool:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    return remove_idle_entry(entry, result, dry_run, plan=plan, scratch_settings=scratch_settings)


def remove_idle_entry(entry: RemovableEntry, result: Sweep, dry_run: bool, *,
    plan: removal_guard.RemovalList | removal_guard.ListMode = removal_guard.ListMode.PRINT_ONLY,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> bool:
    """Keep this entry for manual review."""
    _ = scratch_settings
    path = Path(entry.path)
    if isinstance(entry, ClosedPhaseEntry) and (path.parent.is_symlink() or path.parent.resolve() != Path(entry.root)):
        result.keep(entry.path, "its phases parent changed")
        return False
    with scratch.judgment_lock(path) as idle:
        if not idle:
            result.keep(entry.path, IN_USE)
        else:
            if not dry_run and isinstance(plan, removal_guard.RemovalList):
                _ = plan.add_manual(removal_guard.Tool.TEMPORARY, path, Path(entry.root), entry.size, entry.reason)
            result.keep(entry.path, "manual review")
    return False


def live_entry_use(path: Path) -> str:
    """Return the process that uses an entry, or a reason the scan failed."""
    root = str(path.resolve())
    paths = process_paths(PROC_DIR, (root,))
    if isinstance(paths, ProcessUsesUnavailable):
        paths = lsof_paths((root,), require_complete=True)
    if isinstance(paths, ProcessUsesUnavailable):
        return NO_PROC
    return next((f"pid {pid} {how} {name}" for name, pid, how in paths.paths
                 if Path(name).is_relative_to(root) and (pid != os.getpid() or how == WORKS_IN)), "")


def sweep(dry_run: bool, roots: Roots, now: float, proc: str = PROC_DIR, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    plan: removal_guard.RemovalList | removal_guard.ListMode = removal_guard.ListMode.PRINT_ONLY,
) -> Sweep:
    """Assess idle entries without removing them."""
    result = Sweep()
    unreadable_paths: set[Path] = set()

    def unreadable_copies(path: Path, error: OSError) -> None:
        if path not in unreadable_paths:
            unreadable_paths.add(path)
            result.failed.append(FailedEntry(path=str(path), error=str(error)))

    pane_use = read_pane_runs()
    inspection = scratch.RemoteInspection.LOCAL_ONLY if dry_run else scratch.RemoteInspection.ADVERTISE
    for entry in timed_entries(roots, now, proc, lint_settings=lint_settings, scratch_settings=scratch_settings,
                               pane_use=pane_use, inspection=inspection, unreadable_copies=unreadable_copies):
        if isinstance(entry, KeptEntry):
            path = f"{entry.path}: {entry.use}" if entry.use else entry.path
            result.keep(path, entry.reason)
            continue
        path = Path(entry.path)
        try:
            with scratch.judgment_lock(path) as idle:
                if not idle:
                    result.keep(entry.path, IN_USE)
                    continue
                if isinstance(plan, removal_guard.RemovalList):
                    maker = (removal_guard.review_evidence(path)
                             if isinstance(entry.maker, removal_guard.UnknownMaker) else entry.maker)
                    _ = plan.add_manual(removal_guard.Tool.TEMPORARY, path, Path(entry.root), entry.size,
                                        entry.reason, maker)
        except OSError as error:
            result.failed.append(FailedEntry(path=entry.path, error=str(error)))
            continue
        result.freed += entry.size
        result.deleted.append(DeletedEntry(path=entry.path, gib=round(entry.size / GIB, 3),
                                           last_change=when(entry.newest), reason=entry.reason))
    return result


def prune_dataclass_code(root: str, now: float, days: int, result: Sweep, *,
                         plan: removal_guard.RemovalList | removal_guard.ListMode = removal_guard.ListMode.PRINT_ONLY) -> int:
    """List expired dataclass code entries."""
    if not dataclass_code_usable(root, os.getuid()):
        return 0
    count = 0
    for tag in listing(root):
        if not tag.is_dir(follow_symlinks=False) or not re.fullmatch(r"cpython-[0-9]+[a-z]*", tag.name):
            continue
        for entry in listing(tag.path):
            try:
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_mtime >= now - days * 86400:
                    continue
                if isinstance(plan, removal_guard.RemovalList):
                    _ = plan.add_manual(removal_guard.Tool.DATACLASS, Path(entry.path), Path(root),
                                        info.st_blocks * 512, "expired dataclass code")
                count += 1
            except OSError as error:
                result.failed.append(FailedEntry(path=entry.path, error=str(error)))
    return count


class CleanupEndReason(Enum):
    CLOSED_PHASE = "its phase is closed and its run has no open run pointer; idle and unused"
    WORKERS_ENDED = "all pane workers stopped or closed; idle and unused"
    TASK_ENDED = "its scratch task ended; idle and unused"
    VERIFY_STOPPED = "its recorded verify.sh run stopped; idle and unused"


class CleanupKeepReason(Enum):
    NO_END = "no recorded end permits hand cleanup"
    UNREADABLE = "its end record cannot be read"


def cleanup_end_reason(entry: RemovableEntry, roots: Roots, now: float, proc: str, *,
    pane_use: PaneRunUse | PaneRunsUnreadable,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> CleanupEndReason | CleanupKeepReason:
    """Require end evidence as well as the timed sweep's checks."""
    from scripts.production import staff

    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if isinstance(entry, ClosedPhaseEntry):
        return CleanupEndReason.CLOSED_PHASE
    try:
        for root in roots.runs:
            relative = Path(entry.path).relative_to(root) if Path(entry.path).is_relative_to(root) else Path()
            if relative.parts:
                run = Path(root) / relative.parts[0]
                if not removal_guard.is_real_directory(run):
                    continue
                if isinstance(pane_use, PaneRunsUnreadable):
                    return CleanupKeepReason.UNREADABLE
                owner = run_pane_owner(run, pane_use)
                if isinstance(owner, staff.StaffRow) and owner.id in pane_use.ended:
                    return CleanupEndReason.WORKERS_ENDED
        if entry.root in roots.scratch:
            record = scratch.task_of(Path(entry.path), scratch_settings=scratch_settings,
                                     intent=scratch.RemovalIntentAccess.READ_ONLY)
            if isinstance(record, copy_record.Registered) and scratch.ended(record.task, now):
                return CleanupEndReason.TASK_ENDED
        if entry.root in roots.copies:
            lifetime = scratch.lifetime(Path(entry.path), now, scratch_settings=scratch_settings,
                                        intent=scratch.RemovalIntentAccess.READ_ONLY)
            if isinstance(lifetime, scratch.Ended):
                return CleanupEndReason.TASK_ENDED
        verify = roots.verify_temp_name.fullmatch(Path(entry.path).name) if entry.root in roots.temp else None
        if verify is not None and verify.group(1):
            made = os.stat(entry.path, follow_symlinks=False).st_mtime
            if not verify_running(int(verify.group(1)), made, now, proc):
                return CleanupEndReason.VERIFY_STOPPED
    except (OSError, UnicodeError, ValueError):
        return CleanupKeepReason.UNREADABLE
    return CleanupKeepReason.NO_END


def timed_entries(roots: Roots, now: float, proc: str, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    pane_use: PaneRunUse | PaneRunsUnreadable | ReadPaneRuns = ReadPaneRuns.CURRENT,
    inspection: scratch.RemoteInspection = scratch.RemoteInspection.LOCAL_ONLY,
    unreadable_copies: Callable[[Path, OSError], None] = report_unreadable_copy_folder,
) -> Iterator[KeptEntry | RemovableEntry]:
    """Keep unrecorded work until the configured folder removal age after the other checks pass."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    pane_use = read_pane_runs() if isinstance(pane_use, ReadPaneRuns) else pane_use
    for entry in judge_entries(roots, now, proc, lint_settings=lint_settings, scratch_settings=scratch_settings,
                               pane_use=pane_use, inspection=inspection, unreadable_copies=unreadable_copies):
        if isinstance(entry, KeptEntry) or entry.root not in roots.plain + roots.runs:
            yield entry
            continue
        name = Path(entry.path).name
        if any(pattern.fullmatch(name) for pattern in (TEMPFILE_NAME, INVOKE_TEMP_NAME, roots.verify_temp_name)):
            yield entry
            continue
        ended = cleanup_end_reason(entry, roots, now, proc, pane_use=pane_use, scratch_settings=scratch_settings)
        if isinstance(ended, CleanupEndReason):
            yield replace(entry, reason=ended.value)
            continue
        if ended is CleanupKeepReason.UNREADABLE:
            yield KeptEntry(entry.path, ended.value)
            continue
        change = last_change(entry.path, Limit(now - wide_idle_seconds(lint_settings=lint_settings), NO_END_RECENT))
        if isinstance(change, str):
            yield KeptEntry(entry.path, change)
            continue
        newest, size = change
        yield replace(entry, newest=newest, size=size, reason=NO_END_OLD)


class PlanEntry(TypedDict):
    path: str
    action: str
    reason: str


@dataclass(frozen=True)
class CleanupRemoval:
    entry: RemovableEntry
    reason: CleanupEndReason


def cleanup_entries(roots: Roots, now: float, proc: str, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[KeptEntry | CleanupRemoval]:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    pane_use = read_pane_runs()
    result: list[KeptEntry | CleanupRemoval] = []
    for entry in judge_entries(roots, now, proc, lint_settings=lint_settings, scratch_settings=scratch_settings,
                               pane_use=pane_use):
        if isinstance(entry, KeptEntry):
            result.append(entry)
            continue
        reason = cleanup_end_reason(entry, roots, now, proc, pane_use=pane_use, scratch_settings=scratch_settings)
        if isinstance(reason, CleanupKeepReason):
            result.append(KeptEntry(entry.path, reason.value))
        else:
            result.append(CleanupRemoval(entry, reason))
    return result


def write_cleanup_plan(plan: Path, roots: Roots, now: float, proc: str, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> int:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    rows: list[PlanEntry] = []
    for entry in cleanup_entries(roots, now, proc, lint_settings=lint_settings, scratch_settings=scratch_settings):
        action = "keep" if isinstance(entry, KeptEntry) else "remove"
        reason = entry.reason if isinstance(entry, KeptEntry) else entry.reason.value
        path = entry.path if isinstance(entry, KeptEntry) else entry.entry.path
        rows.append(PlanEntry(path=path, action=action, reason=reason))
    # Preserve an existing diagnostic plan.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=plan.parent,
                                     prefix=f".{plan.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            _ = handle.write(json.dumps({"version": 1, "entries": rows}, indent=2) + "\n")
            handle.close()
            os.link(temporary, plan)
        except FileExistsError:
            raise FileExistsError(f"plan {plan} already exists; inspect it and remove it before making a new plan") from None
        finally:
            temporary.unlink(missing_ok=True)
    for row in rows:
        print(f"{row['action']} {row['path']}: {row['reason']}")
    print(f"tmp sweep: plan {plan}")
    return 0


def planned_removals(plan: Path) -> list[str]:
    record = record_object(plan)
    if not isinstance(record, dict) or record.get("version") != 1 or not isinstance(record.get("entries"), list):
        raise ValueError("invalid cleanup plan")
    paths: list[str] = []
    for value in cast(list[object], record["entries"]):
        if not isinstance(value, dict):
            raise ValueError("invalid cleanup plan entry")
        row = cast(dict[str, object], value)
        path, action = row.get("path"), row.get("action")
        if not isinstance(path, str) or not os.path.isabs(path) or action not in ("keep", "remove"):
            raise ValueError("invalid cleanup plan path or action")
        if action == "remove" and path not in paths:
            paths.append(path)
    return paths


def wide_idle_seconds(*, lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> int:
    """Read the shared removal age for ordinary folders."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    return lint_settings.whole("folder_remove_after_seconds")


def state_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get(STATE_ENV) or DEFAULT_STATE_DIR))


def record(result: Sweep, dry_run: bool, now: float, *,
           lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> RunRecord:
    """The run's log line: every count, and the first lint.conf tmp_sweep_log_sample paths of each listed reason."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    sample = lint_settings.whole("tmp_sweep_log_sample")
    return RunRecord(
        at=datetime.fromtimestamp(now, user_zone()).isoformat(timespec="seconds"),
        dry_run=dry_run,
        freed_gib=round(result.freed / GIB, 3),
        deleted=result.deleted,
        failed=result.failed,
        kept={reason: len(paths) for reason, paths in sorted(result.kept.items())},
        kept_paths={reason: sorted(paths)[:sample] for reason, paths in sorted(result.kept.items())
                    if reason not in UNLISTED_REASONS},
    )


def append_log(path: Path, entry: RunRecord, now: float, *,
               lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> None:
    """Add entry to the log. Drop each line older than LOG_KEEP_SECONDS, then the oldest lines
    while the log is larger than lint.conf tmp_sweep_log_max_mib."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    limit = lint_settings.whole("tmp_sweep_log_max_mib") << 20
    added = (json.dumps(entry, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The first pass measures the lines that stay by age; the second writes the newest that fit.
    sizes: list[int] = []
    try:
        with path.open("rb") as source:
            sizes = [len(line.rstrip(b"\n")) + 1 if logged_at(line.decode(errors="replace")) >= now - LOG_KEEP_SECONDS else 0
                     for line in source]
    except FileNotFoundError:
        pass
    total, first = len(added), len(sizes)
    while first > 0 and total + sizes[first - 1] <= limit:
        first -= 1
        total += sizes[first]
    staged = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        with staged.open("wb") as output:
            if first < len(sizes):
                with path.open("rb") as source:
                    for number, line in enumerate(source):
                        if number >= first and sizes[number]:
                            _ = output.write(line.rstrip(b"\n") + b"\n")
            _ = output.write(added)
        _ = staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def logged_at(line: str) -> float:
    """A log line's time, or 0 for a line that is not a run record."""
    try:
        parsed = cast(object, json.loads(line))
    except json.JSONDecodeError:
        return 0.0
    if not isinstance(parsed, dict):
        return 0.0
    at = cast(dict[str, object], parsed).get("at")
    try:
        return datetime.fromisoformat(at).timestamp() if isinstance(at, str) else 0.0
    except ValueError:
        return 0.0


def summary(result: Sweep, dry_run: bool) -> str:
    verb = "review" if dry_run else "deleted"
    kept = "; ".join(f"{reason} {len(paths)}" for reason, paths in sorted(result.kept.items()))
    return (f"tmp sweep: {verb} {len(result.deleted)} entries ({result.freed / GIB:.1f} GiB),"
            + f" kept {sum(len(paths) for paths in result.kept.values())}: {kept or 'none'}")


def remove_expired_nextest_logs(root: Path, now: float, dry_run: bool, result: Sweep, *,
                               plan: removal_guard.RemovalList | removal_guard.ListMode = removal_guard.ListMode.PRINT_ONLY) -> None:
    """List expired Mac nextest logs for review."""
    _ = dry_run
    try:
        folder = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError as error:
        result.failed.append(FailedEntry(path=str(root), error=str(error)))
        return
    try:
        if os.fstat(folder).st_uid != os.getuid():
            return
        days = nextest_log.retention_days()
        cutoff = now - days * 24 * HOUR_SECONDS
        expired: dict[str, os.stat_result] = {}
        with os.scandir(folder) as entries:
            for entry in entries:
                if not nextest_log.LOG_NAME.fullmatch(entry.name):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                        and info.st_nlink == 1 and info.st_mtime < cutoff):
                    expired[entry.name] = info
        if not expired:
            return
        resolved = os.path.realpath(root)
        paths = lsof_paths(resolved + os.sep, require_complete=True)
        if not os.path.samestat(os.fstat(folder), os.stat(resolved, follow_symlinks=False)):
            return
        used = entry_uses(paths.paths, (resolved,)) if isinstance(paths, ProcessUsesKnown) else {}
        for name, info in expired.items():
            path = str(root / name)
            if isinstance(paths, ProcessUsesUnavailable):
                result.keep(path, NO_PROC)
                continue
            if os.path.join(resolved, name) in used:
                result.keep(path, IN_USE)
                continue
            try:
                with nextest_log.locked_log(folder, name, nextest_log.LogAccess.REMOVE) as descriptor:
                    current = os.fstat(descriptor)
                    if current != info or current.st_ctime_ns != info.st_ctime_ns:
                        result.keep(path, "the nextest log changed during the process scan")
                        continue
                    if isinstance(plan, removal_guard.RemovalList):
                        _ = plan.add_manual(removal_guard.Tool.NEXTEST, Path(path), root,
                                            info.st_blocks * 512, "expired Mac nextest log")
            except BlockingIOError:
                result.keep(path, IN_USE)
                continue
            except FileNotFoundError:
                continue
            except OSError as error:
                result.failed.append(FailedEntry(path=path, error=str(error)))
                continue
            size = info.st_blocks * 512
            result.freed += size
            result.deleted.append(DeletedEntry(path=path, gib=round(size / GIB, 3),
                last_change=when(info.st_mtime), reason="expired Mac nextest log"))
    except (OSError, conf.Unusable) as error:
        result.failed.append(FailedEntry(path=str(root), error=str(error)))
    finally:
        os.close(folder)


def run(dry_run: bool, now: float | None = None, roots: Roots | None = None, proc: str = PROC_DIR, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> int:
    """Print the assessment and publish the removal list."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    moment = time.time() if now is None else now
    roots = roots or default_roots()
    plan = removal_guard.ListMode.PRINT_ONLY if dry_run else removal_guard.RemovalList(removal_guard.Tool.TEMPORARY)
    result = Sweep()
    try:
        result = sweep(dry_run, roots, moment, proc, lint_settings=lint_settings,
                       scratch_settings=scratch_settings, plan=plan)
        for root in roots.dataclass_code:
            count = prune_dataclass_code(root, moment, lint_settings.whole("dataclass_code_days"), result, plan=plan)
            if count:
                print(f"tmp sweep: {count} expired dataclass code entries")
        if sys.platform == "darwin":
            remove_expired_nextest_logs(Path.home() / nextest_log.DIRECTORY, moment, dry_run, result, plan=plan)
    except (OSError, conf.Unusable, removal_guard.Refused) as error:
        result.failed.append(FailedEntry(path="temporary sweep", error=str(error)))
    if isinstance(plan, removal_guard.RemovalList):
        try:
            plan.report()
        except OSError as error:
            result.failed.append(FailedEntry(path="removal list", error=str(error)))
    for entry in result.deleted:
        print(f"tmp sweep: review {entry['path']} ({entry['gib']:.1f} GiB), last changed {entry['last_change']}")
    for failure in result.failed:
        print(f"tmp sweep: could not assess {failure['path']}: {failure['error']}", file=sys.stderr)
    print(summary(result, True))
    return int(bool(result.failed))


@dataclass(frozen=True)
class CompletedSweepAssessment:
    """The sweep assessed its scope and returned this status."""

    status: int


@dataclass(frozen=True)
class SkippedSweepAssessment:
    """The sweep did not assess its scope."""

    @property
    def status(self) -> int:
        return 0


SweepAssessment = CompletedSweepAssessment | SkippedSweepAssessment


def run_if_due(dry_run: bool, now: float | None = None, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> SweepAssessment:
    """Assess only a due sweep. Move the stamp before the attempt."""
    moment = time.time() if now is None else now
    stamp = state_dir() / STAMP_NAME
    try:
        if stamp.stat().st_mtime > moment - RUN_INTERVAL_SECONDS:
            return SkippedSweepAssessment()
    except FileNotFoundError:
        pass
    if not dry_run:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, (moment, moment))
    return CompletedSweepAssessment(run(dry_run, moment, lint_settings=lint_settings, scratch_settings=scratch_settings))


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--plan":
        try:
            return write_cleanup_plan(Path(argv[1]), default_roots(), time.time(), PROC_DIR)
        except (OSError, UnicodeError, ValueError) as error:
            print(f"tmp sweep: {error}", file=sys.stderr)
            return 1
    if any(arg != DRY_RUN_FLAG for arg in argv):
        print(f"usage: tmp_sweep.py [{DRY_RUN_FLAG}] | --plan <new-plan.json>",
              file=sys.stderr)
        return 2
    return run(DRY_RUN_FLAG in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
