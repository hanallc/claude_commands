"""Call the routing parser without a complete verify run."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.lib.helper_tree_test_support import verify_test_environment
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyArgumentParserTests(unittest.TestCase):
    def test_verify_refuses_an_invalid_verb_list_before_setup(self) -> None:
        script = Path(__file__).with_name("verify.sh")
        cases = (("test,unknown",), ("test,build",), ("test,",), ("test,,doc",),
                 ("test,lint", "--test", "one"), ("test,lint", "+"))
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace"
            environment = {**verify_test_environment(), "VERIFY_PY_TRACE": str(trace)}
            for words in cases:
                with self.subTest(words=words):
                    result = subprocess.run(["bash", str(script), *words], cwd=directory,
                                            env=environment, capture_output=True, text=True,
                                            timeout=TEST_HANG_GUARD_SECS, check=False)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertEqual(result.stdout, "")
                    message, = result.stderr.splitlines()
                    self.assertIn("verb list", message)
                    self.assertIn("test, lint, doc", message)
            self.assertEqual(list(Path(directory).glob("trace*")), [])

    def test_valid_step_list_passes_precheck_without_cargo_on_path(self) -> None:
        script = Path(__file__).with_name("verify.sh")
        bash = shutil.which("bash")
        env = shutil.which("env")
        assert bash is not None and env is not None
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory) / "bin"
            tools.mkdir()
            (tools / "env").symlink_to(env)
            environment = {**verify_test_environment(), "PATH": str(tools)}
            result = subprocess.run([bash, str(script), "check", "sample", "+", "lint"], cwd=directory,
                                    env=environment, capture_output=True, text=True,
                                    timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("has no Cargo.toml", result.stderr)

    def test_parser_accepts_routing_flags_and_refuses_a_missing_reason(self) -> None:
        script = shell_functions(Path(__file__).with_name("verify.sh"), "parse_routing_arguments") + '''
CMD=test
VERIFY_CALL_ENVIRONMENT=()
parse_routing_arguments "$@"
set -- "${ARGS[@]}"
printf '%s\\n' "$MAC_ONLY" "$MAC_REASON" "$LOCAL_ONLY" "$NO_CACHE" "$PINNED_TARGET" "$@"
'''
        with tempfile.TemporaryDirectory() as directory:
            accepted = subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", script, "parser-test",
                 "--mac", "macOS graphics", "--no-cache", "sample", "exact test"],
                env=verify_test_environment(),
                cwd=directory, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(accepted.stdout.splitlines(),
                             ["1", "macOS graphics", "0", "1", "", "sample", "exact test"])
            self.assertEqual(accepted.stderr, "")
            refused = subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", script, "parser-test", "sample", "--mac"],
                env=verify_test_environment(),
                cwd=directory, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(refused.stdout, "")
            self.assertEqual(refused.stderr,
                             'verify.sh: --mac needs a reason: --mac "<why this command must run on macOS>".\n')


if __name__ == "__main__":
    _ = unittest.main()
