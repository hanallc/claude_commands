"""Merge gate suites contracts."""

from __future__ import annotations
from scripts.build_queue import queue_place

import unittest
import io
import os
import sqlite3
import subprocess
from collections.abc import Sequence
from typing import final
from contextlib import closing, redirect_stdout
from unittest import mock
from scripts.buildlog import index
from scripts.production import gate
from scripts.production.gate_modules import GateRefused
from scripts.production.gate_test_support import FIXTURE_FOLDERS, gate_suites
from scripts.production.gate_suites_test_support import GateSuitesFixture
from scripts.lint.memory_test_support import write_config_fixture
from pathlib import Path


@final
class MergeGateRerunsTests(GateSuitesFixture):
    def set_module_time_history(self, rows: dict[str, tuple[float, ...]]) -> None:
        config = Path(self.environment["CONF_DIR"])
        _ = write_config_fixture(config / "gate.conf",
                                 "GATE_TIME_LIMIT_FACTOR=4\nrerun_time_limit_floor_seconds=3")
        _ = write_config_fixture(config / "test_times.conf", "TOP_RUNS=3")
        database = index.index_path_in(Path(self.environment["BUILDLOG_DIR"]))
        with closing(sqlite3.connect(database)) as connection, connection:
            for name, walls in rows.items():
                suite, module = name.split("/", 1)
                for attempt, wall in enumerate(walls):
                    run = f"{name}-{attempt}"
                    connection.execute("""INSERT INTO gate_runs
                        (id, started_at, step, vcpus, status, aborted, selection)
                        VALUES (?, ?, 'gate', ?, 0, 0, 'full')""",
                        (run, run, os.cpu_count() or 1)).close()
                    connection.execute("""INSERT INTO gate_modules
                        (run_id, suite, module, attempt, wall_s, status, restart)
                        VALUES (?, ?, ?, 1, ?, 0, '')""", (run, suite, module, wall)).close()

    @gate_suites("hooks", "lib")
    def test_named_module_time_limits_use_the_median_floor_or_default(self) -> None:
        self.set_module_time_history({"hooks/test_first.py": (2.0, 5.0, 20.0),
                                      "lib/test_first.py": (0.5,)})
        named_limits = {
            "hooks/test_first.py": gate.MeasuredModuleTimeLimit(4, 5),
            "lib/test_first.py": gate.FloorModuleTimeLimit(3),
            "lib/test_second.py": gate.DEFAULT_MODULE_TIME_LIMIT,
        }
        named = [word for name in named_limits for word in ("--module", name)]
        cases = (
            ("named", named, named_limits),
            ("full", ["--full"], dict.fromkeys(named_limits, gate.DEFAULT_MODULE_TIME_LIMIT)),
            ("explicit", [*named, "--module-time-limit-s", "1800"],
             dict.fromkeys(named_limits, gate.ExplicitModuleTimeLimit(1800))),
        )
        limits: dict[str, gate.ModuleTimeLimit] = {}
        finish = gate.finish_gate_job

        def capture_limit(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
            limits[f"{job.suite}/{job.module}"] = job.time_limit
            return finish(job, status)

        with mock.patch.object(gate, "finish_gate_job", side_effect=capture_limit):
            for label, options, expected in cases:
                limits.clear()
                result = self.run_gate("--tests-only", *options)
                for name, limit in expected.items():
                    with self.subTest(run=label, module=name):
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(limits.get(name), limit)

    def test_named_module_stops_at_the_fixture_floor(self) -> None:
        self.set_module_time_history({"hooks/test_first.py": (0.5,)})
        self.write(self.checkout / "scripts/hooks/test_first.py", """import os
from pathlib import Path
import time
import unittest


class HangingModule(unittest.TestCase):
    def test_hangs(self) -> None:
        time.sleep(8)
        (Path(os.environ["GATE_TEST_STATE"]) / "module-completed").touch()
""")
        result = self.run_gate("--tests-only", "--module", "hooks/test_first.py")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("hooks/test_first.py killed: still running after 3 s, "
                      + "the module time limit (configured rerun floor)", result.stdout)
        self.assertFalse((self.state / "module-completed").exists(), result.stdout)

    def test_nightly_timeout_names_the_default_limit(self) -> None:
        self.write(self.state / "hold-module", "hold")
        start = gate.start_gate_job

        def start_held_job(suite: str, path: Path, argv: Sequence[str], repo: Path,
                           environment: dict[str, str], log: Path, *,
                           kind: gate.GateJobKind = gate.GateJobKind.TEST_MODULE) -> gate.RunningGateJob:
            job = start(suite, path, argv, repo, environment, log, kind=kind)
            if suite == "hooks":
                job.started -= gate.DEFAULT_MODULE_TIME_LIMIT_S
            return job

        with (mock.patch.object(gate, "start_gate_job", side_effect=start_held_job),
              mock.patch.object(gate, "push_nightly_result"),
              mock.patch.object(gate, "tell_nightly_red", return_value=gate.NightlyNoticeUnreceived()),
              mock.patch.object(gate, "_sweep_nightly_build_folders")):
            result = self.run_gate("--nightly")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("hooks/test_first.py killed: still running after 1800 s, "
                      + "the module time limit (default)", result.stdout)

    def test_memory_stop_with_a_tripwire_reports_failure_without_a_rerun(self) -> None:
        module = self.checkout / "scripts/hooks/test_first.py"
        self.write(module, module.read_text().replace(
            '        if (state / "sample-module-memory").exists():',
            '        import subprocess\n'
            + '        subprocess.run(["cargo", "--version"], check=False)\n'
            + '        if (state / "sample-module-memory").exists():', 1))
        self.configure_gate("merge_gate_governor_gib=4")
        self.record_module_peaks({"hooks/test_first.py": 48.0})
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), \
                self.sampling_module_memory({"hooks/test_first.py": (5120.0, 32.0)}), redirect_stdout(output):
            run = gate.run_suites(self.checkout)
        self.assertFalse(run.passed, output.getvalue())
        result, = run.executions
        self.assertTrue(result.tripwire_tripped)
        self.assertIn("peak memory 5.000 GiB", result.killed_for)
        self.assertIn("a test started real cargo: --version", result.output)
        starts = [event for event in self.module_events() if event["event"] == "start"]
        self.assertEqual(len(starts), 1)
        self.assertNotIn("Rerun alone:", output.getvalue())
        self.assertEqual(gate.read_measures(self.state / "runner/module_peak_mib.json"),
                         {"hooks/test_first.py": 48.0})
        message, = [call for call in self.calls() if call["tool"] == "send.py"]
        self.assertEqual(message["args"][:4], ["--to", "sdlc", "--from", "gate"])
        text = message["args"][message["args"].index("--text") + 1]
        self.assertIn("the gate fails", text)
        self.assertNotIn("runs again alone", text)
        self.assertTrue(text.startswith("gate gate integration checkpoint "), text)
        for detail in (self.base[:7], "hooks/test_first.py", "5.000 GiB", "limit 4 GiB"):
            self.assertIn(detail, text)

    @gate_suites("lib", "hooks")
    def test_memory_stopped_module_passes_alone_and_messages_name_the_gate(self) -> None:
        cases = [(mode, True) for mode in gate.SuiteMode] + [(gate.SuiteMode.GATE, False)]
        for mode, full in cases:
            with self.subTest(mode=mode, full=full):
                self.restore_fixture(("lib", "hooks"))
                self.configure_gate("merge_gate_governor_gib=4")
                self.set_module_time_history({"hooks/test_first.py": (2.0,)})
                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        self.sampling_module_memory({"hooks/test_first.py": (5120.0, 64.0)}) as started, redirect_stdout(output):
                    run = gate.run_suites(self.checkout, mode=mode, full=full)
                self.assertTrue(run.passed, output.getvalue())
                attempts = [job for job in run.executions if job.suite == "hooks"]
                self.assertEqual(len(attempts), 2)
                self.assertNotEqual(attempts[0].returncode, 0)
                self.assertIn("peak memory 5.000 GiB", attempts[0].killed_for)
                self.assertEqual(attempts[1].returncode, 0)
                retry_limit = gate.DEFAULT_MODULE_TIME_LIMIT if full else gate.MeasuredModuleTimeLimit(4, 2)
                self.assertEqual([job.time_limit for job in started if job.suite == "hooks"],
                                 [gate.DEFAULT_MODULE_TIME_LIMIT, retry_limit])
                self.assertEqual([job for job in run.jobs if job.suite == "hooks"], [attempts[-1]])
                events = self.module_events()
                self.assertEqual([event["event"] for event in events[-2:]], ["start", "finish"])
                self.assertEqual(events[-2]["suite"], "hooks")
                self.assertIn("passed on rerun alone; first run", output.getvalue())
                messages = [call for call in self.calls() if call["tool"] == "send.py"]
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0]["args"][:4], ["--to", "sdlc", "--from", "gate"])
                text = messages[0]["args"][messages[0]["args"].index("--text") + 1]
                for detail in (mode.value, "integration", self.base[:7], "hooks/test_first.py", "5.000 GiB",
                               "limit 4 GiB", "runs again alone"):
                    self.assertIn(detail, text)

    def test_memory_retry_count_is_read_at_use_and_exhausted_stops_fail(self) -> None:
        cases = ((0, (5120.0,), False), (1, (5120.0, 6144.0), False), (2, (5120.0, 6144.0, 64.0), True))
        for reruns, samples, passed in cases:
            with self.subTest(reruns=reruns):
                self.restore_fixture(("hooks",))
                self.configure_gate("merge_gate_governor_gib=4")
                _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "gate.conf",
                                         f"memory_stop_reruns={reruns}")
                self.record_module_peaks({"hooks/test_first.py": 48.0})
                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        self.sampling_module_memory({"hooks/test_first.py": samples}) as started, redirect_stdout(output):
                    run = gate.run_suites(self.checkout)
                self.assertEqual(run.passed, passed, output.getvalue())
                self.assertEqual(len(started), len(samples))
                self.assertEqual(len(run.executions), len(samples))
                peaks = gate.read_measures(self.state / "runner/module_peak_mib.json")
                self.assertEqual(peaks, {"hooks/test_first.py": 64.0 if passed else 48.0})
                stops = [job for job in started if job.killed_for]
                messages = [call for call in self.calls() if call["tool"] == "send.py"]
                self.assertEqual(len(messages), len(stops))
                for number, (job, message) in enumerate(zip(stops, messages, strict=True)):
                    self.assert_process_stopped(job.process.pid)
                    text = message["args"][message["args"].index("--text") + 1]
                    self.assertIn(f"{samples[number] / 1024:.3f} GiB", text)
                    self.assertIn("runs again alone" if number < reruns else "the gate fails", text)
                if not passed:
                    self.assertIn(f"peak memory {samples[-1] / 1024:.3f} GiB", run.jobs[0].killed_for)
                    self.assertIn(run.jobs[0].killed_for, run.jobs[0].output)
                    self.assertIn("hooks: RED", output.getvalue())

    @gate_suites(*FIXTURE_FOLDERS)
    def test_memory_stops_retry_even_when_ordinary_failures_exceed_the_limit(self) -> None:
        for suite in self.suites:
            self.write(self.state / ("red-" + suite), "red")
        self.write(self.checkout / "scripts/lib/test_third.py",
                   (self.checkout / "scripts/lib/test_first.py").read_text())
        self.configure_gate("merge_gate_governor_gib=4")
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), \
                self.sampling_module_memory({"hooks/test_first.py": (5120.0, 32.0)}), redirect_stdout(output):
            run = gate.run_suites(self.checkout)
        self.assertFalse(run.passed, output.getvalue())
        starts = [event for event in self.module_events() if event["event"] == "start"]
        self.assertEqual(len(starts), self.module_count + 2)
        self.assertEqual(sum(event["suite"] == "hooks" for event in starts), 2)
        self.assertIn("9 modules failed; this is a real breakage", output.getvalue())
        self.assertEqual(starts[-1]["suite"], "hooks")

    def test_failed_memory_messages_do_not_change_the_gate_verdict(self) -> None:
        for samples, passed in (((5120.0, 32.0), True), ((5120.0, 5120.0), False)):
            with self.subTest(passed=passed):
                self.restore_fixture(("hooks",))
                self.configure_gate("merge_gate_governor_gib=4")
                self.write(self.state / "send-fails", "fail")
                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        self.sampling_module_memory({"hooks/test_first.py": samples}), redirect_stdout(output):
                    run = gate.run_suites(self.checkout)
                self.assertEqual(run.passed, passed, output.getvalue())
                stops = [job for job in run.executions if job.killed_for]
                messages = [call for call in self.calls() if call["tool"] == "send.py"]
                self.assertEqual(len(messages), len(stops))
                self.assertEqual(output.getvalue().count("Memory stop message could not be sent:"), len(stops))
                self.assertIn("fixture message failed", output.getvalue())

    @gate_suites("lib", "hooks")
    def test_red_modules_fail_again_print_both_outputs_and_reset(self) -> None:
        self.write(self.state / "red-lib", "red")
        self.write(self.state / "red-hooks", "red")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assert_not_pushed()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        for suite, module in (("lib", "test_first.py"), ("lib", "test_second.py"), ("hooks", "test_first.py")):
            self.assertIn(f"fixture failure in {suite}/{module}", result.stdout + result.stderr)
            first = result.stdout.index(f"--- {suite}/{module} first run exit=1 ---")
            second = result.stdout.index(f"--- {suite}/{module} rerun alone exit=1 ---")
            self.assertLess(first, second)
            self.assertIn(f"fixture failure in {suite}/{module}", result.stdout[first:second])
            self.assertIn(f"fixture rerun in {suite}/{module}", result.stdout[second:])
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), self.module_count + 3)

    @gate_suites("lib", "hooks")
    def test_failed_modules_pass_alone_and_keep_first_output_and_times(self) -> None:
        self.write(self.state / "fail-first-lib", "fail")
        self.set_module_time_history({"lib/test_first.py": (2.0, 5.0, 20.0), "lib/test_second.py": (0.5,)})
        self.environment["BUILDLOG_SCOPE"] = "1"
        output = io.StringIO()
        first_results: list[gate.GateJobResult] = []
        limits: dict[str, list[gate.ModuleTimeLimit]] = {}
        finish = gate.finish_gate_job

        def record_finish(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
            limits.setdefault(f"{job.suite}/{job.module}", []).append(job.time_limit)
            result = finish(job, status)
            first_results.append(result)
            return result

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(queue_place, "in_builds_slice", return_value=False), \
                mock.patch.object(gate, "finish_gate_job", side_effect=record_finish), redirect_stdout(output):
            run = gate.run_suites(self.checkout, type_dirs=("scripts/production",))
        report = output.getvalue()
        self.assertTrue(run.passed, report)
        self.assertEqual(len(run.jobs), self.module_count + 1)
        self.assertEqual(limits["lib/test_first.py"], [gate.DEFAULT_MODULE_TIME_LIMIT, gate.MeasuredModuleTimeLimit(4, 5)])
        self.assertEqual(limits["lib/test_second.py"], [gate.DEFAULT_MODULE_TIME_LIMIT, gate.FloorModuleTimeLimit(3)])
        for module in ("test_first.py", "test_second.py"):
            self.assertRegex(report, rf"Rerun alone: lib/{module} passed \(first run exit 1, [\d.]+ s\)")
            self.assertIn(f"--- lib/{module} passed on rerun alone; first run exit=1 ---", report)
            self.assertIn(f"fixture first run in lib/{module}", report)
        self.assertRegex(report, r"(?m)^lib: OK [\d.]+s$")
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), self.module_count + 2)
        self.assertTrue(all(row["suite"] == "lib" for row in starts[self.module_count:]))
        events = self.module_events()
        first_rerun = events.index(starts[self.module_count])
        self.assertEqual(sum(row["event"] == "finish" for row in events[:first_rerun]), self.module_count - 2)
        self.assertEqual([row["event"] for row in events[first_rerun:]], ["start", "finish", "start", "finish"])
        runner = self.state / "runner"
        timings = gate.read_measures(runner / "module_seconds.json")
        history = gate.read_history(runner / "module_history.json")
        for first in first_results[:self.module_count + 1]:
            if first.suite != "types":
                key = f"{first.suite}/{first.module}"
                self.assertEqual(timings[key], first.seconds)
                self.assertEqual(history[key], [first.seconds])
        scopes = [call for call in self.calls() if call["tool"] == "systemd-run"]
        self.assertEqual(len(scopes), self.module_count + 3)
        flags = ["--user", "--scope", "--quiet", "--collect", "--expand-environment=no", "--slice=builds.slice", "--"]
        self.assertEqual(scopes[0]["args"], [*flags, "true"])
        for call in scopes[1:]:
            self.assertEqual(call["args"][:len(flags)], flags)
        for call in scopes[-2:]:
            self.assertEqual(sum(previous["args"] == call["args"] for previous in scopes), 2)
        self.assertFalse(any("basedpyright" in " ".join(call["args"]) for call in scopes))

    def test_scope_skips_disabled_failed_probe_and_existing_builds_slice(self) -> None:
        for reason in ("disabled", "probe", "existing"):
            with self.subTest(reason=reason):
                self.environment["GATE_PASSED_DIR"] = str(self.state / ("passes-" + reason))
                self.environment["BUILDLOG_SCOPE"] = "0" if reason == "disabled" else "1"
                self.write(self.state / "scope-fails", "fail")
                (self.state / "tools.jsonl").unlink(missing_ok=True)
                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        mock.patch.object(queue_place, "in_builds_slice", return_value=reason == "existing"), \
                        redirect_stdout(output):
                    run = gate.run_suites(self.checkout)
                self.assertTrue(run.passed, output.getvalue())
                scopes = [call for call in self.calls() if call["tool"] == "systemd-run"]
                self.assertEqual([call["args"][-1] for call in scopes], ["true"] if reason == "probe" else [])
                message = {"disabled": "BUILDLOG_SCOPE=0", "probe": "probe failed (exit 1)",
                           "existing": "gate already runs in builds.slice"}[reason]
                self.assertIn("Module scope skipped: " + message, output.getvalue())

    @gate_suites(*FIXTURE_FOLDERS[:gate.RERUN_LIMIT])
    def test_more_than_the_rerun_limit_keeps_all_first_failures(self) -> None:
        failed_suites = self.suites[:gate.RERUN_LIMIT]
        for suite in failed_suites:
            self.write(self.state / ("red-" + suite), "red")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"{gate.RERUN_LIMIT + 1} modules failed; this is a real breakage, not load", result.stdout)
        self.assertNotIn("Rerun alone:", result.stdout)
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), self.module_count)

    def test_red_gate_restores_tracked_debris_before_rollback(self) -> None:
        self.write(self.state / "modify-tracked", "modify")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assertIn("merge undone", result.stderr)
        self.assert_not_pushed()

    def test_failed_rollback_names_merge_commit_and_blocking_files(self) -> None:
        with mock.patch.object(gate, "git", return_value=subprocess.CompletedProcess([], 1, " M unit.txt", "blocked")), mock.patch.object(
                gate, "checked_git", side_effect=["merge-commit", "unit.txt\0"]):
            with self.assertRaisesRegex(GateRefused, "merge NOT undone; merge commit merge-commit; blocking files: M unit.txt"):
                gate.rollback_merge(self.checkout, self.base)


if __name__ == "__main__":
    _ = unittest.main()
