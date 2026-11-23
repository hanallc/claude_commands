"""Bind real run roots to private test paths, and start a test as a new command."""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import cast
from unittest import TestCase, mock

from scripts.lib import unit_runs


def bind_private_run_roots(case: TestCase, root: Path) -> None:
    """Replace each real root and keep roots that a fixture sets explicitly."""
    for name, current, folder in (
        ("RUN_ROOT", unit_runs.RUN_ROOT, "unit"),
        ("FORMER_RUN_ROOT", unit_runs.FORMER_RUN_ROOT, "delegate"),
        ("ASTRA_RUN_ROOT", unit_runs.ASTRA_RUN_ROOT, "astra"),
    ):
        if current == Path("/tmp/claude") / folder:
            patch = mock.patch.object(unit_runs, name, root / folder)
            case.addCleanup(patch.stop)
            _ = cast(object, patch.start())


def start_new_command(case: TestCase) -> None:
    """Start the test as a new command, so report_anomaly prints each anomaly again."""
    patch = mock.patch.object(unit_runs, "_reported_anomalies", ContextVar("reported_anomalies", default=frozenset[str]()))
    case.addCleanup(patch.stop)
    _ = cast(object, patch.start())
