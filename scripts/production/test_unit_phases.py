"""Tests for each unit's staff name, phase and task."""
from __future__ import annotations

from scripts.agents import remote

import contextlib
import io
import json
import os
import re
import sqlite3
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from dataclasses import replace
from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from typing import NamedTuple, cast, override
from unittest.mock import Mock, patch


from scripts.production import census, land_queue, land_runs, live_units, unit_queue
from scripts.accounts import session_state
from scripts.lib import unit_runs
from scripts.lib.git_test_support import commit_repository, write_fixture_git_config
from scripts.message import sessions
from scripts.production import showrunners
from scripts.production import unit_lookup
from scripts.production import waits_on
from scripts.herdr.herdr import HerdrPlace
from scripts.production import unit_phases
from scripts.production.production_doc import read_production
from scripts.production import style_units
from scripts.style import style_ledger
from scripts.style.ledger_test_support import write_style_units
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file, fixture_member, fixture_runtime_id, recorded_unit

from scripts.direct import phase_table
from scripts.direct import plan_history


def unit(name: str, claude: unit_lookup.Claude | None) -> live_units.LiveUnit:
    member = next((row for row in staff.snapshot().rows if row.kind == "unit" and row.name == name),
                  session_state.UnrecordedStaff())
    activity = (session_state.LiveActivity("idle") if isinstance(claude, unit_lookup.LiveClaude)
                else session_state.NoLiveSession())
    observed = unit_lookup.LabeledSession(name, claude, HerdrPlace(f"label-of-{name}", "w1:p1")) if claude is not None else None
    machine = member.worktree.machine if isinstance(member, staff.StaffRow) and isinstance(member.worktree, staff.PresentWorktree) else ""
    return live_units.LiveUnit(name, observed, status=session_state.classify(member, activity), member=member, machine=machine)


def live(name: str) -> unit_lookup.LiveClaude:
    return unit_lookup.LiveClaude(name, f"{name}-id", f"/run/{name}.sock", 1)


def typed_record(plan: Path, *statuses: str) -> phase_table.PhaseRecord:
    now = datetime(2026, 10, 16, 18, tzinfo=UTC)
    rows: list[phase_table.DonePhase | phase_table.OpenPhase | phase_table.TodoPhase] = []
    current: phase_table.OpenPhase | phase_table.NoOpenPhase = phase_table.NoOpenPhase()
    for index, status in enumerate(statuses, 1):
        if status == "done":
            rows.append(phase_table.DonePhase(str(index), f"Delivery {index}", phase_table.PhaseTimingNotRecorded()))
        elif status == "running":
            current = phase_table.OpenPhase(str(index), f"Delivery {index}", now,
                phase_table.PhaseProgressNotReported(), phase_table.EtaUnavailable(),
                phase_table.EtaNeverStated(), f"instance-{index}")
            rows.append(current)
        else:
            rows.append(phase_table.TodoPhase(str(index), f"Delivery {index}", phase_table.PhaseTimingNotPredicted()))
    return phase_table.PhaseRecord(plan, now, current, rows, phase_table.UnknownFinish())


LOCAL = census.Census((census.CensusMachine("natedev", True),), (), ())


def session_of(member: staff.StaffRow, status: str | None) -> census.CensusSession:
    return census.CensusSession(member.current_session, status)


def census_worker(name: str, parent: staff.StaffId, *, status: str | None = "busy", activity: str | None = None,
                  phase: str = "", runs: Sequence[census.CensusRun] = ()) -> census.CensusWorker:
    assignment = census.CensusPass("impl", phase) if phase else census.NoPass()
    return census.CensusWorker(staff.StaffId(f"{name}-id"), name, parent, status, activity, assignment, tuple(runs))


def tiles(*, sessions: Sequence[census.CensusSession] = (),
          workers: Sequence[census.CensusWorker] = ()) -> census.Census:
    return LOCAL._replace(sessions=tuple(sessions), workers=tuple(workers))


def census_for(units: Sequence[live_units.LiveUnit]) -> census.Census:
    """List each recorded unit that is not exited, with the status word of its live classification."""
    return tiles(sessions=[session_of(selected.member, selected.status) for selected in units
                           if isinstance(selected.member, staff.StaffRow) and selected.status != "exited"])


def table_of(units: list[live_units.LiveUnit], records: dict[str, object]) -> str:
    return unit_phases.table(units, records, columns=1000, census_read=census_for(units))


class DetailRow(NamedTuple):
    """One item, work name, and state from the unit's table."""

    item: str
    work: str
    state: str


def detail_rows(rendered: str, name: str) -> list[DetailRow]:
    """Read one unit's table and join its continued cells."""
    rows: list[DetailRow] = []
    selected = False
    for line in rendered.splitlines():
        if line.startswith("**"):
            if selected:
                break
            selected = line.startswith(f"**{name}**")
        if not selected or not line.startswith("| "):
            continue
        cells = [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
        if cells in (["Item", "Work", "State"], ["---", "---", "---"]):
            continue
        if len(cells) != 3:
            raise AssertionError(f"Expected three cells: {line}")
        item, work, state = cells
        if rows and rows[-1][0] == item and (not work or not state):
            previous = rows.pop()
            rows.append(DetailRow(item, " ".join(filter(None, (previous.work, work))),
                                  " ".join(filter(None, (previous.state, state)))))
        else:
            rows.append(DetailRow(item, work, state))
    return rows


class UnitPhasesTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(patch.object(unit_runs, "RUN_ROOT", self.root / "runs"))
        config = self.root / "table-conf"
        _ = cast(object, shutil.copytree(Path(__file__).resolve().parents[2] / "config", config))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config)})))
        _ = self.enterContext(patch("socket.gethostname", return_value="natedev"))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        from scripts.message import process_table
        sessions = self.root / "sessions"
        sessions.mkdir()
        self.enterContext(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(sessions),
                                                    "UNIT_HISTORY_DIR": str(self.root / "history"),
                                                    "UNIT_QUEUE_STATE_DIR": str(self.root / "queue"),
                                                    "XDG_STATE_HOME": str(self.root / "state"),
                                                    "MERGE_GATE_STATE_DIR": str(self.root / "merge-gate")}))
        _ = self.enterContext(patch.object(process_table, "snapshot", return_value={}))
        _ = self.enterContext(patch.object(census, "read", return_value=LOCAL))
        _ = self.enterContext(patch.object(unit_phases, "REPOSITORY", self.root))

    def test_unit_count_cache_filters_sources_and_retries_failed_reads(self) -> None:
        from scripts.statusline import counts_row
        from scripts.production import session_staff

        doc = self.root / "production.md"
        ledger = self.root / "efficiency.md"
        _ = doc.write_text("# Production\n\n> **Status: PRODUCTION — running**\n"
                           + f"- **Efficiency ledger:** {ledger}\n")
        owner = fixture_member("showrunner", name="cache-owner", doc=staff.LiveDoc("natedev", doc))
        member = fixture_member("unit", name="cache-unit", parent=owner.id)
        _ = ledger.write_text(f"- 2026-11-12 config: Own [by: {member.id}] — proposed\n"
                              + f"- 2026-11-12 config: Other [by: {owner.id}] — proposed\n")
        notifier = self.root / "notifier"
        instance = notifier / f"showrunner-{owner.id}"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            f"TARGET=session:{owner.current_session}\nCHECK=zsh /x/production_check.sh {owner.id}\n")
        _ = (self.root / "table-conf/statusline.conf").write_text("# whole: 1..\ncounts.retry_seconds=7\n")
        environment = {"HOME": str(self.root), "NOTIFIER_STATE_DIR": str(notifier),
                       "SHOWRUNNER_STATE_DIR": str(self.root / "state"),
                       "CHANGE_REVIEW_DIR": str(self.root), "REVIEW_LINKS_ROOT": str(self.root / "links")}
        read_text = Path.read_text

        def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == ledger:
                raise PermissionError(str(path))
            return read_text(path, encoding=encoding, errors=errors)

        with (patch.dict(os.environ, environment),
              patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(()))):
            cache = counts_row.write_unit(member.id)
            self.assertEqual(cache.read_text().splitlines()[:2], ["For you: 1 efficiency", ""])
            with patch.object(Path, "read_text", read), patch.object(time, "time", return_value=1000):
                cache = counts_row.write_unit(member.id)
            line, due, *sources = cache.read_text().splitlines()
            self.assertEqual((line, due), ("", "1007"))
            self.assertIn(str(ledger), sources)
            self.assertIn(str(doc), sources)
            self.assertIn(f"source=unit:{member.id}", sources)
            cache = counts_row.write_unit(member.id)
            self.assertEqual(cache.read_text().splitlines()[:2], ["For you: 1 efficiency", ""])

    def member(self, name: str) -> staff.StaffRow:
        owner = fixture_member("showrunner", name="sdlc")
        occupied = any(isinstance(row.worktree, staff.PresentWorktree) and row.worktree.path == self.root
                       for row in staff.snapshot().rows)
        tree = self.root / name if occupied else self.root
        tree.mkdir(exist_ok=True)
        return fixture_member("unit", name=name, parent=owner.id,
            worktree=staff.PresentWorktree("natedev", tree))

    def write_run(self, member: staff.StaffRow, name: str, phases: Sequence[tuple[str, str]],
                  tail: Sequence[dict[str, object]] = (), *, started: float = 100,
                  plan: str = "") -> Path:
        history = self.root / "history/runs"
        history.mkdir(parents=True, exist_ok=True)
        folder = self.root / "runs" / name
        folder.mkdir(parents=True, exist_ok=True)
        header: dict[str, object] = {"event_type": "run_started", "run_id": name,
            "owner_staff_id": member.id, "owner_session": member.current_session,
            "working_dir": str(self.root), "plan_doc": plan, "run_started_at": started}
        _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps(header))
        events = [header, *({"event_type": "phase_started", "phase_id": phase,
            "phase_title": title, "phase_instance_id": phase + "-instance",
            "phase_started_at": started + index + 1, "timestamp_epoch": started + index + 1}
            for index, (phase, title) in enumerate(phases)), *tail]
        path = history / (name + ".jsonl")
        _ = path.write_text("".join(json.dumps(row) + "\n" for row in events))
        _ = staff.set_run(member.id, staff.OpenRun(folder))
        return path

    def phase_event(self, kind: str, phase: str, **fields: object) -> dict[str, object]:
        return {"event_type": kind, "phase_id": phase, "phase_instance_id": phase + "-instance", **fields}

    def queue(self, member: staff.StaffRow, *items: unit_queue.QueuedItem) -> Path:
        path = unit_queue.state_path(member.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps([unit_queue.stored(item) for item in items]))
        return path

    def blocks(self, members: Sequence[staff.StaffRow], records: dict[str, object], *,
               workers: Sequence[census.CensusWorker] = (), statuses: Sequence[str] = (), columns: int = 1000) -> str:
        return unit_phases.table([unit(row.name, live(row.name)) for row in members], records,
            now=datetime(2026, 11, 15, 18, tzinfo=UTC).timestamp(), zone=ZoneInfo("UTC"),
            columns=columns,
            census_read=tiles(workers=workers, sessions=[session_of(row, statuses[index] if statuses else "idle")
                                                       for index, row in enumerate(members)]))

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def repository(self) -> str:
        self.enterContext(patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(write_fixture_git_config(self.root)), "GIT_CONFIG_NOSYSTEM": "1"}))
        _ = self.git("init", "-q", "-b", "main")
        base = commit_repository(self.root, "Base", allow_empty=True)
        _ = self.git("update-ref", "refs/remotes/origin/main", base)
        return base

    def test_unit_name_comes_from_staff_for_every_session_state(self) -> None:
        member = self.member("namecheck")
        for session in (None, live("another-session"), unit_lookup.ClaudeNotRunning(),
                        unit_lookup.ClaudeUnknown("no records")):
            with self.subTest(session=session):
                rendered = table_of([unit(member.name, session)], {})
                status = "idle" if isinstance(session, unit_lookup.LiveClaude) else "exited"
                self.assertEqual(rendered.splitlines(), [
                    "working 0 · waiting to merge 0 · waiting on other work 0 · idle 1 · 1 units", "",
                    "| Unit | Status |", "| --- | --- |", f"| namecheck | {status} |", "",
                    "**namecheck** — idle", "",
                    "| Item | Work | State |", "| --- | --- | --- |",
                    "| Now | Not recorded | Director's current step not recorded |",
                    "| Next | Not recorded | The director has not chosen the next task |"])

    def test_blocks_keep_full_text_at_each_width(self) -> None:
        member = self.member("wide")
        title = "Review 界面 | delivery " + "é" * 180
        queued = "Read the complete report " + "long_identifier_" * 20
        _ = self.write_run(member, "wide", [("review", title)], [
            self.phase_event("activity_started", "review", activity_instance_id="activity",
                             activity_text="checks " + "all details " * 30)])
        _ = self.queue(member, unit_queue.QueuedItem(queued))
        for columns in (40, 80, 120, 180):
            with self.subTest(columns=columns), patch.dict(os.environ, {"COLUMNS": str(columns)}):
                rendered = self.blocks([member], {}, columns=columns)
                blocks = rendered[rendered.index("**wide**"):]
                rows = detail_rows(rendered, "wide")
                self.assertEqual([(row.item, "".join(row.work.split())) for row in rows],
                                 [("Now", "".join((title + " (review)").split())),
                                  ("Next", "".join(queued.split()))])
                self.assertEqual(" ".join(rows[0][2].split()),
                                 "Director checks " + ("all details " * 30).strip())
                self.assertIn("\\|", blocks)
                self.assertEqual(rendered.count("**wide**"), 1)
                self.assertEqual(rendered.count("| Unit |"), 1)
                self.assertIn("**wide** — working\n\n| Item | Work | State |\n", blocks)
                self.assertGreater(sum(line.startswith("| Now |") for line in blocks.splitlines()), 1)
                for text in ("in work", "merged today", "fix rounds", "Non-Phase Work", "nothing open",
                             "Workers |", "…"):
                    self.assertNotIn(text, rendered)

    def test_blocks_sort_units_and_show_machines_only_when_needed(self) -> None:
        members = [self.member(name) for name in ("zulu", "Beta", "alpha")]
        selected = [unit(row.name, live(row.name)) for row in members]
        selected[1] = selected[1]._replace(machine="mac")
        rendered = unit_phases.table(selected, {}, census_read=census_for(selected))
        self.assertEqual([line for line in rendered.splitlines() if line.startswith("**")],
                         ["**alpha** (natedev) — idle", "**Beta** (mac) — idle", "**zulu** (natedev) — idle"])
        self.assertEqual(rendered.count("\n\n"), 7)
        self.assertEqual(rendered.count("| Item | Work | State |"), 3)
        self.assertEqual(rendered.count("| Now |"), 3)
        self.assertEqual(rendered.count("| Next |"), 3)
        for field in ("Also", "Wait", "Merge"):
            self.assertNotIn(f"| {field} |", rendered)
        self.assertEqual(unit_phases.table([], {}, census_read=LOCAL).strip(),
                         "working 0 · waiting to merge 0 · waiting on other work 0 · idle 0 · 0 units")

    def test_phase_names_the_open_phase_or_where_the_plan_stands(self) -> None:
        member = self.member("plan")
        cases: list[tuple[object, str, DetailRow, DetailRow, tuple[tuple[str, str], ...], unit_phases.PlanSource]] = [
            (typed_record(self.root / "plan.md", "done", "todo", "todo"), "unknown",
             DetailRow("Now", "Not recorded", "Director's current step not recorded"),
             DetailRow("Next", "Delivery 2 (2)", "Not started"), (), unit_phases.PlanSource.READABLE),
            (typed_record(self.root / "plan.md", "done"), "idle",
             DetailRow("Now", "Not recorded", "Director's current step not recorded"),
             DetailRow("Next", "Not recorded", "Nothing queued; the plan is complete"), (), unit_phases.PlanSource.READABLE),
            ({"current": None, "phases": []}, "idle",
             DetailRow("Now", "Not recorded", "Director's current step not recorded"),
             DetailRow("Next", "Not recorded", "The director has not chosen the next task"), (), unit_phases.PlanSource.EMPTY),
            (None, "idle", DetailRow("Now", "Not recorded", "Director's current step not recorded"),
             DetailRow("Next", "Not recorded", "The director has not chosen the next task"), (), unit_phases.PlanSource.ABSENT),
            ({"unavailable": "plan unreadable"}, "unknown",
             DetailRow("Now", "Not recorded", "Director's current step not recorded"),
             DetailRow("Next", "Not recorded", "The director has not chosen the next task"), (), unit_phases.PlanSource.UNAVAILABLE),
            (None, "unknown", DetailRow("Now", "Current work (extra)", "Director's current step not recorded"),
             DetailRow("Next", "Not recorded", "The director has not chosen the next task"),
             (("extra", "Current work"),), unit_phases.PlanSource.ABSENT),
        ]
        original_facts = unit_phases.task_facts
        for record, bucket, now, next_text, phases, source in cases:
            with self.subTest(record=record, phases=phases):
                seen: list[unit_phases.UnitTaskFacts] = []

                def facts(selected: object, events: Sequence[dict[str, object]]) -> unit_phases.UnitTaskFacts:
                    found = original_facts(selected, events)
                    seen.append(found)
                    return found

                _ = self.write_run(member, "plan", phases)
                with patch.object(unit_phases, "task_facts", side_effect=facts):
                    rendered = self.blocks([member], {member.name: record})
                self.assertIn(f"**plan** — {bucket}", rendered)
                rows = detail_rows(rendered, "plan")
                self.assertIn(now, rows)
                self.assertIn(next_text, rows)
                self.assertEqual(len(seen), 1)
                self.assertEqual(seen[0].plan_source, source)
                if isinstance(record, phase_table.PhaseRecord) and len(record.phases) == 3:
                    self.assertIn(("Later", "Delivery 3 (3)", "Not started"), rows)
                if source != unit_phases.PlanSource.READABLE:
                    self.assertNotIn("Nothing queued", rendered)

    def test_workers_join_the_open_pass_by_id_before_assignment(self) -> None:
        member = self.member("roles")
        roles = (("impl", "Implementation worker runs it"),
                 ("implement", "Implementation worker runs it"),
                 ("test", "Test worker runs it"),
                 ("fix", "Fix worker runs it"),
                 ("review", "Review worker runs it"),
                 ("astra-designer", "Designer runs it"),
                 ("other", "named-worker runs it"))
        for role, actor in roles:
            with self.subTest(role=role):
                _ = self.write_run(member, "roles", [("board", "board: Readable board")], [
                    self.phase_event("pass_started", "board", pass_instance_id="pass", pass_kind=role,
                                     worker_id="named-worker-id")])
                rendered = self.blocks([member], {}, workers=[census_worker("named-worker", member.id)])
                self.assertIn("**roles** — working", rendered)
                self.assertIn(("Now", "Readable board (board)", actor), detail_rows(rendered, "roles"))
                self.assertNotIn("board: Readable", rendered)
        for matched, closed, parent in ((True, False, member.id), (False, False, member.id),
                                       (True, True, member.id), (True, False, staff.StaffId("foreign"))):
            with self.subTest(matched=matched, closed=closed, parent=parent):
                tail = [self.phase_event("pass_started", "board", pass_instance_id="pass", pass_kind="test")]
                if closed:
                    tail.append(self.phase_event("pass_finished", "board", pass_instance_id="pass"))
                _ = self.write_run(member, "roles", [("board", "Readable board")], tail)
                rendered = self.blocks([member], {}, workers=[census_worker("fallback", parent,
                                            phase="board-instance" if matched else "another-instance")])
                if matched and not closed and parent == member.id:
                    self.assertIn(("Now", "Readable board (board)", "Test worker runs it"),
                                  detail_rows(rendered, "roles"))
                else:
                    expected = ("Test worker finished at time not recorded; director's next step not recorded"
                                if closed else "Director's current step not recorded")
                    self.assertIn(("Now", "Readable board (board)", expected),
                                  detail_rows(rendered, "roles"))
                    self.assertNotIn("— working", rendered)

    def test_task_is_the_newest_open_activity_before_the_open_pass(self) -> None:
        member = self.member("activity")
        tail = [self.phase_event("pass_started", "board", pass_instance_id="pass", pass_kind="impl"),
                self.phase_event("activity_started", "board", activity_instance_id="old", activity_text="writes the old report"),
                self.phase_event("activity_finished", "board", activity_instance_id="old"),
                self.phase_event("activity_started", "board", activity_instance_id="new", activity_text="reviews the fixes")]
        _ = self.write_run(member, "activity", [("board", "Readable board")], tail)
        rendered = self.blocks([member], {})
        self.assertIn("**activity** — working", rendered)
        self.assertIn(("Now", "Readable board (board)", "Director reviews the fixes"),
                      detail_rows(rendered, "activity"))
        self.assertNotIn("old report", rendered)
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                actors = [self.phase_event("pass_started", "board", pass_instance_id="fix", pass_kind="fix",
                                           worker_id="fixer-id")]
                workers = [census_worker("fixer", member.id)]
                if duplicate:
                    actors.append(self.phase_event("pass_started", "board", pass_instance_id="other-fix",
                                                   pass_kind="fix", worker_id="other-fixer-id"))
                    workers.append(census_worker("other-fixer", member.id))
                else:
                    actors.append(self.phase_event("activity_started", "board", activity_instance_id="review",
                                                   activity_text="reviews the fixes"))
                _ = self.write_run(member, "activity", [("board", "Readable board")], actors)
                rendered = self.blocks([member], {}, workers=workers)
                current = [row for row in detail_rows(rendered, "activity") if row.work == "Readable board (board)"]
                expected = "Fix worker runs it" + ("" if duplicate else "; director reviews the fixes")
                self.assertEqual(current, [DetailRow("Now", "Readable board (board)", expected)])
                self.assertEqual(rendered.count("Fix worker runs it"), 1)
                self.assertNotIn("| Now |  |", rendered)
        tail.append(self.phase_event("activity_finished", "board", activity_instance_id="new",
                                    timestamp_epoch=datetime(2026, 11, 15, 17, 57, tzinfo=UTC).timestamp()))
        _ = self.write_run(member, "activity", [("board", "Readable board")], tail)
        for status in ("idle", "busy"):
            with self.subTest(status=status):
                rendered = self.blocks([member], {}, statuses=[status])
                expected = "Director activity finished at 17:57; director's next step not recorded"
                self.assertIn(("Now", "Readable board (board)", expected), detail_rows(rendered, "activity"))
                self.assertNotIn("reviews the fixes", rendered)

    def test_work_cells_keep_the_capital_at_the_start_of_a_title(self) -> None:
        member = self.member("inline")
        for title in ("The ten suites pass on the Mac", "Mac suites pass"):
            with self.subTest(title=title):
                _ = self.write_run(member, "inline", [("39", title)])
                busy = self.blocks([member], {}, statuses=["busy"])
                self.assertIn(("Now", title + " (39)",
                               "Director's current step not recorded"),
                              detail_rows(busy, "inline"))
                idle = self.blocks([member], {}, statuses=["idle"])
                self.assertIn(("Now", title + " (39)", "Director's current step not recorded"),
                              detail_rows(idle, "inline"))

    def test_queue_work_is_a_short_title_with_a_capital_only_for_a_plain_word(self) -> None:
        cases = [
            ("startlock: detached_runs.py completion callback must not take .start.lock again (recursive); "
             + "test a cancelled run (sdlc ee7cfccd; after acctload)",
             "detached_runs.py completion callback must not take .start.lock again (startlock)"),
            ("acctcolor: one #rrggbb color per account in statusline.conf account.<label>; every reader accepts it",
             "One #rrggbb color per account in statusline.conf account.<label> (acctcolor)"),
            ("acctchooser: swatch and picker popup on the console Accounts page (Try, Save, Cancel); Save through tune.change",
             "Swatch and picker popup on the console Accounts page (acctchooser)"),
            ("Review delivery", "Review delivery"),
        ]
        for text, work in cases:
            with self.subTest(text=text):
                land = land_queue.LandView({}, land_queue.NoLine(), {}, frozenset(), {})
                item = unit_phases.queued_text(unit_queue.QueuedItem(text), None, land,
                                               now=0, zone=ZoneInfo("America/New_York"))
                self.assertEqual(item, unit_phases.WorkItem(work, "Ready"))

    def test_no_task_is_shown_once_its_step_or_phase_has_closed(self) -> None:
        member = self.member("closed")
        finished = datetime(2026, 11, 15, 17, 57, tzinfo=UTC).timestamp()
        for terminal in ("pass_finished", "phase_finished"):
            with self.subTest(terminal=terminal):
                tail = [self.phase_event("pass_started", "work", pass_instance_id="pass", pass_kind="impl", worker_id="writer-id"),
                        self.phase_event(terminal, "work", pass_instance_id="pass", pass_kind="impl", status="completed",
                                         timestamp_epoch=finished, phase_elapsed_seconds=99)]
                _ = self.write_run(member, "closed", [("work", "Write the board")], tail)
                rendered = self.blocks([member], {}, workers=[census_worker("writer", member.id)])
                self.assertNotIn("Implementation worker runs it", rendered)
                self.assertNotIn("— working", rendered)
                if terminal == "pass_finished":
                    self.assertIn(("Now", "Write the board (work)",
                                   "Implementation worker finished at 17:57; director's next step not recorded"),
                                  detail_rows(rendered, member.name))

    def test_table_shows_census_workers_with_their_runs(self) -> None:
        member = self.member("runs")
        _ = self.write_run(member, "runs", [("test", "Check the board")], [
            self.phase_event("pass_started", "test", pass_instance_id="pass", pass_kind="test", worker_id="tester-id")])
        cases: tuple[tuple[census.RunState, str, str], ...] = (
            ("running", "working", "Test worker runs Board tests"),
            ("finished_queued", "unknown", "Worker finished; director has not read the result"),
            ("missing", "unknown", "Worker activity not recorded"))
        for state, bucket, expected in cases:
            with self.subTest(state=state):
                worker = census_worker("tester", member.id, status="idle",
                    runs=[census.CensusRun("Board tests", 100, state)])
                rendered = self.blocks([member], {}, workers=[worker])
                self.assertIn(f"**runs** — {bucket}", rendered)
                self.assertIn(("Now", "Check the board (test)", expected), detail_rows(rendered, "runs"))

    def test_each_open_phase_has_its_work_eta_wait_and_recorded_workers(self) -> None:
        member = self.member("parallel")
        holder = self.member("builder")
        now = datetime(2026, 11, 15, 18, tzinfo=UTC).timestamp()
        tail = [self.phase_event("pass_started", "10", pass_instance_id="test", pass_kind="test", worker_id="tester-id"),
                self.phase_event("pass_started", "8", pass_instance_id="repair", pass_kind="fix", worker_id="repair-id"),
                self.phase_event("eta_stated", "8", eta_phase_id="8", eta_at=now - 300, timestamp_epoch=now - 600),
                self.phase_event("eta_stated", "10", eta_phase_id="10", eta_at=now + 600, timestamp_epoch=now - 600),
                self.phase_event("wait_started", "8", wait_id="memory", wait_kind="memory", wait_reason="memory admission",
                    pass_instance_id="repair",
                    wait_started_epoch=now - 120, holder={"state": "NamedHolder", "resource_kind": "memory",
                        "resource": "build", "holder": {"state": "UnitHolder", "unit": "old-builder"}}),
                self.phase_event("wait_holder_seen", "8", wait_id="memory", timestamp_epoch=now - 30,
                    holder={"state": "NamedHolder", "resource_kind": "memory", "resource": "build",
                            "holder": {"state": "SessionHolder", "session_id": holder.current_session}})]
        _ = self.write_run(member, "parallel", [("10", "Verify delivery"), ("8", "Repair delivery"),
                                                ("extra", "Read the report")], tail)
        record: dict[str, object] = {"phases": [
            {"phase": "8", "title": "Repair delivery", "status": "running"},
            {"phase": "10", "title": "Verify delivery", "status": "running"},
            {"phase": "12", "title": "Later delivery", "status": "todo"}]}
        with patch.object(plan_history, "read_run_events", wraps=plan_history.read_run_events) as reads:
            rendered = self.blocks([member], {"parallel": record},
                workers=[census_worker("tester", member.id), census_worker("repair", member.id)])
        self.assertEqual(reads.call_count, 1)
        rows = detail_rows(rendered, "parallel")
        self.assertIn(("Also", "Verify delivery (10)", "Test worker runs it; ETA 18:10"), rows)
        self.assertIn(("Also", "Repair delivery (8)", "Needs build memory in use by builder; since 17:58; ETA 17:55, late 5m"), rows)
        self.assertEqual(sum(row.state.count("ETA 17:55, late 5m") for row in rows), 1)
        self.assertEqual(sum(row.state.count("ETA 18:10") for row in rows), 1)
        self.assertEqual(sum(row.state.count("ETA ") for row in rows), 2)
        self.assertIn(("Now", "Read the report (extra)", "Director's current step not recorded"), rows)
        self.assertIn(("Next", "Later delivery (12)", "Not started"), rows)
        self.assertNotIn("old-builder", rendered)
        self.assertNotIn(holder.current_session, rendered)
        self.assertEqual(rendered.splitlines()[0],
            "working 1 · waiting to merge 0 · waiting on other work 0 · idle 0 · 1 units")

    def test_status_comes_from_the_census_session_or_the_scanned_machines(self) -> None:
        owner = fixture_member("showrunner", name="status-owner")
        member = fixture_member("unit", name="listed", parent=owner.id,
            worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        listed = unit("listed", live("listed"))
        mac = listed._replace(machine="mac")
        unrecorded = unit("unrecorded", live("unrecorded"))
        failed = census.CensusFailed("census exited 2: bad config")
        cases: list[tuple[census.Census | census.CensusFailed, live_units.LiveUnit, str]] = [
            (tiles(sessions=[session_of(member, "busy")]), listed, "busy"),
            (tiles(sessions=[session_of(member, "paused")]), mac, "paused"),
            (tiles(sessions=[session_of(member, None)]), listed, "—"),
            (tiles(sessions=[census.CensusSession("listed-id", "busy")]), listed, "exited"),
            (tiles(sessions=[census.CensusSession("unrecorded-id", "idle")]), unrecorded, "idle"),
            (LOCAL, unrecorded._replace(session=None), "exited"),
            (LOCAL, listed, "exited"),
            (LOCAL, listed._replace(machine=""), "exited"),
            (LOCAL, mac, "—"),
            (LOCAL._replace(machines=(census.CensusMachine("natedev", False),)), listed, "—"),
            (failed, listed, "—")]
        for read, selected, expected in cases:
            with self.subTest(read=read, machine=selected.machine):
                view = unit_phases.census_view(read)
                self.assertEqual(unit_phases.census_status(view, selected), expected)

    def test_notes_name_background_commands_and_a_sent_checkpoint(self) -> None:
        member = self.member("shells")
        now = datetime(2026, 11, 15, 18, tzinfo=UTC).timestamp()
        read = tiles(sessions=[census.CensusSession(member.current_session, "busy", (
            census.CensusShell("Run the AWS gate", now - 720), census.CensusShell("Watch CI", None)))])
        rendered = unit_phases.table([unit("shells", live("shells"))], {}, now=now, zone=ZoneInfo("UTC"), census_read=read)
        self.assertIn("**shells** — working", rendered)
        self.assertIn(("Now", "Run the AWS gate", "Runs; the director waits for its result"), detail_rows(rendered, "shells"))
        self.assertIn(("Also", "Watch CI", "Runs; the director waits for its result"), detail_rows(rendered, "shells"))
        for resource, reason, holder, expected in (
                ("memory", "memory admission", {"state": "NamedHolder", "resource_kind": "memory", "resource": "build",
                            "holder": {"state": "UnitHolder", "unit": "builder"}},
                 "Needs build memory in use by builder; since 17:58"),
                ("slot", "build queue", {"state": "NamedHolder", "resource_kind": "slot", "resource": "build",
                          "holder": {"state": "UnitHolder", "unit": "builder"}},
                 "Waits for a build slot used by builder; since 17:58"),
                ("lock", "review lock", {"state": "NamedHolder", "resource_kind": "lock", "resource": "review",
                          "holder": {"state": "UnitHolder", "unit": "builder"}},
                 "Waits for review lock, in use by builder; since 17:58"),
                ("memory", "memory admission", {"state": "HolderNotNamed", "reason": "not recorded"},
                 "Waits for memory admission; holder not recorded; since 17:58")):
            with self.subTest(resource=resource, holder=holder):
                _ = self.write_run(member, "shells", [("work", "Check delivery")], [
                    self.phase_event("wait_started", "work", wait_id="wait", wait_kind=resource,
                                     wait_reason=reason, wait_started_epoch=now - 120, holder=holder)])
                read = tiles(sessions=[census.CensusSession(member.current_session, "idle",
                    (census.CensusShell("Build delivery", now - 120),))])
                rendered = unit_phases.table([unit("shells", live("shells"))], {}, now=now,
                                             zone=ZoneInfo("UTC"), census_read=read)
                self.assertIn("**shells** — waiting on other work", rendered)
                rows = detail_rows(rendered, "shells")
                self.assertEqual(rows.count(DetailRow("Now", "Check delivery (work)", expected)), 1)
                self.assertNotIn("Build delivery", [row.work for row in rows])
                self.assertNotIn("held by", rendered)

        events: list[dict[str, object]] = [
            {"event_type": "phase_started", "phase_id": "images", "phase_instance_id": "images"},
            {"event_type": "activity_started", "phase_instance_id": "images", "activity": "write the card"},
            {"event_type": "checkpoint", "phase_instance_id": "images", "checkpoint_commit": "c" * 40},
            {"event_type": "eta_stated", "phase_instance_id": "images", "eta_phase_id": "images"}]
        failed: land_runs.LandResult = {"commit": "c" * 40, "production": "show", "unit": "shells", "phase": "images",
            "landed": False, "step": "red", "state": "failed", "detail": "test_cards red\nmore",
            "time": "2026-11-15T17:50:00+00:00"}
        landed: land_runs.LandResult = {**failed, "landed": True, "step": "", "state": "", "detail": ""}
        second = {"d" * 40: land_queue.Place(1, "other first", "other first"),
                  "c" * 9: land_queue.Place(2, "show images", "shells images")}
        carrier = {"c" * 40: land_queue.Place(3, "show batch", "builder carrier")}
        views: list[tuple[land_queue.LandView, str]] = [
            (land_queue.LandView({}, land_queue.NoLine(), {}, frozenset(), {}), "Merge position not recorded"),
            (land_queue.LandView({"c" * 9: "shells images"}, {}, {}, frozenset(), {}), "sdlc merges it now"),
            (land_queue.LandView({"d" * 40: "other first"}, {}, {}, frozenset(), {}),
             "Merge submission not in the merge line; reason not recorded"),
            (land_queue.LandView({}, second, {}, frozenset(), {}), "2nd in sdlc's merge line"),
            (land_queue.LandView({}, second, {"c" * 40: failed}, frozenset(), {}),
             "2nd in sdlc's merge line"),
            (land_queue.LandView({}, second, {"c" * 40: landed}, frozenset(), {}), "2nd in sdlc's merge line"),
            (land_queue.LandView({}, {}, {"c" * 12: failed}, frozenset(), {}),
             "sdlc cannot merge it: test_cards red"),
            (land_queue.LandView({}, {}, {"c" * 40: landed}, frozenset(), {}), "sdlc merged it"),
            (land_queue.LandView({"c" * 40: "shells images"}, second, {"c" * 40: failed}, frozenset(), {}), "sdlc merges it now"),
            (land_queue.LandView({}, {}, {}, frozenset(), carrier), "3rd in sdlc's merge line"),
            (land_queue.LandView({}, {}, {"c" * 40: failed}, frozenset(), carrier),
             "3rd in sdlc's merge line"),
            (land_queue.LandView({}, {}, {"c" * 40: landed}, frozenset(), carrier), "sdlc merged it"),
            (land_queue.LandView({}, second, {}, frozenset(), carrier), "2nd in sdlc's merge line"),
            (land_queue.LandView({}, {}, {}, frozenset({"c" * 40}), {}), "sdlc merges it now"),
        ]
        for view, expected in views:
            with self.subTest(merge=expected):
                result = unit_phases.unit_lands("shells", view, open_sent={"images": unit_phases.phase_sent_work(events)},
                    now=now, zone=ZoneInfo("UTC"), on_main=lambda _commit: False, production="sdlc")
                matching = [entry for entry in result.entries
                            if entry.work == "Images" and expected.casefold() in entry.state_text.casefold()]
                self.assertTrue(matching, result.entries)
                for entry in matching:
                    self.assertEqual(entry.state_text, expected)
                    self.assertFalse(entry.state_text.endswith("."), entry)
        for kind in ("phase_started", "pass_started", "activity_started"):
            with self.subTest(restarted=kind):
                self.assertIsInstance(unit_phases.phase_sent_work([*events, {"event_type": kind}]), unit_phases.NoSentWork)
        self.assertIsInstance(unit_phases.phase_sent_work(events[:2]), unit_phases.NoSentWork)

    def test_eta_uses_the_latest_promise_and_local_calendar_time(self) -> None:
        member = self.member("eta")
        now = datetime(2026, 11, 15, 18, tzinfo=UTC).timestamp()
        cases = (("UTC", now + 600, "18:10", "single"),
                 ("America/New_York", now + 600, "13:10", "single"),
                 ("UTC", now + 86400, "2026-11-16 18:00", "single"),
                 ("UTC", now + 600, "18:10", "running"),
                 ("UTC", now - 300, "17:55, late 5m", "running"),
                 ("UTC", now + 600, "18:10", "waiting"))
        for zone, target, expected, tasks in cases:
            with self.subTest(zone=zone, target=target, tasks=tasks):
                tail = [self.phase_event("eta_stated", "work", eta_phase_id="work", eta_at=now - 1000, timestamp_epoch=110),
                        self.phase_event("eta_stated", "work", eta_phase_id="work", eta_at=target, timestamp_epoch=120)]
                workers: tuple[census.CensusWorker, ...] = ()
                if tasks != "single":
                    tail.extend([
                        self.phase_event("pass_started", "work", pass_instance_id="writer", pass_kind="impl", worker_id="writer-id"),
                        self.phase_event("pass_started", "work", pass_instance_id="reviewer", pass_kind="review", worker_id="reviewer-id"),
                        self.phase_event("activity_started", "work", activity_instance_id="report", activity_text="checks the report")])
                    workers = (census_worker("writer", member.id), census_worker("reviewer", member.id))
                if tasks == "waiting":
                    tail.append(self.phase_event("wait_started", "work", pass_instance_id="writer", wait_id="memory",
                        wait_kind="memory", wait_reason="memory admission", wait_started_epoch=now - 120))
                _ = self.write_run(member, "eta", [("work", "Write delivery")], tail)
                rendered = unit_phases.table([unit("eta", live("eta"))], {}, now=now, zone=ZoneInfo(zone),
                    census_read=tiles(sessions=[session_of(member, "idle")], workers=workers))
                task_lines = [row.state for row in detail_rows(rendered, "eta") if row.work == "Write delivery (work)"]
                self.assertEqual(len(task_lines), 1)
                self.assertIn("ETA " + expected, task_lines[0])
                self.assertEqual(sum(line.count("ETA ") for line in task_lines), 1)
                for line in task_lines[1:]:
                    self.assertNotIn("ETA ", line)
                if tasks == "waiting":
                    self.assertIn("Review worker runs it", task_lines[0])
                    self.assertNotIn("Also", [row.item for row in detail_rows(rendered, "eta")])
                self.assertNotIn(". late", rendered)
                if target >= now:
                    self.assertNotIn("late", rendered)
                self.assertNotIn("no ETA", rendered)

    def test_non_phase_work_and_queued_items_share_the_units_block(self) -> None:
        member = self.member("selection")
        other = self.member("other")
        plan = self.root / "plan.md"
        _ = plan.write_text("# Plan\n### Phase 1 — Planned work · status: todo\n")
        selected_path = self.write_run(member, "selected", [("ad-hoc", "Other work")], plan=plan.name)
        _ = self.write_run(member, "side-review", [("ad-hoc", "Side review")], started=300)
        _ = self.write_run(other, "foreign", [("ad-hoc", "Foreign task")], started=400)
        _ = staff.set_run(member.id, staff.OpenRun(self.root / "runs/selected"))
        _ = self.queue(member, unit_queue.QueuedItem("Review delivery"), unit_queue.QueuedItem("Clean records"))
        with patch.object(plan_history, "read_run_events", wraps=plan_history.read_run_events) as reads:
            rendered = self.blocks([member], {})
        self.assertIn(("Now", "Other work (ad-hoc)", "Director's current step not recorded"),
                      detail_rows(rendered, "selection"))
        self.assertNotIn("Side review", rendered)
        self.assertNotIn("Foreign task", rendered)
        self.assertIn(("Next", "Review delivery", "Ready"), detail_rows(rendered, "selection"))
        self.assertIn(("Later 1", "Clean records", "Ready"), detail_rows(rendered, "selection"))
        self.assertIn(("Later 2", "Planned work (1)", "Not started"), detail_rows(rendered, "selection"))
        self.assertEqual([call.args[0] for call in reads.call_args_list], [selected_path])
        for closed, reopened in ((True, False), (True, True), (False, False)):
            with self.subTest(closed=closed, reopened=reopened):
                tail: list[dict[str, object]] = [{"event_type": "run_finished"}] if closed else []
                if reopened:
                    tail.append({"event_type": "reopened"})
                _ = self.write_run(member, "selected", [("ad-hoc", "Other work")], tail, plan=plan.name)
                rendered = self.blocks([member], {})
                self.assertEqual("Other work (ad-hoc)" in [row.work for row in detail_rows(rendered, "selection")],
                                 not closed or reopened)
                self.assertIn("Review delivery", rendered)

        finished = datetime(2026, 11, 15, 17, 49, tzinfo=UTC).timestamp()
        _ = self.write_run(member, "selected", [("review", "Read the merge result")], [
            self.phase_event("activity_started", "review", activity_instance_id="merge-step",
                             activity_text="acctland: merge origin/main"),
            self.phase_event("activity_finished", "review", activity_instance_id="merge-step",
                             timestamp_epoch=finished),
        ])
        _ = self.queue(member, unit_queue.QueuedItem("acctland: Merge the Accounts page"),
                       unit_queue.QueuedItem("acctcolor: Add account colors"))
        rendered = self.blocks([member], {})
        rows = detail_rows(rendered, member.name)
        self.assertIn(("Next", "Not recorded", "The queue still lists acctland, which finished at 17:49; "
                       + "the director must clear it"), rows)
        self.assertIn(("Later", "Add account colors (acctcolor)", "Ready"), rows)
        self.assertNotIn("has not started", rendered)

    def test_queued_combines_outside_work_with_only_waiting_plan_phases(self) -> None:
        member = self.member("next")
        plan = self.root / "plan.md"
        waiting = "the workers-back notice, then at least twenty eligible phases " + "with full details " * 12
        _ = plan.write_text("# Plan\n### Phase 1 — Current work · status: todo\n"
            + "### Phase 2 — Later work · status: todo\n**Depends on:** 1\n"
            + "### Phase 3 — Last work · status: todo\n**Waits for:** " + waiting + "\n")
        _ = self.write_run(member, "next", [("1", "Current work")], plan=plan.name)
        base = self.repository()
        dependency = self.member("dependency")
        sent = commit_repository(self.root, "Dependency", allow_empty=True)
        _ = self.git("branch", "pending-branch", sent)
        _ = self.write_run(dependency, "dependency", [("open", "Open work"), ("sent", "Submitted work")], [
            self.phase_event("checkpoint", "sent", checkpoint_commit=sent)])
        with land_queue.edited() as items:
            items.clear()
        _ = self.queue(member, unit_queue.QueuedItem("Review next", "next/future"),
            unit_queue.QueuedItem("Clean records"), unit_queue.QueuedItem("Ready work", base),
            unit_queue.QueuedItem("After submission", "dependency/sent"),
            unit_queue.QueuedItem("After open work", "dependency/open"),
            unit_queue.QueuedItem("After unknown work", "missing-ref"),
            unit_queue.QueuedItem("After branch", "pending-branch"))
        with (patch.object(io, "open", wraps=io.open) as opened,
              patch.object(unit_queue, "read_items", wraps=unit_queue.read_items) as queues,
              patch.object(waits_on, "state", wraps=waits_on.state) as dependencies):
            rendered = self.blocks([member], {})
        rows = detail_rows(rendered, "next")
        self.assertIn(("Next", "Review next", "Waits for future to merge"), rows)
        self.assertIn(("Later 1", "Clean records", "Ready"), rows)
        self.assertIn(("Later 2", "Ready work", "Ready"), rows)
        self.assertIn(("Later 3", "After submission", "Waits for dependency to merge: Submitted work"), rows)
        self.assertIn(("Later 4", "After open work", "Waits for dependency to merge: Open work"), rows)
        self.assertIn(("Later 5", "After unknown work", "Waits for missing-ref to merge"), rows)
        self.assertIn(("Later 6", "After branch", "Waits for pending-branch to merge"), rows)
        self.assertIn(("Later 7", "Later work (2)", "Waits for phase 1"), rows)
        self.assertIn(("Later 8", "Last work (3)", "Waits for " + waiting.strip()), rows)
        self.assertNotIn("Then 3", rendered)
        self.assertEqual([row.work for row in rows if row.item.startswith("Later")][-2:],
                         ["Later work (2)", "Last work (3)"])
        self.assertEqual(queues.call_count, 1)
        self.assertEqual(dependencies.call_count, 6)
        self.assertEqual(sum(call.args[0] == plan for call in opened.call_args_list), 1)

        for submitted, blocked_first, bucket in (
                (False, False, "unknown"), (False, True, "waiting on other work"),
                (True, False, "waiting to merge"), (True, True, "waiting to merge")):
            with self.subTest(submitted=submitted, blocked_first=blocked_first):
                tail = [self.phase_event("checkpoint", "1", checkpoint_commit=base)] if submitted else []
                _ = self.write_run(member, "next", [("1", "Current work")], tail, plan=plan.name)
                ready = unit_queue.QueuedItem("Clean records")
                blocked = unit_queue.QueuedItem("Review next", "next/future")
                pending = (blocked, ready) if blocked_first else (ready, blocked)
                _ = self.queue(member,
                    unit_queue.QueuedItem("1: Continue current work", "started-only/future"), *pending,
                    unit_queue.QueuedItem("Review 1: preserve this text"),
                    unit_queue.QueuedItem("10: Preserve this different id"))
                with (patch.object(unit_queue, "read_items", wraps=unit_queue.read_items) as queues,
                      patch.object(waits_on, "state", wraps=waits_on.state) as dependencies):
                    rendered = self.blocks([member], {})
                self.assertNotIn("Continue current work", rendered)
                self.assertNotIn("started-only/future", rendered)
                rows = detail_rows(rendered, "next")
                queued = [row for row in rows if row.work not in ("Later work (2)", "Last work (3)")
                          and row.item.split()[0] in ("Next", "Later")]
                self.assertEqual(queued, [
                    ("Next", "Review next", "Waits for future to merge"), ("Later 1", "Clean records", "Ready"),
                    ("Later 2", "Preserve this text (Review 1)", "Ready"),
                    ("Later 3", "Preserve this different id (10)", "Ready")] if blocked_first else [
                    ("Next", "Clean records", "Ready"), ("Later 1", "Review next", "Waits for future to merge"),
                    ("Later 2", "Preserve this text (Review 1)", "Ready"),
                    ("Later 3", "Preserve this different id (10)", "Ready")])
                self.assertIn("**next** — " + bucket, rendered)
                if submitted:
                    self.assertIn("Current work (1)", [row.work for row in rows if row.item == "Now"])
                self.assertIn("Later work (2)", [row.work for row in rows if row.item.startswith("Later")])
                self.assertEqual(queues.call_count, 1)
                self.assertEqual([call.args[0] for call in dependencies.call_args_list], ["next/future"])

        _ = self.write_run(member, "next", [("1", "Current work"), ("2", "Later work"), ("3", "Last work")],
                           plan=plan.name)
        _ = self.queue(member, unit_queue.QueuedItem("1: Continue current work"))
        rendered = self.blocks([member], {})
        self.assertIn(("Next", "Not recorded", "The director has not chosen the next task"), detail_rows(rendered, "next"))
        self.assertNotIn("Continue current work", rendered)

        dependency_path = self.write_run(dependency, "dependency", [("22", "Submitted delivery")], [
            self.phase_event("checkpoint", "22", checkpoint_commit=sent),
            {"event_type": "wait_started", "wait_scope": "unit", "target_work": "22", "wait_id": "hold",
             "wait_by": "sdlc", "wait_reason": "the user approves board-reasons", "wait_kind": "WaitingForUser",
             "wait_started_epoch": datetime(2026, 11, 15, 17, 50, tzinfo=UTC).timestamp(),
             "wait_attribution": {"state": "UnitTarget"}}])
        member_path = self.write_run(member, "next", [])
        _ = self.queue(member, unit_queue.QueuedItem("Merge delivery", "dependency/22"),
                       unit_queue.QueuedItem("Read delivery", "dependency/22"))
        with (patch.object(plan_history, "read_run_events", wraps=plan_history.read_run_events) as reads,
              patch.object(waits_on, "state", wraps=waits_on.state) as dependencies):
            rendered = self.blocks([dependency, member], {})
        self.assertIn("**next** — waiting on other work", rendered)
        approval = "Sdlc keeps it waiting until the user approves board-reasons; since 17:50"
        self.assertIn(("Next", "Merge delivery", approval), detail_rows(rendered, "next"))
        self.assertIn(("Later", "Read delivery", approval), detail_rows(rendered, "next"))
        self.assertCountEqual([call.args[0] for call in reads.call_args_list], [dependency_path, member_path])
        self.assertEqual(dependencies.call_count, 1)

        handler = self.member("handler")
        _ = self.write_run(handler, "handler", [("23", "Check the handler")])
        for reference, expected in (("next/41", "Waits for phase 41 to merge"),
                                    ("next/acctcolor", "Waits for acctcolor to merge"),
                                    ("handler/23", "Waits for handler to merge: Check the handler"),
                                    ("handler/24", "Waits for handler phase 24 to merge")):
            with self.subTest(reference=reference):
                _ = self.queue(member, unit_queue.QueuedItem("Read delivery", reference))
                rendered = self.blocks([member], {})
                self.assertIn(("Next", "Read delivery", expected), detail_rows(rendered, "next"))

    def test_plan_waits_control_the_unit_with_no_current_task(self) -> None:
        member = self.member("waiting")
        plan = self.root / "plan.md"
        for metadata, explanation in (("**Waits for:** the workers-back notice", "Waits for the workers-back notice"),
                                      ("**Waits for:** twenty eligible phases (the workers-back notice is met: workers are present)",
                                       "Waits for twenty eligible phases"),
                                      ("**Waits for:** twenty eligible phases (from two units)",
                                       "Waits for twenty eligible phases (from two units)"),
                                      ("**Depends on:** 1", "Waits for phase 1"),
                                      ("**Depends on:** 41", "Waits for phase 41"),
                                      ("**Depends on:** acctcolor", "Waits for acctcolor")):
            with self.subTest(metadata=metadata):
                _ = plan.write_text("# Plan\n### Phase 1 — First work · status: todo\n"
                    + "### Phase 2 — Next work · status: todo\n" + metadata + "\n")
                _ = self.write_run(member, "waiting", [], plan=plan.name)
                record: dict[str, object] = {"plan": str(plan), "phases": [
                    {"phase": "1", "title": "First work", "status": "running"},
                    {"phase": "2", "title": "Next work", "status": "todo"}]}
                details: list[plan_history.PlanPhase] = [
                    {"id": "1", "title": "First work", "done": False},
                    {"id": "2", "title": "Next work", "done": False, "depends_on": ("acctcolor",)}]
                with (patch.object(plan_history, "plan_phases", return_value=details)
                      if metadata == "**Depends on:** acctcolor" else contextlib.nullcontext()):
                    rendered = self.blocks([member], {"waiting": record})
                self.assertIn("**waiting** — waiting on other work", rendered)
                now, = [row for row in detail_rows(rendered, "waiting") if row[0] == "Now"]
                self.assertEqual(now.work, "Not recorded")
                self.assertEqual(now.state, explanation)
                self.assertIn(("Next", "Next work (2)", explanation), detail_rows(rendered, "waiting"))

    def test_unit_waits_name_the_reason_at_each_target_and_set_the_bucket(self) -> None:
        member = self.member("reasons")
        base = self.repository()
        sent = commit_repository(self.root, "Delivery", allow_empty=True)
        plan = self.root / "plan.md"
        _ = plan.write_text("# Plan\n### Phase 30 — Later delivery · status: todo\n"
                            + "### Phase 31 — Last delivery · status: todo\n")
        started = datetime(2026, 11, 15, 17, 50, tzinfo=UTC).timestamp()
        hold: dict[str, object] = {"event_type": "wait_started", "wait_scope": "unit", "wait_id": "hold",
            "wait_by": "the user", "wait_reason": "the user approves the release", "wait_kind": "WaitingForUser",
            "wait_started_epoch": started, "wait_attribution": {"state": "UnitTarget"}}
        cases = (
            ("sent", "delivery", "Now", "waiting to merge",
             "The user keeps it waiting until the user approves the release; since 17:50"),
            ("done", "delivery", "Now", "waiting to merge",
             "The user keeps it waiting until the user approves the release; since 17:50"),
            ("queue", "acctcolor", "Next", "waiting on other work",
             "The user keeps it waiting until the user approves the release; since 17:50"),
            ("queue later", "acctcolor", "Later", "unknown",
             "The user keeps it waiting until the user approves the release; since 17:50"),
            ("queue text", "acctcolor", "Now", "waiting on other work",
             "The user keeps it waiting until the user approves the release; since 17:50"),
            ("plan", "30", "Next", "waiting on other work",
             "The user keeps it waiting until the user approves the release; since 17:50"),
            ("free", "The release", "Now", "waiting on other work",
             "The user keeps it waiting until the user approves the release; since 17:50"),
            ("working", "The release", "Also", "working",
             "The user keeps it waiting until the user approves the release; since 17:50"))
        for target_state, target, label, bucket, expected in cases:
            with self.subTest(target_state=target_state):
                phases: list[tuple[str, str]] = []
                tail: list[dict[str, object]] = []
                if target_state in ("sent", "done"):
                    phases.append(("delivery", "Completed delivery"))
                    tail.append(self.phase_event("checkpoint", "delivery", checkpoint_commit=sent)
                                if target_state == "sent" else self.phase_event("phase_finished", "delivery",
                                    status="completed", timestamp_epoch=started - 60, phase_elapsed_seconds=60))
                if target_state == "working":
                    phases.append(("work", "Current delivery"))
                    tail.append(self.phase_event("pass_started", "work", pass_instance_id="writer-pass",
                                                 pass_kind="impl", worker_id="writer-id"))
                tail.append({**hold, "target_work": target})
                path = self.write_run(member, "reasons", phases, tail, plan=plan.name if target_state == "plan" else "")
                queued_items: list[unit_queue.QueuedItem] = []
                if target_state == "queue later":
                    queued_items.append(unit_queue.QueuedItem("Ready delivery"))
                if target_state in ("queue", "queue later"):
                    queued_items.extend((unit_queue.QueuedItem("acctcolor: Add account colors"),
                                  unit_queue.QueuedItem("Read the report")))
                if target_state == "queue text":
                    queued_items.append(unit_queue.QueuedItem("Review acctcolor: Add account colors"))
                _ = self.queue(member, *queued_items)
                workers = [census_worker("writer", member.id)] if target_state == "working" else []
                with (patch.object(plan_history, "read_run_events", wraps=plan_history.read_run_events) as reads,
                      patch.object(unit_queue, "read_items", wraps=unit_queue.read_items) as queues):
                    rendered = self.blocks([member], {}, workers=workers)
                self.assertIn(f"**reasons** — {bucket}", rendered)
                rows = detail_rows(rendered, "reasons")
                selected = [row for row in rows if row.item.startswith(label)]
                self.assertEqual(sum(expected in row.state for row in selected), 1)
                self.assertTrue(all(not row.state.endswith(".") for row in rows), rows)
                self.assertEqual([call.args[0] for call in reads.call_args_list], [path])
                self.assertEqual(queues.call_count, 1)
                if target_state in ("queue", "plan"):
                    now_row, = [row for row in rows if row[0] == "Now"]
                    self.assertEqual(now_row[1], "Not recorded")
                    self.assertFalse(now_row.state.startswith("Nothing runs"), now_row)
                    wait_target = "it" if target_state == "plan" else target
                    self.assertIn(f"the user keeps {wait_target} waiting until the user approves the release", now_row[2].casefold())
                    self.assertNotIn("has not started; reason not recorded", rendered)
                if target_state in ("queue", "queue later"):
                    queue_row, = [row for row in rows if row.work == "Add account colors (acctcolor)"]
                    self.assertEqual(queue_row[2], expected)
                    self.assertIn("Read the report", [row.work for row in rows if row.item.split()[0] in ("Next", "Later")])
                    self.assertIn("Add account colors (acctcolor)", rendered)
                if target_state == "queue later":
                    self.assertIn(("Now", "Not recorded", "Director's current step not recorded"), rows)
                if target_state == "queue text":
                    self.assertIn(("Next", "Add account colors (Review acctcolor)", "Ready"), rows)
                    self.assertIn(("Now", "acctcolor", expected), rows)
                if target_state == "plan":
                    plan_row, = [row for row in rows if row.item == "Next"]
                    self.assertEqual(plan_row[1], "Later delivery (30)")
                    self.assertIn(("Later", "Last delivery (31)", "Not started"), rows)
                if target_state == "working":
                    self.assertIn(("Now", "Current delivery (work)", "Implementation worker runs it"), rows)
                if target_state in ("free", "working"):
                    self.assertIn((label, target, expected), rows)
                if target_state in ("sent", "done"):
                    self.assertIn(("Now", "Completed delivery (delivery)", expected), rows)
                    self.assertNotIn("absent from the merge line; reason not recorded", rendered)

        for merge_state, expected in (
                ("in line", "1st in sdlc's merge line"),
                ("running", "sdlc merges it now"),
                ("failed", "sdlc cannot merge it: release test failed"),
                ("merged", ""), ("on main", "")):
            with self.subTest(merge_state=merge_state):
                with land_queue.edited() as items:
                    items.clear()
                land_runs.results_path().unlink(missing_ok=True)
                commit = base if merge_state == "on main" else sent
                _ = self.write_run(member, "reasons", [("delivery", "Completed delivery")], [
                    self.phase_event("checkpoint", "delivery", checkpoint_commit=commit),
                    {**hold, "target_work": "delivery"}])
                _ = self.queue(member)
                if merge_state == "in line":
                    land_queue.add("reasons delivery", batch="", commits=[commit], first=False, before=None)
                if merge_state in ("failed", "merged"):
                    land_runs.record_result(commit=commit, production="sdlc", unit="reasons", phase="delivery",
                        landed=merge_state == "merged", step="tests", state="failed", detail="release test failed")
                with land_runs.landing([commit] if merge_state == "running" else [], ["reasons delivery"]):
                    rendered = self.blocks([member], {})
                self.assertNotIn("the user approves the release", rendered)
                self.assertNotIn("keeps it from the merge", rendered)
                if expected:
                    self.assertTrue(any(row.work == "Completed delivery (delivery)" and expected in row.state
                                        for row in detail_rows(rendered, "reasons")))

    def test_merge_groups_show_each_title_once_in_current_rows(self) -> None:
        member = self.member("merge")
        base = self.repository()
        first = commit_repository(self.root, "First", allow_empty=True)
        second = commit_repository(self.root, "Second", allow_empty=True)
        carrier = commit_repository(self.root, "Carrier", allow_empty=True)
        phases = [("a", "First work"), ("b", "Second work")]
        tail = [self.phase_event("checkpoint", phase, checkpoint_commit=commit)
                for phase, commit in (("a", first), ("b", second))]
        _ = self.write_run(member, "merge", phases, tail)
        batch = self.root / "batch"
        _ = batch.write_text(f"merge a {first}\nmerge b {second}\nmerge c {carrier}\n")
        for name in ("carried line", "direct line", "carried running", "direct running", "single", "absent", "unused"):
            with self.subTest(merge=name):
                with land_queue.edited() as items:
                    items.clear()
                if name in ("carried line", "direct line"):
                    land_queue.add("other first", batch="", commits=[base], first=False, before=None)
                    land_queue.add("merge c", batch=str(batch) if name == "direct line" else "",
                        commits=[] if name == "direct line" else [carrier], first=False, before=None)
                elif name == "single":
                    land_queue.add("merge a", batch="", commits=[first], first=False, before=None)
                elif name == "unused":
                    land_queue.line_path().unlink(missing_ok=True)
                running = [carrier] if name == "carried running" else [first, second] if name == "direct running" else []
                owners = ["merge c"] if name == "carried running" else ["merge a", "merge b"] if name == "direct running" else []
                with (land_runs.landing(running, owners),
                      patch.object(land_queue, "land_view", wraps=land_queue.land_view) as merges,
                      patch.object(unit_queue, "read_items", wraps=unit_queue.read_items) as queues):
                    rendered = self.blocks([member], {})
                self.assertIn("**merge** — waiting to merge", rendered)
                rows = detail_rows(rendered, "merge")
                self.assertNotIn("Merge", [row.item for row in rows])
                self.assertEqual(sum(row.work == "First work (a)" for row in rows), 1)
                self.assertEqual(sum(row.work == "Second work (b)" for row in rows), 1)
                if name in ("carried line", "direct line"):
                    self.assertIn(("Now", "First work (a)", "2nd in sdlc's merge line"), rows)
                    self.assertTrue(any(row.work == "Second work (b)" and row.state == "Merges with a"
                                        for row in rows))
                elif name == "single":
                    self.assertIn(("Now", "First work (a)", "1st in sdlc's merge line"), rows)
                    self.assertIn(("Also", "Second work (b)", "Merge submission not in the merge line; reason not recorded"), rows)
                elif name == "absent":
                    for work in ("First work (a)", "Second work (b)"):
                        self.assertIn((work, "Merge submission not in the merge line; reason not recorded"),
                                      [(row.work, row.state) for row in rows if row.item in ("Now", "Also")])
                elif name == "unused":
                    self.assertIn(("Now", "First work (a)", "Merge position not recorded"), rows)
                else:
                    self.assertEqual(sum("sdlc merges it now" == row.state for row in rows), 1)
                    self.assertTrue(any(row.work == "Second work (b)" and row.state == "Merges with a"
                                        for row in rows))
                self.assertEqual(merges.call_count, 1)
                self.assertEqual(queues.call_count, 1)

        commits = {"40": "4" * 40, "40-shrink": "5" * 40, "40-repair": "6" * 40}
        failed: land_runs.LandResult = {"commit": "7" * 40, "production": "sdlc", "unit": "merge",
            "phase": "40-shrink", "landed": False, "step": "tests", "state": "failed",
            "detail": "older shrink attempt failed", "time": "2026-11-15T17:00:00+00:00"}
        view = land_queue.LandView({}, {commits[phase]: land_queue.Place(2, "sdlc batch", f"merge {phase}")
                                       for phase in ("40-shrink", "40-repair", "40")},
                                  {failed["commit"]: failed}, frozenset(), {})
        phases = [(phase, "Completed delivery" if phase == "40" else phase) for phase in commits]
        tail = [event for phase, commit in commits.items() for event in (
            self.phase_event("checkpoint", phase, checkpoint_commit=commit),
            self.phase_event("phase_finished", phase, status="completed"))]
        _ = self.write_run(member, "merge", phases, tail)
        with patch.object(land_queue, "land_view", return_value=view):
            rendered = self.blocks([member], {})
        self.assertIn(("Now", "Completed delivery (40)", "2nd in sdlc's merge line"), detail_rows(rendered, "merge"))
        self.assertEqual(sum(row.item in ("Now", "Also") for row in detail_rows(rendered, "merge")), 1)
        self.assertNotIn("40-shrink", rendered)
        self.assertNotIn("40-repair", rendered)
        self.assertNotIn("older shrink attempt failed", rendered)

        for open_phase in (False, True):
            with self.subTest(open_phase=open_phase):
                phases = [("sent", "Sent delivery"), *([("open", "Current delivery")] if open_phase else [])]
                _ = self.write_run(member, "merge", phases, [
                    self.phase_event("checkpoint", "sent", checkpoint_commit="8" * 40),
                    self.phase_event("phase_finished", "sent", status="completed")])
                view = land_queue.LandView({}, {"8" * 40: land_queue.Place(4, "sdlc batch", "merge sent")},
                                          {}, frozenset(), {})
                with patch.object(land_queue, "land_view", return_value=view):
                    rendered = self.blocks([member], {})
                rows = detail_rows(rendered, "merge")
                self.assertIn(("Also" if open_phase else "Now", "Sent delivery (sent)",
                               "4th in sdlc's merge line"), rows)
                if open_phase:
                    self.assertIn(("Now", "Current delivery (open)", "Director's current step not recorded"), rows)

    def test_stopped_attempts_and_later_results_share_the_progress_history(self) -> None:
        member = self.member("admin-console")
        stopped = [
            ("unknown-reservation-repair", "Repair accepts a reservation the board no longer knows"),
            ("review-trial-ledgers", "review-trial keeps each phase's findings"),
            ("title-in-bar", "The console top bar is the one page title"),
            ("gate-time-limit", "A gate stops when it runs past its normal time"),
            ("image-pairs", "Before and after images as one pair on the Images page"),
        ]
        answer = ("answer-copy", "A code block in a change card's question or answer has a copy button")
        finished = datetime(2026, 11, 14, 15, 56, tzinfo=UTC).timestamp()
        tail = [self.phase_event("phase_finished", task[0], status="stopped", timestamp_epoch=finished - 86400,
                                 phase_elapsed_seconds=60) for task in stopped]
        tail.extend([
            {"event_type": "phase_started", "phase_id": "gate-time-limit", "phase_title": stopped[3][1],
             "phase_instance_id": "gate-later", "phase_started_at": finished - 60},
            {"event_type": "phase_finished", "phase_id": "gate-time-limit", "phase_instance_id": "gate-later",
             "status": "completed", "timestamp_epoch": finished, "phase_elapsed_seconds": 60},
            {"event_type": "phase_started", "phase_id": "image-pairs", "phase_instance_id": "images-later",
             "phase_title": "image-pairs wait closed (the phases unit owns image-pairs; no work here)",
             "phase_started_at": finished + 60},
            {"event_type": "phase_finished", "phase_id": "image-pairs", "phase_instance_id": "images-later",
             "status": "completed", "timestamp_epoch": finished + 120, "phase_elapsed_seconds": 60},
            {"event_type": "phase_started", "phase_id": "different-id", "phase_instance_id": "unrelated",
             "phase_title": stopped[0][1], "phase_started_at": finished + 180},
            {"event_type": "phase_finished", "phase_id": "different-id", "phase_instance_id": "unrelated",
             "status": "completed", "timestamp_epoch": finished + 240, "phase_elapsed_seconds": 60},
            {"event_type": "phase_started", "phase_id": "title-in-bar", "phase_instance_id": "other-plan",
             "phase_title": stopped[2][1], "plan_doc": "other-plan.md", "phase_started_at": finished + 250},
            {"event_type": "phase_finished", "phase_id": "title-in-bar", "phase_instance_id": "other-plan",
             "plan_doc": "other-plan.md", "status": "completed", "timestamp_epoch": finished + 260,
             "phase_elapsed_seconds": 10},
            self.phase_event("checkpoint", answer[0], checkpoint_commit="c" * 40),
        ])
        path = self.write_run(member, "admin-console", [*stopped, answer], tail, plan="missing-plan.md")
        failed: land_runs.LandResult = {
            "commit": "c" * 40, "production": "sdlc", "unit": member.name, "phase": answer[0],
            "landed": False, "step": "tests", "state": "failed",
            "detail": "chart tests also fail on main; log " + str(self.root / "failed.log"),
            "time": "2026-11-15T17:58:00+00:00",
        }
        merge = land_queue.LandView({}, {}, {"c" * 40: failed}, frozenset(), {})
        with (patch.object(land_queue, "land_view", return_value=merge),
              patch.object(plan_history, "read_run_events", wraps=plan_history.read_run_events) as reads,
              patch.object(unit_queue, "read_items", wraps=unit_queue.read_items) as queues):
            rendered = self.blocks([member], {})
        rows = detail_rows(rendered, member.name)
        self.assertEqual(rows, [
            DetailRow("Now", answer[1] + " (answer-copy)", "sdlc cannot merge it: chart tests also fail on main"),
            DetailRow("Next", "Not recorded", "The director has not chosen the next task"),
            *(DetailRow("Stopped", f"{title} ({phase})", "Stopped; reason not recorded")
              for phase, title in stopped[:3]),
            DetailRow("Done", stopped[3][1] + " (gate-time-limit)", "A later attempt finished on 14 Nov"),
            DetailRow("Done", stopped[4][1] + " (image-pairs)", "The phases unit finished this work"),
        ])
        self.assertIn("🟨", rendered.split("**admin-console**", 1)[0])
        self.assertIn("⬜", rendered.split("**admin-console**", 1)[0])
        self.assertEqual([call.args[0] for call in reads.call_args_list], [path])
        self.assertEqual(queues.call_count, 1)
        for text in ("Nothing queued", "Nothing runs", "Then ", "| Merge |"):
            self.assertNotIn(text, rendered)
        self.assertNotIn(str(self.root / "failed.log"), rendered)

        tail[0]["reason"] = "The user stopped this attempt"
        _ = self.write_run(member, "admin-console", [*stopped, answer], tail, plan="missing-plan.md")
        with path.open("a") as stream:
            for event in (
                {"event_type": "phase_started", "phase_id": "gate-time-limit", "phase_instance_id": "gate-new",
                 "phase_title": stopped[3][1], "phase_started_at": finished + 300},
                {"event_type": "pass_started", "phase_id": "gate-time-limit", "phase_instance_id": "gate-new",
                 "pass_instance_id": "gate-writer", "pass_kind": "impl", "worker_id": "writer-id"},
            ):
                _ = stream.write(json.dumps(event) + "\n")
        with patch.object(land_queue, "land_view", return_value=merge):
            rendered = self.blocks([member], {}, workers=[census_worker("writer", member.id)])
        rows = detail_rows(rendered, member.name)
        self.assertIn(("Now", stopped[3][1] + " (gate-time-limit)", "Implementation worker runs it"), rows)
        self.assertIn(("Stopped", stopped[0][1] + " (unknown-reservation-repair)",
                       "Stopped; The user stopped this attempt"), rows)
        self.assertTrue(any(row.item.startswith("Also") and row.work == answer[1] + " (answer-copy)"
                            and row.state == "sdlc cannot merge it: chart tests also fail on main" for row in rows))
        self.assertNotIn("Stopped", [row.item for row in rows if row.work == stopped[3][1] + " (gate-time-limit)"])

    def test_summary_counts_units_once_by_the_first_matching_rule(self) -> None:
        working, merging, waiting, idle, unknown = [self.member(name) for name in
                                                   ("working", "merging", "waiting", "idle", "unknown")]
        now = datetime(2026, 11, 15, 18, tzinfo=UTC).timestamp()
        _ = self.write_run(working, "working", [("work", "Write delivery"), ("sent", "Send delivery")], [
            self.phase_event("pass_started", "work", pass_instance_id="pass", pass_kind="impl", worker_id="writer-id"),
            self.phase_event("checkpoint", "sent", checkpoint_commit="a" * 40)])
        _ = self.write_run(merging, "merging", [("merge", "Merge delivery")], [
            self.phase_event("checkpoint", "merge", checkpoint_commit="b" * 40)])
        _ = self.write_run(waiting, "waiting", [("wait", "Wait for memory")], [
            self.phase_event("wait_started", "wait", wait_id="wait", wait_kind="memory",
                wait_reason="memory admission", wait_started_epoch=now - 120)])
        _ = self.write_run(unknown, "unknown", [("work", "Unexplained work")])
        for row in (working, merging, waiting):
            _ = self.queue(row, unit_queue.QueuedItem("Wait one", "unknown/future"),
                           unit_queue.QueuedItem("Wait two", "unknown/future"))
        members = [unknown, waiting, idle, merging, working]
        with patch.object(waits_on, "state", wraps=waits_on.state) as dependencies:
            rendered = self.blocks(members, {}, workers=[census_worker("writer", working.id)])
        self.assertEqual(rendered.splitlines()[0],
            "working 1 · waiting to merge 1 · waiting on other work 1 · idle 1 · unknown 1 · 5 units")
        buckets = re.findall(r"^\*\*[^\n]+\*\* — (.+)$", rendered, re.MULTILINE)
        self.assertCountEqual(buckets, ["working", "waiting to merge", "waiting on other work", "idle", "unknown"])
        self.assertEqual(sum(map(int, re.findall(r" (\d+)(?: ·|$)", rendered.splitlines()[0].rsplit(" · ", 1)[0]))), len(buckets))
        self.assertEqual(dependencies.call_count, 1)

        for blocked_first in (False, True):
            with self.subTest(blocked_first=blocked_first):
                ready, blocked = unit_queue.QueuedItem("Ready delivery"), unit_queue.QueuedItem("Blocked delivery", "unknown/future")
                _ = self.queue(idle, *(blocked, ready) if blocked_first else (ready, blocked))
                rendered = self.blocks(members, {}, workers=[census_worker("writer", working.id)])
                self.assertEqual(rendered.splitlines()[0],
                    "working 1 · waiting to merge 1 · waiting on other work "
                    + ("2 · idle 0 · unknown 1 · 5 units" if blocked_first else "1 · idle 0 · unknown 2 · 5 units"))
                self.assertIn(("Now", "Not recorded", "Waits for unknown phase future to merge" if blocked_first
                              else "Director's current step not recorded"),
                              detail_rows(rendered, "idle"))

    def test_paused_exited_busy_and_no_worker_units_keep_their_reasons(self) -> None:
        member = self.member("holds")
        for hold, verb in (("pause", "paused"), ("exit", "stopped")):
            for by, why, expected in (
                    ("showrunner", "review the board", f"showrunner {verb} this unit: review the board; since 17:00"),
                    ("", "review the board", f"The unit is {verb}: review the board; since 17:00"),
                    ("showrunner", "", f"showrunner {verb} this unit: reason not recorded; since 17:00"),
                    ("", "", f"The unit is {verb}: reason not recorded; since 17:00")):
                with self.subTest(hold=hold, by=by, why=why):
                    held = replace(member,
                        paused=staff.Paused("2026-11-15T17:00:00+00:00", by, why)
                               if hold == "pause" else staff.Active(),
                        exited=staff.Exited("2026-11-15T17:00:00+00:00", by, why)
                               if hold == "exit" else staff.Active())
                    snapshot = staff.StaffSnapshot((held,))
                    rendered = unit_phases.table([unit("holds", None)._replace(member=held)], {},
                        now=datetime(2026, 11, 15, 18, tzinfo=UTC).timestamp(), zone=ZoneInfo("UTC"),
                        snapshot=snapshot, census_read=LOCAL)
                    self.assertIn("**holds** — idle", rendered)
                    self.assertIn(("Now", "Not recorded", expected), detail_rows(rendered, "holds"))
        rendered = self.blocks([member], {}, statuses=["busy"])
        self.assertIn(("Now", "Not recorded", "Director's current step not recorded"),
                      detail_rows(rendered, "holds"))
        from scripts.whoami import quota_state
        with patch.object(quota_state, "no_worker_mode", return_value=True):
            rendered = self.blocks([member], {})
        self.assertIn(("Also", "Not recorded", "Workers cannot start because no-worker mode is on"), detail_rows(rendered, "holds"))

    def test_queue_read_failure_keeps_other_units_and_names_the_missing_fact(self) -> None:
        member, other = self.member("unreadable"), self.member("other")
        path = self.queue(member)
        _ = path.write_text("not json")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            rendered = self.blocks([member, other], {})
        self.assertIn("**unreadable** — unknown", rendered)
        self.assertIn(("Next", "Not recorded", "Queue unavailable; next task not recorded"), detail_rows(rendered, "unreadable"))
        self.assertIn("**other** — idle", rendered)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn(str(path), errors.getvalue())

    def test_plain_table_reads_live_activity_and_staff_holds_even_when_gone(self) -> None:
        owner = fixture_member("showrunner", name="table-owner")
        first = fixture_member("unit", name="first", parent=owner.id)
        second = fixture_member("unit", name="second", parent=owner.id)
        doc = self.root / "status-production.md"
        _ = doc.write_text(f"# Production\n- **Merge branch:** main\n- **Showrunner checkout:** {self.root}\n- **Log:** log.md\n")
        owner = staff.set_doc(owner.id, staff.LiveDoc("natedev", doc))
        from scripts.production import session_staff

        first_session = unit("first", live("first")).session
        assert first_session is not None
        every = unit_lookup.LabeledProductions({owner.name: {"first": first_session}},
                                              {owner.name: ("first", "second")})

        def render() -> str:
            with (patch.object(unit_lookup, "member_sources", return_value=session_staff.MemberSources(())),
                  patch.object(sessions, "running_sessions", return_value=[sessions.SessionRecord(
                      sessionId=first.current_session, pid=1, name="first", status="idle",
                      messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])])):
                return table_of(live_units.units_in(owner.name, every), {})

        initial = render()
        self.assertIn("**first** — idle", initial)
        self.assertIn(("Now", "Not recorded", "Director's current step not recorded"), detail_rows(initial, "first"))
        _ = staff.pause(second.id, "review the change")
        paused = render()
        self.assertIn("**first** — idle", paused)
        self.assertIn("**second** — idle", paused)
        self.assertTrue(any(item == "Now" and work == "Not recorded" and state.startswith("The unit is paused: review the change; since ")
                            for item, work, state in detail_rows(paused, "second")))
        _ = staff.resume(second.id)
        resumed = render()
        self.assertIn("**second** — idle", resumed)
        self.assertNotIn("The unit is paused", resumed)
        for activity, hold, expected, detached in (("busy", "", "busy", False), ("shell", "", "busy", False),
                ("busy", "pause", "paused", False), ("busy", "exit", "exited", False),
                ("idle", "", "busy", True), ("busy", "", "busy", True),
                ("starting", "", "busy", False), ("starting", "", "busy", True),
                ("starting", "pause", "paused", True), ("starting", "exit", "exited", True)):
            with self.subTest(activity=activity, hold=hold):
                _ = staff.resume(first.id)
                if hold == "pause":
                    _ = staff.pause(first.id, "review")
                elif hold == "exit":
                    _ = staff.exit(first.id, "stop")
                observed = sessions.SessionRecord(sessionId=first.current_session, pid=1, name="first",
                    status=activity, messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
                snapshot = staff.snapshot()
                with (patch.object(sessions, "running_sessions", return_value=[observed]),
                      patch.object(session_state, "running_detached_work", return_value=session_state.RunningDetachedWork(
                          frozenset({first.id}) if detached else frozenset())),
                      patch.object(staff, "snapshot", wraps=staff.snapshot) as staff_read,
                      patch.object(session_state, "classify", wraps=session_state.classify) as states):
                    units = live_units.units_in(owner.name, every)
                    self.assertEqual(units[0].status, expected)
                    staff_read.assert_called_once_with()
                    self.assertEqual(states.call_count, len(units))
        _ = staff.resume(first.id)
        self.assertEqual(staff.by_id(first.id), first)
        codex = unit_lookup.LabeledSession("first", unit_lookup.ClaudeUnknown("no Claude registry record"),
            HerdrPlace("codex", "w1:p1"), process=unit_lookup.ObservedProcess(41, "codex"),
            identity=unit_lookup.LiveSession(first.current_session))
        for observed, machine, hold, expected in (
                (codex, "natedev", "", "idle"),
                (None, "natedev", "", "exited"),
                (codex, "mac", "", "idle"),
                (None, "mac", "", "exited"),
                (None, "mac", "pause", "paused"),
                (None, "mac", "exit", "exited")):
            with self.subTest(process=observed, machine=machine, hold=hold):
                member = replace(first, worktree=staff.PresentWorktree(machine, self.root),
                    paused=staff.Paused("2026-11-08", "fixture", "pause") if hold == "pause" else staff.Active(),
                    exited=staff.Exited("2026-11-08", "fixture", "exit") if hold == "exit" else staff.Active())
                snapshot = staff.StaffSnapshot((owner, member, second))
                marked: dict[str, unit_lookup.LabeledSession] = {"first": observed} if observed is not None else {}
                listing = unit_lookup.LabeledProductions({owner.name: marked}, {owner.name: ("first", "second")})
                with (patch.object(staff, "snapshot", return_value=snapshot) as staff_read,
                      patch.object(sessions, "running_sessions", return_value=[])):
                    units = live_units.units_in(owner.name, listing)
                    self.assertEqual(units[0].status, expected)
                    self.assertEqual(units[0].machine, machine)
                    output = unit_phases.table(units, {}, snapshot=snapshot,
                                               census_read=tiles(sessions=[session_of(member, expected)]))
                    if machine == "mac":
                        self.assertIn("**first** (mac) — idle", output)
                        self.assertIn("**second** (—) — idle", output)
                    else:
                        self.assertNotIn("(natedev)", output)
                        self.assertIn("**first** — idle", output)
                staff_read.assert_called_once_with()

    def test_read_failures_print_below_the_table_and_send_one_configured_message(self) -> None:
        self.assert_anomaly_delivery(send_code=0)

    def test_failed_anomaly_send_keeps_the_table_and_exit_code_and_adds_one_line(self) -> None:
        self.assert_anomaly_delivery(send_code=1)

    def assert_anomaly_delivery(self, *, send_code: int) -> None:
        from scripts.production import session_staff

        doc = self.root / "production.md"
        _ = doc.write_text(f"# Production\n- **Merge branch:** main\n- **Showrunner checkout:** {self.root}\n- **Log:** log.md\n")
        owner = fixture_member("showrunner", name="anomaly-owner", doc=staff.LiveDoc("natedev", doc))
        member = fixture_member("unit", name="member", parent=owner.id)
        config = self.root / "config"
        config.mkdir()
        config_source = Path(__file__).resolve().parents[2] / "config/production.conf"
        _ = (config / "production.conf").write_text(re.sub(r"(?m)^UNIT_ANOMALIES_TO=.*$", "UNIT_ANOMALIES_TO=fixture-director", config_source.read_text()))
        def observed(*, record: staff.StaffSnapshot, running: object = None) -> tuple[session_staff.LabeledSession, ...]:
            self.assertEqual(record.by_id(member.id), member)
            self.assertIsNone(running)
            for pid, identity in enumerate(("unread-codex", "unread-claude"), 920000):
                session_staff.report_anomaly(f"pid {pid}: {identity}: session record is unreadable", pid=pid)
            return ()

        output, errors = io.StringIO(), io.StringIO()
        sender = Mock(return_value=subprocess.CompletedProcess(
            ["send.py"], send_code, "fixture delivery detail" if send_code else "",
            "fixture send failed\nfixture failure detail" if send_code else ""))
        with (patch.dict(os.environ, {"CONF_DIR": str(config)}),
              patch.object(unit_lookup, "observed_session_labels", side_effect=observed),
              patch.object(unit_lookup, "member_sources", return_value=session_staff.MemberSources(())),
              patch.object(unit_phases, "production_records", return_value={}),
              patch.object(unit_phases, "subprocess", SimpleNamespace(run=sender)),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            result = unit_phases.main(["--production", owner.name])
        self.assertEqual(result, 0, errors.getvalue())
        lines = output.getvalue().splitlines()
        table_end = max(index for index, line in enumerate(lines) if line.startswith("| "))
        self.assertEqual(lines[:table_end + 1], [
            "working 0 · waiting to merge 0 · waiting on other work 0 · idle 1 · 1 units", "",
            "| Unit | Status |", "| --- | --- |", "| member | exited |", "",
            "**member** — idle", "",
            "| Item | Work | State |", "| --- | --- | --- |",
            "| Now | Not recorded | Director's current step not recorded |",
            "| Next | Not recorded | The director has not chosen the next task |",
        ])
        anomalies = [line for line in lines if line.startswith("anomaly:")
                     and ("unread-codex" in line or "unread-claude" in line)]
        self.assertEqual(len(anomalies), 2, output.getvalue())
        for identity in ("unread-codex", "unread-claude"):
            matching = [line for line in anomalies if identity in line]
            self.assertEqual(len(matching), 1, output.getvalue())
            self.assertGreater(lines.index(matching[0]), table_end)
        self.assertNotIn("anomaly:", errors.getvalue())
        sender.assert_called_once()
        call = sender.call_args
        assert call is not None
        command = cast(list[str], call.args[0])
        self.assertEqual(command[command.index("--to") + 1], "fixture-director")
        message = command[command.index("--text") + 1]
        for line in anomalies:
            self.assertIn(line, message)
        extra = [line for line in lines[table_end + 1:] if line.strip() and line not in anomalies]
        self.assertEqual(len(extra), 1 if send_code else 0, output.getvalue())
        if send_code:
            self.assertEqual(extra, [
                "anomaly: could not send unit anomalies to fixture-director: "
                + "stderr: fixture send failed fixture failure detail stdout: fixture delivery detail"
            ])
        self.assertEqual(staff.by_id(member.id), member)

    def test_table_without_anomalies_sends_no_message(self) -> None:
        doc = self.root / "production.md"
        _ = doc.write_text(f"# Production\n- **Merge branch:** main\n- **Showrunner checkout:** {self.root}\n- **Log:** log.md\n")
        owner = fixture_member("showrunner", name="quiet-owner", doc=staff.LiveDoc("natedev", doc))
        member = fixture_member("unit", name="member", parent=owner.id)
        from scripts.production import session_staff

        output = io.StringIO()
        sender = Mock()
        with (patch.object(unit_lookup, "observed_session_labels", return_value=()),
              patch.object(unit_lookup, "member_sources", return_value=session_staff.MemberSources(())),
              patch.object(unit_phases, "production_records", return_value={}),
              patch.object(unit_phases, "subprocess", SimpleNamespace(run=sender)), contextlib.redirect_stdout(output)):
            result = unit_phases.main(["--production", owner.name])
        self.assertEqual(result, 0)
        self.assertIn("**member** — idle", output.getvalue())
        self.assertNotIn("anomaly:", output.getvalue())
        sender.assert_not_called()
        self.assertEqual(staff.by_id(member.id), member)

    def test_census_failure_shows_no_status_or_workers_and_one_anomaly(self) -> None:
        doc = self.root / "production.md"
        _ = doc.write_text(f"# Production\n- **Merge branch:** main\n- **Showrunner checkout:** {self.root}\n- **Log:** log.md\n")
        owner = fixture_member("showrunner", name="failed-owner", doc=staff.LiveDoc("natedev", doc))
        _ = fixture_member("unit", name="member", parent=owner.id)
        from scripts.production import session_staff

        output = io.StringIO()
        sender = Mock(return_value=subprocess.CompletedProcess(["send.py"], 0, "", ""))
        with (patch.object(unit_lookup, "observed_session_labels", return_value=()),
              patch.object(unit_lookup, "member_sources", return_value=session_staff.MemberSources(())),
              patch.object(unit_phases, "production_records", return_value={}),
              patch.object(census, "read", return_value=census.CensusFailed("census took longer than 10 s")),
              patch.object(unit_phases, "subprocess", SimpleNamespace(run=sender)), contextlib.redirect_stdout(output)):
            result = unit_phases.main(["--production", owner.name])
        self.assertEqual(result, 0)
        lines = output.getvalue().splitlines()
        table_end = max(index for index, line in enumerate(lines) if line.startswith("| "))
        self.assertEqual(lines[:table_end + 1], [
            "working 0 · waiting to merge 0 · waiting on other work 0 · idle 0 · unknown 1 · 1 units", "",
            "| Unit |", "| --- |", "| member |", "",
            "**member** — unknown", "",
            "| Item | Work | State |", "| --- | --- | --- |",
            "| Now | Not recorded | Workers unavailable; who runs cannot be determined |",
            "| Next | Not recorded | The director has not chosen the next task |"])
        self.assertEqual([line for line in lines if line.startswith("anomaly:")],
                         ["anomaly: census took longer than 10 s"])
        sender.assert_called_once()

    def production_document(self, doc: Path) -> None:
        _ = doc.write_text(doc.read_text() + f"\n- **Merge branch:** main\n"
                           + f"- **Showrunner checkout:** {self.root}\n- **Log:** log.md\n")
        _ = fixture_member("showrunner", name="show", doc=staff.LiveDoc(remote.local_machine(), doc))

    def test_main_keeps_plan_history_functions_intact_while_it_reads_and_renders(self) -> None:
        doc = self.root / "production.md"
        _ = doc.write_text("# Production\n", encoding="utf-8")
        self.production_document(doc)
        headers = plan_history.run_started_event
        paths = plan_history.resolve_plan_path
        scans = plan_history.plan_runs
        calls: list[str] = []

        def records(_doc: Path) -> dict[str, object]:
            self.assertIs(plan_history.run_started_event, headers)
            self.assertIs(plan_history.resolve_plan_path, paths)
            self.assertIs(plan_history.plan_runs, scans)
            calls.append("records")
            return {}

        def render(_units: list[live_units.LiveUnit], _records: dict[str, object], *, zone: ZoneInfo,
                   census_read: census.Census | census.CensusFailed, columns: int | None) -> str:
            self.assertIs(plan_history.run_started_event, headers)
            self.assertIs(plan_history.resolve_plan_path, paths)
            self.assertIs(plan_history.plan_runs, scans)
            self.assertIsInstance(zone, ZoneInfo)
            self.assertIs(census_read, LOCAL)
            self.assertIsNone(columns)
            calls.append("table")
            return "phase table"

        output = io.StringIO()
        with (patch.object(live_units, "read_units", return_value=live_units.UnitListing([unit("one", live("one"))], ())),
              patch.object(unit_phases, "production_records", side_effect=records),
              patch.object(unit_phases, "table", side_effect=render),
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        self.assertEqual(calls, ["records", "table"])
        self.assertEqual(output.getvalue(), "phase table\n")
        self.assertIs(plan_history.run_started_event, headers)
        self.assertIs(plan_history.resolve_plan_path, paths)
        self.assertIs(plan_history.plan_runs, scans)

    def test_main_reads_shared_history_once_for_multiple_unit_plans(self) -> None:
        from scripts.production import session_staff

        doc = self.root / "production.md"
        _ = doc.write_text("# Production\n")
        self.production_document(doc)
        history = self.root / "history" / "runs"
        history.mkdir(parents=True)
        run_files: list[Path] = []
        for name in ("one", "two"):
            tree = self.root / name
            member = recorded_unit("show", name, tree, doc, plan="plan.md")
            _ = (tree / "plan.md").write_text(
                "# Plan\n### Phase 1 — First work · status: todo\n### Phase 2 — Later work · status: todo\n")
            folder = self.root / "runs" / name
            folder.mkdir(parents=True)
            _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps({"owner_staff_id": member.id}))
            _ = staff.set_run(member.id, staff.OpenRun(folder))
            path = history / f"{name}.jsonl"
            events: list[dict[str, object]] = [
                {"event_type": "run_started", "owner_staff_id": member.id, "run_id": name,
                 "working_dir": str(tree), "plan_doc": "plan.md", "run_started_at": 100},
                {"event_type": "phase_started", "phase_id": "1", "phase_title": "First work",
                 "phase_instance_id": name, "phase_started_at": 110},
                {"event_type": "pass_started", "phase_id": "1", "phase_instance_id": name,
                 "pass_instance_id": name, "pass_kind": "impl", "pass_activity": f"write {name}"},
            ]
            _ = path.write_text("".join(json.dumps(event) + "\n" for event in events))
            run_files.append(path)
        output = io.StringIO()
        with (patch.object(unit_lookup, "member_sources", return_value=session_staff.MemberSources(())),
              patch.object(unit_lookup, "observed_session_labels", return_value=()),
              patch.object(plan_history, "run_started_event", wraps=plan_history.run_started_event) as headers,
              patch.object(staff, "snapshot", wraps=staff.snapshot) as staff_reads,
              patch.object(census, "read", wraps=census.read) as census_reads,
              patch.object(io, "open", wraps=io.open) as opened,
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        self.assertCountEqual([call.args[0] for call in headers.call_args_list], run_files)
        self.assertEqual(staff_reads.call_count, 1)
        self.assertEqual(census_reads.call_count, 1)
        for path in run_files:
            with self.subTest(run=path.name):
                reads = sum(call.args[0] == path for call in opened.call_args_list)
                self.assertEqual(reads - 1, 1, "Read the run events once after the header read.")
        self.assertEqual(output.getvalue().splitlines(), [
            "working 0 · waiting to merge 0 · waiting on other work 0 · idle 0 · unknown 2 · 2 units", "",
            "| Unit | Status | Progress | Phase | ETA Next |", "| --- | --- | --- | --- | --- |",
            "| one | exited | 🟨⬜ | 1 | no ETA stated |", "| two | exited | 🟨⬜ | 1 | no ETA stated |", "",
            "Progress: 🟩 done · 🟨 open · ⬜ not started", "",
            "**one** — unknown", "",
            "| Item | Work | State |", "| --- | --- | --- |",
            "| Now | First work (1) | Director's current step not recorded |",
            "| Next | Later work (2) | Not started |", "",
            "**two** — unknown", "",
            "| Item | Work | State |", "| --- | --- | --- |",
            "| Now | First work (1) | Director's current step not recorded |",
            "| Next | Later work (2) | Not started |",
        ])

    def test_without_a_production_the_callers_staff_name_selects_it(self) -> None:
        owner = fixture_member("showrunner", name="table-owner")
        _ = fixture_member("unit", name="a", parent=owner.id)
        doc = self.root / "show-production.md"
        _ = doc.write_text("# Production\n", encoding="utf-8")
        self.production_document(doc)
        out = io.StringIO()
        with (patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "caller"}),
              patch.object(showrunners, "own_name", return_value="show") as found,
              patch.object(live_units, "read_units", return_value=live_units.UnitListing([unit("a", live("a"))], ())),
              patch.object(census, "read", return_value=census_for([unit("a", live("a"))])),
              patch.object(unit_phases, "production_records", return_value={}),
              contextlib.redirect_stdout(out)):
            self.assertEqual(unit_phases.main([]), 0)
        found.assert_called_once_with()
        self.assertIn("**a** — idle", out.getvalue())

    def test_a_session_that_is_no_showrunner_is_told_to_name_the_production(self) -> None:
        err = io.StringIO()
        with (patch.object(showrunners, "own_name", return_value=""),
              contextlib.redirect_stderr(err)):
            self.assertEqual(unit_phases.main([]), 1)
        self.assertIn("pass --production", err.getvalue())

    def test_style_kind_routes_to_the_ticket_table(self) -> None:
        doc = self.root / "style-production.md"
        _ = doc.write_text(f"# Production\n- **Kind:** style\n- **Merge branch:** style/main\n"
                           + f"- **Branch point:** main\n- **Showrunner checkout:** `{self.root}`\n")
        write_style_units(doc)
        self.production_document(doc)
        _ = subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True,
                           timeout=10)
        owner = fixture_member("showrunner", name="show", doc=staff.LiveDoc(remote.local_machine(), doc))
        _ = fixture_member("unit", name="names", parent=owner.id,
                           worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        units = [unit("names", live("names"))]
        records: dict[str, object] = {"names": {"plan": "/tmp/claude/names.md"}}
        output = io.StringIO()
        with (patch.object(live_units, "read_units", return_value=live_units.UnitListing(units, ())),
              patch.object(phase_table, "show_production", return_value=json.dumps(records)),
              patch.object(style_units, "table", return_value="ticket table") as tickets,
              patch.object(style_ledger, "ledger_location", return_value=style_ledger.LedgerLocation(
                  self.root, self.root / "ledger.sqlite")) as locate,
              patch.object(unit_phases, "table") as phases,
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        tickets.assert_called_once_with(units, records, doc=doc, ledger_paths={"names": self.root / "ledger.sqlite"})
        locate.assert_called_once_with(self.root)
        phases.assert_not_called()
        self.assertEqual(output.getvalue(), "ticket table\n")

        unordered = [unit("zulu", live("zulu")), units[0], unit("Alpha", live("Alpha"))]
        output = io.StringIO()
        with (patch.object(live_units, "read_units", return_value=live_units.UnitListing(unordered, ())),
              patch.object(phase_table, "show_production", return_value=json.dumps(records)),
              patch.object(style_units, "table", return_value="sorted ticket table") as tickets,
              patch.object(style_ledger, "ledger_location", return_value=style_ledger.LedgerLocation(
                  self.root, self.root / "ledger.sqlite")),
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        tickets.assert_called_once_with([unordered[2], units[0], unordered[0]], records, doc=doc,
                                        ledger_paths={"names": self.root / "ledger.sqlite"})
        self.assertEqual(output.getvalue(), "sorted ticket table\n")

    def test_product_parent_reports_direct_style_units_and_product_units(self) -> None:
        from scripts.style import style_config

        doc = self.root / "product-production.md"
        _ = doc.write_text(f"# Production\n- **Kind:** product\n- **Merge branch:** main\n"
                           + f"- **Showrunner checkout:** {self.root}\n")
        write_style_units(doc, (style_config.StyleUnitAssignment(
            "cargo-liner-style", ("names", "values", "types", "modules")),))
        self.production_document(doc)
        style_unit = unit("cargo-liner-style", live("cargo-liner-style"))
        product_unit = unit("feature", live("feature"))
        units = [product_unit, style_unit]
        output = io.StringIO()
        before = doc.read_bytes()
        with (patch.object(live_units, "read_units", return_value=live_units.UnitListing(units, ())),
              patch.object(phase_table, "show_production", return_value="{}"),
              patch.object(unit_phases, "style_ledger_paths", return_value={}),
              patch.object(unit_phases, "table", return_value="product table") as phases,
              patch.object(style_units, "table", return_value="ticket table") as tickets,
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        phases.assert_called_once_with([product_unit], {}, zone=read_production("show").zone,
                                                 census_read=LOCAL, columns=None)
        tickets.assert_called_once_with([style_unit], {}, doc=doc, ledger_paths={})
        self.assertEqual(doc.read_bytes(), before)
        self.assertEqual(output.getvalue(), "product table\n\nticket table\n")

    def test_other_or_absent_kind_keeps_the_phase_table(self) -> None:
        owner = fixture_member("showrunner", name="table-owner")
        _ = fixture_member("unit", name="a", parent=owner.id)
        for kind in ("", "- **Kind:** feature\n"):
            with self.subTest(kind=kind):
                doc = self.root / "phase-production.md"
                _ = doc.write_text("# Production\n" + kind, encoding="utf-8")
                self.production_document(doc)
                units = [unit("a", live("a"))]
                output = io.StringIO()
                with (patch.object(live_units, "read_units", return_value=live_units.UnitListing(units, ())),
                      patch.object(census, "read", return_value=census_for(units)),
                      patch.object(unit_phases, "production_records", return_value={}),
                      patch.object(style_units, "table") as tickets, contextlib.redirect_stdout(output)):
                    self.assertEqual(unit_phases.main(["--production", "show"]), 0)
                tickets.assert_not_called()
                self.assertEqual(output.getvalue(),
                    "working 0 · waiting to merge 0 · waiting on other work 0 · idle 1 · 1 units\n\n"
                    + "| Unit | Status |\n| --- | --- |\n| a | idle |\n\n"
                    + "**a** — idle\n\n| Item | Work | State |\n| --- | --- | --- |\n"
                    + "| Now | Not recorded | Director's current step not recorded |\n"
                    + "| Next | Not recorded | The director has not chosen the next task |\n")

                unordered = [unit("zulu", live("zulu"))._replace(machine="natedev"),
                             unit("Beta", live("Beta"))._replace(machine="mac"),
                             unit("alpha", live("alpha"))._replace(machine="natedev")]
                output = io.StringIO()
                with (patch.object(live_units, "read_units", return_value=live_units.UnitListing(unordered, ())),
                      patch.object(unit_phases, "production_records", return_value={}),
                      patch.object(style_units, "table") as tickets, contextlib.redirect_stdout(output)):
                    self.assertEqual(unit_phases.main(["--production", "show"]), 0)
                tickets.assert_not_called()
                self.assertEqual([line.split(" — ")[0] for line in output.getvalue().splitlines()
                                  if line.startswith("**")],
                                 ["**alpha** (natedev)", "**Beta** (mac)", "**zulu** (natedev)"])
                self.assertEqual(output.getvalue().count("| Now |"), 3)
                self.assertEqual(output.getvalue().count("| Next |"), 3)

    def test_a_missing_plan_folder_keeps_ledger_data_and_an_unresolved_worktree_keeps_its_row(self) -> None:
        _ = subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        _ = self.enterContext(patch.dict(os.environ, {"STYLE_LEDGER_STATE_DIR": str(self.root / "state")}))  # pyright: ignore[reportAny]
        doc = self.root / "style-production.md"
        _ = doc.write_text(f"# Production\n- **Kind:** style\n- **Merge branch:** style/main\n"
                           + f"- **Branch point:** main\n- **Showrunner checkout:** `{self.root}`\n")
        write_style_units(doc)
        self.production_document(doc)
        owner = fixture_member("showrunner", name="show", doc=staff.LiveDoc(remote.local_machine(), doc))
        for name, folder in (("names", self.root), ("types", self.root / "gone")):
            _ = fixture_member("unit", name=name, parent=owner.id,
                               worktree=staff.PresentWorktree(remote.local_machine(), folder))
        decision = self.root / "decision.md"
        names = next(row for row in staff.initial_snapshot().find(kind="unit", parent=owner.id) if row.name == "names")
        _ = decision.write_text(f"---\nstatus: open\nunit: {names.id}\nplan: approved\nstyle_rule_blob: names\n---\n")
        database = style_ledger.ledger_location(self.root).database
        database.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            _ = connection.executescript("""
                CREATE TABLE rule_texts(blob TEXT, text BLOB);
                CREATE TABLE tickets(id TEXT,unit TEXT,kind TEXT,family TEXT,rule TEXT,crate TEXT,
                    files TEXT,sites TEXT,rules TEXT,finding_ids TEXT,state TEXT,created_at TEXT);
                CREATE TABLE held_findings(proposal TEXT,state TEXT,build_ticket TEXT,built_at TEXT);
                CREATE TABLE refill_results(unit TEXT,at TEXT,outcome TEXT,split_at TEXT,
                    ready INTEGER,changed INTEGER,structure INTEGER,other INTEGER);
            """)
            _ = connection.execute("INSERT INTO rule_texts VALUES ('names',?)", (b"---\nfamily: names\n---\n",))
            _ = connection.execute("INSERT INTO tickets VALUES ('12',?,'proposal','names',"
                + "'global:rust/read.md','alpha','[]','[]','[]','[\"one\"]','assigned','2026-10-01')", (str(names.id),))
            _ = connection.execute("INSERT INTO held_findings VALUES (?,'filed','12','')", (str(decision),))
        output = io.StringIO()
        with (patch.object(live_units, "read_units", return_value=live_units.UnitListing([unit("names", live("names")),
                                                                        unit("types", live("types"))], ())),
              patch.object(phase_table, "show_production", return_value=json.dumps({
                  "names": {"unavailable": "plan cannot be read"},
                  "types": {"unavailable": "worktree cannot be read"}})),
              contextlib.redirect_stdout(output)):
            self.assertEqual(unit_phases.main(["--production", "show"]), 0)
        self.assertIn("| names | idle | #12 · read · alpha · 1 finding | starting 1 finding | 0 | 0 | 1 |", output.getvalue())
        self.assertIn("| types | idle | idle | idle | 0 | 0 | 0 |", output.getvalue())

    def test_production_of_reads_the_staff_row_addressed_to_the_session(self) -> None:
        doc = self.root / "show-production.md"
        for name, target in (("showrunner-other", "session:someone-else"), ("showrunner-show", "session:caller")):
            directory = self.root / name
            directory.mkdir()
            _ = (directory / "conf").write_text(
                f"TARGET={target}\nCHECK=zsh /opt/production_check.sh show\n", encoding="utf-8")
        caller = fixture_runtime_id("unit-phases-caller")
        _ = fixture_member("showrunner", name="show", runtime_id=caller, doc=staff.LiveDoc(remote.local_machine(), doc))
        self.assertEqual(showrunners.production_of(caller, self.root), doc)
        self.assertIsNone(showrunners.production_of("nobody", self.root))
        self.assertIsNone(showrunners.production_of("", self.root))



from scripts.production.staff_test_support import StaffTestCase


class BranchLedgerLocationTests(StaffTestCase):
    def test_style_ledger_uses_the_staff_worktree(self) -> None:
        fixture = self.separated_unit_folders()
        paths = unit_phases.style_ledger_paths(fixture.doc, [live_units.LiveUnit("member", None)])
        self.assertEqual(paths, {"member": style_ledger.ledger_location(fixture.worktree).database})


if __name__ == "__main__":
    _ = unittest.main()
