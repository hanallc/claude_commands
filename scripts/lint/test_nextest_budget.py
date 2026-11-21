#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""A nextest run with no jobserver pool reports its test threads and the load, in temporary directories only."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

from scripts.build_hold.override_guard import require_checkout

require_checkout(Path(__file__))


INVOKE = Path(__file__).with_name("invoke.sh")


class ShellTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def run_nextest(self, mode: str, *arguments: str, logical_cores: int = 32, shell_setup: str = "") -> str:
        config_reader = self.root / "lint_config.sh"
        _ = config_reader.write_text("lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n")
        script = f"""
set -euo pipefail
source "$1"
shift
require_nextest() {{ :; }}
# Keep the real dispatch. Replace only admission and command execution.
buildlog_step_compiles() {{ return 1; }}
buildlog_finish_memory() {{ :; }}
buildlog_exec() {{
    printf '<%s>' "$@"
    printf '\\n'
}}
# Bash ignores assignments to EPOCHSECONDS, and core counts come from the
# machine: fix the trial arm and both widths so the case never reads either.
nextest_width_arm() {{ LINT_TEST_WIDTH_ARM=logical; }}
nextest_logical_cores() {{ LINT_LOGICAL_CORES={logical_cores}; }}
nextest_physical_cores() {{ LINT_PHYSICAL_CORES=16; }}
LINT_TEST_WIDTH={mode}
{shell_setup}
run_nextest "$@"
"""
        environment = {
            **os.environ,
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            # No jobserver pool: the run takes no tokens.
            "CARGO_MAKEFLAGS": f"--jobserver-auth=fifo:{self.root / 'no-pool'}",
            "BUILDLOG_OFF": "1",
            "BUILDLOG_CALL_ID": "",
            "BUILDLOG_CALLER": "",
            "BUILDLOG_SCOPE": "0",
            "LINT_SKIP_SWEEP": "1",
            "TMPDIR": str(self.root),
            # Every lint step waits for natedev's hold: a real hold must not stop the case.
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "LINT_CONFIG_READER": str(config_reader),
        }
        result = subprocess.run(
            ["bash", "-c", script, "nextest-budget-test", str(INVOKE), *arguments],
            capture_output=True, text=True, check=False, env=environment, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout.partition("\n")[2]

    def test_run_takes_the_mode_width(self) -> None:
        for width in (12, 32):
            with self.subTest(width=width):
                output = self.run_nextest("logical", "--workspace", logical_cores=width)
                self.assertRegex(output,
                    rf"\Anextest: {width} of {width} test threads; load \d+\.\d{{2}} on [1-9]\d* CPUs "
                    + rf"\([^()\n]+\)\n<cargo><nextest><run><--test-threads><{width}><--workspace>\n\Z")

    def test_caller_width_is_kept(self) -> None:
        cases = ((("--test-threads", "3"), 3), (("-j4",), 4), (("--jobs", "5"), 5), (("--jobs=5",), 5))
        for arguments, width in cases:
            with self.subTest(arguments=arguments):
                output = self.run_nextest("logical", *arguments, shell_setup="export NEXTEST_TEST_THREADS=3")
                line, output = output.split("\n", 1)
                self.assertRegex(line,
                    rf"\Anextest: {width} of {width} test threads; load \d+\.\d{{2}} on [1-9]\d* CPUs \([^()\n]+\)\Z")
                rendered = "".join(f"<{argument}>" for argument in arguments)
                self.assertEqual(output, f"<cargo><nextest><run>{rendered}\n")

    def test_trial_mode_keeps_the_arm_width_for_width_trial(self) -> None:
        output = self.run_nextest("trial", "--workspace")
        line, output = output.split("\n", 1)
        self.assertRegex(line,
            r"\Anextest: 32 of 32 test threads; load \d+\.\d{2} on [1-9]\d* CPUs \([^()\n]+\)\Z")
        self.assertEqual(output, "<cargo><nextest><run><--test-threads><32><--workspace>\n")

    def test_unreadable_load_is_unknown_and_the_run_still_starts(self) -> None:
        setup = """buildlog_memory_python() {
    python3 -c 'import runpy, sys; from unittest.mock import patch
script = sys.argv.pop(1)
with patch("os.getloadavg", side_effect=OSError("load is unavailable")):
    runpy.run_path(script, run_name="__main__")' "$@"
} """
        output = self.run_nextest("logical", "--workspace", logical_cores=12, shell_setup=setup)
        self.assertRegex(output,
            r"\Anextest: 12 of 12 test threads; load unknown on [1-9]\d* CPUs "
            + r"\([^()\n]+\)\n<cargo><nextest><run><--test-threads><12><--workspace>\n\Z")


if __name__ == "__main__":
    _ = unittest.main()
