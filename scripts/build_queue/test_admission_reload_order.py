"""Verify keeps a repository-limit waiter's place across a service restart."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from typing import cast
import unittest
from unittest import mock

from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_records as records
from scripts.build_queue.test_placement import PlacementFixture
from scripts.lib import conf
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message.roster import UnitLabels
from scripts.message.session_send_test_support import forbid_real_sender


CHECKOUT = Path(__file__).resolve().parents[2]


def setUpModule() -> None:
    forbid_real_sender()


@dataclass(frozen=True)
class VerifyRun:
    unit: str
    owner: wire.RunIdentity
    since: float

    def assignment_file(self, checkout: Path) -> Path:
        return checkout / f"{self.unit}.json"

    def admission_command(self, checkout: Path, *, preflight: bool) -> list[str]:
        if preflight:
            return ["verify-run-slot", "--pid", str(self.owner.pid), "--episode", ""]
        return [
            "place-step", "--once", "--wait-events", "--episode", self.unit, "--since", str(self.since),
            "--pid", str(self.owner.pid), "--run", f"{self.owner.pid}:{self.owner.start_time}",
            "--repo-root", str(checkout), "--kind", "check", "--source-key", "fixture-source",
            "--assignment-file", str(self.assignment_file(checkout)), "--local-only", "--token", "none",
            "--", "cargo", "check",
        ]


@contextmanager
def verify_client(checkout: Path, unit: str, args: list[str]) -> Generator[subprocess.Popen[str]]:
    environment = {**os.environ, "PYTHONPATH": str(CHECKOUT), "BUILDLOG_UNIT": unit,
        "BUILDLOG_VERIFY_VERB": "check", "BUILDLOG_VERIFY_CALL": "verify check",
        "BUILDLOG_VERIFY_POSITION": "1", "BUILDLOG_VERIFY_TOTAL": "1"}
    with subprocess.Popen([sys.executable, "-m", "scripts.build_queue.admission_client", *args],
                          cwd=checkout, env=environment, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            yield process
        finally:
            if process.returncode is None:
                process.kill()
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)


class AdmissionReloadOrderTests(unittest.TestCase):
    maxDiff: int | None = None

    def assert_client_exit(self, process: subprocess.Popen[str], expected: int) -> None:
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, expected, output + errors)

    def submit_waiter(self, callers: ExitStack, checkout: Path, run: VerifyRun,
                      reason: records.RunLimitWait, *, preflight: bool) -> None:
        process = callers.enter_context(verify_client(checkout, run.unit,
                                                       run.admission_command(checkout, preflight=preflight)))
        if not preflight:
            self.assert_client_exit(process, 78)
            return
        assert process.stderr is not None
        message = records.run_limit_text(reason)
        with LauncherHangGuard(f"{run.unit} reaches the repository limit").protect():
            while line := cast(str, process.stderr.readline()):
                if message in line:
                    return
        self.fail(f"{run.unit} left Verify before it reported {message!r}")

    def repository_queue(self, reason: records.RunLimitWait,
                         waiters: tuple[VerifyRun, VerifyRun]) -> dict[str, float]:
        by_pid = {run.owner.pid: run.unit for run in waiters}
        rows = [step for step in client.view().view.waiting if step.pid in by_pid]
        self.assertEqual(set(step.pid for step in rows), set(by_pid))
        head_reason = rows[0].reason
        self.assertEqual(head_reason.cause if isinstance(head_reason, records.HeadAdmissionStatus)
                         else head_reason, reason)
        return {by_pid[step.pid]: step.since for step in rows}

    def test_restart_keeps_a_repository_limit_waiter_arrival_and_order(self) -> None:
        # Use the first admission command that this checkout's Verify uses.
        verify = (CHECKOUT / "scripts/direct/verify.sh").read_text()
        preflight = "verify-run-slot --pid" in verify
        for bump in (False, True):
            with self.subTest(natemerge_bump=bump), PlacementFixture() as fixture:
                fixture.configure()
                settings = fixture.root / "build_admission.conf"
                _ = settings.write_text(settings.read_text()
                    + f"\nmax_runs.{fixture.server.host}.fixture=1\nmax_runs.exempt_unit=natemerge\n")
                _ = write_config_fixture(conf.path("build_targets"), "targets=natedev\n")
                checkout = fixture.root / "fixture"
                checkout.mkdir()
                _ = (checkout / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n')
                reason = records.RunLimitWait(1, 1, "fixture", fixture.server.host)
                holder = VerifyRun("holder", fixture.run_owner(), fixture.clock.now())
                with fixture.serving():
                    with verify_client(checkout, holder.unit,
                                       holder.admission_command(checkout, preflight=preflight)) as process:
                        self.assert_client_exit(process, 0)
                    if not preflight:
                        with verify_client(checkout, holder.unit, ["finish-step", "--assignment-file",
                                           str(holder.assignment_file(checkout)), "--status", "0"]) as process:
                            self.assert_client_exit(process, 0)
                self.assertEqual([slot.owner.pid for slot in fixture.server.verify_run_slots.values()],
                                 [holder.owner.pid])
                fixture.allow_next_start()
                now = fixture.clock.now()
                older = VerifyRun("older", fixture.run_owner(), now - 2)
                younger = VerifyRun("younger", fixture.run_owner(), now - 1)
                waiters = (older, younger)
                preferred = (younger, older) if bump else waiters
                arrived = {run.unit: run.since for run in waiters}
                with ExitStack() as callers:
                    with fixture.serving():
                        for run in waiters:
                            self.submit_waiter(callers, checkout, run, reason, preflight=preflight)
                        first = self.repository_queue(reason, waiters)
                        if bump:
                            labels = {"younger-session": UnitLabels("younger", unit=younger.unit)}
                            with mock.patch.object(gate, "priority_labels", return_value=labels):
                                self.assertEqual(client.priority_set(younger.unit, "reload order fixture", "natemerge"),
                                                 wire.Ok())
                            _ = fixture.run_pass()
                        before = list(self.repository_queue(reason, waiters))
                    fixture.server.close()
                fixture.clock.advance(1)
                fixture.restart()
                with ExitStack() as callers:
                    with fixture.serving():
                        for run in reversed(waiters):
                            self.submit_waiter(callers, checkout, run, reason, preflight=preflight)
                        after = self.repository_queue(reason, waiters)
                    fixture.end_run(holder.owner)
                    fixture.allow_next_start()
                    _ = fixture.run_pass()
                    pids = {slot.owner.pid for slot in fixture.server.verify_run_slots.values()}
                winner = [run.unit for run in waiters if run.owner.pid in pids]
                order = [run.unit for run in preferred]
                self.assertEqual({
                    "preserved the client arrival": first == arrived,
                    "preserved the arrival across restart": after == first,
                    "order before bump": list(first),
                    "order before restart": before,
                    "order after reverse reconnect": list(after),
                    "winner after holder exits": winner,
                    "slots used": len(pids),
                }, {
                    "preserved the client arrival": True,
                    "preserved the arrival across restart": True,
                    "order before bump": list(arrived),
                    "order before restart": order,
                    "order after reverse reconnect": order,
                    "winner after holder exits": order[:1],
                    "slots used": 1,
                })
