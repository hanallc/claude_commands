"""Private fixtures for tests that render the status line."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import cast
from zoneinfo import ZoneInfo

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.statusline import assignment

PROGRAM = Path(__file__).with_name("statusline.jq")
USER_ZONE = ZoneInfo("America/New_York")
# Preserve the fixture PATH when NixOS starts a child shell.
SHELL_ENVIRONMENT = {"__NIXOS_SET_ENVIRONMENT_DONE": "1", "__ETC_ZSHENV_SOURCED": "1"}


def statusline_environment(root: Path) -> dict[str, str]:
    """Create private pane and zone settings for a test runtime."""
    panes = root / "panes"
    panes.mkdir()
    zone_config = root / "user_zone.conf"
    _ = zone_config.write_text(f"zone={USER_ZONE.key}\n", encoding="utf-8")
    zone_files = next(folder for folder in ("/etc/zoneinfo", "/usr/share/zoneinfo", "/var/db/timezone/zoneinfo")
                      if (Path(folder) / USER_ZONE.key).is_file())
    return {"PATH": os.environ["PATH"], "HOME": str(root), "TZDIR": zone_files,
            "HERDR_PANE_RECORD_DIR": str(panes), "USER_ZONE_CONF": str(zone_config)}


def status_lines(*inputs: object, labels: Path | None = None, account: str = "claude1",
                 colors: Path | str = "", counts: str = "", rebuild: str = "", adhoc: str = "",
                 staff_text: str = "", **marks: str) -> list[str]:
    """Run statusline.jq once over every input and return its output lines.

    Pass a color file path or color settings as text.
    Empty color settings omit the color argument.
    """
    payload = cast(dict[str, object], inputs[0]) if isinstance(inputs[0], dict) else {}
    record = assignment.staff_record(staff_text)
    label, role = assignment.assignment(record, str(payload.get("session_id", "")))
    stdin = "\n".join(json.dumps(each) for each in inputs)
    stored = [] if labels is None else ["--slurpfile", "labels", str(labels)]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        branch = assignment.current_branch(payload, str(root / "checkout")) if role == "unit-director" else ""
        parent = ["--arg", "assignment", label, "--arg", "role", role, "--arg", "counts", counts,
                  "--arg", "rebuild", rebuild, "--arg", "adhoc", adhoc, "--arg", "branch", branch,
                  "--arg", "for_you", marks.pop("for_you", "")]
        configured: list[str] = []
        if colors:
            path = colors if isinstance(colors, Path) else root / "statusline.conf"
            if isinstance(colors, str):
                _ = path.write_text(colors, encoding="utf-8")
            configured = ["--rawfile", "colors", str(path)]
        result = subprocess.run(
            ["jq", "-r", "--arg", "pwd", "/tmp", "--arg", "account", account,
             *stored, *parent, *configured, "-f", str(PROGRAM)],
            input=stdin, capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS,
            env={**SHELL_ENVIRONMENT, **statusline_environment(root), "TZ": USER_ZONE.key, **marks},
        )
    return result.stdout.splitlines()
