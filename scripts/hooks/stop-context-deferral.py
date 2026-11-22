#!/usr/bin/env python3
"""Block one stop when the assistant defers work because of context."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import re
import sys
from bisect import bisect_left, bisect_right
from typing import TypedDict, cast

from scripts.lib import conf

REASON = "Compaction runs by itself mid-work; do the work now."
NON_PROSE = re.compile(
    "|".join((
        r"(?m:^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n).*?(?:(?m:^[ \t]*(?P=fence)[ \t]*(?:\n|$))|\Z)",
        r"(?P<ticks>`+)[^\n]*?(?P=ticks)",
        r'"(?:\\.|[^"\\])*"|“[^”]*”',
        r"(?<!\w)'(?:[^']|'(?=\w))*?'(?!\w)|‘(?:[^’]|’(?=\w))*?’(?!\w)",
    )),
    re.DOTALL,
)


class StopPayload(TypedDict, total=False):
    last_assistant_message: str
    stop_hook_active: bool


def prose(text: str) -> str:
    lines: list[str] = []
    quoted = False
    for line in NON_PROSE.sub(" ", text).splitlines(keepends=True):
        if not line.strip():
            quoted = False
        elif line.lstrip(" \t").startswith(">"):
            quoted = True
        if not quoted:
            lines.append(line)
    return " ".join("".join(lines).split())


def neighbors(matches: list[re.Match[str]], action: re.Match[str]) -> list[re.Match[str]]:
    after = bisect_left(matches, action.start(), key=lambda match: match.start())
    before = bisect_right(matches, action.end(), key=lambda match: match.end()) - 1
    # These three matches cover the nearest left, right, and containing regions.
    return [matches[index] for index in (before, after - 1, after) if 0 <= index < len(matches)]


def blocks(text: str, rules: conf.DeferralRules) -> bool:
    matches = {kind: list(pattern.finditer(text)) for kind, pattern in rules.phrases.items()}
    markers = matches.get("marker", [])
    states = matches.get("state", [])
    later = matches.get("later", [])
    exclusions = matches["exclude"]
    if not markers and not (states and later):
        return False

    def hit(region: tuple[re.Match[str], ...]) -> bool:
        start = min(match.start() for match in region)
        end = max(match.end() for match in region)
        if end - start > rules.window:
            return False
        index = bisect_left(exclusions, end, key=lambda match: match.start()) - 1
        return index < 0 or exclusions[index].end() <= start

    for action in matches["action"]:
        if any(hit((action, marker)) for marker in neighbors(markers, action)):
            return True
        if states and later:
            for state in neighbors(states, action):
                if any(hit((action, state, word)) for word in neighbors(later, action)):
                    return True
    return False


def main() -> None:
    payload = cast(StopPayload, json.loads(sys.stdin.read()))
    if payload.get("stop_hook_active"):
        return
    text = payload.get("last_assistant_message", "")
    if not text:
        return
    source = conf.path("context_deferral").read_text(encoding="utf-8")
    if blocks(prose(text), conf.parse_context_deferral(source)):
        print(REASON, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook failure permits the stop without output.
        pass
    sys.exit(0)
