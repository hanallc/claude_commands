"""Capture selection inputs and replay COUNT on a private ledger copy."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Literal, TypedDict, cast

from scripts.lib import conf
from scripts.production import production_doc, staff
from scripts.style import refill_inputs, refill_measure, style_config, style_ledger, style_rules
from scripts.style.unit_files import unit_worktrees
from scripts.style.unit_plans import unit_plan_path

OBSERVATION_VARIABLES = ("LC_ALL", "LC_CTYPE", "LANG", "XDG_CONFIG_HOME", "RIPGREP_CONFIG_PATH", "STAFF_CONFIG")


class CapturedFile(TypedDict):
    source: str
    copy: str
    sha256: str
    kind: str
    replay: str


class ExternalChange(TypedDict):
    source: str
    expected: str
    actual: str


class SourceProjection(TypedDict):
    table: str
    columns: list[str]
    version_triggers: list[str]


class CaptureManifest(TypedDict):
    format: int
    repo: str
    unit: str
    files: list[CapturedFile]
    schema: list[str]
    schema_version: int
    versions: list[list[str | int]]
    source_projection: list[SourceProjection]
    revisions: dict[str, str]
    objects: dict[str, str]
    environment: dict[str, str]
    tools: dict[str, str]
    board: str
    directories: dict[str, list[str]]
    absent_inputs: list[str]
    references: dict[str, str]
    worktree_status: dict[str, str]
    git_configuration: str


class ReplayReport(TypedDict):
    result_digest: str
    external_changes: list[ExternalChange]
    cleared_tables: tuple[str, ...]
    stored: dict[str, object]
    stages: dict[str, object]
    run: dict[str, object]


def git(repo: Path, *arguments: str, input: bytes = b"") -> str:
    """Read Git without changing the repository."""
    return subprocess.run(["git", "-C", str(repo), *arguments], input=input,
                          capture_output=True, check=True).stdout.decode("utf-8")


def fingerprint(path: Path) -> str:
    """Hash a file or record its absence."""
    if not path.is_file():
        return "missing"
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def repository_files(repo: Path) -> Iterator[Path]:
    """Read working files and ignore files without scanning build products."""
    listed = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    yield from (repo / name for name in listed.split("\0") if name)
    # Git excludes these files from its ordinary working file list.
    yield Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    for folder, directories, filenames in os.walk(repo):
        directories[:] = sorted(name for name in directories if name not in {".git", "target", "node_modules", ".venv"})
        for name in filenames:
            if name in {".gitignore", ".ignore", ".rgignore", "Cargo.toml"} or name.endswith(".rs"):
                yield Path(folder) / name


def board_text(repo: Path) -> str:
    """Read the board through its existing command."""
    result = subprocess.run(["cargo-berth", "board", "--json"], cwd=repo,
                            capture_output=True, text=True, check=False)
    return json.dumps(cast(object, json.loads(result.stdout)), sort_keys=True, separators=(",", ":"))


def capture(repo: Path, ledger: Path, unit: str, folder: Path) -> CaptureManifest:
    """Copy selection inputs without writing the source ledger or repository."""
    folder = folder.resolve()
    folder.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=folder.name + "-", dir=folder.parent) as temporary:
        sibling = Path(temporary)
        manifest = capture_inputs(repo, ledger, unit, sibling)
        if folder.exists():
            shutil.rmtree(folder)
        _ = sibling.rename(folder)
    return manifest


def capture_inputs(repo: Path, ledger: Path, unit: str, folder: Path) -> CaptureManifest:
    """Write one complete capture into a new private folder."""
    repo = repo.resolve()
    database = folder / "ledger.sqlite"
    refill_measure.backup_ledger(ledger, database)
    files: list[CapturedFile] = []
    sources: set[tuple[str, str]] = set()
    directories: dict[str, list[str]] = {}
    absent_inputs: list[str] = []

    def save(path: Path, kind: str, replay: str = "compare", target: str = "") -> None:
        source = str(path.absolute())
        role = (source, replay)
        if role in sources:
            return
        sources.add(role)
        copied = target or f"files/{len(files):06d}"
        if not path.is_file():
            absent_inputs.append(source)
            return
        destination = folder / copied
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(path, destination)
        files.append({"source": source, "copy": copied, "sha256": fingerprint(destination), "kind": kind, "replay": replay})

    # The backup is already in its destination.
    files.append({"source": str(ledger.resolve()), "copy": "ledger.sqlite", "sha256": fingerprint(database),
                  "kind": "ledger", "replay": "backup"})
    configuration = conf.directory()
    repository_config = Path(__file__).resolve().parents[2] / "config"
    for path in sorted(repository_config.glob("*.conf")):
        source = configuration / path.name
        save(source if source.is_file() else path, "configuration", "CONF_DIR", "config/" + path.name)
    for path in sorted(configuration.glob("*.conf")):
        save(path, "configuration", "CONF_DIR", "config/" + path.name)
    checkout = Path(__file__).resolve().parents[2]
    for directory in (repository_config, checkout / "scripts/style", checkout / "scripts/production"):
        for path in sorted(directory.glob("*.toml")):
            save(path, "implementation_configuration")
    executable = shutil.which("rg")
    if executable:
        save(Path(executable), "tool")
    save(staff.record_path(), "staff", "STAFF_FILE", "staff.json")
    if os.environ.get("STAFF_CONFIG"):
        save(Path(os.environ["STAFF_CONFIG"]), "staff_configuration", "STAFF_CONFIG", "staff.conf")
    rule_root = style_rules.NATE_STYLE_DIR.resolve()
    rule_paths = sorted((rule_root / "rust").glob("*.md"))
    directories[str(rule_root / "rust")] = [str(path) for path in rule_paths]
    for path in rule_paths:
        save(path, "rule", "STYLE_HISTORY_NATE_STYLE_DIR", "rules/rust/" + path.name)
    for path in repository_files(repo):
        save(path, "worktree")
    for name in ("config", "config.worktree", "info/attributes"):
        save(Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", name).strip()), "git_configuration")
    save(Path.home() / ".gitconfig", "git_configuration")
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    save(xdg / "git/config", "git_configuration", "XDG_CONFIG_HOME", "xdg/git/config")
    excludes = subprocess.run(["git", "-C", str(repo), "config", "--path", "--get", "core.excludesfile"], capture_output=True, text=True)
    if excludes.returncode == 0:
        save(Path(excludes.stdout.strip()).expanduser(), "ignore")
    regex = refill_inputs.regex_configuration()
    if regex.path:
        save(Path(regex.path), "regex_configuration", "RIPGREP_CONFIG_PATH", "ripgrep.conf")
    for path in regex.ignore_files:
        save(Path(path), "ignore")
    with style_ledger.Ledger(repo, database) as private:
        config = style_config.config_for_unit(unit, private.staff_resolver().record)
        save(config.route.doc, "production")
        proposals = Path(production_doc.production_field(config.route.doc.read_text().splitlines(), "Proposals")).expanduser()
        if not proposals.is_absolute():
            proposals = config.route.doc.parent.parent / proposals
        proposal_paths = sorted(proposals.rglob("*.md")) if proposals.is_dir() else []
        directories[str(proposals)] = [str(path) for path in proposal_paths]
        for path in proposal_paths:
            save(path, "proposal")
        rows = unit_worktrees(config.route.doc)
        worktree_status = {str(repo): git(repo, "status", "--porcelain=v1", "-z")}
        for row in rows:
            save(row.worktree / ".unit.toml", "unit_configuration")
            worktree_status[str(row.worktree)] = git(row.worktree, "status", "--porcelain=v1", "-z")
            for path in repository_files(row.worktree):
                save(path, "unit_worktree")
            try:
                save(unit_plan_path(row.unit, row.worktree), "plan")
            except style_ledger.LedgerError:
                pass
        revisions = {"synced": private.synced_commit()}
        references: dict[str, str] = {}
        for row in rows:
            for label, reference in ((f"unit:{row.unit}", row.branch), (f"main:{row.unit}", row.merge_branch),
                                     (f"pushed:{row.unit}", "refs/remotes/origin/" + row.branch)):
                references[label] = reference
                resolved = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", reference],
                                          capture_output=True, text=True)
                revisions[label] = resolved.stdout.strip() if resolved.returncode == 0 else "absent"
        revisions["main"] = git(repo, "rev-parse", config.route.branch_point).strip()
        references["main"] = config.route.branch_point
    objects: set[str] = {value for value in revisions.values() if re.fullmatch("[0-9a-f]{40}", value)}
    with closing(sqlite3.connect(database)) as connection:
        schema = [str(row[0]) for row in cast(list[tuple[str]], connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall())]
        schema_version = cast(int, connection.execute("SELECT max(version) FROM schema_version").fetchone()[0])
        versions = [list(row) for row in cast(list[tuple[str, int, int]], connection.execute("SELECT * FROM refill_tables ORDER BY source_table").fetchall())]
        projections: list[SourceProjection] = []
        tables = [str(row[0]) for row in cast(list[tuple[str]], connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall())]
        for table in tables:
            if table in refill_measure.DERIVED_TABLES or table.startswith("sqlite_"):
                continue
            columns = [str(row[1]) for row in cast(list[tuple[int, str, str, int, object, int]], connection.execute("PRAGMA table_info(" + refill_measure.quoted(table) + ")").fetchall())]
            triggers = [str(row[0]) for row in cast(list[tuple[str]], connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=? AND sql LIKE '%UPDATE refill_tables%' ORDER BY name", (table,)).fetchall())]
            projections.append({"table": table, "columns": columns, "version_triggers": triggers})
            for row in cast(Iterator[tuple[object, ...]], connection.execute("SELECT * FROM " + refill_measure.quoted(table))):
                for value in row:
                    if isinstance(value, str):
                        objects.update(re.findall(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])", value))
        revisions["checkpoints"] = json.dumps(cast(list[tuple[str, str]], connection.execute("SELECT id,checkpoint FROM tickets WHERE checkpoint<>'' ORDER BY id").fetchall()))
    object_lines = git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", input=("\n".join(sorted(objects)) + "\n").encode())
    availability = dict(line.split(" ", 1) for line in object_lines.splitlines())
    board = board_text(repo)
    _ = (folder / "board.json").write_text(board)
    files.append({"source": "cargo-berth board --json", "copy": "board.json", "sha256": fingerprint(folder / "board.json"),
                  "kind": "board", "replay": "compare"})
    stamp = style_config.reader_install_folder() / "current/stamp"
    save(stamp, "reader_stamp")
    save(xdg / "cargo-mend/config.toml", "lint_configuration", "XDG_CONFIG_HOME", "xdg/cargo-mend/config.toml")
    rg = subprocess.run(["rg", "--version"], capture_output=True, text=True, check=True).stdout
    # Ledger migration can change the private backup, so hash it after admission.
    files[0]["sha256"] = fingerprint(database)
    manifest: CaptureManifest = {"format": 1, "repo": str(repo), "unit": unit, "files": files, "schema": schema,
        "schema_version": schema_version, "versions": versions, "source_projection": projections,
        "revisions": revisions, "objects": availability,
        "environment": {key: os.environ[key] for key in OBSERVATION_VARIABLES if key in os.environ},
        "tools": {"python": platform.python_version(), "ripgrep": rg, "git": git(repo, "--version").strip(),
                  "implementation_commit": git(checkout, "rev-parse", "HEAD").strip(),
                  "implementation_diff": hashlib.sha256(git(checkout, "diff", "--", "scripts/style").encode()).hexdigest(),
                  "selection_sources": hashlib.sha256("\n".join(
                      path.name + ":" + fingerprint(path) for path in sorted((checkout / "scripts/style").glob("*.py"))).encode()).hexdigest(),
                  "reader_source": git(checkout, "rev-parse", "HEAD:rust/style_reader").strip()},
        "board": "board.json", "directories": directories, "absent_inputs": absent_inputs, "references": references,
        "worktree_status": worktree_status, "git_configuration": git(repo, "config", "--list", "-z")}
    _ = (folder / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest


def read_manifest(folder: Path) -> CaptureManifest:
    """Read the versioned capture manifest."""
    return cast(CaptureManifest, json.loads((folder / "manifest.json").read_text()))


def external_changes(folder: Path, manifest: CaptureManifest) -> list[ExternalChange]:
    """Report changed unconfigured inputs without refusing a replay."""
    changes: list[ExternalChange] = []
    for entry in manifest["files"]:
        if entry["replay"] != "backup" and entry["sha256"] != fingerprint(folder / entry["copy"]):
            changes.append({"source": str(folder / entry["copy"]), "expected": entry["sha256"], "actual": fingerprint(folder / entry["copy"])})
        if entry["replay"] not in {"compare", "STAFF_CONFIG"}:
            continue
        if entry["kind"] == "board":
            actual = hashlib.sha256(board_text(Path(manifest["repo"])).encode()).hexdigest()
        else:
            actual = fingerprint(Path(entry["source"]))
        if actual != entry["sha256"]:
            changes.append({"source": entry["source"], "expected": entry["sha256"], "actual": actual})
    repo = Path(manifest["repo"])
    for worktree, expected in manifest["worktree_status"].items():
        actual = git(Path(worktree), "status", "--porcelain=v1", "-z")
        if actual != expected:
            changes.append({"source": "git:status:" + worktree, "expected": hashlib.sha256(expected.encode()).hexdigest(),
                            "actual": hashlib.sha256(actual.encode()).hexdigest()})
    for label, reference in manifest["references"].items():
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", reference], capture_output=True, text=True)
        actual = result.stdout.strip() if result.returncode == 0 else "absent"
        if actual != manifest["revisions"][label]:
            changes.append({"source": "git:" + reference, "expected": manifest["revisions"][label], "actual": actual})
    for source in manifest["absent_inputs"]:
        actual = fingerprint(Path(source))
        if actual != "missing":
            changes.append({"source": source, "expected": "missing", "actual": actual})
    for directory, original in manifest["directories"].items():
        if directory.endswith("/rust"):
            continue  # The rule folder uses the captured setting.
        current = sorted(str(path) for path in Path(directory).rglob("*.md"))
        if current != original:
            changes.append({"source": directory, "expected": hashlib.sha256(json.dumps(original).encode()).hexdigest(),
                            "actual": hashlib.sha256(json.dumps(current).encode()).hexdigest()})
    return changes


def replay_environment(folder: Path, reader: style_config.ReaderSource, private: Path) -> dict[str, str]:
    """Select captured inputs through existing production settings."""
    manifest = read_manifest(folder)
    config = private / "config"
    _ = shutil.copytree(folder / "config", config)
    refill_measure.select_reader(config, reader)
    environment = {key: value for key, value in os.environ.items() if key not in OBSERVATION_VARIABLES}
    environment.update(manifest["environment"])
    environment.update(CONF_DIR=str(config), STAFF_FILE=str(folder / "staff.json"),
                       STYLE_HISTORY_NATE_STYLE_DIR=str(folder / "rules"), STYLE_LEDGER_STATE_DIR=str(private / "state"),
                       XDG_CONFIG_HOME=str(folder / "xdg"))
    for entry in manifest["files"]:
        if entry["replay"] in {"STAFF_CONFIG", "RIPGREP_CONFIG_PATH"}:
            environment[entry["replay"]] = str(folder / entry["copy"])
    return environment


def git_configuration_changes(manifest: CaptureManifest, environment: dict[str, str]) -> list[ExternalChange]:
    """Compare effective Git configuration under the replay settings."""
    actual = subprocess.run(["git", "-C", manifest["repo"], "config", "--list", "-z"],
                            env=environment, capture_output=True, check=True).stdout.decode("utf-8")
    expected = manifest["git_configuration"]
    if actual == expected:
        return []
    return [{"source": "git:configuration", "expected": hashlib.sha256(expected.encode()).hexdigest(),
             "actual": hashlib.sha256(actual.encode()).hexdigest()}]


def replay(folder: Path, reader: style_config.ReaderSource, workspace: Path, *, stages: bool = False) -> ReplayReport:
    """Run one fresh COUNT process from a private capture copy."""
    manifest = read_manifest(folder)
    changes = external_changes(folder, manifest)
    with tempfile.TemporaryDirectory(prefix="selection-replay-", dir=workspace) as temporary:
        private = Path(temporary)
        repo = Path(manifest["repo"])
        database = private / "state" / style_ledger.ledger_location(repo).database.parent.name / "ledger.sqlite"
        database.parent.mkdir(parents=True)
        refill_measure.backup_ledger(folder / "ledger.sqlite", database)
        removed = refill_measure.clear_derived(database)
        environment = replay_environment(folder, reader, private)
        changes.extend(git_configuration_changes(manifest, environment))
        run = refill_measure.measured_run(repo, database, manifest["unit"], environment, stages=stages)
        return {"result_digest": cast(str, run["result_digest"]), "external_changes": changes,
                "cleared_tables": removed, "stored": cast(dict[str, object], run["stored"]),
                "stages": cast(dict[str, object], run.get("stages", {})), "run": run}


def measure_capture(folder: Path, reader: style_config.ReaderSource, workspace: Path, *, stages: bool = False) -> dict[str, object]:
    """Measure cold and warm COUNT processes from one capture."""
    manifest = read_manifest(folder)
    changes = external_changes(folder, manifest)
    with tempfile.TemporaryDirectory(prefix="selection-measure-", dir=workspace) as temporary:
        private = Path(temporary)
        repo = Path(manifest["repo"])
        database = private / "state" / style_ledger.ledger_location(repo).database.parent.name / "ledger.sqlite"
        database.parent.mkdir(parents=True)
        refill_measure.backup_ledger(folder / "ledger.sqlite", database)
        removed = refill_measure.clear_derived(database)
        environment = replay_environment(folder, reader, private)
        changes.extend(git_configuration_changes(manifest, environment))
        cold = refill_measure.measured_run(repo, database, manifest["unit"], environment, stages=stages)
        warm = refill_measure.measured_run(repo, database, manifest["unit"], environment, stages=stages)
        return {"reader": reader.value, "external_changes": changes, "cleared_tables": removed, "cold": cold, "warm": warm,
                "same_result": cold["result_digest"] == warm["result_digest"]}


class CaptureArguments(argparse.Namespace):
    command: Literal["capture", "replay", "measure"] = "capture"
    repo: Path = Path()
    ledger: Path = Path()
    unit: str = ""
    folder: Path = Path()
    workspace: Path = Path()
    reader: style_config.ReaderSource = style_config.ReaderSource.PYTHON
    stages: bool = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("capture")
    for flag, kind in (("--repo", Path), ("--ledger", Path), ("--unit", str), ("--folder", Path)):
        _ = create.add_argument(flag, type=kind, required=True)
    for name in ("replay", "measure"):
        child = commands.add_parser(name)
        _ = child.add_argument("--folder", type=Path, required=True)
        _ = child.add_argument("--workspace", type=Path, required=True)
        _ = child.add_argument("--reader", type=style_config.ReaderSource, choices=list(style_config.ReaderSource), required=True)
        _ = child.add_argument("--stages", action="store_true")
    args = parser.parse_args(namespace=CaptureArguments())
    if args.command == "capture":
        manifest = capture(args.repo, args.ledger, args.unit, args.folder)
        result: object = {"folder": str(args.folder), "files": len(manifest["files"]),
                          "schema_version": manifest["schema_version"], "objects": len(manifest["objects"])}
    elif args.command == "replay":
        result = replay(args.folder, args.reader, args.workspace, stages=args.stages)
    else:
        result = measure_capture(args.folder, args.reader, args.workspace, stages=args.stages)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
