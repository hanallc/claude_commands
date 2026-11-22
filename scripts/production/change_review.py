#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Read unit commit trailers and write change review rows.

A visible trailer requires three fields and a final image <id>, with optional after <step> before the image.
An invisible change uses Change-Review: none — <reason>.
Every supplied review line must parse, whatever the commit subject.
Only checkpoints that change more than Markdown require a review line.
See commands/change_review.md for actions, list selection, urgency, verdicts, and scratch folders.

An open Production cell holds its unit's fixed staff id or its showrunner's fixed staff id.
Readers resolve current names and the unit's current parent.
Reviewed rows retain the production name recorded at the verdict.
Unknown owner names remain unresolved.

Run backfill-production [--list PATH] [--write] to resolve empty cells from recorded merge history.
List writes hold the directory lock. Normal writes update metadata and commit only the list.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import TypeVar, cast
from zoneinfo import ZoneInfo

from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed, prepared_replacement, sync_folder, write_text_atomic  # noqa: E402
from scripts.lib import build_folders, conf, scratch, unit_runs  # noqa: E402
from scripts.direct.progress_history import load_events  # noqa: E402
from scripts.lib import urgency, file_lock  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402

from scripts.production import issues  # noqa: E402
from scripts.production.proposals_folder import owner_docs, scope_docs  # noqa: E402
from scripts.production import change_routing, delivery_state, session_staff, showrunner_tree, showrunners, staff, unit_config, unit_lookup
from scripts.production.production_doc import (
    EnrolledPaths, LegacyProduction, ProductionDocument, ProductionPathsError, Refusal, production_field,
    resolve_production_paths,
)
from scripts.production.production_names import MERGE_CHECKOUT_FIELD
from scripts.review_links import image_store, review_events, review_later
from scripts.agents import remote

TRAILER = "Change-Review"
COVERS = f"{TRAILER}-Covers"
HELD_NEXT_STEP = (f"commit a checkpoint (a plain commit, not a merge: a merge may carry only `{TRAILER}: none` lines) "
                  + f"whose {TRAILER} lines describe the change and that names each "
                  + f"listed commit in a `{COVERS}: <hash>` line, push it, and send its hash")
LIST_HEADER = "- **Change review:**"
SECTION = "## To review"
REVIEWED = "## Reviewed"
CHECK = "How to check"
URGENCY = "Urgency"
# Open rows hold the owner's fixed staff id. Reviewed rows keep their recorded production.
PRODUCTION = "Production"
PRODUCER = change_routing.PRODUCER
IMAGE = "Image"
KIND = "Kind"
REVIEW_KINDS = ("See in action", "Read only")
NOT_YET = "not yet"
FORM = "<what changed for you> | <how to try it: where and what to type> | <what you see> [| after <step>] | image <id>"
SEPARATOR = " | "
SHORT = 8
# Commits made before the rule landed carry no line and are not read: 2026-10-15 00:00 EDT.
CHECKED_FROM = 1792036800
# Commits made before the image rule landed need no image: 2026-11-16 00:00 EST.
IMAGES_CHECKED_FROM = 1794805200
LANDED = "2026-10-15"
# Scan only merges made after the readiness rule starts.
READY_FROM = "2026-11-06"
_CURRENT_REPO = Path(".")
_NONE = re.compile(r"^none\s*[—–-]+\s*(?P<why>\S.*)$")
_LINE = re.compile(rf"^{TRAILER}:\s*(?P<value>.*)$", re.MULTILINE)
_COVERS = re.compile(rf"^{COVERS}:\s*(?P<sha>[0-9a-f]{{7,40}})\s*$", re.MULTILINE)
_NUMBER = re.compile(r"^\| C(\d+) \|", re.MULTILINE)
_ROW = re.compile(r"^[A-Z]\d+$")
_LANDS = re.compile(r"^\s*(?:(?:okay|ok)\s+)?(?:c\d+\s+)?(?:works|fine)\b", re.IGNORECASE)
# A cell boundary is a `|` that _cell did not escape.
_BOUNDARY = re.compile(r"(?<!\\)\|")
_BREAK_TAG = re.compile(r"<(\\*)br>")
_CHECKPOINT = re.compile(r"^checkpoint\((?P<branch>[^)]+)\):")
_HASH = re.compile(r"\b[0-9a-f]{7,40}\b")
# The columns an older row keeps its merge hash in, in the order they are read.
HASH_COLUMNS = ("Merged", "On the Mac build")
lists_directory = change_routing.lists_directory
PREFIX = "change-review - "
FENCE = "---"
PRODUCTIONS = "productions"
_KEY = re.compile(r"^([A-Za-z_][\w-]*):(.*)$")
_ITEM = re.compile(r"^\s+-\s*(.*)$")
# The tables every list holds; a new list puts its frontmatter and title above them.
BODY = """Ask for "change review" any time to walk it one change at a time. A reviewed row moves to Reviewed with your verdict.

## To review

| # | Change | Merged | How to check |
| --- | --- | --- | --- |

## Reviewed

| # | Change | Verdict | Date |
| --- | --- | --- | --- |
"""
Result = TypeVar("Result")


@dataclass(frozen=True)
class ReadyNow:
    """The change needs no further live step."""


@dataclass(frozen=True)
class AfterStep:
    step: str


@dataclass(frozen=True)
class Visible:
    change: str
    check: str
    see: str
    readiness: ReadyNow | AfterStep = ReadyNow()
    image: str = ""

    def how(self) -> str:
        """The row's How to check cell: how to try the change, then what the user sees."""
        stop = "" if self.check.endswith((".", "!", "?", ":")) else "."
        return f"{self.check}{stop} You see: {self.see}"


@dataclass(frozen=True)
class Invisible:
    why: str


@dataclass(frozen=True)
class Malformed:
    text: str


Entry = Visible | Invisible | Malformed


@dataclass(frozen=True)
class Commit:
    """A unit commit with the shared parent graph, including connecting merges."""

    sha: str
    subject: str
    message: str
    files: tuple[str, ...]
    timestamp: int
    ancestry: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]], repr=False, compare=False)


class ChangeReviewError(Exception):
    """A list that cannot take rows, or a git read that failed."""


class RowNotListed(ChangeReviewError):
    """A row ID that no review list holds."""


def parse(value: str) -> Entry:
    text = value.strip()
    if (none := _NONE.match(text)) is not None:
        return Invisible(none.group("why").strip())
    parts = [part.strip() for part in text.split(SEPARATOR)]
    image = ""
    if len(parts) in (4, 5) and parts[-1].startswith("image "):
        image = parts.pop().removeprefix("image ").strip()
        if image_store.IDENTITY.fullmatch(image) is None:
            return Malformed(text)
    if len(parts) == 3 and all(parts):
        return Visible(parts[0], parts[1], parts[2], image=image)
    if (len(parts) == 4 and all(parts) and parts[3].startswith("after ")
            and (step := parts[3].removeprefix("after ").strip())):
        return Visible(parts[0], parts[1], parts[2], AfterStep(step), image)
    return Malformed(text)


def entries(message: str) -> list[Entry]:
    return [parse(found.group("value")) for found in _LINE.finditer(message)]


def checkpoint_changes(message: str) -> tuple[str, ...]:
    """The visible change names in a unit commit message."""
    return tuple(item.change for item in entries(message) if isinstance(item, Visible))


def needs_trailer(subject: str, files: tuple[str, ...]) -> bool:
    """A checkpoint(...) commit needs one unless every file it changes is Markdown."""
    return subject.startswith("checkpoint(") and not (files and all(name.endswith(".md") for name in files))


def problem(subject: str, message: str, files: tuple[str, ...], *, timestamp: int = IMAGES_CHECKED_FROM) -> str:
    """Why a commit's message fails the rule, or the empty string."""
    found = entries(message)
    if not found and needs_trailer(subject, files):
        return f"no {TRAILER} line: add `{TRAILER}: {FORM}` or `{TRAILER}: none — <why nothing visible changes>`"
    bad = [item.text for item in found if isinstance(item, Malformed)]
    if bad:
        return (f"{TRAILER} line {bad[0]!r} needs three parts split by {SEPARATOR.strip()!r}, optional ` | after <step>`, and required ` | image <12-hex id>`: "
                + f"`{TRAILER}: {FORM}`, or `{TRAILER}: none — <why nothing visible changes>`")
    for line, item in zip(_LINE.finditer(message), found):
        if timestamp >= IMAGES_CHECKED_FROM and isinstance(item, Visible) and not item.image:
            return (f"{TRAILER} line {line.group(0)!r} has no image: register one with change_review.py image --image <png> "
                    + "(or --output <text file> for terminal output), then end the line with | image <id>")
    return ""


def merge_review_problem(message: str, *, is_merge: bool) -> str:
    """Merge messages allow only review lines that parse as Invisible."""
    if is_merge and (any(not isinstance(item, Invisible) for item in entries(message))
                     or re.search(rf"^{COVERS}:", message, re.MULTILINE)):
        return (f"Merge commits cannot carry visible {TRAILER} or {COVERS} lines. "
                + "Put the lines on a plain commit after the merge")
    return ""


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ChangeReviewError(f"git {' '.join(arguments)}: {result.stderr.strip()}")
    return result.stdout


def on_main(repo: Path, merged: str) -> bool:
    """Return whether origin/main contains the requested commit."""
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", merged, "origin/main"],
                          capture_output=True, check=False).returncode == 0


def _require_main(repo: Path, merged: str) -> None:
    if not on_main(repo, merged):
        raise ChangeReviewError(
            f"{merged} is not on origin/main; no row added. "
            + "A change goes on the list only when the user can check it now. "
            + "If it just landed, run git fetch and retry.")


def registered_productions() -> Iterable[Path]:
    """Production docs named by the registered update timers."""

    return showrunners.timer_docs().values()


def common_git_directory(repo: Path) -> Path:
    """The repository directory shared by all its worktrees."""
    directory = Path(_git(repo, "rev-parse", "--git-common-dir").strip())
    return (repo / directory).resolve()


def reviewed_merge_refs(repo: Path, productions: Callable[[], Iterable[Path]], *,
                        record: staff.StaffSnapshot | None = None) -> list[str]:
    """Origin branches whose readable productions in this repository already write review rows."""
    return list(dict.fromkeys(_reviewed_merge_refs_by_doc(repo, productions, record=record).values()))


def _reviewed_merge_refs_by_doc(repo: Path, productions: Callable[[], Iterable[Path]], *,
                                record: staff.StaffSnapshot | None = None) -> dict[Path, str]:
    """Keep each production document with its origin merge ref."""
    try:
        common = common_git_directory(repo)
        available = set(_git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/").splitlines())
        docs = tuple(productions())
    except (OSError, UnicodeError, ValueError, Refusal, ChangeReviewError):
        return {}
    refs: dict[Path, str] = {}
    for doc in docs:
        try:
            lines = doc.read_text(encoding="utf-8").splitlines()
            if list_path(doc, record=record) is None:
                continue
            where = Path(production_field(lines, "Repository")).expanduser()
            if common_git_directory(where) != common:
                continue
            branch = production_field(lines, "Merge branch")
            if f"refs/remotes/origin/{branch}" in available:
                refs[doc] = f"origin/{branch}"
        except (OSError, UnicodeError, ValueError, Refusal, ChangeReviewError):
            continue
    return refs


def unit_commits(repo: Path, checkpoint: str, previous: str, merge_branch: str,
                 checked_from: int = CHECKED_FROM, *,
                 productions: Callable[[], Iterable[Path]] = registered_productions) -> list[Commit]:
    """The unit's commits, excluding origin merge branches that already write review rows."""
    excluded = list(dict.fromkeys([f"origin/{merge_branch}", *reviewed_merge_refs(repo, productions)]))
    records = _git(repo, "log", "--topo-order", "-z", "--format=%H %ct %P%n%B", checkpoint, f"^{previous}",
                   *(f"^{ref}" for ref in excluded), "--").split("\0")
    commits: list[Commit] = []
    ancestry: dict[str, tuple[str, ...]] = {}
    for record in filter(str.strip, records):
        header, message = record.split("\n", 1)
        sha, stamp, *parents = header.split()
        ancestry[sha] = tuple(parents)
        if int(stamp) < checked_from:
            continue
        if len(parents) < 2:
            commits.append(Commit(sha, message.partition("\n")[0], message, _files(repo, sha), int(stamp), ancestry))
        elif reason := merge_review_problem(message, is_merge=True):
            raise ChangeReviewError(f"{sha[:SHORT]}: {reason}. Make the merge again without the lines")
    return commits


def _uncovered_problems(commits: list[Commit]) -> Iterable[tuple[Commit, str]]:
    """Find invalid commits that no passing commit covers."""
    covered = [found.group("sha") for commit in commits if entries(commit.message)
               and not problem(commit.subject, commit.message, commit.files, timestamp=commit.timestamp)
               for found in _COVERS.finditer(commit.message)]
    for commit in commits:
        reason = problem(commit.subject, commit.message, commit.files, timestamp=commit.timestamp)
        if reason and not any(commit.sha.startswith(sha) for sha in covered):
            yield commit, reason


def missing(commits: list[Commit]) -> list[str]:
    """Name each invalid commit that no passing commit covers."""
    return [f"{commit.sha[:SHORT]} {commit.subject}: {reason}" for commit, reason in _uncovered_problems(commits)]


def review_commits(commits: list[Commit]) -> list[Commit]:
    """Keep all checkpoints and commits without a descendant checkpoint in this set."""
    replaced: set[str] = set()
    for commit in commits:
        if not commit.subject.startswith("checkpoint("):
            continue
        pending = list(commit.ancestry.get(commit.sha, ()))
        while pending:
            ancestor = pending.pop()
            if ancestor in replaced:
                continue
            replaced.add(ancestor)
            pending.extend(commit.ancestry.get(ancestor, ()))
    return [commit for commit in commits if commit.subject.startswith("checkpoint(") or commit.sha not in replaced]


def _checkpoint_entries(commits: list[Commit]) -> Iterable[tuple[Commit, Visible]]:
    """Read checkpoint lines and lines without a descendant checkpoint."""
    for commit in reversed(review_commits(commits)):
        for item in entries(commit.message):
            if isinstance(item, Visible):
                yield commit, item


def visible(commits: list[Commit]) -> list[Visible]:
    """Read checkpoint changes and changes without a descendant checkpoint."""
    return [item for _, item in _checkpoint_entries(commits)]


def _line_value(doc: Path, *, sources: list[Path] | None = None,
                lines: list[str] | None = None) -> str | None:
    """The path the doc's Change review line names: its first `quoted` part, else the words before
    any ` — ` note. None when the doc has no line."""
    if sources is not None:
        sources.append(doc)
    for line in doc.read_text(encoding="utf-8").splitlines() if lines is None else lines:
        if line.startswith(LIST_HEADER):
            value = line.removeprefix(LIST_HEADER).strip()
            quoted = re.match(r"`([^`]+)`", value)
            return quoted.group(1).strip() if quoted else value.split(" — ")[0].strip()
    return None


def list_path(doc: Path, *,
              record: staff.StaffSnapshot | None = None, sources: list[Path] | None = None) -> Path | None:
    """Return the root showrunner's list, or the document's path when its owner cannot be resolved.

    Return None when the document has no Change review line.
    Report a different path in that line.
    """
    value = _line_value(doc, sources=sources)
    if value is None:
        return None
    named = Path(value).expanduser() if value else None
    try:
        derived = list_file(showrunner_of(doc, record=record), sources=sources)
    except OSError:
        if sources is not None:
            raise
        return named
    except ChangeReviewError:
        return named
    if named is not None and named.absolute() != derived.absolute():
        print(f"change review: {doc} names {named} in its Change review line; the list is {derived}",
              file=sys.stderr)
    return derived


def next_number(text: str) -> int:
    """One past the highest C number anywhere in the list: numbers only go up."""
    return max((int(found.group(1)) for found in _NUMBER.finditer(text)), default=0) + 1


def now() -> datetime:
    """The current time in the user's zone."""
    return datetime.now(user_zone())


def today() -> date:
    return now().date()


def _cell(text: str) -> str:
    """Encode a cell as one table line.

    A final line separator can drop because callers strip cell text.
    """
    escaped = _BREAK_TAG.sub(lambda match: "<\\" + match[1] + "br>", text.replace("|", "\\|"))
    return "<br>".join(escaped.splitlines())


def _plain(cell: str) -> str:
    """Decode line breaks and remove one backslash from each typed break tag."""
    return _BREAK_TAG.sub(lambda match: "<" + match[1][1:] + "br>" if match[1] else "\n",
                          cell.replace("\\|", "|"))


def _cells(line: str) -> list[str]:
    """A table row's cells, still escaped as written."""
    return [cell.strip() for cell in _BOUNDARY.split(line.strip())[1:-1]]


def row_id(row: str) -> str:
    """A written row's id, such as C24."""
    return _cells(row)[0]


def read(path: Path) -> str:
    """The list's text; a list not yet written reads as the skeleton's tables."""
    return path.read_text(encoding="utf-8") if path.exists() else BODY


def review_list_source(path: Path) -> Path:
    """Resolve a review list path and reject existing paths that are not files."""
    source = path.resolve()
    try:
        mode = source.stat().st_mode
    except FileNotFoundError:
        return source
    if not stat.S_ISREG(mode):
        raise ChangeReviewError(f"{source}: the review list must be a regular file")
    return source


def list_file(showrunner: str, *, sources: list[Path] | None = None) -> Path:
    """The list of the top-level showrunner called `showrunner`."""
    folder = lists_directory() if sources is None else lists_directory(sources=sources)
    return folder / f"{PREFIX}{showrunner}.md"


def _owner(path: Path) -> str:
    """The showrunner a list is named for."""
    return path.stem.removeprefix(PREFIX)


def display_id(row: dict[str, str], showrunner: str) -> str:
    """Show the current responsible showrunner beside the local row number."""
    return f"{showrunner}/{row['#'].rpartition('/')[2]}"


def review_routing(paths: Iterable[Path]) -> change_routing.StaffRouting:
    """Prepare the shared staff resolver for the requested lists."""
    return _routing_from_snapshots(_review_snapshots(paths))


def _routing_from_snapshots(snapshots: Mapping[Path, ReviewListSnapshot]) -> change_routing.StaffRouting:
    """Resolve ownership from the same list reads used for row lookup."""
    record = staff.snapshot()
    documents = {member.name: member.doc.path for member in record.find(kind="showrunner")
                 if isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc))}
    documents = change_routing.production_documents(documents,
        ((_owner(path), snapshot.text) for path, snapshot in snapshots.items()))
    return change_routing.StaffRouting(record, documents)


def row_display_id(path: Path, row: str) -> str:
    """Resolve the displayed identity without changing the stored list identity."""
    local = row.rpartition("/")[2]
    found = next((item for item in review_rows(path) if item["#"] == local), None)
    if found is None:
        raise ChangeReviewError(f"{row} is not on the list to review")
    return display_id(found, review_routing((path,)).owner(found, path).tab)


def _shown(doc: Path) -> str:
    """The doc's absolute path, under `~` when it is in the home directory."""
    text, home = str(doc.expanduser().absolute()), str(Path.home())
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def _vault_date(day: date) -> str:
    """A date as the vault writes it: a quoted link to the day's note."""
    return f'"[[{day.isoformat()}]]"'


def skeleton(path: Path, production: Path, day: date) -> str:
    """A new list: frontmatter in key order, the showrunner's title, then the empty tables."""
    owner = _owner(path)
    return "\n".join([FENCE, f"date_created: {_vault_date(day)}", f"date_modified: {_vault_date(day)}",
                      f"{PRODUCTIONS}:", f'  - "{_shown(production)}"', f"showrunner: {owner}", FENCE, "",
                      f"# {owner} showrunner: changes to review", "", BODY])


def _key_span(lines: list[str], end: int, key: str) -> tuple[int, int] | None:
    """The key's line and the lines continuing it, as a slice of the frontmatter, or None."""
    for start in range(1, end):
        found = _KEY.match(lines[start])
        if found is None or found.group(1) != key:
            continue
        stop = start + 1
        while stop < end and _KEY.match(lines[stop]) is None:
            stop += 1
        return start, stop
    return None


def _set_key(lines: list[str], end: int, key: str, value: list[str]) -> None:
    """Replace the key's lines with `value`, or insert them before the first later key."""
    span = _key_span(lines, end, key)
    if span is None:
        at = next((index for index in range(1, end) if (found := _KEY.match(lines[index])) is not None
                   and found.group(1) > key), end)
        lines[at:at] = value
    else:
        lines[span[0]:span[1]] = value


def _stamp(path: Path, lines: list[str], production: Path, day: date) -> None:
    """Set date_modified and add `production` to productions; a list without frontmatter gains one."""
    if not lines or lines[0].rstrip("\r\n") != FENCE:
        lines[0:0] = [f"{FENCE}\n", f"date_created: {_vault_date(day)}\n", f"showrunner: {_owner(path)}\n",
                      f"{FENCE}\n", "\n"]
    end = next((index for index in range(1, len(lines)) if lines[index].rstrip("\r\n") == FENCE), None)
    if end is None:
        raise ChangeReviewError(f"{path} has no closing --- for its frontmatter")
    _set_key(lines, end, "date_modified", [f"date_modified: {_vault_date(day)}\n"])
    end = next(index for index in range(1, len(lines)) if lines[index].rstrip("\r\n") == FENCE)
    span = _key_span(lines, end, PRODUCTIONS)
    listed: list[str] = []
    if span is not None:
        inline = cast(re.Match[str], _KEY.match(lines[span[0]])).group(2)
        listed = [*issues.values(inline), *(issues.plain(item.group(1)) for line in lines[span[0] + 1:span[1]]
                                            if (item := _ITEM.match(line)) is not None)]
    target = production.expanduser().absolute()
    if any(Path(item).expanduser().absolute() == target for item in listed):
        return
    _set_key(lines, end, PRODUCTIONS, [f"{PRODUCTIONS}:\n", *(f'  - "{item}"\n' for item in [*listed, _shown(production)])])


def _print_warning(message: str) -> None:
    print(message, file=sys.stderr)


def _edit(path: Path, production: Path, day: date, write: Callable[[list[str]], Result],
          what: Callable[[Result], str], *, report_warning: Callable[[str], None] = _print_warning,
          publish: Callable[[Path, str], None] | None = None) -> Result:
    """Change the list's lines under the lock, then write and commit them.

    A change that raises an error writes nothing. The lock is the list's directory.
    Concurrent writers never lose a row. No lock file appears beside the list.
    A missing list starts from the skeleton. Each write stamps the frontmatter for `production` on `day`.
    `what` names the write in the commit subject.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _list_lock(path):
        with _image_reference_lock():
            text = path.read_text(encoding="utf-8") if path.exists() else skeleton(path, production, day)
            lines = text.splitlines(keepends=True)
            result = write(lines)
            if lines != text.splitlines(keepends=True):
                _sort(path, lines)
                _stamp(path, lines, production, day)
                (publish or write_text_atomic)(path, "".join(lines))
        _commit([path], f"{_owner(path)} {what(result)}", report_warning=report_warning)
        return result


def _commit(paths: Sequence[Path], what: str, *, report_warning: Callable[[str], None] = _print_warning) -> None:
    """Commit only the lists through Git plumbing, without refreshing the index."""
    folder = paths[0].parent

    def git(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(folder), "--literal-pathspecs", *arguments],
                              capture_output=True, text=True, check=False, env=env,
                              pass_fds=((build_folders.build_folder_lock_fd(Path(env["GIT_INDEX_FILE"]).parent),)
                                        if env is not None and "GIT_INDEX_FILE" in env else ()))

    def checked(*arguments: str, env: dict[str, str] | None = None) -> str:
        result = git(*arguments, env=env)
        if result.returncode:
            reason = next(iter((result.stderr or result.stdout).strip().splitlines()), f"git {arguments[0]} failed")
            raise ChangeReviewError(reason)
        return result.stdout.strip()

    try:
        probe = git("rev-parse", "--is-inside-work-tree")
        if probe.stdout.strip() != "true":
            reason = probe.stderr.strip()
            if probe.returncode and "not a git repository" not in reason:
                raise ChangeReviewError(reason or "git could not read the list directory")
            return
        root = Path(checked("rev-parse", "--show-toplevel"))
        folder = root
        blobs = [(str(path.resolve().relative_to(root)),
                  checked("hash-object", "-w", "--path", str(path.resolve().relative_to(root)), "--", str(path.resolve())))
                 for path in paths]
        subject = f"change-review: {what}"
        with build_folders.tool_folder("change-review-index") as temporary:
            index = temporary / "index"
            environment = {**os.environ, "GIT_INDEX_FILE": str(index)}
            for attempt in range(5):
                head = git("rev-parse", "--verify", "-q", "HEAD").stdout.strip()
                _ = checked("read-tree", head if head else "--empty", env=environment)
                for name, blob in blobs:
                    _ = checked("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", env=environment)
                tree = checked("write-tree", env=environment)
                if head and tree == checked("rev-parse", head + "^{tree}"):
                    return
                for name, blob in blobs:
                    _ = checked("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
                commit = checked("commit-tree", tree, *(["-p", head] if head else []), "-m", subject)
                updated = git("update-ref", "-m", subject, "HEAD", commit, head or "0" * len(commit))
                if not updated.returncode:
                    return
                if attempt == 4 or git("rev-parse", "--verify", "-q", "HEAD").stdout.strip() == head:
                    raise ChangeReviewError(updated.stderr.strip() or "git update-ref failed")
    except (ChangeReviewError, OSError) as error:
        reason = next(iter(str(error).splitlines()), type(error).__name__)
        report_warning(f"change review: {paths[0]} written but not committed: {reason}; the next write commits it")


def _table(path: Path, lines: list[str], section: str) -> tuple[list[str], int, int]:
    """The column names of the table under `section`, the index of its first row and one past its last."""
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == section)
    except StopIteration:
        raise ChangeReviewError(f"{path} has no `{section}` section") from None
    header = next((index for index in range(start + 1, len(lines))
                   if lines[index].startswith("| # |") or lines[index].startswith("## ")), None)
    if header is None or not lines[header].startswith("| # |"):
        raise ChangeReviewError(f"{path}: `{section}` has no `| # |` table")
    end = header + 1
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    return _cells(lines[header]), min(header + 2, end), end


def _join_cells(cells: Iterable[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _row(path: Path, columns: list[str], values: dict[str, str]) -> str:
    """A row with each column's value, in the table's own column order."""
    if unknown := [column for column in columns if column not in values]:
        raise ChangeReviewError(f"{path}: no value for the `{unknown[0]}` column")
    return _join_cells(values[column] for column in columns)


def _insert(lines: list[str], at: int, rows: list[str]) -> None:
    if at and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines[at:at] = [row + "\n" for row in rows]


def queue(path: Path) -> str:
    """The name urgency.py pins this list's current row under."""
    return f"change-{_owner(path)}"


def _add_column(path: Path, lines: list[str], section: str, name: str) -> None:
    """Give the table under `section` an empty last column `name`, unless it has one."""
    columns, first, end = _table(path, lines, section)
    if name in columns:
        return
    header = first - 2
    for index in range(header, end):
        cell = f" {name} |" if index == header else " --- |" if index == header + 1 else " |"
        lines[index] = lines[index].rstrip("\r\n") + cell + "\n"


def _rank(row: dict[str, str]) -> tuple[urgency.Urgency, str]:
    """A row's urgency and prerequisite, from its Urgency cell."""
    try:
        return urgency.parse_cell(row.get(URGENCY, ""))
    except urgency.UrgencyError as error:
        raise ChangeReviewError(f"{row.get('#', '')}: {error}") from None


def _number(row_text: str) -> int:
    found = re.match(r"C(\d+)$", row_text)
    return int(found.group(1)) if found else 0


def _merged_day(row: dict[str, str], *, sources: list[Path] | None = None) -> datetime | None:
    """The day the row's Merged cell starts with: the row's age."""
    merged = row.get("Merged", "")
    if merged in ("", NOT_YET, "—"):
        return None
    try:
        return datetime.fromisoformat(merged.split(" ", 1)[0])
    except ValueError:
        if sources is not None:
            raise
        return None


def row_entries(rows: list[dict[str, str]], *, sources: list[Path] | None = None) -> list[urgency.Entry[dict[str, str]]]:
    """The rows as urgency.py entries, by C number: each with its level, merge day and prerequisite."""
    entries: list[urgency.Entry[dict[str, str]]] = []
    for row in sorted(rows, key=lambda row: _number(row.get("#", ""))):
        level, after = _rank(row)
        entries.append(urgency.Entry(row.get("#", ""), row, level, _merged_day(row, sources=sources), after=after))
    return entries


def prerequisite_key(after: str, source: Path, current: Path) -> str:
    """Map a stored prerequisite to its key in the current list's queue."""
    if after and "/" not in after and source.absolute() != current.absolute():
        after = f"{_owner(source)}/{after}"
    return after.removeprefix(f"{_owner(current)}/")


def ordered(rows: list[dict[str, str]], path: Path) -> list[dict[str, str]]:
    """The rows in urgency.py's order: the pinned row first while it waits, then by urgency, then by C
    number; a row the user moved to the end last."""
    entries = [replace(entry, after=prerequisite_key(entry.after, path, path)) for entry in row_entries(rows)]
    return [entry.item for entry in urgency.queue_order(entries, queue(path))]


def _sort(path: Path, lines: list[str]) -> None:
    """Write the To review rows in the order `show` takes them."""
    columns, first, end = _table(path, lines, SECTION)
    rows = {_cells(line)[0]: line for line in lines[first:end]}
    named = [dict(zip(columns, (_plain(cell) for cell in _cells(line)))) for line in lines[first:end]]
    lines[first:end] = [rows[row["#"]] for row in ordered(named, path)]


def _refuse_image_problem(change: PublicationChange, reason: str) -> None:
    """Refuse an image failure when the caller has no scan diagnostic handler."""
    raise ChangeReviewError(f"{change.checkpoint[:SHORT]}: {reason}")


def append_rows(path: Path, changes: Sequence[Visible | PublicationChange], day: date, merged: str, production: Path,
                rank: str = "", owners: Sequence[Path] = (),
                producers: Sequence[staff.StaffId | UnknownProducer] = (), *,
                record: staff.StaffSnapshot, repo: Path = _CURRENT_REPO,
                on_hold: Callable[[PublicationChange], None] | None = None,
                merge_changes: Sequence[PublicationChange] = (), added_at: datetime | None = None,
                on_image_problem: Callable[[PublicationChange, str], None] = _refuse_image_problem) -> list[str]:
    """Add changes to To review with fixed staff IDs from record.

    owners supplies production documents; producers supplies fixed staff IDs for plain Visible changes.
    merge_changes supplies every change of this merge routed to this list.
    added_at puts the time of day after the date in the Merged cell.
    """
    if not changes:
        return []
    items = [change.item if isinstance(change, PublicationChange) else change for change in changes]
    for index, item in enumerate(items):
        _require_row_image(item.image, f"change {index + 1} ({item.change})")
    row_owners = [change.source.production if isinstance(change, PublicationChange) and isinstance(change.source, UnitSource)
                  else owners[index] if owners else production for index, change in enumerate(changes)]
    row_producers = [change.producer if isinstance(change, PublicationChange)
                     else producers[index] if producers else UnknownProducer() for index, change in enumerate(changes)]

    identities = [str(unit_lookup.doc_owner(owner, record).id)
                  if isinstance(change, PublicationChange)
                  else str(producer) if isinstance(producer, str) else row_production(owner, record)
                  for change, owner, producer in zip(changes, row_owners, row_producers)]
    unregistered: set[str] = set()
    pending_rows: list[str] = []

    def write(lines: list[str]) -> list[str]:
        existing = _changes("".join(lines)) if any(isinstance(change, PublicationChange) for change in changes) else []
        covered = _merge_covered(existing, merge_changes, repo, merged)
        selected: list[int] = []
        for index, change in enumerate(changes):
            if isinstance(change, PublicationChange):
                if change in covered or (not merge_changes and _covered(existing, change, repo)):
                    continue
                if isinstance(change.item.readiness, AfterStep) and on_hold is not None:
                    on_hold(change)
                    continue
                existing.append({"Change": change.item.change, "Merged": merged})
            selected.append(index)
        if not selected:
            return []
        number = next_number("".join(lines))
        row_images: dict[int, str] = {}
        for index in selected:
            try:
                row_images[index] = _row_image(items[index].image, f"C{number + len(row_images)}")
            except ChangeReviewError as error:
                change = changes[index]
                if not isinstance(change, PublicationChange):
                    raise
                on_image_problem(change, str(error))
        selected = list(row_images)
        if not selected:
            return []
        if rank:
            _add_column(path, lines, SECTION, URGENCY)
        _add_column(path, lines, SECTION, PRODUCTION)
        _add_column(path, lines, SECTION, PRODUCER)
        if any(row_images.values()):
            _add_column(path, lines, SECTION, IMAGE)
        columns, first, end = _table(path, lines, SECTION)
        plain = [change for change in changes if isinstance(change, Visible)]
        if merged and (held := _held(columns, lines[first:end], plain, merged)):
            raise ChangeReviewError(f"{held} already holds that change from merge {merged}; no row added")
        added = f"{day.isoformat()} {added_at:%H:%M}" if added_at is not None else day.isoformat()
        when = f"{added} · {_cell(merged)}" if merged else added
        rows = [_row(path, columns, {**dict.fromkeys(columns, ""),
                                     "#": f"C{number + offset}", "Change": _cell(items[index].change), "Merged": when,
                                     "On the Mac build": NOT_YET, CHECK: _cell(items[index].how()), URGENCY: _cell(rank),
                                     KIND: "", PRODUCER: str(row_producers[index]) if isinstance(row_producers[index], str) else "",
                                     PRODUCTION: _cell(identities[index]),
                                     IMAGE: row_images[index]})
                for offset, index in enumerate(selected)]
        _insert(lines, end, rows)
        pending_rows.extend(rows)
        unregistered.update(change.unit_name for index in selected
                            if isinstance(change := changes[index], PublicationChange)
                            and isinstance(change.producer, UnknownProducer) and change.unit_name)
        return rows

    def what(rows: list[str]) -> str:
        if not rows:
            return "no new rows"
        ids = [row_id(row) for row in rows]
        named = ids[0] if len(ids) == 1 else f"{ids[0]}–{ids[-1]}"
        if not merged:
            return f"add {named}"
        return f"{'row' if len(ids) == 1 else 'rows'} {named} from {merged}"

    def publish(path: Path, text: str) -> None:
        try:
            write_text_atomic(path, text)
        except AtomicDurabilityUnconfirmed as error:
            visible = [WrittenReviewRow(path, row) for row in pending_rows]
            raise ReviewListDurabilityUnconfirmed(path, visible, error) from error

    rows = _edit(path, production, day, write, what, publish=publish)
    for name in sorted(unregistered):
        print(f"change review: no staff row for unit {name}; Producer stays empty")
    return rows


def image_path(identity: str) -> str:
    """Return the public path of a change row's image, or an empty string."""
    if not identity:
        return ""
    return image_store.change_image(change_routing.review_root(), identity)


def _require_row_image(identity: str, row: str) -> None:
    """Refuse a visible row without an image."""
    if not identity:
        raise ChangeReviewError(f"{row}: a visible row requires an image; run change_review.py image --image <png> "
                                + "(or --output <text file>), then pass the id with --image")


def _row_image(identity: str, row: str) -> str:
    """Resolve a stored image before a row write."""
    if identity and image_path(identity):
        return identity
    if identity and (copied := image_store.copy_page_image(identity)):
        return copied
    _require_row_image("", f"{row} (image {identity} is unavailable)" if identity else row)
    return ""


def _covered(rows: list[dict[str, str]], change: PublicationChange, repo: Path) -> bool:
    """A matching row covers checkpoints that its merge already contains."""
    for row in rows:
        if row.get("Change") != change.item.change:
            continue
        merged = _HASH.search(row.get("Merged", ""))
        if merged is None:
            return True
        known = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{merged[0]}^{{commit}}"],
                               capture_output=True, check=False)
        if known.returncode:
            return True
        ancestor = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", change.checkpoint, merged[0]],
                                  capture_output=True, check=False)
        if ancestor.returncode == 0:
            return True
    return False


def _merge_covered(rows: list[dict[str, str]], changes: Sequence[PublicationChange],
                   repo: Path, merged: str) -> list[PublicationChange]:
    """Use text matches first, then pair merge rows with changes from the same known producer.

    For each producer, pair ready changes in commit order before held changes that all wait on one step.
    """
    covered: list[PublicationChange] = []
    used: set[int] = set()
    for change in changes:
        for index, row in enumerate(rows):
            if _covered([row], change, repo):
                covered.append(change)
                used.add(index)
                break
    producers: dict[str, list[PublicationChange]] = {}
    for change in changes:
        if change not in covered and isinstance(change.producer, str) and change.producer:
            producers.setdefault(change.producer, []).append(change)
    remaining: dict[str, Iterator[PublicationChange]] = {}
    for producer, uncovered in producers.items():
        ready = [change for change in uncovered if isinstance(change.item.readiness, ReadyNow)]
        held = [change for change in uncovered if isinstance(change.item.readiness, AfterStep)]
        steps = {change.item.readiness.step for change in held if isinstance(change.item.readiness, AfterStep)}
        remaining[producer] = iter(ready + (held if len(steps) == 1 else []))
    for index, row in enumerate(rows):
        found = _HASH.search(row.get("Merged", ""))
        if index not in used and found is not None and (found[0].startswith(merged) or merged.startswith(found[0])):
            candidates = remaining.get(row.get(PRODUCER, ""))
            if candidates is not None and (change := next(candidates, None)) is not None:
                covered.append(change)
    return covered


def _held(columns: list[str], rows: list[str], changes: list[Visible], merged: str) -> str:
    """The To review row that already holds one of `changes` from the merge `merged`, or empty. A short
    hash and a longer one of the same merge match: on 2026-10-22 rows written with 8-character hashes
    repeated rows written with 7 (C94-C99 of C74-C79)."""
    wanted = {item.change for item in changes}
    for line in rows:
        row = dict(zip(columns, (_plain(cell) for cell in _cells(line))))
        found = _HASH.search(row.get("Merged", ""))
        if (found is not None and row.get("Change") in wanted
                and (found[0].startswith(merged) or merged.startswith(found[0]))):
            return row["#"]
    return ""


def pending(path: Path) -> list[dict[str, str]]:
    """Each row still to review, as its cells by column name. Reads only."""
    lines = read(path).splitlines(keepends=True)
    return _review_table(path, lines, SECTION)


def scope_list(doc: Path, *, record: staff.StaffSnapshot | None = None,
               sources: list[Path] | None = None, prepared: PreparedChangeLists | None = None) -> Path:
    """The list that takes the production's rows: the one its Change review line names, else its root's."""

    if prepared is not None:
        record = prepared.routing.record
    lines = prepared.routing.document_lines.get(doc, []) if prepared is not None else None
    if doc.is_file() and (value := _line_value(doc, sources=sources, lines=lines)):
        return Path(value).expanduser()
    record = staff.snapshot() if record is None else record
    name = unit_lookup.doc_owner(doc, record).name
    if not doc.is_file():
        return list_file(name, sources=sources)
    root = showrunner_tree.root_of(name, record=record)
    if prepared is not None:
        for owner in (name, root):
            if owner in prepared.routing.list_paths:
                return prepared.routing.list_paths[owner]
        raise ChangeReviewError(f"{doc} has no `{LIST_HEADER}` line")
    try:
        return (production_list(name, record=record) if sources is None
                else production_list(name, record=record, sources=sources))
    except ChangeReviewError:
        if name == root:
            raise
        return (production_list(root, record=record) if sources is None
                else production_list(root, record=record, sources=sources))


@dataclass(frozen=True)
class ProductionReviewList:
    path: Path
    default_production: str


@dataclass(frozen=True)
class ScopedReviewEntry:
    review_list: ProductionReviewList
    entry: urgency.Entry[dict[str, str]]
    owner: change_routing.ChangeOwner


@dataclass(frozen=True)
class PreparedChangeLists:
    """Share the page's list reads and staff routing with the change queues."""

    snapshots: dict[Path, ReviewListSnapshot]
    routing: change_routing.StaffRouting

    @cached_property
    def routed(self) -> list[tuple[ProductionReviewList, change_routing.ChangeOwner, dict[str, str]]]:
        names = {path.name for path in change_routing.review_root().glob(".review-link-delivery-*")}
        lists = {path: ProductionReviewList(path, _owner(path)) for path in self.snapshots}
        return _route_review_snapshots(lists, self.snapshots, self.routing, names)


def scope_lists(path: Path, doc: Path, below: bool = False, *,
                record: staff.StaffSnapshot | None = None,
                sources: list[Path] | None = None) -> tuple[ProductionReviewList, ...]:
    """The current list and each scoped production's own earlier list, once per path."""
    record = staff.snapshot() if record is None else record
    owner = _owner(path) if path.name.startswith(PREFIX) else showrunner_of(doc, record=record)
    lists = {path.absolute(): ProductionReviewList(path, owner)}
    for production in scope_docs(doc, below, record=record):
        name = unit_lookup.doc_owner(production, record).name
        earlier = list_file(name, sources=sources)
        if earlier.absolute() in lists or earlier.exists():
            lists[earlier.absolute()] = ProductionReviewList(earlier, name)
    return tuple(lists.values())


def routed_review_entries(path: Path, doc: Path, below: bool = False,
                          record: staff.StaffSnapshot | None = None, *,
                          sources: list[Path] | None = None) -> list[tuple[ProductionReviewList, change_routing.ChangeOwner, dict[str, str]]]:
    """Read and route lists. A source collector requires complete reads."""
    return _routed_review_entries(path, doc, below, record if record is not None else staff.snapshot(), sources=sources)


def _routed_review_entries(path: Path, doc: Path, below: bool, record: staff.StaffSnapshot, *,
                           sources: list[Path] | None = None) -> list[tuple[ProductionReviewList, change_routing.ChangeOwner, dict[str, str]]]:
    """Route all rows with one consistent staff snapshot."""
    if sources is not None:
        sources.append(doc)
    listed = {item.path.absolute(): item for item in scope_lists(path, doc, below, record=record, sources=sources)}
    folder = lists_directory() if sources is None else lists_directory(sources=sources)
    for source in issues.source_paths(folder, PREFIX + "*.md", sources=sources):
        _ = listed.setdefault(source.absolute(), ProductionReviewList(source, _owner(source)))
    documents = {row.name: row.doc.path for row in record.find(kind="showrunner")
                 if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc))}
    snapshots = _review_snapshots(listed, sources=sources, include_reviewed=False)
    documents = change_routing.production_documents(documents,
        ((listed[source].default_production, snapshot.text) for source, snapshot in snapshots.items()))
    routing = change_routing.StaffRouting(record, documents, sources=sources)
    names = {path.name for path in issues.source_paths(
        change_routing.review_root(sources=sources), ".review-link-delivery-*", sources=sources)}
    return _route_review_snapshots(listed, snapshots, routing, names, sources=sources)


def _route_review_snapshots(listed: dict[Path, ProductionReviewList], snapshots: dict[Path, ReviewListSnapshot],
                            routing: change_routing.StaffRouting, names: set[str], *,
                            sources: list[Path] | None = None) -> list[tuple[ProductionReviewList, change_routing.ChangeOwner, dict[str, str]]]:
    """Route waiting rows from one set of list reads."""
    routed: list[tuple[ProductionReviewList, change_routing.ChangeOwner, dict[str, str]]] = []
    for source, snapshot in snapshots.items():
        rows = change_routing.waiting_rows(source, snapshot.pending, names, sources=sources)
        try:
            entries = row_entries(rows, sources=sources)
        except ChangeReviewError as error:
            if sources is not None:
                raise ValueError(str(error)) from error
            print(f"change review: {source}: {error}", file=sys.stderr)
            continue
        routed.extend((listed[source], routing.owner(entry.item, source), entry.item) for entry in entries)
    return routed


def review_scope(doc: Path, below: bool, record: staff.StaffSnapshot) -> set[str]:
    """Name the staff tabs counted for this production."""
    documents = set(scope_docs(doc, below, record=record))
    scope = {member.name for member in record.rows
             if member.kind == "showrunner" and isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc))
             and member.doc.path in documents}
    return scope


def scoped_review_entries(path: Path, doc: Path, below: bool = False, *,
                          prepared: PreparedChangeLists | None = None) -> list[ScopedReviewEntry]:
    """Use the same staff tab as the change page."""
    record = staff.snapshot() if prepared is None else prepared.routing.record
    scope = review_scope(doc, below, record)
    grouped: dict[Path, tuple[ProductionReviewList, list[dict[str, str]]]] = {}
    owners: dict[tuple[Path, str], change_routing.ChangeOwner] = {}
    if prepared is None:
        routed = _routed_review_entries(path, doc, below, record)
    else:
        lists = dict.fromkeys(item.path.absolute() for item in scope_lists(path, doc, below, record=record))
        lists.update(dict.fromkeys(source.absolute() for source in prepared.snapshots))
        by_list: dict[Path, list[tuple[ProductionReviewList, change_routing.ChangeOwner, dict[str, str]]]] = {}
        for item in prepared.routed:
            by_list.setdefault(item[0].path.absolute(), []).append(item)
        routed = [item for source in lists for item in by_list.get(source, [])]
    for review_list, owner, row in routed:
        if owner.tab in scope:
            if review_list.path not in grouped:
                grouped[review_list.path] = (review_list, [])
            grouped[review_list.path][1].append(row)
            owners[review_list.path, row["#"]] = owner
    found: list[ScopedReviewEntry] = []
    for review_list, rows in grouped.values():
        try:
            found.extend(ScopedReviewEntry(review_list, entry, owners[review_list.path, entry.key])
                         for entry in row_entries(rows))
        except ChangeReviewError as error:
            print(f"change review: {review_list.path}: {error}", file=sys.stderr)
    return found


def scope_rows(path: Path, doc: Path, below: bool = False) -> list[dict[str, str]]:
    """The production's open rows from its current list and its own earlier list.

    With `below`, include each lower production's rows and earlier list too.
    """
    return [row.entry.item for row in scoped_review_entries(path, doc, below)]


def scope_entries(path: Path, doc: Path, below: bool = False, *,
                  prepared: PreparedChangeLists | None = None) -> list[urgency.Entry[dict[str, str]]]:
    """Show the responsible showrunner while retaining list-based queue keys and prerequisites."""
    entries: list[urgency.Entry[dict[str, str]]] = []
    for scoped in scoped_review_entries(path, doc, below, prepared=prepared):
        entry = scoped.entry
        prefix = (f"{_owner(scoped.review_list.path)}/"
                  if scoped.review_list.path.absolute() != path.absolute() else "")
        key = prefix + entry.key
        after = prerequisite_key(entry.after, scoped.review_list.path, path)
        entries.append(replace(entry, key=key,
                               item=entry.item | {"#": display_id(entry.item, scoped.owner.tab)}, after=after))
    return entries


class WholeReviewList(Enum):
    """An explicit list shown without a production filter."""

    ALL_ROWS = "all rows"


@dataclass(frozen=True)
class ChangeQueue:
    """The change rows a person can review and the name that keeps their pin and moved rows."""

    name: str
    entries: list[urgency.Entry[dict[str, str]]]


def change_queue(path: Path, scope: Path | WholeReviewList, below: bool = False, *,
                 prepared: PreparedChangeLists | None = None) -> ChangeQueue:
    """The same change rows and pin for /next and change_review show."""
    if isinstance(scope, WholeReviewList):
        routing = review_routing((path,))
        entries = row_entries(change_routing.waiting_rows(path, pending(path)))
        return ChangeQueue(queue(path), [replace(entry, item=entry.item | {
            "#": display_id(entry.item, routing.owner(entry.item, path).tab)},
            after=prerequisite_key(entry.after, path, path)) for entry in entries])

    record = staff.snapshot() if prepared is None else prepared.routing.record
    name = unit_lookup.doc_owner(scope, record).name
    name = queue(path) if name == showrunner_tree.root_of(name, record=record) else f"{queue(path)}-{name}"
    return ChangeQueue(urgency.scope_queue(name, below), scope_entries(path, scope, below, prepared=prepared))


def row_production(production: Path, record: staff.StaffSnapshot) -> str:
    """Read the fixed showrunner id for a production-owned change."""
    return str(unit_lookup.doc_owner(credited(production.expanduser(), record), record).id)


def credited(production: Path, record: staff.StaffSnapshot) -> Path:
    """Credit a style production to its staff parent. Other productions keep their own credit."""
    row = record.by_doc(remote.local_machine(), production)
    while row is not None and isinstance(row.parent, staff.ReportsTo):
        try:
            if production_field(production.read_text(encoding="utf-8").splitlines(), "Kind") != "style":
                return production
        except (OSError, UnicodeError, Refusal):
            return production
        row = record.by_id(row.parent.id)
        if row is None or not isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            return production
        production = row.doc.path
    return production


@dataclass(frozen=True)
class MissingUnitWorktree:
    """The unit's branch worktree is unavailable."""


@dataclass(frozen=True)
class UnknownProducer:
    """The change has no known staff producer."""


UNKNOWN_PRODUCER = UnknownProducer()


@dataclass(frozen=True)
class UnitSource:
    """The production document and branch worktree of a unit."""

    production: Path
    worktree: Path | MissingUnitWorktree
    name: str
    producer: staff.StaffId | UnknownProducer = UnknownProducer()


def unit_branches(docs: Iterable[Path]) -> dict[str, UnitSource]:
    """Resolve exact branch names before plan, unit and folder aliases."""
    return _unit_branches(docs, _branch_snapshot())


def _branch_snapshot() -> staff.StaffSnapshot:
    """Return the staff snapshot used to resolve branches."""
    try:
        record = staff.snapshot()
    except staff.StaffUnreadable as error:
        print(f"change review: {error}", file=sys.stderr)
        record = staff.StaffSnapshot(())
    return record


def _unit_branches(docs: Iterable[Path], record: staff.StaffSnapshot) -> dict[str, UnitSource]:
    """Resolve branch names and aliases from the unit staff rows of each document."""
    return _branches_from_units(_document_units(docs, record), record)


def _document_units(docs: Iterable[Path], record: staff.StaffSnapshot) -> list[tuple[Path, staff.StaffRow]]:
    """Collect each document's units and report unreadable staff records."""
    pairs: list[tuple[Path, staff.StaffRow]] = []
    for doc in docs:
        try:
            rows = unit_lookup.doc_units(doc, record)
        except staff.StaffUnreadable as error:
            print(f"change review: {error}", file=sys.stderr)
            continue
        pairs.extend((doc, row) for row in rows)
    return pairs


def _branches_from_units(pairs: Iterable[tuple[Path, staff.StaffRow]],
                         record: staff.StaffSnapshot) -> dict[str, UnitSource]:
    """Resolve exact branches before aliases, in the supplied unit order."""
    found: dict[str, UnitSource] = {}
    aliases: list[tuple[UnitSource, tuple[str, ...]]] = []
    resolver = session_staff.RowResolver(record)
    machine = remote.local_machine()
    for doc, row in pairs:
        tree = row.worktree
        worktree = (tree.path if isinstance(tree, staff.PresentWorktree) and tree.machine == machine
                    else MissingUnitWorktree())
        source = UnitSource(doc, worktree, row.name, row.id)
        branch = resolver.unit_branch(row)
        if isinstance(branch, session_staff.Branch):
            _ = found.setdefault(branch.name, source)
        try:
            config = resolver.unit_config(row)
        except unit_config.ConfigRefused as error:
            print(f"change review: {source.name}: {error}", file=sys.stderr)
            config = unit_config.ConfigMissing(str(error))
        plan = (config.plan.path.stem if isinstance(config, unit_config.UnitConfig)
                and isinstance(config.plan, unit_config.PlanFile) else "")
        aliases.append((source, (plan, plan.removesuffix("-plan"), source.name,
                                 worktree.name if isinstance(worktree, Path) else "")))
    for source, names in aliases:
        for name in names:
            if name:
                _ = found.setdefault(name, source)
    return found


def _owner_of(commit: Commit, branches: dict[str, UnitSource]) -> Path | None:
    """The production of the unit whose branch the checkpoint names, or None for another commit."""
    found = _CHECKPOINT.match(commit.subject)
    unit = branches.get(found["branch"]) if found else None
    return unit.production if unit is not None else None


@dataclass(frozen=True)
class UnresolvedSource:
    """The unit commit has no matching unit source."""


@dataclass(frozen=True)
class PublicationChange:
    """A change from a unit commit, with its unit and fixed producer identity."""

    item: Visible
    source: UnitSource | UnresolvedSource
    producer: staff.StaffId | UnknownProducer
    checkpoint: str
    unit_name: str


def producer_at(directory: Path, record: staff.StaffSnapshot) -> staff.StaffId | UnknownProducer:
    """Read the fixed id of the unit that owns the requested directory."""
    machine = remote.local_machine()
    member = record.unit_by_worktree(machine, directory) or record.by_worktree(machine, directory)
    return member.id if member is not None and member.kind == "unit" else UnknownProducer()


def unit_producer(unit: str) -> staff.StaffId | UnknownProducer:
    """Read the producer of one unit across the staff record."""
    try:
        return unit_lookup.unit_row(unit, staff.snapshot()).id
    except staff.StaffUnreadable:
        return UNKNOWN_PRODUCER


def _publication_docs(production: Path, record: staff.StaffSnapshot) -> list[Path]:
    """Resolve child production documents with the held staff snapshot."""
    return scope_docs(production, below=True, record=record)


def checkpoint_producers(record: staff.StaffSnapshot) -> dict[str, tuple[staff.StaffId | UnknownProducer, str]]:
    """Read each checkpoint's recorded owner once for a publication scan."""
    recorded: dict[str, tuple[staff.StaffId | UnknownProducer, str]] = {}
    events, _ = load_events({"checkpoint"}, record=record)
    for event in events:
        checkpoint = event.get("checkpoint_commit")
        if not isinstance(checkpoint, str):
            continue
        owner = event.get("owner_staff_id")
        if isinstance(owner, str) and owner:
            producer: staff.StaffId | UnknownProducer = staff.StaffId(owner)
        else:
            owner_row = unit_runs.run_owner(event, record)
            unit = (owner_row if isinstance(owner_row, staff.StaffRow) and owner_row.kind == "unit"
                    else record.unit_by_worktree(remote.local_machine(), Path(str(event.get("working_dir", "")))))
            producer = unit.id if unit is not None else UNKNOWN_PRODUCER
        recorded[checkpoint] = producer, str(event.get("unit", ""))
    return recorded


def publication_changes(commits: list[Commit], production: Path, *,
                        record: staff.StaffSnapshot | None = None,
                        known_producer: staff.StaffId | UnknownProducer = UNKNOWN_PRODUCER,
                        recorded_producers: Mapping[str, tuple[staff.StaffId | UnknownProducer, str]] | None = None) -> list[PublicationChange]:
    """Use checkpoint owners first, then the producer recorded for this merge."""
    record = staff.snapshot() if record is None else record
    recorded = checkpoint_producers(record) if recorded_producers is None else recorded_producers
    machine = remote.local_machine()
    sources: dict[staff.StaffId, UnitSource] = {}
    for member in record.find(kind="unit"):
        tree = member.worktree
        owner = record.parent_of(member)
        doc = (owner.doc.path if owner is not None and isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
               and owner.doc.machine == machine else production)
        sources[member.id] = UnitSource(doc, tree.path if isinstance(tree, staff.PresentWorktree) and tree.machine == machine
                                       else MissingUnitWorktree(),
                                       member.name, member.id)
    changes: list[PublicationChange] = []
    for commit, item in _checkpoint_entries(commits):
        identity, name = recorded.get(commit.sha, (known_producer, ""))
        if isinstance(identity, UnknownProducer):
            identity = known_producer
        source = sources.get(identity) if isinstance(identity, str) else None
        name = source.name if source is not None else name
        changes.append(PublicationChange(item, source if source is not None else UnresolvedSource(), identity, commit.sha, name))
    return changes


def merge_commits(repo: Path, merge: str, *, excluded_refs: Sequence[str] = ()) -> list[Commit]:
    """The commits a merge brought in, without merges; none for a commit that is no merge."""
    parents = _git(repo, "rev-list", "--parents", "-n", "1", merge, "--").split()[1:]
    if len(parents) < 2:
        return []
    records = _git(repo, "log", "--topo-order", "--format=%x1e%H %ct %P%x1f%B", parents[1], f"^{parents[0]}",
                   *(f"^{ref}" for ref in excluded_refs), "--")
    commits: list[Commit] = []
    ancestry: dict[str, tuple[str, ...]] = {}
    for record in records.split("\x1e")[1:]:
        header, message = record.split("\x1f", 1)
        sha, stamp, *parents = header.split()
        ancestry[sha] = tuple(parents)
        if len(parents) < 2:
            commits.append(Commit(sha, message.split("\n", 1)[0], message, (), int(stamp), ancestry))
    return commits


def _found_repo(repos: Iterable[Path], sha: str) -> Path | None:
    """The first repository that holds the commit."""
    return next((repo for repo in repos if subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
        capture_output=True, check=False).returncode == 0), None)


@dataclass(frozen=True)
class EnrolledReviewCheckouts:
    main: Path
    integration: Path


def enrolled_review_checkouts(doc: Path, lines: list[str], *, record: staff.StaffSnapshot | None = None
                              ) -> EnrolledReviewCheckouts | LegacyProduction:
    """Read historical checkout paths from the exact wrapped document."""
    if not staff.declares_merge_checkout(lines):
        return LegacyProduction()
    record = staff.snapshot() if record is None else record
    machine = remote.local_machine()
    if any(isinstance(row.doc, staff.WrappedDoc) and row.doc.machine == machine and row.doc.path == doc.absolute()
           for row in record.find(kind="showrunner")):
        return EnrolledReviewCheckouts(Path(production_field(lines, MERGE_CHECKOUT_FIELD)).expanduser(),
                                       Path(production_field(lines, "Showrunner checkout")).expanduser())
    paths = resolve_production_paths(doc)
    if isinstance(paths, EnrolledPaths):
        return EnrolledReviewCheckouts(paths.main, paths.integration)
    raise ProductionPathsError(f"{doc}: enrollment changed during review")


class Tree:
    """The productions one list belongs to: the units they list, their repositories and merge branches."""

    def __init__(self, docs: Iterable[Path]) -> None:
        self.docs: list[Path] = list(dict.fromkeys(docs))
        self.record: staff.StaffSnapshot = _branch_snapshot()
        self.branches: dict[str, UnitSource] = _unit_branches(self.docs, self.record)
        self.repos: list[Path] = []
        self.targets: dict[tuple[Path, str], Path] = {}
        self._chains: dict[tuple[Path, str], set[str]] = {}
        for doc in self.docs:
            try:
                lines = doc.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            try:
                paths = enrolled_review_checkouts(doc, lines, record=self.record)
            except Refusal as error:
                raise ChangeReviewError(str(error)) from error
            if isinstance(paths, EnrolledReviewCheckouts):
                repositories = (paths.main, paths.integration)
            else:
                legacy: list[Path] = []
                for field in ("Repository", "Showrunner checkout"):
                    try:
                        legacy.append(Path(production_field(lines, field)).expanduser())
                    except Refusal:
                        continue
                repositories = tuple(legacy)
            for repo in repositories:
                if repo not in self.repos:
                    self.repos.append(repo)
                try:
                    _ = self.targets.setdefault((repo, production_field(lines, "Merge branch")), doc)
                except Refusal:
                    pass

    def landed_on(self, repo: Path, sha: str) -> Path | None:
        """The production whose merge branch has the commit on its first-parent line: where it merged.

        A child merges its parent's branch in and the parent merges the child's, so plain ancestry
        names both; the first-parent line names only the branch the merge was made on.
        """
        full = _git(repo, "rev-parse", f"{sha}^{{commit}}").strip()
        for (where, branch), doc in self.targets.items():
            if where != repo:
                continue
            if (where, branch) not in self._chains:
                try:
                    self._chains[(where, branch)] = set(_git(repo, "rev-list", "--first-parent", branch, "--").split())
                except ChangeReviewError:
                    self._chains[(where, branch)] = set()
            if full in self._chains[(where, branch)]:
                return doc
        return None


def matching_checkpoint_names(commits: list[Commit], change: str) -> tuple[str, ...]:
    """Match the change text before resolving a checkpoint name."""
    return tuple(dict.fromkeys(match["branch"] for commit in commits
                 if change in checkpoint_changes(commit.message)
                 and (match := _CHECKPOINT.match(commit.subject)) is not None))


def trace(row: dict[str, str], tree: Tree) -> Path | str:
    """The production of an older row, from its merge. In order: the checkpoint that names the row's
    change; else the one production of every checkpoint the merge brought in; else the production
    whose merge branch the merge landed on. A string says why it cannot be traced."""
    found = next((match.group(0) for column in HASH_COLUMNS
                  if (match := _HASH.search(row.get(column, ""))) is not None), None)
    if found is None:
        return "no merge hash"
    repo = _found_repo(tree.repos, found)
    if repo is None:
        return f"{found} is in no repository of this list's productions"
    commits = merge_commits(repo, found)
    change = row.get("Change", "")
    named = {source.production for name in matching_checkpoint_names(commits, change)
             if (source := tree.branches.get(name)) is not None}
    if len(named) == 1:
        return named.pop()
    every = {owner for commit in commits if (owner := _owner_of(commit, tree.branches)) is not None}
    if len(every) == 1:
        return every.pop()
    if (target := tree.landed_on(repo, found)) is not None:
        return target
    if every:
        return f"{found} brings in checkpoints of {len(every)} productions"
    return f"{found} brings in no checkpoint of a listed unit and lands on no listed merge branch"


def list_tree(path: Path, docs: Iterable[Path]) -> Tree:
    """The productions whose root keeps the list `path`, with every production below each root."""

    owned: list[Path] = []
    record = staff.snapshot()
    for doc in docs:
        name = unit_lookup.doc_owner(doc, record).name
        try:
            if showrunner_tree.root_of(name, record=record) != name or list_path(doc, record=record) != path:
                continue
        except OSError:
            continue
        owned.extend(owner_docs(doc, record=record))
    return Tree(owned)


def _fill_column(path: Path, lines: list[str], section: str, column: str,
                 values: dict[str, str]) -> set[str]:
    """Add a column and fill only its empty cells."""
    _add_column(path, lines, section, column)
    columns, first, end = _table(path, lines, section)
    at = columns.index(column)
    changed: set[str] = set()
    for index in range(first, end):
        cells = _cells(lines[index])
        if cells[0] in values and at < len(cells) and not cells[at]:
            cells[at] = _cell(values[cells[0]])
            lines[index] = _join_cells(cells) + "\n"
            changed.add(cells[0])
    return changed


def backfill(path: Path, tree: Tree, write: bool) -> tuple[dict[str, str], dict[str, str]]:
    """Fill each empty Production cell under To review from the row's merge; return the cells filled, by
    row id, and the rows that cannot be traced, with why. Without `write` the list is only read.

    `tree` holds the productions the list belongs to (`list_tree`); a row traces only to one of them.
    """
    filled: dict[str, str] = {}
    untraced: dict[str, str] = {}
    owners: dict[str, Path] = {}
    for row in pending(path):
        if row.get(PRODUCTION, "").strip():
            continue
        found = trace(row, tree)
        if isinstance(found, Path):
            filled[row["#"]], owners[row["#"]] = row_production(found, tree.record), found
        else:
            untraced[row["#"]] = found
    if not write or not filled:
        return filled, untraced

    def fill(lines: list[str]) -> None:
        _ = _fill_column(path, lines, SECTION, PRODUCTION, filled)

    _edit(path, next(iter(owners.values())), today(), fill, lambda _: f"production cells for {len(filled)} rows")
    return filled, untraced


_held_review_locks: ContextVar[frozenset[Path]] = ContextVar("held_review_locks", default=frozenset())


@contextmanager
def _list_lock(path: Path) -> Generator[None]:
    folder = path.parent.resolve()
    held = _held_review_locks.get()
    if folder in held:
        yield
        return
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        token = _held_review_locks.set(held | {folder})
        try:
            yield
        finally:
            _held_review_locks.reset(token)
    finally:
        os.close(descriptor)


@contextmanager
def review_delivery_lock(root: Path, *, wait: bool = True) -> Generator[file_lock.LockResult]:
    lock = root.resolve() / ".review-link-delivery.lock"
    held = _held_review_locks.get()
    if lock in held:
        yield file_lock.LockResult.ACQUIRED
        return
    with file_lock.exclusive(lock, wait=wait) as result:
        if result is file_lock.LockResult.BUSY:
            yield result
            return
        token = _held_review_locks.set(held | {lock})
        try:
            yield result
        finally:
            _held_review_locks.reset(token)


@contextmanager
def checked_review_write(path: Path, root: Path, check: Callable[[], Result]) -> Generator[Result]:
    """Check a Review card while holding its list and delivery locks."""
    with review_delivery_lock(root), _list_lock(path):
        yield check()


@contextmanager
def _image_reference_lock() -> Generator[None]:
    """Protect image checks, row writes, and image cleanup across all lists."""
    root = change_routing.review_root()
    root.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(root / ".change-image-references.lock"):
        yield


def describe(row: dict[str, str], *, record: staff.StaffSnapshot | None = None) -> str:
    """One row in plain lines: its id and change, how to check it, then each other column that has a value."""
    record = staff.initial_snapshot() if record is None else record
    shown = dict(row)
    member = record.by_id(staff.StaffId(row.get(PRODUCTION, "")))
    if member is not None and not row.get("Verdict"):
        parent = record.by_id(member.parent.id) if member.kind == "unit" and isinstance(member.parent, staff.ReportsTo) else member
        shown[PRODUCTION] = parent.name if parent is not None else member.name
    producer = record.by_id(staff.StaffId(row.get(PRODUCER, "")))
    if producer is not None:
        shown[PRODUCER] = producer.name
    rest = [f"  {column}: {value}" for column, value in shown.items() if column not in ("#", "Change", CHECK) and value]
    return "\n".join([f"{row.get('#', '')} {row.get('Change', '')}", f"  {CHECK}: {row.get(CHECK, '')}", *rest])


def lands(verdict: str) -> bool:
    """Land on an opening `works` or `fine`, after optional `okay`/`ok` and row id.
    Case and later words do not affect the result; any other start returns the row."""
    return _LANDS.match(verdict) is not None


def _to_review(path: Path, lines: list[str], row: str) -> tuple[list[str], int]:
    """The To review table's column names and the index of `row` in it; a row not there is refused."""
    columns, first, end = _table(path, lines, SECTION)
    index = next((index for index in range(first, end) if lines[index].startswith(f"| {row} |")), None)
    if index is None:
        raise ChangeReviewError(f"{row} is not on the list to review")
    return columns, index


@dataclass(frozen=True)
class KeepRowText:
    """Keep the current Change and How to check cells."""


@dataclass(frozen=True)
class RowWrite[Result]:
    """A row mutation and its work after the list lock is released."""

    write: Callable[[list[str]], Result]
    after: Callable[[], None]


def _remove_unused_image(path: Path, identity: str) -> None:
    """Remove an image only after all known review lists release it."""
    with _image_reference_lock():
        try:
            paths = {path, *path.parent.glob(PREFIX + "*.md"), *lists_directory().glob(PREFIX + "*.md")}
            for member in staff.initial_snapshot().find(kind="showrunner"):
                if isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc)) and member.doc.path.is_file():
                    named = _line_value(member.doc.path)
                    if named:
                        paths.add(Path(named).expanduser())
            if any(row.get(IMAGE) == identity for source in paths for row in review_rows(source)):
                return
        except (ChangeReviewError, OSError, UnicodeError) as error:
            _print_warning(f"image {identity} kept because a review list could not be checked: {error}")
            return
        image_store.remove_change(change_routing.review_root(), identity)


def _edit_write(path: Path, row: str, item: Visible | KeepRowText, image: str) -> RowWrite[str]:
    """Edit the selected text and image cells without changing the row's identity or position."""
    removed_image = ""

    def write(lines: list[str]) -> str:
        nonlocal removed_image
        columns, index = _to_review(path, lines, row)
        cells = dict(zip(columns, _cells(lines[index])))
        current_image = _plain(cells.get(IMAGE, ""))
        _require_row_image(image or current_image, row)
        resolved_image = _row_image(image or current_image, row)
        _add_column(path, lines, SECTION, IMAGE)
        columns, index = _to_review(path, lines, row)
        cells = dict(zip(columns, _cells(lines[index])))
        if isinstance(item, Visible):
            cells.update({"Change": _cell(item.change), CHECK: _cell(item.how())})
        removed_image = current_image if current_image != resolved_image else ""
        cells[IMAGE] = _cell(resolved_image)
        written = _row(path, columns, cells)
        lines[index] = written + "\n"
        return written

    def after() -> None:
        if removed_image:
            _remove_unused_image(path, removed_image)

    return RowWrite(write, after)


def edit_row(path: Path, row: str, item: Visible | KeepRowText, day: date, production: Path,
             *, image: str = "") -> str:
    """Edit a row and remove its previous image when no review row uses it."""
    change = _edit_write(path, row, item, image)
    written = _edit(path, production, day, change.write, lambda _: f"edit {row}")
    change.after()
    return written


def mark_row(path: Path, row: str, now: datetime, production: Path) -> str:
    """Mark `row` important to the user, keeping its prerequisite; return the row written."""

    def write(lines: list[str]) -> str:
        _add_column(path, lines, SECTION, URGENCY)
        columns, index = _to_review(path, lines, row)
        cells = dict(zip(columns, _cells(lines[index])))
        _, after = _rank({"#": row, URGENCY: _plain(cells.get(URGENCY, ""))})
        written = _row(path, columns, {**cells, URGENCY: _cell(urgency.cell(urgency.mark(now), after))})
        lines[index] = written + "\n"
        return written

    return _edit(path, production, now.date(), write, lambda _: f"mark {row} important")


@dataclass(frozen=True)
class RecordedMerge:
    text: str


@dataclass(frozen=True)
class MergeNotRecorded:
    pass


@dataclass(frozen=True)
class VerdictRecorded:
    merged: RecordedMerge | MergeNotRecorded


@dataclass(frozen=True)
class VerdictAlreadyRecorded:
    verdict: str
    day: str
    merged: RecordedMerge | MergeNotRecorded


@dataclass(frozen=True)
class VerdictConflict:
    recorded: str
    day: str


def row_merge(cells: dict[str, str]) -> RecordedMerge | MergeNotRecorded:
    text = _plain(cells.get("Merged", ""))
    return RecordedMerge(text) if text else MergeNotRecorded()


def _move_to_reviewed(path: Path, lines: list[str], cells: dict[str, str], verdict: str, day: date) -> None:
    """Write a closed row with its merge, history and recorded production."""
    identity = staff.initial_snapshot()
    member = identity.by_id(staff.StaffId(_plain(cells.get(PRODUCTION, ""))))
    if member is not None:
        parent = identity.by_id(member.parent.id) if member.kind == "unit" and isinstance(member.parent, staff.ReportsTo) else member
        cells[PRODUCTION] = _cell(parent.name if parent is not None else member.name)
    if not any(line.strip() == REVIEWED for line in lines):
        reviewed_rows = list[str](("\n" + BODY[BODY.index(REVIEWED):]).splitlines(keepends=True))
        _insert(lines, len(lines), reviewed_rows)
    _add_column(path, lines, REVIEWED, "Merged")
    for column in ("Review log", IMAGE):
        if cells.get(column):
            _add_column(path, lines, REVIEWED, column)
    if member is not None:
        _add_column(path, lines, REVIEWED, PRODUCTION)
    if PRODUCER in cells:
        _add_column(path, lines, REVIEWED, PRODUCER)
    if cells.get(KIND):
        for column in (KIND, CHECK, PRODUCTION):
            _add_column(path, lines, REVIEWED, column)
    reviewed, top, _ = _table(path, lines, REVIEWED)
    _insert(lines, top, [_row(path, reviewed, {**dict.fromkeys(reviewed, ""), **cells,
                                               "Verdict": _cell(verdict), "Date": day.isoformat()})])


def _verdict_write(
    path: Path, row: str, verdict: str, day: date, *, superseded: str = '',
) -> RowWrite[VerdictRecorded | VerdictAlreadyRecorded | VerdictConflict]:
    """Record a verdict once. Retain its merge and the result of an earlier write."""
    removed_image = ""

    def write(lines: list[str]) -> VerdictRecorded | VerdictAlreadyRecorded | VerdictConflict:
        nonlocal removed_image
        columns, first, end = _table(path, lines, SECTION)
        index = next((index for index in range(first, end) if lines[index].startswith(f"| {row} |")), None)
        if index is None:
            reviewed, first, end = _table(path, lines, REVIEWED)
            index = next((index for index in range(first, end) if lines[index].startswith(f"| {row} |")), None)
            if index is None:
                raise ChangeReviewError(f"{row} is not on the list to review")
            cells = dict(zip(reviewed, _cells(lines[index])))
            if cells.get("Verdict", "") == _cell(verdict):
                return VerdictAlreadyRecorded(verdict, _plain(cells.get("Date", "")), row_merge(cells))
            return VerdictConflict(_plain(cells.get("Verdict", "")), _plain(cells.get("Date", "")))
        cells = dict(zip(columns, _cells(lines.pop(index))))
        image = _plain(cells.get(IMAGE, ""))
        if image:
            removed_image = image
            cells[IMAGE] = ""
        if superseded:
            append_row_review_log(cells, superseded, "nate", datetime.now().astimezone(user_zone()))
        _move_to_reviewed(path, lines, cells, verdict, day)
        return VerdictRecorded(row_merge(cells))

    def after() -> None:
        if removed_image:
            _remove_unused_image(path, removed_image)

    return RowWrite(write, after)


def record_verdict(
    path: Path, row: str, verdict: str, day: date, production: Path, *,
    report_warning: Callable[[str], None] = _print_warning,
    publish: Callable[[Path, str], None] | None = None, superseded: str = '',
    commit_warning: Callable[[str], None] | None = None,
) -> VerdictRecorded | VerdictAlreadyRecorded | VerdictConflict:
    """Record a verdict once and remove its image when no row uses it."""
    change = _verdict_write(path, row, verdict, day, superseded=superseded)
    result = _edit(path, production, day, change.write, lambda _: f"verdict {row}",
                   report_warning=commit_warning or report_warning, publish=publish)
    try:
        change.after()
    except Exception as error:
        report_warning(f"{path} {row}: the verdict is saved. Image cleanup failed: {error}")
    return result


def withdraw_row(path: Path, row: str, words: str, stamp: datetime, production: Path) -> bool:
    """Close a row as withdrawn and keep its earlier verdict in the log."""
    prefix = "Withdrawn: "
    event = review_events.ReviewLog(stamp, "nate", prefix + _words("reason", words))
    verdict = "withdrawn"

    def write(lines: list[str]) -> bool:
        for section in (SECTION, REVIEWED):
            if not any(line.strip() == section for line in lines):
                continue
            columns, first, end = _table(path, lines, section)
            index = next((index for index in range(first, end) if lines[index].startswith(f"| {row} |")), None)
            if index is None:
                continue
            cells = dict(zip(columns, _cells(lines[index])))
            events = review_events.decode(_plain(cells.get("Review log", "")))
            previous = _plain(cells.get("Verdict", ""))
            if previous == verdict:
                for earlier in reversed(events):
                    if earlier.who == event.who and earlier.words.startswith(prefix):
                        if earlier.words == event.words:
                            return False
                        raise ChangeReviewError(f"{row} is already withdrawn")
            try:
                slots = change_routing.delivery_slots(delivery, strict=True)
            except (OSError, ValueError) as error:
                raise ChangeReviewError(f"{row}: cannot read its verdict delivery record: {error}") from error
            if isinstance(slots.verdict, delivery_state.DeliveryIntent):
                raise ChangeReviewError(f"{row} has a pending verdict message. Withdraw it after delivery.")
            if previous:
                previous_date = _plain(cells.get("Date", ""))
                try:
                    previous_time = datetime.fromisoformat(previous_date)
                    if previous_time.tzinfo is None:
                        previous_time = previous_time.replace(tzinfo=stamp.tzinfo)
                except ValueError:
                    previous_time = stamp
                    previous = f"Previous verdict ({previous_date or 'date unavailable'}) — {previous}"
                events = (*events, review_events.ReviewLog(previous_time, event.who, previous))
            cells["Review log"] = _cell(review_events.encode((*events, event)))
            if section == SECTION:
                del lines[index]
                _move_to_reviewed(path, lines, cells, verdict, stamp.date())
            else:
                _add_column(path, lines, REVIEWED, "Review log")
                reviewed, _, _ = _table(path, lines, REVIEWED)
                cells.update({"Verdict": verdict, "Date": stamp.date().isoformat()})
                lines[index] = _row(path, reviewed, cells) + "\n"
            return True
        raise RowNotListed(f"{row} is not on the change review list")

    root = change_routing.review_root()
    root.mkdir(parents=True, exist_ok=True)
    delivery = change_routing.delivery_path(root, path, row)
    with review_delivery_lock(root):
        return _edit(path, production, stamp.date(), write, lambda _: f"withdraw {row}")


def review_rows(path: Path) -> list[dict[str, str]]:
    """Read pending and saved page answers from the authoritative list."""
    snapshot = review_snapshot(path)
    return [*snapshot.pending, *snapshot.reviewed]


def _review_table(path: Path, lines: list[str], section: str) -> list[dict[str, str]]:
    """Decode one review table and refuse rows with missing or extra cells."""
    columns, first, end = _table(path, lines, section)
    result: list[dict[str, str]] = []
    for line in lines[first:end]:
        cells = _cells(line)
        if len(cells) != len(columns):
            raise ChangeReviewError(f"{path}: {cells[0] if cells else line.rstrip()} has the wrong number of cells")
        result.append(dict(zip(columns, map(_plain, cells))))
    return result


@dataclass(frozen=True)
class ReviewListSnapshot:
    """One list read, with its metadata and both review tables."""

    text: str
    pending: list[dict[str, str]]
    reviewed: list[dict[str, str]]
    owner_rows: list[dict[str, str]] = field(default_factory=list)


def review_snapshot(path: Path, *, include_reviewed: bool = True, collect_owners: bool = False) -> ReviewListSnapshot:
    """Read selected tables and retain only owner fields from excluded history."""
    owner_rows: list[dict[str, str]] = []
    if include_reviewed or not path.exists():
        text = read(path)
    else:
        lines: list[str] = []
        reviewed = False
        columns: list[str] = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip() == REVIEWED:
                    reviewed = True
                    lines.append(line)
                    continue
                if not reviewed:
                    lines.append(line)
                elif line.startswith("| # |"):
                    columns = _cells(line)
                    lines.append(line)
                elif line.startswith("| ---"):
                    lines.append(line)
                elif columns and line.startswith("| "):
                    cells = _cells(line)
                    owner = {column:_plain(value) for column,value in zip(columns,cells)
                             if column in ("#", "Producer", "Production", "Verdict")}
                    if collect_owners:
                        owner_rows.append(owner)
                    if not owner.get("Verdict"):
                        lines.append(line)
        text = "".join(lines)
    lines = text.splitlines(keepends=True)
    pending = _review_table(path, lines, SECTION)
    history = _review_table(path, lines, REVIEWED) if any(line.strip() == REVIEWED for line in lines) else []
    return ReviewListSnapshot(text, pending, history, owner_rows)


def _review_snapshots(paths: Iterable[Path], *, sources: list[Path] | None = None,
                      include_reviewed: bool = True) -> dict[Path, ReviewListSnapshot]:
    """Report unreadable lists once. A source collector requires complete reads."""
    snapshots: dict[Path, ReviewListSnapshot] = {}
    for path in paths:
        if sources is not None:
            sources.append(path)
        try:
            snapshots[path] = review_snapshot(path) if include_reviewed else review_snapshot(path, include_reviewed=False)
        except (ChangeReviewError, OSError, UnicodeError) as error:
            if sources is not None:
                raise ValueError(str(error)) from error
            print(f"change review: {path}: {error}", file=sys.stderr)
    return snapshots


def append_row_review_log(cells: dict[str, str], words: str, who: str, now: datetime) -> None:
    events = review_events.decode(_plain(cells.get("Review log", "")))
    event = review_events.ReviewLog(now, who, words)
    cells["Review log"] = _cell(review_events.encode(review_events.append(events, event)))


def _append_review_log(path: Path, lines: list[str], row: str, event: review_events.ReviewLog) -> None:
    for section in (SECTION, REVIEWED):
        if not any(line.strip() == section for line in lines):
            continue
        columns, first, end = _table(path, lines, section)
        if not any(line.startswith(f"| {row} |") for line in lines[first:end]):
            continue
        _add_column(path, lines, section, "Review log")
        columns, first, end = _table(path, lines, section)
        index = next(index for index in range(first, end) if lines[index].startswith(f"| {row} |"))
        cells = dict(zip(columns, _cells(lines[index])))
        append_row_review_log(cells, event.words, event.who, event.time)
        lines[index] = _row(path, columns, cells)+"\n"
        return


def record_review_log(path: Path, row: str, words: str, production: Path, who: str = "nate") -> None:
    """Keep the user's Review action in the authoritative table row."""
    now = datetime.now().astimezone(user_zone())
    event = review_events.ReviewLog(now, who, words)
    _edit(path, production, now.date(), lambda lines: _append_review_log(path, lines, row, event),
          lambda _: f"review {row}")


def set_row_kind(path: Path, row: str, kind: str, production: Path) -> str:
    """Classify by whether the change alters something the user runs or sees."""
    if kind not in REVIEW_KINDS:
        raise ChangeReviewError("--kind must be 'See in action' or 'Read only'")

    def write(lines: list[str]) -> str:
        _add_column(path, lines, SECTION, KIND)
        columns, index = _to_review(path, lines, row)
        cells = dict(zip(columns, _cells(lines[index])))
        written = _row(path, columns, {**cells, KIND: kind})
        lines[index] = written + "\n"
        return written

    return _edit(path, production, today(), write, lambda _: f"kind {row}")


def end_row_scratch(path: Path, row: str) -> list[str]:
    """End the scratch task of `row` and return any error.

    A verdict ends the row either way: a row returned to its unit comes back as a new row.
    """
    try:
        _ = scratch.end(f"row:{_owner(path)}-{row}")
        return []
    except (conf.Unusable, scratch.ScratchError, OSError) as error:
        return [f"scratch not removed: {error}"]


class VerdictDurabilityUnconfirmed(RuntimeError):
    """The visible verdict needs a directory sync or a Git commit."""

    def __init__(self, cleanup: PendingVerdictCleanup, reason: str) -> None:
        super().__init__(f"{cleanup.path} {cleanup.row}: {reason}")
        self.cleanup: PendingVerdictCleanup = cleanup


@dataclass(frozen=True)
class NoVerdictCleanup:
    """The verdict has no scratch cleanup left."""


NO_VERDICT_CLEANUP = NoVerdictCleanup()
VERDICT_CLEANUP_PREFIX = ".review-link-scratch-"


@dataclass(frozen=True)
class WaitSupersessionDestination:
    """The production and log selected before a verdict replaces a wait."""

    owner: staff.StaffId
    document: ProductionDocument

    def fields(self) -> dict[str, str]:
        doc = self.document
        return {'owner':self.owner, 'doc':str(doc.doc), 'name':doc.name, 'merge_branch':doc.merge_branch,
                'checkout':str(doc.checkout), 'log':str(doc.log), 'zone':str(doc.zone)}

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> WaitSupersessionDestination:
        return cls(staff.StaffId(fields['owner']), ProductionDocument(
            Path(fields['doc']), fields['name'], fields['merge_branch'], Path(fields['checkout']),
            Path(fields['log']), ZoneInfo(fields['zone'])))


@dataclass(frozen=True)
class NoSavedWaitLogDestination:
    """The intent has no saved destination, including intents from older versions."""


NO_SAVED_WAIT_LOG_DESTINATION = NoSavedWaitLogDestination()


@dataclass(frozen=True)
class PendingVerdictCleanup:
    """Remove the saved row's scratch folders after the response."""

    path: Path
    row: str
    intent: Path
    verdict: str = ''
    feature_review: str = ''
    superseded_wait: str = ''
    wait_log: WaitSupersessionDestination | NoSavedWaitLogDestination = NO_SAVED_WAIT_LOG_DESTINATION
    require_git_commit: bool = True

    @classmethod
    def for_row(cls, root: Path, path: Path, row: str) -> PendingVerdictCleanup:
        name = change_routing.delivery_name(path.resolve(), row)
        return cls(path, row, root / name.replace('.review-link-delivery-', VERDICT_CLEANUP_PREFIX, 1))

    @classmethod
    def load(cls, intent: Path, expected_directory: Path | None = None) -> PendingVerdictCleanup:
        """Read the exact list, row and verdict from a private cleanup record."""
        with os.fdopen(os.open(intent, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), encoding='utf-8') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ChangeReviewError(f"{intent}: scratch cleanup record must be private")
            fields = cast(dict[str, object], json.load(source))
        path = Path(cast(str, fields['list']))
        row = _row_id('row', cast(str, fields['row']))
        if path != review_list_source(path) or (expected_directory is not None and path.parent != expected_directory):
            raise ChangeReviewError(f"{intent}: invalid scratch cleanup source")
        cleanup = replace(cls.for_row(intent.parent, path, row),
                          verdict=_words('verdict', cast(str, fields['verdict'])),
                          feature_review=cast(str, fields.get('feature_review', '')),
                          superseded_wait=cast(str, fields.get('superseded_wait', '')),
                          require_git_commit=fields.get('require_git_commit') is not False)
        if cleanup.intent != intent:
            raise ChangeReviewError(f"{intent}: invalid scratch cleanup source")
        if 'wait_log' in fields:
            cleanup = replace(cleanup, wait_log=WaitSupersessionDestination.from_fields(
                cast(dict[str, str], fields['wait_log'])))
        if fields.get('supersedes_wait') is True and not cleanup.superseded_wait:
            from scripts.review_links import review_events
            for saved in review_snapshot(path).reviewed:
                if saved['#'] == row and saved['Verdict'] == cleanup.verdict:
                    events = review_events.decode(saved.get('Review log', ''))
                    reason = next((event.words for event in reversed(events)
                                   if event.words.startswith('Review ')
                                   and event.words.endswith(' before the answer arrived.')), '')
                    cleanup = replace(cleanup, superseded_wait=reason)
                    break
        return cleanup

    def commit_failed(self, reason: str) -> None:
        raise VerdictDurabilityUnconfirmed(self, reason)

    def sync(self) -> None:
        try:
            sync_folder(self.path.parent)
        except OSError as error:
            raise VerdictDurabilityUnconfirmed(self, f'directory sync failed: {error}') from error

    def confirm_durability(self) -> None:
        """Confirm the visible verdict before recovery performs its closeout."""
        self.sync()
        _commit([self.path], f'{_owner(self.path)} verdict {self.row}',
                report_warning=self.commit_failed if self.require_git_commit else _print_warning)

    def save(self) -> None:
        fields: dict[str, object] = {
            'list':str(self.path.resolve()), 'row':self.row, 'verdict':self.verdict,
            'feature_review':self.feature_review, 'superseded_wait':self.superseded_wait}
        if not self.require_git_commit:
            fields['require_git_commit'] = False
        if isinstance(self.wait_log, WaitSupersessionDestination):
            fields['wait_log'] = self.wait_log.fields()
        write_text_atomic(self.intent, json.dumps(fields)+'\n', new_file_mode=0o600)

    def finish(self, closeout: Callable[[PendingVerdictCleanup], None]) -> list[str]:
        try:
            with file_lock.exclusive(self.intent.parent/'.review-link-scratch.lock'):
                if not self.intent.exists():
                    return []
                closeout(self.load(self.intent, self.path.parent.resolve()))
                lines = end_row_scratch(self.path, self.row)
                if not any(line.startswith('scratch not removed:') for line in lines):
                    self.intent.unlink(missing_ok=True)
                return lines
        except Exception as error:
            return [f"{self.path} {self.row}: the verdict is saved. Verdict follow-up failed: {error}"]


@dataclass(frozen=True)
class VerdictSaved:
    """The output lines for a saved or repeated verdict."""

    lines: list[str]
    warnings: list[str] = field(default_factory=list)
    cleanup: PendingVerdictCleanup | NoVerdictCleanup = NoVerdictCleanup()


@dataclass(frozen=True)
class VerdictRefused:
    """The output lines and refusal for a conflicting verdict."""

    lines: list[str]
    reason: str
    warnings: list[str] = field(default_factory=list)


def _require_verdict_exchange(slots: delivery_state.DeliverySlots, verdict: str, validation_id: str) -> None:
    """Apply the same exchange checks to a single verdict and a batch verdict."""
    try:
        if isinstance(slots.question, delivery_state.OpenQuestion) and not slots.validation_requested:
            _ = delivery_state.question_exchange_for_reply(slots.question, '', verdict)
        if validation_id or slots.validation_requested:
            delivery_state.require_exchange_id(
                slots.validation if isinstance(slots.validation, delivery_state.NoDelivery) else slots.validation.attempt,
                validation_id)
    except delivery_state.StaleQuestionExchange as error:
        raise ChangeReviewError(str(error)) from error


def _validation_fault_event(production: Path, verdict: str) -> review_events.ReviewLog:
    owner = unit_lookup.doc_owner(production, staff.snapshot()).name
    return review_events.ReviewLog(datetime.now(user_zone()), owner, "Check found a problem: "+verdict)


@contextmanager
def clear_change_later(path: Path, row: str) -> Generator[None, None, None]:
    """Clear the Later time before publishing the change state."""
    target = review_later.path()
    if not target.is_file():
        yield
        return
    key = f"{_owner(path)}/{row}"
    with file_lock.exclusive(target.with_suffix('.lock')):
        stored = review_later.times()
        if key not in stored:
            yield
            return
        encoded = {key: until.isoformat() for key, until in stored.items()}
        until = encoded.pop(key)
        try:
            write_text_atomic(target, json.dumps(encoded) + '\n')
        except AtomicDurabilityUnconfirmed:
            encoded[key] = until
            write_text_atomic(target, json.dumps(encoded) + '\n')
            raise
        try:
            yield
        except AtomicDurabilityUnconfirmed:
            raise
        except OSError:
            encoded[key] = until
            write_text_atomic(target, json.dumps(encoded) + '\n')
            raise


def _cleared_validation(delivery: Path, slots: delivery_state.DeliverySlots) -> str:
    fields = cast(dict[str, object], json.loads(delivery.read_text()))
    # A null slot prevents older fields from restoring the completed validation.
    fields["validation"] = None
    if isinstance(slots.question, delivery_state.OpenQuestion):
        fields["question"] = None
    return json.dumps(fields)+"\n"


def _validation_fault_output(shown: str) -> list[str]:
    return [f"{shown}: the check found a problem. The change returned to Open."]


def save_verdict(path: Path, row: str, verdict: str, production: Path, *, validation_id: str = '',
                 cleanup: PendingVerdictCleanup | NoVerdictCleanup = NO_VERDICT_CLEANUP,
                 superseded: str = '') -> VerdictSaved | VerdictRefused:
    """Check the exchange and save the verdict under the same delivery lock."""
    verdict = _words("verdict", verdict)
    local_row = _row_id("row", row.rpartition("/")[2])
    shown = row_display_id(path, local_row)
    warnings: list[str] = []
    root = change_routing.review_root()
    root.mkdir(parents=True, exist_ok=True)
    delivery = change_routing.delivery_path(root, path, local_row)
    with review_delivery_lock(root):
        slots = change_routing.delivery_slots(delivery)
        if superseded and isinstance(cleanup, PendingVerdictCleanup):
            slots = replace(slots, validation=delivery_state.NoDelivery(), question=delivery_state.NoQuestion())
        try:
            _require_verdict_exchange(slots, verdict, validation_id)
        except ChangeReviewError as error:
            return VerdictRefused([], str(error))
        if slots.validation_requested and not lands(verdict) and any(item["#"] == local_row for item in pending(path)):
            cleared = _cleared_validation(delivery, slots)
            event = _validation_fault_event(production, verdict)
            _edit(path, production, event.time.date(), lambda lines: _append_review_log(path, lines, local_row, event),
                  lambda _: f"review {local_row}", report_warning=warnings.append)
            with clear_change_later(path, local_row):
                write_text_atomic(delivery, cleared)
            return VerdictSaved(_validation_fault_output(shown), warnings)
        def publish(target: Path, text: str) -> None:
            assert isinstance(cleanup, PendingVerdictCleanup)
            mode = stat.S_IMODE(target.stat().st_mode)
            with prepared_replacement(target, text, mode=mode) as temporary:
                os.replace(temporary, target)
                try:
                    cleanup.sync()
                except VerdictDurabilityUnconfirmed:
                    cleanup.sync()

        if isinstance(cleanup, NoVerdictCleanup):
            result = record_verdict(path, local_row, verdict, today(), production, report_warning=warnings.append)
        else:
            result = record_verdict(path, local_row, verdict, today(), production, report_warning=warnings.append,
                                    publish=publish, superseded=superseded,
                                    commit_warning=cleanup.commit_failed if cleanup.require_git_commit else warnings.append)
    return _verdict_output(path, local_row, shown, verdict, result, warnings, cleanup=cleanup)


def _verdict_output(path: Path, local_row: str, shown: str, verdict: str,
                    result: VerdictRecorded | VerdictAlreadyRecorded | VerdictConflict,
                    warnings: list[str], *,
                    cleanup: PendingVerdictCleanup | NoVerdictCleanup = NO_VERDICT_CLEANUP) -> VerdictSaved | VerdictRefused:
    """Make verdict output and end the row's scratch folders after its write."""
    lines: list[str] = []
    if isinstance(result, VerdictConflict):
        reason = (f'change review: {shown} has the verdict "{result.recorded}" from {result.day}; '
                  + 'this verdict is not recorded')
        return VerdictRefused(end_row_scratch(path, local_row), reason, warnings)
    if isinstance(result, VerdictAlreadyRecorded):
        lines.append(f"{shown} already has this verdict ({result.day}); the list is unchanged")
    merged = result.merged.text if isinstance(result.merged, RecordedMerge) else "not recorded"
    lines.append(f"{shown} landed" if lands(verdict) else f"{shown} returns to its unit (merged {merged})")
    if isinstance(cleanup, PendingVerdictCleanup):
        return VerdictSaved(lines, warnings, cleanup)
    try:
        lines.extend(end_row_scratch(path, local_row))
    except Exception as error:
        warnings.append(f"{path} {local_row}: the verdict is saved. Scratch cleanup failed: {error}")
    return VerdictSaved(lines, warnings)


def hold_for_row(path: Path, row: str, folders: list[str]) -> list[str]:
    """Hold each folder the user checks `row` in until the row's verdict; return one line per folder."""
    lines: list[str] = []
    for folder in folders:
        try:
            task = scratch.hold(Path(folder).expanduser(), f"row:{_owner(path)}-{row}", f"change review {row}")
        except (scratch.ScratchError, OSError) as error:
            lines.append(f"not held: {error}")
            continue
        lines.append(f"held {folder} for {task}")
    return lines


def production_doc(production: str, *, record: staff.StaffSnapshot | None = None) -> Path:
    """The production doc given; without one, the calling showrunner's."""

    if production:
        row = showrunners.owner_for(production, record)
        if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            return row.doc.path
        raise ChangeReviewError(f"{row.name}: no production document")
    found = showrunners.production_of(staff.runtime_id(), record=record)
    if found is None:
        raise ChangeReviewError("this session is no production's showrunner; pass --production <name>")
    return found


def showrunner_of(doc: Path, *,
                  record: staff.StaffSnapshot | None = None) -> str:
    """Read the staff name of the root showrunner."""

    record = staff.snapshot() if record is None else record
    row = unit_lookup.doc_owner(doc, record)
    return showrunner_tree.root_of(row.name, record=record)


def production_list(production: str, *, record: staff.StaffSnapshot | None = None,
                    sources: list[Path] | None = None) -> Path:
    """Return the review list for the staff name, or for the calling showrunner when the name is empty."""
    doc = production_doc(production, record=record)
    path = list_path(doc, record=record) if sources is None else list_path(doc, record=record, sources=sources)
    if path is None:
        raise ChangeReviewError(f"{doc} has no `{LIST_HEADER}` line")
    return path


def _words(name: str, value: str) -> str:
    """One line of words for a cell; empty or several lines is refused."""
    if not value.strip() or "\n" in value or "\r" in value:
        raise ChangeReviewError(f"--{name} needs one line of words")
    return value.strip()


@dataclass(frozen=True)
class KnownMergeProducer:
    """The unit identity supplied by the checkpoint request for one merge."""

    merge: str
    producer: staff.StaffId


def record_merge_producer(known: KnownMergeProducer) -> None:
    """Store the producer supplied by the request when its merge is made."""
    path = unit_runs.history_root() / "merge_producers" / f"{known.merge}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({"producer": known.producer}) + "\n")


def merge_producer(merge: str) -> staff.StaffId | UnknownProducer:
    """Read the producer recorded when this merge was made."""
    path = unit_runs.history_root() / "merge_producers" / f"{merge}.json"
    if not path.exists():
        return UNKNOWN_PRODUCER
    saved = cast(dict[str, str], json.loads(path.read_text(encoding="utf-8")))
    return staff.StaffId(saved["producer"])


@dataclass(frozen=True)
class ReadinessScan:
    """Scan published merges for changes that need no further step."""


@dataclass(frozen=True)
class CompletedStep:
    merged: str
    step: str


_READINESS_SCAN = ReadinessScan()


def _publication_exclusions(repo: Path, merge: str, refs: Iterable[str]) -> list[str]:
    """Exclude foreign history before each branch imports this merge."""
    merge = _git(repo, "rev-parse", merge).strip()
    excluded: set[str] = set()
    for ref in refs:
        path = [line.split() for line in _git(
            repo, "rev-list", "--ancestry-path", "--parents", f"{merge}..{ref}").splitlines()]
        if not path:
            if _git(repo, "rev-parse", ref).strip() != merge:
                excluded.add(ref)
            continue
        ancestry = {commit[0]: commit[1:] for commit in path}
        current = path[0][0]
        while current in ancestry:
            parent = ancestry[current][0]
            if parent != merge and parent not in ancestry:
                excluded.add(parent)
            current = parent
    return sorted(excluded)


def _staff_descendant(row: staff.StaffRow, ancestor: staff.StaffRow, record: staff.StaffSnapshot) -> bool:
    """Test descent through the held staff parents."""
    seen: set[staff.StaffId] = set()
    parent = record.parent_of(row)
    while parent is not None and parent.id not in seen:
        if parent.id == ancestor.id:
            return True
        seen.add(parent.id)
        parent = record.parent_of(parent)
    return False


def _foreign_merges(repo: Path, refs: Mapping[Path, str], production: Path,
                    merge_branch: str, record: staff.StaffSnapshot) -> frozenset[str]:
    """Exclude descendant merges only when the scanning branch does not share them."""
    owner = unit_lookup.doc_owner(production, record)
    own_merges = set(_git(repo, "rev-list", "--first-parent", "--merges",
                          f"origin/{merge_branch}", "--").split())
    foreign: set[str] = set()
    for doc, ref in refs.items():
        merges = set(_git(repo, "rev-list", "--first-parent", "--merges",
                          f"--since={READY_FROM}T00:00:00Z", ref, "--").split())
        if _staff_descendant(unit_lookup.doc_owner(doc, record), owner, record):
            merges.difference_update(own_merges)
        foreign.update(merges)
    return frozenset(foreign)


@dataclass(frozen=True)
class WrittenReviewRow:
    list_path: Path
    markdown: str


class ReviewListWriteFailed(ChangeReviewError):
    """A list write failed after zero or more rows were saved."""

    def __init__(self, list_path: Path, rows: list[WrittenReviewRow], error: Exception) -> None:
        super().__init__(str(error))
        self.list_path: Path = list_path
        self.written_rows: tuple[WrittenReviewRow, ...] = tuple(rows)


class ReviewListDurabilityUnconfirmed(ReviewListWriteFailed):
    """The new rows are visible after the write, but the directory sync failed."""


def list_ready(repo: Path, production: Path, merge_branch: str, *,
               review_list: Path | None = None, record: staff.StaffSnapshot | None = None,
               selection: ReadinessScan | CompletedStep = _READINESS_SCAN,
               known_producer: KnownMergeProducer | UnknownProducer = UNKNOWN_PRODUCER) -> list[str]:
    """Return the Markdown for the rows written to the review lists."""
    return [row.markdown for row in list_ready_rows(repo, production, merge_branch, review_list=review_list,
                                                   record=record, selection=selection, known_producer=known_producer)]


def list_ready_rows(repo: Path, production: Path, merge_branch: str, *,
                    review_list: Path | None = None, record: staff.StaffSnapshot | None = None,
                    selection: ReadinessScan | CompletedStep = _READINESS_SCAN,
                    known_producer: KnownMergeProducer | UnknownProducer = UNKNOWN_PRODUCER) -> list[WrittenReviewRow]:
    """Add live changes on origin/main, or release one completed step of one merge."""
    repo = repo.expanduser().resolve()
    try:
        fetched = subprocess.run(["git", "-C", str(repo), "fetch", "origin", "main"],
                                 capture_output=True, text=True, check=False)
    except OSError as error:
        print(f"change review: git fetch origin main failed; no rows added: {str(error).splitlines()[0]}", file=sys.stderr)
        return []
    if fetched.returncode:
        detail = " ".join((fetched.stderr or fetched.stdout).splitlines())
        print(f"change review: git fetch origin main failed; no rows added: {detail}", file=sys.stderr)
        return []
    if isinstance(selection, CompletedStep):
        _require_main(repo, selection.merged)
        merges = _git(repo, "log", "-1", "--format=%H%x09%s", selection.merged, "--").splitlines()
    else:
        merges = _git(repo, "log", "--reverse", "--first-parent", "--merges", f"--since={READY_FROM}T00:00:00Z",
                      "--format=%H%x09%s", f"origin/{merge_branch}", "--").splitlines()
    record = staff.snapshot() if record is None else record
    own_list = _line_value(production)
    scoped: set[Path] = set()
    for doc in _publication_docs(production, record):
        try:
            named = _line_value(doc)
        except (OSError, UnicodeError):
            scoped.add(doc)
            continue
        if not named or named == own_list:
            scoped.add(doc)
    outside_docs = [row.doc.path for row in record.find(kind="showrunner")
                    if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) and row.doc.path not in scoped]
    outside = _reviewed_merge_refs_by_doc(repo, lambda: outside_docs, record=record)
    outside_refs = set(outside.values())
    foreign = _foreign_merges(repo, outside, production, merge_branch, record)
    rows: list[WrittenReviewRow] = []
    matched_step = False
    recorded = checkpoint_producers(record)
    for entry in merges:
        merge, subject = entry.split("\t", 1)
        if any(subject.endswith(f" into {ref.removeprefix('origin/')}") for ref in outside_refs):
            continue
        if merge in foreign or not on_main(repo, merge):
            continue
        commits = merge_commits(repo, merge, excluded_refs=_publication_exclusions(repo, merge, outside_refs))
        short = _git(repo, "rev-parse", f"--short={SHORT}", merge).strip()
        write_commits = [replace(commit, timestamp=max(commit.timestamp, IMAGES_CHECKED_FROM)) for commit in commits]
        for commit, reason in _uncovered_problems(write_commits):
            if entries(commit.message):
                print(f"{short}: {commit.sha[:SHORT]} {commit.subject}: {reason}", file=sys.stderr)
        if isinstance(selection, CompletedStep):
            matched_step |= any(isinstance(item.readiness, AfterStep) and item.readiness.step == selection.step
                                for _, item in _checkpoint_entries(commits))
        valid_commits = [commit for commit in review_commits(write_commits)
                         if not problem(commit.subject, commit.message, commit.files, timestamp=commit.timestamp)]
        commits_by_sha = {commit.sha: commit for commit in commits}
        routed: dict[tuple[Path, Path], list[PublicationChange]] = {}
        merge_changes: dict[Path, list[PublicationChange]] = {}
        noted: set[str] = set()

        def note(change: PublicationChange) -> None:
            readiness = change.item.readiness
            if not isinstance(readiness, AfterStep) or readiness.step in noted:
                return
            checkout = Path(__file__).resolve().parents[2]
            owner = unit_lookup.doc_owner(production, record)
            command = shlex.join([str(checkout / "scripts/lib/py"), str(checkout / "scripts/production/change_review.py"),
                                  "live", "--merged", short, "--step", readiness.step,
                                  "--repo", str(repo), "--production", owner.name])
            print(f"{short}: after {readiness.step}; run {command}")
            noted.add(readiness.step)

        producer = (known_producer.producer
                    if isinstance(known_producer, KnownMergeProducer) and known_producer.merge == merge
                    else merge_producer(merge))
        producers = dict(recorded)
        ancestry: Mapping[str, tuple[str, ...]] = commits[0].ancestry if commits else {}
        for inner, parents in reversed(tuple(ancestry.items())):
            if len(parents) < 2:
                continue
            inner_producer = merge_producer(inner)
            if isinstance(inner_producer, UnknownProducer):
                continue
            for checkpoint in _git(repo, "rev-list", parents[1], f"^{parents[0]}", "--").splitlines():
                if checkpoint not in producers or isinstance(producers[checkpoint][0], UnknownProducer):
                    producers[checkpoint] = inner_producer, ""
        if isinstance(producer, UnknownProducer):
            checkpoint = next(iter(ancestry), "")
            checkpoint_owner: tuple[staff.StaffId | UnknownProducer, str] = (UNKNOWN_PRODUCER, "")
            while checkpoint in ancestry:
                owner = recorded.get(checkpoint, (UNKNOWN_PRODUCER, ""))
                if not isinstance(owner[0], UnknownProducer):
                    checkpoint_owner = owner
                if checkpoint not in producers:
                    producers[checkpoint] = checkpoint_owner
                parents = ancestry[checkpoint]
                checkpoint = parents[0] if parents else ""
        for change in publication_changes(valid_commits, production, record=record, known_producer=producer,
                                          recorded_producers=producers):
            readiness = change.item.readiness
            if isinstance(selection, CompletedStep) and isinstance(readiness, AfterStep) and readiness.step == selection.step:
                matched_step = True
            owner_doc = credited(change.source.production if isinstance(change.source, UnitSource) else production, record)
            path = review_list if review_list is not None else list_path(owner_doc, record=record)
            if path is None:
                path = list_path(production, record=record)
            if path is None:
                continue
            routed.setdefault((path, owner_doc), []).append(change)
            merge_changes.setdefault(path, []).append(change)
        def image_problem(change: PublicationChange, reason: str) -> None:
            commit = commits_by_sha[change.checkpoint]
            print(f"{short}: {commit.sha[:SHORT]} {commit.subject}: {reason}", file=sys.stderr)

        for (path, owner_doc), changes in routed.items():
            selected = changes
            if isinstance(selection, CompletedStep):
                selected = [change for change in changes if isinstance(change.item.readiness, AfterStep)
                            and change.item.readiness.step == selection.step]
            stamp = now()
            try:
                written = append_rows(path, selected, stamp.date(), short, owner_doc, record=record, repo=repo,
                                      on_hold=note if isinstance(selection, ReadinessScan) else None,
                                      merge_changes=merge_changes[path], added_at=stamp, on_image_problem=image_problem)
            except ReviewListDurabilityUnconfirmed as error:
                raise ReviewListDurabilityUnconfirmed(path, [*rows, *error.written_rows], error) from error
            except (ChangeReviewError, OSError, UnicodeError) as error:
                raise ReviewListWriteFailed(path, rows, error) from error
            rows.extend(WrittenReviewRow(path, row) for row in written)
    if isinstance(selection, CompletedStep) and not matched_step:
        raise ChangeReviewError(f"{selection.merged}: no change waits for step {selection.step!r}; no row added")
    return rows


def _changes(text: str) -> list[dict[str, str]]:
    """Read each row's cells under To review and Reviewed.

    A reviewed row keeps its change name and its Merged cell.
    """
    found: list[dict[str, str]] = []
    columns: list[str] = []
    for line in text.splitlines():
        if line.startswith("| # |"):
            columns = _cells(line)
        elif _NUMBER.match(line):
            found.append(dict(zip(columns, map(_plain, _cells(line)))))
    return found


def _files(repo: Path, sha: str) -> tuple[str, ...]:
    return tuple(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split())


class CommandLine(argparse.Namespace):
    action: str = ""
    repo: str = "."
    checkpoint: str = ""
    previous: str = ""
    merge_branch: str = ""
    list: str = ""
    merged: str = ""
    step: str = ""
    change: str = ""
    check: str = ""
    see: str = ""
    row: str = ""
    verdict: str = ""
    reason: str = ""
    validation: str = ""
    kind: str = ""
    production: str = ""
    urgency: str = ""
    due: str = ""
    after: str = ""
    write: bool = False
    hold: list[str] | None = None
    image: str = ""
    output: str = ""
    file: str = ""


def _list(options: CommandLine) -> Path:
    """The list --list names, else the production's."""
    return Path(options.list).expanduser() if options.list else production_list(options.production)


def _visible(options: CommandLine) -> Visible:
    """The change, how to try it and what the user sees, from --change, --check and --see."""
    change = _words("change", options.change)
    for name, value in (("check", options.check), ("see", options.see)):
        if not value.strip():
            _ = _words(name, value)
    return Visible(change, options.check.strip(), options.see.strip())


def _register_image(image: str, output: str) -> str:
    """Register an image or rendered output and report store failures."""
    if bool(image) == bool(output):
        raise ChangeReviewError("image needs one of --image or --output")
    try:
        if image_store.IDENTITY.fullmatch(image):
            return _row_image(image, "image")
        return (image_store.register_change(Path(image).expanduser()) if image
                else image_store.register_output(Path(output).expanduser()))
    except (ValueError, subprocess.SubprocessError) as error:
        raise ChangeReviewError(str(error)) from error


@dataclass(frozen=True)
class ReviewRowTarget:
    path: Path
    row: str


def _row_option(options: CommandLine) -> ReviewRowTarget:
    """The list and local row ID selected by --row, as /next printed it."""
    return resolve_row(options.row, production=options.production,
                       review_list=Path(options.list) if options.list else None)


def resolve_row(value: str, *, production: str = "", review_list: Path | None = None,
                selected_list_only: bool = False) -> ReviewRowTarget:
    """Resolve CLI row inputs with the explicit list or the production's selected list."""
    name, separator, row = value.strip().rpartition("/")
    row = _row_id("row", row)
    if separator and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is None:
        raise ChangeReviewError(f"--row {value!r} is not a row id such as C3 or production/C3")
    if review_list is not None:
        path = review_list.expanduser()
    else:
        try:
            doc = production_doc(production)
        except ChangeReviewError:
            if production or not separator:
                raise
            path = list_file(name)
        else:
            path = scope_list(doc)
    if not separator:
        return ReviewRowTarget(path, row)
    paths = ((path,) if review_list is not None or selected_list_only else tuple(dict.fromkeys((path, list_file(name),
        *sorted(lists_directory().glob(PREFIX + "*.md"))))))
    snapshots = _review_snapshots(paths)
    routing = _routing_from_snapshots(snapshots)
    matches: dict[Path, ReviewRowTarget] = {}
    for candidate, snapshot in snapshots.items():
        for item in (*snapshot.pending, *snapshot.reviewed):
            if item["#"] == row and (name == routing.list_owner(candidate)
                                      or name == routing.owner(item, candidate).tab):
                matches[candidate.resolve()] = ReviewRowTarget(candidate, row)
    if len(matches) == 1:
        return next(iter(matches.values()))
    if matches:
        choices = ", ".join(str(candidate) for candidate in matches)
        raise ChangeReviewError(f"{value} matches several lists; pass --list: {choices}")
    if review_list is not None or selected_list_only:
        raise ChangeReviewError(f"--row {value} differs from --list {path}; no matching row belongs to that showrunner")
    raise RowNotListed(f"{value} is not on a review list")


def _row_id(name: str, value: str) -> str:
    row = value.strip()
    if _ROW.match(row) is None:
        raise ChangeReviewError(f"--{name} {row!r} is not a row id such as C3")
    return row


def _rank_option(options: CommandLine, destination: Path) -> str:
    """The Urgency cell --urgency, --due and --after give; empty for a normal row."""
    try:
        level = urgency.read(options.urgency, options.due)
    except urgency.UrgencyError as error:
        raise ChangeReviewError(str(error)) from None
    if level.level == urgency.USER:
        level = urgency.mark(datetime.now(user_zone()).replace(tzinfo=None))
    after = ""
    if options.after.strip():
        target = resolve_row(options.after, production=options.production,
                             review_list=Path(options.list) if options.list else None, selected_list_only=True)
        after = target.row if target.path.resolve() == destination.resolve() else f"{_owner(target.path)}/{target.row}"
    return urgency.cell(level, after)


def _backfill_targets(options: CommandLine) -> tuple[list[Path], list[Path]]:

    documents = list(showrunners.timer_docs().values())
    lists = [Path(options.list).expanduser()] if options.list else sorted(lists_directory().glob(f"{PREFIX}*.md"))
    return documents, lists


@dataclass(frozen=True)
class BatchWrite:
    """One checked item whose mutation returns its work after the lock."""

    index: int
    path: Path
    row: str
    production: Path
    write: Callable[[list[str]], Callable[[], list[str]]]
    prepare: Callable[[], None]


def _batch_item(value: object, index: int, day: date, review_root: Path,
                cleared_validations: dict[ReviewRowTarget, str]) -> BatchWrite:
    if not isinstance(value, dict):
        raise ChangeReviewError("expected an object")
    fields = cast(dict[str, object], value)

    def text(name: str, *, required: bool = False) -> str:
        if required and name not in fields:
            raise ChangeReviewError(f"missing {name}")
        item = fields.get(name, "")
        if not isinstance(item, str):
            raise ChangeReviewError(f"{name} must be text")
        return item

    operation, production, row = text("op", required=True), text("production", required=True), text("row", required=True)
    options = CommandLine(production=production, row=row, list=text("list"))
    target = _row_option(options)
    path = target.path.resolve()
    doc = production_doc(production)
    if operation == "edit":
        options.change, options.check, options.see = text("change"), text("check"), text("see")
        options.image, options.output = text("image"), text("output")
        item = (KeepRowText() if (options.image or options.output) and not any((options.change, options.check, options.see))
                else _visible(options))
        existing = next((row for row in review_rows(path) if row["#"] == target.row), None)
        if existing is None:
            raise RowNotListed(f"{target.row} is not on the list to review")
        _require_row_image(options.image or options.output or existing.get(IMAGE, ""), target.row)

        image = ""

        def prepare_image() -> None:
            nonlocal image
            image = _register_image(options.image, options.output) if options.image or options.output else ""

        def edit(lines: list[str]) -> Callable[[], list[str]]:
            change = _edit_write(path, target.row, item, image)
            written = change.write(lines)
            columns, _, _ = _table(path, lines, SECTION)
            cells = dict(zip(columns, _cells(written)))

            def after() -> list[str]:
                change.after()
                final_columns, _, _ = _table(path, lines, SECTION)
                return [_row(path, final_columns, {**dict.fromkeys(final_columns, ""), **cells})]

            return after

        return BatchWrite(index, path, target.row, doc, edit, prepare_image)
    if operation == "verdict":
        if text("image") or text("output"):
            raise ChangeReviewError("verdict does not accept image or output")
        verdict = _words("verdict", text("verdict", required=True))
        validation_id = text('validation')
        change_verdict = _verdict_write(path, target.row, verdict, day)
        shown = row_display_id(path, target.row)

        def record(lines: list[str]) -> Callable[[], list[str]]:
            delivery = change_routing.delivery_path(review_root, path, target.row)
            slots = change_routing.delivery_slots(delivery)
            _require_verdict_exchange(slots, verdict, validation_id)
            if slots.validation_requested and not lands(verdict):
                _, first, end = _table(path, lines, SECTION)
                if any(line.startswith(f"| {target.row} |") for line in lines[first:end]):
                    cleared_validations[target] = _cleared_validation(delivery, slots)
                    _append_review_log(path, lines, target.row, _validation_fault_event(doc, verdict))
                    return lambda: _validation_fault_output(shown)
            result = change_verdict.write(lines)
            if isinstance(result, VerdictConflict):
                raise ChangeReviewError(f'{target.row} has the verdict "{result.recorded}" from {result.day}')

            def after() -> list[str]:
                change_verdict.after()
                return _verdict_output(path, target.row, shown, verdict, result, []).lines

            return after

        return BatchWrite(index, path, target.row, doc, record, lambda: None)
    raise ChangeReviewError("op must be edit or verdict")


def batch(file: Path) -> list[str]:
    """Apply checked items under one folder lock and commit all target lists once."""
    try:
        values = cast(object, json.loads(file.read_text(encoding="utf-8")))
    except ValueError as error:
        raise ChangeReviewError(f"batch: {error}") from error
    if not isinstance(values, list):
        raise ChangeReviewError("batch: expected a JSON array")
    day = today()
    root = change_routing.review_root()
    items: list[BatchWrite] = []
    rows: dict[tuple[Path, str], int] = {}
    errors: list[str] = []
    cleared_validations: dict[ReviewRowTarget, str] = {}
    for index, value in enumerate(cast(list[object], values)):
        try:
            item = _batch_item(value, index, day, root, cleared_validations)
            key = (item.path, item.row)
            if key in rows:
                raise ChangeReviewError(f"row {item.row} is already item {rows[key]}")
            rows[key] = index
            items.append(item)
        except (ChangeReviewError, OSError, ValueError) as error:
            errors.append(f"batch item {index}: {error}")
    if errors:
        raise ChangeReviewError("\n".join(errors))
    if not items:
        return []
    if len({item.path.parent for item in items}) != 1:
        raise ChangeReviewError("batch: all lists must be in one folder")
    for item in items:
        try:
            item.prepare()
        except (ChangeReviewError, OSError) as error:
            raise ChangeReviewError(f"batch item {item.index}: {error}") from error
    items[0].path.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    after: list[Callable[[], list[str]]] = []
    with review_delivery_lock(root), _list_lock(items[0].path), _image_reference_lock():
        original: dict[Path, str] = {}
        lines_by_path: dict[Path, list[str]] = {}
        for item in items:
            try:
                if item.path not in original:
                    original[item.path] = read(item.path)
                    lines_by_path[item.path] = original[item.path].splitlines(keepends=True)
                after.append(item.write(lines_by_path[item.path]))
            except (ChangeReviewError, OSError) as error:
                raise ChangeReviewError(f"batch item {item.index}: {error}") from error
        changed = {path: lines for path, lines in lines_by_path.items()
                   if lines != original[path].splitlines(keepends=True)}
        for path, lines in changed.items():
            _sort(path, lines)
            for production in dict.fromkeys(item.production for item in items if item.path == path):
                _stamp(path, lines, production, day)
        for path, lines in changed.items():
            write_text_atomic(path, "".join(lines))
        owners: dict[str, int] = {}
        for item in items:
            owner = _owner(item.path)
            owners[owner] = owners.get(owner, 0) + 1
        counts = ", ".join(f"{owner} {count}" for owner, count in owners.items())
        _commit(list(lines_by_path), f"batch {len(items)} writes ({counts})")
        for target, cleared in cleared_validations.items():
            delivery = change_routing.delivery_path(root, target.path, target.row)
            with clear_change_later(target.path, target.row):
                write_text_atomic(delivery, cleared)
    return [line for finish in after for line in finish()]


ACTION_OPTIONS: dict[str, tuple[str, ...]] = {
    "image": ("image", "output"),
    "add": ("repo", "merged", "list", "production", "change", "check", "see", "urgency", "due", "after", "image", "output", "hold"),
    "batch": ("file",),
    "edit": ("list", "production", "row", "change", "check", "see", "image", "output"),
    "show": ("list", "production", "row"),
    "mark-important": ("list", "production", "row"),
    "set-kind": ("list", "production", "row", "kind"),
    "verdict": ("list", "production", "row", "verdict", "validation"),
    "withdraw": ("list", "production", "row", "reason"),
    "list-path": ("production",),
    "showrunner": ("production",),
    "backfill-production": ("list", "write"),
    "list-ready": ("repo", "production", "merge_branch", "list"),
    "live": ("repo", "production", "merge_branch", "list", "merged", "step"),
    "check": ("repo", "checkpoint", "previous", "merge_branch"),
}


def _run(options: CommandLine) -> int:
    for name, value in cast(dict[str, object], vars(options)).items():
        default = "." if name == "repo" else None if name == "hold" else False if name == "write" else ""
        if name != "action" and value != default and name not in ACTION_OPTIONS[options.action]:
            raise ChangeReviewError(f"{options.action} does not accept --{name.replace('_', '-')}")
    if options.image and options.output:
        raise ChangeReviewError("image needs one of --image or --output")
    match options.action:
        case "image":
            identity = _register_image(options.image, options.output)
            print(identity)
            print(f"Put `| image {identity}` at the end of the Change-Review line.")
        case "add":
            repo = Path(options.repo).expanduser().resolve()
            merged = options.merged.strip()
            if merged:
                _require_main(repo, merged)
            path = _list(options)
            item, rank = _visible(options), _rank_option(options, path)
            if options.image or options.output:
                item = replace(item, image=_register_image(options.image, options.output))
            record = staff.snapshot()
            stamp = now()
            rows = append_rows(path, [item], stamp.date(), merged, production_doc(options.production), rank,
                               producers=[producer_at(repo, record)], record=record, added_at=stamp)
            row = row_id(rows[0])
            print(row)
            for line in hold_for_row(path, row, options.hold or []):
                print(line)
        case "batch":
            for line in batch(Path(options.file).expanduser()):
                print(line)
        case "edit":
            target = _row_option(options)
            edited_item = (KeepRowText() if (options.image or options.output) and not any((options.change, options.check, options.see))
                           else _visible(options))
            image = _register_image(options.image, options.output) if options.image or options.output else ""
            print(edit_row(target.path, target.row, edited_item, today(), production_doc(options.production), image=image))
        case "show":
            if options.row:
                target = _row_option(options)
                selected = next((row for row in review_rows(target.path) if row["#"] == target.row), None)
                if selected is None:
                    raise RowNotListed(f"{options.row} is not on the list to review")
                print(describe(selected | {"#": row_display_id(target.path, target.row)}))
                return 0
            # One row only: the user asks for the next one when they want it (user, 2026-10-20).
            if options.list and not options.production:
                waiting = change_queue(_list(options), WholeReviewList.ALL_ROWS)
            else:
                doc = production_doc(options.production)
                path = Path(options.list).expanduser() if options.list else scope_list(doc)
                waiting = change_queue(path, doc)
            shown = urgency.top(waiting.entries, waiting.name)
            print(describe(shown.item) + (f"\n\n{len(waiting.entries) - 1} more to review" if len(waiting.entries) > 1 else "")
                  if shown is not None else "nothing to review")
        case "mark-important":
            target = _row_option(options)
            marked_at = datetime.now(user_zone()).replace(tzinfo=None)
            print(mark_row(target.path, target.row, marked_at, production_doc(options.production)))
        case "set-kind":
            target = _row_option(options)
            print(set_row_kind(target.path, target.row, options.kind, production_doc(options.production)))
        case "verdict":
            target = _row_option(options)
            result = save_verdict(target.path, target.row, options.verdict, production_doc(options.production),
                                  validation_id=options.validation)
            for warning in result.warnings:
                print(warning, file=sys.stderr)
            if isinstance(result, VerdictRefused):
                print(result.reason, file=sys.stderr)
            for line in result.lines:
                print(line)
            if isinstance(result, VerdictRefused):
                return 1
        case "withdraw":
            target = _row_option(options)
            shown = row_display_id(target.path, target.row)
            written = withdraw_row(target.path, target.row, options.reason, now(), production_doc(options.production))
            print(f"{shown} withdrawn" if written else f"{shown} already withdrawn")
            for line in end_row_scratch(target.path, target.row):
                print(line)
        case "list-path":
            print(production_list(options.production))
        case "showrunner":
            print(showrunner_of(production_doc(options.production)))
        case "backfill-production":
            docs, lists = _backfill_targets(options)
            for path in lists:
                filled, untraced = backfill(path, list_tree(path, docs), options.write)
                print(f"{path.name}: {len(filled)} {'filled' if options.write else 'to fill'}, "
                      + f"{len(untraced)} untraced")
                for row, name in filled.items():
                    print(f"  {row}: {name}")
                for row, why in untraced.items():
                    print(f"  {row}: untraced, {why}")
        case "list-ready" | "live":
            selection = (CompletedStep(_words("merged", options.merged), _words("step", options.step))
                         if options.action == "live" else ReadinessScan())
            production = production_doc(options.production)
            branch = options.merge_branch or production_field(production.read_text(encoding="utf-8").splitlines(), "Merge branch")
            rows = list_ready(Path(options.repo), production, branch,
                              review_list=Path(options.list).expanduser() if options.list else None,
                              selection=selection)
            if rows:
                print("\n".join(rows))
        case "check":
            commits = unit_commits(Path(options.repo).resolve(), options.checkpoint, options.previous,
                                   options.merge_branch)
            refused = missing(commits)
            print("\n".join(refused) if refused else f"{len(review_commits(commits))} commit(s) carry their {TRAILER} lines")
            return 1 if refused else 0
        case _:
            pass
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("action", choices=("check", "list-ready", "live", "add", "edit", "show", "verdict", "withdraw",
                                                   "mark-important", "set-kind", "list-path", "showrunner",
                                                   "backfill-production", "image", "batch"))
    _ = parser.add_argument("--repo", default=".")
    for name in ("checkpoint", "previous", "merge-branch", "list", "merged", "step", "change", "check", "see",
                 "row", "verdict", "reason", "validation", "production", "urgency", "due", "after", "kind", "image", "output", "file"):
        _ = parser.add_argument(f"--{name}", default="")
    _ = parser.add_argument("--write", action="store_true")
    _ = parser.add_argument("--hold", action="append")
    try:
        return _run(cast(CommandLine, parser.parse_args(argv)))
    except (ChangeReviewError, OSError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
