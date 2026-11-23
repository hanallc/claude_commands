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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast

from scripts.lib import git_files
from scripts.production import staff
from scripts.statusline import adhoc_items, rebuild_row

try:
    from scripts.review_links import for_you
except Exception:
    for_you = None


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


def staff_record(text: str) -> staff.StaffSnapshot:
    """Read staff rows without requiring each parent row in the display snapshot."""
    try:
        value = cast(dict[str, object], json.loads(text))
        if value["schema"] not in (1, 2):
            return staff.StaffSnapshot(())
        rows = cast(list[object], value["rows"])
        return staff.StaffSnapshot(tuple(staff.StaffRow.from_json(row) for row in rows))
    except (ValueError, KeyError, TypeError, staff.StaffUnreadable):
        return staff.StaffSnapshot(())


def current_branch(payload: dict[str, object], pwd: str) -> str:
    """Read the branch from the repository that contains the displayed folder."""
    workspace = cast(dict[str, object], payload.get("workspace") or {})
    folder = workspace.get("current_dir")
    if folder is None or folder is False:
        folder = payload.get("cwd")
    try:
        head = git_files.head(git_files.locate(Path(str(folder or pwd))))
    except (OSError, ValueError):
        return ""
    if isinstance(head, git_files.OnBranch):
        return head.name
    return f"detached {head.commit[:7]}" if head.commit else ""


def counts_are_fresh(cache: Path, source: str) -> bool:
    """Keep the counts until a deadline passes or a source changes."""
    try:
        modified = cache.stat().st_mtime_ns
        lines = cache.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    due = lines[1] if len(lines) > 1 else ""
    if due:
        try:
            if not due.isascii() or not due.isdecimal() or int(time.time()) >= int(due):
                return False
        except ValueError:
            return False
    cached_source = ""
    for path in lines[2:]:
        if path.startswith("source=unit:"):
            cached_source = path
            continue
        try:
            if Path(path).stat().st_mtime_ns > modified:
                return False
        except OSError:
            pass
    return cached_source == source


def counts_rows(record: staff.StaffSnapshot, session_id: str) -> str:
    """Read the current unit's counts or the showrunner targets' counts."""
    owner = next((row for row in record.rows
                  if row.kind in ("unit", "producer") and row.current_session == session_id), None)
    owners = (owner,) if owner is not None else tuple(row for row in record.rows if row.kind == "showrunner")
    notifier = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
    state = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
    lines: list[str] = []
    for row in owners:
        instance = notifier / f"showrunner-{row.id}"
        source = f"source=unit:{row.id}" if owner is not None else ""
        if owner is not None:
            target = session_id
        else:
            try:
                with (instance / "conf").open(encoding="utf-8") as configured:
                    setting = configured.readline().strip()
            except (OSError, UnicodeError):
                continue
            prefix = "TARGET=session:"
            if not setting.startswith(prefix) or len(setting) == len(prefix):
                continue
            target = setting[len(prefix):]
        cache = state / "counts" / row.id
        if not counts_are_fresh(cache, source):
            try:
                with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
                    from scripts.statusline import counts_row

                    if owner is not None:
                        _ = counts_row.write_unit(row.id)
                    else:
                        _ = counts_row.write(instance)
            except Exception:
                # This catch replaces the old process boundary.
                if owner is not None:
                    continue
        try:
            with cache.open(encoding="utf-8") as stored:
                line = stored.readline().strip()
        except (OSError, UnicodeError):
            continue
        if line:
            lines.append(f"{target} {line}\n")
    return "".join(lines)


def status_action_args(*, remote: bool, rebuild: rebuild_row.RebuildAnswer | None = None) -> list[str]:
    """Pass the waiting rebuild names and other action count to jq."""
    names = ""
    count = "unavailable"
    if for_you is not None:
        try:
            actions = (for_you.console_status_line_actions() if remote
                       else for_you.read_for_status_line(rebuild_row.DEFAULT_CONFIG, rebuild=rebuild).status_line_actions())
            names = rebuild_row.rebuild_names(actions.rebuild_machines)
            count = str(actions.other_count)
        except Exception:
            pass
    return ["--arg", "for_you", count, "--arg", "rebuild", names]


def main(rebuild: rebuild_row.RebuildAnswer | None = None) -> None:
    payload = cast(dict[str, object], json.load(sys.stdin))
    try:
        record = staff_record(Path(os.environ["STAFF_FILE"]).read_text(encoding="utf-8"))
    except OSError:
        record = staff.StaffSnapshot(())
    session_id = str(payload.get("session_id", ""))
    label, role = assignment(record, session_id)
    pwd = next((value for option, name, value in zip(sys.argv, sys.argv[1:], sys.argv[2:])
                if option == "--arg" and name == "pwd"), os.getcwd())
    branch = current_branch(payload, pwd) if role == "unit-director" else ""
    rebuild_config = rebuild_row.DEFAULT_CONFIG
    action_args: list[str] = []
    if rebuild is None:
        system = os.environ.get("STATUSLINE_SYSTEM") or os.uname().sysname
        action_args = status_action_args(remote=system == "Darwin")
        # The cache can expire during the count. Read it again without reading the needs.
        rebuild = rebuild_row._read_cache(rebuild_config, time.time)  # pyright: ignore[reportPrivateUsage]
    if rebuild.refresh_due:
        try:
            _ = subprocess.Popen(
                [sys.executable, "-I", str(Path(rebuild_row.__file__).resolve()), "refresh", "--config", str(rebuild_config)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass
    os.execvp("jq", ["jq", "-n", "--argjson", "payload", json.dumps(payload),
                     "--arg", "assignment", label, "--arg", "role", role,
                     "--arg", "counts", counts_rows(record, session_id),
                     "--arg", "branch", branch,
                     "--arg", "adhoc", adhoc_items.row(session_id),
                     *action_args, *sys.argv[1:]])


if __name__ == "__main__":
    main()
