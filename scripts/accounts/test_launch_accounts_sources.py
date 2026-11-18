"""Check account selection in scans of the repository's own scripts."""
from __future__ import annotations

from pathlib import Path
import re
import unittest

from scripts.accounts.test_launch_accounts import EXCEPTIONS, launch_account_violations, shell_launch_account_violations
from scripts.lib.code_kind import is_test_code

ROOT = Path(__file__).resolve().parents[1]
EXCEPTION_FILES = frozenset(file for file, _ in EXCEPTIONS)
# starts_claude needs the literal "claude" or a "command claude" or "exec claude" string.
CLAUDE_START_WORDS = re.compile(r"""["']claude["']|(?:command|exec) claude""")


class LaunchAccountTests(unittest.TestCase):
    def test_all_python_claude_launchers_use_the_shared_account_resolver(self) -> None:
        violations: list[str] = []
        for path in sorted(ROOT.rglob("*.py")):
            if is_test_code(path):
                continue
            source = path.read_text(encoding="utf-8")
            relative = str(path.relative_to(ROOT))
            # A finding needs a Claude start or an exception entry: a parse of each other file only costs time.
            if relative not in EXCEPTION_FILES and not CLAUDE_START_WORDS.search(source):
                continue
            violations.extend(launch_account_violations(source, relative))
        self.assertEqual(violations, [], "\n".join(violations))

    def test_shell_launchers_do_not_pick_or_set_an_unresolved_account(self) -> None:
        for arguments in ("--print prompt", "-p prompt", "prompt", ""):
            with self.subTest(arguments=arguments):
                selecting = f'chosen=$(python accounts.py pick claude)\nCLAUDE_CONFIG_DIR="$chosen" claude {arguments}\n'
                self.assertTrue(shell_launch_account_violations(selecting, "fixture.sh"))
                unresolved = f'CLAUDE_CONFIG_DIR="/somewhere/.claude3" claude {arguments}\n'
                self.assertTrue(shell_launch_account_violations(unresolved, "fixture.sh"))
                for directory in ("resolver_config_dir", "record_config_dir"):
                    rendering = f'CLAUDE_CONFIG_DIR="${directory}" claude {arguments}\n'
                    self.assertEqual(shell_launch_account_violations(rendering, "fixture.sh"), [])
        violations: list[str] = []
        for path in sorted(ROOT.rglob("*.sh")):
            if is_test_code(path):
                continue
            violations.extend(shell_launch_account_violations(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT))))
        self.assertEqual(violations, [], "\n".join(violations))


if __name__ == "__main__":
    _ = unittest.main()
