"""Render a Markdown reply in the Claude Code terminal and save a PNG cut to the reply's rows.

Usage: reply_shot.py <markdown file> <columns> <png>
A resumed transcript holds the reply, so no model call happens.
The shot ends at the first blank band below the reply, so the Images page shows text at a readable size.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from scripts.lib import conf, nix_env

# Claude Code resumes the transcript with this placeholder key and makes no model call.
PLACEHOLDER_KEY = "sk-ant-shot-only-0000000000000000000000"
FONT = ("-fa", "DejaVu Sans Mono", "-fs", "9")
SCREEN = "3000x3400x24"
BORDER_PIXELS = 8


def lit_rows(row_means: str) -> list[bool]:
    """Read `magick ... -scale 1x! txt:-` output: one mean per row, True when the row holds any pixel."""
    return [line.split()[1] != "(0)" for line in row_means.splitlines()[1:]]


def content_height(lit: Sequence[bool], gap_rows: int) -> int | None:
    """The row count above the first blank band of `gap_rows` rows, or None when no band is that tall."""
    blank = 0
    for index, value in enumerate(lit):
        blank = 0 if value else blank + 1
        if blank == gap_rows:
            return index - gap_rows + 1
    return None


def crop_to_content(png: Path, gap_rows: int) -> None:
    """Cut the shot at the first blank band, and keep a small black border."""
    means = subprocess.run(["magick", str(png), "+repage", "-colorspace", "gray", "-scale", "1x!", "-depth", "16",
                            "txt:-"], check=True, capture_output=True, text=True).stdout
    height = content_height(lit_rows(means), gap_rows)
    if height is None:
        return
    _ = subprocess.run(["magick", str(png), "+repage", "-crop", f"x{height}+0+0", "+repage",
                        "-bordercolor", "black", "-border", str(BORDER_PIXELS), str(png)], check=True)


def transcript(markdown: str, cwd: Path, session: str, version: str) -> str:
    """One user prompt and one assistant reply that holds the Markdown."""
    now = "2026-01-01T00:00:00.000Z"
    user_id, reply_id = str(uuid.uuid4()), str(uuid.uuid4())
    common = {"isSidechain": False, "userType": "external", "cwd": str(cwd), "sessionId": session,
              "version": version, "gitBranch": "", "timestamp": now}
    lines = [
        {**common, "type": "user", "parentUuid": None, "uuid": user_id,
         "message": {"role": "user", "content": "/showrunner:units"}},
        {**common, "type": "assistant", "parentUuid": user_id, "uuid": reply_id,
         "message": {"id": "msg_shot", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                     "content": [{"type": "text", "text": markdown}], "stop_reason": "end_turn",
                     "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    return "".join(json.dumps(line) + "\n" for line in lines)


def shot(markdown: Path, columns: int, png: Path) -> None:
    """Render the reply in Xvfb and xterm, take the shot, and cut it to the reply."""
    settings = conf.read("review_links")
    claude = shutil.which("claude")
    if claude is None:
        raise RuntimeError("claude is not on PATH")
    version = subprocess.run([claude, "--version"], check=True, capture_output=True, text=True).stdout.split()[0]
    with tempfile.TemporaryDirectory(prefix="reply-shot-") as work:
        config, cwd = Path(work) / "config", Path(work) / "cwd"
        cwd.mkdir()
        config.mkdir()
        _ = (config / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True, "theme": "dark", "numStartups": 5,
            "customApiKeyResponses": {"approved": [PLACEHOLDER_KEY[-20:]], "rejected": []},
            "projects": {str(cwd): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))
        session = str(uuid.uuid4())
        project = config / "projects" / str(cwd).replace("/", "-").replace(".", "-")
        project.mkdir(parents=True)
        _ = (project / f"{session}.jsonl").write_text(transcript(markdown.read_text(), cwd, session, version))
        read_end, write_end = os.pipe()
        xvfb = subprocess.Popen(["Xvfb", "-displayfd", str(write_end), "-screen", "0", SCREEN],
                                pass_fds=(write_end,), stderr=subprocess.DEVNULL)
        os.close(write_end)
        try:
            with os.fdopen(read_end) as display_number:
                display = f":{display_number.readline().strip()}"
            env = {**os.environ, "DISPLAY": display, "CLAUDE_CONFIG_DIR": str(config),
                   "ANTHROPIC_API_KEY": PLACEHOLDER_KEY}
            for name in [name for name in env if name.startswith("CLAUDE_CODE") or name in ("CLAUDECODE", "WAYLAND_DISPLAY")]:
                del env[name]
            loaded = nix_env.ensure(cwd, env)
            if isinstance(loaded, nix_env.Refused):
                raise RuntimeError(loaded.reason)
            env = loaded.env
            rows = settings.whole("reply_shot_terminal_rows")
            term = subprocess.Popen(["xterm", "-geometry", f"{columns}x{rows}+0+0", *FONT, "-bg", "black", "-fg", "white",
                                     "-e", claude, "--bare", "--resume", session], cwd=cwd, env=env)
            try:
                time.sleep(settings.whole("reply_shot_render_seconds"))
                _ = subprocess.run(["import", "-display", display, "-window", "root", "-trim", str(png)], check=True)
            finally:
                term.terminate()
                _ = term.wait(timeout=10)
        finally:
            xvfb.terminate()
            _ = xvfb.wait(timeout=10)
    crop_to_content(png, settings.whole("reply_shot_gap_rows"))


def main(argv: Sequence[str]) -> int:
    if len(argv) != 3 or not argv[1].isdigit():
        print("usage: reply_shot.py <markdown file> <columns> <png>", file=sys.stderr)
        return 2
    shot(Path(argv[0]), int(argv[1]), Path(argv[2]))
    print(argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
