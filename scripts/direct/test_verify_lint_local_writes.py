"""Keep lint repairs in the original tree while natedev has a hold."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast, override
import unittest

from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture


class VerifyLintLocalWritesTests(VerifyStepRoutesFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.fixture.environment["PYTHONPYCACHEPREFIX"] = str(self.fixture.root.parent / "bytecode")
        _ = self.fixture.environment.pop("PYTHONDONTWRITEBYTECODE", None)

    def hold_before_repair(self, tool: str) -> Path:
        self.fixture.environment["TEST_REQUIRE_CLIPPY_FIX"] = "1" if tool == "clippy" else "0"
        self.fixture.environment["TEST_REQUIRE_MEND_FIX"] = "1" if tool == "mend" else "0"
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink(missing_ok=True)
        invoke = self.fixture.verify_script.parent.parent / "lint/invoke.sh"
        function = "invoke_mend() {" if tool == "mend" else "invoke_clippy_fix() {"
        hold = 'printf held > "$BUILD_HOLD_DIR/other"'
        if tool == "mend":
            hold = f'if [[ " $* " == *" --fix "* ]]; then {hold}; fi'
        original = invoke.read_text()
        self.assertIn(function, original)
        _ = invoke.write_text(original.replace(function, f"{function}\n    {hold}", 1))
        cargo = self.fixture.bin_directory / "cargo"
        source = cargo.read_text().replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
repair = Path(os.environ["PACKAGE_ROOT"]) / "local-repair"
if arguments[:1] == ["''' + tool + '''"]:
    if "--fix" in arguments:
        repair.write_text("fixed locally")
        repair.with_suffix(".hooks").write_text(json.dumps({
            name: os.environ.get(name) for name in ("LINT_QUEUE_LEAVE_HOOK", "BUILDLOG_MAC_IDLE_HOOK")
        }))
    elif "''' + tool + '''" == "clippy" and not repair.exists():
        raise SystemExit(101)
''')
        _ = cargo.write_text(source)
        return self.fixture.root / "local-repair"

    def assert_local_repair(self, tool: str, selection: tuple[str, ...] = ()) -> None:
        repair = self.hold_before_repair(tool)
        repair.unlink(missing_ok=True)
        self.fixture.local_waits.clear()
        result = self.run_verify("lint", "sample", "--no-cache", *selection)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(repair.exists(), result.stdout + result.stderr)
        self.assertEqual(repair.read_text(), "fixed locally")
        self.assertEqual(cast(dict[str, str], json.loads(repair.with_suffix(".hooks").read_text())), {
            "LINT_QUEUE_LEAVE_HOOK": "false", "BUILDLOG_MAC_IDLE_HOOK": "false",
        })
        self.assertTrue(self.fixture.local_waits)
        calls = [event["args"] for event in self.fixture.cargo_events()]
        self.assertTrue(any(words[:1] == [tool] and "--fix" in words for words in calls), calls)
        self.assertTrue(any("fmt" in words for words in calls), calls)
        for row in self.mac_rows():
            self.assertNotIn("--fix", cast(list[str], row["args"]))
        self.assert_removed()

    def test_mend_fix_waits_for_natedev_and_changes_the_local_tree(self) -> None:
        self.assert_local_repair("mend")

    def test_clippy_fix_waits_for_natedev_and_changes_the_local_tree(self) -> None:
        self.assert_local_repair("clippy")

    def test_a_mac_pin_keeps_mend_tree_writes_on_natedev(self) -> None:
        for selection in (("--target", "mac"), ("--mac", "test local writes")):
            with self.subTest(selection=selection):
                self.assert_local_repair("mend", selection)

    def test_a_mac_pin_keeps_clippy_tree_writes_on_natedev(self) -> None:
        for selection in (("--target", "mac"), ("--mac", "test local writes")):
            with self.subTest(selection=selection):
                self.assert_local_repair("clippy", selection)


if __name__ == "__main__":
    _ = unittest.main()
