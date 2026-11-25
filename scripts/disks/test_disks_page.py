from __future__ import annotations

import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree
from typing import cast, override
from unittest import mock

from scripts.build_queue import memory_files
from scripts.build_queue.test_memory_hover import RAM_FIXTURE, SWAP_FIXTURE
from scripts.review_links.browser_test_support import assert_status_states
from scripts.disks import disks_page, slots
from scripts.production import staff
from scripts.review_links import staff_names
from scripts.lib import file_lock, test_hang_guard
from scripts.lib.process_group import run_process_group
from scripts.review_links import page_frame, test_page_frame

GIB = 1 << 30
KEEP = 256 << 20
SETTINGS: disks_page.PageSettings = {"slider_min_gib": 1, "slider_default_gib": 10, "cache_seconds": 600, "poll_seconds": 5,
                                     "wait_seconds": 3600, "builds_data": "../builds/memory.json", "builds_max_age_seconds": 30,
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


NOW = 1000 * 86400
SLOTS: slots.SlotReport = {"root": "/build", "machine": "natedev", "unused_days": 2, "groups": [
    {"repository": "hana/report", "maximum": None, "slots": [
        {"name": "1", "key": "hana/report/1", "number": 1, "size": 70 * GIB, "last_used": NOW, "needs_review": False,
         "used_by": {"name": "report integrate", "showrunner": "builds"}},
        {"name": "2", "key": "hana/report/2", "number": 2, "size": 0, "last_used": NOW - 9 * 86400, "used_by": None,
         "needs_review": True}]},
    {"repository": "<b>/other", "maximum": None, "slots": [
        {"name": "1", "key": "<b>/other/1", "number": 1, "size": None, "last_used": None, "used_by": None, "needs_review": False}]}]}


def session(name: str, size: int | None, state: str, held: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {"name": name, "size": size, "state": state, "held": held or []}


SESSIONS: dict[str, object] = {"root": "/session", "sessions": [
    session("fps", 28 * GIB, "running", [{"name": "run", "task": "phase:fps/3", "ended": False},
                                         {"name": "center-shot", "task": "phase:fps/3", "ended": True}]),
    session("lfo", 7 * GIB, "ended"),
    session("demo", GIB, "running", [{"name": "<scratch>", "task": "phase:demo/1", "ended": False}]),
    session("<b>", 0, "unknown"), session("sdlc", None, "running")]}


class DisksPageTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = (configuration / "disks.conf").write_text("# whole: 1..\ncache_seconds=600\n")
        # The page reads staff names, and staff reads the machine list.
        _ = shutil.copy(Path(__file__).parents[2] / "config" / "machines.conf", configuration)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(configuration)})))

    def test_current_builds_keep_staff_parts_in_slot_names(self) -> None:
        member = staff.StaffRow(staff.StaffId("builder"), "unit", "builder", staff.StaffRoot(),
                                current_session=staff.StaffId("builder"))
        setup = "globalThis.window=globalThis;" + staff_names.script(
            staff_names.StaffNames(staff.StaffSnapshot((member,))).registry())[8:-9]
        for waiting in (False, True):
            with self.subTest(waiting=waiting):
                row = {"key": "current", "name": "builder check", "slot_folder": "hana/report/1",
                    "slot_machine": "natedev", "name_parts": [{"kind": "staff", "text": "builder"},
                                                               {"kind": "text", "text": " check"}]}
                result = str(self.run_drive(setup + """
                    Date.now = () => input.now * 1000;
                    slotReport = input.slots;
                    builds = input.builds;
                    result = slotsHtml(slotReport, KEEP, input.now, new Set(['hana/report']), slotStep);
                """, extra={"slots": SLOTS, "now": NOW,
                            "builds": {"read_at": NOW, "walk": [] if waiting else [row],
                                       "waiters": [row] if waiting else []}}))
                self.assertEqual(re.findall(r'<span class="staff-name"[^>]*>([^<]*)</span>', result),
                                 ["builder", "builder"])
                self.assertEqual(result.count('</span> check</span>'), 2)

    def test_slot_work_parts_color_only_the_recorded_staff_reference(self) -> None:
        members = staff.StaffSnapshot(tuple(staff.StaffRow(staff.StaffId(name), "unit", name,
            staff.StaffRoot(), current_session=staff.StaffId(name)) for name in ("builder", "helper")))
        setup = "globalThis.window=globalThis;" + staff_names.script(staff_names.StaffNames(members).registry())[8:-9]
        report = {"root": "/build", "groups": [{"repository": "repo/unit", "maximum": None, "slots": [
            {"name": "1", "key": "repo/unit/1", "number": 1, "size": 0, "last_used": NOW, "needs_review": False,
             "used_by": {"name": "builder helper", "showrunner": "", "name_parts": [
                 {"kind": "staff", "text": "builder"}, {"kind": "text", "text": " helper"}]}}]}]}
        body = str(self.run_page("(()=>{" + setup + "return slotsHtml(" + json.dumps(report)
            + ",0," + str(NOW) + ",new Set(['repo/unit']),()=>null);})()"))
        self.assertEqual(re.findall(r'<span class="staff-name"[^>]*>([^<]*)</span>', body), ["builder", "builder"])
        plain = str(self.run_page("(()=>{const report=" + json.dumps(report)
            + ";delete report.groups[0].slots[0].used_by.name_parts;return slotsHtml(report,0,"
            + str(NOW) + ",new Set(['repo/unit']),()=>null);})()"))
        self.assertEqual(re.sub(r'<[^>]+>', '', body), re.sub(r'<[^>]+>', '', plain))
        current = str(self.run_page("(()=>{" + setup + "return slotsHtml(" + json.dumps(report)
            + ",0," + str(NOW) + ",new Set(['repo/unit']),()=>({name:'Current work'}));})()"))
        self.assertEqual(current.count('>Current work</span>'), 2)
        self.assertNotIn('class="staff-name"', current)

    def test_slot_staff_names_have_role_colors_and_task_names_stay_plain(self) -> None:
        runner = staff.StaffRow(staff.StaffId("builds"), "showrunner", "builds", staff.StaffRoot(), current_session=staff.StaffId("builds"))
        names = staff_names.StaffNames(staff.StaffSnapshot((runner,)))
        setup = "globalThis.window=globalThis;" + staff_names.script(names.registry())[8:-9]
        body = str(self.run_page("(()=>{" + setup + "return slotsHtml(" + json.dumps(SLOTS)
                                + ", 0, " + str(NOW) + ", new Set(['hana/report']), ()=>null);})()"))
        self.assertRegex(body, r'data-color-text="role.showrunner"[^>]*>builds</span>')
        self.assertIn('>report integrate</span>', body)
        self.assertNotRegex(body, r'class="staff-name"[^>]*>report integrate</span>')

    def test_tooltip_keeps_task_and_repository_text_plain_when_it_matches_staff(self) -> None:
        runner = staff.StaffRow(staff.StaffId("builds"), "showrunner", "builds", staff.StaffRoot(),
                                current_session=staff.StaffId("builds"))
        names = staff_names.StaffNames(staff.StaffSnapshot((runner,)))
        page = self.page_html()
        functions = page[page.index("function slotTip("):page.index("function smallerTip(")]
        setup = "globalThis.window=globalThis;" + staff_names.script(names.registry())[8:-9] + functions
        body = str(self.run_page("(()=>{" + setup + "return stepTip.tipRowsHtml(slotTip("
            + "{name:'builds',size:0,last_used:0,used_by:{name:'builds',showrunner:'builds'}},"
            + "{repository:'builds'},0,100,30));})()"))
        grid = ElementTree.fromstring(body)
        self.assertEqual(grid[0].text, "builds")
        self.assertEqual(list(grid[0]), [])
        for label in ("Repository", "Used by"):
            value = next(grid[index + 1] for index, cell in enumerate(grid) if cell.text == label)
            self.assertEqual(value.text, "builds")
            self.assertEqual(list(value), [])
        value = next(grid[index + 1] for index, cell in enumerate(grid) if cell.text == "Showrunner")
        self.assertEqual(value.findtext("span"), "builds")
        session_body = str(self.run_page("(()=>{" + setup
            + "return stepTip.tipRowsHtml(sessionTip({name:'builds',state:'running',size:0,"
            + "held:[{name:'builds',task:'builds'}]},0));})()"))
        session_grid = ElementTree.fromstring(session_body)
        self.assertEqual(session_grid[0].findtext("span"), "builds")
        folder = next(cell for cell in session_grid if cell.tag == "dt" and cell.text == "builds")
        self.assertEqual(list(folder), [])

    def test_publish_writes_the_frame_style_and_settings_into_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = disks_page.publish(Path(directory) / "page", SETTINGS).read_text()
        self.assertIn(page_frame.STYLE, page)
        self.assertIn(page_frame.action_failure("measure-failure"), page)
        self.assertIn(page_frame.ACTION_STATUS_SCRIPT, page)
        button = re.search(r'<button type="button" id="measure-now"[^>]*>.*?</button>', page, re.DOTALL)
        assert button is not None
        self.assertIn('aria-label="Measure now" title="Measure now"', button[0])
        self.assertIn(page_frame.icon_svg("refresh-cw"), button[0])
        self.assertIn('<span class="page-status-state">' + button[0], page)
        self.assertIn('</h1><p class="page-status"', page)
        self.assertIn('data-status-age hidden></span><span data-status-trip hidden></span>', page)
        self.assertIn('data-status-render hidden></span><span data-status-reason hidden></span>', page)
        body = re.sub(r'<script\b[^>]*>.*?</script>', '', page.split('<body>', 1)[1], flags=re.DOTALL)
        self.assertNotIn("__", body)
        settings_text = page.split('type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(settings_text.replace("<\\/", "</")), SETTINGS)
        # The step tooltip rules and code come from the Builds page as they are, so the two pages share them.
        tip = disks_page.shared_step_tip(memory_files.PAGE_SOURCE.read_text())
        self.assertIn("#tip {", tip.style)
        self.assertIn("function tooltipRows(", tip.script)
        self.assertIn(tip.style, page)
        self.assertIn(tip.script, page)
        self.assertNotIn("__STEP_TIP_", page)
        with self.assertRaises(ValueError):
            _ = disks_page.shared_step_tip("no marked parts")

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_page_status_uses_the_real_disks_loader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = disks_page.publish(Path(directory) / "page", SETTINGS).read_text()
        assert_status_states(self, page, SETTINGS["poll_seconds"], {"measured_at": 1, "render_seconds": .01,
                                                                "keep_bytes": KEEP, "drives": [{"mount": "/build", "total": 200 * GIB,
                                                                 "used": 100 * GIB, "free": 100 * GIB, "floor": 30 * GIB, "tree": TREE}]})

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
        result = self.run_page("""(() => {
            const range = sliderRange(data.tree, data.settings);
            const limits = [0, SLIDER_STEPS / 2, SLIDER_STEPS].map(step => fromSlider(step, range));
            const precise = sliderRange(data.tree, {slider_min_gib: 1.04});
            const minimum = fromSlider(0, precise);
            const drive = sliderRange({children: [{size: 27 * GIB}]}, {slider_min_gib: 0.7});
            const sessions = sessionRange({sessions: [{size: 3 * GIB}]}, Math.ceil(0.7 * GIB));
            const uneven = fromSlider(SLIDER_STEPS, {...range, max: range.max + 1});
            const nearTib = 1023.99 * GIB;
            const largeRange = {...range, max: 2048 * GIB};
            const snapped = snapLimit(nearTib, largeRange);
            const boundary = snapLimit(999.99 * GIB, largeRange);
            const threeDigits = snapLimit(99.94 * GIB, largeRange);
            return {
                limits, labels: limits.map(limitLabel),
                positions: [toSlider(range.min, range), toSlider(range.max, range)],
                start: startLimit(range, data.settings),
                minimum: [minimum, limitLabel(minimum)],
                top: [fromSlider(SLIDER_STEPS, drive), sessionLimitAt(SLIDER_STEPS, sessions)],
                uneven: [uneven, limitLabel(uneven)],
                large: [limitLabel(nearTib), snapped, limitLabel(snapped), limitLabel(boundary),
                        limitLabel(999.94 * GIB), threeDigits, limitLabel(threeDigits)],
            };
        })()""")
        self.assertEqual(result, {
            "limits": [GIB, 7.75 * GIB, 60 * GIB],
            "labels": ["1.0 GiB", "7.75 GiB", "60 GiB"],
            "positions": [0, 1000],
            "start": 10 * GIB,
            "minimum": [1.04 * GIB, "1.04 GiB"],
            "top": [27 * GIB, 3 * GIB],
            "uneven": [60 * GIB + 1, "≈60 GiB"],
            "large": ["≈1.0 TiB", 1024 * GIB, "1.0 TiB", "0.98 TiB", "≈0.98 TiB", 99.9 * GIB, "99.9 GiB"],
        })

    def test_the_status_line_uses_the_shared_reader_and_measured_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = disks_page.publish(Path(directory), SETTINGS).read_text()
        self.assertIn('</h1><p class="page-status"', page)
        self.assertIn(page_frame.REFRESH_SCRIPT, page)
        self.assertIn("createPageRefresh(loadDisks,()=>settings.poll_seconds)", page)
        self.assertIn('readAt:data.measured_at,renderSeconds:data.render_seconds', page)
        self.assertIn('showActionFailure(measureFailure, "The measure did not finish.")', page)
        scenario = r"""
const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
let generation=0,fileAt=now/1000-4200,measureState='current',stalled=false,failedReads=false;
const pendingReads=[];
const release=()=>pendingReads.splice(0).forEach(resolve=>resolve({ok:true,json:async()=>payload()}));
function render(){}
function renderCards(){}
const payload=()=>({generation,measured_at:fileAt,render_seconds:.03,drives:[]});
const fetch=async(url)=>{
 if(url==='measure' && measureState==='refused') return {ok:false,status:503,json:async()=>({reason:'Measure refused.'})};
 if(url==='measure') return {ok:true,json:async()=>({state:measureState,target:generation+(measureState==='current' ? 0 : 1)})};
 if(failedReads) return {ok:false,status:502};
 if(stalled) return new Promise(resolve=>pendingReads.push(resolve));
 return {ok:true,json:async()=>payload()};
};
START
(async()=>{
 await pageRefresh.refresh();
 check(line.dataset.state==='Live' && parts.age.textContent.includes('70 min ago'),
  'Old sizes without a measure must stay Live and keep their age.');
 measureState='started';await pageRefresh.refresh();
 check(line.dataset.state==='Paused' && parts.reason.textContent==='Measuring…',
  'A measure with old sizes must show Paused and Measuring….');
 check(parts.reason.textContent!=='The feed has stopped.','A running measure must not report a stopped feed.');
 now+=180000;await pageRefresh.refresh();
 check(line.dataset.state==='Paused' && parts.reason.textContent==='Measuring…',
  'Measuring must remain visible during a three-minute measure.');
 generation++;fileAt=now/1000;measureState='current';await pageRefresh.refresh();pageRefresh.changed();
 await Promise.resolve();await Promise.resolve();
 check(line.dataset.state==='Live' && parts.age.textContent==='0 s ago',
  'A completed measure must return to Live with its new age.');
 measureState='started';await pageRefresh.refresh();
 advanceTime((settings.wait_seconds+1)*1000);pageRefresh.changed();
 await Promise.resolve();await Promise.resolve();
 check(!measureIssue.hidden && measureIssue.textContent==='The measure did not finish.',
  'A measure timeout must keep its failure after the age clock runs.');
 measureState='refused';await pageRefresh.refresh();
 check(line.dataset.state==='Live' && !measureIssue.hidden && measureIssue.textContent==='Measure refused.',
  'A refused automatic measure must show its reason with the old sizes.');
 measureState='current';await pageRefresh.refresh();
 check(line.dataset.state==='Live' && measureIssue.hidden,'A server answer must clear the measure failure.');
 measureState='started';await pageRefresh.refresh();
 failedReads=true;await pageRefresh.refresh();pageRefresh.changed();
 await Promise.resolve();await Promise.resolve();
 check(line.dataset.state==='Failed' && parts.reason.textContent==='HTTP 502',
  'A failed read during a measure must show Failed and HTTP 502.');
 check(measuring!==null,'A failed read before the wait limit must retain the measure start.');
 advanceTime((settings.wait_seconds+1)*1000);await pageRefresh.refresh();pageRefresh.changed();
 await Promise.resolve();await Promise.resolve();
 check(measuring===null,'Failed reads after the wait limit must clear measuring.');
 check(line.dataset.state==='Failed' && parts.reason.textContent==='HTTP 502',
  'Failed reads after the wait limit must show Failed and HTTP 502.');
 check(!measureIssue.hidden && measureIssue.textContent==='The measure did not finish.',
  'The timeout notice must show beside the read failure.');
 failedReads=false;measureState='current';await pageRefresh.refresh();
 check(line.dataset.state==='Live','A good read after failed reads must return to Live.');
 stalled=true;const pendingRead=pageRefresh.refresh();
 now+=(JSON.parse(line.dataset.statusOptions).ageLimit+1)*1000;pageRefresh.changed();
 check(line.dataset.state==='Late','A missing server answer must become Late.');
 release();await pendingRead;
 check(line.dataset.state==='Live','A later server answer must clear Late with the same sizes.');
 stalled=false;measureState='started';await pageRefresh.refresh();
 check(line.dataset.state==='Paused' && parts.reason.textContent==='Measuring…',
  'The next measure must start before its reads stop answering.');
 stalled=true;void pageRefresh.refresh();
 now+=settings.wait_seconds*1000;pageRefresh.changed();
 check(line.dataset.state==='Late' && !parts.age.hidden,
  'A pending read at the wait limit must show Late with the data age.');
 now+=1000;pageRefresh.changed();
 check(line.dataset.state!=='Paused' && parts.reason.textContent!=='Measuring…',
  'A pending read must not retain Paused or Measuring after 3601 seconds.');
 check(line.dataset.state==='Late','A pending read past the wait limit must show Late.');
 if(failures.length) throw new Error(failures.join('\n'));
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        self.assert_status_scenario(scenario)

    def test_a_refused_first_measure_shows_failed_without_data(self) -> None:
        self.assert_status_scenario(r"""
function render(){}
function renderCards(){}
const fetch=async(url)=>url==='measure'
 ? {ok:false,status:503,json:async()=>({reason:'Measure refused.'})}
 : {ok:false,status:404};
START
(async()=>{
 await pageRefresh.refresh();
 if(line.dataset.state!=='Failed' || parts.reason.textContent!=='Measure refused.' || !measureIssue.hidden)
  throw new Error('A refused first measure must show its reason in the Failed status.');
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_the_first_measure_keeps_its_data_age_when_the_next_read_stalls(self) -> None:
        self.assert_status_scenario(r"""
const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
const fileAt=now/1000-4200,pendingReads=[];
let stalled=false;
function render(){}
function renderCards(){}
const fetch=async(url)=>{
 if(url==='measure') return {ok:true,json:async()=>({state:'started',target:1})};
 if(stalled) return new Promise(resolve=>pendingReads.push(resolve));
 return {ok:true,json:async()=>({measured_at:fileAt,render_seconds:.03,drives:[]})};
};
START
(async()=>{
 await pageRefresh.refresh();
 check(line.dataset.state==='Paused' && parts.reason.textContent==='Measuring…',
  'The first measure must show Paused and Measuring.');
 check(!parts.age.hidden && parts.age.textContent==='70 min ago',
  'The first measure must record the old data age.');
 stalled=true;void pageRefresh.refresh();
 check(pendingReads.length===2,'The Disks and Builds reads must have separate pending resolvers.');
 const ageLimit=JSON.parse(line.dataset.statusOptions).ageLimit;
 now+=(Math.max(settings.wait_seconds,ageLimit)+1)*1000;pageRefresh.changed();
 check(line.dataset.state==='Late' && parts.reason.textContent==='The feed has stopped.',
  'The first measure must show Late after the wait and age limits.');
 check(!parts.age.hidden && parts.age.textContent===Math.floor((now/1000-fileAt)/60)+' min ago',
  'The stalled first measure must keep the data age.');
 if(failures.length) throw new Error(failures.join('\n'));
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_the_refresh_button_forces_a_measure_and_recovers_after_completion_timeout_or_failure(self) -> None:
        self.assert_status_scenario(r"""
const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
let generation=0,fileAt=now/1000,failed=false;
const posts=[];
function render(){}
function renderCards(){}
const fetch=async(url,options)=>{
 if(url==='measure'){
  posts.push(options.body);
  if(options.body==='force=1' && failed) return {ok:false,status:503,json:async()=>({reason:'Launch failed.'})};
  return {ok:true,json:async()=>({state:options.body==='force=1' ? 'started' : 'current',target:generation+(options.body==='force=1' ? 1 : 0)})};
 }
 return {ok:true,json:async()=>({generation,measured_at:fileAt,render_seconds:.03,drives:[]})};
};
START
(async()=>{
 await pageRefresh.refresh();
 check(!measureButton.disabled && line.dataset.state==='Live','Current data enables the button.');
 const press=measureButton.press();
 check(measureButton.disabled,'A press disables the button before the request ends.');
 await press;
 check(posts.filter(body=>body==='force=1').length===1,'A press sends force=1 with current data.');
 check(line.dataset.state==='Paused' && parts.reason.textContent==='Measuring…','A press shows Measuring.');
 await measureButton.press();
 check(posts.filter(body=>body==='force=1').length===1,'A disabled button sends no second request.');
 now+=1000;generation++;fileAt=now/1000;await pageRefresh.refresh();
 check(!measureButton.disabled && line.dataset.state==='Live','New data enables the button.');
 failed=true;await measureButton.press();
 check(!measureButton.disabled && !measureIssue.hidden && measureIssue.textContent==='Launch failed.' && line.dataset.state==='Live',
  'A failed press shows its reason and enables the button.');
 failed=false;await measureButton.press();
 advanceTime((settings.wait_seconds+1)*1000);
 check(!measureButton.disabled && !measureIssue.hidden && measureIssue.textContent==='The measure did not finish.',
  'The wait limit enables the button and shows the failure.');
 if(failures.length) throw new Error(failures.join('\n'));
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_a_press_owns_its_request_and_its_deadline_despite_stalled_or_older_reads(self) -> None:
        self.assert_status_scenario(r"""
const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
let generation=0,fileAt=now/1000,stallBuilds=false,stallData=false,missing=false,stallPost=false;
const pendingBuilds=[],pendingData=[],pendingPosts=[],posts=[];
const flush=async()=>{for(let i=0;i<20;i++) await Promise.resolve();};
const payload=()=>({generation,measured_at:fileAt,render_seconds:.03,drives:[]});
const dataReply=()=>({ok:true,json:async()=>payload()});
const postReply=()=>({ok:true,json:async()=>({state:'started',target:generation+1})});
function render(){}
function renderCards(){}
const fetch=async(url,options)=>{
 if(url==='measure'){
  posts.push(options.body);
  if(options.body==='force=1'){
   check(measuring!==null && measureButton.disabled && line.dataset.state==='Paused'
    && parts.reason.textContent==='Measuring…','The press must set its state before the POST.');
   if(stallPost) return new Promise(resolve=>pendingPosts.push(resolve));
   return postReply();
  }
  return {ok:true,json:async()=>({state:'current',target:generation})};
 }
 if(url.startsWith(settings.builds_data)){
  if(stallBuilds) return new Promise(resolve=>pendingBuilds.push(resolve));
  return dataReply();
 }
 if(missing) return {ok:false,status:404};
 if(stallData) return new Promise(resolve=>pendingData.push(resolve));
 return dataReply();
};
const forced=()=>posts.filter(body=>body==='force=1').length;
START
(async()=>{
 await pageRefresh.refresh();
 stallBuilds=true;const older=pageRefresh.refresh();
 check(pendingBuilds.length===1,'The older refresh must wait for Builds.');
 stallPost=true;const press=measureButton.press();
 check(forced()===1 && pendingPosts.length===1,'The press must POST before the Builds read ends.');
 check(measuring.since===now/1000 && measuring.target===null,'The press must record its start before the target arrives.');
 pendingBuilds.shift()(dataReply());await older;
 check(measureButton.disabled && measuring!==null,'The older refresh must not enable the button.');
 await measureButton.press();
 check(forced()===1,'A second press must not send another POST.');
 stallBuilds=false;stallPost=false;pendingPosts.shift()(postReply());await press;
 now+=1000;generation++;fileAt=now/1000;await pageRefresh.refresh();
 check(!measureButton.disabled && measuring===null,'New data must end the measure state.');

 missing=true;await pageRefresh.refresh();
 check(line.dataset.state==='Failed' && parts.reason.textContent==='HTTP 404','The data read must fail after earlier data.');
 const after404=measureButton.press();
 check(forced()===2 && line.dataset.state==='Paused','A missing data file must not stop the forced POST.');
 await after404;
 missing=false;now+=1000;generation++;fileAt=now/1000;await pageRefresh.refresh();
 check(!measureButton.disabled,'New data must recover from the missing file.');

 stallData=true;const timedPress=measureButton.press();await flush();
 check(forced()===3 && pendingData.length===1,'The next read must remain pending after the forced POST.');
 advanceTime(settings.wait_seconds*1000);
 check(measuring===null && !measureButton.disabled,'The timer must end the measure state at the wait limit.');
 check(!measureIssue.hidden && measureIssue.textContent==='The measure did not finish.',
  'The timer must show the failure before the stalled read ends.');
 stallData=false;pendingData.shift()(dataReply());await timedPress;
 check(!measureButton.disabled && !measureIssue.hidden && measureIssue.textContent==='The measure did not finish.',
  'The abandoned read must keep the timeout failure.');
 if(failures.length) throw new Error(failures.join('\n'));
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_polls_keep_a_pending_press_and_only_newer_data_ends_an_answered_press(self) -> None:
        self.assert_status_scenario(r"""
const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
let generation=0,fileAt=now/1000;
const pendingPosts=[],posts=[];
function render(){}
function renderCards(){}
const fetch=async(url,options)=>{
 if(url==='measure'){
  posts.push(options.body);
  if(options.body==='force=1') return new Promise(resolve=>pendingPosts.push(resolve));
  return {ok:true,json:async()=>({state:'current',target:generation})};
 }
 return {ok:true,json:async()=>({generation,measured_at:fileAt,render_seconds:.03,drives:[]})};
};
START
(async()=>{
 await pageRefresh.refresh();
 const failedPress=measureButton.press(),pending=measuring;
 fileAt+=1;await pageRefresh.refresh();
 check(data.measured_at===fileAt,'A poll may show data during a pending POST.');
 check(measuring===pending && measureButton.disabled,'A poll must retain the pending press and disabled button.');
 check(line.dataset.state==='Paused' && parts.reason.textContent==='Measuring…',
  'A poll must retain Measuring during the pending POST.');
 await measureButton.press();
 check(posts.filter(body=>body==='force=1').length===1,'A poll must not permit another press.');
 pendingPosts.shift()({ok:false,status:503,json:async()=>({reason:'Forced launch failed.'})});
 await failedPress;
 check(measuring===null && !measureButton.disabled,'A failed POST must clear the press after a poll.');
 check(!measureIssue.hidden && measureIssue.textContent==='Forced launch failed.',
  'The failed POST must show its reason after a poll.');

 const answeredPress=measureButton.press(),before=fileAt;
 fileAt=before-1;
 pendingPosts.shift()({ok:true,json:async()=>({state:'started',target:1})});
 await answeredPress;
 check(measuring!==null && measureButton.disabled && line.dataset.state==='Paused',
  'Older data must not end an answered press.');
 fileAt=before;await pageRefresh.refresh();
 check(measuring!==null && measureButton.disabled && parts.reason.textContent==='Measuring…',
  'Data at the prior measure time must not end an answered press.');
 generation=1;fileAt=before+1;await pageRefresh.refresh();
 check(measuring===null && !measureButton.disabled && line.dataset.state==='Live',
  'Newer data must end an answered press.');

 generation=1;fileAt=100;await pageRefresh.refresh();
 const polledPress=measureButton.press();
 generation=2;fileAt=150;await pageRefresh.refresh();
 pendingPosts.shift()({ok:true,json:async()=>({state:'started',target:3})});
 await polledPress;
 await pageRefresh.refresh();
 check(measuring!==null && measureButton.disabled && line.dataset.state==='Paused'
  && parts.reason.textContent==='Measuring…',
  'Data below the target must not end the press after the reply.');
 generation=3;fileAt=200;await pageRefresh.refresh();
 check(measuring===null && !measureButton.disabled && line.dataset.state==='Live',
  'Data at the target generation must end the press.');
 if(failures.length) throw new Error(failures.join('\n'));
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_the_target_ends_a_press_at_the_reply_or_data_arrival_without_waiting_for_builds(self) -> None:
        self.assert_status_scenario(r"""
const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
let generation=0,stallBuilds=false,omitGeneration=true;
const pendingPosts=[],pendingBuilds=[];
const flush=async()=>{for(let i=0;i<20;i++) await Promise.resolve();};
const payload=()=>({...(omitGeneration ? {} : {generation}),measured_at:100-generation,render_seconds:.03,drives:[]});
const reply=()=>({ok:true,json:async()=>payload()});
function render(){}
function renderCards(){}
const fetch=async(url,options)=>{
 if(url==='measure'){
  if(options.body==='force=1') return new Promise(resolve=>pendingPosts.push(resolve));
  return {ok:true,json:async()=>({state:'current',target:generation})};
 }
 if(url.startsWith(settings.builds_data) && stallBuilds)
  return new Promise(resolve=>pendingBuilds.push(resolve));
 return reply();
};
START
(async()=>{
 await pageRefresh.refresh();
 const delayed=measureButton.press();
 await pageRefresh.refresh();
 check(measuring!==null && measureButton.disabled,'Legacy data must count as generation zero during a press.');
 omitGeneration=false;generation=1;await pageRefresh.refresh();
 check(measuring!==null && measureButton.disabled,'Target data must wait for the POST target.');
 pendingPosts.shift()({ok:true,json:async()=>({state:'started',target:1})});
 await delayed;
 check(measuring===null && !measureButton.disabled && line.dataset.state==='Live',
  'Target data received before the reply must end the press at the reply.');

 const press=measureButton.press();
 pendingPosts.shift()({ok:true,json:async()=>({state:'running',target:2})});
 await press;
 generation=1;stallBuilds=true;const older=pageRefresh.refresh();await flush();
 check(measuring!==null && measureButton.disabled,'Older data received after the reply must retain the press.');
 generation=3;const newer=pageRefresh.refresh(true);await flush();
 check(pendingBuilds.length===2,'Both Builds reads must remain pending.');
 check(measuring===null && !measureButton.disabled && line.dataset.state==='Live',
  'A later generation must end the press before the Builds read ends.');
 stallBuilds=false;pendingBuilds.splice(0).forEach(resolve=>resolve(reply()));
 await Promise.all([older,newer]);
 if(failures.length) throw new Error(failures.join('\n'));
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_the_wait_timer_reports_failure_without_earlier_data(self) -> None:
        self.assert_status_scenario(r"""
const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
function render(){}
function renderCards(){}
const fetch=async(url)=>url==='measure'
 ? {ok:true,json:async()=>({state:'started',target:1})} : {ok:false,status:404};
START
(async()=>{
 await pageRefresh.refresh();
 check(data===null && measuring!==null && measureButton.disabled,'The first measure must start without earlier data.');
 advanceTime(settings.wait_seconds*1000);
 check(measuring===null && !measureButton.disabled,'The timer must clear the first measure at the wait limit.');
 check(!measureIssue.hidden && measureIssue.textContent==='The measure did not finish.',
  'The timer must report failure without earlier data.');
 if(failures.length) throw new Error(failures.join('\n'));
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def assert_status_scenario(self, scenario: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = disks_page.publish(Path(directory), SETTINGS).read_text()
        match = re.search(r'data-status-options="([^"]+)"', page)
        assert match is not None
        environment = test_page_frame.PageStatusTests.environment(html.unescape(match[1])) + """
const settingsText=SETTINGS;
const measureButton={disabled:false,addEventListener:(key,run)=>{
 measureButton.press=()=>measureButton.disabled ? undefined : run({currentTarget:measureButton});
}};
const measureIssue={hidden:true,textContent:''};
const elements={'measure-now':measureButton,'measure-failure':measureIssue};
document.getElementById=key=>elements[key] || {textContent:JSON.stringify(settingsText)};
const localStorage={getItem:()=>null};
""".replace("SETTINGS", json.dumps(SETTINGS))
        functions = page[page.index("const GIB ="):page.index("const updateSessions =")]
        start = re.search(r'const pageRefresh=createPageRefresh.*?(?=void pageRefresh.refresh\(\);)', page, re.DOTALL)
        assert start is not None
        scenario = scenario.replace("START", start[0])
        node = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        script = environment + functions + page_frame.REFRESH_SCRIPT + page_frame.ACTION_STATUS_SCRIPT + scenario
        done = subprocess.run([node, "--unhandled-rejections=strict", "-"], input=script,
                              capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "PASS"), done.stderr)

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
        report = SLOTS
        def cells(opened: list[str]) -> tuple[str, list[str]]:
            html = cast(str, self.run_page(f"slotsHtml({json.dumps(report)}, {KEEP}, {now}, new Set({json.dumps(opened)}))"))
            rows = cast(list[str], re.findall(r"<tr[^>]*>(.*?)</tr>", re.sub(r'<div class="narrow">.*?</div>', "", html)))
            return html, ["|".join(part for part in re.split(r"<[^>]+>", row) if part) for row in rows[1:]]

        # An open group lists the numbered copies for that repository and unit.
        self.assertEqual(cells([])[1], ["hana/report|2 slots · 70 GiB", "&lt;b&gt;/other|1 slot · —"])
        html, shown = cells(["hana/report", "<b>/other"])
        self.assertEqual(shown, ["hana/report|2 slots · 70 GiB", "1|70 GiB|now|report integrate|builds",
                                 "2|&lt; 256 MiB|9 days ago|review", "&lt;b&gt;/other|1 slot · —",
                                 "1|—|—|—"])
        self.assertEqual(re.findall(r'aria-expanded="(\w+)" data-group="([^"]*)"', cells(["hana/report"])[0]),
                         [("true", "hana/report"), ("false", "&lt;b&gt;/other")])
        self.assertEqual(re.findall(r'<div class="narrow">(.*?)</div>', html), [
            '<span class="tag run">report integrate</span><span class="dim">builds</span>', '<span class="tag go">review</span>'])
        empty = cast(str, self.run_page(f"slotsHtml({json.dumps({'root': '/s', 'groups': []})}, {KEEP}, {now}, new Set())"))
        self.assertIn("No slots yet.", empty)

    def test_the_sessions_card_shows_every_session_and_a_size_filter_joins_the_smaller_ones(self) -> None:
        # The user, 11-16: "sessions should show all sessions and have a filter that limits the minimum size - like disks".
        self.assertRegex(self.page_html(), r'id="cards">\s*<section[^>]+id="slots"[^>]*></section>\s*'
                         + r'<section[^>]+id="sessions"[^>]*></section>')

        def cells(limit: int) -> list[str]:
            html = cast(str, self.run_page(f"sessionRows({json.dumps(SESSIONS)}, {KEEP}, {limit})"))
            rows = cast(list[str], re.findall(r"<tr[^>]*>(.*?)</tr>", html))
            return ["|".join(part for part in re.split(r"<[^>]+>", row) if part.strip()) for row in rows]

        self.assertEqual(cells(0), ["fps|28 GiB|run, center-shot |(unsaved work)|2 folders|running", "lfo|7.0 GiB|—|ended",
                                    "demo|1.0 GiB|&lt;scratch&gt;|running", "&lt;b&gt;|&lt; 256 MiB|—|—", "sdlc|—|—|running"])
        self.assertEqual(cells(5 * GIB), ["fps|28 GiB|run, center-shot |(unsaved work)|2 folders|running",
                                          "lfo|7.0 GiB|—|ended", "3 smaller|1.0 GiB"])
        html = cast(str, self.run_page(f"sessionRows({json.dumps(SESSIONS)}, {KEEP}, 0)"))
        self.assertEqual(re.findall(r'<td class="held">(.*?)</td>', html), [
            '<div class="line"><span class="names">run, center-shot <span class="dim">(unsaved work)</span></span>'
            + '<span class="count">2 folders</span></div>',
            '<span class="dim">—</span>',
            '<div class="line"><span class="names">&lt;scratch&gt;</span></div>',
            '<span class="dim">—</span>',
            '<span class="dim">—</span>',
        ])
        self.assertEqual(re.findall(r'<i style="width:([0-9.]+)%">', html), ["100.0", "25.0", "3.6"])
        self.assertEqual(re.findall(r'data-tip="([^"]*)"', cast(str, self.run_page(f"sessionRows({json.dumps(SESSIONS)}, {KEEP}, {5 * GIB})"))),
                         ["session:fps", "session:lfo", "smaller:sessions"])
        self.assertEqual(self.run_page(f"[sessionRange({json.dumps(SESSIONS)}, {KEEP}), sessionLimitAt(0, {{min: 1, max: 2}})]"),
                         [{"min": KEEP, "max": 28 * GIB}, 0])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_session_rows_and_the_full_tooltip_fit_in_the_browser(self) -> None:
        held: list[dict[str, object]] = [
            {"name": f"scratch/phase-review-{index:02d}/long-folder-name-for-the-layout-check",
             "task": "phase:disksdesign/1", "ended": index % 2 == 0} for index in range(40)]
        measure = json.dumps({"state": "current", "slots": SLOTS, "sessions": {"root": "/session", "sessions": [
            session("report", 28 * GIB, "running", held), session("other", 9 * GIB, "ended")]}}).encode()
        reading = json.dumps({"measured_at": NOW, "render_seconds": 0.01, "keep_bytes": KEEP, "drives": []}).encode()
        probe = """
async function checkSessionLayout() {
  const failures = [];
  const check = (passed, detail) => { if (!passed) failures.push(detail); };
  const bounds = element => element.getBoundingClientRect();
  const close = (first, second) => Math.abs(first - second) < 1;
  try {
    check(matchMedia('(prefers-color-scheme: dark)').matches, 'Firefox uses the dark color scheme.');
    const rows = [...document.querySelectorAll('.sessions tbody tr')];
    check(rows.length === 2, 'The Sessions card shows both rows.');
    const cell = rows[0].querySelector('.held');
    if (innerWidth === 1440) {
      const cards = bounds(document.querySelector('#cards'));
      const slots = bounds(document.querySelector('#slots'));
      const sessions = bounds(document.querySelector('#sessions'));
      check(slots.height > 0 && slots.bottom <= sessions.top, 'Slots is above Sessions.');
      check([slots, sessions].every(card => close(card.left, cards.left) && close(card.right, cards.right)),
            'Both cards use the full width.');
      for (const row of rows) {
        const height = Math.max(...[...row.cells].map(cell => {
          const style = getComputedStyle(cell);
          return parseFloat(style.lineHeight) + parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
            + parseFloat(style.borderBottomWidth);
        }));
        check(bounds(row).height <= height + 1, 'The session row uses one line.');
      }
      const count = rows[0].querySelector('.count');
      const names = rows[0].querySelector('.names');
      check(count.textContent === '40 folders' && close(bounds(count).right, bounds(cell).right),
            'The count is at the right edge of the cell.');
      check(names.scrollWidth > names.clientWidth && getComputedStyle(names).textOverflow === 'ellipsis',
            'The cell shortens the list with an ellipsis.');
    }
    rows[0].scrollIntoView({block: 'center'});
    rows[0].dispatchEvent(new PointerEvent('pointermove', {
      bubbles: true, pointerType: 'mouse', clientX: bounds(cell).left, clientY: bounds(cell).top + 8
    }));
    const tip = document.querySelector('#tip.on');
    const folders = [...tip.querySelectorAll('dt')].slice(2);
    const box = bounds(tip);
    check(innerHeight === 900 && box.top >= 0 && box.bottom <= innerHeight,
          'The tooltip fits in the 900 px window. Bounds: ' + JSON.stringify(box));
    check(box.left >= 0 && box.right <= innerWidth, 'The tooltip fits the window width.');
    check(folders.every((folder, index) => {
      const details = folder.nextElementSibling;
      const state = index % 2 === 0 ? 'unsaved work' : 'held';
      const number = String(index).padStart(2, '0');
      return folder.textContent === 'scratch/phase-review-' + number + '/long-folder-name-for-the-layout-check'
        && details.textContent === state + ' · Task phase:disksdesign/1'
        && folder.scrollWidth <= folder.clientWidth && details.scrollWidth <= details.clientWidth
        && Number(getComputedStyle(details).opacity) < 1;
    }), 'Every displayed folder name and its dim details are visible in full.');
    const summary = tip.querySelector('dl').lastElementChild;
    check(summary.textContent === '+' + (40 - folders.length) + ' more folders'
      && Number(getComputedStyle(summary).opacity) < 1, 'A dim last line gives the number of omitted folders.');
    const textLines = element => {
      const range = document.createRange();
      range.selectNodeContents(element);
      return range.getClientRects().length;
    };
    if (innerWidth === 1440) {
      check(folders.length === 30 && folders.every(folder =>
        textLines(folder) === 1 && textLines(folder.nextElementSibling) === 1
        && close(bounds(folder).top, bounds(folder.nextElementSibling).top)),
        'The tooltip shows the first 30 folders on one line each.');
    } else {
      check(folders.length > 0 && folders.length <= 30 && textLines(folders[0]) > 1,
            'Long folder names wrap at phone width.');
      const root = document.documentElement;
      check(root.scrollWidth <= root.clientWidth, 'The phone page has no horizontal scroll.');
    }
  } catch (error) {
    failures.push(String(error));
  }
  await fetch('/results', {method: 'POST', body: JSON.stringify({width: innerWidth, failures})});
}
"""
        page = self.page_html().replace("void pageRefresh.refresh();",
            "void pageRefresh.refresh().then(checkSessionLayout);" + probe)
        page_bytes = page.replace("</body>", '<img hidden src="/complete"></body>').encode()
        results: list[object] = []
        complete = threading.Event()

        class LayoutProbe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/":
                    body, mime = page_bytes, "text/html; charset=utf-8"
                elif self.path.startswith("/disks.json?"):
                    body, mime = reading, "application/json"
                elif self.path == "/complete":
                    _ = complete.wait(test_hang_guard.TEST_HANG_GUARD_SECS)
                    self.send_error(404)
                    return
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.end_headers()
                _ = self.wfile.write(body)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/results":
                    results.append(cast(object, json.loads(body)))
                    self.send_response(204)
                    self.end_headers()
                    complete.set()
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    _ = self.wfile.write(measure)

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory, ThreadingHTTPServer(("127.0.0.1", 0), LayoutProbe) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            try:
                for width in (1440, 390):
                    with self.subTest(width=width):
                        results.clear()
                        complete.clear()
                        profile = Path(directory) / f"profile-{width}"
                        profile.mkdir()
                        _ = (profile / "user.js").write_text('user_pref("ui.systemUsesDarkTheme", 1);\n')
                        done = run_process_group(
                            ["firefox", "--headless", "--no-remote", "--profile", str(profile),
                             "--window-size", f"{width},900", "--screenshot", str(profile / "disks.png"),
                             f"http://127.0.0.1:{server.server_port}/"],
                            timeout=45)
                        self.assertEqual(done.returncode, 0, done.stderr)
                        self.assertEqual(results, [{"width": width, "failures": []}])
            finally:
                server.shutdown()
                thread.join(test_hang_guard.TEST_HANG_GUARD_SECS)

    def test_the_sessions_filter_keeps_its_card_and_limit_across_new_data(self) -> None:
        result = cast(dict[str, object], self.run_drive("""
            const card = new Element();
            const update = sessionsView(card);
            update(input.sessions, KEEP);
            const slider = card.querySelector("input"), rows = () => card.querySelector("tbody").innerHTML.split("<tr").length - 1;
            const all = [rows(), card.querySelector("output").textContent];
            slider.value = "1000";
            slider.fire("input");
            const top = [rows(), card.querySelector("output").textContent];
            update({...input.sessions, sessions: input.sessions.sessions.slice(1)}, KEEP);
            result = {writes: card.writes, sameSlider: card.querySelector("input") === slider, all, top,
                      after: [rows(), card.querySelector("output").textContent]};
            // Sessions that all fall below the measure's least folder leave no range; the filter shows every session.
            update({...input.sessions, sessions: input.sessions.sessions.slice(3)}, KEEP);
            result.small = [rows(), card.querySelector("output").textContent, slider.disabled];
            update({...input.sessions, sessions: []}, KEEP);
            result.empty = card.innerHTML.includes("No session folders yet.");
        """, extra={"sessions": SESSIONS}))
        self.assertEqual(result, {"writes": 1, "sameSlider": True, "all": [5, "any size"], "top": [2, "28 GiB"],
                                  "after": [2, "7.0 GiB"], "small": [2, "any size", True], "empty": True})

    def test_a_slot_shows_the_builds_tooltip_without_a_title_field(self) -> None:
        row = {"name": "report integrate", "unit": "report", "showrunner": "builds", "started_at": NOW - 60,
               "key": "run", "used": 2.0, "slot_folder": "hana/report/1", "slot_machine": "natedev",
               "pid": 4242, "progress": {"kind": "NoProgress", "reason": "none"}}
        builds = {"read_at": NOW, "walk": [row], "waiters": [], "ram": RAM_FIXTURE, "swap": SWAP_FIXTURE}
        body = cast(str, self.run_drive("""
            Date.now = () => input.now * 1000;
            slotReport = input.slots;
            builds = input.builds;
            result = tipFor("slot:hana/report/1");
        """, extra={"slots": SLOTS, "now": NOW, "builds": builds}))
        self.assertIn("report · integrate", body)

    def test_each_row_and_card_title_has_a_tooltip_and_a_slot_in_use_shows_its_builds_step(self) -> None:
        # The user, 11-16: "every row should contain a tooltip that for slots should be the same tooltip that is shown in
        # the build queue so they share code and data".
        now = NOW
        step = {"name": "report integrate", "unit": "report", "showrunner": "builds", "started_at": now - 600, "used": 2.0,
                "slot_folder": "hana/report/1", "pid": 4242, "progress": {"kind": "NoProgress", "reason": "none"}}
        builds = {"read_at": now, "waiters": [], "swap_by": [], "ram": RAM_FIXTURE, "swap": SWAP_FIXTURE,
                  "walk": [{**step, "key": "mac-run", "slot_machine": "mac"}, {**step, "key": "run", "slot_machine": "natedev"},
                           {**step, "key": "free-run", "slot_folder": "hana/report/2", "slot_machine": "natedev"}]}
        result = cast(dict[str, str], self.run_drive("""
            Date.now = () => input.now * 1000;
            data = {keep_bytes: KEEP};
            slotReport = input.slots;
            sessionReport = input.sessions;
            builds = input.builds;
            sessionLimit = 5 * GIB;
            const text = html => html.replace(/<[/](dt|dd)>/g, "|").replace(/<[^>]+>/g, "");
            result = {run: tipFor("slot:hana/report/1"), shared: stepTip.tooltipHtml("row:run", builds),
                      review: text(tipFor("slot:hana/report/2")), group: text(tipFor("group:hana/report")),
                      slots: text(tipFor("about:slots")),
                      sessions: text(tipFor("about:sessions")), session: text(tipFor("session:fps")),
                      smaller: text(tipFor("smaller:sessions")), gone: tipFor("slot:gone")};
            const usedBy = () => slotsHtml(slotReport, KEEP, input.now, new Set(["hana/report"]), slotStep).match(/<td class="use wide">(.*?)<[/]td>/)[1];
            result.runName = usedBy();
            // A step that waits in a held slot names the slot and gives its tooltip, as the Builds page waiting row does.
            builds.walk = builds.walk.filter(row => row.key !== "run");
            builds.waiters = [{...input.wait, key: "wait", slot_folder: "hana/report/1", slot_machine: "natedev"}];
            result.wait = [tipFor("slot:hana/report/1") === stepTip.tooltipHtml("wait:wait", builds), usedBy()];
            // Builds data older than the configured age gives the Disks rows and the lease's name.
            builds.read_at = input.now - 31;
            result.stale = text(tipFor("slot:hana/report/1"));
            result.staleName = usedBy();
            // Builds data with no read time is not current.
            delete builds.read_at;
            result.noTime = usedBy();
        """, extra={"slots": SLOTS, "sessions": SESSIONS, "builds": builds,
                    "now": now, "wait": {"name": "report test named", "unit": "report", "showrunner": "builds", "pid": 4343,
                                         "since": now - 60, "need": 3.0, "reason": "its turn", "fits": False, "why": "turn",
                                         "place": 1}}))
        self.assertEqual(result["run"], result["shared"])
        # The builds showrunner, 11-16: "map the Disks "Used by" lease words through the same build_steps names that the
        # Builds page uses, so one step has one name on both pages."
        self.assertEqual(result["runName"], '<span class="tag run">report integrate</span><span class="dim">builds</span>')
        self.assertEqual(result["wait"], [True, '<span class="tag run">report test named</span><span class="dim">builds</span>'])
        self.assertIn("report · integrate", result["run"])
        self.assertEqual(result["review"], "|".join([
            "2", "review", "Repository", "hana/report", "Size", "&lt; 256 MiB", "Last used", "9 days ago",
            "The nightly sweep lists this folder after 2 days without use.", "",
        ]))
        self.assertEqual(result["group"], "hana/report|2 slots|Size|70 GiB|In use|1 of 2|Limit|not set|")
        self.assertIn("The nightly sweep lists a free copy for review after 2 days without use.", result["slots"])
        self.assertIn("A session folder holds the temporary files of one Claude session", result["sessions"])
        self.assertEqual(result["session"], "fps|running|Size|28 GiB|run|held · Task phase:fps/3|center-shot|unsaved work · Task phase:fps/3|")
        self.assertEqual(result["smaller"], "3 smaller sessions|1.0 GiB|Each is below 5.0 GiB. Move the size filter to the left to show them.|")
        self.assertEqual(result["gone"], "")
        self.assertTrue(result["stale"].startswith("1|in use|Repository|hana/report|"), result["stale"])
        self.assertEqual(result["staleName"], result["runName"])
        self.assertEqual(result["noTime"], result["runName"])

    def test_a_measure_starts_only_when_the_sizes_are_old_and_no_measure_runs(self) -> None:
        launches: list[str] = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(disks_page, "output_folder", return_value=Path(directory)):
            folder = Path(directory)
            self.assertIs(disks_page.start_measure(1000, lambda _generation, _lock: launches.append("missing")).state, disks_page.MeasureStart.STARTED)
            _ = (folder / disks_page.DATA_FILE).write_text(json.dumps({"measured_at": 900}))
            self.assertIs(disks_page.start_measure(1000, lambda _generation, _lock: launches.append("current")).state, disks_page.MeasureStart.CURRENT)
            _ = (folder / disks_page.ACTIVE_FILE).write_text("1\n")
            with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False):
                self.assertIs(disks_page.start_measure(5000, lambda _generation, _lock: launches.append("running")).state, disks_page.MeasureStart.RUNNING)
            self.assertIs(disks_page.start_measure(5000, lambda _generation, _lock: launches.append("old")).state, disks_page.MeasureStart.STARTED)
            _ = (folder / disks_page.DATA_FILE).write_text("[]")
            self.assertIsNone(disks_page.measured_at(folder))
        self.assertEqual(launches, ["missing", "old"])

    def test_a_forced_measure_starts_with_current_data_but_keeps_the_running_lock(self) -> None:
        launches: list[str] = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(disks_page, "output_folder", return_value=Path(directory)):
            folder = Path(directory)
            _ = (folder / disks_page.DATA_FILE).write_text(json.dumps({"measured_at": 900}))
            self.assertIs(disks_page.start_measure(1000, lambda _generation, _lock: launches.append("forced"), force=True).state,
                          disks_page.MeasureStart.STARTED)
            _ = (folder / disks_page.ACTIVE_FILE).write_text("1\n")
            with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False):
                self.assertIs(disks_page.start_measure(1000, lambda _generation, _lock: launches.append("running"), force=True).state,
                              disks_page.MeasureStart.RUNNING)
        self.assertEqual(launches, ["forced"])

    def test_a_measure_request_selects_the_published_or_active_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(disks_page, "output_folder", return_value=Path(directory)):
            folder = Path(directory)
            published = folder / disks_page.DATA_FILE
            _ = published.write_text(json.dumps({"measured_at": 900, "generation": 7}))
            launch = mock.Mock()
            self.assertEqual(disks_page.start_measure(1000, launch),
                             disks_page.MeasureRequest(disks_page.MeasureStart.CURRENT, 7))
            with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False):
                _ = (folder / disks_page.ACTIVE_FILE).write_text("8\n")
                self.assertEqual(disks_page.start_measure(1000, launch, force=True),
                                 disks_page.MeasureRequest(disks_page.MeasureStart.RUNNING, 8))
            launch.assert_not_called()

    def test_a_started_request_passes_its_lock_and_target_to_the_child(self) -> None:
        child_code = """
import json
import subprocess
import sys
from pathlib import Path
from scripts.disks import measure
from scripts.disks.test_measure import settings, no_floors
from scripts.lib import file_lock
folder, generation, descriptor = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
assert sys.stdin.read(1) == 'x'
reservation = measure.MeasureReservation(generation, file_lock.ExclusiveLock(descriptor))
raise SystemExit(measure.measure(settings(folder, str(folder)), floors=no_floors,
    runner=lambda argv, timeout: subprocess.CompletedProcess(argv, 0, json.dumps({"name": str(folder), "size": "1B", "children": []}), ''),
    reservation=reservation))
"""
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(disks_page, "output_folder", return_value=Path(directory)):
            folder = Path(directory)
            _ = (folder / disks_page.DATA_FILE).write_text('{"generation":7}')
            children: list[subprocess.Popen[str]] = []

            def launch(generation: int, lock: file_lock.ExclusiveLock) -> None:
                self.assertEqual(generation, 8)
                self.assertEqual((folder / disks_page.ACTIVE_FILE).read_text(), "8\n")
                with file_lock.exclusive(folder / disks_page.COORD_FILE, wait=False) as coordination:
                    self.assertIs(coordination, file_lock.LockResult.BUSY)
                children.append(subprocess.Popen(
                    [sys.executable, "-c", child_code, str(folder), str(generation), str(lock.descriptor)],
                    pass_fds=(lock.descriptor,), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True))

            try:
                first = disks_page.start_measure(1000, launch, force=True)
                self.assertEqual(first, disks_page.MeasureRequest(disks_page.MeasureStart.STARTED, 8))
                self.assertEqual(json.loads((folder / disks_page.DATA_FILE).read_text()), {"generation": 7})
                with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as held:
                    self.assertIs(held, file_lock.LockResult.BUSY)
                second = disks_page.start_measure(1000, launch, force=True)
                self.assertEqual(second, disks_page.MeasureRequest(disks_page.MeasureStart.RUNNING, 8))
                self.assertEqual(len(children), 1)
            finally:
                for child in children:
                    output, error = child.communicate("x", timeout=10)
                    self.assertEqual(child.returncode, 0, output + error)
            self.assertEqual(json.loads((folder / disks_page.DATA_FILE).read_text())["generation"], 8)
            self.assertFalse((folder / disks_page.ACTIVE_FILE).exists())
            with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as released:
                self.assertIs(released, file_lock.LockResult.ACQUIRED)

    def test_a_stale_active_file_does_not_change_the_next_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(disks_page, "output_folder", return_value=Path(directory)):
            folder = Path(directory)
            _ = (folder / disks_page.DATA_FILE).write_text('{"generation":7}')
            _ = (folder / disks_page.ACTIVE_FILE).write_text("99\n")

            def launch(generation: int, lock: file_lock.ExclusiveLock) -> None:
                self.assertEqual(generation, 8)
                self.assertEqual((folder / disks_page.ACTIVE_FILE).read_text(), "8\n")
                with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as held:
                    self.assertIs(held, file_lock.LockResult.BUSY)
                self.assertGreaterEqual(lock.descriptor, 0)

            self.assertEqual(disks_page.start_measure(1000, launch, force=True),
                             disks_page.MeasureRequest(disks_page.MeasureStart.STARTED, 8))

    def test_a_reaper_failure_keeps_the_child_lock_and_target(self) -> None:
        child_code = """
import sys
from pathlib import Path
from scripts.disks import disks_page, slots
from scripts.lib import file_lock
folder, descriptor = Path(sys.argv[1]), int(sys.argv[2])
assert sys.stdin.buffer.read(1) == b'x'
with file_lock.exclusive(folder / disks_page.COORD_FILE):
    (folder / disks_page.ACTIVE_FILE).unlink()
    file_lock.ExclusiveLock(descriptor).release()
"""
        for failure in ("construct", "start"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, \
                    mock.patch.object(disks_page, "output_folder", return_value=Path(directory)):
                folder = Path(directory)
                _ = (folder / disks_page.DATA_FILE).write_text('{"generation":7}')
                children: list[subprocess.Popen[bytes]] = []

                def launch(_generation: int, lock: file_lock.ExclusiveLock) -> None:
                    child = subprocess.Popen(
                        [sys.executable, "-c", child_code, str(folder), str(lock.descriptor)],
                        pass_fds=(lock.descriptor,), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE)
                    children.append(child)
                    disks_page.reap_measure(child)

                start_reaper = mock.Mock(side_effect=RuntimeError("cannot start reaper"))
                reaper = mock.Mock(start=start_reaper)
                make_reaper = mock.Mock(return_value=reaper)
                if failure == "construct":
                    make_reaper.side_effect = RuntimeError("cannot construct reaper")
                errors = io.StringIO()
                try:
                    with mock.patch("scripts.disks.disks_page.threading.Thread", make_reaper), redirect_stderr(errors):
                        first = disks_page.start_measure(1000, launch, force=True)
                    self.assertEqual(first, disks_page.MeasureRequest(disks_page.MeasureStart.STARTED, 8))
                    self.assertIn(f"review-links disks reaper: cannot {failure} reaper", errors.getvalue())
                    self.assertEqual((folder / disks_page.ACTIVE_FILE).read_text(), "8\n")
                    with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as held:
                        self.assertIs(held, file_lock.LockResult.BUSY)
                    second = disks_page.start_measure(1000, launch, force=True)
                    self.assertEqual(second, disks_page.MeasureRequest(disks_page.MeasureStart.RUNNING, 8))
                    self.assertEqual(len(children), 1)
                finally:
                    for child in children:
                        output, error = child.communicate(b"x", timeout=10)
                        self.assertEqual(child.returncode, 0, (output + error).decode())
                self.assertFalse((folder / disks_page.ACTIVE_FILE).exists())
                with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as released:
                    self.assertIs(released, file_lock.LockResult.ACQUIRED)

    def test_a_launch_failure_keeps_a_stale_active_file_and_releases_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(disks_page, "output_folder", return_value=Path(directory)):
            folder = Path(directory)
            with mock.patch.object(disks_page, "REPOSITORY", folder / "missing"), self.assertRaises(FileNotFoundError):
                _ = disks_page.start_measure(1000, force=True)
            self.assertEqual((folder / disks_page.ACTIVE_FILE).read_text(), "1\n")
            self.assertFalse((folder / disks_page.DATA_FILE).exists())
            with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as held:
                self.assertIs(held, file_lock.LockResult.ACQUIRED)
            launch = mock.Mock()
            self.assertEqual(disks_page.start_measure(1000, launch, force=True),
                             disks_page.MeasureRequest(disks_page.MeasureStart.STARTED, 1))
            launch.assert_called_once_with(1, mock.ANY)

    def test_the_launcher_passes_the_reserved_descriptor_and_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = file_lock.take_exclusive(Path(directory) / disks_page.LOCK_FILE)
            assert isinstance(lock, file_lock.ExclusiveLock)
            collect_exit = mock.Mock()
            start_collector = mock.Mock()
            process = mock.Mock(wait=collect_exit)
            collector = mock.Mock(start=start_collector)
            try:
                with mock.patch("scripts.disks.disks_page.subprocess.Popen", return_value=process) as launch, \
                        mock.patch("scripts.disks.disks_page.threading.Thread", return_value=collector) as thread:
                    disks_page.launch_measure(8, lock)
                    launch.assert_called_once_with(
                        [sys.executable, "-m", "scripts.disks.measure", "--generation", "8",
                         "--lock-fd", str(lock.descriptor)], cwd=disks_page.REPOSITORY, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                        pass_fds=(lock.descriptor,))
                    thread.assert_called_once_with(target=collect_exit, name="disks-measure", daemon=True)
                    start_collector.assert_called_once_with()
            finally:
                lock.release()

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

    def run_drive(self, script: str, storage: str | None = None, extra: dict[str, object] | None = None) -> object:
        """Run the page code on a fake document: each element counts its innerHTML writes and keeps its listeners."""
        html = self.page_html()
        functions = html[html.index("const GIB ="):html.index("const updateSessions =")]
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
        completed = subprocess.run([executable, "-e", javascript], input=json.dumps({"tree": TREE, "settings": SETTINGS, "keep": KEEP, "storage": storage,
                                                                                **(extra or {})}),
                                   text=True, capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return cast(object, json.loads(completed.stdout))

    def rows(self, limit: int, opened: list[str]) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self.run_page(
            f"treeRows(data.tree, '/build', {{limit: {limit}, opened: new Set({json.dumps(opened)})}}, {KEEP})"))

    def page_html(self) -> str:
        """The published page, with the shared step tooltip code in place."""
        with tempfile.TemporaryDirectory() as directory:
            return disks_page.publish(Path(directory), SETTINGS).read_text()

    def run_page(self, expression: str) -> object:
        html = self.page_html()
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
