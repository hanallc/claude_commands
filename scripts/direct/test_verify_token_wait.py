"""Real verify.sh token wait behavior."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from typing import cast, override

from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct import test_verify_step_routes as routes
from scripts.direct.verify_token_wait_test_support import BOARD, VerifyTokenWaitFixture, Record
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


class VerifyTokenWaitTests(VerifyTokenWaitFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = caller_admission(self, self.root, self.environment)

    def test_granted_local_step_starts_cargo_without_a_token_call(self) -> None:
        session = Path(self.environment["UNIT_SESSION_DIR"])
        take = subprocess.run(["bash", str(BOARD), "acquire", str(session), "peer", "cargo",
                               "--pid", str(os.getpid())], env=self.environment,
                              capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(take.returncode, 0, take.stdout + take.stderr)
        original_board = (session / "board.log").read_text()
        observer = self.root / "observe-token.py"
        observations = self.root / "token-observations.json"
        _ = observer.write_text('import sys\n' + f"with open({str(Path(__file__).resolve().parents[2] / 'scripts/lib/package_root.py')!r}) as package_root: exec(package_root.read(), {{'__file__': package_root.name}})\n" + '''import json
import os
from pathlib import Path
import sys
from scripts.build_queue.admission_service import read_state

phase = sys.argv[1]
state = read_state(Path(os.environ["BUILDLOG_DIR"]) / "admission")
reservations = list(state.reservations.values())
lock = Path(os.environ["UNIT_SESSION_DIR"]) / "locks/cargo.d"
capture = Path(os.environ["TEST_TOKEN_OBSERVATIONS"])
rows = json.loads(capture.read_text()) if capture.exists() else {}
assert (lock / "holder").read_text().strip() == "peer"
if phase == "admission":
    assert phase not in rows, rows
    assert not reservations, reservations
    rows[phase] = {"reservations": len(reservations)}
else:
    assert len(reservations) == 1, reservations
    rows[phase] = {"reservations": len(reservations)}
capture.write_text(json.dumps(rows))
''')
        shell = self.root / "observe.sh"
        _ = shell.write_text('''set -T
observe_admission() {
    if [[ "${FUNCNAME[1]:-}" == placement_once && "$BASH_COMMAND" == 'PLACEMENT_CLIENT_ACTIVE=1' \
          && ! -e "$TEST_TOKEN_OBSERVATIONS" ]]; then
        python3 "$TEST_TOKEN_OBSERVER" admission || exit 95
    fi
}
trap observe_admission DEBUG
''')
        result = self.verify({**self.environment, "BASH_ENV": str(shell),
                              "TEST_TOKEN_OBSERVER": str(observer), "TEST_TOKEN_OBSERVATIONS": str(observations)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        captured = cast(dict[str, Record], json.loads(observations.read_text()))
        self.assertEqual(set(captured), {"admission", "run"})
        self.assertEqual(captured["admission"]["reservations"], 0)
        self.assertEqual(captured["run"]["reservations"], 1)
        self.assertEqual((session / "board.log").read_text(), original_board)
        self.assertEqual(self.records()[0]["token_wait_s"], 0)
        directory = self.root / "buildlog/admission"
        times = directory / "queue_times.jsonl"
        rows = [cast(Record, json.loads(line)) for line in times.read_text().splitlines()] if times.exists() else []
        self.assertEqual(sum(row.get("kind") == "token_gap" for row in rows), 0)
        self.assertEqual(list(directory.glob("*.reservation")), [])
        self.assertEqual(len(self.records()), 1)
        self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))

    def test_mac_routing_under_hold_skips_initial_token_acquisition(self) -> None:
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        _ = (holders / "other").write_text("fixture hold")
        targets = self.root / "targets.py"
        runner = self.root / "mac-runner.py"
        _ = targets.write_text(routes.TARGETS)
        _ = runner.write_text(routes.RUNNER)
        _ = (self.root / "build_targets.conf").write_text(
            "targets=natedev,mac\nkind.natedev=local\nplatform.natedev=linux\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac-settings\n")
        _ = write_config_fixture(self.root / "mac-settings", f"offload=on\nlinux_only.{self.root.name}=\n",
                                 template="mac_test")
        self.environment.update({
            "BUILD_TARGETS_SCRIPT": str(targets), "VERIFY_MAC_RUNNER": str(runner),
            "TEST_ROUTE_EVENTS": str(self.root / "route-events"),
            "TEST_MAC_EVENTS": str(self.root / "mac-events"),
            "MAC_TEST_CONFIG": str(self.root / "mac-settings"), "CONF_DIR": str(Path(self.root / "mac-settings").parent), "TEST_MAC_ALLOWED": "1",
        })
        result = self.verify(self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [cast(Record, json.loads(line)) for line in (self.root / "mac-events").read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(cast(Record, rows[0]["record"])["state"], "on_mac")
        self.assertFalse((self.root / "cargo-calls").exists())
        self.assertFalse((self.root / "session" / "locks" / "cargo.d").exists())
        self.assertEqual(self.records()[-1]["token_wait_s"], 0)

    def test_no_token_records_zero(self) -> None:
        environment = {name: value for name, value in self.environment.items()
                       if name != "UNIT_SESSION_DIR"}
        # Use a warm build directory so Cargo seeding stays outside this test.
        environment["CARGO_BUILD_BUILD_DIR"] = environment["CARGO_TARGET_DIR"]
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["token_wait_s"], 0)
        self.assertEqual((self.root / "metadata-calls").read_text().splitlines(), ["metadata"])
        self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))

if __name__ == "__main__":
    _ = unittest.main()
