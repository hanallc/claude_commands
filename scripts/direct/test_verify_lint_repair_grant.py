"""Exercise the local lint grant with a private admission service."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from scripts.build_queue import execution_identity
from scripts.build_queue import admission_client, admission_protocol as wire, admission_service as service, placement
from scripts.build_queue.admission_test_support import caller_admission, caller_provider_controls
from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture
from scripts.lint.lint_repair_grant import check_shell_grant
from scripts.build_queue.admission_records import GIB
from scripts.lint.memory_test_support import write_pressure_reading


class PlacementStop(Enum):
    NONE = ""
    REPAIR_HOLD = "repair-hold"


PLACEMENT_STOP = r"""
case "$TEST_PLACEMENT_STOP" in
    repair-hold)
        eval "$(declare -f build_hold_check_local | sed '1s/build_hold_check_local/fixture_hold_check/')"
        build_hold_check_local() {
            if [[ "${LINT_REPAIR_REUSED_GRANT:-}" == 1 && ! -e "$TEST_STOP_APPLIED" ]]; then
                : > "$TEST_STOP_APPLIED"
                return 10
            fi
            fixture_hold_check
        }
        ;;
esac
"""


@dataclass(frozen=True)
class RepairRun:
    result: subprocess.CompletedProcess[str]
    requested_episodes: set[str]
    grants: list[wire.LocalPlacementGranted]
    cancels: list[wire.CancelPlacement]
    releases: list[wire.ReleaseRequest]
    waiter_events: list[str]
    finish_events: list[str]
    repair_followup: list[bool]


class VerifyLintRepairGrantTests(VerifyStepRoutesFixture):
    def repair(self, *, undersized: bool = False, with_waiter: bool = False, foreign_manifest: bool = False,
               foreign_grant: bool = False, fail_finish: bool = False, fail_cancel: bool = False,
               ordinary: bool = False, cargo_status: int = 0,
               stop: PlacementStop = PlacementStop.NONE) -> RepairRun:
        admission = caller_admission(self.fixture, self.fixture.root, self.fixture.environment)
        controls = caller_provider_controls(self.fixture, admission)
        controls.set_host("mac", placement.TemporaryExclusion("busy"))
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        config = Path(self.fixture.environment["LINT_CONFIG_READER"])
        _ = config.write_text("""\
lint_config_enabled() {
    if [[ "$1" == mend && -n "${TEST_PLACEMENT_STOP_SCRIPT:-}" && -z "${TEST_PLACEMENT_STOP_LOADED:-}" ]]; then
        TEST_PLACEMENT_STOP_LOADED=1
        source "$TEST_PLACEMENT_STOP_SCRIPT"
    fi
    if [[ -n "${TEST_REPAIR_MANIFEST:-}" ]]; then
        lint_rewrite() {
            if [[ "$1" == invoke_mend && "${LINT_MEND_WAIT:-}" == 0 ]]; then
                "$@" --manifest-path "$TEST_REPAIR_MANIFEST"
            else
                "$@"
            fi
        }
    fi
    [[ "$1" == mend || "$1" == fmt || ( "$1" == clippy && "${TEST_ORDINARY_CLEANUP:-}" == 1 ) ]]
}
lint_config_skip_notice() { :; }
_lint_config_raw() { :; }
""")
        git = self.fixture.bin_directory / "git"
        _ = git.write_text(git.read_text().replace(
            "if has \"$@\"; then printf 'fixed-tree\\n'; exit 0; fi",
            "if has \"$@\"; then printf '%s\\nfixed-tree\\n' \"$PACKAGE_ROOT\"; exit 0; fi",
        ))
        if stop is not PlacementStop.NONE:
            stop_script = self.fixture.root / "placement-stop.sh"
            _ = stop_script.write_text(PLACEMENT_STOP)
            self.fixture.environment.update(TEST_PLACEMENT_STOP_SCRIPT=str(stop_script),
                TEST_PLACEMENT_STOP=stop.value, TEST_STOP_APPLIED=str(self.fixture.root / "placement-stopped"))
        if ordinary:
            self.fixture.environment["TEST_ORDINARY_CLEANUP"] = "1"
        if cargo_status:
            cargo = self.fixture.bin_directory / "cargo"
            self.fixture.environment["TEST_REPAIR_STATUS"] = str(cargo_status)
            _ = cargo.write_text(cargo.read_text().replace('if arguments[:1] == ["metadata"]:', """\
if arguments[:1] == ["mend"] and "--fix" in arguments:
    raise SystemExit(int(os.environ["TEST_REPAIR_STATUS"]))
if arguments[:1] == ["metadata"]:""", 1))
        if foreign_manifest:
            foreign = self.fixture.root / "tmp" / "foreign"
            (foreign / ".git").mkdir(parents=True)
            manifest = foreign / "Cargo.toml"
            _ = manifest.write_text('[package]\nname = "foreign"\n')
            self.fixture.environment["TEST_REPAIR_MANIFEST"] = str(manifest)
            _ = git.write_text(git.read_text().replace('wanted=rev-parse\n', """\
if [[ "$*" == *"$TEST_REPAIR_MANIFEST_DIR"* && "$*" == *'HEAD^{tree}'* ]]; then
    printf '%s\nforeign-tree\n' "$TEST_REPAIR_MANIFEST_DIR"
    exit 0
fi
wanted=rev-parse
""", 1))
            self.fixture.environment["TEST_REPAIR_MANIFEST_DIR"] = str(foreign)
        if foreign_grant:
            other = self.fixture.root / "other-grant.sh"
            admission_client.write_shell_grant(other, admission.occupy_host(), 0)
            self.fixture.environment["TEST_OTHER_GRANT"] = str(other)
            targets = Path(self.fixture.environment["BUILD_TARGETS_SCRIPT"])
            _ = targets.write_text(targets.read_text().replace(
                '    if command in ("constraints",', """\
    if command == "assigned-info":
        other = Path(os.environ["TEST_OTHER_GRANT"])
        if other.is_file():
            grant = Path(sys.argv[sys.argv.index("--assignment-file") + 1]).with_suffix(".sh")
            grant.write_bytes(other.read_bytes())
            other.unlink()
    if command in ("constraints",""", 1))
        requested_episodes: set[str] = set()
        grants: list[wire.LocalPlacementGranted] = []
        cancels: list[wire.CancelPlacement] = []
        releases: list[wire.ReleaseRequest] = []
        waiter_events: list[str] = []
        finish_events: list[str] = []
        repair_followup: list[bool] = []
        waiter = admission.request(need=50 * GIB, since=0) if with_waiter else None
        slot_gap_seconds = 1
        admission.configure(waiting_pass_seconds=0.05, slot_gap_seconds=slot_gap_seconds)
        if not ordinary:
            admission.hold_starts()
        previous = admission.server.handle_request

        def handle(request: wire.Request,
                   connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            if isinstance(request, wire.CancelPlacement):
                cancels.append(request)
                if request.reason == "judge lint before local repair":
                    write_pressure_reading(admission.root / "pressure")
            if isinstance(request, wire.ReleaseRequest):
                releases.append(request)
            repair_followup_request = False
            if isinstance(request, wire.PlacementRequest):
                if (stop is PlacementStop.REPAIR_HOLD
                        and grants and request.episode not in requested_episodes and not repair_followup):
                    first = grants[0].assignment
                    saved = admission.server.assignments[execution_identity.attempt_key(first.episode, first.attempt)]
                    repair_followup.extend((
                        first.episode not in admission.server.reservations,
                        first.episode not in admission.server.current_assignments,
                        saved.state == placement.ClosedAssignment("cancelled"),
                    ))
                    repair_followup_request = True
                elif repair_followup and request.episode != grants[0].assignment.episode:
                    admission.clock.advance(slot_gap_seconds + 1)
                    admission.allow_next_start()
                if ordinary and grants and request.episode not in requested_episodes:
                    first_episode = grants[0].assignment.episode
                    active = first_episode in admission.server.reservations
                    finish_events.append(f"next episode requested with first grant active: {active}")
                    admission.allow_next_start()
                requested_episodes.add(request.episode)
                judging = "--fail-on-warn" in request.constraints.command
                if judging and undersized:
                    profile = request.profile
                    request = replace(request, profile=replace(profile, peak=replace(profile.peak, bytes=1)))
            if fail_finish and grants:
                judgment = grants[0]
                if isinstance(request, wire.ReleaseRequest) and request.reservation == judgment.grant.reservation:
                    finish_events.append("release failed")
                    return [wire.Refused("test release failed")]
                if isinstance(request, wire.ExecutionReport) and request.assignment == judgment.assignment:
                    return [wire.Refused("test execution report failed")]
                if isinstance(request, wire.CancelPlacement) and request.episode == judgment.assignment.episode:
                    if fail_cancel:
                        finish_events.append("cancel failed")
                        return [wire.Refused("test cancel failed")]
                    self.assertIn(request.episode, admission.server.reservations)
                    replies = previous(request, connection)
                    self.assertNotIn(request.episode, admission.server.reservations)
                    finish_events.append("cancelled the active grant")
                    return replies
            if repair_followup_request and isinstance(request, wire.PlacementRequest):
                waiting = replace(request, prerequisite=placement.TokenRequired())
                replies = previous(waiting, service.NO_CONNECTION)
                repair_followup.append(any(isinstance(reply, wire.PlacementWait) for reply in replies))
                admission.clock.advance(slot_gap_seconds + 1)
                admission.allow_next_start()
                _ = admission.server.run_pass()
                first = grants[0].assignment
                repair_followup.append(first.episode not in admission.server.reservations)
                if not all(repair_followup):
                    return [wire.Refused("the first episode did not close before the second request")]
                _ = previous(wire.PrerequisiteReport("test-allow-repair", request.episode, request.owner, request.prerequisite))
                return replies
            replies = previous(request, connection)
            for reply in replies:
                if isinstance(reply, wire.LocalPlacementGranted):
                    grants.append(reply)
                    if waiter is not None and len(grants) == 1:
                        _ = previous(waiter)
                        if waiter.episode in admission.server.waiters:
                            waiter_events.append("queued")
            judgment_ended = bool(grants) and (
                (isinstance(request, wire.CancelPlacement) and request.episode == grants[0].assignment.episode)
                or (isinstance(request, wire.ReleaseRequest) and request.reservation == grants[0].grant.reservation)
            )
            if judgment_ended and waiter is not None and waiter.episode in admission.server.waiters:
                admission.clock.advance(slot_gap_seconds + 1)
                admission.allow_next_start()
                _ = admission.server.run_pass()
                if record := admission.server.reservations.get(waiter.episode):
                    phase = "after" if self.mend_fixes() else "before"
                    waiter_events.append(f"started {phase} repair")
                    _ = previous(wire.ReleaseRequest("test-release-waiter", record.reservation_id, 0))
            return replies

        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
            process = self.fixture.start_verify("lint", "sample", "--no-cache")
            try:
                output, errors = process.communicate(timeout=30)
            except subprocess.TimeoutExpired as error:
                self.fixture.stop_verify(process)
                self.fail(f"verify timed out with service observations {repair_followup}\n{error.stdout!r}\n{error.stderr!r}")
            assert process.returncode is not None
            result = subprocess.CompletedProcess(process.args, process.returncode, output, errors)
        return RepairRun(result, requested_episodes, grants, cancels, releases, waiter_events, finish_events, repair_followup)

    def mend_fixes(self) -> list[list[str]]:
        return [event["args"] for event in self.fixture.cargo_events()
                if event["args"][:1] == ["mend"] and "--fix" in event["args"]]

    def test_repair_finishes_once_without_cancelling_the_judgment_grant(self) -> None:
        run = self.repair()
        self.assertEqual(run.result.returncode, 0, run.result.stdout + run.result.stderr)
        self.assertEqual(len(run.grants), 1)
        self.assertEqual([cancel.reason for cancel in run.cancels], ["judge lint before local repair"])
        self.assertEqual(run.requested_episodes, {run.grants[0].assignment.episode, run.cancels[0].episode})
        self.assertEqual(len(self.mend_fixes()), 1)
        self.assertEqual([(release.reservation, release.status) for release in run.releases],
                         [(run.grants[0].grant.reservation, run.result.returncode)])

    def test_an_undersized_judgment_grant_requires_a_new_placement(self) -> None:
        run = self.repair(undersized=True)
        self.assertEqual(run.result.returncode, 0, run.result.stdout + run.result.stderr)
        self.assertEqual(len(run.grants), 2)
        self.assertIn(run.grants[0].assignment.episode, [cancel.episode for cancel in run.cancels])
        self.assertIn("exceeds the judgment grant", run.result.stderr)

    def test_another_waiter_cannot_start_between_judgment_and_repair(self) -> None:
        run = self.repair(with_waiter=True)
        self.assertEqual(run.result.returncode, 0, run.result.stdout + run.result.stderr)
        self.assertEqual(run.waiter_events, ["queued", "started after repair"])

    def test_repair_manifest_in_another_tree_requires_a_new_placement(self) -> None:
        run = self.repair(foreign_manifest=True)
        self.assertEqual(run.result.returncode, 0, run.result.stdout + run.result.stderr)
        self.assertEqual(len(run.grants), 2)
        self.assertIn(run.grants[0].assignment.episode, [cancel.episode for cancel in run.cancels])
        self.assertEqual(run.result.stderr.count("the repair tree differs from the judgment tree"), 1)
        self.assertIn(self.fixture.environment["TEST_REPAIR_MANIFEST"], self.mend_fixes()[0])

    def test_failed_finish_step_cancels_the_reused_grant(self) -> None:
        run = self.repair(fail_finish=True)
        self.assertEqual(run.result.returncode, 0, run.result.stdout + run.result.stderr)
        self.assertEqual(run.finish_events, ["release failed", "cancelled the active grant"])
        self.assertEqual([cancel.episode for cancel in run.cancels].count(run.grants[0].assignment.episode), 1)

    def test_shell_grant_from_another_run_requires_a_new_placement(self) -> None:
        run = self.repair(foreign_grant=True)
        self.assertEqual(run.result.returncode, 0, run.result.stdout + run.result.stderr)
        self.assertEqual(len(run.grants), 2)
        self.assertIn(run.grants[0].assignment.episode, [cancel.episode for cancel in run.cancels])
        self.assertEqual(run.result.stderr.count("the shell grant belongs to a different reservation"), 1)


    def test_a_shell_grant_with_only_the_reservation_is_refused(self) -> None:
        grant = self.fixture.root / "truncated.sh"
        _ = grant.write_text("BUILDLOG_MEM_RESERVATION=test-reservation\n")
        with self.assertRaisesRegex(ValueError, "the shell grant has missing fields"):
            check_shell_grant(grant, "test-reservation")

    def test_an_ordinary_step_clears_the_grant_when_finish_step_fails(self) -> None:
        memory_gate = Path(__file__).resolve().parents[1] / "lint" / "memory_gate.sh"
        first_grant = self.fixture.root / "first-grant.sh"
        later_grant = self.fixture.root / "later-grant.sh"
        first_grant.touch()
        later_grant.touch()
        finish_calls = self.fixture.root / "finish-calls"
        owner = str(os.getpid())
        result = subprocess.run(
            ["bash", "-ec", r"""
source "$1"
buildlog_memory_python() {
    printf '%s\n' "$2:${*:3}" >> "$finish_calls"
    [[ "$2" != finish-step ]] && return 0
    return 2
}
finish_calls=$4
BUILDLOG_MEM_PRELAUNCH_REASON="test hold"
BUILDLOG_MEM_RESERVATION=first
BUILDLOG_PLACEMENT_EPISODE=first-episode
PLACEMENT_OWNER_PID=$5
BUILDLOG_SELECTED_LOCAL_GRANT=$2
BUILDLOG_PLACEMENT_FILE=first.json
buildlog_release_memory 10

[[ ! -e "$2" ]]
[[ -z "$BUILDLOG_MEM_PRELAUNCH_REASON" && -z "$BUILDLOG_SELECTED_LOCAL_GRANT" && -z "$BUILDLOG_PLACEMENT_FILE" ]]
BUILDLOG_MEM_RESERVATION=later
BUILDLOG_PLACEMENT_EPISODE=later-episode
BUILDLOG_SELECTED_LOCAL_GRANT=$3
BUILDLOG_PLACEMENT_FILE=later.json
buildlog_release_memory 0
[[ ! -e "$3" ]]
""", "test", str(memory_gate), str(first_grant), str(later_grant), str(finish_calls), owner],
            env=self.fixture.environment, cwd=self.fixture.root, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(finish_calls.read_text().splitlines(), [
            "finish-step:--assignment-file first.json --status 10 --reason test hold",
            f"cancel-placement:--episode first-episode --pid {owner} --reason finish-step failed",
            "finish-step:--assignment-file later.json --status 0",
            f"cancel-placement:--episode later-episode --pid {owner} --reason finish-step failed",
        ])


    def test_an_ordinary_failed_finish_cancels_before_the_next_step(self) -> None:
        run = self.repair(fail_finish=True, ordinary=True)
        self.assertEqual(run.result.returncode, 0, run.result.stdout + run.result.stderr)
        self.assertEqual(len(run.grants), 2)
        self.assertEqual(run.finish_events, [
            "release failed", "cancelled the active grant", "next episode requested with first grant active: False",
        ])
        self.assertEqual([cancel.episode for cancel in run.cancels], [run.grants[0].assignment.episode])

    def assert_failed_cleanup_preserves_status(self, cargo_status: int) -> None:
        run = self.repair(fail_finish=True, fail_cancel=True, cargo_status=cargo_status)
        self.assertEqual(run.result.returncode, cargo_status, run.result.stdout + run.result.stderr)
        self.assertEqual(run.finish_events, ["release failed", "cancel failed"])
        episode = run.grants[0].assignment.episode
        self.assertEqual((run.result.stdout + run.result.stderr).count(
            f"could not cancel placement episode {episode}"), 1)

    def test_failed_finish_and_cancel_preserve_cargo_success(self) -> None:
        self.assert_failed_cleanup_preserves_status(0)

    def test_failed_finish_and_cancel_preserve_cargo_failure(self) -> None:
        self.assert_failed_cleanup_preserves_status(137)


    def assert_stopped_repair_abandons_judgment(self, stop: PlacementStop) -> None:
        run = self.repair(stop=stop)
        self.assertEqual(run.repair_followup, [True, True, True, True, True],
                         "E1 must end before the E2 request and remain ended while E2 waits")
        self.assertEqual(run.result.returncode, 0, run.result.stdout + run.result.stderr)
        self.assertEqual(len(self.mend_fixes()), 1)
        self.assertEqual(len(run.grants), 2)
        self.assertNotEqual(run.grants[0].assignment.episode, run.grants[1].assignment.episode)

    def test_hold_after_reuse_abandons_judgment_before_placing_again(self) -> None:
        self.assert_stopped_repair_abandons_judgment(PlacementStop.REPAIR_HOLD)

    def test_unresolved_cleanup_preserves_cargo_status_and_blocks_the_next_request(self) -> None:
        run = self.repair(ordinary=True, fail_finish=True, fail_cancel=True)
        self.assertEqual(run.result.returncode, 2, run.result.stdout + run.result.stderr)
        episode = run.grants[0].assignment.episode
        self.assertEqual(run.requested_episodes, {episode})
        self.assertEqual([release.status for release in run.releases], [0])
        self.assertEqual(len(self.mend_fixes()), 1)
        self.assertEqual((run.result.stdout + run.result.stderr).count(
            f"could not cancel placement episode {episode}"), 1)


if __name__ == "__main__":
    _ = unittest.main()
