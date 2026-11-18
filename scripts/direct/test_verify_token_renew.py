"""Check board renewal and loaded lint input identity."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.lib.helper_tree_test_support import VERIFY_HELPERS, copy_helpers

from scripts.direct import verify_lint_scope_test_support as lint_support
from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture
from scripts.lib.shell_test_support import shell_functions
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, BOARD, VERIFY

SCRIPTS = VERIFY.parent.parent


class BoardRenewTests(VerifyTokenWaitFixture):
    def board(self, board: Path, *words: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(BOARD), words[0], str(board), *words[1:]],
                              env=self.environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)

    def test_renew_requires_the_matching_holder_pid(self) -> None:
        board = self.root / "board"
        answer = self.board(board, "acquire", "same", "cargo", "--pid", str(os.getpid()))
        self.assertEqual(answer.returncode, 0, answer.stderr)
        lock = board / "locks" / "cargo.d"
        _ = (lock / "expires").write_text("0")
        wrong = self.board(board, "renew", "same", "cargo", "--pid", str(os.getppid()), "--hold", "30")
        self.assertNotEqual(wrong.returncode, 0)
        self.assertEqual((lock / "expires").read_text(), "0")
        _ = write_config_fixture(self.root / "direct.conf", "TOKEN_GUARD_ATTEMPTS=2\nTOKEN_GUARD_DELAY_MS=1")
        with (board / "locks/cargo.guard").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            busy = self.board(board, "renew", "same", "cargo", "--pid", str(os.getpid()), "--hold", "30")
            self.assertEqual(busy.returncode, 2, busy.stdout + busy.stderr)
            self.assertEqual((lock / "expires").read_text(), "0")
        right = self.board(board, "renew", "same", "cargo", "--pid", str(os.getpid()), "--hold", "30")
        self.assertEqual(right.returncode, 0, right.stderr)
        self.assertGreater(int((lock / "expires").read_text()), 0)
        _ = self.board(board, "release", "same", "cargo", "--pid", str(os.getpid()))

class LintInputIdentityTests(VerifyLintCacheFixture):
    def assert_lint_calls(self, *, fixes: int = 0) -> None:
        judgment = ["--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings"]
        expected = [judgment]
        if fixes:
            fix = ["--jobs", "8", "--fix", "--allow-dirty", "--allow-staged", "--allow-no-vcs", *judgment]
            expected = [judgment, *([fix] * fixes), judgment]
        self.assertEqual(self.calls("clippy"), expected)

    def private_scripts(self) -> tuple[Path, Path]:
        scripts = Path(self.environment["HOME"]) / ".claude" / "scripts"
        _ = copy_helpers(Path(self.environment["HOME"]), *VERIFY_HELPERS)
        return scripts / "direct" / "verify.sh", scripts / "lint" / "invoke.sh"

    def test_script_edits_during_lint_add_no_fix_pass_or_tree_change_notice(self) -> None:
        verify, invoke = self.private_scripts()
        _ = self.enable_cache()
        cargo = lint_support.CARGO_STUB.replace('if arguments[:1] == ["clippy"]:', '''if arguments[:1] == ["clippy"]:
    for name in ("TEST_PRIVATE_VERIFY", "TEST_PRIVATE_INVOKE", "LINT_CONFIG_FILE"):
        with Path(os.environ[name]).open("a") as script:
            script.write("\\n# changed while clippy ran\\n")
if arguments[:1] == ["clippy"]:''')
        self.write_executable(self.root / "bin" / "cargo", cargo)
        self.environment.update(TEST_PRIVATE_VERIFY=str(verify), TEST_PRIVATE_INVOKE=str(invoke))
        with mock.patch.object(lint_support, "VERIFY", verify):
            result = self.verify("lint", "sample")
        self.assert_lint_calls()
        self.assertNotIn("tree changed", result.stdout + result.stderr)

    def test_script_edit_during_fix_cannot_cache_a_pass_for_the_new_script(self) -> None:
        verify, invoke = self.private_scripts()
        cache = self.enable_cache()
        cargo = lint_support.CARGO_STUB.replace('if arguments[:1] == ["clippy"]:', '''if arguments[:1] == ["clippy"] and "--fix" in arguments:
    script = Path(os.environ["TEST_PRIVATE_INVOKE"])
    marker = script.with_suffix(".edited")
    if not marker.exists():
        with script.open("a") as output:
            output.write("\\n# updated once during the fix pass\\n")
        marker.touch()
if arguments[:1] == ["clippy"]:''')
        from scripts.direct.test_verify_lint_judgment import FIRST_JUDGMENT, LINT_FINDING
        self.write_executable(self.root / "bin" / "cargo", FIRST_JUDGMENT + cargo)
        self.environment.update({"TEST_CLIPPY_FIRST_STATUS": "101",
                                 "TEST_CLIPPY_FIRST_DIAGNOSTIC": LINT_FINDING})
        self.environment["TEST_PRIVATE_INVOKE"] = str(invoke)
        with mock.patch.object(lint_support, "VERIFY", verify):
            _ = self.verify("lint", "sample")
            self.assert_lint_calls(fixes=1)
            self.assertEqual(list(cache.glob("*.pass")), [], "old loaded code certified the new script")
            _ = self.verify("lint", "sample")
            self.assert_lint_calls(fixes=1)
            self.assertEqual(len(list(cache.glob("*.pass"))), 1)
            _ = self.verify("lint", "sample")
            self.assertEqual(self.calls("clippy"), [])


    def test_unstarted_status_is_not_cached_as_a_tree_lint_failure(self) -> None:
        cache = self.enable_cache()
        self.environment["TEST_CLIPPY_STATUS"] = "125"
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
        self.assertEqual(list(cache.glob("*.fail")), [])
        self.environment["TEST_CLIPPY_STATUS"] = "0"
        _ = self.verify("lint", "sample")
        self.assert_lint_calls()

    def test_a_lint_script_edit_invalidates_a_previously_reused_pass(self) -> None:
        verify, _ = self.private_scripts()
        _ = self.enable_cache()
        with mock.patch.object(lint_support, "VERIFY", verify):
            _ = self.verify("lint", "sample")
            _ = self.verify("lint", "sample")
            self.assertEqual(self.calls("clippy"), [])
            # LintKeyInputTests checks that the tree key reads each lint script. This complete run checks that
            # an edit to one of them changes the key and stops the reuse of the cached pass.
            with verify.open("a") as output:
                _ = output.write("\n# cache input changed\n")
            _ = self.verify("lint", "sample")
            self.assert_lint_calls()


class LintKeyInputTests(unittest.TestCase):
    def test_the_lint_tree_key_reads_verify_invoke_prebuild_dev_shell_fix_return_and_the_lint_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "verify.sh"
            config = root / "lint.conf"
            _ = script.write_text(shell_functions(VERIFY, "lint_code_files", "tree_key") + "\n" + r'''
verify_helper() { printf '%s\0' "$@"; }
CMD=lint ARGS=(sample) FILTER_RUN=0 CACHE_DIR=cache GATE_METADATA={} VERIFY_USER_ENVIRONMENT=()
TREE_WATCH_SCRIPT=tree_watch.py FIX_RETURN_SCRIPT=fix_return.py
BUILD_HELPER_ROOT=$HOME/.claude/scripts
tree_key --with-tree-content
''')
            result = subprocess.run(["bash", "-e", str(script)], cwd=root, text=True, capture_output=True,
                                    env={**os.environ, "HOME": str(root), "LINT_CONFIG_FILE": str(config)},
                                    timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        words = result.stdout.split("\0")[:-1]
        separator = words.index("--")
        self.assertEqual(words[:2], ["--stdin", "tree_watch.py"])
        self.assertEqual(words[separator - 6:separator],
                         [str(script), str(root / ".claude/scripts/lint/invoke.sh"),
                          str(root / ".claude/scripts/lint/prebuild.py"), str(root / ".claude/scripts/lib/dev_shell.py"),
                          "fix_return.py", str(config)])
        self.assertEqual(words[separator + 1:], ["lint"])

if __name__ == "__main__":
    _ = unittest.main()
