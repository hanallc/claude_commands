"""Test image registration, the Images page, and deletes in private folders."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from io import StringIO
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from urllib.parse import urlencode

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member
from scripts.review_links import change_page, images, serve

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    + "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


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

    def register(self, caption: str = "The nav shows Images. Look at the left column.") -> images.ImageRecord:
        return images.register(self.shot, caption, self.owner_id(), "Phase console-images")

class ImagesTests(ImagesFixture):
    def test_register_copies_the_image_into_a_folder_that_the_record_owns(self) -> None:
        record = self.register()
        folder = self.root / images.FOLDER / record["id"]
        self.assertEqual(sorted(path.name for path in folder.iterdir()), [images.RECORD, "image.png"])
        self.assertEqual((folder / "image.png").read_bytes(), PNG)
        self.assertEqual(images.read_record(folder), record)
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
        self.assertEqual(list((self.root / images.FOLDER).glob("*")) if (self.root / images.FOLDER).exists() else [], [])

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
            "Images page: http://127.0.0.1:8765/console/?page=images"])
        errors = StringIO()
        with redirect_stderr(errors):
            self.assertEqual(images.main(["delete", "--id", "000000000000"]), 1)
        self.assertIn("no image 000000000000", errors.getvalue())
        with redirect_stdout(StringIO()):
            self.assertEqual(images.main(["delete", "--id", record["id"]]), 0)
        self.assertEqual(images.records(self.root), [])

    def test_page_lists_newest_first_with_owner_subject_and_signed_forms(self) -> None:
        older, newer = self.register("Older shot"), self.register("Newer shot")
        older_folder = self.root / images.FOLDER / older["id"]
        older["registered_at"] = (datetime.now(UTC) - timedelta(days=9)).isoformat(timespec="seconds")
        images.write_record(older_folder, older)
        source = images.render(self.root).decode()
        self.assertLess(source.index(newer["id"]), source.index(older["id"]))
        self.assertIn("Newer shot", source)
        self.assertIn("shot-owner (unit)", source)
        self.assertIn("Phase console-images", source)
        self.assertIn(f'src="/images/{newer['id']}/image.png"', source)
        token = images.request_token(images.console_token(self.root), newer["id"])
        self.assertIn(f'name="token" value="{token}"', source)
        self.assertIn('<option value="week" selected>', source)
        self.assertIn("card.remove();", source)
        self.assertIn("home.insertBefore(card, next?.parentNode === home ? next : null);", source)
        self.assertIn("'Not deleted: ' + error.message", source)

    def test_card_shows_the_caption_then_aligned_details_then_the_image_and_delete(self) -> None:
        record = self.register("Look at <the> cards")
        record["registered_at"] = "2026-11-15T03:09:00+00:00"
        images.write_record(self.root / images.FOLDER / record["id"], record)
        card = images.card(record, "token", staff.snapshot())
        path = f"/images/{record['id']}/image.png"
        token = images.request_token("token", record["id"])
        self.assertEqual(card, (
            f'<article data-image="{record['id']}" data-registered="1794712140">'
            + '<h2 class="caption">Look at &lt;the&gt; cards</h2><dl class="image-details">'
            + '<dt>Owner:</dt><dd>shot-owner (unit)</dd><dt>For  :</dt><dd>Phase console-images</dd>'
            + '<dt>Added:</dt><dd><time datetime="2026-11-15T03:09:00+00:00">Nov 14, 2026, 22:09</time></dd></dl>'
            + f'<a href="{path}" target="_blank" rel="noopener"><img src="{path}" alt="Look at &lt;the&gt; cards" loading="lazy"></a>'
            + f'<form method="post" data-delete><input type="hidden" name="image" value="{record['id']}">'
            + f'<input type="hidden" name="token" value="{token}">'
            + '<button type="submit">Delete</button><span role="status" data-delete-status></span></form></article>'))
        with patch.dict(os.environ, {"USER_ZONE": "Europe/Berlin"}):
            self.assertIn(">Nov 15, 2026, 04:09<", images.card(record, "token", staff.snapshot()))

    def test_delete_removes_the_image_at_once_and_sends_no_message(self) -> None:
        record = self.register()
        kept = self.register("Kept shot")
        body = urlencode({"image": record["id"],
                          "token": images.request_token(images.console_token(self.root), record["id"])})
        images.submit(self.root, body)
        self.assertFalse((self.root / images.FOLDER / record["id"]).exists())
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

    def test_an_old_record_with_a_delete_request_reads_and_shows_delete(self) -> None:
        record = self.register()
        folder = self.root / images.FOLDER / record["id"]
        stored = cast(dict[str, object], json.loads((folder / images.RECORD).read_text()))
        stored["delete_request"] = {"state": "sent", "requested_at": record["registered_at"], "error": ""}
        _ = (folder / images.RECORD).write_text(json.dumps(stored))
        self.assertEqual(images.read_record(folder), record)
        self.assertIn('<button type="submit">Delete</button>', images.render(self.root).decode())


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
        status, _, _ = self.http("GET", f"/{images.FOLDER}/{record['id']}/{images.RECORD}")
        self.assertEqual(status, 404)

    def test_server_deletes_the_image_and_refuses_a_bad_token(self) -> None:
        record = self.register()
        status, _, body = self.http("POST", images.PAGE_PATH, urlencode({"image": record["id"], "token": "wrong"}))
        self.assertEqual(status, 400)
        self.assertEqual(cast(dict[str, str], json.loads(body)), {"reason": "the image token does not match this page"})
        token = images.request_token(images.console_token(self.root), record["id"])
        status, _, body = self.http("POST", images.PAGE_PATH, urlencode({"image": record["id"], "token": token}))
        self.assertEqual((status, cast(dict[str, str], json.loads(body))), (200, {"state": "deleted"}))
        self.assertEqual(images.records(self.root), [])
        self.assertEqual(self.sent, [])
        status, _, _ = self.http("GET", images.image_path(record))
        self.assertEqual(status, 404)

    def test_sweep_keeps_old_images(self) -> None:
        record = self.register()
        folder = self.root / images.FOLDER
        os.utime(folder, (0, 0))
        _ = serve.sweep_snapshots(self.root, datetime.now(UTC).timestamp())
        self.assertTrue((folder / record["id"] / "image.png").is_file())


if __name__ == "__main__":
    _ = unittest.main()
