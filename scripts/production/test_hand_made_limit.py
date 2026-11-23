"""Tests of the natedev read limit of hand made runs; each run uses the real bwrap."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import glob
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import override
import unittest
from unittest import mock

from scripts.build_hold import cargo_folder
from scripts.build_queue import queue_place
from scripts.production import hand_made, hand_made_limit
from scripts.production.hand_made_route import Program, Refused
from scripts.production.test_hand_made import FakePlace, namespace_members

BWRAP = shutil.which("bwrap")
NOT_ASSIGNED = "its link does not name the build folder of this checkout"
CRATE = '[package]\nname = "tiny"\nversion = "0.1.0"\nedition = "2021"\n'
# A git dependency that no cargo home holds, so cargo must fetch it.
GIT_CRATE = CRATE + '\n[dependencies]\nmissing = { git = "https://example.invalid/missing" }\n'
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
            return status, output.decode(errors="replace"), not hand_made_limit.setup_failed(status, status_read)
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

    def test_a_tiny_cargo_build_passes_and_its_private_sccache_server_ends_with_the_run(self) -> None:
        (self.checkout / "src").mkdir()
        _ = (self.checkout / "Cargo.toml").write_text(CRATE)
        _ = (self.checkout / "src" / "main.rs").write_text('fn main() { println!("tiny ran"); }\n')
        cache = self.root / "sccache"
        cache.mkdir()
        script = ("cargo build -q && ./target/debug/tiny && sccache --show-stats && readlink /proc/self/ns/pid "
                  + "&& cat /proc/[0-9]*/comm")
        status, output, ran = self.limited(["sh", "-c", script],
                                           self.environment(SCCACHE_DIR=str(cache), RUSTC_WRAPPER="sccache"))
        self.assertEqual((status, ran), (0, True), output)
        self.assertIn("tiny ran", output)
        requests = next(line for line in output.splitlines() if line.startswith("Compile requests "))
        self.assertGreater(int(requests.split()[-1]), 0, output)
        # The private server ran in the limit, with its socket in the attempt temp folder.
        self.assertIn("sccache", output.split("pid:[", 1)[1].splitlines()[1:])
        self.assertTrue((self.temp / "sccache.sock").exists())
        self.assertEqual(namespace_members("pid:[" + output.split("pid:[", 1)[1].split("]", 1)[0] + "]"), [])

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

    def test_the_build_hold_cargo_wrapper_folder_and_its_lib_are_read_only(self) -> None:
        hold, path = self.root / "scripts" / "build_hold", self.root / "bin"
        for folder in (hold, self.root / "scripts" / "lib", path):
            folder.mkdir(parents=True)
        _ = (hold / "cargo").write_text("#!/bin/sh\n")
        (hold / "cargo").chmod(0o755)
        (path / "cargo").symlink_to(hold / "cargo")
        words = " ".join(hand_made_limit.limit_argv("bwrap", Program(("true",), self.checkout, None, ()), self.temp,
                                                    {"HOME": os.environ["HOME"], "PATH": str(path)}, 3))
        for folder in (path, hold, self.root / "scripts" / "lib"):
            self.assertIn(f"--ro-bind {folder} {folder}", words)

    def test_a_bind_with_a_link_in_its_path_or_that_holds_the_home_folder_is_not_bound(self) -> None:
        home = Path(os.environ["HOME"])
        real = self.root / "real"
        real.mkdir()
        (self.root / "link").symlink_to(real)
        for folder in (self.root / "link", Path("/"), home.parent):
            with self.subTest(folder=folder):
                words = hand_made_limit.limit_argv("bwrap", Program(("true",), self.checkout, None, ()), self.temp,
                                                   {"HOME": str(home), "SCCACHE_DIR": str(folder)}, 3)
                self.assertNotIn(str(folder), [words[index + 1] for index, word in enumerate(words) if word == "--bind"])
        # A read only bind can have a link into /nix/store, as ~/.cargo/config.toml has.
        self.assertTrue(hand_made_limit.bindable(Path("/run/current-system/sw/bin/sh"), home, read=True))
        self.assertFalse(hand_made_limit.bindable(Path("/run/current-system/sw/bin/sh"), home, read=False))

    def test_an_entry_of_a_system_folder_that_root_does_not_own_is_hidden(self) -> None:
        entry = next(hand_made_limit.unowned(Path("/etc")), None)
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
        self.assertEqual(str(caught.exception), hand_made_limit.BWRAP_MISSING)
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
        self.assertEqual(str(caught.exception), hand_made_limit.BWRAP_FAILED.format(status=1))
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
            place=self.program.repo / "target", reason=NOT_ASSIGNED))
        self.assertEqual(place.started, [])

    def test_a_dependency_fetch_ends_the_attempt_as_a_refusal_with_the_no_network_line_first(self) -> None:
        checkout = self.program.repo
        (checkout / "src").mkdir()
        _ = (checkout / "Cargo.toml").write_text(GIT_CRATE)
        _ = (checkout / "src" / "main.rs").write_text("fn main() {}\n")
        output = io.BytesIO()
        run = hand_made.NatedevRun(FakePlace(), Program(("cargo", "build"), checkout, None, ()), self.root / "tmp")
        with mock.patch.dict(os.environ, {"PATH": program_path()}), self.assertRaises(Refused) as caught:
            _ = run.run(hand_made.OutputCopy(output, None))
        self.assertEqual(str(caught.exception), hand_made_limit.NO_NETWORK)
        text = output.getvalue().decode()
        note = text.index(f"hand made: {hand_made_limit.NO_NETWORK}\n")
        self.assertLess(note, text.index("error: failed to get `missing`"), text)
        self.assertIn("offline mode", text)


class OfflineNoteTests(unittest.TestCase):
    """The no network line comes just before a cargo error from offline mode, and only then."""

    def written(self, *chunks: bytes) -> tuple[bytes, bool]:
        output = io.BytesIO()
        noted = hand_made_limit.OfflineNote(hand_made.OutputCopy(output, None))
        for chunk in chunks:
            noted.write(chunk)
        noted.end()
        return output.getvalue(), noted.offline

    def test_an_offline_error_gets_the_line_before_it(self) -> None:
        note = f"hand made: {hand_made_limit.NO_NETWORK}\n".encode()
        self.assertEqual(self.written(b"Compiling a\nerror: failed to get `x`\n", b"\nCaused by:\n",
                                      b"  you are in the offline mode (--offline)\n", b"after\n"),
                         (b"Compiling a\n" + note + b"error: failed to get `x`\n\nCaused by:\n"
                          + b"  you are in the offline mode (--offline)\nafter\n", True))

    def test_an_offline_error_that_two_chunks_split_gets_the_line_before_it(self) -> None:
        note = f"hand made: {hand_made_limit.NO_NETWORK}\n".encode()
        for first, second in ((b"Compiling a\nerr", b"or: failed"), (b"Compiling a\n", b"error: failed"),
                              (b"Compiling a\nerror:", b" failed")):
            with self.subTest(first=first):
                self.assertEqual(self.written(first, second, b" to get `x`\n  offline mode (--offline)\n"),
                                 (b"Compiling a\n" + note + b"error: failed to get `x`\n  offline mode (--offline)\n",
                                  True))

    def test_other_output_and_another_error_pass_unchanged(self) -> None:
        chunks = (b"ran\n", b"error: could not compile `x`\n", *(b"line\n" for _ in range(30)),
                  b"the error: is in the line\n", b"err", b"and more\n", b"e")
        self.assertEqual(self.written(*chunks), (b"".join(chunks), False))


class OutputFolderTests(unittest.TestCase):
    """natedev takes no word that names another cargo build output folder."""

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
                                                               reason="a part of its path is a symbolic link"))
        self.assertEqual(hand_made_limit.output_refusal(("make",), root, {"HOME": str(home),
                                                                          "CARGO_TARGET_DIR": str(home.parent)}),
                         hand_made_limit.OUTPUT_REFUSAL.format(place=home.parent, reason="it holds the home folder"))

    def test_a_target_link_to_another_build_folder_or_to_the_home_folder_is_refused(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        checkout, build = root / "checkout", root / "build" / "repo" / "unit"
        for folder in (checkout, build / "short", build / "other"):
            folder.mkdir(parents=True)
        refused = hand_made_limit.OUTPUT_REFUSAL.format(place=checkout / "target", reason=NOT_ASSIGNED)
        assigned = cargo_folder.LinkedOutput(build / "short")
        for named in (build / "other", Path(os.environ["HOME"])):
            with self.subTest(named=named), mock.patch.object(cargo_folder, "assigned_output", return_value=assigned):
                (checkout / "target").unlink(missing_ok=True)
                (checkout / "target").symlink_to(named)
                self.assertEqual(hand_made_limit.output_refusal(("cargo", "build"), checkout, dict(os.environ)), refused)
                self.assertEqual(hand_made_limit.bound_output(checkout, dict(os.environ)), checkout / "target")

    def test_natedev_takes_no_run_that_names_another_output_folder(self) -> None:
        program = Program(("cargo", "build", "--target-dir", "other"), Path("/fixture/checkout"), None, ())
        with mock.patch.dict(os.environ, {}, clear=False):
            _ = os.environ.pop("CARGO_TARGET_DIR", None)
            self.assertEqual(hand_made.ROUTES[hand_made.LOCAL_MACHINE].refusal(program),
                             hand_made_limit.TARGET_REFUSAL.format(place="/fixture/checkout/target"))


if __name__ == "__main__":
    _ = unittest.main()
