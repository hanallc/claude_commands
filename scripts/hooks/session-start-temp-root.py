#!/usr/bin/env python3
"""Set TMPDIR for each Claude session.

The hook uses the current session root or a folder that remains in the former root.
Claude Code reads the export from CLAUDE_ENV_FILE before each Bash command.
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
    fields = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    session_id, cwd = fields.get("session_id"), fields.get("cwd")
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not isinstance(session_id, str) or not session_folder.SESSION_ID.fullmatch(session_id) or not env_file:
        return 0
    try:
        path = session_folder.session_folder(session_id, cwd if isinstance(cwd, str) else "",
                                             starting=fields.get("source") in ("startup", "resume"))
        if path is None:
            path = build_folders.create_build_folder(
                build_folders.StableOwnerFolder(
                    session_id, build_folders.ExplicitBuildParent(session_folder.former_root()), mode=0o700))
        line = export_line(path)
        env = Path(env_file)
        lines = env.read_text(encoding="utf-8").splitlines(keepends=True) if env.exists() else []
        if next((item for item in reversed(lines) if item.startswith("export TMPDIR=")), "") != line:
            with env.open("a", encoding="utf-8") as handle:
                _ = handle.write(line)
    except OSError as error:
        print(f"session temp root: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
