"""The fixed queue stays one row per step and explains waits once."""
from __future__ import annotations

from scripts.build_queue import admission_records

from scripts.lint.memory_test_support import write_pressure_reading

from dataclasses import replace
import json
import os
import shlex
import struct
import subprocess
import time
import unittest
from typing import cast, override
from unittest import mock

from scripts.build_queue import queue_view, queue_evidence
from scripts.build_queue import admission_protocol as wire, cloud_progress, progress_profile
from scripts.build_queue import placement
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue.queue_fixture import NOW, QueueFixture
from scripts.lint import memory_admit as gate
from scripts.message.unit_labels import UnitLabels
from scripts.message import unit_labels
from scripts.build_queue.queue_fixture import write_memory_config


class RecordedOwnerQueueTests(unittest.TestCase):
    def test_repository_slot_wait_is_shown_without_a_memory_need(self) -> None:
        with QueueFixture() as fixture:
            model = fixture.model()
            base = model.reading
            slot = wire.RepositorySlotRow(wire.Owner(701001, "start"),
                wire.Identity("slot-session", "slot-showrunner", "gate", "proof", "slot-unit"),
                base.explanation.now - 60, "natedev",
                wire.RepositorySlotWaiting("widget", (1, 2), 2, "slots.natedev.widget.max"))
            reading = wire.CloudQueueRead(base.view, base.explanation, base.ordered, base.diagnostics,
                cloud_gate_stale_pushes=3, repository_slots=(slot,))
            model = replace(model, reading=reading)
            self.assertEqual(reading.explanation.totals, base.explanation.totals)
            self.assertEqual(reading.explanation.room, base.explanation.room)
            self.assertIn(slot, queue_view.waiting_builds(model))


    def test_detached_and_systemd_rows_resolve_the_recorded_staff_owner(self) -> None:
        from scripts.build_queue import admission_client
        from scripts.production import staff
        from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, isolated_staff_file

        with QueueFixture() as fixture, isolated_staff_file(fixture.root / "staff.json", {}):
            runner = fixture_member("showrunner", name="hana")
            unit = fixture_member("unit", name="demo", parent=runner.id)
            worker = fixture_member("worker", name="impl", parent=unit.id)
            current = fixture_runtime_id("owner-after-session-restart")
            worker = staff.update_current_session(worker.current_session, staff.native_id(current))
            model = fixture.model()
            for source, owner, ran_by in (("detached", worker, unit_labels.Runner.WORKER),
                                          ("systemd", unit, unit_labels.Runner.UNIT_DIRECTOR)):
                with self.subTest(source=source), mock.patch.dict(os.environ, {
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_NAME": "",
                    "BUILDLOG_UNIT": "", "BUILDLOG_OWNER_STAFF_ID": str(owner.id),
                }), mock.patch.object(unit_labels, "newest_sessions", return_value=[]):
                    identity = admission_client.step_identity("test-affected", "/private/tree")
                    self.assertEqual(identity.session_id, owner.current_session)
                    labels = unit_labels.unit_labels_for([identity.session_id], os.environ)
                    waiter = replace(model.reading.view.waiting[0], session_id=identity.session_id,
                                     session_name=identity.name, unit=identity.unit)
                    running = replace(model.reading.view.running[0], session_id=identity.session_id,
                                      session_name=identity.name, unit=identity.unit)
                    for step in (waiter, running):
                        expected = UnitLabels(owner.name, "hana", "demo", unit_labels.OwnerRole.UNIT, ran_by)
                        self.assertEqual(queue_evidence.step_labels(step, labels), expected)
                    cloud = replace(cloud_reading(model.reading).cloud[0], identity=identity)
                    self.assertEqual(queue_view.cloud_step_labels(cloud, replace(model, labels=labels)),
                                     expected)

    def test_live_session_identity_takes_priority_over_the_recorded_staff_owner(self) -> None:
        from scripts.build_queue import admission_client

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "live-session", "CLAUDE_CODE_SESSION_ID": "",
                                         "BUILDLOG_OWNER_STAFF_ID": "other-owner"}):
            self.assertEqual(admission_client.step_identity("nextest", "").session_id, "live-session")

    def test_missing_owner_records_have_explicit_labels_on_every_destination(self) -> None:
        from scripts.build_queue import admission_client
        from scripts.production.staff_test_support import isolated_staff_file

        with QueueFixture() as fixture, isolated_staff_file(fixture.root / "staff.json", {}):
            model = fixture.model()
            for reference in ("", "missing-staff-row"):
                with self.subTest(reference=reference), mock.patch.dict(os.environ, {
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_NAME": "",
                    "BUILDLOG_UNIT": "", "BUILDLOG_OWNER_STAFF_ID": reference,
                }):
                    identity = admission_client.step_identity("test-affected", "")
                    self.assertEqual(identity.session_id, "")
                    for step in (model.reading.view.waiting[0], model.reading.view.running[0]):
                        label = queue_evidence.step_labels(replace(step, session_id="", session_name="", unit=""), {})
                        self.assertEqual((label.showrunner, label.unit), ("owner not recorded", "owner not recorded"))
                    cloud = replace(cloud_reading(model.reading).cloud[0], identity=identity)
                    label = queue_view.cloud_step_labels(cloud, replace(model, labels={}))
                    self.assertEqual((label.showrunner, label.unit), ("owner not recorded", "owner not recorded"))


def with_converted_executions(reading: admission_records.QueueRead) -> admission_records.QueueRead:
    """Keep a local charge beside host-scoped executions with equal process values."""
    local = reading.view.running[0]
    local = replace(local, execution=placement.ObservedExecution(
        placement.ProcessExecution("local-host", "local-generation", 900, "equal-start")), assignment_host="local-host")
    remote = tuple(replace(local, session_id=f"remote-{host}", session_name=f"remote-{host}",
        promise=0, measured=admission_records.RemoteMemoryUnavailable("remote memory is unavailable"), pid=900,
        execution=placement.ObservedExecution(placement.ProcessExecution(host, "remote-generation", 900, "equal-start")),
        assignment_host=host) for host in ("fixture-mac", "fixture-ssh"))
    uncertain = replace(remote[0], session_id="remote-uncertain", session_name="remote-uncertain",
        pid=admission_records.PidNotRecorded(), execution=placement.ExecutionEvidenceUnavailable("transport unavailable", NOW),
        assignment_host="uncertain-mac")
    explained = tuple(admission_records.ExplainedRunningStep(step, admission_records.AdmissionTimeNotRecorded(),
        0, 0, 0, owner=f"remote-{index}") for index, step in enumerate((*remote, uncertain)))
    running = (local, *reading.view.running[1:], *remote, uncertain)
    existing = tuple(replace(row, step=local) if row.step == reading.view.running[0] else row
                     for row in reading.explanation.running)
    return replace(reading, view=replace(reading.view, running=running),
                   explanation=replace(reading.explanation, running=(*existing, *explained)))


def cloud_reading(reading: admission_records.QueueRead) -> wire.CloudQueueRead:
    """Add running and waiting cloud work to a private queue reading."""
    now = reading.explanation.now
    rows = (
        wire.CloudRow(wire.Owner(700001, "100"),
            wire.Identity("cloud-session", "route-one", "gate", "/private/cloud", "cloud-unit"),
            wire.CloudPlace("aws"), now - 245, now - 240,
            cloud_progress.CloudProgress("running", 32, 1, now - 120),
            progress_profile.ProgressCounter("modules", 7, 12), admission_records.ReadyForAdmission()),
        wire.CloudRow(wire.Owner(700002, "200"),
            wire.Identity("cloud-waiter", "route-two", "test-affected", "/private/waiting", "waiting-unit"),
            wire.CloudPlace("aws"), now - 60, 0,
            cloud_progress.NoCloudProgress(), progress_profile.NoCounter(), wire.CloudSlotWait(1, 1)),
    )
    return wire.CloudQueueRead(reading.view, reading.explanation, reading.ordered,
        reading.diagnostics, rows, 1, cloud_gate_stale_pushes=3)


def empty_mac_reading(fixture: QueueFixture) -> None:
    """Keep existing renderer tests on a fresh private Mac reading."""
    path = fixture.root / "mac-builds.json"
    _ = path.write_text(json.dumps({"read_at": NOW, "builds": [], "error": ""}))
    _ = (fixture.root / "memory_feed.conf").write_text("# whole: 1..\nmac_activity_refresh_seconds=30\n")
    _ = fixture.enter_context(mock.patch.object(queue_view, "mac_reading_path", return_value=path))
    _ = fixture.enter_context(mock.patch.object(unit_labels, "unit_labels_for", return_value={}))


class MacQueueViewTests(unittest.TestCase):
    def test_gate_records_classify_process_trees_and_reject_reused_pids(self) -> None:
        with QueueFixture(captured=False) as fixture:
            records = fixture.root / ".local/state/mac-gate/records"
            records.mkdir(parents=True)
            stamp = "Mon Nov 9 12:00:00 2026"
            born = int(time.mktime(time.strptime(stamp, "%a %b %d %H:%M:%S %Y")))
            for pid, label in ((10, "placement episode/attempt"), (20, "user"), (100, "placement stale/attempt")):
                _ = (records / str(pid)).write_text(f"{pid}\t{stamp}\t{label}\n")
            processes = {pid: {"parent": parent, "uid": uid, "started_at": started,
                "resident_bytes": 1024, "command": command, "executable": "/bin/" + command.split()[0]}
                for pid, parent, uid, started, command in (
                    (10, 1, 501, born, "zsh"), (11, 10, 501, born + 1, "cargo build"),
                    (12, 11, 501, born + 2, "rustc --crate-name placed"),
                    (20, 1, 501, born, "zsh"), (21, 20, 501, born + 1, "cargo test"),
                    (100, 1, 501, born + 9, "cargo build"),
                    (40, 1, 502, born, "cargo-nextest run"),
                    (50, 1, 501, born, "rustc --version"),
                    (60, 1, 501, born, "sccache"), (61, 60, 501, born + 3, "rustc --crate-name served"),
                    (70, 1, 501, born, "cargo-tile build"), (71, 70, 501, born + 3, "rustc --crate-name captured"),
                    (80, 1, 501, born, "cargo build"), (81, 80, 501, born + 3, "rustc --crate-name environment"),
                    (90, 1, 501, born + 5, "cargo build"))}
            script = queue_view.mac_build_script().replace("print(json.dumps(read_reading()))",
                "from unittest.mock import patch\n"
                + f"read_processes = lambda: {processes!r}\n"
                + f"read_captures = lambda processes: {{70: dict(folder={str(fixture.root)!r}, command='cargo build', progress_tail='')}}\n"
                + "process_environment = lambda pid: {80: {'MAC_PLACEMENT': 'episode-2/attempt'}, 90: {'MAC_PLACEMENT': 'no placement'}}.get(pid, {})\n"
                + f"output = lambda arguments: 'p11\\nn{fixture.root}\\np21\\nn{fixture.root}\\np40\\nn{fixture.root}'\n"
                + "import types\n"
                + f"with patch.dict(os.environ, HOME={str(fixture.root)!r}), patch('pwd.getpwnam', return_value=types.SimpleNamespace(pw_uid=502)):\n"
                + "    print(json.dumps(read_reading()))")
            result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = cast(dict[str, list[dict[str, object]]], json.loads(result.stdout.removesuffix("rc=0\n")))
            self.assertEqual([row["pid"] for row in data["unplaced_runs"]], [50, 90, 100])
            self.assertEqual({row["pid"]: row["gate_label"] for row in data["builds"]},
                {11: "placement episode/attempt", 21: "user", 40: "hana-ci", 70: "unplaced"})
            _ = (fixture.root / "mac_test.conf").write_text("host=fixture-mac\n# range: 5e-324..\nconnect_timeout_s=2\n")
            path = fixture.root / "mac-builds.json"
            with (mock.patch.object(subprocess, "run", return_value=result),
                  mock.patch.object(queue_view, "mac_reading_path", return_value=path)):
                queue_view.refresh_mac_builds()
                reading = queue_view.read_mac_builds()
            self.assertEqual([run.pid for run in reading.unplaced_runs], [50, 90, 100])
            self.assertEqual([build.pid for build in reading.builds], [40, 70, 11, 21])

    @unittest.skipIf(os.geteuid() == 0, "Root can read a folder with mode 000.")
    def test_project_record_returns_empty_for_an_unreadable_folder(self) -> None:
        with QueueFixture(captured=False) as fixture:
            folder = fixture.root / "unreadable"
            folder.mkdir()
            folder.chmod(0)
            try:
                script = queue_view.mac_build_script().replace("print(json.dumps(read_reading()))",
                    f"print(json.dumps(project_record({str(folder)!r})))")
                result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, timeout=5)
            finally:
                folder.chmod(0o700)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(json.loads(result.stdout.removesuffix("rc=0\n")),
                             {"recorded_unit": "", "session": "", "repository": ""})

    def test_project_record_catches_each_filesystem_error(self) -> None:
        with QueueFixture(captured=False) as fixture:
            for operation in ("resolve", "is_symlink", "stat", "exists", "read_text"):
                with self.subTest(operation=operation):
                    record = fixture.root / ".copy-record.json"
                    if operation == "read_text":
                        _ = record.write_text('{}')
                    script = queue_view.mac_build_script().replace("print(json.dumps(read_reading()))",
                        "from unittest.mock import patch\n"
                        + f"with patch.object(Path, {operation!r}, side_effect=PermissionError('unreadable')):\n"
                        + f"    print(json.dumps(project_record({str(fixture.root)!r})))")
                    result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(json.loads(result.stdout.removesuffix("rc=0\n")),
                                     {"recorded_unit": "", "session": "", "repository": ""})

    def test_mac_reader_keeps_rows_when_one_row_field_fails(self) -> None:
        with QueueFixture(captured=False) as fixture:
            folders = {pid: str(fixture.root / str(pid)) for pid in (100, 200, 300)}
            processes = {pid: {"parent": 1, "command": "cargo build", "executable": "/bin/cargo",
                              "resident_bytes": pid, "started_at": NOW + pid} for pid in folders}
            captures = {pid: {"folder": folder, "command": "cargo build", "progress_tail": ""}
                        for pid, folder in folders.items()}
            for failed in ("project_record", "repository_name", "running_test_pid", "build_origin"):
                with self.subTest(failed=failed):
                    script = queue_view.mac_build_script().replace("print(json.dumps(read_reading()))",
                        f"read_processes = lambda: {processes!r}\n"
                        + f"read_captures = lambda processes: {captures!r}\n"
                        + "output = lambda arguments: ''\n"
                        + "def project_record(folder):\n"
                        + "    return dict(recorded_unit='unit', session='session', repository='')\n"
                        + "repository_name = lambda folder: 'repo'\n"
                        + "running_test_pid = lambda pid, *args: pid + 1\n"
                        + "build_origin = lambda pid, processes: 'ci'\n"
                        + f"original = {failed}\n"
                        + "def fail_one(value, *args):\n"
                        + f"    if value in (200, {folders[200]!r}):\n"
                        + "        raise PermissionError('unreadable row')\n"
                        + "    return original(value, *args)\n"
                        + f"{failed} = fail_one\nprint(json.dumps(read_builds()))")
                    result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    rows = cast(list[dict[str, object]], json.loads(result.stdout.removesuffix("rc=0\n")))
                    self.assertEqual([row["pid"] for row in rows], [100, 200, 300])
                    for row in rows:
                        bad = row["pid"] == 200
                        self.assertEqual(row["recorded_unit"], "" if bad and failed == "project_record" else "unit")
                        self.assertEqual(row["session"], "" if bad and failed == "project_record" else "session")
                        self.assertEqual(row["repository"], "" if bad and failed == "repository_name" else "repo")
                        self.assertEqual(row["test_pid"], -1 if bad and failed == "running_test_pid" else int(cast(int, row["pid"])) + 1)
                        self.assertEqual(row["origin"], "unknown" if bad and failed == "build_origin" else "ci")

    def test_mac_build_origin_survives_a_record_round_trip_and_old_records_stay_unknown(self) -> None:
        for origin in queue_view.MacBuildOrigin:
            for wire, expected in (("user", queue_view.MacGateLabel.USER),
                    ("placement e/a", queue_view.MacGateLabel.PLACEMENT), ("hana-ci", queue_view.MacGateLabel.CI),
                    ("unplaced", queue_view.MacGateLabel.UNPLACED), ("", queue_view.MacGateLabel.UNAVAILABLE),
                    ("future label", queue_view.MacGateLabel.UNAVAILABLE)):
                with self.subTest(origin=origin, wire=wire):
                    build = queue_view.MacBuild(100, "/Users/nate/hana", NOW, origin=origin,
                                                ci_runner="hana-mac-1", ci_job="test-hana")
                    row = queue_view.mac_build_record(build)
                    self.assertEqual(queue_view.mac_build_from_record(row), build)
                    del row["origin"]
                    self.assertIs(queue_view.mac_build_from_record(row).origin, queue_view.MacBuildOrigin.UNKNOWN)
                    del row["ci_runner"], row["ci_job"]
                    self.assertEqual(queue_view.mac_build_from_record(row).ci_runner, "")
                    self.assertEqual(queue_view.mac_build_from_record(row).ci_job, "")
                    row["gate_label"] = wire
                    restored = queue_view.mac_build_from_record(row)
                    self.assertIs(restored.gate_label, expected)
                    if expected == queue_view.MacGateLabel.PLACEMENT:
                        self.assertEqual(queue_view.gate_label_text(restored), wire)

    def test_mac_reader_marks_runner_ancestors_and_stops_at_parent_cycles(self) -> None:
        with QueueFixture(captured=False) as fixture:
            folder = str(fixture.root)
            processes: dict[int, dict[str, object]] = {
                100: {"parent": 90, "command": "/bin/cargo build", "executable": "/bin/cargo",
                      "resident_bytes": 1234, "started_at": NOW},
                90: {"parent": 80, "command": "/bin/sh build.sh", "executable": "/bin/sh",
                     "resident_bytes": 0, "started_at": NOW},
                80: {"parent": 1, "command": "", "executable": "/runner", "resident_bytes": 0, "started_at": NOW},
            }
            for command, parent, expected in (
                ("/actions/Runner.Worker spawn", 1, "ci"),
                ('"/actions runner/Runner.Listener" run', 1, "ci"),
                ("/bin/echo Runner.Worker", 1, "non-ci"),
                ("/actions/Runner.Worker-helper", 1, "non-ci"),
                ("/bin/sh build.sh", 90, "non-ci"),
            ):
                with self.subTest(command=command, parent=parent):
                    processes[80]["command"] = command
                    processes[80]["parent"] = parent
                    script = queue_view.mac_build_script().replace("print(json.dumps(read_reading()))",
                        f"read_processes = lambda: {processes!r}\n" +
                        "read_captures = lambda processes: {}\n" +
                        f"output = lambda arguments: 'p100\\nn{folder}\\n'\n" +
                        "repository_name = lambda folder: 'hana'\n" +
                        "process_environment = lambda pid: {'RUNNER_NAME': 'hana-mac-1', 'GITHUB_JOB': 'test-hana'}\n" +
                        "print(json.dumps(read_builds()))")
                    result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    rows = cast(list[dict[str, object]], json.loads(result.stdout.removesuffix("rc=0\n")))
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["pid"], 100)
                    self.assertEqual(rows[0]["origin"], expected)
                    self.assertEqual(rows[0]["ci_runner"], "hana-mac-1" if expected == "ci" else "")
                    self.assertEqual(rows[0]["ci_job"], "test-hana" if expected == "ci" else "")

    def test_mac_names_use_the_package_then_the_repository(self) -> None:
        for command, repository, expected in (
            ("cargo build --release -p hana", "", "hana build"),
            ("cargo build -phana", "other", "hana build"),
            ("cargo build --package=hana", "other", "hana build"),
            ("cargo check --package hana", "other", "hana check"),
            ("cargo nextest run", "cargo-liner", "cargo-liner nextest"),
            ("cargo build", "", "build"),
            ("", "", "cargo"),
        ):
            with self.subTest(command=command):
                self.assertEqual(queue_view.mac_step_name(queue_view.MacBuild(1, "/project", 0,
                    command, repository)), expected)

    def test_mac_command_jobs_reads_all_supported_positive_values(self) -> None:
        for argument in ("--test-threads 12", "--test-threads=12", "-j12", "-j 12", "--jobs 12", "--jobs=12"):
            with self.subTest(argument=argument):
                self.assertEqual(queue_view.mac_command_jobs("cargo nextest run " + argument), 12)
        for command in ("cargo build", "cargo build -j0", "cargo nextest run --test-threads 0"):
            self.assertIsInstance(queue_view.mac_command_jobs(command), queue_view.MacJobsUnavailable)

    def test_periodic_reader_preserves_folders_and_elapsed_days(self) -> None:
        with QueueFixture(captured=False) as fixture:
            _ = (fixture.root / "mac_test.conf").write_text(
                "host=fixture-mac\n# range: 5e-324..\nconnect_timeout_s=2\nmirror_root=~/.local/state/mac-test/mirror\n")
            path = fixture.root / "mac-builds.json"
            now = NOW
            for elapsed, seconds in (("03:04", 184), ("02:03:04", 7384), ("1-02:03:04", 93784)):
                with self.subTest(elapsed=elapsed):
                    result = subprocess.CompletedProcess(["ssh"], 0,
                        stdout=json.dumps({"builds": [{"pid": 710001, "folder": "/Users/nate/mac clone", "started_at": now - seconds,
                            "resident_bytes": 1048576, "test_pid": 710002, "command": "cargo nextest run --jobs=4",
                            "repository": "hana", "progress_tail": "", "recorded_unit": "", "session": ""}]}) + "\nrc=0\n", stderr="")
                    with (mock.patch.object(queue_view, "mac_reading_path", return_value=path),
                          mock.patch.object(subprocess, "run", return_value=result),
                          mock.patch.object(time, "time", return_value=now)):
                        queue_view.refresh_mac_builds()
                        reading = queue_view.read_mac_builds()
                    self.assertEqual(reading.error, "")
                    self.assertEqual(reading.read_at, now)
                    build, = reading.builds
                    self.assertEqual(build.pid, 710001)
                    self.assertEqual(build.folder, "/Users/nate/mac clone")
                    self.assertEqual(build.started_at, now - seconds)
                    self.assertEqual(build.stage, queue_view.MacTesting(710002))
                    self.assertEqual(build.resident_bytes, 1048576)
                    self.assertEqual(build.jobs, 4)
                    self.assertEqual(queue_view.mac_step_name(build), "hana nextest")


    def test_mac_reader_finds_all_three_tile_runs_and_their_counters(self) -> None:
        with QueueFixture(captured=False) as fixture:
            binary = fixture.root / "bin"
            binary.mkdir()
            born = int(time.mktime(time.strptime("Thu Nov 12 18:00:00 2026", "%a %b %d %H:%M:%S %Y")))
            stamp = "Thu Nov 12 18:00:00 2026"
            records = fixture.root / "captures" / "501"
            (records / "state/pids").mkdir(parents=True)
            folders = [fixture.root / "native" / "trunk-p172", fixture.root / "mirror" / "hana",
                       fixture.root / "mirror" / "cargo-liner"]
            for index, (pid, folder, done, total) in enumerate(zip((100, 200, 300), folders, (604, 647, 983), (1000,) * 3, strict=True)):
                folder.mkdir(parents=True)
                _ = (folder / ".copy-record.json").write_text(json.dumps({
                    "owner": "trunk" if index == 0 else "mirror-unit",
                    "repo": "cargo-liner" if index == 2 else "hana" if index == 1 else ""}))
                if index == 0:
                    _ = (folder / "Cargo.toml").write_text(
                        '[workspace.package]\nrepository = "https://github.com/natepiano/hana"\n')
                generation = f"fixture-{pid}"
                log = f"run-{generation}-{pid}.log"
                arguments = ["build", "--release", "-p", "hana"] if index == 0 else ["nextest", "run", "--test-threads", "12"]
                fields = ["cargo-tile-v3", generation, "fixture-boot", str(born), log, str(folder),
                          str(fixture.root), str(len(arguments)), *arguments]
                _ = (records / "state/pids" / f"{pid}.{generation}").write_bytes("\0".join([*fields, ""]).encode())
                _ = (records / log).write_text(f"\rBuilding [==> ] {done}/{total}: fixture\r")
            # The nested cargo and the compiler stay in their parent's row.
            process_rows = [f"{pid} 1 501 100 01:00 {stamp} /bin/cargo" for pid in (100, 200, 300)]
            process_rows += [f"201 200 501 200 00:59 {stamp} /bin/cargo",
                             f"202 201 501 300 00:58 {stamp} /bin/rustc"]
            scripts = {
                "sysctl": "printf 'fixture-boot\\n'",
                "git": "exit 1",
                "lsof": "exit 1",
                "ps": "case \"$*\" in *comm=*) cat <<'ROWS'\n" + "\n".join(process_rows) + "\nROWS\n;;\n*) cat <<'ROWS'\n" +
                      "\n".join(f"{pid} /bin/cargo nextest run" for pid in (100, 200, 201, 300)) + "\n202 /bin/rustc\nROWS\n;;\nesac",
            }
            for name, body in scripts.items():
                path = binary / name
                _ = path.write_text("#!/bin/sh\n" + body + "\n")
                path.chmod(0o755)
            script = queue_view.mac_build_script().replace('CAPTURE_ROOT = Path("/tmp/cargo-tile")',
                f"CAPTURE_ROOT = Path({str(records.parent)!r})")
            result = subprocess.run(["sh", "-c", script], text=True, capture_output=True,
                env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]}, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            rows = cast(list[dict[str, object]], json.loads(result.stdout.removesuffix("rc=0\n"))["builds"])
            self.assertEqual([row["pid"] for row in rows], [100, 200, 300])
            self.assertEqual([row["resident_bytes"] for row in rows], [100 * 1024, 600 * 1024, 100 * 1024])
            self.assertEqual([row["recorded_unit"] for row in rows], ["trunk", "mirror-unit", "mirror-unit"])
            self.assertEqual([row["repository"] for row in rows], ["hana", "hana", "cargo-liner"])
            self.assertEqual(queue_view.mac_command_jobs(str(rows[1]["command"])), 12)
            percentages: list[float] = []
            for row in rows:
                counter = progress_profile.parse_progress(str(row["progress_tail"]))
                assert isinstance(counter, progress_profile.ProgressCounter)
                percentages.append(counter.percent)
            self.assertEqual(percentages, [60.4, 64.7, 98.3])
            # A reused PID cannot supply the old capture's folder or counter.
            stale = records / "state/pids/100.fixture-100"
            _ = stale.write_bytes(stale.read_bytes().replace(str(born).encode(), str(born - 1).encode()))
            result = subprocess.run(["sh", "-c", script], text=True, capture_output=True,
                env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]}, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = cast(list[dict[str, object]], json.loads(result.stdout.removesuffix("rc=0\n"))["builds"])
            self.assertEqual([row["pid"] for row in rows], [200, 300])

    def test_mac_reader_finds_tests_in_the_selected_target_directory(self) -> None:
        with QueueFixture(captured=False) as fixture:
            binary = fixture.root / "bin"
            binary.mkdir()
            folder = fixture.root / "project"
            (folder / ".cargo").mkdir(parents=True)
            _ = (folder / "Cargo.toml").write_text('[workspace]\n')
            config = folder / ".cargo/config.toml"
            (folder / "linked-output").symlink_to(folder / "out space", target_is_directory=True)
            cases: list[tuple[str, list[str], dict[str, str], str, str, bool, bool]] = [
                ("default", [], {}, "", "target/debug/deps/test_binary", False, True),
                ("argument", ["--target-dir", "out space"], {"CARGO_TARGET_DIR": "wrong"}, "", "out space/debug/deps/test_binary", False, True),
                ("equals", ["--target-dir=out space"], {}, "", "out space/debug/deps/test_binary", False, True),
                ("symlink", ["--target-dir", "linked-output"], {}, "", "out space/debug/deps/test_binary", False, True),
                ("environment", [], {"CARGO_TARGET_DIR": "out space"}, "", "out space/debug/deps/test_binary", False, True),
                ("build environment", [], {"CARGO_BUILD_TARGET_DIR": "out space"}, "", "out space/debug/deps/test_binary", False, True),
                ("configuration", [], {}, "out space", "out space/debug/deps/test_binary", False, True),
                ("CLI configuration", ["--config", 'build.target-dir="out space"'], {}, "", "out space/debug/deps/test_binary", False, True),
                ("compiler observation", [], {}, "", "out space/debug/deps/test_binary", True, True),
                ("unrelated directory", ["--target-dir", "out space"], {}, "", "other/debug/deps/test_binary", False, False),
                ("directory prefix", ["--target-dir", "out space"], {}, "", "out space-other/debug/deps/test_binary", False, False),
                ("build script", ["--target-dir", "out space"], {}, "", "out space/debug/deps/build-script-build", False, False),
            ]
            for name, arguments, environment, configured, executable, compiler, testing in cases:
                with self.subTest(source=name):
                    config.unlink(missing_ok=True)
                    if configured:
                        _ = config.write_text('[build]\ntarget-dir = ' + json.dumps(configured) + '\n')
                    stamp = "Thu Nov 12 18:00:00 2026"
                    process_rows = [f"100 1 501 100 01:00 {stamp} /bin/cargo",
                                    f"101 100 501 100 01:00 {stamp} {folder / executable}"]
                    commands = ["100 " + shlex.join(["cargo", "test", *arguments]), f"101 {folder / executable}"]
                    if compiler:
                        process_rows.append(f"102 100 501 100 01:00 {stamp} /bin/rustc")
                        commands.append("102 " + shlex.join(["rustc", "--crate-name", "hana_tests", "--out-dir",
                                                            str(folder / executable).rsplit("/", 1)[0]]))
                    scripts = {
                        "sysctl": "exit 1",
                        "git": "exit 1",
                        "lsof": "cat <<'ROWS'\np100\nn" + str(folder) + "\nROWS",
                        "ps": "case \"$*\" in *comm=*) cat <<'ROWS'\n" + "\n".join(process_rows) + "\nROWS\n;;\n*) cat <<'ROWS'\n" + "\n".join(commands) + "\nROWS\n;;\nesac",
                    }
                    for program, body in scripts.items():
                        path = binary / program
                        _ = path.write_text("#!/bin/sh\n" + body + "\n")
                        path.chmod(0o755)
                    environment = {"HOME": str(fixture.root), **environment}
                    # The kernel buffer contains argv, then environment fields.
                    data = struct.pack("i", 3) + b"/bin/cargo\0\0cargo\0test\0CARGO_TARGET_DIR=not-an-environment-field\0"
                    data += b"\0".join(os.fsencode(key + "=" + value) for key, value in environment.items())
                    data += b"\0TOKEN=not-public\0\0"
                    script = queue_view.mac_build_script().replace('CAPTURE_ROOT = Path("/tmp/cargo-tile")',
                        f"CAPTURE_ROOT = Path({str(fixture.root / 'captures')!r})")
                    script = script.replace("print(json.dumps(read_reading()))",
                        f"assert cargo_environment({data!r}) == {environment!r}\n"
                        + f"process_environment = lambda pid: cargo_environment({data!r})\nprint(json.dumps(read_builds()))")
                    result = subprocess.run(["sh", "-c", script], text=True, capture_output=True,
                        env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]}, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertNotIn("not-public", result.stdout)
                    rows = cast(list[dict[str, object]], json.loads(result.stdout.removesuffix("rc=0\n")))
                    row, = rows
                    self.assertEqual(row["test_pid"], 101 if testing else 0)
                    row["counter"] = {}
                    build = queue_view.mac_build_from_record(row)
                    self.assertIsInstance(build.stage, queue_view.MacTesting if testing else queue_view.MacBuilding)

    def test_mac_command_jobs_requires_a_recorded_positive_value(self) -> None:
        for command in ("cargo nextest run -j8", "cargo build -j 8", "cargo test --jobs=8", "cargo check --jobs 8"):
            self.assertEqual(queue_view.mac_command_jobs(command), 8)
        for command in ("cargo test", "cargo build --jobs=0", "cargo test --jobs=-1"):
            self.assertIsInstance(queue_view.mac_command_jobs(command), queue_view.MacJobsUnavailable)

    def test_mac_reader_logs_raw_errors_without_publishing_them(self) -> None:
        with QueueFixture(captured=False) as fixture:
            _ = (fixture.root / "mac_test.conf").write_text("host=fixture\n# range: 5e-324..\nconnect_timeout_s=2\nmirror_root=~/.local/state/mac-test/mirror\n")
            path = fixture.root / "mac-builds.json"
            _ = path.write_text('{"read_at":0,"builds":[],"error":""}')
            result = subprocess.CompletedProcess(["ssh"], 0, stdout="rc=0\n", stderr="awk: private raw error")
            with (mock.patch.object(queue_view, "mac_reading_path", return_value=path),
                  mock.patch.object(subprocess, "run", return_value=result) as run,
                  self.assertLogs(queue_view.__name__, level="WARNING") as logs):
                queue_view.refresh_mac_builds()
            self.assertEqual(run.call_count, 1)
            self.assertIn("private raw error", str(logs.output))
            self.assertNotIn("private raw error", path.read_text())


class CloudQueueViewTests(unittest.TestCase):
    def test_page_step_names_use_roles_and_configured_owner_templates(self) -> None:
        model = self.cloud_model()[0]
        reading = model.reading
        assert isinstance(reading, wire.CloudQueueRead)
        row = reading.cloud[0]
        director = unit_labels.make_labels("panes", unit_labels.NoShowrunner(), unit_labels.DirectsUnit("panes"))
        runner = unit_labels.make_labels("builds", unit_labels.RunsProduction("builds"), unit_labels.NoDirector())
        for step, owner, expected in (
            ("test-affected", director, "panes targeted"),
            ("gate", director, "panes merge"),
            ("gate", runner, "showrunner merge"),
            ("gate", unit_labels.UnitLabels("builds", "builds"), "merge gate"),
            ("test-affected", UnitLabels("worker", "sdlc", "panes"), "test affected"),
            ("gate", UnitLabels("worker", "sdlc", "panes"), "merge gate"),
            ("build", director, "panes build"),
            ("literal{step}", director, "panes literal{step}"),
        ):
            with self.subTest(step=step, owner=owner):
                current = replace(row, identity=replace(row.identity, step=step))
                labeled = replace(model, labels={row.identity.session_id: owner})
                self.assertEqual(queue_view.cloud_step_name(current, labeled), expected)
        current = replace(row, identity=replace(row.identity, step="test-affected"))
        settings = dict(model.names.settings)
        settings["owned.test-affected"] = "targeted for {unit}"
        configured = replace(model, names=type(model.names)(settings), labels={row.identity.session_id: director})
        self.assertEqual(queue_view.cloud_step_name(current, configured), "targeted for panes")
        self.assertEqual(configured.names.shown("test-affected"), "test affected")
        self.assertEqual(configured.names.prose("gate"), "merge gate")

    def cloud_model(self) -> tuple[queue_view.QueueModel, QueueFixture]:
        fixture = self.enterContext(QueueFixture(captured=False))
        empty_mac_reading(fixture)
        write_memory_config(fixture.root / "build_admission.conf",
            cloud_gate_slots="1", cloud_gate_stale_pushes="3")
        _ = (fixture.root / "cloud_gate.conf").write_text(
            "[cloud_gate]\n# range: 5e-324..\nlog_push_s=5\n")
        model = fixture.model()
        return replace(model, reading=cloud_reading(model.reading)), fixture


class QueueViewFixtureTests(unittest.TestCase):
    fixture: QueueFixture = cast(QueueFixture, cast(object, None))
    @override
    def setUp(self) -> None:
        self.fixture = self.enterContext(QueueFixture())
        empty_mac_reading(self.fixture)
        _ = self.enterContext(mock.patch.object(os, "cpu_count", return_value=17))


class QueueViewTests(QueueViewFixtureTests):


    def test_snapshot_without_project_fields_reads_with_empty_projects(self) -> None:
        reservation = cast(dict[str, object], json.loads(self.fixture.reservation.read_text()))
        reservation["repo"] = ""
        self.fixture.write_json(self.fixture.reservation, reservation)
        reading = self.fixture.model().reading
        encoded = wire.encode_reply(wire.ViewReply(reading))
        # Remove only the new optional field from the complete service reply.
        def without_project(value: object) -> object:
            if isinstance(value, dict):
                return {key: without_project(item) for key, item in cast(dict[str, object], value).items()
                        if key != "project"}
            if isinstance(value, list):
                return [without_project(item) for item in cast(list[object], value)]
            return value
        reply = wire.parse_reply(json.dumps(without_project(cast(object, json.loads(encoded)))))
        self.assertIsInstance(reply, wire.ViewReply)
        if isinstance(reply, wire.ViewReply):
            self.assertEqual(reply.reading, reading)
            self.assertTrue(all(not step.project for step in reply.reading.view.waiting))
            self.assertTrue(all(not step.project for step in reply.reading.view.running))


    @override
    def setUp(self) -> None:
        super().setUp()
        config = self.fixture.root / "build_admission.conf"
        write_memory_config(config)
        _ = cast(object, self.enterContext(mock.patch.dict("os.environ", {"CONF_DIR": str(config.parent)})))


    def service_model(self, fixture: AdmissionFixture) -> queue_view.QueueModel:
        reply, = fixture.server.handle_request(wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        return replace(self.fixture.model(), reading=reply.reading)


    def test_unit_override_survives_restart_and_matches_priority(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first = fixture.request()
            later = fixture.request(since=first.since + 1)
            later = replace(later, identity=replace(later.identity, unit="queue-unit"),
                            sidecar=str(fixture.scope(later.owner)))
            _ = fixture.server.handle_request(first)
            _ = fixture.server.handle_request(later)
            fixture.restart()
            _ = fixture.server.handle_request(first)
            _ = fixture.server.handle_request(later)
            labels = {first.identity.session_id: UnitLabels("first", "builds", "other-unit"),
                      later.identity.session_id: UnitLabels("gate", "builds", "wrong-unit")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                refused, = fixture.completed_queue_change(wire.PrioritySetRequest("wrong-bump", "wrong-unit", "urgent", "user"))
                self.assertIsInstance(refused, wire.Refused)
                replies = fixture.completed_queue_change(wire.PrioritySetRequest("bump-unit", "queue-unit", "urgent", "user"))
                self.assertEqual(replies, [wire.Ok()])
                model = self.service_model(fixture)
                self.assertEqual([step.pid for step in model.reading.view.waiting], [later.owner.pid, first.owner.pid])
                self.assertEqual(model.reading.view.waiting[0].unit, "queue-unit")
                self.assertEqual(model.reading.view.waiting[1].unit, "")
                self.assertEqual(queue_evidence.step_labels(model.reading.view.waiting[1], labels).unit, "other-unit")
                self.assertEqual(queue_evidence.step_labels(model.reading.view.waiting[0], {}).unit, "queue-unit")
                fixture.free(60 * admission_records.GIB)
                write_pressure_reading(fixture.root / "pressure")
                _ = fixture.server.run_pass()
                _ = fixture.server.run_pass()
                model = self.service_model(fixture)
                running = next(step for step in model.reading.view.running if step.session_id == later.identity.session_id)
                self.assertEqual(queue_evidence.step_labels(running, labels).unit, "queue-unit")


if __name__ == "__main__":
    _ = unittest.main()


class LeftoverAgeTests(unittest.TestCase):
    def test_leftover_age_ranges(self) -> None:
        for minutes, expected in ((0, "0 min"), (45, "45 min"), (119, "119 min"),
                                  (120, "2 h"), (19 * 60, "19 h"), (48 * 60, "2 d")):
            with self.subTest(minutes=minutes):
                self.assertEqual(queue_view.leftover_age(minutes), expected)
