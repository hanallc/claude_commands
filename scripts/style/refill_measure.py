"""Measure cold and warm refill reads on a private ledger backup.

The measurement uses next-phase's selection code in COUNT mode and its candidate
reader. It does not assign tickets, change plans, or change proposal files.
RSS and descendant IO are sampled every 10 milliseconds on Linux. Short-lived
descendants can finish between samples. Each worker also reports its own final
IO counters and kernel RSS high-water mark.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager, redirect_stdout
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from functools import wraps
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
from typing import ParamSpec, TypeVar, cast, override
from unittest.mock import patch

from scripts.lib import berth_board
from scripts.style import refill_facts, refill_findings, refill_inputs, rules, style_config, style_ledger, style_plan, style_proposals, style_reader
from scripts.style.unit_files import unit_worktrees
from scripts.style.unit_plans import family_pilot_scopes, unit_plan_path


def backup_ledger(source: Path, destination: Path) -> None:
    """Copy a ledger through a read-only source connection."""
    with (closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original,
          closing(sqlite3.connect(destination)) as private):
        original.backup(private)


def quoted(name: str) -> str:
    """Quote a SQLite table or column name from its schema."""
    return '"' + name.replace('"', '""') + '"'


DERIVED_TABLES = (
    "refill_dependencies", "refill_candidates", "refill_cells", "refill_findings",
    "refill_inputs", "refill_objects", "listed_sites", "listed_rules",
)


def clear_derived(database: Path) -> tuple[str, ...]:
    """Remove derived facts while preserving source versions and refill outcomes."""
    with closing(sqlite3.connect(database)) as connection, connection:
        tables = {row[0] for row in cast(list[tuple[str]], connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())}
        selected = tuple(name for name in DERIVED_TABLES if name in tables)
        for name in selected:
            connection.execute("DELETE FROM " + quoted(name)).close()
    return selected


def stored_rows(database: Path) -> dict[str, object]:
    """Report table rows and logical payload bytes, with each saved fact kind."""
    result: dict[str, object] = {}
    with closing(sqlite3.connect(database)) as connection:
        tables = cast(list[tuple[str]], connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall())
        for (name,) in tables:
            fields = cast(list[tuple[int, str, str, int, object, int]],
                          connection.execute("PRAGMA table_info(" + quoted(name) + ")").fetchall())
            columns = [field[1] for field in fields]
            length = " + ".join("coalesce(length(cast(" + quoted(column) + " AS BLOB)),0)" for column in columns)
            rows = cast(tuple[int, int], connection.execute(
                "SELECT count(*),coalesce(sum(" + length + "),0) FROM " + quoted(name)).fetchone())
            entry: dict[str, object] = {"rows": rows[0], "payload_bytes": rows[1]}
            kind = next((column for column in ("fact_kind", "input_kind") if column in columns), "")
            if kind:
                groups = cast(list[tuple[str, int, int]], connection.execute(
                    "SELECT " + quoted(kind) + ",count(*),sum(" + length + ") FROM " + quoted(name)
                    + " GROUP BY " + quoted(kind) + " ORDER BY " + quoted(kind)).fetchall())
                entry["kinds"] = {key: {"rows": count, "payload_bytes": size} for key, count, size in groups}
            result[name] = entry
    return result


def result_value(value: object) -> object:
    """Serialize domain values without timings or process identities."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return cast(object, value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, set | frozenset):
        return sorted(cast(set[object] | frozenset[object], value), key=str)
    raise TypeError(f"cannot serialize refill result {type(value).__name__}")


def refill(repo: Path, database: Path, unit: str) -> str:
    """Run the same selection reader that next-phase uses, without ticket writes."""
    with style_ledger.Ledger(repo, database) as ledger:
        config = style_config.config_for_unit(unit, ledger.staff_resolver().record)
        row = next(row for row in unit_worktrees(config.route.doc) if row.unit == unit or str(row.staff_id) == unit)
        plan = unit_plan_path(row.unit, row.worktree)
        text = plan.read_text()
        settings = style_plan.unit_plan_settings(text, repo)
        tip = ledger.synced_commit()
        board = berth_board.read_board(repo)
        if isinstance(board, berth_board.BoardUnavailable):
            raise style_ledger.CheckCouldNotRun(str(board))
        busy = style_plan.busy_files(repo, config.route.doc, ledger, str(row.staff_id), board)
        definitions = {rule.rule_id: rule for rule in rules.load_rules(repo)}
        changes = style_plan.main_changes(ledger, config.route.branch_point, tip)
        families = config.assignment(row.unit).families
        # COUNT follows declarations without writing its projected locations or decisions.
        selected = style_plan.count_refill(ledger, str(row.staff_id), row.branch, plan, repo, config.route.doc, text,
                                           settings.pilot_scope, busy, settings.first_files, definitions, changes,
                                           style_plan.NoCheckpointBlock())
        candidates = {family: style_plan.candidate_tickets(
            repo, ledger, busy, family, settings.pilot_scope, family_pilot_scopes(config.route.doc, repo),
            base=style_plan.unit_base(repo, row.branch), first_files=settings.first_files) for family in families}
        population = tuple(ledger.cells())
        findings = tuple(finding.finding_id for finding in ledger.held_findings(style_ledger.HeldFindingsInState("filed")))
        proofs = ledger.declaration_reading(findings, style_proposals.approved_decision)
        if isinstance(_active_measurements, StageMeasurements):
            _active_measurements.counters["findings"] = len(findings)
            _active_measurements.counters["candidates"] = sum(len(rows) for rows in candidates.values())
        result = json.dumps({"selection": selected, "candidates": candidates, "cells": population, "findings": proofs},
                            default=result_value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(result.encode()).hexdigest()


def process_usage(pid: int) -> tuple[int, int, tuple[int, ...]]:
    """Read one process's resident bytes, written bytes, and direct children."""
    root = Path("/proc") / str(pid)
    status = dict(line.split(":", 1) for line in (root / "status").read_text().splitlines())
    counters = dict(line.split(":", 1) for line in (root / "io").read_text().splitlines())
    children = tuple(int(child) for child in (root / "task" / str(pid) / "children").read_text().split())
    return int(status.get("VmRSS", "0 kB").split()[0]) * 1024, int(counters["write_bytes"]), children


class ProcessTreeUsage:
    """Measure the simultaneous RSS and retain descendant write counters."""

    def __init__(self, pid: int) -> None:
        self.pid: int = pid
        self.peak_rss: int = 0
        self.writes: dict[int, int] = {}

    def sample(self) -> None:
        pending = [self.pid]
        resident = 0
        while pending:
            pid = pending.pop()
            try:
                rss, written, children = process_usage(pid)
            except (OSError, KeyError, ValueError):
                continue
            resident += rss
            self.writes[pid] = max(written, self.writes.get(pid, 0))
            pending.extend(children)
        self.peak_rss = max(self.peak_rss, resident)

    def collect(self, finished: Event) -> None:
        while not finished.wait(0.01):
            self.sample()


def measured_run(repo: Path, database: Path, unit: str, environment: Mapping[str, str], *, stages: bool = False) -> dict[str, object]:
    """Measure a fresh process so a cold or warm run has its own memory peak."""
    command = [sys.executable, "-m", "scripts.style.refill_measure", "--worker", "--repo", str(repo),
               "--ledger", str(database), "--unit", unit]
    if stages:
        command.append("--stages")
    started = time.perf_counter()
    child_environment = dict(environment, REFILL_MEASURE_STARTED=str(started))
    with subprocess.Popen(command, env=child_environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        usage = ProcessTreeUsage(process.pid)
        usage.sample()
        finished = Event()
        monitor = Thread(target=usage.collect, args=(finished,))
        monitor.start()
        try:
            output, errors = process.communicate()
        finally:
            finished.set()
            monitor.join()
        elapsed = time.perf_counter() - started
        if process.returncode:
            raise RuntimeError(errors.decode(errors="replace"))
    worker = cast(dict[str, object], json.loads(output))
    usage.writes[process.pid] = cast(int, worker["write_bytes"])
    return {"wall_seconds": elapsed, "peak_rss_bytes": max(usage.peak_rss, cast(int, worker["peak_rss_bytes"])),
            "write_bytes": sum(usage.writes.values()), "result_digest": worker["result_digest"],
            "reader": worker["reader"],
            "stored": stored_rows(database), "sampling_seconds": 0.01,
            **({"stages": worker["stages"]} if stages else {})}


def measure(repo: Path, live_ledger: Path, reader: style_config.ReaderSource, unit: str, workspace: Path, *,
            stages: bool = False) -> dict[str, object]:
    """Keep every write in a temporary workspace and preserve the live source."""
    with tempfile.TemporaryDirectory(prefix="refill-measure-", dir=workspace) as temporary:
        private = Path(temporary)
        database = private / "state" / style_ledger.ledger_location(repo).database.parent.name / "ledger.sqlite"
        database.parent.mkdir(parents=True)
        backup_ledger(live_ledger, database)
        with style_ledger.Ledger(repo, database):
            pass
        removed = clear_derived(database)
        config = private / "config"
        config.mkdir()
        current = Path(os.environ.get("CONF_DIR", str(Path(__file__).resolve().parents[2] / "config")))
        for source in current.glob("*.conf"):
            _ = shutil.copy2(source, config / source.name)
        select_reader(config, reader)
        environment = dict(os.environ, CONF_DIR=str(config), STYLE_LEDGER_STATE_DIR=str(private / "state"))
        cold = measured_run(repo, database, unit, environment, stages=stages)
        warm = measured_run(repo, database, unit, environment, stages=stages)
        return {"reader": reader.value, "cleared_tables": removed, "cold": cold, "warm": warm,
                "same_result": cold["result_digest"] == warm["result_digest"]}


def select_reader(config: Path, reader: style_config.ReaderSource) -> None:
    """Set the reader only in a private configuration copy."""
    switch = config / "style_reader.conf"
    text = switch.read_text() if switch.is_file() else ""
    lines: list[str] = []
    for line in text.splitlines():
        setting = line.partition("=")[0].strip()
        lines.append(f"{setting}={reader.value}" if "=" in line and (setting == "reader" or setting.startswith("reader.")) else line)
    lines.append(f"reader={reader.value}")
    _ = switch.write_text("\n".join(lines) + "\n")


Parameters = ParamSpec("Parameters")
Result = TypeVar("Result")
@dataclass(frozen=True)
class UnmeasuredRun:
    """The refill has no active measurement counters."""


_UNMEASURED_RUN = UnmeasuredRun()
_active_measurements: StageMeasurements | UnmeasuredRun = _UNMEASURED_RUN


class StageMeasurements:
    """Count calls and measure exclusive time inside a private worker."""

    def __init__(self) -> None:
        names = ("python_start", "ledger_reads", "external_reads", "git_subprocesses", "reader_calls",
                 "derived_encoding", "derived_writes", "selection_compute")
        self.seconds: dict[str, float] = dict.fromkeys(names, 0.0)
        self.calls: dict[str, int] = dict.fromkeys(names, 0)
        self.counters: dict[str, int] = dict.fromkeys(("findings", "candidates", "git_subprocesses", "reader_requests",
            "unique_revision_pairs", "declaration_searches", "rule_scans", "rule_catalogs", "declaration_comparisons"), 0)
        self.pairs: set[tuple[str, ...]] = set()
        self.rule_paths: set[str] = set()
        self.commands: dict[str, int] = {}
        self.stack: list[str] = []
        self.git_processes: set[subprocess.Popen[bytes]] = set()
        self.last_event: float = time.perf_counter()

    def record_time(self) -> None:
        """Assign elapsed time to active Git processes or the current stage."""
        now = time.perf_counter()
        elapsed = now - self.last_event
        self.last_event = now
        if self.git_processes:
            self.seconds["git_subprocesses"] += elapsed * len(self.git_processes)
        elif self.stack:
            self.seconds[self.stack[-1]] += elapsed

    @contextmanager
    def timed(self, stage: str) -> Generator[None]:
        self.record_time()
        self.calls[stage] += 1
        self.stack.append(stage)
        try:
            yield
        finally:
            self.record_time()
            _ = self.stack.pop()

    def wrapper(self, stage: str, function: Callable[Parameters, Result], counter: str = "") -> Callable[Parameters, Result]:
        @wraps(function)
        def measured(*args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
            if counter:
                self.counters[counter] += 1
            with self.timed(stage):
                return function(*args, **kwargs)
        return measured

    def report(self) -> dict[str, object]:
        self.record_time()
        self.counters["unique_revision_pairs"] = len(self.pairs)
        self.counters["unique_rule_files"] = len(self.rule_paths)
        return {"seconds": self.seconds, "calls": self.calls, "counters": self.counters, "git_commands": self.commands,
                "repeated_work": [{"loop": "finding declaration searches × candidate declaration items",
                                   "outer_calls": self.counters["declaration_searches"],
                                   "inner_calls": self.counters["declaration_comparisons"]},
                                  {"loop": "rule catalogs × rule files", "outer_calls": self.counters["rule_catalogs"],
                                   "unique_files": len(self.rule_paths), "inner_calls": self.counters["rule_scans"]}]}

    @contextmanager
    def installed(self, enabled: bool) -> Generator[None]:
        """Install wrappers only in the measurement worker and restore them afterward."""
        if not enabled:
            yield
            return
        global _active_measurements
        _active_measurements = self
        original_connect = sqlite3.connect
        original_process_start = subprocess.Popen.__init__
        original_request = cast(Callable[..., AbstractContextManager[Iterator[dict[str, object]]]], getattr(style_reader.ReaderClient, "_request"))
        original_rule_load = cast(Callable[..., object], getattr(rules, "_load_rule"))
        measurements = self

        class TimedCursor(sqlite3.Cursor):
            @override
            def fetchone(self) -> object:
                with measurements.timed("ledger_reads"):
                    return cast(object, super().fetchone())

            @override
            def fetchall(self) -> list[object]:
                with measurements.timed("ledger_reads"):
                    return cast(list[object], super().fetchall())

            @override
            def __next__(self) -> object:
                with measurements.timed("ledger_reads"):
                    return cast(object, super().__next__())

        class TimedConnection(sqlite3.Connection):
            @override
            def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
                stage = "ledger_reads" if sql.lstrip().upper().startswith(("SELECT", "WITH", "PRAGMA")) else "derived_writes"
                with measurements.timed(stage):
                    return cast(Callable[..., sqlite3.Cursor], self.cursor(TimedCursor).execute)(sql, parameters)

            @override
            def executemany(self, sql: str, parameters: Iterable[object]) -> sqlite3.Cursor:
                with measurements.timed("derived_writes"):
                    return cast(Callable[..., sqlite3.Cursor], self.cursor(TimedCursor).executemany)(sql, parameters)

            @override
            def commit(self) -> None:
                with measurements.timed("derived_writes"):
                    super().commit()

        def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            return cast(Callable[..., sqlite3.Connection], original_connect)(*args, **dict(kwargs, factory=TimedConnection))

        def process_start(process: subprocess.Popen[bytes], args: object, *arguments: object, **keywords: object) -> None:
            if isinstance(args, list | tuple):
                command = cast(list[object] | tuple[object, ...], args)
                if command and isinstance(command[0], str) and Path(command[0]).name == "git":
                    self.record_time()
                    self.git_processes.add(process)
                    self.counters["git_subprocesses"] += 1
                    self.calls["git_subprocesses"] += 1
                    words = tuple(str(word) for word in command)
                    options = words[3:] if len(words) > 2 and words[1] == "-C" else words[1:]
                    operation = options[0] if options else ""
                    self.commands[operation] = self.commands.get(operation, 0) + 1
                    if operation in {"diff", "merge-base", "rev-list"}:
                        revisions = tuple(word for word in options[1:] if not word.startswith("-"))
                        if len(revisions) >= 2:
                            self.pairs.add(revisions[:2])
            try:
                cast(Callable[..., None], original_process_start)(process, args, *arguments, **keywords)
            except BaseException:
                self.record_time()
                self.git_processes.discard(process)
                raise

        def process_completion(function: Callable[Parameters, Result]) -> Callable[Parameters, Result]:
            @wraps(function)
            def completed(*args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
                process = cast(subprocess.Popen[bytes], args[0])
                try:
                    return function(*args, **kwargs)
                finally:
                    if process in self.git_processes and process.returncode is not None:
                        self.record_time()
                        self.git_processes.remove(process)
            return completed

        @contextmanager
        def request(client: style_reader.ReaderClient, repository: Path, **fields: object
                    ) -> Generator[Iterator[dict[str, object]]]:
            self.counters["reader_requests"] += 1
            with self.timed("reader_calls"), original_request(client, repository, **fields) as rows:
                yield rows

        def rule_load(path: Path, *args: object, **kwargs: object) -> object:
            self.rule_paths.add(str(path))
            self.counters["rule_scans"] += 1
            with self.timed("external_reads"):
                return original_rule_load(path, *args, **kwargs)

        try:
            with ExitStack() as patches:
                _ = patches.enter_context(patch.object(sqlite3, "connect", connect))
                _ = patches.enter_context(patch.object(subprocess.Popen, "__init__", process_start))
                for name in ("communicate", "wait", "poll"):
                    function = cast(Callable[..., object], getattr(subprocess.Popen, name))
                    _ = patches.enter_context(patch.object(subprocess.Popen, name, process_completion(function)))
                _ = patches.enter_context(patch.object(Path, "read_text", self.wrapper("external_reads", Path.read_text)))
                _ = patches.enter_context(patch.object(Path, "read_bytes", self.wrapper("external_reads", Path.read_bytes)))
                _ = patches.enter_context(patch.object(style_reader.ReaderClient, "_request", request))
                _ = patches.enter_context(patch.object(rules, "_load_rule", rule_load))
                _ = patches.enter_context(patch.object(rules, "load_rules", self.wrapper("external_reads", rules.load_rules, "rule_catalogs")))
                for name, counter in (("_matching_items", "declaration_searches"), ("_matches_declaration", "declaration_comparisons")):
                    function = cast(Callable[..., object], getattr(style_ledger.Ledger, name))
                    _ = patches.enter_context(patch.object(style_ledger.Ledger, name, self.wrapper("selection_compute", function, counter)))
                for module, name in ((refill_findings, "encode_proof"), (refill_facts, "encode_enumeration"), (refill_inputs, "input_identity")):
                    function = cast(Callable[..., object], getattr(module, name))
                    for consumer in tuple(sys.modules.values()):
                        if consumer and getattr(consumer, "__name__", "").startswith("scripts."):
                            for alias, value in tuple(cast(dict[str, object], vars(consumer)).items()):
                                if value is function:
                                    _ = patches.enter_context(patch.object(consumer, alias, self.wrapper("derived_encoding", function)))
                yield
        finally:
            _active_measurements = _UNMEASURED_RUN


class MeasureArguments(argparse.Namespace):
    """The repository and private workspace of one measurement."""

    repo: Path = Path()
    ledger: Path = Path()
    unit: str = ""
    reader: style_config.ReaderSource = style_config.ReaderSource.PYTHON
    workspace: Path | None = None
    worker: bool = False
    stages: bool = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--repo", type=Path, required=True)
    _ = parser.add_argument("--ledger", type=Path, required=True)
    _ = parser.add_argument("--unit", required=True)
    _ = parser.add_argument("--reader", type=style_config.ReaderSource, choices=list(style_config.ReaderSource),
                        default=style_config.ReaderSource.PYTHON)
    _ = parser.add_argument("--workspace", type=Path)
    _ = parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--stages", action="store_true", help="Measure stages inside the private worker")
    args = parser.parse_args(namespace=MeasureArguments())
    if args.worker:
        measurements = StageMeasurements()
        measurements.seconds["python_start"] = time.perf_counter() - float(os.environ.get("REFILL_MEASURE_STARTED", str(time.perf_counter())))
        measurements.calls["python_start"] = 1
        with redirect_stdout(io.StringIO()), measurements.installed(args.stages):
            digest = refill(args.repo, args.ledger, args.unit)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        written = process_usage(os.getpid())[1]
        print(json.dumps({"result_digest": digest, "write_bytes": written, "peak_rss_bytes": rss,
                          "reader": style_config.refill_reader().value,
                          **({"stages": measurements.report()} if args.stages else {})}))
    else:
        if args.workspace is None:
            parser.error("--workspace must name a private scratch folder")
        print(json.dumps(measure(args.repo, args.ledger, args.reader, args.unit, args.workspace, stages=args.stages), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
