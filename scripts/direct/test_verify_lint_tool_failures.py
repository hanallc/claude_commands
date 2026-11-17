"""Check tooling failures cannot cache a lint judgment."""

from __future__ import annotations

from pathlib import Path
import unittest
from scripts.direct import verify_lint_scope_test_support as support
from scripts.direct.verify_lint_scope_test_support import TAPLO_STUB as TAPLO_STUB

# Fails place-step the way an old module in the helper did; other commands run the real client.
CRASHING_ADMISSION_CLIENT = """from scripts.build_queue import admission_client


def main(argv: list[str]) -> int | None:
    if argv[:1] == ["place-step"]:
        raise ImportError("cannot import name 'StepRoute' (test placement fault)")
    return admission_client.main(argv)
"""

# Points memory_gate.sh at TEST_ADMISSION_CLIENT once the gate is sourced.
ADMISSION_CLIENT_OVERRIDE = r"""
source() {
    builtin source "$@"
    local status=$?
    if [[ "$1" == */memory_gate.sh ]]; then
        if [ -n "${TEST_ADMISSION_CLIENT:-}" ]; then BUILDLOG_MEM_ADMIT_SCRIPT=$TEST_ADMISSION_CLIENT; fi
        unset -f source
    fi
    return "$status"
}
"""


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def crash_place_step(self) -> None:
        original = Path(self.environment["LINT_CONFIG_READER"]).resolve()
        scripts = self.root / "reader-scripts"
        (scripts / "lint").mkdir(parents=True)
        # The reader sources ../lib/conf.sh from its own folder.
        (scripts / "lib").symlink_to(original.parents[1] / "lib", target_is_directory=True)
        reader = scripts / "lint" / "lint_config.sh"
        _ = reader.write_text(original.read_text(encoding="utf-8") + ADMISSION_CLIENT_OVERRIDE, encoding="utf-8")
        client = self.root / "crashing_admission_client.py"
        _ = client.write_text(CRASHING_ADMISSION_CLIENT, encoding="utf-8")
        self.environment.update(LINT_CONFIG_READER=str(reader), TEST_ADMISSION_CLIENT=str(client))

    def test_a_placement_crash_before_mend_starts_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.crash_place_step()
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(cache.glob("*.fail")), [])
        self.assertEqual(self.calls("mend"), [])
        self.assertIn("test placement fault", result.stderr)
        self.assertIn("before the step started", result.stderr)
        self.assertNotIn("No such file", result.stderr)

        del self.environment["TEST_ADMISSION_CLIENT"]
        _ = self.verify("lint", "sample")
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_missing_taplo_fails_before_stages_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.isolate_path_without_taplo()
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr.splitlines(), ["taplo is required"])
        self.assertEqual(self.calls("mend"), [])
        self.assertEqual(self.calls("+nightly"), [])
        self.assertEqual(self.calls("clippy"), [])
        self.assertEqual(self.calls("doc"), [])
        self.assertEqual(self.taplo_calls(), [])
        self.assertEqual(list(cache.glob("*.fail")), [])

        self.write_executable(self.root / "bin" / "taplo", TAPLO_STUB)
        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_taplo_panic_on_linux_is_a_tooling_failure_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.write_shell(self.root / "bin" / "uname", 'printf "Linux\\n"\n')
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=101)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("taplo panicked", result.stderr)
        self.assertIn("tooling failure", result.stderr)
        self.assert_workspace_taplo_check()
        self.assertEqual(list(cache.glob("*.fail")), [])

        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

    def test_taplo_panic_on_macos_requests_unsandboxed_retry_and_is_not_cached(self) -> None:
        cache = self.enable_cache()
        self.write_shell(self.root / "bin" / "uname", 'printf "Darwin\\n"\n')
        result = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=101)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("taplo panicked", result.stderr)
        self.assertIn("macOS sandbox", result.stderr)
        self.assertIn("Re-run the same command with the sandbox disabled", result.stderr)
        self.assert_workspace_taplo_check()
        self.assertEqual(list(cache.glob("*.fail")), [])

        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)


if __name__ == "__main__":
    _ = unittest.main()
