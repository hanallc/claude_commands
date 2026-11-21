"""Provide private sender settings and guard against real sends."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
import unittest

from scripts.message.codex_test_support import CODEX_DAEMON_SETTINGS

POSTMASTER_SETTINGS = """\
# range: 0.001..
wait_seconds=3.0
# range: 0.001..
claim_grace_seconds=15.0
# range: 0.001..
heartbeat_max_age_seconds=2.0
# range: 0.001..
poll_seconds=0.05
"""

SEND_SETTINGS = """\
failed_lookup_owners=aws-spending-guard=builds
# range: 0.001..
waits_review_seconds=30
# range: 0.001..
delivery_timeout=40
# range: 0.001..
kill_grace=10
# range: 0.001..
user_cancel_timeout=30
# range: 0.001..
check_timeout=20
# range: 0.001..
caller_margin_seconds=15
# range: 0.001..
remote_transport_seconds=30
# range: 0.001..
decision_high_priority_window_seconds=1800
# range: 0.001..
main_sync_repeat_minutes=360
# one of: on, off
review_hold=off
# range: 0.001..
review_hold_idle_minutes=30
"""

SEND_TIMING_FIXTURES = {
    "codex_daemon.conf": CODEX_DAEMON_SETTINGS,
    "postmaster.conf": POSTMASTER_SETTINGS,
    "send.conf": SEND_SETTINGS,
}


@dataclass(frozen=True)
class SenderGuardNotInstalled:
    """The module has no sender refusal marker."""


@dataclass(frozen=True)
class SenderRefusalMarker:
    path: Path


refusal_marker: SenderGuardNotInstalled | SenderRefusalMarker = SenderGuardNotInstalled()
real_sender_started = False


def real_sender_was_started() -> bool:
    """Report a refusal before or after the module cleanup."""
    return real_sender_started or (
        isinstance(refusal_marker, SenderRefusalMarker) and refusal_marker.path.is_file()
    )


def forbid_real_sender() -> None:
    """Set one inherited marker path before any test can start the sender."""
    global refusal_marker
    if isinstance(refusal_marker, SenderGuardNotInstalled):
        previous_marker = os.environ.get("TEST_SENDER_GUARD")
        marker = previous_marker
        if not marker:
            directory = unittest.enterModuleContext(tempfile.TemporaryDirectory(prefix="sender-guard-"))
            marker = str(Path(directory) / "refused")
            os.environ["TEST_SENDER_GUARD"] = marker
        installed_marker = SenderRefusalMarker(Path(marker))
        refusal_marker = installed_marker

        def finish_guard() -> None:
            global refusal_marker, real_sender_started
            real_sender_started = real_sender_was_started()
            if previous_marker is None:
                _ = os.environ.pop("TEST_SENDER_GUARD", None)
            else:
                os.environ["TEST_SENDER_GUARD"] = previous_marker
            refusal_marker = SenderGuardNotInstalled()

        # Module cleanups run in reverse order. Record the result before removing the folder.
        unittest.addModuleCleanup(finish_guard)
