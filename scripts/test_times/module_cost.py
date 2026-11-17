#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Measure module CPU, process starts, successful execs and wall time."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
from collections.abc import Iterable
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import time
from typing import Literal, TypedDict, cast

from scripts.lib import build_folders, conf
from scripts.lib.descendants import adopt_descendants
from scripts.lib.terminal_cells import print_table, seconds
from scripts.production.merge_gate import test_module_environment
from scripts.test_times import task_cpu


@dataclass(frozen=True)
class ProcessStarts:
    processes: int
    execs: int


def count_starts(lines: Iterable[str]) -> ProcessStarts:
    processes = execs = 0
    unfinished: dict[str, str] = {}
    for line in lines:
        prefix = re.match(r"^(?:\[pid\s+(\d+)\]\s+|(\d+)\s+)?(.*)$", line.strip())
        if prefix is None:
            continue
        pid = prefix[1] or prefix[2] or ""
        call = prefix[3]
        if call.endswith("<unfinished ...>"):
            unfinished[pid] = call.removesuffix("<unfinished ...>")
            continue
        resumed = re.match(r"<\.\.\. \w+ resumed>(.*)", call)
        if resumed:
            call = unfinished.pop(pid, "") + resumed[1]
        completed = re.match(r"(fork|vfork|clone|clone3|execve)\(.*\)\s+=\s+(-?\d+)(?:\s|$)", call)
        if completed is None:
            continue
        operation, result = completed[1], int(completed[2])
        if operation == "execve":
            execs += int(result == 0)
        elif result > 0 and "CLONE_THREAD" not in call:
            processes += 1
    return ProcessStarts(processes, execs)


class MeasuredTest(TypedDict):
    test: str
    kind: Literal["test", "fixture"]
    cpu_s: float
    s: float


class TimeReport(TypedDict):
    tests: list[MeasuredTest]
    fixtures: list[MeasuredTest]


@dataclass(frozen=True)
class ModuleCost:
    module: str
    cpu_s: float
    starts: ProcessStarts
    wall_s: float
    test_status: int
    trace_status: int
    tests: list[MeasuredTest]

    @property
    def passed(self) -> bool:
        return self.test_status == self.trace_status == 0

    def row(self) -> list[str]:
        status = "PASS" if self.passed else f"RED (test={self.test_status}, trace={self.trace_status})"
        return [self.module, seconds(self.cpu_s), str(self.starts.processes), str(self.starts.execs),
                seconds(self.wall_s), status]


def runner_command(tree: Path, module: str) -> list[str]:
    folder, filename = module.rsplit("/", 1)
    return [sys.executable, str(tree / "scripts/test_times/module_run.py"), f"scripts/{folder}", filename]


def reap_stage_children(child: subprocess.Popen[str]) -> None:
    """Reap exited children, including orphans that this process adopted."""
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if not pid:
            return
        if pid == child.pid:
            child.returncode = os.waitstatus_to_exitcode(status)


def wait_for_stage(child: subprocess.Popen[str], timeout_s: float) -> tuple[int, float]:
    """Return the stage status and CPU after a bounded wait."""
    deadline = time.monotonic() + timeout_s
    pending: tuple[str, ...] = ()
    descriptor = os.pidfd_open(child.pid)
    try:
        ended = select.poll()
        ended.register(descriptor, select.POLLIN)
        if ended.poll(math.ceil(max(0, deadline - time.monotonic()) * 1000)):
            pid, status, usage = os.wait4(child.pid, os.WNOHANG)
            if pid:
                child.returncode = os.waitstatus_to_exitcode(status)
                return child.returncode, usage.ru_utime + usage.ru_stime
    finally:
        os.close(descriptor)
        try:
            if child.returncode is None:
                pending = task_cpu.stop_pending()
        finally:
            reap_stage_children(child)
    raise task_cpu.MeasurementTimeout(timeout_s, pending)


def measure(tree: Path, module: str, strace: str, timeout_s: float) -> ModuleCost:
    command = runner_command(tree, module)
    with build_folders.tool_folder("module-cost") as temporary:
        root = Path(temporary)
        environment = test_module_environment(root, dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        report = root / "times.json"
        environment["TEST_TIMES_OUT"] = str(report)
        lock_fd = build_folders.build_folder_lock_fd(root)
        with (root / "time.log").open("w+") as output:
            print(f"{module}: starting time run", file=sys.stderr, flush=True)
            started = time.perf_counter()
            child = subprocess.Popen(command, cwd=tree, env=environment,
                                     stdout=output, stderr=subprocess.STDOUT, text=True,
                                     pass_fds=(lock_fd,), start_new_session=True)
            test_status, cpu_s = wait_for_stage(child, timeout_s)
            wall_s = time.perf_counter() - started
            if test_status:
                _ = output.seek(0)
                _ = shutil.copyfileobj(output, sys.stderr)
        tests: list[MeasuredTest] = []
        if report.is_file():
            data = cast(TimeReport, json.loads(report.read_text()))
            tests = [row for row in data["tests"] if row["kind"] == "test"] + data["fixtures"]
        del environment["TEST_TIMES_OUT"]
        trace = root / "starts.trace"
        trace.touch()
        with (root / "trace.log").open("w+") as output:
            print(f"{module}: starting trace run", file=sys.stderr, flush=True)
            traced = subprocess.Popen(
                [strace, "--kill-on-exit", "-f", "-e", "trace=fork,vfork,clone,clone3,execve", "-o", str(trace),
                 *command],
                cwd=tree, env=environment, stdout=output, stderr=subprocess.STDOUT, text=True,
                pass_fds=(lock_fd,), start_new_session=True,
            )
            trace_status, _ = wait_for_stage(traced, timeout_s)
            if trace_status:
                _ = output.seek(0)
                _ = shutil.copyfileobj(output, sys.stderr)
        with trace.open() as lines:
            starts = count_starts(lines)
    return ModuleCost(module, cpu_s, starts, wall_s, test_status, trace_status, tests)


@dataclass(init=False)
class CostArguments(argparse.Namespace):
    tree: Path
    modules: list[str]
    tests: int
    commands: int


def main(argv: list[str] | None = None) -> int:
    _ = adopt_descendants()
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--tree", type=Path, required=True)
    _ = parser.add_argument("--tests", type=int, default=0, metavar="N")
    _ = parser.add_argument("--commands", type=int, default=0, metavar="N")
    _ = parser.add_argument("modules", nargs="+", metavar="folder/file")
    request = parser.parse_args(argv, namespace=CostArguments())
    strace = shutil.which("strace")
    if strace is None:
        print("module_cost: strace is not on PATH", file=sys.stderr)
        return 2
    try:
        return report_costs(request, strace, task_cpu.measurement_timeout_s())
    except (conf.Unusable, task_cpu.MeasurementTimeout, task_cpu.LeftoverProcesses) as error:
        print(f"module_cost: {error}", file=sys.stderr, flush=True)
        return 1


def report_costs(request: CostArguments, strace: str, timeout_s: float) -> int:
    tree = request.tree.resolve()
    costs = [measure(tree, module, strace, timeout_s) for module in request.modules]
    passed = all(cost.passed for cost in costs)
    total = ["total", seconds(sum(cost.cpu_s for cost in costs)),
             str(sum(cost.starts.processes for cost in costs)), str(sum(cost.starts.execs for cost in costs)),
             seconds(sum(cost.wall_s for cost in costs)), "PASS" if passed else "RED"]
    print_table(["module", "CPU s", "processes", "execs", "wall s", "test status"],
                [*[cost.row() for cost in costs], total])
    print("Counts include the module_run supervisor: one extra process and no extra exec.")
    if request.tests:
        rows = [[cost.module, row["test"], row["kind"], seconds(row["cpu_s"]), seconds(row["s"])]
                for cost in costs
                for row in sorted(cost.tests, key=lambda row: (-row["cpu_s"], row["test"]))[:request.tests]]
        print_table(["module", "test or fixture", "kind", "CPU s", "wall s"], rows)
    if request.commands:
        for cost in costs:
            command = runner_command(tree, cost.module)
            with build_folders.tool_folder("module-cost-commands") as temporary:
                environment = test_module_environment(Path(temporary), dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
                _ = environment.pop("TEST_TIMES_OUT", None)
                print(f"{cost.module}: starting command run", file=sys.stderr, flush=True)
                tasks = task_cpu.measure(command, cwd=tree, environment=environment, timeout_s=timeout_s,
                                         pass_fds=(build_folders.build_folder_lock_fd(temporary),))
            print_table([cost.module + " command", "starts", "CPU s"],
                        [[row.command, str(row.starts), seconds(row.cpu_s)]
                         for row in tasks.commands[:request.commands]])
            print(f"unassigned {tasks.cpu_s - sum(row.cpu_s for row in tasks.commands):+.6f} CPU s")
            passed = passed and tasks.status == 0
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
