"""Measure cold and warm refill reads on a private ledger backup.

The measurement uses next-phase's selection code in COUNT mode and its candidate
reader. It does not assign tickets, change plans, or change proposal files.
RSS and descendant IO are sampled every 10 milliseconds on Linux. Short-lived
descendants can finish between samples. Each worker also reports its own final
IO counters and kernel RSS high-water mark.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from contextlib import closing, redirect_stdout
from dataclasses import asdict, is_dataclass
from enum import Enum
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
from typing import cast

from scripts.lib import berth_board
from scripts.style import rules, style_config, style_ledger, style_plan, style_proposals
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


def clear_derived(database: Path) -> tuple[str, ...]:
    """Remove saved refill results and generated candidate listings."""
    with closing(sqlite3.connect(database)) as connection, connection:
        tables = cast(list[tuple[str]], connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall())
        selected = tuple(name for (name,) in tables if name.startswith("refill_")
                         or name in {"listed_rules", "listed_sites"})
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


def measured_run(repo: Path, database: Path, unit: str, environment: Mapping[str, str]) -> dict[str, object]:
    """Measure a fresh process so a cold or warm run has its own memory peak."""
    command = [sys.executable, "-m", "scripts.style.refill_measure", "--worker", "--repo", str(repo),
               "--ledger", str(database), "--unit", unit]
    started = time.perf_counter()
    with subprocess.Popen(command, env=dict(environment), stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
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
            "stored": stored_rows(database), "sampling_seconds": 0.01}


def measure(repo: Path, live_ledger: Path, reader: style_config.ReaderSource, unit: str, workspace: Path) -> dict[str, object]:
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
        switch = config / "style_reader.conf"
        text = switch.read_text()
        lines: list[str] = []
        for line in text.splitlines():
            setting = line.partition("=")[0].strip()
            lines.append(f"{setting}={reader.value}" if "=" in line and (setting == "reader" or setting.startswith("reader."))
                         else line)
        lines.append(f"reader={reader.value}")
        _ = switch.write_text("\n".join(lines) + "\n")
        environment = dict(os.environ, CONF_DIR=str(config), STYLE_LEDGER_STATE_DIR=str(private / "state"))
        cold = measured_run(repo, database, unit, environment)
        warm = measured_run(repo, database, unit, environment)
        return {"reader": reader.value, "cleared_tables": removed, "cold": cold, "warm": warm,
                "same_result": cold["result_digest"] == warm["result_digest"]}


class MeasureArguments(argparse.Namespace):
    """The repository and private workspace of one measurement."""

    repo: Path = Path()
    ledger: Path = Path()
    unit: str = ""
    reader: style_config.ReaderSource = style_config.ReaderSource.PYTHON
    workspace: Path | None = None
    worker: bool = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--repo", type=Path, required=True)
    _ = parser.add_argument("--ledger", type=Path, required=True)
    _ = parser.add_argument("--unit", required=True)
    _ = parser.add_argument("--reader", type=style_config.ReaderSource, choices=list(style_config.ReaderSource),
                        default=style_config.ReaderSource.PYTHON)
    _ = parser.add_argument("--workspace", type=Path)
    _ = parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(namespace=MeasureArguments())
    if args.worker:
        with redirect_stdout(io.StringIO()):
            digest = refill(args.repo, args.ledger, args.unit)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        written = process_usage(os.getpid())[1]
        print(json.dumps({"result_digest": digest, "write_bytes": written, "peak_rss_bytes": rss,
                          "reader": style_config.refill_reader().value}))
    else:
        if args.workspace is None:
            parser.error("--workspace must name a private scratch folder")
        print(json.dumps(measure(args.repo, args.ledger, args.reader, args.unit, args.workspace), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
