"""The action a notification says the user takes.

Priority 1 is high and priority 2 is emergency. Both require an action. Emergency
is reserved for an action the user must take now because work cannot continue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FIX = ('Say what the user does with --action "<what they do>", or pass '
       "--no-action when nothing is needed (priority 0 only).")
REFUSED_BEFORE_SENDING = "refused before sending"
LEGACY_NO_ACTION_LINE = "No action needed."

_PLACEHOLDERS = frozenset(("none", "n/a", "nothing", "no action", "-"))


@dataclass(frozen=True)
class ActionRequired:
    text: str


@dataclass(frozen=True)
class NoActionRequired:
    pass


UserAction = ActionRequired | NoActionRequired


@dataclass(frozen=True)
class ActionUnstated:
    pass


@dataclass(frozen=True)
class ActionRefused:
    reason: str


@dataclass(frozen=True)
class ActionAllowed:
    pass


def parse_user_action(action: str | None, no_action: bool) -> UserAction | ActionUnstated | ActionRefused:
    """Turn the two command-line forms into one explicit action state."""
    if action is not None and no_action:
        return ActionRefused("--action and --no-action cannot be used together")
    if action is None:
        return NoActionRequired() if no_action else ActionUnstated()
    text = action.strip()
    if not text:
        return ActionRefused("--action cannot be empty")
    if text.casefold() in _PLACEHOLDERS:
        return ActionRefused(f'--action "{text}" does not state an action; use --no-action')
    return ActionRequired(text)


def refused_at(action: UserAction, priority: int) -> ActionAllowed | ActionRefused:
    """Refuse action-free high or emergency alerts; emergency means act now."""
    if isinstance(action, NoActionRequired) and priority > 0:
        return ActionRefused(f"priority {priority} requires --action")
    return ActionAllowed()


@dataclass(frozen=True)
class SessionLine:
    text: str


@dataclass(frozen=True)
class NoSessionLine:
    pass


NotificationSession = SessionLine | NoSessionLine
NO_SESSION_LINE = NoSessionLine()


def session_prefix(session: NotificationSession) -> str:
    return f"Session: {session.text}\n" if isinstance(session, SessionLine) else ""


def split_session(message: str) -> tuple[NotificationSession, str]:
    line, separator, body = message.partition("\n")
    if line.startswith("Session: "):
        return SessionLine(line.removeprefix("Session: ")), body if separator else ""
    return NO_SESSION_LINE, message


def notification_header(action: UserAction, session: NotificationSession = NO_SESSION_LINE) -> str:
    """Return the session and action lines when present."""
    if isinstance(action, ActionRequired):
        return session_prefix(session) + f"Action: {action.text}"
    return session_prefix(session).removesuffix("\n")


_LEADING_BLANK_LINES = re.compile(r"\A(?:[^\S\r\n]*(?:\r\n|[\r\n]))*(?:[^\S\r\n]*\Z)?")


def notification_body(action: UserAction, body: str) -> str:
    """Remove leading blank lines and the old header from a note."""
    if isinstance(action, ActionRequired):
        return body
    body = _LEADING_BLANK_LINES.sub("", body)
    line, _, rest = body.partition("\n")
    if line.removesuffix("\r") == LEGACY_NO_ACTION_LINE:
        return _LEADING_BLANK_LINES.sub("", rest)
    return body


def notification_message(action: UserAction, body: str, session: NotificationSession = NO_SESSION_LINE,
                         *, header: bool = True) -> str:
    """Join the header and body when both are present."""
    body = notification_body(action, body)
    lines = notification_header(action, session) if header else ""
    if not lines:
        return body
    return f"{lines}\n{body}" if body else lines


def read_notification(message: str, header: str | None, *, action_expected: bool = False
                      ) -> tuple[UserAction, str, NotificationSession]:
    """Read a saved message using its header or the action required by its record."""
    if header is not None:
        if not header:
            return NoActionRequired(), message, NO_SESSION_LINE
        session, action_line = split_session(header)
        action = ActionRequired(action_line.removeprefix("Action: ")) if action_line.startswith("Action: ") else NoActionRequired()
        body = "" if message == header else message.removeprefix(header + "\n")
        return action, body, session
    session, text = split_session(message)
    line, _, body = text.partition("\n")
    if line.removesuffix("\r") == LEGACY_NO_ACTION_LINE:
        action = NoActionRequired()
        return action, notification_body(action, text), session
    if action_expected and line.startswith("Action: "):
        return ActionRequired(line.removeprefix("Action: ")), body, session
    return NoActionRequired(), message, NO_SESSION_LINE


# A literal thing to type in a body: "type" or "type:" and its text, or a "! <command>" shell escape.
# The text ends at the end of its sentence or line.
_TYPED = re.compile(r"(?:\b(?P<verb>type)\b:?[ \t]+|(?<!\S)!(?=[ \t]*[\w~./-]))(?P<text>[^\n]*?)(?=\.(?:\s|$)|\n|$)",
                    re.IGNORECASE)
# "a type of" and "the type" are a noun; "type the go" and "type it" point to text, they do not give it.
_NOUN_BEFORE = frozenset(("a", "an", "the", "this", "that", "each", "every", "any", "one", "its", "your",
                          "their", "which", "what", "same", "file"))
_POINTERS = frozenset(("a", "an", "the", "it", "this", "that", "them", "these", "those", "your", "its", "of"))
# A pane or session name: "in the X pane", "X session", "session named X".
_NAME = r"[\w-]+(?:\.[\w-]+)*"
_PLACE = re.compile(rf"(?<![\w'’.-])(?P<name>{_NAME})\s+(?=(?:pane|session)\b)|\bsession named (?P<named>{_NAME})",
                    re.IGNORECASE)
_PLACE_END = re.compile(rf"\s+(?:in|at|on)\s+(?:the\s+)?{_NAME}\s+(?:pane|session)$", re.IGNORECASE)
# Words before "pane" or "session" that are not a name: "the pane", "a Claude session", "its coding session".
_NOT_NAMES = frozenset(("a", "an", "the", "this", "that", "these", "those", "its", "their", "your", "our", "my",
                        "his", "her", "each", "every", "any", "one", "other", "another", "same", "own", "new",
                        "old", "which", "what", "no", "claude", "codex", "coding", "terminal", "shell"))


def typed_texts(text: str) -> list[str]:
    """Each literal thing the text tells the user to type, without its "!" and the pane or session after it."""
    found: list[str] = []
    for match in _TYPED.finditer(text):
        before, words = text[:match.start()].split(), match.group("text").split()
        if match.group("verb") and ((before and before[-1].casefold() in _NOUN_BEFORE)
                                    or (words and words[0].casefold() in _POINTERS)):
            continue
        typed = _PLACE_END.sub("", match.group("text").strip(" .,;:")).strip(" .,;:\"'`!")
        if typed:
            found.append(typed)
    return found


def named_places(text: str) -> set[str]:
    """The pane and session names from the text, in lower case."""
    names = {(match.group("name") or match.group("named")).casefold()
             for match in _PLACE.finditer(text)
             if match.group("named") or not match.group("name").isdecimal()}
    return names - _NOT_NAMES


def action_conflicts(action: UserAction, body: str) -> list[str]:
    """Why the body (the text after the Session and Action lines) gives a second action: a thing to
    type that the Action line does not hold, or a pane or session that is not the one the Action line
    names. When the Action line names no pane or session, only the first check applies."""
    if not isinstance(action, ActionRequired):
        return []
    held = " ".join(action.text.split()).casefold()
    reasons = [f'the body says to type "{typed}", which the Action line does not hold'
               for typed in typed_texts(body) if " ".join(typed.split()).casefold() not in held]
    named = named_places(action.text)
    if named:
        reasons.extend(f"the body names the {name} pane or session, which the Action line does not name"
                       for name in sorted(named_places(body) - named))
    return reasons
