"""List recorded unit membership with each unit's current or offline session."""
from __future__ import annotations

import json
import os
import socket
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest import mock


from scripts.lib.socket_test_support import short_socket_directory
from scripts.lib.shell_test_support import shell_functions
from scripts.production import fake_showrunner
from scripts.production import fake_unit_host
from scripts.production.fake_unit_host import HostedUnit, labeled as marked
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, isolated_staff_file, unit_membership, showrunner_relationship, fixture_runtime_id

SCRIPT = Path(__file__).with_name("live_units.py")


class RowDocumentLiveUnitsTests(StaffTestCase):
    def test_read_units_collects_lookup_failures_with_the_same_staff_snapshot(self) -> None:
        import io
        from contextlib import redirect_stderr
        from scripts.production import live_units, session_staff
        from scripts.production import unit_lookup

        fixture = self.row_production()
        expected_sources = unit_lookup.member_sources()
        every = unit_lookup.LabeledProductions({}, {fixture.name: ("member",)})
        errors = io.StringIO()

        def observe(*, record: staff.StaffSnapshot, sources: unit_lookup.MemberSources) -> unit_lookup.LabeledProductions:
            self.assertEqual(record.by_id(fixture.unit.id), fixture.unit)
            self.assertIs(sources, expected_sources)
            session_staff.report_anomaly("pid 123: session record is unreadable", pid=123)
            return every

        with (mock.patch.object(unit_lookup, "member_sources", return_value=expected_sources),
              mock.patch.object(unit_lookup, "every_labeled_session", side_effect=observe),
              redirect_stderr(errors)):
            listing = live_units.read_units(fixture.name)
            legacy = live_units.units_of(fixture.name)

        self.assertEqual([item.unit for item in listing.units], [fixture.unit.name])
        self.assertEqual(listing.anomalies, ("anomaly: pid 123: session record is unreadable",))
        self.assertEqual(legacy, listing.units)
        self.assertEqual(errors.getvalue(), "anomaly: pid 123: session record is unreadable\n")

    def test_unit_report_keeps_unproved_pane_owners_unknown_with_the_process_callback(self) -> None:
        from scripts.agents import pane_record
        from scripts.herdr import herdr
        from scripts.herdr.test_herdr import codex_foreground_cases
        from scripts.message import sessions
        from scripts.production import live_units, staff_sources, unit_lookup

        fixture = self.row_production()
        unit = fixture.unit
        child = fixture_member("unit", name="child", parent=fixture.runner.id)
        place = herdr.HerdrPlace("agents", "w1:p1")
        processes = [
            herdr.HerdrProcess(456, place, {"CODEX_THREAD_ID": unit.current_session}, "codex", str(self.root)),
            herdr.HerdrProcess(457, place, {"CLAUDE_CODE_SESSION_ID": child.current_session}, "claude", str(self.root)),
        ]
        claude = sessions.SessionRecord(pid=457, name="child-claude", sessionId=child.current_session,
                                        procStart="1", updatedAt=1, messagingSocketPath="", tmux="", formerNames=[])
        launch = pane_record.launch(place.pane, unit.current_session, str(self.root), {}, ["codex"], kind="codex")
        pane: herdr.PaneIdentity = {"pane_id": place.pane, "workspace_id": "w1", "session_id": unit.current_session}
        with (codex_foreground_cases(self.root) as cases,
              mock.patch.object(herdr, "live_sessions", return_value=[{"name": place.session}]),
              mock.patch.object(herdr, "shared_session", return_value=place.session),
              mock.patch.object(herdr, "herdr_processes", return_value=processes) as scan,
              mock.patch.object(herdr, "process_placement", return_value=place),
              mock.patch.object(herdr, "process_environment", return_value={"CODEX_THREAD_ID": unit.current_session}),
              mock.patch.object(herdr.Herdr, "listed", return_value=[dict(pane)]),
              mock.patch.object(herdr.Herdr, "result") as read,
              mock.patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.PaneSessionReport(unit.current_session), "1", {})),
              mock.patch.object(unit_lookup, "member_sources", return_value=unit_lookup.MemberSources((launch,))),
              mock.patch.object(unit_lookup, "_claudes", return_value=(
                  {place: {457: [claude]}},
                  unit_lookup.ClaudeRegistryEvidence({}, unit_lookup.RecordsRead())))):
            for case in cases:
                with self.subTest(case=case.name):
                    read.return_value = {"process_info": case.info}
                    listing = live_units.read_units(fixture.name, check=staff_sources.CodexProcessThreads().check)
                    units = {item.unit: item for item in listing.units}
                    self.assertEqual(units[unit.name].status, "idle" if case.owner_proved else "unknown")
                    self.assertEqual(unit_lookup.checked_unit(fixture.name, unit.name), units[unit.name].owner)
                    if case.owner_proved:
                        self.assertEqual(units["child"].status, "exited")
                        self.assertIsNone(units["child"].session)
                    else:
                        self.assertTrue(listing.anomalies)
            scan.return_value = []
            read.return_value = {"process_info": {"foreground_processes": [
                {"pid": 456, "name": "node", "argv": []}]}}
            listing = live_units.read_units(fixture.name, check=staff_sources.CodexProcessThreads().check)
            units = {item.unit: item for item in listing.units}
            self.assertEqual(units[unit.name].status, "unknown")

    def test_unassigned_pane_proof_failure_is_visible_in_the_report(self) -> None:
        from scripts.herdr import herdr
        from scripts.production import live_units, unit_lookup

        fixture = self.row_production()
        place = herdr.HerdrPlace("agents", "w1:p1")
        with (mock.patch.object(herdr, "live_sessions", return_value=[{"name": place.session}]),
              mock.patch.object(herdr, "herdr_processes", return_value=[]),
              mock.patch.object(herdr.Herdr, "listed", return_value=[{"pane_id": place.pane}]),
              mock.patch.object(herdr.Herdr, "result", return_value={"process_info": {
                  "foreground_processes": [{"pid": 456, "name": "node"}]}}),
              mock.patch.object(unit_lookup, "member_sources", return_value=unit_lookup.MemberSources(()))):
            listing = live_units.read_units(fixture.name)
        self.assertEqual([unit.status for unit in listing.units], ["unknown"])
        self.assertTrue(any("herdr:agents:w1:p1" in line and "cannot assign" in line
                            for line in listing.anomalies), listing.anomalies)

    def test_report_preserves_unknown_when_no_unit_can_take_the_observation(self) -> None:
        from scripts.herdr import herdr
        from scripts.message import sessions
        from scripts.production import live_units, staff_sources, unit_lookup
        from scripts.production.staff_test_support import fixture_worker

        fixture = self.row_production()
        worker = fixture_worker("worker", fixture.unit.id)
        running = sessions.SessionRecord(pid=456, sessionId=fixture.unit.current_session, name=fixture.unit.name,
                                         status="idle", updatedAt=1, messagingSocketPath="", tmux="", formerNames=[])
        for case in ("worker candidate", "pane list failure", "unplaced registry process", "placement read failure"):
            visible_sessions = [] if case == "placement read failure" else [{"name": "agents"}]
            visible_registry = [running] if case in ("unplaced registry process", "placement read failure") else []
            with (self.subTest(case=case),
                  mock.patch.object(herdr, "live_sessions", return_value=visible_sessions),
                  mock.patch.object(herdr, "herdr_processes", return_value=[]),
                  mock.patch.object(herdr, "_process_environment", side_effect=PermissionError("placement denied")),
                  mock.patch.object(herdr.Herdr, "listed", return_value=[{
                      "pane_id": "w1:p1", "session_id": worker.current_session if case == "worker candidate" else "",
                  }]) as listed,
                  mock.patch.object(herdr.Herdr, "result", return_value={"process_info": {
                      "foreground_processes": [{"pid": 456, "name": "node"}]}}),
                  mock.patch.object(unit_lookup, "member_sources", return_value=unit_lookup.MemberSources(())),
                  mock.patch.object(sessions, "read_registry", return_value=sessions.RegistryRead(
                      tuple(sessions.RegistryObservation(item, {}) for item in visible_registry), ())),
                  mock.patch.object(sessions, "running_session", return_value=True)):
                if case == "pane list failure":
                    listed.side_effect = OSError("pane list denied")
                listing = live_units.read_units(fixture.name, check=staff_sources.CodexProcessThreads().check)
                self.assertEqual([unit.status for unit in listing.units], ["unknown"])
                self.assertTrue(listing.anomalies)

    def test_incomplete_facts_keep_the_live_unit_and_one_named_problem(self) -> None:
        import io
        from contextlib import redirect_stderr
        from scripts.production import live_units, unit_lookup

        fixture = self.row_production()
        every = unit_lookup.LabeledProductions({}, {fixture.runner.name: ("member",)})
        unavailable = fixture.worktree.with_name("unavailable-member")
        for failure in ("branch", "configuration"):
            with self.subTest(failure=failure):
                if failure == "branch":
                    _ = fixture.worktree.rename(unavailable)
                else:
                    _ = unavailable.rename(fixture.worktree)
                    (fixture.worktree / ".unit.toml").unlink()
                errors = io.StringIO()
                with (redirect_stderr(errors),
                      mock.patch("scripts.production.unit_lookup.every_labeled_session", return_value=every)):
                    units = live_units.units_of(fixture.runner.name)
                self.assertEqual([item.unit for item in units], ["member"])
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn(fixture.unit.name, errors.getvalue())
                self.assertIn(str(fixture.worktree), errors.getvalue())
                if failure == "configuration":
                    self.assertIn(".unit.toml", errors.getvalue())

    def test_units_in_uses_the_staff_name_when_the_document_filename_differs(self) -> None:
        from scripts.production import live_units, session_staff

        fixture = self.row_production()
        every = session_staff.LabeledProductions({}, {fixture.runner.name: ("member",)})
        before = self.staff_file.read_bytes()
        selected = live_units.units_in(fixture.runner.name, every)
        self.assertEqual([unit.unit for unit in selected], ["member"])
        self.assertEqual(self.staff_file.read_bytes(), before)


    def test_unknown_name_has_no_units_in_the_supplied_snapshot(self) -> None:
        from scripts.production import live_units, session_staff

        every = session_staff.LabeledProductions({}, {"known": ("member",)})
        with mock.patch.object(staff, "snapshot") as read_staff:
            self.assertEqual(live_units.units_in("unknown", every), [])
        read_staff.assert_not_called()




class LiveUnitsTests(unittest.TestCase):
    root: Path = Path()
    timers: Path = Path()
    doc: Path = Path()
    records: Path = Path()
    environment: dict[str, str] = {}
    pane_sessions: dict[str, str] = {}

    def test_retired_backfill_does_not_add_a_retained_removed_row_to_the_active_output(self) -> None:
        self.members("live")
        self.hosts({})
        before = self.run_script()
        self.assertEqual(before.returncode, 0, before.stderr)
        retired = unit_membership("show", "retired")
        _ = staff.set_worktree(retired.id, staff.RemovedWorktree("natedev", self.root / "retired"), expected=retired.worktree)
        after = self.run_script()
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(after.stdout, before.stdout)
        self.assertIsNotNone(staff.by_id(retired.id))

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.timers = self.root / "notifier"
        self.doc = self.root / "show-production.md"
        self.records = self.root / "sessions"
        self.records.mkdir()
        self.pane_sessions = {}
        self.environment = {**os.environ, "NOTIFIER_STATE_DIR": str(self.timers),
                            "NOTIFIER_SESSIONS_DIR": str(self.records),
                            "HERDR_PANE_RECORD_DIR": str(self.root / "pane-records"),
                            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
                            "HERDR_PROC": str(self.root / "proc"),
                            "HERDR_PLATFORM": "linux",
                            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
                            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json")}
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.environment))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        _ = showrunner_relationship("show")
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {"stdout": "", "code": 1}}))

    def hosts(self, units: dict[str, HostedUnit]) -> None:
        for unit in units.values():
            identity = unit.marks.get("CODEX_THREAD_ID", "")
            row = staff.by_current_session(identity)
            if row is not None:
                self.pane_sessions[unit.pane] = row.current_session
        fake_unit_host.write(self.environment, list(units.values()))

    def members(self, *units: str) -> None:
        for unit in units:
            _ = unit_membership("show", unit)

    def claude(self, name: str, pane: str) -> None:
        """Record a live Claude in `pane`: this test process stands in for it."""
        from scripts.message import sessions

        if pane:
            fake_unit_host.place_process(self.environment, os.getpid(), pane, exec_shell=True)
        path = Path(self.enterContext(short_socket_directory())) / f"{name}.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        held.listen(128)
        self.addCleanup(held.close)
        _ = (self.records / f"{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": self.pane_sessions[pane], "name": name,
            "procStart": sessions.process_start(os.getpid()), "messagingSocketPath": str(path),
            "updatedAt": 1, "tmux": f"label-at-start:@1.{pane}" if pane else ""}))

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(SCRIPT.parents[1] / "lib/py"), str(SCRIPT), *(arguments or ("--production", "show"))],
                              env=self.environment, capture_output=True, text=True, check=False)

    def test_offline_staff_unit_is_listed_without_a_worktree(self) -> None:
        self.members("offline")
        self.hosts({})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "offline\t\tgone\t\n")

    def test_short_unit_ids_keep_their_pane_spelling(self) -> None:
        self.members("writer", "hook")
        self.hosts({"writer": marked("director", "w4:p1", "writer")})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(result.stdout.splitlines()), [
            "hook\t\tgone\t", "writer\therdr:director:w4:p1\tunknown\t",
        ])

    def test_record_membership_wins_over_a_stale_pane_mark(self) -> None:
        self.members("alpha")
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        receiving = showrunner_relationship("receiving")
        _ = staff.move(row.id, staff.ReportsTo(receiving.id))
        self.hosts({"unit": HostedUnit("director", "w4:p1", {
            "CODEX_THREAD_ID": row.current_session, "STAFF_ID": row.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "show", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha",
        })})
        old = self.run_script()
        self.assertEqual((old.returncode, old.stdout), (0, ""), old.stderr)
        self.doc = self.root / "receiving-production.md"
        new = self.run_script("--production", receiving.name)
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(new.stdout, "alpha\therdr:director:w4:p1\tunknown\t\n")

    def test_each_member_is_listed_with_its_session_as_it_is_now(self) -> None:
        self.members("alpha", "beta")
        alpha = unit_membership("show", "alpha")
        alpha = staff.update_current_session(staff.StaffId(alpha.current_session),
                                             staff.StaffId(fixture_runtime_id("replacement-alpha")))
        self.assertNotEqual(alpha.current_session, alpha.id)
        self.hosts({"$1": marked("beta-label", "w5:p1", "beta"),
                                     "$2": marked("renamed-since-launch", "w4:p1", "alpha"),
                                     "$3": HostedUnit("done-label", "w6:p1", {}),
                                     "$4": marked("other-show", "w7:p1", "gone", production_name="other")})
        self.claude("renamed-since-launch", "w4:p1")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        # The unmarked pane has no member. The other production keeps its own member.
        self.assertEqual(sorted(result.stdout.splitlines()), ["alpha\therdr:renamed-since-launch:w4:p1\tlive\trenamed-since-launch",
                                                      "beta\therdr:beta-label:w5:p1\tunknown\t"])
        script = SCRIPT.with_name("unit_status.sh")
        setup = script.read_text().split("NOTIFIER=", 1)[0].replace(
            "REPO=${0:A:h:h:h}", "REPO=" + shlex.quote(str(SCRIPT.parents[2])))
        functions = shell_functions(script, "waiting_on_user")
        for actor, expected in (("alpha", "BLOCK in beta, open 0h0m:blocked: waiting on alpha\n"),
                                ("session:" + alpha.current_session,
                                 f"BLOCK in beta, open 0h0m:blocked: waiting on session:{alpha.current_session}\n"),
                                ("renamed-since-launch", "=== DECISION for you from beta ===\nblocked: waiting on renamed-since-launch\n=== end beta ===\n")):
            with self.subTest(waiting_on=actor):
                command = setup + functions + "\nSEEN=/dev/null\nclear_block() { :; }\n" + \
                    "state_record() { :; }\nblock_age() { print 0h0m; }\n" + \
                    "waiting_on_user beta " + shlex.quote("blocked: waiting on " + actor) + "\n"
                observed = subprocess.run(["zsh", "-s", "--", str(self.root / "status"), "UTC", "--production", "show"],
                    input=command, env=self.environment, capture_output=True, text=True, check=False)
                self.assertEqual((observed.returncode, observed.stdout, observed.stderr), (0, expected, ""))

    def test_herdr_pane_targets_match_between_live_units_and_lookup(self) -> None:
        self.members("alpha")
        self.hosts({"$1": marked("alpha-label", "w41:p1", "alpha")})
        self.claude("alpha-claude", "w41:p1")
        listed = self.run_script()
        target = subprocess.run([str(SCRIPT.parents[1] / "lib/py"), str(SCRIPT.with_name("unit_lookup.py")),
                                 "pane", "show", "alpha"], env=self.environment,
                                capture_output=True, text=True, check=False)
        self.assertEqual((listed.returncode, target.returncode), (0, 0), listed.stderr + target.stderr)
        self.assertEqual(listed.stdout.split("\t")[1], "herdr:alpha-label:w41:p1")
        self.assertEqual(target.stdout, "herdr:alpha-label:w41:p1\n")

    def test_a_herdr_unit_is_listed_with_its_live_claude(self) -> None:
        self.members("alpha")
        self.hosts({"native": marked("native", "w1:p1", "alpha")})
        self.claude("native-claude", "w1:p1")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "alpha\therdr:native:w1:p1\tlive\tnative-claude\n")

    def test_the_staff_name_selects_the_showrunner_with_a_different_session_name(self) -> None:
        self.members("alpha")
        self.hosts({"$1": marked("alpha-label", "w4:p1", "alpha")})
        _ = fake_showrunner.write_timer(self.timers, "show", "director-id", "America/Los_Angeles", self.doc)
        self.addCleanup(fake_showrunner.write_session(self.records, "director", "director-id").close)
        result = self.run_script("show")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["alpha\therdr:alpha-label:w4:p1\tunknown\t"])

    def test_a_host_that_cannot_say_is_an_error_not_an_empty_list(self) -> None:
        self.members("alpha")
        self.environment["HERDR_BIN"] = str(self.root / "no-such-herdr")
        result = self.run_script()
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertIn("no-such-herdr", result.stderr)

    def test_unknown_production_has_no_units(self) -> None:
        self.hosts({})
        result = self.run_script("--production", "unknown")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_a_showrunner_no_timer_names_exits_one(self) -> None:
        result = self.run_script("missing")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("no showrunner with an update timer is called missing", result.stderr)
        self.hosts({})
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.run_script("--production").returncode, 2)


if __name__ == "__main__":
    _ = unittest.main()
