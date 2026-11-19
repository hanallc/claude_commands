#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Exercise mac_run.sh coordination with disposable state and command stand-ins."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
import unittest
from collections.abc import Callable
from contextlib import suppress
from unittest import mock
from pathlib import Path
from typing import ClassVar, TypedDict, cast, final, override

from scripts.copies import copies
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.fork_test_support import ForkChildren
from scripts.lib.process_group import send_process_group
from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import floor_claims
from scripts.production.cli_fixture import ImmutableGitSeed, invoke_cli, process_boundary, process_boundary_for_case


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("mac_run.sh")
SCRIPTS = SCRIPT.parents[1]
MAC_TEST = SCRIPTS / "mac_test" / "mac_test.py"
SHA = "1234567890abcdef1234567890abcdef12345678"
MAC_RUN_CONFIG = {
    "mac_run_host": "natemccoy@mac",
    "mac_run_port": "15710",
    "mac_run_log": "/tmp/mac_run_hana.log",
    "mac_run_launch": "/tmp/mac_run_hana.command",
    "mac_run_status": "/tmp/mac_run_hana.status",
    "mac_run_example_seconds": "20",
    "mac_run_examples": "hana_diegetic typography;hana_lagrange showcase fit_overlay",
    "mac_run_deployment_target": "14.0",
    "mac_run_status_grace_seconds": "60",
    "mac_run_poll_seconds": "1",
    "mac_run_log_lines": "20",
    "mac_run_example_log_lines": "10",
    "mac_run_bundle": "/tmp/mac_run.bundle",
    "mac_run_example_log_prefix": "/tmp/mac_run_",
    "mac_run_source": "~/rust/hana",
}
MAC_TEST_CONFIG = """host=mac
ci_repo=
# range: 5e-324..
connect_timeout_s=6
# range: 5e-324..
gh_timeout_s=30
# range: 5e-324..
block_hours=4
# range: 5e-324..
block_max_hours=48
# range: 5e-324..
block_warn_minutes=15
# range: 5e-324..
claim_idle_minutes=15
# range: 5e-324..
claim_poll_seconds=60
# range: 5e-324..
leftover_timeout_s=10
# range: 5e-324..
alert_seconds=180
# range: 5e-324..
push_seconds=600
# range: 5e-324..
stale_seconds=300
# range: 5e-324..
repeat_minutes=1440
# range: 5e-324..
send_timeout_s=180
"""
# owning_session.owning_session() reads these; a Mac block admits the session they name.
SESSION_SELECTORS = frozenset({
    "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR",
    "PLAN_DELEGATE_BOARD_DIR", "PLAN_DELEGATE_SESSION_DIR",
})


class CommandEvent(TypedDict):
    command: str
    args: list[str]
    claimed: bool
    input: str


class CopiesInvocation(TypedDict):
    arguments: list[str]
    cwd: str
    environment: dict[str, str]


COPIES_DISPATCHER = r'''#!/bin/sh
case "$1:$2" in
    */copies/copies.py:register-remote|*/copies/copies.py:unregister-remote)
        if [ -n "$MAC_RUN_COPIES_SOCKET" ]; then
            shift
            exec "$MAC_RUN_PYTHON" -IS "$MAC_RUN_COPIES_CLIENT" "$@"
        fi ;;
esac
exec "$MAC_RUN_PYTHON" "$@"
'''


COPIES_CLIENT = r'''
import json
import os
import socket
import sys

with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(__HANG_GUARD__)
    connection.connect(os.environ["MAC_RUN_COPIES_SOCKET"])
    if sys.stdin is None:
        connection.sendall(b"-")
    else:
        socket.send_fds(connection, [b"-"], [sys.stdin.fileno()])
    with connection.makefile("rw", encoding="utf-8") as stream:
        stream.write(json.dumps({"arguments": sys.argv[1:], "cwd": os.getcwd(),
                                 "environment": dict(os.environ)}) + "\n")
        stream.flush()
        status, stdout, stderr = json.loads(stream.readline())
sys.stdout.write(stdout)
sys.stderr.write(stderr)
raise SystemExit(status)
'''.replace("__HANG_GUARD__", str(TEST_HANG_GUARD_SECS))


def open_copies_listener() -> socket.socket:
    """Keep the listener separate from the three standard streams."""
    listener = socket.socket(socket.AF_UNIX)
    if listener.fileno() >= 3:
        return listener
    with listener:
        descriptor = fcntl.fcntl(listener.fileno(), fcntl.F_DUPFD_CLOEXEC, 3)
    return socket.socket(fileno=descriptor)


def serve_copies_commands(listener: socket.socket) -> int:
    """Run local registry commands with the client shell's arguments and environment."""
    with listener, open(os.devnull, "rb") as worker_stdin:
        _ = os.dup2(worker_stdin.fileno(), 0)
        while True:
            connection = listener.accept()[0]
            with connection, suppress(BrokenPipeError, ConnectionResetError):
                marker, descriptors = socket.recv_fds(connection, 1, 1)[:2]
                if not marker:
                    continue
                descriptor = descriptors[0] if descriptors else os.open(os.devnull, os.O_RDONLY)
                with os.fdopen(descriptor, encoding="utf-8", errors="surrogateescape") as stdin:
                    with connection.makefile("r", encoding="utf-8") as stream:
                        try:
                            invocation = cast(CopiesInvocation, json.loads(stream.readline()))
                            arguments = invocation["arguments"]
                            with mock.patch.object(sys, "stdin", stdin):
                                result = invoke_cli(copies, lambda: copies.main(arguments), arguments,
                                                    invocation["environment"], Path(invocation["cwd"]))
                            response = (result.returncode, result.stdout, result.stderr)
                        except Exception:
                            response = (1, "", traceback.format_exc())
                    reply = json.dumps(response) + "\n"
                    connection.sendall(reply.encode("utf-8"))


COMMAND_STUB = r'''
run_real_copies() {
    export PATH=$MAC_RUN_REAL_PATH
    export COPIES_CONFIG=$MAC_RUN_REMOTE_COPIES_CONFIG
    export MAC_TEST_STATE_DIR=$MAC_RUN_REMOTE_CLAIMS
    local request=${remote%'; echo mac_exit=$?'}
    local -a words
    words=("${(@Q)${(z)request}}")
    local interpreter=${words[1]}
    if [[ $interpreter == '~/.claude/scripts/lib/py' ]]; then
        interpreter=$MAC_RUN_TEST_SCRIPTS/lib/py
    fi
    if [[ ! -x $interpreter ]]; then
        print -u2 -- "command not found: ${words[1]}"
        print -r -- mac_exit=127
        exit 0
    fi
    case ${words[2]} in
        '~/.claude/scripts/copies/copies.py'|'~/.claude/scripts/lib/scratch.py') ;;
        *) exit 99 ;;
    esac
    MAC_RUN_COPIES_SOCKET= PYTHONPYCACHEPREFIX=$MAC_RUN_REMOTE_BYTECODE "$interpreter" \
        "$MAC_RUN_TEST_SCRIPTS/${words[2]#\~/.claude/scripts/}" "${words[@]:2}"
    print -r -- "mac_exit=$?"
    exit 0
}
state=$MAC_RUN_STUB_STATE
command=${0:t}
remote=${@[-1]}
placed_run=0
if [[ $command == ssh && $remote == *mac_group=* ]]; then
    outer=("${(@Q)${(z)remote}}")
    wrapper=("${(@Q)${(z)outer[3]}}")
    work=${wrapper[1]#work=}
    printf '%s\0' "$work" >> "$state/placed-commands"
    # (z) reads each newline of the wrapper as a ";" word.
    print -rn -- "${wrapper[3]#on_close=}" > "$state/on-close"
    words=("${(@Q)${(z)work}}")
    remote=${words[4]}
    set -- "${@[1,-2]}" "$remote"
    placed_run=1
    print -r -- mac_group=4567
fi
claimed=false
[[ ! -e $MAC_TEST_STATE_DIR/run.json ]] || claimed=true
input=""
if [[ $command == ssh && ${@[-1]} == *'cat >'* ]]; then
    if (( placed_run )); then
        input=${remote#*"<<'HANA_LAUNCH'"$'\n'}
        input=${input%%$'\nHANA_LAUNCH'*}
    else
        input=$(cat; print -rn -- .)
        input=${input%.}
    fi
fi
# NUL separates fields so labels and command input keep their original bytes.
printf '%s\0' "$command" "$claimed" "$input" "$#" "$@" >> "$state/events"

case $command in
    systemd-run|scp|curl) exit 0 ;;
    git)
        if (( ${@[(Ie)merge-base]} && ${@[(Ie)--is-ancestor]} )); then
            [[ -n $MAC_RUN_CLONE_DESCENDANT && ${@[-2]} == $MAC_RUN_SHA && ${@[-1]} == $MAC_RUN_CLONE_DESCENDANT ]]
            exit $?
        fi
        if (( ${@[(Ie)rev-parse]} )); then
            code=0
            (( ! ${@[(Ie)--verify]} )) || code=${MAC_RUN_GIT_RESOLVE_STATUS:-0}
            (( code != 0 )) || print -r -- "$MAC_RUN_SHA"
            exit $code
        fi
        if (( ${@[(Ie)bundle]} && ${@[(Ie)create]} )); then
            if [[ -n $MAC_RUN_CLONE_DESCENDANT ]] && (( ${@[(Ie)^$MAC_RUN_CLONE_DESCENDANT]} )); then
                print -u2 -- 'fatal: Refusing to create empty bundle.'
                exit 128
            fi
            bundle_index=${@[(Ie)-q]}
            print -r -- bundle > "${@[$((bundle_index + 1))]}"
        fi
        exit 0 ;;
    ssh) ;;
    *) exit 99 ;;
esac

if [[ ${@[-1]} == true ]] && (( ${@[(Ie)-o]} )); then
    [[ ${MAC_RUN_REACHABLE:-1} == 1 ]] && exit 0
    exit 255
fi

remote=${@[-1]}
if [[ $remote == *mac_stopped=* ]]; then
    print -r -- mac_stopped=4567
    exit 0
fi
clone_head=$state/clone-head
if [[ $MAC_RUN_REAL_COPIES == 1 && $remote == *'copies.py '* ]]; then
    run_real_copies
fi
if [[ $remote == *'hana_instances.py stop-checkout '* ]]; then
    print -r -- called > "$state/cleanup-called"
    code=${MAC_RUN_CLEANUP_STATUS:-0}
    if [[ $code == 0 && ${MAC_RUN_CONFIRM_STOP:-1} == 1 ]]; then
        print -r -- 'hana-instances: checkout clear'
        print -r -- confirmed > "$state/checkout-clear"
    fi
    print -r -- "mac_exit=$code"
    exit 0
fi
if [[ $remote == *'scratch.py end '* ]]; then
    if [[ ! -e $state/checkout-clear ]]; then
        print -u2 -- 'task end before confirmed exit'
        exit 98
    fi
    if [[ $MAC_RUN_REAL_COPIES == 1 ]]; then
        run_real_copies
    fi
    print -r -- mac_exit=0
    exit 0
fi
if [[ $remote == *'copies.py clone '* ]]; then
    print -rl -- "$MAC_RUN_CHECKOUT" mac_exit=0
    exit 0
fi
if [[ $remote == *'checkout -q --detach'* ]]; then
    print -rn -- "$MAC_RUN_SHA" > "$clone_head"
fi
if [[ $MAC_RUN_HOLD_BUILD == 1 && $remote == *'cargo build -q -p hana'* ]]; then
    # Like the Mac wrapper, the held build ends when the SSH input closes.
    exec "$MAC_RUN_PYTHON" -c '
import os, sys
from pathlib import Path
Path(os.environ["MAC_RUN_STUB_STATE"], "build-started").write_text("started\n")
_ = sys.stdin.read()
Path(os.environ["MAC_RUN_STUB_STATE"], "input-closed").write_text("closed\n")
raise SystemExit(255)
'
fi
if (( placed_run )); then
    print -r -- "HEAD=$MAC_RUN_SHA"
    print -r -- mac_exit=0
    exit 0
fi
if [[ $remote == *'; echo mac_exit=$?'* ]]; then
    code=0
    [[ $remote != *'perl -e'* ]] || code=142
    print -r -- "mac_exit=$code"
    exit 0
fi
if [[ $remote == *'git -C '* && $remote == *'rev-parse HEAD'* ]]; then
    if [[ -e $clone_head ]]; then
        print -r -- "$(< "$clone_head")"
    else
        print -r -- "${MAC_RUN_CLONE_DESCENDANT:-$MAC_RUN_SHA}"
    fi
    exit 0
fi
[[ $remote != *'cat /tmp/mac_run_hana.status'* ]] || print -r -- 142
exit 0
'''


QUEUE_STUB = r'''
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys

from scripts.mac_test import mac_test

args = sys.argv[1:]
state = Path(os.environ["MAC_RUN_STUB_STATE"])
receipt = state / "placement-receipt"
with (state / "queue-events").open("a") as stream:
    stream.write(json.dumps(args) + "\n")
def option(name):
    return args[args.index(name) + 1]

def run_mac_test(*arguments):
    sys.argv = [mac_test.__file__, *arguments]
    return mac_test.main()

if args[0] == "place-step":
    with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
        status = run_mac_test("claim", "--identity", "--pid", option("--pid"),
            "--what", "mac run " + os.environ["MAC_RUN_SHA"][:9], "--worktree", option("--repo-root"), "--wait", "0")
    if status:
        print(output.getvalue(), end="")
        print(errors.getvalue(), end="", file=sys.stderr)
        raise SystemExit(status)
    identity = json.loads(next(line.removeprefix("claim_identity=") for line in output.getvalue().splitlines()
        if line.startswith("claim_identity=")))
    receipt.write_text(json.dumps({"identity": identity, "pid": option("--pid")}))
    assignment = json.dumps({"episode": option("--episode"), "attempt": identity["attempt"]})
    Path(option("--assignment-file")).write_text(assignment)
    (state / "assigned").write_text(assignment)
    print("\t".join(("mac", "mac", "natemccoy@mac", "0", option("--episode"), identity["attempt"])))
elif receipt.exists():
    saved = json.loads(receipt.read_text())
    identity = saved["identity"]
    status = run_mac_test("release", "--pid", saved["pid"],
        "--claim-id", identity["claim_id"], "--episode", identity["episode"], "--attempt", identity["attempt"])
    receipt.unlink()
    raise SystemExit(status)
'''


SITE_CUSTOMIZE = r'''
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if sys.argv[0].endswith("/mac_queue_run.py"):
    with (Path(os.environ["MAC_RUN_STUB_STATE"]) / "runner-events").open("a") as stream:
        stream.write(json.dumps(sys.argv[1:]) + "\n")

if os.environ.get("MAC_TEST_ADVANCE_WAIT") == "1":
    original_monotonic = time.monotonic
    offset = 0.0

    def advancing_monotonic() -> float:
        return original_monotonic() + offset

    def advancing_sleep(seconds: float) -> None:
        global offset
        offset += seconds

    time.monotonic = advancing_monotonic
    time.sleep = advancing_sleep
'''


@final
class MacRunTests(unittest.TestCase):
    clone_source: ClassVar[Path]
    clone_head: ClassVar[str]
    clone_git_config: ClassVar[Path]
    copies_socket: ClassVar[Path]
    copies_dispatcher: ClassVar[Path]
    copies_client: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="mac-run-source-")
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)
        cls.clone_source = ImmutableGitSeed.create(root, "main", "Clone Tests", "clone@example.invalid",
                                                    {"source.txt": "content\n"}).checkout
        cls.clone_git_config = root / "git-config"
        cls.clone_head = subprocess.check_output(
            ["git", "-C", str(cls.clone_source), "rev-parse", "HEAD"], text=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": str(cls.clone_git_config), "GIT_CONFIG_NOSYSTEM": "1"},
            timeout=TEST_HANG_GUARD_SECS,
        ).strip()
        socket_folder = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="mac-run-", dir="/tmp")))
        cls.copies_socket = socket_folder / "copies"
        cls.copies_dispatcher = socket_folder / "python"
        _ = cls.copies_dispatcher.write_text(COPIES_DISPATCHER)
        cls.copies_dispatcher.chmod(0o755)
        cls.copies_client = socket_folder / "client.py"
        _ = cls.copies_client.write_text(COPIES_CLIENT)
        children = cls.enterClassContext(ForkChildren())
        with open_copies_listener() as listener:
            listener.bind(str(cls.copies_socket))
            listener.listen()
            _ = children.start(lambda: serve_copies_commands(listener))

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.state_directory = Path()
        self.stub_state = Path()
        self.bin_directory = Path()
        self.repository = Path()
        self.config_path = Path()
        self.temp_directory = Path()
        self.environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.home.mkdir()
        self.state_directory = self.root / "mac-test-state"
        self.stub_state = self.root / "stub-state"
        self.stub_state.mkdir()
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.config_path = self.root / "config/mac_test.conf"
        self.temp_directory = self.root / "tmp"
        self.temp_directory.mkdir()
        self.config_path.parent.mkdir()
        _ = self.config_path.write_text(MAC_TEST_CONFIG)
        self.write_production_config()
        queue = self.root / "fake-queue.py"
        _ = queue.write_text(QUEUE_STUB)
        queue_command = shlex.join([str(SCRIPTS / "lib/py"), str(queue)])
        _ = (self.root / "config/mac_gate.conf").write_text(
            f"admission_client_command={queue_command}\n# range: 5e-324..\nterminate_grace_seconds=3\n",
        )
        self.write_folder_config()
        copies_config = self.config_path.parent / "copies.conf"
        self.write_copies_config(copies_config, self.root / "mirrors")
        stub = self.bin_directory / "command-stub"
        self.write_executable(stub, COMMAND_STUB)
        for name in ("ssh", "scp", "git", "systemd-run", "gh", "rsync", "curl"):
            (self.bin_directory / name).symlink_to(stub)
        startup = self.root / "python-startup"
        startup.mkdir()
        _ = (startup / "sitecustomize.py").write_text(
            SITE_CUSTOMIZE, encoding="utf-8"
        )
        # The runner's session must not own the fixture's Mac block.
        boundary = process_boundary_for_case(self)
        seed_interpreter_cache(self.root / "cache", sys.executable if boundary else self.copies_dispatcher)
        self.environment = {
            **{name: value for name, value in os.environ.items() if name not in SESSION_SELECTORS},
            "HOME": str(self.home),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "__ETC_ZSHENV_SOURCED": "1",
            "PATH": f"{self.bin_directory}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(startup),
            "CONF_DIR": str(self.config_path.parent),
            "COPIES_CONFIG": str(copies_config),
            "MAC_TEST_STATE_DIR": str(self.state_directory),
            "STAFF_FILE": str(self.root / "staff.json"),
            "USER_ZONE": "UTC",
            "SCRATCH_ROOT": str(self.root / "scratch"),
            "SCRATCH_COPIES_ROOT": str(self.root / "workspaces"),
            "SCRATCH_HOLDS": str(self.root / "holds.json"),
            "SESSION_TEMP_ROOT": str(self.root / "session"),
            "SESSION_FORMER_ROOT": str(self.root / "former-session"),
            "MAC_RUN_STUB_STATE": str(self.stub_state),
            "MAC_RUN_SHA": SHA,
            "MAC_RUN_PYTHON": sys.executable,
            "MAC_RUN_COPIES_SOCKET": "" if boundary else str(self.copies_socket),
            "MAC_RUN_COPIES_CLIENT": str(self.copies_client),
            "MAC_RUN_TEST_SCRIPTS": str(SCRIPTS),
            "MAC_RUN_REMOTE_BYTECODE": str(self.root / "mac-bytecode"),
            "MAC_RUN_CHECKOUT": str(self.home / "scratch" / "gate-mac-run-private" / "checkout"),
            "HANA_TEST_PURPOSE": "Mac launch coordination",
            "TMPDIR": str(self.temp_directory),
        }

        _ = self.environment.pop("CLAUDE_PYTHON", None)

    def write_production_config(self, **overrides: str) -> None:
        settings = MAC_RUN_CONFIG | overrides
        _ = (self.config_path.parent / "production.conf").write_text(
            "".join(f"{name}={value}\n" for name, value in settings.items())
        )

    def write_folder_config(self) -> None:
        config = self.config_path.parent
        _ = (config / "build_targets.conf").write_text(
            "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n",
        )
        _ = (config / "build_folders.conf").write_text(
            """patterns.legacy-unique={purpose}{suffix}
patterns.scratch={purpose}-{suffix}
# range: 0.001..60
locks.probe_wait_seconds=0.25
# range: 0.001..1
locks.probe_retry_seconds=1
""",
        )
        _ = (config / "scratch.conf").write_text(
            f"""visible_root={self.root / 'repro'}
# whole: 1..
default_life_seconds=86400
# whole: 1..
suffix_length=6
# whole: 1..
record_lock_attempts=3
# whole: 1..
repo_depth=3
repo_skip=target node_modules .git
""",
        )

    def write_copies_config(self, path: Path, registry: Path) -> None:
        _ = path.write_text(
            f"""remote_registry={registry}
owner.verify=verify
wait_seconds=0.01
git_timeout_seconds=10
guard_timeout_seconds=10
""",
        )

    def write_executable(self, path: Path, source: str) -> None:
        _ = path.write_text("#!/usr/bin/env zsh\n" + source, encoding="utf-8")
        path.chmod(0o755)

    def state_command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT.parents[1] / "lib/py"), str(MAC_TEST), *arguments],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=TEST_HANG_GUARD_SECS,
        )

    def mac_run(
        self, *, extra: dict[str, str] | None = None, timeout: float = 300
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["zsh", str(SCRIPT), str(self.repository), SHA, "0"],
            cwd=self.root,
            env={**self.environment, **(extra or {})},
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def start_mac_run(
        self, *, extra: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["zsh", str(SCRIPT), str(self.repository), SHA, "0"],
            cwd=self.root,
            env={**self.environment, **(extra or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def events(self) -> list[CommandEvent]:
        path = self.stub_state / "events"
        if not path.exists():
            return []
        fields = path.read_bytes().decode("utf-8").split("\0")
        events: list[CommandEvent] = []
        position = 0
        while position < len(fields) - 1:
            command, claimed, input_text, argument_count = fields[position:position + 4]
            start = position + 4
            position = start + int(argument_count)
            events.append({"command": command, "claimed": claimed == "true",
                           "input": input_text, "args": fields[start:position]})
        return events

    def clear_events(self) -> None:
        (self.stub_state / "events").unlink(missing_ok=True)
        (self.stub_state / "placed-commands").unlink(missing_ok=True)
        (self.stub_state / "queue-events").unlink(missing_ok=True)
        (self.stub_state / "runner-events").unlink(missing_ok=True)

    def assert_no_build_started(self) -> None:
        self.assertFalse(
            any(
                event["command"] == "ssh"
                and any("cargo build" in argument for argument in event["args"])
                for event in self.events()
            )
        )
        self.assertFalse(
            any(event["command"] == "scp" for event in self.events())
        )

    def assert_claim_released(self) -> None:
        self.assertFalse((self.state_directory / "run.json").exists())

    def wait_for_path(self, path: Path, process: subprocess.Popen[str]) -> None:
        wait_until(lambda: path.exists() or process.poll() is not None, path.parent, processes=(process,))
        if process.poll() is not None:
            output, errors = process.communicate()
            self.fail("mac_run exited before the build marker: " + output + errors)
        self.assertTrue(path.exists())

    def test_blocked_mac_exits_nine_without_starting_build(self) -> None:
        blocked = self.state_command(
            "block", "--holder", "demo", "--for", "video capture"
        )
        self.assertEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.clear_events()

        result = self.mac_run()

        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertIn("blocked by demo: video capture", result.stdout)
        self.assert_no_build_started()
        self.assert_claim_released()

    def test_both_builds_set_the_development_deployment_target(self) -> None:
        self.environment.update({
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "STAFF_FILE": str(self.root / "staff.json"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })
        result = self.mac_run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        builds = [event["args"][-1] for event in self.events()
                  if event["command"] == "ssh" and "cargo build" in event["args"][-1]]
        self.assertGreaterEqual(sum(command.count("cargo build") for command in builds), 2)
        self.assertTrue(any("cargo build -q -p hana" in command for command in builds))
        self.assertTrue(any("--example" in command for command in builds))
        for command in builds:
            self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0 cargo build", command)
            for build_kind, pattern in (("hana", "cargo build -q -p hana"), ("example", 'cargo build -q "${args[@]}"')):
                with self.subTest(build_kind=build_kind):
                    statement = next(line for line in command.splitlines() if pattern in line)
                    self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0 cargo build", statement)
        runner_calls = [cast(list[str], json.loads(line))
                        for line in (self.stub_state / "runner-events").read_text().splitlines()]
        self.assertEqual(len(runner_calls), 1)
        call = runner_calls[0]
        self.assertEqual(call[call.index("--lifecycle-script") + 1], str(SCRIPT))
        self.assertEqual(call[call.index("--kind") + 1], "run")
        self.assertEqual(call[call.index("--worktree") + 1], str(self.repository))
        placed_commands = (self.stub_state / "placed-commands").read_text().split("\0")[:-1]
        self.assertEqual(len(placed_commands), 1)
        placed_arguments = shlex.split(placed_commands[0])
        queue_events = [cast(list[str], json.loads(line))
                        for line in (self.stub_state / "queue-events").read_text().splitlines()]
        self.assertEqual([event[0] for event in queue_events], ["place-step", "cancel-placement"])
        placed = queue_events[0]
        assigned = cast(dict[str, str], json.loads((self.stub_state / "assigned").read_text()))
        self.assertEqual(placed_arguments[0], "MAC_PLACEMENT=" + placed[placed.index("--episode") + 1]
                         + "/" + assigned["attempt"])


    def test_busy_mac_exits_nine_without_starting_build(self) -> None:
        claimed = self.state_command(
            "claim",
            "--pid",
            str(os.getpid()),
            "--what",
            "unit tests",
            "--worktree",
            str(self.repository),
        )
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        self.clear_events()

        result = self.mac_run(extra={"MAC_TEST_ADVANCE_WAIT": "1"})

        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"busy: unit tests since \d\d:\d\d")
        self.assert_no_build_started()
        self.assertTrue((self.state_directory / "run.json").exists())
        claim = cast(dict[str, str], json.loads((self.state_directory / "run.json").read_text()))
        released = self.state_command("release", "--pid", str(os.getpid()), "--claim-id", claim["claim_id"],
                                      "--episode", claim["episode"], "--attempt", claim["attempt"])
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)

    def test_unreadable_state_exits_nine_without_starting_build(self) -> None:
        self.state_directory.mkdir()
        block = self.state_directory / "block.json"
        _ = block.write_text("damaged\n", encoding="utf-8")

        result = self.mac_run()

        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertIn(f"state unreadable: {block}", result.stdout)
        self.assert_no_build_started()
        self.assert_claim_released()

    def test_smoke_uses_the_shared_refused_port_and_host(self) -> None:
        self.write_production_config(
            smoke_ready_seconds="300", smoke_exit_seconds="30", smoke_connect_seconds="1",
            smoke_call_seconds="5", smoke_signal_grace_seconds="2", smoke_poll_seconds="1",
        )
        _ = (self.root / "config/hana_shot.conf").write_text(
            "# whole: 1..\nrefused_port=17461\nhost=192.0.2.46\n",
        )
        script = SCRIPT.with_name("smoke_launch.sh")

        def smoke(port: str) -> subprocess.CompletedProcess[str]:
            self.clear_events()
            return subprocess.run(
                ["bash", str(script), str(self.repository), SHA, str(self.root / "smoke.log"), port],
                cwd=self.root, env=self.environment, capture_output=True, text=True,
                timeout=TEST_HANG_GUARD_SECS, check=False)

        refused = smoke("17461")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("17461 is the user's live Hana", refused.stdout)
        self.assertEqual(self.events(), [])

        occupied = smoke("17462")
        self.assertEqual(occupied.returncode, 1, occupied.stdout + occupied.stderr)
        self.assertIn("port 17462 already answers BRP", occupied.stdout)
        self.assertEqual([event["args"][-1] for event in self.events() if event["command"] == "curl"],
                         ["http://192.0.2.46:17462/jsonrpc"])

    def test_unreachable_mac_keeps_exit_three(self) -> None:
        result = self.mac_run(extra={"MAC_RUN_REACHABLE": "0"})

        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("mac unreachable", result.stdout)
        self.assertEqual(
            [event["command"] for event in self.events()], ["ssh"]
        )
        self.assert_claim_released()

    def test_claim_names_the_repository_so_another_repository_runs_beside_it(self) -> None:
        (self.repository / ".git").mkdir()
        other = self.root / "other"
        (other / ".git").mkdir(parents=True)
        claimed = self.state_command("claim", "--pid", str(os.getpid()), "--what", "other tests",
                                     "--worktree", str(other))
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        claims = self.state_directory / "claims"
        before = (claims / "other.json").read_bytes()
        self.clear_events()

        result = self.mac_run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreater(len([event for event in self.events() if event["command"] in {"ssh", "scp"}]), 1)
        self.assertEqual(sorted(path.name for path in claims.iterdir()), ["other.json"])
        self.assertEqual((claims / "other.json").read_bytes(), before)
        self.assert_claim_released()

    def test_success_releases_claim(self) -> None:
        result = self.mac_run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_claim_released()
        remote_events = [
            event for event in self.events() if event["command"] in {"ssh", "scp"}
        ]
        self.assertGreater(len(remote_events), 1)
        self.assertFalse(remote_events[0]["claimed"])
        changing = [event for event in remote_events[1:] if event["command"] == "ssh"
                    and any(action in event["args"][-1] for action in ("clone ", "fetch ", "checkout ", "cargo "))]
        self.assertTrue(all(event["claimed"] for event in changing))

    def test_ancestor_of_the_clone_head_checks_out_without_a_bundle(self) -> None:
        result = self.mac_run(extra={
            "MAC_RUN_CLONE_DESCENDANT": "abcdef1234567890abcdef1234567890abcdef12",
        })

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.events()
        self.assertFalse(any(
            event["command"] == "git" and "bundle" in event["args"]
            and "create" in event["args"] for event in events
        ), events)
        self.assertFalse(any(event["command"] == "scp" for event in events), events)
        remote_commands = [
            event["args"][-1] for event in events if event["command"] == "ssh"
        ]
        self.assertFalse(any("fetch" in command for command in remote_commands), events)
        self.assertEqual(sum(
            f"checkout -q --detach {SHA}" in command for command in remote_commands
        ), 1, events)
        self.assertTrue(any(
            "cargo build -q -p hana" in command for command in remote_commands
        ), events)
        self.assertTrue(any(
            "cargo build" in command and "--example" in command
            for command in remote_commands
        ), events)
        self.assertFalse(any(
            event["command"] == "git" and "update-ref" in event["args"]
            for event in events
        ), events)
        self.assertEqual(list(self.temp_directory.iterdir()), [])
        self.assertIn(f"HEAD={SHA}", result.stdout)
        self.assert_claim_released()

    def test_bundle_with_a_space_has_the_same_sftp_and_ssh_filename(self) -> None:
        remote_bundle = "/tmp/Hana test.bundle"
        self.write_production_config(mac_run_bundle=remote_bundle)
        result = self.mac_run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.events()
        copy = next(event for event in events if event["command"] == "scp")
        destination = copy["args"][-1].split(":", 1)[1]
        command = next(event["args"][-1] for event in events
                       if event["command"] == "ssh" and " fetch -q " in event["args"][-1])
        arguments = shlex.split(command)
        fetched = arguments[arguments.index("fetch") + 2]
        self.assertEqual(destination, remote_bundle)
        self.assertEqual(fetched, remote_bundle)
        self.assertEqual(destination, fetched)

    def test_bundle_uses_fixture_temp_directory(self) -> None:
        result = self.mac_run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        copy = next(event for event in self.events() if event["command"] == "scp")
        self.assertTrue(
            Path(copy["args"][1]).is_relative_to(self.temp_directory), copy
        )

    def test_early_failure_releases_claim(self) -> None:
        result = self.mac_run(extra={"MAC_RUN_GIT_RESOLVE_STATUS": "2"})

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(any(
            event["command"] == "git" and "rev-parse" in event["args"]
            for event in self.events()
        ), self.events())
        self.assertNotIn("HANA_TEST_PURPOSE", result.stdout + result.stderr)
        self.assert_claim_released()
        self.assert_no_build_started()
        events = [cast(list[str], json.loads(line))
                  for line in (self.stub_state / "queue-events").read_text().splitlines()]
        self.assertEqual([event[0] for event in events], ["place-step", "cancel-placement"])

    def test_missing_purpose_refuses_before_ssh(self) -> None:
        for purpose in (None, "", " \t "):
            with self.subTest(purpose=purpose):
                self.clear_events()
                if purpose is None:
                    _ = self.environment.pop("HANA_TEST_PURPOSE", None)
                else:
                    self.environment["HANA_TEST_PURPOSE"] = purpose
                result = self.mac_run()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("HANA_TEST_PURPOSE", result.stderr)
                self.assertEqual(self.events(), [])
                self.assert_claim_released()

    def test_scratch_clone_launch_keeps_purpose_and_owner(self) -> None:
        purpose = "camera's $(touch PURPOSE_INJECTION); frame & light"
        for explicit_owner in (False, True):
            with self.subTest(explicit_owner=explicit_owner):
                self.clear_events()
                owner = "director's $(touch OWNER_INJECTION)" if explicit_owner else "019b3a40-4250-7000-8000-000000000104"
                result = self.mac_run(extra={
                    "HANA_TEST_PURPOSE": purpose,
                    "CLAUDE_CODE_SESSION_ID": "019b3a40-4250-7000-8000-000000000104",
                    "HANA_TEST_OWNER": owner if explicit_owner else "",
                })
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                remote = [event for event in self.events() if event["command"] == "ssh"]
                creation = next(event for event in remote if "copies.py clone " in event["args"][-1])
                create_words = shlex.split(creation["args"][-1])
                task = create_words[create_words.index("--task") + 1]
                self.assertRegex(task, rf"^gate:mac-run-{SHA}-\d+$")
                self.assertIn("--name", create_words)
                launch = next(event["input"] for event in remote if event["input"])
                self.assertIn(f"cd {self.environment['MAC_RUN_CHECKOUT']}", launch)
                line = next(line for line in launch.splitlines() if line.startswith("HANA_TEST_PURPOSE="))
                words = shlex.split(line)
                self.assertIn(f"HANA_TEST_PURPOSE={purpose}", words)
                self.assertIn(f"HANA_TEST_OWNER={owner}", words)
                self.assertIn(f"{self.environment['MAC_RUN_CHECKOUT']}/target/debug/hana", words)
                # Evaluate the original label assignments with Zsh, replacing only the launched program.
                labels = line.partition(" BEVY_ASSET_ROOT=")[0]
                evaluated = subprocess.run(
                    ["zsh", "-f", "-c", labels + " " + shlex.join([
                        sys.executable, "-c", "import os,json; print(json.dumps([os.environ['HANA_TEST_PURPOSE'], os.environ['HANA_TEST_OWNER']]))"
                    ])], cwd=self.root, env=self.environment, capture_output=True, text=True, check=False)
                self.assertEqual(evaluated.returncode, 0, evaluated.stderr)
                self.assertEqual(json.loads(evaluated.stdout), [purpose, owner])
                self.assertFalse((self.root / "PURPOSE_INJECTION").exists())
                self.assertFalse((self.root / "OWNER_INJECTION").exists())
                ends = [event for event in remote if f"scratch.py end --task {task}" in event["args"][-1]]
                self.assertEqual(len(ends), 1)
                self.assertTrue(ends[0]["claimed"])
                self.assert_claim_released()

    def test_task_end_requires_confirmed_exit_and_failed_stops_keep_the_checkout(self) -> None:
        result = self.mac_run()
        events = self.events()
        stop = [event for event in events if event["command"] == "ssh"
                and "hana_instances.py stop-checkout " in event["args"][-1]]
        self.assertEqual(len(stop), 1, events)
        self.assertTrue(stop[0]["claimed"])
        words = shlex.split(stop[0]["args"][-1])
        self.assertEqual(words[words.index("stop-checkout") + 1].rstrip(";"),
                         self.environment["MAC_RUN_CHECKOUT"])
        ended = [event for event in events if event["command"] == "ssh"
                 and "scratch.py end --task " in event["args"][-1]]
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(ended), 1, events)
        self.assertLess(events.index(stop[0]), events.index(ended[0]))
        self.assertTrue(ended[0]["claimed"])
        self.assert_claim_released()

        program = shell_functions(SCRIPT, "cleanup_mac_run") + r'''
clone=$1
clone_arg=${(q)clone}
clone_task=$2
argv_file=$3
bundle=''
on_mac() {
    printf '%s\0' "$1" >> "$MAC_RUN_STUB_STATE/cleanup-commands"
    return "$MAC_RUN_CLEANUP_STATUS"
}
cleanup_mac_run
'''
        commands_path = self.stub_state / "cleanup-commands"
        for stop_status, confirm in ((23, "0"), (0, "0")):
            with self.subTest(stop_status=stop_status, confirm=confirm):
                commands_path.unlink(missing_ok=True)
                result = subprocess.run(
                    ["zsh", "-f", "-c", program, "cleanup-test",
                     self.environment["MAC_RUN_CHECKOUT"], f"gate:mac-run-{SHA}-123",
                     str(self.stub_state / "cleanup-argv")],
                    cwd=self.root,
                    env={**self.environment, "MAC_RUN_CLEANUP_STATUS": str(stop_status)},
                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
                )
                commands = commands_path.read_text().split("\0")[:-1]
                stops = [command for command in commands if "hana_instances.py stop-checkout " in command]
                self.assertEqual(len(stops), 1, commands)
                words = shlex.split(stops[0])
                self.assertEqual(words[words.index("stop-checkout") + 1].rstrip(";"),
                                 self.environment["MAC_RUN_CHECKOUT"])
                ends = [command for command in commands if "scratch.py end --task " in command]
                self.assertEqual(ends, [], commands)
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn(self.environment["MAC_RUN_CHECKOUT"], result.stderr)
                self.assertIn("kept checkout", result.stderr.lower())

    def clone_registry_records(self, config: str) -> list[dict[str, object]]:
        with mock.patch.dict(os.environ, {"COPIES_CONFIG": config}):
            return [cast(dict[str, object], json.loads(copies.remote_record_path(mirror).read_text()))
                    for mirror in copies.remote_mirrors()]

    def test_registry_worker_serves_the_next_command_after_an_exception(self) -> None:
        command = [str(SCRIPTS / "lib/py"), str(SCRIPTS / "copies/copies.py"), "register-remote",
                   "--repository", "hana", "--owner", "verify", "--session", "fixture", "--host", "localhost"]
        bad = subprocess.run([*command, "--path", os.fsdecode(b"/fixture-\xff")], cwd=self.root,
                             env=self.environment, capture_output=True, text=True, check=False,
                             timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        self.assertIn("UnicodeEncodeError", bad.stderr)

        good = subprocess.run([*command, "--path", "/fixture"], cwd=self.root, env=self.environment,
                              capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        record, = self.clone_registry_records(self.environment["COPIES_CONFIG"])
        self.assertEqual(record["path"], "/fixture")

    def test_registry_worker_gives_stdin_only_to_the_command(self) -> None:
        endpoint = self.copies_socket.with_name("stdin")
        worker_input = self.root / "worker-input"
        _ = worker_input.write_bytes(b"worker input\n")

        def read_stdin(_arguments: list[str]) -> int:
            print(os.fsencode(sys.stdin.read()).hex())
            child_input = subprocess.check_output(["cat"], timeout=TEST_HANG_GUARD_SECS)
            print(child_input.hex())
            return 0

        def start_worker() -> int:
            with worker_input.open("rb") as source:
                _ = os.dup2(source.fileno(), 0)
                return serve_copies_commands(listener)

        command = [sys.executable, "-IS", str(self.copies_client), "register-remote", "--help"]
        with ForkChildren() as children, open_copies_listener() as listener:
            listener.bind(str(endpoint))
            listener.listen()
            with mock.patch.object(copies, "main", read_stdin):
                _ = children.start(start_worker)
            for name, payload in (("open", b"bytes\0\xff\n"), ("empty", b""), ("closed", b"")):
                with self.subTest(stdin=name):
                    arguments = command
                    if name == "closed":
                        arguments = ["/bin/sh", "-c", 'exec 0<&-; exec "$@"', "closed-stdin", *command]
                    result = subprocess.run(
                        arguments, cwd=self.root, input=payload,
                        env={**self.environment, "MAC_RUN_COPIES_SOCKET": str(endpoint)},
                        capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, payload.hex().encode() + b"\n\n")

        endpoint = self.copies_socket.with_name("stdin-closed")
        ready = self.root / "registry-listening"

        def start_without_stdin() -> int:
            os.close(0)
            with open_copies_listener() as closed_stdin_listener:
                closed_stdin_listener.bind(str(endpoint))
                closed_stdin_listener.listen()
                ready.touch()
                return serve_copies_commands(closed_stdin_listener)

        with self.subTest(stdin="runner starts closed"), ForkChildren() as children:
            _ = children.start(start_without_stdin)
            wait_until(ready.exists, ready.parent)
            result = subprocess.run(
                command, cwd=self.root, stdin=subprocess.DEVNULL,
                env={**self.environment, "MAC_RUN_COPIES_SOCKET": str(endpoint)},
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage: copies.py register-remote", result.stdout)

    def test_missing_remote_interpreter_returns_127(self) -> None:
        program = shell_functions(SCRIPT, "on_mac") + '\nhost=mac\non_mac "$1"\n'
        command = shlex.quote(str(self.home / "missing-python"))
        command += " ~/.claude/scripts/copies/copies.py unregister-remote --host localhost --path /fixture"
        result = subprocess.run(
            ["zsh", "-f", "-c", program, "missing-interpreter", command],
            cwd=self.root, env={**self.environment, "MAC_RUN_REAL_COPIES": "1"},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 127, result.stdout + result.stderr)
        self.assertIn("missing-python", result.stderr)

    def assert_clone_records_after_cleanup(self, cleanup_status: int, *,
                                           prepare_cleanup: Callable[[Path], None] = lambda _: None) -> None:
        self.write_production_config(mac_run_source=str(self.clone_source))
        local_config = self.environment["COPIES_CONFIG"]
        remote_config = self.root / "remote-copies.conf"
        self.write_copies_config(remote_config, self.root / "remote-mirrors")
        self.environment.update({
            "MAC_RUN_SHA": self.clone_head, "MAC_RUN_REAL_COPIES": "1", "MAC_RUN_REAL_PATH": os.environ["PATH"],
            "MAC_RUN_REMOTE_COPIES_CONFIG": str(remote_config),
            "MAC_RUN_REMOTE_CLAIMS": str(self.root / "remote-claims"),
            "GIT_CONFIG_GLOBAL": str(self.clone_git_config), "GIT_CONFIG_NOSYSTEM": "1",
        })
        process = self.start_mac_run(extra={"MAC_RUN_HOLD_BUILD": "1", "MAC_RUN_CLEANUP_STATUS": str(cleanup_status)})
        try:
            ready = self.stub_state / "build-started"
            wait_until(lambda: ready.exists() or process.poll() is not None, self.stub_state, processes=(process,))
            if process.poll() is not None:
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.fail(output + errors)
            before_local = self.clone_registry_records(local_config)
            before_remote = self.clone_registry_records(str(remote_config))
            self.assertEqual(len(before_local), 1)
            self.assertEqual(len(before_remote), 1)
            self.assertEqual(before_local[0]["path"], before_remote[0]["path"])
            self.assertEqual(before_local[0]["host"], "natemccoy@mac")
            self.assertEqual(before_remote[0]["host"], "localhost")
            prepare_cleanup(Path(str(before_local[0]["path"])))
            send_process_group(process.pid, signal.SIGTERM)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                send_process_group(process.pid, signal.SIGKILL)
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, -signal.SIGTERM, output + errors)
        # The next run cleans the records of the run that the signal ended.
        after = self.mac_run(extra={"MAC_RUN_CLEANUP_STATUS": str(cleanup_status)})
        self.assertEqual(after.returncode, cleanup_status and 4, after.stdout + after.stderr)
        for name, settings, before in (("natedev", local_config, before_local),
                                       ("Mac", str(remote_config), before_remote)):
            with self.subTest(registry=name):
                records = self.clone_registry_records(settings)
                if cleanup_status:
                    # The failed cleanup keeps the ended run's record; the next run keeps its own too.
                    self.assertIn(before[0], records)
                else:
                    self.assertEqual(records, [])

    @process_boundary
    def test_successful_clone_cleanup_removes_both_registry_records(self) -> None:
        self.assert_clone_records_after_cleanup(0)

    def test_old_mirror_cleanup_removes_the_clone_and_both_registry_records(self) -> None:
        checkouts: list[Path] = []

        def remove_exclusions(checkout: Path) -> None:
            checkouts.append(checkout)
            exclude = checkout / ".git/info/exclude"
            patterns = {f"/{name}" for name in floor_claims.MIRROR_METADATA}
            _ = exclude.write_text("\n".join(line for line in exclude.read_text().splitlines()
                                           if line not in patterns) + "\n")
            with mock.patch.dict(os.environ, {"COPIES_CONFIG": self.environment["COPIES_CONFIG"],
                                             "GIT_CONFIG_GLOBAL": str(self.clone_git_config),
                                             "GIT_CONFIG_NOSYSTEM": "1"}):
                status = copies.git(checkout, "status", "--porcelain").decode().splitlines()
            self.assertEqual(set(status), {f"?? {name}" for name in floor_claims.MIRROR_METADATA})

        self.assert_clone_records_after_cleanup(0, prepare_cleanup=remove_exclusions)
        self.assertEqual(len(checkouts), 1)
        self.assertFalse(checkouts[0].exists())

    def test_failed_clone_cleanup_keeps_both_registry_records(self) -> None:
        self.assert_clone_records_after_cleanup(23)

    def test_build_marker_wait_reports_a_failed_child(self) -> None:
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.exit('fixture start failed')"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            with self.assertRaisesRegex(AssertionError, "fixture start failed"):
                self.wait_for_path(self.stub_state / "build-started", process)

    def ended_runs(self) -> list[Path]:
        runs = self.home / ".local/state/mac-queue/runs"
        return sorted(runs.iterdir()) if runs.is_dir() else []

    def test_a_signal_leaves_the_cleanup_to_the_mac_and_the_next_run(self) -> None:
        for signal_number, cleanup_status, whole_group in (
            (signal.SIGTERM, 0, True),
            (signal.SIGHUP, 23, True),
            (signal.SIGTERM, 0, False),
        ):
            with self.subTest(
                signal_number=signal_number, cleanup_status=cleanup_status, whole_group=whole_group
            ):
                self.clear_events()
                for name in ("build-started", "cleanup-called", "checkout-clear", "input-closed", "on-close"):
                    (self.stub_state / name).unlink(missing_ok=True)
                extra = {"MAC_RUN_CLEANUP_STATUS": str(cleanup_status)}
                process = self.start_mac_run(extra={**extra, "MAC_RUN_HOLD_BUILD": "1"})
                try:
                    self.wait_for_path(self.stub_state / "build-started", process)
                    if whole_group:
                        send_process_group(process.pid, signal_number)
                    else:
                        process.send_signal(signal_number)
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                except BaseException:
                    send_process_group(process.pid, signal.SIGKILL)
                    _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    raise
                # No handler: the signal ends the run with its default action.
                self.assertEqual(process.returncode, -signal_number, output + errors)
                self.assertFalse((self.stub_state / "cleanup-called").exists())
                if not whole_group:
                    # The SSH input closes when the run ends, so the Mac work stops at once.
                    wait_until((self.stub_state / "input-closed").exists, self.stub_state)
                # The Mac runs this close step when the SSH input closes.
                checkout = self.environment["MAC_RUN_CHECKOUT"]
                close_step = (self.stub_state / "on-close").read_text()
                self.assertIn("hana_instances.py stop-checkout " + shlex.quote(checkout), close_step)
                self.assertIn("scratch.py end --task gate:mac-run-" + SHA, close_step)
                self.assertLess(close_step.index("stop-checkout"), close_step.index("scratch.py end"))
                self.assertEqual(len(self.ended_runs()), 1)

                # The next run finishes the cleanup of the ended run under its own placement.
                self.clear_events()
                after = self.mac_run(extra=extra)
                stops = [event for event in self.events() if event["command"] == "ssh"
                         and "hana_instances.py stop-checkout " in event["args"][-1]]
                self.assertEqual(len(stops), 2, self.events())
                self.assertTrue(all(stop["claimed"] for stop in stops))
                ends = [event for event in self.events() if event["command"] == "ssh"
                        and "scratch.py end --task " in event["args"][-1]]
                if cleanup_status == 0:
                    self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
                    self.assertEqual(len(ends), 2, self.events())
                    self.assertEqual(self.ended_runs(), [])
                else:
                    self.assertEqual(ends, [], self.events())
                    self.assertIn("cleanup of an ended run failed", after.stderr)
                    self.assertEqual(len(self.ended_runs()), 1)
                    for kept in self.ended_runs():
                        shutil.rmtree(kept)
                self.assert_claim_released()

if __name__ == "__main__":
    _ = unittest.main()
