#!/usr/bin/env python3
"""Deny a Bash call that holds git push together with any other command.

An ask rule such as Bash(git push * -f *) matches the whole command text, so
in a compound command its `*` reaches the words of other commands: rca 261114-1
matched the -f of an `rm -f`. The rule then asks the user. This hook runs before
the ask rules, so the session gets a deny and the user gets no prompt.
"""
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import re
import sys
from collections.abc import Sequence
from typing import TypedDict, cast

from scripts.hooks.checkpoint_gate import ASSIGNMENT, WRAPPER_ARGUMENTS, shell_commands, shell_text, without_redirections

REASON = "run git push alone in its own Bash call"
KEYWORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}"})
# git options that take their value as the next word.
GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path"})
PUSH_TEXT = re.compile(r"\bgit\b.*\bpush\b", re.DOTALL)


class BashInput(TypedDict, total=False):
    command: str


class HookInput(TypedDict, total=False):
    tool_name: str
    tool_input: BashInput


def program_position(words: Sequence[str]) -> int:
    """The index of the program word, after assignments, keywords and wrappers with their options."""
    position = 0
    while position < len(words):
        raw = words[position]
        word = shell_text(raw)
        if ASSIGNMENT.match(raw) or raw in KEYWORDS:
            position += 1
            continue
        wrapper = word.rsplit("/", 1)[-1]
        if wrapper not in WRAPPER_ARGUMENTS:
            return position
        position += 1
        while position < len(words) and shell_text(words[position]).startswith("-"):
            option = shell_text(words[position])
            position += 2 if option in WRAPPER_ARGUMENTS[wrapper] else 1
            if option == "--":
                break
    return position


def is_git_push(words: Sequence[str]) -> bool:
    """Whether one shell command runs git push."""
    words = without_redirections(tuple(words))
    position = program_position(words)
    if position >= len(words) or shell_text(words[position]).rsplit("/", 1)[-1] != "git":
        return False
    position += 1
    while position < len(words):
        word = shell_text(words[position])
        if not word.startswith("-"):
            return word == "push"
        position += 2 if word in GIT_VALUE_OPTIONS else 1
    return False


def push_with_others(command: str) -> bool:
    """Whether the command holds git push and one or more other commands."""
    if not PUSH_TEXT.search(command):
        return False
    try:
        segments = [segment.words for segment in shell_commands(command) if segment.words]
        return len(segments) > 1 and any(is_git_push(words) for words in segments)
    except ValueError:
        # An unreadable command that names git and push and has a separator could hide a push.
        return re.search(r"[;&|\n`]|\$\(", command) is not None


def bash_command(raw: str) -> str | None:
    """The command of a Bash tool call, or None for any other input."""
    try:
        hook_input = cast(HookInput, json.loads(raw))
        if hook_input.get("tool_name") != "Bash":
            return None
        command = hook_input.get("tool_input", {}).get("command")
    except (ValueError, AttributeError):
        return None
    return command if isinstance(command, str) else None


def main() -> None:
    command = bash_command(sys.stdin.read())
    if command is None or not push_with_others(command):
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"Refused: {REASON}. Put each other command in an earlier or later call.",
    }}))


if __name__ == "__main__":
    main()
