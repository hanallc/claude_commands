"""Check feed configuration and publication with private queue readings."""
from __future__ import annotations

from scripts.review_links.browser_test_support import assert_status_states
from scripts.build_queue import admission_records, admission_service, execution_identity, limits_view, placement, queue_evidence, slot_wait
from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue.test_queue_view import with_converted_executions

import json
import io
from html import unescape
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.client import HTTPConnection
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import tempfile
import time
import unittest
from typing import Literal, cast, override
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.aws_target import config as aws_config, hana_result, ledger
from scripts.aws_target.hana_generation import HanaHostGeneration
from scripts.aws_target.ledger_test_support import install_cost_settings, numeric_settings, text_setting
from scripts.buildlog import aws_costs, charts, cost_actions, costs, index, machine_facts, parse, store
from scripts.buildlog.mac_idle_test_support import write_idle_alert, write_idle_config, write_live_mac_state
from scripts.build_queue import admission_client, admission_protocol as wire, cloud_progress, memory_feed, person_actions, step_estimator, progress_profile, queue_lines, queue_view, queue_snapshot, target_availability
from scripts.build_queue.admission_wire import ReservedCloudSlot
from scripts.build_queue import memory_trace
from scripts.build_queue.test_program_memory_feed import TRACE_POLICY
from scripts.build_queue.test_queue_view import cloud_reading
from scripts.build_queue.slot_wait import NoNamedStep, recorded_step
from scripts.build_queue.test_target_availability import MAC, NATEDEV, snapshot
from scripts.build_targets import build_targets, mac_linux, target_config, targets
from scripts.build_queue.queue_view import refresh_mac_builds
from scripts.build_queue.admission_test_support import (
    AdmissionFixture, FixedSystemReader, NOW as ADMISSION_NOW, only_admitted, publish_fixture_view,
)
from scripts.build_queue.queue_fixture import fixture_reservation, write_process, write_scope
from scripts.build_queue.scope_activity import (RunningServiceJob, IdleService, LiveBuildRun, NoLiveBuildRun, NotServiceSlice,
                                                ScopeProcess, ScopeProcesses)
from scripts.lint import test_memory_admit as memory_tests
from scripts.lint.memory_test_support import write_pressure_reading
from scripts.lib import atomic_file, build_folders, conf, test_hang_guard
from scripts.lib.build_slots_test_support import write_private_build_roots
from scripts.lib.process_group import run_process_group
from scripts.review_links import console_theme, page_frame, review_link, test_page_frame, test_serve
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, HAND_MADE, GateJobNotRecorded, StepJob
from scripts.lib.build_steps import OTHER_COMMAND, RECORDED_STEP_NAMES, RECORDED_VERBS, StepNames, command_job, read_step_names
from scripts.message import unit_labels
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, isolated_staff_file
from scripts.production.gate import SuiteMode
from scripts.review_links import staff_names
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import admission_policy_fixture, write_config_fixture
from scripts.mac_test.claim_state import RepositoryScope

REQUEST_TOKEN_DUMP = memory_feed.request_token_dump
STEVIE_OUTPUT = memory_feed.stevie_output
FIXTURE_GUEST_REPORT_SECONDS = 2
FIXTURE_GUEST_START_LIMIT = mac_linux.TRANSFER_ALLOWANCE_SECONDS + FIXTURE_GUEST_REPORT_SECONDS
UNIT_LABELS_FOR = unit_labels.unit_labels_for
STEVIE = Path("/fixture/bin/stevie")
FIXTURE_MACHINE = "fixture"


def needs_all_page_proof_views(repo: Path) -> bool:
    """Use all views when the checkout lease shows nightly work."""
    try:
        lease = cast(object, json.loads(build_folders.lease_path(repo).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False
    return isinstance(lease, dict) and cast(dict[str, object], lease).get("work") == "test-nightly"


class FeedStopped(Exception):
    """The fixture stops the feed after its recorded cycles."""


AWS_COST_TIME = int(datetime(2026, 1, 2, 12, tzinfo=UTC).timestamp())
AWS_COST_JOB = StepJob("integrate", "research", "sdlc")
PRIVATE_AWS_FIELDS = {
    "account_id": "111122223333", "instance_id": "i-0123abcdef", "subnet": "subnet-0123abcdef",
    "volume": "vol-0123abcdef", "snapshot": "snap-0123abcdef",
    "arn": "arn:aws:ec2:us-east-1:111122223333:snapshot/snap-0123abcdef",
    "recipient": "recipient@example.invalid", "email": "notification@example.invalid",
}


def write_aws_cost_fixture(root: Path, *, unknown_unit: str = "", instant: int = AWS_COST_TIME,
                           first_job: StepJob | GateJobNotRecorded | None = AWS_COST_JOB) -> ledger.CostLedger:
    """Save a bill, three finished runs, and the latest fixed costs."""
    settings = root / "aws_target.conf"
    install_cost_settings(settings)
    text_setting(settings, "notification_recipient", PRIVATE_AWS_FIELDS["recipient"])
    _ = (root / "build_steps.conf").write_text(
        "[names]\nnames.test-affected=test affected\nnames.integrate=integrate\nnames.merge-gate=gate\n")
    book = ledger.CostLedger(root / "target-state" / "aws")
    midnight, _ = ledger.period_bounds(datetime.fromtimestamp(instant, UTC).date(), ZoneInfo("UTC"))
    start = midnight - 86400
    book.record_inventory(ledger.ResourceInventory(start,
        (ledger.PricedResource("gate-shared", Decimal("1.00")),
         ledger.PricedResource(PRIVATE_AWS_FIELDS["volume"], Decimal("24.80"))),
         PRIVATE_AWS_FIELDS["snapshot"], builder_removed=True))
    with book.transaction() as state:
        state.hana_storage.append(ledger.HanaStorageExposure(start, Decimal("27.20")))
        state.charge_refresh_at = midnight
    charges = book.directory / "charges"
    charges.mkdir(exist_ok=True)
    bill_day = datetime.fromtimestamp(start, UTC).date().isoformat()
    bill = ledger.ChargeRead(bill_day, midnight, start, midnight, Decimal(12), Decimal("13.57"), Decimal(0),
        (ledger.UsageTypeCost("USE1-SpotUsage:m7a.8xlarge", Decimal(8), Decimal(1), "Hrs"),
         ledger.UsageTypeCost("USE1-EBS:SnapshotUsage", Decimal(4))), provisional=False)
    _ = (charges / (bill.day + ".json")).write_text(json.dumps(ledger.charge_record(bill)))
    rows: list[dict[str, object]] = []
    for index, (unit, mode, job, seconds) in enumerate((("", "merge-gate", first_job, 120),
            (unknown_unit, "merge-gate", GATE_JOB_NOT_RECORDED, 60),
            ("report", "targeted-test", StepJob("test-affected", unit="other"), 60))):
        launch: dict[str, object] = {**PRIVATE_AWS_FIELDS, "attempt": f"fixture-{index}", "instance_type": "m7a.8xlarge",
            "region": "us-east-1", "time": start + 60, "mode": mode, "result": "launched"}
        if not index:
            launch["availability_zone"] = "us-east-1a"
        if job is not None:
            launch["gate_job"] = codec.snapshot_value(job)
        if unit:
            launch["unit"] = unit
        rows.extend((launch, {**launch, "time": start + 60 + seconds, "result": "finished", "gate_s": 20}))
    _ = book.launch_history_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return book


def captured_reading(*, running: tuple[admission_records.RunningStep, ...] = (),
                     host: admission_records.HostMemory | None = None) -> admission_records.QueueRead:
    host = host or admission_records.HostMemory(30 * admission_records.GIB, 60 * admission_records.GIB)
    inputs = admission_records.QueueInputs([], running, (), host,
        admission_records.NO_PRESSURE_HOLD, admission_records.ReadyToStart(), 200_000)
    return gate.queue_read_from(inputs, admission_policy_fixture())


def with_queue_priority(reading: admission_records.QueueRead, units: tuple[str, ...] = ("research",)
                        ) -> admission_records.QueueRead:
    """Add a private user priority to the reading."""
    priority = admission_records.QueuePriority(tuple({"kind": "UnitPriorityEntry", "unit": unit, "why": "the user's bump",
        "set_at": reading.explanation.now, "set_by": "fixture-user"} for unit in units))
    return replace(reading, explanation=replace(reading.explanation, priority=priority))


def queued_reading(reading: admission_records.QueueRead, units: tuple[str, ...]) -> admission_records.QueueRead:
    """Supply an ordered queue with repeated units and an older normal step."""
    waiting = tuple(admission_records.WaitingStep(200 + index, f"wait-{index}", unit, "nextest",
        f"/fixture/{unit}", 100 - index * 10, admission_records.GIB, admission_records.NeedsMemory(), unit=unit)
        for index, unit in enumerate(("research", "research", "cables", "normal")))
    current = replace(reading, view=replace(reading.view, waiting=waiting))
    return with_queue_priority(current, units) if units else current


def report_fixture(*, empty: bool = False) -> charts.PageData:
    end = datetime(2026, 11, 8, tzinfo=UTC)
    rows = charts.NoDataYet.VALUE if empty else (charts.TopCost("fixture", "Memory wait",
        charts.MeasuredCost(120), charts.WindowDiffers(charts.MeasuredCost(240)),
        (charts.MeasuredCost(30), charts.MeasuredCost(60), charts.MeasuredCost(30)),
        cost_actions.LargestShare(costs.StepKind("fixture", "nextest"), 0.75),
        cost_actions.LowerMemoryPeak(costs.StepKind("fixture", "nextest"), 12.4, 3 << 30)),)
    return charts.PageData(charts.TopCosts(end - timedelta(days=3), end, 3, rows))


def reservation_reading(*, project: str = "fixture-project") -> admission_records.QueueRead:
    reservation = memory_tests.ProgressKeepAsideTests.reservation("nextest")
    memory = admission_records.MeasuredMemory(2 * admission_records.GIB, anonymous=admission_records.AnonymousRunUse(admission_records.GIB))
    inputs = memory_tests.ProgressKeepAsideTests.inputs((reservation,), (memory,))
    inputs = replace(inputs, running=(replace(inputs.running[0], project=project),))
    return gate.queue_read_from(inputs, admission_policy_fixture())


def with_wait_reason(reading: admission_records.QueueRead, reason: admission_records.WaitReason) -> admission_records.QueueRead:
    waiter = admission_records.WaitingStep(200, "fixture", "fixture", "nextest", "/fixture", 10,
                              admission_records.GIB, reason, project="fixture-project")
    return replace(reading, view=replace(reading.view, waiting=(waiter,)))


@dataclass(frozen=True)
class GatePlacementWaiter(admission_records.WaitingStep):
    placement_candidates: tuple[placement.CandidateEligibility, ...] = ()


@dataclass(frozen=True)
class GatePlacementStep(admission_records.RunningStep):
    placement_candidates: tuple[placement.CandidateEligibility, ...] = ()


def gate_candidates(guest: placement.HostEligibility) -> tuple[placement.CandidateEligibility, ...]:
    scope = RepositoryScope("claude_commands")
    return (
        placement.CandidateEligibility(placement.PlacementDestination("local", "natedev", "natedev", scope),
                                       placement.AllowedHost()),
        placement.CandidateEligibility(placement.PlacementDestination("mac", "mac-linux", "guest", scope), guest),
        placement.CandidateEligibility(placement.PlacementDestination("scaler", "hana-aws", "aws", scope),
                                       placement.AllowedHost()),
    )


def leftover_step(minutes: int, *, small: bool = False, members: tuple[ScopeProcess, ...] = ()) -> admission_records.RunningStep:
    started_at = 200_000 - minutes * 60
    activity = ScopeProcesses(len(members) or 1, "fixture", "fixture", "/fixture", "python3", started_at,
                              True, NoLiveBuildRun(), members=members)
    memory = admission_records.UnownedSliceMemory(admission_records.GIB // 4 if small else admission_records.GIB,
        owner=f"leftover-{minutes}", activity=activity, pid=minutes)
    return admission_records.RunningStep("fixture", "fixture", f"leftover-{minutes}", "/fixture", 0,
                            memory, pid=minutes, started_at=started_at)


def fixture_published_view(reading: admission_records.QueueRead) -> queue_snapshot.PublishedView:
    return queue_snapshot.PublishedView(reading.explanation.now, reading.explanation.now, reading)


def write_outside_queue_fixture(root: Path, *, siblings: tuple[int, ...] = ()) -> staff.StaffSnapshot:
    """Write process trees and staff for a private CPU time reading."""
    _ = write_config_fixture(root / "test_times.conf",
        "[direct_test]\n# whole: 1..\ndirect_test.time_limit_seconds=120\n")
    runner = staff.StaffRow(staff.StaffId("builds"), "showrunner", "builds", staff.StaffRoot(),
                            current_session=staff.StaffId("runner-session"))
    director = staff.StaffRow(staff.StaffId("queue"), "unit", "queue", staff.ReportsTo(runner.id),
                              current_session=staff.StaffId("director-session"))
    worker = staff.StaffRow(staff.StaffId("test-worker"), "worker", "test", staff.ReportsTo(director.id),
                            current_session=staff.StaffId("worker-session"))
    scope = Path("user/app.slice/app-agents.slice/run-p8100.scope")
    ticks = os.sysconf("SC_CLK_TCK")
    for pid, state, parent, argv, cpu in (
        (8100, "S", 1, "/bin/bash\0--login\0--norc", (200, 0, 0, 0)),
        (8101, "S", 8100, "/bin/codex", (200, 0, 0, 0)),
        (8102, "S", 1, "/bin/python3\0/fixture/scripts/production/targeted_test.py", (41, 17, 23, 7)),
        (8103, "Z", 8105, "", (35, 13, 0, 0)),
        (8104, "S", 8101, "/bin/rustc", (80, 40, 0, 0)),
        (8105, "S", 8102, "/bin/python3", (0, 0, 0, 0)),
        (8107, "S", 8101, "/bin/pytest", (19, 0, 0, 0)),
        *((pid, "S", 8101, "/bin/pytest", (121, 0, 0, 0)) for pid in siblings),
    ):
        process = root / "proc" / str(pid)
        write_process(process.parent, pid, parent=parent)
        fields = [state, str(parent), *(["0"] * 9), *(str(value * ticks) for value in cpu), *(["0"] * 4), str(pid)]
        _ = (process / "stat").write_text(f"{pid} (fixture ) command) {' '.join(fields)}\n")
        _ = (process / "cmdline").write_text(argv + "\0")
        _ = (process / "cgroup").write_text(f"0::/{scope}\n")
    _ = (root / "proc/8101/environ").write_text("CODEX_THREAD_ID=worker-session\0")
    _ = (root / "proc/8102/environ").write_text("BUILDLOG_OWNER_STAFF_ID=builds\0")
    write_scope(root / "cgroup" / scope, members=(8100, 8101, 8102, 8104, 8105, 8107, *siblings))
    write_scope(root / "cgroup/user/builds.slice")
    return staff.StaffSnapshot((runner, director, worker))


def outside_queue_fixture_groups(root: Path) -> dict[str, str]:
    return {"BUILDLOG_USER_CGROUP": str(root / "cgroup/user"),
            "BUILDLOG_BUILDS_CGROUP": str(root / "cgroup/user/builds.slice")}


class MemoryFeedTests(unittest.TestCase):
    root: Path = Path()
    output: Path = Path()
    configuration: Path = Path()

    def test_outside_queue_reads_each_shared_ancestor_environment_once(self) -> None:
        siblings = tuple(range(8200, 8212))
        owners = memory_feed.FeedStaff(write_outside_queue_fixture(self.root, siblings=siblings))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, outside_queue_fixture_groups(self.root))))
        shell_env = self.root / "proc/8100/environ"
        agent_env = self.root / "proc/8101/environ"
        _ = shell_env.write_text("BUILDLOG_OWNER_STAFF_ID=test-worker\0")
        _ = agent_env.write_text("CODEX_THREAD_ID=unknown-session\0")
        reads: Counter[Path] = Counter()
        read_bytes = Path.read_bytes

        def count_bytes(path: Path) -> bytes:
            if path.name == "environ":
                reads[path] += 1
            return read_bytes(path)

        inputs = captured_reading().explanation.decided_from
        with mock.patch.object(Path, "read_bytes", new=count_bytes):
            rows = memory_feed.outside_queue_processes(inputs, owners)

        self.assertEqual([(row["pid"], row["owner"]) for row in rows if row["pid"] in siblings],
                         [(pid, "test") for pid in siblings])
        self.assertEqual(reads[agent_env], 1)
        self.assertEqual(reads[shell_env], 1)
        self.assertEqual(max(reads.values()), 1)

    def test_outside_queue_scans_a_child_after_its_parent_leaves_the_scope(self) -> None:
        owners = memory_feed.FeedStaff(write_outside_queue_fixture(self.root, siblings=(8200,)))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, outside_queue_fixture_groups(self.root))))
        # The membership file still lists the parent, but the parent's cgroup has changed.
        _ = (self.root / "proc/8101/cgroup").write_text("0::/user/app.slice/other.scope\n")

        rows = memory_feed.outside_queue_processes(captured_reading().explanation.decided_from, owners)

        self.assertEqual([(row["pid"], row["command"], row["cpu_seconds"]) for row in rows],
                         [(8102, "targeted_test.py", 136.0), (8200, "pytest", 121.0)])

    def test_outside_queue_marks_a_login_shell_that_execs_a_script(self) -> None:
        owners = memory_feed.FeedStaff(write_outside_queue_fixture(self.root))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, outside_queue_fixture_groups(self.root))))
        inputs = captured_reading().explanation.decided_from
        process = self.root / "proc/8102"
        stat = process / "stat"
        _ = stat.write_bytes(stat.read_bytes().replace(b"(fixture ) command)", b"(bash)", 1))
        cmdline = process / "cmdline"
        _ = cmdline.write_text("/bin/bash\0--login\0--norc\0")
        self.assertEqual(memory_feed.outside_queue_processes(inputs, owners), [])

        _ = cmdline.write_text("/bin/bash\0/fixture/long-build.sh\0")
        rows = memory_feed.outside_queue_processes(inputs, owners)
        self.assertEqual(rows, [
            {"pid": 8102, "unit": "queue", "owner": "test", "command": "long-build.sh", "cpu_seconds": 136.0},
        ])

    def test_cpu_time_marks_only_unadmitted_command_trees_over_the_limit(self) -> None:
        staff_members = write_outside_queue_fixture(self.root)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, outside_queue_fixture_groups(self.root))))
        reading = captured_reading()
        with (mock.patch.object(staff, "snapshot", return_value=staff_members),
              mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading))):
            page = memory_feed.reading()
        self.assertEqual(page["outside_queue"], [
            {"pid": 8102, "unit": "queue", "owner": "test", "command": "targeted_test.py", "cpu_seconds": 136.0},
        ])
        inputs = reading.explanation.decided_from
        owners = memory_feed.FeedStaff(staff_members)
        cmdline = self.root / "proc/8102/cmdline"
        original_command = cmdline.read_text()
        _ = cmdline.write_text("/bin/python3\0/fixture/agent_exec.py\0")
        self.assertEqual([(row["command"], row["owner"]) for row in memory_feed.outside_queue_processes(inputs, owners)],
                         [("agent_exec.py", "test")])
        _ = cmdline.write_text(original_command)

        user = self.root / "cgroup/user"
        scope = user / "builds.slice/build-fixture.scope"
        write_scope(scope)
        child_group = self.root / "proc/8105/cgroup"
        agent_path = child_group.read_text()
        _ = child_group.write_text(f"0::/{scope.relative_to(self.root / 'cgroup')}\n")
        self.assertEqual(memory_feed.outside_queue_processes(inputs, owners), [])
        config = self.root / "test_times.conf"
        _ = config.write_text("# whole: 1..\ndirect_test.time_limit_seconds=80\n")
        rows = memory_feed.outside_queue_processes(inputs, owners)
        self.assertEqual([(row["pid"], row["cpu_seconds"]) for row in rows], [(8104, 120.0), (8102, 88.0)])
        _ = child_group.write_text(agent_path)
        _ = config.write_text("# whole: 1..\ndirect_test.time_limit_seconds=120\n")

        write_scope(user / "app.slice/app-agents.slice/run-p8100.scope", members=(8100, 8101, 8104, 8107))
        run_slice = user / "builds.slice/builds-detached.slice/builds-detached-20261117T19250434733015b4c.slice"
        detached = run_slice / "detached-python-tests-20261117T192504-3473301-5b4c.scope"
        write_scope(detached, members=(8102, 8105))
        for pid in (8102, 8103, 8105):
            _ = (self.root / f"proc/{pid}/cgroup").write_text(f"0::/{detached.relative_to(self.root / 'cgroup')}\n")
        cpu_stat = detached / "cpu.stat"
        _ = cpu_stat.write_text("user_usec 90000000\nusage_usec 151500000\n")
        byte_reads: Counter[Path] = Counter()
        read_bytes = Path.read_bytes
        text_reads: Counter[Path] = Counter()
        read_text = Path.read_text

        def count_bytes(path: Path) -> bytes:
            byte_reads[path] += 1
            return read_bytes(path)

        def count_text(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            text_reads[path] += 1
            return read_text(path, encoding=encoding, errors=errors)

        with (mock.patch.object(Path, "read_bytes", new=count_bytes),
              mock.patch.object(Path, "read_text", new=count_text)):
            outside = memory_feed.outside_queue_processes(inputs, owners)
            self.assertEqual(text_reads[cpu_stat], 1)
            self.assertEqual(memory_feed.outside_queue_processes(inputs, owners), outside)
            self.assertEqual(text_reads[cpu_stat], 2)
        self.assertEqual([(row["pid"], row["owner"], row["cpu_seconds"]) for row in outside], [(8102, "builds", 151.5)])
        self.assertEqual(byte_reads[self.root / "proc/8102/environ"], 2)
        self.assertEqual(byte_reads[cmdline], 2)
        self.assertEqual(byte_reads[self.root / "proc/8105/cmdline"], 2)
        self.assertEqual(byte_reads[self.root / "proc/8107/cmdline"], 2)
        self.assertEqual(byte_reads[self.root / "proc/8107/environ"], 0)
        self.assertEqual(byte_reads[self.root / "proc/8101/environ"], 0)
        self.assertEqual(byte_reads[self.root / "proc/8102/stat"], 2)

        reservation = fixture_reservation(8105, admission_records.GIB)
        self.assertEqual(memory_feed.outside_queue_processes(replace(inputs, reservations=(reservation,)), owners), [])
        reservation["start_time"] = "old-start"
        self.assertEqual(memory_feed.outside_queue_processes(replace(inputs, reservations=(reservation,)), owners), outside)

        use = admission_records.AnonymousRunUse(0)
        for old_source in (admission_records.ScopeSource(scope, frozenset({8102})),
                           admission_records.ProcessSource((admission_records.ProcessAnonymousUse(8102, use),))):
            with self.subTest(source=old_source):
                memory = admission_records.MeasuredMemory(0, anonymous_source=old_source)
                step = admission_records.RunningStep("", "", "check", "/fixture", 0, memory, pid=8106)
                self.assertEqual(memory_feed.outside_queue_processes(replace(inputs, running=(step,)), owners), outside)

        write_scope(detached, members=(8102,))
        write_scope(scope, members=(8105,))
        _ = (self.root / "proc/8105/cgroup").write_text(f"0::/{scope.relative_to(self.root / 'cgroup')}\n")
        self.assertEqual(memory_feed.outside_queue_processes(inputs, owners), outside)

        with mock.patch.object(conf, "read", wraps=conf.read) as read:
            self.assertEqual(memory_feed.direct_test_time_limit_seconds(), 120.0)
            read.assert_not_called()
            _ = config.write_text("[direct_test]\n")
            errors = io.StringIO()
            with redirect_stderr(errors):
                self.assertEqual(memory_feed.outside_queue_processes(inputs, owners), [])
                self.assertEqual(memory_feed.outside_queue_processes(inputs, owners), [])
            read.assert_called_once_with(config)
            self.assertEqual(len(errors.getvalue().splitlines()), 1)
            self.assertIn("direct_test.time_limit_seconds missing", errors.getvalue())
        _ = config.write_text("# whole: 1..\ndirect_test.time_limit_seconds=130\n")
        self.assertEqual(memory_feed.outside_queue_processes(inputs, owners), outside)

        environ = self.root / "proc/8102/environ"
        _ = environ.write_text("BUILDLOG_OWNER_STAFF_ID=test-worker\0")
        byte_reads.clear()
        with mock.patch.object(Path, "read_bytes", new=count_bytes):
            rows = memory_feed.outside_queue_processes(inputs, owners)
            self.assertEqual(memory_feed.outside_queue_processes(inputs, owners), rows)
        self.assertEqual([(row["pid"], row["command"], row["owner"]) for row in rows],
                         [(8102, "targeted_test.py", "test")])
        self.assertEqual(byte_reads[cmdline], 2)
        self.assertEqual(byte_reads[environ], 2)

        queued = run_slice / "cargo.scope"
        write_scope(queued, members=(8105,))
        _ = (queued / "cpu.stat").write_text(cpu_stat.read_text())
        _ = child_group.write_text(f"0::/{queued.relative_to(self.root / 'cgroup')}\n")
        source = admission_records.ScopeSource(queued, frozenset({8105}))
        memory = admission_records.MeasuredMemory(0, anonymous_source=source)
        step = admission_records.RunningStep("", "", "check", "/fixture", 0, memory, pid=8105)
        self.assertEqual(memory_feed.outside_queue_processes(replace(inputs, running=(step,)), owners), rows)

        dust_pid = 8108
        proc = self.root / "proc"
        write_process(proc, dust_pid, name="dust")
        _ = (proc / str(dust_pid) / "cmdline").write_text("/bin/dust\0")
        for name in ("run-r0001.scope", "detached-other-20261117T192504-3473301-5b4d.scope"):
            with self.subTest(sibling=name):
                sibling = run_slice / name
                write_scope(sibling, members=(dust_pid,))
                _ = (sibling / "cpu.stat").write_text(cpu_stat.read_text())
                _ = (proc / str(dust_pid) / "cgroup").write_text(f"0::/{sibling.relative_to(self.root / 'cgroup')}\n")
                self.assertEqual(memory_feed.outside_queue_processes(replace(inputs, running=(step,)), owners), rows)

    def test_each_reading_shares_current_staff_role_names_with_the_page(self) -> None:
        runner = staff.StaffRow(staff.StaffId("fixture-runner"), "showrunner", "fixture-runner", staff.StaffRoot(),
                                current_session=staff.StaffId("fixture-runner"))
        owner = unit_labels.make_labels("fixture-runner", unit_labels.RunsProduction("fixture-runner"), unit_labels.NoDirector())
        with mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((runner,))):
            page = self.owner_machine_page(owner)
        self.assertIn('data-color-text="role.showrunner"', page.get("staff_names", {}).get(runner.name, ""))
        self.assertNotIn("nate", page.get("staff_names", {}))

    def test_feed_reads_the_staff_record_once_across_destinations(self) -> None:
        reading = cloud_reading(with_wait_reason(reservation_reading(), admission_records.NeedsMemory()))
        build = queue_view.MacBuild(90082, "/fixture/hana", reading.explanation.now - 45, "cargo check", "hana")
        with (mock.patch.object(staff, "is_owner", return_value=True),
              mock.patch.object(staff, "_load", return_value=staff.StaffSnapshot(())) as load,
              mock.patch.object(unit_labels, "unit_labels_for", new=UNIT_LABELS_FOR),
              mock.patch.object(unit_labels, "newest_sessions", return_value=[]),
              mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(queue_view, "read_mac_builds", return_value=queue_view.MacBuildReading(
                  reading.explanation.now, (build,)))):
            page = memory_feed.reading()
        self.assertGreaterEqual(len(page["walk"]) + len(page["waiters"]), 5)
        load.assert_called_once_with()

    def test_the_token_pool_names_its_memory_pause(self) -> None:
        gib = admission_records.GIB
        pool = memory_feed.token_pool(13 * gib, STEVIE)
        self.assertEqual(pool, {"kind": "TokenPool", "total": 31, "free": 4, "memory_floor_bytes": 12 * gib, "paused": False})
        paused = memory_feed.token_pool(11 * gib, STEVIE)
        self.assertTrue(paused.get("paused"))
        for output, error in (("12288\n4\n", None), ("12288\nfour\n31\n", None), ("", ValueError("stevie exited 1: no device"))):
            with self.subTest(output=output, error=error), mock.patch.object(memory_feed, "stevie_output",
                    side_effect=error, return_value=output):
                unavailable = memory_feed.token_pool(11 * gib, STEVIE)
                self.assertEqual(unavailable["kind"], "TokenPoolUnavailable")

    def test_stevie_runs_from_the_configured_path_without_a_search(self) -> None:
        program = self.root / "programs" / "stevie"
        program.parent.mkdir()
        _ = program.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        program.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "empty")}):
            self.assertEqual(STEVIE_OUTPUT(program).split(), ["-a", "-t", "-j"])
            with self.assertRaisesRegex(ValueError, "stevie did not answer"):
                _ = STEVIE_OUTPUT(self.root / "missing" / "stevie")

    def test_the_newest_steve_dump_gives_the_tokens_of_each_process(self) -> None:
        journal = textwrap.dedent("""\
            1763270000.000000 natedev steve[1304]: steve: currently 20 tokens available out of 31
            1763270000.000001 natedev steve[1304]:   PID 50 (cargo build) holds 9 tokens:
            1763270010.000000 natedev steve[1304]: steve: currently 26 tokens available out of 31
            1763270010.000001 natedev steve[1304]:   PID 60 (cargo nextest run (x)) holds 3 tokens:
            1763270010.000002 natedev steve[1304]:     job 0x2b running for 1.5s
            1763270010.000003 natedev steve[1304]:   PID 61 holds 2 tokens:
            1763270010.000004 natedev steve[1304]:   1 processes waiting for a job token (in order):
            1763270010.000005 natedev steve[1304]:     PID 62 is performing a blocking read
            1763270011.000000 natedev steve[1304]: Warning: PID 63 (build-script-main) returned incorrect token value 0x2b
            1763270011.000001 natedev steve[1304]:   PID 64 holds 7 tokens:
            """)
        self.assertEqual(memory_feed.token_holders(journal), memory_feed.TokenHolders({60: 3, 61: 2}))
        self.assertIsInstance(memory_feed.token_holders("1763270011.0 natedev steve[1304]: Device closed by PID 5\n"),
                              memory_feed.TokenHoldersUnknown)

    def test_an_incomplete_steve_dump_gives_way_to_the_complete_one_before_it(self) -> None:
        complete = textwrap.dedent("""\
            1763270000.000000 natedev steve[1304]: steve: currently 25 tokens available out of 31
            1763270000.000001 natedev steve[1304]:   PID 50 (cargo build) holds 6 tokens:
            """)
        cut = "1763270010.000000 natedev steve[1304]: steve: currently 26 tokens available out of 31\n"
        self.assertEqual(memory_feed.token_holders(complete + cut), memory_feed.TokenHolders({50: 6}))
        self.assertEqual(memory_feed.token_holders(cut + "1763270010.000001 natedev steve[1304]:   PID 60 holds 3 tokens:\n"),
                         memory_feed.TokenHoldersUnknown("incomplete steve token dumps"))
        self.assertEqual(memory_feed.token_holders("1763270000.0 natedev steve[1304]: steve: currently 31 tokens available out of 31\n"),
                         memory_feed.TokenHolders({}))

    def test_tokens_go_to_the_nearest_running_step_above_each_holder(self) -> None:
        proc = self.root / "proc"
        for pid, parent in ((300, 200), (200, 100), (100, 1), (400, 1)):
            (proc / str(pid)).mkdir()
            _ = (proc / str(pid) / "stat").write_text(f"{pid} (rustc (x) y) S {parent} 0 0\n")
        holders = memory_feed.TokenHolders({300: 2, 200: 1, 400: 5, 999: 4})
        self.assertEqual(memory_feed.tokens_by_run(holders, {100, 200}), {200: 3})

    def test_local_running_rows_carry_their_tokens_only_when_counts_are_known(self) -> None:
        reading = reservation_reading()
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            unknown = memory_feed.reading()

        def rows(page: memory_feed.MemoryPage) -> list[dict[str, object]]:
            return [cast(dict[str, object], cast(object, row)) for row in page["walk"]]

        def local(row: dict[str, object]) -> bool:
            return row.get("machine") == "fixture" and not row.get("ci") and isinstance(row.get("pid"), int)

        pids = [row["pid"] for row in rows(unknown) if local(row)]
        self.assertTrue(pids)
        self.assertTrue(all("tokens" not in row for row in rows(unknown)))
        self.assertEqual(unknown["token_counts"], memory_feed.NO_TOKEN_DUMP_YET.reason)
        self.assertEqual(unknown["tokens"], {"kind": "TokenPool", "total": 31, "free": 4,
                                             "memory_floor_bytes": 12 * admission_records.GIB,
                                             "paused": unknown["available"] < 12})
        pid = pids[0]
        assert isinstance(pid, int)
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            known = memory_feed.reading(holders=memory_feed.TokenHolders({pid: 6}))
        self.assertEqual(known["token_counts"], "")
        self.assertEqual({row["pid"]: row["tokens"] for row in rows(known) if local(row) and "tokens" in row}, {pid: 6})
        self.assertTrue(all("tokens" not in row for row in rows(known) if local(row) and row["pid"] != pid))
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            idle = memory_feed.reading(holders=memory_feed.TokenHolders({pid: 0}))
        self.assertEqual({row["pid"]: row["tokens"] for row in rows(idle) if "tokens" in row}, {pid: 0})
        self.assertTrue(all("tokens" not in row for row in rows(known) if not local(row)))

    def test_program_memory_main_publishes_a_single_reservation(self) -> None:
        now = 10 * 86_400
        started = now - 8
        store = memory_trace.TraceStore(self.root / "buildlog" / "admission" / "memory_traces", TRACE_POLICY)
        writer = store.start(memory_trace.TraceHeader("live-trace", "a" * 64, (now - 6) * 1000, 2000, 10))
        _ = writer.append(memory_trace.CompileSample(2000, 7, memory_trace.CargoProgress(1, 2), "hana"))
        reservation: admission_records.Reservation = {
            **fixture_reservation(101, 10, step="nextest"), "repo": "hana", "reservation_id": "live-trace",
            "scoped": True, "admitted_at": datetime.fromtimestamp(started, UTC).isoformat(),
        }
        memory = admission_records.MeasuredMemory(8, anonymous=admission_records.AnonymousRunUse(8))
        step = admission_records.RunningStep("", "fixture", "nextest", "/fixture", 10, memory,
            pid=101, run=LiveBuildRun(101, "live-trace", started), started_at=started)
        inputs = admission_records.QueueInputs([], (step,), (reservation,),
            admission_records.HostMemory(30 * admission_records.GIB, 60 * admission_records.GIB),
            admission_records.NO_PRESSURE_HOLD, admission_records.ReadyToStart(), now)
        queue = gate.queue_read_from(inputs, admission_policy_fixture())
        with (mock.patch.object(memory_trace, "read_policy", return_value=TRACE_POLICY),
              mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(queue))):
            self.assertEqual(memory_feed.main(["--once"]), 0)
        page = cast(memory_feed.MemoryPage, json.loads((self.output / "memory.json").read_text()))
        row = next(cast(memory_feed.RunRow, row) for row in page["walk"] if row["pid"] == 101)
        view = row.get("program_memory")
        assert view is not None
        self.assertEqual(view["compile_reserve"], {"bytes": 10, "source": "live"})
        self.assertEqual(view["live"]["compile"]["peak"], [2000, 7])

    def test_the_feed_shows_why_token_counts_are_unknown_when_steve_refuses_a_dump(self) -> None:
        reading = reservation_reading()
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            self.assertEqual(memory_feed.main(["--once"]), 0)
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertEqual(page["token_counts"], "Job type reload is not applicable for unit steve.service.")

    def test_a_denied_token_dump_gives_a_plain_reason(self) -> None:
        denied = subprocess.CompletedProcess(memory_feed.STEVE_DUMP_COMMAND, 1, "",
                                             "Failed to reload steve.service: Access denied.\nSee system logs for details.")
        failed = subprocess.CompletedProcess(memory_feed.STEVE_DUMP_COMMAND, 1, "", "Unit steve.service not loaded.\nMore text.")
        for result, expected in ((denied, memory_feed.STEVE_DUMP_DENIED_REASON),
                                 (failed, "systemctl reload steve exited 1: Unit steve.service not loaded."),
                                 (subprocess.CompletedProcess(memory_feed.STEVE_DUMP_COMMAND, 0, "", ""), "")):
            with self.subTest(expected=expected), mock.patch.object(subprocess, "run", return_value=result):
                self.assertEqual(REQUEST_TOKEN_DUMP(), expected)

    def test_running_rows_keep_the_natedev_and_mac_run_folders(self) -> None:
        local_home = self.root / "local-home"
        mac_home = self.root / "remote-home"
        local_folder = str(local_home / "build/hana/unit/2")
        mac_folder = str(mac_home / "build/hana/unit/3")
        reading = with_wait_reason(reservation_reading(), admission_records.ReadyForAdmission())
        step = replace(reading.view.running[0], folder=local_folder)
        reading = replace(reading, view=replace(reading.view, running=(step,)),
                          explanation=replace(reading.explanation,
                              running=(replace(reading.explanation.running[0], step=step),)))
        build = queue_view.MacBuild(710001, mac_folder, reading.explanation.now - 30,
                                    "cargo nextest run", "hana")
        mac = queue_view.MacBuildReading(reading.explanation.now, (build,), home=str(mac_home))
        with (mock.patch.dict(os.environ, {"HOME": str(local_home)}),
              mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(memory_feed, "host_name", return_value="natedev"),
              mock.patch.object(queue_view, "read_mac_builds", return_value=mac)):
            page = memory_feed.reading()
        for machine, folder, home in (("natedev", local_folder, local_home), ("mac", mac_folder, mac_home)):
            with self.subTest(machine=machine):
                row, = [row for row in page["walk"] if row.get("machine") == machine]
                self.assertEqual(row.get("run_folder"), memory_feed.RecordedRunFolder(folder, str(home)))
                self.assertEqual(staff_names.parts_text(row.get("name_parts", [])), row["name"])
        self.assertTrue(page["waiters"])
        self.assertTrue(all("run_folder" not in row for row in page["waiters"]))

    def test_mac_reading_collects_and_preserves_the_remote_home(self) -> None:
        mac_home = str(self.root / "remote-home")
        script = queue_view.mac_build_script().replace("print(json.dumps(read_reading()))",
            "read_processes = lambda: {}\n"
            + "read_gate_records = lambda: {}\n"
            + "read_captures = lambda processes: {}\n"
            + "read_builds = lambda *args: []\n"
            + "unplaced_runs = lambda *args: []\n"
            + "print(json.dumps(read_reading()))")
        result = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                                env={**os.environ, "HOME": mac_home}, timeout=5, check=True)
        data = cast(dict[str, object], json.loads(result.stdout.removesuffix("rc=0\n")))
        self.assertEqual(data["home"], mac_home)
        _ = (self.root / "mac_test.conf").write_text("host=private-mac\n# range: 5e-324..\nconnect_timeout_s=2\n")
        with mock.patch.object(subprocess, "run", return_value=result) as remote:
            refresh_mac_builds()
            remote.assert_called_once()
            self.assertEqual(queue_view.read_mac_builds().home, mac_home)
            remote.side_effect = OSError("Mac is unavailable")
            refresh_mac_builds()
        reading = queue_view.read_mac_builds()
        self.assertEqual(reading.home, mac_home)
        self.assertEqual(reading.error, "Mac reading unavailable")

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write_private_build_roots(self.root / "build_folders.conf", self.root, FIXTURE_MACHINE)
        self.output = self.root / "page"
        self.configuration = self.root / "memory_feed.conf"
        _ = write_config_fixture(self.root / "build_admission.conf",
                                 f"query_timeout_seconds={FIXTURE_GUEST_REPORT_SECONDS}\n")
        _ = write_config_fixture(self.root / "build_steps.conf",
            "changes.default=no branch\nchanges.integrate={branch} branch\n"
            + "names.integrate=integrate\nnames.test-affected=test affected\nnames.test=test\n"
            + "names.gate=gate\n")
        self.configure()
        self.configure_console()
        _ = (self.root / "mac-builds.json").write_text(json.dumps({"read_at": ADMISSION_NOW, "builds": [], "error": ""}))
        proc = self.root / "proc"
        proc.mkdir()
        _ = (proc / "meminfo").write_text("SwapTotal: 8388608 kB\nSwapFree: 8388608 kB\n")
        _ = (self.root / "buildlog.conf").write_text("# whole: 1..\nTOP_COST_RECENT_DAYS=3\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "CONF_DIR": str(self.root),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILD_TARGETS_STATE_DIR": str(self.root / "target-state"),
            "AWS_TARGET_CONFIG": str(self.root / "aws_target.conf"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "STAFF_FILE": str(self.root / "staff.json"),
            "USER_ZONE": "UTC",
            "MAC_IDLE_STATE_DIR": str(self.root / "mac-idle"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "MACHINE_FACTS_FILE": str(self.root / "machine-facts.json"),
            "BUILDLOG_USER_CGROUP": str(self.root / "user"),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "user/builds.slice"),
        })))
        _ = self.enterContext(mock.patch.object(memory_trace, "read_policy", return_value=memory_trace.TracesOff("fixture")))
        _ = self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", return_value={}))
        _ = self.enterContext(mock.patch.object(memory_feed, "host_name", return_value=FIXTURE_MACHINE))
        _ = self.enterContext(mock.patch.object(memory_feed, "PROC", self.root / "proc"))
        _ = self.enterContext(mock.patch.object(memory_feed, "SYS_BLOCK", self.root / "sys" / "block"))
        _ = self.enterContext(mock.patch.object(memory_feed, "CI_CGROUP", self.root / "ci"))
        # The live jobserver and its journal stay out of the tests: 12 GiB floor, 4 free of 31 tokens.
        _ = self.enterContext(mock.patch.object(memory_feed, "stevie_output", return_value="12288\n4\n31\n"))
        _ = self.enterContext(mock.patch.object(memory_feed, "request_token_dump",
            return_value="Job type reload is not applicable for unit steve.service."))
        _ = self.enterContext(mock.patch.object(memory_feed, "steve_journal", return_value=""))
        _ = self.enterContext(mock.patch.object(memory_feed, "CGROUP", self.root / "cgroup"))
        _ = self.enterContext(mock.patch.object(charts, "page_data", return_value=report_fixture()))
        _ = self.enterContext(mock.patch.object(queue_view, "mac_reading_path", return_value=self.root / "mac-builds.json"))
        _ = self.enterContext(mock.patch.object(queue_view, "refresh_mac_builds"))
        _ = self.enterContext(mock.patch.object(target_availability, "read_snapshots", return_value=()))

    def test_unplaced_mac_runs_appear_on_the_page_and_send_one_message(self) -> None:
        from scripts.buildlog import mac_idle

        baseline = captured_reading()
        for label in ("placement episode/attempt", "user", "hana-ci", "unplaced"):
            with self.subTest(label=label):
                build = queue_view.MacBuild(710001, "/Mac/hana", baseline.explanation.now - 30,
                    "cargo build -p hana", "hana", gate_label=queue_view.parse_gate_label(label),
                    gate_placement=label.removeprefix("placement ") if label.startswith("placement ") else "")
                unplaced = (queue_view.UnplacedMacRun(build.pid, build.started_at, build.command),) if label == "unplaced" else ()
                reading = queue_view.MacBuildReading(baseline.explanation.now, (build,), unplaced_runs=unplaced)
                notified: set[tuple[int, float]] = set()
                with (mock.patch.object(mac_idle, "notify_showrunner", return_value=True) as send,
                      mock.patch.object(queue_view, "read_mac_builds", return_value=reading),
                      mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(baseline)),
                      mock.patch.object(memory_feed, "mac_build_units", return_value={})):
                    page = memory_feed.reading()
                    memory_feed.notify_unplaced_mac_runs(reading, notified)
                    memory_feed.notify_unplaced_mac_runs(reading, notified)
                    self.assertEqual(send.call_count, 1 if label == "unplaced" else 0)
                    row = next(row for row in page["walk"] if row["pid"] == build.pid)
                    if label.startswith("placement "):
                        self.assertNotIn("placement", row["name"])
                        self.assertEqual(cast(memory_feed.MacBuildRow, row)["gate_label"], label)
                    else:
                        self.assertIn(label, row["name"])
                    if label == "unplaced":
                        self.assertIn("pid 710001", str(send.call_args))
                        self.assertIn("Mac unplaced run: pid 710001", page["mac_idle_alert"])
                        first_key = cast(mac_idle.OutageMessage, send.call_args.kwargs["identity"]).key
                        next_run = queue_view.UnplacedMacRun(build.pid, build.started_at + 0.00001, build.command)
                        memory_feed.notify_unplaced_mac_runs(replace(reading, unplaced_runs=(next_run,)), notified)
                        self.assertEqual(send.call_count, 2)
                        self.assertNotEqual(first_key, cast(mac_idle.OutageMessage, send.call_args.kwargs["identity"]).key)
                        newer = (queue_view.UnplacedMacRun(710002, build.started_at, "rustc source.rs"),
                                 queue_view.UnplacedMacRun(710003, build.started_at, "cargo nextest run"))
                        memory_feed.notify_unplaced_mac_runs(replace(reading, unplaced_runs=newer), notified)
                        self.assertEqual(send.call_count, 3)
                        self.assertIn("pid 710002", cast(str, send.call_args.args[0]))
                        self.assertIn("pid 710003", cast(str, send.call_args.args[0]))
                        memory_feed.notify_unplaced_mac_runs(replace(reading, unplaced_runs=newer), notified)
                        self.assertEqual(send.call_count, 3)

    def configure(self, *, seconds: float = 0.125) -> None:
        _ = write_config_fixture(self.configuration,
                                 f"output_folder={self.output}\n# range: 5e-324..\ninterval_seconds={seconds}\n"
                                 + "history_days=7\nswap_rows=6\nreport_refresh_seconds=300\n"
                                 + "other_rows=6\ntoken_dump_seconds=10\nstevie_program=/run/current-system/sw/bin/stevie\n")

    def configure_console(self, age_limit: int = 15) -> None:
        settings = {"round_trip_limit": 1, "render_limit": 1, "reading_delay": 1, "builds.age_limit": age_limit}
        _ = (self.root / "console.conf").write_text("".join(
            f"# range: 0.001..86400\nstatus.{key}_seconds={value}\n" for key, value in settings.items()))

    def expected_page(self, seconds: float = 0.125) -> str:
        page = memory_feed.page_document(seconds)
        self.assertIn(console_theme.TOKENS, page)
        self.assertIn(f"const SECONDS = {json.dumps(seconds)};", page)
        self.assertIn(f"const buildLimitIcons = {json.dumps(memory_feed.build_limit_icons())};", page)
        self.assertNotRegex(page, r"__[A-Z_]+__")
        return page

    def test_page_sections_open_from_heading_buttons(self) -> None:
        page = self.expected_page()
        # The user, 11-22: the swap, Top 3 costs and leftover process sections are icon buttons at the top right;
        # each button has a tooltip and opens its section in a dialog.
        sections = (("builds-swap", "Swap and memory pressure", "memory-stick",
                     ("pressure-reason", "gauges", "gf-some", "gl-some", "gv-some", "limit-some", "gf-full", "gl-full",
                      "gv-full", "limit-full", "swap-sum", "gf-swap", "gv-swap", "swap-reserve", "swaprows")),
                    ("builds-costs", "Top 3 costs", "chart-bar-decreasing", ("report-top", "cost-window", "top-costs")),
                    ("builds-leftovers", "Leftover processes", "ghost", ("leftover-rows",)))
        actions = page[page.index('<div class="page-heading-actions" data-page-actions>'):page.index("</div>", page.index("data-page-actions>"))]
        self.assertEqual(re.findall(r'<button [^>]*>', actions), [
            f'<button type="button" class="page-action" data-page-action="{action}" aria-label="{label}" '
            + f'title="{label}" aria-haspopup="dialog" aria-controls="{action}-dialog">'
            for action, label, _icon, _ids in sections])
        self.assertEqual(re.findall(r'class="lucide lucide-([a-z-]+)"', actions), [icon for _action, _label, icon, _ids in sections])
        main = page[page.index("<main"):page.index("</main>")]
        self.assertIn('id="build-limits"', main)
        self.assertEqual(page.count(page_frame.CONSOLE_DIALOG_ASSETS), 1)
        for action, label, _icon, ids in sections:
            start = page.index(f'<dialog id="{action}-dialog" aria-labelledby="{action}-title" data-console-dialog>')
            dialog = page[start:page.index("</dialog>", start)]
            self.assertIn(f'<h2 id="{action}-title">', dialog)
            self.assertIn(label, dialog)
            for element in ids:
                self.assertIn(f'id="{element}"', dialog)
                self.assertNotIn(f'id="{element}"', main)
        for removed in ('id="swap"', 'id="report-charts"', 'id="leftovers"', 'href="#swap"'):
            self.assertNotIn(removed, page)
        self.assertIn('id="pill" data-tip="pill" aria-haspopup="dialog" aria-controls="builds-swap-dialog"', main)
        render = page[page.index("function renderLeftovers("):page.index("function reportCost(")]
        javascript = render + """
const elements = {};
const $ = id => (elements[id] ??= {innerHTML: ""});
const shownReading = () => ({leftoverReadings: new Map()});
renderLeftovers({leftovers: []});
process.stdout.write($("leftover-rows").innerHTML);
"""
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        result = subprocess.run([executable, "-e", javascript], text=True, capture_output=True,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "No leftover processes.")

    def test_converted_rows_show_their_hosts_without_charging_remote_memory_locally(self) -> None:
        baseline = reservation_reading()
        reading = with_converted_executions(baseline)
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            page = memory_feed.reading()
        remote = [row for row in page["walk"] if "used" in row and "counted" in row and "reserved" in row and row.get("machine") in
                  ("fixture-mac", "fixture-ssh", "uncertain-mac")]
        self.assertEqual(len(remote), 3)
        self.assertEqual({row.get("machine") for row in remote}, {"fixture-mac", "fixture-ssh", "uncertain-mac"})
        self.assertTrue(all(row["counted"] == 0 and row["reserved"] == 0 for row in remote))
        self.assertTrue(all(isinstance(row["used"], admission_records.RunUseUnavailable) for row in remote))
        uncertain, = [row for row in remote if row.get("machine") == "uncertain-mac"]
        self.assertIsInstance(uncertain["pid"], admission_records.PidNotRecorded)
        self.assertEqual(uncertain["state"], "uncertain")
        self.assertEqual(reading.explanation.room, baseline.explanation.room)

    def gate_wait_page(self, guest: placement.HostEligibility) -> memory_feed.MemoryPage:
        return self.placement_wait_page("/fixture/claude_commands", gate_candidates(guest))

    def placement_wait_page(self, folder: str,
                            candidates: tuple[placement.CandidateEligibility, ...]) -> memory_feed.MemoryPage:
        base = captured_reading()
        waiter = GatePlacementWaiter(8100, "gate-session", "fixture gate", "gate", folder,
            base.explanation.now - 30, admission_records.GIB, admission_records.NeedsMemory(),
            job=StepJob("gate"), placement_candidates=candidates)
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(replace(base, view=replace(base.view, waiting=(waiter,))))):
            return memory_feed.reading()

    def test_a_fixed_guest_exclusion_is_not_a_target_and_the_route_fit_names_the_targets(self) -> None:
        row, = self.placement_wait_page("/fixture/hana", gate_candidates(placement.FixedExclusion("repo")))["waiters"]
        route_fit, = self.placement_wait_page("/fixture/hana", ())["waiters"]
        self.assertNotIn("mac-linux", row.get("targets", []))
        self.assertNotIn("mac-linux", row.get("runs_on", ""))
        self.assertEqual((row.get("runs_on"), row.get("targets"), row.get("because")),
                         (route_fit.get("runs_on"), route_fit.get("targets"), route_fit.get("because")))

    def test_an_allowed_guest_is_a_target_and_a_fixed_exclusion_is_not(self) -> None:
        candidates = tuple(replace(candidate, eligibility=placement.FixedExclusion("offload_exclude"))
                           if candidate.destination.target == "hana-aws" else candidate
                           for candidate in gate_candidates(placement.AllowedHost()))
        row, = self.placement_wait_page("/fixture/claude_commands", candidates)["waiters"]
        self.assertEqual(row.get("targets"), ["natedev", "mac-linux"])
        self.assertEqual(row.get("runs_on"), "natedev or mac-linux")

    def test_gate_wait_row_lists_every_named_machine_once(self) -> None:
        page = self.gate_wait_page(placement.AllowedHost())
        row, = page["waiters"]
        self.assertEqual(row.get("targets"), ["natedev", "mac-linux", "hana-aws"])
        self.assertEqual(row.get("runs_on"), "natedev or mac-linux or hana-aws")
        self.assertEqual(row.get("because"), "")
        payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
        _tips, table = self.render_assignment_table(payload)
        self.assertEqual(table.count(f'data-tip="wait:{row.get("key")}" tabindex="0"'), 1)
        rows = dict(cast(list[tuple[str, str]], re.findall(r'<tr[^>]*data-tip="wait:([^"]+)"[^>]*>(.*?)</tr>', table, flags=re.S)))
        self.assertEqual(machine_icons(rows[str(row.get("key"))]), [("dim", "natedev"), ("dim", "mac-linux"), ("dim", "hana-aws")])

    def test_gate_wait_row_shows_the_recorded_mac_linux_exclusion(self) -> None:
        for guest in (placement.TemporaryExclusion("backoff until 15:30 EST"),
                      placement.EvidenceNeeded("unreadable record"),
                      placement.TemporaryExclusion("the Mac does not answer")):
            with self.subTest(guest=guest):
                row, = self.gate_wait_page(guest)["waiters"]
                self.assertEqual(row.get("because"), f"mac-linux: {guest.reason}")
                self.assertEqual(row.get("targets"), ["natedev", "mac-linux", "hana-aws"])
        page = self.gate_wait_page(placement.TemporaryExclusion("backoff until 15:30 EST"))
        payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
        tips, _table = self.render_assignment_table(payload)
        self.assertIn("mac-linux: backoff until 15:30 EST", "\n".join(tips["waiters"][0]))
        waiters = cast(list[dict[str, object]], payload["waiters"])
        waiters[0]["head_room"] = {"unavailable": True}
        unavailable_tips, _table = self.render_assignment_table(payload)
        self.assertIn("mac-linux: backoff until 15:30 EST", "\n".join(unavailable_tips["waiters"][0]))

    def test_native_mac_gate_row_keeps_named_targets_and_guest_exclusion(self) -> None:
        base = captured_reading()
        now = base.explanation.now
        scope = RepositoryScope("claude_commands")
        native = placement.CandidateEligibility(placement.PlacementDestination("mac", "mac", "mac", scope),
                                               placement.AllowedHost())
        step = GatePlacementStep("gate-session", "fixture gate", "gate", "/Mac/claude_commands", 0,
            admission_records.RemoteMemoryUnavailable("remote memory is unavailable"),
            pid=8200, started_at=now - 30, assignment_host="mac", mac_placement="episode/native-attempt",
            job=StepJob("gate"), placement_candidates=(native, *gate_candidates(placement.TemporaryExclusion("not answering"))))
        reading = replace(base, view=replace(base.view, running=(step,)))
        build = queue_view.MacBuild(8202, step.folder, step.started_at, "gate", "claude_commands",
            gate_label=queue_view.MacGateLabel.PLACEMENT, gate_placement=step.mac_placement)
        other = replace(build, pid=8203, gate_placement="episode/other-attempt")
        mac = queue_view.MacBuildReading(now, (build, other))
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(queue_view, "read_mac_builds", return_value=mac),
              mock.patch.object(memory_feed, "mac_build_units", return_value={})):
            page = memory_feed.reading()
        row = next(row for row in page["walk"] if row["pid"] == build.pid)
        self.assertEqual(row.get("targets"), ["mac", "natedev", "mac-linux", "hana-aws"])
        self.assertEqual(row.get("because"), "mac-linux: not answering")
        unrelated = next(row for row in page["walk"] if row["pid"] == other.pid)
        self.assertNotIn("targets", unrelated)
        payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
        tips, table = self.render_assignment_table(payload)
        text = "\n".join(tips["walk"][page["walk"].index(row)])
        self.assertIn("mac-linux: not answering", text)
        self.assertIn("Named targets  Mac or natedev or mac-linux or hana-aws", text)
        rows = dict(cast(list[tuple[str, str]], re.findall(r'<tr[^>]*data-tip="row:([^"]+)"[^>]*>(.*?)</tr>', table, flags=re.S)))
        self.assertEqual(machine_icons(rows[str(row.get("key"))]), [("run", "mac")])

    def test_consumed_guest_gate_uses_the_recorded_job_and_owner(self) -> None:
        now = 200_000.0
        step = GatePlacementStep("gate-session", "phases", "gate", "/fixture/claude_commands", 0,
            admission_records.RemoteMemoryUnavailable("remote memory is unavailable"),
            pid=admission_records.PidNotRecorded(), started_at=now - 30, unit="phases",
            assignment_host="guest", assignment_target="mac-linux", job=StepJob("test-affected"),
            placement_candidates=gate_candidates(placement.AllowedHost()))
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(captured_reading(running=(step,)))):
            page = memory_feed.reading()
        (row,) = (cast(memory_feed.RunRow, row) for row in page["walk"] if row.get("guest_unconfirmed"))
        self.assertEqual(row["title"], {"unit": "phases", "step": "test affected"})
        self.assertEqual((row.get("caller"), row.get("machine"), row["state"]),
                         ("phases", "mac-linux", "running"))

    def test_guest_gate_progress_comes_from_the_placement_sidecar(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, assignment_for, consume_guest_permission, guest_gate, mac_linux_and_natedev_request,
            publish_candidate,
        )

        for sidecar_state in ("empty", "missing file", "written counter"):
            with self.subTest(sidecar=sidecar_state):
                readings: list[queue_snapshot.PublishedView] = []
                with PlacementFixture() as fixture:
                    request = mac_linux_and_natedev_request(fixture, natedev=False)
                    assert isinstance(request.local, wire.LocalAdmissionContext)
                    sidecar = fixture.root / "gate.cgroup"
                    request = replace(request, identity=replace(request.identity, step="gate",
                        unit="phases", job=StepJob("test-affected")), local=replace(request.local,
                        sidecar="" if sidecar_state == "empty" else str(sidecar)))
                    if sidecar_state == "written counter":
                        progress_profile.publish_counter(sidecar, progress_profile.ProgressCounter("modules", 7, 12))
                    _ = publish_candidate(fixture, request)
                    assignment = assignment_for(fixture, request)
                    granted, = consume_guest_permission(fixture, assignment, guest_gate(fixture, assignment))
                    assert isinstance(granted, wire.FirstAuthorization), granted
                    execution = execution_identity.ObservedExecution(execution_identity.ProcessExecution(
                        assignment.destination.host, "host-generation-a", 700001, "guest-start"))
                    for confirmed in (False, True):
                        if confirmed:
                            _ = fixture.server.handle_request(
                                wire.ExecutionReport("guest-running", assignment, granted.claim, execution))
                        _ = fixture.run_pass()
                        readings.append(queue_snapshot.read_view_snapshot(fixture.state_dir))

                for confirmed, reading in zip((False, True), readings, strict=True):
                    with self.subTest(confirmed=confirmed):
                        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=reading):
                            page = memory_feed.reading()
                        row, = [row for row in page["walk"] if row.get("machine") == "mac-linux"]
                        self.assertEqual(bool(row.get("guest_run")), confirmed)
                        self.assertEqual(bool(row.get("guest_unconfirmed")), not confirmed)
                        payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
                        tips, table = self.render_assignment_table(payload)
                        if sidecar_state == "written counter":
                            self.assertEqual(row.get("progress", {}).get("phase"), "testing")
                            self.assertIn('<td class="place">testing</td><td class="done">58.3%</td>', table)
                            self.assertIn("Testing  7 of 12 modules", "\n".join(tips["walk"][page["walk"].index(row)]))
                        else:
                            self.assertIn('<td class="place">—</td><td class="done">—</td>', table)

    def test_kept_place_feed_keeps_its_number_and_formats_times_in_the_user_zone(self) -> None:
        reading = queued_reading(reservation_reading(), ())
        waiting = reading.view.waiting
        reading = replace(reading, view=replace(reading.view, waiting=(replace(waiting[0], took_place="failed"), *waiting[1:])))
        failed_at = datetime(2026, 11, 24, 6, 7, tzinfo=UTC).timestamp()
        kept = admission_records.HeldPlace("queue", "fixture", "lint", 85, failed_at, failed_at + 1800,
            "4242-99", ("natedev", "mac"), 1, "failed", failed_step="lint", position=2)
        reading = replace(reading, held_places=(kept,))
        publish_fixture_view(self.root / "buildlog" / "admission", reading, failed_at)
        with mock.patch.object(memory_feed, "user_zone", return_value=ZoneInfo("America/Los_Angeles")):
            page = memory_feed.reading()
        row, = page["held_places"]
        self.assertEqual((row["unit"], row["position"], row["targets"]), ("queue", 2, ("natedev", "mac")))
        self.assertEqual((row["failed_step"], row["failed_at"], row["kept_until"]), ("lint", "22:07 PST", "22:37 PST"))
        self.assertEqual([row.get("place") for row in page["waiters"]], [1, 3, 4, 5])
        self.assertEqual(page["waiters"][0].get("order_note"), "Took the place of its failed run.")

    def test_kept_place_follows_a_ci_job_that_started_before_it(self) -> None:
        from scripts.build_queue import build_queue

        with AdmissionFixture(private=True) as fixture:
            fixture.free(0)
            fixture.hold_starts()
            now = fixture.clock.now()
            ci = replace(fixture.service_request(), job_start=admission_records.JobStart(now - 30))
            fixture.server.held_places = (admission_records.HeldPlace("queue", "fixture", "test-all", now - 20,
                now, now + 100, "4242-99", ("natedev",), 1, "failed"),)
            _ = fixture.server.handle_request(ci)
            published = queue_snapshot.read_view_snapshot(fixture.state_dir)
            ordered, = published.reading.ordered
            self.assertEqual(gate.waiter_order(ordered[1])[1], now - 30)
            self.assertEqual(ordered[1]["since"], now)

        publish_fixture_view(self.root / "buildlog" / "admission", published.reading, published.written_at)
        page = memory_feed.reading()
        kept, = page["held_places"]
        ci_row, = page["waiters"]
        self.assertEqual((ci_row["since"], ci_row.get("order_since")), (now, now - 30))
        self.assertEqual((ci_row.get("place"), kept["position"]), (1, 2))
        self.assertEqual([(number, step.pid) for number, step in build_queue.waiting_positions(published.reading).items()],
                         [(1, ci.owner.pid)])

    def test_queue_timeout_keeps_the_last_guest_row_in_running(self) -> None:
        now = 200_000.0
        step = GatePlacementStep("gate-session", "phases", "gate", "/fixture/claude_commands", 0,
            admission_records.RemoteMemoryUnavailable("remote memory is unavailable"),
            pid=admission_records.PidNotRecorded(), started_at=now - 30, unit="phases",
            assignment_host="guest", assignment_target="mac-linux", job=StepJob("test-affected"))
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", side_effect=[
                  fixture_published_view(captured_reading(running=(step,))), wire.AdmissionUnavailable("queue read timed out")]),
              mock.patch.object(time, "sleep", side_effect=[None, FeedStopped]),
              self.assertRaises(FeedStopped)):
            _ = memory_feed.main([])
        payload = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["read_error"], "queue read timed out")
        rows = cast(list[dict[str, object]], payload["walk"])
        row, = [row for row in rows if row.get("guest_unconfirmed")]
        _tips, table = self.render_assignment_table(payload)
        self.assertEqual(table.count(f'data-tip="row:{row["key"]}" tabindex="0"'), 1)

    def guest_gate_page(self, *, unconfirmed_age: float = FIXTURE_GUEST_START_LIMIT) -> memory_feed.MemoryPage:
        base = captured_reading()
        now = base.explanation.now
        guest = GatePlacementStep("gate-session", "fixture gate", "gate", "/fixture/claude_commands", 0,
            admission_records.RemoteMemoryUnavailable("remote memory is unavailable"),
            pid=8200, started_at=now - 30, assignment_host="guest-ssh-name", assignment_target="mac-linux",
            mac_placement="episode/placement-attempt",
            job=StepJob("gate"), confirmed_guest_run={"target": "mac-linux", "attempt": "guest-attempt"},
            execution=execution_identity.ObservedExecution(execution_identity.GuestExecution(
                "mac-linux", "guest-attempt", "/home/gate-runner/build/claude/guest/8", "/home/gate-runner")),
            counter=progress_profile.ProgressCounter("modules", 7, 12),
            placement_candidates=gate_candidates(placement.AllowedHost()))
        # The queue names no Mac placement for an unconfirmed guest attempt.
        pending = replace(guest, pid=8201, started_at=now - unconfirmed_age, mac_placement="", confirmed_guest_run=None,
                          execution=execution_identity.ExecutionEvidenceUnavailable("guest has no start record", now))
        running = (guest, pending)
        explained = tuple(admission_records.ExplainedRunningStep(step, admission_records.AdmissionTimeNotRecorded(),
            0, 0, 0, owner=str(step.pid)) for step in running)
        reading = replace(base, view=replace(base.view, running=running),
                          explanation=replace(base.explanation, running=explained))
        native = queue_view.MacBuild(8202, "/Mac/claude_commands", now - 30, "gate", "claude_commands",
            gate_label=queue_view.MacGateLabel.PLACEMENT, gate_placement=guest.mac_placement)
        mac = queue_view.MacBuildReading(now, (native,))
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(queue_view, "read_mac_builds", return_value=mac),
              mock.patch.object(memory_feed, "mac_build_units", return_value={})):
            return memory_feed.reading()

    def test_guest_gate_is_starting_just_before_the_start_report_limit(self) -> None:
        page = self.guest_gate_page(unconfirmed_age=FIXTURE_GUEST_START_LIMIT - 0.001)
        pending = next(row for row in page["walk"] if row["pid"] == 8201)
        self.assertEqual(pending.get("guest_unconfirmed"), "starting")
        self.assertNotIn("guest_run", pending)

    def test_guest_gate_is_not_confirmed_at_the_start_report_limit(self) -> None:
        page = self.guest_gate_page(unconfirmed_age=FIXTURE_GUEST_START_LIMIT)
        pending = next(row for row in page["walk"] if row["pid"] == 8201)
        self.assertEqual(pending.get("guest_unconfirmed"), "not confirmed")
        self.assertNotIn("guest_run", pending)

    def test_guest_gate_rows_show_on_mac_linux_with_the_confirmed_attempt_or_not_confirmed(self) -> None:
        page = self.guest_gate_page()
        row = next(row for row in page["walk"] if row["pid"] == 8200)
        self.assertEqual(row.get("guest_run"), {"target": "mac-linux", "attempt": "guest-attempt"})
        self.assertNotIn("guest_unconfirmed", row)
        self.assertEqual((row.get("machine"), row.get("state")), ("mac-linux", "running"))
        self.assertIn("run_folder", row)
        self.assertEqual(row.get("run_folder"), memory_feed.RecordedRunFolder(
            "/home/gate-runner/build/claude/guest/8", "/home/gate-runner"))
        self.assertFalse(any(other["pid"] == 8202 for other in page["walk"]))
        unconfirmed = next(row for row in page["walk"] if row["pid"] == 8201)
        self.assertNotIn("guest_run", unconfirmed)
        self.assertIn("run_folder", unconfirmed)
        self.assertIsInstance(unconfirmed.get("run_folder"), memory_feed.RunFolderNotReported)
        self.assertEqual((unconfirmed.get("machine"), unconfirmed.get("guest_unconfirmed"), unconfirmed.get("state")),
                         ("mac-linux", "not confirmed", "running"))
        payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
        tips, table = self.render_assignment_table(payload)
        self.assertEqual(table.count(f'data-tip="row:{row.get("key")}" tabindex="0"'), 1)
        text = "\n".join(tips["walk"][page["walk"].index(row)])
        self.assertIn("Runs on  mac-linux", text)
        self.assertIn("Folder  ~/build/claude/guest/8", text)
        self.assertIn("Testing  7 of 12 modules", text)
        self.assertIn("Done  58.3%", text)
        self.assertIn("Named targets  natedev or mac-linux or hana-aws", text)
        self.assertNotIn("Guest attempt", text)
        self.assertNotIn("guest-attempt", text)

        from scripts.build_queue import mac_run
        from scripts.build_queue.test_placement import (
            PlacementFixture, adopt_assignment, assignment_for, consume_permission,
            mac_linux_and_natedev_request, publish_candidate,
        )

        for job in (StepJob("gate"), HAND_MADE):
            with self.subTest(guest=job.step, check="admission restart"), PlacementFixture() as fixture:
                request = mac_linux_and_natedev_request(fixture, natedev=False)
                request = replace(request, identity=replace(request.identity, step=job.step, job=job))
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                with mock.patch.object(mac_run, "claim_held", return_value=True):
                    granted, = fixture.server.handle_request(consume_permission("guest-launch", assignment, claim))
                self.assertEqual(granted, wire.FirstAuthorization(assignment, claim))
                folder = "/home/gate-runner/build/claude/guest/8"
                started = execution_identity.GuestExecution("mac-linux", assignment.attempt, folder, "/home/gate-runner")
                report = wire.ExecutionReport("guest-started", assignment, claim, execution_identity.ObservedExecution(started))
                _ = fixture.server.handle_request(wire.parse_request(wire.encode_request(report)))
                _ = fixture.run_pass()
                before, = fixture.server.handle_request(wire.ViewRequest())
                fixture.restart()
                _ = fixture.run_pass()
                after, = fixture.server.handle_request(wire.ViewRequest())

                for read in (before, after):
                    assert isinstance(read, wire.ViewReply), read
                    with mock.patch.object(queue_snapshot, "read_view_snapshot",
                                           return_value=fixture_published_view(read.reading)):
                        page = memory_feed.reading()
                    guest_row, = (item for item in page["walk"] if item.get("machine") == "mac-linux")
                    self.assertEqual(guest_row.get("guest_run"), {"target": "mac-linux", "attempt": assignment.attempt})
                    payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
                    tips, _ = self.render_assignment_table(payload)
                    hover = "\n".join(tips["walk"][page["walk"].index(guest_row)]).splitlines()
                    if job == HAND_MADE:
                        self.assertNotIn("run_folder", guest_row)
                        self.assertFalse(any(line.startswith("Folder  ") for line in hover), hover)
                    else:
                        self.assertEqual(guest_row.get("run_folder"), memory_feed.RecordedRunFolder(folder, started.home))
                        self.assertIn("Folder  ~/build/claude/guest/8", hover)

    def test_running_gate_target_cell_shows_only_the_running_machine(self) -> None:
        # The user, 11-23: a running row shows one icon where it runs; the named targets go in the tooltip.
        page = self.guest_gate_page()
        row = next(row for row in page["walk"] if row["pid"] == 8200)
        payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
        tips, table = self.render_assignment_table(payload)
        cells = dict(cast(list[tuple[str, str]],
            re.findall(r'<tr[^>]*data-tip="row:([^"]+)"[^>]*>(.*?)</tr>', table, flags=re.S)))
        self.assertEqual(machine_icons(cells[str(row.get("key"))]), [("run", "mac-linux")])
        self.assertIn("Named targets  natedev or mac-linux or hana-aws", "\n".join(tips["walk"][page["walk"].index(row)]))

    def test_consumed_unconfirmed_guest_gate_shows_one_mac_linux_row(self) -> None:
        base = captured_reading()
        now = base.explanation.now
        consumed = GatePlacementStep("gate-session", "fixture gate", "gate", "/fixture/claude_commands", 0,
            admission_records.RemoteMemoryUnavailable("remote memory is unavailable"),
            pid=admission_records.PidNotRecorded(), started_at=now - 30, assignment_host="linux-guest",
            assignment_target="mac-linux", job=StepJob("gate"),
            counter=progress_profile.ProgressCounter("modules", 2, 5),
            execution=execution_identity.ExecutionEvidenceUnavailable("guest has no start record", now),
            placement_candidates=gate_candidates(placement.AllowedHost()))
        # Before the consume, the queue has no start time and the gate starts nothing on the guest.
        available = replace(consumed, session_id="available-session", started_at=0.0)
        native = GatePlacementStep("mac-session", "fixture mac gate", "gate", "/Mac/claude_commands", 0,
            admission_records.RemoteMemoryUnavailable("remote memory is unavailable"),
            pid=8300, started_at=now - 20, assignment_host="mac", mac_placement="episode/native-attempt",
            job=StepJob("gate"))
        running = (consumed, available, native)
        explained = tuple(admission_records.ExplainedRunningStep(step, admission_records.AdmissionTimeNotRecorded(),
            0, 0, 0, owner=str(index)) for index, step in enumerate(running))
        reading = replace(base, view=replace(base.view, running=running),
                          explanation=replace(base.explanation, running=explained))
        build = queue_view.MacBuild(8302, native.folder, native.started_at, "gate", "claude_commands",
            gate_label=queue_view.MacGateLabel.PLACEMENT, gate_placement=native.mac_placement)
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(queue_view, "read_mac_builds", return_value=queue_view.MacBuildReading(now, (build,))),
              mock.patch.object(memory_feed, "mac_build_units", return_value={})):
            page = memory_feed.reading()
        remote = [row for row in page["walk"] if row.get("keep_text") == "remote memory is unavailable"]
        guest = next(row for row in remote if row["started_at"] == consumed.started_at)
        self.assertEqual((guest.get("machine"), guest.get("guest_unconfirmed")), ("mac-linux", "starting"))
        self.assertNotIn("guest_run", guest)
        waiting = next(row for row in remote if row["started_at"] == 0)
        self.assertEqual(waiting.get("machine"), "linux-guest")
        self.assertNotIn("guest_unconfirmed", waiting)
        host_row = next(row for row in remote if row["pid"] == native.pid)
        mac_row = next(row for row in page["walk"] if row["pid"] == build.pid)
        payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
        tips, table = self.render_assignment_table(payload)
        counts = {name: table.count(f'data-tip="row:{row.get("key")}" tabindex="0"')
                  for name, row in (("guest", guest), ("waiting", waiting), ("host", host_row), ("mac", mac_row))}
        # The Mac host run shows once, as its Mac build row; the guest run shows once, as its queue row.
        self.assertEqual(counts, {"guest": 1, "waiting": 0, "host": 0, "mac": 1})
        # The Machine cell shows the bright guest icon and the confirmation status.
        self.assertIn('class="machine machine-icon run" data-machine="mac-linux"', table)
        self.assertIn("</svg></span> starting</td>", table)
        text = "\n".join(tips["walk"][page["walk"].index(guest)])
        self.assertIn("Runs on  mac-linux", text)
        self.assertIn("Testing  2 of 5 modules", text)
        self.assertIn("Done  40.0%", text)
        self.assertIn("Named targets  natedev or mac-linux or hana-aws", text)
        self.assertNotIn("Guest attempt", text)
        self.assertNotIn("unavailable", text.splitlines()[0])
        # The confirmation adds a pid and an attempt; the row keeps its key, so an open hover stays.
        confirmed = cast(memory_feed.RunRow, guest.copy())
        confirmed["pid"] = 8400
        confirmed["guest_run"] = memory_feed.ConfirmedGuestRun(target="mac-linux", attempt="guest-attempt")
        del confirmed["guest_unconfirmed"]
        self.assertEqual(memory_feed.step_key(confirmed), guest.get("key"))

    def render_assignment_table(self, payload: dict[str, object]) -> tuple[dict[str, list[list[str]]], str]:
        html = self.expected_page()
        tooltip = html.index("function field(")
        functions = html[tooltip:html.index("const tip = document.getElementById", tooltip)]
        start = html.index("function renderWalk(")
        render = (html[html.index("function rowAdd("):html.index("const queueMovePath =")]
                  + html[start:html.index("document.addEventListener('click'", start)])
        escape = html[html.index("function esc("):html.index("const consoleHidden =")]
        javascript = "globalThis.window=globalThis;" + staff_names.script({})[8:-9] + functions + render + escape + """
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {style:{}, classList:{toggle(){}}, innerHTML:""});
  return elements.get(id);
};
function fitLabels() {}
const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));
staffNames.set(data.staff_names);
renderWalk(data);
process.stdout.write(JSON.stringify({
  walk:data.walk.map(row=>tipText('row:'+row.key,data)),
  waiters:data.waiters.map(row=>tipText('wait:'+row.key,data)),
  table:$("walk").innerHTML}));
"""
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        result = subprocess.run([executable, "-e", javascript], input=json.dumps(payload), text=True,
            capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rendered = cast(dict[str, object], json.loads(result.stdout))
        tips = {key: cast(list[list[str]], rendered[key]) for key in ("walk", "waiters")}
        return tips, cast(str, rendered["table"])

    def test_step_table_keeps_assignment_fields_with_report_tooltips(self) -> None:
        reading = cloud_reading(with_wait_reason(reservation_reading(), admission_records.StartSpacingWait(110)))
        units = ("cloud-unit", "waiting-unit")
        jobs = (StepJob("integrate"), StepJob("test-affected"))
        reading = replace(reading, cloud=tuple(
            replace(row, identity=replace(row.identity, session_id=unit, job=job))
            for row, unit, job in zip(reading.cloud, units, jobs, strict=True)))
        labels = {"cloud-unit": unit_labels.make_labels("cloud-unit", unit_labels.NoShowrunner(),
                                                       unit_labels.DirectsUnit("reassigned-unit"))}
        members = staff.StaffSnapshot(tuple(staff.StaffRow(staff.StaffId(unit), "unit", unit, staff.StaffRoot(),
            current_session=staff.StaffId(f"{unit}-session")) for unit in units))
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(unit_labels, "unit_labels_for", return_value=labels),
              mock.patch.object(staff, "snapshot", return_value=members),
              mock.patch.object(os, "cpu_count", return_value=4)):
            page = memory_feed.reading()
        payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
        rows = [*page["walk"], *page["waiters"]]
        keys = [row.get("key") for row in rows]
        self.assertEqual(len(set(keys)), len(rows))
        for row in reversed(rows):
            original = row.get("key")
            cast(dict[str, object], cast(object, row))["name"] = "changed owner label"
            self.assertEqual(memory_feed.step_key(row), original)
        # Two CI rows with no runner differ only by their cgroup.
        with tempfile.TemporaryDirectory() as folder:
            ci_keys = set[str]()
            for name in ("hana-linux-1.slice", "hana-linux-2.slice"):
                (cgroup := Path(folder) / name).mkdir()
                ci_keys.add(memory_feed.step_key(memory_feed.build_ci_row(0.0, 0.0, cgroup)))
        self.assertEqual(len(ci_keys), 2)
        tips, table = self.render_assignment_table(payload)
        self.assertNotIn('class="unit"', table)
        names = {f"{kind}:{key}": name for kind, key, name in cast(list[tuple[str, str, str]],
            re.findall(r'<div class="wrow[^"]*" data-tip="(row|wait):([^"]+)"[^>]*>'
                       + r'<span class="row-label"><span class="row-heading"><span class="name">(.*?)</span></span>'
                       + r'<span class="owner">', table, flags=re.S))}
        matches = cast(list[tuple[str, str, str]],
            re.findall(r'<tr[^>]*data-tip="(row|wait):([^"]+)"[^>]*>(.*?)</tr>', table, flags=re.S))
        cells = {f"{kind}:{key}": body for kind, key, body in matches}
        for collection, kind in (("walk", "row"), ("waiters", "wait")):
            rows = cast(list[dict[str, object]], payload[collection])
            local_position = 0
            for index, row in enumerate(rows):
                if kind == "row" and not (row.get("queued") or row.get("kind") == "cloud"):
                    continue
                if kind == "wait" and row.get("kind") != "cloud":
                    local_position += 1
                with self.subTest(kind=kind, pid=row["pid"]):
                    text = "\n".join(tips[collection][index])
                    cell = cells[f"{kind}:{row['key']}"]
                    progress = cast(dict[str, object], row.get("progress", {}))
                    place = (str(row.get("stage") or progress.get("phase", "—")) if kind == "row" else
                             "cloud" if row.get("kind") == "cloud" else str(local_position))
                    self.assertIn(f'<td class="place">{place}</td>', cell)
                    self.assertRegex(cell, r'<td class="row-action"><button type="button" class="row-add"[^>]*>.*</button></td>$')
                    self.assertIn(f'<td class="pid">{row["pid"]}</td>', cell)
                    machine = str(row.get("machine") or "")
                    if kind == "wait":
                        # A queued row shows a dim icon for each machine that can take it.
                        self.assertEqual(machine, "")
                        targets = cast(list[str], row["targets"])
                        self.assertTrue(targets)
                        self.assertEqual(machine_icons(cell), [("dim", target) for target in targets])
                    else:
                        self.assertTrue(machine)
                        self.assertEqual([name for _, name in machine_icons(cell)], [machine])
                    if kind == "wait":
                        self.assertIn('<td class="vcpus" colspan="2"></td>', cell)
                    else:
                        cpus = row.get("vcpus")
                        shown_cpus = str(cpus) if isinstance(cpus, int) else "—"
                        self.assertIn(f'<td class="vcpus">{shown_cpus}</td>', cell)
                    title = cast(memory_feed.QueueRowTitle, row["title"])
                    # A row with no unit shows the step after ": " in its name.
                    name = str(row["name"])
                    shown_name = names[f"{kind}:{row['key']}"]
                    step = (title["step"] if row["unit"] not in ("", "—") else
                            name.partition(": ")[2])
                    self.assertIn(f'<th scope="row" class="step"><span class="cell-text">{step}</span></th>', cell)
                    if row.get("kind") == "cloud":
                        unit = units[0 if kind == "row" else 1]
                        prefix, _, step = name.partition(": ")
                        self.assertEqual(row["unit"], unit)
                        self.assertEqual(prefix, unit)
                        self.assertRegex(shown_name, r'^<span class="staff-name" data-color-text="role\.unit-director" '
                                                     + rf'style="color:[^"]+">{unit}</span>$')
                        self.assertIn(f"{unit} · {step}", text)
                        self.assertNotIn("Uses  ", text)
                        if kind == "row":
                            self.assertIn("Runs on  AWS", text)
                            self.assertIn("Testing  7 of 12 modules", text)
                        else:
                            self.assertIn("Waits for  AWS slot", text)
                    else:
                        self.assertEqual(row["unit"], "")
                        self.assertEqual(shown_name, name.partition(": ")[0])
                        self.assertIn(name, text)

    def test_cloud_step_keeps_its_unit_color_after_the_session_is_replaced(self) -> None:
        reading = cloud_reading(reservation_reading())
        running = reading.cloud[0]
        unit = running.identity.unit
        running = replace(running, identity=replace(running.identity, session_id=unit, job=StepJob("integrate")))
        reading = replace(reading, cloud=(running, *reading.cloud[1:]))
        director = staff.StaffRow(staff.StaffId(unit), "unit", unit, staff.StaffRoot(),
                                 current_session=staff.StaffId("replacement-session"))
        members = staff.StaffSnapshot((director,))
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(staff, "snapshot", return_value=members)):
            page = memory_feed.reading()
        row, = [row for row in page["walk"] if row.get("kind") == "cloud"]
        self.assertEqual(row["unit"], unit)
        self.assertEqual(row["name"], "cloud-unit: integrate")
        self.assertEqual(row.get("name_parts"), [{"kind": "staff", "text": unit}, {"kind": "text", "text": ": integrate"}])
        self.assertIn('data-color-text="role.unit-director"', page.get("staff_names", {}).get(unit, ""))

    def test_ci_memory_stays_in_the_taken_total_with_an_idle_or_running_job(self) -> None:
        cgroup = self.root / "ci"
        (cgroup / "github-runner-fixture.service").mkdir(parents=True)
        for busy in (False, True):
            with self.subTest(busy=busy):
                runner = {"unit": "github-runner-fixture.service", "name": "fixture",
                          "state": "busy" if busy else "idle", "used": 0.125, "pid": 701,
                          "started_at": 199_000, "job": "tests"}
                step = admission_records.RunningStep("", "hana-ci.slice", "service", str(cgroup), 0,
                    admission_records.UnownedSliceMemory(admission_records.GIB // 8,
                        anonymous=admission_records.AnonymousRunUse(admission_records.GIB // 8),
                        cgroup=str(cgroup), owner="hana-ci.slice", pid=701,
                        job_state=RunningServiceJob(199_000) if busy else IdleService()), pid=701)
                reading = captured_reading(running=(step,))
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
                      mock.patch.object(memory_feed, "read_ci_runner", return_value=runner)):
                    page = memory_feed.reading()
                row, = [row for row in page["walk"] if row.get("ci")]
                self.assertNotIn("run_folder", row)
                self.assertEqual(cast(memory_feed.RunRow, row).get("ci_runners", [])[0]["state"], "busy" if busy else "idle")
                self.assertEqual(page["ci_ram"], 0.125)
                self.assertEqual(page["taken"], page["used"] + page["ci_ram"] + page["reserved"])
                self.assertEqual(page["taken"], 0.125)
                self.assertEqual(page["taken"], cast(float, page["cap"]) - page["room"])
                self.assertEqual(row.get("admitted"), busy)

    def test_the_feed_publishes_configured_colors_for_both_themes(self) -> None:
        original = self.configuration.read_text()
        keys = ("remote_memory", "machine_natedev", "machine_mac", "machine_aws")
        for theme in ("light", "dark"):
            for index, key in enumerate(keys):
                value = f"{index + 1:06x}"
                text = "\n".join(f"{key}_{theme}={value}" if line.startswith(f"{key}_{theme}=") else line
                                 for line in original.splitlines())
                with self.subTest(theme=theme, key=key):
                    _ = self.configuration.write_text(text)
                    configuration = memory_feed.read_configuration()
                    with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(captured_reading())):
                        page = memory_feed.reading(configuration)
                    palette = page["colors"]["light"] if theme == "light" else page["colors"]["dark"]
                    actual = palette["remote_memory"] if key == "remote_memory" else palette["machines"].get(key.removeprefix("machine_"))
                    self.assertEqual(actual, "#" + value)

    def test_waiting_and_unmatched_ci_jobs_do_not_claim_admission(self) -> None:
        from scripts.build_queue.scope_activity import WaitingServiceJob, UnmatchedServiceJob

        for state in (WaitingServiceJob(199_000), UnmatchedServiceJob(199_000), RunningServiceJob(199_000)):
            with self.subTest(state=state):
                step = admission_records.RunningStep("", "hana-ci.slice", "service", "/ci", 0,
                    admission_records.UnownedSliceMemory(admission_records.GIB // 8, owner="hana-ci.slice",
                        anonymous=admission_records.AnonymousRunUse(admission_records.GIB // 8),
                        job_state=RunningServiceJob(199_000), service_job_state=state, pid=701), pid=701)
                reading = with_wait_reason(captured_reading(running=(step,)), admission_records.StartSpacingWait(110))
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
                    page = memory_feed.reading()
                ci, = [row for row in page["walk"] if row.get("ci")]
                self.assertEqual(ci.get("admitted"), isinstance(state, RunningServiceJob))
                self.assertIn("another step just started; the queue waits a few seconds between starts", page["waiters"][0]["reason"])
                self.assertEqual(page["taken"], 0.125)
                self.assertEqual(page["ci_ram"], 0.125)

    def test_page_heading_shows_next_step_room_without_a_waiting_count(self) -> None:
        page = self.expected_page()
        self.assertIn('id="answer" data-tip="room">Free for the next step</div>', page)
        self.assertNotIn("steps wait", page)
        self.assertNotIn("step waits", page)

    def test_parentless_owner_keeps_its_name_and_names_its_steps(self) -> None:
        reading = with_wait_reason(reservation_reading(), admission_records.ReadyForAdmission())
        labels = {step.session_id: unit_labels.UnitLabels("solo")
                  for step in (*reading.view.running, *reading.view.waiting)}
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(unit_labels, "unit_labels_for", return_value=labels)):
            page = memory_feed.reading()
        running, = [row for row in page["walk"] if row.get("queued")]
        waiting, = page["waiters"]
        for row in (running, waiting):
            self.assertEqual(row.get("showrunner"), "solo")
            self.assertEqual(row["unit"], "—")
            self.assertTrue(row["name"].startswith("solo: "))

    def test_gate_rows_name_the_job_from_the_command_arguments(self) -> None:
        gate_command = ["python3", "/repo/scripts/production/merge_gate.py", "--merge-branch", "sdlc"]
        cases = (
            (["--unit", "handler", "--checkpoint", "abc"], StepJob("integrate", unit="handler", branch="sdlc")),
            (["--tests-only", "--full"], StepJob("test-all", branch="")),
            (["--tests-only", "--module", "a", "--module=b"], StepJob("test-modules", modules=2)),
            (["--tests-only"], StepJob("test-all")),
            (["--nightly"], StepJob("test-nightly", branch="sdlc")),
        )
        names = read_step_names()
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            for pid, (arguments, expected) in enumerate(cases, start=10):
                with self.subTest(arguments=arguments):
                    (proc / str(pid)).mkdir()
                    command = gate_command[:2] + arguments + (gate_command[2:] if expected.branch else [])
                    _ = (proc / str(pid) / "cmdline").write_bytes(b"\0".join(part.encode() for part in command) + b"\0")
                    job = memory_feed.step_job("gate", pid, proc)
                    self.assertEqual(job, expected)
                    notes = memory_feed.job_notes(job, names)
                    self.assertTrue(notes.get("about"))
                    self.assertEqual(notes.get("changes"), "sdlc branch" if expected.step == "integrate" else "no branch")
            self.assertIsInstance(memory_feed.step_job("nextest", 10, proc), memory_feed.NotGateStep)
            self.assertIsInstance(memory_feed.step_job("gate", 99, proc), memory_feed.CommandUnreadable)
        for command, named in (
            (["python3", "-X", "dev", "/repo/scripts/production/targeted_test.py"], StepJob("test-affected")),
            (["py", "targeted_test.py", "--full"], StepJob("test-all")),
            (["python3", "targeted_test.py", "--module", "a"], StepJob("test-modules", modules=1)),
            (["python3", "-m", "scripts.production.merge_gate", "--tests-only"], StepJob("test-all")),
            (["python3", "/repo/scripts/production/gate.py", "--unit", "handler", "--merge-branch", "sdlc"],
             StepJob("integrate", unit="handler", branch="sdlc")),
            (["python3", "-m", "scripts.production.gate", "--unit", "handler", "--merge-branch=sdlc"],
             StepJob("integrate", unit="handler", branch="sdlc")),
            (["gate.py", "--tests-only"], StepJob("test-all")),
            (["gate.py", "--tests-only", "--module=a", "--module", "b"], StepJob("test-modules", modules=2)),
            (["gate.py", "--nightly", "--merge-branch", "sdlc"], StepJob("test-nightly", branch="sdlc")),
            (["gate.py", "--promote-only", "--merge-branch", "sdlc"], StepJob("promote", branch="sdlc")),
            (["python3", "-m", "scripts.production.gate", "--tests-only"], StepJob("test-all")),
            (["python3", "-m", "scripts.production.gate", "--tests-only", "--module=a"],
             StepJob("test-modules", modules=1)),
            (["python3", "-mscripts.production.gate", "--nightly", "--merge-branch=sdlc"],
             StepJob("test-nightly", branch="sdlc")),
            (["python3", "-m", "scripts.production.gate", "--promote-only", "--merge-branch", "sdlc"],
             StepJob("promote", branch="sdlc")),
            (["python3", "/repo/scripts/production/gate.py", "--affected"], StepJob("test-affected")),
            (["gate.py", "--affected", "--full"], StepJob("test-all")),
            (["gate.py", "--affected", "--module", "a", "--module=b"], StepJob("test-modules", modules=2)),
            (["python3", "-m", "scripts.production.gate", "--affected"], StepJob("test-affected")),
            (["python3", "-m", "scripts.production.gate", "--affected", "--full"], StepJob("test-all")),
            (["python3", "-m", "scripts.production.gate", "--affected", "--module=a"],
             StepJob("test-modules", modules=1)),
            (["merge_gate.py", "--affected"], StepJob("test-affected")),
            (["python3", "-c", "pass", "gate.py"], OTHER_COMMAND),
            (["python3", "tool.py", "gate.py", "--affected"], OTHER_COMMAND),
            (["verify.sh", "--session-dir", "/session", "lint"], StepJob("lint")),
            (["bash", "verify.sh", "test", "hana"], StepJob("test")),
            (["python3", "-c", "pass", "merge_gate.py"], OTHER_COMMAND),
            (["python3", "-cpass", "merge_gate.py"], OTHER_COMMAND),
            (["python3", "-Bc", "pass", "merge_gate.py"], OTHER_COMMAND),
            (["python3", "-", "merge_gate.py"], OTHER_COMMAND),
            (["bash", "-c", "true", "verify.sh"], OTHER_COMMAND),
            (["python3", "-Xdev", "-mscripts.production.merge_gate", "--nightly"], StepJob("test-nightly")),
            (["python3", "-u", "-W", "ignore", "merge_gate.py", "--tests-only", "--full"], StepJob("test-all")),
            (["python3", "tool.py", "merge_gate.py"], OTHER_COMMAND),
            (["cargo", "build"], OTHER_COMMAND),
        ):
            with self.subTest(command=command):
                self.assertEqual(command_job(command), named)
        about = "Merges the checkpoint into the sdlc branch, runs every test module, then pushes sdlc"
        self.assertEqual(names.about(StepJob("integrate", unit="handler", branch="sdlc")), about)
        self.assertEqual(names.about(StepJob("test-all")), "Runs every test module")
        reading = reservation_reading()
        integrate = StepJob("integrate", unit="handler", branch="sdlc")
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(memory_feed, "step_job", return_value=integrate)):
            page = memory_feed.reading()
        running, = [row for row in page["walk"] if row.get("queued")]
        for row in (running, *page["waiters"]):
            self.assertEqual(row.get("changes"), "sdlc branch")
            self.assertEqual(row.get("about"), about)
            self.assertIn("integrate", row["name"])

    def test_step_descriptions_use_the_current_templates_and_recorded_work(self) -> None:
        path = self.root / "build_steps.conf"
        _ = write_config_fixture(path, "about.verify-final=Runs the final checks and tests\n")
        names = read_step_names()
        verify = admission_records.VerifyCall("final", "verify final hana", 2, 2)
        notes = memory_feed.job_notes(memory_feed.NotGateStep(), names,
                                     step="nextest", verify=verify)
        self.assertEqual(notes.get("about"), "Runs the final checks and tests")
        self.assertNotIn("changes", notes)
        for step, ci, kind, expected in (
            ("bench", True, "build-step", "Runs the selected benchmarks for CI"),
            ("nextest", True, "build-step", "Runs the selected tests for CI"),
            ("c", False, "build-step", "Checks that the selected targets compile"),
            ("", False, "compiler-cache", "This cache stores compiled code for reuse"),
            ("", False, "unowned-processes", "These processes run outside the queue"),
        ):
            with self.subTest(step=step, kind=kind):
                notes = memory_feed.job_notes(memory_feed.NotGateStep(), names, step=step, ci=ci, kind=kind)
                self.assertEqual(notes.get("about"), expected)

    def test_full_targeted_test_keeps_changes_without_a_unit(self) -> None:
        reading = with_wait_reason(captured_reading(), admission_records.ReadyForAdmission())
        waiter = replace(reading.view.waiting[0], step="targeted-test", session_id="missing-session", unit="")
        reading = replace(reading, view=replace(reading.view, waiting=(waiter,)))
        proc = self.root / "proc"
        command = proc / str(waiter.pid) / "cmdline"
        command.parent.mkdir()
        _ = command.write_bytes(b"python3\0targeted_test.py\0--full\0")
        _ = write_config_fixture(self.root / "build_steps.conf",
                                 "names.test-all=test all\nabout.test-all=runs every test\nabout.gate-job=\n"
                                 + "changes.default=no branch\n")
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            page = memory_feed.reading()
        row, = page["waiters"]
        # Every row has a description (b1ea3138c0); a run with no unit keeps the default changes text.
        self.assertEqual(row.get("changes"), "no branch")
        self.assertEqual(row.get("about"), "runs every test")
        self.assertEqual(row["unit"], "")
        self.assertIn("test all", row["name"])


    def test_local_test_counters_reach_the_page_through_admission(self) -> None:
        for step in ("test-affected", "gate", "nextest", "test", "t"):
            with self.subTest(step=step):
                with AdmissionFixture() as fixture, fixture.loop_turn():
                    owner = fixture.proc.spawn()
                    sidecar = fixture.scope(owner)
                    request = replace(fixture.request(owner=owner, step=step), sidecar=str(sidecar))
                    grant = only_admitted(fixture.server.handle_request(request))
                    phase = "suites:1" if step in ("test-affected", "gate") else "test:1"
                    counter = progress_profile.ProgressCounter(phase, 7, 12)
                    progress_profile.publish_counter(sidecar, counter)
                    _ = fixture.run_pass()
                    reply, = fixture.server.handle_request(wire.ViewRequest())
                    assert isinstance(reply, wire.ViewReply)
                    reading = reply.reading
                    running, = [row for row in reading.view.running if row.pid == owner.pid]
                    self.assertEqual(queue_view.step_counter(running), counter)
                    fixture.release(grant)
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
                    page = memory_feed.reading()
                row, = [row for row in page["walk"] if row["pid"] == owner.pid]
                self.assertEqual(row.get("progress"), {"kind": "RowProgress", "phase": "testing",
                    "unit": "modules" if phase == "suites:1" else "tests",
                    "done": 7, "total": 12, "percent": counter.percent})
                self.assertTrue(row.get("queued"))


    def test_pressure_text_on_the_page_rounds_down_to_one_decimal(self) -> None:
        baseline = reservation_reading()
        for pressure, expected in (
            (admission_records.MemoryPressure(0, some_avg10=30, condition="some"), "some avg10 30.0"),
            (admission_records.MemoryPressure(0, swap_used_percent=95.99), "swap 95.9% used"),
        ):
            with self.subTest(condition=pressure.condition):
                inputs = replace(baseline.explanation.decided_from, pressure=pressure)
                reading = gate.queue_read_from(inputs, admission_policy_fixture())
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
                    page = memory_feed.reading()
                self.assertIn(expected, page["pressure"])
                self.assertNotIn("96.0%", page["pressure"])


    def test_native_mac_row_shows_stage_unit_and_memory_without_changing_local_totals(self) -> None:
        baseline = reservation_reading()
        now = baseline.explanation.now
        build = queue_view.MacBuild(710001, "/Users/nate/rust/hana", now - 45,
            "cargo nextest run --test-threads 12", "hana", queue_view.MacTesting(710002),
            3 * admission_records.GIB, 8, progress_profile.ProgressCounter("test:1", 7, 10), "trunk")
        native = queue_view.MacBuildReading(now, (build,))
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(baseline)):
            before = memory_feed.reading()
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(baseline)),
              mock.patch.object(queue_view, "read_mac_builds", return_value=native),
              mock.patch.object(memory_feed, "mac_build_units", return_value={710001: unit_labels.UnitLabels("", unit="trunk")})):
            page = memory_feed.reading()
        row = cast(memory_feed.MacBuildRow, next(row for row in page["walk"] if row["pid"] == 710001))
        self.assertEqual(row["kind"], "mac-build")
        self.assertEqual(row["name"], "trunk: nextest")
        self.assertEqual(row["repository"], "hana")
        self.assertEqual(row["showrunner"], "—")
        self.assertEqual(row["machine"], "mac")
        self.assertEqual(row["unit"], "trunk")
        self.assertEqual(row["stage"], queue_view.MacTesting(710002))
        self.assertEqual(row["used"], 3)
        self.assertEqual(row["vcpus"], 12)
        self.assertEqual(row.get("progress"), {"kind": "RowProgress", "phase": "testing", "unit": "tests",
            "done": 7, "total": 10, "percent": 70})
        for field in ("counted", "reserved", "need"):
            self.assertNotIn(field, row)
        for field in ("used", "reserved", "taken", "room", "cap", "other", "shown_reserved"):
            self.assertEqual(page.get(field), before.get(field), field)

    def test_cargo_job_reads_cargo_words_after_the_ci_front_options(self) -> None:
        front = ("python3", "-I", "/store/scripts/build_queue/ci_cargo_front.py", "--front", "/store/bin/cargo",
                 "--shared-folder", "/run/hana-ci-admission/hana-linux-1", "--")
        for words, job in ((("build", "--release"), "release build"), (("nextest", "run"), "tests"),
                           (("+nightly", "doc"), "doc"), ((), "")):
            with self.subTest(words=words):
                self.assertEqual(memory_feed.cargo_job((*front, *words)), job)
                self.assertEqual(memory_feed.cargo_job(("/store/bin/cargo", *words)), job)

    def test_decoded_ci_rows_use_the_configured_label_when_the_recorded_label_is_unknown(self) -> None:
        _ = (self.root / "build_admission.conf").write_text("ci_label=Fixture CI · {runner} · {job}\n")
        base = with_wait_reason(reservation_reading(), admission_records.ReadyForAdmission())
        inputs = base.explanation.decided_from
        runner = "hana-linux-2"
        running = replace(inputs.running[0], ci_runner=runner, session_name="unregistered session")
        waiting = replace(base.view.waiting[0], ci_runner=runner, session_name="unregistered session")
        reading = gate.queue_read_from(replace(inputs, running=(running,)), admission_policy_fixture())
        reading = replace(reading, view=replace(reading.view, waiting=(waiting,)))
        reply = wire.parse_reply(wire.encode_reply(wire.ViewReply(reading)))
        assert isinstance(reply, wire.ViewReply)
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reply.reading)):
            page = memory_feed.reading()
        run, = page["walk"]
        wait, = page["waiters"]
        for row, pid in ((run, 50), (wait, 200)):
            with self.subTest(pid=pid):
                self.assertEqual((row["pid"], row["name"]), (pid, "hana CI: nextest"))
                self.assertEqual(row.get("ci_label"), "hana CI")
                self.assertEqual(row["title"], {"unit": "", "step": "hana CI"})

    def test_a_waiting_ci_step_shows_its_job_start_and_age(self) -> None:
        _ = (self.root / "build_admission.conf").write_text("ci_label=Fixture CI · {runner} · {job}\n")
        base = with_wait_reason(reservation_reading(), admission_records.ReadyForAdmission())
        started = base.explanation.now - 70 * 60
        waiting = replace(base.view.waiting[0], ci_runner="hana-linux-2", session_name="unregistered session",
                          ci_job_start=admission_records.JobStart(started))
        reading = replace(base, view=replace(base.view, waiting=(waiting,)))
        reply = wire.parse_reply(wire.encode_reply(wire.ViewReply(reading)))
        assert isinstance(reply, wire.ViewReply)
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reply.reading)), \
             mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}):
            page = memory_feed.reading()
        wait, = page["waiters"]
        clock = datetime.fromtimestamp(started, UTC).strftime("%H:%M")
        self.assertEqual(wait.get("job_start"), f"{clock} UTC · 1 h 10 min ago")

    def test_ci_feed_rejects_incomplete_label_templates(self) -> None:
        for template, valid in (("CI {runner} {job}", "CI hana-linux-2 compile"),
                                ("CI {runner} ({job})", "CI hana-linux-2 (compile)")):
            _ = (self.root / "build_admission.conf").write_text(f"ci_label={template}\n")
            for job, expected in (("compile", valid), ("—", "hana CI"), ("", "hana CI"), ("  ", "hana CI")):
                with self.subTest(template=template, job=job):
                    self.assertEqual(memory_feed.ci_step_label("hana-linux-2", job), expected)

    def test_row_parts_mark_the_staff_unit_owner(self) -> None:
        for owner, what, unit, expected in (("panes", "tests", "panes", "panes: tests"),
                                            ("panes", "—", "panes", "panes"),
                                            ("caller", "tests", "panes", "caller: tests"),
                                            ("—", "tests", "—", "unregistered session: tests"),
                                            ("—", "—", "—", "unregistered session")):
            with self.subTest(owner=owner, what=what):
                parts = staff_names.row_parts(owner, what, unit)
                self.assertEqual(staff_names.parts_text(parts), expected)
                self.assertEqual([part["text"] for part in parts if part["kind"] == "staff"],
                                 ["panes"] if owner == unit == "panes" else [])

    def test_mac_work_uses_the_caller_after_an_unknown_owner(self) -> None:
        build = queue_view.MacBuild(90082, "/private/hana", 0, "cargo nextest run", "hana")
        identity = unit_labels.UnitLabels("mac caller", "unregistered session")
        row = memory_feed.mac_work_row(build, queue_view.MacBuildReading(1, (build,)), identity,
                                      names=read_step_names())
        self.assertEqual((row.get("caller"), row.get("owner_text")), ("mac caller", "mac caller"))
        self.assertEqual(row["name"], "mac caller: nextest")

    def test_a_placed_mac_run_shows_the_source_checkout_and_what(self) -> None:
        build = queue_view.MacBuild(90082, "/private/hana", 0, "cargo nextest run", "hana",
            gate_label=queue_view.MacGateLabel.PLACEMENT, gate_placement="episode/attempt")
        for unit, name in (("cargo-liner", ""), ("", "cargo-liner")):
            with self.subTest(unit=unit, name=name):
                placed = admission_records.RunningStep("", name, "run", "/private/hana", 0,
                    admission_records.RemoteMemoryUnavailable("remote memory is unavailable"),
                    pid=admission_records.PidNotRecorded(), unit=unit, mac_placement="episode/attempt",
                    what="run 6 installs")
                row = memory_feed.mac_work_row(build, queue_view.MacBuildReading(1, (build,)),
                                              unit_labels.UnitLabels(""), names=read_step_names(),
                                              placed_step=placed)
                self.assertEqual(row["name"], "hana: run 6 installs")

    def test_ci_runner_records_name_the_owner_in_running_and_waiting(self) -> None:
        base = with_wait_reason(reservation_reading(), admission_records.ReadyForAdmission())
        memory = admission_records.UnownedSliceMemory(admission_records.GIB // 8,
            cgroup=str(self.root / "ci"), owner="hana-ci.slice", pid=701, job_state=IdleService())
        service = admission_records.RunningStep("", "hana-ci.slice", "service", "/ci", 0, memory, pid=701)
        (self.root / "ci" / "github-runner-hana-linux-1.service").mkdir(parents=True)
        (self.root / "ci" / "github-runner-hana-linux-2.service").mkdir()
        inputs = base.explanation.decided_from
        original = inputs.running[0]
        for runner in ("hana-linux-1", "hana-linux-2"):
            with self.subTest(runner=runner):
                running = replace(original, unit=runner, session_id="", session_name="unrelated display name")
                reading = gate.queue_read_from(replace(inputs, running=(running, service)), admission_policy_fixture())
                waiting = replace(base.view.waiting[0], unit=runner, session_id="", session_name="unrelated display name")
                reading = replace(reading, view=replace(reading.view, waiting=(waiting,)))
                def runner_record(path: Path, proc: Path) -> memory_feed.CIRunner:
                    del proc
                    return {"unit": path.name, "name": path.name.removeprefix("github-runner-").removesuffix(".service"),
                            "state": "idle", "used": 0.0625, "pid": 701}
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
                      mock.patch.object(memory_feed, "read_ci_runner", side_effect=runner_record)):
                    page = memory_feed.reading()
                run = next(row for row in page["walk"] if row.get("queued"))
                self.assertEqual(run.get("showrunner"), "")
                self.assertEqual(run.get("ci_label"), "unrelated display name")
                self.assertEqual(run["name"], "hana CI: nextest")
                self.assertEqual(page["waiters"][0]["showrunner"], "")
                self.assertEqual(page["waiters"][0].get("ci_label"), "unrelated display name")
                self.assertEqual(run["unit"], "")
                ci = next(row for row in page["walk"] if row.get("ci"))
                self.assertEqual(ci["unit"], "")
                self.assertTrue(ci.get("ci_label"))
                for row in (run, page["waiters"][0], ci):
                    self.assertFalse(row.get("mac_text"))
                for row in (run, ci):
                    self.assertFalse(row.get("runs_on"))
                # bob, 2026-11-23: a waiting CI step can run only on the host, where the running CI row shows it.
                host = FIXTURE_MACHINE
                self.assertEqual(ci.get("machine"), host)
                self.assertEqual(page["waiters"][0].get("targets"), [host])
                self.assertEqual(page["waiters"][0].get("runs_on"), f"{host} only")
        impostor = replace(base.view.waiting[0], unit="other", session_name="hana-linux-1")
        self.assertNotEqual(memory_feed.feed_step_labels(impostor, {}, frozenset({"hana-linux-1"})).owner_text, "hana CI")

    def test_mac_unit_requires_a_claim_and_uses_its_staff_record(self) -> None:
        from scripts.copies import copies
        from scripts.mac_test import claim_state, floor_claims

        build = queue_view.MacBuild(100, "/Users/nate/rust/hana", 200_000,
            repository="hana", recorded_unit="verify", session="owner")
        record: claim_state.ProcessBoundClaim = {"claim_id": "claim", "episode": "episode", "attempt": "attempt",
            "submitting_session": "owner", "pid": 100, "proc_start": "start", "what": "test",
            "worktree": "/Users/nate/rust/hana", "since": "2026-11-12T18:00:00+00:00"}
        claim = claim_state.HeldClaim(claim_state.RepositoryScope("hana"), self.root / "claim.json", record)
        lease = floor_claims.HeldMirror(floor_claims.MirrorIdentity("mac", build.folder), "owner")
        identity = unit_labels.UnitLabels("worker", "hana", "trunk")
        for claims, leases in (((claim,), ()), ((), (lease,)), ((), ())):
            with (mock.patch.object(copies, "held_remote_mirrors", return_value=leases),
                  mock.patch.object(unit_labels, "unit_labels_for", return_value={"owner": identity})):
                result = memory_feed.mac_build_units((build,), claims)
            self.assertEqual(result.get(100, unit_labels.UnitLabels("")).unit, "trunk" if claims else "—")

    def test_mac_remote_claim_matches_its_session_and_repository(self) -> None:
        from scripts.copies import copies
        from scripts.mac_test import claim_state

        build = queue_view.MacBuild(100, "/Users/nate/.local/state/mac-test/mirror/hana", 200_000,
            repository="hana", recorded_unit="verify", session="claim-session")
        record: claim_state.RemoteJobClaim = {"claim_id": "claim", "episode": "episode", "attempt": "attempt",
            "submitting_session": "claim-session", "pid": 900, "proc_start": "start", "what": "nextest hana",
            "worktree": "/home/nate/source", "since": "2026-11-12T18:00:00+00:00", "remote_host": "mac", "repo_name": "hana"}
        claim = claim_state.HeldClaim(claim_state.RepositoryScope("hana"), self.root / "claim.json", record)
        identity = unit_labels.UnitLabels("worker", "hana", "trunk")
        with (mock.patch.object(claim_state, "read_claims", side_effect=AssertionError("claims come from the page reading")),
              mock.patch.object(copies, "held_remote_mirrors", return_value=()),
              mock.patch.object(unit_labels, "unit_labels_for", return_value={"claim-session": identity})):
            self.assertEqual(memory_feed.mac_build_units((build,), (claim,))[100], identity)
            self.assertEqual(memory_feed.mac_build_units((replace(build, repository="cargo-liner"),), (claim,)), {})
            self.assertEqual(memory_feed.mac_build_units((replace(build, session="unclaimed"),), (claim,)), {})

    def test_old_native_mac_readings_hide_their_process_rows(self) -> None:
        _ = write_config_fixture(self.configuration, "mac_activity_refresh_seconds=30\n")
        baseline = reservation_reading()
        now = baseline.explanation.now
        build = queue_view.MacBuild(710001, "/Users/nate/mac-clone", now - 120)
        for read_at, error in ((now - 90, ""), (now - 30, "Mac unreachable"), (0, "Mac reading missing")):
            with self.subTest(read_at=read_at, error=error):
                native = queue_view.MacBuildReading(read_at, (build,), error)
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(baseline)),
                      mock.patch.object(queue_view, "read_mac_builds", return_value=native)):
                    page = memory_feed.reading()
                self.assertFalse(any(row["pid"] == 710001 for row in page["walk"]))
                payload = memory_feed.page_json(page)
                self.assertIn("Mac reading is old", payload)
                if error:
                    self.assertNotIn(error, payload)
                self.assertEqual(sum(row.get("kind") == "mac-unavailable" for row in page["walk"]), 1)

    def mac_clone_units(self, build: queue_view.MacBuild, *, host: str = "mac", count: int = 1,
                        mirror: bool = False) -> dict[int, unit_labels.UnitLabels]:
        """Resolve a manual clone against private claims and mirror records."""
        from scripts.copies import copies
        from scripts.mac_test import claim_state, floor_claims

        claims: list[claim_state.HeldClaim] = []
        labels: dict[str, unit_labels.UnitLabels] = {}
        for index in range(count):
            session = f"claim-session-{index}"
            record: claim_state.RemoteJobClaim = {"claim_id": f"claim-{index}", "episode": "episode",
                "attempt": "attempt", "submitting_session": session, "pid": 900 + index,
                "proc_start": "start", "what": "hana build", "worktree": "/home/nate/source",
                "since": "2026-11-12T18:00:00+00:00", "remote_host": host, "repo_name": "hana"}
            claims.append(claim_state.HeldClaim(claim_state.RepositoryScope("hana"),
                self.root / f"claim-{index}.json", record))
            labels[session] = unit_labels.UnitLabels("worker", "hana", "research" if index == 0 else "builds")
        leases = ((floor_claims.HeldMirror(floor_claims.MirrorIdentity(host, build.folder),
                   "claim-session-0"),) if mirror else ())
        with (mock.patch.object(copies, "held_remote_mirrors", return_value=leases),
              mock.patch.object(unit_labels, "unit_labels_for", return_value=labels)):
            return dict(memory_feed.mac_build_units((build,), tuple(claims)))

    def test_mac_claims_require_unambiguous_checkout_or_session_evidence(self) -> None:
        build = queue_view.MacBuild(68214, "/Users/nate/rust/hana_handmade", 200_000,
            "cargo build -p hana", "hana", origin=queue_view.MacBuildOrigin.NON_CI)
        matching_checkout = replace(build, folder="/home/nate/source")
        cases = (
            ("manual clone", build, "mac", 1, "—"),
            ("SSH user", matching_checkout, "natemccoy@mac", 1, "research"),
            ("matching checkout", matching_checkout, "mac", 1, "research"),
            ("other host", matching_checkout, "other", 1, "—"),
            ("CI", replace(build, origin=queue_view.MacBuildOrigin.CI), "mac", 1, "—"),
            ("old cache", replace(build, origin=queue_view.MacBuildOrigin.UNKNOWN), "mac", 1, "—"),
            ("duplicate claims with different owners", matching_checkout, "mac", 2, "—"),
            ("no claim", build, "mac", 0, "—"),
            ("other session", replace(build, session="another"), "mac", 1, "—"),
            ("other session in worktree", replace(matching_checkout, session="another"), "mac", 1, "—"),
            ("other repository", replace(build, repository="cargo-liner"), "mac", 1, "—"),
        )
        for name, row, host, count, expected in cases:
            with self.subTest(name=name):
                result = self.mac_clone_units(row, host=host, count=count)
                self.assertEqual(result.get(row.pid, unit_labels.UnitLabels("")).unit, expected)

    def test_mac_mirror_match_keeps_its_unit_with_an_unknown_origin(self) -> None:
        build = queue_view.MacBuild(100, "/Users/nate/mirror/hana", 200_000, repository="hana")
        self.assertEqual(self.mac_clone_units(build, host="natemccoy@mac", mirror=True)[100].unit, "research")

    def test_configuration_reads_all_keys_from_the_feed_config(self) -> None:
        with mock.patch.object(conf, "read", wraps=conf.read) as read:
            configuration = memory_feed.read_configuration()
        self.assertIn(mock.call("memory_feed"), read.call_args_list)
        self.assertEqual(configuration, memory_feed.FeedConfiguration(self.output, 0.125, 7, 6, 300, configuration.colors, 6, 10,
                                                                   Path("/run/current-system/sw/bin/stevie")))

    def test_configuration_expands_the_output_folder(self) -> None:
        original = self.configuration.read_text()
        _ = self.configuration.write_text(original.replace(f"output_folder={self.output}", "output_folder=~/page"))
        self.assertEqual(memory_feed.read_configuration().output_folder, Path.home() / "page")
        _ = self.configuration.write_text(original.replace(f"output_folder={self.output}", "output_folder=$FEED_TEST_ROOT/page"))
        with mock.patch.dict(os.environ, {"FEED_TEST_ROOT": str(self.root)}):
            self.assertEqual(memory_feed.read_configuration().output_folder, self.output)

    def test_a_missing_key_fails_with_its_name(self) -> None:
        original = self.configuration.read_text()
        for key in ("output_folder", "interval_seconds", "history_days", "swap_rows", "report_refresh_seconds"):
            remaining = "\n".join(line for line in original.splitlines()
                                  if not line.startswith(key + "="))
            with self.subTest(key=key):
                _ = self.configuration.write_text(remaining)
                with conf.diagnostics(report=False), self.assertRaisesRegex(conf.Unusable, key):
                    _ = memory_feed.read_configuration(conf.read(self.configuration))

    def test_link_prints_the_configured_folder_with_the_review_host_and_port(self) -> None:
        root = self.root / "review-links"
        folder = root / "fixture folder"
        _ = self.configuration.write_text(f"output_folder={folder}\n")
        _ = (self.root / "review_links.conf").write_text("# whole: 1..65535\nserver_port=9432\n")
        folder.mkdir(parents=True)
        page = folder / "memory_live.html"
        _ = page.write_text("published page")
        output = io.StringIO()
        with (mock.patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(root), "REVIEW_LINKS_HOST": "fixture.example."}),
              mock.patch.object(memory_feed, "publish_page", side_effect=AssertionError("The link must not publish the page")),
              redirect_stdout(output)):
            self.assertEqual(memory_feed.page_url(), "http://fixture.example:9432/fixture%20folder/memory_live.html")
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(memory_feed.main(["link"]), 0)
        self.assertEqual(output.getvalue(), "http://fixture.example:9432/fixture%20folder/memory_live.html\n")
        self.assertEqual(page.read_text(), "published page")
        self.assertEqual(list(folder.iterdir()), [page])

    def test_a_missing_page_raises_an_allowed_error_and_publishes_nothing(self) -> None:
        root = self.root / "review-links"
        folder = root / "fixture"
        _ = self.configuration.write_text(f"output_folder={folder}\n")
        _ = (self.root / "review_links.conf").write_text("# whole: 1..65535\nserver_port=9432\n")
        output = io.StringIO()
        with (mock.patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(root), "REVIEW_LINKS_HOST": "fixture.example"}),
              mock.patch.object(memory_feed, "publish_page", side_effect=AssertionError("The link must not publish the page")),
              redirect_stdout(output)):
            with self.assertRaises(review_link.SNAPSHOT_ERRORS) as raised:
                _ = memory_feed.page_url()
            self.assertIsInstance(raised.exception, FileNotFoundError)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(memory_feed.page_link(), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertTrue(output.getvalue().startswith("Build page link: "))
        self.assertIn("Builds page is not available", output.getvalue())
        self.assertFalse(root.exists())

    def test_link_refuses_a_folder_outside_the_review_root_with_one_plain_line(self) -> None:
        root = self.root / "review-links"
        output = io.StringIO()
        with (mock.patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(root), "REVIEW_LINKS_HOST": "fixture.example"}),
              redirect_stdout(output)):
            with self.assertRaises(review_link.SNAPSHOT_ERRORS):
                _ = memory_feed.page_url()
            self.assertEqual(memory_feed.main(["link"]), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertTrue(output.getvalue().startswith("Build page link: "))
        self.assertFalse(self.output.exists())

    def test_page_url_keeps_snapshot_errors_for_the_console_caller(self) -> None:
        with (mock.patch.object(review_link, "review_root", return_value=self.root),
              mock.patch.object(review_link, "snapshot_url", side_effect=OSError("page unavailable"))):
            with self.assertRaisesRegex(OSError, "page unavailable"):
                _ = memory_feed.page_url()

    def test_served_page_allows_same_origin_framing(self) -> None:
        # Read the feed configuration first: the server fixture then points CONF_DIR at its own folder.
        configuration = memory_feed.read_configuration()
        server_fixture = test_serve.ReviewLinksServerTests()
        self.addCleanup(server_fixture.doCleanups)
        server_fixture.setUp()
        memory_feed.publish_page(replace(configuration, output_folder=server_fixture.root))
        connection = HTTPConnection("127.0.0.1", server_fixture.port, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.addCleanup(connection.close)
        connection.request("GET", "/memory_live.html")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertNotIn(response.getheader("X-Frame-Options", "").upper(), ("DENY",))
        self.assertNotIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy", "").lower())
        page = response.read().decode()
        self.assertEqual(page, self.expected_page())

    def test_start_publishes_the_page_before_the_first_queue_reading(self) -> None:
        def view(_folder: Path) -> queue_snapshot.PublishedView:
            self.assertEqual((self.output / "memory_live.html").read_text(), self.expected_page())
            return fixture_published_view(captured_reading())

        with mock.patch.object(queue_snapshot, "read_view_snapshot", side_effect=view) as read:
            self.assertEqual(memory_feed.main(["--once"]), 0)
        read.assert_called_once_with(self.root / "buildlog" / "admission")
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertIs(page["ok"], True)
        self.assertEqual(page["read_at"], 200_000)
        self.assertEqual(page["total"], 60)
        self.assertEqual(page["available"], 30)
        self.assertEqual(page["walk"], [])
        self.assertEqual(page["limits"], {"some": 25, "full": 5})
        self.assertEqual(set(path.name for path in self.output.iterdir()), {"memory_live.html", "memory.json", memory_feed.REPORT_NAME})
        for name in ("memory_live.html", "memory.json", memory_feed.REPORT_NAME):
            with self.subTest(name=name):
                self.assertEqual((self.output / name).stat().st_mode & 0o777, 0o644)

    def test_a_one_time_run_reads_the_mac_but_sends_no_alert(self) -> None:
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(captured_reading())),
              mock.patch.object(memory_feed, "notify_unplaced_mac_runs") as notify):
            self.assertEqual(memory_feed.main(["--once"]), 0)
            notify.assert_not_called()
            with (mock.patch.object(time, "sleep", side_effect=FeedStopped),
                  self.assertRaises(FeedStopped)):
                _ = memory_feed.main([])
        notify.assert_called_once()

    def test_restart_replaces_the_served_page_with_configured_timing(self) -> None:
        self.output.mkdir()
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        for seconds, stale in ((0.125, 15), (60.0, 300)):
            with self.subTest(seconds=seconds):
                self.configure(seconds=seconds)
                self.configure_console(stale)
                _ = (self.output / "memory_live.html").write_text("previous page")
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(captured_reading())):
                    self.assertEqual(memory_feed.main(["--once"]), 0)
                page = (self.output / "memory_live.html").read_text()
                self.assertEqual(page, self.expected_page(seconds))
                constants = page[page.index("const SECONDS ="):page.index("const $ =")]
                result = subprocess.run(
                    [executable, "-e", constants + "\nprocess.stdout.write(String(SECONDS));"],
                    text=True, capture_output=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(float(result.stdout), seconds)
                self.assertIn(f'&quot;ageLimit&quot;: {float(stale)}', page)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_page_status_uses_the_real_builds_loader_with_configured_timing(self) -> None:
        self.output.mkdir()
        for seconds, stale in ((0.125, 15), (60.0, 300)):
            with self.subTest(seconds=seconds):
                self.configure(seconds=seconds)
                self.configure_console(stale)
                _ = (self.output / "memory_live.html").write_text("previous page")
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(captured_reading())):
                    _ = memory_feed.main(["--once"])
                page = (self.output / "memory_live.html").read_text()
                payload = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
                # The shared browser test sets the data time for each response.
                del payload["fresh_until"]
                assert_status_states(self, page, seconds, payload)

    def test_page_proof_removes_chrome_files_after_exit_and_a_killed_child(self) -> None:
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        chrome = self.root / "chrome"
        report = self.root / "chrome-report.json"
        _ = chrome.write_text(f"#!{sys.executable}\n" + textwrap.dedent(r"""
            import base64, hashlib, json, os, signal, socket, sys, tempfile
            from pathlib import Path
            profile = next(argument.split('=', 1)[1] for argument in sys.argv if argument.startswith('--user-data-dir='))
            allocated = tempfile.mkdtemp(prefix='url_fetcher_')
            record = {'profile': profile, 'temporary': os.environ['TMPDIR'], 'allocated': allocated}
            report = Path(os.environ['CHROME_REPORT'])
            report.write_text(json.dumps(record))
            if os.environ['CHROME_END'] == 'request':
                with socket.socket() as server:
                    server.bind(('127.0.0.1', 0))
                    server.listen(1)
                    print(f'DevTools listening on ws://127.0.0.1:{server.getsockname()[1]}/debugger', file=sys.stderr, flush=True)
                    with server.accept()[0] as connection:
                        stream = connection.makefile('rb')
                        headers = {}
                        while line := stream.readline().strip():
                            if b':' in line:
                                key, value = line.split(b':', 1)
                                headers[key.lower()] = value.strip()
                        key = headers[b'sec-websocket-key'] + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                        accept = base64.b64encode(hashlib.sha1(key).digest())
                        connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                        frame = stream.read(2)
                        size = frame[1] & 127
                        if size == 126:
                            size = int.from_bytes(stream.read(2), 'big')
                        mask = stream.read(4)
                        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(stream.read(size)))
                        record['request'] = json.loads(payload)['method']
                        report.write_text(json.dumps(record))
                        os.kill(os.getpid(), signal.SIGKILL)
            if os.environ['CHROME_END'] == 'kill':
                os.kill(os.getpid(), signal.SIGKILL)
            sys.exit(1)
        """))
        chrome.chmod(0o755)
        proof = self.root / "proof"
        for ending in ('exit', 'kill', 'request'):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory(dir=self.root) as directory:
                caller = Path(directory)
                environment = {**os.environ, "TMPDIR": str(caller), "PYTHON": sys.executable,
                    "CHROME": str(chrome), "CHROME_REPORT": str(report), "CHROME_END": ending}
                result = run_process_group([executable, str(Path(memory_feed.__file__).with_name("memory_page_proof.mjs")),
                    str(proof)], env=environment, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                self.assertNotEqual(result.returncode, 0)
                if ending == 'request':
                    self.assertRegex(result.stderr, "Chrome (closed during a debugger request|debugger connection closed)")
                else:
                    self.assertIn("Chrome closed before its debugger started", result.stderr)
                record = cast(dict[str, str], json.loads(report.read_text()))
                if ending == 'request':
                    self.assertEqual(record['request'], 'Target.createTarget')
                profile, temporary, allocated = (Path(record[key]) for key in ("profile", "temporary", "allocated"))
                self.assertTrue(temporary.is_relative_to(profile))
                self.assertTrue(allocated.is_relative_to(temporary))
                self.assertFalse(profile.exists())
                self.assertEqual(list(caller.iterdir()), [])

    def test_an_interrupted_page_publication_preserves_the_previous_page(self) -> None:
        self.output.mkdir()
        target = self.output / "memory_live.html"
        _ = target.write_text("previous page")
        with mock.patch("scripts.lib.atomic_file.os.replace", side_effect=OSError("publication interrupted")):
            with self.assertRaisesRegex(OSError, "publication interrupted"):
                memory_feed.publish_page(memory_feed.read_configuration())
        self.assertEqual(target.read_text(), "previous page")
        self.assertEqual(list(self.output.iterdir()), [target])

    def write_view_file(self, written_at: float, pass_interval: float = 30) -> None:
        publish_fixture_view(self.root / "buildlog" / "admission", captured_reading(), written_at, pass_interval)

    def test_feed_uses_published_view_when_the_socket_does_not_answer(self) -> None:
        written_at = 200_001.0
        self.write_view_file(written_at)
        with mock.patch.object(admission_client, "view", side_effect=wire.AdmissionUnavailable("no answer in 0.5 s")) as query:
            self.assertEqual(memory_feed.main(["--once"]), 0)
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertIs(page["ok"], True)
        self.assertNotIn("read_error", page)
        self.assertEqual(page["read_at"], written_at)
        query.assert_not_called()

    def test_view_is_late_only_after_the_pass_interval_and_page_limit(self) -> None:
        written_at = 200_000.0
        self.write_view_file(written_at)
        self.assertEqual(memory_feed.main(["--once"]), 0)
        page = (self.output / "memory_live.html").read_text()
        options = re.search(r'data-status-options="([^"]+)"', page)
        assert options is not None
        source = Path(memory_feed.__file__).with_name("memory_live.html").read_text(encoding="utf-8")
        loader = source[source.index("async function tick("):source.index("let reportVersion =")]
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        payload = (self.output / "memory.json").read_text()
        script = test_page_frame.PageStatusTests.environment(unescape(options[1])) + page_frame.REFRESH_SCRIPT + loader + f"""
const data = {payload};
const fetch = async () => ({{ok:true, json:async()=>data}});
const consoleHidden = () => false, paused = false;
const render = () => {{}}, level = () => "ok", latest = data, $ = () => node();
const reportVersion = data.report_version;
const refresh = createPageRefresh(tick);
(async () => {{
    for (const age of [20, 45, 46]) {{
        now = ({written_at} + age) * 1000;
        await refresh.refresh();
        console.log(line.dataset.state, parts.age.textContent);
    }}
}})();
"""
        result = subprocess.run([executable, "-e", script], text=True, capture_output=True, check=False,
                                timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.assertEqual((result.returncode, result.stdout.splitlines()),
                         (0, ["Live 20 s ago", "Live 45 s ago", "Late 46 s ago"]), result.stderr)
        self.assertIn(loader, page)

    def test_missing_or_unreadable_view_file_keeps_the_queue_error(self) -> None:
        folder = self.root / "buildlog" / "admission"
        folder.mkdir(parents=True)
        for contents in (None, b"\xff", b"{}", b"[]"):
            with self.subTest(contents=contents):
                if contents is not None:
                    _ = (folder / "view.json").write_bytes(contents)
                self.assertEqual(memory_feed.main(["--once"]), 0)
                page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
                self.assertIs(page["ok"], False)
                self.assertEqual(page["error"], "The queue did not answer.")

    def test_the_first_failed_reading_publishes_an_error(self) -> None:
        with mock.patch.object(queue_snapshot, "read_view_snapshot",
                               side_effect=wire.AdmissionUnavailable("private socket unavailable")):
            self.assertEqual(memory_feed.main(["--once"]), 0)
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertIs(page["ok"], False)
        self.assertEqual(page["error"], "private socket unavailable")

    def test_a_failed_cycle_retains_the_last_successful_reading(self) -> None:
        with (mock.patch.object(queue_snapshot, "read_view_snapshot",
                                side_effect=[fixture_published_view(captured_reading()), RuntimeError("queue unavailable")]),
              mock.patch.object(time, "sleep", side_effect=[None, FeedStopped]) as sleep):
            with self.assertRaises(FeedStopped):
                _ = memory_feed.main([])
        self.assertEqual(sleep.call_args_list, [mock.call(0.125), mock.call(0.125)])
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertIs(page["ok"], True)
        self.assertEqual(page["read_at"], 200_000)
        self.assertEqual(page["read_error"], "queue unavailable")

    def test_missing_pressure_has_a_named_state(self) -> None:
        pressure = memory_feed.read_stalls()
        self.assertIsInstance(pressure["some"], memory_feed.PressureNotRecorded)
        self.assertIsInstance(pressure["full"], memory_feed.PressureNotRecorded)

    def test_report_configuration_uses_private_values(self) -> None:
        text = self.configuration.read_text().replace("history_days=7", "history_days=11").replace("swap_rows=6", "swap_rows=4").replace(
            "report_refresh_seconds=300", "report_refresh_seconds=600")
        _ = self.configuration.write_text(text)
        configuration = memory_feed.read_configuration()
        self.assertEqual(configuration.history_days, 11)
        self.assertEqual(configuration.swap_rows, 4)
        self.assertEqual(configuration.report_refresh_seconds, 600)

    def test_report_reads_follow_the_configured_refresh_window(self) -> None:
        fixed_costs = aws_costs.empty_costs(AWS_COST_TIME)
        report_calls_per_cycle: list[int] = []
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(captured_reading())) as queue,
              mock.patch.object(charts, "page_data", return_value=report_fixture()) as provider,
              mock.patch.object(aws_costs, "read_saved_costs", return_value=fixed_costs),
              # Each cycle reads the clock for the loop, token dump, Mac source, and limits source.
              mock.patch.object(time, "monotonic", side_effect=(
                  0.0, 0.0, 0.0, 0.0,
                  100.0, 100.0, 100.0, 100.0,
                  301.0, 301.0, 301.0, 301.0,
              )) as clock,
              mock.patch.object(time, "sleep", side_effect=[None, None, FeedStopped]),
              mock.patch.object(memory_feed, "write_text_atomic", wraps=atomic_file.write_text_atomic) as write):
            def view(_folder: Path) -> queue_snapshot.PublishedView:
                report_calls_per_cycle.append(provider.call_count)
                return fixture_published_view(captured_reading())

            queue.side_effect = view
            with self.assertRaises(FeedStopped):
                _ = memory_feed.main([])
        self.assertEqual(queue.call_count, 3)
        self.assertEqual(clock.call_count, 12)
        self.assertEqual(report_calls_per_cycle, [1, 1, 2])
        self.assertEqual(provider.call_args_list, [mock.call(7, n=3), mock.call(7, n=3)])
        # The second report read has the same text, so only the first one writes report.json.
        written = [cast(Path, call.args[0]).name for call in write.call_args_list]
        self.assertEqual(written.count(memory_feed.REPORT_NAME), 1)
        self.assertLess(written.index(memory_feed.REPORT_NAME), written.index("memory.json"))
        report_text = (self.output / memory_feed.REPORT_NAME).read_text()
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertNotIn("report", page)
        expected_report = memory_feed.report_file(memory_feed.report_reading(7), fixed_costs)
        self.assertEqual(page["report_version"], expected_report.version)
        self.assertEqual(report_text, expected_report.text)

    def test_mac_readings_follow_the_configured_refresh_window(self) -> None:
        for interval, expected in ((30, [710001, 710001, 710002]), (60, [710001, 710001, 710001])):
            with self.subTest(interval=interval):
                self.configure()
                _ = write_config_fixture(self.configuration, f"mac_activity_refresh_seconds={interval}\n")
                builds = iter((710001, 710002))
                published: list[int] = []

                def refresh() -> None:
                    pid = next(builds)
                    _ = (self.root / "mac-builds.json").write_text(json.dumps({"read_at": 200_000,
                        "builds": [queue_view.mac_build_record(queue_view.MacBuild(pid, f"/private/mac-{pid}", 199_900))], "error": ""}))

                def after_cycle(seconds: float) -> None:
                    del seconds
                    page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
                    rows = cast(list[dict[str, object]], page["walk"])
                    row, = [row for row in rows if row.get("machine") == "mac"]
                    published.append(cast(int, row["pid"]))
                    if len(published) == 3:
                        raise FeedStopped

                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(captured_reading())),
                      mock.patch.object(queue_view, "refresh_mac_builds", side_effect=refresh),
                      mock.patch.object(time, "monotonic", side_effect=(
                          0.0, 0.0, 0.0, 0.0,
                          29.0, 29.0, 29.0, 29.0,
                          # The third cycle has no token dump request.
                          30.0, 30.0, 30.0,
                      )) as clock,
                      mock.patch.object(time, "sleep", side_effect=after_cycle)):
                    with self.assertRaises(FeedStopped):
                        _ = memory_feed.main([])
                self.assertEqual(published, expected)
                self.assertEqual(clock.call_count, 11)

    def test_token_dump_requests_keep_the_interval_after_slow_reads(self) -> None:
        clock = [0.0]
        requests: list[float] = []

        def slow_journal(dump_seconds: int) -> memory_feed.TokenHoldersUnknown:
            del dump_seconds
            clock[0] += 4
            return memory_feed.NO_TOKEN_DUMP_YET

        def request() -> str:
            requests.append(clock[0])
            return ""

        def sleep(seconds: float) -> None:
            del seconds
            clock[0] += 9
            if clock[0] > 30:
                raise FeedStopped

        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(captured_reading())),
              mock.patch.object(memory_feed, "newest_token_holders", side_effect=slow_journal),
              mock.patch.object(memory_feed, "request_token_dump", side_effect=request),
              mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(time, "sleep", side_effect=sleep)):
            with self.assertRaises(FeedStopped):
                _ = memory_feed.main([])
        # Each journal read takes 4 s and each cycle sleeps 9 s; requests stay at least 10 s apart.
        self.assertEqual(requests, [4.0, 26.0])

    def test_report_dates_use_the_configured_user_zone(self) -> None:
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            report = memory_feed.report_reading(7)
        assert isinstance(report, memory_feed.ReportSections)
        self.assertEqual(report.zone, "America/New_York")
        self.assertEqual(report.start, "2026-11-04T19:00:00-05:00")
        self.assertEqual(report.end, "2026-11-07T19:00:00-05:00")

    def cgroup(self, name: str, swapped: int) -> Path:
        path = self.root / "cgroup" / name
        path.mkdir(parents=True, exist_ok=True)
        _ = (path / "memory.swap.current").write_text(str(swapped))
        return path

    def host_swap(self, total: int, used: int) -> None:
        _ = (self.root / "proc/meminfo").write_text(
            f"SwapTotal: {total // 1024} kB\nSwapFree: {(total - used) // 1024} kB\n")

    def test_swap_accounts_for_the_host_and_names_the_agent_owner_once(self) -> None:
        self.host_swap(32 * admission_records.GIB, 20 * admission_records.GIB)
        _ = self.cgroup("user.slice", 16 * admission_records.GIB)
        user = "user.slice/user@1000.service"
        _ = self.cgroup(user, 15 * admission_records.GIB)
        _ = self.cgroup(user + "/app.slice", 12 * admission_records.GIB)
        _ = self.cgroup(user + "/app.slice/app-agents.slice", 10 * admission_records.GIB)
        agent = self.cgroup(user + "/app.slice/app-agents.slice/first.scope", 4 * admission_records.GIB)
        _ = self.cgroup(user + "/app.slice/app-agents.slice/second.scope", 6 * admission_records.GIB)
        _ = self.cgroup(user + "/app.slice/app-browser.scope", 2 * admission_records.GIB)
        _ = self.cgroup(user + "/session.slice", 2 * admission_records.GIB)
        _ = self.cgroup(user + "/builds.slice", admission_records.GIB)
        scope = self.cgroup(user + "/builds.slice/run.scope", admission_records.GIB)
        _ = self.cgroup("system.slice", 2 * admission_records.GIB)
        _ = self.cgroup("hana.slice", admission_records.GIB)
        queued = reservation_reading().view.running[0]
        memory = queued.measured
        assert isinstance(memory, admission_records.MeasuredMemory)
        queued = replace(queued, measured=replace(memory, anonymous_source=admission_records.ScopeSource(scope, frozenset())))
        reads: Counter[Path] = Counter()
        original = Path.read_text
        original_bytes = Path.read_bytes

        def read(path: Path) -> str:
            reads[path] += 1
            return original(path)

        def read_bytes(path: Path) -> bytes:
            if path == self.root / "proc/meminfo":
                reads[path] += 1
            return original_bytes(path)

        names = read_step_names()
        with (mock.patch.object(Path, "read_text", new=read),
              mock.patch.object(Path, "read_bytes", new=read_bytes)):
            swap = memory_feed.read_swap((queued, queued), {}, 6, names)
        assert isinstance(swap, memory_feed.SwapReading)
        rows = {row["name"]: row for row in swap.rows}
        self.assertEqual(rows["agent sessions (2)"]["bytes"], 10 * admission_records.GIB)
        self.assertEqual(rows["other apps: browser"]["bytes"], 2 * admission_records.GIB)
        self.assertEqual(rows["desktop session"]["bytes"], 2 * admission_records.GIB)
        self.assertEqual(rows["system services"]["bytes"], 2 * admission_records.GIB)
        self.assertEqual(rows["hana CI"]["bytes"], admission_records.GIB)
        self.assertEqual(rows[queue_lines.step_text(queued, read_step_names())]["bytes"], admission_records.GIB)
        self.assertEqual(rows["Unclassified groups"]["bytes"], admission_records.GIB)
        self.assertEqual(rows["Not attributed"]["bytes"], admission_records.GIB)
        self.assertEqual(rows["All swap used"]["bytes"], swap.used)
        self.assertEqual(rows["agent sessions (2)"]["owner_count"], 2)
        self.assertEqual({name: rows[name]["pool"] for name in ("hana CI", "system services", "Not attributed")},
                         {"hana CI": "builds_ci", "system services": "outside", "Not attributed": "outside"})
        self.assertEqual(rows[queue_lines.step_text(queued, read_step_names())]["pool"], "builds_ci")
        self.assertEqual(swap.builds_ci, 2 * admission_records.GIB)
        self.assertEqual(sum(row["bytes"] for row in swap.rows if row["kind"] != "total"), swap.used)
        self.assertEqual(swap.files_read, sum(reads.values()))
        self.assertTrue(all(count == 1 for count in reads.values()))
        self.assertNotIn(agent / "memory.swap.current", reads)

    def test_swap_app_titles_check_the_program_name(self) -> None:
        self.host_swap(2 * admission_records.GIB, admission_records.GIB)
        _ = self.cgroup("app.slice", admission_records.GIB)
        _ = self.cgroup("app.slice/app-test.scope", admission_records.GIB)
        for program, expected in (("browser", "other apps: browser"), ("—", "unregistered session"),
                                  (" ", "unregistered session")):
            with self.subTest(program=program), mock.patch.object(memory_feed, "program_name", return_value=program):
                reading = memory_feed.read_swap((), {}, 6, read_step_names())
                assert isinstance(reading, memory_feed.SwapReading)
                self.assertEqual([row["name"] for row in reading.rows if row["kind"] == "owner"], [expected])

    def test_swap_step_titles_check_the_configured_step_and_owner(self) -> None:
        self.host_swap(2 * admission_records.GIB, admission_records.GIB)
        _ = self.cgroup("builds.slice", admission_records.GIB)
        scope = self.cgroup("builds.slice/test.scope", admission_records.GIB)
        step = reservation_reading().view.running[0]
        memory = step.measured
        assert isinstance(memory, admission_records.MeasuredMemory)
        step = replace(step, measured=replace(memory, anonymous_source=admission_records.ScopeSource(scope, frozenset())))
        for unit, shown, expected in (("widget", "tests", "widget: tests"),
                                      ("widget", "—", "widget"),
                                      ("—", "tests", "unregistered session: tests"),
                                      ("—", "—", "unregistered session")):
            with self.subTest(unit=unit, shown=shown):
                row = replace(step, unit="", project=unit, session_name="")
                reading = memory_feed.read_swap((row,), {}, 6, StepNames({f"names.{row.step}": shown}))
                assert isinstance(reading, memory_feed.SwapReading)
                self.assertEqual([item["name"] for item in reading.rows if item["kind"] == "owner"], [expected])

    def owner_swap_reading(self, owner: unit_labels.UnitLabels) -> memory_feed.SwapReading:
        """Read a real swap counter with its recorded staff owner."""
        self.host_swap(2 * admission_records.GIB, admission_records.GIB)
        _ = self.cgroup("builds.slice", admission_records.GIB)
        scope = self.cgroup("builds.slice/owner.scope", admission_records.GIB)
        step = reservation_reading().view.running[0]
        memory = step.measured
        assert isinstance(memory, admission_records.MeasuredMemory)
        step = replace(step, measured=replace(memory,
                       anonymous_source=admission_records.ScopeSource(scope, frozenset())))
        reading = memory_feed.read_swap((step,), {step.session_id: owner}, 6, read_step_names())
        assert isinstance(reading, memory_feed.SwapReading)
        return reading

    def test_swap_rows_keep_the_showrunner_and_unit_for_tooltips(self) -> None:
        owner = unit_labels.make_labels("impl", unit_labels.UnderShowrunner("hana", "hana", ""),
                                       unit_labels.UnderDirector("research", "research", ""))
        reading = self.owner_swap_reading(owner)
        row, = [row for row in reading.rows if row.get("unit") == "research"]
        self.assertEqual(row.get("showrunner"), "hana")
        self.assertEqual(row.get("title_kind"), "program")
        self.assertEqual(row["bytes"], admission_records.GIB)
        self.assertEqual(sum(row["bytes"] for row in reading.rows if row["kind"] != "total"), reading.used)

    def test_swap_details_keep_staff_references_separate_from_paths(self) -> None:
        owner = unit_labels.make_labels("builder", unit_labels.UnderShowrunner("hana", "hana", ""),
                                       unit_labels.UnderDirector("research", "research", ""))
        reading = self.owner_swap_reading(owner)
        row, = [row for row in reading.rows if row.get("unit") == "research"]
        self.assertEqual(row["detail"], "builder · builds.slice/owner.scope")
        self.assertEqual(row.get("detail_parts"), [{"kind": "staff", "text": "builder"},
            {"kind": "text", "text": " · builds.slice/owner.scope"}])
        self.assertEqual(staff_names.parts_text(row.get("detail_parts", [])), row["detail"])

    def test_grouped_process_labels_keep_staff_references_separate_from_programs(self) -> None:
        reading = captured_reading(running=(leftover_step(45, small=True),))
        proc = self.root / "proc"
        write_process(proc, 45)
        _ = (proc / "45" / "cmdline").write_bytes(b"python3\0-u\0/fixture/check_rows.py\0")
        owner = unit_labels.UnitLabels("helper", "hana", "builder")
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(unit_labels, "unit_labels_for", return_value={"fixture": owner})):
            page = memory_feed.reading()
        grouped, = [cast(memory_feed.RunRow, row) for row in page["walk"] if row.get("group")]
        part, = grouped["parts"]
        self.assertEqual(part.get("who_parts"), [{"kind": "staff", "text": "builder"},
            {"kind": "text", "text": " · check_rows.py"}])
        self.assertEqual(staff_names.parts_text(part.get("who_parts", [])), part["who"])

    def test_mac_hold_reasons_keep_staff_parts(self) -> None:
        from scripts.build_queue.test_target_availability import MAC_BLOCK

        routes = target_availability.MacRoutes((snapshot(MAC, MAC_BLOCK),),
            {"mac-session": unit_labels.UnitLabels("helper", "hana", "builder")}, UTC)
        hold = memory_feed.target_notes(target_config.MAC_ONLY, routes, "nextest", "/fixture", "")
        parts = hold.get("because_parts")
        self.assertIsNotNone(parts)
        self.assertEqual([part["text"] for part in parts or [] if part["kind"] == "staff"], ["builder", "hana"])
        self.assertEqual("".join(part["text"] for part in parts or []), hold.get("because"))

    def test_swap_keeps_the_unattributed_bytes_without_rounding_them_away(self) -> None:
        self.host_swap(2 ** 20, 2 ** 20)
        _ = self.cgroup("system.slice", 2 ** 20 - 7)
        swap = memory_feed.read_swap((), {}, 6, read_step_names())
        assert isinstance(swap, memory_feed.SwapReading)
        residual = next(row for row in swap.rows if row["kind"] == "unattributed")
        self.assertEqual(residual["bytes"], 7)
        self.assertEqual(sum(row["bytes"] for row in swap.rows[:-1]), swap.used)
        self.assertIn("Not attributed: 7 B", memory_feed.swap_text(swap.rows))

    def test_swap_exposes_a_signed_counter_difference_without_changing_owner_values(self) -> None:
        self.host_swap(2 ** 20, 2 ** 20)
        _ = self.cgroup("system.slice", 2 ** 20 + 7)
        swap = memory_feed.read_swap((), {}, 6, read_step_names())
        assert isinstance(swap, memory_feed.SwapReading)
        self.assertEqual(swap.rows[0]["bytes"], 2 ** 20 + 7)
        residual = next(row for row in swap.rows if row["kind"] == "difference")
        self.assertEqual((residual["bytes"], residual["name"]), (-7, "Reading difference"))
        self.assertEqual(sum(row["bytes"] for row in swap.rows[:-1]), swap.used)

    def test_swap_names_ci_instead_of_the_raw_service_step(self) -> None:
        self.host_swap(2 * admission_records.GIB, admission_records.GIB)
        _ = self.cgroup("hana.slice", admission_records.GIB)
        path = self.cgroup("hana.slice/hana-ci.slice", admission_records.GIB)
        step = admission_records.RunningStep("", "hana-ci.slice", "service", str(path), 0,
            admission_records.UnownedSliceMemory(admission_records.GIB, cgroup=str(path), owner="hana-ci.slice",
                                    pid=admission_records.PidNotRecorded(), job_state=IdleService()), pid=admission_records.PidNotRecorded())
        swap = memory_feed.read_swap((step,), {}, 6, read_step_names())
        assert isinstance(swap, memory_feed.SwapReading)
        self.assertEqual(swap.rows[0]["name"], "hana CI")
        self.assertEqual(swap.rows[0]["bytes"], admission_records.GIB)

    def test_swap_names_between_steps_owners_in_the_shared_rows(self) -> None:
        self.host_swap(4 * admission_records.GIB, 2 * admission_records.GIB)
        _ = self.cgroup("builds.slice", 2 * admission_records.GIB)
        steps: list[admission_records.RunningStep] = []
        labels = {name: unit_labels.UnitLabels(name) for name in ("owner-a", "owner-b")}
        for owner in labels:
            path = self.cgroup("builds.slice/" + owner + ".scope", admission_records.GIB)
            steps.append(admission_records.RunningStep(owner, owner, "between steps", str(path), 0,
                admission_records.UnownedSliceMemory(admission_records.GIB, cgroup=str(path), owner=owner, pid=admission_records.PidNotRecorded()),
                pid=admission_records.PidNotRecorded()))
        swap = memory_feed.read_swap(steps, labels, 6, read_step_names())
        assert isinstance(swap, memory_feed.SwapReading)
        self.assertEqual({row["name"] for row in swap.rows if row["kind"] == "owner"}, set(labels))
        self.assertTrue(all("between steps" in row["detail"] for row in swap.rows if row["kind"] == "owner"))
        self.assertTrue(all(row.get("title_kind") == "staff" for row in swap.rows if row["kind"] == "owner"))

    def test_swap_leaves_detached_tool_scope_remainder_unclassified(self) -> None:
        gib = admission_records.GIB
        self.host_swap(8 * gib, 4 * gib)
        builds = self.cgroup("builds.slice", 4 * gib)
        detached = "builds.slice/builds-detached.slice"
        _ = self.cgroup(detached, 4 * gib)
        parent = self.cgroup(detached + "/tool.scope", 4 * gib)
        child = self.cgroup(detached + "/tool.scope/cargo.scope", gib)
        tool = admission_records.RunningStep("tool-owner", "tool owner", "between steps", str(parent), 0,
            admission_records.UnownedSliceMemory(gib, cgroup=str(parent), pid=700001), pid=700001)
        cargo = reservation_reading().view.running[0]
        memory = cargo.measured
        assert isinstance(memory, admission_records.MeasuredMemory)
        cargo = replace(cargo, measured=replace(memory, anonymous_source=admission_records.ScopeSource(child, frozenset())))
        with mock.patch.object(gate, "builds_slice_cgroup", return_value=builds):
            names = read_step_names()
            baseline = memory_feed.read_swap((cargo,), {}, 6, names)
            swap = memory_feed.read_swap((tool, cargo), {}, 6, names)
        assert isinstance(swap, memory_feed.SwapReading)
        self.assertEqual(swap, baseline)
        remainder, = (row for row in swap.rows if row["kind"] == "ungrouped")
        self.assertEqual((remainder["name"], remainder["bytes"]), ("Unclassified groups", 3 * gib))
        owner, = (row for row in swap.rows if row["kind"] == "owner")
        self.assertEqual(owner["bytes"], gib)
        self.assertEqual(swap.builds_ci, 4 * gib)

    def test_swap_shows_both_cgroups_below_a_detached_tool_scope(self) -> None:
        gib = admission_records.GIB
        total = 3 * gib // 4
        self.host_swap(2 * gib, total)
        user = "user.slice/user-1000.slice/user@1000.service"
        for group in ("user.slice", "user.slice/user-1000.slice", user):
            _ = self.cgroup(group, total)
        builds = user + "/builds.slice"
        detached = builds + "/builds-detached.slice"
        run = detached + "/builds-detached-r1.slice"
        for group in (builds, detached, run):
            _ = self.cgroup(group, total)
        parent = self.cgroup(run + "/tool.scope", total)
        _ = self.cgroup(run + "/tool.scope/first.scope", gib // 2)
        _ = self.cgroup(run + "/tool.scope/second.scope", gib // 4)
        tool = admission_records.RunningStep("", "tool", "unowned", str(parent), 0,
            admission_records.UnownedSliceMemory(gib, cgroup=str(parent), owner="tool", pid=700001), pid=700001)
        names = read_step_names()
        baseline = memory_feed.read_swap((), {}, 6, names)
        swap = memory_feed.read_swap((tool,), {}, 6, names)
        assert isinstance(swap, memory_feed.SwapReading)
        self.assertEqual(swap, baseline)
        self.assertEqual([(row["name"], row["bytes"]) for row in swap.rows if row["kind"] == "owner"],
                         [("first", gib // 2), ("second", gib // 4)])

    def test_swap_small_groups_and_total_use_the_same_current_host_reading(self) -> None:
        self.host_swap(4 * admission_records.GIB, 3 * admission_records.GIB)
        for number in range(3):
            _ = self.cgroup(f"owner-{number}.slice", admission_records.GIB // 2)
        gib = admission_records.GIB
        for returned in (0, gib):
            with self.subTest(returned=returned):
                host = admission_records.HostMemory(30 * gib, 60 * gib, 4 * gib, gib, build_and_ci_swap=0,
                                                    swap_in_window_bytes=returned)
                reading = captured_reading(host=host)
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
                      mock.patch.object(gate.SwapInHistory, "read", side_effect=AssertionError("the feed must use the service reading"))):
                    page = memory_feed.reading(replace(memory_feed.read_configuration(), swap_rows=1))
                self.assertEqual((page["swap"]["used_bytes"], page["swap"]["capacity_bytes"]), (3 * gib, 4 * gib))
                self.assertEqual((page["swap"]["builds_ci_bytes"], page["swap"]["outside_bytes"]), (0, 3 * gib))
                self.assertEqual([row["kind"] for row in page["swap_by"]], ["owner", "smaller", "unattributed", "total"])
                self.assertEqual(sum(row["bytes"] for row in page["swap_by"][:-1]), 3 * admission_records.GIB)
                self.assertEqual(page["swap_by"][1]["bytes"], admission_records.GIB)
                self.assertEqual(page["ram"]["swap_target_bytes"], returned)
                self.assertEqual(page["ram"]["swap_covered_bytes"], returned)

    def test_swap_smaller_members_keep_the_staff_title_marker(self) -> None:
        rows = [memory_feed.swap_row(name, "", 1024, "outside", "owner")
                for name in ("staff owner", "program", "old reading")]
        rows[0]["title_kind"] = "staff"
        rows[1]["title_kind"] = "program"
        grouped, = memory_feed.pool_rows(rows, (), "outside", 3072, 0)
        self.assertEqual(grouped["kind"], "smaller")
        self.assertEqual(grouped["members"], [
            {"name": "staff owner", "bytes": 1024, "title_kind": "staff"},
            {"name": "program", "bytes": 1024, "title_kind": "program"},
            {"name": "old reading", "bytes": 1024, "title_kind": "program"}])

    def test_between_steps_hold_names_the_ended_step_and_current_unit(self) -> None:
        now = 200_000
        policy = admission_policy_fixture()
        run = LiveBuildRun(100, "100", now - 60)
        member = staff.StaffRow(staff.StaffId("fixed-director"), "unit", "startup", staff.StaffRoot(),
                                current_session=staff.StaffId("new-session"))
        reservation = memory_tests.ProgressKeepAsideTests.reservation("test")
        reservation.update(package="hana", run_origin=admission_records.CallerRun(run), slot_staff=member.id)
        slot = gate.ended_step_slot(reservation, now, policy)
        assert isinstance(slot, admission_records.BetweenStepsSlot)
        inputs = replace(captured_reading().explanation.decided_from, slots=(slot,))
        reading = gate.queue_read_from(inputs, policy)
        self.assertEqual(reading.view.running, ())
        self.assertEqual(reading.view.waiting, ())
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((member,)))):
            page = memory_feed.reading()
        row, = page["walk"]
        self.assertEqual(row["name"], "test hana")
        self.assertEqual(row["unit"], "startup")
        self.assertEqual(row.get("detail"), "between steps")

    def test_released_holds_keep_the_recorded_caller(self) -> None:
        members = staff.StaffSnapshot(())
        for continuation in (False, True):
            with (self.subTest(continuation=continuation), AdmissionFixture() as fixture,
                  mock.patch.object(staff, "local_snapshot", return_value=members),
                  mock.patch.object(staff, "snapshot", return_value=members)):
                run = fixture.run_owner()
                first = fixture.request(run=run, step="gate", need=6 * admission_records.GIB)
                first = replace(first, identity=replace(first.identity, name="caller with no staff record"))
                grant = only_admitted(fixture.server.handle_request(first))
                if continuation:
                    fixture.free(0)
                    _ = fixture.server.handle_request(fixture.request(run=run, need=8 * admission_records.GIB))
                fixture.release(grant)
                _ = fixture.run_pass()
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                slot, = reply.reading.explanation.decided_from.slots
                self.assertIsInstance(slot, admission_records.ContinuationSlot if continuation
                                      else admission_records.BetweenStepsSlot)
                for stage, current in (("released", reply), ("snapshot", wire.parse_reply(wire.encode_reply(reply)))):
                    with self.subTest(stage=stage):
                        assert isinstance(current, wire.ViewReply)
                        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(current.reading)):
                            page = memory_feed.reading()
                        row, = [row for row in page["walk"] if row.get("between_steps")]
                        self.assertEqual(row["name"], first.identity.name)
                        self.assertEqual(row.get("caller"), first.identity.name)

    def test_legacy_gate_holds_recover_the_fixed_staff_caller(self) -> None:
        inputs = captured_reading().explanation.decided_from
        policy = admission_policy_fixture()
        run = LiveBuildRun(101, "start", inputs.now - 15)
        director = staff.StaffRow(staff.StaffId("director"), "unit", "fixture unit", staff.StaffRoot(),
                                  current_session=staff.StaffId("director-session"))
        worker = staff.StaffRow(staff.StaffId("worker"), "worker", "fixture worker", staff.ReportsTo(director.id),
                                current_session=staff.StaffId("worker-session"))
        members = staff.StaffSnapshot((director, worker))
        absent = admission_records.EndedStepFactNotRecorded()
        for caller, member, expected in (
            (None, worker.id, worker.name),
            ({"kind": "EndedStepFactNotRecorded"}, worker.id, worker.name),
            ("", worker.id, worker.name),
            (" \t", worker.id, worker.name),
            ("—", worker.id, worker.name),
            (None, staff.StaffId("missing"), "unregistered session"),
            (None, admission_records.NoEndedStepStaff(), "unregistered session"),
            ("—", admission_records.NoEndedStepStaff(), "unregistered session"),
            ("owner not recorded", worker.id, worker.name),
            ("$CALLER", worker.id, worker.name),
            ("556caf6e-713d-47c0-a8fc-5dd5e8c26ce4", worker.id, worker.name),
            ("saved caller", staff.StaffId("missing"), "saved caller"),
        ):
            for legacy in (False, True):
                for continuation in (False, True):
                    with self.subTest(caller=caller, member=member, legacy=legacy, continuation=continuation):
                        facts = admission_records.EndedStepFacts("merge-gate", admission_records.PackageNotRecorded(),
                                                                 absent, absent, member)
                        held = admission_records.BetweenStepsSlot(run, "finished", admission_records.GIB,
                                                                  inputs.now + 10, facts)
                        slot = admission_records.ContinuationSlot(held, "next") if continuation else held
                        reading = gate.queue_read_from(replace(inputs, slots=(slot,), reservations=()), policy)
                        raw = cast(dict[str, object], codec.snapshot_value(reading))
                        explanation = cast(dict[str, object], raw["explanation"])
                        decided = cast(dict[str, object], explanation["decided_from"])
                        encoded, = cast(list[dict[str, object]], decided["slots"])
                        if continuation:
                            encoded = cast(dict[str, object], encoded["slot"])
                        identity = cast(dict[str, object], encoded["identity"])
                        if legacy:
                            identity.update(kind="EndedStepIdentity", source="step", name="merge-gate")
                        _ = identity.pop("caller_name", None)
                        if caller is not None:
                            identity["caller_name"] = caller
                        encoded["identity"] = identity

                        def view(_folder: Path) -> queue_snapshot.PublishedView:
                            decoded = cast(admission_records.QueueRead, codec.parse_snapshot_value(raw, admission_records.QueueRead))
                            decoded_slot, = decoded.explanation.decided_from.slots
                            restored = decoded_slot.slot if isinstance(decoded_slot, admission_records.ContinuationSlot) else decoded_slot
                            self.assertEqual(restored.identity, replace(facts, caller_name=expected))
                            self.assertEqual(restored.bytes, held.bytes)
                            return fixture_published_view(decoded)

                        with (mock.patch.object(staff, "snapshot", return_value=members),
                              mock.patch.object(queue_snapshot, "read_view_snapshot", side_effect=view)):
                            page = memory_feed.reading()
                        row, = [row for row in page["walk"] if row.get("between_steps")]
                        self.assertEqual(row["name"], expected)
                        self.assertEqual(row.get("caller"), expected)
                        self.assertEqual(page["reserved"], 1.0)

    def test_between_steps_holds_name_their_distinct_owners(self) -> None:
        inputs = captured_reading().explanation.decided_from
        policy = admission_policy_fixture()
        slots: list[admission_records.RunSlot] = []
        for pid, step, repository in ((101, "test hana", "hana"), (102, "check", "hana"),
                                      (103, "/fixture/check_rows.py", "hana"), (104, "", "widget")):
            run = LiveBuildRun(pid, str(pid), inputs.now - 15)
            reservation = memory_tests.ProgressKeepAsideTests.reservation(step, identity=str(pid), need=admission_records.GIB)
            reservation.update(repo=repository, run_origin=admission_records.CallerRun(run))
            ended = gate.ended_step_slot(reservation, inputs.now, policy)
            assert isinstance(ended, admission_records.BetweenStepsSlot)
            slots.append(ended if pid != 103 else admission_records.ContinuationSlot(ended, "successor"))
        reading = gate.queue_read_from(replace(inputs, slots=tuple(slots)), policy)
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            page = memory_feed.reading()
        self.assertEqual(len(page["walk"]), 4)
        self.assertEqual({row["pid"]: row["name"] for row in page["walk"]},
                         {101: "test hana", 102: "check", 103: "check_rows.py", 104: "widget build"})
        self.assertTrue(all(row.get("detail") == "between steps" for row in page["walk"]))

    def test_older_gap_remains_in_ram_without_an_unnamed_row(self) -> None:
        inputs = captured_reading().explanation.decided_from
        policy = admission_policy_fixture()
        run = LiveBuildRun(101, "101", inputs.now - 15)
        absent = admission_records.EndedStepFactNotRecorded()
        identity = admission_records.EndedStepFacts("check", admission_records.PackageNotRecorded(),
                                                    absent, absent, admission_records.NoEndedStepStaff())
        slot = admission_records.BetweenStepsSlot(run, "finished", admission_records.GIB, inputs.now + 10, identity)
        reading = gate.queue_read_from(replace(inputs, slots=(slot,)), policy)
        raw = cast(dict[str, object], codec.snapshot_value(reading))
        explanation = cast(dict[str, object], raw["explanation"])
        decided = cast(dict[str, object], explanation["decided_from"])
        del cast(list[dict[str, object]], decided["slots"])[0]["identity"]
        legacy = cast(admission_records.QueueRead, codec.parse_snapshot_value(raw, admission_records.QueueRead))
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(legacy)):
            page = memory_feed.reading()
        self.assertEqual(page["walk"], [])
        self.assertEqual(page["reserved"], 1.0)
        room = gate.inputs_room(legacy.explanation.decided_from, policy)
        assert isinstance(room, admission_records.MemoryRoom)
        self.assertEqual(room.kept, admission_records.GIB)

    def test_between_steps_hold_colors_the_resolved_unit(self) -> None:
        inputs = captured_reading().explanation.decided_from
        run = LiveBuildRun(101, "retained-run", inputs.now - 60)
        director = staff.StaffRow(staff.StaffId("panes"), "unit", "panes", staff.StaffRoot(),
                                 current_session=staff.StaffId("director-session"))
        facts = admission_records.EndedStepFacts("merge-gate", admission_records.PackageNotRecorded(),
            admission_records.EndedStepFactNotRecorded(), "/fixture", director.id)
        slot = admission_records.BetweenStepsSlot(run, "previous", admission_records.GIB, inputs.now + 10, facts)
        reading = gate.queue_read_from(replace(inputs, slots=(slot,)), admission_policy_fixture())
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((director,)))):
            page = memory_feed.reading()
        row, = [row for row in page["walk"] if row.get("between_steps")]
        self.assertEqual(row["name"], "panes")
        self.assertEqual(row.get("detail"), "between steps")
        self.assertEqual(row.get("name_parts"), [{"kind": "staff", "text": "panes"}])
        self.assertIn('data-color-text="role.unit-director"', page.get("staff_names", {}).get("panes", ""))

    def test_pressure_failure_names_the_path_without_the_python_error(self) -> None:
        values = memory_feed.read_stalls()
        for value in values.values():
            assert isinstance(value, memory_feed.PressureNotRecorded)
            self.assertEqual(value.reason, f"Pressure is unreadable: {self.root / 'proc/pressure/memory'}")
            self.assertNotIn("Errno", value.reason)

    def test_history_shorter_than_the_recent_window_names_the_configuration_error(self) -> None:
        _ = (self.root / "buildlog.conf").write_text("# whole: 1..\nTOP_COST_RECENT_DAYS=8\n")
        with (mock.patch.object(charts, "page_data") as provider,
              mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reservation_reading()))):
            page = memory_feed.reading()
            text = memory_feed.report_file(memory_feed.report_reading(memory_feed.read_configuration().history_days)).text
        provider.assert_not_called()
        self.assertIn("history_days", text)
        self.assertIn("TOP_COST_RECENT_DAYS", text)
        self.assertTrue(page["walk"])

    def test_installed_feed_sources_and_startup_files_change_the_fixed_version(self) -> None:
        installed = self.root / "installed"
        for relative in memory_feed.FEED_CODE_PATHS:
            target = installed / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_text("fixture\n")
        cases = (
            ("scripts/build_queue/memory_feed.py", True),
            ("scripts/buildlog/charts.py", True),
            ("scripts/buildlog/holds.py", True),
            ("scripts/build_queue/memory_live.html", True),
            ("config/memory_feed.conf", True),
            ("config/build_steps.conf", False),
            ("scripts/unrelated.py", False),
        )
        before = memory_feed.installed_feed_code_version(installed)
        for relative, changes_version in cases:
            with self.subTest(path=relative):
                target = installed / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                previous = target.read_text() if target.exists() else ""
                _ = target.write_text("changed fixture\n")
                self.assertEqual(memory_feed.installed_feed_code_version(installed) != before, changes_version)
                _ = target.write_text(previous)
        (installed / "scripts/build_queue/memory_feed.py").unlink()
        self.assertEqual(memory_feed.installed_feed_code_version(installed), "")

    def test_an_installed_schema_change_requires_a_restart_before_the_next_read(self) -> None:
        installed = self.root / "installed"
        source = installed / "scripts" / "build_queue" / "admission_records.py"
        for relative in memory_feed.FEED_CODE_PATHS:
            target = installed / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_text("fixture\n")
        _ = source.write_text("schema = 'before'\n")
        before = memory_feed.installed_feed_code_version(installed)

        def install_schema(seconds: float) -> None:
            self.assertEqual(seconds, 0.125)
            _ = source.write_text("schema = 'after'\n")
            self.assertNotEqual(memory_feed.installed_feed_code_version(installed), before)

        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(captured_reading())) as view,
              mock.patch.object(memory_feed, "LOADED_FEED_CODE_VERSION", before),
              mock.patch.object(time, "sleep", side_effect=install_schema)):
            self.assertEqual(memory_feed.main(["--code-root", str(installed)]), 1)
        view.assert_called_once_with(self.root / "buildlog" / "admission")
        self.assertNotEqual(memory_feed.installed_feed_code_version(installed), before)
        changed = replace(captured_reading(), explanation=replace(captured_reading().explanation, now=200_001))
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(changed)) as restarted,
              mock.patch.object(memory_feed, "LOADED_FEED_CODE_VERSION", memory_feed.installed_feed_code_version(installed))):
            self.assertEqual(memory_feed.main(["--once", "--code-root", str(installed)]), 0)
        restarted.assert_called_once_with(self.root / "buildlog" / "admission")
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertEqual(page["read_at"], 200_001)

    def test_each_wait_state_keeps_its_reason_and_the_other_readings(self) -> None:
        reasons = (
            (admission_records.PressureUnavailable("cannot read /private/proc/pressure/memory"), "/private/proc/pressure/memory", False),
            (admission_records.StartSpacingWait(200_010), "another step just started; the queue waits a few seconds between starts", False),
            (placement.CargoFolderWait(), placement.CARGO_FOLDER_WAIT_REASON, False),
            (admission_records.BehindHead("first", 1, "head-session"), "behind", False),
            (admission_records.BehindHead("compiler", 2, "second-session"), "behind #2 · compiler: nextest", True),
        )
        for reason, expected, has_blocker in reasons:
            with self.subTest(reason=reason):
                reading = with_wait_reason(reservation_reading(), reason)
                if has_blocker:
                    first = admission_records.WaitingStep(201, "first-session", "first", "build",
                        "/fixture", 1, admission_records.GIB, admission_records.ReadyForAdmission())
                    second = replace(first, pid=202, session_id="second-session", session_name="second",
                        step="nextest", since=2, reason=admission_records.NeedsMemory(), unit="compiler")
                    reading = replace(reading, view=replace(reading.view,
                        waiting=(first, second, *reading.view.waiting)))
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
                    page = memory_feed.reading()
                row = page["waiters"][-1 if has_blocker else 0]
                self.assertIn(expected, row["reason"])
                if isinstance(reason, placement.CargoFolderWait):
                    self.assertEqual(row["name"], "fixture: nextest")
                if has_blocker:
                    self.assertNotIn("first step in line", row["reason"])
                self.assertTrue(page["walk"])
                self.assertGreater(page["room"], 0)

    def test_a_schema_install_during_import_restarts_a_fresh_interpreter(self) -> None:
        cases = (("scripts.build_queue", "snapshot_codec", "codec"), ("scripts.buildlog", "charts", "charts"))
        for package, module, alias in cases:
            with self.subTest(module=module):
                installed = self.root / "installed" / module
                source = Path(memory_feed.__file__).resolve().parents[1]
                _ = shutil.copytree(source, installed / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                (installed / "config").mkdir()
                _ = shutil.copyfile(conf.path("memory_feed"), installed / "config/memory_feed.conf")
                _ = write_config_fixture(installed / 'config/console.conf')
                probe = textwrap.dedent("""
                    import builtins
                    import json
                    import sys
                    from pathlib import Path
                    from unittest import mock

                    root = Path(sys.argv[1])
                    source = root / sys.argv[2].replace('.', '/') / (sys.argv[3] + '.py')
                    original_import = builtins.__import__
                    changed = False
                    def install_during_import(name, globals=None, locals=None, fromlist=(), level=0):
                        global changed
                        result = original_import(name, globals, locals, fromlist, level)
                        if name == sys.argv[2] and sys.argv[3] in fromlist and not changed:
                            changed = True
                            source.write_text(source.read_text() + '\\nfrom dataclasses import dataclass\\n'
                                '@dataclass(frozen=True)\\nclass InstalledSchemaRevision:\\n    revision: str\\n')
                        return result
                    builtins.__import__ = install_during_import
                    from scripts.build_queue import memory_feed
                    builtins.__import__ = original_import
                    assert changed
                    assert not hasattr(getattr(memory_feed, sys.argv[4]), 'InstalledSchemaRevision')
                    with (mock.patch.object(memory_feed.program_memory_feed, 'configured_feed', return_value=None),
                          mock.patch.object(memory_feed.admission_client, 'view',
                            side_effect=AssertionError('The old decoder must not read the queue'))):
                        status = memory_feed.main(['--once', '--code-root', str(root)])
                    print(json.dumps({'status': status, 'changed':
                        memory_feed.LOADED_FEED_CODE_VERSION != memory_feed.installed_feed_code_version(root)}))
                    sys.exit(status)
                """)
                result = subprocess.run([sys.executable, "-c", probe, str(installed), package, module, alias], cwd=installed,
                    env={**os.environ, "PYTHONPATH": str(installed / "scripts/lib/python_startup")}, capture_output=True, text=True,
                    timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), {"status": 1, "changed": True})
                restarted = textwrap.dedent("""
                    import sys
                    from unittest import mock
                    from scripts.build_queue import memory_feed
                    decoded = memory_feed.codec.parse_snapshot_value(
                        {'kind': 'InstalledSchemaRevision', 'revision': 'after'},
                        getattr(memory_feed, sys.argv[2]).InstalledSchemaRevision)
                    with (mock.patch.object(memory_feed.program_memory_feed, 'configured_feed', return_value=None),
                          mock.patch.object(memory_feed.queue_view, 'refresh_mac_builds'),
                          mock.patch.object(memory_feed, 'report_reading',
                            return_value=memory_feed.charts.IndexUnavailable('private report fixture')),
                          mock.patch.object(memory_feed, 'reading', return_value={
                            'ok': True, 'read_at': 200001, 'decoder_revision': decoded.revision})):
                        sys.exit(memory_feed.main(['--once', '--code-root', sys.argv[1]]))
                """)
                result = subprocess.run([sys.executable, "-c", restarted, str(installed), alias], cwd=installed,
                    env={**os.environ, "PYTHONPATH": str(installed / "scripts/lib/python_startup")}, capture_output=True, text=True,
                    timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
                self.assertEqual(page["decoder_revision"], "after")

    def test_retired_copy_wait_does_not_reach_the_queued_table(self) -> None:
        reading = cloud_reading(with_wait_reason(captured_reading(), admission_records.NeedsMemory()))
        folder = self.root / "buildlog" / "admission"
        folder.mkdir(parents=True)
        now = reading.explanation.now
        encoded = wire.encode_reply(wire.ViewReply(reading))
        queue_snapshot.write_view_snapshot(folder, now, now, encoded.encode())
        before = memory_feed.reading()

        envelope = cast(dict[str, object], json.loads(encoded))
        reply = cast(dict[str, object], envelope["data"])
        saved = cast(dict[str, object], reply["reading"])
        identity = wire.Identity("slot-session", "slot-showrunner", "gate", "proof", "slot-unit")
        for facts in (identity, replace(identity, job=StepJob("test-all", unit="widget"))):
            with self.subTest(job=facts.job):
                saved["repository_slots"] = [{
                    "kind": "RepositorySlotRow",
                    "owner": codec.snapshot_value(wire.Owner(701001, "start")),
                    "identity": codec.snapshot_value(facts),
                    "since": now - 60,
                    "machine": "natedev",
                    "wait": {"kind": "RepositorySlotWaiting", "repository": "widget", "held": [1, 2],
                             "maximum": 2, "maximum_key": "slots.natedev.widget.max"},
                }]
                queue_snapshot.write_view_snapshot(folder, now, now, json.dumps(envelope).encode())
                after = memory_feed.reading()
                self.assertEqual(after["waiters"], before["waiters"])
                self.assertEqual(after["reserved"], before["reserved"])
                self.assertEqual(after["used"], before["used"])
                self.assertEqual(after["waiting"], before["waiting"])

    def test_wrapped_verify_command_names_the_first_call_in_feed_notes(self) -> None:
        verify = "bash ~/.claude/scripts/direct/verify.sh --session-dir"
        cases = (
            ("startup", f"{verify} /fixture/sessions/startup test hana --filter 'name with spaces'",
             ("hana", "--filter", "name with spaces"), "verify test hana --filter 'name with spaces'"),
            ("widget", f"direnv allow . >/dev/null; direnv exec . {verify} /fixture/sessions/widget test hana_diegetic; "
             + f"{verify} /fixture/sessions/widget build hana_diegetic --example scrolling --local test",
             ("hana_diegetic",), "verify test hana_diegetic"),
        )
        for unit, command, arguments, call in cases:
            with (self.subTest(unit=unit),
                  mock.patch.object(slot_wait, "verify_package", return_value=arguments[0]) as parse):
                name, _, facts = slot_wait.command_slot_job(("bash", "-c", command), RECORDED_STEP_NAMES)
                assert isinstance(name, memory_feed.QueueRowName)
                step = f"test {arguments[0]}"
                row = memory_feed.queue_row_labels(name, name, unit=memory_feed.StaffUnitName(unit))
                self.assertEqual(row["title"], {"unit": unit, "step": step})
                self.assertEqual(memory_feed.call_notes(facts, name).get("verify"),
                    {"verb": "test", "call": call, "position": 1, "total": 1, "step": step})
                parse.assert_called_once_with("test", arguments)

    def test_queue_row_name_rejects_shell_words_and_ids_inside_names(self) -> None:
        for name in ("startup bash", "startup /bin/env", "exec -a", "startup $UNIT",
                     "test 556caf6e-713d-47c0-a8fc-5dd5e8c26ce4"):
            with self.subTest(name=name):
                self.assertIsInstance(recorded_step(name, read_step_names()), NoNamedStep)
                self.assertEqual(memory_feed.QueueRowName.script("check.service.sh"), "check.service.sh")

    def test_queue_names_are_typed_on_every_machine(self) -> None:
        _ = (self.root / "build_admission.conf").write_text("ci_label=hana CI · {runner} · {job}\n")
        page = self.owner_machine_page(unit_labels.UnitLabels("unknown"), ci=True)
        for row in (*page["walk"], *page["waiters"]):
            with self.subTest(row=row["name"], machine=row.get("machine")):
                self.assertIsInstance(row["name"], memory_feed.QueueRowName)

    def test_queue_row_name_skips_blank_names_wrappers_and_session_ids(self) -> None:
        self.assertIsInstance(recorded_step(" ", read_step_names()), NoNamedStep)
        for wrapper in ("bash", "sh", "zsh", "dash", "env", "direnv", "python", "python3", "python3.13"):
            with self.subTest(wrapper=wrapper):
                self.assertIsInstance(recorded_step(f"/bin/{wrapper}", read_step_names()), NoNamedStep)
                self.assertEqual(memory_feed.QueueRowName.script("script.py"), "script.py")
                self.assertIsInstance(memory_feed.QueueRowName.recorded_program(wrapper), NoNamedStep)
        for program in ("make", "c++"):
            with self.subTest(program=program):
                self.assertEqual(memory_feed.QueueRowName.recorded_program(program), program)
        self.assertIsInstance(recorded_step("556caf6e-713d-47c0-a8fc-5dd5e8c26ce4", read_step_names()), NoNamedStep)

    def test_every_recorded_build_step_has_its_own_name(self) -> None:
        script = Path(__file__).resolve().parents[1] / "direct/verify.sh"
        accepted = re.search(r'^case "\$CMD" in\n    ([\w|-]+)\) ;;', script.read_text(), re.MULTILINE)
        assert accepted is not None
        verbs = set(accepted[1].split("|"))
        jobs = (("merge_gate.py",), ("merge_gate.py", "--tests-only"), ("merge_gate.py", "--nightly"),
                ("merge_gate.py", "--promote-only"), ("targeted_test.py",), ("targeted_test.py", "--module", "test_sample"))
        recorded_jobs: list[str] = []
        for command in jobs:
            job = command_job(command)
            assert isinstance(job, StepJob)
            recorded_jobs.append(job.step)
        steps = verbs | parse.CARGO_BUILD_VERBS | RECORDED_VERBS | set(recorded_jobs)
        for step in sorted(steps):
            with self.subTest(step=step):
                self.assertEqual(parse.step_name(["cargo", step]), step)
                name = recorded_step(step, RECORDED_STEP_NAMES)
                self.assertIsInstance(name, memory_feed.QueueRowName)
                self.assertEqual(name, "cargo run" if step == "run" else step)

    def test_script_name_survives_recorded_package_metadata(self) -> None:
        self.assertEqual(memory_feed.build_row_step("/fixture/check_rows.py", "hana", "hana", "/fixture/hana",
                                                   RECORDED_STEP_NAMES), "check_rows.py")

    def test_between_steps_hold_uses_the_recorded_step_text(self) -> None:
        inputs = captured_reading().explanation.decided_from
        policy = admission_policy_fixture()
        run = LiveBuildRun(101, "101", inputs.now - 15)
        for step, names in ((parse.step_name(["bash", "-c", ":"]), ["bash"]), ("", [])):
            with self.subTest(step=step):
                reservation = memory_tests.ProgressKeepAsideTests.reservation(step, need=admission_records.GIB)
                reservation.update(repo="", worktree="/", run_origin=admission_records.CallerRun(run))
                slot = gate.ended_step_slot(reservation, inputs.now, policy)
                assert isinstance(slot, admission_records.BetweenStepsSlot)
                reading = gate.queue_read_from(replace(inputs, slots=(slot,)), policy)
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
                    page = memory_feed.reading()
                self.assertEqual([row["name"] for row in page["walk"]], names)
                self.assertEqual(page["reserved"], 1.0)

    def test_a_wait_without_a_name_source_keeps_other_waits_in_order(self) -> None:
        base = with_wait_reason(captured_reading(), admission_records.NeedsMemory())
        known = replace(base.view.waiting[0], step="check")
        for step, folder, names in (("", "", []), (parse.step_name(["bash", "-c", ":"]), "/", ["fixture: bash"]),
                                    ("unknown", "/", ["fixture: unknown"])):
            with self.subTest(step=step, folder=folder):
                missing = replace(known, pid=known.pid + 1, step=step, project="", folder=folder)
                reading = cloud_reading(replace(base, view=replace(base.view, waiting=(missing, known))))
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
                    page = memory_feed.reading()
                self.assertEqual([row["name"] for row in page["waiters"]],
                                 [*names, "fixture: check", "route-two"])

    def test_blank_or_unrelated_configured_name_uses_the_verb(self) -> None:
        for text in ("", "umask"):
            with self.subTest(text=text):
                _ = write_config_fixture(self.root / "build_steps.conf", f"names.test={text}\n")
                self.assertEqual(recorded_step("test", read_step_names()), "test")

    def test_recorded_queue_rows_always_have_a_name(self) -> None:
        _ = write_config_fixture(self.root / "build_steps.conf", "names.test=\n")
        make = parse.step_name(["make", "-f", "/fixture/Makefile"])
        bash = parse.step_name(["bash", "-c", ":"])
        cxx = parse.step_name(["c++", "/fixture/main.cpp"])
        cases = (("", "hana"), (" ", "hana"), ("556caf6e-713d-47c0-a8fc-5dd5e8c26ce4", "hana"),
                 ("umask", "hana"), ("$UNIT", "hana"), ("unrecognized", "local repo"),
                 ("unrecognized", "hana"), ("test", "hana"), ("mend", "local repo"), ("install", ""), ("fix", ""),
                 (make, ""), (bash, ""), (cxx, ""))
        policy = admission_policy_fixture()
        gib = admission_records.GIB
        for step, repository in cases:
            with self.subTest(step=step, repository=repository):
                folder = f"/fixture/{repository}" if repository else "/"
                reservation = memory_tests.ProgressKeepAsideTests.reservation(step)
                reservation.update(repo=repository, worktree=folder)
                inputs = memory_tests.ProgressKeepAsideTests.inputs((reservation,), (
                    admission_records.MeasuredMemory(gib, anonymous=admission_records.AnonymousRunUse(gib)),))
                inputs = replace(inputs, running=(replace(inputs.running[0], project=repository),))
                run = LiveBuildRun(101, "101", inputs.now - 15)
                ended = gate.ended_step_slot({**reservation, "run_origin": admission_records.CallerRun(run)}, inputs.now, policy)
                self.assertIsInstance(ended, admission_records.BetweenStepsSlot)
                assert isinstance(ended, admission_records.BetweenStepsSlot)
                legacy = admission_records.BetweenStepsSlot(LiveBuildRun(102, "102", run.started_at),
                    "legacy", gib, ended.expires_at, admission_records.HoldPredatesStepFacts())
                read = with_converted_executions(gate.queue_read_from(replace(inputs, slots=(ended, legacy)), policy))
                read = with_wait_reason(read, admission_records.NeedsMemory())
                wait = replace(read.view.waiting[0], step=step, project=repository, folder=folder)
                token = replace(wait, pid=201, reason=admission_records.TokenWaitingWaiter(wait.since))
                cloud = cloud_reading(replace(read, view=replace(read.view, waiting=(wait, token))))
                cloud = replace(cloud, cloud=tuple(replace(row, identity=replace(row.identity, step=step))
                    for row in cloud.cloud))
                build = queue_view.MacBuild(90082, folder, inputs.now - 30, f"cargo {step}", repository)
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(cloud)),
                      mock.patch.object(queue_view, "read_mac_builds", return_value=queue_view.MacBuildReading(inputs.now, (build,)))):
                    page = memory_feed.reading()
                # Each row shows `<owner>: <what>`; this test checks the "what" part.
                by_pid = {row["pid"]: row["name"].rpartition(codec.ROW_SEPARATOR)[2]
                          for row in (*page["walk"], *page["waiters"]) if isinstance(row["pid"], int)}
                if not repository:
                    expected_names = {pid: step for pid in (50, 101, 200, 201, 900)}
                    for pid, name in expected_names.items():
                        with self.subTest(pid=pid):
                            self.assertIn(pid, by_pid)
                            self.assertEqual(by_pid[pid], name)
                self.assertTrue({50, 101, 200, 201, 900, 700001, 700002, 90082} <= by_pid.keys())
                self.assertNotIn(legacy.run.pid, by_pid)
                shown_reserved = sum(row["reserved"] for row in page["walk"] if "reserved" in row)
                self.assertEqual(page["reserved"], shown_reserved + legacy.bytes / gib)
                if step == "unrecognized":
                    self.assertEqual(by_pid[101], f"{repository} build")
                if step in ("test", "mend"):
                    self.assertEqual(by_pid[101], step)
                for row in (*page["walk"], *page["waiters"]):
                    with self.subTest(pid=row["pid"]):
                        self.assertTrue(row["name"].strip())
                        self.assertNotIn(row["name"], {"run", "Build step", "Between steps", "Unknown program"})

    def bump_fixture(self) -> AdmissionFixture:
        fixture = self.enterContext(AdmissionFixture(log_dir=self.root / "buildlog"))
        fixture.free(0)
        fixture.hold_starts()
        fixture.clock.epoch = datetime(2026, 11, 23, 16, 30, tzinfo=UTC).timestamp()
        request = fixture.request(owner=fixture.fixture_owner(200, "200"))
        request = replace(request, identity=replace(request.identity, unit="report"))
        _ = self.enterContext(mock.patch.object(unit_labels, "unit_labels_for",
            return_value={request.identity.session_id: unit_labels.UnitLabels("report", "fixture-runner", "report")}))
        _ = fixture.server.handle_request(request)
        person_actions.save_bump(fixture.state_dir, person_actions.Bump("gone",
            person_actions.SetBump("departed-step"), "queue", "user", fixture.clock.now(), "urgent"))
        _ = fixture.server.run_pass()
        step_key = fixture.server.waiters[request.episode].row.get("move_id", "")
        assert step_key
        person_actions.save_bump(fixture.state_dir, person_actions.Bump("waiting",
            person_actions.SetBump(step_key), "report", "user", fixture.clock.now(), "urgent"))
        return fixture

    def test_bump_notices_show_saved_time_and_refusal_until_the_pass(self) -> None:
        fixture = self.bump_fixture()
        with mock.patch.dict(os.environ, {"CONF_DIR": str(self.root), "USER_ZONE": "America/Los_Angeles"}):
            waiting = memory_feed.reading()["bump_notices"]
            self.assertEqual(waiting, [
                {"unit": "queue", "detail": "not applied: the step left the queue"},
                {"unit": "report", "detail": "saved at 08:30 PST, waiting for the queue"},
            ])
        _ = fixture.server.run_pass()
        with mock.patch.dict(os.environ, {"CONF_DIR": str(self.root), "USER_ZONE": "America/Los_Angeles"}):
            applied = memory_feed.reading()
        self.assertEqual(applied["bump_notices"], waiting[:1])
        self.assertEqual(applied["queued_priority_units"], ["report"])

    def test_applied_bump_reaches_waiting_and_running_rows_after_restart(self) -> None:
        fixture = self.enterContext(AdmissionFixture(log_dir=self.root / "buildlog"))
        fixture.free(0)
        fixture.hold_starts()
        fixture.clock.epoch = datetime(2026, 11, 23, 20, 22, tzinfo=UTC).timestamp()
        plain = fixture.request(owner=fixture.fixture_owner(200, "200"))
        request = fixture.request(owner=fixture.fixture_owner(201, "201"), since=fixture.clock.now() + 1)
        request = replace(request, identity=replace(request.identity, unit="report"),
                          sidecar=str(fixture.scope(request.owner)))
        _ = fixture.server.handle_request(plain)
        _ = fixture.server.handle_request(request)
        _ = self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", return_value={
            plain.identity.session_id: unit_labels.UnitLabels("phases", "fixture-runner", "phases"),
            request.identity.session_id: unit_labels.UnitLabels("report", "fixture-runner", "report"),
        }))
        key = fixture.server.waiters[request.episode].row.get("move_id", "")
        assert key
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        nate = fixture_member("showrunner", name="nate")
        person_actions.save_bump(fixture.state_dir, person_actions.Bump("move-201",
            person_actions.SetBump(key, 2), "report", str(nate.id), fixture.clock.now(), "urgent"))
        _ = fixture.server.run_pass()
        fixture.restart()
        _ = fixture.server.handle_request(plain)
        _ = fixture.server.handle_request(request)
        _ = fixture.server.run_pass()
        expected: memory_feed.LastMove = {
            "kind": "bump", "who": "nate", "from_place": 2, "to_place": 1, "at": "12:22 PST"}
        with mock.patch.dict(os.environ, {"CONF_DIR": str(self.root), "USER_ZONE": "America/Los_Angeles"}):
            page = memory_feed.reading()
            waiting = page["waiters"]
            self.assertEqual(page["priority_text"], 'queue priority: report (nate: "urgent")')
            self.assertEqual(waiting[0].get("priority_lines"), ['report (nate: "urgent")'])
            self.assertEqual([(row["pid"], row.get("last_move")) for row in waiting],
                             [(201, expected), (200, None)])
        fixture.free(60 * admission_records.GIB)
        write_pressure_reading(fixture.root / "pressure")
        _ = fixture.server.handle_request(request)
        _ = fixture.server.run_pass()
        with mock.patch.dict(os.environ, {"CONF_DIR": str(self.root), "USER_ZONE": "America/Los_Angeles"}):
            running = memory_feed.reading()["walk"]
            moved, = [row for row in running if row["pid"] == 201]
            self.assertEqual(moved.get("last_move"), expected)

    def test_queue_priority_reaches_the_feed_and_clears_when_removed(self) -> None:
        base = captured_reading()
        first = with_queue_priority(base)
        priority = first.explanation.priority
        assert isinstance(priority, admission_records.QueuePriority)
        two = replace(first, explanation=replace(first.explanation, priority=admission_records.QueuePriority(
            (*priority.entries, {"kind": "UnitPriorityEntry", "unit": "cables", "why": "urgent", "set_at": first.explanation.now,
                                 "set_by": "actor"}))))
        cases = ((first, "queue priority: research (the user's bump)"),
                 (two, "queue priority: research (the user's bump), cables (urgent)"), (base, ""))
        for reading, expected in cases:
            with self.subTest(expected=expected):
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
                    payload = memory_feed.reading()
                self.assertEqual(payload["priority_text"], expected)
                entries = reading.explanation.priority
                self.assertEqual(payload["unmatched_priority_entries"],
                    list(entries.entries) if isinstance(entries, admission_records.QueuePriority) else [])

    def test_queued_priorities_follow_the_captured_order_and_clear_when_removed(self) -> None:
        base = reservation_reading()
        for units, expected in (((), []), (("research",), ["research"]),
                                (("research", "cables"), ["research", "cables"]),
                                (("cables", "research", "absent"), ["research", "cables"]),
                                (("absent",), [])):
            with self.subTest(priority=units):
                reading = queued_reading(base, units)
                labels = {step.session_id: unit_labels.UnitLabels(step.unit, "fixture-runner", step.unit)
                          for step in reading.view.waiting}
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
                      mock.patch.object(unit_labels, "unit_labels_for", return_value=labels)):
                    payload = memory_feed.reading()
                self.assertEqual(payload["queued_priority_units"], expected)
                self.assertEqual([row["pid"] for row in payload["waiters"]], [200, 201, 202, 203])
                self.assertEqual([row["since"] for row in payload["waiters"]], [100, 90, 80, 70])
                decoded = cast(dict[str, object], json.loads(memory_feed.page_json(payload)))
                self.assertEqual(decoded["queued_priority_units"], expected)

    def test_queue_notes_follow_the_head_and_the_priority_unit(self) -> None:
        reading = cloud_reading(with_queue_priority(with_wait_reason(
            reservation_reading(), admission_records.StartSpacingWait(110))))
        for priority, unit in ((True, "research"), (False, "research"), (True, "other")):
            with self.subTest(priority=priority, unit=unit):
                labels = {step.session_id: unit_labels.UnitLabels("fixture", "fixture-production", unit)
                          for step in (*reading.view.running, *reading.view.waiting)}
                current = reading if priority else replace(reading, explanation=replace(
                    reading.explanation, priority=admission_records.NoQueuePriority()))
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(current)),
                      mock.patch.object(unit_labels, "unit_labels_for", return_value=labels)):
                    page = memory_feed.reading()
                for row in (*page["walk"], *page["waiters"]):
                    expected = ["research (the user's bump)"] if priority and row["unit"] == "research" else []
                    self.assertEqual(row.get("priority_lines", []), expected)
                    self.assertEqual([entry["unit"] for entry in row.get("priority_entries", [])],
                                     ["research"] if expected else [])
                self.assertEqual(page["unmatched_priority_lines"],
                    ["research (the user's bump)"] if priority and unit == "other" else [])
                self.assertEqual([row for row in page["waiters"] if "head_room" in row], [])
                self.assertNotIn("head_text", page)

    def test_can_run_on_names_the_mac_only_when_its_routes_allow_the_step(self) -> None:
        for fit, expected in ((target_availability.Allowed("busy with nextest hana"), "natedev or Mac"),
                              (target_availability.NotAllowed("creates a Linux install"), "natedev only"),
                              (target_availability.FitUnreadable("Mac state unreadable"), "natedev only"),
                              (target_availability.NoMacTarget(), "natedev only")):
            with self.subTest(fit=fit):
                self.assertEqual(memory_feed.local_runs_on(fit).text(), expected)

    def test_can_run_on_names_a_route_that_limits_the_step_before_the_mac_routes(self) -> None:
        routes = target_availability.MacRoutes((snapshot(MAC, targets.Free()), snapshot(NATEDEV, targets.Free())), {}, UTC)
        for route, expected in ((target_config.LOCAL_CALLER, "natedev only"),
                                (target_config.WRITES_TREE, "natedev only"),
                                (target_config.MAC_ONLY, "Mac only"),
                                (target_config.PinnedTarget("mac"), "Mac only"),
                                (target_config.PinnedTarget("natedev"), "natedev only")):
            with self.subTest(route=route), mock.patch.object(routes, "fit") as fit:
                self.assertEqual(memory_feed.step_runs_on(route, routes, "nextest", "/fixture/hana", "hana").text(), expected)
                self.assertEqual(memory_feed.mac_only_route(route, routes), expected == "Mac only")
                fit.assert_not_called()
        with mock.patch.object(routes, "fit", return_value=target_availability.Allowed("no block or claim recorded")):
            self.assertEqual(memory_feed.step_runs_on(target_config.ANY_ELIGIBLE_TARGET, routes, "nextest",
                                                      "/fixture/hana", "hana"), memory_feed.RunsOn(("natedev", "mac")))

    def test_a_mac_only_step_names_what_holds_the_mac_on_its_queued_run_and_waiting_rows(self) -> None:
        base = with_wait_reason(reservation_reading(), admission_records.ReadyForAdmission())
        reason = "it edits the merged checkout on natedev with mend and fmt, and the push goes from there"
        cases = (
            (target_config.MAC_ONLY, "Mac only", True, "build hold by research (hana) until released"),
            (target_config.PinnedTarget("mac"), "Mac only", True, "build hold by research (hana) until released"),
            (target_config.PinnedTarget("natedev"), "natedev only", False, ""),
            (target_config.LOCAL_CALLER, "natedev only", False, "requested by the caller"),
            (target_config.WRITES_TREE, "natedev only", False, "edits the tree"),
            (target_config.LocalRequired(reason), "natedev only", False,
             "edits the merged checkout on natedev with mend and fmt, and the push goes from there"),
        )
        for route, runs_on, mac_only, because in cases:
            with self.subTest(route=route):
                view, explanation = base.view, base.explanation
                reading = replace(base, view=replace(view,
                    running=tuple(replace(step, route=route) for step in view.running),
                    waiting=tuple(replace(step, route=route) for step in view.waiting)),
                    explanation=replace(explanation, running=tuple(replace(entry, step=replace(entry.step, route=route))
                                                                  for entry in explanation.running)))
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
                      mock.patch.object(target_availability, "read_snapshots", return_value=(
                          snapshot(MAC, targets.Free()), snapshot(NATEDEV, targets.Free()))),
                      mock.patch.object(target_availability.MacRoutes, "holds", return_value="build hold by research (hana) until released")):
                    page = memory_feed.reading()
                running, = [row for row in page["walk"] if row.get("queued")]
                waiting, = page["waiters"]
                for row in (running, waiting):
                    self.assertEqual(row.get("because"), because)
                    if mac_only:
                        self.assertEqual((row.get("runs_on"), row.get("mac_state")),
                                         ("Mac only", "build hold by research (hana) until released"))
                    else:
                        self.assertEqual(row.get("runs_on"), runs_on)
                        self.assertNotIn("mac_state", row)
                    self.assertEqual(row.get("mac_text"), "")

    def test_target_notes_name_what_holds_the_mac_only_for_a_mac_only_step_while_something_holds_it(self) -> None:
        routes = target_availability.MacRoutes((snapshot(MAC, targets.Free()), snapshot(NATEDEV, targets.Free())), {}, UTC)
        with mock.patch.object(routes, "holds", return_value="busy with check cargo-handler"):
            self.assertEqual(memory_feed.target_notes(target_config.PinnedTarget("mac"), routes, "nextest", "/r", "p"),
                             {"runs_on": "Mac only", "targets": ["mac"], "because": "busy with check cargo-handler", "mac_state": "busy with check cargo-handler",
                              "mac_text": ""})
            self.assertEqual(memory_feed.target_notes(target_config.LOCAL_CALLER, routes, "nextest", "/r", "p"),
                             {"runs_on": "natedev only", "targets": ["natedev"], "because": "requested by the caller",
                              "mac_text": ""})
        with mock.patch.object(routes, "holds", return_value=""):
            self.assertEqual(memory_feed.target_notes(target_config.MAC_ONLY, routes, "nextest", "/r", "p"),
                             {"runs_on": "Mac only", "targets": ["mac"], "because": "", "mac_text": "Mac: no block or claim recorded"})

    def macroute_page(self, *, local_reason: str = "") -> memory_feed.MemoryPage:
        """Build three rows from their recorded placement routes."""
        from scripts.build_targets import targets
        from scripts.build_targets.target_config import SshTarget
        from scripts.mac_test import claim_state, eligibility, mac_test

        reading = queued_reading(captured_reading(host=admission_records.HostMemory(
            0, 60 * admission_records.GIB)), ("build", "mend", "direct"))
        routes = (target_config.ANY_ELIGIBLE_TARGET, target_config.WRITES_TREE,
                  target_config.LocalRequired(local_reason) if local_reason else admission_records.DIRECT_CARGO)
        rows = tuple(replace(row, pid=8100 + index, step=step, route=route,
                             reason=admission_records.NeedsMemory(need=row.need, free=0))
                     for index, (row, step, route) in enumerate(zip(
                         reading.view.waiting[:3], ("build", "mend", "build"), routes, strict=True)))
        reading = replace(reading, view=replace(reading.view, waiting=rows))
        # The Mac fit note reads the claim folder, so the running job has its claim there too.
        record: claim_state.ProcessBoundClaim = {
            "claim_id": "fixture", "episode": "fixture", "attempt": "fixture", "submitting_session": "", "pid": 4242,
            "proc_start": "1", "what": "nextest hana_catalyst", "worktree": "/fixture/hana_catalyst",
            "since": datetime.now(UTC).isoformat()}
        claim = claim_state.HeldClaim(RepositoryScope("hana_catalyst"), self.root / "claim.json", record)
        snapshots = (targets.TargetSnapshot(SshTarget("mac", "macos", self.root / "mac.conf"),
                     (targets.Running((targets.running_job(claim),)),), (claim,), eligibility.NO_BACKOFF),)
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(mac_test, "run_is_live", return_value=True),
              mock.patch.object(target_availability, "read_snapshots", return_value=snapshots),
              mock.patch.object(build_targets, "step_policy", return_value=eligibility.KnownRepository("handler", "/r"))):
            return memory_feed.reading()

    def test_direct_request_reason_reaches_the_waiter_page(self) -> None:
        for reason in ("", "it edits the merged checkout on natedev with mend and fmt, and the push goes from there"):
            with self.subTest(reason=reason), AdmissionFixture() as fixture:
                _ = fixture.occupy_host()
                original = fixture.request(step="check")
                fixture.free(0)
                with (mock.patch.dict(os.environ, {"BUILD_QUEUE_LOCAL_REASON": reason}),
                      mock.patch.object(admission_client, "CLOCK", fixture.clock),
                      mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                          original.profile, step_estimator.ReusableEstimate()))):
                    request = admission_client.step_request(list(original.argv), original.cwd, original.owner,
                        original.since, original.sidecar, original.episode)
                with fixture.loop_turn():
                    replies = fixture.server.handle_request(wire.parse_request(wire.encode_request(request)))
                    self.assertTrue(any(isinstance(reply, wire.Wait) for reply in replies), repr(replies))
                    reply, = fixture.server.handle_request(wire.ViewRequest())
                    assert isinstance(reply, wire.ViewReply)
                    reading = reply.reading
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
                    page = memory_feed.reading()
                row, = page["waiters"]
                self.assertEqual(row.get("runs_on"), "natedev only")
                self.assertEqual(row.get("mac_text"), "")
                self.assertEqual(row.get("because"), reason.removeprefix("it ") if reason else "direct cargo command")

    def test_verify_wait_at_the_natedev_limit_keeps_its_mac_route(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request, publish_candidate
        from scripts.mac_test import eligibility

        with PlacementFixture() as fixture:
            fixture.configure(slot_gap_seconds=60)
            config = fixture.root / "build_admission.conf"
            _ = config.write_text(config.read_text() + f"\nmax_runs.{fixture.server.host}.fixture=0\nmax_runs.exempt_unit=natemerge\n")
            _ = fixture.run_pass()
            fixture.clock.advance(1)
            step = placement_request(fixture)
            _ = publish_candidate(fixture, step, placement.TemporaryExclusion("busy"))
            request = replace(step,
                identity=replace(step.identity, verify=admission_records.VerifyCall("test", "verify test", 1, 1)),
                constraints=replace(step.constraints, candidates=(local_candidate(fixture), *step.constraints.candidates)))
            reply, = fixture.connect(request).replies()
            assert isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait), reply
            reason = reply.wait.reason
            self.assertEqual(reason.cause if isinstance(reason, admission_records.HeadAdmissionStatus) else reason,
                             admission_records.RunLimitWait(0, 0, "fixture", fixture.server.host))
            view, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(view, wire.ViewReply)
            job = targets.RunningJob("nextest fixture", datetime.now(UTC), targets.NotSeenIdle())
            snapshots = (snapshot(MAC, targets.Running((job,))), snapshot(NATEDEV, targets.Free()))
            with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(view.reading)),
                  mock.patch.object(target_availability, "read_snapshots", return_value=snapshots),
                  mock.patch.object(build_targets, "step_policy",
                                    return_value=eligibility.KnownRepository("fixture", str(fixture.root)))):
                page = memory_feed.reading()
            row, = page["waiters"]
            self.assertEqual(row["pid"], request.owner.pid)
            self.assertEqual(row.get("runs_on"), "natedev or Mac")
            self.assertEqual(row.get("targets"), ["natedev", "mac"])

    def verify_call_page(self) -> memory_feed.MemoryPage:
        """Read the verify facts from admissions through the private service socket."""
        with AdmissionFixture() as fixture:
            facts = admission_records.VerifyCall("final", "verify final sample --local 'creates a Linux install'", 1, 2)
            run = fixture.run_owner()
            first = fixture.request(step="nextest", need=admission_records.GIB, run=run)
            first = replace(first, sidecar=str(fixture.scope(first.owner)),
                            identity=replace(first.identity, verify=replace(facts, position=2)))
            _ = only_admitted(fixture.server.handle_request(first))
            fixture.allow_next_start()
            fixture.free(0)
            direct = fixture.request(step="check", need=2 * admission_records.GIB)
            direct_peer = fixture.connect(direct)
            direct_reply, = direct_peer.replies()
            self.assertIsInstance(direct_reply, wire.Wait)
            fixture.clock.advance(1)
            second = fixture.request(step="check", need=4 * admission_records.GIB)
            environment = {"BUILDLOG_VERIFY_VERB": facts.verb, "BUILDLOG_VERIFY_CALL": facts.call,
                           "BUILDLOG_VERIFY_POSITION": "1", "BUILDLOG_VERIFY_TOTAL": "2",
                           "BUILD_QUEUE_LOCAL_REASON": "creates a Linux install"}
            with (mock.patch.dict(os.environ, environment),
                  mock.patch.object(admission_client, "CLOCK", fixture.clock),
                  mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                      second.profile, step_estimator.ReusableEstimate()))):
                second = admission_client.step_request(list(second.argv), second.cwd, second.owner,
                    second.since, second.sidecar, second.episode)
            self.assertEqual(second.identity.verify, facts)
            waiter = fixture.connect(second)
            reply, = waiter.replies()
            self.assertIsInstance(reply, wire.Wait)
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.inputs.running[0].verify, replace(facts, position=2))
            self.assertNotIn("verify", snapshot.inputs.ordered[0][1])
            self.assertEqual(snapshot.inputs.ordered[1][1].get("verify"), facts)
            self.assertEqual(snapshot.inputs.reservations[0].get("queue_waiter", {}).get("verify"),
                             replace(facts, position=2))
            state = admission_service.read_state(fixture.state_dir)
            self.assertEqual(state.reservations[first.episode].queue_waiter.get("verify"), replace(facts, position=2))
            reply, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(reply, wire.ViewReply)
            with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reply.reading)):
                return memory_feed.reading()

    def mac_verify_call_page(self) -> memory_feed.MemoryPage:
        from scripts.build_queue.test_placement import (
            PlacementFixture, assignment_for, placement_request, publish_candidate,
        )

        with PlacementFixture() as fixture:
            facts = admission_records.VerifyCall("test", "verify test sample --no-cache", 1, 1)
            request = placement_request(fixture)
            request = replace(request, identity=replace(request.identity, step="nextest", verify=facts))
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            reply, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(reply, wire.ViewReply)
            now = reply.reading.explanation.now
            build = queue_view.MacBuild(710001, "/Mac/hana", now - 10, "cargo nextest run", "hana",
                gate_label=queue_view.MacGateLabel.PLACEMENT,
                gate_placement=f"{assignment.episode}/{assignment.attempt}")
            other = replace(build, pid=710002, gate_placement=f"{assignment.episode}/other-attempt")
            mac = queue_view.MacBuildReading(now, (build, other))
            with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reply.reading)),
                  mock.patch.object(queue_view, "read_mac_builds", return_value=mac),
                  mock.patch.object(memory_feed, "mac_build_units", return_value={})):
                return memory_feed.reading()

    def test_placed_mac_build_shows_the_verify_call_without_process_evidence(self) -> None:
        page = self.mac_verify_call_page()
        placed, unrelated = [row for row in page["walk"] if row.get("kind") == "mac-build"]
        self.assertEqual(placed.get("verify"), {
            "step": "nextest", "verb": "test", "call": "verify test sample --no-cache", "position": 1, "total": 1})
        self.assertEqual(placed.get("about"), "Runs the selected tests")
        self.assertEqual(unrelated.get("about"), "Runs the selected tests")
        self.assertNotIn("verify", unrelated)
        self.assertEqual(unrelated.get("call"), "cargo nextest run")
        self.assertNotIn("call", placed)

    def test_verify_call_reaches_running_and_waiting_feed_rows(self) -> None:
        page = self.verify_call_page()
        running, = [row for row in page["walk"] if "verify" in row]
        direct, waiting = page["waiters"]
        running_call = running.get("verify")
        waiting_call = waiting.get("verify")
        assert running_call is not None and waiting_call is not None
        self.assertEqual((running_call["step"], running_call["position"]), ("nextest", 2))
        self.assertEqual((waiting_call["step"], waiting_call["position"]), ("check", 1))
        self.assertEqual(running_call["call"], "verify final sample --local 'creates a Linux install'")
        self.assertEqual(waiting_call["call"], running_call["call"])
        self.assertEqual((running_call["verb"], waiting_call["total"]), ("final", 2))
        self.assertNotIn("verify", direct)

    def test_the_page_shortens_calls_without_changing_the_record(self) -> None:
        cases = (
            ("verify test " + " ".join(f"--filter check_{index}" for index in range(14)),
             "verify test · 14 filters"),
            ("verify test --filter=first --filter second --no-cache", "verify test --no-cache · 2 filters"),
            ("verify check /work/project/ --manifest-path='/work/project/Cargo test.toml'",
             "verify check project '--manifest-path=Cargo test.toml'"),
            ("verify test --run-id fixture --commit f4ab123 --job 786a38ed-dffe-4fea-b6cf-814fa44aeee5",
             "verify test --run-id --commit --job"),
            ("verify final sample --local 'creates a Linux install'", "verify final sample --local 'creates a Linux install'"),
            ("verify test --filter first --filter second hana", "verify test hana · 2 filters"),
            ("verify test --filter=first --filter=second hana", "verify test hana · 2 filters"),
            ("verify test --no-cache --no-cache hana", "verify test hana · --no-cache ×2"),
            ("verify check --keep-going --keep-going hana", "verify check hana · --keep-going ×2"),
            ("verify test hana --mac screenshots --fetch target/shots",
             "verify test hana --mac screenshots --fetch target/shots"),
            ("verify test --features hana/dynamic_linking", "verify test --features hana/dynamic_linking"),
        )
        for original, expected in cases:
            with self.subTest(call=original):
                saved = admission_records.VerifyCall("test", original, 1, 1)
                verify = memory_feed.call_notes(saved, "nextest").get("verify")
                assert verify is not None
                self.assertEqual(verify["call"], expected)
                self.assertEqual(saved.call, original)

    def test_cargo_and_gate_commands_use_the_call_formatter(self) -> None:
        cases = (
            (("cargo", "test", "-p", "sample", "--release"), "cargo test -p sample --release"),
            (("cargo", "test", "-F", "hana/dynamic_linking", "--manifest-path=work/project/Cargo.toml"),
             "cargo test -F hana/dynamic_linking --manifest-path=work/project/Cargo.toml"),
            (("cargo", "check", "--manifest-path=~/work/project/Cargo.toml"),
             "cargo check --manifest-path=Cargo.toml"),
            (("cargo", "test", "-v", "-v"), "cargo test · -v ×2"),
            (("cargo", "test", "--", "--skip", "first", "--skip", "second", "hana"), "cargo test -- hana · 2 skips"),
            (("py", "gate.py", "--tests-only", "--module", "one", "--module=two"),
             "gate.py --tests-only · 2 modules"),
            (("py", "gate.py", "--checkout", "/work/project/", "--tests-only", "--module", "suite/module"),
             "gate.py --checkout project --tests-only --module suite/module"),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(memory_feed.build_command_call(arguments), expected)

    def test_queued_cargo_keeps_the_command_from_the_admission_child(self) -> None:
        base = with_wait_reason(reservation_reading(), admission_records.NeedsMemory())
        waiter, = base.view.waiting
        current: admission_records.RankedWaiter = {
            "pid": waiter.pid, "start_time": "waiter", "need": waiter.need, "since": waiter.since,
            "session_id": waiter.session_id, "session_name": waiter.session_name,
            "step": waiter.step, "folder": waiter.folder, "order": admission_records.NormalOrder(),
        }
        reading = replace(base, ordered=((Path("fixture.waiting"), current),))
        proc = self.root / "proc"
        write_process(proc, waiter.pid, start="waiter")
        child = waiter.pid + 1
        write_process(proc, child, parent=waiter.pid)
        _ = (proc / str(child) / "cmdline").write_text(
            "\0".join(("python3", "queue_place.py", "admit", "--pid", str(waiter.pid), "--",
                        "cargo", "nextest", "run", "-p", "hana")) + "\0")
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            waiting, = memory_feed.reading()["waiters"]
        self.assertEqual(waiting.get("call"), "cargo nextest run -p hana")
        self.assertNotIn("verify", waiting)


    def test_queued_tooltips_use_the_verify_route_or_the_direct_command_reason(self) -> None:
        page = self.macroute_page()
        self.assertEqual([row.get("mac_text") for row in page["waiters"]], [
            "Mac: busy with nextest hana_catalyst",
            "",
            ""])
        self.assertEqual([row.get("runs_on") for row in page["waiters"]],
                         ["natedev or Mac", "natedev only",
                          "natedev only"])

    def test_head_room_rows_are_inside_the_queue_cap_and_add_up(self) -> None:
        gib = admission_records.GIB
        self.ram_fixture()
        tools = memory_feed.BuildToolRam(rust_analyzer=gib, sccache=0, other=0)
        room = admission_records.MemoryRoom(28 * gib, 3 * gib, 2 * gib, 8 * gib, 7 * gib, 20 * gib, (),
                                            ci_use=3 * gib, agent_swap_demand=10 * gib)
        ram = memory_feed.ram_blocks(room, tools, 100.0, 1)
        for ahead in (0, 2):
            with self.subTest(ahead=ahead):
                check = admission_records.RoomCheck(16 * gib, (7 - ahead) * gib, False, basis=admission_records.DoesNotFit())
                shown = memory_feed.head_room(check, room, ram)
                rows = [(row["label"], row["gib"]) for row in shown["rows"]]
                self.assertEqual(rows, [("Queue cap", 21.0), ("− Builds", 6.0), ("− Build reserved", 8.0),
                                        *([("− Earlier waiters", float(ahead))] if ahead else []), ("= Head room" if ahead else "= Free", 7.0 - ahead)])
                self.assertEqual(max(0, rows[0][1] - sum(amount for _, amount in rows[1:-1])), rows[-1][1])
                self.assertEqual(rows[0][1], memory_feed.gib(ram["cap_bytes"]))
                self.assertEqual(shown["unavailable"], "")
                self.assertNotIn("note", shown)
        for available, build in ((16, 3), (8, 0), (2, 0)):
            with self.subTest(available=available):
                short = replace(room, available=available * gib, signed_room=(available - 21) * gib)
                check = admission_records.RoomCheck(16 * gib, short.signed_room, False,
                                                   basis=admission_records.DoesNotFit())
                blocks = memory_feed.ram_blocks(short, tools, 100.0, 1)
                shown = memory_feed.head_room(check, short, blocks)
                self.assertEqual([(row["label"], row["gib"]) for row in shown["rows"]],
                                 [("Queue cap", float(6 + build)), ("− Builds", 6.0), ("− Build reserved", float(build)),
                                  ("= Free", 0.0)])
                self.assertEqual(shown["rows"][0]["gib"] - sum(row["gib"] for row in shown["rows"][1:-1]),
                                 shown["rows"][-1]["gib"])
                self.assertEqual(shown["rows"][2]["gib"], memory_feed.gib(blocks["build_covered_bytes"]))
                self.assertNotIn("note", shown)
        swap_short = replace(room, available=4 * gib, agent_swap_demand=6 * gib, signed_room=-11 * gib)
        check = admission_records.RoomCheck(16 * gib, -11 * gib, False, basis=admission_records.DoesNotFit())
        swap_ram = memory_feed.ram_blocks(swap_short, tools, 100.0, 1)
        self.assertEqual(swap_ram["swap_covered_bytes"], gib)
        self.assertEqual(swap_ram["swap_target_bytes"], 6 * gib)
        shown = memory_feed.head_room(check, swap_short, swap_ram)
        self.assertEqual([(row["label"], row["gib"]) for row in shown["rows"]],
                         [("Queue cap", 6.0), ("− Builds", 6.0), ("− Build reserved", 0.0), ("= Free", 0.0)])
        self.assertNotIn("note", shown)
        no_swap = replace(room, agent_swap_demand=0, cap=27 * gib, signed_room=17 * gib)
        check = admission_records.RoomCheck(16 * gib, 17 * gib, False, basis=admission_records.DoesNotFit())
        no_swap_ram = memory_feed.ram_blocks(no_swap, tools, 100.0, 1)
        self.assertEqual(no_swap_ram["swap_target_bytes"], 0)
        self.assertEqual(no_swap_ram["swap_covered_bytes"], 0)
        shown = memory_feed.head_room(check, no_swap, no_swap_ram)
        self.assertEqual([(row["label"], row["gib"]) for row in shown["rows"]],
                         [("Queue cap", 31.0), ("− Builds", 6.0), ("− Build reserved", 8.0), ("= Free", 17.0)])
        self.assertNotIn("note", shown)
        run = admission_records.RunMemory("organon", gib, admission_records.RunUseUnavailable("no cgroup"))
        unavailable = memory_feed.head_room(check, replace(room, cap=admission_records.CapUnavailable((run,))), ram)
        self.assertEqual((unavailable["unavailable"], unavailable["rows"]),
                         ("memory use is unavailable for organon: no cgroup", []))
        unread = memory_feed.head_room(check, admission_records.ReadingUnavailable("meminfo is missing"), ram)
        self.assertEqual((unread["unavailable"], unread["rows"]), ("meminfo is missing", []))

    def test_only_a_head_that_memory_holds_shows_its_room_rows(self) -> None:
        gib = admission_records.GIB
        reading = with_wait_reason(reservation_reading(), admission_records.NeedsMemory())
        room = admission_records.RoomCheck(20 * gib, 15 * gib, False, basis=admission_records.DoesNotFit())
        expected_rows = [[("Queue cap", 28.0), ("− Builds", 1.0),
                          ("− Build reserved", 9.0), ("− Earlier waiters", 3.0), ("= Head room", 15.0)]]
        states: tuple[tuple[Literal["admit", "hold"], admission_records.HeadCause], ...] = (
            ("admit", admission_records.NeedsMemory()), ("hold", admission_records.NeedsMemory()),
            ("hold", admission_records.StartSpacingWait(110)), ("hold", admission_records.GpuSlotWait(1, 1)))
        for state, cause in states:
            with self.subTest(state=state):
                head = admission_records.HeadExplanation(reading.view.waiting[0], admission_records.NeedSourceNotRecorded(),
                    admission_records.AdmissionDecision(state, 0, 0, 0, 0), room,
                    admission_records.FreeCheck(30 * gib, 20 * gib, 0, 0, 0, True), False,
                    cause=cause)
                current = replace(reading, explanation=replace(reading.explanation, head=head))
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(current)):
                    page = memory_feed.reading()
                shown = [[(row["label"], row["gib"]) for row in waiter["head_room"]["rows"]]
                         for waiter in page["waiters"] if "head_room" in waiter]
                self.assertEqual(shown, expected_rows if state == "hold" else [])
                self.assertNotIn("admission_check", page)
                first = page["waiters"][0]
                self.assertEqual((first.get("place"), first.get("why")),
                                 (1, "memory"))
                self.assertEqual([row.get("first_in_line", False) for row in page["waiters"]],
                                 [index == 0 for index in range(len(page["waiters"]))])
        # The step the queue admits next shows its room, even when another step is ahead of it in the list.
        last = reading.view.waiting[-1]
        head = admission_records.HeadExplanation(last, admission_records.NeedSourceNotRecorded(),
            admission_records.AdmissionDecision("hold", 0, 0, 0, 0), room,
            admission_records.FreeCheck(30 * gib, 20 * gib, 0, 0, 0, True), False, cause=admission_records.NeedsMemory())
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(replace(reading, explanation=replace(reading.explanation, head=head)))):
            page = memory_feed.reading()
        last_pid = last.run.pid if isinstance(last.run, LiveBuildRun) else last.pid
        self.assertEqual([row.get("first_in_line", False) for row in page["waiters"]],
                         [row["pid"] == last_pid for row in page["waiters"]])

    def test_each_wait_reason_says_why_in_the_same_form(self) -> None:
        gib = admission_records.GIB
        decision = admission_records.AdmissionDecision("hold", 0, 0, 0, 0)
        cases: tuple[tuple[admission_records.WaitReason, str], ...] = (
            (admission_records.NeedsMemory(need=4 * gib, free=gib), "memory"),
            (admission_records.BehindHead("first", 0, "fixture"), "its turn"),
            (admission_records.TokenWaitingWaiter(0), "Cargo token"),
            (admission_records.StartSpacingWait(0), "its turn"),
            (admission_records.MemoryPressure(1.0), "memory pressure"),
            (admission_records.GpuSlotWait(1, 1), "GPU slot"),
            (admission_records.EstimateExpired(0), "memory"),
            (admission_records.NatedevHeld("research", "a disk walk"), "build hold"),
            (admission_records.ReadingUnavailable("meminfo is missing"), "memory"),
            (admission_records.PressureUnavailable("PSI is missing"), "memory"),
            (admission_records.ReadyForAdmission(), "its turn"),
            (admission_records.HeadAdmissionStatus(decision, 0, 0, cause=admission_records.ReadyForAdmission()), "its turn"),
            (admission_records.HeadAdmissionStatus(decision, 0, 0, cause=admission_records.MemoryPressure(1.0)), "memory pressure"))
        for reason, expected in cases:
            with self.subTest(reason=type(reason).__name__):
                self.assertEqual(memory_feed.wait_why(reason), expected)

    def test_builds_page_shows_the_repository_run_limit(self) -> None:
        wait = admission_records.RunLimitWait(2, 2, "claude_commands", "natedev")
        reading = with_wait_reason(reservation_reading(), wait)
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            page = memory_feed.reading()
        row = cast(memory_feed.MemoryWaitRow, page["waiters"][0])
        self.assertEqual(row["why"], "2 of 2 claude_commands runs on natedev")

    def test_mac_idle_alert_reaches_the_feed_and_clears_with_the_watch_state(self) -> None:
        _ = write_config_fixture(self.root / "mac_test.conf", "stale_seconds=300\n")
        _ = write_idle_config(self.root)
        reading = captured_reading()
        now = reading.explanation.now
        cases = (
            ("idle", 0, "MAC BLOCKED by research since 07:23 (P15 F11 L0 run): 7 steps wait for the Mac, "
                + "idle 10 min; /build:mac unblock clears it."),
            ("busy", 0, ""),
            ("unreachable", 0, "MAC UNREACHABLE until 07:38: 7 steps wait for the Mac."),
            ("unreadable", 0, "MAC IDLE WATCH: Mac state unreadable: P15 F11 L0 run."),
            ("idle", 301, "MAC IDLE WATCH: no reading since 07:28; check buildlog-sample.timer."),
            ("absent", 0, ""),
        )
        for state, written_age, expected in cases:
            with self.subTest(state=state, written_age=written_age):
                path = write_idle_alert(self.root / "mac-idle", now, written_age=written_age)
                if state == "absent":
                    path.unlink()
                else:
                    saved = cast(dict[str, object], json.loads(path.read_text()))
                    saved.update(state=state, until=now + 300)
                    _ = path.write_text(json.dumps(saved))
                live = write_live_mac_state(self.root / "mac-state", block=state == "idle",
                                            unreachable_until=now + 300 if state == "unreachable" else 0)
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)), \
                     mock.patch.object(time, "time", return_value=now), \
                     mock.patch.dict(os.environ, {"MAC_TEST_STATE_DIR": str(live)}):
                    page = memory_feed.reading()
                payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
                self.assertEqual(payload["mac_idle_alert"], expected)

    def test_mac_block_shows_its_card_and_waiter_line_and_omits_the_duplicate_alert(self) -> None:
        from scripts.build_targets import build_targets, targets
        from scripts.build_targets.target_config import LocalTarget, SshTarget
        from scripts.mac_test import eligibility

        _ = write_idle_config(self.root)
        reading = queued_reading(captured_reading(), ())
        reading = replace(reading, view=replace(reading.view, waiting=tuple(
            replace(row, route=target_config.ANY_ELIGIBLE_TARGET) for row in reading.view.waiting)))
        now = reading.explanation.now
        since = datetime.fromtimestamp(now - 600, UTC)
        block = targets.Held("research", "P15 F11 L0 run", since, targets.UntilReleased(), targets.HolderSession("mac-owner"))
        snapshots = (targets.TargetSnapshot(SshTarget("mac", "macos", self.root / "mac.conf"), (block,), (), eligibility.NO_BACKOFF),
                     targets.TargetSnapshot(LocalTarget("natedev", "linux"), (targets.Free(),), (), eligibility.NO_BACKOFF))
        labels = {"mac-owner": unit_labels.UnitLabels("director", "hana", "research")}
        background = limits_view.Background(frozenset({"https://example.invalid/team/hana"}),
                                            limits_view.Metric("money", None, None, "USD"))
        mac = limits_view.MacReading(limits={"hana": 1}, claims_readable=True)
        for admission_error, expected in (("", ""), ("fixture down", "MAC IDLE WATCH: admission query failed: fixture down.")):
            with self.subTest(admission_error=admission_error):
                path = write_idle_alert(self.root / "mac-idle", now)
                saved = cast(dict[str, object], json.loads(path.read_text()))
                saved.update(admission_error=admission_error)
                _ = path.write_text(json.dumps(saved))
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)), \
                     mock.patch.object(time, "time", return_value=now), \
                     mock.patch.object(target_availability, "read_snapshots", return_value=snapshots), \
                     mock.patch.object(unit_labels, "unit_labels_for", return_value=labels), \
                     mock.patch.object(build_targets, "step_policy", return_value=eligibility.KnownRepository("hana")):
                    page = memory_feed.reading(limit_source=lambda _: background, mac_source=lambda _: mac)
                self.assertEqual(page["mac_idle_alert"], expected)
                limits = page.get("build_limits")
                assert limits is not None
                self.assertEqual(limits.mac.state, "Held")
                self.assertEqual([(row.name, row.mac.used, row.mac.limit) for row in limits.repositories],
                                 [("hana", 0, 1)])
                self.assertEqual(page["targets"], [{"target": "mac", "state": "Blocked", "reason": "P15 F11 L0 run",
                    "holder": "research", "unit": "research", "showrunner": "hana", "since": "since " + since.strftime("%H:%M UTC"),
                    "until": "until released"}])
                self.assertEqual({waiter.get("runs_on") for waiter in page["waiters"]}, {"natedev or Mac"})
                self.assertEqual({tuple(waiter.get("targets", ())) for waiter in page["waiters"]}, {("natedev", "mac")})

    def test_feed_carries_saved_machine_facts_into_build_limits(self) -> None:
        reading = queued_reading(captured_reading(), ())
        now = reading.explanation.now
        background = limits_view.Background(frozenset(), limits_view.Metric("money", None, None, "USD"))
        saved: machine_facts.SavedFacts = {"version": machine_facts.VERSION, "written": now, "machines": {
            "natedev": machine_facts.facts("read", load=8.04, cpus=32),
            "mac": machine_facts.facts("read", link_ms=3.2, load=1.5, cpus=12, battery={"percent": 85, "mains": True}),
            "aws": machine_facts.facts("no_box")}}

        def limits() -> limits_view.BuildLimits:
            with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)), \
                 mock.patch.object(time, "time", return_value=now):
                page = memory_feed.reading(limit_source=lambda _: background, mac_source=lambda _: None)
            shown = page.get("build_limits")
            assert shown is not None
            return shown

        self.assertEqual(limits().mac.facts, "facts not sampled")
        _ = (self.root / "machine-facts.json").write_text(json.dumps(saved))
        shown = limits()
        self.assertEqual((shown.natedev.facts, shown.mac.facts, shown.aws.facts),
                         ("load 8.0/32", "link 3 ms · load 1.5/12 · battery 85% · not in use", "no box running"))

    def test_cloud_rows_show_stage_and_location(self) -> None:
        reading = cloud_reading(reservation_reading())
        first = reading.cloud[0]
        assert isinstance(first.progress, cloud_progress.CloudProgress)
        for stage, region, zone, location in (
            ("tools ready", "", "", ""),
            ("tests start", "eu-south-2", "eu-south-2a", "eu-south-2, zone a"),
            ("launch", "", "", ""),
            ("uploading", "eu-south-2", "eu-south-2b", "eu-south-2, zone b"),
            ("recording", "eu-south-2", "us-east-1b", "eu-south-2, zone us-east-1b"),
            ("", "", "eu-south-2a", "zone eu-south-2a"),
            ("", "eu-south-2", "", "eu-south-2"),
            ("", "", "", ""),
        ):
            with self.subTest(stage=stage, region=region, zone=zone):
                state = "uploading" if stage == "uploading" else "setup" if stage == "launch" else "running"
                current = replace(first, progress=replace(first.progress, state=state, stage=stage,
                                                          region=region, zone=zone))
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(
                        replace(reading, cloud=(current, *reading.cloud[1:])))),
                      mock.patch.object(queue_view, "cloud_push_interval", return_value=queue_view.CloudPushInterval(5))):
                    page = memory_feed.reading()
                payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
                rows = cast(list[dict[str, object]], payload["walk"])
                remote, = [row for row in rows if row.get("kind") == "cloud"]
                self.assertEqual(remote["stage"], stage or "testing")
                self.assertEqual(remote["location"], location)
                status = " · ".join(part for part in (state if state != "running" else "", stage, location,
                                                    "7 of 12 modules", "1 failed module") if part)
                self.assertEqual(remote["status"], status)
                self.assertEqual(remote["chart_status"], "1 failed module")
                self.assertEqual(remote.get("run_folder"), {"kind": "RunFolderNotReported"})
                self.assertTrue(all("run_folder" not in row for row in page["waiters"]))

    def cloud_owner_page(self, step: str, owner: unit_labels.UnitLabels | None = None, *,
                         identity: wire.Identity | None = None) -> memory_feed.MemoryPage:
        """Use the same identity for running and waiting cloud steps."""
        reading = cloud_reading(reservation_reading())
        rows = tuple(replace(row, identity=identity or replace(row.identity, step=step, unit="")) for row in reading.cloud)
        labels = {row.identity.session_id: owner for row in rows}
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(replace(reading, cloud=rows))):
            if owner is not None:
                with mock.patch.object(unit_labels, "unit_labels_for", return_value=labels):
                    return memory_feed.reading()
            return memory_feed.reading()

    def gate_job_page(self, step: str, argv: tuple[str, ...], *,
                      running_pid: int | admission_records.PidNotRecorded = 700000,
                      showrunner: bool = False, unit_known: bool = True,
                      job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED,
                      owner: unit_labels.UnitLabels | None = None) -> memory_feed.MemoryPage:
        """Render the same gate mode on both machines and in both tables."""
        config = Path(__file__).resolve().parents[2] / "config" / "build_steps.conf"
        _ = shutil.copyfile(config, self.root / "build_steps.conf")
        base = with_wait_reason(reservation_reading(), admission_records.StartSpacingWait(110))
        running = replace(base.view.running[0], step=step, pid=running_pid, job=job)
        waiting = replace(base.view.waiting[0], step=step, job=job)
        reading = cloud_reading(replace(base, view=replace(base.view, running=(running,), waiting=(waiting,)),
            explanation=replace(base.explanation, running=(replace(base.explanation.running[0], step=running),))))
        rows = tuple(replace(row, identity=replace(row.identity, step=step, job=job)) for row in reading.cloud)
        for pid, start in ((running.pid, ""), (waiting.pid, ""), *((row.owner.pid, row.owner.start_time) for row in rows)):
            if isinstance(pid, int):
                process = self.root / "proc" / str(pid)
                cmdline = process / "cmdline"
                if argv:
                    write_process(process.parent, pid, start=start or str(pid))
                    _ = cmdline.write_bytes(("\0".join(argv) + "\0").encode())
                else:
                    cmdline.unlink(missing_ok=True)
        owner = owner or (unit_labels.make_labels("sdlc", unit_labels.RunsProduction("sdlc"), unit_labels.NoDirector())
                 if showrunner else unit_labels.make_labels("impl", unit_labels.UnderShowrunner("sdlc", "sdlc", ""),
                                                            unit_labels.UnderDirector("devtools", "devtools", "")))
        labels = {session: owner for session in
                  (running.session_id, waiting.session_id, *(row.identity.session_id for row in rows))} if unit_known else {}
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(replace(reading, cloud=rows))),
              mock.patch.object(unit_labels, "unit_labels_for", return_value=labels)):
            return memory_feed.reading()

    def test_unreadable_gate_rows_strip_the_showrunner_prefix_on_local_and_cloud_rows(self) -> None:
        merge_staff = unit_labels.make_labels("sdlc-merge-gate", unit_labels.UnderShowrunner("sdlc", "sdlc", ""),
                                             unit_labels.DirectsUnit("sdlc-merge-gate"))
        page = self.gate_job_page("gate", (), owner=merge_staff)
        rows = [*(row for row in page["walk"] if row.get("queued") or row.get("kind") == "cloud"),
                *(row for row in page["waiters"] if row.get("kind") in (None, "cloud"))]
        self.assertEqual(len(rows), 4)
        for row in rows:
            with self.subTest(pid=row["pid"]):
                self.assertEqual(row["name"], "merge-gate")
                self.assertEqual(staff_names.parts_text(row.get("name_parts", [])), "merge-gate")

    def test_gate_modes_and_jobs_show_approved_names_on_local_and_cloud_rows(self) -> None:
        approved = {"integrate", "test all", "test named", "test affected", "test nightly", "fast track",
                    "promote", "CI", "toolchain trial"}
        modes = {mode.value for mode in SuiteMode}
        self.assertEqual(modes | {"merge-gate", "targeted-test"}, memory_feed.GATE_STEPS)
        for mode in sorted(modes):
            cases = (("--affected",), ("--affected", "--full"), ("--affected", "--module", "test_sample")) if mode == SuiteMode.TARGETED_TEST.value else (
                ("--unit", "devtools", "--merge-branch", "sdlc"),
                ("--tests-only", "--full"), ("--tests-only", "--module", "test_sample"),
                ("--nightly", "--merge-branch", "sdlc"), ("--promote-only",))
            for arguments in (*cases, memory_feed.CommandUnreadable("missing cmdline"), admission_records.PidNotRecorded()):
                with self.subTest(mode=mode, arguments=arguments):
                    argv = ("python3", "gate.py", *arguments) if isinstance(arguments, tuple) else ()
                    page = self.gate_job_page(mode, argv,
                        running_pid=arguments if isinstance(arguments, admission_records.PidNotRecorded) else 700000)
                    job = command_job(argv)
                    names = read_step_names()
                    named = names.shown(job.step) if isinstance(job, StepJob) else ""
                    if argv:
                        self.assertIn(named, approved)
                    local_run, = [row for row in page["walk"] if row.get("queued")]
                    cloud_run, = [row for row in page["walk"] if row.get("kind") == "cloud"]
                    local_wait, = [row for row in page["waiters"] if not row.get("kind")]
                    cloud_wait, = [row for row in page["waiters"] if row.get("kind") == "cloud"]
                    for row in (local_run, cloud_run, local_wait, cloud_wait):
                        self.assertEqual(row["name"], "devtools: " + named if named else "devtools")
                        self.assertEqual(row["title"]["step"], named or "devtools")
                        self.assertEqual(staff_names.parts_text(row.get("name_parts", [])), row["name"])
                        about = names.about(job) if isinstance(job, StepJob) else "Runs a merge or test job"
                        self.assertEqual(row.get("about", ""), about)
                        self.assertEqual(row.get("changes", ""), names.changes(job) if isinstance(job, StepJob) else "")

        copied = ("python3", "/repo/scripts/copies/copies.py", "run", "--repo=/repo", "--",
                  "python3", "/repo/scripts/production/merge_gate.py", "--tests-only", "--full")
        for step in ("gate", "merge-gate"):
            with self.subTest(step=step):
                page = self.gate_job_page(step, copied)
                local, = [row for row in page["walk"] if row.get("queued")]
                waiting, = [row for row in page["waiters"] if not row.get("kind")]
                for row in (local, waiting):
                    self.assertIn(row["title"]["step"], approved)
                    self.assertEqual(row["name"], "devtools: test all")
                    self.assertEqual(row.get("about"), "Runs every test module")

        page = self.gate_job_page("gate", (), job=StepJob("test-all"))
        local, = [row for row in page["walk"] if row.get("queued")]
        waiting, = [row for row in page["waiters"] if not row.get("kind")]
        for row in (local, waiting):
            self.assertEqual(row["name"], "devtools: test all")
            self.assertEqual(row.get("about"), "Runs every test module")

        base = captured_reading()
        inputs = base.explanation.decided_from
        policy = admission_policy_fixture()
        owner = staff.StaffRow(staff.StaffId("director"), "unit", "devtools", staff.StaffRoot(),
                               current_session=staff.StaffId("director-session"))
        for step, expected in (("test-all", "test all"), ("gate", "devtools")):
            with self.subTest(ended_step=step):
                run = LiveBuildRun(101, "101", inputs.now - 15)
                reservation = memory_tests.ProgressKeepAsideTests.reservation(step, need=admission_records.GIB)
                reservation.update(run_origin=admission_records.CallerRun(run), slot_staff=owner.id,
                                   session_name=owner.name)
                hold = gate.ended_step_slot(reservation, inputs.now, policy)
                assert isinstance(hold, admission_records.BetweenStepsSlot)
                reading = gate.queue_read_from(replace(inputs, slots=(hold,)), policy)
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
                      mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((owner,)))):
                    held, = memory_feed.reading()["walk"]
                self.assertEqual(held["name"], expected)
                self.assertEqual(held["unit"], "devtools")

        from scripts.aws_target.aws_trial import PAIRED_TRIAL_JOB

        # Without a staff unit the stored caller name is the owner, and the title shows the whole name.
        for unit_known, name, title in ((True, "devtools: test named", "test named"),
                                        (False, "{caller}: test named", "{caller}: test named")):
            with self.subTest(paired_trial_unit_known=unit_known):
                trial = self.gate_job_page(PAIRED_TRIAL_JOB.step, (), unit_known=unit_known, job=PAIRED_TRIAL_JOB)
                for rows, caller in ((trial["walk"], "route-one"), (trial["waiters"], "route-two")):
                    row, = [row for row in rows if row.get("kind") == "cloud"]
                    self.assertEqual(row["title"]["step"], title.format(caller=caller))
                    self.assertEqual(row["name"], name.format(caller=caller))
                    self.assertEqual(row.get("about"), "Runs 1 named test modules")
                    self.assertEqual(row.get("changes"), "no branch")

        integration = self.gate_job_page("gate", ("python3", "gate.py"), showrunner=True)
        for rows in (integration["walk"], integration["waiters"]):
            row, = [row for row in rows if row.get("kind") == "cloud"]
            self.assertEqual(row["name"], "sdlc: integrate")

    def test_cloud_full_tests_use_the_test_all_name_and_description(self) -> None:
        page = self.gate_job_page("gate", ("python3", "scripts/production/gate.py", "--tests-only", "--full"))
        row, = [row for row in page["walk"] if row.get("kind") == "cloud"]
        self.assertEqual(row["name"], "devtools: test all")
        self.assertEqual(row.get("about"), "Runs every test module")
        self.assertEqual(row.get("changes"), "no branch")
        self.assertEqual(row.get("call"), "gate.py --tests-only --full")

    def owner_machine_page(self, owner: unit_labels.UnitLabels, *, ci: bool = False) -> memory_feed.MemoryPage:
        """Use the same owner across the local, Mac, and cloud rows."""
        reading = cloud_reading(with_wait_reason(reservation_reading(), admission_records.ReadyForAdmission()))
        cloud = tuple(replace(row, identity=replace(row.identity, step="nextest",
                      unit=owner.unit if owner.unit != "—" else "")) for row in reading.cloud)
        reading = replace(reading, cloud=cloud)
        labels = {step.session_id: owner for step in (*reading.view.running, *reading.view.waiting)}
        labels.update({row.identity.session_id: owner for row in cloud})
        build = queue_view.MacBuild(90082, "/Users/nate/hana", reading.explanation.now - 45,
            "cargo nextest run --jobs=8", "hana", queue_view.MacTesting(90083), admission_records.GIB,
            origin=queue_view.MacBuildOrigin.CI if ci else queue_view.MacBuildOrigin.NON_CI,
            ci_runner="hana-mac-1" if ci else "", ci_job="test-hana" if ci else "")
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(unit_labels, "unit_labels_for", return_value=labels),
              mock.patch.object(queue_view, "read_mac_builds", return_value=queue_view.MacBuildReading(
                  reading.explanation.now, (build,))),
              mock.patch.object(memory_feed, "mac_build_units", return_value={build.pid: owner})):
            return memory_feed.reading()

    def test_step_owners_name_every_machine_and_both_tables(self) -> None:
        for owner, name in (
            (unit_labels.make_labels("impl", unit_labels.UnderShowrunner("hana", "hana", ""),
                unit_labels.UnderDirector("research", "research", "")), "research: nextest"),
            (unit_labels.make_labels("hana", unit_labels.RunsProduction("hana"), unit_labels.NoDirector()),
                "hana: nextest"),
        ):
            with self.subTest(role=owner.role):
                page = self.owner_machine_page(owner)
                self.assertEqual({row.get("machine") for row in page["walk"]}, {"fixture", "mac", "aws"})
                for row in (*page["walk"], *page["waiters"]):
                    self.assertEqual(row["name"], name)
                    parts = row.get("name_parts", [])
                    self.assertEqual(staff_names.parts_text(parts), name)
                    self.assertEqual([part["text"] for part in parts if part["kind"] == "staff"],
                                     ["research"] if owner.role is unit_labels.OwnerRole.UNIT else [])
                    self.assertEqual(row.get("showrunner"), "hana")
                    self.assertEqual(row["unit"], owner.unit)

    def test_decoded_work_rows_keep_caller_names_separate_from_display_labels(self) -> None:
        for stored, reservation, caller, held in (
            ("recorded caller", "reservation caller", "recorded caller", "recorded caller"),
            ("", "reservation caller", "staff member", "reservation caller"),
            (" \t", "reservation caller", "staff member", "reservation caller"),
            ("—", "reservation caller", "staff member", "reservation caller"),
            ("$CALLER", "reservation caller", "staff member", "reservation caller"),
            ("unregistered session", "reservation caller", "staff member", "reservation caller"),
            ("unregistered session", "unregistered session", "staff member", "staff member"),
        ):
            with self.subTest(stored=stored, reservation=reservation):
                page = self.caller_selection_page(stored_caller=stored, reservation_caller=reservation)
                running, waiting = page["walk"], page["waiters"]
                self.assertEqual([row["pid"] for row in running], [700001, 50, 301, 302, 304])
                self.assertEqual([row["pid"] for row in waiting], [200, 700002, 303])
                self.assertEqual(len({row.get("key") for row in (*running, *waiting)}), 8)
                self.assertEqual(page["reserved"], 11.0)
                self.assertEqual((running[1]["name"], running[1].get("ci_label")),
                                 ("hana CI: nextest", "hana CI"))
                self.assertEqual((running[2].get("caller"), running[2].get("owner_text")), (held, held))
                for row in (running[0], *waiting):
                    with self.subTest(pid=row["pid"]):
                        self.assertEqual((row.get("caller"), row.get("owner_text")), (caller, caller))
                        self.assertEqual((row["unit"], row.get("showrunner")), ("fixture unit", "fixture showrunner"))
                self.assertEqual(waiting[0]["name"], "fixture unit")
                self.assertEqual(waiting[0]["title"]["step"], "fixture unit")
                for cloud_row in (running[0], *waiting[1:]):
                    self.assertEqual(cloud_row["name"], "fixture unit: nextest")
                    self.assertEqual(cloud_row["title"], {"unit": "fixture unit", "step": "nextest"})

    def caller_selection_page(self, *, stored_caller: str | None = None,
                              reservation_caller: str = "") -> memory_feed.MemoryPage:
        """Decode work rows with both usable and unusable names."""
        initial = with_wait_reason(reservation_reading(), admission_records.ReadyForAdmission())
        inputs = initial.explanation.decided_from
        now = inputs.now
        worker = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "held caller", staff.StaffRoot(),
                                current_session=staff.StaffId("new-worker-session"))
        absent = admission_records.EndedStepFactNotRecorded()
        facts = admission_records.EndedStepFacts("gate", admission_records.PackageNotRecorded(),
                                                  absent, absent, worker.id, caller_name="$CALLER")
        retained = admission_records.BetweenStepsSlot(LiveBuildRun(301, "301", now - 5), "retained",
                                                      admission_records.GIB, now + 10, facts)
        continued = replace(retained, run=LiveBuildRun(302, "302", now - 5), reservation="continued",
                            identity=replace(facts, staff_id=admission_records.NoEndedStepStaff()))
        ci = replace(inputs.running[0], ci_runner="hana-linux-2", session_name="—")
        local = replace(initial.view.waiting[0], session_name="recorded local", step="gate")
        members = staff.StaffSnapshot((worker,))
        reservations = inputs.reservations
        if stored_caller is not None:
            runner = staff.StaffRow(staff.StaffId("runner"), "showrunner", "fixture showrunner", staff.StaffRoot(),
                                    current_session=staff.StaffId("runner-session"))
            director = staff.StaffRow(staff.StaffId("director"), "unit", "fixture unit", staff.ReportsTo(runner.id),
                                      current_session=staff.StaffId("director-session"))
            worker = replace(worker, name="staff member", parent=staff.ReportsTo(director.id))
            members = staff.StaffSnapshot((runner, director, worker))
            local = replace(local, session_id=worker.current_session, session_name=stored_caller)
            retained = replace(retained, reservation="step", identity=replace(facts, caller_name=stored_caller))
            reservations = tuple({**row, "session_name": reservation_caller} for row in inputs.reservations)
        reading = gate.queue_read_from(replace(inputs, running=(ci,), reservations=reservations,
            slots=(retained, admission_records.ContinuationSlot(continued, "next"))), admission_policy_fixture())
        cloud = cloud_reading(replace(reading, view=replace(reading.view, waiting=(local,))))
        cloud = replace(cloud, cloud=tuple(replace(row, identity=replace(
            row.identity, name="recorded cloud", step="gate")) for row in cloud.cloud))
        running_cloud, waiting_cloud = cloud.cloud
        extra_wait = replace(waiting_cloud, owner=wire.Owner(303, "303"), since=now - 30,
                             identity=wire.Identity("slot-session", "recorded slot", "gate", "/private/widget"))
        if stored_caller is not None:
            cloud = replace(cloud, cloud=tuple(replace(row, identity=replace(
                row.identity, session_id=worker.current_session, name=stored_caller, step="nextest")) for row in cloud.cloud))
            extra_wait = replace(extra_wait, identity=replace(
                extra_wait.identity, session_id=worker.current_session, name=stored_caller, step="nextest"))
            running_cloud, waiting_cloud = cloud.cloud
        message = wire.encode_reply(wire.ViewReply(replace(cloud, cloud=(running_cloud, waiting_cloud, extra_wait))))
        mac = queue_view.MacBuild(304, "/private/hana", now - 45, "cargo nextest run --jobs=8", "hana",
                                   queue_view.MacTesting(305), admission_records.GIB)

        def view(_folder: Path) -> queue_snapshot.PublishedView:
            reply = wire.parse_reply(message)
            assert isinstance(reply, wire.ViewReply) and isinstance(reply.reading, wire.CloudQueueRead)
            return fixture_published_view(reply.reading)

        unknown = unit_labels.UnitLabels("$OWNER")
        labels = {session: unknown for session in (local.session_id, *(row.identity.session_id for row in cloud.cloud))}
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", side_effect=view),
              mock.patch.object(unit_labels, "unit_labels_for", return_value=labels),
              mock.patch.object(staff, "snapshot", return_value=members),
              mock.patch.object(queue_view, "read_mac_builds", return_value=queue_view.MacBuildReading(now, (mac,))),
              mock.patch.object(memory_feed, "mac_build_units", return_value={mac.pid: unknown})):
            return memory_feed.reading()

    def test_mac_ci_uses_the_configured_label_without_a_staff_owner(self) -> None:
        _ = (self.root / "build_admission.conf").write_text("ci_label=hana CI · {runner} · {job}\n")
        page = self.owner_machine_page(unit_labels.UnitLabels("unknown"), ci=True)
        mac, = [row for row in page["walk"] if row.get("kind") == "mac-build"]
        self.assertEqual(mac["name"], "hana CI: nextest")
        self.assertEqual(mac.get("ci_label"), "hana CI · hana-mac-1 · test-hana")
        self.assertEqual(mac["title"]["step"], "hana CI · hana-mac-1 · test-hana")
        self.assertEqual(mac.get("showrunner"), "")
        self.assertEqual(mac["unit"], "")
        for runner, job, label in (("", "", "hana CI"), ("hana-mac-1", "", "hana CI"),
                                   ("", "test-hana", "hana CI")):
            with self.subTest(runner=runner, job=job):
                build = queue_view.MacBuild(90082, "/private/hana", 0, "cargo nextest run", "hana",
                    origin=queue_view.MacBuildOrigin.CI, ci_runner=runner, ci_job=job)
                row = memory_feed.mac_work_row(build, queue_view.MacBuildReading(1, (build,)),
                                              unit_labels.UnitLabels("unknown"), names=read_step_names())
                self.assertEqual(row["name"], "hana CI: nextest")
                self.assertEqual(row.get("ci_label"), label)

    def test_mac_rows_resolve_the_source_checkout_and_session(self) -> None:
        checkout = self.root / "workspace"
        runner = staff.StaffRow(staff.StaffId(fixture_runtime_id("runner")), "showrunner", "spacetime", staff.StaffRoot(),
            current_session=staff.StaffId(fixture_runtime_id("runner-session")))
        idle_checkout = self.root / "idle-checkout"
        source_mac_host = "fixture-mac"
        idle_runner = staff.StaffRow(staff.StaffId(fixture_runtime_id("idle-runner")), "showrunner", "idle",
            staff.StaffRoot(), current_session=staff.StaffId(""),
            worktree=staff.PresentWorktree(source_mac_host, idle_checkout))
        unit_checkout = self.root / "praxis-checkout"
        unit = staff.StaffRow(staff.StaffId(fixture_runtime_id("unit")), "unit", "praxis", staff.ReportsTo(runner.id),
            current_session=staff.StaffId(fixture_runtime_id("unit-session")),
            worktree=staff.PresentWorktree(FIXTURE_MACHINE, unit_checkout), unit_type=staff.UnitDetails("direct"))
        mirror = staff.StaffRow(staff.StaffId(fixture_runtime_id("mirror")), "showrunner", "mirror",
            staff.StaffRoot(), current_session=staff.StaffId(fixture_runtime_id("mirror-session")))
        members = staff.StaffSnapshot((runner, idle_runner, unit, mirror))
        reading = captured_reading()
        now = reading.explanation.now
        build = queue_view.MacBuild(7801, "/Users/nate/mirror/hana", now - 1, "cargo clippy", "hana",
                                   gate_label=queue_view.MacGateLabel.PLACEMENT)
        call_id = "v20261123T211623-1723351-13033"
        idle_call_id = "v20261123T211624-1723351-13034"
        recent_call_id = "v20261123T211625-1723351-13035"
        index_file = index.index_path()
        index_file.parent.mkdir()
        ledger_source = f"{source_mac_host}/2026-11.jsonl"
        ledger_path = store.root() / ledger_source
        with closing(sqlite3.connect(index_file)) as database, database:
            index.create_schema(database)
            index.add_call(database, {"id": call_id, "tool": "verify.sh", "caller": "verify",
                "worktree": str(self.root / "moved-worktree"), "session": unit.current_session}, "verify-fixture.jsonl")
            index.add_call(database, {"id": idle_call_id, "tool": "verify.sh", "caller": "verify",
                "cwd": str(idle_checkout), "session": "", "host": source_mac_host}, "verify-fixture.jsonl")
            index.add_step(database, {"id": "mac-step", "call_id": call_id, "caller": "verify-mac",
                "worktree": build.folder, "step": "clippy", "status": 0}, "mac-fixture.jsonl")
        store.append_line(ledger_path, {
            "kind": "call", "id": recent_call_id, "tool": "verify.sh", "host": source_mac_host,
            "cwd": str(idle_checkout), "worktree": "", "session": "", "outcome": "ran", "status": 0,
            "started_at": "2026-11-23T21:16:25Z", "ended_at": "2026-11-23T21:17:25Z",
        })

        cases = (
            ("unit", str(unit_checkout), "", "", True, "praxis", "spacetime", "praxis"),
            ("showrunner", str(checkout), runner.current_session, "", True, "spacetime", "spacetime", "—"),
            ("checkout only", str(self.root / "cargo-liner"), "", "", True, "cargo-liner", "", "—"),
            ("finished", str(self.root / "moved-worktree"), unit.current_session, call_id, False,
             "praxis", "spacetime", "praxis"),
            ("sessionless Verify on Mac", str(idle_checkout), "", idle_call_id, False, "idle", "idle", "—"),
            ("placement beats Verify", str(unit_checkout), "", idle_call_id, True, "praxis", "spacetime", "praxis"),
        )
        for case, source, session, verify_call, placed, owner, showrunner, shown_unit in cases:
            with self.subTest(source=case):
                placement = f"{verify_call}-step-1/attempt" if verify_call else "episode/attempt"
                mac = queue_view.mac_build_record(replace(build, gate_placement=placement, verify_call_id=verify_call))
                mac.update(launcher="terminal")
                _ = (self.root / "mac-builds.json").write_text(json.dumps({"read_at": now, "builds": [mac]}))
                step = admission_records.RunningStep(session, "verify-mac", "clippy", source, 0,
                    admission_records.RemoteMemoryUnavailable("remote memory is unavailable"),
                    pid=admission_records.PidNotRecorded(), mac_placement=placement)
                running = (step,) if placed else ()
                with (mock.patch.object(staff, "snapshot", return_value=members),
                      mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(
                          replace(reading, view=replace(reading.view, running=running))))):
                    page = memory_feed.reading()
                row, = [row for row in page["walk"] if row.get("kind") == "mac-build"]
                self.assertEqual((row["name"], row.get("showrunner"), row["unit"]),
                                 (f"{owner}: clippy", showrunner, shown_unit))
        with self.subTest(source="batched Verify calls"):
            builds = (replace(build, verify_call_id=call_id), replace(build, pid=7802, verify_call_id=idle_call_id))
            statements: list[str] = []
            with closing(index.read_only()) as database:
                database.set_trace_callback(statements.append)
                with (mock.patch.object(staff, "snapshot", return_value=members),
                      mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
                      mock.patch.object(queue_view, "read_mac_builds",
                                        return_value=queue_view.MacBuildReading(now, builds)),
                      mock.patch.object(index, "read_only", return_value=database)):
                    page = memory_feed.reading()
            queries = [statement for statement in statements if statement.lstrip().startswith("SELECT")]
            self.assertEqual(len(queries), 1, queries)
            names = {row["pid"]: row["name"] for row in page["walk"] if row.get("kind") == "mac-build"}
            self.assertEqual(names, {7801: "praxis: clippy", 7802: "idle: clippy"})
        with self.subTest(source="completed Verify before and after indexing"):
            recent = replace(build, session=mirror.current_session, verify_call_id=recent_call_id)
            pending_statements: list[str] = []
            with (mock.patch.object(staff, "snapshot", return_value=members),
                  mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
                  mock.patch.object(queue_view, "read_mac_builds", return_value=queue_view.MacBuildReading(now, (recent,)))):
                with closing(index.read_only()) as database, mock.patch.object(index, "read_only", return_value=database):
                    database.set_trace_callback(pending_statements.append)
                    pending, = [row for row in memory_feed.reading()["walk"] if row.get("kind") == "mac-build"]
                self.assertEqual((pending["name"], pending.get("row_owner"), pending.get("showrunner"), pending["unit"]),
                                 ("unregistered session: clippy", "unregistered session", "", "—"))
                pending_queries = [statement for statement in pending_statements if statement.lstrip().startswith("SELECT")]
                self.assertEqual(len(pending_queries), 1, pending_queries)
                with closing(sqlite3.connect(index_file)) as database, database:
                    _ = index.read_file(database, ledger_path, ledger_source, None, set())
                indexed, = [row for row in memory_feed.reading()["walk"] if row.get("kind") == "mac-build"]
            self.assertEqual((indexed["name"], indexed.get("row_owner"), indexed.get("showrunner"), indexed["unit"]),
                             ("idle: clippy", "idle", "idle", "—"))

    def test_unknown_owners_keep_their_current_names_on_every_machine(self) -> None:
        for unit, role in (("", unit_labels.OwnerRole.UNIT), ("—", unit_labels.OwnerRole.UNIT),
                           (unit_labels.OWNER_NOT_RECORDED, unit_labels.OwnerRole.UNIT),
                           (unit_labels.OWNER_NOT_RECORDED, unit_labels.OwnerRole.UNKNOWN)):
            with self.subTest(unit=unit, role=role):
                page = self.owner_machine_page(unit_labels.UnitLabels("unknown", "—", unit, role))
                self.assertEqual([row["name"] for row in page["walk"] if row.get("kind") == "mac-build"], ["unknown: nextest"])
                # A unit of "—" leaves the session name as the owner text, so each row names the session.
                session_owned = unit == "—"
                self.assertEqual([row["name"] for row in page["walk"] if row.get("kind") == "cloud"],
                                 ["unknown: nextest" if session_owned else "route-one: nextest"])
                local, = [row for row in page["walk"] if not row.get("kind")]
                self.assertEqual(local["name"], "unknown: nextest" if session_owned else "fixture: nextest")
                for row in (*page["walk"], *page["waiters"]):
                    self.assertEqual(staff_names.parts_text(row.get("name_parts", [])), row["name"])

    def test_queue_owner_from_staff_worktree_or_terminal(self) -> None:
        folder = self.root / "hana_catalyst"
        pid = 4068001
        base = with_wait_reason(captured_reading(), admission_records.ReadyForAdmission())
        waiting = replace(base.view.waiting[0], pid=pid, session_id="", session_name="",
                          step="build", folder=str(folder), project="hana")
        ordered: admission_records.RankedWaiter = {
            "pid": pid, "start_time": str(pid), "need": waiting.need, "since": waiting.since,
            "order": admission_records.NormalOrder(),
        }
        reading = replace(base, view=replace(base.view, waiting=(waiting,)), ordered=[(folder, ordered)])
        runner = staff.StaffRow(staff.StaffId("runner"), "showrunner", "hana", staff.StaffRoot(),
                               current_session=staff.StaffId("runner-session"))
        director = staff.StaffRow(staff.StaffId("director"), "unit", "startup", staff.ReportsTo(runner.id),
                                  staff.PresentWorktree("fixture", folder),
                                  current_session=staff.StaffId("director-session"))
        process = self.root / "proc" / str(pid)
        write_process(process.parent, pid)
        stat = process / "stat"
        # The feed reads no tty. Only the client stores a terminal name, so an empty stored name stays unknown.
        for members, tty, name, unit, showrunner, caller in (
            ((), 34816, "hana: build", "", "", "unregistered session"),
            ((), 0, "hana: build", "", "", "unregistered session"),
            ((runner, director), 34816, "startup: build", "startup", "hana", "startup"),
        ):
            with self.subTest(tty=tty, unit=unit):
                fields = ["S", "1", str(pid), str(pid), str(tty), *(["0"] * 14), str(pid)]
                _ = stat.write_text(f"{pid} (cargo) {' '.join(fields)}\n")
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
                      mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(members))):
                    row, = memory_feed.reading()["waiters"]
                self.assertEqual(row["name"], name)
                self.assertEqual(row["unit"], unit)
                self.assertEqual(row["showrunner"], showrunner)
                self.assertEqual(row.get("caller", ""), caller)

    def test_queue_name_keeps_the_unit_from_the_staff_record(self) -> None:
        runner = staff.StaffRow(staff.StaffId("runner"), "showrunner", "hana", staff.StaffRoot(),
                               current_session=staff.StaffId("runner-session"))
        organon = staff.StaffRow(staff.StaffId("organon"), "unit", "organon", staff.ReportsTo(runner.id),
                                current_session=staff.StaffId("organon-session"))
        record = staff.StaffSnapshot((runner, organon))
        base = with_wait_reason(captured_reading(), admission_records.ReadyForAdmission())
        waiting = replace(base.view.waiting[0], session_id=organon.current_session, unit="organon-unit")
        reading = replace(base, view=replace(base.view, waiting=(waiting,)))
        labels = unit_labels.recorded_owner_labels({organon.current_session}, record)
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(unit_labels, "unit_labels_for", return_value=labels),
              mock.patch.object(staff, "snapshot", return_value=record)):
            row, = memory_feed.reading()["waiters"]
        self.assertEqual(row["name"], "organon: nextest")
        self.assertEqual(row["unit"], "organon")

        class SessionLookupSnapshot(staff.StaffSnapshot):
            @override
            def by_current_session(self, session_id: str) -> staff.StaffRow | None:
                if session_id in self.rows_by_id:
                    raise AssertionError("Use the staff ID only for a staff lookup.")
                return super().by_current_session(session_id)

        with self.subTest(source="fixed staff ID with no current session"):
            inactive = replace(organon, current_session=staff.StaffId(""))
            membership = SessionLookupSnapshot((runner, inactive))
            display = memory_feed.FeedStaff(membership)
            self.assertEqual(display.fixed_owner_labels(inactive.id), labels[organon.current_session])

    def test_cloud_page_names_use_the_owner_in_running_and_waiting_tables(self) -> None:
        director = unit_labels.make_labels("panes", unit_labels.UnderShowrunner("sdlc", "sdlc", ""),
                                          unit_labels.DirectsUnit("panes"))
        runner = unit_labels.make_labels("builds", unit_labels.RunsProduction("builds"), unit_labels.NoDirector())
        merge_staff = unit_labels.make_labels("sdlc-merge-gate", unit_labels.UnderShowrunner("sdlc", "sdlc", ""),
                                             unit_labels.DirectsUnit("sdlc-merge-gate"))
        unit_worker = unit_labels.make_labels("impl", unit_labels.UnderShowrunner("sdlc", "sdlc", ""),
                                              unit_labels.UnderDirector("panes", "panes", ""))
        runner_worker = unit_labels.make_labels("land", unit_labels.UnderShowrunner("builds", "builds", ""),
                                                unit_labels.NoDirector())
        unknown = unit_labels.UnitLabels("unknown")
        for step, owner, expected in (
            ("test-affected", director, "panes"),
            ("build", director, "panes: build"),
            ("literal{step}", director, "panes: literal{step}"),
            ("gate", runner, "builds"),
            ("gate", director, "panes"),
            ("gate", merge_staff, "merge-gate"),
            ("gate", unit_worker, "panes"),
            ("gate", runner_worker, "builds"),
            ("test-affected", unknown, "unknown"),
            ("gate", unknown, "unknown"),
            ("test-affected", unit_labels.UnitLabels("worker", "sdlc", "panes"), "panes"),
            ("gate", unit_labels.UnitLabels("worker", "sdlc", "panes"), "panes"),
            ("test-affected", unit_labels.UnitLabels("panes", "sdlc", "—", unit_labels.OwnerRole.UNIT), "sdlc"),
            ("gate", unit_labels.UnitLabels("panes", "sdlc", "—", unit_labels.OwnerRole.UNIT), "sdlc"),
        ):
            with self.subTest(step=step, owner=owner):
                page = self.cloud_owner_page(step, owner)
                running = [row for row in page["walk"] if row.get("kind") == "cloud"]
                waiting = [row for row in page["waiters"] if row.get("kind") == "cloud"]
                self.assertEqual([row["name"] for row in running], [expected])
                self.assertEqual([row["name"] for row in waiting], [expected])

        step = "test-affected"
        for owner, expected in ((director, "panes"), (None, "unregistered session")):
            with self.subTest(owner=owner, expected=expected):
                page = self.cloud_owner_page(step, owner, identity=wire.Identity("old-cloud", "", step, ""))
                self.assertEqual([row["name"] for row in page["walk"] if row.get("kind") == "cloud"], [expected])
                self.assertEqual([row["name"] for row in page["waiters"] if row.get("kind") == "cloud"], [expected])

    def test_cloud_gate_names_the_owner_after_an_unknown_caller(self) -> None:
        with isolated_staff_file(self.root / "staff.json", {}):
            runner = fixture_member("showrunner", name="sdlc")
            unit = fixture_member("unit", name="panes", parent=runner.id)
            director = unit_labels.make_labels("panes", unit_labels.UnderShowrunner("sdlc", "sdlc", ""),
                                               unit_labels.DirectsUnit("panes"))
            identity = wire.Identity(unit.current_session, "unregistered session", "test-affected", "")
            page = self.cloud_owner_page(identity.step, director, identity=identity)
            for table, pid in ((page["walk"], 700001), (page["waiters"], 700002)):
                row, = [row for row in table if row.get("kind") == "cloud"]
                self.assertEqual((row["pid"], row["name"], row.get("caller")), (pid, "panes", "panes"))
                self.assertEqual(row.get("name_parts"), [{"kind": "staff", "text": "panes"}])

    def test_unknown_cloud_gate_rows_show_only_the_owner_for_any_step_name(self) -> None:
        config = self.root / "build_steps.conf"
        original = config.read_text()
        for unit, showrunner, shown, expected in (("panes", "sdlc", "selected tests", "panes"),
                                                  ("panes", "sdlc", "—", "panes"),
                                                  ("—", "sdlc", "selected tests", "sdlc"),
                                                  ("—", "—", "—", "unregistered session")):
            with self.subTest(unit=unit, showrunner=showrunner, shown=shown):
                _ = config.write_text(original.replace("names.test-affected=test affected",
                                                        f"names.test-affected={shown}"))
                owner = unit_labels.UnitLabels("", showrunner, unit, unit_labels.OwnerRole.UNIT)
                identity = wire.Identity("old-cloud", "", "test-affected", "")
                page = self.cloud_owner_page(identity.step, owner, identity=identity)
                for table in (page["walk"], page["waiters"]):
                    row, = [row for row in table if row.get("kind") == "cloud"]
                    self.assertEqual((row["name"], row["title"]["step"]), (expected, expected))

    def test_a_cloud_row_with_no_recorded_owner_keeps_the_owner_name(self) -> None:
        director = unit_labels.make_labels("panes", unit_labels.UnderShowrunner("sdlc", "sdlc", ""),
                                          unit_labels.DirectsUnit("panes"))
        page = self.cloud_owner_page("test-affected", director, identity=wire.Identity("legacy", "", "test-affected", ""))
        for table, pid in ((page["walk"], 700001), (page["waiters"], 700002)):
            row, = [row for row in table if row.get("kind") == "cloud"]
            self.assertEqual((row["pid"], row["name"], row["title"]["step"]), (pid, "panes", "panes"))

    def test_detached_and_systemd_cloud_rows_use_the_fixed_staff_owner(self) -> None:
        with isolated_staff_file(self.root / "staff.json", {}):
            runner = fixture_member("showrunner", name="hana")
            unit = fixture_member("unit", name="panes", parent=runner.id)
            worker = fixture_member("worker", name="impl", parent=unit.id)
            current = fixture_runtime_id("cloud-owner-after-restart")
            worker = staff.update_current_session(worker.current_session, staff.native_id(current))
            for source, member, role, worker_name in (
                ("detached", worker, unit_labels.Runner.WORKER, worker.name),
                ("systemd", unit, unit_labels.Runner.UNIT_DIRECTOR, ""),
            ):
                with (self.subTest(source=source), mock.patch.dict(os.environ, {
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_NAME": "",
                    "BUILDLOG_UNIT": "", "BUILDLOG_OWNER_STAFF_ID": str(member.id),
                }), mock.patch.object(unit_labels, "unit_labels_for", new=UNIT_LABELS_FOR),
                      mock.patch.object(unit_labels, "newest_sessions", return_value=[])):
                    identity = admission_client.step_identity("test-affected", "/private/tree")
                    self.assertEqual(identity.session_id, member.current_session)
                    page = self.cloud_owner_page(identity.step, identity=identity)
                    for table in (page["walk"], page["waiters"]):
                        row, = [cast(memory_feed.CloudWorkRow, row) for row in table if row.get("kind") == "cloud"]
                        self.assertEqual((row["name"], row["showrunner"], row["unit"], row.get("runner"), row.get("worker")),
                                         ("panes", "hana", "panes", role, worker_name))

    def test_cloud_page_shows_the_recorded_staff_role(self) -> None:
        for name, role, worker in (("impl", unit_labels.Runner.WORKER, "impl"),
                                   ("panes", unit_labels.Runner.UNIT_DIRECTOR, "")):
            with self.subTest(role=role):
                owner = unit_labels.UnitLabels(name, "sdlc", "panes", unit_labels.OwnerRole.UNIT, role)
                page = self.cloud_owner_page("test-affected", owner)
                rows = [cast(memory_feed.CloudWorkRow, row) for row in (*page["walk"], *page["waiters"])
                        if row.get("kind") == "cloud"]
                self.assertEqual([(row["showrunner"], row["unit"], row.get("runner"), row.get("worker")) for row in rows],
                                 [("sdlc", "panes", role, worker)] * 2)
                self.assertEqual([row.get("name_parts") for row in rows],
                                 [[{"kind": "staff", "text": "panes"}]] * 2)

    def test_queue_views_keep_recorded_names_when_registry_labels_are_empty(self) -> None:
        base = cloud_reading(with_wait_reason(captured_reading(), admission_records.NeedsMemory()))
        waiter = replace(base.view.waiting[0], step="gate", session_name="local caller name")
        reading = replace(base, view=replace(base.view, waiting=(waiter,)))
        labels = {session: unit_labels.UnitLabels("") for session in (
            waiter.session_id, *(row.identity.session_id for row in base.cloud))}
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
              mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())),
              mock.patch.object(unit_labels, "unit_labels_for", return_value=labels)):
            page = memory_feed.reading()
        rows = {row["pid"]: row for row in (*page["walk"], *page["waiters"])}
        for pid, name in ((waiter.pid, waiter.session_name),
                          *((row.owner.pid, row.identity.name) for row in base.cloud)):
            with self.subTest(pid=pid):
                self.assertEqual(rows[pid]["name"], name)
                self.assertEqual(rows[pid].get("caller") or rows[pid].get("showrunner"), name)
        self.assertEqual(queue_evidence.step_labels(waiter, labels).owner_text, waiter.session_name)

    def test_cloud_without_staff_does_not_publish_the_stored_unit(self) -> None:
        base = cloud_reading(captured_reading())
        owners = ("556caf6e-713d-47c0-a8fc-5dd5e8c26ce4", "$UNIT")
        reading = replace(base, cloud=tuple(replace(row, identity=replace(row.identity, unit=unit))
            for row, unit in zip(base.cloud, owners, strict=True)))
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            page = memory_feed.reading()
        self.assertEqual([row["unit"] for row in (*page["walk"], *page["waiters"]) if row.get("kind") == "cloud"],
                         ["", ""])

    def test_cloud_rows_share_the_feed_without_memory_reservations(self) -> None:
        reading = cloud_reading(reservation_reading())
        first = reading.cloud[0]
        assert isinstance(first.progress, cloud_progress.CloudProgress)
        states: tuple[tuple[cloud_progress.CloudState, float, str], ...] = (
            ("requesting", first.progress.updated_at, "2 min"),
            ("setup", first.progress.updated_at, "2 min"),
            ("running", first.progress.updated_at, "2 min"),
            ("uploading", first.progress.updated_at, "2 min"),
            ("running", 0.0, "1 min"),
            ("running", reading.explanation.now, ""),
            ("running", reading.explanation.now - 45, "45 s"),
            ("running", reading.explanation.now - 10800, "3 h"),
            ("running", reading.explanation.now - 172800, "2 d"),
        )
        cases: list[tuple[wire.CloudRow, str, str]] = [
            (replace(first, progress=replace(first.progress, state=state, updated_at=updated_at)), state, late_age)
            for state, updated_at, late_age in states]
        cases.extend((
            (replace(first, progress=cloud_progress.NoCloudProgress("missing")), "progress missing", ""),
            (replace(first, progress=cloud_progress.NoCloudProgress("unreadable")), "progress unreadable", ""),
            (replace(first, counter=progress_profile.NoCounter("no cloud counter")), "running", "2 min"),
        ))
        for cloud, state, late_age in cases:
            with self.subTest(state=state, progress=cloud.progress, counter=cloud.counter):
                current = replace(reading, cloud=(cloud, *reading.cloud[1:]))
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(current)),
                      mock.patch.object(queue_view, "cloud_push_interval", return_value=queue_view.CloudPushInterval(5)),
                      mock.patch.object(os, "cpu_count", return_value=17)):
                    page = memory_feed.reading()
                text = memory_feed.page_json(page)
                payload = cast(dict[str, object], json.loads(text))
                self.assertNotIn("cloud", payload)
                self.assertNotIn("cloud_heading", payload)
                self.assertEqual(page["cloud_slots_used"], 1)
                self.assertEqual(page["cloud_slots"], 1)
                runs = cast(list[dict[str, object]], payload["walk"])
                waits = cast(list[dict[str, object]], payload["waiters"])
                remote, = [row for row in runs if row.get("kind") == "cloud"]
                local, = [row for row in runs if row.get("kind") != "cloud"]
                waiter, = [row for row in waits if row.get("kind") == "cloud"]
                self.assertEqual(waiter["runs_on"], memory_feed.CLOUD_RUNS_ON)
                self.assertEqual(waiter.get("targets"), ["aws"])
                self.assertNotIn("AWS", "".join(str(row.get("runs_on", "")) for row in waits if row.get("kind") != "cloud"))
                self.assertEqual(remote["machine"], "aws")
                self.assertEqual(remote["vcpus"], 32 if isinstance(cloud.progress, cloud_progress.CloudProgress)
                                 else {"kind": "UnknownCpuCount"})
                self.assertEqual(remote["state"], state)
                self.assertEqual(remote["stage"], "testing" if isinstance(cloud.progress, cloud_progress.NoCloudProgress)
                                 or state == "running" else state)
                self.assertEqual(remote["pid"], 700001)
                self.assertEqual(remote["started_at"], first.started_at)
                if isinstance(cloud.counter, progress_profile.ProgressCounter):
                    self.assertEqual(remote["progress"], {"kind": "RowProgress", "phase": "testing",
                        "unit": "modules", "done": 7, "total": 12, "percent": 100 * 7 / 12})
                    self.assertIn("7 of 12 modules", str(remote["status"]))
                else:
                    self.assertEqual(remote["progress"], {"kind": "NoProgress", "reason": cloud.counter.reason})
                    self.assertNotIn("modules unknown", str(remote["status"]))
                if state == "running":
                    self.assertNotIn("running", str(remote["status"]))
                else:
                    self.assertTrue(str(remote["status"]).startswith(state), remote["status"])
                if isinstance(cloud.progress, cloud_progress.CloudProgress):
                    self.assertIn("1 failed module", str(remote["status"]))
                self.assertNotIn("no update for", str(remote["status"]))
                self.assertEqual(remote["chart_status"], "1 failed module" if isinstance(
                    cloud.progress, cloud_progress.CloudProgress) else "")
                self.assertEqual(remote["late_age"], late_age)
                self.assertEqual(remote["stale"], bool(late_age))
                self.assertEqual(local["machine"], "fixture")
                self.assertEqual(local["vcpus"], 17)
                self.assertGreater(float(str(local["reserved"])), 0)
                self.assertEqual(waiter["pid"], 700002)
                self.assertEqual(waiter["machine"], "")
                self.assertEqual(waiter["late_age"], "")
                self.assertFalse(waiter["stale"])
                self.assertEqual(waiter["since"], reading.cloud[1].since)
                self.assertEqual(waiter["progress"], {"kind": "NoProgress", "reason": "no current counter"})
                self.assertEqual(waiter["status"], waiter["reason"])
                self.assertIn("waits for a cloud slot: 1 of 1 in use", str(waiter["reason"]))
                for row in (remote, waiter):
                    for field in ("used", "counted", "reserved", "need"):
                        self.assertNotIn(field, row)

    def test_reserved_cloud_slot_counts_as_used_and_waits_in_the_queue(self) -> None:
        reading = cloud_reading(reservation_reading())
        first, waiting = reading.cloud
        reserved = replace(first, wait=wire.CloudSlotWait(1, 1, ReservedCloudSlot()),
            progress=cloud_progress.NoCloudProgress(), counter=progress_profile.NoCounter())
        for active, table in ((reserved, "waiters"), (first, "walk")):
            with self.subTest(table=table):
                reply = wire.ViewReply(replace(reading, cloud=(active, waiting)))
                decoded = wire.parse_reply(wire.encode_reply(reply))
                self.assertEqual(decoded, reply)
                assert isinstance(decoded, wire.ViewReply)
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(decoded.reading)),
                      mock.patch.object(memory_feed, "host_name", return_value="fixture")):
                    page = memory_feed.reading()
                self.assertEqual(page["cloud_slots_used"], 1)
                queued = [row for row in page["waiters"] if row.get("kind") == "cloud"]
                running = [row for row in page["walk"] if row.get("kind") == "cloud"]
                if table == "waiters":
                    before_launch = next(row for row in queued if row["pid"] == first.owner.pid)
                    self.assertEqual(before_launch["reason"], "waits for launch")
                    cloud_wait = cast(memory_feed.CloudWaitRow, before_launch)
                    self.assertEqual(cloud_wait["status"], "waits for launch")
                    self.assertEqual(cloud_wait["activity"], "waits for launch")
                    self.assertFalse(running)
                else:
                    self.assertEqual([row["pid"] for row in running], [first.owner.pid])
                    self.assertEqual([row["pid"] for row in queued], [waiting.owner.pid])

    def test_project_names_follow_the_queue_formatter_for_running_and_waiting_steps(self) -> None:
        # With no session name, the project is the owner; a folder never is.
        for raw_step, project, folder, expected in (
            ("nextest", "known-project", "/fixture", "known-project: nextest"),
            ("nextest", "", "/fixture", "unregistered session: nextest"),
            ("nextest", "", "/absolute/repository", "unregistered session: nextest"),
            ("integrate", "", "/fixture", "unregistered session: integrate"),
        ):
            with self.subTest(step=raw_step, project=project, folder=folder):
                reading = reservation_reading(project=project)
                step = replace(reading.view.running[0], step=raw_step, folder=folder, session_name="")
                explanation_step = replace(reading.explanation.running[0], step=step)
                reading = replace(reading, view=replace(reading.view, running=(step,)),
                    explanation=replace(reading.explanation, running=(explanation_step,)))
                reading = with_wait_reason(reading, admission_records.StartSpacingWait(110))
                waiter = replace(reading.view.waiting[0], step=raw_step, project=project, folder=folder,
                                 session_name="")
                reading = replace(reading, view=replace(reading.view, waiting=(waiter,)))
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
                      mock.patch.object(memory_feed, "read_step_names", wraps=read_step_names) as names_read):
                    page = memory_feed.reading()
                names_read.assert_called_once_with()
                self.assertEqual(page["walk"][0]["name"],
                    queue_lines.step_text(reading.view.running[0], read_step_names()))
                self.assertEqual(page["waiters"][0]["name"],
                    queue_lines.step_text(waiter, read_step_names()))
                self.assertEqual(page["walk"][0]["name"], expected)
                self.assertEqual(page["waiters"][0]["name"], expected)
                self.assertNotIn(folder, page["walk"][0]["name"])

    def test_leftover_ages_use_the_queue_formatter_for_rows_and_group_members(self) -> None:
        for small in (False, True):
            with self.subTest(grouped=small):
                reading = captured_reading(running=tuple(leftover_step(minutes, small=small)
                    for minutes in (45, 19 * 60, 2 * 24 * 60)))
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
                    page = memory_feed.reading()
                text = memory_feed.page_json(page)
                for minutes in (45, 19 * 60, 2 * 24 * 60):
                    self.assertIn(queue_view.leftover_age(minutes), text)

    def ram_fixture(self, *, meminfo: bool = True) -> None:
        """Write private slice, program and kernel readings: agents 5 GiB, desktop 1.5 GiB, Chrome 3 GiB, system 1 GiB.

        The kernel parts are zram 3 GiB, structures 2 GiB and counters 1 GiB; 1 GiB has no counter.
        """
        gib = admission_records.GIB
        user = self.root / "user"
        write_user_slices(user)
        for folder, anonymous in ((user / "app.slice" / "app-com.google.Chrome-41.scope", 2 * gib),
                                  (user / "app.slice" / "app-google\\x2dchrome@0f.service", gib),
                                  (self.root / "cgroup" / "system.slice", gib)):
            folder.mkdir(parents=True, exist_ok=True)
            _ = (folder / "memory.stat").write_text(f"anon {anonymous}\nshmem 0\n")
        fields = {"MemTotal": 64 * gib, "MemFree": 10 * gib, "MemAvailable": 20 * gib, "Buffers": gib,
                  "Cached": 12 * gib, "Shmem": gib, "KReclaimable": 2 * gib, "SUnreclaim": gib,
                  "KernelStack": gib // 4, "PageTables": gib * 3 // 4, "SecPageTables": 0, "Percpu": gib // 4,
                  "VmallocUsed": gib // 2, "Active(anon)": 10 * gib, "Inactive(anon)": 10 * gib, "Active(file)": 6 * gib,
                  "Inactive(file)": 19 * gib, "Unevictable": 0}
        zram = self.root / "sys" / "block" / "zram0"
        zram.mkdir(parents=True, exist_ok=True)
        _ = (zram / "mm_stat").write_text(f"{9 * gib} {2 * gib} {3 * gib} 0 {3 * gib} 0 0 0 0\n")
        _ = (self.root / "proc" / "vmstat").write_text(f"nr_free_pages 1\nnr_memmap_pages {gib // 2 // os.sysconf('SC_PAGE_SIZE')}\n")
        if not meminfo:
            fields = {"MemTotal": 64 * gib}
        _ = (self.root / "proc" / "meminfo").write_text("".join(f"{name}: {amount // 1024} kB\n" for name, amount in fields.items()))

    @staticmethod
    def fixture_room(available: int) -> admission_records.MemoryRoom:
        gib = admission_records.GIB
        signed = available - 4 * gib - 6 * gib - 2 * gib
        return admission_records.MemoryRoom(available, 4 * gib, gib, 6 * gib, signed, gib + 2 * gib + available - 6 * gib,
                                            (), total=64 * gib, ci_use=2 * gib, agent_swap_demand=2 * gib)

    def test_swap_share_tracks_the_configured_policy(self) -> None:
        for share in (20, 65):
            with self.subTest(share=share):
                _ = write_config_fixture(self.root / "build_admission.conf", f"agent_swap_share_percent={share}\n")
                policy = admission_records.read_memory_config(report=False).policy
                reading = captured_reading()
                reading = replace(reading, explanation=replace(reading.explanation, policy=policy))
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
                    page = memory_feed.reading()
                self.assertEqual(page["swap"]["reserve_share_percent"], share)

    def test_ram_blocks_cover_the_reserves_in_order_and_divide_the_total(self) -> None:
        gib = admission_records.GIB
        self.ram_fixture()
        tools = memory_feed.BuildToolRam(rust_analyzer=4 * gib, sccache=gib // 2, other=gib // 2)
        for available, covered, missing, room, shortfall in (
            (20 * gib, (4 * gib, 2 * gib, 6 * gib), (0, 0, 0), 8 * gib, 0),
            (5 * gib, (4 * gib, gib, 0), (0, gib, 6 * gib), 0, 7 * gib),
            (3 * gib, (3 * gib, 0, 0), (gib, 2 * gib, 6 * gib), 0, 9 * gib),
        ):
            with self.subTest(available=available):
                blocks = memory_feed.ram_blocks(self.fixture_room(available), tools, 100.0, 1)
                self.assertEqual((blocks["safety_covered_bytes"], blocks["swap_covered_bytes"],
                                  blocks["build_covered_bytes"]), covered)
                self.assertEqual((blocks["safety_missing_bytes"], blocks["swap_missing_bytes"],
                                  blocks["build_missing_bytes"]), missing)
                self.assertEqual((blocks["room_bytes"], blocks["shortfall_bytes"]), (room, shortfall))
                self.assertEqual(sum(missing), shortfall)
                self.assertEqual(blocks["build_tools_bytes"], 5 * gib)
                self.assertEqual(blocks["queued_builds_bytes"], gib)
                self.assertEqual(blocks["ci_bytes"], 2 * gib)
                self.assertEqual(blocks["cap_bytes"], gib + 5 * gib + 2 * gib + covered[2] + room)
                self.assertEqual((blocks["agents_bytes"], blocks["desktop_bytes"]), (5 * gib, gib * 3 // 2))
                self.assertEqual(blocks["desktop_protection_bytes"], 2 * gib)
                self.assertEqual(blocks["programs"], [{"id": "chrome", "name": "Chrome", "bytes": 3 * gib}])
                self.assertEqual((blocks["smaller_program_count"], blocks["smaller_programs_bytes"]), (1, gib))
                self.assertEqual(blocks["other_programs_bytes"], 4 * gib)
                self.assertEqual((blocks["kernel_bytes"], blocks["cache_bytes"]), (6 * gib, 4 * gib))
                self.assertEqual((blocks["compressed_swap_bytes"], blocks["kernel_structures_bytes"],
                                  blocks["kernel_counters_bytes"], blocks["kernel_no_counter_bytes"]),
                                 (3 * gib, 2 * gib, gib, gib))
                shown = (blocks["queued_builds_bytes"] + blocks["build_tools_bytes"] + blocks["ci_bytes"]
                         + blocks["build_covered_bytes"] + blocks["room_bytes"]
                         + blocks["safety_covered_bytes"] + gib * 3 // 2 + blocks["swap_covered_bytes"] + 5 * gib
                         + blocks["other_programs_bytes"] + 6 * gib + 4 * gib + blocks["unattributed_bytes"])
                self.assertEqual(shown, 64 * gib)
                self.assertEqual((blocks["missing"], blocks["agreement"]), ([], "agree"))

    def test_the_kernel_parts_read_zram_and_the_counters(self) -> None:
        gib = admission_records.GIB
        self.ram_fixture()
        values = memory_feed.meminfo_bytes()
        self.assertEqual(memory_feed.kernel_ram(values), memory_feed.KernelRam(3 * gib, 2 * gib, gib, gib))
        mm_stat = self.root / "sys" / "block" / "zram0" / "mm_stat"
        mm_stat.unlink()
        mm_stat.parent.rmdir()
        self.assertEqual(memory_feed.kernel_ram(values), memory_feed.KernelRam(0, 2 * gib, gib, 4 * gib))
        mm_stat.parent.mkdir()
        _ = mm_stat.write_text("9\n")
        kernel = memory_feed.kernel_ram(values)
        self.assertIsInstance(kernel.total, admission_records.ReadingUnavailable)
        self.assertEqual(kernel.no_counter, 0)
        (self.root / "proc" / "vmstat").unlink()
        self.assertIsInstance(memory_feed.kernel_ram(values).counters, admission_records.ReadingUnavailable)

    def test_ram_blocks_name_a_missing_reading_and_a_disagreement(self) -> None:
        gib = admission_records.GIB
        self.ram_fixture(meminfo=False)
        blocks = memory_feed.ram_blocks(self.fixture_room(20 * gib), memory_feed.BuildToolRam(0, 0, 0), 100.0, 6)
        self.assertIsInstance(blocks["kernel_bytes"], admission_records.ReadingUnavailable)
        self.assertIsInstance(blocks["cache_bytes"], admission_records.ReadingUnavailable)
        self.assertEqual([line.split(":")[0] for line in blocks["missing"]], ["Kernel, measured", "File and kernel cache"])
        small = replace(self.fixture_room(20 * gib), total=30 * gib)
        self.ram_fixture()
        self.assertEqual(memory_feed.ram_blocks(small, memory_feed.BuildToolRam(0, 0, 0), 100.0, 6)["agreement"], "disagree")

    def test_the_cap_and_ci_block_count_ci_swap_backed_use_and_leave_the_file_cache_to_the_cache_block(self) -> None:
        gib = admission_records.GIB

        def unowned(working_set: int, anonymous: admission_records.AnonymousRunUse | admission_records.RunUseUnavailable,
                    job_state: IdleService | NotServiceSlice, diagnostic: str = "") -> admission_records.UnownedSliceMemory:
            return admission_records.UnownedSliceMemory(working_set, anonymous=anonymous, owner="hana-ci.slice",
                                                        job_state=job_state, diagnostic=diagnostic,
                                                        pid=admission_records.PidNotRecorded())

        ci = unowned(3 * gib, admission_records.AnonymousRunUse(gib // 4), IdleService())
        build_scope = unowned(2 * gib, admission_records.AnonymousRunUse(gib), NotServiceSlice())
        absent = unowned(0, admission_records.RunUseUnavailable("not read"), IdleService(), diagnostic="CI slice absent")
        self.assertEqual(gate.ci_cap_use((ci, build_scope, absent)), gib // 4)
        # A reading without its swap-backed use counts its working set.
        unread = unowned(3 * gib, admission_records.RunUseUnavailable("stat unreadable"), IdleService())
        self.assertEqual(gate.ci_cap_use((ci, unread)), gib // 4 + 3 * gib)

    def test_the_memory_status_names_the_one_reason_a_start_waits(self) -> None:
        gib = admission_records.GIB
        explanation = captured_reading().explanation
        room = self.fixture_room(20 * gib)
        inputs = explanation.decided_from
        for code, pressure, captured in (
            ("none", inputs.pressure, room),
            ("pressure", admission_records.MemoryPressure(9.0), room),
            ("unavailable", admission_records.PressureUnavailable("fixture"), room),
            ("shortfall", inputs.pressure, self.fixture_room(5 * gib)),
            ("no_room", inputs.pressure, self.fixture_room(12 * gib)),
        ):
            with self.subTest(code=code):
                changed = replace(explanation, decided_from=replace(inputs, pressure=pressure))
                status = memory_feed.memory_status(changed, captured, 0)
                self.assertEqual(status["code"], code)
                self.assertEqual(status["check_room_bytes"], captured.room)
        waiter: admission_records.Waiter = {"pid": 999, "start_time": "999", "need": gib, "since": inputs.now,
            "session_id": "waiting", "session_name": "waiting", "unit": "", "step": "check", "folder": "/tree"}
        waiting = gate.queue_read_from(replace(inputs, ordered=[(Path("waiting"), waiter)]), admission_policy_fixture()).explanation
        head = waiting.head
        assert isinstance(head, admission_records.HeadExplanation)
        gpu_wait = replace(waiting, head=replace(head, cause=admission_records.GpuSlotWait(1, 1)))
        self.assertEqual(memory_feed.memory_status(gpu_wait, room, 0)["code"], "gpu_slot")
        # Memory outside the queue that leaves nothing free is the cause, not the GPU slot.
        for code, available in (("shortfall", 5 * gib), ("no_room", 12 * gib)):
            with self.subTest(gpu_slot=code):
                self.assertEqual(memory_feed.memory_status(gpu_wait, self.fixture_room(available), 0)["code"], code)

    def tool_process(self, pid: int, command: str, kib: int, *, parent: int = 1,
                     children: tuple[int, ...] = (), shared_kib: int = 0) -> Path:
        proc = self.root / "proc"
        write_process(proc, pid, kib * 1024, parent=parent)
        process = proc / str(pid)
        _ = (process / "cmdline").write_bytes(command.encode().replace(b" ", b"\0") + b"\0")
        _ = (process / "status").write_text(
            f"VmRSS: {kib + 2 * shared_kib} kB\nRssAnon: {kib} kB\nRssShmem: {2 * shared_kib} kB\n")
        _ = (process / "smaps_rollup").write_text(f"Pss_Anon: {kib} kB\nPss_Shmem: {shared_kib} kB\n")
        task = process / "task" / str(pid)
        task.mkdir(parents=True, exist_ok=True)
        _ = (task / "children").write_text(" ".join(map(str, children)))
        return process

    def tool_scope_unit(self, scope: Path, command: str) -> Path:
        unit = self.root / "runtime" / "systemd" / "transient" / scope.name
        unit.parent.mkdir(parents=True, exist_ok=True)
        _ = unit.write_text(f"[Scope]\nDescription=[systemd-run] {command}\n")
        return unit

    def tool_split(self, *waiting: int) -> memory_feed.BuildToolRam:
        waiters: list[admission_records.RankedWaiter] = [
            {"pid": pid, "start_time": str(pid), "need": 1024, "since": 0, "order": admission_records.NormalOrder()}
            for pid in waiting]
        with (mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root / "runtime")}),
              mock.patch.object(memory_feed, "PROC", self.root / "proc"),
              mock.patch.object(memory_feed, "CGROUP", self.root),
              mock.patch.object(gate, "builds_slice_cgroup", return_value=self.root / "builds.slice")):
            return memory_feed.build_tool_ram((), 0, waiters)

    def test_detect_scope_estimates_test_only_and_mixed_charges(self) -> None:
        _ = self.tool_process(101, "python -m unittest", 10, children=(102,))
        _ = self.tool_process(102, "python3 -c child", 20, parent=101, children=(103,))
        _ = self.tool_process(103, "sleep 10", 10, parent=102)
        _ = self.tool_process(104, "other", 50)
        builds = self.root / "builds.slice"
        scope = builds / "builds-detached.slice" / "detached-detect-20261117T192504-3473301-5b4c.scope"
        cases: tuple[tuple[tuple[int, ...], int], ...] = (
            ((101, 102, 103), 100),
            ((101, 102, 103, 104), 40),
            ((), 0),
        )
        for members, expected in cases:
            with self.subTest(members=members):
                write_scope(builds, anonymous=100 * 1024)
                write_scope(scope.parent, anonymous=100 * 1024)
                write_scope(scope, members=members, anonymous=100 * 1024)
                self.assertEqual(self.tool_split(), memory_feed.BuildToolRam(0, 0, (100 - expected) * 1024,
                                                                           python_tests=expected * 1024))

    def test_detached_test_scope_keeps_its_class_after_the_runner_exits(self) -> None:
        _ = self.tool_process(102, "python3 -c child", 20)
        builds = self.root / "builds.slice"
        run_id = "20261117T192504-3473301-5b4c"
        scope = builds / "builds-detached.slice" / f"detached-python-tests-{run_id}.scope"
        child_group = scope / "children"
        write_scope(builds, anonymous=100 * 1024)
        write_scope(scope.parent, anonymous=100 * 1024)
        write_scope(scope, anonymous=100 * 1024)
        write_scope(child_group, members=(102,), anonymous=70 * 1024)
        record = self.root / "detached_runs" / f"{run_id}.json"
        record.parent.mkdir()
        _ = record.write_text('{"status": "finished", "kind": "python-tests"}')
        expected = memory_feed.BuildToolRam(0, 0, 0, python_tests=100 * 1024)
        with (mock.patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.root)}),
              mock.patch.object(memory_feed.ToolProcessReader, "command", side_effect=AssertionError("live command read"))):
            self.assertEqual(self.tool_split(), expected)
            record.unlink()
            self.assertEqual(self.tool_split(), expected)

    def test_detached_scope_without_test_evidence_counts_as_other_tools(self) -> None:
        builds = self.root / "builds.slice"
        detached = builds / "builds-detached.slice"
        for name, command in (
            ("run-test.scope", "python3 -c child"),
            ("detached-other-20261117T192504-3473301-5b4c.scope", "python3 -m unittest"),
        ):
            with self.subTest(name=name):
                _ = self.tool_process(102, command, 20)
                scope = detached / name
                unit = self.tool_scope_unit(scope, "/usr/bin/python3 -m unittest")
                write_scope(builds, anonymous=100 * 1024)
                write_scope(detached, anonymous=100 * 1024)
                write_scope(scope, members=(102,), anonymous=100 * 1024)
                self.assertEqual(self.tool_split(), memory_feed.BuildToolRam(0, 0, 100 * 1024))
                unit.unlink()
                _ = scope.rename(self.root / name)

    def test_tool_group_split_names_both_byte_counts(self) -> None:
        builds = self.root / "builds.slice"
        detached = builds / "builds-detached.slice"
        scope = detached / "test.scope"
        _ = self.tool_process(101, "python3 -m unittest", 10)
        _ = self.tool_process(102, "other", 20)
        write_scope(detached, anonymous=30 * 1024)
        write_scope(scope, members=(101, 102), anonymous=30 * 1024)
        with (mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root / "runtime")}),
              mock.patch.object(memory_feed, "PROC", self.root / "proc")):
            shares = memory_feed.split_tool_groups(builds, set(), set(), set(), ())[scope]
        self.assertNotIsInstance(shares, tuple)
        self.assertEqual(shares.python_tests_bytes, 10 * 1024)
        self.assertEqual(shares.waiting_runs_bytes, 0)

    def test_shared_pages_in_mixed_group_count_once_for_waiting_wrappers(self) -> None:
        builds = self.root / "builds.slice"
        for pid, command, parent, children, shared in (
            (101, "bash /fixture/verify.sh test", 1, (102,), 15),
            (102, "python3 local_release_wait.py", 101, (), 15),
            (103, "other", 1, (), 0),
        ):
            process = self.tool_process(pid, command, 10 if shared else 50,
                                        parent=parent, children=children, shared_kib=shared)
            _ = (process / "cgroup").write_text("0::/builds.slice/mixed.scope\n")
        write_scope(builds, anonymous=100 * 1024)
        write_scope(builds / "mixed.scope", members=(101, 102, 103), anonymous=100 * 1024)
        self.assertEqual(self.tool_split(102), memory_feed.BuildToolRam(0, 0, 50 * 1024, waiting_runs=50 * 1024))

    def test_waiting_verify_shell_with_options_counts_as_a_wrapper(self) -> None:
        builds = self.root / "builds.slice"
        shell = self.tool_process(101, "bash -e /fixture/verify.sh test", 10, children=(102,))
        owner = self.tool_process(102, "python3 local_release_wait.py", 10, parent=101)
        for process in (shell, owner):
            _ = (process / "cgroup").write_text("0::/builds.slice/verify.scope\n")
        write_scope(builds, anonymous=20 * 1024)
        write_scope(builds / "verify.scope", members=(101, 102), anonymous=20 * 1024)
        self.assertEqual(self.tool_split(102), memory_feed.BuildToolRam(0, 0, 0, waiting_runs=20 * 1024))

    def test_waiting_tools_skip_unrelated_processes_and_cgroup_subtrees(self) -> None:
        builds = self.root / "builds.slice"
        shell = self.tool_process(101, "bash /fixture/verify.sh test", 10, children=(102,))
        owner = self.tool_process(102, "python3 local_release_wait.py", 10, parent=101)
        thread = owner / "task" / "104"
        thread.mkdir()
        _ = (thread / "children").write_text("103")
        helper = self.tool_process(103, "python3 verify_helper.py", 10, parent=102)
        _ = self.tool_process(199, "other", 20)
        for process in (shell, owner):
            _ = (process / "cgroup").write_text("0::/builds.slice/verify.scope\n")
        _ = (helper / "cgroup").write_text("0::/builds.slice/verify.scope/helper.scope\n")
        write_scope(builds, anonymous=50 * 1024)
        write_scope(builds / "verify.scope", members=(101, 102), anonymous=30 * 1024)
        write_scope(builds / "verify.scope" / "helper.scope", members=(103,), anonymous=10 * 1024)
        unrelated = builds / "unrelated.scope" / "child.scope"
        write_scope(unrelated.parent, anonymous=20 * 1024)
        write_scope(unrelated, members=(199,), anonymous=20 * 1024)
        with (mock.patch.object(gate, "process_table", wraps=gate.process_table) as table,
              mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read):
            tools = self.tool_split(102)
        table.assert_not_called()
        self.assertNotIn(mock.call(unrelated / "cgroup.procs"), read.call_args_list)
        self.assertEqual(tools, memory_feed.BuildToolRam(0, 0, 20 * 1024, waiting_runs=30 * 1024))

    def test_python_tests_and_waiting_wrappers_split_tools_without_counting_cargo_twice(self) -> None:
        gib = admission_records.GIB
        for cargo_use in (0, 2 * admission_records.GIB):
            with self.subTest(cargo_use=cargo_use), AdmissionFixture(root=self.root / str(cargo_use)) as fixture:
                python = fixture.fixture_owner(700001, "100")
                process = fixture.proc.path / str(python.pid)
                _ = process.joinpath("comm").write_text("python3\n")
                _ = process.joinpath("cmdline").write_bytes(b"python3\0-m\0unittest\0")
                proc = fixture.proc.path
                shell, waiter_pid, helper, unknown = 700010, 700011, 700012, 700013
                for pid, parent, use, command in (
                    (shell, 1, gib // 4, b"bash\0/fixture/verify.sh\0test\0"),
                    (waiter_pid, shell, gib // 4, b"python3\0local_release_wait.py\0"),
                    (helper, shell, gib // 2, b"python3\0verify_helper.py\0"),
                    (unknown, 1, gib, b"python3\0tree_watch.py\0"),
                ):
                    write_process(proc, pid, use, parent=parent)
                    process = proc / str(pid)
                    _ = (process / "cmdline").write_bytes(command)
                    _ = (process / "smaps_rollup").write_text(f"Pss_Anon: {use // 1024} kB\nPss_Shmem: 0 kB\n")
                    task = process / "task" / str(pid)
                    task.mkdir(parents=True)
                    children = {shell: f"{waiter_pid} {helper}"}
                    _ = (task / "children").write_text(children.get(pid, ""))
                python_use = 4 * gib
                builds = Path(os.environ["BUILDLOG_BUILDS_CGROUP"])
                detached = builds / "builds-detached.slice"
                write_scope(builds, current=python_use + cargo_use + 2 * gib, anonymous=python_use + cargo_use + 2 * gib)
                write_scope(detached, current=python_use + cargo_use + 2 * gib, anonymous=python_use + cargo_use + 2 * gib)
                python_scope = detached / "detached-python-tests-20261117T192504-3473301-5b4c.scope"
                write_scope(python_scope, members=(python.pid,),
                    current=python_use + cargo_use, anonymous=python_use + cargo_use)
                write_scope(detached / "verify.scope", members=(shell, waiter_pid, helper, unknown),
                            current=2 * gib, anonymous=2 * gib)
                if cargo_use:
                    cargo = fixture.fixture_owner(700002, "200")
                    sidecar = fixture.scope(cargo, folder=str((python_scope / "cargo.scope").relative_to(builds.parent)),
                        current=cargo_use, anonymous=cargo_use)
                    request = replace(fixture.request(owner=cargo, need=8 * admission_records.GIB), sidecar=str(sidecar))
                    grant = only_admitted(fixture.server.handle_request(request))
                    self.assertEqual(len(fixture.server.reservations), 1)
                    self.assertIn(grant.reservation, [record.reservation_id for record in fixture.server.reservations.values()])
                fixture.free(60 * gib - python_use - cargo_use - 2 * gib)
                _ = fixture.run_pass()
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                inputs = reply.reading.explanation.decided_from
                for start_time, wrapper_use in ((str(waiter_pid), gib), ("reused pid", 0)):
                    waiter: admission_records.Waiter = {"pid": waiter_pid, "start_time": start_time,
                        "since": inputs.now, "need": 60 * gib, "step": "nextest", "folder": "/fixture"}
                    reading = gate.queue_read_from(replace(inputs, ordered=[(Path("waiter"), waiter)]),
                                                   admission_policy_fixture())
                    with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)),
                          mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root / "runtime")}),
                          mock.patch.object(memory_feed, "PROC", proc)):
                        page = memory_feed.reading()
                    ram = page["ram"]
                    self.assertEqual((ram["python_tests_bytes"], ram["waiting_runs_bytes"], ram["other_build_tools_bytes"]),
                                     (python_use, wrapper_use, 2 * gib - wrapper_use))
                    self.assertEqual(ram["build_tools_bytes"], python_use + 2 * gib)
                    self.assertEqual(page["used"], cargo_use / gib)
                    build_rows = [row for row in page["walk"] if not row.get("ci", False)]
                    self.assertEqual(len(build_rows), int(bool(cargo_use)))
                    self.assertTrue(all(row.get("queued", False) for row in build_rows))
                room = reply.reading.explanation.room
                assert isinstance(room, admission_records.MemoryRoom)
                self.assertEqual(len(room.runs), int(bool(cargo_use)))
                self.assertEqual(room.used, cargo_use)
                self.assertEqual(room.used + room.kept, 8 * admission_records.GIB if cargo_use else 0)

    def test_tracked_run_keeps_its_row_when_detached_scopes_account_for_its_tools(self) -> None:
        gib = admission_records.GIB
        with AdmissionFixture() as fixture:
            proc = fixture.proc.path
            root = fixture.fixture_owner(700001, "100")
            run = wire.RunIdentity(root.pid, root.start_time)
            helper_pids = (700002, 700003)
            write_process(proc, root.pid, gib, start=root.start_time)
            _ = (proc / str(root.pid) / "cmdline").write_bytes(b"bash\0/fixture/verify.sh\0test\0")
            for pid in helper_pids:
                write_process(proc, pid, gib // 4, parent=root.pid)
                _ = (proc / str(pid) / "cmdline").write_bytes(b"sleep\0")
            first = fixture.fixture_owner(700004, "200")
            _ = fixture.finish_step(fixture.request(owner=first, run=run))
            fixture.clock.advance(200)
            self.assertTrue(fixture.server.run_places)

            builds = Path(os.environ["BUILDLOG_BUILDS_CGROUP"])
            detached = builds / "builds-detached.slice"
            run_slice = detached / "builds-detached-r1.slice"
            write_scope(builds, current=5 * gib, anonymous=5 * gib)
            write_scope(detached, current=5 * gib, anonymous=5 * gib)
            write_scope(run_slice, current=5 * gib, anonymous=5 * gib)
            cargo = fixture.fixture_owner(700005, "300")
            sidecar = fixture.scope(cargo, folder=str((run_slice / "cargo.scope").relative_to(fixture.root)),
                                    current=2 * gib, anonymous=2 * gib)
            _ = only_admitted(fixture.server.handle_request(replace(fixture.request(owner=cargo), sidecar=str(sidecar))))

            def page() -> tuple[memory_feed.MemoryPage, admission_records.QueueRead]:
                _ = fixture.run_pass()
                reply, = fixture.server.handle_request(wire.ViewRequest())
                decoded = wire.parse_reply(wire.encode_reply(reply))
                assert isinstance(decoded, wire.ViewReply)
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(decoded.reading)),
                      mock.patch.object(memory_feed, "PROC", proc)):
                    return memory_feed.reading(), decoded.reading

            before, baseline = page()
            original, = (row for row in before["walk"] if row["pid"] == root.pid)
            original = cast(memory_feed.RunRow, original)
            self.assertEqual((original["name"], original["used"], original["counted"]), ("verify.sh", 1.5, 0.0))

            for pid, scope_gib in zip(helper_pids, (1, 2), strict=True):
                write_scope(run_slice / f"helper-{pid}.scope", members=(pid,),
                            current=scope_gib * gib, anonymous=scope_gib * gib)
            after, reading = page()
            matching = {str(run_slice / f"helper-{pid}.scope") for pid in helper_pids}
            self.assertTrue(matching.isdisjoint(step.measured.cgroup for step in reading.view.running
                                               if isinstance(step.measured, admission_records.UnownedSliceMemory)))
            shown, = (row for row in after["walk"] if row["pid"] == root.pid)
            self.assertEqual(shown, original)
            self.assertEqual(after["walk"], before["walk"])
            self.assertEqual(reading.explanation.totals, baseline.explanation.totals)
            self.assertEqual(reading.explanation.totals.measured, 7 * gib // 2)
            ram = after["ram"]
            self.assertEqual((ram["queued_builds_bytes"], ram["build_tools_bytes"], ram["other_build_tools_bytes"]),
                             (2 * gib, 3 * gib, 3 * gib))
            self.assertEqual(after["used"], 2.0)

            other = fixture.fixture_owner(700006, "400")
            other_run = wire.RunIdentity(other.pid, other.start_time)
            for offset, (pid, scope_gib) in enumerate(zip(helper_pids, (1, 2), strict=True)):
                owner = fixture.fixture_owner(700007 + offset, str(500 + offset))
                scope = run_slice / f"helper-{pid}.scope"
                sidecar = fixture.scope(owner, folder=str(scope.relative_to(fixture.root)), members=(pid,),
                                        current=scope_gib * gib, anonymous=scope_gib * gib)
                fixture.allow_next_start()
                request = fixture.request(owner=owner, run=other_run)
                _ = only_admitted(fixture.server.handle_request(replace(request, sidecar=str(sidecar))))

            owned, reading = page()
            self.assertTrue(matching.isdisjoint(step.measured.cgroup for step in reading.view.running
                                               if isinstance(step.measured, admission_records.UnownedSliceMemory)))
            self.assertEqual({str(step.measured.anonymous_source.directory) for step in reading.view.running
                              if isinstance(step.run, LiveBuildRun) and step.run.pid == other.pid
                              and isinstance(step.measured, admission_records.MeasuredMemory)
                              and isinstance(step.measured.anonymous_source, admission_records.ScopeSource)}, matching)
            retained, = (row for row in owned["walk"] if row["pid"] == root.pid)
            self.assertEqual(retained, original)
            self.assertEqual(owned["ram"]["queued_builds_bytes"], 5 * gib)
            self.assertEqual(owned["ram"]["build_tools_bytes"], 0)

    def test_system_read_matches_main_for_nested_detached_scopes(self) -> None:
        gib = admission_records.GIB
        with AdmissionFixture() as fixture:
            proc = fixture.proc.path
            root = fixture.fixture_owner(700001, "100")
            run = wire.RunIdentity(root.pid, root.start_time)
            helpers = (700002, 700003)
            write_process(proc, root.pid, gib, start=root.start_time)
            _ = (proc / str(root.pid) / "cmdline").write_bytes(b"bash\0/fixture/verify.sh\0test\0")
            for pid in helpers:
                write_process(proc, pid, gib // 4, parent=root.pid)
            first = fixture.fixture_owner(700004, "200")
            _ = fixture.finish_step(fixture.request(owner=first, run=run))
            fixture.allow_next_start()

            builds = Path(os.environ["BUILDLOG_BUILDS_CGROUP"])
            helper = builds / "builds-detached.slice/builds-detached-r1.slice/helper.scope"
            child = helper / "cargo.scope"
            write_scope(helper, members=helpers, current=5 * gib, anonymous=5 * gib)
            cargo_owner = fixture.fixture_owner(700005, "300")
            for pid in (root.pid, *helpers, cargo_owner.pid):
                (proc / str(pid) / "cwd").symlink_to(fixture.root)
            sidecar = fixture.scope(cargo_owner, folder=str(child.relative_to(fixture.root)),
                                    current=2 * gib, anonymous=2 * gib)
            request = replace(fixture.request(owner=cargo_owner), sidecar=str(sidecar))
            _ = only_admitted(fixture.server.handle_request(request))
            record = fixture.server.reservations[request.episode]
            config = fixture.server.last_usable_configuration
            assert isinstance(config, admission_records.MemoryConfig)
            plan = admission_service.SystemReadingPlan(replace(config, ci_memory_slice=""), (record,),
                                                       tuple(fixture.server.run_places.values()), False)
            reads: Counter[str] = Counter()
            read_bytes = Path.read_bytes
            read_text = Path.read_text
            readlink = Path.readlink
            iterdir = Path.iterdir

            def read_process(path: Path) -> bytes:
                if path.parent.parent == proc and path.name in ("status", "cmdline", "environ"):
                    reads[path.name] += 1
                return read_bytes(path)

            def read_group(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
                if path.is_relative_to(builds):
                    if path.name == "cgroup.procs":
                        reads["cgroup.procs"] += 1
                    elif path == builds / "sccache.service/memory.current":
                        reads["compiler probe"] += 1
                    elif path.name in ("memory.current", "memory.stat", "memory.swap.current"):
                        reads["memory counters"] += 1
                return read_text(path, encoding=encoding, errors=errors)

            def read_folder(path: Path) -> Path:
                if path.parent.parent == proc and path.name == "cwd":
                    reads["cwd"] += 1
                return readlink(path)

            def list_groups(path: Path) -> Iterator[Path]:
                if path.is_relative_to(builds):
                    reads["cgroup listings"] += 1
                return iterdir(path)

            with (mock.patch.object(Path, "read_bytes", read_process), mock.patch.object(Path, "read_text", read_group),
                  mock.patch.object(Path, "readlink", read_folder), mock.patch.object(Path, "iterdir", list_groups)):
                evidence = admission_service.read_system(plan, fixture.readings, fixture.proc, fixture.clock)
            # The scope with no recorded caller reads its oldest process command line once for its owner name.
            self.assertEqual(dict(reads), {"status": 3, "cmdline": 2, "environ": 1, "cwd": 1,
                                           "cgroup.procs": 2, "cgroup listings": 2, "memory counters": 3,
                                           "compiler probe": 1})
            self.assertEqual(evidence.unowned, ())
            tracked, = evidence.between
            assert isinstance(tracked.measured, admission_records.MeasuredMemory)
            self.assertEqual(tracked.measured.bytes, 3 * gib // 2)
            memory = evidence.reservations[request.episode].memory
            assert isinstance(memory, admission_records.MeasuredMemory)
            self.assertEqual(memory.anonymous_source, admission_records.ScopeSource(child, frozenset({cargo_owner.pid})))

    def test_ci_scan_deducts_a_process_that_enters_a_reserved_scope_between_samples(self) -> None:
        gib = admission_records.GIB
        with AdmissionFixture() as fixture:
            ci = Path(os.environ["BUILDLOG_CI_CGROUP"])
            child = ci / "job.scope"
            write_scope(ci, current=2 * gib, anonymous=2 * gib)
            tree_owner = fixture.fixture_owner(700001, "100")
            scope_owner = fixture.fixture_owner(700002, "200")
            write_process(fixture.proc.path, tree_owner.pid, gib, start=tree_owner.start_time)
            tree_request = fixture.request(owner=tree_owner, need=2 * gib)
            _ = only_admitted(fixture.server.handle_request(tree_request))
            fixture.allow_next_start()
            sidecar = fixture.scope(scope_owner, folder=str(child.relative_to(fixture.root)))
            scope_request = replace(fixture.request(owner=scope_owner, need=2 * gib), sidecar=str(sidecar))
            _ = only_admitted(fixture.server.handle_request(scope_request))
            config = fixture.server.last_usable_configuration
            assert isinstance(config, admission_records.MemoryConfig)
            plan = admission_service.SystemReadingPlan(config, tuple(fixture.server.reservations.values()), (), False)
            read_text = Path.read_text

            def move_process(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
                if path == ci / "memory.current":
                    # The earlier reservation already measured its child.
                    write_scope(ci, current=3 * gib, anonymous=3 * gib)
                    write_scope(child, members=(scope_owner.pid, tree_owner.pid), current=2 * gib, anonymous=2 * gib)
                return read_text(path, encoding=encoding, errors=errors)

            for move in (False, True):
                with self.subTest(move=move), mock.patch.object(Path, "read_text", autospec=True,
                                                                  side_effect=move_process if move else read_text):
                    evidence = admission_service.read_system(plan, fixture.readings, fixture.proc, fixture.clock)
                child_memory = evidence.reservations[scope_request.episode].memory
                assert isinstance(child_memory, admission_records.MeasuredMemory)
                self.assertEqual(child_memory.anonymous_source,
                                 admission_records.ScopeSource(child, frozenset({scope_owner.pid})))
                self.assertEqual(evidence.scope_members[str(ci)], {scope_owner.pid, tree_owner.pid}
                                 if move else {scope_owner.pid})
                fixture.server.system_reader = FixedSystemReader(evidence)
                _ = fixture.run_pass()
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reply.reading)),
                      mock.patch.object(memory_feed, "PROC", fixture.proc.path)):
                    page = memory_feed.reading()
                self.assertEqual(page["ci_ram"], 1.0)

    def test_registered_and_unregistered_ci_jobs_count_once_without_margin_text(self) -> None:
        for registered, registered_use, unregistered_use, kept in (
                ((), 0, 6, 0), ((700001,), 2, 4, 6), ((700001, 700002), 6, 0, 10)):
            with self.subTest(registered=registered), AdmissionFixture() as fixture:
                fixture.configure(safety_gib=3)
                owners: list[wire.Owner] = []
                for pid, worker, used in ((700001, 700011, 2), (700002, 700012, 4)):
                    owner = fixture.fixture_owner(pid, str(pid))
                    owners.append(owner)
                    write_process(fixture.proc.path, worker)
                    _ = (fixture.proc.path / str(worker) / "comm").write_text("Runner.Worker\n")
                    write_process(fixture.proc.path, pid, used * admission_records.GIB, parent=worker)
                    _ = (fixture.proc.path / str(pid) / "comm").write_text("cargo\n")
                ci = Path(os.environ["BUILDLOG_CI_CGROUP"])
                write_scope(ci, members=(700011, 700001, 700012, 700002),
                    current=6 * admission_records.GIB, anonymous=6 * admission_records.GIB)
                for owner in owners:
                    if owner.pid in registered:
                        fixture.allow_next_start()
                        _ = only_admitted(fixture.server.handle_request(fixture.request(owner=owner)))
                fixture.free(20 * admission_records.GIB)
                _ = fixture.run_pass()
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reply.reading)):
                    page = memory_feed.reading()
                self.assertEqual(page["used"], registered_use)
                self.assertEqual(page["ci_ram"], unregistered_use)
                self.assertEqual(page["cap"], 23)
                self.assertEqual(page["reserved"], kept)
                self.assertEqual(page["room"], 17 - kept)
                self.assertEqual(len(page["walk"]), len(registered) + 1)
                self.assertEqual(sum(row.get("queued", False) for row in page["walk"]), len(registered))
                text = memory_feed.page_json(page)
                self.assertNotIn("CI builds do not wait in the queue yet.", text)
                self.assertNotIn("CI keep-aside", text)

    def test_the_leftover_list_uses_the_queue_rule_and_keeps_all_scope_members(self) -> None:
        ended = leftover_step(45)
        memory = ended.measured
        assert isinstance(memory, admission_records.UnownedSliceMemory)
        activity = memory.activity
        assert isinstance(activity, ScopeProcesses)
        ended = replace(ended, measured=replace(memory, activity=replace(activity, count=2,
            members=(ScopeProcess(45, "python3"), ScopeProcess(46, "helper")))))
        command = self.root / "proc" / "45" / "cmdline"
        write_process(command.parent.parent, 45)
        _ = command.write_bytes(b"python3\0-u\0/fixture/check_rows.py\0")
        live = replace(ended, pid=60, measured=replace(memory,
            activity=replace(activity, run=LiveBuildRun(60, "live", 199_000))))
        compiler = replace(ended, pid=70, measured=admission_records.CompilerServiceMemory(admission_records.GIB, pid=70))
        service = replace(ended, pid=80, measured=replace(memory, job_state=IdleService()))
        reading = captured_reading(running=(ended, live, compiler, service))
        with (mock.patch.object(queue_view, "leftover_processes", wraps=queue_view.leftover_processes) as shared_rule,
              mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading))):
            page = memory_feed.reading()
        shared_rule.assert_called_once_with(reading.view.running)
        self.assertEqual(len(page["leftovers"]), 1)
        self.assertEqual(page["leftovers"][0]["members"], [
            {"pid": 45, "program": "check_rows.py"}, {"pid": 46, "program": "helper"}])
        for member in page["leftovers"][0]["members"]:
            self.assertIsInstance(member["program"], memory_feed.ProcessName)
        self.assertEqual(page["leftovers"][0]["age"], "45 min")

    def test_an_unavailable_cap_keeps_rows_room_and_reason(self) -> None:
        reading = reservation_reading()
        room = reading.explanation.room
        assert isinstance(room, admission_records.MemoryRoom)
        missing_run = replace(room.runs[0].memory, use=admission_records.RunUseUnavailable("private memory.stat is missing"))
        for cap, reason in (
                (room.cap, ""),
                (admission_records.CapUnavailable(()),
                 "Cap unavailable because memory use is unavailable for a running build"),
                (admission_records.CapUnavailable((missing_run,)),
                 f"Cap unavailable because memory use is unavailable for {missing_run.owner}: private memory.stat is missing")):
            with self.subTest(cap=cap):
                changed = replace(reading, explanation=replace(reading.explanation, room=replace(room, cap=cap)))
                with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(changed)):
                    page = memory_feed.reading()
                self.assertIsInstance(page["cap"], admission_records.CapUnavailable if reason else float)
                self.assertEqual(page["room"], room.room / admission_records.GIB)
                self.assertTrue(page["walk"])
                self.assertEqual(page["cap_text"], reason)

    def test_an_unavailable_run_use_keeps_its_named_reason(self) -> None:
        reading = reservation_reading()
        room = reading.explanation.room
        assert isinstance(room, admission_records.MemoryRoom)
        missing = admission_records.RunUseUnavailable("private memory.stat is missing")
        run = replace(room.runs[0], memory=replace(room.runs[0].memory, use=missing))
        room = replace(room, runs=(run,), cap=admission_records.CapUnavailable((run.memory,)))
        reading = replace(reading, explanation=replace(reading.explanation, room=room))
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            page = memory_feed.reading()
        run_row = page["walk"][0]
        assert "used" in run_row
        self.assertEqual(run_row["used"], missing)
        self.assertIn("RunUseUnavailable", memory_feed.page_json(page))

    def test_one_report_call_supplies_top_costs(self) -> None:
        with (mock.patch.object(charts, "page_data", return_value=report_fixture()) as provider,
              mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(captured_reading()))):
            report = memory_feed.report_reading(7)
        provider.assert_called_once_with(7, n=3)
        assert isinstance(report, memory_feed.ReportSections)
        row = report.top[0]
        self.assertEqual((row["machine"], row["cause"], row["range"]), ("fixture", "Memory wait", [30 / 3600, 60 / 3600]))
        self.assertEqual(row["share"], "fixture nextest paid 75% of it.")
        self.assertEqual(row["action"], "Action: lower the memory peak of fixture nextest. It holds the most build memory: "
                                        + "12 GiB·h/day. Most runs peak at 3.0 GiB or less.")

    def test_an_unmeasured_day_sends_no_range_and_no_share(self) -> None:
        report = report_fixture()
        assert not isinstance(report.top.rows, charts.NoDataYet)
        row = replace(report.top.rows[0], share=cost_actions.NoShare.VALUE,
                      days=(charts.MeasuredCost(30), charts.CostUnavailable("1 applicable waits missing")))
        with mock.patch.object(charts, "page_data", return_value=replace(report, top=replace(report.top, rows=(row,)))):
            sections = memory_feed.report_reading(7)
        assert isinstance(sections, memory_feed.ReportSections)
        self.assertEqual((sections.top[0]["range"], sections.top[0]["share"]), ([], ""))

    def test_report_failures_keep_the_queue_reading(self) -> None:
        for failure in (charts.IndexUnavailable("private index is missing"),
                        conf.Unusable("private report configuration is invalid")):
            with self.subTest(failure=failure):
                provider = (mock.patch.object(charts, "page_data", side_effect=failure)
                            if isinstance(failure, Exception) else
                            mock.patch.object(charts, "page_data", return_value=failure))
                with (provider,
                      mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reservation_reading()))):
                    page = memory_feed.reading()
                    text = memory_feed.report_file(memory_feed.report_reading(7)).text
                self.assertTrue(page["walk"])
                self.assertIn(failure.reason if isinstance(failure, charts.IndexUnavailable) else str(failure), text)

    def test_an_empty_report_keeps_its_recent_window(self) -> None:
        with mock.patch.object(charts, "page_data", return_value=report_fixture(empty=True)):
            text = memory_feed.report_file(memory_feed.report_reading(7)).text
        self.assertIn("2026-11-05", text)
        self.assertIn("2026-11-08", text)

    def test_move_identity_uses_the_wait_owner_and_keeps_the_service_revision(self) -> None:
        base = with_wait_reason(captured_reading(), admission_records.NeedsMemory())
        step, = base.view.waiting
        step = replace(step, run=LiveBuildRun(900, "run-start", 1))
        member = admission_records.QueueMoveMember("fixture-wait")
        line = admission_records.QueueMoveLine("fixture-revision", (member,))
        row = gate.rank_waiter({"pid": step.pid, "since": step.since, "start_time": "wait-start",
                               "need": step.need, "move_id": member.row_id}, admission_records.NormalOrder())
        reading = replace(base, view=replace(base.view, waiting=(step,), move_line=line),
                          ordered=[(self.root / "fixture", row)])
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
            payload = memory_feed.reading()
        rows = cast(list[dict[str, object]], payload["waiters"])
        self.assertEqual(payload.get("move_revision"), line.revision)
        self.assertEqual(rows[0].get("move"), member)
        self.assertEqual(rows[0]["pid"], 900)

        unnamed = replace(step, pid=201, step="", folder="", project="", run=NoLiveBuildRun())
        unnamed_member = admission_records.QueueMoveMember("unnamed-wait")
        unnamed_row = gate.rank_waiter({"pid": 201, "since": unnamed.since, "start_time": "unnamed-start",
            "need": unnamed.need, "move_id": unnamed_member.row_id}, admission_records.NormalOrder())
        complete = replace(line, members=(unnamed_member, *line.members))
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(replace(reading,
                               view=replace(reading.view, waiting=(unnamed, step), move_line=complete),
                               ordered=[(self.root / "unnamed", unnamed_row), *reading.ordered]))):
            named = memory_feed.reading()
        self.assertEqual(named.get("move_revision"), line.revision)
        self.assertEqual(named.get("move_line"), complete)
        shown, = named["waiters"]
        self.assertEqual(shown["name"], rows[0]["name"])
        self.assertEqual(shown.get("move"), member)
        self.assertEqual(shown.get("place"), 2)

        incomplete = replace(line, members=(*line.members, admission_records.QueueMoveMember("new-wait")))
        with mock.patch.object(queue_snapshot, "read_view_snapshot",
                               return_value=fixture_published_view(replace(reading, view=replace(reading.view, move_line=incomplete)))):
            stale = memory_feed.reading()
        self.assertNotIn("move_revision", stale)

    def test_aws_cost_feed_uses_saved_bills_and_box_time(self) -> None:
        _ = write_aws_cost_fixture(self.root)
        numeric_settings(self.root / "aws_target.conf", {"cost_explorer_interval_seconds": "82800"})
        with (mock.patch.object(subprocess, "run", side_effect=AssertionError("AWS costs only read saved records")),
              mock.patch.object(subprocess, "Popen", side_effect=AssertionError("AWS costs only read saved records"))):
            costs = aws_costs.read_saved_costs(AWS_COST_TIME)
            report = memory_feed.report_file(charts.IndexUnavailable("no index"), costs)
        self.assertEqual(cast(dict[str, object], json.loads(report.text))["aws_costs"], costs)
        self.assertEqual(costs["read_at"], AWS_COST_TIME - 43200)
        self.assertEqual(costs["next_read_at"], AWS_COST_TIME - 43200 + 82800)
        self.assertEqual(costs["records_start"], "2026-01-01")
        self.assertEqual(costs["price_day"], "2026-01-01")
        self.assertEqual(costs["today"], "2026-01-02")
        self.assertEqual(costs["limits"], {"day_usd": "20.00", "month_usd": "400.00", "reserve_usd": "3.00"})
        today, billed = costs["days"]
        self.assertEqual(today["state"], "estimate")
        self.assertEqual(today["amount_usd"], "4.00")
        self.assertEqual(billed["state"], "final")
        self.assertEqual(billed["amount_usd"], "12.00")
        self.assertNotIn("estimate_before_usd", billed)
        self.assertEqual(billed["runs"], 3)
        self.assertEqual(billed["jobs"], [
            {"job": "test affected", "runs": 1, "cost_usd": "2.00", "per_run_usd": "2.00"},
            {"job": "integrate", "runs": 1, "cost_usd": "4.00", "per_run_usd": "4.00"},
            {"job": None, "runs": 1, "cost_usd": "2.00", "per_run_usd": "2.00"},
        ])
        self.assertEqual(billed["units"], [
            {"unit": "report", "runs": 1, "cost_usd": "2.00"},
            {"unit": "research", "runs": 1, "cost_usd": "4.00"},
            {"unit": None, "runs": 1, "cost_usd": "2.00"},
        ])
        self.assertEqual(billed["no_run_usd"], "4.00")
        self.assertEqual(billed["unrecorded_compute_usd"], "0.00")
        self.assertEqual(billed["runs_list"][0], {
            "id": "fixture-0", "start": AWS_COST_TIME - 43200 - 86400 + 60,
            "kind": "gate", "job": "integrate", "unit": "research", "seconds": 120, "cost_usd": "4.00",
            "region": "us-east-1", "zone": "us-east-1a", "machine": "m7a.8xlarge", "mode": "Spot",
        })
        self.assertIsNone(billed["runs_list"][1]["zone"])
        self.assertEqual(costs["kept"], {"month_usd": "53.00", "items": [
            {"name": "Hana data disk", "month_usd": "27.20"},
            {"name": "shared storage for AWS runs", "month_usd": "1.00"},
            {"name": "Other kept resources", "month_usd": "24.80"},
        ]})

    def test_aws_cost_feed_leaves_unrecorded_and_unknown_jobs_blank(self) -> None:
        for job in (GATE_JOB_NOT_RECORDED, None, StepJob("unknown"), StepJob("merge-gate")):
            with self.subTest(job=job), tempfile.TemporaryDirectory(dir=self.root) as folder:
                root = Path(folder)
                _ = write_aws_cost_fixture(root, first_job=job)
                with mock.patch.dict(os.environ, {
                    "CONF_DIR": str(root), "AWS_TARGET_CONFIG": str(root / "aws_target.conf"),
                    "BUILD_TARGETS_STATE_DIR": str(root / "target-state"),
                }):
                    billed = aws_costs.read_saved_costs(AWS_COST_TIME)["days"][1]
                self.assertEqual(billed["runs_list"][0]["kind"], "gate")
                self.assertIsNone(billed["runs_list"][0]["job"])
                self.assertEqual(billed["jobs"], [
                    {"job": "test affected", "runs": 1, "cost_usd": "2.00", "per_run_usd": "2.00"},
                    {"job": None, "runs": 2, "cost_usd": "6.00", "per_run_usd": "3.00"},
                ])

    def test_aws_cost_feed_names_the_job_from_the_mode_when_the_row_has_no_gate_job(self) -> None:
        book = write_aws_cost_fixture(self.root, first_job=None)
        with (self.root / "build_steps.conf").open("a") as names:
            _ = names.write("names.test-all=test all\n")
        modes = {"fixture-0": "gate", "fixture-1": "gate", "fixture-2": "test-affected"}
        rows = [cast(dict[str, object], json.loads(line)) for line in book.launch_history_path.read_text().splitlines()]
        for row in rows:
            row["mode"] = modes[str(row["attempt"])]
            if row["attempt"] == "fixture-2":
                del row["gate_job"]
        _ = book.launch_history_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        billed = aws_costs.read_saved_costs(AWS_COST_TIME)["days"][1]
        # fixture-1 keeps a gate_job that says the job is not recorded, so its mode does not name a job.
        self.assertEqual([(run["id"], run["job"]) for run in billed["runs_list"]],
                         [("fixture-0", "test all"), ("fixture-1", None), ("fixture-2", "test affected")])

    def test_aws_cost_feed_uses_the_same_day_amount_as_the_guard(self) -> None:
        book = write_aws_cost_fixture(self.root)
        settings = aws_config.read_settings(self.root / "aws_target.conf")
        state = book.priced_snapshot(AWS_COST_TIME)
        guard = ledger.gate_spend_days(state, settings, book.read_bills(), AWS_COST_TIME, reserve=True)
        daily, _ = ledger.spending_periods(tuple(charge for day in guard for charge in day.intervals),
                                           settings.accounting_zone)
        actual = aws_costs.read_saved_costs(AWS_COST_TIME)
        billed = next(row for row in actual["days"] if row["day"] == "2026-01-01")
        self.assertEqual(billed["amount_usd"], f'{daily[billed["day"]]:.2f}')
        self.assertEqual(billed["amount_usd"], "12.00")

    def test_aws_cost_feed_excludes_the_query_fee_from_the_billed_amount(self) -> None:
        book = write_aws_cost_fixture(self.root)
        bill, = book.read_bills()
        with book.transaction() as state:
            state.reconciliations.append(ledger.CostReconciliation(
                bill.at, bill.period_start, bill.period_end, bill.actual_usd, "Cost Explorer", Decimal("0.01")))
        settings = aws_config.read_settings(self.root / "aws_target.conf")
        priced = book.priced_snapshot(AWS_COST_TIME)
        days = ledger.gate_spend_days(priced, settings, (bill,), AWS_COST_TIME)
        self.assertEqual(aws_costs.daily_charge_totals(days, settings.accounting_zone)[bill.day], Decimal("12.01"))
        billed = next(row for row in aws_costs.read_saved_costs(AWS_COST_TIME)["days"] if row["day"] == bill.day)
        self.assertEqual(billed["amount_usd"], "12.00")
        self.assertEqual(billed["no_run_usd"], "4.00")

    def test_aws_cost_feed_separates_compute_without_a_run_from_other_costs(self) -> None:
        book = write_aws_cost_fixture(self.root)
        bill, = book.read_bills()
        unmatched = ledger.UsageTypeCost("USE2-SpotUsage:m7a.8xlarge", Decimal("4.24"), Decimal(1), "Hrs")
        bill = replace(bill, actual_usd=bill.actual_usd + unmatched.usd, usage_types=(*bill.usage_types, unmatched))
        bill_path, = book.bill_paths()
        _ = bill_path.write_text(json.dumps(ledger.charge_record(bill)))
        billed = aws_costs.read_saved_costs(AWS_COST_TIME)["days"][1]
        self.assertEqual(billed["amount_usd"], "16.24")
        self.assertEqual(billed["unrecorded_compute_usd"], "4.24")
        self.assertEqual(billed["no_run_usd"], "4.00")

    def test_aws_cost_feed_counts_hana_compute_with_and_without_an_attempt(self) -> None:
        book = write_aws_cost_fixture(self.root)
        bill, = book.read_bills()
        bill = replace(bill, actual_usd=Decimal(14), usage_types=(*bill.usage_types,
            ledger.UsageTypeCost("USE2-SpotUsage:m7a.4xlarge", Decimal(2), Decimal(1), "Hrs")))
        bill_path, = book.bill_paths()
        _ = bill_path.write_text(json.dumps(ledger.charge_record(bill)))
        start = bill.period_start + 300
        with book.transaction() as state:
            state.hana_compute.append(ledger.HanaComputeExposure(start, start + 3600, 1, Decimal(2),
                ledger.HanaExposureClosed(start + 3600), "m7a.4xlarge", region="us-east-2"))

        billed = aws_costs.read_saved_costs(AWS_COST_TIME)["days"][1]
        self.assertEqual(billed["hana"], {
            "runs": 0, "cost_usd": "2.00", "per_run_usd": "0.00", "idle_usd": "2.00", "idle_seconds": 3600})
        self.assertEqual(billed["units"][-1], {"unit": None, "runs": 1, "cost_usd": "4.00"})
        self.assertEqual(billed["no_run_usd"], "4.00")

        generation = HanaHostGeneration("fixture", "token", "boot")
        for attempt, begin, end in (("short", 10, 10.21), ("first", 30, 50), ("second", 60, 100.25)):
            book.record_hana_attempt(hana_result.HanaAttemptCharge(
                hana_result.HanaAttemptKey("episode", attempt, "claim", generation),
                hana_result.LogicalStepLink("call", "episode"), 0, 0, 0,
                start + begin, start + end, ("hana_compute:1",)))
        billed = aws_costs.read_saved_costs(AWS_COST_TIME)["days"][1]
        self.assertEqual(billed["hana"], {
            "runs": 3, "cost_usd": "2.00", "per_run_usd": "0.01", "idle_usd": "1.97", "idle_seconds": 3540})
        self.assertEqual(billed["units"][-1], {"unit": None, "runs": 4, "cost_usd": "4.00"})
        self.assertEqual([(row["id"], row["seconds"], row["cost_usd"])
                          for row in billed["runs_list"] if row["kind"] == "hana"],
                         [("short", 0, "0.00"), ("first", 20, "0.01"), ("second", 40, "0.02")])
        self.assertEqual(billed["runs_list"][-1], {
            "id": "second", "start": start + 60, "kind": "hana", "job": None, "unit": None,
            "region": "us-east-2", "zone": None,
            "machine": "m7a.4xlarge", "mode": "Spot", "seconds": 40, "cost_usd": "0.02",
        })

    def test_aws_cost_feed_counts_hana_idle_seconds_in_each_bill_day(self) -> None:
        book = write_aws_cost_fixture(self.root)
        bill, = book.read_bills()
        midnight = bill.period_end
        with book.transaction() as state:
            state.hana_compute.append(ledger.HanaComputeExposure(midnight - 1800, midnight + 3600, 1, Decimal(2),
                ledger.HanaExposureClosed(midnight + 600), "m7a.4xlarge", region="us-east-2"))
        book.record_hana_attempt(hana_result.HanaAttemptCharge(
            hana_result.HanaAttemptKey("episode", "crossing", "claim", HanaHostGeneration("fixture", "token", "boot")),
            hana_result.LogicalStepLink("call", "episode"), 0, 0, 0, midnight - 100, midnight + 50, ("hana_compute:1",)))

        today, billed = aws_costs.read_saved_costs(AWS_COST_TIME)["days"][:2]
        # The machine has 1800 s before midnight and 600 s after it. The run has 100 s and 50 s.
        self.assertEqual((billed["day"], billed["hana"] and billed["hana"]["idle_seconds"]), (bill.day, 1700))
        self.assertEqual(today["hana"] and today["hana"]["idle_seconds"], 550)
        self.assertIsNone(aws_costs.hana_idle_seconds({}, []))

    def test_aws_cost_feed_counts_overlapping_hana_attempts_once_in_the_idle_seconds(self) -> None:
        book = write_aws_cost_fixture(self.root)
        bill, = book.read_bills()
        start = bill.period_start + 300
        with book.transaction() as state:
            state.hana_compute.append(ledger.HanaComputeExposure(start, start + 3600, 1, Decimal(2),
                ledger.HanaExposureClosed(start + 3600), "m7a.4xlarge", region="us-east-2"))
        generation = HanaHostGeneration("fixture", "token", "boot")
        # Two attempts share 100 s. The third starts after the machine stops.
        for attempt, begin, end in (("first", 100, 400), ("second", 300, 600), ("late", 4000, 4100)):
            book.record_hana_attempt(hana_result.HanaAttemptCharge(
                hana_result.HanaAttemptKey("episode", attempt, "claim", generation),
                hana_result.LogicalStepLink("call", "episode"), 0, 0, 0,
                start + begin, start + end, ("hana_compute:1",)))

        billed = aws_costs.read_saved_costs(AWS_COST_TIME)["days"][1]
        self.assertEqual(billed["hana"] and (billed["hana"]["runs"], billed["hana"]["idle_seconds"]), (3, 3100))

    def test_aws_cost_feed_does_not_copy_private_fields_or_unit_identifiers(self) -> None:
        joined = "research" + PRIVATE_AWS_FIELDS["instance_id"] + "report"
        book = write_aws_cost_fixture(self.root, unknown_unit=joined)
        report = memory_feed.report_file(charts.IndexUnavailable("no index"), aws_costs.read_saved_costs(AWS_COST_TIME))
        for field, value in {**PRIVATE_AWS_FIELDS, "joined unit": joined}.items():
            with self.subTest(field=field):
                self.assertIn(value, book.launch_history_path.read_text())
                self.assertNotIn(value, report.text)
                self.assertNotIn(json.dumps(field), report.text)
                self.assertEqual(aws_costs.recorded_unit(value), "")

    def test_aws_cost_feed_keeps_the_caps_when_evidence_is_missing(self) -> None:
        install_cost_settings(self.root / "aws_target.conf")
        actual = aws_costs.read_saved_costs(AWS_COST_TIME)
        self.assertEqual(actual, {**aws_costs.empty_costs(AWS_COST_TIME), "limits": {
            "day_usd": "20.00", "month_usd": "400.00", "reserve_usd": "3.00"}})

        book = write_aws_cost_fixture(self.root)
        book.path.unlink()
        actual = aws_costs.read_saved_costs(AWS_COST_TIME + 86400)
        self.assertIsNone(actual["kept"])
        self.assertIsNone(actual["read_at"])
        self.assertEqual([(row["day"], row["state"], row["amount_usd"]) for row in actual["days"]], [
            ("2026-01-03", "none", None), ("2026-01-02", "none", None), ("2026-01-01", "final", "12.00"),
        ])

    def test_aws_cost_feed_counts_zero_and_subcent_aws_bills(self) -> None:
        instant = int(datetime(2026, 1, 4, 12, tzinfo=UTC).timestamp())
        book = write_aws_cost_fixture(self.root, instant=instant)
        bill, = book.read_bills()
        for day, value in (("2026-01-01", Decimal(0)), ("2026-01-02", Decimal("0.00004"))):
            start, end = ledger.period_bounds(date.fromisoformat(day), ZoneInfo("UTC"))
            earlier = replace(bill, day=day, at=end, period_start=start, period_end=end, actual_usd=value,
                              usage_types=(), provisional=True)
            _ = (book.directory / "charges" / (day + ".json")).write_text(json.dumps(ledger.charge_record(earlier)))
        actual = aws_costs.read_saved_costs(instant)
        self.assertEqual([(row["day"], row["state"], row["amount_usd"]) for row in actual["days"]], [
            ("2026-01-04", "estimate", "4.00"), ("2026-01-03", "final", "12.00"),
            ("2026-01-02", "billed", "0.00"), ("2026-01-01", "billed", "0.00"),
        ])

    def test_aws_cost_feed_splits_open_box_time_at_utc_midnight(self) -> None:
        midnight = int(datetime(2026, 1, 5, tzinfo=UTC).timestamp())
        instant = midnight + 1800
        book = write_aws_cost_fixture(self.root, instant=midnight - 1800)
        with book.transaction() as state:
            state.gate_reservations.append(ledger.GateReservation(
                "crossing-midnight", midnight - 1800, midnight + 3600, Decimal(8), Decimal(0),
                state=ledger.GateRunning("i-fixture"), instance_type="m7a.8xlarge", region="us-east-1"))
        actual = aws_costs.read_saved_costs(instant)
        today, yesterday = actual["days"][:2]
        for row in (today, yesterday):
            self.assertEqual(row["state"], "estimate")
            self.assertEqual(row["amount_usd"], "8.00")
            self.assertEqual(row["no_run_usd"], "4.00")
            self.assertEqual(row["runs_list"], [
                {"id": "crossing-midnight", "start": midnight - 1800, "kind": "gate", "job": None, "unit": None,
                 "seconds": 1800, "cost_usd": "4.00",
                 "region": "us-east-1", "zone": None, "machine": "m7a.8xlarge", "mode": "Spot"}
            ])

    def test_aws_cost_feed_names_the_user_zone_and_the_failed_read(self) -> None:
        book = write_aws_cost_fixture(self.root)
        read_at = int(datetime(2026, 1, 2, tzinfo=UTC).timestamp())

        def refused(at: int, code: str, operation: str) -> dict[str, object]:
            return {"time": at, "attempt": f"refused-{at}", "result": "fallback", "error_code": code,
                    "reason": f"aws: [ERROR]: An error occurred ({code}) when calling the {operation} "
                              + "operation: The security token included in the request is invalid."}

        with book.launch_history_path.open("a") as history:
            for row in (refused(read_at - 60, "Throttling", "GetCostAndUsage"),
                        refused(read_at + 60, "UnrecognizedClientException", "GetCostAndUsage"),
                        refused(read_at + 120, "Blocked", "RunInstances")):
                _ = history.write(json.dumps(row) + "\n")
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}):
            actual = aws_costs.read_saved_costs(AWS_COST_TIME)
        self.assertEqual((actual["zone"], actual["read_at"], actual["read_error"]),
                         ("America/Los_Angeles", read_at, "AWS refuses the key"))
        self.assertEqual(aws_costs.read_error([refused(read_at + 60, "Throttling", "GetCostAndUsage")], read_at),
                         "AWS refuses the read (Throttling)")
        self.assertIsNone(aws_costs.read_error([refused(read_at - 60, "Throttling", "GetCostAndUsage")], read_at))

    def test_aws_cost_feed_keeps_bill_days_in_utc(self) -> None:
        instant = int(datetime(2026, 2, 1, 2, tzinfo=UTC).timestamp())
        _ = write_aws_cost_fixture(self.root, instant=instant)
        text_setting(self.root / "aws_target.conf", "accounting_zone", "America/New_York")
        actual = aws_costs.read_saved_costs(instant)
        self.assertEqual([(row["day"], row["state"], row["amount_usd"]) for row in actual["days"]], [
            ("2026-02-01", "estimate", "4.00"), ("2026-01-31", "final", "12.00"),
        ])

    def test_the_json_boundary_preserves_named_missing_states(self) -> None:
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(captured_reading())):
            payload = memory_feed.reading()
        payload["cap"] = admission_records.CapUnavailable(())
        page = cast(dict[str, object], json.loads(memory_feed.page_json(payload)))
        self.assertEqual(page["cap"], {"kind": "CapUnavailable", "runs": []})
        self.assertIn("PressureNotRecorded", json.dumps(page["psi"]))


class PageTextTests(unittest.TestCase):
    def test_the_page_and_its_fixtures_use_only_configured_job_names(self) -> None:
        # bob, 2026-11-23 12:59: job names come from config/build_steps.conf (names.gate=gate), so no file shows the retired name.
        retired = " ".join(("merge", "gate"))
        folder = Path(__file__).resolve().parent
        for name in ("memory_live.html", "builds_page_real_rows.json", "memory_page_proof.mjs",
                     "test_memory_feed.py", "test_memory_hover.py", "test_queue_view.py"):
            with self.subTest(file=name):
                self.assertNotIn(retired, (folder / name).read_text(encoding="utf-8").lower())


class BuildsPageProofTests(unittest.TestCase):
    def test_only_a_nightly_lease_selects_all_page_proof_views(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            checkout.mkdir()
            lease = build_folders.lease_path(checkout)
            self.assertFalse(needs_all_page_proof_views(checkout))
            _ = lease.write_text(json.dumps({"work": "test-all"}), encoding="utf-8")
            self.assertFalse(needs_all_page_proof_views(checkout))
            _ = lease.write_text(json.dumps({"work": "test-nightly"}), encoding="utf-8")
            self.assertTrue(needs_all_page_proof_views(checkout))

    def run_page_proof(self, *, gate: bool, mode: str | None = None) -> tuple[Path, dict[str, object]]:
        source = Path(__file__).resolve()
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        proof = source.with_name("memory_page_proof.mjs")
        # Chrome needs a short path for its Unix sockets.
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="builds-page-proof-", dir="/tmp")))
        command = [executable, str(proof), *(["--gate"] if gate else []), str(directory)]
        if mode is not None:
            command.append(mode)
        environment = {**os.environ, "PYTHON": sys.executable}
        for setting in ("MEMORY_PAGE_SHOT", "SWAP_BEFORE_PAGE", "STEADY_BASELINE"):
            _ = environment.pop(setting, None)
        write_private_build_roots(directory / "build_folders.conf", directory, FIXTURE_MACHINE)
        result = run_process_group(command,
            env={**environment, "CONF_DIR": str(directory), "TMPDIR": str(directory)},
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        verdict = cast(dict[str, object], json.loads(result.stdout.splitlines()[-1]))
        self.assertIs(verdict.get("pass"), True)
        return directory, verdict

    def test_builds_page_renders_the_queue_and_tooltips_in_chrome(self) -> None:
        all_views = needs_all_page_proof_views(Path(__file__).resolve().parents[2])
        directory, verdict = self.run_page_proof(gate=not all_views)
        self.assertEqual(verdict.get("views"), 6 if all_views else 1)
        self.assertEqual(list(directory.glob("*.png")), [])
        for report in ("proof.json", "steady-refresh.json"):
            self.assertFalse((directory / report).exists())

    def test_gate_flag_keeps_the_queue_rows_proof_mode(self) -> None:
        directory, verdict = self.run_page_proof(gate=True, mode="--queue-rows")
        self.assertIn("rows", verdict)
        self.assertTrue((directory / "after-edge.png").is_file())


def machine_icons(row: str) -> list[tuple[str, str]]:
    """Return the state and name of each machine in the Machine cell of one rendered table row.

    An icon has the state "run" or "dim" and its name as its accessible name. An unknown machine is text, with no state.
    """
    cell = re.search(r'<td class="machine">(.*?)</td>', row, flags=re.S)
    assert cell is not None
    machines = cast(list[tuple[str, str, str]], re.findall(
        r'<span class="machine(?: machine-icon (run|dim))?" data-machine="([^"]+)"(?:[^>]* role="img" aria-label="([^"]+)")?>',
        cell.group(1)))
    assert all(label == (name if state else "") for state, name, label in machines), machines
    return [(state, name) for state, name, _ in machines]


def write_user_slices(user: Path, *, agents: bool = True, guarantee: str = "2147483648") -> None:
    """Write private session.slice and app-agents.slice readings: 1.5 GiB desktop, 6 GiB agents with 1 GiB inactive cache."""
    gib = admission_records.GIB
    (user / "session.slice").mkdir(parents=True, exist_ok=True)
    _ = (user / "session.slice" / "memory.min").write_text(f"{guarantee}\n")
    _ = (user / "session.slice" / "memory.current").write_text(f"{gib * 3 // 2}\n")
    _ = (user / "session.slice" / "memory.stat").write_text(f"anon {gib * 3 // 2}\nshmem 0\ninactive_file 0\n")
    if agents:
        agents_slice = user / "app.slice" / "app-agents.slice"
        agents_slice.mkdir(parents=True, exist_ok=True)
        _ = (agents_slice / "memory.current").write_text(f"{6 * gib}\n")
        _ = (agents_slice / "memory.stat").write_text(f"anon {5 * gib}\nshmem 0\ninactive_file {gib}\n")
        _ = (agents_slice / "memory.swap.current").write_text(f"{4 * gib}\n")


def write_browser_fixture(folder: Path) -> None:
    """Publish private feed readings for the browser proof."""
    fixture = MemoryFeedTests()
    fixture.setUp()
    _ = cast(object, fixture.enterContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"})))
    write_user_slices(fixture.root / "user")
    pressure_missing = memory_feed.PressureNotRecorded("Pressure is unreadable: /private/proc/pressure/memory")
    _ = cast(object, fixture.enterContext(mock.patch.object(memory_feed, "read_stalls",
        return_value={"some": pressure_missing, "full": pressure_missing})))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        configuration = replace(memory_feed.read_configuration(), output_folder=folder)
        memory_feed.publish_page(configuration)
        base = reservation_reading()
        inputs = base.explanation.decided_from
        live = leftover_step(30)
        memory = live.measured
        assert isinstance(memory, admission_records.UnownedSliceMemory)
        activity = memory.activity
        assert isinstance(activity, ScopeProcesses)
        live = replace(live, pid=70, measured=replace(memory,
            activity=replace(activity, run=LiveBuildRun(70, "live", 199_000))))
        compiler = replace(live, pid=80, measured=admission_records.CompilerServiceMemory(admission_records.GIB, pid=80))
        inputs = replace(inputs, now=200_000, running=(*inputs.running, leftover_step(45,
            members=(ScopeProcess(45, "python3"), ScopeProcess(46, "helper"))),
            leftover_step(19 * 60, small=True), leftover_step(2 * 24 * 60, small=True), live, compiler))
        base = gate.queue_read_from(inputs, admission_policy_fixture())
        readings: dict[str, admission_records.QueueRead] = {"good": base}
        for name, available, swapped in (("head_memory", 8, 0), ("head_shortfall", 2, 0), ("head_swap_shortfall", 4, 6)):
            held = gate.queue_read_from(replace(inputs, pressure=admission_records.NoPressureHold(),
                host=admission_records.HostMemory(available * admission_records.GIB, 60 * admission_records.GIB,
                    8 * admission_records.GIB, (8 - swapped) * admission_records.GIB,
                    swap_in_window_bytes=swapped * admission_records.GIB)),
                admission_policy_fixture())
            held = with_wait_reason(held, admission_records.NeedsMemory())
            held = replace(held, view=replace(held.view,
                waiting=(replace(held.view.waiting[0], need=20 * admission_records.GIB),)))
            held_room = held.explanation.room
            assert isinstance(held_room, admission_records.MemoryRoom)
            check = admission_records.RoomCheck(20 * admission_records.GIB, held_room.signed_room, False,
                                               basis=admission_records.DoesNotFit())
            head = admission_records.HeadExplanation(held.view.waiting[0], admission_records.NeedSourceNotRecorded(),
                admission_records.AdmissionDecision("hold", 0, 0, 0, 0), check,
                admission_records.FreeCheck(held_room.available, check.need, 0, 0, 0, False), False,
                cause=admission_records.NeedsMemory())
            readings[name] = replace(held, explanation=replace(held.explanation, head=head))
        for name, reason in (
            ("pressure", admission_records.PressureUnavailable("cannot read /private/proc/pressure/memory")),
            ("spacing", admission_records.StartSpacingWait(110)),
            ("cargo_folder", placement.CargoFolderWait()),
            ("head", admission_records.BehindHead("first", 1, "head-session")),
        ):
            readings[name] = with_wait_reason(base, reason)
        _ = (fixture.root / "cloud_gate.conf").write_text(
            "[cloud_gate]\n# range: 5e-324..\nlog_push_s=5\n")
        cloud = cloud_reading(with_wait_reason(base, admission_records.StartSpacingWait(110)))
        readings["cloud"] = cloud
        first = cloud.cloud[0]
        assert isinstance(first.progress, cloud_progress.CloudProgress)
        for name, stage, region, zone in (
            ("cloud_stage", "tools ready", "", ""),
            ("cloud_region", "tests start", "eu-south-2", "eu-south-2a"),
            ("cloud_zone", "", "eu-south-2", "us-east-1b"),
            ("cloud_empty", "", "", ""),
        ):
            readings[name] = replace(cloud, cloud=(replace(first, progress=replace(
                first.progress, stage=stage, region=region, zone=zone)), *cloud.cloud[1:]))
        readings["cloud_zero"] = replace(cloud, cloud=(replace(first,
            progress=replace(first.progress, updated_at=0.0, failed_modules=123456),
            counter=progress_profile.ProgressCounter("modules", 123456, 999999)), *cloud.cloud[1:]))
        room = base.explanation.room
        assert isinstance(room, admission_records.MemoryRoom)
        missing = replace(room.runs[0].memory, use=admission_records.RunUseUnavailable("private memory.stat is missing"))
        for name, cap in (("old_cap", admission_records.CapUnavailable(())), ("missing_cap", admission_records.CapUnavailable((missing,)))):
            readings[name] = replace(base, explanation=replace(base.explanation, room=replace(room, cap=cap)))
        missing_room = replace(room, cap=admission_records.CapUnavailable((missing,)),
            runs=(replace(room.runs[0], memory=missing),))
        readings["missing_use"] = replace(base, explanation=replace(base.explanation, room=missing_room))
        pages: dict[str, object] = {}
        pages["caller_selection"] = json.loads(memory_feed.page_json(fixture.caller_selection_page()))
        reports: dict[str, memory_feed.ReportFile] = {}
        names = read_step_names()
        for version in ("before", "after"):
            named_pages: list[memory_feed.MemoryPage] = []
            # The stored caller names before and after the owner names: a unit, then a showrunner.
            for step, prefix in (("test-affected", "panes"), ("gate", "showrunner")):
                name = names.shown(step) if version == "before" else f"{prefix} {names.shown(step)}"
                named_pages.append(fixture.cloud_owner_page(step, identity=wire.Identity("", name, step, "")))
            comparison = named_pages[0]
            comparison["walk"] = [row for page in named_pages for row in page["walk"] if row.get("kind") == "cloud"]
            comparison["waiters"] = [row for page in named_pages for row in page["waiters"] if row.get("kind") == "cloud"]
            pages[f"owner_names_{version}"] = json.loads(memory_feed.page_json(comparison))
        owner = unit_labels.make_labels("impl", unit_labels.UnderShowrunner("hana", "hana", ""),
                                        unit_labels.UnderDirector("research", "research", ""))
        research_director = staff.StaffRow(staff.StaffId("research"), "unit", "research", staff.StaffRoot(),
                                           current_session=staff.StaffId("research"))
        with mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((research_director,))):
            pages["step_owners"] = json.loads(memory_feed.page_json(fixture.owner_machine_page(owner)))
        cast(dict[str, object], pages["step_owners"])["swap_by"] = fixture.owner_swap_reading(owner).rows
        ci_page = fixture.owner_machine_page(owner, ci=True)
        ci_row, = [row for row in ci_page["walk"] if row.get("kind") == "mac-build"]
        ci_waiter = ci_page["waiters"][0]
        ci_waiter["name"] = ci_row["name"]
        ci_waiter["title"] = ci_row["title"]
        ci_waiter["showrunner"] = ""
        ci_waiter["unit"] = ""
        ci_waiter["detail"] = ""
        ci_waiter["ci_label"] = ci_row.get("ci_label", "")
        pages["step_ci"] = json.loads(memory_feed.page_json(ci_page))
        pages["step_unknown"] = json.loads(memory_feed.page_json(fixture.owner_machine_page(
            unit_labels.UnitLabels("unknown", "—", "owner not recorded"))))
        verify_page = fixture.verify_call_page()
        builder = staff.StaffRow(staff.StaffId("builder"), "unit", "builder", staff.StaffRoot(),
                                 current_session=staff.StaffId("builder"))
        builds = staff.StaffRow(staff.StaffId("builds"), "showrunner", "builds", staff.StaffRoot(),
                                current_session=staff.StaffId("builds"))
        hana = staff.StaffRow(staff.StaffId("hana"), "showrunner", "hana", staff.StaffRoot(),
                            current_session=staff.StaffId("hana"))
        units = tuple(staff.StaffRow(staff.StaffId(name), "unit", name, staff.ReportsTo(builds.id),
                                     current_session=staff.StaffId(name))
                      for name in ("queue", "natest", "admin-console"))
        verify_page["staff_names"] = staff_names.StaffNames(staff.StaffSnapshot((builder, builds, hana, *units))).registry()
        verified_waiter, = [row for row in verify_page["waiters"] if "verify" in row]
        builder_unit = memory_feed.StaffUnitName(builder.name)
        verified_waiter["unit"] = builder_unit
        verified_waiter["title"]["unit"] = builder_unit
        verified_waiter["detail"] = builder.name
        verify_page["walk"].extend(row for row in fixture.mac_verify_call_page()["walk"]
                                   if row.get("kind") == "mac-build" and "verify" in row)
        # Give Chrome fixed seven-digit PIDs after the admission checks use the real processes.
        for pid, row in enumerate((*verify_page["walk"], *verify_page["waiters"]), start=4_000_100):
            if "kind" in row and row["kind"] == "mac-unavailable":
                continue
            if isinstance(row["pid"], int):
                row["pid"] = pid
        pages["verify_calls"] = json.loads(memory_feed.page_json(verify_page))
        pages["three_target_gate_wait"] = json.loads(memory_feed.page_json(
            fixture.gate_wait_page(placement.AllowedHost())))
        pages["macroute"] = json.loads(memory_feed.page_json(fixture.macroute_page()))
        pages["macroute_reason"] = json.loads(memory_feed.page_json(fixture.macroute_page(
            local_reason="it edits the merged checkout on natedev with mend and fmt, and the push goes from there")))
        for name, reading in readings.items():
            with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(reading)):
                pages[name] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        for name, units in (("queued_none", ()), ("queued_one", ("research",)),
                            ("queued_two", ("research", "cables"))):
            with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(queued_reading(base, units))):
                pages[name] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        readings["priority"] = with_queue_priority(base)
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(readings["priority"])):
            pages["priority_unmatched"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        labels = {base.view.running[0].session_id: unit_labels.UnitLabels("fixture", "fixture-production", "research")}
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(readings["priority"])),
              mock.patch.object(unit_labels, "unit_labels_for", return_value=labels)):
            pages["priority"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        _ = write_idle_config(fixture.root)
        _ = write_idle_alert(fixture.root / "mac-idle", base.explanation.now)
        live = write_live_mac_state(fixture.root / "mac-state", block=True)
        with mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(base)), \
             mock.patch.object(time, "time", return_value=base.explanation.now), \
             mock.patch.dict(os.environ, {"MAC_TEST_STATE_DIR": str(live)}):
            pages["mac_idle"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        (fixture.root / "mac-idle" / "alert.json").unlink()
        for name, report in (("empty_report", report_fixture(empty=True)),
                             ("missing_report", charts.IndexUnavailable("private index is missing"))):
            with (mock.patch.object(charts, "page_data", return_value=report),
                  mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(base))):
                pages[name] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
                reports[name] = memory_feed.report_file(memory_feed.report_reading(configuration.history_days))
        # The second build has a unit. The others have no staff owner.
        mac_builds = tuple(queue_view.MacBuild(pid, path, base.explanation.now - 120 + index,
            command, repository, queue_view.MacBuilding(), int(used * admission_records.GIB),
            queue_view.mac_command_jobs(command), progress_profile.ProgressCounter("build:1", done, 1000), unit)
            for index, (pid, path, command, repository, used, done, unit) in enumerate((
                (30081, "/private/tmp/trunk-p172-assembly-rows", "cargo build --release -p hana", "hana", 0.6, 604, "trunk"),
                (34606, "/Users/nate/.local/state/mac-test/mirror/hana", "cargo nextest run --test-threads 12", "hana", 0.3, 647, "verify"),
                (42846, "/Users/nate/.local/state/mac-test/mirror/cargo-liner", "cargo nextest run --jobs=6", "cargo-liner", 0.3, 983, "handler"))))
        mac_reading = queue_view.MacBuildReading(base.explanation.now - 5, mac_builds)
        from scripts.mac_test import claim_state
        from scripts.copies import copies
        mac_claim: claim_state.RemoteJobClaim = {"claim_id": "fixture", "episode": "fixture", "attempt": "fixture",
            "submitting_session": "claim-owner", "pid": 900, "proc_start": "start", "what": "nextest hana",
            "worktree": mac_builds[1].folder, "since": "2026-11-12T18:00:00+00:00", "remote_host": "mac", "repo_name": "hana"}
        claim = claim_state.HeldClaim(claim_state.RepositoryScope("hana"), fixture.root / "claim.json", mac_claim)
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(base)),
              mock.patch.object(queue_view, "read_mac_builds", return_value=mac_reading),
              mock.patch.object(target_availability, "mac_claims", return_value=(claim,)),
              mock.patch.object(copies, "held_remote_mirrors", return_value=()),
              mock.patch.object(unit_labels, "unit_labels_for", return_value={"claim-owner": unit_labels.UnitLabels("worker", "hana", "trunk")})):
            pages["mac_three"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(base)),
              mock.patch.object(queue_view, "read_mac_builds", return_value=queue_view.MacBuildReading(
                  base.explanation.now - 540, mac_builds, "awk: private raw error"))):
            pages["mac_failed"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        call_id = "v20261115T234230-2719419-21122"
        index_file = index.index_path()
        index_file.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(index_file)) as database, database:
            index.create_schema(database)
            index.add_call(database, {"id": call_id, "tool": "verify.sh", "worktree": "/fixture/fps"}, "verify-fixture.jsonl")
        placed = queue_view.MacBuild(710001, "/Mac/fps", base.explanation.now - 600,
            "cargo nextest run -p fps", "hana", queue_view.MacTesting(710002),
            int(2.1 * admission_records.GIB), gate_label=queue_view.MacGateLabel.PLACEMENT,
            gate_placement=f"{call_id}-step-1/24e0cf9c4113402abff80a3dfa0edcdb2")
        with (mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(base)),
              mock.patch.object(queue_view, "read_mac_builds", return_value=queue_view.MacBuildReading(
                  base.explanation.now, (placed,)))):
            pages["macroute_running"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        staff_members = write_outside_queue_fixture(fixture.root)
        with (mock.patch.dict(os.environ, outside_queue_fixture_groups(fixture.root)),
              mock.patch.object(staff, "snapshot", return_value=staff_members),
              mock.patch.object(queue_snapshot, "read_view_snapshot", return_value=fixture_published_view(base))):
            pages["outside_queue"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        # The proof serves each reading's report as report.json and shows it with its reading.
        default_report = memory_feed.report_file(memory_feed.report_reading(configuration.history_days))
        for name, page in pages.items():
            report = reports.get(name, default_report)
            cast(dict[str, object], page).update(report_version=report.version, report=cast(object, json.loads(report.text)))
        _ = (folder / "fixtures.json").write_text(json.dumps(pages))
    finally:
        _ = fixture.doCleanups()
