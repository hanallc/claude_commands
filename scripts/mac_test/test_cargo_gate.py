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

from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


GATE = Path(__file__).with_name("cargo_gate") / "bin"
PROGRAM = '''import json, os, subprocess, sys
from pathlib import Path
root = Path(os.environ["GATE_TEST_ROOT"])
records = root / ".local/state/mac-gate/records"
with (root / "runs").open("a") as stream:
    stream.write(json.dumps({"argv":sys.argv[1:], "path":os.environ["PATH"],
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
        if not answer.name.endswith(".timeout"):
            answer.with_name(answer.name + ".timeout").write_text("timeout")
if mode == "broken":
    print("not a tab record")
else:
    state, target, reason, status = {
        "closed": ("closed", "mac", "placement episode/attempt closed", 1),
        "unknown": ("unknown", "mac", "placement not found", 1),
        "unreachable": ("unknown", "mac", "service unavailable", 2),
        "linux": ("closed", "linux", "target is linux", 1),
    }.get(mode, ("current", "mac", "", 0))
    print("\\t".join((state, target, "fixture", "fixture", "claim", reason)))
    raise SystemExit(status)
'''


class CargoGateTests(unittest.TestCase):
    def fixture(self, root: Path) -> dict[str, str]:
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
        _ = ps.write_text('#!/bin/zsh -f\nprint -r -- \"$*\" >> \"$GATE_TEST_ROOT/ps-calls\"\nif [[ ${@[-1]} == lstart= ]]; then\n'
                          + ' kill -0 "$2" 2>/dev/null || exit 1\n print "Mon Nov  9 12:00:00 2026"\nelse\n /bin/ps "$@"\nfi\n')
        ps.chmod(0o755)
        _ = write_config_fixture(root / "config/mac_gate.conf",
            "placement_check_command=" + shlex.join([sys.executable, str(check)])
            + "\nplacement_check_timeout_seconds=1\nplacement_recheck_seconds=15\n")
        ssh = fake / "ssh"
        _ = ssh.write_text(f"#!{sys.executable}\n" + "import json, os, sys\nfrom pathlib import Path\n"
            + "root = Path(os.environ['GATE_TEST_ROOT'])\n"
            + "(root / 'ssh-arguments').write_text(json.dumps(sys.argv[1:]))\n"
            + "remote_at = sys.argv.index('natedev') + 1\n"
            + "os.execv(sys.executable, [sys.executable, str(root / 'check.py'), *sys.argv[remote_at+1:]])\n")
        ssh.chmod(0o755)
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("CLAUDECODE", "CODEX_HOME", "SSH_CONNECTION", "SSH_CLIENT", "MAC_PLACEMENT", "MAC_GATE_RECORD")}
        environment.update(config_environment(root / "config"))
        environment.update(HOME=str(root), GATE_TEST_ROOT=str(root), GATE_TEST_BIN=str(GATE),
            PATH=os.pathsep.join((str(GATE), str(fake), str(real), "/usr/bin", "/bin")))
        return environment

    def run_gate(self, environment: dict[str, str], *arguments: str, name: str = "cargo") -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(GATE / name), *arguments], env=environment, capture_output=True, text=True,
                              timeout=TEST_HANG_GUARD_SECS)

    def test_terminal_and_placed_calls_keep_arguments_status_and_root_records(self) -> None:
        for placement, readable in ((False, True), (False, False), (True, True)):
            for name in ("cargo", "rustc", "cargo-nextest"):
                with self.subTest(placement=placement, readable=readable, name=name), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    environment = self.fixture(root)
                    environment["REAL_STATUS"] = "23"
                    if placement:
                        _ = write_config_fixture(root / "config/mac_gate.conf",
                            (Path(__file__).resolve().parents[2] / "config/mac_gate.conf").read_text()
                            .replace("placement_check_timeout_seconds=10", "placement_check_timeout_seconds=1"))
                        environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt")
                    if not readable:
                        (root / "config/mac_gate.conf").unlink()
                    result = self.run_gate(environment, "quoted value", "", name=name)
                    self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
                    rows = [cast(dict[str, object], json.loads(line)) for line in (root / "runs").read_text().splitlines()]
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

    def test_agent_markers_and_failed_checks_refuse_without_starting_a_program(self) -> None:
        cases = [(marker, "", "current", "no placement")
                 for marker in ("CLAUDECODE", "CODEX_HOME", "SSH_CONNECTION", "SSH_CLIENT")]
        cases += [("SSH_CONNECTION", "episode/attempt", mode, reason) for mode, reason in (
            ("closed", "placement episode/attempt closed"), ("linux", "target is linux"),
            ("slow", "no answer in 1s"), ("broken", "no answer in 1s"),
            ("unknown", "placement not found"), ("unreachable", "no answer in 1s"))]
        cases += [("SSH_CONNECTION", value, "current", "no placement") for value in
                  ("episode/a;touch injected", "e$(id)/a", "episode/a/b", "episode/a b")]
        for marker, placement, mode, reason in cases:
            with self.subTest(marker=marker, mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = {**self.fixture(root), marker: "1", "MAC_PLACEMENT": placement, "CHECK_MODE": mode}
                result = self.run_gate(environment)
                self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
                self.assertEqual(result.stderr, f"mac cargo gate refused: {reason}; run it through mac_queue_run.py\n")
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
            rows = [cast(dict[str, object], json.loads(line)) for line in (root / "runs").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["record"], rows[1]["record"])
            self.assertEqual(len(cast(list[str], rows[1]["records"])), 1)

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
