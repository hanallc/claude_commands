"""Build priority controls use one admission reading and the caller identity."""
from __future__ import annotations

from scripts.build_queue.admission_test_support import AdmissionFixture, Exchange, ScriptedTransport, TimedChunk
from scripts.build_queue import admission_client, admission_protocol as wire, admission_records as records, build_queue
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock
from typing import Protocol, cast, override
from scripts.build_queue.queue_fixture import NOW, QueueFixture, write_memory_config
from scripts.build_queue.scope_activity import LiveBuildRun
from scripts.buildlog.mac_idle_test_support import NotificationGuardedTestCase
from scripts.buildlog import record as buildlog_record
from scripts.lint import memory_admit
from scripts.production import staff
from scripts.message import unit_labels
from scripts.message.unit_labels import UnitLabels

GIB = records.GIB
WORDS = "/build:bump 2"
# The bumps in these tests come from one session.
BUMP_SESSION = {"CLAUDE_CODE_SESSION_ID": "bump-session", "CODEX_THREAD_ID": ""}

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
        def fixture_read(_wait: admission_client.QueueReadWait = "page read") -> records.QueueRead:
            return self.fixture.read()

        _ = self.enterContext(mock.patch.object(memory_admit, "queue_read", side_effect=fixture_read))
        self.labels_read = cast(LabelReadMock, self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", return_value={})))
        config = self.root / "build_admission.conf"
        write_memory_config(config)
        self.enterContext(mock.patch.dict(os.environ, {"COLUMNS": "100", "CONF_DIR": str(config.parent)}))
        self.enterContext(mock.patch.dict(os.environ, {"MAC_IDLE_STATE_DIR": str(self.root / "mac-idle")}))
        self.enterContext(mock.patch.dict(os.environ, BUMP_SESSION))
        _ = self.enterContext(mock.patch.object(os, "cpu_count", return_value=17))

    def test_bare_bump_prints_usage_and_the_builds_page_link(self) -> None:
        from scripts.build_queue import memory_feed

        for arguments in ((), ("--help",)):
            with self.subTest(arguments=arguments), mock.patch("sys.argv", ["build_queue.py", *arguments]), \
                 mock.patch.object(memory_feed, "page_url", return_value="https://fixture/builds"), \
                 mock.patch.object(memory_admit, "queue_read") as read, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(build_queue.main(), 0)
            self.assertEqual(output.getvalue(),
                "Usage: /build:bump [--run] <position|session|folder> | clear [unit]\nhttps://fixture/builds\n")
            read.assert_not_called()

    def test_bump_from_any_session_records_the_actor(self) -> None:
        _ = self.fixture.waiter(100, "head-id", "head")
        _ = self.fixture.waiter(200, "worker-id", "worker")
        admission = self.private_mutation_service()
        labels = {"worker-id": UnitLabels("worker", unit="berth")}
        self.labels_read.return_value = labels
        for claude, codex, actor in (("ordinary-session", "codex-session", "ordinary-session"),
                                      ("", "codex-session", "codex-session"), ("", "", "user")):
            with self.subTest(actor=actor), \
                 mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": claude, "CODEX_THREAD_ID": codex}), \
                 mock.patch.object(memory_admit, "priority_labels", return_value=labels):
                self.assertEqual(self.bump_output("2", "--words", WORDS)[0], 0)
                priority = admission.server.priority
                self.assertNotIsInstance(priority, records.NoQueuePriority)
                if not isinstance(priority, records.NoQueuePriority):
                    self.assertEqual(priority.entries[0]["set_by"], actor)
                events = [cast(dict[str, object], json.loads(line)) for path in (self.root / "buildlog").glob("*/*.jsonl")
                          for line in path.read_text().splitlines() if '"queue_priority"' in line]
                self.assertEqual(events[-1]["set_by"], actor)
                self.assertEqual(admission_client.priority_clear(actor, why=WORDS), wire.Ok())

    def test_clear_one_unit_passes_the_unit_to_the_client(self) -> None:
        with mock.patch.object(records, "queue_command_actor", return_value="actor"), mock.patch.object(
                admission_client, "priority_clear", return_value=wire.Ok()) as clear, mock.patch(
                "sys.argv", ["build_queue.py", "clear", "demo", "--words", WORDS]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(build_queue.main(), 0)
        clear.assert_called_once_with("actor", "demo", WORDS)
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
                    "sys.argv", ["build_queue.py", "clear", "missing", "--words", WORDS]), \
                    redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(build_queue.main(), expected_status)
            clear.assert_called_once_with("actor", "missing", WORDS)
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
            self.assertIn("priority #1", self.bump_output("1", "--words", WORDS)[1])
            self.assertIn("priority #2", self.bump_output("2", "--words", WORDS)[1])
            self.assertIn("priority #1", self.bump_output("1", "--words", WORDS)[1])
        priority = admission.server.priority
        assert isinstance(priority, records.QueuePriority)
        self.assertEqual([entry["unit"] for entry in priority.entries], ["demo", "cables"])

    def test_bump_passes_a_dash_unit_as_one_argparse_value(self) -> None:
        with mock.patch.object(records, "queue_command_actor", return_value="showrunner"), \
             mock.patch.object(build_queue, "bump", return_value=0) as run:
            self.assertEqual(self.bump_output("-x", "--words", WORDS), (0, "", ""))
        run.assert_called_once_with("-x", "showrunner", WORDS, build_queue.WholeUnitBump())

    def test_bump_sets_the_units_priority_and_reports_its_resolved_place(self) -> None:
        first = self.fixture.waiter(100, "head-id", "head")
        second = self.fixture.waiter(200, "worker-id", "worker")
        before = (first.read_bytes(), second.read_bytes())
        admission = self.private_mutation_service(run_pids=(200,))
        labels = {"head-id": UnitLabels("head", unit="demo"), "worker-id": UnitLabels("worker", unit="berth")}
        run_words = "/build:bump --run 2"
        # The rows run in order against one service; each row that sets an entry clears it after its checks.
        for arguments, expected, order, entry_kind in (
                (("--run", "1", "--words", "/build:bump --run 1"),
                 (3, "build queue: waiting step #1 has no run; bump its unit without --run\n", ""), [100, 200], ""),
                (("2", "--words", WORDS), (0, "queue priority set for berth (step #2): first step now #1; priority #1\n"
                  + f'queue priority: berth (bump-session: "{WORDS}")\n', ""), [200, 100], "UnitPriorityEntry"),
                (("--run", "2", "--words", run_words),
                 (0, "queue priority set for berth · nextest (one run) (step #2): first step now #1; priority #1\n"
                  + f'queue priority: berth · nextest (one run) (bump-session: "{run_words}")\n', ""),
                 [200, 100], "RunPriorityEntry")):
            with self.subTest(arguments=arguments), \
                 mock.patch.object(memory_admit, "priority_labels", return_value=labels):
                self.labels_read.return_value = labels
                self.assertEqual(self.bump_output(*arguments), expected)
                self.assertEqual([step.pid for step in memory_admit.queue_view().waiting], order)
                self.assertEqual((first.read_bytes(), second.read_bytes()), before)
                priority = admission.server.priority
                if not entry_kind:
                    self.assertIsInstance(priority, records.NoQueuePriority)
                    continue
                assert isinstance(priority, records.QueuePriority)
                entry, = priority.entries
                self.assertEqual((entry["unit"], entry["why"], entry.get("kind")), ("berth", arguments[-1], entry_kind))
                if records.is_run_entry(entry):
                    step = memory_admit.queue_view().waiting[0]
                    assert isinstance(step.run, LiveBuildRun)
                    self.assertEqual(entry["run"], wire.RunIdentity(step.run.pid, step.run.start_time))
                self.assertEqual(admission_client.priority_clear("bump-session", why=WORDS), wire.Ok())

    def test_bump_waits_while_a_live_service_answers_slowly(self) -> None:
        _ = self.fixture.waiter(100, "head-id", "head")
        _ = self.fixture.waiter(200, "worker-id", "worker")
        # Write the limits before the service runs; a write while it serves races its config read.
        write_memory_config(self.root / "build_admission.conf", query_timeout_seconds="0.5", silence_seconds="2")
        admission = self.private_mutation_service()
        labels = {"head-id": UnitLabels("head", unit="demo"), "worker-id": UnitLabels("worker", unit="berth")}
        self.labels_read.return_value = labels

        def answer(request: wire.Request) -> wire.Reply:
            with admission.loop_turn():
                reply, = admission.server.handle_request(request)
            return reply

        def slow_view() -> Exchange:
            # The live service sends its answer in pieces; the last piece comes after the page limit.
            return Exchange(TimedChunk(0.3, b" "), TimedChunk(0.3, b" "), answer=answer)

        transport = ScriptedTransport(admission.clock, slow_view(), slow_view())
        with mock.patch.object(admission_client, "CLOCK", admission.clock), \
             mock.patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket), \
             mock.patch.object(admission_client, "priority_set", return_value=wire.Ok()), \
             mock.patch.object(memory_admit, "priority_labels", return_value=labels):
            status, output, errors = self.bump_output("2", "--words", WORDS)
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(output, "queue priority set for berth (step #2): first step now #2\n")
        self.assertEqual([type(request) for request in transport.requests], [wire.ViewRequest, wire.ViewRequest])
        self.assertGreater(admission.clock.monotonic(), 1.2)

    def test_bump_stores_the_words_and_the_askers_staff_name(self) -> None:
        _ = self.fixture.waiter(100, "head-id", "head")
        _ = self.fixture.waiter(200, "worker-id", "worker")
        admission = self.private_mutation_service()
        labels = {"worker-id": UnitLabels("worker", unit="hana")}
        self.labels_read.return_value = labels
        merge = staff.StaffRow(staff.StaffId("natemerge-id"), "unit", "natemerge", staff.StaffRoot(),
                               current_session=staff.StaffId("merge-session"))
        words = "natemerge can bump at will"
        asker: records.Asker
        for session, members, name, asker in (("merge-session", (merge,), "natemerge", records.StaffAsker("natemerge")),
                                              ("lone-session", (), "lone-session", records.UnstaffedSession("lone-session"))):
            with self.subTest(name=name), \
                 mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session}), \
                 mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(members)), \
                 mock.patch.object(memory_admit, "priority_labels", return_value=labels):
                self.assertEqual(self.bump_output("2", "--words", words), (0,
                    "queue priority set for hana (step #2): first step now #1; priority #1\n"
                    + f'queue priority: hana ({name}: "{words}")\n', ""))
                priority = admission.server.priority
                assert isinstance(priority, records.QueuePriority)
                entry = priority.entries[0]
                self.assertEqual((entry["why"], entry["set_by"], records.entry_asker(entry)),
                                 (words, session, asker))
                self.assertEqual(admission_client.priority_clear(session, why=WORDS), wire.Ok())

    def test_words_are_required_except_in_a_plain_shell(self) -> None:
        _ = self.fixture.waiter(100, "head-id", "head")
        _ = self.fixture.waiter(200, "worker-id", "worker")
        admission = self.private_mutation_service()
        labels = {"worker-id": UnitLabels("worker", unit="hana")}
        self.labels_read.return_value = labels
        refusal = "build queue: --words is required: pass the user's words; the queue priority did not change\n"
        # The rows run in order: the plain-shell bump gives the session's clear an entry to keep.
        # The service also logs its refusal on stderr, so the rows compare the status and stdout.
        for session, arguments, expected in (("lone-session", ("2",), (3, refusal)),
                                             ("", ("2",), (0, "queue priority set for hana (step #2): first step now #1; "
                                              + 'priority #1\nqueue priority: hana (nate (plain shell): "typed by nate in a plain shell")\n')),
                                             ("lone-session", ("clear",), (3, refusal)),
                                             ("", ("clear",), (0, "queue priority cleared\n"))):
            with self.subTest(session=session, arguments=arguments), \
                 mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session}), \
                 mock.patch.object(memory_admit, "priority_labels", return_value=labels), \
                 mock.patch.object(buildlog_record, "queue_priority", wraps=buildlog_record.queue_priority) as logged:
                before = admission.server.priority
                self.assertEqual(self.bump_output(*arguments)[:2], expected)
                if expected[0] == 3:
                    self.assertEqual(admission.server.priority, before)
                    logged.assert_not_called()
                else:
                    self.assertEqual(logged.call_args.args[2:4], ("typed by nate in a plain shell", "user"))

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
                self.assertEqual(self.bump_output(selector, "--words", WORDS),
                    (0, "queue priority set for actual-unit (step #2): first step now #2\n", ""))
                mutation.assert_called_once_with("actual-unit", WORDS, "actor-id", wire.WHOLE_UNIT)

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

    def test_clear_from_any_session_sends_the_actor(self) -> None:
        for arguments in (("clear",),):
            for claude, codex, actor in (("ordinary-session", "codex-session", "ordinary-session"),
                                          ("", "codex-session", "codex-session"), ("", "", "user")):
                with self.subTest(arguments=arguments, actor=actor), \
                     mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": claude, "CODEX_THREAD_ID": codex}), \
                     mock.patch.object(admission_client, "priority_clear", return_value=wire.Ok()) as clear, \
                     mock.patch("sys.argv", ["build_queue.py", *arguments]), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(build_queue.main(), 0)
                    clear.assert_called_once_with(actor, "", wire.WORDS_NOT_GIVEN)

    def private_mutation_service(self, run_pids: tuple[int, ...] = ()) -> AdmissionFixture:
        """Serve the waiters; a waiter whose pid is in `run_pids` waits in its own run."""
        files = {path: path.read_bytes() for path in self.fixture.directory.glob("*.waiting")}
        admission = self.enterContext(AdmissionFixture(root=self.root, log_dir=self.root / "buildlog"))
        # The fixture drops the caller's session, so the bump session comes back after it.
        self.enterContext(mock.patch.dict(os.environ, BUMP_SESSION))
        for path, contents in files.items():
            _ = path.write_bytes(contents)
        admission.clock.epoch = NOW
        admission.free(0)
        admission.hold_starts()
        for path in self.fixture.directory.glob("*.waiting"):
            row = memory_admit.read_waiter(path)
            owner = admission.fixture_owner(row["pid"], row["start_time"])
            request = admission.request(owner=owner, need=row["need"], since=row["since"],
                                        run=admission.run_owner() if row["pid"] in run_pids else wire.NO_RUN)
            request = replace(request, identity=wire.Identity(row.get("session_id", ""),
                row.get("session_name", ""), row.get("step", ""), row.get("folder", "")))
            _ = admission.server.handle_request(request)
        _ = self.enterContext(admission.serving())
        _ = self.enterContext(mock.patch.object(memory_admit, "queue_read", side_effect=admission_client.view))
        return admission

    def bump_output(self, *units: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["build_queue.py", *units]), \
             redirect_stdout(output), redirect_stderr(errors):
            status = build_queue.main()
        return status, output.getvalue(), errors.getvalue()


class ServiceQueryFailureTests(unittest.TestCase):
    def test_bump_reads_fail_at_once_when_down_and_after_silence_when_silent(self) -> None:
        for silent in (False, True):
            with self.subTest(silent=silent), AdmissionFixture() as fixture:
                fixture.configure(query_timeout_seconds=0.5, silence_seconds=2)
                timeout = 2
                for arguments in (["4"], ["1"]):
                    exchange = (Exchange(TimedChunk(timeout / 4, b"{"), TimeoutError()) if silent
                                else Exchange(connect_error=FileNotFoundError()))
                    transport = ScriptedTransport(fixture.clock, exchange)
                    output, errors = io.StringIO(), io.StringIO()
                    started = fixture.clock.monotonic()
                    with mock.patch.object(admission_client, "CLOCK", fixture.clock), \
                         mock.patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket), \
                         mock.patch("time.sleep", side_effect=AssertionError("a query must not retry")), \
                         mock.patch("sys.argv", ["queue", *arguments]), \
                         redirect_stdout(output), redirect_stderr(errors):
                        status = build_queue.main()
                    self.assertEqual(fixture.clock.monotonic() - started, timeout if silent else 0)
                    self.assertEqual(len(transport.attempts), 1)
                    self.assertEqual(len(transport.requests), 1 if silent else 0)
                    self.assertEqual(status, 2)
                    self.assertEqual(output.getvalue(), "")
                    reason = "no answer in 2 s" if silent else "no socket"
                    self.assertEqual(errors.getvalue(),
                        f"build queue: admission service unavailable: {reason}\n")

    def test_queue_read_returns_the_private_services_exact_reading(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            reply = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reply, wire.ViewReply)
            with fixture.serving():
                self.assertEqual(memory_admit.queue_read(), reply.reading)


if __name__ == "__main__":
    _ = unittest.main()
