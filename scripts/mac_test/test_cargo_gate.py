"""Exercise the Mac gate with local programs and placement checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import cast

from scripts.lib.helper_tree_test_support import copy_helpers
from scripts.lib.shell_test_support import shell_functions
from scripts.mac_test.mac_ci_test_support import BUSY_PROCESS_LIST

from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


GATE = Path(__file__).with_name("cargo_gate") / "bin"
FIXTURE_WORKER_BUDGET = "3"
PROGRAM = '''import json, os, subprocess, sys
from pathlib import Path
root = Path(os.environ["GATE_TEST_ROOT"])
records = root / ".local/state/mac-gate/records"
with (root / "runs").open("a") as stream:
    stream.write(json.dumps({"argv":sys.argv[1:], "path":os.environ["PATH"],
        "cargo_jobs":os.environ.get("CARGO_BUILD_JOBS"),
        "nextest_threads":os.environ.get("NEXTEST_TEST_THREADS"),
        "record":os.environ.get("MAC_GATE_RECORD"),
        "records":[path.read_text() for path in records.iterdir()]}) + "\\n")
if sys.argv[1:] == ["child"]:
    raise SystemExit(subprocess.call([os.environ["GATE_TEST_BIN"] + "/rustc", "quoted value"]))
raise SystemExit(int(os.environ.get("REAL_STATUS", "0")))
'''
CHECK = '''import os, sys, time
from pathlib import Path
root = Path(os.environ["GATE_TEST_ROOT"])
with (root / "checks").open("a") as stream:
    stream.write("check\\n")
mode = os.environ.get("CHECK_MODE", "current")
if mode == "slow":
    time.sleep(10)
if mode == "race":
    for answer in (root / ".local/state/mac-gate").glob("answer-*"):
        if not answer.name.endswith((".timeout", ".errors")):
            answer.with_name(answer.name + ".timeout").write_text("timeout")
if mode == "broken":
    print("not a tab record")
elif mode in ("unreachable", "silent-failure"):
    if mode == "unreachable":
        print("memory admission check failed (no socket); step refused", file=sys.stderr)
        print("admission service unavailable: no socket", file=sys.stderr)
    raise SystemExit(2)
else:
    state, target, reason, status = {
        "closed": ("closed", "mac", "placement episode/attempt closed", 1),
        "unknown": ("unknown", "mac", "placement not found", 1),
        "linux": ("closed", "linux", "target is linux", 1),
    }.get(mode, ("current", "mac", "", 0))
    print("\\t".join((state, target, "fixture", "fixture", "claim", reason)))
    raise SystemExit(status)
'''


class CargoGateTests(unittest.TestCase):
    def fixture(self, root: Path, *, process_list: str = "") -> dict[str, str]:
        real = root / "real"
        fake = root / "fake"
        real.mkdir()
        fake.mkdir()
        for name in ("mkdir", "mv", "rm"):
            executable = shutil.which(name)
            assert executable is not None
            (fake / name).symlink_to(executable)
        for name in ("cargo", "rustc", "cargo-nextest"):
            program = real / name
            _ = program.write_text(f"#!{sys.executable}\n" + PROGRAM)
            program.chmod(0o755)
        check = root / "check.py"
        _ = check.write_text(CHECK)
        parser = fake / "python3"
        _ = parser.write_text("#!/bin/zsh -f\nprint parser >> \"$GATE_TEST_ROOT/parsers\"\nexec "
                              + shlex.quote(sys.executable) + ' "$@"\n')
        parser.chmod(0o755)
        ps = fake / "ps"
        list_processes = f"printf '%s' {shlex.quote(process_list)}" if process_list else '/bin/ps "$@"'
        _ = ps.write_text('#!/bin/zsh -f\nprint -r -- \"$*\" >> \"$GATE_TEST_ROOT/ps-calls\"\nif [[ ${@[-1]} == lstart= ]]; then\n'
                          + ' kill -0 "$2" 2>/dev/null || exit 1\n print "Mon Nov  9 12:00:00 2026"\n'
                          + f'else\n {list_processes}\nfi\n')
        ps.chmod(0o755)
        _ = write_config_fixture(root / "config/mac_gate.conf",
            "placement_check_command=" + shlex.join([sys.executable, str(check)])
            + "\nplacement_check_timeout_seconds=1\nplacement_recheck_seconds=15\nrefusal_exit_code=125\n"
            + f"worker_budget={FIXTURE_WORKER_BUDGET}\n")
        ssh = fake / "ssh"
        _ = ssh.write_text(f"#!{sys.executable}\n" + "import json, os, sys\nfrom pathlib import Path\n"
            + "root = Path(os.environ['GATE_TEST_ROOT'])\n"
            + "(root / 'ssh-arguments').write_text(json.dumps(sys.argv[1:]))\n"
            + "remote_at = sys.argv.index('natedev') + 1\n"
            + "os.execv(sys.executable, [sys.executable, str(root / 'check.py'), *sys.argv[remote_at+1:]])\n")
        ssh.chmod(0o755)
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("CLAUDECODE", "CODEX_HOME", "SSH_CONNECTION", "SSH_CLIENT", "MAC_PLACEMENT",
                                      "MAC_GATE_RECORD", "CARGO_BUILD_JOBS", "NEXTEST_TEST_THREADS")}
        environment.update(config_environment(root / "config"))
        environment.update(HOME=str(root), GATE_TEST_ROOT=str(root), GATE_TEST_BIN=str(GATE),
            PATH=os.pathsep.join((str(GATE), str(fake), str(real), "/usr/bin", "/bin")))
        return environment

    def run_gate(self, environment: dict[str, str], *arguments: str, name: str = "cargo") -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(GATE / name), *arguments], env=environment, capture_output=True, text=True,
                              timeout=TEST_HANG_GUARD_SECS)

    def runs(self, root: Path) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in (root / "runs").read_text().splitlines()]

    def test_copied_gate_links_find_the_copied_configuration_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            copied = copy_helpers(root, "scripts/mac_test")
            gate_bin = copied / "mac_test/cargo_gate/bin"
            _ = write_config_fixture(root / "config/mac_gate.conf", "refusal_exit_code=83\n")
            environment.update(CLAUDECODE="1", GATE_TEST_BIN=str(gate_bin),
                               PATH=os.pathsep.join((str(gate_bin), str(root / "fake"), str(root / "real"),
                                                    "/usr/bin", "/bin")))
            self.assertFalse((copied / "mac_test/test_cargo_gate.py").exists())
            for name in ("cargo", "cargo-nextest", "rustc"):
                with self.subTest(program=name):
                    command = gate_bin / name
                    self.assertTrue(command.is_symlink())
                    self.assertEqual(os.readlink(command), "../gate.zsh")
                    self.assertTrue(command.resolve().is_relative_to(root))
                    result = subprocess.run([str(command)], env=environment, capture_output=True, text=True,
                                            timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(result.returncode, 83, result.stdout + result.stderr)
                    self.assertIn("mac cargo gate refused: no placement", result.stderr)

    def test_terminal_and_placed_calls_keep_arguments_status_and_root_records(self) -> None:
        for placement, readable in ((False, True), (False, False), (True, True)):
            for name in ("cargo", "rustc", "cargo-nextest"):
                with self.subTest(placement=placement, readable=readable, name=name), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    environment = self.fixture(root)
                    environment["REAL_STATUS"] = "23"
                    if placement:
                        _ = write_config_fixture(root / "config/mac_gate.conf",
                            "placement_check_command=ssh -o BatchMode=yes -o ConnectTimeout=6 natedev "
                            + "'~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/admission_client.py placement-status'\n"
                            + "placement_check_timeout_seconds=1\n")
                        environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt")
                    if not readable:
                        (root / "config/mac_gate.conf").unlink()
                    result = self.run_gate(environment, "quoted value", "", name=name)
                    self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
                    rows = self.runs(root)
                    self.assertEqual(rows[0]["argv"], ["quoted value", ""])
                    self.assertNotIn(str(GATE), str(rows[0]["path"]).split(os.pathsep))
                    self.assertIn(str(root / "real"), str(rows[0]["path"]).split(os.pathsep))
                    records = cast(list[str], rows[0]["records"])
                    self.assertEqual(len(records), 1)
                    pid, stamp, label = records[0].strip().split("\t")
                    self.assertEqual(pid, rows[0]["record"])
                    self.assertEqual(stamp, "Mon Nov 9 12:00:00 2026")
                    self.assertEqual(label, "placement episode/attempt" if placement else "user")
                    self.assertEqual((root / "checks").exists(), placement)
                    if not placement:
                        self.assertEqual(len((root / "ps-calls").read_text().splitlines()), 1)
                        self.assertFalse((root / ".local/state/mac-gate/checks").exists())
                    if placement:
                        ssh_arguments = cast(list[str], json.loads((root / "ssh-arguments").read_text()))
                        self.assertEqual(ssh_arguments[ssh_arguments.index("natedev") + 1],
                            "~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/admission_client.py placement-status")
                    self.assertEqual(next((root / ".local/state/mac-gate/records").iterdir()).stat().st_mode & 0o777, 0o600)

    def test_terminal_bypasses_the_limit_during_ci(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root, process_list=BUSY_PROCESS_LIST)
            environment.update(CARGO_BUILD_JOBS="12", NEXTEST_TEST_THREADS="13")
            _ = write_config_fixture(root / "config/mac_test.conf", "max_active_builds=1\n")
            result = self.run_gate(environment, "build", "--jobs=15")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            row, = self.runs(root)
            self.assertEqual(row["argv"], ["build", "--jobs=15"])
            self.assertEqual(row["cargo_jobs"], "12")
            self.assertEqual(row["nextest_threads"], "13")
            self.assertIn("\tuser\n", cast(list[str], row["records"])[0])
            calls = (root / "ps-calls").read_text().splitlines()
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].endswith(" -o lstart="))
            self.assertFalse((root / "checks").exists())
            self.assertFalse((root / "ssh-arguments").exists())

    def test_placed_cargo_caps_jobs_and_preserves_smaller_values(self) -> None:
        budget = FIXTURE_WORKER_BUDGET
        for caller_jobs, arguments, jobs, expected in (
            ("", ("build",), budget, ("build",)),
            ("2", ("build", "-j1"), "2", ("build", "-j1")),
            ("12", ("doc", "--jobs", "8"), budget, ("doc", "--jobs", budget)),
        ):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = self.fixture(root)
                environment.update(CLAUDECODE="1", MAC_PLACEMENT="episode/attempt")
                if caller_jobs:
                    environment["CARGO_BUILD_JOBS"] = caller_jobs
                result = self.run_gate(environment, *arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row, = self.runs(root)
                self.assertEqual(row["cargo_jobs"], jobs)
                self.assertEqual(row["argv"], list(expected))

    def test_placed_nextest_caps_build_and_test_width_before_the_separator(self) -> None:
        budget = FIXTURE_WORKER_BUDGET
        for name, requested, threads, arguments, expected in (
            ("cargo", "", budget, (), ()),
            ("cargo", "10", budget, ("--test-threads", "12"), ("--test-threads", budget)),
            ("cargo-nextest", "2", "2", ("--build-jobs=8", "--jobs=1"), (f"--build-jobs={budget}", "--jobs=1")),
            ("cargo-nextest", "2", "2", ("-j12",), (f"-j{budget}",)),
        ):
            with self.subTest(name=name, arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = self.fixture(root)
                environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt")
                if requested:
                    environment["NEXTEST_TEST_THREADS"] = requested
                tail = ("--", "--test-threads", "99")
                result = self.run_gate(environment, "nextest", "run", *arguments, *tail, name=name)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row, = self.runs(root)
                self.assertEqual(row["cargo_jobs"], budget)
                self.assertEqual(row["nextest_threads"], threads)
                self.assertEqual(row["argv"], ["nextest", "run", *expected, *tail])

    def test_placed_nextest_resolves_cpu_relative_widths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            getconf = root / "fake/getconf"
            _ = getconf.write_text('#!/bin/sh\n[ "$1" = _NPROCESSORS_ONLN ] && printf "12\\n"\n')
            getconf.chmod(0o755)
            environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt",
                               CARGO_BUILD_JOBS="-10", NEXTEST_TEST_THREADS="num-cpus")
            result = self.run_gate(environment, "nextest", "run", "--test-threads=-1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            row, = self.runs(root)
            self.assertEqual(row["cargo_jobs"], "-10")
            self.assertEqual(row["nextest_threads"], FIXTURE_WORKER_BUDGET)
            self.assertEqual(row["argv"], ["nextest", "run", f"--test-threads={FIXTURE_WORKER_BUDGET}"])

    def test_mend_fix_and_follow_up_clippy_use_the_placed_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt")
            lint = GATE.parents[2] / "lint"
            invoke = root / ".claude/scripts/lint/invoke.sh"
            invoke.parent.mkdir(parents=True)
            _ = invoke.write_text(
                'run() { "$@"; }\nlint_config_enabled() { return 0; }\n'
                + shell_functions(lint / "invoke.sh", "invoke_mend", "invoke_clippy")
            )
            result = subprocess.run(["bash", str(lint / "lint"), "mend-fix", "--workspace"],
                                    env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            mend, clippy = self.runs(root)
            self.assertEqual(mend["argv"], ["mend", "--all-targets", "--fix", "--fail-on-warn", "--workspace"])
            self.assertEqual(clippy["argv"], ["clippy", "--workspace", "--all-targets", "--all-features", "--", "-D", "warnings"])
            self.assertEqual(mend["cargo_jobs"], FIXTURE_WORKER_BUDGET)
            self.assertEqual(clippy["cargo_jobs"], FIXTURE_WORKER_BUDGET)

    def test_agent_markers_and_failed_checks_refuse_without_starting_a_program(self) -> None:
        route = "; run it through mac_queue_run.py"
        cases = [(marker, "", "current", "no placement" + route)
                 for marker in ("CLAUDECODE", "CODEX_HOME", "SSH_CONNECTION", "SSH_CLIENT")]
        # A failed check names its own cause: a timeout only when the timer ended it.
        cases += [("SSH_CONNECTION", "episode/attempt", mode, reason) for mode, reason in (
            ("closed", "placement episode/attempt closed" + route), ("linux", "target is linux" + route),
            ("slow", "no answer in 1s"), ("broken", "invalid placement answer"),
            ("unknown", "placement not found" + route),
            ("unreachable", "placement check failed with status 2: admission service unavailable: no socket"),
            ("silent-failure", "placement check failed with status 2"))]
        cases += [("SSH_CONNECTION", value, "current", "no placement" + route) for value in
                  ("episode/a;touch injected", "e$(id)/a", "episode/a/b", "episode/a b")]
        for marker, placement, mode, reason in cases:
            with self.subTest(marker=marker, mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = {**self.fixture(root), marker: "1", "MAC_PLACEMENT": placement, "CHECK_MODE": mode}
                result = self.run_gate(environment)
                self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
                self.assertEqual(result.stderr, f"mac cargo gate refused: {reason}\n")
                self.assertFalse((root / "runs").exists())
                if reason == "no placement":
                    self.assertFalse((root / "checks").exists())
                self.assertEqual(list((root / ".local/state/mac-gate").glob("answer-*")), [])

    def test_cached_calls_start_no_parser_and_expired_calls_check_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**self.fixture(root), "CODEX_HOME": "fake", "MAC_PLACEMENT": "episode/attempt", "CHECK_MODE": "race"}
            state = root / ".local/state/mac-gate"
            state.mkdir(parents=True)
            stale = state / "answer-stale.timeout"
            _ = stale.write_text("timeout")
            os.utime(stale, (0, 0))
            for index in range(3):
                if index == 2:
                    cache = next((root / ".local/state/mac-gate/checks").glob("*.live"))
                    _ = cache.write_text("0\tepisode/attempt\n")
                result = self.run_gate(environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = 1 if index < 2 else 2
                self.assertEqual(len((root / "checks").read_text().splitlines()), expected)
                self.assertFalse((root / "parsers").exists())
                self.assertEqual(list(state.glob("answer-*")), [])

    def test_child_uses_the_recorded_ancestor_without_another_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**self.fixture(root), "CLAUDECODE": "1", "MAC_PLACEMENT": "episode/attempt"}
            result = self.run_gate(environment, "child")
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = self.runs(root)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["record"], rows[1]["record"])
            self.assertEqual(len(cast(list[str], rows[1]["records"])), 1)
            self.assertEqual(rows[1]["cargo_jobs"], FIXTURE_WORKER_BUDGET)
            self.assertEqual(rows[1]["argv"], ["quoted value"])

    def test_missing_program_reports_the_name_without_recursion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            (root / "real/cargo").unlink()
            result = self.run_gate(environment)
            self.assertEqual(result.returncode, 127)
            self.assertEqual(result.stderr, "mac cargo gate: real cargo not found on PATH\n")


if __name__ == "__main__":
    _ = unittest.main()
