"""Check request isolation and recovery through real helper processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
from typing import BinaryIO, cast, override
import unittest

from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib import process_start


DIRECT = Path(__file__).resolve().parent


class VerifyHelperTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.program: Path = self.root / "fixture.py"

    @override
    def setUp(self) -> None:
        _ = self.program.write_text('''import json, os, signal, sys
from pathlib import Path
with Path(__file__).with_suffix('.imports').open('a') as target:
    target.write('imported\\n')
def main():
    if sys.argv[1:2] == ['trap']:
        os.kill(int(sys.argv[2]), signal.SIGUSR1)
        print(os.getpid())
        return 0
    if sys.argv[1:] == ['pid']:
        print(os.getpid())
        return 0
    print(json.dumps(dict(value=os.environ.get('VALUE'), leak=os.environ.get('LEAK'),
                          stdin=sys.stdin.read(), cwd=os.getcwd(), args=sys.argv[1:], pid=os.getpid())))
    print('fixture diagnostic', file=sys.stderr)
    os.environ['LEAK'] = 'first request'
    os.chdir('/')
    if sys.argv[1:] == ['exit']:
        raise SystemExit(7)
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
''')

    def start_helper(self) -> subprocess.Popen[bytes]:
        process = subprocess.Popen([sys.executable, str(DIRECT / "verify_helper.py")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_helper, process)
        return process

    def stop_helper(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def encode(self, value: str, text: str, *arguments: str, directory: Path | None = None) -> bytes:
        environment = {**os.environ, "VALUE": value}
        _ = environment.pop("LEAK", None)
        fields = [str(self.program), str(directory or self.root), str(len(arguments)), *arguments,
                  *(f"{name}={value}" for name, value in environment.items()), "", text, ""]
        return b"\0".join(os.fsencode(value) for value in fields) + b"\0"

    def request(self, process: subprocess.Popen[bytes], value: str, text: str,
                *arguments: str, directory: Path | None = None) -> tuple[int, dict[str, object], str]:
        assert process.stdin is not None
        _ = process.stdin.write(self.encode(value, text, *arguments, directory=directory))
        process.stdin.flush()
        return self.reply(process)

    def reply(self, process: subprocess.Popen[bytes]) -> tuple[int, dict[str, object], str]:
        assert process.stdout is not None
        replies = list(map(self.read_field, (cast(BinaryIO, process.stdout),) * 3))
        decoded = cast(dict[str, object], json.loads(replies[1]))
        return int(replies[0]), decoded, replies[2]

    def read_field(self, source: BinaryIO) -> str:
        value = bytearray()
        while byte := source.read(1):
            if byte == b"\0":
                return os.fsdecode(bytes(value))
            value.extend(byte)
        self.fail("the helper closed its reply before the field ended")

    def test_calls_keep_their_environment_stdin_directory_and_arguments(self) -> None:
        process = self.start_helper()
        second = self.root / "second"
        second.mkdir()
        for value, text, directory, arguments in (("first", "one\n\n", self.root, ["one"]),
                                                   ("second", "two", second, ["two", ""])):
            with self.subTest(value=value):
                status, output, errors = self.request(process, value, text, *arguments, directory=directory)
                self.assertEqual(status, 0)
                self.assertEqual(output, dict(value=value, leak=None, stdin=text, cwd=str(directory),
                                              args=arguments, pid=process.pid))
                self.assertEqual(errors, "fixture diagnostic\n")
        self.assertEqual(self.program.with_suffix(".imports").read_text(), "imported\n")

    def test_two_requests_in_one_write_each_get_a_reply(self) -> None:
        process = self.start_helper()
        assert process.stdin is not None
        _ = process.stdin.write(self.encode("first", "one", "one") + self.encode("second", "two", "two"))
        process.stdin.flush()
        for value, text in (("first", "one"), ("second", "two")):
            with self.subTest(value=value):
                status, output, _ = self.reply(process)
                self.assertEqual(status, 0)
                self.assertEqual((output["value"], output["stdin"], output["args"]), (value, text, [text]))

    def test_nonzero_exit_keeps_its_status_and_output(self) -> None:
        process = self.start_helper()
        status, output, errors = self.request(process, "failure", "input", "exit")
        self.assertEqual(status, 7)
        self.assertEqual(output["stdin"], "input")
        self.assertEqual(errors, "fixture diagnostic\n")
        status, output, errors = self.request(process, "next", "next input", "next")
        self.assertEqual((status, output["value"], output["leak"]), (0, "next", None))

    def test_shell_environment_matches_env_for_exported_values(self) -> None:
        _ = self.program.write_text('''import json, os
def main():
    with open(__file__ + ".calls", "a") as calls:
        calls.write(os.environ.get("FIXTURE_EXECUTION", "helper") + "\\n")
    print(json.dumps(dict(os.environ)))
    return 0
if __name__ == "__main__":
    os.environ["FIXTURE_EXECUTION"] = "local"
    raise SystemExit(main())
''')
        script = shell_functions(DIRECT / "verify.sh", "verify_helper") + r'''
VERIFY_START_DIR=$1; PY=$2
verify_helper --start
export FIXTURE_SPACE='one two' FIXTURE_EQUALS='one=two=three'
export FIXTURE_NEWLINES=$'one\ntwo\n' FIXTURE_EMPTY=''
export FIXTURE_UNSET
declare -ax FIXTURE_ARRAY=(one two)
declare -Ax FIXTURE_MAPPING=([one]=two)
env -0 > "$4/environment"
env() { return 91; }
verify_helper "$3"
compgen() { return 1; }
verify_helper "$3" > "$4/fallback-environment"
unset -f compgen
FIXTURE_EXECUTION=next verify_helper "$3" > "$4/next-environment"
'''
        result = subprocess.run(["bash", "-c", script, "helper-test", str(DIRECT), sys.executable,
                                 str(self.program), str(self.root)], capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = dict(entry.split("=", 1) for entry in
                        (self.root / "environment").read_text().split("\0") if entry)
        actual = cast(dict[str, str], json.loads(result.stdout))
        # Bash sets this entry to each external command's path.
        _ = expected.pop("_", None)
        _ = actual.pop("_", None)
        self.assertEqual(actual, expected)
        fallback = cast(dict[str, str], json.loads((self.root / "fallback-environment").read_text()))
        self.assertEqual(fallback.pop("FIXTURE_EXECUTION"), "local")
        _ = fallback.pop("_", None)
        self.assertEqual(fallback, expected)
        following = cast(dict[str, str], json.loads((self.root / "next-environment").read_text()))
        self.assertEqual(following.pop("FIXTURE_EXECUTION"), "next")
        _ = following.pop("_", None)
        self.assertEqual(following, expected)
        self.assertEqual(self.program.with_suffix(".py.calls").read_text(), "helper\nlocal\nnext\n")

    def test_shell_recovers_after_helper_death_and_keeps_shell_fixtures(self) -> None:
        shell = self.root / "shell-fixture"
        _ = shell.write_text('#!/bin/sh\ncase "$1" in TERM|INT|HUP) kill -s "$1" "$$" ;; esac\n'
                             + 'printf "shell:%s\\n" "$1"\nexit 9\n')
        runner = self.root / "runner"
        _ = runner.write_text(f'#!/bin/bash\nif [[ "$1" == "{shell}" ]]; then exec bash "$@"; fi\nexec "{sys.executable}" "$@"\n')
        runner.chmod(0o755)
        record = self.root / "detached.json"
        started = process_start.process_start(os.getpid())
        self.assertIsInstance(started, process_start.Started)
        assert isinstance(started, process_start.Started)
        _ = record.write_text(json.dumps(dict(run_id="helper-test", command=[], status="running",
                                             command_pid=os.getpid(), command_started=started.start)))
        for mode in ("death", "shell", "TERM", "INT", "HUP", "parent-exit", "forwarded-empty", "trap", "record-exit"):
            with self.subTest(mode=mode):
                if mode == "parent-exit" and sys.platform != "linux":
                    continue
                script = shell_functions(DIRECT / "verify.sh", "verify_helper") + '''
VERIFY_START_DIR=$1; PY=$2
verify_helper --start
if [[ "$3" == death ]]; then
    helper_pid=$(verify_helper "$4" pid)
    kill -KILL "$helper_pid"
    verify_helper --stdin "$4" recovered <<< 'restored input'
elif [[ "$3" == parent-exit ]]; then
    helper_pid=$(verify_helper "$4" pid)
    sleep "$6" <&"$VERIFY_HELPER_OUTPUT" >&"$VERIFY_HELPER_INPUT" 2>/dev/null &
    printf '%s\\n' "$helper_pid" "$!"
elif [[ "$3" == forwarded-empty ]]; then
    exec 3< <(printf '%s\\0' "" VALUE=forwarded "")
    VERIFY_RESULT_ENVIRONMENT_FD=3 verify_helper "$4" pid
    verify_helper "$4" pid
elif [[ "$3" == trap ]]; then
    program=$4
    trap 'verify_helper "$program" pid' USR1
    exec 3< <(printf '\\0')
    VERIFY_RESULT_ENVIRONMENT_FD=3 verify_helper "$4" trap $$
elif [[ "$3" == record-exit ]]; then
    verify_helper "$1/../agents/detached_runs.py" record-exit "$8" 23 --pid "$7"
    verify_helper "$1/../agents/detached_runs.py" record-exit "$8" 31 --pid "$7"
    cat "$8"
elif [[ "$3" == shell ]]; then
    verify_helper "$5" argument
else
    verify_helper "$5" "$3"
fi
'''
                result = subprocess.run(["bash", "-c", script, "helper-test", str(DIRECT), str(runner),
                                         mode, str(self.program), str(shell), str(TEST_HANG_GUARD_SECS),
                                         str(os.getpid()), str(record)], capture_output=True,
                                        text=True, timeout=TEST_HANG_GUARD_SECS)
                if mode == "death":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    output = cast(dict[str, object], json.loads(result.stdout))
                    self.assertEqual((output["args"], output["stdin"]), (["recovered"], "restored input\n"))
                elif mode == "parent-exit":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    helper_pid, holder_pid = map(int, result.stdout.splitlines())
                    try:
                        os.kill(holder_pid, 0)
                        try:
                            descriptor = os.pidfd_open(helper_pid)
                        except ProcessLookupError:
                            pass
                        else:
                            try:
                                exited, _, _ = select.select([descriptor], [], [], TEST_HANG_GUARD_SECS)
                                self.assertTrue(exited, "the helper survived its parent with an open input pipe")
                            finally:
                                os.close(descriptor)
                    finally:
                        try:
                            os.kill(holder_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                elif mode in ("forwarded-empty", "trap"):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    first, second = map(int, result.stdout.splitlines())
                    self.assertEqual(first, second, "the second call started another Python process")
                elif mode == "record-exit":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    saved = cast(dict[str, object], json.loads(result.stdout))
                    self.assertEqual(saved["exit_code"], 31)
                    self.assertEqual(saved["command_pid"], os.getpid())
                elif mode == "shell":
                    self.assertEqual((result.returncode, result.stdout), (9, "shell:argument\n"))
                else:
                    self.assertEqual((result.returncode, result.stdout), (128 + int(signal.Signals["SIG" + mode]), ""))

    def test_stdin_end_closes_the_helper_and_the_callers_output(self) -> None:
        process = self.start_helper()
        _ = self.request(process, "end", "payload", "done")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual((process.returncode, output, errors), (0, b"", b""))
        script = shell_functions(DIRECT / "verify.sh", "verify_helper") + '''
VERIFY_START_DIR=$1; PY=$2
verify_helper --start
verify_helper "$3" pid
'''
        caller = subprocess.run(["bash", "-c", script, "helper-test", str(DIRECT), sys.executable,
                                 str(self.program)], capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(caller.returncode, 0, caller.stderr)
        self.assertGreater(int(caller.stdout), 0)


if __name__ == "__main__":
    _ = unittest.main()
