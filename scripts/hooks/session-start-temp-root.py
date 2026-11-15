#!/usr/bin/env python3
"""Give each Claude session its own folder and point its Bash commands' TMPDIR at it.

Test runs, mktemp and tempfile left their folders loose in /tmp, and nothing named their maker
(user, 2026-10-27). Each session gets its session folder at start (and again at resume, clear and
compact, should it be gone): /session/<member name> (scripts/lib/session_folder.py), or
/tmp/claude/session/<session id> until the session root exists. Claude Code reads the
`export TMPDIR=...` line this hook writes to $CLAUDE_ENV_FILE before each Bash command. The path
stays short, so a Unix socket made in TMPDIR fits the 104-byte limit on the Mac.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import shlex
import sys
from pathlib import Path
from typing import cast


# The former root; tmp_sweep.py judges its entries until cutover.
ROOT = "/tmp/claude/session"


def temp_parent() -> Path:
    from scripts.lib import session_folder

    return session_folder.former_root()


def export_line(path: Path) -> str:
    return f"export TMPDIR={shlex.quote(str(path))}\n"


def main() -> int:
    from scripts.lib import build_folders, session_folder

    try:
        raw = cast(object, json.loads(sys.stdin.read()))
    except (ValueError, UnicodeDecodeError):
        return 0
    session_id = cast(dict[str, object], raw).get("session_id") if isinstance(raw, dict) else None
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not isinstance(session_id, str) or not session_folder.SESSION_ID.fullmatch(session_id) or not env_file:
        return 0
    try:
        # The session uses this TMPDIR after the startup hook exits.
        path = session_folder.session_folder(session_id) or build_folders.create_build_folder(
            build_folders.StableOwnerFolder(
                session_id, build_folders.ExplicitBuildParent(session_folder.former_root()), mode=0o700))
        line = export_line(path)
        env = Path(env_file)
        if line not in (env.read_text(encoding="utf-8") if env.exists() else ""):
            with env.open("a", encoding="utf-8") as handle:
                _ = handle.write(line)
    except OSError as error:
        print(f"session temp root: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
