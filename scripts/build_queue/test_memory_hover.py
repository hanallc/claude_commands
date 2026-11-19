"""Check the memory page hover with private captured readings."""
from __future__ import annotations
from scripts.build_queue import admission_records

from collections.abc import Mapping
import json
from html.parser import HTMLParser
from itertools import combinations
from pathlib import Path
from random import Random
import re
import shutil
import subprocess
import unittest
from xml.etree import ElementTree
from typing import cast
from scripts.lint import memory_admit as gate
from scripts.build_queue import memory_feed, program_memory_feed, progress_profile as progress
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


class MemoryHoverTests(unittest.TestCase):
    def test_row_staff_names_have_role_colors_and_free_names_stay_plain(self) -> None:
        unit = staff.StaffRow(staff.StaffId("builder"), "unit", "builder", staff.StaffRoot(), current_session=staff.StaffId("builder"))
        names = staff_names.StaffNames(staff.StaffSnapshot((unit,)))
        setup = "globalThis.window=globalThis;" + staff_names.script(names.registry())[8:-9]
        body = str(self.page_expression("(()=>{" + setup + "return rowLabel('Compile', 'builder')+rowLabel('Compile', 'nate');})()", {}, walk=True))
        self.assertRegex(body, r'data-color-text="role.unit-director"[^>]*>builder</span>')
        self.assertIn('<span class="owner">nate</span>', body)

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
        data = {"unmatched_priority_entries": entries,
                "walk": [{"name": "Compile", "priority_entries": entries, "queued": True}]}
        for key in ("walkhead", "row:fixture-run-0"):
            with self.subTest(key=key):
                body = str(self.page_expression("(()=>{" + self.role_setup()
                    + "return tooltipHtml(" + json.dumps(key) + ",data);})()", data, walk=True))
                grid = ElementTree.fromstring(body)
                priority = next(grid[index + 1] for index, cell in enumerate(grid) if cell.text == "Priority")
                self.assertEqual("".join(priority.itertext()), "builder, helper, <unknown>")
                self.assertEqual([span.text for span in priority.findall("span")], ["builder", "helper"])

    def test_tooltip_colors_only_fields_that_name_staff(self) -> None:
        data = {"waiters": [{"name": "Compile", "unit": "builder", "showrunner": "helper",
                "runner": "worker", "worker": "builder", "kind": "repository-slot",
                "title": {"unit": "builder", "step": "Compile"}, "because": "builder", "runs_on": "builder", "why": "builder"}]}
        body = str(self.page_expression("(()=>{" + self.role_setup()
            + "return tooltipHtml('wait:fixture-wait-0',data);})()", data, walk=True))
        grid = ElementTree.fromstring(body)
        for label in ("Because", "Runs on"):
            value = next(grid[index + 1] for index, cell in enumerate(grid) if cell.text == label)
            self.assertEqual(value.text, "builder")
            self.assertEqual(list(value), [])
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
                self.assertEqual(root.findtext(".//tr[@data-tip='wait:fixture-wait-0']/th[@class='step']/span"), "first")

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
        for key in ("row:fixture-run-0", "row:fixture-run-3", "wait:fixture-wait-0"):
            with self.subTest(row=key):
                label = root.find(f".//div[@data-tip='{key}']/span[@class='row-label']")
                assert label is not None
                name = label.find("./span/span[@class='name']")
                assert name is not None
                self.assertEqual(len(name), 1)
                self.assertEqual(name[0].get("data-color-text"), "role.unit-director")
                self.assertEqual(name[0].text, "builder")
                self.assertEqual(name[0].tail, " builds")
                owner = label.find("./span[@class='owner']/span")
                assert owner is not None
                self.assertEqual(owner.get("data-color-text"), "role.showrunner")
                self.assertEqual(owner.text, "builds")
        for key in ("fixture-run-1", "fixture-run-2"):
            name = root.find(f".//div[@data-run='{key}']/span/span/span[@class='name']")
            assert name is not None
            self.assertEqual(len(name), 0)
            self.assertEqual(name.text, "builder builds")

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
                        "used": 3, "reserved": 2, "about": caption, "changes": "main",
                        "about_parts": [{"kind": "text", "text": caption}]}
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
                self.assertIn(new_item_dialog.MemorySegment("Build tools", 2, new_item_dialog.MemoryTone.TOOLS),
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
        data = {"read_at": 400, "walk": [{"name": "merge gate", "unit": "fixture", "showrunner": "fixture-production",
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
          globalThis.document = {addEventListener() {}, querySelector: () => null};
          const tip = {
            classList: {add() {}, remove() {}, toggle() {}}, style: {}, offsetWidth: 368,
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

    def test_disks_slot_tooltip_keeps_the_shared_fields_without_program_memory(self) -> None:
        row = program_memory_row(program_memory_fixture(started=True))
        builds_source = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        disks_source = Path(disks_page.__file__).with_name(disks_page.PAGE_NAME).read_text()
        shared = disks_page.shared_step_tip(builds_source)
        slot_tooltip = disks_source[disks_source.index("function slotStep("):disks_source.index("function slotsAbout(")]
        expression = """(() => {
          const builds = data;
          const settings = {builds_max_age_seconds: 30};
          const slotReport = {machine:'natedev', groups:[{slots:[{name:'hana-slot1', used_by:{}}]}]};
          Date.now = () => data.read_at * 1000;
          const stepTip = (() => {
        """ + shared.script + """
            return {tooltipHtml};
          })();
        """ + slot_tooltip + """
          return [tipFor('slot:hana-slot1'), tooltipHtml('row:hana', data)];
        })()"""
        disks, shared_builds = cast(list[str], self.page_expression(expression, {"read_at": 400, "walk": [row]}, walk=True))
        self.assertEqual(disks, shared_builds)
        self.assertIn("<dt>Runs on</dt><dd>natedev</dd>", disks)
        self.assertNotIn("program-memory", disks)
        self.assertNotIn(".program-memory", shared.style)

    def test_a_queued_run_hover_says_its_use_need_room_and_progress(self) -> None:
        row = memory_tests.ProgressKeepAsideTests.reservation("merge-gate", need=4 * GIB)
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
            ("merge-gate", module_counter, True),
            ("merge gate", module_counter, True),
            ("fixture-project merge gate", module_counter, True),
            ("merge-gate", test_counter, False),
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
                    "name": "merge-gate", "unit": "fixture", "queued": True, "keep_text": "", "counted": 1,
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
        about = "merges the handler checkpoint into the sdlc branch, tests every module, then pushes sdlc"
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

    def test_a_ci_hover_keeps_its_ci_line(self) -> None:
        data = {"read_at": 200, "walk": [{"name": "hana CI", "ci": True, "ci_label": "hana CI · runner-1",
                "detail": "busy", "unit": "", "keep_text": "", "runner": "unknown"}]}
        hover = cast(list[str], self.page_expression("tipText('row:fixture-run-0', data)", data))
        self.assertEqual(hover[0], "hana CI · runner-1  running")
        self.assertEqual(hover[1], "Runs on  natedev")

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
                data = {"walk": [{"kind": "cloud", "machine": "aws", "name": "merge-gate",
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
                    {"name": "local build", "queued": True, "slot": 2,
                     "progress": {"kind": "RowProgress", "phase": "building"}},
                    {"name": "merge gate", "queued": True,
                     "progress": {"kind": "RowProgress", "phase": "testing"}},
                    {"name": "Mac builds", "kind": "cloud", "machine": "mac", "slot": 3,
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
                headers = table.findall("./thead/tr/th")
                self.assertEqual([cell.text for cell in headers],
                                 ["Stage", "Done", "Step", "Dur", "Pid", "Target", "vCPU", "Slot", "Tokens", None])
                self.assertEqual(table.findall(".//*[@class='stage']"), [])
                for index, expected in enumerate(("building", "testing", "—", "testing", "—", "testing", "—", "building")):
                    row = table.find(f"./tbody/tr[@data-tip='row:fixture-run-{index}']")
                    assert row is not None
                    self.assertEqual(row[0].get("class"), "place")
                    self.assertEqual(row[0].text, expected)
                    self.assertEqual(row.findtext("td[@class='slot']"), "2" if index == 0 else "3" if index == 2 else "—")
                    self.assertEqual([cell.get("class") for cell in row[-4:]],
                                     ["vcpus", "slot", "tokens", "row-action"])
                queue_headers = table.findall("./tbody/tr[@class='queue-head']")
                spacers = root.findall("./div[@class='walk-chart']/div[@class='walk-chart-head queue-head']")
                self.assertEqual(len(queue_headers), int(waiting))
                self.assertEqual(len(spacers), 1)
                if waiting:
                    columns = queue_headers[0].findall("th")
                    self.assertEqual([cell.text for cell in columns],
                                     ["#", "Done", "Step", "Waiting", "Pid", "Target", "vCPU", None])
                    self.assertTrue(all(cell.get("scope") == "col" for cell in columns))
                    for index, expected in enumerate(("1", "cloud", "2")):
                        row = table.find(f"./tbody/tr[@data-tip='wait:fixture-wait-{index}']")
                        assert row is not None
                        self.assertEqual(row[0].text, expected)
                        self.assertIsNone(row.find("td[@class='slot']"))
                for row in table.findall("./tbody/tr"):
                    self.assertEqual(sum(int(cell.get("colspan", "1")) for cell in row), len(headers))

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
                    ci_name = chart_rows[0].find("./span/span/span[@class='name']")
                    assert ci_name is not None
                    self.assertEqual(ci_name.text, "hana CI")

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
                self.assertEqual(metadata.findtext("th[@class='step']/span"), "hana nextest")
                vcpus = metadata.find("td[@class='vcpus']")
                assert vcpus is not None
                self.assertEqual(vcpus.text, "12")
                owner = table.find(".//div[@data-run='fixture-run-0']/span/span[@class='owner']")
                assert owner is not None
                self.assertEqual(owner.text, "hana")
                tooltip = ElementTree.fromstring(result[1])
                labels = [cell.text for cell in tooltip.findall("dt")]
                values = [cell.text for cell in tooltip.findall("dd")]
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
        self.assertEqual([row.findtext(f"td[@class='{column}']") for column in ("place", "duration", "pid", "vcpus")],
                         ["—", "—", "—", "—"])
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

    def test_fractional_memory_terms_format_raw_totals_and_shortage(self) -> None:
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
                         ["3.1 GiB", "1.0 GiB", "1.0 GiB", "1.0 GiB"])
        run = {str(row["label"]): str(row["value"]) for row in tips[1]}
        self.assertEqual((run["Uses"], run["+ Build reserved"], run["= In queue cap"]),
                         ("1.0 GiB", "1.0 GiB", "2.1 GiB"))
        small, short, head_wait = ({str(row["label"]): str(row["value"]) for row in tip} for tip in firsts)
        self.assertEqual(small["Short by"], "<0.1 GiB")
        self.assertEqual(short["Short by"], "<0.1 GiB")
        self.assertEqual(head_wait["Short by"], "<0.1 GiB")
        terms = ("Queue cap", "− Builds", "− Build reserved", "= Free")
        head = {str(row["label"]): round(float(str(row["value"]).split()[0]) * 10)
                for row in firsts[-1] if row["label"] in terms}
        self.assertLessEqual(abs(head["Queue cap"] - head["− Builds"] - head["− Build reserved"] -
                                 head["= Free"]), 2)
        cap = cast(list[dict[str, object]], self.page_expression("tooltipRows('cap', data)", data))
        cap_fields = {str(row["label"]): str(row["value"]) for row in cap}
        self.assertEqual(given["Free"], cap_fields["+ Free"])
        for head_name, cap_name in (("Queue cap", "Queue cap"), ("− Builds", "Builds"),
                                   ("− Build reserved", "+ Build reserved"), ("= Free", "+ Free")):
            self.assertEqual(str(next(row["value"] for row in firsts[-1] if row["label"] == head_name)), cap_fields[cap_name])
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
                self.assertLessEqual(abs(amounts[0] - sum(amounts[1:])), len(amounts) / 2)
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
            if sample in (7, 8):
                desktop, adjustment = (3.0, -0.5) if sample == 7 else (0.149, -0.696)
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
            "tools": ("Build tools", ("build_tools_bytes",)),
            "g-builds": ("Builds", ("build_tools_bytes", "queued_builds_bytes", "ci_bytes")),
            "g-free": ("Free RAM", ("available_bytes",)),
            "system": ("System reserved", ("safety_covered_bytes", "swap_covered_bytes")),
            "programs": ("Other programs", ("other_programs_bytes",)),
            "kernel": ("Kernel", ("kernel_bytes",)),
            "unattributed": ("Not attributed", ("unattributed_bytes",)),
            "g-else": ("Everything else", ("desktop_bytes", "agents_bytes", "other_programs_bytes",
                                          "kernel_bytes", "cache_bytes", "unattributed_bytes")),
            "cap": ("Queue cap", ("cap_bytes",)),
            "budget": ("RAM", ("total_bytes",)),
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
          ramAmount = bytes => recorded(bytes) ? (bytes / GIB).toFixed(9) + ' GiB' : 'unavailable';
          swapAmount = ramAmount;
          const raw = data.readings.map(reading => render(structuredClone(reading)).at(-1));
          return [shown, raw];
        })()""", {"readings": readings, "limits": new_item_dialog.BUILD_EVIDENCE_LIMITS}, walk=True))
        results, raw_surfaces = result

        def tenths(text: str) -> int:
            if text in ("<0.1 GiB", "-<0.1 GiB"):
                return -1 if text.startswith("-") else 1
            return round(float(text.split()[0]) * 10)

        def formatted(gib: float) -> str:
            sign = "-" if gib < 0 else ""
            magnitude = abs(gib)
            if 0 < magnitude < 0.1:
                return f"{sign}<0.1 GiB"
            return f"{sign}{int(magnitude * 10 + 0.5) / 10:.1f} GiB"

        amount_pattern = re.compile(r"-?<?\d+\.\d+(?= GiB| of -?<?\d+\.\d+ GiB)")

        def shown_value(text: str) -> float:
            if text in ("<0.1", "-<0.1"):
                return -0.05 if text.startswith("-") else 0.05
            return float(text)

        def assert_sum(total: str, *parts: str, subtract: tuple[str, ...] = ()) -> None:
            def value(amount: str) -> float:
                return shown_value(amount.split()[0])

            expected = sum(map(value, parts)) - sum(map(value, subtract))
            tolerance = 0.05 * (1 + len(parts) + len(subtract))
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
                for key, (title, raw_keys) in totals.items():
                    self.assertEqual(tips[key][title], formatted(raw_sum(raw, *raw_keys) / GIB), key)
                    assert_sum(tips[key][title], *(value for label, value in tips[key].items() if label != title))
                taken, cap = tips["walksum"], tips["cap"]
                reserve = taken.get("+ Full reserve", taken.get("+ Build reserved", ""))
                raw_overage = (raw["build_missing_bytes"] - raw["room_bytes"]) / GIB
                expected_overage = raw_overage > 0
                self.assertEqual(overage, expected_overage)
                self.assertEqual("Over cap" in taken, expected_overage)
                cap_amount = cap["Queue cap"]
                builds = raw_sum(raw, "build_tools_bytes", "queued_builds_bytes", "ci_bytes")
                claimed = builds + raw["build_target_bytes" if short["build"] else "build_covered_bytes"]
                self.assertEqual(taken["Taken"], formatted(claimed / GIB))
                assert_sum(taken["Taken"], taken["Builds"], reserve)
                expected_summary = f"{taken['Taken'].split()[0]} of {cap_amount} taken"
                if expected_overage:
                    self.assertEqual(taken["Over cap"], formatted(raw_overage))
                    expected_summary += f" · {formatted(raw_overage)} over cap"
                self.assertEqual(summary, expected_summary)
                if sample == 0:
                    self.assertEqual(tips["reserved"]["= Target"], "8.1 GiB")
                room_tip = tips["room"]
                if sample in (2, 3):
                    self.assertEqual(tips["reserved"]["Build reserved"], "8.0 GiB")
                    self.assertEqual(tips["reserved"]["= Target"], "8.0 GiB")
                    self.assertEqual(room_tip["Free"], "0.0 GiB" if sample == 2 else "<0.1 GiB")
                if sample == 3:
                    segments = cast(list[dict[str, object]], evidence["segments"])
                    self.assertIn({"label": "Free", "gib": 0.01, "tone": "free"}, segments)
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
                    if short[raw_key]:
                        self.assertEqual(shortage, formatted(raw[f"{raw_key}_missing_bytes"] / GIB))
                    self.assertEqual(shortage, tips["shortfall"].get(title, tips["shortfall"].get("+ " + title, "0")))
                self.assertEqual(tips["shortfall"]["Shortfall"], formatted(raw["shortfall_bytes"] / GIB))
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
                        for (raw_left, shown_left), (raw_right, shown_right) in combinations(amounts, 2):
                            if raw_left < raw_right:
                                self.assertLessEqual(shown_left, shown_right, (before, after))
                            elif raw_right < raw_left:
                                self.assertLessEqual(shown_right, shown_left, (before, after))
                if sample == 1:
                    self.assertEqual(tips["shortfall"]["Safety net"], "<0.1 GiB")
                    self.assertEqual(tips["shortfall"]["+ Swap coming back"], "<0.1 GiB")
                if sample == 4:
                    self.assertIn("<0.1 GiB over cap", summary)
                if sample == 5:
                    self.assertEqual(tips["reserved"]["= Target"], "8.5 GiB")
                    self.assertEqual(tips["swap"]["= Target"], "6.7 GiB")
                    self.assertIn("Build reserved holds 8.5 of 8.5 GiB.", warning_text)
                if sample in (7, 8):
                    outside = tips["g-else"]
                    self.assertEqual(outside["Everything else"], "3.1 GiB" if sample == 7 else "<0.1 GiB")
                    self.assertEqual(outside["+ Not attributed"], "-0.5 GiB" if sample == 7 else "-0.7 GiB")
                run = tips["row:run"]
                assert_sum(run["= In queue cap"], run["Uses"], run["+ Build reserved"])
                group = tips["row:group"]
                assert_sum(group["Group"], group["Editor"], group["Indexer"])
                wait, head = tips["wait:wait"], tips["wait:head"]
                waiter, head_waiter = cast(list[dict[str, object]], readings[sample]["waiters"])
                need, free = cast(float, waiter["need"]), cast(float, waiter["free"])
                head_terms = cast(dict[str, list[dict[str, object]]], head_waiter["head_room"])["rows"]
                head_free = cast(float, head_terms[-1]["gib"])
                self.assertEqual(head["= Head room"], formatted(head_free))
                for tip, free_for_step in ((wait, free), (head, head_free)):
                    shortage = need - free_for_step
                    self.assertEqual(tip["Waits for"], "memory" if shortage > 0 else "its turn")
                    self.assertEqual(tip.get("Short by"), formatted(shortage) if shortage > 0 else None)
                difference = raw["room_bytes"] / GIB - free
                self.assertEqual(wait.get("− Held elsewhere"), formatted(difference) if difference > 0 else None)
                self.assertEqual(wait.get("+ Released"), formatted(-difference) if difference < 0 else None)
                gpu_reason = "A GPU test waits: another GPU test holds the GPU test slot."
                if need > free:
                    gpu_reason += f" It also needs {formatted(need - free)} more memory."
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
                    "[reserveAlert(shownReading(data)), tipText('reserved',data)[1], tipText('shortfall',data)[0]]",
                    {"ram": ram})
                self.assertEqual(shown, [
                    "Free RAM is short by <0.1 GiB. Build reserved holds 0.0 of <0.1 GiB.",
                    "Held for running builds to grow into\nTarget  <0.1 GiB\nNot covered  <0.1 GiB",
                    "Shortfall  <0.1 GiB",
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

    def test_repository_slot_renders_the_unit_and_script_title(self) -> None:
        fixture = test_memory_feed.MemoryFeedTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        data = fixture.copies_slot_page("python3 /fixture/check.service.sh", source="/fixture/startup",
                                       unit="startup", staff_id="staff-startup")
        row, = data["waiters"]
        key = row.get("key")
        self.assertIsNotNone(key)
        body = cast(list[str], self.page_expression(
            f"(renderWalk(data), [$('walk').innerHTML, tooltipHtml('wait:{key}',data)])",
            cast(object, json.loads(memory_feed.page_json(data))), walk=True))
        table = ElementTree.fromstring(f"<root>{body[0]}</root>")
        cell = table.find(".//tr[@class='wait wait-first']/th[@class='step']/span")
        self.assertIsNotNone(cell)
        assert cell is not None
        self.assertEqual(cell.text, "check.service.sh · hana")
        self.assertIsNone(table.find(".//td[@class='unit']"))
        self.assertEqual("".join(ElementTree.fromstring(body[1])[0].itertext()), "startup · check.service.sh")

    def test_repository_slot_wait_has_no_memory_group(self) -> None:
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200,
                "waiters": [{"kind": "repository-slot", "name": "gate", "detail": "widget", "need": 0,
                             "since": 100, "reason": "waits for a free widget slot", "runs_on": "natedev"}]}
        rows = cast(list[dict[str, object]], self.page_expression("tooltipRows('wait:fixture-wait-0', data)", data))
        self.assertEqual({row["label"]: row["value"] for row in rows}["Waits for"], "build slot")
        self.assertTrue({"Needs", "Free", "Short by"}.isdisjoint(row["label"] for row in rows))
        markup = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
        root = ElementTree.fromstring(f"<root>{markup}</root>")
        self.assertEqual(root.findtext(".//div[@data-tip='wait:fixture-wait-0']/span/span/span[@class='name']"),
                         "gate · widget")
        self.assertEqual(root.findtext(".//tr[@data-tip='wait:fixture-wait-0']/th[@class='step']/span"), "gate · widget")
        self.assertEqual(rows[0]["label"], "gate · widget")

    def test_heading_keeps_all_structured_priority_entries(self) -> None:
        data = {"unmatched_priority_entries": [{"unit": "research", "why": "first"},
                                                {"unit": "cables", "why": "second"}],
                "unmatched_priority_text": "text is not parsed"}
        heading = self.page_expression("tipText('walkhead', data)", data)
        self.assertIn("Priority  research, cables", str(heading))
        self.assertNotIn("text is not parsed", str(heading))

    def test_equal_group_members_keep_their_process_identity(self) -> None:
        data = {"read_at": 220, "walk": [{"name": "Codex servers", "group": "codex", "used": 0.4,
            "parts": [{"who": "codex", "pid": pid, "started_at": 100, "gib": 0.2} for pid in (123, 456)]}]}
        html = str(self.page_expression("tooltipHtml('row:fixture-run-0', data)", data, walk=True))
        root = ElementTree.fromstring(html)
        self.assertEqual([cell.text for cell in root.findall("dt")][2:], ["codex · 123 · 2 min", "codex · 456 · 2 min"])
        self.assertEqual([cell.text for cell in root.findall("dd")][2:], ["0.2 GiB", "0.2 GiB"])

    def test_a_hidden_console_frame_reads_nothing_until_the_console_shows_it(self) -> None:
        html = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        loop = html[html.index("const consoleHidden ="):html.index("</script>")]
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

    def test_space_pauses_every_read_and_space_again_resumes(self) -> None:
        html = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        self.assertIn('<div class="paused-badge" id="paused" role="status" hidden>', html)
        loop = html[html.index("const consoleHidden ="):html.index("</script>")]
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

    def page_expression(self, expression: str, data: object, *, walk: bool = False, swap: bool = False) -> object:
        html = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        functions = html[html.index("function field("):html.index("const tip =")]
        constants = ""
        if walk:
            start = html.index("function rowAdd(")
            functions += html[start:html.index("document.addEventListener('click'", start)]
            functions += html[html.index("function esc(s)"):html.index("const consoleHidden =")]
            constants += """
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {style: {}, classes, classList: {toggle(name, active) {
      if (active) classes.add(name);
      else classes.delete(name);
    }}, setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; }});
  }
  return elements.get(id);
};
function fitLabels() {}
"""
        if swap:
            start = html.index("const swapView =")
            functions += html[start:html.index('document.addEventListener("click", (e) => {', start)]
            constants += "function placeLimitLabels() {}\nconst document = {querySelectorAll: () => []};\n"
        javascript = functions + constants + "\nconst data = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n" + (
            "process.stdout.write(JSON.stringify(" + expression + "));\n")
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        payload: dict[str, object] = {"walk": [], "waiters": [], "swap_by": [], "ram": RAM_FIXTURE,
                                      "swap": SWAP_FIXTURE, **cast(dict[str, object], data)}
        for kind, rows in (("run", payload["walk"]), ("wait", payload["waiters"])):
            for index, row in enumerate(cast(list[dict[str, object]], rows)):
                _ = row.setdefault("key", f"fixture-{kind}-{index}")
                if row.get("kind") != "repository-slot":
                    _ = row.setdefault("title", {"unit": "", "step": row.get("ci_label") or row["name"]})
                _ = row.setdefault("unit", "")
        result = subprocess.run([executable, "-e", javascript], input=json.dumps(payload),
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
        walk_card = page[page.index('<div class="walk" id="walk">'):page.index('<section class="card" id="leftovers"')]
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
        self.assertIn("grid-template-columns: max-content 1fr", page)
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
            " ramAmount(0), tipText('unattributed', data), tipText('system', data)]", {"ram": ram}))
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

    def test_gate_and_route_text_color_only_marked_staff_parts(self) -> None:
        data = {"waiters": [{"name": "Compile", "unit": "", "about": "Tests builder; helper stays plain",
            "about_parts": [{"kind": "text", "text": "Tests "}, {"kind": "staff", "text": "builder"},
                {"kind": "text", "text": "; helper stays plain"}], "runs_on": "Mac", "kind": "repository-slot",
            "because": "Held by builder; helper stays plain", "because_parts": [
                {"kind": "text", "text": "Held by "}, {"kind": "staff", "text": "builder"},
                {"kind": "text", "text": "; helper stays plain"}]}]}
        body = str(self.page_expression("(()=>{" + self.role_setup()
            + "return tooltipHtml('wait:fixture-wait-0',data);})()", data, walk=True))
        grid = ElementTree.fromstring(body)
        values = [cell for cell in grid if cell.tag == "dd" and "helper stays plain" in "".join(cell.itertext())]
        self.assertEqual(len(values), 2)
        self.assertTrue(all([span.text for span in cell.findall("span")] == ["builder"] for cell in values))
        for field in ("about_parts", "because_parts"):
            del data["waiters"][0][field]
        plain = str(self.page_expression("tooltipHtml('wait:fixture-wait-0',data)", data, walk=True))
        self.assertEqual("".join(grid.itertext()), "".join(ElementTree.fromstring(plain).itertext()))

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
