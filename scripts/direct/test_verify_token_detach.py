"""Real verify.sh token detach behavior."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import ClassVar, override
import unittest
from unittest import mock
from scripts.agents.worker_lifecycle import DETACHED_RUNS_DIR, read_json_object
from scripts.build_queue.admission_client import detached_verify_handoff
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY


class VerifyTokenDetachTests(VerifyTokenWaitFixture):
    def test_a_codex_worker_call_detaches_and_returns_without_building(self) -> None:
        environment = self.detach_environment(0)
        run_dir = self.root / "private-run"
        record_path = run_dir / DETACHED_RUNS_DIR / "fixture.json"
        record_path.parent.mkdir(parents=True)
        environment.update({"UNIT_RUN_DIR": str(run_dir), "TEST_DETACHED_RUN_RECORD": str(record_path)})
        stub = Path(environment["VERIFY_DETACHED_RUNS_SCRIPT"])
        _ = stub.write_text("""\
import json, os, sys
from pathlib import Path

if sys.argv[1:2] == ["start"]:
    label = sys.argv[sys.argv.index("--label") + 1]
    call_id = os.environ["VERIFY_CONTINUED_CALL_ID"]
    assert call_id and label.endswith(f" [{call_id}]")
    command = sys.argv[sys.argv.index("--") + 1:]
    Path(os.environ["TEST_DETACHED_RUN_RECORD"]).write_text(json.dumps({
        "label": label, "command": command, "status": "running",
        "command_pid": os.getpid(), "command_started": "1234",
    }))
""" + stub.read_text())

        result = self.verify(environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call[0] for call in self.detached_runs_calls()], ["start"])
        record = read_json_object(record_path)
        command = ["bash", str(VERIFY.resolve()), "check", "sample"]
        self.assertEqual(record["command"], command)
        label = record["label"]
        assert isinstance(label, str)
        with mock.patch.dict(os.environ, environment):
            self.assertTrue(detached_verify_handoff(label, command))
        self.assertFalse((self.root / "cargo-calls").exists())
        self.assertEqual(self.records(), [])

    def test_a_caller_detached_runs_cannot_detach_runs_attached(self) -> None:
        for status, note in ((3, False), (1, True)):
            with self.subTest(status=status):
                for path in (self.root / "detached-runs-calls", self.root / "cargo-calls"):
                    path.unlink(missing_ok=True)
                result = self.verify(self.detach_environment(status))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.detached_runs_calls()), 1)
                self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))
                self.assertEqual("could not detach" in result.stderr, note, result.stderr)

    def test_a_live_run_limit_refusal_starts_no_attached_command(self) -> None:
        result = self.verify(self.detach_environment(4))
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertEqual(len(self.detached_runs_calls()), 1)
        self.assertFalse((self.root / "cargo-calls").exists())
        self.assertEqual(self.records(), [])
        self.assertNotIn("running attached", result.stderr)

    def test_the_detached_run_itself_does_not_detach_again(self) -> None:
        environment = {**self.detach_environment(0), "UNIT_DETACHED_RUN": "1"}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.detached_runs_calls(), [])
        self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))


class VerifyTokenDetachInsideDetachedRunTests(VerifyTokenDetachTests):
    """The same calls from a suite that a detached run started, as a merge gate does."""

    outer_record: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        outer = tempfile.TemporaryDirectory(prefix="outer-run-")
        cls.addClassCleanup(outer.cleanup)
        cls.outer_record = Path(outer.name) / "outer-run.json"
        with mock.patch.dict(os.environ, {"UNIT_DETACHED_RUN": "1", "UNIT_DETACHED_RECORD": str(cls.outer_record)}):
            super().setUpClass()

    @override
    def tearDown(self) -> None:
        try:
            # verify.sh under test never saves its exit into the outer run's record.
            self.assertNotIn("record-exit", [call[0] for call in self.detached_runs_calls()])
        finally:
            super().tearDown()


if __name__ == "__main__":
    _ = unittest.main()
