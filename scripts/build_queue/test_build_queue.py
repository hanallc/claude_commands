"""Queue reads never send priority changes."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest import mock
from typing import cast

from scripts.build_queue import admission_client, admission_protocol as wire, admission_records as records, admission_service as service, build_queue
from scripts.build_queue.admission_test_support import AdmissionFixture, Exchange, ScriptedTransport, TimedChunk
from scripts.build_queue.queue_fixture import QueueFixture
from scripts.lint import memory_admit
from scripts.message import unit_labels


class QueueReadTests(unittest.TestCase):
    def test_reads_and_clear_filter_send_no_mutation(self) -> None:
        with QueueFixture(captured=False) as fixture:
            _ = fixture.waiter(100, "startup-id", "startup")
            _ = fixture.waiter(200, "clear-id", "clear")
            before = {path: path.read_bytes() for path in fixture.directory.glob("*") if path.is_file()}
            for arguments, expected in (((), [100, 200]), (("startup",), [100]),
                                        (("2",), [200]), (("clear",), [200])):
                with self.subTest(arguments=arguments), \
                     mock.patch.object(memory_admit, "queue_read", side_effect=fixture.read), \
                     mock.patch.object(unit_labels, "unit_labels_for", return_value={}), \
                     mock.patch.object(admission_client, "priority_set", side_effect=AssertionError("read changed priority")), \
                     mock.patch.object(admission_client, "priority_clear", side_effect=AssertionError("read cleared priority")), \
                     mock.patch("sys.argv", ["build_queue.py", *arguments]), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(build_queue.main(), 0)
                    self.assertTrue(output.getvalue().startswith("["), output.getvalue())
                    rows = cast(list[dict[str, object]], json.loads(output.getvalue()))
                    self.assertEqual([row["pid"] for row in rows], expected)
                self.assertEqual({path: path.read_bytes() for path in fixture.directory.glob("*") if path.is_file()}, before)


class ServiceQueryFailureTests(unittest.TestCase):
    def test_views_use_one_query_deadline_for_missing_and_silent_services(self) -> None:
        for silent in (False, True):
            with self.subTest(silent=silent), AdmissionFixture() as fixture:
                timeout = records.read_memory_config().query_timeout_seconds
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
                    reason = "no answer in 0.5 s" if silent else "no socket"
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


class PrivateQueueReadTests(unittest.TestCase):
    def test_reader_preserves_private_priority_ledger_and_logs(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.hold_starts()
            fixture.free(0)
            request = fixture.request()
            _ = fixture.server.handle_request(request)
            fixture.server.priority = records.QueuePriority(({
                "unit": "demo", "why": "urgent", "set_at": fixture.clock.now(), "set_by": "user"},))
            fixture.server.persist()
            # Publish the pass before the reader requests it.
            _ = fixture.server.handle_request(wire.ViewRequest())
            before = service.read_state(fixture.state_dir)
            ledger = dict(fixture.server.ledger)
            priority = fixture.server.priority
            with fixture.serving():
                for arguments in ((), ("clear",)):
                    with mock.patch("sys.argv", ["build_queue.py", *arguments]), redirect_stdout(io.StringIO()):
                        self.assertEqual(build_queue.main(), 0)
            self.assertEqual(fixture.server.priority, priority)
            self.assertEqual(fixture.server.ledger, ledger)
            saved = service.read_state(fixture.state_dir)
            self.assertEqual(saved.priority, before.priority)
            self.assertEqual(saved.ledger, before.ledger)
            events = [line for path in fixture.state_dir.parent.glob("*/*.jsonl")
                      for line in path.read_text().splitlines() if '"queue_priority"' in line]
            self.assertEqual(events, [])
