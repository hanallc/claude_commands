#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""A test step outside a nix shell gets the flake shell's programs last on PATH, in temporary directories only."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import final, override

from scripts.build_hold.override_guard import require_checkout
from scripts.lib import dev_shell
from scripts.lib.shell_test_support import shell_functions

require_checkout(Path(__file__))

DEV_SHELL = Path(__file__).with_name("dev_shell.py")
INVOKE = Path(__file__).parents[1] / "lint/invoke.sh"
STORE = "/nix/store/" + "a" * 32
# The fake nix records each call and prints exports in print-dev-env's form, or fails when told to.
FAKE_NIX = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$NIX_CALLS"
[[ -z "${NIX_FAILS:-}" ]] || { echo 'error: flake has no devShells.default' >&2; exit 1; }
printf '%s\n' 'PATH=${PATH:-}' "HOST_PATH='$STORE-host/bin'" 'export HOST_PATH' \
    "PATH='$STORE-spacetimedb/bin:/path-not-set:$STORE-pkg-config/bin'" 'export PATH'
"""


@final
class DevShellTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        workspace = self.root / "workspace"
        workspace.mkdir()
        for name in ("flake.nix", "flake.lock", "Cargo.toml"):
            _ = (workspace / name).write_text(name + "\n")
        folder = self.root / "bin"
        folder.mkdir()
        nix = folder / "nix"
        _ = nix.write_text(FAKE_NIX)
        nix.chmod(0o755)
        self.environment = {**os.environ, "HOME": str(self.root / "home"), "STORE": STORE,
                            "PATH": f"{folder}:{os.environ['PATH']}", "NIX_CALLS": str(self.root / "calls")}

    def program_path(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(DEV_SHELL), "path", "--root", str(self.root / "workspace")],
                              capture_output=True, text=True, env={**self.environment, **extra}, check=False)

    def test_program_folders_are_the_store_folders_of_the_shell_path(self) -> None:
        result = self.program_path()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{STORE}-spacetimedb/bin:{STORE}-pkg-config/bin\n")
        call = (self.root / "calls").read_text()
        self.assertTrue(call.startswith("print-dev-env path:") and call.endswith("#default\n"), call)

    def test_failed_evaluation_names_its_cause_in_one_line(self) -> None:
        result = self.program_path(NIX_FAILS="1")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, f"dev_shell: {self.root / 'workspace'}: nix print-dev-env failed: "
                         + "error: flake has no devShells.default\n")
        # A flake outside the input contract fails before nix runs.
        (self.root / "workspace/flake.lock").unlink()
        result = self.program_path()
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr.count("\n"), 1, result.stderr)
        self.assertIn("missing shell input", result.stderr)

    def test_exports_without_a_path_give_no_folders(self) -> None:
        exports = self.root / "exports.sh"
        _ = exports.write_text("PATH=${PATH:-}\nexport FOO='1'\n")
        shell = dev_shell.ShellEnvironment("key", exports, self.root)
        self.assertEqual(dev_shell.program_folders(shell), "")

    def test_nix_receives_the_explicit_descriptors(self) -> None:
        result = subprocess.CompletedProcess(['nix'], 0, "export TEST_SHELL=1\n", "")
        with tempfile.TemporaryFile() as lease, mock.patch.object(subprocess, 'run', return_value=result) as evaluate:
            descriptors = (lease.fileno(),)
            _ = dev_shell.prepare_shell(self.root / 'workspace', self.root / 'cache', 'default', 3,
                                        pass_fds=descriptors)
        self.assertEqual(evaluate.call_args.kwargs['pass_fds'], descriptors)

    def test_nix_receives_no_descriptors_by_default(self) -> None:
        result = subprocess.CompletedProcess(['nix'], 0, "export TEST_SHELL=1\n", "")
        with mock.patch.object(subprocess, 'run', return_value=result) as evaluate:
            _ = dev_shell.prepare_shell(self.root / 'workspace', self.root / 'cache', 'default', 3)
        self.assertEqual(evaluate.call_args.kwargs['pass_fds'], ())

    def test_invoke_adds_the_folders_last_and_only_outside_a_nix_shell(self) -> None:
        library = self.root / "lib"
        library.mkdir()
        fake_py = library / "py"
        # The fake helper proves the arguments and prints one folder, or fails when told to.
        expected = f"path --root {self.root / 'workspace'}"
        _ = fake_py.write_text(f'#!{shutil.which("bash")}\n[[ "$2 $3 $4" == "{expected}" ]] || exit 9\n'
                               + '[[ -z "${HELPER_FAILS:-}" ]] || exit 1\necho /nix/store/x-tools/bin\n')
        fake_py.chmod(0o755)
        script = (shell_functions(INVOKE, "lint_dev_shell_path")
                  + f"\nBUILD_FOLDERS_SCRIPT={library}/build_folders.py\nPATH=/host/bin:{library}\n"
                  + 'lint_dev_shell_path; status=$?; printf "%s %s\\n" "$status" "$PATH"\n')
        rows: tuple[tuple[dict[str, str], str, str], ...] = (
            ({}, f"0 /host/bin:{library}:/nix/store/x-tools/bin\n", ""),
            ({"IN_NIX_SHELL": "impure"}, f"0 /host/bin:{library}\n", ""),
            ({"HELPER_FAILS": "1"}, f"1 /host/bin:{library}\n", ""),
        )
        for extra, stdout, stderr in rows:
            with self.subTest(extra=extra):
                environment = {name: value for name, value in os.environ.items() if name != "IN_NIX_SHELL"}
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False,
                                        cwd=self.root / "workspace", env={**environment, **extra})
                self.assertEqual((result.stdout, result.stderr), (stdout, stderr))
        (self.root / "workspace/flake.nix").unlink()
        # No flake: the empty PATH proves the check starts no process.
        result = subprocess.run(["bash", "-c", script.replace(f"PATH=/host/bin:{library}", "PATH=/nonexistent")],
                                capture_output=True, text=True, check=False, cwd=self.root / "workspace")
        self.assertEqual((result.stdout, result.stderr), ("0 /nonexistent\n", ""))


if __name__ == "__main__":
    _ = unittest.main()
