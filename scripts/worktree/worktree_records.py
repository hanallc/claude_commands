"""Read worktree records and the checkouts they name.

This module imports no scratch, sweep or session-move code, so phase_reservation can read records without a cycle.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Generator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import cast

from scripts.lib import build_folders, conf, git_files
from scripts.lib.process_failure import failure_detail


@dataclass(frozen=True)
class UnassignedOutput:
    """The worktree has no recorded output in the build layout."""


@dataclass(frozen=True)
class RecordedWorktreeOutput:
    """The record gives the unit, short name, and output path."""

    unit: str
    short_name: str
    output: Path


class MakeStage(Enum):
    MAKING = "making"
    DONE = "done"


@dataclass(frozen=True)
class WorktreeRecord:
    version: int
    path: str
    branch: str
    repo: str
    base: str
    merge_branch: str
    owner: str
    made_at: str
    layout: RecordedWorktreeOutput | UnassignedOutput = UnassignedOutput()
    stage: MakeStage = MakeStage.DONE


class Refused(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code: int = code


def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo), *arguments],
                            capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise Refused(1, failure_detail(result))
    return result


def state_folder() -> Path:
    return Path(os.environ.get("WORKTREES_STATE_DIR", str(Path.home() / ".local/state/worktrees"))) / "made"


def record_file(path: Path) -> Path:
    return state_folder() / (hashlib.sha256(str(path).encode()).hexdigest() + ".json")


def read_record(path: Path) -> WorktreeRecord:
    try:
        decoded = cast(object, json.loads(path.read_text()))
    except ValueError as error:
        raise ValueError(f"{path}: invalid worktree record: {error}") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"{path}: worktree record must be an object")
    fields = cast(dict[str, object], decoded)

    def text_field(name: str) -> str:
        value = fields.get(name)
        if not isinstance(value, str):
            raise ValueError(f"{path}: worktree record field {name!r} must be text")
        return value

    version = fields.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"{path}: worktree record field 'version' must be an integer")
    try:
        stage = MakeStage(fields.get("stage", MakeStage.DONE.value))
    except ValueError as error:
        raise ValueError(f"{path}: invalid worktree record field 'stage'") from error
    layout: RecordedWorktreeOutput | UnassignedOutput = UnassignedOutput()
    if {"unit", "short_name", "output"} & fields.keys():
        unit, short_name, output = fields.get("unit"), fields.get("short_name"), fields.get("output")
        if not isinstance(unit, str) or not isinstance(short_name, str) or not isinstance(output, str):
            raise ValueError(f"{path}: incomplete worktree output record")
        layout = RecordedWorktreeOutput(unit, short_name, Path(output))
    return WorktreeRecord(
        version=version, path=text_field("path"), branch=text_field("branch"), repo=text_field("repo"),
        base=text_field("base"), merge_branch=text_field("merge_branch"), owner=text_field("owner"),
        made_at=text_field("made_at"), layout=layout, stage=stage)


def has_unit_word(owner: str, unit: str) -> bool:
    """Check if a completed record for this owner uses the unit word."""
    for path in state_folder().glob("*.json"):
        try:
            record = read_record(path)
        except (OSError, ValueError):
            continue
        if (record.owner == owner and record.stage is MakeStage.DONE
                and isinstance(record.layout, RecordedWorktreeOutput) and record.layout.unit == unit):
            return True
    return False


def checkout_record(path: Path) -> WorktreeRecord | None:
    """Read the record for this checkout path if it exists."""
    try:
        value = read_record(record_file(path))
    except FileNotFoundError:
        return None
    if value.path != str(path):
        raise ValueError(f"{path}: worktree record names a different path")
    return value


def registered_worktree_branches(repo: Path) -> Generator[tuple[Path, str]]:
    path = repo
    for field in git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0"):
        if field.startswith("worktree "):
            path = Path(field.removeprefix("worktree "))
        elif field.startswith("branch refs/heads/"):
            yield path, field.removeprefix("branch refs/heads/")


def worktree_branches(repo: Path) -> dict[Path, str]:
    return {path.resolve(): branch for path, branch in registered_worktree_branches(repo)}


def main_checkout(repo: Path) -> Path:
    first = git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0", 1)[0]
    return Path(first.removeprefix("worktree ")).resolve()


def registered_checkout(path: Path, repo: Path, branch: str) -> bool:
    if path.is_symlink() or path == repo or worktree_branches(repo).get(path.resolve()) != branch:
        return False
    try:
        return git_files.at(path).common_dir == git_files.at(repo).common_dir
    except git_files.GitFilesError:
        return False


def main_checkout_path(repo: str) -> Path:
    alias_key = "repo_word.claude_commands"
    alias = conf.read_or_repository("build_folders").values(alias_key).get(alias_key)
    return Path.home() / (".claude" if repo == alias else f"rust/{repo}")


def main_checkout_for_word(repo: str) -> Path:
    """Verify the main checkout selected by the repo word."""
    source = main_checkout_path(repo)
    if not (source / ".git").is_dir() or source.is_symlink() or main_checkout(source) != source.resolve():
        raise ValueError(f"{source}: no main checkout for {repo!r}")
    if build_folders.repo_word(source) != repo:
        raise ValueError(f"{source}: the repo word {repo!r} differs from origin")
    return source.resolve()
