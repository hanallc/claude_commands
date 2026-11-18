"""Test the memory trace boundary with private files and fixed times."""
from __future__ import annotations

from collections.abc import Buffer, Iterator
from dataclasses import replace
from functools import cached_property
from io import BytesIO, FileIO
import os
from pathlib import Path
import tempfile
from typing import override
import unittest
from unittest import mock

from scripts.build_queue import memory_trace as traces
from scripts.build_queue.admission_records import RequestedTestWidth, UnspecifiedTestWidth
from scripts.lib import conf
from scripts.lib.atomic_file import write_bytes_atomic


DAY_MS = 86_400_000
NOW_MS = 10 * DAY_MS
POLICY = traces.MemoryTracePolicy(2, 120, 8192, 2, 3, 3, 1024, final_replay_bytes=100000,
                                  scope_loss_grace_seconds=9, seal_retry_count=4,
                                  max_pending_commands=12, shutdown_timeout_seconds=3, prune_interval_seconds=7)
ARGV = ("cargo", "nextest", "run", "--bin", "hana", "--profile", "ci",
        "--features", "native", "--target", "target-a", "--package", "hana",
        "--test-threads", "8", "-E", "test(alpha)", "--", "--nocapture")
SHAPE = traces.HanaTraceShape("machine", "repo", ARGV, RequestedTestWidth(8))
KEY = traces.shape_key(SHAPE)
UNKNOWN_CRATE = traces.CompilingCrateUnavailable()
UNKNOWN_PROGRESS = traces.CargoProgressUnavailable()
UNKNOWN_RESERVE = traces.TestReserveUnavailable()
WHOLE_RUN = traces.WholeRun()


def compile_sample(at_ms: int, used: int, *, crate: str | traces.CompilingCrateUnavailable = UNKNOWN_CRATE,
                   progress: traces.CargoProgress | traces.CargoProgressUnavailable = UNKNOWN_PROGRESS
                   ) -> traces.CompileSample:
    return traces.CompileSample(at_ms, used, progress, crate)


class MemoryTraceTests(unittest.TestCase):
    @cached_property
    def root(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    @cached_property
    def store(self) -> traces.TraceStore:
        return traces.TraceStore(self.root, POLICY)

    def header(self, reservation_id: str, *, key: str = KEY) -> traces.TraceHeader:
        return traces.TraceHeader(reservation_id, key, NOW_MS - 4 * DAY_MS, POLICY.sample_interval_ms, 10)

    def finished(self, reservation_id: str, *, key: str = KEY, ended_at_ms: int = NOW_MS,
                 compile_ms: int = 2000, test_ms: int = 6000, elapsed_ms: int = 8000,
                 status: int | traces.ExitStatusUnavailable = 0,
                 coverage: traces.WholeRun | traces.IncompleteRun = WHOLE_RUN) -> traces.TraceEnd:
        writer = self.store.start(self.header(reservation_id, key=key))
        _ = writer.append(compile_sample(compile_ms, 5))
        _ = writer.append(traces.TestStarted(test_ms, 8))
        _ = writer.append(traces.TestSample(test_ms, 3))
        return writer.seal(traces.TraceEnd(ended_at_ms, elapsed_ms, status, coverage))

    def test_policy_uses_the_shared_reader_with_injected_settings(self) -> None:
        stored = (
            "sample_seconds=2\n"
            "max_samples_per_run=120\n"
            "max_bytes_per_run=8192\n"
            "retention_days=2\n"
            "max_finished_runs=3\n"
            "points_per_phase=3\n"
            "feed_bytes_per_refresh=1024\n"
            "final_replay_bytes=100000\n"
            "scope_loss_grace_seconds=9\n"
            "seal_retry_count=4\n"
            "max_pending_commands=12\n"
            "shutdown_timeout_seconds=3\n"
            "prune_interval_seconds=7\n"
        )
        path = self.root / "fixture.conf"
        settings = conf.ConfigRead(path, path, conf.settings(stored), repository_allowed=False)
        self.assertEqual(traces.read_policy(settings), POLICY)
        for config in (stored.replace("sample_seconds=2", "sample_seconds=bad"),
                       stored.replace("retention_days=2\n", ""),
                       stored.replace("points_per_phase=3", "points_per_phase=2"),
                       stored.replace("retention_days=2", "retention_days=-1"),
                       stored.replace("final_replay_bytes=100000\n", ""),
                       stored.replace("final_replay_bytes=100000", "final_replay_bytes=0"),
                       stored.replace("final_replay_bytes=100000", "final_replay_bytes=1048577"),
                       stored.replace("scope_loss_grace_seconds=9\n", ""),
                       stored.replace("scope_loss_grace_seconds=9", "scope_loss_grace_seconds=61"),
                       stored.replace("seal_retry_count=4\n", ""),
                       stored.replace("seal_retry_count=4", "seal_retry_count=-1"),
                       stored.replace("seal_retry_count=4", "seal_retry_count=11"),
                       stored.replace("max_pending_commands=12\n", ""),
                       stored.replace("max_pending_commands=12", "max_pending_commands=0"),
                       stored.replace("shutdown_timeout_seconds=3\n", ""),
                       stored.replace("shutdown_timeout_seconds=3", "shutdown_timeout_seconds=61"),
                       stored.replace("prune_interval_seconds=7\n", ""),
                       stored.replace("prune_interval_seconds=7", "prune_interval_seconds=0")):
            with self.subTest(config=config):
                self.assertIsInstance(traces.read_policy(replace(settings, settings=conf.settings(config))),
                                      traces.TracesOff)

    def test_policy_enforces_byte_bounds_without_range_comments(self) -> None:
        stored = (
            "sample_seconds=2\nmax_samples_per_run=120\nretention_days=2\nmax_finished_runs=3\n"
            "points_per_phase=3\nfeed_bytes_per_refresh=1024\nscope_loss_grace_seconds=9\nseal_retry_count=4\n"
            "final_replay_bytes=100000\n"
            "max_pending_commands=12\n"
            "shutdown_timeout_seconds=3\n"
            "prune_interval_seconds=7\n"
        )
        path = self.root / "fixture.conf"
        for value in (1023, 4_194_305, 10**20):
            with self.subTest(value=value):
                settings = conf.ConfigRead(path, path, conf.settings(f"{stored}max_bytes_per_run={value}\n"),
                                           repository_allowed=False)
                self.assertIsInstance(traces.read_policy(settings), traces.TracesOff)
        with self.assertRaisesRegex(ValueError, "max_bytes_per_run"):
            _ = replace(POLICY, max_bytes_per_run=10**20)

    def test_shape_normalizes_known_value_flags_and_keeps_execution_details(self) -> None:
        equivalent = ARGV[:3] + (
            "--bin=hana", "--profile=ci", "--features=native", "--target=target-a", "--package=hana",
            "--test-threads=8", "-E", "test(alpha)", "--", "--nocapture")
        self.assertEqual(traces.shape_key(replace(SHAPE, nextest_argv=equivalent)), KEY)
        for before, after in (("8", "16"), ("target-a", "target-b"), ("ci", "default"), ("native", "other"),
                              ("--bin", "--test"), ("--package", "--exclude"), ("test(alpha)", "test(beta)"),
                              ("--nocapture", "--show-output")):
            other = tuple(after if word == before else word for word in ARGV)
            with self.subTest(flag=before):
                self.assertNotEqual(traces.shape_key(replace(SHAPE, nextest_argv=other)), KEY)
        self.assertNotEqual(traces.shape_key(replace(SHAPE, host="another-host")), KEY)
        self.assertNotEqual(traces.shape_key(replace(SHAPE, repository_identity="another-repo")), KEY)
        self.assertNotEqual(traces.shape_key(replace(SHAPE, requested_test_width=RequestedTestWidth(16))), KEY)
        self.assertNotEqual(traces.shape_key(replace(SHAPE, requested_test_width=UnspecifiedTestWidth())), KEY)
        before_separator = (*ARGV[:-2], "--", "--bin=hana")
        after_separator = (*ARGV[:-2], "--", "--bin", "hana")
        self.assertNotEqual(traces.shape_key(replace(SHAPE, nextest_argv=before_separator)),
                            traces.shape_key(replace(SHAPE, nextest_argv=after_separator)))

    def test_each_record_round_trips_with_compact_rows(self) -> None:
        records: tuple[traces.TraceRecord, ...] = (
            self.header("round-trip"), compile_sample(2000, 12, crate="hana", progress=traces.CargoProgress(3, 4)),
            compile_sample(3000, 6, crate="hana"), compile_sample(4000, 4, progress=traces.CargoProgress(4, 4)),
            traces.TestStarted(4100, 4), traces.TestStarted(4150, UNKNOWN_RESERVE),
            traces.TestSample(4200, 2), traces.TraceLimit.FILE_SIZE,
            traces.TraceEnd(NOW_MS, 4600, 100, traces.WholeRun()),
            traces.TraceEnd(NOW_MS, 4600, traces.ExitStatusUnavailable(),
                            traces.IncompleteRun(traces.IncompleteReason.SCOPE_LOST)),
        )
        for record in records:
            with self.subTest(record=record):
                line = traces.encode_record(record)
                self.assertTrue(line.endswith(b"\n"))
                self.assertEqual(traces.decode_record(line), record)
        self.assertEqual(traces.encode_record(records[1]), b'["c",2000,12,3,4,"hana"]\n')

    def test_bad_complete_records_fail_at_the_boundary(self) -> None:
        header = traces.encode_record(self.header("record"))
        for invalid in (b'["c",1,true,2,3,"hana"]\n', b'["c",1,8,3,2,"hana"]\n',
                        b'["c",1,8,null,2,"hana"]\n', b'["c",1,8,1,2,7]\n', b'["t",2,4]\n',
                        b'["s",1,0]\n', b'["s",1,null]\n["c",2,8,1,2,"hana"]\n',
                        b'["c",4,8,null,null,null]\n["s",3,null]\n',
                        b'["e",864000000,8000,0,null]\n', b'["l","unknown"]\n',
                        b'["e",864000000,8000,true,"scope_lost"]\n', b'["l","byte_limit"]\n["t",2,4]\n'):
            with self.subTest(line=invalid):
                path = self.root / "record.jsonl"
                _ = path.write_bytes(header + invalid)
                self.assertIsInstance(traces.read_trace(path, POLICY), traces.TraceUnavailable)

    def test_reader_ignores_partial_tail_and_rejects_an_unknown_version(self) -> None:
        writer = self.store.start(self.header("partial"))
        sample = compile_sample(2000, 8)
        _ = writer.append(sample)
        with writer.path.open("ab") as stream:
            _ = stream.write(b'["c",4000,9,null,null,null]')
        trace = self.store.read_live("partial")
        assert isinstance(trace, traces.MemoryTrace)
        self.assertEqual(trace.compile_samples, (sample,))
        self.assertIsInstance(trace.state, traces.TraceTailIncomplete)
        _ = writer.path.write_bytes(traces.encode_record(writer.header).replace(b'["h",1,', b'["h",2,', 1))
        self.assertIsInstance(self.store.read_live("partial"), traces.TraceUnavailable)

    def test_end_with_a_partial_suffix_is_not_past_history(self) -> None:
        _ = self.finished("tail")
        path = self.root / "finished" / KEY / f"{NOW_MS}-tail.jsonl"
        with path.open("ab") as stream:
            _ = stream.write(b'["')
        self.assertIsInstance(self.store.newest_whole_run(KEY), traces.NoMatchingWholeRun)

    def test_append_keeps_the_limit_when_it_completes_a_partial_end(self) -> None:
        writer = self.store.start(self.header("partial-end"))
        _ = writer.append(compile_sample(2000, 8))
        end = traces.TraceEnd(NOW_MS, 4000, 0, traces.IncompleteRun(traces.TraceLimit.SAMPLE_COUNT))
        end_line = traces.encode_record(end)
        with writer.path.open("ab") as stream:
            _ = stream.write(traces.encode_record(traces.TraceLimit.SAMPLE_COUNT) + end_line[:-1])
        cached = traces.read_trace(writer.path, POLICY)
        assert isinstance(cached, traces.MemoryTrace)
        self.assertIsInstance(cached.state, traces.TraceTailIncomplete)

        with writer.path.open("ab") as stream:
            _ = stream.write(b"\n")
        updated = cached.read_body(BytesIO(end_line), POLICY)
        self.assertEqual(updated, traces.read_trace(writer.path, POLICY))
        self.assertEqual(updated.state, end)

    def test_late_test_reserve_keeps_the_first_start_and_reserve(self) -> None:
        writer = self.store.start(self.header("late-reserve"))
        with self.assertRaisesRegex(ValueError, "reserve is out of order"):
            _ = writer.append(traces.TestReserveObserved(5))
        _ = writer.append(compile_sample(1000, 3))
        _ = writer.append(traces.TestStarted(2000, UNKNOWN_RESERVE))
        _ = writer.append(traces.TestSample(2000, 3))
        _ = writer.append(traces.TestReserveObserved(5))
        with self.assertRaisesRegex(ValueError, "reserve is out of order"):
            _ = writer.append(traces.TestReserveObserved(6))
        live = self.store.read_live("late-reserve")
        assert isinstance(live, traces.MemoryTrace)
        self.assertEqual(live.test_start, traces.TestStarted(2000, 5))

    def test_writer_marks_the_count_limit_and_preserves_the_end(self) -> None:
        policy = replace(POLICY, max_samples_per_run=1)
        store = traces.TraceStore(self.root, policy)
        writer = store.start(self.header("sample-limit"))
        sample = compile_sample(2000, 8)
        self.assertIsInstance(writer.append(sample), traces.TraceRecording)
        self.assertIs(writer.append(compile_sample(4000, 9)), traces.TraceLimit.SAMPLE_COUNT)
        before = writer.path.read_bytes()
        self.assertIs(writer.append(compile_sample(6000, 10)), traces.TraceLimit.SAMPLE_COUNT)
        self.assertEqual(writer.path.read_bytes(), before)
        live = store.read_live("sample-limit")
        assert isinstance(live, traces.MemoryTrace)
        self.assertEqual(live.compile_samples, (sample,))
        self.assertEqual(live.state, traces.TraceLimit.SAMPLE_COUNT)
        end = writer.seal(traces.TraceEnd(NOW_MS, 8000, 100, traces.WholeRun()))
        self.assertEqual(end.coverage, traces.IncompleteRun(traces.TraceLimit.SAMPLE_COUNT))
        sealed = traces.read_trace(self.root / "finished" / KEY / f"{NOW_MS}-sample-limit.jsonl", policy)
        assert isinstance(sealed, traces.MemoryTrace)
        self.assertEqual(sealed.state, end)
        self.assertEqual(sealed.compile_samples, (sample,))
        self.assertIsInstance(store.newest_whole_run(KEY), traces.NoMatchingWholeRun)

    def test_writer_reserves_space_for_the_end_and_reader_enforces_both_limits(self) -> None:
        policy = replace(POLICY, max_bytes_per_run=300)
        store = traces.TraceStore(self.root, policy)
        writer = store.start(self.header("byte-limit"))
        sample = compile_sample(2000, 8)
        _ = writer.append(sample)
        self.assertIs(writer.append(compile_sample(4000, 9, crate="crate_" * 40)), traces.TraceLimit.FILE_SIZE)
        end = writer.seal(traces.TraceEnd(NOW_MS, 8000, 0, traces.WholeRun()))
        path = self.root / "finished" / KEY / f"{NOW_MS}-byte-limit.jsonl"
        self.assertLessEqual(path.stat().st_size, policy.max_bytes_per_run)
        sealed = traces.read_trace(path, policy)
        assert isinstance(sealed, traces.MemoryTrace)
        self.assertEqual(sealed.compile_samples, (sample,))
        self.assertEqual(sealed.state, end)
        self.assertIsInstance(traces.read_trace(path, replace(policy, max_bytes_per_run=path.stat().st_size - 1)),
                              traces.TraceUnavailable)
        live = store.start(self.header("smaller-limit"))
        _ = live.append(sample)
        _ = live.append(compile_sample(4000, 9))
        self.assertIsInstance(traces.read_trace(live.path, replace(policy, max_samples_per_run=1)),
                              traces.TraceUnavailable)

    def test_retry_after_failed_move_keeps_one_end_record(self) -> None:
        writer = self.store.start(self.header("retry"))
        _ = writer.append(compile_sample(2000, 5))
        _ = writer.append(traces.TestStarted(3000, UNKNOWN_RESERVE))
        _ = writer.append(traces.TestSample(4000, 4))
        finished = self.root / "finished"
        finished.touch()
        end = traces.TraceEnd(NOW_MS, 5000, 0, traces.WholeRun())
        with self.assertRaises(OSError):
            _ = writer.seal(end)
        self.assertIsInstance(self.store.newest_whole_run(KEY), traces.NoMatchingWholeRun)
        with self.assertRaises(FileExistsError):
            _ = self.store.start(writer.header)
        finished.unlink()
        self.assertEqual(writer.seal(replace(end, ended_at_ms=NOW_MS + 1)), end)
        self.assertEqual(writer.seal(end), end)
        self.assertFalse(writer.path.exists())
        past = self.store.newest_whole_run(KEY)
        assert isinstance(past, traces.MemoryTrace)
        self.assertEqual(past.state, end)
        with self.assertRaises(FileExistsError):
            _ = self.store.start(writer.header)

    def test_copy_after_failed_move_keeps_the_verified_end_record(self) -> None:
        writer = self.store.start(self.header("copy"))
        _ = writer.append(compile_sample(2000, 5))
        _ = writer.append(traces.TestStarted(3000, UNKNOWN_RESERVE))
        _ = writer.append(traces.TestSample(4000, 4))
        end = traces.TraceEnd(NOW_MS, 5000, 0, WHOLE_RUN)
        with mock.patch.object(Path, "replace", side_effect=OSError("trace move failed")):
            with self.assertRaises(OSError):
                _ = writer.seal(end)
        expected = writer.path.read_bytes()
        writer.seal_prefix(replace(end, ended_at_ms=NOW_MS + 1))
        sealed = self.root / "finished" / KEY / f"{NOW_MS}-copy.jsonl"
        self.assertEqual(list(sealed.parent.iterdir()), [sealed])
        self.assertEqual(sealed.read_bytes(), expected)
        self.assertFalse(writer.path.exists())

    def test_seal_replaces_a_partial_end_at_the_last_verified_offset(self) -> None:
        writer = self.store.start(self.header("torn-end"))
        _ = writer.append(compile_sample(2000, 5))
        _ = writer.append(traces.TestStarted(3000, UNKNOWN_RESERVE))
        _ = writer.append(traces.TestSample(4000, 4))
        prefix = writer.path.read_bytes()
        end = traces.TraceEnd(NOW_MS, 5000, 0, traces.WholeRun())
        line = traces.encode_record(end)
        with writer.path.open("ab") as stream:
            _ = stream.write(line[:len(line) // 2])

        self.assertEqual(writer.seal(end), end)
        sealed = self.root / "finished" / KEY / f"{NOW_MS}-torn-end.jsonl"
        self.assertEqual(sealed.read_bytes(), prefix + line)

    def test_whole_run_requires_coverage_at_start_between_phases_and_at_end(self) -> None:
        for identifier, compile_ms, test_ms, elapsed_ms in (
            ("late", 4001, 6000, 8000), ("gap", 2000, 6001, 8000), ("end", 2000, 6000, 10001),
        ):
            with self.subTest(gap=identifier):
                end = self.finished(identifier, compile_ms=compile_ms, test_ms=test_ms, elapsed_ms=elapsed_ms)
                self.assertEqual(end.coverage, traces.IncompleteRun(traces.IncompleteReason.SAMPLE_GAP))
        self.assertIsInstance(self.store.newest_whole_run(KEY), traces.NoMatchingWholeRun)
        writer = self.store.start(self.header("short-test"))
        _ = writer.append(compile_sample(2000, 5))
        _ = writer.append(traces.TestStarted(2200, UNKNOWN_RESERVE))
        end = writer.seal(traces.TraceEnd(NOW_MS, 2250, 0, traces.WholeRun()))
        self.assertEqual(end.coverage, traces.IncompleteRun(traces.IncompleteReason.MISSING_TEST))

    def test_newest_whole_run_accepts_test_failure_and_ignores_other_shapes_and_incomplete_runs(self) -> None:
        _ = self.finished("first", ended_at_ms=NOW_MS - 1)
        _ = self.finished("a")
        self.assertIsInstance(self.finished("b", status=100).coverage, traces.WholeRun)
        _ = self.finished("other", key="4" * 64, ended_at_ms=NOW_MS + 1)
        _ = self.finished("invalid-status", status=101, ended_at_ms=NOW_MS + 2)
        _ = self.finished("no-summary", ended_at_ms=NOW_MS + 3,
                          coverage=traces.IncompleteRun(traces.IncompleteReason.NO_NEXTTEST_COMPLETION))
        _ = self.store.start(self.header("restarted"))
        malformed = self.root / "finished" / KEY / f"{NOW_MS + 4}-malformed.jsonl"
        _ = malformed.write_bytes(b"bad\n")
        past = self.store.newest_whole_run(KEY)
        assert isinstance(past, traces.MemoryTrace)
        self.assertEqual(past.header.reservation_id, "b")
        self.assertEqual(past.state, traces.TraceEnd(NOW_MS, 8000, 100, traces.WholeRun()))
        earlier = self.store.newest_whole_run(KEY, exclude_reservations={"b"})
        assert isinstance(earlier, traces.MemoryTrace)
        self.assertEqual(earlier.header.reservation_id, "a")

    def test_retention_applies_age_and_count_across_shapes_and_keeps_active_files(self) -> None:
        _ = self.finished("old", key="5" * 64, ended_at_ms=NOW_MS - 3 * DAY_MS)
        _ = self.finished("extra", ended_at_ms=NOW_MS - 2 * DAY_MS)
        for index in range(3):
            _ = self.finished(f"kept-{index}", key=str(index) * 64, ended_at_ms=NOW_MS - index)
        abandoned = self.store.start(self.header("abandoned"))
        active = self.store.start(self.header("active"))
        recent = self.store.start(self.header("recent"))
        for writer in (abandoned, active):
            timestamp = (NOW_MS - 3 * DAY_MS) * 1_000_000
            os.utime(writer.path, ns=(timestamp, timestamp))
        os.utime(recent.path, ns=(NOW_MS * 1_000_000, NOW_MS * 1_000_000))
        self.store.prune(NOW_MS, {"active"})
        self.assertFalse(abandoned.path.exists())
        self.assertTrue(active.path.exists())
        self.assertTrue(recent.path.exists())
        self.assertEqual(len(list((self.root / "finished").glob("*/*.jsonl"))), 3)
        self.assertFalse((self.root / "finished" / ("5" * 64)).exists())
        self.store.prune(NOW_MS + 3 * DAY_MS, set())
        self.assertFalse(list((self.root / "finished").glob("*/")))

    def test_recovery_continues_after_one_file_cannot_publish(self) -> None:
        blocked = self.store.start(self.header("blocked"))
        healthy = self.store.start(self.header("healthy"))
        def deny_one(destination: Path, content: bytes, *, mode: int) -> None:
            if destination.name.endswith("-blocked.jsonl"):
                raise PermissionError("cannot publish this trace")
            write_bytes_atomic(destination, content, mode=mode)

        with mock.patch.object(traces, "write_bytes_atomic", side_effect=deny_one):
            self.store.recover_abandoned()
        self.assertTrue(blocked.path.exists())
        self.assertFalse(healthy.path.exists())
        path, = (self.root / "finished" / KEY).glob("*-healthy.jsonl")
        trace = traces.read_trace(path, POLICY)
        assert isinstance(trace, traces.MemoryTrace) and isinstance(trace.state, traces.TraceEnd)
        self.assertEqual(trace.state.coverage, traces.IncompleteRun(traces.IncompleteReason.RESTART))

    def test_recovery_keeps_the_verified_end_when_the_suffix_is_partial(self) -> None:
        end = self.finished("torn-after-end", ended_at_ms=NOW_MS)
        sealed = self.root / "finished" / KEY / f"{NOW_MS}-torn-after-end.jsonl"
        live = self.root / "live" / "torn-after-end.jsonl"
        _ = live.write_bytes(sealed.read_bytes() + b'["')
        sealed.unlink()

        self.store.recover_abandoned()

        trace = traces.read_trace(sealed, POLICY)
        assert isinstance(trace, traces.MemoryTrace) and isinstance(trace.state, traces.TraceEnd)
        self.assertFalse(live.exists())
        self.assertEqual(trace.state, replace(end, exit_status=traces.ExitStatusUnavailable(),
                                              coverage=traces.IncompleteRun(traces.IncompleteReason.RESTART)))

    def test_partial_header_is_removed_before_the_next_start(self) -> None:
        header = self.header("partial")
        path = self.root / "live" / "partial.jsonl"

        class ShortHeader(FileIO):
            @override
            def write(self, data: Buffer, /) -> int | None:
                return super().write(memoryview(data)[:5])

        def short_header(target: Path, mode: str = "r") -> FileIO:
            self.assertEqual(target, path)
            return ShortHeader(target, mode)

        with mock.patch.object(Path, "open", autospec=True, side_effect=short_header):
            with self.assertRaises(OSError):
                _ = self.store.start(header)
        self.assertFalse(path.exists())
        writer = self.store.start(header)
        self.assertEqual(writer.path.read_bytes(), traces.encode_record(header))

    def test_prune_continues_after_one_finished_file_cannot_be_deleted(self) -> None:
        _ = self.finished("blocked", ended_at_ms=NOW_MS - 3 * DAY_MS)
        _ = self.finished("healthy", ended_at_ms=NOW_MS - 4 * DAY_MS)
        folder = self.root / "finished" / KEY
        blocked, = folder.glob("*-blocked.jsonl")
        healthy, = folder.glob("*-healthy.jsonl")
        unlink = Path.unlink

        def deny_one(path: Path, missing_ok: bool = False) -> None:
            if path == blocked:
                raise PermissionError("cannot delete this trace")
            unlink(path, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=deny_one):
            self.store.prune(NOW_MS, set())
        self.assertTrue(blocked.exists())
        self.assertFalse(healthy.exists())

    def test_prune_continues_after_denied_directory_and_live_file_reads(self) -> None:
        _ = self.finished("hidden", key="4" * 64, ended_at_ms=NOW_MS - 3 * DAY_MS)
        _ = self.finished("visible", ended_at_ms=NOW_MS - 3 * DAY_MS)
        blocked = self.store.start(self.header("blocked"))
        healthy = self.store.start(self.header("healthy"))
        timestamp = (NOW_MS - 3 * DAY_MS) * 1_000_000
        os.utime(healthy.path, ns=(timestamp, timestamp))
        folder = self.root / "finished" / ("4" * 64)
        iterdir, file_stat = Path.iterdir, Path.stat

        def scan(path: Path) -> Iterator[Path]:
            if path == folder:
                raise PermissionError("cannot scan this shape")
            return iterdir(path)

        def stat_path(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if path == blocked.path:
                raise PermissionError("cannot read this trace's metadata")
            return file_stat(path, follow_symlinks=follow_symlinks)

        with (mock.patch.object(Path, "iterdir", autospec=True, side_effect=scan),
              mock.patch.object(Path, "stat", autospec=True, side_effect=stat_path)):
            self.store.prune(NOW_MS, set())
        self.assertTrue(blocked.path.exists())
        self.assertFalse(healthy.path.exists())
        self.assertTrue(list(folder.glob("*-hidden.jsonl")))
        self.assertFalse(list((self.root / "finished" / KEY).glob("*-visible.jsonl")))

    def test_chart_keeps_endpoints_peak_and_gaps_and_totals_use_all_samples(self) -> None:
        writer = self.store.start(self.header("gaps"))
        progress = traces.CargoProgress(835, 836)
        for elapsed_ms, used in ((0, 4), (2000, 6), (4000, 12), (6000, 5), (12000, 8), (14000, 3)):
            _ = writer.append(compile_sample(elapsed_ms, used, crate="hana", progress=progress))
        _ = writer.append(traces.TestStarted(16000, 8))
        _ = writer.append(traces.TestSample(16000, 5))
        _ = writer.append(traces.TestSample(18000, 2))
        live = self.store.read_live("gaps")
        assert isinstance(live, traces.MemoryTrace)
        charts = traces.project_trace(live, POLICY)
        self.assertEqual(charts.compile, traces.PhaseChart(
            16000, ((traces.ChartPoint(0, 4), traces.ChartPoint(4000, 12)), (traces.ChartPoint(14000, 3),)),
            traces.ChartPoint(4000, 12), 10, 6000,
        ))
        self.assertEqual(charts.test, traces.PhaseChart(
            2000, ((traces.ChartPoint(0, 5), traces.ChartPoint(2000, 2)),), traces.ChartPoint(0, 5), 8, 2000,
        ))
        self.assertEqual(charts.hana_crate_bands, (traces.HanaCrateBand(0, 6000), traces.HanaCrateBand(12000, 14000)))

    def test_hana_compile_fixture_preserves_the_approved_202_second_band(self) -> None:
        # Keep the measured endpoints from nextest-3201363-1794951317.csv.
        writer = self.store.start(self.header("approved-hana"))
        _ = writer.append(compile_sample(137813, 199_815_168, crate="hana_mimesis_tools",
                                         progress=traces.CargoProgress(834, 836)))
        progress = traces.CargoProgress(835, 836)
        _ = writer.append(compile_sample(139812, 1_646_211_072, crate="hana", progress=progress))
        for elapsed_ms in range(141812, 341811, 2000):
            _ = writer.append(compile_sample(elapsed_ms, 3_000_000_000, crate="hana", progress=progress))
        _ = writer.append(compile_sample(341814, 188_473_344, crate="hana", progress=progress))
        _ = writer.append(traces.TestStarted(342724, UNKNOWN_RESERVE))
        _ = writer.append(traces.TestSample(343814, 200_000_000))
        live = self.store.read_live("approved-hana")
        assert isinstance(live, traces.MemoryTrace)
        charts = traces.project_trace(live, POLICY)
        self.assertEqual(charts.hana_crate_bands, (traces.HanaCrateBand(139812, 341814),))
        self.assertEqual(charts.hana_compile_ms, 202002)
        assert isinstance(charts.test, traces.PhaseChart)
        self.assertEqual(charts.test.above_half_reserve_ms, UNKNOWN_RESERVE)
