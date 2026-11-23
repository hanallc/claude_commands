"""Test the console with private configuration and pages."""
from __future__ import annotations

from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import replace
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

from scripts.agents.remote import local_machine
from scripts.aws_target import aws_page, machines_page
from scripts.disks import disks_page
from scripts.lib import conf
from scripts.lint.memory_test_support import config_environment, fixture_feed_code_settings, write_config_fixture
from scripts.production import investigation, staff
from scripts.production.change_review_test_support import (
    REVIEW_ENDPOINT_SETTINGS, REVIEW_REFRESH_SETTINGS, write_change_review_settings,
)
from scripts.production.change_routing import UnavailableProduction
from scripts.review_links import (
    change_browser, change_page, console, for_you, investigations_page, page_frame, record_table, review_link,
    review_submissions,
)
from scripts.review_links.test_accounts_page import firefox_probe_results
from scripts.review_links.test_page_frame import console_settings


with fixture_feed_code_settings():
    from scripts.build_queue import memory_feed


CONSOLE_LINK_SETTINGS = REVIEW_ENDPOINT_SETTINGS + REVIEW_REFRESH_SETTINGS


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
        environment = config_environment(self.configuration, fixtures={
            "console.conf": console_settings(),
            "review_links.conf": CONSOLE_LINK_SETTINGS,
        })
        write_change_review_settings(self.configuration, self.root)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            **environment, "REVIEW_LINKS_ROOT": str(self.root / "snapshots"),
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

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for the owner dialog.")
    def test_unavailable_aws_machines_opens_new_item_for_builds(self) -> None:
        production = self.root / "builds.md"
        _ = production.write_text("# Builds\n> **Status: PRODUCTION — running.**\n")
        builds = staff.StaffRow(staff.StaffId("builds-staff"), "showrunner", "builds", staff.STAFF_ROOT,
                                current_session=staff.StaffId("builds-session"),
                                doc=staff.LiveDoc(local_machine(), production))
        production = self.root / "sdlc.md"
        _ = production.write_text("# SDLC\n> **Status: PRODUCTION — running.**\n")
        sdlc = staff.StaffRow(staff.StaffId("sdlc-staff"), "showrunner", "sdlc", staff.STAFF_ROOT,
                              current_session=staff.StaffId("sdlc-session"),
                              doc=staff.LiveDoc(local_machine(), production))
        morty = staff.StaffRow(staff.StaffId("morty-staff"), "producer", "morty", staff.STAFF_ROOT,
                               current_session=staff.StaffId("morty-session"))
        dwight = staff.StaffRow(staff.StaffId("dwight-staff"), "producer", "dwight", staff.STAFF_ROOT,
                                current_session=staff.StaffId("dwight-session"))
        aws = staff.StaffRow(staff.StaffId("aws-staff"), "unit", "aws", staff.ReportsTo(builds.id),
                             current_session=staff.StaffId("aws-session"),
                             unit_type=staff.UnitDetails("direct"))
        _ = write_config_fixture(self.configuration / "console.conf",
                                 f"page.aws_machines.owner={aws.id}\npage.list.owner={morty.id}\n")
        pages = (console.ConsolePage("aws_machines", "AWS Machines", "server", "AWS Machines", console.UnavailablePage()),
                 console.ConsolePage("list", "List", "list-checks", "The List", console.UnavailablePage()))
        members = staff.StaffSnapshot((aws, builds, sdlc, morty, dwight))
        with patch.object(staff, "local_snapshot", return_value=members):
            source = console.render(pages, console.UnavailablePage())
        settings = conf.read_or_repository("console")
        options: dict[str, object] = {}
        for page in pages:
            owner = review_submissions.page_owner(page.identity, settings, members)
            assert isinstance(owner, staff.StaffRow)
            options[page.identity] = {"token": "fixture", "page_owner": owner.id,
                                      "recipients": review_submissions.recipient_options(members, owner=owner)}
        probe = b"""<script>
(async()=>{
 const failures=[], check=(ok,message)=>{if(!ok) failures.push(message);};
 const realFetch=window.fetch.bind(window), requests=[], captures=[];
 const pageOptions=__PAGE_OPTIONS__;
 window.fetch=async (input, options)=>{
  const route=new URL(input, location.href);
  if(route.pathname==='/console/review/new-item' && route.search) {
   requests.push(route.search);
   return Response.json(pageOptions[route.searchParams.get('page')]);
  }
  if(route.pathname==='/console/review/new-item' && options?.method==='POST') {
   captures.push(options.body);
  }
  return Response.json({}, {status:400});
 };
 try {
  const pane=document.querySelector('#page-unavailable');
  const control=pane.querySelector('[data-page-owner="aws_machines"]');
  const button=control.querySelector('[data-owner-new]');
  check(!pane.hidden && pane.textContent.includes('Not available'), 'The unavailable notice is visible.');
  check(control.getClientRects().length && control.querySelector('.page-owner-name').textContent==='aws',
        'The unavailable page shows its configured owner.');
  check(button.getAttribute('aria-label')==='New item for builds', 'The button names the configured route.');
  const dialog=document.querySelector('#review-new-item'), recipients=dialog.querySelector('[data-recipients]');
  const loaded=()=>new Promise(resolve=>{
   const observer=new MutationObserver(()=>{if(recipients.firstChild){observer.disconnect();resolve();}});
   observer.observe(recipients,{childList:true});
  });
  const awsOptions=loaded();
  button.click();
  check(dialog.open, 'The owner button opens the shared dialog.');
  await awsOptions;
  check(requests.join()==='?page=aws_machines' && recipients.querySelector('input:checked')?.value==='builds-staff',
        'The dialog requests the AWS Machines owner and selects builds.');
  const choices=()=>[...recipients.querySelectorAll('[data-button-face]')].map(chip=>chip.textContent).sort().join();
  check(choices()==='builds,dwight,morty,sdlc', 'The dialog offers every producer and showrunner.');
  const screenshot=dialog.querySelector('[name=include_page]');
  check(!screenshot.getClientRects().length && !screenshot.checked,
        'The unavailable page offers no screenshot.');
  check(dialog.querySelector('[data-capture-slot]').hidden && captures.length===0,
        'The unavailable page starts no capture.');
  recipients.querySelector('input[value=dwight-staff]').closest('label').click();
  check(recipients.querySelector('input:checked')?.value==='dwight-staff',
        'A producer chip changes the selected recipient.');
  dialog.querySelector('[data-close]').click();
  document.querySelector('[data-page=list]').click();
  const listOwner=pane.querySelector('[data-page-owner=list]');
  check(!control.getClientRects().length && listOwner.getClientRects().length,
        'The owner changes with the selected unavailable page.');
  const listOptions=loaded();
  listOwner.querySelector('[data-owner-new]').click();
  await listOptions;
  check(requests.join()==='?page=aws_machines,?page=list' &&
        recipients.querySelector('input:checked')?.value==='morty-staff',
        'The List dialog selects its owner.');
  check(choices()==='builds,dwight,morty,sdlc', 'The List dialog offers the same recipients.');
 } catch(error) {failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>""".replace(b"__PAGE_OPTIONS__", json.dumps(options).encode())
        self.assertEqual(firefox_probe_results(source.replace(b"</body>", probe + b"</body>")), [[]])

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
        self.assertEqual(markup.pages, ["accounts", "aws", "aws_machines", "builds", "disks", "images", "list", "review", "placement_disk"])
        self.assertEqual(markup.sources, [])
        # Each page has its own icon and a hidden new mark; the menu button has one more mark.
        self.assertEqual(source.count('aria-hidden="true" focusable="false"'), 27)
        self.assertEqual(source.count('class="lucide lucide-sparkle"'), 10)
        self.assertEqual(source.count('<span class="new-mark" hidden>'), 9)
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

    def test_fixture_config_names_staff_for_every_console_page(self) -> None:
        settings = conf.read("console").values()
        pages = {key.split(".")[1] for key in settings if key.startswith("page.") and key.endswith(".url")}
        self.assertEqual(pages | {"rebuilds"}, page_frame.OWNER_PAGES)
        self.assertEqual(console.owner_config_problems(settings), ())

    def test_console_owner_problems_name_invalid_missing_and_distinct_ids(self) -> None:
        settings = conf.read("console").values()
        settings["page.aws.owner"] = "-" * 36
        settings["owner.default"] = "12345678-1234-1234-1234-123456789012"
        del settings["page.list.owner"]
        self.assertEqual(console.owner_config_problems(settings), (
            "page.aws.owner: Expected a staff UUID.",
            "page.list.owner: Expected a staff UUID.",
            "owner.default: Use the same staff ID as page.review.owner.",
        ))

    def test_the_placement_disk_page_has_its_keys_and_an_available_icon(self) -> None:
        keys = conf.shipped_keys(Path(__file__).resolve().parents[2] / "config/console.conf")
        self.assertLessEqual({f"page.placement_disk.{name}" for name in ("label", "icon", "title", "url", "owner")}, keys)
        self.assertIn("torus", console.ICON_NAMES)
        self.assertTrue((page_frame.ICONS / "torus.svg").is_file())
        self.assertIn('class="lucide lucide-torus"', console.icon_svg("torus"))

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
        self.assertEqual([page.identity for page in pages], ["placement_disk", "review", "list", "images", "disks", "builds", "aws_machines", "aws", "accounts"])
        self.assertIsInstance(pages[3].destination, console.AvailablePage)
        self.assertIsInstance(pages[5].destination, console.UnavailablePage)
        markup = ConsoleMarkup()
        source = console.render(pages, console.UnavailablePage()).decode()
        self.assertEqual(source.count(page_frame.BUTTON_STATES), 1)
        markup.feed(source)
        self.assertEqual(markup.pages, ["placement_disk", "review", "list", "images", "disks", "builds", "aws_machines", "aws", "accounts"])
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
        self.assertEqual([page.identity for page in pages], ["accounts", "aws", "aws_machines", "builds", "disks", "images", "list", "review", "placement_disk"])
        self.assertIsInstance(pages[3].destination, console.UnavailablePage)
        self.assertIsInstance(pages[-2].destination, console.AvailablePage)
        self.assertIn('aria-label="Builds — Not available"', console.render(pages, console.read_rebuilds()).decode())

    def test_url_functions_cannot_put_an_external_page_in_the_frame(self) -> None:
        port = 8123
        _ = write_config_fixture(self.configuration / "review_links.conf", f"server_port={port}\n", defaults="")
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

    def test_notifier_counts_each_open_row_and_limits_the_panel_to_two_entries(self) -> None:
        rows = (self.action(), replace(self.action("rebuild/mac"), kind="rebuild", machine="mac"),
                self.action("action/third"), replace(self.action("action/done"), status="done"))
        with patch.object(for_you, "read", return_value=for_you.ForYouSnapshot(rows, ())):
            result = console.for_you_payload()
        self.assertTrue(result["available"])
        assert result["available"] is True
        self.assertEqual(result["count"], 3)
        self.assertEqual(len(result["entries"]), 2)
        self.assertEqual(result["entries"][1]["machine"], "mac")
        self.assertEqual(result["entries"][0]["href"],
                         "?page=review&at.review=%2Fconsole%2Freview.html%3Ftype%3Dfor-you%26show%3Dopen%26item%3Daction%252Ffixture")

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
    pages = (console.ConsolePage("list", "List", "list-checks", "The List", console.AvailablePage("/page")),
             console.ConsolePage("review", "Review", "inbox", "Review", console.AvailablePage("/console/review.html")),
             console.ConsolePage("accounts", "Accounts", "users", "Accounts", console.AvailablePage("/accounts")))
    with tempfile.TemporaryDirectory() as directory:
        environment = config_environment(Path(directory), fixtures={"review_links.conf": CONSOLE_LINK_SETTINGS})
        with patch.dict(os.environ, environment):
            source = console.render(pages, console.UnavailablePage()).decode()
    setup = r'''<script>
const nativeFetch=window.fetch.bind(window),timers=new Map();
let mode='failed',reads=0,visibility='visible',copied='',finishRead;
let accountsQuota={level:'urgent',name:'Urgent: 4% left'};
Object.defineProperty(document,'visibilityState',{get:()=>visibility});
window.setInterval=(run,ms)=>{const id=timers.size+1;timers.set(id,{run,ms});return id;};
window.clearInterval=id=>timers.delete(id);
const entries=[{key:'rebuild/natedev',kind:'rebuild',title:'Apply the new console settings',command:'rebuild',
 machine:'natedev',requested_by:'watcher',href:'?page=review&at.review=%2Fconsole%2Freview.html%3Ftype%3Dfor-you%26show%3Dopen%26item%3Drebuild%252Fnatedev'},
 {key:'action/demo',kind:'action',title:'Restore GitHub access',command:'github-warmup',machine:'mac',requested_by:'sdlc',href:'?page=review&at.review=%2Fconsole%2Freview.html%3Ftype%3Dfor-you%26show%3Dopen%26item%3Daction%252Fdemo'}];
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
 check(panel.textContent.includes('natedev') && panel.textContent.includes('mac'),'the entries name each machine');
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
 const link=panel.querySelector('[data-for-you-card]');
 check(new URL(link.href).searchParams.get('at.list')==='/page?filter=mine','the card href keeps the saved List location');
 link.click();
 const reviewFrame=document.querySelector('[data-console-page=review]');
 await new Promise(resolve=>reviewFrame.addEventListener('load',resolve,{once:true}));
 const url=new URL(location.href),place=new URL(url.searchParams.get('at.review'),location.origin);
 check(url.searchParams.get('page')==='review' && place.searchParams.get('type')==='for-you' &&
 place.searchParams.get('item')==='rebuild/natedev','a card opens Review with the reserved parameters and key');
 check(panel.hidden,'a card closes the panel');
 check(url.searchParams.get('at.list')==='/page?filter=mine' && url.searchParams.get('extra')==='kept',
  'opening a card preserves unrelated console parameters');
 const original=reviewFrame.contentDocument;
 trigger.click();await flush();
 const reloaded=new Promise(resolve=>reviewFrame.addEventListener('load',resolve,{once:true}));
 panel.querySelector('footer a').click();
 await reloaded;
 const footerURL=new URL(location.href),footerPlace=new URL(footerURL.searchParams.get('at.review'),location.origin);
 const child=new URL(reviewFrame.contentWindow.location.href);
 check(reviewFrame.contentDocument!==original && child.search===footerPlace.search,
  'The footer reloads the fixture at the recorded query.');
 check(footerURL.searchParams.get('at.list')==='/page?filter=mine' && footerURL.searchParams.get('extra')==='kept' &&
  footerPlace.searchParams.get('type')==='for-you' && !footerPlace.searchParams.has('item'),
  'opening the footer preserves unrelated parameters and clears the card key');
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
