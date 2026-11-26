"""Give natedev sessions commands to copy Mac files named in typed prompts."""

from __future__ import annotations

import re
import sys

from scripts.agents import remote
from scripts.lib import conf


_PATH = re.compile(
    r"""(?<![\w/])(?:"(/Users/[^"\r\n\x00]+)"|'(/Users/[^'\r\n\x00]+)'|`(/Users/[^`\r\n\x00]+)`|"""
    + r"""(/Users/(?:(?![ \t]/Users/|[,;:.!?…—–)\]}>”’][ \t])[^"`\r\n\x00])*?"""
    + r"\.[0-9]*[A-Za-z][A-Za-z0-9]*\b(?!/)))"
)


def find_paths(prompt: str) -> tuple[str, ...]:
    """Return each Mac path once in prompt order."""
    paths: dict[str, None] = {}
    for match in _PATH.finditer(prompt):
        quoted, single_quoted, backticked, unquoted = match.groups("")
        path = quoted or single_quoted or backticked or re.sub(r"\\([\\ ])", r"\1", unquoted)
        paths[path] = None
    return tuple(paths)


def context(prompt: str) -> str:
    """Give one copy command for each Mac path on natedev."""
    if sys.platform != "linux":
        return ""
    if remote.local_machine().casefold() != "natedev":
        return ""
    paths = find_paths(prompt)
    if not paths:
        return ""
    route = conf.read("production").text("mac_run_host")
    lines: list[str] = []
    for path in paths:
        # Escape the SFTP pattern before quoting the shell argument.
        escaped = re.sub(r"([\\*?\[\]])", r"\\\1", path)
        source = f"{route}:{escaped}".replace("'", "'\"'\"'")
        lines.append(
            f"The Mac file {path} is on the Mac. Copy it into your scratch folder with: "
            + f"scp '{source}' \"<your scratch folder>/\" and then read the copy."
        )
    return "\n".join(lines)
