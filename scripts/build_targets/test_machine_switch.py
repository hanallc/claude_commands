"""Test strict machine switches with a private registry."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.build_targets.machine_switch import Machine, MachineSwitch, MachineSwitchError, read


class MachineSwitchTests(unittest.TestCase):
    def test_reads_the_build_target_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"CONF_DIR": directory}):
            source = Path(directory) / "build_targets.conf"
            _ = source.write_text("enabled.mac=off\nenabled.aws-gate=on\nenabled.hana-aws=off\nenabled.hana-gpu=on\n")
            self.assertIs(read(Machine.MAC), MachineSwitch.OFF)
            self.assertIs(read(Machine.AWS_GATE), MachineSwitch.ON)
            self.assertIs(read(Machine.HANA_AWS), MachineSwitch.OFF)
            self.assertIs(read(Machine.HANA_GPU), MachineSwitch.ON)

    def test_names_the_key_and_file_when_the_value_is_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"CONF_DIR": directory}):
            source = Path(directory) / "build_targets.conf"
            for content in ("", "enabled.mac=ON\n", "enabled.mac=\n"):
                with self.subTest(content=content):
                    _ = source.write_text(content)
                    with self.assertRaises(MachineSwitchError) as failure:
                        _ = read(Machine.MAC)
                    self.assertIn(str(source), str(failure.exception))
                    self.assertIn("enabled.mac", str(failure.exception))

    def test_supplied_values_do_not_read_the_registry(self) -> None:
        self.assertIs(read(Machine.HANA_AWS, {"enabled.hana-aws": "on"}), MachineSwitch.ON)
        with self.assertRaisesRegex(MachineSwitchError, "enabled.hana-aws"):
            _ = read(Machine.HANA_AWS, {"enabled": "on"})
