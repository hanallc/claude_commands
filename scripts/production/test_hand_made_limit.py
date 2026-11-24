"""Tests of the natedev read limit of hand made runs; each run uses the real bwrap."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
from contextlib import ExitStack
import fcntl
import glob
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Unpack, override
import unittest
from unittest import mock

from scripts.build_hold import cargo_folder
from scripts.build_queue import queue_place
from scripts.build_queue.queue_place import PopenArguments
from scripts.build_targets import mac_linux
from scripts.lib import build_folder_lock, build_folders
from scripts.production import hand_made, hand_made_limit
from scripts.production.hand_made_route import Program, Refused
from scripts.production.test_hand_made import FakePlace, clear_build_variables
from scripts.worktree import worktrees

BWRAP = shutil.which("bwrap")
NOT_ASSIGNED = "its link does not name the build folder of this checkout"
# Connects to sockets of other processes, and to a loopback port of the program itself.
NETWORK = """
import socket
def result(family, address):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(address)
        return "open"
    except OSError:
        return "closed"
with socket.socket() as server:
    server.bind(("127.0.0.1", 0))
    server.listen()
    print(result(socket.AF_UNIX, "\\0/tmp/.X11-unix/X0"), result(socket.AF_INET, ("127.0.0.1", 4226)),
          result(socket.AF_INET, server.getsockname()))
"""


def program_path() -> str:
    """The program PATH with no build-hold cargo wrapper folder.

    A test runs outside a queue scope, where the wrapper takes its checked route; the queue place of a real run
    is in a builds.slice scope, where the wrapper starts the real cargo at once.
    """
    return os.pathsep.join(entry for entry in os.environ["PATH"].split(os.pathsep)
                           if entry and not hand_made_limit.wrapper_folders(entry))


def make_program() -> str | None:
    """make on PATH, else a make in /nix/store: the limit binds the store, so it runs there."""
    return shutil.which("make") or next(iter(sorted(glob.glob("/nix/store/*-gnumake-*/bin/make"))), None)


@unittest.skipIf(BWRAP is None, "bwrap is not on PATH")
class LimitTests(unittest.TestCase):
    """A program under the limit sees the bound paths only."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.checkout: Path = Path()
        self.outside: Path = Path()
        self.temp: Path = Path()

    @override
    def setUp(self) -> None:
        clear_build_variables(self)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.checkout, self.outside = self.root / "checkout", self.root / "outside"
        self.temp = self.root / "place" / "tmp"
        for folder in (self.checkout, self.outside, self.temp):
            folder.mkdir(parents=True)

    def environment(self, **values: str) -> dict[str, str]:
        return {**hand_made.program_environment(), "PATH": program_path(), **values}

    def limited(self, argv: Sequence[str], environment: Mapping[str, str] | None = None,
                checked: tuple[tuple[Path, str], ...] = ()) -> tuple[int, str, bool]:
        """Run argv in the checkout under the limit: the status, the output, and if bwrap started the program."""
        assert BWRAP is not None
        status_read, status_write = os.pipe()
        try:
            try:
                command = hand_made_limit.limit_argv(BWRAP, Program(tuple(argv), self.checkout, None, checked),
                                                     self.temp, environment or self.environment(), status_write)
                process = subprocess.Popen(command, cwd=self.checkout, env=environment or self.environment(),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           pass_fds=(status_write,))
            finally:
                os.close(status_write)
            output, _ = process.communicate(timeout=300)
            status = process.returncode
            return status, output.decode(errors="replace"), not mac_linux.setup_failed(status, status_read)
        finally:
            os.close(status_read)

    def exists(self, path: Path) -> bool:
        """The path exists under the limit."""
        status, output, ran = self.limited([sys.executable, "-c", "import os, sys; print(os.path.lexists(sys.argv[1]))",
                                            str(path)])
        self.assertEqual((status, ran), (0, True), output)
        return output.strip() == "True"

    def test_a_cc_prefix_option_cannot_reach_an_outside_folder(self) -> None:
        mark = self.root / "mark"
        assembler = self.outside / "p-as"
        _ = assembler.write_text(f"#!/bin/sh\ntouch {mark}\n")
        assembler.chmod(0o755)
        _ = (self.checkout / "t.c").write_text("int x;\n")
        command = ["cc", f"-B{self.outside}/p-", "-c", "t.c", "-o", "t.o"]
        # With no limit, cc runs the outside p-as.
        _ = subprocess.run(command, cwd=self.checkout, env=self.environment(), capture_output=True, check=False)
        self.assertTrue(mark.exists())
        mark.unlink()
        status, output, _ = self.limited(command)
        self.assertEqual(status, 0, output)
        self.assertFalse(mark.exists())
        self.assertFalse(self.exists(assembler))

    def test_a_second_make_folder_outside_the_checkout_does_not_exist(self) -> None:
        make = make_program()
        if make is None:
            self.skipTest("no make on PATH or in /nix/store")
        mark = self.root / "mark"
        _ = (self.outside / "Makefile").write_text(f"all:\n\ttouch {mark}\n")
        status, output, ran = self.limited([make, "-C", str(self.checkout), "-C", str(self.outside)])
        self.assertNotEqual(status, 0, output)
        self.assertTrue(ran)
        self.assertIn(str(self.outside), output)
        self.assertFalse(mark.exists())

    def test_a_link_in_a_read_only_folder_to_an_outside_file_cannot_be_read(self) -> None:
        # A PATH folder is bound read only, as /nix/store is; its link to an outside file leads nowhere.
        store = self.root / "store"
        store.mkdir()
        _ = (self.outside / "secret").write_text("secret\n")
        (store / "link").symlink_to(self.outside / "secret")
        environment = self.environment(PATH=f"{store}{os.pathsep}{program_path()}")
        self.assertIn(f"--ro-bind {store} {store}", " ".join(
            hand_made_limit.limit_argv("bwrap", Program(("true",), self.checkout, None, ()), self.temp, environment,
                                       3)))
        status, output, _ = self.limited(["cat", str(store / "link")], environment)
        self.assertNotEqual(status, 0)
        self.assertNotIn("secret", output.replace(str(self.outside / "secret"), ""))

    def test_a_checked_file_outside_the_checkout_stays_readable_and_its_folder_stays_hidden(self) -> None:
        script = self.outside / "x.sh"
        _ = script.write_text("echo checked ran\n")
        _ = (self.outside / "unread.sh").write_text("echo unread ran\n")
        status, output, _ = self.limited(["sh", "-c", f"sh {script}; sh {self.outside}/unread.sh"],
                                         checked=((script, hand_made.file_sha256(script)),))
        self.assertNotEqual(status, 0)
        self.assertIn("checked ran", output)
        self.assertNotIn("unread ran", output)

    def test_an_outside_fifo_is_not_visible(self) -> None:
        fifo = self.outside / "fifo"
        os.mkfifo(fifo)
        self.assertFalse(self.exists(fifo))
        self.assertFalse(self.exists(self.outside))

    def test_ssh_the_agent_socket_and_the_git_config_are_hidden(self) -> None:
        home = Path(os.environ["HOME"])
        for path in (home / ".ssh", home / ".config" / "git", home / ".cargo" / "credentials.toml",
                     Path(os.environ.get("XDG_RUNTIME_DIR") or "/run/user")):
            with self.subTest(path=path):
                self.assertFalse(self.exists(path))
        status, output, _ = self.limited(["sh", "-c", "echo ${SSH_AUTH_SOCK:-none}"])
        self.assertEqual((status, output), (0, "none\n"))

    def test_a_compile_passes_without_sccache_and_the_cargo_wrapper_is_off(self) -> None:
        # Use a C compile because the gate rejects tests that start Cargo or rustc.
        _ = (self.checkout / "tiny.c").write_text("int main(void) { return 0; }\n")
        cargo_home, cache = self.root / "cargo", self.root / "sccache"
        cargo_home.mkdir()
        cache.mkdir()
        config = '[build]\nrustc-wrapper = "sccache"\n'
        _ = (cargo_home / "config.toml").write_text(config)
        environment = self.environment(CARGO_HOME=str(cargo_home), SCCACHE_DIR=str(cache),
            RUSTC_WRAPPER="sccache", CARGO_BUILD_RUSTC_WRAPPER="sccache")
        script = ("""test "$RUSTC_WRAPPER" = "" && test "$CARGO_BUILD_RUSTC_WRAPPER" = "" """
                  + """&& test ! -e "$1" && cat "$CARGO_HOME/config.toml" """
                  + "&& cc -c tiny.c -o tiny.o && cat /proc/[0-9]*/comm")
        status, output, ran = self.limited(["sh", "-eu", "-c", script, "compile", str(cache)], environment)
        self.assertEqual((status, ran), (0, True), output)
        self.assertTrue((self.checkout / "tiny.o").exists())
        self.assertTrue(output.startswith(config), output)
        self.assertNotIn("sccache", output.removeprefix(config).splitlines())
        self.assertFalse((self.temp / "sccache.sock").exists())

    def test_a_tiny_python_test_folder_passes(self) -> None:
        (self.checkout / "tests").mkdir()
        _ = (self.checkout / "tests" / "test_tiny.py").write_text(
            "import tempfile, unittest\n\nclass TinyTests(unittest.TestCase):\n"
            + "    def test_temp(self) -> None:\n        self.assertEqual(tempfile.gettempdir(), '/tmp')\n")
        # natedev has no pytest; unittest runs the same folder.
        words = (["-m", "pytest", "-q", "tests"] if importlib.util.find_spec("pytest") is not None
                 else ["-m", "unittest", "discover", "-s", "tests"])
        status, output, _ = self.limited([sys.executable, *words])
        self.assertEqual(status, 0, output)

    def test_the_run_builds_in_cargo_target_dir(self) -> None:
        named = self.root / "named"
        named.mkdir()
        status, output, _ = self.limited(["touch", f"{named}/built"], self.environment(CARGO_TARGET_DIR=str(named)))
        self.assertEqual(status, 0, output)
        self.assertTrue((named / "built").exists())

    def test_the_run_builds_in_the_build_folder_of_the_checkout_that_its_target_link_names(self) -> None:
        build = self.root / "build" / "repo" / "unit" / "short"
        build.mkdir(parents=True)
        (self.checkout / "target").symlink_to(build)
        environment = self.environment()
        with mock.patch.object(cargo_folder, "assigned_output", return_value=cargo_folder.LinkedOutput(build)):
            self.assertEqual(hand_made_limit.output_refusal(("cargo", "build"), self.checkout, environment), "")
            status, output, _ = self.limited(["touch", "target/built"], environment)
        self.assertEqual(status, 0, output)
        self.assertTrue((build / "built").exists())

    def test_the_build_hold_cargo_wrapper_folder_and_its_lib_are_not_bound(self) -> None:
        hold, lib, path = self.root / "scripts" / "build_hold", self.root / "scripts" / "lib", self.root / "bin"
        for folder in (hold, lib, path):
            folder.mkdir(parents=True)
        _ = (hold / "cargo").write_text("#!/bin/sh\n")
        (hold / "cargo").chmod(0o755)
        (path / "cargo").symlink_to(hold / "cargo")
        words = hand_made_limit.limit_argv("bwrap", Program(("true",), self.checkout, None, ()), self.temp,
                                           {"HOME": os.environ["HOME"], "PATH": str(path)}, 3)
        bound = [words[index + 1] for index, word in enumerate(words) if word == "--ro-bind"]
        self.assertIn(str(path), bound)
        self.assertNotIn(str(hold), bound)
        self.assertNotIn(str(lib), bound)

    def test_a_bind_with_a_link_in_its_path_or_that_holds_the_home_folder_is_not_bound(self) -> None:
        home = Path(os.environ["HOME"])
        real = self.root / "real"
        real.mkdir()
        (self.root / "link").symlink_to(real)
        for folder in (self.root / "link", Path("/"), home.parent):
            with self.subTest(folder=folder):
                words = hand_made_limit.limit_argv("bwrap", Program(("true",), self.checkout, None, ()), self.temp,
                                                   {"HOME": str(home), "CARGO_TARGET_DIR": str(folder)}, 3)
                self.assertNotIn(str(folder), [words[index + 1] for index, word in enumerate(words) if word == "--bind"])
        # A read only bind can have a link into /nix/store, as ~/.cargo/config.toml has.
        self.assertTrue(mac_linux.bindable(Path("/run/current-system/sw/bin/sh"), home, read=True))
        self.assertFalse(mac_linux.bindable(Path("/run/current-system/sw/bin/sh"), home, read=False))

    def test_an_entry_of_a_system_folder_that_root_does_not_own_is_hidden(self) -> None:
        entry = next(mac_linux.unowned(Path("/etc")), None)
        if entry is None:
            self.skipTest("each entry of /etc is owned by root")
        status, output, _ = self.limited(["sh", "-c", f"ls -A {entry}; cat {entry} 2>/dev/null; echo end"])
        self.assertEqual((status, output), (0, "end\n"))

    def test_the_root_of_the_limit_is_not_writable(self) -> None:
        home = os.environ["HOME"]
        for path in ("/unbound", f"{home}/unbound", "/build/unbound"):
            with self.subTest(path=path):
                status, output, _ = self.limited(["mkdir", "-p", path])
                self.assertNotEqual(status, 0, output)
        status, output, _ = self.limited(["touch", "written", "/tmp/written"])
        self.assertEqual(status, 0, output)

    def test_the_program_reaches_no_socket_of_another_process_and_has_its_own_loopback(self) -> None:
        status, output, _ = self.limited([sys.executable, "-c", NETWORK])
        self.assertEqual((status, output), (0, "closed closed open\n"))

    def test_a_path_folder_that_holds_the_home_or_the_temp_folder_is_not_bound(self) -> None:
        home = os.environ["HOME"]
        words = hand_made_limit.limit_argv("bwrap", Program(("true",), self.checkout, None, ()), self.temp,
                                           {"HOME": home, "PATH": f"/:{Path(home).parent}:{self.root}:relative"}, 3)
        for folder in ("/", str(Path(home).parent), str(self.root), "relative"):
            self.assertNotIn(folder, [words[index + 1] for index, word in enumerate(words) if word == "--ro-bind"])


@unittest.skipIf(BWRAP is None, "bwrap is not on PATH")
class NatedevRunTests(unittest.TestCase):
    """The natedev route starts the program under the limit, or not at all."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.program: Program = Program((), Path(), None, ())

    @override
    def setUp(self) -> None:
        clear_build_variables(self)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        checkout = self.root / "checkout"
        checkout.mkdir()
        self.program = Program(("sh", "-c", f"touch {checkout}/ran /tmp/temp-mark"), checkout, None, ())

    def test_the_attempt_temp_folder_is_tmp_in_the_place_folder_and_goes_with_the_place(self) -> None:
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.root)}):
            with queue_place.queue_place_folder() as folder:
                run = hand_made.NatedevRun(FakePlace(), self.program, folder / "tmp")
                self.assertEqual(run.run(hand_made.OutputCopy(io.BytesIO(), None)), 0)
                self.assertTrue((folder / "tmp" / "temp-mark").exists())
        self.assertFalse(folder.exists())

    def test_a_missing_bwrap_starts_nothing_and_names_bwrap(self) -> None:
        place = FakePlace()
        run = hand_made.NatedevRun(place, self.program, self.root / "tmp")
        with mock.patch.object(shutil, "which", return_value=None), self.assertRaises(Refused) as caught:
            _ = run.run(hand_made.OutputCopy(io.BytesIO(), None))
        self.assertEqual(str(caught.exception), mac_linux.BWRAP_MISSING)
        self.assertEqual((place.started, run.started_at), ([], None))

    def test_a_limit_that_bwrap_cannot_set_up_starts_nothing_and_names_bwrap(self) -> None:
        words = hand_made_limit.limit_argv

        def broken(bwrap: str, program: Program, temp: Path, environment: Mapping[str, str],
                   status_fd: int) -> list[str]:
            command = words(bwrap, program, temp, environment, status_fd)
            end = command.index("--")
            return [*command[:end], "--ro-bind", str(self.root / "missing"), "/missing", *command[end:]]

        place, output = FakePlace(), io.BytesIO()
        run = hand_made.NatedevRun(place, self.program, self.root / "tmp")
        with mock.patch.object(hand_made_limit, "limit_argv", broken), self.assertRaises(Refused) as caught:
            _ = run.run(hand_made.OutputCopy(output, None))
        self.assertEqual(str(caught.exception), mac_linux.BWRAP_FAILED.format(status=1))
        self.assertIn(b"bwrap", output.getvalue())
        self.assertIsNone(run.started_at)
        self.assertFalse((self.program.repo / "ran").exists())

    def test_a_target_link_made_after_the_check_is_refused_before_the_start(self) -> None:
        (self.program.repo / "target").symlink_to(Path(os.environ["HOME"]))
        place = FakePlace()
        run = hand_made.NatedevRun(place, self.program, self.root / "tmp")
        with self.assertRaises(Refused) as caught:
            _ = run.run(hand_made.OutputCopy(io.BytesIO(), None))
        self.assertEqual(str(caught.exception), hand_made_limit.OUTPUT_REFUSAL.format(
            place=self.program.repo / "target", reason=NOT_ASSIGNED, advice=hand_made_limit.TARGET_LINK_ADVICE))
        self.assertEqual(place.started, [])

    def run_script(self, argv: tuple[str, ...], script: str,
                   place: FakePlace | None = None) -> tuple[bytes, Refused | int]:
        """Run the script as the start file of argv under the limit: the output, and the refusal or the status."""
        start = self.program.repo / "start"
        _ = start.write_text(f"#!/bin/sh\n{script}\n")
        start.chmod(0o755)
        output = io.BytesIO()
        run = hand_made.NatedevRun(place or FakePlace(), Program(argv, self.program.repo, start, ()),
                                   self.root / "tmp")
        result: Refused | int
        try:
            result = run.run(hand_made.OutputCopy(output, None))
        except Refused as refused:
            result = refused
        return output.getvalue(), result

    def assigned_cargo(self, unassigned: cargo_folder.UnassignedWorkspace | None = None, *,
                       enforce: bool = True) -> Path:
        """Make the checkout a cargo workspace whose build folder model gives a new build folder, or unassigned,
        and enforce the model or only report; the build folder, which does not exist yet. The variables of an
        outer step that did the folder steps go until the test ends."""
        checkout = self.program.repo
        _ = subprocess.run(["git", "-C", str(checkout), "init", "-q"], check=True, capture_output=True)
        _ = (checkout / "Cargo.toml").write_text("[workspace]\nmembers = []\n")
        root = self.root / "build"
        output = root / "repo" / "unit" / "short"
        for name in ("CARGO_FOLDER_CHECKED", "BUILDLOG_STEP_SCOPE"):
            _ = os.environ.pop(name, None)
        _ = self.enterContext(mock.patch.object(build_folders, "build_root", return_value=root))
        _ = self.enterContext(mock.patch.object(worktrees, "enforce_build_folders", return_value=enforce))
        _ = self.enterContext(mock.patch.object(cargo_folder, "assigned_output",
                                                return_value=unassigned or cargo_folder.LinkedOutput(output)))
        return output

    def recorded_locks(self) -> list[tuple[int, int]]:
        """Each lock descriptor that held_output gives the program, with the inode of its lock file."""
        locks: list[tuple[int, int]] = []
        original = hand_made_limit.held_output

        def recorded(argv: Sequence[str], checkout: Path, environment: Mapping[str, str],
                     held: ExitStack, *, note_outside: bool) -> tuple[int, ...]:
            lock = original(argv, checkout, environment, held, note_outside=note_outside)
            locks.extend((descriptor, os.fstat(descriptor).st_ino) for descriptor in lock)
            return lock

        _ = self.enterContext(mock.patch.object(hand_made_limit, "held_output", side_effect=recorded))
        return locks

    def lock_state(self, output: Path) -> str:
        """held or free: the state of the build folder lock."""
        return build_folder_lock.probe(output, self.root / "build", lambda: build_folder_lock.ProbeTiming(0, 0))

    def shared_lock(self, output: Path) -> str:
        """taken or busy: a second shared lock on the build folder lock file, as a parallel cargo step takes it."""
        with open(output.with_name(output.name + ".lock"), "rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return "busy"
        return "taken"

    def probed_place(self, probe: Callable[[], str], states: list[str]) -> FakePlace:
        """A place that adds the probe result to states as it starts the program."""
        place = FakePlace()
        start = place.popen

        def probed(argv: Sequence[str], **arguments: Unpack[PopenArguments]) -> subprocess.Popen[bytes]:
            states.append(probe())
            return start(argv, **arguments)

        _ = self.enterContext(mock.patch.object(place, "popen", side_effect=probed))
        return place

    def test_a_cargo_run_with_no_target_link_gets_the_link_to_its_build_folder_before_the_start(self) -> None:
        output = self.assigned_cargo()
        target = self.program.repo / "target"
        self.assertFalse(target.is_symlink())
        written, result = self.run_script(("cargo", "build"), "readlink target; touch target/built")
        self.assertEqual((written, result), (f"{output}\n".encode(), 0))
        self.assertEqual(target.readlink(), output)
        self.assertTrue((output / "built").exists())

    def test_the_build_folder_lock_is_held_while_cargo_runs_and_free_after_it_ends_or_fails(self) -> None:
        output = self.assigned_cargo()
        for argv, expected in ((("cargo", "build"), 0), (("cargo", "test"), 1)):
            with self.subTest(argv=argv):
                states: list[str] = []
                written, result = self.run_script(argv, 'test "$1" = build', self.probed_place(lambda: self.lock_state(output), states))
                self.assertEqual(result, expected, written)
                self.assertEqual(states, ["held"])
                self.assertEqual(self.lock_state(output), "free")

    def test_a_parallel_cargo_step_takes_a_shared_lock_after_the_run_makes_the_target_link(self) -> None:
        output = self.assigned_cargo()
        self.assertFalse((self.program.repo / "target").is_symlink())
        states: list[str] = []
        written, result = self.run_script(("cargo", "build"), "true",
                                          self.probed_place(lambda: self.shared_lock(output), states))
        self.assertEqual((result, states), (0, ["taken"]), written)

    def test_a_stale_or_a_dangling_target_link_gets_the_repair_and_the_run_starts(self) -> None:
        output = self.assigned_cargo()
        target, old = self.program.repo / "target", output.with_name("old")
        old.mkdir(parents=True)
        for named in (old, output):
            with self.subTest(named=named):
                shutil.rmtree(output, ignore_errors=True)
                target.unlink(missing_ok=True)
                target.symlink_to(named)
                program = Program(("cargo", "build"), self.program.repo, None, ())
                self.assertEqual(hand_made.ROUTES[hand_made.LOCAL_MACHINE].refusal(program), "")
                self.assertEqual(target.readlink(), output)
                written, result = self.run_script(("cargo", "build"), "readlink target; touch target/built")
                self.assertEqual((written, result), (f"{output}\n".encode(), 0))
                self.assertTrue((output / "built").exists())

    def test_a_cargo_run_writes_the_use_stamp_of_its_build_folder(self) -> None:
        output = self.assigned_cargo()
        written, result = self.run_script(("cargo", "build"), "true")
        self.assertEqual(result, 0, written)
        self.assertEqual((output / build_folders.COPY_USE_STAMP).read_text(), f"{self.program.repo}\n")

    def test_a_refusal_of_the_cargo_folder_check_starts_nothing_and_names_the_reason(self) -> None:
        reason = f"{self.program.repo}: no build folder. Use worktrees.py make <repo> <unit> <short-name>"
        _ = self.assigned_cargo(cargo_folder.UnassignedWorkspace(reason))
        place = FakePlace()
        written, result = self.run_script(("cargo", "build"), "touch ran", place)
        self.assertIsInstance(result, Refused, written)
        self.assertEqual(str(result), hand_made_limit.FOLDER_STEP_REFUSAL.format(reason=reason))
        self.assertEqual(place.started, [])
        self.assertFalse((self.program.repo / "ran").exists())

    def test_the_program_holds_the_lock_descriptor_and_sees_the_lock_held_while_it_runs(self) -> None:
        _ = self.assigned_cargo()
        locks = self.recorded_locks()
        written, result = self.run_script(("cargo", "build"), "grep -Hs FLOCK /proc/self/fdinfo/* || true")
        self.assertEqual(result, 0, written)
        [(descriptor, inode)] = locks
        self.assertRegex(written.decode(), rf"/proc/self/fdinfo/{descriptor}:lock:.*FLOCK.*:{inode} ")

    def test_the_build_folder_lock_is_free_after_a_refusal_that_comes_after_the_lock_is_taken(self) -> None:
        output = self.assigned_cargo()
        states: list[str] = []

        def limit_refused(*_: object) -> str:
            states.append(self.lock_state(output))
            return "refused by the limit"

        with self.subTest(refusal="limit"), mock.patch.object(hand_made_limit, "limit_refusal",
                                                              side_effect=limit_refused):
            _, result = self.run_script(("cargo", "build"), "true")
            self.assertEqual((str(result), states), ("refused by the limit", ["held"]))
            self.assertEqual(self.lock_state(output), "free")
        with self.subTest(refusal="offline"):
            states.clear()
            _, result = self.run_script(("cargo", "build"), "echo '--offline was specified'; exit 101",
                                        self.probed_place(lambda: self.lock_state(output), states))
            self.assertEqual((str(result), states), (hand_made_limit.NO_NETWORK, ["held"]))
            self.assertEqual(self.lock_state(output), "free")
        with self.subTest(refusal="folder check"), mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": "elsewhere"}):
            (self.program.repo / "target").unlink()
            _, result = self.run_script(("cargo", "build"), "touch ran")
            self.assertIsInstance(result, Refused)
            self.assertIn("Cargo must use", str(result))
            self.assertEqual((self.program.repo / "target").readlink(), output)
            self.assertFalse((self.program.repo / "ran").exists())
            self.assertEqual(self.lock_state(output), "free")

    def test_in_report_mode_an_unassigned_checkout_runs_with_a_note_and_no_lock(self) -> None:
        reason = f"{self.program.repo}: no build folder. Use worktrees.py make <repo> <unit> <short-name>"
        _ = self.assigned_cargo(cargo_folder.UnassignedWorkspace(reason), enforce=False)
        locks = self.recorded_locks()
        note = io.StringIO()
        with contextlib.redirect_stderr(note):
            written, result = self.run_script(("cargo", "build"), "touch ran; ! grep -qs FLOCK /proc/self/fdinfo/*")
        self.assertEqual((result, note.getvalue(), locks), (0, f"cargo: {reason}\n", []), written)
        self.assertTrue((self.program.repo / "ran").exists())

    def test_in_report_mode_the_attempt_start_check_and_the_run_give_the_note_once(self) -> None:
        reason = f"{self.program.repo}: no build folder. Use worktrees.py make <repo> <unit> <short-name>"
        _ = self.assigned_cargo(cargo_folder.UnassignedWorkspace(reason), enforce=False)
        note = io.StringIO()
        with contextlib.redirect_stderr(note):
            refusal = hand_made_limit.start_refusal(("cargo", "build"), self.program.repo,
                                                    hand_made.program_environment())
            written, result = self.run_script(("cargo", "build"), "touch ran")
        self.assertEqual((refusal, result, note.getvalue()), ("", 0, f"cargo: {reason}\n"), written)

    def test_a_cargo_run_does_the_folder_steps_only_when_the_wrapper_does(self) -> None:
        clear = {"CARGO_FOLDER_CHECKED": "", "BUILDLOG_STEP_SCOPE": ""}
        # The value of the own scope skips the steps; a value from another scope is stale.
        cgroup = self.root / "cgroup"
        _ = cgroup.write_text("0::/user.slice/builds.slice/builds-detached.slice/run-42.scope\n")
        own_scope = {**clear, "CARGO_WRAPPER_CGROUP_FILE": str(cgroup),
                     "BUILDLOG_STEP_SCOPE": "/sys/fs/cgroup/user.slice/builds.slice/builds-detached.slice/run-42.scope"}
        for argv, variables, due in (
                (("cargo", "build"), clear, True), (("cargo", "+nightly", "--config", "x", "test"), clear, True),
                (("cargo", "clippy", "--build-info"), clear, True), (("cargo", "test", "--", "--help"), clear, True),
                (("cargo", "install", "x"), clear, False), (("cargo", "build", "--help"), clear, False),
                (("cargo", "test", "-h"), clear, False), (("cargo", "check", "-V"), clear, False),
                (("cargo", "nextest", "--version"), clear, False), (("cargo", "mend", "--build-info"), clear, False),
                (("cargo", "metadata"), clear, False), (("cargo", "--color", "build", "tree"), clear, False),
                (("cargo", "build"), {**clear, "CARGO_FOLDER_CHECKED": "1"}, False),
                (("cargo", "build"), {**clear, "BUILDLOG_STEP_SCOPE": "/step.scope"}, True),
                (("cargo", "build"), own_scope, False)):
            with (self.subTest(argv=argv, variables=variables), mock.patch.dict(os.environ, variables),
                  mock.patch.object(cargo_folder, "checked_step", return_value=cargo_folder.NoOutputLock()) as step,
                  ExitStack() as held):
                self.assertEqual(hand_made_limit.held_output(argv, self.program.repo, {"HOME": "/home"}, held,
                                                             note_outside=True), ())
                self.assertEqual(step.called, due)
                if due:
                    step.assert_called_once_with(list(argv[1:]), self.program.repo, held, {"HOME": "/home"},
                                                 note_outside=True)

    def test_a_cargo_failure_with_an_offline_text_in_colour_is_the_no_network_refusal(self) -> None:
        _ = self.assigned_cargo()
        output, result = self.run_script(("cargo", "build"), "printf '\\033[1m\\033[91merror\\033[0m: failed to "
                                         + "download\\n  but \\033[1m--offline\\033[0m was specified\\n'; exit 101")
        self.assertIsInstance(result, Refused)
        self.assertEqual(str(result), hand_made_limit.NO_NETWORK)
        self.assertIn(b"\x1b[91merror\x1b[0m: failed to download\n", output)

    def test_another_program_with_an_error_and_the_offline_word_is_no_refusal(self) -> None:
        output, result = self.run_script(("sh", "-c", "x"), "echo 'error: x'; echo 'see --offline'; exit 1")
        self.assertEqual((output, result), (b"error: x\nsee --offline\n", 1))


class OfflineFailureTests(unittest.TestCase):
    """Only a cargo failure with a cargo offline text is the no network refusal; output passes at once."""

    def test_output_passes_at_once_and_unchanged(self) -> None:
        output = io.BytesIO()
        tail = hand_made_limit.OutputTail(hand_made.OutputCopy(output, None))
        tail.write(b"error: x\n")
        self.assertEqual(output.getvalue(), b"error: x\n")
        tail.write(b"y" * hand_made_limit.TAIL_BYTES)
        self.assertEqual(len(tail.tail), hand_made_limit.TAIL_BYTES)

    def test_a_cargo_status_0_or_another_program_is_no_refusal(self) -> None:
        text = b"attempting to make an HTTP request, but --offline was specified\n"
        self.assertTrue(hand_made_limit.offline_failure(("/bin/cargo", "build"), 101, text))
        self.assertFalse(hand_made_limit.offline_failure(("cargo", "build"), 0, text))
        self.assertFalse(hand_made_limit.offline_failure(("sh", "-c", "cargo build"), 101, text))
        self.assertFalse(hand_made_limit.offline_failure(("cargo", "build"), 101, b"error: x\nsee --offline\n"))


class OutputFolderTests(unittest.TestCase):
    """natedev takes no word that names another cargo build output folder."""

    @override
    def setUp(self) -> None:
        clear_build_variables(self)

    def test_a_word_that_names_another_output_folder_is_refused(self) -> None:
        checkout = Path("/fixture/checkout")
        refused = hand_made_limit.TARGET_REFUSAL.format(place=checkout / "target")
        for argv in (("cargo", "build", "--target-dir", "other"), ("cargo", "build", "--target-dir=/tmp/x"),
                     ("cargo", "build", "--target-dir"), ("cargo", "build", "--config", 'build.target-dir="x"'),
                     ("cargo", "build", "--config=build.target-dir=x")):
            with self.subTest(argv=argv):
                self.assertEqual(hand_made_limit.output_refusal(argv, checkout, {}), refused)
        for argv in (("cargo", "build"), ("cargo", "build", "--target-dir", "target"),
                     ("cargo", "build", f"--target-dir={checkout}/target")):
            with self.subTest(argv=argv):
                self.assertEqual(hand_made_limit.output_refusal(argv, checkout, {}), "")

    def test_cargo_target_dir_is_the_output_folder(self) -> None:
        checkout, named = Path("/fixture/checkout"), {"CARGO_TARGET_DIR": "/fixture/out"}
        self.assertEqual(hand_made_limit.output_refusal(("cargo", "build", "--target-dir", "/fixture/out"), checkout,
                                                        named), "")
        self.assertEqual(hand_made_limit.output_refusal(("cargo", "build", "--target-dir", "target"), checkout, named),
                         hand_made_limit.TARGET_REFUSAL.format(place="/fixture/out"))

    def test_only_a_cargo_word_before_the_first_double_dash_is_checked(self) -> None:
        checkout = Path("/fixture/checkout")
        for argv in (("rg", "target-dir", "src"), ("cargo", "run", "--", "--target-dir", "x")):
            with self.subTest(argv=argv):
                self.assertEqual(hand_made_limit.output_refusal(argv, checkout, {}), "")

    def test_an_output_folder_with_a_link_or_that_holds_the_home_folder_is_refused(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        home = Path(os.environ["HOME"])
        (root / "elsewhere").mkdir()
        (root / "link").symlink_to(root / "elsewhere")
        self.assertEqual(hand_made_limit.output_refusal(("make",), root, {"HOME": str(home), "CARGO_TARGET_DIR": "link"}),
                         hand_made_limit.OUTPUT_REFUSAL.format(place=root / "link",
                                                               reason="a part of its path is a symbolic link",
                                                               advice=hand_made_limit.NAMED_OUTPUT_ADVICE))
        self.assertEqual(hand_made_limit.output_refusal(("make",), root, {"HOME": str(home),
                                                                          "CARGO_TARGET_DIR": str(home.parent)}),
                         hand_made_limit.OUTPUT_REFUSAL.format(place=home.parent, reason="it holds the home folder",
                                                               advice=hand_made_limit.NAMED_OUTPUT_ADVICE))

    def test_a_target_link_to_another_build_folder_or_to_the_home_folder_is_refused(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        checkout, build = root / "checkout", root / "build" / "repo" / "unit"
        for folder in (checkout, build / "short", build / "other"):
            folder.mkdir(parents=True)
        refused = hand_made_limit.OUTPUT_REFUSAL.format(place=checkout / "target", reason=NOT_ASSIGNED,
                                                        advice=hand_made_limit.TARGET_LINK_ADVICE)
        assigned = cargo_folder.LinkedOutput(build / "short")
        for named in (build / "other", Path(os.environ["HOME"])):
            with self.subTest(named=named), mock.patch.object(cargo_folder, "assigned_output", return_value=assigned):
                (checkout / "target").unlink(missing_ok=True)
                (checkout / "target").symlink_to(named)
                self.assertEqual(hand_made_limit.output_refusal(("cargo", "build"), checkout, dict(os.environ)), refused)
                self.assertEqual(hand_made_limit.bound_output(checkout, dict(os.environ)), checkout / "target")

    def test_a_checkout_that_holds_or_is_in_a_tool_folder_is_refused_and_a_worktree_is_not(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        hold = root / "scripts" / hand_made_limit.BUILD_HOLD
        hold.mkdir(parents=True)
        (hold / "cargo").touch(mode=0o755)
        checker = Path(hand_made_limit.__file__).resolve().parents[2]
        # A fixture home outside the checker: a home in the checkout, as on the guest, makes the output refusal first.
        for checkout in (checker, checker / "scripts"):
            with self.subTest(checkout=checkout), mock.patch.dict(os.environ, {"HOME": str(root / "home")}):
                self.assertEqual(hand_made.ROUTES[hand_made.LOCAL_MACHINE].refusal(Program(("true",), checkout, None, ())),
                                 hand_made_limit.TOOLS_REFUSAL.format(bind=checkout))
        refused = hand_made_limit.TOOLS_REFUSAL.format(bind=root)
        self.assertEqual(hand_made_limit.tools_refusal(root, {"PATH": str(hold)}), refused)
        with mock.patch.dict(os.environ, {"CONF_DIR": str(root / "config")}):
            self.assertEqual(hand_made_limit.tools_refusal(root, {}), refused)
        with mock.patch.dict(os.environ, {"CONF_DIR": str(root / "config")}):
            self.assertEqual(hand_made_limit.tools_refusal(root / "worktree", {"PATH": str(root / "bin")}), "")

    def test_an_output_folder_that_holds_or_is_in_a_tool_folder_is_refused(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        checkout = root / "worktree"
        for folder in (checkout, root / "config", root / "cache"):
            folder.mkdir()
        scripts = Path(hand_made_limit.__file__).resolve().parents[1]
        with mock.patch.dict(os.environ, {"CONF_DIR": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}):
            for bind in (scripts, root / "config", root / "cache"):
                with self.subTest(bind=bind):
                    self.assertEqual(hand_made_limit.tools_refusal(checkout,
                                                                  {"HOME": str(root), "CARGO_TARGET_DIR": str(bind)}),
                                     hand_made_limit.TOOLS_REFUSAL.format(bind=bind))
            self.assertEqual(hand_made_limit.tools_refusal(checkout, {"HOME": str(root)}), "")

    def test_natedev_takes_no_run_that_names_another_output_folder(self) -> None:
        program = Program(("cargo", "build", "--target-dir", "other"), Path("/fixture/checkout"), None, ())
        # An outer step did the folder steps, so only the output words decide.
        with mock.patch.dict(os.environ, {"CARGO_FOLDER_CHECKED": "1"}):
            _ = os.environ.pop("CARGO_TARGET_DIR", None)
            self.assertEqual(hand_made.ROUTES[hand_made.LOCAL_MACHINE].refusal(program),
                             hand_made_limit.TARGET_REFUSAL.format(place="/fixture/checkout/target"))


if __name__ == "__main__":
    _ = unittest.main()
