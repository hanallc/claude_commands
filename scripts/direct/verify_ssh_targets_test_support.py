"""Build private fixtures for the SSH target tests."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager

import json
import os
from pathlib import Path
from dataclasses import dataclass
import subprocess
from unittest import mock
from typing import Callable, TypedDict, cast, override

from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission, caller_provider_controls
from scripts.build_queue import admission_protocol as wire, admission_service as service, placement
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, GIT_STUB, install_fixed_cargo
from scripts.lint.memory_test_support import fake_memory_readings, write_config_fixture


REPOSITORY = Path(__file__).resolve().parents[2]
MAC_TEST_SCRIPT = REPOSITORY / "scripts/mac_test/mac_test.py"


class SshRunnerEvent(TypedDict):
    target: str
    owner: int
    slot: int
    reserved: bool
    same_path: bool
    queued_at: str
    memory_reservation: str
    actions: list[str]
    entry_claim_ids: list[str]
    adopted_claim_ids: list[str]
    remaining_claim_ids: list[str]
    mode: str
    result_type: str
    clock: str
    holder: str
    remote_output: str
    remote_status: int
    token_held: bool
    pinned: str
    provider_result: str
    release_state: str


class CompletedMemoryHook(TypedDict):
    left: bool
    clock: str
    attempts: int


@dataclass(frozen=True)
class QueueExchange:
    request: wire.Request
    replies: tuple[wire.Reply, ...]


@dataclass(frozen=True)
class ReleaseMemoryAtWait:
    """Release local memory when a real queue wait occurs."""


RELEASE_MEMORY_AT_WAIT = ReleaseMemoryAtWait()


QUEUED_MEMORY_OBSERVER = r'''
import json
import os
from pathlib import Path
import sys
from unittest import mock

with open(Path(os.environ["TEST_SOURCE_REPO"]) / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {"__file__": package_root.name})
from scripts.build_queue import admission_client, admission_protocol as wire, placement
from scripts.lib.atomic_file import write_text_atomic


def main(arguments):
    root = Path(os.environ["TEST_RUNNER_STATE"])
    request_placement = admission_client.request_placement

    def observe(request, **options):
        reply = request_placement(request, **options)
        events = root / "ssh-events.jsonl"
        attempts = len(events.read_text().splitlines()) if events.exists() else 0
        if attempts < int(os.environ.get("TEST_OBSERVE_AFTER_ATTEMPTS", "0")):
            return reply
        sequence = len(tuple(root.glob("memory-hook-[0-9]*")))
        if (isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait)
                and reply.eligibility
                and not any(isinstance(candidate.eligibility, placement.EvidenceNeeded) for candidate in reply.eligibility)):
            left = False
        elif isinstance(reply, wire.Assignment) and sequence:
            left = True
        else:
            return reply
        clock = os.environ.get("TEST_ROUTING_CLOCK", "")
        event = {"left": left, "clock": Path(clock).read_text() if clock else "", "attempts": attempts,
                 "episode": request.episode,
                 "attempt": reply.assignment.attempt if isinstance(reply, wire.Assignment) else ""}
        if not left:
            with (root / "memory-polls.jsonl").open("a") as output:
                output.write(json.dumps(event) + "\n")
        write_text_atomic(root / f"memory-hook-{sequence + 1}", json.dumps(event))
        if not (root / "memory-hook-settled").exists():
            with (root / "memory-hook-ack").open() as acknowledgment:
                assert acknowledgment.read(len("continue")) == "continue"
        return reply

    with mock.patch.object(admission_client, "request_placement", side_effect=observe):
        return admission_client.main(arguments)
'''


SSH_RUNNER = r'''
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import redirect_stdout
from signal import SIGTERM
from unittest import mock

source = Path(os.environ["TEST_SOURCE_REPO"])
with open(source / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.mac_test import eligibility, offload, mac_test, claim_state
from scripts.build_queue import admission_client, admission_protocol as queue_wire, execution_identity, placement
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.direct.verify_ssh_claim_test_support import RetainedClaimExclusion, claim_exclusion_path
from scripts.mac_test.claim_release_test_support import run_claim_release
from scripts.mac_test.offload_test_commands import capture_fence_query, prepare_probe_fence

args = offload.parser().parse_args(namespace=offload.CliArguments())
request = offload.request_from_arguments(args)
config = eligibility.read_config(Path(args.settings))
controller = offload.ProcessController()
root = Path(os.environ["TEST_RUNNER_STATE"])
configured_mode = json.loads(os.environ.get("TEST_SSH_MODES", "{}" )).get(request.target, "passed")
mode = configured_mode
if mode in ("disk-once", "lost-once"):
    mode = "passed" if (root / "ssh-events.jsonl").exists() else mode.removesuffix("-once")
actions = []
adopted_claim_ids = []
adopted_scopes = {}
initial = mac_test.read_claims(mac_test.state_paths(request.target))
before = {claim.path: claim.record for claim in initial}
reserved = bool(args.claim_owner_pid and any(claim.record["pid"] == args.claim_owner_pid for claim in initial))
same_path = False
remote_output = ""
remote_status = 0
launch_error = (offload.OffloadInterrupted(SIGTERM) if mode == "start-launch-interrupted" else
                OSError("SSH could not start") if mode in ("start-launch-failed", "start-journal-unremovable") else None)
check_start_fence = launch_error is not None or mode in (
    "start-fence-confirmed", "start-fence-unconfirmed", "start-fence-interrupted", "start-release-unconfirmed")

def run_start_fence(command):
    settled = real_run(["bash", "-c", command], capture_output=True,
                       text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
    assert settled.returncode == 0, settled.stderr
    return settled.stdout

def complete(arguments, attempt):
    global same_path
    parsed = mac_test.parser().parse_args(arguments[2:], namespace=mac_test.CliArguments())
    text = io.StringIO()
    with redirect_stdout(text):
        if isinstance(request.placement, offload.AssignedOffloadRequest):
            assigned = request.placement.assignment
            status = mac_test.command_claim(parsed, mac_test.state_paths(parsed.target), watch_claim=False,
                                            session=assigned.session, assignment=assigned)
        else:
            status = mac_test.command_claim(parsed, mac_test.state_paths(parsed.target), watch_claim=False)
    reply = text.getvalue()
    if mode == "start-fence-wrong-path":
        reply = "".join(line for line in reply.splitlines(keepends=True) if not line.startswith("claim_record="))
    controller.collect_claim_reply(attempt, offload.CommandOutput(status, reply, ""))
    actions.append("claim:" + str(status))
    after = mac_test.read_claims(mac_test.state_paths(parsed.target))
    adopted_claim_ids.extend(claim.record["claim_id"] for claim in after if claim.record["pid"] == os.getpid())
    adopted_scopes.update((claim_state.claim_identity(claim.record), claim.scope)
                          for claim in after if claim.record["pid"] == os.getpid())
    same_path = any(claim.path in before and claim.record["pid"] == os.getpid() for claim in after)

def capture(arguments, deadline=offload.NO_COMMAND_TIME_LIMIT):
    if len(arguments) > 2 and arguments[2] == "release":
        actions.append("release")
        if mode in ("start-release-unconfirmed", "takeover-release-unconfirmed"):
            return offload.CommandOutput(1, "", "release was not confirmed")
        return run_claim_release(arguments)
    versions = {("rustc", "-V"): "rustc fixture",
                ("cargo", "nextest", "--version"): "nextest fixture",
                ("cargo", "mend", "--version"): "mend fixture"}
    if tuple(arguments) in versions:
        return offload.CommandOutput(0, versions[tuple(arguments)] + "\n", "")
    if arguments[0] == "git" and arguments[-2:] == ["rev-parse", "HEAD"]:
        return offload.CommandOutput(0, "fixture-commit\n", "")
    assert arguments[0] == "ssh", arguments
    if arguments[-1] == offload.FENCE_HOST_QUERY:
        if request.target != "mac":
            actions.append("reachability")
        return capture_fence_query(arguments[-1])
    if arguments[-1] == "true":
        actions.append("reachability")
        return offload.CommandOutput(0, "", "")
    actions.append("probe")
    if mode == "probe-leftovers":
        return offload.CommandOutput(255, "", "host unavailable")
    prepare_probe_fence(arguments[-1])
    if mode == "probe-refusal":
        (root / (request.target + ".marker")).touch()
        return offload.CommandOutput(75, "linux_startup=unresolved\n", "")
    wire = "free_gib=1000\nhost=linux-build\nnextest=nextest fixture\n"
    wire += "mend=mend fixture\nscripts_commit=fixture-commit\nrc=0\n"
    if mode == "disk":
        wire = wire.replace("free_gib=1000", "free_gib=0")
    if mode == "version-mismatch":
        wire = wire.replace("nextest=nextest fixture", "nextest=nextest other")
    if mode == "after-probe":
        wire += "mac_cleanup=unconfirmed\n"
        (root / (request.target + ".marker")).touch()
    return offload.CommandOutput(0, wire, "")

def copy_scripts(*arguments):
    actions.append("scripts-copy")
    return True

def copy_source(*arguments):
    actions.append("source-copy")
    if isinstance(request.placement, offload.AssignedOffloadRequest):
        source = request.placement.assignment.source_key
        return offload.CopiedTree(offload.KnownTreeKey(source.key) if isinstance(source, execution_identity.SourceKeyKnown)
                                  else offload.UNAVAILABLE_TREE_KEY)
    return offload.CopiedTree(offload.KnownTreeKey("sent-tree"))

def stream(arguments):
    global remote_output, remote_status
    actions.append("run")
    if launch_error is not None:
        with mock.patch.object(subprocess, "Popen", side_effect=launch_error):
            return offload.ProcessController.stream_mac_run(controller, arguments)
    if mode in ("run-refusal", "after-probe"):
        wire, status = "linux_startup=unresolved\n", 75
        (root / (request.target + ".marker")).touch()
    elif mode == "runtime-version-mismatch":
        wire = (request.target + " version mismatch: rustc box=rustc other natedev=rustc fixture; "
                + "nextest box=nextest other natedev=nextest fixture; move the box pins in nixos hosts/linux-build.nix\n"
                + "mac_cleanup=kept\n")
        status = 75
    elif mode == "lost":
        wire, status = "connection lost\n", 255
    else:
        status = 75 if mode == "completed-decline" else 42 if mode == "failed" else 4 if mode == "no-tests" else 0
        cleanup = "unconfirmed" if mode == "run-cleanup-unconfirmed" else "0"
        wire = "mac_exit=" + str(status) + "\nmac_cleanup=" + cleanup + "\n"
        if isinstance(controller.fence, claim_state.FenceRef):
            journal = offload.read_start_fence(controller.fence)
            assert isinstance(journal, claim_state.StartFenceJournal), journal
            command = offload.start_fence_command(journal.fence, settle=True, preparation=journal.preparation)
            wire += run_start_fence(command)

    remote_output, remote_status = wire, status

    class Remote:
        stdout = io.StringIO(wire)
        def wait(self):
            return status
        def poll(self):
            return status

    with mock.patch.object(subprocess, "Popen", return_value=Remote()):
        return offload.ProcessController.stream_mac_run(controller, arguments)

real_run = subprocess.run
def commands(arguments, **options):
    if arguments[0] == "ssh":
        actions.append("cleanup")
        unresolved = mode in ("probe-refusal", "run-refusal", "after-probe")
        count = 2 if mode in ("probe-leftovers", "retained-leftovers", "run-cleanup-unconfirmed") else 0
        fence = ""
        identity = controller.claim_identity
        reference = controller.fence
        if check_start_fence and isinstance(reference, claim_state.FenceRef):
            assert isinstance(identity, claim_state.ClaimIdentity)
            journal = offload.read_start_fence(reference)
            assert isinstance(journal, claim_state.StartFenceJournal), journal
            command = offload.start_fence_command(journal.fence, settle=True, preparation=journal.preparation)
            settles_fence = arguments[-1].startswith(command + " || exit 1\n")
            if mode == "start-fence-interrupted" or launch_error is not None:
                if settles_fence:
                    actions.append("settle-start")
            else:
                assert settles_fence
            if settles_fence and mode not in ("start-fence-interrupted", "start-fence-unconfirmed"):
                fence = run_start_fence(command)
                if mode == "start-journal-unremovable":
                    journal = offload.pending_start_path(request.target, identity)
                    journal.unlink()
                    journal.mkdir()
        elif mode == "start-fence-wrong-path":
            raise AssertionError("Takeover sent a fence command without the original record path")
        elif not unresolved and count == 0 and isinstance(reference, claim_state.FenceRef):
            fence = reference.acknowledgement("settled") + "\n"
        return subprocess.CompletedProcess(arguments, 3 if unresolved else 0,
            "" if unresolved else fence + "leftovers=" + str(count) + "\nrc=0\n", "")
    return real_run(arguments, **options)

real_consume_permission = admission_client.consume_permission
read_authorization_line = admission_client._line

def interrupt_on_closed_authorization(connection, pending, deadline):
    try:
        return read_authorization_line(connection, pending, deadline)
    except admission_client.TransportUnavailable:
        raise offload.OffloadInterrupted(SIGTERM) from None

def consume_permission(permission):
    if mode != "start-fence-interrupted":
        return real_consume_permission(permission)
    # Inject the interrupt at EOF, before the client can reconnect for status.
    with mock.patch.object(admission_client, "_line", side_effect=interrupt_on_closed_authorization):
        return real_consume_permission(permission)

started = time.monotonic()
interrupted_status = 0
with mock.patch.object(controller, "complete_claim", side_effect=complete), \
     mock.patch.object(controller, "capture", side_effect=capture), \
     mock.patch.object(controller, "stream_mac_run", side_effect=stream), \
     mock.patch.object(offload, "copy_tracked_scripts", side_effect=copy_scripts), \
     mock.patch.object(offload, "copy_tree_with_key", side_effect=copy_source), \
     mock.patch.object(offload, "trusted_linux_source", return_value=True), \
     mock.patch.object(admission_client, "consume_permission", side_effect=consume_permission), \
     mock.patch.object(subprocess, "run", side_effect=commands):
    try:
        result = offload.run_offload(controller, config, request, started)
    except offload.OffloadInterrupted as interrupted:
        interrupted_status = 128 + interrupted.signal_number
        result = offload.LostMacRun(offload.elapsed_seconds(started), "interrupted before remote start")
    except IsADirectoryError as error:
        if mode != "start-journal-unremovable":
            raise
        result = offload.LostMacRun(offload.elapsed_seconds(started), str(error))

outcome = root / ("provider-outcome-" + request.target + ".json")
if (isinstance(result, offload.DeclinedMacRun) and not configured_mode.endswith("-once")
        and mode in ("disk", "version-mismatch", "runtime-version-mismatch")):
    write_text_atomic(outcome, json.dumps({"target": request.target, "reason": result.reason}))
release = controller.attempt_evidence.claim_release
if isinstance(release, (offload.ClaimReleaseConfirmed, offload.ClaimReleaseUnconfirmed)):
    exclusion = claim_exclusion_path(root, request.target, release.identity)
    if isinstance(release, offload.ClaimReleaseConfirmed):
        exclusion.unlink(missing_ok=True)
    else:
        scope = adopted_scopes.get(release.identity, controller.claim_scope)
        exclusion.parent.mkdir(exist_ok=True)
        write_text_atomic(exclusion, queue_wire.encode_record(RetainedClaimExclusion(scope)))

status = interrupted_status or offload.write_result(result, request.result, request.last_decline, request.verb,
                                                    request.target, controller.attempt_evidence)
provider_result = request.result.path.read_text() if isinstance(request.result, offload.ResultFile) and not interrupted_status else ""

event = dict(target=request.target, owner=args.claim_owner_pid, slot=controller.attempt_evidence.slot,
             token_held=(Path(os.environ["TEST_TOKEN_RECORD"]).exists()
                         and Path(os.environ["TEST_TOKEN_RECORD"]).read_text().strip() == os.environ.get("BUILDLOG_HOLD_PID", "")),
             pinned=os.environ.get("BUILDLOG_PINNED_TARGET", ""),
             reserved=reserved, same_path=same_path, queued_at=os.environ.get("BUILDLOG_MEM_WAIT_SINCE", ""),
             memory_reservation=os.environ.get("BUILDLOG_MEM_RESERVATION", ""), actions=actions, mode=mode,
             entry_claim_ids=[claim.record["claim_id"] for claim in initial],
             adopted_claim_ids=adopted_claim_ids,
             remaining_claim_ids=[claim.record["claim_id"] for claim in mac_test.read_claims(mac_test.state_paths(request.target))],
             result_type=type(result).__name__, provider_result=provider_result, release_state=type(release).__name__,
             remote_output=remote_output, remote_status=remote_status,
             holder=type(result.holder).__name__ if isinstance(result, offload.BusyMacDecline) else "",
             clock=Path(os.environ["TEST_ROUTING_CLOCK"]).read_text() if os.environ.get("TEST_ROUTING_CLOCK") else "")
with (root / "ssh-events.jsonl").open("a") as output:
    output.write(json.dumps(event) + "\n")
try:
    offload.report_placement_result(controller, request, result)
except (queue_wire.AdmissionUnavailable, OSError, ValueError, ConnectionError) as error:
    print(f"offload: cannot report placement: {error}", file=sys.stderr)
finally:
    try:
        offload.persist_attempt(request, result, controller.attempt_evidence, started)
    except (OSError, ValueError) as error:
        print(f"offload: cannot record the attempt: {error}", file=sys.stderr)
raise SystemExit(status)
'''


class VerifySshTargetsFixture(VerifyMacOffloadFixture):
    admission: AdmissionFixture  # pyright: ignore[reportUninitializedInstanceVariable]
    memory_events_enabled: bool = False
    memory_hook_observer: Callable[[CompletedMemoryHook], bool] | ReleaseMemoryAtWait = RELEASE_MEMORY_AT_WAIT

    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        self.environment.update(fake_memory_readings(self.root))
        self.environment["TEST_SOURCE_REPO"] = str(REPOSITORY)
        # Restore the private wait identity after the gate removes inherited values.
        reader = Path(self.environment["LINT_CONFIG_READER"])
        with reader.open("a") as stream:
            _ = stream.write(r'''
fixture_wait_since=${BUILDLOG_MEM_WAIT_SINCE-}
fixture_wait_first_file=${BUILDLOG_MEM_WAIT_FIRST_FILE-}
source() {
    builtin source "$@"
    local status=$?
    if [[ "$1" == */memory_gate.sh ]]; then
        export BUILDLOG_MEM_WAIT_SINCE=$fixture_wait_since
        export BUILDLOG_MEM_WAIT_FIRST_FILE=$fixture_wait_first_file
        buildlog_memory_python() { "$HOME/.claude/scripts/lib/py" "$@"; }
        if [ -n "${TEST_ADMISSION_CLIENT:-}" ]; then BUILDLOG_MEM_ADMIT_SCRIPT=$TEST_ADMISSION_CLIENT; fi
        unset -f source
    fi
    return "$status"
}
''')
        self.write_executable(self.runner, SSH_RUNNER)
        self.write_executable(self.bin_directory / "git", GIT_STUB.replace(
            "wanted=ls-files", 'wanted=remote\nif has "$@"; then printf "https://example.com/team/hana.git\\n"; exit 0; fi\nwanted=ls-files'))
        _ = (self.root / ".git/config").write_text('[remote "origin"]\nurl=https://example.com/team/hana.git\n')
        self.configure("mac,natedev,linux")
        install_fixed_cargo(self.bin_directory / "cargo")

    @override
    def setUp(self) -> None:
        super().setUp()
        self.admission = caller_admission(self, self.root, self.environment)
        # The interval also bounds admission socket reads. Keep room for scheduling delay at high load.
        self.admission.configure(waiting_pass_seconds=1, running_pass_seconds=0.25)
        self.memory_events_enabled = False
        self.memory_hook_observer = RELEASE_MEMORY_AT_WAIT
        previous = self.admission.server.handle_request

        def observe(request: wire.Request,
                    connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            replies = previous(request, connection)
            if isinstance(request, (wire.PlacementRequest, wire.ConsumePermission, wire.ExecutionReport, wire.ClaimFailure)):
                targets = ({candidate.destination.target for candidate in request.constraints.candidates
                            if candidate.destination.provider != "local"} if isinstance(request, wire.PlacementRequest)
                           else {request.assignment.destination.target})
                event = {"request": json.loads(wire.encode_request(request)),
                         "replies": [json.loads(wire.encode_reply(reply)) for reply in replies],
                         "claims": {target: [held.record for held in caller_provider_controls(self, self.admission).claims(target)]
                                    for target in targets}}
                with (self.runner_state / "queue-events.jsonl").open("a") as output:
                    _ = output.write(json.dumps(event) + "\n")
            return replies

        _ = self.enterContext(mock.patch.object(self.admission.server, "handle_request", side_effect=observe))

    def spare_after_linux_refusal(self) -> AbstractContextManager[None]:
        """Make the spare available after the first provider refuses the step."""
        return self.change_host_after_linux_refusal(
            "spare", before=placement.TemporaryExclusion("starting"), after=placement.AllowedHost())

    @contextmanager
    def change_host_after_linux_refusal(self, target: str, *, before: placement.HostEligibility,
                                        after: placement.HostEligibility) -> Generator[None]:
        """Change private host evidence when Linux reports a refusal."""
        controls = caller_provider_controls(self, self.admission)
        controls.set_host(target, before)
        previous = self.admission.server.handle_request

        def observe(request: wire.Request,
                    connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            replies = previous(request, connection)
            if (isinstance(request, wire.ClaimFailure)
                    or isinstance(request, wire.ExecutionReport)
                    and isinstance(request.outcome, (placement.PrelaunchRefused, placement.AttemptDeclined))):
                if request.assignment.destination.target == "linux":
                    controls.set_host(target, after)
            return replies

        with mock.patch.object(self.admission.server, "handle_request", side_effect=observe):
            yield

    def configure(self, order: str) -> None:
        _ = (self.root / "build_targets.conf").write_text(
            f"targets={order}\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n"
            + "kind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux_target.conf\n"
            + "kind.spare=ssh\nplatform.spare=linux\nsettings.spare=spare.conf\n")
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=off\n")
        for name in ("linux_target", "spare"):
            _ = write_config_fixture(self.root / f"{name}.conf",
                "offload=on\nhost=fake-linux\nci_repo=\nkinds=test,check,doc\nslots=1\nslots.hana=1\n"
                + "devshell=default\nshell_cache=fixture-shells\nshell_timeout_s=2\n"
                + "build_budget_gib=32\ntarget_budget_gib=24\nversion_pin_file=nixos hosts/linux-build.nix\n"
                + "parity.rustc=rustc fixture\nparity.nextest=nextest fixture\n"
                + "parity.mend=mend fixture\nparity.scripts_commit=fixture-commit\n", template="mac_test")
        for target in ("linux", "spare"):
            self.target_state(target).mkdir(parents=True, exist_ok=True)

    def ssh_events(self) -> list[SshRunnerEvent]:
        path = self.runner_state / "ssh-events.jsonl"
        return [cast(SshRunnerEvent, json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def queue_exchanges(self) -> list[QueueExchange]:
        lines = (self.runner_state / "queue-events.jsonl").read_text().rpartition("\n")[0].splitlines()
        exchanges: list[QueueExchange] = []
        for line in lines:
            event = cast(dict[str, object], json.loads(line))
            request = wire.parse_request(json.dumps(event["request"]))
            replies = tuple(wire.parse_reply(json.dumps(reply), request) for reply in cast(list[object], event["replies"]))
            exchanges.append(QueueExchange(request, replies))
        return exchanges

    def queue_requests(self) -> list[wire.Request]:
        return [exchange.request for exchange in self.queue_exchanges()]

    def attempts(self) -> list[dict[str, object]]:
        rows = [cast(dict[str, object], json.loads(line))
                for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines()
                if cast(dict[str, object], json.loads(line)).get("kind") == "ssh_attempt"]
        return sorted(rows, key=lambda row: cast(str, row["at"]))

    def enable_mac(self) -> None:
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=on\nlinux_only.hana=\nlinux_only.case=\n")

    def release_memory_after_hook(self, observe: Callable[[CompletedMemoryHook], bool] | ReleaseMemoryAtWait = RELEASE_MEMORY_AT_WAIT,
                                  *, after_attempts: int = 0) -> None:
        _ = self.hold_memory()
        self.memory_events_enabled = True
        self.memory_hook_observer = observe
        directory = self.root / "observed-client"
        directory.mkdir(exist_ok=True)
        client = directory / "admission_client.py"
        self.write_executable(client, QUEUED_MEMORY_OBSERVER)
        self.environment.update(TEST_ADMISSION_CLIENT=str(client), TEST_OBSERVE_AFTER_ATTEMPTS=str(after_attempts))
        os.mkfifo(self.runner_state / "memory-hook-ack")

    @override
    def verify(self, *arguments: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if not self.memory_events_enabled:
            return super().verify(*arguments, extra=extra)
        acknowledgment = os.open(self.runner_state / "memory-hook-ack", os.O_RDWR)
        try:
            process = self.start_verify(*arguments, extra=extra)
            with self.keep_verify_failure(process):
                sequence = 0
                while True:
                    sequence += 1
                    event_path = self.runner_state / f"memory-hook-{sequence}"
                    self.wait_until(lambda: event_path.exists() or process.poll() is not None,
                                    str(event_path), (process,))
                    if not event_path.exists():
                        with self.admission.loop_turn():
                            authorized = any(isinstance(saved.permission, placement.PermissionConsumed)
                                             and saved.assignment.destination.provider != "local"
                                             for saved in self.admission.server.assignments.values())
                        if authorized and isinstance(self.memory_hook_observer, ReleaseMemoryAtWait):
                            break
                        self.wait_for_path(event_path, (process,))
                    event = cast(CompletedMemoryHook, json.loads(event_path.read_text()))
                    release = (True if isinstance(self.memory_hook_observer, ReleaseMemoryAtWait)
                               else self.memory_hook_observer(event))
                    if release and not event["left"]:
                        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 268435456 kB\n")
                    settled = release or event["left"]
                    if settled:
                        # Admission can wait after memory is free. Its later hooks do not wait for this test.
                        (self.runner_state / "memory-hook-settled").touch()
                    _ = os.write(acknowledgment, b"continue")
                    if settled:
                        break
            return self.finish_verify(process)
        finally:
            os.close(acknowledgment)

    def target_state(self, target: str) -> Path:
        return self.home / ".local/state/build-targets" / target


    def first_wait(self) -> tuple[Path, dict[str, str]]:
        path = self.root / "first-wait"
        _ = path.write_text("100.25\n")
        return path, {"BUILDLOG_MEM_WAIT_SINCE": "100.25", "BUILDLOG_MEM_WAIT_FIRST_FILE": str(path)}


# A wrapper with a top-level main runs inside the verify helper, as build_targets.py does.
# Then the real build_targets.py loads once for each helper.
BUILD_TARGETS_STATUS = r'''
import importlib.util
from types import ModuleType

REAL_BUILD_TARGETS: dict[Path, ModuleType] = {}


def build_targets_status() -> object:
    path = source / "scripts/build_targets/build_targets.py"
    if path not in REAL_BUILD_TARGETS:
        specification = importlib.util.spec_from_file_location("fixture_build_targets", path)
        assert specification is not None and specification.loader is not None
        module = importlib.util.module_from_spec(specification)
        sys.modules[specification.name] = module
        specification.loader.exec_module(module)
        REAL_BUILD_TARGETS[path] = module
    try:
        return REAL_BUILD_TARGETS[path].main() or 0
    except SystemExit as result:
        return result.code or 0
'''
