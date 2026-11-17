"""Check root supervision settings without a guest compiler or source command."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from typing import cast
import unittest
from unittest import mock

from scripts.aws_target import hana_execution, hana_receiver, hana_runner
from scripts.lint.memory_test_support import write_config_fixture


class HanaReceiverSettingsTests(unittest.TestCase):
    def test_source_supervision_uses_the_shared_deadline_without_a_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _ = write_config_fixture(directory / 'build_targets.conf', 'loopback.command_timeout_s=23\n')
            child = mock.Mock(pid=os.getpid())
            with (mock.patch.dict(os.environ, {'CONF_DIR': str(directory)}),
                  mock.patch.object(hana_execution, 'encode_request', return_value=b'{}'),
                  mock.patch.object(hana_runner, 'read_image_compiler_version', side_effect=AssertionError),
                  mock.patch.object(subprocess, 'Popen', return_value=child) as launch):
                hana_receiver.GuestCommandSupervisor(10).start_source_update(
                    cast(hana_execution.HanaExecutionRequest, mock.sentinel.request),
                    directory, ['systemd-run', '--', 'runner'])
            arguments = cast(list[str], launch.call_args.args[0])
            self.assertIn('--property=RuntimeMaxSec=23', arguments)
            self.assertIn('--property=EnvironmentFile=/etc/hana-aws/runner-environment', arguments)


if __name__ == '__main__':
    _ = unittest.main()
