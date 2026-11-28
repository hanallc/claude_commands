"""Run hand made attempts on the Mac and on many machines with an injected Mac transport; no ssh call."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import io
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import TextIO, cast, final, override
import unittest
from unittest import mock

from scripts.copies import copies
from scripts.lib import build_folders
from scripts.lint import sweep
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE_SECONDS, config_environment, write_config_fixture
from scripts.mac_test import claim_state, eligibility, mac_test, offload
from scripts.mac_test import floor_claims
from scripts.mac_test.offload_test_commands import write_shell
from scripts.mac_test.mac_transport import MacPlacement, RemoteRun, RemoteState
from scripts.production import hand_made, hand_made_mac
from scripts.production.hand_made_mac import START, MacLines, MacTransport, MirrorUse
from scripts.production.hand_made_route import (PROGRAM_VARIABLES, Line, Machine, MachineFailed, MachineRun, Placed,
                                                Program)
from scripts.production.test_hand_made import SCRIPT, YES, AttemptCase, local_grant, remote

SESSION_VARIABLES = ("UNIT_RUN_DIR", "PLAN_DELEGATE_BOARD_DIR", "UNIT_SESSION_DIR", "PLAN_DELEGATE_SESSION_DIR")
NATEDEV_RAN = b"hand made ran\n"
MIRROR = "mirror/repo"
PLACEMENT = MacPlacement("mac-host", "fixture-episode", "attempt-mac")
ZSH = shutil.which("zsh")


@dataclass
class FakeMac:
    """The Mac side of SSH: the lines of the start script and the program, then the wrapper exit lines."""
    lines: tuple[str, ...] = (START + "\n", "mac ran\n")
    exit: int | None = 0
    group: int = 42
    copy_error: str = ""
    commands: list[list[str]] = field(default_factory=list[list[str]])
    copies: list[tuple[Path, str]] = field(default_factory=list[tuple[Path, str]])
    stops: list[int] = field(default_factory=list[int])

    def stream(self, command: list[str], remote: RemoteRun, ssh: ExitStack, write: Callable[[str], None]) -> int:
        del ssh
        self.commands.append(command)
        remote.state, remote.group = RemoteState.LIVE, self.group
        for line in self.lines:
            write(line)
        if self.exit is None:
            return 255
        write("\n")
        write(f"mac_exit={self.exit}\n")
        remote.state = RemoteState.EXITED
        return self.exit

    def stop(self, placement: MacPlacement, remote: RemoteRun) -> bool:
        del placement
        self.stops.append(remote.group)
        # As stop_remote: with no group, the stop cannot prove the end.
        return remote.group != 0

    @contextmanager
    def mirror(self, repo: Path, host: str) -> Generator[MirrorUse]:
        if self.copy_error:
            raise MachineFailed(self.copy_error)
        self.copies.append((repo, host))
        yield MirrorUse(MIRROR, lambda command: command)

    def route(self, line: Line, placed: Placed, program: Program) -> AbstractContextManager[MachineRun]:
        return hand_made_mac.mac_route(line, placed, program, MacTransport(self.stream, self.stop, self.mirror))


def checked(repo: Path, name: str = "check.py") -> tuple[tuple[Path, str], ...]:
    return ((repo / name, hashlib.sha256((repo / name).read_bytes()).hexdigest()),)


@final
class MacAttemptTests(AttemptCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.mac = FakeMac()

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "MAC_TEST_STATE_DIR": str(self.root / "mac-test"), "CLAUDE_CODE_SESSION_ID": "this-session"})))
        for name in SESSION_VARIABLES:
            _ = os.environ.pop(name, None)
        self.mac = FakeMac()
        self.routes["mac"] = Machine(self.mac.route, hand_made_mac.mac_refusal)

    def block(self, session: str) -> None:
        now = datetime.now(UTC)
        path = claim_state.state_paths().block
        path.parent.mkdir(parents=True, exist_ok=True)
        mac_test.write_block(path, mac_test.ActiveMacBlock(
            "fixture-holder", "fixture work\nneeds the Mac", now, now + timedelta(hours=1),
            mac_test.RecordedSession(session, "fixture-holder"),
            mac_test.NO_SHOWRUNNER, "not_configured", mac_test.FREE_MESSAGE_NOT_NEEDED))

    def mac_attempt(self, *answers: Placed, machines: tuple[str, ...] = ("natedev", "mac"),
                    argv: tuple[str, ...] = ()) -> tuple[int, str]:
        self.line.answers = list(answers)
        return self.attempt(argv, machines=machines)

    def test_a_mac_run_copies_the_checkout_and_sends_the_checked_files_and_the_placement(self) -> None:
        status, _ = self.mac_attempt(remote(), machines=("mac",))
        self.assertEqual((status, self.output.getvalue()), (0, b"mac ran\n"))
        self.assertEqual(self.mac.copies, [(self.repo, "mac-host")])
        command = self.mac.commands[0]
        self.assertEqual(command[:4], ["ssh", "-o", "BatchMode=yes", "mac-host"])
        for text in ("fixture-episode/attempt-mac", MIRROR, hashlib.sha256(SCRIPT.encode()).hexdigest(), "check.py"):
            self.assertIn(text, command[-1])
        self.assertEqual((self.line.closed, self.line.given_back, self.line.entered), ([("mac", 0, "")], [], []))
        row = self.only_row()
        self.assertEqual((row["machine"], row["machines"], row["status"], row["peak_mem_bytes"], row["failed_machines"]),
                         ("mac", ["mac"], 0, None, []))
        self.assertGreaterEqual(str(row["run_started_at"]), str(row["placed_at"]))
        self.assertEqual((self.root / "place" / "output.log").read_bytes(), b"mac ran\n")

    def test_the_mac_program_status_is_the_attempt_status(self) -> None:
        self.mac.exit = 3
        status, _ = self.mac_attempt(remote(), machines=("mac",))
        self.assertEqual(status, 3)
        self.assertEqual(self.line.closed, [("mac", 3, "")])
        self.assertEqual(self.only_row()["status"], 3)

    def test_a_run_that_names_both_machines_goes_to_the_machine_that_the_queue_gives(self) -> None:
        for answer, machine, output in ((local_grant(), "natedev", NATEDEV_RAN), (remote(), "mac", b"mac ran\n")):
            with self.subTest(machine=machine):
                for path in (self.root / "buildlog").glob("*/*.jsonl"):
                    path.unlink()
                _ = self.output.seek(0)
                _ = self.output.truncate()
                status, _ = self.mac_attempt(answer)
                self.assertEqual((status, self.output.getvalue()), (0, output))
                self.assertEqual(self.line.requests[-1].machines, ("natedev", "mac"))
                row = self.only_row()
                self.assertEqual((row["machine"], row["machines"]), (machine, ["natedev", "mac"]))

    def test_the_files_are_checked_on_natedev_before_the_run_goes_to_the_mac(self) -> None:
        def change(argv: list[str]) -> subprocess.CompletedProcess[str]:
            _ = (self.repo / "check.py").write_text('print("changed")\n')
            return self.answer(YES)(argv)

        self.reply = change
        status, errors = self.mac_attempt(remote(), machines=("mac",))
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertIn("check.py changed after the check", errors)
        self.assertEqual((self.mac.copies, self.mac.commands), ([], []))
        self.assertEqual(self.line.closed, [("mac", None, "a checked file changed after the check")])

    def test_a_folder_word_that_the_check_refuses_at_the_run_start_takes_no_mac_run(self) -> None:
        (self.repo / "out").mkdir()

        def ignore(argv: list[str]) -> subprocess.CompletedProcess[str]:
            _ = (self.repo / ".gitignore").write_text("/out\n")
            return self.answer(YES)(argv)

        self.reply = ignore
        status, errors = self.mac_attempt(remote(), machines=("mac",), argv=(sys.executable, "check.py", "out"))
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertIn("a folder changed after the check: " + hand_made.FOLDER_REFUSAL.format(
            word="out", entry="the folder", folder=self.repo / "out", reason="git ignores"), errors)
        self.assertEqual((self.mac.copies, self.mac.commands), ([], []))
        self.assertEqual(self.line.closed, [("mac", None, "a checked file changed after the check")])

    def test_a_mac_file_that_differs_from_the_checked_file_refuses_the_run(self) -> None:
        self.mac.lines, self.mac.exit = (hand_made_mac.REFUSED + "check.py on the Mac is not the checked file\n",), 1
        status, errors = self.mac_attempt(remote(), local_grant())
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertIn("the Mac refused the run: check.py on the Mac is not the checked file", errors)
        self.assertEqual((self.line.given_back, self.line.entered), ([], []))
        self.assertEqual([(machine, status) for machine, status, _ in self.line.closed], [("mac", None)])
        self.assertEqual(self.output.getvalue(), b"")
        row = self.only_row()
        self.assertEqual((row["status"], row["run_started_at"]), (None, None))

    def test_a_missing_mac_folder_gives_the_run_back_to_natedev(self) -> None:
        self.mac.lines, self.mac.exit = ("cd: no such file or directory: mirror/repo\n",), 1
        status, _ = self.mac_attempt(remote(), local_grant())
        self.assertEqual(status, 0)
        self.assertEqual(self.mac.stops, [])
        self.assertEqual(self.line.given_back, [("mac", "the Mac did not start the program (status 1)")])
        self.assertEqual(self.line.closed, [("natedev", 0, "")])
        self.assertTrue(self.output.getvalue().endswith(NATEDEV_RAN))

    def test_a_failed_copy_gives_the_run_back_to_natedev(self) -> None:
        self.mac.copy_error = "the copy to the Mac mirror failed: connection refused"
        status, _ = self.mac_attempt(remote(), local_grant())
        self.assertEqual((status, self.mac.commands), (0, []))
        self.assertEqual(self.line.given_back, [("mac", self.mac.copy_error)])

    def test_an_unchecked_first_word_on_the_mac_is_refused_before_the_model_call(self) -> None:
        status, errors = self.mac_attempt(machines=("mac",), argv=("/opt/tools/run",))
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertIn("mac cannot take the run: the first word /opt/tools/run is in no Mac program folder", errors)
        self.assertEqual((self.calls, self.mac.commands), ([], []))

    def test_a_checked_file_that_the_copy_leaves_out_keeps_the_run_on_natedev(self) -> None:
        (self.repo / "target").mkdir()
        _ = (self.repo / "target" / "gen.sh").write_text("echo hand made ran\n")
        status, errors = self.mac_attempt(local_grant(), argv=("sh", "target/gen.sh"))
        self.assertEqual((status, self.output.getvalue()), (0, NATEDEV_RAN))
        self.assertIn("mac cannot take the run: target/gen.sh is a checked file that the copy to the Mac leaves out",
                      errors)
        self.assertEqual((self.line.requests[-1].machines, self.mac.commands), (("natedev",), []))

    def test_a_checked_link_to_a_file_outside_the_checkout_keeps_the_run_on_natedev(self) -> None:
        shared = self.root / "shared"
        shared.mkdir()
        _ = (shared / "x.sh").write_text("echo hand made ran\n")
        (self.repo / "tools").mkdir()
        (self.repo / "tools" / "x.sh").symlink_to(shared / "x.sh")
        status, errors = self.mac_attempt(local_grant(), argv=("bash", "tools/x.sh"))
        self.assertEqual((status, self.output.getvalue()), (0, NATEDEV_RAN))
        self.assertIn(f"mac cannot take the run: {self.repo / 'tools' / 'x.sh'} is a checked file outside the "
                      + "checkout or a path with a link in it", errors)
        self.assertEqual((self.line.requests[-1].machines, self.mac.commands), (("natedev",), []))

    def test_a_checked_path_with_dot_dot_after_a_link_keeps_the_run_on_natedev(self) -> None:
        outside = self.root / "outside"
        (outside / "sub").mkdir(parents=True)
        _ = (outside / "x.sh").write_text("echo hand made ran\n")
        _ = (self.repo / "x.sh").write_text("echo hand made ran\n")
        (self.repo / "link").symlink_to(outside / "sub")
        status, errors = self.mac_attempt(local_grant(), argv=("bash", "link/../x.sh"))
        # The kernel takes .. after the link, so bash opens outside/x.sh. The read limit hides that folder.
        self.assertEqual((status, self.output.getvalue()),
                         (127, f"{shutil.which('bash')}: link/../x.sh: No such file or directory\n".encode()))
        self.assertIn("the word link/../x.sh has a .. part", errors)
        self.assertEqual((self.line.requests[-1].machines, self.mac.commands), (("natedev",), []))

    def test_a_checked_path_with_dot_dot_after_a_link_back_into_the_checkout_keeps_the_run_on_natedev(self) -> None:
        (self.repo / "sub").mkdir()
        _ = (self.repo / "x.sh").write_text("echo hand made ran\n")
        (self.repo / "a").symlink_to("sub")
        status, errors = self.mac_attempt(local_grant(), argv=("bash", "a/../x.sh"))
        self.assertEqual((status, self.output.getvalue()), (0, NATEDEV_RAN))
        self.assertIn("the word a/../x.sh has a .. part", errors)
        self.assertEqual((self.line.requests[-1].machines, self.mac.commands), (("natedev",), []))


    def test_a_checkout_that_the_mac_cannot_mirror_stays_out_of_the_mac_line(self) -> None:
        with mock.patch.object(eligibility, "repository_identity", return_value=eligibility.UNKNOWN_REPOSITORY):
            status, errors = self.mac_attempt(local_grant())
            self.assertEqual(status, 0)
            self.assertIn("mac cannot take the run: the checkout has no repository name", errors)
            self.assertEqual(self.line.requests[-1].machines, ("natedev",))
            self.assertIn("Machines: natedev\n", self.calls[-1][-1])
            self.assertEqual(self.only_row()["machines"], ["natedev", "mac"])
            for path in (self.root / "buildlog").glob("*/*.jsonl"):
                path.unlink()
            status, _ = self.mac_attempt(remote(), machines=("mac",))
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.mac.commands, [])

    def test_a_mac_block_of_another_session_refuses_a_run_that_names_only_the_mac(self) -> None:
        self.block("holder-session")
        status, errors = self.mac_attempt(remote(), machines=("mac",))
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertIn("mac failed: the Mac is blocked by fixture-holder: fixture work needs the Mac", errors)
        self.assertEqual((self.mac.copies, self.mac.commands), ([], []))
        self.assertEqual(self.line.closed,
                         [("mac", None, "the Mac is blocked by fixture-holder: fixture work needs the Mac")])
        row = self.only_row()
        self.assertEqual((row["status"], row["machine"], row["failed_machines"]), (None, "mac", ["mac"]))

    def test_a_mac_block_of_another_session_gives_the_run_back_to_natedev(self) -> None:
        self.block("holder-session")
        status, _ = self.mac_attempt(remote(), local_grant())
        self.assertEqual((status, self.output.getvalue()), (0, NATEDEV_RAN))
        self.assertEqual(self.mac.commands, [])
        self.assertEqual(self.line.given_back,
                         [("mac", "the Mac is blocked by fixture-holder: fixture work needs the Mac")])
        self.assertEqual(self.line.closed, [("natedev", 0, "")])
        self.assertEqual(self.only_row()["machine"], "natedev")

    def test_a_mac_block_of_this_session_does_not_hold_the_run(self) -> None:
        self.block("this-session")
        status, _ = self.mac_attempt(remote(), machines=("mac",))
        self.assertEqual((status, self.output.getvalue()), (0, b"mac ran\n"))

    def test_a_failed_mac_gives_the_run_back_to_natedev_and_the_row_names_it(self) -> None:
        self.mac.lines, self.mac.exit = (), 1
        status, errors = self.mac_attempt(remote(), local_grant())
        self.assertEqual(status, 0)
        self.assertEqual(self.mac.stops, [])
        self.assertEqual(self.line.given_back, [("mac", "the Mac did not start the program (status 1)")])
        self.assertEqual(self.line.closed, [("natedev", 0, "")])
        self.assertIn("the run goes back to the lines of the other machines", errors)
        row = self.only_row()
        self.assertEqual((row["machine"], row["status"], row["refusal"], row["failed_machines"]),
                         ("natedev", 0, None, ["mac"]))
        self.assertGreaterEqual(str(row["run_started_at"]), str(row["placed_at"]))

    def test_a_failed_mac_and_a_natedev_refusal_before_its_start_leave_no_start_time(self) -> None:
        def change_then_fail(command: list[str], remote: RemoteRun, ssh: ExitStack,
                             write: Callable[[str], None]) -> int:
            _ = (self.repo / "check.py").write_text('print("changed")\n')
            return FakeMac.stream(self.mac, command, remote, ssh, write)

        self.mac.lines, self.mac.exit = (), 1
        with mock.patch.object(self.mac, "stream", side_effect=change_then_fail):
            status, errors = self.mac_attempt(remote(), local_grant())
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertIn("check.py changed after the check", errors)
        self.assertEqual(self.line.closed, [("natedev", None, "a checked file changed after the check")])
        row = self.only_row()
        self.assertEqual((row["machine"], row["failed_machines"], row["run_started_at"]), ("natedev", ["mac"], None))

    def test_a_lost_mac_group_line_keeps_the_place_and_gives_no_run_back(self) -> None:
        self.mac.lines, self.mac.exit, self.mac.group = (), None, 0
        status, errors = self.mac_attempt(remote(), local_grant())
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertEqual(self.mac.stops, [0])
        self.assertIn(hand_made_mac.STILL_LIVE, errors)
        self.assertEqual((self.line.given_back, self.line.closed, self.line.place.started), ([], [], []))

    def test_a_lost_connection_before_the_start_line_gives_no_run_back(self) -> None:
        self.mac.lines, self.mac.exit = (), None
        status, errors = self.mac_attempt(remote(), local_grant())
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertIn("so the start is unknown (ssh status 255)", errors)
        self.assertEqual(self.mac.stops, [42])
        self.assertEqual((self.line.given_back, self.line.place.started), ([], []))
        self.assertEqual([machine for machine, _, _ in self.line.closed], ["mac"])
        self.assertEqual(self.output.getvalue(), b"")

    def test_a_mac_that_started_the_program_and_lost_ssh_gives_no_run_back(self) -> None:
        self.mac.exit = None
        status, errors = self.mac_attempt(remote(), local_grant())
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertIn("the Mac returned no exit status (ssh status 255)", errors)
        self.assertEqual((self.line.given_back, self.line.place.started), ([], []))
        self.assertEqual([machine for machine, _, _ in self.line.closed], ["mac"])


@final
class MacLinesTests(unittest.TestCase):
    def lines(self, written: tuple[str, ...], exited: bool) -> tuple[bytes, MacLines]:
        output = io.BytesIO()
        lines = MacLines(hand_made.OutputCopy(output, None))
        for line in written:
            lines.write(line)
        lines.end(exited)
        return output.getvalue(), lines

    def test_the_wrapper_exit_line_and_its_blank_line_stay_out_and_the_program_lines_stay_in(self) -> None:
        output, lines = self.lines(("copy note\n", START + "\n", "a\n", "\n", "b\n", "\n", "mac_exit=0\n"), True)
        self.assertEqual(output, b"copy note\na\n\nb\n")
        self.assertTrue(lines.started)

    def test_with_no_exit_status_each_line_stays_in(self) -> None:
        output, lines = self.lines((START + "\n", "a\n", "\n", "mac_exit=7\n", "b\n"), False)
        self.assertEqual(output, b"a\n\nmac_exit=7\nb\n")
        self.assertEqual(lines.refusal, "")

    def test_a_refusal_line_before_the_start_is_the_refusal(self) -> None:
        output, lines = self.lines((hand_made_mac.REFUSED + "x on the Mac is not the checked file\n",), True)
        self.assertEqual((output, lines.refusal, lines.started), (b"", "x on the Mac is not the checked file", False))


@final
@unittest.skipIf(ZSH is None, "the start script needs zsh")
class StartScriptTests(unittest.TestCase):
    """Run the Mac start script with zsh on natedev, with natedev tools in place of the Mac tools."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home = Path()
        self.repo = Path()
        self.system = ""

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.home / "natedev-repo"
        self.repo.mkdir()
        _ = (self.repo / "check.py").write_text(SCRIPT)
        (self.home / MIRROR).mkdir(parents=True)
        _ = shutil.copy(self.repo / "check.py", self.home / MIRROR / "check.py")
        sha256sum, env = shutil.which("sha256sum"), shutil.which("env")
        assert sha256sum is not None and env is not None
        od = shutil.which("od")
        assert od is not None
        self.system = str(Path(env).parent)
        (self.home / "user-bin").mkdir()
        _ = self.enterContext(mock.patch.object(hand_made_mac, "MAC_SHA256", (sha256sum,)))
        _ = self.enterContext(mock.patch.object(hand_made_mac, "MAC_SYSTEM_FOLDERS", (self.system,)))
        _ = self.enterContext(mock.patch.object(hand_made_mac, "MAC_PROGRAM_FOLDERS", (self.system, "~/user-bin")))
        _ = self.enterContext(mock.patch.object(hand_made_mac, "MAC_FIRST_BYTES", (od, "-An", "-N4", "-tx1")))
        _ = self.enterContext(mock.patch.object(hand_made_mac, "MAC_ZSH", str(ZSH)))

    def run_script(self, argv: tuple[str, ...], mirror: str = MIRROR) -> subprocess.CompletedProcess[str]:
        program = Program(argv, self.repo, None, checked(self.repo))
        script = hand_made_mac.start_script(mirror, program, PLACEMENT)
        environment = {"HOME": str(self.home), "PATH": os.environ["PATH"], "LANG": "C.UTF-8",
                       "RUSTFLAGS": "-Zcode", "PYTHONPATH": "/nonexistent"}
        return subprocess.run([str(ZSH), "-c", script], env=environment, cwd=self.home, capture_output=True,
                              text=True, check=False)

    def test_the_program_environment_holds_only_allowed_names(self) -> None:
        result = self.run_script(("env",))
        self.assertEqual(result.returncode, 0, result.stderr)
        first, *variables = result.stdout.splitlines()
        self.assertEqual(first, START)
        values = dict(line.split("=", 1) for line in variables)
        self.assertLessEqual(set(values), PROGRAM_VARIABLES | {"MAC_PLACEMENT"})
        # The shell's startup files may set LANG, so the test checks only the names the script sets.
        self.assertEqual((values["PATH"], values["MAC_PLACEMENT"], values["HOME"]),
                         (f"{self.system}:{self.home}/user-bin", "fixture-episode/attempt-mac", str(self.home)))

    def test_a_mac_file_that_differs_from_the_checked_file_is_refused_before_the_start(self) -> None:
        _ = (self.home / MIRROR / "check.py").write_text('print("other")\n')
        result = self.run_script(("env",))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, hand_made_mac.REFUSED + "check.py on the Mac is not the checked file\n")
        (self.home / MIRROR / "check.py").unlink()
        self.assertEqual(self.run_script(("env",)).stdout,
                         hand_made_mac.REFUSED + "check.py on the Mac is not the checked file\n")

    def test_a_missing_mac_folder_ends_the_script_with_no_start(self) -> None:
        result = self.run_script(("env",), mirror="mirror/missing")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_a_first_word_in_no_mac_program_folder_is_refused_before_the_start(self) -> None:
        result = self.run_script(("hand-made-no-such-tool", "check.py"))
        self.assertEqual(result.stdout, hand_made_mac.REFUSED + "hand-made-no-such-tool is in no Mac program folder\n")

    def test_a_word_that_names_a_mirror_file_that_natedev_did_not_read_is_refused_before_the_start(self) -> None:
        mirror = self.home / MIRROR
        (mirror / "target").mkdir()
        (mirror / "test").mkdir()
        for path in (mirror / "target" / "run.sh", mirror / "out.sh", self.home / "x.sh"):
            _ = path.write_text("echo unread\n")
        env = shutil.which("env")
        assert env is not None
        for word in ("target/run.sh", "out.sh", str(self.home / "x.sh")):
            with self.subTest(word=word):
                result = self.run_script(("echo", word))
                self.assertEqual((result.returncode, result.stdout),
                                 (1, hand_made_mac.REFUSED + f"{word} on the Mac is a file that natedev did not read\n"))
        for word in ("target/missing.sh", "60", "test", env):
            with self.subTest(word=word):
                self.assertEqual(self.run_script(("echo", word)).stdout, f"{START}\n{word}\n")

    def test_a_word_from_a_user_folder_starts_only_a_compiled_program(self) -> None:
        script = self.home / "user-bin" / "helper"
        _ = script.write_text("#!/bin/sh\necho unread\n")
        script.chmod(0o755)
        # A Mach-O 64 bit start; the natedev ELF programs are not compiled Mac programs.
        tool = self.home / "user-bin" / "tool"
        _ = tool.write_bytes(bytes.fromhex("cffaedfe") + bytes(12))
        tool.chmod(0o755)
        _ = shutil.copy(str(shutil.which("env")), self.home / "user-bin" / "elf")
        refusal = hand_made_mac.REFUSED + "helper on the Mac starts a program that is not compiled and not in a system folder\n"
        for argv in (("echo", "helper"), ("helper",), ("timeout", "60", "helper")):
            with self.subTest(argv=argv):
                result = self.run_script(argv)
                self.assertEqual((result.returncode, result.stdout), (1, refusal))
        self.assertEqual(self.run_script(("echo", "tool")).stdout, f"{START}\ntool\n")
        self.assertEqual(self.run_script(("echo", "elf")).stdout, hand_made_mac.REFUSED
                         + "elf on the Mac starts a program that is not compiled and not in a system folder\n")

    def test_a_later_word_that_names_a_text_file_with_no_execute_bit_in_a_user_folder_is_refused(self) -> None:
        # bash finds a script on PATH by read access alone, so whence, which needs the execute bit, is not the check.
        script = self.home / "user-bin" / "helper"
        _ = script.write_text("echo unread\n")
        script.chmod(0o644)
        result = self.run_script(("bash", "helper"))
        self.assertEqual((result.returncode, result.stdout), (1, hand_made_mac.REFUSED
                         + "helper on the Mac starts a program that is not compiled and not in a system folder\n"))

    def test_a_later_path_match_that_is_not_compiled_is_refused_before_the_start(self) -> None:
        script = self.home / "user-bin" / "env"
        _ = script.write_text("#!/bin/sh\necho unread\n")
        script.chmod(0o755)
        result = self.run_script(("timeout", "60", "env"))
        self.assertEqual((result.returncode, result.stdout), (1, hand_made_mac.REFUSED
                         + "env on the Mac starts a program that is not compiled and not in a system folder\n"))

    def test_a_word_that_starts_with_an_equals_sign_stays_one_word(self) -> None:
        self.assertEqual(self.run_script(("echo", "=env")).stdout, f"{START}\n=env\n")

    def test_a_later_word_that_names_a_mac_folder_with_a_main_file_is_refused_before_the_start(self) -> None:
        package = self.home / MIRROR / "tool"
        package.mkdir()
        refusal = (hand_made_mac.REFUSED
                   + "tool on the Mac is a folder that python can run: it has a __main__ file or cannot be listed\n")
        for name in ("__main__.py", "__main__.pyc"):
            with self.subTest(name=name):
                _ = (package / name).write_bytes(b"unread\n")
                result = self.run_script(("python3", "tool"))
                self.assertEqual((result.returncode, result.stdout), (1, refusal))
                (package / name).unlink()
        _ = (package / "main.py").write_text("print('read')\n")
        self.assertEqual(self.run_script(("echo", "tool")).stdout, f"{START}\ntool\n")
        package.chmod(0o300)
        self.addCleanup(package.chmod, 0o755)
        self.assertEqual(self.run_script(("echo", "tool")).stdout, refusal)

    def test_the_mac_stderr_joins_the_stdout_for_the_log(self) -> None:
        program = Program(("sh", "-c", "echo out; echo err >&2"), self.repo, None, checked(self.repo))
        work = hand_made_mac.mac_work(PLACEMENT, MirrorUse(MIRROR, lambda text: text), program)
        result = subprocess.run([str(ZSH), "-c", work], env={"HOME": str(self.home), "PATH": os.environ["PATH"]},
                                cwd=self.home, capture_output=True, text=True, check=False)
        self.assertEqual((result.stdout, result.stderr), (f"{START}\nout\nerr\n", ""))


@final
class CopiedCheckoutTests(unittest.TestCase):
    def test_the_copy_waits_for_the_floor_and_holds_its_lease_on_the_mac(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        repo = root / "repo"
        _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
        home = root / "home"
        shim = home / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        mirror = home / MIRROR
        lease = floor_claims.mirror_lock_path(mirror)
        group = lease.with_name(lease.name + build_folders.MacFloorLeaseFile.GROUP.value)
        receiver = root / "receiver.py"
        _ = receiver.write_text(
            "from pathlib import Path\n"
            + "import fcntl, sys\n"
            + "with Path(sys.argv[1]).open() as lock:\n"
            + "    try:\n"
            + "        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            + "    except BlockingIOError:\n"
            + "        Path(sys.argv[2]).touch()\n"
            + "    else:\n"
            + "        raise SystemExit('receiver has no shared lease')\n")
        started = root / "received"
        python = shlex.quote(sys.executable)
        write_shell(shim, f'if [ "$1" = -B ]; then exec {python} "$@"; fi\n'
                    + "exec " + shlex.join([sys.executable, str(receiver), str(group), str(started)]) + "\n")
        config = root / "config"
        # The mirror sits below the configured root, so the copy takes the floor lease of its folder.
        _ = write_config_fixture(config / "mac_test.conf",
                                 f"floor_sweep_timeout_s=5\nmirror_root=~/{Path(MIRROR).parent}\n")
        checkout = Path(__file__).resolve().parents[2]

        def rsync(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            command = next(word.removeprefix("--rsync-path=") for word in argv if word.startswith("--rsync-path="))
            with ExitStack() as floor:
                self.assertTrue(floor.enter_context(sweep.mac_floor_mirror_leases({mirror})))
                environment = {**os.environ, "HOME": str(home), "PYTHONPATH": str(checkout)}
                with subprocess.Popen(["/bin/sh", "-c", command], cwd=home, env=environment,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                    assert process.stderr is not None
                    stderr = cast(TextIO, process.stderr)
                    try:
                        self.assertTrue(select.select([stderr], [], [], 8)[0])
                        waiting = stderr.readline()
                        if "waiting for the Mac floor sweep" not in waiting:
                            _, errors = process.communicate(timeout=8)
                            self.fail("Expected the receiver to wait for the Mac floor sweep.\n" + waiting + errors)
                        self.assertFalse(started.exists())
                        floor.close()
                        output, errors = process.communicate(timeout=8)
                    finally:
                        if process.poll() is None:
                            process.kill()
                    return subprocess.CompletedProcess(argv, process.returncode, output, errors)

        settings = eligibility.SshConnectionSettings("mac-host", 7.0, 30.0, UNREACHABLE_WAIT_FIXTURE_SECONDS)
        for target, value in ((eligibility, {"repository_identity": eligibility.KnownRepository("repo"),
                                             "read_connection_settings": settings}),
                              (offload, {"remote_mirror": MIRROR, "holder_session": "fixture-session"}),
                              (copies, {"source_owner": "fixture-owner", "remote_lease": nullcontext(),
                                        "register_remote": None})):
            for name, result in value.items():
                _ = self.enterContext(mock.patch.object(target, name, return_value=result))
        with (mock.patch.dict(os.environ, config_environment(config)),
              mock.patch.object(subprocess, "run", side_effect=rsync),
              hand_made_mac.copied_checkout(repo, "mac-host") as copied):
            self.assertTrue(started.is_file())
            self.assertIn("run_after_mac_floor_group", copied.use("pwd"))

    def test_the_copy_leaves_out_git_ignored_files_and_uses_the_offload_ssh_options(self) -> None:
        repo = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve() / "repo"
        repo.mkdir()
        _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
        _ = (repo / ".gitignore").write_text("secret.sh\n")
        _ = (repo / "secret.sh").write_text("echo natedev only\n")
        settings = eligibility.SshConnectionSettings("mac-host", 7.0, 30.0, UNREACHABLE_WAIT_FIXTURE_SECONDS)
        copied: list[tuple[list[str], str]] = []

        def rsync(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            excludes = next(word.removeprefix("--exclude-from=") for word in argv if word.startswith("--exclude-from="))
            copied.append((argv, Path(excludes).read_text()))
            return subprocess.CompletedProcess(argv, 0, "", "")

        for target, value in ((eligibility, {"repository_identity": eligibility.KnownRepository("repo"),
                                             "read_connection_settings": settings}),
                              (offload, {"remote_mirror": MIRROR, "holder_session": "fixture-session"}),
                              (copies, {"source_owner": "fixture-owner", "remote_lease": nullcontext(),
                                        "register_remote": None})):
            for name, result in value.items():
                _ = self.enterContext(mock.patch.object(target, name, return_value=result))
        with mock.patch.object(subprocess, "run", side_effect=rsync), \
                hand_made_mac.copied_checkout(repo, "mac-host") as mirror:
            self.assertEqual(mirror.path, MIRROR)
        (argv, excludes), = copied
        self.assertEqual(argv[argv.index("-e") + 1], shlex.join(["ssh", *offload.ssh_options(settings)]))
        self.assertIn("ConnectTimeout=7", argv[argv.index("-e") + 1])
        self.assertIn("/secret.sh\n", excludes.splitlines(keepends=True))


@final
class MacRefusalTests(unittest.TestCase):
    def test_the_mac_refuses_words_that_it_cannot_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve() / "repo"
            repo.mkdir()
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _ = (repo / "run.sh").write_text("#!/bin/sh\n")
            outside = Path(temporary).resolve() / "tool.sh"
            _ = outside.write_text("#!/bin/sh\n")
            cases = {
                ("cargo", "test"): "",
                ("/usr/bin/make",): "",
                ("./run.sh",): "",
                ("./other.sh",): "the first word ./other.sh is not a checked file",
                ("/opt/run",): "the first word /opt/run is in no Mac program folder",
                ("sh", str(repo / "run.sh")):
                    f"the word {repo / 'run.sh'} names the natedev checkout, which the Mac does not check",
                ("A=1",): "the first word A=1 cannot start on the Mac",
                ("link/../run.sh", "run.sh"): "the word link/../run.sh has a .. part, and the Mac gets each link of "
                                              + "the checkout as a link",
                ("sh", "run.sh", "link/../run.sh"): "the word link/../run.sh has a .. part, and the Mac gets each "
                                                    + "link of the checkout as a link",
                ("sh", "run.sh", "--out=../y"): "the word --out=../y has a .. part, and the Mac gets each link of "
                                                + "the checkout as a link",
                ("sh", "run.sh", "a..b", "x...sh"): "",
            }
            for argv, reason in cases.items():
                with self.subTest(argv=argv):
                    self.assertEqual(hand_made_mac.mac_refusal(Program(argv, repo, None, checked(repo, "run.sh"))),
                                     reason)
            program = Program(("sh", "tool.sh"), repo, None, ((outside, "0" * 64),))
            self.assertIn("is a checked file outside the checkout", hand_made_mac.mac_refusal(program))
            (repo / "tools").mkdir()
            (repo / "tools" / "x.sh").symlink_to(outside)
            program = Program(("bash", "tools/x.sh"), repo, None, checked(repo, "tools/x.sh"))
            self.assertEqual(hand_made_mac.mac_refusal(program), f"{repo / 'tools' / 'x.sh'} is a checked file "
                             + "outside the checkout or a path with a link in it, and the Mac gets only the "
                     + "checkout, with each link as a link")

    def test_a_checked_file_that_the_copy_leaves_out_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve() / "repo"
            for folder in ("target", "out", "keep"):
                (repo / folder).mkdir(parents=True)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _ = (repo / ".gitignore").write_text("out/\ngen.sh\n")
            for name in ("target/gen.sh", "out/x.sh", "gen.sh", "keep/gen.sh.txt", "targets.sh"):
                _ = (repo / name).write_text("#!/bin/sh\n")
                left_out = name in ("target/gen.sh", "out/x.sh", "gen.sh")
                with self.subTest(name=name):
                    self.assertEqual(hand_made_mac.mac_refusal(Program(("sh", name), repo, None, checked(repo, name))),
                                     f"{name} is a checked file that the copy to the Mac leaves out" if left_out else "")


if __name__ == "__main__":
    _ = unittest.main()
