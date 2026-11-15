"""Prepare every change once, with staff tabs and local browser navigation."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts.lib import conf, urgency
from scripts.lib.user_zone import shown_time
from scripts.production import change_review, change_routing, staff, update_registration
from scripts.production.production_doc import read_production
from scripts.production.issues import frontmatter
from scripts.production.change_routing import UnavailableProduction
from scripts.review_links import change_page
from scripts.review_links.change_page import ReviewBrowser as ReviewBrowser
from scripts.statusline import rebuild_row

MANIFEST = change_page.BROWSER_MANIFEST
FOLDER = "/(" + change_page.CONSOLE_FOLDER + "|[a-z0-9]{6})"


@dataclass(frozen=True)
class WaitingChange:
    key: str
    tab: str
    page: change_page.AssignedReviewPage | UnavailableProduction
    row: dict[str, str]
    slots: change_page.DeliverySlots = change_page.DeliverySlots(change_page.NoDelivery(), change_page.NoDelivery())
    anomaly: str = ""
    @property
    def question(self) -> change_page.NoQuestion | change_page.OpenQuestion | change_page.AnsweredQuestion:
        return self.slots.question

    @property
    def waiting(self) -> bool:
        return self.slots.waits_for_user(bool(self.row.get("Verdict")))


@dataclass(frozen=True)
class WaitingQuestion:
    """A filed question stays in its owning production's review tab."""

    key: str
    tab: str
    record: update_registration.QuestionRecord
    production: Path | update_registration.QuestionOwnerUnavailable
    owner: staff.StaffId
    asker: str


@dataclass(frozen=True)
class PreparedReviews:
    changes: tuple[WaitingChange, ...]
    queues: dict[str, change_review.ChangeQueue]
    questions: tuple[WaitingQuestion, ...]
    question_tabs: tuple[str, ...]
    record: staff.StaffSnapshot


def page_at(root: Path, path: str) -> ReviewBrowser:
    match = re.fullmatch(FOLDER + r"/changes\.html", urlsplit(path).path)
    if match is None:
        raise FileNotFoundError("not a change browser")
    token, initial = browser_manifest(browser_folder(root, match[1]))
    kinds = parse_qs(urlsplit(path).query).get("type", ["changes"])
    match kinds:
        case ["changes"]:
            return ReviewBrowser(token, initial, "changes")
        case ["questions"]:
            return ReviewBrowser(token, initial, "questions")
        case _:
            raise ValueError("type must be changes or questions")


def rebuilds_at(root: Path, path: str) -> None:
    """Accept the rebuild needs path only beside a published browser."""
    match = re.fullmatch(FOLDER + "/" + re.escape(change_page.REBUILDS_NAME), urlsplit(path).path)
    if match is None:
        raise FileNotFoundError("not a rebuild page")
    _ = browser_manifest(browser_folder(root, match[1]))


def browser_folder(root: Path, name: str) -> Path:
    """Give the fixed console folder, or an older browser snapshot that still accepts answers."""
    return change_page.console_folder(root) if name == change_page.CONSOLE_FOLDER else root / name


def browser_manifest(folder: Path) -> tuple[str, str]:
    manifest = folder / MANIFEST
    if folder.is_symlink() or manifest.is_symlink():
        raise ValueError("not a regular browser manifest")
    value = cast(object, json.loads(manifest.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError("invalid browser manifest")
    fields = cast(dict[str, object], value)
    token, initial = fields.get("token"), fields.get("initial")
    if not isinstance(token, str) or not token or not isinstance(initial, str):
        raise ValueError("invalid browser manifest")
    return token, initial



def row_token(browser: ReviewBrowser, key: str) -> str:
    return hmac.new(browser.token.encode(), key.encode(), hashlib.sha256).hexdigest()


def prepare(browser: ReviewBrowser, status: change_page.DeliveryStatus) -> PreparedReviews:
    delivery = status.read_view()
    snapshots = {path: change_review.review_snapshot(path)
                 for path in sorted(change_review.lists_directory().glob(change_review.PREFIX + "*.md"))}
    routing = change_page.list_routing(snapshots)
    grouped: dict[str, list[WaitingChange]] = {path.stem.removeprefix(change_review.PREFIX): [] for path in snapshots}
    grouped.update({runner.name: [] for runner in routing.runners.values()})
    defaults: dict[str, Path] = {}
    for path, snapshot in snapshots.items():
        fallback = path.stem.removeprefix(change_review.PREFIX)
        defaults[fallback] = path
        for row in snapshot.pending:
            assignment = routing.owner_and_production(path, row)
            owner = assignment.owner
            tab = owner.tab
            key = fallback + "/" + row["#"]
            doc = assignment.production
            page = (change_page.AssignedReviewPage(path, doc, (row["#"],), row_token(browser, key), owner)
                    if isinstance(doc, Path) else doc)
            slots = delivery.slots(change_page.ReviewPage(path, path, (row["#"],), ""), row["#"])
            grouped.setdefault(tab, []).append(WaitingChange(
                key, tab, page, row, slots,
                owner.note if isinstance(owner, change_routing.UnroutedChange) else ""))
    queues: dict[str, change_review.ChangeQueue] = {}
    changes: list[WaitingChange] = []
    for tab, items in sorted(grouped.items()):
        if not items:
            queues[tab] = change_review.ChangeQueue("", [])
            continue
        page = items[0].page
        runner = next((runner for runner in routing.runners.values() if runner.name == tab), None)
        root = runner
        while root is not None:
            parent = root.parent
            if not isinstance(parent, staff.ReportsTo) or parent.id not in routing.runners:
                break
            root = routing.runners[parent.id]
        current = (defaults.get(root.name) if root is not None else None) or routing.list_paths.get(tab) or defaults.get(tab, page.review_list)
        name = change_review.queue(current)
        if runner is not None and isinstance(runner.parent, staff.ReportsTo) and isinstance(page, change_page.AssignedReviewPage):
            name += "-" + runner.name
        entries: list[urgency.Entry[dict[str, str]]] = []
        by_key: dict[str, WaitingChange] = {}
        for item in items:
            key = item.row["#"] if item.page.review_list == current else item.key
            by_key[key] = item
            if item.waiting:
                entry = change_review.row_entries([item.row])[0]
                after = change_review.prerequisite_key(entry.after, item.page.review_list, current)
                entries.append(replace(entry, key=key, item=item.row | {"#": key}, after=after))
        prepared = change_review.ChangeQueue(name, entries)
        changes.extend(by_key[entry.key] for entry in urgency.queue_order(prepared.entries, prepared.name))
        changes.extend(item for item in items if not item.waiting)
        queues[tab] = prepared
    queues = dict(sorted(queues.items(), key=lambda item: not item[1].entries))
    identities = update_registration.QuestionStaff.for_record(routing.record)
    questions: list[WaitingQuestion] = []
    for document, lines in routing.document_lines.items():
        for question in update_registration.question_records_for_document(document, identities, include_unrouted=True, lines=lines):
            if question.status != "open":
                continue
            owner = routing.runners.get(question.owner)
            tab = owner.name if owner is not None else "unrouted"
            asker = routing.record.by_id(question.asker)
            questions.append(WaitingQuestion("question/" + question.owner + "/" + question.path.name,
                tab, question, document, question.owner, asker.name if asker is not None else str(question.asker)))
    known = {item.record.path for item in questions}
    for question in update_registration.unowned_question_records(identities):
        if question.status != "open" or question.path in known:
            continue
        asker = routing.record.by_id(question.asker)
        questions.append(WaitingQuestion("question/" + question.owner + "/" + question.path.name,
            "unrouted", question, update_registration.QuestionOwnerUnavailable(question.path), question.owner,
            asker.name if asker is not None else str(question.asker)))
    questions.sort(key=lambda item: (item.record.item["since"], item.key))
    question_tabs = tuple(sorted(set(queues) | {item.tab for item in questions},
                                 key=lambda tab: (not any(item.tab == tab for item in questions), tab)))
    return PreparedReviews(tuple(changes), queues, tuple(questions), question_tabs, routing.record)


def section(change: WaitingChange, feedback_rows: int) -> str:
    page, row = change.page, change.row
    shown = change_review.display_id(row, change.tab)
    anomaly = f"<p>{html.escape(change.anomaly)}</p>" if change.anomaly else ""
    if isinstance(page, UnavailableProduction):
        return f'''<section hidden data-type="changes" data-key="{html.escape(change.key)}" data-tab="{html.escape(change.tab)}" data-pending="{str(change.waiting).lower()}">
<h1>{html.escape(row.get('Change', ''))}</h1>
<h2>Proof</h2>{change_page.review_text_html(row.get(change_review.CHECK, ''), "pre")}
{anomaly}{change_page.question_html(change.question)}<p>{html.escape(page.reason)}</p>
{change_page.change_details(row, shown)}</section>'''
    form = change_page.answer_form(page, change_page.AnswerNotice(row["#"]), feedback_rows).replace(
        '<form method="post">', f'<form method="post" action="{change_page.PAGE_NAME}">') if change.waiting else ""
    text = change_page.answer_status(row.get("Verdict", ""), change.slots)
    saved = f'<p>{html.escape(text)}</p>' if text else ""
    return f'''<section hidden data-type="changes" data-key="{html.escape(change.key)}" data-tab="{html.escape(change.tab)}" data-pending="{str(change.waiting).lower()}">
<h1>{html.escape(row.get('Change', ''))}</h1>
<h2>{'Proof' if row.get(change_review.KIND) == 'Read only' else 'How to check'}</h2>
{change_page.review_text_html(row.get(change_review.CHECK, ''), "pre")}
{anomaly}{change_page.question_html(change.question)}{saved}<p role="status" data-save-state></p>{form}
{change_page.change_details(row, shown)}</section>'''


# A question stored as one text marks its parts inline: numbered items, then "Choices:" and "Recommended:".
CHOICES_MARK = re.compile(r"(?:^|(?<=\s))Choices:\s")
RECOMMEND_MARK = re.compile(r"(?:^|(?<=\s))Recommended:\s")
ITEM_MARKS = (re.compile(r"(?<![\w(])(\d+)\)\s"), re.compile(r"\((\d+)\)\s"), re.compile(r"(?<![\w.])(\d+)\.\s"),
              re.compile(r"\(([a-z])\)\s"))
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
BLANK_LINE = re.compile(r"\n\s*\n")


def split_at(text: str, mark: re.Pattern[str]) -> tuple[str, str]:
    found = mark.search(text)
    return (text, "") if found is None else (text[:found.start()].strip(), text[found.end():].strip())


def numbered_run(text: str) -> tuple[str, list[str], str]:
    """Split the text at its first run of two or more items marked 1) 2), 1. 2., (1) (2) or (a) (b).

    Return the text before the run, the items and the text after a blank line in the last item."""
    best: list[re.Match[str]] = []
    for style in ITEM_MARKS:
        run: list[re.Match[str]] = []
        for mark in style.finditer(text):
            value = mark[1]
            if not run and value in ("1", "a"):
                run.append(mark)
            elif run and value == (str(int(run[-1][1]) + 1) if value.isdigit() else chr(ord(run[-1][1]) + 1)):
                run.append(mark)
        if len(run) >= 2 and (not best or run[0].start() < best[0].start()):
            best = run
    if not best:
        return text, [], ""
    ends = [mark.start() for mark in best[1:]] + [len(text)]
    items = [text[mark.end():end].strip() for mark, end in zip(best, ends)]
    last = BLANK_LINE.split(items[-1], maxsplit=1)
    items[-1] = last[0]
    return text[:best[0].start()].strip(), [item.rstrip(";,").strip() for item in items], last[1] if len(last) > 1 else ""


def item_list(items: list[str]) -> str:
    return '<h2>Items</h2><ol class="question-items">' + "".join(
        "<li>" + "<br>".join(change_page.linked(line) for line in entry.splitlines()) + "</li>" for entry in items) + "</ol>"


def choice_buttons(choices: list[str]) -> str:
    buttons = "".join(f'<button type="button" data-choice="{html.escape(choice, quote=True)}" aria-pressed="false">'
                      + f'{html.escape(choice)}</button>' for choice in choices)
    return f'<fieldset class="question-choices"><legend>Choices</legend>{buttons}</fieldset>' if buttons else ""


def text_sections(text: str) -> tuple[str, str]:
    """Show a question stored as one text in sections: the text, its items, its choices and the recommendation."""
    rest, recommend = split_at(text, RECOMMEND_MARK)
    lead, choice_text = split_at(rest, CHOICES_MARK)
    lead, items, trailing = numbered_run(lead)
    if items and "\n" not in text:
        # In one paragraph, the text after the last item's first sentence is not part of that item.
        last = SENTENCE_END.split(items[-1], maxsplit=1)
        items[-1], trailing = last[0], last[1] if len(last) > 1 else ""
    choice_lead, choices, choice_trailing = numbered_run(choice_text)
    if not items and not choice_text and not recommend:
        return change_page.review_text_html(text, links=True), ""
    body = change_page.review_text_html(lead, links=True) if lead else ""
    body += (item_list(items) if items else "") + (change_page.review_text_html(trailing, links=True) if trailing else "")
    # Only numbered choices become buttons; other choice text shows as text.
    if choice_text and not choices:
        body += '<h2>Choices</h2>' + change_page.review_text_html(choice_text, links=True)
    elif choice_lead:
        body += '<h2>Choices</h2>' + change_page.review_text_html(choice_lead, links=True)
    if choice_trailing:
        body += change_page.review_text_html(choice_trailing, links=True)
    if recommend:
        body += '<h2>Recommended</h2>' + change_page.review_text_html(recommend, links=True)
    return body, choice_buttons(choices)


def question_parts(item: update_registration.OutstandingItem) -> tuple[str, str]:
    """Return the question body and its choice buttons; a question stored as one text shows in sections."""
    if "parts" not in item:
        return text_sections(item.get("question", ""))
    parts = item["parts"]
    same = parts["ask"].strip().rstrip("?.").casefold() == item["text"].strip().rstrip("?.").casefold()
    body = "" if same else f'<p class="question-ask">{change_page.linked(parts["ask"])}</p>'
    if parts["context"].strip():
        body += '<h2>Context</h2>' + change_page.review_text_html(parts["context"], links=True)
    if parts["items"]:
        body += item_list(parts["items"])
    if parts["recommend"].strip():
        body += '<h2>Recommended</h2>' + change_page.review_text_html(parts["recommend"], links=True)
    return body, choice_buttons(parts["choices"])


def question_section(question: WaitingQuestion, browser: ReviewBrowser, feedback_rows: int) -> str:
    key = html.escape(question.key)
    body, choices = question_parts(question.record.item)
    owned = question.tab != "unrouted"
    # Without an owner there is no form, so the choices show as text the user cannot pick.
    shown_choices = "" if owned else choices.replace('<button type="button"', '<button type="button" disabled')
    form = '<p>No showrunner owns this question.</p>' if not owned else f'''<form method="post" action="{change_page.PAGE_NAME}">
<input type="hidden" name="row" value="{key}">
<input type="hidden" name="token" value="{row_token(browser, question.key)}">{choices}
<label for="answer-{key}">Answer</label><textarea id="answer-{key}" name="feedback" rows="{feedback_rows}"></textarea>
<button name="outcome" value="answer">Save answer</button></form>'''
    return f'''<section hidden data-type="questions" data-key="{key}" data-tab="{html.escape(question.tab)}" data-pending="true">
<h1>{html.escape(question.record.item["text"])}</h1>
{body}{shown_choices}
<p>Asked by {html.escape(question.asker)}</p><p role="status" data-save-state></p>{form}</section>'''


def rebuild_list(answer: rebuild_row.RebuildAnswer, changes: dict[str, tuple[rebuild_row.Change, ...]],
                 record: staff.StaffSnapshot) -> str:
    """Show each machine the status line names, then one entry per change: who made it, when and why."""
    def who(session: str) -> str:
        member = record.by_member_reference(session) if session else None
        return (f"{member.name} ({member.kind})" if member is not None
                else f"Unknown session: {session}" if session else "Unknown session")

    def entry(session: str, at: str, reason: str) -> tuple[datetime, str]:
        moment = datetime.fromisoformat(at)
        return moment, (f'<li><dl><dt>Who</dt><dd>{html.escape(who(session))}</dd>'
                        + f'<dt>When</dt><dd><time datetime="{html.escape(at, quote=True)}">{shown_time(moment)}</time></dd>'
                        + f'<dt>Reason</dt><dd>{html.escape(reason)}</dd></dl></li>')

    names = ["the Mac" if machine == "mac" else machine for machine in answer.machines]
    blocks: list[str] = []
    for machine_name, name in zip(answer.machines, names):
        records = next((machine.records for machine in answer.needs if machine.newest["machine"] == machine_name), ())
        filed = {item["nixos_commit"] for item in records}
        # A filed need and its commit are one change; the need gives the requester and the reason.
        items = [entry(item["requested_by"] or item["concerns"], item["created_at"], item["reason"]) for item in records]
        items += [entry(change.session, change.at, change.reason)
                  for change in changes.get(machine_name, ()) if change.commit not in filed]
        listed = ('<ul>' + ''.join(shown for _, shown in sorted(items, key=lambda item: item[0], reverse=True)) + '</ul>'
                  if items else '<p>The change list is not available yet.</p>')
        blocks.append(f'<article data-machine="{html.escape(machine_name)}"><h2>Rebuild {html.escape(name)}</h2>{listed}</article>')
    label = f"Rebuild {' and '.join(names)}" if names else ""
    return (f'<div data-rebuilds data-count="{len(names)}" data-label="{html.escape(label, quote=True)}">'
            + (''.join(blocks) if blocks else '<p>No rebuilds needed.</p>') + '</div>')


REBUILD_STYLE = """<style>[data-rebuilds] article+article{border-top:1px solid GrayText;margin-top:32px;padding-top:16px}
[data-rebuilds] h2{margin-top:0}[data-rebuilds] ul{list-style:none;padding:0}[data-rebuilds] li+li{margin-top:16px}
[data-rebuilds] dl{display:grid;grid-template-columns:5em minmax(0,1fr);column-gap:12px;row-gap:4px;margin:0;font-size:15px}
[data-rebuilds] dt{color:GrayText}[data-rebuilds] dd{margin:0;min-width:0;overflow-wrap:anywhere}</style>"""


def render_rebuilds() -> bytes:
    """Render the status line's rebuild answer for the console's rebuild link and overlay."""
    config = conf.path("statusline")
    rebuilds = rebuild_list(rebuild_row.read(config), rebuild_row.read_changes(config), staff.snapshot())
    return change_page.document(REBUILD_STYLE + '<h1>Rebuilds needed</h1>' + rebuilds, "Rebuilds needed")


def render(browser: ReviewBrowser, status: change_page.DeliveryStatus) -> bytes:
    settings = conf.read("review_links")
    feedback_rows = settings.whole("feedback_rows")
    refresh_seconds = settings.number("page_refresh_seconds")
    prepared = prepare(browser, status)
    kind = browser.kind
    names = prepared.queues if kind == "changes" else prepared.question_tabs
    menus: list[str] = []
    for name in names:
        count = (sum(item.tab == name and item.waiting for item in prepared.changes) if kind == "changes"
                 else sum(item.tab == name for item in prepared.questions))
        label = "Owner unavailable" if name == "unrouted" else name
        menus.append(f'<option data-showrunner="{html.escape(name)}" '
                     + f'data-count="{count}" value="{html.escape(name)}">{html.escape(label)} ({count})</option>')
    cards = (''.join(section(item, feedback_rows) for item in prepared.changes) if kind == "changes"
             else ''.join(question_section(item, browser, feedback_rows) for item in prepared.questions))
    content = (PAGE_STYLE + f'<div data-review-content data-kind="{kind}" data-refresh-seconds="{refresh_seconds}">'
               + f'<h1>{change_page.TITLES[kind]}</h1>'
               + '<div data-showrunner-filter><label for="review-showrunner">Showrunner:</label>'
               + '<select id="review-showrunner" aria-label="Showrunner"><option value="">All</option></select></div>'
               + '<template data-runner-options>' + ''.join(menus) + '</template>'
               + '<p hidden role="status" data-save-notice></p>' + cards
               + '<p hidden data-empty></p><div class="navigation"><button type="button" data-next hidden>Skip for now</button></div>'
               + '<noscript>Enable JavaScript to review items and use Skip for now.</noscript></div>'
               + '<script>const initial=' + json.dumps(browser.initial).replace('<', '\\u003c') + ';' + SCRIPT + '</script>')
    return change_page.document(content, change_page.TITLES[kind])


PAGE_STYLE = """<style>[hidden]{display:none!important}
[data-showrunner-filter]{display:flex;align-items:center;gap:8px;flex-wrap:nowrap;margin-bottom:16px}
[data-showrunner-filter] label{white-space:nowrap}
[data-showrunner-filter] select{font:inherit;min-width:0;max-width:100%;flex:1;min-height:44px;color:CanvasText;background:Canvas}
section+section{border:0;margin:0;padding:0}
.question-ask{font-weight:600}.question-items li{margin:8px 0}
.question-choices{display:flex;flex-wrap:wrap;gap:8px;border:0;margin:16px 0;padding:0}
.question-choices legend{padding:0;margin-bottom:8px;font-weight:600}
.question-choices button{text-align:left}.question-choices button[aria-pressed="true"]{outline:2px solid Highlight;outline-offset:2px}
.navigation{padding:12px 0;background:Canvas}.navigation:empty{display:none}button{min-height:44px}
</style>"""


def submit(browser: ReviewBrowser, body: str, status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"row", "token", "outcome", "feedback"} or any(len(value) != 1 for value in fields.values()):
        raise change_page.AnswerRefused("use the answer form on this page")
    key = fields["row"][0]
    if not secrets.compare_digest(fields["token"][0].encode(), row_token(browser, key).encode()):
        raise change_page.AnswerRefused("row or page token does not match this page")
    prepared = prepare(browser, status)
    if key.startswith("question/"):
        question = next((item for item in prepared.questions if item.key == key), None)
        if question is None:
            raise change_page.QuestionAnswerRefused("this question is no longer open; refresh the page")
        if fields["outcome"] != ["answer"] or not fields["feedback"][0].strip():
            raise change_page.AnswerRefused("enter an answer")
        return submit_question(question, fields["feedback"][0], status)
    found = next((item for item in prepared.changes if item.key == key), None)
    if found is None:
        # A repeated save uses its original signed token.
        source, separator, row_id = key.partition("/")
        paths = {path.stem.removeprefix(change_review.PREFIX): path
                 for path in change_review.lists_directory().glob(change_review.PREFIX + "*.md")}
        if not separator or source not in paths or re.fullmatch(r"C\d+", row_id) is None:
            raise change_page.AnswerRefused("this change is not on the page")
        empty: dict[str, str] = {}
        row = next((row for row in change_review.review_rows(paths[source]) if row["#"] == row_id), empty)
        if not row.get("Verdict"):
            raise change_page.AnswerRefused("this change is not on the page")
        routing = change_page.list_routing({paths[source]: change_review.review_snapshot(paths[source])})
        assignment = routing.owner_and_production(paths[source], row)
        owner = assignment.owner
        tab = owner.tab
        doc = assignment.production
        if isinstance(doc, UnavailableProduction):
            raise change_page.AnswerRefused("the production document is not available")
        page = change_page.AssignedReviewPage(paths[source], doc, (row_id,), row_token(browser, key), owner)
        found = WaitingChange(key, tab, page, row)
    if isinstance(found.page, UnavailableProduction):
        raise change_page.AnswerRefused("the production document is not available")
    fields["row"] = [found.row["#"]]
    rewritten = urlencode({key: values[0] for key, values in fields.items()})
    delivery = change_page.submit(found.page, rewritten, status, prepared=prepared.queues[found.tab])
    answer = ""
    if fields["outcome"] not in (["validate"], ["question"]):
        answer = next(row.get("Verdict", "") for row in change_review.review_rows(found.page.review_list)
                      if row["#"] == found.row["#"])
    return delivery, answer


def submit_question(question: WaitingQuestion, answer: str, status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery, str]:
    """Save through the production API before the existing sender delivers the answer."""
    if question.tab == "unrouted":
        raise change_page.QuestionAnswerRefused("No showrunner owns this question.")
    identities = update_registration.QuestionStaff.read()
    production = read_production(question.owner, identities.record)
    item = question.record.item
    relay = update_registration.answer_relay(production, production.name,
        update_registration.question_asker(item, update_registration.OpenItemWithoutAsker(), identities), identities)
    recipient = relay.recipient if isinstance(relay.recipient, str) else question.owner
    message = "From the user (via the review page): " + item["text"]
    if relay.unit:
        message += "\nFor: " + relay.unit
    message += "\n" + answer
    saved: change_page.StoredUserAnswer = {"saved": False, "source": str(question.record.path), "production": str(production.doc),
        "owner": question.owner, "question": item["text"], "answer": answer, "message": message}
    page = change_page.UserQuestionPage(question.record.path, production.doc, (question.record.path.stem,), "", saved)
    # The delivery lock prevents startup recovery from reading an answer during its save.
    with status.locked():
        if frontmatter(question.record.path).get("status") != ["open"]:
            raise change_page.QuestionAnswerRefused("this question is no longer open; refresh the page")
        attempt = secrets.token_hex()
        intent = change_page.DeliveryIntent(attempt, "verdict", recipients=(recipient,))
        status.write_slots(page, page.rows[0], change_page.DeliverySlots(change_page.NoDelivery(), intent))
        status.request_attempts[attempt] = change_page.AttemptOwnership(status.path(page, page.rows[0]), "claimed")
        try:
            relay = update_registration.answer(production, item["text"], identities=identities, only_open=True)
            recipient = relay.recipient if isinstance(relay.recipient, str) else question.owner
            intent = replace(intent, recipients=(recipient,))
            page = replace(page, saved={**saved, "saved": True})
            status.write_slots(page, page.rows[0], change_page.DeliverySlots(change_page.NoDelivery(), intent))
        except BaseException as error:
            try:
                status.clear_locked(page, page.rows[0], status.slots(page, page.rows[0]), "verdict")
            finally:
                _ = status.request_attempts.pop(attempt, None)
            if isinstance(error, update_registration.RegistrationFailure):
                raise change_page.QuestionAnswerRefused("this question is no longer open; refresh the page") from error
            raise
    return change_page.MessageDelivery(page, page.rows[0], (recipient,), message, attempt, (question.owner,)), answer


SCRIPT = r'''
let content=document.querySelector('[data-review-content]');
let rows,runnerOptions,showrunners,runnerMenu,next,empty,saveNotice;
let shownPage=content.outerHTML, heldPage=null, newestRequest=0, activeLoads=0, activeSaves=0, refreshTimer;
const remembered=new Map(), kind=content.dataset.kind; let current=null, tab='', navigation=0, shownHash='';
function bindPage(){
 rows=[...content.querySelectorAll('section[data-key]')];
 runnerOptions=[...content.querySelector('[data-runner-options]').content.children];
 showrunners=content.querySelector('[data-showrunner-filter]');runnerMenu=showrunners.querySelector('select');
 next=content.querySelector('[data-next]');empty=content.querySelector('[data-empty]');
 saveNotice=content.querySelector('[data-save-notice]');
 runnerMenu.addEventListener('change',()=>{tab=runnerMenu.value;go(remembered.get(tab));});
 next.addEventListener('click',()=>advance(current));
 rows.forEach(row=>{
  const form=row.querySelector('form');
  if(form) form.addEventListener('submit',event=>{event.preventDefault();save(row,form,event.submitter.value);});
  // A choice fills the Answer box; the user can add words, then saves.
  form?.querySelectorAll('[data-choice]').forEach(choice=>choice.addEventListener('click',()=>{
   const box=form.querySelector('textarea');box.value=choice.dataset.choice;
   form.querySelectorAll('[data-choice]').forEach(other=>other.setAttribute('aria-pressed',String(other===choice)));
   box.dispatchEvent(new Event('input',{bubbles:true}));box.focus();
  }));
 });
}
function waiting(){return rows.filter(r=>(tab==='' || r.dataset.tab===tab) && r.dataset.pending==='true');}
function show(){
 const place=new URLSearchParams(location.hash.slice(1));
 const requested=place.has('row') ? place.get('row') : initial;
 tab=place.get('showrunner') || '';
 if(tab!=='' && !runnerOptions.some(option=>option.value===tab)) tab='';
 const pending=rows.filter(row=>row.dataset.pending==='true');
 runnerMenu.replaceChildren(new Option('All ('+pending.length+')',''),...runnerOptions.map(option=>{
  const item=option.cloneNode(true);
  item.textContent=(item.value==='unrouted' ? 'Owner unavailable' : item.value)+' ('+pending.filter(row=>row.dataset.tab===item.value).length+')';
  return item;
 }));runnerMenu.value=tab;
 current=rows.find(r=>r.dataset.key===requested && (tab==='' || r.dataset.tab===tab)) || waiting()[0] || null;
 empty.hidden=current!==null;
 empty.textContent=kind==='questions' ? (pending.length ? 'No questions for this showrunner.' : 'No questions to answer.') :
  (pending.length ? 'No changes for this showrunner.' : 'No changes to review.');
 rows.forEach(r=>r.hidden=r!==current);
 next.hidden=!waiting().some(r=>r!==current);
 (current?.querySelector('form:not([hidden]) .answer-actions') || content.querySelector('.navigation')).append(next);
 if(current) remembered.set(tab,current.dataset.key);
 // The default place, All with no card, keeps a plain URL.
 history.replaceState(null,'',tab || current ? '#'+new URLSearchParams({showrunner:tab,row:current?.dataset.key || ''}) : location.pathname+location.search);
 shownHash=location.hash;
 // The console records this place in its own URL, so a reload or a shared link opens it again.
 if(parent!==window) parent.postMessage('console-place',location.origin); }
function clearNotice(){saveNotice.hidden=true;saveNotice.textContent='';}
function go(key){navigation++;clearNotice();location.hash=new URLSearchParams({showrunner:tab,row:key || ''});show();}
function advance(row){
 const at=rows.indexOf(row), candidates=waiting();
 const following=candidates.find(r=>rows.indexOf(r)>at) || candidates[0];
 go(following?.dataset.key || ''); }
async function save(row,form,outcome){
 if(form.dataset.saving==='true') return;
 activeSaves++;let saved=false;
 clearNotice();form.dataset.saving='true'; const state=row.querySelector('[data-save-state]'), position=navigation;
 const data=new URLSearchParams(new FormData(form));
 data.set('row',row.dataset.key);data.set('outcome',outcome);
 form.querySelectorAll('button[name=outcome]').forEach(b=>b.setAttribute('aria-pressed',String(b.value===outcome)));
 state.textContent='Saving'; row.dataset.saveState='saving';
 try{
  const response=await fetch(form.action,{method:'POST',body:data,headers:{Accept:'application/json'}});
  const reply=await response.json();
  if(!response.ok) throw new Error(reply.reason || 'The answer could not be saved. Try again.');
  state.textContent=reply.answer ? 'Saved: '+reply.answer : outcome==='question' ? 'The showrunner will answer this' : outcome==='feedback' ? 'The showrunner will act on this' : 'The showrunner will validate this';
  row.dataset.saveState='saved';
  saved=true;
  row.dataset.pending='false';form.hidden=true;
  if(current===row && (tab==='' || tab===row.dataset.tab) && navigation===position){
   advance(row);saveNotice.textContent=kind==='questions' ? 'Answer saved.' : 'Review saved.';saveNotice.hidden=false;
  }
  else show();
 }catch(error){
  row.dataset.saveState='failed'; state.textContent='Not saved: '+(error instanceof SyntaxError ? 'The answer could not be saved. Try again.' : error.message);
 }finally{
  form.dataset.saving='false';activeSaves--;
  // Read after the save, so an older response cannot restore the saved item.
  if(saved){heldPage=null;newestRequest++;refreshPage(true);}
  else applyHeldPage();
 }
}
function detailKey(detail){
 const owner=detail.closest('[data-key]');
 return owner?.dataset.key+'/'+[...owner.querySelectorAll('details')].indexOf(detail);
}
function applyHeldPage(){
 if(!heldPage || activeSaves || [...content.querySelectorAll('textarea,input[type=text]')].some(field=>field.value!=='' && !field.closest('form')?.hidden)) return;
 const fresh=heldPage;heldPage=null;
 if(fresh.outerHTML===shownPage) return;
 const opened=new Set([...content.querySelectorAll('details[open]')].map(detailKey));
 const left=scrollX,top=scrollY,focused=document.activeElement;
 const controlId=focused===runnerMenu ? focused.id : '';
 const fieldRow=focused?.closest('[data-key]')?.dataset.key;
 const fieldName=focused?.matches('textarea,input[type=text]') ? focused.name : '';
 const selection=[focused?.selectionStart,focused?.selectionEnd,focused?.selectionDirection];
 const previous=rows.indexOf(current),following=rows.slice(previous+1).map(row=>row.dataset.key);
 const candidates=[...fresh.querySelectorAll('section[data-key]')].filter(row=>(tab==='' || row.dataset.tab===tab) && row.dataset.pending==='true');
 const keep=candidates.find(row=>row.dataset.key===current?.dataset.key);
 const chosen=keep || following.map(key=>candidates.find(row=>row.dataset.key===key)).find(Boolean) || candidates[0];
 const notice=saveNotice.hidden ? '' : saveNotice.textContent;
 shownPage=fresh.outerHTML;content.replaceWith(fresh);content=fresh;bindPage();
 content.querySelectorAll('details').forEach(detail=>detail.open=opened.has(detailKey(detail)));
 history.replaceState(null,'','#'+new URLSearchParams({showrunner:tab,row:chosen?.dataset.key || ''}));
 show();
 if(notice){saveNotice.textContent=notice;saveNotice.hidden=false;}
 if(controlId) content.querySelector('#'+controlId)?.focus({preventScroll:true});
 const field=rows.find(row=>row.dataset.key===fieldRow)?.querySelector('[name="'+fieldName+'"]');
 if(field && !field.closest('section').hidden){field.focus({preventScroll:true});field.setSelectionRange(...selection);}
 window.scrollTo({left,top,behavior:'instant'});scheduleRefresh();
}
// A console frame that another page covers is hidden and loads no update.
const paused=()=>document.visibilityState!=='visible' || window.frameElement?.hidden===true;
async function refreshPage(supersede=false){
 if(paused() || (activeLoads && !supersede)) return;
 const request=++newestRequest;
 activeLoads++;
 try{
  const response=await fetch(location.href,{cache:'no-store'});
  if(!response.ok) return;
  const fresh=new DOMParser().parseFromString(await response.text(),'text/html').querySelector('[data-review-content]');
  if(request!==newestRequest || !fresh) return;
  heldPage=fresh;applyHeldPage();
 }catch(error){}
 finally{activeLoads--;}
}
function scheduleRefresh(){
 clearInterval(refreshTimer);
 if(document.visibilityState==='visible') refreshTimer=setInterval(refreshPage,Number(content.dataset.refreshSeconds)*1000);
}
document.addEventListener('input',()=>applyHeldPage());
document.addEventListener('visibilitychange',()=>{scheduleRefresh();if(document.visibilityState==='visible') refreshPage(true);});
window.addEventListener('message',event=>{if(event.source===parent && event.origin===location.origin && event.data==='console-shown') refreshPage(true);});
window.addEventListener('hashchange',()=>{if(location.hash!==shownHash){navigation++;clearNotice();}show();});
bindPage();show();scheduleRefresh();
'''
