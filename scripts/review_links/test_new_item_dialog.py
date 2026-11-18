"""Test the New item dialog against a private HTTP fixture."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from dataclasses import asdict, replace, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts.production import investigation, staff
from scripts.review_links import console_theme, new_item_dialog, review_submissions
from scripts.review_links.test_review_submissions import EVIDENCE, OWNER, PNG, identify_fixture_image

ENDPOINT = "/console/review/new-item"
RECIPIENTS = [{"id": str(OWNER), "name": "builds", "proposal": True}] + [
    {"id": name, "name": name, "proposal": False}
    for name in ("cargo-liner", "hana", "natedev", "sdlc", "spacetime", "brain", "dwight", "morty")]
FORM_TOKEN = "fixture-form-token"
RESTARTED_TOKEN = "fixture-after-restart"
NO_CLIP_TEXT = "Last tooltip detail <strong> & all of its text"
DISPLAY_WORDS = "The nextest row stops updating during shard 2."


SHELL = '''
<style>
 main {width: min(1220px, calc(100% - 36px)); margin: 28px auto}
 h1 {font-size: 28px}
 .fixture-memory {display:flex;height:24px;border-radius:6px;overflow:hidden;margin-top:10px}
 .fixture-memory i:nth-child(1) {width:24%;background:#327fd1}
 .fixture-memory i:nth-child(2) {width:34%;background:#199e70}
 .fixture-memory i:nth-child(3) {width:42%;background:#5c5b57}
 table {width:100%;border-collapse:collapse;font-size:14px;text-align:left}
 th,td {padding:10px;border-bottom:1px solid var(--line)}
 #launcher {border-radius:7px;border:1px solid var(--accent);background:var(--card);color:var(--accent);
 font-size:20px;min-width:30px;cursor:pointer}
 .fixture-active {background:#182536}
 .fixture-secondary {margin-top:22px}
 @media(max-width:640px) { .fixture-extra {display:none} }
</style>
<main>
 <h1>Builds</h1>
 <section class="card">
  <h2>Overall memory</h2>
  <p><strong>18.0 GiB</strong> Free for the next step</p>
  <div class="fixture-memory"><i></i><i></i><i></i></div>
 </section>
 <section class="card">
  <h2>Running</h2>
  <table><thead><tr><th>Step</th><th>Stage</th><th class="fixture-extra">Target</th><th></th></tr></thead>
   <tbody><tr class="fixture-active"><td>hana nextest</td><td>testing</td>
    <td class="fixture-extra">natedev</td><td><button id="launcher" aria-label="New item for hana nextest">+</button></td></tr>
    <tr><td>cargo test</td><td>building</td><td class="fixture-extra">mac</td><td><button id="other-launcher" aria-label="New item for cargo test">+</button></td></tr></tbody></table>
 </section>
 <section class="card fixture-secondary">
  <h2>Recent steps</h2>
  <table><thead><tr><th>Step</th><th>State</th><th class="fixture-extra">Target</th></tr></thead><tbody>
   <tr><td>cargo check</td><td>passed</td><td class="fixture-extra">natedev</td></tr>
   <tr><td>hana checks</td><td>passed</td><td class="fixture-extra">mac</td></tr></tbody></table>
 </section>
</main>
'''


PROBE = r'''
const failures = [];
const check = (ok, description) => { if (!ok) failures.push(description); };
const dialog = document.querySelector('dialog'), body = dialog.querySelector('.new-item-body');
const textarea = dialog.querySelector('textarea'), error = dialog.querySelector('[role="alert"]');
const send = dialog.querySelector('[type="submit"]'), recipient = () => dialog.querySelector('[name="recipient_id"]');
const launch = document.querySelector('#launcher');
const capture = __CAPTURE__, png = '__PNG__';
const open = (options = {}) => window.reviewNewItem.open({trigger: launch, kind: 'bug', evidence: capture, ...options});
const closeToLauncher = () => !dialog.open && document.activeElement === launch;
function type(text) {
 textarea.value = text;
 textarea.dispatchEvent(new InputEvent('input', {bubbles:true}));
}
function paste() {
 const input = new DataTransfer();
 const bytes = Uint8Array.from(atob(png), character => character.charCodeAt(0));
 input.items.add(new File([bytes], 'pasted.png', {type:'image/png'}));
 const event = new ClipboardEvent('paste', {cancelable:true});
 Object.defineProperty(event, 'clipboardData', {value: input});
 textarea.dispatchEvent(event);
}
async function sendAndReadId() {
 const created = new Promise(resolve => document.addEventListener('review-item-created', resolve, {once:true}));
 send.click();
 return (await created).detail.id;
}
function untilError() {
 return new Promise(resolve => {
  const observer = new MutationObserver(() => {
   if (!send.disabled && !error.hidden) { observer.disconnect(); resolve(); }
  });
  observer.observe(dialog, {attributes:true, subtree:true});
 });
}
(async () => {
 try {
  if (__PHOTO__) {
   await open();
   recipient().click();
   type(__DISPLAY_WORDS__);
   document.querySelector('#new-item-title').tabIndex = -1;
   document.querySelector('#new-item-title').focus();
   check(dialog.open && dialog.offsetWidth === Math.min(560, innerWidth - 32), 'Screenshot dialog width');
  } else {
   await open();
   check(dialog.open && dialog.matches(':modal'), 'The dialog opens as a native modal.');
   check(document.activeElement.value === 'bug', 'Focus starts on the selected Type radio.');
   check([...dialog.querySelectorAll('[name="kind"]')].length === 4, 'Four keyboard-selectable types are present.');
   check(!!recipient() && recipient().tabIndex === 0, 'To uses native keyboard-selectable radios.');
   check(send.disabled, 'Send needs words and a recipient.');
   type('Check <index>\nAgain');
   check(send.disabled, 'Words alone do not enable Send.');
   recipient().click();
   check(!send.disabled, 'A recipient and words enable Send.');

   const bounds = dialog.getBoundingClientRect();
   check(innerWidth === __WIDTH__, 'Firefox uses the requested viewport width.');
   check(bounds.width === Math.min(560, innerWidth - 32), 'The dialog fits the viewport.');
   check(Math.abs(bounds.left - (innerWidth - bounds.right)) < 1 &&
         Math.abs(bounds.top - (innerHeight - bounds.bottom)) < 1, 'The dialog is centered on both axes.');
   const chips = [...dialog.querySelectorAll('[data-kind]')];
   check((chips[0].offsetTop !== chips[3].offsetTop) === (innerWidth < 480), 'Type chips wrap on a phone.');
   const evidence = dialog.querySelector('[data-evidence]'), tip = dialog.querySelector('.new-item-tip');
   check(evidence.getBoundingClientRect().bottom < textarea.getBoundingClientRect().top, 'Evidence precedes the words.');
   check(tip.lastElementChild.textContent === __LAST_DETAIL__ && !tip.querySelector('strong strong'),
         'The final tooltip entry stays visible as text.');
   check(tip.clientHeight === tip.scrollHeight && getComputedStyle(tip).backgroundColor === 'rgb(36, 36, 34)',
         'The full tooltip uses the dark background and has no internal scroll.');
   check(body.scrollHeight > body.clientHeight, 'Tall evidence scrolls the dialog body.');
   const fixedTop = dialog.querySelector('header').getBoundingClientRect().top;
   const fixedBottom = dialog.querySelector('footer').getBoundingClientRect().bottom;
   body.scrollTop = body.scrollHeight;
   check(body.scrollTop > 0 && fixedTop === dialog.querySelector('header').getBoundingClientRect().top &&
         fixedBottom === dialog.querySelector('footer').getBoundingClientRect().bottom,
         'Header and footer remain visible while the body scrolls.');
   check(body.scrollWidth === body.clientWidth, 'The dialog body has no horizontal scroll.');

   paste();
   paste();
   check(dialog.querySelectorAll('[data-previews] img').length === 2, 'Pasting adds image previews.');
   dialog.querySelector('[data-previews] button').click();
   check(dialog.querySelectorAll('[data-previews] img').length === 1, 'Remove deletes one preview.');
   await fetch('/restart', {method:'POST'});
   const failed = untilError();
   send.click();
   send.click();
   await failed;
   check(dialog.open && error.textContent === 'Fixture rejected the first send.', 'A failed send displays its reason.');
   check(textarea.value === 'Check <index>\nAgain' && dialog.querySelectorAll('[data-previews] img').length === 1,
         'A failed send keeps words and images.');
   check(document.activeElement === send, 'Focus returns to Send on failure.');
   dialog.querySelector('[data-cancel]').click();
   const other = document.querySelector('#other-launcher');
   const otherCapture = {...capture, step_id:'other-step', title:'cargo test',
                         tip_title:'cargo test', tip:[]};
   await open({trigger:other, kind:'investigation', evidence:otherCapture, recipientName:'builds'});
   check(!textarea.value && error.hidden && dialog.querySelector('[data-previews]').hidden &&
         evidence.querySelector('h4').textContent === 'cargo test',
         'Another row opens with its own evidence and an empty draft.');
   type('Check the other step');
   check(await sendAndReadId() === 'I2', 'Send saves the new item for the other row.');
   check(!dialog.open && document.activeElement === other, 'Send returns focus to the other row.');

   await open({kind:'question', evidence:{...capture, title:'A newer reading'}});
   check(textarea.value === 'Check <index>\nAgain' && !evidence.hidden && recipient().checked &&
         dialog.querySelectorAll('[data-previews] img').length === 1 &&
         error.textContent === 'Fixture rejected the first send.' && !send.disabled,
         'Reopen keeps the failed draft and its error.');
   check(await sendAndReadId() === 'I1', 'The event reports the saved item ID.');
   check(closeToLauncher(), 'A successful send closes the dialog and returns focus.');

   await open({kind:'question', evidence:{kind:'none'}, recipientName:'builds'});
   check(document.activeElement.value === 'question' && evidence.hidden && !textarea.value &&
         dialog.querySelector('[data-previews]').hidden, 'Reopen clears the sent draft and accepts empty evidence.');
   check(recipient().checked && recipient().value === 'builds-staff',
         'The launcher can select a recipient by name when the staff ID differs.');
   const to = () => [...dialog.querySelectorAll('[name="recipient_id"]')];
   const kinds = dialog.querySelector('[name="kind"][value="proposal"]');
   to()[1].click();
   kinds.click();
   check(to().length === 1 && to()[0].value === 'builds-staff' && !to()[0].checked && send.disabled,
         'Proposal filters the recipients and clears a recipient without a proposal folder.');
   dialog.querySelector('[name="kind"][value="question"]').click();
   check(to().length === 9, 'The other types include all recipients.');
   dialog.requestClose();
   check(closeToLauncher(), 'The native cancel action closes the modal and restores focus.');
   await open({evidence:{kind:'none'}, recipientName:'absent'});
   check(!dialog.querySelector('[name="recipient_id"]:checked'), 'A missing default leaves the selection empty.');
   const attempted = [], originalFetch = window.fetch;
   window.fetch = (url, options) => {
    if (url !== '/console/review/new-item' || options?.method !== 'POST') return originalFetch(url, options);
    attempted.push(JSON.parse(options.body));
    if (attempted.length === 1) {
     return Promise.resolve(new Response(
       JSON.stringify({reason:'builds has finished; choose another recipient'}), {status:409}));
    }
    if (attempted.length === 2) {
     return Promise.resolve(new Response(JSON.stringify({error:'Send is forbidden.'}), {status:403}));
    }
    if (attempted.length <= 4) {
     return Promise.resolve(new Response(
       JSON.stringify({code:'stale_token', error:'The token expired again.'}), {status:403}));
    }
    throw new Error('The dialog attempted an extra Send.');
   };
   const trySend = async () => {
    const rejected = untilError();
    send.click();
    await rejected;
   };
   recipient().click();
   type('Keep these words');
   await trySend();
   check(dialog.open && textarea.value === 'Keep these words' &&
         error.textContent === 'builds has finished; choose another recipient', 'A conflict keeps the draft and shows its reason.');
   to()[1].click();
   await trySend();
   check(attempted.length === 2 && error.textContent === 'Send is forbidden.',
         'An unrelated HTTP 403 uses the server message and does not retry.');
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   await trySend();
   window.fetch = originalFetch;
   check(attempted.length === 4 && attempted[2].request_id === attempted[3].request_id &&
         error.textContent === 'The token expired again.', 'The dialog retries an expired token once.');
   check(new Set(attempted.map(post => post.request_id)).size === 3 &&
         attempted.every(post => post.request_id.endsWith('-' + post.kind + '-' + post.recipient_id)),
         'Changing the recipient or type makes a new request ID.');
   dialog.querySelector('[data-cancel]').click();
   check(closeToLauncher(), 'Cancel restores focus to the launcher.');
  }
 } catch (failure) {
  failures.push(String(failure));
 }
 await fetch('/results', {method:'POST', body:JSON.stringify(failures)});
 parent.postMessage('finished',location.origin);
})();
'''


def fixture_page(width: int, *, photo: bool) -> bytes:
    evidence = EVIDENCE if photo else replace(EVIDENCE, tip=(
        *EVIDENCE.tip,
        *(review_submissions.TipField(f"Step detail {n}", f"Run {n}, RSS {n} GiB") for n in range(28)),
        review_submissions.TipDetail(NO_CLIP_TEXT)))
    probe = (PROBE.replace("__CAPTURE__", json.dumps(asdict(evidence)))
             .replace("__PHOTO__", json.dumps(photo)).replace("__PNG__", PNG).replace("__WIDTH__", str(width))
             .replace("__LAST_DETAIL__", json.dumps(NO_CLIP_TEXT))
             .replace("__DISPLAY_WORDS__", json.dumps(DISPLAY_WORDS)))
    return (f'<!doctype html><html lang="en" data-theme="dark"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Builds</title>'
            f'<style>{console_theme.CSS}</style>{SHELL}{new_item_dialog.render()}<script>{probe}</script>').encode()


@dataclass
class DialogFixture:
    root: Path
    results: list[str] = field(default_factory=list)
    posts: list[review_submissions.NewItemRequest] = field(default_factory=list)
    wire_posts: list[dict[str, object]] = field(default_factory=list)
    token: str = FORM_TOKEN
    recipient_reads: int = 0
    completed: bool = False

    def environment(self) -> dict[str, str]:
        return {"SHOWRUNNER_STATE_DIR": str(self.root / "state"), "REVIEW_LINKS_ROOT": str(self.root / "review")}


def capture_dialogs(output: Path, fixtures: dict[int, DialogFixture]) -> None:
    pages = {width: fixture_page(width, photo=False) for width in fixtures}
    finished = threading.Event()
    outer = f"""<!doctype html><meta charset="utf-8">
    <style>body{{margin:0}}iframe{{display:block;border:0}}</style>
    <iframe title="Dialog probe"></iframe><img src="/complete" alt="" hidden>
    <script>
    const widths={json.dumps(list(fixtures))}, frame=document.querySelector('iframe');
    let next=0;
    const load=()=>{{
      if(next===widths.length)return;
      const width=widths[next++];
      frame.width=width;frame.height=width<480?844:900;
      frame.src='/case?width='+width;
    }};
    addEventListener('message', event=>{{
      if(event.origin===location.origin && event.source===frame.contentWindow && event.data==='finished')load();
    }});
    load();
    </script>""".encode()

    class Handler(BaseHTTPRequestHandler):
        def response(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            _ = self.wfile.write(body)

        def send_json(self, status: int, body: object) -> None:
            self.response(status, "application/json", json.dumps(body).encode())

        def fixture(self) -> DialogFixture:
            width = parse_qs(urlsplit(self.headers["Referer"]).query)["width"][0]
            return fixtures[int(width)]

        def do_GET(self) -> None:
            if self.path == ENDPOINT:
                fixture = self.fixture()
                fixture.recipient_reads += 1
                self.send_json(200, {"token": fixture.token, "recipients": RECIPIENTS})
            elif self.path == "/complete":
                _ = finished.wait(10)
                self.response(204, "image/png", b"")
            elif self.path == "/":
                self.response(200, "text/html; charset=utf-8", outer)
            elif self.path.startswith("/case?"):
                width = int(parse_qs(urlsplit(self.path).query)["width"][0])
                self.response(200, "text/html; charset=utf-8", pages[width])
            else:
                self.response(404, "text/plain", b"Unknown fixture path")

        def do_POST(self) -> None:
            if self.path == "/restart":
                self.fixture().token = RESTARTED_TOKEN
                self.response(204, "text/plain", b"")
                return
            data = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/results":
                fixture = self.fixture()
                fixture.results.extend(cast(list[str], json.loads(data)))
                fixture.completed = True
                self.response(204, "text/plain", b"")
                if all(case.completed for case in fixtures.values()):
                    finished.set()
                return
            if self.path != ENDPOINT:
                self.response(404, "text/plain", b"Unknown fixture path")
                return
            if self.headers["Content-Type"] != "application/json":
                self.send_json(400, {"reason": "The request must use JSON."})
                return
            fixture = self.fixture()
            fields = cast(dict[str, object], json.loads(data))
            fixture.wire_posts.append(fields)
            if fields.get("token") != fixture.token:
                self.send_json(403, {"code": "stale_token", "error": "The New item token expired. Try Send again."})
                return
            try:
                request = review_submissions.read_request(fields)
                fixture.posts.append(request)
                with patch.dict(os.environ, fixture.environment()), patch.object(Path, "home", return_value=fixture.root):
                    item = review_submissions.submit(request)
            except review_submissions.SubmissionConflict as error:
                self.send_json(error.status, {"reason": str(error)})
                return
            except (OSError, ValueError) as error:
                self.send_json(400, {"reason": str(error)})
                return
            if len(fixture.posts) == 1:
                self.send_json(503, {"error": "Fixture rejected the first send."})
                return
            self.send_json(201, {"id": item.id, "key": item.key, "state": "Waiting"})

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            profile = output.parent / "profile"
            profile.mkdir()
            run = subprocess.run(
                ["firefox", "--headless", "--no-remote", "--profile", str(profile),
                 "--window-size", "1440,900", "--screenshot", str(output),
                 f"http://127.0.0.1:{server.server_port}/"],
                capture_output=True, text=True, timeout=15, check=False)
            if run.returncode:
                raise AssertionError(run.stderr)
            if not finished.is_set():
                raise AssertionError("Firefox did not report its fixture results.")
        finally:
            server.shutdown()
            thread.join(1)


class NewItemDialogTests(unittest.TestCase):
    def test_render_mounts_one_dialog_with_a_four_line_field_and_four_types(self) -> None:
        markup = new_item_dialog.render()
        self.assertEqual(markup.count('<dialog id="review-new-item"'), 1)
        self.assertIn('name="words" rows="4"', markup)
        self.assertEqual(markup.count('type="radio" name="kind"'), 4)
        self.assertIn('data-kind="bug"', markup)
        self.assertIn('<svg aria-hidden="true"', markup)
        self.assertLess(markup.index('data-evidence'), markup.index('name="words"'))

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_desktop_and_phone_dialogs(self) -> None:
        temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        recipient = staff.StaffRow(OWNER, "producer", "builds", staff.StaffRoot(), current_session=OWNER)
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((recipient,))))
        _ = self.enterContext(patch.object(review_submissions, "_identify_image", autospec=True,
                                           side_effect=identify_fixture_image))
        fixtures = {width: DialogFixture(temporary / str(width)) for width in (1440, 390)}
        capture_dialogs(temporary / "dialog.png", fixtures)
        for width, fixture in fixtures.items():
            with self.subTest(width=width), patch.dict(os.environ, fixture.environment()):
                self.assertEqual(fixture.results, [])
                self.assertEqual(fixture.recipient_reads, 7)
                expired, refreshed, other_wire, reopened = fixture.wire_posts
                self.assertEqual(expired["token"], FORM_TOKEN)
                self.assertEqual(refreshed, {**expired, "token": RESTARTED_TOKEN})
                self.assertEqual(reopened, refreshed)
                first, other, retry = fixture.posts
                self.assertNotEqual(other_wire["request_id"], refreshed["request_id"])
                self.assertEqual(other.recipient_id, OWNER)
                self.assertEqual(other.words, "Check the other step")
                self.assertEqual(other.images, ())
                assert isinstance(other.evidence, review_submissions.BuildStepEvidence)
                self.assertEqual(other.evidence.step_id, "other-step")
                self.assertEqual(other.evidence.title, "cargo test")
                self.assertEqual(first, retry)
                self.assertEqual(first.kind, review_submissions.SubmissionKind.BUG)
                self.assertEqual(first.recipient_id, OWNER)
                self.assertEqual(first.evidence.kind, "build-step")
                self.assertEqual(first.images, (review_submissions.PastedImage("image/png", PNG),))
                self.assertCountEqual([item.id for item in investigation.records(OWNER)], ["I1", "I2"])
