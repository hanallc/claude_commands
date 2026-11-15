"""Register images for the console's Images page, render the page, and send delete requests to each owner."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import re
import secrets
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast
from urllib.parse import parse_qs

from scripts.lib import atomic_file, conf, file_lock
from scripts.production import staff
from scripts.review_links import change_browser, change_page, review_link

FOLDER = "images"
PAGE_NAME = "images.html"
PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/{PAGE_NAME}"
RECORD = ".review-link-image.json"
LOCK = ".review-link-images.lock"
DRAFT_PREFIX = ".review-link-draft-"
SUFFIXES = frozenset((".png", ".jpg", ".jpeg", ".gif", ".webp"))
IDENTITY = re.compile(r"[0-9a-f]{12}")
SENDER = Path(__file__).stem

DeleteState = Literal["pending", "sent", "failed"]


class DeleteRequest(TypedDict):
    requested_at: str
    state: DeleteState
    error: str


class ImageRecord(TypedDict):
    id: str
    file: str
    caption: str
    owner: str
    subject: str
    registered_at: str
    delete_request: NotRequired[DeleteRequest]


@dataclass(frozen=True)
class DeleteAlreadyRequested:
    """The image already has a delete request that is pending or sent."""


def images_root(root: Path) -> Path:
    return root / FOLDER


def page_url() -> str:
    """Return the fixed Images page path; the console's browser token signs its delete requests."""
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def image_path(record: ImageRecord) -> str:
    return f"/{FOLDER}/{record['id']}/{record['file']}"


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
    record: ImageRecord = {
        "id": cast(str, fields["id"]), "file": cast(str, fields["file"]), "caption": cast(str, fields["caption"]),
        "owner": cast(str, fields["owner"]), "subject": cast(str, fields["subject"]),
        "registered_at": cast(str, fields["registered_at"])}
    request = fields.get("delete_request")
    if request is not None:
        if not isinstance(request, dict):
            raise ValueError(f"{folder.name}: invalid delete request")
        parts = cast(dict[str, object], request)
        state, requested_at, error = parts.get("state"), parts.get("requested_at"), parts.get("error")
        if state not in ("pending", "sent", "failed") or not isinstance(requested_at, str) or not isinstance(error, str):
            raise ValueError(f"{folder.name}: invalid delete request")
        record["delete_request"] = {"state": state, "requested_at": requested_at, "error": error}
    return record


def write_record(folder: Path, record: ImageRecord) -> None:
    atomic_file.write_bytes_atomic(folder / RECORD, json.dumps(record).encode("utf-8"), mode=0o600)


def records(root: Path) -> list[ImageRecord]:
    """Read every registered image, newest first; an unreadable record prints one line and stays out."""
    folder = images_root(root)
    found: list[ImageRecord] = []
    try:
        entries = sorted(folder.iterdir())
    except FileNotFoundError:
        return found
    for entry in entries:
        if not IDENTITY.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            found.append(read_record(entry))
        except (OSError, ValueError) as error:
            print(f"review-links images: {error}", file=sys.stderr)
    return sorted(found, key=lambda record: record["registered_at"], reverse=True)


def register(image: Path, caption: str, owner: staff.StaffId, subject: str) -> ImageRecord:
    """Copy the image into a folder that its record owns, so the server serves it and a delete removes it."""
    suffix = image.suffix.casefold()
    if suffix not in SUFFIXES:
        raise ValueError(f"{image}: use one of {', '.join(sorted(SUFFIXES))}")
    if not image.is_file():
        raise ValueError(f"{image}: no such image file")
    if not caption.strip():
        raise ValueError("the caption is empty; say what changed and what to look at")
    if staff.snapshot().by_id(owner) is None:
        raise ValueError(f"no staff record for owner {owner}")
    folder = images_root(review_link.review_root().resolve())
    folder.mkdir(parents=True, exist_ok=True)
    identity = secrets.token_hex(6)
    draft = folder / (DRAFT_PREFIX + identity)
    draft.mkdir(mode=0o755)
    try:
        record: ImageRecord = {"id": identity, "file": "image" + suffix, "caption": caption.strip(), "owner": owner,
                               "subject": subject, "registered_at": datetime.now(UTC).isoformat(timespec="seconds")}
        _ = shutil.copyfile(image, draft / record["file"])
        write_record(draft, record)
        # The page shows a folder only after its image and record are complete.
        _ = draft.rename(folder / identity)
    except BaseException:
        shutil.rmtree(draft, ignore_errors=True)
        raise
    return record


def delete(root: Path, identity: str) -> None:
    if not IDENTITY.fullmatch(identity):
        raise ValueError(f"{identity}: not an image id")
    folder = images_root(root) / identity
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(f"no image {identity}")
    shutil.rmtree(folder)


def console_token(root: Path) -> str:
    return change_browser.browser_manifest(change_page.console_folder(root))[0]


def request_token(token: str, identity: str) -> str:
    return hmac.new(token.encode(), ("image:" + identity).encode(), hashlib.sha256).hexdigest()


def owner_name(record: staff.StaffSnapshot, identity: str) -> str:
    member = record.by_id(staff.StaffId(identity))
    return f"{member.name} ({member.kind})" if member is not None else f"Unknown session: {identity}"


def card(record: ImageRecord, token: str, members: staff.StaffSnapshot) -> str:
    path = html.escape(image_path(record), quote=True)
    caption = record["caption"]
    added = datetime.fromisoformat(record["registered_at"])
    request = record.get("delete_request")
    if request is not None and request["state"] != "failed":
        action = '<p data-delete-state>Delete requested. The owner gets a message.</p>'
    else:
        failure = (f'<p data-delete-state>Not sent: {html.escape(request["error"])}</p>' if request is not None else "")
        action = (failure + '<form method="post" data-delete>'
                  + f'<input type="hidden" name="image" value="{record["id"]}">'
                  + f'<input type="hidden" name="token" value="{request_token(token, record["id"])}">'
                  + '<button type="submit">Request delete</button><span role="status" data-delete-status></span></form>')
    return (f'<article data-image="{record["id"]}" data-registered="{int(added.timestamp())}">'
            + f'<a href="{path}" target="_blank" rel="noopener"><img src="{path}" '
            + f'alt="{html.escape(caption.splitlines()[0], quote=True)}" loading="lazy"></a>'
            + f'<p class="caption">{html.escape(caption)}</p><dl class="image-details">'
            + f'<dt>For</dt><dd>{html.escape(record["subject"])}</dd>'
            + f'<dt>Owner</dt><dd>{html.escape(owner_name(members, record["owner"]))}</dd>'
            + f'<dt>Added</dt><dd><time datetime="{html.escape(record["registered_at"], quote=True)}">'
            + f'{added.strftime("%Y-%m-%d %H:%M UTC")}</time></dd></dl>' + action + '</article>')


def render(root: Path) -> bytes:
    token = console_token(root)
    members = staff.snapshot()
    cards = "".join(card(record, token, members) for record in records(root))
    content = (STYLE + '<h1>Images</h1><div data-image-filter><label for="image-window">Show:</label>'
               + '<select id="image-window"><option value="day">Last day</option>'
               + '<option value="week" selected>Last 7 days</option><option value="all">All</option></select></div>'
               + f'<div data-images>{cards}</div><p hidden data-empty></p>'
               + '<noscript>Enable JavaScript to filter images and to request a delete.</noscript>'
               + f'<script>{SCRIPT}</script>')
    return change_page.document(content, "Images")


def submit(root: Path, body: str) -> ImageRecord | DeleteAlreadyRequested:
    """Record a signed delete request; the caller sends it to the owner after the response."""
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"image", "token"} or any(len(value) != 1 for value in fields.values()):
        raise change_page.AnswerRefused("use the delete form on this page")
    identity = fields["image"][0]
    if not IDENTITY.fullmatch(identity):
        raise change_page.AnswerRefused("not an image id")
    expected = request_token(console_token(root), identity)
    if not secrets.compare_digest(fields["token"][0].encode(), expected.encode()):
        raise change_page.AnswerRefused("the image token does not match this page")
    folder = images_root(root)
    with file_lock.exclusive(folder / LOCK):
        try:
            record = read_record(folder / identity)
        except FileNotFoundError as error:
            raise change_page.AnswerRefused(f"no image {identity}") from error
        request = record.get("delete_request")
        if request is not None and request["state"] != "failed":
            return DeleteAlreadyRequested()
        record["delete_request"] = {"state": "pending", "requested_at": datetime.now(UTC).isoformat(timespec="seconds"),
                                    "error": ""}
        write_record(folder / identity, record)
    return record


def delete_message(record: ImageRecord) -> str:
    origin = f"http://{review_link.link_host()}:{conf.read('review_links').whole('server_port')}"
    return "\n".join((
        "From the user (via the Images page): delete this image.",
        f"Image: {origin}{image_path(record)}", f"Caption: {record['caption']}", f"For: {record['subject']}",
        f"Added: {record['registered_at']}",
        f"To delete it, run: ~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id {record['id']}"))


def deliver(root: Path, record: ImageRecord) -> None:
    """Send the delete request to the owner with the review links delivery budget, then record the result."""
    settings = conf.read("review_links")
    try:
        message = delete_message(record)
    except review_link.SNAPSHOT_ERRORS as error:
        failures = [change_page.DeliveryFailure(staff.StaffId(record["owner"]), str(error))]
    else:
        failures = change_page.send_with_retries(SENDER, (staff.StaffId(record["owner"]),), message,
                                                 settings.whole("delivery_tries"), settings.number("delivery_pause_seconds"))
    folder = images_root(root)
    with file_lock.exclusive(folder / LOCK):
        try:
            current = read_record(folder / record["id"])
        except FileNotFoundError:
            return
        request = current.get("delete_request")
        if request is None or request["state"] != "pending":
            return
        error = " ".join(failures[-1].error.splitlines()) if failures else ""
        request["state"] = "failed" if failures else "sent"
        request["error"] = error
        write_record(folder / record["id"], current)
    if failures:
        print(f"review-links images {record['id']}: delete request not sent: {error}", file=sys.stderr)


def recover_pending(root: Path) -> None:
    """Send the delete requests that a stopped server left pending."""
    for record in records(root):
        request = record.get("delete_request")
        if request is not None and request["state"] == "pending":
            try:
                deliver(root, record)
            except (OSError, ValueError) as error:
                print(f"review-links images {record['id']}: {error}", file=sys.stderr)


STYLE = """<style>[hidden]{display:none!important}
[data-image-filter]{display:flex;align-items:center;gap:8px;margin-bottom:16px}
[data-image-filter] select{font:inherit;min-height:44px;color:CanvasText;background:Canvas}
[data-images] article{padding:24px 0;border-top:1px solid #8886}[data-images] article:first-child{border-top:0;padding-top:8px}
[data-images] img{display:block;max-width:100%;height:auto;border:1px solid #8886;border-radius:6px}
.caption{margin:12px 0 8px}
.image-details{display:grid;grid-template-columns:max-content 1fr;column-gap:1ch;margin:0 0 12px;font:14px/1.45 ui-monospace,monospace;opacity:.8}
.image-details dd{margin:0}[data-delete-state]{margin:8px 0;font-weight:600}[data-delete-status]{margin-left:8px}
</style>"""

SCRIPT = r"""(() => {
const menu = document.getElementById('image-window');
const empty = document.querySelector('[data-empty]');
const spans = {day: 86400, week: 604800, all: Infinity};
let list = document.querySelector('[data-images]');
function apply() {
  const now = Date.now() / 1000, span = spans[menu.value];
  let shown = 0;
  list.querySelectorAll('article').forEach(card => {
    card.hidden = now - Number(card.dataset.registered) > span;
    if (!card.hidden) shown++;
  });
  empty.hidden = shown > 0;
  empty.textContent = list.children.length ? 'No images in this period. Choose a longer period.' : 'No images registered.';
  // The default period keeps a plain URL; the console records any other period.
  history.replaceState(null, '', menu.value === 'week' ? location.pathname : '#' + new URLSearchParams({show: menu.value}));
  if (parent !== window) parent.postMessage('console-place', location.origin);
}
function bind(root) {
  root.querySelectorAll('time').forEach(time => {
    time.textContent = new Date(time.dateTime).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'});
  });
  root.querySelectorAll('form[data-delete]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button'), status = form.querySelector('[data-delete-status]');
    button.disabled = true;
    status.textContent = 'Sending';
    try {
      const response = await fetch(location.pathname, {method: 'POST', body: new URLSearchParams(new FormData(form))});
      if (!response.ok) {
        const reason = await response.json().then(body => body.reason, () => '');
        throw new Error(reason || 'HTTP ' + response.status);
      }
      const done = document.createElement('p');
      done.dataset.deleteState = '';
      done.textContent = 'Delete requested. The owner gets a message.';
      form.closest('article').querySelector('[data-delete-state]')?.remove();
      form.replaceWith(done);
    } catch (error) {
      status.textContent = 'Not sent: ' + error.message;
      button.disabled = false;
    }
  }));
}
async function refresh() {
  if (document.visibilityState !== 'visible' || window.frameElement?.hidden === true) return;
  if (list.querySelector('button:disabled')) return;
  try {
    const response = await fetch(location.pathname, {cache: 'no-store'});
    if (!response.ok) return;
    const fresh = new DOMParser().parseFromString(await response.text(), 'text/html').querySelector('[data-images]');
    if (!fresh || list.querySelector('button:disabled')) return;
    const top = scrollY;
    list.replaceWith(fresh);
    list = fresh;
    bind(list);
    apply();
    scrollTo({top, behavior: 'instant'});
  } catch (_) {}
}
const start = new URLSearchParams(location.hash.slice(1)).get('show');
if (start in spans) menu.value = start;
menu.addEventListener('change', apply);
addEventListener('message', event => {
  if (event.source === parent && event.origin === location.origin && event.data === 'console-shown') refresh();
});
document.addEventListener('visibilitychange', refresh);
bind(list);
apply();
})();"""


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Register an image for the console's Images page, or delete one.")
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser("register", help="copy an image to the Images page with its caption, owner and subject")
    _ = add.add_argument("--image", type=Path, required=True, help="the image file to copy")
    _ = add.add_argument("--caption", required=True, help="what changed and what to look at")
    _ = add.add_argument("--owner", required=True, help="the staff id that receives delete requests")
    subject = add.add_mutually_exclusive_group(required=True)
    _ = subject.add_argument("--change", help="the change id, such as sdlc/C12")
    _ = subject.add_argument("--phase", help="the phase id")
    remove = actions.add_parser("delete", help="remove an image and its folder")
    _ = remove.add_argument("--id", required=True, help="the image id from the delete request")
    args = parser.parse_args(arguments)
    try:
        if cast(str, args.action) == "register":
            change, phase = cast(str | None, args.change), cast(str | None, args.phase)
            record = register(cast(Path, args.image), cast(str, args.caption), staff.StaffId(cast(str, args.owner)),
                              f"Change {change}" if change is not None else f"Phase {phase}")
            origin = f"http://{review_link.link_host()}:{conf.read('review_links').whole('server_port')}"
            print(f"registered {record['id']}: {origin}{image_path(record)}")
            print(f"Images page: {origin}/{change_page.CONSOLE_FOLDER}/?page=images")
        else:
            delete(review_link.review_root().resolve(), cast(str, args.id))
            print(f"deleted {cast(str, args.id)}")
    except (*review_link.SNAPSHOT_ERRORS, staff.StaffRefused) as error:
        print(f"images: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
