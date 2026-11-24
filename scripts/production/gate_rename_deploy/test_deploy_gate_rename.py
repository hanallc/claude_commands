"""Check the words that authorize a deployment drain."""

from __future__ import annotations

from contextlib import nullcontext, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.build_hold import build_hold
from scripts.build_hold.build_hold_test_support import TEST_HOLD_USER_WORDS
from scripts.lib import conf
from scripts.production.gate_rename_deploy import deploy_gate_rename as deploy
from scripts.production.gate_rename_deploy import rename_step_names as rename


class DeploymentHoldTests(unittest.TestCase):
    def test_deployment_requires_user_words_to_create_a_hold(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            settings = deploy.DeploymentSettings(60, 1, 2, ZoneInfo("UTC"), 1000)
            for words in ("", " \t\n"):
                with self.subTest(words=words), \
                     mock.patch.object(build_hold, "holder_directory", return_value=root / "holders"), \
                     mock.patch.object(build_hold, "natedev_holds", return_value=build_hold.NatedevHolds(())), \
                     mock.patch.object(deploy, "hold_command") as hold:
                    paths = deploy.DeploymentPaths(root, root, root / "journal.json",
                                                  rename.RecordRoots(root, root, root, ()), settings, 0, words)
                    state = deploy.DeploymentState(next_step=2)
                    with self.assertRaisesRegex(deploy.DeploymentFailed, "Nate's explicit request") as refused:
                        deploy.execute_step(2, paths, state)
                    self.assertEqual(refused.exception.status, 2)
                    self.assertIn("--user-words", str(refused.exception))
                    self.assertFalse(state.owns_hold)
                    self.assertFalse(paths.journal.exists())
                    hold.assert_not_called()

    def test_deployment_restarts_services_and_releases_its_hold_without_user_words(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch)
            tree = home / ".claude"
            state_folder = home / "deploy-state"
            state_folder.mkdir()
            journal = state_folder / "deploy_gate_rename.state.json"
            holders = home / "holders"
            holders.mkdir()
            (holders / deploy.HOLDER).touch()
            roots = rename.RecordRoots(home, home, home, ())
            settings = deploy.DeploymentSettings(60, 1, 2, ZoneInfo("UTC"), 1000)
            paths = deploy.DeploymentPaths(tree, home, journal, roots, settings, 0, "")
            state = deploy.DeploymentState(next_step=deploy.RESTART_STEP, owns_hold=True, stopped_at=1,
                                           gate_folders=deploy.GateFolders(None, None))
            deploy.save(paths, state)
            with mock.patch.dict(os.environ), mock.patch.object(time, "tzset"), \
                 mock.patch.object(deploy, "HERE", tree / "scripts/production/gate_rename_deploy"), \
                 mock.patch.object(deploy, "checked_home", return_value=home), \
                 mock.patch.object(conf, "read"), \
                 mock.patch.object(deploy.DeploymentSettings, "read", return_value=settings), \
                 mock.patch.object(rename, "configured_roots", return_value=roots), \
                 mock.patch.object(deploy, "run", return_value=str(state_folder)), \
                 mock.patch.object(deploy, "require_installed_code"), \
                 mock.patch.object(deploy, "change_services") as services, \
                 mock.patch.object(build_hold, "holder_directory", return_value=holders), \
                 mock.patch.object(build_hold, "release_lock", return_value=nullcontext()), \
                 mock.patch.object(deploy, "run_python", return_value="") as run_python, redirect_stdout(StringIO()):
                self.assertEqual(deploy.main([]), 0)
            services.assert_called_once_with(mock.ANY, "start")
            run_python.assert_called_once_with(
                mock.ANY, tree / "scripts/build_hold/build_hold.py",
                ["release", "natedev", "--holder", deploy.HOLDER], "release natedev", writes=True,
            )
            finished = deploy.load(journal, False)
            self.assertEqual(finished.next_step, deploy.LAST + 1)
            self.assertEqual(finished.stopped_at, 0)
            self.assertFalse(finished.owns_hold)

    def test_deployment_drain_passes_exact_user_words(self) -> None:
        words = "  " + TEST_HOLD_USER_WORDS + "  "
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            settings = deploy.DeploymentSettings(60, 1, 2, ZoneInfo("UTC"), 1000)
            paths = deploy.DeploymentPaths(root, root, root / "journal.json",
                                           rename.RecordRoots(root, root, root, ()), settings, 0, words)
            state = deploy.DeploymentState()
            with mock.patch.object(build_hold, "holder_directory", return_value=root / "holders"), \
                 mock.patch.object(build_hold, "natedev_holds", return_value=build_hold.NatedevHolds(())), \
                 mock.patch.object(deploy, "run_python", return_value="") as run_python:
                deploy.execute_step(2, paths, state)
            self.assertTrue(state.owns_hold)
            run_python.assert_called_once_with(
                paths, root / "scripts/build_hold/build_hold.py",
                ["drain", "natedev", "--holder", deploy.HOLDER,
                 "--for", "gate folder and step-name deployment", "--release-eta", mock.ANY,
                 "--zone", "UTC", f"--user-words={words}"],
                "drain natedev", writes=True,
            )
