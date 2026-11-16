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

        self.assertEqual(text(waits_on.OnMain("a" * 40)),
                         "acctland. Ready: builds/589 is on the local copy of main.")
        self.assertEqual(text(waits_on.PhaseSent("c" * 40)),
                         "acctland. After builds/589: waiting to merge, 10th in line.")
        self.assertEqual(text(waits_on.NotOnMain("c" * 40), results={"c" * 40: failed}),
                         "acctland. After builds/589: absent from the local copy of main; waiting to merge, "
                         + "10th in line; last attempt failed at red: test_x red; 13:30.")
        self.assertEqual(text(waits_on.PhaseSent("c" * 40), frozenset({"c" * 9})),
                         "acctland. After builds/589: merging now.")
        self.assertEqual(text(waits_on.NotOnMain("d" * 40)),
                         "acctland. After builds/589: absent from the local copy of main.")
        self.assertEqual(text(waits_on.NotOnMain("d" * 40), results={"d" * 40: {**failed, "commit": "d" * 40}}),
                         "acctland. After builds/589: absent from the local copy of main; "
                         + "did not merge: failed at red: test_x red; 13:30.")
        for found, expected in ((waits_on.PhaseOpen(now + 3600), "started"),
                                (waits_on.PhaseOpen(None), "started"),
                                (waits_on.PhaseNotStarted(), "not started"),
                                (waits_on.NotFound(), "dependency not found locally"),
                                (waits_on.PhaseSent("d" * 40),
                                 "submitted; absent from the merge line; reason not recorded")):
            with self.subTest(state=found):
                self.assertEqual(text(found), f"acctland. After builds/589: {expected}.")
        self.assertEqual(unit_phases.queued_text(unit_queue.QueuedItem("plain"), None,
                                                 land_queue.LandView({}, {}, {}, frozenset(), {}), now=now, zone=utc), "plain")
        landed: land_runs.LandResult = {**failed, "commit": "d" * 40, "landed": True}
        self.assertEqual(text(waits_on.PhaseSent("d" * 40), results={"d" * 40: landed}),
                         "acctland. After builds/589: merged 13:30.")
        self.assertEqual(text(waits_on.NotOnMain("d" * 40), results={"d" * 40: landed}),
                         "acctland. After builds/589: absent from the local copy of main; merged 13:30.")

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
            "center-column did not merge: merge conflict in docs/a.md, b.md; 13:30.",
            "images did not merge: merge conflict in docs/a.md, b.md; 13:40.")
        running = "images-card is done; merging now."
        first = "shot-rule is done; waiting to merge, 1st in line."
        paired = "workers-why and land-line are done; merge together, 2nd in line."
        carried_view = view._replace(carried_line={"7" * 40: line["c" * 40]})
        running_view = view._replace(running={"0" * 40: "shelled images-card", "8" * 40: "shelled live-owner"},
                                    carried_running=frozenset({"7" * 40}))
        cases: list[tuple[str, land_queue.LandView, dict[str, unit_phases.PhaseSentWork], bool,
                          tuple[str, ...], frozenset[str]]] = [
            ("no open rows", view, {}, False, (running, first, paired, *failures), frozenset()),
            ("open rows before other phases", view,
             {"images-card": unit_phases.SentCommit("0" * 40), "shot-rule": unit_phases.SentCommit("a" * 40),
              "land-line": unit_phases.SentCommit("c" * 40), "images": unit_phases.SentCommit("f" * 40)}, True,
             (running, first, "land-line and workers-why are done; merge together, 2nd in line.", failures[1]),
             frozenset({"land-line"})),
            ("single open row", view, {"shot-rule": unit_phases.SentCommit("a" * 40)}, False,
             (running, first, paired, *failures), frozenset()),
            ("carried commit joins its item", carried_view,
             {"workers-why": unit_phases.SentCommit("b" * 40), "checkpoint-wait": unit_phases.SentCommit("7" * 40),
              "land-line": unit_phases.SentCommit("c" * 40)}, False,
             (running, first, "workers-why, checkpoint-wait and land-line are done; merge together, 2nd in line.", *failures),
             frozenset({"workers-why", "checkpoint-wait", "land-line"})),
            ("running direct and carried commits", running_view,
             {"carried": unit_phases.SentCommit("7" * 40), "images-card": unit_phases.SentCommit("0" * 40)}, False,
             ("carried, images-card and live-owner are done; merging now.", first, paired, *failures),
             frozenset({"carried", "images-card"})),
            ("single running open row", view, {"images-card": unit_phases.SentCommit("0" * 40)}, False,
             (running, first, paired, *failures), frozenset()),
            ("open failed work remains in Merge", view, {"images": unit_phases.SentCommit("f" * 40)}, False,
             (running, first, paired, failures[1], failures[0]), frozenset()),
            ("carried phase names its other unit", view._replace(carried_line={
                "7" * 40: land_queue.Place(4, "builds batch", "builder carrier")}),
             {"carried": unit_phases.SentCommit("7" * 40)}, False,
             (running, first, paired, "carried is done; merges with builder carrier, 4th in line.", *failures), frozenset()),
            ("submitted work has no line", view,
             {"absent": unit_phases.SentCommit("6" * 40), "no-hash": unit_phases.SentCommitUnrecorded(),
              "not-sent": unit_phases.NoSentWork()}, False,
             (running, first, paired,
              "absent was submitted; absent from the merge line; reason not recorded.",
              "no-hash was submitted; merge position not recorded.", *failures), frozenset()),
        ]
        for name, found, open_sent, on_main, expected, grouped in cases:
            with self.subTest(group=name):
                result = unit_phases.unit_lands("shelled", found, open_sent=open_sent, now=now, zone=utc,
                                                on_main=lambda _commit: on_main)
                self.assertEqual(result.entries, expected)
                self.assertEqual(result.text, " · ".join(expected))
                self.assertEqual(result.grouped_phases, grouped)
                self.assertTrue(result.pending)
        result = unit_phases.unit_lands("nobody", view, open_sent={}, now=now, zone=utc, on_main=lambda _commit: False)
        self.assertEqual(result.text, "")
        self.assertEqual(result.entries, ())
        self.assertEqual(result.grouped_phases, frozenset())
        self.assertFalse(result.pending)


if __name__ == "__main__":
    _ = unittest.main()
