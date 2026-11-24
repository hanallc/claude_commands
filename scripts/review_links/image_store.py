"""Store console images, image questions and temporary change images."""
from __future__ import annotations

import html
import json
import re
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from scripts.lib import atomic_file, file_lock
from scripts.production import change_routing, delivery_state
from scripts.review_links import question_record

CHANGE_FOLDER = "change-images"
IMAGES_FOLDER = "images"
RECORD = ".review-link-image.json"
DRAFT_PREFIX = ".review-link-draft-"
SUFFIXES = frozenset((".png", ".jpg", ".jpeg", ".gif", ".webp"))
IDENTITY = re.compile(r"[0-9a-f]{12}")
_ANSWERS_PREFIX = ".review-link-image-answers-"
_LIST_ITEM_ID = re.compile(r"L[1-9][0-9]*")
LOCK = ".review-link-images.lock"
# Chrome clips a headless screenshot below about 150 pixels.
MIN_OUTPUT_IMAGE_HEIGHT = 160


class ImageReference(TypedDict):
    id: str
    owner: str
    registered_at: str
    list_item: NotRequired[str]
    list_keeper: NotRequired[str]


class ImageRecord(ImageReference):
    file: str
    caption: str
    subject: str
    # A pair keeps its second image in file and its first image here.
    before: NotRequired[str]
    # Set after all recipients accept the answer.
    feedback_at: NotRequired[str]
    questions: NotRequired[list[question_record.StoredQuestion]]


class ImageAnswer(TypedDict):
    message: str
    words: NotRequired[str]
    answered: list[str]
    recipients: list[str]
    confirmed: list[str]


class ImageAnswers(ImageReference):
    attempts: dict[str, ImageAnswer]


def image_reference(fields: dict[str, object], identity: str) -> ImageReference:
    if (fields.get("id") != identity or not isinstance(owner := fields.get("owner"), str)
            or not isinstance(registered := fields.get("registered_at"), str)):
        raise ValueError(f"{identity}: invalid image record")
    reference: ImageReference = {"id": identity, "owner": owner, "registered_at": registered}
    if "list_item" in fields:
        item = fields["list_item"]
        if not isinstance(item, str) or not _LIST_ITEM_ID.fullmatch(item):
            raise ValueError(f"{identity}: invalid List item id")
        reference["list_item"] = item
    if "list_keeper" in fields:
        keeper = fields["list_keeper"]
        if (not isinstance(keeper, str) or not keeper or keeper in (".", "..")
                or Path(keeper).name != keeper or "list_item" not in fields):
            raise ValueError(f"{identity}: invalid List keeper id")
        reference["list_keeper"] = keeper
    return reference


def new_answers(record: ImageReference) -> ImageAnswers:
    answers: ImageAnswers = {"id": record["id"], "owner": record["owner"], "registered_at": record["registered_at"],
                             "attempts": {}}
    if "list_item" in record:
        answers["list_item"] = record["list_item"]
    if "list_keeper" in record:
        answers["list_keeper"] = record["list_keeper"]
    return answers


def _answers_path(directory: Path, identity: str) -> Path:
    if not IDENTITY.fullmatch(identity):
        raise ValueError("Invalid image ID.")
    return directory / f"{_ANSWERS_PREFIX}{identity}.json"


def deleted_answer_ids(directory: Path) -> Iterator[str]:
    """Find saved answers for pictures with no picture record."""
    for path in sorted(directory.glob(f"{_ANSWERS_PREFIX}*.json")):
        identity = path.stem.removeprefix(_ANSWERS_PREFIX)
        folder = directory / identity
        if not IDENTITY.fullmatch(identity) or path.is_symlink() or folder.is_symlink():
            continue
        try:
            _ = (folder / RECORD).lstat()
        except FileNotFoundError:
            yield identity
        except OSError:
            continue


def _texts(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(text, str) for text in cast(list[object], value)):
        raise ValueError("Expected a list of text values in the image answer record.")
    return cast(list[str], value)


def read_answers(directory: Path, identity: str) -> ImageAnswers:
    """Read the saved picture reference and answer attempts."""
    fields = delivery_state.read_fields(_answers_path(directory, identity))
    attempts = fields.get("attempts")
    if not isinstance(attempts, dict):
        raise ValueError(f"{identity}: invalid image answer record")
    answers: ImageAnswers = {**image_reference(fields, identity), "attempts": {}}
    for key, attempt in cast(dict[str, object], attempts).items():
        if not isinstance(attempt, dict):
            raise ValueError(f"{identity}: invalid image answer attempt")
        fields = cast(dict[str, object], attempt)
        if not isinstance(message := fields.get("message"), str):
            raise ValueError(f"{identity}: missing image answer message")
        saved: ImageAnswer = {"message": message, "answered": _texts(fields.get("answered")),
                              "recipients": _texts(fields.get("recipients")),
                              "confirmed": _texts(fields.get("confirmed"))}
        if "words" in fields:
            words = fields["words"]
            if not isinstance(words, str):
                raise ValueError(f"{identity}: invalid image answer words")
            saved["words"] = words
        answers["attempts"][key] = saved
    return answers


def write_answers(directory: Path, answers: ImageAnswers) -> None:
    atomic_file.write_bytes_atomic(_answers_path(directory, answers["id"]), json.dumps(answers).encode(), mode=0o600)


def read_record(folder: Path) -> ImageRecord:
    if folder.is_symlink() or (folder / RECORD).is_symlink():
        raise ValueError(f"{folder.name}: not a regular image record")
    fields = delivery_state.read_fields(folder / RECORD)
    texts = ("file", "caption", "subject")
    if any(not isinstance(fields.get(key), str) for key in texts):
        raise ValueError(f"{folder.name}: invalid image record")
    record: ImageRecord = {**image_reference(fields, folder.name), "file": cast(str, fields["file"]),
                           "caption": cast(str, fields["caption"]), "subject": cast(str, fields["subject"])}
    for key in ("before", "feedback_at"):
        value = fields.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{folder.name}: invalid image record")
    if isinstance(before := fields.get("before"), str):
        record["before"] = before
    if isinstance(feedback_at := fields.get("feedback_at"), str):
        record["feedback_at"] = feedback_at
    if "questions" in fields:
        questions = fields["questions"]
        if not isinstance(questions, list):
            raise ValueError(f"{folder.name}: invalid image questions")
        checked = [question_record.read(question) for question in cast(list[object], questions)]
        if len({question.question_id for question in checked}) != len(checked):
            raise ValueError(f"{folder.name}: duplicate question id")
        record["questions"] = [question_record.stored(question) for question in checked]
    return record


def write_record(folder: Path, record: ImageRecord) -> None:
    atomic_file.write_bytes_atomic(folder / RECORD, json.dumps(record).encode("utf-8"), mode=0o600)


@dataclass(frozen=True)
class ImageQuestionStore:
    """Keep question changes and the answer's return mark in one image write."""

    root: Path

    def folder(self, item: str) -> Path:
        if not IDENTITY.fullmatch(item):
            raise question_record.AnswerRefused('not an image id')
        folder = self.root / item
        if folder.is_symlink() or not folder.is_dir():
            raise question_record.AnswerRefused(f'no image {item}')
        return folder

    def questions(self, item: str) -> tuple[question_record.Question, ...]:
        record = read_record(self.folder(item))
        return tuple(question_record.read(question) for question in record.get('questions', []))

    def add(self, item: str, question: question_record.Question) -> None:
        with file_lock.exclusive(self.root / LOCK):
            folder = self.folder(item)
            record = read_record(folder)
            questions = [question_record.read(saved) for saved in record.get('questions', [])]
            if any(saved.question_id == question.question_id for saved in questions):
                raise question_record.AnswerRefused('This image already has that question id.')
            record['questions'] = [question_record.stored(saved) for saved in (*questions, question)]
            write_record(folder, record)

    def set_answer(self, item: str, question_id: str, answer: question_record.Answer) -> None:
        with file_lock.exclusive(self.root / LOCK):
            folder = self.folder(item)
            record = read_record(folder)
            questions = [question_record.read(saved) for saved in record.get('questions', [])]
            current = next((question for question in questions if question.question_id == question_id), None)
            if current is None or current.answer is not None:
                raise question_record.AnswerRefused('This image has no open question with that id.')
            record['questions'] = [question_record.stored(question_record.Question(
                question.question_id, question.asked_at, question.words, answer)
                if question.question_id == question_id else question) for question in questions]
            _ = record.pop('feedback_at', None)
            write_record(folder, record)


def register_file(image: Path, folder: Path, caption: str = "", owner: str = "", subject: str = "",
                  *, before: Path | None = None, identity: str = "", private: bool = False,
                  list_item: str | None = None, list_keeper: str | None = None) -> ImageRecord:
    """Publish one image or a pair with one record and one atomic folder rename."""
    if list_item is not None and not _LIST_ITEM_ID.fullmatch(list_item):
        raise ValueError("Use a List item id such as L324.")
    sources = (("before", before), ("after", image)) if before is not None else (("image", image),)
    for _, source in sources:
        if source.suffix.casefold() not in SUFFIXES:
            raise ValueError(f"{source}: use one of {', '.join(sorted(SUFFIXES))}")
        if not source.is_file():
            raise ValueError(f"{source}: no such image file")
    identity = identity or secrets.token_hex(6)
    if not IDENTITY.fullmatch(identity):
        raise ValueError("Invalid image ID.")
    folder.mkdir(mode=0o700 if private else 0o777, parents=True, exist_ok=True)
    draft = folder / (DRAFT_PREFIX + identity)
    draft.mkdir(mode=0o700 if private else 0o755)
    try:
        files = {stem: stem + source.suffix.casefold() for stem, source in sources}
        # The last source is the after image of a pair, or the single image.
        record: ImageRecord = {"id": identity, "file": files[sources[-1][0]],
                               "caption": caption.strip(), "owner": owner, "subject": subject,
                               "registered_at": datetime.now(UTC).isoformat(timespec="seconds")}
        if "before" in files:
            record["before"] = files["before"]
        if list_item is not None:
            record["list_item"] = list_item
        if list_keeper is not None:
            record["list_keeper"] = list_keeper
        for stem, source in sources:
            target = draft / files[stem]
            if private:
                atomic_file.write_bytes_atomic(target, source.read_bytes(), mode=0o600)
            else:
                _ = shutil.copyfile(source, target)
        write_record(draft, record)
        # The page shows a folder only after its images and record are complete.
        _ = draft.rename(folder / identity)
    except BaseException:
        shutil.rmtree(draft, ignore_errors=True)
        raise
    return record


def register_change(image: Path) -> str:
    """Register a temporary image for a change review row."""
    return register_file(image, change_routing.review_root().resolve() / CHANGE_FOLDER)["id"]


def _checked_image_file(folder: Path) -> Path | None:
    """Return the checked image file of a store folder, or None."""
    try:
        record = read_record(folder)
        file = folder / record["file"]
        if file.name != record["file"] or file.suffix not in SUFFIXES or file.is_symlink() or not file.is_file():
            return None
    except (OSError, ValueError):
        return None
    return file


def change_image(root: Path, identity: str) -> str:
    """Return the public path of a registered change image, or an empty string."""
    if not IDENTITY.fullmatch(identity):
        return ""
    file = _checked_image_file(root / CHANGE_FOLDER / identity)
    return f"/{CHANGE_FOLDER}/{identity}/{file.name}" if file is not None else ""


def copy_page_image(identity: str) -> str:
    """Copy an Images page file into the change store and return its new id, or an empty string."""
    if not IDENTITY.fullmatch(identity):
        return ""
    file = _checked_image_file(change_routing.review_root() / IMAGES_FOLDER / identity)
    if file is None:
        return ""
    try:
        return register_change(file)
    except (OSError, ValueError):
        return ""


def remove_change(root: Path, identity: str) -> None:
    """Delete a change image after a verdict."""
    if IDENTITY.fullmatch(identity):
        folder = root / CHANGE_FOLDER / identity
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder)


def register_output(output: Path) -> str:
    """Render command output as a dark terminal image and register it."""
    text = output.read_text(encoding="utf-8", errors="replace").expandtabs(4)
    lines = text.splitlines() or [""]
    width = min(1440, max(640, max(map(len, lines)) * 9 + 48))
    with tempfile.TemporaryDirectory(prefix="change-output-") as temporary:
        folder = Path(temporary)
        page = folder / "output.html"
        shot = folder / "output.png"
        _ = page.write_text('<!doctype html><meta charset="utf-8"><style>'
            + 'body{margin:0;padding:24px;background:#161b22;color:#e6edf3;box-sizing:border-box}'
            + 'pre{margin:0;font:15px/22px ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}'
            + '</style><pre>' + html.escape(text) + '</pre><script>'
            + 'document.title="output-size:"+Math.ceil(document.body.getBoundingClientRect().height)'
            + '</script>', encoding="utf-8")
        chrome = ["google-chrome", "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--no-default-browser-check", "--no-first-run", f"--user-data-dir={folder / 'profile'}",
        ]
        measured = subprocess.run([*chrome, f"--window-size={width},600", "--dump-dom", page.as_uri()],
            capture_output=True, text=True, check=True, timeout=30)
        size = re.search(r"<title>output-size:(\d+)</title>", measured.stdout)
        if size is None:
            raise ValueError("Chrome did not report the output page size")
        window_height = min(4096, max(MIN_OUTPUT_IMAGE_HEIGHT, int(size[1])))
        _ = subprocess.run([*chrome, f"--window-size={width},{window_height}", f"--screenshot={shot}", page.as_uri()],
            capture_output=True, text=True, check=True, timeout=30)
        return register_change(shot)
