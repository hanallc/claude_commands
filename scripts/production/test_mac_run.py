#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Exercise mac_run.sh coordination with disposable state and command stand-ins."""

from __future__ import annotations

import json
import os
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import ClassVar, TypedDict, cast, final, override
from scripts.lib.helper_tree_test_support import COPY_HELPERS, FIXTURE_OWNER, copy_helpers
from scripts.lib import build_folders
from scripts.lib.git_test_support import fixture_git_environment

from scripts.direct.wait_for_test_state import wait_until
from scripts.lint.memory_test_support import config_environment, memory_gate_shell_environment, write_config_fixture
from scripts.lib.process_group import send_process_group
from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.lib.shell_test_support import fixture_zsh_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import claim_state


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("mac_run.sh")
MAC_TEST = SCRIPT.parent.parent / "mac_test" / "mac_test.py"
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


COMMAND_STUB = r'''
state=$MAC_RUN_STUB_STATE
command=${0:t}
remote=${@[-1]}
placed_run=0
if [[ $command == ssh && $remote == *smoke-work* ]]; then
    outer=("${(@Q)${(z)remote}}")
    work=${outer[-1]}
    printf '%s\0' "$work" >> "$state/placed-commands"
    words=("${(@Q)${(z)work}}")
    lease=${words[-1]}
    print -r -- "$lease" > "$state/lease-command"
    inner=("${(@Q)${(z)lease}}")
    worker_index=${inner[(Ie)smoke-work]}
    remote=${inner[$((worker_index + 1))]}
    set -- "${@[1,-2]}" "$remote"
    placed_run=1
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
        if (( ${@[(Ie)remote]} && ${@[(Ie)get-url]} )); then
            # Each fixture repository has a distinct origin, so it has a history key.
            folder=${@[$((${@[(Ie)-C]} + 1))]}
            [[ -e $folder/.git ]] || exit 2
            print -r -- "https://example.invalid/${folder:t}.git"
            exit 0
        fi
        if (( ${@[(Ie)rev-parse]} )); then
            code=0
            (( ! ${@[(Ie)--verify]} )) || code=${MAC_RUN_GIT_RESOLVE_STATUS:-0}
            (( code != 0 )) || print -r -- "$MAC_RUN_SHA"
            exit $code
        fi
        if (( ${@[(Ie)bundle]} && ${@[(Ie)create]} )); then
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

if [[ $remote == *mac_smoke_status=* ]]; then
    outer=("${(@Q)${(z)remote}}")
    exec "${outer[@]}"
fi

if (( placed_run )); then
    MAC_PLACEMENT=${words[1]#MAC_PLACEMENT=} exec "$MAC_RUN_PYTHON" -c '
import os, subprocess, sys
from pathlib import Path
state = Path(os.environ["MAC_RUN_STUB_STATE"])
os.setsid()
print("HEAD=" + os.environ["MAC_RUN_SHA"], flush=True)
if os.environ.get("MAC_RUN_CHECKOUT_CLEANUP"):
    os.chdir(os.environ["MAC_RUN_CHECKOUT"])
    subprocess.run([sys.executable, os.environ["MAC_RUN_WORKER"], "smoke-work",
        "touch \"$MAC_RUN_STUB_STATE/build-started\"; cat \"$MAC_RUN_STUB_STATE/hold-build\"",
        "exec \"$MAC_RUN_CHECKOUT_CLEANUP\" cleanup-copy \"$1\" \"$MAC_RUN_LAUNCH\""], check=False)
    sys.exit(0)
if os.environ.get("MAC_RUN_HOLD_BUILD") == "1":
    (state / "build-started").touch()
    _ = sys.stdin.read()
    (state / "input-closed").touch()
    (state / "cleanup-called").touch()
    sys.exit(0)
else:
    (state / "cleanup-called").touch()
    episode = os.environ["MAC_PLACEMENT"].split("/")[0]
    print("mac_smoke_ready=" + episode, flush=True)
    _ = sys.stdin.read()
    (state / "input-closed").touch()
    status_file = Path.home() / ".local/state/mac-queue/smoke" / episode / "status"
    status_file.parent.mkdir(parents=True)
    status_file.write_text("0" + chr(10))
'
fi
if [[ $remote == *'rev-parse --verify HEAD'* ]]; then
    print -r -- "${MAC_RUN_SOURCE_COMMIT:-abcdef1234567890abcdef1234567890abcdef12}"
    exit 0
fi
exit 99
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
    wait_reply = os.environ.get("MAC_RUN_QUEUE_WAIT_REPLY")
    if wait_reply and not (wait_reply == "pending-evidence" and (state / "wait-reply").exists()):
        from unittest.mock import patch
        from scripts.build_queue import admission_client, admission_wire as wire, placement
        from scripts.mac_test import claim_state

        if (state / "wait-reply").exists():
            raise SystemExit("the runner retried the wait reply")
        (state / "wait-reply").touch()
        episode, since = option("--episode"), float(option("--since"))
        destination = placement.PlacementDestination("mac", "mac", "natemccoy@mac", claim_state.WHOLE_MAC)
        if wait_reply in ("busy", "blocked"):
            eligibility = placement.CandidateEligibility(destination, placement.TemporaryExclusion(wait_reply))
            diagnostics = ()
            if wait_reply == "busy" and os.environ.get("MAC_RUN_QUEUE_BUSY_DIAGNOSTIC") != "0":
                diagnostics = (placement.BusyProviderDiagnostic("mac", "unit tests", "2026-11-17T11:00:00+00:00"),)
            if wait_reply == "blocked":
                print("fixture queue warning", file=sys.stderr)
            reply = wire.PlacementWait(episode, since, 1, (eligibility,),
                wire.PlacementResourceWait("mac", wait_reply), provider_diagnostics=diagnostics)
        elif wait_reply == "pending-evidence":
            eligibility = placement.CandidateEligibility(destination, placement.EvidenceNeeded("host evidence is pending"))
            reply = wire.PlacementWait(episode, since, 1, (eligibility,))
        elif wait_reply == "damaged-claim":
            claim_state.state_paths().run.write_text("damaged\n")
            reply = wire.PlacementWait(episode, since, 1, ())
        else:
            raise SystemExit("unknown wait reply in the fixture")
        with patch.object(admission_client, "request_placement", return_value=reply):
            raise SystemExit(admission_client.main(args))
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
    helper_home: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mac-run-helpers-")
        cls.addClassCleanup(temporary.cleanup)
        cls.helper_home = Path(temporary.name)
        _ = copy_helpers(cls.helper_home, *COPY_HELPERS, "scripts/hana_instances", "scripts/accounts",
                         "scripts/whoami", "scripts/notify", "scripts/maintenance", "config")

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
        environment = fixture_git_environment(self.root)
        environment.update({"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
                            "CLAUDE_SESSION_NAME": FIXTURE_OWNER})
        self.enterContext(mock.patch.dict(os.environ, environment, clear=True))
        installed = self.home / ".claude"
        installed.mkdir(parents=True)
        helpers = self.helper_home / ".claude"
        (installed / "scripts").symlink_to(helpers / "scripts", target_is_directory=True)
        _ = shutil.copytree(helpers / "config", installed / "config", symlinks=True)
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
        _ = write_config_fixture(self.config_path, "ci_repo=\nconnect_timeout_s=6\n"
                                 + "block_hours=4\nblock_max_hours=48\nclaim_poll_seconds=60\n")
        self.write_production_config()
        queue = self.root / "fake-queue.py"
        _ = queue.write_text(QUEUE_STUB)
        _ = write_config_fixture(self.root / "config/mac_gate.conf",
            "admission_client_command=" + shlex.join([str(SCRIPT.parents[1] / "lib/py"), str(queue)])
            + "\nterminate_grace_seconds=3\n")
        copies_config = self.config_path.parent / "copies.conf"
        _ = write_config_fixture(copies_config, f"remote_registry={self.root / 'mirrors'}\n"
                                 + "owner.verify=verify\n"
                                 + "git_timeout_seconds=10\nguard_timeout_seconds=10\n")
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
        seed_interpreter_cache(self.root / "cache")
        self.environment = {
            **{name: value for name, value in memory_gate_shell_environment().items() if name not in SESSION_SELECTORS},
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "PYTHONPYCACHEPREFIX": str(self.helper_home / "bytecode"),
            **fixture_zsh_environment(),
            "PATH": f"{self.bin_directory}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(startup),
            "COPIES_CONFIG": str(copies_config),
            "MAC_TEST_STATE_DIR": str(self.state_directory),
            **config_environment(self.config_path.parent),
            "MAC_RUN_STUB_STATE": str(self.stub_state),
            "MAC_RUN_SHA": SHA,
            "MAC_RUN_PYTHON": sys.executable,
            "HANA_TEST_PURPOSE": "Mac launch coordination",
            "TMPDIR": str(self.temp_directory),
        }

        _ = self.environment.pop("CLAUDE_PYTHON", None)
        _ = self.environment.pop("PYTHONDONTWRITEBYTECODE", None)

    def write_production_config(self, **overrides: str) -> None:
        settings = MAC_RUN_CONFIG | overrides
        _ = write_config_fixture(self.config_path.parent / "production.conf",
            "".join(f"{name}={value}\n" for name, value in settings.items())
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

    def assert_placement_wait_cancelled(self) -> None:
        queue_events = [cast(list[str], json.loads(line))
                        for line in (self.stub_state / "queue-events").read_text().splitlines()]
        self.assertEqual([event[0] for event in queue_events], ["place-step", "cancel-placement"])
        self.assertEqual(list((self.home / ".local/state/mac-queue/receipts").iterdir()), [])

    def wait_for_path(self, path: Path, process: subprocess.Popen[str]) -> None:
        wait_until(lambda: path.exists() or process.poll() is not None, path.parent, processes=(process,))
        if process.poll() is not None:
            output, errors = process.communicate()
            self.fail("mac_run exited before the build marker: " + output + errors)
        self.assertTrue(path.exists())

    def test_mac_run_refuses_a_port_above_65535(self) -> None:
        self.write_production_config(mac_run_port="65536")

        result = self.mac_run()

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("mac_run_port='65536': outside range 1..65535", result.stderr)
        self.assert_no_build_started()

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

    def test_queue_busy_reply_exits_nine_without_starting_build(self) -> None:
        for diagnostic in ("1", "0"):
            with self.subTest(diagnostic=diagnostic):
                self.clear_events()
                (self.stub_state / "wait-reply").unlink(missing_ok=True)
                result = self.mac_run(extra={"MAC_RUN_QUEUE_WAIT_REPLY": "busy",
                                             "MAC_RUN_QUEUE_BUSY_DIAGNOSTIC": diagnostic},
                                      timeout=TEST_HANG_GUARD_SECS)

                self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                busy_lines = [line for line in result.stderr.splitlines()
                              if line.startswith("busy:") or line == "Mac is busy"]
                self.assertEqual(len(busy_lines), 1, result.stderr)
                if diagnostic == "1":
                    self.assertRegex(busy_lines[0], r"busy: unit tests since \d\d:\d\d")
                else:
                    self.assertEqual(busy_lines[0], "Mac is busy")
                self.assert_placement_wait_cancelled()
                self.assertEqual([event["args"][-1] for event in self.events() if event["command"] == "ssh"], ["true"])
                self.assert_no_build_started()
                self.assert_claim_released()
                self.assertEqual(list((self.state_directory / "claims").glob("*.json")), [])

    def test_queue_blocked_reply_exits_nine_without_starting_build(self) -> None:
        result = self.mac_run(extra={"MAC_RUN_QUEUE_WAIT_REPLY": "blocked"}, timeout=TEST_HANG_GUARD_SECS)

        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("fixture queue warning", result.stderr.splitlines())
        self.assertEqual(result.stderr.splitlines().count("Mac is blocked"), 1, result.stderr)
        self.assert_placement_wait_cancelled()
        self.assert_no_build_started()
        self.assert_claim_released()

    def test_pending_host_evidence_retries_placement_and_starts_the_build(self) -> None:
        result = self.mac_run(extra={"MAC_RUN_QUEUE_WAIT_REPLY": "pending-evidence",
                                     "MAC_TEST_ADVANCE_WAIT": "1"}, timeout=TEST_HANG_GUARD_SECS)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        queue_events = [cast(list[str], json.loads(line))
                        for line in (self.stub_state / "queue-events").read_text().splitlines()]
        self.assertEqual([event[0] for event in queue_events], ["place-step", "place-step", "cancel-placement"])
        self.assertEqual(queue_events[0], queue_events[1])
        self.assertTrue(any(event["command"] == "ssh" and "cargo build" in event["args"][-1]
                            for event in self.events()))
        self.assertEqual(list((self.home / ".local/state/mac-queue/receipts").iterdir()), [])
        self.assert_claim_released()

    def test_damaged_claim_exits_nine_without_starting_build(self) -> None:
        self.state_directory.mkdir()
        _ = (self.state_directory / "run.json").write_text("damaged\n", encoding="utf-8")

        result = self.mac_run(timeout=TEST_HANG_GUARD_SECS)

        self.assert_damaged_claim_refusal(result)
        self.assertFalse((self.stub_state / "queue-events").exists())

    def test_damaged_claim_during_placement_exits_nine_without_retry(self) -> None:
        self.state_directory.mkdir()

        result = self.mac_run(extra={"MAC_RUN_QUEUE_WAIT_REPLY": "damaged-claim"}, timeout=TEST_HANG_GUARD_SECS)

        self.assert_damaged_claim_refusal(result)
        self.assert_placement_wait_cancelled()

    def assert_damaged_claim_refusal(self, result: subprocess.CompletedProcess[str]) -> None:
        claim = self.state_directory / "run.json"
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertEqual(result.stdout, f"state unreadable: {claim}\n")
        self.assertEqual(list(self.state_directory.iterdir()), [claim])
        self.assertEqual(claim.read_text(), "damaged\n")
        self.assertEqual([event["args"][-1] for event in self.events() if event["command"] == "ssh"], ["true"])
        self.assert_no_build_started()

    def test_smoke_uses_the_shared_refused_port_and_host(self) -> None:
        self.write_production_config(
            smoke_ready_seconds="300", smoke_exit_seconds="30", smoke_connect_seconds="1",
            smoke_call_seconds="5", smoke_signal_grace_seconds="2", smoke_poll_seconds="1",
        )
        _ = write_config_fixture(self.root / "config/hana_shot.conf",
                                 "refused_port=17461\nhost=192.0.2.46\n")
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

    def test_claim_file_keyed_by_repository_lets_another_repository_run_beside_it(self) -> None:
        (self.repository / ".git").mkdir()
        other = self.root / "other"
        (other / ".git").mkdir(parents=True)
        claimed = self.state_command("claim", "--pid", str(os.getpid()), "--what", "other tests",
                                     "--worktree", str(other))
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        claims = self.state_directory / "claims"
        # A Mac claim file takes its name from the repository key.
        other_key = "git-dir:" + str(other.resolve() / ".git")
        other_claim = claims / f"{claim_state.mac_claim_stem(other_key, 0)}.json"
        before = other_claim.read_bytes()
        self.clear_events()

        result = self.mac_run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreater(len([event for event in self.events() if event["command"] in {"ssh", "scp"}]), 1)
        self.assertEqual(sorted(claims.iterdir()), [other_claim])
        self.assertEqual(other_claim.read_bytes(), before)
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
                    and any(action in event["args"][-1] for action in ("fetch ", "checkout ", "cargo "))]
        self.assertTrue(all(event["claimed"] for event in changing))

    def test_commit_on_the_mac_source_uses_the_numbered_copy_without_a_bundle(self) -> None:
        result = self.mac_run(extra={
            "MAC_RUN_SOURCE_COMMIT": SHA,
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
        lease = shlex.split((self.stub_state / "lease-command").read_text())
        self.assertIn("copies.py", Path(lease[2]).name)
        self.assertEqual(lease[3], "run")
        self.assertEqual(lease[lease.index("--commit") + 1], SHA)
        self.assertEqual(lease[lease.index("--repo") + 1], "$HOME/rust/hana")
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
        command = (self.stub_state / "lease-command").read_text()
        arguments = shlex.split(command.splitlines()[0])
        fetched = arguments[arguments.index("--") + 1]
        lease = shlex.split(command)
        self.assertEqual(lease[lease.index("--commit") + 1], SHA)
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

    def test_numbered_copy_launch_keeps_purpose_and_owner(self) -> None:
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
                command = (self.stub_state / "lease-command").read_text()
                words = shlex.split(command)
                lease = words[words.index("exec"):]
                self.assertEqual(lease[3], "run")
                self.assertEqual(lease[lease.index("--owner") + 1], "verify")
                launch = next(event["input"] for event in remote if event["input"])
                line = next(line for line in launch.splitlines() if line.startswith("HANA_TEST_PURPOSE="))
                words = shlex.split(line)
                self.assertIn(f"HANA_TEST_PURPOSE={purpose}", words)
                self.assertIn(f"HANA_TEST_OWNER={owner}", words)
                self.assertIn("$checkout/target/debug/hana", words)
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
                self.assertTrue((self.stub_state / "cleanup-called").exists())
                self.assert_claim_released()

    def install_checkout_cleanup(self, stop_command: str) -> Path:
        installed = self.root / "installed"
        library = installed / "lib"
        library.mkdir(parents=True)
        self.write_executable(library / "py", """\
if [[ ${1:t} != hana_instances.py || $2 != stop-checkout ]]; then
    print -u2 "expected stop-checkout, got $*"
    exit 92
fi
""" + stop_command)
        script = installed / "production/mac_run.sh"
        script.parent.mkdir()
        _ = shutil.copy2(SCRIPT, script)
        return script

    def test_cleanup_keeps_the_copy_when_checkout_clear_is_unconfirmed(self) -> None:
        script = self.install_checkout_cleanup('print "hana-instances: a process still uses the checkout"\n')
        keep = self.root / "copy.keep"
        launch = self.root / "launch.command"
        token = self.repository / "launch-token"
        token.touch()
        launch.touch()
        result = subprocess.run(
            ["zsh", str(script), "cleanup-copy", str(self.repository), str(launch)],
            env={**self.environment, "COPIES_KEEP_FILE": str(keep), "MAC_SMOKE_LAUNCH_TOKEN": str(token)},
            cwd=self.root, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("a process still uses the checkout", keep.read_text())
        self.assertFalse(launch.exists())

    def test_a_launcher_opened_before_cleanup_cannot_start_hana_afterward(self) -> None:
        launch = self.root / "launch.command"
        status = self.root / "status"
        token = self.repository / "launch-token"
        keep = self.root / "copy.keep"
        keep.touch()
        token.touch()
        self.write_production_config(mac_run_launch=str(launch), mac_run_status=str(status))
        result = self.mac_run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        remote = next(event["args"][-1] for event in self.events() if event["input"])
        launch_work = remote.partition("open -a Terminal")[0]
        self.assertNotEqual(launch_work, remote)
        self.write_executable(self.bin_directory / "cargo", "exit 0\n")
        self.write_executable(self.bin_directory / "perl", 'print started > "$HANA_STARTED"\n')
        started = self.root / "hana-started"
        environment = {**self.environment, "MAC_SMOKE_LAUNCH_TOKEN": str(token),
                       "COPIES_KEEP_FILE": str(keep), "HANA_STARTED": str(started)}
        generated = subprocess.run(["zsh", "-c", launch_work], env=environment, cwd=self.repository,
                                   capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        opened = launch.read_text()
        self.assertTrue(opened.startswith("#!/bin/zsh\n"))
        active = subprocess.run(["zsh", "-c", opened], env=environment, cwd=self.root,
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(active.returncode, 0, active.stdout + active.stderr)
        started.unlink()
        script = self.install_checkout_cleanup('print "hana-instances: checkout clear"\n')
        cleared = subprocess.run(["zsh", str(script), "cleanup-copy", str(self.repository), str(launch)],
                                 env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        resumed = subprocess.run(["zsh", "-c", opened], env=environment, cwd=self.root,
                                 capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertFalse(started.exists(), resumed.stdout + resumed.stderr)
        self.assertFalse(launch.exists())
        self.assertFalse(keep.exists())

    def test_hup_keeps_the_numbered_copy_when_stop_checkout_fails(self) -> None:
        script = self.install_checkout_cleanup('print "hana-instances: stop failed"\nexit 1\n')
        build_root = self.root / "build"
        checkout = build_root / "hana/verify/1"
        checkout.mkdir(parents=True)
        keep = build_folders.copy_keep_file(checkout)
        os.mkfifo(self.stub_state / "hold-build")
        process = self.start_mac_run(extra={
            "MAC_RUN_CHECKOUT_CLEANUP": str(script),
            "MAC_RUN_CHECKOUT": str(checkout),
            "MAC_RUN_WORKER": str(SCRIPT.parents[1] / "mac_test/mac_queue_run.py"),
            "MAC_RUN_LAUNCH": str(self.root / "launch.command"),
            "COPIES_KEEP_FILE": str(keep),
        })
        try:
            self.wait_for_path(self.stub_state / "build-started", process)
            send_process_group(process.pid, signal.SIGHUP)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        except BaseException:
            send_process_group(process.pid, signal.SIGKILL)
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            raise
        self.assertEqual(process.returncode, -signal.SIGHUP, output + errors)
        self.assertIn("stop failed", keep.read_text())
        with mock.patch.object(build_folders, "build_root", return_value=build_root):
            self.assertEqual(build_folders.build_folder_state(checkout), "kept")
        self.assertEqual(list(checkout.glob(".mac-smoke-launch-*")), [])

    def test_build_marker_wait_reports_a_failed_child(self) -> None:
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.exit('fixture start failed')"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            with self.assertRaisesRegex(AssertionError, "fixture start failed"):
                self.wait_for_path(self.stub_state / "build-started", process)

    def ended_runs(self) -> list[Path]:
        runs = self.home / ".local/state/mac-queue/runs"
        return sorted(runs.iterdir()) if runs.is_dir() else []

    def test_a_signal_closes_ssh_and_the_next_run_removes_the_local_bundle(self) -> None:
        process = self.start_mac_run(extra={"MAC_RUN_HOLD_BUILD": "1"})
        try:
            self.wait_for_path(self.stub_state / "build-started", process)
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        except BaseException:
            send_process_group(process.pid, signal.SIGKILL)
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            raise
        self.assertEqual(process.returncode, -signal.SIGTERM, output + errors)
        wait_until((self.stub_state / "cleanup-called").exists, self.stub_state)
        self.assertTrue((self.stub_state / "input-closed").exists())
        self.assertEqual(len(self.ended_runs()), 1)
        self.assertEqual(len(list(self.temp_directory.glob("*.bundle"))), 1)

        after = self.mac_run()
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertEqual(self.ended_runs(), [])
        self.assertEqual(list(self.temp_directory.glob("*.bundle")), [])
        self.assert_claim_released()

if __name__ == "__main__":
    _ = unittest.main()
