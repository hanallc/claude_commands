"""Run hand made attempts on the Mac Linux guest route with an injected guest start, run and end."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager, redirect_stderr
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import cast, final, override
import unittest
from unittest import mock

from scripts.build_queue import mac_run
from scripts.build_queue.placement import MAC_LINUX_NO_ANSWER
from scripts.build_targets import mac_linux, target_config
from scripts.lib import models_registry, nix_env
from scripts.mac_test import eligibility
from scripts.lint.memory_test_support import write_config_fixture
from scripts.notify import review
from scripts.production import hand_made, hand_made_guest
from scripts.production.hand_made_route import Machine
from scripts.production.test_hand_made import (REGISTRY_TEXT, SCRIPT, YES, FakeLine, envelope, local_grant, no_envrc,
                                               remote, same_model)

REPOSITORY = Path(__file__).resolve().parents[2]
SNAPSHOT = mac_linux.checkout_source
ORIGIN = "https://example.test/claude_commands.git"
COMMAND = ("python3", "check.py")
# The injected stream: it reads the request from the bootstrap on its input, and prints the lock line when it
# has one. Then it prints the setup error, and the refused line when it has one, and exits with the status. Or it
# prints the start line and the request and exits with the status.
GUEST_RUN = """import json, sys, tarfile
status, start, locked, setup_error, refused = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
    for member in archive:
        if member.name == "request.json":
            request = json.load(archive.extractfile(member))
if locked:
    print(locked, request["attempt"], file=sys.stderr, flush=True)
if setup_error:
    print(setup_error, file=sys.stderr, flush=True)
    if refused:
        print(refused, request["attempt"], flush=True)
    sys.exit(status)
print(start, request["attempt"], flush=True)
print("guest ran", json.dumps(request), flush=True)
sys.exit(status)
"""


@dataclass
class InjectedGuest:
    """The guest start, run and end of mac_linux, with no SSH. The snapshot of the checkout is real."""
    status: int = 0
    locked: bool = True
    arm_error: str = ""
    setup_error: str = ""
    refused: bool = False
    stream_program: str = sys.executable
    before_snapshot: Callable[[], None] = lambda: None
    proofs: list[mac_linux.GuestEndProven | mac_linux.GuestEndPending] = field(
        default_factory=lambda: [mac_linux.GuestEndProven()])
    events: list[str] = field(default_factory=list[str])

    def source(self, repo: Path) -> mac_linux.CheckoutSource:
        self.events.append("snapshot")
        self.before_snapshot()
        return SNAPSHOT(repo)

    def bundle(self, _repo: Path, _settings: mac_linux.GuestSettings) -> mac_linux.FullBundle:
        return mac_linux.FullBundle(mac_linux.BundleReason.FIRST_RUN)

    @contextmanager
    def refs(self, _repo: Path, _source: mac_linux.CheckoutSource, attempt: str) -> Generator[str]:
        self.events.append("refs")
        try:
            yield f"refs/mac-linux/{attempt}"
        finally:
            self.events.append("refs removed")

    def arm(self, _settings: mac_linux.GuestSettings, attempt: str) -> mac_linux.ArmedGuestAttempt:
        if self.arm_error:
            raise mac_linux.GuestUnavailable(self.arm_error)
        self.events.append("arm")
        return mac_linux.ArmedGuestAttempt(attempt, "0" * 32, "boot", 1, "fixture", "/fixture/checkout")

    def stream(self, _settings: mac_linux.GuestSettings, _source: mac_linux.CheckoutSource, _bootstrap_bytes: int,
               _armed: mac_linux.ArmedGuestAttempt, _prefix: str, _send: mac_linux.BundleSend, *,
               count: bool) -> list[str]:
        self.events.append(f"run count={count}")
        return [self.stream_program, "-c", GUEST_RUN, str(self.status), mac_linux.RUN_START,
                mac_linux.RUN_LOCKED if self.locked else "", self.setup_error,
                mac_linux.RUN_REFUSED if self.refused else ""]

    def prove(self, _settings: mac_linux.GuestSettings, armed: mac_linux.ArmedGuestAttempt, *,
              proof_deadline: float) -> mac_linux.GuestEndProven | mac_linux.GuestEndPending:
        del proof_deadline
        self.events.append(f"prove {armed.attempt == self.attempt()}")
        return self.proofs.pop(0) if len(self.proofs) > 1 else self.proofs[0]

    def attempt(self) -> str:
        return next(event.split()[1] for event in self.events if event.startswith("attempt "))

    @contextmanager
    def installed(self) -> Generator[None]:
        arm = self.arm

        def record_arm(settings: mac_linux.GuestSettings, attempt: str) -> mac_linux.ArmedGuestAttempt:
            self.events.append(f"attempt {attempt}")
            return arm(settings, attempt)

        with (mock.patch.object(mac_linux, "checkout_source", self.source),
              mock.patch.object(mac_linux, "guest_bundle", self.bundle),
              mock.patch.object(mac_linux, "bundle_refs", self.refs),
              mock.patch.object(mac_linux, "arm_guest", record_arm),
              mock.patch.object(mac_linux, "stream_arguments", self.stream),
              mock.patch.object(mac_linux, "prove_guest_end", self.prove)):
            yield


@final
class GuestRouteTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.repo = Path()
        self.config = Path()
        self.calls: list[list[str]] = []
        self.output = io.BytesIO()
        self.guest = InjectedGuest()
        self.line = FakeLine(Path())

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.config = self.root / "config"
        self.config.mkdir()
        self.switch("on")
        _ = (self.config / "mac_linux.conf").write_text(
            "ssh.hops=fixture-mac fixture-guest\nguest.user=fixture\ntest.jobs=2\nrun.time_limit_seconds=7\n")
        _ = write_config_fixture(self.config / "mac_test.conf", "unreachable_backoff_s=30\n")
        (self.root / "tmp").mkdir()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILDLOG_DIR": str(self.root / "buildlog"), "BUILDLOG_TARGET": "natedev", "BUILDLOG_UNIT": "aws-unit",
            "CONF_DIR": str(self.config), "TMPDIR": str(self.root / "tmp"),
            "BUILD_TARGETS_STATE_DIR": str(self.root / "target-state"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test"})))
        self.repo = self.checkout("repo", ORIGIN)
        _ = self.enterContext(mock.patch.object(review, "reviewer_environment", return_value=({}, "claude2")))
        _ = self.enterContext(mock.patch.object(
            review, "reviewer_binary", return_value=review.ResolvedReviewerBinary(self.root / "claude")))
        _ = self.enterContext(mock.patch.object(nix_env, "ensure", side_effect=no_envrc))
        _ = self.enterContext(mock.patch.object(models_registry, "claude_model", side_effect=same_model))
        self.calls = []
        self.output = io.BytesIO()
        self.guest = InjectedGuest()
        (self.root / "place").mkdir()
        # The queue gives the run to the guest; a later wait gives it to natedev.
        self.line = FakeLine(self.root / "place", answers=[remote(hand_made_guest.MACHINE), local_grant()])

    def checkout(self, name: str, origin: str) -> Path:
        """A clean checkout with check.py and an origin/main at its head."""
        repo = self.root / name
        repo.mkdir()
        _ = (repo / "check.py").write_text(SCRIPT)
        _ = (repo / ".gitignore").write_text("target/\n")
        for command in (["init", "-q", "--initial-branch=main"], ["add", "-A"], ["commit", "-qm", "fixture"],
                        ["remote", "add", "origin", origin], ["update-ref", "refs/remotes/origin/main", "HEAD"]):
            _ = self.git(repo, *command)
        return repo

    def git(self, repo: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True,
                              text=True).stdout.strip()

    def switch(self, value: str) -> None:
        _ = write_config_fixture(self.config / "build_targets.conf",
                                 f"enabled.mac-linux={value}\n{target_config.UNREACHABLE_WAIT_KEY}=20\n")

    def runner(self, argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        del timeout, env
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, envelope(YES), "")

    def attempt(self, argv: tuple[str, ...] = COMMAND, repo: Path | None = None,
                machines: tuple[str, ...] = (hand_made_guest.MACHINE,)) -> tuple[int, str]:
        request = hand_made.Request("trial of the guest route", machines, repo or self.repo, argv)
        services = hand_made.Services(registry_text=lambda: REGISTRY_TEXT, runner=self.runner, line=self.line,
                                      output=lambda: self.output)
        errors = io.StringIO()
        with redirect_stderr(errors), self.guest.installed():
            status = hand_made.attempt(request, services)
        return status, errors.getvalue()

    def rows(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for path in sorted((self.root / "buildlog").glob("*/*.jsonl"))
                for line in path.read_text().splitlines()]

    def only_row(self) -> dict[str, object]:
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def assert_refused(self, argv: tuple[str, ...], cause: str, *, checked: bool) -> None:
        """The attempt is a refusal with the cause, and the guest starts nothing."""
        status, errors = self.attempt(argv)
        self.assertEqual(status, hand_made.REFUSED_EXIT, errors)
        self.assertIn(cause, errors)
        self.assertEqual(bool(self.calls), checked)
        self.assertNotIn("arm", self.guest.events)
        self.assertEqual(self.rows()[-1]["status"], None)

    def assert_failed(self, status: int, errors: str, failure: str) -> None:
        """The guest failed before the start, and the attempt that names only the guest ends with no run."""
        self.assertEqual(status, hand_made.REFUSED_EXIT, errors)
        cause = f"mac-linux failed: {failure}"
        self.assertIn(cause, errors)
        self.assertEqual(self.line.closed, [("mac-linux", None, failure)])
        row = self.only_row()
        self.assertEqual((row["refusal"], row["status"], row["machine"], row["failed_machines"]),
                         (cause, None, "mac-linux", ["mac-linux"]))

    def test_the_guest_route_is_the_mac_linux_entry_of_the_route_table(self) -> None:
        self.assertEqual(hand_made.ROUTES["mac-linux"],
                         Machine(hand_made_guest.guest_route, hand_made_guest.guest_refusal))
        registry = (REPOSITORY / "config/build_targets.conf").read_text()
        self.assertIn(f"kind.{hand_made_guest.MACHINE}=ssh", registry)

    def test_a_guest_run_sends_the_argv_streams_the_output_and_records_the_guest(self) -> None:
        with mock.patch.dict(os.environ, {"CC": "fixture-cc"}):
            status, errors = self.attempt()
        self.assertEqual(status, 0, errors)
        output = self.output.getvalue().decode()
        self.assertTrue(output.startswith("guest ran "), output)
        request = cast(dict[str, object], json.loads(output.removeprefix("guest ran ")))
        self.assertEqual((request["argv"], request["work"], request["unit"], request["commit"], request["tree"]),
                         (list(COMMAND), "hand-made", "aws-unit", self.git(self.repo, "rev-parse", "HEAD"),
                          self.git(self.repo, "rev-parse", "HEAD^{tree}")))
        self.assertEqual(request["checked"], ["check.py"])
        attempt = self.guest.attempt()
        self.assertEqual(request["attempt"], attempt)
        self.assertEqual(self.guest.events, ["snapshot", "refs", f"attempt {attempt}", "arm", "run count=False",
                                             "prove True", "refs removed"])
        self.assertEqual((self.line.closed, self.line.given_back), ([("mac-linux", 0, "")], []))
        row = self.only_row()
        self.assertEqual((row["machine"], row["machines"], row["status"], row["refusal"], row["peak_mem_bytes"],
                          row["failed_machines"]), ("mac-linux", ["mac-linux"], 0, None, None, []))
        self.assertEqual((self.root / "place" / "output.log").read_text(), output)
        prompt = self.calls[0][-1]
        self.assertIn("Machines: mac-linux", prompt)
        # The guest gives the program its own variables, so the model does not see the natedev ones.
        self.assertNotIn("fixture-cc", prompt)

    def test_a_guest_that_is_off_is_refused_before_the_model_call(self) -> None:
        self.switch("off")
        status, errors = self.attempt()
        self.assertEqual(status, hand_made.REFUSED_EXIT, errors)
        cause = "mac-linux cannot take the run: mac-linux is off (build_targets.conf enabled.mac-linux=off)"
        self.assertIn(cause, errors)
        # The attempt ends at once: no model call and no queue line.
        self.assertEqual((self.calls, self.line.requests, self.guest.events), ([], [], []))
        row = self.only_row()
        self.assertEqual((row["refusal"], row["status"], row["machine"]), (cause, None, None))

    def test_a_guest_that_is_off_keeps_the_run_on_natedev(self) -> None:
        self.switch("off")
        self.line.answers = [local_grant()]
        status, errors = self.attempt(machines=("natedev", hand_made_guest.MACHINE))
        self.assertEqual(status, 0, errors)
        self.assertEqual([request.machines for request in self.line.requests], [("natedev",)])
        self.assertEqual(self.only_row()["machine"], "natedev")

    def test_a_guest_in_retry_backoff_takes_no_run_when_the_queue_places_it(self) -> None:
        # The backoff is no refusal: the queue line waits for the backoff end, and this placement is a fixture.
        with mock.patch.object(eligibility, "read_backoff", return_value=eligibility.ACTIVE_BACKOFF):
            status, errors = self.attempt()
        self.assert_failed(status, errors, "mac-linux: guest unreachable during retry backoff")
        self.assertEqual(self.guest.events, [])

    def test_a_refused_mac_claim_takes_no_run(self) -> None:
        with mock.patch.object(mac_run, "held", side_effect=mac_run.MacRunRefused("mac is busy with fixture")):
            status, errors = self.attempt()
        self.assert_failed(status, errors, "not started: mac is busy with fixture")
        self.assertEqual(self.guest.events, [])

    def test_a_failed_guest_start_takes_no_run_and_names_the_cause(self) -> None:
        self.guest.arm_error = "guest arm exited 255: fixture SSH refused the connection"
        status, errors = self.attempt()
        self.assert_failed(status, errors, "guest start: guest arm exited 255: fixture SSH refused the connection")
        self.assertEqual(self.guest.events, ["snapshot", "refs", f"attempt {self.guest.attempt()}", "refs removed"])

    def test_a_busy_guest_run_lock_takes_no_run_at_once_with_no_end_proof(self) -> None:
        self.guest.locked, self.guest.status = False, mac_linux.GUEST_NOT_LOCKED_STATUS
        self.guest.setup_error = "The guest run lock is held."
        status, errors = self.attempt()
        self.assert_failed(status, errors, "guest start: The guest run lock is held.")
        self.assertEqual(self.output.getvalue(), b"")
        self.assertEqual(self.guest.events[-2:], ["run count=False", "refs removed"])

    def test_a_guest_that_fails_before_the_start_gives_the_run_to_natedev(self) -> None:
        self.guest.locked, self.guest.status = False, mac_linux.GUEST_NOT_LOCKED_STATUS
        self.guest.setup_error = "The guest run lock is held."
        status, errors = self.attempt(machines=("natedev", hand_made_guest.MACHINE))
        self.assertEqual(status, 0, errors)
        self.assertEqual(self.output.getvalue(), b"hand made ran\n")
        self.assertIn("mac-linux failed: guest start: The guest run lock is held.; the run goes back to the lines "
                      + "of the other machines", errors)
        # The queue reads the guest no answer reason, so the guest waits before it takes a new run.
        self.assertEqual(self.line.given_back, [("mac-linux", MAC_LINUX_NO_ANSWER)])
        self.assertEqual(self.line.closed, [("natedev", 0, "")])
        row = self.only_row()
        self.assertEqual((row["machine"], row["machines"], row["status"], row["refusal"], row["failed_machines"]),
                         ("natedev", ["natedev", "mac-linux"], 0, None, ["mac-linux"]))

    def test_a_guest_refusal_before_the_model_call_keeps_the_run_on_natedev(self) -> None:
        self.line.answers = [local_grant()]
        word = str(self.repo / "check.py")
        status, errors = self.attempt(("python3", word), machines=("natedev", hand_made_guest.MACHINE))
        self.assertEqual(status, 0, errors)
        self.assertIn("mac-linux cannot take the run: mac-linux takes no absolute path in the command", errors)
        self.assertEqual([request.machines for request in self.line.requests], [("natedev",)])
        self.assertEqual(self.guest.events, [])
        self.assertEqual(self.only_row()["machine"], "natedev")

    def assert_start_unknown(self, status: int, errors: str, detail: str) -> None:
        """The guest got the command and the start is unknown: the attempt ends, and natedev does not run."""
        self.assertEqual(status, hand_made.REFUSED_EXIT, errors)
        cause = f"mac-linux start unknown: the guest got the command and gave no start line: {detail}"
        self.assertIn(cause, errors)
        self.assertEqual(self.output.getvalue(), b"")
        self.assertEqual((self.line.given_back, self.line.place.started), ([], []))
        self.assertEqual(self.line.closed, [("mac-linux", None, f"the guest run stopped: {cause}")])
        self.assertEqual(self.guest.events[-3:], ["run count=False", "prove True", "refs removed"])
        row = self.only_row()
        self.assertEqual((row["refusal"], row["status"], row["machine"], row["failed_machines"]),
                         (cause, None, "mac-linux", []))

    def test_a_stream_that_ends_after_the_lock_line_ends_the_attempt_with_the_start_unknown(self) -> None:
        # The status of a lock refusal after the lock line comes from a later step, so the start is unknown.
        self.guest.status = mac_linux.GUEST_NOT_LOCKED_STATUS
        self.guest.setup_error = "fixture bundle error"
        status, errors = self.attempt(machines=("natedev", hand_made_guest.MACHINE))
        self.assert_start_unknown(status, errors, "fixture bundle error")
        self.assertNotIn(mac_linux.RUN_LOCKED, errors)

    def test_a_connection_that_drops_after_the_command_is_sent_does_not_run_natedev(self) -> None:
        # The guest can print the start line just before the drop, so natedev does not run the program again.
        self.guest.status = 255
        self.guest.setup_error = "Connection to fixture-guest closed by remote host."
        status, errors = self.attempt(machines=("natedev", hand_made_guest.MACHINE))
        self.assert_start_unknown(status, errors, "Connection to fixture-guest closed by remote host.")

    def test_a_guest_that_refuses_the_command_word_ends_the_attempt_as_a_refusal(self) -> None:
        self.guest.status, self.guest.refused = 1, True
        self.guest.setup_error = "The guest refuses the command word target/run.sh: fixture"
        status, errors = self.attempt(machines=("natedev", hand_made_guest.MACHINE))
        self.assertEqual(status, hand_made.REFUSED_EXIT, errors)
        cause = "mac-linux refused the run: The guest refuses the command word target/run.sh: fixture"
        self.assertIn(cause, errors)
        self.assertNotIn(mac_linux.RUN_REFUSED, errors)
        # As on the Mac, the run does not go back to natedev.
        self.assertEqual((self.line.given_back, self.line.place.started, self.output.getvalue()), ([], [], b""))
        self.assertEqual(self.line.closed, [("mac-linux", None, f"the guest run stopped: {cause}")])
        self.assertEqual(self.guest.events[-3:], ["run count=False", "prove True", "refs removed"])
        row = self.only_row()
        self.assertEqual((row["refusal"], row["status"], row["machine"], row["failed_machines"]),
                         (cause, None, "mac-linux", []))

    def test_a_folder_word_that_the_check_refuses_at_the_run_start_takes_no_guest_run(self) -> None:
        (self.repo / "out").mkdir()
        answer = self.runner

        def ignore(argv: list[str], *, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            _ = (self.repo / ".gitignore").write_text("target/\n/out\n")
            return answer(argv, timeout=timeout, env=env)

        with mock.patch.object(self, "runner", ignore):
            status, errors = self.attempt(COMMAND + ("out",))
        self.assertEqual(status, hand_made.REFUSED_EXIT, errors)
        self.assertIn("a folder changed after the check: " + hand_made.FOLDER_REFUSAL.format(
            word="out", entry="the folder", folder=self.repo / "out", reason="git ignores"), errors)
        self.assertEqual((self.guest.events, self.line.given_back), ([], []))
        self.assertEqual(self.line.closed, [("mac-linux", None, "a checked file changed after the check")])

    def test_the_guest_end_is_proved_after_a_failed_run(self) -> None:
        self.guest.status = 3
        status, errors = self.attempt()
        self.assertEqual(status, 3, errors)
        self.assertEqual(self.guest.events[-2:], ["prove True", "refs removed"])
        self.assertEqual(self.line.closed, [("mac-linux", 3, "")])
        self.assertEqual((self.only_row()["status"], self.only_row()["refusal"]), (3, None))

    def test_the_guest_end_is_proved_after_a_start_failure_after_the_arm(self) -> None:
        self.guest.stream_program = str(self.root / "missing-ssh")
        status, errors = self.attempt()
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertIn("mac-linux failed: guest start: ", errors)
        self.assertIn("missing-ssh", errors)
        self.assertEqual(self.guest.events[-3:], ["run count=False", "prove True", "refs removed"])

    def unproven(self) -> tuple[int, str]:
        """Run an attempt with a guest that never proves the end, with a clock that the waits move."""
        self.guest.proofs = [mac_linux.GuestEndPending("fixture cleanup SSH refused the connection")]
        now = 0.0

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        with mock.patch.object(mac_linux, "monotonic", lambda: now), mock.patch.object(mac_linux, "sleep", advance):
            return self.attempt()

    def test_a_guest_end_with_no_proof_keeps_the_run_status_and_the_place(self) -> None:
        self.guest.status = 5
        status, errors = self.unproven()
        self.assertEqual(status, 5)
        self.assertIn("mac-linux: guest unreachable: fixture-guest did not confirm the run ended within 20 seconds "
                      + f"({target_config.UNREACHABLE_WAIT_KEY}); {hand_made_guest.STILL_LIVE}", errors)
        self.assertEqual(self.guest.events.count("prove True"), 3)
        self.assertEqual(self.guest.events[-1], "refs removed")
        # The queue keeps the place until this run ends.
        self.assertEqual((self.line.closed, self.line.given_back), ([], []))
        row = self.only_row()
        self.assertEqual((row["status"], row["refusal"]), (5, None))
        self.assertIsNotNone(row["log"])

    def test_a_start_failure_with_no_end_proof_names_both_causes_and_gives_no_run_back(self) -> None:
        self.guest.status, self.guest.setup_error = 1, "fixture bundle error"
        status, errors = self.unproven()
        self.assertEqual(status, hand_made.REFUSED_EXIT, errors)
        cause = "mac-linux start unknown: the guest got the command and gave no start line: fixture bundle error"
        self.assertIn("mac-linux: guest unreachable: fixture-guest did not confirm the run ended within 20 "
                      + f"seconds ({target_config.UNREACHABLE_WAIT_KEY}); the first error: {cause}; "
                      + hand_made_guest.STILL_LIVE, errors)
        self.assertEqual((self.line.closed, self.line.given_back), ([], []))
        self.assertEqual((self.only_row()["refusal"], self.only_row()["status"]), (cause, None))

    def test_a_file_that_changes_before_the_snapshot_is_refused_before_the_guest_starts(self) -> None:
        def change() -> None:
            _ = (self.repo / "check.py").write_text('print("changed")\n')

        self.guest.before_snapshot = change
        self.assert_refused(COMMAND, "mac-linux failed: guest start: the commit for the guest does not hold the "
                            + "checked text of check.py: the file changed after the check, or git does not keep it",
                            checked=True)
        self.assertEqual(self.guest.events, ["snapshot"])

    def test_a_program_file_that_git_ignores_is_refused_before_the_guest_starts(self) -> None:
        (self.repo / "target").mkdir()
        _ = (self.repo / "target/tool.py").write_text(SCRIPT)
        self.assert_refused(("python3", "target/tool.py"), "does not hold the checked text of target/tool.py",
                            checked=True)

    def test_a_program_file_with_a_link_in_its_path_is_refused_before_the_model_call(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        _ = (outside / "tool.py").write_text(SCRIPT)
        (self.repo / "linked.py").symlink_to(outside / "tool.py")
        (self.repo / "tools").symlink_to(outside, target_is_directory=True)
        _ = self.git(self.repo, "add", "linked.py", "tools")
        _ = self.git(self.repo, "commit", "-qm", "links")
        (self.repo / "sub").mkdir()
        for word in ("linked.py", "tools/tool.py", "sub/../check.py"):
            with self.subTest(word=word):
                self.assert_refused(("python3", word), "mac-linux takes no program file with a symbolic link or .. "
                                    + "in its path in the checkout, because the guest copy can hold another file "
                                    + f"there: {self.repo / word}; give the real path", checked=False)

    def test_an_absolute_path_in_the_command_is_refused_before_the_model_call(self) -> None:
        word = str(self.repo / "check.py")
        self.assert_refused(("python3", word), "mac-linux takes no absolute path in the command, because the path "
                            + f"names a file of the guest: {word}; give a path in the checkout or a program name",
                            checked=False)

    def test_another_repository_is_refused_before_the_model_call(self) -> None:
        other = self.checkout("other", "https://example.test/other.git")
        status, errors = self.attempt(repo=other)
        self.assertEqual(status, hand_made.REFUSED_EXIT, errors)
        self.assertIn("mac-linux: the Mac Linux guest accepts claude_commands, but the source repository is other",
                      errors)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.guest.events, [])

    def test_a_program_file_outside_the_checkout_is_refused_before_the_model_call(self) -> None:
        tools = self.root / "tools"
        tools.mkdir()
        script = tools / "fixture-tool"
        _ = script.write_text("#!/bin/sh\necho tool\n")
        script.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}):
            status, errors = self.attempt(("fixture-tool",))
        self.assertEqual(status, hand_made.REFUSED_EXIT)
        self.assertIn("mac-linux runs its own copy of each program file outside the checkout, and the check reads "
                      + f"only the natedev copy: {script}; use a file in the checkout", errors)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.guest.events, [])
        files = cast(list[dict[str, object]], self.only_row()["files"])
        self.assertEqual([item["path"] for item in files], [str(script)])


if __name__ == "__main__":
    _ = unittest.main()
