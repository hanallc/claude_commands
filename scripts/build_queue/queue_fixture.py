"""The 10:46 queue, stored as admission inputs rather than renderer decisions."""

from __future__ import annotations

from scripts.build_queue import admission_records
from scripts.lib import test_phase

from contextlib import ExitStack
from datetime import UTC, datetime
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from unittest import mock
from typing import TYPE_CHECKING, cast

from scripts.build_queue import snapshot_codec as codec
from scripts.buildlog import store
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import admission_policy_fixture, fake_memory_readings, write_config_fixture
from scripts.build_queue.scope_activity import IdleService, RunningServiceJob, NotServiceSlice, LiveBuildRun

if TYPE_CHECKING:
    from scripts.build_queue import queue_view
    from scripts.message.unit_labels import UnitLabels

NOW = datetime(2026, 10, 30, 14, 46, 12, tzinfo=UTC).timestamp()

# production, unit, session, step, need in GiB, minutes waited, folder
STEPS = (
    ("tool-based-ui", "cables", "work-lead", "nextest", 11.5, 6, "/tmp/work"),
    ("tool-based-ui", "cables", "work-lead", "nextest", 11.5, 6, "/tmp/measure-repair2"),
    ("tool-based-ui", "hana-migration", "hana-migration", "mend", 7.6, 39, "/tmp/hana_catalyst-hana-migration"),
    ("cargo-liner", "berth", "cargo-liner-berth-parallel-impl", "clippy", 2.9, 37, "/home/natepiano/rust/cargo-liner/repo"),
    ("tool-based-ui", "cables", "work-pan", "mend", 7.6, 36, "/tmp/tree"),
    ("tool-based-ui", "lfo", "hana_catalyst-lfo-impl6", "nextest", 10.8, 35, "/tmp/lfo-repo"),
    ("cargo-liner-style", "cargo-liner-style-modules", "cargo-liner-style-modules", "doc", 3.5, 35, "/tmp/style-modules"),
    ("cargo-liner-style", "—", "cargo-liner-style", "nextest", 1.8, 33, "/tmp/cargo-liner-style"),
    ("performance", "metrics-gathering", "tool-based-ui-frame-time-metrics-gatheri-test", "mend", 7.6, 32, "/tmp/frame-time"),
    ("tool-based-ui", "—", "hana", "clippy", 6.8, 31, "/tmp/hana_catalyst"),
    ("cargo-liner-style", "cargo-liner-style-types", "cargo-liner-style-types", "doc", 3.5, 31, "/tmp/style-types"),
    ("tool-based-ui", "geometry-material", "organon-w89", "nextest", 1.0, 30, "/tmp/base86"),
    ("tool-based-ui", "trunk", "tool-based-ui-trunk-portal", "doc", 4.4, 30, "/tmp/portal"),
    ("cargo-liner", "handler", "handler", "clippy", 3.0, 30, "/tmp/handler"),
    ("tool-based-ui", "demo", "tool-based-ui-demo-impl", "clippy", 6.8, 27, "/tmp/demo"),
    ("tool-based-ui", "trunk", "tool-based-ui-trunk-fix2", "doc", 4.4, 24, "/tmp/trunk-fix2"),
    ("cargo-liner", "port", "cargo-liner-port-impl", "mend", 3.2, 24, "/tmp/cargo-liner-port"),
    ("cargo-liner", "port", "cargo-liner-port-impl", "nextest", 1.8, 24, "/tmp/cargo-liner-port"),
    ("tool-based-ui", "inspectors", "hana_catalyst-inspectors-fix2", "nextest", 11.5, 21, "/tmp/inspectors"),
    ("tool-based-ui", "demo", "tool-based-ui-demo-impl3", "mend", 7.6, 20, "/tmp/demo3"),
    ("tool-based-ui", "startup", "tool-based-ui-startup-fix2", "nextest", 10.8, 14, "/tmp/land60"),
    ("tool-based-ui", "geometry-material", "organon-w74", "clippy", 6.1, 14, "/tmp/mergeclone"),
    ("tool-based-ui", "hana-migration", "hana-migration", "nextest", 12.1, 10, "/tmp/hana_catalyst-hana-migration"),
    ("performance", "metrics-gathering", "tool-based-ui-frame-time-metrics-gatheri-test", "nextest", 12.1, 10, "/tmp/frame-time"),
    ("cargo-liner", "handler", "handler", "nextest", 1.8, 9, "/tmp/handler"),
    ("performance", "metrics-gathering", "tool-based-ui-frame-time-metrics-gatheri-test", "nextest", 8.5, 8, "/tmp/frame-time-second"),
    ("performance", "benchmark", "benchmark", "nextest", 12.1, 8, "/tmp/benchmark"),
    ("cargo-liner", "tile", "cargo-liner-tile-fixes-impl", "nextest", 10.8, 6, "/tmp/tile"),
    ("tool-based-ui", "lfo", "hana_catalyst-lfo-test2", "nextest", 1.0, 3, "/tmp/lfo-test"),
    ("tool-based-ui", "trunk", "tool-based-ui-trunk-fix3", "nextest", 10.8, 2, "/tmp/trunk-fix3"),
    ("tool-based-ui", "fastest", "hana_catalyst-build-speed-tests-test", "clippy", 6.8, 1, "/tmp/fastest"),
    ("tool-based-ui", "assembly", "hana_catalyst-assembly-impl", "clippy", 6.8, 0, "/tmp/assembly"),
)


def write_memory_config(path: Path, **changes: str | None) -> None:
    """Write one complete, private memory configuration for admission consumers."""
    _ = write_config_fixture(path, template="build_admission")
    lines = [line for line in path.read_text().splitlines()
             if not line.startswith("peak_hold_seconds.")
             and (not line.startswith("program_first_peak_gib.")
                  or line.startswith("program_first_peak_gib.cargo="))]
    for key, value in changes.items():
        indices = [index for index, line in enumerate(lines) if line.partition("=")[0].strip() == key]
        if indices:
            index = indices[0]
            if value is None:
                del lines[index]
            else:
                lines[index] = f"{key}={value}"
        elif value is not None:
            form = "whole: 1.." if key.startswith("peak_hold_seconds.") else "range: 5e-324..1.6742321987285425e299"
            lines.extend((f"# {form}", f"{key}={value}"))
    _ = path.write_text("\n".join(lines) + "\n")


HEAD_NOW = datetime(2026, 10, 30, 11, 0, tzinfo=UTC).timestamp()


def write_process(proc: Path, pid: int, used: int = 0, *, parent: int = 1,
                  start: str | None = None, anonymous: int | None = None, swap: int = 0) -> None:
    """Write private process identity, resident use, anonymous use, and swap."""
    directory = proc / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    fields = ["S", str(parent), *(["0"] * 17), str(pid) if start is None else start]
    if (directory / "stat").write_text(f"{pid} (verify) {' '.join(fields)}\n") <= 0:
        raise OSError("empty fixture process")
    anon = used if anonymous is None else anonymous
    if (directory / "status").write_text(
            f"VmRSS: {used // 1024} kB\nRssAnon: {anon // 1024} kB\nRssShmem: 0 kB\nVmSwap: {swap // 1024} kB\n") <= 0:
        raise OSError("empty fixture memory")


def write_scope(directory: Path, *, members: tuple[int, ...] = (), current: int = 0,
                anonymous: int = 0, swap: int = 0) -> None:
    """Write one private scope with complete memory readings."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in {
        "cgroup.procs": "\n".join(map(str, members)), "cgroup.events": "populated 1\n",
        "memory.current": str(current), "memory.stat": f"anon {anonymous}\nshmem 0\ninactive_file 0\n",
        "memory.swap.current": str(swap),
    }.items():
        _ = (directory / name).write_text(text)


def fixture_reservation(pid: int, need: int, *, step: str = "check", folder: str = "/fixture") -> admission_records.Reservation:
    """Build the reservation evidence for one fixture step."""
    return {"pid": pid, "start_time": str(pid), "need": need, "repo": "fixture",
            "step": step, "worktree": folder, "admitted_at": "", "sidecar": "",
            "test_threads": admission_records.NoTestWidth(), "call_id": test_phase.NoCallRecord()}


def blocking_slot(now: float, held: int = 0) -> admission_records.BetweenStepsSlot:
    """Prevent the first-run exception without another active step."""
    return admission_records.BetweenStepsSlot(LiveBuildRun(999, "fixture", now), "fixture", held, now + 10)


def queue_read_fixture(view: admission_records.QueueView, policy: admission_records.AdmissionPolicy) -> admission_records.QueueRead:
    """Build queue evidence before the shared pass selects each reason."""
    ordered: list[tuple[Path, admission_records.RankedWaiter]] = []
    for step in view.waiting:
        row: admission_records.RankedWaiter = {
            "pid": step.pid, "start_time": str(step.pid), "need": step.need, "since": step.since,
            "session_id": step.session_id, "session_name": step.session_name,
            "step": step.step, "folder": step.folder, "order": admission_records.NormalOrder(),
            "unit": step.unit, "build_run": step.run,
        }
        if isinstance(step.reason, admission_records.TokenWaitingWaiter):
            row["token_left_at"] = step.reason.left_at
        ordered.append((Path(f"{step.pid}-{step.pid}.waiting"), row))
    reservations = tuple(fixture_reservation(index, step.promise, step=step.step, folder=step.folder)
                         for index, step in enumerate(view.running, 1) if gate.running_has_reservation(step))
    host: admission_records.HostMemory | admission_records.ReadingUnavailable = admission_records.HostMemory(view.available, admission_records.UnknownHostTotal())
    pressure: admission_records.Pressure = admission_records.NoPressureHold()
    spacing: admission_records.StartSpacing = admission_records.ReadyToStart()
    if view.waiting:
        first = view.waiting[0]
        reason = first.reason.cause if isinstance(first.reason, admission_records.HeadAdmissionStatus) else first.reason
        if isinstance(reason, admission_records.ReadingUnavailable):
            host = reason
        elif isinstance(reason, (admission_records.MemoryPressure, admission_records.PressureUnavailable)):
            pressure = reason
        elif isinstance(reason, admission_records.StartSpacingWait):
            spacing = admission_records.NextStartAt(reason.at)
        elif isinstance(reason, admission_records.EstimateExpired):
            path, row = ordered[0]
            ordered[0] = (path, {**row, "estimated_at": reason.estimated_at})
    slots: tuple[admission_records.RunSlot, ...] = ()
    if view.waiting and not reservations:
        slots = (blocking_slot(view.now, view.promised),)
    inputs = admission_records.QueueInputs(ordered, view.running, reservations, host, pressure, spacing,
                              view.now, slots=slots)
    return gate.queue_read_from(inputs, policy)


def head_reading(*, since: float = HEAD_NOW - 600, now: float = HEAD_NOW,
                 fields: dict[str, object] | None = None, step_name: str = "nextest") -> admission_records.QueueRead:
    step = admission_records.WaitingStep(123, "head-session", "head", step_name, "/tmp/head", since,
                            admission_records.GIB, admission_records.ReadyForAdmission())
    reading = queue_read_fixture(admission_records.QueueView((step,), (), 30 * admission_records.GIB, 0, 3 * admission_records.GIB, now), policy=admission_policy_fixture())
    row = cast(admission_records.RankedWaiter, cast(object, {"pid": step.pid, "start_time": "123", "need": step.need,
                            "since": since, "session_id": step.session_id,
                            "session_name": step.session_name, "step": step.step,
                            "folder": step.folder, "order": admission_records.NormalOrder(), **(fields or {})}))
    return replace(reading, ordered=[(Path("123-123.waiting"), row)])


def fixture_queue_inputs(directory: Path, processes: dict[int, gate.ProcessIdentity], now: float,
                         policy: admission_records.AdmissionPolicy) -> admission_records.QueueInputs:
    """Read captured fixture rows through the import readers and the shared builder."""
    try:
        host = gate.read_meminfo(Path(os.environ["BUILDLOG_MEMINFO"]))
    except (OSError, ValueError):
        host = admission_records.ReadingUnavailable()
    rows: list[tuple[Path, admission_records.Waiter]] = []
    for path in directory.glob("*.waiting"):
        try:
            rows.append((path, gate.read_waiter(path)))
        except (OSError, ValueError, TypeError):
            continue
    ordered, priority = gate.resolve_queue_order(directory, rows)
    reservations = [(row, gate.reservation_memory(path, row, datetime.fromtimestamp(now, UTC),
                     processes, Path(os.environ["BUILDLOG_PROC"]), policy))
                    for path, row in sorted(gate.reservation_rows(directory), key=lambda item: item[0])]
    readings = gate.QueueSnapshotReadings(processes, {}, host=host,
        pressure=gate.read_pressure(host if isinstance(host, admission_records.HostMemory) else admission_records.HostMemory(0, admission_records.UnknownHostTotal()), policy=policy), spacing=admission_records.ReadyToStart(),
        compilers=gate.sccache_memory(), policy=policy)
    return gate.build_queue_inputs(ordered, reservations, readings, now, priority)


def fixture_queue_read(directory: Path, processes: dict[int, gate.ProcessIdentity], now: float) -> admission_records.QueueRead:
    config = admission_records.read_memory_config()
    inputs = fixture_queue_inputs(directory, processes, now, config.policy)
    return gate.queue_read_from(inputs, config.policy)


class QueueFixture(ExitStack):
    """Private files for every renderer, card and CLI fixture consumer."""

    def __init__(self, *, captured: bool = True) -> None:
        from scripts.message import roster
        from scripts.message.unit_labels import UnitLabels

        super().__init__()
        _ = self.enter_context(mock.patch.object(store, "host_name", return_value="fixture-host"))
        self.root: Path = Path(self.enter_context(tempfile.TemporaryDirectory()))
        self.directory: Path = self.root / "buildlog/admission"
        self.proc: Path = self.root / "proc"
        self.directory.mkdir(parents=True)
        self.proc.mkdir()
        environment = fake_memory_readings(self.root, available_kib=int(24.5 * admission_records.GIB) // 1024,
                                            total_kib=60 * admission_records.GIB // 1024, full_avg10=7.7)
        environment.update({"BUILDLOG_DIR": str(self.root / "buildlog"), "BUILDLOG_PROC": str(self.proc),
                            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
                            "USER_ZONE": "America/New_York"})
        self.enter_context(mock.patch.dict(os.environ, environment))
        config = self.root / "build_admission.conf"
        write_memory_config(config)
        self.enter_context(mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent)}))
        self.labels: dict[str, UnitLabels] = {}
        _ = self.enter_context(mock.patch.object(gate, "priority_labels", return_value=self.labels))
        _ = self.enter_context(mock.patch.object(roster, "roster",
            side_effect=AssertionError("a queue fixture must not read the live roster")))
        self.paths: list[Path] = []
        self.reservation: Path = self.directory / "900-900.reservation"
        if not captured:
            return
        for position, (production, unit, session, step, need, waited, folder) in enumerate(STEPS, 1):
            pid = 1000 + position
            # Session identities repeat exactly where the captured queue does.
            session_id = "id-" + session
            self.labels[session_id] = UnitLabels(session, production, unit)
            since = NOW - waited * 60 - 12 + (position / 100 if position > 2 else 0)
            row: dict[str, object] = {"pid": pid, "start_time": str(pid), "need": int(need * admission_records.GIB),
                "since": since, "session_id": session_id, "session_name": session,
                "step": step, "folder": folder}
            if position <= 2:
                row.update(priority_since=NOW - 12)
            self.paths.append(self.write_waiter(row))
        self.labels["id-widget"] = UnitLabels("widget-enhancements-title", "tool-based-ui", "widget")
        self.labels["id-widget-director"] = UnitLabels("widget", "tool-based-ui", "widget")
        self.write_running(used=int(5.1 * admission_records.GIB), need=int(11.1 * admission_records.GIB))
        self.write_json(self.directory / "queue_priority.json", {"unit": "cables", "why": "captured priority",
            "set_at": NOW - 12, "set_by": "user", "transition": codec.snapshot_value(admission_records.SettledPriority())})

    @staticmethod
    def write_json(path: Path, row: dict[str, object]) -> None:
        if path.write_text(json.dumps(row, separators=(",", ":"))) <= 0:
            raise OSError("empty fixture row")

    def write_running(self, *, used: int, need: int, step: str = "nextest") -> None:
        write_process(self.proc, 900, used)
        scope = self.root / "widget.scope"
        write_scope(scope, members=(900,), current=used, anonymous=used)
        sidecar = self.root / "widget.scope-name"
        _ = sidecar.write_text(str(scope))
        self.write_json(self.reservation, {"pid": 900, "start_time": "900", "need": need,
            "admitted_at": store.utc_iso(NOW - 25), "repo": "hana", "worktree": "/tmp/widget",
            "sidecar": str(sidecar), "session_id": "id-widget", "session_name": "widget-enhancements-title",
            "step": step, "folder": "/tmp/widget", "test_threads": None, "call_id": None,
            "queue_waiter": {"pid": 900, "start_time": "900", "need": need, "since": NOW - 25,
                "session_id": "id-widget", "session_name": "widget-enhancements-title",
                "step": step, "folder": "/tmp/widget"}})

    def write_waiter(self, row: dict[str, object]) -> Path:
        pid = int(str(row["pid"]))
        write_process(self.proc, pid)
        path = self.directory / f"{pid}-{row['start_time']}.waiting"
        self.write_json(path, row)
        return path

    def waiter(self, pid: int, session: str, name: str, step: str = "nextest") -> Path:
        return self.write_waiter({"pid": pid, "start_time": str(pid), "need": 12 * admission_records.GIB,
            "since": NOW + pid, "session_id": session, "session_name": name,
            "step": step, "folder": f"/tmp/tree-{pid}"})

    def change(self, position: int, **fields: object) -> None:
        path = self.paths[position - 1]
        row = cast(dict[str, object], json.loads(path.read_text()))
        row.update(fields)
        self.write_json(path, row)


    def inputs(self, policy: admission_records.AdmissionPolicy) -> admission_records.QueueInputs:
        """Resolve the fixed 10:46 queue once, including raw pressure."""
        return fixture_queue_inputs(self.directory, gate.process_table(self.proc), NOW, policy)

    def read(self) -> admission_records.QueueRead:
        return fixture_queue_read(self.directory, gate.process_table(self.proc), NOW)

    def model(self) -> queue_view.QueueModel:
        from scripts.build_queue import queue_view

        return queue_view.QueueModel(self.read(), self.labels)


def ci_step(bytes: int, job_state: RunningServiceJob | IdleService | NotServiceSlice) -> admission_records.RunningStep:
    """Build one CI service reading without an admission reservation."""
    return admission_records.RunningStep("ci", "ci", "service", "/ci", 0,
        admission_records.UnownedSliceMemory(bytes, owner="hana-ci.slice", job_state=job_state, pid=admission_records.PidNotRecorded()),
        pid=admission_records.PidNotRecorded())
