"""Store console images and temporary change images in separate folders."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import secrets
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs

from scripts.lib import conf, file_lock
from scripts.lib.user_zone import shown_time
from scripts.production import staff
from scripts.review_links import change_browser, change_page, console, image_store, page_frame, review_link

PAGE_NAME = "images.html"
PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/{PAGE_NAME}"
LOCK = ".review-link-images.lock"
SENDER = Path(__file__).stem
# Each answer button and the first line of the message that it sends to the owner.
ANSWERS = {"approved": "your image is approved.", "not approved": "your image is not approved.",
           "feedback": "feedback on your image."}
# A page loaded before the answer buttons existed can still post delete.
ACTIONS = frozenset(("delete", *ANSWERS))


class AnswerNotSent(Exception):
    """No try reached the owner; the image stays on the page."""


def images_root(root: Path) -> Path:
    return root / image_store.IMAGES_FOLDER


def page_url() -> str:
    """Return the fixed Images page path; the console's browser token signs its deletes."""
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def image_path(record: image_store.ImageRecord, file: str = "") -> str:
    return f"/{image_store.IMAGES_FOLDER}/{record['id']}/{file or record['file']}"


def shown_files(record: image_store.ImageRecord) -> tuple[tuple[str, str], ...]:
    """Return each image's label and file: a pair shows Before, then After; one image has no label."""
    if "before" in record:
        return ("Before", record["before"]), ("After", record["file"])
    return ("", record["file"]),


def records(root: Path) -> list[image_store.ImageRecord]:
    """Read every registered image, oldest first; an unreadable record prints one line and stays out."""
    folder = images_root(root)
    found: list[image_store.ImageRecord] = []
    try:
        entries = sorted(folder.iterdir())
    except FileNotFoundError:
        return found
    for entry in entries:
        if not image_store.IDENTITY.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            found.append(image_store.read_record(entry))
        except (OSError, ValueError) as error:
            print(f"review-links images: {error}", file=sys.stderr)
    # A new image goes to the end, so the cards above it keep their places.
    return sorted(found, key=lambda record: record["registered_at"])


def waiting_records(root: Path) -> list[image_store.ImageRecord]:
    """Return the Images records that still wait for the user's answer."""
    return [record for record in records(root) if "feedback_at" not in record]


def waiting_images(request: console.NewsRequest) -> frozenset[str]:
    """Name the images that wait for the user, for the console's new mark."""
    return frozenset(record["id"] for record in waiting_records(request.root))


def register(image: Path, caption: str, owner: staff.StaffId, subject: str,
             *, before: Path | None = None) -> image_store.ImageRecord:
    """Register one image or a before and after pair for its staff owner."""
    if not caption.strip():
        raise ValueError("the caption is empty; say what changed and what to look at")
    if staff.snapshot().by_id(owner) is None:
        raise ValueError(f"no staff record for owner {owner}")
    return image_store.register_file(image, images_root(review_link.review_root().resolve()),
                                     caption, str(owner), subject, before=before)


def delete(root: Path, identity: str) -> None:
    if not image_store.IDENTITY.fullmatch(identity):
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


def picture(record: image_store.ImageRecord, label: str, file: str) -> str:
    """Link one image to the shared zoom; show its pair label above it."""
    path = html.escape(image_path(record, file), quote=True)
    alt = record["caption"].splitlines()[0]
    linked = (f'<a data-image-zoom href="{path}" target="_blank" rel="noopener"><img src="{path}" '
              + f'alt="{html.escape(f"{label}: {alt}" if label else alt, quote=True)}" loading="lazy"></a>')
    return f'<figure><figcaption>{label}</figcaption>{linked}</figure>' if label else linked


def card(record: image_store.ImageRecord, token: str, members: staff.StaffSnapshot, feedback_rows: int) -> str:
    """Show the caption as the title, then the aligned details, the image or pair, the feedback field and the answer buttons."""
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
            + '<div class="image-actions"><button type="submit" name="action" value="approved">Approved</button>'
            + '<button type="submit" name="action" value="not approved">Not Approved</button>'
            + '<button type="submit" name="action" value="feedback">Send Feedback</button>'
            + '<span role="status" data-image-status></span></div></form></article>')


def render(root: Path) -> bytes:
    token = console_token(root)
    members = staff.snapshot()
    rows = conf.read("review_links").whole("feedback_rows")
    cards = "".join(card(record, token, members, rows) for record in waiting_records(root))
    content = (STYLE + '<h1 class="page-title">Images</h1><div data-image-filter><label for="image-window">Show:</label>'
               + '<select id="image-window"><option value="day">Last day</option>'
               + '<option value="week" selected>Last 7 days</option><option value="all">All</option></select></div>'
               + '<div data-image-layout><nav data-image-groups aria-label="Image groups"></nav>'
               + f'<div><div data-images data-script="{SCRIPT_VERSION}">{cards}</div><p hidden data-empty></p></div></div>'
               + '<noscript>Enable JavaScript to filter images or answer on an image.</noscript>'
               + f'<script>{SCRIPT}</script>')
    return change_page.document(content, "Images")


def submit(root: Path, body: str) -> None:
    """Send the answer on the image that a signed form names to its owner, or delete the image for an older page."""
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    # A page loaded before Send feedback existed posts only the image and its token, which means delete.
    if not {"image", "token"} <= set(fields) <= {"image", "token", "feedback", "action"} or any(
            len(value) != 1 for value in fields.values()):
        raise change_page.AnswerRefused("use the form on this page")
    identity = fields["image"][0]
    if not image_store.IDENTITY.fullmatch(identity):
        raise change_page.AnswerRefused("not an image id")
    expected = request_token(console_token(root), identity)
    if not secrets.compare_digest(fields["token"][0].encode(), expected.encode()):
        raise change_page.AnswerRefused("the image token does not match this page")
    action = fields.get("action", ["delete"])[0]
    if action not in ACTIONS:
        raise change_page.AnswerRefused("use the form on this page")
    if action in ANSWERS:
        send_answer(root, identity, action, fields.get("feedback", [""])[0].strip())
        return
    with file_lock.exclusive(images_root(root) / LOCK):
        try:
            delete(root, identity)
        except ValueError as error:
            raise change_page.AnswerRefused(str(error)) from error


def answer_message(record: image_store.ImageRecord, answer: str, feedback: str) -> str:
    """Name the answer, then the feedback when the user wrote some, the image, and the delete command."""
    origin = f"http://{review_link.link_host()}:{conf.read('review_links').whole('server_port')}"
    return "\n".join((
        f"From the user (via the Images page): {ANSWERS[answer]}",
        *((f"Feedback: {feedback}",) if feedback else ()),
        *(f"{label or 'Image'}: {origin}{image_path(record, file)}" for label, file in shown_files(record)),
        f"Caption: {record['caption']}", f"For: {record['subject']}",
        "After you act on it, delete the image: "
        + f"~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id {record['id']}"))


def send_answer(root: Path, identity: str, answer: str, feedback: str) -> None:
    """Send the answer to the owner's current session, then hide the image until the owner deletes it."""
    if answer == "feedback" and not feedback:
        raise change_page.AnswerRefused("enter feedback")
    folder = images_root(root) / identity
    try:
        record = image_store.read_record(folder)
    except (OSError, ValueError) as error:
        raise change_page.AnswerRefused(f"no image {identity}") from error
    settings = conf.read("review_links")
    failures = change_page.send_with_retries(SENDER, (staff.StaffId(record["owner"]),),
                                             answer_message(record, answer, feedback), settings.whole("delivery_tries"),
                                             settings.number("delivery_pause_seconds"))
    if failures:
        raise AnswerNotSent(failures[-1].error)
    with file_lock.exclusive(images_root(root) / LOCK):
        try:
            current = image_store.read_record(folder)
        except (OSError, ValueError):
            # The owner deleted the image while the message was on its way.
            return
        current["feedback_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        image_store.write_record(folder, current)


# At 1108px and wider, the page fills the frame inside its 32px gutters, and the
# image column keeps its full 680px. 1108 = 32 + 300 + 64 + 680 + 32.
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
.image-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.image-actions button{margin:0}
@media (max-width:480px){.image-actions{gap:6px}.image-actions button{font-size:15px;padding:6px 8px}}
main{max-width:1124px}
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
[data-image-groups]{position:static;max-height:none;margin-bottom:16px;grid-template-columns:minmax(0,1fr) max-content max-content}}
@media (min-width:1108px){main{max-width:none}
[data-image-layout]{grid-template-columns:300px minmax(0,1fr);column-gap:64px}
[data-image-layout]>div{width:100%;max-width:1376px;justify-self:center}
[data-images] article:not(.pair),[data-empty]{max-width:680px;margin-inline:auto}}
</style>"""

SCRIPT = page_frame.REFRESH_SCRIPT + page_frame.MOTION_SCRIPT + r"""(() => {
const motion = createCardMotion();
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
      selectImage(row.card);
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
    // The card fades before removal; a failed request restores the card.
    const card = form.closest('article'), home = card.parentNode, next = card.nextSibling;
    const body = new URLSearchParams(new FormData(form, event.submitter));
    status.textContent = '';
    sending++;
    await motion.removeCard(card, apply);
    try {
      const response = await fetch(location.pathname, {method: 'POST', body});
      if (!response.ok) {
        const reason = await response.json().then(body => body.reason, () => '');
        throw new Error(reason || 'HTTP ' + response.status);
      }
    } catch (error) {
      home.insertBefore(card, next?.parentNode === home ? next : null);
      status.textContent = 'Not sent: ' + error.message;
      apply();
    } finally {
      sending--;
    }
  }));
}
async function refresh() {
  if (sending) return;
  try {
    const response = await fetch(location.pathname, {cache: 'no-store'});
    if (!response.ok) return;
    const fresh = new DOMParser().parseFromString(await response.text(), 'text/html').querySelector('[data-images]');
    if (!fresh || sending) return;
    // A page from an older server script cannot bind the new cards, so it loads again.
    if (fresh.dataset.script !== list.dataset.script) {
      location.reload();
      return;
    }
    const top = scrollY;
    motion.finish();
    list.replaceWith(fresh);
    list = fresh;
    bind(list);
    apply();
    scrollTo({top, behavior: 'instant'});
  } catch (_) {}
}
function selectImage(target) {
  if (target.hidden) {
    menu.value = 'all';
    apply();
  }
  void motion.scrollCard(target);
}
function openImageLink() {
  const start = new URLSearchParams(location.hash.slice(1));
  const target = document.getElementById('image-' + start.get('image'));
  if (target) selectImage(target);
}
const start = new URLSearchParams(location.hash.slice(1));
if (start.get('show') in spans) menu.value = start.get('show');
menu.addEventListener('change', () => { motion.finish(); apply(); });
addEventListener('hashchange', openImageLink);
createPageRefresh(refresh);
bind(list);
// An image link opens its image with a longer period when the image is older.
const linkedImage = start.get('image');
apply();
if (linkedImage) {
  const target = document.getElementById('image-' + linkedImage);
  if (target) {
    if (target.hidden) { menu.value = 'all'; apply(); }
    motion.scroll(target, true);
  }
}
})();"""
SCRIPT_VERSION = change_page.script_version(SCRIPT + page_frame.ZOOM_SCRIPT + page_frame.ZOOM_STYLE)


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
