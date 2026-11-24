"""Test process identity reads for admission clients."""
from __future__ import annotations

from dataclasses import replace
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
    def test_a_caller_with_no_name_is_not_a_terminal(self) -> None:
        caller = records.queue_caller("", "", "", "")
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
    """A process for the fake proc root. STATE in an argument or the cwd is the showrunner state root.

    The first process takes the cgroup of the fixture call; another process has a cgroup only when it names one."""
    argv: tuple[str, ...]
    tty: int = 0
    cwd: str = ""
    cgroup: str = ""


FIXTURE_WORKTREE = "/work/queue-fixture"


def fixture_owner(family: tuple[FakeProcess, ...], cgroup: str, members: tuple[staff.StaffRow, ...] = (),
                  environment: dict[str, str] | None = None,
                  folder_owner: str = "") -> records.QueueCaller | records.OwnerNotGiven:
    """Write the family to a fake proc root, this process first, and return the request owner."""
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
            if process_cgroup := (cgroup if index == 0 else process.cgroup):
                _ = (directory / "cgroup").write_text(f"0::{process_cgroup}\n")
        with (patch.dict(os.environ, {**NO_CALLER, **(environment or {}), "SHOWRUNNER_STATE_DIR": str(state)}),
              patch.object(records, "PROC_ROOT", proc),
              patch.object(records, "folder_owner", return_value=folder_owner),
              patch.object(records, "queue_staff", return_value=staff.StaffSnapshot(members))):
            return records.request_owner(FIXTURE_WORKTREE)


def fixture_identity(family: tuple[FakeProcess, ...], cgroup: str, members: tuple[staff.StaffRow, ...] = (),
                     environment: dict[str, str] | None = None, folder_owner: str = "") -> records.WaiterIdentity:
    """Return the stored request parts for a fake caller family."""
    return records.waiter_identity("check", FIXTURE_WORKTREE,
                                   fixture_owner(family, cgroup, members, environment, folder_owner))


class FamilyOwnerTests(unittest.TestCase):
    """A caller with no session and no staff reference gets its owner from its process family."""

    def shown_name(self, family: tuple[FakeProcess, ...], cgroup: str,
                   members: tuple[staff.StaffRow, ...] = (), environment: dict[str, str] | None = None) -> str:
        return fixture_identity(family, cgroup, members, environment)["session_name"]

    def install_family(self, script: str = f"STATE/{SESSION}/session/merge6/install_rest.sh",
                       cwd: str = "") -> tuple[FakeProcess, ...]:
        return (FakeProcess(("python3", "scripts/mac_test/mac_queue_run.py", "--what", "run 6 installs")),
                FakeProcess(("zsh", "-l", script), cwd=cwd),
                FakeProcess(("/run/current-system/systemd/lib/systemd/systemd", "--user")))

    def test_a_script_under_the_showrunner_state_names_its_staff_member(self) -> None:
        self.assertEqual(self.shown_name(self.install_family(), SERVICE, (CARGO_LINER,)), "cargo-liner")

    def test_an_unknown_state_session_uses_its_short_id(self) -> None:
        self.assertEqual(self.shown_name(self.install_family(), SERVICE), "4dbdb78e")

    def test_a_relative_script_resolves_against_the_process_folder(self) -> None:
        family = self.install_family("session/merge6/install_rest.sh", cwd=f"STATE/{SESSION}")
        self.assertEqual(self.shown_name(family, SERVICE, (CARGO_LINER,)), "cargo-liner")

    def test_an_option_value_is_not_the_script(self) -> None:
        for argv, cgroup, shown in (
            (("zsh", "-o", "pipefail", f"STATE/{SESSION}/session/install_rest.sh"), SCOPE, "cargo-liner"),
            (("python3", "-X", "dev", "/opt/outer.py"), SERVICE, "systemd · cargo-liner-install7.service"),
        ):
            with self.subTest(argv=argv):
                family = (FakeProcess(("python3", "-m", "scripts.mac_test.mac_queue_run")), FakeProcess(argv))
                self.assertEqual(self.shown_name(family, cgroup, (CARGO_LINER,)), shown)

    def test_a_family_with_a_tty_and_no_agent_is_the_terminal(self) -> None:
        for cgroup in (SCOPE, SERVICE):
            with self.subTest(cgroup=cgroup):
                family = (FakeProcess(("python3", "scripts/mac_test/mac_queue_run.py")), FakeProcess(("zsh",), 34869))
                self.assertEqual(self.shown_name(family, cgroup), "terminal")

    def test_an_empty_proc_root_keeps_the_terminal_name(self) -> None:
        self.assertEqual(self.shown_name((), SCOPE), "terminal")

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

    def test_an_agent_family_with_no_session_names_the_outermost_script(self) -> None:
        agent = FakeProcess(("/home/user/.local/bin/claude", "--resume", "x"), 34869)
        for family, shown in (
            ((FakeProcess(("python3", "scripts/mac_test/mac_queue_run.py")), FakeProcess(("zsh", "-c", "run ./x.sh")),
              FakeProcess(("bash", "/opt/outer.sh")), agent), "script · outer.sh"),
            ((FakeProcess(("python3", "-m", "scripts.mac_test.mac_queue_run")), FakeProcess(("zsh",), 34869), agent),
             "script · python3"),
        ):
            with self.subTest(shown=shown):
                self.assertEqual(self.shown_name(family, SCOPE), shown)

    def test_a_desktop_scope_with_no_agent_is_the_user(self) -> None:
        # Zed on the desktop starts rust-analyzer through queue_place.py, with no tty and no owner variable.
        # The desktop moves each app to an app-*.scope, and systemd --user in init.scope is its parent.
        user_manager = FakeProcess(("/run/current-system/systemd/lib/systemd/systemd", "--user"),
                                   cgroup="/user.slice/user-1000.slice/user@1000.service/init.scope")
        for cgroup in ("/user.slice/user-1000.slice/user@1000.service/app.slice/app-dev.zed.Zed-4242.scope",
                       "/user.slice/user-1000.slice/session-3.scope"):
            with self.subTest(cgroup=cgroup):
                family = (FakeProcess(("python3", "/home/user/.claude/scripts/build_queue/queue_place.py", "rust-analyzer")),
                          FakeProcess(("/run/current-system/sw/bin/zeditor",), cgroup=cgroup), user_manager)
                self.assertEqual(self.shown_name(family, cgroup), "terminal")

    def test_a_scope_that_a_service_starts_is_refused(self) -> None:
        # A timer service runs `systemd-run --user --scope`: the scope is not the user's, and the service is its parent.
        scope = "/user.slice/user-1000.slice/user@1000.service/app.slice/run-r5d1.scope"
        timer = FakeProcess(("bash", "/opt/nightly.sh"),
                            cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/nightly.service")
        family = (FakeProcess(("python3", "scripts/build_queue/queue_place.py", "cargo")), timer)
        self.assertEqual(fixture_owner(family, scope), records.OwnerNotGiven("systemd · nightly.service"))
        # A scope that is not a desktop app or a login session, with no service and no tty, does not say who started it.
        self.assertEqual(fixture_owner(family[:1], scope), records.OwnerNotGiven("script · queue_place.py"))

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


IMPL = staff.StaffRow(staff.StaffId("impl-id"), "showrunner", "impl", staff.StaffRoot(),
                      current_session=staff.StaffId("impl-session"))
UNIT = staff.StaffRow(staff.StaffId("unit-id"), "unit", "navigation", staff.ReportsTo(IMPL.id),
                      current_session=staff.StaffId("unit-session"))
SERVICE_FAMILY = (FakeProcess(("python3", "scripts/build_queue/admission_client.py")),
                  FakeProcess(("zsh", "-l", "/opt/install.sh")))


class RequestOwnerRowTests(unittest.TestCase):
    """Each form that says who started a request gives the row text `<owner>: <what>`."""

    def row(self, family: tuple[FakeProcess, ...] = SERVICE_FAMILY, environment: dict[str, str] | None = None,
            folder_owner: str = "") -> str:
        from scripts.build_queue.queue_lines import step_text
        from scripts.lib.build_steps import StepNames

        identity = fixture_identity(family, SERVICE, (IMPL, UNIT, CARGO_LINER), environment, folder_owner)
        step = records.WaitingStep(1, identity["session_id"], identity["session_name"], identity["step"],
                                   identity["folder"], 0, 1, records.ReadyForAdmission(), unit=identity.get("unit", ""))
        return step_text(step, StepNames({"names.check": "type check"}))

    def test_a_session_names_its_staff_member(self) -> None:
        self.assertEqual(self.row(environment={"CLAUDE_CODE_SESSION_ID": "impl-session"}), "impl: type check")

    def test_a_unit_session_names_its_unit(self) -> None:
        self.assertEqual(self.row(environment={"CODEX_THREAD_ID": "unit-session"}), "navigation: type check")

    def test_a_staff_reference_names_its_staff_member(self) -> None:
        self.assertEqual(self.row(environment={"BUILDLOG_OWNER_STAFF_ID": "impl-id"}), "impl: type check")

    def test_a_session_name_names_the_caller(self) -> None:
        self.assertEqual(self.row(environment={"CLAUDE_SESSION_NAME": "nightly"}), "nightly: type check")

    def test_a_script_under_the_state_root_names_its_staff_member(self) -> None:
        family = (FakeProcess(("python3", "scripts/build_queue/admission_client.py")),
                  FakeProcess(("zsh", "-l", f"STATE/{SESSION}/session/merge6/install_rest.sh")))
        self.assertEqual(self.row(family), "cargo-liner: type check")

    def test_a_terminal_names_the_user(self) -> None:
        family = (FakeProcess(("python3", "scripts/build_queue/admission_client.py")), FakeProcess(("zsh",), 34869))
        self.assertEqual(self.row(family), "terminal: type check")

    def test_the_worktree_owner_names_a_caller_with_no_other_owner(self) -> None:
        self.assertEqual(self.row(folder_owner="unit-id"), "navigation: type check")
        self.assertEqual(self.row(folder_owner="verify"), "verify: type check")


class FolderOwnerTests(unittest.TestCase):
    def test_active_gate_then_copy_record_then_staff_supplies_the_owner(self) -> None:
        from scripts.agents.remote import local_machine
        from scripts.copies import copies
        from scripts.lib import copy_record, scratch

        tree = Path(FIXTURE_WORKTREE).resolve()
        member = replace(UNIT, worktree=staff.PresentWorktree(local_machine(), tree))
        registration = copy_record.Registered("run:fixture", {"owner_unit": "copy_unit"})
        with (patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((member,))),
              patch.object(scratch, "task_of", return_value=registration) as task_of):
            token = copies.ACTIVE_GATES.set((copies.ActiveGate(tree, tree, "gate_unit"),))
            try:
                self.assertEqual(records.folder_owner(tree), "gate_unit")
                self.assertEqual(copies.recorded_owner(tree), "gate_unit")
                task_of.assert_not_called()
            finally:
                copies.ACTIVE_GATES.reset(token)
            self.assertEqual(records.folder_owner(tree), "copy_unit")
            self.assertEqual(copies.recorded_owner(tree), "copy_unit")
            task_of.return_value = copy_record.NotRegistered()
            self.assertEqual(records.folder_owner(tree), str(member.id))
            self.assertEqual(copies.recorded_owner(tree), str(member.id))
            task_of.return_value = copy_record.Registered("run:fixture", {"owner_unit": "invalid owner"})
            self.assertEqual(records.folder_owner(tree), "")
            with self.assertRaisesRegex(copies.CopyError, "owner must match"):
                _ = copies.recorded_owner(tree)

    def test_a_queue_request_reads_the_copy_record_without_recovery_and_source_owner_recovers(self) -> None:
        from scripts.copies import copies
        from scripts.lib import scratch

        intents: list[scratch.RemovalIntentAccess] = []

        def task_of(_source: Path, *, intent: scratch.RemovalIntentAccess) -> None:
            intents.append(intent)

        with patch.object(scratch, "task_of", side_effect=task_of), \
                patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())):
            _ = records.folder_owner(Path("/work/queue-fixture"))
            _ = copies.recorded_owner(Path("/work/queue-fixture"))
        self.assertEqual(intents, [scratch.RemovalIntentAccess.READ_ONLY, scratch.RemovalIntentAccess.RECOVER])


class DefaultOwnerTests(unittest.TestCase):
    """Gates and fixtures name an owner only when the caller names none."""

    def test_a_default_owner_names_a_staff_member_or_a_session_name_and_then_leaves(self) -> None:
        # The default owner never goes in BUILDLOG_OWNER_STAFF_ID: a hold sends its restart notice to that member.
        for reference, expected in (
            ("impl-id", {"CLAUDE_SESSION_NAME": "impl"}),
            ("unit-id", {"CLAUDE_SESSION_NAME": "navigation", records.BUILDLOG_UNIT: "navigation"}),
            ("nightly", {"CLAUDE_SESSION_NAME": "nightly"}),
        ):
            with self.subTest(reference=reference), patch.dict(os.environ, NO_CALLER), \
                    patch.object(records, "queue_staff", return_value=staff.StaffSnapshot((IMPL, UNIT))):
                with records.default_owner(reference):
                    self.assertEqual({name: os.environ[name] for name in (*records.OWNER_VARIABLES, records.BUILDLOG_UNIT)
                                      if os.environ.get(name)}, expected)
                self.assertFalse(records.owner_given(os.environ))
                self.assertEqual(os.environ[records.BUILDLOG_UNIT], "")

    def test_a_given_owner_stays(self) -> None:
        with patch.dict(os.environ, {**NO_CALLER, "CLAUDE_CODE_SESSION_ID": "impl-session"}):
            with records.default_owner("nightly"):
                self.assertEqual(os.environ["CLAUDE_SESSION_NAME"], "")
        self.assertEqual(records.named_owner({"BUILDLOG_OWNER_STAFF_ID": "impl-id"}, "gate module"), {})
        self.assertEqual(records.named_owner({"CLAUDE_SESSION_NAME": ""}, "gate module"),
                         {"CLAUDE_SESSION_NAME": "gate module"})
