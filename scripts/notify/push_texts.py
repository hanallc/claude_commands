"""Fixed phone texts, their examples, and the argv used to deliver them."""
from __future__ import annotations

import hashlib
import importlib
import json
import string
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Literal, NotRequired, TypedDict, cast

from scripts.lib.conf import UseConfiguredValue
from scripts.lib.display_text import display_field
from scripts.message import session_send
from scripts.message.send_result import SEND_RETRY_EXIT as SEND_RETRY_EXIT

from scripts.notify.user_action import (ActionRequired, NoActionRequired, UserAction, NotificationSession,
                         NoSessionLine, NO_SESSION_LINE, SessionLine, first_line)

Need = Literal["note", "decision", "blocked"]
MESSAGE_MAX = 1024
SOURCE_MAX = 80
FREE_TEXT = "none (free text)"
REVIEWED_FILE = Path(__file__).with_name("push_texts_reviewed.json")
TEMPLATE_REQUIRED = "phone pushes require --text-id: decision.choices for choices, alert.blocked for emergencies, or nate.adhoc for notes"


@dataclass(frozen=True)
class ActionTemplate:
    text: str
    omit_when_empty: bool = False


@dataclass(frozen=True)
class NoActionNeeded:
    pass


@dataclass(frozen=True)
class SessionTemplate:
    text: str


@dataclass(frozen=True)
class PushExample:
    name: str
    values: dict[str, str]


@dataclass(frozen=True)
class TemplateValues:
    displayed: dict[str, str]
    supplied: dict[str, str]


def display_values(given: dict[str, str]) -> TemplateValues:
    return TemplateValues(dict(given), dict(given))


@dataclass(frozen=True)
class PushText:
    id: str
    sender: str
    need: Need
    title: str
    action: ActionTemplate | NoActionNeeded
    body: str
    examples: tuple[PushExample, ...]
    # One line for /nate help: when the push goes out. Not part of the approval fingerprint.
    purpose: str
    session: SessionTemplate | NoSessionLine = NO_SESSION_LINE
    # False: the phone shows the title and body exactly as rendered, with no
    # "No action needed." line. Only an entry with no action and no session can opt out.
    header: bool = True
    # An agent writes the text at send time; no person approved it, so check it like free text.
    agent_written: bool = False
    # True: the phone shows the app name as the title and the rendered title as the whole message.
    # Only an entry with no action, no session and no body can use it.
    one_line: bool = False
    expand_values: Callable[[dict[str, str]], TemplateValues] = display_values


@dataclass(frozen=True)
class KnownActionEnd:
    index: int


@dataclass(frozen=True)
class NoHeader:
    pass


@dataclass(frozen=True)
class UnknownActionEnd:
    pass


ActionEnd = KnownActionEnd | NoHeader | UnknownActionEnd


def read_action_end(value: object, message: str) -> ActionEnd:
    """Read the header state from a delivery or log record."""
    if type(value) is int:
        if value == 0:
            return NoHeader()
        if 0 < value < len(message) and message[value] == "\n":
            return KnownActionEnd(value)
    return UnknownActionEnd()


def sender_name(source: str) -> str:
    """Use the session name when send.py supplies a session source."""
    return " ".join(source.removeprefix("send.py in ").split())[:SOURCE_MAX]


def phone_footer(sender: str, text_id: str) -> str:
    return f"\n\nSent by: {sender} · Template: {text_id or FREE_TEXT}"


def phone_message_limit(source: str, text_id: str) -> int:
    """Return the message space before the phone footer."""
    return MESSAGE_MAX - len(phone_footer(sender_name(source), text_id))


@dataclass(frozen=True)
class PhoneBudget:
    characters: int


@dataclass(frozen=True)
class FullPhoneMessage:
    pass


FULL_PHONE_MESSAGE = FullPhoneMessage()


def format_phone_body(message: str, action_end: ActionEnd, *, length: PhoneBudget | FullPhoneMessage) -> str:
    """Use one empty line before the body when the action end is known."""
    if isinstance(length, PhoneBudget):
        message = message[:MESSAGE_MAX]
    if not isinstance(action_end, KnownActionEnd):
        return (message[:length.characters] if isinstance(length, PhoneBudget) else message).rstrip()
    index = action_end.index
    header = message[:index]
    body = message[index + 1:].lstrip("\r\n").rstrip()
    separator = "\n\n" if body else ""
    if isinstance(length, PhoneBudget):
        prefix_length = index + len(separator)
        if prefix_length > length.characters:
            raise ValueError("Session and action exceed the message space before the footer")
        body = body[:length.characters - prefix_length].rstrip()
    return header + separator + body if body else header


@dataclass(frozen=True)
class RenderedPush:
    id: str
    sender: str
    need: Need
    title: str
    action: UserAction
    body: str
    values: dict[str, str]
    session: NotificationSession = NO_SESSION_LINE
    header: bool = True
    one_line: bool = False

    @property
    def posted_title(self) -> str:
        """The title Pushover gets; empty for a one-line text, so the phone shows the app name."""
        return "" if self.one_line else self.title

    @property
    def posted_body(self) -> str:
        """The body a Draft carries: the line alone for a one-line text."""
        return self.title if self.one_line else self.body

    @property
    def message(self) -> str:
        if self.one_line:
            return self.title
        if not self.header:
            return self.body
        header = first_line(self.action, self.session)
        return f"{header}\n{self.body}" if self.body else header

    @property
    def action_end(self) -> KnownActionEnd | NoHeader:
        if self.header and not self.one_line:
            return KnownActionEnd(len(first_line(self.action, self.session)))
        return NoHeader()

    @property
    def phone_message(self) -> str:
        """The phone message before the sender and template footer."""
        budget = PhoneBudget(phone_message_limit(self.sender, self.id))
        return format_phone_body(self.message, self.action_end, length=budget)

    @property
    def phone_message_length(self) -> int:
        """The phone message length before any cut or footer."""
        return len(format_phone_body(self.message, self.action_end, length=FULL_PHONE_MESSAGE))


def catalog() -> tuple[PushText, ...]:
    module = importlib.import_module('scripts.notify.push_catalog')
    entries = cast(tuple[PushText, ...], module.ENTRIES)
    if len({entry.id for entry in entries}) != len(entries):
        raise ValueError("duplicate push text id")
    for entry in entries:
        if not entry.examples or len({example.name for example in entry.examples}) != len(entry.examples):
            raise ValueError(f"{entry.id}: examples need distinct names and must not be empty")
        if not entry.header and (isinstance(entry.action, ActionTemplate) or isinstance(entry.session, SessionTemplate)):
            raise ValueError(f"{entry.id}: an entry without a header cannot state an action or a session")
        if (isinstance(entry.action, ActionTemplate) and entry.action.omit_when_empty
                and isinstance(entry.session, SessionTemplate)):
            raise ValueError(f"{entry.id}: an entry that omits an empty action cannot state a session")
        if entry.one_line and (isinstance(entry.action, ActionTemplate) or isinstance(entry.session, SessionTemplate)
                               or entry.body):
            raise ValueError(f"{entry.id}: a one-line entry cannot state an action, a session or a body")
    return entries




@dataclass(frozen=True)
class DecisionChoice:
    label: str
    effect: str


def decision_values(given: dict[str, str]) -> TemplateValues:
    """Build decision rows from structured choices."""
    for key, value in given.items():
        if key != "choices" and value.splitlines() != [value]:
            raise ValueError("decision.choices: values must have one line")
    try:
        raw = cast(object, json.loads(given.get("choices", "")))
    except ValueError as error:
        raise ValueError("decision.choices: choices must be a JSON list of label/effect objects") from error
    if not isinstance(raw, list):
        raise ValueError("decision.choices: choices must be a JSON list")
    choices: list[DecisionChoice] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            raise ValueError("decision.choices: each choice needs a label and effect")
        fields = cast(dict[str, object], item)
        label, effect = fields.get("label"), fields.get("effect")
        if (set(fields) != {"label", "effect"} or not isinstance(label, str)
                or not isinstance(effect, str) or not label.strip() or not effect.strip()
                or label.splitlines() != [label] or effect.splitlines() != [effect]):
            raise ValueError("decision.choices: each choice needs a nonempty label and effect")
        choices.append(DecisionChoice(label.strip(), effect.strip()))
    if len(choices) < 2 or len({choice.label.casefold() for choice in choices}) != len(choices):
        raise ValueError("decision.choices: supply at least two distinct choices")
    if given.get("recommended_choice") not in {choice.label for choice in choices}:
        raise ValueError("decision.choices: recommended_choice must name a listed choice")
    if "choice_labels" in given:
        raise ValueError("decision.choices: choice_labels is generated from choices")
    displayed = dict(given) | {"choice_labels": " or ".join(choice.label.lower() for choice in choices),
                    "choices": "\n".join(f"{choice.label}: {choice.effect}" for choice in choices)}
    return TemplateValues(displayed, dict(given))


def render_entry(entry: PushText, given: dict[str, str]) -> RenderedPush:
    templates = [entry.sender, entry.title, entry.body]
    if isinstance(entry.action, ActionTemplate):
        templates.append(entry.action.text)
    if isinstance(entry.session, SessionTemplate):
        templates.append(entry.session.text)
    fields: set[str] = set()
    for template in templates:
        for _, field, _, _ in string.Formatter().parse(template):
            if field is not None:
                if not field.isidentifier():
                    raise ValueError(f"{entry.id}: placeholder must be a name: {field}")
                fields.add(field)
    expanded = entry.expand_values(given)
    values = expanded.displayed
    if fields != set(values):
        raise ValueError(f"{entry.id}: missing values {sorted(fields - set(values))}; extra values {sorted(set(values) - fields)}")
    action: UserAction = NoActionRequired()
    header = entry.header
    if isinstance(entry.action, ActionTemplate):
        action_text = entry.action.text.format(**values)
        if action_text or not entry.action.omit_when_empty:
            action = ActionRequired(action_text)
        else:
            header = False
    return RenderedPush(entry.id, entry.sender.format(**values), entry.need,
                        entry.title.format(**values), action, entry.body.format(**values), expanded.supplied,
                        SessionLine(entry.session.text.format(**values)) if isinstance(entry.session, SessionTemplate) else NO_SESSION_LINE,
                        header, entry.one_line)


def render(id: str, **values: str) -> RenderedPush:
    for entry in catalog():
        if entry.id == id:
            return render_entry(entry, values)
    raise ValueError(f"unknown push text id: {id}")


def listing(entries: tuple[PushText, ...]) -> str:
    """One line per entry, sorted by id: the id in one column, then its purpose."""
    ordered = sorted(entries, key=lambda entry: entry.id)
    width = max(len(entry.id) for entry in ordered)
    return "\n".join(f"{entry.id:<{width}}  {entry.purpose}" for entry in ordered)


def shown(entry: PushText) -> str:
    """The entry's template with its placeholders, then its first example as the phone shows it."""
    template = RenderedPush(entry.id, entry.sender, entry.need, entry.title,
                            ActionRequired(entry.action.text) if isinstance(entry.action, ActionTemplate) else NoActionRequired(),
                            entry.body, {},
                            SessionLine(entry.session.text) if isinstance(entry.session, SessionTemplate) else NO_SESSION_LINE,
                            entry.header, entry.one_line)
    example = entry.examples[0]
    filled = render_entry(entry, example.values)
    return (f"{entry.id}  {entry.purpose}\n\n"
            + f"Template:\n{shown_title(template)}\n{template.phone_message}\n\n"
            + f"Example ({example.name}):\n{shown_title(filled)}\n{filled.phone_message}")


def shown_title(rendered: RenderedPush) -> str:
    return f"Title: {rendered.posted_title}" if rendered.posted_title else "Title: none (the phone shows the app name)"


def user_send_argv(rendered: RenderedPush, *, sender: str = "", machine: str = "",
                   priority: str = "", due: str = "", question: str = "", timeout: float = 0, key: str = "", self_clearing: bool = False,
                   send_script: Path | UseConfiguredValue = UseConfiguredValue.CURRENT) -> list[str]:
    """The send.py argv; a `key` lets `send.py retire --to user --key KEY` stop a blocked push's repeats.
    `self_clearing`: the caller retires its own blocked push, so send.py needs no --clears-when."""
    command = session_send.send_argv("--to", "user", "--from", sender or rendered.sender,
                                     "--need", rendered.need, "--text-id", rendered.id, send_script=send_script)
    if key:
        command.extend(["--key", key])
    if self_clearing:
        command.append("--self-clearing")
    for name, value in sorted(rendered.values.items()):
        command.extend(["--value", f"{name}={value}"])
    if machine:
        command.extend(["--machine", machine])
    if question:
        command.extend(["--question", question])
    if due:
        command.extend(["--due", due])
    if priority:
        command.extend(["--priority", priority])
    if timeout:
        command.extend(["--timeout", str(timeout)])
    return command


def send_user_push(rendered: RenderedPush, *, sender: str, key: str, clears_when: str, send_timeout_s: float,
                   options: tuple[str, ...] = ()) -> bool:
    """Send one outage phone push to the user, with its clear check. Print the reason when send.py refuses it."""
    from scripts.lib.process_failure import failure_detail

    command = user_send_argv(rendered, sender=sender, key=key)
    command.extend(["--clears-when", clears_when, *options, "--recipient-key", "user"])
    try:
        sent = subprocess.run(command, capture_output=True, text=True, timeout=send_timeout_s, check=False)
        if sent.returncode == 0:
            return True
        reason = failure_detail(sent)
    except (OSError, subprocess.SubprocessError) as error:
        reason = str(error)
    print(f"{sender}: phone push {key} failed: {display_field(reason)}", file=sys.stderr)
    return False


def user_retire_argv(sender: str, key: str) -> list[str]:
    """The send.py argv that stops the repeats of the emergency push `user_send_argv` sent with `key`."""
    return session_send.send_argv("retire", "--to", "user", "--from", sender, "--key", key)


class FilledExampleRecord(TypedDict):
    session: NotRequired[str]
    name: str
    values: dict[str, str]
    sender: str
    need: Need
    title: str
    action: str | None
    body: str


class FingerprintRecord(TypedDict):
    session: NotRequired[str]
    header: NotRequired[bool]
    one_line: NotRequired[bool]
    sender: str
    need: Need
    title: str
    action: dict[str, str | bool]
    body: str
    examples: list[FilledExampleRecord]


def agent_written(text_id: str) -> bool:
    return any(entry.id == text_id and entry.agent_written for entry in catalog())


def user_specified(text_id: str) -> bool:
    """Whether the user specified this text: its values are written per send, so a failed review is
    about one send's words, not the template, and no template fix is due."""
    try:
        approvals = cast(object, json.loads(REVIEWED_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False
    entry = cast(dict[str, object], approvals).get(text_id) if isinstance(approvals, dict) else None
    if not isinstance(entry, dict) or not entry:
        return False
    return all(isinstance(approval, dict) and str(cast(dict[str, object], approval).get("reviewer", "")).startswith("user-spec:")
               for approval in cast(dict[str, object], entry).values())


def approval_date(text_id: str) -> str:
    """The date (YYYY-MM-DD) of the newest approval when every example of the entry has a current
    approval in REVIEWED_FILE; "" when one is missing or its template changed since."""
    entry = next((item for item in catalog() if item.id == text_id), None)
    try:
        approvals = cast(object, json.loads(REVIEWED_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ""
    examples = cast(dict[str, object], approvals).get(text_id) if isinstance(approvals, dict) else None
    if entry is None or not isinstance(examples, dict):
        return ""
    digest, times = fingerprint(entry), list[str]()
    for example in entry.examples:
        approval = cast(dict[str, object], examples).get(example.name)
        if not isinstance(approval, dict):
            return ""
        record = cast(dict[str, object], approval)
        time = record.get("time")
        if record.get("sha256") != digest or not isinstance(time, str):
            return ""
        times.append(time)
    return max(times)[:10]


def fingerprint(entry: PushText) -> str:
    examples: list[FilledExampleRecord] = []
    for example in entry.examples:
        rendered = render_entry(entry, example.values)
        examples.append({"name": example.name, "values": example.values, "sender": rendered.sender,
                         "need": rendered.need, "title": rendered.title,
                         "action": rendered.action.text if isinstance(rendered.action, ActionRequired) else None,
                         "body": rendered.body})
        if isinstance(rendered.session, SessionLine):
            examples[-1]["session"] = rendered.session.text
    value: FingerprintRecord = {"sender": entry.sender, "need": entry.need, "title": entry.title,
             "action": {"required": entry.action.text} if isinstance(entry.action, ActionTemplate) else {"none": True},
             "body": entry.body, "examples": examples}
    if isinstance(entry.session, SessionTemplate):
        value["session"] = entry.session.text
    if isinstance(entry.action, ActionTemplate) and entry.action.omit_when_empty:
        value["action"]["omit_when_empty"] = True
    if not entry.header:
        value["header"] = False
    if entry.one_line:
        value["one_line"] = True
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
