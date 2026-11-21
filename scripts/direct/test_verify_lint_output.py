"""Check shell invocation output and failures of tee and lint configuration."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from typing import final
import unittest

from scripts.direct.verify_lint_scope_test_support import SCRIPTS, VERIFY
from scripts.lib.shell_test_support import shell_function
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import test_invoke_scope


@final
class InvokeOutputTests(unittest.TestCase):
    def function_body(self, name: str) -> str:
        source = (SCRIPTS / "lint" / "invoke.sh").read_text(encoding="utf-8")
        return shell_function(source, name)

    def shell(self, script: str, root: Path) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ, HOME=str(root), TMPDIR=str(root))
        return subprocess.run(
            ["bash", "-c", script], cwd=root, env=environment,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def command_stubs(self) -> str:
        return r'''
set -euo pipefail
SANDBOX_SIGNATURE=not-a-sandbox-failure
buildlog_step_compiles() { return 1; }
buildlog_begin() { :; }
buildlog_check_nested_step() { :; }
buildlog_exec() { "$@"; }
buildlog_finish_memory() { :; }
buildlog_end() { :; }
sweep_after_step() { :; }
'''

    def test_shell_libraries_use_no_named_descriptor_redirections(self) -> None:
        for path in (SCRIPTS / "lint" / "invoke.sh", VERIFY):
            with self.subTest(path=path):
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"\{[A-Za-z_][A-Za-z_0-9]*\}[<>]")

    def test_ordinary_step_returns_command_status_when_tee_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            tee = binary / "tee"
            _ = tee.write_text("#!/bin/sh\ncat\nexit 7\n", encoding="utf-8")
            tee.chmod(0o755)
            for status in (0, 47):
                with self.subTest(status=status):
                    script = self.command_stubs() + "\n".join(self.function_body(name) for name in (
                        "buildlog_retry_unstarted_step", "without_progress_frames", "buildlog_exec_with_progress",
                        "run_step_attempt", "run_once"))
                    script += f'\nexport PATH="$PWD/bin:$PATH"\nif run_once bash -c "exit {status}"; then result=0; else result=$?; fi\nexit "$result"\n'
                    result = self.shell(script, root)
                    self.assertEqual(result.returncode, status, result.stdout + result.stderr)

    def test_inherited_artifact_path_does_not_capture_ordinary_output(self) -> None:
        script = r"""
buildlog_exec_with_progress printf 'ordinary\n'
capture_artifacts() {
    local BUILD_TEST_ARTIFACTS_FILE="$PWD/captured"
    buildlog_exec_with_progress printf 'artifact\n'
}
capture_artifacts
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inherited = root / "inherited"
            _ = inherited.write_text("original\n", encoding="utf-8")
            result = test_invoke_scope.BuildAdmissionTests.run_invoke(
                root, script, [], cwd=root, environment={
                    "BUILD_TEST_ARTIFACTS_FILE": str(inherited),
                    "BUILDLOG_MEM_RESERVATION": "",
                    "BUILDLOG_PEAK": "",
                    "BUILDLOG_RESERVED_STEP": "",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(inherited.read_text(encoding="utf-8"), "original\n")
            self.assertEqual(result.stdout, "ordinary\n")
            self.assertEqual((root / "captured").read_text(encoding="utf-8"), "artifact\n")

    def test_inherited_probe_flag_does_not_skip_formatting(self) -> None:
        script = r"""
LINT_QUEUE_PLACEMENT=0
VERIFY_LINT_RUSTFMT_VERSION_FILE=""
lint_config_enabled() { return 0; }
cargo() {
    if [[ "$*" == '+nightly fmt --version' ]]; then
        printf 'probe\n' >> probes
        return 0
    fi
    printf '%s\n' "$*"
    return 47
}
run() { "$@"; }
"""
        for command in ("fmt_cargo --all", "validation_cargo_step fmt"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = test_invoke_scope.BuildAdmissionTests.run_invoke(
                    root, script + "\n" + command, [], cwd=root,
                    environment={"LINT_RUSTFMT_CHECK_ONLY": "1"},
                )
                self.assertEqual(result.returncode, 47, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "+nightly fmt --all\n")
                self.assertEqual((root / "probes").read_text(encoding="utf-8"), "probe\n")

    def test_clippy_fix_preserves_extra_lints_and_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = "set -euo pipefail\nCLIPPY_FIX_JOBS=8\n"
            script += self.function_body("invoke_clippy") + "\n"
            script += self.function_body("invoke_clippy_fix") + r'''
lint_config_enabled() { return 0; }
run() { printf '%s\n' "$@"; return 47; }
invoke_clippy_fix -p sample --lib -- -A clippy::type_complexity
'''
            result = self.shell(script, root)
            self.assertEqual(result.returncode, 47, result.stdout + result.stderr)
            self.assertEqual(result.stdout.splitlines(), [
                "env", "-u", "CARGO_MAKEFLAGS", "-u", "MAKEFLAGS", "-u", "MFLAGS",
                "cargo", "clippy", "--jobs", "8", "--fix", "--allow-dirty", "--allow-staged", "--allow-no-vcs",
                "-p", "sample", "--lib", "--", "-D", "warnings", "-A", "clippy::type_complexity",
            ])

    def test_clippy_fix_off_prints_skip_notice_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = "set -euo pipefail\n"
            script += self.function_body("invoke_clippy") + "\n"
            script += self.function_body("invoke_clippy_fix") + r'''
lint_config_enabled() { return 1; }
lint_config_skip_notice() { printf 'SKIPPED: %s\n' "$2"; }
run() { printf 'unexpected run\n'; return 99; }
invoke_clippy_fix --workspace --all-targets --all-features --keep-going
'''
            result = self.shell(script, root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.splitlines(), [
                "SKIPPED: cargo clippy --fix --allow-dirty --allow-staged --allow-no-vcs"
                + " --workspace --all-targets --all-features --keep-going",
            ])



if __name__ == "__main__":
    _ = unittest.main()
