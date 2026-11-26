"""Check the report of starts before a memory stop."""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import override
import unittest
from unittest import mock

from scripts.build_queue import loan_starts, memory_trace
from scripts.buildlog import memory, store


STOP = datetime(2026, 11, 25, 16, tzinfo=UTC)
GIB = memory.GIB
POLICY = memory_trace.MemoryTracePolicy(2, 10, 8192, 7, 8, 1024, 1024, 8)


class LoanStartsTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        temporary = self.enterContext(TemporaryDirectory())
        state = Path(temporary) / "admission"
        state.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"BUILDLOG_DIR": temporary, "USER_ZONE": "America/Los_Angeles"}))
        _ = self.enterContext(mock.patch.object(memory_trace, "read_policy", return_value=POLICY))
        at = STOP.timestamp()

        def admitted(identifier: str, offset: int, **fields: object) -> dict[str, object]:
            return {"kind": "admitted", "reservation_id": identifier, "at": at + offset,
                    "admitted_at": at + offset, "session_name": identifier, "repo": "demo",
                    "step": "lint", "need": 3 * GIB // 2, **fields}

        def ended(identifier: str, offset: int, *, written: int, peak: int) -> dict[str, object]:
            return {"kind": "ended", "reservation_id": identifier, "at": at + written, "ended_at": at + offset,
                    "peak": {"kind": "PeakSampled", "bytes": peak}}

        rows = (
            admitted("too-early", -86_401),
            admitted("boundary", -86_400),
            admitted("late-end", -300, room=2 * GIB, loan=0, fit="free"),
            admitted("ended-at-stop", -240),
            admitted("running", -180, room=-GIB // 4, loan=GIB // 8, fit="loan"),
            admitted("trace", -120, unit="builds"),
            admitted("long", -1800, at=at - 100),
            ended("trace", -30, written=-30, peak=8 * GIB),
            admitted("at-stop", 0),
            ended("ended-at-stop", 0, written=0, peak=5 * GIB),
            ended("late-end", -60, written=15, peak=7 * GIB // 2),
            ended("running", 60, written=60, peak=3 * GIB),
        )
        _ = (state / "queue_times.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        traces = memory_trace.TraceStore(state / "memory_traces", POLICY)
        shape = memory_trace.GeneralTraceShape("natedev", "git:demo", "lint", ("verify.sh", "lint"))
        started = int(at - 120) * 1000
        writer = traces.start(memory_trace.TraceHeader(
            "trace", memory_trace.shape_key(shape), started, POLICY.sample_interval_ms, 3 * GIB // 2, shape))
        _ = writer.append(memory_trace.CompileSample(
            0, 9 * GIB // 4, memory_trace.CargoProgressUnavailable(), memory_trace.CompilingCrateUnavailable()))
        _ = writer.seal(memory_trace.TraceEnd(started + 3000, 3000, 0, memory_trace.WholeRun()))

    def command(self, argv: list[str]) -> tuple[str, list[str], list[list[str]]]:
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(loan_starts.main(argv), 0)
        stop, header, *rows = output.getvalue().splitlines()
        return stop, re.split(r" {2,}", header), [re.split(r" {2,}", row) for row in rows]

    def test_default_shows_steps_running_at_the_earlyoom_stop(self) -> None:
        with (
            mock.patch.object(memory, "kernel_oom_kills", return_value=[
                (STOP - timedelta(minutes=1), "oom-kill: task=python3,pid=3")]),
            mock.patch.object(memory, "earlyoom_kills", return_value=[
                memory.EarlyoomKill(STOP, 2, "new"),
                memory.EarlyoomKill(STOP - timedelta(minutes=3), 1, "old")]),
        ):
            stop, header, rows = self.command([])
        self.assertEqual(stop, "Stop 2026-11-25 08:00:00 PST (earlyoom kill)")
        self.assertNotIn("Ended", header)
        self.assertEqual([row[1] for row in rows], ["boundary", "long", "running"])
        self.assertEqual(rows[1][0], "2026-11-25 07:30:00 PST")
        self.assertEqual(rows[1][4:], ["1.50", "not recorded", "not recorded", "not recorded", "not recorded", "none"])
        self.assertEqual(rows[2][4:], ["1.50", "-0.25", "0.12", "loan", "3.00", "ended"])

    def test_default_uses_the_latest_kernel_kill_in_the_journal(self) -> None:
        def journal_messages(args: list[str], start: datetime, end: datetime) -> list[tuple[datetime, str]]:
            self.assertEqual(end - start, timedelta(days=7))
            if args == ["_TRANSPORT=kernel"]:
                return [(STOP, "oom-kill: task=python3,pid=2"),
                        (STOP + timedelta(minutes=1), "python3 invoked oom-killer")]
            self.assertEqual(args, ["-u", "earlyoom"])
            return [(STOP - timedelta(minutes=1), 'sending SIGTERM to process 1 uid 1000 "python3"')]

        with (
            mock.patch.object(store, "host_name", return_value="natedev"),
            mock.patch.object(memory, "journal_messages", side_effect=journal_messages) as journal,
        ):
            stop, _, rows = self.command([])
        journal.assert_has_calls([
            mock.call(["_TRANSPORT=kernel"], mock.ANY, mock.ANY),
            mock.call(["-u", "earlyoom"], mock.ANY, mock.ANY),
        ])
        self.assertEqual(stop, "Stop 2026-11-25 08:00:00 PST (kernel OOM kill)")
        self.assertEqual([row[1] for row in rows], ["boundary", "long", "running"])

    def test_window_shows_end_times_and_prefers_the_trace_peak(self) -> None:
        with mock.patch.object(memory, "journal_messages", side_effect=AssertionError("read the journal")):
            stop, header, rows = self.command(["--stop", STOP.isoformat(), "--window-minutes", "5"])
        self.assertEqual(stop, "Stop 2026-11-25 08:00:00 PST (--stop)")
        self.assertEqual(header[4:], ["Need (GiB)", "Room (GiB)", "Loan (GiB)", "Fit",
                                     "Peak (GiB)", "Peak source", "Ended"])
        self.assertEqual([row[1] for row in rows], ["late-end", "ended-at-stop", "running", "builds"])
        self.assertEqual(rows[0][4:], ["1.50", "2.00", "0.00", "free", "3.50", "ended", "2026-11-25 07:59:00 PST"])
        self.assertEqual(rows[1][-1], "2026-11-25 08:00:00 PST")
        self.assertEqual(rows[2][-1], "running at stop")
        self.assertEqual(rows[3][-3:], ["2.25", "trace", "2026-11-25 07:59:30 PST"])
