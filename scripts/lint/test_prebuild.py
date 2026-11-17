#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""A declared prebuild builds before the tests that need it, in temporary directories only."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final, override

from scripts.build_hold.override_guard import require_checkout
from scripts.lib.shell_test_support import shell_functions
from scripts.lint import prebuild
from scripts.lint.prebuild import DeclarationError, SelectedPackages, WholeWorkspace

require_checkout(Path(__file__))

PREBUILD = Path(__file__).with_name("prebuild.py")
INVOKE = Path(__file__).with_name("invoke.sh")
DECLARATION = """[workspace]
members = []

[workspace.metadata.dev.prebuild.agora-server]
target = "wasm32-unknown-unknown"
env = "AGORA_SERVER_WASM"
for = ["agora", "hana"]
"""
# The fake cargo records each call. Its build prints one artifact message, or fails when told to.
FAKE_CARGO = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CARGO_CALLS"
if [[ "$1" == build ]]; then
    [[ -z "${CARGO_BUILD_FAILS:-}" ]] || { echo 'error: could not compile' >&2; exit 101; }
    echo '{"reason":"compiler-artifact","target":{"name":"serde","kind":["lib"]},"filenames":["/t/libserde.rlib"],"executable":null}'
    echo '{"reason":"compiler-artifact","target":{"name":"agora_server","kind":["cdylib"]},"filenames":["/t/wasm32-unknown-unknown/release/agora_server.wasm"],"executable":null}'
    exit 0
fi
printf 'tests see %s\n' "${AGORA_SERVER_WASM:-nothing}"
"""


@final
class SelectionTests(unittest.TestCase):
    def test_selection_rows(self) -> None:
        rows: tuple[tuple[tuple[str, ...], frozenset[str] | None], ...] = (
            (("--workspace", "--lib"), None),
            (("--workspace", "-E", "package(hana)"), frozenset({"hana"})),
            (("--workspace", "-E", "(package(hana) & test(x)) & not (test(slow))"), frozenset({"hana"})),
            (("--workspace", "-E", "not (package(agora))"), None),
            (("--workspace", "-E", "all() - package(agora)"), None),
            (("--workspace", "-E", "package(hana*)"), None),
            (("--workspace", "--filterset=package(=agora)"), frozenset({"agora"})),
            (("-p", "agora", "-E", "test(x)"), frozenset({"agora"})),
            (("--workspace", "--", "-E", "package(hana)"), None),
        )
        for words, expected in rows:
            with self.subTest(words=words):
                selection = prebuild.test_selection(words)
                if expected is None:
                    self.assertIsInstance(selection, WholeWorkspace)
                else:
                    self.assertEqual(selection, SelectedPackages(expected))

    def test_declaration_errors(self) -> None:
        rows = (
            ('target = "w"\nenv = "A"\nfor = ["x"]\nprofile = "dev"\n', "unknown key profile"),
            ('target = "w"\nenv = "1A"\nfor = ["x"]\n', "must be a variable name"),
            ('target = "w"\nenv = "A"\nfor = []\n', "non-empty list"),
            ('env = "A"\nfor = ["x"]\n', ".target must be a non-empty string"),
            ('target = "w"\nenv = "A"\nfor = ["x"]\nplatforms = ["windows"]\n', "list of linux and macos"),
        )
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "Cargo.toml"
            for body, message in rows:
                with self.subTest(message=message):
                    _ = manifest.write_text("[workspace.metadata.dev.prebuild.p]\n" + body)
                    with self.assertRaisesRegex(DeclarationError, message):
                        _ = prebuild.read_prebuilds(manifest)


@final
class RunTests(unittest.TestCase):
    root: Path = Path()
    manifest: Path = Path()
    calls: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.manifest = self.root / "Cargo.toml"
        _ = self.manifest.write_text(DECLARATION)
        folder = self.root / "bin"
        folder.mkdir()
        cargo = folder / "cargo"
        _ = cargo.write_text(FAKE_CARGO)
        cargo.chmod(0o755)
        self.calls = self.root / "calls"
        self.environment = {**os.environ, "PATH": f"{folder}:{os.environ['PATH']}", "CARGO_CALLS": str(self.calls)}

    def run_tests(self, *words: str, failing: bool = False) -> subprocess.CompletedProcess[str]:
        environment = {**self.environment, **({"CARGO_BUILD_FAILS": "1"} if failing else {})}
        return subprocess.run(
            [sys.executable, str(PREBUILD), "--manifest", str(self.manifest), "--", "cargo", "nextest", "run", *words],
            capture_output=True, text=True, env=environment, check=False)

    def test_selected_package_builds_first_and_tests_see_the_path(self) -> None:
        result = self.run_tests("--workspace", "-E", "package(hana)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "tests see /t/wasm32-unknown-unknown/release/agora_server.wasm\n")
        calls = self.calls.read_text().splitlines()
        self.assertEqual(calls[0], f"build --release --manifest-path {self.manifest} -p agora-server "
                         + "--target wasm32-unknown-unknown --message-format=json-render-diagnostics")
        self.assertEqual(calls[1], "nextest run --workspace -E package(hana)")

    def test_other_package_runs_tests_without_a_build(self) -> None:
        result = self.run_tests("--workspace", "-E", "package(other)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "tests see nothing\n")
        self.assertEqual(self.calls.read_text().splitlines(), ["nextest run --workspace -E package(other)"])

    def test_platform_outside_the_list_runs_tests_without_a_build(self) -> None:
        other = "linux" if prebuild.host_platform() == "macos" else "macos"
        _ = self.manifest.write_text(DECLARATION + f'platforms = ["{other}"]\n')
        result = self.run_tests("--workspace", "-E", "package(hana)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "tests see nothing\n")
        self.assertIn(f"agora-server builds only on {other}", result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), ["nextest run --workspace -E package(hana)"])

    def test_failed_build_stops_before_the_tests(self) -> None:
        result = self.run_tests("--workspace", failing=True)
        self.assertEqual(result.returncode, 101)
        self.assertIn("error: could not compile", result.stderr)
        self.assertIn("the tests did not run", result.stderr)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_invoke_puts_the_prefix_only_on_a_declaring_workspace(self) -> None:
        script = shell_functions(INVOKE, "lint_prebuild_prefix") + '\nlint_prebuild_prefix\nprintf "%s\\n" "${LINT_PREBUILD_PREFIX[@]}"\n'
        for body, expected in ((DECLARATION, f"{PREBUILD}\n--manifest\n{self.manifest}\n--\n"), ("[workspace]\n", "\n")):
            with self.subTest(declares=body != "[workspace]\n"):
                _ = self.manifest.write_text(body)
                result = subprocess.run(
                    ["bash", "-c", f"BUILD_FOLDERS_SCRIPT=/lib/build_folders.py; LINT_PREBUILD_SCRIPT={PREBUILD}\n" + script],
                    capture_output=True, text=True, check=False, cwd=self.root)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "/lib/py\n" + expected if body == DECLARATION else expected)


if __name__ == "__main__":
    _ = unittest.main()
