"""Check that native agent launches give plain Python the startup hook."""
from __future__ import annotations

from itertools import product
import os
from pathlib import Path
import unittest
from unittest import mock

from scripts.herdr import herdr
from scripts.production import staff


class DirectTestEnvironmentTests(unittest.TestCase):
    def test_agents_get_the_startup_path_outside_a_test_limit(self) -> None:
        root = Path(__file__).resolve().parents[2]
        startup = root / "scripts/lib/session_python_startup"
        inherited = str(root / "fixture-py-libs")
        prepared = herdr.PreparedAgentEnvironment(root, {"PYTHONPATH": inherited})
        for kind, limited in product(("claude", "codex"), (False, True)):
            with (self.subTest(kind=kind, limited=limited),
                  mock.patch.object(herdr, "within_test_limit", return_value=limited),
                  mock.patch("scripts.herdr.herdr.models_registry.claude_arguments", return_value=[]),
                  mock.patch.object(herdr, "trust_claude_launch"),
                  mock.patch("scripts.herdr.herdr._tab_staff", return_value=staff.StaffSnapshot(())),
                  mock.patch.object(herdr, "open_tab", return_value="w1:p1") as opened,
                  mock.patch.object(herdr.Herdr, "run_in_pane"),
                  mock.patch.object(herdr, "forget_pane_evidence"),
                  mock.patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired())):
                result = herdr.launch_prepared_agent(
                    "agents", herdr.NewTab("workers", "worker"), prepared, kind, [],
                    herdr.NoOpeningPrompt(), 5000, agent="worker")
                self.assertIsInstance(result.outcome, herdr.PromptSubmissionNotRequired)
                pythonpath = inherited if limited else os.pathsep.join((str(startup), inherited))
                opened.assert_called_once_with("agents", "workers", "worker", str(root), {"PYTHONPATH": pythonpath})
