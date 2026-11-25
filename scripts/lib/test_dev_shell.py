#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""A test step outside a nix shell gets the flake shell's programs last on PATH, in temporary directories only."""

from __future__ import annotations

import os
import shlex
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

    def cache_workspace(self) -> Path:
        workspace = self.root / "workspace"
        (workspace / "nix").mkdir()
        _ = (workspace / "nix/shell.nix").write_text("{}\n")
        (workspace / "src").mkdir()
        (workspace / "target").mkdir()
        _ = (workspace / "private.conf").write_text("private fixture\n")
        _ = (workspace / "target/large-build").write_text("build fixture\n")
        _ = (self.root / "bin/nix").write_text(
            "#!/usr/bin/env bash\n"
            + "printf '%s\\n' \"$*\" >> \"$NIX_CALLS\"\n"
            + '[[ -z "$NIX_FAILS" ]] || exit 1\n'
            + "printf '%s\\n' 'export TEST_SHELL_PROOF=loaded'\n")
        return workspace

    def prepare_cached_shell(self, workspace: Path, selector: str = "ci", **extra: str) -> dev_shell.ShellEnvironment:
        with mock.patch.dict(os.environ, {**self.environment, "NIX_FAILS": "", **extra}, clear=True):
            return dev_shell.prepare_shell(workspace, self.root / "cache", selector, 3)

    def shell_calls(self) -> list[str]:
        return (self.root / "calls").read_text().splitlines()

    def use_cached_shell(self, workspace: Path, shell: dev_shell.ShellEnvironment) -> tuple[str, str, str]:
        script = 'source "$1"\nwarm=no\nif [[ -f target/warm-proof ]]; then warm=yes; fi\n'
        script += 'printf "warm\\n" > target/warm-proof\nprintf "%s\\n" "$TEST_SHELL_PROOF" "$warm" "$PWD"\n'
        result = subprocess.run(["bash", "-c", script, "shell-test", str(shell.exports)],
                                cwd=workspace, capture_output=True, text=True, check=True)
        proof, warm, cwd = result.stdout.splitlines()
        return proof, warm, cwd

    def test_shell_inputs_exclude_source_build_files_and_private_files(self) -> None:
        workspace = self.cache_workspace()
        _ = (workspace / "src/main.rs").write_text("fn main() {}\n")
        shell = self.prepare_cached_shell(workspace)

        calls = self.shell_calls()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith(f"path:{shell.inputs}#ci"))
        inputs = sorted(path.relative_to(shell.inputs).as_posix() for path in shell.inputs.rglob("*") if path.is_file())
        self.assertEqual(inputs, ["flake.lock", "flake.nix", "nix/shell.nix"])
        self.assertEqual(self.use_cached_shell(workspace, shell)[0], "loaded")

    def test_unchanged_inputs_reuse_exports_and_the_build_mirror(self) -> None:
        workspace = self.cache_workspace()
        first = self.prepare_cached_shell(workspace)
        _ = self.use_cached_shell(workspace, first)
        _ = (workspace / "src/main.rs").write_text('fn main() { println!("changed"); }\n')

        _, warm, cwd = self.use_cached_shell(workspace, self.prepare_cached_shell(workspace))

        self.assertEqual(len(self.shell_calls()), 1)
        self.assertEqual(warm, "yes")
        self.assertEqual(cwd, str(workspace))
        self.assertTrue((workspace / "target/warm-proof").is_file())

    def test_shell_content_and_path_changes_invalidate_exports(self) -> None:
        workspace = self.cache_workspace()
        _ = self.prepare_cached_shell(workspace)
        _ = (workspace / "nix/shell.nix").write_text("{ changed = true; }\n")
        _ = self.prepare_cached_shell(workspace)
        _ = (workspace / "nix/shell.nix").rename(workspace / "nix/renamed.nix")
        _ = self.prepare_cached_shell(workspace)

        self.assertEqual(len(self.shell_calls()), 3)

    def test_shell_selector_change_invalidates_exports(self) -> None:
        workspace = self.cache_workspace()
        _ = self.prepare_cached_shell(workspace)
        _ = self.prepare_cached_shell(workspace, "other")

        calls = self.shell_calls()
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[-1].endswith("#other"))

    def test_each_top_level_flake_input_change_invalidates_exports(self) -> None:
        workspace = self.cache_workspace()
        _ = self.prepare_cached_shell(workspace)
        for count, name in enumerate(("flake.nix", "flake.lock"), start=2):
            with self.subTest(name=name):
                path = workspace / name
                _ = path.write_text(path.read_text() + "\n")
                _ = self.prepare_cached_shell(workspace)
                self.assertEqual(len(self.shell_calls()), count)

    def test_partial_cache_directory_requires_a_new_evaluation(self) -> None:
        workspace = self.cache_workspace()
        key = dev_shell.shell_key(workspace, "ci")
        partial = self.root / "cache" / key / "inputs"
        for name in ("flake.nix", "flake.lock", "nix/shell.nix"):
            path = partial / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text((workspace / name).read_text())

        shell = self.prepare_cached_shell(workspace)

        self.assertEqual(len(self.shell_calls()), 1)
        self.assertEqual(self.use_cached_shell(workspace, shell)[0], "loaded")

    def test_missing_store_path_in_cached_exports_requires_a_new_evaluation(self) -> None:
        workspace = self.cache_workspace()
        first = self.prepare_cached_shell(workspace)
        missing = "/nix/store/00000000000000000000000000000000-fixture-collected-shell/bin"
        self.assertFalse(Path(missing).exists())
        _ = first.exports.write_text(f"export PATH={shlex.quote(missing)}:$PATH\nexport TEST_SHELL_PROOF=stale\n")

        shell = self.prepare_cached_shell(workspace)

        self.assertEqual(len(self.shell_calls()), 2)
        self.assertEqual(self.use_cached_shell(workspace, shell)[0], "loaded")

    def test_failed_evaluation_never_publishes_usable_exports(self) -> None:
        workspace = self.cache_workspace()
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            shell = self.prepare_cached_shell(workspace, NIX_FAILS="1")
            _ = self.use_cached_shell(workspace, shell)
        self.assertFalse((workspace / "target/warm-proof").exists(), str(failure.exception))
        self.assertNotIn("mac_exit=0\n", str(failure.exception))
        self.assertEqual(list((self.root / "cache").rglob("exports.sh")), [])

        shell = self.prepare_cached_shell(workspace)
        self.assertEqual(self.use_cached_shell(workspace, shell)[0], "loaded")
        self.assertEqual(len(self.shell_calls()), 2)

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
