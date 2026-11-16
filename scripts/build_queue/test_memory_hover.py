"""Check the memory page hover with private captured readings."""
from __future__ import annotations
from scripts.build_queue import admission_records

import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest
from xml.etree import ElementTree
from typing import cast
from scripts.lint import memory_admit as gate
from scripts.build_queue import memory_feed, progress_profile as progress
from scripts.lint.memory_test_support import admission_policy_fixture
from scripts.lint import test_memory_admit as memory_tests
from scripts.lib import test_hang_guard
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


class MemoryHoverTests(unittest.TestCase):
    def test_builds_page_has_no_navigation_or_framing_restriction(self) -> None:
        page = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        self.assertIn("<title>Builds</title>", page)
        self.assertIn('<h1 class="page-title">Builds</h1>', page)
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
                    "walk": [{"name": "running", "queued": True}],
                    "waiters": [{"name": "first", "since": 100, "detail": "research", "pid": 200}]}
                rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
                root = ElementTree.fromstring(f"<root>{rendered}</root>")
                chart = root.find("./div[@class='walk-chart']")
                assert chart is not None
                header = chart.find("./div[@class='walk-chart-head queue-head']")
                assert header is not None
                title = header.find("./span[@class='queue-title section-head']")
                assert title is not None
                self.assertEqual(title.text, expected)
                self.assertEqual(title.get("title"), expected)
                room = chart.find("./div[@class='wrow room']")
                assert room is not None
                children = list(chart)
                self.assertEqual(children.index(room) + 1, children.index(header))
                self.assertEqual(children[children.index(header) + 1].get("data-tip"), "wait:fixture-wait-0")
                table_rows = root.findall(".//tbody/tr")
                self.assertEqual([row.get("class") for row in table_rows],
                                 ["tools", "", "room", "queue-head", "wait wait-first"])
                self.assertEqual(root.findtext(".//tr[@data-tip='wait:fixture-wait-0']/td[@class='place']"), "1")
                self.assertEqual(root.findtext(".//tr[@data-tip='wait:fixture-wait-0']/th[@class='step']/span"), "first")

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

    def test_a_token_wait_is_the_stage_of_its_running_row(self) -> None:
        row = memory_tests.ProgressKeepAsideTests.reservation("nextest", need=4 * GIB)
        memory = admission_records.MeasuredMemory(9 * GIB, anonymous=admission_records.AnonymousRunUse(GIB))
        counter = progress.parse_progress("Finished `test` profile in 29s\n" + progress.token_wait_line(4) + "\n")
        adjusted = gate.progress_memory(row, memory, gate.AttributedStepUse(memory.anonymous, True), counter,
            progress.NoMatchingProfile("no profile"), admission_policy_fixture())
        inputs = memory_tests.ProgressKeepAsideTests.inputs((row,), (adjusted,))
        stage = memory_feed.row_progress(inputs.running, progress.TOKEN_WAIT_REASON)
        self.assertEqual(stage, {"kind": "TokenWaitProgress", "stage": "waiting for jobserver tokens", "tokens": 4})
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "waiters": [],
                "walk": [{"name": "hana nextest", "queued": True, "progress": stage}]}
        rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
        root = ElementTree.fromstring(f"<root>{rendered}</root>")
        cells = root.find(".//tr[@data-tip='row:fixture-run-0']")
        assert cells is not None
        self.assertEqual(cells[0].text, "waiting for jobserver tokens")
        self.assertEqual(cells[1].text, "—")

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
                              + "Pause below free RAM  12.0 GiB\nPool  paused\nPer-build counts  unavailable\n"
                              + "Because  steve cannot be asked"])
        unavailable = self.page_expression("tipText('tokens',data)",
            {"tokens": {"kind": "TokenPoolUnavailable", "reason": "stevie exited 1"}})
        self.assertEqual(unavailable, ["Tokens", "Status  unavailable\nBecause  stevie exited 1"])

    def test_the_mac_memory_note_follows_all_tooltip_fields(self) -> None:
        data = {"read_at": 200, "walk": [{"kind": "mac-build", "machine": "mac", "name": "hana nextest",
            "used": 3.5, "started_at": 100, "read_at": 190, "stage": {"kind": "MacTesting"},
            "progress": {"kind": "RowProgress", "phase": "testing", "unit": "tests", "done": 7, "total": 10, "percent": 70}}]}
        rows = cast(list[dict[str, object]], self.page_expression("tooltipRows('row:fixture-run-0', data)", data))
        self.assertEqual([(row["label"], row["value"]) for row in rows], [
            ("hana nextest", "running 2 min"), ("Runs on", "Mac"), ("Uses", "3.5 GiB"),
            ("Reading age", "<1 min"), ("In queue cap", "no"), ("Testing", "7 of 10 tests"), ("Done", "70.0%")])

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
        about = "Merges the handler checkpoint into the sdlc branch, tests every module, then pushes sdlc."
        row = {"name": "natemerge integrate", "queued": True, "used": 1, "reserved": 0, "counted": 1,
               "showrunner": "builds", "unit": "natemerge", "runner": "unit director",
               "about": about, "changes": "branch sdlc"}
        data = {"read_at": 200, "walk": [row], "waiters": [{**row, "need": 2, "since": 100, "why": "its turn"}]}
        for key in ("row:fixture-run-0", "wait:fixture-wait-0"):
            with self.subTest(key=key):
                html = cast(str, self.page_expression(f"tooltipHtml('{key}', data)", data, walk=True))
                grid = ElementTree.fromstring(html)
                cells = [(cell.text or "", cell.get("class") or "") for cell in grid]
                self.assertTrue(cells[0][0].startswith("natemerge · integrate"))
                self.assertIn((about, "caption"), cells[2:4])
                fields = [(cell.text, grid[index+1].text) for index, cell in enumerate(grid) if cell.tag == "dt"]
                self.assertEqual(fields[1:3], [("Changes", "branch sdlc"), ("Showrunner", "builds")])

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

    def test_cloud_hover_shows_the_progress_and_reservation(self) -> None:
        for status, counter in (("7 of 12 modules", progress.ProgressCounter("modules", 7, 12)),
                                 ("setup", progress.NoCounter("no cloud counter")), ("", progress.NoCounter("no cloud counter"))):
            with self.subTest(status=status):
                hover = cast(list[str], self.page_expression("tipText('row:fixture-run-0', data)", {"walk": [{
                    "kind": "cloud", "machine": "aws", "name": "merge-gate", "unit": "cloud-unit", "showrunner": "fixture",
                    "status": status, "activity": status, "progress": memory_feed.cloud_row_progress(counter)}]}))
                self.assertIn("Showrunner  fixture\nRuns on  AWS", hover[1])
                self.assertEqual("Status  " in hover[1], bool(status))
                self.assertNotIn("Uses", hover[1])
                if isinstance(counter, progress.ProgressCounter):
                    self.assertIn("Testing  7 of 12 modules\nDone  58.3%\nOrder  slowest first", hover[1])

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
                    {"name": "cloud test", "kind": "cloud", "machine": "aws",
                     "progress": {"kind": "RowProgress", "phase": "testing"}},
                    {"name": "cloud setup", "kind": "cloud", "machine": "aws",
                     "progress": {"kind": "NoProgress"}},
                    {"name": "Mac test", "kind": "cloud", "machine": "mac",
                     "progress": {"kind": "RowProgress", "phase": "testing"}},
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
                                 ["Stage", "Done", "Step", "Dur", "Unit", "Pid", "Target", "vCPU", "Slot", "Tokens"])
                self.assertEqual(table.findall(".//*[@class='stage']"), [])
                for index, expected in enumerate(("building", "testing", "—", "testing", "—", "testing", "—", "building")):
                    row = table.find(f"./tbody/tr[@data-tip='row:fixture-run-{index}']")
                    assert row is not None
                    self.assertEqual(row[0].get("class"), "place")
                    self.assertEqual(row[0].text, expected)
                    self.assertEqual(row.findtext("td[@class='slot']"), "2" if index == 0 else "3" if index == 2 else "—")
                    self.assertEqual(row[-1].get("class"), "tokens")
                    self.assertEqual(row[-2].get("class"), "slot")
                    self.assertEqual(row[-3].get("class"), "vcpus")
                queue_headers = table.findall("./tbody/tr[@class='queue-head']")
                spacers = root.findall("./div[@class='walk-chart']/div[@class='walk-chart-head queue-head']")
                self.assertEqual(len(queue_headers), int(waiting))
                self.assertEqual(len(spacers), int(waiting))
                if waiting:
                    self.assertEqual([cell.text for cell in queue_headers[0]],
                                     ["#", "Done", "Step", "Waiting", "Unit", "Pid", "Target", "vCPU"])
                    self.assertTrue(all(cell.get("scope") == "col" for cell in queue_headers[0]))
                    for index, expected in enumerate(("1", "cloud", "2")):
                        row = table.find(f"./tbody/tr[@data-tip='wait:fixture-wait-{index}']")
                        assert row is not None
                        self.assertEqual(row[0].text, expected)
                        self.assertIsNone(row.find("td[@class='slot']"))
                for row in table.findall("./tbody/tr"):
                    self.assertEqual(len(row), len(headers) - 2 * int(row.get("class", "").startswith(("wait", "queue-head"))))

    def test_ci_row_requires_a_running_job_and_preserves_the_used_total(self) -> None:
        for state in ("idle", "waiting", "unmatched", "busy", "unreadable"):
            with self.subTest(state=state):
                data = {"total": 64, "cap": 48, "taken": 4.125, "used": 3, "ci_ram": 0.125,
                        "ram": {**RAM_FIXTURE, "build_tools_bytes": 0, "rust_analyzer_bytes": 0,
                                "sccache_bytes": 0, "other_build_tools_bytes": 0, "queued_builds_bytes": 3 * GIB,
                                "ci_bytes": 0.125 * GIB, "build_covered_bytes": GIB, "room_bytes": 43.875 * GIB},
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
                    ci_unit = rows[0].find("td[@class='unit']/span")
                    assert ci_unit is not None
                    self.assertEqual(ci_unit.text, "CI")

    def test_mac_row_uses_its_stage_unit_memory_and_aligned_tooltip(self) -> None:
        for stage, expected in (("MacBuilding", "building"), ("MacTesting", "testing"),
                                ("MacTestingCounter", "testing"), ("MacStageUnavailable", "—")):
            with self.subTest(stage=stage):
                row = {"kind": "mac-build", "name": "hana nextest", "unit": "trunk", "pid": 710001,
                       "machine": "mac", "vcpus": 12, "repository": "hana", "showrunner": "hana", "started_at": 100, "read_at": 190,
                       "used": 3.5, "stage": {"kind": stage}}
                data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "walk": [row], "waiters": []}
                result = cast(list[str], self.page_expression(
                    "(renderWalk(data), [$('walk').innerHTML, tooltipHtml('row:fixture-run-0', data)])", data, walk=True))
                table = ElementTree.fromstring(f"<root>{result[0]}</root>")
                metadata = table.find(".//tr[@data-tip='row:fixture-run-0']")
                assert metadata is not None
                self.assertEqual(metadata[0].text, expected)
                unit = metadata.find("td[@class='unit']")
                vcpus = metadata.find("td[@class='vcpus']")
                assert unit is not None and vcpus is not None
                self.assertEqual(unit[0].text, "trunk")
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
        self.assertEqual([row[index].text for index in (0, 3, 5, 7)], ["—", "—", "—", "—"])
        self.assertEqual(row[4][0].text, "—")
        self.assertEqual(result[1].count("unavailable"), 1)

    def test_queue_notes_appear_once_in_their_row_tooltips(self) -> None:
        room = {"unavailable": "", "rows": [{"label": "Queue cap", "gib": 11.3},
            {"label": "− Builds", "gib": 3.2},
            {"label": "− Build reserved", "gib": 8.1}, {"label": "= Free", "gib": 0}]}
        for index, cause in enumerate(("memory", "its turn", "memory")):
            data = {"read_at": 200, "cap": 11.3,
                "ram": {**RAM_FIXTURE, "queued_builds_bytes": 3.2 * GIB, "build_tools_bytes": 0, "ci_bytes": 0,
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

    def test_fractional_memory_terms_sum_the_shown_leaves_and_short_by(self) -> None:
        ram = {**RAM_FIXTURE, "build_tools_bytes": 1.04 * GIB, "rust_analyzer_bytes": 1.04 * GIB,
               "sccache_bytes": 0, "other_build_tools_bytes": 0, "queued_builds_bytes": 1.04 * GIB,
               "ci_bytes": 1.04 * GIB, "build_covered_bytes": 1.04 * GIB, "room_bytes": 1.04 * GIB}
        waiters: list[dict[str, object]] = [
            {"name": "first", "need": 1.04, "free": 0.99},
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
        self.assertEqual((given["Free"], given["+ Given back by other steps"]), ("1.0 GiB", "1.0 GiB"))
        self.assertNotIn("Short by", given)
        kept = {str(row["label"]): str(row["value"]) for row in firsts.pop()}
        self.assertEqual((kept["Free"], kept["− Kept for other steps"], kept["Short by"]), ("1.0 GiB", "0.5 GiB", "0.6 GiB"))
        # The user, 11-16: the queue admits one step at a time, so a step after the first shows only what it needs.
        behind = cast(list[dict[str, object]], self.page_expression("tooltipRows('wait:fixture-wait-1', data)", data))
        self.assertEqual([row["label"] for row in behind if str(row["value"]).endswith(" GiB")], ["Needs"])
        self.assertEqual([row["value"] for row in tips[0] if row["kind"] != "caption"],
                         ["3.0 GiB", "1.0 GiB", "1.0 GiB", "1.0 GiB"])
        run = {str(row["label"]): str(row["value"]) for row in tips[1]}
        self.assertEqual((run["Uses"], run["+ Build reserved"], run["= In queue cap"]),
                         ("1.0 GiB", "1.0 GiB", "2.0 GiB"))
        for tip in firsts:
            fields = {str(row["label"]): str(row["value"]) for row in tip}
            free = fields.get("Free", fields.get("= Free", "0"))
            need_tenths = round(float(fields["Needs"].split()[0]) * 10)
            free_tenths = round(float(free.split()[0]) * 10)
            short_tenths = round(float(fields.get("Short by", "0").split()[0]) * 10)
            self.assertEqual(short_tenths, max(0, need_tenths - free_tenths))
        head = {str(row["label"]): round(float(str(row["value"]).split()[0]) * 10)
                for row in firsts[-1] if str(row["value"]).endswith(" GiB")}
        self.assertEqual(head["Queue cap"] - head["− Builds"] - head["− Build reserved"], head["= Free"])
        cap = cast(list[dict[str, object]], self.page_expression("tooltipRows('cap', data)", data))
        cap_fields = {str(row["label"]): str(row["value"]) for row in cap}
        self.assertEqual(given["Free"], cap_fields["+ Free"])
        for head_name, cap_name in (("Queue cap", "Queue cap"), ("− Builds", "Builds"),
                                   ("− Build reserved", "+ Build reserved"), ("= Free", "+ Free")):
            self.assertEqual(str(next(row["value"] for row in firsts[-1] if row["label"] == head_name)), cap_fields[cap_name])

    def test_fractional_totals_and_negative_leaves_share_one_shown_value(self) -> None:
        ram: dict[str, object] = {**RAM_FIXTURE, "rust_analyzer_bytes": 1.02 * GIB, "sccache_bytes": 0,
               "other_build_tools_bytes": 0, "build_tools_bytes": 1.02 * GIB,
               "queued_builds_bytes": 0, "ci_bytes": 0, "build_covered_bytes": 1.02 * GIB,
               "room_bytes": 1.02 * GIB, "desktop_bytes": 10 * GIB, "agents_bytes": 0,
               "programs": [], "smaller_programs_bytes": 0, "other_programs_bytes": 0,
               "compressed_swap_bytes": 0, "kernel_structures_bytes": 0, "kernel_counters_bytes": 0,
               "kernel_bytes": 0, "cache_bytes": 0, "unattributed_bytes": -GIB,
               "kernel_no_counter_bytes": 0}
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
        self.assertEqual(tips[2][0]["value"], "3.0 GiB")
        self.assertEqual(tips[3][0]["value"], "2.0 GiB")
        self.assertEqual(tips[5][0]["value"], "9.0 GiB")
        self.assertEqual(tips[5][-1]["value"], "-1.0 GiB")

    def test_taken_counts_the_full_reserve_above_the_cap(self) -> None:
        ram = {**RAM_FIXTURE, "build_covered_bytes": 6 * GIB, "build_missing_bytes": GIB,
               "build_target_bytes": 7 * GIB, "room_bytes": 0}
        data = {"ram": ram, "cap": 13, "taken": 13}
        rows = cast(list[dict[str, object]], self.page_expression("tooltipRows('walksum', data)", data))
        self.assertEqual([(row["label"], row["value"]) for row in rows], [
            ("Taken", "14.0 GiB"), ("", "Memory used or held by builds"),
            ("Builds", "7.0 GiB"), ("+ Build reserved (full)", "7.0 GiB"),
            ("Queue cap", "13.0 GiB"), ("Over cap", "1.0 GiB")])
        fields = {str(row["label"]): float(str(row["value"]).split()[0])
                  for row in rows if str(row["value"]).endswith(" GiB")}
        self.assertEqual(fields["Taken"], fields["Builds"] + fields["+ Build reserved (full)"])
        self.assertEqual(fields["Over cap"], fields["Taken"] - fields["Queue cap"])

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
                    self.assertEqual(rows[labels.index("Share held")]["value"], f"{share}%")
                    self.assertLess(labels.index("Share held"), labels.index("Target"))

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
                        self.assertEqual(fields["Share held"], "50%")
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

    def test_repository_slot_wait_has_no_memory_group(self) -> None:
        data = {"waiters": [{"kind": "repository-slot", "name": "widget build", "need": 0,
                            "since": 100, "reason": "waits for a free widget slot", "runs_on": "natedev"}]}
        rows = cast(list[dict[str, object]], self.page_expression("tooltipRows('wait:fixture-wait-0', data)", data))
        self.assertEqual({row["label"]: row["value"] for row in rows}["Waits for"], "build slot")
        self.assertTrue({"Needs", "Free", "Short by"}.isdisjoint(row["label"] for row in rows))

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
let listener = null;
const parent = {name: "console"};
const location = {origin: "http://natedev:8080", pathname: "/memory_live.html"};
const window = {frameElement: JSON.parse(process.argv[1])};
const SECONDS = 5;
const fetch = url => { calls.push(String(url).split("?")[0]); return new Promise(() => {}); };
const setInterval = () => 0;
const addEventListener = (kind, handler) => { listener = handler; };
""" + loop + """
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
const setInterval = () => 0;
const badge = {hidden: true};
const $ = () => badge;
const lastRead = null;
function updateReadingStatus() {}
const addEventListener = (kind, handler) => { handlers[kind] = handler; };
""" + loop + """
const steps = [];
const record = label => steps.push([label, calls.length, badge.hidden]);
const outside = {closest: () => null};
const press = (target, extra) => handlers.keydown({code: "Space", repeat: false, target, preventDefault() {}, ...extra});
record("loaded");
press(outside, {});
tick();
reloadWhenPageChanges();
record("paused");
const typed = [];
press({closest: selector => { typed.push(selector); return {}; }}, {});
press(outside, {repeat: true});
press(outside, {ctrlKey: true});
record("ignored");
handlers.message({source: {}, origin: location.origin, data: "console-space"});
record("foreign");
handlers.message({source: parent, origin: location.origin, data: "console-space"});
record("resumed");
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

    def page_expression(self, expression: str, data: object, *, walk: bool = False) -> object:
        html = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        functions = html[html.index("function field("):html.index("const tip =")]
        constants = ""
        if walk:
            functions += html[html.index("function renderWalk(d)"):html.index("function fitAxisLabels()")]
            functions += html[html.index("function esc(s)"):html.index("let lastRead =")]
            constants += """
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {style: {}, classList: {toggle() {}}});
  return elements.get(id);
};
function fitLabels() {}
"""
        javascript = functions + constants + "\nconst data = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n" + (
            "process.stdout.write(JSON.stringify(" + expression + "));\n")
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        payload: dict[str, object] = {"walk": [], "waiters": [], "swap_by": [], "ram": RAM_FIXTURE,
                                      "swap": SWAP_FIXTURE, **cast(dict[str, object], data)}
        for kind, rows in (("run", payload["walk"]), ("wait", payload["waiters"])):
            for index, row in enumerate(cast(list[dict[str, object]], rows)):
                _ = row.setdefault("key", f"fixture-{kind}-{index}")
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
            "g-free", "reserved", "room", "system", "safety", "swap", "shortfall", "g-else", *other])
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
        # The user, 11-15: the queue cap starts at 0 and ends at the end of free.
        self.assertIn('<div class="brace cap" id="b-cap" data-tip="cap">', bar)
        for removed in ("updateNativeOwnerTips", "pageOwnerTipFields", "nativeTipText", 'id="mark"', 'id="marklabel"'):
            self.assertNotIn(removed, page)

    def test_each_ram_block_tooltip_uses_the_design_text(self) -> None:
        keys = ["g-builds", "tools", "queued", "ci", "g-free", "reserved", "room", "system", "safety", "swap",
                "g-else", "desktop", "agents", "programs", "kernel", "cache", "unattributed", "cap", "budget"]
        tips = cast(list[list[str]], self.page_expression(
            "[" + ", ".join(f"tipText('{key}', data)" for key in keys) + "]", {"cap": 31, "cap_text": "fixture"}))
        self.assertEqual(dict(zip(keys, tips, strict=True)), {'g-builds': ['Builds  7.0 GiB', 'RAM that builds use now\nBuild tools  2.0 GiB\n+ Queued builds  3.0 GiB\n+ hana CI  2.0 GiB'], 'tools': ['Build tools  2.0 GiB', 'RAM that build tools use now\nrust-analyzer  1.0 GiB\n+ sccache  0.5 GiB\n+ Other build tools  0.5 GiB'], 'queued': ['Queued builds  3.0 GiB', 'RAM of builds the queue started'], 'ci': ['hana CI  2.0 GiB', 'RAM of hana CI programs'], 'g-free': ['Free RAM  30.0 GiB', 'RAM that no program holds\nBuild reserved  6.0 GiB\n+ Free  18.0 GiB\n+ System reserved  6.0 GiB'], 'reserved': ['Build reserved  6.0 GiB', 'Held for running builds to grow into'], 'room': ['Free  18.0 GiB', 'Where the next step can start\nQueue cap  31.0 GiB\n− Builds  7.0 GiB\n− Build reserved  6.0 GiB'], 'system': ['System reserved  6.0 GiB', 'RAM kept outside the queue cap\nSafety net  4.0 GiB\n+ Swap coming back  2.0 GiB'], 'safety': ['Safety net  4.0 GiB', 'RAM the queue always leaves free'], 'swap': ['Swap coming back  2.0 GiB', 'RAM held for swap that comes back\nShare held  50%'], 'g-else': ['Everything else  27.0 GiB', 'RAM used outside builds\nDesktop  3.0 GiB\n+ Agents  8.0 GiB\n+ Other programs  4.0 GiB\n+ Kernel  3.0 GiB\n+ File and kernel cache  7.0 GiB\n+ Not attributed  2.0 GiB'], 'desktop': ['Desktop  3.0 GiB', 'RAM of the desktop session\nProtected  2.0 GiB'], 'agents': ['Agents  8.0 GiB', 'RAM of agent sessions and their tools'], 'programs': ['Other programs  4.0 GiB', 'RAM of all other programs\nfirefox  2.0 GiB\nzed  1.0 GiB\n5 smaller programs  1.0 GiB'], 'kernel': ['Kernel  3.0 GiB', 'RAM the kernel uses for itself\nCompressed swap (zram)  1.5 GiB\n+ Slab, stacks, page tables  1.0 GiB\n+ Other kernel counters  0.5 GiB'], 'cache': ['File and kernel cache  7.0 GiB', 'Estimated cache outside free RAM'], 'unattributed': ['Not attributed  2.0 GiB', 'RAM that the readings do not explain\nKernel, no counter  1.5 GiB\n+ No program name  0.5 GiB'], 'cap': ['Queue cap  31.0 GiB', 'What builds may use\nBuilds  7.0 GiB\n+ Build reserved  6.0 GiB\n+ Free  18.0 GiB'], 'budget': ['RAM  64.0 GiB', 'Memory across this machine\nBuilds  7.0 GiB\n+ Free RAM  30.0 GiB\n+ Everything else  27.0 GiB']})
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
                shown = self.page_expression("reserveDescription(data.description, data.needed, data.covered)",
                    {"description": description, "needed": needed * GIB, "covered": covered * GIB})
                self.assertEqual(shown, expected)
        ram = {**RAM_FIXTURE, "available_bytes": 5 * GIB, "room_bytes": 0, "shortfall_bytes": 7 * GIB,
               "swap_covered_bytes": GIB, "build_covered_bytes": 0, "swap_missing_bytes": GIB,
               "build_missing_bytes": 6 * GIB, "rust_analyzer_bytes": 0, "sccache_bytes": 20 * MIB,
               "build_tools_bytes": 532 * MIB,
               "kernel_bytes": {"kind": "ReadingUnavailable", "reason": "meminfo is missing"},
               "kernel_no_counter_bytes": 0}
        tips = cast(list[list[str]], self.page_expression(
            "[tipText('shortfall', data), tipText('reserved', data), tipText('kernel', data), tipText('tools', data)," +
            " ramAmount(0), tipText('unattributed', data), tipText('system', data)]", {"ram": ram}))
        self.assertEqual(tips[0], ["Shortfall  7.0 GiB", "Reserves that free RAM does not cover\nUncovered swap  1.0 GiB\n+ Uncovered reserve  6.0 GiB"])
        self.assertEqual(tips[1], ["Build reserved  0.0 GiB", "Held for running builds to grow into\nTarget  6.0 GiB\nNot covered  6.0 GiB"])
        self.assertEqual(tips[2], ["Kernel  unavailable", ""])
        self.assertEqual(tips[3], ["Build tools  0.5 GiB", "RAM that build tools use now\nrust-analyzer  0.0 GiB\n+ sccache  0.0 GiB\n+ Other build tools  0.5 GiB"])
        self.assertEqual(tips[5], ["Not attributed  2.0 GiB", "RAM that the readings do not explain\nKernel, no counter  0.0 GiB\n+ No program name  2.0 GiB"])
        self.assertEqual(tips[4], "0.0 GiB")
        self.assertEqual(tips[6], ["System reserved  5.0 GiB", "RAM kept outside the queue cap\nSafety net  4.0 GiB\n+ Swap coming back  1.0 GiB"])
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
             "A GPU test waits: another GPU test holds the GPU test slot. It also needs 12.0 GiB, and free is 9.0 GiB."),
            ("pressure", RAM_FIXTURE, "Memory pressure prevents a new start; free is 18.0 GiB."),
            ("shortfall", {**RAM_FIXTURE, "room_bytes": 0, "shortfall_bytes": 3 * GIB},
             "Nothing is free: reserves exceed free RAM by 3.0 GiB."),
            ("no_room", {**RAM_FIXTURE, "room_bytes": 0},
             "Nothing is free: the safety net, swap coming back and build reserved use all free RAM."),
            ("one_running_memory_wait", RAM_FIXTURE, "One build runs; another needs 12.0 GiB, but free is 9.0 GiB."),
            ("none", RAM_FIXTURE, ""),
        ):
            with self.subTest(code=code):
                status = {"code": code, "active_local_build_count": 1, "next_need_bytes": 12 * GIB,
                          "check_room_bytes": 9 * GIB}
                self.assertEqual(self.page_expression("statusLine(data)", {"ram": ram, "memory_status": status}),
                                 expected)
        fits = {"code": "gpu_slot", "active_local_build_count": 1, "next_need_bytes": 2 * GIB, "check_room_bytes": 9 * GIB}
        self.assertEqual(self.page_expression("statusLine(data)", {"ram": RAM_FIXTURE, "memory_status": fits}),
                         "A GPU test waits: another GPU test holds the GPU test slot.")

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
