"""The push-alone guard denies a Bash call that holds git push with any other command."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import TypedDict, cast

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

HOOK = Path(__file__).with_name("pre-tool-use-push-alone.py")
REASON = "run git push alone in its own Bash call"
# rca 261114-1: the ask rule Bash(git push * -f *) matched this command through 'rm -f'.
NATEST_COMMAND = ("git push -q origin --delete natest-trial-267-branch && "
                  + "export GIT_INDEX_FILE=$(mktemp) && git read-tree HEAD && "
                  + "rm -f $GIT_INDEX_FILE && git push -q origin HEAD:refs/heads/natest-trial-268-branch")


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


def run_hook(command: str, tool: str = "Bash") -> str:
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
    result = subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True, text=True,
                            check=False, timeout=TEST_HANG_GUARD_SECS)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


class PushAloneTests(unittest.TestCase):
    def assert_denied(self, command: str) -> None:
        response = cast(HookResponse, json.loads(run_hook(command)))
        decision = response["hookSpecificOutput"]
        self.assertEqual((decision["hookEventName"], decision["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn(REASON, decision["permissionDecisionReason"])

    def test_the_natest_command_is_denied(self) -> None:
        self.assert_denied(NATEST_COMMAND)

    def test_a_push_with_any_other_command_is_denied(self) -> None:
        for command in (
            "cd /tmp/repo && git push -q origin HEAD:refs/heads/x",
            "git push -q origin HEAD:refs/heads/x; echo done",
            "git push origin x 2>&1 | tail -3",
            "git fetch -q origin\ngit push -q origin x",
            "for b in a b; do git push -q origin $b; done",
            "env GIT_SSH_COMMAND=ssh git -C /tmp/repo push origin x || true",
            "out=$(git push --porcelain origin x) && echo $out",
            "git status --short\n/usr/bin/git push origin x",
        ):
            with self.subTest(command=command):
                self.assert_denied(command)

    def test_a_push_alone_passes(self) -> None:
        for command in (
            "git push -q origin HEAD:refs/heads/x",
            "git -C /tmp/repo push -q git@github.com:natepiano/claude_commands HEAD:refs/heads/x",
            "GIT_SSH_COMMAND=ssh git push origin x 2>&1  # push the branch",
        ):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command), "")

    def test_other_commands_pass(self) -> None:
        for command, tool in (
            ("git fetch -q origin && git log --oneline -1", "Bash"),
            ("echo 'git push' && ls", "Bash"),
            ("git commit -q -m 'push the branch' && git status", "Bash"),
            ("git push origin x && ls", "Write"),
        ):
            with self.subTest(command=command):
                self.assertEqual(run_hook(command, tool), "")


if __name__ == "__main__":
    _ = unittest.main()
