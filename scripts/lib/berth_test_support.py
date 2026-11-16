"""A cargo-berth stub for fixtures whose repository cargo-berth does not configure."""

from __future__ import annotations

from pathlib import Path

# A fixture's .git is often not a real worktree, and a real cargo-berth then
# answers that the ledger is unreadable. This answer makes the lint fix pass
# write the worktree in place, as in any repository that berth does not configure.
UNCONFIGURED_BERTH = """#!/bin/sh
printf '%s\\n' '{"output_contract_version":6,"verb":"board","status":"unconfigured","exit_code":4,"payload":{"kind":"no_facts"},"message":"fixture"}'
exit 4
"""


def write_unconfigured_berth(directory: Path) -> None:
    """Put the unconfigured cargo-berth stub in directory, which is on the fixture PATH."""
    path = directory / "cargo-berth"
    _ = path.write_text(UNCONFIGURED_BERTH, encoding="utf-8")
    path.chmod(0o755)
