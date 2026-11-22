from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import override
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

    def test_an_undo_before_the_time_ends_deletes_the_send(self) -> None:
        replays = Replays()
        holds = Holds(self.folder, replays)
        hold_id = holds.hold("/review", {"Origin": "http://host", "Cookie": "kept out"}, b"words", 60)
        self.assertTrue(send_hold.valid_hold_id(hold_id))
        self.assertIs(holds.result(hold_id, wait=0), HoldState.PENDING)
        self.assertIs(holds.undo(hold_id), UndoResult.UNDONE)
        self.assertEqual(self.held_files(), [])
        self.assertIs(holds.undo(hold_id), UndoResult.MISSING)
        self.assertIs(holds.result(hold_id, wait=0), HoldState.MISSING)
        self.assertEqual(replays.sent, [])

    def test_the_send_leaves_when_the_time_ends_and_its_result_is_read_once(self) -> None:
        replays = Replays()
        holds = Holds(self.folder, replays)
        hold_id = holds.hold("/review", {"Origin": "http://host", "X-Undo-Hold": "1", "Cookie": "c"}, b"words", 0.05)
        self.assertEqual(holds.result(hold_id, wait=5), OK)
        self.assertEqual(replays.sent, [("/review", {"Origin": "http://host"}, b"words")])
        self.assertIs(holds.result(hold_id, wait=0), HoldState.MISSING)
        self.assertEqual(self.held_files(), [])

    def test_an_undo_after_the_send_left_says_already_sent(self) -> None:
        replays = Replays()
        holds = Holds(self.folder, replays)
        hold_id = holds.hold("/list", {}, b"words", 0.0)
        self.assertTrue(replays.done.wait(5))
        self.assertIs(holds.undo(hold_id), UndoResult.ALREADY_SENT)
        self.assertEqual(holds.result(hold_id, wait=5), OK)

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
        timer = before._timers.pop(waiting)  # pyright: ignore[reportPrivateUsage]
        timer.cancel()
        saved = json.loads((self.folder / f"{waiting}.json").read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
        saved["due"] = time.time()
        _ = (self.folder / f"{waiting}.json").write_text(json.dumps(saved), encoding="utf-8")
        old = "0" * 24
        _ = (self.folder / f"{old}.json").write_text(json.dumps({
            "id": old, "path": "/review", "headers": {}, "body": "", "due": 0.0,
            "response": {"status": 200, "content_type": "text/plain", "body": ""},
            "released_at": time.time() - send_hold.RESULT_KEEP_SECONDS - 1}), encoding="utf-8")
        _ = (self.folder / "not-a-hold.json").write_text("{}", encoding="utf-8")

        replays = Replays()
        after = Holds(self.folder, replays)
        after.recover()
        self.assertEqual(after.result(waiting, wait=5), OK)
        self.assertEqual(replays.sent, [("/review", {}, b"words")])
        self.assertEqual(self.held_files(), ["not-a-hold.json"])


HELD_SEND_PROBE = r"""<script>
(async () => {
 const failures = [], calls = [], hold = 'a'.repeat(24), host = window.top.document;
 const check = (ok, label) => { if (!ok) failures.push(label); };
 const wait = ms => new Promise(done => setTimeout(done, ms));
 const realFetch = window.fetch.bind(window);
 window.fetch = async (url, init = {}) => {
  url = String(url);
  calls.push([init.method || 'GET', url, new Headers(init.headers).get('X-Undo-Hold')]);
  if (url === '/send') return new Response(JSON.stringify({hold, seconds: 1.2}), {status: 202, headers: {'X-Send-Hold': 'held'}});
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
