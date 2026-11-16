"""Store console images and temporary change images without page dependencies."""
from __future__ import annotations

import html
import json
import re
import secrets
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from scripts.lib import atomic_file
from scripts.production import change_routing

CHANGE_FOLDER = "change-images"
RECORD = ".review-link-image.json"
DRAFT_PREFIX = ".review-link-draft-"
SUFFIXES = frozenset((".png", ".jpg", ".jpeg", ".gif", ".webp"))
IDENTITY = re.compile(r"[0-9a-f]{12}")


class ImageRecord(TypedDict):
    id: str
    file: str
    caption: str
    owner: str
    subject: str
    registered_at: str
    # A before and after pair keeps the after image in `file` and the before image here.
    before: NotRequired[str]
    # Set when the owner received the user's answer; the page hides the image until the owner deletes it.
    feedback_at: NotRequired[str]


def read_record(folder: Path) -> ImageRecord:
    if folder.is_symlink() or (folder / RECORD).is_symlink():
        raise ValueError(f"{folder.name}: not a regular image record")
    value = cast(object, json.loads((folder / RECORD).read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError(f"{folder.name}: invalid image record")
    fields = cast(dict[str, object], value)
    texts = ("id", "file", "caption", "owner", "subject", "registered_at")
    if any(not isinstance(fields.get(key), str) for key in texts) or fields["id"] != folder.name:
        raise ValueError(f"{folder.name}: invalid image record")
    record: ImageRecord = {"id": cast(str, fields["id"]), "file": cast(str, fields["file"]),
                           "caption": cast(str, fields["caption"]), "owner": cast(str, fields["owner"]),
                           "subject": cast(str, fields["subject"]), "registered_at": cast(str, fields["registered_at"])}
    for key in ("before", "feedback_at"):
        value = fields.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{folder.name}: invalid image record")
    if isinstance(before := fields.get("before"), str):
        record["before"] = before
    if isinstance(feedback_at := fields.get("feedback_at"), str):
        record["feedback_at"] = feedback_at
    return record


def write_record(folder: Path, record: ImageRecord) -> None:
    atomic_file.write_bytes_atomic(folder / RECORD, json.dumps(record).encode("utf-8"), mode=0o600)


def register_file(image: Path, folder: Path, caption: str = "", owner: str = "", subject: str = "",
                  *, before: Path | None = None) -> ImageRecord:
    """Publish one image or a pair with one record and one atomic folder rename."""
    sources = (("before", before), ("after", image)) if before is not None else (("image", image),)
    for _, source in sources:
        if source.suffix.casefold() not in SUFFIXES:
            raise ValueError(f"{source}: use one of {', '.join(sorted(SUFFIXES))}")
        if not source.is_file():
            raise ValueError(f"{source}: no such image file")
    folder.mkdir(parents=True, exist_ok=True)
    identity = secrets.token_hex(6)
    draft = folder / (DRAFT_PREFIX + identity)
    draft.mkdir(mode=0o755)
    try:
        files = {stem: stem + source.suffix.casefold() for stem, source in sources}
        # The last source is the after image of a pair, or the single image.
        record: ImageRecord = {"id": identity, "file": files[sources[-1][0]],
                               "caption": caption.strip(), "owner": owner, "subject": subject,
                               "registered_at": datetime.now(UTC).isoformat(timespec="seconds")}
        if "before" in files:
            record["before"] = files["before"]
        for stem, source in sources:
            _ = shutil.copyfile(source, draft / files[stem])
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


def change_image(root: Path, identity: str) -> str:
    """Return the public path of a registered change image, or an empty string."""
    if not IDENTITY.fullmatch(identity):
        return ""
    folder = root / CHANGE_FOLDER / identity
    try:
        record = read_record(folder)
        file = folder / record["file"]
        if file.name != record["file"] or file.suffix not in SUFFIXES or file.is_symlink() or not file.is_file():
            return ""
    except (OSError, ValueError):
        return ""
    return f"/{CHANGE_FOLDER}/{identity}/{record['file']}"


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
    height = min(4096, max(120, len(lines) * 22 + 48))
    with tempfile.TemporaryDirectory(prefix="change-output-") as temporary:
        folder = Path(temporary)
        page = folder / "output.html"
        shot = folder / "output.png"
        _ = page.write_text('<!doctype html><meta charset="utf-8"><style>'
            + 'body{margin:0;padding:24px;background:#161b22;color:#e6edf3;box-sizing:border-box}'
            + 'pre{margin:0;font:15px/22px ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}'
            + '</style><pre>' + html.escape(text) + '</pre>', encoding="utf-8")
        _ = subprocess.run(["google-chrome", "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
            "--no-default-browser-check", "--no-first-run", f"--user-data-dir={folder / 'profile'}",
            f"--window-size={width},{height}", f"--screenshot={shot}", page.as_uri()],
            capture_output=True, text=True, check=True, timeout=30)
        return register_change(shot)
