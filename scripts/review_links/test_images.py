"""Test image registration, the Images page, deletes and feedback in private folders."""
from __future__ import annotations

import hashlib
import html
from inspect import signature
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable, Iterator
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import ClassVar, Literal, cast, override
from unittest.mock import call, patch
from urllib.parse import parse_qs, urlencode

from scripts.console_text.test_support import accepting_console_text
from scripts.review_links.browser_test_support import assert_status_states, firefox_probe_results
from scripts.review_links.test_staff_names import without_role_colors
from scripts.lib import atomic_file, conf, file_lock, process_start
from scripts.lib.process_group import run_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import send_result
from scripts.notify import review
from scripts.production import behavior_request, delivery_state, staff, the_list, unrouted_alert
from scripts.review_links.test_page_question import FakeQuestionTransport, question_outcome
from scripts.production.staff_test_support import fixture_member
from scripts.review_links import change_page, console, image_store, images, list_page, page_frame, serve
from scripts.review_links import question_record, send_hold
from scripts.review_links.server_test_support import started_review_server

SEND_MESSAGE = change_page.send_message
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
        _ = write_config_fixture(configuration / "change_review.conf")
        _ = write_config_fixture(configuration / "console.conf",
            "page.images.url=python:scripts.review_links.images:page_url\n"
            + "page.images.items=python:scripts.review_links.images:waiting_images\n")
        settings = configuration / "review_links.conf"
        _ = write_config_fixture(
            settings, "delivery_pause_seconds=0\ndelivery_tries=3\nserver_port=8765\nmax_chars.image_title=80\n")
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
        self.enterContext(accepting_console_text())
        _ = self.enterContext(patch.object(change_page, "send_message", side_effect=self.capture))
        _ = self.enterContext(patch.object(change_page, "send_question", side_effect=self.capture_question))

    def submit(self, body: str) -> None:
        delivery = images.submit(self.root, body, self.status)
        if not isinstance(delivery, images.ImageAnswerSent):
            change_page.deliver(delivery, self.status)

    def capture(self, recipient: str, sender: str, message: str, **_delivery: str) -> None:
        self.sent.append((recipient, sender, message))

    def capture_question(self, recipient: staff.StaffId, message: str, _message_id: str
                         ) -> tuple[send_result.ConfirmedSendOutcome, str]:
        owner = staff.snapshot().by_id(recipient)
        assert owner is not None
        self.capture(f"session:{owner.current_session}", change_page.SENDER, message)
        return question_outcome()

    def owner_id(self) -> staff.StaffId:
        assert self.owner is not None
        return self.owner.id

    def register(self, title: str = "The Images navigation",
                 *, look_for: tuple[str, ...] = ()) -> image_store.ImageRecord:
        return images.register(self.shot, title, self.owner_id(), "Phase console-images", look_for=look_for)

    def list_item(self) -> tuple[staff.StaffRow, staff.StaffRow, the_list.ListItem]:
        self.enterContext(patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.temporary / "state")}))
        keeper = fixture_member("producer", name="morty")
        producer = fixture_member("producer", name="shot-producer")
        item = the_list.add(keeper.id, "Check the picture", owner=keeper.id, source="nate",
                            changes={"producer": producer.id, "state": the_list.ItemState.WAITING_FOR_NATE})
        return keeper, producer, item

    def run_firefox(self, handler: type[BaseHTTPRequestHandler], name: str, *, timeout: int = 45,
                    release: Callable[[], None] = lambda: None) -> None:
        """Serve one headless Firefox screenshot load; release frees held requests before the server stops."""
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            # Join the handlers before the fixture folder is deleted.
            server.daemon_threads = False
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                profile = self.temporary / f"{name}-profile"
                profile.mkdir()
                run = run_process_group(["firefox", "--headless", "--no-remote", "--profile", str(profile),
                                         "--window-size", "1200,900", "--screenshot", str(self.temporary / f"{name}.png"),
                                         f"http://127.0.0.1:{server.server_port}/"],
                                        timeout=timeout)
                self.assertEqual(run.returncode, 0, run.stderr)
            finally:
                release()
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)


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

        with patch("scripts.review_links.image_store.run_process_group", side_effect=chrome) as run:
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

                with patch("scripts.review_links.image_store.run_process_group", side_effect=chrome) as run:
                    identity = image_store.register_output(output)
                self.assertEqual(run.call_count, 2)
                self.assertTrue(image_store.change_image(self.root, identity))


class ImagesTests(ImagesFixture):
    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_selecting_an_image_at_viewport_top_clears_the_frozen_header(self) -> None:
        _ = self.register()
        owner = self.owner
        assert owner is not None
        page = page_frame.with_owner(images.render(self.status), "images", owner,
                                     staff.StaffSnapshot((owner,)), lambda: "")
        probe = b'''<script>
(async()=>{
 const failures=[];
 try{
  frameElement.style.width='1024px';
  frameElement.style.height='800px';
  const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  document.querySelector('[data-image-layout]').style.minHeight='2400px';
  const target=document.querySelector('[data-images] article');
  const header=document.querySelector('.page-top');
  await painted();
  scrollTo({top:target.getBoundingClientRect().top+scrollY,behavior:'instant'});
  await painted();
  if(Math.abs(target.getBoundingClientRect().top)>=1 || header.getBoundingClientRect().bottom<30 ||
     matchMedia('(prefers-reduced-motion: reduce)').matches){
   throw new Error('The image must start at viewport y=0 under the header with motion enabled.');
  }
  const moved=new Promise(resolve=>{
   const done=()=>{clearTimeout(timer);removeEventListener('scrollend',done);resolve();};
   const timer=setTimeout(done,1500);
   addEventListener('scrollend',done,{once:true});
  });
  document.querySelector('[data-image-groups] a').click();
  await moved;
  await painted();
  const gap=target.getBoundingClientRect().top-header.getBoundingClientRect().bottom;
  if(gap< -1 || gap>5) failures.push('The selected image must clear the header. Gap: '+gap+' px.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        self.assertEqual(firefox_probe_results(page.replace(b'</body>', probe + b'</body>')), [[]])

    def test_staff_image_owners_have_role_colors_and_unknown_owners_stay_plain(self) -> None:
        _ = (self.temporary / "config/statusline.conf").write_text("role.unit-director=#ffaf02\n")
        record = self.register()
        body = images.render(self.status).decode()
        self.assertRegex(body, r'data-color-text="role.unit-director"[^>]*>shot-owner</span>')
        self.assertRegex(body, r'\(<span[^>]*data-color-text="role.unit-director" style="color:#ffaf02">unit</span>\)</dd>')
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
        cases = ((text, "Cards", self.owner_id(), "use one of"),
                 (self.temporary / "absent.png", "Cards", self.owner_id(), "no such image file"),
                 (self.shot, "  ", self.owner_id(), "one nonempty line"),
                 (self.shot, "Cards\nPanel", self.owner_id(), "one nonempty line"),
                 (self.shot, "Cards", staff.StaffId("absent-owner"), "no staff record"))
        for image, title, owner, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                _ = images.register(image, title, owner, "Change sdlc/C1")
        with self.assertRaisesRegex(ValueError, "Each --look-for value must be one nonempty line."):
            _ = self.register(look_for=("First line\nSecond line",))
        self.assertEqual(list((self.root / image_store.IMAGES_FOLDER).glob("*")) if (self.root / image_store.IMAGES_FOLDER).exists() else [], [])

    def test_register_refuses_a_caption_and_stores_the_revision(self) -> None:
        command = ["register", "--image", str(self.shot), "--owner", self.owner_id(), "--change", "sdlc/C9"]
        errors = StringIO()
        with redirect_stderr(errors):
            self.assertEqual(images.main([*command, "--title", "Read /tmp/proof.png."]), 1)
        self.assertIn("Revise the image caption draft. omit-file-path: /tmp/proof.png", errors.getvalue())
        self.assertEqual(images.records(self.root), [])

        caption = "The larger cards show both labels."
        with redirect_stdout(StringIO()):
            self.assertEqual(images.main([*command, "--title", caption]), 0)
        self.assertEqual([record["title"] for record in images.records(self.root)], [caption])

    def test_caption_reader_gets_each_image_layout(self) -> None:
        caption = "Here is the result."
        after = self.temporary / "after.png"
        _ = after.write_bytes(PNG)
        failures = (review.RuleFailure("console-standard-11", caption),
                    review.RuleFailure("console-standard-12", caption))
        key = review.QuestionKey("image caption", caption)
        with patch.object(review, "review_question", return_value=review.QuestionAccepted(caption)) as reader:
            _ = self.register(caption)
            reader.assert_called_once_with(key, caption, console_kind="image caption", caption_layout="single picture")
            reader.reset_mock()
            _ = images.register(after, caption, self.owner_id(), "Change sdlc/C9", before=self.shot)
            reader.assert_called_once_with(key, caption, console_kind="image caption", caption_layout="before and after pair")

        before = images.records(self.root)
        command = ["register", "--image", str(self.shot), "--two-halves", "--owner", self.owner_id(),
                   "--change", "sdlc/C9", "--title", caption]
        errors = StringIO()
        with (patch.object(review, "review_question",
                           return_value=review.QuestionReviewFailed(caption, (), failures)) as reader,
              redirect_stderr(errors)):
            self.assertEqual(images.main(command), 1)
        self.assertRegex(errors.getvalue(), "console-standard-11: .*console-standard-12:")
        reader.assert_called_once_with(key, caption, console_kind="image caption", caption_layout="picture with two halves")
        self.assertEqual(images.records(self.root), before)

    def test_register_reports_the_limit_and_length_of_a_long_title(self) -> None:
        errors = StringIO()
        with redirect_stderr(errors):
            code = images.main(["register", "--image", str(self.shot), "--title", "A" * 81,
                                "--owner", self.owner_id(), "--phase", "image-card"])
        self.assertEqual(code, 1)
        self.assertEqual(errors.getvalue(),
                         "images: --title has 81 characters. The limit is 80. Put the details in --look-for lines.\n")
        self.assertFalse(images.images_root(self.root).exists())

    def test_command_stores_the_title_and_look_for_lines_prints_links_and_deletes(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = images.main(["register", "--image", str(self.shot), "--title", "List cards",
                                "--look-for", "The count above the table", "--look-for", " The owner label ",
                                "--owner", self.owner_id(), "--change", "sdlc/C9"])
        self.assertEqual(code, 0)
        record = images.records(self.root)[0]
        stored = cast(dict[str, object], json.loads(
            (images.images_root(self.root) / record["id"] / image_store.RECORD).read_text()))
        self.assertEqual(stored["title"], "List cards")
        self.assertEqual(stored["look_for"], ["The count above the table", "The owner label"])
        self.assertEqual(stored["caption"], "List cards\nLook for:\n- The count above the table\n- The owner label")
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
        settings = self.temporary / "config/review_links.conf"
        _ = write_config_fixture(settings, "card_fade_out_ms=365\ncard_fade_in_ms=725\n")
        with patch.object(conf, "read", wraps=conf.read) as read:
            source = images.render(self.status).decode()
        self.assertEqual(sum(call.args == ("review_links",) for call in read.call_args_list), 1)
        self.assertEqual(source.count(page_frame.BUTTON_STATES), 1)
        self.assertEqual(source.count(page_frame.ZOOM_STYLE), 1)
        self.assertEqual(source.count(page_frame.ZOOM_SCRIPT), 1)
        self.assertEqual(source.count(page_frame.MOTION_SCRIPT), 1)
        self.assertEqual(source.count(page_frame.REFRESH_SCRIPT), 1)
        self.assertIn('data-refresh-seconds="0"', source)
        self.assertIn('data-card-fade-out-ms="365" data-card-fade-in-ms="725"', source)
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
        self.assertIn("await motion.removeCard(card, apply);", source)
        self.assertIn("const undone = error instanceof SendUndone;", source)
        self.assertIn("showActionFailure(status, message);", source)
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
 const actions = [['approved', ''], ['feedback', 'Too dark.'],
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
  if (words !== 'Please retry.') {
   const status = list().querySelector('[data-image-status]');
   check(status.hidden && !status.textContent, action + ' leaves no action status.');
  }
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
 const departingForm = previous.querySelector('form');
 const feedbackButton = departingForm.querySelector('button[value="feedback"]');
 submitFeedback('Routine feedback.');
 const departingCard = departingForm.closest('article');
 let fade;
 for (let step = 0; step < 100 && !fade; step++) {
  fade = departingCard.getAnimations()[0];
  if (!fade) await wait(5);
 }
 if (!fade) throw new Error('The card must fade after the server confirms feedback.');
 fade.pause();
 fade.currentTime = fade.effect.getTiming().duration / 2;
 check([...departingForm.querySelectorAll('button')].every(button => button.disabled),
       'The card disables all form buttons during the fade.');
 feedbackButton.click();
 departingForm.requestSubmit(feedbackButton);
 fade.finish();
 for (let step = 0; step < 100 && list() === previous; step++) await wait(20);
 check(list() !== previous, 'routine feedback refreshes the list');
 check(!list().querySelector('article'), 'routine feedback leaves the card absent');
 releaseOldRefresh();
 for (let step = 0; step < 100 && !oldRefreshReleased; step++) await wait(20);
 check(oldRefreshReleased && !list().querySelector('article'), 'the older refresh cannot restore the card');
 const updated = list();
 document.dispatchEvent(new Event('visibilitychange'));
 for (let step = 0; step < 100 && list() === updated; step++) await wait(20);
 check(list() !== updated && !list().querySelector('article'), 'A later refresh still replaces the list.');
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

        self.run_firefox(BrowserProbe, "browser", release=probe_complete.set)
        self.assertEqual(results, [[]], "The browser must complete every check without a failure.")
        self.assertEqual(len(gets), 12, "Load, update, reload, update, one refresh per POST, and the later update.")
        self.assertEqual([(post["action"], post["feedback"]) for post in posts], [
            (["approved"], [""]), (["feedback"], ["Too dark."]),
            (["question"], ["Which check passed?"]), (["feedback"], ["Please retry."]),
            (["feedback"], ["Retry after refresh fails."]), (["feedback"], ["Keep until next feedback."]),
            (["feedback"], ["Routine feedback."]),
        ])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_undo_returns_the_card_and_says_edit_and_send_again(self) -> None:
        _ = self.register()
        owner = self.owner
        assert owner is not None
        name = "Shot owner " + "W" * 80
        members = staff.StaffSnapshot((replace(owner, name=name),))
        probe = r"""<script>
(async () => {
 const failures = [], check = (ok, text) => { if (!ok) failures.push(text); };
 const realFetch = window.fetch.bind(window), host = window.top.document, posts = [];
 const until = async (test, label) => {
  const end = performance.now() + 8000;
  while (!test()) {
   if (performance.now() > end) throw new Error(label);
   await new Promise(resolve => setTimeout(resolve, 20));
  }
 };
 window.fetch = async (path, options = {}) => {
  if (String(path).endsWith('/undo')) return Response.json({outcome: 'undone'});
  if (String(path).startsWith('/send-hold/'))
   return Response.json({reason: 'The image send failed.'}, {status: 503});
  if (options.method !== 'POST') return new Promise(() => {});
  posts.push(new URLSearchParams(options.body));
  return Response.json({hold: 'a'.repeat(24), seconds: 30}, {status: 202, headers: {'X-Send-Hold': 'held'}});
 };
 try {
  const card = document.querySelector('[data-images] article'), form = card.querySelector('form');
  const status = form.querySelector('[data-image-status]');
  const group = () => document.querySelector('[data-image-groups] a[href="#' + card.id + '"]');
  form.elements.feedback.value = 'Too dark.';
  const view = host.defaultView;
  check(view.innerWidth === 390 && view.matchMedia('(prefers-color-scheme: dark)').matches,
        'The browser uses the phone width and dark theme.');
  for (const action of ['feedback', 'approved', 'question']) {
   form.requestSubmit(form.querySelector('button[value="' + action + '"]'));
   await until(() => host.querySelector('[data-send-undo] button'), 'The Undo toast did not show.');
   const toast = host.querySelector('[data-send-undo]'), text = toast.querySelector('span');
   check(!card.getClientRects().length && !group(), 'The card and its group leave when the toast appears.');
   const button = toast.querySelector('button'), box = toast.getBoundingClientRect();
   check(text.textContent === 'Sent to ' + __RECEIVER__ + '.' && button.textContent === 'Undo',
         'The toast names the image owner and offers Undo.');
   check(box.left >= 0 && box.right <= view.innerWidth && button.getBoundingClientRect().right < box.right,
         'The phone viewport shows the toast and its button.');
   const style = view.getComputedStyle(text);
   check(text.clientHeight > parseFloat(style.lineHeight) && text.scrollWidth <= text.clientWidth,
         'The full receiver name wraps inside the toast.');
   check(view.getComputedStyle(toast).backgroundColor === 'rgb(20, 27, 40)' &&
         style.color === 'rgb(228, 234, 244)', 'The toast uses its dark colors.');
   button.click();
   await until(() => status.textContent === 'Undone. Edit and send again.', 'The card did not say Undone.');
   check(card.isConnected && form.elements.feedback.value === 'Too dark.', 'After Undo the card returns with its words.');
   check(card.getClientRects().length && group() && document.activeElement === form.elements.feedback,
         'Undo shows the card and focuses its feedback field.');
  }
  check(posts.map(post => post.get('action')).join(',') === 'feedback,approved,question', 'Undo sends nothing more.');
  form.requestSubmit(form.querySelector('[value="question"]'));
  await until(() => host.querySelector('[data-send-undo] button'), 'The next Undo toast did not show.');
  host.querySelector('[data-send-undo]').getAnimations()[0].finish();
  await until(() => status.textContent === 'Not sent: The image send failed.', 'The card did not show the failure.');
  check(card.getClientRects().length && group() && form.elements.feedback.value === 'Too dark.',
        'A failed send restores the card, its group and its words.');
 } catch (error) {
  failures.push(String(error));
 }
 await realFetch('/results', {method: 'POST', body: JSON.stringify(failures)});
})();
</script>"""
        with patch.object(staff, "snapshot", return_value=members):
            page = images.render(self.status).decode()
        page = page.replace("</body>", probe.replace("__RECEIVER__", json.dumps(name)) + "</body>")
        self.assertEqual(firefox_probe_results(page.encode(), window_size=(390, 860), dark=True), [[]])

    def test_card_shows_the_title_look_for_details_image_and_actions(self) -> None:
        record = self.register("Look at <the> cards", look_for=("Check <the> count.", "Look for a gap."))
        record["registered_at"] = "2026-11-15T03:09:00+00:00"
        image_store.write_record(self.root / image_store.IMAGES_FOLDER / record["id"], record)
        card = images.card(record, "token", staff.snapshot(), 3)
        path = f"/images/{record['id']}/image.png"
        token = images.request_token("token", record["id"])
        self.assertEqual(without_role_colors(card), (
            f'<article id="image-{record['id']}" data-image="{record['id']}"'
            + f' data-owner="{record['owner']}" data-owner-name="shot-owner" data-send-receiver="shot-owner">'
            + '<h2 class="image-title">Look at &lt;the&gt; cards</h2>'
            + '<ul class="image-look-for"><li>Check &lt;the&gt; count.</li><li>Look for a gap.</li></ul>'
            + '<dl class="image-details">'
            + '<dt>Owner:</dt><dd>shot-owner (unit)</dd><dt>For  :</dt><dd>Phase console-images</dd>'
            + '<dt>Added:</dt><dd><time datetime="2026-11-15T03:09:00+00:00">Nov 14, 2026, 22:09</time></dd></dl>'
            + f'<a data-image-zoom href="{path}" target="_blank" rel="noopener"><img src="{path}" alt="Look at &lt;the&gt; cards" loading="lazy"></a>'
            + f'<form method="post" data-image-form><input type="hidden" name="image" value="{record['id']}">'
            + f'<input type="hidden" name="token" value="{token}">'
            + f'<input type="hidden" name="feedback_round" value="{images.feedback_round(set())}">'
            + f'<label for="feedback-{record['id']}">Feedback</label>'
            + f'<textarea id="feedback-{record['id']}" name="feedback" rows="3" autocomplete="off"></textarea>'
            + '<div class="review-actions"><div class="review-action-group"><span>Closes the item</span><div>'
            + '<button type="submit" name="action" value="approved">Approved</button>'
            + '<button type="submit" name="action" value="feedback">Send Feedback</button>'
            + '</div></div><div class="review-action-group"><span>The item returns to you</span><div>'
            + '<button type="submit" name="action" value="question">Ask question</button></div></div></div>'
            + '<span role="status" data-image-status hidden></span></form></article>'))
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
        with self.assertRaisesRegex(question_record.AnswerRefused, f"no image {record['id']}"):
            self.submit(body)

    def test_delete_refuses_a_wrong_token_or_unknown_image(self) -> None:
        record = self.register()
        token = images.console_token(self.root)
        for body in (urlencode({"image": record["id"], "token": "wrong"}),
                     urlencode({"image": "000000000000", "token": images.request_token(token, "000000000000")}),
                     urlencode({"image": record["id"]}),
                     urlencode({"image": "../x", "token": "t"})):
            with self.subTest(body=body), self.assertRaises(question_record.AnswerRefused):
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

    def add_legacy_caption(self) -> str:
        record = self.register()
        path = images.images_root(self.root) / record["id"] / image_store.RECORD
        stored = cast(dict[str, object], json.loads(path.read_text()))
        _ = stored.pop("title")
        _ = stored.pop("look_for")
        caption = "Check <the> cards. " * 12 + "\nThe last line stays visible."
        stored["caption"] = caption
        _ = path.write_text(json.dumps(stored))
        return caption

    def test_an_old_caption_only_record_shows_the_full_title(self) -> None:
        caption = self.add_legacy_caption()
        page = images.render(self.status).decode()
        self.assertIn('<h2 class="image-title">' + html.escape(caption) + '</h2>', page)
        self.assertNotIn('<ul class="image-look-for">', page)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_shows_the_title_at_the_limit_and_wraps_long_text_in_dark_mode(self) -> None:
        title = "W" * 80
        _ = self.register(title, look_for=("Check the spacing above each button.",))
        _ = self.add_legacy_caption()
        probe = b'''<script>
(async()=>{
 const failures=[];
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 try{
  document.documentElement.dataset.theme='dark';
  frameElement.style.width='1382px';
  await painted();
  const points=document.querySelector('.image-look-for');
  const title=points.closest('article').querySelector('.image-title');
  const legacy=[...document.querySelectorAll('.image-title')].find(heading=>heading!==title);
  const style=getComputedStyle(title);
  check(innerWidth===1382 && title.textContent==='W'.repeat(80) &&
        title.scrollWidth===title.clientWidth && title.scrollHeight===title.clientHeight &&
        title.getBoundingClientRect().height>1.5*parseFloat(style.lineHeight),
        'The full title wraps at 1382 px.');
  check(style.fontWeight==='400' && getComputedStyle(legacy).fontWeight==='400' && style.fontSize==='17px',
        'Both titles use normal weight and the List font size.');
  check(style.color==='rgb(240, 239, 236)' && getComputedStyle(points).color==='rgb(195, 194, 183)',
        'The dark theme uses secondary text for the bullet list.');
  check(legacy.getBoundingClientRect().height>2*parseFloat(style.lineHeight),
        'The legacy title wraps in the card.');
  frameElement.style.width='390px';
  await painted();
  const root=document.documentElement;
  check(innerWidth===390 && root.scrollWidth===root.clientWidth &&
        title.getBoundingClientRect().height>1.5*parseFloat(style.lineHeight) && title.scrollWidth===title.clientWidth,
        'The phone title wraps without horizontal overflow.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        page = images.render(self.status)
        self.assertEqual(firefox_probe_results(page.replace(b'</body>', probe + b'</body>')), [[]])

    def feedback_body(self, record: image_store.ImageRecord, feedback: str, action: str = "feedback") -> str:
        token = images.request_token(images.console_token(self.root), record["id"])
        return urlencode({"image": record["id"], "token": token, "feedback": feedback, "action": action})

    def test_direct_linked_answer_is_not_nates_words(self) -> None:
        keeper, _, item = self.list_item()
        record = images.register(self.shot, "Check the rows", self.owner_id(), "Phase check", list_item=item.id)
        words = "Keep the old rows."
        _ = images.send_answer(self.root, record["id"], "feedback", words)
        entry = the_list.load(keeper.id, item.id).log[-1]
        self.assertIsInstance(entry, the_list.ImageAnswerChange)
        self.assertNotEqual(entry.source, "nate via Images")
        source = the_list.folder(keeper.id) / f"{item.id}.md"
        self.assertEqual(behavior_request.verified_dates(source, {words}), {})

    def test_retry_of_an_older_linked_answer_keeps_only_answer_words(self) -> None:
        keeper, _, item = self.list_item()
        title = "Keep the third row."
        words = "Keep the second row.\nIncrease its height."
        record = images.register(self.shot, title, self.owner_id(), "Phase check", list_item=item.id,
                                 look_for=("Keep the first row.",))
        identity = record["id"]
        key = "review:" + "a" * 64
        saved = image_store.new_answers(record)
        saved["attempts"][key] = {"message": images.answer_message(record, "feedback", words), "answered": [],
                                  "recipients": [], "confirmed": []}
        image_store.write_answers(images.images_root(self.root), saved)
        images.delete(self.root, identity)
        token = images.request_token(images.console_token(self.root), identity)
        self.submit(urlencode({"image": identity, "token": token, "action": "retry", "retry_key": key}))
        entry = the_list.load(keeper.id, item.id).log[-1]
        self.assertEqual(entry.what, f"Image {identity}: {images.ANSWERS['feedback']}\n{words}")
        source = the_list.folder(keeper.id) / f"{item.id}.md"
        self.assertEqual(behavior_request.verified_dates(source, {"Title: " + title, "- Keep the first row."}), {})

    def test_signed_answer_recovers_a_failed_decision_write(self) -> None:
        first_time = datetime(2026, 11, 24, 23, 58, tzinfo=UTC)
        retry_time = first_time + timedelta(days=1)
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                record = self.register()
                identity = record["id"]
                body = self.feedback_body(record, "Keep the first column.")
                with (patch.object(images, "datetime", wraps=datetime) as clock,
                      patch.object(atomic_file, "write_text_atomic", side_effect=OSError("disk unavailable"))):
                    clock.configure_mock(**{"now.return_value": first_time})
                    with self.assertRaisesRegex(images.AnswerNotSent, "disk unavailable"):
                        self.submit(body)
                key, = image_store.read_answers(images.images_root(self.root), identity)["attempts"]
                if deleted:
                    images.delete(self.root, identity)
                    token = images.request_token(images.console_token(self.root), identity)
                    body = urlencode({"image": identity, "token": token, "action": "retry", "retry_key": key})
                with patch.object(images, "datetime", wraps=datetime) as clock:
                    clock.configure_mock(**{"now.return_value": retry_time})
                    self.submit(body)
                path, = images.decision_root(self.root).glob(f"{identity}-*.json")
                self.assertEqual(images.read_image_decision(self.root, path),
                                 images.ImageDecision(identity, "feedback", "Keep the first column.", first_time))

    def test_signing_the_same_answer_with_or_without_a_round_keeps_one_decision(self) -> None:
        for explicit_first in (True, False):
            with self.subTest(explicit_first=explicit_first):
                record = self.register()
                sends = len(self.sent)
                body = self.feedback_body(record, "Keep the first column.")
                explicit = body + f"&feedback_round={images.feedback_round(set())}"
                first, second = (explicit, body) if explicit_first else (body, explicit)
                self.submit(first)
                archive = images.decision_root(self.root)
                identity = record["id"]
                path, = archive.glob(f"{identity}-*.json")
                original = path.read_bytes()
                self.submit(second)
                self.assertEqual(list(archive.glob(f"{identity}-*.json")), [path])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(len(self.sent), sends + 1)

    def test_signed_correction_in_the_same_round_has_its_own_record(self) -> None:
        record = self.register()
        round_id = images.feedback_round(set())
        keep = self.feedback_body(record, "Keep the red row.") + f"&feedback_round={round_id}"
        remove = self.feedback_body(record, "Remove the red row.") + f"&feedback_round={round_id}"
        self.submit(keep)
        archive = images.decision_root(self.root)
        path, = archive.glob(f"{record['id']}-*.json")
        original = path.read_bytes()
        old_key = hashlib.sha256(json.dumps((record["registered_at"], round_id)).encode()).hexdigest()
        path = path.rename(archive / f"{record['id']}-{old_key}.json")
        self.submit(keep)
        self.submit(remove)
        self.submit(remove)
        self.assertEqual(path.read_bytes(), original)
        decisions = [images.read_image_decision(self.root, path) for path in archive.glob(f"{record['id']}-*.json")]
        self.assertEqual(len(decisions), 2)
        self.assertEqual({decision.feedback for decision in decisions if isinstance(decision, images.ImageDecision)},
                         {"Keep the red row.", "Remove the red row."})

    def test_fresh_form_request_without_a_round_sends_in_the_current_round(self) -> None:
        keeper, _, item = self.list_item()
        record = images.register(self.shot, "Check the rows", self.owner_id(), "Phase check", list_item=item.id)
        identity = record["id"]
        change_page.deliver(images.ask_question(self.root, identity, "Which check passed?", self.status), self.status)
        self.sent.clear()
        body = self.feedback_body(record, "Keep the red row.") + "&answer_request="
        first = body + "a" * 32
        self.submit(first)
        first_round = images.feedback_round(set())
        self.submit(first + f"&feedback_round={first_round}")
        self.assertEqual(len(self.sent), 2)

        images.answer_question(self.root, identity, "Q1", "The gate passed.")
        current_round = images.feedback_round({"Q1"})
        second = body + "b" * 32
        self.submit(second)
        self.assertEqual(len(self.sent), 4)
        self.submit(second + f"&feedback_round={current_round}")
        self.assertEqual(len(self.sent), 4)
        self.submit(body + "c" * 32)

        attempts = image_store.read_answers(images.images_root(self.root), identity)["attempts"]
        self.assertEqual(len(attempts), 3)
        rounds = [attempt["signed_answer"]["round_id"] for attempt in attempts.values() if "signed_answer" in attempt]
        self.assertEqual(rounds, [first_round, current_round, current_round])
        self.assertEqual(len(self.sent), 6)
        saved = the_list.load(keeper.id, item.id)
        entries = [entry for entry in saved.log if isinstance(entry, the_list.ImageAnswerChange)]
        self.assertEqual(len({entry.key for entry in entries}), 3)
        for entry in entries:
            self.assertIn("Keep the red row.", entry.what)

    def test_retry_of_an_older_form_request_finishes_its_original_attempt(self) -> None:
        record = self.register()
        identity = record["id"]
        keep, remove = "Keep the red row.", "Remove the red row."
        first_time = datetime(2026, 11, 24, 23, 58, tzinfo=UTC)
        other_time = first_time + timedelta(days=1)
        retry_time = first_time + timedelta(days=2)
        round_field = f"&feedback_round={images.feedback_round(set())}"
        keep_form = self.feedback_body(record, keep) + round_field + "&answer_request=" + "a" * 32
        remove_form = self.feedback_body(record, remove) + round_field + "&answer_request=" + "b" * 32
        with patch.object(images, "datetime", wraps=datetime) as clock:
            clock.configure_mock(**{"now.return_value": first_time})
            with (patch.object(change_page, "send_message", side_effect=RuntimeError("owner offline")),
                  self.assertRaisesRegex(images.AnswerNotSent, "owner offline")):
                self.submit(keep_form)
            directory = images.images_root(self.root)
            key, = image_store.read_answers(directory, identity)["attempts"]
            clock.configure_mock(**{"now.return_value": other_time})
            self.submit(remove_form)
            clock.configure_mock(**{"now.return_value": retry_time})
            self.submit(keep_form)
        attempts = image_store.read_answers(directory, identity)["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[key]["confirmed"], [self.owner_id()])
        self.assertIn(keep, self.sent[-1][2])
        paths = images.decision_root(self.root).glob(f"{identity}-*.json")
        self.assertCountEqual([images.read_image_decision(self.root, path) for path in paths],
                              [images.ImageDecision(identity, "feedback", keep, first_time),
                               images.ImageDecision(identity, "feedback", remove, other_time)])

    def test_retry_without_a_round_uses_the_original_form_request(self) -> None:
        keep, remove = "Keep the red row.", "Remove the red row."
        round_id = images.feedback_round(set())
        request_id = "a" * 32
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                record = self.register()
                identity = record["id"]
                directory = images.images_root(self.root)
                keep_form = self.feedback_body(record, keep) + f"&answer_request={request_id}"
                self.submit(keep_form + f"&feedback_round={round_id}")
                if legacy:
                    saved = image_store.read_answers(directory, identity)
                    attempt, = saved["attempts"].values()
                    attempt["form_request_keys"] = [change_page.review_delivery_key(
                        directory / identity / image_store.RECORD, identity,
                        json.dumps([record["registered_at"], "feedback", keep, round_id]),
                        purpose=f"images-form:{request_id}")]
                    image_store.write_answers(directory, saved)
                self.submit(self.feedback_body(record, remove) + f"&feedback_round={round_id}&answer_request=" + "b" * 32)
                sends = len(self.sent)
                paths = set(images.decision_root(self.root).glob(f"{identity}-*.json"))
                self.submit(keep_form)
                self.assertEqual(len(self.sent), sends)
                self.assertEqual(set(images.decision_root(self.root).glob(f"{identity}-*.json")), paths)

    def test_retry_of_a_newer_removed_answer_keeps_the_older_draft(self) -> None:
        record = self.register()
        identity = record["id"]
        keep, remove = "Keep the red row.", "Remove the red row."
        round_id = images.feedback_round(set())
        request_id = "a" * 32
        keep_form = self.feedback_body(record, keep) + f"&feedback_round={round_id}&answer_request="
        directory = images.images_root(self.root)
        with patch.object(change_page, "send_message", side_effect=RuntimeError("owner offline")):
            with self.assertRaisesRegex(images.AnswerNotSent, "owner offline"):
                self.submit(keep_form + request_id)
        original_key, = image_store.read_answers(directory, identity)["attempts"]
        self.submit(self.feedback_body(record, remove) + f"&feedback_round={round_id}&answer_request=" + "b" * 32)
        with patch.object(change_page, "send_message", side_effect=RuntimeError("owner offline")):
            with self.assertRaisesRegex(images.AnswerNotSent, "owner offline"):
                self.submit(keep_form + "c" * 32)
        images.delete(self.root, identity)
        attempts = image_store.read_answers(directory, identity)["attempts"]
        new_key, = (key for key, attempt in attempts.items() if key != original_key and not attempt["confirmed"])
        token = images.request_token(images.console_token(self.root), identity)
        retry = {"image": identity, "token": token, "action": "retry", "retry_key": new_key,
                 "draft_action": "feedback", "feedback_round": round_id, "feedback": keep}
        result = images.submit(self.root, urlencode({**retry, "answer_request": request_id}), self.status)
        assert isinstance(result, images.ImageAnswerSent)
        self.assertFalse(result.draft_confirmed)
        attempts = image_store.read_answers(directory, identity)["attempts"]
        self.assertEqual(attempts[original_key]["confirmed"], [])
        self.assertEqual(attempts[new_key]["confirmed"], [self.owner_id()])
        result = images.submit(self.root, urlencode({**retry, "answer_request": "c" * 32}), self.status)
        assert isinstance(result, images.ImageAnswerSent)
        self.assertTrue(result.draft_confirmed)

    def test_three_tabs_can_restore_an_earlier_answer_in_the_same_round(self) -> None:
        keep = "Keep the red row."
        remove = "Remove the red row."
        start = datetime(2026, 11, 24, 23, 58, tzinfo=UTC)
        keeper, _, item = self.list_item()
        for linked in (False, True):
            with self.subTest(linked=linked):
                record = images.register(self.shot, "Check the rows", self.owner_id(), "Phase check",
                                         list_item=item.id if linked else None)
                round_id = images.feedback_round(set())
                self.sent.clear()
                for index, words in enumerate((keep, remove, keep, keep)):
                    with patch.object(images, "datetime", wraps=datetime) as clock:
                        clock.configure_mock(**{"now.return_value": start + timedelta(days=index)})
                        self.submit(self.feedback_body(record, words) + f"&feedback_round={round_id}")
                self.assertEqual(len(self.sent), 6 if linked else 3)
                times = [start + timedelta(days=index) for index in range(3)]
                if linked:
                    source = the_list.folder(keeper.id) / f"{item.id}.md"
                    self.assertEqual(behavior_request.verified_dates(source, {keep, remove}),
                                     {keep: times[2].date(), remove: times[1].date()})
                    records = [entry for entry in the_list.load(keeper.id, item.id).log
                               if isinstance(entry, the_list.ImageAnswerChange)]
                    self.assertEqual([entry.time for entry in records], times)
                else:
                    paths = images.decision_root(self.root).glob(f"{record['id']}-*.json")
                    decisions = [images.read_image_decision(self.root, path) for path in paths]
                    self.assertCountEqual(decisions, [images.ImageDecision(record["id"], "feedback", words, moment)
                                                   for words, moment in zip((keep, remove, keep), times, strict=True)])

    def test_signed_resubmission_with_a_legacy_marker_creates_a_record(self) -> None:
        record = self.register()
        identity = record["id"]
        words = "Keep the red row."
        directory = images.images_root(self.root)
        folder = directory / identity
        key = change_page.review_delivery_key(folder / image_store.RECORD, identity,
                                              json.dumps([record["registered_at"], "feedback", words]))
        marker = folder / (".review-link-feedback-" + key.removeprefix("review:"))
        _ = marker.write_text("")
        body = self.feedback_body(record, words)
        self.submit(body)
        path, = images.decision_root(self.root).glob(f"{identity}-*.json")
        decision = images.read_image_decision(self.root, path)
        assert isinstance(decision, images.ImageDecision)
        self.assertEqual(decision.feedback, words)

    def test_public_sender_cannot_choose_the_signed_path(self) -> None:
        self.assertNotIn("origin", signature(images.send_answer).parameters)
        record = self.register()
        _ = images.send_answer(self.root, record["id"], "approved", "")
        self.assertFalse(images.decision_root(self.root).exists())
        pending = self.register()
        body = self.feedback_body(pending, "Keep the red row.")
        with patch.object(images, "_save_image_decision", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(images.AnswerNotSent, "disk unavailable"):
                self.submit(body)
        _ = images.send_answer(self.root, pending["id"], "feedback", "Keep the red row.")
        self.assertFalse(images.decision_root(self.root).exists())
        self.submit(body)
        self.assertEqual(len(list(images.decision_root(self.root).glob("*.json"))), 1)

    def test_retry_of_a_failed_list_write_keeps_the_original_signed_time(self) -> None:
        keeper, _, item = self.list_item()
        record = images.register(self.shot, "Check the rows", self.owner_id(), "Phase check", list_item=item.id)
        words = "Keep the old rows."
        answer_time = datetime(2026, 11, 24, 23, 58, tzinfo=UTC)
        retry_time = answer_time + timedelta(days=1)
        body = self.feedback_body(record, words)
        with (patch.object(images, "datetime", wraps=datetime) as clock,
              patch.object(the_list, "save_image_answer", side_effect=OSError("disk unavailable"))):
            clock.configure_mock(**{"now.return_value": answer_time})
            with self.assertRaisesRegex(images.AnswerNotSent, "disk unavailable"):
                self.submit(body)
        with (patch.object(images, "datetime", wraps=datetime) as clock,
              patch.object(the_list, "now", return_value=retry_time)):
            clock.configure_mock(**{"now.return_value": retry_time})
            self.submit(body)
        updated = the_list.load(keeper.id, item.id)
        self.assertEqual(updated.log[-1].time, answer_time)
        self.assertEqual(updated.updated, retry_time)
        source = the_list.folder(keeper.id) / f"{item.id}.md"
        self.assertEqual(behavior_request.verified_dates(source, {words}), {words: answer_time.date()})

    def test_approved_and_feedback_send_the_answer_with_feedback_only_when_written(self) -> None:
        for action, first, feedback, lines in (
                ("approved", "your image is approved.", "", ()),
                ("approved", "your image is approved.", "  The counts are right. ", ("Feedback: The counts are right.",)),
                ("feedback", "your image is not approved. The feedback follows.", "  The counts are wrong. ",
                 ("Feedback: The counts are wrong.",))):
            with self.subTest(action=action, feedback=feedback):
                self.sent.clear()
                record = self.register("Should we keep the new panel?", look_for=("Check the counts.",))
                accepted_after = datetime.now(UTC)
                self.submit(self.feedback_body(record, feedback, action))
                archive = images.decision_root(self.root)
                path, = archive.glob(f"{record['id']}-*.json")
                fields = delivery_state.read_fields(path)
                self.assertEqual(fields["source"], "nate via Images")
                decision = images.read_image_decision(self.root, path)
                assert isinstance(decision, images.ImageDecision)
                self.assertEqual(decision, images.ImageDecision(record["id"], action, feedback.strip(), decision.time))
                self.assertEqual(decision.time.tzinfo, UTC)
                self.assertLessEqual(accepted_after, decision.time)
                self.assertLessEqual(decision.time, datetime.now(UTC))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual([message for _, _, message in self.sent], ["\n".join((
                    f"From the user (via the Images page): {first}", *lines,
                    f"Image: http://127.0.0.1:8765/images/{record['id']}/image.png",
                    "Title: Should we keep the new panel?", "Look for:", "- Check the counts.",
                    "For: Phase console-images",
                    "After you act on it, delete the image: "
                    + f"~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id {record['id']}"))])
                self.assertIn("feedback_at", image_store.read_record(self.root / image_store.IMAGES_FOLDER / record["id"]))
                self.assertNotIn(record["id"], images.render(self.status).decode())
                images.delete(self.root, record["id"])
                directory = images.images_root(self.root)
                (directory / f".review-link-image-answers-{record['id']}.json").unlink()
                self.assertEqual(images.read_image_decision(self.root, path), decision)
                self.assertEqual(delivery_state.read_fields(path)["image_text"],
                                 "Should we keep the new panel?\nLook for:\n- Check the counts.")

    def test_linked_answers_save_before_delivery_and_survive_owner_delete(self) -> None:
        self.enterContext(patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.temporary / "state")}))
        morty = fixture_member("producer", name="morty")
        producer = fixture_member("producer", name="shot-producer")
        owner = self.owner
        assert owner is not None
        for action, subject in (("approved", "--phase"), ("feedback", "--change")):
            with self.subTest(action=action):
                item = the_list.add(morty.id, "Check the picture", owner=morty.id, source="nate",
                                    changes={"producer": producer.id, "state": the_list.ItemState.DELIVERED})
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    self.assertEqual(images.main(["register", "--image", str(self.shot), "--title", "Check the rows",
                        "--look-for", "Keep the second row.", "--owner", self.owner_id(), subject, "test/C1", "--list-item", item.id]), 0)
                record, = images.records(self.root)
                self.assertEqual(record.get("list_item"), item.id)
                page = images.render(self.status).decode()
                self.assertIn('<h2 class="image-title">Check the rows</h2>'
                              + '<ul class="image-look-for"><li>Keep the second row.</li></ul>', page)
                words = "Check the two rows.\nKeep this text."
                received: list[tuple[str, str]] = []
                producer_online = action == "approved"

                def receive(recipient: str, _sender: str, message: str, *, key: str,
                            recipient_key: str) -> Literal["skipped"] | None:
                    saved = the_list.load(morty.id, item.id)
                    self.assertIs(saved.state, the_list.ItemState.DELIVERED if action == "approved"
                                  else the_list.ItemState.WORKING)
                    self.assertTrue(any(isinstance(entry.what, str) and words in entry.what for entry in saved.log))
                    self.assertIn(words, message)
                    self.assertIn(f"List item: {item.id}", message)
                    self.assertIn("Title: Check the rows\nLook for:\n- Keep the second row.", message)
                    if recipient_key == producer.id and not producer_online:
                        raise RuntimeError("producer offline")
                    if (recipient_key, key) in received:
                        return "skipped"
                    received.append((recipient_key, key))
                    if action == "approved" and recipient == f"session:{owner.current_session}":
                        images.delete(self.root, record["id"])
                    return None

                body = self.feedback_body(record, words, action)
                with patch.object(change_page, "send_message", side_effect=receive):
                    if action == "feedback":
                        with self.assertRaisesRegex(images.AnswerNotSent, "producer offline"):
                            self.submit(body)
                        saved = the_list.load(morty.id, item.id)
                        producer_online = True
                        self.submit(body)
                        self.assertEqual(the_list.load(morty.id, item.id), saved)
                        images.delete(self.root, record["id"])
                    else:
                        self.submit(body)
                self.assertEqual([target for target, _ in received], [owner.id, producer.id])
                self.assertEqual(len({key for _, key in received}), 1)
                self.assertEqual(images.records(self.root), [])
                saved = the_list.load(morty.id, item.id)
                self.assertTrue(any(isinstance(entry.what, str) and words in entry.what for entry in saved.log))
                self.assertFalse(images.decision_root(self.root).exists())

    def test_registration_saves_the_legacy_list_keeper(self) -> None:
        keeper, producer, item = self.list_item()
        legacy = staff.StaffId("legacy-list")
        the_list.folder(legacy).parent.mkdir()
        _ = the_list.folder(keeper.id).rename(the_list.folder(legacy))
        record = images.register(self.shot, "Check", self.owner_id(), "Phase check", list_item=item.id)
        self.assertEqual(record.get("list_keeper"), legacy)
        self.submit(self.feedback_body(record, "Check the width."))
        self.assertIs(the_list.load(legacy, item.id).state, the_list.ItemState.WORKING)
        self.assertEqual(self.sent[-1][0], f"session:{producer.current_session}")

    def test_deleted_picture_keeps_the_pending_producer_send(self) -> None:
        keeper, producer, item = self.list_item()
        record = images.register(self.shot, "Check", self.owner_id(), "Phase check", list_item=item.id)
        calls: list[tuple[str, str]] = []
        online = False

        def send(_recipient: str, _sender: str, message: str, *, key: str, recipient_key: str) -> None:
            calls.append((recipient_key, key))
            self.assertIn("Keep these words.", message)
            if recipient_key == self.owner_id():
                images.delete(self.root, record["id"])
            elif not online:
                raise RuntimeError("producer offline")

        body = self.feedback_body(record, "Keep these words.")
        with patch.object(change_page, "send_message", side_effect=send):
            with self.assertRaisesRegex(images.AnswerNotSent, "producer offline"):
                self.submit(body)
            online = True
            self.submit(body)
            self.submit(body)
        self.assertEqual([recipient for recipient, _ in calls], [self.owner_id(), *([producer.id] * 4)])
        self.assertEqual(len({key for _, key in calls}), 1)
        saved = the_list.load(keeper.id, item.id)
        self.assertEqual(sum(isinstance(entry.what, str) and "Keep these words." in entry.what
                             for entry in saved.log), 1)

    def test_unconfirmed_producer_leaves_the_card_and_retry_sends_only_to_the_producer(self) -> None:
        _, producer, item = self.list_item()
        record = images.register(self.shot, "Check", self.owner_id(), "Phase check", list_item=item.id)
        online = False
        calls: list[tuple[str, str]] = []

        def send(_recipient: str, _sender: str, _message: str, *, key: str,
                 recipient_key: str) -> Literal["pending"] | None:
            calls.append((recipient_key, key))
            return "pending" if recipient_key == producer.id and not online else None

        body = self.feedback_body(record, "Keep it visible until delivery.")
        with patch.object(change_page, "send_message", side_effect=send):
            with self.assertRaisesRegex(images.AnswerNotSent, "has not confirmed"):
                self.submit(body)
            self.assertIn(record["id"], images.render(self.status).decode())
            online = True
            self.submit(body)
        self.assertEqual([recipient for recipient, _ in calls], [self.owner_id(), *([producer.id] * 4)])
        self.assertEqual(len({key for _, key in calls}), 1)
        self.assertNotIn(record["id"], images.render(self.status).decode())

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for the picture form")
    def test_browser_keeps_words_for_a_removed_picture_until_send_succeeds(self) -> None:
        record = self.register()
        page = images.render(self.status)
        images.delete(self.root, record["id"])
        fresh = json.dumps(images.render(self.status).decode()).replace("<", r"\u003c")
        setup = r"""<script>
const realFetch = window.fetch.bind(window), posts = [];
let release;
window.fetch = async (url, options) => {
  if (url === location.pathname) {
    if (options.method === 'POST') {
      posts.push(options.body.toString());
      if (posts.length === 1) {
        await new Promise(resolve => { release = resolve; });
        return new Response(JSON.stringify({reason:'The producer is offline.'}), {status:503});
      }
      return new Response(JSON.stringify({notice:'This picture was removed; your words went to shot-owner.'}));
    }
    return new Response(FRESH);
  }
  return realFetch(url, options);
};
</script>""".replace("FRESH", fresh)
        probe = r"""<script>
(async () => {
 const failures = [], check = (result, text) => { if (!result) failures.push(text); };
 const form = () => document.querySelector('[data-image-form]');
 const find = selector => document.querySelector(selector);
 const until = async (label, condition) => {
   for (let count = 0; count < 50; count++){
     if (condition()) return;
     await new Promise(resolve => setTimeout(resolve, 50));
   }
   throw Error(label);
 };
 const words = 'Keep the width.\nUse a darker border.';
 try {
   form().elements.feedback.value = words;
   form().querySelector('[value=feedback]').click();
   await until('The first send starts.', () => release);
   check(!!form()?.getClientRects().length, 'The card stays visible until the send is confirmed.');
   release();
   const original = form();
   await until('The refresh keeps the removed picture form.',
     () => form() && form() !== original && find('[data-image-status]').textContent.includes('offline'));
   check(form().elements.feedback.value === words, 'The refresh keeps the words.');
   form().querySelector('[value=feedback]').click();
   await until('The confirmed send removes the form.', () => !form());
   check(posts.length === 2 && posts[0] === posts[1], 'The retry submits the same request.');
   check(/^[0-9a-f]{32}$/.test(new URLSearchParams(posts[0]).get('answer_request')), 'The form sends a request ID.');
   check(find('[data-image-notice]').textContent === 'This picture was removed; your words went to shot-owner.'
     && !find('[data-image-notice]').hidden, 'The page names the recipient after success.');
 } catch(error) { failures.push(String(error)); }
 await realFetch('/results', {method:'POST', body:JSON.stringify(failures)});
})();</script>"""
        markup = page.replace(b"<head>", b"<head>" + setup.encode()).replace(b"</body>", probe.encode() + b"</body>")
        self.assertEqual(firefox_probe_results(markup), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for the picture form")
    def test_script_reload_restores_each_draft_and_clears_only_the_sent_draft(self) -> None:
        first, second = self.register("First shot"), self.register("Second shot")
        page = images.render(self.status).decode()
        fresh = json.dumps(page).replace("<", r"\u003c")
        probe = r"""<script>
(async () => {
 const phase = sessionStorage.getItem('probe-phase');
 const first = document.querySelector('#feedback-FIRST'), second = document.querySelector('#feedback-SECOND');
 const firstWords = 'Keep the title.\nUse a darker border.', secondWords = 'Keep the second draft.';
 const failures = [], check = (ok, text) => { if (!ok) failures.push(text); };
 const realFetch = window.fetch.bind(window);
 const fresh = FRESH;
 const update = () => document.dispatchEvent(new Event('visibilitychange'));
 const version = document.querySelector('[data-images]').dataset.script;
 window.fetch = async (url, options) => {
   if (url !== location.pathname) return realFetch(url, options);
   if (options?.method === 'POST') return new Response('{"state":"done"}');
   return new Response(phase === 'sending' ? fresh : fresh.replace(
     'data-script="' + version + '"', 'data-script="new-script"'));
 };
 if (!phase) {
   first.value = firstWords;
   second.value = secondWords;
   sessionStorage.setItem('probe-phase', 'sending');
   update();
   return;
 }
 if (phase === 'sending') {
   check(first.value === firstWords, 'The first draft survives the script reload.');
   check(second?.value === secondWords, 'The second draft survives when its picture is gone.');
   if (failures.length) {
     await realFetch('/results', {method:'POST', body:JSON.stringify(failures)});
     return;
   }
   const form = first.form;
   const before = document.querySelector('[data-images]');
   form.requestSubmit(form.querySelector('button[value=feedback]'));
   for (let count = 0; count < 60 && document.querySelector('[data-images]') === before; count++) {
     await new Promise(resolve => setTimeout(resolve, 50));
   }
   sessionStorage.setItem('probe-phase', 'sent');
   location.reload();
   return;
 }
 check(first.value === '', 'The confirmed draft stays clear after reload.');
 check(second?.value === secondWords, 'The second draft remains after the send.');
 await realFetch('/results', {method:'POST', body:JSON.stringify(failures)});
})();</script>""".replace("FIRST", first["id"]).replace("SECOND", second["id"]).replace("FRESH", fresh)
        remove_picture = ("""<script>
if (sessionStorage.getItem('probe-phase')) document.querySelector('#image-SECOND').remove();
</script>""").replace("SECOND", second["id"])
        markup = page.replace(f"<script>{images.SCRIPT}", remove_picture + f"<script>{images.SCRIPT}")
        self.assertEqual(firefox_probe_results(markup.replace("</body>", probe + "</body>").encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for the picture form")
    def test_picture_guard_blocks_both_submit_orders_and_keeps_each_error(self) -> None:
        removed, other = self.register("Removed shot"), self.register("Other shot")
        identity = removed["id"]
        round_id = images.feedback_round(set())
        images.delete(self.root, identity)
        with patch.object(change_page, "send_message", side_effect=RuntimeError("owner offline")):
            for words in ("Keep the title.", "Keep the caption."):
                with self.assertRaises(images.AnswerNotSent):
                    self.submit(self.feedback_body(removed, words) + f"&feedback_round={round_id}")
        page = images.render(self.status).decode()
        fresh = json.dumps(page).replace("<", r"\u003c")
        token = images.request_token(images.console_token(self.root), identity)
        drafts = [[identity, {"words": "Keep the title.", "token": token, "round": round_id,
                              "retryAction": "feedback", "retryRound": round_id}]]
        seed = """<script>
sessionStorage.setItem('images.answer-drafts:' + location.pathname, JSON.stringify(DRAFTS));
</script>""".replace("DRAFTS", json.dumps(drafts))
        probe = r"""<script>
(async () => {
 const failures = [], check = (ok, message) => { if (!ok) failures.push(message); };
 const realFetch = window.fetch.bind(window), posts = [];
 const draft = id => document.getElementById('feedback-' + id);
 const edit = () => draft('REMOVED').form;
 const retries = () => [...document.querySelectorAll('button[value=retry]')];
 const retry = () => retries().find(button => button.closest('article').textContent.includes('Keep the title.'));
 const status = form => form.querySelector('[data-image-status]').textContent;
 const say = (ok, reason) => new Response(JSON.stringify(ok ? {draft_confirmed: true} : {reason}),
                                            {status: ok ? 200 : 503});
 const submit = button => button.form.dispatchEvent(new SubmitEvent('submit', {
   bubbles:true, cancelable:true, submitter:button
 }));
 const until = async (message, ready) => {
   for (let step = 0; step < 65; step++) {
     if (ready()) return;
     await new Promise(resolve => setTimeout(resolve, 30));
   }
   throw new Error(message);
 };
 const list = () => document.querySelector('[data-images]');
 const refresh = async () => {
   const previous = list();
   document.dispatchEvent(new Event('visibilitychange'));
   await until('The refresh replaces the list.', () => list() !== previous);
 };
 window.fetch = (url, options) => {
   if (url !== location.pathname) return realFetch(url, options);
   if (options?.method !== 'POST') return Promise.resolve(new Response(FRESH));
   return new Promise(resolve => posts.push({body: options.body, resolve}));
 };
 const rejectAll = () => posts.forEach(post => post.resolve(say(false,
   post.body.get('action') === 'retry' ? 'Retry failed.' : 'Approval failed.')));
 const sameButtons = () => [...edit().querySelectorAll('button'), ...retries()];
 const otherForm = () => draft('OTHER').form;
 const approved = () => edit().querySelector('button[value=approved]');
 try {
   submit(retry());
   check(sameButtons().every(button => button.disabled), 'Retry disables every submit for its picture.');
   submit(approved());
   check(posts.length === 1, 'Retry blocks a second submit in code.');
   const other = otherForm().querySelector('button[value=question]');
   check(!other.disabled, 'Another picture can still submit.');
   draft('OTHER').value = 'Which check passed?';
   submit(other);
   check(posts.some(post => post.body.get('image') === 'OTHER'), 'Another picture starts a request.');
   const before = list();
   rejectAll();
   await until('The other request refreshes the list.', () => before !== list());
   check(status(retry().form) === 'Not sent: Retry failed.', 'The refresh keeps the Retry error.');
   check(retries().some(button => status(button.form) === 'Not sent yet'), 'Another attempt keeps its own status.');
   check(draft('REMOVED').value === 'Keep the title.', 'Retry failure keeps the draft.');
   const count = posts.length, previous = list();
   submit(approved());
   check(sameButtons().every(button => button.disabled), 'Approval disables every submit for its picture.');
   submit(retry());
   check(posts.length === count + 1, 'Approval blocks Retry in code.');
   rejectAll();
   await until('Approval failure refreshes the list.', () => previous !== list());
   check(status(edit()) === 'Not sent: Approval failed.', 'The refresh keeps the approval error.');
   check(status(retry().form) === 'Not sent: Retry failed.', 'The refresh still shows the earlier Retry error.');
   check(draft('REMOVED').value === 'Keep the title.', 'Approval failure keeps the draft.');
   check(sameButtons().every(button => !button.disabled), 'The forms can submit again after failure.');
   await refresh();
   check(status(retry().form) === 'Not sent: Retry failed.', 'A later refresh keeps the Retry error.');
   const beforeFeedback = list();
   submit(edit().querySelector('button[value=feedback]'));
   rejectAll();
   await until('Feedback failure refreshes the list.', () => beforeFeedback !== list());
   const beforeRetry = list();
   submit(retry());
   check(status(retry().form) === 'Not sent: Retry failed.', 'The error stays until the new Retry has a result.');
   const post = posts.at(-1);
   check(post.body.get('draft_action') === 'feedback', 'Retry submits the stored action.');
   check(post.body.get('feedback_round') === ROUND, 'Retry submits the stored round.');
   for (const words of ['A newer edit.', 'Keep the title.']) {
     const field = draft('REMOVED');
     field.value = words;
     field.dispatchEvent(new Event('input', {bubbles:true}));
   }
   post.resolve(say(true));
   await until('The confirmed Retry refreshes the list.', () => beforeRetry !== list());
   check(draft('REMOVED')?.value === 'Keep the title.', 'Retry keeps the newer revision with the same words.');
   const saved = new Map(JSON.parse(sessionStorage.getItem('images.answer-drafts:' + location.pathname)));
   check(saved.get('REMOVED')?.words === 'Keep the title.', 'The newer revision remains in storage.');
 } catch (error) { failures.push(String(error)); }
 await realFetch('/results', {method:'POST', body:JSON.stringify(failures)});
})();</script>""".replace("REMOVED", identity).replace("OTHER", other["id"]).replace("FRESH", fresh).replace(
            "ROUND", json.dumps(round_id))
        markup = page.replace(f"<script>{images.SCRIPT}", seed + f"<script>{images.SCRIPT}")
        self.assertEqual(firefox_probe_results(markup.replace("</body>", probe + "</body>").encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for the picture form")
    def test_ordinary_reload_restores_unsent_picture_words(self) -> None:
        record = self.register()
        probe = r"""<script>
(async () => {
 const feedback = document.getElementById('feedback-IMAGEID');
 const words = 'Keep the title.\nUse a darker border.';
 if (!sessionStorage.getItem('typed')) {
   feedback.value = words;
   feedback.dispatchEvent(new Event('input', {bubbles:true}));
   sessionStorage.setItem('typed', 'yes');
   location.reload();
   return;
 }
 const failures = feedback.value === words ? [] : ['The ordinary reload must restore the unsent words.'];
 await fetch('/results', {method:'POST', body:JSON.stringify(failures)});
})();</script>""".replace("IMAGEID", record["id"])
        page = images.render(self.status).replace(b"</body>", probe.encode() + b"</body>")
        self.assertEqual(firefox_probe_results(page), [[]])

    def test_a_card_from_before_i21_that_posts_not_approved_gets_a_clear_refusal(self) -> None:
        record = self.register()
        with self.assertRaisesRegex(question_record.AnswerRefused, "^Not Approved was removed. Use Send Feedback.$"):
            self.submit(self.feedback_body(record, "", "not approved"))
        self.assertEqual(self.sent, [])
        self.assertIn(record["id"], images.render(self.status).decode())

    def test_feedback_goes_to_the_owner_session_and_hides_the_image_but_keeps_it(self) -> None:
        record = self.register("Panel counts", look_for=("Check the counts.", "Check their order."))
        self.submit(self.feedback_body(record, "  The counts are wrong.  "))
        assert self.owner is not None
        self.assertEqual(len(self.sent), 1)
        recipient, sender, message = self.sent[0]
        self.assertEqual((recipient, sender), (f"session:{self.owner.current_session}", "images"))
        self.assertEqual(message, "\n".join((
            "From the user (via the Images page): your image is not approved. The feedback follows.",
            "Feedback: The counts are wrong.",
            f"Image: http://127.0.0.1:8765/images/{record['id']}/image.png",
            "Title: Panel counts", "Look for:", "- Check the counts.", "- Check their order.",
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

    def test_feedback_refuses_empty_text_an_unknown_action_and_a_wrong_token(self) -> None:
        record = self.register()
        token = images.request_token(images.console_token(self.root), record["id"])
        for body, reason in ((self.feedback_body(record, "   "), "enter feedback"),
                             (urlencode({"image": record["id"], "token": token, "action": "move"}), "use the form"),
                             (urlencode({"image": record["id"], "token": "wrong", "action": "feedback",
                                         "feedback": "Use a larger title."}), "the image token does not match")):
            with self.subTest(reason=reason), self.assertRaisesRegex(question_record.AnswerRefused, reason):
                self.submit(body)
        self.assertEqual((images.records(self.root), self.sent), ([record], []))
        self.assertFalse(images.decision_root(self.root).exists())

    def test_a_retry_does_not_date_an_older_answer(self) -> None:
        record = self.register()
        identity = record["id"]
        words = "Use a larger title."
        _ = images.send_answer(self.root, identity, "feedback", words)
        key, = image_store.read_answers(images.images_root(self.root), identity)["attempts"]
        images.delete(self.root, identity)
        token = images.request_token(images.console_token(self.root), identity)
        self.submit(urlencode({"image": identity, "token": token, "action": "retry", "retry_key": key}))
        self.assertFalse(images.decision_root(self.root).exists())
        self.submit(self.feedback_body(record, words))
        self.assertEqual(len(list(images.decision_root(self.root).glob(f"{identity}-*.json"))), 1)


class ImageQuestionTests(ImagesFixture):
    def ask(self, record: image_store.ImageRecord, words: str = 'Which check passed?') -> None:
        self.submit(urlencode({'image': record['id'],
            'token': images.request_token(images.console_token(self.root), record['id']),
            'action': 'question', 'feedback': words}))

    def test_image_reply_review_runs_before_the_write_lock(self) -> None:
        record = self.register()
        self.ask(record)
        identity = record["id"]
        store = image_store.ImageQuestionStore(images.images_root(self.root))
        before = store.questions(identity)
        reply = "Step B passed."
        failure = review.RuleFailure("console-standard-5", "Step B")
        with (patch.object(file_lock, "exclusive", wraps=file_lock.exclusive) as locked,
              patch.object(review, "review_question") as reader):
            def reject(*_args: object, **_kwargs: object) -> review.QuestionReviewFailed:
                locked.assert_not_called()
                return review.QuestionReviewFailed(reply, (), (failure,))

            reader.side_effect = reject
            with self.assertRaisesRegex(question_record.AnswerRefused, "console-standard-5: Step B"):
                images.answer_question(self.root, identity, "Q1", reply)
            reader.assert_called_once_with(review.QuestionKey(f"reply/image/{identity}/Q1", ""), reply, console_kind="reply")
        self.assertEqual(store.questions(identity), before)

    def test_feedback_completion_keeps_an_answer_received_during_delivery_visible_and_new(self) -> None:
        for action in ('feedback', 'approved'):
            with self.subTest(action=action):
                record = self.register()
                pages = tuple(page for page in console.read_pages() if page.identity == 'images')
                self.assertEqual(console.news(self.root, pages, 'images'), ())
                self.ask(record)
                self.assertEqual(console.news(self.root, pages, 'changes'), ())

                def answer_during_delivery(recipient: str, sender: str, message: str, *,
                                           key: str, recipient_key: str) -> None:
                    self.assertTrue(key.startswith("review:"))
                    self.assertEqual(recipient_key, self.owner_id())
                    images.answer_question(self.root, record['id'], 'Q1', 'The gate passed.')
                    self.capture(recipient, sender, message)

                with patch.object(change_page, 'send_message', side_effect=answer_during_delivery):
                    _ = images.send_answer(self.root, record['id'], action, 'Check the gate.')
                stored = image_store.read_record(images.images_root(self.root) / record['id'])
                self.assertNotIn('feedback_at', stored)
                content = images.render(self.status).decode()
                self.assertIn(record['id'], content)
                self.assertIn('The gate passed.', content)
                self.assertIn(record['id']+':Q1', images.waiting_images(console.NewsRequest(self.root)))
                self.assertEqual(console.news(self.root, pages, 'changes'), ('images',))
                self.assertEqual(console.news(self.root, pages, 'images'), ())

    def test_repeated_feedback_preserves_a_new_answer_without_resending(self) -> None:
        record = self.register()
        self.ask(record)
        self.sent.clear()
        keys: list[str] = []

        def send(recipient: str, sender: str, message: str, *options: str
                 ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            self.assertEqual(options[options.index("--recipient-key") + 1], self.owner_id())
            key = options[options.index("--key") + 1]
            first = key not in keys
            if first:
                self.capture(recipient, sender, message)
            keys.append(key)
            return question_outcome("sent" if first else "skipped")

        body = urlencode({"image": record["id"], "action": "feedback", "feedback": "Check the gate.",
                          "token": images.request_token(images.console_token(self.root), record["id"])})
        with (patch.object(change_page, "send_message", new=SEND_MESSAGE),
              patch.object(unrouted_alert, "message_outcome", side_effect=send)):
            self.submit(body)
            images.answer_question(self.root, record["id"], "Q1", "The gate passed.")
            self.submit(body)
        self.assertEqual(len(keys), 1)
        self.assertEqual(len(self.sent), 1)
        saved = image_store.read_record(images.images_root(self.root) / record["id"])
        self.assertNotIn("feedback_at", saved)
        self.assertIn("The gate passed.", images.render(self.status).decode())

    def test_feedback_retry_keeps_an_answer_received_after_an_unconfirmed_send(self) -> None:
        record = self.register()
        self.ask(record)
        body = urlencode({"image": record["id"], "action": "feedback", "feedback": "Check the gate.",
                          "token": images.request_token(images.console_token(self.root), record["id"])})
        attempts: list[str] = []
        outcomes: Iterator[Literal["pending", "skipped"]] = iter(("pending", "pending", "pending", "skipped"))

        def send(_recipient: str, _sender: str, _message: str, *, key: str,
                 recipient_key: str) -> Literal["pending", "skipped"]:
            self.assertEqual(recipient_key, self.owner_id())
            attempts.append(key)
            return next(outcomes)

        with patch.object(change_page, "send_message", side_effect=send):
            with self.assertRaisesRegex(images.AnswerNotSent, "has not confirmed"):
                self.submit(body)
            images.answer_question(self.root, record["id"], "Q1", "The gate passed.")
            self.submit(body)
            self.submit(body)
        self.assertEqual(len(attempts), 4)
        self.assertEqual(len(set(attempts)), 1)
        saved = image_store.read_record(images.images_root(self.root) / record["id"])
        self.assertNotIn("feedback_at", saved)
        self.assertIn(f'data-image="{record["id"]}"', images.render(self.status).decode())

    def test_new_approval_sends_once_after_an_answer_reopens_the_image(self) -> None:
        record = self.register()
        identity = record["id"]
        folder = images.images_root(self.root) / identity
        self.ask(record)
        self.sent.clear()
        token = images.console_token(self.root)
        keys: list[str] = []

        def approval() -> str:
            current = image_store.read_record(folder)
            card = images.card(current, token, staff.snapshot(), 3)
            fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', card))
            return urlencode({**fields, "action": "approved"})

        def send(recipient: str, sender: str, message: str, *options: str
                 ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            key = options[options.index("--key") + 1]
            first = key not in keys
            if first:
                self.capture(recipient, sender, message)
            keys.append(key)
            return question_outcome("sent" if first else "skipped")

        with (patch.object(change_page, "send_message", new=SEND_MESSAGE),
              patch.object(unrouted_alert, "message_outcome", side_effect=send)):
            first_post = approval()
            self.submit(first_post)
            self.assertIn("feedback_at", image_store.read_record(folder))
            images.answer_question(self.root, identity, "Q1", "The gate passed.")
            self.assertIn("The gate passed.", images.render(self.status).decode())
            with self.assertRaisesRegex(question_record.AnswerRefused, "This page is stale"):
                self.submit(first_post)
            self.assertNotIn("feedback_at", image_store.read_record(folder))
            fresh_post = approval()
            self.submit(fresh_post)
            self.assertIn("feedback_at", image_store.read_record(folder))
            self.assertNotIn(identity, images.render(self.status).decode())
            self.submit(fresh_post)

        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(list(images.decision_root(self.root).glob(f"{identity}-*.json"))), 2)

    def test_an_old_tab_cannot_restore_an_answer_after_its_round_ends(self) -> None:
        record = self.register()
        identity = record["id"]
        self.ask(record)
        token = images.request_token(images.console_token(self.root), identity)

        def post(words: str, answered: set[str]) -> str:
            return urlencode({"image": identity, "token": token, "action": "feedback", "feedback": words,
                              "feedback_round": images.feedback_round(answered)})

        keep = post("Keep the red row.", set())
        self.submit(keep)
        self.submit(post("Shorten the red row.", set()))
        images.answer_question(self.root, identity, "Q1", "The gate passed.")
        self.submit(post("Remove the red row.", {"Q1"}))
        with self.assertRaisesRegex(question_record.AnswerRefused, "This page is stale"):
            self.submit(keep)
        paths = images.decision_root(self.root).glob(f"{identity}-*.json")
        self.assertEqual(len(list(paths)), 3)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for the picture form")
    def test_browser_keeps_a_stale_draft_for_the_current_round(self) -> None:
        record = self.register("Stale fixture")
        _ = self.register("Other fixture")
        page = images.render(self.status)
        self.ask(record)
        identity = record["id"]
        images.answer_question(self.root, identity, "Q1", "The gate passed.")
        current = images.render(self.status)
        token = images.request_token(images.console_token(self.root), identity)
        current_round = images.feedback_round({"Q1"})
        body = {"image": identity, "token": token, "action": "feedback", "feedback": "Remove the red row.",
                "feedback_round": current_round}
        self.submit(urlencode(body))
        stale = {**body, "feedback": "Keep the red row.", "feedback_round": images.feedback_round(set())}
        with self.assertRaisesRegex(question_record.AnswerRefused, "This page is stale") as refusal:
            self.submit(urlencode(stale))
        answered = images.render(self.status)
        error = json.dumps({"reason": str(refusal.exception)})
        probe = r'''
async (find, whenPageShows, check) => {
 const realFetch = window.fetch.bind(window);
 const posts = [];
 window.fetch = (url, options) => {
  if (url !== location.pathname) return realFetch(url, options);
  if (options?.method === 'POST') {
   posts.push(new URLSearchParams(options.body));
   return Promise.resolve(posts.length === 1 ? new Response(ERROR_BODY, {status: 400}) : new Response('{}'));
  }
  return Promise.resolve(new Response(posts.length === 1 ? FRESH_PAGE : ANSWERED_PAGE));
 };
 const oldList = document.querySelector('[data-images]');
 const stale = find('Stale fixture');
 const form = stale.querySelector('form');
 const words = 'Keep the red row.';
 form.elements.feedback.value = words;
 find('Other fixture').querySelector('textarea').value = 'Keep this draft.';
 form.requestSubmit(form.querySelector('button[value=feedback]'));
 await whenPageShows('the refreshed list', () => !oldList.isConnected);
 const drafts = sessionStorage.getItem('images.answer-drafts:' + location.pathname) ?? '';
 check(drafts.includes(words), 'Storage keeps the draft words.');
 const status = document.getElementById('image-action-failure');
 check(!status.hidden && status.textContent.includes('This page is stale'), 'The page shows the refusal.');
 check(find('Other fixture').querySelector('textarea').value === 'Keep this draft.', 'The other draft remains.');
 if (ALREADY_ANSWERED) {
  check(!document.getElementById(stale.id), 'An answered image has no stale form.');
  return;
 }
 const fresh = find('Stale fixture').querySelector('form');
 check(fresh !== form && fresh.elements.feedback.value === words, 'The current form keeps the draft words.');
 check(fresh.elements.feedback_round.value === CURRENT_ROUND, 'The form uses the current round.');
 if (!fresh.elements.feedback.value) return;
 fresh.requestSubmit(fresh.querySelector('button[value=feedback]'));
 await whenPageShows('the sent form removed', () => !fresh.isConnected);
 check(posts.length === 2 && posts[1].get('feedback') === words && posts[1].get('feedback_round') === CURRENT_ROUND,
       'One click sends the preserved words in the current round.');
 check(posts[1].get('answer_request') && posts[1].get('answer_request') !== posts[0].get('answer_request'),
       'The send uses a fresh request ID.');
}'''
        for already_answered in (False, True):
            with self.subTest(already_answered=already_answered):
                fresh = answered if already_answered else current
                browser_probe = (probe.replace("ERROR_BODY", json.dumps(error))
                                 .replace("ALREADY_ANSWERED", json.dumps(already_answered))
                                 .replace("CURRENT_ROUND", json.dumps(current_round))
                                 .replace("FRESH_PAGE", json.dumps(fresh.decode()).replace("<", r"\u003c"))
                                 .replace("ANSWERED_PAGE", json.dumps(answered.decode()).replace("<", r"\u003c")))
                script = f"<script>{self.question_probe(browser_probe)}</script>".encode()
                self.assertEqual(firefox_probe_results(page.replace(b"</body>", script + b"</body>")), [[]])

    def test_confirmed_feedback_retry_hides_the_image_after_a_failed_write(self) -> None:
        record = self.register()
        self.ask(record)
        images.answer_question(self.root, record["id"], "Q1", "The gate passed.")
        body = urlencode({"image": record["id"], "action": "feedback", "feedback": "Check the gate.",
                          "token": images.request_token(images.console_token(self.root), record["id"])})
        folder = images.images_root(self.root) / record["id"]
        with (patch.object(change_page, "send_message", new=SEND_MESSAGE),
              patch.object(unrouted_alert, "message_outcome",
                           return_value=question_outcome("sent")) as send):
            with (patch.object(image_store, "write_record", side_effect=OSError("The feedback write failed.")),
                  self.assertRaisesRegex(OSError, "The feedback write failed.")):
                self.submit(body)
            self.assertNotIn("feedback_at", image_store.read_record(folder))
            self.assertIn("The gate passed.", images.render(self.status).decode())
            self.submit(body)
        self.assertEqual(send.call_count, 1)
        self.assertIn("feedback_at", image_store.read_record(folder))
        self.assertNotIn(record["id"], images.render(self.status).decode())

    def test_startup_resumes_an_image_question_after_the_sender_stops(self) -> None:
        record = self.register()
        delivery = images.ask_question(self.root, record["id"], "Which check passed?", self.status)
        path = self.status.path(delivery.page, delivery.row)
        with (patch.object(change_page, "send_question", side_effect=SystemExit),
              self.assertRaises(SystemExit)):
            change_page.deliver(delivery, self.status)
        sending = change_page.read_question_attempt(path)
        self.assertEqual(sending.state, delivery_state.QuestionSendState.SENDING)
        transport = FakeQuestionTransport()
        with patch.object(change_page, "send_question", side_effect=transport.send):
            for _ in range(2):
                change_page.recover_review_messages(change_page.DeliveryStatus(self.root))
        self.assertEqual(transport.calls, [sending.message_id])
        restored = change_page.read_question_attempt(path)
        self.assertEqual(restored.state, delivery_state.QuestionSendState.SENT)
        self.assertEqual(restored.attempt_id, sending.attempt_id)

    def test_feedback_started_after_an_answer_still_hides_the_image(self) -> None:
        record = self.register()
        self.ask(record)
        images.answer_question(self.root, record['id'], 'Q1', 'The gate passed.')
        _ = images.send_answer(self.root, record['id'], 'feedback', 'Use a larger title.')
        stored = image_store.read_record(images.images_root(self.root) / record['id'])
        self.assertIn('feedback_at', stored)
        self.assertNotIn(record['id'], images.render(self.status).decode())
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset())

    def test_an_asked_image_leaves_the_page_and_its_answer_returns_as_new(self) -> None:
        record = self.register('Check <the> gate', look_for=('Check the first row.',))
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
        self.assertNotIn(f'data-image="{record["id"]}"', card)
        self.assertNotIn('Which check passed?', card)
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset())
        self.assertEqual(console.news(self.root, pages, 'changes'), ())
        assert self.owner is not None
        self.assertEqual(self.sent[0][0], 'session:'+self.owner.current_session)
        self.assertIn(f"Images {record['id']}: Ask question", self.sent[0][2])
        self.assertIn("Check <the> gate\nLook for:\n- Check the first row.\n", self.sent[0][2])
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
        for action in ('approved', 'feedback', 'question'):
            self.assertIn(f'name="action" value="{action}"', card)
        self.assertNotIn('value="not approved"', card)
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

    def test_a_card_returns_after_all_its_questions_have_answers_in_numeric_order(self) -> None:
        record = self.register()
        for number in range(1, 11):
            self.ask(record, f'Question number {number}?')
        images.answer_question(self.root, record['id'], 'Q2', 'Second answer')
        images.answer_question(self.root, record['id'], 'Q1', 'First answer')
        source = images.render(self.status).decode()
        self.assertEqual(len(self.sent), 10)
        self.assertEqual(len(image_store.ImageQuestionStore(images.images_root(self.root)).questions(record['id'])), 10)
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset())
        self.assertNotIn(f'data-image="{record["id"]}"', source)
        for number in reversed(range(3, 11)):
            images.answer_question(self.root, record['id'], f'Q{number}', f'Answer number {number}.')
        source = images.render(self.status).decode()
        positions = [source.index(f'data-question="Q{number}"') for number in range(1, 11)]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('<h3>Answer</h3><p>First answer</p>', source)
        self.assertIn('<h3>Answer</h3><p>Second answer</p>', source)
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset((record['id']+':Q10',)))

    def test_a_failed_question_send_keeps_the_record_and_retries_the_same_question(self) -> None:
        record = self.register()
        with (patch.object(change_page, "send_question", return_value=question_outcome("refused", "owner offline")) as send,
              patch.object(change_page, "alert_delivery_failures")):
            self.ask(record)
        self.assertEqual(send.call_count, 3)
        content = images.render(self.status).decode()
        self.assertEqual(content.count(change_page.question_failure_html(True)), 1)
        self.assertIn(f'data-image="{record["id"]}"', content)
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset((record['id'],)))
        paths = list(self.root.glob(change_page.DELIVERY_PREFIX+'*'))
        self.assertEqual(len(paths), 1)
        saved = change_page.read_question_attempt(paths[0])
        self.assertEqual(saved.state, delivery_state.QuestionSendState.FAILED)
        self.assertEqual(saved.error, 'owner offline')
        self.status: change_page.DeliveryStatus = change_page.DeliveryStatus(self.root)
        pending = images.ask_question(self.root, record['id'], 'Which check passed?', self.status)
        content = images.render(self.status).decode()
        self.assertNotIn(f'data-image="{record["id"]}"', content)
        self.assertIn(f'data-waiting-images="{record["id"]}"', content)
        change_page.deliver(pending, self.status)
        self.assertEqual(change_page.read_question_attempt(paths[0]).state, delivery_state.QuestionSendState.SENT)
        self.ask(record)
        self.assertEqual(len(self.sent), 1)
        questions = image_store.ImageQuestionStore(images.images_root(self.root)).questions(record['id'])
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].question_id, 'Q1')
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset())

    def test_startup_waits_for_the_sender_lock_before_recovery(self) -> None:
        record = self.register()
        delivery = images.ask_question(self.root, record['id'], 'Which check passed?', self.status)
        publish = change_page.publish_question_attempt

        def interrupt_after_send(path: Path, attempt: delivery_state.QuestionAttempt) -> None:
            if self.sent:
                raise OSError('Cannot write the send confirmation.')
            publish(path, attempt)

        with patch.object(change_page, 'publish_question_attempt', side_effect=interrupt_after_send):
            with self.assertRaisesRegex(OSError, 'Cannot write the send confirmation'):
                change_page.deliver(delivery, self.status)

        self.status = change_page.DeliveryStatus(self.root)
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        with file_lock.exclusive(Path(cast(str, delivery.job.lock.name))):
            change_page.recover_review_messages(self.status)
        sending = change_page.read_question_attempt(self.status.path(delivery.page, delivery.row))
        self.assertEqual(sending.attempt_id, delivery.attempt)
        self.assertEqual(sending.state, delivery_state.QuestionSendState.SENDING)
        self.assertEqual(len(self.sent), 1)
        content = images.render(self.status).decode()
        self.assertIn('Which check passed?', content)
        self.assertNotIn('Question not delivered.', content)
        self.assertEqual(images.waiting_images(console.NewsRequest(self.root)), frozenset((record['id'],)))
        change_page.recover_review_messages(self.status)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(image_store.ImageQuestionStore(images.images_root(self.root)).questions(record['id'])), 1)

    def test_recovery_refusals_keep_the_first_send_result_unknown(self) -> None:
        record = self.register()
        delivery = images.ask_question(self.root, record['id'], 'Which check passed?', self.status)
        publish = change_page.publish_question_attempt

        def fail_confirmation(path: Path, attempt: delivery_state.QuestionAttempt) -> None:
            if self.sent:
                raise OSError('Cannot write the send confirmation.')
            publish(path, attempt)

        with patch.object(change_page, 'publish_question_attempt', side_effect=fail_confirmation):
            with self.assertRaisesRegex(OSError, 'Cannot write the send confirmation'):
                change_page.deliver(delivery, self.status)

        self.status = change_page.DeliveryStatus(self.root)
        with patch.object(change_page, "send_question",
                          return_value=question_outcome("refused", "owner offline")) as send:
            change_page.recover_review_messages(self.status)
            self.assertEqual(send.call_count, 3)
            self.ask(record)
            self.assertEqual(send.call_count, 3)
        path = self.status.path(delivery.page, delivery.row)
        saved = change_page.read_question_attempt(path)
        self.assertEqual(saved.state, delivery_state.QuestionSendState.UNKNOWN)
        self.assertTrue(saved.earlier_unconfirmed)
        self.assertTrue(self.status.has_deferred_questions())
        self.assertNotIn("Question not delivered.", images.render(self.status).decode())
        with patch.object(change_page, "send_question", return_value=question_outcome("skipped")) as recover:
            change_page.recover_deferred_questions(self.status)
            change_page.recover_deferred_questions(self.status)
        recover.assert_called_once_with(saved.recipient_id, saved.message, saved.message_id)
        self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)
        self.assertEqual(len(self.sent), 1)
        questions = image_store.ImageQuestionStore(images.images_root(self.root)).questions(record['id'])
        self.assertEqual([question.question_id for question in questions], ['Q1'])

    def test_ready_write_failure_keeps_the_saved_question_and_shows_a_notice(self) -> None:
        record = self.register()
        publish = change_page.publish_question_attempt

        def fail_intent_update(path: Path, attempt: delivery_state.QuestionAttempt) -> None:
            if attempt.state is delivery_state.QuestionSendState.READY:
                raise OSError('Cannot update the send intent.')
            publish(path, attempt)

        with patch.object(change_page, 'publish_question_attempt', side_effect=fail_intent_update):
            self.ask(record)

        path, = self.root.glob(change_page.DELIVERY_PREFIX + '*')
        failed = change_page.read_question_attempt(path)
        self.assertEqual(failed.state, delivery_state.QuestionSendState.FAILED)
        self.assertEqual(failed.error, 'Cannot update the send intent.')
        self.assertEqual(self.sent, [])
        self.assertIn('Question not delivered.', images.render(self.status).decode())

        self.status = change_page.DeliveryStatus(self.root)
        change_page.recover_review_messages(self.status)
        self.assertEqual(self.sent, [])
        self.ask(record)
        self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('--question Q1', self.sent[0][2])
        self.assertNotIn('Question not delivered.', images.render(self.status).decode())
        questions = image_store.ImageQuestionStore(images.images_root(self.root)).questions(record['id'])
        self.assertEqual([question.question_id for question in questions], ['Q1'])

    def test_pending_send_retries_with_the_saved_message_id(self) -> None:
        record = self.register()
        with patch.object(change_page, 'send_question', return_value=question_outcome('pending')) as send:
            self.ask(record)
        path, = self.root.glob(change_page.DELIVERY_PREFIX + '*')
        attempt = change_page.read_question_attempt(path)
        self.assertEqual(attempt.state, delivery_state.QuestionSendState.UNKNOWN)
        self.assertEqual(send.call_args_list, [call(self.owner_id(), attempt.message, attempt.message_id)] * 3)
        self.assertTrue(self.status.has_deferred_questions())

        self.status = change_page.DeliveryStatus(self.root)
        with patch.object(change_page, 'send_question', return_value=question_outcome('skipped')) as recover:
            change_page.recover_review_messages(self.status)
            change_page.recover_review_messages(self.status)
        recover.assert_called_once_with(self.owner_id(), attempt.message, attempt.message_id)
        self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)

    def test_malformed_question_delivery_metadata_renders_as_unknown(self) -> None:
        record = self.register()
        other = self.register('An image without a question')
        delivery = images.ask_question(self.root, record['id'], 'Which check passed?', self.status)
        change_page.deliver(delivery, self.status)
        path = self.status.path(delivery.page, delivery.row)

        for saved in (None, {'source': str(delivery.page.review_list)}):
            with self.subTest(saved=saved):
                _ = path.write_text(json.dumps({'review_message': saved}))
                content = images.render(self.status).decode()
                self.assertIn(f'data-image="{record["id"]}"', content)
                self.assertIn(f'data-image="{other["id"]}"', content)
                self.assertNotIn('Question not delivered.', content)
                self.assertIn('<h3>Question Q1 — waiting for an answer</h3><p>Which check passed?</p>', content)
                self.assertEqual(images.waiting_images(console.NewsRequest(self.root)),
                                 frozenset((record['id'], other['id'])))

    def test_an_unreadable_sender_with_a_held_lock_prevents_a_second_send(self) -> None:
        record = self.register()
        words = 'Which check passed?'
        delivery = images.ask_question(self.root, record['id'], words, self.status)
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        with patch.object(process_start, "process_start", return_value=process_start.Unreadable("Cannot read the sender.")):
            content = images.render(self.status).decode()
            self.assertIn(words, content)
            self.assertNotIn('Question not delivered.', content)
            self.assertNotIn('Resend is unavailable until the sender can be checked.', content)
            self.assertNotIn('Enter the same words and press Ask question to send it again.', content)
            self.ask(record, words)
            self.assertEqual(self.sent, [])
        _ = delivery.job.release()
        content = images.render(self.status).decode()
        self.assertNotIn('Question not delivered.', content)
        change_page.recover_review_messages(self.status)
        self.assertEqual(len(self.sent), 1)

    def test_a_failed_send_mark_records_failure_before_transport_runs(self) -> None:
        record = self.register()
        delivery = images.ask_question(self.root, record['id'], 'Which check passed?', self.status)
        with (patch.object(atomic_file, 'sync_folder', side_effect=OSError('Cannot sync the send mark.')),
              redirect_stderr(StringIO()) as output):
            change_page.deliver(delivery, self.status)
        path = self.status.path(delivery.page, delivery.row)
        self.assertIs(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.FAILED)
        self.assertIn("Cannot sync the send mark.", output.getvalue())
        change_page.recover_review_messages(self.status)
        self.assertEqual(self.sent, [])
        self.assertIn("Question not delivered.", images.render(self.status).decode())

    def test_startup_recovers_after_a_failed_failure_receipt(self) -> None:
        record = self.register()
        delivery = images.ask_question(self.root, record['id'], 'Which check passed?', self.status)
        publish = change_page.publish_question_attempt

        def fail_result(path: Path, attempt: delivery_state.QuestionAttempt) -> None:
            if attempt.state is delivery_state.QuestionSendState.FAILED:
                raise OSError("Cannot write the failure.")
            publish(path, attempt)

        with (patch.object(change_page, "send_question", return_value=question_outcome("refused", "owner offline")) as send,
              patch.object(change_page, "publish_question_attempt", side_effect=fail_result),
              redirect_stderr(StringIO()) as errors):
            change_page.deliver(delivery, self.status)
        self.assertEqual(send.call_count, 3)
        self.assertTrue(self.status.has_deferred_questions())
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("Cannot write the failure.", errors.getvalue())
        self.status = change_page.DeliveryStatus(self.root)
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        with file_lock.exclusive(Path(cast(str, delivery.job.lock.name))):
            change_page.recover_review_messages(self.status)
        self.assertEqual(self.sent, [])

        change_page.recover_review_messages(self.status)
        questions = image_store.ImageQuestionStore(images.images_root(self.root)).questions(record['id'])
        self.assertEqual([question.question_id for question in questions], ['Q1'])
        self.assertEqual(len(self.sent), 1)
        self.assertIn('--question Q1', self.sent[0][2])

    def test_empty_questions_and_unknown_or_answered_ids_preserve_the_image(self) -> None:
        record = self.register()
        with self.assertRaisesRegex(question_record.AnswerRefused, 'Enter a question first'):
            self.ask(record, '  ')
        self.assertEqual((images.records(self.root), self.sent), ([record], []))
        self.ask(record)
        with redirect_stderr(StringIO()) as error:
            self.assertEqual(images.main(['answer', '--id', record['id'], '--question', 'Q9', '--text', 'Wrong']), 1)
        self.assertIn('Unknown question id', error.getvalue())
        images.answer_question(self.root, record['id'], 'Q1', 'First answer')
        before = images.records(self.root)
        with self.assertRaisesRegex(question_record.AnswerRefused, 'already has an answer'):
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
    def test_a_direct_link_to_a_waiting_image_shows_only_the_wait_status(self) -> None:
        _ = self.register('Another image')
        probe = r'''
async (find, whenPageShows, check) => {
 const shown = element => !!element?.getClientRects().length;
 const status = document.querySelector('[data-image-waiting]');
 check(shown(status) && status.textContent === 'The owner will answer this',
       'The waiting link shows one line.');
 check(![...document.querySelectorAll('[data-images] button')].some(shown),
       'The waiting link shows no action buttons.');
 check(![...document.querySelectorAll('[data-image]')].some(shown), 'The waiting link shows no cards.');
 await fetch('/fixture-answer', {method:'POST'});
 document.dispatchEvent(new Event('visibilitychange'));
 await whenPageShows('the returned image', () => shown(find('Question fixture')));
 check(!shown(status), 'The waiting line clears when the answer returns.');
}'''
        _ = self.run_question_browser(self.question_probe(probe), waiting_link=True)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser questions')
    def test_browser_removes_the_card_and_recounts_until_the_owner_answers(self) -> None:
        _ = self.register('Another image')
        probe = r'''
async (find, whenPageShows, check) => {
 const postFixture = path => fetch(path, {method:'POST', signal:AbortSignal.timeout(20000)});
 const refresh = () => {
  const list = document.querySelector('[data-images]');
  const replaced = whenPageShows('the refreshed image list', () => !list.isConnected);
  document.dispatchEvent(new Event('visibilitychange'));
  return replaced;
 };
 const card = () => find('Question fixture');
 const ask = () => card().querySelector('button[value="question"]');
 const count = () => document.querySelector('.group-count')?.textContent;
 const form = card().querySelector('form');
 check(count() === '2', 'The owner count includes both images.');
 form.requestSubmit(ask());
 check(form.querySelector('[data-image-status]').textContent === 'Enter a question first.',
       'An empty question gives its reason.');
 form.elements.feedback.value = 'Which check passed?';
 form.requestSubmit(ask());
 check(form.isConnected, 'The card stays until the request saves.');
 await whenPageShows('the card removed', () => !card());
 check(count() === '1', 'The owner count drops as soon as the card leaves.');
 check((await postFixture('/fixture-delivery')).ok, 'The fixture completes delivery.');
 await refresh();
 check(!card() && count() === '1', 'Delivery alone leaves the card out of the count.');
 check((await postFixture('/fixture-answer')).ok, 'The fixture records the answer.');
 await Promise.all([whenPageShows('the answered card', card), refresh()]);
 const question = card().querySelector('.page-question').textContent;
 check(question.includes('Which check passed?') && question.includes('The gate passed.'),
       'The returned card shows the question and answer.');
 check(!question.includes('waiting for an answer'), 'The returned card clears the waiting line.');
 check(card().querySelector('img') !== null, 'The image returns with the answer.');
 check(!card().querySelector('[data-question] [role="status"]'), 'The answer has no delivery notice.');
 const actions = [...card().querySelectorAll('button[name="action"]:enabled')].map(button => button.value);
 check(actions.join() === 'approved,feedback,question', 'All three answer buttons return.');
 check(count() === '2', 'The owner count includes the returned card.');
 card().querySelector('form').elements.feedback.value = 'Which check failed?';
 card().querySelector('form').requestSubmit(ask());
 await whenPageShows('the card removed again', () => !card());
 check((await postFixture('/fixture-failure')).ok, 'The fixture records the delivery failure.');
 await refresh();
 await whenPageShows('the card with the failed question', card);
 check(count() === '2', 'The owner count includes the card after a delivery failure.');
 check(card().querySelector('[data-question="Q2"] [role="status"]').textContent ===
       'Question not delivered.', 'The card shows the delivery failure.');
 card().querySelector('form').elements.feedback.value = 'Which check failed?';
 card().querySelector('form').requestSubmit(ask());
 await whenPageShows('the card removed after resend', () => !card());
 check((await postFixture('/fixture-resend')).ok, 'The fixture records the successful resend.');
 await refresh();
 check(!card() && count() === '1', 'The successful resend keeps the card out of the count.');
}'''
        requests = self.run_question_browser(self.question_probe(probe))
        self.assertEqual([(request['action'], request['feedback']) for request in requests],
                         [(['question'], ['Which check passed?']), (['question'], ['Which check failed?']),
                          (['question'], ['Which check failed?'])])
        self.assertEqual(len(self.sent), 3)
        self.assertIn('Change review delivery needs attention', self.sent[1][2])
        self.assertIn('--question Q2', self.sent[2][2])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser questions')
    def test_browser_removes_a_saved_question_after_an_incomplete_success_body(self) -> None:
        probe = r'''
async (find, whenPageShows, check) => {
 const form = find('Question fixture').querySelector('form');
 form.elements.feedback.value = 'Was the gate checked?';
 form.requestSubmit(form.querySelector('button[value="question"]'));
 await whenPageShows('the card removed', () => !find('Question fixture'));
 check(form.querySelector('[data-image-status]').textContent === '', 'The form had no error.');
 check((await fetch('/fixture-delivery', {method:'POST'})).ok, 'The fixture completes delivery.');
}'''
        requests = self.run_question_browser(self.question_probe(probe), response_body=b'{"state":')
        self.assertEqual([(request['action'], request['feedback']) for request in requests],
                         [(['question'], ['Was the gate checked?'])])
        self.assertEqual(len(self.sent), 1)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser questions')
    def test_timed_refresh_keeps_focus_and_selection_on_another_image(self) -> None:
        _ = self.register('Feedback fixture')
        probe = r'''
async (find, whenPageShows, check, controlRefresh) => {
 const tickRefresh = controlRefresh();
 const other = find('Feedback fixture');
 const question = find('Question fixture').querySelector('form');
 question.elements.feedback.value = 'Is this ready?';
 question.requestSubmit(question.querySelector('button[value="question"]'));
 await whenPageShows('the refreshed list without the asked card', () => !other.isConnected && !find('Question fixture'));
 const draft = () => find('Feedback fixture').querySelector('textarea');
 const old = draft();
 old.value = 'Keep editing this draft';
 old.focus();
 old.setSelectionRange(2, 9, 'backward');
 await Promise.all([
  whenPageShows('the replacement draft field', () => !old.isConnected),
  tickRefresh()
 ]);
 const fresh = draft();
 check(document.activeElement === fresh, 'The timed refresh keeps focus on the other card.');
 check(fresh.value === 'Keep editing this draft', 'The timed refresh keeps the draft.');
 check(fresh.selectionStart === 2 && fresh.selectionEnd === 9 && fresh.selectionDirection === 'backward',
       'The timed refresh keeps the selection and its direction.');
 fresh.setSelectionRange(4, 4);
 document.dispatchEvent(new Event('visibilitychange'));
 await whenPageShows('the visibility refresh', () => !fresh.isConnected);
 check(document.activeElement === draft() && draft().selectionStart === 4 && draft().selectionEnd === 4,
       'The visibility refresh keeps the caret.');
 // The visibility refresh still has to present its result on two render frames.
 const visible = draft();
 await Promise.all([
  whenPageShows('a timed refresh after the active visibility refresh', () => !visible.isConnected),
  tickRefresh()
 ]);
 check((await fetch('/fixture-delivery', {method: 'POST'})).ok, 'The fixture completes delivery.');
}'''
        _ = self.run_question_browser(self.question_probe(probe))

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser questions')
    def test_a_failed_send_result_write_keeps_the_card_out_and_does_not_resend(self) -> None:
        probe = r'''
async (find, whenPageShows, check) => {
 const card = () => find('Question fixture');
 const form = card().querySelector('form');
 const button = form.querySelector('button[value="question"]');
 form.elements.feedback.value = 'Is this ready?';
 const body = new URLSearchParams(new FormData(form, button));
 form.requestSubmit(button);
 await whenPageShows('the card removed', () => !card());
 check((await fetch('/fixture-delivery', {method: 'POST'})).ok, 'The fixture interrupts the result write.');
 check(!card(), 'A failed result write does not return the card.');
 check((await fetch(location.pathname, {method:'POST', body})).ok, 'The repeated request succeeds.');
 await fetch('/fixture-answer', {method:'POST'});
 document.dispatchEvent(new Event('visibilitychange'));
 await whenPageShows('the answered card', card);
 check(document.querySelectorAll('[data-question]').length === 1, 'The repeated request preserves the question ID.');
 check(document.querySelector('[data-images]').dataset.refreshSeconds === '0', 'An answer stops the refresh timer.');
}'''
        requests = self.run_question_browser(self.question_probe(probe), fail_result_write=True)
        self.assertEqual([request['feedback'] for request in requests], [['Is this ready?'], ['Is this ready?']])
        self.assertEqual(len(self.sent), 1)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser questions')
    def test_successful_question_retries_the_first_failed_refresh(self) -> None:
        probe = r'''
async (find, whenPageShows, check, controlRefresh) => {
 const tickRefresh = controlRefresh();
 const card = () => find('Question fixture');
 const form = card().querySelector('form');
 const status = () => document.querySelector('[data-page-status]').dataset.state;
 form.elements.feedback.value = 'Is this ready?';
 form.requestSubmit(form.querySelector('button[value="question"]'));
 await whenPageShows('the first refresh error', () => status() === 'Failed');
 check(!card(), 'The refresh failure does not return a saved card.');
 await Promise.all([
  whenPageShows('the page status after it retries', () => status() === 'Live'),
  tickRefresh()
 ]);
 check(!card(), 'The successful refresh still leaves the card out.');
 check((await fetch('/fixture-delivery', {method: 'POST'})).ok, 'The fixture completes delivery.');
}'''
        _ = self.run_question_browser(self.question_probe(probe), fail_first_refresh=True)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser actions')
    def test_successful_delete_shows_refresh_failure_outside_the_removed_card(self) -> None:
        probe = r'''
async (find, whenPageShows, check) => {
 const card = find('Question fixture'), form = card.querySelector('form');
 const notice = document.getElementById('image-action-failure');
 // A post without an action is the legacy delete.
 form.requestSubmit();
 await whenPageShows('the refresh failure', () => !notice.hidden);
 check(notice.textContent === 'Update failed: HTTP 503', 'The page notice shows the refresh error.');
 check(!card.isConnected, 'The deleted card stays out of the list.');
 check(form.querySelector('[data-image-status]').hidden, 'The removed card has no failure notice.');
 document.dispatchEvent(new Event('visibilitychange'));
 await whenPageShows('the cleared notice', () => notice.hidden);
 check(!find('Question fixture'), 'The refreshed list also leaves the deleted card out.');
}'''
        requests = self.run_question_browser(self.question_probe(probe), fail_first_refresh=True)
        self.assertEqual([request.get('action') for request in requests], [None])

    @staticmethod
    def question_probe(check_page: str) -> str:
        return r'''
(async () => {
 const failures = [];
 const check = (ok, message) => {if (!ok) failures.push(message);};
 const find = title => [...document.querySelectorAll('[data-image]')]
  .find(card => card.querySelector('.image-title').textContent === title);
 const browserNow = performance.now.bind(performance);
 const waitLimit = 12000;
 const whenPageShows = (name, predicate) => new Promise((resolve, reject) => {
  const observer = new MutationObserver(read);
  const timeout = setTimeout(() => {
   observer.disconnect();
   reject(new Error('The page did not show ' + name + '.'));
  }, waitLimit);
  function stop() {
   observer.disconnect();
   clearTimeout(timeout);
  }
  function read() {
   try {
    if (!predicate()) return;
    stop();
    resolve();
   } catch (error) {
    stop();
    reject(error);
   }
  }
  observer.observe(document.body, {attributes: true, childList: true, characterData: true, subtree: true});
  read();
 });
 const controlRefresh = () => {
  // Capture only the image refresh timer. Leave the page status clock active.
  const period = Number(document.querySelector('[data-images]').dataset.answerRefreshSeconds) * 1000;
  const startInterval = window.setInterval.bind(window);
  const stopInterval = window.clearInterval.bind(window);
  let nextId = 0, elapsed = 0, timer = null;
  performance.now = () => browserNow() + elapsed;
  window.setInterval = (run, delay) => {
   if (delay !== period) return startInterval(run, delay);
   timer = {id: --nextId, run};
   return timer.id;
  };
  window.clearInterval = id => {
   if (timer?.id === id) timer = null;
   else stopInterval(id);
  };
  function startRefresh() {
   if (!timer) throw new Error('The page did not schedule a refresh.');
   const fetchFromPage = window.fetch;
   let started = false;
   window.fetch = (url, options) => {
    if (url === location.pathname && options?.cache === 'no-store') started = true;
    return fetchFromPage.call(window, url, options);
   };
   try {
    elapsed += period;
    timer.run();
   } finally {
    window.fetch = fetchFromPage;
   }
   return started;
  }
  return async () => {
   // An active refresh can skip a tick until it presents its result.
   const deadline = browserNow() + waitLimit;
   while (browserNow() < deadline) {
    if (startRefresh()) return;
    await new Promise(resolve => requestAnimationFrame(resolve));
   }
   throw new Error('The scheduled refresh did not start.');
  };
 };
 try {await (CHECK_PAGE)(find, whenPageShows, check, controlRefresh);}
 catch (error) {failures.push(String(error));}
 await fetch('/results', {method: 'POST', body: JSON.stringify(failures)});
})();'''.replace('CHECK_PAGE', check_page)

    def run_question_browser(self, probe: str, *, response_body: bytes = b'{"state":"done"}',
                             fail_first_refresh: bool = False, fail_result_write: bool = False,
                             waiting_link: bool = False
                             ) -> list[dict[str, list[str]]]:
        record = self.register('Question fixture')
        root = self.root
        identity = record['id']
        if waiting_link:
            self.ask(record)
        place = images.PAGE_PATH + (f'#image={identity}' if waiting_link else '')
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
        resend_done = threading.Event()

        def fail_result_sync(folder: Path) -> None:
            for path in folder.glob(change_page.DELIVERY_PREFIX + "*"):
                if change_page.read_question_attempt(path).state is delivery_state.QuestionSendState.SENT:
                    raise OSError("Cannot sync the delivery result.")
            sync(folder)

        sync = atomic_file.sync_folder

        class Probe(BaseHTTPRequestHandler):
            timeout: ClassVar[float | None] = 5

            def do_GET(self) -> None:
                if self.path == '/complete':
                    _ = complete.wait(TEST_HANG_GUARD_SECS)
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
                    _ = self.wfile.write(('<iframe src="'+place+'" width="1100" height="800"></iframe>'
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
                elif self.path == '/fixture-resend':
                    if not resend_done.wait(5):
                        self.send_error(504, 'The fixture resend did not finish.')
                        return
                else:
                    requests.append(parse_qs(payload))
                    result = images.submit(root, payload, delivery_status)
                    if isinstance(result, change_page.MessageDelivery):
                        delivery = result
                        body = response_body
                        is_first_delivery = len(requests) == 1
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                try:
                    _ = self.wfile.write(body)
                    self.wfile.flush()
                finally:
                    if is_first_delivery:
                        if first_delivery_released.wait(TEST_HANG_GUARD_SECS):
                            if fail_result_write:
                                with patch.object(atomic_file, "sync_folder", side_effect=fail_result_sync):
                                    change_page.deliver(delivery, delivery_status)
                            else:
                                change_page.deliver(delivery, delivery_status)
                            first_delivery_done.set()
                    elif isinstance(delivery, change_page.MessageDelivery) and delivery.row.endswith('/Q2') and not second_delivery_done.is_set():
                        with patch.object(change_page, "send_question",
                                          return_value=question_outcome("refused", "The owner is unavailable.")):
                            change_page.deliver(delivery, delivery_status)
                        second_delivery_done.set()
                    else:
                        change_page.deliver(delivery, delivery_status)
                        if isinstance(delivery, change_page.MessageDelivery) and delivery.row.endswith('/Q2'):
                            resend_done.set()

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        def release() -> None:
            complete.set()
            first_delivery_released.set()

        self.run_firefox(Probe, 'question-browser', timeout=60, release=release)
        self.assertEqual(results, [[]])
        return requests


class ImagePairTests(ImagesFixture):
    def register_pair(self, title: str = "Panel counts") -> image_store.ImageRecord:
        after = self.temporary / "after.webp"
        _ = after.write_bytes(PNG[::-1])
        return images.register(after, title, self.owner_id(), "Phase image-pairs", before=self.shot)

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
            _ = images.register(after, "Cards", self.owner_id(), "Phase image-pairs", before=self.shot)
        self.assertEqual(list((self.root / image_store.IMAGES_FOLDER).iterdir()), [])
        self.assertEqual(images.records(self.root), [])

    def test_a_pair_with_a_bad_before_image_leaves_no_folder(self) -> None:
        text = self.temporary / "notes.txt"
        _ = text.write_text("not an image")
        for before, reason in ((text, "use one of"), (self.temporary / "absent.png", "no such image file")):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                _ = images.register(self.shot, "Cards", self.owner_id(), "Phase image-pairs", before=before)
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
            code = images.main(["register", "--before", str(self.shot), "--after", str(after), "--title", "Cards",
                                "--look-for", "Check the row count.",
                                "--owner", self.owner_id(), "--phase", "image-pairs"])
        self.assertEqual(code, 0)
        record = images.records(self.root)[0]
        self.assertEqual((record["title"], record["look_for"]), ("Cards", ["Check the row count."]))
        origin = f"http://127.0.0.1:8765/images/{record['id']}"
        self.assertEqual(output.getvalue().splitlines(), [
            f"registered {record['id']}: {origin}/before.png {origin}/after.png",
            "Image link: http://127.0.0.1:8765/console/?page=images&at.images="
            + f"%2Fconsole%2Fimages.html%23image%3D{record['id']}"])
        for options in (["--before", str(self.shot)], ["--image", str(self.shot), "--after", str(after)], []):
            with self.subTest(options=options), redirect_stderr(StringIO()) as errors, \
                    self.assertRaises(SystemExit):
                _ = images.main(["register", *options, "--title", "Cards", "--owner", self.owner_id(),
                                 "--phase", "image-pairs"])
            self.assertIn("give --image, or --before and --after", errors.getvalue())
        self.assertEqual(images.records(self.root), [record])

    def test_a_pair_card_shows_before_then_after_with_one_form(self) -> None:
        record = self.register_pair("Look at <the> counts")
        card = images.card(record, "token", staff.snapshot(), 3)
        before, after = (f"/images/{record['id']}/before.png", f"/images/{record['id']}/after.webp")
        self.assertTrue(card.startswith(f'<article id="image-{record['id']}" class="pair" data-image='))
        self.assertIn(
            '<div class="image-pair" data-image-zoom-group>'
            + '<figure><figcaption>Before</figcaption>'
            + f'<a data-image-zoom data-image-zoom-label="Before" href="{before}" target="_blank" rel="noopener">'
            + f'<img src="{before}" alt="Before: Look at &lt;the&gt; counts" loading="lazy"></a></figure>'
            + '<figure><figcaption>After</figcaption>'
            + f'<a data-image-zoom data-image-zoom-label="After" href="{after}" target="_blank" rel="noopener">'
            + f'<img src="{after}" alt="After: Look at &lt;the&gt; counts" loading="lazy"></a></figure></div>'
            + '<form method="post" data-image-form>', card)
        self.assertEqual((card.count("<form"), card.count('value="approved"'), card.count('value="feedback"'),
                          card.count("<h2"), card.count("<dl")), (1, 1, 1, 1, 1))
        page = images.render(self.status).decode()
        self.assertEqual(page.count(page_frame.ZOOM_STYLE), 1)
        self.assertEqual(page.count(page_frame.ZOOM_SCRIPT), 1)
        # Hover or keyboard focus shows an arrow button's tip; a turned-off button shows none.
        focus_tip = "[data-image-zoom-step]:not([disabled]):focus-visible [data-image-zoom-tip]{visibility:visible}"
        hover_tip = ("@media (hover:hover){[data-image-zoom-step]:not([disabled]):hover [data-image-zoom-tip]"
                     "{visibility:visible}}")
        self.assertIn(focus_tip, page_frame.ZOOM_STYLE)
        self.assertIn(hover_tip, page_frame.ZOOM_STYLE)
        self.assertNotIn("<figure>", images.card(self.register(), "token", staff.snapshot(), 3))
        # A narrow card stacks the pair, before on top.
        self.assertIn("@container (max-width:599px){.image-pair{grid-template-columns:minmax(0,1fr)}}", images.STYLE)
        self.submit(urlencode({"image": record["id"], "action": "question", "feedback": "What changed?",
                               "token": images.request_token(images.console_token(self.root), record["id"])}))
        page = images.render(self.status).decode()
        self.assertNotIn(before, page)
        self.assertNotIn(after, page)
        images.answer_question(self.root, record["id"], "Q1", "The counts now match.")
        page = images.render(self.status).decode()
        self.assertLess(page.index(before), page.index(after))
        self.assertIn("The counts now match.", page)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser behavior")
    def test_the_arrow_keys_and_buttons_show_the_before_and_after_pictures_in_the_large_view(self) -> None:
        _ = self.register_pair()
        _ = self.register()
        probe = """<script>
(async () => {
 const failures = [], check = (ok, text) => { if (!ok) failures.push(text); };
 const zoom = document.querySelector('[data-image-zoom-dialog]');
 const label = zoom.querySelector('[data-image-zoom-badge]');
 const [before, after] = document.querySelectorAll('[data-image-zoom-group] a[data-image-zoom]');
 const press = key => zoom.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles:true, cancelable:true}));
 const shows = (link, text, step) => {
  check(zoom.open && zoom.querySelector('img').src === link.href, step + ' shows the ' + text + ' picture');
  check(!label.hidden && label.textContent === text, step + ' badges the picture ' + text);
  check(zoom.getAttribute('aria-label') === link.querySelector('img').alt, step + ' names the zoom ' + text);
 };
 const [left, right] = zoom.querySelectorAll('[data-image-zoom-step]');
 const tip = button => button.querySelector('[data-image-zoom-tip]');
 const tipShown = button => getComputedStyle(tip(button)).visibility === 'visible';
 after.click();
 shows(after, 'After', 'A click on After');
 check(!left.hidden && !left.disabled && left.getAttribute('aria-label') === 'Show Before', 'The left button shows Before');
 check(!right.hidden && right.disabled, 'The right button is off on the last picture');
 check(getComputedStyle(right).opacity === '0.3', 'An off button is dim');
 check(left.getAttribute('aria-keyshortcuts') === 'ArrowLeft' && right.getAttribute('aria-keyshortcuts') === 'ArrowRight',
       'Each button names its key for a screen reader');
 check(tip(left).textContent === 'Press ← to show Before' && tip(left).querySelector('kbd').textContent === '←',
       'The left tip names the key and the picture');
 check(!tipShown(left), 'The tip is hidden until hover or focus');
 left.focus();
 left.click();
 shows(before, 'Before', 'A click on the left button');
 check(left.disabled && !right.disabled && tip(right).textContent === 'Press → to show After',
       'On the first picture the left button is off and the right tip names After');
 check(document.activeElement === zoom, 'A button that turns off returns focus to the large view');
 right.click();
 shows(after, 'After', 'A click on the right button');
 press('ArrowRight');
 shows(after, 'After', 'The right arrow on the last picture');
 press('ArrowLeft');
 shows(before, 'Before', 'The left arrow');
 press('ArrowLeft');
 shows(before, 'Before', 'The left arrow on the first picture');
 press('ArrowRight');
 shows(after, 'After', 'The right arrow');
 zoom.dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowLeft', altKey:true, bubbles:true, cancelable:true}));
 shows(after, 'After', 'Alt and the left arrow');
 const list = document.querySelector('[data-images]');
 list.replaceWith(list.cloneNode(true));
 const [freshBefore, freshAfter] = document.querySelectorAll('[data-image-zoom-group] a[data-image-zoom]');
 press('ArrowLeft');
 shows(freshBefore, 'Before', 'The left arrow after a page refresh');
 press('Escape');
 check(!zoom.open && document.activeElement === freshAfter, 'Escape returns focus to the link that opened the zoom');
 const single = document.querySelector('article:not(.pair) a[data-image-zoom]');
 single.click();
 check(zoom.open && label.hidden && label.textContent === '', 'A single image has no badge');
 check(left.hidden && right.hidden, 'A single image has no arrow buttons');
 press('ArrowLeft');
 check(zoom.querySelector('img').src === single.href, 'The arrows keep a single image');
 press('Escape');
 const frame = document.createElement('iframe');
 frame.src = '/mac';
 await new Promise(done => { frame.onload = done; document.body.append(frame); });
 const mac = frame.contentDocument;
 mac.querySelector('[data-image-zoom-group] a[data-image-zoom]').click();
 const macTip = mac.querySelector('[data-image-zoom-step="1"] [data-image-zoom-tip]');
 check(macTip.textContent === 'Press ▶ to show After', 'On a Mac the tip names the ▶ key');
 await fetch('/results', {method:'POST', body:JSON.stringify(failures)});
})().catch(error => fetch('/results', {method:'POST', body:JSON.stringify([String(error)])}));
</script><img src="/probe-complete" alt="" hidden>"""
        rendered = images.render(self.status).decode()
        page = rendered.replace("</body>", probe + "</body>").encode()
        # Firefox has no userAgentData, so navigator.platform alone selects the Mac keys.
        mac_platform = "<script>Object.defineProperty(Navigator.prototype,'platform',{get:()=>'MacIntel'})</script>"
        mac_page = rendered.replace("<head>", "<head>" + mac_platform, 1).encode()
        results: list[list[str]] = []
        complete = threading.Event()

        class Probe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/probe-complete":
                    # Keep Firefox alive until the checks report their result.
                    if not complete.wait(20):
                        self.send_error(504, "The browser checks did not complete.")
                        return
                    self.send_response(204)
                    self.end_headers()
                    return
                pages = {"/": page, "/mac": mac_page}
                body, kind = (pages[self.path], "text/html; charset=utf-8") if self.path in pages else (PNG, "image/png")
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.end_headers()
                _ = self.wfile.write(body)

            def do_POST(self) -> None:
                payload = self.rfile.read(int(self.headers["Content-Length"])).decode()
                results.append(cast(list[str], json.loads(payload)))
                complete.set()
                self.send_response(204)
                self.end_headers()

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        self.run_firefox(Probe, "pair-zoom", release=complete.set)
        self.assertEqual(results, [[]], "The browser must complete every check without a failure.")

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_image_pair_stays_side_by_side_at_1024_pixels(self) -> None:
        _ = self.register_pair()
        owner = self.owner
        assert owner is not None
        page = page_frame.with_owner(images.render(self.status), "images", owner,
                                     staff.StaffSnapshot((owner,)), lambda: "")
        probe = b'''<script>
(async()=>{
 const failures=[];
 try{
  frameElement.style.width='1024px';
  frameElement.style.height='800px';
  document.querySelector('[data-image-layout]').style.minHeight='300vh';
  const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  const frozen=document.querySelector('.page-top'), groups=document.querySelector('[data-image-groups]');
  for(const top of [0,64,800]){
   scrollTo({top,behavior:'instant'});
   await painted();
   const [before,after]=[...document.querySelectorAll('.image-pair figure')].map(figure=>figure.getBoundingClientRect());
   if(innerWidth!==1024 || scrollY!==top || before.width===0 || after.width===0 ||
      before.top!==after.top || before.right>=after.left){
    failures.push('The Images pair must fit side by side at 1024 px and scroll position '+top+'.');
   }
  }
  const top=frozen.getBoundingClientRect();
  const parts=[...frozen.querySelectorAll('h1,.page-owner,[data-page-status]')];
  if(Math.abs(top.top)>=1 || parts.length!==3 || parts.some(element=>{
   const box=element.getBoundingClientRect();
   return box.height===0 || box.top<top.top || box.bottom>top.bottom;
  })){
   failures.push('At scroll position 800 the title, actions, and status stay in the frozen top.');
  }
  if(groups.getBoundingClientRect().top<top.bottom+15.5){
   failures.push('The image group index stays 16 px or more below the frozen top.');
  }
  frameElement.style.width='1382px';
  groups.querySelector('a span').textContent='admin-console-reviewer';
  await painted();
  for(const cell of groups.querySelectorAll('a > span')){
   const tops=new Set(), text=document.createTreeWalker(cell,NodeFilter.SHOW_TEXT), range=document.createRange();
   while(text.nextNode()){
    range.selectNodeContents(text.currentNode);
    for(const rect of range.getClientRects()) tops.add(Math.round(rect.top));
   }
   if(tops.size!==1) failures.push('The group cell "'+cell.textContent+'" must stay on one line at 1382 px.');
  }
  if(groups.scrollWidth>groups.clientWidth) failures.push('The image group index must not scroll sideways at 1382 px.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        self.assertEqual(firefox_probe_results(page.replace(b'</body>', probe + b'</body>')), [[]])

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
            server = self.enterContext(started_review_server(("127.0.0.1", 0), self.root))
        self.port = server.server_port
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, TEST_HANG_GUARD_SECS)
        self.addCleanup(server.shutdown)

    def http(self, method: str, path: str, body: str = "", *,
             held: bool = False) -> tuple[int, str, bytes]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=TEST_HANG_GUARD_SECS)
        try:
            if method == "POST":
                headers = {"Content-Type": "application/x-www-form-urlencoded"}
                if held:
                    headers.update({send_hold.HOLD_HEADER: "1", "Origin": f"http://127.0.0.1:{self.port}"})
                elif path.startswith(send_hold.HOLD_PATH + "/"):
                    headers["Origin"] = f"http://127.0.0.1:{self.port}"
                connection.request("POST", path, body, headers)
            else:
                connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type") or "", response.read()
        finally:
            connection.close()

    def test_a_saved_question_stays_off_the_page_before_and_after_startup_recovery(self) -> None:
        record = self.register()
        delivery = images.ask_question(self.root, record['id'], 'Which check passed?', self.status)
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        _ = delivery.job.release()
        started, release, completed = threading.Event(), threading.Event(), threading.Event()
        recover = change_page.recover_pending

        def recover_after_first_read(status: change_page.DeliveryStatus) -> None:
            started.set()
            if release.wait(TEST_HANG_GUARD_SECS):
                recover(status)

        def list_recovered(_ledger: Path) -> None:
            completed.set()

        with (patch.object(change_page, 'recover_pending', side_effect=recover_after_first_read),
              patch.object(change_page.change_review, 'lists_directory', return_value=self.temporary / 'empty'),
              patch.object(list_page, 'recover_pending', side_effect=list_recovered),
              serve.ReviewLinksServer(('127.0.0.1', 0), self.root) as server):
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            self.port = server.server_port
            try:
                self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                code, _, content = self.http('GET', images.PAGE_PATH)
                self.assertEqual(code, 200)
                self.assertNotIn(f'data-image="{record["id"]}"'.encode(), content)
                self.assertNotIn(b'Question not delivered.', content)
                self.assertRegex(content, rb'data-refresh-seconds="[1-9][0-9.]*"')
                self.assertEqual(self.sent, [])
                news_path = f'/{change_page.CONSOLE_FOLDER}/{console.NEWS_NAME}'
                self.assertEqual(self.http('POST', news_path, 'open=images')[0], 200)
                seen_path = self.root / change_page.CONSOLE_FOLDER / console.SEEN_NAME
                self.assertEqual(cast(dict[str, list[str]], json.loads(seen_path.read_text()))['images'], [])

                release.set()
                self.assertTrue(completed.wait(TEST_HANG_GUARD_SECS))
                code, _, content = self.http('GET', images.PAGE_PATH)
                self.assertEqual(code, 200)
                self.assertRegex(content, rb'data-refresh-seconds="[1-9][0-9.]*"')
                self.assertNotIn(f'data-image="{record["id"]}"'.encode(), content)
                self.assertNotIn(b'Sending to the owner.', content)
                self.assertNotIn(b'Question not delivered.', content)
                self.assertEqual(len(self.sent), 1)
            finally:
                release.set()
                _ = completed.wait(TEST_HANG_GUARD_SECS)
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)

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
                         (503, {"reason": "shot-owner did not get the message (session gone). Try again."}))
        status, _, answer = self.http("POST", images.PAGE_PATH, body)
        self.assertEqual((status, cast(dict[str, str], json.loads(answer))), (200, {"state": "done"}))
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn(record["id"], self.http("GET", images.PAGE_PATH)[2].decode())

    def test_image_answer_reports_the_failed_producer_or_list_save(self) -> None:
        _, producer, item = self.list_item()
        record = images.register(self.shot, "Check", self.owner_id(), "Phase check", list_item=item.id)
        token = images.request_token(images.console_token(self.root), record["id"])
        body = urlencode({"image": record["id"], "token": token, "feedback": "Too dark.", "action": "feedback"})
        with patch.object(the_list, "save_image_answer", side_effect=OSError("disk unavailable")):
            status, _, answer = self.http("POST", images.PAGE_PATH, body)
        self.assertEqual(status, 503)
        self.assertIn(f"Could not save the answer on {item.id}", answer.decode())
        self.assertNotIn("the owner did not get", answer.decode())
        self.assertEqual(self.sent, [])

        def send(_recipient: str, _sender: str, _message: str, *, key: str, recipient_key: str) -> None:
            self.assertTrue(key)
            if recipient_key == producer.id:
                raise RuntimeError("producer offline")

        with patch.object(change_page, "send_message", side_effect=send):
            status, _, answer = self.http("POST", images.PAGE_PATH, body)
        self.assertEqual(status, 503)
        self.assertIn(producer.name, answer.decode())
        self.assertNotIn("the owner did not get", answer.decode())

    def test_answer_to_an_already_deleted_picture_reaches_its_owner_and_list(self) -> None:
        keeper, producer, item = self.list_item()
        record = images.register(self.shot, "Check", self.owner_id(), "Phase check", list_item=item.id)
        token = images.request_token(images.console_token(self.root), record["id"])
        images.delete(self.root, record["id"])
        words = "Keep the title.\nUse a darker border."
        body = urlencode({"image": record["id"], "token": token, "feedback": words, "action": "feedback",
                          "feedback_round": images.feedback_round(set())})
        status, _, answer = self.http("POST", images.PAGE_PATH, body)
        self.assertEqual(status, 200)
        self.assertIn("This picture was removed; your words went to shot-owner.", answer.decode())
        saved = the_list.load(keeper.id, item.id)
        self.assertIs(saved.state, the_list.ItemState.WORKING)
        self.assertIsInstance(saved.log[-1].what, str)
        self.assertIn(words, cast(str, saved.log[-1].what))
        self.assertEqual([recipient for recipient, _, _ in self.sent],
                         [f"session:{self.owner.current_session}" if self.owner is not None else "",
                          f"session:{producer.current_session}"])
        self.assertTrue(all(words in message for _, _, message in self.sent))

    def test_held_linked_picture_answer_can_be_undone_or_released_once(self) -> None:
        keeper, _, item = self.list_item()
        record = images.register(self.shot, "Check", self.owner_id(), "Phase check", list_item=item.id)
        identity = record["id"]
        images.delete(self.root, identity)
        token = images.request_token(images.console_token(self.root), identity)
        body = urlencode({"image": identity, "token": token, "action": "feedback", "feedback": "Keep the title."})
        _ = write_config_fixture(conf.path("review_links"), "undo_seconds=60\n")

        def hold() -> str:
            status, _, answer = self.http("POST", images.PAGE_PATH, body, held=True)
            self.assertEqual(status, 202, answer)
            saved = cast(dict[str, str], json.loads(answer))
            self.assertEqual(the_list.load(keeper.id, item.id), item)
            self.assertEqual(self.sent, [])
            return f"{send_hold.HOLD_PATH}/{saved['hold']}"

        undone = hold()
        self.assertEqual(self.http("POST", undone + "/undo")[0], 200)
        self.assertEqual(self.http("GET", undone)[0], 404)
        released = hold()
        self.assertEqual(self.http("POST", released + "/send")[0], 200)
        status, _, answer = self.http("GET", released)
        self.assertEqual(status, 200, answer)
        self.assertIn(b"This picture was removed; your words went to shot-owner.", answer)
        self.assertEqual(self.http("GET", released)[2], answer)
        saved = the_list.load(keeper.id, item.id)
        self.assertIs(saved.state, the_list.ItemState.WORKING)
        self.assertEqual(sum(isinstance(entry.what, str) and "Keep the title." in entry.what
                             for entry in saved.log), 1)
        self.assertEqual(len(self.sent), 2)

    def test_reload_shows_the_saved_answer_and_retries_only_the_missing_recipient(self) -> None:
        keeper, producer, item = self.list_item()
        record = images.register(self.shot, "Check <the> rows", self.owner_id(), "Phase check",
                                 look_for=("Check <the> count.",), list_item=item.id)
        identity = record["id"]
        token = images.request_token(images.console_token(self.root), identity)
        images.delete(self.root, identity)
        words = "Keep <this> & the title.\nUse a darker border."
        body = urlencode({"image": identity, "token": token, "feedback": words, "action": "feedback"})
        calls: list[tuple[str, str]] = []
        online = False

        def send(_recipient: str, _sender: str, message: str, *, key: str, recipient_key: str) -> None:
            calls.append((recipient_key, key))
            self.assertIn(words, message)
            if recipient_key == producer.id and not online:
                raise RuntimeError("producer offline")

        with patch.object(change_page, "send_message", side_effect=send):
            self.assertEqual(self.http("POST", images.PAGE_PATH, body)[0], 503)
            saved = the_list.load(keeper.id, item.id)
            key = calls[0][1]
            page = self.http("GET", images.PAGE_PATH)[2].decode()
            self.assertIn('<h2 class="image-title">Check &lt;the&gt; rows</h2>'
                          + '<ul class="image-look-for"><li>Check &lt;the&gt; count.</li></ul>', page)
            self.assertIn("Not sent yet", page)
            self.assertIn("Keep &lt;this&gt; &amp; the title.\nUse a darker border.", page)
            self.assertIn(f'name="retry_key" value="{key}"', page)
            self.assertIn(">Retry</button>", page)

            directory = images.images_root(self.root)
            legacy = image_store.read_answers(directory, identity)
            del legacy["attempts"][key]["words"]
            _ = legacy.pop("heading")
            image_store.write_answers(directory, legacy)
            legacy_page = self.http("GET", images.PAGE_PATH)[2].decode()
            self.assertIn("Keep &lt;this&gt; &amp; the title.", legacy_page)
            self.assertIn(f'<h2 class="image-title">Removed picture {identity}</h2>', legacy_page)

            online = True
            retry = urlencode({"image": identity, "token": token, "action": "retry", "retry_key": key})
            self.assertEqual(self.http("POST", images.PAGE_PATH, retry)[0], 200)
            self.assertEqual(self.http("POST", images.PAGE_PATH, retry)[0], 200)

        self.assertEqual(calls, [(self.owner_id(), key), *([(producer.id, key)] * 4)])
        self.assertEqual(the_list.load(keeper.id, item.id), saved)
        self.assertNotIn(key, self.http("GET", images.PAGE_PATH)[2].decode())

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for the picture form")
    def test_retry_clears_only_the_matching_draft_after_reload(self) -> None:
        first, second = self.register("First shot"), self.register("Second shot")
        words = "Keep the title."
        round_id = images.feedback_round(set())
        picture_ids = [first["id"], second["id"]]
        drafts: list[tuple[str, dict[str, str]]] = []
        with patch.object(change_page, "send_message", side_effect=RuntimeError("owner offline")):
            for record, draft_action in ((first, "feedback"), (second, "approved")):
                identity = record["id"]
                token = images.request_token(images.console_token(self.root), identity)
                body = urlencode({"image": identity, "token": token, "action": "feedback",
                                  "feedback": words, "feedback_round": round_id})
                self.assertEqual(self.http("POST", images.PAGE_PATH, body)[0], 503)
                images.delete(self.root, identity)
                drafts.append((identity, {"words": words, "token": token, "round": round_id,
                                          "retryAction": draft_action, "retryRound": round_id}))
        seed = """<script>
if (!sessionStorage.getItem('probe-phase')) {
  sessionStorage.setItem('images.answer-drafts:' + location.pathname, JSON.stringify(DRAFTS));
}
</script>""".replace("DRAFTS", json.dumps(drafts))
        probe = r"""<script>
(async () => {
 const ids = IDS, failures = [];
 const draft = id => document.getElementById('feedback-' + id);
 const retry = id => [...document.querySelectorAll('button[value=retry]')]
   .find(button => button.form.elements.image.value === id);
 const check = (ok, text) => { if (!ok) failures.push(text); };
 const phase = sessionStorage.getItem('probe-phase');
 if (!phase) {
   check(ids.every(id => draft(id)?.value === 'Keep the title.'), 'Both drafts start with their saved words.');
   for (const id of ids) {
     retry(id).click();
     for (let step = 0; step < 80 && retry(id); step++) {
       await new Promise(resolve => setTimeout(resolve, 50));
     }
     check(!retry(id), 'The confirmed retry leaves the page.');
   }
   check(!draft(ids[0]), 'Retry removes the matching editable draft.');
   check(draft(ids[1])?.value === 'Keep the title.', 'Retry keeps the draft for another verdict.');
   sessionStorage.setItem('probe-phase', JSON.stringify(failures));
   location.reload();
   return;
 }
 failures.push(...JSON.parse(phase));
 check(!draft(ids[0]), 'The confirmed draft stays clear after reload.');
 check(draft(ids[1])?.value === 'Keep the title.', 'The other draft survives reload.');
 await fetch('/results', {method:'POST', body:JSON.stringify(failures)});
})().catch(error => fetch('/results', {method:'POST', body:JSON.stringify([String(error)])}));
</script>""".replace("IDS", json.dumps(picture_ids))
        results: list[list[str]] = []
        complete = threading.Event()
        fixture = self

        class Probe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/complete":
                    _ = complete.wait(TEST_HANG_GUARD_SECS)
                    self.send_payload(204, "", b"")
                elif self.path == "/":
                    self.send_payload(200, "text/html", f'<iframe src="{images.PAGE_PATH}"></iframe>'
                                      .encode() + b'<img hidden src="/complete">')
                elif self.path == images.PAGE_PATH:
                    _, kind, page = fixture.http("GET", self.path)
                    page = page.replace(b"<head>", b"<head>" + seed.encode(), 1)
                    self.send_payload(200, kind, page.replace(b"</body>", probe.encode() + b"</body>"))
                else:
                    self.send_error(404)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                if self.path == "/results":
                    results.append(cast(list[str], json.loads(body)))
                    self.send_payload(204, "", b"")
                    complete.set()
                else:
                    self.send_payload(*fixture.http("POST", self.path, body))

            def send_payload(self, status: int, kind: str, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.end_headers()
                _ = self.wfile.write(payload)

        self.run_firefox(Probe, "retry-draft", release=complete.set)
        self.assertEqual(results, [[]])
        self.assertEqual(len(self.sent), 2)

    def test_retry_recovers_an_answer_after_an_interrupted_delete(self) -> None:
        record = self.register()
        identity = record["id"]
        token = images.request_token(images.console_token(self.root), identity)
        body = urlencode({"image": identity, "token": token, "action": "feedback", "feedback": "Keep the title."})
        with patch.object(change_page, "send_message", side_effect=RuntimeError("owner offline")):
            self.assertEqual(self.http("POST", images.PAGE_PATH, body)[0], 503)

        def stop_delete(folder: Path) -> None:
            (folder / image_store.RECORD).unlink()
            raise OSError("Delete stopped.")

        with patch.object(shutil, "rmtree", side_effect=stop_delete):
            with self.assertRaisesRegex(OSError, "Delete stopped"):
                images.delete(self.root, identity)
        directory = images.images_root(self.root)
        self.assertTrue((directory / identity).is_dir())
        key, = image_store.read_answers(directory, identity)["attempts"]
        page = self.http("GET", images.PAGE_PATH)[2].decode()
        self.assertIn(f"Removed picture {identity}", page)
        self.assertIn(f'name="retry_key" value="{key}"', page)
        self.assertIn("Keep the title.", page)
        retry = urlencode({"image": identity, "token": token, "action": "retry", "retry_key": key})
        self.assertEqual(self.http("POST", images.PAGE_PATH, retry)[0], 200)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn(key, self.http("GET", images.PAGE_PATH)[2].decode())

    def test_sweep_keeps_old_images(self) -> None:
        record = self.register()
        folder = self.root / image_store.IMAGES_FOLDER
        os.utime(folder, (0, 0))
        _ = serve.sweep_snapshots(self.root, datetime.now(UTC).timestamp())
        self.assertTrue((folder / record["id"] / "image.png").is_file())


if __name__ == "__main__":
    _ = unittest.main()
