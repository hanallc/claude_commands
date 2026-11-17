"""Check the program memory charts and trace cache."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest import mock

from scripts.build_queue import admission_records, memory_trace, program_memory_feed
from scripts.build_queue.queue_fixture import fixture_reservation


TRACE_NOW = 10 * 86_400
TRACE_POLICY = memory_trace.MemoryTracePolicy(2, 64, 8_192, 2, 8, 8, 65_536)
TRACE_SHAPE = "a" * 64


class ProgramMemoryFeedTests(unittest.TestCase):
    root: Path = Path()
    reservations: program_memory_feed.ReservationsByOwner = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.reservations = {}

    def trace_store(self, policy: memory_trace.MemoryTracePolicy = TRACE_POLICY) -> memory_trace.TraceStore:
        return memory_trace.TraceStore(self.root, policy)

    def start_trace(self, store: memory_trace.TraceStore, trace_id: str, *, start: int = TRACE_NOW - 6,
                    reserve: int = 10) -> memory_trace.TraceWriter:
        return store.start(memory_trace.TraceHeader(trace_id, TRACE_SHAPE, start * 1000, store.policy.sample_interval_ms, reserve))

    def compile_trace_sample(self, at: int, used: int) -> memory_trace.CompileSample:
        return memory_trace.CompileSample(at, used, memory_trace.CargoProgress(1, 2), "hana")

    def seal_past_trace(self, store: memory_trace.TraceStore, trace_id: str, ended: int) -> None:
        writer = self.start_trace(store, trace_id, start=ended - 8, reserve=4)
        _ = writer.append(self.compile_trace_sample(0, 3))
        _ = writer.append(self.compile_trace_sample(2000, 8))
        _ = writer.append(memory_trace.TestStarted(4000, 6))
        _ = writer.append(memory_trace.TestSample(4000, 4))
        _ = writer.append(memory_trace.TestSample(6000, 6))
        _ = writer.seal(memory_trace.TraceEnd(ended * 1000, 8000, 100, memory_trace.WholeRun()))

    def trace_rows(self, *ids: str, aggregate: bool = False) -> list[tuple[str, float]]:
        rows: list[tuple[str, float]] = []
        for index, trace_id in enumerate(ids):
            pid = 101 + index
            started_at = TRACE_NOW - 8 - index
            reservation: admission_records.Reservation = {
                **fixture_reservation(pid, 10, step="nextest"),
                "repo": "hana", "reservation_id": trace_id, "scoped": True,
            }
            step = admission_records.RunningStep("", "fixture", "nextest", "/fixture", 10,
                admission_records.MeasuredMemory(8), pid=pid, started_at=started_at)
            owner = ids[0] if aggregate else trace_id
            entry = admission_records.ExplainedRunningStep(step, started_at, 10, 8, 10, owner=owner)
            self.reservations.setdefault(owner, []).append((reservation, entry))
            if not index or not aggregate:
                rows.append((owner, started_at))
        return rows

    def views(self, feed: program_memory_feed.ProgramMemoryFeed, rows: list[tuple[str, float]]
              ) -> dict[int, program_memory_feed.ProgramMemoryView]:
        return dict(feed.views(rows, self.reservations, TRACE_NOW, "fixture"))

    def test_program_memory_uses_recorded_reserves_and_sealed_cache(self) -> None:
        store = self.trace_store()
        writer = self.start_trace(store, "live-trace")
        _ = writer.append(self.compile_trace_sample(2000, 7))
        self.seal_past_trace(store, "past-trace", TRACE_NOW - 10)
        feed = program_memory_feed.ProgramMemoryFeed(store)
        rows = self.trace_rows("live-trace")
        with mock.patch.object(memory_trace, "read_trace", wraps=memory_trace.read_trace) as read:
            first = self.views(feed, rows).get(0)
            assert first is not None
            self.assertEqual(first["compile_reserve"], {"bytes": 10, "source": "live"})
            self.assertEqual(first["test_reserve"], {"bytes": 6, "source": "past"})
            self.assertEqual(first["live"]["test"]["state"], "not_started")
            past = first["past"]
            assert past is not None
            self.assertEqual(past["compile"]["above_half_reserve_ms"], 2000)
            self.assertEqual(past["test"]["above_half_reserve_ms"], 2000)
            self.assertEqual(past["compile"]["peak"], (2000, 8))
            self.assertEqual(past["hana_crate_bands"], ((0, 2000),))
            self.assertEqual(past["hana_compile_ms"], 2000)
            _ = writer.append(memory_trace.TestStarted(4000, memory_trace.TestReserveUnavailable()))
            _ = writer.append(memory_trace.TestSample(6000, 5))
            waiting = self.views(feed, rows).get(0)
            assert waiting is not None
            self.assertIsNone(waiting["test_reserve"])
            _ = writer.append(memory_trace.TestReserveObserved(8))
            second = self.views(feed, rows).get(0)
            assert second is not None
            self.assertEqual(second["test_reserve"], {"bytes": 8, "source": "live"})
            self.assertEqual(second["live"]["compile"]["state"], "complete")
            self.assertEqual(second["live"]["test"]["peak"], (2000, 5))
            self.assertEqual(read.call_count, 2)
            sealed, = (store.root / "finished" / TRACE_SHAPE).glob("*.jsonl")
            _ = sealed.write_bytes(b"invalid\n")
            after = self.views(feed, rows).get(0)
            assert after is not None
            self.assertIsNone(after["past"])
            self.assertEqual(read.call_count, 3)

    def test_program_memory_skips_aggregate_rows(self) -> None:
        store = self.trace_store()
        writer = self.start_trace(store, "live-trace")
        _ = writer.append(self.compile_trace_sample(2000, 7))
        rows = self.trace_rows("live-trace", "second", aggregate=True)
        self.assertEqual(self.views(program_memory_feed.ProgramMemoryFeed(store), rows), {})

    def test_program_memory_skips_stale_records_and_keeps_live_without_past(self) -> None:
        store = self.trace_store()
        rows = self.trace_rows("live-trace")
        feed = program_memory_feed.ProgramMemoryFeed(store)
        self.seal_past_trace(store, "old-trace", TRACE_NOW - (TRACE_POLICY.retention_days + 1) * 86_400)
        writer = self.start_trace(store, "live-trace", start=TRACE_NOW - 9)
        _ = writer.append(self.compile_trace_sample(0, 8))
        self.assertEqual(self.views(feed, rows), {})
        writer.path.unlink()
        writer = self.start_trace(store, "live-trace")
        _ = writer.append(self.compile_trace_sample(0, 7))
        current = self.views(feed, rows).get(0)
        assert current is not None
        self.assertEqual(current["live"]["compile"]["state"], "stopped")
        self.assertEqual(current["live"]["compile"]["peak"], (0, 7))
        self.assertIsNone(current["past"])
        self.assertIsNone(current["test_reserve"])
        foreign = self.start_trace(store, "other-trace")
        _ = foreign.append(self.compile_trace_sample(2000, 9))
        _ = foreign.path.replace(writer.path)
        self.assertEqual(self.views(feed, rows), {})

    def test_program_memory_reads_live_from_last_complete_line(self) -> None:
        store = self.trace_store()
        writer = self.start_trace(store, "live-trace")
        _ = writer.append(self.compile_trace_sample(2000, 7))
        feed = program_memory_feed.ProgramMemoryFeed(store)
        rows = self.trace_rows("live-trace")
        sample = memory_trace.encode_record(self.compile_trace_sample(4000, 8))
        for fragment, peak in ((sample[:4], (2000, 7)), (sample[4:], (4000, 8))):
            with writer.path.open("ab") as stream:
                _ = stream.write(fragment)
            with mock.patch.object(memory_trace, "read_trace", wraps=memory_trace.read_trace) as read:
                recorded = self.views(feed, rows).get(0)
                assert recorded is not None
                self.assertEqual(recorded["live"]["compile"]["peak"], peak)
                self.assertEqual(read.call_count, int(peak == (2000, 7)))

    def test_program_memory_enforces_exact_budget_in_page_order(self) -> None:
        store = self.trace_store()
        for trace_id in ("later", "earlier"):
            writer = self.start_trace(store, trace_id)
            _ = writer.append(self.compile_trace_sample(2000, 7))
        rows = self.trace_rows("later", "earlier")
        view = self.views(program_memory_feed.ProgramMemoryFeed(store), rows).get(1)
        assert view is not None
        size = len(json.dumps({"program_memory": view}).encode("utf-8"))
        for budget, expected in ((size, [1]), (size - 1, [])):
            with self.subTest(budget=budget):
                limited = self.trace_store(replace(TRACE_POLICY, feed_bytes_per_refresh=budget))
                page = self.views(program_memory_feed.ProgramMemoryFeed(limited), rows)
                self.assertEqual(list(page), expected)
