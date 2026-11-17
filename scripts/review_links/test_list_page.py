"""Test The List page with private records and a real browser."""
from __future__ import annotations

import html
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode
from uuid import uuid4
from xml.etree import ElementTree

from scripts.accounts import accounts
from scripts.agents.remote import local_machine
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import change_review, change_routing, delivery_state, investigation, session_staff, showrunners, staff, the_list
from scripts.production.update_registration import QuestionStaff
from scripts.review_links import console, list_page, staff_names
from scripts.review_links import change_browser, change_page
from scripts.review_links import review_items, review_page
from scripts.review_links.test_change_page import BrowserForms
from scripts.review_links.test_page_frame import assert_status_states
from scripts.review_links.test_staff_names import without_role_colors
from scripts.statusline import counts_row, rebuild_row

MORTY = staff.StaffRow(staff.StaffId("536997b5-4415-4cb1-b683-d777e02e9a22"), "producer", "morty", staff.StaffRoot(),
                        current_session=staff.StaffId("536997b5-4415-4cb1-b683-d777e02e9a22"))
OWNER = staff.StaffRow(staff.StaffId("a0882376-677b-4da5-adbb-f11db9146021"), "showrunner", "sdlc", staff.StaffRoot(),
                        current_session=staff.StaffId("a0882376-677b-4da5-adbb-f11db9146021"))
OTHER = staff.StaffRow(staff.StaffId("f5636030-6745-4ff4-aeda-4c2773d18da8"), "showrunner", "builds", staff.StaffRoot(),
                        current_session=staff.StaffId("f5636030-6745-4ff4-aeda-4c2773d18da8"))
DWIGHT = staff.StaffRow(staff.StaffId("920b0a64-6eee-479e-8c07-e03e4dcd4ef6"), "producer", "dwight", staff.StaffRoot(),
                        current_session=staff.StaffId("920b0a64-6eee-479e-8c07-e03e4dcd4ef6"))
NOW = datetime(2026, 11, 16, 18, tzinfo=UTC)


class ListPageTests(unittest.TestCase):
    root: Path = Path()

    def test_staff_owner_and_producer_names_have_role_colors_but_nate_stays_plain(self) -> None:
        _ = self.add("Staff names")
        _ = self.add("User name", owner=the_list.UnassignedOwner.NATE)
        body = list_page.render(self.root / "reviews").decode()
        self.assertRegex(body, r'data-color-text="role.showrunner"[^>]*>sdlc</span>')
        self.assertRegex(body, r'data-color-text="role.producer"[^>]*>morty</span>')
        self.assertIn('<td data-label="Owner">nate</td>', body)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="list-page-test-")))
        config = self.root / "config"
        config.mkdir()
        for name in ("review_links", "console", "change_review"):
            _ = shutil.copyfile(Path(__file__).resolve().parents[2] / f"config/{name}.conf", config / f"{name}.conf")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config),
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"), "STAFF_FILE": str(self.root / "staff.json"),
            "USER_ZONE": "America/New_York", "REVIEW_LINKS_ROOT": str(self.root / "reviews")})))
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
            row.to_json() for row in (MORTY, DWIGHT, OWNER, OTHER)]}))
        _ = self.enterContext(patch.object(the_list, "now", return_value=NOW))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        _ = self.enterContext(patch.object(rebuild_row, "read", return_value=rebuild_row.RebuildAnswer((), False)))

    def add(self, title: str, state: the_list.ItemState = the_list.ItemState.WORKING, *,
            owner: the_list.ItemOwner = OWNER.id, number: int | the_list.NextNumber = the_list.NextNumber.AVAILABLE,
            next_check: the_list.NextCheck = the_list.NoTimestamp.UNSET) -> the_list.ListItem:
        return the_list.add(MORTY.id, title, owner=owner, number=number, source="nate", changes={
            "state": state, "next_check": next_check, "next_step": "Read the result and check the page.",
            "latest_status": "The page reads the saved records.", "done_when": "The checks pass.",
            "evidence": "Read https://example.invalid/proof?x=1&y=2.",
            "eta": the_list.EtaRange(NOW + timedelta(hours=1), NOW + timedelta(hours=2))})

    def fixture_items(self) -> list[the_list.ListItem]:
        items = [self.add("Remove the mesh code", the_list.ItemState.WAITING_FOR_NATE, number=58),
                 self.add("Console status", number=105, owner=OTHER.id, next_check=NOW - timedelta(minutes=10)),
                 self.add("The List in the console Straße", number=106, next_check=NOW + timedelta(hours=1)),
                 self.add("Shared documentation structure", the_list.ItemState.PAUSED, number=41),
                 self.add("Read the delivered page", the_list.ItemState.DONE, number=63),
                 self.add("Removed task", number=54)]
        _ = the_list.mark_read(MORTY.id, items[0].id, source="nate")
        _ = the_list.update(MORTY.id, items[1].id, {"producer": DWIGHT.id}, source="morty")
        _ = the_list.park(MORTY.id, items[3].id, NOW + timedelta(days=2), source="nate")
        _ = the_list.close(MORTY.id, items[5].id, "The user removed this task.", source="nate")
        return items

    def test_records_share_cli_order_and_each_render_reads_current_fields(self) -> None:
        items = self.fixture_items()
        _ = the_list.update(MORTY.id, items[0].id, {"related_to": (106,),
            "blocked_by": (the_list.ItemDependency(105, "The status page reaches main"),)}, source="morty")
        body = without_role_colors(list_page.render(self.root / "reviews").decode())
        self.assertEqual(body.count('<tbody data-list-item'), 6)
        for label in list_page.HEADERS:
            self.assertIn(label + '<span data-sort-mark', body)
        for value, label in list_page.SHOWS:
            self.assertIn(f'<option value="{value}">{label}</option>', body)
        self.assertIn('<option value="' + OWNER.id + '">sdlc</option>', body)
        self.assertIn('<option value="' + OTHER.id + '">builds</option>', body)
        producers = body.split('<select id="list-producer">', 1)[1].split('</select>', 1)[0]
        self.assertEqual(producers, '<option value="">All</option><option value="' + DWIGHT.id + '">dwight</option>'
                         + '<option value="' + MORTY.id + '">morty</option>')
        self.assertEqual(body.count('<td data-label="Producer">dwight</td>'), 1)
        self.assertEqual(body.count('<td data-label="Producer">morty</td>'), 5)
        self.assertIn(f'data-producer="{DWIGHT.id}"', body)
        for text in ('Latest status', 'Done when', 'Log · newest first', 'Links', 'Closing words',
                     'Check overdue', '14:00 EST', '15:00 EST'):
            self.assertIn(text, body)
        self.assertNotIn('<h3>Evidence</h3>', body)
        self.assertEqual(the_list.load(MORTY.id, items[0].id).evidence, items[0].evidence)
        self.assertEqual(body.count('<form method="post" data-feedback-form>'), 6)
        self.assertEqual(body.count('value="read">Mark read</button>'), 6)
        self.assertEqual([entry.item.number for entry in list_page.records()],
                         [item.number for item in sorted(the_list.records(MORTY.id, include_closed=True), key=the_list.order)])
        closed = body.split(f'data-key="{MORTY.id}/L54"', 1)[1].split('</tbody>', 1)[0]
        self.assertNotIn('Check overdue', closed)
        _ = the_list.update(MORTY.id, "L106", {"title": "New title <script>", "latest_status": "New status"}, source="morty")
        changed = list_page.render(self.root / "reviews").decode()
        self.assertIn('New title &lt;script&gt;', changed)
        self.assertNotIn('<script>New title', changed)
        self.assertIn('New status', changed)

    def test_linked_reviews_share_one_current_read_and_link_to_the_review_cards(self) -> None:
        def change(identity: str, state: review_items.ReviewState) -> review_items.ReviewItem:
            source = review_items.ChangeSource(self.root / "changes.md", {"#": identity},
                change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(OWNER.id, OWNER.name), self.root / "sdlc.md"),
                delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery()))
            return review_items.ReviewItem("sdlc/" + identity, identity, review_items.ReviewType.CHANGE,
                OWNER.id, OWNER.id, "Check " + identity, state, review_items.ReviewTimes(NOW, NOW), (), (), source)

        ready = (change("C1", review_items.ReviewState.FOR_YOU), change("C2", review_items.ReviewState.FOR_YOU))
        waiting = change("C3", review_items.ReviewState.WAITING)
        done = change("C4", review_items.ReviewState.DONE)
        later = replace(change("C5", review_items.ReviewState.FOR_YOU),
                        times=review_items.ReviewTimes(NOW, NOW, NOW + timedelta(days=1)))
        question = investigation.file(OWNER.id, "Find the cause", "Why did it stop?", waiting_why='The owner must complete the investigation.')
        _ = investigation.answer(OWNER.id, question.id, "The cause is known.")
        answer = review_items.investigation_items(staff.snapshot())[0]
        reviews = [*ready, waiting, done, later, answer]
        cases = (
            ((ready[0].key, ready[1].key), "2 changes for you"),
            ((ready[0].key, answer.key), "2 reviews for you"),
            ((waiting.key, done.key, "missing/C9"), "1 review waiting on others"),
            ((waiting.key, later.key), "2 reviews waiting on others"),
            ((answer.key,), "1 review for you"),
            ((done.key,), "Reviews done — decide: close, keep or park"),
            (("missing/C9",), "Reviews done — decide: close, keep or park"),
            ((), ""),
            ((later.key,), "1 review waiting on others"),
            ((ready[0].key, waiting.key, done.key), "1 change for you"),
        )
        items: list[the_list.ListItem] = []
        for index, (identities, _) in enumerate(cases):
            item = self.add(f"Review group {index}")
            items.append(the_list.link(MORTY.id, item.id, identities, source="morty"))
        paths = [the_list.folder(MORTY.id) / f"{item.id}.md" for item in items]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        root = self.root / "reviews"
        with patch.object(review_items, "read", return_value=reviews) as read, patch.object(list_page, "counts_strip", return_value=""):
            body = list_page.render(root).decode()
        read.assert_called_once_with(root, members=staff.snapshot())
        for item, (identities, expected) in zip(items, cases):
            with self.subTest(item=item.id):
                row = body.split(f'data-key="{MORTY.id}/{item.id}"', 1)[1].split('</tbody>', 1)[0]
                state = row.split('<td data-label="State">', 1)[1].split('</td>', 1)[0]
                if not identities:
                    self.assertNotIn(review_page.PAGE_PATH, row)
                    continue
                self.assertIn(expected, state)
                self.assertEqual(row.count(expected), 2)
                self.assertIn(f'href="{review_page.PAGE_PATH}" target="_top">{expected}</a>', state)
                detail = row.split('<div class="list-panel"><div>', 1)[1]
                self.assertLess(detail.index(expected), detail.index('<form'))
                positions: list[int] = []
                for identity in identities:
                    matching = [review for review in reviews if review.key == identity]
                    if matching:
                        href = html.escape(review_page.PAGE_PATH + '#' + urlencode({"item": identity}), quote=True)
                        card = f'<a href="{href}" target="_top">{html.escape(matching[0].id)}</a>'
                    else:
                        card = html.escape(identity)
                        self.assertNotIn('#' + urlencode({"item": identity}), detail)
                    self.assertIn(card, detail)
                    positions.append(detail.index(card))
                self.assertEqual(positions, sorted(positions))
        with patch.object(review_items, "read", return_value=[
                replace(review, state=review_items.ReviewState.DONE) for review in reviews]) as read:
            refreshed = list_page.render(root).decode()
        read.assert_called_once()
        first = refreshed.split(f'data-key="{MORTY.id}/{items[0].id}"', 1)[1].split('</tbody>', 1)[0]
        self.assertIn("Reviews done — decide: close, keep or park", first)
        self.assertNotIn("2 changes for you", first)
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

    def test_unlinked_rows_do_not_read_the_review_stores(self) -> None:
        _ = self.add("Work without reviews")
        with patch.object(review_items, "read", side_effect=AssertionError("No review read is needed")) as read:
            body = list_page.render(self.root / "reviews").decode()
        read.assert_not_called()
        self.assertIn('<strong>Work without reviews</strong>', body)
        self.assertNotIn("Reviews done — decide", body)

    def test_review_keys_keep_duplicate_ids_distinct_and_link_only_supported_cards(self) -> None:
        source = review_items.ChangeSource(self.root / "changes.md", {"#": "C1"},
            change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(OWNER.id, OWNER.name), self.root / "sdlc.md"),
            delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery()))
        ready = review_items.ReviewItem("change/sdlc/C1", "C1", review_items.ReviewType.CHANGE,
            OWNER.id, OWNER.id, "Check C1", review_items.ReviewState.FOR_YOU, review_items.ReviewTimes(NOW, NOW), (), (), source)
        done = replace(ready, key="change/builds/C1", state=review_items.ReviewState.DONE)
        proposal = replace(ready, key="proposal/sdlc/P1", id="P1", type=review_items.ReviewType.PROPOSAL,
                           source=review_items.ProposalLedgerSource(self.root / "proposals.md"))
        efficiency = replace(proposal, key="efficiency/sdlc/E1", id="sdlc/E1", type=review_items.ReviewType.EFFICIENCY,
                             source=review_items.EfficiencySource(self.root / "efficiencies.md", "Result"))
        for review, expected in ((ready, "1 change for you"), (done, "Reviews done — decide: close, keep or park"),
                                 (proposal, "1 review for you"), (efficiency, "1 review for you")):
            with self.subTest(key=review.key):
                item = self.add("Review " + review.key)
                _ = the_list.link(MORTY.id, item.id, (review.key,), source="morty")
                with patch.object(review_items, "read", return_value=[ready, done, proposal, efficiency]) as read, patch.object(
                        list_page, "counts_strip", return_value=""):
                    body = list_page.render(self.root / "reviews").decode()
                read.assert_called_once()
                row = body.split(f'data-key="{MORTY.id}/{item.id}"', 1)[1].split('</tbody>', 1)[0]
                self.assertIn(expected, row)
                card = review_page.PAGE_PATH + '#' + urlencode({"item": review.key})
                if review.type in review_page.TYPES:
                    self.assertIn(f'<a href="{html.escape(card, quote=True)}" target="_top">{review.id}</a>', row)
                else:
                    self.assertNotIn(card, row)
                    self.assertIn('<p>' + review.id + '</p>', row)

    def test_unknown_owners_and_unassigned_owners_keep_their_labels(self) -> None:
        missing = staff.StaffId("owner-without-row")
        for owner in (missing, the_list.UnassignedOwner.NATE, the_list.UnassignedOwner.NOT_RECORDED):
            _ = self.add("Work for " + owner, owner=owner)
        body = list_page.render(self.root / "reviews").decode()
        for label in (str(missing), "nate", "Not recorded"):
            self.assertIn('>' + label + '</option>', body)
        _ = (self.root / "staff.json").write_text('{"schema":1,"rows":[]}')
        self.assertEqual(len(list_page.records()), 3)

    def test_feedback_button_belongs_to_the_words_box_and_starts_disabled(self) -> None:
        item = self.add("Explain this item")
        form = list_page.feedback_form(list_page.ListRecord(MORTY.id, item), "fixture-secret")
        actions, words = form.split('</div>', 1)
        self.assertNotIn('value="tell"', actions)
        self.assertNotIn('<label for="words-', words)
        self.assertIn('placeholder="Your words for morty about this item"', words)
        self.assertIn('aria-label="Your words for morty about this item"', words)
        self.assertIn('</textarea><div class="list-tell"><span role="status" data-send-status></span>'
                      + '<button name="action" value="tell" disabled>Tell morty</button></div>', words)
        self.assertIn("!words.value.trim()", list_page.SCRIPT)
        self.assertIn("words.addEventListener('input',retain)", list_page.SCRIPT)
        self.assertIn("buttons.forEach(button=>button.disabled=false);syncTell()", list_page.SCRIPT)

    def test_record_fields_share_the_markdown_formatter(self) -> None:
        item = self.add("Formatted record")
        names = (("latest_status", "Status"), ("done_when", "Completion"), ("next_step", "Next"))

        def text(heading: str) -> str:
            return f"## {heading}\n\n**{heading} field:** Ready.\n- One\n- Two"

        _ = the_list.update(MORTY.id, item.id, {"latest_status": text("Status"), "done_when": text("Completion"),
            "evidence": text("Evidence"), "next_step": text("Next")}, source="morty")
        _ = the_list.close(MORTY.id, item.id, "## Closing\n\n**Closing field:** Closed.\n1. First\n2. Second", source="nate")
        body = list_page.render(self.root / "reviews").decode()
        next_step = body.split('<td data-label="Next step">', 1)[1].split('</td>', 1)[0]
        details = body.split('<div class="list-panel"><div>', 1)[1].split('<section><h3>Log · newest first', 1)[0]
        self.assertNotIn('<h3>Evidence</h3>', details)
        self.assertNotIn('<h2>Evidence</h2>', details)
        self.assertEqual(the_list.load(MORTY.id, item.id).evidence, text("Evidence"))
        for _, heading in (*names, ("closing_words", "Closing")):
            rendered = next_step if heading == "Next" else details
            self.assertIn(f'<h2>{heading}</h2>', rendered)
            self.assertIn(f'<strong>{heading} field:</strong>', rendered)
            self.assertNotIn('**', rendered)
            self.assertNotIn('## ', rendered)
        self.assertIn('<ul><li>One</li><li>Two</li></ul>', details)
        self.assertIn('<ol><li>First</li><li>Second</li></ol>', details)

    def test_bad_records_leave_valid_rows_and_nav_news(self) -> None:
        good = self.add("Valid item")
        bad = self.add("Invalid item")
        path = the_list.folder(MORTY.id) / f"{bad.id}.md"
        _ = path.write_text(path.read_text().replace('state: "Working"', 'state: "bad"'))
        errors = io.StringIO()
        with redirect_stderr(errors):
            body = list_page.render(self.root / "reviews").decode()
            news = list_page.waiting_updates(console.NewsRequest(self.root))
        self.assertIn('<strong>Valid item</strong>', body)
        self.assertNotIn('<strong>Invalid item</strong>', body)
        self.assertEqual(news, frozenset({f"{MORTY.id}/{good.id}/{NOW.isoformat()}"}))
        self.assertEqual(len(errors.getvalue().splitlines()), 2)

    def test_nav_marks_new_update_times_and_keeps_read_operations_out_of_the_page(self) -> None:
        pages = tuple(page for page in console.read_pages() if page.identity == "list")
        root = self.root / "reviews"
        self.assertEqual(list_page.page_url(), '/console/list.html')
        self.assertEqual(console.news(root, pages, "changes"), ())
        item = self.add("Unread item")
        self.assertEqual(console.news(root, pages, "changes"), ("list",))
        self.assertEqual(console.news(root, pages, "list"), ())
        self.assertEqual(console.news(root, pages, "changes"), ())
        with patch.object(the_list, "now", return_value=NOW + timedelta(minutes=1)):
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "A new result arrived."}, source="morty")
        self.assertEqual(console.news(root, pages, "changes"), ("list",))
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        self.assertEqual(console.news(root, pages, "changes"), ())

    def test_count_strip_colors_its_showrunner_with_the_page_name_reading(self) -> None:
        members = staff.StaffSnapshot((replace(OWNER, doc=staff.LiveDoc(local_machine(), self.root / "sdlc.md")),))
        with (patch.object(staff, "snapshot", return_value=members),
              patch.object(list_page, "change_counts", return_value={"sdlc": 1}),
              patch.object(accounts, "role_colors",
                           wraps=accounts.role_colors) as colors,
              patch.object(counts_row, "question_counts", return_value=(0, ""))):
            body = list_page.render(self.root / "reviews").decode()
        colors.assert_called_once()
        chip = body.split('<span class="list-count-chip">', 1)[1].split('<a ', 1)[0]
        self.assertRegex(chip, r'data-color-text="role.showrunner"[^>]*>sdlc</span> · ')

    def test_counts_use_status_line_readers(self) -> None:
        owners: list[staff.StaffRow] = []
        listings: list[Path] = []
        for owner, count in ((OWNER, 3), (OTHER, 1)):
            doc = self.root / f"{owner.name}.md"
            listing = self.root / f"{change_review.PREFIX}{owner.name}.md"
            _ = doc.write_text(f'# Production\n- **Change review:** `{listing}`\n')
            _ = listing.write_text('## To review\n\n| # | Change | How to check | Producer |\n'
                + '| --- | --- | --- | --- |\n'
                + ''.join(f'| C{number} | Work | Read it | {owner.id} |\n' for number in range(1, count + 1)))
            owners.append(replace(owner, doc=staff.LiveDoc(local_machine(), doc)))
            listings.append(listing)
        members = staff.StaffSnapshot(tuple(owners))
        with listings[0].open('a') as output:
            _ = output.write(f'| C4 | A change for builds | Read it | {OTHER.id} |\n')
        _ = self.add("A valid List row")
        with (patch.object(counts_row, "question_counts", return_value=(2, "")) as questions,
              patch.object(staff, "snapshot", return_value=members),
              patch.object(showrunners, "timer_docs", return_value={owner.name: owner.doc.path
                  for owner in owners if isinstance(owner.doc, staff.LiveDoc)}),
              patch.object(change_review, "lists_directory", return_value=self.root),
              patch.object(change_review, "review_snapshot", wraps=change_review.review_snapshot) as read,
              patch.object(change_routing, "StaffRouting", wraps=change_routing.StaffRouting) as routing,
              patch.object(session_staff, "member_sources", side_effect=ValueError("Invalid pane record")) as panes,
              patch.object(rebuild_row, "read", return_value=rebuild_row.RebuildAnswer(("mac",), False)) as rebuilds):
            body = list_page.counts_strip(members, staff_names.StaffNames(members))
            self.assertEqual(questions.call_count, 2)
            self.assertEqual(questions.call_args_list[0].args[0], owners[1])
            self.assertEqual(questions.call_args_list[1].args[0], owners[0])
            self.assertCountEqual([call.args[0] for call in read.call_args_list], listings)
            routing.assert_called_once()
            panes.assert_not_called()
            rebuilds.assert_called_once_with(conf.path("statusline"))
            root = self.root / "reviews"
            root.mkdir(exist_ok=True)
            status = change_page.DeliveryStatus(root)
            with patch.object(QuestionStaff, "for_record", return_value=QuestionStaff(members, session_staff.MemberSources(()))):
                for validation in (False, True):
                    with self.subTest(validation=validation):
                        if validation:
                            page = change_page.ReviewPage(listings[0], listings[0], ("C1",), "")
                            _ = status.path(page, "C1").write_text(json.dumps({"validation": {
                                "attempt": "recorded", "state": "pending", "feedback": "Check it"}}))
                        forms = BrowserForms()
                        forms.feed(change_browser.render(change_page.ReviewBrowser("fixture"), status).decode())
                        totals = list_page.change_counts()
                        self.assertEqual(totals, forms.counts)
                        self.assertEqual(totals, {"sdlc": 2 if validation else 3, "builds": 2})
                        chips = list_page.counts_strip(members, staff_names.StaffNames(members))
                        for name, count in totals.items():
                            chip = without_role_colors(chips).split(name + ' · ', 1)[1].split('</span>', 1)[0]
                            self.assertIn(f'<b>{count}</b> changes', chip)
            for reader, label in (("questions", "questions"), ("changes", "changes"), ("rebuilds", "rebuild")):
                target = (counts_row if reader == "questions" else list_page if reader == "changes" else rebuild_row)
                function = {"questions": "question_counts", "changes": "change_counts", "rebuilds": "read"}[reader]
                with (self.subTest(failed_count=reader), patch.object(target, function, side_effect=OSError("Unavailable")),
                      patch.object(staff, "snapshot", return_value=members)):
                    available = list_page.render(self.root / "reviews").decode()
                    self.assertIn('<strong>A valid List row</strong>', available)
                    self.assertNotIn(f'</b> {label}<', available)
                    self.assertNotIn(f'</b> {label}s<', available)
                    self.assertIn('</b> changes<' if reader == "questions" else '</b> questions<', available)
        for page, place in (("questions", '/console/changes.html?type=questions#showrunner=sdlc'),
                            ("changes", '/console/changes.html#showrunner=sdlc'),
                            ("rebuilds", '/console/rebuilds.html')):
            link = '/console/' + console.place_query(page, place)
            self.assertIn(f'href="{html.escape(link, quote=True)}" target="_top"', body)
        for text in ('<b>2</b> questions', '<b>3</b> changes', '<b>1</b> rebuild'):
            self.assertIn(text, body)
        self.assertNotIn("proposals", body)
        self.assertNotIn("efficiencies", body)

    def fields(self, item: the_list.ListItem, action: str, **changes: str) -> dict[str, str]:
        request = uuid4().hex
        return {"keeper": MORTY.id, "id": item.id, "request": request,
                "token": list_page.request_token(self.root / "reviews", MORTY.id, item.id, request),
                "action": action, "words": "", "until": "", **changes}

    def test_delivery_notices_color_staff_names_and_keep_the_recorded_words(self) -> None:
        _ = self.add("Delivery notice")
        cases: tuple[tuple[the_list.PageFeedback, str, tuple[str, ...]], ...] = (
            (the_list.PageFeedback("request", "Words"), "sent to morty", ("morty",)),
            (the_list.PageFeedback("request", "Words", "sent"), "sent to morty", ("morty",)),
            (the_list.PageFeedback("request", "Words", "alerted", "sdlc"), "not sent; sdlc told", ("sdlc",)),
            (the_list.PageFeedback("request", "Words", "alerted", "<unknown>"), "not sent; <unknown> told", ()),
            (the_list.PageFeedback("request", "Words", "alerted"), "not sent; no showrunner for alert", ()),
        )
        for feedback, text, colored in cases:
            with (self.subTest(state=feedback.state, recipient=feedback.alerted_to),
                  patch.object(the_list, "sent_to_morty", return_value=NOW),
                  patch.object(the_list, "feedback_requests", return_value={feedback.request: feedback})):
                body = list_page.render(self.root / "reviews").decode()
                cell = body.split('<td data-label="Update">', 1)[1].split('</td>', 1)[0]
                notice = ElementTree.fromstring('<root>' + cell + '</root>').find('small')
                assert notice is not None
                self.assertEqual(''.join(notice.itertext()), text + ", 13:00 EST")
                self.assertEqual(tuple(span.text for span in notice.findall('span')), colored)

    def test_signed_feedback_logs_words_and_uses_the_shared_sender(self) -> None:
        root = self.root / "reviews"
        item = self.add("The feedback row", number=106)
        initial = item
        with patch.object(change_page, "send_to_staff") as send:
            delivery = list_page.submit(root, urlencode(self.fields(item, "read")))
            list_page.deliver(delivery)
            send.assert_not_called()
            item = the_list.load(MORTY.id, item.id)
            self.assertIs(item.unread_since, the_list.NoTimestamp.UNSET)
            self.assertEqual(item.log[-1].what, "Marked read")
            for action, extra, words in (("remove", {}, "Remove 106"),
                    ("park", {"until": "2026-12-03"}, "Park 106 until 2026-12-03"),
                    ("tell", {"words": "Explain this simply.\nKeep these words."}, "Explain this simply.\nKeep these words.")):
                with self.subTest(action=action):
                    fields = self.fields(item, action, **extra)
                    delivery = list_page.submit(root, urlencode(fields))
                    item = the_list.load(MORTY.id, item.id)
                    self.assertIn(json.dumps(words), item.log[-1].what)
                    self.assertEqual(the_list.sent_to_morty(item), NOW)
                    body = list_page.render(root).decode()
                    self.assertIn("sent to morty, 13:00 EST", without_role_colors(body))
                    self.assertIn('data-feedback-form', body)
                    list_page.deliver(delivery)
                    self.assertEqual(send.call_args.args[0], MORTY.id)
                    self.assertIn("nate's words for item 106\n" + words, cast(str, send.call_args.args[2]))
                    item = the_list.load(MORTY.id, item.id)
                    before = send.call_count, item.log
                    list_page.deliver(list_page.submit(root, urlencode(fields)))
                    self.assertEqual((send.call_count, the_list.load(MORTY.id, item.id).log), before)
            self.assertEqual(item.state, initial.state)
            self.assertEqual(item.parked_until, initial.parked_until)
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "Morty acts."}, source="morty")
            self.assertNotIn("<small>sent to morty,", without_role_colors(list_page.render(root).decode()))
            good = self.fields(item, "tell", words="Keep it.")
            for body in (urlencode({**good, "token": "bad"}), urlencode(good) + '&id=L2',
                         urlencode({**good, "words": ""}), urlencode({**good, "words": " \t\n "}),
                         urlencode({**good, "action": "unknown"}),
                         urlencode({**good, "action": "park", "until": "2026-02-30"})):
                with self.subTest(body=body), self.assertRaises(ValueError):
                    _ = list_page.submit(root, body)

    def test_feedback_recovers_after_restart_and_alerts_after_failed_tries(self) -> None:
        root = self.root / "reviews"
        item = self.add("Recover the message")
        _ = list_page.submit(root, urlencode(self.fields(item, "tell", words="Ask sdlc for an ETA.")))
        saved = the_list.load(MORTY.id, item.id)
        with (patch.object(change_page, "send_to_staff") as send,
              patch.object(change_review, "lists_directory", return_value=self.root / "lists")):
            list_page.recover_pending()
            send.assert_called_once()
            self.assertIn("Ask sdlc for an ETA.", cast(str, send.call_args.args[2]))
            list_page.recover_pending()
            send.assert_called_once()
        self.assertEqual(the_list.feedback_requests(the_list.load(MORTY.id, item.id)),
                         {key: replace(value, state="sent") for key, value in the_list.feedback_requests(saved).items()})
        delivery = list_page.submit(root, urlencode(self.fields(item, "remove")))
        def fail_message(recipient: staff.StaffId, _sender: str, _message: str) -> None:
            if recipient == MORTY.id:
                raise RuntimeError("fixture offline")
        with (patch.object(change_page, "send_to_staff", side_effect=fail_message) as send,
              patch.object(time, "sleep"), redirect_stderr(io.StringIO())):
            list_page.deliver(delivery)
            tries = conf.read("review_links").whole("delivery_tries")
            self.assertEqual(send.call_count, tries + 1)
            self.assertIn("List feedback needs attention", cast(str, send.call_args.args[2]))
            self.assertIn("Remove 1", cast(str, send.call_args.args[2]))
            self.assertEqual(send.call_args.args[0], OWNER.id)
            list_page.recover_pending()
            list_page.deliver(delivery)
            self.assertEqual(send.call_count, tries + 1)
            alerted = the_list.load(MORTY.id, item.id)
            latest = tuple(the_list.feedback_requests(alerted).values())[-1]
            self.assertEqual((latest.state, latest.alerted_to), ("alerted", "sdlc"))
            self.assertIn("not sent; sdlc told", without_role_colors(list_page.render(root).decode()))
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "Alert handled"}, source="morty")
            self.assertNotIn("not sent; sdlc told", without_role_colors(list_page.render(root).decode()))
        self.assertEqual(the_list.load(MORTY.id, item.id).state, item.state)

    def test_failed_alert_keeps_feedback_pending_for_post_and_startup_retries(self) -> None:
        root = self.root / "reviews"
        item = self.add("Retry when the alert fails")
        fields = self.fields(item, "tell", words="Keep trying until someone receives this.")
        delivery = list_page.submit(root, urlencode(fields))
        pending = the_list.load(MORTY.id, item.id)
        with (patch.object(change_page, "send_to_staff", side_effect=RuntimeError("all sends fail")) as send,
              patch.object(time, "sleep"), redirect_stderr(io.StringIO())):
            list_page.deliver(delivery)
            tries = conf.read("review_links").whole("delivery_tries")
            self.assertEqual(send.call_count, tries + 1)
            self.assertEqual(the_list.feedback_requests(the_list.load(MORTY.id, item.id))[fields["request"]].state, "pending")
            self.assertNotIn("not sent; sdlc told", without_role_colors(list_page.render(root).decode()))
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            self.assertEqual(send.call_count, 2 * (tries + 1))
            list_page.recover_pending()
            self.assertEqual(send.call_count, 3 * (tries + 1))
            self.assertEqual(the_list.load(MORTY.id, item.id), pending)
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
            list_page.recover_pending()
            send.assert_called_once()
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].state, "sent")

    def test_feedback_save_failures_leave_recoverable_requests_only(self) -> None:
        root = self.root / "reviews"
        item = self.add("Save before sending")
        for published in (False, True):
            with self.subTest(published=published):
                fields = self.fields(item, "tell", words="Keep these words.")
                original = the_list.save_feedback
                def failed_save(keeper: staff.StaffId, identity: str, feedback: the_list.PageFeedback) -> the_list.PageFeedback:
                    if published:
                        _ = original(keeper, identity, feedback)
                    raise OSError("fixture save failed")
                with patch.object(the_list, "save_feedback", side_effect=failed_save):
                    with self.assertRaisesRegex(OSError, "fixture save failed"):
                        _ = list_page.submit(root, urlencode(fields))
                saved = the_list.load(MORTY.id, item.id)
                self.assertEqual(isinstance(the_list.sent_to_morty(saved), datetime), published)
                self.assertEqual(fields["request"] in the_list.feedback_requests(saved), published)
                with patch.object(change_page, "send_to_staff") as send:
                    if published:
                        list_page.deliver(list_page.submit(root, urlencode(fields)))
                        send.assert_called_once()
                    list_page.recover_pending()
                    list_page.recover_pending()
                    self.assertEqual(send.call_count, int(published))
                    if published:
                        list_page.deliver(list_page.submit(root, urlencode(fields)))
                        self.assertEqual(send.call_count, 1)
                if published:
                    item = the_list.update(MORTY.id, item.id, {"latest_status": "Recorded"}, source="morty")

    def test_feedback_claim_releases_the_list_lock_during_delivery(self) -> None:
        root = self.root / "reviews"
        item = self.add("Send without blocking edits")
        other = self.add("Mark this item read")
        delivery = list_page.submit(root, urlencode(self.fields(item, "tell", words="Keep these words.")))
        entered, release = threading.Event(), threading.Event()
        def send(_recipient: staff.StaffId, _sender: str, _message: str) -> None:
            entered.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("the test did not release delivery")
        def edit_items() -> None:
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "Morty edited during delivery"}, source="morty")
            _ = the_list.mark_read(MORTY.id, other.id, source=the_list.PAGE_SOURCE)
        with patch.object(change_page, "send_to_staff", side_effect=send) as sends, ThreadPoolExecutor(max_workers=2) as pool:
            sending = pool.submit(list_page.deliver, delivery)
            try:
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                list_page.deliver(delivery)
                edited = pool.submit(edit_items)
                edited.result(TEST_HANG_GUARD_SECS)
                self.assertIs(the_list.load(MORTY.id, other.id).unread_since, the_list.NoTimestamp.UNSET)
                self.assertEqual(sends.call_count, 1)
            finally:
                release.set()
            sending.result(TEST_HANG_GUARD_SECS)
        current = the_list.load(MORTY.id, item.id)
        self.assertEqual(current.latest_status, "Morty edited during delivery")
        self.assertEqual(tuple(the_list.feedback_requests(current).values())[-1].state, "sent")
        self.assertIs(the_list.sent_to_morty(current), the_list.NoTimestamp.UNSET)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_shared_page_status_uses_the_real_list_loader(self) -> None:
        _ = self.fixture_items()
        assert_status_states(self, list_page.render(self.root / "reviews").decode(), 15)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_sort_filters_hash_refresh_open_rows_and_phone_cards(self) -> None:
        _ = self.fixture_items()
        self.browser(list_page.render(self.root / "reviews").decode())

    def browser(self, markup: str) -> None:
        setup = r'''<script>
const nativeFetch=fetch, nativeTimeout=setTimeout, errors=[], reads=[],posts=[];
let original=MARKUP,holdPost=false,finishPost,failRefresh=false,realFeedback=false,loseResponse=false;
window.setInterval=()=>0;window.clearInterval=()=>{};
window.fetch=async(_path,options={})=>{
 if(options.method==='POST'){
  const fields=new URLSearchParams(options.body);posts.push(fields);
  if(realFeedback){
   const result=await nativeFetch('/feedback',options);
   if(loseResponse){loseResponse=false;throw new Error('fixture lost response');}
   return result;
  }
  if(fields.get('action')==='read'){
   const page=new DOMParser().parseFromString(original,'text/html');
   const row=[...page.querySelectorAll('[data-list-item]')].find(row=>row.dataset.number==='106');
   row.dataset.unread='false';row.querySelector('.list-dot')?.remove();row.querySelector('[value="read"]')?.remove();
   original=page.documentElement.outerHTML;
  }
  const result={ok:true,status:200,json:async()=>({state:'sent',request:String(posts.length).padStart(32,'0'),token:'b'.repeat(64)})};
  if(holdPost) return await new Promise(resolve=>finishPost=()=>resolve(result));
  return result;
 }
 reads.push(1);if(failRefresh) throw new Error('fixture refresh failed');
 if(realFeedback) return await nativeFetch('/fresh');
 return {ok:true,status:200,text:async()=>original};
};
addEventListener('error',event=>errors.push(event.message));
addEventListener('unhandledrejection',event=>errors.push(String(event.reason)));
</script>'''
        probe = r'''<script>
(async()=>{
const failed=[],check=(ok,text)=>{if(!ok) failed.push(text);};
const flush=()=>new Promise(resolve=>nativeTimeout(resolve,50));
const rows=()=>[...document.querySelectorAll('[data-list-item]')];
const visible=()=>rows().filter(row=>!row.hidden);
const change=(id,value,event='change')=>{const field=document.getElementById(id);field.value=value;field.dispatchEvent(new Event(event,{bubbles:true}));};
try{
 await flush();
 if(innerWidth>=960){
  check(document.querySelector('main').getBoundingClientRect().width===1124,'the desktop uses the shared column width');
  for(const header of document.querySelectorAll('[data-sort]')){
   const label=document.createRange();label.selectNodeContents(header.firstChild);
   check(label.getClientRects().length===1,'the desktop header fits on one line: '+header.textContent);
  }
 }
 check(visible().map(row=>row.dataset.number).join(',')==='58,105,106','the default order and Open filter');
 document.querySelector('[data-sort="0"]').click();
 check(visible().map(row=>row.dataset.number).join(',')==='58,105,106','the number sort is numeric');
 document.querySelector('[data-sort="0"]').click();
 check(visible().map(row=>row.dataset.number).join(',')==='106,105,58','a second click reverses the sort');
 for(let column=1;column<8;column++){
  document.querySelector('[data-sort="'+column+'"]').click();
  check(document.querySelector('[data-sort="'+column+'"]').parentElement.getAttribute('aria-sort')==='ascending','sort column '+column);
 }
 change('list-show','needs-me');check(visible().length===1 && visible()[0].dataset.number==='58','Needs me');
 change('list-show','unread');check(visible().length===5,'Unread includes the saved unread updates');
 change('list-show','done');check(visible().length===1 && visible()[0].dataset.number==='63','Done');
 change('list-show','parked');check(visible().length===1 && visible()[0].dataset.number==='41','Parked');
 change('list-show','closed');check(visible().length===1 && visible()[0].dataset.number==='54','Closed');
 change('list-show','all');check(visible().length===6,'All');
 change('list-owner',OWNER);check(visible().length===5,'Owner');
 change('list-owner','');
 change('list-producer',DWIGHT);check(visible().map(row=>row.dataset.number).join(',')==='105','Producer');
 check(new URLSearchParams(location.hash.slice(1)).get('producer')===DWIGHT,'the address keeps the producer');
 change('list-producer','');
 check(visible().length===6 && !new URLSearchParams(location.hash.slice(1)).has('producer'),'All producers');
 change('list-search','#106','input');
 check(visible().length===1 && visible()[0].dataset.number==='106','number search');
 change('list-search','console','input');check(visible().length===2,'title search');
 check(new URLSearchParams(location.hash.slice(1)).get('q')==='console','the address keeps the query');
 for(const title of ['Straße','STRAẞE']){
  change('list-search',title,'input');
  check(visible().length===1 && visible()[0].dataset.number==='106','Unicode title search: '+title);
 }
 change('list-search','console','input');
 const selected=visible().find(row=>row.dataset.number==='106');selected.querySelector('.list-toggle').click();
 check(!selected.querySelector('[data-detail]').hidden,'a row opens');
 const words=selected.querySelector('textarea'),tell=selected.querySelector('[value="tell"]');
 check(tell.textContent==='Tell morty','the button names its recipient');
 check(words.placeholder==='Your words for morty about this item' && words.getAttribute('aria-label')===words.placeholder,'the words box has its accessible name');
 check(!selected.querySelector('label[for="'+words.id+'"]'),'the words box has no repeated visible label');
 check(!tell.closest('.list-actions') && tell.parentElement.querySelector('[data-send-status]'),'the status stays beside the separate primary button');
 const wordsRect=words.getBoundingClientRect(),tellRect=tell.getBoundingClientRect();
 check(tellRect.top>=wordsRect.bottom && Math.abs(tellRect.right-wordsRect.right)<1,'the button sits below the box at its right edge');
 check(tell.disabled,'empty words disable Tell morty');
 words.value=' \t\n ';words.dispatchEvent(new Event('input'));
 check(tell.disabled,'spaces disable Tell morty');
 const emptyPosts=posts.length;tell.click();check(posts.length===emptyPosts,'empty words cannot send');
 words.value='x';words.dispatchEvent(new Event('input'));check(!tell.disabled,'typed text enables Tell morty immediately');
 words.value='';words.dispatchEvent(new Event('input'));check(tell.disabled,'cleared words disable Tell morty immediately');
 words.value='Keep my draft';words.dispatchEvent(new Event('input'));
 const field=document.querySelector('#list-search');field.focus();field.setSelectionRange(1,3);
 document.body.style.minHeight='2400px';scrollTo(0,100);
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
 const refreshed=rows().find(row=>row.dataset.number==='106');
 check(reads.length===1,'the shared status requests a fresh page');
 check(document.querySelector('#list-search').value==='console','refresh keeps the query');
 check(document.activeElement.id==='list-search','refresh keeps focus');
 check(document.activeElement.selectionStart===1 && document.activeElement.selectionEnd===3,'refresh keeps the selection');
 check(!refreshed.querySelector('[data-detail]').hidden,'refresh keeps open rows');
 check(refreshed.querySelector('textarea').value==='Keep my draft','refresh keeps feedback words');
 check(!refreshed.querySelector('[value="tell"]').disabled,'a restored draft enables Tell morty');
 check(scrollY===100,'refresh keeps scroll');
 realFeedback=true;loseResponse=true;
 let retryForm=rows().find(row=>row.dataset.number==='106').querySelector('form');
 retryForm.elements.words.value='Deliver this feedback once';retryForm.elements.words.dispatchEvent(new Event('input'));
 const retryId=retryForm.elements.request.value,retryToken=retryForm.elements.token.value,startPosts=posts.length;
 retryForm.requestSubmit(retryForm.querySelector('[value="tell"]'));
 await flush();await flush();await flush();await flush();
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();await flush();
 retryForm=rows().find(row=>row.dataset.number==='106').querySelector('form');
 check(retryForm.elements.request.value===retryId && retryForm.elements.token.value===retryToken,'refresh keeps the failed request identity');
 retryForm.requestSubmit(retryForm.querySelector('[value="tell"]'));
 await flush();await flush();await flush();await flush();
 check(posts.length===startPosts+2,'the lost response has one retry');
 check(posts.at(-1).get('request')===retryId && posts.at(-1).get('token')===retryToken,'the retry uses its original signature');
 retryForm=rows().find(row=>row.dataset.number==='106').querySelector('form');
 check(retryForm.elements.request.value!==retryId,'success clears the saved retry identity');
 check(retryForm.elements.words.value==='' && retryForm.querySelector('[value="tell"]').disabled,'a successful send clears the box and disables Tell morty');
 realFeedback=false;failRefresh=true;
 const requestIds=[];
 for(const action of ['read','remove','park','tell']){
  const row=rows().find(row=>row.dataset.number==='106'),form=row.querySelector('form');
  form.elements.until.value='2026-12-03';form.elements.words.value='Explain this simply';
  requestIds.push(form.elements.request.value);
  holdPost=action==='tell';
  form.requestSubmit(form.querySelector('[value="'+action+'"]'));
  if(holdPost){await flush();form.elements.words.value='New draft during send';form.elements.words.dispatchEvent(new Event('input'));
   check(form.querySelector('[value="tell"]').disabled,'editing words during delivery keeps Tell morty disabled');finishPost();}
  check(form.querySelector('[data-send-status]').textContent===(action==='read' ? 'Saving…' : 'Sending…'),'immediate response '+action);
  await flush();await flush();
  check(posts.at(-1).get('action')===action && posts.at(-1).get('id')==='L106','signed feedback '+action);
  check(posts.at(-1).get('token').length===64,'the form carries its token');
  check(form.elements.request.value!==requestIds.at(-1),'success renews the request without refresh');
  if(action==='tell'){
   check(form.elements.words.value==='New draft during send','a newer draft survives the send');
   check(!form.querySelector('[value="tell"]').disabled,'the newer draft enables Tell morty after delivery');
  }
  check(!rows().find(row=>row.dataset.number==='106').querySelector('[data-detail]').hidden,'feedback keeps the row open');
 }
 check(new Set(requestIds).size===4,'each new action uses a fresh request');
 location.hash=new URLSearchParams({show:'closed',sort:'#',dir:'desc'});await flush();
 check(visible().length===1 && visible()[0].dataset.number==='54','hash navigation restores the filters');
 location.hash=new URLSearchParams({show:'all',producer:DWIGHT});await flush();
 check(document.getElementById('list-producer').value===DWIGHT && visible().map(row=>row.dataset.number).join(',')==='105',
   'an address restores the producer filter');
 location.hash=new URLSearchParams({show:'all',producer:'missing'});await flush();
 check(document.getElementById('list-producer').value==='' && visible().length===6,'an unknown producer shows all items');

 for(const target of rows().filter(row=>['Done','Closed'].includes(row.dataset.state) || row.dataset.parked==='true')){
  location.hash=new URLSearchParams({item:target.dataset.key});await flush();
  check(!target.hidden && !target.querySelector('[data-detail]').hidden,'an item link shows '+target.dataset.state);
  check(document.getElementById('list-show').value==='all' && document.getElementById('list-owner').value==='' &&
    document.getElementById('list-producer').value==='' && document.getElementById('list-search').value==='','an item link clears filters');
 }
 const retained=rows().find(row=>row.dataset.number==='106');
 location.hash=new URLSearchParams({item:retained.dataset.key,owner:retained.dataset.owner,show:'open',q:'106',sort:'#',dir:'desc'});await flush();
 check(document.getElementById('list-owner').value===retained.dataset.owner && document.getElementById('list-show').value==='open' &&
   document.getElementById('list-search').value==='106','an address with an open item keeps its filters');
 check(!retained.hidden && !retained.querySelector('[data-detail]').hidden,'an address restores its open item');
 check(document.querySelectorAll('[data-feedback-form]').length===6,'each row has feedback controls');
 check(document.documentElement.scrollWidth<=innerWidth,'the page has no horizontal overflow');
 if(innerWidth<960){
  check(getComputedStyle(visible()[0]).display==='block','a phone row is a card');
  const card=number=>rows().find(row=>row.dataset.number===number);
  const cell=(number,label,part=null)=>getComputedStyle(card(number).querySelector('td[data-label="'+label+'"]'),part);
  check(cell('105','Item').fontSize==='17px' && cell('105','Producer').fontSize!=='17px','a phone card shows the title largest');
  check(cell('105','Producer','::before').content.includes('Producer'),'a phone card labels another producer');
  check(cell('106','Producer').display==='none','a phone card leaves out the keeper as producer');
 }
}catch(error){failed.push(String(error));}
await nativeFetch('/results',{method:'POST',body:JSON.stringify([...failed,...errors])});
})();</script>'''.replace('OWNER', json.dumps(OWNER.id)).replace('DWIGHT', json.dumps(DWIGHT.id))
        for width in (1440, 390):
            with self.subTest(width=width):
                markup = list_page.render(self.root / "reviews").decode()
                setup_for_width = setup.replace('MARKUP', json.dumps(markup).replace('</', '<\\/'))
                page = markup.replace('<head>', '<head>' + setup_for_width, 1).replace('</body>', probe + '</body>').encode()
                root = self.root / "reviews"
                results: list[list[str]] = []
                complete = threading.Event()

                class Probe(BaseHTTPRequestHandler):
                    def do_GET(self) -> None:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        if self.path == '/fresh':
                            _ = self.wfile.write(list_page.render(root).decode().encode())
                        elif self.path == '/complete':
                            _ = complete.wait(40)
                        else:
                            _ = self.wfile.write(page if self.path == '/page' else b'<iframe style="border:0;width:100vw;height:100vh" src="/page"></iframe><img hidden src="/complete">')

                    def do_POST(self) -> None:
                        if self.path == '/feedback':
                            body = self.rfile.read(int(self.headers['Content-Length'])).decode()
                            delivery = list_page.submit(root, body)
                            list_page.deliver(delivery)
                            fields = parse_qs(body)
                            result = {"state": "sent", **list_page.fresh_request(root, fields["keeper"][0], fields["id"][0])}
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            _ = self.wfile.write(json.dumps(result).encode())
                            return
                        results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                        complete.set()
                        self.send_response(204)
                        self.end_headers()

                    @override
                    def log_message(self, format: str, *args: object) -> None:
                        pass

                with ThreadingHTTPServer(('127.0.0.1', 0), Probe) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        profile = self.root / f'profile-{width}'
                        profile.mkdir()
                        with patch.object(change_page, "send_to_staff") as sends:
                            done = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', str(profile),
                                '--window-size', f'{width},900', '--screenshot', str(self.root / f'page-{width}.png'),
                                f'http://127.0.0.1:{server.server_port}/'], capture_output=True, text=True, timeout=45)
                            sends.assert_called_once()
                        self.assertEqual(done.returncode, 0, done.stderr)
                    finally:
                        server.shutdown()
                        thread.join(5)
                self.assertEqual(results, [[]])


if __name__ == '__main__':
    _ = unittest.main()
