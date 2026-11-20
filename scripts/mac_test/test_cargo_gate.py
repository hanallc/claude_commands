"""Exercise the Mac gate with local programs and placement checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import socket
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
from scripts.mac_test.mac_gate_records import MacGateRecord, MacProcessIdentity


GATE = Path(__file__).with_name("cargo_gate") / "bin"
FIXTURE_WORKER_BUDGET = "3"
FIXTURE_STARTED_US = 1_793_280_000_123_456
REGISTER_PYTHON = '''import errno, fcntl, io, json, os, subprocess, sys
from contextlib import closing, contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

root = Path(os.environ["GATE_TEST_ROOT"])
if sys.argv[1] == "-B" and Path(sys.argv[2]).name == "register.py":
    from unittest.mock import patch

    path = Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(path.parents[3]))
    from scripts.lib import darwin_process
    from scripts.lib.darwin_process_test_support import native_process_read, write_process_info
    from scripts.mac_test import mac_gate_records
    from scripts.mac_test.cargo_gate import register
    from scripts.mac_test.mac_gate_records import MacGateRecord, MacProcessIdentity

    failures = json.loads(os.environ.get("GATE_TEST_PROBE_FAILURES", "{}"))
    peer_entries = json.loads(os.environ.get("GATE_TEST_PEER_ENTRIES", "[]"))
    record_race = os.environ.get("GATE_TEST_RECORD_RACE", "")
    peer_pid = os.environ.get("GATE_TEST_RACE_PEER_PID")
    original_flock = fcntl.flock
    original_kill = os.kill
    original_open = Path.open
    original_scandir = os.scandir

    def register_contender():
        # Try the competing registration once at this exact point in the race.
        def try_flock(descriptor, operation):
            if operation == fcntl.LOCK_EX:
                operation |= fcntl.LOCK_NB
            original_flock(descriptor, operation)

        with patch.object(fcntl, "flock", try_flock), redirect_stdout(io.StringIO()):
            try:
                register.register(os.getpid(), "", "placement episode/contender")
            except BlockingIOError:
                outcome = "deferred"
            except register.RootRefusal as error:
                outcome = str(error)
            else:
                outcome = "registered"
        (root / "contender").write_text(outcome)

    def open_record(file, mode="r", *args, **kwargs):
        global record_race
        stream = original_open(file, mode, *args, **kwargs)
        if record_race == "publish" and mode == "x":
            record_race = ""
            try:
                register_contender()
            except BaseException:
                stream.close()
                raise
        return stream

    def refuse_peer(identity):
        from scripts.lib.shell_test_support import shell_function

        source = path.with_name("gate.zsh").read_text().replace("\\n    ", "\\n")
        refuse = shell_function(source, "refuse")
        state = mac_gate_records.mac_gate_state()
        record = state / "records" / identity.key
        pipe = state / (register.JOBSERVER_PREFIX + identity.key)
        result = subprocess.run([
            "/bin/zsh", "-f", "-c",
            'source "$3"\\nroot_record=$1 fifo=$2\\n' + refuse + "\\nrefuse 'fixture peer refused' ''",
            "refuse-peer", str(record), str(pipe), str(path.parents[2] / "lib/conf.sh"),
        ], capture_output=True, text=True)
        assert result.returncode and result.stderr == "mac cargo gate refused: fixture peer refused\\n", result

    def read(pid, flavor, argument, buffer, size):
        assert (flavor, argument) == (darwin_process.PROC_PIDTBSDINFO, 1)
        with (root / "probes").open("a") as stream:
            stream.write(f"{pid}\\n")
        if str(pid) in failures:
            return size - 1
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "ppid=", "-o", "state="],
                                text=True, capture_output=True)
        if result.returncode:
            return 0
        parent, state = result.stdout.split()
        status = darwin_process.SZOMB if state.startswith("Z") else 2
        info = darwin_process.DarwinProcessInfo(pid, int(parent), FIXTURE_STARTED_US, status, 0)
        if str(pid) == peer_pid:
            if record_race == "withdraw":
                refuse_peer(MacProcessIdentity(pid, FIXTURE_STARTED_US))
            if record_race == "cleanup":
                # The contender sees the owner end after this probe found it alive.
                failures[str(pid)] = "ended"
                register_contender()
        return write_process_info(buffer, size, info)

    def check_pid(pid, signal):
        assert signal == 0
        if str(pid) == os.environ.get("GATE_TEST_LEGACY_REUSE"):
            record = MacGateRecord(MacProcessIdentity(pid, FIXTURE_STARTED_US), "placement episode/attempt")
            (mac_gate_records.mac_gate_state() / "records" / record.identity.key).write_text(record.line())
            raise ProcessLookupError(errno.ESRCH, "old owner ended")
        match failures.get(str(pid)):
            case "denied":
                raise PermissionError(errno.EPERM, "cannot check the PID")
            case "ended":
                raise ProcessLookupError(errno.ESRCH, "owner ended")
            case _:
                original_kill(pid, signal)

    @contextmanager
    def scan(directory):
        relative, failure = os.environ.get("GATE_TEST_SCAN_FAILURE", ":").split(":")
        selected = relative and Path(directory) == mac_gate_records.mac_gate_state() / relative
        if selected and failure == "open":
            raise OSError("cannot open the directory")

        # Open the directory at iteration so the next read sees the published peer.
        def read_entries():
            with original_scandir(directory) as entries:
                if selected and failure == "iterate":
                    for entry in entries:
                        if entry.name == os.environ["GATE_TEST_ENDED_ENTRY"]:
                            yield entry
                            raise OSError("cannot continue reading the directory")
                    raise AssertionError("the scan fixture has no ended entry")
                yield from entries
            if peer_entries:
                record, pipe = map(Path, peer_entries)
                state = mac_gate_records.mac_gate_state()
                record.rename(state / "records" / record.name)
                pipe.rename(state / pipe.name)
                peer_entries.clear()

        with closing(read_entries()) as entries:
            yield entries

    with (root / "registrations").open("a") as stream:
        stream.write("register\\n")
    sys.argv = sys.argv[2:]
    with (patch.object(mac_gate_records.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(root))),
          patch.object(sys, "platform", "darwin"), native_process_read(read),
          patch.object(os, "kill", check_pid), patch.object(os, "scandir", scan),
          patch.object(Path, "open", open_record)):
        raise SystemExit(register.main())
if sys.argv[1:3] != ["-I", "-c"]:
    with (root / "parsers").open("a") as stream:
        stream.write("parser\\n")
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
'''
PROGRAM = '''import json, os, shutil, socket, stat, subprocess, sys, tomllib
from pathlib import Path

def hold_jobserver():
    with socket.socket(fileno=int(os.environ["GATE_TEST_ROOT_EVENT"])) as channel:
        channel.sendall(b"r")
        assert channel.recv(1) == b"+"

root = Path(os.environ["GATE_TEST_ROOT"])
arguments = sys.argv[1:]
if Path(sys.argv[0]).name == "cargo" and arguments:
    home = Path(os.environ.get("CARGO_HOME") or root / ".cargo")
    home = Path.cwd() / home
    config = home / "config.toml"
    if config.exists():
        alias = tomllib.loads(config.read_text()).get("alias", {}).get(arguments[0])
        if alias:
            arguments = alias.split() + arguments[1:]
    if arguments[0] in ("nextest", "ntr"):
        search = os.environ["PATH"].split(os.pathsep)
        if str(home / "bin") not in search:
            search.insert(0, str(home / "bin"))
        nextest = shutil.which(f"cargo-{arguments[0]}", path=os.pathsep.join(search))
        assert nextest is not None
        os.execv(nextest, [nextest, *arguments])
records = root / ".local/state/mac-gate/records"
flags = os.environ.get("CARGO_MAKEFLAGS")
pool = ()
tokens = b""
inode = None
if "MAC_GATE_JOBSERVER" in os.environ:
    pool = tuple(map(int, flags.rsplit("--jobserver-auth=", 1)[1].split(",")))
    reader, writer = pool
    read_info, write_info = os.fstat(reader), os.fstat(writer)
    assert stat.S_ISFIFO(read_info.st_mode) and stat.S_ISFIFO(write_info.st_mode)
    assert read_info.st_ino == write_info.st_ino
    inode = read_info.st_ino
    os.set_blocking(reader, False)
    try:
        tokens = os.read(reader, 4096)
    except BlockingIOError:
        pass
    finally:
        os.set_blocking(reader, True)
    assert os.write(writer, tokens) == len(tokens)
with (root / "runs").open("a") as stream:
    stream.write(json.dumps({"argv":sys.argv[1:], "path":os.environ["PATH"],
        "cargo_jobs":os.environ.get("CARGO_BUILD_JOBS"),
        "cargo_makeflags":flags, "makeflags":os.environ.get("MAKEFLAGS"), "mflags":os.environ.get("MFLAGS"),
        "jobserver_inode":inode, "jobserver_tokens":len(tokens),
        "nextest_threads":os.environ.get("NEXTEST_TEST_THREADS"),
        "record":os.environ.get("MAC_GATE_RECORD"),
        "records":[path.read_text() for path in records.iterdir()]}) + "\\n")
if arguments == ["hold-root"]:
    hold_jobserver()
elif arguments == ["leave-child"] and os.fork() == 0:
    with open(os.devnull, "w") as discard:
        os.dup2(discard.fileno(), 1)
        os.dup2(discard.fileno(), 2)
    hold_jobserver()
    os._exit(0)
if sys.argv[1:] == ["child"]:
    raise SystemExit(subprocess.call([os.environ["GATE_TEST_BIN"] + "/rustc", "quoted value"], pass_fds=pool))
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
        for name in ("getconf", "mkdir", "mkfifo", "mv", "rm"):
            executable = shutil.which(name)
            assert executable is not None
            (fake / name).symlink_to(executable)
        for name in ("cargo", "rustc", "cargo-nextest"):
            program = real / name
            _ = program.write_text(f"#!{sys.executable}\n" + PROGRAM)
            program.chmod(0o755)
        check = root / "check.py"
        _ = check.write_text(CHECK)
        python = fake / "python3"
        _ = python.write_text(f"#!{sys.executable}\nFIXTURE_STARTED_US = {FIXTURE_STARTED_US}\n" + REGISTER_PYTHON)
        python.chmod(0o755)
        ps = fake / "ps"
        list_processes = f"printf '%s' {shlex.quote(process_list)}" if process_list else '/bin/ps "$@"'
        _ = ps.write_text('#!/bin/zsh -f\nprint -r -- "$*" >> "$GATE_TEST_ROOT/ps-calls"\n'
                          + list_processes + "\n")
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
                                      "MAC_GATE_RECORD", "MAC_GATE_JOBSERVER", "CARGO_BUILD_JOBS", "CARGO_HOME",
                                      "CARGO_MAKEFLAGS", "MAKEFLAGS", "MFLAGS", "NEXTEST_TEST_THREADS")}
        environment.update(config_environment(root / "config"))
        environment.update(HOME=str(root), CLAUDE_PYTHON=str(python), GATE_TEST_ROOT=str(root), GATE_TEST_BIN=str(GATE),
            PATH=os.pathsep.join((str(GATE), str(fake), str(real), "/usr/bin", "/bin")))
        return environment

    def run_gate(self, environment: dict[str, str], *arguments: str, name: str = "cargo",
                 pass_fds: tuple[int, ...] = (), cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(GATE / name), *arguments], cwd=cwd, env=environment, capture_output=True, text=True,
                              pass_fds=pass_fds, timeout=TEST_HANG_GUARD_SECS)

    def runs(self, root: Path) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in (root / "runs").read_text().splitlines()]

    def legacy_record(self, records: Path, pid: int, label: str) -> Path:
        path = records / str(pid)
        _ = path.write_text(f"{pid}\tMon Nov 9 12:00:00 2026\t{label}\n")
        return path

    def owner_entries(self, root: Path, pid: int, *, has_record: bool = True,
                      started_us: int = FIXTURE_STARTED_US) -> tuple[Path, Path]:
        state = root / ".local/state/mac-gate"
        records = state / "records"
        records.mkdir(parents=True, exist_ok=True)
        owner = MacGateRecord(MacProcessIdentity(pid, started_us), "placement episode/attempt")
        record = records / owner.identity.key
        if has_record:
            _ = record.write_text(owner.line())
        pipe = state / ("jobserver-" + owner.identity.key)
        os.mkfifo(pipe)
        return record, pipe

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
                    self.assertEqual(rows[0]["record"], f"{pid}-{FIXTURE_STARTED_US}")
                    self.assertEqual(stamp, str(FIXTURE_STARTED_US))
                    self.assertEqual(label, "placement episode/attempt" if placement else "user")
                    self.assertEqual((root / "checks").exists(), placement)
                    if not placement:
                        self.assertFalse((root / "ps-calls").exists())
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
            environment.update(CARGO_BUILD_JOBS="12", NEXTEST_TEST_THREADS="13", CARGO_MAKEFLAGS="terminal")
            _ = write_config_fixture(root / "config/mac_test.conf", "max_active_builds=1\n")
            result = self.run_gate(environment, "build", "--jobs=15")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            row, = self.runs(root)
            self.assertEqual(row["argv"], ["build", "--jobs=15"])
            self.assertEqual(row["cargo_jobs"], "12")
            self.assertEqual(row["nextest_threads"], "13")
            self.assertEqual(row["cargo_makeflags"], "terminal")
            self.assertIn("\tuser\n", cast(list[str], row["records"])[0])
            self.assertFalse((root / "ps-calls").exists())
            self.assertEqual((root / "registrations").read_text().splitlines(), ["register"])
            self.assertFalse((root / "checks").exists())
            self.assertFalse((root / "ssh-arguments").exists())

    def test_placed_compile_uses_the_jobserver_without_rewriting_arguments(self) -> None:
        for name, arguments in (
            ("cargo", ("build",)),
            ("cargo", ("build", "--config", "build.jobs=12")),
            ("cargo", ("build", "-vj12")),
            ("cargo", ("build", "-j00000000000000012")),
            ("cargo", ("build", "-j1")),
            ("rustc", ("--version",)),
        ):
            with self.subTest(name=name, arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = self.fixture(root)
                environment.update(CLAUDECODE="1", MAC_PLACEMENT="episode/attempt", CARGO_BUILD_JOBS="2")
                result = self.run_gate(environment, *arguments, name=name)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row, = self.runs(root)
                self.assertEqual(row["jobserver_tokens"], int(FIXTURE_WORKER_BUDGET) - 1)
                self.assertEqual(row["cargo_jobs"], "2")
                self.assertIsNone(row["nextest_threads"])
                self.assertEqual(row["argv"], list(arguments))
                self.assertIsInstance(row["jobserver_inode"], int)
                self.assertEqual(list((root / ".local/state/mac-gate").glob("jobserver-*")), [])

    def test_a_single_jobserver_slot_needs_no_pipe_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            _ = write_config_fixture(root / "config/mac_gate.conf", "worker_budget=1\n")
            environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt")
            result = self.run_gate(environment, "build")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            row, = self.runs(root)
            self.assertIsInstance(row["jobserver_inode"], int)
            self.assertEqual(row["jobserver_tokens"], 0)

    def test_placed_cargo_replaces_an_inherited_jobserver(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            reader, writer = os.pipe()
            try:
                inherited = f"-j --jobserver-auth={reader},{writer}"
                environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt",
                                   CARGO_MAKEFLAGS=inherited, MAKEFLAGS=inherited, MFLAGS=inherited)
                inherited_tokens = b"inherited tokens"
                self.assertEqual(os.write(writer, inherited_tokens), len(inherited_tokens))
                result = self.run_gate(environment, "build", pass_fds=(reader, writer))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row, = self.runs(root)
                self.assertNotEqual(row["jobserver_inode"], os.fstat(reader).st_ino)
                self.assertNotEqual(row["cargo_makeflags"], inherited)
                self.assertIsNone(row["makeflags"])
                self.assertIsNone(row["mflags"])
                self.assertEqual(row["jobserver_tokens"], int(FIXTURE_WORKER_BUDGET) - 1)
                os.set_blocking(reader, False)
                self.assertEqual(os.read(reader, len(inherited_tokens)), inherited_tokens)
            finally:
                os.close(reader)
                os.close(writer)

    def test_placed_nextest_caps_test_width_and_uses_the_compile_jobserver(self) -> None:
        budget = FIXTURE_WORKER_BUDGET
        for name, requested, threads, arguments, expected in (
            ("cargo", "", budget, (), ()),
            ("cargo", "10", budget, ("--test-threads", "12"), ("--test-threads", budget)),
            ("cargo-nextest", "2", "2", ("--build-jobs=8", "--jobs=1"), ("--build-jobs=8", "--jobs=1")),
            ("cargo-nextest", "2", "2", ("-j12",), (f"-j{budget}",)),
            ("cargo-nextest", "", budget, ("-vj12",), (f"-vj{budget}",)),
            ("cargo-nextest", "", budget, ("-vj00000000000000012",), (f"-vj{budget}",)),
            ("cargo", "", budget, ("-Pjobs", "-j12"), ("-Pjobs", f"-j{budget}")),
            ("cargo-nextest", "", budget, ("-R./jobs.zip",), ("-R./jobs.zip",)),
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
                self.assertEqual(row["jobserver_tokens"], int(budget) - 1)
                self.assertEqual(row["nextest_threads"], threads)
                self.assertEqual(row["argv"], ["nextest", "run", *expected, *tail])

    def test_cargo_alias_caps_threads_after_cargo_resolves_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            home_bin = root / "cargo-home/bin"
            home_bin.mkdir(parents=True)
            _ = shutil.copy2(root / "real/cargo-nextest", home_bin / "cargo-nextest")
            (home_bin / "cargo-ntr").symlink_to("cargo-nextest")
            _ = (home_bin.parent / "config.toml").write_text(
                '[alias]\nnx = "nextest run"\nnxr = "nextest run -j12"\n'
            )
            environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt")
            calls = ((str(home_bin.parent), ("nx", "-j12")), ("cargo-home", ("nxr",)),
                     (str(home_bin.parent), ("ntr", "-j12")))
            for home, arguments in calls:
                result = self.run_gate({**environment, "CARGO_HOME": home}, *arguments, cwd=root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for row, (_, arguments) in zip(self.runs(root), calls, strict=True):
                command = ["ntr"] if arguments[0] == "ntr" else ["nextest", "run"]
                self.assertEqual(row["argv"], [*command, f"-j{FIXTURE_WORKER_BUDGET}"])
                self.assertEqual(row["jobserver_tokens"], int(FIXTURE_WORKER_BUDGET) - 1)
                self.assertEqual(len(cast(list[str], row["records"])), 1)

    def test_gate_blocks_current_roots_and_ignores_legacy_roots(self) -> None:
        for case in ("foreground", "background", "unreadable", "different home", "legacy"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = {**self.fixture(root), "SSH_CONNECTION": "fake", "MAC_PLACEMENT": "episode/attempt"}
                event, holder = socket.socketpair()
                background = case == "background"
                command = "leave-child" if background else "hold-root"
                with event, holder, subprocess.Popen(
                    [str(GATE / "cargo"), command], env={**environment, "GATE_TEST_ROOT_EVENT": str(holder.fileno())},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=(holder.fileno(),),
                ) as first:
                    holder.close()
                    event.settimeout(TEST_HANG_GUARD_SECS)
                    try:
                        self.assertEqual(event.recv(1), b"r")
                        if not background:
                            state = root / ".local/state/mac-gate"
                            cache, = (state / "checks").glob("*.live")
                            _ = cache.write_text("0\t" + environment["MAC_PLACEMENT"] + "\n")
                            record, = (state / "records").iterdir()
                            message = "placement already has a live build root"
                            if case == "legacy":
                                record.unlink()
                                record = self.legacy_record(record.parent, first.pid, "placement episode/attempt")
                            elif case == "unreadable":
                                _ = record.write_bytes(b"\xff")
                                message = "cannot verify an existing build root"
                            home = root / "different-home" if case == "different home" else root
                            overlap = self.run_gate({**environment, "HOME": str(home)}, "build")
                            if case == "legacy":
                                self.assertEqual(overlap.returncode, 0, overlap.stdout + overlap.stderr)
                                self.assertTrue(record.exists())
                                self.assertEqual(len(self.runs(root)), 2)
                            else:
                                self.assertEqual(overlap.returncode, 125, overlap.stdout + overlap.stderr)
                                self.assertIn(message, overlap.stderr)
                                self.assertEqual(list((state / "records").iterdir()), [record])
                                if home != root:
                                    self.assertFalse((home / ".local/state/mac-gate").exists())
                                self.assertEqual(len(self.runs(root)), 1)
                                self.assertEqual((root / "checks").read_text().splitlines(), ["check"])
                            event.sendall(b"+")
                        output = first.communicate(timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual(first.returncode, 0, "".join(output))

                        result = self.run_gate(environment, "build")
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        before, *overlaps, after = self.runs(root)
                        self.assertEqual(len(overlaps), int(case == "legacy"))
                        self.assertNotEqual(before["record"], after["record"])
                        self.assertEqual(after["jobserver_tokens"], int(FIXTURE_WORKER_BUDGET) - 1)
                        self.assertEqual(len(cast(list[str], after["records"])), 1)
                        if background:
                            self.assertNotEqual(before["jobserver_inode"], after["jobserver_inode"])
                            event.sendall(b"+")
                            self.assertEqual(event.recv(1), b"")
                    finally:
                        if first.returncode is None:
                            first.kill()

    def test_short_native_read_uses_the_pid_existence_result(self) -> None:
        for evidence in ("live", "denied", "ended"):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = self.fixture(root)
                pid = os.getpid()
                environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt",
                                   GATE_TEST_PROBE_FAILURES=json.dumps({pid: evidence}))
                record, pipe = self.owner_entries(root, pid)
                original = record.read_text()
                result = self.run_gate(environment, "build")
                self.assertEqual((root / "probes").read_text().splitlines().count(str(pid)), 1)
                if evidence == "ended":
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse(record.exists())
                    self.assertFalse(pipe.exists())
                else:
                    self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
                    self.assertIn("cannot verify an existing build root", result.stderr)
                    self.assertTrue(pipe.exists())
                    self.assertEqual(list(record.parent.iterdir()), [record])
                    self.assertEqual(record.read_text(), original)
                    self.assertFalse((root / "runs").exists())

    def test_scan_failure_preserves_ended_records_and_pipe_names(self) -> None:
        for directory in ("records", "."):
            for failure in ("open", "iterate"):
                with self.subTest(directory=directory, failure=failure), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    environment = self.fixture(root)
                    pid = os.getpid()
                    record, pipe = self.owner_entries(root, pid)
                    environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt",
                                       GATE_TEST_PROBE_FAILURES=json.dumps({pid: "ended"}),
                                       GATE_TEST_SCAN_FAILURE=f"{directory}:{failure}",
                                       GATE_TEST_ENDED_ENTRY=(record if directory == "records" else pipe).name)
                    result = self.run_gate(environment, "build")
                    self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
                    self.assertIn("cannot enumerate", result.stderr)
                    self.assertEqual(list(record.parent.iterdir()), [record])
                    self.assertTrue(pipe.exists())
                    self.assertNotIn(str(pid), (root / "probes").read_text().splitlines())

    def test_live_pipe_requires_a_matching_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt")
            record, pipe = self.owner_entries(root, os.getpid(), has_record=False)
            result = self.run_gate(environment, "build")
            self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
            self.assertIn("cannot verify an existing jobserver owner", result.stderr)
            self.assertEqual(list(record.parent.iterdir()), [])
            self.assertTrue(pipe.exists())
            self.assertFalse((root / "runs").exists())

    def test_peer_can_publish_its_record_and_pipe_between_scans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            record, pipe = self.owner_entries(root / "peer", os.getpid())
            environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/other-attempt",
                               GATE_TEST_PEER_ENTRIES=json.dumps([str(record), str(pipe)]))
            result = self.run_gate(environment, "build")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = root / ".local/state/mac-gate"
            self.assertTrue((state / "records" / record.name).exists())
            self.assertTrue((state / pipe.name).exists())
            self.assertEqual(len(self.runs(root)), 1)

    def test_record_races_allow_a_root_in_another_placement(self) -> None:
        for race in ("withdraw", "cleanup", "publish"):
            with self.subTest(race=race), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = self.fixture(root)
                pid = os.getpid()
                record, pipe = self.owner_entries(root, pid)
                environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/other-attempt")
                result = self.run_gate({
                    **environment, "GATE_TEST_RECORD_RACE": race, "GATE_TEST_RACE_PEER_PID": str(pid),
                }, "build")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(record.exists())
                if race == "withdraw":
                    self.assertFalse(pipe.exists())
                else:
                    self.assertEqual((root / "contender").read_text(), "deferred")
                if race == "cleanup":
                    result = self.run_gate({**environment, "GATE_TEST_PROBE_FAILURES": json.dumps({pid: "ended"})},
                                           "build")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse(record.exists())
                    self.assertFalse(pipe.exists())

    def test_shell_refusal_leaves_its_record_for_the_next_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt")
            refusal = self.run_gate({**environment, "CHECK_MODE": "closed"}, "build")
            self.assertEqual(refusal.returncode, 125, refusal.stdout + refusal.stderr)
            self.assertIn("placement episode/attempt closed", refusal.stderr)
            records = root / ".local/state/mac-gate/records"
            refused, = records.iterdir()
            result = self.run_gate(environment, "build")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(refused.exists())
            self.assertEqual(len(self.runs(root)), 1)

    def test_legacy_cleanup_preserves_live_or_unknown_pids_and_stamped_replacements(self) -> None:
        for evidence in ("live", "denied", "reused"):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = self.fixture(root)
                pid = os.getpid()
                records = root / ".local/state/mac-gate/records"
                records.mkdir(parents=True)
                legacy = self.legacy_record(records, pid, "user")
                environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt",
                                   GATE_TEST_PROBE_FAILURES=json.dumps({pid: evidence}))
                if evidence == "reused":
                    environment["GATE_TEST_LEGACY_REUSE"] = str(pid)
                result = self.run_gate(environment, "build")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn(str(pid), (root / "probes").read_text().splitlines())
                if evidence == "reused":
                    self.assertFalse(legacy.exists())
                    replacement = records / MacProcessIdentity(pid, FIXTURE_STARTED_US).key
                    self.assertTrue(replacement.exists())
                else:
                    self.assertTrue(legacy.exists())

    def test_pid_reuse_within_one_second_releases_the_old_record_and_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt")
            record, pool = self.owner_entries(root, os.getpid(), started_us=FIXTURE_STARTED_US - 1)
            result = self.run_gate(environment, "build")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(record.exists())
            self.assertFalse(pool.exists())
            self.assertEqual(len(self.runs(root)), 1)

    def test_cargo_refuses_relative_home_with_a_directory_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt", CARGO_HOME="cargo-home")
            for arguments in (("-C", "project"), ("-Cproject",), ("-vCproject",), ("-vvC=project",)):
                with self.subTest(arguments=arguments):
                    result = self.run_gate(environment, "+nightly", "-Z", "unstable-options", *arguments, "build",
                                           cwd=root)
                    self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
                    self.assertIn("Cargo -C requires an absolute CARGO_HOME", result.stderr)
            self.assertFalse((root / "checks").exists())
            self.assertFalse((root / "registrations").exists())
            self.assertFalse((root / "runs").exists())

    def test_placed_nextest_refuses_unreadable_test_thread_options(self) -> None:
        for arguments in (("-vj",), ("-vjbad",), ("--test-threads=",), ("-xj12",)):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = self.fixture(root)
                environment.update(SSH_CONNECTION="fake", MAC_PLACEMENT="episode/attempt")
                result = self.run_gate(environment, "nextest", "run", *arguments)
                self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
                self.assertIn("nextest test thread", result.stderr)
                self.assertFalse((root / "runs").exists())

    def test_placed_nextest_resolves_cpu_relative_widths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.fixture(root)
            getconf = root / "fake/getconf"
            getconf.unlink()
            _ = getconf.write_text('#!/bin/sh\ncase "$1" in _NPROCESSORS_ONLN) echo 12 ;; PIPE_BUF) echo 512 ;; esac\n')
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
            for row in (mend, clippy):
                self.assertEqual(row["jobserver_tokens"], int(FIXTURE_WORKER_BUDGET) - 1)

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

    def test_cached_calls_start_only_registration_and_expired_calls_check_again(self) -> None:
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
                self.assertEqual((root / "registrations").read_text().splitlines(), ["register"] * (index + 1))
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
            self.assertEqual(rows[0]["jobserver_inode"], rows[1]["jobserver_inode"])
            self.assertEqual(rows[1]["jobserver_tokens"], int(FIXTURE_WORKER_BUDGET) - 1)
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
