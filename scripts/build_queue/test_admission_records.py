"""Test process identity reads for admission clients."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import NamedTuple
import unittest
from unittest.mock import patch

from scripts.build_queue import admission_records as records
from scripts.production import staff


class ProcessStartTimeTests(unittest.TestCase):
    def test_process_stat_with_invalid_name_keeps_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            directory = proc / "123"
            directory.mkdir()
            _ = (directory / "stat").write_bytes(b"123 (verify\xff) S " + b"0 " * 18 + b"4200\n")

            with patch.object(records, "Path", return_value=proc):
                self.assertEqual(records.process_start_time(123), "4200")


class QueueCallerTests(unittest.TestCase):
    def test_an_unknown_worktree_does_not_get_a_terminal_prefix(self) -> None:
        caller = records.queue_caller("", "", "", "", "/private/unregistered session")
        self.assertEqual(caller.name, "unregistered session")


SESSION = "4dbdb78e-a83b-4248-b8f9-ff278dcbc60c"
SERVICE = "/user.slice/user-1000.slice/user@1000.service/app.slice/cargo-liner-install7.service"
RUN_SERVICE = "/user.slice/user-1000.slice/user@1000.service/app.slice/run-p1-i2.service"
SCOPE = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-agents.slice/run-p1-i2.scope"
NO_CALLER = {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_NAME": "",
             "CLAUDE_SESSION_FOLDER": "", "BUILDLOG_OWNER_STAFF_ID": "", "BUILDLOG_UNIT": ""}
CARGO_LINER = staff.StaffRow(staff.StaffId(SESSION), "showrunner", "cargo-liner", staff.StaffRoot(),
                             current_session=staff.StaffId("cargo-liner-session"))


class FakeProcess(NamedTuple):
    """A process for the fake proc root. STATE in an argument or the cwd is the showrunner state root."""
    argv: tuple[str, ...]
    tty: int = 0
    cwd: str = ""


class FamilyOwnerTests(unittest.TestCase):
    """A caller with no session and no staff reference gets its owner from its process family."""

    def shown_name(self, family: tuple[FakeProcess, ...], cgroup: str,
                   members: tuple[staff.StaffRow, ...] = (), environment: dict[str, str] | None = None) -> str:
        """Write the family to a fake proc root, this process first, and return the shown queue name."""
        with tempfile.TemporaryDirectory() as temporary:
            proc, state = Path(temporary) / "proc", Path(temporary) / "showrunner"
            proc.mkdir()
            pids = [os.getpid(), *range(9001, 9000 + len(family))]
            for index, process in enumerate(family):
                directory = proc / str(pids[index])
                directory.mkdir()
                parent = pids[index + 1] if index + 1 < len(pids) else 0
                _ = (directory / "stat").write_text(f"{pids[index]} (p) S {parent} 0 0 {process.tty} " + "0 " * 15 + "\n")
                _ = (directory / "cmdline").write_text(
                    "".join(f"{part.replace('STATE', str(state))}\0" for part in process.argv))
                if process.cwd:
                    (directory / "cwd").symlink_to(process.cwd.replace("STATE", str(state)))
            if family:
                _ = (proc / str(os.getpid()) / "cgroup").write_text(f"0::{cgroup}\n")
            with (patch.dict(os.environ, {**NO_CALLER, **(environment or {}), "SHOWRUNNER_STATE_DIR": str(state)}),
                  patch.object(records, "PROC_ROOT", proc),
                  patch.object(records, "queue_staff", return_value=staff.StaffSnapshot(members))):
                return records.waiter_identity("install", "/work/queue-fixture")["session_name"]

    def install_family(self, script: str = f"STATE/{SESSION}/session/merge6/install_rest.sh",
                       cwd: str = "") -> tuple[FakeProcess, ...]:
        return (FakeProcess(("python3", "scripts/mac_test/mac_queue_run.py", "--what", "run 6 installs")),
                FakeProcess(("zsh", "-l", script), cwd=cwd),
                FakeProcess(("/run/current-system/systemd/lib/systemd/systemd", "--user")))

    def test_a_script_under_the_showrunner_state_names_its_staff_member(self) -> None:
        self.assertEqual(self.shown_name(self.install_family(), SERVICE, (CARGO_LINER,)),
                         "cargo-liner · install_rest.sh")

    def test_an_unknown_state_session_uses_its_short_id(self) -> None:
        self.assertEqual(self.shown_name(self.install_family(), SERVICE), "4dbdb78e · install_rest.sh")

    def test_a_relative_script_resolves_against_the_process_folder(self) -> None:
        family = self.install_family("session/merge6/install_rest.sh", cwd=f"STATE/{SESSION}")
        self.assertEqual(self.shown_name(family, SERVICE, (CARGO_LINER,)), "cargo-liner · install_rest.sh")

    def test_an_option_value_is_not_the_script(self) -> None:
        for argv, cgroup, shown in (
            (("zsh", "-o", "pipefail", f"STATE/{SESSION}/session/install_rest.sh"), SCOPE, "cargo-liner · install_rest.sh"),
            (("python3", "-X", "dev", "/opt/outer.py"), SCOPE, "script · outer.py"),
        ):
            with self.subTest(argv=argv):
                family = (FakeProcess(("python3", "-m", "scripts.mac_test.mac_queue_run")), FakeProcess(argv))
                self.assertEqual(self.shown_name(family, cgroup, (CARGO_LINER,)), shown)

    def test_a_family_with_a_tty_and_no_agent_is_the_terminal(self) -> None:
        for cgroup in (SCOPE, SERVICE):
            with self.subTest(cgroup=cgroup):
                family = (FakeProcess(("python3", "scripts/mac_test/mac_queue_run.py")), FakeProcess(("zsh",), 34869))
                self.assertEqual(self.shown_name(family, cgroup), "terminal · queue-fixture")

    def test_an_empty_proc_root_keeps_the_terminal_name(self) -> None:
        self.assertEqual(self.shown_name((), SCOPE), "terminal · queue-fixture")

    def test_a_service_unit_names_systemd(self) -> None:
        family = (FakeProcess(("python3", "scripts/mac_test/mac_queue_run.py")), FakeProcess(("zsh", "-l", "/opt/install.sh")))
        self.assertEqual(self.shown_name(family, SERVICE), "systemd · cargo-liner-install7.service")

    def test_a_generated_run_unit_names_its_script_or_folder(self) -> None:
        for family, shown in (
            ((FakeProcess(("python3", "-m", "unittest")), FakeProcess(("bash", "/opt/outer.sh"))), "systemd · outer.sh"),
            ((FakeProcess(("python3", "-m", "unittest"), cwd="/work/queue-fixture"),), "systemd · queue-fixture"),
        ):
            with self.subTest(shown=shown):
                self.assertEqual(self.shown_name(family, RUN_SERVICE), shown)

    def test_other_callers_name_the_outermost_script(self) -> None:
        for family, shown in (
            ((FakeProcess(("python3", "scripts/mac_test/mac_queue_run.py")), FakeProcess(("zsh", "-c", "run ./x.sh")),
              FakeProcess(("bash", "/opt/outer.sh"))), "script · outer.sh"),
            ((FakeProcess(("python3", "-m", "scripts.mac_test.mac_queue_run")), FakeProcess(("zsh",), 34869),
              FakeProcess(("/home/user/.local/bin/claude", "--resume", "x"), 34869)), "script · python3"),
        ):
            with self.subTest(shown=shown):
                self.assertEqual(self.shown_name(family, SCOPE), shown)

    def test_the_family_read_stops_at_the_agent(self) -> None:
        family = (FakeProcess(("python3", "scripts/mac_test/mac_queue_run.py")), FakeProcess(("zsh", "-c", "run it")),
                  FakeProcess(("/home/user/.local/bin/claude", "--resume", "x"), 34869),
                  FakeProcess(("python3", "/home/user/.claude/scripts/lib/relaunch.py", "run"), 34869))
        self.assertEqual(self.shown_name(family, SCOPE), "script · mac_queue_run.py")

    def test_a_session_or_staff_reference_wins(self) -> None:
        member = staff.StaffRow(staff.StaffId("impl-id"), "showrunner", "impl", staff.StaffRoot(),
                                current_session=staff.StaffId("impl-session"))
        for environment in ({"BUILDLOG_OWNER_STAFF_ID": "impl-id"}, {"CLAUDE_CODE_SESSION_ID": "impl-session"}):
            with self.subTest(environment=environment):
                self.assertEqual(self.shown_name(self.install_family(), SERVICE, (member,), environment), "impl")
