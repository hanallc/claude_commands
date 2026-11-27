#!/usr/bin/env python3
"""Send the user one Pushover alert. Mac pushes and cancellations run on natedev.

  pushover.py --text-id ID --value KEY=VALUE ... [--due ISO8601] [--source NAME] [--key KEY] [--through-silence] [--user-asked]
  pushover.py cancel [--source NAME] KEY     stop the emergency push sent with --key KEY

Priority 0 is a normal notification, 1 high (sounds in Pushover's quiet hours),
2 emergency: it repeats every RETRY_S seconds until the user taps Acknowledge,
for at most EXPIRE_S. Deleting it on the phone does not stop it.
A decision uses priority 1 only before an explicit deadline within the configured decision window.
Other decisions use priority 0. Release checks the deadline again.
An emergency push with --key carries the Pushover tag push_tag(KEY).
`cancel` stops every emergency push with that tag.
It prints "pushover: cancelled N" or "pushover: nothing to cancel" and exits 0;
it exits 1 when Pushover refused it or could not be reached, 2 on usage or keys missing.

The keys are in KEYS_FILE (PUSHOVER_USER, PUSHOVER_TOKEN; 0600, filled in by the
user). Nothing here prints them. (user, 2026-10-05) Each LOG_FILE JSON line
holds the local time, source, priority, posted title and message, and outcome.

Every posted push ends with one line after review: "Sent by: <sender> · Template: <text id>",
or "Sent by: <sender> · Template: none (free text)" for a legacy kept push. The
sender is the source; "send.py in hana" shows as "hana". A long body is cut so that last line
always fits. A push with no sender is refused and nothing is posted.

While the user's silence is on (silence.py), a push below priority 2 is kept unless it uses --through-silence.
A kept push prints "pushover: kept: silence until ..." and exits 0.
`silence.py off` posts it later. Replacement matches the source, key and Question reference.
--through-silence preserves review and priority. A normal push does not repeat or need acknowledgement.
--user-asked sends a requested nate.adhoc push as written. Silence rules still apply.

New pushes require a registered template before review, credentials, claims, or posting. Exit 0
sent or kept, 1 Pushover refused it or could not be reached, 2 refused before sending,
usage, or keys missing; 4 an existing claim is busy, 5 replaced, 6 not sent after failed review. Registry and emergency pushes still send.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import hashlib
import http.client
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, NotRequired, TypedDict, cast

from scripts.message import session_send
from scripts.notify import review
from scripts.notify import push_texts
from scripts.notify import push_reference
from scripts.notify import question_pushes
from scripts.notify import silence
from scripts.notify import decision_priority
from scripts.notify.push_catalog import (NATE_ADHOC_TEXT_ID, PAID_CREDIT_ACTION_PREFIX, RESTART_FAILED_TEXT_ID,
                                         remaining_credit_accounts)
from scripts.notify.user_action import (FIX, REFUSED_BEFORE_SENDING, ActionAllowed, ActionRefused, ActionRequired,
                         ActionUnstated, UserAction, first_line, parse_user_action, refused_at,
                         NotificationSession, NO_SESSION_LINE, split_session, session_prefix)
from scripts.whoami import token_expiry

API_URL = "https://api.pushover.net/1/messages.json"
CANCEL_URL = "https://api.pushover.net/1/receipts/cancel_by_tag/{tag}.json"
KEYS_FILE = Path.home() / ".config" / "pushover" / "env"
LOG_FILE = Path.home() / ".local" / "state" / "notify" / "pushover.jsonl"
PRIORITIES = ("0", "1", "2")
TOKEN_ALERT_IDS = token_expiry.ALERT_IDS | {token_expiry.LEGACY_MAC_EXPIRY_ID}
TOKEN_APPROVAL_REQUIRED = "Token alert template needs current approval"
PAID_CREDIT_TEXT_IDS = frozenset({"quota.paid_credits", "quota.paid_credits_queued"})
PAID_CREDIT_ACCOUNT = re.compile(r"codex[1-9][0-9]*")
PAID_CREDIT_DETAIL_PREFIX = "paid-credit account="
RETRY_S = 300
EXPIRE_S = 10800
TITLE_MAX = 250
MESSAGE_MAX = 1024
ACTION_TEXT_MAX = MESSAGE_MAX - len("Action: ") - 1
TIMEOUT_S = 15
SOURCE_MAX = 80
# send.py's default_sender: the Claude session that ran send.py follows this prefix.
SESSION_SOURCE = "send.py in "
FREE_TEXT = "none (free text)"
USAGE = "usage: pushover.py --text-id ID --value KEY=VALUE ... [--due ISO8601] [--source NAME] [--key KEY] [--through-silence] [--user-asked]"
CANCEL_USAGE = "usage: pushover.py cancel [--source NAME] KEY"
REMOTE = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/pushover.py"'


class Keys(TypedDict):
    user: str
    token: str


class Reply(TypedDict, total=False):
    status: int
    request: str
    receipt: str
    canceled: int
    errors: list[str]


class ReviewedPushRecord(review.MessageRecord):
    reviewed: NotRequired[bool]
    held: NotRequired[bool]
    text_id: NotRequired[str]
    failed_rules: NotRequired[list[str]]
    findings: NotRequired[list[review.RuleFailureRecord]]
    sent_despite_failed_review: NotRequired[bool]
    held_source: NotRequired[str]
    held_identity: NotRequired[str]


class PushLogRecord(TypedDict):
    key: NotRequired[str]
    template: NotRequired[str]
    reference: NotRequired[push_reference.PushReference]
    time: str
    source: str
    priority: str
    title: str
    message: str
    outcome: str


Post = Callable[[str, bytes], tuple[int, bytes]]
AskNatedev = Callable[[Sequence[str], str], subprocess.CompletedProcess[str]]


def read_keys(path: Path) -> Keys | None:
    """Both keys from a KEY=VALUE file, or None when either is missing or empty."""
    if not path.is_file():
        return None
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and not name.lstrip().startswith("#"):
            values[name.strip()] = value.strip().strip("\"'")
    user, token = values.get("PUSHOVER_USER", ""), values.get("PUSHOVER_TOKEN", "")
    return {"user": user, "token": token} if user and token else None


def push_tag(key: str) -> str:
    """The Pushover tag for an emergency push with this key: the same key gives the same tag."""
    return "key-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def fields(keys: Keys, title: str, message: str, priority: str, key: str = "") -> dict[str, str]:
    body = {
        "token": keys["token"],
        "user": keys["user"],
        "message": message[:MESSAGE_MAX],
        "priority": priority,
    }
    if title:
        # With no title, Pushover shows the app name.
        body["title"] = title[:TITLE_MAX]
    if priority == "2":
        body |= {"retry": str(RETRY_S), "expire": str(EXPIRE_S)}
        if key:
            body["tags"] = push_tag(key)
    return body


def http_post(url: str, data: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # pyright: ignore[reportAny]
            return cast(int, response.status), cast(bytes, response.read())  # pyright: ignore[reportAny]
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.read()
        except (OSError, http.client.HTTPException):
            if 400 <= error.code < 500:
                return error.code, b""
            raise


def parent_source() -> str:
    """The parent command line, bounded for the audit log."""
    try:
        found = subprocess.run(
            ["ps", "-o", "args=", "-p", str(os.getppid())],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        found = ""
    return (found or f"parent pid {os.getppid()}")[:SOURCE_MAX]


def sender_name(source: str) -> str:
    """The "Sent by" name: the session for a send.py source, else the source, on one line."""
    return " ".join(source.removeprefix(SESSION_SOURCE).split())[:SOURCE_MAX]


def _phone_footer(sender: str, text_id: str) -> str:
    return f"\n\nSent by: {sender} · Template: {text_id or FREE_TEXT}"


def phone_message_limit(source: str, text_id: str) -> int:
    """Return the message space before the phone footer."""
    return MESSAGE_MAX - len(_phone_footer(sender_name(source), text_id))


def log(source: str, priority: str, title: str, message: str, outcome: str, *,
        key: str = "", template: str = "", reference: push_reference.PushReference = push_reference.NO_REFERENCE) -> None:
    entry: PushLogRecord = {"time": datetime.now().astimezone().replace(microsecond=0).isoformat(),
             "source": source, "priority": priority, "title": title, "message": message, "outcome": outcome}
    if key or template or reference["kind"] != "none":
        entry["key"] = key
        entry["template"] = template
        entry["reference"] = reference
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as out:
            _ = out.write(json.dumps(entry) + "\n")
    except OSError as error:
        print(f"pushover: could not write log: {error}; record: {json.dumps(entry)}", file=sys.stderr)


class Arguments(TypedDict):
    silence_policy: silence.SilencePolicy
    user_asked: bool
    reference: push_reference.PushReference
    need: str
    priority: str
    source: str | None
    key: str
    action: str | None
    no_action: bool
    answerable: bool
    title: str
    message: str
    text_id: str
    paid_credit_account: str
    session: NotificationSession
    header: bool
    due: str


def parse(argv: list[str]) -> Arguments | None:
    """Parse options without exiting, so callers get send()'s usage status."""
    priority = "0"
    question = forwarded_reference = ""
    remote_forward = False
    due = ""
    silence_policy = silence.SilencePolicy.KEEP_DURING_SILENCE
    user_asked = False
    source: str | None = None
    action: str | None = None
    no_action = False
    answerable = False
    text_id = ""
    push_key = ""
    values: dict[str, str] = {}
    index = 0
    while index < len(argv) and argv[index].startswith("--"):
        option = argv[index]
        if option == "--through-silence":
            silence_policy = silence.SilencePolicy.SEND_THROUGH_SILENCE
            index += 1
            continue
        if option == "--user-asked":
            user_asked = True
            index += 1
            continue
        if option == "--ssh-forwarded-reference":
            if "SSH_CONNECTION" not in os.environ:
                return None
            remote_forward = True
            index += 1
            continue
        if option == "--answerable":
            answerable = True
            index += 1
            continue
        if option == "--no-action":
            no_action = True
            index += 1
            continue
        if option not in ("--priority", "--source", "--key", "--action", "--text-id", "--value", "--due", "--question", "--push-reference") or index + 1 >= len(argv):
            return None
        value = argv[index + 1]
        if option == "--priority":
            if value not in PRIORITIES:
                return None
            priority = value
        elif option == "--question":
            question = value
        elif option == "--push-reference":
            forwarded_reference = value
        elif option == "--due":
            due = value
        elif option == "--source":
            if not value.strip():
                return None
            source = value
        elif option == "--key":
            push_key = value
        elif option == "--text-id":
            text_id = value
        elif option == "--value":
            key, separator, item = value.partition("=")
            if not separator or key in values:
                return None
            values[key] = item
        else:
            action = value
        index += 2
    if user_asked and text_id != NATE_ADHOC_TEXT_ID:
        print(f"pushover: {REFUSED_BEFORE_SENDING}: --user-asked requires --text-id {NATE_ADHOC_TEXT_ID}", file=sys.stderr)
        return None
    try:
        _ = decision_priority.read_due(due)
    except ValueError:
        return None
    try:
        reference = push_reference.admission_reference(text_id, question, forwarded_reference, remote_forward=remote_forward)
    except (OSError, ValueError) as error:
        print(f"pushover: {error}", file=sys.stderr)
        return None
    if not push_key and reference["kind"] == "question":
        push_key = "question:" + reference["path"]
    session: NotificationSession = NO_SESSION_LINE
    header = True
    one_line = False
    need = "blocked" if priority == "2" else "decision" if priority == "1" or due else "note"
    positionals = argv[index:]
    if text_id:
        try:
            rendered = push_texts.render(text_id, **values)
        except ValueError:
            return None
        if positionals or action is not None or no_action:
            return None
        source = source or rendered.sender
        need = "blocked" if priority == "2" else rendered.need
        if due and need != "decision":
            return None
        action = rendered.action.text if isinstance(rendered.action, ActionRequired) else None
        no_action = action is None
        one_line = rendered.one_line
        positionals = [rendered.posted_title, rendered.posted_body]
        session = rendered.session
        header = rendered.header and not one_line
    elif values:
        return None
    try:
        priority = decision_priority.priority(need, due)
    except ValueError:
        return None
    if (len(positionals) != 2 or (not one_line and not positionals[0].strip())
            or (not text_id and not positionals[1].strip())):
        return None
    if not text_id:
        session, positionals[1] = split_session(positionals[1])
    return {
        "silence_policy": silence_policy,
        "user_asked": user_asked,
        "reference": reference,
        "need": need,
        "due": due,
        "session": session,
        "priority": priority,
        "source": source,
        "key": push_key,
        "action": action,
        "no_action": no_action,
        "answerable": answerable,
        "title": positionals[0],
        "message": positionals[1],
        "text_id": text_id,
        "paid_credit_account": values.get("account", "") if text_id in PAID_CREDIT_TEXT_IDS else "",
        "header": header,
    }


def trimmed_body(action: UserAction, message: str, session: NotificationSession = NO_SESSION_LINE,
                 *, header: bool = True) -> str:
    body_limit = max(MESSAGE_MAX - len(first_line(action, session)) - 1, 0) if header else MESSAGE_MAX
    return message[:body_limit]


def posted_message(action: UserAction, message: str, session: NotificationSession = NO_SESSION_LINE,
                   *, header: bool = True) -> str:
    body = trimmed_body(action, message, session, header=header)
    return f"{first_line(action, session)}\n{body}" if header else body


def refuse(source: str, priority: str, title: str, message: str, reason: str) -> int:
    outcome = f"{REFUSED_BEFORE_SENDING}: {reason}"
    log(source, priority, title, message, outcome)
    print(f"pushover: {outcome}. {FIX}", file=sys.stderr)
    return 2


def send(argv: list[str], keys_file: Path = KEYS_FILE, post: Post = http_post) -> int:
    if "--text-id" not in argv:
        print("pushover: " + push_texts.TEMPLATE_REQUIRED, file=sys.stderr)
        return 2
    arguments = parse(argv)
    if arguments is None:
        print(USAGE, file=sys.stderr)
        return 2
    if not arguments["text_id"]:
        print("pushover: " + push_texts.TEMPLATE_REQUIRED, file=sys.stderr)
        return 2
    priority = arguments["priority"]
    source = arguments["source"] if arguments["source"] is not None else parent_source()
    title = arguments["title"][:TITLE_MAX]
    message = arguments["message"][:MESSAGE_MAX]
    action = parse_user_action(arguments["action"], arguments["no_action"])
    if isinstance(action, ActionRefused):
        return refuse(source, priority, title, message, action.reason)
    if isinstance(action, ActionUnstated):
        return refuse(source, priority, title, message, "no action stated")
    action_limit = max(ACTION_TEXT_MAX - len(session_prefix(arguments["session"])), 0)
    if isinstance(action, ActionRequired) and len(action.text) > action_limit:
        return refuse(source, priority, title, message,
                      f"--action may hold at most {action_limit} characters")
    if len(first_line(action, arguments["session"])) + 1 > MESSAGE_MAX:
        return refuse(source, priority, title, message, "Session and action exceed the message limit")
    action_priority = {"blocked": 2, "decision": 1, "note": 0}[arguments["need"]]
    match refused_at(action, action_priority):
        case ActionRefused(reason):
            return refuse(source, priority, title, message, reason)
        case ActionAllowed():
            pass
    draft = review.Draft(source, priority, title, action,
                         trimmed_body(action, arguments["message"], arguments["session"], header=arguments["header"]),
                         arguments["answerable"], arguments["session"], arguments["due"], arguments["reference"])
    header, key = arguments["header"], arguments["key"]
    registered_text = bool(arguments["text_id"]) and not push_texts.agent_written(arguments["text_id"])
    user_asked = arguments["user_asked"]
    claimed = False
    try:
        found = review.NoHold() if registered_text or user_asked else review.held_for(source)
        if isinstance(found, review.LiveHold):
            claimed = review.claim(source)
            if not claimed:
                print("pushover: held for review: this source's push is being sent", file=sys.stderr)
                return 4
            found = review.held_for(source)
    except OSError as error:
        found = review.UnreadableHold(review.held_path(source), str(error))
    detail = ""
    approved = push_texts.approval_date(arguments["text_id"]) if registered_text else ""
    if arguments["text_id"] == "quota.claude_expiry" and not approved:
        return refuse(source, priority, title, message, "Claude expiry template needs current approval")
    if arguments["text_id"] in TOKEN_ALERT_IDS and not approved:
        return refuse(source, priority, title, message, TOKEN_APPROVAL_REQUIRED)
    if user_asked:
        detail = "user asked"
        outcome: review.ReviewOutcome = review.Unreviewed(detail)
        version = draft
    elif approved:
        # The text was reviewed and approved ahead of time: no live review, no reviewer wait.
        detail = f"registered text, approved {approved}"
        outcome = review.Unreviewed(detail)
        version = draft
    elif review.switched_off():
        outcome = review.Unreviewed("review off")
        version = draft
        detail = "review: off"
    else:
        outcome = (review.review(draft, revision=claimed, text_id=arguments["text_id"]) if registered_text
                   else review.review(draft, revision=claimed))
        # For registered texts that are not agent-written, review is a logged check only.
        version = draft if registered_text else review.best_version(draft, outcome)
        if isinstance(outcome, review.Unreviewed):
            detail = "unreviewed: " + outcome.why
        elif registered_text and isinstance(outcome, review.Rewritten):
            detail = "registered text sent as rendered, rewrite not applied"
    if isinstance(outcome, review.Questions):
        outcome = review.Failed(outcome.best_version, outcome.failed_rules, outcome.metadata, outcome.failures)
    rejected = isinstance(outcome, (review.Failed, review.Withheld))
    template_required = not registered_text and isinstance(outcome, (review.Failed, review.Withheld)) and any(rule.startswith("use-registered-template:") for rule in outcome.failed_rules)
    send_anyway = rejected and not template_required and (priority == "2" or registered_text)
    if send_anyway:
        detail = "sent despite failed review: " + ", ".join(outcome.failed_rules if isinstance(outcome, (review.Failed, review.Withheld)) else [])
    record: ReviewedPushRecord = {**review.message_record(version), "reviewed": not isinstance(outcome, review.Unreviewed)}
    if arguments["text_id"]:
        record["text_id"] = arguments["text_id"]
    if rejected:
        assert isinstance(outcome, (review.Failed, review.Withheld))
        record["failed_rules"] = outcome.failed_rules
        record["findings"] = [{"rule": failure.rule, "quote": failure.quote} for failure in outcome.failures]
        record["held"] = not send_anyway
        record["sent_despite_failed_review"] = send_anyway
    if claimed and isinstance(found, review.LiveHold):
        record["held_source"] = source
        record["held_identity"] = review.hold_identity(found.hold)
    if rejected and not send_anyway and claimed and isinstance(found, review.LiveHold):
        review.retire_claim(found.hold)
    print("pushover: reviewed JSON: " + json.dumps(record), flush=True)
    if rejected and not send_anyway:
        review.log_review(draft, outcome, detail="not sent, failed review", text_id=arguments["text_id"])
        assert isinstance(outcome, (review.Failed, review.Withheld))
        why = outcome.why if isinstance(outcome, review.Withheld) else ", ".join(outcome.failed_rules)
        print("pushover: not sent: " + why, file=sys.stderr)
        return 6
    if claimed:
        if not isinstance(found, review.LiveHold):
            completion: review.ClaimDelivery = review.ClaimCancelled()
        else:
            completion = review.post_claim(found.hold, lambda: deliver(version, keys_file, post, detail=detail, header=header,
                                    key=key, text_id=arguments["text_id"], silence_policy=arguments["silence_policy"],
                                    paid_credit_account=arguments["paid_credit_account"]))
        if isinstance(completion, review.ClaimCancelled):
            review.log_review(draft, outcome, detail="claim cancelled before posting", text_id=arguments["text_id"])
            print("pushover: replaced: a newer version replaced this push; nothing was sent")
            return 5
        code = completion.code if isinstance(completion, review.ClaimFailed) else 0
    else:
        code = deliver(version, keys_file, post, detail=detail, header=header, key=key, text_id=arguments["text_id"],
                       silence_policy=arguments["silence_policy"], paid_credit_account=arguments["paid_credit_account"])
    delivery = review.Sent(version) if code == 0 else review.NotSent()
    review.log_review(draft, outcome, delivery, detail=detail, text_id=arguments["text_id"])
    if code == 0 and send_anyway:
        print("pushover: " + detail)
        if registered_text:
            request_template_fix(record)
    if code == 0 and not registered_text and isinstance(outcome, (review.Rewritten, review.Questions)):
        print("pushover: sent as rewritten:")
        print(version.title)
        print(first_line(version.action, version.session))
        print(version.body)
    return code


def request_template_fix(record: ReviewedPushRecord) -> None:
    """Direct timer callers use the sender's daily repair-notice handler too."""
    try:
        _ = subprocess.Popen(session_send.send_argv("template-fix", json.dumps(record)),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        print(f"pushover: template fix notice failed: {error}", file=sys.stderr)
        log("template fix", "0", record["title"], record["body"], f"notice failed: {error}")


def ask_natedev(argv: Sequence[str], text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), input=text, capture_output=True, text=True, check=False)


def via_natedev(arguments: Sequence[str], text: str, asker: AskNatedev, *,
                source: str, priority: str, title: str, message: str) -> int:
    """Return natedev's output and status. Log a failed connection without a local post."""
    try:
        reply = asker([*silence.SSH, f"{REMOTE} {shlex.join(arguments)}"], text)
    except (OSError, TimeoutError) as error:
        cause = str(error)
    else:
        if reply.returncode != 255 and (reply.stdout or reply.stderr):
            print(reply.stdout, end="")
            print(reply.stderr, end="", file=sys.stderr)
            return reply.returncode
        cause = reply.stderr.strip() or f"exit {reply.returncode}: no answer"
    log(source, priority, title, message, f"unreachable: natedev: {cause}")
    print(f"pushover: could not reach natedev: {cause}", file=sys.stderr)
    return 1


def deliver(draft: review.Draft, keys_file: Path = KEYS_FILE,
            post: Post = http_post, *, detail: str = "", header: bool = True, key: str = "", text_id: str = "",
            asker: AskNatedev = ask_natedev,
            silence_policy: silence.SilencePolicy = silence.SilencePolicy.KEEP_DURING_SILENCE,
            paid_credit_account: str = "") -> int:
    """Post an already reviewed version, including expiry sends; do not review again.

    A keyed push replaces a pending push from the same source only when their Question references match.
    """
    source, priority, title = draft.source, draft.priority, draft.title
    if not key and draft.reference["kind"] == "question":
        key = "question:" + draft.reference["path"]
    need = "blocked" if priority == "2" else "decision" if draft.due or priority == "1" else "note"
    priority = decision_priority.priority(need, draft.due)
    message = posted_message(draft.action, draft.body, draft.session, header=header)
    if text_id in PAID_CREDIT_TEXT_IDS and PAID_CREDIT_ACCOUNT.fullmatch(paid_credit_account):
        # Save the account even if the title is too short to show it.
        detail = f"{PAID_CREDIT_DETAIL_PREFIX}{paid_credit_account}\n{detail}"
    push: silence.KeptPush = {"source": source, "priority": priority, "title": title,
                              "message": message, "detail": detail, "key": key, "text_id": text_id,
                              "due": draft.due, "need": need, "reference": draft.reference}
    if silence_policy == silence.SilencePolicy.SEND_THROUGH_SILENCE:
        push["silence_policy"] = silence_policy
    if silence.on_mac():
        return via_natedev(["deliver"], json.dumps(push), asker,
                           source=source, priority=priority, title=title, message=message)
    return deliver_here(push, keys_file, post)


def paid_credit_details(push: silence.KeptPush) -> tuple[str, str]:
    """Return the log detail and the saved paid-credit account."""
    detail = push["detail"]
    if push["text_id"] in PAID_CREDIT_TEXT_IDS and detail.startswith(PAID_CREDIT_DETAIL_PREFIX):
        account, _, detail = detail.removeprefix(PAID_CREDIT_DETAIL_PREFIX).partition("\n")
        return detail, account
    return detail, ""


def deliver_here(push: silence.KeptPush, keys_file: Path = KEYS_FILE, post: Post = http_post) -> int:
    """Keep a reviewed push during silence or post it on natedev."""
    reference = push.get("reference", push_reference.NO_REFERENCE)
    if reference["kind"] == "question" and not push["key"]:
        push = {**push, "key": "question:" + reference["path"]}
    identity = silence.push_identity(push)
    if reference["kind"] == "question":
        try:
            if _can_keep(push):
                _ = silence.end_if_past(deliver=lambda kept: post_kept(kept, keys_file, post))
            with silence.locked():
                binding = question_pushes.bind_locked(identity, emergency=push["priority"] == "2", allow_unreadable=True)
                if binding.admitted_without_read:
                    print(f"{push_reference.UNREADABLE_QUESTION_REPORT}{reference['path']!r}.", file=sys.stderr)
                return _deliver_here(push, identity, keys_file, post, locked=True)
        except question_pushes.SettledQuestion as error:
            print(f"pushover: replaced: {error}")
            return 5
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"pushover: Question push could not be admitted: {error}", file=sys.stderr)
            return 1
    return _deliver_here(push, identity, keys_file, post)


def _can_keep(push: silence.KeptPush) -> bool:
    return (push["priority"] != "2" and push["text_id"] not in TOKEN_ALERT_IDS | {"quota.claude_expiry"}
            and push.get("silence_policy", silence.SilencePolicy.KEEP_DURING_SILENCE) == silence.SilencePolicy.KEEP_DURING_SILENCE)


def _deliver_here(push: silence.KeptPush, identity: push_reference.PushIdentity,
                  keys_file: Path, post: Post, *, locked: bool = False) -> int:
    source, priority, title, message = push["source"], push["priority"], push["title"], push["message"]
    detail, _ = paid_credit_details(push)
    if push["text_id"] == "quota.claude_expiry" or push["text_id"] in TOKEN_ALERT_IDS:
        if silence.active_locked() if locked else silence.active():
            return refuse(source, priority, title, message, "silence is active")
    elif _can_keep(push):
        kept = (silence.keep_locked(push) if locked else
                silence.keep_here(push, deliver=lambda kept: post_kept(kept, keys_file, post)))
        if isinstance(kept, silence.Kept):
            log(source, priority, title, message, f"kept: silence {kept.until}" + (f", {detail}" if detail else ""))
            print(f"pushover: kept: silence {kept.until}")
            return 0
    return _post_message(identity, priority, title, message, keys_file, post, detail, push["text_id"]).exit_code


def _update_kept_restart_failure(message: str) -> push_texts.RenderedPush | None:
    """Read the session and account from the saved legacy text."""
    action, _, body = message.partition("\n")
    prefix = "Action: In the Claude session named natedev on the natedev computer, run /agents restart "
    if not action.startswith(prefix) or not action.endswith("."):
        return None
    account = action[len(prefix):-1]
    prefix = "After your restart request, "
    if not body.startswith(prefix):
        return None
    session, found, _ = body[len(prefix):].partition(f" on Claude account {account} did not restart on ")
    if not found or re.match(r"(?:one|[0-9]+) sessions?\b", session, re.IGNORECASE):
        return None
    try:
        return push_texts.render(RESTART_FAILED_TEXT_ID, session=session, account=account, identity="")
    except ValueError:
        return None


def refresh_paid_credit_action(push: silence.KeptPush, account: str) -> str:
    """Use the live account list for a paid-credit push released from silence."""
    message = push["message"]
    prefix = "Action: " + PAID_CREDIT_ACTION_PREFIX
    if push["text_id"] not in PAID_CREDIT_TEXT_IDS or not message.startswith(prefix):
        return message
    # Earlier kept pushes saved the rendered title and action.
    account = account or push["title"].partition(" ")[0]
    try:
        if not PAID_CREDIT_ACCOUNT.fullmatch(account):
            raise ValueError("account missing from the kept push")
        from scripts.whoami.agent_accounts import codex_credit_accounts

        accounts = remaining_credit_accounts(account, codex_credit_accounts())
    except (OSError, ValueError) as error:
        log(push["source"], push["priority"], push["title"], message,
            f"anomaly: kept paid-credit action uses the saved account list: {error}",
            key=push["key"], template=push["text_id"])
        return message
    _, newline, body = message.partition("\n")
    return f"{prefix}{accounts}{newline}{body}"


def post_kept(push: silence.KeptPush, keys_file: Path = KEYS_FILE, post: Post = http_post) -> int:
    """Drop settled requests and retired alerts before posting kept pushes."""
    reference = push.get("reference", push_reference.NO_REFERENCE)
    text_id = push["text_id"]
    reason = ""
    identity = silence.push_identity(push)
    confirmed_open = False
    admitted_unreadable = False
    title, message = push["title"], push["message"]
    if text_id and not any(entry.id == text_id for entry in push_texts.catalog()):
        reason = f"retired template {text_id}"
    else:
        if text_id == RESTART_FAILED_TEXT_ID and (
                title == "Claude coding work remains stopped" or "/agents restart " in message.partition("\n")[0]):
            rendered = _update_kept_restart_failure(message)
            if rendered is None:
                reason = "restart failure lacks saved resume values"
            else:
                title, message = rendered.posted_title, rendered.message
        if text_id in TOKEN_ALERT_IDS and not push_texts.approval_date(text_id):
            reason = TOKEN_APPROVAL_REQUIRED
        if reference["kind"] == "question":
            binding_path = question_pushes.binding_path(push["source"], push["key"])
            try:
                reason = question_pushes.settled(reference)
                confirmed_open = not reason
            except BlockingIOError:
                return 1
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                log(push["source"], push["priority"], push["title"], push["message"],
                    f"anomaly: question record {reference['path']}: {error}",
                    key=push["key"], template=push["text_id"], reference=reference)
                if binding_path.exists() or reference.get("machine") == "mac":
                    try:
                        binding = question_pushes.read_binding(binding_path)
                    except (OSError, ValueError):
                        return 1
                    if binding is None or binding.identity != identity or not binding.admitted_without_read:
                        return 1
                    admitted_unreadable = True
    if reason:
        log(push["source"], push["priority"], push["title"], push["message"], "dropped: " + reason,
            key=push["key"], template=push["text_id"], reference=reference)
        return push_reference.KeptPushDropped()
    detail, account = paid_credit_details(push)
    detail = f"kept during silence, {detail}" if detail else "kept during silence"
    if push["text_id"] in PAID_CREDIT_TEXT_IDS:
        message = refresh_paid_credit_action(push, account)
    need = push.get("need") or {"0": "note", "1": "decision", "2": "blocked"}[push["priority"]]
    priority = decision_priority.priority(need, push.get("due", ""))

    def post_once(url: str, data: bytes) -> tuple[int, bytes]:
        if priority == "2" and (confirmed_open or admitted_unreadable):
            _ = question_pushes.bind_locked(identity, emergency=True, allow_unreadable=admitted_unreadable)
        silence.mark_post_started(push)
        return post(url, data)

    def rejected(reason: str) -> None:
        if push.get("post_started", False):
            silence.mark_post_rejected(push, reason)

    exit_code = 1
    try:
        exit_code = post_message(identity.sender, priority, title, message, keys_file, post_once, detail,
                                 identity.key, text_id, reference=reference, on_rejected=rejected)
        return exit_code
    finally:
        if exit_code != 0 and push.get("post_started", False):
            log(push["source"], priority, title, message, "kept push post unconfirmed",
                key=push["key"], template=text_id, reference=reference)


@dataclass(frozen=True)
class PostAccepted:
    exit_code: ClassVar[int] = 0


@dataclass(frozen=True)
class PostRejected:
    reason: str
    exit_code: int = 1


@dataclass(frozen=True)
class PostUnknown:
    exit_code: ClassVar[int] = 1


PostResult = PostAccepted | PostRejected | PostUnknown


def post_message(source: str, priority: str, title: str, message: str,
                 keys_file: Path, post: Post, detail: str, key: str = "", text_id: str = "", *,
                 reference: push_reference.PushReference = push_reference.NO_REFERENCE,
                 on_rejected: Callable[[str], None] | None = None) -> int:
    """Post a push and report a confirmed rejection to the caller."""
    result = _post_message(push_reference.PushIdentity(source, key, reference), priority, title, message,
                           keys_file, post, detail, text_id)
    if isinstance(result, PostRejected) and on_rejected is not None:
        on_rejected(result.reason)
    return result.exit_code


def _post_message(identity: push_reference.PushIdentity, priority: str, title: str, message: str,
                  keys_file: Path, post: Post, detail: str, text_id: str) -> PostResult:
    source = identity.sender
    if text_id in TOKEN_ALERT_IDS and not push_texts.approval_date(text_id):
        _ = refuse(source, priority, title, message, TOKEN_APPROVAL_REQUIRED)
        return PostRejected(TOKEN_APPROVAL_REQUIRED, 2)
    suffix = f", {detail}" if detail else ""
    sender = sender_name(source)
    if not sender:
        log(source, priority, title, message, f"{REFUSED_BEFORE_SENDING}: no sender" + suffix)
        print(f"pushover: {REFUSED_BEFORE_SENDING}: no sender. Name the sender with --source NAME.", file=sys.stderr)
        return PostRejected("no sender", 2)
    message = message[:phone_message_limit(source, text_id)].rstrip() + _phone_footer(sender, text_id)
    keys = read_keys(keys_file)
    if keys is None:
        outcome = f"{REFUSED_BEFORE_SENDING}: keys missing"
        log(source, priority, title, message, outcome + suffix)
        print(f"pushover: {outcome}. Set PUSHOVER_USER and PUSHOVER_TOKEN in {keys_file}.", file=sys.stderr)
        return PostRejected("keys missing", 2)
    data = urllib.parse.urlencode(fields(keys, title, message, priority, identity.tag_key)).encode()
    try:
        status, raw = post(API_URL, data)
    except (OSError, http.client.HTTPException) as error:
        log(source, priority, title, message, f"unreachable: {error}" + suffix)
        print(f"pushover: could not reach Pushover: {error}", file=sys.stderr)
        return PostUnknown()
    try:
        decoded = cast(object, json.loads(raw))
    except (UnicodeError, json.JSONDecodeError):
        decoded = None
    reply = cast(dict[str, object], decoded) if isinstance(decoded, dict) else {}
    outcome_code = reply.get("status")
    if status == 200 and type(outcome_code) is int and outcome_code == 1:
        receipt = reply.get("receipt")
        outcome = f"sent, receipt {receipt}" if receipt else "sent"
        log(source, priority, title, message, outcome + suffix)
        print(f"pushover: {outcome}")
        return PostAccepted()
    errors = reply.get("errors")
    reason = ("; ".join(error for error in cast(list[object], errors) if isinstance(error, str))
              if isinstance(errors, list) else "") or f"HTTP {status}"
    if 400 <= status < 500 or status == 200 and type(outcome_code) is int and outcome_code != 1:
        log(source, priority, title, message, f"refused: {reason}" + suffix)
        print(f"pushover: refused: {reason}", file=sys.stderr)
        return PostRejected(reason)
    log(source, priority, title, message, f"unconfirmed: {reason}" + suffix)
    print(f"pushover: unconfirmed: {reason}", file=sys.stderr)
    return PostUnknown()


def cancel(argv: list[str], keys_file: Path = KEYS_FILE, post: Post = http_post, *,
           asker: AskNatedev = ask_natedev) -> int:
    """Cancel the exact tag named by the command."""
    source = parent_source()
    if argv[:1] == ["--exact-tag-key"]:
        argv = argv[1:]
    reference: push_reference.PushReference = push_reference.NO_REFERENCE
    if argv[:1] == ["--reference"] and len(argv) >= 2:
        try:
            reference = push_reference.read_reference(cast(object, json.loads(argv[1])))
        except ValueError as error:
            print(f"pushover: {error}", file=sys.stderr)
            return 2
        argv = argv[2:]
    if len(argv) == 3 and argv[0] == "--source" and argv[1].strip():
        source, argv = argv[1], argv[2:]
    if len(argv) != 1 or not argv[0] or argv[0].startswith("--"):
        print(CANCEL_USAGE, file=sys.stderr)
        return 2
    identity = push_reference.PushIdentity(source, argv[0], reference)
    tag = push_tag(identity.tag_key)
    title = "cancel emergency push"
    message = f"key {identity.key}, tag {tag}"
    if silence.on_mac():
        return via_natedev(["cancel", "--exact-tag-key", "--source", source, identity.tag_key], "", asker,
                           source=source, priority="2", title=title, message=message)
    keys = read_keys(keys_file)
    if keys is None:
        log(source, "2", title, message, "cancel: keys missing")
        print(f"pushover: PUSHOVER_USER and PUSHOVER_TOKEN must both be set in {keys_file}", file=sys.stderr)
        return 2
    try:
        status, raw = post(CANCEL_URL.format(tag=tag), urllib.parse.urlencode({"token": keys["token"]}).encode())
    except (urllib.error.URLError, TimeoutError) as error:
        log(source, "2", title, message, f"cancel: unreachable: {error}")
        print(f"pushover: could not reach Pushover: {error}", file=sys.stderr)
        return 1
    try:
        decoded = cast(object, json.loads(raw))
    except json.JSONDecodeError:
        decoded = None
    reply = cast(Reply, cast(object, decoded)) if isinstance(decoded, dict) else Reply()
    if status == 200 and reply.get("status") == 1:
        count = reply.get("canceled")
        outcome = "nothing to cancel" if count == 0 else f"cancelled {count}" if isinstance(count, int) else "cancelled"
        log(source, "2", title, message, "cancel: " + outcome)
        print(f"pushover: {outcome}")
        return 0
    errors = "; ".join(reply.get("errors", [])) or f"HTTP {status}"
    log(source, "2", title, message, f"cancel: refused: {errors}")
    print(f"pushover: refused: {errors}", file=sys.stderr)
    return 1


def main(argv: list[str], keys_file: Path = KEYS_FILE, post: Post = http_post) -> int:
    if len(argv) == 2 and argv[0] == "question-opening" and "SSH_CONNECTION" in os.environ:
        try:
            opening = push_reference.read_question(Path(argv[1]))
        except (OSError, ValueError) as error:
            print(error, file=sys.stderr)
            return 1
        print(json.dumps({"status": opening.status, "since": opening.since}))
        return 0
    if argv == ["deliver"]:
        push = silence.parse_push(sys.stdin.read())
        if push is None:
            print("pushover: deliver reads one push as JSON on stdin", file=sys.stderr)
            return 2
        return deliver_here(push, keys_file, post)
    return cancel(argv[1:], keys_file, post) if argv[:1] == ["cancel"] else send(argv, keys_file, post)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
