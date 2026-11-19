"""Route a queued lint to the Mac before local repairs start."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from typing import override

from scripts.build_queue.admission_test_support import caller_admission, caller_provider_controls
from scripts.build_queue import admission_protocol as wire, placement
from scripts.direct.verify_hold_wait_test_support import VerifyHoldTools
from scripts.direct.verify_lint_scope_test_support import CARGO_STUB, SCRIPTS, TAPLO_STUB
from scripts.direct.verify_mac_offload_test_support import RUNNER_STUB
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


class VerifyLintFreeMacTests(VerifyHoldTools):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.write_executable(self.bin_directory / "cargo", CARGO_STUB)
        self.write_executable(self.bin_directory / "taplo", TAPLO_STUB)
        config = self.root / "lint.conf"
        _ = write_config_fixture(config, "mend=on\nfmt=on\nclippy=on\n")
        self.environment.update({
            "LINT_CONFIG_READER": str(SCRIPTS / "lint/lint_config.sh"),
            "LINT_CONFIG_FILE": str(config),
            "LINT_SKIP_SWEEP": "1",
            "TEST_TAPLO_EVENTS": str(self.root / "taplo-events.jsonl"),
            "TEST_TAPLO_STATUS": "0",
            "TEST_RUNNER_MAC": "passed",
            "TEST_RUNNER_STATUS": "0",
        })

    def queued_lint(self) -> subprocess.Popen[str]:
        _ = self.hold_memory()
        admission = caller_admission(self, self.root, self.environment)
        controls = caller_provider_controls(self, admission)
        controls.set_host("mac", placement.TemporaryExclusion("busy"))
        previous = admission.server.reply_filter

        def release_provider(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait):
                _ = controls.hosts.pop("mac", None)
            return previous(reply)

        admission.server.reply_filter = release_provider
        process = self.start_verify("lint", "sample")
        self.addCleanup(self.stop_verify, process)
        return process

    def finish_lint(self, process: subprocess.Popen[str], *, status: int = 0) -> str:
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, status, output + errors)
        return output + errors

    def release_memory(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        with admission.loop_turn():
            admission.free(64 * 1024**3)

    def local_mend(self) -> list[list[str]]:
        return [row["args"] for row in self.cargo_events()
                if row["args"][:1] == ["mend"] and "--version" not in row["args"]]

    def mac_verbs(self) -> list[str]:
        words = [row["args"] for row in self.runner_events()]
        for arguments in words:
            self.assertNotIn("--fix", arguments)
        return [arguments[arguments.index("--verb") + 1] for arguments in words]

    def test_clean_lint_leaves_memory_for_the_mac_without_local_repairs(self) -> None:
        process = self.queued_lint()
        output = self.finish_lint(process)
        self.assertIn("another machine is free after", output)
        self.assertEqual(self.mac_verbs(), ["mend-check", "clippy"])
        self.assertEqual(self.local_mend(), [])
        local = [row["args"] for row in self.cargo_events()]
        self.assertTrue(any("fmt" in arguments and "--all" in arguments for arguments in local), local)
        self.assertFalse(any(arguments[:1] == ["clippy"] for arguments in local), local)
        self.assertEqual(list(Path(self.environment["TMPDIR"]).glob("tmp.*")), [])

    def test_mac_finding_returns_to_memory_and_repairs_on_natedev(self) -> None:
        self.environment.update({"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "101"})
        process = self.queued_lint()
        self.wait_until(lambda: len(self.runner_events()) == 3, "the Mac judgments and fix pass", (process,),
                        directory=self.runner_state)
        self.wait_for_memory_wait(process)
        self.assertEqual(self.local_mend(), [])
        self.set_mac_answer("passed", "", 0)
        self.release_memory()
        output = self.finish_lint(process)
        self.assertIn("the Mac fix pass did not finish; natedev repairs the tree", output)
        self.assertEqual(self.mac_verbs(), ["mend-check", "mend-fix", "clippy", "clippy"])
        self.assertEqual(self.local_mend(), [["mend", "--all-targets", "--workspace", "--fix"]])
        local = [row["args"] for row in self.cargo_events()]
        self.assertFalse(any(arguments[:1] == ["clippy"] for arguments in local), local)

    def repair_lint_on_mac(self, *, status: int = 0, clippy_runs: bool = True) -> str:
        self.write_executable(self.runner, RUNNER_STUB.replace(
            "if verb_answer.exists():",
            'if verb_answer.exists() and (attempt == 1 or verb_answer.name != "runner-answer-mend-check"):',
        ))
        self.set_verb_answer("mend-check", "failed", 1)
        self.set_verb_answer("mend-fix", "passed", 0, "applied")
        output = self.finish_lint(self.queued_lint(), status=status)
        expected = ["mend-check", "mend-fix", "mend-check"]
        if clippy_runs:
            expected.append("clippy")
        self.assertEqual(self.mac_verbs(), expected)
        return output

    def assert_lint_runs_again(self) -> None:
        self.assertTrue((self.board / "verify_cache").is_dir())
        self.assertEqual(self.saved_results(), [])
        self.clear_observations()
        for answer in self.runner_state.glob("runner-answer-*"):
            answer.unlink()
        self.release_memory()
        process = self.start_verify("lint", "sample")
        self.addCleanup(self.stop_verify, process)
        output = self.finish_lint(process)
        self.assertTrue(self.local_mend(), output)

    def test_mac_fix_pass_repairs_the_copy_without_natedev(self) -> None:
        output = self.repair_lint_on_mac()
        self.assertIn("the returned worktree passed lint after formatting", output)
        fix_words = self.runner_events()[1]["args"]
        self.assertIn("--workspace", fix_words)
        self.assertNotIn("--fail-on-warn", fix_words)
        self.assertEqual(self.local_mend(), [])
        local = [row["args"] for row in self.cargo_events()]
        self.assertFalse(any(arguments[:1] == ["clippy"] for arguments in local), local)

    def test_final_taplo_panic_does_not_cache_a_lint_failure(self) -> None:
        self.write_executable(self.bin_directory / "taplo", TAPLO_STUB.replace(
            'status = int(os.environ["TEST_TAPLO_STATUS"])',
            '''status = int(os.environ["TEST_TAPLO_STATUS"])
if "--check" in sys.argv and (Path(os.environ["TEST_RUNNER_STATE"]) / "runner-events.jsonl").exists():
    status = 101''',
        ))
        output = self.repair_lint_on_mac(status=2, clippy_runs=False)
        self.assertIn("taplo panicked", output)
        self.assert_lint_runs_again()

    def test_final_mac_clippy_sandbox_failure_does_not_cache_a_lint_failure(self) -> None:
        self.set_verb_answer("clippy", "failed", 3)
        _ = self.repair_lint_on_mac(status=3)
        self.assert_lint_runs_again()

    def test_final_mac_clippy_kill_preserves_status_without_a_verdict(self) -> None:
        self.environment["TEST_RUNNER_KILLED_VERB"] = "clippy"
        self.set_verb_answer("clippy", "failed", 101)
        _ = self.repair_lint_on_mac(status=101)
        self.assert_lint_runs_again()

    def test_final_mac_clippy_finding_still_caches(self) -> None:
        self.set_verb_answer("clippy", "failed", 101)
        _ = self.repair_lint_on_mac(status=1)
        self.assertEqual([path.suffix for path in self.saved_results()], [".fail"])
        self.release_memory()
        process = self.start_verify("lint", "sample")
        self.addCleanup(self.stop_verify, process)
        self.assertIn("FAIL (recorded)", self.finish_lint(process, status=1))
        self.assertEqual(self.mac_verbs(), ["mend-check", "mend-fix", "mend-check", "clippy"])

    def test_held_clippy_sandbox_failure_preserves_its_status(self) -> None:
        self.set_verb_answer("clippy", "failed", 3)
        _ = self.finish_lint(self.queued_lint(), status=3)
        self.assertEqual(self.mac_verbs(), ["mend-check", "clippy"])
        self.assertEqual(self.local_mend(), [])
        self.assert_lint_runs_again()

    def test_mac_mend_fix_tooling_failure_preserves_its_status(self) -> None:
        self.set_verb_answer("mend-check", "failed", 1)
        self.set_verb_answer("mend-fix", "failed", 3)
        _ = self.finish_lint(self.queued_lint(), status=3)
        self.assertEqual(self.mac_verbs(), ["mend-check", "mend-fix"])
        self.assertEqual(self.local_mend(), [])
        self.assert_lint_runs_again()

    def test_held_taplo_format_panic_is_a_tooling_failure(self) -> None:
        self.environment["TEST_TAPLO_STATUS"] = "101"
        output = self.finish_lint(self.queued_lint(), status=2)
        self.assertIn("taplo panicked", output)
        self.assertNotIn("TOML check failed", output)
        self.assertEqual(self.mac_verbs(), [])
        self.assertEqual(self.local_mend(), [])
        self.environment["TEST_TAPLO_STATUS"] = "0"
        self.assert_lint_runs_again()

    def test_refused_mac_fix_repairs_on_natedev(self) -> None:
        self.set_verb_answer("mend-check", "failed", 1)
        self.set_verb_answer("mend-fix", "passed", 0, "refused")
        process = self.queued_lint()
        self.wait_until(lambda: len(self.runner_events()) == 3, "the Mac judgments and fix pass", (process,),
                        directory=self.runner_state)
        self.wait_for_memory_wait(process)
        self.assertEqual(self.local_mend(), [])
        self.release_memory()
        output = self.finish_lint(process)
        self.assertIn("the Mac fix pass did not finish; natedev repairs the tree", output)
        self.assertEqual(self.mac_verbs(), ["mend-check", "mend-fix", "clippy", "clippy"])
        self.assertEqual(self.local_mend(), [["mend", "--all-targets", "--workspace", "--fix"]])

    def test_refused_mac_fix_does_not_cache_failed_lint(self) -> None:
        self.set_verb_answer("mend-check", "failed", 1)
        self.set_verb_answer("mend-fix", "passed", 0, "refused")
        # Make the local repair fail before it changes the tree.
        self.write_executable(self.bin_directory / "cargo", CARGO_STUB + '''
if arguments[:1] == ["mend"]:
    raise SystemExit(1)
''')
        process = self.queued_lint()
        self.wait_until(lambda: len(self.runner_events()) == 3, "the Mac judgments and fix pass", (process,),
                        directory=self.runner_state)
        self.wait_for_memory_wait(process)
        self.release_memory()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)

        self.assertEqual(process.returncode, 1, output + errors)
        self.assertIn("the Mac fix pass did not finish", output)
        self.assertEqual(self.mac_verbs(), ["mend-check", "mend-fix", "clippy"])
        self.assertEqual(self.local_mend(), [["mend", "--all-targets", "--workspace", "--fix"]])
        cache = self.board / "verify_cache"
        self.assertTrue(cache.is_dir())
        self.assertEqual(list(cache.glob("*.fail")), [])

    def test_busy_mac_keeps_the_lint_in_the_local_memory_queue(self) -> None:
        self.environment["TEST_RUNNER_BUSY_UNTIL"] = str(self.runner_state / "mac-free")
        process = self.queued_lint()
        self.wait_for_memory_wait(process)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_mend(), [])
        self.release_memory()
        _ = self.finish_lint(process)
        self.assertEqual(self.mac_verbs(), [])
        self.assertEqual(sum(row["args"][:1] == ["clippy"] and "--fix" not in row["args"]
                             for row in self.cargo_events()), 1)
        self.assertEqual(self.local_mend(), [["mend", "--all-targets", "--workspace", "--fix"]])

    def test_linux_only_workspace_lint_leaves_memory_for_the_mac(self) -> None:
        # Only the Mac compiles macOS code, so a lint ignores linux_only and the Mac judges the whole workspace.
        settings = Path(self.environment["MAC_TEST_CONFIG"])
        _ = write_config_fixture(settings, f"offload=on\nlinux_only.{self.root.name}=sample\n", template="mac_test")
        process = self.queued_lint()
        output = self.finish_lint(process)
        self.assertIn("another machine is free after", output)
        self.assertEqual(self.mac_verbs(), ["mend-check", "clippy"])
        self.assertEqual(self.local_mend(), [])


if __name__ == "__main__":
    _ = unittest.main()
