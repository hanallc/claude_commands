"""The /new_item command files through a fake console server and resolves each assignee."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, NoReturn, cast, override
from unittest import mock

from scripts.production import staff, the_list
from scripts.lib import main_publish
from scripts.review_links import for_you, new_item, new_item_dialog, review_page, review_submissions


def member(identity: str, kind: staff.Kind, name: str, parent: staff.StaffRow | None = None) -> staff.StaffRow:
    reports: staff.ReportsTo | staff.StaffRoot = staff.ReportsTo(parent.id) if parent is not None else staff.StaffRoot()
    return staff.StaffRow(staff.StaffId(identity), kind, name, reports, current_session=staff.StaffId(identity))


DWIGHT = member("920b0a64-6eee-479e-8c07-e03e4dcd4ef6", "producer", "dwight")
BOB = member("2c1f7d2e-5a43-4a3e-9d0b-6f1f2b9e8c11", "producer", "bob")
SPACETIME = member("a0882376-677b-4da5-adbb-f11db9146021", "showrunner", "spacetime")
MODELS = member("f5636030-6745-4ff4-aeda-4c2773d18da8", "unit", "models", SPACETIME)
MEMBERS = staff.StaffSnapshot((DWIGHT, BOB, SPACETIME, MODELS))
ADDRESS = for_you.ConsoleAddress("192.0.2.1", 8765)
LIST_KEY = f"{SPACETIME.id}/L3"
SET_UNIT = new_item.set_unit
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

    def exchange(self, _address: for_you.ConsoleAddress, method: Literal["GET", "POST"], target: str,
                 body: bytes = b"") -> new_item.ServerReply:
        if method == "GET":
            assert target == new_item.OPTIONS_TARGET
            return new_item.ServerReply(200, {"token": "fixture-token", "recipients": [
                {"id": SPACETIME.id, "name": SPACETIME.name, "proposal": True},
                {"id": DWIGHT.id, "name": DWIGHT.name, "proposal": False}]})
        assert target == review_page.NEW_ITEM_PATH
        self.posts.append(cast(dict[str, object], json.loads(body)))
        return new_item.ServerReply(200, {"id": "I7", "key": "fixture-key", "version": "1", "list_key": LIST_KEY})


class NewItemTests(unittest.TestCase):
    config: Path = Path()
    server: FakeServer = FakeServer()
    unit_updates: list[tuple[str, staff.StaffRow]] = []

    @override
    def setUp(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = folder / "producer_map.conf"
        _ = self.config.write_text("# Producer map\n", encoding="utf-8")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(folder)})))
        self.server = FakeServer()
        _ = self.enterContext(mock.patch.object(new_item, "exchange", side_effect=self.server.exchange))
        _ = self.enterContext(mock.patch.object(for_you, "console_address", return_value=ADDRESS))
        _ = self.enterContext(mock.patch("platform.system", return_value="Linux"))
        _ = self.enterContext(mock.patch.object(staff, "snapshot", return_value=MEMBERS))
        self.unit_updates = []

        def set_unit(list_key: str, unit: staff.StaffRow, members: staff.StaffSnapshot) -> None:
            self.assertIs(members, MEMBERS)
            self.unit_updates.append((list_key, unit))

        _ = self.enterContext(mock.patch.object(new_item, "set_unit", side_effect=set_unit))

    def map_row(self, row: staff.StaffRow, producer: staff.StaffRow) -> None:
        with self.config.open("a", encoding="utf-8") as stream:
            _ = stream.write(f"# {row.name} -> {producer.name}\n{row.id}={producer.id}\n")

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = new_item.main(list(arguments))
        return code, output.getvalue(), errors.getvalue()

    def test_no_arguments_print_the_usage(self) -> None:
        code, output, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn(f"Usage: {new_item.FORM}", output)
        self.assertIn("Types: bug, question, feedback, proposal, investigation.", output)
        self.assertIn("To row: spacetime, dwight.", output)
        self.assertIn("A unit name is allowed.", output)
        self.assertEqual(self.server.posts, [])

    def test_feedback_files_as_an_investigation(self) -> None:
        self.assertEqual(new_item.filed_kind("feedback"), new_item_dialog.SubmissionKind.INVESTIGATION)
        for kind in new_item_dialog.SubmissionKind:
            self.assertEqual(new_item.filed_kind(kind.value), kind)
        self.map_row(SPACETIME, DWIGHT)
        code, output, _ = self.run_main("Feedback", "spacetime", "The chart is slow.")
        self.assertEqual(code, 0)
        self.assertEqual(self.server.posts[0]["kind"], "investigation")
        self.assertEqual(output, f"Feedback I7 to spacetime; List {LIST_KEY}\n")
        self.assertEqual(self.unit_updates, [])

    def test_a_unit_files_to_its_showrunner_and_names_the_unit(self) -> None:
        options = new_item.read_options(ADDRESS)
        self.assertEqual(new_item.resolve("Models", options, MEMBERS),
                         new_item.Assignee(MODELS, new_item.Recipient(SPACETIME.id, SPACETIME.name)))
        self.map_row(SPACETIME, DWIGHT)
        code, output, _ = self.run_main("bug", "models", "Fix the chart.", "Today, before the merge.")
        self.assertEqual(code, 0)
        self.assertEqual(self.server.posts[0]["recipient_id"], SPACETIME.id)
        self.assertEqual(output, f"Bug I7 to models; List {LIST_KEY}\n")
        self.assertEqual(self.unit_updates, [(LIST_KEY, MODELS)])

    def test_an_unknown_name_is_refused_with_the_names(self) -> None:
        code, _, errors = self.run_main("bug", "nobody", "Fix the chart.")
        self.assertEqual(code, 2)
        self.assertIn("To row: spacetime, dwight. Active units: models.", errors)
        self.assertEqual(self.server.posts, [])

    def test_no_producer_exits_3_without_filing(self) -> None:
        code, output, _ = self.run_main("question", "models", "Which chart?")
        self.assertEqual(code, 3)
        self.assertEqual(output, "models has no producer. Pass spacetime to producer_map.py set. "
                         + "Active producers: dwight, bob.\n")
        self.assertEqual(self.server.posts, [])
        code, _, _ = self.run_main("question", "dwight", "Which chart?")
        self.assertEqual(code, 0)

    def test_a_server_that_does_not_answer_gives_one_error_line(self) -> None:
        with mock.patch.object(new_item, "exchange", side_effect=new_item.ServerUnavailable("the console server does not answer")):
            code, output, errors = self.run_main("bug", "spacetime", "Fix the chart.")
        self.assertEqual((code, output, errors), (1, "", "new_item: the console server does not answer\n"))
        lookup = subprocess.CalledProcessError(1, ["tailscale"])
        with mock.patch.object(for_you, "console_address", side_effect=lookup):
            code, output, errors = self.run_main("bug", "spacetime", "Fix the chart.")
        self.assertEqual((code, output), (1, ""))
        self.assertTrue(errors.startswith("new_item: cannot find the console server address: "), errors)

    def test_a_post_without_a_reply_says_to_check_the_list(self) -> None:
        with mock.patch("scripts.review_links.new_item.HTTPConnection", new=SilentConnection):
            cases: tuple[tuple[Literal["GET", "POST"], bool], ...] = (("POST", True), ("GET", False))
            for method, expected in cases:
                with self.subTest(method=method), self.assertRaises(new_item.ServerUnavailable) as caught:
                    _ = EXCHANGE(ADDRESS, method, review_page.NEW_ITEM_PATH, b"{}")
                self.assertEqual(new_item.MAY_BE_FILED in str(caught.exception), expected)

    def test_the_mac_forwards_the_whole_command_to_natedev_and_files_nothing(self) -> None:
        arguments = ["bug", "models", "Fix the chart.", "Today."]
        with (mock.patch("platform.system", return_value="Darwin"),
                mock.patch.dict(os.environ, {"ZONE_SSH": "fixture-ssh"}),
                mock.patch.object(main_publish, "forward", return_value=0) as forward):
            code, output, errors = self.run_main(*arguments)
        self.assertEqual((code, output, errors), (0, "", ""))
        forward.assert_called_once_with("review_links/new_item.py", arguments, label="new_item", ssh_program="fixture-ssh")
        self.assertEqual(self.server.posts, [])
        self.assertEqual(self.unit_updates, [])

    def test_post_body_has_the_window_fields(self) -> None:
        self.map_row(MODELS, BOB)
        code, _, _ = self.run_main("proposal", "models", "Add a chart.", "Next week.")
        self.assertEqual(code, 0)
        body = self.server.posts[0]
        request_id = body.pop("request_id")
        self.assertTrue(review_submissions.is_draft_id(request_id))
        self.assertEqual(body, {
            "token": "fixture-token", "kind": "proposal", "recipient_id": SPACETIME.id,
            "words": "Add a chart.\n\nNext week.", "evidence": {"kind": "none"}, "images": [],
            "page": "list", "page_recipient": "selected"})
        request = new_item_dialog.read_request({**body, "request_id": request_id})
        self.assertEqual((request.kind, request.recipient_id, request.page_recipient),
                         (new_item_dialog.SubmissionKind.PROPOSAL, SPACETIME.id, new_item_dialog.PageRecipient.SELECTED))

    def test_set_unit_names_the_unit_and_applies_a_unit_producer_row(self) -> None:
        item = cast(the_list.ListItem, cast(object, SimpleNamespace(producer=DWIGHT.id)))
        updates: list[tuple[staff.StaffId, str, the_list.ItemChanges, str]] = []

        def update(keeper: staff.StaffId, identity: str, changes: the_list.ItemChanges, *, source: str) -> the_list.ListItem:
            updates.append((keeper, identity, changes, source))
            return item

        with mock.patch.object(the_list, "load", return_value=item), mock.patch.object(the_list, "update", side_effect=update):
            self.map_row(SPACETIME, DWIGHT)
            SET_UNIT(LIST_KEY, MODELS, MEMBERS)
            self.map_row(MODELS, BOB)
            SET_UNIT(LIST_KEY, MODELS, MEMBERS)
        self.assertEqual(updates, [
            (SPACETIME.id, "L3", {"unit": "models"}, new_item.SOURCE),
            (SPACETIME.id, "L3", {"unit": "models", "producer": BOB.id}, new_item.SOURCE)])


if __name__ == "__main__":
    _ = unittest.main()
