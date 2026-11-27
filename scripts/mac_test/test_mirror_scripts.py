"""Test the paths in Mac control scripts with the process selectors."""

from __future__ import annotations

from collections.abc import Sequence
from functools import cached_property
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.copies import copies
from scripts.lib import conf
from scripts.mac_test import claim_state, eligibility, mac_test, offload
from scripts.mac_test.mirror_root import MacMirrorRoot


CONFIG = Path(__file__).resolve().parents[2] / "config"


def command_pattern(scope: offload.MirrorScope) -> re.Pattern[str]:
    assignment = offload.mirror_selector_script(scope).splitlines()[0]
    value, = shlex.split(assignment.removeprefix("mirror_command_pattern="))
    return re.compile(value.replace("[:space:]", r"\s"))


class MirrorScriptTests(unittest.TestCase):
    @cached_property
    def settings(self) -> conf.ConfigRead:
        return conf.read(CONFIG / "mac_test.conf")

    @cached_property
    def repositories(self) -> tuple[eligibility.KnownRepository, ...]:
        return tuple(
            eligibility.KnownRepository(key.removeprefix("linux_only."))
            for key in self.settings.values() if key.startswith("linux_only.")
        )

    @cached_property
    def root(self) -> MacMirrorRoot:
        return MacMirrorRoot.read(self.settings)

    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(CONFIG)})))
        self.assertTrue(self.repositories)
        self.addCleanup(offload.offload_mirror_root.reset, offload.offload_mirror_root.set(self.root))

    def test_mac_control_scripts_do_not_match_mirror_selectors(self) -> None:
        config = eligibility.read_config(self.settings.source)
        paths = [offload.remote_mirror(repository) for repository in self.repositories]
        patterns = [command_pattern(scope) for scope in (*self.repositories, offload.ALL_MIRRORS)]
        legacy_names = tuple(repository.legacy_mirror_name for repository in self.repositories)
        identity = claim_state.ClaimIdentity("claim", "episode", "attempt")
        fence = offload.start_fence_relative(identity, "mac").with_suffix(".lock")
        machine = "d764b59a-1a37-4c78-8a1a-9c0aa3e5962b"
        controller = offload.ProcessController()
        captured: list[str] = []

        def capture(arguments: Sequence[str], _deadline: offload.CommandDeadline) -> offload.CommandOutput:
            captured.append(arguments[-1])
            return offload.CommandOutput(0, "free_gib=120\nhost=fixture-mac\nrc=0\n", "")

        with (mock.patch.object(offload, "record_reachable"),
              mock.patch.object(controller, "capture", side_effect=capture)):
            _ = offload.probe_mac_host(
                controller, eligibility.probe_settings(self.settings), "mac",
                tuple(repository.name for repository in self.repositories), setup_mirrors=legacy_names,
            )
        host_probe, = captured
        captured.clear()
        with (tempfile.TemporaryDirectory() as state,
              mock.patch.object(eligibility, "state_directory", return_value=Path(state)),
              mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(config.free_floor_gib, "mac")),
              mock.patch.object(controller, "capture", side_effect=capture)):
            self.assertEqual(offload.ensure_mac_disk_floor(
                controller, config, self.repositories[0], offload.ProbeSnapshot(0, "mac"),
            ), offload.MacDiskReady(config.free_floor_gib))
        floor_sweep, = captured
        scripts: dict[str, str] = {
            "host probe setup and scan": host_probe,
            "floor sweep": floor_sweep,
            "activity": mac_test.mac_activity_script(),
            "fence setup": offload.prepare_start_fence_command(identity, "mac", machine_id_timeout=1),
            "fence cleanup": offload.clean_start_fences_command(identity, "mac"),
            "fence holder program": offload.FENCE_HOLDER_PROGRAM,
            "fence release": offload.start_fence_command(identity, "mac", settle=True, report_holders=True),
        }
        for repository in self.repositories:
            request = offload.OffloadRequest(
                CONFIG, repository.name, "script-test", offload.NO_RESULT_FILE, (),
                owner="verify", holder_session="fixture-session",
            )
            mirror = offload.request_mirror(config, request, repository)
            cleanup = offload.remote_cleanup_command(config, request, repository)
            observe = offload.leftover_check_script(repository, fence=fence, observe=True, mirror_root=self.root)
            repository_scripts = {
                "probe": offload.probe_script(repository),
                "observation": observe,
                "watched fence release": offload.start_fence_command(
                    identity, "mac", settle=True, report_holders=True, leftover_observation=(observe, 1.0, machine),
                ),
                "end cleanup": cleanup,
                "cleanup-only wrapper": copies.remote_use_command(mirror, cleanup),
                "fetch size": offload.fetch_size_command(repository, "mac", 1, offload.FetchFolder("target/shots")),
                "floor removal": copies.remote_copies_command("mirror-remove", mirror, "--host", mirror.host),
            }
            scripts.update((f"{name} for {repository.name}", script) for name, script in repository_scripts.items())
            run = offload.remote_test_command(config, request, repository)
            self.assertRegex(run, command_pattern(repository))
        for name, script in scripts.items():
            with self.subTest(script=name):
                for path in paths:
                    self.assertNotIn(path, script)
                for pattern in patterns:
                    self.assertNotRegex(script, pattern)

    def test_mac_probe_child_decodes_the_original_path(self) -> None:
        for repository in self.repositories:
            path = offload.remote_mirror(repository)
            with self.subTest(repository=repository.name):
                setup = offload.probe_script(repository).split(" || exit 1", 1)[0]
                program, encoded = shlex.split(setup.split(" -c ", 1)[1])
                self.assertNotIn(path, encoded)
                with (mock.patch.object(copies, "probe_remote_folder") as probe,
                      mock.patch.object(sys, "argv", ["-c", encoded])):
                    exec(program, {})
                probe.assert_called_once_with(path)
