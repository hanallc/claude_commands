"""BRP launch admission uses the shared memory gate and a temporary hold cycle."""

from __future__ import annotations

from scripts.build_queue import app_gate, admission_records

from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission, only_admitted
from scripts.build_queue import admission_protocol as wire
from scripts.lint import memory_admit as gate
from unittest.mock import patch
import time
from scripts.buildlog import store
from scripts.lib import conf

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast, override

from scripts.lint.memory_test_support import admission_policy_fixture, fake_memory_readings
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.py_floor_test_support import seed_interpreter_cache


HOOK = Path(__file__).with_name("pre-tool-use-brp-launch-gate.sh")
SETTINGS = HOOK.parent.parent.parent / "settings.json"
CYCLE_ID = "a" * 32


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


class PendingLaunchSession(TypedDict):
    session_id: str
    name: str
    state: str


class PendingLaunchCycle(TypedDict):
    release_started_at: str
    entries: list[PendingLaunchSession]


class BrpLaunchGateTests(unittest.TestCase):
    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))
    root: Path = Path()
    environment: dict[str, str] = {}
    cycle_file: Path = Path()
    hook: Path = HOOK

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        holders = self.root / "holders"
        release = self.root / "release"
        holders.mkdir()
        (release / CYCLE_ID).mkdir(parents=True)
        _ = (holders / "holder").write_text(json.dumps({
            "holder": "holder", "since": datetime.now().astimezone().isoformat(),
            "for": "the test", "release_eta": "unknown",
        }) + "\n")
        _ = (release / "current").write_text(CYCLE_ID + "\n")
        self.cycle_file = release / CYCLE_ID / "cycle.json"
        self.write_cycle()
        (holders / "holder").unlink(missing_ok=True)
        self.environment = {
            **os.environ,
            "HOME": str(self.root),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "BUILD_HOLD_DIR": str(holders),
            "BUILD_HOLD_RELEASE_DIR": str(release),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILD_TARGETS_SCRIPT": str(HOOK.parent.parent / "build_targets" / "build_targets.py"),
            **fake_memory_readings(self.root),
            "BUILDLOG_MEM_POLL_S": "0.01",
            "STAFF_FILE": str(self.root / "staff.json"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "BUILDLOG_DIR": str(self.root / "buildlog"),
        }
        _ = self.environment.pop("CLAUDE_PYTHON", None)
        seed_interpreter_cache(self.root / "cache")
        self.admission = caller_admission(self, self.root, self.environment)

    def write_cycle(self) -> None:
        now = datetime.now().astimezone().isoformat()
        cycle: dict[str, object] = {
            "id": CYCLE_ID, "opened_at": now, "release_started_at": now,
            "holders": {"holder": {"since": now, "released_at": ""}},
            "recipients": {"unit-worker": "unit"},
            "entries": [{
                "session_id": "unit-worker", "name": "unit", "state": "ReleasedAwaitingAdmission",
                "released_at": now,
            }],
        }
        _ = self.cycle_file.write_text(json.dumps(cycle) + "\n")

    def entry(self) -> dict[str, object]:
        cycle = cast(dict[str, object], json.loads(self.cycle_file.read_text()))
        entries = cast(list[dict[str, object]], cycle["entries"])
        return entries[0]

    def run_hook(
        self, session_id: str = "unit-worker", *, include_session: bool = True,
        tool_input: dict[str, object] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        hook_input: dict[str, object] = {
            "tool_name": "mcp__brp__brp_launch",
            "tool_input": tool_input if tool_input is not None else {
                "target_name": "other-app", "env": {},
            },
        }
        if include_session:
            hook_input["session_id"] = session_id
        return subprocess.run(
            ["bash", str(self.hook)], input=json.dumps(hook_input),
            capture_output=True, text=True, check=False, env=self.environment, timeout=TEST_HANG_GUARD_SECS,
        )

    def use_memory_wait_stub(self, body: str) -> None:
        hooks = self.root / "hooks"
        lint = self.root / "lint"
        hooks.mkdir()
        lint.mkdir()
        (self.root / "build_queue").symlink_to(HOOK.parent.parent / "build_queue", target_is_directory=True)
        (self.root / "lib").symlink_to(HOOK.parent.parent / "lib", target_is_directory=True)
        self.hook = hooks / HOOK.name
        _ = self.hook.write_text(HOOK.read_text())
        _ = (lint / "memory_gate.sh").write_text(
            "buildlog_wait_for_memory() {\n" + body + "\n}\n"
        )

    def test_hana_without_a_nonempty_purpose_is_denied_before_memory_wait(self) -> None:
        self.use_memory_wait_stub("printf 'memory wait reached\\n' >&2\nreturn 0")
        for environment in ({}, {"HANA_TEST_PURPOSE": ""}, {"HANA_TEST_PURPOSE": "   "}):
            with self.subTest(environment=environment):
                result = self.run_hook(tool_input={"target_name": "hana", "env": environment})
                self.assertEqual(result.returncode, 0, result.stderr)
                response = cast(HookResponse, json.loads(result.stdout))
                decision = response["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")
                self.assertIn("HANA_TEST_PURPOSE", decision["permissionDecisionReason"])
                self.assertIn("env", decision["permissionDecisionReason"])
                self.assertNotIn("memory wait reached", result.stderr)

    def test_hook_filename_in_its_folder_returns_the_denial(self) -> None:
        self.use_memory_wait_stub("printf 'memory wait reached\\n' >&2\nreturn 0")
        result = subprocess.run(
            ["bash", self.hook.name], cwd=self.hook.parent, env=self.environment,
            input=json.dumps({"tool_name": "mcp__brp__brp_launch",
                              "tool_input": {"target_name": "hana", "env": {}}}),
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = cast(HookResponse, json.loads(result.stdout))
        self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("HANA_TEST_PURPOSE", response["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertNotIn("memory wait reached", result.stderr)

    def test_hana_with_a_purpose_requires_the_app_memory_gate(self) -> None:
        self.use_memory_wait_stub("printf 'labelled Hana reached memory wait\\n' >&2\nreturn 0")
        result = self.run_hook(tool_input={"target_name": "hana", "env": {"HANA_TEST_PURPOSE": "CI icon"}})
        self.assertEqual(result.returncode, 0, result.stderr)
        response = cast(HookResponse, json.loads(result.stdout))
        self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("hana_shot.py launch --purpose <feature>", response["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(result.stderr, "")

    def test_another_app_needs_no_hana_purpose(self) -> None:
        self.use_memory_wait_stub("printf 'other app reached memory wait\\n' >&2\nreturn 0")
        result = self.run_hook(tool_input={"target_name": "other-app", "env": {}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "other app reached memory wait\n")

    def test_aged_launch_without_a_reservation_still_holds_for_free_memory(self) -> None:
        with self.admission.loop_turn():
            busy = only_admitted(self.admission.server.handle_request(
                self.admission.request(run=self.admission.run_owner())))
            self.admission.allow_next_start()
        self.admission.free(4 * admission_records.GIB)
        self.admission.compiler(8 * admission_records.GIB)
        self.environment.update(BUILDLOG_MEM_WAIT_LIMIT_S="0",
            BUILDLOG_MEM_WAIT_SINCE=str(self.admission.clock.now() - admission_policy_fixture().timing.estimate_refresh_seconds - 1))
        result = self.run_hook()
        self.assert_denied(result,
            "BRP launch memory check failed (exit 2): memory admission check failed "
            + "(launch wait limit reached without admission); step refused")
        with self.admission.loop_turn():
            self.assertEqual([record.reservation_id for record in self.admission.server.reservations.values()],
                             [busy.reservation])
            self.assertEqual(self.admission.server.handle_request(
                wire.ReleaseRequest("release-busy", busy.reservation, 0)), [wire.Ok()])
        self.assertEqual(self.admission.server.reservations, {})
        self.assertEqual(self.admission.server.waiters, {})

    def test_failed_memory_wait_denies_launch_with_its_last_reason_line(self) -> None:
        self.use_memory_wait_stub(
            "printf 'memory check started\\nchecker could not read meminfo\\n' >&2\nreturn 7"
        )
        self.assert_denied(
            self.run_hook(),
            "BRP launch memory check failed (exit 7): checker could not read meminfo",
        )

    def test_failed_memory_wait_without_output_denies_launch_with_exit_status(self) -> None:
        self.use_memory_wait_stub("return 9")
        self.assert_denied(self.run_hook(), "BRP launch memory check failed (exit 9)")

    def test_live_pressure_reading_denies_launch_with_the_tripwire_reason(self) -> None:
        record = self.root / "expected-live-memory-read.jsonl"
        self.environment["BUILDLOG_LIVE_MEMORY_TRIPWIRE"] = str(record)
        del self.environment["BUILDLOG_MEMORY_PRESSURE"]
        reason = (
            "live memory reading /proc/pressure/memory in a test; "
            + "fake BUILDLOG_MEMINFO and BUILDLOG_MEMORY_PRESSURE (scripts/lint/memory_test_support.py)"
        )
        with patch.dict(os.environ, {"BUILDLOG_LIVE_MEMORY_TRIPWIRE": str(record),
                                     "BUILDLOG_MEMORY_PRESSURE": "/proc/pressure/memory"}):
            with self.assertRaises(gate.LiveMemoryReading):
                _ = gate.read_pressure(policy=admission_policy_fixture())
        self.admission.server.reply_filter = lambda reply: wire.Refused(reason)
        self.assert_denied(
            self.run_hook(),
            f"BRP launch memory check failed (exit 2): memory admission check failed ({reason}); step refused",
        )
        rows = record.read_text().splitlines()
        self.assertEqual(len(rows), 1)
        row = cast(dict[str, object], json.loads(rows[0]))
        self.assertEqual(row["reading"], "/proc/pressure/memory")

    def test_allowed_memory_wait_passes_launch_without_a_deny_answer(self) -> None:
        self.use_memory_wait_stub("printf 'memory admitted\\n' >&2\nreturn 0")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "memory admitted\n")

    def test_low_memory_wait_leaves_old_release_record_unchanged(self) -> None:
        with self.admission.loop_turn():
            _ = only_admitted(self.admission.server.handle_request(
                self.admission.request(run=self.admission.run_owner())))
            self.admission.allow_next_start()
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 8388608 kB\n")
        states = self.root / "wait-states"

        def free_after_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.Wait):
                _ = states.write_text(str(self.entry()["state"]) + "\n")
                self.admission.free(60 * admission_records.GIB)
            return reply

        self.admission.server.reply_filter = free_after_wait

        before = self.cycle_file.read_bytes()
        result = self.run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("waiting for memory", result.stderr)
        self.assertEqual(states.read_text().splitlines(), ["ReleasedAwaitingAdmission"])
        self.assertEqual(self.cycle_file.read_bytes(), before)

    def assert_denied(self, result: subprocess.CompletedProcess[str], reason: str) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        response = cast(HookResponse, json.loads(result.stdout))
        decision = response["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(decision["permissionDecisionReason"], reason)
        self.assertNotIn("waiting for memory", result.stderr)

    def prepare_hold_before_release(self) -> None:
        _ = (Path(self.environment["BUILD_HOLD_DIR"]) / "holder").write_text(json.dumps({
            "holder": "holder", "since": datetime.now().astimezone().isoformat(),
            "for": "the test", "release_eta": "unknown",
        }))
        cycle = cast(PendingLaunchCycle, json.loads(self.cycle_file.read_text()))
        cycle["release_started_at"] = ""
        cycle["entries"] = [{"session_id": "unit-worker", "name": "unit", "state": "AwaitingRelease"}]
        _ = self.cycle_file.write_text(json.dumps(cycle))

    def test_holder_session_launch_passes_through_without_a_deny_answer(self) -> None:
        self.prepare_hold_before_release()
        holder_file = Path(self.environment["BUILD_HOLD_DIR"]) / "holder"
        before = (self.cycle_file.read_bytes(), holder_file.read_bytes())
        # The hook input's session id equals the holder file's name.
        result = self.run_hook("holder")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual((self.cycle_file.read_bytes(), holder_file.read_bytes()), before)
        self.assertEqual(self.entry().get("delivery", "NotAttempted"), "NotAttempted")
        self.assertNotIn("outcome", self.entry())

    def test_another_session_launch_is_denied_without_memory_wait_or_mark(self) -> None:
        self.prepare_hold_before_release()
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 1 kB\n")
        self.assert_denied(self.run_hook("unit-worker"),
                           "natedev is held by holder for the test; launch after the release.")
        self.assertEqual(self.entry().get("delivery", "NotAttempted"), "NotAttempted")
        self.assertNotIn("outcome", self.entry())
        self.assertNotIn("wait_started_at", self.entry())

    def test_unreleased_session_is_denied_without_memory_wait_or_mark(self) -> None:
        self.prepare_hold_before_release()
        cycle = cast(dict[str, object], json.loads(self.cycle_file.read_text()))
        entries = cast(list[dict[str, object]], cycle["entries"])
        entries[0] = {"session_id": "unit-worker", "name": "unit", "state": "AwaitingRelease"}
        _ = self.cycle_file.write_text(json.dumps(cycle))
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 1 kB\n")
        self.assert_denied(self.run_hook(), "natedev is held by holder for the test; launch after the release.")
        self.assertEqual(self.entry().get("delivery", "NotAttempted"), "NotAttempted")
        self.assertNotIn("outcome", self.entry())

    def test_missing_session_id_before_release_is_denied_without_memory_wait_or_mark(self) -> None:
        self.prepare_hold_before_release()
        self.environment["CLAUDE_CODE_SESSION_ID"] = "unit-worker"
        cycle = cast(dict[str, object], json.loads(self.cycle_file.read_text()))
        cycle["release_started_at"] = ""
        _ = self.cycle_file.write_text(json.dumps(cycle))
        before = self.cycle_file.read_bytes()
        self.assert_denied(self.run_hook(include_session=False), "natedev is held by holder for the test; launch after the release.")
        self.assertEqual(self.cycle_file.read_bytes(), before)

    def test_missing_session_id_during_release_is_admitted_without_a_mark(self) -> None:
        self.environment["CLAUDE_CODE_SESSION_ID"] = "unit-worker"
        before = self.cycle_file.read_bytes()
        result = self.run_hook(include_session=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.cycle_file.read_bytes(), before)

    def test_unheld_launch_passes_through(self) -> None:
        (Path(self.environment["BUILD_HOLD_DIR"]) / "holder").unlink(missing_ok=True)
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unreadable_hold_state_denies_launch_without_memory_admission(self) -> None:
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        (holders / "holder").unlink(missing_ok=True)
        holders.rmdir()
        _ = holders.write_text("not a directory")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        response = cast(HookResponse, json.loads(result.stdout))
        decision = response["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("hold state cannot be read", decision["permissionDecisionReason"])
        self.assertNotIn("waiting for memory", result.stderr)
        self.assertEqual(self.entry()["state"], "ReleasedAwaitingAdmission")

    def test_unreadable_release_record_denies_launch_without_repairing_it(self) -> None:
        self.prepare_hold_before_release()
        _ = self.cycle_file.write_text("{broken")
        release = Path(self.environment["BUILD_HOLD_RELEASE_DIR"])
        holder = Path(self.environment["BUILD_HOLD_DIR"]) / "holder"
        before = (holder.read_bytes(), (release / "current").read_bytes())
        result = self.run_hook()
        response = cast(HookResponse, json.loads(result.stdout))
        self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.cycle_file.read_text(), "{broken")
        self.assertEqual((holder.read_bytes(), (release / "current").read_bytes()), before)
        self.assertEqual(list(release.rglob("*.damaged-*")), [])
        self.assertNotIn("waiting for memory", result.stderr)

    def test_failed_hold_check_without_output_denies_with_a_reason(self) -> None:
        helper = self.root / "failed-hold-check.py"
        _ = helper.write_text("raise SystemExit(12)\n")
        self.environment["BUILD_TARGETS_SCRIPT"] = str(helper)
        before = self.cycle_file.read_bytes()
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        response = cast(HookResponse, json.loads(result.stdout))
        decision = response["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertTrue(decision["permissionDecisionReason"].strip())
        self.assertNotIn("waiting for memory", result.stderr)
        self.assertEqual(self.cycle_file.read_bytes(), before)

    def test_enabled_hana_hook_requires_a_grant_and_bounds_its_wait(self) -> None:
        for outcome, check_seconds in (("grant", 0.1), ("refuse", 0.1), ("wait-limit", 0.1), ("wait-limit-slow", 0.25)):
            with self.subTest(outcome=outcome, check_seconds=check_seconds):
                workspace = self.root / outcome
                (workspace / "crates/hana").mkdir(parents=True)
                now = [0.0]
                settings = conf.ConfigRead("private", self.root / "private.conf", {
                    "brp_adopt_wait_seconds": conf.Setting("1", "# range: 0..900", 1),
                    "brp_adopt_handoff_seconds": conf.Setting("2", "# range: 0..300", 2),
                    "brp_adopt_check_seconds": conf.Setting(str(check_seconds), "# range: 0.05..5", 3),
                }, False)
                directories: list[Path] = []

                def start(launch: app_gate.AdoptLaunch, directory: Path, wait: float, handoff: float) -> None:
                    self.assertEqual((launch.instances, wait, handoff), (2, 1, 2))
                    self.assertEqual(launch.binary, workspace / "target/debug/hana")
                    directories.append(directory)
                    _ = (directory / "status").write_text("queue place 4: need exceeds room")
                    if outcome == "grant":
                        _ = (directory / "granted").write_text("reservation")
                    if outcome == "refuse":
                        _ = (directory / "refused").write_text("fixture policy refuses this app")

                def tick(seconds: float) -> None:
                    self.assertAlmostEqual(seconds, min(check_seconds, 1 - now[0]))
                    now[0] += seconds

                with (patch.object(app_gate, "BRP_ADOPTION_ENABLED", True),
                      patch.object(app_gate, "start_adopter", side_effect=start),
                      patch.object(conf, "read", return_value=settings),
                      patch.object(store, "root", return_value=self.root / "buildlog"),
                      patch.object(time, "monotonic", side_effect=lambda: now[0]),
                      patch.object(time, "sleep", side_effect=tick)):
                    reason = app_gate.pre_launch({"tool_use_id": outcome, "cwd": str(workspace),
                        "tool_input": {"target_name": "hana", "instance_count": 2,
                                       "env": {"HANA_TEST_PURPOSE": "feature"}}})
                if outcome == "grant":
                    self.assertEqual(reason, "")
                elif outcome == "refuse":
                    self.assertEqual(reason, "fixture policy refuses this app")
                else:
                    self.assertIn("queue place 4: need exceeds room", reason)
                    self.assertTrue((directories[0] / "cancel").exists())
                    self.assertLessEqual(now[0], 1 + check_seconds)

    def test_post_hook_rejects_missing_or_malformed_results_without_losing_the_hold(self) -> None:
        for index, response in enumerate((None, {}, {"instances": []}, {"instances": [{"pid": "bad"}]})):
            tool_id = f"post-{index}"
            with patch.object(store, "root", return_value=self.root / "buildlog"):
                directory = app_gate.adoption_path(tool_id)
            directory.mkdir(parents=True)
            result = subprocess.run(["bash", str(HOOK.with_name("post-tool-use-brp-launch-hold.sh"))],
                input=json.dumps({"tool_use_id": tool_id, "tool_response": response}), env=self.environment,
                text=True, capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIsInstance(app_gate.read_handoff(directory), app_gate.FailedHandoff)

    def test_post_handoff_saves_live_identities_and_accepts_already_ended_pids(self) -> None:
        for ended in (False, True):
            with self.subTest(ended=ended), patch.object(store, "root", return_value=self.root / "buildlog"):
                tool_id = f"handoff-{ended}"
                directory = app_gate.adoption_path(tool_id)
                directory.mkdir(parents=True)
                _ = (directory / "app.adopt").write_text(json.dumps({"owner_pid": 100, "owner_start": "owner", "processes": {}}))
                processes = {} if ended else {101: gate.ProcessIdentity(1, "start"), 102: gate.ProcessIdentity(101, "child")}
                with patch.object(gate, "process_table", return_value=processes):
                    app_gate.publish_handoff({"tool_use_id": tool_id, "tool_response": {"instances": [{"pid": 101}]}}, self.root / "proc")
                handoff = app_gate.read_handoff(directory)
                self.assertEqual(handoff, app_gate.AdoptedProcesses(() if ended else (wire.Owner(101, "start"), wire.Owner(102, "child"))))

    def test_settings_registers_hook_with_long_enough_timeout(self) -> None:
        settings = cast(dict[str, object], json.loads(SETTINGS.read_text()))
        hooks = cast(dict[str, list[dict[str, object]]], settings["hooks"])
        registrations = [item for item in hooks["PreToolUse"] if item.get("matcher") == "mcp__brp__brp_launch"]
        self.assertEqual(len(registrations), 1)
        commands = cast(list[dict[str, object]], registrations[0]["hooks"])
        self.assertEqual(len(commands), 1)
        self.assertIn("pre-tool-use-brp-launch-gate.sh", cast(str, commands[0]["command"]))
        self.assertGreater(cast(int, commands[0]["timeout"]), 900)
        settings = cast(dict[str, object], json.loads(SETTINGS.read_text()))
        hooks = cast(dict[str, list[dict[str, object]]], settings["hooks"])
        post = [entry for entry in hooks["PostToolUse"] if entry.get("matcher") == "mcp__brp__brp_launch"]
        self.assertEqual(len(post), 1)
        post_commands = cast(list[dict[str, object]], post[0]["hooks"])
        self.assertIn("post-tool-use-brp-launch-hold.sh", str(post_commands[0]["command"]))


if __name__ == "__main__":
    _ = unittest.main()
