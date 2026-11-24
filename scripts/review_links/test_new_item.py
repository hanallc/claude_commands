"""Test how /new_item sends a request and reports the List number."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal, NoReturn, cast, override
from unittest import mock
from urllib.parse import urlencode

from scripts.agents.remote import local_machine
from scripts.lib import conf, main_publish
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import staff, the_list
from scripts.production.staff_test_support import fixture_member
from scripts.review_links import for_you, new_item, new_item_dialog, review_page, review_submissions, test_serve

def member(identity: str, kind: staff.Kind, name: str, parent: staff.StaffRow | None = None) -> staff.StaffRow:
    reports: staff.ReportsTo | staff.StaffRoot = staff.ReportsTo(parent.id) if parent is not None else staff.StaffRoot()
    return staff.StaffRow(staff.StaffId(identity), kind, name, reports, current_session=staff.StaffId(identity))


DWIGHT = member("920b0a64-6eee-479e-8c07-e03e4dcd4ef6", "producer", "dwight")
BOB = member("2c1f7d2e-5a43-4a3e-9d0b-6f1f2b9e8c11", "producer", "bob")
SPACETIME = member("a0882376-677b-4da5-adbb-f11db9146021", "showrunner", "spacetime")
MODELS = member("f5636030-6745-4ff4-aeda-4c2773d18da8", "unit", "models", SPACETIME)
NATEDEV = member("96b016bf-87cf-4a19-af7a-77425852c2e2", "showrunner", "natedev")
POSTMASTER = member("e404d92e-8cc6-4714-952f-528c8de5cb7c", "unit", "postmaster", NATEDEV)
CARGO = member("4dbdb78e-a83b-4248-b8f9-ff278dcbc60c", "showrunner", "cargo-liner")
TILE = member("10000000-0000-4000-8000-000000000006", "unit", "tile", CARGO)
HIDDEN = member("10000000-0000-4000-8000-000000000007", "showrunner", "hidden")
CARGO_STYLE = member("10000000-0000-4000-8000-000000000008", "unit", "style", CARGO)
SPACETIME_STYLE = member("10000000-0000-4000-8000-000000000009", "unit", "style", SPACETIME)
MORTY = member("01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a", "producer", "morty")
ROWS = (DWIGHT, BOB, SPACETIME, MODELS, NATEDEV, POSTMASTER, CARGO, TILE, HIDDEN, CARGO_STYLE, SPACETIME_STYLE)
MEMBERS = staff.StaffSnapshot(ROWS, retired_producers=(MORTY,))
TO_ROW = (SPACETIME, NATEDEV, CARGO, DWIGHT, BOB)
ADDRESS = for_you.ConsoleAddress("192.0.2.1", 8765)
EXCHANGE = new_item.exchange


class SilentConnection:
    """Take the request, then give no reply."""

    def __init__(self, *_arguments: object, **_options: object) -> None:
        pass

    def request(self, *_arguments: object, **_options: object) -> None:
        pass

    def getresponse(self) -> NoReturn:
        raise TimeoutError("timed out")

    def close(self) -> None:
        pass


class FakeServer:
    """Answer the window's two requests without a socket."""

    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []
        self.reads: int = 0
        self.list_number: int | None = 3
        self.review_key: str | None = "fixture-key"

    def exchange(self, _address: for_you.ConsoleAddress, method: Literal["GET", "POST"], target: str,
                 body: bytes = b"") -> new_item.ServerReply:
        if method == "GET":
            assert target == new_item.OPTIONS_TARGET
            self.reads += 1
            return new_item.ServerReply(200, {"token": "fixture-token", "recipients": [
                {"id": row.id, "name": row.name, "proposal": row.kind != "producer"} for row in TO_ROW]})
        assert target == review_page.NEW_ITEM_PATH
        self.posts.append(cast(dict[str, object], json.loads(body)))
        fields: dict[str, object] = {"id": "I7", "version": "1"}
        if self.review_key is not None:
            fields["key"] = self.review_key
        if self.list_number is not None:
            fields["list_number"] = self.list_number
        return new_item.ServerReply(200, fields)


class ServerFixture(unittest.TestCase):
    """Patch the console server, the address and the producer map for each test."""

    config: Path = Path()
    folder: Path = Path()
    server: FakeServer = FakeServer()

    @override
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.folder / "producer_map.conf"
        _ = self.config.write_text("# The producer of a showrunner or of a unit.\n", encoding="utf-8")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(self.folder),
                                                                        "XDG_STATE_HOME": str(self.folder / "state")})))
        self.server = FakeServer()
        _ = self.enterContext(mock.patch.object(new_item, "exchange", side_effect=self.server.exchange))
        _ = self.enterContext(mock.patch.object(for_you, "console_address", return_value=ADDRESS))
        _ = self.enterContext(mock.patch("platform.system", return_value="Linux"))

    def map_row(self, row: staff.StaffRow, producer: staff.StaffRow) -> None:
        with self.config.open("a", encoding="utf-8") as stream:
            _ = stream.write(f"{row.id} = {producer.id}  # {row.name} = {producer.name}\n")

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = new_item.main([*arguments, "--session", "another-session"]) if arguments else new_item.main([])
        return code, output.getvalue(), errors.getvalue()


class NewItemTests(ServerFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch.object(staff, "snapshot", return_value=MEMBERS))

    def test_no_arguments_print_the_usage(self) -> None:
        code, output, _ = self.run_main()
        self.assertEqual(code, new_item.Outcome.USAGE)
        self.assertEqual(output.splitlines(), [
            "/new_item <type> <assignee> <description> [extra, such as a time]",
            "Types: bug, question, investigation, proposal, efficiency, feedback, enhancement",
            "Assignee: a producer, a showrunner or a unit",
        ])
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_seven_types_and_each_files_as_a_server_kind(self) -> None:
        self.assertEqual([kind.value for kind in new_item.ItemType],
                         ["bug", "question", "investigation", "proposal", "efficiency", "feedback", "enhancement"])
        investigation = new_item_dialog.SubmissionKind.INVESTIGATION
        self.assertEqual(new_item.FILED_KINDS[new_item.ItemType.FEEDBACK], investigation)
        self.assertEqual(new_item.FILED_KINDS[new_item.ItemType.EFFICIENCY], investigation)
        for kind in new_item_dialog.SubmissionKind:
            self.assertEqual(new_item.FILED_KINDS[new_item.ItemType(kind.value)], kind)
        for item_type in new_item.ItemType:
            with self.subTest(item_type=item_type):
                code, output, _ = self.run_main(item_type.value.upper(), "bob", "Check the chart.")
                self.assertEqual(code, 0)
                self.assertEqual(output, f"{item_type.value.capitalize()} \u2192 bob. List item 3.\n")
                self.assertEqual(self.server.posts[-1]["kind"], new_item.FILED_KINDS[item_type])
                self.assertEqual(self.server.posts[-1]["notice"], {"kind": item_type.value, "from_command": True})
                words = "Efficiency: Check the chart." if item_type == new_item.ItemType.EFFICIENCY else "Check the chart."
                self.assertEqual(self.server.posts[-1]["words"], words)
        with self.assertRaises(SystemExit):
            _ = self.run_main("chore", "bob", "Check the chart.")

    def test_a_producer_assignee_has_no_lookup(self) -> None:
        code, output, _ = self.run_main("bug", "bob", "A waiting row has no drag grip.")
        self.assertEqual((code, output), (0, "Bug \u2192 bob. List item 3.\n"))
        self.assertEqual(self.server.posts[0]["recipient_id"], BOB.id)
        self.assertEqual(self.server.posts[0]["list_assignee"], BOB.id)

    def test_a_showrunner_with_a_row_files_nate_extra_words_as_written(self) -> None:
        self.map_row(SPACETIME, DWIGHT)
        code, output, _ = self.run_main("question", "spacetime", "Can the server start with no saved world?",
                                        "Answer by Monday.")
        self.assertEqual(code, 0)
        self.assertEqual(output, "Question \u2192 spacetime. Producer: dwight (set for spacetime). List item 3.\n")
        self.assertEqual(self.server.posts[0]["words"], "Can the server start with no saved world?\n\nAnswer by Monday.")
        self.assertEqual(self.server.posts[0]["list_assignee"], SPACETIME.id)

    def test_a_unit_uses_its_showrunner_row(self) -> None:
        self.map_row(SPACETIME, DWIGHT)
        code, output, _ = self.run_main("bug", "models", "The frame time graph has a gap.")
        self.assertEqual(code, 0)
        self.assertEqual(output, "Bug \u2192 models, a unit of spacetime. Producer: dwight (from spacetime). List item 3.\n")
        self.assertEqual(self.server.posts[0]["recipient_id"], SPACETIME.id)
        self.assertEqual(self.server.posts[0]["list_assignee"], MODELS.id)

    def test_a_unit_row_wins_over_its_showrunner_row(self) -> None:
        self.map_row(NATEDEV, DWIGHT)
        self.map_row(POSTMASTER, BOB)
        code, output, _ = self.run_main("proposal", "postmaster", "One phone text for each nightly result.")
        self.assertEqual(code, 0)
        self.assertEqual(output,
                         "Proposal \u2192 postmaster, a unit of natedev. Producer: bob (set for postmaster). List item 3.\n")
        self.assertEqual(self.server.posts[0]["recipient_id"], NATEDEV.id)
        self.assertEqual(self.server.posts[0]["list_assignee"], POSTMASTER.id)

    def test_an_item_for_this_session_says_so(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = new_item.main(["feedback", "dwight", "The status of item 65 is old.", "--session", DWIGHT.id])
        self.assertEqual((code, output.getvalue()), (0, "Feedback \u2192 dwight, this session. List item 3.\n"))

    def test_a_showrunner_with_no_row_asks_and_files_nothing(self) -> None:
        code, output, _ = self.run_main("investigation", "cargo-liner", "Why does the tile test take 4 minutes?")
        self.assertEqual(code, new_item.Outcome.NO_PRODUCER)
        self.assertEqual(output, "cargo-liner has no producer yet. Which producer?\ndwight / bob\n")
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_unit_with_no_row_asks_about_both(self) -> None:
        code, output, _ = self.run_main("bug", "tile", "The tile demo clips the last column.")
        self.assertEqual(code, new_item.Outcome.NO_PRODUCER)
        self.assertEqual(output, "tile is a unit of cargo-liner. Neither has a producer yet. Which producer?\n"
                         + "dwight / bob\n")
        self.assertEqual(self.server.posts, [])

    def test_an_unknown_name_asks_and_files_nothing(self) -> None:
        code, output, _ = self.run_main("bug", "til", "The demo clips the last column.")
        self.assertEqual(code, new_item.Outcome.UNKNOWN_NAME)
        self.assertEqual(output, 'No session has the name "til". Do you mean tile, a unit of cargo-liner? yes / a different name\n')
        code, output, _ = self.run_main("bug", "qqqq", "The demo clips the last column.")
        self.assertEqual((code, output), (new_item.Outcome.UNKNOWN_NAME, 'No session has the name "qqqq".\n'))
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_name_with_many_matches_asks_which_one(self) -> None:
        code, output, _ = self.run_main("bug", "style", "The demo clips the last column.")
        self.assertEqual(code, new_item.Outcome.UNKNOWN_NAME)
        self.assertEqual(output, 'More than one session has the name "style": cargo-liner/style, spacetime/style. Which one?\n')
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_guess_that_many_sessions_share_asks_which_one(self) -> None:
        code, output, _ = self.run_main("bug", "styl", "The demo clips the last column.")
        self.assertEqual(code, new_item.Outcome.UNKNOWN_NAME)
        self.assertEqual(output, 'No session has the name "styl". More than one session has the name "style": '
                         + "cargo-liner/style, spacetime/style. Which one?\n")
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_stale_row_asks_for_its_holder_and_files_nothing(self) -> None:
        self.map_row(NATEDEV, DWIGHT)
        self.map_row(POSTMASTER, MORTY)
        code, output, _ = self.run_main("proposal", "postmaster", "One phone text for each nightly result.")
        self.assertEqual(code, new_item.Outcome.NO_PRODUCER)
        self.assertEqual(output, "The row of postmaster names morty, who is not active. Which producer for postmaster?\n"
                         + "dwight / bob\n")
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_member_with_no_to_row_owner_is_refused(self) -> None:
        self.map_row(HIDDEN, DWIGHT)
        code, _, errors = self.run_main("bug", "hidden", "Fix the chart.")
        self.assertEqual(code, new_item.Outcome.REFUSED)
        self.assertIn("hidden: neither it nor an owner above it is in the To row: spacetime, natedev, cargo-liner, dwight, bob",
                      errors)
        self.assertEqual(self.server.posts, [])

    def test_a_server_that_does_not_answer_gives_one_error_line(self) -> None:
        self.map_row(SPACETIME, DWIGHT)
        with mock.patch.object(new_item, "exchange", side_effect=new_item.ServerUnavailable("the console server does not answer")):
            code, output, errors = self.run_main("bug", "spacetime", "Fix the chart.")
        self.assertEqual((code, output, errors), (1, "", "new_item: the console server does not answer\n"))
        lookup = subprocess.CalledProcessError(1, ["tailscale"])
        with mock.patch.object(for_you, "console_address", side_effect=lookup):
            code, output, errors = self.run_main("bug", "spacetime", "Fix the chart.")
        self.assertEqual((code, output), (1, ""))
        self.assertTrue(errors.startswith("new_item: cannot find the console server address: "), errors)

        self.map_row(POSTMASTER, MORTY)
        cases = (
            ("cargo-liner", "cargo-liner has no producer yet. Which producer?"),
            ("postmaster", "The row of postmaster names morty, who is not active. Which producer for postmaster?"),
        )
        for assignee, question in cases:
            with self.subTest(assignee=assignee), mock.patch.object(for_you, "console_address", side_effect=lookup):
                result = self.run_main("bug", assignee, "Fix the chart.")
                self.assertEqual(result, (new_item.Outcome.NO_PRODUCER, f"{question}\ndwight / bob\n", ""))
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_post_without_a_reply_says_to_check_the_list(self) -> None:
        with mock.patch("scripts.review_links.new_item.HTTPConnection", new=SilentConnection):
            cases: tuple[tuple[Literal["GET", "POST"], bool], ...] = (("POST", True), ("GET", False))
            for method, expected in cases:
                with self.subTest(method=method), self.assertRaises(new_item.ServerUnavailable) as caught:
                    _ = EXCHANGE(ADDRESS, method, review_page.NEW_ITEM_PATH, b"{}")
                self.assertEqual(new_item.MAY_BE_FILED in str(caught.exception), expected)

    def test_the_mac_forwards_the_command_and_its_session_to_natedev(self) -> None:
        arguments = ["bug", "models", "Fix the chart.", "Today."]
        output = io.StringIO()
        with (mock.patch("platform.system", return_value="Darwin"),
                mock.patch.dict(os.environ, {"ZONE_SSH": "fixture-ssh", "CODEX_THREAD_ID": "",
                                             "CLAUDE_CODE_SESSION_ID": "mac-session"}),
                mock.patch.object(main_publish, "forward", return_value=0) as forward, redirect_stdout(output)):
            self.assertEqual(new_item.main(arguments), 0)
            self.assertEqual(new_item.main([]), new_item.Outcome.USAGE)
        forward.assert_called_once_with("review_links/new_item.py", [*arguments, "--session", "mac-session"],
                                        label="new_item", ssh_program="fixture-ssh")
        self.assertEqual(output.getvalue(), new_item.usage() + "\n")
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_post_body_has_the_window_fields(self) -> None:
        self.map_row(MODELS, BOB)
        code, _, _ = self.run_main("proposal", "models", "Add a chart.", "Next week.")
        self.assertEqual(code, 0)
        body = self.server.posts[0]
        request_id = body.pop("request_id")
        self.assertTrue(review_submissions.is_draft_id(request_id))
        self.assertEqual(body, {
            "token": "fixture-token", "kind": "proposal", "recipient_id": SPACETIME.id,
            "notice": {"kind": "proposal", "from_command": True},
            "words": "Add a chart.\n\nNext week.", "evidence": {"kind": "none"}, "images": [],
            "page": "list", "page_recipient": "selected", "list_assignee": MODELS.id})
        request = new_item_dialog.read_request({**body, "request_id": request_id})
        self.assertEqual((request.kind, request.recipient_id, request.page_recipient),
                         (new_item_dialog.SubmissionKind.PROPOSAL, SPACETIME.id, new_item_dialog.PageRecipient.SELECTED))


    def test_a_reply_without_a_list_number_reports_the_saved_review_item(self) -> None:
        self.server.list_number = None
        code, output, errors = self.run_main("bug", "bob", "A waiting row has no drag grip.")
        self.assertEqual((code, errors), (new_item.Outcome.FILED, ""))
        self.assertEqual(output, "Bug → bob. Filed as I7. The console server returned no List number.\n")

    def test_enhancement_with_no_producer_saves_nothing(self) -> None:
        code, output, errors = self.run_main("enhancement", "tile", "Show the next check in the row.")
        self.assertEqual((code, errors), (new_item.Outcome.NO_PRODUCER, ""))
        self.assertIn("Which producer?", output)
        self.assertEqual((self.server.reads, self.server.posts), (0, []))
        drafts = (self.folder / "state/new-item").glob("*")
        self.assertEqual([draft for draft in drafts if draft.suffix != ".lock"], [])

    def test_a_reply_without_a_review_key_reports_the_list_number(self) -> None:
        self.server.review_key = None
        code, output, errors = self.run_main("bug", "bob", "A waiting row has no drag grip.")
        self.assertEqual((code, output, errors), (new_item.Outcome.FILED, "Bug → bob. List item 3.\n", ""))

    def test_a_reply_without_a_review_key_or_list_number_exits_filed(self) -> None:
        self.server.review_key = ""
        self.server.list_number = None
        code, output, errors = self.run_main("bug", "bob", "A waiting row has no drag grip.")
        self.assertEqual((code, errors), (new_item.Outcome.FILED, ""))
        self.assertEqual(output, "Bug → bob. Filed as I7. The console server returned no List number.\n")

    def test_a_list_write_failure_after_filing_keeps_the_draft(self) -> None:
        def exchange(address: for_you.ConsoleAddress, method: Literal["GET", "POST"], target: str,
                     body: bytes = b"") -> new_item.ServerReply:
            reply = self.server.exchange(address, method, target, body)
            if method == "POST" and len(self.server.posts) <= 2:
                return new_item.ServerReply(503, {"error": "No space left on device"})
            return reply

        arguments = ("bug", "bob", "A waiting row has no drag grip.")
        with mock.patch.object(new_item, "exchange", side_effect=exchange):
            code, output, errors = self.run_main(*arguments)
            self.assertEqual((code, output), (new_item.Outcome.FAILED, ""))
            self.assertIn("No space left on device", errors)
            self.assertEqual(self.run_main(*arguments),
                             (new_item.Outcome.FILED, "Bug → bob. List item 3.\n", ""))
        self.assertEqual(len(self.server.posts), 3)
        self.assertEqual(self.server.posts, [self.server.posts[0]] * 3)


class NewItemServerTests(unittest.TestCase):
    def test_identical_words_create_separate_list_rows_and_review_links(self) -> None:
        server = test_serve.ReviewLinksServerTests()
        self.addCleanup(server.doCleanups)
        server.setUp()
        case = server.review_fixture()
        document = server.root.parent / "bob"
        document.mkdir()
        bob = fixture_member("producer", name="bob", doc=staff.LiveDoc(local_machine(), document))
        morty = fixture_member("producer", name="morty")
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={morty.id}\n")
        _ = conf.path("producer_map").write_text("# Producers use their own rows.\n", encoding="utf-8")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        address = for_you.ConsoleAddress("127.0.0.1", server.port)
        output, errors = io.StringIO(), io.StringIO()
        words = "A waiting row has no drag grip."
        arguments = ["bug", bob.name, words, "--session", "another-session"]
        with (mock.patch.object(server.server, "review_cache", cache),
              mock.patch.object(server.server, "delivery_status", case.status),
              mock.patch.object(server.server.RequestHandlerClass, "log_request"),
              mock.patch.object(for_you, "console_address", return_value=address),
              mock.patch("platform.system", return_value="Linux"),
              redirect_stdout(output), redirect_stderr(errors)):
            for _ in range(2):
                self.assertEqual(new_item.main(arguments), new_item.Outcome.FILED, errors.getvalue())
                self.assertTrue(server.new_item_posts.wait_until_none_run())
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(output.getvalue().splitlines(), ["Bug → bob. List item 1.", "Bug → bob. List item 2."])
        first, second = the_list.records(morty.id, include_closed=True)
        self.assertEqual((first.number, second.number), (1, 2))
        first_key, = first.reviews
        second_key, = second.reviews
        self.assertNotEqual(first_key, second_key)
        reviews = cache.get().records
        for item, key in ((first, first_key), (second, second_key)):
            self.assertEqual((item.owner, item.producer, item.unit, item.title), (bob.id, bob.id, "", words))
            self.assertIn(urlencode({"jump": key}), item.evidence)
            self.assertEqual((reviews[key].owner, reviews[key].title), (bob.id, words))


if __name__ == "__main__":
    _ = unittest.main()
