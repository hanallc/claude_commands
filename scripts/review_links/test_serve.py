"""HTTP behavior on localhost and retention in disposable snapshot folders."""
from __future__ import annotations

from scripts.lint.memory_test_support import write_config_fixture

from scripts.lib.checkout_test_support import python_tool_source

import fcntl
import base64
import hashlib
import importlib.util
import json
import gzip
import os
import secrets
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
from http.client import HTTPConnection, RemoteDisconnected, parse_headers
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch


from typing import TextIO, cast, override
from urllib.parse import urlencode, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo


from scripts.aws_target import aws_page, machines_page
from scripts.agents import remote
from scripts.agents.remote import local_machine
from scripts.build_queue import kill_action, memory_feed
from scripts.for_you.action import ActionStore
from scripts.message import send_result
from scripts.review_links.test_page_question import question_outcome
from scripts.review_links.server_test_support import started_review_server
from scripts.production import (
    change_review, change_routing, delivery_state, investigation, next_item, proposal_issue,
    record_store, staff, the_list, tidy_wrapped, update_registration,
)
from scripts.production.production_doc import read_new_production
from scripts.production.staff_test_support import fixture_member
from scripts.review_links import test_change_page
from scripts.review_links import console_tune, new_item_dialog, page_frame, page_question, question_record, reply_tokens, review_submissions
from scripts.review_links import accounts_page, change_browser, change_page, console, for_you, image_store, images, investigations_page, list_page, new_item, placement_disk, review_actions, review_later, review_page, review_items, serve, test_review_page
from scripts.lib import atomic_file, conf, file_lock, process_start, scratch, urgency
from scripts.feature_log import feature_log
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.statusline import rebuild_row
from scripts.whoami import usage_store


ROLE_COLOR_CONFIG = (
    "role.showrunner=#a379fd\n"
    "role.unit-director=#ffaf02\n"
    "role.producer=#90fe42\n"
    "role.worker=#ff0404\n"
)


@dataclass(frozen=True)
class HttpReply:
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict[str, str], compare=False)


def fake_firefox(tool: str) -> str | None:
    """Allow the capture tests to run without Firefox."""
    return "/test/firefox" if tool == "firefox" else find_executable(tool)


find_executable = shutil.which
FIXTURE_CAPTURE = new_item_dialog.PageCaptureRequest("/console/review.html?show=all", 876, 640, True)


def setUpModule() -> None:
    configuration = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = write_config_fixture(configuration / "change_review.conf")
    _ = write_config_fixture(configuration / "review_links.conf",
        "code_check_seconds=5\ntailscale_check_seconds=60\nsnapshot_sweep_seconds=3600\n")
    _ = write_config_fixture(configuration / "console.conf",
        "page.review.title=Review\npage.review.url=python:scripts.review_links.review_page:page_url\n"
        + "page.review.items=python:scripts.review_links.review_page:waiting_items\n"
        + "page.list.items=python:scripts.review_links.list_page:waiting_updates\n"
        + "rebuilds.url=python:scripts.review_links.change_page:rebuilds_url\n")
    _ = write_config_fixture(configuration / "scratch.conf", "record_lock_attempts=3\n")
    _ = (configuration / "statusline.conf").write_text(ROLE_COLOR_CONFIG, encoding="utf-8")
    _ = cast(object, unittest.enterModuleContext(patch.dict(os.environ, {
        "CONF_DIR": str(configuration), "USER_ZONE": "America/New_York"})))


@contextmanager
def capture_sender(name: str) -> Generator[Mock]:
    """Capture the recipient, sender, and message of a keyed send."""
    send = Mock()

    def capture(recipient: str, sender: str, message: str, **_options: str) -> None:
        _ = cast(object, send(recipient, sender, message))

    with patch.object(change_page, name, new=capture):
        yield send


class HandlerCalls:
    """Count the handler calls in progress, so a test can wait until each one ends."""

    def __init__(self) -> None:
        self._running: int = 0
        self._changed: threading.Condition = threading.Condition()

    @contextmanager
    def running(self) -> Generator[None]:
        with self._changed:
            self._running += 1
        try:
            yield
        finally:
            with self._changed:
                self._running -= 1
                self._changed.notify_all()

    def wait_until_none_run(self) -> bool:
        """A late end of an earlier call cannot release this wait, as an Event set could."""
        with self._changed:
            return self._changed.wait_for(lambda: self._running == 0, TEST_HANG_GUARD_SECS)


class ReviewLinksServerTests(unittest.TestCase):
    root: Path = Path()
    outside: Path = Path()
    port: int = 0
    _server: serve.ReviewLinksServer | None = None

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.review_posts: HandlerCalls = HandlerCalls()
        self.new_item_posts: HandlerCalls = HandlerCalls()

    @property
    def server(self) -> serve.ReviewLinksServer:
        assert self._server is not None
        return self._server

    @override
    def setUp(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-server-test-")))
        self.root = temporary / "root"
        self.root.mkdir()
        _ = self.enterContext(patch.object(reply_tokens, "platform", "linux"))
        _ = self.enterContext(patch.object(usage_store, "store_file", return_value=temporary / "usage.json"))
        staff_file = temporary / "staff.json"
        _ = staff_file.write_text('{"schema":2,"rows":[]}', encoding="utf-8")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "SHOWRUNNER_STATE_DIR": str(temporary / "investigations-state"),
            "CHANGE_REVIEW_DIR": str(temporary / "change-reviews"),
            "NEXT_STATE_DIR": str(temporary / "next"),
            "FOR_YOU_ACTIONS_DIR": str(temporary / "for-you-actions"),
            "REBUILD_NEEDS_DIR": str(temporary / "rebuild-needs"),
            "XDG_STATE_HOME": str(temporary / "key-state"),
            "STAFF_FILE": str(staff_file)})))
        self.outside = temporary / "outside.txt"
        _ = self.outside.write_text("outside the served root", encoding="utf-8")
        # The startup tests exercise code scans and recovery separately.
        with (patch.object(serve, "loaded_module_stamps", return_value={}),
              patch.object(change_page, "recover_pending"),
              patch.object(list_page, "recover_pending")):
            server = self.enterContext(started_review_server(("127.0.0.1", 0), self.root))
        server.review_cache.synchronize()
        self._server = server
        review_posts = self.review_posts
        new_item_posts = self.new_item_posts

        class ReviewFixtureHandler(serve.ReviewLinksHandler):
            @override
            def review_post(self) -> None:
                with review_posts.running():
                    super().review_post()

            @override
            def new_item_post(self) -> None:
                with new_item_posts.running():
                    super().new_item_post()

        server.RequestHandlerClass = ReviewFixtureHandler
        self.port = server.server_port
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)

    def review_fixture(self, *, changes: int = 0, feature_project: bool = False) -> test_review_page.ReviewPageTests:
        case = test_review_page.ReviewPageTests('runTest')
        case.fixture = test_change_page.ChangePageTests("runTest")
        self.addCleanup(case.doCleanups)
        case.addCleanup(case.fixture.doCleanups)
        # The fixture list is outside a Git checkout.
        _ = case.enterContext(patch("scripts.production.change_review._commit"))
        project = 'claude' if feature_project else None
        _ = case.enterContext(patch.object(feature_log, "project_of", return_value=project))
        case.fixture.set_up_review(changes=changes)
        case.configure_review()
        return case

    def list_keeper_item(self, title: str) -> tuple[staff.StaffRow, the_list.ListItem]:
        """Add an item through a morty row that only a patched staff snapshot names."""
        morty = staff.StaffRow(staff.StaffId("fixture-morty"), "producer", "morty", staff.StaffRoot(),
                               current_session=staff.StaffId("fixture-morty-session"))
        with patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((morty,))):
            item = the_list.add(morty.id, title, owner=the_list.UnassignedOwner.NATE, source="nate", changes={})
        return morty, item

    def propose_feature(self, key: str) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
            result = feature_log.main(['propose', '--review', key, '--project', 'claude',
                '--title', 'Numbered moves', '--kind', 'feature', '--summary', 'Numbered moves',
                '--behavior', 'Each group starts at 1', '--see', 'Numbers start at 1',
                '--link', '1'*40, '--evidence', 'The page test passes', '--completed', '2026-11-01'])
        self.assertEqual((result, errors.getvalue()), (0, ''))

    def new_item_http(self, body: bytes | None = None, *, query: str = '',
                      headers: dict[str, str] | None = None, finished: bool = False) -> HttpReply:
        connection = HTTPConnection('127.0.0.1', self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            connection.request('GET' if body is None else 'POST', review_page.NEW_ITEM_PATH + query,
                               body, {'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse()
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            return HttpReply(response.status, response.getheader('Content-Type', ''), response.read())
        finally:
            connection.close()
            if finished:
                self.assertTrue(self.new_item_posts.wait_until_none_run())

    def new_item_body(self, request: new_item_dialog.NewItemRequest) -> bytes:
        return json.dumps({**asdict(request), "token": self.server.new_item_token},
                          ensure_ascii=False, separators=(",", ":")).encode()

    def test_remote_staff_reads_reuse_the_snapshot_until_it_expires(self) -> None:
        case = self.review_fixture(changes=1)
        change = next(item for item in review_items.read(case.root) if item.type == review_items.ReviewType.CHANGE)
        keeper = fixture_member("producer", name="morty").id
        item = the_list.add(keeper, "Read the published review", owner=the_list.UnassignedOwner.NATE,
                            source="nate", changes={})
        _ = the_list.link(keeper, item.id, (change.key,), source="nate")
        seconds = conf.read("review_links").number("staff_snapshot_seconds")
        for mode, expected in (
            ("owner", ((200, 1), (200, 2), (200, 3))),
            ("remote", ((200, 1), (200, 1), (200, 2))),
            ("remote failure", ((503, 2), (503, 3), (200, 4))),
        ):
            self.server.clear_staff_snapshot()
            now = 100.0
            delay = seconds / 2 if mode == "remote failure" else 0
            finished = now + delay
            reads = 0

            def read() -> staff.StaffSnapshot:
                nonlocal now, reads
                reads += 1
                if mode == "remote failure" and now < finished + seconds:
                    if reads == 1:
                        now += delay
                    raise staff.StaffUnreadable("The staff authority is unavailable.")
                return staff.local_snapshot()

            with (self.subTest(mode=mode),
                  patch.object(staff, "is_owner", lambda: mode == "owner"),
                  patch.object(staff, "authority_snapshot", read),
                  patch.object(self.server, "verified_tailscale_address", self.server.bound_address),
                  patch.object(time, "monotonic", lambda: now)):
                for at, (status, count) in zip((now, finished + seconds * 0.75, finished + seconds + 1), expected):
                    now = at
                    self.assertEqual(self.new_item_http().status, status)
                    self.assertEqual(reads, count)
                    if mode == "remote" and now == finished:
                        for query in (list_page.COUNTS_QUERY, list_page.COUNTS_AND_LINKS_QUERY):
                            with self.subTest(query=query), patch.object(self.server, "root", case.root):
                                response = self.request(f"{list_page.PAGE_PATH}?{query}")
                                self.assertEqual(response.status, 200)
                                if query == list_page.COUNTS_AND_LINKS_QUERY:
                                    self.assertIn(f">{change.id}</a>".encode(), response.body)
                                self.assertEqual(reads, count)

    def test_post_discards_the_cached_staff_snapshot(self) -> None:
        configuration = self.root.parent / "cache-config"
        _ = shutil.copytree(conf.path("review_links").parent, configuration)
        _ = write_config_fixture(configuration / "disks.conf", f"output_folder={self.root / 'disks'}\n")
        _ = write_config_fixture(configuration / "memory_feed.conf", f"output_folder={self.root / 'builds'}\n")
        complete = threading.Event()
        reads: list[staff.StaffSnapshot] = []
        during_post: list[tuple[int, int]] = []
        get_recipients = self.new_item_http

        class StaffCacheFixtureHandler(serve.ReviewLinksHandler):
            @override
            def do_POST(self) -> None:
                try:
                    super().do_POST()
                finally:
                    complete.set()

            @override
            def new_item_post(self) -> None:
                during_post.append((get_recipients().status, len(reads)))
                super().new_item_post()

        def read() -> staff.StaffSnapshot:
            members = staff.local_snapshot()
            reads.append(members)
            return members

        def post_action(path: str, body: str) -> int:
            complete.clear()
            status = self.post(path, body)[0]
            self.assertTrue(complete.wait(TEST_HANG_GUARD_SECS))
            return status

        def new_item() -> int:
            self.server.clear_staff_snapshot()
            before = len(reads)
            complete.clear()
            status = self.new_item_http(b"{").status
            self.assertTrue(complete.wait(TEST_HANG_GUARD_SECS))
            self.assertEqual(during_post, [(200, before + 1)])
            return status

        with (patch.dict(os.environ, {"CONF_DIR": str(configuration),
                                      "REVIEW_LINKS_ROOT": str(self.root), "REVIEW_LINKS_HOST": "127.0.0.1",
                                      "XDG_STATE_HOME": str(self.root.parent / "state")}),
              patch.object(accounts_page, "TUNE", (sys.executable, "-c", "pass")),
              patch.object(self.server, "RequestHandlerClass", StaffCacheFixtureHandler),
              patch.object(self.server, "verified_tailscale_address", self.server.bound_address)):
            cache = self.server.review_cache
            cache.invalidate()
            cache.synchronize()
            previous = cache.get()
            with (patch.object(staff, "is_owner", lambda: False),
                  patch.object(staff, "authority_snapshot", read),
                  patch.object(time, "monotonic", lambda: 100.0)):
                self.assertEqual(reads, [])
                self.assertEqual(self.new_item_http().status, 200)
                self.assertEqual(len(reads), 1)
                for action, submit, status, additional_reads in (
                    ("console", lambda: self.request("/console/").status, 200, 0),
                    ("news", partial(post_action, "/console/news", "open=review"), 200, 0),
                    ("Accounts color", partial(post_action, accounts_page.PAGE_PATH, "key=role.worker&value=%23010203"), 200, 0),
                    ("new item", new_item, 400, 2),
                ):
                    with self.subTest(action=action):
                        before = len(reads)
                        self.assertEqual(submit(), status)
                        self.assertEqual(self.new_item_http().status, 200)
                        count = before + additional_reads
                        self.assertEqual(len(reads), count)
                        for _ in range(2):
                            cache.invalidate()
                            cache.synchronize()
                            current = cache.get()
                            self.assertIsNot(current, previous)
                            self.assertEqual(len(reads), count)
                            self.assertIs(current.members, reads[-1])
                            previous = current
                        self.assertEqual(self.new_item_http().status, 200)
                        self.assertEqual(len(reads), count)

    def test_new_item_options_and_refusals(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        options = self.new_item_http()
        self.assertEqual(options.status, 200)
        fields = cast(dict[str, object], json.loads(options.body))
        self.assertEqual(fields['recipients'], [{'id': owner.id, 'name': owner.name, 'proposal': True}])
        token = fields['token']
        request = case.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner.id)
        payload = {**asdict(request), 'token': token}

        def encoded(fields: dict[str, object]) -> bytes:
            return json.dumps(fields).encode()

        with patch.object(self.server, 'review_cache', cache), patch.object(self.server, 'delivery_status', case.status):
            expired = self.new_item_http(encoded({**payload, 'token': 'different'}))
            self.assertEqual(expired.status, 403)
            self.assertEqual(json.loads(expired.body), {
                'code': 'stale_token', 'error': 'The New item token expired. Try Send again.'})
            cases: tuple[tuple[bytes, str, dict[str, str], int], ...] = (
                (encoded(payload), '?extra=1', {}, 400),
                (encoded(payload), '', {'Content-Type': 'text/plain'}, 400),
                (encoded(payload), '', {'Origin': 'https://elsewhere.invalid'}, 403),
                (b'\xff', '', {}, 400),
                (b'{', '', {}, 400),
                (encoded({**payload, 'images': 'invalid'}), '', {}, 400),
                (encoded(payload), '', {'Content-Length': str(serve.MAX_NEW_ITEM_BODY+1)}, 400),
            )
            for body, query, headers, status in cases:
                with self.subTest(code=status, query=query, headers=headers):
                    response = self.new_item_http(body, query=query, headers=headers)
                    self.assertEqual(response.status, status, response.body)
                    self.assertEqual(set(cast(dict[str, object], json.loads(response.body))), {'error'})
            with patch.object(self.server, 'verified_tailscale_address', '127.0.0.2'):
                self.assertEqual(self.new_item_http(encoded(payload)).status, 403)
            original = case.fixture.production.read_text()
            _ = case.fixture.production.write_text(original.replace('running.**', 'stopped.**'))
            self.assertEqual(cast(dict[str, object], json.loads(self.new_item_http().body))['recipients'], [])
            self.assertEqual(self.new_item_http(encoded(payload)).status, 409)
            _ = case.fixture.production.write_text(original)
            with patch.object(review_submissions, 'submit', side_effect=OSError('The native store is unavailable.')):
                self.assertEqual(self.new_item_http(encoded(payload)).status, 503)
            self.assertEqual(cache.get().records, {})

    def test_new_item_options_refuse_an_unpublished_host(self) -> None:
        host = f"other.invalid:{self.port}"
        with (patch.object(self.server, "verified_tailscale_address", self.server.bound_address),
              patch.object(staff, "is_owner", return_value=True),
              patch.dict(os.environ, {"REVIEW_LINKS_HOST": ""})):
            reply = self.new_item_http(headers={"Host": host, "Origin": f"http://{host}"})
        self.assertEqual((reply.status, json.loads(reply.body)),
                         (403, {"error": "Open the dialog from a console page owner."}))

    def test_new_item_options_accept_the_bound_and_published_hosts_on_the_serving_port(self) -> None:
        with (patch.object(self.server, "verified_tailscale_address", self.server.bound_address),
              patch.object(staff, "is_owner", return_value=True),
              patch.dict(os.environ, {"REVIEW_LINKS_HOST": "console.invalid."})):
            for host in (self.server.bound_address, "console.invalid"):
                with self.subTest(host=host):
                    authority = f"{host}:{self.port}"
                    reply = self.new_item_http(headers={"Host": authority, "Origin": f"http://{authority}"})
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(cast(dict[str, object], json.loads(reply.body))["token"], self.server.new_item_token)
                    wrong_port = f"{host}:0"
                    self.assertEqual(self.new_item_http(headers={"Host": wrong_port}).status, 403)
                    self.assertEqual(self.new_item_http(headers={"Host": host}).status, 403)

    def test_new_item_options_accept_bare_hosts_on_port_80(self) -> None:
        with (patch.object(self.server, "server_port", 80),
              patch.object(self.server, "verified_tailscale_address", self.server.bound_address),
              patch.object(staff, "is_owner", return_value=True),
              patch.dict(os.environ, {"REVIEW_LINKS_HOST": "console.invalid."})):
            for host in (self.server.bound_address, "console.invalid"):
                with self.subTest(host=host):
                    reply = self.new_item_http(headers={"Host": host, "Origin": f"http://{host}"})
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(cast(dict[str, object], json.loads(reply.body))["token"], self.server.new_item_token)

    def test_new_item_options_require_a_host(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        self.addCleanup(connection.close)
        with (patch.object(self.server, "verified_tailscale_address", self.server.bound_address),
              patch.object(staff, "is_owner", return_value=True)):
            connection.putrequest("GET", review_page.NEW_ITEM_PATH, skip_host=True)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual((response.status, json.loads(response.read())),
                             (403, {"error": "Open the dialog from a console page owner."}))

    def test_review_owner_button_mounts_one_dialog_with_the_full_recipient_set(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        other = fixture_member("producer", name="another-recipient")
        builds_doc = self.root / "builds.md"
        _ = builds_doc.write_text("# Builds\n> **Status: PRODUCTION — running.**\n")
        builds = fixture_member("showrunner", name="builds", doc=staff.LiveDoc(local_machine(), builds_doc))
        aws = fixture_member("unit", name="aws", parent=builds.id)
        config = conf.path("console")
        _ = write_config_fixture(config, f"page.review.owner={owner.id}\npage.aws_machines.owner={aws.id}\n"
                                 + f"page.list.owner={other.id}\npage.review.label=Review\n"
                                 + "page.aws_machines.label=AWS Machines\npage.list.label=List\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        with patch.object(self.server, "review_cache", cache):
            review = self.request(review_page.PAGE_PATH)
            connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
            self.addCleanup(connection.close)
            with patch.object(gzip, "compress", wraps=gzip.compress) as compress:
                for _ in range(2):
                    connection.request("GET", review_page.PAGE_PATH, headers={"Accept-Encoding": "gzip"})
                    response = connection.getresponse()
                    self.assertEqual((response.status, response.getheader("Content-Encoding")), (200, "gzip"))
                    self.assertEqual(gzip.decompress(response.read()), review.body)
                compress.assert_called_once()
        expected = [{"id": owner.id, "name": owner.name, "proposal": True},
                    {"id": builds.id, "name": builds.name, "proposal": False},
                    {"id": other.id, "name": other.name, "proposal": False}]
        for page, default, reason in (
                ("review", owner, f"(why {owner.name}? because {owner.name} owns the Review page!)"),
                ("aws_machines", builds,
                 "(why builds? because aws owns the AWS Machines page and builds is its showrunner!)"),
                ("list", other, f"(why {other.name}? because {other.name} owns the List page!)")):
            with self.subTest(page=page):
                options = self.new_item_http(query=f"?page={page}")
                fields = cast(dict[str, object], json.loads(options.body))
                self.assertEqual(options.status, 200)
                self.assertEqual((fields["page_owner"], fields["page_owner_reason"]), (default.id, reason))
                self.assertCountEqual(cast(list[dict[str, object]], fields["recipients"]), expected)
        self.assertEqual(self.new_item_http(query="?page=other").status, 400)
        self.assertEqual(review.status, 200)
        self.assertIn(b'data-page-owner="review"', review.body)
        self.assertNotIn(b'data-review-new', review.body)
        self.assertEqual(review.body.count(b'data-owner-new aria-label='), 1)
        self.assertEqual(review.body.count(b'<dialog id="review-new-item"'), 1)

    def test_review_has_a_launcher_without_a_default_recipient(self) -> None:
        case = self.review_fixture()
        other = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"),
                                 "page.review.owner=missing-owner\nowner.default=missing-default\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        with patch.object(self.server, "review_cache", cache):
            review = self.request(review_page.PAGE_PATH)
            options = self.new_item_http(query="?page=review")
        self.assertEqual(review.status, 200)
        self.assertEqual(review.body.count(b'data-owner-new aria-label="New item"'), 1)
        self.assertEqual(review.body.count(b'<dialog id="review-new-item"'), 1)
        self.assertEqual(options.status, 200)
        fields = cast(dict[str, object], json.loads(options.body))
        self.assertEqual((fields["page_owner"], fields["page_owner_reason"]), ("", ""))
        self.assertEqual(fields["recipients"], [{"id": other.id, "name": other.name, "proposal": True}])

    def test_page_proposal_refuses_the_named_recipient_without_a_native_folder(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        other = fixture_member("producer", name="another-recipient")
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.PROPOSAL, other.id), page="review")
        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              capture_sender("send_message") as send):
            response = self.new_item_http(self.new_item_body(request), finished=True)
            send.assert_not_called()
        self.assertEqual(response.status, 400, response.body)
        self.assertEqual(json.loads(response.body), {"error": "Select a recipient with a native proposal folder."})
        self.assertFalse(any(item.type == review_items.ReviewType.PROPOSAL for item in cache.get().records.values()))

    def test_list_replays_a_draft_after_the_selected_recipient_disappears(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        other = fixture_member("producer", name="another-recipient")
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.INVESTIGATION, other.id),
                          page="list", request_id="r6-" + uuid4().hex,
                          page_recipient=new_item_dialog.PageRecipient.SELECTED)

        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              capture_sender("send_message"), self.lose_list_send_response() as replies):
            with self.assertRaises(RemoteDisconnected):
                _ = self.new_item_http(self.new_item_body(request), finished=True)
            saved, = replies
            members = staff.local_snapshot()
            _ = staff.record_path().write_text(replace(members, pending_retire=(other.id,)).encode())
            options = cast(dict[str, object], json.loads(self.new_item_http(query="?page=list").body))
            self.assertEqual(options["page_owner"], owner.id)
            recipients = cast(list[dict[str, object]], options["recipients"])
            self.assertNotIn(other.id, [row["id"] for row in recipients])
            response = self.new_item_http(self.new_item_body(request), finished=True)
            self.assertEqual(response.status, 409, response.body)
            replay = cast(dict[str, str], json.loads(response.body))
            self.assertEqual((replay["code"], replay["id"]), ("already_filed", saved["id"]))
            self.assertEqual(replay["key"], saved["key"])

        self.assertEqual(cache.get().records[cast(str, saved["key"])].owner, other.id)
        self.assertEqual(len(investigation.records(other.id)), 1)
        self.assertEqual(the_list.records(other.id), [])
        self.assertEqual(investigation.records(owner.id), [])
        self.assertEqual(the_list.records(owner.id), [])

    @contextmanager
    def lose_list_send_response(self) -> Generator[list[Mapping[str, object]]]:
        handler = cast(type[serve.ReviewLinksHandler], self.server.RequestHandlerClass)
        respond = handler.json_response
        saved: list[Mapping[str, object]] = []

        def lose_first(active: serve.ReviewLinksHandler, status: int, fields: Mapping[str, object]) -> None:
            if active.path == review_page.NEW_ITEM_PATH and status == 200 and not saved:
                saved.append(fields)
                active.close_connection = True
            else:
                respond(active, status, fields)

        with patch.object(handler, "json_response", lose_first):
            yield saved

    def test_list_draft_with_edited_words_returns_the_item_from_the_lost_reply(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.INVESTIGATION, owner.id),
                          page="list", request_id="r6-" + uuid4().hex)
        edited = replace(request, words="The edited draft words.")
        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              capture_sender("send_message"), self.lose_list_send_response() as replies):
            with self.assertRaises(RemoteDisconnected):
                _ = self.new_item_http(self.new_item_body(request), finished=True)
            saved, = replies
            edited_reply = self.new_item_http(self.new_item_body(edited), finished=True)
            self.assertEqual(edited_reply.status, 409, edited_reply.body)
            self.assertEqual(json.loads(edited_reply.body)["code"], "already_filed")
            invalid = {**asdict(edited), "recipient_id": "", "kind": "invalid", "images": [None] * 5,
                       "capture_key": "invalid", "token": self.server.new_item_token}
            invalid_reply = self.new_item_http(json.dumps(invalid).encode(), finished=True)
            self.assertEqual(invalid_reply.status, 409, invalid_reply.body)
            for reply in (edited_reply, invalid_reply):
                result = cast(dict[str, str], json.loads(reply.body))
                self.assertEqual((result["id"], result["key"]), (saved["id"], saved["key"]))
        original, = investigation.records(owner.id)
        self.assertEqual(original.question, request.words)
        self.assertEqual(the_list.records(owner.id), [])

    def test_concurrent_list_sends_of_one_draft_file_one_item(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.INVESTIGATION, owner.id),
                          page="list", request_id="r6-" + uuid4().hex)
        arrived = threading.Barrier(2, timeout=TEST_HANG_GUARD_SECS)
        completed = threading.Barrier(3, timeout=TEST_HANG_GUARD_SECS)
        handler = cast(type[serve.ReviewLinksHandler], self.server.RequestHandlerClass)
        post = handler.new_item_post

        def finish(active: serve.ReviewLinksHandler) -> None:
            try:
                post(active)
            finally:
                _ = completed.wait()

        def send(words: str) -> HttpReply:
            body = self.new_item_body(replace(request, words=words))
            _ = arrived.wait()
            return self.new_item_http(body)

        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status), capture_sender("send_message"),
              patch.object(handler, "new_item_post", finish),
              ThreadPoolExecutor(max_workers=2) as workers):
            first, second = tuple(workers.map(send, ("First words.", "Second words.")))
            _ = completed.wait()
        self.assertEqual(sorted((first.status, second.status)), [200, 409])
        first_fields, second_fields = (cast(dict[str, str], json.loads(reply.body)) for reply in (first, second))
        for field in ("id", "key"):
            self.assertEqual(first_fields[field], second_fields[field])
        conflict = first_fields if first.status == 409 else second_fields
        self.assertEqual(conflict["code"], "already_filed")
        original, = investigation.records(owner.id)
        self.assertEqual(original.id, first_fields["id"])
        self.assertEqual(the_list.records(owner.id), [])

    def test_list_retry_recovers_delivery_after_the_capture_expires(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.INVESTIGATION, owner.id),
                          page="list", request_id="r6-" + uuid4().hex,
                          page_recipient=new_item_dialog.PageRecipient.SELECTED)
        server = self.server
        key = server.page_captures.start(replace(FIXTURE_CAPTURE, place=list_page.PAGE_PATH))
        image = new_item_dialog.PageScreenshot(base64.b64decode(PNG), new_item_dialog.PageViewport(876, 640, True))
        _ = server.page_captures.complete(key, image)
        body = json.dumps({**asdict(request), "token": server.new_item_token, "capture_key": key}).encode()

        with (patch.object(server, "review_cache", cache),
              patch.object(server, "delivery_status", case.status),
              patch.object(change_page, "send_to_staff") as deliver):
            with patch.object(review_actions, "prepare_submission_delivery", side_effect=OSError("Delivery setup stopped.")):
                failed = self.new_item_http(body, finished=True)
            self.assertEqual(failed.status, 503, failed.body)
            original, = investigation.records(owner.id)
            self.assertEqual(the_list.records(owner.id), [])
            deliver.assert_not_called()
            with patch.object(server, "page_captures", serve.PageCaptures(self.root / ".review-link-empty-captures")):
                retry = self.new_item_http(body, finished=True)
                self.assertEqual(retry.status, 409, retry.body)
                saved = cast(dict[str, str], json.loads(retry.body))
                self.assertEqual(saved["code"], "already_filed")
            deliver.assert_called_once()

        self.assertEqual(saved["id"], original.id)
        self.assertEqual(the_list.records(owner.id), [])
        self.assertEqual(investigation.records(owner.id), [original])
        self.assertEqual(self.request(review_page.ATTACHMENT_PATH + "?" + urlencode(
            {"key": saved["key"], "id": review_submissions.PAGE_SCREENSHOT.id})),
            HttpReply(200, "image/png", image.png))

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_shared_dialog_opens_from_all_ten_served_console_pages(self) -> None:
        from scripts.build_queue import memory_files
        from scripts.disks import disks_page
        from scripts.review_links.test_new_item_dialog import DialogFixture, capture_dialogs

        owner = fixture_member("producer", name="console-owner")
        poc = self.root.parent / placement_disk.PAGE_NAME
        _ = poc.write_bytes(change_page.document("<h1>placement_disk</h1>"))
        _ = write_config_fixture(conf.path("console"), "".join(
            f"page.{page}.owner={owner.id}\n" for page in sorted(page_frame.OWNER_PAGES)) + f"placement_disk.file={poc}\n")
        for name, leaf in (("aws_page", aws_page.PAGE_NAME), ("disks", disks_page.PAGE_NAME),
                           ("memory_feed", memory_files.PAGE_NAME)):
            folder = self.root / name
            folder.mkdir()
            _ = write_config_fixture(conf.path(name), f"output_folder={folder}\n")
            _ = (folder / leaf).write_bytes(change_page.document(f"<h1>{name}</h1>"))
        _ = (self.root / "aws_page" / machines_page.PAGE_NAME).write_bytes(change_page.document("<h1>aws_machines</h1>"))
        _ = change_page.console_folder(self.root)
        document = change_page.document("<h1>Console page</h1>")
        pages = {
            "accounts": accounts_page.PAGE_PATH,
            "aws": f"/aws_page/{aws_page.PAGE_NAME}",
            "aws_machines": f"/aws_page/{machines_page.PAGE_NAME}",
            "builds": f"/memory_feed/{memory_files.PAGE_NAME}",
            "disks": f"/disks/{disks_page.PAGE_NAME}",
            "images": images.PAGE_PATH,
            "list": list_page.PAGE_PATH,
            "placement_disk": placement_disk.PAGE_PATH,
            "rebuilds": f"/console/{change_page.REBUILDS_NAME}",
            "review": review_page.PAGE_PATH,
        }
        probe = b"""<script>
(async ()=>{
 const failures=[];
 try {
  const buttons=document.querySelectorAll('[data-owner-new]');
  const dialogs=document.querySelectorAll('#review-new-item');
  if(buttons.length!==1 || dialogs.length!==1) throw new Error('Expected one button and one dialog.');
  buttons[0].click();
  if(!dialogs[0].matches(':modal')) throw new Error('The owner button did not open the dialog.');
  const checked=dialogs[0].querySelector('[name="kind"]:checked');
  if(checked?.value!=='bug') throw new Error('The dialog did not select Bug.');
  dialogs[0].querySelector('[data-cancel]').click();
  if(dialogs[0].open) throw new Error('Cancel did not close the dialog.');
 } catch(error) { failures.push(String(error)); }
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
 parent.postMessage('finished',location.origin);
})();
</script>"""
        self.assertEqual(pages.keys(), page_frame.OWNER_PAGES)
        fixtures: dict[int, DialogFixture] = {}
        documents: dict[int, bytes] = {}
        with (patch.object(accounts_page, "render_with_facts", return_value=accounts_page.AccountsRender(document, 0, 0)),
              patch.object(change_browser, "render_rebuilds", return_value=document)):
            for index, (page, path) in enumerate(pages.items(), start=800):
                with self.subTest(page=page):
                    response = self.request(path)
                    self.assertEqual(response.status, 200, response.body)
                    self.assertIn(f'data-page-owner="{page}"'.encode(), response.body)
                    self.assertIn(f'aria-label="New item for {owner.name}"'.encode(), response.body)
                    documents[index] = response.body.replace(b"</body>", probe + b"</body>")
                    fixtures[index] = DialogFixture(self.root)
        self.assertEqual(len(documents), len(pages))
        capture_dialogs(self.root.parent / "owner-dialogs.png", fixtures, pages=documents)
        for page, fixture in zip(pages, fixtures.values(), strict=True):
            with self.subTest(page=page):
                self.assertEqual(fixture.results, [])

    def test_placement_disk_page_reads_its_file_on_each_request(self) -> None:
        owner = fixture_member("producer", name="console-owner")
        poc = self.root.parent / placement_disk.PAGE_NAME
        _ = write_config_fixture(conf.path("console"), f"page.placement_disk.owner={owner.id}\nplacement_disk.file={poc}\n")
        missing = self.request(placement_disk.PAGE_PATH)
        self.assertEqual(missing.status, 503, missing.body)
        self.assertIn(b"The Placement disk POC page is not available.", missing.body)
        # The real page has an empty scene main and its heading in the panel after it.
        for heading in (b"First draft", b"Second draft"):
            with self.subTest(heading=heading):
                _ = poc.write_bytes(b'<!doctype html><html><head><title>POC</title></head><body><main id="stage"></main>'
                                    + b'<aside id="panel"><h1>' + heading + b'</h1></aside></body></html>')
                response = self.request(placement_disk.PAGE_PATH)
                self.assertEqual(response.status, 200, response.body)
                self.assertNotIn("content-security-policy", response.headers)
                self.assertIn(b'<main id="stage"></main>', response.body)
                self.assertIn(b'<aside id="panel"><div class="page-heading-actions" data-page-actions>', response.body)
                self.assertIn(b'</div></div><div class="page-heading"><h1>' + heading + b'</h1>'
                              + page_frame.HEADING_MEASURE.encode() + b'</div></aside>', response.body)
                self.assertIn(b'data-page-owner="placement_disk"', response.body)

    def test_placement_disk_file_expands_the_home_folder(self) -> None:
        home = self.root.parent / "home"
        _ = write_config_fixture(conf.path("console"), "placement_disk.file=~/poc/page.html\n")
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(placement_disk.source(), home / "poc" / "page.html")
        _ = write_config_fixture(conf.path("console"), "placement_disk.file=~no-such-user-fixture/page.html\n")
        with self.assertRaises(ValueError):
            _ = placement_disk.source()

    def test_page_owner_dialog_accepts_a_proposal_for_the_page_owner(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.PROPOSAL, owner.id),
                          page="review", request_id="r6-" + uuid4().hex,
                          page_recipient=new_item_dialog.PageRecipient.SELECTED)

        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              capture_sender("send_message")):
            response = self.new_item_http(self.new_item_body(request), finished=True)
        self.assertEqual(response.status, 200, response.body)
        saved = cast(dict[str, str], json.loads(response.body))
        item = cache.get().records[saved["key"]]
        self.assertEqual((item.owner, item.type), (owner.id, review_items.ReviewType.PROPOSAL))
        details = review_items.submission_origin(item)
        assert isinstance(details, new_item_dialog.ReviewSubmission)
        self.assertEqual(details.page, "review")

    def test_page_owner_dialog_files_for_the_selected_recipient(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        other = fixture_member("producer", name="another-recipient")
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.INVESTIGATION, other.id),
                          page="review", request_id="r6-" + uuid4().hex,
                          page_recipient=new_item_dialog.PageRecipient.SELECTED)

        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              capture_sender("send_message") as send):
            response = self.new_item_http(self.new_item_body(request), finished=True)
            send.assert_called_once_with(f"session:{other.current_session}", change_page.SENDER, ANY)
        self.assertEqual(response.status, 200, response.body)
        saved = cast(dict[str, str], json.loads(response.body))
        item = cache.get().records[saved["key"]]
        self.assertEqual((item.owner, item.type), (other.id, review_items.ReviewType.INVESTIGATION))
        filed, = investigation.records(other.id)
        self.assertEqual(filed.question, request.words)
        self.assertEqual(investigation.records(owner.id), [])

    def test_list_owner_control_and_sends_use_the_configured_producer(self) -> None:
        case = self.review_fixture()
        default = case.ready_submission_recipient()
        morty = fixture_member("producer", name="morty")
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={morty.id}\nowner.default={default.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        listing = self.request(list_page.PAGE_PATH)
        self.assertEqual(listing.status, 200)
        self.assertIn(b'data-page-owner="list"', listing.body)
        self.assertNotIn(b'data-list-new', listing.body)
        self.assertEqual(listing.body.count(b'data-owner-new aria-label='), 1)
        owner_control = listing.body.partition(b'data-page-owner="list">')[2].partition(b"</div>")[0]
        self.assertRegex(owner_control, rb'data-color-text="role.producer"[^>]*>morty</span>')
        self.assertIn(b'aria-label="New item for morty"', listing.body)
        self.assertEqual(listing.body.count(b'<dialog id="review-new-item"'), 1)
        options = self.new_item_http(query="?page=list")
        self.assertEqual(options.status, 200)
        fields = cast(dict[str, object], json.loads(options.body))
        expected = [{"id": morty.id, "name": "morty", "proposal": False},
                    {"id": default.id, "name": default.name, "proposal": True}]
        self.assertEqual(fields["page_owner"], morty.id)
        self.assertCountEqual(cast(list[dict[str, object]], fields["recipients"]), expected)
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={default.id}\nowner.default={default.id}\n")
        current_options = cast(dict[str, object], json.loads(self.new_item_http(query="?page=list").body))
        self.assertEqual(current_options["page_owner"], default.id)
        self.assertCountEqual(cast(list[dict[str, object]], current_options["recipients"]), expected)

        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              capture_sender("send_message") as send):
            cases = (
                (new_item_dialog.SubmissionKind.QUESTION, review_items.ReviewType.QUESTION, morty),
                (new_item_dialog.SubmissionKind.INVESTIGATION, review_items.ReviewType.INVESTIGATION, morty),
                (new_item_dialog.SubmissionKind.BUG, review_items.ReviewType.BUG, morty),
                (new_item_dialog.SubmissionKind.BUG, review_items.ReviewType.BUG, default),
            )
            for kind, review_type, recipient in cases:
                with self.subTest(kind=kind, recipient=recipient.name):
                    send.reset_mock()
                    request = replace(case.submission_request(kind, recipient.id), page="list")
                    if kind != new_item_dialog.SubmissionKind.BUG:
                        request = replace(request, request_id="r2-" + uuid4().hex)
                    body = self.new_item_body(request)
                    response = self.new_item_http(body, finished=True)
                    self.assertEqual(response.status, 200, response.body)
                    saved = cast(dict[str, str], json.loads(response.body))
                    item = cache.get().records[saved["key"]]
                    self.assertEqual((item.owner, item.type), (recipient.id, review_type))
                    retry = self.new_item_http(body, finished=True)
                    self.assertEqual(retry.status, 200, retry.body)
                    repeated = cast(dict[str, str], json.loads(retry.body))
                    self.assertEqual(repeated, saved | {"version": repeated["version"]})
                    self.assertEqual(the_list.records(recipient.id, include_closed=True), [])
                    self.assertNotIn("list_key", saved)
                    send.assert_called_once_with(f"session:{recipient.current_session}", change_page.SENDER, ANY)

    def test_new_item_command_files_through_the_handler_and_makes_its_list_item(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        unit = case.fixture.unit
        morty = fixture_member("producer", name="morty")
        producer = fixture_member("producer", name="bob")
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={owner.id}\n")
        _ = conf.path("producer_map").write_text(f"{owner.id} = {producer.id}\n", encoding="utf-8")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        words = "The frame time graph has a gap."
        output, errors = StringIO(), StringIO()
        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              patch.object(for_you, "console_address", return_value=for_you.ConsoleAddress("127.0.0.1", self.port)),
              patch("platform.system", return_value="Linux"),
              capture_sender("send_message"), redirect_stdout(output), redirect_stderr(errors)):
            code = new_item.main(["bug", unit.name, words, "--session", "another-session"])
            # The server replies before it delivers the item.
            self.assertTrue(self.new_item_posts.wait_until_none_run())
        # The server's request log shares stderr with the command.
        self.assertEqual(code, new_item.Outcome.FILED, errors.getvalue())
        self.assertNotIn("new_item:", errors.getvalue())
        key, = [key for key, record in cache.get().records.items() if record.type == review_items.ReviewType.BUG]
        self.assertEqual(cache.get().records[key].owner, owner.id)
        item, = the_list.records(morty.id, include_closed=True)
        self.assertEqual(output.getvalue(), f"Bug \u2192 {unit.name}, a unit of {owner.name}. "
                         + f"Producer: bob (from {owner.name}). List item {item.number}.\n")
        self.assertEqual((item.title, item.owner, item.producer, item.unit, item.reviews),
                         (words, owner.id, producer.id, unit.name, (key,)))

    def test_list_draft_retries_after_failed_delivery_setup(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        image = base64.b64decode(PNG)

        def sent_message(delivery: change_page.MessageDelivery, status: change_page.DeliveryStatus) -> str:
            assert isinstance(delivery.page, change_page.ReviewMessagePage)
            reply = delivery.page.saved.get("scoped_reply")
            assert reply is not None
            prefix, separator, command = delivery.message.rpartition("\n")
            self.assertEqual(command, change_page.reply_command(reply["key"]))
            proof = review_submissions.current_reply_proof(reply["key"])
            token = reply_tokens.derive(proof)
            self.assertNotIn(token, delivery.message)
            record = status.path(delivery.page, delivery.row).read_text(encoding="utf-8")
            self.assertNotIn(token, record)
            return prefix + separator + change_page.reply_command(reply["key"], token)

        def capture_body(request: new_item_dialog.NewItemRequest) -> tuple[str, bytes]:
            capture_key = self.server.page_captures.start(FIXTURE_CAPTURE)
            self.assertTrue(capture_key)
            self.assertIsNone(self.server.page_captures.complete(capture_key,
                new_item_dialog.PageScreenshot(image, new_item_dialog.PageViewport(1280, 800, True))))
            return capture_key, json.dumps({**asdict(request), "token": self.server.new_item_token,
                                            "capture_key": capture_key}).encode()

        def prepare_transfer(sources: tuple[staff.StaffRow, ...], successor: staff.StaffRow) -> Callable[[], None]:
            children: dict[staff.StaffId, staff.StaffRow] = {}
            for child in sources:
                assert isinstance(child.doc, staff.LiveDoc)
                children[child.id] = replace(child, parent=staff.ReportsTo(successor.id),
                                             doc=staff.WrappedDoc(child.doc.machine, child.doc.path))
            members = staff.local_snapshot()
            wrapped = replace(members, rows=tuple(children.get(row.id, row) for row in members.rows))
            _ = staff.record_path().write_text(wrapped.encode())
            def transfer() -> None:
                with (patch.object(tidy_wrapped, "STATE_DIR", record_store.state_root()),
                      patch("scripts.copies.copies.remove_trees")):
                    for child in children.values():
                        assert isinstance(child.doc, staff.WrappedDoc)
                        _ = tidy_wrapped.tidy(child.name, child.doc.path,
                                              identities=update_registration.QuestionStaff.for_record(wrapped))

            return transfer

        cases = ("same-owner", "transfer-open", "transfer-reopen", "config-reopen",
                 "transfer-two-reopen", "other-damaged-reopen", "transfer-during-retry-reopen")
        for scenario in cases:
            with self.subTest(scenario=scenario):
                case = self.review_fixture()
                owner = case.ready_submission_recipient()
                later_owner = None
                if scenario in ("transfer-two-reopen", "other-damaged-reopen"):
                    other_doc = case.fixture.temporary / "other.md"
                    _ = other_doc.write_text(case.fixture.production.read_text())
                    other = fixture_member("showrunner", name="other", doc=staff.LiveDoc(local_machine(), other_doc))
                    # Make the older item's marker sort first.
                    owner, later_owner = sorted((owner, other), key=lambda row: row.id)
                _ = write_config_fixture(conf.path("console"), f"page.list.owner={owner.id}\n")
                cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
                request = replace(case.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner.id),
                                  page="list", request_id="r2-" + uuid4().hex)
                capture_key, body = capture_body(request)
                deliveries: list[change_page.MessageDelivery] = []
                deliver = change_page.deliver_with_outcome

                def record_delivery(delivery: change_page.MessageDelivery | change_page.NoMessageDelivery,
                                    status: change_page.DeliveryStatus) -> change_page.DeliveryOutcome:
                    if isinstance(delivery, change_page.MessageDelivery):
                        deliveries.append(delivery)
                    return deliver(delivery, status)

                with (patch.object(self.server, "review_cache", cache),
                      patch.object(self.server, "delivery_status", case.status),
                      patch.object(change_page, "deliver_with_outcome", record_delivery),
                      patch.object(change_page, "send_message") as send,
                      patch.object(image_store, "register_file", wraps=image_store.register_file) as register):
                    with patch.object(review_actions, "prepare_submission_delivery",
                                      side_effect=OSError("Delivery setup stopped.")):
                        self.assertEqual(self.new_item_http(body, finished=True).status, 503)
                    original, = update_registration.question_records_for_owner(owner.id, strict=True)
                    send.assert_not_called()
                    register.assert_called_once()

                    delivered_id = ""
                    sources = (owner,)
                    if later_owner is not None:
                        _ = write_config_fixture(conf.path("console"), f"page.list.owner={later_owner.id}\n")
                        self.server.page_captures.discard(capture_key)
                        capture_key, later_body = capture_body(replace(request, recipient_id=later_owner.id))
                        handler = cast(type[serve.ReviewLinksHandler], self.server.RequestHandlerClass)
                        respond = handler.json_response

                        def lose_response(active: serve.ReviewLinksHandler, status: int,
                                          fields: Mapping[str, object]) -> None:
                            nonlocal delivered_id
                            if active.path == review_page.NEW_ITEM_PATH and status == 200:
                                delivered_id = str(fields["id"])
                                active.close_connection = True
                            else:
                                respond(active, status, fields)

                        with (patch.object(handler, "json_response", lose_response),
                              self.assertRaises(RemoteDisconnected)):
                            _ = self.new_item_http(later_body, finished=True)
                        self.assertTrue(delivered_id)
                        delivery, = deliveries
                        send.assert_called_once_with(
                            f"session:{later_owner.current_session}", change_page.SENDER,
                            sent_message(delivery, case.status),
                            key=change_page.review_delivery_key(delivery.page.review_list, delivery.row, delivery.attempt),
                            recipient_key=later_owner.id)
                        self.assertEqual(register.call_count, 2)
                        if scenario == "transfer-two-reopen":
                            sources += (later_owner,)

                    target = owner
                    transfer_at_lock = None
                    if scenario.startswith("transfer"):
                        parent_doc = case.fixture.temporary / "successor.md"
                        _ = parent_doc.write_text(case.fixture.production.read_text())
                        target = fixture_member("showrunner", name="successor",
                                                doc=staff.LiveDoc(local_machine(), parent_doc))
                        transfer = prepare_transfer(sources, target)
                        if scenario == "transfer-during-retry-reopen":
                            transfer_at_lock = transfer
                        else:
                            transfer()
                    elif scenario == "config-reopen":
                        target = fixture_member("producer", name="new-list-owner")
                        _ = write_config_fixture(conf.path("console"), f"page.list.owner={target.id}\n")
                    elif scenario == "other-damaged-reopen":
                        _ = write_config_fixture(conf.path("console"), f"page.list.owner={owner.id}\n")

                    if scenario.endswith("reopen"):
                        options = cast(dict[str, object], json.loads(self.new_item_http(query="?page=list").body))
                        self.assertEqual(options["page_owner"], target.id)
                        self.assertIn(target.id, [row["id"] for row in cast(
                            list[dict[str, object]], options["recipients"])])
                        self.server.page_captures.discard(capture_key)
                        _, body = capture_body(replace(request, recipient_id=target.id))

                    locks = investigation.recipient_locks

                    @contextmanager
                    def finish_transfer_at_lock(*owners: staff.StaffId) -> Generator[None]:
                        nonlocal transfer_at_lock
                        if transfer_at_lock is not None and target.id in owners:
                            transfer, transfer_at_lock = transfer_at_lock, None
                            transfer()
                        with locks(*owners):
                            yield

                    with ExitStack() as race:
                        if transfer_at_lock is not None:
                            _ = race.enter_context(patch.object(investigation, "recipient_locks", finish_transfer_at_lock))
                        response = self.new_item_http(body, finished=True)
                    self.assertIsNone(transfer_at_lock)
                    self.assertEqual(response.status, 200, response.body)
                    saved = cast(dict[str, str], json.loads(response.body))
                    retry = self.new_item_http(body, finished=True)
                    self.assertEqual(retry.status, 200, retry.body)
                    repeated = cast(dict[str, str], json.loads(retry.body))
                    self.assertEqual(repeated, saved | {"version": repeated["version"]})

                    if scenario == "same-owner":
                        fields = cast(dict[str, object], json.loads(body))
                        fields["request_id"] = "r6-" + request.request_id[3:]
                        compatible = self.new_item_http(json.dumps(fields).encode(), finished=True)
                        self.assertEqual(compatible.status, 409, compatible.body)
                        self.assertEqual(json.loads(compatible.body)["key"], saved["key"])
                        fields["words"] = "An edit after the old List item was filed."
                        replay = self.new_item_http(json.dumps(fields).encode(), finished=True)
                        self.assertEqual(replay.status, 409, replay.body)
                        self.assertEqual(json.loads(replay.body)["key"], saved["key"])

                    item = cache.get().records[saved["key"]]
                    self.assertEqual(item.owner, target.id)
                    records = update_registration.question_records_for_owner(target.id, strict=True)
                    self.assertEqual(len(records), len(sources))
                    native, = [record for record in records if record.item.get("review_id") == saved["id"]]
                    self.assertEqual(native.item["text"], original.item["text"])
                    origin = review_items.submission_origin(item)
                    assert isinstance(origin, new_item_dialog.ReviewSubmission)
                    original_origin = review_submissions.native_origin(original)
                    assert isinstance(original_origin, new_item_dialog.ReviewSubmission)
                    if scenario == "transfer-two-reopen":
                        assert later_owner is not None
                        self.assertEqual(saved["id"], delivered_id)
                        self.assertNotEqual(origin.request_id, original_origin.request_id)
                        self.assertEqual(update_registration.question_records_for_owner(later_owner.id, strict=True), [])
                    elif scenario == "config-reopen":
                        self.assertNotEqual(origin.request_id, original_origin.request_id)
                        self.assertEqual(update_registration.question_records_for_owner(owner.id, strict=True), [original])
                    else:
                        self.assertEqual(origin.request_id, original_origin.request_id)
                        self.assertEqual(native.item.get("review_id"), original.item.get("review_id"))
                        if target != owner:
                            self.assertEqual(
                                update_registration.question_records_for_owner(owner.id, strict=True), [])
                            self.assertEqual(the_list.records(owner.id), [])

                    self.assertEqual(the_list.records(target.id), [])
                    self.assertEqual(origin.images, (review_submissions.PAGE_SCREENSHOT,))
                    attachment = urlencode({"key": saved["key"], "id": review_submissions.PAGE_SCREENSHOT.id})
                    self.assertEqual(self.request(review_page.ATTACHMENT_PATH + "?" + attachment).body, image)
                    if scenario == "other-damaged-reopen":
                        assert later_owner is not None
                        other_record, = update_registration.question_records_for_owner(later_owner.id, strict=True)
                        other_origin = review_submissions.native_origin(other_record)
                        assert isinstance(other_origin, new_item_dialog.ReviewSubmission)
                        index = review_submissions.image_folder(other_origin).parent.with_name(
                            f"r4-{other_origin.request_digest}")
                        marker = index / f"page-list-{later_owner.id}"
                        other_body = self.new_item_body(replace(request, recipient_id=later_owner.id))

                        def check_retry() -> None:
                            refused = self.new_item_http(other_body, finished=True)
                            self.assertEqual(refused.status, 503, refused.body)
                            replay = self.new_item_http(body, finished=True)
                            self.assertEqual(replay.status, 200, replay.body)
                            fields = cast(dict[str, str], json.loads(replay.body))
                            self.assertEqual(fields, saved | {"version": fields["version"]})
                            self.assertEqual(send.call_count, 2)
                            self.assertEqual(register.call_count, 2)

                        with self.subTest(damage="marker"):
                            original_marker = marker.read_bytes()
                            try:
                                _ = marker.write_bytes(b"\xff")
                                check_retry()
                            finally:
                                _ = marker.write_bytes(original_marker)
                        with self.subTest(damage="native record"):
                            hidden = other_record.path.rename(other_record.path.with_suffix(".hidden"))
                            try:
                                check_retry()
                            finally:
                                _ = hidden.rename(other_record.path)
                        continue
                    sent_to = later_owner if later_owner is not None else target
                    delivery, = deliveries
                    send.assert_called_once_with(
                        f"session:{sent_to.current_session}", change_page.SENDER, sent_message(delivery, case.status),
                        key=change_page.review_delivery_key(delivery.page.review_list, delivery.row, delivery.attempt),
                        recipient_key=sent_to.id)
                    self.assertEqual(register.call_count, 2 if later_owner is not None or scenario == "config-reopen" else 1)

    def test_f014_standard_new_replays_the_item_filed_for_the_new_page_owner(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        parent_doc = case.fixture.temporary / "parent.md"
        _ = parent_doc.write_text(case.fixture.production.read_text())
        parent = fixture_member("showrunner", name="parent", doc=staff.LiveDoc(local_machine(), parent_doc))
        members = staff.local_snapshot()
        child = replace(owner, parent=staff.ReportsTo(parent.id))
        members = replace(members, rows=tuple(child if row.id == child.id else row for row in members.rows))
        _ = staff.record_path().write_text(members.encode())
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={child.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.BUG, child.id),
                          page="review", request_id="r2-" + uuid4().hex)
        body = json.dumps({**asdict(request), "token": self.server.new_item_token}).encode()
        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              patch.object(change_page, "deliver_with_outcome", return_value=change_page.DeliveryConfirmed())):
            recipients = cast(dict[str, object], json.loads(self.new_item_http(query="?page=review").body))
            self.assertEqual(recipients["page_owner"], child.id)
            self.assertIn({"id": child.id, "name": child.name, "proposal": True},
                          cast(list[dict[str, object]], recipients["recipients"]))
            with patch.object(investigation.Investigations, "write", side_effect=OSError("Interrupted write.")):
                self.assertEqual(self.new_item_http(body).status, 503)
            self.assertEqual(investigation.records(child.id), [])
            _ = staff.record_path().write_text(replace(members, pending_retire=(child.id,)).encode())
            recipients = cast(dict[str, object], json.loads(self.new_item_http(query="?page=review").body))
            self.assertEqual(recipients["page_owner"], parent.id)
            self.assertNotIn(child.id, [row["id"] for row in cast(
                list[dict[str, object]], recipients["recipients"])])
            page = self.request(review_page.PAGE_PATH)
            self.assertIn(b'aria-label="New item for parent"', page.body)
            response = self.new_item_http(body)
            self.assertEqual(response.status, 200, response.body)
            saved = cast(dict[str, str], json.loads(response.body))
            self.assertEqual(cache.get().records[saved["key"]].owner, parent.id)
            self.assertEqual(investigation.records(child.id), [])
            standard = replace(request, page="", recipient_id=parent.id)
            replay = self.new_item_http(json.dumps({**asdict(standard), "token": self.server.new_item_token}).encode())
            self.assertEqual(replay.status, 200, replay.body)
            self.assertEqual(cast(dict[str, str], json.loads(replay.body))["key"], saved["key"])
            self.assertEqual([item.id for item in investigation.records(parent.id)], [saved["id"]])

    def test_f016_page_owner_retry_reuses_item_after_child_retires(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        parent_doc = case.fixture.temporary / "parent.md"
        _ = parent_doc.write_text(case.fixture.production.read_text())
        parent = fixture_member("showrunner", name="parent", doc=staff.LiveDoc(local_machine(), parent_doc))
        members = staff.local_snapshot()
        child = replace(owner, parent=staff.ReportsTo(parent.id))
        members = replace(members, rows=tuple(child if row.id == child.id else row for row in members.rows))
        _ = staff.record_path().write_text(members.encode())
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={child.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.BUG, child.id),
                          page="review", request_id="r2-" + uuid4().hex)

        def send(payload: new_item_dialog.NewItemRequest) -> dict[str, str]:
            response = self.new_item_http(json.dumps({**asdict(payload), "token": self.server.new_item_token}).encode())
            self.assertEqual(response.status, 200, response.body)
            return cast(dict[str, str], json.loads(response.body))

        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              patch.object(change_page, "deliver_with_outcome", return_value=change_page.DeliveryConfirmed())):
            saved = send(request)
            self.assertEqual(cache.get().records[saved["key"]].owner, child.id)
            _ = staff.record_path().write_text(replace(members, pending_retire=(child.id,)).encode())
            options = cast(dict[str, object], json.loads(self.new_item_http(query="?page=review").body))
            self.assertEqual(options["page_owner"], parent.id)
            self.assertNotIn(child.id, [row["id"] for row in cast(
                list[dict[str, object]], options["recipients"])])
            retry = replace(request, recipient_id=parent.id)
            self.assertEqual(send(retry)["key"], saved["key"])
            self.assertEqual(investigation.records(parent.id), [])

            alternate = send(replace(retry, page=""))
            self.assertNotEqual(alternate["key"], saved["key"])
            self.assertEqual(send(retry)["key"], saved["key"])
            self.assertEqual([item.id for item in investigation.records(child.id)], [saved["id"]])
            self.assertEqual([item.id for item in investigation.records(parent.id)], [alternate["id"]])

    def test_f018_page_owner_send_files_after_lost_standard_response(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        other = fixture_member("producer", name="another-recipient")
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\npage.list.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        standard = replace(case.submission_request(new_item_dialog.SubmissionKind.BUG, other.id),
                           request_id="r2-" + uuid4().hex)
        page = replace(standard, recipient_id=owner.id, page="review")

        def send(payload: new_item_dialog.NewItemRequest) -> dict[str, str]:
            response = self.new_item_http(json.dumps({**asdict(payload), "token": self.server.new_item_token}).encode())
            self.assertEqual(response.status, 200, response.body)
            return cast(dict[str, str], json.loads(response.body))

        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              patch.object(change_page, "deliver_with_outcome", return_value=change_page.DeliveryConfirmed())):
            chosen = send(standard)
            owner_item = send(page)
            self.assertNotEqual(owner_item["key"], chosen["key"])
            self.assertEqual(send(page)["key"], owner_item["key"])
            self.assertEqual(send(replace(page, page=""))["key"], owner_item["key"])

            other_page = replace(page, page="list")
            other_item = send(other_page)
            self.assertEqual(other_item["key"], owner_item["key"])
            self.assertEqual(send(other_page)["key"], owner_item["key"])
            self.assertEqual(send(standard)["key"], chosen["key"])

        chosen_record, = investigation.records(other.id)
        self.assertEqual(chosen_record.id, chosen["id"])
        assert isinstance(chosen_record.submission, new_item_dialog.ReviewSubmission)
        self.assertEqual(chosen_record.submission.page, "")
        owner_record, = investigation.records(owner.id)
        self.assertEqual(owner_record.id, owner_item["id"])
        assert isinstance(owner_record.submission, new_item_dialog.ReviewSubmission)
        self.assertEqual(owner_record.submission.page, "review")

    def test_f019_page_owner_send_reuses_standard_item_for_same_recipient(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        standard = replace(case.submission_request(new_item_dialog.SubmissionKind.BUG, owner.id),
                           request_id="r2-" + uuid4().hex)
        # The page request has no capture key or page evidence.
        page = replace(standard, page="review")

        def send(payload: new_item_dialog.NewItemRequest) -> dict[str, str]:
            response = self.new_item_http(json.dumps({**asdict(payload), "token": self.server.new_item_token}).encode())
            self.assertEqual(response.status, 200, response.body)
            return cast(dict[str, str], json.loads(response.body))

        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              patch.object(change_page, "deliver_with_outcome", return_value=change_page.DeliveryConfirmed())):
            saved = send(standard)
            retry = send(page)
            self.assertEqual(retry["key"], saved["key"])

        item, = investigation.records(owner.id)
        self.assertEqual(item.id, saved["id"])
        assert isinstance(item.submission, new_item_dialog.ReviewSubmission)
        self.assertEqual(item.submission.page, "")

    def test_f023_remote_owner_replies_to_question_in_sender_store(self) -> None:
        self.check_reply_from_another_machine(new_item_dialog.SubmissionKind.QUESTION)

    def test_f026_remote_owner_replies_to_bug_in_sender_store(self) -> None:
        self.check_reply_from_another_machine(new_item_dialog.SubmissionKind.BUG)

    def test_f026_remote_owner_replies_to_feedback_in_sender_store(self) -> None:
        self.check_reply_from_another_machine(new_item_dialog.SubmissionKind.INVESTIGATION)

    def check_reply_from_another_machine(self, kind: new_item_dialog.SubmissionKind) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        mac = "Nates-MacBook-Pro-2023"
        natedev = case.fixture.temporary / "natedev"
        remote_path = natedev / "production.md"
        remote_owner = replace(owner, doc=staff.LiveDoc("natedev", remote_path))
        members = staff.local_snapshot()
        _ = staff.record_path().write_text(replace(members, rows=(remote_owner,)).encode())
        config = conf.path("console")
        _ = write_config_fixture(conf.path("machines"))
        _ = write_config_fixture(config, f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(kind, owner.id), page="review", request_id="r2-" + uuid4().hex)
        body = json.dumps({**asdict(request), "token": self.server.new_item_token}).encode()
        delivered = threading.Event()

        def note_delivery(_recipient: staff.StaffId, _sender: str, _words: str, **_options: str) -> None:
            delivered.set()

        self.assertFalse(remote_path.exists())
        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              patch.object(review_submissions, "local_machine", return_value=mac),
              patch.object(record_store, "local_machine", return_value=mac),
              patch.object(remote, "local_machine", return_value=mac),
              patch.object(change_page, "send_to_staff", side_effect=note_delivery) as deliver):
            options = self.new_item_http(query="?page=review")
            self.assertEqual(options.status, 200, options.body)
            self.assertEqual(json.loads(options.body)["recipients"],
                             [{"id": owner.id, "name": owner.name, "proposal": False}])
            response = self.new_item_http(body, finished=True)
            self.assertEqual(response.status, 200, response.body)
            saved = cast(dict[str, str], json.loads(response.body))
            item = cache.get().records[saved["key"]]
            self.assertEqual((item.owner, item.state), (owner.id, review_items.ReviewState.WAITING))
            path = review_items.source_path(item)
            self.assertTrue(path.is_relative_to(record_store.state_root()))
            self.assertTrue(delivered.wait(TEST_HANG_GUARD_SECS))
            deliver.assert_called_once()
            message = cast(str, deliver.call_args.args[2])
        command = next(line for line in message.splitlines() if " answer-question " in line)
        arguments = shlex.split(command)[2:]
        self.assertEqual(arguments[arguments.index("--machine") + 1], mac)
        words = "The owner's reply includes 'quotes'.\nThe second line reached Review."
        arguments[-1] = words
        sender_environment = {key: os.environ[key] for key in ("REVIEW_LINKS_ROOT", "SHOWRUNNER_STATE_DIR")}

        def on_mac(ssh: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(ssh[:4], ["ssh", "-o", "BatchMode=yes", "mac"])
            forwarded = shlex.split(ssh[4])[2:]
            self.assertEqual(forwarded[0], "answer-question")
            self.assertNotIn("--machine", forwarded)
            output = StringIO()
            with (patch.dict(os.environ, sender_environment), patch.object(remote, "local_machine", return_value=mac),
                  patch.object(reply_tokens, "path", return_value=case.root / "absent-reply-token.key"),
                  patch.object(record_store, "local_machine", return_value=mac), redirect_stdout(output)):
                status = change_page.main(forwarded)
            return subprocess.CompletedProcess(ssh, status, output.getvalue(), "")

        with (patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(natedev / "review"),
                                    "SHOWRUNNER_STATE_DIR": str(natedev / "state")}),
              patch.object(remote, "local_machine", return_value="natedev"),
              patch.object(remote, "this_machine", return_value="natedev"),
              patch.object(subprocess, "run", side_effect=on_mac) as transport, redirect_stdout(StringIO())):
            self.assertEqual(change_page.main(arguments), 0)
            transport.assert_called_once()
            self.assertEqual(update_registration.question_records_for_owner(owner.id, strict=True), [])
            self.assertEqual(investigation.records(owner.id), [])

        with (patch.object(record_store, "local_machine", return_value=mac),
              patch.object(self.server, "review_cache", cache)):
            rebuilt = review_page.build_snapshot(case.root)
            answered = rebuilt.records[saved["key"]]
            self.assertEqual(review_items.source_path(answered), path)
            self.assertEqual(answered.state, review_items.ReviewState.OPEN)
            self.assertIn("Answer: " + words, [event.words for event in answered.log])
            with patch.object(cache, "get", return_value=rebuilt):
                card = self.request(review_page.PAGE_PATH + "?" + urlencode({"body": saved["key"]}))
            self.assertEqual(card.status, 200)
            self.assertIn(b"The second line reached Review.", card.body)

    def test_f024_question_retry_after_lost_response_reuses_one_sender_record(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        remote_owner = replace(owner, doc=staff.LiveDoc("natedev", case.fixture.temporary / "remote.md"))
        members = replace(staff.local_snapshot(), rows=(remote_owner,))
        _ = staff.record_path().write_text(members.encode())
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner.id),
                          page="review", request_id="r2-" + uuid4().hex)
        body = json.dumps({**asdict(request), "token": self.server.new_item_token}).encode()
        handler = cast(type[serve.ReviewLinksHandler], self.server.RequestHandlerClass)
        respond = handler.json_response
        lost: list[str] = []
        real_delivery = change_page.deliver_with_outcome

        def lose_response(active: serve.ReviewLinksHandler, status: int, fields: Mapping[str, object]) -> None:
            if active.path == review_page.NEW_ITEM_PATH and status == 200 and not lost:
                lost.append(str(fields["key"]))
                active.close_connection = True
                return
            respond(active, status, fields)

        with (patch.object(self.server, "review_cache", cache), patch.object(self.server, "delivery_status", case.status),
              patch.object(review_submissions, "local_machine", return_value="mac"),
              patch.object(record_store, "local_machine", return_value="mac"),
              patch.object(handler, "json_response", lose_response),
              patch.object(change_page, "deliver_with_outcome", return_value=change_page.DeliveryConfirmed()) as deliver):
            state = self.root / "state"
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}):
                overlap = self.new_item_http(body, finished=True)
                self.assertEqual((overlap.status, json.loads(overlap.body)),
                                 (503, {"error": "The reply key must be outside the served Review Links root. Try Send again."}))
                self.assertFalse(state.exists())
            with self.assertRaises(RemoteDisconnected):
                _ = self.new_item_http(body, finished=True)
            deliver.assert_called_once()
            first = cast(change_page.MessageDelivery, deliver.call_args.args[0])
            deliver.reset_mock()
            _ = staff.record_path().write_text(replace(members, pending_retire=(owner.id,)).encode())
            key_file = reply_tokens.path()
            self.assertEqual(key_file, Path(os.environ["XDG_STATE_HOME"]) / "review-links" / "reply-token.key")
            self.assertFalse(key_file.is_relative_to(self.root))
            self.assertFalse(key_file.is_relative_to(case.root))
            self.assertEqual(key_file.parent.stat().st_mode & 0o777, 0o700)
            _ = (self.root / key_file.name).write_bytes(b"fixture key")
            self.assertEqual(self.request("/" + key_file.name).status, 404)
            original = key_file.read_bytes()
            token = change_page.outbound_review_message(first).token
            key_file.unlink()
            failure = {"error": "The local reply key is missing. Try Send again."}
            unavailable = self.new_item_http(body, finished=True)
            self.assertEqual((unavailable.status, json.loads(unavailable.body)), (503, failure))
            for invalid, reason in ((b"short", "The local reply key must contain 32 bytes."),
                                    (bytes(byte ^ 1 for byte in original), "The local reply key does not match the note.")):
                _ = key_file.write_bytes(invalid)
                key_file.chmod(0o600)
                unavailable = self.new_item_http(body, finished=True)
                self.assertEqual((unavailable.status, json.loads(unavailable.body)),
                                 (503, {"error": f"{reason} Try Send again."}))
                self.assertEqual(key_file.read_bytes(), invalid)
            deliver.assert_not_called()
            _ = key_file.write_bytes(original)

            deliver.side_effect = real_delivery
            with (patch.object(change_page, "send_to_staff",
                               side_effect=change_page.FinalSendFailure("Transport refused.")) as send,
                  patch.object(case.status, "record_result_locked", side_effect=OSError("Cannot save delivery result."))):
                refused = self.new_item_http(body, finished=True)
                self.assertEqual((refused.status, json.loads(refused.body)),
                                 (503, {"error": "Transport refused. Try Send again."}))
                self.assertGreater(send.call_count, 0)
            self.assertIsInstance(case.status.failed_delivery(first.page, first.row), change_page.NoFailedDelivery)

            def lose_key_before_delivery(pending: change_page.MessageDelivery,
                                         status: change_page.DeliveryStatus) -> change_page.DeliveryOutcome:
                key_file.unlink()
                return real_delivery(pending, status)

            deliver.side_effect = lose_key_before_delivery
            with patch.object(change_page, "send_to_staff") as send:
                late_failure = self.new_item_http(body, finished=True)
                self.assertEqual((late_failure.status, json.loads(late_failure.body)), (503, failure))
                send.assert_not_called()
                recorded = case.status.failed_delivery(first.page, first.row)
                assert isinstance(recorded, change_page.FailedDelivery)
                self.assertEqual((recorded.attempt, recorded.last_error),
                                 (first.attempt, "The local reply key is missing."))
                _ = key_file.write_bytes(original)
                key_file.chmod(0o600)
                deliver.reset_mock(side_effect=True)
                deliver.side_effect = real_delivery
                response = self.new_item_http(body, finished=True)
                send.assert_called_once()
            self.assertIsInstance(case.status.failed_delivery(first.page, first.row), change_page.NoFailedDelivery)
            self.assertEqual(response.status, 200, response.body)
            deliver.assert_called_once()
            retry = cast(change_page.MessageDelivery, deliver.call_args.args[0])
            self.assertEqual(retry.attempt, first.attempt)
            saved = cast(dict[str, str], json.loads(response.body))
            self.assertEqual(lost, [saved["key"]])
            record, = update_registration.question_records_for_owner(owner.id, strict=True)
            self.assertEqual(record.item.get("review_id"), saved["id"])
            key_file.unlink()
            assert isinstance(first.page, change_page.ReviewMessagePage)
            command = first.page.saved.get("scoped_reply")
            assert command is not None
            self.assertTrue(review_submissions.answer(command["key"], "The owner replied.", token))
            after_reply = self.new_item_http(body, finished=True)
            self.assertEqual(after_reply.status, 200, after_reply.body)
            self.assertEqual(json.loads(after_reply.body)["key"], saved["key"])
            self.assertIsInstance(cast(object, deliver.call_args.args[0]), change_page.NoMessageDelivery)
            self.assertFalse(key_file.exists())

    def test_f027_mac_question_send_uses_authoritative_retirement(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        parent_doc = staff.LiveDoc("natedev", case.fixture.temporary / "parent.md")
        child_doc = staff.LiveDoc("natedev", case.fixture.temporary / "child.md")
        parent = fixture_member("showrunner", name="parent", doc=parent_doc)
        child = replace(owner, parent=staff.ReportsTo(parent.id), doc=child_doc)
        mac_copy = replace(staff.local_snapshot(), rows=(parent, child))
        mac_staff = case.fixture.temporary / "mac-staff.json"
        _ = mac_staff.write_text(mac_copy.encode())
        authority = replace(mac_copy, pending_retire=(child.id,))
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={child.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.QUESTION, child.id),
                          page="review", request_id="r2-" + uuid4().hex)
        body = json.dumps({**asdict(request), "token": self.server.new_item_token}).encode()

        with (patch.dict(os.environ), patch.object(staff, "_default_file", return_value=mac_staff),
              patch.object(staff, "_remote", return_value=json.loads(authority.encode())) as staff_call,
              patch.object(remote, "other_machine", return_value="natedev"),
              patch.object(review_submissions, "local_machine", return_value="mac"),
              patch.object(record_store, "local_machine", return_value="mac"),
              patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status), patch.object(change_page, "send_to_staff")):
            del os.environ["STAFF_FILE"]
            self.assertFalse(staff.is_owner())
            self.assertEqual(staff.local_snapshot().pending_retire, ())
            response = self.new_item_http(body, finished=True)
            self.assertEqual(response.status, 200, response.body)
            saved = cast(dict[str, str], json.loads(response.body))
            self.assertEqual(cache.get().records[saved["key"]].owner, parent.id)
            staff_call.assert_called_once_with(["show", "--snapshot"])
            self.assertEqual(update_registration.question_records_for_owner(child.id, strict=True), [])
            record, = update_registration.question_records_for_owner(parent.id, strict=True)
            self.assertEqual(record.item.get("review_id"), saved["id"])

    def test_f028_waiting_remote_question_accepts_a_new_question(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        remote_owner = replace(owner, doc=staff.LiveDoc("natedev", case.fixture.temporary / "remote.md"))
        members = replace(staff.local_snapshot(), rows=(remote_owner,))
        _ = staff.record_path().write_text(members.encode())
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner.id),
                          page="review", request_id="r2-" + uuid4().hex)
        with (patch.object(review_submissions, "local_machine", return_value="mac"),
              patch.object(record_store, "local_machine", return_value="mac"),
              patch.object(remote, "local_machine", return_value="mac"),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              patch.object(change_page, "send_to_staff"),
              patch.object(change_page, "deliver", wraps=change_page.deliver) as deliver):
            response = self.new_item_http(json.dumps({**asdict(request), "token": self.server.new_item_token}).encode(), finished=True)
            self.assertEqual(response.status, 200, response.body)
            saved = cast(dict[str, str], json.loads(response.body))
            snapshot = cache.get()
            item = snapshot.records[saved["key"]]
            self.assertEqual(item.state, review_items.ReviewState.WAITING)
            original = review_items.submission_origin(item)
            assert isinstance(original, new_item_dialog.ReviewSubmission)
            original_token = reply_tokens.derive(original.reply_proof)
            self.assertTrue(original_token)
            words = "Can you check the second case?"
            status, body = self.post(review_page.PAGE_PATH, case.card_body(snapshot, item.key, "question", words))
            self.assertEqual(status, 200, body)
            updated = cache.get().records[item.key]
            self.assertEqual(updated.state, review_items.ReviewState.WAITING)
            self.assertEqual(review_items.source_path(updated), review_items.source_path(item))
            self.assertIn("Superseded followup: " + request.words, [event.words for event in updated.log])
            self.assertIn("question: " + words, [event.words for event in updated.log])
            details = review_items.submission_origin(updated)
            assert isinstance(details, new_item_dialog.ReviewSubmission)
            token = reply_tokens.derive(details.reply_proof)
            self.assertTrue(token)
            self.assertNotEqual(original_token, token)
            self.assertNotEqual(original.reply_token_nonce, details.reply_token_nonce)
            delivery = cast(change_page.MessageDelivery, deliver.call_args.args[0])
            self.assertNotIn(token, delivery.message)
            self.assertIn("--question " + token + " --machine mac", change_page.outbound_review_message(delivery).text)

    def test_page_capture_rejects_non_page_addresses_before_launch(self) -> None:
        self.server.verified_tailscale_address = "127.0.0.1"
        token = self.server.new_item_token
        with patch.object(serve, "page_screenshot", autospec=True) as capture:
            for place in ("//example.invalid/", "/console/review/new-item", "/console/review.html?index", "/%2fconsole/review.html"):
                with self.subTest(place=place):
                    body = json.dumps({"token": token, "page_capture": {
                        "place": place, "width": 876, "height": 640, "dark": True}}).encode()
                    self.assertEqual(self.new_item_http(body).status, 400)
            with patch.object(shutil, "which", return_value=None):
                body = json.dumps({"token": token, "page_capture": {
                    "place": "/console/review.html", "width": 876, "height": 640, "dark": True}}).encode()
                self.assertEqual(self.new_item_http(body).status, 503)
        capture.assert_not_called()

    def test_bug_send_writes_one_page_state_record_per_draft(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        place = "/console/review.html?show=all#owner=builds"
        url = f"http://127.0.0.1:{self.port}{place}"
        image = base64.b64decode(PNG)
        report_folder = case.root / "page-states"
        with (patch.object(self.server, "root", case.root), patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status), patch.object(change_page, "send_to_staff"),
              patch.object(review_submissions, "this_machine", return_value="natedev"),
              patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=image) as capture):
            for checked in (False, True):
                with self.subTest(checked=checked):
                    viewport = new_item_dialog.PageViewport(1280, 800, True)
                    state = new_item_dialog.BugPageState(
                        url, None, 2.4, ("Could not load brain.",),
                        (new_item_dialog.PageScriptError("Error", url, 9, 3, "2026-11-18T16:05:00Z"),),
                        "State Open · Owner builds", False, "Review")
                    data = new_item_dialog.PageDataEvidence("Review", (("Item", "State"), ("Console", "Open")))
                    request = replace(case.submission_request(new_item_dialog.SubmissionKind.BUG, owner.id),
                                      page="review", request_id="r2-" + uuid4().hex, evidence=data, page_state=state)
                    fields = {**asdict(request), "token": self.server.new_item_token}
                    if checked:
                        key = self.start_page_capture(place, 1280, 800)
                        fields["capture_key"] = key
                        self.assertEqual(self.page_capture_preview(key), HttpReply(200, "image/png", image))
                    response = self.new_item_http(json.dumps(fields).encode(), finished=True)
                    self.assertEqual(response.status, 200, response.body)
                    saved = cast(dict[str, str], json.loads(response.body))
                    item = cache.get().records[saved["key"]]
                    origin = review_items.submission_origin(item)
                    assert isinstance(origin, new_item_dialog.ReviewSubmission)
                    self.assertEqual(origin.evidence, data)
                    path = report_folder / (origin.request_id + ".json")
                    record = cast(dict[str, object], json.loads(path.read_text()))
                    self.assertIsNotNone(datetime.fromisoformat(str(record["filed_at"])).utcoffset())
                    self.assertEqual(record, {
                        "request_id": origin.request_id, "item_key": item.key, "filed_at": record["filed_at"],
                        "machine": "natedev", "page": "review", "url": url,
                        **({"viewport": asdict(viewport)} if checked else {}),
                        "render_seconds": 2.4, "notices": ["Could not load brain."],
                        "script_errors": [asdict(error) for error in state.script_errors],
                        "page_data": {"title": "Review", "rows": [["Item", "State"], ["Console", "Open"]]},
                        "screenshot": review_submissions.PAGE_SCREENSHOT.id if checked else None, "words": request.words,
                    })
                    before = path.stat()
                    retry = {**fields, "page_state": asdict(replace(state, render_seconds=3.5))}
                    again = self.new_item_http(json.dumps(retry).encode(), finished=True)
                    self.assertEqual(again.status, 200, again.body)
                    self.assertEqual(json.loads(again.body)["key"], saved["key"])
                    self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
                    self.assertEqual(json.loads(path.read_text()), record)
                    self.assertEqual(self.request("/page-states/" + path.name).status, 404)
                    self.assertEqual(capture.call_count, int(checked))
            capture.assert_called_once_with("/test/firefox", url, new_item_dialog.PageCaptureRequest(place, 1280, 800, True))
        self.assertEqual(len(list(report_folder.glob("*.json"))), 2)
        self.assertEqual(len(investigation.records(owner.id)), 2)

    def page_bug_fixture(self, *, screenshot: bool = False
                         ) -> tuple[Path, review_page.ReviewSnapshotCache, new_item_dialog.NewItemRequest]:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        _ = self.enterContext(patch.object(self.server, "review_cache", cache))
        _ = self.enterContext(patch.object(self.server, "delivery_status", case.status))
        _ = self.enterContext(patch.object(review_submissions, "this_machine", return_value="natedev"))
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.BUG, owner.id),
                          page="review", request_id="r2-" + uuid4().hex,
                          evidence=new_item_dialog.PageDataEvidence("Review", (("Item", "State"),)),
                          page_state=new_item_dialog.BugPageState(
                              f"http://127.0.0.1:{self.port}/console/review.html#owner=builds",
                              new_item_dialog.PageViewport(1280, 800, True), 1.5, (), (), "Owner builds", screenshot))
        return case.root / "page-states", cache, request

    def test_published_builds_page_versions_each_write_in_one_second(self) -> None:
        from scripts.build_queue import memory_files

        output = self.root / "builds"
        output.mkdir()
        _ = conf.path("memory_feed").write_text(f"output_folder={output}\n", encoding="utf-8")
        page = output / memory_files.PAGE_NAME
        route = f"/builds/{memory_files.PAGE_NAME}"
        source = memory_files.PAGE_SOURCE.read_bytes()
        versions: list[str] = []
        for offset in (100_000_000, 200_000_000):
            with self.subTest(offset=offset):
                _ = page.write_bytes(source)
                mark = 1_700_000_000_000_000_000 + offset
                os.utime(page, ns=(mark, mark))
                reply = self.request(route)
                self.assertEqual(reply.status, 200)
                version = reply.headers.get("x-builds-page-version", "")
                self.assertTrue(version)
                self.assertIn(f'<meta name="builds-page-version" content="{version}">'.encode(), reply.body)
                versions.append(version)
                with (socket.create_connection(("127.0.0.1", self.port), TEST_HANG_GUARD_SECS) as client,
                      client.makefile("rb") as head_reply):
                    client.sendall((f"HEAD {route} HTTP/1.0\r\n"
                                    "Host: 127.0.0.1\r\nConnection: close\r\n\r\n").encode())
                    raw = head_reply.read()
                head, delimiter, body = raw.partition(b"\r\n\r\n")
                self.assertTrue(delimiter)
                status_line, _, raw_headers = head.partition(b"\r\n")
                self.assertEqual(status_line.split()[1:2], [b"200"])
                self.assertEqual(parse_headers(BytesIO(raw_headers))["X-Builds-Page-Version"], version)
                self.assertEqual(len(body), 0, "HEAD sent a response body")
        self.assertEqual(len(set(versions)), 2)

    def test_only_the_published_aws_machines_path_matches_the_console_page(self) -> None:
        output = self.root / "AWS reports"
        output.mkdir()
        leaf = machines_page.PAGE_NAME
        _ = (output / leaf).write_text("<h1>AWS Machines</h1>", encoding="utf-8")
        _ = write_config_fixture(conf.path("aws_page"), f"output_folder={output}\n")
        self.assertEqual(serve.published_console_page(self.root, f"/AWS reports/{leaf}"), "aws_machines")
        self.assertEqual(serve.capture_page_key(self.root, f"/AWS%20reports/{leaf}"), "aws_machines")
        self.assertEqual(self.request(f"/AWS%20reports/{leaf}").body, b"<h1>AWS Machines</h1>")
        for path in (f"/{leaf}", f"/elsewhere/{leaf}", "/AWS reports/other.html"):
            with self.subTest(path=path):
                self.assertEqual(serve.published_console_page(self.root, path), "")

    def test_bug_captures_the_encoded_aws_page_path(self) -> None:
        from scripts.review_links.review_link import snapshot_url
        from scripts.review_links.test_review_submissions import PNG

        folder, cache, request = self.page_bug_fixture(screenshot=True)
        state = request.page_state
        assert state is not None
        _ = write_config_fixture(conf.path("console"), f"page.aws.owner={request.recipient_id}\n")
        output = self.root / "AWS reports"
        output.mkdir()
        _ = write_config_fixture(conf.path("aws_page"), f"output_folder={output}\n")
        page = output / aws_page.PAGE_NAME
        _ = page.write_text("<!doctype html><html><head><title>AWS</title></head><body>AWS reports</body></html>")
        url = snapshot_url(self.root, page, host="127.0.0.1", port=self.port)
        place = urlsplit(url).path
        self.assertIn("AWS%20reports", place)
        data = new_item_dialog.PageDataEvidence("AWS", (("AWS reports",),))
        request = replace(request, page="aws", evidence=data, page_state=replace(state, url=url))
        with (patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=base64.b64decode(PNG)) as capture,
              patch.object(change_page, "send_to_staff") as deliver):
            self.assertEqual(self.request(place).status, 200)
            capture_key = self.start_page_capture(place, 1280, 800)
            self.assertEqual(self.page_capture_preview(capture_key).body, base64.b64decode(PNG))
            fields = {**asdict(request), "token": self.server.new_item_token,
                      "capture_key": capture_key}
            response = self.new_item_http(json.dumps(fields).encode(), finished=True)
        self.assertEqual(response.status, 200, response.body)
        deliver.assert_called_once()
        capture.assert_called_once_with("/test/firefox", url, new_item_dialog.PageCaptureRequest(place, 1280, 800, True))
        key = cast(dict[str, str], json.loads(response.body))["key"]
        self.assertIn(b"Screenshot of AWS", cache.get().body(key))
        path, = folder.glob("*.json")
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(record["url"], url)
        self.assertEqual(record["screenshot"], review_submissions.PAGE_SCREENSHOT.id)

    def test_bug_send_trims_a_long_url_with_or_without_a_screenshot(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        folder, cache, request = self.page_bug_fixture()
        state = request.page_state
        assert state is not None
        prefix = f"http://127.0.0.1:{self.port}{review_page.PAGE_PATH}#q="
        url = prefix + "x" * (9_075 - len(prefix))
        limit = new_item_dialog.MAX_PAGE_URL_LENGTH
        self.assertGreater(len(url), limit)
        with (patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=base64.b64decode(PNG)) as capture,
              patch.object(change_page, "send_to_staff") as deliver):
            for checked in (False, True):
                with self.subTest(checked=checked):
                    attempt = replace(request, request_id="r2-" + uuid4().hex,
                                      page_state=replace(state, url=url, include_screenshot=checked))
                    fields = {**asdict(attempt), "token": self.server.new_item_token}
                    viewport = state.viewport
                    if checked:
                        capture_key = self.start_page_capture()
                        self.assertEqual(self.page_capture_preview(capture_key).status, 200)
                        fields["capture_key"] = capture_key
                        viewport = new_item_dialog.PageViewport(876, 640, True)
                    response = self.new_item_http(json.dumps(fields).encode(), finished=True)
                    self.assertEqual(response.status, 200, response.body)
                    key = cast(dict[str, str], json.loads(response.body))["key"]
                    origin = review_items.submission_origin(cache.get().records[key])
                    assert isinstance(origin, new_item_dialog.ReviewSubmission)
                    self.assertEqual(origin.page_state, replace(
                        state, url=url[:limit], viewport=viewport, include_screenshot=checked))
                    path = folder / (origin.request_id + ".json")
                    record = cast(dict[str, object], json.loads(path.read_text()))
                    self.assertEqual(record["url"], url[:limit])
                    self.assertEqual(record["screenshot"], review_submissions.PAGE_SCREENSHOT.id if checked else None)
        self.assertEqual(deliver.call_count, 2)
        capture.assert_called_once()

    def test_r2_bug_without_a_capture_key_uses_the_server_screenshot(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        folder, cache, request = self.page_bug_fixture(screenshot=True)
        state = request.page_state
        assert state is not None
        image = base64.b64decode(PNG)
        with (patch.object(change_page, "send_to_staff") as deliver,
              patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=image) as capture):
            reply = self.new_item_http(self.new_item_body(request), finished=True)
        self.assertEqual(reply.status, 200, reply.body)
        result = cast(dict[str, str], json.loads(reply.body))
        capture.assert_called_once_with("/test/firefox", state.url, new_item_dialog.PageCaptureRequest(
            "/console/review.html#owner=builds", 1280, 800, True))
        deliver.assert_called_once()
        path, = folder.glob("*.json")
        self.assertEqual(json.loads(path.read_text())["screenshot"], review_submissions.PAGE_SCREENSHOT.id)
        key = result["key"]
        self.assertIn(b"Screenshot of Review", cache.get().body(key))
        attachment = self.request(review_page.ATTACHMENT_PATH + "?" + urlencode(
            {"key": key, "id": review_submissions.PAGE_SCREENSHOT.id}))
        self.assertEqual(attachment, HttpReply(200, "image/png", image))

    def test_r2_bug_without_a_screenshot_keeps_the_reported_viewport(self) -> None:
        folder, cache, request = self.page_bug_fixture()
        state = request.page_state
        assert state is not None and state.viewport is not None
        with (patch.object(change_page, "send_to_staff"),
              patch.object(serve, "page_screenshot") as capture):
            reply = self.new_item_http(self.new_item_body(request), finished=True)
        self.assertEqual(reply.status, 200, reply.body)
        capture.assert_not_called()
        result = cast(dict[str, str], json.loads(reply.body))
        origin = review_items.submission_origin(cache.get().records[result["key"]])
        assert isinstance(origin, new_item_dialog.ReviewSubmission)
        self.assertEqual(origin.page_state, state)
        path, = folder.glob("*.json")
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(record["viewport"], asdict(state.viewport))
        self.assertIsNone(record["screenshot"])

    def test_r2_review_send_attaches_the_pending_capture_to_the_saved_item(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        server = self.server
        started, release, completed = (threading.Event() for _ in range(3))
        image = base64.b64decode(PNG)
        capture_page = server.capture_page

        def screenshot(_firefox: str, _url: str, _request: new_item_dialog.PageCaptureRequest) -> bytes:
            started.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise OSError("The fixture did not release Firefox.")
            return image

        def capture(key: str, firefox: str, url: str, request: new_item_dialog.PageCaptureRequest) -> None:
            try:
                capture_page(key, firefox, url, request)
            finally:
                completed.set()

        request = replace(case.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner.id),
                          page="review", request_id="r2-" + uuid4().hex,
                          evidence=new_item_dialog.PageDataEvidence("Review", (("Item", "State"),)))
        with (patch.object(server, "review_cache", cache),
              patch.object(server, "delivery_status", case.status),
              patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", side_effect=screenshot),
              patch.object(server, "capture_page", side_effect=capture),
              patch.object(change_page, "send_to_staff") as deliver):
            capture_key = self.start_page_capture()
            try:
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                fields = {**asdict(request), "token": server.new_item_token, "capture_key": capture_key}
                body = json.dumps(fields).encode()
                first = self.new_item_http(body, finished=True)
                self.assertEqual(first.status, 200, first.body)
                saved = cast(dict[str, str], json.loads(first.body))
                item = cache.get().records[saved["key"]]
                self.assertEqual(item.owner, owner.id)
                origin = review_items.submission_origin(item)
                assert isinstance(origin, new_item_dialog.ReviewSubmission)
                self.assertEqual(origin.images, ())
                retry = self.new_item_http(body, finished=True)
                self.assertEqual(retry.status, 200, retry.body)
                self.assertEqual(json.loads(retry.body)["key"], saved["key"])
                other = {**fields, "request_id": "r2-" + uuid4().hex}
                rejected = self.new_item_http(json.dumps(other).encode(), finished=True)
                self.assertEqual(rejected.status, 409, rejected.body)
                deliver.assert_called_once()
            finally:
                release.set()
                self.assertTrue(completed.wait(TEST_HANG_GUARD_SECS))
            self.assertEqual(list(cache.get().records), [saved["key"]])
            attachment = self.request(review_page.ATTACHMENT_PATH + "?" + urlencode(
                {"key": saved["key"], "id": review_submissions.PAGE_SCREENSHOT.id}))
            self.assertEqual(attachment, HttpReply(200, "image/png", image))

    def test_r2_bug_with_an_expired_or_failed_capture_keeps_page_details(self) -> None:
        folder, cache, request = self.page_bug_fixture(screenshot=True)
        state = request.page_state
        assert state is not None and state.viewport is not None
        server = self.server
        failed = server.page_captures.start(FIXTURE_CAPTURE)
        self.assertTrue(failed)
        self.assertIsNone(server.page_captures.complete(failed, new_item_dialog.NO_PAGE_SCREENSHOT))
        with patch.object(serve, "PAGE_CAPTURE_TTL_SECONDS", 0):
            expired = server.page_captures.start(FIXTURE_CAPTURE)
        self.assertTrue(expired)

        with (patch.object(change_page, "send_to_staff") as deliver,
              patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", side_effect=OSError("The screenshot is unavailable."))):
            for capture_key in (expired, failed):
                with self.subTest(capture_key=capture_key):
                    attempt = replace(request, request_id="r2-" + uuid4().hex)
                    fields = {**asdict(attempt), "token": server.new_item_token, "capture_key": capture_key}
                    body = json.dumps(fields).encode()
                    reply = self.new_item_http(body, finished=True)
                    self.assertEqual(reply.status, 200, reply.body)
                    saved = cast(dict[str, str], json.loads(reply.body))
                    item = cache.get().records[saved["key"]]
                    self.assertEqual(item.owner, request.recipient_id)
                    origin = review_items.submission_origin(item)
                    assert isinstance(origin, new_item_dialog.ReviewSubmission)
                    self.assertEqual((origin.page_state, origin.evidence, origin.images), (state, request.evidence, ()))
                    record = cast(dict[str, object], json.loads((folder / (origin.request_id + ".json")).read_text()))
                    self.assertEqual(record["viewport"], asdict(state.viewport))
                    self.assertIsNone(record["screenshot"])
                    retry = self.new_item_http(body, finished=True)
                    self.assertEqual(retry.status, 200, retry.body)
                    self.assertEqual(json.loads(retry.body)["key"], saved["key"])
        self.assertEqual(deliver.call_count, 2)
        self.assertEqual(len(cache.get().records), 2)

    def test_capture_key_from_before_a_restart_expires_without_filing(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        _, cache, request = self.page_bug_fixture()
        server = self.server
        with (patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=base64.b64decode(PNG)),
              patch.object(change_page, "send_to_staff") as deliver):
            capture_key = self.start_page_capture()
            self.assertEqual(self.page_capture_preview(capture_key).status, 200)
            old_token = server.new_item_token
            with (patch.object(server, "new_item_token", "new-token"),
                  patch.object(server, "page_captures", serve.PageCaptures(self.root / ".review-link-empty-captures"))):
                for kind in (new_item_dialog.SubmissionKind.QUESTION, new_item_dialog.SubmissionKind.INVESTIGATION):
                    with self.subTest(kind=kind):
                        attempt = replace(request, kind=kind, page_state=None, request_id="r6-" + uuid4().hex)
                        fields = {**asdict(attempt), "token": old_token, "capture_key": capture_key}
                        stale = self.new_item_http(json.dumps(fields).encode(), finished=True)
                        self.assertEqual(stale.status, 403, stale.body)
                        self.assertEqual(json.loads(stale.body)["code"], "stale_token")
                        fields["token"] = server.new_item_token
                        reply = self.new_item_http(json.dumps(fields).encode(), finished=True)
                        self.assertEqual(reply.status, 409, reply.body)
                        self.assertEqual(json.loads(reply.body), {
                            "code": "screenshot_expired", "error": "The screenshot expired."})
            self.assertEqual(cache.get().records, {})
            deliver.assert_not_called()

    def test_capture_keys_keep_ready_images_and_resume_pending_previews_after_restart(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        _, cache, request = self.page_bug_fixture()
        server = self.server
        image = base64.b64decode(PNG)
        with (patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=image) as screenshot,
              patch.object(change_page, "send_to_staff") as deliver):
            ready = self.start_page_capture()
            self.assertEqual(self.page_capture_preview(ready).body, image)
            pending = server.page_captures.start(FIXTURE_CAPTURE)
            self.assertTrue(pending)
            old_token = server.new_item_token
            with (patch.object(server, "new_item_token", secrets.token_urlsafe()),
                  patch.object(server, "page_captures", serve.PageCaptures(self.root / serve.PAGE_CAPTURE_FOLDER))):
                self.assertEqual(self.page_capture_preview(pending, token=old_token).status, 403)
                self.assertEqual(screenshot.call_count, 1)
                self.assertEqual(self.page_capture_preview(ready).body, image)
                self.assertEqual(screenshot.call_count, 1)
                with server.capture_slot, patch.object(serve, "PAGE_CAPTURE_PREVIEW_SECONDS", 0):
                    for _ in range(2):
                        self.assertEqual(self.page_capture_preview(pending).status, 202)
                    self.assertEqual(screenshot.call_count, 1)
                self.assertEqual(self.page_capture_preview(pending), HttpReply(200, "image/png", image))
                self.assertEqual(screenshot.call_count, 2)
                screenshot.assert_called_with(
                    "/test/firefox", f"http://127.0.0.1:{self.port}{FIXTURE_CAPTURE.place}", FIXTURE_CAPTURE)
                attempt = replace(request, request_id="r6-" + uuid4().hex)
                body = json.dumps({**asdict(attempt), "token": server.new_item_token, "capture_key": pending}).encode()
                filed = self.new_item_http(body, finished=True)
                self.assertEqual(filed.status, 200, filed.body)
                retry = self.new_item_http(body, finished=True)
                self.assertEqual(retry.status, 200, retry.body)
                self.assertEqual(json.loads(retry.body)["key"], json.loads(filed.body)["key"])
                self.assertEqual(self.page_capture_preview(pending).status, 404)
                key = cast(dict[str, str], json.loads(filed.body))["key"]
                attachment = self.request(review_page.ATTACHMENT_PATH + "?" + urlencode(
                    {"key": key, "id": review_submissions.PAGE_SCREENSHOT.id}))
                self.assertEqual(attachment, HttpReply(200, "image/png", image))
            self.assertEqual(len(cache.get().records), 1)
            deliver.assert_called_once()

    def test_send_binds_the_capture_before_the_browser_leaves(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        folder, cache, request = self.page_bug_fixture()
        server = self.server
        image = base64.b64decode(PNG)
        attempt = replace(request, request_id="r6-" + uuid4().hex)
        pending = attempt.request_id + uuid4().hex
        fields = {**asdict(attempt), "token": server.new_item_token, "capture_key": pending,
                  "defer_capture": asdict(FIXTURE_CAPTURE)}
        body = json.dumps(fields).encode()
        url = f"http://127.0.0.1:{self.port}{FIXTURE_CAPTURE.place}"
        with (patch.object(change_page, "send_to_staff") as deliver,
              patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(server, "launch_page_capture") as launch,
              patch.object(serve, "page_screenshot", return_value=image)):
            foreign = json.dumps({**fields, "request_id": "r6-" + uuid4().hex}).encode()
            self.assertEqual(self.new_item_http(foreign, finished=True).status, 400)
            malformed = json.dumps({**fields, "capture_key": pending[:-1] + "x"}).encode()
            self.assertEqual(self.new_item_http(malformed, finished=True).status, 400)
            self.assertEqual(cache.get().records, {})
            launch.assert_not_called()

            first = self.new_item_http(body, finished=True)
            self.assertEqual(first.status, 200, first.body)
            saved = cast(dict[str, str], json.loads(first.body))
            self.assertEqual(len(cache.get().records), 1)
            viewport = new_item_dialog.PageViewport(876, 640, True)
            start = json.dumps({"token": server.new_item_token, "capture_key": pending,
                                "page_capture": asdict(FIXTURE_CAPTURE)}).encode()
            self.assertEqual(self.new_item_http(start).status, 202)
            launch.assert_called_once_with(pending, "/test/firefox", FIXTURE_CAPTURE, queued=False)
            server.page_captures.discard(pending)
            stored = serve.PageCaptures(self.root / serve.PAGE_CAPTURE_FOLDER)
            saved_key = next(iter(stored.bound_work()))[2]
            self.assertEqual(saved_key.item_id, saved["id"])
            with self.assertRaises(new_item_dialog.SubmissionConflict):
                _ = stored.bind(pending, attempt.request_id, replace(saved_key, item_id="I999"))

            server.capture_page(pending, "/test/firefox", url, FIXTURE_CAPTURE)
            unbound = server.page_captures.start(FIXTURE_CAPTURE)
            self.assertIsNone(server.page_captures.complete(unbound, new_item_dialog.PageScreenshot(image, viewport)))
            self.assertEqual(self.page_capture_preview(unbound), HttpReply(200, "image/png", image))
            restored = serve.PageCaptures(self.root / serve.PAGE_CAPTURE_FOLDER)
            with patch.object(server, "page_captures", restored):
                filed_retry = self.new_item_http(body, finished=True)
                self.assertEqual(filed_retry.status, 200, filed_retry.body)
                self.assertEqual(json.loads(filed_retry.body)["key"], saved["key"])
            attachment = self.request(review_page.ATTACHMENT_PATH + "?" + urlencode(
                {"key": saved["key"], "id": review_submissions.PAGE_SCREENSHOT.id}))
            self.assertEqual(attachment, HttpReply(200, "image/png", image))
            self.assertEqual(len(cache.get().records), 1)
            self.assertEqual(len(investigation.records(request.recipient_id)), 1)
            case, = folder.glob("*.json")
            fields = cast(dict[str, object], json.loads(case.read_text()))
            self.assertEqual(fields["screenshot"], review_submissions.PAGE_SCREENSHOT.id)
            self.assertEqual(fields["viewport"], asdict(viewport))
            deliver.assert_called_once()

    def test_list_retry_keeps_the_capture_after_a_lost_send_response(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        _, cache, request = self.page_bug_fixture()
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={request.recipient_id}\n")
        server = self.server
        page_capture = replace(FIXTURE_CAPTURE, place=list_page.PAGE_PATH)
        url = f"http://127.0.0.1:{self.port}{page_capture.place}"
        attempt = replace(request, page="list", kind=new_item_dialog.SubmissionKind.INVESTIGATION,
                          request_id="r6-" + uuid4().hex, page_state=None,
                          page_recipient=new_item_dialog.PageRecipient.SELECTED)
        key = attempt.request_id + uuid4().hex
        token = server.new_item_token
        body = json.dumps({**asdict(attempt), "token": token, "capture_key": key,
                           "defer_capture": asdict(page_capture)}).encode()
        handler = cast(type[serve.ReviewLinksHandler], server.RequestHandlerClass)
        respond = handler.json_response
        lost: dict[str, object] = {}
        image = base64.b64decode(PNG)

        def lose_first_reply(active: serve.ReviewLinksHandler, status: int, fields: Mapping[str, object]) -> None:
            if active.path == review_page.NEW_ITEM_PATH and status == 200 and "key" in fields and not lost:
                lost.update(fields)
                active.close_connection = True
                return
            respond(active, status, fields)

        with (patch.object(change_page, "send_to_staff") as deliver,
              patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(server, "launch_page_capture") as launch,
              patch.object(serve, "page_screenshot", return_value=image),
              patch.object(handler, "json_response", lose_first_reply)):
            start = json.dumps({"token": token, "capture_key": key, "page_capture": asdict(page_capture)}).encode()
            self.assertEqual(self.new_item_http(start).status, 202)
            with self.assertRaises(RemoteDisconnected):
                _ = self.new_item_http(body, finished=True)
            retry = self.new_item_http(body, finished=True)
            self.assertEqual(retry.status, 200, retry.body)
            confirmed = cast(dict[str, str], json.loads(retry.body))
            self.assertEqual(confirmed["key"], lost["key"])
            changed = json.dumps({**asdict(attempt), "token": token, "capture_key": key,
                                  "defer_capture": asdict(page_capture), "words": "Changed after Send."}).encode()
            replay = self.new_item_http(changed, finished=True)
            self.assertEqual(replay.status, 409, replay.body)
            self.assertEqual(json.loads(replay.body)["key"], confirmed["key"])
            launch.assert_called_once_with(key, "/test/firefox", page_capture, queued=False)
            server.capture_page(key, "/test/firefox", url, page_capture)
            attachment = self.request(review_page.ATTACHMENT_PATH + "?" + urlencode(
                {"key": confirmed["key"], "id": review_submissions.PAGE_SCREENSHOT.id}))
            self.assertEqual(attachment, HttpReply(200, "image/png", image))
            self.assertEqual(len(cache.get().records), 1)
            self.assertEqual(len(investigation.records(request.recipient_id)), 1)
            self.assertEqual(the_list.records(request.recipient_id), [])
            deliver.assert_called_once()

    def test_deferred_capture_failure_keeps_the_item_and_page_data(self) -> None:
        _, cache, request = self.page_bug_fixture()
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={request.recipient_id}\n")
        with (patch.object(change_page, "send_to_staff") as deliver,
              patch.object(self.server, "launch_page_capture") as launch):
            for page, prefix, kind, path in (
                    ("review", "r6-", new_item_dialog.SubmissionKind.BUG, review_page.PAGE_PATH),
                    ("list", "r2-", new_item_dialog.SubmissionKind.INVESTIGATION, list_page.PAGE_PATH)):
                other_page = list_page.PAGE_PATH if page == "review" else review_page.PAGE_PATH
                for failure, capture in (
                        ("viewport", asdict(replace(FIXTURE_CAPTURE, place=path, width=239))),
                        ("long address", asdict(replace(FIXTURE_CAPTURE, place=path + "?q=" + "a" * 2048))),
                        ("invalid address", asdict(replace(FIXTURE_CAPTURE, place="//example.invalid"))),
                        ("different page", asdict(replace(FIXTURE_CAPTURE, place=other_page))),
                        ("invalid capture object", "invalid")):
                    with self.subTest(page=page, failure=failure):
                        attempt = replace(request, page=page, kind=kind, request_id=prefix + uuid4().hex,
                                          words=f"Report the {failure}.",
                                          page_state=request.page_state if page == "review" else None)
                        key = attempt.request_id + uuid4().hex
                        body = json.dumps({**asdict(attempt), "token": self.server.new_item_token, "capture_key": key,
                                           "defer_capture": capture}).encode()
                        response = self.new_item_http(body, finished=True)
                        self.assertEqual(response.status, 200, response.body)
                        saved = cast(dict[str, str], json.loads(response.body))
                        details = review_items.submission_origin(cache.get().records[saved["key"]])
                        assert isinstance(details, new_item_dialog.ReviewSubmission)
                        self.assertEqual((details.evidence, details.images), (attempt.evidence, ()))
                        self.assertEqual(details.capture_intent, new_item_dialog.DeferredCaptureIntent(
                            key, attempt.request_id, new_item_dialog.UnavailableCapture.UNAVAILABLE))
                        self.assertEqual(self.page_capture_preview(key).status, 404)
                        retry = self.new_item_http(body, finished=True)
                        self.assertEqual(retry.status, 200, retry.body)
                        confirmed = cast(dict[str, str], json.loads(retry.body))
                        self.assertEqual((confirmed["id"], confirmed["key"]), (saved["id"], saved["key"]))
            launch.assert_not_called()
            self.assertEqual(deliver.call_count, 10)
            self.assertEqual(the_list.records(request.recipient_id), [])

    def test_restart_finds_capture_intent_before_a_lookup_or_receipt_exists(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        folder, cache, request = self.page_bug_fixture()
        server = self.server
        attempt = replace(request, request_id="r6-" + uuid4().hex)
        key = attempt.request_id + uuid4().hex
        body = json.dumps({**asdict(attempt), "token": server.new_item_token, "capture_key": key,
                           "defer_capture": asdict(FIXTURE_CAPTURE)}).encode()
        image = base64.b64decode(PNG)
        with (patch.object(change_page, "send_to_staff") as deliver,
              patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(server, "launch_page_capture") as launch,
              patch.object(serve, "page_screenshot", return_value=image)):
            with patch.object(server, "process_committed_capture"):
                response = self.new_item_http(body, finished=True)
            self.assertEqual(response.status, 200, response.body)
            saved_key = cast(dict[str, str], json.loads(response.body))["key"]
            details = review_items.submission_origin(cache.get().records[saved_key])
            assert isinstance(details, new_item_dialog.ReviewSubmission)
            receipt = review_submissions.image_folder(details).parent / "receipt.json"
            _ = receipt.rename(receipt.with_name("pending.json"))
            store = self.root / serve.PAGE_CAPTURE_FOLDER
            self.assertFalse((store / key).exists())
            with patch.object(server, "page_captures", serve.PageCaptures(store)):
                server.recover_page_captures()
                launch.assert_called_once_with(key, "/test/firefox", FIXTURE_CAPTURE, queued=False)
                with patch.object(review_submissions, "record_deferred_bug_screenshot", side_effect=OSError("Case write")):
                    server.capture_page(key, "/test/firefox", f"http://127.0.0.1:{self.port}{FIXTURE_CAPTURE.place}",
                                        FIXTURE_CAPTURE)
                ready, = server.page_captures.bound_work()
                self.assertIsInstance(ready[1], new_item_dialog.PageScreenshot)
                proof = store / key
                header, _, png = proof.read_bytes().partition(b"\n")
                values = cast(dict[str, object], json.loads(header))
                _ = proof.write_bytes(json.dumps({**values, "expires": 0}).encode() + b"\n" + png)
                restored = serve.PageCaptures(store)
                ready_key, ready_image, target = restored.bound_work()[0]
                assert isinstance(ready_image, new_item_dialog.PageScreenshot)
                with patch.object(server, "page_captures", restored):
                    server.finish_bound_capture(ready_key, target, ready_image)
                    self.assertEqual(restored.bound_work(), ())
                attachment = self.request(review_page.ATTACHMENT_PATH + "?" + urlencode(
                    {"key": saved_key, "id": review_submissions.PAGE_SCREENSHOT.id}))
                self.assertEqual(attachment, HttpReply(200, "image/png", image))
            report, = folder.glob("*.json")
            self.assertEqual(json.loads(report.read_text())["screenshot"], review_submissions.PAGE_SCREENSHOT.id)
            self.assertEqual(len(investigation.records(request.recipient_id)), 1)
            deliver.assert_called_once()

    def test_bug_evidence_write_failure_keeps_the_success_and_delivery(self) -> None:
        folder, cache, request = self.page_bug_fixture()
        events: list[str] = []
        create_text = atomic_file.create_text_atomic

        def fail_case_write(path: Path, text: str) -> bool:
            if path.parent == folder:
                events.append("evidence")
                raise OSError("The case write failed.\nFixture detail.")
            return create_text(path, text)

        def delivered(_owner: staff.StaffId, _sender: str, _message: str, **_options: str) -> None:
            events.append("delivery")

        errors = StringIO()
        with (patch.object(atomic_file, "create_text_atomic", side_effect=fail_case_write),
              patch.object(change_page, "send_to_staff", side_effect=delivered),
              redirect_stderr(errors)):
            response = self.new_item_http(self.new_item_body(request), finished=True)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(events, ["delivery", "evidence"])
        key = cast(dict[str, str], json.loads(response.body))["key"]
        self.assertIn(key, cache.get().records)
        self.assertEqual(len(investigation.records(request.recipient_id)), 1)
        self.assertEqual(list(folder.glob("*.json")), [])
        lines = [line for line in errors.getvalue().splitlines() if line.startswith("review-links Bug evidence")]
        self.assertEqual(lines, ["review-links Bug evidence for I1: The case write failed. Fixture detail."])

    def test_bug_sends_the_png_from_its_authorized_preview(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        folder, cache, request = self.page_bug_fixture(screenshot=True)
        started, release = threading.Event(), threading.Event()

        def capture(_firefox: str, _url: str, _request: new_item_dialog.PageCaptureRequest) -> bytes:
            started.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise OSError("The fixture did not release Firefox.")
            return base64.b64decode(PNG)

        with (patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", side_effect=capture) as launch,
              patch.object(change_page, "send_to_staff") as deliver,
              ThreadPoolExecutor(max_workers=1) as worker):
            capture_key = self.start_page_capture()
            try:
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                denied = self.new_item_http(query="?" + urlencode({"capture_key": capture_key}))
                self.assertEqual(denied.status, 403)
                self.assertEqual(self.page_capture_preview(capture_key, token="wrong").status, 403)
                client = worker.submit(self.page_capture_preview, capture_key)
                self.assertFalse(client.done())
                deliver.assert_not_called()
                self.assertEqual(list(folder.glob("*.json")), [])
            finally:
                release.set()
            preview = client.result(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(preview, HttpReply(200, "image/png", base64.b64decode(PNG)))
            fields = {**asdict(request), "token": self.server.new_item_token,
                      "capture_key": capture_key}
            response = self.new_item_http(json.dumps(fields).encode(), finished=True)
            self.assertEqual(response.status, 200, response.body)
            key = cast(dict[str, str], json.loads(response.body))["key"]
            self.assertEqual(self.page_capture_preview(capture_key).status, 404)
            expired = {**fields, "request_id": "r2-" + uuid4().hex}
            refusal = self.new_item_http(json.dumps(expired).encode())
            self.assertEqual((refusal.status, json.loads(refusal.body)["code"]), (409, "screenshot_expired"))
            reply = self.request(review_page.ATTACHMENT_PATH + "?" +
                                 urlencode({"key": key, "id": review_submissions.PAGE_SCREENSHOT.id}))
            self.assertEqual(reply, preview)
            launch.assert_called_once()
            deliver.assert_called_once()
        path, = folder.glob("*.json")
        self.assertEqual(json.loads(path.read_text())["screenshot"], review_submissions.PAGE_SCREENSHOT.id)
        self.assertIn(b"Screenshot of Review", cache.get().body(key))

    def test_bug_page_data_preserves_the_full_pasted_image_budget(self) -> None:
        folder, _, request = self.page_bug_fixture()
        state = request.page_state
        assert state is not None
        errors = (new_item_dialog.PageScriptError('"' * 512, '\\' * 2048, 9, 3, "2026-11-18T16:05:00Z"),) * 20
        state = replace(state, script_errors=errors, notices=('"' * 512,) * 48)
        data = new_item_dialog.PageDataEvidence("Review", (("😀" * 240,) * 16,) * 4)
        mb = 1024 * 1024
        images = tuple(new_item_dialog.PastedImage("image/png", base64.b64encode(byte * (size * mb)).decode())
                       for byte, size in ((b"A", 5), (b"B", 5), (b"C", 2)))
        request = replace(request, images=images, evidence=data, page_state=state, words="😀" * 20_000)
        payload = self.new_item_body(request)
        former_limit = 4 * ((new_item_dialog.MAX_TOTAL_IMAGE_BYTES + 2) // 3) + 256 * 1024
        self.assertGreater(len(payload), former_limit)
        identified = subprocess.CompletedProcess(["identify"], 0, stdout=b"PNG\n")
        with (patch.object(review_submissions, "_identify_image", return_value=identified) as identify,
              patch.object(change_page, "send_to_staff"), patch.object(serve, "page_screenshot") as screenshot):
            response = self.new_item_http(payload, finished=True)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(identify.call_count, len(images))
        screenshot.assert_not_called()
        path, = folder.glob("*.json")
        self.assertEqual(json.loads(path.read_text())["page_data"]["rows"], [list(row) for row in data.rows])

    def test_bug_owner_retry_adds_page_state_to_a_standard_bug_once(self) -> None:
        from scripts.review_links.test_review_submissions import EVIDENCE

        folder, cache, request = self.page_bug_fixture()
        standard = replace(request, page="", page_state=None, evidence=EVIDENCE)
        handler = cast(type[serve.ReviewLinksHandler], self.server.RequestHandlerClass)
        respond = handler.json_response
        lost: list[str] = []

        def lose_standard_reply(active: serve.ReviewLinksHandler, status: int, fields: Mapping[str, object]) -> None:
            if active.path == review_page.NEW_ITEM_PATH and status == 200 and not lost:
                lost.append(str(fields["key"]))
                active.close_connection = True
                return
            respond(active, status, fields)

        with (patch.object(handler, "json_response", lose_standard_reply),
              patch.object(change_page, "send_to_staff") as deliver):
            with self.assertRaises(RemoteDisconnected):
                _ = self.new_item_http(self.new_item_body(standard), finished=True)
            self.assertEqual(list(folder.glob("*.json")), [])
            response = self.new_item_http(self.new_item_body(request), finished=True)
            self.assertEqual(response.status, 200, response.body)
            key = cast(dict[str, str], json.loads(response.body))["key"]
            self.assertEqual(lost, [key])
            records = list(folder.glob("*.json"))
            self.assertEqual(len(records), 1, "The retry did not save the case for the Standard Bug.")
            path, = records
            before = path.stat()
            retry = self.new_item_http(self.new_item_body(request), finished=True)
            self.assertEqual(retry.status, 200, retry.body)
            self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
            deliver.assert_called_once()
        item = cache.get().records[key]
        origin = review_items.submission_origin(item)
        assert isinstance(origin, new_item_dialog.ReviewSubmission)
        self.assertEqual(origin.evidence, EVIDENCE)
        self.assertEqual(origin.bug_page_data, request.evidence)
        self.assertIn(b"Page state", cache.get().body(key))
        self.assertEqual(json.loads(path.read_text())["request_id"], origin.request_id)
        self.assertEqual(len(investigation.records(request.recipient_id)), 1)

    def test_list_bug_retry_adds_the_preview_to_the_saved_standard_bug(self) -> None:
        from scripts.review_links.test_review_submissions import EVIDENCE, PNG

        folder, cache, request = self.page_bug_fixture()
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={request.recipient_id}\n")
        state = request.page_state
        assert state is not None
        place = list_page.PAGE_PATH
        request = replace(request, page="list", page_state=replace(state, url=f"http://127.0.0.1:{self.port}{place}"),
                          evidence=new_item_dialog.PageDataEvidence("List", (("Item", "State"),)))
        standard = replace(request, page="", page_state=None, evidence=EVIDENCE)
        image = base64.b64decode(PNG)
        with (patch.object(change_page, "send_to_staff") as deliver,
              patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=image) as capture):
            first = self.new_item_http(self.new_item_body(standard), finished=True)
            self.assertEqual(first.status, 200, first.body)
            key = cast(dict[str, str], json.loads(first.body))["key"]
            self.assertEqual(list(folder.glob("*.json")), [])
            capture_key = self.start_page_capture(place)
            preview = self.page_capture_preview(capture_key)
            self.assertEqual(preview, HttpReply(200, "image/png", image))
            fields = {**asdict(request), "token": self.server.new_item_token, "capture_key": capture_key}
            retry = self.new_item_http(json.dumps(fields).encode(), finished=True)
            self.assertEqual(retry.status, 200, retry.body)
            self.assertEqual(json.loads(retry.body)["key"], key)
            capture.assert_called_once()
            deliver.assert_called_once()
        item = cache.get().records[key]
        origin = review_items.submission_origin(item)
        assert isinstance(origin, new_item_dialog.ReviewSubmission)
        self.assertEqual(origin.evidence, EVIDENCE)
        self.assertEqual(origin.bug_page_data, request.evidence)
        self.assertIn(b"Screenshot of List", cache.get().body(key))
        path, = folder.glob("*.json")
        self.assertEqual(json.loads(path.read_text())["screenshot"], review_submissions.PAGE_SCREENSHOT.id)
        self.assertEqual(self.request(review_page.ATTACHMENT_PATH + "?" + urlencode(
            {"key": key, "id": review_submissions.PAGE_SCREENSHOT.id})), preview)
        self.assertEqual(len(investigation.records(request.recipient_id)), 1)

    def test_list_investigation_retry_attaches_the_ready_preview(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={owner.id}\n")
        server = self.server
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.INVESTIGATION, owner.id),
                          page="list", request_id="r2-" + uuid4().hex,
                          evidence=new_item_dialog.PageDataEvidence("List", (("Item", "State"),)))
        handler = cast(type[serve.ReviewLinksHandler], server.RequestHandlerClass)
        respond = handler.json_response
        lost: dict[str, object] = {}

        def lose_first_reply(active: serve.ReviewLinksHandler, status: int, fields: Mapping[str, object]) -> None:
            if active.path == review_page.NEW_ITEM_PATH and status == 200 and not lost:
                lost.update(fields)
                active.close_connection = True
                return
            respond(active, status, fields)

        image = base64.b64decode(PNG)
        with (patch.object(server, "review_cache", cache),
              patch.object(server, "delivery_status", case.status),
              patch.object(handler, "json_response", lose_first_reply),
              patch.object(change_page, "send_to_staff") as deliver):
            with self.assertRaises(RemoteDisconnected):
                _ = self.new_item_http(self.new_item_body(request), finished=True)
            key = str(lost["key"])
            origin = review_items.submission_origin(cache.get().records[key])
            assert isinstance(origin, new_item_dialog.ReviewSubmission)
            self.assertEqual(origin.images, ())
            capture_key = server.page_captures.start(FIXTURE_CAPTURE)
            viewport = new_item_dialog.PageViewport(876, 640, True)
            _ = server.page_captures.complete(capture_key, new_item_dialog.PageScreenshot(image, viewport))
            preview = self.page_capture_preview(capture_key)
            self.assertEqual(preview, HttpReply(200, "image/png", image))
            fields = {**asdict(request), "token": server.new_item_token, "capture_key": capture_key}
            retry = self.new_item_http(json.dumps(fields).encode(), finished=True)
            self.assertEqual(retry.status, 200, retry.body)
            saved = cast(dict[str, str], json.loads(retry.body))
            for name in ("id", "key"):
                self.assertEqual(saved[name], lost[name])
            self.assertEqual(self.page_capture_preview(capture_key).status, 404)
            with server.page_captures.for_filing(capture_key) as capture:
                self.assertEqual(capture, new_item_dialog.FiledPageScreenshot(
                    new_item_dialog.PageScreenshot(image, viewport).digest))
            deliver.assert_called_once()
        self.assertEqual(len(investigation.records(owner.id)), 1)
        self.assertEqual(the_list.records(owner.id), [])
        self.assertEqual(self.request(review_page.ATTACHMENT_PATH + "?" + urlencode(
            {"key": key, "id": review_submissions.PAGE_SCREENSHOT.id})), preview)

    def test_interrupted_list_send_replaces_its_unfiled_preview(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={owner.id}\n")
        request = replace(case.submission_request(new_item_dialog.SubmissionKind.INVESTIGATION, owner.id),
                          page="list", request_id="r2-" + uuid4().hex)
        server = self.server
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        first_image = base64.b64decode(PNG)
        second_image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4WQAAAAASUVORK5CYII=")

        def send(image: bytes) -> HttpReply:
            key = server.page_captures.start(FIXTURE_CAPTURE)
            viewport = new_item_dialog.PageViewport(876, 640, True)
            _ = server.page_captures.complete(key, new_item_dialog.PageScreenshot(image, viewport))
            fields = {**asdict(request), "token": server.new_item_token, "capture_key": key}
            return self.new_item_http(json.dumps(fields).encode(), finished=True)

        with (patch.object(server, "review_cache", cache),
              patch.object(server, "delivery_status", case.status),
              patch.object(change_page, "send_to_staff") as deliver):
            with patch.object(investigation, "file", side_effect=OSError("The native write stopped.")):
                first = send(first_image)
                self.assertEqual(first.status, 503, first.body)
            self.assertEqual(investigation.records(owner.id), [])
            self.assertEqual(the_list.records(owner.id), [])
            deliver.assert_not_called()
            with (patch.object(server, "new_item_token", "new-token"),
                  patch.object(server, "page_captures", serve.PageCaptures(self.root / ".review-link-empty-captures"))):
                saved = send(second_image)
                self.assertEqual(saved.status, 200, saved.body)
                fields = cast(dict[str, str], json.loads(saved.body))
                retry = send(first_image)
                self.assertEqual(retry.status, 200, retry.body)
                self.assertEqual(json.loads(retry.body)["key"], fields["key"])
            deliver.assert_called_once()
        self.assertEqual(len(investigation.records(owner.id)), 1)
        self.assertEqual(the_list.records(owner.id), [])
        attachment = self.request(review_page.ATTACHMENT_PATH + "?" + urlencode(
            {"key": fields["key"], "id": review_submissions.PAGE_SCREENSHOT.id}))
        self.assertEqual(attachment, HttpReply(200, "image/png", second_image))

    def test_bug_owner_retry_keeps_the_evidence_of_the_file_already_saved(self) -> None:
        from scripts.review_links.test_review_submissions import EVIDENCE

        folder, cache, request = self.page_bug_fixture()
        request = replace(request, request_id="r6-" + uuid4().hex)
        standard = replace(request, page="", page_state=None, evidence=EVIDENCE)
        handler = cast(type[serve.ReviewLinksHandler], self.server.RequestHandlerClass)
        respond = handler.json_response
        lost: list[str] = []

        def lose_standard_reply(active: serve.ReviewLinksHandler, status: int, fields: Mapping[str, object]) -> None:
            if active.path == review_page.NEW_ITEM_PATH and status == 200 and not lost:
                lost.append(str(fields["key"]))
                active.close_connection = True
                return
            respond(active, status, fields)

        with (patch.object(handler, "json_response", lose_standard_reply),
              patch.object(change_page, "send_to_staff") as deliver):
            with self.assertRaises(RemoteDisconnected):
                _ = self.new_item_http(self.new_item_body(standard), finished=True)
            self.assertEqual(list(folder.glob("*.json")), [])
            repeated = self.new_item_http(self.new_item_body(standard), finished=True)
            self.assertEqual(repeated.status, 200, repeated.body)
            key = cast(dict[str, str], json.loads(repeated.body))["key"]
            response = self.new_item_http(self.new_item_body(request), finished=True)
            self.assertEqual(response.status, 409, response.body)
            conflict = cast(dict[str, str], json.loads(response.body))
            self.assertEqual(conflict["code"], "already_filed")
            self.assertEqual(conflict["key"], key)
            self.assertEqual(conflict["id"], cache.get().records[key].id.rsplit("/", 1)[-1])
            self.assertEqual(lost, [key])
            self.assertEqual(list(folder.glob("*.json")), [])
            deliver.assert_called_once()
        item = cache.get().records[key]
        origin = review_items.submission_origin(item)
        assert isinstance(origin, new_item_dialog.ReviewSubmission)
        self.assertEqual(origin.evidence, EVIDENCE)
        self.assertIsNone(origin.bug_page_data)
        self.assertNotIn(b"Page state", cache.get().body(key))
        self.assertEqual(len(investigation.records(request.recipient_id)), 1)

    def test_edited_send_resumes_publication_of_the_first_bug(self) -> None:
        folder, cache, request = self.page_bug_fixture()
        request = replace(request, request_id="r6-" + uuid4().hex)
        with patch.object(change_page, "send_to_staff") as send:
            with patch.object(cache, "publish_item", side_effect=OSError("Publication interrupted.")):
                failed = self.new_item_http(self.new_item_body(request), finished=True)
            self.assertEqual(failed.status, 503)
            first, = investigation.records(request.recipient_id)
            send.assert_not_called()
            edited = replace(request, kind=new_item_dialog.SubmissionKind.INVESTIGATION,
                             words="These are the edited words.", page_state=None)
            second = self.new_item_http(self.new_item_body(edited), finished=True)
            self.assertEqual(second.status, 409)
            self.assertEqual(json.loads(second.body)["id"], first.id)
            self.assertEqual(json.loads(second.body)["code"], "already_filed")
            recipient, _, message = cast(tuple[staff.StaffId, str, str], send.call_args.args)
            self.assertEqual(recipient, request.recipient_id)
            self.assertIn(request.words, message)
            self.assertNotIn(edited.words, message)
            self.assertEqual(investigation.records(request.recipient_id), [first])
            report, = folder.glob("*.json")
            self.assertEqual(json.loads(report.read_text())["words"], request.words)
            resent = replace(edited, request_id="r6-" + uuid4().hex)
            confirmed = self.new_item_http(self.new_item_body(resent), finished=True)
            self.assertEqual(confirmed.status, 200)
        self.assertEqual(send.call_count, 2)

    def test_new_item_reports_delivery_failure_and_retries_the_saved_item(self) -> None:
        _folder, _cache, request = self.page_bug_fixture()
        _ = write_config_fixture(conf.path("review_links"), "delivery_pause_seconds=0\n")
        request = replace(request, request_id="r6-" + uuid4().hex)
        with patch.object(change_page, "send_to_staff", side_effect=OSError("Delivery interrupted.")) as send:
            failed = self.new_item_http(self.new_item_body(request), finished=True)
        self.assertEqual(failed.status, 503)
        self.assertEqual(json.loads(failed.body)["error"], "Delivery interrupted. Try Send again.")
        first, = investigation.records(request.recipient_id)
        with patch.object(change_page, "send_to_staff") as retry:
            confirmed = self.new_item_http(self.new_item_body(request), finished=True)
        self.assertEqual(confirmed.status, 200)
        self.assertEqual(json.loads(confirmed.body)["id"], first.id)
        self.assertEqual(investigation.records(request.recipient_id), [first])
        retry.assert_called_once_with(*send.call_args.args, **send.call_args.kwargs)

    def test_new_item_retry_recovers_a_confirmed_delivery_without_sending_again(self) -> None:
        _folder, _cache, request = self.page_bug_fixture()
        request = replace(request, request_id="r6-" + uuid4().hex)
        body = self.new_item_body(request)
        with patch.object(change_page, "send_to_staff", return_value=None) as send:
            with patch.object(self.server.delivery_status, "finish_intent_locked",
                              side_effect=OSError("Delivery cleanup interrupted.")):
                interrupted = self.new_item_http(body, finished=True)
            self.assertEqual(interrupted.status, 503)
            first, = investigation.records(request.recipient_id)
            confirmed = self.new_item_http(body, finished=True)
        self.assertEqual(confirmed.status, 200, confirmed.body)
        self.assertEqual(json.loads(confirmed.body)["id"], first.id)
        self.assertEqual(investigation.records(request.recipient_id), [first])
        send.assert_called_once()

    def start_page_capture(self, place: str = "/console/review.html?show=all",
                           width: int = 876, height: int = 640) -> str:
        body = json.dumps({"token": self.server.new_item_token, "page_capture": {
            "place": place, "width": width, "height": height, "dark": True}}).encode()
        reply = self.new_item_http(body)
        self.assertEqual(reply.status, 202, reply.body)
        return new_item_dialog.read_capture_key(cast(dict[str, object], json.loads(reply.body))["capture_key"])

    def page_capture_preview(self, key: str, *, token: str | None = None) -> HttpReply:
        headers = {"X-Review-Token": self.server.new_item_token if token is None else token}
        return self.new_item_http(query="?" + urlencode({"capture_key": key}), headers=headers)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for a real page capture.")
    def test_page_capture_keeps_a_png_at_the_browser_size(self) -> None:
        case = self.review_fixture()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        completed = threading.Event()
        images: list[new_item_dialog.PageScreenshot | new_item_dialog.NoPageScreenshot] = []
        complete = self.server.page_captures.complete

        def capture_done(key: str, image: new_item_dialog.PageScreenshot | new_item_dialog.NoPageScreenshot) -> None:
            _ = complete(key, image)
            images.append(image)
            completed.set()

        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server.page_captures, "complete", side_effect=capture_done)):
            _ = self.start_page_capture()
            self.assertTrue(completed.wait(TEST_HANG_GUARD_SECS))
        image, = images
        assert isinstance(image, new_item_dialog.PageScreenshot)
        self.assertEqual(image.png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual((int.from_bytes(image.png[16:20]), int.from_bytes(image.png[20:24])), (876, 640))

    def test_expired_preview_saves_nothing_and_the_next_preview_is_filed(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        image = base64.b64decode(PNG)
        server = self.server
        with (patch.object(server, "review_cache", cache), patch.object(server, "delivery_status", case.status),
              patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=image) as launch,
              patch.object(change_page, "send_to_staff") as send):
            for kind in (new_item_dialog.SubmissionKind.QUESTION, new_item_dialog.SubmissionKind.BUG):
                with self.subTest(kind=kind):
                    before = cache.get().records.copy()
                    sent = send.call_count
                    first = self.start_page_capture()
                    self.assertEqual(self.page_capture_preview(first).body, image)
                    server.page_captures.discard(first)
                    data = new_item_dialog.PageDataEvidence("Review", (("Visible row",),))
                    request = replace(case.submission_request(kind, owner.id), page="review", evidence=data)
                    fields = {**asdict(request), "token": server.new_item_token, "capture_key": first}
                    refusal = self.new_item_http(json.dumps(fields).encode(), finished=True)
                    self.assertEqual((refusal.status, json.loads(refusal.body)["code"]), (409, "screenshot_expired"))
                    self.assertEqual(cache.get().records, before)
                    self.assertEqual(send.call_count, sent)
                    second = self.start_page_capture()
                    self.assertEqual(self.page_capture_preview(second).body, image)
                    self.assertNotEqual(first, second)
                    response = self.new_item_http(json.dumps({**fields, "capture_key": second}).encode(), finished=True)
                    self.assertEqual(response.status, 200, response.body)
                    key = cast(dict[str, str], json.loads(response.body))["key"]
                    origin = review_items.submission_origin(cache.get().records[key])
                    assert isinstance(origin, new_item_dialog.ReviewSubmission)
                    self.assertEqual(origin.images, (review_submissions.PAGE_SCREENSHOT,))
                    self.assertEqual(origin.evidence, data if kind == new_item_dialog.SubmissionKind.BUG
                                     else new_item_dialog.NoEvidence())
                    reply = self.request(review_page.ATTACHMENT_PATH + "?" +
                                         urlencode({"key": key, "id": origin.images[0].id}))
                    self.assertEqual((reply.status, reply.body), (200, image))
            self.assertEqual(len(cache.get().records), 2)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(launch.call_count, 4)

    def test_lost_send_response_reuses_the_item_after_recapture_of_the_same_png(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        _, cache, request = self.page_bug_fixture(screenshot=True)
        request = replace(request, request_id="r6-" + uuid4().hex)
        image = base64.b64decode(PNG)
        handler = cast(type[serve.ReviewLinksHandler], self.server.RequestHandlerClass)
        respond = handler.json_response
        lost: list[Mapping[str, object]] = []

        def lose_first_reply(active: serve.ReviewLinksHandler, status: int, fields: Mapping[str, object]) -> None:
            if active.path == review_page.NEW_ITEM_PATH and status == 200 and not lost:
                lost.append(fields)
                active.close_connection = True
                return
            respond(active, status, fields)

        def send(key: str) -> HttpReply:
            fields = {**asdict(request), "token": self.server.new_item_token, "capture_key": key}
            return self.new_item_http(json.dumps(fields).encode(), finished=True)

        with (patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=image),
              patch.object(handler, "json_response", lose_first_reply),
              patch.object(change_page, "send_to_staff") as deliver):
            first = self.start_page_capture()
            self.assertEqual(self.page_capture_preview(first).body, image)
            with self.assertRaises(RemoteDisconnected):
                _ = send(first)
            self.assertEqual(self.page_capture_preview(first).status, 404)
            same_key = send(first)
            self.assertEqual(same_key.status, 200, same_key.body)
            second = self.start_page_capture()
            self.assertNotEqual(first, second)
            self.assertEqual(self.page_capture_preview(second).body, image)
            reply = send(second)
            self.assertEqual(reply.status, 200, reply.body)
            saved = cast(dict[str, str], json.loads(reply.body))
            self.assertEqual(saved["id"], lost[0]["id"])
            self.assertEqual(saved["key"], lost[0]["key"])
            self.assertEqual(json.loads(same_key.body)["id"], saved["id"])
            self.assertEqual(list(cache.get().records), [saved["key"]])
            self.assertEqual(len(investigation.records(request.recipient_id)), 1)
            deliver.assert_called_once()

    def test_lost_screenshot_reply_reports_the_filed_id_after_restart(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        _, cache, request = self.page_bug_fixture(screenshot=True)
        server = self.server
        handler = cast(type[serve.ReviewLinksHandler], server.RequestHandlerClass)
        respond = handler.json_response
        lost: list[Mapping[str, object]] = []

        def lose_first_reply(active: serve.ReviewLinksHandler, status: int, fields: Mapping[str, object]) -> None:
            if active.path == review_page.NEW_ITEM_PATH and status == 200 and not lost:
                lost.append(fields)
                active.close_connection = True
                return
            respond(active, status, fields)

        image = base64.b64decode(PNG)
        with (patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=image),
              patch.object(handler, "json_response", lose_first_reply),
              patch.object(change_page, "send_to_staff") as deliver):
            attempt = replace(request, request_id="r6-" + uuid4().hex)
            key = self.start_page_capture()
            self.assertEqual(self.page_capture_preview(key).status, 200)
            fields = {**asdict(attempt), "token": server.new_item_token, "capture_key": key}
            with self.assertRaises(RemoteDisconnected):
                _ = self.new_item_http(json.dumps(fields).encode(), finished=True)

            reopened = self.start_page_capture()
            self.assertNotEqual(reopened, key)
            self.assertEqual(self.page_capture_preview(reopened).status, 200)
            restarted_cache = review_page.ReviewSnapshotCache(cache.root, start_worker=False)
            with (patch.object(server, "new_item_token", "new-token"),
                  patch.object(server, "page_captures", serve.PageCaptures(self.root / ".review-link-empty-captures")),
                  patch.object(server, "review_cache", restarted_cache)):
                without_key = {**asdict(attempt), "token": server.new_item_token}
                malformed_key = {**fields, "kind": "invalid", "capture_key": "invalid"}
                self.assertEqual(self.new_item_http(json.dumps(malformed_key).encode(), finished=True).status, 400)
                for retry in (fields, {**fields, "capture_key": reopened},
                              {**fields, "kind": "invalid"}, without_key):
                    reply = self.new_item_http(json.dumps(retry).encode(), finished=True)
                    self.assertEqual(reply.status, 409, reply.body)
                    saved = cast(dict[str, str], json.loads(reply.body))
                    self.assertEqual(saved["code"], "already_filed")
                    self.assertEqual((saved["id"], saved["key"]), (lost[0]["id"], lost[0]["key"]))
            self.assertEqual(list(restarted_cache.get().records), [lost[0]["key"]])

        self.assertEqual(len(investigation.records(request.recipient_id)), 1)
        deliver.assert_called_once()

    def test_restart_recovery_requires_a_recorded_browser_draft_token(self) -> None:
        _, cache, request = self.page_bug_fixture()
        request = replace(request, request_id="r6-" + uuid4().hex)
        with patch.object(change_page, "send_to_staff") as deliver:
            first = self.new_item_http(self.new_item_body(request), finished=True)
            self.assertEqual(first.status, 200, first.body)
            deliver.assert_called_once()
        key = cast(dict[str, str], json.loads(first.body))["key"]
        origin = review_items.submission_origin(cache.get().records[key])
        assert isinstance(origin, new_item_dialog.ReviewSubmission)
        receipt = review_submissions.image_folder(origin).parent / "receipt.json"
        before = receipt.read_bytes()
        fields = {**asdict(request), "token": secrets.token_urlsafe(), "capture_key": uuid4().hex}
        with (patch.object(cache, "create_submission", wraps=cache.create_submission) as submit,
              patch.object(review_submissions, "locate_native_submission") as locate,
              patch.object(change_page, "send_to_staff") as deliver):
            reply = self.new_item_http(json.dumps(fields).encode(), finished=True)
            self.assertEqual(reply.status, 403, reply.body)
            self.assertEqual(json.loads(reply.body)["code"], "stale_token")
            submit.assert_called_once()
            locate.assert_not_called()
            deliver.assert_not_called()
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(list(cache.get().records), [key])
        self.assertEqual(len(investigation.records(request.recipient_id)), 1)

    def test_restart_uses_the_original_browser_token_after_a_wrapped_move(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        _, cache, request = self.page_bug_fixture(screenshot=True)
        request = replace(request, request_id="r6-" + uuid4().hex)
        server = self.server
        original_token = server.new_item_token
        digest = hashlib.sha256(original_token.encode("ascii")).hexdigest()
        screenshot = new_item_dialog.PageScreenshot(base64.b64decode(PNG), new_item_dialog.PageViewport(876, 640, True))
        with review_submissions.browser_draft_token(review_submissions.CurrentBrowserDraftToken(digest)):
            filed = cache.create_submission(request, screenshot=screenshot)
        fields = {**asdict(request), "token": original_token, "capture_key": server.page_captures.start(FIXTURE_CAPTURE)}

        child = staff.local_snapshot().by_id(request.recipient_id)
        assert child is not None and isinstance(child.doc, staff.LiveDoc)
        child_doc = child.doc.path
        parent_doc = child_doc.with_name("parent-production.md")
        _ = parent_doc.write_text(child_doc.read_text())
        parent = fixture_member("showrunner", name="parent", doc=staff.LiveDoc(local_machine(), parent_doc))
        _ = child_doc.write_text(child_doc.read_text().replace("— running.**", "— wrapped.**"))
        wrapped_child = replace(child, parent=staff.ReportsTo(parent.id), doc=staff.WrappedDoc(child.doc.machine, child_doc))
        members = staff.local_snapshot()
        wrapped = replace(members, rows=tuple(wrapped_child if row.id == child.id else row for row in members.rows))
        _ = staff.record_path().write_text(wrapped.encode())
        with (patch.object(tidy_wrapped, "STATE_DIR", record_store.state_root()),
              patch("scripts.copies.copies.remove_trees")):
            _ = tidy_wrapped.tidy(child.name, child_doc, identities=update_registration.QuestionStaff.for_record(wrapped))
        self.assertFalse(filed.saved.path.exists())

        moved_key = f"investigation/{parent.id}/{filed.saved.id}"
        restarted_cache = review_page.ReviewSnapshotCache(cache.root, start_worker=False)
        with (patch.object(server, "new_item_token", secrets.token_urlsafe()),
              patch.object(server, "page_captures", serve.PageCaptures(self.root / ".review-link-empty-captures")),
              patch.object(server, "review_cache", restarted_cache),
              patch.object(change_page, "send_to_staff") as deliver):
            unrelated = self.new_item_http(json.dumps({**fields, "token": secrets.token_urlsafe()}).encode(), finished=True)
            self.assertEqual(unrelated.status, 403, unrelated.body)
            self.assertEqual(json.loads(unrelated.body)["code"], "stale_token")
            deliver.assert_not_called()
            retry = self.new_item_http(json.dumps(fields).encode(), finished=True)
            self.assertEqual(retry.status, 409, retry.body)
            result = cast(dict[str, str], json.loads(retry.body))
            self.assertEqual((result["code"], result["id"], result["key"]), ("already_filed", filed.saved.id, moved_key))
            deliver.assert_called_once()
        receipt = review_submissions.image_folder(filed.saved).parent / "receipt.json"
        receipt_fields = cast(dict[str, object], json.loads(receipt.read_bytes()))
        self.assertEqual(receipt_fields["browser_token_digest"], digest)
        self.assertEqual(restarted_cache.get().records[moved_key].owner, parent.id)
        self.assertEqual([item.id for item in investigation.records(parent.id)], [filed.saved.id])

    def test_legacy_browser_receipt_resumes_delivery_at_once(self) -> None:
        self.check_legacy_browser_delivery_resume()

    def test_legacy_browser_receipt_resumes_when_local_staff_is_unreadable(self) -> None:
        self.check_legacy_browser_delivery_resume(local_staff_unreadable=True)

    def test_legacy_browser_pending_file_resumes_delivery_at_once(self) -> None:
        self.check_legacy_browser_delivery_resume(pending=True)

    def check_legacy_browser_delivery_resume(self, *, local_staff_unreadable: bool = False,
                                             pending: bool = False) -> None:
        from scripts.review_links.test_review_submissions import PNG

        _, cache, request = self.page_bug_fixture(screenshot=True)
        request = replace(request, request_id="r6-" + uuid4().hex)
        screenshot = new_item_dialog.PageScreenshot(base64.b64decode(PNG), new_item_dialog.PageViewport(876, 640, True))
        filed = cache.create_submission(request, screenshot=screenshot)
        receipt = review_submissions.image_folder(filed.saved).parent / "receipt.json"
        receipt_fields = cast(dict[str, object], json.loads(receipt.read_bytes()))
        self.assertNotIn("browser_token_digest", receipt_fields)
        pending_file = receipt.with_name("pending.json")
        if pending:
            _ = receipt.rename(pending_file)
        server = self.server
        fields = {**asdict(request), "token": server.new_item_token, "capture_key": server.page_captures.start(FIXTURE_CAPTURE)}
        restarted_cache = review_page.ReviewSnapshotCache(cache.root, start_worker=False)
        members = staff.snapshot()
        with (patch.object(server, "new_item_token", secrets.token_urlsafe()),
              patch.object(server, "page_captures", serve.PageCaptures(self.root / ".review-link-empty-captures")),
              patch.object(server, "review_cache", restarted_cache),
              patch.object(change_page, "send_to_staff") as deliver, ExitStack() as faults):
            if local_staff_unreadable:
                _ = faults.enter_context(patch.object(staff, "local_snapshot", side_effect=staff.StaffUnreadable(
                    "The local staff record is unreadable.")))
                _ = faults.enter_context(patch.object(staff, "authority_snapshot", return_value=members))
            reply = self.new_item_http(json.dumps(fields).encode(), finished=True)
            self.assertEqual(reply.status, 409, reply.body)
            saved = cast(dict[str, str], json.loads(reply.body))
            self.assertEqual((saved["code"], saved["id"], saved["key"]), ("already_filed", filed.saved.id, filed.key))
            deliver.assert_called_once()
        self.assertEqual(json.loads(receipt.read_bytes()), receipt_fields)
        self.assertFalse(pending_file.exists())
        self.assertEqual(list(restarted_cache.get().records), [filed.key])
        self.assertEqual(len(investigation.records(request.recipient_id)), 1)

    def test_restart_uses_the_original_token_after_native_cleanup_fails(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        _, cache, request = self.page_bug_fixture(screenshot=True)
        request = replace(request, request_id="r6-" + uuid4().hex)
        server = self.server
        capture_key = server.page_captures.start(FIXTURE_CAPTURE)
        screenshot = new_item_dialog.PageScreenshot(base64.b64decode(PNG), new_item_dialog.PageViewport(876, 640, True))
        _ = server.page_captures.complete(capture_key, screenshot)
        fields = {**asdict(request), "token": server.new_item_token, "capture_key": capture_key}
        unlink = Path.unlink
        failed_cleanup = False

        def unlink_native_temporary(path: Path, missing_ok: bool = False) -> None:
            nonlocal failed_cleanup
            target = atomic_file.temporary_target(path.name)
            if (not failed_cleanup and path.parent.name == "investigations" and target is not None
                    and (path.parent / target).is_file()):
                failed_cleanup = True
                raise OSError("The native temporary file cleanup failed.")
            unlink(path, missing_ok=missing_ok)

        with patch.object(change_page, "send_to_staff") as deliver:
            with patch.object(Path, "unlink", autospec=True, side_effect=unlink_native_temporary):
                first = self.new_item_http(json.dumps(fields).encode(), finished=True)
            self.assertTrue(failed_cleanup)
            self.assertEqual(first.status, 200, first.body)
            saved = cast(dict[str, str], json.loads(first.body))
            restarted_cache = review_page.ReviewSnapshotCache(cache.root, start_worker=False)
            with (patch.object(server, "new_item_token", secrets.token_urlsafe()),
                  patch.object(server, "page_captures", serve.PageCaptures(self.root / ".review-link-empty-captures")),
                  patch.object(server, "review_cache", restarted_cache)):
                retry = self.new_item_http(json.dumps(fields).encode(), finished=True)
                self.assertEqual(retry.status, 409, retry.body)
                recovered = cast(dict[str, str], json.loads(retry.body))
                self.assertEqual((recovered["code"], recovered["id"], recovered["key"]),
                                 ("already_filed", saved["id"], saved["key"]))
            deliver.assert_called_once()
        self.assertEqual(list(restarted_cache.get().records), [saved["key"]])
        self.assertEqual(len(investigation.records(request.recipient_id)), 1)

    def test_failed_native_write_keeps_the_same_screenshot_for_retry(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        folder, cache, request = self.page_bug_fixture(screenshot=True)
        image = base64.b64decode(PNG)
        create = record_store.create_record_text
        fail_write = True

        def create_bug(path: Path, text: str) -> None:
            nonlocal fail_write
            if fail_write and path.parent.name == "investigations":
                fail_write = False
                raise OSError("Fixture native write failed.")
            create(path, text)

        with (patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=image) as capture,
              patch.object(record_store, "create_record_text", side_effect=create_bug),
              patch.object(change_page, "send_to_staff") as deliver):
            capture_key = self.start_page_capture()
            preview = self.page_capture_preview(capture_key)
            self.assertEqual(preview, HttpReply(200, "image/png", image))
            fields = {**asdict(request), "token": self.server.new_item_token, "capture_key": capture_key}
            body = json.dumps(fields).encode()
            failed = self.new_item_http(body, finished=True)
            self.assertEqual(failed.status, 503, failed.body)
            self.assertEqual(investigation.records(request.recipient_id), [])
            self.assertEqual(list(folder.glob("*.json")), [])
            deliver.assert_not_called()
            self.assertEqual(self.page_capture_preview(capture_key), preview)
            response = self.new_item_http(body, finished=True)
            self.assertEqual(response.status, 200, response.body)
            key = cast(dict[str, str], json.loads(response.body))["key"]
            origin = review_items.submission_origin(cache.get().records[key])
            assert isinstance(origin, new_item_dialog.ReviewSubmission)
            self.assertEqual(origin.images, (review_submissions.PAGE_SCREENSHOT,))
            attachment = self.request(review_page.ATTACHMENT_PATH + "?" +
                                      urlencode({"key": key, "id": origin.images[0].id}))
            self.assertEqual(attachment, preview)
            self.assertEqual(len(investigation.records(request.recipient_id)), 1)
            self.assertEqual(self.page_capture_preview(capture_key).status, 404)
            capture.assert_called_once()
            deliver.assert_called_once()

    def test_failed_send_discards_the_staged_evidence(self) -> None:
        from scripts.review_links.test_review_submissions import PNG, identify_fixture_image

        report_folder, cache, request = self.page_bug_fixture(screenshot=True)
        request = replace(request, request_id="r6-" + uuid4().hex,
                          images=(new_item_dialog.PastedImage("image/png", PNG),))
        image = base64.b64decode(PNG)
        with (patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=image),
              patch.object(review_submissions, "_identify_image", side_effect=identify_fixture_image),
              patch.object(change_page, "send_to_staff") as deliver):
            capture = self.start_page_capture()
            self.assertEqual(self.page_capture_preview(capture).body, image)
            fields = {**asdict(request), "token": self.server.new_item_token, "capture_key": capture}
            with patch.object(record_store, "create_record_text", side_effect=OSError("Native write failed.")):
                failed = self.new_item_http(json.dumps(fields).encode(), finished=True)
            self.assertEqual(failed.status, 503, failed.body)
            self.assertEqual(self.page_capture_preview(capture).body, image)
            self.assertEqual(investigation.records(request.recipient_id), [])
            self.assertEqual(list(report_folder.glob("*.json")), [])
            folder = cache.root.parent / "review-attachments" / request.request_id
            self.assertEqual({path.name for path in folder.iterdir()}, {".lock"})
            deliver.assert_not_called()

            edited = replace(request, kind=new_item_dialog.SubmissionKind.INVESTIGATION, images=(),
                             page_state=None, evidence=new_item_dialog.NoEvidence())
            reply = self.new_item_http(self.new_item_body(edited), finished=True)
            self.assertEqual(reply.status, 200, reply.body)
            key = cast(dict[str, str], json.loads(reply.body))["key"]
            origin = review_items.submission_origin(cache.get().records[key])
            assert isinstance(origin, new_item_dialog.ReviewSubmission)
            self.assertEqual(origin.images, ())
            self.assertEqual(origin.evidence, new_item_dialog.NoEvidence())
            self.assertEqual(len(investigation.records(request.recipient_id)), 1)
            deliver.assert_called_once()

    def test_discard_during_a_failed_filing_releases_the_capture(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        folder, cache, request = self.page_bug_fixture(screenshot=True)
        started, release = threading.Event(), threading.Event()
        image = base64.b64decode(PNG)

        def fail_write(_path: Path, _text: str) -> None:
            started.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise OSError("The fixture did not release the native write.")
            raise OSError("The fixture native write failed.")

        with (patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", return_value=image),
              patch.object(record_store, "create_record_text", side_effect=fail_write),
              patch.object(change_page, "send_to_staff") as deliver,
              ThreadPoolExecutor(max_workers=1) as worker):
            key = self.start_page_capture()
            self.assertEqual(self.page_capture_preview(key).body, image)
            token = self.server.new_item_token
            fields = {**asdict(request), "token": token, "capture_key": key}
            send = worker.submit(self.new_item_http, json.dumps(fields).encode())
            try:
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                discarded = self.new_item_http(json.dumps({"token": token, "discard_capture": key}).encode())
                self.assertEqual(discarded.status, 200, discarded.body)
                self.assertEqual(self.page_capture_preview(key).body, image)
            finally:
                release.set()
            reply = send.result(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(reply.status, 503, reply.body)
            self.assertEqual(self.page_capture_preview(key).status, 404)
            self.assertEqual(cache.get().records, {})
            self.assertEqual(list(folder.glob("*.json")), [])
            deliver.assert_not_called()

    def test_closing_without_send_discards_the_page_capture(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        server = self.server
        release, captured = threading.Event(), threading.Event()
        complete = server.page_captures.complete

        def screenshot(_firefox: str, _url: str, _request: new_item_dialog.PageCaptureRequest) -> bytes:
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise OSError("Test did not release the screenshot.")
            return base64.b64decode(PNG)

        def capture_done(key: str, image: new_item_dialog.PageScreenshot | new_item_dialog.NoPageScreenshot) -> None:
            _ = complete(key, image)
            captured.set()

        request = case.submission_request(new_item_dialog.SubmissionKind.BUG, owner.id)
        with (patch.object(server, "review_cache", cache),
              patch.object(shutil, "which", side_effect=fake_firefox),
              patch.object(serve, "page_screenshot", side_effect=screenshot),
              patch.object(server.page_captures, "complete", side_effect=capture_done)):
            key = self.start_page_capture()
            try:
                reply = self.new_item_http(json.dumps({"token": server.new_item_token, "discard_capture": key}).encode())
                self.assertEqual(reply.status, 200, reply.body)
            finally:
                release.set()
            self.assertTrue(captured.wait(TEST_HANG_GUARD_SECS))
            self.assertEqual(cache.get().records, {})
            origin = new_item_dialog.ReviewSubmission(request.request_id, "", request.evidence, (), "")
            self.assertFalse(review_submissions.image_folder(origin).parent.parent.exists())
            self.assertEqual(self.page_capture_preview(key).status, 404)

    def test_new_item_offers_a_producer_with_a_directory_document(self) -> None:
        _ = self.review_fixture()
        directory = self.root.parent / 'brain'
        directory.mkdir()
        producer = fixture_member('producer', name='brain', doc=staff.LiveDoc(local_machine(), directory))
        response = self.new_item_http()
        self.assertEqual(response.status, 200)
        fields = cast(dict[str, object], json.loads(response.body))
        self.assertEqual(fields['recipients'], [{'id': producer.id, 'name': producer.name, 'proposal': False}])

    def test_new_item_offers_the_list_producer_and_sends_to_its_current_session(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        morty = fixture_member("producer", name="morty")
        morty = staff.update_current_session(morty.current_session, staff.native_id(str(uuid4())))
        self.assertNotEqual(morty.id, morty.current_session)
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={morty.id}\nowner.default={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        option: dict[str, object] = {"id": morty.id, "name": "morty", "proposal": False}
        for page in ("", *page_frame.OWNER_PAGES):
            with self.subTest(page=page):
                options = self.new_item_http(query=f"?page={page}" if page else "")
                self.assertEqual(options.status, 200)
                fields = cast(dict[str, object], json.loads(options.body))
                recipients = cast(list[dict[str, object]], fields["recipients"])
                self.assertEqual(recipients.count(option), 1)
                if page:
                    self.assertEqual(fields["page_owner"], morty.id if page == "list" else owner.id)
        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              capture_sender("send_message") as send):
            for page, kind in (("", new_item_dialog.SubmissionKind.QUESTION),
                               ("review", new_item_dialog.SubmissionKind.INVESTIGATION),
                               ("images", new_item_dialog.SubmissionKind.BUG)):
                with self.subTest(page=page, kind=kind):
                    send.reset_mock()
                    choice = (new_item_dialog.PageRecipient.LIST_OWNER if page
                              else new_item_dialog.PageRecipient.PAGE_OWNER)
                    request = replace(case.submission_request(kind, morty.id), page=page, page_recipient=choice,
                                      request_id="r2-" + uuid4().hex)
                    response = self.new_item_http(self.new_item_body(request), finished=True)
                    self.assertEqual(response.status, 200, response.body)
                    saved = cast(dict[str, str], json.loads(response.body))
                    item = cache.get().records[saved["key"]]
                    self.assertEqual(item.owner, morty.id)
                    details = review_items.submission_origin(item)
                    assert isinstance(details, new_item_dialog.ReviewSubmission)
                    self.assertEqual(details.page, page)
                    send.assert_called_once_with(f"session:{morty.current_session}", change_page.SENDER, ANY)

    def test_new_item_sends_each_type_to_each_name_in_the_to_row(self) -> None:
        case = self.review_fixture()
        showrunner = case.ready_submission_recipient()
        document = self.root.parent / "producer.md"
        _ = document.write_text(f"# Producer\n- **Proposals:** {self.root.parent / 'producer-proposals'}\n")
        producer = fixture_member("producer", name="producer", doc=staff.LiveDoc(local_machine(), document))
        morty = fixture_member("producer", name="morty")
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={morty.id}\nowner.default={showrunner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        # The dialog sends Feedback as an Investigation.
        kinds = [new_item_dialog.SubmissionKind(kind if kind != "feedback" else "investigation")
                 for kind, _, _ in new_item_dialog.KINDS]
        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              capture_sender("send_message")):
            for page in sorted(page_frame.OWNER_PAGES):
                options = self.new_item_http(query=f"?page={page}")
                self.assertEqual(options.status, 200, options.body)
                recipients = cast(list[dict[str, object]], cast(dict[str, object], json.loads(options.body))["recipients"])
                self.assertCountEqual([row["id"] for row in recipients], [showrunner.id, producer.id, morty.id])
                # A Send to a selected chip reads the page only to choose the List draft path or the standard path.
                if page not in ("list", "review"):
                    continue
                proposal_names = [row["name"] for row in recipients if row["proposal"]]
                self.assertCountEqual(proposal_names, [showrunner.name, producer.name])
                for kind in kinds:
                    for row in recipients:
                        if kind == new_item_dialog.SubmissionKind.PROPOSAL and row["name"] not in proposal_names:
                            continue
                        with self.subTest(page=page, kind=kind, name=row["name"]):
                            request = replace(case.submission_request(kind, staff.StaffId(str(row["id"]))), page=page,
                                              page_recipient=new_item_dialog.PageRecipient.SELECTED,
                                              request_id="r6-" + uuid4().hex)
                            response = self.new_item_http(self.new_item_body(request), finished=True)
                            self.assertEqual(response.status, 200, response.body)

    def test_standard_new_item_refuses_the_list_producer_with_an_unreadable_document(self) -> None:
        case = self.review_fixture()
        document = self.root.parent / "unreadable.md"
        _ = document.write_text("# Morty\n")
        producer = fixture_member("producer", name="morty", doc=staff.LiveDoc(local_machine(), document))
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={producer.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        read_text = Path.read_text

        def read(path: Path, encoding: str | None = None, errors: str | None = None,
                 newline: str | None = None) -> str:
            if path == document:
                raise PermissionError("The staff document is unreadable.")
            return read_text(path, encoding, errors, newline)

        with (patch.object(Path, "read_text", read),
              patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              patch.object(change_page, "send_message") as send):
            options = self.new_item_http()
            self.assertEqual(options.status, 200, options.body)
            fields = cast(dict[str, object], json.loads(options.body))
            recipients = cast(list[dict[str, object]], fields["recipients"])
            self.assertIn({"id": producer.id, "name": producer.name, "proposal": False}, recipients)

            question = case.submission_request(new_item_dialog.SubmissionKind.QUESTION, producer.id)
            for request in (question, replace(question, request_id="r2-" + uuid4().hex)):
                with self.subTest(request_id=request.request_id):
                    reply = self.new_item_http(self.new_item_body(request), finished=True)
                    self.assertEqual(reply.status, 400, reply.body)
                    self.assertEqual(json.loads(reply.body), {"error": f"The morty production document {document} cannot be read. You must fix the morty staff row."})
            self.assertEqual(update_registration.question_records_for_owner(producer.id), [])
            send.assert_not_called()

    def test_new_item_excludes_the_retired_list_producer(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        directory = self.root.parent / "former-producer"
        directory.mkdir()
        producer = fixture_member("producer", name="former-list-owner", doc=staff.LiveDoc(local_machine(), directory))
        _ = staff.set_producer_retirement(producer.id, staff.RetiredProducer("test-profile"))
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={producer.id}\nowner.default={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        for page in ("", "list", "review"):
            with self.subTest(page=page):
                reply = self.new_item_http(query=f"?page={page}" if page else "")
                self.assertEqual(reply.status, 200, reply.body)
                options = cast(dict[str, object], json.loads(reply.body))
                recipients = cast(list[dict[str, object]], options["recipients"])
                self.assertEqual([choice["id"] for choice in recipients], [owner.id])
                if page:
                    self.assertEqual(options["page_owner"], owner.id)

        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              patch.object(change_page, "send_message") as send):
            standard = case.submission_request(new_item_dialog.SubmissionKind.INVESTIGATION, producer.id)
            scoped = replace(standard, page="review", page_recipient=new_item_dialog.PageRecipient.LIST_OWNER,
                             request_id="r2-" + uuid4().hex)
            for request in (standard, scoped):
                with self.subTest(page=request.page):
                    reply = self.new_item_http(self.new_item_body(request), finished=True)
                    self.assertEqual(reply.status, 409, reply.body)
            self.assertEqual(investigation.records(producer.id), [])
            send.assert_not_called()

    def test_new_item_publishes_and_delivers_native_records_before_success(self) -> None:
        from scripts.review_links.test_review_submissions import EVIDENCE, PNG, identify_fixture_image

        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        _ = cache.get()
        address = self.enterContext(patch.object(serve, 'tailscale_ip', return_value='127.0.0.1'))
        token = cast(dict[str, object], json.loads(self.new_item_http().body))['token']
        self.assertEqual(self.new_item_http().status, 200)
        handler = cast(type[serve.ReviewLinksHandler], self.server.RequestHandlerClass)
        respond = handler.json_response
        delivered = threading.Event()

        def see_published(active: serve.ReviewLinksHandler, status: int, fields: Mapping[str, object]) -> None:
            if active.path == review_page.NEW_ITEM_PATH and status == 200:
                key = str(fields['key'])
                snapshot = cache.get()
                self.assertIn(key, snapshot.records)
                self.assertEqual(snapshot.version, fields['version'])
                self.assertEqual(snapshot.records[key].state, review_items.ReviewState.WAITING)
                self.assertTrue(delivered.is_set())
            respond(active, status, fields)

        def message_sent(_recipient: staff.StaffId, _sender: str, _message: str, **_options: str) -> None:
            delivered.set()

        with (patch.object(self.server, 'review_cache', cache),
              patch.object(self.server, 'delivery_status', case.status),
              patch.object(handler, 'json_response', see_published),
              patch.object(review_items, 'read', side_effect=AssertionError('Send scanned Review.')),
              patch.object(review_submissions, '_identify_image', autospec=True, side_effect=identify_fixture_image),
              patch.object(change_page, 'send_to_staff', side_effect=message_sent) as send):
            for kind in new_item_dialog.SubmissionKind:
                with self.subTest(kind=kind):
                    delivered.clear()
                    request = case.submission_request(kind, owner.id)
                    if kind == new_item_dialog.SubmissionKind.BUG:
                        request = replace(request, evidence=EVIDENCE,
                                          images=(new_item_dialog.PastedImage('image/png', PNG),))
                    body = json.dumps({**asdict(request), 'token': token}).encode()
                    if kind == new_item_dialog.SubmissionKind.BUG:
                        with ThreadPoolExecutor(max_workers=2) as workers:
                            first = workers.submit(self.new_item_http, body)
                            second = workers.submit(self.new_item_http, body)
                            replies = [first.result(TEST_HANG_GUARD_SECS), second.result(TEST_HANG_GUARD_SECS)]
                        for index, reply in enumerate(replies):
                            if reply.status == 503:
                                self.assertEqual(json.loads(reply.body), {"error": "This item is still sending. Try Send again."})
                                replies[index] = self.new_item_http(body)
                    else:
                        replies = [self.new_item_http(body), self.new_item_http(body)]
                    for reply in replies:
                        self.assertEqual(reply.status, 200, reply.body)
                    self.assertTrue(delivered.wait(TEST_HANG_GUARD_SECS))
                    saved = cast(dict[str, str], json.loads(replies[0].body))
                    self.assertEqual(saved, json.loads(replies[1].body))
                    self.assertEqual(set(saved), {'id', 'key', 'version'})
                    self.assertRegex(saved['id'], '^'+new_item_dialog.NATIVE_ID_PREFIX[kind]+r'[1-9][0-9]*$')
                    snapshot = cache.get()
                    origin = review_items.submission_origin(snapshot.records[saved['key']])
                    assert isinstance(origin, new_item_dialog.ReviewSubmission)
                    self.assertNotIn(origin.reply_token.encode(), replies[0].body + snapshot.index_response() + snapshot.page)
                    if kind == new_item_dialog.SubmissionKind.BUG:
                        self.assertNotIn(EVIDENCE.title.encode(), snapshot.index_response())
                        card = self.request(review_page.PAGE_PATH+'?'+urlencode({'body': saved['key']}))
                        self.assertIn(EVIDENCE.title.encode(), card.body)
                        image, = origin.images
                        attachment = self.request(review_page.ATTACHMENT_PATH+'?'+urlencode({
                            'key': saved['key'], 'id': image.id}))
                        self.assertEqual((attachment.status, attachment.content_type, attachment.body),
                                         (200, 'image/png', base64.b64decode(PNG)))
                        for fields in ({'key': 'missing', 'id': image.id},
                                       {'key': saved['key'], 'id': '../receipt.json'}):
                            self.assertEqual(self.request(review_page.ATTACHMENT_PATH+'?'+urlencode(fields)).status, 404)
            self.assertEqual(len(cache.get().records), 4)
            self.assertEqual(send.call_count, 4)
        address.assert_called_once_with()

    def test_restart_recovers_a_new_item_when_the_first_claim_write_fails(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        request = case.submission_request(new_item_dialog.SubmissionKind.BUG, owner.id)
        with (patch.object(self.server, "review_cache", cache),
              patch.object(self.server, "delivery_status", case.status),
              patch.object(change_page, "send_to_staff") as send):
            with patch.object(case.status, "write_slots", side_effect=OSError("The claim write failed.")):
                reply = self.new_item_http(self.new_item_body(request), finished=True)
            self.assertEqual(reply.status, 503, reply.body)
            self.assertEqual(len(cache.get().records), 1)
            send.assert_not_called()
            for _ in range(2):
                restarted = change_page.DeliveryStatus(case.root)
                change_page.recover_pending(restarted)
                review_actions.recover_submission_deliveries(restarted, review_page.PAGE_PATH)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0], owner.id)
            self.assertIn(request.words, cast(str, send.call_args.args[2]))
            self.assertEqual(len(investigation.records(owner.id, include_read=True)), 1)

    def test_sent_proposal_key_survives_a_rebuild_with_a_symlinked_folder(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        folder = case.fixture.production.parent
        native = folder / 'real-proposals'
        native.mkdir()
        (folder / 'native-proposals').symlink_to(native, target_is_directory=True)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        token = cast(dict[str, object], json.loads(self.new_item_http().body))['token']
        request = case.submission_request(new_item_dialog.SubmissionKind.PROPOSAL, owner.id)
        with (patch.object(self.server, 'review_cache', cache),
              patch.object(self.server, 'delivery_status', case.status),
              patch.object(change_page, 'send_to_staff')):
            response = self.new_item_http(json.dumps({**asdict(request), 'token': token}).encode(), finished=True)
            self.assertEqual(response.status, 200, response.body)
            key = cast(dict[str, str], json.loads(response.body))['key']
            self.assertIn(key, cache.get().records)
            rebuilt = review_page.build_snapshot(case.root)
            self.assertIn(key, rebuilt.records)
            self.assertEqual(review_items.source_path(rebuilt.records[key]).parent, native.resolve())
            with patch.object(cache, 'get', return_value=rebuilt):
                card = self.request(review_page.PAGE_PATH+'?'+urlencode({'body': key}))
            self.assertEqual(card.status, 200)

    def request(self, path: str, *, method: str = "GET") -> HttpReply:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            headers = {name.lower(): value for name, value in response.getheaders()}
            return HttpReply(response.status, headers.get("content-type", ""), response.read(), headers)
        finally:
            connection.close()

    def test_files_are_served_with_the_required_utf8_content_types(self) -> None:
        text = "review café — ready\n"
        feed = json.dumps({"reading": text}, ensure_ascii=False)
        for filename, content_type, payload in (
            ("notes.md", "text/plain; charset=utf-8", text),
            ("notes.txt", "text/plain; charset=utf-8", text),
            ("notes.html", "text/html; charset=utf-8", text),
            ("memory.json", "text/plain; charset=utf-8", feed),
        ):
            with self.subTest(filename=filename):
                _ = (self.root / filename).write_text(payload, encoding="utf-8")
                reply = self.request("/" + filename)
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.content_type, content_type)
                self.assertEqual(reply.body.decode("utf-8"), payload)

        path = "/memory.json"
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        self.addCleanup(connection.close)
        gzip_headers = {"Accept-Encoding": "gzip"}
        connection.request("GET", path, headers=gzip_headers)
        response = connection.getresponse()
        wire = response.read()
        etag = response.getheader("ETag", "")
        modified = response.getheader("Last-Modified", "")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Encoding"), "gzip")
        self.assertEqual(response.getheader("Content-Length"), str(len(wire)))
        self.assertEqual(response.getheader("Vary"), "Accept-Encoding")
        self.assertEqual(gzip.decompress(wire).decode("utf-8"), feed)

        for method, validators, status in (
            ("HEAD", {}, 200),
            ("GET", {"If-None-Match": etag}, 304),
            ("GET", {"If-Modified-Since": modified}, 200),
        ):
            with self.subTest(method=method, validators=validators):
                headers = {"Host": "127.0.0.1", "Connection": "close", **gzip_headers, **validators}
                fields = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
                with (socket.create_connection(("127.0.0.1", self.port), TEST_HANG_GUARD_SECS) as client,
                      client.makefile("rb") as reply):
                    client.sendall(f"{method} {path} HTTP/1.0\r\n{fields}\r\n".encode())
                    raw = reply.read()
                head, delimiter, body = raw.partition(b"\r\n\r\n")
                self.assertTrue(delimiter)
                status_line, _, raw_headers = head.partition(b"\r\n")
                self.assertEqual(status_line.split()[1:2], [str(status).encode()])
                received = parse_headers(BytesIO(raw_headers))
                self.assertEqual(received["ETag"], etag)
                self.assertEqual(received["Last-Modified"], modified)
                self.assertEqual(received["Vary"], "Accept-Encoding")
                if status == 200:
                    self.assertEqual(received["Content-Encoding"], "gzip")
                    self.assertEqual(received["Content-Length"], str(len(wire)))
                if method == "HEAD" or status == 304:
                    self.assertEqual(body, b"")
                else:
                    self.assertEqual(gzip.decompress(body).decode("utf-8"), feed)

        file = self.root / "memory.json"
        stamp = file.stat()
        updated = feed.replace("ready", "saved").encode()
        atomic_file.write_bytes_atomic(file, updated)
        os.utime(file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        connection.request("GET", path, headers={**gzip_headers, "If-None-Match": etag, "If-Modified-Since": modified})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertNotEqual(response.getheader("ETag"), etag)
        self.assertEqual(response.getheader("Last-Modified"), modified)
        self.assertEqual(gzip.decompress(response.read()), updated)

    def test_review_request_passes_the_selected_show_view(self) -> None:
        snapshot = review_page.build_snapshot(self.review_fixture().root)
        for query, show in (('', 'open'), ('?show=Done', 'Done'), ('?show=all', 'all'),
                            ('?show=For+you', 'For you'), ('?show=Waiting', 'Waiting')):
            with self.subTest(show=show), patch.object(review_page.ReviewSnapshotCache,'get',return_value=snapshot), patch.object(
                    review_page.ReviewSnapshot,'render',return_value=b'Review') as render:
                reply = self.request('/console/review.html'+query)
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.body, b'Review')
                render.assert_called_once_with(show=show)

    def test_review_snapshot_endpoints_do_not_read_stores_and_use_gzip(self) -> None:
        case = self.review_fixture()
        key=case.question()
        action = ActionStore().add(title='Restore GitHub access', command='github-warmup', machine='natedev',
                                   cwd='/fixture', requested_by=case.fixture.unit.id, reason='The login needs your browser.',
                                   waits='The release check', concerns='admin-console', dedupe_key='github-access')
        snapshot=review_page.build_snapshot(case.root)
        with patch.object(review_page.ReviewSnapshotCache,'get',return_value=snapshot), patch.object(
                review_items,'read',side_effect=AssertionError('A GET read a store.')), patch.object(
                staff,'snapshot',side_effect=AssertionError('A GET read staff.')):
            version=self.request(review_page.PAGE_PATH+'?version=1')
            self.assertEqual(version.status,200)
            self.assertEqual(json.loads(version.body)['version'],snapshot.version)
            index=self.request(review_page.PAGE_PATH+'?index=1')
            self.assertEqual(index.status,200)
            self.assertEqual(json.loads(index.body)['version'],snapshot.version)
            legacy_rows = test_review_page.unpack_index(json.dumps(json.loads(index.body)['index']))
            self.assertIn(key, {entry['key'] for entry in legacy_rows})
            self.assertTrue(all(entry['type'] != review_items.ReviewType.FOR_YOU for entry in legacy_rows))
            current = self.request(review_page.PAGE_PATH+'?index=1&for-you=1')
            self.assertEqual(current.status, 200)
            current_rows = test_review_page.unpack_index(json.dumps(json.loads(current.body)['index']))
            self.assertIn(action.key, {entry['key'] for entry in current_rows if entry['type'] == review_items.ReviewType.FOR_YOU})
            self.assertEqual(legacy_rows, [entry for entry in current_rows if entry['type'] != review_items.ReviewType.FOR_YOU])
            card=self.request(review_page.PAGE_PATH+'?'+urlencode({'body':key}))
            self.assertEqual(card.status,200)
            self.assertIn(b'data-review-form',card.body)
            self.assertEqual(self.request(review_page.PAGE_PATH+'?body=missing').status,404)
            connection=HTTPConnection('127.0.0.1',self.port,timeout=TEST_HANG_GUARD_SECS)
            try:
                connection.request('GET',review_page.PAGE_PATH+'?index=1',headers={'Accept-Encoding':'gzip'})
                response=connection.getresponse()
                wire=response.read()
                self.assertEqual(response.status,200)
                self.assertEqual(response.getheader('Content-Encoding'),'gzip')
                self.assertEqual(response.getheader('Vary'),'Accept-Encoding')
                self.assertEqual(gzip.decompress(wire), index.body)
                self.assertLess(len(wire), len(index.body))
            finally:
                connection.close()

    def test_server_initializes_review_on_a_fresh_root(self) -> None:
        with (patch.object(serve, "loaded_module_stamps", return_value={}),
              patch.object(change_page, "recover_pending"), patch.object(list_page, "recover_pending")):
            server = self.enterContext(started_review_server(("127.0.0.1", 0), self.root, initialize_review=True))
        server.review_cache.synchronize()
        self.assertTrue((self.root / change_page.CONSOLE_FOLDER / change_page.BROWSER_MANIFEST).exists())
        self.port = server.server_port
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, TEST_HANG_GUARD_SECS)
        self.addCleanup(server.shutdown)
        reply = self.request(review_page.PAGE_PATH)
        self.assertEqual(reply.status, 200)
        self.assertIn(b'data-review-index', reply.body)

    def test_review_index_omits_an_unknown_handoff_time(self) -> None:
        case = self.review_fixture(changes=2)
        saved = '2026-11-16T12:49:08-05:00'
        validator = staff.snapshot().by_doc(local_machine(), case.fixture.production)
        assert validator is not None
        for row, time_fields in (('C1', {}), ('C2', {'handed_at': saved})):
            path = change_routing.delivery_path(case.root, case.fixture.review_list, row)
            validation = {'attempt': row, 'state': 'sent', 'recipients': [validator.id]}
            _ = path.write_text(json.dumps({'validation': validation, **time_fields}))
            os.utime(path, (1795123200, 1795123200))
        snapshot = review_page.build_snapshot(case.root)
        with patch.object(review_page.ReviewSnapshotCache, 'get', return_value=snapshot):
            reply = self.request(review_page.PAGE_PATH + '?index=1')
        self.assertEqual(reply.status, 200)
        entries = {entry['key']: entry for entry in
                   test_review_page.unpack_index(json.dumps(json.loads(reply.body)['index']))}
        unknown = entries['page-owner/C1']
        self.assertEqual(unknown['state'], review_items.ReviewState.VALIDATING)
        self.assertNotIn('handoff', unknown)
        self.assertEqual(entries['page-owner/C2'].get('handoff'), (saved, 'Nov 16 12:49 EST'))

    def test_review_get_initializes_a_fresh_root(self) -> None:
        _ = self.review_fixture()
        self.assertFalse((self.root / change_page.CONSOLE_FOLDER / change_page.BROWSER_MANIFEST).exists())
        reply = self.request(review_page.PAGE_PATH)
        self.assertEqual(reply.status, 200)
        self.assertIn(b'data-review-index', reply.body)

    def test_review_body_reports_its_version_and_gzip_uses_quality(self) -> None:
        case = self.review_fixture()
        key = case.question()
        snapshot = review_page.build_snapshot(case.root)
        with patch.object(review_page.ReviewSnapshotCache, 'get', return_value=snapshot):
            path = review_page.PAGE_PATH+'?'+urlencode({'body':key, 'version':'earlier'})
            status, headers, body = self.head(path)
            self.assertEqual(status, 200)
            with self.subTest('body version'):
                self.assertEqual(headers.get('x-review-version'), snapshot.version)
            self.assertEqual(body, snapshot.body(key))
            for value, compressed in (('gzip;q=0.5', True), ('GZip', True), ('gzip;q=0', False), ('notgzip', False)):
                with self.subTest(value=value):
                    connection = HTTPConnection('127.0.0.1', self.port, timeout=TEST_HANG_GUARD_SECS)
                    try:
                        connection.request('GET', review_page.PAGE_PATH+'?index=1', headers={'Accept-Encoding':value})
                        response = connection.getresponse()
                        wire = response.read()
                        self.assertEqual(response.getheader('Content-Encoding') == 'gzip', compressed)
                        self.assertEqual(gzip.decompress(wire) if compressed else wire, snapshot.legacy_index.body)
                    finally:
                        connection.close()

    def test_review_later_decision_and_news_use_retained_records(self) -> None:
        self.server.review_cache.close()
        case = self.review_fixture()
        proposal, _ = case.decisions()
        key = case.question()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        snapshot = cache.get()
        page = console.ConsolePage('review', 'Review', 'inbox', 'Review',
                                  console.AvailablePage(review_page.PAGE_PATH), review_page.waiting_items)
        with patch.object(serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(
                self.server, 'review_cache', cache), patch.object(
                change_browser, 'browser_manifest', return_value=(snapshot.browser.token, snapshot.browser.initial)), patch.object(
                console, 'read_pages', return_value=(page,)), patch.object(
                review_items, 'read', return_value=[]) as read, patch.object(
                review_items, 'read_open', return_value=[]) as read_open, patch.object(
                review_actions, 'later', return_value='later'), patch.object(
                review_actions, 'decision_action', return_value=(change_page.NoMessageDelivery(), 'approved')):
            for row, outcome, extra in ((key, 'later', '&later=2030-12-02T18%3A30'), (proposal, 'approve', '')):
                with self.subTest(outcome=outcome):
                    self.assertEqual(self.post(review_page.PAGE_PATH, case.card_body(cache.get(), row, outcome)+extra)[0], 200)
            self.assertEqual(self.post('/console/news', 'open=review')[0], 200)
            seen = (self.root / change_page.CONSOLE_FOLDER / console.SEEN_NAME).read_text()
            with self.subTest('Review news identifies the retained question'):
                self.assertIn(key, seen)
            with self.subTest('Later and decisions do not scan the stores'):
                read.assert_not_called()
            with self.subTest('Review news does not scan the stores'):
                read_open.assert_not_called()

    def test_review_save_invalidates_before_reply_even_when_delivery_fails(self) -> None:
        case = self.review_fixture()
        key = case.question()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        events: list[str] = []
        complete = threading.Event()
        respond = serve.ReviewLinksHandler.json_response

        def response(handler: serve.ReviewLinksHandler, status: int, fields: dict[str, object]) -> None:
            events.append('respond')
            respond(handler, status, fields)

        def deliver(_delivery: change_page.NoMessageDelivery, _status: change_page.DeliveryStatus) -> None:
            events.append('deliver')
            complete.set()
            raise OSError('The fixture delivery failed.')

        with patch.object(serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(
                self.server, 'review_cache', cache), patch.object(
                cache, 'invalidate', side_effect=lambda: events.append('invalidate')), patch.object(
                serve.ReviewLinksHandler, 'json_response', response), patch.object(
                change_page, 'deliver', side_effect=deliver):
            self.assertEqual(self.post(review_page.PAGE_PATH, case.card_body(cache.get(), key, 'later')+'&later=2030-12-02T18%3A30')[0], 200)
            self.assertTrue(complete.wait(TEST_HANG_GUARD_SECS))
            self.assertEqual(events, ['invalidate', 'respond', 'deliver'])

    def test_review_save_publishes_the_current_record_before_reply(self) -> None:
        case = self.review_fixture()
        proposal, _ = case.decisions()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        before = cache.get()
        with patch.object(self.server, 'review_cache', cache), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(change_page, 'deliver'):
            status, reply = self.post(review_page.PAGE_PATH, case.card_body(before, proposal, 'approve'))
            self.assertEqual(status, 200)
            payload = cast(dict[str, str], json.loads(reply))
            version = cast(dict[str, str], json.loads(self.request(review_page.PAGE_PATH+'?version=1').body))
            shell = self.request(review_page.PAGE_PATH+'?show=all').body.decode()
            entries = test_review_page.unpack_index(shell.split('data-review-index>',1)[1].split('</script>',1)[0])
            with self.subTest(finding='F022'):
                self.assertNotEqual(version['version'], before.version)
                self.assertEqual(payload['version'], version['version'])
                self.assertEqual(payload['state'], 'Done')
                self.assertNotIn(proposal, {entry['key'] for entry in entries})
                self.assertEqual(self.request(review_page.PAGE_PATH+'?'+urlencode({'body': proposal})).status, 404)
            with self.subTest(finding='F015'):
                before_decline = next(item for item in review_items.read(case.root) if item.key == proposal)
                with patch.object(cache, 'get', return_value=before):
                    status, reply = self.post(review_page.PAGE_PATH, case.card_body(before, proposal, 'decline'))
                self.assertEqual(status, 409, reply)
                self.assertEqual(next(item for item in review_items.read(case.root) if item.key == proposal), before_decline)

    def test_review_saves_unchanged_questions_with_a_prerequisite(self) -> None:
        case = self.review_fixture()
        _ = case.question('Which layout comes first?')
        answer = case.question('Which layout comes second?')
        later = case.question('Which layout comes third?')
        for key in (answer, later):
            path = case.question_path(key)
            _ = path.write_text(path.read_text().replace('registration: {',
                'registration: {"follows": "Which layout comes first?", '))
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        with patch.object(self.server, 'review_cache', cache), patch.object(self.server, 'delivery_status', case.status), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(change_page, 'deliver'):
            for key, outcome, words, extra in (
                    (answer, 'answer', 'Use the compact layout.', ''),
                    (later, 'later', '', '&later=2030-12-02T18%3A30')):
                with self.subTest(outcome=outcome):
                    status, body = self.post(review_page.PAGE_PATH, case.card_body(cache.get(), key, outcome, words)+extra)
                    self.assertEqual(status, 200, body)
        self.assertNotIn(answer, cache.get().records)
        self.assertIsInstance(cache.get().records[later].times.later, datetime)

    def test_review_verdict_uses_the_current_production_after_a_transfer(self) -> None:
        case = self.review_fixture(changes=1)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        key = 'page-owner/C1'
        form = case.card_body(cache.get(), key, 'works', 'The fixture passes.')
        doc = case.fixture.temporary / 'destination.md'
        _ = doc.write_text(case.fixture.production.read_text())
        new_owner = fixture_member('showrunner', name='destination', doc=staff.LiveDoc(local_machine(), doc))
        _ = staff.move(case.fixture.unit.id, staff.ReportsTo(new_owner.id))
        complete = threading.Event()
        deliveries: list[change_page.MessageDelivery] = []

        def deliver(delivery: change_page.MessageDelivery, _status: change_page.DeliveryStatus) -> None:
            deliveries.append(delivery)
            complete.set()

        with patch.object(self.server, 'review_cache', cache), patch.object(self.server, 'delivery_status', case.status), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(change_page, 'deliver', side_effect=deliver):
            status, body = self.post(review_page.PAGE_PATH, form)
            self.assertEqual(status, 200, body)
            self.assertTrue(complete.wait(TEST_HANG_GUARD_SECS))
        delivery, = deliveries
        self.assertEqual(delivery.recipients, (case.fixture.unit.id, new_owner.id))
        self.assertEqual(delivery.page.production, doc)
        self.assertIn('Production: destination', delivery.message)

    def test_review_proposal_conflicts_after_transfer_and_uses_the_current_recipient(self) -> None:
        case = self.review_fixture()
        proposal, _ = case.decisions()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        form = case.card_body(cache.get(), proposal, 'approve')
        doc = case.fixture.temporary/'destination.md'
        _ = doc.write_text(case.fixture.production.read_text())
        runner = fixture_member('showrunner', name='destination', doc=staff.LiveDoc(local_machine(), doc))
        _ = staff.move(case.fixture.unit.id, staff.ReportsTo(runner.id))
        deliveries: list[change_page.MessageDelivery] = []
        complete = threading.Event()

        def deliver(message: change_page.MessageDelivery, _status: change_page.DeliveryStatus) -> None:
            deliveries.append(message)
            complete.set()

        with patch.object(self.server, 'review_cache', cache), patch.object(self.server, 'delivery_status', case.status), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(change_page, 'deliver', side_effect=deliver):
            status, body = self.post(review_page.PAGE_PATH, form)
            self.assertEqual(status, 409, body)
            self.assertEqual(deliveries, [])
            fresh = cache.get()
            self.assertEqual(fresh.records[proposal].owner, runner.id)
            status, body = self.post(review_page.PAGE_PATH, case.card_body(fresh, proposal, 'approve'))
            self.assertEqual(status, 200, body)
            self.assertTrue(complete.wait(TEST_HANG_GUARD_SECS))
        delivery, = deliveries
        self.assertEqual(delivery.recipients, (runner.id,))

    def test_review_approves_a_proposal_when_another_document_is_unavailable(self) -> None:
        for condition in ('unreadable', 'missing', 'prerequisite'):
            with self.subTest(source=condition):
                case = self.review_fixture()
                try:
                    self.approve_proposal_with_source_failure(case, condition)
                finally:
                    case.doCleanups()

    def approve_proposal_with_source_failure(self, case: test_review_page.ReviewPageTests, condition: str) -> None:
        proposal, _ = case.decisions()
        path = case.fixture.temporary/'unrelated.md'
        if condition == 'prerequisite':
            selected = Path(proposal.removeprefix('proposal/'))
            path = selected.with_name('P2.md')
            original = selected.read_text()
            _ = path.write_text(original.replace('proposal_id: P1', 'proposal_id: P2'))
            _ = selected.write_text(original.replace('proposal_id: P1', 'proposal_id: P1\ndepends_on: [P2]'))
        else:
            _ = fixture_member('showrunner', name='unrelated', doc=staff.LiveDoc(local_machine(), path))
            if condition == 'unreadable':
                _ = path.write_text('# Unrelated production')
        open_text = cast(Callable[..., TextIO], Path.open)

        def read(source: Path, *args: str, **kwargs: str) -> TextIO:
            if source == path and condition != 'missing':
                raise PermissionError('The fixture source is unreadable.')
            return open_text(source, *args, **kwargs)

        with patch.object(Path, 'open', read):
            cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
            with patch.object(self.server, 'review_cache', cache), patch.object(
                    self.server, 'delivery_status', case.status), patch.object(change_page, 'deliver'):
                status, body = self.post(review_page.PAGE_PATH, case.card_body(cache.get(), proposal, 'approve'))
                self.assertEqual(status, 200, body)
                self.assertNotIn(proposal, cache.get().records)

    def test_review_finishes_verdict_scratch_cleanup_after_reply_and_before_delivery(self) -> None:
        case = self.review_fixture(changes=1)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        key = 'page-owner/C1'
        events: list[str] = []
        complete = threading.Event()
        deliveries: list[change_page.MessageDelivery] = []
        respond = serve.ReviewLinksHandler.json_response

        def response(handler: serve.ReviewLinksHandler, status: int, fields: dict[str, object]) -> None:
            self.assertNotIn(key, cache.get().records)
            events.append('respond')
            respond(handler, status, fields)

        def cleanup(_path: Path, _row: str) -> list[str]:
            events.append('cleanup')
            raise OSError('The fixture cleanup failed.')

        def deliver(delivery: change_page.MessageDelivery, _status: change_page.DeliveryStatus) -> None:
            events.append('deliver')
            deliveries.append(delivery)
            complete.set()

        with patch.object(self.server, 'review_cache', cache), patch.object(self.server, 'delivery_status', case.status), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(
                serve.ReviewLinksHandler, 'json_response', response), patch.object(
                change_review, 'end_row_scratch', side_effect=cleanup), patch.object(change_page, 'deliver', side_effect=deliver):
            status, body = self.post(review_page.PAGE_PATH, case.card_body(cache.get(), key, 'works'))
            self.assertEqual(status, 200, body)
            self.assertTrue(complete.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(events, ['respond', 'cleanup', 'deliver'])
        self.assertIn('The fixture cleanup failed.', deliveries[0].message)

    def test_review_does_not_close_the_feature_log_when_verdict_preparation_fails(self) -> None:
        case = self.review_fixture(changes=1, feature_project=True)
        key = 'page-owner/C1'
        self.propose_feature(key)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        before = case.fixture.review_list.read_bytes()
        proposal = case.fixture.proposal(key)
        claim = change_routing.delivery_path(case.root, case.fixture.review_list, 'C1')
        with patch.object(self.server, 'review_cache', cache), patch.object(
                self.server, 'delivery_status', case.status):
            for operation in ('intent', 'claim', 'list'):
                with self.subTest(operation=operation):
                    targets = {'intent': (change_review.PendingVerdictCleanup, 'save'),
                               'claim': (case.status, 'begin'),
                               'list': (change_review, 'prepared_replacement')}
                    target, method = targets[operation]
                    with patch.object(target, method, side_effect=PermissionError('The fixture write failed.')):
                        status, body = self.post(review_page.PAGE_PATH, case.card_body(cache.get(), key, 'works'))
                    self.assertEqual(case.fixture.proposal(key), proposal)
                    self.assertFalse((feature_log.vault_dir() / 'feature-log/claude/numbered moves.md').exists())
                    self.assertFalse(claim.exists())
                    self.assertEqual(case.status.request_attempts, {})
                    self.assertEqual(list(case.root.glob('.review-link-scratch-*')), [])
                    self.assertEqual(case.fixture.review_list.read_bytes(), before)
                    self.assertEqual(cache.get().records[key].state, review_items.ReviewState.OPEN)
                    self.assertIn(status, (400, 503), body)
                    self.assertIn(b'could not be saved', body)

    def test_review_can_return_a_change_after_its_verdict_replace_fails(self) -> None:
        case = self.review_fixture(changes=1, feature_project=True)
        key = 'page-owner/C1'
        self.propose_feature(key)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        listing = case.fixture.review_list
        original = case.fixture.proposal(key)
        replace_file = os.replace

        def refuse(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            if target == listing:
                raise PermissionError('The fixture verdict replace failed.')
            replace_file(source, target)

        with patch.object(self.server, 'review_cache', cache), patch.object(self.server, 'delivery_status', case.status):
            with patch.object(os, 'replace', refuse):
                status, body = self.post(review_page.PAGE_PATH, case.card_body(cache.get(), key, 'works'))
            self.assertIn(status, (400, 503), body)
            self.assertEqual(cache.get().records[key].state, review_items.ReviewState.OPEN)
            self.assertEqual(case.fixture.proposal(key), original)
            status, body = self.post(review_page.PAGE_PATH, case.card_body(cache.get(), key, 'not works'))
            self.assertEqual(status, 200, body)
            self.assertTrue(case.fixture.delivery_done.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(case.fixture.proposal(key)['status'], 'dropped')

    def test_review_recovery_retries_feature_closeout_after_the_verdict(self) -> None:
        case = self.review_fixture(changes=1, feature_project=True)
        key = 'page-owner/C1'
        self.propose_feature(key)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)

        land = feature_log.land_review

        def fail_closeout(review: str, if_proposed: bool = False, *, check_only: bool = False) -> list[str]:
            if check_only:
                return land(review, if_proposed, check_only=True)
            self.assertNotIn(key, cache.get().records)
            raise PermissionError('The fixture closeout failed.')

        with patch.object(feature_log, 'land_review', side_effect=fail_closeout) as closeout:
            saved = cache.save(case.card_body(cache.get(), key, 'works'), case.status)
            _ = change_page.finish_verdict_cleanup(saved.delivery, case.status)
            self.assertEqual(closeout.call_count, 2)
        self.assertNotIn(key, cache.get().records)
        self.assertEqual(case.fixture.proposal(key)['status'], 'open')
        with patch.object(change_page, 'send_to_staff'):
            change_page.recover_pending(change_page.DeliveryStatus(case.root))
        self.assertEqual(case.fixture.proposal(key)['status'], 'landed')

    def test_review_recovery_preserves_the_original_reply_id_after_interruption(self) -> None:
        case = self.review_fixture(changes=1)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        key = 'page-owner/C1'
        with patch.object(change_review, 'prepared_replacement', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = cache.save(case.card_body(cache.get(), key, 'works'), case.status)
        page = change_page.ReviewPage(case.fixture.review_list, case.fixture.production, ('C1',), '')
        intent = change_review.PendingVerdictCleanup.for_row(case.root, page.review_list, 'C1')
        self.assertTrue(intent.intent.exists())
        # Recreate a legacy record with an interrupted verdict and an open question.
        asked = case.status.ask(page, 'C1', 'What changed?')
        assert isinstance(asked, change_page.QuestionDelivery)
        self.assertIsInstance(case.status.slots(page, 'C1').verdict, change_page.DeliveryIntent)
        recovered = change_page.DeliveryStatus(case.root)
        with patch.object(change_page, 'send_to_staff'):
            change_page.recover_pending(recovered)
        self.assertFalse(intent.intent.exists())
        slots = recovered.slots(page, 'C1')
        self.assertIsInstance(slots.verdict, change_page.NoDelivery)
        question = slots.question
        self.assertIsInstance(question, change_page.OpenQuestion)
        assert isinstance(question, change_page.OpenQuestion)
        self.assertEqual(question.question_id, asked.question_id)
        self.assertTrue(change_page.answer_item(key, 'The layout changed.', asked.question_id, recovered))

    def test_response_followup_uses_the_same_owner_and_got_it_closes_the_question(self) -> None:
        case = self.review_fixture()
        owner = case.ready_submission_recipient()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = case.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner.id)
        with (patch.object(self.server, 'review_cache', cache),
              patch.object(self.server, 'delivery_status', case.status),
              capture_sender('send_to_staff') as send):
            created = self.new_item_http(self.new_item_body(request), finished=True)
            self.assertEqual(created.status, 200, created.body)
            sent = cast(dict[str, str], json.loads(created.body))
            key = sent['key']
            waiting = cache.get().records[key]
            self.assertEqual((waiting.type, waiting.state), (review_items.ReviewType.QUESTION,
                                                            review_items.ReviewState.WAITING))
            path = review_items.source_path(waiting)
            initial = review_items.submission_origin(waiting)
            assert isinstance(initial, new_item_dialog.ReviewSubmission)
            reply_key = new_item_dialog.submission_key(request.kind, owner.id, sent['id'], request.request_id)
            self.assertTrue(change_page.answer_item(reply_key, 'Use the wide layout.', initial.reply_token, case.status))
            response, _ = cache.publish_record(cache.get(), key)
            self.assertEqual(response.records[key].type, review_items.ReviewType.RESPONSE)
            card_url = review_page.PAGE_PATH + '?' + urlencode({'body': key})
            card = self.request(card_url)
            self.assertEqual(card.status, 200, card.body)
            self.assertIn(b'Use the wide layout.', card.body)
            self.assertIn(b'Got it', card.body)

            send.reset_mock()
            followup = 'Can I use it on a phone?'
            post = case.card_body(response, key, 'question', followup)
            status, body = self.post(review_page.PAGE_PATH, post)
            self.assertEqual(status, 200, body)
            result = cast(dict[str, str], json.loads(body))
            self.assertEqual((result['type'], result['state']), ('question', 'Waiting'))
            send.assert_called_once_with(owner.id, change_page.SENDER, ANY)
            message = cast(str, send.call_args.args[2])
            self.assertIn(followup, message)
            command = shlex.split(next(line for line in message.splitlines() if ' answer-question ' in line))
            self.assertEqual(command[command.index('--item')+1], reply_key)
            token = command[command.index('--question')+1]
            self.assertNotEqual(token, initial.reply_token)
            self.assertEqual(self.post(review_page.PAGE_PATH, post)[0], 409)
            send.assert_called_once()
            with self.assertRaises(question_record.AnswerRefused):
                _ = change_page.answer_item(reply_key, 'Old reply.', initial.reply_token, case.status)

            self.assertTrue(change_page.answer_item(reply_key, 'Yes, use the narrow layout.', token, case.status))
            self.assertFalse(change_page.answer_item(reply_key, 'Yes, use the narrow layout.', token, case.status))
            returned, _ = cache.publish_record(cache.get(), key)
            self.assertEqual(self.post(review_page.PAGE_PATH, post)[0], 409)
            self.assertEqual(returned.records[key].type, review_items.ReviewType.RESPONSE)
            self.assertEqual(review_items.source_path(returned.records[key]), path)
            self.assertIn(b'Yes, use the narrow layout.', self.request(card_url).body)
            self.assertEqual([record.path for record in update_registration.question_records_for_owner(owner.id)], [path])

            status, body = self.post(review_page.PAGE_PATH, case.card_body(returned, key, 'read'))
            self.assertEqual(status, 200, body)
            self.assertEqual(cast(dict[str, str], json.loads(body))['state'], 'Done')
            self.assertNotIn(key, cache.get().records)
            self.assertEqual(update_registration.read_review_question(path, owner.id).status, 'closed')
            self.assertEqual(self.request(card_url).status, 404)
            send.assert_called_once()

    def test_review_waits_for_directory_sync_and_list_commit_before_feature_closeout(self) -> None:
        case = self.review_fixture(changes=1, feature_project=True)
        key = 'page-owner/C1'
        self.propose_feature(key)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        listing = case.fixture.review_list
        intent = change_review.PendingVerdictCleanup.for_row(case.root, listing, 'C1')
        events: list[str] = []

        def commit(*_args: object, report_warning: Callable[[str], None]) -> None:
            events.append('commit')
            if events.count('commit') == 1:
                report_warning('The fixture commit failed.')

        def sync_directory(_folder: Path) -> None:
            events.append('sync')

        with (patch.object(change_review, 'sync_folder', side_effect=OSError('The fixture directory sync failed.')) as sync,
              patch.object(self.server, 'review_cache', cache),
              patch.object(self.server, 'delivery_status', case.status),
              patch.object(change_page, 'recover_pending', wraps=change_page.recover_pending) as recover,
              patch.object(change_page, 'send_to_staff') as send):
            status, body = self.post(review_page.PAGE_PATH, case.card_body(cache.get(), key, 'works'))
            self.assertEqual(status, 503, body)
            self.assertEqual(case.fixture.proposal(key)['status'], 'open')
            self.assertTrue(intent.intent.exists())
            send.assert_not_called()
            recover.assert_called_once_with(case.status, only=replace(intent, verdict='works', feature_review=key))
            self.assertEqual(sync.call_args_list, [call(listing.parent)] * 3)
        with (patch.object(change_page, 'send_to_staff') as send,
              patch.object(change_review, 'sync_folder', side_effect=sync_directory),
              patch.object(change_review, '_commit', side_effect=commit)):
            change_page.recover_pending(change_page.DeliveryStatus(case.root))
            self.assertEqual(case.fixture.proposal(key)['status'], 'open')
            self.assertTrue(intent.intent.exists())
            send.assert_not_called()
            change_page.recover_pending(change_page.DeliveryStatus(case.root))
        self.assertEqual(events, ['sync', 'commit', 'sync', 'commit'])
        self.assertEqual(case.fixture.proposal(key)['status'], 'landed')
        self.assertFalse(intent.intent.exists())

    def test_review_alerts_once_when_a_durable_feature_closeout_is_permanently_blocked(self) -> None:
        case = self.review_fixture(changes=3, feature_project=True)
        def feature(command: str, title: str, *arguments: str) -> None:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                result = feature_log.main([command, '--project', 'claude', '--kind', 'feature',
                    '--title', title, '--summary', title, '--behavior', 'Each group starts at 1',
                    '--see', 'Numbers start at 1', '--link', ({'Old moves': '2', 'First revision': '3', 'Second revision': '4'}[title])*40,
                    '--evidence', 'The page test passes', '--completed', '2026-11-01', *arguments])
            self.assertEqual((result, errors.getvalue()), (0, ''))
        feature('add', 'Old moves')
        feature('propose', 'First revision', '--review', 'page-owner/C1', '--old', 'Old moves')
        feature('propose', 'Second revision', '--review', 'page-owner/C2', '--old', 'Old moves')
        self.propose_feature('page-owner/C3')
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        saved = [cache.save(case.card_body(cache.get(), key, 'works'), case.status)
                 for key in ('page-owner/C1', 'page-owner/C2', 'page-owner/C3')]
        _ = change_page.finish_verdict_cleanup(saved[0].delivery, case.status)
        _ = feature_log.drop_review('page-owner/C3', 'The proposal was dropped.')
        alerts: list[tuple[staff.StaffId, str]] = []
        refused_states: list[str] = []

        def send(owner: staff.StaffId, _sender: str, message: str, **_options: str) -> None:
            if 'feature log failed' in message:
                alerts.append((owner, message))
                if 'already dropped' in message:
                    intent = change_review.PendingVerdictCleanup.for_row(case.root, case.fixture.review_list, 'C3')
                    refused_states.append(intent.load(intent.intent, intent.path.parent).feature_review)
                    raise RuntimeError('The fixture refused the closeout alert.')

        with patch.object(change_page, 'send_to_staff', side_effect=send), redirect_stderr(StringIO()) as errors:
            for _ in range(2):
                change_page.recover_pending(change_page.DeliveryStatus(case.root))
        self.assertEqual(case.fixture.proposal('page-owner/C1')['status'], 'landed')
        self.assertEqual(case.fixture.proposal('page-owner/C2')['status'], 'open')
        self.assertEqual(case.fixture.proposal('page-owner/C3')['status'], 'dropped')
        self.assertEqual(refused_states, [''])
        self.assertEqual(errors.getvalue().count('The fixture refused the closeout alert.'), 1)
        self.assertEqual(list(case.root.glob('.review-link-scratch-*')), [])
        self.assertEqual(len(alerts), 2)
        assert isinstance(case.fixture.unit.parent, staff.ReportsTo)
        for row, reason in (('C2', 'old note old moves is not live'), ('C3', 'already dropped')):
            matching = [(owner, message) for owner, message in alerts if row in message]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0][0], case.fixture.unit.parent.id)
            self.assertIn(reason, matching[0][1])

    def test_review_recovery_logs_the_saved_wait_supersession(self) -> None:
        case = self.review_fixture(changes=1)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        key = 'page-owner/C1'
        asked = cache.save(case.card_body(cache.get(), key, 'question', 'What changed?'), case.status).delivery
        assert isinstance(asked, change_page.MessageDelivery)
        question = case.status.slots(asked.page, 'C1').question
        assert isinstance(question, change_page.OpenQuestion)
        original = case.fixture.production
        event = f'Review {key}: works superseded the question {question.question_id} before the answer arrived.'
        parent = case.fixture.unit.parent
        assert isinstance(parent, staff.ReportsTo)
        owner = staff.snapshot().by_id(parent.id)
        assert owner is not None
        listing = case.fixture.review_list
        # Recreate a saved verdict and its legacy cleanup record.
        cleanup = replace(change_review.PendingVerdictCleanup.for_row(case.root, listing, 'C1'),
                          verdict='works', superseded_wait=event,
                          wait_log=change_review.WaitSupersessionDestination(
                              owner.id, read_new_production(original, owner.name)))
        cleanup.save()
        saved = change_review.save_verdict(listing, 'C1', 'works', original, cleanup=cleanup, superseded=event)
        self.assertIsInstance(saved, change_review.VerdictSaved)
        moved_doc = case.fixture.temporary / 'destination.md'
        _ = moved_doc.write_text(original.read_text().replace('progress.log', 'destination.log'))
        destination = fixture_member('showrunner', name='destination', doc=staff.LiveDoc(local_machine(), moved_doc))
        with patch.object(change_page, 'log_wait_supersession', side_effect=OSError('The log is unavailable.')):
            _ = change_page.finish_saved_verdict(cleanup, case.status)
        self.assertTrue(cleanup.intent.exists())
        _ = staff.move(case.fixture.unit.id, staff.ReportsTo(destination.id))
        _ = original.write_text(original.read_text().replace('progress.log', 'renamed.log'))
        with patch.object(change_page, 'send_to_staff'):
            for _ in range(2):
                change_page.recover_pending(change_page.DeliveryStatus(case.root))
        log_path = case.fixture.repo / 'progress.log'
        log = log_path.read_text() if log_path.exists() else ''
        self.assertEqual(log.count(event), 1)
        self.assertFalse((case.fixture.repo / 'destination.log').exists())
        self.assertFalse((case.fixture.repo / 'renamed.log').exists())

    def test_selected_change_returns_the_shared_handoff_refusal(self) -> None:
        case = self.review_fixture(changes=2)
        listing = case.fixture.review_list
        production = case.fixture.production
        token = 'stale-selection'
        folder = self.root / 'z12345'
        folder.mkdir()
        manifest: change_page.PageManifest = {"review_list": str(listing), "production": str(production),
                                              "rows": ["C1", "C2"], "token": token}
        _ = (folder / change_page.MANIFEST).write_text(json.dumps(manifest), encoding='utf-8')
        _ = change_review.record_verdict(listing, 'C1', 'works', date(2026, 11, 21), production)
        page = change_page.page_at(self.root, '/z12345/changes.html')
        _ = case.status.begin(page, 'C2', 'validation', 'Check the proof.')
        claim = case.status.path(page, 'C2')
        before = (listing.read_bytes(), claim.read_bytes(), case.fixture.messages[:])

        with patch.object(self.server, 'delivery_status', case.status):
            for row, reason in (('C1', 'this row already has a saved answer'), ('C2', change_page.WITH_SHOWRUNNER)):
                with self.subTest(row=row):
                    status, body = self.post('/z12345/changes.html', urlencode({
                        'row': row, 'token': token, 'outcome': 'validate', 'feedback': ''}))
                    self.assertEqual(status, 400, body)
                    self.assertIn(f'<p role="alert">Not saved: {reason}</p>'.encode(), body)
        self.assertEqual((listing.read_bytes(), claim.read_bytes(), case.fixture.messages), before)

    def test_review_refuses_a_verdict_while_a_change_is_validating(self) -> None:
        case = self.review_fixture(changes=1)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        key = 'page-owner/C1'
        _ = cache.save(case.card_body(cache.get(), key, 'validate', 'Check the change.'), case.status)
        listing = case.fixture.review_list
        claim = change_routing.delivery_path(case.root, listing, 'C1')
        before = (listing.read_bytes(), claim.read_bytes(), dict(case.status.request_attempts))
        snapshot = cache.get()
        revision = review_page.record_revision(snapshot.records[key], snapshot.assets)
        request = case.body(key, 'works') + '&' + urlencode({'revision': revision})
        with (patch.object(self.server, 'review_cache', cache),
              patch.object(self.server, 'delivery_status', case.status),
              patch.object(change_review, 'prepared_replacement') as prepare):
            status, body = self.post(review_page.PAGE_PATH, request)
        prepare.assert_not_called()
        self.assertEqual(status, 400, body)
        self.assertIn(change_page.WITH_SHOWRUNNER.encode(), body)
        self.assertEqual((listing.read_bytes(), claim.read_bytes(), case.status.request_attempts), before)
        self.assertEqual(cache.get().records[key].state, review_items.ReviewState.VALIDATING)
        self.assertEqual(list(case.root.glob('.review-link-scratch-*')), [])

    def test_validating_ask_returns_the_saved_body_and_delivery_state(self) -> None:
        case = self.review_fixture(changes=1)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        key = 'page-owner/C1'
        _ = cache.save(case.card_body(cache.get(), key, 'validate', 'Check the change.'), case.status)
        started, release = threading.Event(), threading.Event()

        def send(_delivery: change_page.MessageDelivery, _status: change_page.DeliveryStatus) -> None:
            started.set()
            self.assertTrue(release.wait(TEST_HANG_GUARD_SECS))

        request = case.card_body(cache.get(), key, 'question', 'Which check passed?')
        with (patch.object(self.server, 'review_cache', cache),
              patch.object(self.server, 'delivery_status', case.status),
              patch.object(change_page, 'deliver', side_effect=send)):
            try:
                code, body = self.post(review_page.PAGE_PATH, request, wait_for_delivery=False)
                self.assertEqual(code, 200, body)
                result = cast(dict[str, object], json.loads(body))
                self.assertIn('Which check passed?', cast(str, result['detail_html']))
                self.assertIs(result['delivery_pending'], True)
                version_path = review_page.PAGE_PATH + '?' + urlencode({'version': '1', 'delivery': result['delivery']})
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                pending = cast(dict[str, object], json.loads(self.request(version_path).body))
                self.assertIs(pending['delivery_pending'], True)
            finally:
                release.set()
                self.assertTrue(self.review_posts.wait_until_none_run())
            settled = cast(dict[str, object], json.loads(self.request(version_path).body))
            self.assertIs(settled['delivery_pending'], False)

    def test_review_body_serves_the_validating_question_in_both_card_views(self) -> None:
        case = self.review_fixture(changes=1)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        key = 'page-owner/C1'
        _ = cache.save(case.card_body(cache.get(), key, 'validate', 'Check the change.'), case.status)
        with patch.object(self.server, 'review_cache', cache):
            table = self.request(review_page.PAGE_PATH + '?' + urlencode({'body': key}))
            card = self.request(review_page.card_url(key))
            missing = self.request(review_page.card_url('missing/key'))
        self.assertEqual(table.status, 200)
        self.assertEqual(card.status, 200)
        self.assertIn(b'<template data-card-body><div class="list-panel review-panel change">', card.body)
        for body in (table.body, card.body):
            self.assertIn(b'<h3>How to check</h3>', body)
            self.assertIn(b'Sends to page-owner', body)
            self.assertIn(b'value="question">Ask a question</button>', body)
            self.assertNotIn(b'value="works"', body)
            self.assertNotIn(b'data-later-toggle', body.split(b'</template>', 1)[0])
        self.assertEqual(missing.status, 404)
        self.assertIn(b'role="status"', missing.body)

    def test_review_recovery_isolates_unreadable_lists_and_releases_locks_before_cleanup(self) -> None:
        case = self.review_fixture(changes=3)
        listing = case.fixture.review_list
        unreadable = listing.with_name('change-review - unreadable.md')
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        recovered = change_page.DeliveryStatus(case.root)
        locks = (case.root/'.review-link-delivery.lock', listing.parent)
        events: list[str] = []
        read_text = Path.read_text

        def read(path: Path, *args: str, **kwargs: str) -> str:
            if path == unreadable:
                raise PermissionError('The unrelated Change list is unreadable.')
            return read_text(path, *args, **kwargs)

        def cleanup(path: Path, row: str) -> list[str]:
            self.assertEqual((path, row), (listing, 'C1'))
            for lock in locks:
                with self.subTest(lock=lock.name):
                    descriptor = os.open(lock, os.O_RDONLY)
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        os.close(descriptor)
            events.append('cleanup')
            return []

        def send(_recipient: staff.StaffId, _sender: str, _message: str, **_options: str) -> None:
            events.append('deliver')

        for row, condition in (('C1', 'locks'), ('C2', 'unreadable'), ('C3', 'intent alone')):
            with self.subTest(condition=condition):
                _ = cache.save(case.card_body(cache.get(), 'page-owner/'+row, 'works', 'The fixture passes.'), case.status)
                notice = StringIO()
                with patch.object(change_page, 'send_to_staff', side_effect=send), redirect_stderr(notice):
                    if condition == 'locks':
                        with patch.object(change_review, 'end_row_scratch', side_effect=cleanup):
                            change_page.recover_pending(recovered)
                        self.assertEqual(events, ['cleanup', 'deliver', 'deliver'])
                    else:
                        _ = unreadable.write_text('A fixture list.')
                        if condition == 'intent alone':
                            delivery = change_routing.delivery_path(case.root, listing, row)
                            delivery.unlink()
                            unreadable_reads = 0

                            def count_read(path: Path, *args: str, **kwargs: str) -> str:
                                nonlocal unreadable_reads
                                unreadable_reads += int(path == unreadable)
                                return read(path, *args, **kwargs)

                            with patch.object(Path, 'read_text', count_read), patch.object(
                                    change_review, 'end_row_scratch', return_value=[]) as clean:
                                change_page.recover_pending(recovered)
                            clean.assert_called_once_with(listing, row)
                            self.assertEqual(unreadable_reads, 0)
                        else:
                            with patch.object(Path, 'read_text', read), patch.object(
                                    change_review, 'end_row_scratch', return_value=[]) as clean:
                                change_page.recover_pending(recovered)
                            clean.assert_any_call(listing, row)
                            self.assertEqual(notice.getvalue().count('The unrelated Change list is unreadable.'), 1)
        self.assertEqual(list(case.root.glob('.review-link-scratch-*')), [])

    def test_review_restart_finishes_saved_verdict_cleanup_before_delivery(self) -> None:
        case = self.review_fixture(changes=1)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        key = 'page-owner/C1'
        events: list[str] = []

        def cleanup(path: Path, row: str) -> list[str]:
            self.assertEqual((path, row), (case.fixture.review_list, 'C1'))
            events.append('cleanup')
            return []

        def send(_recipient: staff.StaffId, _sender: str, _message: str, **_options: str) -> None:
            events.append('deliver')

        with patch.object(change_review, 'end_row_scratch', side_effect=cleanup):
            _ = cache.save(case.card_body(cache.get(), key, 'works', 'The fixture passes.'), case.status)
            self.assertEqual(events, [])
            self.assertNotIn(key, cache.get().records)
            self.assertEqual(len(list(case.root.glob('.review-link-scratch-*'))), 1)
            with patch.object(change_page, 'send_to_staff', side_effect=send):
                change_page.recover_pending(change_page.DeliveryStatus(case.root))
                change_page.recover_pending(change_page.DeliveryStatus(case.root))
        self.assertEqual(events, ['cleanup', 'deliver', 'deliver'])
        self.assertEqual(list(case.root.glob('.review-link-scratch-*')), [])

    def test_review_skips_one_change_when_its_delivery_record_is_unreadable(self) -> None:
        case = self.review_fixture(changes=2)
        key = 'page-owner/C1'
        _ = review_page.submit(case.root, case.body(key, 'question', 'What did you see?'), case.status)
        before = review_page.build_snapshot(case.root)
        self.assertEqual(before.records[key].state, review_items.ReviewState.WAITING)
        path = change_routing.delivery_path(case.root, case.fixture.review_list, 'C1')
        read_text = Path.read_text

        def read(source: Path, *args: str, **kwargs: str) -> str:
            if source == path:
                raise PermissionError('The fixture delivery record is unreadable.')
            return read_text(source, *args, **kwargs)

        with patch.object(Path, 'read_text', read):
            snapshot = review_page.build_snapshot(case.root)
        self.assertEqual(snapshot.records.keys(), before.records.keys()-{key})
        self.assertTrue(any('C1' in notice for notice in snapshot.notices), snapshot.notices)

    def test_review_snapshot_keeps_a_reply_to_a_question_without_an_id(self) -> None:
        case = self.review_fixture()
        key = case.question()
        path = case.question_path(key)
        lines = path.read_text().splitlines(keepends=True)
        index = next(index for index, line in enumerate(lines) if line.startswith('registration: '))
        fields = cast(update_registration.OutstandingItem, json.loads(lines[index].removeprefix('registration: ')))
        self.assertNotIn('review_id', fields)
        fields['followup'] = update_registration.PendingFollowup(text='Which layout works?', sent='2026-11-12T12:01')
        lines[index] = 'registration: '+json.dumps(fields)+'\n'
        _ = path.write_text(''.join(lines))
        self.assertFalse((investigation.state_root() / 'record-ids' / 'question.water').exists())
        record, = update_registration.selected_question_records([path], (), None)
        identities = update_registration.QuestionStaff.for_record(staff.snapshot())
        native_write = update_registration.write_question_record
        project = review_items.question_item
        replied = False

        def reply() -> None:
            nonlocal replied
            if not replied:
                replied = True
                update_registration.reply_record(record, 'The compact layout works.', 'builder-unit', identities=identities)

        def write(before: update_registration.QuestionRecord) -> None:
            if before.path == path:
                reply()
            native_write(before)

        def project_question(before: update_registration.QuestionRecord, members: staff.StaffSnapshot,
                             *, unowned: bool = False, zone: ZoneInfo | None = None) -> review_items.ReviewItem:
            if before.path == path:
                reply()
            return project(before, members, unowned=unowned, zone=zone)

        with patch.object(update_registration, 'write_question_record', write), patch.object(
                review_items, 'question_item', project_question):
            snapshot = review_page.build_snapshot(case.root)
        self.assertIn(key, snapshot.records)
        self.assertTrue(replied)
        current, = update_registration.selected_question_records([path], (), None)
        self.assertNotIn('followup', current.item)
        self.assertEqual(current.item.get('followups'), [{'followup':'Which layout works?',
                                                        'reply':'The compact layout works.'}])
        self.assertEqual(current.item.get('review_id'), 'Q1')

    def test_review_stale_waiting_question_refuses_after_owner_reply(self) -> None:
        case = self.review_fixture()
        key = case.question()
        path = case.question_path(key)
        _ = review_page.submit(case.root, case.body(key, 'question', 'Which case passed?'), case.status)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        stale = case.card_body(cache.get(), key, 'answer', 'Use that case.')
        record, = update_registration.selected_question_records([path], (), None)
        update_registration.answer_followup(record.item, 'The second case.')
        update_registration.write_question_record(record)
        before = path.read_bytes()
        with patch.object(self.server, 'review_cache', cache), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'):
            status, reply = self.post(review_page.PAGE_PATH, stale)
            self.assertEqual(status, 409, reply)
        self.assertEqual(path.read_bytes(), before)

    def test_review_rejects_an_unavailable_assignee_before_saving(self) -> None:
        case = self.review_fixture()
        proposal, efficiency = case.decisions()
        worker = fixture_member('worker', name='worker', parent=case.fixture.unit.id)
        wrapped = fixture_member('showrunner', name='wrapped',
                                 doc=staff.WrappedDoc(local_machine(), case.fixture.temporary / 'wrapped.md'))
        orphan = fixture_member('unit', name='without-showrunner', parent=wrapped.id)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        snapshot = cache.get()
        with (patch.object(self.server, 'review_cache', cache),
              patch.object(self.server, 'delivery_status', case.status),
              patch.object(change_page, 'send_to_staff') as send):
            for key in (proposal, efficiency):
                item = snapshot.records[key]
                path = review_items.source_path(item)
                before = path.read_bytes()
                request = case.card_body(snapshot, key, 'approve')
                for assignee in ('unknown', worker.id, wrapped.id):
                    with self.subTest(key=key, assignee=assignee):
                        status, reply = self.post(review_page.PAGE_PATH, request+'&'+urlencode({'assignee': assignee}))
                        self.assertEqual(status, 400, reply)
                        self.assertIn(b'Choose a current showrunner or unit director.', reply)
                if key == proposal:
                    status, reply = self.post(review_page.PAGE_PATH, request+'&'+urlencode({'assignee': orphan.id}))
                    self.assertEqual(status, 400, reply)
                    self.assertIn(b'The selected unit director has no current parent showrunner.', reply)
                self.assertEqual(path.read_bytes(), before)
                current = review_items.read_current(case.root, item, staff.snapshot())
                self.assertEqual(current.state, item.state)
                self.assertEqual(current.log, item.log)
            send.assert_not_called()

    def test_review_shows_a_refused_unit_approval_without_saving(self) -> None:
        case = self.review_fixture()
        proposal, _ = case.decisions()
        path = Path(proposal.removeprefix('proposal/'))
        before = path.read_bytes()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        request = case.card_body(cache.get(), proposal, 'approve')+'&'+urlencode({'assignee': case.fixture.unit.id})
        row = path.stem
        page = change_page.ReviewPage(path, path.parent, (row,), '')
        reason = 'The issue changed before approval.'
        with (patch.object(self.server, 'review_cache', cache),
              patch.object(self.server, 'delivery_status', case.status),
              patch.object(proposal_issue, 'write_issue', side_effect=proposal_issue.Refusal(reason)) as write,
              patch.object(change_page, 'send_to_staff') as send):
            status, reply = self.post(review_page.PAGE_PATH, request)
            self.assertEqual(status, 400, reply)
            self.assertEqual(json.loads(reply), {'reason': reason})
            write.assert_called_once()
            send.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(case.status.path(page, row).exists())
        self.assertFalse(case.status.request_attempts)

    def test_review_unit_approval_uses_the_parent_after_a_transfer(self) -> None:
        case = self.review_fixture()
        proposal, _ = case.decisions()
        path = Path(proposal.removeprefix('proposal/'))
        previous_parent = fixture_member('showrunner', name='former-parent')
        current_parent = fixture_member('showrunner', name='current-parent')
        unit = fixture_member('unit', name='implementation', parent=previous_parent.id)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        snapshot = cache.get()
        request = case.card_body(snapshot, proposal, 'approve')+'&'+urlencode({'assignee': unit.id})
        issue_lock = proposal_issue.issue_lock

        @contextmanager
        def transfer_under_lock(target: Path) -> Generator[None]:
            with issue_lock(target):
                if target == path:
                    previous = staff.snapshot()
                    _ = staff.move(unit.id, staff.ReportsTo(current_parent.id))
                    with staff.supplied_snapshot(previous):
                        yield
                else:
                    yield

        with (patch.object(self.server, 'review_cache', cache),
              patch.object(self.server, 'delivery_status', case.status),
              patch.object(proposal_issue, 'issue_lock', transfer_under_lock),
              patch.object(change_page, 'deliver')):
            status, reply = self.post(review_page.PAGE_PATH, request)
            self.assertEqual(status, 200, reply)
        saved = review_items.read_current(case.root, snapshot.records[proposal], staff.snapshot())
        assert isinstance(saved.source, review_items.ProposalSource)
        self.assertEqual(saved.source.record.value('unit'), unit.id)
        self.assertEqual(saved.source.record.value('approved_in'), current_parent.id)

    def test_review_rejects_the_old_form_after_a_new_revision_is_published(self) -> None:
        case = self.review_fixture()
        proposal, _ = case.decisions()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        previous = cache.get()
        request = case.card_body(previous, proposal, 'approve')
        _ = review_page.submit(case.root, case.body(proposal, 'question', 'What changed?'), case.status)
        fresh, _ = cache.publish_record(previous, proposal)
        self.assertEqual(fresh.records[proposal].state, review_items.ReviewState.WAITING)
        with patch.object(self.server, 'review_cache', cache), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(
                review_actions, 'decision_action', return_value=(change_page.NoMessageDelivery(), 'approved')) as action:
            status, reply = self.post(review_page.PAGE_PATH, request)
            self.assertEqual(status, 409, reply)
            action.assert_not_called()
            reason = cast(dict[str, str], json.loads(reply))['reason']
            self.assertIn('changed', reason)

    def test_review_rejects_a_form_for_changed_text_in_the_same_state(self) -> None:
        case = self.review_fixture()
        proposal, _ = case.decisions()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        previous = cache.get()
        request = case.card_body(previous, proposal, 'approve')
        path = Path(proposal.removeprefix('proposal/'))
        with patch.object(self.server, 'review_cache', cache), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(
                review_actions, 'decision_action', return_value=(change_page.NoMessageDelivery(), 'approved')) as action:
            first = self.request(review_page.card_url(proposal))
            self.assertEqual(first.status, 200)
            _ = path.write_text(path.read_text().replace('Run fewer checks.', 'Run a different check.'))
            status, reply = self.post(review_page.PAGE_PATH, request)
            self.assertEqual(status, 409, reply)
            action.assert_not_called()
            fresh = cache.get()
            self.assertEqual(fresh.records[proposal].state, previous.records[proposal].state)
            revision = review_page.record_revision(fresh.records[proposal], fresh.assets)
            self.assertNotIn(revision.encode(), first.body)
            card = self.request(review_page.card_url(proposal))
            self.assertEqual(card.status, 200)
            self.assertIn(b'Run a different check.', card.body)
            self.assertIn(f'name="revision" value="{revision}"'.encode(), card.body)
            table_body = self.request(review_page.PAGE_PATH + '?' + urlencode({'body': proposal, 'version': fresh.version}))
            self.assertEqual(table_body.status, 200)
            self.assertIn(b'Run a different check.', table_body.body)
            self.assertIn(f'name="revision" value="{revision}"'.encode(), table_body.body)
            status, reply = self.post(review_page.PAGE_PATH, case.card_body(fresh, proposal, 'approve'))
            self.assertEqual(status, 200, reply)
            action.assert_called_once()

    def test_review_returns_success_and_delivers_if_the_saved_record_cannot_be_read(self) -> None:
        case = self.review_fixture()
        proposal, _ = case.decisions()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        snapshot = cache.get()
        request = case.card_body(snapshot, proposal, 'approve')
        complete = threading.Event()
        read = review_items.read_current
        record = read(case.root, snapshot.records[proposal], snapshot.members)
        deliver = change_page.deliver
        messages: list[str] = []

        def send(_recipient: staff.StaffId, _sender: str, message: str, **_options: str) -> None:
            messages.append(message)

        def deliver_then_signal(message: change_page.MessageDelivery | change_page.NoMessageDelivery,
                                status: change_page.DeliveryStatus) -> None:
            deliver(message, status)
            complete.set()

        self.addCleanup(cache.close)
        with patch.object(self.server, 'review_cache', cache), patch.object(
                self.server, 'delivery_status', case.status), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(
                cache, 'invalidate', wraps=cache.invalidate) as invalidate, patch.object(
                change_page, 'send_to_staff', side_effect=send), patch.object(
                change_page, 'deliver', side_effect=deliver_then_signal), patch.object(
                review_items, 'read_current', side_effect=[record, record,
                    OSError('The saved record cannot be read.')]):
            status, reply = self.post(review_page.PAGE_PATH, request)
            self.assertEqual(status, 200, reply)
            self.assertTrue(complete.wait(TEST_HANG_GUARD_SECS))
            self.assertEqual(len(messages), 1)
            self.assertIn('Approve accepts the proposed ranking', messages[0])
            self.assertFalse(case.status.request_attempts)
            invalidate.assert_called()
            self.assertIs(cache.get(), snapshot)
        cache.start()
        cache.synchronize()
        self.assertNotEqual(cache.get().version, snapshot.version)
        self.assertNotIn(proposal, cache.get().records)

    def test_review_keeps_a_verdict_after_cleanup_or_message_failure(self) -> None:
        case = self.review_fixture(changes=2)
        shot = case.fixture.temporary/'review.png'
        _ = shot.write_bytes(b'fixture image')
        image = image_store.register_change(shot)
        _ = change_review.edit_row(case.fixture.review_list, 'C1', change_review.KeepRowText(),
                                  datetime(2026, 11, 1).date(), case.fixture.production, image=image)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        with patch.object(self.server, 'review_cache', cache), patch.object(
                self.server, 'delivery_status', case.status), patch.object(
                change_page, 'send_to_staff') as sent, patch.object(cache, 'invalidate', wraps=cache.invalidate) as invalidate:
            for finding, row, module, method in (
                    ('F033', 'C1', image_store, 'remove_change'),
                    ('F039', 'C2', change_page, 'review_message')):
                with self.subTest(finding=finding):
                    sent.reset_mock()
                    invalidate.reset_mock()
                    case.fixture.delivery_done.clear()
                    request = case.card_body(cache.get(), 'page-owner/'+row, 'works')
                    with patch.object(module, method, side_effect=PermissionError('The fixture step failed.')) as failed:
                        status, reply = self.post(review_page.PAGE_PATH, request)
                        self.assertEqual(status, 200, reply)
                        self.assertTrue(case.fixture.delivery_done.wait(TEST_HANG_GUARD_SECS))
                        failed.assert_called()
                    self.assertEqual(change_review.review_snapshot(case.fixture.review_list).reviewed[0]['#'], row)
                    sent.assert_called()
                    invalidate.assert_called()

    def test_review_retries_delivery_when_the_claim_fails_after_save(self) -> None:
        case = self.review_fixture()
        proposal, _ = case.decisions()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        deliver = change_page.deliver
        retried = threading.Event()
        first_claim = True

        def fail_first_claim(delivery: change_page.MessageDelivery | change_page.NoMessageDelivery,
                             status: change_page.DeliveryStatus) -> None:
            nonlocal first_claim
            if first_claim:
                first_claim = False
                with patch.object(status, 'locked', side_effect=PermissionError('The claim failed.')):
                    deliver(delivery, status)
            else:
                deliver(delivery, status)
                retried.set()

        with patch.object(self.server, 'review_cache', cache), patch.object(
                self.server, 'delivery_status', case.status), patch.object(
                change_page, 'deliver', side_effect=fail_first_claim), patch.object(
                change_page, 'send_to_staff') as sent:
            status, reply = self.post(review_page.PAGE_PATH, case.card_body(cache.get(), proposal, 'approve'))
            self.assertEqual(status, 200, reply)
            self.assertTrue(retried.wait(TEST_HANG_GUARD_SECS))
            sent.assert_called_once()
            self.assertFalse(case.status.request_attempts)
            self.assertNotIn(proposal, cache.get().records)

    def test_review_conflicts_when_proposal_rank_or_state_changes_or_the_asset_disappears(self) -> None:
        case = self.review_fixture()
        key, _ = case.decisions()
        path = Path(key.removeprefix('proposal/'))
        original = path.read_text()
        lock = proposal_issue.issue_lock
        read = proposal_issue.read_issue
        entered = threading.Event()

        @contextmanager
        def activate_before_lock(target: Path) -> Generator[None]:
            if target == path and not entered.is_set():
                entered.set()
                _ = target.write_text(original.replace('stage: backlog', 'stage: active')+'\n')
            with lock(target):
                yield

        def delete_before_asset_read(target: Path) -> str:
            if target == path:
                target.unlink(missing_ok=True)
            return read(target)

        with patch.object(self.server, 'delivery_status', case.status), patch.object(change_page, 'send_to_staff') as sent:
            for finding in ('F037', 'F040', 'F038'):
                with self.subTest(finding=finding):
                    sent.reset_mock()
                    case.fixture.delivery_done.clear()
                    _ = path.write_text(original)
                    cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
                    self.addCleanup(cache.close)
                    snapshot = cache.get()
                    request = case.card_body(snapshot, key, 'decline')
                    if finding == 'F037':
                        _ = path.write_text(original.replace('stage: backlog', 'stage: backlog\nbacklog_risk: ★★★'))
                    with patch.object(self.server, 'review_cache', cache), patch.object(proposal_issue, 'issue_lock',
                        side_effect=activate_before_lock if finding == 'F040' else lock), patch.object(
                            proposal_issue, 'read_issue', side_effect=delete_before_asset_read if finding == 'F038' else read):
                        status, reply = self.post(review_page.PAGE_PATH, request)
                    if status == 200:
                        self.assertTrue(case.fixture.delivery_done.wait(TEST_HANG_GUARD_SECS))
                    self.assertEqual(status, 409, reply)
                    sent.assert_not_called()
                    if finding == 'F040':
                        self.assertTrue(entered.is_set())
                        self.assertIn('stage: active', path.read_text())
                        self.assertNotIn('style_answer:', path.read_text())

    def test_review_rechecks_each_record_after_the_first_read(self) -> None:
        case = self.review_fixture(changes=1)
        question = case.question()
        report = case.investigation()
        change = 'page-owner/C1'
        investigation_key = f'investigation/{report.owner}/{report.id}'
        self.assert_stale_review_actions(case, (
            (change, 'works'), (change, 'question'), (change, 'validate'),
            (question, 'answer'), (investigation_key, 'read'), (question, 'later')))

        _, efficiency = case.decisions()
        config = Path(__file__).resolve().parents[2]/'config/nightly_review.conf'
        _ = shutil.copyfile(config, case.fixture.temporary/'config/nightly_review.conf')
        self.assert_stale_review_actions(case, ((efficiency, 'approve'), (efficiency, 'decline')))

    def assert_stale_review_actions(self, case: test_review_page.ReviewPageTests,
                                    scenarios: tuple[tuple[str, str], ...]) -> None:
        for key, outcome in scenarios:
            with self.subTest(key=key, outcome=outcome):
                cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
                try:
                    self.assert_conflict_after_a_new_later_time(case, cache, key, outcome)
                finally:
                    cache.close()
                    review_later.path().unlink(missing_ok=True)

    def assert_conflict_after_a_new_later_time(self, case: test_review_page.ReviewPageTests,
                                               cache: review_page.ReviewSnapshotCache, key: str, outcome: str) -> None:
        read = review_items.read_current
        until = datetime.fromisoformat('2031-12-01T18:30:00-05:00')
        snapshot = cache.get()
        request = case.card_body(snapshot, key, outcome, 'These words must stay unsaved.')
        if outcome == 'later':
            request += '&later=2099-01-01T12%3A00'
        changed = False
        path = review_items.source_path(snapshot.records[key])
        before = path.read_bytes()

        def insert_later_after_read(root: Path, item: review_items.ReviewItem,
                                    members: staff.StaffSnapshot) -> review_items.ReviewItem:
            nonlocal changed
            current = read(root, item, members)
            if not changed:
                changed = True
                review_later.save(key, until)
            return current

        with patch.object(self.server, 'review_cache', cache), patch.object(
                self.server, 'delivery_status', case.status), patch.object(change_page, 'send_to_staff') as sent, patch.object(
                review_items, 'read_current', side_effect=insert_later_after_read):
            status, reply = self.post(review_page.PAGE_PATH, request)
            if status == 200:
                self.assertTrue(case.fixture.delivery_done.wait(TEST_HANG_GUARD_SECS))
            self.assertEqual(status, 409, reply)
            sent.assert_not_called()
        self.assertTrue(changed)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(review_later.times().get(key), until)
        self.assertEqual(cache.get().records[key].state, snapshot.records[key].state)

    def test_review_accepts_later_on_an_open_question(self) -> None:
        case = self.review_fixture()
        key = case.question()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        snapshot = cache.get()
        self.assertEqual(snapshot.records[key].state, review_items.ReviewState.OPEN)
        with patch.object(self.server, 'review_cache', cache), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'):
            status, reply = self.post(review_page.PAGE_PATH,
                case.card_body(snapshot, key, 'later')+'&later=2030-12-02T18%3A30')
            self.assertEqual(status, 200, reply)
            item = cache.get().records[key]
            self.assertEqual(item.state, review_items.ReviewState.OPEN)
            self.assertIsInstance(item.times.later, datetime)

            with self.subTest('Question log rollback'):
                path = review_items.source_path(item)
                prior_log = path.read_bytes()
                prior_deadline = review_later.path().read_bytes()
                note = 'Keep the previous deadline if this note rolls back.'
                sync = atomic_file.sync_folder
                failed = False

                def fail_question_sync(folder: Path) -> None:
                    nonlocal failed
                    if folder == path.parent and not failed:
                        self.assertIn(note, path.read_text(encoding='utf-8'))
                        failed = True
                        raise OSError('The Question directory sync failed.')
                    sync(folder)

                with patch.object(atomic_file, 'sync_folder', side_effect=fail_question_sync):
                    status, reply = self.post(review_page.PAGE_PATH,
                        case.card_body(cache.get(), key, 'later', note)+'&later=2030-12-03T18%3A30')
                self.assertTrue(failed)
                self.assertEqual(status, 503, reply)
                self.assertEqual(path.read_bytes(), prior_log)
                self.assertEqual(review_later.path().read_bytes(), prior_deadline)

    def test_review_shows_only_current_question_failures(self) -> None:
        case = self.review_fixture(changes=1)
        report = case.investigation()
        investigation_key = f'investigation/{report.owner}/{report.id}'
        proposal, efficiency = case.decisions()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        for key in ('page-owner/C1', investigation_key, proposal, efficiency):
            with self.subTest(key=key):
                saved = cache.save(case.card_body(cache.get(), key, 'question', 'Which checks passed?'), case.status)
                assert isinstance(saved.delivery, change_page.MessageDelivery)
                with patch.object(change_page, 'send_to_staff', side_effect=ValueError('The fixture owner is offline.')):
                    change_page.deliver(saved.delivery, case.status)
                self.assertNotIn('question-delivery:'+key, cache.get().assets)
                cache.publish_delivery_result(key)
                current = cache.get()
                self.assertIn('Question not delivered.', current.assets['question-delivery:'+key])
                self.assertIn(b'data-question-delivery-failed', current.body(key))
                if key == investigation_key:
                    question = case.status.slots(saved.delivery.page, saved.delivery.row).question
                    assert isinstance(question, change_page.OpenQuestion)
                    _ = investigation.answer(report.owner, report.id, 'The smoke checks passed.',
                                             question_id=question.question_id)
                    cache.publish_delivery_result(investigation_key)
                    self.assertNotIn('question-delivery:'+investigation_key, cache.get().assets)
                elif key in (proposal, efficiency):
                    self.assertNotIn('question-delivery:'+investigation_key, current.assets)

    def test_review_returns_conflict_for_a_deleted_investigation(self) -> None:
        case = self.review_fixture()
        report = case.investigation()
        key = f'investigation/{report.owner}/{report.id}'
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        request = case.card_body(cache.get(), key, 'read')
        (investigation.folder(report.owner)/(report.id+'.md')).unlink()
        with patch.object(self.server, 'review_cache', cache), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'):
            status, reply = self.post(review_page.PAGE_PATH, request)
            self.assertEqual(status, 409, reply)
            self.assertIn(b'no longer on the list', reply)
        self.assertNotIn(key, cache.get().records)

    def test_review_verdict_and_question_answer_read_only_the_selected_source(self) -> None:
        case = self.review_fixture(changes=2)
        question = case.question()
        other = case.question('A different question?')
        request, _ = review_page.submit(case.root, case.body('page-owner/C2', 'question', 'What did you see?'), case.status)
        assert isinstance(request, change_page.MessageDelivery)
        pending = case.status.slots(request.page, request.row).question
        assert isinstance(pending, change_page.OpenQuestion)
        _ = change_page.answer_item('page-owner/C2', 'The check passed.', pending.question_id, case.status)
        path = case.question_path(other)
        _ = path.write_text(path.read_text().replace('registration: {', 'registration: {bad'))
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        with patch.object(self.server, 'review_cache', cache), patch.object(self.server, 'delivery_status', case.status), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(change_page, 'deliver'), patch.object(
                change_browser, 'prepare', side_effect=AssertionError('A save scanned the Review stores.')), patch.object(
                review_items, 'read', side_effect=AssertionError('A save scanned the Review stores.')), patch.object(
                next_item, 'read_queue', side_effect=AssertionError('A save scanned another queue.')):
            for key, outcome in (('page-owner/C1', 'works'), (question, 'answer'), ('page-owner/C2', 'validate')):
                with self.subTest(key=key):
                    status, reply = self.post(review_page.PAGE_PATH, case.card_body(cache.get(), key, outcome, 'Use this result.'))
                    self.assertEqual(status, 200, reply)

    def test_other_text_files_are_readable_as_utf8(self) -> None:
        text = "a report with no standard text extension — café\n"
        _ = (self.root / "report.log").write_text(text, encoding="utf-8")
        reply = self.request("/report.log")
        self.assertEqual(reply.status, 200)
        self.assertTrue(reply.content_type.startswith("text/"), reply.content_type)
        self.assertIn("charset=utf-8", reply.content_type)
        self.assertEqual(reply.body.decode("utf-8"), text)

    def head(self, path: str) -> tuple[int, dict[str, str], bytes]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, {name.lower(): value for name, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def test_console_route_renders_current_navigation_at_one_fixed_address(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "REVIEW_LINKS_ROOT": str(self.root), "REVIEW_LINKS_HOST": "127.0.0.1",
            "STAFF_FILE": str(self.root.parent / "staff.json")})))
        _ = self.enterContext(patch.object(memory_feed, "page_url", return_value="/builds/memory_live.html", create=True))
        _ = self.enterContext(patch.object(aws_page, "page_url", return_value="/aws/aws_prices.html"))
        status, headers, body = self.head("/console/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn("frame-src 'self'", headers["content-security-policy"])
        self.assertNotEqual(headers.get("x-frame-options"), "DENY")
        self.assertIn(b"<h1>Hana console</h1>", body)
        self.assertIn(b'data-url="/console/review.html" data-title="Review"', body)
        self.assertIn(b'href="?page=rebuilds" data-page="rebuilds"\ndata-url="/console/rebuilds.html"', body)
        self.assertTrue((self.root / change_page.CONSOLE_FOLDER / change_page.BROWSER_MANIFEST).is_file())
        for path in ("/console", "/console?page=questions"):
            with self.subTest(path=path):
                status, headers, _ = self.head(path)
                self.assertEqual((status, headers["location"]), (303, "/console/" + path[len("/console"):]))
        for path in ("/../console/", "/%2e%2e/console/", "/.review-link-console.html"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path).status, 404)

    def post(self, path: str, body: str, *, origin: str = "", headers: Mapping[str, str] | None = None,
             wait_for_delivery: bool = True) -> tuple[int, bytes]:
        review_post = path == review_page.PAGE_PATH
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            sent = {"Content-Type": "application/x-www-form-urlencoded"}
            if origin:
                sent["Origin"] = origin
            connection.request("POST", path, body, {**sent, **(headers or {})})
            response = connection.getresponse()
            content = response.read()
            if review_post and wait_for_delivery:
                self.assertTrue(self.review_posts.wait_until_none_run())
            return response.status, content
        finally:
            connection.close()

    def test_the_disks_measure_answers_at_once_with_the_measure_state_the_slots_and_the_sessions(self) -> None:
        from scripts.disks import disks_page, sessions, slots

        _ = self.enterContext(patch.object(serve, "tailscale_ip", return_value="127.0.0.1"))
        _ = self.enterContext(patch.object(disks_page, "output_folder", return_value=self.root / "disks"))
        start = self.enterContext(patch.object(disks_page, "start_measure", return_value=disks_page.MeasureRequest(disks_page.MeasureStart.STARTED, 7)))
        report: slots.SlotReport = {"root": "/build/slot", "machine": "natedev", "unused_days": 3, "groups": []}
        _ = self.enterContext(patch.object(slots, "slot_report", return_value=report))
        folders: sessions.SessionReport = {"root": "/session", "sessions": []}
        _ = self.enterContext(patch.object(sessions, "session_report", return_value=folders))
        answer = (b'{"state": "started", "target": 7, "slots": {"root": "/build/slot", "machine": "natedev", "unused_days": 3, "groups": []}, '
                  + b'"sessions": {"root": "/session", "sessions": []}}')
        self.assertEqual(self.post("/disks/measure", ""), (200, answer))
        self.assertEqual(start.call_count, 1)
        start.assert_called_once_with(ANY, force=False)
        self.assertNotEqual(self.post("/other/measure", "")[0], 200)
        self.assertEqual(start.call_count, 1)
        start.reset_mock()
        self.assertEqual(self.post("/disks/measure", "force=1"), (200, answer))
        start.assert_called_once_with(ANY, force=True)

    def test_review_records_signed_for_you_completion_once(self) -> None:
        case = self.review_fixture()
        store = ActionStore()
        action = store.add(title='Restore GitHub access', command='github-warmup', machine='natedev',
                           cwd='/fixture', requested_by=case.fixture.unit.id, reason='The login needs your browser.',
                           waits='The release check', concerns='admin-console', dedupe_key='github-access')
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        snapshot = cache.get()
        self.assertIn(b'<code>github-warmup</code>', snapshot.body(action.key))
        request = case.card_body(snapshot, action.key, 'done')
        with (patch.object(self.server, 'review_cache', cache),
              patch.object(self.server, 'delivery_status', case.status)):
            status, reply = self.post(review_page.PAGE_PATH, request.replace('token=', 'token=wrong'))
            self.assertEqual(status, 400, reply)
            self.assertEqual(store.get(action.key), action)
            with patch.object(atomic_file, 'sync_folder', side_effect=OSError('The action directory sync failed.')):
                status, reply = self.post(review_page.PAGE_PATH, request)
            self.assertEqual(status, 200, reply)
            self.assertEqual(json.loads(reply)['state'], 'Done')
            completed = store.get(action.key)
            self.assertEqual(completed.status, 'done')
            self.assertTrue(completed.completed_at)
            self.assertEqual(completed.evidence, 'Marked Done in Review.')
            with patch.object(ActionStore, 'mark_done', side_effect=AssertionError('Done must not write again.')):
                status, reply = self.post(review_page.PAGE_PATH, request)
            self.assertEqual(status, 200, reply)
            self.assertEqual(json.loads(reply)['state'], 'Done')
            self.assertEqual(store.get(action.key), completed)
        self.assertNotIn(b'value="done"', cache.get().body(action.key))

    def test_for_you_json_reads_global_actions_without_caching_the_response(self) -> None:
        payload: console.ForYouPayload = {"available": True, "count": 0, "entries": []}
        with patch.object(console, "for_you_payload", return_value=payload) as read:
            status, headers, body = self.head("/console/for-you.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(json.loads(body), payload)
        self.assertEqual(read.call_count, 1)
        with patch.object(console, "for_you_payload", side_effect=OSError("Fixture read failed.")):
            reply = self.request("/console/for-you.json")
        self.assertEqual(reply.status, 503)
        self.assertEqual(json.loads(reply.body), {"available": False})

    def test_for_you_count_view_sends_no_command_text(self) -> None:
        row = for_you.ForYouRow("action/fixture", "action", "Long command", "x" * 70000,
                               "mac", "/fixture", "sdlc", "Login", "Release", "", "open", "manual", "")
        rebuild = replace(row, key="rebuild/mac", kind="rebuild", title="Rebuild mac", command="rebuild")
        snapshot = for_you.ForYouSnapshot((row, rebuild), (), ("mac",))
        with patch.object(for_you, "read", return_value=snapshot) as read:
            status, headers, body = self.head("/console/for-you.json?view=count")
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(json.loads(body), {"available": True, "count": 2, "other_count": 1, "rebuild_machines": ["mac"]})
        read.assert_called_once_with(include_comparison_reason=False)

    def test_for_you_count_view_keeps_rebuilds_when_the_read_exceeds_the_recursion_limit(self) -> None:
        rebuild = rebuild_row.RebuildAnswer(("natedev", "mac"), False)
        with (patch.object(for_you, "read", side_effect=RecursionError("Fixture read failed.")),
              patch.object(rebuild_row, "read", return_value=rebuild)):
            status, _, body = self.head("/console/for-you.json?view=count")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"available": False, "rebuild_machines": ["natedev", "mac"]})

    def test_console_news_records_the_open_page_and_names_pages_with_new_items(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "REVIEW_LINKS_ROOT": str(self.root), "REVIEW_LINKS_HOST": "127.0.0.1",
            "STAFF_FILE": str(self.root.parent / "staff.json")})))
        _ = self.enterContext(patch.object(serve, "tailscale_ip", return_value="127.0.0.1"))
        _ = self.enterContext(patch.object(change_browser, "waiting_changes", return_value=frozenset({"a/C1"})))
        questions = self.enterContext(patch.object(review_page, "waiting_items", return_value=frozenset({"q1"})))
        self.assertEqual(self.post("/console/news", "open=images"), (200, b'{"new": []}'))
        questions.return_value = frozenset({"q1", "q2"})
        self.assertEqual(self.post("/console/news", "open=images"), (200, b'{"new": ["review"]}'))
        self.assertEqual(self.post("/console/news", "open=review"), (200, b'{"new": []}'))
        # The Rebuilds page names no open page; the marks still follow new items.
        questions.return_value = frozenset({"q1", "q2", "q3"})
        self.assertEqual(self.post("/console/news", "open="), (200, b'{"new": ["review"]}'))
        for body in ("open=nowhere", "", "open=images&open=review"):
            with self.subTest(body=body):
                self.assertEqual(self.post("/console/news", body)[0], 400)
        self.assertEqual(self.request("/console/" + console.SEEN_NAME).status, 404)

    def test_investigation_mark_read_saves_and_reports_refused_or_failed_requests(self) -> None:
        _ = self.enterContext(patch.object(serve, "tailscale_ip", return_value="127.0.0.1"))
        morty = fixture_member("producer", name="morty")
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((morty,))))
        owner = staff.StaffId("fixture-owner")
        item = investigation.file(owner, "Find the cause", "Why?", waiting_why='The owner must complete the investigation.')
        fields = {"owner": owner, "id": item.id,
                  "token": investigations_page.request_token(self.root, owner, item.id)}
        self.assertEqual(self.request(investigations_page.PAGE_PATH).status, 303)
        self.assertEqual(self.post(investigations_page.PAGE_PATH, urlencode(fields))[0], 400)
        _ = investigation.answer(owner, item.id, "Found it.")
        for body in (urlencode({**fields, "token": "bad"}), urlencode(fields) + '&owner=other'):
            with self.subTest(body=body):
                self.assertEqual(self.post(investigations_page.PAGE_PATH, body)[0], 400)
                self.assertEqual(investigation.load(owner, item.id).state, investigation.InvestigationState.ANSWER_READY)
        with patch.object(investigations_page, "submit", side_effect=OSError("fixture write failed")):
            status, body = self.post(investigations_page.PAGE_PATH, urlencode(fields))
            self.assertEqual(status, 503)
            self.assertIn(b'"reason"', body)
        self.assertEqual(investigation.load(owner, item.id).state, investigation.InvestigationState.ANSWER_READY)
        self.assertEqual(self.post(investigations_page.PAGE_PATH, urlencode(fields)), (200, b'{"state": "read"}'))
        self.assertEqual(investigation.load(owner, item.id).state, investigation.InvestigationState.READ)
        item = the_list.add(morty.id, "Read the List page", owner=the_list.UnassignedOwner.NATE,
                            source="nate", changes={})
        response = self.request(list_page.PAGE_PATH)
        self.assertEqual(response.status, 200)
        self.assertIn(b"Read the List page", response.body)
        _ = the_list.update(morty.id, item.id, {"title": "Read the new List page"}, source="morty")
        self.assertIn(b"Read the new List page", self.request(list_page.PAGE_PATH).body)
        self.assertNotEqual(self.post(list_page.PAGE_PATH, "id=L1")[0], 200)
        with patch.object(list_page, "render", side_effect=OSError("fixture read failed")):
            failed = self.request(list_page.PAGE_PATH)
            self.assertEqual(failed.status, 503)
            self.assertIn(b"The List page is not available.", failed.body)

    def test_list_serves_published_review_links_and_counts_in_separate_requests(self) -> None:
        case = self.review_fixture(changes=1)
        cache = serve.ConsoleReviewSnapshotCache(case.root, start_worker=False)
        snapshot = cache.get()
        change = next(item for item in snapshot.records.values() if item.type == review_items.ReviewType.CHANGE)
        owner = fixture_member("producer", name="morty").id
        item = the_list.add(owner, "Read the published review", owner=the_list.UnassignedOwner.NATE,
                            source="nate", changes={})
        _ = the_list.link(owner, item.id, (change.key,), source="nate")
        footer = '<aside class="list-counts">2 questions</aside>'
        with (patch.object(self.server, "root", case.root), patch.object(self.server, "review_cache", cache),
              patch.object(list_page, "counts_strip", return_value=footer) as counts,
              patch.object(list_page, "read_linked_reviews", side_effect=AssertionError("The List read all reviews."))):
            page = self.request(list_page.PAGE_PATH)
            self.assertEqual(page.status, 200)
            self.assertIn(b"Read the published review", page.body)
            self.assertIn(b"1 change for you", page.body)
            self.assertIn(b'data-list-counts-url="/console/list.html?counts=1"', page.body)
            self.assertNotIn(footer.encode(), page.body)
            counts.assert_not_called()
            with patch.object(list_page, "records", side_effect=AssertionError("The counts request read the List.")):
                response = self.request(f"{list_page.PAGE_PATH}?{list_page.COUNTS_QUERY}")
            self.assertEqual((response.status, response.body), (200, footer.encode()))
            counts.assert_called_once()

    def test_list_reads_a_change_missing_from_a_partial_review_snapshot(self) -> None:
        case = self.review_fixture(changes=1)
        change = next(item for item in review_items.read(case.root) if item.type == review_items.ReviewType.CHANGE)
        owner = fixture_member("producer", name="morty").id
        item = the_list.add(owner, "Read the omitted review", owner=the_list.UnassignedOwner.NATE,
                            source="nate", changes={})
        _ = the_list.link(owner, item.id, (change.key,), source="nate")
        with patch.dict(os.environ, {"NEXT_STATE_DIR": str(self.root / "fixture-next")}):
            queue = urgency.queue_state_path(change.queue)
            queue.parent.mkdir()
            _ = queue.write_text("invalid JSON", encoding="utf-8")
            cache = serve.ConsoleReviewSnapshotCache(case.root, start_worker=False)
            self.assertNotIn(change.key, cache.get().records)
            with (patch.object(self.server, "root", case.root), patch.object(self.server, "review_cache", cache),
                  patch.object(self.server, "delivery_status", case.status), patch.object(list_page, "deliver"),
                  patch.object(list_page, "read_linked_reviews", wraps=list_page.read_linked_reviews) as read):
                page = self.request(list_page.PAGE_PATH)
                self.assertEqual(page.status, 200)
                self.assertIn(b"Checking linked reviews", page.body)
                self.assertNotIn(b"Reviews done", page.body)
                read.assert_not_called()
                updates = self.request(f"{list_page.PAGE_PATH}?{list_page.COUNTS_AND_LINKS_QUERY}")
                read.assert_called_once()
                request = uuid4().hex
                fields = {"keeper": owner, "id": item.id, "request": request, "words": "Which check passed?",
                          "until": "", "token": list_page.request_token(case.root, owner, item.id, request),
                          "action": "question"}
                read.reset_mock()
                status, body = self.post(list_page.PAGE_PATH, urlencode(fields))
                self.assertEqual(status, 200)
                read.assert_called_once()
                recovery = urlencode({"item-state": item.id, "keeper": owner})
                read.reset_mock()
                recovered = self.request(f"{list_page.PAGE_PATH}?{recovery}")
                self.assertEqual(recovered.status, 200)
                read.assert_called_once()
                for source, response in (("action", body), ("recovery", recovered.body)):
                    with self.subTest(source=source):
                        saved = cast(list_page.SavedActionState, json.loads(response))
                        self.assertEqual(saved["waiting_for_answer"], "true")
                        panel = saved.get("waiting_details_html", "")
                        self.assertIn("1 change for you", saved.get("review_summary_html", ""))
                        self.assertIn("1 change for you", panel)
                        self.assertIn(f">{change.id}</a>", panel)
                        self.assertNotIn(list_page.REVIEW_UNAVAILABLE, panel)
            self.assertEqual(updates.status, 200)
            self.assertIn(b"1 change for you", updates.body)

    def test_list_returns_counts_when_the_linked_change_list_is_malformed(self) -> None:
        case = self.review_fixture(changes=1)
        owner = fixture_member("producer", name="morty").id
        item = the_list.add(owner, "Read the malformed review list", owner=the_list.UnassignedOwner.NATE,
                            source="nate", changes={})
        _ = the_list.link(owner, item.id, ("page-owner/C1",), source="nate")
        _ = case.fixture.review_list.write_text("# Missing review table\n", encoding="utf-8")
        counts = '<aside class="list-counts">2 questions</aside>'
        with (patch.object(self.server, "root", case.root), patch.object(list_page, "counts_strip", return_value=counts),
              patch.object(list_page, "read_linked_reviews", wraps=list_page.read_linked_reviews) as read):
            response = self.request(f"{list_page.PAGE_PATH}?{list_page.COUNTS_AND_LINKS_QUERY}")
            read.assert_called_once()
        self.assertEqual(response.status, 200)
        self.assertIn(counts.encode(), response.body)
        self.assertIn(b"data-list-review-read-error", response.body)
        self.assertNotIn(b"data-list-review-key=", response.body)

    def test_list_serves_linked_items_with_a_producer_directory_document(self) -> None:
        case = self.review_fixture(changes=2)
        _ = conf.path("statusline").write_text(ROLE_COLOR_CONFIG, encoding="utf-8")
        directory = self.root.parent / "brain"
        directory.mkdir()
        producer = fixture_member("producer", name="brain", doc=staff.LiveDoc(local_machine(), directory))
        keeper = fixture_member("producer", name="morty").id
        item = the_list.add(keeper, "Read the linked changes", owner=the_list.UnassignedOwner.NATE,
                            source="nate", changes={"producer": producer.id})
        _ = the_list.link(keeper, item.id, ("page-owner/C1", "page-owner/C2"), source="nate")
        with patch.object(self.server, "root", case.root):
            self.assertEqual(self.request(list_page.PAGE_PATH).status, 200)
            change_routing.delivery_path(case.root, case.fixture.review_list, "C1").mkdir()
            with patch.object(list_page, "read_linked_reviews", wraps=list_page.read_linked_reviews) as read:
                response = self.request(list_page.PAGE_PATH)
                read.assert_not_called()
                self.assertEqual(response.status, 200)
                self.assertIn(b"Read the linked changes", response.body)
                self.assertIn(b"Checking linked reviews", response.body)
                review = self.request(f"{list_page.PAGE_PATH}?{list_page.COUNTS_AND_LINKS_QUERY}")
                read.assert_called_once()
        self.assertEqual(review.status, 200)
        self.assertIn(b">page-owner/C2</a>", review.body)

    def test_older_console_browser_and_rebuild_links_forward_to_the_fixed_address(self) -> None:
        for path, location in (
            ("/abc123/console.html?page=questions", "/console/?page=questions"),
            ("/abc123/console.html", "/console/"),
            ("/abc123/changes.html?type=questions", "/console/changes.html?type=questions"),
            ("/abc123/rebuilds.html", "/console/rebuilds.html"),
        ):
            with self.subTest(path=path):
                status, headers, _ = self.head(path)
                self.assertEqual((status, headers["location"]), (303, location))
        row = self.root / "def456"
        row.mkdir()
        _ = (row / change_page.MANIFEST).write_text("{}", encoding="utf-8")
        with self.assertRaises(FileNotFoundError):
            _ = serve.old_console_link(self.root, "/def456/changes.html")
        for path in ("/abc123/notes.html", "/ABC123/console.html", "/abc123/sub/console.html"):
            with self.subTest(path=path):
                with self.assertRaises(FileNotFoundError):
                    _ = serve.old_console_link(self.root, path)

    def test_snapshot_update_keeps_a_delivery_park_with_a_ready_change(self) -> None:
        self.server.review_cache.close()
        case = self.review_fixture(changes=1)
        keeper = fixture_member("producer", name="morty").id
        cache = serve.ConsoleReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        before = cache.get()
        change = next(item for item in before.records.values() if item.type == review_items.ReviewType.CHANGE)
        item = the_list.add(keeper, "Finish the remaining work", owner=change.owner, source="morty",
                            changes={"state": the_list.ItemState.WORKING})
        _ = the_list.link(keeper, item.id, (change.key,), source="morty")
        parked = the_list.park(keeper, item.id, the_list.ParkUntilDelivered.DELIVERED, source="nate")

        cache.invalidate()
        cache.start()
        cache.synchronize()

        after = cache.get()
        self.assertIsNot(after, before)
        self.assertNotIn(change.key, after.unavailable_cards)
        self.assertTrue(after.records[change.key].for_you(the_list.now()))
        saved = the_list.load(keeper, item.id)
        self.assertIs(saved.state, the_list.ItemState.WORKING)
        self.assertIs(saved.parked_until, the_list.ParkUntilDelivered.DELIVERED)
        self.assertEqual(saved, parked)

    def test_delivery_park_retries_after_list_ask_and_busy_review_locks(self) -> None:
        self.server.review_cache.close()
        case = self.review_fixture(changes=1)
        change = next(item for item in review_items.read(case.root) if item.type == review_items.ReviewType.CHANGE)
        keeper = fixture_member("producer", name="morty").id
        item = the_list.add(keeper, "Keep the park after Ask", owner=change.owner, source="morty",
                            changes={"state": the_list.ItemState.WORKING})
        _ = the_list.link(keeper, item.id, (change.key,), source="morty")
        parked = the_list.park(keeper, item.id, the_list.ParkUntilDelivered.DELIVERED, source="nate")
        list_locked, finish_ask = threading.Event(), threading.Event()
        ask = page_question.ask

        def claim(store: page_question.QuestionStore, target: change_page.QuestionDeliveryTarget, words: str,
                  status: change_page.DeliveryStatus) -> change_page.MessageDelivery:
            list_locked.set()
            if not finish_ask.wait(TEST_HANG_GUARD_SECS):
                raise TimeoutError("The test did not finish Ask.")
            return ask(store, target, words, status)

        with (patch.object(page_question, "ask", side_effect=claim),
              patch.object(change_page, "recover_pending"), patch.object(list_page, "recover_pending"),
              patch.object(change_page, "send_question", return_value=question_outcome()),
              ExitStack() as running):
            pool = running.enter_context(ThreadPoolExecutor(max_workers=1))
            status = change_page.DeliveryStatus(case.root)
            question = the_list.PageRequest("ask", "question", "Which check passed?")
            asked = pool.submit(the_list.ask_question, keeper, item.id, question, status)
            try:
                self.assertTrue(list_locked.wait(TEST_HANG_GUARD_SECS))
                server = running.enter_context(
                    serve.ReviewLinksServer(("127.0.0.1", 0), case.root, initialize_review=True))
                snapshot = server.review_cache.get()
                self.assertIn(change.key, snapshot.records)
                self.assertEqual(the_list.load(keeper, item.id), parked)
                with change_review.review_delivery_lock(case.root, wait=False) as lock:
                    self.assertIs(lock, file_lock.LockResult.ACQUIRED)
                self.assertFalse(asked.done(), "Ask must still be paused.")
            finally:
                finish_ask.set()
            change_page.deliver(asked.result(timeout=TEST_HANG_GUARD_SECS), status)
            pending = the_list.load(keeper, item.id)
            self.assertEqual([saved.words for saved in pending.questions], [question.words])
            self.assertIs(pending.parked_until, the_list.ParkUntilDelivered.DELIVERED)

            with change_review.review_delivery_lock(case.root):
                server.review_cache.invalidate()
                server.review_cache.synchronize()
                self.assertEqual(the_list.load(keeper, item.id), pending)

            directory = os.open(case.fixture.review_list.parent, os.O_RDONLY)
            try:
                fcntl.flock(directory, fcntl.LOCK_EX)
                server.review_cache.invalidate()
                server.review_cache.synchronize()
                self.assertEqual(the_list.load(keeper, item.id), pending)
            finally:
                os.close(directory)

            server.review_cache.invalidate()
            server.review_cache.synchronize()
            self.assertEqual(the_list.load(keeper, item.id), pending)
            delivered = the_list.update(keeper, item.id, {"state": the_list.ItemState.DELIVERED}, source="morty")
            self.assertIs(delivered.parked_until, the_list.NoTimestamp.UNSET)
            self.assertEqual(delivered.log[-1].what, the_list.UnparkedForDelivery(the_list.ItemDelivered.RECORDED))
            self.assertEqual(delivered.questions, pending.questions)
            server.review_cache.invalidate()
            server.review_cache.synchronize()
            self.assertEqual(the_list.load(keeper, item.id), delivered)

    def test_list_feedback_saves_before_delivery_and_recovers_at_server_start(self) -> None:
        morty, item = self.list_keeper_item("The feedback item")
        sent = threading.Event()
        completed = threading.Event()
        real_deliver = list_page.deliver
        def complete_delivery(message: list_page.ListFeedbackDelivery | change_page.NoMessageDelivery) -> None:
            real_deliver(message)
            completed.set()
        def send(_recipient: staff.StaffId, _sender: str, _message: str, **_options: str) -> None:
            sent.set()
        with (patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((morty,))),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(list_page, "deliver", side_effect=complete_delivery),
              patch.object(change_page, "send_to_staff", side_effect=send) as delivery):
            request = list_page.ListFormRequestPrefix(uuid4().hex, the_list.unread_update(item)).format()
            fields = {"keeper": morty.id, "id": item.id, "request": request,
                    "token": list_page.request_token(self.root, morty.id, item.id, request),
                    "action": "read", "words": "Explain this simply.", "until": "2026-12-03"}
            for action in ("read", "park", "tell", "remove"):
                fields["action"] = action
                with self.subTest(action=action):
                    before = the_list.load(morty.id, item.id)
                    self.assertEqual(self.post(list_page.PAGE_PATH, urlencode({**fields, "token": "bad"}))[0], 400)
                    self.assertEqual(the_list.load(morty.id, item.id), before)
                    sent.clear()
                    completed.clear()
                    code, body = self.post(list_page.PAGE_PATH, urlencode(fields))
                    self.assertEqual(code, 200)
                    self.assertTrue(completed.wait(TEST_HANG_GUARD_SECS))
                    self.assertEqual(json.loads(body)["state"], "read" if action == "read" else "sent")
                    response = cast(dict[str, str], json.loads(body))
                    self.assertNotIn("request", response)
                    self.assertNotIn("token", response)
                    fields["request"] = f"{request}.{uuid4().hex}"
                    if action == "remove":
                        self.assertIs(cast(dict[str, object], json.loads(body)).get("detail_closed"), True)
                    else:
                        self.assertIn("detail_revision", response)
                        self.assertIn("detail_html", response)
                    if action == "read":
                        delivery.assert_not_called()
                        self.assertEqual(response["unread"], "false")
                    else:
                        self.assertTrue(sent.wait(TEST_HANG_GUARD_SECS))
                        expected = ("nate's words for item 1" if action == "tell" else
                                "nate removed item 1." if action == "remove" else
                                "nate parked item 1 until 2026-12-03.")
                        self.assertIn(expected, cast(str, delivery.call_args.args[2]))
            self.assertEqual(the_list.load(morty.id, item.id).state, the_list.ItemState.CLOSED)
            fields["action"] = "tell"
            fields["request"] = uuid4().hex
            fields["token"] = list_page.request_token(self.root, morty.id, item.id, fields["request"])
            _ = list_page.submit(self.root, urlencode(fields))
            sent.clear()
            recovered = threading.Event()
            recover = list_page.recover_pending
            def recover_and_finish() -> None:
                try:
                    recover()
                finally:
                    recovered.set()
            with patch.object(list_page, "recover_pending", side_effect=recover_and_finish):
                restarted = serve.ReviewLinksServer(("127.0.0.1", 0), self.root)
                try:
                    self.assertTrue(recovered.wait(TEST_HANG_GUARD_SECS))
                    self.assertTrue(sent.is_set())
                finally:
                    restarted.server_close()

    def test_list_unpark_retry_after_expiry_preserves_new_parking(self) -> None:
        start = datetime(2026, 11, 18, tzinfo=timezone.utc)
        expiry = start + timedelta(hours=1)
        new_time = expiry + timedelta(hours=1)
        with patch.object(the_list, "now", return_value=start) as clock:
            morty, item = self.list_keeper_item("Unpark this item")
            parked = the_list.park(morty.id, item.id, expiry, source="morty")
            request = uuid4().hex
            def unpark_body(request_id: str) -> str:
                return urlencode({"keeper": morty.id, "id": item.id, "request": request_id,
                                  "token": list_page.request_token(self.root, morty.id, item.id, request_id),
                                  "action": "unpark", "words": "", "until": ""})

            body = unpark_body(request)
            clock.return_value = expiry
            delivered = threading.Event()

            def finish_delivery(_submission: list_page.ListActionSubmission) -> None:
                delivered.set()

            with (patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((morty,))),
                  patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
                  patch.object(list_page, "deliver", side_effect=finish_delivery) as deliver):
                code, response = self.post(list_page.PAGE_PATH, body)
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(response)["parked"], "false")
                self.assertEqual(json.loads(response)["unpark_outcome"], "expired")
                self.assertTrue(delivered.wait(TEST_HANG_GUARD_SECS))
                saved = the_list.load(morty.id, item.id)
                self.assertEqual(saved.parked_until, expiry)
                self.assertEqual(saved.log[:-1], parked.log)
                self.assertEqual(saved.log[-1].what,
                                 the_list.UnparkNoChangeReceipt(
                                     the_list.PageRequest(request, "unpark", "List page: nate unparked item 1."),
                                     the_list.UnparkNoChangeReason.EXPIRED))
                reparked = the_list.park(morty.id, item.id, new_time, source="morty")
                delivered.clear()
                code, response = self.post(list_page.PAGE_PATH, body)
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(response)["parked"], "true")
                self.assertEqual(json.loads(response)["unpark_outcome"], "expired")
                self.assertTrue(delivered.wait(TEST_HANG_GUARD_SECS))
                self.assertEqual(the_list.load(morty.id, item.id), reparked)
                expired = list_page.ListUnparkSubmission(list_page.UnparkOutcome.EXPIRED,
                                                         change_page.NoMessageDelivery())
                self.assertEqual([call.args for call in deliver.call_args_list],
                                 [(expired,), (expired,)])
                fresh = uuid4().hex
                delivered.clear()
                code, response = self.post(list_page.PAGE_PATH, unpark_body(fresh))
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(response)["parked"], "false")
                self.assertEqual(json.loads(response)["unpark_outcome"], "cleared")
                self.assertTrue(delivered.wait(TEST_HANG_GUARD_SECS))
                deliver.assert_called_with(list_page.ListUnparkSubmission(list_page.UnparkOutcome.CLEARED,
                    list_page.ListFeedbackDelivery(morty.id, item.id, fresh)))
                delivered.clear()
                code, response = self.post(list_page.PAGE_PATH, unpark_body(uuid4().hex))
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(response)["parked"], "false")
                self.assertEqual(json.loads(response)["unpark_outcome"], "no_active_parking")
                self.assertTrue(delivered.wait(TEST_HANG_GUARD_SECS))
                deliver.assert_called_with(list_page.ListUnparkSubmission(list_page.UnparkOutcome.NO_ACTIVE_PARKING,
                    change_page.NoMessageDelivery()))
                newer = the_list.park(morty.id, item.id, new_time + timedelta(hours=1), source="morty")
                delivered.clear()
                code, response = self.post(list_page.PAGE_PATH, unpark_body(fresh))
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(response)["parked"], "true")
                self.assertEqual(json.loads(response)["unpark_outcome"], "cleared_newer_parking_active")
                self.assertTrue(delivered.wait(TEST_HANG_GUARD_SECS))
                self.assertEqual(the_list.load(morty.id, item.id), newer)

                park_request = uuid4().hex
                delayed = new_time + timedelta(hours=2)
                _ = the_list.park(morty.id, item.id, delayed, source=the_list.PAGE_SOURCE,
                    notice=the_list.PageItemNotice(park_request, "Park from the List.", "park"))
                for checked, saved in ((park_request, True), (fresh, False), (uuid4().hex, False)):
                    with self.subTest(park_request=checked):
                        query = urlencode({"item-state": item.id, "keeper": morty.id, "park-request": checked})
                        reply = self.request(list_page.PAGE_PATH + "?" + query)
                        self.assertEqual(reply.status, 200)
                        payload = cast(list_page.SavedActionState, json.loads(reply.body))
                        self.assertEqual(payload["parked"], "true")
                        self.assertIs(payload.get("park_request_saved"), saved)

    def test_list_remove_reports_saved_when_directory_sync_fails_after_replace(self) -> None:
        morty, item = self.list_keeper_item("Remove this item")
        request = uuid4().hex
        fields = {"keeper": morty.id, "id": item.id, "request": request,
                  "token": list_page.request_token(self.root, morty.id, item.id, request),
                  "action": "remove", "words": "", "until": ""}
        folder = the_list.folder(morty.id)
        sync_folder = atomic_file.sync_folder
        delivered = threading.Event()

        def fail_list_sync(path: Path) -> None:
            if path == folder:
                raise OSError("Fixture directory sync failed after replacement.")
            sync_folder(path)

        def finish_delivery(_delivery: list_page.ListFeedbackDelivery | change_page.NoMessageDelivery) -> None:
            delivered.set()

        with (patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((morty,))),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(atomic_file, "sync_folder", side_effect=fail_list_sync),
              patch.object(list_page, "deliver", side_effect=finish_delivery) as deliver):
            code, body = self.post(list_page.PAGE_PATH, urlencode(fields))
            saved = the_list.load(morty.id, item.id)
            self.assertEqual(saved.state, the_list.ItemState.CLOSED)
            self.assertEqual(the_list.feedback_requests(saved)[request].state, "pending")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["item_state"], "Closed")
            self.assertTrue(delivered.wait(TEST_HANG_GUARD_SECS))
            deliver.assert_called_once_with(list_page.ListFeedbackDelivery(morty.id, item.id, request))

    def test_undeletable_expired_snapshot_does_not_prevent_bind_or_other_links(self) -> None:
        expired = self.root / "aaaaaa"
        expired.mkdir()
        os.utime(expired, (0, 0))
        other = self.root / "bbbbbb"
        other.mkdir()
        _ = (other / "draft.txt").write_text("still served", encoding="utf-8")
        errors = StringIO()
        with patch.object(shutil, "rmtree", side_effect=PermissionError("cannot delete snapshot")), redirect_stderr(errors):
            with started_review_server(("127.0.0.1", 0), self.root) as server:
                # handle_request serves one disposable connection without a background service.
                thread = threading.Thread(target=server.handle_request)
                thread.start()
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=TEST_HANG_GUARD_SECS)
                try:
                    connection.request("GET", "/bbbbbb/draft.txt")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"still served")
                finally:
                    connection.close()
                    thread.join(TEST_HANG_GUARD_SECS)
        self.assertTrue(expired.exists())
        self.assertIn("cannot delete snapshot", errors.getvalue())
        self.assertIn("aaaaaa", errors.getvalue())

    def test_images_keep_their_image_content_type_and_binary_bytes(self) -> None:
        image = b"\x89PNG\r\n\x1a\n\x00\xff"
        _ = (self.root / "diagram.png").write_bytes(image)
        reply = self.request("/diagram.png")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.content_type, "image/png")
        self.assertEqual(reply.body, image)

    def test_an_image_question_answers_the_browser_before_delivery_finishes(self) -> None:
        _ = self.enterContext(patch.object(serve, 'tailscale_ip', return_value='127.0.0.1'))
        page = change_page.ReviewPage(self.root / 'fixture-image', self.root, ('Images fixture/Q1',), '')
        delivery = change_page.MessageDelivery(page, page.rows[0], (), 'Fixture question', 'fixture-attempt')
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def send(message: change_page.MessageDelivery | change_page.NoMessageDelivery,
                 status: change_page.DeliveryStatus) -> None:
            self.assertIs(message, delivery)
            self.assertEqual(status.root, self.root)
            started.set()
            try:
                self.assertTrue(release.wait(TEST_HANG_GUARD_SECS))
            finally:
                finished.set()

        with patch.object(images, 'submit', return_value=delivery) as submit, patch.object(
                change_page, 'deliver', side_effect=send):
            try:
                code, body = self.post(images.PAGE_PATH, 'fixture-question')
                self.assertEqual((code, json.loads(body)), (200, {'state': 'done'}))
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(finished.is_set())
                submit.assert_called_once()
                self.assertEqual(submit.call_args.args[:2], (self.root, 'fixture-question'))
            finally:
                release.set()
                if started.is_set():
                    self.assertTrue(finished.wait(TEST_HANG_GUARD_SECS))

    def test_list_ask_saves_before_delivery_and_retries_the_same_question(self) -> None:
        morty, item = self.list_keeper_item("The question item")
        words = "Which check passed?"
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        claiming, resume_claim = threading.Event(), threading.Event()
        claim_question = change_page.claim_question

        def claim(status: change_page.DeliveryStatus,
                  delivery: change_page.VerifiedQuestionAttempt) -> change_page.PageQuestionSend:
            claiming.set()
            if not resume_claim.wait(TEST_HANG_GUARD_SECS):
                raise TimeoutError("The test did not resume the send claim.")
            return claim_question(status, delivery)
        deliveries: list[tuple[staff.StaffId, str]] = []
        saved_at_send: list[the_list.ListItem] = []

        def send(recipient: staff.StaffId, message: str, _message_id: str
                 ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            saved_at_send.append(the_list.load(morty.id, item.id))
            deliveries.append((recipient, message))
            started.set()
            if len(deliveries) == 1:
                self.assertTrue(release.wait(TEST_HANG_GUARD_SECS))
            return question_outcome()

        real_deliver = list_page.deliver

        def deliver(message: list_page.ListActionSubmission) -> None:
            try:
                real_deliver(message)
            finally:
                if isinstance(message, list_page.ListQuestionDelivery) and message.status.root == self.root:
                    finished.set()

        request = uuid4().hex
        fields = {"keeper": morty.id, "id": item.id, "request": request, "words": words, "until": "",
                  "token": list_page.request_token(self.root, morty.id, item.id, request), "action": "question"}
        with (patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((morty,))),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(change_page, "send_question", side_effect=send),
              patch.object(list_page, "deliver", side_effect=deliver)):
            try:
                with (self.subTest("List GET during the send claim"),
                      ThreadPoolExecutor(max_workers=1) as pool,
                      patch.object(change_page, "claim_question", side_effect=claim)):
                    ask = pool.submit(self.post, list_page.PAGE_PATH, urlencode(fields))
                    try:
                        self.assertTrue(claiming.wait(TEST_HANG_GUARD_SECS))
                        with patch.object(change_page, "_question_lock_state") as probe:
                            reply = self.request(list_page.PAGE_PATH)
                            self.assertEqual(reply.status, 200)
                            self.assertIn(words.encode(), reply.body)
                            probe.assert_not_called()
                    finally:
                        resume_claim.set()
                    code, body = ask.result(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)["state"], "saved")
                self.assertEqual(json.loads(body)["waiting_for_answer"], "true")
                self.assertEqual(json.loads(body)["open_question"], "true")
                result = cast(list_page.SavedActionState, json.loads(body))
                self.assertIn(words, result.get("detail_html", ""))
                reply_delivery = cast(dict[str, object], json.loads(body))
                self.assertIs(reply_delivery["delivery_pending"], True)
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(finished.is_set())
                path = list_page.PAGE_PATH + "?" + urlencode({"delivery": reply_delivery["delivery"]})
                response, headers, _ = self.head(path)
                self.assertEqual(response, 200)
                self.assertEqual(headers.get("x-item-delivery-pending"), "true")
                opened = saved_at_send[0]
                self.assertEqual([(question.question_id, question.words) for question in opened.questions], [("Q1", words)])
                self.assertEqual(len(deliveries), 1)
                self.assertEqual(deliveries[0][0], morty.id)
                self.assertIn("List item 1: Ask question\nThe question item\n" + words, deliveries[0][1])
                self.assertIn('the_list.py answer L1 --question Q1 --text "<answer>"', deliveries[0][1])
            finally:
                release.set()
                if started.is_set():
                    self.assertTrue(finished.wait(TEST_HANG_GUARD_SECS))
            finished.clear()
            self.assertEqual(self.post(list_page.PAGE_PATH, urlencode(fields))[0], 200)
            self.assertTrue(finished.wait(TEST_HANG_GUARD_SECS))
            self.assertEqual(deliveries[0][0], morty.id)
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(the_list.load(morty.id, item.id), opened)
            for different in ({"action": "tell"}, {"words": "What else changed?"}):
                self.assertEqual(self.post(list_page.PAGE_PATH, urlencode({**fields, **different}))[0], 400)
            finished.clear()
            self.assertEqual(self.post(list_page.PAGE_PATH, urlencode(
                {**fields, "request": f"{request}.{uuid4().hex}"}))[0], 200)
            self.assertTrue(finished.wait(TEST_HANG_GUARD_SECS))
            self.assertEqual(len(deliveries), 1)
            with (patch.object(staff, "caller_identity", return_value=morty.current_session),
                  redirect_stdout(StringIO())):
                self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The gate passed."]), 0)
            self.assertEqual(self.post(list_page.PAGE_PATH, urlencode(fields))[0], 200)
            saved = the_list.load(morty.id, item.id)
            self.assertEqual([question.question_id for question in saved.questions], ["Q1"])
            self.assertEqual(len(deliveries), 1)
            new_request = uuid4().hex
            finished.clear()
            self.assertEqual(self.post(list_page.PAGE_PATH, urlencode({**fields, "request": new_request,
                "token": list_page.request_token(self.root, morty.id, item.id, new_request), "words": "What else changed?"}))[0], 200)
            self.assertTrue(finished.wait(TEST_HANG_GUARD_SECS))
            self.assertEqual(len(deliveries), 2)
            self.assertEqual([question.question_id for question in the_list.load(morty.id, item.id).questions], ["Q1", "Q2"])
            closed = the_list.close(morty.id, item.id, "This report is retired.", source="nate")
            code, body = self.post(list_page.PAGE_PATH, urlencode(fields))
            self.assertEqual((code, json.loads(body)["state"]), (200, "saved"))
            self.assertEqual(self.post(list_page.PAGE_PATH, urlencode(
                {**fields, "request": f"{request}.{uuid4().hex}"}))[0], 400)
            current = self.request(list_page.PAGE_PATH + "?" + urlencode({"item-state": item.id, "keeper": morty.id}))
            self.assertEqual(current.status, 200)
            self.assertEqual(json.loads(current.body)["item_state"], "Closed")
            self.assertEqual(the_list.load(morty.id, item.id), closed)
            self.assertEqual(len(deliveries), 2)

            with self.subTest(recovery="live retry takes the claim"):
                item = the_list.add(morty.id, "The retry item", owner=the_list.UnassignedOwner.NATE,
                                    source="nate", changes={})
                recovery_root = self.root.parent / "live-retry-recovery"
                deliveries.clear()
                started.clear()
                release.clear()
                request = uuid4().hex
                body = urlencode({**fields, "id": item.id, "request": request, "words": "Is the gate ready?",
                    "token": list_page.request_token(recovery_root, morty.id, item.id, request)})
                status = change_page.DeliveryStatus(recovery_root)
                pending = list_page.submit(recovery_root, body, status)
                assert isinstance(pending, list_page.ListQuestionDelivery)
                original = pending.delivery
                assert isinstance(original, change_page.PageQuestionSend)
                assert isinstance(original.job, change_page.QuestionJob)
                original.job.release()
                attempt_read, resume_recovery = threading.Event(), threading.Event()
                recover_question = change_page.recover_page_question

                def recover_delivery(status: change_page.DeliveryStatus, path: Path,
                                     record: delivery_state.QuestionAttempt) -> None:
                    self.assertEqual(record.attempt_id, original.attempt)
                    attempt_read.set()
                    self.assertTrue(resume_recovery.wait(TEST_HANG_GUARD_SECS))
                    recover_question(status, path, record)

                def retry() -> None:
                    delivery = list_page.submit(recovery_root, body, status)
                    assert isinstance(delivery, list_page.ListQuestionDelivery)
                    real_deliver(delivery)

                with (patch.object(change_page, "recover_page_question", side_effect=recover_delivery),
                      ThreadPoolExecutor(max_workers=2) as executor):
                    recovery = executor.submit(change_page.recover_review_messages, status)
                    try:
                        self.assertTrue(attempt_read.wait(TEST_HANG_GUARD_SECS))
                        first = executor.submit(retry)
                        self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                        resume_recovery.set()
                        recovery.result(TEST_HANG_GUARD_SECS)
                        retry()
                        self.assertEqual(len(deliveries), 1)
                    finally:
                        resume_recovery.set()
                        release.set()
                    first.result(TEST_HANG_GUARD_SECS)

            with self.subTest(recovery="result lock fails"):
                item = the_list.add(morty.id, "The lock failure item", owner=the_list.UnassignedOwner.NATE,
                                    source="nate", changes={})
                recovery_root = self.root.parent / "lock-failure-recovery"
                previous = change_page.DeliveryStatus(recovery_root)
                pending_question = the_list.ask_question(morty.id, item.id,
                    the_list.PageRequest(uuid4().hex, "question", "Did the build finish?"), previous)
                assert isinstance(pending_question, change_page.PageQuestionSend)
                assert isinstance(pending_question.job, change_page.QuestionJob)
                pending_question.job.release()
                status = change_page.DeliveryStatus(recovery_root)
                deliveries.clear()
                release.set()
                lock = status.locked
                accepted: set[tuple[staff.StaffId, str]] = set()

                def send_with_receipt(recipient: staff.StaffId, message: str, message_id: str
                                      ) -> tuple[send_result.ConfirmedSendOutcome, str]:
                    key = recipient, message_id
                    if key in accepted:
                        return question_outcome("skipped", "The transport has a receipt.")
                    result = send(recipient, message, message_id)
                    accepted.add(key)
                    return result

                @contextmanager
                def lock_before_send() -> Generator[None]:
                    if deliveries:
                        raise OSError("The result lock is unavailable.")
                    with lock():
                        yield

                path = status.path(pending_question.page, pending_question.row)
                with patch.object(change_page, "send_question", side_effect=send_with_receipt) as transport:
                    with (patch.object(status, "locked", side_effect=lock_before_send),
                          redirect_stderr(StringIO()) as errors):
                        change_page.recover_review_messages(status)
                    interrupted = change_page.read_question_attempt(path)
                    self.assertEqual(interrupted.state, delivery_state.QuestionSendState.SENDING)
                    self.assertFalse(status.question_jobs)
                    change_page.recover_review_messages(change_page.DeliveryStatus(recovery_root))
                self.assertIn("The result lock is unavailable.", errors.getvalue())
                self.assertEqual(transport.call_args_list, [
                    call(morty.id, interrupted.message, interrupted.message_id),
                    call(morty.id, interrupted.message, interrupted.message_id),
                ])
                self.assertEqual(len(deliveries), 1)
                self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)
                self.assertEqual(status.request_attempts, {})

    def test_list_question_claim_failure_and_deferred_recovery(self) -> None:
        keeper = fixture_member("producer", name="morty").id
        item = the_list.add(keeper, "Check the queue", owner=the_list.UnassignedOwner.NATE, source="nate", changes={})
        status = change_page.DeliveryStatus(self.root)
        words = "Which check passed?"
        request = the_list.PageRequest(uuid4().hex, "question", words)
        with (patch.object(process_start, "process_start",
                           return_value=process_start.Unreadable("fixture read error")),
              redirect_stderr(StringIO()) as errors):
            refused = the_list.ask_question(keeper, item.id, request, status)
        assert isinstance(refused, change_page.PageQuestionSend)
        path = status.path(refused.page, refused.row)
        failed = change_page.read_question_attempt(path)
        self.assertEqual(failed.state, delivery_state.QuestionSendState.FAILED)
        self.assertIsInstance(failed.owner, delivery_state.NoQuestionSendOwner)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)

        # A new Ask can try the same saved question after a confirmed refusal.
        request = the_list.PageRequest(uuid4().hex, "question", words)
        pending = the_list.ask_question(keeper, item.id, request, status)
        assert isinstance(pending, change_page.PageQuestionSend)
        assert isinstance(pending.job, change_page.QuestionJob)
        restarted = change_page.DeliveryStatus(self.root)
        with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
            try:
                change_page.recover_review_messages(restarted)
                send.assert_not_called()
                self.assertTrue(restarted.has_deferred_questions())
            finally:
                pending.job.release()
            change_page.recover_deferred_questions(restarted)
            change_page.recover_deferred_questions(restarted)
        send.assert_called_once()
        self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)
        self.assertFalse(restarted.has_deferred_questions())

    def test_list_question_recovers_when_the_failure_cannot_be_recorded(self) -> None:
        keeper = fixture_member("producer", name="morty").id
        for fault in ("read", "write"):
            with self.subTest(fault=fault):
                item = the_list.add(keeper, "Check the queue", owner=the_list.UnassignedOwner.NATE,
                                    source="nate", changes={})
                status = change_page.DeliveryStatus(self.root)
                request = the_list.PageRequest(uuid4().hex, "question", "Which check passed?")
                delivery = the_list.ask_question(keeper, item.id, request, status)
                assert isinstance(delivery, change_page.PageQuestionSend)
                path = status.path(delivery.page, delivery.row)
                attempt = change_page.read_question_attempt(path)
                failure = OSError("Cannot access the delivery record.")
                writer = change_page.publish_question_attempt

                def write_attempt(path: Path, record: delivery_state.QuestionAttempt) -> None:
                    if record.state is delivery_state.QuestionSendState.FAILED:
                        raise failure
                    writer(path, record)

                fault_patch = (patch.object(change_page, "read_question_attempt", side_effect=failure)
                               if fault == "read" else patch.object(change_page, "publish_question_attempt",
                                                                    side_effect=write_attempt))
                with (fault_patch, redirect_stderr(StringIO()) as errors,
                      patch.object(change_page, "send_question", return_value=question_outcome("refused")) as send):
                    change_page.deliver(delivery, status)
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn(str(failure), errors.getvalue())
                self.assertTrue(status.has_deferred_questions())
                self.assertFalse(status.question_jobs)
                if fault == "read":
                    send.assert_not_called()

                with patch.object(change_page, "send_question", return_value=question_outcome()) as recover:
                    change_page.recover_deferred_questions(status)
                    change_page.recover_deferred_questions(status)
                recover.assert_called_once_with(keeper, attempt.message, attempt.message_id)
                self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)

    def test_list_question_shows_a_failed_saved_message_mismatch(self) -> None:
        keeper = fixture_member("producer", name="morty").id
        item = the_list.add(keeper, "Check the queue", owner=the_list.UnassignedOwner.NATE, source="nate", changes={})
        status = change_page.DeliveryStatus(self.root)
        request = the_list.PageRequest(uuid4().hex, "question", "Which check passed?")
        pending = the_list.ask_question(keeper, item.id, request, status)
        assert isinstance(pending, change_page.PageQuestionSend)
        assert isinstance(pending.job, change_page.QuestionJob)
        pending.job.release()
        path = status.path(pending.page, pending.row)
        attempt = change_page.read_question_attempt(path)
        with status.locked():
            change_page.advance_question_attempt(path, attempt, replace(attempt, message="Different message"))
        with (patch.object(change_page, "send_question") as send, redirect_stderr(StringIO()) as errors):
            change_page.recover_review_messages(status)
        send.assert_not_called()
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        saved = the_list.load(keeper, item.id)
        view = the_list.question_visibility(keeper, saved, status)
        self.assertEqual(view, the_list.QuestionAttention(frozenset({"Q1"})))

    def test_list_ask_preserves_unicode_separators(self) -> None:
        morty, item = self.list_keeper_item("The question item")
        words = "First check?\u2028Second check?\u2029Third check?"
        request = uuid4().hex
        fields = {"keeper": morty.id, "id": item.id, "request": request, "words": words, "until": "",
                  "token": list_page.request_token(self.root, morty.id, item.id, request), "action": "question"}
        finished = threading.Event()
        real_deliver = change_page.deliver

        def deliver(message: change_page.MessageDelivery | change_page.NoMessageDelivery,
                    status: change_page.DeliveryStatus) -> None:
            try:
                real_deliver(message, status)
            finally:
                finished.set()

        with (patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((morty,))),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(change_page, "deliver", side_effect=deliver),
              patch.object(change_page, "send_question", return_value=question_outcome()) as send):
            for _ in range(2):
                finished.clear()
                self.assertEqual(self.post(list_page.PAGE_PATH, urlencode(fields))[0], 200)
                self.assertTrue(finished.wait(TEST_HANG_GUARD_SECS))
            send.assert_called_once()
        self.assertEqual([(question.question_id, question.words) for question in the_list.load(morty.id, item.id).questions],
                         [("Q1", words)])

    def test_exhausted_list_question_reports_failure_after_automatic_retries(self) -> None:
        morty, item = self.list_keeper_item("The question item")
        request, words = uuid4().hex, "Which check passed?"
        fields = {"keeper": morty.id, "id": item.id, "request": request, "words": words, "until": "",
                  "token": list_page.request_token(self.root, morty.id, item.id, request), "action": "question"}
        finished = threading.Event()
        real_deliver = change_page.deliver
        messages: list[str] = []
        tries = conf.read("review_links").whole("delivery_tries")

        def deliver(message: change_page.MessageDelivery | change_page.NoMessageDelivery,
                    status: change_page.DeliveryStatus) -> None:
            try:
                if isinstance(message, change_page.MessageDelivery):
                    messages.append(message.message)
                real_deliver(message, status)
            finally:
                finished.set()

        with (patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((morty,))),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(list_page, "counts_strip", return_value=""),
              patch.object(change_page, "deliver", side_effect=deliver),
              patch.object(change_page, "alert_delivery_failures"),
              patch.object(change_page, "send_question", side_effect=[
                  *([question_outcome("refused", "Producer offline")] * tries)]) as send):
            code, response = self.post(list_page.PAGE_PATH, urlencode(fields))
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(response)["waiting_for_answer"], "true")
            self.assertEqual(json.loads(response)["open_question"], "true")
            self.assertTrue(finished.wait(TEST_HANG_GUARD_SECS))
            saved = the_list.load(morty.id, item.id)
            self.assertEqual([(question.question_id, question.words) for question in saved.questions], [("Q1", words)])
            opened = self.request(list_page.PAGE_PATH)
            self.assertEqual(opened.status, 200)
            markup = opened.body.decode().split('<script>', 1)[0]
            self.assertRegex(markup, r'<tbody data-list-item[^>]*data-waiting-for-answer="false"[^>]*">')
            self.assertIn('data-open-question="true"', markup)
            self.assertIn("Question not delivered.", markup)
            self.assertNotIn('value="resend-', markup)
            self.assertNotIn("ready to send", markup)
            code, _ = self.post(list_page.PAGE_PATH, urlencode({**fields, "action": "resend-Q1"}))
            self.assertEqual(code, 400)
            self.assertEqual(the_list.load(morty.id, item.id), saved)
            self.assertEqual(send.call_count, tries)
            self.assertEqual(len(messages), 1)
            self.assertIn(f"\n{words}\n", messages[0])
            self.assertIn("answer L1 --question Q1", messages[0])

    def test_an_image_question_delivers_when_the_browser_response_fails(self) -> None:
        _ = self.enterContext(patch.object(serve, 'tailscale_ip', return_value='127.0.0.1'))
        page = change_page.ReviewPage(self.root / 'fixture-image', self.root, ('Images fixture/Q1',), '')
        delivery = change_page.MessageDelivery(page, page.rows[0], (), 'Fixture question', 'fixture-attempt')
        completed = threading.Event()

        def send(message: change_page.MessageDelivery | change_page.NoMessageDelivery,
                 status: change_page.DeliveryStatus) -> None:
            self.assertIs(message, delivery)
            self.assertEqual(status.root, self.root)
            completed.set()

        with patch.object(images, 'submit', return_value=delivery), patch.object(
                change_page, 'deliver', side_effect=send), patch.object(
                serve.ReviewLinksHandler, 'json_response', side_effect=BrokenPipeError('fixture disconnect')):
            with self.assertRaises(OSError):
                _ = self.post(images.PAGE_PATH, 'fixture-question')
            self.assertTrue(completed.wait(TEST_HANG_GUARD_SECS))

    def test_quota_clear_changes_the_nav_until_an_escalation_or_reset(self) -> None:
        from scripts.accounts import accounts

        reset = datetime.now(timezone.utc) + timedelta(days=1)
        quotas = {"claude1": accounts.WeeklyQuota(9, reset), "codex1": accounts.WeeklyQuota(4, reset)}
        path = "/console/for-you.json?view=console"
        state = accounts_page.quota_state(self.root)

        def clear(label: str, level: str, at: datetime | None) -> int:
            form = urlencode({"quota_account": label, "quota_level": level,
                              "quota_reset": at.isoformat() if at is not None else ""})
            return self.post(accounts_page.PAGE_PATH, form)[0]

        def nav() -> dict[str, str]:
            reply = self.request(path)
            self.assertEqual(reply.status, 200)
            return cast(dict[str, dict[str, str]], json.loads(reply.body))["accounts_quota"]

        with (patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(accounts, "stored_weekly_quotas", return_value=quotas),
              patch.object(console, "for_you_payload", return_value={"available": True, "count": 0, "entries": []})):
            self.assertEqual(nav(), {"level": "urgent", "name": "Urgent: 4% left"})
            for body in ("quota_account=codex1&quota_level=urgent",
                         "quota_account=codex1&quota_account=claude1&quota_level=urgent&quota_reset=",
                         "quota_account=codex1&quota_level=invalid&quota_reset=",
                         "quota_account=codex1&quota_level=urgent&quota_reset=invalid"):
                with self.subTest(body=body):
                    self.assertEqual(self.post(accounts_page.PAGE_PATH, body)[0], 400)
                    self.assertFalse(state.exists())
            self.assertEqual(clear("codex1", "urgent", reset), 200)
            self.assertEqual(nav(), {"level": "warning", "name": "Warning: 9% left"})
            self.assertEqual(clear("claude1", "warning", reset), 200)
            self.assertEqual(nav(), {"level": "none", "name": ""})
            self.assertEqual(json.loads(state.read_text()), {
                "claude1": {"level": "warning", "reset": reset.isoformat()},
                "codex1": {"level": "urgent", "reset": reset.isoformat()},
            })
            quotas["codex1"] = accounts.WeeklyQuota(7, reset)
            self.assertEqual(nav(), {"level": "none", "name": ""})
            quotas["claude1"] = accounts.WeeklyQuota(5, reset)
            self.assertEqual(nav(), {"level": "urgent", "name": "Urgent: 5% left"})
            self.assertEqual(clear("claude1", "urgent", reset), 200)
            quotas["codex1"] = accounts.WeeklyQuota(7, reset + timedelta(days=7))
            self.assertEqual(nav(), {"level": "warning", "name": "Warning: 7% left"})

            next_reset = reset + timedelta(days=7)
            self.assertEqual(clear("codex1", "warning", next_reset), 200)
            for observed_reset in (accounts.StatusUnavailable(), next_reset):
                with self.subTest(reset_read=isinstance(observed_reset, datetime)):
                    quotas["codex1"] = accounts.WeeklyQuota(4, observed_reset)
                    if not isinstance(observed_reset, datetime):
                        self.assertEqual(nav(), {"level": "urgent", "name": "Urgent: 4% left"})
                        self.assertEqual(clear("codex1", "urgent", None), 200)
                        self.assertEqual(json.loads(state.read_text())["codex1"]["reset"], next_reset.isoformat())
                    self.assertEqual(nav(), {"level": "none", "name": ""})

            for contents in (b"{", b"[]", b'{"codex1":{"level":"urgent","reset":"invalid"}}'):
                with self.subTest(contents=contents):
                    _ = state.write_bytes(contents)
                    reply = self.request(path)
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(json.loads(reply.body), {
                        "available": True, "count": 0, "entries": [],
                        "accounts_quota": {"level": "none", "name": ""},
                    })
                    form = urlencode({"quota_account": "codex1", "quota_level": "urgent",
                                      "quota_reset": next_reset.isoformat()})
                    code, body = self.post(accounts_page.PAGE_PATH, form)
                    self.assertEqual(code, 503)
                    self.assertEqual(json.loads(body), {"reason": f"Invalid quota clear state in {state}."})
                    self.assertEqual(state.read_bytes(), contents)

    def test_quota_clear_accepts_a_submitted_reset_from_the_same_week(self) -> None:
        from scripts.accounts import accounts

        reset = datetime.now(timezone.utc) + timedelta(days=1)
        state = accounts_page.quota_state(self.root)
        quotas = {"claude1": accounts.WeeklyQuota(4, reset)}
        cases = ((reset - timedelta(days=7), 400),
                 (reset + timedelta(microseconds=526917), 200))
        with (patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(accounts, "stored_weekly_quotas", return_value=quotas)):
            for submitted, status in cases:
                with self.subTest(submitted=submitted):
                    form = urlencode({"quota_account": "claude1", "quota_level": "urgent",
                                      "quota_reset": submitted.isoformat()})
                    self.assertEqual(self.post(accounts_page.PAGE_PATH, form)[0], status)
                    if status == 400:
                        self.assertFalse(state.exists())
                    else:
                        self.assertEqual(json.loads(state.read_text()), {
                            "claude1": {"level": "urgent", "reset": reset.isoformat()},
                        })

    def test_warning_clear_keeps_an_urgent_record_for_the_same_week(self) -> None:
        from scripts.accounts import accounts

        reset = datetime.now(timezone.utc) + timedelta(days=1)
        quotas = {"claude1": accounts.WeeklyQuota(9, reset)}
        state = accounts_page.quota_state(self.root)
        state.parent.mkdir(parents=True, exist_ok=True)
        receipt = {"claude1": {"level": "urgent", "reset": (reset - timedelta(microseconds=526917)).isoformat()}}
        _ = state.write_text(json.dumps(receipt), encoding="utf-8")
        form = urlencode({"quota_account": "claude1", "quota_level": "warning", "quota_reset": reset.isoformat()})
        with (patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(accounts, "stored_weekly_quotas", return_value=quotas)):
            self.assertEqual(self.post(accounts_page.PAGE_PATH, form)[0], 200)
        self.assertEqual(json.loads(state.read_text()), receipt)

    def test_accounts_post_saves_and_clears_a_producers_next_move(self) -> None:
        from scripts.accounts import accounts, next_moves

        entry = next_moves.ClaudeGroupMove("fixture", "producers", 1, 0, "claude2", datetime.now(timezone.utc))
        with (patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(accounts, "claude_account_dir", return_value=self.root / "claude2"),
              patch.object(next_moves, "console_next_move_percent", return_value=1.0),
              patch.object(next_moves, "set_first_next_move", return_value=entry) as save,
              patch.object(next_moves, "read_next_moves", return_value=(entry,)),
              patch.object(next_moves, "remove_next_move") as clear):
            status, content = self.post(accounts_page.PAGE_PATH, "group=producers&to=claude2")
            self.assertEqual((status, json.loads(content)), (200, {"state": "saved"}))
            save.assert_called_once_with("producers", 1.0, "claude2")
            status, content = self.post(accounts_page.PAGE_PATH, "group=producers&clear=first")
            self.assertEqual((status, json.loads(content)), (200, {"state": "saved"}))
            clear.assert_called_once_with("producers", next_moves.FIRST_GROUP_POSITION)

    def test_accounts_color_rejects_an_unpublished_host_before_tune(self) -> None:
        configuration = self.root.parent / "config"
        configuration.mkdir()
        _ = (configuration / "statusline.conf").write_text(ROLE_COLOR_CONFIG, encoding="utf-8")
        tune = Mock(return_value=subprocess.CompletedProcess((), 0, "", ""))
        body = "key=role.worker&value=%23af875f"
        host = f"other.invalid:{self.port}"
        with (patch.object(self.server, "verified_tailscale_address", self.server.bound_address),
              patch.dict(os.environ, {"CONF_DIR": str(configuration), "REVIEW_LINKS_HOST": ""}),
              patch.object(accounts_page, "subprocess", Mock(wraps=subprocess, run=tune))):
            status, _ = self.post(accounts_page.PAGE_PATH, body, headers={"Host": host, "Origin": f"http://{host}"})
            self.assertEqual(status, 403)
            tune.assert_not_called()
            status, content = self.post(accounts_page.PAGE_PATH, body)
            self.assertEqual((status, json.loads(content)), (200, {"state": "saved"}))
            tune.assert_called_once()

    def test_builds_move_requires_the_owner_origin_token_and_bounded_body(self) -> None:
        from scripts.build_queue import admission_client, admission_protocol as admission, admission_records as records

        configuration = self.root / "queue-move-conf"
        configuration.mkdir()
        _ = write_config_fixture(configuration / "build_queue.conf",
            "move_timeout_seconds=3\nmove_request_bytes=512\nmove_highlight_seconds=2\n")
        _ = write_config_fixture(configuration / "review_links.conf")
        origin = f"http://127.0.0.1:{self.port}"
        request = admission.QueueMoveRequest("1" * 32, "2" * 32, "3" * 32, admission.QueueDrop("4" * 32, "before"))
        line = records.QueueMoveLine("5" * 32, (records.QueueMoveMember(request.row_id),))
        content = cast(dict[str, object], json.loads(admission.encode_record(request)))
        del content["code_version"]
        undo = admission.QueueUndoRequest("7" * 32, request.row_id, request.revision, request.request_id)
        undo_content = cast(dict[str, object], json.loads(admission.encode_record(undo)))
        del undo_content["code_version"]
        headers = {"Content-Type": "application/json"}
        remote_node: dict[str, object] = {}
        remote_user = {"ID": 8}
        self_node: dict[str, object] = {"UserID": 7}

        def tailscale(args: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            if args == ["tailscale", "status", "--json"]:
                output = {"Self": self_node}
            else:
                self.assertEqual(args[:-1], ["tailscale", "whois", "--json", "--proto=tcp"])
                self.assertRegex(args[-1], r"^127\.0\.0\.1:[1-9][0-9]*$")
                output = {"Node": remote_node, "UserProfile": remote_user}
            return subprocess.CompletedProcess(args, 0, json.dumps(output), "")

        with (patch.dict(os.environ, {"CONF_DIR": str(configuration)}),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(serve, "subprocess", Mock(wraps=subprocess, run=tailscale)),
              patch.object(admission_client, "move_queue",
                           return_value=admission.QueueMoveConflict(line, "The queue changed.")) as move):
            self.assertEqual(self.request(serve.QUEUE_MOVE_PATH).status, 403)
            remote_user["ID"] = 7
            remote_node["Tags"] = ["tag:fixture"]
            self.assertEqual(self.request(serve.QUEUE_MOVE_PATH).status, 403)
            del remote_node["Tags"]
            self_node["Tags"] = ["tag:fixture"]
            self.assertEqual(self.request(serve.QUEUE_MOVE_PATH).status, 403)
            del self_node["Tags"]
            token = self.request(serve.QUEUE_MOVE_PATH)
            self.assertEqual(token.status, 200)
            content["token"] = undo_content["token"] = self.server.new_item_token
            body = json.dumps(content)
            for sent, sent_origin, status in (
                (body, "", 403),
                (body, "http://other.invalid", 403),
                (json.dumps({**content, "token": "wrong"}), origin, 403),
                (json.dumps({**content, "destination": {"kind": "QueueDrop",
                    "neighbor": "not-a-row-id", "side": "before"}}), origin, 400),
                (json.dumps({**content, "undo_destination": content["destination"]}), origin, 400),
                (json.dumps({**undo_content, "destination": content["destination"]}), origin, 400),
                (body + " " * 512, origin, 400),
            ):
                with self.subTest(status=status, origin=sent_origin):
                    actual, _ = self.post(serve.QUEUE_MOVE_PATH, sent, origin=sent_origin, headers=headers)
                    self.assertEqual(actual, status)
            move.assert_not_called()
            status, reply = self.post(serve.QUEUE_MOVE_PATH, body, origin=origin, headers=headers)
            self.assertEqual(status, 409)
            self.assertIn(b'"reason": "The queue changed."', reply)
            move.assert_called_once_with(request, 3)
            move.reset_mock()
            status, _ = self.post(serve.QUEUE_MOVE_PATH, json.dumps(undo_content), origin=origin, headers=headers)
            self.assertEqual(status, 409)
            move.assert_called_once_with(undo, 3)
    def test_build_kill_requires_origin_operator_token_and_bounded_body(self) -> None:
        settings = self.root.parent / "kill-config"
        settings.mkdir()
        _ = write_config_fixture(settings / "review_links.conf",
                                 "page_refresh_seconds=17\n# whole: 1024..65536\nkill.request_bytes=4096")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(settings)})))
        origin = f"http://127.0.0.1:{self.port}"
        token = self.server.new_item_token
        reference, handle = "a" * 64, "b" * 64
        form = {"token": token, "reference": reference, "handle": handle, "words": "Use the next slot."}
        result: kill_action.KillResult = {"reference": reference, "step": "check", "pid": 17,
            "stop": kill_action.StopState.STOPPED, "delivery": kill_action.DeliveryState.QUEUED,
            "time": "19:41 EST", "recipient_name": "fixture", "recipient_role": "worker"}
        with (patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(serve, "builds_operator", return_value=False) as operator,
              patch.object(self.server.kill_actions, "options", return_value=({}, [])) as options,
              patch.object(self.server.kill_actions, "stop", return_value=result) as stop):
            self.assertEqual(self.request(kill_action.PATH).status, 403)
            options.assert_not_called()
            self.assertEqual(self.post(kill_action.PATH, urlencode(form), origin=origin)[0], 403)
            operator.return_value = True
            response = self.request(kill_action.PATH)
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.body), {"token": token, "offers": {}, "results": [],
                                                          "refresh_seconds": 17})
            options.assert_called_once_with(17)
            for sent_origin, values, expected in (
                ("", form, 403),
                ("http://wrong.invalid", form, 403),
                (origin, {**form, "token": "wrong"}, 403),
                (origin, {**form, "words": "x" * 4097}, 400),
            ):
                with self.subTest(origin=sent_origin, status=expected):
                    self.assertEqual(self.post(kill_action.PATH, urlencode(values), origin=sent_origin)[0], expected)
            stop.assert_not_called()
            status, body = self.post(kill_action.PATH, urlencode(form), origin=origin)
            self.assertEqual((status, json.loads(body)), (200, {"result": result}))
            stop.assert_called_once_with(reference, handle, form["words"])

    def test_builds_operator_checks_the_peer_against_the_local_owner(self) -> None:
        settings = self.root.parent / "operator-config"
        settings.mkdir()
        _ = write_config_fixture(settings / "review_links.conf", "operator_login=local-node-owner")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(settings)})))
        peer = ("100.64.0.17", 43210)
        whois = ["tailscale", "whois", "--json", "--proto=tcp", "100.64.0.17:43210"]
        status = ["tailscale", "status", "--json"]
        for user, tags, local_tags, allowed in ((52, [], [], True), (53, [], [], False),
                                               (52, ["tag:shared"], [], False),
                                               (52, [], ["tag:shared"], False)):
            with self.subTest(user=user, tags=tags, local_tags=local_tags):
                remote = subprocess.CompletedProcess(whois, 0, json.dumps(
                    {"Node": {"Tags": tags}, "UserProfile": {"ID": user, "LoginName": "fixture"}}))
                local = subprocess.CompletedProcess(status, 0, json.dumps(
                    {"Self": {"UserID": 52, "Tags": local_tags}}))
                with patch.object(subprocess, "run", side_effect=(remote, local)) as run:
                    self.assertIs(serve.builds_operator(peer), allowed)
                self.assertEqual(run.call_args_list[0].args[0], whois)
                self.assertEqual(run.call_count, 1 if tags else 2)

    def test_console_tune_requires_the_origin_and_token(self) -> None:
        key = "faster_box_max_cost_ratio"
        setting = console_tune.TunableSetting("console_tune_test", "AWS Machines",
            Decimal("1.00"), Decimal("3.00"), Decimal("0.05"))
        origin = f"http://127.0.0.1:{self.port}"
        form = {"page": "aws_machines", "key": key, "value": "1.50"}
        with (patch.dict(console_tune.SETTINGS, {("aws_machines", key): setting}, clear=True),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(console_tune, "run") as publish):
            token_reply = self.request(console_tune.PAGE_PATH)
            self.assertEqual(token_reply.status, 200)
            token = cast(dict[str, str], json.loads(token_reply.body))["token"]
            self.assertEqual(token, self.server.new_item_token)
            for sent_origin, fields in (
                ("", {**form, "token": token}),
                ("http://wrong.invalid", {**form, "token": token}),
                (origin, form),
                (origin, {**form, "token": "wrong"}),
            ):
                with self.subTest(origin=sent_origin, includes_token="token" in fields):
                    status, _ = self.post(console_tune.PAGE_PATH, urlencode(fields), origin=sent_origin)
                    self.assertEqual(status, 403)
            publish.assert_not_called()

    def test_console_tune_reports_live_value_after_publish_failure_and_refusal(self) -> None:
        key = "faster_box_max_cost_ratio"
        document = "console_tune_test"
        config = conf.path(document)
        self.addCleanup(config.unlink, missing_ok=True)
        _ = config.write_text(f"{key}=1.00\n", encoding="utf-8")
        setting = console_tune.TunableSetting(document, "AWS Machines", Decimal("1.00"), Decimal("3.00"), Decimal("0.05"))
        origin = f"http://127.0.0.1:{self.port}"
        token = self.server.new_item_token

        def fail_after_change(*_args: object, **_options: object) -> subprocess.CompletedProcess[str]:
            _ = config.write_text(f"{key}=1.50\n", encoding="utf-8")
            return subprocess.CompletedProcess(["tune"], 2, stderr="two lines\nThe setting stays live.\n")

        with (patch.dict(console_tune.SETTINGS, {("aws_machines", key): setting}, clear=True),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(console_tune, "run", side_effect=fail_after_change) as publish):
            for page, field, value, reason, live in (
                ("aws", key, "1.50", "This page cannot change that setting.", None),
                ("aws_machines", "other", "1.50", "This page cannot change that setting.", None),
                ("aws_machines", key, "3.05", "Enter a value from 1.00 to 3.00.", "1.00"),
                ("aws_machines", key, "1.52", "Use steps of 0.05 from 1.00.", "1.00"),
                ("aws_machines", key, "NaN", "Enter a decimal number.", "1.00"),
            ):
                with self.subTest(page=page, field=field, value=value):
                    form = urlencode({"page": page, "key": field, "value": value, "token": token})
                    status, body = self.post(console_tune.PAGE_PATH, form, origin=origin)
                    self.assertEqual((status, json.loads(body)), (400, {"reason": reason, "live_value": live}))
            publish.assert_not_called()
            form = urlencode({"page": "aws_machines", "key": key, "value": "1.50", "token": token})
            status, body = self.post(console_tune.PAGE_PATH, form, origin=origin)
            self.assertEqual((status, json.loads(body)), (503, {"reason": "The setting stays live.", "live_value": "1.50"}))
            publish.assert_called_once_with((*accounts_page.TUNE, document, key, "1.50",
                "--why", "The user confirmed the change with Go on the AWS Machines page.",
                "--user-words", f"{key} set to 1.50 with Go on the AWS Machines page"),
                capture_output=True, text=True, check=False)
            publish.side_effect = None
            publish.return_value = subprocess.CompletedProcess(["tune"], 0)
            status, body = self.post(console_tune.PAGE_PATH, form, origin=origin)
            self.assertEqual((status, json.loads(body)), (200, {"state": "saved", "live_value": "1.50"}))

    def test_accounts_route_renders_per_request_and_reports_a_failed_read(self) -> None:
        with (patch.object(accounts_page, "render_with_facts", side_effect=OSError("staff file absent")),
              redirect_stderr(StringIO()) as error):
            reply = self.request(accounts_page.PAGE_PATH)
        self.assertEqual(reply.status, 503)
        self.assertIn(b"The Accounts page is not available.", reply.body)
        self.assertNotIn(b"staff file absent", reply.body)
        self.assertIn("review-links accounts: staff file absent", error.getvalue())
        with patch.object(accounts_page, "render_with_facts", return_value=accounts_page.AccountsRender(b"<p>board</p>", 0.5, 100)) as render:
            reply = self.request(accounts_page.PAGE_PATH)
        self.assertEqual((reply.status, reply.content_type, reply.body), (200, "text/html; charset=utf-8", b"<p>board</p>"))
        render.assert_called_once_with()
        with patch.object(serve, "tailscale_ip", return_value="127.0.0.1"):
            for result, expected, body in (
                (accounts_page.Saved(), 200, {"state": "saved"}),
                (accounts_page.Refused("Unknown key."), 400, {"reason": "Unknown key."}),
                (accounts_page.SaveFailed("tune: refused"), 503, {"reason": "tune: refused"}),
            ):
                with self.subTest(result=result), patch.object(accounts_page, "save_color", return_value=result) as save:
                    status, content = self.post(accounts_page.PAGE_PATH, "key=role.worker&value=%23af875f")
                    self.assertEqual((status, json.loads(content)), (expected, body))
                    save.assert_called_once_with("role.worker", "#af875f")
                with self.subTest(move=result), patch.object(accounts_page, "save_next_move", return_value=result) as move:
                    status, content = self.post(accounts_page.PAGE_PATH, "group=units&to=claude2")
                    self.assertEqual((status, json.loads(content)), (expected, body))
                    move.assert_called_once_with("units", "claude2")
                with self.subTest(clear=result), patch.object(accounts_page, "clear_next_move", return_value=result) as clear:
                    status, content = self.post(accounts_page.PAGE_PATH, "group=showrunners&clear=first")
                    self.assertEqual((status, json.loads(content)), (expected, body))
                    clear.assert_called_once_with("showrunners")
                with self.subTest(model_color=result), patch.object(accounts_page, "save_color", return_value=result) as save:
                    status, content = self.post(accounts_page.PAGE_PATH, "key=model.claude-opus-5-5&value=%23e08a5c")
                    self.assertEqual((status, json.loads(content)), (expected, body))
                    save.assert_called_once_with("model.claude-opus-5-5", "#e08a5c")
            for form in ("", "key=account.claude1", "label=claude1&value=x", "key=account.claude1&value=x&value=y",
                         "key=account.claude1&value=x&extra=y", "group=units", "group=units&to=claude2&value=x",
                         "key=account.claude1&to=claude2", "group=units&clear=all", "group=units&clear=",
                         "group=units&clear=first&to=claude2", "group=units&clear=first&clear=first"):
                with (self.subTest(form=form), patch.object(accounts_page, "save_color") as save,
                      patch.object(accounts_page, "save_next_move") as move,
                      patch.object(accounts_page, "clear_next_move") as clear):
                    status, _ = self.post(accounts_page.PAGE_PATH, form)
                    self.assertEqual(status, 400)
                    save.assert_not_called()
                    move.assert_not_called()
                    clear.assert_not_called()
        with patch.object(accounts_page, "render_with_facts", return_value=accounts_page.AccountsRender(b"new color", .5, 101)):
            self.assertEqual(self.request(accounts_page.PAGE_PATH).body, b"new color")

    def test_accounts_cache_keeps_the_page_during_one_render_and_after_a_failure(self) -> None:
        cache = serve.AccountsPageCache()
        first = accounts_page.AccountsRender(b'<p data-page-facts>first</p>', 0.25, 100)
        with patch.object(accounts_page, "render_with_facts", return_value=first):
            kept = cache.get()
        for fail in (False, True):
            with self.subTest(failed_render=fail):
                entered, release = threading.Event(), threading.Event()

                def render() -> accounts_page.AccountsRender:
                    entered.set()
                    if not release.wait(TEST_HANG_GUARD_SECS):
                        raise OSError("the test did not release the render")
                    if fail:
                        raise OSError("no stored data")
                    return accounts_page.AccountsRender(b'<p data-page-facts>second</p>', 0.75, 200)

                before = tuple(cache.recent_times)
                with patch.object(accounts_page, "render_with_facts", side_effect=render) as child, redirect_stderr(StringIO()) as errors:
                    try:
                        self.assertEqual(cache.get(), kept)
                        self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                        self.assertEqual(cache.get(), kept)
                        child.assert_called_once_with()
                        self.assertEqual(tuple(cache.recent_times), before)
                    finally:
                        release.set()
                        with cache.condition:
                            self.assertTrue(cache.condition.wait_for(lambda: not cache.rendering, TEST_HANG_GUARD_SECS))
                    if fail:
                        self.assertEqual(cache.page, kept)
                        self.assertEqual(tuple(cache.recent_times), before)
                    else:
                        self.assertIn(b">second</p>", cache.page)
                        self.assertIn(b'data-render-median="0.500000"', cache.page)
                        kept = cache.page
                if fail:
                    self.assertEqual(errors.getvalue(), "review-links accounts: no stored data\n")

    def test_accounts_request_waits_for_a_new_render_when_cached_page_is_old(self) -> None:
        cache = serve.AccountsPageCache()
        refresh = conf.read("review_links").number("page_refresh_seconds")
        with (patch.object(time, "monotonic", return_value=100),
              patch.object(accounts_page, "render_with_facts", return_value=accounts_page.AccountsRender(b"old page", .5, 100))):
            self.assertEqual(cache.get(), b"old page")
        with (patch.object(time, "monotonic", return_value=101 + refresh),
              patch.object(accounts_page, "render_with_facts", return_value=accounts_page.AccountsRender(b"new page", .5, 200)) as render):
            reply = cache.get()
            with cache.condition:
                self.assertTrue(cache.condition.wait_for(lambda: not cache.rendering, TEST_HANG_GUARD_SECS))
            self.assertEqual(reply, b"new page")
            render.assert_called_once_with()

    def test_accounts_request_refreshes_in_background_within_current_configured_period(self) -> None:
        cache = serve.AccountsPageCache()
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory()))
        settings = configuration / "review_links.conf"
        _ = write_config_fixture(settings, "page_refresh_seconds=37\n")
        with (patch.dict(os.environ, {"CONF_DIR": str(configuration)}),
              patch.object(time, "monotonic", return_value=100) as clock,
              patch.object(accounts_page, "render_with_facts") as render):
            render.return_value = accounts_page.AccountsRender(b"first page", .5, 100)
            self.assertEqual(cache.get(), b"first page")
            clock.return_value = 130
            render.return_value = accounts_page.AccountsRender(b"second page", .5, 130)
            self.assertEqual(cache.get(), b"first page")
            with cache.condition:
                self.assertTrue(cache.condition.wait_for(lambda: not cache.rendering, TEST_HANG_GUARD_SECS))
            self.assertEqual(cache.page, b"second page")
            self.assertEqual(render.call_count, 2)
            _ = write_config_fixture(settings, "page_refresh_seconds=15\n")
            clock.return_value = 160
            render.return_value = accounts_page.AccountsRender(b"third page", .5, 160)
            reply = cache.get()
            with cache.condition:
                self.assertTrue(cache.condition.wait_for(lambda: not cache.rendering, TEST_HANG_GUARD_SECS))
            self.assertEqual(reply, b"third page")
            self.assertEqual(render.call_count, 3)

    def test_old_accounts_requests_share_the_running_background_render(self) -> None:
        entered, release, waiters_entered = threading.Event(), threading.Event(), threading.Event()
        waiters: set[str] = set()

        class WaiterCondition(threading.Condition):
            @override
            def __enter__(self) -> bool:
                acquired = super().__enter__()
                name = threading.current_thread().name
                if name.startswith("old-accounts-request"):
                    waiters.add(name)
                    if len(waiters) == 2:
                        waiters_entered.set()
                return acquired

        cache = serve.AccountsPageCache(condition=WaiterCondition())
        refresh = conf.read("review_links").number("page_refresh_seconds")
        replies: list[bytes] = []

        def render_page() -> accounts_page.AccountsRender:
            entered.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise OSError("the test did not release the render")
            return accounts_page.AccountsRender(b"new page", .5, 200)

        def request() -> None:
            replies.append(cache.get())

        requests = [threading.Thread(target=request, name=f"old-accounts-request-{index}") for index in range(2)]
        with (patch.object(time, "monotonic", return_value=100) as clock,
              patch.object(accounts_page, "render_with_facts") as render):
            render.return_value = accounts_page.AccountsRender(b"old page", .5, 100)
            self.assertEqual(cache.get(), b"old page")
            render.reset_mock()
            render.side_effect = render_page
            try:
                self.assertEqual(cache.get(), b"old page")
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                clock.return_value = 101 + refresh
                for reader in requests:
                    reader.start()
                self.assertTrue(waiters_entered.wait(TEST_HANG_GUARD_SECS))
            finally:
                release.set()
                for reader in requests:
                    if reader.ident is not None:
                        reader.join(TEST_HANG_GUARD_SECS)
                        self.assertFalse(reader.is_alive())
                with cache.condition:
                    self.assertTrue(cache.condition.wait_for(lambda: not cache.rendering, TEST_HANG_GUARD_SECS))
            self.assertEqual(replies, [b"new page", b"new page"])
            render.assert_called_once_with()

    def test_old_accounts_request_keeps_last_good_page_when_the_new_render_fails(self) -> None:
        cache = serve.AccountsPageCache()
        refresh = conf.read("review_links").number("page_refresh_seconds")
        first = accounts_page.AccountsRender(b'<p data-page-facts>board</p>', .25, 100)
        with (patch.object(time, "monotonic", return_value=100),
              patch.object(accounts_page, "render_with_facts", return_value=first)):
            good = cache.get()
        with (patch.object(time, "monotonic", return_value=101 + refresh),
              patch.object(accounts_page, "render_with_facts", side_effect=OSError("fixture render failure")),
              redirect_stderr(StringIO())):
            failed = cache.get()
            with cache.condition:
                self.assertTrue(cache.condition.wait_for(lambda: not cache.rendering, TEST_HANG_GUARD_SECS))
        self.assertEqual(failed, good.replace(b'data-page-facts', b'data-page-facts data-read-error="The Accounts render failed."'))
        self.assertEqual(cache.page, good)
        self.assertEqual(tuple(cache.recent_times), (.25,))

    def test_accounts_requests_share_a_new_render_after_the_store_changes_during_a_render(self) -> None:
        entered, release, requests_entered = threading.Event(), threading.Event(), threading.Event()
        readers: set[str] = set()

        class ReaderCondition(threading.Condition):
            @override
            def __enter__(self) -> bool:
                acquired = super().__enter__()
                name = threading.current_thread().name
                if name.startswith("accounts-new-usage-"):
                    readers.add(name)
                    if len(readers) == 2:
                        requests_entered.set()
                return acquired

        cache = serve.AccountsPageCache(condition=ReaderCondition())
        store = usage_store.store_file()
        _ = store.write_bytes(b"11%")
        refresh = conf.read("review_links").number("page_refresh_seconds")
        replies: list[bytes] = []

        def render_page() -> accounts_page.AccountsRender:
            usage = store.read_bytes()
            if usage == b"11%":
                entered.set()
                if not release.wait(TEST_HANG_GUARD_SECS):
                    raise OSError("the test did not release the render")
            return accounts_page.AccountsRender(usage, .5, 200)

        def request() -> None:
            replies.append(cache.get())

        requests = [threading.Thread(target=request, name=f"accounts-new-usage-{index}") for index in range(2)]
        with (patch.object(time, "monotonic", return_value=100) as clock,
              patch.object(accounts_page, "render_with_facts") as render):
            render.return_value = accounts_page.AccountsRender(b"11%", .5, 100)
            self.assertEqual(cache.get(), b"11%")
            render.side_effect = render_page
            try:
                self.assertEqual(cache.get(), b"11%")
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                updated = store.with_suffix(".new")
                _ = updated.write_bytes(b"10%")
                _ = updated.replace(store)
                clock.return_value = 101 + refresh
                for reader in requests:
                    reader.start()
                self.assertTrue(requests_entered.wait(TEST_HANG_GUARD_SECS))
                self.assertEqual(render.call_count, 2)
            finally:
                release.set()
                for reader in requests:
                    if reader.ident is not None:
                        reader.join(TEST_HANG_GUARD_SECS)
                        self.assertFalse(reader.is_alive())
                with cache.condition:
                    self.assertTrue(cache.condition.wait_for(lambda: not cache.rendering, TEST_HANG_GUARD_SECS))
            self.assertEqual(replies, [b"10%", b"10%"])
            self.assertEqual(render.call_count, 3)

    def test_accounts_request_returns_after_one_new_render_during_repeated_store_updates(self) -> None:
        entered, release, request_waiting = threading.Event(), threading.Event(), threading.Event()

        class ReaderCondition(threading.Condition):
            @override
            def wait(self, timeout: float | None = None) -> bool:
                request_waiting.set()
                return super().wait(timeout)

        cache = serve.AccountsPageCache(condition=ReaderCondition())
        store = usage_store.store_file()
        _ = store.write_bytes(b"1")
        replies: list[bytes] = []

        def render_page() -> accounts_page.AccountsRender:
            usage = store.read_bytes()
            if usage < b"4":
                updated = store.with_suffix(".new")
                _ = updated.write_bytes(str(int(usage) + 1).encode())
                _ = updated.replace(store)
            if usage == b"1":
                entered.set()
                if not release.wait(TEST_HANG_GUARD_SECS):
                    raise OSError("the test did not release the render")
            return accounts_page.AccountsRender(usage, .5, 200)

        def request() -> None:
            replies.append(cache.get())

        with patch.object(accounts_page, "render_with_facts") as render:
            render.return_value = accounts_page.AccountsRender(b"1", .5, 100)
            self.assertEqual(cache.get(), b"1")
            render.side_effect = render_page
            reader = threading.Thread(target=request)
            try:
                self.assertEqual(cache.get(), b"1")
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                reader.start()
                self.assertTrue(request_waiting.wait(TEST_HANG_GUARD_SECS))
            finally:
                release.set()
                if reader.ident is not None:
                    reader.join(TEST_HANG_GUARD_SECS)
                    self.assertFalse(reader.is_alive())
                with cache.condition:
                    self.assertTrue(cache.condition.wait_for(lambda: not cache.rendering, TEST_HANG_GUARD_SECS))
            self.assertEqual(replies, [b"2"])
            self.assertEqual(render.call_count, 3)
            self.assertEqual(cache.get(), b"3")
            self.assertEqual(store.read_bytes(), b"4")
            self.assertEqual(render.call_count, 4)

    def test_accounts_route_keeps_last_good_page_when_refresh_setting_is_unreadable(self) -> None:
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory()))
        settings = configuration / "review_links.conf"
        _ = write_config_fixture(settings)
        first = accounts_page.AccountsRender(b'<p data-page-facts>board</p>', .25, 100)
        with (patch.dict(os.environ, {"CONF_DIR": str(configuration)}),
              patch.object(accounts_page, "render_with_facts", return_value=first) as render,
              redirect_stderr(StringIO())):
            good = self.request(accounts_page.PAGE_PATH)
            self.assertEqual(good.status, 200)
            settings.unlink()
            failed = self.request(accounts_page.PAGE_PATH)
            self.assertEqual(failed.status, 200)
            self.assertIn(b'data-read-error="The Accounts refresh setting could not be read."', failed.body)
            self.assertIn(b'data-read-at="100.000000"', failed.body)
            self.assertIn(b'>board</p>', failed.body)

            self.server.accounts_cache = serve.AccountsPageCache()
            first_failure = self.request(accounts_page.PAGE_PATH)
            self.assertEqual(first_failure.status, 503)
            render.assert_called_once_with()

    def test_accounts_cached_http_response_carries_a_render_failure_then_recovers(self) -> None:
        cache = serve.AccountsPageCache()
        first = accounts_page.AccountsRender(b'<p data-page-facts>board</p>', .25, 100)
        refresh = conf.read("review_links").number("page_refresh_seconds")
        with (patch.object(time, "monotonic", return_value=100),
              patch.object(accounts_page, "render_with_facts", return_value=first)):
            good = cache.get()
        with (patch.object(time, "monotonic", return_value=101 + refresh),
              patch.object(accounts_page, "render_with_facts", side_effect=OSError("private failure")),
              redirect_stderr(StringIO())):
            failed = cache.get()
        self.assertIn(b'data-read-error="The Accounts render failed."', failed)
        self.assertIn(b'data-read-at="100.000000"', failed)
        self.assertIn(b'>board</p>', failed)
        self.assertEqual(cache.page, good)
        self.assertEqual(tuple(cache.recent_times), (.25,))
        with patch.object(serve.AccountsPageCache, "get", return_value=failed):
            response = self.request(accounts_page.PAGE_PATH)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, failed)
        with (patch.object(time, "monotonic", return_value=101 + refresh),
              patch.object(accounts_page, "render_with_facts", return_value=first)):
            recovered = cache.get()
        self.assertNotIn(b'data-read-error', recovered)

    def test_accounts_first_request_waits_for_its_render(self) -> None:
        cache = serve.AccountsPageCache()
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        replies: list[bytes] = []

        def render() -> accounts_page.AccountsRender:
            entered.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise OSError("the test did not release the render")
            return accounts_page.AccountsRender(b"first page", 0.5, 100)

        def request() -> None:
            replies.append(cache.get())
            returned.set()

        with patch.object(accounts_page, "render_with_facts", side_effect=render) as child:
            threads = [threading.Thread(target=request)]
            try:
                for thread in threads:
                    thread.start()
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(returned.is_set())
            finally:
                release.set()
                for thread in threads:
                    thread.join(TEST_HANG_GUARD_SECS)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(replies, [b"first page"])
            child.assert_called_once_with()

    def test_accounts_cache_keeps_only_the_last_ten_successful_render_times(self) -> None:
        cache = serve.AccountsPageCache()
        refresh = conf.read("review_links").number("page_refresh_seconds")
        for seconds in range(1, 13):
            with (patch.object(time, "monotonic", return_value=seconds * (refresh + 1)),
                  patch.object(accounts_page, "render_with_facts",
                               return_value=accounts_page.AccountsRender(b'<p data-page-facts></p>', seconds, seconds + 100))):
                _ = cache.get()
        self.assertEqual(tuple(cache.recent_times), tuple(range(3, 13)))
        self.assertIn(b'data-render-seconds="12.000000"', cache.page)
        self.assertIn(b'data-read-at="112.000000"', cache.page)
        self.assertIn(b'data-render-median="7.500000"', cache.page)
        self.assertIn(b'data-render-count="10"', cache.page)

    def test_accounts_first_render_failure_reaches_every_waiter(self) -> None:
        entered, release, waiters_ready = threading.Event(), threading.Event(), threading.Event()
        waiting: set[str] = set()

        class WaiterCondition(threading.Condition):
            @override
            def __enter__(self) -> bool:
                acquired = super().__enter__()
                name = threading.current_thread().name
                if name.startswith("accounts-waiter-"):
                    waiting.add(name)
                    if len(waiting) == 2:
                        waiters_ready.set()
                return acquired

        cache = serve.AccountsPageCache(condition=WaiterCondition())
        failures: list[str] = []

        def render() -> accounts_page.AccountsRender:
            entered.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise OSError("the test did not release the render")
            raise OSError("the first render timed out")

        def request() -> None:
            try:
                _ = cache.get()
            except OSError as error:
                failures.append(str(error))

        first = threading.Thread(target=request)
        waiters = [threading.Thread(target=request, name=f"accounts-waiter-{index}") for index in range(2)]
        with patch.object(accounts_page, "render_with_facts", side_effect=render) as child:
            try:
                first.start()
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                for waiter in waiters:
                    waiter.start()
                self.assertTrue(waiters_ready.wait(TEST_HANG_GUARD_SECS))
            finally:
                release.set()
                for thread in (first, *waiters):
                    if thread.ident is not None:
                        thread.join(TEST_HANG_GUARD_SECS)
                        self.assertFalse(thread.is_alive())
            self.assertEqual(failures, ["the first render timed out"] * 3)
            child.assert_called_once_with()
        self.assertEqual(tuple(cache.recent_times), ())
        with patch.object(accounts_page, "render_with_facts", return_value=accounts_page.AccountsRender(b"retry page", 0.5, 100)) as retry:
            self.assertEqual(cache.get(), b"retry page")
            retry.assert_called_once_with()

    def test_inaccessible_sweep_entries_do_not_prevent_bind_or_other_links(self) -> None:
        session = self.root / "manual"
        session.mkdir()
        target = self.root.parent / "unsearchable" / "snapshot"
        target.mkdir(parents=True)
        symlink = session / "link"
        symlink.symlink_to(target, target_is_directory=True)
        unreadable = session / "unreadable"
        other = self.root / "bbbbbb"
        other.mkdir()
        _ = (other / "draft.txt").write_text("still served", encoding="utf-8")
        is_dir = Path.is_dir

        for inaccessible in (symlink, unreadable):
            with self.subTest(entry=inaccessible.name):
                if inaccessible == unreadable:
                    unreadable.mkdir()

                def checked_is_dir(path: Path) -> bool:
                    if path == inaccessible:
                        raise PermissionError("cannot inspect entry")
                    return is_dir(path)

                errors = StringIO()
                with patch.object(Path, "is_dir", new=checked_is_dir), redirect_stderr(errors):
                    with started_review_server(("127.0.0.1", 0), self.root) as server:
                        self.port = server.server_port
                        thread = threading.Thread(target=server.handle_request)
                        thread.start()
                        try:
                            reply = self.request("/bbbbbb/draft.txt")
                            self.assertEqual(reply.status, 200)
                            self.assertEqual(reply.body, b"still served")
                        finally:
                            thread.join(TEST_HANG_GUARD_SECS)
                self.assertTrue(symlink.is_symlink())
                self.assertTrue(target.is_dir())
                if inaccessible == unreadable:
                    self.assertIn("cannot inspect entry", errors.getvalue())

    def test_directories_return_404_even_when_an_index_page_exists(self) -> None:
        snapshot = self.root / "session" / "snapshot"
        snapshot.mkdir(parents=True)
        _ = (snapshot / "index.html").write_text("<title>Not a directory endpoint</title>", encoding="utf-8")
        for path in ("/", "/session", "/session/", "/session/snapshot", "/session/snapshot/"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path).status, 404)
        self.assertEqual(self.request("/session/snapshot/index.html").status, 200)

    def test_plain_and_encoded_traversal_cannot_read_a_file_outside_the_root(self) -> None:
        for path in ("/../outside.txt", "/%2e%2e/outside.txt", "/%2e%2e%2foutside.txt"):
            with self.subTest(path=path):
                reply = self.request(path)
                self.assertEqual(reply.status, 404)
                self.assertNotIn(self.outside.read_bytes(), reply.body)

    def test_a_symlink_to_an_outside_file_or_directory_returns_404(self) -> None:
        (self.root / "shortcut.txt").symlink_to(self.outside)
        (self.root / "shortcut").symlink_to(self.outside.parent, target_is_directory=True)
        for path in ("/shortcut.txt", "/shortcut/outside.txt"):
            with self.subTest(path=path):
                reply = self.request(path)
                self.assertEqual(reply.status, 404)
                self.assertNotIn(self.outside.read_bytes(), reply.body)

    def test_url_encoded_filename_with_spaces_and_unicode_is_served(self) -> None:
        payload = b"unchanged snapshot bytes"
        _ = (self.root / "draft café.txt").write_bytes(payload)
        reply = self.request("/draft%20caf%C3%A9.txt")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, payload)

    def test_flat_and_legacy_snapshot_urls_both_keep_serving_their_files(self) -> None:
        for folder in ("k3x9qa", "da928300/20261029T151117462923Z-review-46806d56e97e"):
            with self.subTest(folder=folder):
                snapshot = self.root / folder
                snapshot.mkdir(parents=True)
                _ = (snapshot / "draft.html").write_bytes(b"<title>Review snapshot</title>")
                reply = self.request(f"/{folder}/draft.html")
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.body, b"<title>Review snapshot</title>")

    def test_an_atomic_index_temporary_is_not_served_before_publication(self) -> None:
        private = b"private index source paths and page tokens"
        publish = os.replace
        requested: list[str] = []

        def replace_file(source: Path, destination: Path) -> None:
            requested.append(source.name)
            self.assertEqual(source.read_bytes(), private)
            reply = self.request("/" + source.name)
            self.assertEqual(reply.status, 404)
            self.assertNotIn(private, reply.body)
            publish(source, destination)

        with patch.object(os, "replace", side_effect=replace_file):
            atomic_file.write_bytes_atomic(self.root / ".index.json", private, mode=0o600)
        self.assertEqual(len(requested), 1)
        self.assertEqual((self.root / ".index.json").read_bytes(), private)

    def test_index_and_unfinished_writes_are_not_served(self) -> None:
        for filename in (".index.json", ".review-link-unfinished"):
            _ = (self.root / filename).write_text("private source paths or incomplete bytes", encoding="utf-8")
            self.assertEqual(self.request("/" + filename).status, 404)
            self.assertEqual(self.request("/./" + filename).status, 404)
        (self.root / "index-alias.txt").symlink_to(self.root / ".index.json")
        self.assertEqual(self.request("/index-alias.txt").status, 404)

    def test_private_files_in_any_letter_case_return_404(self) -> None:
        private = "private source paths or incomplete bytes"
        for folder in (self.root, self.root / "session"):
            folder.mkdir(exist_ok=True)
            for filename in (
                ".index.json", ".INDEX.JSON", ".Index.Json",
                ".index.lock", ".INDEX.LOCK", ".Index.Lock",
                ".review-link-x", ".REVIEW-LINK-X", ".Review-Link-x",
                ".review-link-delivery-user-answer.json",
            ):
                with self.subTest(folder=folder.name, filename=filename):
                    _ = (folder / filename).write_text(private, encoding="utf-8")
                    path = (folder / filename).relative_to(self.root).as_posix()
                    for request_path in ("/" + path, "/" + path.replace(".", "%2e", 1)):
                        reply = self.request(request_path)
                        self.assertEqual(reply.status, 404)
                        self.assertNotIn(private.encode("utf-8"), reply.body)
        snapshot = self.root / "abc123"
        snapshot.mkdir()
        _ = (snapshot / "draft.txt").write_bytes(b"review content")
        reply = self.request("/abc123/draft.txt")
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, b"review content")

    def test_private_path_parts_are_blocked_before_symlink_resolution(self) -> None:
        snapshot = self.root / "abc123"
        snapshot.mkdir()
        _ = (snapshot / "draft.txt").write_bytes(b"public snapshot through a private alias")
        for name in (".INDEX.JSON", ".Index.Lock", ".Review-Link-x"):
            (self.root / name).symlink_to(snapshot, target_is_directory=True)
            for prefix in (name, "%2e" + name[1:]):
                with self.subTest(prefix=prefix):
                    self.assertEqual(self.request(f"/{prefix}/draft.txt").status, 404)
        self.assertEqual(self.request("/abc123/draft.txt").status, 200)


class SnapshotSweepTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-sweep-test-")))
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory()))
        settings = configuration / "review_links.conf"
        _ = write_config_fixture(settings, "snapshot_retention_seconds=604800\nsnapshot_sweep_seconds=3600\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))

    def test_change_image_sweep_keeps_named_and_recent_images(self) -> None:
        _ = write_config_fixture(conf.path("review_links"), "change_image_unclaimed_days=10\n")
        now = 1_800_000_000.0
        listing = self.root / "lists"
        listing.mkdir()
        _ = (listing / (change_review.PREFIX + "test.md")).write_text(
            f"## To review\n\n| # | {change_review.IMAGE} |\n| --- | --- |\n| C1 | 111111111111 |\n")
        for identity, age in (("000000000000", 20), ("111111111111", 20), ("222222222222", 2)):
            folder = self.root / "change-images" / identity
            folder.mkdir(parents=True)
            _ = (folder / "image.png").write_bytes(b"fixture image")
            modified = now - age * 86400
            os.utime(folder, (modified, modified))
        with patch("scripts.production.change_review.lists_directory", return_value=listing):
            self.assertEqual(serve.sweep_snapshots(self.root, now), 1)
        self.assertFalse((self.root / "change-images" / "000000000000").exists())
        self.assertTrue((self.root / "change-images" / "111111111111").is_dir())
        self.assertTrue((self.root / "change-images" / "222222222222").is_dir())

    def snapshot(self, session: str, name: str, modified: float) -> Path:
        directory = self.root / session / name
        directory.mkdir(parents=True)
        artifact = directory / "review.html"
        _ = artifact.write_text("<title>Snapshot</title>", encoding="utf-8")
        os.utime(artifact, (modified, modified))
        os.utime(directory, (modified, modified))
        return directory

    def test_sweep_removes_eight_day_snapshots_and_keeps_six_day_snapshots_across_sessions(self) -> None:
        now = 1_800_000_000.0
        six_days_ago = now - 6 * 24 * 60 * 60
        eight_days_ago = now - 8 * 24 * 60 * 60
        kept = self.snapshot("session-a", "recent-review", six_days_ago)
        removed = self.snapshot("session-a", "expired-review", eight_days_ago)
        other_removed = self.snapshot("session-b", "expired-review", eight_days_ago)
        _ = serve.sweep_snapshots(self.root, now)
        self.assertTrue((kept / "review.html").is_file())
        self.assertFalse(removed.exists())
        self.assertFalse(other_removed.exists())

    def test_sweep_keeps_the_fixed_console_folder_at_any_age(self) -> None:
        now = 1_800_000_000.0
        folder = self.root / change_page.CONSOLE_FOLDER
        folder.mkdir()
        _ = (folder / change_page.BROWSER_MANIFEST).write_text("{}", encoding="utf-8")
        os.utime(folder, (now - 30 * 24 * 60 * 60, now - 30 * 24 * 60 * 60))
        self.assertEqual(serve.sweep_snapshots(self.root, now), 0)
        self.assertTrue((folder / change_page.BROWSER_MANIFEST).is_file())

    def test_sweep_tolerates_a_root_that_has_not_been_created(self) -> None:
        missing = self.root / "not-created-yet"
        _ = serve.sweep_snapshots(missing, 1_800_000_000.0)
        self.assertFalse(missing.exists())

    def test_sweep_expires_flat_ids_and_legacy_snapshots_in_the_same_root(self) -> None:
        now = 1_800_000_000.0
        recent = now - 6 * 24 * 60 * 60
        expired = now - 8 * 24 * 60 * 60
        flat_kept = self.snapshot("", "abc123", recent)
        flat_removed = self.snapshot("", "def456", expired)
        legacy_kept = self.snapshot("da928300", "20261029T151117462923Z-review-46806d56e97e", recent)
        legacy_removed = self.snapshot("da928300", "20261020T151117462923Z-review-46806d56e97e", expired)
        manual_kept = self.snapshot("manual", "20261029T151117462923Z-review-46806d56e97e", recent)
        manual_removed = self.snapshot("manual", "20261020T151117462923Z-review-46806d56e97e", expired)
        os.utime(self.root / "manual", (expired, expired))
        self.assertEqual(serve.sweep_snapshots(self.root, now), 3)
        for kept in (flat_kept, legacy_kept, manual_kept):
            self.assertTrue((kept / "review.html").is_file())
        for removed in (flat_removed, legacy_removed, manual_removed):
            self.assertFalse(removed.exists())

    def test_flat_snapshot_id_manual_expires_when_it_contains_files(self) -> None:
        now = 1_800_000_000.0
        folder = self.snapshot("", "manual", now - 8 * 24 * 60 * 60)
        self.assertEqual(serve.sweep_snapshots(self.root, now), 1)
        self.assertFalse(folder.exists())

    def test_expired_manual_session_with_only_a_symlink_keeps_link_and_target(self) -> None:
        now = 1_800_000_000.0
        expired = now - 8 * 24 * 60 * 60
        target = self.root / "outside" / "snapshot"
        target.mkdir(parents=True)
        artifact = target / "keep.txt"
        _ = artifact.write_text("outside the snapshot root", encoding="utf-8")
        os.utime(target, (expired, expired))
        root = self.root / "served"
        manual = root / "manual"
        manual.mkdir(parents=True)
        link = manual / "old-snapshot"
        link.symlink_to(target, target_is_directory=True)
        os.utime(manual, (expired, expired))
        self.assertEqual(serve.sweep_snapshots(root, now), 0)
        self.assertTrue(link.is_symlink())
        self.assertEqual(artifact.read_text(encoding="utf-8"), "outside the snapshot root")

    def test_sweep_ignores_index_and_temporary_files(self) -> None:
        now = 1_800_000_000.0
        expired = now - 8 * 24 * 60 * 60
        for name in (".index.json", ".review-link-unfinished"):
            file = self.root / name
            _ = file.write_text("keep", encoding="utf-8")
            os.utime(file, (expired, expired))
        self.assertEqual(serve.sweep_snapshots(self.root, now), 0)
        self.assertEqual({file.name for file in self.root.iterdir()}, {".index.json", ".review-link-unfinished"})

    def test_sweep_skips_flat_and_legacy_symlinks_and_non_directories(self) -> None:
        now = 1_800_000_000.0
        outside = self.root / "outside"
        outside.mkdir()
        kept_file = outside / "keep.txt"
        _ = kept_file.write_text("outside snapshot storage", encoding="utf-8")
        os.utime(outside, (now - 8 * 24 * 60 * 60, now - 8 * 24 * 60 * 60))
        served = self.root / "served"
        served.mkdir()
        (served / "abc123").symlink_to(outside, target_is_directory=True)
        legacy = served / "da928300"
        legacy.mkdir()
        (legacy / "old-snapshot").symlink_to(outside, target_is_directory=True)
        plain = served / "def456"
        _ = plain.write_text("not a directory", encoding="utf-8")
        _ = serve.sweep_snapshots(served, now)
        self.assertTrue(kept_file.is_file())
        self.assertTrue((served / "abc123").is_symlink())
        self.assertTrue((legacy / "old-snapshot").is_symlink())
        self.assertTrue(plain.is_file())

    def test_server_sweeps_on_startup_and_again_after_an_hour(self) -> None:
        now = 1_800_000_000.0
        expired_at_start = self.snapshot("session", "expired-at-start", now - 8 * 24 * 60 * 60)
        flat_at_start = self.snapshot("", "abc123", now - 8 * 24 * 60 * 60)
        with (patch.object(time, "time", return_value=now),
              patch.object(time, "monotonic", return_value=100.0) as elapsed,
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              started_review_server(("127.0.0.1", 0), self.root) as server):
            self.assertFalse(expired_at_start.exists())
            self.assertFalse(flat_at_start.exists())
            expires_later = self.snapshot("session", "expires-later", now - 8 * 24 * 60 * 60)
            flat_later = self.snapshot("", "def456", now - 8 * 24 * 60 * 60)
            elapsed.return_value = 100.0 + 3599.0
            serve.ReviewLinksServer.service_actions(server)
            self.assertTrue(expires_later.exists())
            self.assertTrue(flat_later.exists())
            elapsed.return_value = 100.0 + 3600.0
            serve.ReviewLinksServer.service_actions(server)
            self.assertFalse(expires_later.exists())
            self.assertFalse(flat_later.exists())

    def test_sweep_does_not_follow_a_session_symlink_outside_the_root(self) -> None:
        outside = self.root / "outside"
        root = self.root / "served"
        root.mkdir()
        expired = outside / "expired-review"
        expired.mkdir(parents=True)
        _ = (expired / "keep.txt").write_text("not a snapshot owned by the service", encoding="utf-8")
        old = 1_800_000_000.0 - 8 * 24 * 60 * 60
        os.utime(expired, (old, old))
        (root / "session-symlink").symlink_to(outside, target_is_directory=True)
        _ = serve.sweep_snapshots(root, 1_800_000_000.0)
        self.assertTrue((expired / "keep.txt").is_file())


class ServingObservationComplete(Exception):
    """The healthy server continued through two scheduled address checks."""


class ServerAddressHealthTests(unittest.TestCase):
    elapsed: float = 100.0
    address_checks: list[float] = []
    next_address: Callable[[], str] = staticmethod(lambda: "127.0.0.1")
    stderr: StringIO = StringIO()

    @override
    def setUp(self) -> None:
        root = self.enterContext(tempfile.TemporaryDirectory(prefix="review-address-test-"))
        folder = Path(root)
        staff_file = folder / "staff.json"
        _ = staff_file.write_text('{"schema":2,"rows":[]}', encoding="utf-8")
        self.elapsed = 100.0
        self.address_checks = []
        self.next_address = lambda: "127.0.0.1"
        self.stderr = self.enterContext(redirect_stderr(StringIO()))
        _ = self.enterContext(redirect_stdout(StringIO()))
        _ = cast(dict[str, str], self.enterContext(patch.dict(os.environ, {
            "REVIEW_LINKS_ROOT": root,
            "STAFF_FILE": str(staff_file),
            "CHANGE_REVIEW_DIR": str(folder / "changes"),
            "SHOWRUNNER_STATE_DIR": str(folder / "state"),
            "NEXT_STATE_DIR": str(folder / "next"),
        })))
        _ = self.enterContext(patch.object(time, "monotonic", side_effect=lambda: self.elapsed))
        _ = self.enterContext(patch.object(serve, "tailscale_ip", side_effect=self.read_address))
        # The restart tests exercise scans of loaded modules.
        _ = self.enterContext(patch.object(serve, "loaded_module_stamps", return_value={}))
        observations = self

        class ClockDrivenServer(serve.ReviewLinksServer):
            @override
            def serve_forever(self, poll_interval: float = 0.5) -> None:
                super().serve_forever(poll_interval=0)

            @override
            def service_actions(self) -> None:
                observations.elapsed += 1
                if observations.elapsed > 221:
                    raise ServingObservationComplete()
                super().service_actions()

        _ = self.enterContext(patch.object(serve, "ReviewLinksServer", ClockDrivenServer))

    def read_address(self) -> str:
        self.address_checks.append(self.elapsed)
        if len(self.address_checks) == 1:
            return "127.0.0.1"
        return self.next_address()

    def test_tailscale_failure_after_startup_leaves_the_serve_loop_and_main_returns_one(self) -> None:
        def unavailable() -> str:
            raise subprocess.CalledProcessError(1, ["tailscale", "ip", "-4"])

        self.next_address = unavailable
        self.assertEqual(serve.main(["--port", "0"]), 1)
        self.assertEqual(self.address_checks, [100.0, 160.0])
        diagnostic = self.stderr.getvalue().splitlines()
        self.assertEqual(len(diagnostic), 1)
        self.assertIn("tailscale", diagnostic[0].lower())
        self.assertIn("gone", diagnostic[0].lower())
        self.assertIn("127.0.0.1", diagnostic[0])

    def test_changed_tailscale_address_leaves_the_serve_loop_and_main_returns_one(self) -> None:
        self.next_address = lambda: "127.0.0.2"
        self.assertEqual(serve.main(["--port", "0"]), 1)
        self.assertEqual(self.address_checks, [100.0, 160.0])
        diagnostic = self.stderr.getvalue().splitlines()
        self.assertEqual(len(diagnostic), 1)
        self.assertIn("tailscale", diagnostic[0].lower())
        self.assertIn("127.0.0.1", diagnostic[0])
        self.assertTrue("changed" in diagnostic[0].lower() or "gone" in diagnostic[0].lower())

    def test_unchanged_tailscale_address_keeps_serving_and_checks_again_after_sixty_seconds(self) -> None:
        with self.assertRaises(ServingObservationComplete):
            _ = serve.main(["--port", "0"])
        self.assertEqual(self.address_checks, [100.0, 160.0, 220.0])
        self.assertEqual(self.stderr.getvalue(), "")


class ServerCodeHealthTests(unittest.TestCase):
    checkout: Path = Path()
    elapsed: float = 100.0
    module_file: Path = Path()

    @override
    def setUp(self) -> None:
        self.checkout = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-code-test-")))
        self.enterContext(patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.checkout / "state")}))
        server_file = self.checkout / "scripts/review_links/serve.py"
        server_file.parent.mkdir(parents=True)
        _ = server_file.write_text("# The server uses this temporary checkout.\n")
        os.utime(server_file, ns=(serve.STARTED_NS - 1_000_000_000, serve.STARTED_NS - 1_000_000_000))
        _ = self.enterContext(patch.object(serve, "__file__", str(server_file)))
        self.elapsed = 100.0
        _ = self.enterContext(patch.object(time, "monotonic", side_effect=lambda: self.elapsed))
        self.module_file = self.checkout / "review_code_fixture.py"
        _ = self.module_file.write_text("VALUE = 'before'\n")
        os.utime(self.module_file, ns=(serve.STARTED_NS - 1_000_000_000, serve.STARTED_NS - 1_000_000_000))
        self.load_module(self.module_file)

    def start_server(self) -> serve.ReviewLinksServer:
        return self.enterContext(started_review_server(("127.0.0.1", 0), self.checkout / "snapshots"))

    def load_module(self, path: Path) -> None:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        _ = cast(object, self.enterContext(patch.dict(sys.modules, {path.stem: module})))
        spec.loader.exec_module(module)

    def test_unchanged_code_keeps_serving_and_reads_settings_only_when_the_check_is_due(self) -> None:
        server = self.start_server()
        server.review_cache.synchronize()
        service_thread = threading.current_thread()
        read_config = conf.read
        read = Mock(wraps=read_config)

        def count_service_reads(source: str | Path, *,
                                load: Callable[[Path], conf.ConfigurationBytes] | None = None) -> conf.ConfigRead:
            if threading.current_thread() is service_thread:
                return cast(conf.ConfigRead, read(source) if load is None else read(source, load=load))
            return read_config(source, load=load)

        with patch.object(conf, "read", count_service_reads):
            self.elapsed = 104.0
            serve.ReviewLinksServer.service_actions(server)
            serve.ReviewLinksServer.service_actions(server)
            read.assert_not_called()
            self.elapsed = 105.0
            serve.ReviewLinksServer.service_actions(server)
            read.assert_called_once_with("review_links")
            self.elapsed = 109.0
            serve.ReviewLinksServer.service_actions(server)
            read.assert_called_once_with("review_links")
            self.elapsed = 110.0
            serve.ReviewLinksServer.service_actions(server)
            self.assertEqual(read.call_count, 2)

    def test_a_module_imported_through_a_file_symlink_keeps_its_target_stamp(self) -> None:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        target = self.checkout / "review_code_target.py"
        _ = target.write_text("VALUE = 'loaded through a link'\n")
        alias = outside / "review_code_alias.py"
        alias.symlink_to(target)
        self.load_module(alias)
        stamps = serve.loaded_module_stamps()
        facts = target.stat()
        self.assertEqual(stamps.get(target), (facts.st_mtime_ns, facts.st_size))
        self.assertNotIn(alias, stamps)

    def test_a_deleted_loaded_module_requires_a_restart(self) -> None:
        server = self.start_server()
        self.module_file.unlink()
        self.elapsed = 105.0
        with self.assertRaises(serve.CodeChanged) as changed:
            serve.ReviewLinksServer.service_actions(server)
        self.assertIn(str(self.module_file), str(changed.exception))

    def test_a_module_loaded_after_start_joins_the_checks_without_a_restart(self) -> None:
        server = self.start_server()
        late_module = self.checkout / "late_review_code_fixture.py"
        _ = late_module.write_text("VALUE = 'first'\n")
        os.utime(late_module, ns=(serve.STARTED_NS - 1_000_000_000, serve.STARTED_NS - 1_000_000_000))
        self.load_module(late_module)
        self.elapsed = 105.0
        serve.ReviewLinksServer.service_actions(server)
        _ = late_module.write_text("VALUE = 'changed later'\n")
        self.elapsed = 110.0
        with self.assertRaises(serve.CodeChanged) as changed:
            serve.ReviewLinksServer.service_actions(server)
        self.assertIn(str(late_module), str(changed.exception))

    def test_a_newly_seen_module_modified_after_process_start_requires_a_restart(self) -> None:
        server = self.start_server()
        late_module = self.checkout / "late_review_code_fixture.py"
        _ = late_module.write_text("VALUE = 'promoted before its first check'\n")
        os.utime(late_module, ns=(serve.STARTED_NS + 1_000_000_000, serve.STARTED_NS + 1_000_000_000))
        self.load_module(late_module)
        self.elapsed = 105.0
        with self.assertRaises(serve.CodeChanged) as changed:
            serve.ReviewLinksServer.service_actions(server)
        self.assertIn(str(late_module), str(changed.exception))

    def test_an_active_verdict_defers_restart_and_keeps_the_recorded_stamps(self) -> None:
        server = self.start_server()
        stamps = dict(server.module_stamps)
        page = change_page.ReviewPage(self.checkout / "changes.md", self.checkout / "production.md", ("C1",), "token")
        status = server.delivery_status
        attempt = status.begin(page, "C1", "verdict")
        _ = self.module_file.write_text("VALUE = 'changed during the verdict'\n")
        os.utime(self.module_file, ns=(serve.STARTED_NS - 1_000_000_000, serve.STARTED_NS - 1_000_000_000))
        self.elapsed = 105.0
        serve.ReviewLinksServer.service_actions(server)
        self.assertEqual(server.module_stamps, stamps)
        status.record(change_page.MessageDelivery(page, "C1", (), "", attempt))
        self.elapsed = 110.0
        with self.assertRaises(serve.CodeChanged) as changed:
            serve.ReviewLinksServer.service_actions(server)
        self.assertIn(str(self.module_file), str(changed.exception))


class ServerCodeRestartTests(unittest.TestCase):
    def test_a_changed_module_restarts_the_same_process_and_answers_a_request_sent_during_the_restart(self) -> None:
        checkout = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-restart-test-")))
        server_file = checkout / "scripts/review_links/serve.py"
        server_file.parent.mkdir(parents=True)
        _ = server_file.write_text("# The server uses this temporary checkout.\n")
        module_file = checkout / "review_restart_fixture.py"
        _ = module_file.write_text("VALUE = 'before'\n")
        _ = write_config_fixture(checkout / "review_links.conf", "code_check_seconds=0.05\n")
        root = checkout / "snapshots"
        root.mkdir()
        _ = (root / "answer.txt").write_text("The server answers after the restart.\n")
        driver = checkout / "driver.py"
        _ = driver.write_text(python_tool_source('''import importlib.util
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from scripts.review_links import serve

checkout = Path(__file__).parent
if serve.LISTEN_FD in os.environ:
    # Hold the restart open, so the test sends a request before the new server starts.
    print(f"restarting {os.getpid()}", file=sys.__stdout__, flush=True)
    time.sleep(0.5)
serve.__file__ = str(checkout / "scripts/review_links/serve.py")
serve.tailscale_ip = lambda: "127.0.0.1"
spec = importlib.util.spec_from_file_location("review_restart_fixture", checkout / "review_restart_fixture.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

class ObservedServer(serve.ReviewLinksServer):
    def __init__(self, address, root, *, initialize_review=False):
        super().__init__(address, root, initialize_review=initialize_review)
        os.environ["REVIEW_TEST_PORT"] = str(self.server_port)
        line = f"{os.getpid()} {module.VALUE} {self.server_port}"
        with (checkout / "starts.txt").open("a") as output:
            output.write(line + "\\n")
        print(line, file=sys.__stdout__, flush=True)

serve.ReviewLinksServer = ObservedServer
with redirect_stdout(sys.stderr):
    raise SystemExit(serve.main(["--port", os.environ.get("REVIEW_TEST_PORT", "0")]))
'''))
        errors_path = checkout / "errors.txt"
        errors = self.enterContext(errors_path.open("w"))
        child: subprocess.Popen[str] = subprocess.Popen(
            [str(Path(__file__).resolve().parents[1] / "lib/py"), "-P", "-B", str(driver)],
            cwd=checkout, env={**os.environ, "CONF_DIR": str(checkout), "REVIEW_LINKS_ROOT": str(root),
                               "SHOWRUNNER_STATE_DIR": str(checkout / "state"),
                               "CLAUDE_PYTHON": sys.executable},
            stdout=subprocess.PIPE, stderr=errors, text=True,
        )

        def stop_child() -> None:
            child.terminate()
            try:
                _ = child.communicate(timeout=TEST_HANG_GUARD_SECS)
            except subprocess.TimeoutExpired:
                child.kill()
                _ = child.communicate(timeout=TEST_HANG_GUARD_SECS)

        self.addCleanup(stop_child)

        def read_start() -> tuple[int, str, int]:
            assert child.stdout is not None
            ready, _, _ = select.select([child.stdout], [], [], TEST_HANG_GUARD_SECS)
            self.assertEqual(ready, [child.stdout], errors_path.read_text())
            line = cast(str, child.stdout.readline())
            self.assertTrue(line, errors_path.read_text())
            pid, value, port = line.split()
            return int(pid), value, int(port)

        first = read_start()
        self.assertEqual(first[0], child.pid)
        self.assertEqual(first[1], "before")
        _ = module_file.write_text("VALUE = 'after_code_change'\n")
        assert child.stdout is not None
        ready, _, _ = select.select([child.stdout], [], [], TEST_HANG_GUARD_SECS)
        self.assertEqual(ready, [child.stdout], errors_path.read_text())
        self.assertEqual(cast(str, child.stdout.readline()).split(), ["restarting", str(first[0])])
        during = HTTPConnection("127.0.0.1", first[2], timeout=TEST_HANG_GUARD_SECS)
        try:
            during.request("GET", "/answer.txt")
            response = during.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read().decode(), (root / "answer.txt").read_text())
        finally:
            during.close()
        second = read_start()
        self.assertEqual(second, (first[0], "after_code_change", first[2]))
        self.assertEqual((checkout / "starts.txt").read_text().splitlines(),
                         [f"{pid} {value} {port}" for pid, value, port in (first, second)])
        connection = HTTPConnection("127.0.0.1", second[2], timeout=TEST_HANG_GUARD_SECS)
        try:
            connection.request("GET", "/answer.txt")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read().decode(), (root / "answer.txt").read_text())
        finally:
            connection.close()
        self.assertIn(f"review-links: code changed: {module_file}", errors_path.read_text())


class ServerStartupTests(unittest.TestCase):
    def test_list_fixture_serves_private_records_and_exits_when_the_folder_is_removed(self) -> None:
        checkout = Path(__file__).resolve().parents[2]
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-list-fixture-"))).resolve()
        store = temporary / "fixture"
        store.mkdir()
        keeper = staff.StaffId(str(uuid4()))
        producer = staff.StaffRow(keeper, "producer", "morty", staff.StaffRoot(), current_session=keeper)
        staff_file = store / "staff.json"
        _ = staff_file.write_text(json.dumps({"schema": 2, "rows": [producer.to_json()]}))
        with patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(store / "records"), "STAFF_FILE": str(staff_file)}):
            item = the_list.add(keeper, "Private List item", owner=the_list.UnassignedOwner.NATE,
                                source="morty", changes={})

        tailscale = temporary / "tailscale"
        _ = tailscale.write_text("#!/bin/sh\necho 127.0.0.1\n")
        tailscale.chmod(0o755)
        other_store = temporary / "other-store"
        configuration = temporary / "config"
        _ = shutil.copytree(checkout / "config", configuration)
        _ = write_config_fixture(configuration / "console.conf", f"page.list.owner={keeper}\n")
        rebuild_cache = temporary / "rebuild-cache"
        _ = (configuration / "statusline.conf").write_text(f"rebuild.cache={rebuild_cache}\n")
        _ = rebuild_cache.write_text("rebuild mac\n0\n")
        environment = {**os.environ, "PATH": str(temporary), "CLAUDE_PYTHON": sys.executable,
                       "CONF_DIR": str(configuration),
                       "REVIEW_LINKS_ROOT": str(other_store), "SHOWRUNNER_STATE_DIR": str(other_store),
                       "STAFF_FILE": str(other_store / "staff.json")}
        child: subprocess.Popen[str]
        with subprocess.Popen(
            ["/bin/bash", str(checkout / "scripts/lib/py"), str(Path(__file__).with_name("serve.py")),
             "--port", "0", "--list-fixture", str(store)],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as child:
            try:
                assert child.stdout is not None
                ready, _, _ = select.select([child.stdout], [], [], TEST_HANG_GUARD_SECS)
                self.assertEqual(ready, [child.stdout])
                start = cast(str, child.stdout.readline())
                self.assertIn(f"serving {store} on 127.0.0.1:", start)
                port = int(start.rsplit(":", 1)[1])
                connection = HTTPConnection("127.0.0.1", port, timeout=TEST_HANG_GUARD_SECS)
                try:
                    connection.request("GET", review_page.PAGE_PATH)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 404)
                    _ = response.read()
                    connection.request("GET", list_page.PAGE_PATH)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    page = response.read()
                    self.assertIn(b"Private List item", page)
                    self.assertIn(b'data-owner-new aria-label=', page)
                    connection.request("GET", review_page.NEW_ITEM_PATH + "?page=list")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    options = cast(dict[str, object], json.loads(response.read()))
                    self.assertEqual(options["recipients"], [{"id": keeper, "name": "morty", "proposal": False}])
                    self.assertEqual(options["token"], "")
                    self.assertEqual(options["send_disabled"], "Sending is off on a practice List.")
                    connection.request("GET", review_page.NEW_ITEM_PATH + "?page=review")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 404)
                    _ = response.read()
                    connection.request("POST", review_page.NEW_ITEM_PATH, b"{}", {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 404)
                    _ = response.read()
                    with self.subTest("the fixture omits live counts"):
                        self.assertNotIn(b"Waiting on other pages", page)
                    request = uuid4().hex
                    fields = {"keeper": keeper, "id": item.id, "request": request, "words": "", "until": "",
                              "token": list_page.request_token(store, keeper, item.id, request)}
                    headers = {"Content-Type": "application/x-www-form-urlencoded"}
                    connection.request("POST", list_page.PAGE_PATH,
                                       urlencode(fields | {"action": "tell", "words": "Please check this item."}), headers)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    self.assertIn(b"Only Remove is available in this demo.", response.read())
                    connection.request("POST", list_page.PAGE_PATH, urlencode(fields | {"action": "remove"}), headers)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b'"item_state": "Closed"', response.read())
                    with scratch.copy_record_lock(store):
                        console_folder = store / change_page.CONSOLE_FOLDER
                        shutil.rmtree(console_folder)
                        connection.request("GET", list_page.PAGE_PATH)
                        response = connection.getresponse()
                        _ = response.read()
                        with self.subTest("a refresh during removal cannot recreate the console folder"):
                            self.assertFalse(console_folder.exists())
                            self.assertEqual(response.status, 503)
                        connection.request("POST", list_page.PAGE_PATH,
                                           urlencode(fields | {"action": "remove"}), headers)
                        response = connection.getresponse()
                        _ = response.read()
                        with self.subTest("Remove during removal cannot recreate the console folder"):
                            self.assertFalse(console_folder.exists())
                            self.assertEqual(response.status, 503)
                        shutil.rmtree(store)
                finally:
                    connection.close()
                _, errors = child.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(child.returncode, 0, errors)
                self.assertFalse(store.exists())
                self.assertFalse(other_store.exists())
            finally:
                if child.poll() is None:
                    child.kill()

    def test_missing_list_fixture_is_reported_before_tailscale(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-missing-fixture-")))
        missing = root / "missing-parent" / "missing-fixture"
        with patch.object(serve, "tailscale_ip", side_effect=OSError("Tailscale lookup failed")) as address:
            with redirect_stderr(StringIO()) as errors:
                result = serve.main(["--list-fixture", str(missing)])
        self.assertEqual(result, 1)
        self.assertIn(str(missing), errors.getvalue())
        address.assert_not_called()

    def test_server_exits_nonzero_when_tailscale_ipv4_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="review-startup-test-") as directory:
            root = Path(directory)
            tailscale = root / "tailscale"
            _ = tailscale.write_text("#!/bin/sh\necho 'Tailscale is down' >&2\nexit 1\n", encoding="utf-8")
            tailscale.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(Path(__file__).resolve().parents[1] / "lib/py"),
                 str(Path(serve.__file__).resolve()), "--port", "0"],
                env={**os.environ, "PATH": str(root), "CLAUDE_PYTHON": sys.executable,
                     "REVIEW_LINKS_ROOT": str(root / "snapshots"), "SHOWRUNNER_STATE_DIR": str(root / "state")},
                text=True, capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tailscale", result.stderr.lower())


class ReviewLinksConfigTests(unittest.TestCase):
    def test_service_polls_read_settings_only_when_a_check_is_due(self) -> None:
        server = serve.ReviewLinksServer.__new__(serve.ReviewLinksServer)
        server.mode = serve.ServerMode.LIVE
        server.next_attachment_retry = 105.0
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        server.page_captures = serve.PageCaptures(root / "captures")
        server.next_tailscale_check = 200.0
        server.next_sweep = 300.0
        server.next_code_check = 400.0
        server.bound_address = "127.0.0.1"
        server.root = Path("unused")
        with (patch.object(time, "monotonic", return_value=100.0) as elapsed,
              patch.object(conf, "read", wraps=conf.read) as read,
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1") as address,
              patch.object(serve, "sweep_snapshots", return_value=0) as sweep):
            server.service_actions()
            server.service_actions()
            read.assert_not_called()
            address.assert_not_called()
            sweep.assert_not_called()
            elapsed.return_value = 200.0
            server.service_actions()
            read.assert_called_once_with("review_links")
            address.assert_called_once_with()
            sweep.assert_not_called()
            self.assertEqual(server.next_tailscale_check, 260.0)
            server.next_tailscale_check = 1000.0
            elapsed.return_value = 300.0
            server.service_actions()
            self.assertEqual(read.call_count, 2)
            sweep.assert_called_once()
            self.assertEqual(server.next_sweep, 3900.0)

    def test_snapshot_sweeps_read_the_retention_setting_at_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "review_links.conf"
            snapshot = root / "abc123"
            snapshot.mkdir()
            _ = (snapshot / "draft.txt").write_text("keep me")
            os.utime(snapshot, (10, 10))
            with patch.dict(os.environ, {"CONF_DIR": str(root)}):
                _ = settings.write_text("# range: 0..\nsnapshot_retention_seconds=100\n")
                self.assertEqual(serve.sweep_snapshots(root, 50), 0)
                self.assertTrue(snapshot.is_dir())
                _ = settings.write_text("# range: 0..\nsnapshot_retention_seconds=1\n")
                self.assertEqual(serve.sweep_snapshots(root, 50), 1)
                self.assertFalse(snapshot.exists())

    def test_shipped_config_supplies_the_settings_used_by_review_links(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / "review_links.conf"
        _ = write_config_fixture(path)
        settings = conf.read(path)
        values = settings.values()
        self.assertEqual(settings.text("unrouted_alert_recipient"), values["unrouted_alert_recipient"])
        for key in ("delivery_tries", "server_port", "snapshot_allocation_tries",
                    "card_fade_out_ms", "card_fade_in_ms"):
            with self.subTest(key=key):
                self.assertEqual(settings.whole(key), int(values[key]))
        for key in ("page_refresh_seconds", "delivery_pause_seconds", "unrouted_alert_retry_seconds",
                    "tailscale_timeout_seconds", "snapshot_retention_seconds", "snapshot_sweep_seconds",
                    "tailscale_check_seconds"):
            with self.subTest(key=key):
                self.assertEqual(settings.number(key), float(values[key]))

    def builds_operator_with(self, overrides: str, login_name: str) -> tuple[bool, list[list[str]]]:
        """Run builds_operator against a fake Tailscale peer with one review_links.conf override."""
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = write_config_fixture(folder / "review_links.conf", overrides)
        commands: list[list[str]] = []

        def tailscale(args: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            commands.append(args)
            self.assertEqual(args[:-1], ["tailscale", "whois", "--json", "--proto=tcp"])
            output = {"Node": {}, "UserProfile": {"ID": 8, "LoginName": login_name}}
            return subprocess.CompletedProcess(args, 0, json.dumps(output), "")

        with (patch.dict(os.environ, {"CONF_DIR": str(folder)}),
              patch.object(serve, "subprocess", Mock(wraps=subprocess, run=tailscale))):
            return serve.builds_operator(("127.0.0.1", 4242)), commands

    def test_builds_operator_refuses_each_peer_when_the_login_is_empty(self) -> None:
        self.assertEqual(self.builds_operator_with("operator_login=\n", "nate@example.com"), (False, []))

    def test_builds_operator_accepts_only_the_named_login(self) -> None:
        allowed, commands = self.builds_operator_with("operator_login=nate@example.com\n", "nate@example.com")
        self.assertTrue(allowed)
        self.assertEqual(len(commands), 1)
        refused, _ = self.builds_operator_with("operator_login=nate@example.com\n", "other@example.com")
        self.assertFalse(refused)

if __name__ == "__main__":
    _ = unittest.main()
