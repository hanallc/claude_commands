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
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(configuration), "REVIEW_LINKS_ROOT": str(self.root), "REVIEW_LINKS_HOST": "127.0.0.1",
            "STAFF_FILE": str(self.temporary / "staff.json"), "USER_ZONE": "America/New_York"})))
        runner = fixture_member("showrunner", name="shot-runner")
        self.owner = fixture_member("unit", name="shot-owner", parent=runner.id)
        self.shot = self.temporary / "before.png"
        _ = self.shot.write_bytes(PNG)
        self.sent = []
        _ = self.enterContext(patch.object(change_page, "send_message", side_effect=self.capture))

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
        body = images.render(self.root).decode()
        self.assertNotIn(answered["id"], body)
        self.assertNotIn(change, body)
        waiting = self.register("Waiting image")
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset((waiting["id"],)))
        self.assertIn(waiting["id"], images.render(self.root).decode())
        self.assertEqual(console.news(self.root, pages, "changes"), ("images",))


class ChangeImageTests(ImagesFixture):
    def test_change_images_do_not_appear_on_the_images_page(self) -> None:
        identity = image_store.register_change(self.shot)
        self.assertEqual(images.records(self.root), [])
        self.assertNotIn(identity, images.render(self.root).decode())
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
            shot = next(value.split("=", 1)[1] for value in arguments if value.startswith("--screenshot="))
            _ = Path(shot).write_bytes(PNG)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch("scripts.review_links.image_store.subprocess.run", side_effect=chrome) as run:
            identity = image_store.register_output(output)
        self.assertTrue(image_store.change_image(self.root, identity))
        arguments = cast(list[str], run.call_args.args[0])
        self.assertFalse(Path(arguments[-1].removeprefix("file://")).parent.exists())


class ImagesTests(ImagesFixture):
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
        source = images.render(self.root).decode()
        self.assertEqual(source.count(page_frame.ZOOM_STYLE), 1)
        self.assertEqual(source.count(page_frame.ZOOM_SCRIPT), 1)
        self.assertEqual(source.count(page_frame.MOTION_SCRIPT), 1)
        self.assertIn("prefers-reduced-motion: reduce", source)
        self.assertLess(source.index(older["id"]), source.index(newer["id"]))
        self.assertIn('<nav data-image-groups aria-label="Image groups"></nav>', source)
        self.assertIn("last.owner === card.dataset.owner", source)
        self.assertIn("link.href = '#' + row.card.id;", source)
        self.assertIn("document.getElementById('image-' + start.get('image'))", source)
        self.assertIn("if (target.hidden) {", source)
        self.assertIn("Newer shot", source)
        self.assertIn("shot-owner (unit)", source)
        self.assertIn("Phase console-images", source)
        self.assertIn(f'src="/images/{newer['id']}/image.png"', source)
        token = images.request_token(images.console_token(self.root), newer["id"])
        self.assertIn(f'name="token" value="{token}"', source)
        self.assertIn('<option value="week" selected>', source)
        self.assertIn("card.remove();", source)
        self.assertIn("home.insertBefore(card, next?.parentNode === home ? next : null);", source)
        self.assertIn("status.textContent = 'Not sent: ' + error.message;", source)
        self.assertIn("new FormData(form, event.submitter)", source)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_updates_keep_zoom_and_feedback_working_after_an_older_script_reloads(self) -> None:
        _ = self.register()
        current = images.render(self.root).decode()
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
 const card = list().querySelector('article'), form = card.querySelector('form');
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
 form.elements.feedback.value = 'Too dark.';
 form.requestSubmit(form.querySelector('button[value="feedback"]'));
 for (let step = 0; step < 100 && card.isConnected; step++) await wait(20);
 check(!card.isConnected, 'Send Feedback removes the card from the page');
 await wait(200);
 check(list() !== null && location.pathname === '""" + images.PAGE_PATH + """', 'the page stays after Send Feedback');
 await report(failures);
})().catch(error => fetch('/results', {method:'POST', body:JSON.stringify([String(error)])}));
</script>"""
        gets: list[str] = []
        posts: list[dict[str, list[str]]] = []
        results: list[list[str]] = []
        probe_complete = threading.Event()

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
                # The first load is a page from an older script; every later answer is the current page.
                page = current.replace(marker, 'data-script="older-script"') if len(gets) == 1 else current
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
                posts.append(parse_qs(payload))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                _ = self.wfile.write(b'{"state": "done"}')

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
        self.assertEqual(len(gets), 4, "load, update, reload, update")
        self.assertEqual([(post["action"], post["feedback"]) for post in posts], [(["feedback"], ["Too dark."])])

    def test_card_shows_the_caption_details_image_feedback_field_and_the_answer_buttons(self) -> None:
        record = self.register("Look at <the> cards")
        record["registered_at"] = "2026-11-15T03:09:00+00:00"
        image_store.write_record(self.root / image_store.IMAGES_FOLDER / record["id"], record)
        card = images.card(record, "token", staff.snapshot(), 3)
        path = f"/images/{record['id']}/image.png"
        token = images.request_token("token", record["id"])
        self.assertEqual(card, (
            f'<article id="image-{record['id']}" data-image="{record['id']}" data-registered="1794712140"'
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
            + '<span role="status" data-image-status></span></div></form></article>'))
        with patch.dict(os.environ, {"USER_ZONE": "Europe/Berlin"}):
            self.assertIn(">Nov 15, 2026, 04:09<", images.card(record, "token", staff.snapshot(), 3))

    def test_delete_removes_the_image_at_once_and_sends_no_message(self) -> None:
        record = self.register()
        kept = self.register("Kept shot")
        body = urlencode({"image": record["id"],
                          "token": images.request_token(images.console_token(self.root), record["id"])})
        images.submit(self.root, body)
        self.assertFalse((self.root / image_store.IMAGES_FOLDER / record["id"]).exists())
        self.assertEqual(images.records(self.root), [kept])
        self.assertEqual(self.sent, [])
        with self.assertRaisesRegex(change_page.AnswerRefused, f"no image {record['id']}"):
            images.submit(self.root, body)

    def test_delete_refuses_a_wrong_token_or_unknown_image(self) -> None:
        record = self.register()
        token = images.console_token(self.root)
        for body in (urlencode({"image": record["id"], "token": "wrong"}),
                     urlencode({"image": "000000000000", "token": images.request_token(token, "000000000000")}),
                     urlencode({"image": record["id"]}),
                     urlencode({"image": "../x", "token": "t"})):
            with self.subTest(body=body), self.assertRaises(change_page.AnswerRefused):
                images.submit(self.root, body)
        self.assertEqual(images.records(self.root), [record])

    def test_an_old_record_with_a_delete_request_reads_and_shows_the_answer_buttons(self) -> None:
        record = self.register()
        folder = self.root / image_store.IMAGES_FOLDER / record["id"]
        stored = cast(dict[str, object], json.loads((folder / image_store.RECORD).read_text()))
        stored["delete_request"] = {"state": "sent", "requested_at": record["registered_at"], "error": ""}
        _ = (folder / image_store.RECORD).write_text(json.dumps(stored))
        self.assertEqual(image_store.read_record(folder), record)
        page = images.render(self.root).decode()
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
                images.submit(self.root, self.feedback_body(record, feedback, action))
                self.assertEqual([message for _, _, message in self.sent], ["\n".join((
                    f"From the user (via the Images page): {first}", *lines,
                    f"Image: http://127.0.0.1:8765/images/{record['id']}/image.png",
                    "Caption: After: the panel.", "For: Phase console-images",
                    "After you act on it, delete the image: "
                    + f"~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id {record['id']}"))])
                self.assertIn("feedback_at", image_store.read_record(self.root / image_store.IMAGES_FOLDER / record["id"]))
                self.assertNotIn(record["id"], images.render(self.root).decode())

    def test_feedback_goes_to_the_owner_session_and_hides_the_image_but_keeps_it(self) -> None:
        record = self.register("After: the panel. Look at the counts.")
        images.submit(self.root, self.feedback_body(record, "  The counts are wrong.  "))
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
        self.assertNotIn(record["id"], images.render(self.root).decode())
        images.delete(self.root, record["id"])
        self.assertEqual(images.records(self.root), [])

    def test_feedback_that_reaches_no_one_keeps_the_image_shown(self) -> None:
        record = self.register()
        with patch.object(change_page, "send_message", side_effect=RuntimeError("session gone")) as send:
            with self.assertRaisesRegex(images.AnswerNotSent, "session gone"):
                images.submit(self.root, self.feedback_body(record, "Wrong colors."))
        self.assertEqual(send.call_count, 3)
        self.assertEqual(images.records(self.root), [record])
        self.assertIn(record["id"], images.render(self.root).decode())

    def test_feedback_refuses_empty_text_and_an_unknown_action(self) -> None:
        record = self.register()
        token = images.request_token(images.console_token(self.root), record["id"])
        for body, reason in ((self.feedback_body(record, "   "), "enter feedback"),
                             (urlencode({"image": record["id"], "token": token, "action": "move"}), "use the form")):
            with self.subTest(reason=reason), self.assertRaisesRegex(change_page.AnswerRefused, reason):
                images.submit(self.root, body)
        self.assertEqual((images.records(self.root), self.sent), ([record], []))


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
        page = images.render(self.root).decode()
        self.assertEqual(page.count(page_frame.ZOOM_STYLE), 1)
        self.assertEqual(page.count(page_frame.ZOOM_SCRIPT), 1)
        self.assertNotIn("<figure>", images.card(self.register(), "token", staff.snapshot(), 3))
        # A narrow card stacks the pair, before on top.
        self.assertIn("@container (max-width:599px){.image-pair{grid-template-columns:minmax(0,1fr)}}", images.STYLE)

    def test_pair_feedback_names_the_before_and_after_images(self) -> None:
        record = self.register_pair()
        token = images.request_token(images.console_token(self.root), record["id"])
        images.submit(self.root, urlencode({"image": record["id"], "token": token, "feedback": "Good.",
                                            "action": "feedback"}))
        lines = self.sent[0][2].splitlines()
        self.assertEqual(lines[1:4], ["Feedback: Good.",
                                      f"Before: http://127.0.0.1:8765/images/{record['id']}/before.png",
                                      f"After: http://127.0.0.1:8765/images/{record['id']}/after.webp"])
        self.assertNotIn(record["id"], images.render(self.root).decode())


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
