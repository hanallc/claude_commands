"""Tests for the state of the work a queued item waits on."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.direct import phase_waits, plan_history
from scripts.lib.git_test_support import commit_repository, write_fixture_git_config
from scripts.production import land_queue, land_runs, staff, unit_phases, unit_queue, waits_on


class WaitsOnTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(write_fixture_git_config(self.root)),
                                                  "GIT_CONFIG_NOSYSTEM": "1"}))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _ = self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def test_bulk_phase_merges_keep_the_newest_commit_time_with_one_read(self) -> None:
        output = ("a" * 40 + "\0" + "200\0Merge builds phase 5 (1234567) into sdlc\n"
                  + "b" * 40 + "\0" + "100\0Merge builds phase 5 (7654321) into sdlc\n"
                  + "c" * 40 + "\0" + "300\0Revert Merge builds phase 6 (1234567) into sdlc\n"
                  + "d" * 40 + "\0" + "400\0Merge other phase 5 (1234567) into sdlc\n"
                  + "e" * 40 + "\0" + "500\0Merge upper phase 5 (ABCDEF1) into sdlc\n")
        with patch.object(waits_on, "git", return_value=subprocess.CompletedProcess([], 0, output, "")) as read:
            self.assertEqual(waits_on.landed_phases(self.repo), {
                ("builds", "5"): waits_on.PhaseMerge("a" * 40, 200, "1234567"),
                ("other", "5"): waits_on.PhaseMerge("d" * 40, 400, "1234567"),
                ("upper", "5"): waits_on.PhaseMerge("e" * 40, 500, "abcdef1")})
        read.assert_called_once_with(self.repo, "log", "origin/main", "--merges", "--format=%H%x00%ct%x00%s")

    def test_main_commit_identities_use_one_read_and_ignore_bad_records(self) -> None:
        output = "100\0Checkpoint delivery\n101\0Checkpoint delivery\n100\0Other delivery\nbad\0Invalid time\nmissing separator\n"
        with patch.object(waits_on, "git", return_value=subprocess.CompletedProcess([], 0, output, "")) as read:
            self.assertEqual(waits_on.main_commit_identities(self.repo), frozenset({
                waits_on.CommitIdentity(100, "Checkpoint delivery"), waits_on.CommitIdentity(101, "Checkpoint delivery"),
                waits_on.CommitIdentity(100, "Other delivery")}))
        read.assert_called_once_with(self.repo, "log", "origin/main", "--no-merges", "--format=%at%x00%s")
        with patch.object(waits_on, "git", return_value=subprocess.CompletedProcess([], 128, output, "unreadable")):
            self.assertEqual(waits_on.main_commit_identities(self.repo), frozenset())

    def test_copied_commit_requires_both_identity_fields_and_a_readable_commit(self) -> None:
        identities = frozenset({waits_on.CommitIdentity(100, "Checkpoint delivery")})
        for output, status, expected in (("100\0Checkpoint delivery\n", 0, True),
                                         ("101\0Checkpoint delivery\n", 0, False),
                                         ("100\0Other delivery\n", 0, False),
                                         ("100\0Checkpoint delivery\n", 128, False), ("", 128, False)):
            with self.subTest(output=output, status=status):
                with patch.object(waits_on, "git", return_value=subprocess.CompletedProcess([], status, output, "")) as read:
                    self.assertEqual(waits_on.copied_on_main(self.repo, "a" * 40, identities), expected)
                read.assert_called_once_with(self.repo, "show", "-s", "--format=%at%x00%s", "a" * 40)
        with patch.object(waits_on, "git") as read:
            self.assertFalse(waits_on.copied_on_main(self.repo, "a" * 40, frozenset()))
        read.assert_not_called()

    def test_each_kind_of_work_has_a_state(self) -> None:
        base = commit_repository(self.repo, "base", allow_empty=True)
        _ = self.git("switch", "-q", "-c", "builds-589")
        _ = commit_repository(self.repo, "phase 589", allow_empty=True)
        _ = self.git("switch", "-q", "main")
        _ = self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "merge", "-q", "--no-ff",
                 "-m", "Merge builds phase 589 (1234567) into sdlc", "builds-589")
        main = self.git("rev-parse", "HEAD")
        _ = self.git("update-ref", "refs/remotes/origin/main", main)
        _ = self.git("switch", "-q", "-c", "feature")
        waiting = commit_repository(self.repo, "not landed", allow_empty=True)
        _ = self.git("update-ref", "refs/remotes/origin/remote-only", waiting)
        sent = commit_repository(self.repo, "sent checkpoint", allow_empty=True)

        run = self.root / "handler.jsonl"
        runs: dict[Path, list[dict[str, object]]] = {plan_history.run_events_path(run): [
            {"event_type": "phase_started", "phase_id": "22", "phase_instance_id": "a"},
            {"event_type": "phase_started", "phase_id": "23", "phase_instance_id": "b"},
            {"event_type": "eta_stated", "phase_instance_id": "b", "eta_phase_id": "23", "eta_at": 1_794_760_000},
            {"event_type": "checkpoint", "phase_instance_id": "a", "checkpoint_commit": sent},
            {"event_type": "phase_started", "phase_id": "24", "phase_instance_id": "c"},
            {"event_type": "checkpoint", "phase_instance_id": "c", "checkpoint_commit": base}]}

        def row(name: str, run_state: staff.OpenRun | staff.NoRun) -> staff.StaffRow:
            return staff.StaffRow(staff.StaffId(name), "unit", name, staff.StaffRoot(), current_session=staff.StaffId(name),
                                  run=run_state)

        snapshot = staff.StaffSnapshot((row("handler", staff.OpenRun(run)), row("builds", staff.NoRun())))

        def state(ref: str) -> waits_on.WaitState:
            return waits_on.state(ref, repo=self.repo, snapshot=snapshot, read_run=lambda path: runs.get(path, []))

        self.assertEqual(state(base[:9]), waits_on.OnMain(base))
        self.assertEqual(state("feature"), waits_on.NotOnMain(sent))
        self.assertEqual(state("remote-only"), waits_on.NotOnMain(waiting))
        self.assertEqual(state("no-such-branch"), waits_on.NotFound())
        self.assertEqual(state("builds/589"), waits_on.OnMain(main))
        self.assertEqual(state("builds/590"), waits_on.PhaseNotStarted())
        self.assertEqual(state("handler/22"), waits_on.PhaseSent(sent))
        self.assertEqual(state("handler/23"), waits_on.PhaseOpen(1_794_760_000.0))
        self.assertEqual(state("handler/24"), waits_on.OnMain(base))
        self.assertEqual(state("handler/25"), waits_on.PhaseNotStarted())
        self.assertEqual(state("nobody/1"), waits_on.NotFound())
        self.assertEqual(waits_on.carried(self.repo, sent), [sent, waiting])
        self.assertEqual(waits_on.carried(self.repo, main), [])
        self.assertEqual(waits_on.carried(self.repo, "f" * 40), [])

        path = plan_history.run_events_path(run)
        runs[path].extend([
            {"event_type": "wait_started", "wait_scope": "unit", "target_work": "22", "wait_id": "hold",
             "wait_by": "sdlc", "wait_reason": "the user approves board-reasons", "wait_kind": "WaitingForUser",
             "wait_started_epoch": 1_794_749_000, "wait_attribution": {"state": "UnitTarget"}},
            {"event_type": "wait_started", "wait_scope": "unit", "target_work": "23", "wait_id": "other",
             "wait_by": "builds", "wait_reason": "the build finishes", "wait_kind": "WaitingForSupplier",
             "wait_started_epoch": 1_794_749_100, "wait_attribution": {"state": "UnitTarget"}}])
        reads: list[Path] = []

        def read_run(path: Path) -> list[dict[str, object]]:
            reads.append(path)
            return runs.get(path, [])

        held = waits_on.state("handler/22", repo=self.repo, snapshot=snapshot, read_run=read_run)
        self.assertEqual(held, waits_on.PhaseSent(sent, phase_waits.UnitWait(
            "22", "sdlc", "the user approves board-reasons", "WaitingForUser", 1_794_749_000, "hold")))
        self.assertEqual(reads, [path])
        runs[path].append({"event_type": "wait_finished", "wait_id": "hold"})
        self.assertEqual(state("handler/22"), waits_on.PhaseSent(sent))

    def test_work_after_a_checkpoint_reopens_only_an_unfinished_phase(self) -> None:
        base = commit_repository(self.repo, "base", allow_empty=True)
        _ = self.git("update-ref", "refs/remotes/origin/main", base)
        sent = commit_repository(self.repo, "sent", allow_empty=True)
        member = staff.StaffRow(staff.StaffId("handler"), "unit", "handler", staff.StaffRoot(),
                                current_session=staff.StaffId("handler-session"), run=staff.OpenRun(self.root / "run"))
        for commit in (base, sent):
            for start in ("pass_started", "activity_started"):
                for finished in (False, True):
                    with self.subTest(commit=commit, start=start, finished=finished):
                        events: list[dict[str, object]] = [
                            {"event_type": "phase_started", "phase_id": "23", "phase_instance_id": "current"},
                            {"event_type": "checkpoint", "phase_instance_id": "current", "checkpoint_commit": commit}]
                        if finished:
                            events.append({"event_type": "phase_finished", "phase_instance_id": "current"})
                        events.extend([
                            {"event_type": start, "phase_instance_id": "current"},
                            {"event_type": "eta_stated", "eta_phase_id": "23", "eta_at": 1_794_760_000}])
                        found = waits_on.phase_state(self.repo, member, "23", lambda _: events)
                        expected = (waits_on.OnMain(base) if commit == base else waits_on.PhaseSent(sent))
                        self.assertEqual(found, expected if finished else waits_on.PhaseOpen(1_794_760_000.0))
                        if not finished:
                            with patch.object(waits_on, "landed_phase", return_value=base):
                                self.assertEqual(waits_on.phase_state(self.repo, member, "23", lambda _: events),
                                                 waits_on.PhaseOpen(1_794_760_000.0))
                            events.append({"event_type": "checkpoint", "phase_instance_id": "current",
                                           "checkpoint_commit": sent})
                            self.assertEqual(waits_on.phase_state(self.repo, member, "23", lambda _: events),
                                             waits_on.PhaseSent(sent))

    def test_bare_commit_dependencies_have_a_commit_label(self) -> None:
        view = land_queue.LandView({}, {}, {}, frozenset(), {})
        cases: tuple[tuple[str, waits_on.WaitState, tuple[str, ...], str], ...] = (
            ("e75b8ec06", waits_on.NotOnMain("e75b8ec06" + "a" * 31), (), "commit e75b8ec06"),
            ("e75b8ec", waits_on.NotFound(), (), "commit e75b8ec"),
            ("E75B8EC06" + "A" * 31, waits_on.NotFound(), (), "commit E75B8EC06"),
            ("release", waits_on.NotOnMain("b" * 40), (), "branch release"),
            ("feature/report", waits_on.NotOnMain("b" * 40), (), "branch feature/report"),
            ("e75b8ec06", waits_on.PhaseOpen(None), ("e75b8ec06",), "e75b8ec06"),
            ("1234567", waits_on.PhaseNotStarted(), ("1234567",), "phase 1234567"),
            ("handler/e75b8ec06", waits_on.PhaseOpen(None), (), "e75b8ec06"),
            ("other/e75b8ec06", waits_on.PhaseOpen(None), (), "other phase e75b8ec06"))
        for reference, found, phase_ids, label in cases:
            with self.subTest(reference=reference, found=found):
                item = unit_queue.QueuedItem("Read delivery", reference)
                self.assertEqual(unit_phases.queued_text(item, found, view, now=0, zone=ZoneInfo("UTC"),
                    unit="handler", phase_ids=phase_ids).state_text,
                    f"After {label}; " + ("not on main yet" if isinstance(found, waits_on.NotFound)
                        and label.startswith("commit ") else unit_phases.wait_text(found, view, now=0, zone=ZoneInfo('UTC'))))

    def test_missing_commits_wait_for_main_without_a_missing_dependency_claim(self) -> None:
        view = land_queue.LandView({}, {}, {}, frozenset(), {})
        for reference, phase_ids, expected in (
                ("e75b8ec06", (), "After commit e75b8ec06; not on main yet"),
                ("E75B8EC06" + "A" * 31, (), "After commit E75B8EC06; not on main yet"),
                ("e75b8ec06", ("e75b8ec06",), "After e75b8ec06; dependency not found locally"),
                ("other/23", (), "After other phase 23; dependency not found locally"),
                ("missing-ref", (), "After missing-ref; dependency not found locally")):
            with self.subTest(reference=reference, phase_ids=phase_ids):
                entry = unit_phases.queued_text(unit_queue.QueuedItem("Read delivery", reference), waits_on.NotFound(),
                    view, now=0, zone=ZoneInfo("UTC"), unit="handler", phase_ids=phase_ids)
                self.assertEqual(entry.state_text, expected)

    def test_the_queued_cell_names_the_wait_and_its_state(self) -> None:
        now, utc = 1_794_750_000.0, ZoneInfo("UTC")
        failed: land_runs.LandResult = {"commit": "c" * 40, "production": "builds", "unit": "report", "phase": "89",
                                        "landed": False, "step": "red", "state": "failed", "detail": "test_x red",
                                        "time": datetime.fromtimestamp(now - 600, UTC).isoformat()}
        line = {"c" * 40: land_queue.Place(10, "builds batch589", "report 89")}
        item = unit_queue.QueuedItem("acctland: Merge the Accounts page", "builds/589")

        def text(found: waits_on.WaitState | None, running: frozenset[str] | None = None,
                 results: dict[str, land_runs.LandResult] | None = None) -> unit_phases.WorkItem:
            view = land_queue.LandView(dict.fromkeys(running or (), ""), line, results or {}, frozenset(), {})
            return unit_phases.queued_text(item, found, view, now=now, zone=utc)

        self.assertEqual(text(waits_on.OnMain("a" * 40)),
                         unit_phases.WorkItem("Merge the Accounts page (acctland)", "Ready"))
        landed: land_runs.LandResult = {**failed, "commit": "d" * 40, "landed": True}
        held = waits_on.PhaseSent("d" * 40, phase_waits.UnitWait(
            "589", "sdlc", "the user approves board-reasons", "WaitingForUser", now - 600, "hold"))
        approval = "Sdlc keeps it waiting until the user approves board-reasons; since 13:30"
        cases: tuple[tuple[waits_on.WaitState, frozenset[str], dict[str, land_runs.LandResult], str], ...] = (
            (waits_on.PhaseSent("c" * 40), frozenset(), {}, "After builds phase 589; 10th in the merge line"),
            (waits_on.PhaseSent("e" * 40), frozenset(), {}, "After builds phase 589; not in the merge line"),
            (waits_on.NotOnMain("c" * 40), frozenset(), {"c" * 40: failed},
             "After branch builds/589; 10th in the merge line; last attempt failed at red: test_x red; 13:30"),
            (waits_on.PhaseSent("c" * 40), frozenset({"c" * 9}), {}, "After builds phase 589; merging now"),
            (waits_on.NotOnMain("d" * 40), frozenset(), {}, "After branch builds/589; not on main yet"),
            (waits_on.PhaseOpen(now + 3600), frozenset(), {}, "After builds phase 589; started"),
            (waits_on.PhaseOpen(None), frozenset(), {}, "After builds phase 589; started"),
            (waits_on.PhaseNotStarted(), frozenset(), {}, "After builds phase 589; not started"),
            (waits_on.NotFound(), frozenset(), {}, "After builds phase 589; dependency not found locally"),
            (waits_on.PhaseSent("d" * 40), frozenset(), {"d" * 40: landed}, "After builds phase 589; merged 13:30"),
            (held, frozenset(), {}, approval),
            (held, frozenset({"d" * 40}), {}, "After builds phase 589; merging now"),
            (held, frozenset(), {"d" * 40: landed}, "After builds phase 589; merged 13:30"),
            (held, frozenset(), {"d" * 40: {**failed, "commit": "d" * 40}},
             "After builds phase 589; did not merge: failed at red: test_x red; 13:30"),
            (waits_on.PhaseSent("c" * 40, held.wait), frozenset(), {}, "After builds phase 589; 10th in the merge line"),
        )
        for found, running, results, expected in cases:
            with self.subTest(state=found, running=running, results=results):
                self.assertEqual(text(found, running, results), unit_phases.WorkItem(
                    "Merge the Accounts page (acctland)", expected))
        view = land_queue.LandView({}, {}, {}, frozenset(), {})
        for queued, expected in (
                ("plain", "Plain"),
                ("plugin: the board plugin draws every row", "The board plugin draws every row (plugin)"),
                ("acctcolor: One color: every reader", "One color: every reader (acctcolor)")):
            with self.subTest(queued=queued):
                self.assertEqual(unit_phases.queued_text(unit_queue.QueuedItem(queued), None,
                                                         view, now=now, zone=utc),
                                 unit_phases.WorkItem(expected, "Ready"))
        for reference, expected in (("handler/41", "After phase 41; not started"),
                                    ("handler/acctcolor", "After acctcolor; not started"),
                                    ("other/23", "After other phase 23; not started")):
            with self.subTest(reference=reference):
                self.assertEqual(unit_phases.queued_text(unit_queue.QueuedItem("Read delivery", reference),
                    waits_on.PhaseNotStarted(), view, now=now, zone=utc, unit="handler"),
                    unit_phases.WorkItem("Read delivery", expected))

    def test_the_first_row_names_each_sent_checkpoint_of_the_unit(self) -> None:
        now, utc = 1_794_750_000.0, ZoneInfo("UTC")
        held: land_runs.LandResult = {"commit": "e" * 40, "production": "sdlc", "unit": "shelled", "phase": "center-column",
                                      "landed": False, "step": "conflicts", "state": "held", "detail": "conflict: stdout: 1234\nCONFLICT (content): Merge conflict in docs/a.md\nCONFLICT (content): Merge conflict in b.md",
                                      "time": datetime.fromtimestamp(now - 600, UTC).isoformat()}
        later: land_runs.LandResult = {**held, "commit": "f" * 40, "phase": "images", "time": datetime.fromtimestamp(now, UTC).isoformat()}
        older: land_runs.LandResult = {**later, "commit": "9" * 40, "landed": True, "time": datetime.fromtimestamp(now - 900, UTC).isoformat()}
        line = {"a" * 40: land_queue.Place(1, "sdlc first", "shelled shot-rule"),
                "b" * 40: land_queue.Place(2, "sdlc pair", "shelled workers-why"),
                "c" * 40: land_queue.Place(2, "sdlc pair", "shelled land-line"),
                "d" * 40: land_queue.Place(3, "builds batch9", "shelledx 2")}
        view = land_queue.LandView({"0" * 40: "shelled images-card", "1" * 40: "other 1"}, line,
                                   {"e" * 40: held, "f" * 40: later, "9" * 40: older}, frozenset(), {})

        failures = (
            unit_phases.WorkItem("center-column", "sdlc cannot merge it: merge conflict in docs/a.md, b.md"),
            unit_phases.WorkItem("Images", "sdlc cannot merge it: merge conflict in docs/a.md, b.md"))
        running = unit_phases.WorkItem("images-card", "sdlc merges it now")
        first = unit_phases.WorkItem("shot-rule", "1st in sdlc's merge line")
        paired = (unit_phases.WorkItem("workers-why", "2nd in sdlc's merge line"),
                  unit_phases.WorkItem("land-line", "Merges with workers-why"))
        carried_view = view._replace(carried_line={"7" * 40: line["c" * 40]})
        running_view = view._replace(running={"0" * 40: "shelled images-card", "8" * 40: "shelled live-owner"},
                                    carried_running=frozenset({"7" * 40}))
        cases: list[tuple[str, land_queue.LandView, dict[str, unit_phases.PhaseSentWork], bool,
                          tuple[unit_phases.WorkItem, ...], frozenset[str]]] = [
            ("no open rows", view, {}, False, (running, first, *paired, *failures), frozenset()),
            ("open rows before other phases", view,
             {"images-card": unit_phases.SentCommit("0" * 40), "shot-rule": unit_phases.SentCommit("a" * 40),
              "land-line": unit_phases.SentCommit("c" * 40), "images": unit_phases.SentCommit("f" * 40)}, True,
             (running, first, unit_phases.WorkItem("land-line", "2nd in sdlc's merge line"),
              unit_phases.WorkItem("workers-why", "Merges with land-line"), failures[1]),
             frozenset({"land-line"})),
            ("single open row", view, {"shot-rule": unit_phases.SentCommit("a" * 40)}, False,
             (running, first, *paired, *failures), frozenset()),
            ("carried commit joins its item", carried_view,
             {"workers-why": unit_phases.SentCommit("b" * 40), "checkpoint-wait": unit_phases.SentCommit("7" * 40),
              "land-line": unit_phases.SentCommit("c" * 40)}, False,
             (running, first, paired[0], unit_phases.WorkItem("checkpoint-wait", "Merges with workers-why"),
              paired[1], *failures), frozenset({"workers-why", "checkpoint-wait", "land-line"})),
            ("running direct and carried commits", running_view,
             {"carried": unit_phases.SentCommit("7" * 40), "images-card": unit_phases.SentCommit("0" * 40)}, False,
             (unit_phases.WorkItem("Carried", "sdlc merges it now"),
              unit_phases.WorkItem("images-card", "Merges with carried"),
              unit_phases.WorkItem("live-owner", "Merges with carried"), first, *paired, *failures),
             frozenset({"carried", "images-card"})),
            ("single running open row", view, {"images-card": unit_phases.SentCommit("0" * 40)}, False,
             (running, first, *paired, *failures), frozenset()),
            ("open failed work remains current", view, {"images": unit_phases.SentCommit("f" * 40)}, False,
             (running, first, *paired, failures[1], failures[0]), frozenset()),
            ("carried phase has its live rank", view._replace(carried_line={
                "7" * 40: land_queue.Place(4, "builds batch", "builder carrier")}),
             {"carried": unit_phases.SentCommit("7" * 40)}, False,
             (running, first, *paired, unit_phases.WorkItem("Carried", "4th in sdlc's merge line"), *failures), frozenset()),
            ("checkpoint has no line", view,
             {"absent": unit_phases.SentCommit("6" * 40), "no-hash": unit_phases.SentCommitUnrecorded(),
              "not-sent": unit_phases.NoSentWork()}, False,
             (running, first, *paired,
              unit_phases.WorkItem("Absent", "Checkpoint 666666666 at time not recorded; not in sdlc's merge line"),
              unit_phases.WorkItem("no-hash", "Merge position not recorded"), *failures), frozenset()),
        ]
        for name, found, open_sent, on_main, expected, grouped in cases:
            with self.subTest(group=name):
                result = unit_phases.unit_lands("shelled", found, open_sent=open_sent, now=now, zone=utc,
                                                on_main=lambda _commit: on_main, production="sdlc")
                self.assertEqual(result.entries, expected)
                self.assertEqual(result.grouped_phases, grouped)
                self.assertTrue(result.pending)
        result = unit_phases.unit_lands("nobody", view, open_sent={}, now=now, zone=utc, on_main=lambda _commit: False)
        self.assertEqual(result.entries, ())
        self.assertEqual(result.grouped_phases, frozenset())
        self.assertFalse(result.pending)
        numbered = land_queue.LandView({}, {
            "1" * 40: land_queue.Place(2, "sdlc batch", "shelled 40"),
            "2" * 40: land_queue.Place(2, "sdlc batch", "shelled 41")}, {}, frozenset(), {})
        result = unit_phases.unit_lands("shelled", numbered, open_sent={}, now=now, zone=utc,
                                       on_main=lambda _commit: False, production="sdlc")
        self.assertEqual(result.entries, (unit_phases.WorkItem("40", "2nd in sdlc's merge line"),
                                          unit_phases.WorkItem("41", "Merges with phase 40")))


if __name__ == "__main__":
    _ = unittest.main()
