"""Test image registration, the Images page, deletes and feedback in private folders."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode

from scripts.review_links.test_page_frame import assert_status_states
from scripts.review_links.test_staff_names import without_role_colors
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member
from scripts.review_links import change_page, console, image_store, images, page_frame, serve

PNG = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63606060f80f00010401005fe5c34b0000000049454e44ae426082')


class ImagesFixture(unittest.TestCase):
    temporary: Path = Path()
    root: Path = Path()
    shot: Path = Path()
    owner: staff.StaffRow | None = None
    sent: list[tuple[str, str, str]] = []
    status: change_page.DeliveryStatus = change_page.DeliveryStatus(Path())

    @override
    def setUp(self) -> None:
        self.temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="images-test-")))
        configuration = self.temporary / "config"
        configuration.mkdir()
        for stem in ("console", "review_links", "change_review"):
            _ = shutil.copyfile(Path(__file__).resolve().parents[2] / f"config/{stem}.conf",
                                configuration / f"{stem}.conf")
        settings = configuration / "review_links.conf"
        _ = settings.write_text(settings.read_text().replace("delivery_pause_seconds=5", "delivery_pause_seconds=0"))
        self.root = self.temporary / "snapshots"
        self.status = change_page.DeliveryStatus(self.root)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(configuration), "REVIEW_LINKS_ROOT": str(self.root), "REVIEW_LINKS_HOST": "127.0.0.1",
            "STAFF_FILE": str(self.temporary / "staff.json"), "USER_ZONE": "America/New_York"})))
        runner = fixture_member("showrunner", name="shot-runner")
        self.owner = fixture_member("unit", name="shot-owner", parent=runner.id)
        self.shot = self.temporary / "before.png"
        _ = self.shot.write_bytes(PNG)
        self.sent = []
        _ = self.enterContext(patch.object(change_page, "send_message", side_effect=self.capture))

    def submit(self, body: str) -> None:
        delivery = images.submit(self.root, body, self.status)
        change_page.deliver(delivery, self.status)

    def capture(self, recipient: str, sender: str, message: str) -> None:
        self.sent.append((recipient, sender, message))

    def owner_id(self) -> staff.StaffId:
        assert self.owner is not None
        return self.owner.id

    def register(self, caption: str = "The nav shows Images. Look at the left column.") -> image_store.ImageRecord:
        return images.register(self.shot, caption, self.owner_id(), "Phase console-images")


class ImagesNewsTests(ImagesFixture):
    def pages(self) -> tuple[console.ConsolePage, ...]:
        return tuple(page for page in console.read_pages() if page.identity == "images")

    def test_the_first_visit_records_waiting_images_without_a_new_mark(self) -> None:
        record = self.register()
        self.assertEqual(console.news(self.root, self.pages(), "changes"), ())
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset((record["id"],)))
        seen = cast(dict[str, list[str]], json.loads((self.root / change_page.CONSOLE_FOLDER / console.SEEN_NAME).read_text()))
        self.assertEqual(seen["images"], [record["id"]])

    def test_a_new_image_marks_images_until_the_user_opens_the_page(self) -> None:
        pages = self.pages()
        self.assertEqual(console.news(self.root, pages, "changes"), ())
        _ = self.register()
        self.assertEqual(console.news(self.root, pages, "changes"), ("images",))
        self.assertEqual(console.news(self.root, pages, "images"), ())
        self.assertEqual(console.news(self.root, pages, "changes"), ())

    def test_feedback_and_temporary_change_images_give_no_new_mark(self) -> None:
        pages = self.pages()
        self.assertEqual(console.news(self.root, pages, "changes"), ())
        answered = self.register("Answered image")
        answered["feedback_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        image_store.write_record(images.images_root(self.root) / answered["id"], answered)
        change = image_store.register_change(self.shot)
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset())
        self.assertEqual(console.news(self.root, pages, "changes"), ())
        body = images.render(self.status).decode()
        self.assertNotIn(answered["id"], body)
        self.assertNotIn(change, body)
        waiting = self.register("Waiting image")
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset((waiting["id"],)))
        self.assertIn(waiting["id"], images.render(self.status).decode())
        self.assertEqual(console.news(self.root, pages, "changes"), ("images",))


class ChangeImageTests(ImagesFixture):
    def test_change_images_do_not_appear_on_the_images_page(self) -> None:
        identity = image_store.register_change(self.shot)
        self.assertEqual(images.records(self.root), [])
        self.assertNotIn(identity, images.render(self.status).decode())
        self.assertTrue(image_store.change_image(self.root, identity).startswith("/change-images/"))

    def test_output_registers_a_dark_terminal_image_with_a_private_profile(self) -> None:
        output = self.temporary / "output.txt"
        _ = output.write_text("$ verify\nAll <checks> passed & ready\n")

        def chrome(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            page = Path(arguments[-1].removeprefix("file://"))
            self.assertIn("All &lt;checks&gt; passed &amp; ready", page.read_text())
            self.assertIn("background:#161b22", page.read_text())
            profile = next(value.split("=", 1)[1] for value in arguments if value.startswith("--user-data-dir="))
            self.assertTrue(Path(profile).is_relative_to(page.parent))
            if "--dump-dom" in arguments:
                self.assertIn("Math.ceil(document.body.getBoundingClientRect().height)", page.read_text())
                return subprocess.CompletedProcess(arguments, 0, "<title>output-size:92</title>", "")
            self.assertIn("--window-size=640,160", arguments)
            shot = next(value.split("=", 1)[1] for value in arguments if value.startswith("--screenshot="))
            _ = Path(shot).write_bytes(PNG)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch("scripts.review_links.image_store.subprocess.run", side_effect=chrome) as run:
            identity = image_store.register_output(output)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(image_store.change_image(self.root, identity))
        arguments = cast(list[str], run.call_args.args[0])
        self.assertFalse(Path(arguments[-1].removeprefix("file://")).parent.exists())

    def test_output_sizes_wrapped_lines_with_a_minimum_height_and_a_cap(self) -> None:
        output = self.temporary / "wrapped.txt"
        _ = output.write_text("x" * 300 + "\n" + "short\n" * 5)
        for content, height in ((70, 160), (160, 160), (246, 246), (5000, 4096)):
            with self.subTest(content=content):
                def chrome(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    self.assertIn("--force-device-scale-factor=1", arguments)
                    if "--dump-dom" in arguments:
                        self.assertIn("--window-size=1440,600", arguments)
                        return subprocess.CompletedProcess(arguments, 0,
                            f"<title>output-size:{content}</title>", "")
                    self.assertIn(f"--window-size=1440,{height}", arguments)
                    shot = next(value.split("=", 1)[1] for value in arguments if value.startswith("--screenshot="))
                    _ = Path(shot).write_bytes(PNG)
                    return subprocess.CompletedProcess(arguments, 0, "", "")

                with patch("scripts.review_links.image_store.subprocess.run", side_effect=chrome) as run:
                    identity = image_store.register_output(output)
                self.assertEqual(run.call_count, 2)
                self.assertTrue(image_store.change_image(self.root, identity))


class ImagesTests(ImagesFixture):
    def test_staff_image_owners_have_role_colors_and_unknown_owners_stay_plain(self) -> None:
        record = self.register()
        body = images.render(self.status).decode()
        self.assertRegex(body, r'data-color-text="role.unit-director"[^>]*>shot-owner</span>')
        unknown = record.copy()
        unknown["owner"] = "missing-owner"
        self.assertIn('Unknown session: missing-owner</dd>', images.card(unknown, "private", staff.snapshot(), 2))

    def test_register_copies_the_image_into_a_folder_that_the_record_owns(self) -> None:
        record = self.register()
        folder = self.root / image_store.IMAGES_FOLDER / record["id"]
        self.assertEqual(sorted(path.name for path in folder.iterdir()), [image_store.RECORD, "image.png"])
        self.assertEqual((folder / "image.png").read_bytes(), PNG)
        self.assertEqual(image_store.read_record(folder), record)
        self.assertEqual(record["subject"], "Phase console-images")
        self.shot.unlink()
        self.assertEqual(images.records(self.root), [record])
        images.delete(self.root, record["id"])
        self.assertFalse(folder.exists())
        self.assertEqual(images.records(self.root), [])

    def test_register_refuses_bad_input_and_leaves_no_folder(self) -> None:
        text = self.temporary / "notes.txt"
        _ = text.write_text("not an image")
        cases = ((text, "caption", self.owner_id(), "use one of"),
                 (self.temporary / "absent.png", "caption", self.owner_id(), "no such image file"),
                 (self.shot, "  ", self.owner_id(), "the caption is empty"),
                 (self.shot, "caption", staff.StaffId("absent-owner"), "no staff record"))
        for image, caption, owner, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                _ = images.register(image, caption, owner, "Change sdlc/C1")
        self.assertEqual(list((self.root / image_store.IMAGES_FOLDER).glob("*")) if (self.root / image_store.IMAGES_FOLDER).exists() else [], [])

    def test_command_prints_the_image_and_console_links_and_deletes(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = images.main(["register", "--image", str(self.shot), "--caption", "Look at the cards",
                                "--owner", self.owner_id(), "--change", "sdlc/C9"])
        self.assertEqual(code, 0)
        record = images.records(self.root)[0]
        self.assertEqual(record["subject"], "Change sdlc/C9")
        self.assertEqual(output.getvalue().splitlines(), [
            f"registered {record['id']}: http://127.0.0.1:8765/images/{record['id']}/image.png",
            "Image link: http://127.0.0.1:8765/console/?page=images&at.images="
            + f"%2Fconsole%2Fimages.html%23image%3D{record['id']}"])
        errors = StringIO()
        with redirect_stderr(errors):
            self.assertEqual(images.main(["delete", "--id", "000000000000"]), 1)
        self.assertIn("no image 000000000000", errors.getvalue())
        with redirect_stdout(StringIO()):
            self.assertEqual(images.main(["delete", "--id", record["id"]]), 0)
        self.assertEqual(images.records(self.root), [])

    def test_page_lists_oldest_first_with_owner_subject_group_links_and_signed_forms(self) -> None:
        older, newer = self.register("Older shot"), self.register("Newer shot")
        older_folder = self.root / image_store.IMAGES_FOLDER / older["id"]
        older["registered_at"] = (datetime.now(UTC) - timedelta(days=9)).isoformat(timespec="seconds")
        image_store.write_record(older_folder, older)
        source = images.render(self.status).decode()
        self.assertEqual(source.count(page_frame.ZOOM_STYLE), 1)
        self.assertEqual(source.count(page_frame.ZOOM_SCRIPT), 1)
        self.assertEqual(source.count(page_frame.MOTION_SCRIPT), 1)
        self.assertEqual(source.count(page_frame.REFRESH_SCRIPT), 1)
        self.assertIn('data-refresh-seconds="0"', source)
        self.assertIn("prefers-reduced-motion: reduce", source)
        self.assertLess(source.index(older["id"]), source.index(newer["id"]))
        self.assertIn('<nav data-image-groups aria-label="Image groups"></nav>', source)
        self.assertIn("last.owner === card.dataset.owner", source)
        self.assertIn("link.href = '#' + row.card.id;", source)
        self.assertIn("document.getElementById('image-' + start.get('image'))", source)
        self.assertIn(f'<article id="image-{older["id"]}"', source)
        self.assertIn("Newer shot", source)
        self.assertIn("shot-owner (unit)", without_role_colors(source))
        self.assertIn("Phase console-images", source)
        self.assertIn(f'src="/images/{newer['id']}/image.png"', source)
        token = images.request_token(images.console_token(self.root), newer["id"])
        self.assertIn(f'name="token" value="{token}"', source)
        self.assertNotIn('data-image-filter', source)
        self.assertNotIn('image-window', source)
        self.assertNotIn("start.get('show')", source)
        self.assertIn("card.remove();", source)
        self.assertIn("home.insertBefore(card, next?.parentNode === home ? next : null);", source)
        self.assertIn("status.textContent = 'Not sent: ' + error.message;", source)
        self.assertIn("new FormData(form, event.submitter)", source)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_page_status_uses_the_real_images_loader(self) -> None:
        older = self.register("Older shot")
        _ = self.register("Newer shot")
        older_folder = self.root / image_store.IMAGES_FOLDER / older["id"]
        older["registered_at"] = (datetime.now(UTC) - timedelta(days=9)).isoformat(timespec="seconds")
        image_store.write_record(older_folder, older)
        source = images.render(self.status).decode()
        self.assertIn('</h1><p class="page-status"', source)
        self.assertLess(source.index('data-page-status '), source.index('<div data-image-layout'))
        assert_status_states(self, source, 0)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_updates_keep_zoom_and_feedback_working_after_an_older_script_reloads(self) -> None:
        older = self.register()
        older["registered_at"] = (datetime.now(UTC) - timedelta(days=9)).isoformat(timespec="seconds")
        image_store.write_record(images.images_root(self.root) / older["id"], older)
        current = images.render(self.status).decode()
        marker = f'data-script="{images.SCRIPT_VERSION}"'
        self.assertEqual(current.count(marker), 1)
        probe = """<script>
(async () => {
 const wait = ms => new Promise(done => setTimeout(done, ms));
 const report = failures => fetch('/results', {method: 'POST', body: JSON.stringify(failures)});
 const list = () => document.querySelector('[data-images]');
 if (list().dataset.script === 'older-script') {
  document.dispatchEvent(new Event('visibilitychange'));
  await wait(5000);
  await report(['an update from a newer script did not reload the page']);
  return;
 }
 const failures = [], check = (ok, text) => { if (!ok) failures.push(text); };
 const before = list();
 document.dispatchEvent(new Event('visibilitychange'));
 for (let step = 0; step < 100 && list() === before; step++) await wait(20);
 check(list() !== before, 'the update puts in a new list');
 const card = list().querySelector('article');
 check(!document.querySelector('[data-image-filter], #image-window'), 'the page has no age filter');
 check(!card.hidden && Date.now() - Date.parse(card.querySelector('time').dateTime) > 7*86400000,
       'the image older than seven days stays visible after an update');
 const link = card.querySelector('a[data-image-zoom]');
 const zoom = document.querySelector('[data-image-zoom-dialog]');
 link.click();
 check(zoom.open, 'a plain click opens the zoom after the list changes');
 check(document.activeElement === zoom, 'the zoom takes focus');
 check(zoom.getAttribute('role') === 'dialog' && zoom.getAttribute('aria-modal') === 'true', 'the zoom is modal');
 check(zoom.getAttribute('aria-label') === link.querySelector('img').alt, 'the image names the zoom');
 await zoom.querySelector('img').decode();
 check(zoom.querySelector('img').getBoundingClientRect().width === 1, 'the zoom keeps the natural size limit');
 zoom.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}));
 check(!zoom.open && document.activeElement === link, 'Escape closes the zoom and returns focus');
 for (const modified of [{shiftKey:true}, {ctrlKey:true}, {metaKey:true}, {altKey:true}, {button:1}]) {
  let allowed = false;
  const stopNavigation = event => {allowed = !event.defaultPrevented; event.preventDefault();};
  document.addEventListener('click', stopNavigation, {once:true});
  link.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, ...modified}));
  check(allowed && !zoom.open, 'modified clicks keep the link action');
 }
 link.click();zoom.querySelector('img').click();
 check(!zoom.open && document.activeElement === link, 'an image click closes the zoom and returns focus');
 link.click();zoom.click();
 check(!zoom.open, 'a backdrop click closes the zoom');
 link.click();zoom.dispatchEvent(new Event('cancel', {cancelable:true}));
 check(!zoom.open, 'the native cancel event closes the zoom');
 const actions = [['approved', ''], ['not approved', ''], ['feedback', 'Too dark.'],
                  ['question', 'Which check passed?'], ['feedback', 'Please retry.']];
 for (const [action, words] of actions) {
  const previous = list();
  const form = previous.querySelector('form');
  form.elements.feedback.value = words;
  form.requestSubmit(form.querySelector(`button[value="${action}"]`));
  for (let step = 0; step < 100 && list() === previous; step++) await wait(20);
  check(list() !== previous, action + ' refreshes the cards after the POST');
  check(!form.isConnected, action + ' replaces the old form');
  check(list().querySelector('article')?.id === card.id, action + ' shows the card that the server kept');
 }
 const restored = list().querySelector('form');
 check(restored.elements.feedback.value === 'Please retry.', 'failed feedback keeps its draft');
 check(restored.querySelector('[data-image-status]').textContent === 'Not sent: The owner is unavailable.',
       'failed feedback keeps its status');
 const fetchPage = window.fetch.bind(window);
 let failedRefresh = false, holdNextRefresh = false, oldRefreshReady = false, oldRefreshReleased = false;
 let releaseOldRefresh;
 const holdOldRefresh = new Promise(resolve => {releaseOldRefresh = resolve;});
 window.fetch = async (path, options) => {
  const isRefresh = path === location.pathname && options?.cache === 'no-store';
  const hold = isRefresh && holdNextRefresh;
  if (hold) holdNextRefresh = false;
  const response = await fetchPage(path, options);
  if (isRefresh && !response.ok) setTimeout(() => {failedRefresh = true;}, 0);
  if (!hold) return response;
  const body = await response.text();
  oldRefreshReady = true;
  await holdOldRefresh;
  setTimeout(() => {oldRefreshReleased = true;}, 0);
  return {ok: response.ok, text: async () => body};
 };
 const submitFeedback = words => {
  const form = list().querySelector('form');
  form.elements.feedback.value = words;
  form.requestSubmit(form.querySelector('button[value="feedback"]'));
 };
 submitFeedback('Retry after refresh fails.');
 for (let step = 0; step < 100 && !failedRefresh; step++) await wait(20);
 check(failedRefresh, 'the refresh after refused feedback fails');
 check(restored.elements.feedback.value === 'Retry after refresh fails.', 'both failures keep the draft');
 check(restored.querySelector('[data-image-status]').textContent === 'Not sent: The owner is unavailable.',
       'both failures keep the POST error');
 holdNextRefresh = true;
 submitFeedback('Keep until next feedback.');
 for (let step = 0; step < 100 && !oldRefreshReady; step++) await wait(20);
 check(oldRefreshReady, 'the older refresh has a page with the card');
 const previous = list();
 submitFeedback('Routine feedback.');
 for (let step = 0; step < 100 && list() === previous; step++) await wait(20);
 check(list() !== previous, 'routine feedback refreshes the list');
 check(!list().querySelector('article'), 'routine feedback leaves the card absent');
 releaseOldRefresh();
 for (let step = 0; step < 100 && !oldRefreshReleased; step++) await wait(20);
 check(oldRefreshReleased && !list().querySelector('article'), 'the older refresh cannot restore the card');
 check(list() !== null && location.pathname === '""" + images.PAGE_PATH + """', 'the page stays after Send Feedback');
 await report(failures);
})().catch(error => fetch('/results', {method:'POST', body:JSON.stringify([String(error)])}));
</script>"""
        gets: list[str] = []
        posts: list[dict[str, list[str]]] = []
        results: list[list[str]] = []
        probe_complete = threading.Event()
        fixture = self

        class BrowserProbe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/probe-complete":
                    # Keep Firefox alive until the asynchronous checks report their result.
                    if not probe_complete.wait(20):
                        self.send_error(504, "The browser checks did not complete.")
                        return
                    self.send_response(204)
                    self.end_headers()
                    return
                if self.path == "/":
                    # A frame holds the page, as in the console, so its reload does not end the screenshot load.
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    _ = self.wfile.write(f'<iframe src="{images.PAGE_PATH}" width="1100" height="800"></iframe>'
                                         .encode() + b'<img src="/probe-complete" alt="" hidden>')
                    return
                if self.path.endswith("/image.png"):
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    _ = self.wfile.write(PNG)
                    return
                if self.path != images.PAGE_PATH:
                    self.send_error(404)
                    return
                gets.append(self.path)
                words = posts[-1]["feedback"][0] if posts else ""
                if words == "Retry after refresh fails.":
                    self.send_error(503, "The list is unavailable.")
                    return
                if len(gets) == 1:
                    page = current.replace(marker, 'data-script="older-script"')
                elif words == "Routine feedback.":
                    page = images.render(fixture.status).decode()
                else:
                    page = current
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                _ = self.wfile.write(page.replace("</body>", probe + "</body>").encode())

            def do_POST(self) -> None:
                payload = self.rfile.read(int(self.headers["Content-Length"])).decode()
                if self.path == "/results":
                    results.append(cast(list[str], json.loads(payload)))
                    probe_complete.set()
                    self.send_response(204)
                    self.end_headers()
                    return
                fields = parse_qs(payload, keep_blank_values=True)
                posts.append(fields)
                words = fields["feedback"][0]
                refused = words in ("Please retry.", "Retry after refresh fails.", "Keep until next feedback.")
                if words == "Routine feedback.":
                    fixture.submit(payload)
                self.send_response(503 if refused else 200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                receipt = {"reason": "The owner is unavailable."} if refused else {"state": "done"}
                _ = self.wfile.write(json.dumps(receipt).encode())

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), BrowserProbe) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                profile = self.temporary / "browser-profile"
                profile.mkdir()
                run = subprocess.run(["firefox", "--headless", "--no-remote", "--profile", str(profile),
                                      "--window-size", "1200,900", "--screenshot", str(self.temporary / "browser.png"),
                                      f"http://127.0.0.1:{server.server_port}/"],
                                     capture_output=True, text=True, timeout=45)
                self.assertEqual(run.returncode, 0, run.stderr)
            finally:
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)
        self.assertEqual(results, [[]], "The browser must complete every check without a failure.")
        self.assertEqual(len(gets), 12, "load, update, reload, update, then one refresh per POST")
        self.assertEqual([(post["action"], post["feedback"]) for post in posts], [
            (["approved"], [""]), (["not approved"], [""]), (["feedback"], ["Too dark."]),
            (["question"], ["Which check passed?"]), (["feedback"], ["Please retry."]),
            (["feedback"], ["Retry after refresh fails."]), (["feedback"], ["Keep until next feedback."]),
            (["feedback"], ["Routine feedback."]),
        ])

    def test_card_shows_the_caption_details_image_feedback_field_and_the_answer_buttons(self) -> None:
        record = self.register("Look at <the> cards")
        record["registered_at"] = "2026-11-15T03:09:00+00:00"
        image_store.write_record(self.root / image_store.IMAGES_FOLDER / record["id"], record)
        card = images.card(record, "token", staff.snapshot(), 3)
        path = f"/images/{record['id']}/image.png"
        token = images.request_token("token", record["id"])
        self.assertEqual(without_role_colors(card), (
            f'<article id="image-{record['id']}" data-image="{record['id']}"'
            + f' data-owner="{record['owner']}" data-owner-name="shot-owner">'
            + '<h2 class="caption">Look at &lt;the&gt; cards</h2><dl class="image-details">'
            + '<dt>Owner:</dt><dd>shot-owner (unit)</dd><dt>For  :</dt><dd>Phase console-images</dd>'
            + '<dt>Added:</dt><dd><time datetime="2026-11-15T03:09:00+00:00">Nov 14, 2026, 22:09</time></dd></dl>'
            + f'<a data-image-zoom href="{path}" target="_blank" rel="noopener"><img src="{path}" alt="Look at &lt;the&gt; cards" loading="lazy"></a>'
            + f'<form method="post" data-image-form><input type="hidden" name="image" value="{record['id']}">'
            + f'<input type="hidden" name="token" value="{token}">'
            + f'<label for="feedback-{record['id']}">Feedback</label>'
            + f'<textarea id="feedback-{record['id']}" name="feedback" rows="3"></textarea>'
            + '<div class="image-actions"><button type="submit" name="action" value="approved">Approved</button>'
            + '<button type="submit" name="action" value="not approved">Not Approved</button>'
            + '<button type="submit" name="action" value="feedback">Send Feedback</button>'
            + '<button type="submit" name="action" value="question">Ask question</button>'
            + '<span role="status" data-image-status></span></div></form></article>'))
        with patch.dict(os.environ, {"USER_ZONE": "Europe/Berlin"}):
            self.assertIn(">Nov 15, 2026, 04:09<", images.card(record, "token", staff.snapshot(), 3))

    def test_delete_removes_the_image_at_once_and_sends_no_message(self) -> None:
        record = self.register()
        kept = self.register("Kept shot")
        body = urlencode({"image": record["id"],
                          "token": images.request_token(images.console_token(self.root), record["id"])})
        self.submit(body)
        self.assertFalse((self.root / image_store.IMAGES_FOLDER / record["id"]).exists())
        self.assertEqual(images.records(self.root), [kept])
        self.assertEqual(self.sent, [])
        with self.assertRaisesRegex(change_page.AnswerRefused, f"no image {record['id']}"):
            self.submit(body)

    def test_delete_refuses_a_wrong_token_or_unknown_image(self) -> None:
        record = self.register()
        token = images.console_token(self.root)
        for body in (urlencode({"image": record["id"], "token": "wrong"}),
                     urlencode({"image": "000000000000", "token": images.request_token(token, "000000000000")}),
                     urlencode({"image": record["id"]}),
                     urlencode({"image": "../x", "token": "t"})):
            with self.subTest(body=body), self.assertRaises(change_page.AnswerRefused):
                self.submit(body)
        self.assertEqual(images.records(self.root), [record])

    def test_an_old_record_with_a_delete_request_reads_and_shows_the_answer_buttons(self) -> None:
        record = self.register()
        folder = self.root / image_store.IMAGES_FOLDER / record["id"]
        stored = cast(dict[str, object], json.loads((folder / image_store.RECORD).read_text()))
        stored["delete_request"] = {"state": "sent", "requested_at": record["registered_at"], "error": ""}
        _ = (folder / image_store.RECORD).write_text(json.dumps(stored))
        self.assertEqual(image_store.read_record(folder), record)
        page = images.render(self.status).decode()
        self.assertIn('<button type="submit" name="action" value="approved">Approved</button>', page)
        self.assertNotIn('value="delete"', page)

    def feedback_body(self, record: image_store.ImageRecord, feedback: str, action: str = "feedback") -> str:
        token = images.request_token(images.console_token(self.root), record["id"])
        return urlencode({"image": record["id"], "token": token, "feedback": feedback, "action": action})

    def test_approved_and_not_approved_send_the_answer_with_feedback_only_when_written(self) -> None:
        for action, first, feedback, lines in (
                ("approved", "your image is approved.", "", ()),
                ("not approved", "your image is not approved.", "  The counts are wrong. ", ("Feedback: The counts are wrong.",))):
            with self.subTest(action=action):
                self.sent.clear()
                record = self.register("After: the panel.")
                self.submit(self.feedback_body(record, feedback, action))
                self.assertEqual([message for _, _, message in self.sent], ["\n".join((
                    f"From the user (via the Images page): {first}", *lines,
                    f"Image: http://127.0.0.1:8765/images/{record['id']}/image.png",
                    "Caption: After: the panel.", "For: Phase console-images",
                    "After you act on it, delete the image: "
                    + f"~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id {record['id']}"))])
                self.assertIn("feedback_at", image_store.read_record(self.root / image_store.IMAGES_FOLDER / record["id"]))
                self.assertNotIn(record["id"], images.render(self.status).decode())

    def test_feedback_goes_to_the_owner_session_and_hides_the_image_but_keeps_it(self) -> None:
        record = self.register("After: the panel. Look at the counts.")
        self.submit(self.feedback_body(record, "  The counts are wrong.  "))
        assert self.owner is not None
        self.assertEqual(len(self.sent), 1)
        recipient, sender, message = self.sent[0]
        self.assertEqual((recipient, sender), (f"session:{self.owner.current_session}", "images"))
        self.assertEqual(message, "\n".join((
            "From the user (via the Images page): feedback on your image.",
            "Feedback: The counts are wrong.",
            f"Image: http://127.0.0.1:8765/images/{record['id']}/image.png",
            "Caption: After: the panel. Look at the counts.",
            "For: Phase console-images",
            "After you act on it, delete the image: "
            + f"~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id {record['id']}")))
        folder = self.root / image_store.IMAGES_FOLDER / record["id"]
        self.assertTrue((folder / "image.png").is_file())
        self.assertIn("feedback_at", image_store.read_record(folder))
        self.assertNotIn(record["id"], images.render(self.status).decode())
        images.delete(self.root, record["id"])
        self.assertEqual(images.records(self.root), [])

    def test_feedback_that_reaches_no_one_keeps_the_image_shown(self) -> None:
        record = self.register()
        with patch.object(change_page, "send_message", side_effect=RuntimeError("session gone")) as send:
            with self.assertRaisesRegex(images.AnswerNotSent, "session gone"):
                self.submit(self.feedback_body(record, "Wrong colors."))
        self.assertEqual(send.call_count, 3)
        self.assertEqual(images.records(self.root), [record])
        self.assertIn(record["id"], images.render(self.status).decode())

    def test_feedback_refuses_empty_text_and_an_unknown_action(self) -> None:
        record = self.register()
        token = images.request_token(images.console_token(self.root), record["id"])
        for body, reason in ((self.feedback_body(record, "   "), "enter feedback"),
                             (urlencode({"image": record["id"], "token": token, "action": "move"}), "use the form")):
            with self.subTest(reason=reason), self.assertRaisesRegex(change_page.AnswerRefused, reason):
                self.submit(body)
        self.assertEqual((images.records(self.root), self.sent), ([record], []))


class ImageQuestionTests(ImagesFixture):
    def ask(self, record: image_store.ImageRecord, words: str = 'Which check passed?') -> None:
        self.submit(urlencode({'image': record['id'],
            'token': images.request_token(images.console_token(self.root), record['id']),
            'action': 'question', 'feedback': words}))

    def test_feedback_completion_keeps_an_answer_received_during_delivery_visible_and_new(self) -> None:
        for action in ('feedback', 'approved', 'not approved'):
            with self.subTest(action=action):
                record = self.register()
                pages = tuple(page for page in console.read_pages() if page.identity == 'images')
                self.assertEqual(console.news(self.root, pages, 'images'), ())
                self.ask(record)
                self.assertEqual(console.news(self.root, pages, 'changes'), ())

                def answer_during_delivery(recipient: str, sender: str, message: str) -> None:
                    images.answer_question(self.root, record['id'], 'Q1', 'The gate passed.')
                    self.capture(recipient, sender, message)

                with patch.object(change_page, 'send_message', side_effect=answer_during_delivery):
                    images.send_answer(self.root, record['id'], action, 'Check the gate.')
                stored = image_store.read_record(images.images_root(self.root) / record['id'])
                self.assertNotIn('feedback_at', stored)
                content = images.render(self.status).decode()
                self.assertIn(record['id'], content)
                self.assertIn('The gate passed.', content)
                self.assertIn(record['id']+':Q1', images.waiting_images(console.NewsRequest(self.root)))
                self.assertEqual(console.news(self.root, pages, 'changes'), ('images',))
                self.assertEqual(console.news(self.root, pages, 'images'), ())

    def test_feedback_started_after_an_answer_still_hides_the_image(self) -> None:
        record = self.register()
        self.ask(record)
        images.answer_question(self.root, record['id'], 'Q1', 'The gate passed.')
        images.send_answer(self.root, record['id'], 'feedback', 'Use a larger caption.')
        stored = image_store.read_record(images.images_root(self.root) / record['id'])
        self.assertIn('feedback_at', stored)
        self.assertNotIn(record['id'], images.render(self.status).decode())
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset())

    def test_an_asked_image_stays_visible_and_its_answer_returns_as_new(self) -> None:
        record = self.register('Check <the> gate')
        pages = tuple(page for page in console.read_pages() if page.identity == 'images')
        self.assertEqual(console.news(self.root, pages, 'images'), ())
        self.ask(record)
        folder = images.images_root(self.root) / record['id']
        stored = image_store.read_record(folder)
        questions = image_store.ImageQuestionStore(images.images_root(self.root)).questions(record['id'])
        self.assertEqual([(question.question_id, question.words, question.answer) for question in questions],
                         [('Q1', 'Which check passed?', None)])
        self.assertTrue((folder / record['file']).is_file())
        card = images.render(self.status).decode()
        self.assertIn('Question Q1 — waiting for an answer', card)
        self.assertIn('Which check passed?', card)
        self.assertIn(record['id'], card)
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset())
        self.assertEqual(console.news(self.root, pages, 'changes'), ())
        assert self.owner is not None
        self.assertEqual(self.sent[0][0], 'session:'+self.owner.current_session)
        self.assertIn(f"Images {record['id']}: Ask question", self.sent[0][2])
        self.assertIn(f"--id {record['id']} --question Q1 --text", self.sent[0][2])
        # An unrelated feedback mark must not prevent the answer from returning.
        stored['feedback_at'] = datetime.now(UTC).isoformat()
        image_store.write_record(folder, stored)
        with redirect_stdout(StringIO()) as output:
            code = images.main(['answer', '--id', record['id'], '--question', 'Q1', '--text', '<Passed> & checked'])
        self.assertEqual(code, 0)
        self.assertIn('answered '+record['id']+' Q1', output.getvalue())
        stored = image_store.read_record(folder)
        self.assertNotIn('feedback_at', stored)
        card = images.render(self.status).decode()
        self.assertIn('Which check passed?', card)
        self.assertIn('&lt;Passed&gt; &amp; checked', card)
        self.assertNotIn('waiting for an answer', card)
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset((record['id']+':Q1',)))
        self.assertEqual(console.news(self.root, pages, 'changes'), ('images',))
        self.assertEqual(console.news(self.root, pages, 'images'), ())
        self.assertEqual(console.news(self.root, pages, 'changes'), ())
        self.assertEqual(len(self.sent), 1)

    def test_repeat_asks_send_once_and_a_later_question_gets_the_next_id(self) -> None:
        record = self.register()
        other = self.register('Second image')
        self.ask(record)
        self.ask(record)
        self.assertEqual(len(self.sent), 1)
        images.answer_question(self.root, record['id'], 'Q1', 'The first check.')
        self.ask(record, 'Which repair worked?')
        self.ask(other)
        store = image_store.ImageQuestionStore(images.images_root(self.root))
        self.assertEqual([question.question_id for question in store.questions(record['id'])], ['Q1', 'Q2'])
        self.assertEqual([question.question_id for question in store.questions(other['id'])], ['Q1'])
        self.assertEqual(len(self.sent), 3)

    def test_cards_show_all_open_questions_before_answered_pairs_in_numeric_order(self) -> None:
        record = self.register()
        for number in range(1, 11):
            self.ask(record, f'Question number {number}?')
        images.answer_question(self.root, record['id'], 'Q2', 'Second answer')
        images.answer_question(self.root, record['id'], 'Q1', 'First answer')
        source = images.render(self.status).decode()
        positions = [(question, source.index(f'data-question="Q{question}"'))
                     for question in (*range(3, 11), 1, 2)]
        self.assertEqual([position for _, position in positions], sorted(position for _, position in positions))
        self.assertEqual(len(self.sent), 10)
        self.assertEqual(len(image_store.ImageQuestionStore(images.images_root(self.root)).questions(record['id'])), 10)
        self.assertIn('First answer', source)
        self.assertIn('Second answer', source)
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset())

    def test_a_failed_question_send_keeps_the_record_and_retries_the_same_question(self) -> None:
        record = self.register()
        with patch.object(change_page, 'send_message', side_effect=RuntimeError('owner offline')) as send:
            self.ask(record)
        self.assertEqual(send.call_count, 4)
        self.assertIn('waiting for an answer', images.render(self.status).decode())
        paths = list(self.root.glob(change_page.DELIVERY_PREFIX+'*'))
        self.assertEqual(len(paths), 1)
        saved = cast(change_page.StoredDeliverySlots, json.loads(paths[0].read_text()))
        assert 'review_message' in saved
        self.assertTrue(saved['review_message']['saved'])
        self.status: change_page.DeliveryStatus = change_page.DeliveryStatus(self.root)
        notice = 'Not sent. Press Ask question again with the same words.'
        self.assertIn(notice, images.render(self.status).decode())
        pending = images.ask_question(self.root, record['id'], 'Which check passed?', self.status)
        self.assertIn(notice, images.render(self.status).decode())
        change_page.deliver(pending, self.status)
        self.assertNotIn(notice, images.render(self.status).decode())
        self.ask(record)
        self.assertEqual(len(self.sent), 1)
        questions = image_store.ImageQuestionStore(images.images_root(self.root)).questions(record['id'])
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].question_id, 'Q1')
        self.assertEqual(images.waiting_records(self.root), [])

    def test_empty_questions_and_unknown_or_answered_ids_preserve_the_image(self) -> None:
        record = self.register()
        with self.assertRaisesRegex(change_page.AnswerRefused, 'Enter a question first'):
            self.ask(record, '  ')
        self.assertEqual((images.records(self.root), self.sent), ([record], []))
        self.ask(record)
        with redirect_stderr(StringIO()) as error:
            self.assertEqual(images.main(['answer', '--id', record['id'], '--question', 'Q9', '--text', 'Wrong']), 1)
        self.assertIn('Unknown question id', error.getvalue())
        images.answer_question(self.root, record['id'], 'Q1', 'First answer')
        before = images.records(self.root)
        with self.assertRaisesRegex(change_page.AnswerRefused, 'already has an answer'):
            images.answer_question(self.root, record['id'], 'Q1', 'Changed answer')
        self.assertEqual(images.records(self.root), before)
        self.assertTrue((images.images_root(self.root) / record['id'] / record['file']).is_file())

    def test_an_older_record_needs_no_question_field_or_migration(self) -> None:
        record = self.register()
        folder = images.images_root(self.root) / record['id']
        original = (folder / image_store.RECORD).read_bytes()
        self.assertEqual(image_store.ImageQuestionStore(images.images_root(self.root)).questions(record['id']), ())
        self.assertIn('Ask question', images.render(self.status).decode())
        self.assertEqual((folder / image_store.RECORD).read_bytes(), original)
        self.assertNotIn('questions', image_store.read_record(folder))

    def test_invalid_question_data_is_not_silently_dropped(self) -> None:
        record = self.register()
        folder = images.images_root(self.root) / record['id']
        fields = cast(dict[str, object], json.loads((folder / image_store.RECORD).read_text()))
        invalid_question: dict[str, object] = {}
        invalid_questions: tuple[object, ...] = (False, [invalid_question])
        for questions in invalid_questions:
            fields['questions'] = questions
            _ = (folder / image_store.RECORD).write_text(json.dumps(fields))
            with self.subTest(questions=questions), self.assertRaises(ValueError):
                _ = image_store.read_record(folder)


    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser questions')
    def test_browser_clears_sending_after_delivery_and_keeps_the_image_for_the_answer(self) -> None:
        probe = r'''
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const pause=()=>new Promise(resolve=>setTimeout(resolve,30));
 const until=async predicate=>{
  for(let attempt=0;attempt<300;attempt++){if(predicate()) return;await pause();}
  throw new Error('The page did not reach the expected state.');
 };
 const card=()=>document.querySelector('[data-image]');
 const ask=()=>card().querySelector('button[value="question"]');
 const notice=id=>card().querySelector(`[data-question="${id}"] [role="status"]`)?.textContent;
 const sending='Question saved. Sending to the owner.';
 try{
  const form=card().querySelector('form');
  form.requestSubmit(ask());
  check(form.querySelector('[data-image-status]').textContent==='Enter a question first.','empty question gives its reason');
  form.elements.feedback.value='Which check passed?';
  form.requestSubmit(ask());
  check(card().isConnected,'the card stays during the request');
  await until(()=>card()?.querySelector('[data-question]'));
  check(card().querySelector('.page-question').textContent.includes('Which check passed?'),'the open question appears');
  check(card().querySelector('.page-question').textContent.includes('waiting for an answer'),'the owner has the question');
  check(notice('Q1')===sending,'a pending delivery shows its sending notice');
  const released=await fetch('/fixture-delivery',{method:'POST'});
  check(released.ok,'the fixture completes delivery');
  await until(()=>!notice('Q1'));
  check(card().querySelector('.page-question').textContent.includes('waiting for an answer'),
        'the open question waits for an answer after delivery');
  check(document.querySelector('[data-images]').dataset.refreshSeconds==='0',
        'the page stops automatic refresh after delivery');
  await fetch('/fixture-answer',{method:'POST'});
  document.dispatchEvent(new Event('visibilitychange'));
  await until(()=>card()?.querySelector('.page-question').textContent.includes('The gate passed.'));
  check(card().querySelector('.page-question').textContent.includes('Which check passed?'),'the answer keeps the question');
  check(!card().querySelector('.page-question').textContent.includes('waiting for an answer'),'the answer clears the open line');
  check(card().querySelector('img')!==null,'the image stays with the answer');
  check(!notice('Q1'),'the answered question has no delivery notice');
  card().querySelector('form').elements.feedback.value='Which check failed?';
  card().querySelector('form').requestSubmit(ask());
  await until(()=>card()?.querySelector('[data-question="Q2"]'));
  const receipt=await fetch('/fixture-failure',{method:'POST'});
  check(receipt.ok,'the fixture records the delivery failure');
  document.dispatchEvent(new Event('visibilitychange'));
  await until(()=>card()?.querySelector('[data-question="Q2"] [role="status"]'));
  check(notice('Q2')==='Not sent. Press Ask question again with the same words.',
        'the delivery failure shows the retry notice');
  check(card().querySelector('[data-image-status]').textContent==='','the form has no conflicting notice');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''
        requests = self.run_question_browser(probe)
        self.assertEqual([(request['action'], request['feedback']) for request in requests],
                         [(['question'], ['Which check passed?']), (['question'], ['Which check failed?'])])
        self.assertEqual(len(self.sent), 2)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser questions')
    def test_browser_refreshes_a_saved_question_after_an_incomplete_success_body(self) -> None:
        probe = r'''
(async()=>{
 const failures=[];
 try{
  const form=()=>document.querySelector('[data-image-form]');
  form().elements.feedback.value='Was the gate checked?';
  form().requestSubmit(form().querySelector('button[value="question"]'));
  for(let attempt=0;attempt<150 && !document.querySelector('[data-question]');attempt++){
   await new Promise(resolve=>setTimeout(resolve,30));
  }
  const question=document.querySelector('[data-question]');
  if(!question?.textContent.includes('Was the gate checked?')) failures.push('The refresh must show the saved question.');
  if(form().querySelector('[data-image-status]').textContent) failures.push('The form must have no error.');
  if(form().elements.feedback.value) failures.push('The saved draft must clear.');
  const released=await fetch('/fixture-delivery',{method:'POST'});
  if(!released.ok) failures.push('The fixture must complete delivery.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''
        requests = self.run_question_browser(probe, response_body=b'{"state":')
        self.assertEqual([(request['action'], request['feedback']) for request in requests],
                         [(['question'], ['Was the gate checked?'])])
        self.assertEqual(len(self.sent), 1)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser questions')
    def test_timed_refresh_keeps_focus_and_selection_on_another_image(self) -> None:
        _ = self.register('Feedback fixture')
        probe = r'''
async (find, until, check) => {
 const question = find('Question fixture').querySelector('form');
 question.elements.feedback.value = 'Is this ready?';
 question.requestSubmit(question.querySelector('button[value="question"]'));
 await until(() => document.querySelector('[data-question]'));
 const draft = () => find('Feedback fixture').querySelector('textarea');
 const old = draft();
 old.value = 'Keep editing this draft';
 old.focus();
 old.setSelectionRange(2, 9, 'backward');
 await until(() => !old.isConnected);
 const fresh = draft();
 check(document.activeElement === fresh, 'The timed refresh keeps focus on the other card.');
 check(fresh.value === 'Keep editing this draft', 'The timed refresh keeps the draft.');
 check(fresh.selectionStart === 2 && fresh.selectionEnd === 9 && fresh.selectionDirection === 'backward',
       'The timed refresh keeps the selection and its direction.');
 fresh.setSelectionRange(4, 4);
 document.dispatchEvent(new Event('visibilitychange'));
 await until(() => !fresh.isConnected);
 check(document.activeElement === draft() && draft().selectionStart === 4 && draft().selectionEnd === 4,
       'The visibility refresh keeps the caret.');
 check((await fetch('/fixture-delivery', {method: 'POST'})).ok, 'The fixture completes delivery.');
}'''
        _ = self.run_question_browser(self.question_probe(probe))

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser questions')
    def test_delivery_result_write_failure_stops_refresh_and_offers_retry(self) -> None:
        probe = r'''
async (find, until, check) => {
 const form = () => find('Question fixture').querySelector('form');
 const ask = () => {
  form().elements.feedback.value = 'Is this ready?';
  form().requestSubmit(form().querySelector('button[value="question"]'));
 };
 const notice = () => document.querySelector('[data-question="Q1"] [role="status"]')?.textContent;
 ask();
 await until(() => notice() === 'Question saved. Sending to the owner.');
 check((await fetch('/fixture-delivery', {method: 'POST'})).ok, 'The fixture interrupts the result write.');
 await until(() => notice() === 'Not sent. Press Ask question again with the same words.');
 check(document.querySelector('[data-images]').dataset.refreshSeconds === '0', 'The abandoned attempt stops the timer.');
 ask();
 await until(() => !notice());
 check(document.querySelectorAll('[data-question]').length === 1, 'The retry preserves the question ID.');
}'''
        requests = self.run_question_browser(self.question_probe(probe), fail_result_write=True)
        self.assertEqual([request['feedback'] for request in requests], [['Is this ready?'], ['Is this ready?']])
        self.assertEqual(len(self.sent), 2)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser questions')
    def test_successful_question_retries_the_first_failed_refresh(self) -> None:
        probe = r'''
async (find, until, check) => {
 const form = () => find('Question fixture').querySelector('form');
 const status = () => form().querySelector('[data-image-status]').textContent;
 form().elements.feedback.value = 'Is this ready?';
 form().requestSubmit(form().querySelector('button[value="question"]'));
 await until(() => status().startsWith('Update failed:'));
 check(!document.querySelector('[data-question]'), 'The first refresh fails before the question appears.');
 await until(() => document.querySelector('[data-question]')?.textContent.includes('Is this ready?'));
 check(status() === '', 'The automatic refresh clears the refresh error.');
 check(form().elements.feedback.value === '', 'The saved draft stays clear.');
 check((await fetch('/fixture-delivery', {method: 'POST'})).ok, 'The fixture completes delivery.');
}'''
        _ = self.run_question_browser(self.question_probe(probe), fail_first_refresh=True)

    @staticmethod
    def question_probe(check_page: str) -> str:
        return r'''
(async () => {
 const failures = [];
 const check = (ok, message) => {if (!ok) failures.push(message);};
 const find = caption => [...document.querySelectorAll('[data-image]')]
  .find(card => card.querySelector('.caption').textContent === caption);
 const until = async predicate => {
  for (let attempt = 0; attempt < 300; attempt++) {
   if (predicate()) return;
   await new Promise(resolve => setTimeout(resolve, 30));
  }
  throw new Error('The page did not reach the expected state.');
 };
 try {await (CHECK_PAGE)(find, until, check);}
 catch (error) {failures.push(String(error));}
 await fetch('/results', {method: 'POST', body: JSON.stringify(failures)});
})();'''.replace('CHECK_PAGE', check_page)

    def run_question_browser(self, probe: str, *, response_body: bytes = b'{"state":"done"}',
                             fail_first_refresh: bool = False, fail_result_write: bool = False
                             ) -> list[dict[str, list[str]]]:
        record = self.register('Question fixture')
        root = self.root
        identity = record['id']
        delivery_status = self.status
        settings = self.temporary / 'config' / 'review_links.conf'
        _ = settings.write_text(settings.read_text().replace('page_refresh_seconds=15', 'page_refresh_seconds=5'))
        results: list[list[str]] = []
        requests: list[dict[str, list[str]]] = []
        complete = threading.Event()
        first_refresh_failed = threading.Event()
        first_delivery_released = threading.Event()
        first_delivery_done = threading.Event()
        second_delivery_done = threading.Event()
        second_deliveries: list[change_page.MessageDelivery] = []
        fixture = self

        class Probe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == '/complete':
                    _ = complete.wait(20)
                    self.send_response(204)
                    self.end_headers()
                    return
                if (self.path == images.PAGE_PATH and fail_first_refresh and requests
                        and not first_refresh_failed.is_set()):
                    first_refresh_failed.set()
                    self.send_error(503, 'The fixture cannot read the page.')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'image/png' if self.path.endswith('.png') else 'text/html; charset=utf-8')
                self.end_headers()
                if self.path.endswith('.png'):
                    _ = self.wfile.write(PNG)
                elif self.path == images.PAGE_PATH:
                    page = images.render(delivery_status).decode()
                    _ = self.wfile.write(page.replace('</body>', f'<script>{probe}</script></body>').encode())
                else:
                    _ = self.wfile.write(('<iframe src="'+images.PAGE_PATH+'" width="1100" height="800"></iframe>'
                                          +'<img hidden src="/complete">').encode())

            def do_POST(self) -> None:
                payload = self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode()
                delivery: change_page.MessageDelivery | change_page.NoMessageDelivery = change_page.NoMessageDelivery()
                body = b'{"state":"done"}'
                is_first_delivery = False
                if self.path == '/results':
                    results.append(cast(list[str], json.loads(payload)))
                    complete.set()
                elif self.path == '/fixture-answer':
                    images.answer_question(root, identity, 'Q1', 'The gate passed.')
                elif self.path == '/fixture-delivery':
                    first_delivery_released.set()
                    if not first_delivery_done.wait(5):
                        self.send_error(504, 'The fixture delivery did not finish.')
                        return
                elif self.path == '/fixture-failure':
                    if not second_delivery_done.wait(5):
                        self.send_error(504, 'The fixture delivery did not finish.')
                        return
                    second = second_deliveries[0]
                    with delivery_status.locked():
                        delivery_status.record_result_locked(second, change_page.FailedDelivery(
                            second.attempt, second.page.review_list, second.row, 'The owner is unavailable.'))
                else:
                    requests.append(parse_qs(payload))
                    delivery = images.submit(root, payload, delivery_status)
                    if isinstance(delivery, change_page.MessageDelivery):
                        body = response_body
                        is_first_delivery = len(requests) == 1
                        if delivery.row.endswith('/Q2'):
                            second_deliveries.append(delivery)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                try:
                    _ = self.wfile.write(body)
                    self.wfile.flush()
                finally:
                    if is_first_delivery:
                        if first_delivery_released.wait(15):
                            if fail_result_write:
                                with patch.object(delivery_status, 'record_result_locked',
                                                  side_effect=OSError('Cannot write the delivery result.')):
                                    with fixture.assertRaisesRegex(OSError, 'Cannot write the delivery result'):
                                        change_page.deliver(delivery, delivery_status)
                            else:
                                change_page.deliver(delivery, delivery_status)
                            first_delivery_done.set()
                    else:
                        change_page.deliver(delivery, delivery_status)
                    if isinstance(delivery, change_page.MessageDelivery) and delivery.row.endswith('/Q2'):
                        second_delivery_done.set()

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Probe) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                profile = self.temporary / 'question-browser-profile'
                profile.mkdir()
                done = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', str(profile),
                    '--window-size', '1200,900', '--screenshot', str(self.temporary/'question-browser.png'),
                    f'http://127.0.0.1:{server.server_port}/'], capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr)
            finally:
                first_delivery_released.set()
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)
        self.assertEqual(results, [[]])
        return requests


class ImagePairTests(ImagesFixture):
    def register_pair(self, caption: str = "The panel. Look at the counts.") -> image_store.ImageRecord:
        after = self.temporary / "after.webp"
        _ = after.write_bytes(PNG[::-1])
        return images.register(after, caption, self.owner_id(), "Phase image-pairs", before=self.shot)

    def test_a_pair_is_one_record_in_one_folder_and_one_delete_removes_both(self) -> None:
        record = self.register_pair()
        folder = self.root / image_store.IMAGES_FOLDER / record["id"]
        self.assertEqual(sorted(path.name for path in folder.iterdir()), [image_store.RECORD, "after.webp", "before.png"])
        self.assertEqual(((folder / "before.png").read_bytes(), (folder / "after.webp").read_bytes()), (PNG, PNG[::-1]))
        self.assertEqual((record["file"], record.get("before")), ("after.webp", "before.png"))
        self.assertEqual(images.records(self.root), [record])
        images.delete(self.root, record["id"])
        self.assertFalse(folder.exists())

    def test_a_failed_pair_copy_removes_the_draft_and_publishes_no_record(self) -> None:
        after = self.temporary / "after.png"
        _ = after.write_bytes(PNG)
        copy = shutil.copyfile

        def copy_image(source: Path, target: Path) -> str:
            if source == after:
                raise OSError("the after image cannot be copied")
            return str(copy(source, target))

        with patch("scripts.review_links.image_store.shutil.copyfile", side_effect=copy_image), \
                self.assertRaisesRegex(OSError, "the after image cannot be copied"):
            _ = images.register(after, "caption", self.owner_id(), "Phase image-pairs", before=self.shot)
        self.assertEqual(list((self.root / image_store.IMAGES_FOLDER).iterdir()), [])
        self.assertEqual(images.records(self.root), [])

    def test_a_pair_with_a_bad_before_image_leaves_no_folder(self) -> None:
        text = self.temporary / "notes.txt"
        _ = text.write_text("not an image")
        for before, reason in ((text, "use one of"), (self.temporary / "absent.png", "no such image file")):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                _ = images.register(self.shot, "caption", self.owner_id(), "Phase image-pairs", before=before)
        self.assertFalse((self.root / image_store.IMAGES_FOLDER).exists())

    def test_a_record_whose_before_is_not_text_stays_off_the_page(self) -> None:
        record = self.register_pair()
        folder = self.root / image_store.IMAGES_FOLDER / record["id"]
        stored = cast(dict[str, object], json.loads((folder / image_store.RECORD).read_text()))
        stored["before"] = 7
        _ = (folder / image_store.RECORD).write_text(json.dumps(stored))
        with redirect_stderr(StringIO()) as errors:
            self.assertEqual(images.records(self.root), [])
        self.assertIn("invalid image record", errors.getvalue())

    def test_command_files_a_pair_and_refuses_a_half_pair(self) -> None:
        after = self.temporary / "after.png"
        _ = after.write_bytes(PNG)
        output = StringIO()
        with redirect_stdout(output):
            code = images.main(["register", "--before", str(self.shot), "--after", str(after), "--caption", "Cards",
                                "--owner", self.owner_id(), "--phase", "image-pairs"])
        self.assertEqual(code, 0)
        record = images.records(self.root)[0]
        origin = f"http://127.0.0.1:8765/images/{record['id']}"
        self.assertEqual(output.getvalue().splitlines(), [
            f"registered {record['id']}: {origin}/before.png {origin}/after.png",
            "Image link: http://127.0.0.1:8765/console/?page=images&at.images="
            + f"%2Fconsole%2Fimages.html%23image%3D{record['id']}"])
        for options in (["--before", str(self.shot)], ["--image", str(self.shot), "--after", str(after)], []):
            with self.subTest(options=options), redirect_stderr(StringIO()) as errors, \
                    self.assertRaises(SystemExit):
                _ = images.main(["register", *options, "--caption", "Cards", "--owner", self.owner_id(),
                                 "--phase", "image-pairs"])
            self.assertIn("give --image, or --before and --after", errors.getvalue())
        self.assertEqual(images.records(self.root), [record])

    def test_a_pair_card_shows_before_then_after_with_one_form(self) -> None:
        record = self.register_pair("Look at <the> counts")
        card = images.card(record, "token", staff.snapshot(), 3)
        before, after = (f"/images/{record['id']}/before.png", f"/images/{record['id']}/after.webp")
        self.assertTrue(card.startswith(f'<article id="image-{record['id']}" class="pair" data-image='))
        self.assertIn(
            '<div class="image-pair">'
            + f'<figure><figcaption>Before</figcaption><a data-image-zoom href="{before}" target="_blank" rel="noopener">'
            + f'<img src="{before}" alt="Before: Look at &lt;the&gt; counts" loading="lazy"></a></figure>'
            + f'<figure><figcaption>After</figcaption><a data-image-zoom href="{after}" target="_blank" rel="noopener">'
            + f'<img src="{after}" alt="After: Look at &lt;the&gt; counts" loading="lazy"></a></figure></div>'
            + '<form method="post" data-image-form>', card)
        self.assertEqual((card.count("<form"), card.count('value="approved"'), card.count('value="feedback"'),
                          card.count("<h2"), card.count("<dl")), (1, 1, 1, 1, 1))
        page = images.render(self.status).decode()
        self.assertEqual(page.count(page_frame.ZOOM_STYLE), 1)
        self.assertEqual(page.count(page_frame.ZOOM_SCRIPT), 1)
        self.assertNotIn("<figure>", images.card(self.register(), "token", staff.snapshot(), 3))
        # A narrow card stacks the pair, before on top.
        self.assertIn("@container (max-width:599px){.image-pair{grid-template-columns:minmax(0,1fr)}}", images.STYLE)

    def test_pair_feedback_names_the_before_and_after_images(self) -> None:
        record = self.register_pair()
        token = images.request_token(images.console_token(self.root), record["id"])
        self.submit(urlencode({"image": record["id"], "token": token, "feedback": "Good.",
                                            "action": "feedback"}))
        lines = self.sent[0][2].splitlines()
        self.assertEqual(lines[1:4], ["Feedback: Good.",
                                      f"Before: http://127.0.0.1:8765/images/{record['id']}/before.png",
                                      f"After: http://127.0.0.1:8765/images/{record['id']}/after.webp"])
        self.assertNotIn(record["id"], images.render(self.status).decode())


class ImagesServerTests(ImagesFixture):
    port: int = 0

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(serve, "tailscale_ip", return_value="127.0.0.1"))
        with patch.object(change_page, "recover_pending"):
            server = serve.ReviewLinksServer(("127.0.0.1", 0), self.root)
        self.port = server.server_port
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, TEST_HANG_GUARD_SECS)
        self.addCleanup(server.shutdown)

    def http(self, method: str, path: str, body: str = "") -> tuple[int, str, bytes]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            if method == "POST":
                connection.request("POST", path, body, {"Content-Type": "application/x-www-form-urlencoded"})
            else:
                connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type") or "", response.read()
        finally:
            connection.close()

    def test_server_shows_the_page_serves_the_image_and_hides_the_record(self) -> None:
        record = self.register()
        status, kind, body = self.http("GET", images.PAGE_PATH)
        self.assertEqual((status, kind), (200, "text/html; charset=utf-8"))
        self.assertIn(record["id"], body.decode())
        status, kind, body = self.http("GET", images.image_path(record))
        self.assertEqual((status, kind, body), (200, "image/png", PNG))
        status, _, _ = self.http("GET", f"/{image_store.IMAGES_FOLDER}/{record['id']}/{image_store.RECORD}")
        self.assertEqual(status, 404)

    def test_server_serves_both_images_of_a_pair(self) -> None:
        after = self.temporary / "after.png"
        _ = after.write_bytes(PNG)
        record = images.register(after, "Cards", self.owner_id(), "Phase image-pairs", before=self.shot)
        for file in ("before.png", "after.png"):
            with self.subTest(file=file):
                self.assertEqual(self.http("GET", images.image_path(record, file)), (200, "image/png", PNG))

    def test_server_deletes_the_image_and_refuses_a_bad_token(self) -> None:
        record = self.register()
        status, _, body = self.http("POST", images.PAGE_PATH, urlencode({"image": record["id"], "token": "wrong"}))
        self.assertEqual(status, 400)
        self.assertEqual(cast(dict[str, str], json.loads(body)), {"reason": "the image token does not match this page"})
        token = images.request_token(images.console_token(self.root), record["id"])
        status, _, body = self.http("POST", images.PAGE_PATH, urlencode({"image": record["id"], "token": token}))
        self.assertEqual((status, cast(dict[str, str], json.loads(body))), (200, {"state": "done"}))
        self.assertEqual(images.records(self.root), [])
        self.assertEqual(self.sent, [])
        status, _, _ = self.http("GET", images.image_path(record))
        self.assertEqual(status, 404)

    def test_server_sends_feedback_and_answers_503_when_it_reaches_no_one(self) -> None:
        record = self.register()
        token = images.request_token(images.console_token(self.root), record["id"])
        body = urlencode({"image": record["id"], "token": token, "feedback": "Too dark.", "action": "feedback"})
        with patch.object(change_page, "send_message", side_effect=RuntimeError("session gone")):
            status, _, answer = self.http("POST", images.PAGE_PATH, body)
        self.assertEqual((status, cast(dict[str, str], json.loads(answer))),
                         (503, {"reason": "the owner did not get the message (session gone). Try again."}))
        status, _, answer = self.http("POST", images.PAGE_PATH, body)
        self.assertEqual((status, cast(dict[str, str], json.loads(answer))), (200, {"state": "done"}))
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn(record["id"], self.http("GET", images.PAGE_PATH)[2].decode())

    def test_sweep_keeps_old_images(self) -> None:
        record = self.register()
        folder = self.root / image_store.IMAGES_FOLDER
        os.utime(folder, (0, 0))
        _ = serve.sweep_snapshots(self.root, datetime.now(UTC).timestamp())
        self.assertTrue((folder / record["id"] / "image.png").is_file())


if __name__ == "__main__":
    _ = unittest.main()
