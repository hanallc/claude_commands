"""Check failed probes, repository declines and recovery with private targets."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import time
import threading
from typing import cast, override
import unittest
from unittest import mock

from scripts.lib.helper_tree_test_support import COPY_HELPERS, copy_helpers

from scripts.build_hold.owning_session import NoOwningSession
from scripts.aws_target import ledger
from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.build_queue.admission_test_support import caller_admission
from scripts.build_targets import build_targets, target_config, targets
from scripts.direct.verify_lint_scope_test_support import GIT_STUB, VerifyLintScopeFixture
from scripts.lib.checkout_test_support import write_python_tool
from scripts.lib.process_group import send_process_group
from scripts.lint.memory_test_support import ADMISSION_CONFIG_FIXTURE, write_config_fixture
from scripts.mac_test import eligibility, mac_test, offload, claim_state
from scripts.mac_test.offload_test_commands import write_shell
from scripts.mac_test.offload_test_support import FIXTURE_MIRROR_ROOT, OffloadCommandFixture
from scripts.production.cli_fixture import invoke_cli

PROBE_CONFIG = {
    "connect_timeout_s": "6",
    "probe_timeout_s": "300",
    "leftover_timeout_s": "300",
    "unreachable_backoff_s": "300",
    "mirror_busy_backoff_s": "120",
    "free_floor_gib": "60",
    "abandoned_session_minutes": "9",
    "mac_budget_gib": "24",
    "mac_budget_gib.hana": "64",
    "mirror_root": "~/" + FIXTURE_MIRROR_ROOT,
    "ci_repo": "",
    "gh_timeout_s": "30",
    "block_hours": "4",
    "block_max_hours": "48",
    "block_warn_minutes": "15",
    "claim_idle_minutes": "15",
    "claim_poll_seconds": "60",
}

PINNED_PROBE_CONFIG = "".join(f"{name}={value}\n" for name, value in PROBE_CONFIG.items())

PINNED_ADMISSION_CONFIG = ADMISSION_CONFIG_FIXTURE.read_text() + (
    "merge_gate_governor_gib=4\npeak_history_share_percent=10\nprogress_profile_sweep_seconds=0.05\n"
    + "program_first_peak_gib.targeted-test=4\nremote_start_hold_seconds=1200\nhead_stall_alert_minutes=30\n"
    + "peak_hold_seconds.merge-gate=180\npeak_hold_seconds.targeted-test=180\nnested_tokens=3\n"
)

TRACKED_SCRIPT_GIT = r'''
case " $* " in
    *" ls-files -z scripts config "*) printf "scripts/mac_test/linux_runner.py\0"; exit 0 ;;
    *" rev-parse HEAD "*) printf "fixture\n"; exit 0 ;;
    *" diff HEAD --quiet -- scripts/mac_test/linux_runner.py "*) exit 0 ;;
esac
'''


class FailedProbeTests(OffloadCommandFixture):
    linux_settings: Path = Path()
    identity: eligibility.KnownRepository = eligibility.KnownRepository("hana")

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        assert cls.tools_root is not None
        git = shutil.which("git")
        assert git is not None
        checkout = Path(__file__).resolve().parents[2]
        write_shell(cls.tools_root / "git",
            f'if [[ ${{1-}} == -C && ${{2-}} == {shlex.quote(str(checkout))} ]]; then\n'
            + TRACKED_SCRIPT_GIT + f'fi\nexec {shlex.quote(git)} "$@"\n')
        write_python_tool(cls.tools_root / "receive-tree.py", '''
import shlex
import shutil
import sys
from pathlib import Path
from scripts.copies import copies

source = Path(sys.argv[1])
destination = Path.home() / shlex.split(sys.argv[2].split(":", 1)[1])[0]
shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
copies.mark_mirror(destination, "verify", "hana", "fixture")
''')

    @override
    def setUp(self) -> None:
        super().setUp()
        # This checkout has no origin or commits. Use its folder name for identity.
        (self.repository / ".git/config").unlink()
        self.config.update(PROBE_CONFIG)
        self.sync_config()
        self.linux_settings = self.root / "box.conf"
        _ = write_config_fixture(self.linux_settings,
            "offload=on\nhost=fake-box\nkinds=test,check,lint,doc,build\nslots=1\nslots.hana=1\n"
            + "shell_timeout_s=2\nparity.rustc=fixture\nparity.nextest=fixture\n"
            + "parity.mend=fixture\nparity.scripts_commit=fixture\n" + PINNED_PROBE_CONFIG, template="mac_test")
        _ = (self.root / "build_targets.conf").write_text(
            "targets=mac,natedev,box\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\nkind.box=ssh\nplatform.box=linux\nsettings.box=box.conf\n")
        self.environment.update({name: "" for name in (
            "UNIT_RUN_DIR", "UNIT_SESSION_DIR", "UNIT_TEAM_ROLE", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")})
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))

    @override
    def start_admission(self) -> None:
        _ = write_config_fixture(self.root / "build_admission.conf", PINNED_ADMISSION_CONFIG)
        super().start_admission()

    def probe(self, target: str = "box") -> offload.ProbeResult:
        self.sync_fixture()
        config = eligibility.read_config(self.config_path if target == "mac" else self.linux_settings)
        return offload.probe_mac(offload.ProcessController(), config, self.identity, target)

    def healthy(self) -> None:
        # Recovery starts after the configured failure backoff expires.
        for target in ("mac", "box"):
            offload.write_backoff(time.time() - 1, target)
        self.fixture["probe_status"] = 0
        self.fixture["probe_lines"] = ["free_gib=120", "host=fixture", "rc=0",
                                       "nextest=fixture", "mend=fixture", "scripts_commit=fixture"]
        self.sync_fixture()

    def free(self, target: str = "box", repository: Path | None = None) -> str:
        args = mac_test.CliArguments()
        args.target = target
        args.repo_root = str(self.repository if repository is None else repository)
        args.settings = str(self.config_path if target == "mac" else self.linux_settings)
        output = io.StringIO()
        with redirect_stdout(output):
            status = mac_test.command_free_now(args, mac_test.state_paths(target))
        answer = output.getvalue().strip()
        self.assertEqual(status, 0 if answer == "free" else 1)
        return answer

    def reserve(self, *, new_step: bool = False) -> int:
        args = build_targets.CliArguments()
        args.kind, args.repo_root, args.package = "test", str(self.repository), "hana"
        args.target, args.pid = "box", os.getpid()
        args.new_step = new_step
        args.reservation_file = str(self.root / "reservation")
        return build_targets.command_reserve(args)

    def release_reservation(self) -> int:
        claim, = mac_test.read_claims(mac_test.state_paths("box"))
        return build_targets.command_release_wait(os.getpid(),
            identity=claim_state.claim_identity(claim.record), session=NoOwningSession())

    def test_two_mac_eligibility_reads_share_the_failure_backoff(self) -> None:
        target = target_config.configured_ssh_target("mac")
        for failure in (offload.COMMAND_COULD_NOT_START, offload.COMMAND_TIMED_OUT,
                        offload.CommandOutput(255, "", "private connection refused")):
            with self.subTest(failure=type(failure).__name__):
                eligibility.backoff_path("mac").unlink(missing_ok=True)
                (eligibility.state_directory("mac") / "failed_probe").unlink(missing_ok=True)
                with mock.patch.object(offload.ProcessController, "capture", return_value=failure) as capture, \
                     redirect_stderr(io.StringIO()):
                    first = build_targets.mac_target_eligibility(target, self.repository, "hana", "test",
                        targets.ANY_ELIGIBLE_TARGET, session=NoOwningSession())
                    second = build_targets.mac_target_eligibility(target, self.repository, "hana", "test",
                        targets.ANY_ELIGIBLE_TARGET, session=NoOwningSession())
                self.assertIsInstance(first, targets.ExcludedTarget)
                self.assertIsInstance(second, targets.ExcludedTarget)
                self.assertEqual(capture.call_count, 1)
                self.assertIsInstance(eligibility.read_backoff(time.time(), "mac"), eligibility.ActiveBackoff)
                self.assertIsInstance(eligibility.read_failed_probe("mac"), eligibility.FailedProbe)

    def test_probe_logs_only_failure_and_recovery_transitions(self) -> None:
        config = eligibility.read_config(self.config_path)
        errors = io.StringIO()
        with mock.patch.object(offload.ProcessController, "capture", return_value=offload.COMMAND_COULD_NOT_START) as capture, \
             redirect_stderr(errors):
            for _ in range(3):
                self.assertIsInstance(offload.probe_mac(offload.ProcessController(), config, self.identity),
                                      offload.UnavailableProbe)
            self.assertEqual(capture.call_count, 3)
            self.assertEqual(len(errors.getvalue().splitlines()), 1)
            capture.return_value = offload.CommandOutput(0, "free_gib=120\nhost=fixture\nrc=0\n", "")
            self.assertIsInstance(offload.probe_mac(offload.ProcessController(), config, self.identity),
                                  offload.ProbeSnapshot)
        lines = errors.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("could not start", lines[0])
        self.assertIn("reachable again", lines[1])
        self.assertIsInstance(eligibility.read_failed_probe("mac"), eligibility.NoFailedProbe)
        self.assertIsInstance(eligibility.read_backoff(time.time(), "mac"), eligibility.NoBackoff)

    def test_version_decline_stays_repository_specific_after_backoff_expires(self) -> None:
        self.healthy()
        self.fixture["probe_lines"][-3] = "nextest=wrong"
        with redirect_stderr(io.StringIO()):
            probe = self.probe()
        self.assertIsInstance(probe, offload.UnavailableProbe)
        self.assertIsInstance(eligibility.read_backoff(time.time(), "box"), eligibility.NoBackoff)
        self.assertEqual(self.free(), "bad")
        offload.write_backoff(time.time() - 1, "box")
        _ = eligibility.linux_version_decline_path("box", "hana").write_text("0")
        self.assertEqual(self.free(), "bad")
        other = self.root / "other"
        _ = subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)
        self.assertEqual(self.free(repository=other), "free")
        self.healthy()
        self.assertIsInstance(self.probe(), offload.ProbeSnapshot)
        self.assertEqual(self.free(), "bad", "A target probe must keep a repository decline")

    def test_failed_reach_stays_bad_for_every_repository_until_a_good_probe(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        offload.write_backoff(time.time() - 1, "box")
        other = self.root / "other"
        _ = subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)
        self.assertEqual(self.free(), "bad")
        self.assertEqual(self.free(repository=other), "bad")
        self.healthy()
        self.assertIsInstance(self.probe(), offload.ProbeSnapshot)
        self.assertEqual(self.free(), "free")
        self.assertEqual(self.free(repository=other), "free")

    def test_waiting_reservation_refuses_bad_but_new_step_reaches_the_claim(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        before = len(self.events())
        self.assertEqual(self.reserve(), 1)
        self.assertEqual(len(self.events()), before)
        self.assertEqual(self.reserve(new_step=True), 0)
        claim = next(claim for claim in mac_test.read_claims(mac_test.state_paths("box"))
                     if claim.record["pid"] == os.getpid())
        self.assertEqual(claim.record["what"], "nextest hana")
        self.assertEqual((self.root / "reservation").read_text(), f"box\t{self.linux_settings}\n")
        self.assertGreater(len(self.events()), before)
        self.assertEqual(self.release_reservation(), 0)

    def run_reserve_cli(self, *, new_step: bool) -> subprocess.CompletedProcess[str]:
        command = ["reserve", "--target", "box",
                   "--repo-root", str(self.repository), "--package", "hana", "--kind", "test",
                   "--pid", str(os.getpid()), "--reservation-file", str(self.root / "ssh-reservation")]
        if new_step:
            command.append("--new-step")
        admission = caller_admission(self, self.root, self.environment)
        return invoke_cli(build_targets, build_targets.main, command, self.environment, self.root,
                          environment_lock=admission.loop_access)

    def test_retry_refuses_a_failed_target_without_a_new_probe(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        before = self.events_for("ssh")
        result = self.run_reserve_cli(new_step=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.events_for("ssh"), before)
        self.assertEqual(self.free(), "bad")
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("box")), ())

    def test_new_step_reserve_command_reaches_a_bad_target(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        before = self.events_for("ssh")
        result = self.run_reserve_cli(new_step=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        probes = self.events_for("ssh")[len(before):]
        self.assertEqual(len(probes), 1, probes)
        self.assertEqual(probes[0]["args"][-1], "true")
        self.assertFalse(probes[0]["claimed"], "the probe must precede the claim")
        self.assertEqual((self.root / "ssh-reservation").read_text(), f"box\t{self.linux_settings}\n")
        self.assertEqual(self.release_reservation(), 0)

    def test_bad_mark_preserves_busy_blocked_and_off_precedence(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        self.assertEqual(self.reserve(new_step=True), 0)
        self.assertEqual(self.free(), "busy")
        self.assertEqual(self.release_reservation(), 0)
        paths = mac_test.state_paths("box")
        _ = paths.block.write_text(json.dumps({"version": 2, "holder": "fixture", "for": "test",
            "since": "2026-01-01T00:00:00+00:00", "expires": "2099-01-01T00:00:00+00:00",
            "session": None, "showrunner": None,
            "state": "active", "ci": "not_configured", "free_message": {"kind": "not_needed"}}))
        self.assertEqual(self.free(), "blocked")
        _ = write_config_fixture(self.linux_settings, "offload=off\n", template="mac_test")
        self.assertEqual(self.free(), "off")

    def test_reach_error_prints_only_the_last_nonempty_stderr_line(self) -> None:
        self.fixture["probe_status"] = 1
        self.fixture["probe_error"] = "Traceback (most recent call last):\n  broken call\nValueError: fixture failed\n\n"
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertIsInstance(self.probe("mac"), offload.UnavailableProbe)
        self.assertEqual(output.getvalue().splitlines(),
                         ["mac_test: could not reach the Mac: ValueError: fixture failed"])
        offload.write_backoff(time.time() - 1, "mac")
        self.assertEqual(self.free("mac"), "bad")


    def test_failed_mark_precedes_aws_status_and_mac_status_then_clears(self) -> None:
        self.fixture["probe_status"] = 255
        for name in ("mac", "box"):
            with redirect_stderr(io.StringIO()):
                self.assertIsInstance(self.probe(name), offload.UnavailableProbe)
        linux = target_config.SshTarget("box", "linux", self.root / "linux.conf")
        self.assertIsInstance(targets.target_state(linux), targets.Unreachable)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mac_test.command_target_status(mac_test.state_paths("mac")), 0)
        self.assertEqual(output.getvalue(), "unreachable: failed probe\n")
        self.healthy()
        for name in ("mac", "box"):
            self.assertIsInstance(self.probe(name), offload.ProbeSnapshot)
        self.assertIsInstance(targets.target_state(target_config.configured_ssh_target("box")), targets.Free)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mac_test.command_target_status(mac_test.state_paths("mac")), 0)
        self.assertEqual(output.getvalue(), "free\n")

    def install_script_copy_tools(self, *, copy_source: bool = False) -> None:
        stub = self.bin_directory / "command-stub"
        source = stub.read_text().replace("printf 'started\\n' > \"$root/probe-started\"",
            'if [[ ${!#} == *".build-target-script-commit"* && ${!#} == printf* ]]; then\n'
            + '    printf fixture > "$root/remote-stamp"\nfi\n'
            + "printf 'started\\n' > \"$root/probe-started\"")
        source = source.replace("for line in \"${probe_lines[@]}\"; do printf '%s\\n' \"$line\"; done",
            'for line in "${probe_lines[@]}"; do\n'
            + '    if [[ $line == scripts_commit=fixture ]]; then\n'
            + '        line="scripts_commit=$(< "$root/remote-stamp")"\n    fi\n'
            + "    printf '%s\\n' \"$line\"\ndone")
        tools = self.private_tools()
        if copy_source:
            receiver = tools / "receive-tree.py"
            source = source.replace('    exit "$rsync_status"',
                '    if [[ " $* " != *" --files-from="* ]]; then\n'
                + f'        {shlex.quote(str(receiver))} "${{@: -2:1}}" "${{!#}}" || exit $?\n'
                + '    fi\n    exit "$rsync_status"')
        _ = stub.write_text(source)
        (self.bin_directory / "git").symlink_to(tools / "git")

    def test_new_step_copies_the_tree_and_deduplicates_the_probe(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        offload.write_version_decline("box", "hana")
        decline = eligibility.linux_version_decline_path("box", "hana").read_text()
        self.healthy()
        self.fixture["hold_probe"] = True
        self.sync_fixture()
        self.install_script_copy_tools(copy_source=True)
        _ = (self.stub_directory / "remote-stamp").write_text("old")
        mirror = Path(self.environment["HOME"]) / offload.remote_mirror(self.identity, "box")
        mirror.mkdir(parents=True)
        stale_source = mirror / "stale-source"
        _ = stale_source.write_text("old repository tree")
        _ = (self.repository / "stale-source").write_text("current repository tree")
        target = target_config.configured_ssh_target("box")
        result = build_targets.ssh_target_eligibility(target, self.repository, "hana", "test",
                                                     targets.LocalRequired("local step"), new_step=True)
        self.assertIsInstance(result, targets.ExcludedTarget)
        # The worker holds the probe gate. The eligibility answer must return first.
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = build_targets.ssh_target_eligibility(target, self.repository, "hana", "test",
                                                     targets.ANY_ELIGIBLE_TARGET, new_step=True)
        self.assertIsInstance(result, targets.EligibleTarget)
        descriptor = os.open(self.stub_directory / "continue-probe", os.O_WRONLY)
        try:
            _ = os.write(descriptor, b"continue\ncontinue\ncontinue\n")
        finally:
            os.close(descriptor)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertIsInstance(eligibility.read_failed_probe("box"), eligibility.NoFailedProbe)
        self.assertEqual(eligibility.linux_version_decline_path("box", "hana").read_text(), decline)
        self.assertEqual(stale_source.read_text(), "current repository tree")
        copies = [event for event in self.events() if event["command"] == "rsync"]
        self.assertEqual(len(copies), 2)
        self.assertTrue(any(word.startswith("--files-from=") for word in copies[0]["args"]))
        self.assertIn("--delete", copies[1]["args"])
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("box")), ())

    def test_new_pinned_step_reserves_before_recovery_can_take_its_claim(self) -> None:
        self.healthy()
        self.fixture["probe_lines"][-3] = "nextest=wrong"
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.assertEqual(self.free(), "bad")
        self.healthy()
        self.fixture["hold_probe"] = True
        self.sync_fixture()
        self.install_script_copy_tools()
        ready = self.stub_directory / "claim-ready"
        os.mkfifo(ready)
        stub = self.bin_directory / "command-stub"
        source = stub.read_text().replace('printf \'started\\n\' > "$root/probe-started"',
            'if [[ ${!#} == true ]]; then exit 0; fi\n'
            + 'printf \'started\\n\' > "$root/probe-started"')
        source = source.replace('if (( hold_probe )); then await_release continue-probe; fi',
            'if (( hold_probe )); then\n'
            + '    if [[ ! -f "$root/claim-ready-sent" ]]; then\n'
            + '        printf sent > "$root/claim-ready-sent"\n'
            + '        printf "ready\\n" > "$root/claim-ready"\n    fi\n'
            + '    await_release continue-probe\nfi')
        _ = stub.write_text(source)
        start_recovery = build_targets.start_probe_recovery
        recovery_started = False

        def observe_recovery(target: target_config.SshTarget, repo_root: Path,
                             package: str, kind: build_targets.StepKind) -> None:
            nonlocal recovery_started
            recovery_started = True
            start_recovery(target, repo_root, package, kind)
            # Force the old ordering: the probe owns the claim before eligibility returns.
            with ready.open() as signal:
                self.assertEqual(signal.readline(), "ready\n")

        args = build_targets.CliArguments()
        args.target, args.kind, args.repo_root, args.package = "box", "test", str(self.repository), "hana"
        args.pid, args.new_step = os.getpid(), True
        args.reservation_file = str(self.root / "pinned-reservation")
        output = io.StringIO()
        try:
            with mock.patch.object(build_targets, "start_probe_recovery", new=observe_recovery), redirect_stdout(output):
                self.assertEqual(build_targets.command_pinned_eligible(args, targets.ANY_ELIGIBLE_TARGET), 0)
            self.assertEqual(mac_test.read_claims(mac_test.state_paths("box")), ())
            self.assertEqual(build_targets.command_reserve(args), 0)
            self.assertIn(f"box\teligible\t-\t{self.linux_settings}\tlinux\n", output.getvalue())
            claim, = mac_test.read_claims(mac_test.state_paths("box"))
            self.assertEqual(claim.record["pid"], os.getpid())
            self.assertEqual(Path(args.reservation_file).read_text(), f"box\t{self.linux_settings}\n")
        finally:
            if recovery_started:
                with (self.stub_directory / "continue-probe").open("w") as release:
                    _ = release.write("continue\ncontinue\ncontinue\n")
                with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.release_reservation(), 0)

    def test_pinned_eligibility_repeated_in_a_wait_starts_no_recovery(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        self.install_script_copy_tools()
        args = build_targets.CliArguments()
        args.target, args.kind, args.repo_root, args.package = "box", "test", str(self.repository), "hana"
        self.clear_events()
        for attempt in range(2):
            with self.subTest(attempt=attempt), redirect_stdout(io.StringIO()):
                self.assertEqual(build_targets.command_pinned_eligible(args, targets.ANY_ELIGIBLE_TARGET), 0)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.free(), "bad")

    def test_mac_version_decline_clears_when_the_next_run_matches(self) -> None:
        self.fixture["run_lines"] = [{"stream": "stdout", "text": "mac version mismatch: nextest differs"}]
        self.fixture["run_status_reported"] = False
        self.fixture["run_ssh_status"] = 1
        self.sync_fixture()
        result = self.command()
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertEqual(self.free("mac"), "bad")
        self.assertIsInstance(eligibility.read_backoff(time.time()), eligibility.NoBackoff)

        self.fixture["run_lines"] = []
        self.fixture["run_status_reported"] = True
        self.fixture["run_ssh_status"] = 0
        self.sync_fixture()
        passed = self.command()
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertEqual(self.free("mac"), "free")

    def test_busy_target_skips_recovery_and_keeps_the_mark(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        self.assertEqual(self.reserve(new_step=True), 0)
        count = len(self.events())
        target = target_config.configured_ssh_target("box")
        result = build_targets.ssh_target_eligibility(target, self.repository, "hana", "test",
                                                     targets.ANY_ELIGIBLE_TARGET, new_step=True)
        self.assertIsInstance(result, targets.ExcludedTarget)
        self.assertEqual(len(self.events()), count)
        self.assertIsInstance(eligibility.read_failed_probe("box"), eligibility.FailedProbe)
        self.assertEqual(self.release_reservation(), 0)

    def install_probe_gate(self) -> Path:
        ready = self.stub_directory / "recovery-ready"
        os.mkfifo(ready)
        stub = self.bin_directory / "command-stub"
        source = stub.read_text().replace('if (( hold_probe )); then await_release continue-probe; fi', r'''
if (( hold_probe )) && [[ ${!#} != true && ${!#} != printf* ]]; then
    if mkdir "$root/probe-gated" 2>/dev/null; then
        printf 'ready\n' > "$root/recovery-ready"
        await_release continue-probe
    fi
fi
''')
        _ = stub.write_text(source)
        self.fixture["hold_probe"] = True
        self.sync_fixture()
        return ready

    def finish_recovery(self, target: str = "box") -> None:
        with (self.stub_directory / "continue-probe").open("w") as release:
            _ = release.write("continue\n")
        with (eligibility.state_directory(target) / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)

    def test_mac_eligibility_uses_its_first_capacity_answer_during_recovery(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe("mac"), offload.UnavailableProbe)
        self.healthy()
        self.config.update({f"parity.{name}": "fixture" for name in ("rustc", "nextest", "mend", "scripts_commit")})
        self.sync_config()
        self.install_script_copy_tools()
        ready = self.install_probe_gate()
        start_recovery = build_targets.start_probe_recovery

        def claim_before_return(target: target_config.SshTarget, repo_root: Path,
                                package: str, kind: build_targets.StepKind) -> None:
            start_recovery(target, repo_root, package, kind)
            with ready.open() as signal:
                self.assertEqual(signal.readline(), "ready\n")

        self.clear_events()
        try:
            with mock.patch.object(build_targets, "start_probe_recovery", new=claim_before_return):
                result = build_targets.ssh_target_eligibility(target_config.configured_ssh_target("mac"),
                    self.repository, "hana", "test", targets.ANY_ELIGIBLE_TARGET, new_step=True)
            self.assertIsInstance(result, targets.EligibleTarget)
            probes = [event for event in self.events_for("ssh")
                      if not event["args"][-1].startswith("printf") and event["args"][-1] != "true"]
            self.assertEqual(len(probes), 1)
            self.assertEqual(self.free("mac"), "busy")
            self.assertIsInstance(eligibility.read_failed_probe(), eligibility.FailedProbe)
        finally:
            self.finish_recovery("mac")
        self.assertEqual(self.free("mac"), "free")

    def test_stopped_recovery_releases_its_claim_and_the_next_step_can_recover(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        self.install_script_copy_tools()
        ready = self.install_probe_gate()
        reaped = threading.Event()
        statuses: list[tuple[int, int]] = []
        real_waitpid = os.waitpid

        def observe_reap(pid: int, options: int) -> tuple[int, int]:
            result = real_waitpid(pid, options)
            if threading.current_thread().name == "probe-recovery-reaper":
                statuses.append(result)
                reaped.set()
            return result

        _ = self.enterContext(mock.patch.object(os, "waitpid", side_effect=observe_reap))
        build_targets.start_probe_recovery(target_config.configured_ssh_target("box"), self.repository, "hana", "test")
        with ready.open() as notification:
            self.assertEqual(notification.readline(), "ready\n")
        claim, = mac_test.read_claims(mac_test.state_paths("box"))

        def stop_recovery_group() -> None:
            try:
                send_process_group(claim.record["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass

        self.addCleanup(stop_recovery_group)
        os.kill(claim.record["pid"], signal.SIGTERM)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("box")), ())
        self.assertEqual(self.free(), "bad")
        self.assertTrue(reaped.wait(5), "the recovery child was not reaped")
        pid, status = statuses[0]
        self.assertEqual(pid, claim.record["pid"])
        self.assertEqual(os.waitstatus_to_exitcode(status), 143)
        result = build_targets.ssh_target_eligibility(target_config.configured_ssh_target("box"),
            self.repository, "hana", "test", targets.ANY_ELIGIBLE_TARGET, new_step=True)
        self.assertIsInstance(result, targets.EligibleTarget)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(self.free(), "free")

    def test_recovery_preserves_the_repository_decline_when_tools_still_differ(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        offload.write_version_decline("box", "hana")
        record = eligibility.linux_version_decline_path("box", "hana")
        previous = record.read_text()
        self.healthy()
        self.fixture["probe_lines"][-3] = "nextest=wrong"
        self.sync_fixture()
        self.install_script_copy_tools()
        result = build_targets.ssh_target_eligibility(target_config.configured_ssh_target("box"),
            self.repository, "hana", "test", targets.ANY_ELIGIBLE_TARGET, new_step=True)
        self.assertIsInstance(result, targets.EligibleTarget)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(record.read_text(), previous)
        self.assertIsInstance(eligibility.read_failed_probe("box"), eligibility.FailedProbe)

    def test_recovery_copies_an_absent_mirror_before_the_real_linux_shell_probe(self) -> None:
        self.fixture["probe_status"] = 255
        with redirect_stderr(io.StringIO()):
            self.assertIsInstance(self.probe(), offload.UnavailableProbe)
        self.healthy()
        self.install_script_copy_tools(copy_source=True)
        _ = (self.repository / "flake.nix").write_text("{ outputs = _: {}; }\n")
        _ = (self.repository / "flake.lock").write_text("{}\n")
        _ = write_config_fixture(self.linux_settings,
            "shell_cache=.local/state/probe-shells\ndevshell=default\n", template="mac_test")
        home = Path(self.environment["HOME"])
        _ = copy_helpers(home, *COPY_HELPERS)
        for name, program in {
            "nix": '#!/bin/sh\nprintf "export IN_NIX_SHELL=1\\n"\n',
            "cargo-nextest": '#!/bin/sh\nprintf "fixture\\n"\n',
            "cargo-mend": '#!/bin/sh\nprintf "fixture\\n"\n',
        }.items():
            path = self.bin_directory / name
            _ = path.write_text(program)
            path.chmod(0o755)
        ssh = self.bin_directory / "ssh"
        ssh.unlink()
        write_shell(ssh, r'''
command=${!#}
case $command in
    true) exit 0 ;;
    printf*) exec bash -c "$command" ;;
    *dispatch_start_settled=*leftovers=0*|*".claude/scripts/mac_test/linux_runner.py probe "*)
        cd -- "$HOME" || exit 1
        exec bash -c "$command"
        ;;
esac
printf 'Unexpected remote command: %s\n' "$command" >&2
exit 1
''')
        result = build_targets.ssh_target_eligibility(target_config.configured_ssh_target("box"),
            self.repository, "hana", "test", targets.LocalRequired("local fixture tools"), new_step=True)
        self.assertIsInstance(result, targets.ExcludedTarget)
        with (eligibility.state_directory("box") / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        self.assertEqual(self.free(), "free")
        mirror = home / offload.remote_mirror(self.identity, "box")
        self.assertEqual((mirror / "flake.nix").read_text(), (self.repository / "flake.nix").read_text())
        self.assertEqual((mirror / "flake.lock").read_text(), (self.repository / "flake.lock").read_text())
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("box")), ())


class StepRecoveryTests(VerifyLintScopeFixture):
    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        _ = write_config_fixture(self.root / "build_admission.conf", PINNED_ADMISSION_CONFIG)

    def test_each_compiling_lint_command_has_one_placement_episode(self) -> None:
        settings = self.root / "mac_test.conf"
        _ = write_config_fixture(settings, PINNED_PROBE_CONFIG + f"offload=on\nlinux_only.{self.root.name}=\n")
        _ = (self.root / "build_targets.conf").write_text(
            "targets=natedev,mac\nkind.natedev=local\nplatform.natedev=linux\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n")
        state = self.root / "mac-state"
        state.mkdir()
        _ = (state / "failed_probe").write_text("failed\n")
        admission = caller_admission(self, self.root, self.environment)
        requests: list[wire.PlacementRequest] = []
        previous = admission.server.handle_request

        def observe(request: wire.Request,
                    connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            if isinstance(request, wire.PlacementRequest):
                requests.append(request)
            return previous(request, connection)

        _ = self.enterContext(mock.patch.object(admission.server, "handle_request", side_effect=observe))
        self.environment.update({"BUILD_TARGETS_SCRIPT": str(Path(build_targets.__file__)),
                                 "MAC_TEST_STATE_DIR": str(state)})
        self.write_config(clippy="on")
        # A held recovery lock keeps the mark while the real local steps run.
        with (state / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.verify("lint", "sample", "--local", "local fixture tools")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        episodes = {request.episode: request.constraints.command for request in requests}
        local_steps = [row for row in self.cargo_calls()
                       if row[:1] in (["mend"], ["+nightly"], ["clippy"]) and "--version" not in row]
        taplo_steps = [json.loads(line) for line in self.taplo_events.read_text().splitlines()]
        self.assertGreaterEqual(len(local_steps), 2)
        self.assertTrue(taplo_steps)
        for name in ("mend", "clippy"):
            expected = [row for row in local_steps if row[0] == name]
            placed = [command for command in episodes.values() if name in command]
            self.assertEqual(len(placed), len(expected), (name, episodes, local_steps))
            self.assertEqual(len(placed), 1, (name, episodes))
        self.assertEqual(len(episodes), 2, episodes)
        self.assertEqual([request for request in requests if "fmt" in request.constraints.command], [])
        self.assertEqual([request for request in requests if "taplo" in request.constraints.command], [])
        self.assertEqual(list((self.root / "mac-state/claims").glob("*.json")), [])

    def test_local_work_finishes_while_a_new_step_recovers_linux(self) -> None:
        _ = write_config_fixture(self.root / "mac_test.conf",
            PINNED_PROBE_CONFIG + f"offload=on\nlinux_only.{self.root.name}=\n")
        _ = write_config_fixture(self.root / "linux.conf",
            "offload=on\nhost=fake-linux\nci_repo=\nkinds=test,check,doc,lint\nslots=1\n"
            + "devshell=default\nshell_cache=.local/state/build-target/shells\nshell_timeout_s=2\n"
            + "parity.rustc=fixture\nparity.nextest=fixture\nparity.mend=fixture\n"
            + "parity.scripts_commit=fixture\n" + PINNED_PROBE_CONFIG, template="mac_test")
        _ = (self.root / "build_targets.conf").write_text(
            "targets=mac,natedev,linux\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\nkind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n")
        state = Path(self.environment["HOME"]) / ".local/state/build-targets/linux"
        state.mkdir(parents=True)
        book = ledger.CostLedger(state)
        observed = int(time.time())
        book.record_inventory(ledger.ResourceInventory(observed, (
            ledger.PricedResource("retained-storage", Decimal("1")),), "private priced inventory"))
        book.observe(ledger.InstanceObservation(observed, "stopped", "fake DescribeInstances"))
        _ = (state / "failed_probe").write_text("failed\n")
        _ = (self.root / "remote-stamp").write_text("old")
        gate = self.root / "probe-release"
        os.mkfifo(gate)
        descriptor = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, descriptor)
        self.write_shell(self.root / "bin/git", TRACKED_SCRIPT_GIT + GIT_STUB)
        write_shell(self.root / "bin/rsync", r'''
{ json_args "$@"; printf '\n'; } >> "$PACKAGE_ROOT/script-copies"
''')
        write_shell(self.root / "bin/ssh", r'''
command=${!#}
case $command in
    true) exit 0 ;;
    printf*".build-target-script-commit"*)
        printf fixture > "$PACKAGE_ROOT/remote-stamp"
        exit
        ;;
    *dispatch_start_settled=*leftovers=0*)
        cd -- "$HOME" || exit 1
        exec bash -c "$command"
        ;;
    *".claude/scripts/mac_test/linux_runner.py probe "*)
        { json_args "$@"; printf '\n'; } >> "$PACKAGE_ROOT/recovery-probes"
        IFS= read -r release < "$PACKAGE_ROOT/probe-release" || exit 1
        [[ $release == continue ]] || exit 1
        printf 'free_gib=1000\nhost=fake-linux\nnextest=fixture\nmend=fixture\nrc=0\n'
        printf 'scripts_commit=%s\n' "$(< "$PACKAGE_ROOT/remote-stamp")"
        exit
        ;;
esac
printf 'fake Mac unavailable\n' >&2
exit 255
''')
        self.write_config(clippy="on")
        self.environment["BUILD_TARGETS_SCRIPT"] = str(Path(build_targets.__file__))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        admission = caller_admission(self, self.root, self.environment)
        admission.server.host_probe = admission.server.read_provider_evidence
        result = self.verify("lint", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(args[:1] == ["clippy"] for args in self.cargo_calls()))
        # The remote probe remains blocked after local work finishes.
        with mock.patch.dict(os.environ, self.environment):
            self.assertIsInstance(eligibility.read_failed_probe("linux"), eligibility.FailedProbe)
        _ = os.write(descriptor, b"continue\n")
        with (state / "recovery-probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        with mock.patch.dict(os.environ, self.environment):
            self.assertEqual(mac_test.free_now(mac_test.state_paths("linux"),
                self.root / "linux.conf", self.root), "free")
            answer = build_targets.ssh_target_eligibility(target_config.configured_ssh_target("linux"),
                self.root, "sample", "lint", targets.ANY_ELIGIBLE_TARGET)
        self.assertIsInstance(answer, targets.EligibleTarget)
        probes = (self.root / "recovery-probes").read_text().splitlines()
        self.assertEqual(len(probes), 1)
        copies = [cast(list[str], json.loads(line)) for line in (self.root / "script-copies").read_text().splitlines()]
        self.assertEqual(len(copies), 2)
        self.assertTrue(any(word.startswith("--files-from=") for word in copies[0]))
        self.assertIn("--delete", copies[1])


if __name__ == "__main__":
    _ = unittest.main()
