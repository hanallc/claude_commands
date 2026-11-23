"""Check that a local judgment grant covers its lint repair."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import sys

from scripts.build_queue import execution_identity
from scripts.build_queue import admission_client, admission_records, admission_wire as wire, placement
from scripts.buildlog import parse, store, treekey
from scripts.lib import git_files


def copied_tree_key(checkout: git_files.Checkout, copy: Path) -> str | None:
    """Read the copy with the source checkout's HEAD and index."""
    context = {"GIT_DIR": str(checkout.git_dir), "GIT_WORK_TREE": str(copy)}
    previous = {name: os.environ[name] for name in context if name in os.environ}
    os.environ.update(context)
    try:
        return treekey.tree_key(str(copy))
    finally:
        for name in context:
            _ = os.environ.pop(name)
        os.environ.update(previous)


def repair_tree_key(cwd: Path, command: list[str], copy_tree: str) -> str | None:
    """Read the tree that contains Cargo's manifest."""
    manifest_word = parse.manifest_path(command)
    if manifest_word is None:
        for folder in (cwd, *cwd.parents):
            if (candidate := folder / "Cargo.toml").is_file():
                manifest_word = str(candidate)
                break
    if manifest_word is None:
        return None
    manifest = (cwd / manifest_word).resolve()
    if not manifest.is_file():
        return None

    checkout = git_files.locate(cwd)
    if copy_tree and manifest.is_relative_to(copy := Path(copy_tree).resolve()):
        return copied_tree_key(checkout, copy)
    if git_files.locate(manifest.parent) != checkout:
        return None
    return treekey.tree_key(str(manifest.parent))


def check_shell_grant(grant_file: Path, reservation: str) -> None:
    """Check that the local shell grant is complete and matches the reservation."""
    contents = grant_file.read_text()
    fields = dict(word.split("=", 1) for word in shlex.split(contents))
    if fields.get("BUILDLOG_MEM_RESERVATION") != reservation:
        raise ValueError("the shell grant belongs to a different reservation")
    required = {
        "BUILDLOG_MEM_OUTCOME", "BUILDLOG_MEM_WAIT_S", "BUILDLOG_MEM_RESERVATION",
        "BUILDLOG_MEM_MAX_PATH", "BUILDLOG_MEM_WAVE_PATH", "BUILDLOG_MEM_SAMPLING_CONTEXT",
        "BUILDLOG_MEM_CLEANUP_TIMING",
    }
    if required - fields.keys():
        raise ValueError("the shell grant has missing fields")
    if (fields.keys() != required
            or contents != "".join(f"{name}={shlex.quote(value)}\n" for name, value in fields.items())):
        raise ValueError("the shell grant has invalid assignments")


def check_repair_grant(assignment_file: Path, grant_file: Path, episode: str, pid: int,
                       copy_tree: str, command: list[str]) -> None:
    """Refuse reuse when the grant cannot cover the repair."""
    saved = wire.read_assignment(assignment_file)
    if not isinstance(saved, wire.LocalPlacementGranted) or not grant_file.is_file():
        raise ValueError("the local grant is missing")

    start = admission_records.process_start_time(pid)
    if (start is None or saved.assignment.episode != episode
            or saved.assignment.owner != placement.SubmittingOwner(store.host_name(), pid, start)):
        raise ValueError("the grant belongs to a different run")

    check_shell_grant(grant_file, saved.grant.reservation)
    cwd = os.getcwd()
    tree = repair_tree_key(Path(cwd), command, copy_tree)
    source = saved.assignment.source_key
    if tree is None or not isinstance(source, execution_identity.SourceKeyKnown) or source.key != tree:
        raise ValueError("the repair tree differs from the judgment tree")

    repair = admission_client.step_request(command, cwd, wire.Owner(pid, start), saved.assignment.since, "", episode)
    if repair.profile.peak.bytes > saved.grant.need:
        raise ValueError("the repair memory estimate exceeds the judgment grant")


def main() -> int:
    assignment, grant, episode, pid, copy_tree, *command = sys.argv[1:]
    try:
        check_repair_grant(Path(assignment), Path(grant), episode, int(pid), copy_tree, command)
    except (OSError, ValueError, wire.AdmissionUnavailable) as error:
        print(str(error).partition("\n")[0])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
