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
from scripts.review_links.browser_test_support import firefox_probe_results
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
        hold_id = holds.hold("/review", {"Origin": "http://host", "Cookie": "kept out"}, b"words", 60)["hold"]
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

    def test_a_failed_undo_write_keeps_the_send_and_its_timer(self) -> None:
        replays = Replays()
        holds = Holds(self.folder, replays)
        hold_id = holds.hold("/review", {}, b"words", 0.3)["hold"]
        with patch.object(send_hold, "write_text_atomic", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            _ = holds.undo(hold_id)
        self.assertIs(holds.result(hold_id, wait=0), HoldState.PENDING)
        self.assertEqual(holds.result(hold_id, wait=5), OK)
        self.assertEqual(replays.sent, [("/review", {}, b"words")])
        self.assertIs(holds.undo(hold_id), UndoResult.ALREADY_SENT)

    def test_a_pending_file_without_a_timer_waits_before_its_answer(self) -> None:
        holds = Holds(self.folder, Replays())
        hold_id = holds.hold("/review", {}, b"words", 60)["hold"]
        self.assertTrue(holds.stop_if_idle())
        after = Holds(self.folder, Replays())
        started = time.monotonic()
        self.assertIs(after.result(hold_id, wait=0.3), HoldState.PENDING)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)

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
        hold_id = holds.hold("/review", {}, b"words", 60)["hold"]
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
        hold_id = holds.hold("/review", headers, b"words", 0.05)["hold"]
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
        hold_id = holds.hold("/list", {}, b"words", 0.0)["hold"]
        self.assertTrue(replays.done.wait(5))
        self.assertEqual(holds.result(hold_id, wait=5), OK)
        self.assertIs(holds.undo(hold_id), UndoResult.ALREADY_SENT)
        self.assertEqual(replays.sent, [("/list", {}, b"words")])

    def test_send_now_releases_a_pending_send_once(self) -> None:
        replays = Replays()
        holds = Holds(self.folder, replays)
        hold_id = holds.hold("/review", {}, b"words", 60)["hold"]
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
        hold_id = holds.hold("/review", {}, b"words", 0.0)["hold"]
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
        late = holds.hold("/list", {}, b"later", 0.0)["hold"]
        time.sleep(0.2)
        self.assertEqual(calls, ["/review"])
        replays = Replays()
        after = Holds(self.folder, replays)
        after.recover()
        self.assertEqual(after.result(late, wait=5), OK)
        self.assertEqual(replays.sent, [("/list", {}, b"later")])

    def test_a_crash_during_a_replay_leaves_a_may_have_left_result(self) -> None:
        holds = Holds(self.folder, Replays())
        hold_id = holds.hold("/review", {}, b"words", 60)["hold"]
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
        hold_id = holds.hold("/review", {}, b"words", 60)["hold"]
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
        result = holds.result(holds.hold("/review", {}, b"words", 0.0)["hold"], wait=5)
        assert isinstance(result, HeldResponse)
        self.assertEqual(result.status, 503)
        self.assertIn(b"The held send failed", result.body)

    def test_a_restart_sends_each_held_send_and_deletes_old_results(self) -> None:
        before = Holds(self.folder, Replays())
        waiting = before.hold("/review", {}, b"words", 60)["hold"]
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
 const lateFailure = '1'.repeat(24), deliveryFailure = '2'.repeat(24);
 let releaseResult, readingResult;
 const read = new Promise(resolve => { readingResult = resolve; });
 const result = new Promise(resolve => { releaseResult = resolve; });
 const host = window.top.document;
 const check = (ok, label) => { if (!ok) failures.push(label); };
 const wait = ms => new Promise(done => setTimeout(done, ms));
 const realFetch = window.fetch.bind(window);
 const retries = [];
 window.fetch = async (url, init = {}) => {
  url = String(url);
  calls.push([init.method || 'GET', url, new Headers(init.headers).get('X-Undo-Hold')]);
  if (url === '/send') return new Response(JSON.stringify({hold, seconds: 1.2}), {status: 202, headers: {'X-Send-Hold': 'held'}});
  if (url === '/late') return new Response(JSON.stringify({hold: late, seconds: 5}), {status: 202, headers: {'X-Send-Hold': 'held'}});
  if (url === '/flaky') return new Response(JSON.stringify({hold: flaky, seconds: .2}), {status: 202, headers: {'X-Send-Hold': 'held'}});
  for (const [path, id] of [['/modal', modal], ['/closing', closing], ['/broken', broken],
                           ['/late-failure', lateFailure], ['/delivery-failure', deliveryFailure]])
   if (url === path) return new Response(JSON.stringify({hold: id, seconds: 30}), {status: 202, headers: {'X-Send-Hold': 'held'}});
  if (url === '/send-hold/' + broken + '/undo') throw new TypeError('offline');
  if ([late, lateFailure].some(id => url === '/send-hold/' + id + '/undo'))
   return new Response('{"outcome":"already sent"}', {status: 409});
  if (url === '/send-hold/' + lateFailure) {
   readingResult();
   return result;
  }
  if (url === '/send-hold/' + deliveryFailure)
   return Response.json({saved:true,send_failure:'Delivery failed: The owner is offline.'});
  if (url === '/send-hold/' + flaky && calls.filter(([, called]) => called === url).length < 2) throw new TypeError('restart');
  if (url === '/plain') return new Response('{"plain":true}', {status: 200});
  if (url === '/retry') {
   const id=new Headers(init.headers).get('X-Undo-Request');
   retries.push(id);
   if(retries.length<=2) throw new TypeError('The hold response was lost.');
   return Response.json({hold:id,seconds:2},{status:202,headers:{'X-Send-Hold':'held'}});
  }
  if (url === '/no-time') return Response.json({hold:'3'.repeat(24),seconds:0},{status:202,headers:{'X-Send-Hold':'held'}});
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
 check(toast?.querySelector('span')?.textContent === 'Sending.' && toast.querySelector('button')?.textContent === 'Undo',
       'An unknown receiver shows Sending and Undo in the top page.');
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
 const lateSend = heldFetch('/late', {method: 'POST', body: 'words'}, 'the owner');
 await wait(100);
 host.querySelector('[data-send-undo] button')?.click();
 check((await lateSend).status === 200, 'A late Undo gives the real response.');
 check(host.querySelector('[data-send-undo]')?.textContent === 'Too late to undo. Sent to the owner.',
       'A late Undo still names the receiver.');
 const flakyStarted = performance.now();
 check((await heldFetch('/flaky', {method: 'POST', body: 'words'})).status === 200, 'A lost result read is read again.');
 check(performance.now() - flakyStarted >= 1000, 'The read waits before it tries again.');
 const newestToast = () => [...host.querySelectorAll('[data-send-undo]')].at(-1);
 const failedLate = heldFetch('/late-failure', {method: 'POST', body: 'words'}, 'the owner');
 await wait(100);
 newestToast().querySelector('button').click();
 await read;
 check(newestToast().textContent === 'Too late to undo. Checking the result…',
       'A late Undo waits for the send result.');
 releaseResult(Response.json({send_failure:'Delivery failed.'}));
 check((await failedLate).ok && newestToast().textContent === 'Too late to undo. Delivery failed.',
       'A late Undo reports the delivery failure after a confirmed save.');
 const failedDelivery = heldFetch('/delivery-failure', {method: 'POST', body: 'words'}, 'the owner');
 await wait(100);
 const deliveryToast = newestToast();
 deliveryToast.getAnimations()[0].finish();
 const failedResponse = await failedDelivery;
 check(failedResponse.ok && (await failedResponse.json()).saved, 'A failed delivery keeps the save.');
 check(deliveryToast.textContent === 'Delivery failed: The owner is offline.' &&
       getComputedStyle(deliveryToast).opacity === '1', 'Normal release shows the delivery failure.');
 const stack = () => host.querySelector('[data-send-undo-stack]');
 const dialog = host.createElement('dialog');
 host.body.append(dialog);
 const inDialog = heldFetch('/modal', {method: 'POST', body: 'words'}, null);
 await wait(100);
 const dialogToast = newestToast();
 check(dialogToast.querySelector('span').textContent === 'Saved.', 'An action with no message shows Saved.');
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
 const retry=()=>heldFetch('/retry',{method:'POST',body:'words'});
 await retry().then(()=>check(false,'The first hold response is lost.'),()=>{});
 const previousTime=performance.now();
 Object.defineProperty(performance,'now',{value:()=>previousTime+10_000});
 const retried=retry();
 await wait(100);
 const retryToast=newestToast();
 check(retryToast?.getAnimations()[0]?.effect.getTiming().duration===2000,'A retry uses the time from the server.');
 retryToast?.querySelector('button')?.click();
 await retried.then(()=>check(false,'Undo stops the retry.'),error=>check(error instanceof SendUndone,'The retry can be undone.'));
 check(retries.length===3 && /^[0-9a-f]{24}$/.test(retries[0]) && new Set(retries).size===1,
       'Each retry uses the same request ID.');
 const oldToasts=new Set(host.querySelectorAll('[data-send-undo]'));
 check((await heldFetch('/no-time',{method:'POST',body:'words'})).ok &&
       [...host.querySelectorAll('[data-send-undo]')].every(toast=>oldToasts.has(toast)),
       'A hold with no time gives the result without an Undo toast.');
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

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_a_hold_retry_stops_before_the_result_can_expire(self) -> None:
        probe = r"""<script>
(async()=>{
 const failures=[],realFetch=window.fetch.bind(window),started=Date.now(),posts=[];
 let now=started;
 Date.now=()=>now;
 window.fetch=async(url,options={})=>{
  if(options.method==='POST'){
   const id=new Headers(options.headers).get('X-Undo-Request');
   posts.push([url,id]);
   if(url==='/no-answer') throw new TypeError('The response was lost.');
   return Response.json({hold:id,seconds:0},{status:202,headers:{'X-Send-Hold':'held'}});
  }
  now+=600_000;
  return Response.json({reason:'The result expired.'},{status:404});
 };
 const send=url=>heldFetch(url,{method:'POST',body:'words'});
 const error=async url=>send(url).then(()=>'',failure=>failure.message);
 await error('/no-answer');
 now+=119_000;
 await error('/no-answer');
 now+=1_000;
 for(let retry=0;retry<2;retry++){
  if(await error('/no-answer')!=='The retry time ended. The send may have left.')
   failures.push('An expired retry must report the failure.');
 }
 if(posts.length!==4 || new Set(posts.map(([,id])=>id)).size!==1)
  failures.push('Only retries within the window can POST.');
 if((await send('/lost-result')).status!==404) failures.push('Read the missing result after the page resumes.');
 await error('/lost-result');
 if(posts.filter(([url])=>url==='/lost-result').length!==1)
  failures.push('An expired result must not allow a new POST for the same action.');
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})().catch(error=>fetch('/results',{method:'POST',body:JSON.stringify([String(error)])}));
</script>"""
        page = f"<!doctype html><html><body><script>{page_frame.HELD_SEND_SCRIPT}</script>{probe}</body></html>"
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_the_held_post_keeps_alive_only_when_its_body_fits(self) -> None:
        probe = r"""<script>
(async()=>{
 const failures=[],realFetch=window.fetch.bind(window),sent=[];
 window.fetch=async(url,options)=>{
  sent.push([url,new TextEncoder().encode(String(options.body)).length,options.keepalive]);
  return Response.json({saved:true});
 };
 for(const [url,body] of [
  ['/form',new URLSearchParams({words:'Use these words.'})],
  ['/limit','é'.repeat(32_768)],
  ['/image','é'.repeat(32_768)+'x']
 ]) await heldFetch(url,{method:'POST',body});
 if(JSON.stringify(sent)!==JSON.stringify([['/form',22,true],['/limit',65536,true],['/image',65537,false]]))
  failures.push('The actual body size determines keepalive: '+JSON.stringify(sent));
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})().catch(error=>fetch('/results',{method:'POST',body:JSON.stringify([String(error)])}));
</script>"""
        page = f"<!doctype html><html><body><script>{page_frame.HELD_SEND_SCRIPT}</script>{probe}</body></html>"
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_a_rejected_keepalive_post_retries_with_the_same_id(self) -> None:
        probe = r"""<script>
(async()=>{
 const failures=[],realFetch=window.fetch.bind(window),attempts=[],received=[];
 window.fetch=async(url,options)=>{
  if(url==='/save'){
   const id=new Headers(options.headers).get('X-Undo-Request');
   attempts.push([options.keepalive,id]);
   if(options.keepalive) throw new TypeError('The keepalive limit is in use.');
   received.push([options.method,options.body]);
   return Response.json({hold:id,seconds:0},{status:202,headers:{'X-Send-Hold':'held'}});
  }
  if(url==='/send-hold/'+attempts[0][1]) return Response.json({saved:true});
  throw new Error('Unexpected URL '+url);
 };
 const result=await heldFetch('/save',{method:'POST',body:'Keep these words.'});
 const id=attempts[0]?.[1];
 if(!/^[0-9a-f]{24}$/.test(id) || JSON.stringify(attempts)!==JSON.stringify([[true,id],[false,id]]))
  failures.push('Retry once with the same request ID and without keepalive.');
 if(JSON.stringify(received)!==JSON.stringify([['POST','Keep these words.']]) || !(await result.json()).saved)
  failures.push('The server receives one action and the page reads its result.');
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})().catch(error=>fetch('/results',{method:'POST',body:JSON.stringify([String(error)])}));
</script>"""
        page = f"<!doctype html><html><body><script>{page_frame.HELD_SEND_SCRIPT}</script>{probe}</body></html>"
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_page_close_releases_a_send_during_its_post_or_after_a_failed_undo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / send_hold.HOLD_FOLDER
            replays = Replays()
            request_id = send_hold.new_hold_id()
            self.assertTrue(Holds(folder, replays).send_now(request_id, expect_post=True))
            holds = Holds(folder, replays)
            holds.recover()
            self.assertEqual(holds.hold("/review", {}, b"words", 60, request_id=request_id)["hold"], request_id)
            self.assertEqual(holds.result(request_id, wait=2), OK)
            self.assertFalse(holds.send_now(request_id, expect_post=True))
            self.assertEqual(replays.sent, [("/review", {}, b"words")])
        probe = r"""<script>
(async()=>{
 const failures=[],realFetch=window.fetch.bind(window),host=window.top;
 const check=(ok,label)=>{if(!ok) failures.push(label);};
 const releases=[];
 let firstResponse,receive,requestId,readResult,deliverResult;
 window.fetch=async(url,options={})=>{
  if(url==='/save'){
   requestId=new Headers(options.headers).get('X-Undo-Request');
   firstResponse=Response.json({hold:requestId,seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
   return new Promise(resolve=>{
    receive=resolve;
    if(options.body==='fail Undo') receive(firstResponse);
   });
  }
  if(url.endsWith('/undo')) return Response.json({reason:'Cannot undo.'},{status:503});
  if(url.endsWith('/send')){
   releases.push([url,options.keepalive,new Headers(options.headers).get('X-Undo-Request')]);
   return Response.json({});
  }
  readResult();
  return new Promise(resolve=>{deliverResult=resolve;});
 };
 const close=()=>{
  host.dispatchEvent(new host.PageTransitionEvent('pagehide'));
  dispatchEvent(new PageTransitionEvent('pagehide'));
 };
 for(const body of ['pending POST','fail Undo']){
  const reading=new Promise(resolve=>{readResult=resolve;});
  const saving=heldFetch('/save',{method:'POST',body},null);
  if(body==='fail Undo'){
   await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
   host.document.querySelector('[data-send-undo] button').click();
   await reading;
  }
  const previous=releases.length;
  close();
  check(releases.length===previous+1 &&
   JSON.stringify(releases.at(-1))===JSON.stringify(['/send-hold/'+requestId+'/send',true,requestId]),
   body+' releases exactly once.');
  if(body==='pending POST') receive(firstResponse);
  await reading;
  check(!host.document.querySelector('[data-send-undo]'),'A closed page has no toast.');
  deliverResult(Response.json({saved:true}));
  check((await saving).ok,'The page can still read the result.');
  close();
  check(releases.length===previous+1,'A confirmed result does not release again.');
 }
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})().catch(error=>fetch('/results',{method:'POST',body:JSON.stringify([String(error)])}));
</script>"""
        markup = f"<!doctype html><html><head></head><body><script>{page_frame.HELD_SEND_SCRIPT}</script>{probe}</body></html>"
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_failed_undo_reports_the_delivery_failure(self) -> None:
        probe = r"""<script>
(async()=>{
 const failures=[],realFetch=window.fetch.bind(window),hold='a'.repeat(24);
 let readResult,release;
 const reading=new Promise(resolve=>{readResult=resolve;});
 const delivery=new Promise(resolve=>{release=resolve;});
 window.fetch=async url=>{
  if(url==='/save') return Response.json({hold,seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
  if(url.endsWith('/undo')) return Response.json({reason:'Cannot undo.'},{status:503});
  readResult();
  return delivery;
 };
 const saving=heldFetch('/save',{method:'POST',body:'words'},null);
 await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const toast=window.top.document.querySelector('[data-send-undo]');
 toast.querySelector('button').click();
 await reading;
 if(toast.textContent!=='Undo failed. The send can still leave.') failures.push('The toast must report the failed Undo.');
 release(Response.json({saved:true,send_failure:'Delivery failed: The owner is offline.'}));
 const result=await saving;
 if(!result.ok || !(await result.json()).saved) failures.push('The action must stay saved.');
 if(!toast.isConnected || toast.textContent!=='Delivery failed: The owner is offline.')
  failures.push('The toast must report the delivery failure.');
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})().catch(error=>fetch('/results',{method:'POST',body:JSON.stringify([String(error)])}));
</script>"""
        markup = f"<!doctype html><html><head></head><body><script>{page_frame.HELD_SEND_SCRIPT}</script>{probe}</body></html>"
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])


if __name__ == "__main__":
    _ = unittest.main()
