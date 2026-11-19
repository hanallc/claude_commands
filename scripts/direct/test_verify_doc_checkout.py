#!/usr/bin/env python3
"""Exercise doc verification against real Git trees and fake Cargo."""
from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import json
import os
import select
import signal
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast
import unittest
from unittest import mock

from scripts.agents.remote import local_machine
from scripts.build_queue import admission_protocol as wire, admission_records, admission_service as service, execution_identity, memory_feed, placement
from scripts.buildlog import treekey
from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.direct.verify_doc_test_support import CARGO_STUB, VERIFY, VerifyDocFixture
from scripts.direct.verify_mac_offload_test_support import processes_carrying, reap, wait_for_marker_exit
from scripts.direct.verify_scope_test_support import use_fixture_scope_readings, write_real_scope_launcher
from scripts.direct.wait_for_test_state import wait_for_pid, wait_until
from scripts.lib import build_folders, build_slots
from scripts.lib.build_steps import RECORDED_STEP_NAMES
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_admit import scope_members
from scripts.lint.memory_test_support import write_config_fixture, write_pressure_reading
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, isolated_staff_file


DOC_COMMAND = ("bash", str(VERIFY), "doc")


class VerifyDocPipeTests(unittest.TestCase):
    def run_checkout_pipe_probe(self, limit: int, probe: str) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            owner = root / "copy-owner.sh"
            _ = owner.write_text(r'''
if [[ "$TEST_COPY_LIMIT" == 0 ]]; then
    printf '%s\n' "$PWD"
fi
if IFS= read -r request; then
    exit 99
fi
: > owner-received-eof
''')
            script = shell_functions(VERIFY, "step_checkout", "step_close_checkout") + r'''
export TEST_COPY_LIMIT=$1
checkout() { step_checkout "$BASH" ./copy-owner.sh; }
''' + probe
            try:
                result = subprocess.run(["bash", "-euc", script, "doc-pipes", str(limit)], cwd=root,
                                        capture_output=True, text=True, timeout=5)
            except subprocess.TimeoutExpired as error:
                self.fail(f"doc checkout cleanup timed out: {error.stderr!r}")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((root / "owner-received-eof").is_file())

    def test_doc_closes_owned_pipes_when_a_move_fails(self) -> None:
        for limit in (10, 11):
            with self.subTest(limit=limit):
                self.run_checkout_pipe_probe(limit, r'''
ulimit -Sn "$TEST_COPY_LIMIT"
status=0
checkout || status=$?
[[ "$status" == 2 ]]
''')

    def test_doc_preserves_files_that_reuse_the_original_pipe_numbers(self) -> None:
        self.run_checkout_pipe_probe(0, r'''
set -T
trap '
    if [[ "$BASH_COMMAND" == DOC_COPY_OWNER_PID=* && -n "${DOC_COPY[1]:-}" ]]; then
        original_request=${DOC_COPY[1]}
        original_reply=${DOC_COPY[0]}
    fi
' DEBUG
checkout
trap - DEBUG
[[ ! -e "/dev/fd/$original_request" && ! -e "/dev/fd/$original_reply" ]]
file=$PWD/reused
eval 'exec '"$original_request"'>"$file" '"$original_reply"'<"$file"'
step_close_checkout
[[ "/dev/fd/$original_request" -ef "$file" && "/dev/fd/$original_reply" -ef "$file" ]]
''')


class VerifyDocCheckoutTests(VerifyDocFixture):
    def start_doc(self, *, command: tuple[str, ...] = DOC_COMMAND,
                  pass_fds: tuple[int, ...] = ()) -> tuple[subprocess.Popen[bytes], Path, str]:
        log = self.root / "verify.log"
        marker = f"TEST_DOC_CHECKOUT={self.root}"
        with log.open("wb") as output:
            process = subprocess.Popen(
                command, cwd=self.repo, stdout=output, stderr=output,
                env={**self.environment, "TEST_DOC_CHECKOUT": str(self.root)}, start_new_session=True, pass_fds=pass_fds,
            )
        self.addCleanup(process.wait, timeout=TEST_HANG_GUARD_SECS)
        self.addCleanup(reap, marker)
        return process, log, marker

    @contextmanager
    def hold_cargo_token(self) -> Generator[None]:
        board = self.root / "board"
        self.environment.update({"UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "doc"})
        helper = VERIFY.with_name("board.sh")
        token = [str(board), "other", "cargo", "--pid", str(os.getpid())]
        _ = subprocess.run(["bash", str(helper), "acquire", *token], env=self.environment,
                           check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        try:
            yield
        finally:
            _ = subprocess.run(["bash", str(helper), "release", *token], env=self.environment,
                               check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)

    @contextmanager
    def occupy_doc_slot(self, wait_policy: build_slots.SlotWaitPolicy = build_slots.SlotWaitPolicy.QUEUE) -> Generator[Path]:
        admission = caller_admission(self, self.root, self.environment)
        settings = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        _ = write_config_fixture(settings, f"# whole: 1..\nslots.{admission.server.host}.workspace.max=1\n")
        with mock.patch.dict(os.environ, self.environment), build_slots.take_slot(
                self.repo, "HEAD", showrunner="builds", unit="doc-test", work="competing copy", purpose="doc",
                wait_policy=wait_policy) as slot:
            yield slot.path

    def use_real_scope(self) -> AdmissionFixture:
        write_real_scope_launcher(self.root / "bin")
        admission = caller_admission(self, self.root, self.environment)
        use_fixture_scope_readings(self, self.root, admission)
        return admission

    def install_scope_interpreter(self, supervisor_code: str, *,
                                  scripts: tuple[str, ...] = ("/verify_scoped_step.py",)) -> None:
        wrapper = self.root / "scope-interpreter"
        self.executable(wrapper, f'''import os
import sys
arguments = sys.argv[1:]
script_arguments = arguments[1:] if arguments[0] == "-P" else arguments
script = script_arguments[0]
if not script.endswith({scripts!r}):
    os.execv(sys.executable, [sys.executable, *arguments])
''' + supervisor_code)
        self.environment["CLAUDE_PYTHON"] = str(wrapper)

    def instrument_scope_supervisor(self, setup: str, patches: str) -> None:
        self.install_scope_interpreter(f'''
from unittest.mock import patch

sys.path.insert(0, {str(VERIFY.parents[2])!r})
from scripts.direct import verify_scoped_step
{setup}
with {patches}:
    raise SystemExit(verify_scoped_step.main(script_arguments[1:]))
''')

    def test_doc_finishes_while_another_caller_holds_the_cargo_token(self) -> None:
        with self.hold_cargo_token():
            result = self.verify()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(self.cargo_calls()), 1)
            self.assertEqual(len(self.pass_records()), 1)

    def assert_doc_retains_checkout_until_scope_empties(self, *, interrupt: bool,
                                                       sweep_scope_sidecar: bool = False,
                                                       command: tuple[str, ...] = DOC_COMMAND) -> None:
        admission = self.use_real_scope()
        scope_file = self.root / "doc-scope"
        child_pid = self.root / "doc-descendant"
        pending_pid = child_pid.with_suffix(".new")
        lease_at_launch = self.root / "lease-at-launch.json"
        release = self.root / "end-cargo"
        helper = VERIFY.with_name("wait_for_test_state.py")
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            '    raise SystemExit(int(os.environ.get("TEST_DOC_STATUS", "0")))', f'''
    sys.path.insert(0, {str(VERIFY.parents[2])!r})
    from scripts.direct.wait_for_test_state import wait_for_file

    checkout = Path.cwd()
    lease = checkout.with_name(checkout.name + ".lease.json")
    Path({str(lease_at_launch)!r}).write_text(lease.read_text())
    child = subprocess.Popen([sys.executable, {str(helper)!r}, {str(self.root / "end-descendant")!r}],
                             close_fds=True)
    Path({str(scope_file)!r}).write_text(os.environ["BUILDLOG_STEP_SCOPE"])
    Path({str(pending_pid)!r}).write_text(str(child.pid))
    Path({str(pending_pid)!r}).replace({str(child_pid)!r})
    wait_for_file(Path({str(release)!r}))
    raise SystemExit(0)
'''))
        process, log, marker = self.start_doc(command=command)
        wait_until(lambda: child_pid.is_file() or process.poll() is not None, self.root, processes=(process,))
        self.assertTrue(child_pid.is_file(), log.read_text())
        descendant = int(child_pid.read_text())
        scope = Path(scope_file.read_text())
        call, = self.cargo_calls()
        checkout = Path(call["cwd"])
        lease = cast(dict[str, object], json.loads(lease_at_launch.read_text()))
        copy_owner = os.pidfd_open(cast(int, lease["pid"]))
        self.addCleanup(os.close, copy_owner)
        kill_file = scope / "cgroup.kill"
        self.assertIn(descendant, scope_members(scope))
        kill_file.chmod(0)
        try:
            if sweep_scope_sidecar:
                reservation, = admission.caller_reservations().values()
                Path(reservation.sidecar).unlink()
            if interrupt:
                process.terminate()
            else:
                release.touch()
            status = process.wait(TEST_HANG_GUARD_SECS)
            self.assertIn(descendant, scope_members(scope))
            ended, _, _ = select.select([copy_owner], [], [], TEST_HANG_GUARD_SECS)
            self.assertTrue(ended, "the checkout copy owner remained after Verify exited")
            with build_folders.collector_lease(checkout) as free:
                self.assertFalse(free, "the collector accepted a checkout with a populated scope")
            self.assertEqual(lease["scope"], str(scope))
            self.assertEqual(status, 125, log.read_text())
            self.assertIn("keeping the reservation", log.read_text())
            _ = admission.run_pass()
            self.assertTrue(admission.caller_reservations())
            self.assertFalse(self.pass_records())
            with self.assertRaises(build_slots.SlotsFull), self.occupy_doc_slot(build_slots.SlotWaitPolicy.REFUSE):
                self.fail("another caller took the checkout while the scope was populated")
        finally:
            kill_file.chmod(0o200)
            _ = kill_file.write_text("1\n")
            self.assertEqual(wait_for_marker_exit(marker, TEST_HANG_GUARD_SECS), set())
            _ = admission.run_pass()
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        with self.occupy_doc_slot(build_slots.SlotWaitPolicy.REFUSE) as next_checkout:
            self.assertEqual(next_checkout, checkout)

    def test_doc_scope_stop_failure_retains_checkout_after_cargo_exits(self) -> None:
        self.assert_doc_retains_checkout_until_scope_empties(interrupt=False)

    def test_doc_scope_stop_failure_retains_checkout_after_interrupt(self) -> None:
        self.assert_doc_retains_checkout_until_scope_empties(interrupt=True)

    def test_doc_scope_stop_failure_retains_checkout_and_reservation_after_sidecar_sweep(self) -> None:
        self.assert_doc_retains_checkout_until_scope_empties(interrupt=False, sweep_scope_sidecar=True)

    def copies_verify_command(self, *arguments: str) -> tuple[str, ...]:
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.environment))
        caller = fixture_member("showrunner", name="verify-copy")
        self.environment["CLAUDE_CODE_SESSION_ID"] = caller.current_session
        copies = VERIFY.parents[1] / "copies/copies.py"
        return (sys.executable, str(copies), "run", "--repo", str(self.repo), "--commit", "HEAD", "--",
                "bash", str(VERIFY), *arguments)

    def test_copies_run_binds_the_doc_checkout_before_cargo(self) -> None:
        self.assert_doc_retains_checkout_until_scope_empties(
            interrupt=False, command=self.copies_verify_command("doc"))

    def test_in_place_lint_keeps_the_source_copy_after_the_owner_dies(self) -> None:
        _ = write_config_fixture(self.config, "mend=on\nfmt=off\nclippy=off\nsweep=off\n")
        self.assert_copies_run_keeps_folder_after_owner_exit("lint")

    def test_doc_keeps_the_outer_target_after_the_owner_dies(self) -> None:
        _ = self.environment.pop("CARGO_TARGET_DIR")
        self.assert_copies_run_keeps_folder_after_owner_exit("doc")

    def assert_copies_run_keeps_folder_after_owner_exit(self, command: str) -> None:
        admission = self.use_real_scope()
        started = self.root / "cargo-started.json"
        release = self.root / "end-cargo"
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            '    print(os.environ["TEST_METADATA"])', '''
    metadata = json.loads(os.environ["TEST_METADATA"])
    metadata["target_directory"] = os.environ["CARGO_TARGET_DIR"]
    print(json.dumps(metadata))
''').replace('elif arguments[:1] == ["doc"]:', f'''
elif arguments[:1] == ["doc"] or arguments[:1] == ["mend"] and "--fix" in arguments:
    sys.path.insert(0, {str(VERIFY.parents[2])!r})
    from scripts.direct.wait_for_test_state import wait_for_file

    ready = Path({str(started)!r})
    pending = ready.with_suffix(".new")
    pending.write_text(json.dumps({{"pid": os.getpid(), "source": os.environ["COPIES_STEP_SOURCE"],
                                  "target": os.environ["CARGO_TARGET_DIR"], "cwd": str(Path.cwd())}}))
    pending.replace(ready)
    wait_for_file(Path({str(release)!r}))
    raise SystemExit(0)
elif arguments[:1] == ["doc"]:
'''))
        process, log, marker = self.start_doc(command=self.copies_verify_command(command))
        wait_until(lambda: started.is_file() or process.poll() is not None, self.root, processes=(process,))
        self.assertTrue(started.is_file(), log.read_text())
        cargo = cast(dict[str, int | str], json.loads(started.read_text()))
        cargo_pid = int(cargo["pid"])
        source = Path(str(cargo["source"]))
        source_folder = source.parent
        if command == "doc":
            self.assertTrue(Path(str(cargo["target"])).is_relative_to(source_folder))
            self.assertNotEqual(Path(str(cargo["cwd"])), source)
        else:
            self.assertEqual(Path(str(cargo["cwd"])), source)
        sidecar, = (self.root / "tmp").glob("buildlog.*.peak.cgroup")
        scope = Path(sidecar.read_text().strip())
        kill_file = scope / "cgroup.kill"
        self.assertIn(cargo_pid, scope_members(scope))
        _ = admission.run_pass()
        kill_file.chmod(0)
        try:
            process.kill()
            self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), -signal.SIGKILL)
            self.assertIn(cargo_pid, scope_members(scope))
            self.assertEqual(build_folders.build_folder_state(source_folder, wait=False), "held")
            record = source_folder.with_name(source_folder.name + ".lease.json")
            lease = cast(dict[str, object], json.loads(record.read_text()))
            self.assertEqual(lease.get("scope"), str(scope))
        finally:
            release.touch()
            if kill_file.exists():
                kill_file.chmod(0o200)
            self.assertEqual(wait_for_marker_exit(marker, TEST_HANG_GUARD_SECS), set())
            _ = admission.run_pass()
        self.assertEqual(build_folders.build_folder_state(source_folder, wait=False), "free")

    def test_fmt_does_not_inherit_the_source_copy_lock(self) -> None:
        _ = self.use_real_scope()
        _ = write_config_fixture(self.config, "mend=off\nfmt=on\nclippy=off\nsweep=off\n")
        observed = self.root / "fmt-locks.json"
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            'elif arguments[:1] == ["doc"]:', f'''
elif arguments[:2] == ["+nightly", "fmt"]:
    if "--version" in arguments:
        print("rustfmt fixture")
        raise SystemExit(0)
    source = Path(os.environ["COPIES_STEP_SOURCE"]).parent
    lock = source.with_name(source.name + ".lock").stat()
    inherited = []
    for entry in Path("/proc/self/fd").iterdir():
        try:
            descriptor = entry.stat()
        except FileNotFoundError:
            continue
        if (descriptor.st_dev, descriptor.st_ino) == (lock.st_dev, lock.st_ino):
            inherited.append(entry.name)
    lease = json.loads(source.with_name(source.name + ".lease.json").read_text())
    Path({str(observed)!r}).write_text(json.dumps({{"inherited": inherited,
        "scope": os.environ.get("BUILDLOG_STEP_SCOPE"), "lease_scope": lease.get("scope")}}))
elif arguments[:1] == ["doc"]:
'''))
        self.executable(self.root / "bin/taplo", "#!/bin/sh\nexit 0\n")
        process, log, _ = self.start_doc(command=self.copies_verify_command("lint"))
        self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 0, log.read_text())
        fmt = cast(dict[str, object], json.loads(observed.read_text()))
        self.assertEqual(fmt["inherited"], [])
        self.assertIsInstance(fmt["scope"], str)
        self.assertEqual(fmt["scope"], fmt["lease_scope"])

    def test_slot_doc_leaves_the_folder_claim_to_the_slot(self) -> None:
        self.assert_doc_folder_claim(use_slot_handoff=True)

    def test_fixed_folder_doc_sends_its_folder_to_admission(self) -> None:
        self.assert_doc_folder_claim(use_slot_handoff=False)

    def assert_doc_folder_claim(self, *, use_slot_handoff: bool) -> None:
        admission = caller_admission(self, self.root, self.environment)
        directory = self.root / "configured-build"
        self.environment["CARGO_BUILD_BUILD_DIR"] = str(directory)
        metadata = cast(dict[str, object], json.loads(self.environment["TEST_METADATA"]))
        metadata["build_directory"] = str(directory)
        self.environment["TEST_METADATA"] = json.dumps(metadata)
        started = self.root / "cargo-build-directory"
        release = self.root / "end-doc"
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            'elif arguments[:1] == ["doc"]:', f'''
elif arguments[:1] == ["doc"]:
    sys.path.insert(0, {str(VERIFY.parents[2])!r})
    from scripts.direct.wait_for_test_state import wait_for_file

    ready = Path({str(started)!r})
    pending = ready.with_suffix(".new")
    pending.write_text(json.dumps({{"build": os.environ["CARGO_BUILD_BUILD_DIR"], "checkout": str(Path.cwd())}}))
    pending.replace(ready)
    wait_for_file(Path({str(release)!r}))
'''))
        with nullcontext() if use_slot_handoff else self.prepared_doc_slot():
            pass_fds = () if use_slot_handoff else (build_folders.build_folder_lock_fd(self.repo),)
            process, log, _ = self.start_doc(pass_fds=pass_fds)
            try:
                wait_until(lambda: started.is_file() or process.poll() is not None, self.root, processes=(process,))
                self.assertTrue(started.is_file(), log.read_text())
                with admission.loop_turn():
                    reservation, = admission.caller_reservations().values()
                launch = cast(dict[str, str], json.loads(started.read_text()))
                launched_build_folder = Path(launch["build"])
                if use_slot_handoff:
                    self.assertEqual(reservation.cargo_folder, placement.NoCargoFolder())
                    self.assertEqual(launched_build_folder, self.root / "cargo" / Path(launch["checkout"]).name)
                else:
                    self.assertEqual(reservation.cargo_folder, placement.CargoFolder(launched_build_folder))
                    self.assertEqual(launched_build_folder, directory)
            finally:
                release.touch()
            self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 0, log.read_text())

    def test_doc_uses_the_build_directory_of_the_slot_it_holds(self) -> None:
        build_directory = self.root / "cargo-build-directory"
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            'elif arguments[:1] == ["doc"]:', f'''
elif arguments[:1] == ["doc"]:
    Path({str(build_directory)!r}).write_text(os.environ["CARGO_BUILD_BUILD_DIR"])
'''))
        with mock.patch.dict(os.environ, self.environment), build_slots.take_slot(
                self.repo, "HEAD", showrunner="builds", unit="doc-test", work="another build", purpose="doc",
                wait_policy=build_slots.SlotWaitPolicy.REFUSE) as first:
            self.environment.update(first.env)
            metadata = cast(dict[str, object], json.loads(self.environment["TEST_METADATA"]))
            metadata["build_directory"] = str(first.cargo_dir)
            self.environment["TEST_METADATA"] = json.dumps(metadata)
            result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        checkout = Path(self.cargo_calls()[0]["cwd"])
        self.assertNotEqual(checkout, first.path)
        self.assertEqual(Path(build_directory.read_text()), first.cargo_dir.with_name(checkout.name))

    def test_doc_refuses_launch_when_the_scope_publication_is_lost(self) -> None:
        config = Path(self.environment["CONF_DIR"]) / "direct.conf"
        _ = write_config_fixture(config, "# range: 5e-324..\nVERIFY_SCOPE_PUBLICATION_SECONDS=1\n")
        waiting = self.root / "scope-awaiting-permission"
        self.environment["TEST_SCOPE_WAITING"] = str(waiting)
        launcher = self.root / "bin/systemd-run"
        self.executable(launcher, launcher.read_text().replace('exec "$@" 2>&1', '''
rm -- "$peak.cgroup"
: > "$TEST_SCOPE_WAITING"
IFS= read -r permission || exit 125
exit 99
'''))
        self.install_scope_interpreter(f'''
from pathlib import Path
import runpy
sys.path.insert(0, {str(VERIFY.parents[2])!r})
from scripts.direct.wait_for_test_state import wait_for_file
from unittest.mock import patch
from scripts.direct import verify_scoped_step

start = verify_scoped_step.subprocess.Popen

def start_after_publication_is_lost(*args, **kwargs):
    process = start(*args, **kwargs)
    wait_for_file(Path({str(waiting)!r}))
    return process

with patch.object(verify_scoped_step.subprocess, "Popen", start_after_publication_is_lost):
    sys.argv = script_arguments
    runpy.run_path(script, run_name="__main__")
''')
        process, log, _ = self.start_doc()
        wait_until(lambda: waiting.is_file() or process.poll() is not None, self.root, processes=(process,))
        self.assertTrue(waiting.is_file(), log.read_text())
        self.assertEqual(process.wait(timeout=8), 125, log.read_text())
        output = log.read_text()
        self.assertIn("the checkout scope publication timed out", output)
        self.assertIn("keeping the reservation", output)
        self.assertFalse(self.events.exists())
        self.assertFalse(self.pass_records())

    def test_doc_refuses_checkout_confirmation_after_the_deadline(self) -> None:
        self.instrument_scope_supervisor('''
import time
from types import SimpleNamespace

read = os.read
elapsed = 0
reply_fd = int(os.environ["DOC_COPY_REPLY_FD"])

def receive_reply(descriptor, size):
    global elapsed
    reply = read(descriptor, size)
    if descriptor == reply_fd and reply == b"bound\\n":
        elapsed = 60
    return reply

clock = SimpleNamespace(monotonic=lambda: time.monotonic() + elapsed)
''', 'patch.object(os, "read", receive_reply), patch.object(verify_scoped_step, "time", clock)')
        self.assert_doc_publication_timeout()

    def test_doc_refuses_launch_when_the_final_owner_check_passes_the_deadline(self) -> None:
        self.instrument_scope_supervisor('''
import select
import time
from types import SimpleNamespace

real_poll = select.poll
elapsed = 0
reply_fd = int(os.environ["DOC_COPY_REPLY_FD"])

class OwnerPoll:
    def __init__(self):
        self.poller = real_poll()
        self.confirmed = False

    def __getattr__(self, name):
        return getattr(self.poller, name)

    def unregister(self, descriptor):
        self.poller.unregister(descriptor)
        if descriptor == reply_fd:
            self.confirmed = True

    def poll(self, timeout=-1):
        global elapsed
        ready = self.poller.poll(timeout)
        if self.confirmed and timeout == 0:
            elapsed = 60
        return ready

clock = SimpleNamespace(monotonic=lambda: time.monotonic() + elapsed)
''', 'patch.object(select, "poll", OwnerPoll), patch.object(verify_scoped_step, "time", clock)')
        self.assert_doc_publication_timeout()

    def assert_doc_publication_timeout(self) -> None:
        result = self.verify()
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 125, output)
        self.assertIn("the checkout scope publication timed out", output)
        self.assertIn("keeping the reservation", output)
        self.assertFalse(self.events.exists())
        self.assertFalse(self.pass_records())

    def test_doc_accepts_a_checkout_confirmation_split_across_reads(self) -> None:
        self.instrument_scope_supervisor('''
read = os.read
reply_fd = int(os.environ["DOC_COPY_REPLY_FD"])

def split_reply(descriptor, size):
    return read(descriptor, min(size, 5) if descriptor == reply_fd else size)
''', 'patch.object(os, "read", split_reply)')
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.cargo_calls()), 1)
        self.assertEqual(len(self.pass_records()), 1)

    def test_doc_accepts_a_publication_timeout_above_the_poll_limit(self) -> None:
        config = Path(self.environment["CONF_DIR"]) / "direct.conf"
        _ = write_config_fixture(config, "# range: 5e-324..\nVERIFY_SCOPE_PUBLICATION_SECONDS=1e308\n")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.cargo_calls()), 1)
        self.assertEqual(len(self.pass_records()), 1)

    def test_doc_scope_start_error_writes_release_state_before_status_125(self) -> None:
        recorded_failure = self.root / "recorded-scope-failure"
        self.install_scope_interpreter(f'''
import runpy
from pathlib import Path
from unittest.mock import patch

with patch.object(os, "setpgid", side_effect=OSError("cannot set the supervisor group")):
    sys.argv = script_arguments
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit:
        Path({str(recorded_failure)!r}).write_text(Path(sys.argv[3]).read_text())
        raise
''')
        result = self.verify()
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 125, output)
        self.assertIn("cannot set the supervisor group", recorded_failure.read_text())
        self.assertIn("keeping the reservation", output)
        self.assertFalse(self.events.exists())
        self.assertFalse(self.pass_records())
        with self.occupy_doc_slot(build_slots.SlotWaitPolicy.REFUSE):
            pass

    def test_copy_owner_exit_during_publication_cannot_change_the_lease(self) -> None:
        lease_path = self.root / "interrupted-lease"
        self.install_scope_interpreter(f'''
import json
from pathlib import Path
import runpy
import select
import signal
from unittest.mock import patch

replace = os.replace

def owner_exits_before_replace(source, target, *, src_dir_fd=None, dst_dir_fd=None):
    if str(target).endswith(".lease.json"):
        with os.fdopen(os.open(source, os.O_RDONLY, dir_fd=src_dir_fd)) as pending:
            record = json.load(pending)
        if "scope" in record:
            directory = Path(os.readlink(f"/proc/self/fd/{{dst_dir_fd}}"))
            Path({str(lease_path)!r}).write_text(str(directory / target))
            owner = record["pid"]
            owner_fd = os.pidfd_open(owner)
            try:
                os.kill(owner, signal.SIGKILL)
                ready, _, _ = select.select([owner_fd], [], [], {TEST_HANG_GUARD_SECS!r})
                assert ready, "the copy owner did not exit"
            finally:
                os.close(owner_fd)
    replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

with patch.object(os, "replace", owner_exits_before_replace):
    sys.argv = script_arguments
    runpy.run_path(script, run_name="__main__")
''', scripts=("/verify_scoped_step.py", "/copies.py"))
        result = self.verify()
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 125, output)
        self.assertIn("keeping the reservation", output)
        record = cast(dict[str, object], json.loads(Path(lease_path.read_text()).read_text()))
        self.assertNotIn("scope", record)
        self.assertFalse(self.events.exists())

    def test_doc_scope_command_does_not_inherit_checkout_pipe(self) -> None:
        _ = self.use_real_scope()
        lease_pipes = self.root / "lease-pipes"
        cargo_pipes = self.root / "cargo-pipes"
        self.install_scope_interpreter(f'''
from pathlib import Path
descriptors = (os.environ["DOC_COPY_LEASE_FD"], os.environ["DOC_COPY_REPLY_FD"])
Path({str(lease_pipes)!r}).write_text("\\n".join(os.readlink(f"/proc/self/fd/{{fd}}") for fd in descriptors))
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''')
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            '    raise SystemExit(int(os.environ.get("TEST_DOC_STATUS", "0")))', f'''
    targets = []
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            targets.append(os.readlink(descriptor))
        except FileNotFoundError:
            pass
    Path({str(cargo_pipes)!r}).write_text("\\n".join(targets))
    raise SystemExit(0)
'''))
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        pipes = lease_pipes.read_text().splitlines()
        self.assertEqual(len(pipes), 2)
        for pipe in pipes:
            self.assertTrue(pipe.startswith("pipe:["))
            self.assertNotIn(pipe, cargo_pipes.read_text().splitlines())

    def test_doc_slot_wait_names_the_unit_without_a_session(self) -> None:
        _ = self.enterContext(isolated_staff_file(self.root / "doc-staff.json", self.environment))
        showrunner = fixture_member("showrunner", name="builds")
        unit = fixture_member("unit", name="doc-proof", parent=showrunner.id,
                              worktree=staff.PresentWorktree(local_machine(), self.repo))
        _ = self.environment.pop("CLAUDE_CODE_SESSION_ID", None)
        _ = self.environment.pop("CODEX_THREAD_ID", None)
        self.assert_doc_slot_wait_identity(showrunner, unit, session=str(unit.id), folder=str(self.repo))

    def test_doc_slot_wait_names_the_unit_from_an_unregistered_clone(self) -> None:
        registered = self.root / "registered" / self.repo.name
        registered.parent.mkdir()
        _ = self.repo.rename(registered)
        _ = subprocess.run(["git", "clone", "-q", "--", str(registered), str(self.repo)],
                           env=self.environment, check=True, timeout=TEST_HANG_GUARD_SECS)
        _ = self.enterContext(isolated_staff_file(self.root / "doc-staff.json", self.environment))
        showrunner = fixture_member("showrunner", name="builds")
        unit = fixture_member("unit", name="doc-proof", parent=showrunner.id,
                              worktree=staff.PresentWorktree(local_machine(), registered))
        self.environment["CLAUDE_CODE_SESSION_ID"] = unit.current_session
        _ = self.environment.pop("CODEX_THREAD_ID", None)
        self.assert_doc_slot_wait_identity(showrunner, unit, session=unit.current_session, folder="")

    def assert_doc_slot_wait_identity(self, showrunner: staff.StaffRow, unit: staff.StaffRow, *,
                                      session: str, folder: str) -> None:
        admission = caller_admission(self, self.root, self.environment)
        waiting = Event()

        def observe_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.RepositorySlotWaiting):
                waiting.set()
            return reply

        with (self.occupy_doc_slot(), mock.patch.object(admission.server, "reply_filter", side_effect=observe_wait)):
            process, log, _ = self.start_doc()
            self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS), log.read_text())
            with admission.loop_turn():
                waiter, = admission.server.repository_slot_waiters.values()
                request, reply = waiter.request, waiter.reply
                assert isinstance(reply, wire.RepositorySlotWaiting)
            labels = memory_feed.FeedStaff(staff.StaffSnapshot((showrunner, unit)))
            slot = wire.RepositorySlotRow(request.owner, request.identity, request.since, request.machine, reply)
            row = memory_feed.repository_slot_row(slot, RECORDED_STEP_NAMES, labels)
            assert isinstance(row, dict)
            self.assertEqual(row["unit"], unit.name)
            self.assertEqual(request.identity.session_id, session)
            self.assertEqual(request.identity.folder, folder)
        self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 0, log.read_text())

    def test_remote_doc_refuses_a_tree_staged_during_checkout_preparation(self) -> None:
        self.enable_held_mac_doc()
        wrapper = self.root / "checkout-interpreter"
        staged = self.repo / "alpha/src/lib.rs"
        original_tree = self.git("write-tree")
        self.executable(wrapper, f'''import os
from pathlib import Path
import subprocess
import sys

command = [sys.executable, *sys.argv[1:]]
arguments = sys.argv[1:]
if not (len(arguments) >= 2 and arguments[0].endswith("/copies.py")
        and arguments[1] == "hold" and "--scratch" in arguments and "--" not in arguments):
    os.execv(sys.executable, command)
with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as copy:
    checkout = copy.stdout.readline()
    if not checkout:
        raise SystemExit(copy.wait() or 1)
    Path({str(staged)!r}).write_text("pub fn staged_during_checkout() {{}}\\n")
    subprocess.run(["git", "-C", {str(self.repo)!r}, "add", "--", {str(staged)!r}], check=True)
    print(checkout, end="", flush=True)
    raise SystemExit(copy.wait())
''')
        self.environment["CLAUDE_PYTHON"] = str(wrapper)
        result = self.verify()
        self.assertNotEqual(self.git("write-tree"), original_tree, result.stdout + result.stderr)
        self.assertFalse(self.events.exists(), "the remote runner dispatched the old tree")
        self.assertEqual(result.returncode, 77, result.stdout + result.stderr)
        self.assertIn("the staged tree changed", result.stderr)
        self.assertEqual(self.pass_records(), [])

    def test_doc_refuses_a_tree_staged_during_the_copy_slot_wait(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        waiting = Event()

        def observe_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.RepositorySlotWaiting):
                waiting.set()
            return reply

        with (self.occupy_doc_slot(),
              mock.patch.object(admission.server, "reply_filter", side_effect=observe_wait)):
            process, log, _ = self.start_doc()
            self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS), log.read_text())
            with admission.loop_turn():
                waiter, = admission.server.repository_slot_waiters.values()
                self.assertEqual(waiter.request.owner.pid, process.pid)
                self.assertNotIn(waiter.request.episode, admission.caller_reservations())
            self.assertFalse(self.events.exists())
            _ = (self.repo / "alpha/src/lib.rs").write_text("pub fn staged_while_waiting() {}\n")
            _ = self.git("add", "alpha/src/lib.rs")
        self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 77, log.read_text())
        self.assertIn("the staged tree changed", log.read_text())
        self.assertFalse(self.events.exists())
        self.assertEqual(self.pass_records(), [])
        self.assertFalse(admission.caller_reservations())

    def test_verify_exit_releases_the_slot_wait(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        waiting = Event()

        def observe_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.RepositorySlotWaiting):
                waiting.set()
            return reply

        for stop in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(stop=stop):
                waiting.clear()
                with (self.occupy_doc_slot(),
                      mock.patch.object(admission.server, "reply_filter", side_effect=observe_wait)):
                    if stop == signal.SIGKILL:
                        config = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
                        _ = write_config_fixture(config, "# range: 5e-324..\nslots.wait_seconds=300\n")
                    process, log, marker = self.start_doc()
                    self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS), log.read_text())
                    with admission.loop_turn():
                        waiter, = admission.server.repository_slot_waiters.values()
                        owner = waiter.request.owner.pid
                        children = processes_carrying(marker)
                        self.assertIn(owner, children)
                        exited = -1
                        if stop == signal.SIGKILL:
                            if owner != process.pid:
                                helper = owner
                            else:
                                helper, = (child for child in children
                                    if b"wait-slot" in (Path("/proc") / str(child) / "cmdline").read_bytes().split(b"\0"))
                            exited = os.pidfd_open(helper)
                            self.addCleanup(os.close, exited)
                    process.send_signal(stop)
                    expected = 143 if stop == signal.SIGTERM else -signal.SIGKILL
                    self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), expected, log.read_text())
                    if stop == signal.SIGTERM:
                        self.assertEqual(processes_carrying(marker), set())
                    else:
                        self.assertTrue(select.select([exited], [], [], 30)[0],
                                        "the folder waiter survived verify")
                    with admission.loop_turn():
                        _ = admission.server.run_pass()
                        self.assertFalse(admission.server.repository_slot_waiters)
                    self.assertFalse(admission.caller_reservations())
                self.assertFalse(self.events.exists())
                self.assertEqual(self.pass_records(), [])
                self.assertTrue(all(build_folders.build_folder_state(path) == "free"
                                    for path in Path(waiter.request.root).glob("*-slot*") if path.is_dir()))

    @contextmanager
    def hold_background_sweep(self, probe: str = "") -> Generator[Path]:
        release = self.root / "sweep-release"
        started = self.root / "sweep-pid"
        os.mkfifo(release)
        self.install_scope_interpreter(f'''
from pathlib import Path
{probe}
with open({str(release)!r}) as gate:
    Path({str(started)!r}).write_text(str(os.getpid()))
    gate.read()
''', scripts=("/sweep.py",))
        self.executable(self.root / "bin/systemd-cat", "#!/bin/sh\nexec cat\n")
        self.environment["LINT_SKIP_SWEEP"] = "0"
        with os.fdopen(os.open(release, os.O_RDWR), "wb"):
            yield started

    def test_doc_records_a_pass_while_the_background_sweep_waits(self) -> None:
        sweep_events = self.root / "sweep-events"
        with self.hold_background_sweep('''
for descriptor in (8, 74):
    try:
        os.write(descriptor, f"sweep started on {descriptor}\\n".encode())
    except OSError:
        pass
''') as started:
            process, log, marker = self.start_doc(command=(
                "bash", "-c", 'exec 8>"$1" 74>&8; shift; exec bash "$@"',
                "doc-sweep", str(sweep_events), str(VERIFY), "doc",
            ))
            sweep_pid = wait_for_pid(started, processes=(process,))
            self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 0, log.read_text())
            self.assertIn(sweep_pid, processes_carrying(marker))
            self.assertEqual(len(self.pass_records()), 1)
        self.assertEqual(sweep_events.read_text().splitlines(), ["sweep started on 8", "sweep started on 74"])

    def test_doc_waits_for_admission_without_a_slot_or_lease(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        waiting = Event()
        grants: list[wire.LocalPlacementGranted] = []
        reservation = self.root / "doc-reservation"
        self.environment.update({"BUILDLOG_OFF": "0", "BUILDLOG_SYNC": "1"})
        member = self.repo / "alpha/src/lib.rs"
        staged = "pub fn staged() {}\n"
        _ = member.write_text(staged)
        _ = self.git("add", "alpha/src/lib.rs")
        _ = member.write_text("pub fn unstaged() {}\n")
        _ = (self.repo / "notes.md").write_text("untracked")
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            'elif arguments[:1] == ["doc"]:', 'elif arguments[:1] == ["doc"]:\n' +
            f'    Path({str(reservation)!r}).write_text(os.environ["BUILDLOG_RESERVED_STEP"].split("\\t")[0])'))

        def observe_reply(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait):
                waiting.set()
            if isinstance(reply, wire.LocalPlacementGranted):
                grants.append(reply)
            return reply

        admission.hold_starts()
        with (mock.patch.object(admission.server, "reply_filter", side_effect=observe_reply),
              ThreadPoolExecutor(max_workers=1) as executor):
            result = executor.submit(self.verify)
            try:
                self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS), "doc did not wait for admission")
                self.assertFalse(admission.caller_reservations())
                self.assertEqual(list((self.root / "slots").glob("*")), [])
                self.assertFalse(self.events.exists())
            finally:
                write_pressure_reading(self.root / "pressure")
            finished = result.result(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        grant, = grants
        self.assertEqual(reservation.read_text(), grant.grant.reservation)
        self.assertFalse(admission.caller_reservations())
        call, = self.cargo_calls()
        self.assertEqual(call["sources"]["alpha/src/lib.rs"], staged)
        self.assertFalse(call["notes_present"])
        checkout = Path(call["cwd"])
        self.assertTrue(checkout.is_relative_to(self.root / "slots"))
        key = treekey.tree_key(str(checkout))
        assert key is not None
        self.assertEqual(grant.assignment.source_key, execution_identity.SourceKeyKnown(key))
        rows = (cast(dict[str, object], json.loads(line))
                for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines())
        row, = (row for row in rows if row.get("kind") == "step" and row.get("step") == "doc")
        self.assertEqual(row["tree_key"], key)
        self.assertIs(row["tree_changed"], False)
        self.assertEqual(len(self.pass_records()), 1)

    def test_hold_after_grant_refuses_doc_without_taking_a_checkout(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        holder = holders / "hana"
        grants: list[wire.LocalPlacementGranted] = []
        later_requests: list[wire.PlacementRequest] = []
        handle_request = admission.server.handle_request

        def observe_request(request: wire.Request, connection: service.Connection | service.NoConnection
                            = service.NO_CONNECTION) -> list[wire.Reply]:
            if grants and isinstance(request, wire.PlacementRequest):
                later_requests.append(request)
            return handle_request(request, connection)

        def start_hold(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.LocalPlacementGranted):
                grants.append(reply)
                _ = holder.write_text(json.dumps({
                    "holder": "hana", "since": "2026-11-17T20:00:00+00:00", "for": "review", "release_eta": "unknown",
                }))
            return reply

        with (mock.patch.object(admission.server, "reply_filter", side_effect=start_hold),
              mock.patch.object(admission.server, "handle_request", side_effect=observe_request)):
            result = self.verify()
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertIn("natedev is held by hana for review", result.stdout + result.stderr)
        self.assertEqual(len(grants), 1)
        self.assertEqual(later_requests, [])
        self.assertTrue(holder.exists())
        self.assertFalse(self.events.exists())
        self.assertEqual(self.pass_records(), [])
        self.assertFalse(admission.caller_reservations())
        self.assertEqual(list((self.root / "slots").glob("workspace-slot*/")), [])

    def test_doc_refuses_a_tree_staged_during_admission(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        waiting = Event()

        def observe_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait):
                waiting.set()
            return reply

        admission.hold_starts()
        with (mock.patch.object(admission.server, "reply_filter", side_effect=observe_wait),
              ThreadPoolExecutor(max_workers=1) as executor):
            result = executor.submit(self.verify)
            try:
                self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS), "doc did not wait for admission")
                _ = (self.repo / "alpha/src/lib.rs").write_text("pub fn staged_while_waiting() {}\n")
                _ = self.git("add", "alpha/src/lib.rs")
            finally:
                write_pressure_reading(self.root / "pressure")
            finished = result.result(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(finished.returncode, 77, finished.stdout + finished.stderr)
        self.assertIn("the staged tree changed", finished.stderr)
        self.assertFalse(self.events.exists())
        self.assertEqual(self.pass_records(), [])
        self.assertFalse(admission.caller_reservations())

    @contextmanager
    def prepared_doc_slot(self) -> Generator[Path]:
        original = self.repo
        with mock.patch.dict(os.environ, self.environment), build_slots.take_slot(
                original, "HEAD", showrunner="builds", unit="doc-test", work="prepared copy", purpose="doc") as slot:
            self.repo: Path = slot.path
            self.environment["BUILDLOG_PREPARED_COPY"] = str(slot.path)
            descriptor = build_folders.build_folder_lock_fd(slot.path)
            self.environment["BUILDLOG_PREPARED_COPY_LOCK_FD"] = str(descriptor)
            self.environment["BUILDLOG_RUN"] = f"{os.getpid()}:{admission_records.process_start_time(os.getpid())}"
            self.environment["TEST_METADATA"] = self.environment["TEST_METADATA"].replace(str(original), str(slot.path))
            yield slot.path

    def assert_doc_uses_the_prepared_copy(self, command: tuple[str, ...]) -> None:
        with self.prepared_doc_slot() as checkout:
            descriptor = build_folders.build_folder_lock_fd(checkout)
            process, log, _ = self.start_doc(command=command, pass_fds=(descriptor,))
            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0, log.read_text())
            self.assertEqual(self.cargo_calls()[0]["cwd"], str(checkout))
            self.assertEqual(build_folders.build_folder_state(checkout), "held")
            self.assertEqual(list((self.root / "slots").glob("*-slot*.lease.json")),
                             [build_slots.lease_path(checkout)])

    def test_doc_uses_the_prepared_copy_without_taking_a_second_slot(self) -> None:
        self.assert_doc_uses_the_prepared_copy(DOC_COMMAND)

    def assert_prepared_lock_on_descriptor(self, descriptor: int) -> None:
        self.install_scope_interpreter('''
from pathlib import Path

tree = Path.cwd()
lock = tree.with_name(tree.name + ".lock")
inherited = int(os.environ["BUILDLOG_PREPARED_COPY_LOCK_FD"])
if not os.path.samestat(os.fstat(inherited), lock.stat()):
    raise SystemExit("Verify replaced the handed-off checkout lock")
os.execv(sys.executable, [sys.executable, *arguments])
''')
        handoff = '''target=$1
shift
if [[ "$BUILDLOG_PREPARED_COPY_LOCK_FD" != "$target" ]]; then
    eval "exec $target>&$BUILDLOG_PREPARED_COPY_LOCK_FD-"
fi
export BUILDLOG_PREPARED_COPY_LOCK_FD=$target
exec "$@"'''
        self.assert_doc_uses_the_prepared_copy((
            "bash", "-c", handoff, "prepared-doc", str(descriptor), *DOC_COMMAND,
        ))

    def test_prepared_doc_keeps_the_slot_lock_inherited_on_descriptor_nine(self) -> None:
        self.assert_prepared_lock_on_descriptor(9)

    def test_prepared_doc_keeps_the_slot_lock_inherited_on_descriptor_seventy(self) -> None:
        self.assert_prepared_lock_on_descriptor(70)

    def test_prepared_doc_keeps_the_slot_lock_inherited_on_descriptor_seventy_one(self) -> None:
        self.assert_prepared_lock_on_descriptor(71)

    def assert_prepared_lock_stays_out_of_the_sweep(self, command: tuple[str, ...]) -> None:
        with self.hold_background_sweep() as started:
            with self.prepared_doc_slot() as checkout:
                descriptor = build_folders.build_folder_lock_fd(checkout)
                lock = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
                process, log, marker = self.start_doc(command=command, pass_fds=(descriptor,))
                sweep_pid = wait_for_pid(started, processes=(process,))
                self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0, log.read_text())
                self.assertIn(sweep_pid, processes_carrying(marker))
                descriptors = Path(f"/proc/{sweep_pid}/fd")
                self.assertNotIn(lock, [entry.resolve() for entry in descriptors.iterdir()])
            self.assertEqual(build_folders.build_folder_state(checkout, wait=False), "free")

    def test_prepared_doc_lock_does_not_reach_the_background_sweep(self) -> None:
        self.assert_prepared_lock_stays_out_of_the_sweep(DOC_COMMAND)

    def test_prepared_doc_lock_above_descriptor_nine_does_not_reach_the_sweep(self) -> None:
        handoff = '''exec {descriptor}>&"$BUILDLOG_PREPARED_COPY_LOCK_FD"-
export BUILDLOG_PREPARED_COPY_LOCK_FD=$descriptor
exec "$@"'''
        self.assert_prepared_lock_stays_out_of_the_sweep((
            "bash", "-c", handoff, "prepared-doc", *DOC_COMMAND,
        ))

    def assert_doc_preserves_independent_open(self, handoff: str) -> None:
        self.install_scope_interpreter('''
import fcntl

try:
    descriptor = int(os.environ["TEST_INDEPENDENT_CHECKOUT_FD"])
    lock_fd = int(os.environ["BUILDLOG_PREPARED_COPY_LOCK_FD"])
    matches = os.path.samestat(os.fstat(descriptor), os.fstat(lock_fd))
    read_only = (fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE) == os.O_RDONLY
except OSError:
    matches = read_only = False
if not matches or not read_only:
    raise SystemExit("Verify did not preserve the independent lock file descriptor")
os.execv(sys.executable, [sys.executable, *arguments])
''')
        command = handoff + '''
export TEST_INDEPENDENT_CHECKOUT_FD=$independent
exec "$@"'''
        self.assert_doc_uses_the_prepared_copy((
            "bash", "-c", command, "prepared-doc", *DOC_COMMAND,
        ))

    def test_prepared_doc_preserves_an_independent_open_of_the_lock_file(self) -> None:
        self.assert_doc_preserves_independent_open('exec {independent}<"/dev/fd/$BUILDLOG_PREPARED_COPY_LOCK_FD"\n')

    def test_prepared_doc_preserves_an_independent_open_on_descriptor_sixty_nine(self) -> None:
        self.assert_doc_preserves_independent_open('''
exec {source}>&"$BUILDLOG_PREPARED_COPY_LOCK_FD"-
export BUILDLOG_PREPARED_COPY_LOCK_FD=$source
exec 69<"/dev/fd/$BUILDLOG_PREPARED_COPY_LOCK_FD"
independent=69
''')

    def test_doc_takes_its_own_slot_when_the_prepared_copy_names_another_tree(self) -> None:
        self.environment["BUILDLOG_PREPARED_COPY"] = str(self.root / "another-copy")
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        checkout = Path(self.cargo_calls()[0]["cwd"])
        self.assertNotEqual(checkout, self.repo)
        self.assertTrue(checkout.is_relative_to(self.root / "slots"))
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertEqual(len(list((self.root / "slots").glob("*-slot*.lease.json"))), 1)

    def test_doc_replaces_checkout_and_reuses_target_preserving_unchanged_mtimes(self) -> None:
        stamp = 1_000_000_000_000_000_000
        probe = '''
    if os.environ.get("TEST_DOC_STAMP"):
        stamp = int(os.environ["TEST_DOC_STAMP"])
        os.utime("beta/src/lib.rs", ns=(stamp, stamp))
    with Path(os.environ["TEST_DOC_TIMES"]).open("a") as output:
        output.write(str(Path("beta/src/lib.rs").stat().st_mtime_ns) + "\\n")
    assert Path("deleted.txt").is_file() == (os.environ.get("TEST_DOC_DELETED") == "present")
'''
        self.executable(self.root / "bin/cargo", CARGO_STUB.replace(
            'elif arguments[:1] == ["doc"]:', 'elif arguments[:1] == ["doc"]:' + probe))
        times = self.root / "doc-times"
        self.environment.update({"TEST_DOC_STAMP": str(stamp), "TEST_DOC_TIMES": str(times),
                                 "TEST_DOC_DELETED": "present"})
        removed = self.repo / "deleted.txt"
        _ = removed.write_text("remove in second tree\n", encoding="utf-8")
        _ = self.git("add", "deleted.txt")
        first = self.verify()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        initial = self.cargo_calls()[0]
        checkout = Path(initial["cwd"])
        self.assertTrue(checkout.is_relative_to(self.root / "slots"))
        self.assertTrue(checkout.exists())
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertEqual(initial["target"], str(self.root / "target"))
        del self.environment["TEST_DOC_STAMP"]
        self.environment["TEST_DOC_DELETED"] = "absent"
        _ = (self.repo / "alpha/src/lib.rs").write_text("pub fn next_tree() {}\n", encoding="utf-8")
        _ = self.git("add", "alpha/src/lib.rs")
        removed.unlink()
        _ = self.git("add", "-u", "--", "deleted.txt")
        second = self.verify()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        current = self.cargo_calls()[1]
        self.assertEqual(current["cwd"], initial["cwd"])
        self.assertTrue(Path(current["cwd"]).exists())
        self.assertEqual(build_folders.build_folder_state(Path(current["cwd"])), "free")
        self.assertEqual(current["target"], initial["target"])
        self.assertEqual(times.read_text().splitlines(), [str(stamp), str(stamp)])
        self.assertEqual(current["sources"]["alpha/src/lib.rs"], "pub fn next_tree() {}\n")

    def test_doc_target_defaults_to_repo_metadata_and_reuses_cache(self) -> None:
        del self.environment["CARGO_TARGET_DIR"]
        for run in range(2):
            with self.subTest(run=run):
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, second = self.cargo_calls()
        self.assertEqual(first["target"], str(self.root / "target"))
        self.assertEqual(second["target"], first["target"])
        self.assertEqual(second["cwd"], first["cwd"])
        self.assertTrue(all(Path(call["cwd"]).exists() for call in (first, second)))
        self.assertEqual({build_folders.build_folder_state(Path(call["cwd"])) for call in (first, second)}, {"free"})
        records = list((self.root / "slots").glob("*-slot*.lease.json"))
        self.assertEqual(len(records), 1)
        self.assertTrue((Path(first["cwd"]) / "target").is_dir())


if __name__ == "__main__":
    _ = unittest.main()
