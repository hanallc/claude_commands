"""Prepare every change once, with staff tabs and local browser navigation."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import shlex
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts.lib import conf, urgency
from scripts.lib.user_zone import shown_time
from scripts.production import change_review, change_routing, next_item, staff, update_registration
from scripts.production.production_doc import Production, read_production
from scripts.production.issues import frontmatter
from scripts.production.change_routing import UnavailableProduction
from scripts.review_links import change_page, console, page_frame
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


def question_order(questions: list[WaitingQuestion]) -> list[WaitingQuestion]:
    """Order the questions as /next orders them: by urgency and age, and a skipped question last."""
    entries = [update_registration.question_entry(question.owner, question.record.item, question.record.path)
               for question in questions]
    by_key = {entry.key: question for entry, question in zip(entries, questions)}
    ended: dict[str, datetime] = {}
    for owner in {question.owner for question in questions}:
        ended |= urgency.ended(update_registration.question_queue_name(owner))
    return [by_key[entry.key] for entry in urgency.order(entries, "", urgency.URGENT, ended)]


@dataclass(frozen=True)
class PreparedReviews:
    changes: tuple[WaitingChange, ...]
    queues: dict[str, change_review.ChangeQueue]
    orders: dict[str, tuple[str, ...]]
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


def waiting_changes(request: console.NewsRequest) -> frozenset[str]:
    """Name the changes that wait for the user, for the console's new mark."""
    return frozenset(item.key for item in request.shared(console_reviews).changes if item.waiting)


def waiting_questions(request: console.NewsRequest) -> frozenset[str]:
    """Name the questions that wait for the user, for the console's new mark."""
    return frozenset(item.key for item in request.shared(console_reviews).questions)


def console_reviews(root: Path) -> PreparedReviews:
    """Read the change lists and questions once for the console's news request."""
    token, initial = browser_manifest(change_page.console_folder(root))
    return prepare(ReviewBrowser(token, initial), change_page.DeliveryStatus(root))


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


def change_orders(grouped: dict[str, list[WaitingChange]], source: change_review.PreparedChangeLists, first_root: str
                  ) -> tuple[dict[str, change_review.ChangeQueue], dict[str, tuple[str, ...]]]:
    """Use each /next queue in turn for All and each showrunner's queue for its view."""
    routing = source.routing
    waiting = {item.key: item for items in grouped.values() for item in items if item.waiting}
    queues: dict[str, change_review.ChangeQueue] = {}
    orders: dict[str, tuple[str, ...]] = {}
    roots: dict[str, tuple[Path, next_item.Queue]] = {}
    for tab in sorted(grouped):
        runner = routing.runners_by_name.get(tab)
        doc = routing.production_by_runner.get(runner.id) if runner is not None else None
        if runner is None or doc is None:
            queues[tab] = change_review.ChangeQueue("", [])
            orders[tab] = tuple(item.key for item in grouped[tab] if item.waiting)
            continue
        path = change_review.scope_list(doc, prepared=source)
        queue = change_review.change_queue(path, doc, prepared=source)
        queues[tab] = queue
        prefix = path.stem.removeprefix(change_review.PREFIX) + "/"
        ordered = urgency.queue_order(next_item.change_queue(doc, prepared=queue).entries, queue.name)
        orders[tab] = tuple(key for entry in ordered
                            if (key := entry.key if "/" in entry.key else prefix + entry.key) in waiting
                            and waiting[key].tab == tab)
        if not isinstance(runner.parent, staff.ReportsTo) or runner.parent.id not in routing.runners:
            roots[runner.name] = (path, next_item.change_queue(doc, below=True, prepared=source))
    all_keys: dict[str, None] = {}
    for name in sorted(roots, key=lambda name: (name != first_root, name)):
        path, queue = roots[name]
        prefix = path.stem.removeprefix(change_review.PREFIX) + "/"
        for entry in urgency.queue_order(queue.entries, queue.name):
            key = entry.key if "/" in entry.key else prefix + entry.key
            if key in waiting:
                all_keys[key] = None
    orders[""] = tuple(all_keys) + tuple(key for key in waiting if key not in all_keys)
    queues = dict(sorted(queues.items(), key=lambda item: (not any(change.waiting for change in grouped[item[0]]), item[0])))
    return queues, orders


def prepare(browser: ReviewBrowser, status: change_page.DeliveryStatus, *,
            settings: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> PreparedReviews:
    if isinstance(settings, conf.UseConfiguredValue):
        settings = conf.read("review_links")
    delivery = status.read_view()
    snapshots = {path: change_review.review_snapshot(path)
                 for path in sorted(change_review.lists_directory().glob(change_review.PREFIX + "*.md"))}
    routing = change_page.list_routing(snapshots)
    grouped: dict[str, list[WaitingChange]] = {path.stem.removeprefix(change_review.PREFIX): [] for path in snapshots}
    grouped.update({runner.name: [] for runner in routing.runners.values()})
    for path, snapshot in snapshots.items():
        fallback = path.stem.removeprefix(change_review.PREFIX)
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
    queues, orders = change_orders(grouped, change_review.PreparedChangeLists(snapshots, routing),
                                  settings.text("changes_first_root"))
    by_key = {item.key: item for items in grouped.values() for item in items}
    changes = [by_key[key] for key in orders[""]]
    changes.extend(item for item in by_key.values() if not item.waiting)
    identities = update_registration.QuestionStaff.for_record(routing.record)
    questions: list[WaitingQuestion] = []
    for document, lines in routing.document_lines.items():
        for question in update_registration.question_records_for_document(document, identities, include_unrouted=True, lines=lines):
            # A question with a follow-up waits for the asker's reply, not for the user.
            if question.status != "open" or "followup" in question.item:
                continue
            owner = routing.runners.get(question.owner)
            tab = owner.name if owner is not None else "unrouted"
            asker = routing.record.by_id(question.asker)
            questions.append(WaitingQuestion("question/" + question.owner + "/" + question.path.name,
                tab, question, document, question.owner, asker.name if asker is not None else str(question.asker)))
    known = {item.record.path for item in questions}
    for question in update_registration.unowned_question_records(identities):
        if question.status != "open" or "followup" in question.item or question.path in known:
            continue
        asker = routing.record.by_id(question.asker)
        questions.append(WaitingQuestion("question/" + question.owner + "/" + question.path.name,
            "unrouted", question, update_registration.QuestionOwnerUnavailable(question.path), question.owner,
            asker.name if asker is not None else str(question.asker)))
    questions = question_order(questions)
    question_tabs = tuple(sorted(set(queues) | {item.tab for item in questions},
                                 key=lambda tab: (not any(item.tab == tab for item in questions), tab)))
    return PreparedReviews(tuple(changes), queues, orders, tuple(questions), question_tabs, routing.record)


def section(change: WaitingChange, feedback_rows: int) -> str:
    page, row = change.page, change.row
    shown = change_review.display_id(row, change.tab)
    anomaly = f"<p>{html.escape(change.anomaly)}</p>" if change.anomaly else ""
    attributes = (f'hidden data-type="changes" data-key="{html.escape(change.key)}" '
                  + f'data-shown="{html.escape(shown)}" data-tab="{html.escape(change.tab)}" '
                  + f'data-pending="{str(change.waiting).lower()}"')
    if isinstance(page, UnavailableProduction):
        content = f'''<h1>{html.escape(row.get('Change', ''))}</h1>
<h2>Proof</h2>{change_page.review_html(row.get(change_review.CHECK, ''))}
{anomaly}{change_page.question_html(change.question)}<p>{html.escape(page.reason)}</p>
{change_page.change_details(row, shown)}'''
    else:
        form = change_page.answer_form(page, change_page.AnswerNotice(row["#"]), feedback_rows).replace(
            '<form method="post">', f'<form method="post" action="{change_page.PAGE_NAME}">') if change.waiting else ""
        text = change_page.answer_status(row.get("Verdict", ""), change.slots)
        saved = f'<p>{html.escape(text)}</p>' if text else ""
        content = f'''<h1>{html.escape(row.get('Change', ''))}</h1>
<h2>{'Proof' if row.get(change_review.KIND) == 'Read only' else 'How to check'}</h2>
{change_page.review_html(row.get(change_review.CHECK, ''))}
{anomaly}{change_page.question_html(change.question)}{saved}<p role="status" data-save-state></p>{form}
{change_page.change_details(row, shown)}'''
    return f'<section {attributes}>' + change_page.change_content(row, content) + '</section>'


# A question stored as one text marks its parts inline: numbered items, then "Choices:" and "Recommended:".
CHOICES_MARK = re.compile(r"(?:^|(?<=\s))Choices:\s")
RECOMMEND_MARK = re.compile(r"(?:^|(?<=\s))Recommended:\s")
ITEM_MARKS = (re.compile(r"(?<![\w(])(\d+)\)\s"), re.compile(r"\((\d+)\)\s"), re.compile(r"(?<![\w.])(\d+)\.\s"),
              re.compile(r"\(([a-z])\)\s"))
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
BLANK_LINE = re.compile(r"\n\s*\n")
CHOICE_LABEL_PATTERN = r'''(?:'[^'\n]+'|"[^"\n]+"|[^'"\n:.!?;]{1,60}?)(?:\s+\(recommended\))?:\s+'''
CHOICE_LABEL = re.compile(CHOICE_LABEL_PATTERN, re.IGNORECASE)
INLINE_CHOICE = re.compile(r"(?:^|(?<=[.!?;])\s+)" + CHOICE_LABEL_PATTERN, re.IGNORECASE)


def split_at(text: str, mark: re.Pattern[str]) -> tuple[str, str]:
    found = mark.search(text)
    return (text, "") if found is None else (text[:found.start()].strip(), text[found.end():].strip())


def numbered_run(text: str) -> tuple[str, list[str], str]:
    """Split the text at its first run of two or more items marked 1) 2), 1. 2., (1) (2) or (a) (b).

    Return the text before the run, the items and the text after a blank line in the last item."""
    best: list[re.Match[str]] = []
    code_blocks = list(page_frame.CODE_BLOCK.finditer(text))
    for style in ITEM_MARKS:
        run: list[re.Match[str]] = []
        for mark in style.finditer(text):
            if any(block.start() <= mark.start() < block.end() for block in code_blocks):
                continue
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
    last = items[-1]
    blocks = list(page_frame.CODE_BLOCK.finditer(last))
    trailing = ""
    for blank in BLANK_LINE.finditer(last):
        if any(block.start() <= blank.start() < block.end()
               or block.start() == blank.end() and last[:blank.start()].rstrip().endswith(":") for block in blocks):
            continue
        items[-1], trailing = last[:blank.start()], last[blank.end():]
        break
    return text[:best[0].start()].strip(), [item.rstrip(";,").strip() for item in items], trailing


def item_html(entry: str) -> str:
    """Show a code block alone or after an item's text that ends with a colon."""
    whole = page_frame.CODE_BLOCK.fullmatch(entry.strip())
    if whole is not None:
        return page_frame.code_block_html(whole)
    for block in page_frame.CODE_BLOCK.finditer(entry):
        before, after = entry[:block.start()].rstrip(), entry[block.end():].strip()
        if before.endswith(":"):
            return item_html(before) + item_html(block[0]) + (item_html(after) if after else "")
    return "<br>".join(change_page.inline_html(line) for line in entry.splitlines())


def item_list(items: list[str]) -> str:
    entries: list[str] = []
    for entry in items:
        if entries and entries[-1].rstrip().endswith(":") and page_frame.CODE_BLOCK.fullmatch(entry.strip()) is not None:
            entries[-1] += "\n" + entry
        else:
            entries.append(entry)
    return '<h2>Items</h2><ol class="question-items">' + "".join(f"<li>{item_html(entry)}</li>" for entry in entries) + "</ol>"


def choice_list(choices: list[str]) -> str:
    """Show the choices as a list of radio items; a choice fills the answer box."""
    options = "".join(f'<label><input type="radio" name="choice" value="{html.escape(choice, quote=True)}">'
                      + f'<span>{change_page.inline_html(choice)}</span></label>' for choice in choices)
    return f'<fieldset class="question-choices"><legend>Choices</legend>{options}</fieldset>' if options else ""


def followup_sections(item: update_registration.OutstandingItem) -> str:
    """Show each follow-up the user sent and the asker's reply."""
    return "".join('<h2>Follow-up</h2>' + change_page.review_text_html(each["followup"], inline=True)
                   + '<h2>Reply</h2>' + change_page.review_text_html(each["reply"], inline=True)
                   for each in item.get("followups", []))


def question_text(item: update_registration.OutstandingItem) -> tuple[str, str]:
    """Separate the question sentence from its context without repeating the title."""
    text = item.get("reviewed", item.get("question", item["text"])).strip()
    if "reviewed" in item:
        title, _, context = text.partition("\n")
        if "?" in title:
            return title.strip(), context.strip()
        first = SENTENCE_END.split(title, maxsplit=1)
        return first[0], "\n\n".join(part for part in (*first[1:], context.strip()) if part)
    question = SENTENCE_END.split(text[:text.index("?") + 1])[-1] if "?" in text else ""
    if question:
        before, _, after = text.partition(question)
        return question.strip(), (before.strip() + "\n\n" + after.strip()).strip()
    title, _, context = text.partition("\n")
    first = SENTENCE_END.split(title, maxsplit=1)
    return first[0], "\n\n".join(part for part in (*first[1:], context.strip()) if part)


def question_paragraphs(text: str) -> str:
    """Keep paragraphs and list lines separate; split sentences in text stored on one line."""
    paragraphs = BLANK_LINE.split(text.strip()) if "\n" in text else SENTENCE_END.split(text.strip())
    return "".join(change_page.review_text_html(paragraph, inline=True) for paragraph in paragraphs if paragraph)


def reviewed_choices(text: str) -> tuple[str, list[str]]:
    """Recognize a final group of labeled choice paragraphs after the context."""
    paragraphs = BLANK_LINE.split(text)
    start = len(paragraphs)
    while start and CHOICE_LABEL.match(paragraphs[start - 1]) is not None:
        start -= 1
    choices = paragraphs[start:]
    if len(choices) < 2 or any("\n" in choice for choice in choices):
        return text, []
    return "\n\n".join(paragraphs[:start]), choices


def text_sections(text: str, *, reviewed: bool = False) -> tuple[str, str]:
    """Show context, items, choices and the recommendation as separate sections."""
    rest, recommend = split_at(text, RECOMMEND_MARK)
    lead, choice_text = split_at(rest, CHOICES_MARK)
    paragraphs: list[str] = []
    if reviewed and not choice_text:
        lead, paragraphs = reviewed_choices(lead)
    lead, items, trailing = numbered_run(lead)
    if items and "\n" not in text:
        # In one paragraph, the text after the last item's first sentence is not part of that item.
        last = SENTENCE_END.split(items[-1], maxsplit=1)
        items[-1], trailing = last[0], last[1] if len(last) > 1 else ""
    choice_lead, choices, choice_trailing = numbered_run(choice_text)
    choices = paragraphs or choices
    if choice_text and not choices:
        marks = list(INLINE_CHOICE.finditer(choice_text))
        if len(marks) >= 2 and marks[0].start() == 0:
            choices = [choice_text[mark.start():end].strip().rstrip(";")
                       for mark, end in zip(marks, [mark.start() for mark in marks[1:]] + [len(choice_text)])]
            choice_lead = ""
    body = question_paragraphs(lead) if lead else ""
    body += (item_list(items) if items else "") + (question_paragraphs(trailing) if trailing else "")
    # Unmarked choices stay text because their boundaries do not identify safe answer values.
    if choice_text and not choices:
        lines = [part.strip() for part in re.split(r";\s+|\n+|(?<=[.!?])\s+", choice_text) if part.strip()]
        body += '<h2>Choices</h2><ul>' + "".join(f'<li>{change_page.inline_html(line)}</li>' for line in lines) + '</ul>'
    elif choice_lead:
        body += '<h2>Choices</h2>' + question_paragraphs(choice_lead)
    if choice_trailing:
        body += question_paragraphs(choice_trailing)
    if recommend:
        body += '<h2>Recommended</h2>' + question_paragraphs(recommend)
    return body, choice_list(choices)


def question_parts(item: update_registration.OutstandingItem) -> tuple[str, str]:
    """Return the question body and its choice buttons; a question stored as one text shows in sections."""
    if "parts" not in item:
        _, context = question_text(item)
        body, choices = text_sections(context, reviewed="reviewed" in item)
        return body + followup_sections(item), choices
    parts = item["parts"]
    same = parts["ask"].strip().rstrip("?.").casefold() == item["text"].strip().rstrip("?.").casefold()
    body = "" if same else f'<p class="question-ask">{change_page.inline_html(parts["ask"])}</p>'
    if parts["context"].strip():
        body += '<h2>Context</h2>' + question_paragraphs(parts["context"])
    if parts["items"]:
        body += item_list(parts["items"])
    if parts["recommend"].strip():
        body += '<h2>Recommended</h2>' + change_page.review_text_html(parts["recommend"], inline=True)
    return body + followup_sections(item), choice_list(parts["choices"])


ANSWER_HELP = "A choice fills this. Edit it or write your own. Nothing sends until you press Answer."


def question_section(question: WaitingQuestion, browser: ReviewBrowser, feedback_rows: int) -> str:
    key = html.escape(question.key)
    body, choices = question_parts(question.record.item)
    title = question.record.item["text"] if "parts" in question.record.item else question_text(question.record.item)[0]
    owned = question.tab != "unrouted"
    # Without an owner there is no form, so the choices show as a list the user cannot pick.
    shown_choices = "" if owned else choices.replace('<input type="radio"', '<input type="radio" disabled')
    form = '<p>No showrunner owns this question.</p>' if not owned else f'''<form method="post" action="{change_page.PAGE_NAME}">
<input type="hidden" name="row" value="{key}">
<input type="hidden" name="token" value="{row_token(browser, question.key)}">{choices}
<label for="answer-{key}">Your answer</label>
<p class="answer-help" id="answer-help-{key}">{ANSWER_HELP}</p>
<textarea id="answer-{key}" name="feedback" rows="{feedback_rows}" aria-describedby="answer-help-{key}"></textarea>
<div class="question-actions"><button name="outcome" value="answer" disabled data-needs-text>Answer</button>
<button name="outcome" value="followup" disabled data-needs-text>Followup</button>
<button name="outcome" value="skip">Skip</button></div></form>'''
    return f'''<section hidden data-type="questions" data-key="{key}" data-tab="{html.escape(question.tab)}" data-pending="true">
<h1>{html.escape(title)}</h1>
{body}{shown_choices}
<p>Asked by {html.escape(question.asker)}</p><p role="status" data-save-state></p>{form}</section>'''


@dataclass(frozen=True)
class Rebuild:
    """One change that a machine's rebuild applies: its time, its owner and its reason."""

    at: str
    owner: str
    reason: str


def rebuild_list(answer: rebuild_row.RebuildAnswer, changes: dict[str, tuple[rebuild_row.Change, ...]],
                 record: staff.StaffSnapshot) -> str:
    """Show each machine the status line names, then its changes under each owner and each shown time."""
    def who(session: str) -> str:
        member = record.by_member_reference(session) if session else None
        return (f"{member.name} ({member.kind})" if member is not None
                else f"Unknown session: {session}" if session else "Unknown session")

    names = ["the Mac" if machine == "mac" else machine for machine in answer.machines]
    blocks: list[str] = []
    for machine_name, name in zip(answer.machines, names):
        records = next((machine.records for machine in answer.needs if machine.newest["machine"] == machine_name), ())
        filed = {item["nixos_commit"] for item in records}
        # A filed need and its commit are one change; the need gives the requester and the reason.
        items = [Rebuild(item["created_at"], who(item["requested_by"] or item["concerns"]), item["reason"])
                 for item in records]
        items += [Rebuild(change.at, who(change.session), change.reason)
                  for change in changes.get(machine_name, ()) if change.commit not in filed]
        listed = owner_groups(items) if items else '<p>The change list is not available yet.</p>'
        blocks.append(f'<article data-machine="{html.escape(machine_name)}"><h2>Rebuild {html.escape(name)}</h2>{listed}</article>')
    label = f"Rebuild {' and '.join(names)}" if names else ""
    return (f'<div data-rebuilds data-count="{len(names)}" data-label="{html.escape(label, quote=True)}">'
            + (''.join(blocks) if blocks else '<p>No rebuilds needed.</p>') + '</div>')


def owner_groups(items: list[Rebuild]) -> str:
    """Name each owner once, newest change first; under an owner, changes at one shown time share that time."""
    owners: dict[str, dict[str, list[Rebuild]]] = {}
    for item in sorted(items, key=lambda item: datetime.fromisoformat(item.at), reverse=True):
        owners.setdefault(item.owner, {}).setdefault(shown_time(datetime.fromisoformat(item.at)), []).append(item)
    return ''.join(
        f'<div data-owner><h3>{html.escape(owner)}</h3><dl>'
        + ''.join(f'<dt><time datetime="{html.escape(group[0].at, quote=True)}">{html.escape(shown)}</time></dt>'
                  + ''.join(f'<dd>{html.escape(item.reason)}</dd>' for item in group)
                  for shown, group in times.items())
        + '</dl></div>' for owner, times in owners.items())


# Machines sit side by side when the frame is wide; under each owner, a time is a column beside its reasons.
REBUILD_STYLE = """<style>main{max-width:1124px}
[data-rebuilds]{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:32px 48px;align-items:start}
[data-rebuilds] h2{margin:8px 0 0}[data-rebuilds] h3{font-size:17px;line-height:1.3;margin:24px 0 8px}
[data-rebuilds] dl{display:grid;grid-template-columns:max-content minmax(0,1fr);column-gap:16px;row-gap:4px;margin:0;font-size:15px}
[data-rebuilds] dt{grid-column:1;color:light-dark(#5f5f5f,#b0b0b0)}[data-rebuilds] dd{grid-column:2;margin:0;min-width:0;overflow-wrap:anywhere}
[data-rebuilds] dd+dt,[data-rebuilds] dd+dt+dd{padding-top:8px}
@media (max-width:480px){[data-rebuilds] dl{display:block}[data-rebuilds] dd+dt{padding-top:12px}[data-rebuilds] dd+dt+dd{padding-top:0}}</style>"""


def render_rebuilds() -> bytes:
    """Render the status line's rebuild answer for the console's rebuild link and Rebuilds page."""
    config = conf.path("statusline")
    rebuilds = rebuild_list(rebuild_row.read(config), rebuild_row.read_changes(config), staff.snapshot())
    return change_page.document(REBUILD_STYLE + '<h1 class="page-title">Rebuilds needed</h1>' + rebuilds, "Rebuilds needed")


def render(browser: ReviewBrowser, status: change_page.DeliveryStatus) -> bytes:
    settings = conf.read("review_links")
    feedback_rows = settings.whole("feedback_rows")
    refresh_seconds = settings.number("page_refresh_seconds")
    prepared = prepare(browser, status, settings=settings)
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
    content = (PAGE_STYLE + f'<div data-review-content data-kind="{kind}" data-refresh-seconds="{refresh_seconds}" data-script="{SCRIPT_VERSION}">'
               + f'<h1 class="page-title">{change_page.TITLES[kind]}</h1>'
               + '<div data-showrunner-filter><label for="review-showrunner">Showrunner:</label>'
               + '<select id="review-showrunner" aria-label="Showrunner"><option value="">All</option></select></div>'
               + '<template data-runner-options>' + ''.join(menus) + '</template>'
               + '<script type="application/json" data-change-orders>'
               + json.dumps(prepared.orders if kind == "changes" else {}).replace('<', '\\u003c') + '</script>'
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
[data-type="questions"] code{font:.9em ui-monospace,monospace;background:#8882;padding:1px 4px;border-radius:4px}
[data-type="questions"] pre{margin:8px 0}[data-type="questions"] pre code{font:inherit;background:none;padding:0}
.question-choices{display:grid;gap:8px;border:0;margin:16px 0;padding:0}
.question-choices legend{padding:0;margin-bottom:8px;font-weight:600}
.question-choices label{display:flex;gap:10px;align-items:flex-start;line-height:1.5;padding:10px 12px;border:1px solid #8886;border-radius:6px;cursor:pointer}
.question-choices label:has(:checked){border-color:Highlight;outline:1px solid Highlight}
.question-choices input{font:inherit;width:1em;height:1em;margin:.25em 0 0;flex:none}.question-choices label:has(:disabled){cursor:default}
.answer-help{margin:4px 0 8px;font-size:14px;opacity:.8}
[data-type="questions"] textarea{width:100%;box-sizing:border-box;font:inherit}
.question-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.question-actions button{margin:0}
.question-actions button:disabled{opacity:.45;cursor:default}
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
        outcome = fields["outcome"][0]
        if outcome not in ("answer", "followup", "skip"):
            raise change_page.AnswerRefused("use the answer form on this page")
        if outcome != "skip" and not fields["feedback"][0].strip():
            raise change_page.AnswerRefused("enter an answer")
        return submit_question(question, outcome, fields["feedback"][0], status)
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


def question_message(outcome: change_page.QuestionOutcome, production: str, text: str, unit: str, words: str) -> str:
    """The message the asker gets for an answer, a follow-up or a skip."""
    lead = {"answer": "From the user (via the review page): ",
            "followup": "From the user (via the review page), a follow-up on your question: ",
            "skip": "From the user (via the review page), skipped for now; it stays open at the end of the list: "}[outcome]
    message = lead + text + (f"\nFor: {unit}" if unit else "")
    if outcome == "answer":
        return message + "\n" + words
    if outcome == "followup":
        return (message + "\n" + words.strip() + "\nReply with this command; the question then returns to the user with your reply:\n"
                + f"~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py reply --production {shlex.quote(production)} "
                + f"--item {shlex.quote(text)} --text '<your reply>'")
    return message


def save_outcome(production: Production, question: WaitingQuestion, outcome: change_page.QuestionOutcome, words: str,
                 identities: update_registration.QuestionStaff) -> update_registration.AnswerRelay:
    """Answer, send a follow-up or move the question to the end of its queue; return whom the message goes to."""
    text = question.record.item["text"]
    if outcome == "answer":
        return update_registration.answer(production, text, identities=identities, only_open=True)
    if outcome == "followup":
        return update_registration.followup(production, text, words, identities=identities)
    urgency.move_to_end(update_registration.question_queue(production), update_registration.item_key(question.owner, text),
                        datetime.now(production.zone).replace(tzinfo=None))
    return update_registration.answer_relay(production, production.name, update_registration.question_asker(
        question.record.item, update_registration.OpenItemWithoutAsker(), identities), identities)


def submit_question(question: WaitingQuestion, outcome: change_page.QuestionOutcome, words: str,
                    status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery, str]:
    """Save through the production API before the existing sender delivers the message to the asker."""
    if question.tab == "unrouted":
        raise change_page.QuestionAnswerRefused("No showrunner owns this question.")
    identities = update_registration.QuestionStaff.read()
    production = read_production(question.owner, identities.record)
    item = question.record.item
    relay = update_registration.answer_relay(production, production.name,
        update_registration.question_asker(item, update_registration.OpenItemWithoutAsker(), identities), identities)
    recipient = relay.recipient if isinstance(relay.recipient, str) else question.owner
    message = question_message(outcome, production.name, item["text"], relay.unit, words)
    answer = words if outcome == "answer" else ""
    saved: change_page.StoredUserAnswer = {"saved": False, "source": str(question.record.path), "production": str(production.doc),
        "owner": question.owner, "question": item["text"], "answer": answer, "message": message, "kind": outcome}
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
            relay = save_outcome(production, question, outcome, words, identities)
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


SCRIPT = page_frame.REFRESH_SCRIPT + page_frame.MOTION_SCRIPT + r'''
const motion=createCardMotion();
let content=document.querySelector('[data-review-content]');
let rows,viewOrders,runnerOptions,showrunners,runnerMenu,next,empty,saveNotice;
let shownPage=content.outerHTML, heldPage=null, newestRequest=0, activeLoads=0, activeSaves=0;
const remembered=new Map(), kind=content.dataset.kind; let current=null, tab='', navigation=0, shownHash='';
function ordersFor(page,cards){
 const byKey=new Map(cards.map(row=>[row.dataset.key,row]));
 return new Map(Object.entries(JSON.parse(page.querySelector('[data-change-orders]').textContent))
  .map(([view,keys])=>[view,keys.map(key=>byKey.get(key)).filter(Boolean)]));
}
function bindPage(){
 rows=[...content.querySelectorAll('section[data-key]')];
 viewOrders=ordersFor(content,rows);
 runnerOptions=[...content.querySelector('[data-runner-options]').content.children];
 showrunners=content.querySelector('[data-showrunner-filter]');runnerMenu=showrunners.querySelector('select');
 next=content.querySelector('[data-next]');empty=content.querySelector('[data-empty]');
 saveNotice=content.querySelector('[data-save-notice]');
 runnerMenu.addEventListener('change',()=>{tab=runnerMenu.value;go(remembered.get(tab));});
 next.addEventListener('click',()=>advance(current));
 rows.forEach(row=>{
  const form=row.querySelector('form');
  if(form) form.addEventListener('submit',event=>{event.preventDefault();save(row,form,event.submitter.value);});
  // A choice fills the answer box; the user can edit it, then presses Answer.
  const box=form?.querySelector('textarea');
  form?.querySelectorAll('input[name=choice]').forEach(choice=>choice.addEventListener('change',()=>{
   box.value=choice.value;box.dispatchEvent(new Event('input',{bubbles:true}));box.focus();
  }));
  // Answer and Followup send the box text, so they wait for text.
  const needsText=()=>form.querySelectorAll('[data-needs-text]').forEach(button=>button.disabled=!box.value.trim());
  if(box && form.querySelector('[data-needs-text]')){box.addEventListener('input',needsText);needsText();}
 });
}
function viewRows(){return viewOrders.get(tab) || rows.filter(r=>tab==='' || r.dataset.tab===tab);}
function waiting(){return viewRows().filter(r=>r.dataset.pending==='true');}
function show(){
 const previous=current;
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
 const target=current, cards=rows;
 const navigationTarget=target?.querySelector('form:not([hidden]) .answer-actions') || content.querySelector('.navigation');
 const reveal=()=>{cards.forEach(row=>row.hidden=row!==target);navigationTarget.append(next);};
 if(previous && previous.dataset.key!==target?.dataset.key && previous.isConnected)
  void motion.switchCard(previous,target,reveal);
 else if(previous!==target){motion.finish();reveal();}
 else if(!previous) reveal();
 // A question has its own Skip, which the server keeps.
 next.hidden=kind==='questions' || !waiting().some(r=>r!==current);
 if(current) remembered.set(tab,current.dataset.key);
 // The default place, All with no card, keeps a plain URL.
 history.replaceState(null,'',tab || current ? '#'+new URLSearchParams({showrunner:tab,row:current?.dataset.key || ''}) : location.pathname+location.search);
 shownHash=location.hash;
 // The console records this place in its own URL, so a reload or a shared link opens it again.
 if(parent!==window) parent.postMessage('console-place',location.origin); }
function clearNotice(){saveNotice.hidden=true;saveNotice.textContent='';}
function go(key){navigation++;clearNotice();location.hash=new URLSearchParams({showrunner:tab,row:key || ''});show();}
function advance(row){
 const ordered=viewRows(), at=ordered.indexOf(row), candidates=waiting();
 const following=candidates.find(r=>ordered.indexOf(r)>at) || candidates[0];
 go(following?.dataset.key || ''); }
async function save(row,form,outcome){
 if(form.dataset.saving==='true') return;
 activeSaves++;let saved=false;
 clearNotice();form.dataset.saving='true'; const state=row.querySelector('[data-save-state]'), position=navigation;
 const data=new URLSearchParams(new FormData(form));
 data.set('row',row.dataset.key);data.set('outcome',outcome);data.delete('choice');
 form.querySelectorAll('button[name=outcome]').forEach(b=>b.setAttribute('aria-pressed',String(b.value===outcome)));
 state.textContent='Saving'; row.dataset.saveState='saving';
 const moveOn=notice=>{
  row.dataset.pending='false';
  if(current===row && (tab==='' || tab===row.dataset.tab) && navigation===position){advance(row);saveNotice.textContent=notice;saveNotice.hidden=false;}
  else show();
  motion.settled().then(()=>{if(row.dataset.pending==='false') form.hidden=true;});
 };
 // Works and Does not work show the next change at once; a failed save puts the change back.
 const early=kind!=='questions' && (outcome==='works' || outcome==='not works');
 if(early) moveOn('Saving review.');
 try{
  const response=await fetch(form.action,{method:'POST',body:data,headers:{Accept:'application/json'}});
  const reply=await response.json();
  if(!response.ok) throw new Error(reply.reason || 'The answer could not be saved. Try again.');
  state.textContent=reply.answer ? 'Saved: '+reply.answer : outcome==='question' ? 'The showrunner will answer this' : outcome==='feedback' ? 'The showrunner will act on this' :
   outcome==='followup' ? 'Follow-up sent to the asker' : outcome==='skip' ? 'Moved to the end of the list' : 'The showrunner will validate this';
  row.dataset.saveState='saved';
  saved=true;
  if(!early) moveOn(kind!=='questions' ? 'Review saved.' : outcome==='followup' ? 'Follow-up sent.' : outcome==='skip' ? 'Skipped.' : 'Answer saved.');
  else if(!saveNotice.hidden && saveNotice.textContent==='Saving review.') saveNotice.textContent='Review saved.';
 }catch(error){
  const reason=error instanceof SyntaxError ? 'The answer could not be saved. Try again.' : error.message;
  row.dataset.saveState='failed'; state.textContent='Not saved: '+reason;
  if(early){
   row.dataset.pending='true';form.hidden=false;show();
   const open=document.createElement('button');open.type='button';open.textContent='Open it';
   open.addEventListener('click',()=>go(row.dataset.key));
   saveNotice.replaceChildren('Not saved: '+row.dataset.shown+'. '+reason+' It is back in the list. ',open);
   saveNotice.hidden=false;
  }
 }finally{
  form.dataset.saving='false';activeSaves--;
  // Read after the save, so an older response cannot restore the saved item.
  if(saved){await motion.settled();heldPage=null;newestRequest++;pageRefresh.refresh(true);}
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
 // A page from an older server script cannot bind the new rows, so it loads again.
 if(fresh.dataset.script!==content.dataset.script){location.reload();return;}
 const opened=new Set([...content.querySelectorAll('details[open]')].map(detailKey));
 const left=scrollX,top=scrollY,focused=document.activeElement;
 const controlId=focused===runnerMenu ? focused.id : '';
 const fieldRow=focused?.closest('[data-key]')?.dataset.key;
 const fieldName=focused?.matches('textarea,input[type=text]') ? focused.name : '';
 const selection=[focused?.selectionStart,focused?.selectionEnd,focused?.selectionDirection];
 const ordered=viewRows(), previous=ordered.indexOf(current),following=ordered.slice(previous+1).map(row=>row.dataset.key);
 const freshRows=[...fresh.querySelectorAll('section[data-key]')];
 const candidates=(ordersFor(fresh,freshRows).get(tab) || freshRows.filter(row=>tab==='' || row.dataset.tab===tab))
  .filter(row=>row.dataset.pending==='true');
 const keep=candidates.find(row=>row.dataset.key===current?.dataset.key);
 const chosen=keep || following.map(key=>candidates.find(row=>row.dataset.key===key)).find(Boolean) || candidates[0];
 const notice=saveNotice.hidden ? '' : saveNotice.textContent;
 motion.finish();
 shownPage=fresh.outerHTML;content.replaceWith(fresh);content=fresh;bindPage();
 content.querySelectorAll('details').forEach(detail=>detail.open=opened.has(detailKey(detail)));
 history.replaceState(null,'','#'+new URLSearchParams({showrunner:tab,row:chosen?.dataset.key || ''}));
 show();
 if(notice){saveNotice.textContent=notice;saveNotice.hidden=false;}
 if(controlId) content.querySelector('#'+controlId)?.focus({preventScroll:true});
 const field=rows.find(row=>row.dataset.key===fieldRow)?.querySelector('[name="'+fieldName+'"]');
 if(field && !field.closest('section').hidden){field.focus({preventScroll:true});field.setSelectionRange(...selection);}
 window.scrollTo({left,top,behavior:'instant'});pageRefresh.restart();
}
async function refreshPage(supersede=false){
 if(activeLoads && !supersede) return;
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
const pageRefresh=createPageRefresh(refreshPage,()=>content.dataset.refreshSeconds);
document.addEventListener('input',()=>applyHeldPage());
window.addEventListener('hashchange',()=>{if(location.hash!==shownHash){navigation++;clearNotice();}show();});
bindPage();show();
'''
SCRIPT_VERSION = change_page.script_version(SCRIPT + page_frame.ZOOM_SCRIPT + page_frame.ZOOM_STYLE
                                          + page_frame.COPY_SCRIPT + page_frame.COPY_STYLE + page_frame.COPY_BUTTON)
