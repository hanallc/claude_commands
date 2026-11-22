#!/usr/bin/env python3
"""Protect the main checkouts of enrolled showrunners."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from fnmatch import fnmatchcase
from importlib import import_module
from pathlib import Path
from threading import Thread
from typing import Literal, TypedDict, cast, final

from scripts.copies import shell_syntax as shell
from scripts.lib import git_files

CLAUDE_MATCHER = "Bash|Edit|Write|NotebookEdit|MultiEdit|Agent"
CLAUDE_TOOLS = frozenset(CLAUDE_MATCHER.split("|"))
CODEX_TOOLS = frozenset(("Bash", "apply_patch"))
PUBLICATION = frozenset(("merge_checkpoint.py", "land_main.py", "main_publish.py"))
READ_ONLY_GIT = frozenset(("status", "log", "diff", "show", "rev-parse", "grep", "ls-files", "cat-file",
                          "ls-tree", "ls-remote", "rev-list", "describe", "show-ref", "for-each-ref",
                          "merge-base", "blame", "name-rev", "check-ignore", "check-attr", "check-ref-format"))
GIT_PATHS = {"--git-dir": "GIT_DIR", "--work-tree": "GIT_WORK_TREE"}
GIT_ENV_PATHS = frozenset(("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_CONFIG"))
GIT_WORKTREE_WRITES = frozenset(("checkout", "switch", "reset", "restore", "clean", "merge", "pull", "rebase",
                                "cherry-pick", "revert", "am", "stash", "apply", "mv", "rm"))
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")
MAX_SHELL_TARGET_SCOPES = 32
SHELL_BUILTINS = frozenset(("cd", "pushd", "popd", "dirs", "export", "declare", "typeset", "unset", "command", "builtin", "exec"))
TARGET_ENV = GIT_ENV_PATHS | {"PATH"}
AGENT_REFUSAL = (
    "An enrolled showrunner may write only through the permitted paths and routes below. "
    "Refuse built-in `Agent` launches, including read-only launches. "
    "Use a read-only review worker for reviews. "
    "Skills that need built-in `Agent` use the review worker route."
)
PERMITTED_ROUTES = (
    "Permitted paths and routes:\n"
    "- Your live production document, LOG, and production notes folder.\n"
    "- Your exact Claude runtime files and the authorized settings route.\n"
    "- Existing lifecycle, launch, sync, and question or review routes.\n"
    "- The serving merge unit for publication.\n"
    "- A read-only review worker for reviews."
)


@dataclass(frozen=True)
class Allow:
    """The guard permits the call."""


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class Unknown:
    scope: Literal["identity", "target"]
    reason: str


Decision = Allow | Deny | Unknown


@dataclass(frozen=True)
class NotEnrolled:
    """The session or production has no guard enrollment."""


@dataclass(frozen=True)
class EnrolledPaths:
    main: Path
    live_doc: Path
    log: Path
    notes: Path
    integration: Path


@dataclass(frozen=True)
class Enrolled:
    paths: EnrolledPaths
    session_id: str
    claude_home: Path
    transcript: Path | NoTranscript


@dataclass(frozen=True)
class UnusableEnrollment:
    """The production declares enrollment but its paths are unavailable."""

    session_id: str
    reason: str


@dataclass(frozen=True)
class NoTranscript:
    """The payload does not identify this session's transcript."""


Resolver = Callable[[Path], EnrolledPaths | NotEnrolled]


@dataclass(frozen=True)
class FileWrite:
    path: Path
    permitted_runtime_file: bool = False


@dataclass(frozen=True)
class GitChange:
    command: str
    paths: tuple[Path, ...]


class RepositoryConfigWrite(Enum):
    LOCAL = "local"
    WORKTREE = "worktree"


class DiscoverGitDirectory(Enum):
    FROM_CWD = "from cwd"


@dataclass(frozen=True)
class SharedRefWrite:
    refs: tuple[str, ...]
    current_branch: bool = False
    refs_from_input: bool = False


class RepositoryStorageWrite(Enum):
    WORKTREE_METADATA = "worktree metadata"
    APPEND_OBJECTS = "append objects"
    SHARED_STATE = "shared repository state"


GitEffect = RepositoryConfigWrite | SharedRefWrite | RepositoryStorageWrite


@dataclass(frozen=True)
class GitRepositoryWrite:
    command: str
    checkout_directory: Path | Unknown
    git_directory: Path | Unknown | DiscoverGitDirectory
    effects: tuple[GitEffect, ...]


@dataclass(frozen=True)
class DirectPublication:
    command: str


@dataclass(frozen=True)
class AgentLaunch:
    """The tool launches a built-in agent."""


Operation = FileWrite | GitChange | DirectPublication | AgentLaunch
TargetCandidate = Operation | GitRepositoryWrite | Unknown
Policy = Callable[[Enrolled, tuple[Operation, ...]], Decision]


class ConfiguredBudget(Enum):
    READ_CONFIG = "read config"


class TargetError(ValueError):
    """The guard cannot resolve a write target."""


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _items(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def policy_timeout() -> float:
    from scripts.lib import conf

    with conf.diagnostics(report=False):
        config = conf.read_or_repository("showrunner_write_guard")
        timeout = config.value("policy_timeout_seconds", float, recover_invalid=False)
    if not math.isfinite(timeout) or timeout < 0.001:
        raise ValueError("invalid showrunner guard policy timeout")
    return timeout


def hook_timeout() -> float:
    """Reserve time to resolve scope and render the policy decision."""
    try:
        return policy_timeout() + 5
    except (OSError, ValueError):
        # The front still needs time to refuse a protected call.
        return 7


def resolve_production_paths(doc: Path) -> EnrolledPaths | NotEnrolled:
    """Use the production resolver when the paths phase provides it."""
    from scripts.production import production_doc

    module = vars(production_doc)
    try:
        resolver = cast(Callable[[Path], object], module["resolve_production_paths"])
        legacy = cast(type[object], module["LegacyProduction"])
        kind = cast(type[object], module["EnrolledPaths"])
    except KeyError as error:
        raise LookupError("the production path resolver is unavailable") from error
    paths = resolver(doc)
    if isinstance(paths, legacy):
        return NotEnrolled()
    if not isinstance(paths, kind):
        raise TypeError("production resolver returned an invalid result")
    return cast(EnrolledPaths, paths)


def _path(value: str, cwd: Path | Unknown) -> Path:
    if not value or "\0" in value or shell.UNKNOWN_VALUE in value:
        raise TargetError("a path is missing or depends on a shell expansion")
    path = Path(value).expanduser()
    if not path.is_absolute():
        if isinstance(cwd, Unknown):
            raise TargetError(cwd.reason)
        path = cwd / path
    return path.resolve()


def _cwd(payload: dict[str, object]) -> Path | Unknown:
    value = payload.get("cwd")
    if not isinstance(value, str) or not value or not Path(value).expanduser().is_absolute():
        return Unknown("target", "the payload has no absolute cwd")
    return _path(value, Path.home())


def _transcript(payload: dict[str, object], session: str, home: Path) -> Path | NoTranscript:
    value = payload.get("transcript_path")
    if not isinstance(value, str) or not Path(value).is_absolute():
        return NoTranscript()
    try:
        path = _path(value, home)
    except (OSError, RuntimeError, ValueError):
        return NoTranscript()
    projects = home / "projects"
    if (path.name == f"{session}.jsonl" and path.parent.parent == projects):
        return path
    return NoTranscript()


def _enrollment(payload: dict[str, object], resolver: Resolver) -> Enrolled | UnusableEnrollment | NotEnrolled | Unknown:
    from scripts.production import staff

    session = payload.get("session_id")
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
        return Unknown("identity", "the payload has no valid session ID")
    try:
        row = staff.by_current_session(session)
    except Exception as error:
        return Unknown("identity", str(error))
    if row is None or row.kind != "showrunner" or not isinstance(row.doc, staff.LiveDoc):
        return NotEnrolled()
    try:
        paths = resolver(row.doc.path)
    except (OSError, ValueError) as error:
        return UnusableEnrollment(session, str(error))
    except Exception as error:
        return Unknown("identity", str(error))
    if isinstance(paths, NotEnrolled):
        return paths
    home = (Path.home() / ".claude").resolve()
    return Enrolled(paths, session, home, _transcript(payload, session, home))


def _protected_roots(
    enrollment: Enrolled | UnusableEnrollment, resolver: Resolver,
) -> tuple[set[Path], tuple[Unknown, ...]]:
    from scripts.production import staff

    roots = {enrollment.paths.main} if isinstance(enrollment, Enrolled) else set[Path]()
    unread: list[Unknown] = []
    try:
        rows = staff.find(kind="showrunner")
    except Exception as error:
        return roots, (Unknown("target", str(error)),)
    for row in rows:
        if row.current_session == enrollment.session_id or not isinstance(row.doc, staff.LiveDoc):
            continue
        try:
            paths = resolver(row.doc.path)
            if not isinstance(paths, NotEnrolled):
                roots.add(paths.main)
        except Exception as error:
            unread.append(Unknown("target", str(error)))
    return roots, tuple(unread)


def _plan_owned(path: Path, enrolled: Enrolled) -> bool:
    if path.name == f"{enrolled.session_id}.md":
        return True
    transcript = enrolled.transcript
    if isinstance(transcript, NoTranscript):
        return False
    try:
        with transcript.open("rb") as source:
            size = source.seek(0, os.SEEK_END)
            _ = source.seek(max(0, size - 131072))
            lines = source.read(131072).splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            entry = _object(cast(object, json.loads(line)))
        except (ValueError, UnicodeError):
            continue
        slug = entry.get("slug")
        if isinstance(slug, str) and entry.get("sessionId") == enrolled.session_id:
            return path.name == f"{slug}.md"
    return False


def _read_object(path: Path) -> dict[str, object]:
    return _object(cast(object, json.loads(path.read_text(encoding="utf-8"))))


def _team_owned(config: Path, session_id: str) -> bool:
    try:
        team = _read_object(config)
    except (OSError, ValueError):
        return False
    return team.get("leadSessionId") == session_id or any(
        _object(member).get("sessionId") == session_id for member in _items(team.get("members"))
    )


def _session_record_owned(path: Path, session_id: str) -> bool:
    if path.name == f"{session_id}.json":
        return True
    try:
        return path.suffix == ".json" and _read_object(path).get("sessionId") == session_id
    except (OSError, ValueError):
        return False


def _git_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}


def _untracked_settings(home: Path) -> bool:
    # Read index evidence only for this exception. The policy uses the result.
    try:
        result = subprocess.run(
            ["git", "-C", str(home), "ls-files", "-z", "--", "settings.local.json"],
            env=_git_environment(), capture_output=True, check=False, timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and not result.stdout


def _memory_folder(cwd: Path, enrolled: Enrolled) -> str:
    try:
        checkout = git_files.locate(cwd)
    except git_files.NotACheckout:
        root = cwd
    else:
        if checkout.git_dir == checkout.common_dir:
            root = checkout.toplevel
        elif checkout.common_dir.name == ".git":
            root = checkout.common_dir.parent
        else:
            main = git_files.at(enrolled.paths.main)
            if main.common_dir != checkout.common_dir:
                raise TargetError("cannot locate the main checkout for the session's memory")
            root = main.toplevel
    return re.sub(r"[^A-Za-z0-9]", "-", str(root))


def _runtime_file(path: Path, enrolled: Enrolled, cwd: Path | Unknown) -> bool:
    home = enrolled.claude_home
    if not path.is_relative_to(home) or path.is_dir():
        return False
    parts = path.relative_to(home).parts
    session = enrolled.session_id
    project = enrolled.transcript
    match parts:
        case ("history.jsonl",):
            return True
        case ("settings.local.json",):
            return _untracked_settings(home)
        case ("plans", _) if path.suffix == ".md":
            return _plan_owned(path, enrolled)
        case ("todos", name):
            return name == f"{session}.json" or name == f"{session}-agent-{session}.json"
        case ("tasks", listing, _) if path.suffix == ".json":
            return listing == (os.environ.get("CLAUDE_CODE_TASK_LIST_ID") or session)
        case ("teams", team, _):
            return _team_owned(home / "teams" / team / "config.json", session)
        case ("sessions", _):
            return _session_record_owned(path, session)
        case ("state" | "file-history" | "session-env", owner, _):
            return owner == session
        case ("shell-snapshots" | "paste-cache" | "uploads", _):
            return True
        case ("projects", folder, "memory", _) if path.suffix == ".md":
            return isinstance(cwd, Path) and folder == _memory_folder(cwd, enrolled)
        case ("projects", folder, name):
            return isinstance(project, Path) and folder == project.parent.name and name == project.name
        case ("projects", folder, owner, "tool-results", _):
            return isinstance(project, Path) and folder == project.parent.name and owner == session
        case _:
            return False


def _refusal(operation: Operation) -> Deny:
    match operation:
        case FileWrite(path):
            return Deny(f"Cannot write to protected checkout path {path}.\n{PERMITTED_ROUTES}")
        case GitChange(command, targets):
            return Deny(f"Git {command} changes a protected checkout at {targets[0]}.\n{PERMITTED_ROUTES}")
        case DirectPublication(command):
            return Deny(f"Run publication through the serving merge unit. Command: {command}.")
        case AgentLaunch():
            return Deny(f"{AGENT_REFUSAL}\n\n{PERMITTED_ROUTES}")


def decide(enrolled: Enrolled, operations: tuple[Operation, ...]) -> Allow | Deny:
    """Apply the policy to resolved operations in the guard's scope."""
    paths = enrolled.paths
    for operation in operations:
        if isinstance(operation, FileWrite):
            path = operation.path
            if (operation.permitted_runtime_file or path in (paths.live_doc, paths.log)
                    or path != paths.notes and path.is_relative_to(paths.notes)):
                continue
        return _refusal(operation)
    return Allow()


def _file_writes(value: str, cwd: Path | Unknown) -> Iterable[FileWrite | Unknown]:
    try:
        path = _path(value, cwd)
    except Exception as error:
        yield Unknown("target", str(error))
        path = Unknown("target", str(error))
    else:
        yield FileWrite(path)
    if not value or "\0" in value or shell.UNKNOWN_VALUE in value:
        return
    try:
        raw = Path(value).expanduser()
        if raw.name not in ("", ".", ".."):
            entry = _path(str(raw.parent), cwd) / raw.name
            if entry != path:
                yield FileWrite(entry)
    except Exception as error:
        yield Unknown("target", str(error))


def _file_values(tool: str, inputs: dict[str, object]) -> Iterable[str | Unknown]:
    if tool == "apply_patch":
        from scripts.direct.phase_reservation import patch_paths

        patch = inputs.get("command", inputs.get("patch"))
        paths = patch_paths(patch) if isinstance(patch, str) else []
        yield from paths or [Unknown("target", "the patch has no file headers")]
        return
    found = False
    for entry in (inputs, *(_object(edit) for edit in _items(inputs.get("edits")))):
        for key in ("file_path", "notebook_path", "file_paths"):
            if key not in entry:
                continue
            found = True
            value = entry[key]
            values = _items(value)
            for target in values or [value]:
                yield target if isinstance(target, str) else Unknown("target", f"invalid {key}")
    if not found:
        yield Unknown("target", f"{tool} has no file path")


CONFIG_VALUE_OPTIONS = frozenset(("-f", "--file", "--blob", "--type", "--default", "--comment", "--value", "--url"))
CONFIG_READ_OPTIONS = frozenset((
    "-l", "--list", "--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color", "--get-colorbool",
))
CONFIG_WRITE_OPTIONS = frozenset((
    "--add", "--unset", "--unset-all", "--replace-all", "--rename-section", "--remove-section", "--edit", "-e",
))


def _git_options(arguments: list[str], valued: frozenset[str]) -> tuple[dict[str, str], list[str]]:
    options: dict[str, str] = {}
    operands: list[str] = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        index += 1
        if word == "--":
            operands.extend(arguments[index:])
            break
        if not word.startswith("-") or word == "-":
            operands.append(word)
            continue
        option, equals, value = word.partition("=")
        if not option.startswith("--") and len(option) > 2 and option[:2] in valued:
            value = option[2:] + (equals + value if equals else "")
            option, equals = option[:2], "="
        if option in valued and not equals and index < len(arguments):
            value = arguments[index]
            index += 1
        options[option] = value
    return options, operands


def _read_only_git(command: str, arguments: list[str]) -> bool:
    if command in READ_ONLY_GIT:
        return True
    if command == "fetch":
        # A named remote can update tracking refs even when the refspec has no destination.
        if "--dry-run" in arguments:
            return True
        sources = [value for value in arguments if not value.startswith("-")]
        return ({"--no-write-fetch-head", "--no-tags", "--refmap="}.issubset(arguments)
                and len(sources) > 1 and all(":" not in ref for ref in sources[1:])
                and not any(value in arguments for value in ("--all", "--multiple", "--prune", "-p", "--tags")))
    if command == "config":
        options, operands = _git_options(arguments, CONFIG_VALUE_OPTIONS)
        write = bool(CONFIG_WRITE_OPTIONS.intersection(options)) or (
            bool(operands) and operands[0] in {"set", "unset", "rename-section", "remove-section", "edit"}
        )
        return not write and (bool(CONFIG_READ_OPTIONS.intersection(options)) or operands[:1] in (["get"], ["list"]))
    if command in {"branch", "tag"}:
        return not arguments or arguments[0] in {"--list", "-l", "--show-current"} or arguments == ["-v"]
    if command in {"remote", "worktree"}:
        return not arguments or arguments[0] == "list" or arguments == ["-v"]
    return command in {"help", "version", "--version", "--help"}


def _option_path(arguments: list[str], *options: str) -> Iterable[str]:
    for index, argument in enumerate(arguments):
        option, equals, value = argument.partition("=")
        if option in options:
            yield value if equals else arguments[index + 1] if index + 1 < len(arguments) else ""


CLONE_VALUE_OPTIONS = frozenset((
    "-b", "--branch", "--revision", "-o", "--origin", "-u", "--upload-pack", "-c", "--config", "-j", "--jobs",
    "--template", "--reference", "--reference-if-able", "--separate-git-dir", "--depth",
    "--shallow-since", "--shallow-exclude", "--filter", "--bundle-uri", "--server-option", "--ref-format",
))


def _clone_destination(arguments: list[str]) -> str:
    options, operands = _git_options(arguments, CLONE_VALUE_OPTIONS)
    if len(operands) > 1:
        return operands[1]
    if not operands:
        return ""
    source = operands[0].rstrip("/")
    name = source.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if name == ".git":
        name = source.removesuffix("/.git").rsplit("/", 1)[-1]
    else:
        name = name.removesuffix(".git").removesuffix(".bundle")
    return name + ".git" if {"--bare", "--mirror"}.intersection(options) else name


def _git_effects(command: str, arguments: list[str]) -> tuple[GitEffect, ...]:
    local = RepositoryStorageWrite.WORKTREE_METADATA
    objects = RepositoryStorageWrite.APPEND_OBJECTS
    shared = RepositoryStorageWrite.SHARED_STATE
    config = RepositoryConfigWrite.LOCAL
    if command == "commit":
        return (local, objects, SharedRefWrite((), current_branch=True))
    if command in {"add", "apply", "update-index"}:
        return (local, objects)
    if command in {"clean", "restore", "mv", "rm"}:
        return (local,)
    if command == "switch" and arguments == ["--detach", "HEAD"]:
        return (local,)
    if command == "reset":
        return (local, SharedRefWrite((), current_branch=True))
    if command == "branch":
        options, operands = _git_options(arguments, frozenset(("--format", "--sort", "--points-at", "--set-upstream-to", "-u")))
        if {"-m", "-M", "--move", "-c", "-C", "--copy"}.intersection(options):
            return (config, SharedRefWrite(tuple(operands[:2]), current_branch=len(operands) == 1))
        if {"-d", "-D", "--delete"}.intersection(options):
            return (config, SharedRefWrite(tuple(operands)))
        refs = SharedRefWrite(tuple(operands[:1]))
        ref_only = "--no-track" in options and set(options) <= {"--no-track", "-f", "--force"}
        return (refs,) if ref_only else (config, refs)
    if command in {"update-ref", "symbolic-ref"}:
        options, operands = _git_options(arguments, frozenset(("-m",)))
        refs = tuple(operands[:1])
        if "--stdin" in options:
            return (SharedRefWrite((), refs_from_input=True),)
        if refs == ("HEAD",):
            if command == "symbolic-ref" or "--no-deref" in options:
                return (local,)
            return (local, SharedRefWrite((), current_branch=True))
        return (SharedRefWrite(refs),) if refs else (shared,)
    if command == "fetch":
        options, operands = _git_options(arguments, frozenset(("--refmap", "--depth", "--jobs", "-j",
                                                              "--upload-pack", "-u", "--server-option")))
        destinations = tuple(value.partition(":")[2] for value in operands[1:] if ":" in value)
        if destinations and options.get("--refmap") == "" and "--no-tags" in options and not (
            {"--all", "--multiple", "--prune", "-p", "--stdin", "--tags"}.intersection(options)
        ):
            return (local, objects, SharedRefWrite(destinations))
        return (local, objects, shared)
    if command == "submodule" and arguments[:1] == ["init"]:
        return (config,)
    if command == "remote":
        return (config, shared)
    return (shared,)


def _git_operation(words: list[str], cwd: Path | Unknown, variables: dict[str, str]) -> Iterable[TargetCandidate]:
    overrides = {name: variables[name] for name in GIT_ENV_PATHS - {"GIT_CONFIG"}
                 if name in variables and variables[name]}
    directories: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        option, equals, value = word.partition("=")
        if option in {*GIT_PATHS, "-C", "-c", "--config-env", "--namespace", "--super-prefix", "--attr-source"}:
            if not equals:
                index += 1
                if index == len(words):
                    yield Unknown("target", f"Git has no value for {option}")
                    return
                value = words[index]
            if option == "-C":
                directories.append(value)
            elif option in GIT_PATHS:
                overrides[GIT_PATHS[option]] = value
        elif word in {"--bare", "--no-replace-objects", "--no-optional-locks", "--paginate", "--no-pager", "-P", "-p",
                      "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs"}:
            if word == "--bare" and "GIT_DIR" not in overrides:
                overrides["GIT_DIR"] = "."
        elif word.startswith("--exec-path="):
            pass
        else:
            break
        index += 1
    if index == len(words):
        return
    command = words[index]
    args = words[index + 1:]
    if command == "push":
        yield DirectPublication("git push")
        return
    extra_paths = list(_option_path(args, "--output")) if command in {"diff", "show", "log"} else []
    read_only = _read_only_git(command, args)
    if not extra_paths and read_only:
        return
    use_cwd = not read_only and ("GIT_DIR" not in overrides or (
        command in GIT_WORKTREE_WRITES and "GIT_WORK_TREE" not in overrides
    ))
    effects = () if read_only else _git_effects(command, args)
    if command == "config":
        config_options, _ = _git_options(args, CONFIG_VALUE_OPTIONS)
        extra_paths.extend(value for option, value in config_options.items() if option in {"-f", "--file"})
        if configured := variables.get("GIT_CONFIG"):
            extra_paths.append(configured)
        outside_config = bool(extra_paths) or bool({"--global", "--system"}.intersection(config_options))
        if outside_config:
            use_cwd = False
            effects = ()
        else:
            config = RepositoryConfigWrite.WORKTREE if "--worktree" in config_options else RepositoryConfigWrite.LOCAL
            effects = (config,)
    if command == "clone":
        destination = _clone_destination(args)
        if not destination:
            yield Unknown("target", "Git clone has no known destination")
            return
        extra_paths.append(destination)
        extra_paths.extend(_option_path(args, "--separate-git-dir"))
        use_cwd = False
        effects = ()
    if command == "worktree" and args and args[0] in {"add", "move", "remove", "lock", "unlock", "repair"}:
        extra_paths.extend(value for value in args[1:] if not value.startswith("-"))
    if command == "init" and args and not args[-1].startswith("-"):
        extra_paths.append(args[-1])
    for directory in directories:
        if directory:
            try:
                cwd = _path(directory, cwd)
            except Exception as error:
                cwd = Unknown("target", str(error))
    targets: list[Path] = []
    unknowns: list[Unknown] = []
    git_directory: Path | Unknown | DiscoverGitDirectory = DiscoverGitDirectory.FROM_CWD
    if "GIT_DIR" in overrides:
        try:
            git_directory = _path(overrides.pop("GIT_DIR"), cwd)
        except Exception as error:
            git_directory = Unknown("target", str(error))
    for value in (*overrides.values(), *extra_paths):
        try:
            targets.append(_path(value, cwd))
        except Exception as error:
            unknowns.append(Unknown("target", str(error)))
    if use_cwd:
        if isinstance(cwd, Path):
            targets.append(cwd)
        else:
            unknowns.append(cwd)
    if targets:
        yield GitChange(command, tuple(dict.fromkeys(targets)))
    if effects or not isinstance(git_directory, DiscoverGitDirectory):
        yield GitRepositoryWrite(command, cwd, git_directory, effects)
    yield from unknowns


def _expansions(word: shell.Word) -> Iterable[shell.Program]:
    for segment in word.segments:
        if isinstance(segment, shell.Execution):
            yield segment.program
        elif isinstance(segment, shell.Parameter):
            yield from segment.programs
        elif isinstance(segment, shell.ArrayElements):
            for element in segment.words:
                yield from _expansions(element)


@dataclass
class ShellScope:
    cwd: Path | Unknown
    variables: dict[str, str]
    directories: tuple[Path | Unknown, ...] | Unknown = ()

    def copy(self) -> ShellScope:
        return ShellScope(self.cwd, dict(self.variables), self.directories)


@dataclass(frozen=True)
class WrapperOptions:
    flags: frozenset[str]
    values: frozenset[str]
    optional_values: frozenset[str]


def _options(flags: str = "", values: str = "", optional: str = "") -> WrapperOptions:
    return WrapperOptions(frozenset(flags.split()), frozenset(values.split()), frozenset(optional.split()))


WRAPPERS = {
    "env": _options("- -i --ignore-environment -0 --null -v --debug",
                    "-C --chdir -u --unset -S --split-string --argv0 -a",
                    "--block-signal --default-signal --ignore-signal"),
    "timeout": _options("--foreground --preserve-status -v --verbose", "-k --kill-after -s --signal"),
    "nice": _options(values="-n --adjustment"),
    "nohup": _options(),
    "time": _options("-p --portability -a --append -v --verbose -q --quiet", "-f --format -o --output"),
    "stdbuf": _options(values="-i --input -o --output -e --error"),
    "ionice": _options("-t --ignore", "-c --class -n --classdata -p --pid -P --pgid -u --uid"),
    "xargs": _options("-0 --null -o --open-tty -p --interactive -r --no-run-if-empty -t --verbose -x --exit",
                      "-a --arg-file -d --delimiter -I --replace -L --max-lines -n --max-args -P --max-procs -s --max-chars",
                      "-e -E --eof -i -l"),
    "command": _options("-p -v -V"),
    "builtin": _options(),
    "exec": _options("-c -l", "-a"),
    "sudo": _options("-A -b -E -H -K -k -n -P -S -i -s --login --shell --non-interactive",
                     "-u --user -g --group -C --close-from -D --chdir -h --host -p --prompt -R --chroot "
                     + "-r --role -t --type -T --command-timeout", "--preserve-env"),
}


def _change_directory(scope: ShellScope, value: str) -> None:
    try:
        scope.cwd = _path(value, scope.cwd)
        scope.variables["PWD"] = str(scope.cwd)
    except (OSError, RuntimeError, ValueError) as error:
        scope.cwd = Unknown("target", str(error))
        _ = scope.variables.pop("PWD", None)


def _wrapper_arguments(name: str, words: list[str], scope: ShellScope) -> list[str]:
    options = WRAPPERS[name]
    while words and words[0].startswith("-"):
        option, equals, value = words[0].partition("=")
        words = words[1:]
        if option == "--":
            break
        if name == "nice" and re.fullmatch(r"-[0-9]+", option):
            continue
        if not option.startswith("--") and len(option) > 2:
            attached = option[2:]
            option = option[:2]
            if option in options.values or option in options.optional_values:
                value = attached + (equals + value if equals else "")
                equals = "="
            elif option in options.flags and not equals:
                words.insert(0, f"-{attached}")
        if option in options.values and not equals:
            if not words:
                raise TargetError(f"{name} has no value for {option}")
            value, *words = words
        elif option not in options.values and option not in options.optional_values and (
            option not in options.flags or equals
        ):
            raise TargetError(f"unknown {name} option {option}")
        if name == "command" and option in {"-v", "-V"}:
            return []
        if name == "env":
            if option in {"-C", "--chdir"}:
                _change_directory(scope, value)
            elif option in {"-", "-i", "--ignore-environment"}:
                scope.variables.clear()
            elif option in {"-u", "--unset"}:
                _ = scope.variables.pop(value, None)
            elif option in {"-S", "--split-string"}:
                words = shlex.split(value) + words
        if name == "exec" and option == "-c":
            scope.variables.clear()
        if name == "sudo" and option in {"-D", "--chdir"}:
            _change_directory(scope, value)
        if name == "sudo" and option in {"-i", "--login", "-R", "--chroot"}:
            scope.cwd = Unknown("target", f"{name} {option} changes the command directory")
            _ = scope.variables.pop("PWD", None)
    return words[1:] if name == "timeout" else words


def _canonical_program(program: str, scope: ShellScope) -> str:
    if shell.UNKNOWN_VALUE in program:
        raise TargetError("the command depends on a shell expansion")
    invoked = Path(program).name
    if "/" not in program:
        if program in SHELL_BUILTINS or program == "time":
            return program
        if found := shutil.which(program, path=scope.variables.get("PATH")):
            resolved = Path(found).resolve().name
        elif isinstance(scope.cwd, Unknown):
            return program
        else:
            resolved = _path(program, scope.cwd).name
    else:
        resolved = _path(program, scope.cwd).name
    # Multicall programs use the invocation name to select the wrapper.
    return invoked if invoked in WRAPPERS and invoked not in SHELL_BUILTINS and resolved not in PUBLICATION else resolved


def _publication_script(arguments: list[str], cwd: Path | Unknown) -> str:
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "-m":
            return arguments[index + 1].rsplit(".", 1)[-1] + ".py" if index + 1 < len(arguments) else ""
        if word == "-c" or word == "-":
            return ""
        if word == "--":
            index += 1
            break
        if not word.startswith("-"):
            break
        index += 2 if word in {"-W", "-X", "--check-hash-based-pycs"} else 1
    return _path(arguments[index], cwd).name if index < len(arguments) else ""


def _set_directory(scope: ShellScope, cwd: Path | Unknown) -> None:
    if isinstance(scope.cwd, Path):
        scope.variables["OLDPWD"] = scope.variables.get("PWD", str(scope.cwd))
    else:
        _ = scope.variables.pop("OLDPWD", None)
    scope.cwd = cwd
    if isinstance(cwd, Path):
        scope.variables["PWD"] = str(cwd)
    else:
        _ = scope.variables.pop("PWD", None)


def _builtin_cd(args: list[str], scope: ShellScope) -> None:
    index = 0
    physical = False
    while index < len(args) and re.fullmatch(r"-[LPe]+", args[index]):
        for flag in args[index][1:]:
            if flag in "LP":
                physical = flag == "P"
        index += 1
    end_of_options = args[index:index + 1] == ["--"]
    if end_of_options:
        index += 1
    values = args[index:]
    if len(values) > 1:
        return
    value = values[0] if values else scope.variables.get("HOME", str(Path.home()))
    if value == "-":
        value = scope.variables.get("OLDPWD", "")
        if not value:
            return
    elif not end_of_options and value.startswith("-"):
        _set_directory(scope, Unknown("target", f"unsupported cd option {value}"))
        return
    try:
        for name in _cd_destinations(scope, value):
            if shell.UNKNOWN_VALUE in name:
                raise TargetError("the directory depends on a shell expansion")
            logical = Path(name).expanduser()
            working_directory = scope.variables.get("PWD")
            if not physical and not logical.is_absolute() and working_directory:
                logical = Path(working_directory) / logical
            if not physical and logical.is_absolute():
                logical = Path(os.path.normpath(logical))
            directory = _path(str(logical), scope.cwd)
            if directory.is_dir():
                _set_directory(scope, directory)
                if not physical and logical.is_absolute():
                    scope.variables["PWD"] = str(logical)
                return
    except Exception as error:
        _set_directory(scope, Unknown("target", str(error)))


def _cd_destinations(scope: ShellScope, value: str) -> Iterable[str]:
    cdpath = scope.variables.get("CDPATH")
    if cdpath is not None and not value.startswith(("/", "~", "./", "../")) and value not in {".", ".."}:
        for folder in cdpath.split(":"):
            yield str(Path(folder) / value)
    yield value


def _directory_builtin(name: str, args: list[str], scope: ShellScope) -> None:
    if name == "cd":
        _builtin_cd(args, scope)
        return
    stack_only = args[:1] == ["-n"]
    values = args[1:] if stack_only else args
    if values[:1] == ["--"]:
        values = values[1:]
    if len(values) > 1 or (name == "pushd" and stack_only and not values):
        return
    value = values[0] if values else ""
    position = re.fullmatch(r"([+-])([0-9]+)", value)
    if (name == "popd" and value and not position) or (value.startswith("-") and not position):
        reason = Unknown("target", f"unsupported {name} argument")
        scope.directories = reason
        if not stack_only:
            _set_directory(scope, reason)
        return
    if name == "pushd" and value and position is None:
        try:
            if stack_only:
                if "\0" in value or shell.UNKNOWN_VALUE in value:
                    raise TargetError("the stack entry depends on a shell expansion")
                folder = Path(value).expanduser()
            else:
                folder = _path(value, scope.cwd)
        except Exception as error:
            reason = Unknown("target", str(error))
            scope.directories = reason
            if not stack_only:
                _set_directory(scope, reason)
            return
        if isinstance(scope.directories, tuple) and (stack_only or folder.is_dir()):
            previous = folder if stack_only else scope.cwd
            scope.directories = (previous, *scope.directories)
        if not stack_only and folder.is_dir():
            _set_directory(scope, folder)
        return
    if isinstance(scope.directories, Unknown):
        if not stack_only and (name == "pushd" or value in {"", "+0"}):
            _set_directory(scope, scope.directories)
        return
    if not scope.directories:
        return
    stack = [scope.cwd, *scope.directories]
    index = 0 if name == "popd" else 1
    if position:
        number = int(position[2])
        index = number if position[1] == "+" else len(stack) - 1 - number
    if not 0 <= index < len(stack):
        return
    if name == "popd":
        del stack[1 if stack_only and index == 0 else index]
    elif index:
        stack = stack[index:] + stack[:index]
    changes_directory = index == 0 if name == "popd" else index != 0
    if not stack_only and changes_directory:
        directory = stack[0]
        if isinstance(directory, Path):
            try:
                directory = _path(str(directory), scope.cwd)
                if not directory.is_dir():
                    return
            except Exception as error:
                directory = Unknown("target", str(error))
        _set_directory(scope, directory)
    scope.directories = tuple(stack[1:])


def _variable_builtin(name: str, args: list[str], scope: ShellScope) -> None:
    index = 0
    flags: set[str] = set()
    while index < len(args) and args[index].startswith(("-", "+")):
        option = args[index]
        index += 1
        if option == "--":
            break
        flags.update(option[1:])
    if "f" in flags or "p" in flags or (name in {"declare", "typeset"} and "F" in flags):
        return
    unknown_attributes = name in {"declare", "typeset"} and bool(flags.intersection("aAiIlnu"))
    for argument in args[index:]:
        variable, equals, value = argument.partition("=")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", variable):
            continue
        if name == "unset":
            _ = scope.variables.pop(variable, None)
        elif unknown_attributes:
            scope.variables[variable] = shell.UNKNOWN_VALUE
        elif equals:
            scope.variables[variable] = value


def _merge_shell_scopes(left: ShellScope, right: ShellScope) -> ShellScope:
    unknown = Unknown("target", "the shell has multiple possible directories")
    cwd = left.cwd if left.cwd == right.cwd else unknown
    if left.directories == right.directories:
        directories = left.directories
    elif isinstance(left.directories, tuple) and isinstance(right.directories, tuple) and (
        len(left.directories) == len(right.directories)
    ):
        directories = tuple(a if a == b else unknown for a, b in zip(left.directories, right.directories, strict=True))
    else:
        directories = unknown
    variables = {
        name: left.variables.get(name, shell.UNKNOWN_VALUE)
        if left.variables.get(name) == right.variables.get(name) else shell.UNKNOWN_VALUE
        for name in left.variables.keys() | right.variables.keys()
    }
    return ShellScope(cwd, variables, directories)


@final
class ShellTargets:
    """Collect direct operations without running the command."""

    def __init__(self, cwd: Path | Unknown) -> None:
        self.scope = ShellScope(cwd, {"HOME": str(Path.home()), **{
            name: os.environ[name] for name in TARGET_ENV | {"OLDPWD", "CDPATH"} if name in os.environ
        }})
        if isinstance(cwd, Path):
            self.scope.variables["PWD"] = str(cwd)
        self.operations: list[TargetCandidate] = []
        self.budget = shell.ShellBudget()

    def _word(self, word: shell.Word, variables: dict[str, str]) -> str:
        pieces: list[str] = []
        for segment in word.segments:
            if isinstance(segment, shell.Literal):
                piece = segment.text
            elif isinstance(segment, shell.Parameter):
                piece = variables.get(segment.name, shell.UNKNOWN_VALUE)
            else:
                piece = shell.UNKNOWN_VALUE
            self.budget.charge(len(piece) + 1)
            pieces.append(piece)
        return "".join(pieces)

    def _nested(self, word: shell.Word, scope: ShellScope) -> None:
        for program in _expansions(word):
            _ = self._commands(program.commands, [scope.copy()])

    def _commands(self, commands: Iterable[shell.SimpleCommand | shell.Compound],
                  scopes: list[ShellScope]) -> list[ShellScope]:
        for command in commands:
            next_scopes: dict[
                tuple[Path | Unknown, tuple[Path | Unknown, ...] | Unknown, tuple[tuple[str, str], ...]], ShellScope
            ] = {}
            merged: ShellScope | None = None
            for current in scopes:
                for branch in self._command_scopes(command, current):
                    self.budget.charge(len(branch.variables) + 1)
                    if merged is not None:
                        merged = _merge_shell_scopes(merged, branch)
                        continue
                    key = (branch.cwd, branch.directories, tuple(sorted(branch.variables.items())))
                    next_scopes[key] = branch
                    if len(next_scopes) > MAX_SHELL_TARGET_SCOPES:
                        states = iter(next_scopes.values())
                        combined = next(states)
                        for state in states:
                            combined = _merge_shell_scopes(combined, state)
                        merged = combined
                        next_scopes.clear()
            scopes = [merged] if merged is not None else list(next_scopes.values())
        return scopes

    def _command_scopes(self, command: shell.SimpleCommand | shell.Compound,
                        scope: ShellScope) -> Iterable[ShellScope]:
        with self.budget.nested():
            yield from self._command_effects(command, scope)

    def _command_effects(self, command: shell.SimpleCommand | shell.Compound,
                         scope: ShellScope) -> Iterable[ShellScope]:
        self.budget.charge(len(scope.variables) + 1)
        if isinstance(command, shell.SimpleCommand):
            self._command(command, scope)
            yield scope
            return
        for word in command.headers:
            self._nested(word, scope)
        if command.kind in {"group", "branch", "case", "prefix"}:
            yield from self._commands(command.body.commands, [scope])
        elif command.kind == "alternatives":
            for branch in command.body.commands:
                yield from self._command_scopes(branch, scope.copy())
        elif command.kind == "pipeline":
            for stage in command.body.commands:
                yield_scopes = self._command_scopes(stage, scope.copy())
                for stage_scope in yield_scopes:
                    self.budget.charge(len(stage_scope.variables) + 1)
            yield scope
        else:
            branches = self._commands(command.body.commands, [scope.copy()])
            if command.kind in {"loop", "for"}:
                yield from branches
            yield scope

    def _command(self, command: shell.SimpleCommand, scope: ShellScope) -> None:
        for word in command.words:
            self._nested(word, scope)
        for redirect in command.redirects:
            self._nested(redirect.target, scope)
            if isinstance(redirect.document, shell.HereDocument):
                for program in redirect.document.expansions:
                    _ = self._commands(program.commands, [scope.copy()])
        words = [self._word(word, scope.variables) for word in command.words]
        local = scope.copy()
        wrapped = False
        destination = scope
        while words:
            self.budget.charge(1 + len(words))
            program, *arguments = words
            if ASSIGNMENT.match(program):
                variable, _, value = program.partition("=")
                local.variables[variable] = value
                words = arguments
                continue
            try:
                name = _canonical_program(program, local)
                if name in WRAPPERS:
                    wrapped = True
                    if name not in {"command", "builtin"} or "/" in program:
                        destination = local
                    words = _wrapper_arguments(name, arguments, local)
                    if name == "builtin" and words and words[0] not in SHELL_BUILTINS:
                        if shell.UNKNOWN_VALUE in words[0]:
                            raise TargetError("the builtin name depends on a shell expansion")
                        return
                    continue
                self._inspect(name, arguments, local, local if "/" in program else destination)
            except shell.ShellWorkLimit:
                raise
            except Exception as error:
                self.operations.append(Unknown("target", str(error)))
            return
        if not wrapped:
            scope.variables.update(local.variables)

    def _inspect(self, name: str, args: list[str], local: ShellScope, scope: ShellScope) -> None:
        if name in {"cd", "pushd", "popd"}:
            _directory_builtin(name, args, scope)
        elif name == "dirs":
            if any(re.fullmatch(r"-[clpv]*c[clpv]*", arg) for arg in args):
                scope.directories = ()
        elif name in {"export", "declare", "typeset", "unset"}:
            _variable_builtin(name, args, scope)
        elif name == "git":
            self.operations.extend(_git_operation(args, local.cwd, local.variables))
        elif name in PUBLICATION:
            self.operations.append(DirectPublication(name))
        elif re.fullmatch(r"python(?:[23][0-9.]*)?|py", name):
            script = _publication_script(args, local.cwd)
            if script in PUBLICATION:
                self.operations.append(DirectPublication(script))
        elif name in {"bash", "sh", "zsh", "dash"}:
            for index, arg in enumerate(args[:-1]):
                if re.fullmatch(r"-[A-Za-z]*c", arg):
                    self._scan(args[index + 1], local)
                    break

    def _scan(self, command: str, scope: ShellScope) -> None:
        if command == shell.UNKNOWN_VALUE:
            self.operations.append(Unknown("target", "the shell script depends on an unresolved expansion"))
            return
        scopes = [scope]

        def consume(parsed: shell.SimpleCommand | shell.Compound) -> None:
            nonlocal scopes
            scopes = self._commands((parsed,), scopes)

        try:
            with self.budget.nested():
                shell.read_commands(command, self.budget, consume)
        except shell.ShellWorkLimit:
            raise
        except Exception as error:
            self.operations.append(Unknown("target", str(error)))

    def scan(self, command: str) -> tuple[TargetCandidate, ...]:
        try:
            self._scan(command, self.scope)
        except shell.ShellWorkLimit as error:
            self.operations.append(Unknown("target", str(error)))
        return tuple(self.operations)


def _operations(tool: str, payload: dict[str, object], cwd: Path | Unknown) -> tuple[TargetCandidate, ...]:
    if tool == "Agent":
        return (AgentLaunch(),)
    inputs = _object(payload.get("tool_input"))
    if tool == "Bash":
        if isinstance(workdir := inputs.get("workdir"), str):
            try:
                cwd = _path(workdir, cwd)
            except (OSError, RuntimeError, ValueError) as error:
                cwd = Unknown("target", str(error))
        command = inputs.get("command")
        vector = _items(command)
        if vector and all(isinstance(word, str) for word in vector):
            command = shlex.join(cast(list[str], vector))
        if not isinstance(command, str) or (len(command) <= shell.MAX_SHELL_WORK and not command.strip()):
            return (Unknown("target", "Bash has no command"),)
        return ShellTargets(cwd).scan(command)
    writes: list[Operation | Unknown] = []
    try:
        for value in _file_values(tool, inputs):
            if isinstance(value, Unknown):
                writes.append(value)
            else:
                writes.extend(_file_writes(value, cwd))
    except Exception as error:
        writes.append(Unknown("target", str(error)))
    return tuple(writes)


@dataclass(frozen=True)
class OutsideProtectedCheckouts:
    """The Git write does not change a protected checkout."""


def _read_git_fact(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8", errors="surrogateescape") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise TargetError(f"{path}: expected a regular file")
        return source.read()


def _worktree_config_enabled(common_dir: Path) -> bool:
    try:
        config = _read_git_fact(common_dir / "config")
    except FileNotFoundError:
        return False
    value = "false"
    in_extensions = False
    for line in config.replace("\\\n", "").splitlines():
        line = line.strip().lower()
        if line.startswith("["):
            in_extensions = bool(re.fullmatch(r"\[extensions\]\s*(?:[#;].*)?", line))
        elif in_extensions:
            match = re.fullmatch(r'worktreeconfig\s*(?:=\s*("[^"]*"|[^#;]*))?\s*(?:[#;].*)?', line)
            if match:
                value = "true" if match[1] is None else match[1].strip().strip('"')
    if value in {"true", "yes", "on"}:
        return True
    number = re.fullmatch(r"([+-]?[0-9]+)[kmg]?", value)
    return number is not None and int(number[1]) != 0


def _linked_checkout(directory: Path, common: Path) -> git_files.Checkout | OutsideProtectedCheckouts:
    if directory.parent != common / "worktrees":
        return OutsideProtectedCheckouts()
    try:
        entry = Path(_read_git_fact(directory / "gitdir").strip())
        if entry.name != ".git":
            return OutsideProtectedCheckouts()
        checkout = git_files.at((directory / entry).resolve().parent)
    except git_files.NotACheckout:
        return OutsideProtectedCheckouts()
    if (checkout.git_dir, checkout.common_dir) == (directory, common):
        return checkout
    return OutsideProtectedCheckouts()


def _git_write_checkout(
    candidate: GitRepositoryWrite, checkouts: list[git_files.Checkout], roots: set[Path],
) -> git_files.Checkout | Path | Unknown | OutsideProtectedCheckouts:
    directory = candidate.git_directory
    if isinstance(directory, Unknown):
        return directory
    if isinstance(directory, Path):
        if directory.is_file():
            if any(directory.is_relative_to(root) for root in roots):
                return directory
            entry = _read_git_fact(directory).strip()
            location = entry.removeprefix("gitdir:").strip()
            if not entry.startswith("gitdir:") or not location:
                return OutsideProtectedCheckouts()
            directory = _path(location, directory.parent)
        checkout = OutsideProtectedCheckouts()
        for protected in checkouts:
            if directory == protected.git_dir:
                return directory
            if directory.is_relative_to(protected.common_dir):
                try:
                    linked = _linked_checkout(directory, protected.common_dir)
                except Exception:
                    return directory
                if isinstance(linked, OutsideProtectedCheckouts):
                    return directory
                checkout = linked
                break
        checkout_path = checkout.toplevel if isinstance(checkout, git_files.Checkout) else directory
        if any(checkout_path.is_relative_to(root) for root in roots):
            return directory
        if isinstance(checkout, OutsideProtectedCheckouts):
            return checkout
    else:
        cwd = candidate.checkout_directory
        if isinstance(cwd, Unknown):
            return cwd
        try:
            checkout = git_files.locate(cwd)
        except git_files.NotACheckout:
            return OutsideProtectedCheckouts()
    return checkout


def _shared_effect_target(
    effect: GitEffect, checkout: git_files.Checkout, protected: git_files.Checkout, roots: set[Path],
) -> Path | OutsideProtectedCheckouts:
    common = protected.common_dir
    # An ordinary linked checkout can append Git objects without changing shared refs or config.
    if effect is RepositoryStorageWrite.SHARED_STATE or effect is RepositoryConfigWrite.LOCAL:
        return common
    if effect is RepositoryConfigWrite.WORKTREE:
        if not _worktree_config_enabled(common):
            return common
        local = checkout.git_dir / "config.worktree"
        destination = local.resolve()
        if destination != local and any(
            destination.is_relative_to(root) for root in (*roots, protected.git_dir, common)
        ):
            return destination
    if isinstance(effect, SharedRefWrite):
        if effect.refs_from_input or any(
            ref.startswith("refs/") and not ref.startswith(("refs/heads/", "refs/bisect/", "refs/worktree/", "refs/rewritten/"))
            for ref in effect.refs
        ):
            return common
        head = git_files.head(protected)
        if isinstance(head, git_files.OnBranch):
            ref = f"refs/heads/{head.name}"
            if any(fnmatchcase(ref, target if target.startswith("refs/") else f"refs/heads/{target}")
                   for target in effect.refs):
                return common
            if effect.current_branch and git_files.head(checkout) == head:
                return common
    return OutsideProtectedCheckouts()


def _protected_shared_write(
    candidate: GitRepositoryWrite, checkouts: list[git_files.Checkout], roots: set[Path],
) -> Path | Unknown | OutsideProtectedCheckouts:
    checkout = _git_write_checkout(candidate, checkouts, roots)
    if not isinstance(checkout, git_files.Checkout):
        return checkout
    unresolved: Unknown | OutsideProtectedCheckouts = OutsideProtectedCheckouts()
    for protected in checkouts:
        if protected.common_dir != checkout.common_dir:
            continue
        for effect in candidate.effects:
            try:
                target = _shared_effect_target(effect, checkout, protected, roots)
            except Exception as error:
                unresolved = Unknown("target", str(error))
                continue
            if isinstance(target, Path):
                return target
    return unresolved


def _scope(
    enrollment: Enrolled | UnusableEnrollment, candidates: tuple[TargetCandidate, ...],
    resolver: Resolver, cwd: Path | Unknown,
) -> tuple[tuple[Operation, ...], tuple[Unknown, ...]]:
    operations: list[Operation] = []
    unknowns: list[Unknown] = []
    roots: set[Path] = set()
    git_roots: set[Path] = set()
    if any(isinstance(candidate, (FileWrite, GitChange, GitRepositoryWrite)) for candidate in candidates):
        try:
            roots, unread = _protected_roots(enrollment, resolver)
            unknowns.extend(unread)
        except Exception as error:
            unknowns.append(Unknown("target", str(error)))
    checkouts: list[git_files.Checkout] = []
    if any(isinstance(candidate, (GitChange, GitRepositoryWrite)) for candidate in candidates):
        for root in roots:
            try:
                checkout = git_files.at(root)
                checkouts.append(checkout)
                git_roots.update((checkout.git_dir, checkout.common_dir))
            except git_files.NotACheckout:
                continue
            except Exception as error:
                unknowns.append(Unknown("target", str(error)))
    protected_git_paths = roots | git_roots
    for candidate in candidates:
        match candidate:
            case Unknown():
                unknowns.append(candidate)
            case FileWrite(path):
                try:
                    protected = any(path.is_relative_to(root) for root in roots)
                except Exception as error:
                    unknowns.append(Unknown("target", str(error)))
                    continue
                if protected:
                    try:
                        permitted = isinstance(enrollment, Enrolled) and _runtime_file(path, enrollment, cwd)
                    except Exception:
                        permitted = False
                    operations.append(FileWrite(path, permitted))
            case GitChange(command, paths):
                known: list[Path] = []
                for path in paths:
                    try:
                        if any(path.is_relative_to(root) for root in protected_git_paths):
                            known.append(path)
                    except Exception as error:
                        unknowns.append(Unknown("target", str(error)))
                if known:
                    operations.append(GitChange(command, tuple(known)))
            case GitRepositoryWrite():
                try:
                    target = _protected_shared_write(candidate, checkouts, roots)
                except Exception as error:
                    unknowns.append(Unknown("target", str(error)))
                    continue
                if isinstance(target, Path):
                    operations.append(GitChange(candidate.command, (target,)))
                elif isinstance(target, Unknown):
                    unknowns.append(target)
            case _:
                operations.append(candidate)
    return tuple(operations), tuple(unknowns)


def _bounded_policy(
    policy: Policy, enrolled: Enrolled, operations: tuple[Operation, ...], budget: float | ConfiguredBudget,
) -> Allow | Deny:
    result: Future[Decision] = Future()

    def run() -> None:
        try:
            result.set_result(policy(enrolled, operations))
        except BaseException as error:
            result.set_result(Deny(f"The showrunner guard policy failed for a protected call. {error}"))

    try:
        timeout = policy_timeout() if isinstance(budget, ConfiguredBudget) else budget
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid policy timeout")
        Thread(target=run, daemon=True).start()
        decision = result.result(timeout=timeout)
    except TimeoutError:
        return Deny("The showrunner guard policy timed out for a protected call.")
    except (Exception, SystemExit) as error:
        return Deny(f"The showrunner guard policy failed for a protected call. {error}")
    if isinstance(decision, Allow):
        return decision
    if isinstance(decision, Deny) and isinstance(cast(object, decision.reason), str) and decision.reason.strip():
        return decision
    return Deny("The showrunner guard policy returned an invalid decision for a protected call.")


def evaluate(
    raw: str, *,
    resolver: Resolver = resolve_production_paths,
    policy: Policy = decide,
    policy_timeout_seconds: float | ConfiguredBudget = ConfiguredBudget.READ_CONFIG,
    tools: frozenset[str] = CLAUDE_TOOLS | CODEX_TOOLS,
) -> Decision:
    try:
        payload = _object(cast(object, json.loads(raw)))
    except (ValueError, RecursionError) as error:
        return Unknown("identity", str(error))
    tool = payload.get("tool_name")
    if not isinstance(tool, str):
        return Unknown("target", "the payload has no tool name")
    if tool not in tools:
        return Allow()
    try:
        enrollment = _enrollment(payload, resolver)
    except Exception as error:
        return Unknown("identity", str(error))
    if isinstance(enrollment, NotEnrolled):
        return Allow()
    if isinstance(enrollment, Unknown):
        return enrollment
    cwd: Path | Unknown = Unknown("target", "Agent does not need a working directory")
    if tool != "Agent":
        try:
            cwd = _cwd(payload)
        except Exception as error:
            cwd = Unknown("target", str(error))
    try:
        operations, unknowns = _scope(enrollment, _operations(tool, payload, cwd), resolver, cwd)
    except Exception as error:
        reason = enrollment.reason if isinstance(enrollment, UnusableEnrollment) else str(error)
        return Unknown("target", reason)
    if isinstance(enrollment, UnusableEnrollment):
        if operations:
            return Deny(f"{_refusal(operations[0]).reason}\n{enrollment.reason}")
        return Unknown("target", enrollment.reason)
    if operations:
        decision = _bounded_policy(policy, enrollment, operations, policy_timeout_seconds)
        if isinstance(decision, Deny):
            return decision
    return unknowns[0] if unknowns else Allow()


def claude_front(
    raw: str, *,
    resolver: Resolver = resolve_production_paths,
    policy: Policy = decide,
    policy_timeout_seconds: float | ConfiguredBudget = ConfiguredBudget.READ_CONFIG,
) -> Decision:
    return evaluate(raw, resolver=resolver, policy=policy, policy_timeout_seconds=policy_timeout_seconds,
                    tools=CLAUDE_TOOLS)


def codex_front(
    raw: str, *,
    resolver: Resolver = resolve_production_paths,
    policy: Policy = decide,
    policy_timeout_seconds: float | ConfiguredBudget = ConfiguredBudget.READ_CONFIG,
) -> Decision:
    return evaluate(raw, resolver=resolver, policy=policy, policy_timeout_seconds=policy_timeout_seconds,
                    tools=CODEX_TOOLS)


class HookDecision(TypedDict):
    hookEventName: Literal["PreToolUse"]
    permissionDecision: Literal["deny"]
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


def emit(decision: Decision) -> None:
    if isinstance(decision, Unknown):
        reason = " ".join(decision.reason.splitlines())
        print(f"showrunner guard: unknown {decision.scope}: {reason}", file=sys.stderr)
    elif isinstance(decision, Deny):
        response: HookResponse = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": decision.reason}}
        print(json.dumps(response))


class ClaudeRegistration(TypedDict):
    settings: str
    event: Literal["PreToolUse"]
    matcher: str
    command: str


class CodexRegistration(TypedDict):
    codex_home: str
    event: Literal["PreToolUse"]
    tool: str
    command: str


class GuardReadiness(TypedDict):
    version: Literal[1]
    revision: str
    recorded_at: str
    claude: list[ClaudeRegistration]
    codex: list[CodexRegistration]


@dataclass(frozen=True)
class RegisteredGuard:
    matcher: str
    command: str
    timeout: object


def _registrations(path: Path, checkout: Path, script: str, arguments: tuple[str, ...] = ()) -> Iterable[RegisteredGuard]:
    settings = _read_object(path)
    if settings.get("disableAllHooks") is True:
        return
    commands = _object(settings.get("hooks"))
    launcher = checkout / "scripts/lib/py"
    front = checkout / "scripts/hooks" / script
    for value in _items(commands.get("PreToolUse")):
        group = _object(value)
        matcher = group.get("matcher", "*")
        if not isinstance(matcher, str):
            continue
        for value in _items(group.get("hooks")):
            handler = _object(value)
            command = handler.get("command")
            if (handler.get("type") != "command" or not isinstance(command, str)
                    or handler.get("async") is True or handler.get("asyncRewake") is True):
                continue
            try:
                words = shlex.split(command)
            except ValueError:
                continue
            if len(words) != 2 + len(arguments) or tuple(words[2:]) != arguments:
                continue
            paths = [Path(os.path.expandvars(word)).expanduser() for word in words[:2]]
            if all(path.is_absolute() for path in paths) and [path.resolve() for path in paths] == [
                launcher.resolve(), front.resolve(),
            ]:
                yield RegisteredGuard(matcher, command, handler.get("timeout"))


def _matched_tools(matcher: str, tools: frozenset[str]) -> set[str]:
    if matcher in ("", "*"):
        return set(tools)
    try:
        return {tool for tool in tools if re.fullmatch(matcher, tool)}
    except re.error:
        return set()


def _longer_timeout(timeout: object, policy_seconds: float) -> bool:
    return (isinstance(timeout, (float, int)) and not isinstance(timeout, bool)
            and math.isfinite(timeout) and timeout > policy_seconds)


def _claude_registrations(
    settings: Path, checkout: Path, policy_seconds: float, problems: list[str],
) -> list[ClaudeRegistration]:
    covered: set[str] = set()
    entries: list[ClaudeRegistration] = []
    for registration in _registrations(settings, checkout, "hook_dispatch.py", ("pre_tool_use",)):
        tools = _matched_tools(registration.matcher, CLAUDE_TOOLS)
        if not tools:
            continue
        covered.update(tools)
        if not _longer_timeout(registration.timeout, policy_seconds):
            problems.append(f"{settings}: Claude guard timeout must exceed {policy_seconds:g} seconds "
                            + f"for {', '.join(sorted(tools))}")
            continue
        entries.append({"settings": str(settings), "event": "PreToolUse", "matcher": registration.matcher,
                        "command": registration.command})
    if missing := CLAUDE_TOOLS - covered:
        problems.append(f"{settings}: missing Claude guard for {', '.join(sorted(missing))}")
    return entries


def _codex_registrations(
    home: Path, checkout: Path, policy_seconds: float, problems: list[str],
) -> list[CodexRegistration]:
    path = home / "hooks.json"
    covered: set[str] = set()
    registrations: list[tuple[str, str, int]] = []
    entries: list[CodexRegistration] = []
    for registration in _registrations(path, checkout, "pre-tool-use-showrunner-guard-codex.py"):
        tools = _matched_tools(registration.matcher, CODEX_TOOLS)
        if not tools:
            continue
        covered.update(tools)
        timeout = registration.timeout
        if not isinstance(timeout, int) or not _longer_timeout(timeout, policy_seconds):
            problems.append(f"{path}: Codex guard timeout must exceed {policy_seconds:g} seconds "
                            + f"for {', '.join(sorted(tools))}")
            continue
        registrations.append((registration.matcher, registration.command, timeout))
        entries.extend({"codex_home": str(home), "event": "PreToolUse", "tool": tool, "command": registration.command}
                       for tool in sorted(tools))
    if missing := CODEX_TOOLS - covered:
        problems.append(f"{path}: missing Codex guard for {', '.join(sorted(missing))}")
    if registrations:
        installer = vars(import_module("scripts.hooks.codex_hooks"))
        check = cast(Callable[[Path, Sequence[tuple[str, str, int]]], list[str]], installer["check_showrunner_guard"])
        problems.extend(check(home, registrations))
    return entries


GUARD_FILES = (
    "scripts/hooks/showrunner_guard.py",
    "scripts/hooks/pre-tool-use-showrunner-guard-codex.py",
    "scripts/hooks/hook_dispatch.py",
    "config/showrunner_write_guard.conf",
)


def _main_revision(checkout: Path) -> str:
    revision = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "--verify", "refs/heads/main^{commit}"],
        env=_git_environment(), text=True, stderr=subprocess.PIPE, timeout=2,
    ).strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("main has no 40-character revision")
    return revision


def _check_guard_blobs(checkout: Path, revision: str, problems: list[str]) -> None:
    for file in GUARD_FILES:
        try:
            committed = subprocess.check_output(
                ["git", "-C", str(checkout), "cat-file", "blob", f"{revision}:{file}"],
                env=_git_environment(), stderr=subprocess.PIPE, timeout=2,
            )
            installed = (checkout / file).read_bytes()
        except (OSError, subprocess.SubprocessError) as error:
            problems.append(f"{file}: cannot verify the installed guard against main. {error}")
            continue
        if installed != committed:
            problems.append(f"{file}: installed guard differs from main {revision}")

def record_ready(
    checkout: Path, claude_settings: Sequence[Path], codex_homes: Sequence[Path], destination: Path,
) -> int:
    """Write the proof after every client provides the required registrations."""
    from scripts.lib.atomic_file import write_text_atomic

    checkout = checkout.resolve()
    try:
        policy_seconds = policy_timeout()
        revision = _main_revision(checkout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"showrunner guard: record-ready: {error}", file=sys.stderr)
        return 1
    claude: list[ClaudeRegistration] = []
    codex: list[CodexRegistration] = []
    problems: list[str] = []
    _check_guard_blobs(checkout, revision, problems)
    if not claude_settings or not codex_homes:
        problems.append("both Claude settings and Codex homes are required")
    for path in claude_settings:
        try:
            claude.extend(_claude_registrations(path.absolute(), checkout, policy_seconds, problems))
        except (OSError, RuntimeError, ValueError) as error:
            problems.append(str(error))
    for home in codex_homes:
        try:
            codex.extend(_codex_registrations(home.absolute(), checkout, policy_seconds, problems))
        except (OSError, RuntimeError, ValueError) as error:
            problems.append(str(error))
    if problems:
        for problem in problems:
            print(f"showrunner guard: record-ready: {problem}", file=sys.stderr)
        return 1
    try:
        record: GuardReadiness = {
            "version": 1, "revision": revision, "recorded_at": datetime.now(timezone.utc).isoformat(),
            "claude": claude, "codex": codex,
        }
        write_text_atomic(destination, json.dumps(record, indent=2) + "\n", new_file_mode=0o600)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"showrunner guard: record-ready: {error}", file=sys.stderr)
        return 1
    return 0


def _record_installed_guard() -> int:
    from scripts.lib.transcripts import config_dirs

    dispatcher = vars(import_module("scripts.hooks.hook_dispatch"))
    registrations = cast(tuple[object, ...], dispatcher["PRE_TOOL_USE"])
    if dispatcher["SHOWRUNNER_GUARD"] not in registrations:
        print("showrunner guard: record-ready: Claude dispatcher has no showrunner guard registration", file=sys.stderr)
        return 1
    home = Path.home()
    checkout = home / ".claude"
    if Path(__file__).resolve().parents[2] != checkout.resolve():
        print("showrunner guard: record-ready: run the installed guard from ~/.claude", file=sys.stderr)
        return 1
    settings = sorted({checkout / "settings.json", *(folder / "settings.json" for folder in config_dirs())})
    installer = vars(import_module("scripts.hooks.codex_hooks"))
    discover = cast(Callable[[], list[Path]], installer["known_codex_homes"])
    return record_ready(checkout, settings, discover(),
                        home / ".local/state/showrunner/guard-ready.json")


def main(
    *,
    resolver: Resolver = resolve_production_paths,
    policy: Policy = decide,
    policy_timeout_seconds: float | ConfiguredBudget = ConfiguredBudget.READ_CONFIG,
) -> int:
    if sys.argv[1:] == ["record-ready"]:
        return _record_installed_guard()
    emit(claude_front(sys.stdin.read(), resolver=resolver, policy=policy,
                      policy_timeout_seconds=policy_timeout_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
