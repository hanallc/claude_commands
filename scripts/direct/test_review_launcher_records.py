"""Check review passes and turns are recorded in the requested phase."""

from __future__ import annotations


from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

import unittest
from scripts.direct import review_launcher_test_support as support
from scripts.direct.wait_for_test_state import wait_until
from typing import cast
import json
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


class ReviewLauncherPassTests(support.ReviewLauncherFixture):

    def test_plain_reviews_of_two_phases_write_no_turn_records(self) -> None:
        session = self.start_implementation("two-phases")
        self.finish_implementation(session)
        first = self.run_claude_review(session)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.turn_records(session), [])
        self.recorder("finish-phase", "--session-dir", str(session), "--status", "completed")
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", 'Next "phase"')
        second = self.run_claude_review(session)
        self.assertEqual(second.returncode, 0, second.stderr)
        records = self.turn_records(session)
        self.assertEqual(records, [])

    def test_plain_ux_review_writes_no_turn_record_or_pass(self) -> None:
        session = self.start_implementation("own-ux")
        self.finish_implementation(session)
        environment = self.environment()
        self.trace_pass_recorder(environment)
        result = subprocess.run(self.review_command(session, "ux", ""), env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session), [])
        self.assertEqual(self.call_records("pass-calls.jsonl"), [])

    def test_a_review_naming_an_older_open_phase_starts_its_pass_in_that_phase(self) -> None:
        session = self.start_implementation("named-phase-pass")
        self.finish_implementation(session)
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", "Next phase")
        environment = self.environment()
        self.trace_pass_recorder(environment)
        command = self.review_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        start, = [call for call in self.call_records("pass-calls.jsonl") if call["verb"] == "start-pass"]
        self.assertEqual(cast("list[str]", start["argv"])[-2:], ["--phase-id", "3"])

    def test_matching_lenses_keep_their_phase_and_pass_on_completion(self) -> None:
        session = self.start_implementation("parallel-review-completion")
        self.finish_implementation(session)
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", "Next phase")

        def reviews(verb: str) -> list[dict[str, object]]:
            return [event for event in self.pass_events(session.name)
                    if event["event_type"] == verb and event["pass_kind"] == "review"]

        with ExitStack() as processes:
            def launch(phase: str, name: str) -> tuple[subprocess.Popen[str], Path]:
                entered = self.root / f"{name}-entered"
                release = self.root / f"{name}-release"
                environment = {**self.environment(), "TEST_WORKER_STARTED": str(entered),
                               "STUB_REVIEW_FINISH_FILE": str(release)}
                command = self.review_command(session)
                command[2:2] = ["--phase", phase]
                process = processes.enter_context(subprocess.Popen(
                    command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                _ = processes.callback(release.touch)
                wait_until(entered.exists, self.root, processes=[process])
                return process, release

            first, release_first = launch("3", "first")
            second, release_second = launch("4", "second")
            original, other = reviews("pass_started")
            self.assertNotEqual(original["team_slot"], other["team_slot"])
            self.assertEqual(reviews("pass_finished"), [])

            retry, release_retry = launch("3", "retry")
            _, _, replacement = reviews("pass_started")
            release_first.touch()
            _, error = first.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(first.returncode, 0, error)
            self.assertEqual([(event["pass_instance_id"], event["status"]) for event in reviews("pass_finished")],
                             [(original["pass_instance_id"], "interrupted")])

            for process, release in ((second, release_second), (retry, release_retry)):
                release.touch()
                _, error = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 0, error)

        self.assertEqual([(event["phase_id"], event["pass_instance_id"], event["status"])
                          for event in reviews("pass_finished")],
                         [("3", original["pass_instance_id"], "interrupted"),
                          ("4", other["pass_instance_id"], "completed"),
                          ("3", replacement["pass_instance_id"], "completed")])

    def test_plain_review_of_an_older_phase_keeps_findings_without_a_turn_record(self) -> None:
        session = self.start_implementation("named-phase-turn")
        self.finish_implementation(session)
        older = cast(dict[str, object], json.loads(
            (session / "progress_history_state.json").read_text(encoding="utf-8"))["open_phases"][0])
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", "Next phase")
        environment = self.environment()
        environment["STUB_FINDINGS"] = "1. First finding\n2. Second finding\n"
        command = self.review_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session), [])
        current = cast(dict[str, object], json.loads(
            (session / "progress_history_state.json").read_text(encoding="utf-8"))["open_phases"][1])
        self.assertNotEqual(older["instance_id"], current["instance_id"])
        self.assertEqual((session / "phases/3/review_findings_1_contract.txt").read_text(),
                         environment["STUB_FINDINGS"])

    def test_a_review_naming_its_phase_writes_its_files_in_that_phase_directory(self) -> None:
        # Two phases reviewing at once each pass their own id, so neither reads the other's findings.
        session = self.start_implementation("phase-directory-files")
        self.finish_implementation(session)
        environment = self.environment()
        command = self.review_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        phase_dir = session / "phases" / "3"
        self.assertTrue(list(phase_dir.glob("review_findings_*.txt")))
        self.assertTrue(list(phase_dir.glob("review_status*")))
        self.assertEqual(sorted(path.name for path in session.glob("review_*")), [])


    def test_a_phase_id_with_a_space_names_a_directory_without_it(self) -> None:
        session = self.start_implementation("phase-directory-name")
        self.finish_implementation(session)
        command = self.review_command(session)
        command[2:2] = ["--phase", "ad hoc"]
        result = subprocess.run(command, env=self.environment(), capture_output=True, text=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertNotEqual(result.returncode, 2, result.stderr)
        self.assertTrue(list((session / "phases" / "adhoc").glob("review_status*")))

    def test_failed_plain_closure_keeps_findings_without_a_turn_record(self) -> None:
        session = self.start_implementation("own-failed")
        self.finish_implementation(session)
        environment = self.environment()
        environment["STUB_TURN_FAILURE"] = "1"
        environment["STUB_FINDINGS"] = "1. Partial finding\n"
        command = self.review_command(session, "", "")
        command[-3] = "2"
        result = subprocess.run(command, env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.turn_records(session), [])
        self.assertEqual((session / "review_findings_2.txt").read_text(), environment["STUB_FINDINGS"])
        self.assertEqual([event["status"] for event in self.pass_events(session.name)
                          if event.get("event_type") == "pass_finished"][-1], "error")

    def test_failed_plain_review_and_retry_keep_passes_without_turn_records(self) -> None:
        session = self.start_implementation("unit-review-retry")
        self.finish_implementation(session)
        environment = self.environment()
        environment["STUB_TURN_FAILURE"] = "1"
        failed = subprocess.run(self.review_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual((session / "review_status_contract").read_text().strip(), "error")
        self.assertEqual(self.turn_records(session), [])
        _ = environment.pop("STUB_TURN_FAILURE")
        retry = subprocess.run(self.review_command(session, "contract", ""), env=environment,
                               capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        records = self.turn_records(session)
        self.assertEqual(records, [])
        self.assertEqual([event["status"] for event in self.pass_events(session.name)
                          if event.get("event_type") == "pass_finished" and event.get("pass_kind") == "review"],
                         ["error", "completed"])

    def test_unit_review_still_starts_and_finishes_a_pass(self) -> None:
        session = self.start_implementation("code-pass")
        self.finish_implementation(session)
        environment = self.environment()
        self.trace_pass_recorder(environment)
        result = subprocess.run(self.review_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["verb"] for call in self.call_records("pass-calls.jsonl")],
                         ["start-pass", "finish-pass"])


    def test_plain_review_preserves_existing_turn_records_from_the_same_second(self) -> None:
        session = self.start_implementation("two-turn-records")
        self.finish_implementation(session)
        from scripts.direct import progress_history
        metadata = cast(dict[str, object], json.loads((session / "progress_history_state.json").read_text())["open_phases"][0])
        progress = {"phase": metadata["id"], "phase_instance_id": metadata["instance_id"]}
        with patch.dict("os.environ", self.environment()):
            for raised in (3, 7):
                progress_history.write_review_turn(session, progress, reviewer="existing reviewer", thread_id="existing-thread",
                                                   lens="contract", pass_number=1, started_at=100, status="reviewed",
                                                   raised=raised, details={})
        before = self.turn_records(session)
        result = subprocess.run(self.review_command(session), env=self.environment(),
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "review_status_contract").read_text().strip(), "reviewed")
        records = self.turn_records(session)
        self.assertEqual(records, before)
        self.assertEqual(len(records), 2)
        self.assertCountEqual([record["raised"] for record in records], [3, 7])
        self.assertEqual(records[0]["started_at"], records[1]["started_at"])
        for record in records:
            self.assertEqual(record["status"], "reviewed")
            self.assertTrue(record["thread_id"])
            self.assertEqual(record["thread_id"], records[0]["thread_id"])


if __name__ == "__main__":
    _ = unittest.main()
