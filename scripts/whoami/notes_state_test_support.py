"""Private state paths for tests that run the agent notes pass."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts.whoami import agent_notes, codex_pacer, quota_alert, run_out


@contextmanager
def private_notes_state(root: Path) -> Generator[None]:
    """Point the notes pass state files at root.

    These modules fix their paths at import, so a changed HOME or XDG_STATE_HOME
    does not move them. Without this, a test reads and writes the live state.
    """
    pacer = root / "codex-pacer"
    with ExitStack() as stack:
        for module, name, path in (
            (agent_notes, "READINGS_LOG", root / "readings.jsonl"),
            (run_out, "READINGS_LOG", root / "readings.jsonl"),
            (quota_alert, "STATE", root / "quota_alert.json"),
            (codex_pacer, "STATE_DIR", pacer),
            (codex_pacer, "STATE_FILE", pacer / "state.json"),
            (codex_pacer, "PLAN_FILE", pacer / "plan.json"),
            (codex_pacer, "TIER_FILE", pacer / "tier"),
        ):
            _ = stack.enter_context(patch.object(module, name, path))
        yield
