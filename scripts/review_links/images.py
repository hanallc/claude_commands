"""Register images for the console's Images page, render the page, delete images, and send feedback to owners."""
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
from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast
from urllib.parse import parse_qs

from scripts.lib import atomic_file, conf, file_lock
from scripts.lib.user_zone import shown_time
from scripts.production import staff
from scripts.review_links import change_browser, change_page, console, review_link

FOLDER = "images"
PAGE_NAME = "images.html"
PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/{PAGE_NAME}"
RECORD = ".review-link-image.json"
LOCK = ".review-link-images.lock"
DRAFT_PREFIX = ".review-link-draft-"
SUFFIXES = frozenset((".png", ".jpg", ".jpeg", ".gif", ".webp"))
IDENTITY = re.compile(r"[0-9a-f]{12}")
SENDER = Path(__file__).stem
ACTIONS = frozenset(("delete", "feedback"))


class ImageRecord(TypedDict):
    id: str
    file: str
    caption: str
    owner: str
    subject: str
    registered_at: str
    # A before and after pair keeps the after image in `file` and the before image here.
    before: NotRequired[str]
    # Set when the owner received the user's feedback; the page hides the image until the owner deletes it.
    feedback_at: NotRequired[str]


class FeedbackNotSent(Exception):
    """No try reached the owner; the image stays on the page."""


def images_root(root: Path) -> Path:
    return root / FOLDER


def page_url() -> str:
    """Return the fixed Images page path; the console's browser token signs its deletes."""
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def image_path(record: ImageRecord, file: str = "") -> str:
    return f"/{FOLDER}/{record['id']}/{file or record['file']}"


def shown_files(record: ImageRecord) -> tuple[tuple[str, str], ...]:
    """Return each image's label and file: a pair shows Before, then After; one image has no label."""
    if "before" in record:
        return ("Before", record["before"]), ("After", record["file"])
    return ("", record["file"]),


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


def records(root: Path) -> list[ImageRecord]:
    """Read every registered image, oldest first; an unreadable record prints one line and stays out."""
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
    # A new image goes to the end, so the cards above it keep their places.
    return sorted(found, key=lambda record: record["registered_at"])


def register(image: Path, caption: str, owner: staff.StaffId, subject: str,
             *, before: Path | None = None) -> ImageRecord:
    """Copy the image, or a before and after pair, into a folder that its record owns.

    The server serves the folder's images, and a delete removes the folder.
    """
    sources = (("before", before), ("after", image)) if before is not None else (("image", image),)
    for _, source in sources:
        if source.suffix.casefold() not in SUFFIXES:
            raise ValueError(f"{source}: use one of {', '.join(sorted(SUFFIXES))}")
        if not source.is_file():
            raise ValueError(f"{source}: no such image file")
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
        files = {stem: stem + source.suffix.casefold() for stem, source in sources}
        # The last source is the after image of a pair, or the one image.
        record: ImageRecord = {"id": identity, "file": files[sources[-1][0]],
                               "caption": caption.strip(), "owner": owner, "subject": subject,
                               "registered_at": datetime.now(UTC).isoformat(timespec="seconds")}
        if "before" in files:
            record["before"] = files["before"]
        for stem, source in sources:
            _ = shutil.copyfile(source, draft / files[stem])
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


def owner_names(record: staff.StaffSnapshot, identity: str) -> tuple[str, str]:
    """Return the card's owner text, name and kind, and the group panel's owner text, the name alone."""
    member = record.by_id(staff.StaffId(identity))
    if member is None:
        unknown = f"Unknown session: {identity}"
        return unknown, unknown
    return f"{member.name} ({member.kind})", member.name


def picture(record: ImageRecord, label: str, file: str) -> str:
    """Link one image to its full size; a pair image shows its label above it."""
    path = html.escape(image_path(record, file), quote=True)
    alt = record["caption"].splitlines()[0]
    linked = (f'<a href="{path}" target="_blank" rel="noopener"><img src="{path}" '
              + f'alt="{html.escape(f"{label}: {alt}" if label else alt, quote=True)}" loading="lazy"></a>')
    return f'<figure><figcaption>{label}</figcaption>{linked}</figure>' if label else linked


def card(record: ImageRecord, token: str, members: staff.StaffSnapshot, feedback_rows: int) -> str:
    """Show the caption as the title, then the aligned details, the image or pair, the feedback field and the buttons."""
    pair = "before" in record
    pictures = "".join(picture(record, label, file) for label, file in shown_files(record))
    caption = record["caption"]
    added = datetime.fromisoformat(record["registered_at"])
    owner, name = owner_names(members, record["owner"])
    return (f'<article id="image-{record["id"]}"{' class="pair"' if pair else ''} data-image="{record["id"]}" data-registered="{int(added.timestamp())}" '
            + f'data-owner="{html.escape(record["owner"], quote=True)}" data-owner-name="{html.escape(name, quote=True)}">'
            + f'<h2 class="caption">{html.escape(caption)}</h2><dl class="image-details">'
            + f'<dt>Owner:</dt><dd>{html.escape(owner)}</dd>'
            + f'<dt>For  :</dt><dd>{html.escape(record["subject"])}</dd>'
            + f'<dt>Added:</dt><dd><time datetime="{html.escape(record["registered_at"], quote=True)}">'
            + f'{shown_time(added)}</time></dd></dl>'
            + (f'<div class="image-pair">{pictures}</div>' if pair else pictures)
            + '<form method="post" data-image-form>'
            + f'<input type="hidden" name="image" value="{record["id"]}">'
            + f'<input type="hidden" name="token" value="{request_token(token, record["id"])}">'
            + f'<label for="feedback-{record["id"]}">Feedback</label>'
            + f'<textarea id="feedback-{record["id"]}" name="feedback" rows="{feedback_rows}"></textarea>'
            + '<div class="image-actions"><button type="submit" name="action" value="delete">Delete</button>'
            + '<button type="submit" name="action" value="feedback">Send feedback</button>'
            + '<span role="status" data-image-status></span></div></form></article>')


def render(root: Path) -> bytes:
    token = console_token(root)
    members = staff.snapshot()
    rows = conf.read("review_links").whole("feedback_rows")
    cards = "".join(card(record, token, members, rows) for record in records(root) if "feedback_at" not in record)
    content = (STYLE + '<h1>Images</h1><div data-image-filter><label for="image-window">Show:</label>'
               + '<select id="image-window"><option value="day">Last day</option>'
               + '<option value="week" selected>Last 7 days</option><option value="all">All</option></select></div>'
               + '<div data-image-layout><nav data-image-groups aria-label="Image groups"></nav>'
               + f'<div><div data-images>{cards}</div><p hidden data-empty></p></div></div>'
               + '<noscript>Enable JavaScript to filter images, delete an image or send feedback.</noscript>'
               + f'<script>{SCRIPT}</script>')
    return change_page.document(content, "Images")


def submit(root: Path, body: str) -> None:
    """Delete the image that a signed form names, or send its feedback to the image's owner."""
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    # A page loaded before Send feedback existed posts only the image and its token, which means delete.
    if not {"image", "token"} <= set(fields) <= {"image", "token", "feedback", "action"} or any(
            len(value) != 1 for value in fields.values()):
        raise change_page.AnswerRefused("use the form on this page")
    identity = fields["image"][0]
    if not IDENTITY.fullmatch(identity):
        raise change_page.AnswerRefused("not an image id")
    expected = request_token(console_token(root), identity)
    if not secrets.compare_digest(fields["token"][0].encode(), expected.encode()):
        raise change_page.AnswerRefused("the image token does not match this page")
    action = fields.get("action", ["delete"])[0]
    if action not in ACTIONS:
        raise change_page.AnswerRefused("use the form on this page")
    if action == "feedback":
        send_feedback(root, identity, fields.get("feedback", [""])[0].strip())
        return
    with file_lock.exclusive(images_root(root) / LOCK):
        try:
            delete(root, identity)
        except ValueError as error:
            raise change_page.AnswerRefused(str(error)) from error


def feedback_message(record: ImageRecord, feedback: str) -> str:
    origin = f"http://{review_link.link_host()}:{conf.read('review_links').whole('server_port')}"
    return "\n".join((
        "From the user (via the Images page): feedback on your image.",
        f"Feedback: {feedback}",
        *(f"{label or 'Image'}: {origin}{image_path(record, file)}" for label, file in shown_files(record)),
        f"Caption: {record['caption']}",
        f"For: {record['subject']}",
        "After you act on it, delete the image: "
        + f"~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id {record['id']}"))


def send_feedback(root: Path, identity: str, feedback: str) -> None:
    """Send the feedback to the owner's current session, then hide the image until the owner deletes it."""
    if not feedback:
        raise change_page.AnswerRefused("enter feedback")
    folder = images_root(root) / identity
    try:
        record = read_record(folder)
    except (OSError, ValueError) as error:
        raise change_page.AnswerRefused(f"no image {identity}") from error
    settings = conf.read("review_links")
    failures = change_page.send_with_retries(SENDER, (staff.StaffId(record["owner"]),),
                                             feedback_message(record, feedback), settings.whole("delivery_tries"),
                                             settings.number("delivery_pause_seconds"))
    if failures:
        raise FeedbackNotSent(failures[-1].error)
    with file_lock.exclusive(images_root(root) / LOCK):
        try:
            current = read_record(folder)
        except (OSError, ValueError):
            # The owner deleted the image while the message was on its way.
            return
        current["feedback_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        write_record(folder, current)


# At 1104px and wider, the page starts at the console's 30px header padding, so
# the groups panel sits under the header title. 1104 = 30 + 300 + 64 + 680 + 30.
# There a single card keeps 680px, and a pair card takes up to two of them: 1376 = 680 + 16 + 680.
# A pair stacks, before on top, when its card is narrower than 600px.
STYLE = """<style>[hidden]{display:none!important}
[data-image-filter]{display:flex;align-items:center;gap:8px;margin-bottom:16px}
[data-image-filter] select{font:inherit;min-height:44px;color:CanvasText;background:Canvas}
[data-images] article{padding:24px 0;border-top:1px solid #8886}[data-images] article:first-child{border-top:0;padding-top:8px}
[data-images] img{display:block;max-width:100%;height:auto;border:1px solid #8886;border-radius:6px}
[data-images] article{container-type:inline-size}
.image-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;align-items:start}
.image-pair figure{margin:0}.image-pair figcaption{margin:0 0 6px;font-weight:600}
@container (max-width:599px){.image-pair{grid-template-columns:minmax(0,1fr)}}
.caption{margin:0 0 8px;font-size:1.25rem;line-height:1.35;font-weight:600;white-space:pre-line}
.image-details{display:grid;grid-template-columns:max-content 1fr;column-gap:1ch;margin:0 0 12px;font:14px/1.45 ui-monospace,monospace;opacity:.8}
.image-details dt{white-space:pre}.image-details dd{margin:0}
[data-images] form{display:grid;gap:8px;margin-top:12px}
[data-images] textarea{font:inherit;width:100%;box-sizing:border-box;color:CanvasText;background:Canvas}
.image-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
main{max-width:1060px}
[data-image-layout]{display:grid;grid-template-columns:300px minmax(0,680px);gap:32px;align-items:start}
[data-image-groups]{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto;display:grid;
grid-template-columns:minmax(0,max-content) max-content max-content;font-size:14px;border:1px solid #8886;border-radius:8px}
[data-image-groups] a{grid-column:1/-1;display:grid;grid-template-columns:subgrid;cursor:pointer;
color:inherit;text-decoration:none;overflow-wrap:anywhere}
[data-image-groups] a+a{border-top:1px solid #8884}
[data-image-groups] span{padding:6px 10px}[data-image-groups] span+span{border-left:1px solid #8884}
[data-image-groups] a:hover,[data-image-groups] a:focus-visible{background:#8883;outline:none}
[data-image-groups] .group-count{text-align:right;font-variant-numeric:tabular-nums}
@media (max-width:900px){[data-image-layout]{display:block}
[data-image-groups]{position:static;max-height:none;margin-bottom:16px}}
@media (min-width:1104px){main{max-width:none;margin:0;padding-left:30px;padding-right:30px}
[data-image-layout]{grid-template-columns:300px minmax(0,1fr);column-gap:64px}
[data-image-layout]>div{width:100%;max-width:1376px;justify-self:center}
[data-images] article:not(.pair),[data-empty]{max-width:680px;margin-inline:auto}}
</style>"""

SCRIPT = r"""(() => {
const menu = document.getElementById('image-window');
const empty = document.querySelector('[data-empty]');
const spans = {day: 86400, week: 604800, all: Infinity};
const groups = document.querySelector('[data-image-groups]');
let list = document.querySelector('[data-images]');
let sending = 0;
function apply() {
  const now = Date.now() / 1000, span = spans[menu.value];
  let shown = 0;
  list.querySelectorAll('article').forEach(card => {
    card.hidden = now - Number(card.dataset.registered) > span;
    if (!card.hidden) shown++;
  });
  empty.hidden = shown > 0;
  showGroups();
  empty.textContent = list.children.length ? 'No images in this period. Choose a longer period.' : 'No images registered.';
  // The default period keeps a plain URL; the console records any other period.
  history.replaceState(null, '', menu.value === 'week' ? location.pathname : '#' + new URLSearchParams({show: menu.value}));
  if (parent !== window) parent.postMessage('console-place', location.origin);
}
// One row for each run of shown images from the same owner; the row opens the first image of the run.
function showGroups() {
  const rows = [];
  list.querySelectorAll('article:not([hidden])').forEach(card => {
    const last = rows[rows.length - 1];
    if (last && last.owner === card.dataset.owner) { last.count++; return; }
    rows.push({owner: card.dataset.owner, card, count: 1});
  });
  groups.replaceChildren(...rows.map(row => {
    const link = document.createElement('a');
    link.href = '#' + row.card.id;
    link.addEventListener('click', event => {
      event.preventDefault();
      row.card.scrollIntoView({block: 'start'});
    });
    const name = row.card.dataset.ownerName;
    [name, String(row.count), row.card.querySelector('time').textContent].forEach((text, index) => {
      const cell = document.createElement('span');
      cell.textContent = text;
      if (index === 1) cell.className = 'group-count';
      link.append(cell);
    });
    return link;
  }));
  groups.hidden = rows.length === 0;
}
function bind(root) {
  root.querySelectorAll('form[data-image-form]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const feedback = event.submitter?.value === 'feedback', status = form.querySelector('[data-image-status]');
    if (feedback && !form.elements.feedback.value.trim()) {
      status.textContent = 'Enter feedback first.';
      return;
    }
    // The card leaves at once and the request runs in the background; a failure puts the card back.
    const card = form.closest('article'), home = card.parentNode, next = card.nextSibling;
    const body = new URLSearchParams(new FormData(form, event.submitter));
    status.textContent = '';
    card.remove();
    sending++;
    apply();
    try {
      const response = await fetch(location.pathname, {method: 'POST', body});
      if (!response.ok) {
        const reason = await response.json().then(body => body.reason, () => '');
        throw new Error(reason || 'HTTP ' + response.status);
      }
    } catch (error) {
      home.insertBefore(card, next?.parentNode === home ? next : null);
      status.textContent = (feedback ? 'Not sent: ' : 'Not deleted: ') + error.message;
      apply();
    } finally {
      sending--;
    }
  }));
}
async function refresh() {
  if (document.visibilityState !== 'visible' || window.frameElement?.hidden === true) return;
  if (sending) return;
  try {
    const response = await fetch(location.pathname, {cache: 'no-store'});
    if (!response.ok) return;
    const fresh = new DOMParser().parseFromString(await response.text(), 'text/html').querySelector('[data-images]');
    if (!fresh || sending) return;
    const top = scrollY;
    list.replaceWith(fresh);
    list = fresh;
    bind(list);
    apply();
    scrollTo({top, behavior: 'instant'});
  } catch (_) {}
}
const start = new URLSearchParams(location.hash.slice(1));
if (start.get('show') in spans) menu.value = start.get('show');
// An image link (#image=<id>) opens the page at that image, with a longer period when the image is older.
const target = document.getElementById('image-' + start.get('image'));
menu.addEventListener('change', apply);
addEventListener('message', event => {
  if (event.source === parent && event.origin === location.origin && event.data === 'console-shown') refresh();
});
document.addEventListener('visibilitychange', refresh);
bind(list);
apply();
if (target) {
  if (target.hidden) {
    menu.value = 'all';
    apply();
  }
  target.scrollIntoView({block: 'start'});
}
})();"""


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Register an image for the console's Images page, or delete one.")
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser("register", help="copy an image or a before and after pair to the Images page")
    _ = add.add_argument("--image", type=Path, help="the image file to copy")
    _ = add.add_argument("--before", type=Path, help="the before image of a pair; give --after with it")
    _ = add.add_argument("--after", type=Path, help="the after image of a pair; give --before with it")
    _ = add.add_argument("--caption", required=True, help="what changed and what to look at")
    _ = add.add_argument("--owner", required=True, help="the staff id that owns the image")
    subject = add.add_mutually_exclusive_group(required=True)
    _ = subject.add_argument("--change", help="the change id, such as sdlc/C12")
    _ = subject.add_argument("--phase", help="the phase id")
    remove = actions.add_parser("delete", help="remove an image and its folder")
    _ = remove.add_argument("--id", required=True, help="the image id")
    args = parser.parse_args(arguments)
    try:
        if cast(str, args.action) == "register":
            change, phase = cast(str | None, args.change), cast(str | None, args.phase)
            image, before, after = (cast(Path | None, args.image), cast(Path | None, args.before),
                                    cast(Path | None, args.after))
            if image is not None and before is None and after is None:
                shown = image
            elif image is None and before is not None and after is not None:
                shown = after
            else:
                parser.error("give --image, or --before and --after")
            record = register(shown, cast(str, args.caption), staff.StaffId(cast(str, args.owner)),
                              f"Change {change}" if change is not None else f"Phase {phase}", before=before)
            origin = f"http://{review_link.link_host()}:{conf.read('review_links').whole('server_port')}"
            print(f"registered {record['id']}: "
                  + " ".join(f"{origin}{image_path(record, file)}" for _, file in shown_files(record)))
            place = console.place_query("images", f"{PAGE_PATH}#image={record['id']}")
            print(f"Image link: {origin}/{change_page.CONSOLE_FOLDER}/{place}")
        else:
            delete(review_link.review_root().resolve(), cast(str, args.id))
            print(f"deleted {cast(str, args.id)}")
    except (*review_link.SNAPSHOT_ERRORS, staff.StaffRefused) as error:
        print(f"images: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
