"""Status discovery retains healthy units when process evidence is incomplete."""
from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
import os
import subprocess
from typing import cast
import json
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts.lib import process_start, unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.message import process_table, sessions
from scripts.production import session_staff as labels, staff, staff_sources
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, fixture_worker, fixture_session, unit_membership
from scripts.herdr import herdr, pane_launch
from scripts.herdr.test_herdr import codex_foreground_cases


class StatusDiscoveryTests(StaffTestCase):
    def test_worker_home_uses_the_recorded_account_with_shared_sessions(self) -> None:
        fixture = self.row_production()
        shared = Path.home() / ".codex" / "sessions"
        shared.mkdir(parents=True)
        numbered = Path.home() / ".codex1"
        numbered.mkdir()
        (numbered / "sessions").symlink_to(shared, target_is_directory=True)
        (shared / f"{fixture.worker.current_session}.jsonl").touch()
        row = staff.set_home(fixture.worker.id, staff.SessionHome(numbered))
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(shared.parent)}):
            self.assertEqual(labels.worker_home({"staff_id": row.id}, staff.local_snapshot()), str(numbered))

    def test_worker_directory_reports_only_unavailable_worktree_states(self) -> None:
        fixture = self.row_production()
        checkout = fixture.doc.parents[1]
        write_text_atomic(fixture.doc, "# Production\n"
                          + f"- **Repository:** `{checkout}`\n"
                          + f"- **Showrunner checkout:** `{checkout}`\n"
                          + "- **Merge branch:** `merge-from-git`\n"
                          + "- **Log:** `production.log`\n")
        retired = replace(fixture.unit, worktree=staff.RemovedWorktree("natedev", fixture.worktree))
        unfilled = replace(fixture.unit, worktree=staff.WorktreeUnfilled())
        show_worker = replace(fixture.worker, parent=staff.ReportsTo(fixture.runner.id))
        cases = (
            ("retired-worker", (retired, fixture.worker), fixture.worker, Path.cwd(), ""),
            ("missing-parent", (fixture.worker,), fixture.worker, Path.cwd(), "staff worktree is unavailable"),
            ("unfilled-worker", (unfilled, fixture.worker), fixture.worker, Path.cwd(), "staff worktree is unavailable"),
            ("present-worker", (fixture.unit, fixture.worker), fixture.worker, fixture.worktree, ""),
            ("showrunner-worker", (fixture.runner, show_worker), show_worker, checkout, ""),
            ("retired-unit", (retired,), retired, Path.cwd(), "staff worktree is unavailable"),
        )
        before = self.staff_state()
        for state, rows, row, expected, anomaly in cases:
            with self.subTest(state=state):
                with labels.anomalies() as lines:
                    self.assertEqual(labels.worker_directory(staff.StaffSnapshot(rows), row.id), expected)
                self.assertEqual(lines, [f"anomaly: {row.id}: {anomaly}"] if anomaly else [])
        self.assertEqual(self.staff_state(), before)

        record = staff.snapshot()
        expected = labels.RowResolver(record).production_doc(fixture.unit)
        facts = labels.RowResolver(record).production_document_facts()
        with (self.machine("mac"), labels.supplied_production_facts(facts),
              mock.patch.object(subprocess, "run", side_effect=AssertionError("document fact ssh"))):
            for row in (fixture.runner, fixture.unit, fixture.worker):
                with self.subTest(kind=row.kind):
                    self.assertEqual(labels.RowResolver(record).production_doc(row), expected)
        write_text_atomic(fixture.doc, "# Changed production\n")
        current = labels.RowResolver(record).production_doc(fixture.unit)
        self.assertIsInstance(current, labels.ProductionDoc)
        assert isinstance(current, labels.ProductionDoc)
        self.assertEqual(current.text, "# Changed production\n")

    def test_run_workers_follow_the_staff_owner_without_a_roster(self) -> None:
        fixture = self.row_production()
        showrunner_worker = replace(fixture.worker, parent=staff.ReportsTo(fixture.runner.id))
        resumed_unit = replace(fixture.unit, current_session=fixture_runtime_id("resumed-run-owner"))
        rows = (fixture.runner, fixture.unit, fixture.worker)
        before = self.staff_state()
        for state, membership, expected in (
            ({"owner_staff_id": fixture.unit.id}, rows, (fixture.worker,)),
            ({"owner_staff_id": fixture.runner.id}, (*rows[:-1], showrunner_worker), (showrunner_worker,)),
            ({"owner_staff_id": fixture.unit.id}, (fixture.runner, resumed_unit, fixture.worker), (fixture.worker,)),
            ({}, rows, ()),
        ):
            with self.subTest(state=state):
                self.assertEqual(labels.run_workers(state, staff.StaffSnapshot(membership)), expected)
        self.assertEqual(self.staff_state(), before)


    def test_codex_launch_membership_keeps_its_native_environment_without_a_claude_identity(self) -> None:
        from scripts.agents import pane_record
        from scripts.agents.launch_names import LaunchNames

        fixture = self.row_production()
        launch = pane_record.launch("w1:p1", fixture.unit.id, str(fixture.worktree),
                                   {"CODEX_HOME": str(self.root / ".codex2")}, ["codex"], kind="codex")
        launch["environment"] = {"CODEX_HOME": str(cast(staff.SessionHome, pane_launch.config_home(launch)).path),
                                 "CODEX_THREAD_ID": fixture.unit.current_session, "STAFF_ID": fixture.unit.id}
        fixture_session(fixture.unit.current_session, staff.CodexBackend(self.root / ".codex2"))
        names = LaunchNames(staff.snapshot())
        before = self.staff_state()
        self.assertEqual(names.environment(launch), {
            "CODEX_HOME": str(cast(staff.SessionHome, pane_launch.config_home(launch)).path), "CODEX_THREAD_ID": fixture.unit.current_session,
        })
        self.assertEqual(self.staff_state(), before)

    def test_pane_publication_keeps_the_staff_reference_and_pane_data(self) -> None:
        fixture = self.row_production()
        entry = pane_launch.launch("w1:p1", fixture.unit.current_session, str(fixture.worktree),
            {"SHOWRUNNER_UNIT": "old"}, ["claude", "--model", "sonnet"])
        self.assertEqual(entry["environment"], {})
        entry["environment"] = {"STAFF_ID": fixture.unit.id,
            "CLAUDE_CODE_SESSION_ID": fixture.unit.current_session, "ENABLE_TOOL_SEARCH": "true"}
        entry["pid"], entry["proc_start"] = 123, "start"
        path = self.root / "panes.json"
        pane_launch.write(path, entry)
        saved = pane_launch.read_strict(path)["w1:p1"]
        self.assertEqual(saved, {**{field: value for field, value in entry.items()
            if field not in {"session_id", "name", "cwd", "kind", "config_dir"}}, "staff_id": fixture.unit.id})
        self.assertEqual(entry["session_id"], fixture.unit.current_session)

    def test_record_membership_derives_staff_marks_only_at_launch(self) -> None:
        from scripts.agents import pane_record
        from scripts.agents.launch_names import LaunchNames

        fixture = self.row_production()
        entry = pane_launch.launch("w1:p1", fixture.unit.current_session, str(fixture.worktree),
            {}, ["claude", "--model", "sonnet"])
        fixture_session(fixture.unit.current_session, staff.ClaudeBackend())
        with mock.patch.object(pane_record, "launches", return_value=[]):
            saved = labels.record_membership(entry)
            self.assertEqual(saved, entry)
            self.assertEqual(LaunchNames(staff.snapshot()).environment(saved),
                             {"CLAUDE_CODE_SESSION_ID": fixture.unit.current_session})

    def test_repair_removes_saved_staff_marks_and_keeps_pane_data(self) -> None:
        from scripts.message import sessions
        from scripts.agents import pane_record

        fixture = self.row_production()
        entry = pane_launch.launch("w1:p1", fixture.unit.current_session, str(fixture.worktree),
                                  {}, ["claude", "--model", "sonnet"])
        entry["environment"] = {"CLAUDE_CODE_SESSION_ID": fixture.unit.current_session,
                                "STAFF_ID": fixture.unit.id, "ENABLE_TOOL_SEARCH": "true"}
        directory = self.root / "retained"
        path = directory / "agents.json"
        entry = pane_launch.retained_launch(entry, staff.initial_snapshot())
        write_text_atomic(path, json.dumps({"w1:p1": entry}))
        with (mock.patch.object(pane_record, "RECORD_DIR", directory),
              mock.patch.object(sessions, "running_sessions", return_value=[])):
            self.assertEqual(len(labels.repair_marks()), 1)
            saved = pane_record.read_strict("agents")["w1:p1"]
            self.assertEqual(saved, {**entry, "environment": {"ENABLE_TOOL_SEARCH": "true"}})
            self.assertEqual(labels.repair_marks(), [])

    def test_repair_preserves_the_retained_codex_launch_in_both_process_orders(self) -> None:
        from scripts.agents import pane_record
        from scripts.message import sessions

        parent = unit_membership("build", "parent")
        child = unit_membership("build", "child")
        place = herdr.HerdrPlace("agents", "w1:p1")
        saved = pane_record.launch(place.pane, parent.id, str(self.root), {}, ["codex"], kind="codex")
        candidate = pane_record.launch(place.pane, child.id, str(self.root), {}, ["claude"], kind="claude")
        record = sessions.SessionRecord(pid=457, sessionId=child.id, name="child", procStart="1",
                                        updatedAt=1, messagingSocketPath="", tmux="", formerNames=[])
        processes = [herdr.HerdrProcess(456, place, {}, "codex", str(self.root)),
                     herdr.HerdrProcess(457, place, {}, "claude", str(self.root))]
        directory = self.root / "retained"
        path = directory / "agents.json"
        with (codex_foreground_cases(self.root) as cases,
              mock.patch.object(pane_record, "RECORD_DIR", directory),
              mock.patch.object(pane_record, "running_launch", return_value=candidate),
              mock.patch.object(sessions, "running_sessions", return_value=[record]),
              mock.patch.object(herdr, "herdr_places", return_value={457: place}),
              mock.patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.PaneSessionReport(parent.id), "1", {})) as native,
              mock.patch.object(herdr.Herdr, "result") as result):
            for case, order in zip(cases[:2], (processes, processes[::-1]), strict=True):
                with (self.subTest(case=case.name),
                      mock.patch.object(herdr, "herdr_processes", return_value=order)):
                    result.return_value = {"process_info": case.info}
                    pane_record.write(place.session, saved)
                    before = path.read_bytes()
                    self.assertEqual(labels.repair_marks(), [])
                    self.assertEqual(path.read_bytes(), before)

            result.return_value = {"process_info": {"foreground_processes": [
                {"pid": 457, "argv": ["claude"], "cwd": str(self.root)}]}}
            native.return_value = herdr.NativeAgentProcess(herdr.EnvironmentThread(child.id), "1", {})
            candidate.update(pid=457, proc_start="2")
            self.assertEqual(labels.repair_marks(), [])
            candidate["proc_start"] = "1"
            self.assertEqual(len(labels.repair_marks()), 1)
            self.assertEqual(pane_record.read_strict(place.session)[place.pane].get("staff_id"), child.id)

    def test_pane_parents_selects_only_local_pane_workers_from_the_record(self) -> None:
        runner = fixture_member("showrunner", name="pane-show", doc=staff.LiveDoc("natedev", self.root / "show.md"))
        selected = fixture_member("unit", name="pane", parent=runner.id,
                                  worktree=staff.PresentWorktree("natedev", self.root / "pane"))
        other = fixture_member("unit", name="other-machine", parent=runner.id,
                               worktree=staff.PresentWorktree("mac", self.root / "other"))
        empty = fixture_member("unit", name="no-workers", parent=runner.id,
                               worktree=staff.PresentWorktree("natedev", self.root / "empty"))
        children = [fixture_worker("impl", parent.id) for parent in (runner, selected, other)]
        record = staff.snapshot()
        before = self.staff_state()
        self.assertEqual({row.id for row in labels.pane_parents(record)}, {runner.id, selected.id})
        self.assertEqual({row.id for row in record.find(kind="worker")}, {child.id for child in children})
        self.assertNotIn(empty, labels.pane_parents(record))
        self.assertEqual(self.staff_state(), before)

    def test_unknown_process_identity_preserves_healthy_unit_status(self) -> None:
        from scripts.production import unit_lookup

        broken = unit_membership("show", "broken")
        healthy = unit_membership("show", "healthy")
        place = labels.Place("agents", "w1:p1")
        launch = pane_launch.NativePaneLaunch(pane_id=place.pane, session_id=broken.current_session, config_dir="", cwd="",
                            name="", model="", remote_control=False, environment={}, flags_read=True, written_at="")
        processes = [herdr.HerdrProcess(910001, place, {}, "claude", ""),
                     herdr.HerdrProcess(910002, labels.Place("agents", "w2:p1"),
                                       {"CLAUDE_CODE_SESSION_ID": healthy.current_session}, "zsh", "")]
        errors = StringIO()

        def read(*arguments: str) -> dict[str, object]:
            pid, program = (910001, "claude") if arguments[-1] == "w1:p1" else (910002, "zsh")
            return {"process_info": {"shell_pid": pid, "foreground_process_group_id": pid,
                                     "foreground_processes": [{"pid": pid, "argv": [program]}]}}

        with (mock.patch.object(herdr, "live_sessions", return_value=[{"name": "agents"}]),
              mock.patch.object(herdr.Herdr, "listed", return_value=[{"pane_id": "w1:p1"}, {"pane_id": "w2:p1"}]),
              mock.patch.object(herdr.Herdr, "result", side_effect=read),
              mock.patch.object(process_table, "snapshot", return_value={
                  910001: process_table.Process(910001, 1, "claude", "claude")}),
              mock.patch.object(herdr, "herdr_processes", return_value=processes), redirect_stderr(errors)):
            observed = unit_lookup.observed_session_labels(record=staff.snapshot(), running=[])
            grouped = labels.group_labeled_sessions(observed, record=staff.snapshot(),
                                                    sources=labels.MemberSources((launch,)))
        self.assertEqual(errors.getvalue(),
                         "anomaly: pid 910001 (claude) in herdr:agents:w1:p1: "
                         + "cannot read its thread; staff status may be incomplete\n")
        states = grouped.status_units("show")
        self.assertIsInstance(states["broken"].claude, labels.ClaudeUnknown)
        self.assertIsInstance(states["healthy"].claude, labels.ClaudeNotRunning)

    def test_empty_shell_does_not_hide_an_unreadable_pane_for_the_unit(self) -> None:
        row = unit_membership("show", "unit")
        place = labels.Place("agents", "w1:p1")
        candidate = labels.UnprovedPaneIdentity(frozenset({row.current_session}))
        empty = labels.LabeledSession("", labels.ClaudeNotRunning(), place,
                                      identity=candidate, empty=herdr.ObservedEmpty(place, 910001))
        unknown = labels.LabeledSession("", labels.ClaudeUnknown("process info denied"),
                                        labels.Place("agents", "w2:p1"), identity=candidate)
        record = staff.snapshot()
        sources = labels.MemberSources(())
        for observations in ((empty, unknown), (unknown, empty)):
            with self.subTest(observations=observations):
                report = labels.group_labeled_sessions(observations, record=record, sources=sources)
                self.assertEqual(report.checked_unit("show", row.name), labels.UnknownUnit("process info denied"))

    def test_denied_thread_read_reports_only_when_the_pane_holds_staff(self) -> None:
        row = unit_membership("show", "unit")
        place = labels.Place("agents", "w2C")
        launch = pane_launch.NativePaneLaunch(pane_id=place.pane, session_id=row.current_session, config_dir="", cwd="",
                            name="", model="", remote_control=False, environment={}, flags_read=True, written_at="")
        unknown = labels.LabeledSession("", labels.ClaudeUnknown("thread read denied"), place,
                                        labels.ObservedProcess(910001, "codex"))
        for launches in ((launch,), ()):
            with (self.subTest(staff_pane=bool(launches)),
                  mock.patch.object(herdr, "shared_session", return_value=place.session),
                  mock.patch.object(herdr, "process_environment", return_value={}),
                  mock.patch.object(process_table, "snapshot", return_value={
                      910001: process_table.Process(910001, 1, "codex", "codex")}),
                  mock.patch.object(herdr, "codex_resume_thread", side_effect=PermissionError("thread read denied")),
                  labels.anomalies(staff_sources.CodexProcessThreads().check) as lines):
                grouped = labels.group_labeled_sessions([unknown], record=staff.snapshot(),
                                                        sources=labels.MemberSources(launches))
            if launches:
                self.assertEqual(lines, [f"anomaly: {labels.holder_text(unknown)}: "
                                         + "cannot read its thread; staff status may be incomplete"])
                self.assertIsInstance(grouped.status_units("show")[row.name].claude, labels.ClaudeUnknown)
            else:
                self.assertEqual(lines, [])
                self.assertEqual(grouped.status_units("show"), {})

    def test_exited_process_in_a_staff_pane_is_quiet_and_not_placed(self) -> None:
        row = unit_membership("show", "unit")
        place = labels.Place("agents", "w2C")
        launch = pane_launch.NativePaneLaunch(pane_id=place.pane, session_id=row.current_session, config_dir="", cwd="",
                            name="", model="", remote_control=False, environment={}, flags_read=True, written_at="")
        unknown = labels.LabeledSession("", labels.ClaudeUnknown("thread read denied"), place,
                                        labels.ObservedProcess(910001, "codex"))
        with (mock.patch.object(herdr, "shared_session", return_value=place.session),
              mock.patch.object(process_table, "snapshot", return_value={}),
              redirect_stderr(StringIO()) as errors, labels.anomalies() as lines):
            grouped = labels.group_labeled_sessions([unknown], record=staff.snapshot(),
                                                    sources=labels.MemberSources((launch,)))
        self.assertEqual(grouped.status_units("show"), {})
        self.assertEqual(lines, [])
        self.assertEqual(errors.getvalue(), "")
        unobserved = replace(unknown, process=labels.ProcessUnobserved())
        with (mock.patch.object(herdr, "shared_session", return_value=place.session),
              mock.patch.object(process_table, "snapshot") as snapshot, labels.anomalies() as lines):
            grouped = labels.group_labeled_sessions([unobserved], record=staff.snapshot(),
                                                    sources=labels.MemberSources((launch,)))
        snapshot.assert_not_called()
        self.assertIsInstance(grouped.status_units("show")[row.name].claude, labels.ClaudeUnknown)
        self.assertEqual(len(lines), 1)

    def test_unregistered_thread_in_a_unit_pane_is_quiet_and_not_placed(self) -> None:
        row = unit_membership("show", "unit")
        place = labels.Place("agents", "w2C")
        launch = pane_launch.NativePaneLaunch(pane_id=place.pane, session_id=row.current_session, config_dir="", cwd="",
                            name="", model="", remote_control=False, environment={}, flags_read=True, written_at="")
        observed = [labels.LabeledSession("", labels.ClaudeUnknown("no registered session"), place,
                    labels.ObservedProcess(910001, "codex"), labels.LiveSession(fixture_runtime_id("fake-thread"))),
                    labels.LabeledSession("", labels.ClaudeNotRunning(), place,
                    labels.ObservedProcess(910002, "zsh"), labels.LiveSession(row.current_session))]
        errors = StringIO()
        with (mock.patch.object(herdr, "shared_session", return_value=place.session), redirect_stderr(errors)):
            grouped = labels.group_labeled_sessions(observed, record=staff.snapshot(),
                                                    sources=labels.MemberSources((launch,)))
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(grouped.status_units("show"), {row.name: replace(observed[1], unit=row.name)})

    def test_unreadable_session_record_keeps_placement_and_reports_one_failure(self) -> None:
        from scripts.production import unit_lookup
        from scripts.message import sessions

        row = unit_membership("show", "unit")
        place = labels.Place("agents", "w1:p1")
        processes = [herdr.HerdrProcess(910001, place, {"CLAUDE_CODE_SESSION_ID": row.current_session}, "claude", "")]
        errors = StringIO()
        with (mock.patch.object(herdr, "live_sessions", return_value=[{"name": "agents"}]),
              mock.patch.object(herdr, "herdr_processes", return_value=processes),
              mock.patch.object(herdr.Herdr, "listed", return_value=[{"pane_id": place.pane}]),
              mock.patch("scripts.production.unit_lookup.sessions.read_registry", return_value=sessions.RegistryRead((), (self.root / "910001.json",))),
              redirect_stderr(errors), labels.anomalies() as lines):
            observed = unit_lookup.observed_session_labels(record=staff.snapshot())
            grouped = labels.group_labeled_sessions(observed, record=staff.snapshot(), sources=labels.MemberSources(()))
        self.assertIsInstance(grouped.status_units("show")[row.name].claude, labels.ClaudeUnknown)
        self.assertEqual(lines, ["anomaly: pid 910001 in herdr:agents:w1:p1: the session record of pid 910001 could not be read"])
        self.assertEqual(errors.getvalue(), "")

    def test_agent_children_select_the_agent_without_an_anomaly(self) -> None:
        row = unit_membership("show", "unit")
        row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("claim-after-clear")))
        place = labels.Place("agents", "w1:p1")
        children = [labels.LabeledSession("", labels.ClaudeUnknown("child process"), place,
                    labels.ObservedProcess(910001 + index, command), labels.LiveSession(row.current_session))
                    for index, command in enumerate(("bevy_brp_mcp", "zsh", "python3", "sleep", "sh", "codex-code-mode-host"))]
        for command in ("claude", "codex"):
            with self.subTest(agent=command):
                agent = replace(children[0], process=labels.ObservedProcess(910099, command))
                errors = StringIO()
                with redirect_stderr(errors):
                    grouped = labels.group_labeled_sessions([*children, agent], record=staff.snapshot(),
                                                            sources=labels.MemberSources(()))
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(grouped.units, {"show": {row.name: replace(agent, unit=row.name)}})

    def test_keeper_without_environment_identity_never_uses_the_retained_unit_launch(self) -> None:
        row = unit_membership("show", "unit")
        place = labels.Place("agents", "w2C")
        launch = pane_launch.NativePaneLaunch(pane_id=place.pane, session_id=row.current_session, config_dir="", cwd="",
                            name="", model="", remote_control=False, environment={}, flags_read=True, written_at="")
        unknown = labels.LabeledSession("", labels.ClaudeUnknown("no identity"), place,
                                        labels.ObservedProcess(198033, "codex"))
        keeper = fixture_runtime_id("retained-pane-keeper")
        producer = fixture_member("producer", name="keeper", runtime_id=keeper)
        config = self.root / "config"
        config.mkdir(exist_ok=True)
        _ = (config / "console.conf").write_text(f"page.list.owner={producer.id}\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(config)})))
        from scripts.message import codex_daemon

        sessions = [codex_daemon.DaemonSession(keeper, str(self.root), "idle")]

        with (mock.patch.object(herdr, "shared_session", return_value=place.session),
              mock.patch.object(herdr, "process_environment", return_value={}),
              mock.patch.object(herdr, "herdr_place_of", return_value=place),
              mock.patch.object(herdr.Herdr, "result", return_value={
                  "pane": {"terminal_title_stripped": "keeper | idle"},
                  "process_info": {"foreground_processes": [{"pid": 198033, "argv": ["codex"]}]}}),
              mock.patch.object(codex_daemon, "loaded_sessions", return_value=sessions),
              labels.anomalies(staff_sources.CodexProcessThreads().check) as lines):
            grouped = labels.group_labeled_sessions([unknown], record=staff.snapshot(),
                                                    sources=labels.MemberSources((launch,)))
        self.assertEqual(grouped.status_units("show"), {})
        self.assertEqual(lines, [])

    def test_report_keeps_one_failure_line_per_process(self) -> None:
        errors = StringIO()
        with redirect_stderr(errors), labels.anomalies() as lines:
            labels.report_anomaly("pid 294399: session record is unreadable", pid=294399)
            labels.report_anomaly("pid 294399: owner record is unreadable", pid=294399)
            labels.report_anomaly("registry is unreadable")
        self.assertEqual(lines, ["anomaly: pid 294399: session record is unreadable",
                                 "anomaly: registry is unreadable"])
        self.assertEqual(errors.getvalue(), "")
        with redirect_stderr(errors):
            labels.report_anomaly("session record is unreadable", pid=294399)
        self.assertEqual(errors.getvalue(), "anomaly: session record is unreadable\n")

    def test_exit_report_does_not_hide_a_later_holder_failure(self) -> None:
        holder_pid, exited_pid = 3276803, 3276804
        with (mock.patch.object(process_start, "process_start", return_value=process_start.Gone()) as read,
              labels.anomalies() as lines):
            labels.report_anomaly(herdr.process_exit_reason(exited_pid), pid=holder_pid, exited_pid=exited_pid)
            labels.report_anomaly("session record is unreadable", pid=holder_pid)
        read.assert_called_once_with(exited_pid)
        self.assertEqual(lines, [f"anomaly: pid {holder_pid}: session record is unreadable"])

    def test_report_checks_only_the_pid_named_in_an_exit_reason(self) -> None:
        from scripts.production import unit_lookup

        foreground_pid, holder_pid = 3276803, 3276804
        place = labels.Place("agents", "w16:p437")
        info = {"foreground_processes": [{"pid": foreground_pid, "argv": ["claude"]}]}
        holder = herdr.HerdrProcess(holder_pid, place, {}, "claude", "")
        exit_reason = herdr.process_exit_reason(foreground_pid)
        denied = f"pid {foreground_pid}: permission denied"
        exit_failure = herdr.NativeRegistryChanging(exit_reason)
        cases = (
            (exit_failure, process_start.Gone(), True, (foreground_pid,), False),
            (exit_failure, process_start.Started("test"), True, (foreground_pid,), True),
            (exit_failure, process_start.Unreadable("permission denied"), True, (foreground_pid,), True),
            (exit_failure, process_start.Gone(), False, (foreground_pid,), False),
            (PermissionError(denied), process_start.Started("test"), True, (), True),
        )
        for failure, start, has_holder, probes, visible in cases:
            with self.subTest(reason=str(failure), start=type(start).__name__, has_holder=has_holder):
                holders = [holder] if has_holder else []
                starts = {foreground_pid: start, holder_pid: process_start.Gone()}
                with (mock.patch.object(herdr, "live_sessions", return_value=[{"name": place.session}]),
                      mock.patch.object(herdr.Herdr, "listed", return_value=[{"pane_id": place.pane}]),
                      mock.patch.object(herdr.Herdr, "result", return_value={"process_info": info}),
                      mock.patch.object(herdr, "herdr_processes", return_value=holders),
                      mock.patch.object(herdr, "native_process", side_effect=failure),
                      mock.patch.object(process_start, "process_start", side_effect=starts.__getitem__) as read,
                      labels.anomalies() as lines):
                    observed = unit_lookup.observed_session_labels(registry=sessions.RegistryRead((), ()))
                    _ = labels.group_labeled_sessions(
                        observed, record=staff.StaffSnapshot(()), sources=labels.MemberSources(()))
                    read.assert_not_called()
                self.assertEqual(read.call_args_list, [mock.call(pid) for pid in probes])
                prefix = f"pid {holder_pid}: " if has_holder else ""
                line = f"anomaly: {prefix}{labels.place_text(place)}: cannot assign the pane to a unit: {failure}"
                self.assertEqual(lines, [line] if visible else [])

    def test_two_orphan_processes_have_separate_lines(self) -> None:
        row = replace(unit_membership("show", "unit"), parent=staff.StaffRoot())
        record = staff.StaffSnapshot((row,))
        observed = [labels.LabeledSession("", labels.ClaudeUnknown("not registered"), labels.Place("agents", "w2C"),
                    labels.ObservedProcess(pid, "codex"), labels.LiveSession(row.current_session)) for pid in (1001, 1002)]
        with labels.anomalies() as lines:
            _ = labels.group_labeled_sessions(observed, record=record, sources=labels.MemberSources(()))
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("no showrunner staff parent" in line for line in lines))


class WorkerRunDiscoveryTests(StaffTestCase):
    def test_run_index_keeps_the_staff_run_without_a_worker_record(self) -> None:
        owner = unit_membership("show", "owner")
        worker = fixture_worker("worker", owner.id)
        runs = self.root / "runs"
        run = runs / "marker-run"
        run.mkdir(parents=True)
        with (mock.patch.object(unit_runs, "run_roots", return_value=(runs,)),
              mock.patch.dict(os.environ, {"UNIT_RUN_DIR": "", "PLAN_DELEGATE_BOARD_DIR": ""})):
            _ = staff.set_run(owner.id, staff.OpenRun(run))
            record = staff.local_snapshot()
            found = labels.worker_run_index(record)
            entries = labels.worker_run_entries(record, (worker,))
        self.assertEqual(found[worker.id], (run,))
        self.assertNotIn(worker.id, entries)


    def test_run_index_reads_workers_without_writing_staff(self) -> None:
        owner = unit_membership("show", "owner")
        worker = fixture_worker("worker", owner.id)
        runs = self.root / "runs"
        run = runs / "worker-run"
        run.mkdir(parents=True)
        with (mock.patch.object(unit_runs, "run_roots", return_value=(runs,)),
              mock.patch.dict(os.environ, {"UNIT_RUN_DIR": "", "PLAN_DELEGATE_BOARD_DIR": ""})):
            _ = staff.set_run(owner.id, staff.OpenRun(run))
            before = staff.record_path().read_bytes()
            found = labels.worker_run_index(staff.local_snapshot())
            after = staff.record_path().read_bytes()
        self.assertEqual(found[worker.id], (run,))
        self.assertEqual(after, before)

