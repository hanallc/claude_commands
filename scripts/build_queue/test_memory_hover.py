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
from scripts.build_queue import test_memory_feed as feed_tests
from scripts.build_queue.admission_records import reason_phrase
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


def field_values(fields: ElementTree.Element) -> list[str | None]:
    """Read the value cells of a field list, without the colon cells."""
    return [cell.text for cell in fields.findall("dd") if cell.get("class") != "colon"]


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
                                  (60, "1 min"), (2580, "43 min"), (3600, "1 h 0 min")):
            with self.subTest(elapsed=elapsed):
                data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200_000,
                    "walk": [{"name": "running", "queued": True, "started_at": 200_000 - elapsed,
                              "used": 1, "reserved": 0, "counted": 1}],
                    "waiters": [{"name": "queued", "since": 200_000 - elapsed, "detail": "fixture",
                                 "pid": 200, "need": 1, "reason": "free memory"}]}
                rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
                root = ElementTree.fromstring(f"<root>{rendered}</root>")
                self.assertEqual(root.findtext(".//tr[@data-tip='row:0']/td[@class='duration']"), expected)
                self.assertEqual(root.findtext(".//tr[@data-tip='wait:0']/td[@class='duration']"), expected)
                for key in ("row:0", "wait:0"):
                    html = str(self.page_expression(f"tooltipHtml('{key}', data)", data, walk=True))
                    head = ElementTree.fromstring(f"<root>{html}</root>").find("b")
                    assert head is not None
                    self.assertEqual((head.get("class"), [span.text for span in head]), ("tip-head", [head[0].text, expected]))
                    self.assertNotIn("ago", html)

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
                self.assertEqual(children[children.index(header) + 1].get("data-tip"), "wait:0")
                table_rows = root.findall(".//tbody/tr")
                self.assertEqual([row.get("class") for row in table_rows],
                                 ["tools", "", "room", "queue-head", "wait wait-first"])
                self.assertEqual(root.findtext(".//tr[@data-tip='wait:0']/td[@class='place']"), "1")
                self.assertEqual(root.findtext(".//tr[@data-tip='wait:0']/th[@class='step']/span"), "first")

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
                row = root.find(".//tr[@data-tip='row:0']")
                assert row is not None
                self.assertEqual(row[0].text, phase)
                self.assertEqual(row[1].get("class"), "done")
                self.assertEqual(row[1].text, expected)
                self.assertEqual(root.findtext(".//tr[@data-tip='row:1']/td[@class='done']"), "—")

    def test_a_token_wait_is_the_stage_of_its_running_row(self) -> None:
        row = memory_tests.ProgressKeepAsideTests.reservation("nextest", need=4 * GIB)
        memory = admission_records.MeasuredMemory(9 * GIB, anonymous=admission_records.AnonymousRunUse(GIB))
        counter = progress.parse_progress("Finished `test` profile in 29s\n" + progress.token_wait_line(4) + "\n")
        adjusted = gate.progress_memory(row, memory, gate.AttributedStepUse(memory.anonymous, True), counter,
            progress.NoMatchingProfile("no profile"), admission_policy_fixture())
        inputs = memory_tests.ProgressKeepAsideTests.inputs((row,), (adjusted,))
        stage = memory_feed.row_progress(inputs.running)
        self.assertEqual(stage, {"kind": "TokenWaitProgress", "stage": "waiting for jobserver tokens", "tokens": 4})
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "waiters": [],
                "walk": [{"name": "hana nextest", "queued": True, "progress": stage}]}
        rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
        root = ElementTree.fromstring(f"<root>{rendered}</root>")
        cells = root.find(".//tr[@data-tip='row:0']")
        assert cells is not None
        self.assertEqual(cells[0].text, "waiting for jobserver tokens")
        self.assertEqual(cells[1].text, "—")

    def test_the_mac_memory_note_follows_all_tooltip_fields(self) -> None:
        data = {"read_at": 200, "walk": [{"kind": "mac-build", "machine": "mac", "name": "hana nextest",
            "used": 3.5, "started_at": 100, "read_at": 190, "stage": {"kind": "MacTesting"},
            "progress": {"kind": "RowProgress", "phase": "testing", "unit": "tests",
                         "done": 7, "total": 10, "percent": 70}}]}
        rendered = self.page_expression("tooltipHtml('row:0', data)", data, walk=True)
        root = ElementTree.fromstring(f"<root>{rendered}</root>")
        self.assertEqual(root[-1].get("class"), "mac-note")
        self.assertEqual(root[-1].text, "Mac memory does not count against natedev.")
        self.assertEqual(root[-2].tag, "dl")
        self.assertIn("70.0%", "".join(root[-2].itertext()))

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
        module_counter = memory_feed.row_progress(inputs.running)
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
                hover = cast(list[str], self.page_expression("tipText('row:0', data)", {"read_at": 400, "walk": [{
                    "name": name, "unit": "fixture", "showrunner": "fixture-production", "started_at": 100,
                    "used": 1, "reserved": 0.5, "counted": 1.5, "queued": True, "keep_text": "",
                    "need": memory_feed.gib(hold.memory.need), "need_part": True, "progress": counter}]}))
                self.assertEqual(hover[0], name)
                lines = ["Current memory: 1.0 GiB",
                         f"Historically uses {memory_feed.gib(hold.memory.need):.1f} GiB in the next part of the build.",
                         "The queue holds 0.5 GiB more for it, so it has room to grow to 1.5 GiB.",
                         f"Testing: 8 of 10 {counter['unit']} done."]
                if ordering_note:
                    lines.append("Modules start slowest first, so this reads low early.")
                self.assertEqual(hover[1], "\n".join(lines))
        for row, body in (({"between_steps": True, "used": 1, "reserved": 2},
                           "Between steps. The queue holds 2.0 GiB for its next step."),
                          ({"used": {"kind": "RunUseUnavailable", "reason": "private memory.stat is missing"}, "reserved": 2},
                           "Use unavailable: private memory.stat is missing. The queue holds 2.0 GiB for it."),
                          ({"used": 1, "reserved": 0, "started_at": 100},
                           "Current memory: 1.0 GiB\nHistorically uses 4.0 GiB.\n"
                           + "The queue holds no more memory for it.")):
            with self.subTest(body=body):
                hover = cast(list[str], self.page_expression("tipText('row:0', data)", {"read_at": 400, "walk": [{
                    "name": "merge-gate", "unit": "fixture", "queued": True, "keep_text": "", "counted": 1,
                    "need": 4.0, "need_part": False, **row}]}))
                self.assertEqual(hover[1], body)

    def test_every_running_and_queued_hover_starts_with_who_ran_it(self) -> None:
        owner = {"showrunner": "builds", "unit": "report"}
        for runner, extra, expected in (
            ("showrunner", {"unit": "—"}, [("showrunner", "builds"), ("unit", "this run is from the showrunner")]),
            ("unit director", {}, [("showrunner", "builds"), ("unit", "report"),
                                   ("worker", "this run is from the unit director")]),
            ("worker", {"worker": "impl2"}, [("showrunner", "builds"), ("unit", "report"), ("worker", "impl2")]),
            ("worker", {"unit": "—", "worker": "staff1"}, [("showrunner", "builds"),
                                                          ("unit", "this run is from the showrunner"), ("worker", "staff1")]),
            ("unknown", {"showrunner": "owner not recorded", "unit": "owner not recorded"}, []),
        ):
            row: dict[str, object] = {"name": "hana nextest", "queued": True, "used": 1, "reserved": 0, "counted": 1,
                                      "progress": {"kind": "NoProgress"}, **owner, "runner": runner, **extra}
            waiter: dict[str, object] = {"name": "hana nextest", "need": 2, "since": 100, "reason": "start spacing",
                                         **owner, "runner": runner, **extra}
            mac: dict[str, object] = {**row, "kind": "mac-build", "machine": "mac", "started_at": 100, "read_at": 190}
            data = {"read_at": 200, "walk": [row, mac], "waiters": [waiter]}
            for key in ("row:0", "row:1", "wait:0"):
                with self.subTest(runner=runner, extra=extra, key=key):
                    rendered = cast(str, self.page_expression(f"tooltipHtml('{key}', data)", data, walk=True))
                    root = ElementTree.fromstring(f"<root>{rendered}</root>")
                    self.assertEqual(root[0].tag, "b")
                    fields = expected + ([("reason", "start spacing")] if key == "wait:0" else [])
                    if not fields:
                        self.assertIsNone(root.find("dl[@class='identity']"), rendered)
                        self.assertNotIn("not recorded", rendered)
                        continue
                    self.assertEqual(root[1].get("class"), "identity")
                    names = [cell.text for cell in root[1].findall("dt")]
                    values = field_values(root[1])
                    self.assertEqual([cell.text for cell in root[1].findall("dd[@class='colon']")], [":"] * len(names))
                    self.assertEqual(list(zip(names, values, strict=True)), fields)
                    self.assertEqual({cell.get("aria-hidden") for cell in root[1].findall("dd[@class='colon']")}, {"true"})

    def test_a_ci_hover_keeps_its_ci_line(self) -> None:
        data = {"read_at": 200, "waiters": [], "walk": [{"name": "hana CI", "ci": True, "ci_label": "hana CI · runner-1",
                                                          "detail": "busy", "unit": "", "keep_text": "", "runner": "unknown"}]}
        rendered = cast(str, self.page_expression("tooltipHtml('row:0', data)", data, walk=True))
        root = ElementTree.fromstring(f"<root>{rendered}</root>")
        self.assertEqual([(cell.text) for cell in root[1].findall("dt")], ["CI"])
        self.assertEqual(field_values(root[1]), ["hana CI · runner-1"])

    def test_a_running_row_hover_ends_the_memory_text_before_the_source_sentence(self) -> None:
        for keep_text, queued, expected in (
            ("need 11.3 GiB\nfull need: no matching anonymous profile", True,
             "full need: no matching anonymous profile. The queue holds this memory for the run."),
            ("Memory the compiler cache holds. The queue does not start it.", False,
             "Memory the compiler cache holds. The queue does not start it. Host free memory already accounts for this use."),
            ("Memory these processes hold. The queue did not start them.", False,
             "Memory these processes hold. The queue did not start them. Host free memory already accounts for this use."),
            ("", False, "It reserves no memory. Host free memory already accounts for this use."),
        ):
            with self.subTest(keep_text=keep_text, queued=queued):
                hover = cast(list[str], self.page_expression("tipText('row:0', data)", {"read_at": 400, "walk": [{
                    "name": "fixture nextest", "unit": "fixture", "showrunner": "fixture-production", "used": 1,
                    "reserved": 0, "counted": 1, "queued": queued, "keep_text": keep_text, "started_at": 100}]}))
                self.assertTrue(hover[1].endswith(expected), hover[1])
                self.assertTrue(hover[1].startswith((keep_text or "It uses")[0].upper()), hover[1])
                self.assertNotIn("Started", hover[1])

    def test_cloud_hover_shows_the_progress_and_reservation(self) -> None:
        for status, counter, expected in (
            ("7 of 12 modules", progress.ProgressCounter("modules", 7, 12), "testing · 7 of 12 modules · 58.3% done"),
            ("setup", progress.NoCounter("no cloud counter"), "setup. Target: aws."),
            ("", progress.NoCounter("no cloud counter"), "Target: aws. No local memory reservation."),
        ):
            with self.subTest(status=status):
                hover = cast(list[str], self.page_expression("tipText('row:0', data)", {"walk": [{
                    "kind": "cloud", "machine": "aws", "name": "merge-gate", "unit": "cloud-unit", "showrunner": "fixture",
                    "status": status, "progress": memory_feed.cloud_row_progress(counter)}]}))
                self.assertIn(expected, hover[1])
                self.assertIn("Target: aws. No local memory reservation.", hover[1])
                owner = {"walk": [{"unit": "cloud-unit", "showrunner": "fixture"}]}
                identity = ElementTree.fromstring(
                    f"<root>{self.page_expression('identityHtml(data.walk[0])', owner, walk=True)}</root>")
                fields = identity.find("dl")
                assert fields is not None
                self.assertEqual(field_values(fields), ["fixture", "cloud-unit"])
                self.assertFalse(hover[1].startswith("."), hover[1])

    def test_progress_percent_rounds_down_until_the_counter_is_complete(self) -> None:
        for done, total, percent, expected in (
            (835, 836, 100 * 835 / 836, "99.8%"),
            (836, 836, 100, "100.0%"),
            (999, 1000, 100, "99.9%"),
            (0, 836, 0, "0.0%"),
        ):
            with self.subTest(done=done, total=total, percent=percent):
                text = self.page_expression("progressTip(data)", {"progress": {
                    "kind": "RowProgress", "phase": "building", "unit": "units",
                    "done": done, "total": total, "percent": percent}})
                self.assertIn(f"building · {done} of {total} units · {expected} done", str(text))

    def test_pressure_percent_uses_one_decimal_and_rounds_down(self) -> None:
        hover = cast(list[list[str]], self.page_expression(
            "[tipText('g-some', data), tipText('g-full', data), tipText('g-swap', data)]", {
            "psi": {"some": 1.29, "full": 0.09}, "limits": {"some": 25, "full": 5}}))
        self.assertEqual(hover, [
            ["Some tasks stall · 1.2%", "Memory waits affecting at least one task.\n" +
             "The reading uses a ten-second average.\nThe queue holds above 25.0%."],
            ["All tasks stall · 0.0%", "Memory waits affecting all active tasks.\n" +
             "The reading uses a ten-second average.\nThe queue holds above 5.0%."],
            ["Swap used · 3.0 GiB", "Swap capacity: 8.0 GiB.\nSwap use alone does not hold the queue."]])

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
                                 ["Stage", "Done", "Step", "Dur", "Unit", "Pid", "Target", "vCPU", "Slot"])
                self.assertEqual(table.findall(".//*[@class='stage']"), [])
                for index, expected in enumerate(("building", "testing", "—", "testing", "—", "testing", "—", "building")):
                    row = table.find(f"./tbody/tr[@data-tip='row:{index}']")
                    assert row is not None
                    self.assertEqual(row[0].get("class"), "place")
                    self.assertEqual(row[0].text, expected)
                    self.assertEqual(row.findtext("td[@class='slot']"), "2" if index == 0 else "3" if index == 2 else "—")
                    self.assertEqual(row[-1].get("class"), "slot")
                    self.assertEqual(row[-2].get("class"), "vcpus")
                queue_headers = table.findall("./tbody/tr[@class='queue-head']")
                spacers = root.findall("./div[@class='walk-chart']/div[@class='walk-chart-head queue-head']")
                self.assertEqual(len(queue_headers), int(waiting))
                self.assertEqual(len(spacers), int(waiting))
                if waiting:
                    self.assertEqual([cell.text for cell in queue_headers[0]],
                                     ["#", "Done", "Step", "Waiting", "Unit", "Pid", "Target", "vCPU"])
                    self.assertTrue(all(cell.get("scope") == "col" for cell in queue_headers[0]))
                    for index, expected in enumerate(("1", "cloud", "2")):
                        row = table.find(f"./tbody/tr[@data-tip='wait:{index}']")
                        assert row is not None
                        self.assertEqual(row[0].text, expected)
                        self.assertIsNone(row.find("td[@class='slot']"))
                for row in table.findall("./tbody/tr"):
                    self.assertEqual(len(row), len(headers) - int(row.get("class", "").startswith(("wait", "queue-head"))))

    def test_ci_row_requires_a_running_job_and_preserves_the_used_total(self) -> None:
        for state in ("idle", "waiting", "unmatched", "busy", "unreadable"):
            with self.subTest(state=state):
                data = {"total": 64, "cap": 48, "taken": 4.125, "used": 3, "ci_ram": 0.125,
                        "read_at": 200, "waiters": [], "walk": [{
                            "name": "hana CI", "ci": True, "admitted": state == "busy",
                            "ci_runners": [{"state": "busy" if state in ("waiting", "unmatched") else state}],
                            "used": 0.125, "queued": False, "started_at": 100,
                            "progress": {"kind": "NoProgress"}}]}
                result = cast(list[object], self.page_expression(
                    "(renderWalk(data), [$('walk').innerHTML, $('walk-sum').textContent, data.ci_ram, data.used])",
                    data, walk=True))
                root = ElementTree.fromstring(f"<root>{result[0]}</root>")
                rows = root.findall(".//tr[@data-tip='row:0']")
                chart_rows = root.findall(".//div[@data-run='0']")
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
                    "(renderWalk(data), [$('walk').innerHTML, tooltipHtml('row:0', data)])", data, walk=True))
                table = ElementTree.fromstring(f"<root>{result[0]}</root>")
                metadata = table.find(".//tr[@data-tip='row:0']")
                assert metadata is not None
                self.assertEqual(metadata[0].text, expected)
                unit = metadata.find("td[@class='unit']")
                vcpus = metadata.find("td[@class='vcpus']")
                assert unit is not None and vcpus is not None
                self.assertEqual(unit[0].text, "trunk")
                self.assertEqual(vcpus.text, "12")
                owner = table.find(".//div[@data-run='0']/span/span[@class='owner']")
                assert owner is not None
                self.assertEqual(owner.text, "hana")
                tooltip = ElementTree.fromstring(f"<root>{result[1]}</root>")
                title = tooltip.find("b")
                sentence = tooltip.find("span")
                assert title is not None and sentence is not None
                self.assertEqual((title.get("class"), [span.text for span in title]), ("tip-head", ["hana nextest · 3.5 GiB", "2 min"]))
                self.assertEqual(sentence.text, "Mac memory does not count against natedev.")
                labels = [cell.text for cell in tooltip.findall("dl/dt")]
                values = [value for fields in tooltip.findall("dl") for value in field_values(fields)]
                self.assertEqual(labels, ["showrunner", "unit", "Target", "Used", "Reading age"])
                fields = dict(zip(labels, values, strict=True))
                self.assertEqual(fields["showrunner"], "hana")
                self.assertEqual(fields["unit"], "trunk")
                self.assertEqual(fields["Target"], "mac")
                self.assertIn("3.5 GiB", result[1])

    def test_mac_failed_reading_shows_a_short_status_and_unknown_cells(self) -> None:
        data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200, "waiters": [], "walk": [{
            "kind": "mac-unavailable", "name": "Mac builds", "unit": "—", "machine": "mac",
            "started_at": 0, "read_at": 0, "used": {"kind": "MacMemoryUnavailable"},
            "stage": {"kind": "MacStageUnavailable"}, "status": "Mac reading is old · age unavailable"}]}
        result = cast(list[str], self.page_expression(
            "(renderWalk(data), [$('walk').innerHTML, tooltipHtml('row:0', data)])", data, walk=True))
        root = ElementTree.fromstring(f"<root>{result[0]}</root>")
        row = root.find(".//tr[@data-tip='row:0']")
        assert row is not None
        self.assertEqual([row[index].text for index in (0, 3, 5, 7)], ["—", "—", "—", "—"])
        self.assertEqual(row[4][0].text, "—")
        self.assertEqual(result[1].count("unavailable"), 3)

    def test_queue_notes_appear_once_in_their_row_tooltips(self) -> None:
        priority = "queue priority: port (the user's bump)"
        swap_note = ("Other programs have 2.0 GiB of memory in swap. "
                     "It comes back into RAM when they run, so the queue holds RAM for it.")
        room = {"rows": [{"label": "Free memory now", "gib": 10.5}, {"label": "− Safety margin", "gib": 3.0},
                         {"label": "− Other programs' swap, which can come back", "gib": 2.0},
                         {"label": "− Held for running builds", "gib": 6.0},
                         {"label": "= Free for builds", "gib": 0.0}], "note": swap_note}
        data = {"read_at": 200, "waiters": [
            {"name": "port nextest", "unit": "port", "need": 1.9, "since": 100, "place": 1,
             "why": "only 0.0 GiB is free for builds. It starts when 1.9 GiB is free.",
             "reason": "free memory", "head_room": room, "priority_text": priority, "runs_on": "natedev only"},
            {"name": "trunk nextest", "unit": "trunk", "need": 2, "since": 100, "place": 2,
             "why": "it is behind the first build in line, which gets memory first.",
             "reason": "behind #1: the first step in line gets memory first",
             "runs_on": "natedev or Mac"},
            {"name": "fps nextest", "unit": "fps", "need": 2, "since": 100, "place": 3,
             "why": "its memory estimate expired. The queue reads a new estimate before it starts.",
             "reason": "estimate expired"}]}
        expected = ("\n".join((
            "It is first in line.", "It needs 1.9 GiB to start.",
            "Why it waits: only 0.0 GiB is free for builds. It starts when 1.9 GiB is free.",
            "can run on: natedev only", "Free memory now: 10.5 GiB", "− Safety margin: 3.0 GiB",
            "− Other programs' swap, which can come back: 2.0 GiB", "− Held for running builds: 6.0 GiB",
            "= Free for builds: 0.0 GiB", swap_note, priority)),
            "It is number 2 in line.\nIt needs 2.0 GiB to start.\n"
            + "Why it waits: it is behind the first build in line, which gets memory first.\ncan run on: natedev or Mac",
            "It is number 3 in line.\nIt needs 2.0 GiB to start.\n"
            + "Why it waits: its memory estimate expired. The queue reads a new estimate before it starts.")
        for index, body in enumerate(expected):
            with self.subTest(index=index):
                hover = cast(list[str], self.page_expression(f"tipText('wait:{index}', data)", data))
                self.assertEqual(hover[1], body)
                self.assertNotIn("Head", hover[1])
                html = str(self.page_expression(f"tooltipHtml('wait:{index}', data)", data, walk=True))
                self.assertEqual(html.count("Free for builds"), int(index == 0))
                self.assertEqual(html.count(priority), int(index == 0))
        page = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        for removed in ("priority-line", "head-line", "wait-reasons", "row-time", ".wait-reason"):
            self.assertNotIn(removed, page)
        for unmatched in ("", priority):
            with self.subTest(unmatched=unmatched):
                heading = str(self.page_expression("tipText('walkhead', data)", {
                    **data, "unmatched_priority_text": unmatched}))
                self.assertEqual(heading.count(priority), int(bool(unmatched)))

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

    def page_expression(self, expression: str, data: object, *, walk: bool = False) -> object:
        html = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        functions = html[html.index("const TIPS ="):html.index("const tip =")]
        constants = html[html.index("const f1 ="):html.index("function setSeg(")]
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
        result = subprocess.run([executable, "-e", javascript], input=json.dumps(payload),
            text=True, capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return cast(object, json.loads(result.stdout))

    def test_each_wait_state_has_a_page_hover_reason(self) -> None:
        for reason, expected in (
            (admission_records.PressureUnavailable("cannot read /private/proc/pressure/memory"), "/private/proc/pressure/memory"),
            (admission_records.StartSpacingWait(110), "start spacing"),
            (admission_records.BehindHead("first", 1, "head-session"), "behind"),
        ):
            with self.subTest(reason=reason):
                reading = feed_tests.with_wait_reason(feed_tests.captured_reading(), reason)
                waiter = reading.view.waiting[0]
                hover = self.page_expression("tipText('wait:0', data)", {"read_at":200_000, "waiters":[{
                    "name":waiter.step, "unit":"", "showrunner":"", "need":1, "since":waiter.since,
                    "reason":reason_phrase(reason, reading.view.waiting)}]})
                self.assertIn(expected, str(hover))

    def test_a_mac_only_wait_hover_names_what_holds_the_mac_below_can_run_on(self) -> None:
        waiter = {"name":"tile nextest", "unit":"", "showrunner":"", "need":1, "since":200_000 - 240,
                  "reason":"start spacing", "runs_on":"Mac only", "mac_state":"build hold by research (hana) until released — Mac CI restore"}
        hover = cast(list[str], self.page_expression("tipText('wait:0', data)", {"read_at":200_000, "waiters":[waiter]}))
        lines = hover[1].split("\n")
        self.assertEqual(lines[lines.index("can run on: Mac only") + 1], "Mac: build hold by research (hana) until released — Mac CI restore")
        del waiter["mac_state"]
        hover = cast(list[str], self.page_expression("tipText('wait:0', data)", {"read_at":200_000, "waiters":[waiter]}))
        self.assertNotIn("Mac:", hover[1])

    def test_missing_run_use_has_a_page_hover_reason(self) -> None:
        hover = self.page_expression("tipText('row:0', data)", {"read_at":200_000, "walk":[{
            "name":"fixture", "unit":"", "showrunner":"", "used":{
                "kind":"RunUseUnavailable", "reason":"private memory.stat is missing"},
            "reserved":4, "counted":4, "queued":True, "keep_text":"full reservation", "progress":{
                "kind":"NoProgress", "reason":"no counter"}}]})
        self.assertIn("private memory.stat is missing", str(hover))
        self.assertNotIn("NaN", str(hover))

    def test_missing_pressure_keeps_the_other_limit_readings(self) -> None:
        result = self.page_expression("[level(data), tipText('g-full', data), tipText('cap', data)]", {
            "psi":{"some":{"kind":"PressureNotRecorded", "reason":"private PSI is missing"}, "full":1},
            "limits":{"some":25,"full":5}, "pressure":"none", "cap":20,
            "cap_text":"fixture room"})
        self.assertIn("neutral", str(result))
        self.assertIn("1.0", str(result))
        self.assertIn("20.0 GiB", str(result))

    def test_the_bar_and_legend_show_the_blocks_in_their_groups(self) -> None:
        page = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        builds, free, other = ["tools", "queued", "ci"], ["reserved", "room", "safety", "swap"], [
            "desktop", "agents", "programs", "kernel", "cache", "unattributed"]
        bar = page[page.index('<div class="bar-wrap'):page.index('<div class="scale')]
        legend = page[page.index('<div class="legend">'):page.index('<details class="room-explainer">')]
        self.assertEqual([part.split('"')[0] for part in bar.split('id="s-')[1:]], [*builds, *free, *other])
        # The user, 11-15: each legend group sits under its own heading, so the legend ties to the bar.
        self.assertEqual([part.split('"')[0] for part in legend.split('data-tip="')[1:]], [
            "g-builds", *builds, "cap", "g-builds", "reserved", "room", "cap",
            "g-free", "reserved", "room", "system", "safety", "swap", "shortfall", "g-else", *other])
        # The user, 11-15: the waiting and progress key sits under the Running and Queued chart.
        walk_card = page[page.index('<div class="walk" id="walk">'):page.index('<section class="card" id="leftovers"')]
        key = walk_card[walk_card.index('<div class="walk-key">'):]
        self.assertEqual([part.split('"')[0] for part in key.split('data-tip="')[1:]], ["waiting", "progress"])
        self.assertIn('<details class="legend-box" open>', page)
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
        self.assertEqual(dict(zip(keys, tips, strict=True)), {
            "g-builds": ["Builds · 7.0 GiB", "RAM that builds use now.\nBuild tools: 2.0 GiB.\n" +
                         "Queued builds: 3.0 GiB.\nhana CI: 2.0 GiB."],
            "tools": ["Build tools · 2.0 GiB", "RAM that build tools use now.\n" +
                      "rust-analyzer: 1.0 GiB.\nsccache: 0.5 GiB.\nOther build tools: 0.5 GiB."],
            "queued": ["Queued builds · 3.0 GiB", "RAM that the builds the queue started use now."],
            "ci": ["hana CI · 2.0 GiB", "RAM used by hana CI programs.\nFile cache and kernel memory appear separately."],
            "g-free": ["Free RAM · 30.0 GiB", "RAM that no program holds. It includes cache that Linux can take back.\n" +
                       "The queue divides it into build reserved, free and system reserved."],
            "reserved": ["Build reserved · 6.0 GiB", "RAM the queue holds for running builds.\n" +
                         "The queue predicts the peak of each build, and the build grows into it."],
            "room": ["Free · 18.0 GiB", "RAM where the next waiting step can start.\nFree RAM: 30.0 GiB.\n" +
                     "− Safety net: 4.0 GiB.\n− Swap coming back: 2.0 GiB.\n− Build reserved: 6.0 GiB."],
            "system": ["System reserved · 6.0 GiB",
                       "RAM the queue never gives to builds.\nSafety net: 4.0 GiB.\nSwap coming back: 2.0 GiB."],
            "safety": ["Safety net · 4.0 GiB",
                       "RAM the queue always leaves free, so the machine does not run out of memory.\nA fixed setting: 4.0 GiB."],
            "swap": ["Swap coming back · 2.0 GiB", "Empty RAM the queue holds for memory in swap, mostly agents' memory.\n" +
                     "That memory can come back into RAM at any time."],
            "g-else": ["Everything else · 27.0 GiB",
                       "RAM that the desktop, agents, other programs, the kernel and the cache use."],
            "desktop": ["Desktop · 3.0 GiB",
                        "RAM that the desktop session uses, so the mouse and keyboard stay responsive.\n" +
                        "The system never takes back the first 2.0 GiB."],
            "agents": ["Agents · 8.0 GiB",
                       "RAM used by agent sessions and their tools.\nFile cache and kernel memory appear separately."],
            "programs": ["Other programs · 4.0 GiB",
                         "RAM that programs other than builds, agents and the desktop use.\nfirefox: 2.0 GiB\nzed: 1.0 GiB\nand 5 smaller programs: 1.0 GiB"],
            "kernel": ["Kernel · 3.0 GiB",
                       "RAM that the kernel uses for itself.\nCompressed swap (zram): 1.5 GiB.\nSlab, stacks and page tables: 1.0 GiB.\n" +
                       "Other kernel counters: 0.5 GiB.\n" +
                       "Reclaimable kernel cache appears in the cache block."],
            "cache": ["File and kernel cache · 7.0 GiB", "Estimated cache outside available RAM.\n" +
                      "Includes file cache and reclaimable kernel cache.\nPrograms can close while their file cache remains."],
            "unattributed": ["Not attributed · 2.0 GiB", "RAM that the readings do not explain.\n" +
                             "Kernel memory with no counter, such as driver memory: 1.5 GiB.\n" +
                             "Memory with no program name: 0.5 GiB."],
            "cap": ["Queue cap · 31.0 GiB", "What builds may use.\nBuilds: 7.0 GiB.\n+ Build reserved: 6.0 GiB.\n" +
                    "+ Free: 18.0 GiB.\nA step starts when the memory it needs fits in free."],
            "budget": ["RAM · 64.0 GiB", "The bar divides this machine's RAM into builds, free RAM and everything else.\n" +
                       "Free RAM includes cache that Linux can take back."],
        })
        blocks = cast(int, self.page_expression("RAM_BLOCKS.reduce((sum, [, , amount]) => sum + amount(data.ram), 0)", {}))
        self.assertEqual(blocks, 64 * GIB)
        groups = cast(list[int], self.page_expression("['builds', 'free', 'else'].map(group => groupBytes(data.ram, group))", {}))
        self.assertEqual(groups, [7 * GIB, 30 * GIB, 27 * GIB])

    def test_missing_reserves_small_amounts_and_unavailable_readings_have_their_own_text(self) -> None:
        ram = {**RAM_FIXTURE, "available_bytes": 5 * GIB, "room_bytes": 0, "shortfall_bytes": 3 * GIB,
               "swap_covered_bytes": GIB, "build_covered_bytes": 0, "swap_missing_bytes": GIB,
               "build_missing_bytes": 6 * GIB, "rust_analyzer_bytes": 0, "sccache_bytes": 20 * MIB,
               "build_tools_bytes": 532 * MIB,
               "kernel_bytes": {"kind": "ReadingUnavailable", "reason": "meminfo is missing"},
               "kernel_no_counter_bytes": 0}
        tips = cast(list[list[str]], self.page_expression(
            "[tipText('shortfall', data), tipText('reserved', data), tipText('kernel', data), tipText('tools', data)," +
            " ramAmount(0), tipText('unattributed', data), tipText('system', data)]", {"ram": ram}))
        self.assertEqual(tips[0], ["Shortfall · 3.0 GiB",
                                   "Reserves that free RAM does not cover.\nSwap coming back: 1.0 GiB.\nBuild reserved: 6.0 GiB."])
        self.assertEqual(tips[1], ["Build reserved · 0.0 GiB", "RAM the queue holds for running builds.\n" +
                                   "The queue predicts the peak of each build, and the build grows into it.\n" +
                                   "Free RAM does not cover 6.0 GiB of it."])
        self.assertEqual(tips[2], ["Kernel · unavailable",
                                   "The latest reading is unavailable.\nThis amount is unknown."])
        self.assertEqual(tips[3], ["Build tools · 0.5 GiB",
                                   "RAM that build tools use now.\nsccache: <0.1 GiB.\nOther build tools: 0.5 GiB."])
        self.assertEqual(tips[5], ["Not attributed · 2.0 GiB",
                                   "RAM that the readings do not explain.\nNo program name is available for this amount."])
        self.assertEqual(tips[4], "0.0 GiB")
        self.assertEqual(tips[6], ["System reserved · 5.0 GiB",
                                   "RAM the queue never gives to builds.\nSafety net: 4.0 GiB.\nSwap coming back: 1.0 GiB."])
        empty = {**RAM_FIXTURE, "build_tools_bytes": 0, "queued_builds_bytes": 0, "rust_analyzer_bytes": 0,
                 "sccache_bytes": 0, "other_build_tools_bytes": 0}
        self.assertEqual(self.page_expression("[tipText('tools', data), tipText('g-builds', data)]", {"ram": empty}),
                         [["Build tools · 0.0 GiB", "RAM that build tools use now.\nNo build tool RAM is recorded."],
                          ["Builds · 2.0 GiB", "RAM that builds use now.\nhana CI: 2.0 GiB."]])

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
            ["Agents (3) · 1.0 GiB", "Swap charged to agent sessions and their tools."],
            ["hana nextest · 3.0 MiB", "Swap charged to this program or build.\nShowrunner: builds\nUnit: report"],
            ["2 smaller groups · 2.0 KiB", "Combined swap of 2 named groups.\nThe expanded list shows each group."],
            ["Unclassified groups · 512 B", "Swap charged to groups without a program name."],
            ["Not attributed · 4.0 MiB", "Swap without a matching group reading."],
            ["Reading difference · −2.0 MiB",
             "Group readings exceed the host total.\nThis is an accounting difference, not negative swap."],
            ["All swap used · 3.0 GiB", "Swap space used across this machine.\n" +
             "Includes builds, CI, agents, and other programs.\nSome pages can also have RAM copies."],
            ["Outside builds and CI · 2.0 GiB", "Swap used outside the builds and CI groups.\n" +
             "Queue reserve share: 50%.\nSwap coming back: 2.0 GiB."],
            ["Builds and CI · 1.0 GiB",
             "Swap used by builds, build tools, and CI.\nThe queue excludes this from its swap reserve target."],
        ])
