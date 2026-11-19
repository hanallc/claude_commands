"""Real verify step routing: lint."""
from __future__ import annotations

from pathlib import Path
import unittest
from typing import cast
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_service as service, placement
from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture


class VerifyStepRoutesTests(VerifyStepRoutesFixture):
    def test_lint_formats_locally_and_judges_on_mac_without_fix_passes(self) -> None:
        result = self.run_verify("lint", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.mac_rows()
        verbs: list[str] = []
        for row in rows:
            words = cast(list[str], row["args"])
            verbs.append(words[words.index("--verb") + 1])
            self.assertNotIn("--fix", words)
        self.assertEqual(verbs, ["mend-check", "clippy"])
        local = [event["args"] for event in self.fixture.cargo_events() if event["args"][:1] != ["metadata"]]
        self.assertTrue(any("fmt" in words for words in local), local)
        self.assertFalse(any("mend" in words or "clippy" in words for words in local), local)
        self.assert_removed()

    def test_refused_internal_mac_pin_returns_to_lint(self) -> None:
        holder = Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other"
        holder.unlink()
        reader = Path(self.fixture.environment["LINT_CONFIG_READER"])
        with reader.open("a") as stream:
            _ = stream.write("""
lint_config_enabled() {
    if [[ "$1" == mend && -z "${TEST_MEND_HOLD_STARTED:-}" ]]; then
        TEST_MEND_HOLD_STARTED=1
        printf 'test hold\n' > "$BUILD_HOLD_DIR/other"
    fi
    return 0
}
""")
        admission = caller_admission(self.fixture, self.fixture.root, self.fixture.environment)
        previous = admission.server.handle_request
        refused: list[placement.HostSelection] = []
        reason = "The fixture refused the held lint pass"

        def refuse_judgment(request: wire.Request,
                            connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                            ) -> list[wire.Reply]:
            if isinstance(request, wire.PlacementRequest) and "--fail-on-warn" in request.constraints.command:
                refused.append(request.constraints.selection)
                holder.unlink()
                return [wire.DispatchStatus(placement.UnknownAssignment(request.episode, ""),
                    placement.ClosedAssignment(reason), placement.NoLaunchPermission(reason))]
            return previous(request, connection)

        with mock.patch.object(admission.server, "handle_request", side_effect=refuse_judgment):
            result = self.run_verify("lint", "sample", "--no-cache")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(refused, [placement.PinnedHost("mac")])
        self.assertNotIn(reason, output)
        local = [event["args"] for event in self.fixture.cargo_events()]
        self.assertTrue(any(args[:1] == ["mend"] and "--fix" in args for args in local), local)
        self.assert_removed()

    def test_new_hold_during_formatting_keeps_noncompiling_work_protected(self) -> None:
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        cargo = self.fixture.bin_directory / "cargo"
        source = cargo.read_text().replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
if "fmt" in arguments:
    with open(Path.home() / ".claude/scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
    from scripts.direct.step_row_test_support import read_step_row
    record = read_step_row(os.environ["BUILDLOG_CALL_ID"])
    (Path(os.environ["PACKAGE_ROOT"]) / "fmt-state").write_text(record["state"])
    (Path(os.environ["BUILD_HOLD_DIR"]) / "other").write_text("new hold")
''')
        _ = cargo.write_text(source)
        self.fixture.write_executable(self.fixture.bin_directory / "taplo", '''
import os
from pathlib import Path
with open(Path.home() / ".claude/scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.direct.step_row_test_support import read_step_row
record = read_step_row(os.environ["BUILDLOG_CALL_ID"])
(Path(os.environ["PACKAGE_ROOT"]) / "taplo-state").write_text(record["state"])
''')
        result = self.run_verify("lint", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.fixture.root / "fmt-state").read_text(), "local_noncompiling")
        self.assertEqual((self.fixture.root / "taplo-state").read_text(), "local_noncompiling")
        self.assertTrue(self.mac_rows())
        self.assert_removed()

if __name__ == "__main__":
    _ = unittest.main()
