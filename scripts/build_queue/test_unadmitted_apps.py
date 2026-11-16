"""Find and alert on private Hana process fixtures without launching an app."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from typing import cast, override
from unittest import mock

from scripts.build_queue import admission_records as records, unadmitted_apps as apps
from scripts.build_queue.queue_fixture import fixture_reservation, write_process
from scripts.lint import memory_admit as gate
from scripts.message import unit_labels
from scripts.production import staff


class UnadmittedAppTests(unittest.TestCase):
    root: Path = Path()
    proc: Path = Path()
    cgroup: Path = Path()
    binary: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())))
        _ = self.enterContext(mock.patch.object(unit_labels, "newest_sessions", return_value=[]))
        self.proc = self.root / "proc"
        self.proc.mkdir()
        _ = (self.proc / "stat").write_text("btime 100\n")
        self.cgroup = self.root / "cgroup"
        self.binary = self.root / "target/debug/hana"
        self.binary.parent.mkdir(parents=True)
        _ = self.binary.write_text("fixture binary; never execute")
        sessions = self.root / "sessions"
        sessions.mkdir()
        _ = (sessions / "fixture.json").write_text(json.dumps({"sessionId": "session-id", "name": "fixture session"}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(sessions)})))

    def process(self, pid: int = 101, *, parent: int = 1, name: str = "hana", environment: str = "",
                cgroup: str = "/user.slice/session.scope", used: int = 1024, swap: int = 2048,
                start: str = "123") -> Path:
        write_process(self.proc, pid, used, parent=parent, swap=swap, start=start)
        directory = self.proc / str(pid)
        binary = self.binary.with_name(name)
        if not binary.exists():
            _ = binary.write_text("fixture binary; never execute")
        (directory / "exe").symlink_to(binary)
        _ = (directory / "environ").write_text(environment)
        _ = (directory / "cmdline").write_text(str(binary) + "\0")
        _ = (directory / "cgroup").write_text("0::" + cgroup + "\n")
        return directory

    def scan(self, reservations: Sequence[records.Reservation] = ()) -> apps.AppScan:
        directory = self.root / "admission"
        directory.mkdir(exist_ok=True)
        _ = (directory / "state.json").write_text(json.dumps({"reservations": {
            str(index): {"step": row["step"], "sidecar": row["sidecar"]}
            for index, row in enumerate(reservations)}}))
        return apps.scan(self.proc, directory, self.cgroup)

    def test_app_without_admission_has_one_alert_across_passes_and_restart(self) -> None:
        _ = self.process(environment="CLAUDE_CODE_SESSION_ID=session-id\0")
        ledger_path = self.root / "feed-state/alerts.json"
        ledger = apps.AppAlertLedger(ledger_path)
        with mock.patch.object(gate, "process_table", wraps=gate.process_table) as processes:
            reading = self.scan()
        processes.assert_called_once_with(self.proc)
        self.assertEqual(len(reading.apps), 1)
        app = reading.apps[0]
        self.assertEqual(app.owner, apps.AppOwner("fixture session", "session:session-id"))
        self.assertEqual((app.pid, app.binary, app.ram_bytes, app.swap_bytes), (101, str(self.binary), 1024, 2048))
        self.assertAlmostEqual(app.started_at, 100 + 123 / int(os.sysconf("SC_CLK_TCK")))
        send = mock.Mock(return_value=True)
        ledger.update(reading, send)
        ledger.update(self.scan(), send)
        apps.AppAlertLedger(ledger_path).update(self.scan(), send)
        send.assert_called_once_with(app)
        shutil.rmtree(self.proc / "101")
        restarted = apps.AppAlertLedger(ledger_path)
        restarted.update(self.scan(), send)
        self.assertEqual(restarted.claimed, set())
        self.assertEqual(json.loads(ledger_path.read_text()), [])

    def test_live_app_admission_excludes_only_its_scope(self) -> None:
        _ = self.process(100, name="python3", start="100")
        _ = self.process(cgroup="/builds.slice/builds-appfixture.slice/app-fixture.scope")
        _ = self.process(102, cgroup="/builds.slice/builds-appfixture.slice/app-fixture.scope/child")
        _ = self.process(103, cgroup="/builds.slice/builds-appfixture.slice/app-fixture.scope-other")
        scope_file = self.root / "app-scopes/app-fixture.cgroup"
        scope_file.parent.mkdir()
        _ = scope_file.write_text(str(self.cgroup / "builds.slice/builds-appfixture.slice/app-fixture.scope"))
        reservation = fixture_reservation(100, 4096, step="app")
        reservation["sidecar"] = str(scope_file)
        reading = self.scan((reservation,))
        self.assertEqual([app.pid for app in reading.apps], [103])
        reservation["start_time"] = "different start"
        self.assertEqual([app.pid for app in self.scan((reservation,)).apps], [103])
        reservation["start_time"] = "100"
        reservation["step"] = "build"
        self.assertEqual(len(self.scan((reservation,)).apps), 3)
        self.assertEqual(len(self.scan().apps), 3)

    def test_children_belong_to_the_first_hana_tree(self) -> None:
        _ = self.process(101, used=1024, swap=2048)
        _ = self.process(102, parent=101, used=3072, swap=4096)
        _ = self.process(103, parent=102, name="helper", used=5120, swap=6144)
        reading = self.scan()
        self.assertEqual(len(reading.apps), 1)
        self.assertEqual((reading.apps[0].ram_bytes, reading.apps[0].swap_bytes), (9216, 12288))

    def test_owner_environment_has_priority_over_session_ancestry(self) -> None:
        _ = self.process(100, name="shell", environment="CODEX_THREAD_ID=thread-id\0")
        _ = self.process(parent=100, environment="HANA_TEST_OWNER=feature owner\0")
        reading = self.scan()
        self.assertEqual(reading.apps[0].owner, apps.AppOwner("feature owner", "feature owner"))
        _ = (self.proc / "101/environ").write_text("")
        self.assertEqual(self.scan().apps[0].owner,
                         apps.AppOwner("thread-id", "codex:thread-id"))

    def test_recorded_session_name_can_receive_the_alert(self) -> None:
        _ = self.process(environment="CLAUDE_SESSION_NAME=fixture name\0")
        self.assertEqual(self.scan().apps[0].owner,
                         apps.AppOwner("fixture name", "fixture name"))

    def test_unknown_owner_gets_a_row_without_an_alert(self) -> None:
        _ = self.process()
        reading = self.scan()
        self.assertIsInstance(reading.apps[0].owner, apps.UnknownAppOwner)
        send = mock.Mock(return_value=True)
        apps.AppAlertLedger(self.root / "alerts.json").update(reading, send)
        send.assert_not_called()

    def test_exact_binary_match_excludes_similar_names_and_dead_processes(self) -> None:
        _ = self.process(101, name="hana-helper")
        dead = self.process(102)
        stat = dead / "stat"
        _ = stat.write_text(stat.read_text().replace(") S ", ") Z "))
        _ = self.process(103)
        self.assertEqual([app.pid for app in self.scan().apps], [103])

    def test_pid_reuse_gets_a_new_alert_and_live_admitted_keys_remain(self) -> None:
        _ = self.process(environment="HANA_TEST_OWNER=fixture\0")
        ledger = apps.AppAlertLedger(self.root / "alerts.json")
        reading = self.scan()
        send = mock.Mock(return_value=True)
        ledger.update(reading, send)
        ledger.update(replace(reading, apps=()), send)
        self.assertEqual(ledger.claimed, {"101:123"})
        write_process(self.proc, 101, start="124")
        ledger.update(self.scan(), send)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(ledger.claimed, {"101:124"})

    def test_failed_delivery_is_not_retried(self) -> None:
        _ = self.process(environment="HANA_TEST_OWNER=fixture\0")
        reading = self.scan()
        path = self.root / "alerts.json"
        send = mock.Mock(return_value=False)
        apps.AppAlertLedger(path).update(reading, send)
        apps.AppAlertLedger(path).update(reading, send)
        send.assert_called_once_with(reading.apps[0])
        self.assertEqual(json.loads(path.read_text()), ["101:123"])

    def test_restart_after_claim_does_not_send(self) -> None:
        _ = self.process(environment="HANA_TEST_OWNER=fixture\0")
        reading = self.scan()
        path = self.root / "alerts.json"
        ledger = apps.AppAlertLedger(path)
        save = ledger.save

        def claim_then_interrupt() -> None:
            save()
            raise InterruptedError("fixture interruption after the durable claim")

        send = mock.Mock(return_value=True)
        with mock.patch.object(ledger, "save", side_effect=claim_then_interrupt), self.assertRaises(InterruptedError):
            ledger.update(reading, send)
        apps.AppAlertLedger(path).update(reading, send)
        send.assert_not_called()
        self.assertEqual(json.loads(path.read_text()), ["101:123"])

    def check_exited_supervisor_admission(self, *, reused: bool) -> None:
        scope = self.cgroup / "builds.slice/builds-appfixture.slice/app-fixture.scope"
        _ = self.process(cgroup="/" + scope.relative_to(self.cgroup).as_posix(),
                         environment="HANA_TEST_OWNER=fixture\0")
        if reused:
            _ = self.process(100, name="python3", start="999")
        sidecar = self.root / "app.cgroup"
        _ = sidecar.write_text(str(scope))
        reservation = fixture_reservation(100, 4096, step="app")
        reservation["sidecar"] = str(sidecar)
        reading = self.scan((reservation,))
        self.assertEqual(reading.apps, ())
        send = mock.Mock(return_value=True)
        apps.AppAlertLedger(self.root / "alerts.json").update(reading, send)
        send.assert_not_called()

    def test_exited_supervisor_keeps_scope_admitted(self) -> None:
        self.check_exited_supervisor_admission(reused=False)

    def test_reused_supervisor_pid_keeps_scope_admitted(self) -> None:
        self.check_exited_supervisor_admission(reused=True)

    def check_staff_owner(self, *, codex: bool) -> None:
        session = staff.StaffId("thread-id" if codex else "session-id")
        parent = staff.StaffRow(staff.StaffId("parent"), "producer", "director", staff.StaffRoot(),
                                current_session=staff.StaffId("parent-session"))
        worker = staff.StaffRow(staff.StaffId("worker"), "worker", "current staff name", staff.ReportsTo(parent.id),
                                current_session=session)
        variable = "CODEX_THREAD_ID" if codex else "CLAUDE_CODE_SESSION_ID"
        _ = self.process(environment=f"{variable}={session}\0")
        with (mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((parent, worker))),
              mock.patch.object(unit_labels, "unit_labels_for", wraps=unit_labels.unit_labels_for) as labels):
            reading = self.scan()
        labels.assert_called_once_with([session], os.environ)
        self.assertEqual(reading.apps[0].owner,
                         apps.AppOwner("current staff name", f"{'codex' if codex else 'session'}:{session}"))
        _ = (self.proc / "101/environ").write_text(f"{variable}={session}\0HANA_TEST_OWNER=explicit owner\0")
        with mock.patch.object(unit_labels, "unit_labels_for") as labels:
            self.assertEqual(self.scan().apps[0].owner,
                             apps.AppOwner("explicit owner", "explicit owner"))
        labels.assert_not_called()

    def test_claude_owner_uses_current_staff_name(self) -> None:
        self.check_staff_owner(codex=False)

    def test_codex_owner_uses_current_staff_name(self) -> None:
        self.check_staff_owner(codex=True)


if __name__ == "__main__":
    _ = unittest.main()
