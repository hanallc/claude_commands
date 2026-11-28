"""Check Console text before a command publishes it."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast, get_args

from scripts.notify import review

Kind = Literal["question", "reply", "change", "proposal", "investigation answer",
                    "efficiency candidate", "list field", "image caption"]

_FAST_ONLY = frozenset(("efficiency candidate", "list field"))
_URL = re.compile(r"https?://[^\s<>()`]+")
_PATH_PART = r"[\w.-]+"
_PATH = re.compile(
    rf"(?<![\w./])(?:(?:~|\.\.?)?/{_PATH_PART}(?:/{_PATH_PART})*"
    + rf"|(?:{_PATH_PART}/)+{_PATH_PART}\.[\w-]+)(?<!\.)"
)
_SESSION = re.compile(r"(?<![\w-])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![\w-])", re.I)
_RUN = re.compile(
    rf"(?<![\w-])\d{{8}}T\d{{6}}Z?-\d+(?:-[0-9a-f]{{4,}})?(?:@{_SESSION.pattern})?(?![\w-])",
    re.I,
)
_COMMIT = re.compile(r"\b(?:[0-9a-f]{40}|[0-9a-f]{64}|"
                     + r"(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,39})\b", re.I)
_SHELL_FENCE = re.compile(r"```(?:sh|bash|zsh|shell|console)\s*$", re.I)
_SHELL_LINE = re.compile(
    r"\$\s+\S|[a-z./~][\w./~+-]*(?: +[^\s-]+)* +"
    + r"(?:--?[a-zA-Z0-9][\w-]*(?:\s|=|$)|(?:\|\|?|&&|>>?)\s+\S)"
)
_CODE_COMMAND = re.compile(r"`(?:[a-z_][\w-]*|[\w./~+-]+\s+[^\n`]+)`")
_ITEM = re.compile(r"^(?:\d+\. |[-*] )")
_SENTENCE_END = re.compile(
    r"""(?:(?<!\b[A-Z])(?<!\b[A-Z][a-z])(?<!\b[A-Z][a-z]{2})\.|[!?])[.!?]*["'’”)]*(?=\s|$)"""
)
_YES_NO = re.compile(r"Choices:\s*\n- (yes|no)\b[^\n]*\n- (yes|no)\b[^\n]*\s*\Z", re.I)
_COMMAND_LABEL = re.compile(r"(?<![\w`])(?i:run) this command:")
_LABELED_CODE_COMMAND = re.compile(
    rf"{_COMMAND_LABEL.pattern}[ \t]*({_CODE_COMMAND.pattern}|`[\w.~+-]*/[\w./~+-]+`)(?!`)"
)


def _length_failures(text: str, kind: Kind) -> list[review.RuleFailure]:
    blocks = re.split(r"\n[ \t]*\n", text.strip())
    point_index = int(len(blocks) > 1 and (kind == "question" or blocks[0].startswith("#")))
    if blocks[point_index].strip("* ").casefold() == "the point" and len(blocks) > point_index + 1:
        point_index += 1
    point = blocks[point_index]
    if kind == "question" and (_ITEM.match(point) or point.startswith(("Choices:", "Recommended:"))):
        point = blocks[0]
    failures: list[review.RuleFailure] = []
    sentences = len(_SENTENCE_END.findall(point))
    if _SENTENCE_END.split(point)[-1].strip():
        sentences += 1
    if sentences > 3:
        failures.append(review.RuleFailure("point-at-most-three-sentences", point))
    choices = _YES_NO.search(text)
    if choices and choices[1].casefold() != choices[2].casefold():
        facts = [line for line in text[:choices.start()].splitlines() if _ITEM.match(line)]
        if len(facts) > 3:
            failures.append(review.RuleFailure("yes-no-at-most-three-fact-lines", facts[3]))
    return failures


def _check_visible_text(content: str, failures: list[review.RuleFailure]) -> None:
    visible = _URL.sub(" ", content)
    for rule, pattern in (("omit-run-id", _RUN), ("omit-session-id", _SESSION),
                          ("omit-file-path", _PATH), ("omit-commit-hash", _COMMIT)):
        for match in pattern.finditer(visible):
            failures.append(review.RuleFailure(rule, match[0]))
        visible = pattern.sub(" ", visible)


def fast_check(text: str, kind: Kind) -> tuple[review.RuleFailure, ...]:
    """Return failed patterns with quotes. Put the question title in the first block."""
    failures = _length_failures(text, kind)
    command_allowed = used_command = shell_fence = False
    for line in text.splitlines():
        content = _ITEM.sub("", line.strip(), count=1)
        parts = _LABELED_CODE_COMMAND.split(content) if not shell_fence else [content]
        if len(parts) > 1:
            for command in parts[1::2]:
                if used_command:
                    failures.append(review.RuleFailure("command-for-user-only", command))
                used_command = True
            command_allowed = False
            for prose in parts[::2]:
                for command in _CODE_COMMAND.finditer(prose):
                    failures.append(review.RuleFailure("command-for-user-only", command[0]))
                prose = _CODE_COMMAND.sub(" ", prose).strip()
                if _SHELL_LINE.match(prose):
                    failures.append(review.RuleFailure("command-for-user-only", prose))
                else:
                    _check_visible_text(prose, failures)
            continue
        if label := _COMMAND_LABEL.match(content):
            command_allowed = not used_command
            content = content[label.end():].strip()
        if not content:
            continue
        if _SHELL_FENCE.fullmatch(content):
            shell_fence = True
            continue
        if content == "```":
            shell_fence = command_allowed = False
            continue
        if shell_fence or _SHELL_LINE.match(content) or _CODE_COMMAND.fullmatch(content):
            if command_allowed:
                used_command = True
            else:
                failures.append(review.RuleFailure("command-for-user-only", content))
            command_allowed = False
            continue
        command_allowed = False
        _check_visible_text(content, failures)
    return tuple(failures)


def check(text: str, kind: Kind, *, key: review.QuestionKey | None = None,
          title: str = "", caption_layout: review.CaptionLayout | None = None) -> review.QuestionOutcome:
    """Run the filing check. Use a stable key to retain line approvals and holds across revisions."""
    failures = fast_check(text, kind)
    heading = f"## {title}"
    lines = text.splitlines()
    separate_title = bool(title) and title not in lines and heading not in lines
    if separate_title:
        failures += fast_check(title, "list field")
    if failures:
        outcome = review.QuestionReviewFailed(text, (), failures)
        if key is not None:
            review.save_question_review(key, outcome)
        return outcome
    if kind in _FAST_ONLY:
        return review.QuestionAccepted(text)
    draft = f"{heading}\n\n{text}" if separate_title else text
    model_key = key or review.QuestionKey(kind, draft)
    if title:
        model_key = replace(model_key, console_draft_digest=review.question_part_digest((title, text)),
                            console_title=title)
    if caption_layout is None:
        result = review.review_question(model_key, draft, console_kind=kind)
    else:
        result = review.review_question(model_key, draft, console_kind=kind, caption_layout=caption_layout)
    return replace(result, question=text) if separate_title else result


def require(text: str | Sequence[str], kind: Kind, *, identity: str | review.QuestionKey = "", title: str = "",
            refusal: type[Exception] = ValueError, caption_layout: review.CaptionLayout | None = None) -> None:
    """Refuse a failed draft. Use a sequence for separate fields that need only the fast check."""
    if isinstance(identity, str):
        key = review.QuestionKey(f"{kind}/{identity}", "") if identity else None
    else:
        key = identity
    drafts = (text,) if isinstance(text, str) else text
    reasons: list[str] = []
    for draft in drafts:
        if not draft.strip():
            continue
        result = check(draft, kind, key=key, title=title, caption_layout=caption_layout)
        if isinstance(result, (review.QuestionReviewFailed, review.QuestionAwaitingFacts)):
            details = result.questions if isinstance(result, review.QuestionAwaitingFacts) else result.details
            reasons.extend(details)
            reasons.extend(f"{failure.rule}: {failure.quote}" for failure in result.failures)
    if reasons:
        raise refusal(f"Revise the {kind} draft. " + "; ".join(reasons))


def main(arguments: Sequence[str]) -> int:
    """Check a draft before the filing command writes it."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    _ = parser.add_argument("--kind", choices=get_args(Kind), required=True)
    _ = parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        path = cast(Path, args.file).resolve()
        draft = path.read_text(encoding="utf-8")
        if not draft.strip():
            raise ValueError("The draft is empty.")
        kind = cast(Kind, args.kind)
        require(draft, kind, identity=review.QuestionKey(kind, str(path)))
    except (OSError, ValueError) as error:
        print(f"Console text: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
