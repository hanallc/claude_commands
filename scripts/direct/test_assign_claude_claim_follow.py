"""Test Claude follow refusal and claim behavior."""
from __future__ import annotations

import json
import multiprocessing
import os
import signal
from typing import ClassVar, cast
from unittest.mock import patch

from scripts.direct.assign_claude_test_support import AssignClaudeLauncherFixture
from scripts.direct import assign_claude, worker_state
from scripts.lib import process_start
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.direct.test_assign_claude import GROUP_STOP_READY, stop_assignment


class AssignClaudeLauncherFollowTests(AssignClaudeLauncherFixture):
    direct_cli_calls: ClassVar[bool] = True

    def test_new_launch_and_delivery_retry_preserve_a_live_claim(self) -> None:
        session = self.start_phase("held-publication")
        _ = self.claude_worker(session)
        claim = session / "impl_claim_fixture-impl.json"
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()

        def publish() -> None:
            worker_state.publish_worker_claim(claim, "fixture-impl")
            ready.set()
            _ = release.wait(TEST_HANG_GUARD_SECS)

        publisher = context.Process(target=publish)
        publisher.start()
        try:
            self.assertTrue(ready.wait(TEST_HANG_GUARD_SECS))
            pid = publisher.pid
            assert pid is not None
            self.assertNotEqual(pid, os.getpid())
            self.assertEqual(worker_state.read_worker_claim(claim), worker_state.HeldWorkerClaim(pid))
            original = claim.read_bytes()
            before = self.open_passes(session)
            for kind in ("new", "retry"):
                with self.subTest(kind=kind):
                    if kind == "retry":
                        with patch.object(assign_claude, "_switch", return_value=assign_claude.ClaudeDeliveryRetry()):
                            result = self.worker_launch(session, to="fixture-impl")
                    else:
                        result = self.worker_launch(session, slot="fixture-impl")
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("claimed by launcher", result.stderr)
                    self.assertEqual(claim.read_bytes(), original)
                    self.assertEqual(self.open_passes(session), before)
                    self.assertEqual(self.launch_records(session, "pass_started"), [])
                    self.assertFalse((self.root / "delivered_message.txt").exists())
        finally:
            release.set()
            publisher.join(timeout=TEST_HANG_GUARD_SECS)
            if publisher.is_alive():
                publisher.kill()
                publisher.join()
            publisher.close()

    def test_group_stop_leaves_a_free_claim_and_the_next_follow_proceeds(self) -> None:
        session = self.start_phase("killed-follow")
        transcript = self.claude_worker(session)
        sender = self.root / "send"
        original = sender.read_text()
        environment = self.launch_environment(session, "fixture-impl")
        environment.update({"STUB_TRANSCRIPT": str(transcript),
            "CLAUDE_PROJECTS_DIR": str(transcript.parent.parent), "CLAUDE_WORKER_SEND_SCRIPT": str(sender)})
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(number=number):
                _ = sender.write_text(original.replace(
                    "behavior = os.environ.get('STUB_SEND_BEHAVIOR', 'complete')", GROUP_STOP_READY))
                result, pid = stop_assignment(self.assign_command(session, "--to", "fixture-impl"), environment, number)
                self.assertEqual(result.returncode, -number, result.stdout + result.stderr)
                self.assertIsInstance(process_start.process_start(pid), (process_start.Gone, process_start.Zombie))
                claim = session / "impl_claim_fixture-impl.json"
                self.assertTrue(claim.exists())
                self.assertIsInstance(worker_state.read_worker_claim(claim), worker_state.EndedWorkerClaim)
                _ = worker_state.worker_status_paths(session)
                self.assertEqual((session / "impl_status_fixture-impl").read_text(), "error\n")
                self.assertIn("this worker is down", (session / "board.log").read_text())
                _ = sender.write_text(original)
                retry = self.worker_launch(session, to="fixture-impl")
                self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
                self.assertEqual((session / "impl_status_fixture-impl").read_text(), "implemented\n")
                self.assertFalse(claim.exists())

    def test_busy_or_missing_worker_preserves_the_pass_and_status(self) -> None:
        session = self.start_phase("busy-or-gone")
        transcript = self.claude_worker(session)
        status = session / "impl_status_fixture-impl"
        _ = status.write_text("previous\n")
        before = self.open_passes(session)
        original = transcript.read_bytes()
        for state in ("busy", "gone"):
            with self.subTest(state=state):
                _ = (self.root / "worker_state").write_text(state)
                result = self.worker_launch(session, to="fixture-impl")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("busy or gone", result.stderr)
                self.assertEqual(status.read_text(), "previous\n")
                self.assertEqual(self.open_passes(session), before)
                self.assertEqual(transcript.read_bytes(), original)

    def test_follow_claim_records_only_the_launcher_and_worker(self) -> None:
        session = self.start_phase("claim-fields")
        _ = self.claude_worker(session)
        sender = self.root / "send"
        _ = sender.write_text(sender.read_text().replace(
            "behavior = os.environ.get('STUB_SEND_BEHAVIOR', 'complete')",
            "claim = pathlib.Path(os.environ['STUB_SUMMARY']).parent / 'impl_claim_fixture-impl.json'\n"
            + "(root / 'claim-at-delivery.json').write_bytes(claim.read_bytes())\n"
            + "behavior = os.environ.get('STUB_SEND_BEHAVIOR', 'complete')"))
        result = self.worker_launch(session, to="fixture-impl")
        self.assertEqual(result.returncode, 0, result.stderr)
        claim = cast(dict[str, str | int], json.loads((self.root / "claim-at-delivery.json").read_text()))
        self.assertEqual(set(claim), {"pid", "started", "worker", "scope"})
        self.assertIsInstance(claim["started"], str)
        self.assertTrue(claim["started"])
        self.assertTrue(claim["scope"])
        self.assertGreater(int(claim["pid"]), 0)
        self.assertEqual(claim["worker"], "fixture-impl")
