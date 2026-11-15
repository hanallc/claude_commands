"""Test the console with private configuration and pages."""
from __future__ import annotations

from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from typing import cast, override
from unittest.mock import patch

from scripts.aws_target import aws_page
from scripts.build_queue import memory_feed
from scripts.lib import conf
from scripts.production import staff
from scripts.production.change_routing import UnavailableProduction
from scripts.review_links import change_browser, change_page, console, review_link


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
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(self.configuration), "REVIEW_LINKS_ROOT": str(self.root / "snapshots"),
            "REVIEW_LINKS_HOST": "127.0.0.1", "STAFF_FILE": str(self.root / "staff.json")})))

    def test_publication_reuses_the_changes_link_and_inlines_only_local_icons(self) -> None:
        with (patch.object(memory_feed, "page_url", return_value="/builds/memory_live.html", create=True),
              patch.object(aws_page, "page_url", return_value="/aws/aws_prices.html")):
            source = console.render(console.read_pages(), console.read_rebuilds()).decode()
        self.assertEqual(change_page.console_url(), f"http://127.0.0.1:{conf.read('review_links').whole('server_port')}/console/")
        folder = self.root / "snapshots" / change_page.CONSOLE_FOLDER
        self.assertTrue((folder / change_page.BROWSER_MANIFEST).is_file())
        markup = ConsoleMarkup()
        markup.feed(source)
        self.assertEqual(markup.pages, ["aws", "builds", "changes", "images", "questions"])
        self.assertEqual(markup.sources, [])
        # Each page has its own icon and a hidden new mark; the menu button has one more mark.
        self.assertEqual(source.count('aria-hidden="true" focusable="false"'), 15)
        self.assertEqual(source.count('class="lucide lucide-sparkle"'), 6)
        self.assertEqual(source.count('<span class="new-mark" hidden>'), 5)
        self.assertIn('<span class="new-mark" id="menu-new" hidden>', source)
        self.assertIn('data-news="/console/news"', source)
        self.assertIn("showNews();\n    loadNews();\n    if (navigate && phone.matches) collapse(true);", source)
        self.assertIn("const value = fresh.has(entry.dataset.page) && !entry.hasAttribute('aria-current');", source)
        self.assertIn("function refresh() { loadRebuilds(); loadNews(); }", source)
        base = f"/{change_page.CONSOLE_FOLDER}"
        changes = f"{base}/{change_page.PAGE_NAME}"
        self.assertIn(f'data-url="{changes}" data-title="Changes"', source)
        self.assertIn(f'data-url="{changes}?type=questions" data-title="Questions"', source)
        self.assertIn(f'data-url="{base}/{change_page.REBUILDS_NAME}" data-title="Rebuilds needed" hidden>', source)
        retry = conf.read("review_links").number("frame_retry_seconds")
        self.assertIn(f'data-frame-retry-seconds="{retry}"', source)
        self.assertIn("try { loaded = frame.contentWindow.location.origin === location.origin; } catch (_) {}", source)
        self.assertIn("setTimeout(() => { frame.src = place; }, Number(shell.dataset.frameRetrySeconds) * 1000);", source)
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
        for removed in ("<dialog", "showModal", "lucide-x"):
            self.assertNotIn(removed, source)
        self.assertIn('class="lucide lucide-refresh-cw"', source)
        self.assertIn("/builds/memory_live.html", source)
        self.assertIn("/aws/aws_prices.html", source)
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
        self.assertNotIn('page-header', source)
        self.assertIn('.workspace{flex:1;width:100%}', source)
        self.assertIn("mobileToggle.setAttribute('aria-expanded', String(!value))", source)

    def test_builds_url_resolves_from_its_public_function(self) -> None:
        folder = self.root / "snapshots/builds"
        folder.mkdir(parents=True)
        page = folder / "memory_live.html"
        _ = page.write_text("<!doctype html><title>Builds</title><h1>Builds</h1>")
        _ = (self.configuration / "memory_feed.conf").write_text(f"output_folder={folder}\n")
        pages = console.read_pages()
        self.assertEqual(pages[1].destination, console.AvailablePage("/builds/memory_live.html"))
        self.assertIn('data-url="/builds/memory_live.html" data-title="Builds"', console.render(pages, console.UnavailablePage()).decode())
        self.assertEqual(page.read_text(), "<!doctype html><title>Builds</title><h1>Builds</h1>")

    def test_config_order_and_unavailable_pages_remain_visible(self) -> None:
        source = (self.configuration / "console.conf").read_text()
        blocks = source.split("\n\n")
        _ = (self.configuration / "console.conf").write_text("\n\n".join(reversed(blocks)))
        with patch.object(memory_feed, "page_url", side_effect=FileNotFoundError("fixture page absent"), create=True):
            pages = console.read_pages()
        self.assertEqual([page.identity for page in pages], ["questions", "images", "changes", "builds", "aws"])
        self.assertIsInstance(pages[1].destination, console.AvailablePage)
        self.assertIsInstance(pages[3].destination, console.UnavailablePage)
        markup = ConsoleMarkup()
        source = console.render(pages, console.UnavailablePage()).decode()
        markup.feed(source)
        self.assertEqual(markup.pages, ["questions", "images", "changes", "builds", "aws"])
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
        self.assertEqual([page.identity for page in pages], ["aws", "builds", "changes", "images", "questions"])
        self.assertIsInstance(pages[1].destination, console.UnavailablePage)
        self.assertIsInstance(pages[2].destination, console.AvailablePage)
        self.assertIn('aria-label="Builds — Not available"', console.render(pages, console.read_rebuilds()).decode())

    def test_url_functions_cannot_put_an_external_page_in_the_frame(self) -> None:
        for path in ("https://example.invalid/page", "//example.invalid/page", "/../outside", "/%2e%2e/outside",
                     "http://127.0.0.1:1/page", "https://127.0.0.1:8765/page"):
            with self.subTest(path=path), patch.object(memory_feed, "page_url", return_value=path, create=True):
                pages = console.read_pages()
                self.assertIsInstance(pages[1].destination, console.UnavailablePage)

    def news_pages(self, changes: set[str], questions: set[str]) -> tuple[console.ConsolePage, ...]:
        with (patch.object(change_browser, "waiting_changes", return_value=frozenset(changes)),
              patch.object(change_browser, "waiting_questions", return_value=frozenset(questions))):
            return console.read_pages()

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
            pages = console.read_pages()
        self.assertEqual(console.news(root, pages, "questions"), ())
        self.assertEqual(console.news(root, self.news_pages({"a/C1"}, {"q1", "q2"}), "changes"), ("questions",))
        unavailable = tuple(console.ConsolePage(page.identity, page.label, page.icon, page.title, console.UnavailablePage(), page.items)
                            for page in self.news_pages({"a/C1"}, {"q1", "q2"}))
        self.assertEqual(console.news(root, unavailable, "changes"), ())

    def test_one_news_request_reads_the_change_lists_once_for_both_pages(self) -> None:
        change = change_browser.WaitingChange("a/C1", "a", UnavailableProduction(Path("fixture.md")), {"#": "C1"})
        question = cast(change_browser.WaitingQuestion, cast(object, SimpleNamespace(key="question/q1")))
        reviews = change_browser.PreparedReviews((change,), {}, (question,), (), cast(staff.StaffSnapshot, cast(object, None)))
        with patch.object(change_browser, "console_reviews", return_value=reviews) as read:
            self.assertEqual(console.news(self.root / "snapshots", console.read_pages(), "changes"), ())
        self.assertEqual(read.call_count, 1)
        seen = cast(dict[str, list[str]], json.loads((self.root / "snapshots" / change_page.CONSOLE_FOLDER / console.SEEN_NAME).read_text()))
        self.assertEqual((seen["changes"], seen["questions"]), (["a/C1"], ["question/q1"]))


if __name__ == "__main__":
    _ = unittest.main()
