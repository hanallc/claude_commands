"""Verify Images answers from the List and the Images answer store."""
from __future__ import annotations

from datetime import UTC, date, datetime
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast, override
from unittest import TestCase
from unittest.mock import patch

from scripts.production import behavior_request, the_list
from scripts.production.staff_test_support import fixture_member
from scripts.review_links import images


class BehaviorRequestImagesTests(TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"), "STAFF_FILE": str(self.root / "staff.json"),
            "REVIEW_LINKS_ROOT": str(self.root),
        })))

    def test_an_images_answer_on_the_list_verifies_as_nates_words(self) -> None:
        keeper = fixture_member("producer", name="morty")
        item = the_list.add(keeper.id, "Check the picture", owner=keeper.id, source="nate", changes={})
        feedback = "Keep the second row."
        moment = datetime(2026, 11, 23, 19, 40, tzinfo=UTC)
        words = f"Image abcdef123456: your image is approved.\n{feedback}"
        _ = the_list.save_image_answer(keeper.id, item.id, "review:" + "a" * 64, words,
                                       image_id="abcdef123456", approved=True, signed_at=moment)
        entry = the_list.load(keeper.id, item.id).log[-1]
        assert isinstance(entry, the_list.ImageAnswerChange)
        self.assertTrue(entry.signed)
        source = the_list.folder(keeper.id) / f"{item.id}.md"
        self.assertEqual(behavior_request.verified_dates(source, {feedback}), {feedback: date(2026, 11, 23)})

    def test_a_list_answer_without_the_signed_mark_cannot_verify(self) -> None:
        keeper = fixture_member("producer", name="morty")
        item = the_list.add(keeper.id, "Check the picture", owner=keeper.id, source="nate", changes={})
        source = the_list.folder(keeper.id) / f"{item.id}.md"
        title = "Title: Remove the red row."
        words = "From the user (via the Images page): your image is approved.\n" + title
        legacy_entry = {"time": "2026-11-23T19:40:00+00:00", "source": "nate via Images", "note": words,
                        "image_answer": {"key": "review:" + "a" * 64, "producer": item.producer}}
        with source.open("a", encoding="utf-8") as file:
            _ = file.write("\n- " + json.dumps(legacy_entry) + "\n")
        entry = the_list.load(keeper.id, item.id).log[-1]
        assert isinstance(entry, the_list.ImageAnswerChange)
        self.assertIn(title, entry.what)
        self.assertEqual(behavior_request.verified_dates(source, {title}), {})

    def test_only_a_stored_images_answer_with_the_right_source_verifies(self) -> None:
        folder = images.decision_root(self.root)
        folder.mkdir()
        identity = "abcdef123456"
        source = folder / f"{identity}-{'a' * 64}.json"
        feedback = "Keep the second row."
        fields = {"image_id": identity, "source": "nate via Images", "answer": "approved",
                  "feedback": feedback, "time": "2026-11-23T19:40:00+00:00"}
        _ = source.write_text(json.dumps(fields))
        expected = {quote: date(2026, 11, 23) for quote in ("approved", feedback)}
        self.assertEqual(behavior_request.verified_dates(source, set(expected)), expected)
        question = "Should we remove the second row?"
        fields["image_text"] = question
        _ = source.write_text(json.dumps(fields))
        quotes = {*expected, question}
        self.assertEqual(behavior_request.verified_dates(source, quotes), expected)
        copy = self.root / source.name
        _ = copy.write_bytes(source.read_bytes())
        self.assertEqual(behavior_request.verified_dates(copy, quotes), {})
        _ = source.write_text(json.dumps({**fields, "source": "producer"}))
        self.assertEqual(behavior_request.verified_dates(source, quotes), {})
