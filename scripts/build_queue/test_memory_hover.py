"""Check the memory page hover with private captured readings."""
from __future__ import annotations
from scripts.build_queue import admission_records

from collections.abc import Mapping
import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from random import Random
import re
import shutil
import subprocess
import tempfile
import unittest
from xml.etree import ElementTree
from typing import cast
from scripts.lint import memory_admit as gate
from scripts.build_queue import limits_view, memory_feed, program_memory_feed, progress_profile as progress, snapshot_codec
from scripts.build_queue import test_memory_feed
from scripts.disks import disks_page
from scripts.lint.memory_test_support import admission_policy_fixture
from scripts.lint import test_memory_admit as memory_tests
from scripts.lib import test_hang_guard
from scripts.review_links import page_frame
from scripts.review_links import new_item_dialog
from scripts.production import staff
from scripts.review_links import staff_names
GIB = admission_records.GIB
MIB = 1024 ** 2
RAM_FIXTURE: dict[str, object] = {
    "capture_time": 100.0, "attribution_read_at": 100.0, "total_bytes": 64 * GIB, "available_bytes": 30 * GIB,
    "safety_target_bytes": 4 * GIB, "swap_target_bytes": 2 * GIB, "build_target_bytes": 6 * GIB,
    "room_bytes": 18 * GIB, "shortfall_bytes": 0, "safety_covered_bytes": 4 * GIB, "swap_covered_bytes": 2 * GIB,
    "build_covered_bytes": 6 * GIB, "safety_missing_bytes": 0, "swap_missing_bytes": 0, "build_missing_bytes": 0,
    "queued_builds_bytes": 3 * GIB, "build_tools_bytes": 2 * GIB, "cap_bytes": 31 * GIB, "rust_analyzer_bytes": GIB, "sccache_bytes": 512 * MIB,
    "other_build_tools_bytes": 512 * MIB, "ci_bytes": 2 * GIB, "agents_bytes": 8 * GIB, "desktop_bytes": 3 * GIB,
    "desktop_protection_bytes": 2 * GIB, "other_programs_bytes": 4 * GIB, "smaller_programs_bytes": GIB,
    "smaller_program_count": 5,
    "programs": [{"id": "firefox", "name": "firefox", "bytes": 2 * GIB}, {"id": "zed", "name": "zed", "bytes": GIB}],
    "kernel_bytes": 3 * GIB, "compressed_swap_bytes": GIB * 3 // 2, "kernel_structures_bytes": GIB,
    "kernel_counters_bytes": GIB // 2, "kernel_no_counter_bytes": GIB * 3 // 2, "cache_bytes": 7 * GIB, "unattributed_bytes": 2 * GIB, "missing": [], "agreement": "agree",
}
SWAP_FIXTURE: dict[str, object] = {"used_bytes": 3 * GIB, "capacity_bytes": 8 * GIB, "builds_ci_bytes": GIB,
                                   "outside_bytes": 2 * GIB, "reserve_share_percent": 50}


def step_text(element: ElementTree.Element, path: str) -> str:
    """Return the whole text of the Step cell at path; a waiting step keeps each word in its own span."""
    cell = element.find(path)
    assert cell is not None
    return "".join(cell.itertext())


def program_memory_fixture(*, started: bool, with_past: bool = True) -> program_memory_feed.ProgramMemoryView:
    compile_phase: program_memory_feed.ProgramMemoryPhase = {
        "state": "complete", "duration_ms": 8000, "segments": (((2000, GIB), (8000, 4 * GIB)),),
        "peak": (8000, 4 * GIB), "reserve_bytes": 5 * GIB, "above_half_reserve_ms": 2000,
    }
    test_phase: program_memory_feed.ProgramMemoryPhase = {
        "state": "sampling", "duration_ms": 6000, "segments": (((2000, GIB), (6000, 2 * GIB)),),
        "peak": (6000, 2 * GIB), "reserve_bytes": 3 * GIB, "above_half_reserve_ms": 0,
    }
    past: program_memory_feed.ProgramMemoryRun = {
        "compile": {**compile_phase, "duration_ms": 12000, "reserve_bytes": 6 * GIB,
                    "segments": (((2000, GIB), (4000, 7 * GIB)), ((10000, 4 * GIB), (12000, GIB))),
                    "peak": (4000, 7 * GIB), "above_half_reserve_ms": 4000},
        "test": {**test_phase, "state": "complete", "duration_ms": 10000, "reserve_bytes": 4 * GIB},
        "hana_crate_bands": ((8000, 12000),), "hana_compile_ms": 4000,
    }
    not_started: program_memory_feed.ProgramMemoryPhase = {
        "state": "not_started", "duration_ms": 0, "segments": (), "peak": None,
        "reserve_bytes": None, "above_half_reserve_ms": None,
    }
    return {
        "live": {"compile": compile_phase if started else {**compile_phase, "state": "sampling"},
                 "test": test_phase if started else not_started, "hana_crate_bands": (), "hana_compile_ms": 0},
        "past": past if with_past else None, "compile_reserve": {"bytes": 5 * GIB, "source": "live"},
        "test_reserve": ({"bytes": 3 * GIB, "source": "live"} if started else
                         {"bytes": 4 * GIB, "source": "past"} if with_past else None),
    }


def program_memory_row(memory: program_memory_feed.ProgramMemoryView) -> dict[str, object]:
    return {"key": "hana", "name": "Hana · nextest", "used": 4, "reserved": 1, "queued": True,
            "started_at": 390, "slot_folder": "hana-slot1", "slot_machine": "natedev", "program_memory": memory}


def aws_cost_fixture() -> dict[str, object]:
    """Supply the same bill days to the page checks and the browser proof."""
    def money(cents: int) -> str:
        return f"{cents // 100}.{cents % 100:02}"

    prices: tuple[tuple[str, int | None, int], ...] = (
        ("estimate", 525, 193), ("estimate", 903, 359), ("billed", 253, 169),
        ("billed", 306, 214), ("billed", 471, 359), ("billed", 397, 216),
        ("billed", 835, 276), ("billed", 1319, 347), ("billed", 1029, 342),
        ("billed", 617, 145), ("billed", 1422, 325), ("billed", 770, 42),
        ("billed", 687, 0), ("final", 367, 0), ("billed", 286, 0),
        ("billed", 260, 0), ("none", None, 0), ("billed", 0, 0),
    )
    days: list[dict[str, object]] = []
    no_run_costs = {19: 127, 18: 128, 17: 37}
    for number, (state, cents, count) in zip(range(19, 1, -1), prices, strict=True):
        start = int(datetime(2026, 11, number, 9, 7, tzinfo=UTC).timestamp())
        no_run = no_run_costs.get(number, min(cents or 0, 56)) if count else cents or 0
        unrecorded_compute = 424 if number == 7 else 0
        if number == 7:
            no_run -= 103 + unrecorded_compute
        work_cost = (cents or 0) - no_run if count else 0
        work: list[tuple[str | None, int, int]] = (
            [("integrate", 84, 152), ("test all", 35, 62), ("test named", 8, 13),
             ("test affected", 58, 91), ("test nightly", 1, 2)] if number == 19 else
            [(None, count, work_cost)] if count else [])
        hana: dict[str, object] | None = {
            "runs": 7, "cost_usd": "0.78", "per_run_usd": "0.07", "idle_usd": "0.30", "idle_seconds": 8575,
        } if number == 19 else {
            "runs": 0, "cost_usd": "1.03", "per_run_usd": "0.00", "idle_usd": "1.03", "idle_seconds": None,
        } if number == 7 else None
        runs: list[dict[str, object]] = []
        hana_work: list[tuple[str | None, int, int]] = [(None, 7, 48)] if number == 19 else []
        for kind, (job, jobs, cost) in [("gate", row) for row in work] + [("hana", row) for row in hana_work]:
            offset = len(runs)
            group_start = start - offset * 20
            cents_per_run, remainder = divmod(cost, jobs)
            east = job != "test all"
            region = "us-east-1" if east else "us-west-2"
            runs.extend({"id": f"{number}-{offset + index}", "start": group_start - index * 20,
                         "job": job, "unit": "report" if job == "integrate" else None,
                         "kind": kind, "region": region, "zone": region + "a" if job else None,
                         "machine": "c7i.8xlarge" if east else "m7a.8xlarge",
                         "mode": "Spot" if east else "On-Demand",
                         "seconds": index if kind == "hana" else 574,
                         "cost_usd": money(cents_per_run + int(index < remainder))}
                        for index in range(jobs))
        if number in (17, 18):
            split_start = int(datetime(2026, 11, 18, tzinfo=UTC).timestamp())
            runs[0].update(id="midnight", start=split_start - (60 if number == 17 else 0), seconds=60)
        days.append({
            "day": f"2026-11-{number:02}", "state": state,
            "amount_usd": money(cents) if cents is not None else None,
            "runs": count,
            "jobs": [{"job": job, "runs": jobs, "cost_usd": money(cost),
                      "per_run_usd": money((cost + jobs // 2) // jobs)} for job, jobs, cost in work],
            "hana": hana, "units": [
                {"unit": "report", "runs": 84, "cost_usd": "1.52"},
                {"unit": None, "runs": 109, "cost_usd": "2.46"},
            ] if number == 19 else [{"unit": None, "runs": count, "cost_usd": money(
                work_cost if count else 103)}] if count or number == 7 else [],
            "unrecorded_compute_usd": money(unrecorded_compute), "no_run_usd": money(no_run), "runs_list": runs,
        })
    return {"zone": "America/New_York", "read_at": int(datetime(2026, 11, 18, 18, 43, tzinfo=UTC).timestamp()),
            "next_read_at": int(datetime(2026, 11, 19, 17, 43, tzinfo=UTC).timestamp()), "read_error": None,
            "price_day": "2026-11-17", "records_start": "2026-11-02", "today": "2026-11-19",
            "limits": {"day_usd": "35.00", "month_usd": "400.00", "reserve_usd": "3.00"},
            "days": days, "kept": {"month_usd": "28.20", "items": [
                {"name": "Hana data disk", "month_usd": "27.20"},
                {"name": "shared storage for AWS runs", "month_usd": "1.00"}]}}


class MemoryHoverTests(unittest.TestCase):
    def test_build_limits_move_names_and_wait_reasons_to_hover(self) -> None:
        wait = limits_view.WaitGroup(limits_view.WaitWords("1 run waits for space.", "{count} runs wait for space."),
                                     ["Test <one>", "Test two"])
        waits = {"disk": wait}
        cell = limits_view.Cell(limit=2, used=1, runs=["Build <one>"], waits=waits)
        disk = limits_view.Metric("disk", 20, 31, "GiB", "minimum", waits)
        machine = limits_view.MachineLimits("In use", (disk,), waits)
        empty = limits_view.MachineLimits("", (), {})
        mac = limits_view.MachineLimits("On", (), {}, 1, "A Mac claim has no repository name that this page can show.")
        hana = limits_view.Cell(limit=2, used=2, gpu=limits_view.Metric("gpu", 0, 2), verify_used=2)
        section = limits_view.BuildLimits(machine, mac, empty,
            (limits_view.RepositoryRow("repo:example", natedev=cell),
             limits_view.RepositoryRow("hana", natedev=hana, mac=limits_view.Cell(waits=waits))), False, {})
        keys = ("limit:cell:natedev:repo:example", "limit:machine:natedev", "limit:metric:natedev:disk",
                "limit:cell:natedev:hana", "limit:machine:mac", "limit:cell:mac:hana")
        rendered, tooltip, heading, metric, hana_tip, mac_tip, mac_wait_tip = cast(list[str], self.page_expression("""(() => {
            renderBuildLimits(data.build_limits);
            return [$('build-limits-content').innerHTML, ...data.keys.map(key => pageTooltipHtml(key, data))];
        })()""", {"build_limits": snapshot_codec.snapshot_value(section), "keys": keys}, limits=True))
        table = ElementTree.fromstring(f"<root>{rendered}</root>")
        detail = table.find(f".//td[@data-tip='{keys[0]}']/span")
        assert detail is not None
        self.assertEqual([mark.get("class") for mark in detail.findall("span/{http://www.w3.org/2000/svg}svg")],
                         ["lucide lucide-loader-circle", "lucide lucide-hourglass"])
        self.assertEqual([mark.get("class") for mark in detail.findall("span/span")],
                         ["build-limit-mark free", "build-limit-mark line", None])
        self.assertEqual(detail.findtext("span[2]"), "1 of 2")
        self.assertNotIn("Test two", rendered)
        self.assertNotIn("wait for space", rendered)
        pending = table.find(f".//td[@data-tip='{keys[5]}']/span/span[@data-wait-count='2']")
        assert pending is not None
        self.assertEqual(pending.findtext("span"), "2")
        self.assertEqual([icon.get("class") for icon in pending.findall("{http://www.w3.org/2000/svg}svg")],
                         ["lucide lucide-hourglass"])
        gpu = table.find(f".//td[@data-tip='{keys[3]}']/span/span[@class='build-limit-chip build-limit-gpu']")
        assert gpu is not None
        self.assertEqual(gpu.findtext("span"), "0 of 2")
        self.assertEqual(len(table.findall(".//tbody//{http://www.w3.org/2000/svg}svg[@class='lucide lucide-microchip']")), 1)
        self.assertIsNone(table.find(".//thead//{http://www.w3.org/2000/svg}svg[@class='lucide lucide-microchip']"))
        self.assertIsNotNone(table.find(".//th[@data-tip='limit:machine:mac']"))
        for value in (tooltip, heading, metric, mac_wait_tip):
            column = ElementTree.fromstring(value)
            self.assertEqual(column.tag, "dl")
            self.assertIn("Waiting", [row.text for row in column.findall("dt")])
            self.assertIn("Because", [row.text for row in column.findall("dt")])
            self.assertIn("Test <one>, Test two", [row.text for row in column.findall("dd")])
            self.assertIn("2 runs wait for space.", [row.text for row in column.findall("dd")])
        self.assertIn("Build &lt;one&gt;", tooltip)
        self.assertNotIn("Build &lt;one&gt;", rendered)
        self.assertIn("Verify runs", hana_tip)
        self.assertIn("The GPU limit applies to tests for this repository on natedev.", hana_tip)
        self.assertIn("A Mac claim has no repository name that this page can show.", mac_tip)

    def test_decoded_work_names_match_the_table_chart_and_hover(self) -> None:
        fixture = test_memory_feed.MemoryFeedTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        page = fixture.caller_selection_page()
        expected = (
            (700001, "recorded cloud", "", "recorded cloud"),
            (50, "hana CI: nextest", "", "hana CI"),
            (301, "held caller", "", "held caller"),
            (302, "unregistered session", "", "unregistered session"),
            (304, "hana: nextest", "unregistered session", "hana: nextest"),
            (200, "recorded local", "", "recorded local"),
            (700002, "recorded cloud", "", "recorded cloud"),
            (303, "recorded slot", "", "recorded slot"),
        )
        self.assertEqual(page["reserved"], 11.0)
        self.assertEqual([row["pid"] for row in (*page["walk"], *page["waiters"])],
                         [pid for pid, *_ in expected])
        keys = [f"{kind}:{row.get('key')}" for kind, table in (("row", page["walk"]), ("wait", page["waiters"]))
                for row in table]
        self.assertEqual(len(set(keys)), len(expected))
        rendered = cast(list[str], self.page_expression(
            f"(renderWalk(data), [$('walk').innerHTML, ...{json.dumps(keys)}.map(key => tooltipHtml(key, data))])",
            cast(object, json.loads(memory_feed.page_json(page))), walk=True))
        table = ElementTree.fromstring(f"<root>{rendered[0]}</root>")
        for (pid, name, caller, title), key, hover, row in zip(expected, keys, rendered[1:],
                (*page["walk"], *page["waiters"]), strict=True):
            with self.subTest(pid=pid):
                cell = table.find(f".//tr[@data-tip='{key}']/th[@class='step']")
                chart = table.find(f".//div[@data-tip='{key}']")
                assert cell is not None and chart is not None
                unit = row["unit"] if row["unit"] != "—" and not row.get("ci_label") else ""
                # A row with no unit starts with the text before ": " in its name; the rest goes in Step.
                owner_name, _, step = name.partition(": ")
                self.assertEqual("".join(cell.itertext()), row["title"]["step"] if unit else step)
                self.assertEqual(chart.findtext(".//span[@class='name']"), unit or owner_name)
                owner = chart.find(".//span[@class='owner']")
                assert owner is not None
                self.assertEqual("".join(owner.itertext()), row.get("showrunner", "") if unit else "")
                tip = ElementTree.fromstring(hover)
                self.assertEqual("".join(tip[0].itertext()), title)
                if caller:
                    self.assertEqual(next(tip[index + 1].text for index, field in enumerate(tip)
                                          if field.text == "Caller"), caller)

    def test_the_small_text_shows_only_a_showrunner_that_differs_from_the_unit(self) -> None:
        walk = [{"name": "aws-unit: test affected", "unit": "aws-unit", "showrunner": "aws", "caller": "aws-unit",
                 "queued": True},
                {"name": "builds: gate", "unit": "builds", "showrunner": "builds", "queued": True},
                {"name": "hana CI: nextest", "unit": "hana", "showrunner": "hana", "ci_label": "hana CI · hana-mac-1"},
                {"kind": "mac-build", "name": "hana: nextest", "unit": "—", "showrunner": "hana"},
                {"kind": "cloud", "name": "held caller", "caller": "held caller"}]
        lines = self.page_expression("data.walk.map(row => queueRowLabels(row).owner)", {"walk": walk})
        self.assertEqual(lines, ["aws", "", "", "", ""])

    def test_a_mac_build_with_no_unit_starts_with_who_started_it(self) -> None:
        # bob, 2026-11-23: the launcher starts the row, the repository is the small text and the verb fills Step.
        runner = staff.StaffRow(staff.StaffId("builds"), "showrunner", "builds", staff.StaffRoot(),
                                current_session=staff.StaffId("builds"))
        names = staff_names.StaffNames(staff.StaffSnapshot((runner,)))
        setup = "globalThis.window=globalThis;" + staff_names.script(names.registry())[8:-9]
        mac = {"kind": "mac-build", "unit": "—", "showrunner": "—", "repository": "hana", "machine": "mac",
               "started_at": 100, "read_at": 190, "used": 1.5, "stage": {"kind": "MacBuilding"}}
        walk = [{**mac, "name": "terminal: build", "started_by": {"name": "terminal", "step": "build"}},
                {**mac, "name": "builds: test", "started_by": {"name": "builds", "step": "test"}}]
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "walk": walk, "waiters": []}
        rendered = cast(list[str], self.page_expression("(()=>{" + setup + "renderWalk(data);"
            + "return [$('walk').innerHTML, ...data.walk.map(row => tooltipHtml('row:'+row.key, data))];})()", data, walk=True))
        root = ElementTree.fromstring(f"<root>{rendered[0]}</root>")
        for index, (start, color, step) in enumerate((("terminal", None, "build"), ("builds", "role.showrunner", "test"))):
            with self.subTest(start=start):
                chart = root.find(f".//div[@data-run='fixture-run-{index}']")
                table = root.find(f".//tr[@data-tip='row:fixture-run-{index}']")
                assert chart is not None and table is not None
                name = chart.find(".//span[@class='name']")
                assert name is not None
                self.assertEqual("".join(name.itertext()), start)
                colored = name.find("span[@data-color-text]")
                self.assertEqual(colored.get("data-color-text") if colored is not None else None, color)
                self.assertEqual(chart.findtext(".//span[@class='owner']"), "hana")
                self.assertEqual(step_text(table, "th[@class='step']"), step)
                self.assertIn("<dt>Started by</dt>", rendered[index + 1])
                self.assertIn(start, rendered[index + 1].split("<dt>Started by</dt>", 1)[1].split("</dd>", 1)[0])

    def mac_starter_hover(self, extra: dict[str, object]) -> list[tuple[object, object]]:
        """Give the hover rows of one Mac build that a staff owner named "builds" started."""
        row = {"kind": "mac-build", "machine": "mac", "name": "builds: build", "unit": "—", "repository": "hana",
               "used": 1.5, "started_at": 100, "read_at": 190, "stage": {"kind": "MacBuilding"},
               "started_by": {"name": "builds", "step": "build"}, **extra}
        rows = cast(list[dict[str, object]], self.page_expression("tooltipRows('row:fixture-run-0', data)",
                                                                   {"read_at": 200, "walk": [row]}))
        return [(row["label"], row["value"]) for row in rows]

    def test_a_caller_that_started_a_mac_build_shows_once(self) -> None:
        rows = self.mac_starter_hover({"caller": "builds", "showrunner": "—"})
        self.assertEqual([row for row in rows if row[0] == "Started by"], [("Started by", "builds")])
        self.assertEqual([row for row in rows if row[1] == "builds"], [("Started by", "builds")])

    def test_a_showrunner_that_started_a_mac_build_shows_once(self) -> None:
        rows = self.mac_starter_hover({"showrunner": "builds"})
        self.assertEqual([row for row in rows if row[0] == "Started by"], [("Started by", "builds")])
        self.assertEqual([row for row in rows if row[1] == "builds"], [("Started by", "builds")])

    def test_queue_rows_show_unit_showrunner_step_and_one_running_target(self) -> None:
        # nate, 2026-11-21 and 2026-11-23: the unit starts the row, the showrunner is the small text under it,
        # Step comes after Verify, no Unit or Slot column, and a running row's Machine cell is one icon.
        unit = staff.StaffRow(staff.StaffId("builder"), "unit", "builder", staff.StaffRoot(), current_session=staff.StaffId("builder"))
        runner = staff.StaffRow(staff.StaffId("builds"), "showrunner", "builds", staff.StaffRoot(),
                                current_session=staff.StaffId("builds"))
        names = staff_names.StaffNames(staff.StaffSnapshot((unit, runner)))
        setup = "globalThis.window=globalThis;" + staff_names.script(names.registry())[8:-9]
        row = {"name": "builder: nextest hana", "unit": "builder", "showrunner": "builds", "caller": "impl3",
               "queued": True, "started_at": 100, "used": 1, "reserved": 0, "counted": 1, "machine": "natedev",
               "title": {"unit": "builder", "step": "nextest hana"}, "detail": "building tests",
               "verify": {"verb": "test", "call": "verify test hana", "step": "nextest", "total": 1, "position": 1}}
        ci = {"name": "hana CI", "unit": "", "showrunner": "builds", "ci": True, "admitted": True,
              "ci_label": "hana CI · runner", "ci_runners": [{"state": "busy", "job": "doctests"}],
              "started_at": 100, "used": 1, "reserved": 0, "counted": 1}
        guest = {"name": "builder: gate", "unit": "builder", "showrunner": "builds", "queued": True,
                 "started_at": 100, "used": 1, "reserved": 0, "counted": 1, "machine": "mac-linux",
                 "guest_run": {"target": "mac-linux", "attempt": "guest-attempt"},
                 "targets": ["natedev", "mac-linux", "hana-aws"], "runs_on": "natedev or mac-linux or hana-aws",
                 "title": {"unit": "builder", "step": "gate"}}
        native = {**guest, "name": "builder: mac gate", "machine": "mac", "guest_run": None,
                  "targets": ["mac", "natedev", "mac-linux", "hana-aws"], "runs_on": "Mac or natedev or mac-linux or hana-aws"}
        waiting = {"name": "builder: gate", "unit": "builder", "showrunner": "builds", "caller": "test",
                   "since": 100, "need": 1, "title": {"unit": "builder", "step": "test affected"},
                   "targets": ["natedev", "mac-linux", "hana-aws"]}
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "walk": [row, ci, guest, native], "waiters": [waiting]}
        rendered = cast(list[str], self.page_expression("(()=>{" + setup + "renderWalk(data);"
            + "return [$('walk').innerHTML, ...data.walk.map(row => tooltipHtml('row:'+row.key, data))];})()", data, walk=True))
        root = ElementTree.fromstring(f"<root>{rendered[0]}</root>")
        for selector, labels in (
            (".//thead/tr/th", ["Stage", "Done", "Dur", "Verify", "Step", "Pid", "Machine", "vCPU", "Tokens", "Moved"]),
            (".//tr[@class='queue-head']/th", ["#", "Done", "Waiting", "Verify", "Step", "Pid", "Machine", "vCPU", "Moved"]),
        ):
            headers = root.findall(selector)
            self.assertEqual([cell.text for cell in headers[:-1]], labels)
            self.assertEqual((headers[-1].get("class"), headers[-1].text), ("row-action", None))
        self.assertEqual(root.findall(".//*[@class='unit']") + root.findall(".//*[@class='slot']"), [])
        for key, verify, step in (("row:fixture-run-0", "test", "nextest hana"), ("row:fixture-run-2", "", "gate"),
                                  ("wait:fixture-wait-0", "", "test affected")):
            with self.subTest(key=key):
                chart = root.find(f".//div[@data-tip='{key}']")
                table = root.find(f".//tr[@data-tip='{key}']")
                assert chart is not None and table is not None
                self.assertEqual(chart.findtext(".//span[@class='name']/span[@data-color-text='role.unit-director']"), "builder")
                self.assertEqual(chart.findtext(".//span[@class='owner']/span[@data-color-text='role.showrunner']"), "builds")
                self.assertEqual(table.findtext("td[@class='verify']"), verify)
                self.assertEqual(step_text(table, "th[@class='step']"), step)
        for key, machine in (("row:fixture-run-0", "natedev"), ("row:fixture-run-2", "mac-linux"),
                             ("row:fixture-run-3", "mac")):
            with self.subTest(key=key):
                cell = root.find(f".//tr[@data-tip='{key}']/td[@class='machine']")
                assert cell is not None
                self.assertEqual([(icon.get("class"), icon.get("aria-label"), icon.get("data-machine")) for icon in cell],
                                 [("machine machine-icon run", machine, machine)])
        waiting_target = root.find(".//tr[@data-tip='wait:fixture-wait-0']/td[@class='machine']")
        assert waiting_target is not None
        self.assertEqual([(icon.get("class"), icon.get("aria-label"), icon.get("data-machine")) for icon in waiting_target],
                         [("machine machine-icon dim", name, name) for name in ("natedev", "mac-linux", "hana-aws")])
        # Each machine name in the hover shows in its machine color.
        def machines(*names: str) -> str:
            return " or ".join(f'<span class="machine" data-machine="{name}">{name}</span>' for name in names)

        self.assertIn(f"<dt>Runs on</dt><dd>{machines('mac-linux')}</dd>", rendered[3])
        self.assertIn(f"<dt>Named targets</dt><dd>{machines('natedev', 'mac-linux', 'hana-aws')}</dd>", rendered[3])
        self.assertIn(f"<dt>Named targets</dt><dd>{machines('Mac', 'natedev', 'mac-linux', 'hana-aws')}</dd>", rendered[4])
        self.assertEqual(root.findtext(".//div[@data-run='fixture-run-1']//span[@class='name']"), "hana CI")
        self.assertEqual(root.findtext(".//div[@data-run='fixture-run-1']//span[@class='owner']"), "")
        self.assertEqual(step_text(root, ".//tr[@data-tip='row:fixture-run-1']/th[@class='step']"), "")
        self.assertNotIn("impl3", rendered[0])
        self.assertNotIn("building tests", rendered[0])
        self.assertIn("<dt>Caller</dt><dd>impl3</dd>", rendered[1])
        self.assertIn("<dt>Detail</dt><dd>building tests</dd>", rendered[1])
        self.assertIn("<dt>Detail</dt><dd>doctests</dd>", rendered[2])

    def test_each_machine_has_its_icon_in_both_states(self) -> None:
        forms = {
            "natedev": (None, None, "🐧"),
            "mac": ("#machine-apple", None, None),
            "mac-linux": ("#machine-apple", "🐧", None),
            "aws": ("#machine-cloud", "🐧", None),
            "hana-aws": ("#machine-cloud", "🌸", None),
            "hana-gpu": ("#machine-cloud", "⚡", None),
        }
        rendered = self.page_expression(
            'data.states.flatMap(state => data.names.map(name => machineIcon(name, state))).join("")',
            {"states": ["run", "dim"], "names": list(forms)}, walk=True)
        icons = ElementTree.fromstring(f"<root>{rendered}</root>")
        self.assertEqual(len(icons), 2 * len(forms))
        for index, state in enumerate(("run", "dim")):
            for offset, (name, (symbol, badge, emoji)) in enumerate(forms.items()):
                with self.subTest(name=name, state=state):
                    icon = icons[index * len(forms) + offset]
                    self.assertEqual(icon.get("class"), f"machine machine-icon {state}")
                    self.assertEqual(icon.get("data-machine"), name)
                    self.assertEqual(icon.get("aria-label"), name)
                    self.assertEqual(icon.get("role"), "img")
                    self.assertEqual(icon.get("data-badge"), badge)
                    self.assertEqual(len(icon), 1)
                    self.assertEqual(icon[0].get("aria-hidden"), "true")
                    if emoji is None:
                        self.assertEqual(icon[0].tag, "svg")
                        self.assertEqual([use.get("href") for use in icon[0].findall("use")], [symbol])
                    else:
                        self.assertEqual(icon[0].get("class"), "machine-emoji")
                        self.assertEqual("".join(icon.itertext()), emoji)

    def role_setup(self) -> str:
        members = tuple(staff.StaffRow(staff.StaffId(name), "unit", name, staff.StaffRoot(),
                        current_session=staff.StaffId(name)) for name in ("builder", "helper"))
        names = staff_names.StaffNames(staff.StaffSnapshot(members))
        return "globalThis.window=globalThis;" + staff_names.script(names.registry())[8:-9]

    def test_target_fallback_keeps_holder_text_and_colors_only_staff_names(self) -> None:
        for holder in ("nate", "builder", "<unknown>"):
            for role_label in ("", "—"):
                with self.subTest(holder=holder, role_label=role_label):
                    rows = [{"target": "Mac", "state": "Held", "until": "", "since": "",
                             "holder": holder, "unit": role_label, "showrunner": role_label}]
                    body = str(self.page_expression("(()=>{" + self.role_setup()
                        + "renderTargets(data.targets);return $('targets').innerHTML;})()", {"targets": rows}, walk=True))
                    owner = ElementTree.fromstring("<root>" + body + "</root>").find(".//div[@class='target-owner']")
                    assert owner is not None
                    self.assertEqual("".join(owner.itertext()), "Holder " + holder)
                    self.assertEqual(len(owner.findall("span")), int(holder == "builder"))

    def test_priority_tooltips_color_each_name_before_joining(self) -> None:
        entries = [{"unit": name} for name in ("builder", "helper", "<unknown>")]
        # One bump line for a unit entry and one for a run entry.
        lines = ['builder (natemerge: "natemerge can bump at will")',
                 'helper · nextest (one run) (nate (plain shell): "typed by nate in a plain shell")']
        data = {"unmatched_priority_entries": entries, "unmatched_priority_lines": lines,
                "walk": [{"name": "Compile", "priority_entries": entries, "priority_lines": lines, "queued": True}]}
        for key in ("walkhead", "row:fixture-run-0"):
            with self.subTest(key=key):
                body = str(self.page_expression("(()=>{" + self.role_setup()
                    + "return tooltipHtml(" + json.dumps(key) + ",data);})()", data, walk=True))
                grid = ElementTree.fromstring(body)
                priority = next(grid[index + 1] for index, cell in enumerate(grid) if cell.text == "Priority")
                self.assertEqual("".join(priority.itertext()), "builder, helper, <unknown>")
                self.assertEqual([span.text for span in priority.findall("span")], ["builder", "helper"])
                self.assertEqual(["".join(grid[index + 1].itertext()) for index, cell in enumerate(grid)
                                  if cell.text == "Bump"], lines)

    def test_bump_notices_use_the_unit_color_and_escape_the_detail(self) -> None:
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "bump_notices": [
            {"unit": "builder", "detail": "saved at 16:30 PST, waiting for the queue"},
            {"unit": "", "detail": "not applied: <reason>"},
        ]}
        body, shown, hidden = cast(tuple[str, bool, bool], self.page_expression("(() => {" + self.role_setup()
            + "renderWalk(data);const notices = $('bump-notices');"
            + "const body = notices.innerHTML, shown = !notices.hidden;"
            + "renderWalk({...data, bump_notices: []});return [body, shown, notices.hidden];})()", data, walk=True))
        self.assertTrue(shown)
        self.assertTrue(hidden)
        notices = ElementTree.fromstring(f"<root>{body}</root>")
        self.assertEqual([notice.tag for notice in notices], ["div", "div"])
        self.assertEqual("".join(notices[0].itertext()), "Bump for builder saved at 16:30 PST, waiting for the queue")
        self.assertEqual(notices[0][0].get("data-color-text"), "role.unit-director")
        self.assertEqual("".join(notices[1].itertext()), "Bumps not applied: <reason>")

    def test_bump_mark_is_before_the_action_and_shows_the_saved_move(self) -> None:
        move = {"kind": "bump", "who": "nate <test>", "from_place": 31, "to_place": 1, "at": "12:22 PST"}
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200,
                "walk": [{"name": "phases", "queued": True, "last_move": move}],
                "waiters": [{"name": "report", "since": 100, "need": 1, "place": 2, "last_move": move},
                            {"name": "fps", "since": 100, "need": 1, "place": 3}],
                "held_places": [{"kind": "held", "position": 4, "unit": "queue", "kept_until": "22:37 PST"}]}
        rendered, running_tip, waiting_tip = cast(tuple[str, str, str], self.page_expression(
            "(renderWalk(data), [$('walk').innerHTML, tooltipHtml('moved:row:fixture-run-0', data),"
            + " tooltipHtml('moved:wait:fixture-wait-0', data)])", data, walk=True))
        table = ElementTree.fromstring(f"<root>{rendered}</root>").find(".//table")
        assert table is not None
        self.assertTrue(all([cell.get("class") for cell in row[-2:]] == ["moved", "row-action"]
                            for row in table.findall(".//tr")))
        self.assertEqual(table.findtext("./thead/tr/th[@class='moved']"), "Moved")
        self.assertEqual(table.findtext(".//tr[@class='queue-head']/th[@class='moved']"), "Moved")
        self.assertEqual(table.findtext(".//tr[@class='held']/td[@class='moved']"), "—")
        self.assertEqual(table.findtext(".//tr[@data-tip='wait:fixture-wait-1']/td[@class='moved']"), "—")
        for key, hover, place in (("row:fixture-run-0", running_tip, "place 1"),
                                  ("wait:fixture-wait-0", waiting_tip, "place 2")):
            with self.subTest(key=key):
                mark = table.find(f".//td[@class='moved']/span[@data-tip='moved:{key}']")
                assert mark is not None
                self.assertEqual(mark.findtext("span"), "nate <test>")
                fields = ElementTree.fromstring(hover)
                self.assertEqual(fields.get("class"), "tip")
                self.assertEqual([cell.text for cell in fields.findall("dt")], ["Bump", "Moved by", "From", "To", "At"])
                self.assertEqual([cell.text for cell in fields.findall("dd")],
                                 [place, "nate <test>", "place 31", "the top", "12:22 PST"])

    def test_tooltip_colors_only_fields_that_name_staff(self) -> None:
        data = {"waiters": [{"name": "Compile", "unit": "builder", "showrunner": "helper",
                "runner": "worker", "worker": "builder", "kind": "build-step",
                "title": {"unit": "builder", "step": "Compile"}, "because": "builder", "runs_on": "builder", "why": "builder"}]}
        body = str(self.page_expression("(()=>{" + self.role_setup()
            + "return tooltipHtml('wait:fixture-wait-0',data);})()", data, walk=True))
        grid = ElementTree.fromstring(body)
        because = next(grid[index + 1] for index, cell in enumerate(grid) if cell.text == "Because")
        self.assertEqual((because.text, list(because)), ("builder", []))
        # Runs on names machines, so its words take the machine colors and never a staff color.
        runs_on = next(grid[index + 1] for index, cell in enumerate(grid) if cell.text == "Runs on")
        self.assertEqual([(span.get("class"), span.text) for span in runs_on.iter("span")], [("machine", "builder")])
        for label, name in (("Showrunner", "helper"), ("Worker", "builder")):
            value = next(grid[index + 1] for index, cell in enumerate(grid) if cell.text == label)
            self.assertEqual(value.findtext("span"), name)
        self.assertEqual(grid[0].findtext("span"), "builder")

    def test_builds_page_has_no_navigation_or_framing_restriction(self) -> None:
        page = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        self.assertIn("<title>Builds</title>", page)
        self.assertIn('<h1 class="page-title">Builds</h1>', page_frame.template(page, "Builds", "builds"))
        elements: list[tuple[str, dict[str, str]]] = []

        def collect(tag: str, attrs: list[tuple[str, str | None]]) -> None:
            elements.append((tag, {key: value or "" for key, value in attrs}))

        parser = HTMLParser()
        parser.handle_starttag = collect
        parser.feed(page)
        self.assertNotIn("nav", [element[0] for element in elements])
        for tag, attributes in elements:
            if tag == "a" and not attributes.get("href", "").startswith("#"):
                self.assertEqual(attributes.get("target"), "_top")
            if tag == "meta":
                self.assertNotEqual(attributes.get("http-equiv", "").lower(), "x-frame-options")
                self.assertNotIn("frame-ancestors 'none'", attributes.get("content", "").lower())

    def test_time_cells_and_tooltip_heads_use_short_times_without_a_wait_prefix(self) -> None:
        for elapsed, expected in ((0, "<1 min"), (29, "<1 min"), (30, "1 min"),
                                  (60, "1 min"), (2580, "43 min"), (3600, "1 h 0 min"), (86400, "1 d 0 h"), (183600, "2 d 3 h")):
            with self.subTest(elapsed=elapsed):
                data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200_000,
                    "walk": [{"name": "running", "queued": True, "started_at": 200_000 - elapsed,
                              "used": 1, "reserved": 0, "counted": 1}],
                    "waiters": [{"name": "queued", "since": 200_000 - elapsed, "detail": "fixture",
                                 "pid": 200, "need": 1, "reason": "free memory"}]}
                rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
                root = ElementTree.fromstring(f"<root>{rendered}</root>")
                self.assertEqual(root.findtext(".//tr[@data-tip='row:fixture-run-0']/td[@class='duration']"), expected)
                self.assertEqual(root.findtext(".//tr[@data-tip='wait:fixture-wait-0']/td[@class='duration']"), expected)
                for key in ("row:fixture-run-0", "wait:fixture-wait-0"):
                    html = str(self.page_expression(f"tooltipHtml('{key}', data)", data, walk=True))
                    head = ElementTree.fromstring(html)
                    self.assertEqual(head[0].get("class"), "title")
                    state = "waiting" if key.startswith("wait:") else "running"
                    self.assertEqual(head[1].text, f"{state} {expected}")
                    self.assertNotIn("ago", html)
                for kind, collection in (("row", "walk"), ("wait", "waiters")):
                    moved = self.page_expression(
                        f"(() => {{const row=data.{collection}[0]; row.key='same-step'; "
                        + f"data.{collection}.unshift({{...row,key:'new-step',name:'new step'}}); "
                        + f"return tooltipRows('{kind}:same-step',data)[0].label;}})()", data)
                    self.assertEqual(moved, "running" if kind == "row" else "queued")

    def test_queued_titles_share_the_header_row_without_moving_the_room_row(self) -> None:
        for units, expected in (([], "Queued, in order of submission"),
                                (["research"], "Queued, research first, then in submission order"),
                                (["research", "cables"], "Queued, research followed by cables and then in submission order"),
                                (["a<&", "b"], "Queued, a<& followed by b and then in submission order")):
            with self.subTest(units=units):
                data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "queued_priority_units": units,
                    "walk": [{"name": "running", "queued": True}, {"name": "second running", "queued": True}],
                    "waiters": [{"name": "first", "since": 100, "detail": "research", "pid": 200}]}
                running_title, rendered = cast(list[str], self.page_expression(
                    "(renderWalk(data), [$('walk-title').textContent, $('walk').innerHTML])", data, walk=True))
                self.assertEqual(running_title, "Running, in start order (2)")
                root = ElementTree.fromstring(f"<root>{rendered}</root>")
                chart = root.find("./div[@class='walk-chart']")
                assert chart is not None
                header = chart.find("./div[@class='walk-chart-head queue-head']")
                assert header is not None
                title = header.find("./span[@class='queue-title section-head']")
                assert title is not None
                self.assertEqual("".join(title.itertext()), f"{expected} (1)")
                self.assertEqual(title.get("data-tip"), "queuehead")
                room = chart.find("./div[@class='wrow room']")
                assert room is not None
                children = list(chart)
                self.assertEqual(children.index(room) + 1, children.index(header))
                self.assertEqual(children[children.index(header) + 1].get("data-tip"), "wait:fixture-wait-0")
                table_rows = root.findall(".//tbody/tr")
                self.assertEqual([row.get("class") for row in table_rows],
                                 ["tools", "", "", "room", "queue-head", "wait wait-first"])
                self.assertEqual(root.findtext(".//tr[@data-tip='wait:fixture-wait-0']/td[@class='place']"), "1")
                self.assertEqual(root.findtext(".//div[@data-tip='wait:fixture-wait-0']//span[@class='name']"), "first")
                self.assertEqual(step_text(root, ".//tr[@data-tip='wait:fixture-wait-0']/th[@class='step']"), "")

    def test_running_and_queued_names_color_only_the_leading_unit(self) -> None:
        names = staff_names.StaffNames(staff.StaffSnapshot((
            staff.StaffRow(staff.StaffId("builder"), "unit", "builder", staff.StaffRoot(),
                           current_session=staff.StaffId("builder")),
            staff.StaffRow(staff.StaffId("builds"), "showrunner", "builds", staff.StaffRoot(),
                           current_session=staff.StaffId("builds")),
        )))
        setup = "globalThis.window=globalThis;" + staff_names.script(names.registry())[8:-9]
        step = {"name": "builder builds", "unit": "builder", "showrunner": "builds"}
        owned_step = {**step, "name_parts": [{"kind": "staff", "text": "builder"},
                                            {"kind": "text", "text": " builds"}]}
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200,
                "walk": [{**owned_step, "queued": True},
                         {"name": "builder builds", "queued": True},
                         {**step, "queued": True, "name_parts": [{"kind": "text", "text": "builder builds"}]},
                         {**owned_step, "kind": "mac-build", "machine": "mac", "used": 1.5}],
                "waiters": [{**step, "since": 100}]}
        rendered = self.page_expression("(()=>{" + setup + "renderWalk(data);return $('walk').innerHTML;})()",
                                        data, walk=True)
        root = ElementTree.fromstring(f"<root>{rendered}</root>")
        self.assertEqual(root.findall(".//table//*[@class='unit']"), [])
        for key in ("row:fixture-run-0", "row:fixture-run-2", "row:fixture-run-3", "wait:fixture-wait-0"):
            with self.subTest(row=key):
                label = root.find(f".//div[@data-tip='{key}']/span[@class='row-label']")
                assert label is not None
                name = label.find("./span/span[@class='name']")
                assert name is not None
                self.assertEqual(len(name), 1)
                self.assertEqual(name[0].get("data-color-text"), "role.unit-director")
                self.assertEqual(name[0].text, "builder")
                self.assertIsNone(name[0].tail)
                owner = label.find("./span[@class='owner']/span")
                assert owner is not None
                self.assertEqual(owner.get("data-color-text"), "role.showrunner")
                self.assertEqual(owner.text, "builds")
        name = root.find(".//div[@data-run='fixture-run-1']/span/span/span[@class='name']")
        assert name is not None
        self.assertEqual(len(name), 0)
        self.assertEqual(name.text, "builder builds")

    def test_verify_column_and_tooltip_use_the_recorded_call(self) -> None:
        data: dict[str, object] = {"total": 64, "cap": 48, "taken": 4, "read_at": 200,
            "walk": [{"name": "demo nextest", "queued": True, "started_at": 100, "used": 1,
                      "reserved": 0, "counted": 1, "verify": {"verb": "final", "step": "nextest",
                          "call": "verify final demo", "position": 2, "total": 2}}],
            "waiters": [{"name": "demo clippy", "since": 100, "need": 1, "reason": "free memory",
                         "verify": {"verb": "example", "step": "clippy", "position": 1, "total": 1,
                                    "call": "verify example demo · 14 filters"}},
                        {"name": "rust-analyzer", "since": 100, "need": 1, "reason": "free memory"},
                        {"name": "demo check", "unit": "builder", "detail": "checkout",
                         "title": {"unit": "builder", "step": "demo check"},
                         "since": 100, "need": 1,
                         "why": "the build in the same Cargo folder",
                         "reason": "waits for the build in the same Cargo folder",
                         "verify": {"verb": "check", "step": "check", "position": 1, "total": 1,
                                    "call": "verify check demo"}},
                        {"kind": "build-step", "name": "demo build", "unit": "builder",
                         "title": {"unit": "builder", "step": "demo build"},
                         "since": 100, "need": 0, "reason": "waits for memory",
                         "verify": {"verb": "build", "step": "build", "position": 1, "total": 1,
                                    "call": "verify build demo"}}]}
        rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
        root = ElementTree.fromstring(f"<root>{rendered}</root>")
        self.assertEqual([cell.text for cell in root.findall(".//th[@class='verify']")], ["Verify", "Verify"])
        for key, verb, step, call in (
            ("row:fixture-run-0", "final", "nextest (2 of 2)", "verify final demo"),
            ("wait:fixture-wait-0", "example", "clippy", "verify example demo · 14 filters"),
            ("wait:fixture-wait-1", "", "", ""),
            ("wait:fixture-wait-2", "check", "check", "verify check demo"),
            ("wait:fixture-wait-3", "build", "build", "verify build demo"),
        ):
            with self.subTest(key=key):
                self.assertEqual(root.findtext(f".//tr[@data-tip='{key}']/td[@class='verify']"), verb)
                tip = str(self.page_expression(f"tooltipHtml('{key}', data)", data, walk=True))
                grid = ElementTree.fromstring(tip)
                labels = {cell.text: "".join(grid[index + 1].itertext()) for index, cell in enumerate(grid) if cell.tag == "dt"}
                if verb:
                    self.assertEqual(labels["Step"], step)
                    self.assertEqual(labels["Call"], call)
                    self.assertEqual(grid[-2].text, "Call")
                    self.assertEqual(grid[-1].get("class"), "call group")
                else:
                    self.assertNotIn("Step", labels)
                    self.assertNotIn("Call", labels)
        self.assertEqual(root.findtext(".//div[@data-tip='wait:fixture-wait-2']//span[@class='wait-note']"),
                         "waits for the build in the same Cargo folder")
        for key, name in (("wait:fixture-wait-2", "demo check"), ("wait:fixture-wait-3", "demo build")):
            row = root.find(f".//tr[@data-tip='{key}']")
            assert row is not None
            self.assertEqual(step_text(row, "th[@class='step']"), name)
            self.assertEqual(root.findtext(f".//div[@data-tip='{key}']//span[@class='name']"), "builder")

    def test_a_waiting_row_note_shows_only_its_reason_and_the_hover_shows_the_job_start(self) -> None:
        started = "10:14 PST · 1 h 10 min ago"
        held = "natedev is held by fps for Phase 3 timing; waiting for release"
        data: dict[str, object] = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "walk": [],
            "waiters": [{"name": "hana CI: build", "since": 100, "need": 1, "reason": "free memory",
                         "job_start": started},
                        {"name": "hana CI: test", "since": 110, "need": 1, "why": "build hold", "reason": held,
                         "job_start": started},
                        {"name": "claude gate", "since": 120, "need": 1, "why": "machine choice",
                         "reason": "waits for a machine choice"}]}
        rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
        root = ElementTree.fromstring(f"<root>{rendered}</root>")
        self.assertEqual(root.findtext(".//div[@data-tip='wait:fixture-wait-2']//span[@class='wait-note']"),
                         "waits for a machine choice")
        self.assertNotIn("job started", str(rendered))
        self.assertNotIn(started, str(rendered))
        for key, note in (("wait:fixture-wait-0", None), ("wait:fixture-wait-1", held)):
            with self.subTest(key=key):
                self.assertEqual(root.findtext(f".//div[@data-tip='{key}']//span[@class='wait-note']"), note)
                grid = ElementTree.fromstring(str(self.page_expression(f"tooltipHtml('{key}', data)", data, walk=True)))
                labels = {cell.text: "".join(grid[index + 1].itertext()) for index, cell in enumerate(grid) if cell.tag == "dt"}
                self.assertEqual(labels["Job start"], started)
                self.assertNotIn("Order", labels)

    def test_cargo_and_gate_tooltips_show_a_call_without_verify(self) -> None:
        data = {"walk": [{"name": "demo nextest", "queued": True, "used": 1,
                          "reserved": 0, "call": "cargo nextest run -p demo"}],
                "waiters": [{"kind": "build-step", "name": "demo test all", "need": 0,
                             "call": "gate.py --tests-only"}]}
        for key, call in (("row:fixture-run-0", "cargo nextest run -p demo"),
                          ("wait:fixture-wait-0", "gate.py --tests-only")):
            with self.subTest(key=key):
                tip = ElementTree.fromstring(str(self.page_expression(f"tooltipHtml('{key}', data)", data, walk=True)))
                self.assertEqual(tip[-2].text, "Call")
                self.assertEqual("".join(tip[-1].itertext()), call)
                self.assertEqual([word.text for word in tip[-1].findall("div/span")], call.split())
                self.assertEqual(tip[-1].get("class"), "call group")
                self.assertNotIn("Step", [cell.text for cell in tip if cell.tag == "dt"])

    def test_unavailable_memory_keeps_the_verify_tooltip_fields(self) -> None:
        verify = {"verb": "final", "step": "nextest", "position": 2, "total": 2, "call": "verify final demo"}
        row = {"name": "demo nextest", "about": "Checks the workspace targets and runs the selected tests", "changes": "no branch", "verify": verify}
        data = {"walk": [{**row, "kind": "mac-build", "used": {"kind": "MacMemoryUnavailable"}}],
                "waiters": [{**row, "head_room": {"unavailable": True}}]}
        for key in ("row:fixture-run-0", "wait:fixture-wait-0"):
            with self.subTest(key=key):
                rows = cast(list[dict[str, object]], self.page_expression(f"tooltipRows('{key}', data)", data))
                self.assertEqual([(row["label"], row["value"]) for row in rows],
                                 [("demo nextest", "unavailable"), ("", "Checks the workspace targets and runs the selected tests"),
                                  ("Changes", "no branch"), ("Step", "nextest (2 of 2)"),
                                  ("Call", "verify final demo")])

    def test_the_done_column_uses_each_counter_and_preserves_unknown_progress(self) -> None:
        for phase, unit, done, total, percent, expected in (
            ("building", "units", 7, 10, 70, "70.0%"),
            ("testing", "modules", 722, 727, None, "99.3%"),
            ("testing", "tests", 0, 12, 0, "0.0%"),
            ("testing", "modules", 12, 12, 100, "100.0%"),
            ("testing", "modules", 11, 12, 100, "99.9%"),
        ):
            counter: dict[str, object] = {"kind": "RowProgress", "phase": phase, "unit": unit,
                                         "done": done, "total": total}
            if percent is not None:
                counter["percent"] = percent
            with self.subTest(counter=counter):
                data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "waiters": [],
                        "walk": [{"name": "counter", "queued": True, "progress": counter},
                                 {"name": "unknown", "queued": True, "progress": {"kind": "NoProgress"}}]}
                rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
                root = ElementTree.fromstring(f"<root>{rendered}</root>")
                row = root.find(".//tr[@data-tip='row:fixture-run-0']")
                assert row is not None
                self.assertEqual(row[0].text, phase)
                self.assertEqual(row[1].get("class"), "done")
                self.assertEqual(row[1].text, expected)
                self.assertEqual(root.findtext(".//tr[@data-tip='row:fixture-run-1']/td[@class='done']"), "—")

    def test_running_and_waiting_steps_each_have_an_accessible_new_item_button(self) -> None:
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200,
                "walk": [{"name": "Running <gate>", "queued": True},
                         {"name": "Mac nextest", "kind": "mac-build"},
                         {"name": "Mac offline", "kind": "mac-unavailable"},
                         {"name": "hana CI", "ci": True, "admitted": True}],
                "waiters": [{"name": "Queued gate", "since": 100}]}
        rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
        root = ElementTree.fromstring(f"<root>{rendered}</root>")
        buttons = root.findall(".//button[@class='row-add']")
        self.assertEqual([button.get("aria-label") for button in buttons],
                         ["New item for Running <gate>", "New item for Mac nextest",
                          "New item for hana CI", "New item for Queued gate"])
        rows = root.findall(".//tr")
        self.assertEqual([row[-1].find("button") for row in rows if row[-1].find("button") is not None], buttons)
        self.assertTrue(all(row[-1].get("class") == "row-action" for row in rows))
        for button in buttons:
            icon = button.find("{http://www.w3.org/2000/svg}svg")
            assert icon is not None
            self.assertEqual(icon.get("class"), "lucide lucide-plus")

    def test_new_item_evidence_uses_the_readings_memory_and_tooltip(self) -> None:
        limits = new_item_dialog.BUILD_EVIDENCE_LIMITS
        long_title = "t" * (limits["title"] - 1) + "🚀 shortened"
        long_caption = "c" * (limits["text"] - 1) + "🚀 shortened"
        cases = ((31, 31.0, "Builds gate", "Test all modules"),
                 ("unavailable", None, "Builds gate", "Test all modules"),
                 (31, 31.0, long_title, long_caption))
        for cap, expected, title, caption in cases:
            with self.subTest(cap=cap, title_length=len(title)):
                step = {"name": title, "queued": True, "showrunner": "builds", "started_at": 100,
                        "used": 3, "reserved": 2, "about": caption, "changes": "main"}
                rendered = cast(dict[str, object], self.page_expression(
                    "(() => {const capture = buildStepEvidence('row:fixture-run-0', data, data.walk[0], "
                    + "[{label:'Pid',value:'123'}], tooltipRows('row:fixture-run-0', data), "
                    + json.dumps(limits) + "); return {evidence:capture.evidence, preview:tipRowsHtml(capture.clippedTip)};})()",
                    {"cap": cap, "read_at": 200, "walk": [step]}, walk=True))
                evidence = new_item_dialog.read_evidence(rendered["evidence"])
                assert isinstance(evidence, new_item_dialog.BuildStepEvidence)
                self.assertEqual(evidence.step_id, "fixture-run-0")
                self.assertEqual(evidence.owner, "builds")
                self.assertEqual(evidence.captured_at, "1970-01-01T00:03:20.000Z")
                self.assertEqual(evidence.title, title[:limits["title"]])
                self.assertEqual(evidence.tip_title, evidence.title)
                self.assertEqual(evidence.cells, (new_item_dialog.BuildCell("Pid", "123"),))
                self.assertEqual(evidence.memory.queue_cap_gib, expected)
                self.assertEqual(evidence.memory.free_for_next_gib, 18.0)
                self.assertEqual(evidence.memory.cap_text, "31.0 GiB" if expected is not None else "unavailable")
                self.assertEqual(evidence.memory.free_text, "18.0 GiB")
                self.assertIn(new_item_dialog.MemorySegment("Build tools", 2, new_item_dialog.MemoryTone.TOOLS, "2.0 GiB"),
                              evidence.memory.segments)
                expected_entry = (new_item_dialog.TipField("Work", caption) if len(caption) <= 30
                                  else new_item_dialog.TipDetail(caption[:limits["text"]]))
                self.assertIn(expected_entry, evidence.tip)
                self.assertIn(new_item_dialog.TipField("Changes", "main"), evidence.tip)
                self.assertIn(new_item_dialog.TipField("Uses", "3.0 GiB"), evidence.tip)
                preview = ElementTree.fromstring(cast(str, rendered["preview"]))
                self.assertIn(caption[:limits["text"]], "".join(preview.itertext()))

    def test_new_item_evidence_includes_the_values_from_both_program_memory_charts(self) -> None:
        row = program_memory_row(program_memory_fixture(started=True))
        rendered = cast(dict[str, object], self.page_expression(
            "(() => {const capture = buildStepEvidence('row:hana', data, data.walk[0], [], "
            + "tooltipRows('row:hana', data), " + json.dumps(new_item_dialog.BUILD_EVIDENCE_LIMITS)
            + "); return {evidence:capture.evidence, preview:tipRowsHtml(capture.clippedTip)};})()",
            {"read_at": 400, "walk": [row]}, walk=True))
        evidence = new_item_dialog.read_evidence(rendered["evidence"])
        assert isinstance(evidence, new_item_dialog.BuildStepEvidence)
        expected = (
            ("Compile · Live last sample", "4.0 GiB"),
            ("Compile · Past peak", "7.0 GiB"),
            ("Compile · Past >½ reserve", "4 s"),
            ("Compile · Past hana crate", "4 s"),
            ("Test · Live sample", "2.0 GiB"),
            ("Test · Past peak", "2.0 GiB"),
            ("Test · Past >½ reserve", "0 s"),
        )
        self.assertEqual(evidence.tip[-len(expected):], tuple(new_item_dialog.TipField(*pair) for pair in expected))
        preview = ElementTree.fromstring(cast(str, rendered["preview"]))
        self.assertEqual([element.text for element in list(preview)[-2 * len(expected):]],
                         [text for pair in expected for text in pair])
        self.assertIsNone(preview.find(".//svg"))

    def test_a_token_wait_is_the_stage_of_its_running_row(self) -> None:
        row = memory_tests.ProgressKeepAsideTests.reservation("nextest", need=4 * GIB)
        memory = admission_records.MeasuredMemory(9 * GIB, anonymous=admission_records.AnonymousRunUse(GIB))
        counter = progress.parse_progress("Finished `test` profile in 29s\n" + progress.token_wait_line(4) + "\n")
        adjusted = gate.progress_memory(row, memory, gate.AttributedStepUse(memory.anonymous, True), counter,
            progress.NoMatchingProfile("no profile"), admission_policy_fixture())
        inputs = memory_tests.ProgressKeepAsideTests.inputs((row,), (adjusted,))
        stage = memory_feed.row_progress(inputs.running, progress.TOKEN_WAIT_REASON)
        self.assertEqual(stage, {"kind": "TokenWaitProgress", "stage": "waiting for jobserver tokens", "tokens": 4})
        running: dict[str, object] = {"name": "hana nextest", "queued": True, "progress": stage}
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "waiters": [], "walk": [running]}
        rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
        root = ElementTree.fromstring(f"<root>{rendered}</root>")
        cells = root.find(".//tr[@data-tip='row:fixture-run-0']")
        assert cells is not None
        # The Stage column shows a short word; the tooltip gives the full reason.
        self.assertEqual(cells[0].text, "waiting")
        self.assertEqual(cells[1].text, "—")
        running.update(used=1, reserved=0, counted=1)
        grid = ElementTree.fromstring(cast(str, self.page_expression("tooltipHtml('row:fixture-run-0', data)", data, walk=True)))
        fields = [(cell.text, grid[index+1].text) for index, cell in enumerate(grid) if cell.tag == "dt"]
        self.assertIn(("Stage", "waiting for jobserver tokens"), fields)

    def test_the_running_table_shows_tokens_and_the_header_shows_the_pool(self) -> None:
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "waiters": [],
                "walk": [{"name": "hana nextest", "queued": True, "pid": 10, "tokens": 3, "progress": {"kind": "NoProgress"}},
                         {"name": "mac nextest", "queued": True, "pid": 11, "progress": {"kind": "NoProgress"}}]}
        rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
        root = ElementTree.fromstring(f"<root>{rendered}</root>")
        self.assertEqual(root.findtext(".//th[@class='tokens']"), "Tokens")
        self.assertEqual(root.findtext(".//tr[@data-tip='row:fixture-run-0']/td[@class='tokens']"), "3")
        self.assertEqual(root.findtext(".//tr[@data-tip='row:fixture-run-1']/td[@class='tokens']"), "—")
        gib = 1024 ** 3
        for pool, expected in (({"kind": "TokenPool", "total": 31, "free": 4, "memory_floor_bytes": 12 * gib, "paused": True},
                                [("Total", "31"), ("Available", "4")]),
                               ({"kind": "TokenPoolUnavailable", "reason": "stevie exited 1"}, [("Status", "unavailable")])):
            with self.subTest(pool=pool):
                header = self.page_expression("(renderWalk(data), $('walk-tokens').innerHTML)",
                                              {**data, "tokens": pool}, walk=True)
                fields = ElementTree.fromstring(f"<dl>{header}</dl>")
                self.assertEqual(list(zip((row.text for row in fields.findall("dt")),
                                          (row.text for row in fields.findall("dd")), strict=True)), expected)
        tip = cast(list[str], self.page_expression("tipText('tokens',data)", {"token_counts": "steve cannot be asked.",
            "tokens": {"kind": "TokenPool", "total": 31, "free": 0, "memory_floor_bytes": 12 * gib, "paused": True}}))
        self.assertEqual(tip, ["Tokens", "Total  31\nAvailable  0\nJob capacity  one extra job per token\n"
                              + "Shared by  natedev cargo and nextest\nCI pool  separate\n"
                              + "Pause below  12.0 GiB\nPool  paused\nPer-build counts  unavailable\n"
                              + "Because  steve cannot be asked"])
        unavailable = self.page_expression("tipText('tokens',data)",
            {"tokens": {"kind": "TokenPoolUnavailable", "reason": "stevie exited 1"}})
        self.assertEqual(unavailable, ["Tokens  unavailable", ""])

    def test_the_mac_memory_note_follows_all_tooltip_fields(self) -> None:
        data = {"read_at": 200, "walk": [{"kind": "mac-build", "machine": "mac", "name": "hana nextest",
            "used": 3.5, "started_at": 100, "read_at": 190, "stage": {"kind": "MacTesting"},
            "progress": {"kind": "RowProgress", "phase": "testing", "unit": "tests", "done": 7, "total": 10, "percent": 70}}]}
        rows = cast(list[dict[str, object]], self.page_expression("tooltipRows('row:fixture-run-0', data)", data))
        self.assertEqual([(row["label"], row["value"]) for row in rows], [
            ("hana nextest", "running 2 min"), ("Runs on", "Mac"), ("Uses", "3.5 GiB"),
            ("Reading age", "<1 min"), ("In queue cap", "no"), ("Testing", "7 of 10 tests"), ("Done", "70.0%")])

    def test_a_queued_run_with_an_empty_part_list_shows_its_own_use(self) -> None:
        data = {"read_at": 400, "walk": [{"name": "fixture: gate", "unit": "fixture", "showrunner": "fixture-production",
            "started_at": 100, "used": 4.7, "reserved": 0.5, "counted": 5.2, "queued": True, "keep_text": "",
            "parts": list[dict[str, object]]()}]}
        rows = cast(list[dict[str, object]], self.page_expression("tooltipRows('row:fixture-run-0', data)", data))
        amounts = {row["label"]: row["value"] for row in rows}
        self.assertEqual(amounts["Uses"], "4.7 GiB")
        self.assertEqual(amounts["+ Build reserved"], "0.5 GiB")
        self.assertEqual(amounts["= In queue cap"], "5.2 GiB")

    def test_program_memory_draws_both_phases_with_separate_reserves_and_axes(self) -> None:
        row = program_memory_row(program_memory_fixture(started=True))
        html = self.page_expression("buildsTooltipHtml('row:hana', data)", {"read_at": 400, "walk": [row]}, walk=True)
        tip = ElementTree.fromstring(str(html))
        self.assertEqual(["".join(item.itertext()) for item in tip.findall("./dd/div[@class='program-key']/span")],
                         ["This run (live)", "Past run"])
        compile_phase, test_phase = tip.findall("./dd/section[@class='program-phase']")
        self.assertEqual(compile_phase.findtext("div/h2"), "Compile")
        self.assertEqual(test_phase.findtext("div/h2"), "Test")
        fields = [(cell.text, tip[index + 1].text) for index, cell in enumerate(tip) if cell.tag == "dt"]
        self.assertIn(("Elapsed", "8 s"), fields)
        self.assertIn(("Axis", "scaled to past run"), fields)
        self.assertEqual(compile_phase.findtext("./div[@class='program-times']/span[3]"), "12 s")
        self.assertEqual(test_phase.findtext("./div[@class='program-times']/span[3]"), "10 s")
        self.assertEqual(len(compile_phase.findall("./svg/polyline[@class='program-past-line']")), 2)
        self.assertEqual(len(compile_phase.findall("./svg/rect[@class='program-crate']")), 1)
        self.assertIn("hana crate", list(compile_phase.itertext()))
        self.assertIn("7.0 GiB peak", list(compile_phase.itertext()))
        self.assertIn("5.0 GiB reserved", list(compile_phase.itertext()))
        self.assertIn("3.0 GiB reserved", list(test_phase.itertext()))
        self.assertEqual([chart.get("x1") for chart in tip.findall(".//line[@class='program-marker']")],
                         ["238", "144.4"])
        self.assertEqual(fields[-3:], [("Live sample", "2.0 GiB"), ("Past peak", "2.0 GiB"), ("Past >½ reserve", "0 s")])
        self.assertFalse(tip.findall(".//dl"))
        unavailable = {**row, "used": {"kind": "RunUseUnavailable", "reason": "memory.stat is missing"}}
        html = self.page_expression("buildsTooltipHtml('row:hana', data)",
                                    {"read_at": 400, "walk": [unavailable]}, walk=True)
        tip = ElementTree.fromstring(str(html))
        self.assertEqual(tip.findtext("./dd[@class='title']"), "unavailable")
        self.assertEqual([section.findtext("div/h2") for section in tip.findall("./dd/section")],
                         ["Compile", "Test"])
        self.assertEqual(len(tip.findall("./dd/section/svg")), 2)

    def test_program_memory_test_before_start_uses_the_past_reserve(self) -> None:
        row = program_memory_row(program_memory_fixture(started=False))
        html = self.page_expression("buildsTooltipHtml('row:hana', data)", {"walk": [row]}, walk=True)
        tip = ElementTree.fromstring(str(html))
        test_phase = tip.findall(".//section[@class='program-phase']")[1]
        self.assertIn("not started", list(tip.itertext()))
        self.assertIn("4.0 GiB reserved", list(test_phase.itertext()))
        fields = [(cell.text, tip[index + 1].text) for index, cell in enumerate(tip) if cell.tag == "dt"]
        self.assertEqual(fields[-4:-2], [("Reserve source", "past run"), ("Live sample", "not started")])
        self.assertIsNone(test_phase.find("./svg/polyline[@class='program-live-line']"))

    def test_program_memory_shows_live_compile_without_a_past_run(self) -> None:
        row = program_memory_row(program_memory_fixture(started=False, with_past=False))
        html = self.page_expression("buildsTooltipHtml('row:hana', data)", {"walk": [row]}, walk=True)
        tip = ElementTree.fromstring(str(html))
        compile_phase, test_phase = tip.findall(".//section[@class='program-phase']")
        self.assertIn("none recorded", list(tip.itertext()))
        self.assertEqual(compile_phase.findtext("./div[@class='program-times']/span[3]"), "8 s")
        self.assertIsNotNone(compile_phase.find("./svg/polyline[@class='program-live-line']"))
        self.assertIsNone(compile_phase.find("./svg/polyline[@class='program-past-line']"))
        self.assertIsNone(test_phase.find("./svg/line[@class='program-reserve']"))

    def test_open_program_memory_tooltip_updates_with_the_next_reading(self) -> None:
        before = program_memory_row(program_memory_fixture(started=False))
        after = program_memory_row(program_memory_fixture(started=True))
        source = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        restore = source[source.index("function restoreReportView("):source.index("function render(d)")]
        expression = """(() => {
          globalThis.latest = data;
          globalThis.window = {innerWidth: 1600, innerHeight: 720, addEventListener() {}};
          globalThis.document = {documentElement: {clientWidth: 1600, clientHeight: 720},
            addEventListener() {}, querySelector: () => null};
          const tip = {
            classList: {add() {}, remove() {}, toggle() {}}, style: {setProperty() {}}, offsetWidth: 368,
            get offsetHeight() { return this.innerHTML.includes('not started') ? 180 : 614; },
          };
          const bounds = () => {
            const top = parseFloat(tip.style.top);
            return [top, top + tip.offsetHeight];
          };
          const tips = hoverTips(tip, currentBuildsTooltip);
          const hideTip = tips.hide;
          tips.show('row:hana', 600, 350);
          const before = tip.innerHTML;
          const initialBounds = bounds();
          latest = { ...data, walk: [data.next] };
        """ + restore + """
          restoreReportView({details: []});
          const refreshed = tip.innerHTML;
          const refreshedBounds = bounds();
          const key = tips.key();
          hideTip();
          tips.show('row:hana', 600, 350);
          return [before, refreshed, key, [initialBounds, refreshedBounds, bounds()]];
        })()"""
        opened, refreshed, key, positions = cast(tuple[str, str, str, list[tuple[int, int]]], self.page_expression(
            expression, {"read_at": 400, "walk": [before], "next": after}, walk=True))
        self.assertIn("not started", opened)
        self.assertNotIn("not started", refreshed)
        test_phase = ElementTree.fromstring(refreshed).findall(".//section[@class='program-phase']")[1]
        self.assertIsNotNone(test_phase.find("./svg/polyline[@class='program-live-line']"))
        self.assertEqual(key, "row:hana")
        for stage, (top, bottom) in zip(("initial", "refreshed", "reopened"), positions, strict=True):
            with self.subTest(stage=stage):
                self.assertGreaterEqual(top, 8)
                self.assertLessEqual(bottom, 712)

    def test_disks_slot_tooltip_reads_traced_steps_without_showing_charts(self) -> None:
        row = program_memory_row(program_memory_fixture(started=True))
        builds_source = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        disks_source = Path(disks_page.__file__).with_name(disks_page.PAGE_NAME).read_text()
        shared = disks_page.shared_step_tip(builds_source)
        slot_tooltip = disks_source[disks_source.index("function slotStep("):disks_source.index("function slotsAbout(")]
        probe = """
          const data = null;
          const settings = {builds_max_age_seconds: 30};
          const slotReport = {machine:'natedev', groups:[{slots:[{name:'hana-slot1', used_by:{}}]}]};
          Date.now = () => builds.read_at * 1000;
          const stepTip = (() => {
        """ + shared.script + """
            return {tooltipHtml, shownReading};
          })();
        """ + slot_tooltip + """
          const amounts = stepTip.shownReading(builds).programReadings.get(builds.walk[0].program_memory);
          return [tipFor('slot:hana-slot1'), amounts.compile.peak.text, amounts.test.current.text];
        """
        expression = ("[...new Function('builds', 'esc', " + json.dumps(probe)
                      + ")(data, esc), tooltipHtml('row:hana', data)]")
        disks, compile_peak, test_sample, shared_builds = cast(
            list[str], self.page_expression(expression, {"read_at": 400, "walk": [row]}, walk=True))
        self.assertEqual((compile_peak, test_sample), ("7.0 GiB", "2.0 GiB"))
        self.assertEqual(disks, shared_builds)
        self.assertIn('<dt>Runs on</dt><dd><span class="machine" data-machine="natedev">natedev</span></dd>', disks)
        self.assertNotIn("program-memory", disks)
        self.assertNotIn(".program-memory", shared.style)
        self.assertIn(".tip :is(dt, dd) { overflow-wrap: anywhere; }", shared.style)

    def test_a_queued_run_hover_says_its_use_need_room_and_progress(self) -> None:
        row = memory_tests.ProgressKeepAsideTests.reservation("gate", need=4 * GIB)
        memory = admission_records.MeasuredMemory(9 * GIB, anonymous=admission_records.AnonymousRunUse(GIB))
        adjusted = gate.progress_memory(row, memory, gate.AttributedStepUse(memory.anonymous, True),
            progress.ProgressCounter("suites:1", 8, 10), memory_tests.ProgressKeepAsideTests.profile("suites:1", 3 * GIB // 2),
            admission_policy_fixture())
        inputs = memory_tests.ProgressKeepAsideTests.inputs((row,), (adjusted,))
        room = gate.inputs_room(inputs, admission_policy_fixture())
        assert isinstance(room, admission_records.MemoryRoom)
        self.assertTrue(memory_feed.need_in_next_part(inputs.running))
        self.assertFalse(memory_feed.need_in_next_part(()))
        module_counter = memory_feed.row_progress(inputs.running, progress.TOKEN_WAIT_REASON)
        assert module_counter["kind"] == "RowProgress"
        test_counter: memory_feed.RowProgress = {**module_counter, "unit": "tests"}
        hold = room.runs[0]
        for name, counter, ordering_note in (
            ("gate", module_counter, True),
            ("test affected", module_counter, True),
            ("fixture-project test affected", module_counter, True),
            ("gate", test_counter, False),
        ):
            with self.subTest(name=name, unit=counter["unit"]):
                hover = cast(list[str], self.page_expression("tipText('row:fixture-run-0', data)", {"read_at": 400, "walk": [{
                    "name": name, "unit": "fixture", "showrunner": "fixture-production", "started_at": 100,
                    "title": {"unit": "fixture", "step": name},
                    "used": 1, "reserved": 0.5, "counted": 1.5, "queued": True, "keep_text": "",
                    "need": memory_feed.gib(hold.memory.need), "need_part": True, "progress": counter}]}))
                self.assertEqual(hover[0], f"fixture · {name}  running 5 min")
                lines = ["Showrunner  fixture-production", "Runs on  natedev", "Uses  1.0 GiB",
                         "+ Build reserved  0.5 GiB", "= In queue cap  1.5 GiB",
                         f"Testing  8 of 10 {counter['unit']}", "Done  80.0%"]
                if ordering_note:
                    lines.append("Order  slowest first")
                self.assertEqual(hover[1], "\n".join(lines))
        for row, expected in (({"between_steps": True, "used": 1, "reserved": 2}, "between steps"),
                              ({"used": {"kind": "RunUseUnavailable", "reason": "private memory.stat is missing"}, "reserved": 2}, "unavailable"),
                              ({"used": 1, "reserved": 0, "started_at": 100}, "running 5 min")):
            with self.subTest(row=row):
                hover = cast(list[str], self.page_expression("tipText('row:fixture-run-0', data)", {"read_at": 400, "walk": [{
                    "name": "gate", "unit": "fixture", "queued": True, "keep_text": "", "counted": 1,
                    "need": 4.0, "need_part": False, **row}]}))
                self.assertIn(expected, hover[0])
                if expected == "unavailable":
                    self.assertEqual(hover[1], "")
                else:
                    self.assertIn("Uses  1.0 GiB", hover[1])
                    self.assertEqual("+ Build reserved" in hover[1], bool(row["reserved"]))

    def test_every_running_and_queued_hover_starts_with_who_ran_it(self) -> None:
        for runner, extra, expected in (
            ("showrunner", {"unit": "—"}, [("Showrunner", "builds")]),
            ("unit director", {}, [("Showrunner", "builds")]),
            ("worker", {"worker": "impl2"}, [("Showrunner", "builds"), ("Worker", "impl2")]),
            ("worker", {"unit": "—", "worker": "staff1"}, [("Showrunner", "builds"), ("Worker", "staff1")]),
            ("unknown", {"showrunner": "owner not recorded", "unit": "owner not recorded"}, [])):
            row = {"name": "hana nextest", "queued": True, "used": 1, "reserved": 0, "counted": 1,
                   "showrunner": "builds", "unit": "report", "runner": runner, **extra}
            data = {"read_at": 200, "walk": [row, {**row, "kind": "mac-build", "started_at": 100, "read_at": 190}],
                    "waiters": [{**row, "need": 2, "since": 100, "why": "its turn"}]}
            for key in ("row:fixture-run-0", "row:fixture-run-1", "wait:fixture-wait-0"):
                with self.subTest(runner=runner, key=key):
                    html = cast(str, self.page_expression(f"tooltipHtml('{key}', data)", data, walk=True))
                    grid = ElementTree.fromstring(html)
                    self.assertEqual((grid.tag, grid.get("class")), ("dl", "tip"))
                    fields = [(cell.text, grid[index+1].text) for index, cell in enumerate(grid) if cell.tag == "dt"]
                    self.assertEqual(fields[1:1+len(expected)], expected)
                    self.assertNotIn("Unit", [pair[0] for pair in fields])
                    self.assertNotIn("not recorded", html)

    def test_a_gate_hover_says_what_the_job_does_and_changes_below_its_title(self) -> None:
        about = "Merges the checkpoint into the sdlc branch, runs every test module, then pushes sdlc"
        row = {"name": "natemerge integrate", "queued": True, "used": 1, "reserved": 0, "counted": 1,
               "showrunner": "builds", "unit": "natemerge", "runner": "unit director",
               "title": {"unit": "natemerge", "step": "integrate"},
               "about": about, "changes": "sdlc branch"}
        data = {"read_at": 200, "walk": [row], "waiters": [{**row, "need": 2, "since": 100, "why": "its turn"}]}
        for key in ("row:fixture-run-0", "wait:fixture-wait-0"):
            with self.subTest(key=key):
                html = cast(str, self.page_expression(f"tooltipHtml('{key}', data)", data, walk=True))
                grid = ElementTree.fromstring(html)
                cells = [(cell.text or "", cell.get("class") or "") for cell in grid]
                self.assertTrue(cells[0][0].startswith("natemerge · integrate"))
                self.assertEqual(cells[2:5], [("Work", ""), ("", ""), (about, "long")])
                fields = [(cell.text, grid[index+1].text) for index, cell in enumerate(grid) if cell.tag == "dt"]
                self.assertEqual(fields[2:4], [("Changes", "sdlc branch"), ("Showrunner", "builds")])

    def test_a_gate_hover_shows_changes_without_a_caption(self) -> None:
        row = {"name": "test all", "queued": True, "used": 1, "reserved": 0, "counted": 1,
               "unit": "owner not recorded", "changes": "no branch"}
        data = {"read_at": 200, "walk": [row], "waiters": [{**row, "need": 2, "since": 100, "why": "its turn"}]}
        for key in ("row:fixture-run-0", "wait:fixture-wait-0"):
            with self.subTest(key=key):
                html = str(self.page_expression(f"tooltipHtml('{key}', data)", data, walk=True))
                grid = ElementTree.fromstring(html)
                self.assertIn("<dt>Changes</dt><dd>no branch</dd>", html)
                self.assertIsNone(grid.find(".//dd[@class='caption']"))

    def test_fixed_final_route_names_natedev_and_its_reason_in_the_tooltip(self) -> None:
        from datetime import UTC
        from scripts.build_queue import admission_client, execution_identity, placement, target_availability
        from scripts.build_targets import target_config
        from scripts.mac_test import claim_state
        from scripts.build_hold.owning_session import NoOwningSession

        mac = placement.PlacementDestination("mac", "mac", "fixture-mac", claim_state.WHOLE_MAC)
        local = placement.PlacementDestination("local", "natedev", "fixture-local", claim_state.WHOLE_MAC)
        constraints = placement.PlacementConstraints(execution_identity.SourceKeyKnown("source"), ("cargo", "check"),
            "/fixture", "", NoOwningSession(), (placement.PlacementCandidate(mac,
                placement.FixedExclusion("full gate runs on natedev")), placement.PlacementCandidate(local)))
        route = admission_client.constrained_step_route(target_config.ANY_ELIGIBLE_TARGET, constraints)
        shown = memory_feed.step_runs_on(route, target_availability.MacRoutes((), {}, UTC), "check", "/fixture", "")
        data = {"read_at": 200, "waiters": [{"name": "cargo-liner check", "since": 100, "need": 5,
            "runs_on": shown.text(), "because": shown.reason}]}
        hover = str(self.page_expression("tipText('wait:fixture-wait-0', data)", data))
        self.assertIn("Runs on  natedev only", hover)
        self.assertIn("full gate runs on natedev", hover)
        self.assertNotIn("natedev or Mac", hover)

    def test_a_ci_hover_keeps_its_ci_line(self) -> None:
        data = {"read_at": 200, "walk": [{"name": "hana CI", "ci": True, "ci_label": "hana CI · runner-1",
                "detail": "busy", "unit": "", "keep_text": "", "runner": "unknown"}]}
        hover = cast(list[str], self.page_expression("tipText('row:fixture-run-0', data)", data))
        self.assertEqual(hover[0], "hana CI · runner-1  running")
        self.assertEqual(hover[1], "Detail  idle\nRuns on  natedev")

    def test_a_running_row_hover_ends_the_memory_text_before_the_source_sentence(self) -> None:
        for keep_text, queued in (("need 11.3 GiB\nfull need: no matching anonymous profile", True),
                                  ("Memory the compiler cache holds. The queue does not start it.", False),
                                  ("Memory these processes hold. The queue did not start them.", False), ("", False)):
            with self.subTest(keep_text=keep_text, queued=queued):
                hover = cast(list[str], self.page_expression("tipText('row:fixture-run-0', data)", {"read_at": 400, "walk": [{
                    "name": "fixture nextest", "unit": "fixture", "showrunner": "fixture-production", "used": 1,
                    "reserved": 0, "counted": 1, "queued": queued, "keep_text": keep_text, "started_at": 100}]}))
                self.assertEqual(hover[1], "Showrunner  fixture-production\nRuns on  natedev\n" +
                    ("" if queued else "Started by  not the queue\n") + "Uses  1.0 GiB")

    def test_cloud_hover_uses_the_table_stage_and_shows_location_and_progress(self) -> None:
        for stage, counter, expected in (
            ("testing", progress.ProgressCounter("modules", 852, 852), "Testing  852 of 852 modules"),
            ("uploading", progress.ProgressCounter("modules", 852, 852), "Uploading  852 of 852 modules"),
            ("launch", progress.NoCounter("no cloud counter"), "Stage  launch"),
            ("", progress.NoCounter("no cloud counter"), ""),
        ):
            with self.subTest(stage=stage):
                data = {"walk": [{"kind": "cloud", "machine": "aws", "name": "gate",
                    "unit": "cloud-unit", "showrunner": "fixture", "stage": stage, "location": "eu-south-2, zone b",
                    "progress": memory_feed.cloud_row_progress(counter)}]}
                shown = cast(list[str], self.page_expression(
                    "[stageText(data.walk[0]), ...tipText('row:fixture-run-0', data)]", data))
                place, _, tip = shown
                self.assertEqual(place, stage or "—")
                self.assertIn("Showrunner  fixture\nRuns on  AWS\nWhere  eu-south-2, zone b", tip)
                self.assertNotIn("Status  ", tip)
                self.assertNotIn("Uses", tip)
                if expected:
                    self.assertIn(expected, tip)
                else:
                    self.assertNotIn("Stage  ", tip)
                if isinstance(counter, progress.ProgressCounter):
                    self.assertIn("Done  100.0%\nOrder  slowest first", tip)

    def test_cloud_chart_shows_only_failures_and_the_late_update(self) -> None:
        for status, late_age, expected in (
            ("1 failed module", "3 min", "1 failed module · no update for 3 min"),
            ("", "3 min", "no update for 3 min"),
            ("", "", ""),
        ):
            with self.subTest(status=status, late_age=late_age):
                row = {"status": status, "stale": bool(late_age), "late_age": late_age}
                html = str(self.page_expression("cloudStatus(data.row, 'row:fixture-run-0')", {"row": row}, walk=True))
                chart = ElementTree.fromstring(html).find("./div[@class='amounts']")
                assert chart is not None
                self.assertEqual("".join(chart.itertext()), expected)

    def test_progress_percent_rounds_to_one_decimal_without_early_completion(self) -> None:
        for done, total, percent, expected in (
            (835, 836, 100 * 835 / 836, "99.9%"),
            (836, 836, 100, "100.0%"),
            (999, 1000, 100, "99.9%"),
            (0, 836, 0, "0.0%"),
        ):
            with self.subTest(done=done, total=total, percent=percent):
                text = self.page_expression("progressRows(data)", {"progress": {
                    "kind": "RowProgress", "phase": "building", "unit": "units",
                    "done": done, "total": total, "percent": percent}})
                self.assertIn(expected, str(text))
                self.assertIn(f"{done} of {total} units", str(text))

    def test_pressure_percent_rounds_to_one_decimal(self) -> None:
        hover = self.page_expression("[tipText('g-some', data), tipText('g-full', data), tipText('g-swap', data)]", {
            "psi": {"some": 1.29, "full": 0.09}, "limits": {"some": 25, "full": 5}})
        self.assertEqual(hover, [
            ["Some tasks stall  1.3%", "Memory waits of at least one task\nAverage over  10 s\nQueue holds above  25.0%"],
            ["All tasks stall  0.1%", "Memory waits of all active tasks\nAverage over  10 s\nQueue holds above  5.0%"],
            ["Swap used  3.0 GiB", "Memory held on disk\nCapacity  8.0 GiB"]])

    def test_pressure_level_uses_stalls_without_a_swap_limit(self) -> None:
        for some, full, used, pressure, expected in (
            (0, 0, 8, "none", "ok"),
            (15, 0, 0, "none", "warn"),
            (0, 3, 0, "none", "warn"),
            (26, 0, 0, "memory pressure: some avg10 26.0", "hold"),
        ):
            with self.subTest(some=some, full=full, swap_used=used):
                result = self.page_expression("level(data)", {
                    "psi": {"some": some, "full": full}, "limits": {"some": 25, "full": 5},
                    "pressure": pressure, "swap": {**SWAP_FIXTURE, "used_bytes": used * GIB}})
                self.assertEqual(result, expected)

    def test_step_table_uses_the_first_column_for_stage_and_waiting_place(self) -> None:
        for waiting in (False, True):
            with self.subTest(waiting=waiting):
                running: list[dict[str, object]] = [
                    {"name": "local build", "queued": True,
                     "progress": {"kind": "RowProgress", "phase": "building"}},
                    {"name": "gate", "queued": True,
                     "progress": {"kind": "RowProgress", "phase": "testing"}},
                    {"name": "Mac builds", "kind": "cloud", "machine": "mac",
                     "progress": {"kind": "NoProgress"}},
                    {"name": "cloud test", "kind": "cloud", "machine": "aws", "stage": "testing",
                     "progress": {"kind": "RowProgress"}},
                    {"name": "cloud setup", "kind": "cloud", "machine": "aws",
                     "progress": {"kind": "NoProgress"}},
                    {"name": "Mac test", "kind": "cloud", "machine": "mac", "stage": "testing",
                     "progress": {"kind": "RowProgress"}},
                    {"name": "hana CI", "ci": True, "admitted": True, "ci_runners": [{"state": "busy"}],
                     "progress": {"kind": "NoProgress"}},
                    {"name": "CI counter", "ci": True, "admitted": True, "ci_runners": [{"state": "busy"}],
                     "progress": {"kind": "RowProgress", "phase": "building"}},
                ]
                waiting_kinds: tuple[tuple[str, dict[str, object]], ...] = (
                    ("first", {}), ("cloud wait", {"kind": "cloud"}), ("second", {}))
                waiters = [{"name": name, "since": 100, "detail": "fixture", "pid": 20 + index,
                            "progress": {"kind": "RowProgress", "phase": "building"}, **extra}
                           for index, (name, extra) in enumerate(waiting_kinds)]
                data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200,
                        "walk": running, "waiters": waiters if waiting else []}
                rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
                root = ElementTree.fromstring(f"<root>{rendered}</root>")
                table = root.find(".//table")
                assert table is not None
                headers = [cell for cell in table.findall("./thead/tr/th") if cell.get("class") != "row-action"]
                self.assertEqual([cell.text for cell in headers],
                                 ["Stage", "Done", "Dur", "Verify", "Step", "Pid", "Machine", "vCPU", "Tokens", "Moved"])
                self.assertEqual(table.findall(".//*[@class='stage']"), [])
                for index, expected in enumerate(("building", "testing", "—", "testing", "—", "testing", "—", "building")):
                    row = table.find(f"./tbody/tr[@data-tip='row:fixture-run-{index}']")
                    assert row is not None
                    self.assertEqual(row[0].get("class"), "place")
                    self.assertEqual(row[0].text, expected)
                    self.assertEqual([cell.get("class") for cell in row[-4:]],
                                     ["vcpus", "tokens", "moved", "row-action"])
                    self.assertEqual(row[-2].text, "—")
                queue_headers = table.findall("./tbody/tr[@class='queue-head']")
                spacers = root.findall("./div[@class='walk-chart']/div[@class='walk-chart-head queue-head']")
                self.assertEqual(len(queue_headers), int(waiting))
                self.assertEqual(len(spacers), 1)
                if waiting:
                    columns = [cell for cell in queue_headers[0] if cell.tag == "th" and cell.get("class") != "row-action"]
                    self.assertEqual([cell.text for cell in columns],
                                     ["#", "Done", "Waiting", "Verify", "Step", "Pid", "Machine", "vCPU", "Moved"])
                    self.assertEqual(queue_headers[0][-1].get("class"), "row-action")
                    self.assertTrue(all(cell.get("scope") == "col" for cell in queue_headers[0]))
                    for index, expected in enumerate(("1", "cloud", "2")):
                        row = table.find(f"./tbody/tr[@data-tip='wait:fixture-wait-{index}']")
                        assert row is not None
                        self.assertEqual(row[0].text, expected)
                        self.assertIsNone(row.find("td[@class='tokens']"))
                for row in table.findall("./tbody/tr"):
                    if "wait" in row.get("class", "").split() or row.get("class") == "queue-head":
                        self.assertEqual(len(row), len(queue_headers[0]))
                    self.assertEqual(sum(int(cell.get("colspan", "1")) for cell in row), len(headers) + 1)

    def test_kept_place_row_has_a_number_and_leaves_the_wait_bars_in_order(self) -> None:
        unit_open = '<span class="staff-name" data-color-text="role.unit-director">'
        setup = ("globalThis.window=globalThis;" + staff_names.script({})[8:-9]
            + f"staffNames.set({{}}, {json.dumps(unit_open)});")
        held = self.kept_place_row()
        reading = {"total": 64, "read_at": 200, "queueControls": {"canMove": True},
            "held_places": [held],
            "waiters": [{"name": "phases", "unit": "phases", "since": 40, "need": 2,
                         "targets": held["targets"], "move": {"row_id": "first"}},
                        {"name": "research", "since": 60, "need": 3, "move": {"row_id": "last"}}]}
        render = "(()=>{" + setup + "renderWalk(data); return $('walk').innerHTML;})()"
        html = self.page_expression(render, reading, walk=True)
        grid = ElementTree.fromstring(f"<root>{html}</root>")
        kept = grid.find(".//div[@data-tip='held:4242-99']")
        assert kept is not None
        self.assertNotIn("wait", kept.get("class", "").split())
        self.assertIsNone(kept.find(".//button"))
        self.assertIsNone(kept.find(".//div[@class='track']"))
        self.assertEqual(step_text(kept, ".//span[@class='name']"), "queue: kept place")
        colored = kept.find(".//span[@class='name']/span[@data-color-text='role.unit-director']")
        assert colored is not None
        self.assertEqual(colored.text, "queue")
        self.assertEqual(colored.tail, ": kept place")
        self.assertEqual(kept.findtext(".//span[@class='owner']"), "last run failed in lint")
        self.assertEqual(grid.findall(".//*[@class='unit']"), [])
        table = grid.find(".//tr[@data-tip='held:4242-99']")
        assert table is not None
        self.assertNotIn("wait", table.get("class", "").split())
        self.assertEqual([table.findtext(f"./td[@class='{name}']") for name in ("place", "duration")],
                         ["2", "until 22:37"])
        self.assertEqual(len(table), len(grid.findall(".//tr[@class='queue-head']/th")))
        self.assertEqual(sum(int(cell.get("colspan", "1")) for cell in table), len(grid.findall(".//thead/tr/th")))
        action = table.find("./td[@class='row-action']")
        assert action is not None
        self.assertEqual(len(action), 0)
        machine = table.find("./td[@class='machine']")
        waiting_machine = grid.find(".//tr[@data-tip='wait:fixture-wait-0']/td[@class='machine']")
        assert machine is not None and waiting_machine is not None
        self.assertEqual(ElementTree.tostring(machine), ElementTree.tostring(waiting_machine))
        self.assertEqual([(icon.attrib["class"], icon.attrib["aria-label"]) for icon in machine],
                         [("machine machine-icon dim", "natedev"), ("machine machine-icon dim", "mac-linux")])
        wait_rows = [row for row in grid.findall(".//div") if "wait" in row.get("class", "").split()]
        self.assertEqual([row.get("data-tip") for row in wait_rows], ["wait:fixture-wait-0", "wait:fixture-wait-1"])
        self.assertTrue(all(row.find(".//div[@class='track']") is not None for row in wait_rows))
        self.assertEqual(grid.findtext(".//tr[@data-tip='wait:fixture-wait-1']/td[@class='place']"), "3")
        reading["held_places"] = [{**held, "failed_step": ""}]
        html = self.page_expression(render, reading, walk=True)
        self.assertNotIn("last run failed in", str(html))

    def test_queue_drag_updates_the_kept_place_before_the_next_view(self) -> None:
        first = {"row_id": "first", "kept_before": 0}
        last = {"row_id": "last", "kept_before": 1}
        line = {"revision": "original", "members": [first, last], "kept_runs": ["4242-99"]}
        reading = {"total": 64, "read_at": 200, "move_revision": "original",
            "move_line": line,
            "held_places": [self.kept_place_row()],
            "waiters": [{"name": "phases", "since": 70, "order_since": 40, "place": 1, "move": first},
                        {"name": "cloud", "kind": "cloud", "since": 60},
                        {"name": "research", "since": 60, "order_since": 60, "place": 3, "move": last}]}
        result = self.page_expression("""(() => {
          const snapshot = JSON.stringify(data);
          const positions = reading => queueRows(reading.waiters, reading.held_places)
            .map(row => [row.key, row.position ?? row.place ?? null]);
          const original = positions(data);
          queueDrag = {reading:data, rows:data.waiters, row:data.waiters[2], pointer:null};
          previewQueueMove(data.waiters[0]);
          const preview = positions(painted);
          const previewHover = pageTooltipHtml("held:4242-99", data).includes("place 1");
          queueReply = {previousRevisions:[data.move_revision], line:{...data.move_line,
            revision:"confirmed", members:[...data.move_line.members].reverse()}};
          const confirmed = queueReading(data);
          showWalk(confirmed);
          const confirmedHover = pageTooltipHtml("held:4242-99", data).includes("place 1");
          return [original, preview, positions(confirmed), queuePlace(confirmed, "last", "confirmed"),
            previewHover, confirmedHover, snapshot === JSON.stringify(data)];
        })()""", reading, walk=True, queue_move=True)
        self.assertEqual(result, [
            [["fixture-wait-0", 1], ["4242-99", 2], ["fixture-wait-1", None], ["fixture-wait-2", 3]],
            [["4242-99", 1], ["fixture-wait-2", 2], ["fixture-wait-1", None], ["fixture-wait-0", 3]],
            [["4242-99", 1], ["fixture-wait-2", 2], ["fixture-wait-1", None], ["fixture-wait-0", 3]],
            2, True, True, True])

    def test_queue_drag_counts_a_hidden_waiter_before_the_kept_place(self) -> None:
        first = {"row_id": "first", "kept_before": 0}
        hidden = {"row_id": "hidden", "kept_before": 0}
        moving = {"row_id": "moving", "kept_before": 1}
        last = {"row_id": "last", "kept_before": 1}
        line = {"revision": "original", "members": [first, moving, hidden, last], "kept_runs": ["4242-99"]}
        reading = {"total": 64, "read_at": 200, "move_revision": "original", "move_line": line,
            "held_places": [self.kept_place_row()],
            "waiters": [{"name": "first", "since": 35, "place": 1, "move": first},
                        {"name": "moving", "since": 60, "place": 3, "move": moving},
                        {"name": "last", "since": 55, "place": 5, "move": last}]}
        confirmed = {**line, "revision": "confirmed", "members": [first, hidden, last, moving]}
        result = self.page_expression("""(() => {
          const kept = reading => reading.held_places[0].position;
          queueDrag = {reading:data, rows:data.waiters, row:data.waiters[1], pointer:null};
          previewQueueMove(data.waiters[2]);
          const preview = kept(painted);
          queueReply = {previousRevisions:[data.move_revision], line:data.confirmed};
          const confirmed = queueReading(data);
          showWalk(confirmed);
          const published = {...data, move_revision:"confirmed", move_line:data.confirmed,
            held_places:[{...data.held_places[0], position:3}],
            waiters:[data.waiters[0], {...data.waiters[2], place:4}, {...data.waiters[1], place:5}]};
          return [preview, kept(confirmed), kept(queueReading(published)),
            queuePlace(confirmed, "moving", "confirmed"), painted.waiters.map(row => row.place)];
        })()""", {**reading, "confirmed": confirmed}, walk=True, queue_move=True)
        self.assertEqual(result, [3, 3, 3, 5, [1, 4, 5]])

    def test_kept_place_hover_uses_the_saved_jobs_match_rule(self) -> None:
        now = datetime(2026, 11, 24, tzinfo=UTC).timestamp()
        for job, expected in (("test-all", "same repository and test family"),
                              ("lint", "same repository and job")):
            with self.subTest(job=job):
                kept = admission_records.HeldPlace("queue", "fixture", job, now - 60, now, now + 60,
                    "4242-99", ("natedev",), 1, "failed", failed_step="lint", position=2)
                row = memory_feed.held_place_row(kept, UTC)
                html = self.page_expression("tooltipHtml('held:4242-99', data)", {"held_places": [row]}, walk=True)
                self.assertIn(f"({expected})", str(html))

    def test_kept_place_hover_names_the_failure_and_the_deadline(self) -> None:
        reading = {"held_places": [self.kept_place_row()]}
        html = self.page_expression("tooltipHtml('held:4242-99', data)", reading, walk=True)
        tip = ElementTree.fromstring(str(html))
        self.assertEqual([(node.tag, node.get("class"), node.text) for node in tip[:2]],
                         [("dt", "title", "Kept place"), ("dd", "title", "place 2")])
        fields = {"Unit": "queue", "Failed run": "4242-99", "Failed step": "lint",
                  "Failed at": "22:07 PST", "Kept until": "22:37 PST"}
        self.assertEqual({node.text: tip[index + 1].text for index, node in enumerate(tip)
                          if node.tag == "dt" and index > 0}, fields)
        self.assertEqual(tip.findtext("dd[@class='long']"),
            "If the next run of unit queue (same repository and job) arrives before 22:37 PST, "
            + "it starts from place 2. Runs behind place 2 do not wait for it.")

    @staticmethod
    def kept_place_row() -> dict[str, object]:
        return {"kind": "held", "key": "4242-99", "since": 50, "position": 2, "unit": "queue",
                "failed_run": "4242-99", "failed_step": "lint", "failed_at": "22:07 PST",
                "kept_until": "22:37 PST", "targets": ["natedev", "mac-linux"], "match_rule": "same repository and job"}

    def test_guest_row_shows_confirmation_and_caller(self) -> None:
        for guest, state in (({"guest_run": {"attempt": "fixture-attempt"}}, ""),
                             ({"guest_unconfirmed": True}, "not confirmed")):
            with self.subTest(state=state):
                reading = {"total": 64, "read_at": 200, "walk": [{
                    "name": "phases: test affected", "title": {"unit": "phases", "step": "test affected"},
                    "caller": "phases", "machine": "mac-linux", "started_at": 100,
                    "used": {"kind": "RunUseUnavailable", "reason": "remote memory is unavailable"},
                    "progress": {"kind": "NoProgress"}, **guest}]}
                table, tip = cast(tuple[str, str], self.page_expression(
                    "(renderWalk(data), [$('walk').innerHTML, tipText('row:fixture-run-0', data).join('\\n')])",
                    reading, walk=True))
                row = ElementTree.fromstring(f"<root>{table}</root>").find(".//tr[@data-tip='row:fixture-run-0']")
                assert row is not None
                machine = row.find("td[@class='machine']")
                assert machine is not None
                self.assertEqual([(icon.get("class"), icon.get("aria-label"), icon.get("data-machine")) for icon in machine],
                                 [("machine machine-icon run", "mac-linux", "mac-linux")])
                self.assertEqual("".join(machine.itertext()).strip(), state)
                for column in ("place", "done", "vcpus"):
                    self.assertEqual(row.findtext(f"td[@class='{column}']"), "—")
                self.assertIn("Caller  phases", tip)
                self.assertIn("Runs on  mac-linux", tip)
                self.assertNotIn("Guest attempt", tip)
                self.assertNotIn("fixture-attempt", tip)

    def test_guest_row_shows_on_mac_linux_in_the_memory_column(self) -> None:
        reading = {"total": 64, "read_at": 200, "walk": [{
            "name": "test affected", "machine": "mac-linux", "guest_unconfirmed": True,
            "used": {"kind": "RunUseUnavailable", "reason": "remote memory is unavailable"}}]}
        table = self.page_expression("(renderWalk(data), $('walk').innerHTML)", reading, walk=True)
        chart = ElementTree.fromstring(f"<root>{table}</root>").find(".//div[@data-run='fixture-run-0']")
        assert chart is not None
        self.assertEqual(chart.findtext(".//div[@class='amounts']"), "on mac-linux")

    def test_guest_shows_one_running_icon_and_names_targets_in_the_hover(self) -> None:
        targets = ["natedev", "mac-linux"]
        reading = {"total": 64, "read_at": 200, "walk": [{
            "name": "guest", "guest_run": {"attempt": "fixture-attempt"}, "machine": "mac-linux",
            "targets": targets, "progress": {"kind": "RowProgress", "phase": "testing", "done": 2, "total": 5,
                                             "unit": "modules"}}],
            "waiters": [{"name": "gate", "machine": "natedev", "targets": targets, "since": 100}]}
        rendered, tip = cast(tuple[str, str], self.page_expression(
            "(renderWalk(data), [$('walk').innerHTML, tipText('row:fixture-run-0', data).join('\\n')])",
            reading, walk=True))
        table = ElementTree.fromstring(f"<root>{rendered}</root>")
        running = table.find(".//tr[@data-tip='row:fixture-run-0']/td[@class='machine']")
        waiting = table.find(".//tr[@data-tip='wait:fixture-wait-0']/td[@class='machine']")
        assert running is not None and waiting is not None
        self.assertEqual([(icon.get("class"), icon.get("aria-label"), icon.get("data-machine")) for icon in running],
                         [("machine machine-icon run", "mac-linux", "mac-linux")])
        self.assertEqual("".join(running.itertext()).strip(), "")
        self.assertIn("Runs on  mac-linux", tip.splitlines())
        self.assertIn("Named targets  natedev or mac-linux", tip.splitlines())
        self.assertIn("Testing  2 of 5 modules", tip.splitlines())
        self.assertIn("Done  40.0%", tip.splitlines())
        self.assertNotIn("Guest attempt", tip)
        self.assertNotIn("fixture-attempt", tip)
        self.assertEqual([(icon.get("class"), icon.get("aria-label"), icon.get("data-machine")) for icon in waiting],
                         [("machine machine-icon dim", target, target) for target in targets])

    def test_ci_row_requires_a_running_job_and_preserves_the_used_total(self) -> None:
        for state in ("idle", "waiting", "unmatched", "busy", "unreadable"):
            with self.subTest(state=state):
                data = {"total": 64, "cap": 48, "taken": 4.125, "used": 3, "ci_ram": 0.125,
                        "ram": {**RAM_FIXTURE, "build_tools_bytes": 0, "rust_analyzer_bytes": 0,
                                "sccache_bytes": 0, "other_build_tools_bytes": 0, "queued_builds_bytes": 3 * GIB,
                                "ci_bytes": 0.125 * GIB, "build_covered_bytes": GIB, "build_target_bytes": GIB,
                                "room_bytes": 43.875 * GIB},
                        "read_at": 200, "waiters": [], "walk": [{
                            "name": "hana CI", "ci": True, "admitted": state == "busy",
                            "ci_runners": [{"state": "busy" if state in ("waiting", "unmatched") else state}],
                            "used": 0.125, "queued": False, "started_at": 100,
                            "progress": {"kind": "NoProgress"}}]}
                result = cast(list[object], self.page_expression(
                    "(renderWalk(data), [$('walk').innerHTML, $('walk-sum').textContent, data.ci_ram, data.used])",
                    data, walk=True))
                root = ElementTree.fromstring(f"<root>{result[0]}</root>")
                rows = root.findall(".//tr[@data-tip='row:fixture-run-0']")
                chart_rows = root.findall(".//div[@data-run='fixture-run-0']")
                self.assertEqual(len(rows), int(state == "busy"))
                self.assertEqual(len(chart_rows), int(state == "busy"))
                self.assertEqual(result[1:], ["4.1 of 48.0 GiB taken", 0.125, 3])
                if rows:
                    self.assertEqual(rows[0][0].text, "—")
                    self.assertEqual(step_text(rows[0], "th[@class='step']"), "")
                    self.assertEqual(chart_rows[0].findtext(".//span[@class='name']"), "hana CI")

    def test_mac_row_uses_its_stage_step_name_memory_and_aligned_tooltip(self) -> None:
        for stage, expected in (("MacBuilding", "building"), ("MacTesting", "testing"),
                                ("MacTestingCounter", "testing"), ("MacStageUnavailable", "—")):
            with self.subTest(stage=stage):
                row = {"kind": "mac-build", "name": "hana nextest", "unit": "trunk", "pid": 710001,
                       "title": {"unit": "trunk", "step": "hana nextest"},
                       "machine": "mac", "vcpus": 12, "repository": "hana", "showrunner": "hana", "started_at": 100, "read_at": 190,
                       "used": 3.5, "stage": {"kind": stage}}
                data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "walk": [row], "waiters": []}
                result = cast(list[str], self.page_expression(
                    "(renderWalk(data), [$('walk').innerHTML, tooltipHtml('row:fixture-run-0', data)])", data, walk=True))
                table = ElementTree.fromstring(f"<root>{result[0]}</root>")
                metadata = table.find(".//tr[@data-tip='row:fixture-run-0']")
                assert metadata is not None
                self.assertEqual(metadata[0].text, expected)
                vcpus = metadata.find("td[@class='vcpus']")
                assert vcpus is not None
                self.assertEqual(step_text(metadata, "th[@class='step']"), "hana nextest")
                self.assertEqual(table.findtext(".//div[@data-run='fixture-run-0']//span[@class='name']"), "trunk")
                self.assertEqual(vcpus.text, "12")
                owner = table.find(".//div[@data-run='fixture-run-0']/span/span[@class='owner']")
                assert owner is not None
                self.assertEqual(owner.text, "hana")
                tooltip = ElementTree.fromstring(result[1])
                labels = [cell.text for cell in tooltip.findall("dt")]
                values = ["".join(cell.itertext()) for cell in tooltip.findall("dd")]
                self.assertEqual(labels, ["trunk · hana nextest", "Showrunner", "Runs on", "Uses", "Reading age", "In queue cap"])
                self.assertEqual(values, ["running 2 min", "hana", "Mac", "3.5 GiB", "<1 min", "no"])

    def test_mac_failed_reading_shows_a_short_status_and_unknown_cells(self) -> None:
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "waiters": [], "walk": [{
            "kind": "mac-unavailable", "name": "Mac builds", "unit": "—", "machine": "mac",
            "started_at": 0, "read_at": 0, "used": {"kind": "MacMemoryUnavailable"},
            "stage": {"kind": "MacStageUnavailable"}, "status": "Mac reading is old · age unavailable"}]}
        result = cast(list[str], self.page_expression(
            "(renderWalk(data), [$('walk').innerHTML, tooltipHtml('row:fixture-run-0', data)])", data, walk=True))
        root = ElementTree.fromstring(f"<root>{result[0]}</root>")
        row = root.find(".//tr[@data-tip='row:fixture-run-0']")
        assert row is not None
        for column in ("place", "duration", "pid", "vcpus"):
            self.assertEqual(row.findtext(f"td[@class='{column}']"), "—")
        self.assertEqual(step_text(row, "th[@class='step']"), "")
        self.assertEqual(result[1].count("unavailable"), 1)

    def test_queue_notes_appear_once_in_their_row_tooltips(self) -> None:
        room = {"unavailable": "", "rows": [{"label": "Queue cap", "gib": 11.3},
            {"label": "− Builds", "gib": 3.2},
            {"label": "− Build reserved", "gib": 8.1}, {"label": "= Free", "gib": 0}]}
        for index, cause in enumerate(("memory", "its turn", "memory")):
            data = {"read_at": 200, "cap": 11.3,
                "ram": {**RAM_FIXTURE, "queued_builds_bytes": 3.2 * GIB, "build_tools_bytes": 0, "rust_analyzer_bytes": 0, "sccache_bytes": 0,
                        "other_build_tools_bytes": 0, "ci_bytes": 0, "build_target_bytes": 8.1 * GIB,
                        "build_covered_bytes": 8.1 * GIB, "room_bytes": 0},
                "waiters": [{"name": "port nextest", "unit": "port", "need": 1.9,
                "since": 100, "place": index+1, "why": cause, "free": 0, "runs_on": "natedev only", "first_in_line": True,
                **({"head_room": room} if index == 0 else {})}]}
            with self.subTest(cause=cause):
                hover = cast(list[str], self.page_expression("tipText('wait:fixture-wait-0', data)", data))
                self.assertIn(f"Place in line  {index+1}\nWaits for  {cause}", hover[1])
                self.assertEqual(hover[1].count("Needs  1.9 GiB"), 1)
                self.assertEqual(hover[1].count("= Free  0.0 GiB"), int(index == 0))
                self.assertIn("Short by  1.9 GiB", hover[1])
                self.assertNotIn("Build reserved is", hover[1])
                self.assertNotIn("queue priority:", hover[1])

    def test_fractional_memory_terms_add_shown_parts_and_keep_raw_shortage(self) -> None:
        ram = {**RAM_FIXTURE, "build_tools_bytes": 1.04 * GIB, "rust_analyzer_bytes": 1.04 * GIB,
               "sccache_bytes": 0, "other_build_tools_bytes": 0, "queued_builds_bytes": 1.04 * GIB,
               "ci_bytes": 1.04 * GIB, "build_covered_bytes": 1.04 * GIB, "build_target_bytes": 1.04 * GIB,
               "room_bytes": 1.04 * GIB}
        waiters: list[dict[str, object]] = [
            {"name": "first", "need": 1.04, "free": 1.0},
            {"name": "second", "need": 1.06, "free": 1.04},
            {"name": "head", "place": 3, "need": 1.06, "free": 1.04, "head_room": {"rows": [
                {"label": "Queue cap", "gib": 5.2}, {"label": "− Builds", "gib": 3.12},
                {"label": "− Build reserved", "gib": 1.04}, {"label": "= Free", "gib": 1.04}]}},
            {"name": "partial", "need": 1.06, "free": 0.5},
            {"name": "after a slot", "need": 1.5, "free": 2.0}]
        data = {"ram": ram, "cap": 5.2, "walk": [{"name": "build", "used": 1.04, "reserved": 1.04, "queued": True}],
                "waiters": waiters}
        tips = cast(list[list[dict[str, object]]], self.page_expression(
            "['g-builds','row:fixture-run-0'].map(key => tooltipRows(key, data))", data))
        # Each step as the first in line shows its room.
        firsts = [cast(list[dict[str, object]], self.page_expression("tooltipRows('wait:first', data)",
                                                                    {**data, "waiters": [{**waiter, "key": "first", "first_in_line": True}]})) for waiter in waiters]
        given = {str(row["label"]): str(row["value"]) for row in firsts.pop()}
        self.assertEqual((given["Free"], given["+ Released"]), ("1.0 GiB", "1.0 GiB"))
        self.assertNotIn("Short by", given)
        kept = {str(row["label"]): str(row["value"]) for row in firsts.pop()}
        self.assertEqual((kept["Free"], kept["− Held elsewhere"], kept["Short by"]), ("1.0 GiB", "0.5 GiB", "0.6 GiB"))
        # The user, 11-16: the queue admits one step at a time, so a step after the first shows only what it needs.
        behind = cast(list[dict[str, object]], self.page_expression("tooltipRows('wait:fixture-wait-1', data)", data))
        self.assertEqual([row["label"] for row in behind if str(row["value"]).endswith(" GiB")], ["Needs"])
        self.assertEqual([row["value"] for row in tips[0] if row["kind"] != "caption"],
                         ["3.1 GiB", "1.1 GiB", "1.0 GiB", "1.0 GiB"])
        run = {str(row["label"]): str(row["value"]) for row in tips[1]}
        self.assertEqual((run["Uses"], run["+ Build reserved"], run["= In queue cap"]),
                         ("1.0 GiB", "1.1 GiB", "2.1 GiB"))
        small, short, head_wait = ({str(row["label"]): str(row["value"]) for row in tip} for tip in firsts)
        self.assertEqual(small["Short by"], "<0.1 GiB")
        self.assertEqual(short["Short by"], "<0.1 GiB")
        self.assertEqual(head_wait["Short by"], "<0.1 GiB")
        terms = ("Queue cap", "− Builds", "− Build reserved", "= Free")
        head = {str(row["label"]): round(float(str(row["value"]).split()[0]) * 10)
                for row in firsts[-1] if row["label"] in terms}
        self.assertEqual(head["Queue cap"], head["− Builds"] + head["− Build reserved"] + head["= Free"])
        cap = cast(list[dict[str, object]], self.page_expression("tooltipRows('cap', data)", data))
        cap_fields = {str(row["label"]): str(row["value"]) for row in cap}
        self.assertEqual(run["+ Build reserved"], cap_fields["+ Build reserved"])
        self.assertEqual(run["Uses"], next(row["value"] for row in tips[0] if row["label"] == "+ Queued builds"))
        head_data = {**data, "waiters": [{**waiters[2], "key": "first", "first_in_line": True}]}
        head_cap = cast(list[dict[str, object]], self.page_expression("tooltipRows('cap', data)", head_data))
        head_cap_fields = {str(row["label"]): str(row["value"]) for row in head_cap}
        for head_name, cap_name in (("Queue cap", "Queue cap"), ("− Builds", "Builds"),
                                   ("− Build reserved", "+ Build reserved"), ("= Free", "+ Free")):
            self.assertEqual(head_wait[head_name], head_cap_fields[cap_name])
        room: dict[str, object] = {"head_room": {"rows": [
            {"label": "Queue cap", "gib": 13.049}, {"label": "− Builds", "gib": 7},
            {"label": "− Build reserved", "gib": 6}, {"label": "= Free", "gib": 0.049}]}}
        for head_room in ({}, room):
            with self.subTest(head_room=head_room):
                tiny = {"cap": 13.049, "ram": {**RAM_FIXTURE, "room_bytes": round(0.049 * GIB)},
                        "waiters": [{"name": "tiny", "need": 0.051, "free": 0.049,
                                     "first_in_line": True, **head_room}]}
                hover = cast(list[str], self.page_expression("tipText('wait:fixture-wait-0', data)", tiny))
                self.assertIn("Needs  <0.1 GiB", hover[1])
                self.assertIn("Free  <0.1 GiB", hover[1])
                self.assertIn("Short by  <0.1 GiB", hover[1])

    def memory_rounding_cases(self) -> list[tuple[str, str, dict[str, object], object]]:
        one_gib_build: dict[str, object] = {
            **RAM_FIXTURE, "build_tools_bytes": 1.02 * GIB, "rust_analyzer_bytes": 1.02 * GIB,
            "sccache_bytes": 0, "other_build_tools_bytes": 0, "queued_builds_bytes": 0, "ci_bytes": 0,
            "build_covered_bytes": 1.02 * GIB, "room_bytes": 1.02 * GIB,
        }

        def head(room: float, earlier: float, free: float, need: float, used: float = 1.02,
                 reserved: float = 1.02) -> dict[str, object]:
            cap = used + reserved + room
            return {"ram": {**one_gib_build, "room_bytes": room * GIB, "build_tools_bytes": used * GIB,
                            "rust_analyzer_bytes": used * GIB, "build_covered_bytes": reserved * GIB},
                    "cap": cap, "waiters": [
                {"name": "build", "need": need, "first_in_line": True, "head_room": {"rows": [
                    {"label": "Queue cap", "gib": cap}, {"label": "− Builds", "gib": used},
                    {"label": "− Build reserved", "gib": reserved}, {"label": "− Earlier waiters", "gib": earlier},
                    {"label": "= Head room", "gib": free},
                ]}},
            ]}

        def outside(other: float, swap: float, adjustment: float, no_counter: float = 0) -> dict[str, object]:
            return {"ram": {**RAM_FIXTURE, "desktop_bytes": other * GIB, "agents_bytes": other * GIB,
                "other_programs_bytes": other * GIB, "programs": [{"name": "Editor", "bytes": other * GIB}],
                "kernel_bytes": swap * GIB, "compressed_swap_bytes": swap * GIB,
                "kernel_structures_bytes": 0, "kernel_counters_bytes": 0, "cache_bytes": other * GIB,
                "unattributed_bytes": adjustment * GIB, "kernel_no_counter_bytes": no_counter * GIB}}

        def shared_ram(outside_gib: float) -> dict[str, object]:
            return {
                **one_gib_build, "total_bytes": (1.08 + outside_gib) * GIB,
                "available_bytes": 0.06 * GIB, "desktop_bytes": outside_gib * GIB,
                "build_covered_bytes": 0.02 * GIB, "build_target_bytes": 0.02 * GIB,
                "safety_covered_bytes": 0.02 * GIB, "safety_target_bytes": 0.02 * GIB,
                "swap_covered_bytes": 0, "swap_target_bytes": 0, "room_bytes": 0.02 * GIB,
                "agents_bytes": 0, "other_programs_bytes": 0, "programs": [], "smaller_program_count": 0,
                "kernel_bytes": 0, "compressed_swap_bytes": 0, "kernel_structures_bytes": 0,
                "kernel_counters_bytes": 0, "cache_bytes": 0, "unattributed_bytes": 0, "kernel_no_counter_bytes": 0,
            }

        head_values = "amounts('wait:fixture-wait-0')"
        outside_values = "[amounts('g-else'), amounts('unattributed')]"
        return [
            ("cap uses shown parts", "amounts('cap')", {"ram": one_gib_build, "cap": 3.06},
             ["3.1 GiB", "1.0 GiB", "1.0 GiB", "1.1 GiB"]),
            ("Taken stays within the cap", "amounts('walksum')",
             {"cap": 8.989, "ram": {**one_gib_build, "room_bytes": 0.02 * GIB,
                                    "build_covered_bytes": 7.949 * GIB, "build_target_bytes": 7.962 * GIB,
                                    "build_missing_bytes": 0.013 * GIB}},
             ["9.0 GiB", "1.0 GiB", "8.0 GiB"]),
            ("head uses shown deductions", head_values, head(1.02, 0.154, 0.866, 0.87),
             ["3.1 GiB", "1.0 GiB", "1.0 GiB", "0.2 GiB", "0.9 GiB", "0.9 GiB", "<0.1 GiB"]),
            ("small head room", head_values, head(0.952, 0.948, 0.004, 0.005),
             ["3.0 GiB", "1.0 GiB", "1.0 GiB", "0.9 GiB", "<0.1 GiB", "<0.1 GiB", "<0.1 GiB"]),
            ("signed cancellation", outside_values, outside(0.149, 0.149, -0.696),
             [["<0.1 GiB", "0.2 GiB", "0.2 GiB", "0.1 GiB", "0.1 GiB", "0.1 GiB", "-0.7 GiB"],
              ["-0.7 GiB", "0.0 GiB", "-0.7 GiB"]]),
            ("signed part below the threshold",
             "amounts('unattributed')", outside(0.149, 0.149, -0.696, -0.099),
             ["-0.7 GiB", "-<0.1 GiB", "-0.6 GiB"]),
            ("adjustments keep small labels",
             "[-1, 1].map(sign => tooltipRows('unattributed', {...data, "
             + "ram:{...data.ram,unattributed_bytes:sign * 0.099 * GIB}})[0].value)", {},
             ["-<0.1 GiB", "<0.1 GiB"]),
            ("Build tools keep their order", "amounts('tools')",
             {"ram": {**RAM_FIXTURE, "build_tools_bytes": 410 * MIB, "rust_analyzer_bytes": 150 * MIB,
                      "sccache_bytes": 0, "python_tests_bytes": 120 * MIB,
                      "waiting_runs_bytes": 70 * MIB, "other_build_tools_bytes": 70 * MIB}},
             ["0.4 GiB", "0.1 GiB", "0.0 GiB", "0.1 GiB", "<0.1 GiB", "<0.1 GiB"]),
            ("compressed swap shares the Kernel reading",
             "[amounts('kernel')[1], compressedSwapNote(shownReading(data).memory.kernel)]",
             outside(0.146, 0.149, -0.729), ["0.2 GiB", "Of which compressed swap: 0.2 GiB."]),
            ("head and cap share Builds",
             "[amounts('cap'), " + head_values + "]", head(0.952, 0.948, 0.004, 0.005, 1.049, 1.051),
             [["3.1 GiB", "1.0 GiB", "1.1 GiB", "1.0 GiB"],
              ["3.1 GiB", "1.0 GiB", "1.1 GiB", "0.9 GiB", "<0.1 GiB", "<0.1 GiB", "<0.1 GiB"]]),
            ("Kernel keeps its measured order beside Desktop",
             "[amounts('g-else').slice(0, 5), amounts('kernel')]",
             {"ram": {**RAM_FIXTURE, "desktop_bytes": 0.36 * GIB, "agents_bytes": 0,
                      "other_programs_bytes": 0, "programs": [], "smaller_program_count": 0,
                      "kernel_bytes": 0.41 * GIB, "compressed_swap_bytes": 0.137 * GIB,
                      "kernel_structures_bytes": 0.137 * GIB, "kernel_counters_bytes": 0.136 * GIB,
                      "cache_bytes": 0, "unattributed_bytes": 0}},
             [["0.8 GiB", "0.4 GiB", "0.0 GiB", "0.0 GiB", "0.4 GiB"],
              ["0.4 GiB", "0.2 GiB", "0.1 GiB", "0.1 GiB"]]),
            ("Taken stays within the cap with small coverage and room",
             "[amounts('cap'), amounts('walksum')]",
             {"cap": 1.198, "ram": {**one_gib_build, "build_tools_bytes": 1.049 * GIB,
                                   "rust_analyzer_bytes": 1.049 * GIB, "room_bytes": 0.079 * GIB,
                                   "build_covered_bytes": 0.07 * GIB, "build_target_bytes": 0.119 * GIB,
                                   "build_missing_bytes": 0.049 * GIB}},
             [["1.2 GiB", "1.1 GiB", "<0.1 GiB", "<0.1 GiB"], ["1.2 GiB", "1.1 GiB", "0.1 GiB"]]),
            ("small parts give the cap a range", "[amounts('cap'), capRange(), amounts('walksum')]",
             {"cap": 1.198, "ram": {**one_gib_build, "build_tools_bytes": 1.049 * GIB,
                                   "rust_analyzer_bytes": 1.049 * GIB, "room_bytes": 0.050 * GIB,
                                   "build_covered_bytes": 0.099 * GIB, "build_target_bytes": 0.119 * GIB,
                                   "build_missing_bytes": 0.020 * GIB}},
             [["1.2 GiB", "1.1 GiB", "<0.1 GiB", "<0.1 GiB"], [11, 12, 13], ["1.2 GiB", "1.1 GiB", "0.1 GiB"]]),
            ("Kernel stays in order with a small child", "[amounts('g-else').slice(0, 5), amounts('kernel')]",
             {"ram": {**RAM_FIXTURE, "desktop_bytes": 0.36 * GIB, "agents_bytes": 0.12 * GIB,
                      "other_programs_bytes": 0, "programs": [], "smaller_program_count": 0,
                      "kernel_bytes": 0.39 * GIB, "compressed_swap_bytes": 0.30 * GIB,
                      "kernel_structures_bytes": 0.09 * GIB, "kernel_counters_bytes": 0,
                      "cache_bytes": 0, "unattributed_bytes": 0}},
             [["0.9 GiB", "0.4 GiB", "0.1 GiB", "0.0 GiB", "0.4 GiB"],
              ["0.4 GiB", "0.3 GiB", "<0.1 GiB", "0.0 GiB"]]),
            ("status uses the displayed room", "[amounts('room')[0], statusLine(shownReading(data))]",
             {"cap": 2.295, "ram": {**one_gib_build, "build_covered_bytes": 1.03 * GIB, "room_bytes": 0.245 * GIB},
              "memory_status": {"code": "one_running_memory_wait", "next_need_bytes": 0.5 * GIB,
                                "check_room_bytes": 0.245 * GIB}},
             ["0.2 GiB", "One build runs; another needs 0.5 GiB, but free is 0.2 GiB."]),
            ("Queued builds and Uses share the running step",
             "[amounts('queued'), amounts('row:fixture-run-0'), amounts('cap')]",
             {"cap": 1.321, "ram": {**one_gib_build, "build_tools_bytes": 0, "rust_analyzer_bytes": 0,
                                   "queued_builds_bytes": 1.055 * GIB, "build_covered_bytes": 0,
                                   "build_target_bytes": 0, "room_bytes": 0.266 * GIB},
              "walk": [{"name": "queued", "queued": True, "used": 1.055, "reserved": 0}]},
             [["1.0 GiB"], ["1.0 GiB"], ["1.3 GiB", "1.0 GiB", "0.0 GiB", "0.3 GiB"]]),
            ("The step reserve shares Build reserved",
             "[amounts('cap'), amounts('row:fixture-run-0')]",
             {"cap": 1.282, "ram": {**one_gib_build, "build_tools_bytes": 0, "rust_analyzer_bytes": 0,
                                   "queued_builds_bytes": 1.01 * GIB, "build_covered_bytes": 0.138 * GIB,
                                   "build_target_bytes": 0.138 * GIB, "room_bytes": 0.134 * GIB},
              "walk": [{"name": "run", "queued": True, "used": 1.01, "reserved": 0.138}]},
             [["1.3 GiB", "1.0 GiB", "0.2 GiB", "0.1 GiB"], ["1.0 GiB", "0.2 GiB", "1.2 GiB"]]),
            ("The wait shortage participates in the cap sum", "[amounts('cap'), " + head_values + "]",
             {"cap": 1.16, "ram": {**one_gib_build, "build_covered_bytes": 0, "build_target_bytes": 0,
                                  "room_bytes": 0.14 * GIB},
              "waiters": [{"name": "first", "first_in_line": True, "need": 0.289, "free": 0.14}]},
             [["1.2 GiB", "1.1 GiB", "0.0 GiB", "0.1 GiB"], ["0.3 GiB", "0.1 GiB", "0.2 GiB"]]),
            ("Swap totals share pool and member sums",
             "[amounts('g-swap')[0], amounts('swap-pool:builds_ci')[0], amounts('swap-pool:outside')[0], "
             + "amounts('swap:0')[0], ...[...shownReading(data).swapDisplay.members.values()].map(node => node.text)]",
             {"swap": {**SWAP_FIXTURE, "used_bytes": 2.28 * GIB, "builds_ci_bytes": 1.14 * GIB,
                       "outside_bytes": 1.14 * GIB},
              "swap_by": [{"name": "Smaller groups", "pool": "outside", "kind": "smaller", "bytes": 1.14 * GIB,
                           "members": [{"name": "one", "bytes": 0.57 * GIB}, {"name": "two", "bytes": 0.57 * GIB}]}]},
             ["2.3 GiB", "1.2 GiB", "1.1 GiB", "1.1 GiB", "583.7 MiB", "583.7 MiB"]),
            ("The preview uses the displayed memory",
             "(() => {const row = data.walk[0]; const key = 'row:fixture-run-0'; "
             + "const {memory} = buildStepEvidence(key, data, row, [], tooltipRows(key, data), data.limits).evidence; "
             + "return [amounts('g-builds')[0], memory.segments[0].text, memory.free_text, memory.cap_text];})()",
             {"cap": 1.321, "read_at": 200, "limits": new_item_dialog.BUILD_EVIDENCE_LIMITS,
              "ram": {**one_gib_build, "build_tools_bytes": 0, "rust_analyzer_bytes": 0,
                      "queued_builds_bytes": 1.055 * GIB, "build_covered_bytes": 0, "build_target_bytes": 0,
                      "room_bytes": 0.266 * GIB},
              "walk": [{"name": "queued", "queued": True, "used": 1.055, "reserved": 0}]},
             ["1.0 GiB", "1.0 GiB", "0.3 GiB", "1.3 GiB"]),
            ("The cap note and token wait reuse displayed amounts",
             "[capUnavailableNote(shownReading(data)), tooltipRows('row:fixture-run-0', data).at(-1).value, "
             + "tooltipRows('tokens',data).find(row => row.label === 'Pause below').value]",
             {"cap": None, "cap_text": "Cap unavailable", "tokens": {
                 "kind": "TokenPool", "total": 31, "free": 4, "memory_floor_bytes": 12.04 * GIB, "paused": True},
              "walk": [{"name": "run", "queued": True, "used": 3, "progress": {
                  "kind": "TokenWaitProgress", "memory_paused": True, "stage": "waiting for jobserver tokens"}}]},
             ["Cap unavailable; free RAM 30.0 GiB; safety 4.0 GiB; swap coming back 2.0 GiB; "
              + "kept aside 6.0 GiB; room 18.0 GiB",
              "waiting for jobserver tokens (memory under 12.0 GiB)", "12.0 GiB"]),
            ("Taken, Over cap and the reserve target share both sums",
             "[amounts('walksum'), amounts('reserved'), amounts('shortfall')]",
             {"cap": 1.126, "ram": {**one_gib_build, "build_tools_bytes": GIB, "rust_analyzer_bytes": GIB,
                                   "build_covered_bytes": 0.126 * GIB, "build_missing_bytes": 0.126 * GIB,
                                   "build_target_bytes": 0.252 * GIB, "shortfall_bytes": 0.126 * GIB,
                                   "room_bytes": 0}},
             [["1.2 GiB", "1.0 GiB", "0.2 GiB", "1.1 GiB", "0.1 GiB"],
              ["0.1 GiB", "0.2 GiB", "0.1 GiB"], ["0.1 GiB", "0.1 GiB"]]),
            ("Scale and RAM share the total",
             "[amounts('ticks')[0], amounts('budget')[0]]", {"cap": 1.06, "ram": shared_ram(1.06)},
             ["2.1 GiB", "2.1 GiB"]),
            ("Scale uses the total that the parts require",
             "[amounts('ticks')[0], amounts('budget')[0]]",
             {"cap": 1.06, "ram": {**shared_ram(1.08), "total_bytes": 2.14 * GIB, "available_bytes": 0,
                                   "build_tools_bytes": 1.06 * GIB, "rust_analyzer_bytes": 1.06 * GIB,
                                   "build_covered_bytes": 0, "build_target_bytes": 0, "room_bytes": 0,
                                   "safety_covered_bytes": 0, "safety_target_bytes": 0}},
             ["2.2 GiB", "2.2 GiB"]),
            ("Builds keeps its order beside Everything else",
             "[amounts('cap'), amounts('budget')]", {"cap": 1.06, "ram": shared_ram(1.04)},
             [["1.1 GiB", "1.0 GiB", "<0.1 GiB", "<0.1 GiB"],
              ["2.1 GiB", "1.0 GiB", "<0.1 GiB", "1.0 GiB"]]),
            ("Taken and the cap share Builds and Build reserved",
             "[amounts('cap'), amounts('walksum')]",
             {"cap": 1.259, "ram": {**one_gib_build, "build_covered_bytes": 0.14 * GIB,
                                   "build_target_bytes": 0.14 * GIB, "room_bytes": 0.099 * GIB}},
             [["1.3 GiB", "1.0 GiB", "0.2 GiB", "<0.1 GiB"], ["1.2 GiB", "1.0 GiB", "0.2 GiB"]]),
        ]

    def test_memory_rounding_preserves_measured_values_and_shown_sums(self) -> None:
        for name, expression, data, expected in self.memory_rounding_cases():
            with self.subTest(name=name):
                wrapper = """(() => {
                    const amounts = key => tooltipRows(key, data).map(row => row.value)
                      .filter(value => String(value).endsWith(' GiB'));
                    function capRange() {
                      const [cap, ...parts] = amounts('cap');
                      const small = parts.filter(value => value.startsWith('<')).length;
                      const tenths = value => Math.round(parseFloat(value) * 10);
                      const low = parts.filter(value => !value.startsWith('<'))
                        .reduce((sum, value) => sum + tenths(value), 0);
                      return [low, tenths(cap), low + small];
                    }
                """
                self.assertEqual(self.page_expression(wrapper + "return " + expression + "; })()", data, walk=True), expected)

    def test_memory_rounding_search_matches_exhaustive_choices(self) -> None:
        script = """(() => {
          function better(a, b) {
            if (a.order !== b.order) return a.order < b.order;
            if (a.sums !== b.sums) return a.sums < b.sums;
            if (Math.abs(a.distance - b.distance) > 1e-9) return a.distance < b.distance;
            const i = a.tie.findIndex((value, index) => value !== b.tie[index]);
            return i >= 0 && a.tie[i] > b.tie[i];
          }
          function verify(roots) {
            const nodes = [];
            function visit(node) {
              if (nodes.includes(node)) return;
              nodes.push(node);
              for (const [, child] of node.parts) visit(child);
              for (const sum of node.sums ?? []) sum.forEach(visit);
            }
            roots.forEach(visit);
            const units = nodes.map(node => node.unit ?? GIB);
            const raw = nodes.map((node, i) => typeof node.bytes === 'number' ? node.bytes / units[i] * 10 : 0);
            const ranges = raw.map((value, i) => units[i] === GIB && value && Math.abs(value) < 1
              ? [Math.min(0, Math.sign(value)), Math.max(0, Math.sign(value))] : [0, 0]);
            const options = raw.map((value, i) => {
              if (ranges[i].some(Boolean)) return [0];
              if (nodes[i].fixed) return [Math.sign(value) * Math.round(Math.abs(value))];
              return [...new Set([Math.floor(value), Math.ceil(value)])];
            });
            const sums = nodes.flatMap((node, i) =>
              [node.parts.map(([, child]) => child), ...(node.sums ?? [])].flatMap(parts => {
                const bytes = parts.reduce((sum, child) => sum + (typeof child.bytes === 'number' ? child.bytes : 0), 0);
                return parts.length && typeof node.bytes === 'number' && Math.abs(node.bytes - bytes) < 1
                  ? [[i, parts.map(child => nodes.indexOf(child))]] : [];
              }));
            function score(values) {
              let order = 0, badSum = 0;
              for (const [i, children] of sums) {
                for (const a of children) for (const b of children) {
                  if (nodes[a].bytes < nodes[b].bytes && values[a] * units[a] > values[b] * units[b]) ++order;
                }
                const margin = children.some(j => units[j] !== units[i])
                  ? Math.max(units[i], ...children.map(j => units[j])) / 2 : 0;
                const low = children.reduce((sum, j) => sum + (values[j] + ranges[j][0]) * units[j], 0);
                const high = children.reduce((sum, j) => sum + (values[j] + ranges[j][1]) * units[j], 0);
                if ((values[i] + ranges[i][1]) * units[i] + margin < low ||
                    (values[i] + ranges[i][0]) * units[i] - margin > high) ++badSum;
              }
              const distance = values.reduce((sum, value, i) =>
                sum + (ranges[i].some(Boolean) ? 0 : Math.abs(value - raw[i])), 0);
              return {order, sums: badSum, distance, tie: values.map(Math.abs)};
            }
            const shown = nodes.map((node, i) => {
              if (ranges[i].some(Boolean)) return node.text === (raw[i] < 0 ? '-<0.1 GiB' : '<0.1 GiB') ? 0 : NaN;
              return typeof node.bytes === 'number' ? Math.round(parseFloat(node.text) * 10) : 0;
            });
            if (shown.some((value, i) => !options[i].includes(value))) return false;
            const actual = score(shown);
            function search(values) {
              if (values.length === nodes.length) return !better(score(values), actual);
              return options[values.length].every(value => search([...values, value]));
            }
            return search([]);
          }
          const solve = memoryTotal;
          const misses = [];
          let name, searches = 0;
          memoryTotal = (...roots) => {
            ++searches;
            const result = solve(...roots);
            if (!verify(roots)) misses.push(name);
            return result;
          };
          for (const [label, reading] of data.cases) {
            name = label;
            const sample = {...reading, ram: reading.ram ?? data.ram};
            searches = 0;
            shownReading(sample);
            for (const row of sample.walk ?? []) tooltipRows('row:' + row.key, sample);
            for (const row of sample.waiters ?? []) tooltipRows('wait:' + row.key, sample);
            if (searches !== 1) misses.push(name + ' searches: ' + searches);
          }
          for (const [label, amounts] of [
            ['partial ceiling counts', [0.11, 0.14, 0.17, 0.201, 0.272, 0.293]],
            ['one ceiling count in order', [0.11, 0.14, 0.17, 0.12, 0.16, 0.13]],
          ]) {
            name = label;
            const parts = amounts.map(gib => memoryBreakdown(gib * GIB));
            const total = memoryBreakdown(sumMemory(parts.map(part => part.bytes)));
            addMemorySum(total, ...parts);
            const rows = parts.map(part => memoryBreakdown(part.bytes + GIB, [['Uses', part], ['Reserve', GIB]]));
            memoryTotal(total, ...rows);
          }
          return misses;
        })()"""
        cases = [(name, data) for name, _, data, _ in self.memory_rounding_cases()]
        self.assertEqual(self.page_expression(script, {"cases": cases}), [])

    def test_fractional_totals_and_negative_leaves_share_one_shown_value(self) -> None:
        ram: dict[str, object] = {**RAM_FIXTURE, "rust_analyzer_bytes": 1.02 * GIB, "sccache_bytes": 0,
               "other_build_tools_bytes": 0, "build_tools_bytes": 1.02 * GIB,
               "queued_builds_bytes": 0, "ci_bytes": 0, "build_covered_bytes": 1.02 * GIB, "build_target_bytes": 1.02 * GIB,
               "room_bytes": 1.02 * GIB, "desktop_bytes": 10 * GIB, "agents_bytes": 0,
               "programs": [], "smaller_programs_bytes": 0, "other_programs_bytes": 0,
               "compressed_swap_bytes": 0, "kernel_structures_bytes": 0, "kernel_counters_bytes": 0,
               "kernel_bytes": 0, "cache_bytes": 0, "unattributed_bytes": -GIB,
               "kernel_no_counter_bytes": 0, "available_bytes": 8.04 * GIB, "total_bytes": 18.06 * GIB}
        keys = ["tools", "g-builds", "cap", "walksum", "g-free", "g-else", "budget"]
        tips = cast(list[list[dict[str, object]]], self.page_expression(
            "[" + ",".join(f"tooltipRows('{key}',data)" for key in keys) + "]",
            {"ram": ram, "cap": 3.06, "taken": 2.04}))
        for key, rows in zip(keys, tips, strict=True):
            with self.subTest(key=key):
                amounts = [round(float(str(row["value"]).split()[0]) * 10)
                           for row in rows if str(row["value"]).endswith(" GiB")]
                self.assertEqual(amounts[0], sum(amounts[1:]))
        self.assertEqual(tips[0][0]["value"], tips[1][2]["value"])
        self.assertEqual(tips[2][0]["value"], "3.1 GiB")
        self.assertEqual(tips[3][0]["value"], "2.0 GiB")
        self.assertEqual(tips[5][0]["value"], "9.0 GiB")
        self.assertEqual(tips[5][-1]["value"], "-1.0 GiB")
        ram = {**RAM_FIXTURE, "total_bytes": round(51.103 * GIB), "available_bytes": round(24.051 * GIB),
               "build_tools_bytes": 0, "rust_analyzer_bytes": 0, "sccache_bytes": 0, "other_build_tools_bytes": 0,
               "queued_builds_bytes": round(0.052 * GIB), "ci_bytes": 0, "room_bytes": round(12.051 * GIB)}
        values = self.page_expression("tooltipRows('budget', data).filter(row=>row.kind!=='caption').map(row=>row.value)",
                                      {"ram": ram})
        self.assertEqual(values, ["51.1 GiB", "<0.1 GiB", "24.1 GiB", "27.0 GiB"])

    def test_seeded_memory_tooltips_use_the_shown_inputs(self) -> None:
        rng = Random(173)
        readings: list[dict[str, object]] = []

        def amount() -> float:
            return rng.randrange(1, 4000) / 1000

        def raw_sum(ram: Mapping[str, object], *keys: str) -> float:
            return sum(cast(float, ram[key]) for key in keys)

        for sample in range(72):
            ram = {**RAM_FIXTURE, **{key: amount() * GIB for key in (
                "rust_analyzer_bytes", "sccache_bytes", "other_build_tools_bytes", "queued_builds_bytes",
                "ci_bytes", "build_covered_bytes", "room_bytes", "safety_covered_bytes", "safety_target_bytes",
                "swap_covered_bytes", "desktop_bytes", "agents_bytes", "compressed_swap_bytes",
                "kernel_structures_bytes", "kernel_counters_bytes", "cache_bytes", "unattributed_bytes",
                "kernel_no_counter_bytes", "smaller_programs_bytes")}}
            ram["programs"] = [{"name": "Editor", "bytes": amount() * GIB}]
            ram["other_programs_bytes"] = sum(cast(float, program["bytes"]) for program in
                cast(list[dict[str, object]], ram["programs"])) + cast(float, ram["smaller_programs_bytes"])
            ram["kernel_bytes"] = sum(cast(float, ram[key]) for key in
                ("compressed_swap_bytes", "kernel_structures_bytes", "kernel_counters_bytes"))
            growth, share, between, unread = amount(), amount() * 25, amount(), amount()
            swap_in, swap_share = amount(), amount() * 25
            if sample == 0:
                growth, share, between, unread = 10.1, 80, 0, 0
                ram.update(build_covered_bytes=8.04 * GIB, room_bytes=0.1 * GIB)
            if sample == 1:
                ram.update(safety_covered_bytes=0, safety_target_bytes=0.051 * GIB, swap_covered_bytes=0)
                swap_in, swap_share = 0.051, 100
            if sample in (2, 3, 4):
                growth, share, between, unread = (10.04 if sample == 4 else 10.06), 80, 0, 0
                covered = 8.02 if sample == 4 else 8.048
                ram.update(build_covered_bytes=covered * GIB, room_bytes={3: 0.01, 4: 0.005}.get(sample, 0) * GIB)
                ram["safety_target_bytes"] = ram["safety_covered_bytes"]
                swap_in, swap_share = cast(float, ram["swap_covered_bytes"]) / GIB, 100
            if sample == 5:
                growth, share, between, unread = 8.049, 100, 0.249, 0.214
                ram.update(build_covered_bytes=8.451 * GIB, swap_covered_bytes=6.653 * GIB, room_bytes=0)
                swap_in, swap_share = 8.32, 80
            if sample == 6:
                growth, share, between, unread = 7.06, 100, 0.451, 0.451
                ram.update(build_covered_bytes=7.949 * GIB, room_bytes=0.02 * GIB)
            if sample in (7, 8, 9):
                desktop, adjustment = {7: (3.0, -0.5), 8: (0.149, -0.696), 9: (0.149, -0.64)}[sample]
                ram.update(desktop_bytes=desktop * GIB, agents_bytes=0.149 * GIB,
                           kernel_bytes=0.149 * GIB, compressed_swap_bytes=0.149 * GIB,
                           kernel_structures_bytes=0, kernel_counters_bytes=0, cache_bytes=0.149 * GIB,
                           other_programs_bytes=0.149 * GIB, programs=[{"name": "Editor", "bytes": 0.149 * GIB}],
                           smaller_programs_bytes=0, kernel_no_counter_bytes=0,
                           unattributed_bytes=adjustment * GIB)
            build_target = (growth * share / 100 + between + unread) * GIB
            if sample in (2, 3):
                build_target = cast(float, ram["build_covered_bytes"])
            ram.update(build_target_bytes=build_target, swap_target_bytes=swap_in * swap_share / 100 * GIB)
            shortfall = 0.0
            for key in ("build", "safety", "swap"):
                target = cast(float, ram[f"{key}_target_bytes"])
                covered = cast(float, ram[f"{key}_covered_bytes"])
                missing = max(0, target - covered)
                ram[f"{key}_missing_bytes"] = missing
                shortfall += missing
            ram["shortfall_bytes"] = shortfall
            ram["build_tools_bytes"] = raw_sum(ram, "rust_analyzer_bytes", "sccache_bytes", "other_build_tools_bytes")
            builds = raw_sum(ram, "build_tools_bytes", "queued_builds_bytes", "ci_bytes")
            ram["cap_bytes"] = builds + raw_sum(ram, "build_covered_bytes", "room_bytes")
            ram["available_bytes"] = raw_sum(ram, "build_covered_bytes", "room_bytes",
                                            "safety_covered_bytes", "swap_covered_bytes")
            ram["total_bytes"] = builds + raw_sum(ram, "available_bytes", "desktop_bytes", "agents_bytes",
                                                  "kernel_bytes", "cache_bytes", "other_programs_bytes", "unattributed_bytes")
            room = cast(float, ram["room_bytes"]) / GIB
            earlier = min(room, amount())
            need, free = amount(), amount()
            if sample in (2, 3, 4):
                need, free = {2: (1.0, 1.04), 3: (1.0, 1.0), 4: (1.04, 1.0)}[sample]
            head_free = room - earlier
            status = "gpu_slot"
            if sample % 3 == 2:
                status = "one_running_memory_wait" if need > free else "pressure"
            used, reserved = amount(), amount()
            group_parts = [{"who": name, "gib": amount()} for name in ("Editor", "Indexer")]
            readings.append({
                "cap": ram["cap_bytes"] / GIB, "read_at": 200, "ram": ram,
                "psi": {"some": 0, "full": 0}, "limits": {"some": 1, "full": 1}, "pressure": "none",
                "build_reserve": {"growth_bytes": growth * GIB, "share_percent": share,
                                  "between_steps_bytes": between * GIB, "unread_bytes": unread * GIB},
                "swap": {**SWAP_FIXTURE, "window_seconds": 600, "swap_in_window_bytes": swap_in * GIB,
                         "reserve_share_percent": swap_share},
                "swap_by": [{"name": "Swap", "kind": "total", "bytes": 3.01 * GIB},
                            {"name": "Difference", "kind": "difference", "bytes": -1.04 * GIB}],
                "walk": [{"key": "run", "name": "run", "queued": True, "used": used, "reserved": reserved},
                         {"key": "group", "name": "Group", "group": True,
                          "used": sum(cast(float, part["gib"]) for part in group_parts), "parts": group_parts}],
                "memory_status": {"code": status, "next_need_bytes": need * GIB, "check_room_bytes": free * GIB},
                "waiters": [{"key": "wait", "name": "wait", "since": 100, "need": need, "free": free, "first_in_line": True,
                             "why": "memory" if need > free else "its turn"},
                            {"key": "head", "name": "head", "since": 100, "need": need, "first_in_line": True,
                             "why": "memory" if need > head_free else "its turn",
                             "head_room": {"rows": [{}, {}, {}, {"label": "− Earlier waiters", "gib": earlier},
                                                    {"label": "= Head room", "gib": head_free}]}}],
            })

        totals = {
            "tools": "Build tools", "g-builds": "Builds", "g-free": "Free RAM", "system": "System reserved",
            "programs": "Other programs", "kernel": "Kernel", "unattributed": "Not attributed",
            "g-else": "Everything else", "cap": "Queue cap", "budget": "RAM",
        }
        result = cast(tuple[
            list[tuple[dict[str, dict[str, str]], list[bool], bool, bool, str, bool, str, str,
                       dict[str, object], dict[str, str]]],
            list[dict[str, str]],
        ], self.page_expression("""(() => {
          function render(reading) {
            elements.clear();
            const keys = [...Object.keys(TIPS), ...reading.walk.map(row => 'row:' + row.key),
              ...reading.waiters.map(row => 'wait:' + row.key), ...reading.swap_by.map((_, i) => 'swap:' + i),
              'swap-pool:builds_ci', 'swap-pool:outside'];
            const tips = Object.fromEntries(keys.map(key => [key,
              Object.fromEntries(tooltipRows(key, reading).filter(row => row.label).map(row => [row.label, row.value]))]));
            const shown = shownReading(reading);
            renderReserveStatus(shown);
            renderWalk(reading);
            $('room-why').textContent = statusLine(shown);
            const marks = RESERVES.map(([, , id]) => $(id).classes.has('short-reserve'));
            const warning = $('mac-idle-alert');
            const sum = $('walk-sum');
            const row = reading.walk[0];
            const capture = buildStepEvidence('row:run', reading, row, [], tooltipRows('row:run', reading), data.limits);
            const surfaces = Object.fromEntries([
              ...keys.map(key => [key, tipText(key, reading).join('\\n')]),
              ...[...elements].filter(([, node]) => node.textContent).map(([key, node]) => ['message:' + key, node.textContent]),
            ]);
            return [tips, marks, !warning.hidden, warning['data-tip'] === 'shortfall', warning.textContent,
              sum.classes.has('over-cap'), sum.textContent, statusLine(shown), capture.evidence.memory, surfaces];
          }
          const shown = data.readings.map(render);
          formatRamAmount = bytes => recorded(bytes) ? (bytes / GIB).toFixed(9) + ' GiB' : 'unavailable';
          const raw = data.readings.map(reading => render(structuredClone(reading)).at(-1));
          return [shown, raw];
        })()""", {"readings": readings, "limits": new_item_dialog.BUILD_EVIDENCE_LIMITS}, walk=True))
        results, raw_surfaces = result

        def tenths(text: str) -> int:
            if text in ("<0.1 GiB", "-<0.1 GiB"):
                return -1 if text.startswith("-") else 1
            return round(float(text.split()[0]) * 10)

        amount_pattern = re.compile(r"-?<?\d+\.\d+(?= GiB| of -?<?\d+\.\d+ GiB)")

        def shown_value(text: str) -> float:
            if text in ("<0.1", "-<0.1"):
                return -0.05 if text.startswith("-") else 0.05
            return float(text)

        def assert_sum(total: str, *parts: str, subtract: tuple[str, ...] = ()) -> None:
            def value(amount: str) -> float:
                return shown_value(amount.split()[0])

            expected = sum(map(value, parts)) - sum(map(value, subtract))
            tolerance = 0.05 * sum("<" in amount for amount in (total, *parts, *subtract))
            self.assertLessEqual(abs(value(total) - expected), tolerance + 1e-9, (total, parts, subtract))

        for sample, (tips, marks, warning, warning_tip, warning_text, overage, summary, gpu,
                     evidence, surfaces) in enumerate(results):
            with self.subTest(sample=sample):
                raw = cast(dict[str, float], readings[sample]["ram"])
                short = {key: raw[f"{key}_missing_bytes"] > 0 for key in ("safety", "swap", "build")}
                self.assertEqual(marks, list(short.values()))
                self.assertEqual(warning, any(short.values()))
                self.assertEqual(warning_tip, warning)
                self.assertEqual(evidence["free_for_next_gib"], raw["room_bytes"] / GIB)
                self.assertEqual(evidence["queue_cap_gib"], readings[sample]["cap"])
                for key, title in totals.items():
                    assert_sum(tips[key][title], *(value for label, value in tips[key].items() if label != title))
                taken, cap = tips["walksum"], tips["cap"]
                reserve = taken.get("+ Full reserve", taken.get("+ Build reserved", ""))
                raw_overage = (raw["build_missing_bytes"] - raw["room_bytes"]) / GIB
                expected_overage = raw_overage > 0
                self.assertEqual(overage, expected_overage)
                self.assertEqual("Over cap" in taken, expected_overage)
                cap_amount = cap["Queue cap"]
                assert_sum(taken["Taken"], taken["Builds"], reserve)
                expected_summary = f"{taken['Taken'].split()[0]} of {cap_amount} taken"
                if expected_overage:
                    assert_sum(taken["Taken"], cap_amount, taken["Over cap"])
                    expected_summary += f" · {taken['Over cap']} over cap"
                self.assertEqual(summary, expected_summary)
                if sample == 0:
                    self.assertEqual(tips["reserved"]["= Target"], "8.1 GiB")
                room_tip = tips["room"]
                if sample in (2, 3):
                    expected_reserve = "8.0 GiB" if sample == 2 else "8.1 GiB"
                    self.assertEqual(tips["reserved"]["Build reserved"], expected_reserve)
                    self.assertEqual(tips["reserved"]["= Target"], expected_reserve)
                    self.assertEqual(room_tip["Free"], "0.0 GiB" if sample == 2 else "<0.1 GiB")
                if sample == 3:
                    segments = cast(list[dict[str, object]], evidence["segments"])
                    self.assertIn({"label": "Free", "gib": 0.01, "tone": "free", "text": "<0.1 GiB"}, segments)
                assert_sum(room_tip["Free"], room_tip["Queue cap"],
                           subtract=(room_tip["− Builds"], room_tip["− Build reserved"]))
                for key, title, input_key in (
                    ("safety", "Safety net", ""),
                    ("swap", "Swap coming back", "Swap-in"),
                    ("reserved", "Build reserved", "Room to grow"),
                ):
                    reserve_tip = tips[key]
                    raw_key = "build" if key == "reserved" else key
                    target = reserve_tip.get("= Target", reserve_tip.get("Target", reserve_tip[title]))
                    if input_key:
                        growth = tenths(reserve_tip[input_key])
                        share_tenths = round(float(reserve_tip["× Share held"].rstrip("%")) * 10)
                        basis = (growth * share_tenths + 500) // 1000 + tenths(reserve_tip.get("+ Between steps", "0"))
                        basis += tenths(reserve_tip.get("+ Unread steps", "0"))
                        self.assertLessEqual(abs(tenths(target) - basis), 1)
                    shortage = reserve_tip.get("Not covered", "0")
                    self.assertEqual("Not covered" in reserve_tip, short[raw_key])
                    if raw_key != "build":
                        system_label = "+ " + title if raw_key == "swap" else title
                        self.assertEqual(tips["system"][system_label], reserve_tip[title])
                    self.assertEqual(shortage, tips["shortfall"].get(title, tips["shortfall"].get("+ " + title, "0")))
                assert_sum(tips["shortfall"]["Shortfall"], *(value for label, value in tips["shortfall"].items()
                                                             if label != "Shortfall"))
                for key, before in raw_surfaces[sample].items():
                    after = surfaces[key]
                    with self.subTest(surface=key):
                        self.assertEqual(amount_pattern.sub("#", before), amount_pattern.sub("#", after))
                        raw_amounts = [float(amount.group()) for amount in amount_pattern.finditer(before)]
                        shown_amounts = [shown_value(amount.group()) for amount in amount_pattern.finditer(after)]
                        self.assertEqual(len(raw_amounts), len(shown_amounts))
                        amounts = list(zip(raw_amounts, shown_amounts, strict=True))
                        for raw_value, shown_value_in_text in amounts:
                            self.assertEqual(raw_value > 0, shown_value_in_text > 0)
                            self.assertEqual(raw_value < 0, shown_value_in_text < 0)
                        for raw_value, shown_value_in_text in amounts:
                            if 0 < abs(raw_value) < 0.1:
                                self.assertEqual(abs(shown_value_in_text), 0.05)
                            else:
                                self.assertLess(abs(raw_value - shown_value_in_text), 0.100000001)
                if sample == 1:
                    self.assertEqual(tips["shortfall"]["Safety net"], "<0.1 GiB")
                    self.assertEqual(tips["shortfall"]["+ Swap coming back"], "<0.1 GiB")
                if sample == 4:
                    self.assertIn("<0.1 GiB over cap", summary)
                if sample == 5:
                    self.assertEqual(tips["reserved"]["= Target"], "8.5 GiB")
                    self.assertEqual(tips["swap"]["= Target"], "6.7 GiB")
                    self.assertIn("Build reserved holds 8.5 of 8.5 GiB.", warning_text)
                if sample in (7, 8, 9):
                    outside = tips["g-else"]
                    expected = {7: "3.1 GiB", 8: "<0.1 GiB", 9: "0.1 GiB"}
                    self.assertEqual(outside["Everything else"], expected[sample])
                    self.assertEqual(outside["+ Not attributed"], tips["unattributed"]["Not attributed"])
                run = tips["row:run"]
                assert_sum(run["= In queue cap"], run["Uses"], run["+ Build reserved"])
                group = tips["row:group"]
                assert_sum(group["Group"], group["Editor"], group["Indexer"])
                wait, head = tips["wait:wait"], tips["wait:head"]
                waiter, head_waiter = cast(list[dict[str, object]], readings[sample]["waiters"])
                need, free = cast(float, waiter["need"]), cast(float, waiter["free"])
                head_terms = cast(dict[str, list[dict[str, object]]], head_waiter["head_room"])["rows"]
                head_free = cast(float, head_terms[-1]["gib"])
                assert_sum(head["= Head room"], head["Queue cap"],
                           subtract=(head["− Builds"], head["− Build reserved"], head["− Earlier waiters"]))
                for tip, free_for_step in ((wait, free), (head, head_free)):
                    shortage = need - free_for_step
                    self.assertEqual(tip["Waits for"], "memory" if shortage > 0 else "its turn")
                    self.assertEqual("Short by" in tip, shortage > 0)
                if "Short by" in head:
                    assert_sum(head["Needs"], head["= Head room"], head["Short by"])
                difference = raw["room_bytes"] / GIB - free
                self.assertEqual("− Held elsewhere" in wait, difference > 0)
                self.assertEqual("+ Released" in wait, difference < 0)
                gpu_reason = "A GPU test waits: another GPU test holds the GPU test slot."
                if need > free:
                    gpu_reason += f" It also needs {wait['Short by']} more memory."
                if sample % 3 != 2:
                    self.assertEqual(gpu, gpu_reason)

    def test_taken_counts_the_full_reserve_above_the_cap(self) -> None:
        ram = {**RAM_FIXTURE, "build_covered_bytes": 6 * GIB, "build_missing_bytes": GIB,
               "build_target_bytes": 7 * GIB, "room_bytes": 0}
        data = {"ram": ram, "cap": 13, "taken": 13}
        rows = cast(list[dict[str, object]], self.page_expression("tooltipRows('walksum', data)", data))
        self.assertEqual([(row["label"], row["value"]) for row in rows], [
            ("Taken", "14.0 GiB"), ("", "Memory used or held by builds"),
            ("Builds", "7.0 GiB"), ("+ Full reserve", "7.0 GiB"),
            ("Queue cap", "13.0 GiB"), ("Over cap", "1.0 GiB")])
        fields = {str(row["label"]): float(str(row["value"]).split()[0])
                  for row in rows if str(row["value"]).endswith(" GiB")}
        self.assertEqual(fields["Taken"], fields["Builds"] + fields["+ Full reserve"])
        self.assertEqual(fields["Over cap"], fields["Taken"] - fields["Queue cap"])
        self.assertEqual(self.page_expression("tipText('reserved', data)[0]", data), "Build reserved  6.0 GiB")

    def test_swap_tooltip_uses_the_feed_share_before_the_target(self) -> None:
        for share in (20, 65, None):
            swap = {**SWAP_FIXTURE}
            if share is None:
                del swap["reserve_share_percent"]
            else:
                swap["reserve_share_percent"] = share
            with self.subTest(share=share):
                rows = cast(list[dict[str, object]], self.page_expression("tooltipRows('swap',data)", {"swap": swap,
                    "ram": {**RAM_FIXTURE, "swap_target_bytes": 3 * GIB, "swap_missing_bytes": GIB}}))
                labels = [row["label"] for row in rows]
                if share is None:
                    self.assertNotIn("Share held", labels)
                else:
                    self.assertEqual(rows[labels.index("Share held")]["value"], f"{share:.1f}%")
                    self.assertLess(labels.index("Share held"), labels.index("Target"))

    def test_a_small_reserve_shortage_uses_the_small_amount_label(self) -> None:
        for missing in (20 * MIB, round(0.01 * GIB), round(0.002 * GIB)):
            with self.subTest(missing=missing):
                ram = {**RAM_FIXTURE, "build_covered_bytes": 0, "build_target_bytes": missing,
                       "build_missing_bytes": missing, "shortfall_bytes": missing}
                shown = self.page_expression(
                    "[reserveAlert(shownReading(data)), tipText('reserved',data)[1], tipText('shortfall',data)[0], "
                    + "capUnavailableNote(shownReading(data)).split('; ').at(-1)]",
                    {"cap": None, "ram": ram})
                self.assertEqual(shown, [
                    "Free RAM is short by <0.1 GiB. Build reserved holds 0.0 of <0.1 GiB.",
                    "Held for running builds to grow into\nTarget  <0.1 GiB\nNot covered  <0.1 GiB",
                    "Shortfall  <0.1 GiB",
                    "shortfall <0.1 GiB",
                ])

    def test_reserve_tooltips_show_the_basis_of_each_target(self) -> None:
        # The user, 11-17: the swap tooltip shows the swap-in it takes a share of; build reserved shows its share of growth.
        swap = {**SWAP_FIXTURE, "reserve_share_percent": 20, "window_seconds": 600, "swap_in_window_bytes": 3 * GIB}
        # The fixture gives each reserve a complete reading.
        ram = {**RAM_FIXTURE, "swap_target_bytes": 3 * GIB * 20 // 100, "swap_covered_bytes": 3 * GIB * 20 // 100,
               "build_target_bytes": 10 * GIB, "build_covered_bytes": 10 * GIB}
        for basis, expected in (
            ({"growth_bytes": 10 * GIB, "share_percent": 80, "between_steps_bytes": 2 * GIB, "unread_bytes": 0},
             [("Room to grow", "10.0 GiB"), ("× Share held", "80.0%"), ("+ Between steps", "2.0 GiB"), ("= Target", "10.0 GiB")]),
            ({"growth_bytes": 5 * GIB, "share_percent": 80, "between_steps_bytes": 0, "unread_bytes": 6 * GIB},
             [("Room to grow", "5.0 GiB"), ("× Share held", "80.0%"), ("+ Unread steps", "6.0 GiB"),
              ("= Target", "10.0 GiB")]),
        ):
            with self.subTest(basis=basis):
                rows = cast(list[dict[str, object]], self.page_expression(
                    "tooltipRows('reserved', data)", {"ram": ram, "build_reserve": basis}))
                self.assertEqual([(row["label"], row["value"]) for row in rows[2:]], expected)
        rows = cast(list[dict[str, object]], self.page_expression("tooltipRows('swap', data)", {"ram": ram, "swap": swap}))
        self.assertEqual([(row["label"], row["value"]) for row in rows[2:]],
                         [("Lookback", "10 min"), ("Swap-in", "3.0 GiB"), ("× Share held", "20.0%"), ("= Target", "0.6 GiB")])
        no_target = cast(list[dict[str, object]], self.page_expression("tooltipRows('reserved', data)", {
            "ram": {**RAM_FIXTURE, "build_target_bytes": 0, "build_covered_bytes": 0},
            "build_reserve": {"growth_bytes": 0, "share_percent": 80, "between_steps_bytes": 0, "unread_bytes": 0}}))
        self.assertEqual(len(no_target), 2)
        self.assertEqual(self.page_expression("reserveAlert(shownReading(data))", {}), "")

    def test_reserve_targets_appear_only_above_the_covered_amount(self) -> None:
        for key, reserve, title in (("reserved", "build", "Build reserved"),
                                    ("safety", "safety", "Safety net"),
                                    ("swap", "swap", "Swap coming back"),
                                    ("swap-pool:outside", "swap", "Outside builds and CI")):
            for target in (2 * GIB, 3 * GIB):
                with self.subTest(key=key, target=target):
                    data = {"ram": {**RAM_FIXTURE, f"{reserve}_covered_bytes": 2 * GIB,
                                    f"{reserve}_target_bytes": target,
                                    f"{reserve}_missing_bytes": target - 2 * GIB}}
                    rows = cast(list[dict[str, object]], self.page_expression(f"tooltipRows('{key}',data)", data))
                    fields = {row["label"]: row["value"] for row in rows}
                    self.assertEqual(fields[title], "2.0 GiB")
                    if key == "swap":
                        self.assertEqual(fields["Share held"], "50.0%")
                    if target == 2 * GIB:
                        self.assertNotIn("Target", fields)
                    else:
                        self.assertEqual(fields["Target"], "3.0 GiB")
                        self.assertNotEqual(fields["Target"], fields[title])

    def test_swap_pool_uses_the_covered_legend_amount_and_names_the_target(self) -> None:
        data = {"ram": {**RAM_FIXTURE, "swap_covered_bytes": GIB, "swap_target_bytes": 2 * GIB, "swap_missing_bytes": GIB}}
        rows = cast(list[dict[str, object]], self.page_expression("tooltipRows('swap-pool:outside', data)", data))
        fields = {row["label"]: row["value"] for row in rows}
        self.assertEqual(fields["Swap coming back"], "1.0 GiB")
        self.assertEqual(fields["Target"], "2.0 GiB")

    def test_heading_keeps_all_structured_priority_entries(self) -> None:
        data = {"unmatched_priority_entries": [{"unit": "research", "why": "first"},
                                                {"unit": "cables", "why": "second"}],
                "unmatched_priority_text": "text is not parsed"}
        heading = self.page_expression("tipText('walkhead', data)", data)
        self.assertIn("Priority  research, cables", str(heading))
        self.assertNotIn("text is not parsed", str(heading))

    def test_equal_group_members_keep_their_process_identity(self) -> None:
        data = {"read_at": 220, "walk": [{"name": "Codex servers", "group": "codex", "used": 0.4,
            "about": "These processes serve Codex sessions",
            "parts": [{"who": "codex", "pid": pid, "started_at": 100, "gib": 0.2} for pid in (123, 456)]}]}
        html = str(self.page_expression("tooltipHtml('row:fixture-run-0', data)", data, walk=True))
        root = ElementTree.fromstring(html)
        self.assertEqual(root.findtext("dd[@class='caption']"), "These processes serve Codex sessions")
        self.assertEqual([cell.text for cell in root.findall("dt")][2:], ["codex · 123 · 2 min", "codex · 456 · 2 min"])
        self.assertEqual([cell.text for cell in root.findall("dd")][3:], ["0.2 GiB", "0.2 GiB"])

    def test_a_hidden_console_frame_reads_nothing_until_the_console_shows_it(self) -> None:
        html = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        loop = html[(start := html.index("const consoleHidden =")):html.index("</script>", start)]
        javascript = """
const calls = [];
const listeners = [];
const parent = {name: "console"};
const location = {origin: "http://natedev:8080", pathname: "/memory_live.html"};
const window = {frameElement: JSON.parse(process.argv[1])};
const SECONDS = 5;
const fetch = url => { calls.push(String(url).split("?")[0]); return new Promise(() => {}); };
const setInterval = () => 0, clearInterval = () => {}, setTimeout = () => 0, clearTimeout = () => {};
const performance = {now:()=>0,getEntriesByType:()=>[]};
const document = {visibilityState:'visible',querySelector:()=>null,addEventListener:()=>{}};
const addEventListener = (kind, handler) => { listeners.push(handler); };
window.addEventListener = addEventListener;
const listener = event => listeners.forEach(run=>run(event));
""" + page_frame.REFRESH_SCRIPT + loop + """
const loaded = calls.length;
listener({source: {}, origin: location.origin, data: "console-shown"});
listener({source: parent, origin: "http://other", data: "console-shown"});
const foreign = calls.length;
if (window.frameElement) window.frameElement.hidden = false;
listener({source: parent, origin: location.origin, data: "console-shown"});
process.stdout.write(JSON.stringify([loaded, foreign, calls.length, calls.slice(0, 2)]));
"""
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        reads = ["memory.json", "/memory_live.html"]
        for frame, expected in (("null", [2, 2, 4, reads]), ('{"hidden": false}', [2, 2, 4, reads]),
                                ('{"hidden": true}', [0, 0, 2, reads])):
            with self.subTest(frame=frame):
                result = subprocess.run([executable, "-e", javascript, frame], text=True, capture_output=True,
                    timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)

    def test_page_reload_compares_the_loaded_page_on_start_and_on_visibility(self) -> None:
        html = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        loop = html[(start := html.index("const consoleHidden =")):html.index("</script>", start)]
        javascript = """
const [initial, shown] = JSON.parse(process.argv[1]);
let stamp = initial, reloads = 0;
const document = {visibilityState: "hidden", querySelector: selector =>
  selector === 'meta[name="builds-page-version"]' ? {content: "v1"} : null};
const location = {pathname: "/memory_live.html", reload: () => { reloads++; }};
const window = {frameElement: null};
const visibility = [];
document.addEventListener = (kind, handler) => { if (kind === "visibilitychange") visibility.push(handler); };
const addEventListener = () => {};
const page = {refresh() {}};
const createPageRefresh = () => page;
const setInterval = () => 0;
const fetch = async () => {
  const version = stamp;
  return {ok: true, headers: {get: name => name === "X-Builds-Page-Version" ? version : null}};
};
""" + loop + """
async function check() {
  await Promise.resolve();
  const atStart = reloads;
  stamp = shown;
  document.visibilityState = "visible";
  for (const handler of visibility) handler();
  await Promise.resolve();
  const atVisibility = reloads;
  await reloadWhenPageChanges();
  process.stdout.write(JSON.stringify([atStart, atVisibility, reloads]));
}
void check();
"""
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        current, changed = "v1", "v2"
        for initial, shown, expected in ((current, current, [0, 0, 0]), (current, changed, [0, 1, 1]),
                                         (changed, changed, [1, 1, 1]), (None, current, [0, 0, 0])):
            with self.subTest(initial=initial, shown=shown):
                result = subprocess.run([executable, "-e", javascript, json.dumps([initial, shown])],
                    text=True, capture_output=True,
                    timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)

    def test_space_pauses_every_read_and_space_again_resumes(self) -> None:
        html = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        self.assertIn('<div class="paused-badge" id="paused" role="status" hidden>', html)
        loop = html[(start := html.index("const consoleHidden =")):html.index("</script>", start)]
        javascript = """
const calls = [];
const handlers = {};
const parent = {name: "console"};
const location = {origin: "http://natedev:8080", pathname: "/memory_live.html"};
const window = {frameElement: null};
const SECONDS = 5;
const fetch = url => { calls.push(String(url).split("?")[0]); return new Promise(() => {}); };
const setInterval = () => 0, clearInterval = () => {}, setTimeout = () => 0, clearTimeout = () => {};
const performance = {now:()=>0,getEntriesByType:()=>[]};
const parts = {};
const line = {dataset:{statusOptions:'{}'},querySelectorAll:()=>[],querySelector:selector=>
  parts[selector] ||= {hidden:false,textContent:'',classList:{toggle(){}}}};
const document = {visibilityState:'visible',querySelector:()=>line,addEventListener:()=>{}};
const badge = {hidden: true};
const $ = () => badge;
const listeners = {};
const addEventListener = (kind, handler) => {
  (listeners[kind] ||= []).push(handler);
  handlers[kind] = event => listeners[kind].forEach(run=>run(event));
};
window.addEventListener = addEventListener;
""" + page_frame.REFRESH_SCRIPT + loop + """
const steps = [];
const record = label => steps.push([label, calls.length, badge.hidden]);
const outside = {closest: () => null};
const press = (target, extra) => handlers.keydown({code: "Space", repeat: false, target, preventDefault() {}, ...extra});
record("loaded");
press(outside, {});
tick();
reloadWhenPageChanges();
record("paused");
if (line.dataset.state !== "Paused") throw new Error("Space must set the shared status to Paused.");
if (parts['[data-status-reason]'].textContent !== "Press space to resume.")
  throw new Error("The shared status must show the pause reason.");
const typed = [];
press({closest: selector => { typed.push(selector); return {}; }}, {});
press(outside, {repeat: true});
press(outside, {ctrlKey: true});
record("ignored");
if (line.dataset.state !== "Paused") throw new Error("A control must keep the shared status Paused.");
handlers.message({source: {}, origin: location.origin, data: "console-space"});
record("foreign");
handlers.message({source: parent, origin: location.origin, data: "console-space"});
record("resumed");
if (line.dataset.state === "Paused") throw new Error("Resume must clear the shared pause status.");
steps.push(typed);
process.stdout.write(JSON.stringify(steps));
"""
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        result = subprocess.run([executable, "-e", javascript], text=True, capture_output=True,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), [["loaded", 2, True], ["paused", 2, False],
            ["ignored", 2, False], ["foreign", 2, False], ["resumed", 4, True],
            ["input, textarea, select, button, summary, [contenteditable]"]])

    def test_aws_cost_hover_uses_the_label_and_value_grid(self) -> None:
        cases = (
            ("(awsCostTiles(data, awsSum(data.days), awsLimitTips), tooltipHtml('aws-cost:day-limit', data))",
             [("Daily limit", ""), ("Limit", "$35.00"), ("New work stops at", "$32.00"),
              ("Starts again", "Thu Nov 19, 7:00 PM EST")]),
            ("(awsCostTiles(data, awsSum(data.days), awsLimitTips), tooltipHtml('aws-cost:month-limit', data))",
             [("Monthly limit", ""), ("Limit", "$400.00"), ("New work stops at", "$397.00"),
              ("Starts again", "Mon Nov 30, 7:00 PM EST")]),
            ("(awsPeriodTips.set('day', awsDateRange('Sun Nov 1', awsDate('2026-11-01'),"
             + " awsDate('2026-11-02'))), tooltipHtml('aws-cost:day', data))",
             [("Sun Nov 1 (UTC)", ""), ("Starts", "Sat Oct 31, 8:00 PM EDT"), ("Ends", "Sun Nov 1, 7:00 PM EST")]),
        )
        for expression, expected in cases:
            with self.subTest(title=expected[0]):
                grid = ElementTree.fromstring(str(self.page_expression(expression, aws_cost_fixture(), walk=True)))
                self.assertEqual((grid.tag, grid.get("class")), ("dl", "tip"))
                labels, values = grid.findall("dt"), grid.findall("dd")
                self.assertEqual((labels[0].get("class"), values[0].get("class")), ("title", "title"))
                fields = [(label.text or "", value.text or "") for label, value in zip(labels, values, strict=True)]
                self.assertEqual(fields, expected)

    def test_aws_cost_breakdowns_use_bill_day_states_and_recorded_job_and_unit_names(self) -> None:
        estimate, units, billed, no_attempt, older, zero = cast(list[str], self.page_expression(
            "[awsDayDetail(data.days[0], data.price_day, {groupBy:'job', openGroups:new Set(['[\"hana\"]'])}),"
            + " awsDayRuns(data.days[0], {groupBy:'unit', openGroups:new Set()}),"
            + " awsDayDetail(data.days[2], data.price_day),"
            + """ awsDayDetail(data.days[12], data.price_day, {groupBy:'job', openGroups:new Set(['["hana"]'])}),"""
            + " awsDayDetail(data.days[16], data.price_day), awsDayDetail(data.days[17], data.price_day)]",
            aws_cost_fixture(), walk=True))
        for words in ("Hana check and lint", "integrate", "For one run", "Run time", "Group by", "Download CSV",
                      "us-east-1", "c7i.8xlarge", "Spot", "9 min 34 s", "newest AWS amount (Nov 17)", "~$5.25", "~$1.27"):
            self.assertIn(words, estimate)
        self.assertIn("<td class=\"num\">~$1.52</td><td class=\"num\">~$0.018</td>", estimate)
        self.assertIn('Hana check and lint</button></td><td class="num">7</td><td class="num">21 s</td>'
                      + '<td class="num">~$0.78</td><td class="num">—</td>', estimate)
        self.assertRegex(estimate, r'Hana machine with no run</td>.*<td class="num">2 h 22 min 55 s</td>.*~\$0\.30</td>')
        self.assertRegex(estimate, r'Total</td><td class="num">193</td><td class="num">29 h 39 min 45 s</td>')
        self.assertIn('Unit not recorded</button><span class="aws-dim aws-hint">Run records before this release have no unit.',
                      units)
        self.assertRegex(units, r'Hana machine with no run</td>.*<td class="num">2 h 22 min 55 s</td>.*~\$0\.30</td>')
        self.assertRegex(units, r'Costs with no run.*<td class="num">~\$1\.27</td>')
        self.assertIn("Job not recorded", billed)
        self.assertNotIn("Our estimate before the AWS amount", billed)
        self.assertNotIn("Start (Eastern)", billed)
        self.assertIn('Hana check and lint</button></td><td class="num">—</td><td class="num">—</td>', no_attempt)
        self.assertRegex(no_attempt, r'Hana machine with no run</td>.*<td class="num">—</td>.*\$1\.03</td>')
        self.assertRegex(no_attempt, r'Compute with no run record</td>.*\$4\.24</td>')
        self.assertRegex(no_attempt, r'Costs with no run.*<td class="num">\$1\.60</td>')
        self.assertNotIn("with no run", zero)
        self.assertIn("no record", older)
        for detail in (estimate, billed, older):
            self.assertNotRegex(detail, r"(?i)\bgate\b|targeted test")

    def test_aws_cost_day_with_no_unit_leaves_out_the_unit_column_and_names_the_first_unit_day(self) -> None:
        first, no_first, no_unit, no_unit_job, unit_day, no_record = cast(list[str], self.page_expression(
            "[awsFirstUnitDay(data.days), awsFirstUnitDay(data.days.slice(1)),"
            + " awsDayDetail(data.days[2], data.price_day, {groupBy:'none', openGroups:new Set()}, '2026-11-19'),"
            + """ awsDayRuns(data.days[2], {groupBy:'job', openGroups:new Set(['["job",null]'])}),"""
            + " awsDayDetail(data.days[0], data.price_day, {groupBy:'none', openGroups:new Set()}, '2026-11-19'),"
            + " awsDayDetail(data.days[2], data.price_day, undefined, null)]",
            aws_cost_fixture(), walk=True))
        hana_only, no_run, no_run_no_first = cast(list[str], self.page_expression(
            "[awsDayDetail({...data.days[0], day:'2026-11-20', runs_list:data.days[0].runs_list.filter(run => run.kind === 'hana')},"
            + " data.price_day, undefined, '2026-11-19'),"
            + " awsDayDetail({...data.days[2], day:'2026-11-21', runs:0, runs_list:[]}, data.price_day, undefined, '2026-11-19'),"
            + " awsDayDetail({...data.days[2], runs:0, runs_list:[]}, data.price_day, undefined, null)]",
            aws_cost_fixture(), walk=True))
        self.assertEqual((first, no_first), ("2026-11-19", None))
        for detail in (no_unit, no_unit_job):
            self.assertNotIn(">Unit</th>", detail)
            self.assertNotIn("Unit not recorded", detail)
            # Each run row has one cell for each header.
            table = detail[detail.index('<table class="aws-table"><thead>', detail.index("aws-run-scroll")):]
            row = table[table.index("<tbody>"):table.index("</tr>", table.index("<tbody>"))]
            self.assertEqual(row.count("<td"), table[:table.index("</thead>")].count("<th "))
        self.assertNotIn('data-aws-group-by="unit"', no_unit)
        self.assertIn('<p class="aws-note aws-unit-note">Runs before Thu Nov 19 have no unit record.</p>', no_unit)
        self.assertIn(">Unit</th>", unit_day)
        self.assertIn('data-aws-group-by="unit"', unit_day)
        self.assertNotIn("no unit record", unit_day)
        self.assertIn('<p class="aws-note aws-unit-note">Runs have no unit record.</p>', no_record)
        # A Hana-only day and a day with no run have no unit line.
        for detail in (hana_only, no_run, no_run_no_first):
            self.assertNotIn("aws-unit-note", detail)

    def test_aws_cost_hana_idle_row_shows_the_machine_time_with_no_run(self) -> None:
        details = cast(list[str], self.page_expression(
            "[data.days[0], data.days[12], {...data.days[12], hana:{...data.days[12].hana, idle_seconds:3521}}]"
            + """.map(day => awsDayRuns(day, {groupBy:'job', openGroups:new Set(['["hana"]'])}))""",
            aws_cost_fixture(), walk=True))
        cells = r"((?:<td[^>]*>[^<]*</td>)+)</tr>"
        # The cell before the cost is the Time on AWS cell of the idle row, and the Run time cell of the group row.
        idle = [re.findall(r"<td[^>]*>([^<]*)</td>", match[1])[-2] if (match := re.search(
            "<td>Hana machine with no run</td>" + cells, detail)) else None for detail in details]
        group = [re.findall(r"<td[^>]*>([^<]*)</td>", match[1])[1] if (match := re.search(
            "Hana check and lint</button></td>" + cells, detail)) else None for detail in details]
        self.assertEqual(idle, ["2 h 22 min 55 s", "—", "58 min 41 s"])
        # A group with no run has no run time, as other groups with no run.
        self.assertEqual(group, ["21 s", "—", "—"])

    def test_aws_cost_times_use_the_user_zone(self) -> None:
        for zone, rule, start, header in (
            ("America/Los_Angeles", "In Pacific time, a day starts at <b>4:00 PM on the day before</b> and ends at 4:00 PM.",
             "Sun Nov 22, 4:00 PM PST", "Start (Pacific)"),
            ("America/New_York", "In Eastern time, a day starts at <b>7:00 PM on the day before</b> and ends at 7:00 PM.",
             "Sun Nov 22, 7:00 PM EST", "Start (Eastern)"),
            ("Europe/Berlin", "In Central European time, a day starts at <b>1:00 AM</b> and ends at 1:00 AM on the next day.",
             "Mon Nov 23, 1:00 AM GMT+1", "Start (Central European)"),
        ):
            with self.subTest(zone=zone):
                actual = cast(list[str], self.page_expression(
                    "[awsDayRule('2026-11-23'), awsDateTime(awsDate('2026-11-23')), awsRunHeaders()[0]]",
                    {"zone": zone}))
                self.assertEqual([" ".join(actual[0].split()), *actual[1:]], [rule, start, header])

    def test_aws_cost_tile_names_a_late_read_and_its_recorded_error(self) -> None:
        read_at = int(datetime(2026, 11, 19, 21, 43, tzinfo=UTC).timestamp())
        due_at = read_at + 86400
        cases = (
            (due_at - 60, None, "The next AWS read is due Fri Nov 20, 1:43 PM PST"),
            (due_at + 60, None, "No AWS read since Nov 19"),
            (due_at + 60, "AWS refuses the key", "No AWS read since Nov 19: AWS refuses the key"),
        )
        for now, error, expected in cases:
            with self.subTest(expected=expected):
                actual = self.page_expression(f"(Date.now = () => {now * 1000}, awsNextRead(data))", {
                    "zone": "America/Los_Angeles", "read_at": read_at, "next_read_at": due_at, "read_error": error},
                    walk=True)
                self.assertEqual(actual, expected)

    def test_aws_cost_history_shows_zero_runs_as_a_count(self) -> None:
        days: list[dict[str, object]] = [{"day": "2026-11-21", "state": "estimate", "amount_usd": "1.21", "runs": 0, "runs_list": []},
                {"day": "2026-11-20", "state": "none", "amount_usd": None, "runs": 0, "runs_list": []},
                {"day": "2026-11-19", "state": "none", "amount_usd": None, "runs": 5, "runs_list": []}]
        cells = cast(list[str], self.page_expression(
            "awsPeriods('day', data.days).map(row => awsHistoryRow(row, 0, data, {day:121}, new Map(), new Map())"
            + ".match(/<td class=\"num[^\"]*\">([^<]*)<\\/td>/)[1])",
            {"zone": "America/New_York", "today": "2026-11-21", "records_start": "2026-11-19", "price_day": None,
             "days": days}, history=True))
        self.assertEqual(cells, ["0", "no record", "5"])

    def test_aws_cost_csv_joins_run_parts_across_bill_days(self) -> None:
        fixture = aws_cost_fixture()
        csv_text = cast(str, self.page_expression("awsRunsCsv(data.days)", fixture, walk=True))
        # The run that crosses midnight has one part on Nov 17 and one on Nov 18. Its CSV row costs both parts.
        parts = [run for day in cast(list[dict[str, list[dict[str, object]]]], fixture["days"])
                 for run in day["runs_list"] if run["id"] == "midnight"]
        joined_cents = sum(round(float(str(run["cost_usd"])) * 100) for run in parts)
        self.assertEqual((len(parts), joined_cents), (2, 5))
        lines = csv_text.splitlines()
        self.assertEqual(lines[0], '"UTC day","Start (Eastern)","Job","Unit","Region","Zone","Machine type",'
                                    + '"Time on AWS (seconds)","Cost (USD)","Mode"')
        self.assertEqual(len(lines), 2987)
        self.assertTrue(lines[1].startswith('"2026-11-19","2026-11-19T04:07:00-05:00",'))
        self.assertIn('"integrate","report","us-east-1","us-east-1a","c7i.8xlarge",574,0.02,"Spot"', lines[1])
        self.assertRegex(csv_text, r'"m7a\.8xlarge",574,\d+\.\d{2},"On-Demand"')
        self.assertIn('"2026-11-17","2026-11-17T18:59:00-05:00","","","us-east-1","","c7i.8xlarge",120,0.05,"Spot"',
                      lines)
        self.assertIn('"2026-11-08"', lines[-1])
        self.assertNotIn("Costs with no run", csv_text)
        self.assertNotIn("Hana machine with no run", csv_text)

    def test_aws_cost_period_sums_use_utc_weeks_and_clip_a_week_under_its_month(self) -> None:
        actual = self.page_expression("""(() => {
          const totals = rows => rows.map(row => [awsPeriodName(row), awsAmount(row), row.runs]);
          const months = awsPeriods('month', data.days);
          return {
            weeks: totals(awsPeriods('week', data.days)),
            months: totals(months),
            years: totals(awsPeriods('year', data.days)),
            clipped: months.map(month => totals(awsPeriods('week', month.days, month))),
          };
        })()""", {"days": [
            {"day": "2027-01-02", "amount_usd": "0.30", "runs": 2, "state": "estimate"},
            {"day": "2027-01-01", "amount_usd": "0.00", "runs": 0, "state": "billed"},
            {"day": "2026-12-31", "amount_usd": "0.10", "runs": 3, "state": "final"},
            {"day": "2026-12-30", "amount_usd": None, "runs": 0, "state": "none"},
            {"day": "2026-12-27", "amount_usd": "0.20", "runs": 1, "state": "billed"},
        ]}, walk=True)
        self.assertEqual(actual, {
            "weeks": [["Dec 28 to Jan 3", "~$0.40", 5], ["Dec 21 to Dec 27", "$0.20", 1]],
            "months": [["January 2027", "~$0.30", 2], ["December 2026", "$0.30", 4]],
            "years": [["2027", "~$0.30", 2], ["2026", "$0.30", 4]],
            "clipped": [[["Jan 1 to Jan 3", "~$0.30", 2]],
                        [["Dec 28 to Dec 31", "$0.10", 3], ["Dec 21 to Dec 27", "$0.20", 1]]],
        })

    def page_expression(self, expression: str, data: object, *, walk: bool = False, swap: bool = False,
                        limits: bool = False, queue_move: bool = False, history: bool = False) -> object:
        html = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        functions = html[html.index("function field("):html.index("const tip =")]
        if history:
            functions += html[html.index("function awsCurrentNote("):html.index("function renderAwsCosts(")]
        constants = ""
        if walk:
            functions += html[html.index("function rowAdd("):html.index("const queueMovePath =")]
            start = html.index("function renderWalk(d)")
            functions += html[start:html.index("document.addEventListener('click'", start)]
        if limits:
            functions += html[html.index("function limitMarks("):html.index('$("build-limits").addEventListener')]
            constants += f"const buildLimitIcons = {json.dumps(memory_feed.build_limit_icons())};\n"
        if walk or limits:
            functions += html[html.index("function esc(s)"):html.index("const consoleHidden =")]
            constants += """
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {style: {}, classes, classList: {toggle(name, active) {
      if (active) classes.add(name);
      else classes.delete(name);
    }}, setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; },
      open: true, querySelector: () => ({})});
  }
  return elements.get(id);
};
function fitLabels() {}
"""
        if queue_move:
            start = html.index("const queueMovePath =")
            functions += html[start:html.index('$("queue-action").addEventListener', start)]
            constants += """
const document = {activeElement:null};
let painted;
renderWalk = reading => { painted = reading; };
"""
        if swap:
            start = html.index("const swapView =")
            functions += html[start:html.index('document.addEventListener("click", (e) => {', start)]
            constants += "function placeLimitLabels() {}\nconst document = {querySelectorAll: () => []};\n"
        javascript = functions + constants + "\nconst data = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n" + (
            "if (data.zone) awsZone = data.zone;\n") + (
            "process.stdout.write(JSON.stringify(" + expression + "));\n")
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        payload: dict[str, object] = {"walk": [], "waiters": [], "swap_by": [], "ram": RAM_FIXTURE,
                                      "swap": SWAP_FIXTURE, **cast(dict[str, object], data)}
        for kind, rows in (("run", payload["walk"]), ("wait", payload["waiters"])):
            for index, row in enumerate(cast(list[dict[str, object]], rows)):
                _ = row.setdefault("key", f"fixture-{kind}-{index}")
                _ = row.setdefault("title", {"unit": "", "step": row.get("ci_label") or row["name"]})
                _ = row.setdefault("unit", "")
        with tempfile.TemporaryDirectory(prefix="memory-hover-") as directory:
            script = Path(directory) / "fixture.cjs"
            _ = script.write_text(javascript, encoding="utf-8")
            result = subprocess.run([executable, str(script)], input=json.dumps(payload),
                text=True, capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return cast(object, json.loads(result.stdout))

    def test_each_wait_state_has_a_page_hover_reason(self) -> None:
        for reason, expected in ((admission_records.PressureUnavailable("cannot read PSI"), "memory"),
                                  (admission_records.StartSpacingWait(110), "its turn"),
                                  (admission_records.BehindHead("first", 1, "head-session"), "its turn")):
            with self.subTest(reason=reason):
                hover = self.page_expression("tipText('wait:fixture-wait-0', data)", {"read_at": 200_000, "waiters": [{
                    "name": "fixture", "need": 1, "since": 100, "why": memory_feed.wait_why(reason)}]})
                self.assertIn("Waits for  "+expected, str(hover))

    def test_build_space_wait_shows_the_reason_in_the_chart_and_tooltip(self) -> None:
        wait = admission_records.BuildSpaceLow(8, 10)
        reason = "8 GiB free, 10 GiB required"
        data = {"read_at": 200, "waiters": [{"name": "fixture", "need": 1, "since": 100,
                "why": memory_feed.wait_why(wait), "reason": reason}]}
        chart, tip = cast(list[str], self.page_expression(
            "(renderWalk(data), [$('walk').innerHTML, tooltipHtml('wait:fixture-wait-0', data)])",
            data, walk=True))
        self.assertIn(reason, chart)
        tooltip = ElementTree.fromstring(tip)
        labels = dict(zip((label.text for label in tooltip.findall("dt")),
                          (value.text for value in tooltip.findall("dd")), strict=True))
        self.assertEqual(labels["Waits for"], "build space")
        self.assertEqual(labels["Reason"], reason)

    def test_a_mac_only_wait_hover_names_what_holds_the_mac_below_can_run_on(self) -> None:
        reason = "held by research for Mac CI restore"
        for because in ("Mac is held", reason, ""):
            waiter = {"name": "tile nextest", "need": 1, "since": 100, "why": "Mac", "runs_on": "Mac only", "because": because}
            with self.subTest(because=because):
                html = str(self.page_expression("tooltipHtml('wait:fixture-wait-0', data)", {"read_at":200_000,"waiters":[waiter]}, walk=True))
                grid = ElementTree.fromstring(html)
                self.assertEqual(html.count("Because"), int(bool(because)))
                self.assertEqual(len(grid.findall("dd[@class='long']")), int(len(because)>30))
                if because:
                    self.assertEqual(html.count(because), 1)

    def test_missing_run_use_has_a_page_hover_reason(self) -> None:
        hover = self.page_expression("tipText('row:fixture-run-0', data)", {"read_at":200_000, "walk":[{
            "name":"fixture", "unit":"", "showrunner":"", "used":{
                "kind":"RunUseUnavailable", "reason":"private memory.stat is missing"},
            "reserved":4, "counted":4, "queued":True, "keep_text":"full reservation", "progress":{
                "kind":"NoProgress", "reason":"no counter"}}]})
        self.assertEqual(hover, ["fixture  unavailable", ""])
        self.assertNotIn("NaN", str(hover))

    def test_the_queue_cap_tooltip_names_what_is_outside_the_cap(self) -> None:
        hover = self.page_expression("tipText('cap', data)", {"cap": 31})
        self.assertEqual(hover, ["Queue cap  31.0 GiB", "What builds may use\nBuilds  7.0 GiB\n+ Build reserved  6.0 GiB\n+ Free  18.0 GiB"])

    def test_missing_pressure_keeps_the_other_limit_readings(self) -> None:
        result = self.page_expression("[level(data), tipText('g-full', data), tipText('cap', data)]", {
            "psi":{"some":{"kind":"PressureNotRecorded", "reason":"private PSI is missing"}, "full":1},
            "limits":{"some":25,"full":5}, "pressure":"none", "cap":20,
            "cap_text":"fixture room", "ram": {**RAM_FIXTURE, "room_bytes": 7 * GIB}})
        self.assertIn("neutral", str(result))
        self.assertIn("1.0", str(result))
        self.assertIn("20.0 GiB", str(result))

    def test_the_bar_and_legend_show_the_blocks_in_their_groups(self) -> None:
        page = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        builds, free, other = ["tools", "queued", "ci"], ["reserved", "room", "safety", "swap"], [
            "desktop", "agents", "programs", "kernel", "cache", "unattributed"]
        bar = page[page.index('<div class="bar-wrap'):page.index('<div class="scale')]
        legend = page[page.index('<div class="legend">'):page.index('</details>', page.index('<div class="legend">'))]
        self.assertEqual([part.split('"')[0] for part in bar.split('id="s-')[1:]], [*builds, *free, *other])
        # The user, 11-15: each legend group sits under its own heading, so the legend ties to the bar.
        self.assertEqual([part.split('"')[0] for part in legend.split('data-tip="')[1:]], [
            "g-builds", *builds, "cap", "g-builds", "reserved", "room", "cap",
            "g-free", "reserved", "room", "system", "safety", "swap", "g-else", *other])
        # The user, 11-15: the waiting and progress key sits under the Running and Queued chart.
        walk_card = page[page.index('<div class="walk" id="walk">'):page.index('</main>')]
        key = walk_card[walk_card.index('<div class="walk-key">'):]
        self.assertEqual([part.split('"')[0] for part in key.split('data-tip="')[1:]], ["waiting", "progress"])
        # The user, 11-16: the legend starts collapsed; a click opens it.
        self.assertIn('<details class="legend-box">', page)
        # The user, 11-16: the free tooltip explains free, so the legend has no "How free is worked out" section.
        self.assertNotIn("How free is worked out", page)
        # The user, 11-15: braces name builds, free RAM and everything else; free RAM has three parts.
        self.assertEqual([part.split('"')[0] for part in bar.split('class="brace ')[1:]],
                         ["l1", "l1 free", "l1", "l2 reserved", "l2 free", "l2", "cap"])
        self.assertEqual([part.split('"')[0] for part in bar.split('id="b-')[1:]],
                         ["builds", "free", "else", "reserved", "room", "system", "cap"])
        # The user, 11-17: a short reserve has a red outline; the over-cap box, brace and Shortfall row are gone.
        for removed in ('data-tip="over"', 'data-tip="shortfall"', 'id="bc-reserved"', "bar-over"):
            self.assertNotIn(removed, page)
        # The user, 11-15: the queue cap starts at 0 and ends at the end of free.
        self.assertIn('<div class="brace cap" id="b-cap" data-tip="cap">', bar)
        for removed in ("updateNativeOwnerTips", "pageOwnerTipFields", "nativeTipText", 'id="mark"', 'id="marklabel"'):
            self.assertNotIn(removed, page)

    def test_memory_tooltips_use_the_shared_aligned_grid(self) -> None:
        data = {"read_at": 400, "cap": 31, "psi": {"some": 3.2, "full": 0}, "limits": {"some": 10, "full": 2},
                "tokens": {"kind": "TokenPool", "total": 31, "free": 4, "memory_floor_bytes": 2 * GIB},
                "walk": [program_memory_row(program_memory_fixture(started=True)),
                         {"name": "Mac nextest", "kind": "mac-build", "used": 2, "started_at": 340, "read_at": 380},
                         {"name": "AWS integrate", "kind": "cloud", "stage": "uploading", "started_at": 340}],
                "waiters": [{"name": "cables build", "since": 280, "need": 1, "place": 1, "runs_on": "natedev only",
                             "because": "edits the merged checkout with mend and fmt.", "first_in_line": True}],
                "swap_by": [{"name": "firefox", "kind": "owner", "bytes": 32 * MIB}]}
        grids = cast(dict[str, str], self.page_expression("""
            Object.fromEntries([...Object.keys(TIPS), 'row:hana', 'row:fixture-run-1', 'row:fixture-run-2',
                'wait:fixture-wait-0', 'swap:0', 'swap-pool:builds_ci', 'swap-pool:outside'].map(
                    key => [key, buildsTooltipHtml(key, data)]))
        """, data, walk=True))
        for key, markup in grids.items():
            with self.subTest(key=key):
                grid = ElementTree.fromstring(markup)
                self.assertEqual((grid.tag, grid.get("class")), ("dl", "tip"))
                self.assertFalse(grid.findall(".//dl"))
                self.assertEqual([cell.tag for cell in grid[:2]], ["dt", "dd"])
                self.assertEqual([cell.get("class") for cell in grid[:2]], ["title", "title"])
                pairs: list[tuple[str, str]] = []
                for index, cell in enumerate(grid):
                    classes = (cell.get("class") or "").split()
                    if cell.tag == "dt":
                        value = grid[index + 1]
                        self.assertEqual((value.tag, value.get("class")), ("dd", cell.get("class")))
                        label, text = "".join(cell.itertext()), "".join(value.itertext())
                        self.assertNotIn(":", label)
                        self.assertNotRegex(text, r"[.!?]$")
                        pairs.append((label, text))
                    else:
                        self.assertEqual(cell.tag, "dd")
                        if "caption" in classes or "long" in classes:
                            self.assertNotRegex(cell.text or "", r"[.!?]$")
                        if "caption" in classes:
                            self.assertLessEqual(len(cell.text or ""), 44)
                        if "program-memory" in classes:
                            self.assertEqual(len(set(pairs)), len(pairs))
                            pairs.clear()
                self.assertEqual(len(set(pairs)), len(pairs))
        self.assertIn('<dt>Because</dt><dd></dd><dd class="long">edits the merged checkout with mend and fmt</dd>',
                      grids["wait:fixture-wait-0"])
        page = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        self.assertRegex(page, r"\.tip \{[^}]*grid-template-columns: fit-content\(44%\) minmax\(0, 1fr\)")
        self.assertIn(".tip :is(dt, dd) { overflow-wrap: anywhere; }", page)
        self.assertRegex(page, r"\.tip dt \{[^}]*text-align: left")
        self.assertRegex(page, r"\.tip dd \{[^}]*text-align: right")

    def test_each_ram_block_tooltip_uses_the_design_text(self) -> None:
        keys = ["g-builds", "tools", "queued", "ci", "g-free", "reserved", "room", "system", "safety", "swap",
                "g-else", "desktop", "agents", "programs", "kernel", "cache", "unattributed", "cap", "budget"]
        tips = cast(list[list[str]], self.page_expression(
            "[" + ", ".join(f"tipText('{key}', data)" for key in keys) + "]", {"cap": 31, "cap_text": "fixture"}))
        self.assertEqual(dict(zip(keys, tips, strict=True)), {'g-builds': ['Builds  7.0 GiB', 'RAM that builds use now\nBuild tools  2.0 GiB\n+ Queued builds  3.0 GiB\n+ hana CI  2.0 GiB'], 'tools': ['Build tools  2.0 GiB', 'RAM that build tools use now\nrust-analyzer  1.0 GiB\n+ sccache  0.5 GiB\n+ Other build tools  0.5 GiB'], 'queued': ['Queued builds  3.0 GiB', 'RAM of builds the queue started'], 'ci': ['hana CI  2.0 GiB', 'RAM of hana CI programs'], 'g-free': ['Free RAM  30.0 GiB', 'RAM that no program holds\nBuild reserved  6.0 GiB\n+ Free  18.0 GiB\n+ System reserved  6.0 GiB'], 'reserved': ['Build reserved  6.0 GiB', 'Held for running builds to grow into'], 'room': ['Free  18.0 GiB', 'Where the next step can start\nQueue cap  31.0 GiB\n− Builds  7.0 GiB\n− Build reserved  6.0 GiB'], 'system': ['System reserved  6.0 GiB', 'RAM kept outside the queue cap\nSafety net  4.0 GiB\n+ Swap coming back  2.0 GiB'], 'safety': ['Safety net  4.0 GiB', 'RAM the queue always leaves free'], 'swap': ['Swap coming back  2.0 GiB', 'RAM held for swap that comes back\nShare held  50.0%'], 'g-else': ['Everything else  27.0 GiB', 'RAM used outside builds\nDesktop  3.0 GiB\n+ Agents  8.0 GiB\n+ Other programs  4.0 GiB\n+ Kernel  3.0 GiB\n+ File and kernel cache  7.0 GiB\n+ Not attributed  2.0 GiB'], 'desktop': ['Desktop  3.0 GiB', 'RAM of the desktop session\nProtected  2.0 GiB'], 'agents': ['Agents  8.0 GiB', 'RAM of agent sessions and their tools'], 'programs': ['Other programs  4.0 GiB', 'RAM of all other programs\nfirefox  2.0 GiB\nzed  1.0 GiB\n5 smaller programs  1.0 GiB'], 'kernel': ['Kernel  3.0 GiB', 'RAM the kernel uses for itself\nCompressed swap (zram)  1.5 GiB\n+ Slab, stacks, page tables  1.0 GiB\n+ Other kernel counters  0.5 GiB'], 'cache': ['File and kernel cache  7.0 GiB', 'Estimated cache outside free RAM'], 'unattributed': ['Not attributed  2.0 GiB', 'RAM without an attribution\nKernel, no counter  1.5 GiB\n+ No program name  0.5 GiB'], 'cap': ['Queue cap  31.0 GiB', 'What builds may use\nBuilds  7.0 GiB\n+ Build reserved  6.0 GiB\n+ Free  18.0 GiB'], 'budget': ['RAM  64.0 GiB', 'Memory across this machine\nBuilds  7.0 GiB\n+ Free RAM  30.0 GiB\n+ Everything else  27.0 GiB']})
        blocks = cast(int, self.page_expression("RAM_BLOCKS.reduce((sum, [, , amount]) => sum + amount(data.ram), 0)", {}))
        self.assertEqual(blocks, 64 * GIB)
        groups = cast(list[int], self.page_expression("['builds', 'free', 'else'].map(group => groupBytes(data.ram, group))", {}))
        self.assertEqual(groups, [7 * GIB, 30 * GIB, 27 * GIB])

    def test_build_tool_parts_keep_the_displayed_total_and_hide_an_empty_remainder(self) -> None:
        for remainder, python_tests, waiting_runs, expected in (
            (0, 264, 248, ["+ Python tests  0.3 GiB", "+ Waiting runs  0.2 GiB"]),
            (128, 136, 248, ["+ Python tests  0.1 GiB", "+ Waiting runs  0.3 GiB", "+ Other build tools  0.1 GiB"]),
            (192, 256, 64, ["+ Python tests  0.3 GiB", "+ Waiting runs  <0.1 GiB", "+ Other build tools  0.2 GiB"]),
            (492, 20, 0, ["+ Python tests  <0.1 GiB", "+ Waiting runs  0.0 GiB", "+ Other build tools  0.5 GiB"]),
            (163, 155, 194, ["+ Python tests  0.1 GiB", "+ Waiting runs  0.2 GiB", "+ Other build tools  0.2 GiB"]),
            (412, 80, 20, ["+ Python tests  <0.1 GiB", "+ Waiting runs  <0.1 GiB", "+ Other build tools  0.4 GiB"]),
        ):
            ram = {**RAM_FIXTURE, "python_tests_bytes": python_tests * MIB, "waiting_runs_bytes": waiting_runs * MIB,
                   "other_build_tools_bytes": remainder * MIB}
            with self.subTest(remainder=remainder):
                result = self.page_expression("[tipText('tools', data), tipText('cap', data)]", {"ram": ram, "cap": 31})
                self.assertEqual(result, [["Build tools  2.0 GiB", "\n".join([
                    "RAM that build tools use now", "rust-analyzer  1.0 GiB", "+ sccache  0.5 GiB", *expected])],
                    ["Queue cap  31.0 GiB", "What builds may use\nBuilds  7.0 GiB\n+ Build reserved  6.0 GiB\n+ Free  18.0 GiB"]])

    def test_missing_reserves_small_amounts_and_unavailable_readings_have_their_own_text(self) -> None:
        for description, needed, covered, expected in (
                ("Running builds grow into it.", 9, 5,
                 "Running builds grow into it. Needs 9.0 GiB; free RAM covers 5.0 GiB."),
                ("A fixed setting.", 3, 2, "A fixed setting. Needs 3.0 GiB; free RAM covers 2.0 GiB."),
                ("RAM held for swap that comes back.", 6, 1,
                 "RAM held for swap that comes back. Needs 6.0 GiB; free RAM covers 1.0 GiB."),
                ("Running builds grow into it.", 9, 9, "Running builds grow into it.")):
            with self.subTest(description=description, covered=covered):
                ram = {"build_target_bytes": needed * GIB, "build_covered_bytes": covered * GIB,
                       "build_missing_bytes": (needed - covered) * GIB}
                shown = self.page_expression("reserveDescription(data.description, shownReading(data).reserves.build)",
                                             {"description": description, "ram": ram})
                self.assertEqual(shown, expected)
        ram = {**RAM_FIXTURE, "available_bytes": 5 * GIB, "room_bytes": 0, "shortfall_bytes": 7 * GIB,
               "swap_covered_bytes": GIB, "build_covered_bytes": 0, "swap_missing_bytes": GIB,
               "build_missing_bytes": 6 * GIB, "rust_analyzer_bytes": 0, "sccache_bytes": 20 * MIB,
               "build_tools_bytes": 532 * MIB,
               "kernel_bytes": {"kind": "ReadingUnavailable", "reason": "meminfo is missing"},
               "kernel_no_counter_bytes": 0}
        tips = cast(list[list[str]], self.page_expression(
            "[reserveAlert(shownReading(data)), tipText('reserved', data), tipText('kernel', data), tipText('tools', data)," +
            " shownReading(data).memory.room.text, tipText('unattributed', data), tipText('system', data)]", {"ram": ram}))
        # The user, 11-17: one line names each reserve that free RAM does not fill.
        self.assertEqual(tips[0], "Free RAM is short by 7.0 GiB. Swap coming back holds 1.0 of 2.0 GiB; Build reserved holds 0.0 of 6.0 GiB.")
        self.assertEqual(tips[1], ["Build reserved  0.0 GiB", "Held for running builds to grow into\nTarget  6.0 GiB\nNot covered  6.0 GiB"])
        self.assertEqual(tips[2], ["Kernel  unavailable", ""])
        self.assertEqual(tips[3], ["Build tools  0.5 GiB", "RAM that build tools use now\nrust-analyzer  0.0 GiB\n+ sccache  <0.1 GiB\n+ Other build tools  0.5 GiB"])
        self.assertEqual(tips[5], ["Not attributed  2.0 GiB", "RAM without an attribution\nKernel, no counter  0.0 GiB\n+ No program name  2.0 GiB"])
        self.assertEqual(tips[4], "0.0 GiB")
        self.assertEqual(tips[6], ["System reserved  5.0 GiB", "RAM kept outside the queue cap\nSafety net  4.0 GiB\n+ Swap coming back  1.0 GiB"])
        small_bytes = round(0.01 * GIB)
        small = {**RAM_FIXTURE, "queued_builds_bytes": small_bytes, "room_bytes": small_bytes}
        self.assertEqual(self.page_expression(
            "[tipText('queued', data)[0], tipText('wait:fixture-wait-0', data)[1]]",
            {"ram": small, "waiters": [{"name": "fixture", "need": 0.03,
                                        "free": small_bytes / GIB, "first_in_line": True}]}),
            ["Queued builds  <0.1 GiB", "Waits for  its turn\nNeeds  <0.1 GiB\nFree  <0.1 GiB\nShort by  <0.1 GiB"])
        empty = {**RAM_FIXTURE, "build_tools_bytes": 0, "queued_builds_bytes": 0, "rust_analyzer_bytes": 0,
                 "sccache_bytes": 0, "other_build_tools_bytes": 0}
        self.assertEqual(self.page_expression("[tipText('tools', data), tipText('g-builds', data)]", {"ram": empty}),
                         [["Build tools  0.0 GiB", "RAM that build tools use now\nrust-analyzer  0.0 GiB\n+ sccache  0.0 GiB\n+ Other build tools  0.0 GiB"],
                          ["Builds  2.0 GiB", "RAM that builds use now\nBuild tools  0.0 GiB\n+ Queued builds  0.0 GiB\n+ hana CI  2.0 GiB"]])

    def test_the_status_line_names_the_one_reason_a_start_waits(self) -> None:
        for code, ram, expected in (
            ("unavailable", RAM_FIXTURE, "Memory reading unavailable; build capacity is unknown."),
            # Builds 11-15: a GPU wait also names a memory need that free does not cover.
            ("gpu_slot", RAM_FIXTURE,
             "A GPU test waits: another GPU test holds the GPU test slot. It also needs 3.0 GiB more memory."),
            ("pressure", RAM_FIXTURE, "Memory pressure prevents a new start; free is 18.0 GiB."),
            ("shortfall", {**RAM_FIXTURE, "room_bytes": 0, "shortfall_bytes": 3 * GIB},
             "Nothing is free: the reserves need more than free RAM."),
            ("shortfall", {**RAM_FIXTURE, "room_bytes": 0, "shortfall_bytes": 3 * GIB,
                           "build_missing_bytes": 2 * GIB, "swap_missing_bytes": GIB},
             "Nothing is free: the reserves need more than free RAM."),
            # The user, 11-16: a build reserve over the cap is a promise, not RAM in use.
            ("shortfall", {**RAM_FIXTURE, "room_bytes": 0, "shortfall_bytes": 3 * GIB, "build_missing_bytes": 3 * GIB,
                           "safety_missing_bytes": 0, "swap_missing_bytes": 0},
             "Nothing is free: build reserved needs more than free RAM. A reserve is room a step may still grow into, not RAM in use."),
            ("no_room", {**RAM_FIXTURE, "room_bytes": 0},
             "Nothing is free: the safety net, swap coming back and build reserved use all free RAM."),
            ("one_running_memory_wait", RAM_FIXTURE, "One build runs; another needs 12.0 GiB, but free is 9.0 GiB."),
            ("none", RAM_FIXTURE, ""),
        ):
            with self.subTest(code=code):
                status = {"code": code, "active_local_build_count": 1, "next_need_bytes": 12 * GIB,
                          "check_room_bytes": 9 * GIB}
                self.assertEqual(self.page_expression("statusLine(shownReading(data))", {"ram": ram, "memory_status": status}),
                                 expected)
        fits = {"code": "gpu_slot", "active_local_build_count": 1, "next_need_bytes": 2 * GIB, "check_room_bytes": 9 * GIB}
        self.assertEqual(self.page_expression("statusLine(shownReading(data))", {"ram": RAM_FIXTURE, "memory_status": fits}),
                         "A GPU test waits: another GPU test holds the GPU test slot.")

    def test_swap_titles_color_marked_staff_and_keep_program_titles_plain(self) -> None:
        for title_kind, name, expected in (("staff", "builder", ["builder"]),
                ("program", "builder", []), ("staff", "<unknown>", [])):
            with self.subTest(title_kind=title_kind, name=name):
                data = {"swap_by": [{"name": name, "title_kind": title_kind, "kind": "owner",
                                     "bytes": GIB, "owner_count": 0}]}
                body = str(self.page_expression("(()=>{" + self.role_setup()
                    + "return tooltipHtml('swap:0',data);})()", data, walk=True))
                grid = ElementTree.fromstring(body)
                self.assertEqual(''.join(grid[0].itertext()), name)
                self.assertEqual(grid[1].text, "1.0 GiB")
                self.assertEqual([span.text for span in grid[0].findall('span')], expected)

    def test_swap_chart_colors_only_marked_staff_titles_and_members(self) -> None:
        for title_kind, name, expected in (("staff", "builder", ["builder"]),
                ("program", "builder", []), ("staff", "<unknown>", []), ("", "builder", [])):
            for grouped in (False, True):
                with self.subTest(title_kind=title_kind, name=name, grouped=grouped):
                    owner = {"name": name, "title_kind": title_kind, "kind": "owner", "pool": "outside",
                             "bytes": GIB, "detail": "helper", "owner_count": 0}
                    row = {"name": "1 smaller groups", "kind": "smaller", "pool": "outside", "bytes": GIB,
                           "members": [owner]} if grouped else owner
                    data = {"swap_by": [row], "psi": {"some": 0, "full": 0},
                            "limits": {"some": 10, "full": 10}, "pressure": "none"}
                    body = str(self.page_expression("(()=>{" + self.role_setup()
                        + "renderSwap(data);return $('swaprows').innerHTML;})()", data, walk=True, swap=True))
                    root = ElementTree.fromstring("<root>" + body + "</root>")
                    chart_row = root.find(".//div[@data-tip='swap:0']")
                    assert chart_row is not None
                    self.assertEqual([span.text for span in chart_row.findall(".//span[@class='staff-name']")], expected)
                    if grouped:
                        member = chart_row.find(".//div[@class='swap-members']/div")
                        assert member is not None
                        self.assertEqual("".join(member.itertext()), name + ": 1.0 GiB")
                    else:
                        title = chart_row.find("span[@class='name']")
                        assert title is not None
                        self.assertEqual("".join(title.itertext()), name)
                        self.assertEqual(chart_row.findtext("small"), "helper")

    def test_swap_detail_staff_parts_keep_paths_and_matching_free_text_plain(self) -> None:
        for owner, expected in (("builder", ["builder"]), ("<unknown>", [])):
            with self.subTest(owner=owner):
                data = {"swap_by": [{"name": "Compile", "kind": "owner", "pool": "outside", "bytes": GIB,
                    "detail": owner + " · /fixture/path · helper", "detail_parts": [
                        {"kind": "staff", "text": owner}, {"kind": "text", "text": " · /fixture/path · helper"}]}],
                    "psi": {"some": 0, "full": 0}, "limits": {"some": 10, "full": 10}, "pressure": "none"}
                body = str(self.page_expression("(()=>{" + self.role_setup()
                    + "renderSwap(data);return $('swaprows').innerHTML;})()", data, walk=True, swap=True))
                detail = ElementTree.fromstring("<root>" + body + "</root>").find(".//div[@data-tip='swap:0']/small")
                assert detail is not None
                self.assertEqual("".join(detail.itertext()), owner + " · /fixture/path · helper")
                self.assertEqual([span.text for span in detail.findall("span")], expected)

    def test_swap_detail_parts_keep_the_existing_public_text_filter(self) -> None:
        for path in ("builds.slice/fixture.scope", "user.slice/fixture.service", "—", "/fixture/path"):
            with self.subTest(path=path):
                row: dict[str, object] = {"name": "Compile", "kind": "owner", "pool": "outside", "bytes": GIB,
                    "detail": "builder · " + path + " · helper · 512 MiB"}
                data = {"swap_by": [row], "psi": {"some": 0, "full": 0},
                        "limits": {"some": 10, "full": 10}, "pressure": "none"}
                expression = "(()=>{" + self.role_setup() + "renderSwap(data);return $('swaprows').innerHTML;})()"
                old = str(self.page_expression(expression, data, walk=True, swap=True))
                row["detail_parts"] = [{"kind": "staff", "text": "builder"},
                    {"kind": "text", "text": " · " + path + " · helper · 512 MiB"}]
                new = str(self.page_expression(expression, data, walk=True, swap=True))
                old_detail = ElementTree.fromstring("<root>" + old + "</root>").find(".//div[@data-tip='swap:0']/small")
                new_detail = ElementTree.fromstring("<root>" + new + "</root>").find(".//div[@data-tip='swap:0']/small")
                assert old_detail is not None and new_detail is not None
                self.assertEqual("".join(new_detail.itertext()), "".join(old_detail.itertext()))
                self.assertEqual([span.text for span in new_detail.findall("span")], ["builder"])

    def test_grouped_process_tooltips_color_staff_parts_and_keep_programs_plain(self) -> None:
        data = {"walk": [{"name": "unowned processes (1)", "group": "unowned processes", "used": 0.2,
            "parts": [{"who": "builder · helper", "who_parts": [{"kind": "staff", "text": "builder"},
                {"kind": "text", "text": " · helper"}], "pid": 123, "started_at": 0, "gib": 0.2}]}]}
        body = str(self.page_expression("(()=>{" + self.role_setup()
            + "return tooltipHtml('row:fixture-run-0',data);})()", data, walk=True))
        grid = ElementTree.fromstring(body)
        label = next(cell for cell in grid if "builder" in "".join(cell.itertext()))
        self.assertEqual("".join(label.itertext()), "builder · helper · 123")
        self.assertEqual([span.text for span in label.findall("span")], ["builder"])
        walk = cast(list[dict[str, object]], data["walk"])
        parts = cast(list[dict[str, object]], walk[0]["parts"])
        del parts[0]["who_parts"]
        plain = str(self.page_expression("tooltipHtml('row:fixture-run-0',data)", data, walk=True))
        self.assertEqual("".join(grid.itertext()), "".join(ElementTree.fromstring(plain).itertext()))

    def test_captions_stay_plain_and_route_text_colors_marked_staff_parts(self) -> None:
        about = "Runs every test module"
        because = "Held by builder"
        data = {"waiters": [{"name": "Compile", "unit": "", "about": about, "runs_on": "Mac",
            "kind": "build-step", "because": because, "because_parts": [
                {"kind": "text", "text": "Held by "}, {"kind": "staff", "text": "builder"}]}]}
        body = str(self.page_expression("(()=>{" + self.role_setup()
            + "return tooltipHtml('wait:fixture-wait-0',data);})()", data, walk=True))
        grid = ElementTree.fromstring(body)
        caption = grid.find("dd[@class='caption']")
        assert caption is not None
        self.assertEqual(caption.text, about)
        self.assertEqual(list(caption), [])
        reason = next(cell for cell in grid if cell.tag == "dd" and "".join(cell.itertext()) == because)
        self.assertEqual([span.text for span in reason.findall("span")], ["builder"])

    def test_swap_rows_use_their_own_text_and_show_one_owner_only(self) -> None:
        def row(name: str, kind: str, amount: int, **extra: object) -> dict[str, object]:
            return {"name": name, "detail": "fixture", "swap": amount / GIB, "bytes": amount, "kind": kind,
                    "pool": "outside", "category": "", "members": [], "owner_count": 0, **extra}
        rows = [
            row("Agents (3)", "owner", GIB, category="agents", owner_count=3, showrunner="builds", unit="report"),
            row("hana nextest", "owner", 3 * MIB, owner_count=1, showrunner="builds", unit="report"),
            row("2 smaller groups", "smaller", 2048, owner_count=2,
                members=[{"name": "a", "bytes": 1024}, {"name": "b", "bytes": 1024}]),
            row("Unclassified groups", "ungrouped", 512),
            row("Not attributed", "unattributed", 4 * MIB),
            row("Reading difference", "difference", -2 * MIB),
            row("All swap used", "total", 3 * GIB, pool="all"),
        ]
        tips = cast(list[list[str]], self.page_expression(
            "[...data.swap_by.map((row, index) => tipText(`swap:${index}`, data)), tipText('swap-pool:outside', data)," +
            " tipText('swap-pool:builds_ci', data)]", {"swap_by": rows}))
        self.assertEqual(tips, [
            ["Agents (3)  1.0 GiB", "Swap charged to this program"],
            ["hana nextest  3.0 MiB", "Swap charged to this program\nShowrunner  builds"],
            ["2 smaller groups  2.0 KiB", "Swap of the smaller named groups"],
            ["Unclassified groups  512.0 B", "Swap without a program name"],
            ["Not attributed  4.0 MiB", "Swap without a matching group"],
            ["Reading difference  −2.0 MiB", "Group readings above the host total"],
            ["All swap used  3.0 GiB", "Swap across this machine"],
            ["Outside builds and CI  2.0 GiB", "Reserve share  50.0%\nSwap coming back  2.0 GiB"],
            ["Builds and CI  1.0 GiB", "Swap outside the reserve target"]])
