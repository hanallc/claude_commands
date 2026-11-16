"""Check runner result files without complete verify.sh calls."""
from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from typing import override
from unittest import mock

from scripts.direct import runner_result, verify_helper
from scripts.direct.runner_result import UnusableRunnerResult, UsableRunnerResult, read_result


class RunnerResultTests(unittest.TestCase):
    root: Path = Path()
    path: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "result"

    def write(self, text: str) -> None:
        _ = self.path.write_bytes(text.encode("utf-8", errors="surrogateescape"))

    def test_missing_results_are_unusable_for_both_moved_statuses(self) -> None:
        for status in (75, 1):
            with self.subTest(status=status):
                self.assertIsInstance(read_result(self.path, status, True), UnusableRunnerResult)

    def test_empty_and_directory_results_are_unusable(self) -> None:
        self.write("")
        self.assertIsInstance(read_result(self.path, 75, True), UnusableRunnerResult)
        self.path.unlink()
        self.path.mkdir()
        self.assertIsInstance(read_result(self.path, 75, True), UnusableRunnerResult)

    def test_inconsistent_results_are_unusable(self) -> None:
        for word, status in (("other", 75), ("passed", 1), ("failed", 0)):
            with self.subTest(word=word, status=status):
                self.write(f"mac={word}\nreason=\nseconds=3.25\n")
                self.assertIsInstance(read_result(self.path, status, True), UnusableRunnerResult)

    def test_lost_and_busy_results_keep_the_moved_fields(self) -> None:
        for text, expected in (
            ("mac=lost\n", UsableRunnerResult("lost")),
            ("mac=declined\nreason=busy\n", UsableRunnerResult("declined", reason="busy")),
        ):
            with self.subTest(text=text):
                self.write(text)
                self.assertEqual(read_result(self.path, 75, True), expected)

    def test_invalid_text_is_unusable(self) -> None:
        self.write("invalid\n")
        self.assertIsInstance(read_result(self.path, 75, True), UnusableRunnerResult)

    def test_valid_words_keep_the_runner_status_rules(self) -> None:
        for word, status in (("passed", 0), ("failed", 1), ("failed", 42),
                             ("lost", 0), ("lost", 75), ("declined", 0), ("declined", 75)):
            with self.subTest(word=word, status=status):
                self.write(f"mac={word}\n")
                result = read_result(self.path, status, True)
                self.assertIsInstance(result, UsableRunnerResult)
                assert isinstance(result, UsableRunnerResult)
                self.assertEqual(result.word, word)

    def test_outcome_counts_only_for_a_non_mac_target(self) -> None:
        self.write("outcome=passed\n")
        self.assertEqual(read_result(self.path, 0, False), UsableRunnerResult("passed"))
        self.assertIsInstance(read_result(self.path, 0, True), UnusableRunnerResult)
        self.write("outcome=failed\nmac=passed\n")
        self.assertEqual(read_result(self.path, 0, True), UsableRunnerResult("passed"))
        self.assertIsInstance(read_result(self.path, 0, False), UnusableRunnerResult)

    def test_mac_counts_for_both_targets(self) -> None:
        self.write("mac=passed\n")
        for selected_is_mac in (True, False):
            with self.subTest(selected_is_mac=selected_is_mac):
                self.assertEqual(read_result(self.path, 0, selected_is_mac), UsableRunnerResult("passed"))

    def test_exactly_one_word_is_required(self) -> None:
        for text in ("reason=busy\n", "mac=passed\nmac=passed\n", "mac=\n", "mac=Passed\n",
                     "mac= passed\n", "mac=passed \n", "outcome=passed\noutcome=passed\n"):
            with self.subTest(text=text):
                self.write(text)
                self.assertIsInstance(read_result(self.path, 0, False), UnusableRunnerResult)

    def test_all_fields_keep_their_values_and_the_last_value_wins(self) -> None:
        text = ("mac=declined\nunknown=ignored\nreason=first\nreason=busy\nseconds=3.25\n"
                "holder=two words\\path\nfetch=fetched\nfix=changed\n"
                "build_target_directory=/private/target with spaces=a\n")
        self.write(text)
        before = self.path.read_bytes()
        self.assertEqual(read_result(self.path, 75, True),
                         UsableRunnerResult("declined", "busy", "3.25", "two words\\path", "fetched",
                                            "changed", "/private/target with spaces=a"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_empty_fields_replace_earlier_values(self) -> None:
        self.write("mac=passed\nreason=first\nreason=\nseconds=\nholder=\nfetch=\nfix=\nbuild_target_directory=\n")
        self.assertEqual(read_result(self.path, 0, True), UsableRunnerResult("passed", fetch=""))

    def test_line_handling_matches_bash(self) -> None:
        for text, expected in (("mac=passed", UsableRunnerResult("passed")),
                               ("mac=pa\0ssed\n\n", UsableRunnerResult("passed")),
                               ("mac=passed\nreason=with\rreturn\n", UsableRunnerResult("passed", "with\rreturn")),
                               ("mac=passed\nreason=with\vseparator\n", UsableRunnerResult("passed", "with\vseparator")),
                               ("mac=passed\nreason=\udcff\n", UsableRunnerResult("passed", "\udcff"))):
            with self.subTest(text=text):
                self.write(text)
                self.assertEqual(read_result(self.path, 0, True), expected)
        self.write("mac=passed\r\n")
        self.assertIsInstance(read_result(self.path, 0, True), UnusableRunnerResult)

    def test_unreadable_or_failed_reads_are_unusable(self) -> None:
        self.write("mac=passed\n")
        with mock.patch.object(os, "access", return_value=False):
            self.assertIsInstance(read_result(self.path, 0, True), UnusableRunnerResult)
        with mock.patch.object(Path, "open", side_effect=PermissionError("read refused")):
            self.assertIsInstance(read_result(self.path, 0, True), UnusableRunnerResult)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "A FIFO requires os.mkfifo")
    def test_a_non_regular_file_is_not_opened(self) -> None:
        os.mkfifo(self.path)
        self.assertIsInstance(read_result(self.path, 0, True), UnusableRunnerResult)

    def test_a_readable_symlink_keeps_the_regular_file_rule(self) -> None:
        target = self.root / "target"
        _ = target.write_text("mac=passed\n")
        self.path.symlink_to(target)
        self.assertEqual(read_result(self.path, 0, True), UsableRunnerResult("passed"))
        target.unlink()
        self.assertIsInstance(read_result(self.path, 0, True), UnusableRunnerResult)

    def test_main_writes_seven_lines_with_empty_fields(self) -> None:
        self.write("mac=passed\nfetch=\n")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner_result.main([str(self.path), "0", "1"]), 0)
        self.assertEqual(output.getvalue(), "passed\n\n\n\n\n\n\n")
        self.write("mac=declined\nreason=busy\nseconds=2\nholder=owner\nfetch=fetched\nfix=fixed\nbuild_target_directory=/target\n")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner_result.main([str(self.path), "75", "0"]), 0)
        self.assertEqual(output.getvalue(), "declined\nbusy\n2\nowner\nfetched\nfixed\n/target\n")

    def test_main_returns_failure_without_result_fields(self) -> None:
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner_result.main([str(self.path), "75", "1"]), 1)
        self.assertEqual(output.getvalue(), "")
        for arguments in ([], [str(self.path), "bad", "1"], [str(self.path), "0", "mac"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(runner_result.main(arguments), 2)

    def test_shell_reports_helper_faults_but_keeps_unusable_results_quiet(self) -> None:
        shell = Path(__file__).with_name("verify.sh").read_text()
        start = shell.index('    if result_text="$(verify_helper "$VERIFY_START_DIR/runner_result.py"')
        end = shell.index('    if [[ -d "$MAC_RESULT_FILE" ]]', start)
        program = """set -euo pipefail
VERIFY_START_DIR=/unused MAC_RESULT_FILE=/unused runner_status=0 selected_is_mac=1
result_usable=0
verify_helper() { return "$HELPER_STATUS"; }
""" + shell[start:end]
        program += 'printf "%s\\n" "$result_usable"\n'
        for status in (1, 2, 127):
            with self.subTest(status=status):
                result = subprocess.run(
                    ["bash", "-c", program], env={**os.environ, "HELPER_STATUS": str(status)},
                    capture_output=True, text=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "0\n")
                expected = ("" if status == 1 else
                            f"verify.sh: runner_result.py failed (status {status}); the Mac result is not used\n")
                self.assertEqual(result.stderr, expected)

    def test_verify_helper_loads_the_top_level_main(self) -> None:
        self.write("mac=passed\n")
        program = verify_helper.load_program(str(Path(runner_result.__file__)))
        self.assertIsInstance(program, verify_helper.LoadedProgram)
        call = verify_helper.HelperCall(str(Path(runner_result.__file__)), str(self.root),
                                        [str(self.path), "0", "1"], dict(os.environ), "", [])
        self.assertEqual(verify_helper.run_call(call, {call.program: program}),
                         ("0", "passed\n\n\n\nfailed\n\n\n", ""))
        self.path.unlink()
        self.assertEqual(verify_helper.run_call(call, {call.program: program}), ("1", "", ""))


if __name__ == "__main__":
    _ = unittest.main()
