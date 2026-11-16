from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.disks import disks_page
from scripts.lib import file_lock, test_hang_guard
from scripts.review_links import page_frame

GIB = 1 << 30
KEEP = 256 << 20
SETTINGS: disks_page.PageSettings = {"slider_min_gib": 1, "slider_default_gib": 10, "cache_seconds": 600, "poll_seconds": 5,
                                     "wait_seconds": 3600, "sessions_shown": 3,
                                     "names": {"/build": "Build </script> drive"}}


def folder(name: str, size: int, children: list[dict[str, object]] | None = None) -> dict[str, object]:
    listed = children or []
    return {"name": name, "size": size, "other": size - sum(cast(int, child["size"]) for child in listed), "children": listed}


TREE = folder("/build", 100 * GIB, [
    folder("cargo", 60 * GIB, [folder("slot1", 30 * GIB, [folder("debug", 20 * GIB)]), folder("slot2", 20 * GIB),
                               folder("slot3", 5 * GIB), folder("slot4", 4 * GIB)]),
    folder("paths", 30 * GIB, [folder("home", 2 * GIB)]),
    folder("small", 3 * GIB),
])


class DisksPageTests(unittest.TestCase):
    def test_publish_writes_the_frame_style_and_settings_into_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = disks_page.publish(Path(directory) / "page", SETTINGS).read_text()
        self.assertIn(page_frame.STYLE, page)
        self.assertNotIn("__", page.split("<script>", 1)[0].split("</style>", 1)[1])
        settings_text = page.split('type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(settings_text.replace("<\\/", "</")), SETTINGS)

    def test_the_size_filter_names_top_folders_only_and_other_opens_to_list_the_rest(self) -> None:
        # The user, 11-16: the size filter is for the top level only; the Other row opens.
        rows = self.rows(10 * GIB, ["/build/cargo"])
        self.assertEqual([(row["name"], row["depth"], row["kind"]) for row in rows], [
            ("/build/cargo", 0, "open"), ("slot1", 1, "shut"), ("slot2", 1, "leaf"), ("slot3", 1, "leaf"), ("slot4", 1, "leaf"),
            ("Files and small folders", 1, "files"), ("/build/paths", 0, "shut"), ("/build/small", 0, "leaf"),
            ("Files and small folders", 0, "files")])
        rows = self.rows(31 * GIB, ["other:/build", "/build/paths"])
        self.assertEqual([(row["name"], row["depth"], row["kind"], cast(int, row["size"]) // GIB, cast(int, row["parentSize"]) // GIB,
                           cast(int, row["start"]) // GIB)
                          for row in rows], [
            ("/build/cargo", 0, "shut", 60, 100, 0), ("Other · 2 folders and files", 0, "open", 40, 100, 60),
            ("/build/paths", 1, "open", 30, 40, 60), ("home", 2, "leaf", 2, 30, 60), ("Files and small folders", 2, "files", 28, 30, 62),
            ("/build/small", 1, "leaf", 3, 40, 90), ("Files and small folders", 1, "files", 7, 40, 93)])

    def test_each_row_starts_where_the_row_before_it_ends_inside_its_parent(self) -> None:
        rows = self.rows(GIB, ["/build/cargo", "/build/cargo/slot1"])
        debug = next(row for row in rows if row["name"] == "debug")
        self.assertEqual((debug["path"], debug["start"], debug["parentStart"]), ("/build/cargo/slot1/debug", 0, 0))
        top = [row for row in rows if row["depth"] == 0]
        self.assertEqual([row["start"] for row in top], [0, 60 * GIB, 90 * GIB, 93 * GIB])
        self.assertEqual(sum(cast(int, row["size"]) for row in top), 100 * GIB)

    def test_sizes_times_and_the_floor_note_use_the_page_text(self) -> None:
        calls = ("size(3 * GIB / 2)", "size(734 * GIB)", "size(2.6 * 1024 * GIB)", "size(300 * 1024 ** 2)", "share(1, 400)",
                 "age(30)", "age(60)", "age(7200)", "floorNote({floor: 300 * GIB, total: 1500 * GIB})",
                 "floorNote({floor: null, total: 1})")
        result = cast(list[str], self.run_page(f"[{', '.join(calls)}]"))
        self.assertEqual(result, ["1.5 GiB", "734 GiB", "2.6 TiB", "300 MiB", "<1%", "less than a minute ago",
                                  "1 minute ago", "2 hours ago", "Cleanup starts below 300 GiB free (20%).",
                                  "The cleanup floor is unknown."])

    def test_the_slider_runs_on_a_log_scale_from_the_least_value_to_the_largest_top_folder(self) -> None:
        calls = ("range.min", "range.max", "toSlider(range.min, range)", "toSlider(range.max, range)",
                 "fromSlider(SLIDER_STEPS / 2, range)", "startLimit(range, data.settings)")
        result = cast(list[float], self.run_page(
            f"(() => {{ const range = sliderRange(data.tree, data.settings); return [{', '.join(calls)}]; }})()"))
        self.assertEqual(result[:4], [GIB, 60 * GIB, 0, 1000])
        middle: float = result[4]
        self.assertAlmostEqual(middle, GIB * math.sqrt(60), delta=1)
        self.assertEqual(result[5], 10 * GIB)

    def test_the_status_line_names_the_age_then_the_measure_then_a_problem(self) -> None:
        recent = "statusText(Date.now() / 1000 - 120, Date.now() / 1000, {}).replace(/^Sizes from [^,]+, /, '')"
        calls = ("statusText(null, 0, true, '', 600)", "statusText(null, 0, false, '', 600)",
                 "statusText(null, 0, false, 'The measure did not finish.', 600)",
                 recent.format("false, '', 600"), recent.format("true, 'The measure did not finish.', 600"))
        self.assertEqual(self.run_page(f"[{', '.join(calls)}]"), [
            "Measuring the drives. The first measure takes some minutes.", "No sizes yet.", "The measure did not finish.",
            "2 minutes ago. The page measures again when you open it and the sizes are more than 10 minutes old.",
            "2 minutes ago. Measuring… The measure did not finish."])

    def test_publish_writes_the_page_only_when_its_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = disks_page.publish(Path(directory), SETTINGS)
            os.utime(page, (0, 0))
            _ = disks_page.publish(Path(directory), SETTINGS)
            self.assertEqual(page.stat().st_mtime, 0)
            _ = disks_page.publish(Path(directory), {**SETTINGS, "poll_seconds": 6})
            self.assertNotEqual(page.stat().st_mtime, 0)

    def test_the_slots_card_shows_each_group_and_each_slot_with_its_use(self) -> None:
        now = 1000 * 86400
        report = {"root": "/build/slot", "groups": [
            {"repository": "hana", "maximum": 8, "slots": [
                {"name": "hana-slot1", "number": 1, "size": 70 * GIB, "last_used": now, "deleted_tonight": False,
                 "used_by": {"purpose": "fps", "unit": "report", "showrunner": "builds"}},
                {"name": "hana-slot2", "number": 2, "size": 0, "last_used": now - 9 * 86400, "used_by": None,
                 "deleted_tonight": True}]},
            {"repository": "<b>", "maximum": None, "slots": [
                {"name": "x-slot1", "number": 1, "size": None, "last_used": None, "used_by": None, "deleted_tonight": False}]}]}
        def cells(opened: list[str]) -> tuple[str, list[str]]:
            html = cast(str, self.run_page(f"slotsHtml({json.dumps(report)}, {KEEP}, {now}, new Set({json.dumps(opened)}))"))
            rows = cast(list[str], re.findall(r"<tr[^>]*>(.*?)</tr>", re.sub(r'<div class="narrow">.*?</div>', "", html)))
            return html, ["|".join(part for part in re.split(r"<[^>]+>", row) if part) for row in rows[1:]]

        # The card starts with one band per repository; an open band lists its slots.
        self.assertEqual(cells([])[1], ["hana|2 of 8 slots · 70 GiB", "&lt;b&gt;|1 slot · —"])
        html, shown = cells(["hana", "<b>"])
        self.assertEqual(shown, ["hana|2 of 8 slots · 70 GiB", "hana-slot1|70 GiB|now|fps|report (builds)",
                                 "hana-slot2|&lt; 256 MiB|9 days ago|deleted tonight", "&lt;b&gt;|1 slot · —",
                                 "x-slot1|—|—|—"])
        self.assertEqual(re.findall(r'aria-expanded="(\w+)" data-group="([^"]*)"', cells(["hana"])[0]),
                         [("true", "hana"), ("false", "&lt;b&gt;")])
        self.assertEqual(re.findall(r'<div class="narrow">(.*?)</div>', html), [
            '<span class="tag run">fps</span><span class="dim">report (builds)</span>', '<span class="tag go">deleted tonight</span>'])
        empty = cast(str, self.run_page(f"slotsHtml({json.dumps({'root': '/s', 'groups': []})}, {KEEP}, {now}, new Set())"))
        self.assertIn("No slots yet.", empty)

    def test_the_sessions_card_names_the_largest_sessions_then_adds_up_the_rest(self) -> None:
        def row(name: str, size: int | None, state: str, held: list[dict[str, object]] | None = None) -> dict[str, object]:
            return {"name": name, "size": size, "state": state, "held": held or []}

        report = {"root": "/session", "sessions": [
            row("fps", 28 * GIB, "running", [{"name": "run", "task": "phase:fps/3", "ended": False},
                                             {"name": "center-shot", "task": "phase:fps/3", "ended": True}]),
            row("lfo", 7 * GIB, "ended"), row("<b>", 0, "unknown"), row("demo", GIB, "running"), row("sdlc", None, "running")]}
        html = cast(str, self.run_page(f"sessionsHtml({json.dumps(report)}, {KEEP}, 3)"))
        rows = cast(list[str], re.findall(r"<tr[^>]*>(.*?)</tr>", html))
        cells = ["|".join(part for part in re.split(r"<[^>]+>", row) if part.strip()) for row in rows[1:]]
        self.assertEqual(cells, ["fps|28 GiB|run|, |center-shot|(unsaved work)|running", "lfo|7.0 GiB|—|ended",
                                 "&lt;b&gt;|&lt; 256 MiB|—|—", "2 more|1.0 GiB"])
        self.assertEqual(re.findall(r'<i style="width:([0-9.]+)%">', html), ["100.0", "25.0"])
        self.assertIn('<span title="phase:fps/3">run</span>', html)
        self.assertIn('<span class="tag go">ended</span>', html)
        empty = cast(str, self.run_page(f"sessionsHtml({json.dumps({'root': '/s', 'sessions': []})}, {KEEP}, 3)"))
        self.assertIn("No session folders yet.", empty)

    def test_a_measure_starts_only_when_the_sizes_are_old_and_no_measure_runs(self) -> None:
        launches: list[str] = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(disks_page, "output_folder", return_value=Path(directory)):
            folder = Path(directory)
            self.assertIs(disks_page.start_measure(1000, lambda: launches.append("missing")), disks_page.MeasureStart.STARTED)
            _ = (folder / disks_page.DATA_FILE).write_text(json.dumps({"measured_at": 900}))
            self.assertIs(disks_page.start_measure(1000, lambda: launches.append("current")), disks_page.MeasureStart.CURRENT)
            with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False):
                self.assertIs(disks_page.start_measure(5000, lambda: launches.append("running")), disks_page.MeasureStart.RUNNING)
            self.assertIs(disks_page.start_measure(5000, lambda: launches.append("old")), disks_page.MeasureStart.STARTED)
            _ = (folder / disks_page.DATA_FILE).write_text("[]")
            self.assertIsNone(disks_page.measured_at(folder))
        self.assertEqual(launches, ["missing", "old"])

    def test_a_slider_step_a_toggle_and_new_data_keep_the_card_so_a_drag_goes_on(self) -> None:
        # The browser ends a drag when the slider it moves leaves the page.
        result = cast(dict[str, object], self.run_drive("""
            const card = new Element();
            data = {keep_bytes: KEEP};
            const drive = {mount: "/build", tree: input.tree, total: 200 * GIB, used: 100 * GIB, free: 100 * GIB, floor: null};
            const update = driveView(card);
            update(drive);
            const slider = card.querySelector("input"), body = () => card.querySelector("tbody");
            const rows = () => body().innerHTML.split("<tr").length - 1;
            const shut = rows();
            body().fire("click", {target: {closest: () => ({dataset: {path: "/build/cargo"}})}});
            const afterToggle = rows();
            slider.value = "1000";
            slider.fire("input");
            update({...drive, free: 90 * GIB});
            result = {writes: card.writes, sameSlider: card.querySelector("input") === slider, output: card.querySelector("output").textContent,
                      rows: [shut, afterToggle, rows()]};
        """))
        self.assertEqual(result, {"writes": 1, "sameSlider": True, "output": "60 GiB", "rows": [4, 9, 7]})

    def test_open_rows_stay_open_across_a_reload_until_their_folder_or_repository_is_gone(self) -> None:
        # The user, 11-16: "disks should start out collapsed and should remember it's collapse points as long as they are
        # still valid between data updates and page refreshes".
        load = """
            const card = new Element();
            data = {keep_bytes: KEEP, drives: [{mount: "/build", tree: input.tree, total: 200 * GIB, used: 100 * GIB, free: 100 * GIB, floor: null}]};
            driveView(card)(data.drives[0]);
            const shown = () => [...card.querySelector("tbody").innerHTML.matchAll(/aria-expanded="true" data-path="([^"]*)"/g)].map(m => m[1]);
        """
        first = cast(dict[str, object], self.run_drive(load + """
            const closed = shown();
            card.querySelector("tbody").fire("click", {target: {closest: () => ({dataset: {path: "/build/cargo"}})}});
            toggleOpen(opened.groups, "hana");
            result = {closed, open: shown(), storage: localStorage.getItem(OPEN_KEY)};
        """))
        self.assertEqual((first["closed"], first["open"]), ([], ["/build/cargo"]))
        second = cast(dict[str, object], self.run_drive(load + """
            const reloaded = shown();
            slotReport = {groups: [{repository: "cargo-liner", slots: []}]};
            data.drives[0].tree = {...input.tree, children: input.tree.children.filter(child => child.name !== "cargo")};
            forgetGone();
            result = {reloaded, storage: JSON.parse(localStorage.getItem(OPEN_KEY))};
        """, storage=cast(str, first["storage"])))
        self.assertEqual(second, {"reloaded": ["/build/cargo"], "storage": {"folders": [], "groups": []}})
        # A joined name, a folder named "|other", and a gone folder on a drive inside a drive with no tree.
        drives = [{"mount": "/", "tree": None},
                  {"mount": "/build", "tree": folder("/build", 2 * GIB, [folder("cargo/project", GIB), folder("|other", GIB)])}]
        paths = ["/build/cargo/project", "/build/|other", "other:/build", "/build/gone", "/home/kept", "other:/"]
        kept = self.run_page(f"[...knownFolders({json.dumps(drives)}, {json.dumps(paths)})]")
        self.assertEqual(kept, ["/build/cargo/project", "/build/|other", "other:/build", "/home/kept", "other:/"])

    def run_drive(self, script: str, storage: str | None = None) -> object:
        """Run driveView on a fake document: each element counts its innerHTML writes and keeps its listeners."""
        html = Path(disks_page.__file__).with_name(disks_page.PAGE_NAME).read_text()
        functions = html[html.index("const GIB ="):html.index("function renderStatus")]
        fake = ("""
            const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
            const KEEP = input.keep;
            class Element {
              constructor() { this.listeners = {}; this.parts = new Map(); this.html = ""; this.writes = 0; this.textContent = ""; this.value = ""; }
              set innerHTML(html) { this.html = html; this.writes += 1; this.parts.clear(); }
              get innerHTML() { return this.html; }
              querySelector(selector) { if (!this.parts.has(selector)) this.parts.set(selector, new Element()); return this.parts.get(selector); }
              querySelectorAll() { return []; }
              contains() { return false; }
              addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
              fire(type, event = {}) { for (const listener of this.listeners[type] || []) listener(event); }
              focus() {}
            }
            const document = {activeElement: null, getElementById: () => ({textContent: JSON.stringify(input.settings)})};
            const CSS = {escape: text => text};
            const saved = new Map(input.storage == null ? [] : [["disks-open", input.storage]]);
            const localStorage = {getItem: key => saved.has(key) ? saved.get(key) : null, setItem: (key, value) => saved.set(key, value)};
            let result = null;
        """)
        javascript = fake + functions + script + "\nprocess.stdout.write(JSON.stringify(result));\n"
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        completed = subprocess.run([executable, "-e", javascript], input=json.dumps({"tree": TREE, "settings": SETTINGS, "keep": KEEP, "storage": storage}),
                                   text=True, capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return cast(object, json.loads(completed.stdout))

    def rows(self, limit: int, opened: list[str]) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self.run_page(
            f"treeRows(data.tree, '/build', {{limit: {limit}, opened: new Set({json.dumps(opened)})}}, {KEEP})"))

    def run_page(self, expression: str) -> object:
        html = Path(disks_page.__file__).with_name(disks_page.PAGE_NAME).read_text()
        functions = html[html.index("const GIB ="):html.index("const settings =")]
        javascript = functions + "\nconst data = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n" + (
            "process.stdout.write(JSON.stringify(" + expression + "));\n")
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        result = subprocess.run([executable, "-e", javascript], input=json.dumps({"tree": TREE, "settings": SETTINGS}),
                                text=True, capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return cast(object, json.loads(result.stdout))


if __name__ == "__main__":
    _ = unittest.main()
