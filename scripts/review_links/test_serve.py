"""HTTP behavior on localhost and retention in disposable snapshot folders."""
from __future__ import annotations

from scripts.lint.memory_test_support import write_config_fixture

from scripts.lib.checkout_test_support import python_tool_source

import fcntl
import base64
import importlib.util
import json
import gzip
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from http.client import HTTPConnection
from io import StringIO
from pathlib import Path
from unittest.mock import ANY, call, patch


from typing import TextIO, cast, override
from urllib.parse import urlencode
from uuid import uuid4


from scripts.aws_target import aws_page
from scripts.agents.remote import local_machine
from scripts.build_queue import memory_feed
from scripts.production import change_review, change_routing, investigation, next_item, proposal_issue, staff, the_list, update_registration
from scripts.production.production_doc import read_new_production
from scripts.production.staff_test_support import fixture_member
from scripts.review_links import test_change_page
from scripts.review_links import new_item_dialog, review_submissions
from scripts.review_links import accounts_page, change_browser, change_page, console, for_you, image_store, images, investigations_page, list_page, review_actions, review_later, review_page, review_items, serve, test_review_page
from scripts.lib import atomic_file, conf, scratch
from scripts.feature_log import feature_log
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@dataclass(frozen=True)
class HttpReply:
    status: int
    content_type: str
    body: bytes


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
    _ = cast(object, unittest.enterModuleContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))


class ReviewLinksServerTests(unittest.TestCase):
    root: Path = Path()
    outside: Path = Path()
    port: int = 0
    _server: serve.ReviewLinksServer | None = None

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.review_post_done: threading.Event = threading.Event()

    @property
    def server(self) -> serve.ReviewLinksServer:
        assert self._server is not None
        return self._server

    @override
    def setUp(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="review-server-test-")))
        self.root = temporary / "root"
        self.root.mkdir()
        staff_file = temporary / "staff.json"
        _ = staff_file.write_text('{"schema":2,"rows":[]}', encoding="utf-8")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "SHOWRUNNER_STATE_DIR": str(temporary / "investigations-state"),
            "CHANGE_REVIEW_DIR": str(temporary / "change-reviews"),
            "NEXT_STATE_DIR": str(temporary / "next"),
            "STAFF_FILE": str(staff_file)})))
        self.outside = temporary / "outside.txt"
        _ = self.outside.write_text("outside the served root", encoding="utf-8")
        # The startup tests exercise code scans and recovery separately.
        with (patch.object(serve, "loaded_module_stamps", return_value={}),
              patch.object(change_page, "recover_pending"),
              patch.object(list_page, "recover_pending")):
            server = serve.ReviewLinksServer(("127.0.0.1", 0), self.root)
        self._server = server
        complete = self.review_post_done

        class ReviewFixtureHandler(serve.ReviewLinksHandler):
            @override
            def review_post(self) -> None:
                try:
                    super().review_post()
                finally:
                    complete.set()

        server.RequestHandlerClass = ReviewFixtureHandler
        self.port = server.server_port
        self.addCleanup(server.server_close)
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

    def propose_feature(self, key: str) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
            result = feature_log.main(['propose', '--review', key, '--project', 'claude',
                '--title', 'Numbered moves', '--kind', 'feature', '--summary', 'Numbered moves',
                '--behavior', 'Each group starts at 1', '--see', 'Numbers start at 1',
                '--link', '1'*40, '--evidence', 'The page test passes', '--completed', '2026-11-01'])
        self.assertEqual((result, errors.getvalue()), (0, ''))

    def new_item_http(self, body: bytes | None = None, *, query: str = '',
                      headers: dict[str, str] | None = None) -> HttpReply:
        connection = HTTPConnection('127.0.0.1', self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            connection.request('GET' if body is None else 'POST', review_page.NEW_ITEM_PATH + query,
                               body, {'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse()
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            return HttpReply(response.status, response.getheader('Content-Type', ''), response.read())
        finally:
            connection.close()

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

    def test_new_item_publishes_native_records_before_success(self) -> None:
        from scripts.review_links.test_review_submissions import EVIDENCE, PNG

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
            respond(active, status, fields)

        def message_sent(_recipient: staff.StaffId, _sender: str, _message: str) -> None:
            delivered.set()

        with (patch.object(self.server, 'review_cache', cache),
              patch.object(self.server, 'delivery_status', case.status),
              patch.object(handler, 'json_response', see_published),
              patch.object(review_items, 'read', side_effect=AssertionError('Send scanned Review.')),
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
                    self.assertNotIn(origin.reply_token.encode(), replies[0].body + snapshot.payload + snapshot.page)
                    if kind == new_item_dialog.SubmissionKind.BUG:
                        self.assertNotIn(EVIDENCE.title.encode(), snapshot.payload)
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
              patch.object(change_page, 'deliver')):
            response = self.new_item_http(json.dumps({**asdict(request), 'token': token}).encode())
            self.assertEqual(response.status, 200, response.body)
            key = cast(dict[str, str], json.loads(response.body))['key']
            self.assertIn(key, cache.get().records)
            rebuilt = review_page.build_snapshot(case.root)
            self.assertIn(key, rebuilt.records)
            self.assertEqual(review_items.source_path(rebuilt.records[key]).parent, native.resolve())
            with patch.object(cache, 'get', return_value=rebuilt):
                card = self.request(review_page.PAGE_PATH+'?'+urlencode({'body': key}))
            self.assertEqual(card.status, 200)

    def request(self, path: str) -> HttpReply:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            headers = {name.lower(): value for name, value in response.getheaders()}
            return HttpReply(response.status, headers.get("content-type", ""), response.read())
        finally:
            connection.close()

    def test_files_are_served_with_the_required_utf8_content_types(self) -> None:
        text = "review café — ready\n"
        for filename, content_type in (
            ("notes.md", "text/plain; charset=utf-8"),
            ("notes.txt", "text/plain; charset=utf-8"),
            ("notes.html", "text/html; charset=utf-8"),
        ):
            with self.subTest(filename=filename):
                _ = (self.root / filename).write_text(text, encoding="utf-8")
                reply = self.request("/" + filename)
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.content_type, content_type)
                self.assertEqual(reply.body.decode("utf-8"), text)

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
                self.assertEqual(gzip.decompress(wire),snapshot.index_response())
                self.assertLess(len(wire),len(snapshot.index_response()))
            finally:
                connection.close()

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
                        self.assertEqual(gzip.decompress(wire) if compressed else wire, snapshot.index_response())
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
            shell = self.request(review_page.PAGE_PATH+'?show=Done').body.decode()
            entries = test_review_page.unpack_index(shell.split('data-review-index>',1)[1].split('</script>',1)[0])
            with self.subTest(finding='F022'):
                self.assertNotEqual(version['version'], before.version)
                self.assertEqual(payload['version'], version['version'])
                self.assertEqual(next(entry['state'] for entry in entries if entry['key']==proposal), 'Done')
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
        self.assertEqual(cache.get().records[answer].state, review_items.ReviewState.DONE)
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
                self.assertEqual(cache.get().records[proposal].state, review_items.ReviewState.DONE)

    def test_review_finishes_verdict_scratch_cleanup_after_reply_and_before_delivery(self) -> None:
        case = self.review_fixture(changes=1)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        key = 'page-owner/C1'
        events: list[str] = []
        complete = threading.Event()
        deliveries: list[change_page.MessageDelivery] = []
        respond = serve.ReviewLinksHandler.json_response

        def response(handler: serve.ReviewLinksHandler, status: int, fields: dict[str, object]) -> None:
            self.assertEqual(cache.get().records[key].state, review_items.ReviewState.DONE)
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
            self.assertEqual(cache.get().records[key].state, review_items.ReviewState.DONE)
            raise PermissionError('The fixture closeout failed.')

        with patch.object(feature_log, 'land_review', side_effect=fail_closeout) as closeout:
            saved = cache.save(case.card_body(cache.get(), key, 'works'), case.status)
            _ = change_page.finish_verdict_cleanup(saved.delivery, case.status)
            self.assertEqual(closeout.call_count, 2)
        self.assertEqual(cache.get().records[key].state, review_items.ReviewState.DONE)
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

        def send(owner: staff.StaffId, _sender: str, message: str) -> None:
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

    def test_review_refuses_a_verdict_while_a_change_is_validating(self) -> None:
        case = self.review_fixture(changes=1)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        key = 'page-owner/C1'
        _ = cache.save(case.card_body(cache.get(), key, 'question', 'What changed?'), case.status)
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

        def send(_recipient: staff.StaffId, _sender: str, _message: str) -> None:
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

        def send(_recipient: staff.StaffId, _sender: str, _message: str) -> None:
            events.append('deliver')

        with patch.object(change_review, 'end_row_scratch', side_effect=cleanup):
            _ = cache.save(case.card_body(cache.get(), key, 'works', 'The fixture passes.'), case.status)
            self.assertEqual(events, [])
            self.assertEqual(cache.get().records[key].state, review_items.ReviewState.DONE)
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
        self.assertEqual(before.records[key].state, review_items.ReviewState.VALIDATING)
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
        _ = fields.pop('review_id')
        fields['followup'] = update_registration.PendingFollowup(text='Which layout works?', sent='2026-11-12T12:01')
        lines[index] = 'registration: '+json.dumps(fields)+'\n'
        _ = path.write_text(''.join(lines))
        (investigation.state_root() / 'record-ids' / 'question.water').unlink()
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
                             *, unowned: bool = False) -> review_items.ReviewItem:
            if before.path == path:
                reply()
            return project(before, members, unowned=unowned)

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

    def test_review_rejects_the_old_form_after_a_new_revision_is_published(self) -> None:
        case = self.review_fixture()
        proposal, _ = case.decisions()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        previous = cache.get()
        request = case.card_body(previous, proposal, 'approve')
        _ = review_page.submit(case.root, case.body(proposal, 'question', 'What changed?'), case.status)
        fresh = cache.publish_record(previous, proposal)
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
        _ = path.write_text(path.read_text().replace('Run fewer checks.', 'Run a different check.'))
        fresh = cache.publish_record(previous, proposal)
        self.assertEqual(fresh.records[proposal].state, previous.records[proposal].state)
        self.assertIn(b'Run a different check.', fresh.body(proposal))
        with patch.object(self.server, 'review_cache', cache), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'), patch.object(
                review_actions, 'decision_action', return_value=(change_page.NoMessageDelivery(), 'approved')) as action:
            status, reply = self.post(review_page.PAGE_PATH, request)
            self.assertEqual(status, 409, reply)
            action.assert_not_called()
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

        def send(_recipient: staff.StaffId, _sender: str, message: str) -> None:
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
        self.assertEqual(cache.get().records[proposal].state, review_items.ReviewState.DONE)

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
            self.assertEqual(cache.get().records[proposal].state, review_items.ReviewState.DONE)

    def test_review_conflicts_when_proposal_rank_or_state_changes_or_the_asset_disappears(self) -> None:
        case = self.review_fixture()
        key, _ = case.decisions()
        path = Path(key.removeprefix('proposal/'))
        original = path.read_text()
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
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

        with patch.object(self.server, 'review_cache', cache), patch.object(
                self.server, 'delivery_status', case.status), patch.object(change_page, 'send_to_staff') as sent:
            for finding in ('F037', 'F040', 'F038'):
                with self.subTest(finding=finding):
                    sent.reset_mock()
                    case.fixture.delivery_done.clear()
                    _ = path.write_text(original)
                    snapshot = cache.publish_record(cache.get(), key)
                    request = case.card_body(snapshot, key, 'decline')
                    if finding == 'F037':
                        _ = path.write_text(original.replace('stage: backlog', 'stage: backlog\nbacklog_risk: ★★★'))
                    with patch.object(proposal_issue, 'issue_lock',
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

    def test_review_accepts_later_on_a_current_done_card(self) -> None:
        case = self.review_fixture()
        key = case.question()
        _ = review_page.submit(case.root, case.body(key, 'answer', 'Use the first layout.'), case.status)
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        snapshot = cache.get()
        self.assertEqual(snapshot.records[key].state, review_items.ReviewState.DONE)
        with patch.object(self.server, 'review_cache', cache), patch.object(
                serve, 'tailscale_ip', return_value='127.0.0.1'):
            status, reply = self.post(review_page.PAGE_PATH,
                case.card_body(snapshot, key, 'later')+'&later=2030-12-02T18%3A30')
            self.assertEqual(status, 200, reply)
            item = cache.get().records[key]
            self.assertEqual(item.state, review_items.ReviewState.DONE)
            self.assertIsInstance(item.times.later, datetime)

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

    def post(self, path: str, body: str) -> tuple[int, bytes]:
        review_post = path == review_page.PAGE_PATH
        if review_post:
            self.review_post_done.clear()
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            connection.request("POST", path, body, {"Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            content = response.read()
            if review_post:
                self.assertTrue(self.review_post_done.wait(TEST_HANG_GUARD_SECS))
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
        with patch.object(for_you, "read", return_value=for_you.ForYouSnapshot((row,), ())) as read:
            status, headers, body = self.head("/console/for-you.json?view=count")
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(sorted(cast(dict[str, object], json.loads(body))), ["available", "count"])
        self.assertEqual(json.loads(body), {"available": True, "count": 1})
        self.assertEqual(read.call_count, 1)

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
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())))
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
        item = the_list.add(owner, "Read the List page", owner=the_list.UnassignedOwner.NATE,
                            source="nate", changes={})
        response = self.request(list_page.PAGE_PATH)
        self.assertEqual(response.status, 200)
        self.assertIn(b"Read the List page", response.body)
        _ = the_list.update(owner, item.id, {"title": "Read the new List page"}, source="morty")
        self.assertIn(b"Read the new List page", self.request(list_page.PAGE_PATH).body)
        self.assertNotEqual(self.post(list_page.PAGE_PATH, "id=L1")[0], 200)
        with patch.object(list_page, "render", side_effect=OSError("fixture read failed")):
            failed = self.request(list_page.PAGE_PATH)
            self.assertEqual(failed.status, 503)
            self.assertIn(b"The List page is not available.", failed.body)

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

    def test_list_feedback_saves_before_delivery_and_recovers_at_server_start(self) -> None:
        morty = staff.StaffRow(staff.StaffId("fixture-morty"), "producer", "morty", staff.StaffRoot(),
                               current_session=staff.StaffId("fixture-morty-session"))
        item = the_list.add(morty.id, "The feedback item", owner=the_list.UnassignedOwner.NATE, source="nate", changes={})
        sent = threading.Event()
        completed = threading.Event()
        real_deliver = list_page.deliver
        def complete_delivery(message: list_page.ListFeedbackDelivery | change_page.NoMessageDelivery) -> None:
            real_deliver(message)
            completed.set()
        def send(_recipient: staff.StaffId, _sender: str, _message: str) -> None:
            sent.set()
        with (patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((morty,))),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(list_page, "deliver", side_effect=complete_delivery),
              patch.object(change_page, "send_to_staff", side_effect=send) as delivery):
            request = uuid4().hex
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
                    if action == "read":
                        delivery.assert_not_called()
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

    def test_list_remove_reports_saved_when_directory_sync_fails_after_replace(self) -> None:
        morty = staff.StaffRow(staff.StaffId("fixture-morty"), "producer", "morty", staff.StaffRoot(),
                               current_session=staff.StaffId("fixture-morty-session"))
        item = the_list.add(morty.id, "Remove this item", owner=the_list.UnassignedOwner.NATE, source="nate", changes={})
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
            with serve.ReviewLinksServer(("127.0.0.1", 0), self.root) as server:
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
        morty = staff.StaffRow(staff.StaffId("fixture-morty"), "producer", "morty", staff.StaffRoot(),
                               current_session=staff.StaffId("fixture-morty-session"))
        item = the_list.add(morty.id, "The question item", owner=the_list.UnassignedOwner.NATE, source="nate", changes={})
        words = "Which check passed?"
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        deliveries: list[tuple[staff.StaffId, str]] = []

        def send(recipient: staff.StaffId, _sender: str, message: str) -> None:
            deliveries.append((recipient, message))
            started.set()
            self.assertTrue(release.wait(TEST_HANG_GUARD_SECS))

        real_deliver = change_page.deliver

        def deliver(message: change_page.MessageDelivery | change_page.NoMessageDelivery,
                    status: change_page.DeliveryStatus) -> None:
            try:
                real_deliver(message, status)
            finally:
                finished.set()

        request = uuid4().hex
        fields = {"keeper": morty.id, "id": item.id, "request": request, "words": words, "until": "",
                  "token": list_page.request_token(self.root, morty.id, item.id, request), "action": "question"}
        with (patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((morty,))),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(change_page, "send_to_staff", side_effect=send),
              patch.object(change_page, "deliver", side_effect=deliver)):
            try:
                code, body = self.post(list_page.PAGE_PATH, urlencode(fields))
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)["state"], "saved")
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(finished.is_set())
                opened = the_list.load(morty.id, item.id)
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

    def test_list_ask_preserves_unicode_separators(self) -> None:
        morty = staff.StaffRow(staff.StaffId("fixture-morty"), "producer", "morty", staff.StaffRoot(),
                               current_session=staff.StaffId("fixture-morty-session"))
        item = the_list.add(morty.id, "The question item", owner=the_list.UnassignedOwner.NATE, source="nate", changes={})
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
              patch.object(change_page, "send_to_staff") as send):
            for _ in range(2):
                finished.clear()
                self.assertEqual(self.post(list_page.PAGE_PATH, urlencode(fields))[0], 200)
                self.assertTrue(finished.wait(TEST_HANG_GUARD_SECS))
            send.assert_called_once()
        self.assertEqual([(question.question_id, question.words) for question in the_list.load(morty.id, item.id).questions],
                         [("Q1", words)])

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

    def test_accounts_cached_http_response_carries_a_render_failure_then_recovers(self) -> None:
        cache = serve.AccountsPageCache()
        first = accounts_page.AccountsRender(b'<p data-page-facts>board</p>', .25, 100)
        with patch.object(accounts_page, "render_with_facts", return_value=first):
            good = cache.replace()
        with (patch.object(accounts_page, "render_with_facts", side_effect=OSError("private failure")),
              redirect_stderr(StringIO())):
            _ = cache.replace()
        with patch.object(threading.Thread, "start"):
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
        with patch.object(accounts_page, "render_with_facts", return_value=first):
            recovered = cache.replace()
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
        for seconds in range(1, 13):
            with patch.object(accounts_page, "render_with_facts", return_value=accounts_page.AccountsRender(b'<p data-page-facts></p>', seconds, seconds+100)):
                _ = cache.replace()
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
                    with serve.ReviewLinksServer(("127.0.0.1", 0), self.root) as server:
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
              serve.ReviewLinksServer(("127.0.0.1", 0), self.root) as server):
            self.assertFalse(expired_at_start.exists())
            self.assertFalse(flat_at_start.exists())
            expires_later = self.snapshot("session", "expires-later", now - 8 * 24 * 60 * 60)
            flat_later = self.snapshot("", "def456", now - 8 * 24 * 60 * 60)
            elapsed.return_value = 100.0 + 3599.0
            server.service_actions()
            self.assertTrue(expires_later.exists())
            self.assertTrue(flat_later.exists())
            elapsed.return_value = 100.0 + 3600.0
            server.service_actions()
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
        return self.enterContext(serve.ReviewLinksServer(("127.0.0.1", 0), self.checkout / "snapshots"))

    def load_module(self, path: Path) -> None:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        _ = cast(object, self.enterContext(patch.dict(sys.modules, {path.stem: module})))
        spec.loader.exec_module(module)

    def test_unchanged_code_keeps_serving_and_reads_settings_only_when_the_check_is_due(self) -> None:
        server = self.start_server()
        with patch.object(conf, "read", wraps=conf.read) as read:
            self.elapsed = 104.0
            server.service_actions()
            server.service_actions()
            read.assert_not_called()
            self.elapsed = 105.0
            server.service_actions()
            read.assert_called_once_with("review_links")
            self.elapsed = 109.0
            server.service_actions()
            read.assert_called_once_with("review_links")
            self.elapsed = 110.0
            server.service_actions()
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
            server.service_actions()
        self.assertIn(str(self.module_file), str(changed.exception))

    def test_a_module_loaded_after_start_joins_the_checks_without_a_restart(self) -> None:
        server = self.start_server()
        late_module = self.checkout / "late_review_code_fixture.py"
        _ = late_module.write_text("VALUE = 'first'\n")
        os.utime(late_module, ns=(serve.STARTED_NS - 1_000_000_000, serve.STARTED_NS - 1_000_000_000))
        self.load_module(late_module)
        self.elapsed = 105.0
        server.service_actions()
        _ = late_module.write_text("VALUE = 'changed later'\n")
        self.elapsed = 110.0
        with self.assertRaises(serve.CodeChanged) as changed:
            server.service_actions()
        self.assertIn(str(late_module), str(changed.exception))

    def test_a_newly_seen_module_modified_after_process_start_requires_a_restart(self) -> None:
        server = self.start_server()
        late_module = self.checkout / "late_review_code_fixture.py"
        _ = late_module.write_text("VALUE = 'promoted before its first check'\n")
        os.utime(late_module, ns=(serve.STARTED_NS + 1_000_000_000, serve.STARTED_NS + 1_000_000_000))
        self.load_module(late_module)
        self.elapsed = 105.0
        with self.assertRaises(serve.CodeChanged) as changed:
            server.service_actions()
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
        server.service_actions()
        self.assertEqual(server.module_stamps, stamps)
        status.record(change_page.MessageDelivery(page, "C1", (), "", attempt))
        self.elapsed = 110.0
        with self.assertRaises(serve.CodeChanged) as changed:
            server.service_actions()
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
    def __init__(self, address, root):
        super().__init__(address, root)
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
        _ = (store / "staff.json").write_text(json.dumps({"schema": 2, "rows": [producer.to_json()]}))
        with patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(store / "records")}):
            item = the_list.add(keeper, "Private List item", owner=the_list.UnassignedOwner.NATE,
                                source="morty", changes={})

        tailscale = temporary / "tailscale"
        _ = tailscale.write_text("#!/bin/sh\necho 127.0.0.1\n")
        tailscale.chmod(0o755)
        other_store = temporary / "other-store"
        configuration = temporary / "config"
        _ = shutil.copytree(checkout / "config", configuration)
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
        for key in ("delivery_tries", "server_port", "snapshot_allocation_tries"):
            with self.subTest(key=key):
                self.assertEqual(settings.whole(key), int(values[key]))
        for key in ("page_refresh_seconds", "delivery_pause_seconds", "unrouted_alert_retry_seconds",
                    "tailscale_timeout_seconds", "snapshot_retention_seconds", "snapshot_sweep_seconds",
                    "tailscale_check_seconds"):
            with self.subTest(key=key):
                self.assertEqual(settings.number(key), float(values[key]))

if __name__ == "__main__":
    _ = unittest.main()
