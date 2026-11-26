"""Read the session's assignment from its staff row."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Generic, TypeVar, cast

if TYPE_CHECKING:
    from scripts.production import staff

NO_READ_DEADLINE = 2**63 - 1
REVIEW_COUNTS_VERSION = "review-counts=5"


class RenderSources:
    """The file versions and deadline that permit reuse of one render."""

    def __init__(self) -> None:
        self.stamps: dict[Path, str] = {}
        self.deadline: int = NO_READ_DEADLINE
        self.complete: bool = True

    def record(self, path: Path, stamp: str) -> None:
        self.complete &= self.stamps.setdefault(path.absolute(), stamp) == stamp

    def watch(self, path: Path) -> None:
        try:
            self.record(path, file_stamp(path))
        except (OSError, ValueError):
            self.complete = False

    def verified_absence(self, path: Path) -> bool:
        return self.stamps.get(path.absolute()) == "missing"

    def mark_incomplete(self) -> None:
        self.complete = False

    def merge(self, other: RenderSources) -> None:
        for path, stamp in other.stamps.items():
            self.record(path, stamp)
        self.deadline = min(self.deadline, other.deadline)
        self.complete &= other.complete


T = TypeVar("T")


@dataclass(frozen=True)
class RenderRead(Generic[T]):
    value: T
    sources: RenderSources


def assignment(record: staff.StaffSnapshot, session_id: str) -> tuple[str, str]:
    """Return the assignment text and its color role."""
    row = record.by_current_session(session_id)
    if row is None:
        return "unassigned", "unassigned"
    if row.kind == "showrunner":
        return f"{row.name} showrunner", "showrunner"
    if row.kind == "worker":
        return "worker", "worker"
    if row.kind == "producer":
        return f"{row.name} the producer", "producer"
    parent = record.parent_of(row)
    label = f"{row.name} unit"
    if parent is not None:
        label += f" for {parent.name}"
    return label, "unit-director"


def staff_record(text: str, sources: RenderSources | None = None) -> staff.StaffSnapshot:
    """Read staff rows without requiring each parent row in the display snapshot."""
    from scripts.production import staff

    try:
        value = cast(dict[str, object], json.loads(text))
        if value["schema"] not in (1, 2):
            raise ValueError("Unknown staff schema.")
        rows = value["rows"]
        if not isinstance(rows, list):
            raise ValueError("Staff rows must be a list.")
        return staff.StaffSnapshot(tuple(staff.StaffRow.from_json(row) for row in cast(list[object], rows)))
    except (ValueError, KeyError, TypeError, staff.StaffUnreadable):
        if sources is not None:
            sources.complete = False
        return staff.StaffSnapshot(())


def read_staff(path: Path) -> RenderRead[staff.StaffSnapshot]:
    sources = RenderSources()
    sources.watch(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        text = ""
    return RenderRead(staff_record(text, sources), sources)


def read_branch(payload: dict[str, object], pwd: str, role: str) -> RenderRead[str]:
    sources = RenderSources()
    return RenderRead(current_branch(payload, pwd, sources) if role == "unit-director" else "", sources)


def read_adhoc(session_id: str) -> RenderRead[str]:
    sources = RenderSources()
    value = ""
    if session_id and Path(session_id).name == session_id and not session_id.startswith("."):
        from scripts.statusline import adhoc_items

        path = Path.home() / ".local/state/adhoc_review" / session_id
        sources.watch(path)
        try:
            value = adhoc_items.read_row(path)
        except FileNotFoundError:
            sources.record(path, "missing")
        except (OSError, ValueError):
            sources.complete = False
    return RenderRead(value, sources)


def current_branch(payload: dict[str, object], pwd: str, sources: RenderSources | None = None) -> str:
    """Read the branch from the repository that contains the displayed folder."""
    from scripts.lib import git_files

    workspace = cast(dict[str, object], payload.get("workspace") or {})
    folder = workspace.get("current_dir")
    if folder is None or folder is False:
        folder = payload.get("cwd")
    try:
        path = Path(str(folder or pwd))
        if sources is not None:
            sources.watch(path.expanduser())
            resolved = path.expanduser().resolve()
            if resolved.is_dir():
                for parent in (resolved, *resolved.parents):
                    marker = parent / ".git"
                    sources.watch(marker)
                    if marker.exists() or marker.is_symlink():
                        break
        checkout = git_files.locate(path)
        if sources is not None:
            for metadata, prefix in ((checkout.toplevel / ".git", "gitdir:"),
                                     (checkout.git_dir / "commondir", "")):
                sources.watch(metadata)
                if metadata.is_file():
                    try:
                        text = git_files._read_entry(metadata)  # pyright: ignore[reportPrivateUsage]
                        target = text.strip().removeprefix(prefix).strip()
                        sources.watch(metadata.parent / target)
                    except (OSError, git_files.GitFilesError):
                        sources.complete = False
            sources.watch(checkout.git_dir / "HEAD")
            if git_files.locate(path) != checkout:
                sources.complete = False
        head = git_files.head(checkout)
    except git_files.NotACheckout:
        return ""
    except (OSError, ValueError, RuntimeError):
        if sources is not None:
            sources.complete = False
        return ""
    if isinstance(head, git_files.OnBranch):
        return head.name
    return f"detached {head.commit[:7]}" if head.commit else ""


def stat_stamp(stat: os.stat_result) -> str:
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"


def file_stamp(path: Path) -> str:
    try:
        return stat_stamp(path.stat())
    except FileNotFoundError:
        try:
            return stat_stamp(path.lstat())
        except FileNotFoundError:
            return "missing"


def state_directory() -> Path:
    return Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")


def cached_review_row(cache: Path, source: str, sources: RenderSources, *, allow_stale: bool = False) -> str | None:
    """Read a Review row from the current count version."""
    try:
        with cache.open(encoding="utf-8") as stored:
            observed = os.fstat(stored.fileno())
            lines = stored.read().splitlines()
    except (OSError, UnicodeError):
        return None
    if len(lines) < 3 or lines[-1] != REVIEW_COUNTS_VERSION:
        return None
    if allow_stale:
        return lines[0]
    due = lines[1]
    if due:
        try:
            if not due.isascii() or not due.isdecimal():
                return None
            deadline = int(due)
        except ValueError:
            return None
    else:
        deadline = NO_READ_DEADLINE
    cached_source = ""
    for path in lines[2:-1]:
        if path.startswith("source="):
            cached_source = path
            continue
        try:
            status = Path(path).stat()
            if status.st_mtime_ns > observed.st_mtime_ns:
                return None
            sources.record(Path(path), stat_stamp(status))
        except FileNotFoundError:
            sources.record(Path(path), "missing")
        except OSError:
            sources.complete = False
    if cached_source != source or time.time() >= deadline:
        return None
    sources.record(cache, stat_stamp(observed))
    sources.deadline = min(sources.deadline, deadline)
    return lines[0]


def read_counts(record: staff.StaffSnapshot, session_id: str) -> RenderRead[str]:
    """Read the current unit's counts or the showrunner targets' counts."""
    sources = RenderSources()
    row = record.by_current_session(session_id)
    owner = row.id if row is not None and row.kind in ("unit", "producer") else None
    owners = (owner,) if owner is not None else tuple(row.id for row in record.rows if row.kind == "showrunner")
    notifier = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
    state = state_directory()
    lines: list[str] = []
    for identity in owners:
        instance = notifier / f"showrunner-{identity}"
        source = f"source=unit:{identity}" if owner is not None else ""
        if owner is not None:
            target = session_id
        else:
            sources.watch(instance / "conf")
            try:
                with (instance / "conf").open(encoding="utf-8") as configured:
                    setting = configured.readline().strip()
            except FileNotFoundError:
                sources.record(instance / "conf", "missing")
                continue
            except (OSError, UnicodeError):
                sources.complete = False
                continue
            prefix = "TARGET=session:"
            if not setting.startswith(prefix) or len(setting) == len(prefix):
                continue
            target = setting[len(prefix):]
        cache = state / "counts" / identity
        candidate = RenderSources()
        line = cached_review_row(cache, source, candidate)
        if line is None:
            try:
                with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
                    from scripts.statusline import counts_row

                    if owner is not None:
                        _ = counts_row.write_unit(identity)
                    else:
                        _ = counts_row.write(instance)
            except Exception:
                # Keep the fallback output when the refresh fails.
                sources.complete = False
                if owner is not None:
                    continue
            candidate = RenderSources()
            line = cached_review_row(cache, source, candidate)
            if line is None:
                candidate.complete = False
                line = cached_review_row(cache, source, candidate, allow_stale=True)
        sources.merge(candidate)
        if line and (line := line.strip()):
            lines.append(f"{target} {line}\n")
    return RenderRead("".join(lines), sources)


@dataclass(frozen=True)
class ActionFields:
    other_count: str
    waiting_rebuilds: str
    refresh_due: bool


def read_actions(*, remote: bool) -> RenderRead[ActionFields]:
    from scripts.statusline import rebuild_row

    sources = RenderSources()
    config = rebuild_row.DEFAULT_CONFIG
    actions_count, waiting_rebuilds = "unavailable", ""
    refresh_due, deadline = False, 0
    try:
        from scripts.review_links import for_you

        if remote:
            actions = for_you.console_status_line_actions()
            sources.complete = isinstance(actions.other_count, int)
        else:
            snapshot = for_you.read_for_status_line(config, watch=sources)
            actions = snapshot.status_line_actions()
            deadline = snapshot.comparison_deadline
            sources.complete &= not snapshot.unavailable and deadline > 0
            sources.deadline = deadline
            refresh_due = snapshot.comparison_refresh_due or 0 < deadline <= time.time()
        waiting_rebuilds = rebuild_row.rebuild_names(actions.rebuild_machines)
        actions_count = f"{actions.other_count}+" if actions.partial else str(actions.other_count)
    except Exception:
        sources.complete = False
    if remote or not deadline:
        refresh_due = refresh_due or rebuild_row._read_cache(config, time.time).refresh_due  # pyright: ignore[reportPrivateUsage]
    if refresh_due and not remote:
        actions_count = "unavailable"
        sources.complete = False
    return RenderRead(ActionFields(actions_count, waiting_rebuilds, refresh_due), sources)


def watch_implementation(sources: RenderSources) -> None:
    scripts = Path(__file__).absolute().parents[1]
    for name in (
        "statusline/statusline.sh", "statusline/statusline.jq", "statusline/assignment.py",
        "statusline/rebuild_row.py", "statusline/adhoc_items.py", "statusline/counts_row.py",
        "review_links/for_you.py", "for_you/action.py", "rebuild/need.py", "production/staff.py",
        "direct/recorder_state.py", "message/review_hold.py", "lib/conf_reads.py", "lib/git_files.py",
        "lib/conf.py", "lib/user_zone.py", "lib/package_root.py",
    ):
        path = scripts / name
        sources.watch(path)
        sources.complete &= sources.stamps.get(path, "missing") != "missing"


def publish_render(target: Path, key: str, fields: dict[str, str], sources: RenderSources) -> None:
    """Publish the session fields and the exact file versions that produced them."""
    if (not sources.complete or not sources.stamps or not time.time() < sources.deadline < NO_READ_DEADLINE
            or any("\0" in value for value in fields.values())):
        return
    from datetime import datetime, timezone
    from scripts.lib import atomic_file

    def stat_time(nanoseconds: str) -> str:
        seconds, fraction = divmod(int(nanoseconds), 10**9)
        moment = datetime.fromtimestamp(seconds, timezone.utc)
        return f"{moment:%Y-%m-%d %H:%M:%S}.{fraction:09} +0000"

    rows: list[str] = []
    for path, stamp in sources.stamps.items():
        if any(separator in str(path) for separator in "\r\n\t"):
            return
        if stamp != "missing":
            device, inode, modified, changed = stamp.split(":")
            stamp = f"{device}:{inode}:{stat_time(modified)}:{stat_time(changed)}"
        rows.append(f"{path}\t{stamp}")
    content = "\n".join((key, str(sources.deadline), str(len(rows)), json.dumps(fields), *rows, ""))
    with atomic_file.prepared_replacement(target, content, mode=0o600) as temporary:
        if all(file_stamp(path) == stamp for path, stamp in sources.stamps.items()) and time.time() < sources.deadline:
            _ = temporary.replace(target)


def render_status(*, remote: bool) -> None:
    payload = cast(dict[str, object], json.load(sys.stdin))
    file = Path(os.environ["STAFF_FILE"])
    session_id = str(payload.get("session_id", ""))
    key = os.environ.get("STATUSLINE_RENDER_KEY", "")
    safe_session = len(session_id) <= 128 and all(
        value.isascii() and (value.isalnum() or value in "-_") for value in session_id)
    sources = RenderSources()
    cacheable = bool(key and safe_session and not remote)
    # Save renders below counts to preserve the watched state directory's modification time.
    target = state_directory() / "counts" / "statusline-render-v2" / f"s-{session_id}"
    if cacheable:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            watch_implementation(sources)
        except OSError:
            sources.complete = False
    members = read_staff(file)
    label, role = assignment(members.value, session_id)
    pwd = next((value for option, name, value in zip(sys.argv, sys.argv[1:], sys.argv[2:])
                if option == "--arg" and name == "pwd"), os.getcwd())
    branch = read_branch(payload, pwd, role)
    adhoc = read_adhoc(session_id)
    actions = read_actions(remote=remote)
    if actions.value.refresh_due:
        from scripts.statusline import rebuild_row

        try:
            _ = subprocess.Popen(
                [sys.executable, "-I", str(Path(rebuild_row.__file__).resolve()), "refresh", "--config", str(rebuild_row.DEFAULT_CONFIG)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass
    if cacheable:
        from scripts.lib import user_zone

        sources.watch(Path(user_zone.CONF))
    counts = read_counts(members.value, session_id)
    fields = dict(assignment=label, role=role, counts=counts.value, branch=branch.value,
                  adhoc=adhoc.value, for_you=actions.value.other_count, rebuild=actions.value.waiting_rebuilds)
    if cacheable:
        for read in (members, branch, adhoc, actions, counts):
            sources.merge(read.sources)
        with suppress(OSError, ValueError, OverflowError):
            publish_render(target, key, fields, sources)
    arguments = [argument for name, value in fields.items() for argument in ("--arg", name, value)]
    os.execvp("jq", ["jq", "-n", "--argjson", "payload", json.dumps(payload), *arguments, *sys.argv[1:]])


def main() -> None:
    render_status(remote=(os.environ.get("STATUSLINE_SYSTEM") or os.uname().sysname) == "Darwin")


if __name__ == "__main__":
    main()
