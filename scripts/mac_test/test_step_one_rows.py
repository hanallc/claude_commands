"""Check the Mac probe and SSH settings in private fixtures."""
from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.lib import conf
from scripts.mac_test import claim_state, eligibility, offload


class StepOneRows(unittest.TestCase):
    def test_mac_probe_saves_exact_start_fence_before_ssh(self) -> None:
        """offload.probe_mac row 6."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "mac_test.conf"
            registry = root / "build_targets.conf"
            _ = settings.write_text("host=fixture-host\nmirror_root=~/fixture/mirror\n# range: 1..\nconnect_timeout_s=2\n# range: 1..\nprobe_timeout_s=3\n")
            _ = registry.write_text("targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
                                    + "# whole: 1..\nunreachable_wait_seconds=60\n")
            with mock.patch.dict(os.environ, {"CONF_DIR": temporary, "MAC_TEST_STATE_DIR": str(root / "state")}):
                controller = offload.ProcessController()
                identity = claim_state.ClaimIdentity("claim", "episode", "attempt")
                controller.claim_identity = identity
                controller.claim_scope_key = "run/second"
                key = claim_state.pending_start_key(identity)
                path = root / "state/pending-starts" / f"{key}-second"

                def capture(command: Sequence[str], deadline: offload.CommandDeadline) -> offload.CommandOutput:
                    self.assertEqual(path.read_text(), key)
                    self.assertEqual(deadline, offload.CommandTimeLimit(3))
                    self.assertIn('if [ "$probe_status" -eq 0 ]; then', command[-1])
                    relative = f".local/state/mac-test/dispatch-fences/run/{key}-second"
                    self.assertIn(relative.encode().hex(), command[-1])
                    return offload.CommandOutput(0, "rc=0\nfree_gib=42\nhost=fixture-host\n", "")

                with mock.patch.object(controller, "capture", side_effect=capture) as transport:
                    config = eligibility.probe_settings(conf.read(settings), conf.read(registry))
                    result = offload.probe_mac(controller, config, eligibility.KnownRepository("fixture", "key"))
                self.assertEqual(result, offload.ProbeSnapshot(42, "fixture-host"))
                transport.assert_called_once()

    def test_guest_target_reads_kind_platform_and_settings(self) -> None:
        """Target registry and settings helpers row 7."""
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "build_targets.conf"
            _ = registry.write_text("kind.mac-linux=ssh\nplatform.mac-linux=linux\nsettings.mac-linux=guest.conf\n")
            with (mock.patch.dict(os.environ, {"CONF_DIR": temporary}),
                  mock.patch.object(conf, "read", wraps=conf.read) as read,
                  mock.patch.object(conf.ConfigRead, "text", autospec=True,
                                    side_effect=conf.ConfigRead.text) as key):
                target = eligibility.configured_ssh_target("mac-linux")
                self.assertEqual(target.platform, "linux")
                self.assertEqual(target.settings, registry.with_name("guest.conf"))
                self.assertEqual([call.args[1] for call in key.call_args_list],
                                 ["kind.mac-linux", "platform.mac-linux", "settings.mac-linux"])
            read.assert_called_once_with("build_targets")
            _ = registry.write_text("kind.mac-linux=ssh\nplatform.mac-linux=macos\n")
            with (mock.patch.dict(os.environ, {"CONF_DIR": temporary}),
                  self.assertRaisesRegex(conf.Unusable, "must use SSH and the Linux platform")):
                _ = eligibility.configured_ssh_target("mac-linux")

    def test_probe_settings_read_wait_before_connection_keys(self) -> None:
        """Target registry and settings helpers row 11."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ssh.conf"
            _ = path.write_text("host=fixture-host\n# range: 1..\nconnect_timeout_s=2\n"
                                + "# range: 1..\nprobe_timeout_s=3\n")
            registry_path = path.with_name("build_targets.conf")
            _ = registry_path.write_text("# whole: 1..\nunreachable_wait_seconds=60\n")
            config, registry = conf.read(path), conf.read(registry_path)
            with (mock.patch.object(conf.ConfigRead, "whole", autospec=True,
                                    side_effect=conf.ConfigRead.whole) as whole,
                  mock.patch.object(conf.ConfigRead, "text", autospec=True,
                                    side_effect=conf.ConfigRead.text) as text,
                  mock.patch.object(conf.ConfigRead, "number", autospec=True,
                                    side_effect=conf.ConfigRead.number) as number):
                settings = eligibility.probe_settings(config, registry)
                whole.assert_called_once_with(registry, "unreachable_wait_seconds")
                text.assert_not_called()
                number.assert_not_called()
                self.assertEqual(settings.host, "fixture-host")
                self.assertEqual(settings.connect_timeout_s, 2)
                self.assertEqual(settings.probe_timeout_s, 3)
                text.assert_called_once_with(config, "host")
                self.assertEqual(number.call_args_list,
                                 [mock.call(config, "connect_timeout_s"), mock.call(config, "probe_timeout_s")])
