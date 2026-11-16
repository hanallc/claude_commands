"""Check remote cleanup status through a fake SSH connection."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import cast
from unittest import mock

from scripts.lib import conf
from scripts.build_queue import admission_records
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import loopback_proof


REMOTE_SCRATCH = '''import json, os, sys, types
from pathlib import Path
scratch = types.ModuleType("scripts.lib.scratch")
def main(arguments):
    Path(os.environ["CLEANUP_ARGUMENTS"]).write_text(json.dumps(arguments))
    status = int(os.environ["CLEANUP_STATUS"])
    if status:
        print("scratch: kept " + os.environ["FAKE_SCRATCH_HOME"] + ": busy", file=sys.stderr)
    return status
scratch.main = main
scratch.end = lambda task: []
lib = types.ModuleType("scripts.lib")
lib.scratch = scratch
sys.modules["scripts"] = types.ModuleType("scripts")
sys.modules["scripts.lib"] = lib
sys.modules["scripts.lib.scratch"] = scratch
'''


class LoopbackCleanupTests(unittest.TestCase):
    def check_cleanup(self, cleanup_status: int) -> tuple[int, str, str, Path]:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        home = root / "home"
        home.mkdir()
        package_root = home / ".claude/scripts/lib/package_root.py"
        package_root.parent.mkdir(parents=True)
        _ = package_root.write_text(REMOTE_SCRATCH)
        arguments_file = root / "cleanup-arguments.json"
        remote_home = root / "remote-home"
        environment = {**os.environ, "HOME": str(home), "CLEANUP_STATUS": str(cleanup_status),
                       "CLEANUP_ARGUMENTS": str(arguments_file), "FAKE_SCRATCH_HOME": str(remote_home)}
        targets = root / "build_targets.conf"
        _ = write_config_fixture(targets, "loopback.memory_gib=4\nloopback.host=fake-ssh\n")
        mac = root / "mac_test.conf"
        _ = write_config_fixture(mac)
        configurations = [conf.read(targets), conf.read(mac)]
        memory = mock.Mock(program_first_peaks={"cargo": 1}, policy=mock.Mock(safety_bytes=1))
        run = subprocess.run

        def fake_ssh(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "git":
                return subprocess.CompletedProcess(command, 0, "scripts/lib/scratch.py\0", "")
            if command[0] == "rsync":
                return subprocess.CompletedProcess(command, 0, "", "")
            self.assertEqual(command[0], "ssh")
            remote = shlex.split(command[-1])
            if remote[0] == "mkdir":
                return subprocess.CompletedProcess(command, 0, "", "")
            self.assertEqual(remote[:2], ["python3", "-c"])
            if "scratch.new(" in remote[2]:
                return subprocess.CompletedProcess(command, 0, str(remote_home) + "\n", "")
            if remote[2] == loopback_proof.REMOTE_PROOF:
                return subprocess.CompletedProcess(command, 0, "", "")
            result = run([sys.executable, *remote[1:]], env=environment,
                         text=True, capture_output=True, check=False)
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            return result

        output, error = io.StringIO(), io.StringIO()
        with (mock.patch.object(conf, "read", side_effect=configurations),
              mock.patch.object(admission_records, "read_memory_config", return_value=memory),
              mock.patch.object(subprocess, "run", side_effect=fake_ssh),
              redirect_stdout(output), redirect_stderr(error)):
            code = loopback_proof.main()
        self.assertTrue(arguments_file.is_file(), "The remote command must call scratch.main().")
        arguments = cast(list[str], json.loads(arguments_file.read_text()))
        self.assertEqual(arguments[:2], ["end", "--task"])
        self.assertTrue(arguments[2].startswith("gate:linux-loopback-"))
        return code, output.getvalue(), error.getvalue(), remote_home

    def test_cleanup_reports_a_removed_remote_home(self) -> None:
        code, output, error, home = self.check_cleanup(0)
        self.assertEqual((code, error), (0, ""))
        self.assertIn(f"removed the managed scratch home {home}", output)

    def test_cleanup_reports_a_retained_remote_home(self) -> None:
        code, output, error, home = self.check_cleanup(1)
        self.assertNotEqual(code, 0)
        self.assertNotIn("removed the managed scratch home", output)
        self.assertIn(f"scratch: kept {home}: busy", error)
        self.assertIn(f"loopback: kept the managed scratch home {home}", error)


if __name__ == "__main__":
    _ = unittest.main()
