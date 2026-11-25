#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Create or attach one standing style production per repository."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from typing import cast

SCRIPTS = Path(__file__).resolve().parents[1]

from scripts.production import session_staff, showrunners, staff
from scripts.lib import build_folders, git_files
from scripts.worktree import worktree_records, worktrees
from scripts.lib.git_exclude import ensure_excluded, require_untracked_ignored
from scripts.production import showrunner_tree
from scripts.production.production_names import LIVE_DOCUMENT_SUFFIX, MERGE_CHECKOUT_FIELD
from scripts.production.add_showrunner import NoEndingTab, herdr_tab_open
from scripts.herdr import herdr
from scripts.production.production_doc import LegacyProduction, Production, Refusal, production_field, read_production, read_new_production, optional_field, FieldPresent

from scripts.style import rules, style_config
from scripts.style import style_ledger
from scripts.lib.atomic_file import create_text_atomic, write_text_atomic

FAMILIES: tuple[rules.Family, ...] = ("names", "values", "types", "modules")
PLAN_DIRECTORY = Path("docs/style-production/units")
LEGACY_STYLE_BRANCH_PREFIX = "style/"
LAUNCHER = Path(os.environ.get("ADD_STYLE_LAUNCHER") or SCRIPTS / "production/add_showrunner.py")
SHOWRUNNERS = Path(os.environ.get("ADD_STYLE_SHOWRUNNERS") or SCRIPTS / "production/showrunners.py")


class StyleBranchPointTaken(Refusal):
    """The repository's standing production already follows another branch point."""


@dataclass(frozen=True)
class PilotUnit:
    family: rules.Family
    packages: tuple[str, ...]  # An empty selection covers every workspace package.


@dataclass(frozen=True)
class AllStyleUnits:
    """Launch all four families without crate restrictions."""


@dataclass(frozen=True)
class PilotUnits:
    units: tuple[PilotUnit, ...]


def _pilot_units(arguments: list[str], repository: Path) -> AllStyleUnits | PilotUnits:
    if not arguments:
        return AllStyleUnits()
    manifests = {
        path.relative_to(repository).as_posix(): path.read_text()
        for path in repository.rglob("Cargo.toml") if path.is_file()
    }
    packages = {member.package_name for member in rules.workspace_members(manifests)}
    units: list[PilotUnit] = []
    seen: set[str] = set()
    for argument in arguments:
        unit, separator, selected = argument.partition("=")
        family = rules.style_family(unit)
        if isinstance(family, rules.NotStyleUnit):
            raise Refusal(f"unknown pilot unit {unit!r}")
        if family in seen:
            raise Refusal(f"duplicate pilot unit {unit!r}")
        seen.add(family)
        requested = tuple(package.strip() for package in selected.split(",")) if separator else ()
        for package in requested:
            if package not in packages:
                raise Refusal(f"unknown pilot crate {package!r}")
        units.append(PilotUnit(family, tuple(dict.fromkeys(requested))))
    return PilotUnits(tuple(units))


@dataclass(frozen=True)
class StandingStyleProduction:
    repository: Path
    name: str
    checkout: Path
    doc: Path
    branch_point: str


@dataclass(frozen=True)
class NoStyleProduction:
    repository: Path
    name: str

    @property
    def doc(self) -> Path:
        return self.repository / "docs" / f"{self.name}-production{LIVE_DOCUMENT_SUFFIX}"


class LegacyStyleEnrollmentNotNeeded(Enum):
    CURRENT_DOCUMENT = "no legacy files need publication"


@dataclass(frozen=True)
class LegacyStyleEnrollment:
    relative_log: Path
    files: tuple[tuple[Path, str], ...]

    def publish(self) -> None:
        for path, text in self.files:
            if not create_text_atomic(path, text) and path.read_bytes() != text.encode("utf-8"):
                raise Refusal(f"{path}: conflicts with the legacy style bootstrap")


@dataclass(frozen=True)
class StyleProduction:
    parent: Production
    repository: Path
    name: str
    branch_point: str
    branch_tip: str
    merge_branch: str
    checkout: Path
    doc: Path
    proposals: str
    merge_tests: tuple[str, ...]
    enrollment: LegacyStyleEnrollment | LegacyStyleEnrollmentNotNeeded = LegacyStyleEnrollmentNotNeeded.CURRENT_DOCUMENT

    @property
    def log(self) -> Path:
        if isinstance(self.enrollment, LegacyStyleEnrollment):
            return self.enrollment.relative_log
        return (Path(production_field(self.doc.read_text().splitlines(), "Log")) if self.doc.is_file()
                else Path("docs") / f"{self.name}-log.md")


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise Refusal(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def standing_style_production(repo_dir: Path) -> StandingStyleProduction | NoStyleProduction:
    """Locate the repository's standing style production."""
    return _style_production(repo_dir, include_direct=False)


def configured_style_production(repo_dir: Path) -> StandingStyleProduction | NoStyleProduction:
    """Locate the production that controls the repository's configured style units."""
    return _style_production(repo_dir, include_direct=True)


def _style_production(repo_dir: Path, *, include_direct: bool) -> StandingStyleProduction | NoStyleProduction:
    repository = Path(_git(repo_dir, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
    trees = tuple(Path(line.removeprefix("worktree ")) for line in
                  _git(repository, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree "))
    for row in staff.snapshot().find(kind="showrunner"):
        if not isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) or not row.doc.path.is_file():
            continue
        doc = row.doc.path
        checkout = next((tree for tree in trees if doc.is_relative_to(tree)), None)
        if checkout is None:
            continue
        lines = doc.read_text().splitlines()
        if optional_field(lines, "Kind") == FieldPresent("style"):
            branch_point = production_field(lines, "Branch point").removeprefix("refs/heads/")
            if staff.declares_merge_checkout(lines):
                checkout = read_production(row).checkout
            return StandingStyleProduction(repository, row.name, checkout, doc, branch_point)
        if include_direct and style_config.has_style_units(lines):
            config = style_config.read_config(doc)
            return StandingStyleProduction(repository, row.name, config.route.checkout, doc, config.route.branch_point)
    return NoStyleProduction(repository, f"{repository.name}-style")


def style_production_branch(name: str) -> str:
    return f"{name}-{worktrees.UNIT_WORKTREE_NAME}"


def _integration_style_branch(branch: str, production: str) -> bool:
    return branch.endswith(f"-integration-{production}")


def is_style_production_branch(branch: str, production: str) -> bool:
    return (branch.startswith(LEGACY_STYLE_BRANCH_PREFIX) or branch == style_production_branch(production)
            or _integration_style_branch(branch, production))


@dataclass(frozen=True)
class UnfinishedStyleCheckout:
    checkout: Path
    doc: Path
    branch: str


def _enrolled_style_checkout(standing: NoStyleProduction) -> UnfinishedStyleCheckout:
    from scripts.production.production_admission import require_guard_ready

    _require_bootstrap_destination(standing.doc, standing.repository)
    try:
        document = read_new_production(standing.doc, standing.name)
    except Refusal as error:
        raise Refusal(f"{standing.doc}: conflicts with the legacy style bootstrap. {error}") from error
    if document.merge_checkout != standing.repository:
        raise Refusal(f"{standing.doc}: Merge checkout must name {standing.repository}")
    try:
        _ = session_staff.production_session_directory(document.doc, document.checkout)
    except ValueError as error:
        raise Refusal(f"{standing.doc}: {error}") from error
    _require_bootstrap_destination(document.log, standing.repository)
    require_guard_ready()
    return UnfinishedStyleCheckout(document.checkout, document.doc, document.merge_branch)


def _unfinished_style_checkout(standing: NoStyleProduction) -> UnfinishedStyleCheckout | None:
    repository, name = standing.repository, standing.name
    checkout = build_folders.worktree_path(build_folders.repo_word(repository), name, worktrees.UNIT_WORKTREE_NAME)
    branches = worktree_records.worktree_branches(repository)
    for candidate in (checkout, checkout.with_name("production")):
        doc = candidate / "docs" / f"{name}-production.md"
        if doc.is_file():
            branch = production_field(doc.read_text().splitlines(), "Merge branch")
            return UnfinishedStyleCheckout(candidate, doc, branch)
        branch = branches.get(candidate.resolve(), "")
        if not branch and (record := worktree_records.checkout_record(candidate)) is not None:
            if Path(record.repo) == repository:
                branch = record.branch
        if is_style_production_branch(branch, name):
            return UnfinishedStyleCheckout(candidate, standing.doc, branch)
    return None


def _request(name: str) -> StyleProduction:
    parent = read_production(name)
    lines = parent.doc.read_text().splitlines()
    proposals = production_field(lines, "Proposals")
    declared_repo = Path(production_field(lines, "Repository")).expanduser().resolve()
    standing = standing_style_production(declared_repo)
    repository, name = standing.repository, standing.name
    branch_point = parent.merge_branch.removeprefix("refs/heads/")
    _ = _git(repository, "check-ref-format", f"refs/heads/{branch_point}")
    branch_tip = _git(repository, "rev-parse", "--verify", f"refs/heads/{branch_point}^{{commit}}")
    if isinstance(standing, StandingStyleProduction):
        checkout, doc = standing.checkout, standing.doc
        branch = read_production(standing.name).merge_branch
    else:
        route = (_enrolled_style_checkout(standing) if standing.doc.exists() or standing.doc.is_symlink()
                 else _unfinished_style_checkout(standing))
        if route is not None:
            checkout, doc, branch = route.checkout, route.doc, route.branch
        else:
            from scripts.production.production_lifecycle import require_guard_ready

            require_guard_ready()
            merge_unit = staff.serving_merge_unit(parent.owner.id)
            if merge_unit is None:
                raise Refusal(f"{parent.name}: add a serving merge unit before creating the style production")
            suffix = f"integration-{name}"
            checkout = build_folders.worktree_path(build_folders.repo_word(repository), merge_unit.name, suffix)
            doc = standing.doc
            branch = f"{merge_unit.name}-{suffix}"
    return StyleProduction(
        parent, repository, name, branch_point, branch_tip, branch, checkout, doc, proposals,
        tuple(line for line in lines if line.startswith("- **Merge tests:** ")),
    )


def _require_branch_point(style: StyleProduction, branch_point: str) -> None:
    if branch_point != style.branch_point:
        raise StyleBranchPointTaken(
            f"{style.repository.name} already has style production {style.name} on branch point {branch_point}; "
            + "one branch point per repository",
        )


def _legacy_branch_point(style: StyleProduction) -> str:
    suffix = style.merge_branch.removeprefix(LEGACY_STYLE_BRANCH_PREFIX)
    branches = _git(style.repository, "for-each-ref", "--format=%(refname:lstrip=2)", "refs/heads").splitlines()
    matches = [branch for branch in branches if branch.replace("/", "-") == suffix]
    if len(matches) > 1:
        raise Refusal(f"{style.merge_branch}: more than one branch point matches: {', '.join(matches)}")
    return matches[0] if matches else suffix


def _branch_description_key(style: StyleProduction) -> str:
    return f"branch.{style.merge_branch}.description"


def _recorded_branch_point(style: StyleProduction) -> str:
    result = worktree_records.git(style.repository, "config", "--local", "--get", _branch_description_key(style), check=False)
    if result.returncode not in (0, 1):
        raise Refusal(result.stderr.strip() or f"cannot read the description for {style.merge_branch}")
    return result.stdout.rstrip("\n")


def _record_branch_point(style: StyleProduction) -> None:
    with worktrees.move_lock():
        branch_point = _recorded_branch_point(style)
        if branch_point:
            _require_branch_point(style, branch_point)
        else:
            _ = _git(style.repository, "config", "--local", _branch_description_key(style), style.branch_point)


def _check_existing(style: StyleProduction) -> bool:
    if style.doc.is_file():
        lines = style.doc.read_text().splitlines()
        _require_branch_point(style, production_field(lines, "Branch point").removeprefix("refs/heads/"))
        existing = read_new_production(style.doc, style.name)
        if production_field(lines, "Kind") != "style" or existing.merge_branch != style.merge_branch:
            raise Refusal(f"{style.doc}: does not describe {style.name} on {style.merge_branch}")
    elif style.merge_branch.startswith(LEGACY_STYLE_BRANCH_PREFIX):
        _require_branch_point(style, _legacy_branch_point(style))
    else:
        branch_point = _recorded_branch_point(style)
        if branch_point:
            _require_branch_point(style, branch_point)
        elif (style.merge_branch == style_production_branch(style.name)
              and ((style.checkout / ".git").exists() or worktree_records.checkout_record(style.checkout) is not None)):
            raise Refusal(f"{style.checkout}: no recorded branch point for {style.merge_branch}")
    if (style.checkout / ".git").exists():
        common = _git(style.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common != _git(style.repository, "rev-parse", "--path-format=absolute", "--git-common-dir"):
            raise Refusal(f"{style.checkout}: belongs to another repository")
        if _git(style.checkout, "symbolic-ref", "--short", "HEAD") != style.merge_branch:
            raise Refusal(f"{style.checkout}: must be on {style.merge_branch}")
    elif style.checkout.exists() and any(style.checkout.iterdir()):
        raise Refusal(f"{style.checkout}: is not an empty directory or a style worktree")
    return style.doc.exists() and style.name in showrunners.timer_names()


def _require_bootstrap_destination(path: Path, repository: Path) -> None:
    if path.resolve() != path or path.is_symlink() or (path.exists() and not path.is_file()):
        raise Refusal(f"{path}: conflicts with the legacy style bootstrap")
    parent = next(folder for folder in path.parents if folder.exists())
    if git_files.locate(parent) != git_files.at(repository):
        raise Refusal(f"{path}: expected a file in {repository}")
    relative = path.relative_to(repository).as_posix()
    require_untracked_ignored(repository, relative, planned_pattern=f"/{relative}")


def _enroll_legacy_bootstrap(style: StyleProduction, standing: NoStyleProduction) -> StyleProduction:
    """Plan publication of the unregistered bootstrap in the main checkout."""
    from scripts.production.production_admission import require_guard_ready

    if any(row.name == style.name
           or isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc))
           and row.doc.path.resolve() in (style.doc, standing.doc)
           for row in staff.snapshot().find(kind="showrunner")):
        raise Refusal(f"{style.name}: the legacy style bootstrap already has a staff row")
    require_guard_ready()
    legacy = read_new_production(style.doc, style.name)
    if (not isinstance(legacy.merge_checkout, LegacyProduction) or legacy.checkout != style.checkout
            or legacy.doc != style.doc or not worktree_records.registered_checkout(style.checkout, style.repository, style.merge_branch)):
        raise Refusal(f"{style.doc}: expected an unregistered style bootstrap on {style.merge_branch}")
    main = git_files.at(style.repository)
    if git_files.head(main) != git_files.OnBranch("main"):
        raise Refusal(f"{style.repository}: expected the main checkout on main")

    source = style.doc.read_bytes().decode("utf-8")
    lines = source.splitlines(keepends=True)
    repository = optional_field(source.splitlines(), "Repository")
    if isinstance(repository, FieldPresent):
        declared = production_field(source.splitlines(), "Repository")
        if Path(declared).expanduser().resolve() != style.repository:
            raise Refusal(f"{style.doc}: Repository must name {style.repository}")
    log = legacy.log.resolve()
    if (legacy.log != log or not log.is_relative_to(style.checkout) or log == style.checkout or log == legacy.doc):
        raise Refusal(f"{legacy.log}: expected a separate log below {style.checkout}")
    relative_log = log.relative_to(style.checkout)
    live_log = style.repository / relative_log
    if standing.doc == live_log:
        raise Refusal(f"{live_log}: the live document and log need separate files")
    for path in (standing.doc, live_log):
        _require_bootstrap_destination(path, style.repository)

    files: list[tuple[Path, str]] = []
    if log.exists():
        if not log.is_file():
            raise Refusal(f"{log}: expected a log file")
        log_text = log.read_bytes().decode("utf-8")
        if live_log.exists():
            if live_log.read_bytes() != log_text.encode("utf-8"):
                raise Refusal(f"{live_log}: conflicts with the legacy style log")
        else:
            files.append((live_log, log_text))
    elif live_log.exists():
        raise Refusal(f"{live_log}: conflicts with the missing legacy style log")

    index = next(index for index, line in enumerate(lines) if line.startswith("- **Showrunner checkout:**"))
    newline = "\r\n" if lines[index].endswith("\r\n") else "\n"
    lines.insert(index, f"- **{MERGE_CHECKOUT_FIELD}:** {style.repository}{newline}")
    repository_line = f"- **Repository:** {style.repository}{newline}"
    for repository_index, line in enumerate(lines):
        if line.startswith("- **Repository:**"):
            lines[repository_index] = repository_line
            break
    else:
        lines.insert(index, repository_line)
    files.append((standing.doc, "".join(lines)))
    return replace(style, doc=standing.doc, enrollment=LegacyStyleEnrollment(relative_log, tuple(files)))


def _production_text(style: StyleProduction) -> str:
    return "\n".join([
        f"# Production — {style.name}", "",
        "> **Status: PRODUCTION — running.** Standing style coverage and mechanical fixes.", "",
        "## Production Context", "",
        f"- **Repository:** {style.repository}",
        f"- **Merge branch:** {style.merge_branch}",
        "- **Kind:** style",
        f"- **Branch point:** {style.branch_point}",
        f"- **Showrunner checkout:** {style.checkout}",
        f"- **{MERGE_CHECKOUT_FIELD}:** {style.repository}",
        f"- **Log:** {style.log}",
        f"- **Proposals:** {style.proposals}",
        "- **Updates:** on demand", "- **Push:** git", *style.merge_tests, "",
        "## Style units", "", "| Unit | Families | Open tickets |", "| --- | --- | --- |",
        *(f"| {style.name}-{family} | {family} | 1 |" for family in FAMILIES), "",
        "## Unit launch", "",
        "Launch each configured unit through `production/add_unit.py --unit-type style`.",
        "The selected command is `/unit:style`; read `docs/direct/style_unit.md` for ticket contracts.", "",
    ])


def _plan_text(style: StyleProduction, assignment: style_config.StyleUnitAssignment, catalog: tuple[rules.Rule, ...]) -> str:
    family = ", ".join(assignment.families)
    family_rules = [f"  - `{rule.rule_id}` — tier {rule.tier}" for rule in catalog if rule.family in assignment.families]
    return "\n".join([
        f"# {assignment.unit} — {family}", "",
        "> **Standing:** style_plan.py", "", "## Delegation Context", "",
        f"- **Project:** {style.repository.name} — standing {family} style coverage",
        f"- **Repository:** {style.repository}",
        (f"- **Launch:** `~/.claude/scripts/lib/py ~/.claude/scripts/production/add_unit.py "
         + f"--production {style.name} {assignment.unit} --unit-type style "
         + f"--plan {PLAN_DIRECTORY / (assignment.unit + '-unit.md')}`"),
        "- **Stack:** Rust; Python style ledger and ticket planner",
        "- **Layout:** Ticket files are selected at refill; this unit owns no files.",
        "- **Key files:** `~/.claude/docs/style_production.md` — standing procedure, yielding and mechanical criteria",
        "- **Test lanes:** The touched packages' unit and integration tests.",
        "- **Build:** For each touched package: `bash ~/.claude/scripts/direct/verify.sh check [<package>]`",
        "- **Test:** For each touched package: `bash ~/.claude/scripts/direct/verify.sh test [<package>]`",
        "- **Lint:** For each touched package: `bash ~/.claude/scripts/direct/verify.sh lint [<package>]`",
        f"- **Family:** {family}; current rules: `~/.claude/scripts/lib/py ~/.claude/scripts/style/rules.py list --repo {style.checkout}`",
        *family_rules,
        "- **Invariants:** Follow `~/.claude/docs/style_production.md`. Product work wins; skip reserved, unmerged or dirty files. "
        + "Apply only mechanical fixes under its criteria; hold every other finding as a proposal. "
        + "Resume the open ticket before refill. The ledger is the record; never commit or shrink this standing plan.",
        "", "## Phases", "",
    ])


def _write_missing(path: Path, text: str) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, text)


def _configure_pilot(style: StyleProduction, pilot: PilotUnits) -> None:
    selected = {unit.family: unit for unit in pilot.units}
    launched = {
        family for family in FAMILIES
        if (style.repository.parent / f"{style.name}-{family}").exists()
    }
    units = ", ".join(f"{style.name}-{family}" for family in FAMILIES if family in launched or family in selected)
    _set_fields(style, {"Pilot units": units})
    for family in FAMILIES:
        if family in launched:
            continue
        path = style.checkout / PLAN_DIRECTORY / f"{style.name}-{family}-unit.md"
        original = path.read_text()
        plan = original.splitlines(keepends=True)
        context = plan.index("## Delegation Context\n") + 1
        stop = next((index for index in range(context, len(plan)) if plan[index].startswith("## ")), len(plan))
        plan[context:stop] = [line for line in plan[context:stop] if not line.lstrip("- ").startswith("**Pilot scope:**")]
        unit = selected.get(family)
        if unit is not None and unit.packages:
            family_line = next(index for index in range(context, len(plan)) if plan[index].startswith("- **Family:**"))
            insertion = family_line + 1
            while insertion < len(plan) and plan[insertion].startswith("  - "):
                insertion += 1
            plan.insert(insertion, "- **Pilot scope:** " + ", ".join(unit.packages) + "\n")
        text = "".join(plan)
        if text != original:
            write_text_atomic(path, text)


def _pilot_steps(steps: tuple[str, ...], pilot: AllStyleUnits | PilotUnits, name: str) -> tuple[str, ...]:
    if isinstance(pilot, AllStyleUnits):
        units = ", ".join(f"{name}-{family}" for family in FAMILIES)
        return (*steps[:4], steps[4] + f"; units {units}", *steps[5:])
    units = "; ".join(f"{name}-{unit.family}" for unit in pilot.units)
    scopes = "; ".join(
        f"{name}-{unit.family}: {', '.join(unit.packages) if unit.packages else 'all crates'}"
        for unit in pilot.units
    )
    return (*steps[:3], steps[3] + f"; pilot units {units}",
            steps[4] + f"; pilot scopes {scopes}; preserve launched units", *steps[5:])


def _exclude(style: StyleProduction) -> None:
    relative_doc = style.doc.relative_to(style.repository if style.doc.is_relative_to(style.repository) else style.checkout)
    patterns = ("/docs/style-production/", f"/{relative_doc.as_posix()}", f"/{style.log.as_posix()}")
    for pattern in patterns:
        ensure_excluded(style.repository, pattern)


def _run_script(script: Path, *arguments: str) -> None:
    result = subprocess.run([str(SCRIPTS / "lib/py"), str(script), *arguments], check=False)
    if result.returncode:
        raise Refusal(f"{script.name} exited {result.returncode}; rerun add_style to continue")


def _open_session_guidance(style: StyleProduction, reason: str) -> str:
    attach = herdr.attach_hint(style.name, style.name)
    return (
        f"{style.name} was left open by add_showrunner.py ({reason}); look at it with {attach}; "
        + "once it has its update timer, rerun add_style, which attaches it; "
        + "if it never gets one, end that session and rerun"
    )


def _attach_parent(style: StyleProduction) -> None:
    current_parent = showrunner_tree.parent_of(style.name)
    if current_parent != style.parent.name:
        if current_parent:
            _run_script(SHOWRUNNERS, "detach", style.name)
        _run_script(SHOWRUNNERS, "attach", style.name, style.parent.name)


def _attach(style: StyleProduction) -> None:
    _attach_parent(style)
    _update_destination(style)
    _run_script(SCRIPTS / "style/style_sync.py", "resume", "--repo", str(style.checkout),
                "--production", style.name, "--branch-point", style.branch_point,
                "--style-branch", style.merge_branch)


def _update_destination(style: StyleProduction) -> None:
    _set_fields(style, {"Proposals": style.proposals})


def _set_fields(style: StyleProduction, replacements: dict[str, str]) -> None:
    previous = style.doc.read_text()
    lines = previous.splitlines()
    for field, value in replacements.items():
        prefix = f"- **{field}:** "
        for index, line in enumerate(lines):
            if line.startswith(prefix):
                lines[index] = prefix + value
                break
        else:
            lines.insert(lines.index("## Production Context") + 1, prefix + value)
    updated = "\n".join(lines) + "\n"
    if updated != previous:
        write_text_atomic(style.doc, updated)


def _steps(style: StyleProduction, existing: bool) -> tuple[str, ...]:
    if existing:
        return (
            f"read {style.parent.doc}; repository {style.repository}; Proposals {style.proposals}",
            f"attach standing {style.name} under {style.parent.name}; branch point {style.branch_point}",
            *((f"skipped: {style.name} already exists",) * 4),
            "update Proposals, run style_sync.py resume; synced tip unchanged",
        )
    return (
        f"read {style.parent.doc}; repository {style.repository}; Proposals {style.proposals}",
        f"find standing {style.name} under {style.parent.name}; branch point {style.branch_point}",
        f"ensure {style.merge_branch} from {style.branch_tip} and worktree {style.checkout}",
        (f"publish the legacy production document at {style.doc}"
         if isinstance(style.enrollment, LegacyStyleEnrollment)
         else f"set Proposals in existing production doc {style.doc}"
         if style.doc.exists() else f"write missing production doc {style.doc}"),
        f"write missing standing plans for the configured units under {style.checkout / PLAN_DIRECTORY}",
        f"exclude docs/style-production/, {style.doc} and {style.log} in shared info/exclude",
        f"initialize ledger at {style.branch_tip} with {style.merge_branch}; launch via add_showrunner.py",
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--production", required=True)
    _ = parser.add_argument("--model", required=True)
    _ = parser.add_argument("--effort", required=True)
    _ = parser.add_argument("--check", action="store_true")
    _ = parser.add_argument("--pilot", action="append", default=[], metavar="UNIT[=CRATE,...]")
    args = parser.parse_args(argv)
    try:
        style = _request(cast(str, args.production))
        existing = _check_existing(style)
        standing = standing_style_production(style.repository)
        if isinstance(standing, NoStyleProduction) and style.doc != standing.doc:
            style = _enroll_legacy_bootstrap(style, standing)
        if (isinstance(standing, NoStyleProduction) and not (style.checkout / ".git").exists()
                and not worktrees.pending_checkout(style.checkout, style.repository, style.merge_branch)):
            raise Refusal(f"the serving merge unit must prepare {style.checkout} on {style.merge_branch}")
        pilot_arguments = cast(list[str], args.pilot)
        if existing and pilot_arguments:
            raise Refusal(f"--pilot applies only while creating {style.name}")
        pilot = _pilot_units(pilot_arguments, style.repository)
        catalog = rules.load_rules(style.repository)
        if cast(bool, args.check):
            steps = _steps(style, existing)
            if not existing:
                steps = _pilot_steps(steps, pilot, style.name)
            for number, step in enumerate(steps, 1):
                print(f"{number}. {step}")
            return 0
        if not existing and style.name not in showrunners.timer_names() and herdr_tab_open(style.name, NoEndingTab()):
            raise Refusal(_open_session_guidance(style, "live session without an update timer"))
        branch = (build_folders.ExistingWorktreeBranch(style.merge_branch)
                  if worktrees.has_branch(style.repository, style.merge_branch)
                  else build_folders.NewWorktreeBranch(style.merge_branch, style.branch_tip))
        repo_word = build_folders.repo_word(style.repository)
        record = worktree_records.checkout_record(style.checkout)
        if _integration_style_branch(style.merge_branch, style.name):
            layout = (record.layout if record is not None else
                      worktrees.output_record(repo_word, style.checkout.parent.name, style.checkout.name))
        else:
            layout = worktrees.unit_checkout_output(style.checkout, repo_word, style.name)
        if (not style.doc.is_file() and not isinstance(style.enrollment, LegacyStyleEnrollment)
                and not style.merge_branch.startswith(LEGACY_STYLE_BRANCH_PREFIX)):
            _record_branch_point(style)
        _ = worktrees.create_checkout(style.checkout, style.repository, branch, layout, owner=style.name,
                                      allow_empty=True, allow_existing=True)
        if existing:
            _attach(style)
            return 0
        _exclude(style)
        if isinstance(style.enrollment, LegacyStyleEnrollment):
            style.enrollment.publish()
        _write_missing(style.doc, _production_text(style))
        _update_destination(style)
        config = style_config.read_config(style.doc)
        for assignment in config.assignments:
            _write_missing(style.checkout / PLAN_DIRECTORY / f"{assignment.unit}-unit.md", _plan_text(style, assignment, catalog))
        if isinstance(pilot, PilotUnits):
            _configure_pilot(style, pilot)
        style_tip = _git(style.repository, "rev-parse", "--verify", f"refs/heads/{style.merge_branch}^{{commit}}")
        with style_ledger.open_ledger(style.checkout) as ledger:
            ledger.ensure_synced_tip(style_tip, style.merge_branch)
        if isinstance(standing_style_production(style.repository), NoStyleProduction):
            production = read_new_production(style.doc, style.name)
            doc_options = ["--doc", str(production.doc)]
        else:
            production = read_production(style.name)
            doc_options = []
        result = subprocess.run([
            str(SCRIPTS / "lib/py"), str(LAUNCHER), "--production", production.name, *doc_options,
            "--model", cast(str, args.model), "--effort", cast(str, args.effort),
        ], check=False)
        if result.returncode in (1, 3):
            print("add_style: " + _open_session_guidance(style, f"exit {result.returncode}"), file=sys.stderr)
            return result.returncode
        if result.returncode:
            raise Refusal(f"{LAUNCHER.name} exited {result.returncode}; rerun add_style to continue")
        _attach_parent(style)
        return 0
    except (Refusal, worktree_records.Refused, rules.RuleValidationError, style_ledger.LedgerError,
            OSError, ValueError, sqlite3.Error) as error:
        print("add_style: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
