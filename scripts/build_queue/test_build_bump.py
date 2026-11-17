"""Build priority controls use one admission reading and the caller identity."""
from __future__ import annotations

from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue import admission_client, admission_protocol as wire, admission_records as records, build_bump
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
from unittest import mock
from typing import Protocol, cast, override
from scripts.build_queue.queue_fixture import NOW, QueueFixture, write_memory_config
from scripts.buildlog.mac_idle_test_support import NotificationGuardedTestCase
from scripts.lint import memory_admit
from scripts.message import unit_labels
from scripts.message.unit_labels import UnitLabels

GIB = records.GIB

class LabelReadMock(Protocol):
    return_value: dict[str, UnitLabels]

class QueueTests(NotificationGuardedTestCase):
    fixture: QueueFixture = cast(QueueFixture, cast(object, None))
    root: Path = Path()
    labels_read: LabelReadMock = cast(LabelReadMock, cast(object, None))

    @override
    def setUp(self) -> None:
        super().setUp()
        self.fixture = self.enterContext(QueueFixture(captured=False))
        self.root = self.fixture.root
        _ = self.enterContext(mock.patch.object(memory_admit, "queue_read", side_effect=self.fixture.read))
        self.labels_read = cast(LabelReadMock, self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", return_value={})))
        config = self.root / "build_admission.conf"
        write_memory_config(config)
        self.enterContext(mock.patch.dict(os.environ, {"COLUMNS": "100", "CONF_DIR": str(config.parent)}))
        self.enterContext(mock.patch.dict(os.environ, {"MAC_IDLE_STATE_DIR": str(self.root / "mac-idle")}))
        _ = self.enterContext(mock.patch.object(os, "cpu_count", return_value=17))

    def test_bare_bump_prints_usage_and_the_builds_page_link(self) -> None:
        from scripts.build_queue import memory_feed

        for arguments in ((), ("--help",)):
            with self.subTest(arguments=arguments), mock.patch("sys.argv", ["build_bump.py", *arguments]), \
                 mock.patch.object(memory_feed, "page_url", return_value="https://fixture/builds"), \
                 mock.patch.object(memory_admit, "queue_read") as read, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(build_bump.main(), 0)
            self.assertEqual(output.getvalue(),
                "Usage: /build:bump <position|session|folder> | clear [unit]\nhttps://fixture/builds\n")
            read.assert_not_called()

    def test_user_bump_records_the_verified_actor(self) -> None:
        _ = self.fixture.waiter(200, "worker-id", "worker")
        admission = self.private_mutation_service()
        labels = {"worker-id": UnitLabels("worker", unit="berth")}
        self.labels_read.return_value = labels
        with mock.patch.object(memory_admit, "priority_labels", return_value=labels):
            self.assertEqual(self.bump_output("1")[0], 0)
        priority = admission.server.priority
        assert isinstance(priority, records.QueuePriority)
        self.assertEqual(priority.entries[0]["set_by"], "user")

    def test_clear_one_unit_passes_the_unit_to_the_client(self) -> None:
        with mock.patch.object(records, "queue_command_actor", return_value="actor"), mock.patch.object(
                admission_client, "priority_clear", return_value=wire.Ok()) as clear, mock.patch(
                "sys.argv", ["build_bump.py", "clear", "demo"]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(build_bump.main(), 0)
        clear.assert_called_once_with("actor", "demo")
        self.assertEqual(output.getvalue(), "queue priority cleared\n")

    def test_clear_unknown_unit_distinguishes_refusal_from_unavailable_service(self) -> None:
        cases = (
            (wire.Refused("unknown unit missing"), 3, "build queue: unknown unit missing\n", ""),
            (wire.AdmissionUnavailable("no socket"), 2, "",
             "build queue: admission service unavailable: no socket\n"),
        )
        for result, expected_status, expected_output, expected_errors in cases:
            output, errors = io.StringIO(), io.StringIO()
            with self.subTest(result=result), mock.patch.object(
                    records, "queue_command_actor", return_value="actor"), mock.patch.object(
                    admission_client, "priority_clear", side_effect=(
                        result if isinstance(result, wire.AdmissionUnavailable) else None),
                    return_value=result) as clear, mock.patch(
                    "sys.argv", ["build_bump.py", "clear", "missing"]), \
                    redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(build_bump.main(), expected_status)
            clear.assert_called_once_with("actor", "missing")
            self.assertEqual(output.getvalue(), expected_output)
            self.assertEqual(errors.getvalue(), expected_errors)

    def test_repeat_bump_prints_the_same_priority_place(self) -> None:
        _ = self.fixture.waiter(100, "head-id", "head")
        _ = self.fixture.waiter(200, "worker-id", "worker")
        admission = self.private_mutation_service()
        labels = {"head-id": UnitLabels("head", unit="demo"),
                  "worker-id": UnitLabels("worker", unit="cables")}
        self.labels_read.return_value = labels
        with mock.patch.object(memory_admit, "priority_labels", return_value=labels):
            self.assertIn("priority #1", self.bump_output("1")[1])
            self.assertIn("priority #2", self.bump_output("2")[1])
            self.assertIn("priority #1", self.bump_output("1")[1])
        priority = admission.server.priority
        assert isinstance(priority, records.QueuePriority)
        self.assertEqual([entry["unit"] for entry in priority.entries], ["demo", "cables"])

    def test_bump_passes_a_dash_unit_as_one_argparse_value(self) -> None:
        with mock.patch.object(records, "queue_command_actor", return_value="showrunner"), \
             mock.patch.object(build_bump, "bump", return_value=0) as run:
            self.assertEqual(self.bump_output("-x"), (0, "", ""))
        run.assert_called_once_with("-x", "showrunner")

    def test_bump_sets_the_units_priority_and_reports_its_resolved_place(self) -> None:
        first = self.fixture.waiter(100, "head-id", "head")
        second = self.fixture.waiter(200, "worker-id", "worker")
        before = (first.read_bytes(), second.read_bytes())
        admission = self.private_mutation_service()
        with mock.patch.object(memory_admit, "priority_labels", return_value={"worker-id": UnitLabels("worker", unit="berth")}):
            self.labels_read.return_value = {"worker-id": UnitLabels("worker", unit="berth")}
            status, output, errors = self.bump_output("2")
            self.assertEqual([step.pid for step in memory_admit.queue_view().waiting], [200, 100])
        self.assertEqual((status, output, errors), (0, "queue priority set for berth (step #2): first step now #1; priority #1\n", ""))
        self.assertEqual((first.read_bytes(), second.read_bytes()), before)
        priority = admission.server.priority
        self.assertNotIsInstance(priority, records.NoQueuePriority)
        if not isinstance(priority, records.NoQueuePriority):
            self.assertEqual((priority.entries[0]["unit"], priority.entries[0]["why"]), ("berth", "queue bump"))

    def test_unmatched_bump_selector_refuses_without_writing_priority(self) -> None:
        admission = self.private_mutation_service()
        with mock.patch.object(admission_client, "priority_set", side_effect=AssertionError("unmatched priority write")):
            status, output, errors = self.bump_output("berth")
        self.assertEqual((status, output, errors), (3, "build queue: no waiting step matches berth\n", ""))
        self.assertIsInstance(admission.server.priority, records.NoQueuePriority)

    def test_bump_requires_exactly_one_selector_without_running_priority(self) -> None:
        for selectors in (("berth", "other"), ("two words",)):
            with self.subTest(selectors=selectors), \
                 mock.patch.object(admission_client, "priority_set", side_effect=AssertionError("usage ran priority")):
                self.assertEqual(self.bump_output(*selectors),
                    (3, "build bump: use /build:bump <position|session|folder>\n", ""))

    def test_bump_selectors_send_the_selected_rows_real_unit_and_actor(self) -> None:
        _ = self.fixture.waiter(100, "head-id", "head")
        _ = self.fixture.waiter(200, "worker-id", "worker")
        self.labels_read.return_value = {"worker-id": UnitLabels("worker", unit="actual-unit")}
        for selector in ("2", "worker-id", "worker", "/tmp/tree-200", "tree-200"):
            with self.subTest(selector=selector), \
                 mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "actor-id"}), \
                 mock.patch.object(admission_client, "priority_set", return_value=wire.Ok()) as mutation:
                self.assertEqual(self.bump_output(selector),
                    (0, "queue priority set for actual-unit (step #2): first step now #2\n", ""))
                mutation.assert_called_once_with("actual-unit", "queue bump", "actor-id")

    def test_bump_row_without_a_unit_refuses_without_writing_priority(self) -> None:
        _ = self.fixture.waiter(100, "head-id", "head")
        self.labels_read.return_value = {}
        with mock.patch.object(admission_client, "priority_set", side_effect=AssertionError("unitless priority write")):
            self.assertEqual(self.bump_output("1"), (3, "build queue: waiting step #1 has no unit\n", ""))

    def test_unowned_row_refuses_a_bump_without_writing_priority(self) -> None:
        _ = self.fixture.waiter(100, "", "")
        self.labels_read.return_value = {}
        with mock.patch.object(admission_client, "priority_set", side_effect=AssertionError("unowned priority write")):
            self.assertEqual(self.bump_output("1"), (3, "build queue: waiting step #1 has no unit\n", ""))

    def test_clear_sends_an_untrusted_actor_hint(self) -> None:
        for arguments in (("clear",),):
            for claude, codex, actor in (("ordinary-session", "codex-session", "ordinary-session"),
                                          ("", "codex-session", "codex-session"), ("", "", "user")):
                with self.subTest(arguments=arguments, actor=actor), \
                     mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": claude, "CODEX_THREAD_ID": codex}), \
                     mock.patch.object(admission_client, "priority_clear", return_value=wire.Ok()) as clear, \
                     mock.patch("sys.argv", ["build_bump.py", *arguments]), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(build_bump.main(), 0)
                    clear.assert_called_once_with(actor)

    def private_mutation_service(self) -> AdmissionFixture:
        files = {path: path.read_bytes() for path in self.fixture.directory.glob("*.waiting")}
        admission = self.enterContext(AdmissionFixture(root=self.root, log_dir=self.root / "buildlog"))
        for path, contents in files.items():
            _ = path.write_bytes(contents)
        admission.clock.epoch = NOW
        admission.free(0)
        admission.hold_starts()
        for path in self.fixture.directory.glob("*.waiting"):
            row = memory_admit.read_waiter(path)
            owner = admission.fixture_owner(row["pid"], row["start_time"])
            request = admission.request(owner=owner, need=row["need"], since=row["since"])
            request = replace(request, identity=wire.Identity(row.get("session_id", ""),
                row.get("session_name", ""), row.get("step", ""), row.get("folder", "")))
            _ = admission.server.handle_request(request)
        _ = self.enterContext(admission.serving())
        _ = self.enterContext(mock.patch.object(memory_admit, "queue_read", side_effect=admission_client.view))
        return admission

    def bump_output(self, *units: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["build_bump.py", *units]), \
             redirect_stdout(output), redirect_stderr(errors):
            status = build_bump.main()
        return status, output.getvalue(), errors.getvalue()


