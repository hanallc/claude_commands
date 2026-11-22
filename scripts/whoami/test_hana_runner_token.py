"""Check renewal with fake commands and a private credential path."""
from __future__ import annotations

import io
import os
import signal
import stat
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import ClassVar, cast, final, override
from unittest.mock import patch

from scripts.whoami import hana_runner_token


@final
class HanaRunnerTokenTests(unittest.TestCase):
    SECRET: ClassVar[bytes] = b"private-fixture-token"
    SEALED: ClassVar[bytes] = b"sealed fixture credential"
    OLD: ClassVar[bytes] = b"old sealed credential"

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.path = Path()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.commands: list[list[str]] = []
        self.approval_status = 0
        self.read_status = 0
        self.token = self.SECRET
        self.encrypt_status = 0
        self.interrupt_encrypt = False

    @override
    def setUp(self) -> None:
        original_interrupt = signal.signal(signal.SIGINT, signal.default_int_handler)
        original_terminate = signal.getsignal(signal.SIGTERM)
        assert original_interrupt is not None and original_terminate is not None
        self.addCleanup(signal.signal, signal.SIGINT, original_interrupt)
        self.addCleanup(signal.signal, signal.SIGTERM, original_terminate)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = root / "hana-runner-token.cred"
        _ = self.path.write_bytes(self.OLD)
        _ = self.enterContext(redirect_stdout(self.stdout))
        _ = self.enterContext(redirect_stderr(self.stderr))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.run_command))

    def run_command(self, argv: list[str], *, check: bool, stdout: int, stderr: int | None = None,
                    input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        self.assertFalse(check)
        self.assertFalse(any(self.SECRET in word.encode() for word in argv))
        self.assertIsNone(stderr)
        self.assert_original()
        self.commands.append(argv)
        if len(self.commands) == 1:
            self.assertEqual(argv, ["sudo", "-v"])
            self.assertEqual(stdout, subprocess.DEVNULL)
            self.assertIsNone(input)
            return subprocess.CompletedProcess(argv, self.approval_status)

        self.assertEqual(stdout, subprocess.PIPE)
        if len(self.commands) == 2:
            self.assertEqual(argv, ["op", "read", "--no-newline",
                                    "op://hana/natedev - hana self-hosted ci runner token/credential"])
            self.assertIsNone(input)
            return subprocess.CompletedProcess(argv, self.read_status, self.token)

        self.assertEqual(len(self.commands), 3)
        self.assertEqual(argv, ["sudo", "-n", "systemd-creds", "encrypt", "--with-key=host+tpm2",
                                "--tpm2-pcrs=", "--name=hana-runner-token", "-", "-"])
        self.assertTrue(input == self.SECRET, "Encryption must receive the fixture on stdin.")
        if self.interrupt_encrypt:
            os.kill(os.getpid(), signal.SIGINT)
        return subprocess.CompletedProcess(argv, self.encrypt_status, self.SEALED)

    def assert_original(self) -> None:
        self.assertEqual(self.path.read_bytes(), self.OLD)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def reseal(self) -> int:
        code = hana_runner_token.main(["reseal"], path=self.path)
        self.assertFalse(self.SECRET in self.stdout.getvalue().encode())
        self.assertFalse(self.SECRET in self.stderr.getvalue().encode())
        return code

    def assert_failure(self, steps: int, reason: str, code: int = 1) -> None:
        self.assertEqual(self.reseal(), code)
        self.assertEqual(len(self.commands), steps)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), reason + "\n")
        self.assert_original()

    def assert_replaced(self) -> None:
        self.assertEqual(len(self.commands), 3)
        self.assertEqual(self.path.read_bytes(), self.SEALED)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertEqual(self.stdout.getvalue(), f"Replaced {self.path}.\n")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_success_replaces_the_credential(self) -> None:
        self.assertEqual(self.reseal(), 0)
        self.assert_replaced()

    def test_failed_sudo_creates_nothing(self) -> None:
        self.approval_status = 1
        self.assert_failure(1, "Sudo authentication failed.")

    def test_failed_read_creates_nothing(self) -> None:
        self.read_status = 1
        self.assert_failure(2, "Cannot read the runner token from 1Password.")

    def test_empty_value_creates_nothing(self) -> None:
        self.token = b""
        self.assert_failure(2, "1Password returned an empty runner token.")

    def test_failed_encryption_creates_nothing(self) -> None:
        self.encrypt_status = 1
        self.assert_failure(3, "Cannot seal the runner credential.")

    def test_interrupt_during_encryption_creates_nothing(self) -> None:
        self.interrupt_encrypt = True
        self.assert_failure(3, "Reseal interrupted.", 130)

    def interrupt_publication(self) -> None:
        os.kill(os.getpid(), signal.SIGINT)
        self.assertIn(signal.SIGINT, cast(set[signal.Signals], signal.sigpending()))

    def test_interrupt_during_temporary_file_creation_allows_publication(self) -> None:
        create = tempfile.NamedTemporaryFile

        def interrupted_create(*, dir: Path, prefix: str, suffix: str, delete: bool):
            output = create(dir=dir, prefix=prefix, suffix=suffix, delete=delete)
            self.interrupt_publication()
            return output

        with patch.object(tempfile, "NamedTemporaryFile", side_effect=interrupted_create):
            self.assertEqual(self.reseal(), 0)
        self.assert_replaced()

    def patch_temporary_write(self, before_write: Callable[[], None]) -> None:
        create = tempfile.NamedTemporaryFile

        def intercepted_create(*, dir: Path, prefix: str, suffix: str, delete: bool):
            output = create(dir=dir, prefix=prefix, suffix=suffix, delete=delete)
            write = output.write

            def intercepted_write(sealed: bytes) -> int:
                before_write()
                return write(sealed)

            _ = self.enterContext(patch.object(output, "write", side_effect=intercepted_write))
            return output

        _ = self.enterContext(patch.object(tempfile, "NamedTemporaryFile", side_effect=intercepted_create))

    def test_interrupt_during_write_allows_publication(self) -> None:
        self.patch_temporary_write(self.interrupt_publication)
        self.assertEqual(self.reseal(), 0)
        self.assert_replaced()

    def test_interrupt_after_replacement_keeps_the_success_status(self) -> None:
        replace = os.replace

        def interrupted_replace(temporary: Path, destination: Path) -> None:
            replace(temporary, destination)
            self.interrupt_publication()

        with patch.object(os, "replace", side_effect=interrupted_replace):
            self.assertEqual(self.reseal(), 0)
        self.assert_replaced()

    def test_interrupt_during_cleanup_keeps_the_write_failure_status(self) -> None:
        unlink = Path.unlink

        def fail_write() -> None:
            raise OSError("fixture write failure")

        def interrupted_unlink(temporary: Path, *, missing_ok: bool = False) -> None:
            self.interrupt_publication()
            unlink(temporary, missing_ok=missing_ok)

        self.patch_temporary_write(fail_write)
        with patch.object(Path, "unlink", new=interrupted_unlink):
            self.assert_failure(3, "Cannot run the reseal command or write the sealed credential.")

    def test_replacement_has_owner_only_mode_under_a_restrictive_umask(self) -> None:
        previous = os.umask(0o777)
        try:
            self.assertEqual(self.reseal(), 0)
        finally:
            _ = os.umask(previous)
        self.assert_replaced()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_failed_replacement_removes_the_temporary_file(self) -> None:
        with patch.object(os, "replace", side_effect=OSError("fixture failure")):
            self.assert_failure(3, "Cannot run the reseal command or write the sealed credential.")
