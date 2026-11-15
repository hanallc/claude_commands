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

from scripts.direct import plan_history
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

    def test_the_queued_cell_names_the_wait_and_its_state(self) -> None:
        now, utc = 1_794_750_000.0, ZoneInfo("UTC")
        failed: land_runs.LandResult = {"commit": "c" * 40, "production": "builds", "unit": "report", "phase": "89",
                                        "landed": False, "step": "red", "state": "failed", "detail": "test_x red",
                                        "time": datetime.fromtimestamp(now - 600, UTC).isoformat()}
        line = {"c" * 40: land_queue.Place(10, "builds batch589", "report 89")}
        item = unit_queue.QueuedItem("acctland", "builds/589")

        def text(found: waits_on.WaitState | None, running: frozenset[str] | None = None,
                 results: dict[str, land_runs.LandResult] | None = None) -> str:
            view = land_queue.LandView(dict.fromkeys(running or (), ""), line, results or {}, frozenset(), {})
            return unit_phases.queued_text(item, found, view, now=now, zone=utc)

        self.assertEqual(text(waits_on.OnMain("a" * 40)), "ready: acctland (builds/589 on main)")
        self.assertEqual(text(waits_on.PhaseSent("c" * 40)), "acctland — after builds/589: done, waits to land, 10th in line")
        self.assertEqual(text(waits_on.NotOnMain("c" * 40), results={"c" * 40: failed}),
                         "acctland — after builds/589: waits to land, 10th in line; last try failed at red 13:30: test_x red")
        self.assertEqual(text(waits_on.PhaseSent("c" * 40), frozenset({"c" * 9})), "acctland — after builds/589: done, landing now")
        self.assertEqual(text(waits_on.NotOnMain("d" * 40)), "acctland — after builds/589: not on main")
        self.assertEqual(text(waits_on.NotOnMain("d" * 40), results={"d" * 40: {**failed, "commit": "d" * 40}}),
                         "acctland — after builds/589: did not land: failed at red 13:30: test_x red")
        self.assertEqual(text(waits_on.PhaseOpen(now + 3600)), "acctland — after builds/589: in work, ETA 14:40")
        self.assertEqual(text(waits_on.PhaseOpen(None)), "acctland — after builds/589: in work, no ETA")
        self.assertEqual(text(waits_on.PhaseNotStarted()), "acctland — after builds/589: not started")
        self.assertEqual(text(waits_on.NotFound()), "acctland — after builds/589: not found")
        self.assertEqual(unit_phases.queued_text(unit_queue.QueuedItem("plain"), None,
                                                 land_queue.LandView({}, {}, {}, frozenset(), {}), now=now, zone=utc), "plain")
        landed: land_runs.LandResult = {**failed, "commit": "d" * 40, "landed": True}
        self.assertEqual(text(waits_on.PhaseSent("d" * 40), results={"d" * 40: landed}),
                         "acctland — after builds/589: done, landed 13:30, not on main yet")

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

        def lands(skip: tuple[str, ...] = (), on_main: bool = False) -> str:
            return unit_phases.unit_lands("shelled", view, skip=skip, now=now, zone=utc, on_main=lambda _commit: on_main)

        self.assertEqual(lands(), "landing now: images-card · done, waits to land: shot-rule (1st in line), "
                         + "workers-why + land-line (2nd in line) · center-column did not land: merge conflict in docs/a.md, b.md 13:30 "
                         + "· images did not land: merge conflict in docs/a.md, b.md 13:40")
        self.assertEqual(lands(("images-card", "shot-rule", "land-line", "images"), on_main=True),
                         "done, waits to land: workers-why (2nd in line)")
        self.assertEqual(unit_phases.unit_lands("nobody", view, skip=(), now=now, zone=utc, on_main=lambda _commit: False), "")


if __name__ == "__main__":
    _ = unittest.main()
