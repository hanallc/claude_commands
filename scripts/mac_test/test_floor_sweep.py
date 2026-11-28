"""Check shared Mac floor sweeps and disk admission."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import fcntl
import io
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import TextIO, cast, override
from unittest import mock

from scripts.mac_test import eligibility, offload
from scripts.lib import conf
from scripts.lint import sweep
from scripts.mac_test.offload_test_commands import COMMAND_STUB, write_shell
from scripts.mac_test.offload_test_support import OffloadCommandFixture
from scripts.lint.memory_test_support import config_environment, write_config_fixture


class MacDiskFloorTests(unittest.TestCase):
    def test_free_space_above_the_floor(self) -> None:
        result = offload.check_mac_disk_floor(offload.ProbeSnapshot(120, "fake-mac"), 100)
        self.assertEqual(result, offload.MacDiskReady(120))

    def test_free_space_equal_to_the_floor(self) -> None:
        result = offload.check_mac_disk_floor(offload.ProbeSnapshot(100, "fake-mac"), 100)
        self.assertEqual(result, offload.MacDiskReady(100))

    def test_free_space_below_the_floor_starts_no_sweep_or_command(self) -> None:
        with (mock.patch.object(offload, "ensure_mac_disk_floor") as sweep_floor,
              mock.patch.object(subprocess, "Popen") as command):
            result = offload.check_mac_disk_floor(offload.ProbeSnapshot(99, "fake-mac"), 100)
        self.assertEqual(result, offload.MacDiskShort(99, 100))
        sweep_floor.assert_not_called()
        command.assert_not_called()


class FloorSweepTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.settings: Path = Path()
        self.controller: offload.ProcessController = offload.ProcessController()
        self.repository: eligibility.KnownRepository = eligibility.KnownRepository("hana")
        self.before: offload.ProbeSnapshot = offload.ProbeSnapshot(50, "fake-mac")

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **config_environment(self.root), "MAC_TEST_STATE_DIR": str(self.root / "state"),
        })))
        self.settings = self.root / "mac_test.conf"
        _ = write_config_fixture(self.settings, "host=mac\nfree_floor_gib=100\nfloor_sweep_timeout_s=9\n")
        _ = write_config_fixture(self.root / "build_targets.conf", "platform.mac=macos\n")
        self.controller = offload.ProcessController()
        self.repository = eligibility.KnownRepository("hana")
        self.before = offload.ProbeSnapshot(50, "fake-mac")

    def sweep(self) -> offload.MacDiskReady | offload.SweepShort | offload.SweepFailed:
        return offload.ensure_mac_disk_floor(self.controller, eligibility.read_config(self.settings), self.repository, self.before)

    def test_host_probe_reads_disk_and_saved_mirror_processes_in_one_command(self) -> None:
        _ = write_config_fixture(self.settings, "host=fixture-mac\nmirror_root=~/fixture-mirrors\n")
        scans = self.root / "scans"
        commands = (
            "df() { printf 'header\\nfs a b 164\\n'; }\n"
            + "hostname() { printf 'fixture-mac\\n'; }\n"
            + "pgrep() { return 1; }\n"
            + f"lsof() {{ printf 'scan\\n' >> {shlex.quote(str(scans))}; "
            + "printf 'p41\\nn/home/fixture/fixture-mirrors/old\\np42\\nn/home/fixture/fixture-mirrors/new\\n'; }\n"
            + """ps() { printf 'fixture-start-%s\\n' "$2"; }""" + "\n"
        )

        def capture(arguments: list[str], _deadline: offload.CommandDeadline) -> offload.CommandOutput:
            self.assertEqual((arguments[0], arguments[-2]), ("ssh", "fixture-mac"))
            result = subprocess.run(["/bin/sh", "-c", commands + arguments[-1]],
                                    capture_output=True, text=True, timeout=5, check=False)
            return offload.CommandOutput(result.returncode, result.stdout, result.stderr)

        settings = eligibility.probe_settings(conf.read(self.settings))
        with mock.patch.object(self.controller, "capture", side_effect=capture) as command:
            snapshot = offload.probe_mac_host(self.controller, settings, "mac", ("old", "new", "absent"))
        self.assertEqual(snapshot, offload.MacHostProbeSnapshot(offload.ProbeSnapshot(164, "fixture-mac"), {
            "old": offload.MacMirrorProcess(41, "fixture-start-41"),
            "new": offload.MacMirrorProcess(42, "fixture-start-42"),
        }, {}))
        self.assertEqual(scans.read_text().splitlines(), ["scan"])
        command.assert_called_once()

    def test_queued_probe_waits_for_the_floor_before_preparing_a_mirror(self) -> None:
        home = self.root / "home"
        shim = home / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        write_shell(shim, "exec " + shlex.quote(sys.executable) + ' -B "$@"\n')
        mirror = home / "mirrors/hana"
        _ = write_config_fixture(self.settings, "mirror_root=~/mirrors\n")
        _ = write_config_fixture(conf.path("build_admission"), "waiting_pass_seconds=12\n")
        commands = ("df() { printf 'header\\nfs a b 164\\n'; }\n"
                    + "hostname() { printf 'fixture-mac\\n'; }\n")

        def capture(arguments: list[str], _deadline: offload.CommandDeadline) -> offload.CommandOutput:
            with subprocess.Popen(["/bin/sh", "-c", "read start\n" + commands + arguments[-1]],
                                  env={**os.environ, "HOME": str(home)}, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                assert process.stdin is not None and process.stderr is not None
                stderr = cast(TextIO, process.stderr)
                try:
                    with sweep.mac_floor_mirror_leases({mirror}) as acquired:
                        self.assertTrue(acquired)
                        _ = process.stdin.write("\n")
                        process.stdin.flush()
                        self.assertTrue(select.select([stderr], [], [], 5)[0])
                        waiting = stderr.readline()
                        self.assertIn("waiting for the Mac floor sweep", waiting)
                        self.assertFalse(mirror.exists())
                    output, errors = process.communicate(timeout=10)
                    return offload.CommandOutput(process.returncode, output, waiting + errors)
                finally:
                    if process.poll() is None:
                        process.kill()

        settings = eligibility.probe_settings(conf.read(self.settings))
        with mock.patch.object(self.controller, "capture", side_effect=capture), redirect_stderr(io.StringIO()):
            snapshot = offload.probe_mac_host(self.controller, settings, "mac", (),
                                              setup_mirrors=(self.repository.legacy_mirror_name,))
        self.assertEqual(snapshot, offload.MacHostProbeSnapshot(offload.ProbeSnapshot(164, "fixture-mac"), {},
                             {self.repository.legacy_mirror_name: offload.MacMirrorSetup.READY}))
        self.assertTrue(mirror.is_dir())

    def test_queued_probe_uses_one_deadline_for_all_mirrors_and_reads_disk(self) -> None:
        home = self.root / "home"
        shim = home / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        write_shell(shim, "exec " + shlex.quote(sys.executable) + ' -B "$@"\n')
        _ = write_config_fixture(self.settings, "mirror_root=~/mirrors\nfloor_sweep_timeout_s=2\nprobe_timeout_s=20\n")
        mirrors = (eligibility.LegacyMirrorName("first"), eligibility.LegacyMirrorName("second"))
        ready = eligibility.LegacyMirrorName("ready")
        commands = ("df() { printf 'header\\nfs a b 164\\n'; }\n"
                    + "hostname() { printf 'fixture-mac\\n'; }\n")

        def capture(arguments: list[str], deadline: offload.CommandDeadline) -> offload.CommandOutput:
            assert isinstance(deadline, offload.CommandTimeLimit)
            self.assertEqual(deadline.seconds, 20.0)
            result = subprocess.run(["/bin/sh", "-c", commands + arguments[-1]],
                                    env={**os.environ, "HOME": str(home)}, capture_output=True,
                                    text=True, timeout=deadline.seconds, check=False)
            return offload.CommandOutput(result.returncode, result.stdout, result.stderr)

        output = io.StringIO()
        settings = eligibility.probe_settings(conf.read(self.settings))
        with (sweep.mac_floor_mirror_leases({home / "mirrors" / name for name in mirrors}) as acquired,
              mock.patch.object(self.controller, "capture", side_effect=capture), redirect_stderr(output)):
            self.assertTrue(acquired)
            snapshot = offload.probe_mac_host(self.controller, settings, "mac", (), setup_mirrors=(*mirrors, ready))
        setups = dict.fromkeys(mirrors, offload.MacMirrorSetup.FAILED)
        setups[ready] = offload.MacMirrorSetup.READY
        self.assertEqual(snapshot, offload.MacHostProbeSnapshot(offload.ProbeSnapshot(164, "fixture-mac"), {},
                         setups))
        diagnostics = output.getvalue()
        self.assertEqual(diagnostics.count("waiting for the Mac floor sweep to finish one removal group"), 1, diagnostics)
        self.assertIn("timed out waiting for the Mac floor sweep", diagnostics)

    def test_queued_probe_caps_a_long_floor_wait_within_the_evidence_lifetime(self) -> None:
        home = self.root / "home"
        shim = home / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        write_shell(shim, "exec " + shlex.quote(sys.executable) + ' -B "$@"\n')
        _ = write_config_fixture(self.settings, "mirror_root=~/mirrors\nfloor_sweep_timeout_s=180\nprobe_timeout_s=12\n")
        mirror = self.repository.legacy_mirror_name
        commands = "df() { printf 'header\\nfs a b 164\\n'; }\n" + "hostname() { printf 'fixture-mac\\n'; }\n"

        def capture(arguments: list[str], deadline: offload.CommandDeadline) -> offload.CommandOutput:
            # The floor wait stays inside the probe time limit that admission keeps a reading for.
            self.assertEqual(deadline, offload.CommandTimeLimit(12))
            result = subprocess.run(["/bin/sh", "-c", commands + arguments[-1]],
                                    env={**os.environ, "HOME": str(home)}, capture_output=True,
                                    text=True, timeout=14, check=False)
            return offload.CommandOutput(result.returncode, result.stdout, result.stderr)

        settings = eligibility.probe_settings(conf.read(self.settings))
        diagnostics = io.StringIO()
        with (sweep.mac_floor_mirror_leases({home / "mirrors" / mirror}) as acquired,
              mock.patch.object(self.controller, "capture", side_effect=capture), redirect_stderr(diagnostics)):
            self.assertTrue(acquired)
            snapshot = offload.probe_mac_host(self.controller, settings, "mac", (), setup_mirrors=(mirror,))
        self.assertEqual(snapshot, offload.MacHostProbeSnapshot(
            offload.ProbeSnapshot(164, "fixture-mac"), {}, {mirror: offload.MacMirrorSetup.FAILED}))
        self.assertIn("timed out waiting for the Mac floor sweep", diagnostics.getvalue())

    def test_two_callers_share_one_sweep_and_both_read_after_it(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        real_flock = fcntl.flock

        def observe_flock(file: TextIO, operation: int) -> None:
            try:
                real_flock(file, operation)
            except BlockingIOError:
                waiting.set()
                raise

        def capture(arguments: list[str], deadline: offload.CommandDeadline) -> offload.CommandOutput:
            _ = arguments, deadline
            entered.set()
            if not release.wait(10):
                raise AssertionError("The sweep did not receive its release.")
            return offload.CommandOutput(0, "", "")

        with (mock.patch.object(self.controller, "capture", side_effect=capture) as command,
              mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(100, "fake-mac")) as probe,
              mock.patch.object(fcntl, "flock", side_effect=observe_flock),
              ThreadPoolExecutor(max_workers=2) as workers):
            first = workers.submit(self.sweep)
            try:
                self.assertTrue(entered.wait(10))
                second = workers.submit(self.sweep)
                self.assertTrue(waiting.wait(10))
                self.assertEqual(probe.call_count, 0)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10), offload.MacDiskReady(100))
            self.assertEqual(second.result(timeout=10), offload.MacDiskReady(100))
        self.assertEqual(command.call_count, 1)
        self.assertEqual(probe.call_count, 2)

    def test_sweep_uses_live_timeout_and_holds_no_claim_lock(self) -> None:
        config = eligibility.read_config(self.settings)
        _ = write_config_fixture(self.settings, "floor_sweep_timeout_s=3\n")
        state = self.root / "state"
        state.mkdir()

        def capture(arguments: list[str], deadline: offload.CommandDeadline) -> offload.CommandOutput:
            self.assertEqual(deadline, offload.CommandTimeLimit(3))
            self.assertEqual(arguments[-2], "mac")
            self.assertEqual(shlex.split(arguments[-1]), [
                "~/.claude/scripts/lib/py", "~/.claude/scripts/lint/sweep.py",
                "--mac-floor-deadline", str(sent_at + 1.5), "--floor-only", "--host", "mac",
            ])
            with (state / "state.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return offload.CommandOutput(0, "", "")

        sent_at = time.time()
        with (mock.patch.object(time, "time", return_value=sent_at),
              mock.patch.object(self.controller, "capture", side_effect=capture),
              mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(100, "fake-mac"))):
            self.assertEqual(offload.ensure_mac_disk_floor(
                self.controller, config, self.repository, self.before), offload.MacDiskReady(100))

    def test_failed_sweep_result_keeps_its_cause(self) -> None:
        for result, cause in (
            (offload.CommandOutput(7, "mac_floor_cleanup=complete\n", "cannot sweep"),
             "floor sweep exited with status 7: cannot sweep"),
            (offload.CommandOutput(137, "discovery blocked\n", ""),
             "floor sweep exited with status 137: discovery blocked. Mac floor cleanup is unconfirmed"),
            (offload.CommandOutput(143, "mac_floor_cleanup=complete\n", "floor sweep stopped by SIGTERM"),
             "floor sweep exited with status 143: floor sweep stopped by SIGTERM"),
            (offload.CommandOutput(124, "mac_floor_cleanup=complete\n", "floor sweep timed out"),
             "floor sweep timed out on the Mac"),
            (offload.CommandOutput(124, "", "floor sweep timed out"),
             "floor sweep timed out on the Mac. Mac floor cleanup is unconfirmed"),
            (offload.COMMAND_TIMED_OUT, "floor sweep timed out after 9 s. Mac floor cleanup is unconfirmed"),
            (offload.COMMAND_COULD_NOT_START, "floor sweep could not start"),
        ):
            errors = io.StringIO()
            with (self.subTest(cause=cause),
                  mock.patch.object(self.controller, "capture", return_value=result),
                  mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(99, "fake-mac")) as probe,
                  redirect_stderr(errors)):
                self.assertEqual(self.sweep(), offload.SweepShort(99))
                self.assertEqual(errors.getvalue(), f"mac_test: {cause}\n")
                probe.assert_called_once()

    def test_recheck_failure_keeps_the_disk_exclusion(self) -> None:
        with (mock.patch.object(self.controller, "capture", return_value=offload.CommandOutput(0, "", "")),
              mock.patch.object(offload, "probe_mac", return_value=offload.UNAVAILABLE_PROBE)):
            self.assertEqual(self.sweep(), offload.SweepFailed("Mac disk reading failed after the floor sweep"))

    def test_a_sweep_error_allows_the_step_when_the_fresh_read_meets_the_minimum(self) -> None:
        errors = io.StringIO()
        with (mock.patch.object(self.controller, "capture",
                  return_value=offload.CommandOutput(7, "mac_floor_cleanup=complete\n", "cannot remove old output")),
              mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(120, "fake-mac")) as probe,
              redirect_stderr(errors)):
            self.assertEqual(self.sweep(), offload.MacDiskReady(120))
        self.assertIn("floor sweep exited with status 7: cannot remove old output", errors.getvalue())
        probe.assert_called_once()

    def test_the_disk_probe_never_creates_a_mirror(self) -> None:
        home = self.root / "home"
        home.mkdir()
        shim = home / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        write_shell(shim, "exec " + shlex.quote(sys.executable) + ' -B "$@"\n')
        script = "df() { printf 'Available\\n120\\n' | awk '{print \"0 0 0 \" $1}'; }\n"
        script += "hostname() { printf 'fake-mac\\n'; }\n"
        script += offload.probe_script(self.repository)
        mirror = home / offload.remote_mirror(self.repository)
        answer = subprocess.run(["sh", "-c", script], env={**os.environ, "HOME": str(home)},
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        self.assertEqual(offload.parse_probe(answer.stdout), offload.ProbeSnapshot(120, "fake-mac"))
        self.assertFalse(mirror.exists())

    def test_mirror_launch_waits_for_the_current_floor_group(self) -> None:
        mirror = self.root / "mirror" / "hana fixture"
        program = """import sys
from pathlib import Path
from scripts.copies import copies
with copies.local_mirror_lease(Path(sys.argv[1]), "fixture"):
    print("run started", flush=True)
"""
        launch = shlex.join([sys.executable, "-B", "-c", program, str(mirror)])
        remote = offload.after_mac_floor_group(str(mirror), launch)
        command = shlex.quote(sys.executable) + remote.removeprefix("~/.claude/scripts/lib/py")
        with subprocess.Popen(["sh", "-c", "read start; exec " + command],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) as process:
            assert process.stdin is not None and process.stdout is not None
            stdout = cast(TextIO, process.stdout)
            try:
                with sweep.mac_floor_mirror_leases({mirror}) as acquired:
                    self.assertTrue(acquired)
                    _ = process.stdin.write("\n")
                    process.stdin.flush()
                    ready, _, _ = select.select([stdout], [], [], 10)
                    self.assertTrue(ready)
                    self.assertIn("waiting for the Mac floor sweep", stdout.readline())
                output, _ = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, output)
                self.assertEqual(output, "run started\n")
            finally:
                if process.poll() is None:
                    process.kill()

    def test_offload_rejects_invalid_mirror_roots(self) -> None:
        for root in ("~//tmp", "~/", "/abs", "~/a/../b", "~/a b", "~/a/./b", "~/a/", "~/a//b", "~/a$b"):
            with self.subTest(root=root):
                _ = write_config_fixture(self.settings, f"mirror_root={root}\n")
                with self.assertRaisesRegex(ValueError, "mirror_root"):
                    _ = offload.remote_mirror(self.repository)

    def test_sweep_rejects_invalid_mirror_roots(self) -> None:
        for root in ("~//tmp", "~/", "/abs", "~/a/../b", "~/a b", "~/a/./b", "~/a/", "~/a//b", "~/a$b"):
            with self.subTest(root=root):
                _ = write_config_fixture(self.settings,
                    f"mirror_root={root}\nsweep_floor_roots=/tmp ~/rust {root}\n")
                with self.assertRaisesRegex(ValueError, "mirror_root"):
                    _ = sweep.mac_floor_settings(conf.read(self.settings))

    def test_selector_rejects_its_assignment_text_and_near_miss_paths(self) -> None:
        configured = "~/.fixture-mirrors"
        for root in (configured, "~/fixture-mirrors"):
            _ = write_config_fixture(self.settings, f"mirror_root={root}\n")
            relative = root.removeprefix("~/")
            near = relative.replace(".", "X", 1) if "." in relative else relative.replace("-", "X", 1)
            for scope in (offload.ALL_MIRRORS, self.repository):
                with self.subTest(root=root, scope=type(scope).__name__):
                    script = offload.mirror_selector_script(scope)
                    suffix = "/hana" if isinstance(scope, eligibility.KnownRepository) else ""
                    candidates = (script, f"cargo /Users/test/{near}{suffix}/Cargo.toml")
                    # Use the generated selector with the same regular expression engine as pgrep.
                    commands = r'''
pgrep() {
    printf '%s\n' "$candidates" | awk -v pattern="$4" '$0 ~ pattern { print 811 }'
}
lsof() { printf 'p812\nn/Users/test/%s/\n' "$cwd_candidate"; }
select_leftovers
'''
                    for candidate in candidates:
                        answer = subprocess.run(["/bin/sh", "-c",
                            script + "candidates=" + shlex.quote(candidate) + "\n"
                            + "cwd_candidate=" + shlex.quote(near + suffix) + "\n" + commands],
                            capture_output=True, text=True, timeout=10, check=False)
                        self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "", ""))
                    positive = subprocess.run(["/bin/sh", "-c",
                        script + "candidates=" + shlex.quote(f"cargo /Users/test/{relative}{suffix}/Cargo.toml")
                        + "\ncwd_candidate=" + shlex.quote(relative + suffix) + "\n" + commands],
                        capture_output=True, text=True, timeout=10, check=False)
                    self.assertEqual((positive.returncode, positive.stdout, positive.stderr), (0, "811\n812\n", ""))
                    if root == configured:
                        pattern = subprocess.run(["/bin/sh", "-c", script + 'printf "%s" "$mirror_command_pattern"'],
                            capture_output=True, text=True, timeout=10, check=False)
                        expected = "[.]" + relative[1:] + ("/hana([/'\"[:space:]]|$)" if suffix else "/")
                        self.assertEqual((pattern.returncode, pattern.stdout, pattern.stderr), (0, expected, ""))

    def test_waiter_logs_an_incomplete_result_and_reads_free_space_after_owner_exit(self) -> None:
        directory = self.root / "state"
        directory.mkdir()
        path = directory / "floor-sweep-mac.lock"
        waiting = threading.Event()
        real_flock = fcntl.flock

        def observe_flock(file: TextIO, operation: int) -> None:
            try:
                real_flock(file, operation)
            except BlockingIOError:
                waiting.set()
                raise

        for saved, cause in (
            ("", "floor sweep result missing. Mac floor cleanup is unconfirmed"),
            ("floor sweep was interrupted. Mac floor cleanup is unconfirmed",
             "floor sweep was interrupted. Mac floor cleanup is unconfirmed"),
        ):
            errors = io.StringIO()
            with (self.subTest(saved=saved), path.open("a+") as owner,
                  mock.patch.object(self.controller, "capture") as command,
                  mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(99, "fake-mac")) as probe,
                  mock.patch.object(fcntl, "flock", side_effect=observe_flock),
                  redirect_stderr(errors),
                  ThreadPoolExecutor(max_workers=1) as workers):
                waiting.clear()
                real_flock(owner, fcntl.LOCK_EX)
                _ = owner.truncate(0)
                _ = owner.write(saved)
                owner.flush()
                inode = path.stat().st_ino
                waiter = workers.submit(self.sweep)
                try:
                    self.assertTrue(waiting.wait(10))
                finally:
                    owner.close()
                self.assertEqual(waiter.result(timeout=10), offload.SweepShort(99))
                self.assertEqual(errors.getvalue(), f"mac_test: {cause}\n")
                self.assertEqual(path.stat().st_ino, inode)
                self.assertEqual(command.call_count, 0)
                probe.assert_called_once()

    def test_mirror_root_has_one_configuration_source(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        needle = "/".join((".local", "state", "mac-test", "mirror"))
        found: list[str] = []
        for path in (repository / "scripts").rglob("*"):
            if not path.is_file() or path.suffix not in (".py", ".sh") or path.name.startswith("test"):
                continue
            if needle in path.read_text():
                found.append(str(path.relative_to(repository)))
        self.assertEqual(found, [])
        _ = write_config_fixture(self.settings, f"mirror_root=~/{needle}\n")
        configured = conf.read(self.settings).text("mirror_root")
        self.assertEqual(configured, "~/" + needle)
        root = "~/fixture-mirrors"
        _ = write_config_fixture(self.settings,
            f"mirror_root={root}\nsweep_floor_roots=/tmp ~/rust {root}\n")
        self.assertEqual(offload.remote_mirror(self.repository), "fixture-mirrors/hana")
        self.assertIn("[f]ixture-mirrors/", offload.mirror_selector_script(offload.ALL_MIRRORS))
        _, roots = sweep.mac_floor_settings(conf.read(self.settings))
        self.assertIn(root, roots)


def install_floor_sweep_stub(fixture: OffloadCommandFixture) -> None:
    """Match the deadline argument in the fixture's SSH command."""
    write_shell(fixture.bin_directory / "command-stub", COMMAND_STUB.replace(
        "'sweep.py --floor-only --host mac'",
        "'sweep.py --mac-floor-deadline '*' --floor-only --host mac'",
    ))


class FloorSweepCommandTests(OffloadCommandFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        install_floor_sweep_stub(self)
        self.fixture["probe_lines"] = ["free_gib=59", "host=Mac", "rc=0"]
        self.sync_fixture()

    def test_sweep_admits_the_step_at_the_disk_minimum(self) -> None:
        _ = (self.stub_directory / "free-after-sweep").write_text("60\n")
        answer = self.command()
        self.assertEqual(answer.returncode, 0, answer.stdout)
        self.assert_wire("passed", "")
        self.assertEqual((self.stub_directory / "sweeps").read_text().splitlines(), ["called"])
        self.assertEqual(len(self.events_for("rsync")), 1)

    def test_short_sweep_keeps_the_step_on_natedev(self) -> None:
        _ = (self.stub_directory / "free-after-sweep").write_text("59\n")
        self.assert_declined(self.command(), "disk")
        self.assertEqual((self.stub_directory / "sweeps").read_text().splitlines(), ["called"])
        self.assertEqual(self.events_for("rsync"), [])

    def test_failed_sweep_names_the_cause_and_keeps_the_step_on_natedev(self) -> None:
        _ = (self.stub_directory / "sweep-failure").write_text("cannot remove old mirrors\n")
        answer = self.command()
        self.assertEqual(answer.returncode, 75, answer.stdout)
        self.assert_wire("declined", "disk")
        self.assertIn("floor sweep exited with status 7: cannot remove old mirrors", answer.stdout)
        self.assertEqual(sum('df -g "$HOME"' in event["args"][-1] for event in self.events_for("ssh")), 2)
        self.assertEqual(self.events_for("rsync"), [])
        self.assertFalse(self.claim_file.exists())

    def test_timed_out_sweep_names_the_cause_and_keeps_the_step_on_natedev(self) -> None:
        self.config["floor_sweep_timeout_s"] = "0.2"
        self.sync_config()
        os.mkfifo(self.stub_directory / "continue-sweep")
        answer = self.command()
        self.assertEqual(answer.returncode, 75, answer.stdout)
        self.assert_wire("declined", "disk")
        self.assertIn("mac_test: floor sweep timed out after 0.2 s. Mac floor cleanup is unconfirmed", answer.stdout)
        self.assertEqual(answer.stdout.splitlines()[-1], "mac_test: not running on the Mac (disk)")
        self.assertEqual(self.events_for("rsync"), [])
        self.assertFalse(self.claim_file.exists())

    def test_cancel_during_sweep_releases_the_sweep_and_claim_locks(self) -> None:
        os.mkfifo(self.stub_directory / "continue-sweep")
        process = self.start_command()
        try:
            self.wait_for_path(self.stub_directory / "sweeps")
            with (self.state_directory / "state.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
        self.assertFalse(self.claim_file.exists())
        locks = list(self.state_directory.glob("floor-sweep-*.lock"))
        self.assertEqual(len(locks), 1)
        for path in [*locks, self.state_directory / "state.lock"]:
            with path.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertFalse((self.stub_directory / "sweep-finished").exists())


if __name__ == "__main__":
    _ = unittest.main()
