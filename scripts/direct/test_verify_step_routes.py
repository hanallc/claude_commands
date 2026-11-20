"""Real verify step routing: admission."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest
from typing import cast
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_records as records, admission_service as service
from scripts.build_queue.admission_test_support import caller_admission
from scripts.build_targets import target_config

from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture, TARGETS as TARGETS, RUNNER as RUNNER
from scripts.lib.shell_test_support import shell_functions


class VerifyCallShellTests(unittest.TestCase):
    def run_shell(self, script: str) -> str:
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_clippy_repair_uses_the_clippy_position(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        script = shell_functions(scripts / "direct" / "verify.sh", "run_step")
        script += shell_functions(scripts / "lint" / "invoke.sh", "invoke_clippy", "invoke_clippy_fix")
        script += r'''
set -eu
CMD=lint
VERIFY_STEP_COUNT=2
BUILDLOG_FIRST_STEP_REQUEST=fixture
CLIPPY_FIX_JOBS=8
lint_config_enabled() { return 0; }
buildlog_step_compiles() { return 0; }
step_writes_tree() { return 0; }
route_compiling_step() { printf '%s of %s\n' "$BUILDLOG_VERIFY_POSITION" "$BUILDLOG_VERIFY_TOTAL"; }
run() { (run_step "$@"); }
invoke_clippy -p sample
invoke_clippy_fix -p sample
'''
        self.assertEqual(self.run_shell(script).splitlines(), ["2 of 2", "2 of 2"])

    def test_verify_call_omits_the_worker_session_directory(self) -> None:
        script = shell_functions(Path(__file__).with_name("verify.sh"), "format_verify_call")
        script += r'''
set -eu
format_verify_call --session-dir '/private/worker session' final sample --local 'creates a Linux install'
printf '%s\n' "$VERIFY_CALL_TEXT"
'''
        self.assertEqual(self.run_shell(script).strip(), "verify final sample --local 'creates a Linux install'")


class VerifyStepRoutesTests(VerifyStepRoutesFixture):
    def test_check_uses_mac_during_hold_and_removes_record(self) -> None:
        result = self.run_verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.mac_rows()
        self.assertEqual(len(rows), 1)
        record = cast(dict[str, object], rows[0]["record"])
        self.assertEqual((record["kind"], record["state"]), ("check", "on_mac"))
        self.assertEqual(record["runs_on"], {"kind": "any_target", "mac_targets": ["mac"]})
        self.assertEqual([event for event in self.fixture.cargo_events()
                          if event["args"][:1] != ["metadata"]], [])
        decisions = [cast(dict[str, object], json.loads(line)) for line in self.events.read_text().splitlines()
                     if cast(dict[str, object], json.loads(line))["command"] == "constraints"]
        self.assertTrue(decisions)
        self.assertTrue(all(row["call_id"] == "" for row in decisions))
        self.assert_removed()

    def test_natedev_only_waits_for_release_before_running(self) -> None:
        # A check is natedev_only only through --local; linux_only never keeps a check off the Mac.
        self.fixture.environment["TEST_MAC_ALLOWED"] = "0"
        admission = caller_admission(self.fixture, self.fixture.root, self.fixture.environment)
        previous = admission.server.handle_request
        submitted: list[wire.PlacementRequest] = []

        def handle(request: wire.Request, connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                   ) -> list[wire.Reply]:
            if isinstance(request, wire.PlacementRequest):
                submitted.append(request)
            return previous(request, connection)

        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
            result = self.run_verify("check", "sample", "--local", "creates a Linux install")
        self.assertTrue(submitted)
        self.assertTrue(all(request.route == target_config.LocalRequired("creates a Linux install")
                            for request in submitted))
        facts = records.VerifyCall("check", "verify check sample --local 'creates a Linux install'", 1, 1)
        self.assertTrue(all(request.identity.verify == facts for request in submitted))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = [cast(dict[str, object], json.loads(line))["command"] for line in self.events.read_text().splitlines()]
        self.assertTrue(self.fixture.local_waits)
        self.assertEqual(commands.count("constraints"), 1)
        self.assertEqual(self.mac_rows(), [])
        self.assertTrue(any(event["args"][:1] == ["check"] for event in self.fixture.cargo_events()))
        self.assert_removed()

    def test_each_verify_verb_sends_its_call_and_step_position(self) -> None:
        admission = caller_admission(self.fixture, self.fixture.root, self.fixture.environment)
        previous = admission.server.handle_request
        submitted: list[wire.PlacementRequest] = []

        def handle(request: wire.Request, connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                   ) -> list[wire.Reply]:
            if isinstance(request, wire.PlacementRequest):
                submitted.append(request)
            return previous(request, connection)

        metadata = cast(dict[str, object], json.loads(self.fixture.environment["TEST_METADATA"]))
        metadata["target_directory"] = str(self.fixture.root / "target")
        package, = cast(list[dict[str, object]], metadata["packages"])
        targets = cast(list[dict[str, object]], package["targets"])
        source = self.fixture.root / "src" / "main.rs"
        _ = source.write_text("fn main() {}\n")
        targets.append({"name": "sample", "kind": ["bin"], "test": True, "src_path": str(source)})
        self.fixture.environment["TEST_METADATA"] = json.dumps(metadata)
        cases: tuple[tuple[records.VerifyVerb, tuple[str, ...], tuple[str, ...]], ...] = (
            ("check", ("--keep-going",), ("check",)),
            ("test", ("--no-cache",), ("nextest",)),
            ("lint", ("--check", "--no-cache"), ("mend", "clippy")),
            ("doc", (), ("doc",)),
            ("example", (), ("clippy",)),
            ("build", ("--release",), ("build",)),
            ("final", (), ("check", "nextest")),
        )
        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
            for verb, flags, steps in cases:
                with self.subTest(verb=verb):
                    submitted.clear()
                    if verb == "final":
                        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink(missing_ok=True)
                    if verb == "doc":
                        # Prepare the doc request before the caller takes its checkout lease.
                        prepared = self.fixture.root / "doc-request.json"
                        with mock.patch.dict(self.fixture.environment, {
                            "BUILDLOG_FIRST_STEP_REQUEST": str(prepared),
                            "BUILDLOG_FIRST_STEP_EPISODE": "doc-fixture",
                            "BUILDLOG_FIRST_STEP_SINCE": "1",
                        }):
                            result = self.run_verify(verb, "sample", *flags)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        request = wire.parse_request(prepared.read_text())
                        assert isinstance(request, wire.PlacementRequest)
                        submitted.append(request)
                    else:
                        result = self.run_verify(verb, "sample", *flags)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    seen = list(dict.fromkeys(request.identity.step for request in submitted))
                    self.assertEqual(seen, list(steps))
                    for request in submitted:
                        self.assertEqual(request.identity.verify, records.VerifyCall(
                            verb, " ".join(("verify", verb, "sample", *flags)),
                            steps.index(request.identity.step) + 1, len(steps)))

    def test_unreadable_record_refuses_before_local_or_mac_compile(self) -> None:
        script = Path(self.fixture.environment["BUILD_TARGETS_SCRIPT"])
        # Damage the record in a check that still reports the active hold.
        source = TARGETS.replace('    if command in ("constraints",', '''    if command == "assigned-info":
        for record in Path(os.environ["VERIFY_STEPS_DIR"]).glob("*.json"):
            record.write_text("{")
    if command in ("constraints",''')
        _ = script.write_text(source)
        result = self.run_verify("check", "sample")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("step refused", result.stderr)
        self.assertEqual(self.mac_rows(), [])
        self.assertFalse(any(event["args"][:1] == ["check"] for event in self.fixture.cargo_events()))
        self.assert_removed()

if __name__ == "__main__":
    _ = unittest.main()
