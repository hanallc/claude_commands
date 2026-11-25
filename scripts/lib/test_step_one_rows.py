"""Check configuration recovery, process reads, and file publication."""
from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from scripts.lib import atomic_file, conf, process_start


class StepOneRows(unittest.TestCase):
    def test_unavailable_key_raises(self) -> None:
        """Configuration reading and recovery row 8."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "example.conf"
            _ = path.write_text("amount=bad\n")
            with (mock.patch.object(conf, "_committed_files", return_value=()) as history,
                  redirect_stderr(StringIO()), self.assertRaisesRegex(conf.Unusable, "no usable committed value")):
                _ = conf.read(path).whole("amount")
            history.assert_called_once_with(path)

    def test_unreadable_map_recovers_the_whole_file(self) -> None:
        """Configuration reading and recovery row 9."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.conf"
            _ = path.write_bytes(b"\xff")
            with (mock.patch.object(conf, "_repository_path") as repository,
                  mock.patch.object(conf, "_committed_files", return_value=("first=1\nsecond=2\n",)) as history,
                  redirect_stderr(StringIO())):
                self.assertEqual(conf.read(path).values(), {"first": "1", "second": "2"})
            repository.assert_not_called()
            history.assert_called_once_with(path)

    def test_missing_named_map_uses_repository_before_history(self) -> None:
        """Configuration reading and recovery row 10."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "example.conf"
            repository = path.with_name("repository.conf")
            _ = repository.write_text("amount=42\n")
            with (mock.patch.dict(os.environ), mock.patch.object(conf, "directory", return_value=path.parent),
                  mock.patch.object(conf, "_repository_path", return_value=repository) as resolve,
                  mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read,
                  mock.patch.object(conf, "_committed_files") as history, redirect_stderr(StringIO()) as warning):
                _ = os.environ.pop("CONF_DIR", None)
                self.assertEqual(conf.read("example").values(), {"amount": "42"})
            resolve.assert_called_once_with("example")
            self.assertEqual(read.call_args_list, [
                mock.call(path, encoding="utf-8"), mock.call(repository, encoding="utf-8")])
            history.assert_not_called()
            self.assertEqual(warning.getvalue(), "")

    def test_process_deadline_and_invalid_pid_skip_reads(self) -> None:
        """Run identity and process helpers row 13."""
        with (mock.patch.object(time, "monotonic", return_value=10),
              mock.patch.object(Path, "read_bytes") as read,
              mock.patch.object(subprocess, "run") as run):
            self.assertEqual(process_start.process_start(42, budget=process_start.ProcessReadDeadline(1)),
                             process_start.Unreadable("the process read reached its deadline"))
            for platform in ("darwin", "linux"):
                with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                    self.assertEqual(process_start.process_start(0), process_start.Unreadable("invalid process id"))
        read.assert_not_called()
        run.assert_not_called()

    def test_macos_process_read_decodes_ps_rows(self) -> None:
        """Run identity and process helpers row 14."""
        started = "Mon Nov  2 09:10:11 2026"
        for code, output, expected in (
            (0, f"{started} S\n", process_start.Started(started)),
            (0, f"{started} Z\n", process_start.Zombie(started)),
            (1, "", process_start.Gone()),
            (0, "bad output\n", process_start.Unreadable("invalid ps process identity")),
        ):
            with (self.subTest(output=output), mock.patch.object(sys, "platform", "darwin"),
                  mock.patch.object(time, "monotonic", return_value=10),
                  mock.patch.object(subprocess, "run",
                                    return_value=subprocess.CompletedProcess([], code, output, "")) as run):
                self.assertEqual(process_start.process_start(42, budget=process_start.ProcessReadDeadline(12)),
                                 expected)
            run.assert_called_once_with(["ps", "-p", "42", "-o", "lstart=", "-o", "stat="],
                                        capture_output=True, text=True, timeout=2, check=False,
                                        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})

    def test_linux_process_read_uses_the_selected_proc_root(self) -> None:
        """Run identity and process helpers row 15."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "42/stat"
            path.parent.mkdir()
            for state, expected in (("S", process_start.Started("567")), ("Z", process_start.Zombie("567")),
                                    ("?", process_start.Unreadable("invalid process stat fields"))):
                _ = path.write_bytes(f"42 (name with ) ) {state} {'0 ' * 18}567\n".encode())
                with (mock.patch.dict(os.environ, {"VERIFY_PROC_ROOT": temporary}),
                      mock.patch.object(sys, "platform", "linux"),
                      mock.patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as read):
                    self.assertEqual(process_start.process_start(42), expected)
                    self.assertEqual(process_start.process_start(43), process_start.Gone())
                self.assertEqual(read.call_args_list, [mock.call(path), mock.call(path.parent.parent / "43/stat")])

    def test_default_publication_mode_uses_an_unlinked_probe(self) -> None:
        """Atomic publication helper row 4."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result"
            measured_modes: list[int] = []

            def measure(descriptor: int) -> os.stat_result:
                info = real_fstat(descriptor)
                self.assertEqual(info.st_nlink, 0)
                measured_modes.append(info.st_mode & 0o777)
                return info

            real_fstat = os.fstat
            with mock.patch.object(os, "fstat", side_effect=measure) as read:
                atomic_file.write_text_atomic(path, "complete")
            read.assert_called_once()
            self.assertEqual(path.stat().st_mode & 0o777, measured_modes[0])
            self.assertEqual(path.read_text(), "complete")
            self.assertEqual(list(path.parent.iterdir()), [path])
