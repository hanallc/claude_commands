"""Synthetic sign-in files for account and launcher tests."""

from __future__ import annotations

from datetime import datetime, timezone
import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.whoami.usage_store import StoredUsage


def write_codex_auth(home: Path, login: str) -> None:
    """Write a fake ID token identifying the login in an existing Codex home."""
    claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
    _ = (home / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}})
    )


def store_usage(tool: str, login: str, usage: StoredUsage) -> None:
    """Store one login's readings under the store lock."""
    from scripts.whoami import usage_store

    with usage_store.read_lock():
        saved = usage_store.load()
        saved[usage_store.key(tool, login)] = usage
        usage_store.write(saved)


def stored_room(login: str, left: float, resets: datetime) -> None:
    """Store a fresh weekly reading for a Claude fixture account."""
    from scripts.whoami import usage_store

    store_usage("Claude", login, usage_store.StoredUsage(weekly=usage_store.StoredWindow(
        left=left, at=datetime.now(timezone.utc).isoformat(), resets=resets.isoformat())))


def create_claude_account(label: str, login: str) -> None:
    """Create the account note and its signed-in identity."""
    from scripts.accounts import accounts
    from scripts.whoami import note_file

    notes = note_file.notes_dir()
    notes.mkdir(parents=True, exist_ok=True)
    _ = (notes / note_file.note_name(label)).write_text(f"---\nlogin: {login}\n---\n")
    problems = accounts.setup(label)
    if problems:
        raise AssertionError(problems)
    _ = (accounts.account_dir(label) / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": login},
    }))


def write_claude_account(label: str, login: str, left: float, reset: datetime) -> None:
    """Create a Claude account with a fresh stored quota reading."""
    create_claude_account(label, login)
    stored_room(login, left, reset)
