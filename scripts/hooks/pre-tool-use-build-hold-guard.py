#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Refuse unguarded test starts and calls subject to the build hold."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import re
import shlex
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict, cast

from scripts.buildlog.parse import CARGO_BUILD_VERBS, cargo_subcommand
from scripts.hooks.tool_call_text import scan_text

from scripts.lib.raw_starts import SHELLS, is_python_command, is_test_start, python_target, shell_target
from scripts.lib.suite_runners import SUITE_RUNNERS, starts_suite_runner

GUARD = Path("scripts/lib/walled_tests.py")
SHIM = Path("scripts/lib/py")
RUNNER = Path("scripts/test_times/module_run.py")
GUARDED_RUNNERS = SUITE_RUNNERS | {"gate.py", RUNNER.name, "coverage_overlap.py"}
# Keep these markers in sync with the shell hook.
GUARD_CANDIDATE = re.compile(
    r"unittest|pytest|py[.]test|test_|proof|module_run|coverage_overlap|merge_gate|targeted_test|walled_tests"
    + r"|(?<![\w.-])gate[.]py"
)
CANDIDATE = re.compile(r"\b(?:" + "|".join(re.escape(name) for name in sorted({"cargo", *SUITE_RUNNERS})) + r")\b")
PREFIXES = frozenset({"env", "sudo", "exec", "command", "nohup", "time", "timeout", "nice",
                      "!", "{", "then", "else", "do", "if", "while", "until"})
PREFIX_ARGUMENTS = frozenset({"-u", "--unset", "--user", "-g", "--group", "-n", "--adjustment",
                             "-s", "--signal", "-k", "--kill-after"})
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=")
HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\2[^\n]*\n")
SHELL_HEREDOC = re.compile(r"\b(?:bash|sh|zsh)(?:[ \t]+[^<\n]*)?[ \t]*$")


class BuildHoldCommandInput(TypedDict, total=False):
    workdir: str
    command: str | list[str]
    cmd: str | list[str]


class BuildHoldHookPayload(TypedDict, total=False):
    cwd: str
    session_id: str
    tool_name: str
    tool_input: BuildHoldCommandInput


def without_literal_heredocs(command: str) -> str:
    """Keep written command examples out of the build check."""
    for found in reversed(list(HEREDOC.finditer(command))):
        end = re.search(rf"^{('[\t]*' if found.group(1) else '')}{found.group(3)}$",
                        command[found.end():], re.MULTILINE)
        if end:
            start, stop = found.end(), found.end() + end.end()
            body = command[start:stop]
            if SHELL_HEREDOC.search(command[:found.start()]):
                continue
            if not found.group(2) and ("$(" in body or "`" in body):
                continue
            command = command[:start] + "\n" * command[start:stop].count("\n") + command[stop:]
    return command


def without_prefixes(words: list[str]) -> list[str]:
    while words:
        if ASSIGNMENT.match(words[0]):
            words = words[1:]
            continue
        prefix = Path(words[0]).name
        if prefix not in PREFIXES:
            break
        words = words[1:]
        while words and words[0].startswith("-"):
            words = words[2:] if words[0] in PREFIX_ARGUMENTS else words[1:]
        if prefix == "timeout":
            words = words[1:]
    return words


def builds(words: list[str], *, suite_runner_only: bool = False) -> bool:
    """Check one command after its environment and command prefixes."""
    words = without_prefixes(words)
    if not words:
        return False
    program = Path(words[0]).name
    arguments = words[1:]
    if program in SHELLS:
        inline, child = shell_target(tuple(arguments))
        if inline:
            return bool(child) and building_command(child[0], suite_runner_only=suite_runner_only)
        return False
    if starts_suite_runner(words):
        return True
    if is_python_command(words[0]):
        return starts_suite_runner(python_target(tuple(arguments)))
    if suite_runner_only:
        return False
    if program != "cargo":
        return False
    return cargo_subcommand(arguments) in CARGO_BUILD_VERBS


def shell_words(command: str) -> Iterator[list[str]]:
    command = scan_text("Bash", {"command": command}).replace("\\\n", "")
    lexer = shlex.shlex(without_literal_heredocs(command), posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    words: list[str] = []
    for word in lexer:
        if word and all(char in ";&|()\n" for char in word):
            if words:
                yield words
            words = []
        else:
            words.append(word)
    if words:
        yield words


def building_command(command: str, *, suite_runner_only: bool = False) -> bool:
    return CANDIDATE.search(command) is not None and any(
        builds(words, suite_runner_only=suite_runner_only) for words in shell_words(command)
    )


def checkout_refusal(directory: Path) -> str:
    for root in (directory, *directory.parents):
        if (root / GUARD).is_file():
            break
        if (root / SHIM).is_file() and (root / RUNNER).is_file():
            return f"Test starts require {GUARD} in {root}."
    return ""


def test_refusal(words: list[str], directory: Path) -> str:
    words = without_prefixes(words)
    if not words:
        return ""
    program, *arguments = words
    name = Path(program).name
    shim = Path(program).parts[-3:] == SHIM.parts
    target = python_target(tuple(arguments)) if is_python_command(program) else tuple(words)
    if name in SHELLS:
        inline, child = shell_target(tuple(arguments))
        if inline:
            return guard_refusal(shell_words(child[0]), directory) if child else ""
        return test_refusal(list(child), directory)
    if not target or target[0] == "-c":
        return ""

    module = target[0] == "-m"
    if module and len(target) < 2:
        return ""
    entry = target[1] if module else target[0]
    basename = Path(entry).name
    if module and entry.startswith("scripts."):
        basename = entry.rsplit(".", 1)[-1] + ".py"
    launcher = Path(entry).parts[-3:] == GUARD.parts or entry == "scripts.lib.walled_tests"
    runner = basename in GUARDED_RUNNERS
    direct_test = is_test_start(words)
    if not (launcher or runner or direct_test):
        return ""
    if direct_test and not (shim or runner):
        return f"Start tests through {SHIM} or {GUARD}."

    paths = [directory]
    if not module and "/" in entry:
        paths.append((directory / Path(entry).expanduser()).resolve().parent)
    if shim:
        paths.append((directory / Path(program).expanduser()).resolve().parent)
    if basename in {RUNNER.name, "coverage_overlap.py"}:
        first_argument = 2 if module else 1
        if len(target) > first_argument:
            paths.append((directory / Path(target[first_argument]).expanduser()).resolve())
    if runner:
        for index, option in enumerate(target):
            if option.startswith("--repo="):
                paths.append((directory / Path(option.split("=", 1)[1]).expanduser()).resolve())
            elif option == "--repo" and index + 1 < len(target):
                paths.append((directory / Path(target[index + 1]).expanduser()).resolve())
    return next((reason for path in paths if (reason := checkout_refusal(path))), "")


def guard_refusal(commands: Iterator[list[str]], directory: Path) -> str:
    for words in commands:
        call = without_prefixes(words)
        if call[:1] == ["cd"] and len(call) == 2:
            directory = (directory / Path(call[1]).expanduser()).resolve()
            continue
        reason = test_refusal(words, directory)
        if reason:
            return reason
    return ""


def decide(raw: str) -> str:
    payload = cast(BuildHoldHookPayload, json.loads(raw))
    if payload.get("tool_name") != "Bash":
        return ""
    tool_input = payload.get("tool_input", {})
    command = tool_input.get("command") or tool_input.get("cmd") or ""
    if isinstance(command, list):
        command = shlex.join(command)
    command = command.replace("\\\n", "")
    if GUARD_CANDIDATE.search(command):
        cwd = Path(payload.get("cwd") or os.getcwd())
        directory = (cwd / tool_input.get("workdir", "")).resolve()
        reason = guard_refusal(shell_words(command), directory)
        if reason:
            return reason
    if not building_command(command):
        return ""
    directory = Path(os.environ.get("BUILD_HOLD_DIR", str(Path.home() / ".local/state/build-hold")))
    try:
        if not any(directory.iterdir()):
            return ""
    except FileNotFoundError:
        return ""
    # Load the hold reader only for a build call with a possible hold.
    from scripts.build_hold.hold_applies import HoldStateReadError, hold_applies

    if not os.environ.get("CLAUDE_CODE_SESSION_ID"):
        os.environ["CLAUDE_CODE_SESSION_ID"] = payload.get("session_id", "")
    try:
        reason = hold_applies()
    except HoldStateReadError as error:
        return f"natedev hold state cannot be read: {' '.join(str(error).split())}"
    if reason and building_command(command, suite_runner_only=True):
        return reason + " This run waits for the hold to end. Run it after the release."
    return reason


def main() -> None:
    try:
        reason = decide(sys.stdin.read())
    except Exception as error:
        reason = f"natedev build hold decision cannot be read: {type(error).__name__}: {' '.join(str(error).split())}"
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
