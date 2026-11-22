"""Check request isolation and recovery through real helper processes."""
from __future__ import annotations

from contextlib import suppress
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
from typing import BinaryIO, cast, override
import unittest

from scripts.lib.helper_tree_test_support import verify_test_environment
from scripts.agents.detached_runs import RunRecord
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
                                   env=verify_test_environment(),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_helper, process)
        return process

    def stop_helper(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def encode(self, value: str, text: str, *arguments: str, directory: Path | None = None) -> bytes:
        environment = {**verify_test_environment(), "VALUE": value}
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
        if not replies[1]:
            self.fail(replies[2])
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

    def test_a_tree_update_loads_the_new_tree_whole_in_the_next_call(self) -> None:
        # A call loads base; an update then adds a name to base and a module that imports it.
        # The update lands between two calls, or during the call that loaded base.
        for during_call in (False, True):
            with self.subTest(during_call=during_call):
                tree = self.root / f"tree-{during_call}"
                package = tree / "fixturepkg"
                package.mkdir(parents=True)
                _ = (package / "__init__.py").write_text("")
                _ = (package / "base.py").write_text("OLD = 'old'\n")
                self.program = tree / "lazy_program.py"
                _ = self.program.write_text('''import json, os, sys
from pathlib import Path
def update():
    package = Path(__file__).parent / "fixturepkg"
    (package / "base.py.new").write_text("OLD = 'old'\\nNEW = 'new'\\n")
    os.replace(package / "base.py.new", package / "base.py")
    (package / "newer.py").write_text("from fixturepkg.base import NEW as VALUE\\n")
def main():
    if sys.argv[1:] == ["newer"]:
        from fixturepkg.newer import VALUE
    else:
        from fixturepkg.base import OLD as VALUE
    if sys.argv[1:] == ["update"]:
        update()
    print(json.dumps(dict(value=VALUE)))
    return 0
''')
                process = self.start_helper()
                status, output, errors = self.request(process, "first", "", "update" if during_call else "base")
                self.assertEqual((status, output, errors), (0, dict(value="old"), ""))
                if not during_call:
                    _ = self.request(process, "update", "", "update")
                status, output, errors = self.request(process, "second", "", "newer")
                self.assertEqual((status, output, errors), (0, dict(value="new"), ""))

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

    def tree_watch_cleanup_script(self) -> str:
        return shell_functions(
            DIRECT / "verify.sh", "after_tree_change", "stop_tree_watch", "defer_tree_watch_cleanup",
            "cleanup_tree_watch_folders", "cancel_tree_run", "start_tree_watch", "finish_tree_watch",
            "find_mac_runner_pid", "forward_mac_runner_signal", "run_log_tee", "start_run_log",
            "stop_interrupted_run_log", "build_seconds", "cache_record", "lint_failure_is_the_tree",
            "finish_run", "verify_cleanup", "verify_exit", "verify_interrupted",
        ) + r'''
set -euo pipefail
root=$1
PY=$2
CMD=test VERIFY_PACKAGE=sample COMMAND_ARGS=()
BUILD_FOLDERS_SCRIPT=build_folders.py
TREE_CHANGED_STATUS=77 TREE_WATCH_PID="" TREE_WATCH_DIR=""
TREE_WATCH_CLEANUP_DIRS=()
STEP_RECORD_ACTIVE=0 ACTIVITY_ACTIVE=0 HOLD_WAIT_DIR=""
RUN_LOG="$root/run.log" CACHE_DIR="$root/cache" RUN_STARTED=$SECONDS
RUN_LOG_PIDS=()
VERDICT_KEY=fixture VERDICT_TREE_KEY=""
mkdir -p "$CACHE_DIR" "$root/watch-without-key" "$root/watch"
: > "$RUN_LOG"
tree_key() { printf 'fixture\n'; }
set_verdict_keys() { :; }
user_clock() { printf 'fixture time\n'; }
note_event() {
    [[ -e "$root/call-result" ]] || printf '%s %s\n' "$1" "$6" > "$root/call-result"
}
verify_helper() {
    [[ "$1" == "$BUILD_FOLDERS_SCRIPT" ]] || return 98
    if [[ "$2" == tool ]]; then
        printf '%s\n' "$root/watch-without-key"
        return 0
    fi
    if [[ "$2" != remove || ! -s "$root/call-result" || ! -s "$root/exit-status" ]]; then
        printf 'cleanup ran before the result was recorded\n' >&2
        return 99
    fi
    if grep -qx 'ran 0' "$root/call-result" && ! grep -qx 'status=0' "$CACHE_DIR/fixture.pass"; then
        printf 'cleanup ran before the pass was cached\n' >&2
        return 99
    fi
    printf '%s\n' "$3" >> "$root/removals"
    printf 'build folders: %s: the build folder lock was removed or replaced before cleanup\n' "$3" >&2
    return 1
}
lint_copy_drop() { :; }
stop_copy_watch() { :; }
interrupt_hold_wait() { :; }
apply_placement_outcome() { :; }
finish_hold_wait() { :; }
release_token() { :; }
release_wait_claim() { :; }
remove_step_record() { :; }
finish_verify_run() { printf '%s\n' "$1" > "$root/exit-status"; }
trap 'verify_exit "$?" "$BASH_COMMAND"' EXIT
'''

    def test_tree_watch_cleanup_failure_preserves_the_pass_record_and_log(self) -> None:
        script = self.tree_watch_cleanup_script() + r'''
start_run_log
if [[ "$3" == open ]]; then
    printf 'tests passed\n' >&2
else
    printf 'tests passed\n'
fi
IFS= read -r command
[[ "$command" == exit ]]
start_tree_watch
TREE_WATCH_DIR="$root/watch"
finish_tree_watch
exit 0
'''
        for stderr_open in (True, False):
            with self.subTest(stderr_open=stderr_open):
                root = self.root / ("open" if stderr_open else "closed")
                read_fd, write_fd = os.pipe()
                os.close(read_fd)
                with os.fdopen(write_fd, "wb") as broken_pipe, subprocess.Popen(
                    ["bash", "-c", script, "tree-watch-cleanup", str(root), sys.executable, root.name],
                    env=verify_test_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE if stderr_open else broken_pipe, text=True,
                ) as process:
                    ready_output = process.stderr if stderr_open else process.stdout
                    assert ready_output is not None
                    ready, _, _ = select.select([ready_output], [], [], TEST_HANG_GUARD_SECS)
                    self.assertTrue(ready, "tee did not forward the test result")
                    self.assertEqual(ready_output.readline(), "tests passed\n")
                    try:
                        _, errors = process.communicate("exit\n", timeout=TEST_HANG_GUARD_SECS)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        raise
                    self.assertEqual(process.returncode, 0, errors)
                paths = [root / "watch-without-key", root / "watch"]
                self.assertEqual((root / "removals").read_text().splitlines(), [str(path) for path in paths])
                if stderr_open:
                    assert errors is not None
                    cause = "the build folder lock was removed or replaced before cleanup"
                    self.assertEqual(errors.splitlines(), [
                        f"verify.sh: cleanup failed: build folders: {path}: {cause} (status 1)"
                        for path in paths
                    ])
                self.assertEqual((root / "call-result").read_text(), "ran 0\n")
                self.assertEqual((root / "exit-status").read_text(), "0\n")
                self.assertEqual((root / "cache/fixture.log").read_text(), "tests passed\n")
                self.assertEqual(list((root / "cache").glob("*.fail")), [])

    @unittest.skipUnless(sys.platform == "linux", "Requires the Linux pipe capacity query.")
    def test_full_stderr_pipe_does_not_block_interrupted_tree_watch_cleanup(self) -> None:
        script = self.tree_watch_cleanup_script() + r'''
RUN_STEP_PID=""
TREE_WATCH_DIR="$root/watch"
trap 'verify_interrupted 1' HUP
start_run_log
printf 'test output\n' >&2
kill -HUP "$$"
exit 99
'''
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as stderr_reader, os.fdopen(write_fd, "wb") as stderr_writer:
            capacity = fcntl.fcntl(write_fd, fcntl.F_GETPIPE_SZ)
            self.assertEqual(os.write(write_fd, b"x" * capacity), capacity)
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
            try:
                process = subprocess.Popen(
                    ["bash", "-c", script, "tree-watch-hup", str(self.root), sys.executable],
                    env=verify_test_environment(), stdout=subprocess.DEVNULL, stderr=stderr_writer,
                    start_new_session=True,
                )
            finally:
                _ = signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            with process:
                try:
                    self.assertEqual(process.wait(timeout=10), 129)
                    self.assertTrue(os.get_blocking(write_fd))
                    stderr_writer.close()
                    # Hangup means that no process still holds the write end.
                    poller = select.poll()
                    poller.register(stderr_reader, select.POLLHUP)
                    self.assertEqual(poller.poll(5000), [(read_fd, select.POLLHUP)],
                                     "The diagnostic writer still holds stderr.")
                finally:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
        self.assertEqual((self.root / "call-result").read_text(), "interrupted 129\n")
        self.assertEqual((self.root / "exit-status").read_text(), "129\n")
        self.assertEqual((self.root / "removals").read_text(), f"{self.root / 'watch'}\n")

    def test_tree_watch_cleanup_preserves_stderr_socket_flags(self) -> None:
        script = self.tree_watch_cleanup_script() + r'''
interpreter=$PY
inspect_cleanup_write() {
    "$interpreter" -S -c '
import os
import sys

record, source, *arguments = sys.argv[1:]
write = os.write

def inspect_write(fd, message):
    with open(record, "w", encoding="utf-8") as flags:
        flags.write(f"{os.get_blocking(fd)}\n")
    return write(fd, message)

os.write = inspect_write
sys.argv = ["-c", *arguments]
exec(source)
' "$root/stderr-blocking" "$3" "${@:4}"
}
PY=inspect_cleanup_write
TREE_WATCH_DIR="$root/watch"
exit 0
'''
        reader, writer = socket.socketpair()
        with reader, writer:
            result = subprocess.run(
                ["bash", "-c", script, "tree-watch-socket", str(self.root), sys.executable],
                env=verify_test_environment(), stdout=subprocess.DEVNULL, stderr=writer.fileno(),
                timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0)
            reader.settimeout(10)
            with reader.makefile("r", encoding="utf-8") as diagnostics:
                self.assertIn("(status 1)", diagnostics.readline())
            self.assertTrue(os.get_blocking(writer.fileno()))
        self.assertEqual((self.root / "stderr-blocking").read_text(), "True\n")

    def test_late_tree_watch_signal_preserves_mac_interruption(self) -> None:
        script = self.tree_watch_cleanup_script() + r'''
TREE_WATCH_DIR="$root/watch"
TREE_WATCH_PID=998
MAC_RUNNER_PID=999 MAC_RUNNER_LAUNCHING=0
MAC_RESULT_FILE="" MAC_LOG_FILE=""
collect_child() {
    if [[ "$1" == 999 && "$2" == TERM ]]; then
        printf 'changed\n' > "$root/watch/fired"
        kill -USR2 "$$"
    fi
}
trap 'cancel_tree_run' USR2
trap 'forward_mac_runner_signal TERM 143' TERM
kill -TERM "$$"
exit 99
'''
        result = subprocess.run(["bash", "-c", script, "tree-watch-mac-term", str(self.root), sys.executable],
                                env=verify_test_environment(), capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertEqual((self.root / "watch/fired").read_text(), "changed\n")
        self.assertEqual((self.root / "call-result").read_text(), "interrupted 143\n")
        self.assertEqual((self.root / "exit-status").read_text(), "143\n")
        self.assertEqual((self.root / "removals").read_text(), f"{self.root / 'watch'}\n")
        self.assertIn(f"verify.sh: cleanup failed: build folders: {self.root / 'watch'}:", result.stderr)

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
export complete='caller complete' exported='caller exported' input='caller input' entry='caller entry'
export FIXTURE_UNSET
declare -ax FIXTURE_ARRAY=(one two)
declare -Ax FIXTURE_MAPPING=([one]=two)
fixture_function() { :; }
export -f fixture_function
env -0 > "$4/environment"
env() { return 91; }
verify_helper "$3"
compgen() { return 1; }
verify_helper_environment='caller array name' verify_helper "$3" > "$4/env-without-compgen"
unset -f compgen
(unset VERIFY_HELPER_INPUT input entry; verify_helper "$3") > "$4/fallback-environment"
FIXTURE_EXECUTION=next verify_helper "$3" > "$4/next-environment"
'''
        result = subprocess.run(["bash", "-c", script, "helper-test", str(DIRECT), sys.executable,
                                 str(self.program), str(self.root)],
                                 env={**verify_test_environment(), "inherited-key": "caller"}, capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = dict(entry.split("=", 1) for entry in
                        (self.root / "environment").read_text().split("\0") if entry)
        self.assertIn("BASH_FUNC_fixture_function%%", expected)
        self.assertEqual(expected["inherited-key"], "caller")
        actual = cast(dict[str, str], json.loads(result.stdout))
        # Bash sets this entry to each external command's path.
        _ = expected.pop("_", None)
        helper_expected = {name: value for name, value in expected.items()
                           if name not in ("BASH_FUNC_fixture_function%%", "inherited-key")}
        self.assertEqual(actual, helper_expected)
        without_compgen = cast(dict[str, str], json.loads((self.root / "env-without-compgen").read_text()))
        self.assertEqual(without_compgen, {**helper_expected, "verify_helper_environment": "caller array name"})
        fallback = cast(dict[str, str], json.loads((self.root / "fallback-environment").read_text()))
        self.assertEqual(fallback.pop("FIXTURE_EXECUTION"), "local")
        _ = fallback.pop("_", None)
        self.assertEqual(fallback, {name: value for name, value in expected.items() if name not in ("input", "entry")})
        following = cast(dict[str, str], json.loads((self.root / "next-environment").read_text()))
        self.assertEqual(following.pop("FIXTURE_EXECUTION"), "next")
        self.assertEqual(following, helper_expected)
        self.assertEqual(self.program.with_suffix(".py.calls").read_text(), "helper\nhelper\nlocal\nnext\n")

    def test_failed_or_truncated_environment_leaves_the_helper_ready(self) -> None:
        env = self.root / "env"
        _ = env.write_text("#!/bin/sh\nprintf 'VALUE=wrong\\000partial'\nexit \"$FIXTURE_STREAM_EXIT\"\n")
        env.chmod(0o755)
        script = shell_functions(DIRECT / "verify.sh", "verify_helper") + r'''
set -e
VERIFY_START_DIR=$1; PY=$2
verify_helper --start
compgen() { return 1; }
verify_helper "$3" pid
for status in 1 0; do
    PATH="$4:$PATH" VALUE=caller FIXTURE_STREAM_EXIT="$status" verify_helper --stdin "$3" "$status" <<< 'fixture input'
done
verify_helper "$3" pid
'''
        result = subprocess.run(["bash", "-c", script, "helper-test", str(DIRECT), sys.executable,
                                 str(self.program), str(self.root)], env=verify_test_environment(),
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        before, failed, truncated, after = result.stdout.splitlines()
        self.assertEqual(before, after)
        for status, reply in (("1", failed), ("0", truncated)):
            with self.subTest(status=status):
                decoded = cast(dict[str, object], json.loads(reply))
                self.assertNotEqual(decoded["pid"], int(before))
                self.assertEqual(decoded["value"], "caller")
                self.assertEqual(decoded["stdin"], "fixture input\n")
                self.assertEqual(decoded["args"], [status])

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
        detached: RunRecord = {
            "run_id": record.stem, "status": "running", "session_dir": str(self.root),
            "worker": "helper-test", "staff_id": "", "label": "verify helper",
            "command": [str(shell)], "log": str(record.with_suffix(".log")), "started_at": 1.0,
            "pid": os.getpid(), "director_dir": "", "done_message_file": "",
            "command_pid": os.getpid(), "command_started": started.start,
        }
        _ = record.write_text(json.dumps(detached))
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
                                         str(os.getpid()), str(record)],
                                         env=verify_test_environment(), capture_output=True,
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
                                 str(self.program)], env=verify_test_environment(), capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(caller.returncode, 0, caller.stderr)
        self.assertGreater(int(caller.stdout), 0)


if __name__ == "__main__":
    _ = unittest.main()
