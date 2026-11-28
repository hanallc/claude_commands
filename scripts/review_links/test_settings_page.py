"""Check how the console reads its Undo time."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.lib import conf
from scripts.review_links import settings_page


class UndoTimeTests(TestCase):
    def test_undo_time_uses_the_saved_choice_or_the_config_default(self) -> None:
        config = conf.read(Path(__file__).resolve().parents[2] / "config/review_links.conf")
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(settings_page.undo_seconds(root, config), 10)
            path = root / settings_page.SETTINGS_FILE
            _ = path.write_text('{"seconds":20}', encoding="utf-8")
            self.assertEqual(settings_page.undo_seconds(root, config), 20)
            for contents in ("{", "[]", '{"seconds":true}', '{"seconds":15}', '{"seconds":"20"}'):
                with self.subTest(contents=contents):
                    _ = path.write_text(contents, encoding="utf-8")
                    self.assertEqual(settings_page.undo_seconds(root, config), 10)
