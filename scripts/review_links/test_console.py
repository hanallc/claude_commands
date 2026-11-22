"""Test the console with private configuration and pages."""
from __future__ import annotations

from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import replace
from datetime import datetime
import subprocess
import threading
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from typing import Literal, cast, override
from unittest.mock import patch

from scripts.aws_target import aws_page, machines_page
from scripts.build_queue import memory_feed
from scripts.disks import disks_page
from scripts.lib import conf
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import investigation, staff
from scripts.production.change_routing import UnavailableProduction
from scripts.review_links import (
    change_browser, change_page, console, for_you, investigations_page, page_frame, record_table, review_link,
)


class ConsoleMarkup(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.pages: list[str] = []
        self.sources: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("class") == "nav-entry" and (identity := values.get("data-page")):
            self.pages.append(identity)
        if tag in ("script", "link", "img", "iframe"):
            self.sources.extend(value for key in ("src", "href") if (value := values.get(key)))


class ConsoleTests(unittest.TestCase):
    root: Path = Path()
    configuration: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="console-test-")))
        self.configuration = self.root / "config"
        self.configuration.mkdir()
        for stem in ("console", "review_links", "change_review"):
            _ = shutil.copyfile(Path(__file__).resolve().parents[2] / f"config/{stem}.conf",
                                self.configuration / f"{stem}.conf")
        _ = write_config_fixture(self.configuration / "console.conf",
            "page.accounts.label=Accounts\npage.accounts.title=Accounts\npage.accounts.icon=users\n"
            + "page.accounts.url=python:scripts.review_links.accounts_page:page_url\n"
            + "page.aws.label=AWS\npage.aws.title=AWS\npage.aws.icon=cloud\n"
            + "page.aws.url=python:scripts.aws_target.aws_page:page_url\n"
            + "page.builds.icon=hammer\npage.builds.label=Builds\npage.builds.title=Builds\n"
            + "page.builds.url=python:scripts.build_queue.memory_feed:page_url\n"
            + "page.disks.label=Disks\npage.disks.title=Disks\npage.disks.icon=hard-drive\n"
            + "page.disks.url=python:scripts.disks.disks_page:page_url\n"
            + "page.images.label=Images\npage.images.title=Images\npage.images.icon=image\n"
            + "page.images.url=python:scripts.review_links.images:page_url\n"
            + "page.list.label=List\npage.list.icon=list-checks\npage.list.title=The List\n"
            + "page.list.url=python:scripts.review_links.list_page:page_url\n"
            + "page.review.label=Review\npage.review.icon=inbox\npage.review.title=Review\n"
            + "page.review.url=python:scripts.review_links.review_page:page_url\n"
            + "rebuilds.url=python:scripts.review_links.change_page:rebuilds_url\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(self.configuration), "REVIEW_LINKS_ROOT": str(self.root / "snapshots"),
            "REVIEW_LINKS_HOST": "127.0.0.1", "STAFF_FILE": str(self.root / "staff.json"),
            "SHOWRUNNER_STATE_DIR": str(self.root / "state")})))

    def test_aws_machines_stays_next_to_aws_when_the_page_is_unavailable(self) -> None:
        self.assertIn("server", console.ICON_NAMES)
        self.assertTrue((page_frame.ICONS / "server.svg").is_file())
        self.assertIn('class="lucide lucide-server"', console.icon_svg("server"))
        with patch.object(machines_page, "output_folder", return_value=self.root / "snapshots/aws"):
            pages = console.read_pages()
        aws = next(index for index, page in enumerate(pages) if page.identity == "aws")
        self.assertEqual(pages[aws + 1].identity, "aws_machines")
        self.assertIsInstance(pages[aws + 1].destination, console.UnavailablePage)
        source = console.render(pages, console.UnavailablePage()).decode()
        self.assertIn('data-page="aws_machines" data-url="" data-title="AWS Machines" aria-label="AWS Machines — Not available"', source)

    def test_publication_reuses_the_changes_link_and_inlines_only_local_icons(self) -> None:
        output = self.root / "snapshots/aws"
        output.mkdir(parents=True)
        _ = (output / machines_page.PAGE_NAME).write_text("<title>AWS Machines</title>", encoding="utf-8")
        with (patch.object(memory_feed, "page_url", return_value="/builds/memory_live.html", create=True),
              patch.object(aws_page, "page_url", return_value="/aws/aws_prices.html"),
              patch.object(machines_page, "output_folder", return_value=output),
              patch.object(disks_page, "page_url", return_value="/disks/disks.html")):
            source = console.render(console.read_pages(), console.read_rebuilds()).decode()
        self.assertEqual(change_page.console_url(), f"http://127.0.0.1:{conf.read('review_links').whole('server_port')}/console/")
        folder = self.root / "snapshots" / change_page.CONSOLE_FOLDER
        self.assertTrue((folder / change_page.BROWSER_MANIFEST).is_file())
        markup = ConsoleMarkup()
        markup.feed(source)
        self.assertEqual(markup.pages, ["accounts", "aws", "aws_machines", "builds", "disks", "images", "list", "review"])
        self.assertEqual(markup.sources, [])
        # Each page has its own icon and a hidden new mark; the menu button has one more mark.
        self.assertEqual(source.count('aria-hidden="true" focusable="false"'), 25)
        self.assertEqual(source.count('class="lucide lucide-sparkle"'), 9)
        self.assertEqual(source.count('<span class="new-mark" hidden>'), 8)
        self.assertIn('<span class="new-mark" id="menu-new" hidden>', source)
        self.assertIn('data-news="/console/news"', source)
        self.assertIn("showNews();\n    loadNews();\n    if (navigate && phone.matches) collapse(true);", source)
        self.assertIn("const value = fresh.has(entry.dataset.page) && !entry.hasAttribute('aria-current');", source)
        self.assertIn("function refresh() { loadRebuilds(); loadNews(); actions.refresh(); }", source)
        base = f"/{change_page.CONSOLE_FOLDER}"
        changes = f"{base}/review.html"
        self.assertIn(f'data-url="{changes}" data-title="Review"', source)
        self.assertNotIn('data-page="questions"', source)
        self.assertNotIn('data-page="investigations"', source)
        self.assertIn('data-url="/console/list.html" data-title="The List"', source)
        self.assertIn('data-page="review"', source)
        self.assertIn(f'data-url="{base}/{change_page.REBUILDS_NAME}" data-title="Rebuilds needed" hidden>', source)
        retry = conf.read("review_links").number("frame_retry_seconds")
        self.assertIn(f'data-frame-retry-seconds="{retry}"', source)
        self.assertIn("try { loaded = frame.contentWindow.location.origin === location.origin; } catch (_) {}", source)
        self.assertIn('<a class="rebuild-link" id="rebuild-link" href="?page=rebuilds" data-page="rebuilds"', source)
        self.assertIn("const pages = [...entries, rebuildLink];", source)
        self.assertIn("if (shown) shown.replaceWith(shown.ownerDocument.importNode(parsed, true));", source)
        self.assertIn("open: current === rebuildLink ? '' : current.dataset.page", source)
        self.assertNotIn("<iframe", source)
        self.assertIn(".frame-area iframe[hidden]{display:block!important;visibility:hidden;pointer-events:none}", source)

        self.assertIn("const placeKey = identity => 'at.' + identity;", source)
        self.assertIn("target.pathname === new URL(base, location.href).pathname", source)
        self.assertIn("event.data !== 'console-place'", source)
        self.assertIn("if (opened) frame.contentWindow.postMessage('console-shown', location.origin);", source)
        self.assertIn("frame.contentWindow.postMessage('console-space', location.origin);", source)
        self.assertIn("if (event.target.closest?.('input, textarea, select, button, summary, [contenteditable]')) return;", source)
        for removed in ("<dialog", "showModal", "lucide-x"):
            self.assertNotIn(removed, source)
        self.assertIn('class="lucide lucide-refresh-cw"', source)
        self.assertIn("/builds/memory_live.html", source)
        self.assertIn("/aws/aws_prices.html", source)
        self.assertIn('data-url="/aws/machines.html" data-title="AWS Machines"', source)
        self.assertIn('class="lucide lucide-cloud"', source)
        self.assertNotIn("Workspace", source)
        self.assertNotIn("sidebar-footer", source)
        self.assertEqual(source.count("<h1>Hana console</h1>"), 2)
        self.assertIn("<title>Hana console</title>", source)
        self.assertIn("document.title = 'Hana console · ' + title", source)
        self.assertIn('class="mobile-header"', source)
        self.assertIn('id="mobile-nav-toggle"', source)
        self.assertIn('aria-label="Open navigation"', source)
        self.assertIn('class="lucide lucide-menu"', source)
        self.assertIn('.sidebar .brand,.collapsed .sidebar{display:none}', source)
        self.assertEqual(source.count(f'@media {console.PHONE_QUERY}'), 2)
        self.assertIn(f"matchMedia('{console.PHONE_QUERY}')", source)
        self.assertNotIn('page-header', source)
        self.assertIn('.workspace{flex:1;width:100%}', source)
        self.assertIn("mobileToggle.setAttribute('aria-expanded', String(!value))", source)

    def test_repository_config_names_staff_for_every_console_page(self) -> None:
        path = Path(__file__).resolve().parents[2] / "config" / "console.conf"
        settings = conf.read(path).values()
        pages = {key.split(".")[1] for key in settings if key.startswith("page.") and key.endswith(".url")}
        self.assertEqual(pages | {"rebuilds"}, page_frame.OWNER_PAGES)
        for page in page_frame.OWNER_PAGES:
            with self.subTest(page=page):
                self.assertRegex(settings[f"page.{page}.owner"], r"^[a-f0-9-]{36}$")
        self.assertEqual(settings["owner.default"], settings["page.review.owner"])

    def test_builds_url_resolves_from_its_public_function(self) -> None:
        folder = self.root / "snapshots/builds"
        folder.mkdir(parents=True)
        page = folder / "memory_live.html"
        _ = page.write_text("<!doctype html><title>Builds</title><h1>Builds</h1>")
        _ = (self.configuration / "memory_feed.conf").write_text(f"output_folder={folder}\n")
        pages = console.read_pages()
        self.assertEqual(pages[3].destination, console.AvailablePage("/builds/memory_live.html"))
        self.assertIn('data-url="/builds/memory_live.html" data-title="Builds"', console.render(pages, console.UnavailablePage()).decode())
        self.assertEqual(page.read_text(), "<!doctype html><title>Builds</title><h1>Builds</h1>")

    def test_config_order_and_unavailable_pages_remain_visible(self) -> None:
        source = (self.configuration / "console.conf").read_text()
        blocks = source.split("\n\n")
        _ = (self.configuration / "console.conf").write_text("\n\n".join(reversed(blocks)))
        with patch.object(memory_feed, "page_url", side_effect=FileNotFoundError("fixture page absent"), create=True):
            pages = console.read_pages()
        self.assertEqual([page.identity for page in pages], ["review", "list", "images", "disks", "builds", "aws_machines", "aws", "accounts"])
        self.assertIsInstance(pages[2].destination, console.AvailablePage)
        self.assertIsInstance(pages[4].destination, console.UnavailablePage)
        markup = ConsoleMarkup()
        source = console.render(pages, console.UnavailablePage()).decode()
        self.assertEqual(source.count(page_frame.BUTTON_STATES), 1)
        markup.feed(source)
        self.assertEqual(markup.pages, ["review", "list", "images", "disks", "builds", "aws_machines", "aws", "accounts"])
        self.assertIn('data-page="rebuilds"\ndata-url="" data-title', source)
        self.assertIn('aria-label="Builds — Not available"', source)
        self.assertNotIn("fixture page absent", source)

    def test_a_missing_url_function_keeps_the_entry_unavailable(self) -> None:
        configuration = self.configuration / "console.conf"
        source = configuration.read_text().replace(
            "python:scripts.build_queue.memory_feed:page_url",
            "python:scripts.build_queue.memory_feed:fixture_page_url_absent")
        _ = configuration.write_text(source)
        pages = console.read_pages()
        self.assertEqual([page.identity for page in pages], ["accounts", "aws", "aws_machines", "builds", "disks", "images", "list", "review"])
        self.assertIsInstance(pages[3].destination, console.UnavailablePage)
        self.assertIsInstance(pages[-1].destination, console.AvailablePage)
        self.assertIn('aria-label="Builds — Not available"', console.render(pages, console.read_rebuilds()).decode())

    def test_url_functions_cannot_put_an_external_page_in_the_frame(self) -> None:
        port = 8123
        _ = write_config_fixture(self.configuration / "review_links.conf", f"server_port={port}\n")
        for path in ("https://example.invalid/page", "//example.invalid/page", "/../outside", "/%2e%2e/outside",
                     "http://127.0.0.1:1/page", f"https://127.0.0.1:{port}/page"):
            with self.subTest(path=path), patch.object(memory_feed, "page_url", return_value=path, create=True):
                pages = console.read_pages()
                self.assertIsInstance(pages[3].destination, console.UnavailablePage)

    def news_pages(self, changes: set[str], questions: set[str]) -> tuple[console.ConsolePage, ...]:
        return (console.ConsolePage("changes", "Changes", "list-checks", "Changes", console.AvailablePage("/console/review.html"),
                    lambda _request: frozenset(changes)),
                console.ConsolePage("questions", "Questions", "list-checks", "Questions", console.AvailablePage("/console/review.html"),
                    lambda _request: frozenset(questions)))

    def test_ready_investigation_answers_mark_the_page_until_a_visit(self) -> None:
        pages = (console.ConsolePage("investigations", "Investigations", "search", "Investigations",
                    console.AvailablePage("/console/review.html"), investigations_page.waiting_investigations),)
        root = self.root / "snapshots"
        owner = staff.StaffId("orphan-owner")
        self.assertEqual(console.news(root, pages, "changes"), ())
        item = investigation.file(owner, "Find the cause", "Why?", waiting_why='The owner must complete the investigation.')
        self.assertEqual(console.news(root, pages, "changes"), ())
        ready = investigation.answer(owner, item.id, "Found it.")
        self.assertEqual(investigations_page.waiting_investigations(console.NewsRequest(root)),
                         frozenset({f"{owner}/{item.id}/{ready.answered}"}))
        self.assertEqual(console.news(root, pages, "changes"), ("investigations",))
        self.assertEqual(console.news(root, pages, "investigations"), ())
        self.assertEqual(console.news(root, pages, "changes"), ())
        _ = investigation.mark_read(owner, item.id)
        self.assertEqual(investigations_page.waiting_investigations(console.NewsRequest(root)), frozenset())

    def test_a_page_is_new_when_an_item_arrives_after_its_last_open(self) -> None:
        root = self.root / "snapshots"
        # The first visit records every page's items, so nothing shows as new.
        self.assertEqual(console.news(root, self.news_pages({"a/C1"}, {"q1"}), "changes"), ())
        self.assertEqual(console.news(root, self.news_pages({"a/C1"}, {"q1", "q2"}), "changes"), ("questions",))
        # A new change on the open page is seen at once.
        self.assertEqual(console.news(root, self.news_pages({"a/C1", "a/C2"}, {"q1", "q2"}), "changes"), ("questions",))
        self.assertEqual(console.news(root, self.news_pages({"a/C1", "a/C2"}, {"q1", "q2"}), "questions"), ())
        # A question answered on its page and filed again is new again.
        self.assertEqual(console.news(root, self.news_pages({"a/C1", "a/C2"}, {"q2"}), "questions"), ())
        self.assertEqual(console.news(root, self.news_pages({"a/C2"}, {"q1", "q2"}), "changes"), ("questions",))
        self.assertTrue(review_link.private_name(console.SEEN_NAME))

    def test_a_failing_or_unavailable_page_shows_no_mark_and_keeps_its_record(self) -> None:
        root = self.root / "snapshots"
        _ = console.news(root, self.news_pages({"a/C1"}, {"q1"}), "changes")
        with (patch.object(change_browser, "waiting_changes", return_value=frozenset({"a/C1"})),
              patch.object(change_browser, "waiting_questions", side_effect=OSError("fixture list unreadable"))):
            pages = tuple(console.ConsolePage(page.identity,page.label,page.icon,page.title,page.destination,
                change_browser.waiting_questions if page.identity == "questions" else page.items)
                for page in self.news_pages({"a/C1"}, {"q1"}))
        self.assertEqual(console.news(root, pages, "questions"), ())
        self.assertEqual(console.news(root, self.news_pages({"a/C1"}, {"q1", "q2"}), "changes"), ("questions",))
        unavailable = tuple(console.ConsolePage(page.identity, page.label, page.icon, page.title, console.UnavailablePage(), page.items)
                            for page in self.news_pages({"a/C1"}, {"q1", "q2"}))
        self.assertEqual(console.news(root, unavailable, "changes"), ())

    def test_one_news_request_reads_the_change_lists_once_for_both_pages(self) -> None:
        change = change_browser.WaitingChange("a/C1", "a", UnavailableProduction(Path("fixture.md")), {"#": "C1"})
        question = cast(change_browser.WaitingQuestion, cast(object, SimpleNamespace(key="question/q1")))
        reviews = change_browser.PreparedReviews(
            changes=(change,), queues={}, orders={}, questions=(question,), question_tabs=(), record=staff.StaffSnapshot(()))
        with patch.object(change_browser, "console_reviews", return_value=reviews) as read:
            self.assertEqual(console.news(self.root / "snapshots", (
                console.ConsolePage("changes","Changes","list-checks","Changes",console.AvailablePage("/console/review.html"),change_browser.waiting_changes),
                console.ConsolePage("questions","Questions","list-checks","Questions",console.AvailablePage("/console/review.html"),change_browser.waiting_questions)), "changes"), ())
        self.assertEqual(read.call_count, 1)
        seen = cast(dict[str, list[str]], json.loads((self.root / "snapshots" / change_page.CONSOLE_FOLDER / console.SEEN_NAME).read_text()))
        self.assertEqual((seen["changes"], seen["questions"]), (["a/C1"], ["question/q1"]))


class ForYouNotifierTests(unittest.TestCase):
    'Use fixture snapshots for the notifier and browser controls.'

    @staticmethod
    def action(key: str = "action/fixture", status: Literal["open", "done", "withdrawn", "unknown"] = "open") -> for_you.ForYouRow:
        return for_you.ForYouRow(key, "action", "Restore GitHub access", "github-warmup", "natedev",
                                 "/fixture", "sdlc", "Login needs your browser.", "Release check", "",
                                 status, "manual", "")

    def test_notifier_counts_and_returns_every_open_row_in_order(self) -> None:
        difference = "The running system differs from the configured system."
        requests = tuple(for_you.RebuildRequest("mac", reason, "builds", "Console review",
                                              f"2026-11-21T12:0{index}:00+00:00", "", "")
                         for index, reason in enumerate((
                             "Apply settings.", "Refresh certificates.", "Update packages.",
                             "Enable the service.", "Apply settings.",
                         )))
        completed = replace(requests[0], reason="Apply older settings.", evidence="Switch recorded.")
        rows = (self.action(), replace(self.action("rebuild/mac"), kind="rebuild", machine="mac", requested_by="",
                                       reason="Apply settings.\n" + difference, requests=(completed, *requests),
                                       open_requests=requests, system_difference=difference),
                replace(self.action("action/done"), status="done"), self.action("action/third"))
        snapshot = for_you.ForYouSnapshot(rows, ())
        with patch.object(for_you, "read", return_value=snapshot):
            result = console.for_you_payload()
        self.assertTrue(result["available"])
        assert result["available"] is True
        self.assertEqual(result["count"], 3)
        self.assertEqual([entry["key"] for entry in result["entries"]],
                         ["action/fixture", "rebuild/mac", "action/third"])
        self.assertEqual(result["entries"][1]["machine"], "mac")
        self.assertEqual(result["entries"][1]["requested_by"], "")
        self.assertEqual(result["entries"][0]["reason"], "Login needs your browser.")
        self.assertEqual(result["entries"][0]["requests"], [])
        self.assertEqual(result["entries"][1]["reason"], "")
        self.assertEqual(result["entries"][1]["system_difference"], difference)
        self.assertEqual(result["entries"][1]["requests"], [
            {"reason": request.reason, "requested_by": request.requested_by,
             "created_at": datetime.fromisoformat(request.created_at).timestamp()}
            for request in requests
        ])
        self.assertEqual(console.for_you_count(snapshot), {"available": True, "count": 3})
        for entry in result["entries"]:
            self.assertNotIn("href", entry)

    def test_zero_and_unavailable_counts_have_distinct_answers(self) -> None:
        for failures, available in (((), True), ((for_you.UnavailableSource("fixture", "Read failed."),), False)):
            with self.subTest(available=available), patch.object(for_you, "read", return_value=for_you.ForYouSnapshot((), failures)):
                answer = console.for_you_payload()
                self.assertEqual(answer["available"], available)
                self.assertEqual(answer, {"available": True, "count": 0, "entries": []} if available else {"available": False})

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for notifier controls.")
    def test_notifier_controls_on_desktop_and_phone(self) -> None:
        for width in (1400, 390, 681, 700, 715, 716):
            with self.subTest(width=width):
                assert_notifier_browser(self, width)


def assert_notifier_browser(case: unittest.TestCase, width: int) -> None:
    'Exercise the real shell with controlled reads, timers, and clipboard writes.'
    settings = Path(__file__).resolve().parents[2] / "config"
    pages = (console.ConsolePage("list", "List", "list-checks", "The List", console.AvailablePage("/page")),
             console.ConsolePage("review", "Review", "inbox", "Review", console.AvailablePage("/console/review.html")),
             console.ConsolePage("accounts", "Accounts", "users", "Accounts", console.AvailablePage("/accounts")))
    with patch.dict(os.environ, {"CONF_DIR": str(settings)}):
        source = console.render(pages, console.UnavailablePage()).decode()
    setup = r'''<script>
const nativeFetch=window.fetch.bind(window),timers=new Map();
let mode='failed',reads=0,visibility='visible',copied='',finishRead;
let accountsQuota={level:'urgent',name:'Urgent: 4% left'};
Object.defineProperty(document,'visibilityState',{get:()=>visibility});
window.setInterval=(run,ms)=>{const id=timers.size+1;timers.set(id,{run,ms});return id;};
window.clearInterval=id=>timers.delete(id);
const request=(reason,requested_by,minutes)=>({reason,requested_by,created_at:Date.now()/1000-minutes*60});
const longReason='Keep the changes that update the console settings and add the new machine configuration for the next restart.';
const difference='The running system differs from the configured system.';
const entries=[{key:'rebuild/natedev',kind:'rebuild',title:'Apply the new console settings',command:'rebuild',
 machine:'natedev',requested_by:'watcher, builds, sdlc',system_difference:difference,requests:[
  request(longReason,'sdlc',40),request('Update packages.','builds',15),request('Enable the service.','builds',25),
  request(longReason,'watcher',5),request('Refresh certificates.','sdlc',12)]},
 {key:'action/demo',kind:'action',title:'Restore GitHub access',command:'github-warmup',machine:'mac',requested_by:'sdlc',
  reason:'Login needs your browser.'}];
Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{copied=text;}}});
window.fetch=async(url,options)=>{
 if(String(url).includes('for-you.json')){reads++;if(mode==='failed') throw Error('Read failed.');
  const unavailable=mode==='unavailable' || mode==='http-unavailable';
  const payload=unavailable ? {available:false} : {available:true,count:mode==='zero'?0:2,entries:mode==='zero'?[]:entries};
  payload.accounts_quota=accountsQuota;
  const status=mode==='http-unavailable' ? 503 : 200;
  const answer={status,ok:status===200,json:async()=>payload};
  if(mode==='delayed') return new Promise(resolve=>{finishRead=()=>resolve(answer);});
  return answer;}
 if(options?.method==='POST') return new Response('{"new":[]}');
 return nativeFetch(url,options);
};
</script>'''
    probe = r'''<script>
(async()=>{
const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
try{
 await flush();
 const trigger=[...document.querySelectorAll('[data-for-you-toggle]')].find(node=>node.getBoundingClientRect().width);
 const panel=document.querySelector('#action-popover'),shell=document.querySelector('.shell');
 check(Boolean(trigger),'a visible notifier exists');
 if(!trigger) throw Error('Missing notifier.');
 check(trigger.dataset.state==='unavailable' && trigger.textContent.includes('?'),'the first failed read shows unavailable');
 mode='good';trigger.click();await flush();
 check(!panel.hidden && trigger.getAttribute('aria-expanded')==='true','the button opens the panel');
 check(trigger.textContent.includes('2') && panel.querySelectorAll('article').length===2,'the count and two entries appear');
 const machineLines=()=>[...panel.querySelectorAll('[data-entry-machine]')].map(line=>line.textContent);
 check(machineLines().join('|')==='natedev|mac · sdlc','only the action adds its requester to the machine line');
 const requestList=()=>panel.querySelector('[data-key="rebuild/natedev"] [data-rebuild-requests]');
 const requestToggle=()=>panel.querySelector('[data-key="rebuild/natedev"] [data-request-toggle]');
 const visibleRequests=()=>[...requestList().querySelectorAll('li:not([hidden])')];
 const lines=visibleRequests();
 check(lines.map(line=>line.querySelector('[data-request-text]').textContent).join('|')===
  [longReason,'Refresh certificates.','Update packages.'].join('|'),'the newest three groups show first');
 check(lines[0]?.querySelector('[data-request-count]').textContent.trim()==='×2' &&
  lines[0]?.querySelector('[data-request-by]').textContent===' · watcher' &&
  lines[0]?.querySelector('time').textContent==='5 min','the group shows its count, newest requester and age');
 const longText=lines[0]?.querySelector('[data-request-text]');
 check(lines[0]?.title===longReason && getComputedStyle(longText).textOverflow==='ellipsis' &&
  longText.scrollWidth>longText.clientWidth && getComputedStyle(longText).whiteSpace==='nowrap',
  'the long reason uses one line and has a full title');
 check(requestToggle().textContent==='Show 1 more','five requests form four groups');
 entries[0].requests.push(request('Apply the proxy setting.','sdlc',35));
 dispatchEvent(new Event('focus'));await flush();
 const showMore=requestToggle();
 check(showMore.matches('button') && showMore.textContent==='Show 2 more' &&
  showMore.getAttribute('aria-expanded')==='false' && showMore.getAttribute('aria-controls')===requestList().id,
  'six requests form five groups with an accessible disclosure button');
 showMore.click();
 check(showMore.textContent==='Show less' && showMore.getAttribute('aria-expanded')==='true' &&
  visibleRequests().length===5 && requestList().tabIndex===0 && getComputedStyle(requestList()).overflowY==='auto' &&
  getComputedStyle(requestList()).maxHeight!=='none','expansion shows all groups in a bounded list');
 showMore.click();
 check(showMore.textContent==='Show 2 more' && showMore.getAttribute('aria-expanded')==='false' &&
  visibleRequests().length===3,'the button restores the preview');
 check(panel.querySelectorAll('[data-system-difference]').length===1 &&
  panel.querySelector('[data-system-difference]').textContent===difference &&
  showMore.nextElementSibling.matches('[data-system-difference]'),'the difference appears once after the list');
 check(panel.querySelector('[data-entry-reason]').textContent===entries[1].reason,'the action shows its reason');
 check(requestList().scrollWidth===requestList().clientWidth,'the reasons fit the available width');
 check(panel.getBoundingClientRect().right<=innerWidth && panel.getBoundingClientRect().left>=0,'the panel fits the viewport');
 check(document.documentElement.scrollWidth===innerWidth,'the shell has no horizontal overflow');
 const copy=panel.querySelector('[data-copy-code]');copy.focus();copy.click();await flush();
 check(copied==='rebuild' && copy.getAttribute('aria-label')==='Copied','Copy writes the command');
 const code=panel.querySelector('code'),selection=getSelection(),range=document.createRange();
 range.selectNodeContents(code);selection.removeAllRanges();selection.addRange(range);
 mode='good';dispatchEvent(new Event('focus'));await flush();
 check(document.activeElement===copy && selection.toString()==='rebuild','refresh keeps focus and command selection');
 entries[0].command='rebuild --new';dispatchEvent(new Event('focus'));await flush();
 check(document.activeElement===copy && selection.toString()==='rebuild' && code.textContent==='rebuild','changed entries wait for the focused selection');
 selection.removeAllRanges();trigger.focus();await flush();
 check(panel.querySelector('code').textContent==='rebuild --new','released commands receive the pending refresh');
 const note=panel.querySelector('.panel-note');
 const crossCode=panel.querySelector('code');
 selection.setBaseAndExtent(note.firstChild,0,crossCode.firstChild,crossCode.textContent.length);
 const selected=selection.toString();
 entries[0].command='rebuild --later';dispatchEvent(new Event('focus'));await flush();
 check(selected.includes('rebuild --new') && selection.toString()===selected &&
  panel.querySelector('code')===crossCode,'a selection from the panel note into a command survives refresh');
 selection.removeAllRanges();await flush();
 check(panel.querySelector('code').textContent==='rebuild --later','releasing a cross-entry selection applies the update');
 const heading=panel.querySelector('[data-panel-count]');
 for(const result of ['zero','unavailable','http-unavailable']){
  mode='good';dispatchEvent(new Event('focus'));await flush();
  const start=heading.firstChild,end=panel.querySelector('code').firstChild;
  selection.setBaseAndExtent(start,0,end,end.length);
  const text=selection.toString(), count=result==='zero'?'0':'?';
  mode=result;dispatchEvent(new Event('focus'));await flush();
  check(trigger.querySelector('b').textContent===count,result+' updates the badge during selection');
  check(text.startsWith('2') && text.endsWith('rebuild --later') && selection.toString()===text &&
   selection.anchorNode===start && selection.focusNode===end,result+' preserves the panel count and command selection');
  selection.removeAllRanges();await flush();
  check(heading.textContent===count && panel.querySelectorAll('article').length===0 &&
   note.textContent===(result==='zero'?'No actions required.':'The action count is unavailable.'),
   result+' updates the panel after selection ends');
 }
 mode='good';dispatchEvent(new Event('focus'));await flush();
 mode='failed';dispatchEvent(new Event('focus'));await flush();
 check(trigger.dataset.state==='stale' && trigger.textContent.includes('2'),'a failed read keeps the last count and marks it stale');
 if(innerWidth<=680){
  const toggle=document.querySelector('#mobile-nav-toggle');
  const initial=shell.classList.contains('collapsed');
  for(const collapsed of [true,false]){
   if(shell.classList.contains('collapsed')!==collapsed) toggle.click();
   const badge=trigger.querySelector('b');
   check(badge.getBoundingClientRect().width>0 && getComputedStyle(badge).outlineStyle==='dotted',
    'the phone badge shows stale with '+(collapsed?'closed':'open')+' navigation');
  }
  if(shell.classList.contains('collapsed')!==initial) toggle.click();
 }
 mode='unavailable';dispatchEvent(new Event('focus'));await flush();
 check(trigger.dataset.state==='unavailable' && trigger.querySelector('b').textContent==='?' &&
  panel.querySelectorAll('article').length===0,'an unavailable count clears the previous number and entries');
 visibility='hidden';document.dispatchEvent(new Event('visibilitychange'));const before=reads;
 for(const timer of timers.values()) timer.run();await flush();
 check(reads===before && timers.size===0,'hidden pages stop timed reads');
 mode='good';visibility='visible';document.dispatchEvent(new Event('visibilitychange'));await flush();
 check(reads>before && timers.size>0,'visibility resumes reads and the timer');
 const completed=reads;dispatchEvent(new MessageEvent('message',{origin:location.origin,source:document.querySelector('iframe').contentWindow,data:'for-you-completed'}));await flush();
 check(reads>completed,'completion refreshes actions');
 const frame=document.querySelector('iframe');
 const queued=reads;mode='delayed';dispatchEvent(new Event('focus'));
 check(reads===queued+1 && typeof finishRead==='function','the fixture starts one blocked read');
 const completion={origin:location.origin,source:frame.contentWindow,data:'for-you-completed'};
 dispatchEvent(new MessageEvent('message',completion));
 dispatchEvent(new MessageEvent('message',completion));
 check(reads===queued+1,'completion does not start a concurrent read');
 mode='zero';finishRead();await flush();
 check(reads===queued+2 && trigger.dataset.state==='quiet','completion queues one read after a read in progress');
 mode='good';dispatchEvent(new Event('focus'));await flush();
 const collapsed=shell.classList.contains('collapsed');
 const escapeInFrame=()=>new frame.contentWindow.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true});
 const frameSearch=frame.contentDocument.querySelector('[data-filter-words]');
 frameSearch.value='auto';
 frameSearch.focus();
 frameSearch.dispatchEvent(escapeInFrame());
 check(!panel.hidden && frameSearch.value==='' && frame.contentDocument.activeElement===frameSearch,
  'Escape clears the List filter and keeps For you open.');
 const frameButton=frame.contentDocument.querySelector('button');
 frameButton.focus();
 check(document.activeElement===frame && frame.contentDocument.activeElement===frameButton,'focus enters the page frame');
 frameButton.dispatchEvent(new frame.contentWindow.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
 await flush();
 check(panel.hidden && document.activeElement===trigger,'Escape in the page frame closes the panel and restores focus');
 check(shell.classList.contains('collapsed')===collapsed,'frame Escape preserves sidebar state');
 if(panel.hidden) trigger.click();
 trigger.focus();
 trigger.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
 check(panel.hidden && document.activeElement===trigger,'Escape in the shell closes the panel and restores focus');
 check(shell.classList.contains('collapsed')===collapsed,'shell Escape preserves sidebar state');
 await flush();
 mode='zero';trigger.click();await flush();
 check(trigger.dataset.state==='quiet' && trigger.textContent.includes('0'),'zero uses a quiet visible notifier');
 check(panel.textContent.includes('No actions required.'),'the empty panel names the zero state');
 mode='http-unavailable';dispatchEvent(new Event('focus'));await flush();
 check(trigger.dataset.state==='unavailable' && trigger.querySelector('b').textContent==='?',
  'an explicit HTTP 503 replaces a known zero');
 mode='zero';dispatchEvent(new Event('focus'));await flush();
 mode='unavailable';dispatchEvent(new Event('focus'));await flush();
 check(trigger.dataset.state==='unavailable' && trigger.querySelector('b').textContent==='?',
  'an unavailable count replaces a known zero');
 mode='good';dispatchEvent(new Event('focus'));await flush();
 entries[0].requests.push(...Array.from({length:8},(_,index)=>request('Earlier request '+index,'builds',60+index)));
 dispatchEvent(new Event('focus'));await flush();
 panel.style.maxHeight='300px';
 requestToggle().click();
 const expandedList=requestList(),panelEntries=panel.querySelector('[data-for-you-entries]');
 expandedList.scrollTop=40;
 panelEntries.scrollTop=40;
 const listScrollTop=expandedList.scrollTop,panelScrollTop=panelEntries.scrollTop;
 check(listScrollTop>0 && panelScrollTop>0,'the expanded list and panel have scrolled');
 entries[1].reason='The browser login expired.';
 dispatchEvent(new Event('focus'));await flush();
 check(panel.querySelector('[data-entry-reason]').textContent===entries[1].reason,'the action update is visible');
 check(requestList()===expandedList && requestToggle().getAttribute('aria-expanded')==='true' &&
  requestList().scrollTop===listScrollTop && panelEntries.scrollTop===panelScrollTop,
  'an action refresh reuses the expanded rebuild list and preserves both scroll positions');
 entries[0].requests.push(request('One more rebuild request.','sdlc',90));
 dispatchEvent(new Event('focus'));await flush();
 check(requestList().lastElementChild.title==='One more rebuild request.' &&
  requestToggle().getAttribute('aria-expanded')==='true' && requestList().scrollTop===listScrollTop &&
  panelEntries.scrollTop===panelScrollTop,'a rebuild refresh restores the expanded list and both scroll positions');
 panel.style.removeProperty('max-height');
 entries[0].requests=[];entries[1].requested_by='';entries[1].reason='';
 dispatchEvent(new Event('focus'));await flush();
 check(machineLines().join('|')==='natedev|mac','an action without a requester shows the machine alone');
 check(requestList().hidden && requestToggle().hidden && panel.querySelector('[data-system-difference]').textContent===difference,
  'a rebuild with no requests shows only the difference');
 check(!panel.querySelector('[data-entry-reason]'),'an empty action reason leaves no element');
 check(!panel.querySelector('a,footer'),'the panel has no card or footer links');
 const url=new URL(location.href);
 check(url.searchParams.get('page')==='list' && url.searchParams.get('at.list')==='/page?filter=mine' &&
  url.searchParams.get('extra')==='kept','using the panel preserves the List location and other console parameters');
 panel.querySelector('#close-actions').click();
 if(innerWidth>680){document.getElementById('nav-toggle').click();await flush();
  check(trigger.getBoundingClientRect().width>0 && trigger.querySelector('b').getBoundingClientRect().width>0,'collapsed navigation keeps the icon and badge');
  trigger.click();await flush();
  const bounds=panel.getBoundingClientRect();
  check(bounds.width>0 && bounds.left>=0 && bounds.right<=innerWidth,'the panel fits beside collapsed navigation');
  check(document.documentElement.scrollWidth===innerWidth,'collapsed navigation has no horizontal overflow');}
 if(innerWidth===1400){
  const badge=document.querySelector('[data-page=accounts] #nav-quota');
  check(!badge.hidden && badge.getBoundingClientRect().width>0 && badge.getAttribute('aria-label')==='Urgent: 4% left',
   'the collapsed Accounts entry shows the urgent icon and its accessible name');
  if(shell.classList.contains('collapsed')) document.getElementById('nav-toggle').click();
  accountsQuota={level:'warning',name:'Warning: 9% left'};
  dispatchEvent(new Event('focus'));await flush();
  check(badge.getBoundingClientRect().width>0 && badge.querySelector('.lucide-triangle-alert')
   && !badge.querySelector('.lucide-octagon-alert'),'the next count read changes the open entry to warning');
  accountsQuota={level:'none',name:''};
  dispatchEvent(new Event('focus'));await flush();
  check(badge.hidden,'the next count read removes the last alert');
  const fixture=document.createElement('iframe');
  fixture.style.cssText='position:fixed;left:0;top:0;width:716px;height:800px;border:0';
  const bookmark='/console/?page=investigations&at.investigations='+
   encodeURIComponent('/console/investigations.html?show=all#showrunner=builds&q=shard');
  fixture.src=bookmark;
  await new Promise(resolve=>{fixture.addEventListener('load',resolve,{once:true});document.body.append(fixture);});
  const layout=fixture.contentDocument;
  const opened=new URL(fixture.contentWindow.location.href);
  const reviewFrame=layout.querySelector('iframe[title="Review"]');
  const filters=new URLSearchParams(new URL(reviewFrame.src).hash.slice(1));
  check(opened.searchParams.get('page')==='review' && filters.get('type')==='investigations' &&
   filters.get('show')==='all' && filters.get('source-owner')==='builds' && filters.get('q')==='shard',
   'the legacy Console bookmark opens the combined filter and preserves its filters');
  const sidebar=layout.querySelector('.shell'),toggle=layout.querySelector('#nav-toggle');
  const box=layout.querySelector('#action-popover');
  const button=layout.querySelector('.for-you-slot button');
  button.click();
  for(const collapsed of [false,true]){
   if(sidebar.classList.contains('collapsed')!==collapsed) toggle.click();
   for(let width=681;width<=716;width++){
    fixture.style.width=width+'px';
    const bounds=box.getBoundingClientRect();
    check(bounds.width>0 && bounds.left>=0 && bounds.right<=width,
     'the panel fits at '+width+' px with '+(collapsed?'collapsed':'expanded')+' navigation');
   }
  }
  fixture.style.width='390px';
  await flush();
  const mobile=layout.querySelector('.mobile-header [data-for-you-toggle]');
  layout.dispatchEvent(new fixture.contentWindow.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  check(box.hidden && layout.activeElement===mobile,'Escape restores focus to the visible button after resizing');
  fixture.remove();

  const listLoaded=()=>new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
  const loaded=listLoaded();
  frame.src='/page?filter=mine&restart=1#q=auto';
  await loaded;
  const list=frame.contentWindow;
  const cleared=new Promise(resolve=>list.addEventListener('hashchange',resolve,{once:true}));
  list.location.hash='q=';
  await cleared;
  const saved=new URL(location.href).searchParams.get('at.list');
  check(saved==='/page?filter=mine&restart=1#q=','The console saves the cleared List address.');
  const failedLoad=listLoaded();
  frame.src='data:text/html,Unavailable';
  await failedLoad;
  await listLoaded();
  check(list.location.pathname+list.location.search+list.location.hash===saved,
   'The frame retry opens the last saved List address with the words cleared.');
 }
}catch(error){failures.push(String(error));}
await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
    layout = source.replace('</head>', setup + '</head>')
    page = layout.replace('</body>', probe + '<img hidden src="/complete"></body>').encode()
    list_fixture = (
        '<!doctype html><title>List fixture</title><button>Fixture control</button>'
        + record_table.words_box('list-search', 'Filter by words')
        + '<script>' + record_table.SCRIPT
        + "filterControl.bind(document.querySelector('[data-filter-words]'),()=>{});</script>"
    ).encode()
    results: list[list[str]] = []
    complete = threading.Event()

    class Probe(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            if self.path == '/complete':
                _ = complete.wait(35)
            elif self.path.startswith('/console/?page=investigations'):
                _ = self.wfile.write(layout.encode())
            elif self.path.startswith('/?'):
                _ = self.wfile.write(page)
            elif self.path.startswith('/page?'):
                _ = self.wfile.write(list_fixture)
            else:
                _ = self.wfile.write(b'<!doctype html><title>Fixture</title><button>Fixture control</button>')

        def do_POST(self) -> None:
            results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(204)
            self.end_headers()
            complete.set()

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(('127.0.0.1', 0), Probe) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            profile = Path(temporary) / 'profile'
            profile.mkdir()
            _ = (profile / 'user.js').write_text('user_pref("ui.systemUsesDarkTheme",1);')
            result = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', str(profile),
                                     '--window-size', f'{width},800', '--screenshot', str(Path(temporary) / 'page.png'),
                                     f'http://127.0.0.1:{server.server_port}/?page=list&at.list=%2Fpage%3Ffilter%3Dmine&extra=kept'],
                                    capture_output=True, text=True, timeout=40)
            case.assertEqual(result.returncode, 0, result.stderr)
        finally:
            server.shutdown()
            thread.join(5)
    case.assertEqual(results, [[]])


if __name__ == "__main__":
    _ = unittest.main()
