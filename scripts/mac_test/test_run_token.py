"""Test the Mac token scan and shell protocol on a local fixture."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import ctypes
from dataclasses import replace
from functools import partial
from io import StringIO
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import ClassVar, cast, override
import unittest
from unittest import mock

from scripts.buildlog import record
from scripts.mac_test import claim_state, eligibility, offload, run_token
from scripts.mac_test.offload_test_commands import write_shell, write_token_reader
from scripts.message.darwin_test_processes import ProcessIdentityInfo, ProcessStateInfo
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.whoami import account


IDENTITY = claim_state.ClaimIdentity("claim", "episode", "attempt")
TOKEN = run_token.token(IDENTITY)


def process(pid: int, *, token: str = "", command: str = "fixture") -> run_token.Process:
    return run_token.Process(run_token.ProcessIdentity(pid, pid + 1000, 1),
                             frozenset({token}) if token else frozenset(), command)


class ProcessTableTests(unittest.TestCase):
    def test_kill_leaves_a_path_argument_and_mirror_cwd_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temp, ExitStack() as resources:
            mirror = Path(temp) / "mirror/hana"
            mirror.mkdir(parents=True)
            environment = os.environ.copy()
            _ = environment.pop(run_token.TOKEN_NAME, None)
            stdin_reader = "import sys; sys.stdin.buffer.read()"
            user_command = resources.enter_context(subprocess.Popen(
                [sys.executable, "-c", stdin_reader, str(mirror / "target/log")],
                stdin=subprocess.PIPE, env=environment,
            ))
            user_shell = resources.enter_context(subprocess.Popen(
                ["/bin/sh", "-c", "read -r answer"], cwd=mirror, stdin=subprocess.PIPE, env=environment,
            ))
            argv_entry = process(user_command.pid, command=f"tail {mirror}/target/log")
            cwd_entry = process(user_shell.pid, command="sh")
            different = process(14, token=TOKEN + "-later")
            owned = process(15, token=TOKEN, command="cargo test")
            live = {item.identity: item for item in (argv_entry, cwd_entry, different, owned)}
            output: list[str] = []

            def send(identity: run_token.ProcessIdentity, signum: int) -> bool:
                self.assertEqual(signum, signal.SIGTERM)
                self.assertEqual(identity, owned.identity)
                del live[identity]
                return True

            self.assertEqual(run_token.report("kill", [[TOKEN]], read=lambda: run_token.ProcessTable(tuple(live.values())),
                                             send=send, pause=lambda seconds: self.assertEqual(seconds, 1),
                                             write=output.append), 0)
            self.assertEqual(list(live.values()), [argv_entry, cwd_entry, different])
            self.assertIsNone(user_command.poll())
            self.assertIsNone(user_shell.poll())
            self.assertEqual(output, ["leftover_killed pid=15 command=cargo test"])

    def test_unreadable_process_has_no_signal_and_keeps_check_unconfirmed(self) -> None:
        owned = process(20, token=TOKEN)
        table = iter((run_token.ProcessTable((owned,), frozenset({21})),
                      run_token.ProcessTable((), frozenset({21}))))
        output: list[str] = []

        def send(identity: run_token.ProcessIdentity, signum: int) -> bool:
            self.assertEqual((identity, signum), (owned.identity, signal.SIGTERM))
            return True

        status = run_token.report("kill", [[TOKEN]], read=lambda: next(table), send=send,
                                  pause=lambda seconds: self.assertEqual(seconds, 1), write=output.append)
        self.assertEqual(status, 1)
        self.assertEqual(output, ["leftover_killed pid=20 command=fixture"])

    def test_later_scan_failure_keeps_the_confirmed_kill_line(self) -> None:
        ended = process(20, token=TOKEN, command="cargo test")
        remaining = process(21, token=TOKEN)
        scans = iter((run_token.ProcessTable((ended, remaining)), run_token.ProcessTable((remaining,))))
        signals: list[tuple[run_token.ProcessIdentity, int]] = []
        output = StringIO()
        errors = StringIO()
        kill_line = "leftover_killed pid=20 command=cargo test\n"

        def read() -> run_token.ProcessTable:
            table = next(scans, None)
            if table is None:
                self.assertEqual(output.getvalue(), kill_line)
                raise OSError("the final scan failed")
            return table

        def send(identity: run_token.ProcessIdentity, signum: int) -> bool:
            signals.append((identity, signum))
            return True

        report = partial(run_token.report, read=read, send=send,
                         pause=lambda seconds: self.assertEqual(seconds, 1))
        with (mock.patch.object(run_token, "report", report),
              mock.patch.object(sys, "argv", ["run_token", "kill", f'[["{TOKEN}"]]']),
              redirect_stdout(output), redirect_stderr(errors)):
            self.assertEqual(run_token.main(), 1)
        self.assertEqual(output.getvalue(), kill_line)
        self.assertIn("the final scan failed", errors.getvalue())
        self.assertEqual(signals, [(ended.identity, signal.SIGTERM), (remaining.identity, signal.SIGTERM),
                                   (remaining.identity, signal.SIGKILL)])

    def test_kernel_scan_finds_exact_tokens_after_a_pfz_variable(self) -> None:
        marker = f"MAC_RUN_TOKEN={TOKEN}".encode()
        record_bytes = (2).to_bytes(4, sys.byteorder) + b"/bin/tail\0" + bytes(6) + b"tail\0" + marker + b"\0"

        def info(pid: int, _kind: int, value: ctypes.Structure) -> bool:
            if isinstance(value, ProcessStateInfo):
                value.flags = run_token.PROC_FLAG_LP64
                value.status = run_token.SZOMB if pid == 16 else 1
                value.real_uid = os.getuid() + 1 if pid == 13 else os.getuid()
                value.uid = os.geteuid()
            else:
                assert isinstance(value, ProcessIdentityInfo)
                value.unique_id, value.version = pid + 1000, 1
            return True

        def read_args(pid: int) -> bytes:
            if pid == 14:
                raise PermissionError("fixture denies the environment")
            entries = b"pfz=fixture\0" + marker + b"\0MAC_RUN_TOKEN=other/run\0" if pid == 15 else b""
            return record_bytes + entries + b"pfz=0x0\0stack_guard=0x0\0"

        with (mock.patch.object(run_token, "_user_pids", return_value=[12, 13, 14, 15, 16]),
              mock.patch.object(run_token, "_process_info", side_effect=info),
              mock.patch.object(account, "kern_procargs2", side_effect=read_args) as read):
            observed = run_token.mac_processes()
        self.assertEqual(read.call_args_list, [mock.call(12), mock.call(14), mock.call(15)])
        self.assertEqual(observed.unreadable_pids, frozenset({14}))
        self.assertEqual([item.identity.pid for item in observed.select([TOKEN])], [15])
        self.assertEqual([item.identity.pid for item in observed.select(["other/run"])], [15])

    def test_empty_argv_zero_keeps_cleanup_unconfirmed_even_with_a_token(self) -> None:
        for flags, padding in ((0, 1), (run_token.PROC_FLAG_LP64, 5)):
            with self.subTest(flags=flags):
                argv = (2).to_bytes(4, sys.byteorder) + b"/bin/cargo\0" + bytes(padding) + b"\0nextest\0"
                payload = argv + f"MAC_RUN_TOKEN={TOKEN}\0OTHER=fixture\0".encode()
                with mock.patch.object(account, "kern_procargs2", return_value=payload):
                    self.assert_unconfirmed_cleanup(flags=flags)

    def test_truncated_empty_argv_zero_reply_keeps_cleanup_unconfirmed(self) -> None:
        argv = (2).to_bytes(4, sys.byteorder) + b"/bin/cargo\0" + bytes(5) + b"\0nextest\0"
        # XNU can include HOME and omit the token that follows it.
        with mock.patch.object(account, "kern_procargs2", return_value=argv + b"HOME=/Users/fixture\0"):
            self.assert_unconfirmed_cleanup()

    def test_full_kernel_reply_with_cut_argv_zero_keeps_cleanup_unconfirmed(self) -> None:
        def fill_reply(_query: object, _length: int, buffer: ctypes.Array[ctypes.c_char],
                       _size: object, _new: object, _new_length: int) -> int:
            prefix = (1).to_bytes(4, sys.byteorder) + f"ure1234\0MAC_RUN_TOKEN={TOKEN}\0FILL=".encode()
            payload = prefix.ljust(ctypes.sizeof(buffer) - 1, b"x") + b"\0"
            _ = ctypes.memmove(buffer, payload, len(payload))
            return 0

        sysctl = mock.Mock(side_effect=fill_reply)
        with mock.patch.object(ctypes, "CDLL", return_value=SimpleNamespace(sysctl=sysctl)):
            self.assert_unconfirmed_cleanup()
        sysctl.assert_called_once()

    def test_withheld_or_empty_environment_keeps_cleanup_unconfirmed(self) -> None:
        argv = (1).to_bytes(4, sys.byteorder) + b"/bin/cargo\0" + bytes(5) + b"cargo\0"
        for tail in (b"", b"\0\0"):
            with (self.subTest(tail=tail),
                  mock.patch.object(account, "kern_procargs2", return_value=argv + tail)):
                self.assert_unconfirmed_cleanup()

    def assert_unconfirmed_cleanup(self, *, flags: int = run_token.PROC_FLAG_LP64) -> None:
        def info(_pid: int, _kind: int, value: ctypes.Structure) -> bool:
            if isinstance(value, ProcessStateInfo):
                value.flags = flags
                value.status, value.real_uid, value.uid = 1, os.getuid(), os.geteuid()
            return True

        with (mock.patch.object(run_token, "_user_pids", return_value=[20]),
              mock.patch.object(run_token, "_process_info", side_effect=info),
              mock.patch.object(run_token, "signal_process", return_value=False) as send,
              mock.patch("builtins.print") as write):
            self.assertEqual(run_token.report("kill", [[TOKEN]], send=send, pause=mock.Mock(), write=write), 1)
            send.assert_not_called()
            write.assert_not_called()


class RunTokenShellTests(unittest.TestCase):
    root: Path = Path()
    repo: ClassVar[eligibility.KnownRepository] = eligibility.KnownRepository("hana")

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, config_environment(self.root))))
        _ = write_config_fixture(self.root / "mac_test.conf")
        _ = write_config_fixture(self.root / "build_targets.conf", "platform.mac=macos\n")

    @property
    def config(self) -> eligibility.OffloadConfig:
        return eligibility.read_config(self.root / "mac_test.conf")

    @property
    def request(self) -> offload.OffloadRequest:
        return offload.OffloadRequest(self.root, "hana", "call", offload.NO_RESULT_FILE, (),
                                      owner="verify", holder_session="session")

    @property
    def environment(self) -> dict[str, str]:
        return {**os.environ, "HOME": str(self.root), "TMPDIR": str(self.root), "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(Path(__file__).resolve().parents[2])}

    def run_shell(self, text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/bin/sh", "-c", text], cwd=self.root, env=self.environment,
                              capture_output=True, text=True, timeout=5, check=False)

    def launch_prefix(self) -> str:
        script = offload.remote_test_command(self.config, self.request, self.repo, identity=IDENTITY)
        return script.split("\ncd ", 1)[0] + "\n"

    def test_export_reaches_both_pipeline_processes(self) -> None:
        text = self.launch_prefix() + "sh -c 'printenv MAC_RUN_TOKEN' | sh -c 'cat; printenv MAC_RUN_TOKEN'\n"
        result = self.run_shell(text)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, f"{TOKEN}\n{TOKEN}\n", ""))

    def test_sccache_startup_receives_no_run_token(self) -> None:
        sccache = self.root / ".cargo/bin/sccache"
        sccache.parent.mkdir(parents=True)
        write_shell(sccache, r'''
[[ ! -v MAC_RUN_TOKEN && $2 == 'compiler argument' ]] || exit 2
sh -c 'test "${MAC_RUN_TOKEN+x}" != x && echo "$1"' sh "$1"
''')
        lint = self.root / ".claude/scripts/lint/lint"
        lint.parent.mkdir(parents=True)
        write_shell(lint, r'''
printenv MAC_RUN_TOKEN &&
sccache first 'compiler argument' &&
sccache replacement 'compiler argument'
''')
        (self.root / offload.remote_mirror(self.repo)).mkdir(parents=True)
        request = replace(self.request, verb="check", nextest_words=("--target-dir=fixture",))
        with mock.patch.object(offload, "remote_cleanup_command", return_value=":"):
            command = offload.remote_test_command(self.config, request, self.repo, identity=IDENTITY)
        result = self.run_shell(command)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, f"{TOKEN}\nfirst\nreplacement\nmac_exit=0\n", ""))
        self.assertEqual(list(self.root.glob("mac-sccache.*")), [])

    def test_sccache_wrapper_stays_absolute_with_relative_tmpdir(self) -> None:
        temporary = self.root / "relative temp"
        temporary.mkdir()
        (self.root / "mirror").mkdir()
        command = "TMPDIR='relative temp'\n" + self.launch_prefix()
        command += 'cd mirror && test -x "$mac_sccache_dir/sccache" && printf "%s\\n" "$mac_sccache_dir"\n'
        result = self.run_shell(command)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        wrapper_dir = Path(result.stdout.strip())
        self.assertEqual(wrapper_dir.parent, temporary)
        self.assertFalse(wrapper_dir.exists())
        self.assertEqual(list(temporary.iterdir()), [])

    def test_launch_refuses_ambiguous_run_token_components(self) -> None:
        ambiguous = (claim_state.ClaimIdentity("first", "a/b", "c"),
                     claim_state.ClaimIdentity("second", "a", "b/c"))
        for identity, field in zip(ambiguous, ("episode", "attempt"), strict=True):
            with self.subTest(field=field):
                self.assertEqual(f"{identity.episode}/{identity.attempt}", "a/b/c")
                message = f"Mac run token {field} cannot contain"
                with self.assertRaisesRegex(ValueError, message):
                    _ = offload.remote_test_command(self.config, self.request, self.repo, identity=identity)
                result = self.run_shell(offload.leftover_check_script(self.repo, identity=identity))
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertIn(message, result.stderr)
        for character in ("=", "\0", " ", "\t", "\n"):
            with self.subTest(character=character):
                for identity in (replace(IDENTITY, episode=character), replace(IDENTITY, attempt=character)):
                    with self.assertRaises(run_token.InvalidRunToken):
                        _ = run_token.token(identity)

    def test_forwarded_environment_cannot_replace_the_claim_token(self) -> None:
        request = replace(self.request, environment=((run_token.TOKEN_NAME, "other/attempt"),))
        with self.assertRaisesRegex(ValueError, "sets MAC_RUN_TOKEN from the claim"):
            _ = offload.remote_test_command(self.config, request, self.repo, identity=IDENTITY)

    def test_cleanup_keeps_the_kill_lines_with_the_attempt_result(self) -> None:
        line = "leftover_killed pid=20 command=cargo nextest"
        script = offload.leftover_check_script(self.repo, identity=IDENTITY)
        for status in (0, 1):
            with self.subTest(status=status):
                write_token_reader(self.root, f'''[[ $3 == kill && $4 == '[["{TOKEN}"]]' ]] || exit 99
printf '%s\\n' {shlex.quote(line)}
exit {status}
''')
                result = self.run_shell(script)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(result.stdout, line + "\n" + ("leftovers=0\nrc=0\n" if status == 0 else ""))
                with mock.patch.object(subprocess, "run", return_value=result):
                    observed = offload.read_leftovers(self.config.connection, "mac", script, "mac")
                self.assertIsInstance(observed.result, offload.LeftoversEnded if status == 0 else offload.LeftoverCheckFailed)
                evidence = offload.AttemptEvidence(repository_key="fixture")
                offload.observe_cleanup(evidence, observed.result)
                with mock.patch.object(record, "ssh_attempt") as append:
                    request = replace(self.request, target="mac")
                    offload.persist_attempt(request, offload.LostMacRun(1, "fixture"), evidence, 0)
                self.assertEqual(append.call_args.args[0]["leftover_killed"], [line])
                self.assertEqual(append.call_args.args[0]["cleanup"], "confirmed" if status == 0 else "unconfirmed")

    def test_cleanup_timeout_records_only_complete_kill_lines(self) -> None:
        line = "leftover_killed pid=20 command=cargo nextest"
        complete = f"{line}\r\nleftovers=0\nrc=0\n"
        partial = complete + "leftover_killed pid=21 command=comp"
        for output in (partial.encode(), partial):
            with self.subTest(output_type=type(output)):
                error = subprocess.TimeoutExpired("fixture", 1, output=output)
                with mock.patch.object(subprocess, "run", side_effect=error):
                    observed = offload.read_leftovers(self.config.connection, "mac", "fixture", "mac")
                self.assertIsInstance(observed.result, offload.LeftoverCheckFailed)
                self.assertEqual(observed.output, "")
                self.assertEqual(observed.result.killed, (line,))
                evidence = offload.AttemptEvidence(repository_key="fixture")
                offload.observe_cleanup(evidence, observed.result)
                with mock.patch.object(record, "ssh_attempt") as append:
                    request = replace(self.request, target="mac")
                    offload.persist_attempt(request, offload.LostMacRun(1, "fixture"), evidence, 0)
                attempt = cast(dict[str, object], append.call_args.args[0])
                self.assertEqual((attempt["cleanup"], attempt["unresolved_count"], attempt["leftover_killed"]),
                                 ("unconfirmed", None, [line]))
