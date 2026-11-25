#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Print a short link for deleting merged branches from the local origin."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import html
import json
import re
import secrets
import subprocess
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qs, urlsplit

from scripts.lib import build_folders
from scripts.direct import plan_history
from scripts.agents import remote
from scripts.lib import atomic_file, file_lock, origin_deletion, unit_runs
from scripts.production import session_staff, staff
from scripts.production.production_doc import Refusal, production_field
from scripts.review_links import change_page, page_frame, review_link
from scripts.review_links import staff_names

MANIFEST = ".review-link-branches.json"
PAGE_NAME = "branches.html"
KEPT = ".branch-page-kept.json"
USED = ".review-link-branches.used"


@dataclass(frozen=True)
class ActiveUnitBranch:
    branch: str
    state: Literal["active"] = field(default="active", init=False)


@dataclass(frozen=True)
class InactiveUnit:
    state: Literal["not active"] = field(default="not active", init=False)


@dataclass(frozen=True)
class MergedUnitBranch:
    name: str
    unit: str
    tip: str
    commit_date: str
    merge: str
    merge_date: str
    unit_status: ActiveUnitBranch | InactiveUnit
    section: Literal["unit"] = field(default="unit", init=False)


@dataclass(frozen=True)
class MergedUnownedHead:
    name: str
    tip: str
    commit_date: str


@dataclass(frozen=True)
class MergedUnownedBranch:
    name: str
    tip: str
    commit_date: str
    merge: str
    merge_date: str
    section: Literal["unowned"] = field(default="unowned", init=False)


@dataclass(frozen=True)
class BranchListing:
    rows: tuple[MergedUnitBranch | MergedUnownedBranch, ...]
    unowned_total: int


@dataclass(frozen=True)
class BranchPage:
    repository: Path
    rows: tuple[MergedUnitBranch | MergedUnownedBranch, ...]
    token: str
    publisher: staff.StaffId
    folder: Path
    unowned_total: int = 0


@dataclass(frozen=True)
class DeleteResult:
    name: str
    message: str
    deleted: bool


@dataclass(frozen=True)
class BranchDeletion:
    page: BranchPage
    selected: tuple[str, ...]


@dataclass(frozen=True)
class PublishedBranchPage:
    url: str
    page: BranchPage


class PageSubmitted(ValueError):
    """An older marker has no address for the next branch page."""


@dataclass(frozen=True)
class FormerUnit:
    name: str
    worktree: Path
    branch: str


@dataclass(frozen=True)
class LatestUnitBranch:
    started_at: float
    branch: str


@dataclass(frozen=True)
class UnitBranchEvidence:
    units: dict[str, str]
    latest_by_owner: dict[staff.StaffId, LatestUnitBranch]


class RepositoryScope:
    """Identify this origin and its recorded local worktrees."""

    def __init__(self, repository: Path) -> None:
        self.common: str = git_text(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.origin: str = git_text(repository, "remote", "get-url", "origin")
        self.registered: dict[Path, str] = {}
        worktree = repository
        for field in git_text(repository, "worktree", "list", "--porcelain", "-z").split("\0"):
            if field.startswith("worktree "):
                worktree = Path(field.removeprefix("worktree "))
            elif field.startswith("branch refs/heads/"):
                self.registered[worktree] = field.removeprefix("branch refs/heads/")
        self.former_worktrees: set[Path] = set()
        self.matches: dict[Path, bool] = {}

    def contains(self, worktree: Path) -> bool:
        worktree = worktree.expanduser().resolve()
        if worktree in self.matches:
            return self.matches[worktree]
        common = git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode == 0:
            origin = git(worktree, "remote", "get-url", "origin")
            matches = (common.stdout.strip() == self.common
                       or (origin.returncode == 0 and origin.stdout.strip() == self.origin))
        else:
            matches = worktree in self.registered or worktree in self.former_worktrees
        self.matches[worktree] = matches
        return matches


def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "--no-optional-locks", "-C", str(repository), *arguments],
                          capture_output=True, text=True, check=False)


def git_text(repository: Path, *arguments: str) -> str:
    result = git(repository, *arguments)
    result.check_returncode()
    return result.stdout.strip()


def history_roots() -> tuple[Path, ...]:
    """Read both history names when a migration leaves records in both folders."""
    if unit_runs.environ("HISTORY_DIR"):
        return (unit_runs.history_root(),)
    state = unit_runs.state_dir()
    return tuple(dict.fromkeys((unit_runs.history_root(), state / unit_runs.HISTORY_NAME,
                                state / unit_runs.FORMER_HISTORY_NAME)))


def former_units(repository: Path, scope: RepositoryScope,
                 live_documents: tuple[str, ...]) -> tuple[FormerUnit, ...]:
    """Read former staff rows from the production documents' retained Units tables."""
    found = git(repository, "grep", "-l", "^## Units", "refs/remotes/origin/main", "--", "docs/plans")
    if found.returncode != 1:
        found.check_returncode()
    documents = (*(git_text(repository, "show", source) for source in found.stdout.splitlines()), *live_documents)
    units: list[FormerUnit] = []
    for document in documents:
        lines = document.splitlines()
        if not scope.contains(Path(production_field(lines, "Repository"))):
            continue
        in_units = False
        columns: list[str] = []
        for line in lines:
            if line.startswith("## "):
                in_units = line == "## Units"
                columns = []
            if not in_units or not line.startswith("|"):
                continue
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            if not columns:
                columns = cells
                continue
            row = dict(zip(columns, cells))
            name, tree, branch = (row.get(key, "") for key in ("Unit", "Worktree", "Branch"))
            machine = row.get("Machine", remote.local_machine())
            if (machine == "natedev" and name and branch and branch != "—"
                    and tree.startswith("/")):
                units.append(FormerUnit(name, Path(tree).resolve(), branch))
    scope.former_worktrees.update(unit.worktree for unit in units)
    return tuple(units)


def recorded_branches(record: staff.StaffSnapshot, roots: tuple[Path, ...],
                      resolver: session_staff.RowResolver, scope: RepositoryScope,
                      former: tuple[FormerUnit, ...]) -> UnitBranchEvidence:
    """Join run branches to current staff or to the former staff worktree."""
    branches = {unit.branch: unit.name for unit in former if scope.contains(unit.worktree)}
    by_worktree = {unit.worktree: unit for unit in former}
    branch_started: dict[str, float] = {}
    latest: dict[staff.StaffId, LatestUnitBranch] = {}
    for root in roots:
        for path in sorted((root / "runs").glob("*.jsonl")):
            event = plan_history.run_started_event(path)
            if event is None:
                continue
            if event.get("skill") not in ("unit-history", "plan-delegate"):
                continue
            try:
                owner = unit_runs.run_owner(event, record)
            except staff.StaffUnreadable:
                owner = unit_runs.RunOwnerUnrecorded("the owner left staff")
            branch = event.get("branch")
            if not isinstance(branch, str) or not branch:
                continue
            stamp = event.get("run_started_at", event.get("timestamp_epoch", 0))
            started = float(stamp) if isinstance(stamp, (int, float)) else 0.0
            if isinstance(owner, staff.StaffRow):
                if owner.id not in latest or started > latest[owner.id].started_at:
                    latest[owner.id] = LatestUnitBranch(started, branch)
            directory = event.get("working_dir", event.get("worktree", ""))
            if isinstance(directory, str) and Path(directory).is_absolute():
                worktree = Path(directory).resolve()
            elif isinstance(owner, staff.StaffRow) and isinstance(owner.worktree, (staff.PresentWorktree, staff.RemovedWorktree)):
                worktree = owner.worktree.path
            else:
                continue
            if not scope.contains(worktree):
                continue
            if branch in branch_started and started <= branch_started[branch]:
                continue
            if (isinstance(owner, staff.StaffRow) and owner.kind == "unit"
                    and staff.worktree_machine(owner) == "natedev"):
                branches[branch] = owner.name
                branch_started[branch] = started
            elif isinstance(owner, unit_runs.RunOwnerUnrecorded) and worktree in by_worktree:
                branches[branch] = by_worktree[worktree].name
                branch_started[branch] = started
    for row in record.find(kind="unit"):
        if (isinstance(row.worktree, staff.RemovedWorktree) and row.worktree.machine == "natedev"
                and scope.contains(row.worktree.path)):
            branch = resolver.unit_branch(row)
            if isinstance(branch, session_staff.Branch):
                branches[branch.name] = row.name
    return UnitBranchEvidence(branches, latest)


@dataclass(frozen=True)
class MainHistory:
    """Each commit that origin main holds, with its commit date and its first mainline commit."""
    dates: dict[str, str]
    merges: dict[str, str]


def main_history(repository: Path, main: str) -> MainHistory:
    """Read the graph of main in one git call, so no branch costs a git call of its own."""
    dates: dict[str, str] = {}
    parents: dict[str, list[str]] = {}
    for line in git_text(repository, "log", "--format=%H %cI %P", main).splitlines():
        commit, date, *above = line.split()
        dates[commit] = date
        parents[commit] = above
    mainline = [main]
    while parents[mainline[-1]]:
        mainline.append(parents[mainline[-1]][0])
    # The oldest mainline commit takes each commit it holds; a newer one takes only the rest.
    merges: dict[str, str] = {}
    for merge in reversed(mainline):
        pending = [merge]
        while pending:
            commit = pending.pop()
            if commit not in merges:
                merges[commit] = merge
                pending.extend(parents[commit])
    return MainHistory(dates, merges)


def kept_branches() -> set[str]:
    path = review_link.review_root() / KEPT
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return set()
    if not isinstance(value, list) or not all(isinstance(name, str) and name for name in cast(list[object], value)):
        raise ValueError("invalid kept branch list")
    return set(cast(list[str], value))


def production_branches(resolver: session_staff.RowResolver, scope: RepositoryScope) -> tuple[set[str], tuple[str, ...]]:
    """Read protected branches and enrolled unit tables from staff documents."""
    from scripts.production.production_doc import EnrolledPaths, ProductionPathsError, resolve_production_paths

    protected: set[str] = {"main"}
    live_documents: list[str] = []
    for row in resolver.record.rows:
        if row.kind != "showrunner" or not isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            continue
        document = resolver.production_doc(row)
        if not isinstance(document, session_staff.ProductionDoc):
            raise ValueError(f"{row.name}: cannot read the production: {document.reason}")
        protected.add(production_field(document.text.splitlines(), "Merge branch"))
        if isinstance(row.doc, staff.LiveDoc) and row.doc.machine == remote.local_machine():
            try:
                paths = resolve_production_paths(row.doc.path)
            except ProductionPathsError as error:
                raise ValueError(str(error)) from error
            if isinstance(paths, EnrolledPaths) and scope.contains(paths.integration):
                live_documents.append(document.text)
    return protected, tuple(live_documents)


def list_branches(repository: Path, record: staff.StaffSnapshot,
                  roots: tuple[Path, ...]) -> BranchListing:
    """Fetch origin's graph once without changing any branch on origin."""
    _ = git_text(repository, "fetch", "--no-write-fetch-head", "-q", "origin")
    scope = RepositoryScope(repository)
    resolver = session_staff.RowResolver(record)
    protected, live_documents = production_branches(resolver, scope)
    former = former_units(repository, scope, live_documents)
    recorded = recorded_branches(record, roots, resolver, scope, former)
    active_branches: dict[str, ActiveUnitBranch] = {}
    for row in record.rows:
        # A producer's worktree is its launch folder; it holds no branch of this repository.
        if row.kind != "producer" and isinstance(row.worktree, staff.PresentWorktree):
            tree = row.worktree
            if tree.machine == "natedev" and tree.path in scope.registered:
                active_branch = scope.registered[tree.path]
            else:
                branch = resolver.unit_branch(row)
                if not isinstance(branch, session_staff.Branch):
                    if row.id not in recorded.latest_by_owner:
                        raise ValueError(f"{row.name}: cannot read the active branch: {branch.reason}")
                    active_branch = recorded.latest_by_owner[row.id].branch
                else:
                    active_branch = branch.name
            protected.add(active_branch)
            active_branches[row.name] = ActiveUnitBranch(active_branch)
    heads = {ref.removeprefix("refs/heads/"): tip for line in
             git_text(repository, "ls-remote", "--heads", "origin").splitlines()
             for tip, ref in [line.split()]}
    main = heads.get("main", "")
    if not main:
        raise ValueError("origin has no main branch")
    history = main_history(repository, main)
    rows: list[MergedUnitBranch | MergedUnownedBranch] = []
    unowned: list[MergedUnownedHead] = []
    kept = kept_branches()
    for name in sorted(heads.keys() - protected):
        if name not in recorded.units and name in kept:
            continue
        tip = heads[name]
        if tip not in history.merges:
            continue
        commit_date = history.dates[tip]
        if name in recorded.units:
            unit = recorded.units[name]
            merge = history.merges[tip]
            rows.append(MergedUnitBranch(name, unit, tip, commit_date, merge, history.dates[merge],
                                         active_branches.get(unit, InactiveUnit())))
        else:
            unowned.append(MergedUnownedHead(name, tip, commit_date))
    unowned.sort(key=lambda row: (datetime.fromisoformat(row.commit_date), row.name))
    for head in unowned[:10]:
        merge = history.merges[head.tip]
        rows.append(MergedUnownedBranch(head.name, head.tip, head.commit_date, merge, history.dates[merge]))
    return BranchListing(tuple(rows), len(unowned))


def page_at(root: Path, request_path: str) -> BranchPage:
    match = re.fullmatch(r"/([a-z0-9]{6})/branches\.html", urlsplit(request_path).path)
    if match is None:
        raise FileNotFoundError("not a branch page")
    folder = current_folder(root, match[1])
    manifest = folder / MANIFEST
    if manifest.is_symlink():
        raise ValueError("not a regular branch manifest")
    value = cast(object, json.loads(manifest.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError("invalid branch manifest")
    fields = cast(dict[str, object], value)
    repository, token, raw_rows, unowned_total, publisher = (fields.get(key) for key in
                                                 ("repository", "token", "rows", "unowned_total", "publisher"))
    if not (set(fields) == {"repository", "token", "rows", "unowned_total", "publisher"}
            and isinstance(publisher, str) and publisher
            and type(unowned_total) is int and unowned_total >= 0
            and isinstance(repository, str) and Path(repository).is_absolute()
            and isinstance(token, str) and token and isinstance(raw_rows, list)):
        raise ValueError("invalid branch manifest")
    rows: list[MergedUnitBranch | MergedUnownedBranch] = []
    for raw in cast(list[object], raw_rows):
        if not isinstance(raw, dict):
            raise ValueError("invalid branch row")
        row = cast(dict[str, object], raw)
        common_keys = ("name", "tip", "commit_date", "merge", "merge_date")
        if row.get("section") == "unowned":
            if (set(row) != {*common_keys, "section"}
                    or not all(isinstance(row[key], str) and row[key] for key in common_keys)):
                raise ValueError("invalid branch row")
            rows.append(MergedUnownedBranch(*(cast(str, row[key]) for key in common_keys)))
            continue
        keys = ("name", "unit", "tip", "commit_date", "merge", "merge_date")
        if (row.get("section") != "unit" or set(row) != {*keys, "unit_status", "section"}
                or not all(isinstance(row[key], str) and row[key] for key in keys)):
            raise ValueError("invalid branch row")
        raw_status = row["unit_status"]
        if not isinstance(raw_status, dict):
            raise ValueError("invalid unit status")
        status_fields = cast(dict[str, object], raw_status)
        if status_fields == {"state": "not active"}:
            unit_status = InactiveUnit()
        elif (set(status_fields) == {"state", "branch"} and status_fields["state"] == "active"
              and isinstance(status_fields["branch"], str) and status_fields["branch"]):
            unit_status = ActiveUnitBranch(status_fields["branch"])
        else:
            raise ValueError("invalid unit status")
        rows.append(MergedUnitBranch(*(cast(str, row[key]) for key in keys), unit_status=unit_status))
    unowned_count = sum(isinstance(row, MergedUnownedBranch) for row in rows)
    if unowned_count > 10 or unowned_count > unowned_total:
        raise ValueError("invalid no-owner branch count")
    kept = kept_branches()
    available = tuple(row for row in rows if not isinstance(row, MergedUnownedBranch) or row.name not in kept)
    return BranchPage(Path(repository), available, token, staff.StaffId(publisher), folder,
                      unowned_total - (len(rows) - len(available)))


def current_folder(root: Path, slug: str) -> Path:
    seen: set[str] = set()
    while slug not in seen:
        seen.add(slug)
        folder = root / slug
        marker = folder / USED
        if folder.is_symlink() or marker.is_symlink():
            raise ValueError("not a regular branch page")
        try:
            successor = marker.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            if len(seen) > 1 and not (folder / MANIFEST).is_file():
                raise ValueError("The current branch list is unavailable.") from None
            return folder
        if successor == "submitted":
            raise PageSubmitted()
        if not re.fullmatch(r"[a-z0-9]{6}", successor):
            raise ValueError("invalid next branch page")
        slug = successor
    raise ValueError("branch pages form a loop")


def render_date(value: str, date_class: str) -> str:
    return f'<strong class="{date_class}">{html.escape(value[:10])}</strong>{html.escape(value[10:])}'


def branch_heading(failure: str) -> str:
    return '<h1>Branches to delete</h1>' + page_frame.action_failure("branch-failure", failure)


def unavailable_page() -> bytes:
    return change_page.document(branch_heading("This branch page is not available."), "Branches to delete")


def empty_page(failure: str = "") -> bytes:
    return change_page.document(branch_heading(failure) + "<p>No branches are available to delete.</p>",
                                "Branches to delete")


def render(page: BranchPage, *, failure: str = "", action: str = "") -> bytes:
    if not page.rows:
        return empty_page(failure)
    names = staff_names.read()
    unit_rows: list[str] = []
    unowned_rows: list[str] = []
    for row in page.rows:
        name = html.escape(row.name, quote=True)
        owner = ""
        if isinstance(row, MergedUnitBranch):
            unit_status = (f"active, now on branch {html.escape(row.unit_status.branch)}"
                           if isinstance(row.unit_status, ActiveUnitBranch) else "not active")
            owner = f"Unit: {names.html(row.unit)} — {unit_status}\n"
        rows = unit_rows if isinstance(row, MergedUnitBranch) else unowned_rows
        dates_in_order = datetime.fromisoformat(row.commit_date) <= datetime.fromisoformat(row.merge_date)
        date_class = "date-in-order" if dates_in_order else "date-out-of-order"
        rows.append(f'''<section><label><input type="checkbox" name="branch" value="{name}" checked>
<strong>{name}</strong></label><p>{owner}Last commit: {render_date(row.commit_date, date_class)}
Merged into main: {html.escape(row.merge[:12])} · {render_date(row.merge_date, date_class)}</p></section>''')
    content = branch_heading(failure)
    action_attribute = f' action="{html.escape(action, quote=True)}"' if action else ""
    unowned_heading = f"Merged, no owner recorded ({len(unowned_rows)} of {page.unowned_total})"
    content += f'''<p>Clear each branch that you want to keep.</p>
<form method="post"{action_attribute}><input type="hidden" name="token" value="{html.escape(page.token, quote=True)}">
{"".join(unit_rows)}<h2>{unowned_heading}</h2>{"".join(unowned_rows)}<button>Delete</button></form>'''
    styles = b"""
:root{--date-in-order:#166534;--date-out-of-order:#b91c1c}
@media(prefers-color-scheme:dark){:root{--date-in-order:#86efac;--date-out-of-order:#fca5a5}}
.date-in-order{color:var(--date-in-order)}
.date-out-of-order{color:var(--date-out-of-order)}
"""
    return change_page.document(content, "Branches to delete").replace(b"</style>", styles + b"</style>", 1)


def submit(page: BranchPage, body: str) -> BranchDeletion:
    fields = parse_qs(body, keep_blank_values=True)
    if fields.get("token") != [page.token]:
        raise ValueError("invalid branch form token")
    selected = tuple(dict.fromkeys(fields.get("branch", [])))
    listed = {row.name: row for row in page.rows}
    if any(name not in listed for name in selected):
        raise ValueError("the selection contains a branch outside this page")
    publisher = staff.snapshot().by_id(page.publisher)
    if publisher is None:
        raise ValueError(f"no staff record for publisher {page.publisher}")
    unchecked = {row.name for row in page.rows if isinstance(row, MergedUnownedBranch) and row.name not in selected}
    if unchecked:
        root = review_link.review_root()
        root.mkdir(parents=True, exist_ok=True)
        with file_lock.exclusive(root / (KEPT + ".lock")):
            kept = kept_branches() | unchecked
            atomic_file.write_bytes_atomic(root / KEPT, json.dumps(sorted(kept)).encode("utf-8"), mode=0o600)
    return BranchDeletion(page, selected)


def delete_selected(deletion: BranchDeletion) -> tuple[DeleteResult, ...]:
    tips = {row.name: row.tip for row in deletion.page.rows}
    results: list[DeleteResult] = []
    for name in deletion.selected:
        result = origin_deletion.delete(deletion.page.repository, "origin", f"refs/heads/{name}", tips[name])
        if isinstance(result, origin_deletion.Kept):
            message = f"kept: {result.reason}"
        elif isinstance(result, origin_deletion.Deleted):
            message = "deleted"
        else:
            message = "already absent"
        results.append(DeleteResult(name, message, not isinstance(result, origin_deletion.Kept)))
    return tuple(results)


def without_branches(page: BranchPage, names: set[str]) -> BranchPage:
    rows = tuple(row for row in page.rows if row.name not in names)
    removed_unowned = sum(isinstance(row, MergedUnownedBranch) and row.name in names for row in page.rows)
    return replace(page, rows=rows, unowned_total=page.unowned_total - removed_unowned)


def finish_deletion(deletion: BranchDeletion) -> tuple[set[str], str]:
    page = deletion.page
    results = delete_selected(deletion)
    lines = [f"{result.name}: {' '.join(result.message.splitlines())}" for result in results]
    failures = [line for result, line in zip(results, lines) if not result.deleted]
    deleted = {result.name for result in results if result.deleted}
    if deleted:
        kept = {row.name for row in page.rows
                if isinstance(row, MergedUnownedBranch) and row.name not in deletion.selected}
        try:
            write_manifest(without_branches(page, deleted | kept))
        except OSError as error:
            print(f"review-links branch manifest: {error}", file=sys.stderr)
            failures.append("The original branch list could not be updated.")
    try:
        published = publish_for(page.repository, page.publisher)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, staff.StaffRefused, Refusal) as error:
        lines.append("Next page failed: " + " ".join(str(error).splitlines()))
        failures.append("The next branch page could not be prepared.")
    else:
        next_page = published.page
        try:
            atomic_file.write_text_atomic(page.folder / USED, next_page.folder.name + "\n", new_file_mode=0o600)
        except OSError as error:
            print(f"review-links branch marker: {error}", file=sys.stderr)
            failures.append("The next list could not be saved at this address.")
        if next_page.rows:
            shown = sum(isinstance(row, MergedUnownedBranch) for row in next_page.rows)
            lines.append(f"Next page: {published.url} ({shown} of {next_page.unowned_total} no-owner branches)")
        else:
            lines.append("No branches remain to delete.")
    try:
        change_page.send_to_staff(page.publisher, change_page.SENDER, "\n".join(lines))
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"review-links branch delivery: {error}", file=sys.stderr)
        failures.append("The branch results could not be sent.")
    return deleted, "\n".join(failures)


def publish(repository: Path) -> str:
    publisher = staff.snapshot().by_current_session(staff.caller_identity())
    if publisher is None:
        raise ValueError("the current session has no staff row")
    return publish_for(repository, publisher.id).url


def publish_for(repository: Path, publisher: staff.StaffId) -> PublishedBranchPage:
    listing = list_branches(repository, staff.snapshot(), history_roots())
    root = review_link.review_root()
    with build_folders.tool_folder("review-branches") as temporary:
        page = BranchPage(repository.resolve(), listing.rows, secrets.token_urlsafe(), publisher,
                          Path(temporary), listing.unowned_total)
        source = Path(temporary) / PAGE_NAME
        _ = source.write_bytes(render(page))
        url = review_link.snapshot_at(root, [source], new=True)[0]
    folder = root / urlsplit(url).path.split("/")[1]
    page = replace(page, folder=folder)
    write_manifest(page)
    return PublishedBranchPage(url, page)


def write_manifest(page: BranchPage) -> None:
    manifest = {"repository": str(page.repository), "rows": [asdict(row) for row in page.rows],
                "token": page.token, "unowned_total": page.unowned_total, "publisher": page.publisher}
    atomic_file.write_bytes_atomic(page.folder / MANIFEST, json.dumps(manifest).encode("utf-8"), mode=0o600)


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--list", action="store_true", help="print names and count without publishing a page")
    args = parser.parse_args(arguments)
    repository = Path.home() / ".claude"
    try:
        if cast(bool, args.list):
            listing = list_branches(repository, staff.snapshot(), history_roots())
            for row in listing.rows:
                print(row.name)
            print(f"{len(listing.rows)} branches")
        else:
            print(publish(repository))
    except (OSError, ValueError, subprocess.SubprocessError, staff.StaffRefused, Refusal) as error:
        print(f"branch_page: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
