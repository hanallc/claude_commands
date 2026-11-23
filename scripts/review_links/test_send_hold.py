from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.review_links import page_frame, send_hold
from scripts.review_links.test_accounts_page import firefox_probe_results
from scripts.review_links.send_hold import HeldResponse, Holds, HoldState, UndoResult

OK = HeldResponse(200, "application/json; charset=utf-8", b'{"saved":true}')


class Replays:
    """Record each replayed send and answer it with one fixed response."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, str], bytes]] = []
        self.done: threading.Event = threading.Event()

    def __call__(self, path: str, headers: Mapping[str, str], body: bytes) -> HeldResponse:
        self.sent.append((path, dict(headers), body))
        self.done.set()
        return OK


class HoldsTests(unittest.TestCase):
    folder: Path = Path()

    @override
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / send_hold.HOLD_FOLDER
        _ = self.enterContext(patch.object(send_hold, "RELEASE_GRACE_SECONDS", 0.0))

    def held_files(self) -> list[str]:
        return sorted(path.name for path in self.folder.glob("*.json"))

    def test_an_undo_before_the_time_ends_replaces_the_send_with_an_undone_mark(self) -> None:
        replays = Replays()
        holds = Holds(self.folder, replays)
        hold_id = holds.hold("/review", {"Origin": "http://host", "Cookie": "kept out"}, b"words", 60)
        self.assertTrue(send_hold.valid_hold_id(hold_id))
        self.assertIs(holds.result(hold_id, wait=0), HoldState.PENDING)
        self.assertIs(holds.undo(hold_id), UndoResult.UNDONE)
        self.assertEqual(self.held_files(), [f"{hold_id}.json"])
        self.assertNotIn(base64.b64encode(b"words").decode(), (self.folder / f"{hold_id}.json").read_text())
        # A repeated undo, after a lost answer, still says undone.
        self.assertIs(holds.undo(hold_id), UndoResult.UNDONE)
        self.assertIs(holds.result(hold_id, wait=0), HoldState.UNDONE)
        with patch.object(send_hold, "RESULT_KEEP_SECONDS", -1.0):
            holds.expire()
        self.assertEqual(self.held_files(), [])
        self.assertIs(holds.undo(hold_id), UndoResult.MISSING)
        self.assertEqual(replays.sent, [])

    def test_a_stop_waits_for_a_hold_request_and_refuses_one_after_it(self) -> None:
        holds = Holds(self.folder, Replays())
        with holds.answering():
            self.assertFalse(holds.stop_if_idle())
        self.assertTrue(holds.stop_if_idle())
        with self.assertRaises(send_hold.HoldsStopped), holds.answering():
            pass

    def test_a_failed_result_write_keeps_the_result_in_memory_and_deletes_the_send(self) -> None:
        replays = Replays()
        holds = Holds(self.folder, replays)
        hold_id = holds.hold("/review", {}, b"words", 60)
        _ = holds.undo(hold_id)
        pending: send_hold.PendingFile = {"state": "pending", "path": "/review", "headers": {}, "body": "d29yZHM=",
                                          "due": time.time() + 60}
        _ = (self.folder / f"{hold_id}.json").write_text(json.dumps(pending), encoding="utf-8")
        after = Holds(self.folder, replays)
        after.recover()
        with patch.object(send_hold, "write_text_atomic", side_effect=OSError("disk full")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(after.send_now(hold_id))
            result = after.result(hold_id, wait=5)
        assert isinstance(result, HeldResponse)
        self.assertEqual(result.status, 503)
        self.assertEqual(replays.sent, [])
        self.assertEqual(self.held_files(), [])
        self.assertIs(after.undo(hold_id), UndoResult.ALREADY_SENT)

    def test_the_send_leaves_when_the_time_ends_and_its_result_stays_until_the_sweep(self) -> None:
        replays = Replays()
        holds = Holds(self.folder, replays)
        headers = {"origin": "http://host", "X-Undo-Hold": "1", "Cookie": "c", "Content-Type": "text/plain"}
        hold_id = holds.hold("/review", headers, b"words", 0.05)
        self.assertEqual(holds.result(hold_id, wait=5), OK)
        self.assertEqual(replays.sent, [("/review", {"origin": "http://host", "Content-Type": "text/plain"}, b"words")])
        # A browser that lost the answer reads it again.
        self.assertEqual(holds.result(hold_id, wait=0), OK)
        holds.expire()
        self.assertEqual(self.held_files(), [f"{hold_id}.json"])
        with patch.object(send_hold, "RESULT_KEEP_SECONDS", -1.0):
            holds.expire()
        self.assertEqual(self.held_files(), [])
        self.assertIs(holds.result(hold_id, wait=0), HoldState.MISSING)

    def test_an_undo_after_the_send_left_says_already_sent(self) -> None:
        replays = Replays()
        holds = Holds(self.folder, replays)
        hold_id = holds.hold("/list", {}, b"words", 0.0)
        self.assertTrue(replays.done.wait(5))
        self.assertEqual(holds.result(hold_id, wait=5), OK)
        self.assertIs(holds.undo(hold_id), UndoResult.ALREADY_SENT)
        self.assertEqual(replays.sent, [("/list", {}, b"words")])

    def test_send_now_releases_a_pending_send_once(self) -> None:
        replays = Replays()
        holds = Holds(self.folder, replays)
        hold_id = holds.hold("/review", {}, b"words", 60)
        self.assertTrue(holds.send_now(hold_id))
        self.assertEqual(holds.result(hold_id, wait=5), OK)
        self.assertFalse(holds.send_now(hold_id))
        self.assertIs(holds.undo(hold_id), UndoResult.ALREADY_SENT)
        self.assertEqual(len(replays.sent), 1)

    def test_a_stop_waits_for_a_running_replay_and_no_replay_starts_after_it(self) -> None:
        started, finish, calls = threading.Event(), threading.Event(), list[str]()

        def slow(path: str, _headers: Mapping[str, str], _body: bytes) -> HeldResponse:
            calls.append(path)
            started.set()
            _ = finish.wait(5)
            return OK

        holds = Holds(self.folder, slow)
        hold_id = holds.hold("/review", {}, b"words", 0.0)
        self.assertTrue(started.wait(5))
        self.assertFalse(holds.stop_if_idle())
        # The saved file already says the send may have left, so a crash now never sends it again.
        saved = cast(send_hold.ReleasedFile, json.loads((self.folder / f"{hold_id}.json").read_text(encoding="utf-8")))
        self.assertEqual((saved["status"], json.loads(base64.b64decode(saved["body"]))),
                         (503, {"reason": send_hold.MAY_HAVE_LEFT}))
        self.assertIs(holds.result(hold_id, wait=0), HoldState.PENDING)
        finish.set()
        self.assertEqual(holds.result(hold_id, wait=5), OK)
        self.assertTrue(holds.stop_if_idle())
        late = holds.hold("/list", {}, b"later", 0.0)
        time.sleep(0.2)
        self.assertEqual(calls, ["/review"])
        replays = Replays()
        after = Holds(self.folder, replays)
        after.recover()
        self.assertEqual(after.result(late, wait=5), OK)
        self.assertEqual(replays.sent, [("/list", {}, b"later")])

    def test_a_crash_during_a_replay_leaves_a_may_have_left_result(self) -> None:
        holds = Holds(self.folder, Replays())
        hold_id = holds.hold("/review", {}, b"words", 60)
        _ = holds.undo(hold_id)
        released = send_hold.released_file(send_hold.failed(send_hold.MAY_HAVE_LEFT))
        _ = (self.folder / f"{hold_id}.json").write_text(json.dumps(released), encoding="utf-8")
        replays = Replays()
        after = Holds(self.folder, replays)
        after.recover()
        result = after.result(hold_id, wait=0)
        assert isinstance(result, HeldResponse)
        self.assertEqual(result.status, 503)
        self.assertEqual(replays.sent, [])

    def test_a_bad_hold_file_is_skipped_and_the_folder_is_private(self) -> None:
        holds = Holds(self.folder, Replays())
        hold_id = holds.hold("/review", {}, b"words", 60)
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.folder / f"{hold_id}.json").stat().st_mode & 0o777, 0o600)
        _ = (self.folder / f"{'1' * 24}.json").write_text("not json", encoding="utf-8")
        _ = (self.folder / f"{'2' * 24}.json").write_text('{"state": "other"}', encoding="utf-8")
        stale = self.folder / f".{hold_id}.json.1.x.tmp"
        _ = stale.write_text("cut write", encoding="utf-8")
        os.utime(stale, (0, 0))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            Holds(self.folder, Replays()).recover()
            holds.expire()
        self.assertIn("unknown state", stderr.getvalue())
        # The sweep deletes each bad hold and each old file that is not a hold; the pending hold stays.
        self.assertEqual(self.held_files(), [f"{hold_id}.json"])
        self.assertFalse(stale.exists())
        self.assertIs(holds.result("1" * 24, wait=0), HoldState.MISSING)

    def test_a_failed_replay_gives_a_503_result(self) -> None:
        def refused(_path: str, _headers: Mapping[str, str], _body: bytes) -> HeldResponse:
            raise ConnectionRefusedError("refused")

        holds = Holds(self.folder, refused)
        result = holds.result(holds.hold("/review", {}, b"words", 0.0), wait=5)
        assert isinstance(result, HeldResponse)
        self.assertEqual(result.status, 503)
        self.assertIn(b"The held send failed", result.body)

    def test_a_restart_sends_each_held_send_and_deletes_old_results(self) -> None:
        before = Holds(self.folder, Replays())
        waiting = before.hold("/review", {}, b"words", 60)
        _ = before.undo(waiting)
        pending: send_hold.PendingFile = {"state": "pending", "path": "/review", "headers": {}, "body": "d29yZHM=",
                                          "due": time.time()}
        _ = (self.folder / f"{waiting}.json").write_text(json.dumps(pending), encoding="utf-8")
        old = "0" * 24
        released: send_hold.ReleasedFile = {
            "state": "released", "status": 200, "content_type": "text/plain", "body": "",
            "released_at": time.time() - send_hold.RESULT_KEEP_SECONDS - 1}
        _ = (self.folder / f"{old}.json").write_text(json.dumps(released), encoding="utf-8")
        _ = (self.folder / "not-a-hold.json").write_text("{}", encoding="utf-8")

        replays = Replays()
        after = Holds(self.folder, replays)
        after.recover()
        self.assertEqual(after.result(waiting, wait=5), OK)
        self.assertEqual(replays.sent, [("/review", {}, b"words")])
        self.assertEqual(self.held_files(), [f"{waiting}.json", "not-a-hold.json"])


HELD_SEND_PROBE = r"""<script>
(async () => {
 const failures = [], calls = [], hold = 'a'.repeat(24), late = 'b'.repeat(24), flaky = 'c'.repeat(24);
 const modal = 'd'.repeat(24), closing = 'e'.repeat(24), broken = 'f'.repeat(24);
 const host = window.top.document;
 const check = (ok, label) => { if (!ok) failures.push(label); };
 const wait = ms => new Promise(done => setTimeout(done, ms));
 const realFetch = window.fetch.bind(window);
 window.fetch = async (url, init = {}) => {
  url = String(url);
  calls.push([init.method || 'GET', url, new Headers(init.headers).get('X-Undo-Hold')]);
  if (url === '/send') return new Response(JSON.stringify({hold, seconds: 1.2}), {status: 202, headers: {'X-Send-Hold': 'held'}});
  if (url === '/late') return new Response(JSON.stringify({hold: late, seconds: 5}), {status: 202, headers: {'X-Send-Hold': 'held'}});
  if (url === '/flaky') return new Response(JSON.stringify({hold: flaky, seconds: .2}), {status: 202, headers: {'X-Send-Hold': 'held'}});
  for (const [path, id] of [['/modal', modal], ['/closing', closing], ['/broken', broken]])
   if (url === path) return new Response(JSON.stringify({hold: id, seconds: 30}), {status: 202, headers: {'X-Send-Hold': 'held'}});
  if (url === '/send-hold/' + broken + '/undo') throw new TypeError('offline');
  if (url === '/send-hold/' + late + '/undo') return new Response('{"outcome":"already sent"}', {status: 409});
  if (url === '/send-hold/' + flaky && calls.filter(([, called]) => called === url).length < 2) throw new TypeError('restart');
  if (url === '/plain') return new Response('{"plain":true}', {status: 200});
  if (url.endsWith('/undo')) return new Response('{"outcome":"undone"}', {status: 200});
  if (url.startsWith('/send-hold/')) {
   const first = calls.filter(([, called]) => called === url).length < 2;
   return first ? new Response('{}', {status: 202, headers: {'X-Send-Hold': 'pending'}})
                : new Response('{"saved":true}', {status: 200});
  }
  return realFetch(url, init);
 };
 const undone = heldFetch('/send', {method: 'POST', body: 'words'});
 await wait(100);
 const toast = host.querySelector('[data-send-undo]');
 check(toast?.textContent === 'SendingUndo', 'The Undo toast shows in the top page.');
 check(calls[0]?.[2] === '1', 'The send carries the hold header.');
 toast?.querySelector('button')?.click();
 try {
  await undone;
  check(false, 'Undo rejects the send.');
 } catch (error) {
  check(error instanceof SendUndone && error.message === 'Undone. Edit and send again.', 'Undo gives SendUndone.');
 }
 check(calls.some(([method, url]) => method === 'POST' && url === '/send-hold/' + hold + '/undo'), 'Undo posts the undo.');
 check(!host.querySelector('[data-send-undo-stack]'), 'The toast goes after Undo.');
 const started = performance.now();
 const sent = heldFetch('/send', {method: 'POST', body: 'words'});
 await wait(300);
 const opacity = () => Number(getComputedStyle(host.querySelector('[data-send-undo]')).opacity);
 check(opacity() > .95, 'The toast stays clear for the first half of the time.');
 await wait(700);
 check(opacity() < .6, 'The toast fades in the second half of the time.');
 const response = await sent;
 check(response.status === 200 && (await response.json()).saved === true, 'The send gives the real response.');
 check(performance.now() - started >= 1100, 'The send waits for the Undo time.');
 check(!host.querySelector('[data-send-undo]'), 'The toast goes after the send.');
 const plain = await heldFetch('/plain', {method: 'POST', body: 'words'});
 check((await plain.json()).plain === true, 'A send that the server does not hold returns at once.');
 const lateSend = heldFetch('/late', {method: 'POST', body: 'words'});
 await wait(100);
 host.querySelector('[data-send-undo] button')?.click();
 check((await lateSend).status === 200, 'A late Undo gives the real response.');
 check(host.querySelector('[data-send-undo]')?.textContent === 'Too late to undo. Sent.', 'A late Undo says the send left.');
 const flakyStarted = performance.now();
 check((await heldFetch('/flaky', {method: 'POST', body: 'words'})).status === 200, 'A lost result read is read again.');
 check(performance.now() - flakyStarted >= 1000, 'The read waits before it tries again.');
 const newestToast = () => [...host.querySelectorAll('[data-send-undo]')].at(-1);
 const stack = () => host.querySelector('[data-send-undo-stack]');
 const dialog = host.createElement('dialog');
 host.body.append(dialog);
 const inDialog = heldFetch('/modal', {method: 'POST', body: 'words'});
 await wait(100);
 const dialogToast = newestToast();
 check(stack()?.parentElement === host.body, 'The toast starts in the page body.');
 dialog.showModal();
 await wait(50);
 check(stack()?.parentElement === dialog && dialog.contains(dialogToast), 'The toast moves into a modal dialog that opens.');
 dialog.close();
 await wait(50);
 check(stack()?.parentElement === host.body, 'The toast moves back to the body when the dialog closes.');
 dialog.remove();
 dialogToast?.querySelector('button')?.click();
 await inDialog.then(() => check(false, 'Undo in the dialog rejects the send.'),
                     error => check(error instanceof SendUndone, 'Undo in the dialog gives SendUndone.'));
 const closingSend = heldFetch('/closing', {method: 'POST', body: 'words'});
 await wait(100);
 const closingToast = newestToast();
 window.top.dispatchEvent(new window.top.PageTransitionEvent('pagehide'));
 check(calls.some(([method, url]) => method === 'POST' && url === '/send-hold/' + closing + '/send'),
       'A pagehide on the top window posts the send.');
 check(closingToast && !closingToast.isConnected, 'A pagehide on the top window removes the toast.');
 check((await closingSend).status === 200, 'A send that left on pagehide reads its result.');
 const brokenSend = heldFetch('/broken', {method: 'POST', body: 'words'});
 await wait(100);
 const brokenToast = newestToast();
 brokenToast?.querySelector('button')?.click();
 const failedText = 'Undo failed. The send can still leave.';
 for (let step = 0; step < 100 && brokenToast?.textContent !== failedText; step++) await wait(50);
 check(calls.filter(([, url]) => url === '/send-hold/' + broken + '/undo').length === 2, 'Undo tries two times.');
 check(brokenToast?.textContent === failedText,
       'An Undo whose two posts fail says the send can still leave.');
 check((await brokenSend).status === 200, 'A failed Undo leaves the send to continue.');
 await realFetch('/results', {method: 'POST', body: JSON.stringify(failures)});
})().catch(error => fetch('/results', {method: 'POST', body: JSON.stringify([String(error)])}));
</script>"""


class HeldSendScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_the_undo_toast_fades_and_undo_stops_the_send(self) -> None:
        # The card scripts and the New item dialog can both load the script on one page.
        script = f"<script>{page_frame.HELD_SEND_SCRIPT}</script>"
        page = f"<!doctype html><html><head></head><body>{script}{script}{HELD_SEND_PROBE}</body></html>"
        self.assertEqual(firefox_probe_results(page.encode()), [[]])


if __name__ == "__main__":
    _ = unittest.main()
