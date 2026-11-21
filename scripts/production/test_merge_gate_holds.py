"""Hold and interruption contracts for merge-gate jobs."""

from __future__ import annotations

import unittest
import json
import contextlib
import io
import os
import fcntl
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace
from typing import final, override
from contextlib import redirect_stdout
from unittest import mock
from scripts.aws_target import cloud_gate
from scripts.production import merge_gate
from scripts.production.gate_modules import GateRefused
from scripts.direct.local_release_wait import IntervalReleasePolling
from scripts.build_hold.test_hold_applies import TemporaryHoldTest
from scripts.production.merge_gate_test_support import MergeGateFixture, gate_suites, suites_for_case, wait_for_file


@final
class MergeGateHoldTests(TemporaryHoldTest):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.gate = MergeGateFixture()
        self.hold_environment: dict[str, str] = {}

    @classmethod
    @override
    def setUpClass(cls) -> None:
        MergeGateFixture.setUpClass()
        cls.addClassCleanup(MergeGateFixture.doClassCleanups)

    @override
    def setUp(self) -> None:
        super().setUp()
        self.hold_environment = {name: os.environ[name] for name in (
            "BUILD_HOLD_DIR", "BUILD_HOLD_RELEASE_DIR", "MAC_TEST_STATE_DIR",
            "NOTIFIER_SESSIONS_DIR", "CLAUDE_CODE_SESSION_ID",
            "UNIT_RUN_DIR", "UNIT_SESSION_DIR", "PLAN_DELEGATE_BOARD_DIR", "PLAN_DELEGATE_SESSION_DIR",
        )}
        self.addCleanup(self.gate.doCleanups)
        self.gate.setUp()
        self.restore_gate_fixture()

    def restore_gate_fixture(self) -> None:
        self.gate.restore_fixture(suites_for_case(self))
        # Keep the hold helper's roots and owner, and the gate helper's tools and repos.
        _ = (Path(self.gate.environment["CONF_DIR"]) / "build_targets.conf").write_bytes(
            (self.root / "build_targets.conf").read_bytes())
        # The fake process root also identifies the real process that owns this gate.
        process = Path(os.environ["VERIFY_PROC_ROOT"]) / str(os.getpid())
        process.mkdir(parents=True, exist_ok=True)
        _ = (process / "stat").write_bytes(Path(f"/proc/{os.getpid()}/stat").read_bytes())
        self.enterContext(mock.patch.dict(os.environ, {**self.gate.environment, **self.hold_environment}))

    def record(self, event: str) -> None:
        with (self.gate.state / "modules.jsonl").open("a") as output:
            fcntl.flock(output, fcntl.LOCK_EX)
            _ = output.write(json.dumps({"suite": "", "module": "", "pid": os.getpid(),
                                         "event": event, "tmpdir": ""}) + "\n")

    @gate_suites("lib", "hooks")
    def test_only_local_gate_jobs_wait_for_a_natedev_hold(self) -> None:
        arguments = ["--repo", str(self.gate.checkout), "--merge-branch", "integration",
                     "--unit-branch", "unit-phase", "--checkpoint", self.gate.checkpoint,
                     "--previous-checkpoint", self.gate.base, "--unit", "fixture",
                     "--phase", "hold",
                     "--production", "quiet",
                     "--main-repo", str(self.gate.main), "--ssh", str(self.gate.bin / "ssh")]
        cloud_pass = merge_gate.GateRun((merge_gate.GateJobResult("lib", "test_first.py", 0, 1, ""),), 1, 0)
        fallback = "AWS declined the fixture gate"
        cases = (
            ("local tests only", ("--tests-only",), fallback),
            ("checkpoint with local fallback", (), fallback),
            ("promotion only", ("--promote-only",), cloud_pass),
            ("AWS checkpoint and promotion", ("--promote",), cloud_pass),
        )
        for label, flags, cloud_result in cases:
            with self.subTest(label=label):
                if flags == ("--promote",):
                    self.restore_gate_fixture()
                os.environ["MERGE_GATE_PASSED_DIR"] = str(self.gate.state / ("passes-" + label))
                self.gate.clear_gate_events()
                self.hold()
                holder = self.holders / "other"
                holder_bytes = holder.read_bytes()
                waits_for_local = isinstance(cloud_result, str)
                runs_gate = flags != ("--promote-only",)
                promotes = flags in (("--promote-only",), ("--promote",))
                before_merge = self.gate.git(self.gate.checkout, "rev-parse", "HEAD")
                before_main = self.gate.git(self.gate.main, "rev-parse", "HEAD")

                def route(_repo: Path, **_options: object) -> merge_gate.GateRun | str:
                    self.assertEqual(holder.read_bytes(), holder_bytes)
                    if flags != ("--tests-only",):
                        self.assertNotEqual(self.gate.git(self.gate.checkout, "rev-parse", "HEAD"), before_merge)
                    self.record("cloud")
                    return cloud_result

                def release(_seconds: float) -> None:
                    self.assertTrue(waits_for_local, label)
                    self.assertEqual([event["event"] for event in self.gate.module_events()], ["cloud"])
                    self.assertEqual(self.gate.calls(), [])
                    if flags == ("--tests-only",):
                        self.assertEqual(self.gate.git(self.gate.checkout, "rev-parse", "HEAD"), before_merge)
                    else:
                        self.gate.assert_not_pushed()
                    self.assertEqual(self.gate.git(self.gate.main, "rev-parse", "HEAD"), before_main)
                    self.record("release")
                    holder.unlink()

                events = IntervalReleasePolling()
                output = io.StringIO()
                with (mock.patch.object(cloud_gate, "route", side_effect=route),
                      mock.patch.object(merge_gate, "local_release_waiter", return_value=events),
                      mock.patch.object(events, "wait", side_effect=release) as wait,
                      redirect_stdout(output)):
                    code = merge_gate.main([*arguments, *flags])
                self.assertEqual(code, 0, output.getvalue())
                self.assertEqual(wait.call_count, int(waits_for_local))
                self.assertEqual(output.getvalue().count("natedev is held by builder; waiting"), int(waits_for_local))
                self.assertEqual(output.getvalue().count("Gate hold: released after"), int(waits_for_local))
                recorded = [event["event"] for event in self.gate.module_events()]
                if waits_for_local:
                    self.assertEqual(recorded[:2], ["cloud", "release"])
                    self.assertEqual(recorded.count("finish"), self.gate.module_count)
                else:
                    self.assertEqual(recorded, ["cloud"] if runs_gate else [])
                    self.assertEqual(holder.read_bytes(), holder_bytes)
                self.assertEqual(len([call for call in self.gate.calls() if call["tool"] == "ssh"]), int(promotes))
                if promotes:
                    tip = self.gate.git(self.gate.origin, "rev-parse", "integration")
                    self.assertNotEqual(tip, before_main)
                    self.assertEqual(self.gate.git(self.gate.origin, "rev-parse", "main"), tip)
                    self.assertEqual(self.gate.git(self.gate.main, "rev-parse", "HEAD"), tip)

    @gate_suites("lib", "hooks")
    def test_sole_holder_runs_all_modules_and_types_without_waiting(self) -> None:
        self.hold("caller", "builder")
        output = io.StringIO()
        with mock.patch.object(merge_gate, "local_release_waiter",
                               side_effect=AssertionError("the sole holder must run")), redirect_stdout(output):
            run = merge_gate.run_suites(self.gate.checkout, type_dirs=("scripts/production",))
        self.assertTrue(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), self.gate.module_count + 1)
        self.assertTrue((self.holders / "caller").exists())
        self.assertTrue((self.gate.state / "types-ran").exists())
        self.assertNotIn("Gate hold:", output.getvalue())
        self.assertNotIn("Gate hold wait:", output.getvalue())

    @gate_suites("lib", "hooks")
    def test_mid_gate_hold_stops_new_starts_and_excludes_wait_time(self) -> None:
        self.gate.write(self.gate.state / "concurrency-barrier", "held")
        start = merge_gate.start_gate_job
        finish = merge_gate.finish_gate_job
        started: list[merge_gate.RunningGateJob] = []
        now = [0.0]
        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=time.sleep, time=time.time)

        def start_module(suite: str, path: Path, argv: list[str], repo: Path,
                         environment: dict[str, str], log: Path) -> merge_gate.RunningGateJob:
            self.assertFalse((self.holders / "other").exists(), self.gate.module_events())
            job = start(suite, path, argv, repo, environment, log)
            started.append(job)
            wait_for_file(self.gate.state / f"{job.process.pid}.started")
            if len(started) == 1:
                self.hold()
                self.record("hold")
            (self.gate.state / f"{job.process.pid}.released").touch()
            return job

        def finish_module(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
            result = finish(job, status)
            if job is started[0]:
                recorded = self.gate.module_events()
                self.assertEqual([event["event"] for event in recorded], ["start", "hold", "finish"])
                now[0] += 37.0
                self.record("release")
                (self.holders / "other").unlink()
            return result

        output = io.StringIO()
        with mock.patch.object(merge_gate, "start_gate_job", side_effect=start_module), \
                mock.patch.object(merge_gate, "finish_gate_job", side_effect=finish_module), \
                mock.patch.object(merge_gate, "time", clock), redirect_stdout(output):
            run = merge_gate.run_suites(self.gate.checkout, max_running_jobs=2)
        self.assertTrue(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), self.gate.module_count)
        self.assertEqual(len([event for event in self.gate.module_events() if event["event"] == "finish"]), self.gate.module_count)
        self.assertEqual(run.seconds, 0.0)
        self.assertIn("Gate hold wait: 37.00s (excluded from Gate tests)", output.getvalue())
        self.assertIn("Gate tests: 0.00s;", output.getvalue())

    def test_local_hold_read_error_undoes_the_merge_without_pushing(self) -> None:
        def refuse_cloud(_repo: Path, **_options: object) -> str:
            self.assertNotEqual(self.gate.git(self.gate.checkout, "rev-parse", "HEAD"), self.gate.base)
            self.unreadable()
            return "AWS declined the fixture gate"

        output = io.StringIO()
        with mock.patch.object(cloud_gate, "route", side_effect=refuse_cloud), \
                contextlib.redirect_stderr(output), redirect_stdout(output):
            code = merge_gate.main([
                "--repo", str(self.gate.checkout), "--merge-branch", "integration",
                "--unit-branch", "unit-phase", "--checkpoint", self.gate.checkpoint,
                "--previous-checkpoint", self.gate.base, "--unit", "fixture", "--phase", "hold",
                "--production", "quiet",
            ])
        self.assertNotEqual(code, 0, output.getvalue())
        self.assertIn("natedev hold state cannot be read", output.getvalue())
        self.assertIn("merge undone, not pushed", output.getvalue())
        self.assertEqual(self.gate.git(self.gate.checkout, "rev-parse", "HEAD"), self.gate.base)
        self.gate.assert_not_pushed()
        self.assertFalse((self.gate.checkout / "unit.txt").exists())
        self.assertEqual(self.gate.module_events(), [])

    def test_signalled_module_and_types_jobs_resume_after_hold_release(self) -> None:
        for suite in ("hooks", "types"):
            with self.subTest(suite=suite):
                os.environ["MERGE_GATE_PASSED_DIR"] = str(self.gate.state / ("passes-" + suite))
                (self.gate.state / "modules.jsonl").unlink(missing_ok=True)
                self.gate.write(self.gate.state / "signal-hooks", "once")
                if suite == "types":
                    self.gate.write(self.gate.bin / "basedpyright", "#!" + sys.executable + "\n"
                                    + "import os, pathlib, signal\n"
                                    + "marker = pathlib.Path(os.environ['MERGE_GATE_TEST_STATE']) / 'signal-types'\n"
                                    + "if marker.exists():\n"
                                    + "    marker.unlink()\n"
                                    + "    os.kill(os.getpid(), signal.SIGTERM)\n"
                                    + "print('0 errors, 0 warnings, 0 notes')\n")
                    self.gate.write(self.gate.state / "signal-types", "once")
                    (self.gate.state / "signal-hooks").unlink()
                start = merge_gate.start_gate_job
                starts: list[str] = []

                def start_job(name: str, path: Path, argv: list[str], repo: Path,
                              environment: dict[str, str], log: Path, *,
                              kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE) -> merge_gate.RunningGateJob:
                    self.assertFalse((self.holders / "other").exists())
                    if not starts:
                        self.hold()
                        self.record("hold")
                    starts.append(name)
                    return start(name, path, argv, repo, environment, log, kind=kind)

                def release(_seconds: float) -> None:
                    self.assertEqual(starts, [suite])
                    self.record("release")
                    (self.holders / "other").unlink()

                events = IntervalReleasePolling()
                output = io.StringIO()
                with mock.patch.object(merge_gate, "RERUN_LIMIT", 0), \
                        mock.patch.object(merge_gate, "start_gate_job", side_effect=start_job), \
                        mock.patch.object(merge_gate, "local_release_waiter", return_value=events), \
                        mock.patch.object(events, "wait", side_effect=release), redirect_stdout(output):
                    run = merge_gate.run_suites(self.gate.checkout, max_running_jobs=1,
                                               type_dirs=("scripts/production",) if suite == "types" else ())
                self.assertTrue(run.passed, output.getvalue())
                self.assertEqual(starts[:2], [suite, suite])
                self.assertEqual(len(run.jobs), 2 if suite == "types" else 1)
                self.assertIn("stopped during a natedev hold; queued again", output.getvalue())
                self.assertNotIn("Rerun alone:", output.getvalue())
                events_recorded = [event["event"] for event in self.gate.module_events()]
                self.assertLess(events_recorded.index("hold"), events_recorded.index("release"))
                self.assertLess(events_recorded.index("release"), events_recorded.index("finish"))

    def test_a_signal_without_a_hold_keeps_the_module_red(self) -> None:
        self.gate.write(self.gate.state / "signal-hooks", "always")
        output = io.StringIO()
        with redirect_stdout(output):
            run = merge_gate.run_suites(self.gate.checkout)
        self.assertFalse(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), 1)
        self.assertEqual(run.jobs[0].returncode, -signal.SIGTERM)
        self.assertEqual([event["event"] for event in self.gate.module_events()], ["start", "start"])
        self.assertNotIn("queued again", output.getvalue())

    def test_unreadable_hold_state_refuses_before_any_job(self) -> None:
        self.unreadable()
        with self.assertRaisesRegex(GateRefused, "natedev hold state cannot be read"):
            _ = merge_gate.run_suites(self.gate.checkout, type_dirs=("scripts/production",))
        self.assertEqual(self.gate.calls(), [])
        self.assertEqual(self.gate.module_events(), [])


if __name__ == "__main__":
    _ = unittest.main()
