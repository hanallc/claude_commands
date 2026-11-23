"""Placement preserves attempt identity and grants one execution authorization."""
from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest import mock
from typing import TextIO, cast, override

from scripts.build_queue import execution_identity
from scripts.build_queue.placement import MAC_LINUX_NO_ANSWER
from scripts.aws_target import hana_placement as hana
from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_queue import admission_protocol as wire, admission_records as records, admission_service as service, placement
from scripts.build_queue import admission_client as client, mac_run, queue_snapshot
from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue import limits_view, memory_feed
from scripts.build_queue.admission_test_support import AdmissionFixture, FixedSystemReader, ObservedAdmissionServer, only_admitted, GIB
from scripts.buildlog import store
from scripts.mac_test.claim_state import RepositoryScope, WholeMacScope
from scripts.mac_test import claim_state, eligibility, mac_test, offload
from scripts.lint import memory_test_support
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE, write_pressure_reading
from scripts.lint import memory_admit as gate
from scripts.message.unit_labels import UnitLabels
from scripts.lib import conf
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.build_queue.queue_fixture import write_memory_config, write_process
from scripts.build_queue.test_snapshot_codec import OLDER_CLIENTS, pinned_snapshot_reader
from scripts.build_targets.target_config import ANY_ELIGIBLE_TARGET, LOCAL_CALLER, PinnedTarget


SUBMITTING_SESSION = OwningSession("submitting-session")
ALLOWED_HOST = placement.AllowedHost()
HANA_REPOSITORY = "https://github.com/hanallc/hana"
FIXTURE_MAC_SCOPE = RepositoryScope("hana")


# The mac_test settings these tests depend on. A tune of the repository settings must not change a test.
MAC_TEST_PINS = "offload=on\nprobe_timeout_s=15\nlinux_only.hana=hana_video,hana_clerestory\n"


def write_config_fixture(path: Path, overrides: str = "", *, template: str | None = None) -> int:
    """Write a fixture settings file. A new mac_test file gets the pins in place of the repository values."""
    pins = MAC_TEST_PINS if (template or path.stem) == "mac_test" and not path.exists() else ""
    return memory_test_support.write_config_fixture(path, pins + overrides, template=template)


class PlacementFixture(AdmissionFixture):
    """Supply private provider evidence across each service restart."""

    read_host_evidence: Callable[[placement.HostEvidenceKey], placement.HostEvidence]

    def __init__(self) -> None:
        self.provider_evidence: dict[placement.HostEvidenceKey, placement.HostEvidence] = {}
        self.settings_pinned: bool = False
        super().__init__()

    @override
    def read_provider(self, key: placement.HostEvidenceKey) -> placement.HostEvidence:
        if key.destination.provider == "local" and key not in self.provider_evidence:
            return placement.FreshHostEvidence(key, self.clock.now(), "", placement.AllowedHost())
        return self.provider_evidence.get(key, placement.HostEvidenceUnavailable(key, self.clock.now(), "not read"))

    @override
    def new_server(self, *, start: bool = True) -> ObservedAdmissionServer:
        settings = conf.path("mac_test")
        if settings.parent != self.root:
            raise AssertionError(f"a placement test must not read the live settings: {settings}")
        if not self.settings_pinned:
            settings.unlink(missing_ok=True)
            _ = write_config_fixture(settings)
            self.settings_pinned = True
        server = super().new_server(start=False)
        self.read_host_evidence = server.read_provider_evidence
        server.host_probe = self.read_provider
        if start:
            server.start()
        return server


def placement_request(fixture: AdmissionFixture, *, session: placement.SubmittingSession = SUBMITTING_SESSION,
                      since: float | None = None, scope: RepositoryScope = FIXTURE_MAC_SCOPE) -> wire.PlacementRequest:
    local = fixture.request(since=since)
    local = replace(local, run=wire.RunIdentity(local.owner.pid, local.owner.start_time))
    destination = placement.PlacementDestination("mac", "mac", "fixture-mac", scope)
    constraints = placement.PlacementConstraints(execution_identity.SourceKeyKnown("source-a"), ("cargo", "nextest", "run"), "hana", "hana", session,
                                                 (placement.PlacementCandidate(destination),))
    owner = placement.SubmittingOwner(fixture.server.host, local.owner.pid, local.owner.start_time)
    return wire.PlacementRequest(local.episode, owner, local.since, local.profile, constraints,
                                 identity=local.identity, run=local.run,
                                 local=wire.LocalAdmissionContext(constraints.command, local.cwd, local.sidecar,
                                     local.call, local.test_width, local.fresh_test),
                                 code_version=local.code_version)


def mac_claim_request(fixture: AdmissionFixture) -> tuple[wire.PlacementRequest, Path]:
    """Give a provider claim a private checkout that matches its assignment."""
    worktree = fixture.root / "hana"
    _ = subprocess.run(["git", "init", "-q", str(worktree)],
                       capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)
    _ = subprocess.run(["git", "-C", str(worktree), "remote", "add", "origin", HANA_REPOSITORY],
                       capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)
    return placement_request(fixture, scope=RepositoryScope("hana", HANA_REPOSITORY)), worktree


def coordinator_holder(assignment: placement.PlacementAssignment) -> placement.ClaimHolderIdentity:
    return placement.ClaimHolderIdentity(assignment.owner.host, assignment.owner.pid, assignment.owner.start_time)


def consume_permission(request_id: str, assignment: placement.PlacementAssignment,
                       claim: placement.ProviderClaimReference) -> wire.ConsumePermission:
    return wire.ConsumePermission(request_id, assignment, claim, holder=coordinator_holder(assignment))


def local_candidate(fixture: AdmissionFixture) -> placement.PlacementCandidate:
    return placement.PlacementCandidate(placement.PlacementDestination(
        "local", "natedev", fixture.server.host, WholeMacScope()))


def mac_linux_fixture(fixture: AdmissionFixture, *, enabled: str = "on", registered: bool = True) -> Path:
    names = "mac-linux,natedev" if registered else "natedev"
    _ = (fixture.root / "build_targets.conf").write_text(
        f"targets={names}\nkind.natedev=local\nplatform.natedev=linux\n"
        + "kind.mac-linux=ssh\nplatform.mac-linux=linux\nsettings.mac-linux=mac_linux.conf\n"
        + f"enabled.mac-linux={enabled}\nunreachable_wait_seconds=600\n")
    _ = (fixture.root / "mac_linux.conf").write_text(
        "ssh.hops=fixture-mac fixture-guest\nguest.user=fixture\ntest.jobs=8\n"
        + "run.time_limit_seconds=7200\n")
    backoff = fixture.root / "target-state" / "mac-linux" / "unreachable_until"
    backoff.parent.mkdir(parents=True, exist_ok=True)
    return backoff


def mac_linux_candidate() -> placement.PlacementCandidate:
    return placement.PlacementCandidate(placement.PlacementDestination(
        "ssh", "mac-linux", "fixture-guest", RepositoryScope("claude", slot=1), 1))


def publish_candidate(fixture: PlacementFixture, request: wire.PlacementRequest,
                      eligibility: placement.HostEligibility = ALLOWED_HOST,
                      claims: tuple[placement.AdoptedProviderClaim, ...] = ()) -> placement.FreshHostEvidence:
    destination = request.constraints.candidates[0].destination
    key = fixture.server.host_evidence_key(request, destination)
    evidence = placement.FreshHostEvidence(key, fixture.clock.now(), "host-generation-a", eligibility, claims)
    fixture.provider_evidence[key] = evidence
    fixture.server.publish_host_evidence(evidence)
    return evidence


def mac_linux_and_natedev_request(fixture: PlacementFixture, *, natedev: bool = True) -> wire.PlacementRequest:
    """Name the Mac Linux guest first and natedev second, as a named gate does."""
    _ = (fixture.root / "build_targets.conf").write_text(
        "targets=mac-linux,natedev\nkind.mac-linux=ssh\nplatform.mac-linux=linux\nsettings.mac-linux=guest.conf\n"
        + "enabled.mac-linux=on\nkind.natedev=local\nplatform.natedev=linux\n")
    _ = write_config_fixture(fixture.root / "guest.conf", template="mac_test")
    guest = mac_linux_candidate()
    request = placement_request(fixture)
    candidates = (guest, local_candidate(fixture)) if natedev else (guest,)
    return replace(request, constraints=replace(request.constraints, candidates=candidates))


def assignment_for(fixture: AdmissionFixture, request: wire.PlacementRequest) -> placement.PlacementAssignment:
    reply, = fixture.server.handle_request(request)
    if not isinstance(reply, wire.Assignment):
        raise AssertionError(f"placement did not assign the eligible destination: {reply!r}")
    return reply.assignment


def guest_gate(fixture: PlacementFixture, assignment: placement.PlacementAssignment) -> wire.Owner:
    """Start a fixture gate process as a child of the placement owner, as copies.py starts the gate."""
    process = fixture.proc.spawn(used=0)
    write_process(fixture.proc.path, process.pid, parent=assignment.owner.pid, start=process.start_time)
    return process


def consume_guest_permission(fixture: PlacementFixture, assignment: placement.PlacementAssignment,
                             gate_process: wire.Owner, *, held: bool | Exception = True) -> list[wire.Reply]:
    """Send the guest gate's consume. The local Mac claim state gives `held` for the gate's claim."""
    destination = assignment.destination
    claim = placement.ProviderClaimReference(destination.provider, destination.target, destination.scope,
        mac_test.MAC_LINUX_CLAIM_ID_PREFIX + "fixture", assignment.episode, assignment.attempt)
    holder = placement.ClaimHolderIdentity(assignment.owner.host, gate_process.pid, gate_process.start_time)
    with mock.patch.object(mac_run, "claim_held", return_value=held,
                           side_effect=held if isinstance(held, Exception) else None):
        return fixture.server.handle_request(wire.ConsumePermission("guest-launch", assignment, claim, holder))


def hana_policy_request(fixture: PlacementFixture) -> tuple[wire.PlacementRequest, hana.HanaControlTransport]:
    repository = fixture.root / "hana"
    repository.mkdir()
    settings = fixture.root / "hana_aws.conf"
    _ = write_config_fixture(settings, f"enabled=on\nrepository_identity={HANA_REPOSITORY}\n")
    _ = (fixture.root / "build_targets.conf").write_text(
        "targets=hana-aws\nkind.hana-aws=ssh\nplatform.hana-aws=linux\nsettings.hana-aws=hana_aws.conf\n")
    scope = RepositoryScope("hana", HANA_REPOSITORY, 1)
    destination = placement.PlacementDestination("ssh", "hana-aws", "aws-hana", scope, 1)
    request = placement_request(fixture)
    assert isinstance(request.profile, records.StepProfile)
    request = replace(request, profile=replace(request.profile, step="check"), constraints=replace(
        request.constraints, command=("cargo", "check"), command_kind="check", repository=str(repository),
        candidates=(placement.PlacementCandidate(destination),)))
    return request, hana.HanaControlTransport(settings, destination.host)


def report_hana_refusal(fixture: PlacementFixture, transport: hana.HanaControlTransport,
                        assignment: placement.PlacementAssignment, *, before_report: Callable[[], object]) -> None:
    controller = offload.ProcessController()
    request = offload.OffloadRequest(fixture.root / "hana", "hana", "call", offload.NO_RESULT_FILE, (),
        owner="fixture-adapter", holder_session=claim_state.session_id(assignment.session), verb="check",
        kind="check", target=assignment.destination.target, placement=offload.AssignedOffloadRequest(assignment))
    with mock.patch.object(eligibility, "repository_identity",
                           return_value=eligibility.KnownRepository("hana", HANA_REPOSITORY)):
        refused = offload.run_hana_offload(controller, transport, request, time.monotonic())
    assert isinstance(refused, offload.DeclinedMacRun) and (refused.reason, refused.detail) == ("off", "off"), refused
    _ = before_report()
    with fixture.serving():
        offload.report_placement_result(controller, request, refused)


def adopt_assignment(fixture: PlacementFixture, request: wire.PlacementRequest,
                     assignment: placement.PlacementAssignment) -> placement.ProviderClaimReference:
    destination = assignment.destination
    reference = placement.ProviderClaimReference(destination.provider, destination.target, destination.scope,
                                                  f"claim-{assignment.attempt}", assignment.episode, assignment.attempt)
    adopted = placement.AdoptedProviderClaim(reference, coordinator_holder(assignment), assignment.session,
                                             destination.host, "host-generation-a")
    _ = publish_candidate(fixture, request, claims=(adopted,))
    return reference


def with_cargo_folder(request: wire.PlacementRequest, path: str) -> str:
    """Write a folder tag that the original service also accepts."""
    envelope = cast(dict[str, object], json.loads(wire.encode_request(request)))
    data = cast(dict[str, object], envelope["data"])
    constraints = cast(dict[str, object], data["constraints"])
    constraints["cargo_folder"] = {"kind": "CargoFolder", "path": path}
    return json.dumps(envelope) + "\n"


def local_cargo_request(fixture: PlacementFixture) -> wire.PlacementRequest:
    request = placement_request(fixture)
    return replace(request, constraints=replace(request.constraints, candidates=(local_candidate(fixture),)))


def scoped_cargo_request(fixture: PlacementFixture, folder: placement.CargoFolder) -> wire.PlacementRequest:
    request = local_cargo_request(fixture)
    owner = wire.Owner(request.owner.pid, request.owner.start_time)
    member = fixture.request().owner
    sidecar = fixture.scope(owner, members=(member.pid,))
    assert isinstance(request.local, wire.LocalAdmissionContext)
    return replace(request, local=replace(request.local, sidecar=str(sidecar)),
                   constraints=replace(request.constraints, cargo_folder=folder))


class CargoFolderTests(unittest.TestCase):
    def test_the_same_local_folder_waits_without_memory_and_keeps_its_place(self) -> None:
        for prior_need in (0, 4 * GIB):
            with self.subTest(prior_need=prior_need), PlacementFixture() as fixture:
                first = local_cargo_request(fixture)
                folder = str(fixture.root / "target")
                granted, = fixture.connect(with_cargo_folder(first, folder)).replies()
                assert isinstance(granted, wire.LocalPlacementGranted)
                fixture.allow_next_start()
                second = local_cargo_request(fixture)
                if prior_need:
                    run = fixture.run_owner()
                    _ = fixture.finish_step(fixture.request(need=prior_need, run=run))
                    fixture.allow_next_start()
                    second = replace(second, run=run)
                second_line = with_cargo_folder(second, folder)
                waiting, = fixture.connect(second_line).replies()
                self.assertIsInstance(waiting, wire.PlacementWait)
                assert isinstance(waiting, wire.PlacementWait)
                assert isinstance(waiting.wait, wire.Wait)
                self.assertEqual(waiting.wait.text, "waits for the build in the same Cargo folder")
                self.assertEqual(waiting.position, 1)
                if prior_need:
                    assert isinstance(second.run, wire.RunIdentity)
                    view, = fixture.server.handle_request(wire.ViewRequest())
                    assert isinstance(view, wire.ViewReply)
                    owner = records.run_owner(second.run.pid, second.run.start_time)
                    inputs = view.reading.explanation.decided_from
                    self.assertEqual(sum(memory.held for memory in gate.run_memories(inputs)
                                         if memory.owner == owner), 0)
                self.assertEqual(set(fixture.caller_reservations()), {first.episode})
                fixture.clock.advance(1)
                third = local_cargo_request(fixture)
                third_line = with_cargo_folder(third, str(fixture.root / "other-target"))
                behind, = fixture.connect(third_line).replies()
                assert isinstance(behind, wire.PlacementWait)
                self.assertEqual(behind.position, 2)
                self.assertEqual(fixture.queue_pids(), [second.owner.pid, third.owner.pid])
                self.assertEqual(set(fixture.caller_reservations()), {first.episode})

                fixture.release(granted.grant)
                _ = fixture.run_pass()
                resumed, = fixture.connect(second_line).replies()
                self.assertIsInstance(resumed, wire.LocalPlacementGranted)
                self.assertEqual(set(fixture.caller_reservations()), {second.episode})

    def test_cargo_folder_stays_claimed_until_the_orphaned_scope_empties(self) -> None:
        with PlacementFixture() as fixture:
            folder = placement.CargoFolder(fixture.root / "target")
            holder = scoped_cargo_request(fixture, folder)
            grant, = fixture.connect(holder).replies()
            assert isinstance(grant, wire.LocalPlacementGranted)
            key = execution_identity.attempt_key(holder.episode, grant.assignment.attempt)
            fixture.end_owner(wire.Owner(holder.owner.pid, holder.owner.start_time))
            _ = fixture.run_pass()
            self.assertIsInstance(fixture.server.assignments[key].state, placement.ClosedAssignment)
            self.assertNotIn(holder.episode, fixture.server.current_assignments)
            self.assertIn(holder.episode, fixture.server.reservations)
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.inputs.occupied_cargo_folders, frozenset({folder}))

            config = fixture.server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            fixture.clock.advance(config.ended_placement_seconds + 1)
            _ = fixture.run_pass()
            self.assertNotIn(holder.episode, fixture.server.placement_requests)
            self.assertNotIn(key, fixture.server.assignments)
            fixture.restart()
            self.assertIn(holder.episode, fixture.server.reservations)

            run = fixture.run_owner()
            _ = fixture.finish_step(fixture.request(need=4 * GIB, run=run))
            fixture.allow_next_start()
            successor = replace(local_cargo_request(fixture), run=run)
            successor = replace(successor, constraints=replace(successor.constraints, cargo_folder=folder))
            waiting, = fixture.connect(successor).replies()
            assert isinstance(waiting, wire.PlacementWait)
            assert isinstance(waiting.wait, wire.Wait)
            assert isinstance(waiting.wait.reason, records.HeadAdmissionStatus)
            self.assertIsInstance(waiting.wait.reason.cause, placement.CargoFolderWait)
            self.assertEqual(waiting.position, 1)
            view, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(view, wire.ViewReply)
            run_key = records.run_owner(run.pid, run.start_time)
            self.assertEqual(sum(memory.held for memory in gate.run_memories(view.reading.explanation.decided_from)
                                 if memory.owner == run_key), 0)

            assert isinstance(holder.local, wire.LocalAdmissionContext)
            scope = Path(Path(holder.local.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.caller_reservations()), {successor.episode})
            resumed, = fixture.connect(successor).replies()
            self.assertIsInstance(resumed, wire.LocalPlacementGranted)

    def test_restart_recovers_a_legacy_cargo_folder_once(self) -> None:
        for evidence in ("current", "closed", "pruned"):
            with self.subTest(evidence=evidence), PlacementFixture() as fixture:
                folder = placement.CargoFolder(fixture.root / "target")
                holder = scoped_cargo_request(fixture, folder)
                grant, = fixture.connect(holder).replies()
                assert isinstance(grant, wire.LocalPlacementGranted)
                if evidence != "current":
                    fixture.end_owner(wire.Owner(holder.owner.pid, holder.owner.start_time))
                    _ = fixture.run_pass()
                    self.assertNotIn(holder.episode, fixture.server.current_assignments)
                if evidence == "pruned":
                    config = fixture.server.last_usable_configuration
                    assert isinstance(config, records.MemoryConfig)
                    fixture.clock.advance(config.ended_placement_seconds + 1)
                    _ = fixture.run_pass()
                    self.assertNotIn(holder.episode, fixture.server.placement_requests)

                path = fixture.state_dir / "state.json"
                raw_state = cast(dict[str, object], json.loads(path.read_text()))
                raw_reservations = cast(dict[str, dict[str, object]], raw_state["reservations"])
                del raw_reservations[holder.episode]["cargo_folder"]
                _ = path.write_text(json.dumps(raw_state))
                with redirect_stderr(io.StringIO()) as warnings:
                    fixture.restart()
                expected = placement.NoCargoFolder() if evidence == "pruned" else folder
                self.assertEqual(fixture.server.reservations[holder.episode].cargo_folder, expected)
                self.assertEqual(service.read_state(fixture.state_dir).reservations[holder.episode].cargo_folder, expected)
                saved = cast(dict[str, object], json.loads(path.read_text()))
                self.assertEqual(cast(dict[str, dict[str, object]], saved["reservations"])[holder.episode]["cargo_folder"],
                                 codec.snapshot_value(expected))
                message = f"cannot recover the Cargo folder for reservation {grant.grant.reservation}"
                self.assertEqual(warnings.getvalue().count(message), int(evidence == "pruned"))
                with redirect_stderr(io.StringIO()) as warnings:
                    fixture.restart()
                self.assertNotIn(message, warnings.getvalue())

    def test_distinct_local_folders_can_run_together(self) -> None:
        with PlacementFixture() as fixture:
            requests = (local_cargo_request(fixture), local_cargo_request(fixture))
            for number, request in enumerate(requests):
                path = str(fixture.root / f"target-{number}")
                granted, = fixture.connect(with_cargo_folder(request, path)).replies()
                self.assertIsInstance(granted, wire.LocalPlacementGranted)
                saved = fixture.server.placement_requests[request.episode]
                self.assertEqual(cast(dict[str, object], codec.snapshot_value(saved.constraints)).get("cargo_folder"),
                                 {"kind": "CargoFolder", "path": path})
                fixture.allow_next_start()
            self.assertEqual(set(fixture.caller_reservations()), {request.episode for request in requests})

    def test_the_snapshot_records_two_occupied_cargo_folders(self) -> None:
        with PlacementFixture() as fixture:
            folders = [fixture.root / "target-b", fixture.root / "target-a"]
            for folder in folders:
                request = local_cargo_request(fixture)
                reply, = fixture.connect(with_cargo_folder(request, str(folder))).replies()
                self.assertIsInstance(reply, wire.LocalPlacementGranted)
                fixture.allow_next_start()
            waiting = local_cargo_request(fixture)
            reply, = fixture.server.handle_request(replace(waiting, constraints=replace(
                waiting.constraints, cargo_folder=placement.CargoFolder(folders[0]))))
            self.assertIsInstance(reply, wire.PlacementWait)
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.inputs.occupied_cargo_folders,
                             frozenset(placement.CargoFolder(folder) for folder in folders))

    def test_an_older_waiter_keeps_the_freed_folder_before_a_continuation(self) -> None:
        with PlacementFixture() as fixture:
            folder = str(fixture.root / "target")
            holder = local_cargo_request(fixture)
            active, = fixture.connect(with_cargo_folder(holder, folder)).replies()
            assert isinstance(active, wire.LocalPlacementGranted)
            fixture.allow_next_start()
            run = fixture.run_owner()
            prior = fixture.request(run=run)
            prior_grant = only_admitted(fixture.server.handle_request(prior))
            fixture.allow_next_start()
            older = replace(local_cargo_request(fixture), since=prior.since - 1)
            reply, = fixture.connect(with_cargo_folder(older, folder)).replies()
            self.assertIsInstance(reply, wire.PlacementWait)
            later = replace(local_cargo_request(fixture), run=run)
            reply, = fixture.connect(with_cargo_folder(later, folder)).replies()
            self.assertIsInstance(reply, wire.PlacementWait)
            self.assertEqual(fixture.queue_pids(), [older.owner.pid, later.owner.pid])

            fixture.release(active.grant)
            fixture.release(prior_grant)
            _ = fixture.run_pass()

            self.assertEqual(set(fixture.caller_reservations()), {older.episode})
            self.assertEqual(set(fixture.server.waiters), {later.episode})

    def test_a_remote_grant_preserves_the_later_local_continuation(self) -> None:
        with PlacementFixture() as fixture:
            folder = str(fixture.root / "target")
            run = fixture.run_owner()
            prior = fixture.request(run=run)
            prior_grant = only_admitted(fixture.server.handle_request(prior))
            remote = placement_request(fixture, since=prior.since - 1)
            _ = publish_candidate(fixture, remote, placement.TemporaryExclusion("busy"))
            older = replace(remote, constraints=replace(remote.constraints,
                candidates=(*remote.constraints.candidates, local_candidate(fixture))))
            older_line = with_cargo_folder(older, folder)
            reply, = fixture.connect(older_line).replies()
            self.assertIsInstance(reply, wire.PlacementWait)
            later = replace(local_cargo_request(fixture), run=run)
            later_line = with_cargo_folder(later, folder)
            reply, = fixture.connect(later_line).replies()
            self.assertIsInstance(reply, wire.PlacementWait)

            fixture.release(prior_grant)
            _ = publish_candidate(fixture, remote)
            _ = fixture.run_pass()

            assigned, = fixture.connect(older_line).replies()
            assert isinstance(assigned, wire.Assignment)
            self.assertEqual(assigned.assignment.destination, remote.constraints.candidates[0].destination)
            self.assertEqual(set(fixture.caller_reservations()), set())

            claim = adopt_assignment(fixture, older, assigned.assignment)
            authorized, = fixture.connect(consume_permission("remote-authorized", assigned.assignment, claim)).replies()
            self.assertIsInstance(authorized, wire.FirstAuthorization)
            execution = execution_identity.ProcessExecution(assigned.assignment.destination.host, "host-generation-a", 700001, "start")
            _ = fixture.connect(wire.ExecutionReport("remote-started", assigned.assignment, claim,
                execution_identity.ObservedExecution(execution), placement.AttemptRunning())).replies()
            fixture.allow_next_start()
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.caller_reservations()), {later.episode})

    def test_a_symlink_and_its_target_share_the_local_folder(self) -> None:
        with PlacementFixture() as fixture:
            target = fixture.root / "target"
            target.mkdir()
            alias = fixture.root / "alias"
            alias.symlink_to(target, target_is_directory=True)
            first = local_cargo_request(fixture)
            active, = fixture.connect(with_cargo_folder(first, str(alias))).replies()
            self.assertIsInstance(active, wire.LocalPlacementGranted)
            fixture.allow_next_start()
            second = local_cargo_request(fixture)
            reply, = fixture.connect(with_cargo_folder(second, str(target))).replies()
            self.assertIsInstance(reply, wire.PlacementWait)
            self.assertEqual(set(fixture.caller_reservations()), {first.episode})

    def test_wait_output_names_the_cargo_folder_block_at_the_head(self) -> None:
        with PlacementFixture() as fixture:
            folder = str(fixture.root / "target")
            first = local_cargo_request(fixture)
            active, = fixture.connect(with_cargo_folder(first, folder)).replies()
            self.assertIsInstance(active, wire.LocalPlacementGranted)
            fixture.allow_next_start()
            second = local_cargo_request(fixture)
            reply, = fixture.connect(with_cargo_folder(second, folder)).replies()
            assert isinstance(reply, wire.PlacementWait)
            assert isinstance(reply.wait, wire.Wait)
            with redirect_stderr(io.StringIO()) as output:
                client.WaitOutput()(reply.wait)
            self.assertIn("head: waits for the build in the same Cargo folder", output.getvalue())

    def test_view_releases_the_folder_before_the_next_pass(self) -> None:
        with PlacementFixture() as fixture:
            folder = str(fixture.root / "target")
            first = local_cargo_request(fixture)
            active, = fixture.connect(with_cargo_folder(first, folder)).replies()
            assert isinstance(active, wire.LocalPlacementGranted)
            fixture.allow_next_start()
            second = local_cargo_request(fixture)
            reply, = fixture.connect(with_cargo_folder(second, folder)).replies()
            self.assertIsInstance(reply, wire.PlacementWait)

            with mock.patch.object(fixture.server, "run_pass", side_effect=AssertionError("unexpected full pass")):
                fixture.release(active.grant)
                viewed, = fixture.server.handle_request(wire.ViewRequest())

            assert isinstance(viewed, wire.ViewReply)
            head = viewed.reading.explanation.head
            assert isinstance(head, records.HeadExplanation)
            self.assertEqual(head.waiter.pid, second.owner.pid)
            self.assertNotIsInstance(head.cause, placement.CargoFolderWait)

    def test_mixed_request_uses_a_free_remote_slot_when_the_local_folder_is_busy(self) -> None:
        for another_remote_step in (False, True):
            with self.subTest(another_remote_step=another_remote_step), PlacementFixture() as fixture:
                local = local_cargo_request(fixture)
                folder = str(fixture.root / "target")
                granted, = fixture.connect(with_cargo_folder(local, folder)).replies()
                assert isinstance(granted, wire.LocalPlacementGranted)
                fixture.allow_next_start()

                older = local_cargo_request(fixture)
                waiting, = fixture.connect(with_cargo_folder(older, folder)).replies()
                assert isinstance(waiting, wire.PlacementWait)
                assert isinstance(waiting.wait, wire.Wait)
                self.assertEqual(waiting.wait.text, "waits for the build in the same Cargo folder")
                fixture.clock.advance(1)

                remote = placement_request(fixture)
                if another_remote_step:
                    other = placement_request(fixture)
                    destination = replace(other.constraints.candidates[0].destination, scope=RepositoryScope("other"))
                    other = replace(other, constraints=replace(other.constraints,
                        candidates=(placement.PlacementCandidate(destination),)))
                    _ = publish_candidate(fixture, other)
                    other_reply, = fixture.connect(with_cargo_folder(other, folder)).replies()
                    assert isinstance(other_reply, wire.Assignment)

                _ = publish_candidate(fixture, remote)
                mixed = replace(remote, constraints=replace(remote.constraints,
                    candidates=(local_candidate(fixture), *remote.constraints.candidates)))
                reply, = fixture.connect(with_cargo_folder(mixed, folder)).replies()
                self.assertIsInstance(reply, wire.Assignment)
                assert isinstance(reply, wire.Assignment)
                self.assertEqual(reply.assignment.destination, remote.constraints.candidates[0].destination)
                self.assertEqual(set(fixture.caller_reservations()), {local.episode})
                self.assertEqual(fixture.queue_pids(), [older.owner.pid, mixed.owner.pid])

    def test_relative_folder_is_refused_and_an_older_request_still_runs(self) -> None:
        with PlacementFixture() as fixture:
            request = local_cargo_request(fixture)
            malformed = with_cargo_folder(request, "target")
            with self.assertRaises(ValueError):
                _ = wire.parse_request(malformed)
            refused, = fixture.connect(malformed).replies()
            self.assertIsInstance(refused, wire.Refused)

            envelope = cast(dict[str, object], json.loads(wire.encode_request(request)))
            data = cast(dict[str, object], envelope["data"])
            constraints = cast(dict[str, object], data["constraints"])
            _ = constraints.pop("cargo_folder", None)
            legacy = json.dumps(envelope) + "\n"
            decoded = wire.parse_request(legacy)
            self.assertEqual(decoded, request)
            assert isinstance(decoded, wire.PlacementRequest)
            self.assertEqual(cast(dict[str, object], codec.snapshot_value(decoded.constraints)).get("cargo_folder"),
                             {"kind": "NoCargoFolder"})
            granted, = fixture.connect(legacy).replies()
            self.assertIsInstance(granted, wire.LocalPlacementGranted)

    def test_wire_and_snapshot_keep_both_folder_tags(self) -> None:
        with PlacementFixture() as fixture:
            request = local_cargo_request(fixture)
            folder = fixture.root / "target"
            for line, tagged in ((wire.encode_request(request), {"kind": "NoCargoFolder"}),
                                 (with_cargo_folder(request, str(folder)), {"kind": "CargoFolder", "path": str(folder)})):
                with self.subTest(tag=tagged["kind"]):
                    decoded = wire.parse_request(line)
                    assert isinstance(decoded, wire.PlacementRequest)
                    snapshot = cast(dict[str, object], codec.snapshot_value(decoded.constraints))
                    self.assertEqual(snapshot.get("cargo_folder"), tagged)
                    self.assertEqual(codec.parse_snapshot_value(snapshot, placement.PlacementConstraints), decoded.constraints)
                    self.assertEqual(wire.parse_request(wire.encode_request(decoded)), decoded)


class PlacementTests(unittest.TestCase):
    def test_a_tune_of_the_repository_settings_reaches_no_placement_fixture(self) -> None:
        original = cast(Callable[[str], Path], getattr(memory_test_support, "_template"))
        repository = original("mac_test.conf").read_text().splitlines()
        tunes = {"offload": ("on", "off"), "probe_timeout_s": ("15", "16"),
                 "linux_only.hana": ("hana_video,hana_clerestory", "hana_video")}
        readings: dict[str, list[dict[str, str]]] = {}
        with TemporaryDirectory() as folder:
            for tune in ("pinned", "tuned"):
                values = {key: pair[tune == "tuned"] for key, pair in tunes.items()}
                tuned = Path(folder) / tune / "mac_test.conf"
                tuned.parent.mkdir()
                _ = tuned.write_text("".join(
                    f"{key}={values[key]}\n" if (key := line.partition("=")[0]) in values else line + "\n"
                    for line in repository))

                def template(name: str, tuned: Path = tuned) -> Path:
                    return tuned if name == tuned.name else original(name)

                with mock.patch.object(memory_test_support, "_template", template), PlacementFixture() as fixture:
                    settings, _, _ = self.target_settings_fixture(fixture)
                    readings[tune] = [{key: conf.read(file).text(key) for key in tunes} for file in ("mac_test", settings)]
        self.assertEqual(readings["tuned"], readings["pinned"])

    def test_a_placement_fixture_refuses_settings_outside_its_root(self) -> None:
        with TemporaryDirectory() as outside, mock.patch.object(conf, "directory", return_value=Path(outside)):
            with self.assertRaisesRegex(AssertionError, "must not read the live settings"):
                with PlacementFixture():
                    pass
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_gate_constraints_name_the_guest_and_natedev(self) -> None:
        from scripts.build_targets import build_targets
        from scripts.lib import build_folders

        with PlacementFixture() as fixture:
            _ = mac_linux_fixture(fixture, enabled="off")
            with (mock.patch.object(eligibility, "repository_identity", return_value=eligibility.KnownRepository("claude")),
                  mock.patch.object(build_folders, "repo_word", return_value="claude"),
                  mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(fixture.root / "target-state")})):
                constraints = build_targets.fixed_placement_constraints(fixture.root, "", "lint",
                    ("scripts/production/gate.py", "--affected"), SUBMITTING_SESSION, "source-a")
            self.assertEqual(constraints.candidates, (mac_linux_candidate(), local_candidate(fixture)))

    def test_mac_linux_candidate_checks_switch_backoff_and_the_latest_good_reading(self) -> None:
        cases = (("off", "mac_linux_off"), ("unregistered", "mac_linux_unregistered"),
                 ("unreadable", "mac_linux_backoff_unreadable"), ("active", "mac_linux_backoff"),
                 ("stale", "mac_linux_reading_required"), ("fresh", ""))
        for case, reason in cases:
            with self.subTest(case=case), PlacementFixture() as fixture:
                backoff = mac_linux_fixture(fixture, enabled="off" if case == "off" else "on",
                                            registered=case != "unregistered")
                now = fixture.clock.now()
                if case in ("unreadable", "active", "stale", "fresh"):
                    deadline = now + 5 if case == "active" else now - 1
                    _ = backoff.write_text("invalid" if case == "unreadable" else str(deadline))
                    failed_at = now if case == "stale" else now - 2
                    os.utime(backoff, (failed_at, failed_at))
                guest = mac_linux_candidate()
                request = placement_request(fixture)
                request = replace(request, constraints=replace(request.constraints, candidates=(guest, local_candidate(fixture))))
                with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(backoff.parents[1])}):
                    _ = publish_candidate(fixture, request)
                    reply, = fixture.server.handle_request(request)
                if not reason:
                    assert isinstance(reply, wire.Assignment), reply
                    self.assertEqual(reply.assignment.destination, guest.destination)
                    continue
                assert isinstance(reply, wire.LocalPlacementGranted), reply
                self.assertEqual(reply.assignment.destination, local_candidate(fixture).destination)
                self.assertEqual(fixture.server.placement_eligibility[request.episode][0],
                                 placement.CandidateEligibility(guest.destination, placement.TemporaryExclusion(reason)))
                if case == "off":
                    _ = fixture.run_pass()
                    view, = fixture.server.handle_request(wire.ViewRequest())
                    assert isinstance(view, wire.ViewReply)
                    running, = (row for row in view.reading.view.running if row.pid == request.owner.pid)
                    self.assertEqual(running.placement_candidates, (
                        placement.CandidateEligibility(guest.destination, placement.TemporaryExclusion("the guest is off")),
                        placement.CandidateEligibility(local_candidate(fixture).destination, ALLOWED_HOST)))

    def test_a_placement_wait_row_waits_for_a_machine_choice_not_a_reading(self) -> None:
        with PlacementFixture() as fixture:
            backoff = mac_linux_fixture(fixture)
            parent = fixture.request(run=fixture.run_owner())
            _ = only_admitted(fixture.connect(parent).replies())
            # A speculative copy waits for the guest while its parent holds local memory.
            request = replace(placement_request(fixture), purpose=placement.SpeculativeCopy(parent.episode, fixture.clock.now()))
            request = replace(request, constraints=replace(request.constraints, candidates=(mac_linux_candidate(),)))
            now = fixture.clock.now()
            _ = backoff.write_text(str(now + 1))
            os.utime(backoff, (now, now))
            with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(backoff.parents[1])}):
                _ = publish_candidate(fixture, request)
                wait, = fixture.server.handle_request(request)
                assert isinstance(wait, wire.PlacementWait), wait
                _ = fixture.run_pass()
                reply, = fixture.server.handle_request(wire.ViewRequest())
            decoded = wire.parse_reply(wire.encode_reply(reply))
            assert isinstance(decoded, wire.ViewReply), decoded
            row, = decoded.reading.view.waiting
            self.assertEqual(row.reason, records.MachineChoiceWait())
            self.assertEqual(records.reason_phrase(row.reason, (row,)), "waits for a machine choice")
            self.assertEqual(memory_feed.wait_why(row.reason), "machine choice")
            self.assertEqual(limits_view.wait_words(limits_view.waiting_cause(row)).one, "1 run waits for a machine choice.")

    def test_a_guest_reading_after_backoff_can_restore_a_waiting_guest(self) -> None:
        with PlacementFixture() as fixture:
            backoff = mac_linux_fixture(fixture)
            guest = mac_linux_candidate()
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(guest,)))
            now = fixture.clock.now()
            _ = backoff.write_text(str(now + 1))
            os.utime(backoff, (now, now))
            with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(backoff.parents[1])}):
                _ = publish_candidate(fixture, request)
                first, = fixture.server.handle_request(request)
                assert isinstance(first, wire.PlacementWait), first
                self.assertEqual(first.eligibility[0].eligibility, placement.TemporaryExclusion("mac_linux_backoff"))
                recorded = (placement.CandidateEligibility(
                    guest.destination, placement.TemporaryExclusion("the guest is in retry backoff")),)
                snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
                assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
                self.assertEqual(tuple(snapshot.inputs.placement_candidates.values()), (recorded,))
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                decoded = wire.parse_reply(wire.encode_reply(reply))
                assert isinstance(decoded, wire.ViewReply)
                row, = decoded.reading.view.waiting
                self.assertEqual((row.pid, row.since, row.placement_candidates),
                                 (request.owner.pid, request.since, recorded))
                fixture.clock.advance(2)
                waiting, = fixture.server.handle_request(request)
                assert isinstance(waiting, wire.PlacementWait), waiting
                self.assertEqual(waiting.eligibility[0].eligibility,
                                 placement.TemporaryExclusion("mac_linux_reading_required"))
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                execution = execution_identity.ObservedExecution(execution_identity.ProcessExecution(
                    guest.destination.host, "host-generation-a", 700001, "guest-start"))
                _ = fixture.server.handle_request(wire.ExecutionReport("guest-before-launch", assignment, claim, execution,
                    placement.PrelaunchRefused("fixture")))
                with mock.patch.object(mac_run, "claim_held", return_value=True):
                    granted, = fixture.server.handle_request(consume_permission("guest-launch", assignment, claim))
                self.assertEqual(granted, wire.FirstAuthorization(assignment, claim))
                wrong = replace(assignment, attempt="another-attempt")
                _ = fixture.server.handle_request(wire.ExecutionReport("stale-guest-running", wrong, claim, execution))
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                pending, = (row for row in reply.reading.view.running if row.assignment_target == "mac-linux")
                self.assertEqual((pending.confirmed_guest_run, pending.mac_placement), (None, ""))

                _ = fixture.server.handle_request(wire.ExecutionReport("guest-running", assignment, claim, execution))
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                decoded = wire.parse_reply(wire.encode_reply(reply))
                assert isinstance(decoded, wire.ViewReply)
                running, = (row for row in decoded.reading.view.running if row.assignment_target == "mac-linux")
                self.assertEqual((running.pid, running.execution, running.placement_candidates),
                                 (700001, execution, (placement.CandidateEligibility(guest.destination, ALLOWED_HOST),)))
                self.assertEqual(running.confirmed_guest_run, {"target": "mac-linux", "attempt": assignment.attempt})
                self.assertEqual(running.mac_placement, f"{assignment.episode}/{assignment.attempt}")
            self.assertEqual((assignment.destination, assignment.since), (guest.destination, request.since))

    def test_a_native_mac_assignment_keeps_every_named_target_and_exclusion(self) -> None:
        with PlacementFixture() as fixture:
            backoff = mac_linux_fixture(fixture, enabled="off")
            registry = fixture.root / "build_targets.conf"
            _ = registry.write_text(registry.read_text().replace("targets=", "targets=mac,", 1)
                                    + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n")
            request = placement_request(fixture)
            native, = request.constraints.candidates
            guest = mac_linux_candidate()
            local = replace(local_candidate(fixture), eligibility=placement.FixedExclusion("command"))
            request = replace(request, constraints=replace(request.constraints, candidates=(guest, native, local)))
            with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(backoff.parents[1])}):
                _ = publish_candidate(fixture, replace(request, constraints=replace(request.constraints, candidates=(native,))))
                assignment = assignment_for(fixture, request)
                self.assertEqual(assignment.destination, native.destination)
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                decoded = wire.parse_reply(wire.encode_reply(reply))
                assert isinstance(decoded, wire.ViewReply)
            running, = (row for row in decoded.reading.view.running if row.assignment_host == native.destination.host)
            self.assertEqual(running.placement_candidates, (
                placement.CandidateEligibility(guest.destination, placement.TemporaryExclusion("the guest is off")),
                placement.CandidateEligibility(native.destination, ALLOWED_HOST),
                placement.CandidateEligibility(local.destination,
                    placement.FixedExclusion("the target does not support this command"))))
            self.assertIsNone(running.confirmed_guest_run)
            self.assertEqual(running.mac_placement, f"{assignment.episode}/{assignment.attempt}")

    def test_blocked_provider_claim_and_cleanup_keep_local_socket_requests_responsive(self) -> None:
        for operation in ("claim", "cleanup"):
            with self.subTest(operation=operation), PlacementFixture() as fixture:
                fixture.enter_context(mock.patch.dict(os.environ, {
                    "MAC_TEST_STATE_DIR": str(fixture.root / "blocked-provider"),
                }))
                _ = (fixture.root / "build_targets.conf").write_text(
                    "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n")
                request, worktree = mac_claim_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                reference = adopt_assignment(fixture, request, assignment)
                paths = claim_state.state_paths(assignment.destination.target)
                paths.directory.mkdir()
                record: mac_test.RemoteJobClaim = {"claim_id": reference.claim_id, "episode": reference.episode,
                    "attempt": reference.attempt, "submitting_session": "submitting-session",
                    "pid": request.owner.pid, "proc_start": request.owner.start_time, "remote_host": assignment.destination.host,
                    "what": "blocked provider", "worktree": str(worktree), "since": "2026-11-10T12:00:00+00:00"}
                mac_test.write_json(paths.run, record)
                before = paths.run.read_bytes()
                granted = only_admitted(fixture.connect(fixture.request()).replies())
                lock_requested = threading.Event()
                provider_lock = mac_test.locked

                def observe_lock_request(path: Path) -> AbstractContextManager[None]:
                    if path == paths.lock:
                        lock_requested.set()
                    return provider_lock(path)

                def adapter() -> int:
                    args = mac_test.CliArguments()
                    args.pid, args.worktree, args.remote = request.owner.pid, str(worktree), assignment.destination.host
                    args.claim_id, args.episode, args.attempt = reference.claim_id, reference.episode, reference.attempt
                    with redirect_stdout(io.StringIO()):
                        if operation == "cleanup":
                            return mac_test.command_release(args, paths)
                        return mac_test.command_claim(args, paths, watch_claim=False,
                            session=assignment.session, assignment=assignment)

                with (ThreadPoolExecutor(max_workers=2) as workers,
                      mock.patch.object(mac_test, "locked", new=observe_lock_request)):
                    with provider_lock(paths.lock):
                        pending = workers.submit(adapter)
                        LauncherHangGuard(f"Provider {operation} lock request").event(lock_requested)
                        self.assertFalse(pending.done())
                        fixture.allow_next_start()
                        for message in (wire.ViewRequest(), wire.ReleaseRequest("release-responsive", granted.reservation, 0),
                                        fixture.request()):
                            reply_guard = LauncherHangGuard(
                                f"{type(message).__name__} while provider {operation} waits for the lock")
                            replies = reply_guard.result(workers.submit(fixture.connect(message).replies))
                            self.assertTrue(replies)
                            self.assertIsInstance(replies[-1], wire.ViewReply if isinstance(message, wire.ViewRequest)
                                else wire.Ok if isinstance(message, wire.ReleaseRequest) else wire.Admitted)
                            self.assertFalse(pending.done())
                        self.assertEqual(paths.run.read_bytes(), before)
                    _ = LauncherHangGuard(f"Provider {operation} completion").result(pending)

    def test_speculative_placement_keeps_the_newest_live_candidate_and_its_local_parent_charge(self) -> None:
        cases = (
            ((("v1", 100.0, True), ("v2", 200.0, True), ("v3", 300.0, False)), "v2"),
            ((("v-a", 100.0, True), ("v-b", 100.0, True)), "v-b"),
            ((), ""),
            ((("v1", 100.0, False),), ""),
        )
        for candidates, expected in cases:
            with self.subTest(candidates=candidates), PlacementFixture() as fixture:
                parent = fixture.request(run=fixture.run_owner())
                grant = only_admitted(fixture.connect(parent).replies())
                submitted: list[wire.PlacementRequest] = []
                for episode, started, live in candidates:
                    request = replace(placement_request(fixture), episode=episode,
                        purpose=placement.SpeculativeCopy(parent.episode, started))
                    _ = publish_candidate(fixture, request, placement.TemporaryExclusion("busy"))
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    submitted.append(request)
                    if not live:
                        fixture.end_owner(wire.Owner(request.owner.pid, request.owner.start_time))
                for request in submitted:
                    _ = publish_candidate(fixture, request)
                _ = fixture.run_pass()
                selected = [request.episode for request in submitted
                            if request.episode in fixture.server.current_assignments]
                self.assertEqual(selected, [expected] if expected else [])
                for _ in range(2):
                    _ = fixture.run_pass()
                    self.assertEqual([request.episode for request in submitted
                                      if request.episode in fixture.server.current_assignments], selected)
                self.assertEqual(set(fixture.server.reservations), {parent.episode})
                self.assertEqual(fixture.server.reservations[parent.episode].reservation_id, grant.reservation)

    def test_fixed_and_movable_waiters_share_order_and_priority_changes(self) -> None:
        for prioritize_movable in (False, True):
            with self.subTest(prioritize_movable=prioritize_movable), PlacementFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                fixed = fixture.request(since=fixture.clock.now() - 60, run=fixture.run_owner())
                movable = placement_request(fixture, since=fixture.clock.now() - 10)
                movable = replace(movable, constraints=replace(movable.constraints,
                    candidates=(local_candidate(fixture),)))
                labels = {fixed.identity.session_id: UnitLabels("older", unit="older"),
                          movable.identity.session_id: UnitLabels("movable", unit="movable")}
                with mock.patch.object(gate, "priority_labels", return_value=labels):
                    self.assertIsInstance(fixture.connect(fixed).replies()[-1], wire.Wait)
                    self.assertIsInstance(fixture.connect(movable).replies()[-1], wire.PlacementWait)
                    view, = fixture.connect(wire.ViewRequest()).replies()
                    self.assertIsInstance(view, wire.ViewReply)
                    assert isinstance(view, wire.ViewReply)
                    self.assertEqual([row.pid for row in view.reading.view.waiting],
                                     [fixed.owner.pid, movable.owner.pid])
                    places = dict(fixture.server.run_places)
                    if prioritize_movable:
                        self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                            "movable-first", "movable", "the user", "fixture-director")), [wire.Ok()])
                    view, = fixture.connect(wire.ViewRequest()).replies()
                    assert isinstance(view, wire.ViewReply)
                    expected = ([movable.owner.pid, fixed.owner.pid] if prioritize_movable
                                else [fixed.owner.pid, movable.owner.pid])
                    self.assertEqual([row.pid for row in view.reading.view.waiting], expected)
                    self.assertEqual(fixture.server.run_places, places)
                    fixture.free(60 * GIB)
                    write_pressure_reading(fixture.root / "pressure")
                    _ = fixture.run_pass()
                    self.assertEqual(set(fixture.server.reservations),
                                     {movable.episode if prioritize_movable else fixed.episode})
                    if prioritize_movable:
                        granted, = fixture.connect(movable).replies()
                        self.assertIsInstance(granted, wire.LocalPlacementGranted)

    def test_each_step_carries_its_request_route_into_the_waiting_and_running_views(self) -> None:
        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            local = placement_request(fixture, since=fixture.clock.now() - 60)
            mac = local.constraints.candidates[0]
            local = replace(local, prerequisite=placement.TokenNotRequired(), route=LOCAL_CALLER,
                constraints=replace(local.constraints, candidates=(local_candidate(fixture),
                    replace(mac, eligibility=placement.FixedExclusion(LOCAL_CALLER.reason)))))
            pinned = placement_request(fixture, since=fixture.clock.now() - 30)
            pinned = replace(pinned, route=PinnedTarget("natedev"),
                constraints=replace(pinned.constraints, candidates=(local_candidate(fixture),)))
            routed = placement_request(fixture, since=fixture.clock.now() - 10)
            routed = replace(routed, constraints=replace(routed.constraints,
                candidates=(local_candidate(fixture), replace(mac, eligibility=placement.FixedExclusion("linux_only")))))
            for request in (local, pinned, routed):
                self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
            view, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(view, wire.ViewReply)
            self.assertEqual([(row.pid, row.route) for row in view.reading.view.waiting],
                             [(local.owner.pid, LOCAL_CALLER), (pinned.owner.pid, PinnedTarget("natedev")),
                              (routed.owner.pid, ANY_ELIGIBLE_TARGET)])
            fixture.free(60 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            _ = fixture.run_pass()
            granted, = fixture.connect(local).replies()
            self.assertIsInstance(granted, wire.LocalPlacementGranted)
            view, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(view, wire.ViewReply)
            self.assertEqual([step.route for step in view.reading.view.running if step.pid == local.owner.pid],
                             [LOCAL_CALLER])

    def test_token_readiness_blocks_local_memory_but_allows_remote_placement(self) -> None:
        cases = ((False, False, False, True), (True, False, False, True),
                 (False, True, False, True), (True, True, False, True),
                 (True, True, True, True), (True, True, True, False), (True, False, True, True))
        for remote_ready, local_first, unchecked, available_after_check in cases:
            with self.subTest(remote_ready=remote_ready, local_first=local_first,
                              unchecked=unchecked, available_after_check=available_after_check), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request, ALLOWED_HOST if remote_ready
                                      else placement.TemporaryExclusion("disk"))
                local = local_candidate(fixture)
                candidates = ((local, *request.constraints.candidates) if local_first
                              else (*request.constraints.candidates, local))
                prerequisite = (placement.TokenRequired(placement.TokenReadinessUnchecked())
                                if unchecked else placement.TokenRequired())
                request = replace(request, prerequisite=prerequisite, constraints=replace(
                    request.constraints, candidates=candidates))
                reply, = fixture.connect(request).replies()
                remote_selected = remote_ready and not (unchecked and local_first)
                self.assertIsInstance(reply, wire.Assignment if remote_selected else wire.PlacementWait)
                self.assertEqual(fixture.caller_reservations(), {})
                if remote_selected:
                    assert isinstance(reply, wire.Assignment)
                    self.assertEqual(reply.assignment.destination.provider, "mac")
                else:
                    confirmed = (placement.TokenHeld() if available_after_check else
                                 placement.TokenRequired(placement.TokenUnavailable()))
                    _ = fixture.connect(wire.PrerequisiteReport(
                        "token-checked", request.episode, request.owner, confirmed)).replies()
                    reply, = fixture.connect(request).replies()
                    if available_after_check:
                        self.assertIsInstance(reply, wire.LocalPlacementGranted)
                        self.assertEqual(set(fixture.server.reservations), {request.episode})
                    else:
                        self.assertIsInstance(reply, wire.Assignment)
                        assert isinstance(reply, wire.Assignment)
                        self.assertEqual(reply.assignment.destination.provider, "mac")
                        self.assertEqual(fixture.caller_reservations(), {})

    def test_released_local_grants_cannot_authorize_a_replayed_step(self) -> None:
        for ending in ("release", "cancel"):
            with self.subTest(ending=ending), PlacementFixture() as fixture:
                request = placement_request(fixture)
                request = replace(request, constraints=replace(request.constraints,
                    candidates=(local_candidate(fixture),)))
                granted, = fixture.connect(request).replies()
                self.assertIsInstance(granted, wire.LocalPlacementGranted)
                assert isinstance(granted, wire.LocalPlacementGranted)
                key = execution_identity.attempt_key(granted.assignment.episode, granted.assignment.attempt)
                self.assertIsInstance(fixture.server.assignments[key].permission, placement.PermissionConsumed)
                if ending == "release":
                    fixture.release(granted.grant)
                else:
                    self.assertEqual(fixture.server.handle_request(wire.CancelPlacement(
                        "cancel-consumed-local", request.episode, request.owner, "the call was interrupted")), [wire.Ok()])
                    self.assertIsInstance(fixture.server.assignments[key].state, placement.ClosedAssignment)
                self.assertEqual(fixture.caller_reservations(), {})
                replay, = fixture.connect(request).replies()
                self.assertNotIsInstance(replay, wire.LocalPlacementGranted)

    def test_restart_requires_token_confirmation_before_a_local_grant(self) -> None:
        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = placement_request(fixture)
            request = replace(request, prerequisite=placement.TokenHeld(), constraints=replace(
                request.constraints, candidates=(local_candidate(fixture),)))
            self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
            places = dict(fixture.server.run_places)
            fixture.restart()
            fixture.free(60 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
            self.assertEqual(fixture.caller_reservations(), {})
            self.assertEqual(fixture.server.run_places, places)
            _ = fixture.connect(wire.PrerequisiteReport(
                "token-reconfirmed", request.episode, request.owner, placement.TokenHeld())).replies()
            self.assertIsInstance(fixture.connect(request).replies()[-1], wire.LocalPlacementGranted)
            self.assertEqual(set(fixture.server.reservations), {request.episode})

    def test_a_head_keeps_its_local_place_while_a_mac_start_is_pending(self) -> None:
        hold_seconds = 600
        for ending in ("busy before authorization", "busy after authorization", "busy with stale local evidence",
                       "busy after estimate expiry", "confirmed", "time limit", "time limit with slot", "caller death"):
            with self.subTest(ending=ending), PlacementFixture() as fixture:
                refresh_seconds = 5 if ending == "busy after estimate expiry" else hold_seconds + 60
                fixture.configure(estimate_refresh_seconds=refresh_seconds, remote_start_hold_seconds=hold_seconds)
                head = placement_request(fixture, since=fixture.clock.now() - 60)
                slot_key = ""
                if ending == "time limit with slot":
                    run = fixture.run_owner()
                    slot_key = fixture.finish_step(fixture.request(run=run, need=16 * GIB)).reservation
                    head = replace(head, run=run)
                    fixture.free(11 * GIB)
                if ending == "busy after estimate expiry":
                    assert isinstance(head.profile, records.StepProfile)
                    head = replace(head, profile=replace(head.profile,
                        estimated_at=fixture.clock.now() - refresh_seconds + 1))
                head = replace(head, constraints=replace(head.constraints,
                    candidates=(*head.constraints.candidates, local_candidate(fixture))))
                local_only = replace(head, constraints=replace(head.constraints, candidates=(local_candidate(fixture),)))
                stale_local = ending in ("busy with stale local evidence", "busy after estimate expiry")
                if stale_local:
                    fixture.server.background_local_evidence = True
                    _ = publish_candidate(fixture, local_only)
                _ = publish_candidate(fixture, head)
                remote = assignment_for(fixture, head)
                self.assertEqual(remote.destination.provider, "mac")
                if slot_key:
                    self.assertIsInstance(fixture.server.slots[slot_key], records.ContinuationSlot)

                later = replace(local_cargo_request(fixture), route=LOCAL_CALLER)
                if stale_local:
                    fixture.clock.advance(records.read_memory_config().waiting_pass_seconds + 1)
                    _ = publish_candidate(fixture, later)
                    key = fixture.server.host_evidence_key(local_only, local_only.constraints.candidates[0].destination)
                    self.assertIsInstance(fixture.server.host_evidence(key), placement.StaleHostEvidence)

                reply, = fixture.connect(later).replies()
                self.assertIsInstance(reply, wire.PlacementWait)
                self.assertEqual(fixture.caller_reservations(), {})
                self.assertEqual(fixture.queue_pids(), [head.owner.pid, later.owner.pid])
                snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
                assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
                self.assertEqual([row["pid"] for _, row in snapshot.inputs.ordered], [head.owner.pid, later.owner.pid])
                self.assertEqual(snapshot.inputs.pending_remote_starts, frozenset({
                    records.run_owner(head.owner.pid, head.owner.start_time)}))

                claim = adopt_assignment(fixture, head, remote)
                if ending in ("busy after authorization", "busy after estimate expiry", "confirmed",
                               "time limit", "time limit with slot"):
                    authorized, = fixture.connect(consume_permission("mac-authorized", remote, claim)).replies()
                    self.assertIsInstance(authorized, wire.FirstAuthorization)
                    if ending == "busy after estimate expiry":
                        fixture.clock.advance(1)
                    _ = fixture.run_pass()
                    self.assertEqual(fixture.caller_reservations(), {})

                if ending.startswith("busy"):
                    if not stale_local:
                        _ = publish_candidate(fixture, local_only)
                    _ = publish_candidate(fixture, head, placement.TemporaryExclusion("busy"))
                    before_authorization = ending in ("busy before authorization", "busy with stale local evidence")
                    refusal = (wire.ClaimFailure("mac-busy", remote, "busy") if before_authorization
                        else wire.ExecutionReport("mac-busy", remote, claim, execution_identity.ExecutionNotStarted(),
                            placement.PrelaunchRefused("busy"), placement.DispatchPathSettled()))
                    refused, = fixture.connect(refusal).replies()
                    assert isinstance(refused, wire.DispatchStatus)
                    self.assertEqual(refused.state, placement.ClosedAssignment("busy"))

                    if stale_local:
                        _ = publish_candidate(fixture, later)
                        waiting, = fixture.connect(later).replies()
                        self.assertIsInstance(waiting, wire.PlacementWait)
                        self.assertEqual(fixture.caller_reservations(), {})
                        self.assertEqual(fixture.queue_pids(), [head.owner.pid, later.owner.pid])
                        _ = publish_candidate(fixture, local_only)
                    if ending == "busy after estimate expiry":
                        waiting, = fixture.connect(head).replies()
                        assert isinstance(waiting, wire.PlacementWait) and isinstance(waiting.wait, wire.Wait)
                        reason = waiting.wait.reason
                        cause = reason.cause if isinstance(reason, records.HeadAdmissionStatus) else reason
                        self.assertIsInstance(cause, records.EstimateExpired)
                        self.assertEqual((waiting.position, waiting.since), (1, head.since))
                        self.assertEqual(fixture.queue_pids(), [head.owner.pid, later.owner.pid])
                        self.assertEqual(fixture.caller_reservations(), {})
                        assert isinstance(head.profile, records.StepProfile)
                        assert isinstance(head.run, wire.RunIdentity)
                        estimate = replace(fixture.request(owner=wire.Owner(head.owner.pid, head.owner.start_time),
                            episode=head.episode, since=head.since, run=head.run), profile=head.profile)
                        fresh = replace(estimate, profile=replace(head.profile, estimated_at=fixture.clock.now()))
                        path = fixture.root / "assignment.json"
                        args = ["place-step", "--once", "--pid", str(head.owner.pid), "--episode", head.episode,
                                "--since", str(head.since), "--run", f"{head.run.pid}:{head.run.start_time}",
                                "--assignment-file", str(path), "--", *head.constraints.command]
                        with (fixture.serving(), mock.patch.object(client, "CLOCK", fixture.clock),
                              mock.patch.object(client, "step_request", side_effect=(estimate, fresh)) as estimate_step,
                              mock.patch("scripts.build_targets.build_targets.fixed_placement_constraints",
                                         return_value=head.constraints),
                              redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO())):
                            self.assertEqual(client.main(args), 0, errors.getvalue())
                        self.assertEqual(estimate_step.call_count, 2)
                        local = client.read_assignment(path)
                    else:
                        local, = fixture.connect(head).replies()
                    assert isinstance(local, wire.LocalPlacementGranted)
                    self.assertEqual(local.assignment.since, head.since)
                    self.assertEqual(set(fixture.caller_reservations()), {head.episode})
                    continue

                if ending == "confirmed":
                    execution = execution_identity.ProcessExecution(remote.destination.host, "host-generation-a", 700001, "start")
                    key = fixture.server.host_evidence_key(head, remote.destination)
                    evidence = fixture.provider_evidence[key]
                    assert isinstance(evidence, placement.FreshHostEvidence)
                    confirmed = replace(evidence, executions=((claim, execution_identity.ObservedExecution(execution)),))
                    fixture.provider_evidence[key] = confirmed
                    fixture.server.publish_host_evidence(confirmed)
                elif ending in ("time limit", "time limit with slot"):
                    fixture.clock.advance(hold_seconds - 1)
                    if ending == "time limit":
                        fixture.restart()
                    _ = fixture.run_pass()
                    self.assertEqual(fixture.caller_reservations(), {})
                    if slot_key:
                        self.assertIsInstance(fixture.server.slots[slot_key], records.ContinuationSlot)
                    fixture.clock.advance(1)
                else:
                    fixture.end_owner(wire.Owner(head.owner.pid, head.owner.start_time))
                _ = fixture.run_pass()
                if slot_key:
                    self.assertNotIn(slot_key, fixture.server.slots)
                self.assertEqual(set(fixture.caller_reservations()), {later.episode})
                self.assertNotIn(head.owner.pid, fixture.queue_pids())

    def test_ending_a_remote_start_releases_only_the_heads_claim(self) -> None:
        hold_seconds = 600
        deadline_cases = ("deadline with a newer slot", "deadline with no initial claim")
        for ending in (*deadline_cases, "lost local eligibility", "missing assignment"):
            with self.subTest(ending=ending), PlacementFixture() as fixture:
                fixture.configure(remote_start_hold_seconds=hold_seconds)
                run = fixture.run_owner()
                head = replace(placement_request(fixture, since=fixture.clock.now() - 60), run=run)
                head = replace(head, constraints=replace(head.constraints,
                    candidates=(*head.constraints.candidates, local_candidate(fixture))))
                later = (only_admitted(fixture.server.handle_request(fixture.request(run=run)))
                         if ending in deadline_cases else None)
                slot_key = ("" if ending == "deadline with no initial claim"
                            else fixture.finish_step(fixture.request(run=run)).reservation)
                _ = publish_candidate(fixture, head)
                remote = assignment_for(fixture, head)
                self.assertEqual(remote.destination.provider, "mac")
                self.assertIn(head.episode, fixture.server.waiters)
                claimed = fixture.server.slots.get(slot_key)
                if slot_key:
                    self.assertIsInstance(claimed, records.ContinuationSlot)
                else:
                    self.assertEqual(fixture.server.slots, {})

                if later is not None:
                    fixture.clock.advance(hold_seconds - 1)
                    fixture.release(later)
                    later_slot = fixture.server.slots[later.reservation]
                    if slot_key:
                        self.assertIsInstance(later_slot, records.BetweenStepsSlot)
                        self.assertIs(fixture.server.slots[slot_key], claimed)
                    else:
                        later_slot = gate.between_steps(later_slot)
                        _ = fixture.run_pass()
                    fixture.clock.advance(1)
                    _ = fixture.run_pass()
                    self.assertEqual(fixture.server.slots.get(later.reservation), later_slot)
                elif ending == "lost local eligibility":
                    retry = replace(head, local=wire.LocalAdmissionUnavailable("no local estimate"))
                    reply, = fixture.connect(retry).replies()
                    self.assertIsInstance(reply, wire.Assignment)
                else:
                    key = execution_identity.attempt_key(remote.episode, remote.attempt)
                    del fixture.server.assignments[key]
                    _ = fixture.run_pass()

                self.assertNotIn(head.episode, fixture.server.waiters)
                if slot_key:
                    self.assertNotIn(slot_key, fixture.server.slots)

    def test_a_pending_mac_start_keeps_its_place_without_overriding_a_local_hold(self) -> None:
        with PlacementFixture() as fixture:
            head = placement_request(fixture, since=fixture.clock.now() - 60)
            local = replace(head, constraints=replace(head.constraints, candidates=(local_candidate(fixture),)))
            _ = publish_candidate(fixture, local, placement.TemporaryExclusion("blocked"))
            _ = publish_candidate(fixture, head)
            head = replace(head, constraints=replace(head.constraints,
                candidates=(*head.constraints.candidates, local_candidate(fixture))))
            _ = assignment_for(fixture, head)

            later = replace(local_cargo_request(fixture), route=LOCAL_CALLER)
            granted, = fixture.connect(later).replies()
            self.assertIsInstance(granted, wire.LocalPlacementGranted)
            self.assertEqual(set(fixture.caller_reservations()), {later.episode})

            _ = publish_candidate(fixture, local)
            _ = fixture.run_pass()
            self.assertEqual(fixture.queue_pids(), [head.owner.pid])

    def test_a_pending_mac_only_start_does_not_hold_natedev(self) -> None:
        with PlacementFixture() as fixture:
            head = placement_request(fixture, since=fixture.clock.now() - 60)
            _ = publish_candidate(fixture, head)
            _ = assignment_for(fixture, head)

            later = replace(local_cargo_request(fixture), route=LOCAL_CALLER)
            granted, = fixture.connect(later).replies()
            self.assertIsInstance(granted, wire.LocalPlacementGranted)
            self.assertEqual(set(fixture.caller_reservations()), {later.episode})
            self.assertNotIn(head.owner.pid, fixture.queue_pids())

    def test_remote_refusal_gets_one_local_reservation_for_the_replacement(self) -> None:
        cases = ((offload.DeclinedMacRun("disk", 0), True, False),
                 (offload.NoTestMatchedOnMac(0), True, True), (offload.LostMacRun(0), True, True),
                 (offload.LostMacRun(0), False, False))
        for result, consumed, authorized in cases:
            with self.subTest(result=type(result).__name__, consumed=consumed), PlacementFixture() as fixture:
                request = placement_request(fixture)
                request = replace(request, constraints=replace(request.constraints,
                    candidates=(*request.constraints.candidates, local_candidate(fixture))))
                _ = publish_candidate(fixture, request)
                remote = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, remote)
                if consumed:
                    self.assertIsInstance(fixture.connect(consume_permission("remote-permission", remote, claim)).replies()[-1],
                                          wire.FirstAuthorization)
                _ = publish_candidate(fixture, request, placement.TemporaryExclusion("disk"))
                controller = offload.ProcessController()
                controller.attempt_evidence.provider = claim
                controller.attempt_evidence.cleanup = "confirmed"
                controller.attempt_evidence.launch_authorized = authorized
                adapter_request = offload.OffloadRequest(fixture.root, "hana", "call", offload.NO_RESULT_FILE, (),
                    owner="fixture-adapter", holder_session=claim_state.session_id(remote.session),
                    target=remote.destination.target, placement=offload.AssignedOffloadRequest(remote))
                with fixture.serving():
                    offload.report_placement_result(controller, adapter_request, result)
                fixture.clock.advance(0.01)
                _ = publish_candidate(fixture, request, placement.TemporaryExclusion("disk"))
                granted, = fixture.connect(request).replies()
                self.assertIsInstance(granted, wire.LocalPlacementGranted)
                if not isinstance(granted, wire.LocalPlacementGranted):
                    self.fail(f"A settled refusal did not permit local admission: {granted!r}")
                self.assertNotEqual(granted.assignment.attempt, remote.attempt)
                self.assertEqual((granted.assignment.episode, granted.assignment.since), (request.episode, request.since))
                self.assertEqual(set(fixture.server.reservations), {request.episode})
                self.assertEqual(fixture.connect(request).replies(), [granted])
                view, = fixture.connect(wire.ViewRequest()).replies()
                assert isinstance(view, wire.ViewReply)
                local = [row for row in view.reading.view.running if row.pid == request.owner.pid]
                self.assertEqual(len(local), 1)
                self.assertEqual(local[0].promise, 8 * GIB)
                self.assertEqual(fixture.connect(consume_permission("late-old-consumption", remote, claim)).replies()[0].__class__,
                                 wire.DispatchStatus)

    def test_remote_selection_releases_only_its_unused_continuation_without_spacing(self) -> None:
        with PlacementFixture() as fixture:
            leaving_run, staying_run = fixture.run_owner(), fixture.run_owner()
            leaving_key = fixture.finish_step(fixture.request(run=leaving_run)).reservation
            staying_key = fixture.finish_step(fixture.request(run=staying_run)).reservation
            self.assertIn(leaving_key, fixture.server.slots)
            retained = fixture.server.slots[staying_key]
            spacing = records.NextStartAt(fixture.clock.now() + 60)
            fixture.server.spacing = spacing
            request = replace(placement_request(fixture), run=leaving_run)
            _ = publish_candidate(fixture, request)
            assigned = assignment_for(fixture, request)
            self.assertEqual(assigned.destination.provider, "mac")
            self.assertNotIn(leaving_key, fixture.server.slots)
            self.assertEqual(fixture.server.slots[staying_key], retained)
            self.assertEqual(fixture.server.spacing, spacing)
            self.assertEqual(fixture.caller_reservations(), {})

    def test_terminal_reports_close_the_exact_attempt_and_cannot_close_its_replacement(self) -> None:
        cases = (("busy", 0, False, False, False), ("blocked", 0, False, False, False),
                 ("disk", 0, False, False, False), ("unreachable", 180, False, False, False),
                 ("disk", 0, True, False, False), ("disk", 0, False, True, False),
                 ("busy", 0, True, False, True), ("busy", 0, False, True, True))
        for reason, delay, delayed_absence, stale_absence, foreign_claim in cases:
            with self.subTest(reason=reason, delayed_absence=delayed_absence,
                              stale_absence=stale_absence, foreign_claim=foreign_claim), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                original = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, original)
                if foreign_claim:
                    claim = replace(claim, claim_id="retained-claim", episode="previous-episode", attempt="previous-attempt")
                    retained = placement.AdoptedProviderClaim(claim, coordinator_holder(original), original.session,
                        original.destination.host, "host-generation-a")
                    _ = publish_candidate(fixture, request, claims=(retained,))
                else:
                    _ = fixture.connect(consume_permission("consume-original", original, claim)).replies()
                if not delayed_absence:
                    _ = publish_candidate(fixture, request)
                if stale_absence:
                    fixture.clock.advance(1)
                report = (wire.ClaimFailure("retained-claim-refused", original, reason, claim) if foreign_claim
                    else wire.ExecutionReport("prelaunch-refused", original, claim, execution_identity.ExecutionNotStarted(),
                        placement.PrelaunchRefused(reason), placement.DispatchPathSettled()))
                reported_at = fixture.clock.now()
                _ = fixture.connect(report).replies()
                if stale_absence:
                    closed = fixture.server.assignments[execution_identity.attempt_key(original.episode, original.attempt)]
                    self.assertIsInstance(closed.state, placement.ClosedAssignment)
                    self.assertEqual(closed.provider, claim)
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    fixture.clock.advance(1)
                    _ = publish_candidate(fixture, request)
                    _ = fixture.run_pass()
                if delayed_absence:
                    closed = fixture.server.assignments[execution_identity.attempt_key(original.episode, original.attempt)]
                    self.assertIsInstance(closed.state, placement.ClosedAssignment)
                    self.assertEqual(closed.provider, claim)
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    fixture.restart()
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    self.assertEqual(fixture.server.assignments[execution_identity.attempt_key(
                        original.episode, original.attempt)].provider, claim)
                    fixture.clock.advance(7)
                    _ = publish_candidate(fixture, request)
                    _ = fixture.run_pass()
                saved = fixture.server.assignments[execution_identity.attempt_key(original.episode, original.attempt)]
                self.assertIsInstance(saved.state, placement.ClosedAssignment)
                self.assertEqual(saved.retry, placement.RetryAt(reported_at + delay))
                places = dict(fixture.server.run_places)
                fixture.restart()
                if delay:
                    _ = publish_candidate(fixture, request)
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    fixture.clock.advance(delay - 0.01)
                    _ = publish_candidate(fixture, request)
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    fixture.clock.advance(0.01)
                _ = publish_candidate(fixture, request)
                replacement = assignment_for(fixture, request)
                self.assertNotEqual(replacement.attempt, original.attempt)
                self.assertEqual(replacement.since, original.since)
                self.assertEqual(fixture.server.run_places, places)
                _ = fixture.connect(replace(report, request_id="late-old-report")).replies()
                self.assertEqual(assignment_for(fixture, request), replacement)
                replacement_claim = adopt_assignment(fixture, request, replacement)
                self.assertIsInstance(fixture.connect(consume_permission(
                    "replacement-launch", replacement, replacement_claim)).replies()[-1], wire.FirstAuthorization)

    def test_a_mac_linux_guest_that_took_no_run_waits_before_the_same_guest_again(self) -> None:
        guest = placement.PlacementDestination("ssh", "mac-linux", "fixture-guest", FIXTURE_MAC_SCOPE)
        local = placement.PlacementDestination("local", "natedev", "natedev", FIXTURE_MAC_SCOPE)
        self.assertEqual(placement.retry_decision(guest, MAC_LINUX_NO_ANSWER, 10.0), placement.RetryAt(190.0))
        self.assertEqual(placement.retry_decision(local, "busy", 10.0), placement.RetryAt(10.0))

    def test_a_guest_that_took_no_run_frees_the_run_for_natedev_at_once(self) -> None:
        # Two named machines with the real queue choice: the Mac Linux guest declines, natedev takes the run at once.
        with PlacementFixture() as fixture:
            request = mac_linux_and_natedev_request(fixture)
            _ = publish_candidate(fixture, request)
            declined = assignment_for(fixture, request)
            self.assertEqual(declined.destination.target, "mac-linux")
            status, = fixture.connect(wire.ClaimFailure("guest-took-no-run", declined, MAC_LINUX_NO_ANSWER)).replies()
            assert isinstance(status, wire.DispatchStatus)
            self.assertEqual(status.state, placement.ClosedAssignment(MAC_LINUX_NO_ANSWER))
            self.assertEqual(status.retry, placement.RetryAt(fixture.clock.now() + 180))
            replacement, = fixture.server.handle_request(request)
            assert isinstance(replacement, wire.LocalPlacementGranted), replacement
            self.assertEqual(replacement.assignment.destination.target, "natedev")
            self.assertNotEqual(replacement.assignment.attempt, declined.attempt)

    def test_a_consumed_guest_assignment_stays_current_while_its_holder_lives(self) -> None:
        # The gate consumes the launch permission. The guest stays busy past the remote start hold
        # and after copies.py ends, until the gate ends.
        hold_seconds = 120
        with PlacementFixture() as fixture:
            fixture.configure(remote_start_hold_seconds=hold_seconds)
            request = mac_linux_and_natedev_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            destination = assignment.destination
            self.assertEqual(destination.target, "mac-linux")
            gate_process = guest_gate(fixture, assignment)
            granted, = consume_guest_permission(fixture, assignment, gate_process)
            self.assertIsInstance(granted, wire.FirstAuthorization)

            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)

            def passes(count: int) -> None:
                for _ in range(count):
                    fixture.clock.advance(60)
                    _ = publish_candidate(fixture, request)
                    _ = fixture.run_pass()

            passes(hold_seconds // 60 + 3)
            self.assertEqual(fixture.server.current_assignments.get(assignment.episode), key)
            fixture.end_owner(wire.Owner(request.owner.pid, request.owner.start_time))
            passes(2)
            self.assertEqual(fixture.server.current_assignments.get(assignment.episode), key)

            other = mac_linux_and_natedev_request(fixture, natedev=False)
            _ = publish_candidate(fixture, other)
            wait, = fixture.server.handle_request(other)
            assert isinstance(wait, wire.PlacementWait), wait
            self.assertEqual(wait.eligibility[0].eligibility, placement.TemporaryExclusion("busy"))
            view, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(view, wire.ViewReply)
            running, = (row for row in view.reading.view.running if row.assignment_target == "mac-linux")
            self.assertEqual(running.assignment_host, destination.host)

            fixture.end_owner(gate_process)
            passes(1)
            self.assertNotIn(assignment.episode, fixture.server.current_assignments)

    def test_a_guest_gate_that_took_no_run_after_its_consume_closes_for_a_retry(self) -> None:
        # The gate ends before copies.py reports the claim failure. The last system reading can still show the gate.

        with PlacementFixture() as fixture:
            request = mac_linux_and_natedev_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            gate_process = guest_gate(fixture, assignment)
            granted, = consume_guest_permission(fixture, assignment, gate_process)
            self.assertIsInstance(granted, wire.FirstAuthorization)
            _ = fixture.run_pass()
            reading = fixture.server.system_evidence
            assert isinstance(reading, service.SystemEvidence)
            self.assertIn(gate_process.pid, reading.processes)

            live, = fixture.server.handle_request(wire.ClaimFailure("gate-live", assignment, MAC_LINUX_NO_ANSWER))
            assert isinstance(live, wire.DispatchStatus), live
            self.assertEqual(live.state, placement.CurrentAssignment())

            fixture.end_owner(gate_process)
            fixture.server.system_reader = FixedSystemReader(reading)
            settled, = fixture.server.handle_request(wire.ClaimFailure("gate-ended", assignment, MAC_LINUX_NO_ANSWER))
            assert isinstance(settled, wire.DispatchStatus), settled
            self.assertEqual(settled.state, placement.ClosedAssignment(MAC_LINUX_NO_ANSWER))
            self.assertIsInstance(settled.permission, placement.PermissionConsumed)
            self.assertEqual(settled.retry, placement.RetryAt(fixture.clock.now() + 180))
            self.assertNotIn(assignment.episode, fixture.server.current_assignments)

    def test_a_guest_attempt_stays_current_after_its_gate_ends_while_its_owner_lives(self) -> None:
        # A pass that reads the ended gate and fresh guest evidence leaves the attempt to copies.py.
        with PlacementFixture() as fixture:
            def ended_gate() -> tuple[wire.PlacementRequest, placement.PlacementAssignment]:
                request = mac_linux_and_natedev_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                gate_process = guest_gate(fixture, assignment)
                granted, = consume_guest_permission(fixture, assignment, gate_process)
                self.assertIsInstance(granted, wire.FirstAuthorization)
                fixture.end_owner(gate_process)
                fixture.clock.advance(60)
                _ = publish_candidate(fixture, request)
                _ = fixture.run_pass()
                self.assertEqual(fixture.server.current_assignments.get(assignment.episode),
                                 execution_identity.attempt_key(assignment.episode, assignment.attempt))
                return request, assignment

            _, reported = ended_gate()
            settled, = fixture.server.handle_request(wire.ClaimFailure("gate-ended", reported, MAC_LINUX_NO_ANSWER))
            assert isinstance(settled, wire.DispatchStatus), settled
            self.assertEqual(settled.state, placement.ClosedAssignment(MAC_LINUX_NO_ANSWER))
            self.assertEqual(settled.retry, placement.RetryAt(fixture.clock.now() + 180))

            request, silent = ended_gate()
            fixture.end_owner(wire.Owner(request.owner.pid, request.owner.start_time))
            _ = fixture.run_pass()
            self.assertNotIn(silent.episode, fixture.server.current_assignments)

    def test_a_gate_newer_than_the_system_reading_consumes_its_guest_permission(self) -> None:
        # copies.py starts the gate after the last system reading, so that reading does not show the holder.

        with PlacementFixture() as fixture:
            request = mac_linux_and_natedev_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            reading = fixture.server.system_evidence
            assert isinstance(reading, service.SystemEvidence)
            fixture.server.system_reader = FixedSystemReader(reading)
            gate_process = guest_gate(fixture, assignment)
            self.assertNotIn(gate_process.pid, reading.processes)
            granted, = consume_guest_permission(fixture, assignment, gate_process)
            self.assertIsInstance(granted, wire.FirstAuthorization)

    def test_a_gate_whose_pid_named_another_process_in_the_reading_consumes_its_guest_permission(self) -> None:
        # The last system reading shows the gate pid with the start time of an earlier process.

        with PlacementFixture() as fixture:
            request = mac_linux_and_natedev_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            gate_process = guest_gate(fixture, assignment)
            write_process(fixture.proc.path, gate_process.pid, parent=1, start="earlier-process")
            _ = fixture.run_pass()
            reading = fixture.server.system_evidence
            assert isinstance(reading, service.SystemEvidence)
            self.assertEqual(reading.processes[gate_process.pid].start, "earlier-process")
            write_process(fixture.proc.path, gate_process.pid, parent=assignment.owner.pid, start=gate_process.start_time)
            fixture.server.system_reader = FixedSystemReader(reading)
            granted, = consume_guest_permission(fixture, assignment, gate_process)
            self.assertIsInstance(granted, wire.FirstAuthorization)

    def test_a_holder_the_fresh_read_does_not_confirm_keeps_the_guest_permission(self) -> None:
        # The last system reading shows the holder pid with the start time of an earlier process.
        # The fresh read then refuses a holder that is not a descendant or that states a start that is not live.

        cases = (("not a descendant", False, None), ("not the live start", True, "not-the-live-start"))
        for case, descendant, stated_start in cases:
            with self.subTest(case), PlacementFixture() as fixture:
                request = mac_linux_and_natedev_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                gate_process = guest_gate(fixture, assignment)
                write_process(fixture.proc.path, gate_process.pid, parent=1, start="earlier-process")
                _ = fixture.run_pass()
                reading = fixture.server.system_evidence
                assert isinstance(reading, service.SystemEvidence)
                self.assertEqual(reading.processes[gate_process.pid].start, "earlier-process")
                parent = assignment.owner.pid if descendant else 1
                write_process(fixture.proc.path, gate_process.pid, parent=parent, start=gate_process.start_time)
                fixture.server.system_reader = FixedSystemReader(reading)
                holder = wire.Owner(gate_process.pid, stated_start or gate_process.start_time)
                refused, = consume_guest_permission(fixture, assignment, holder)
                assert isinstance(refused, wire.DispatchStatus), refused
                self.assertEqual(refused.reason, "claim holder is not a live coordinator descendant")
                self.assertEqual(refused.state, placement.CurrentAssignment())
                self.assertEqual(refused.permission, placement.PermissionAvailable())

    def test_a_guest_consume_without_the_held_claim_keeps_the_permission(self) -> None:
        with PlacementFixture() as fixture:
            request = mac_linux_and_natedev_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            refused, = consume_guest_permission(fixture, assignment, guest_gate(fixture, assignment), held=False)
            assert isinstance(refused, wire.DispatchStatus), refused
            self.assertEqual(refused.reason, "the exact claim is not adopted")
            self.assertEqual(refused.state, placement.CurrentAssignment())
            self.assertEqual(refused.permission, placement.PermissionAvailable())

    def test_a_guest_consume_with_unreadable_claim_state_keeps_the_permission(self) -> None:
        with PlacementFixture() as fixture:
            request = mac_linux_and_natedev_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            unreadable = claim_state.ClaimStateUnreadable(fixture.root / "claims", OSError("fixture read error"))
            refused, = consume_guest_permission(fixture, assignment, guest_gate(fixture, assignment), held=unreadable)
            assert isinstance(refused, wire.DispatchStatus), refused
            self.assertEqual(refused.reason, f"adoption evidence is unavailable: {unreadable}")
            self.assertEqual(refused.state, placement.CurrentAssignment())
            self.assertEqual(refused.permission, placement.PermissionAvailable())

    def test_a_second_decline_ends_the_guest_for_the_run(self) -> None:
        # A cause that does not clear must not repeat on the same guest: the second decline ends the guest for the run.
        with PlacementFixture() as fixture:
            request = mac_linux_and_natedev_request(fixture, natedev=False)
            for decline in range(2):
                _ = publish_candidate(fixture, request)
                declined = assignment_for(fixture, request)
                self.assertEqual(declined.destination.target, "mac-linux")
                status, = fixture.connect(wire.ClaimFailure(f"decline-{decline}", declined, MAC_LINUX_NO_ANSWER)).replies()
                assert isinstance(status, wire.DispatchStatus)
                self.assertEqual(status.state, placement.ClosedAssignment(MAC_LINUX_NO_ANSWER))
                fixture.clock.advance(181)
            _ = publish_candidate(fixture, request)
            reply, = fixture.server.handle_request(request)
            assert isinstance(reply, wire.DispatchStatus), reply
            self.assertEqual(reply.retry, placement.DoNotRetry(MAC_LINUX_NO_ANSWER))
            self.assertEqual(reply.state, placement.ClosedAssignment(MAC_LINUX_NO_ANSWER))

    def test_settings_refusals_retry_after_the_provider_settings_change(self) -> None:
        for reason in ("off", "repo", "command"):
            with self.subTest(reason=reason), PlacementFixture() as fixture:
                settings = fixture.root / "provider.conf"
                _ = write_config_fixture(settings, template="mac_test")
                request = placement_request(fixture)
                candidate = replace(request.constraints.candidates[0],
                    settings=placement.ExplicitProviderSettings(settings))
                request = replace(request, constraints=replace(request.constraints, candidates=(candidate,)))
                original_evidence = publish_candidate(fixture, request)
                assigned = assignment_for(fixture, request)
                if reason == "repo":
                    claim = adopt_assignment(fixture, request, assigned)
                    _ = fixture.server.handle_request(wire.ExecutionReport("settings-refused", assigned,
                        claim, execution_identity.ExecutionNotStarted(), placement.PrelaunchRefused(reason),
                        placement.DispatchPathSettled()))
                else:
                    _ = fixture.server.handle_request(wire.ClaimFailure("settings-refused", assigned, reason))
                fixture.restart()
                _ = publish_candidate(fixture, request)
                reply, = fixture.server.handle_request(request)
                assert isinstance(reply, wire.DispatchStatus)
                self.assertEqual(reply.retry, placement.DoNotRetry(reason))

                _ = write_config_fixture(settings, "probe_timeout_s=16\n", template="mac_test")
                fixture.clock.advance(1)
                current_evidence = publish_candidate(fixture, request)
                self.assertNotEqual(original_evidence.key.configuration_generation,
                                    current_evidence.key.configuration_generation)
                with mock.patch.object(fixture.server, "host_evidence_key",
                                       wraps=fixture.server.host_evidence_key) as read_key:
                    _ = fixture.run_pass()
                read_key.assert_called_once_with(request, assigned.destination)
                if reason == "command":
                    self.assertNotIn(assigned.episode, fixture.server.current_assignments)
                    self.assertEqual(fixture.server.placement_eligibility[assigned.episode],
                        (placement.CandidateEligibility(assigned.destination, placement.FixedExclusion(reason)),))
                else:
                    key = fixture.server.current_assignments.get(assigned.episode)
                    self.assertIsNotNone(key, "the next pass must assign the target")
                    assert key is not None
                    renewed = fixture.server.assignments[key].assignment
                    self.assertNotEqual(renewed.attempt, assigned.attempt)
                    self.assertEqual((renewed.destination, renewed.since), (assigned.destination, assigned.since))

    def test_an_older_settings_refusal_without_a_generation_can_retry(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assigned = assignment_for(fixture, request)
            _ = fixture.server.handle_request(wire.ClaimFailure("old-off-refusal", assigned, "off"))
            key = execution_identity.attempt_key(assigned.episode, assigned.attempt)
            path = fixture.state_dir / "state.json"
            state = cast(dict[str, object], json.loads(path.read_text()))
            assignments = cast(dict[str, dict[str, object]], state["assignments"])
            _ = assignments[key].pop("assigned_settings_generation")
            _ = assignments[key].pop("refusal_settings")
            assignments[key]["settings_refusal_generation"] = {"kind": "SettingsRefusalGeneration", "digest": "old"}
            _ = path.write_text(json.dumps(state))
            fixture.restart()
            self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment("off"))
            _ = publish_candidate(fixture, request)
            _ = fixture.run_pass()
            self.assertIn(assigned.episode, fixture.server.current_assignments)
            self.assertNotEqual(fixture.server.current_assignments[assigned.episode], key)

    def test_enabling_the_route_before_the_refusal_report_permits_a_retry(self) -> None:
        with PlacementFixture() as fixture:
            settings = fixture.root / "provider.conf"
            _ = write_config_fixture(settings, "offload=off\n", template="mac_test")
            request = placement_request(fixture)
            candidate = replace(request.constraints.candidates[0], settings=placement.ExplicitProviderSettings(settings))
            request = replace(request, constraints=replace(request.constraints, candidates=(candidate,)))
            original = publish_candidate(fixture, request)
            assigned = assignment_for(fixture, request)
            fixture.restart()
            _ = write_config_fixture(settings, "offload=on\n", template="mac_test")
            self.assertIsInstance(fixture.server.handle_request(
                wire.ClaimFailure("delayed-off-refusal", assigned, "off"))[0], wire.DispatchStatus)
            enabled = publish_candidate(fixture, request)
            self.assertNotEqual(original.key.configuration_generation, enabled.key.configuration_generation)
            _ = fixture.run_pass()
            key = fixture.server.current_assignments.get(assigned.episode)
            self.assertIsNotNone(key, "the first pass with the enabled settings must assign the target")
            assert key is not None
            renewed = fixture.server.assignments[key].assignment
            self.assertNotEqual(renewed.attempt, assigned.attempt)
            self.assertEqual((renewed.destination, renewed.since), (assigned.destination, assigned.since))

    def test_hana_retries_when_settings_are_restored_before_the_dispatch_refusal(self) -> None:
        with PlacementFixture() as fixture:
            request, transport = hana_policy_request(fixture)
            settings = transport.settings
            enabled_settings = settings.read_bytes()
            original = publish_candidate(fixture, request)
            assigned = assignment_for(fixture, request)

            _ = write_config_fixture(settings, "enabled=off\n")
            disabled_key = fixture.server.host_evidence_key(request, assigned.destination)
            self.assertNotEqual(disabled_key.configuration_generation, original.key.configuration_generation)

            # The fixture executes the actual prelaunch check. No provider dispatch was started.
            with mock.patch.object(service.HostEvidenceWorker, "dispatch_pending", return_value=False):
                report_hana_refusal(fixture, transport, assigned, before_report=lambda: settings.write_bytes(enabled_settings))
                restored = publish_candidate(fixture, request)
                self.assertEqual(restored.key.configuration_generation, original.key.configuration_generation)
                _ = fixture.run_pass()
                key = execution_identity.attempt_key(assigned.episode, assigned.attempt)
                self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment("off"))
                replacement = assignment_for(fixture, request)
                self.assertNotEqual(replacement.attempt, assigned.attempt)
                self.assertEqual((replacement.destination, replacement.since), (assigned.destination, assigned.since))
                fixture.restart()
                self.assertEqual(fixture.server.assignments[key].refusal_settings,
                    records.RefusalSettings(disabled_key.configuration_generation))
                _ = publish_candidate(fixture, request)
                self.assertEqual(assignment_for(fixture, request), replacement)

    def test_hana_settings_edit_between_digest_and_policy_permits_one_retry(self) -> None:
        with PlacementFixture() as fixture:
            request, transport = hana_policy_request(fixture)
            enabled = publish_candidate(fixture, request)
            assigned = assignment_for(fixture, request)
            policy = hana.fixed_policy

            def disable_hana(settings: hana.HanaControlTransport, repository: str, kind: str
                             ) -> hana.HanaStepRefused | hana.EnabledHanaRoute:
                _ = write_config_fixture(settings.settings, "enabled=off\n")
                return policy(settings, repository, kind)

            with mock.patch.object(service.HostEvidenceWorker, "dispatch_pending", return_value=False):
                with mock.patch.object(hana, "fixed_policy", side_effect=disable_hana):
                    report_hana_refusal(fixture, transport, assigned, before_report=lambda: None)
                # The provider evidence permits a retry so the fixture can check the second report.
                disabled = publish_candidate(fixture, request)
                self.assertNotEqual(disabled.key.configuration_generation, enabled.key.configuration_generation)
                replacement = assignment_for(fixture, request)
                self.assertNotEqual(replacement.attempt, assigned.attempt)
                report_hana_refusal(fixture, transport, replacement, before_report=lambda: None)
                _ = publish_candidate(fixture, request)
                _ = fixture.run_pass()

            self.assertNotIn(assigned.episode, fixture.server.current_assignments)
            self.assertEqual(fixture.server.placement_eligibility[assigned.episode],
                (placement.CandidateEligibility(assigned.destination, placement.FixedExclusion("off")),))
            self.assertEqual(len(fixture.server.assignments), 2)
            for attempt, evidence in ((assigned, enabled), (replacement, disabled)):
                saved = fixture.server.assignments[execution_identity.attempt_key(attempt.episode, attempt.attempt)]
                self.assertEqual(saved.refusal_settings, records.RefusalSettings(evidence.key.configuration_generation))

    def test_settled_repo_decline_waits_for_a_settings_change(self) -> None:
        with PlacementFixture() as fixture:
            settings = fixture.root / "provider.conf"
            _ = write_config_fixture(settings, template="mac_test")
            request = placement_request(fixture)
            candidate = replace(request.constraints.candidates[0], settings=placement.ExplicitProviderSettings(settings))
            request = replace(request, constraints=replace(request.constraints, candidates=(candidate,)))
            _ = publish_candidate(fixture, request)
            assigned = assignment_for(fixture, request)
            claim = adopt_assignment(fixture, request, assigned)
            self.assertEqual(fixture.server.handle_request(consume_permission("repo-consume", assigned, claim)),
                [wire.FirstAuthorization(assigned, claim)])
            _ = fixture.connect(wire.ExecutionReport("repo-decline", assigned, claim, execution_identity.ExecutionNotStarted(),
                placement.AttemptDeclined("repo"), placement.DispatchPathSettled())).replies()
            key = execution_identity.attempt_key(assigned.episode, assigned.attempt)
            for pass_number in range(3):
                with self.subTest(pass_number=pass_number):
                    fixture.clock.advance(1)
                    _ = publish_candidate(fixture, request)
                    _ = fixture.run_pass()
                    self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment("repo"))
                    self.assertEqual(fixture.server.assignments[key].retry, placement.DoNotRetry("repo"))
                    self.assertNotIn(assigned.episode, fixture.server.current_assignments)
                if pass_number == 0:
                    fixture.restart()
            _ = write_config_fixture(settings, "linux_only.hana=\n", template="mac_test")
            _ = publish_candidate(fixture, request)
            _ = fixture.run_pass()
            replacement = assignment_for(fixture, request)
            self.assertNotEqual(replacement.attempt, assigned.attempt)
            self.assertEqual((replacement.destination, replacement.since), (assigned.destination, assigned.since))

    def test_a_settings_read_error_does_not_interrupt_a_refusal_report(self) -> None:
        with PlacementFixture() as fixture:
            settings = fixture.root / "provider.conf"
            _ = write_config_fixture(settings, template="mac_test")
            request = placement_request(fixture)
            candidate = replace(request.constraints.candidates[0], settings=placement.ExplicitProviderSettings(settings))
            request = replace(request, constraints=replace(request.constraints, candidates=(candidate,)))
            _ = publish_candidate(fixture, request)
            assigned = assignment_for(fixture, request)
            read_bytes = conf.read_bytes

            def read(path: Path) -> conf.ConfigurationBytes:
                if path == settings:
                    raise PermissionError("private provider settings are unreadable")
                return read_bytes(path)

            with mock.patch.object(conf, "read_bytes", new=read):
                reply, = fixture.connect(wire.ClaimFailure("unreadable-provider", assigned, "off")).replies()
                self.assertIsInstance(reply, wire.DispatchStatus)
                self.assertIsInstance(fixture.connect(wire.ViewRequest()).replies()[0], wire.ViewReply)
                denied, = fixture.server.handle_request(request)
                self.assertIsInstance(denied, wire.PlacementWait)
            key = execution_identity.attempt_key(assigned.episode, assigned.attempt)
            saved = service.read_state(fixture.state_dir).assignments[key]
            self.assertEqual(saved.state, placement.ClosedAssignment("off"))
            self.assertIsInstance(saved.terminal_report, placement.TerminalAttemptReport)
            assert isinstance(saved.terminal_report, placement.TerminalAttemptReport)
            self.assertEqual(saved.terminal_report.outcome, placement.PrelaunchRefused("off"))

    def test_final_command_results_never_retry_on_another_available_host(self) -> None:
        for status in (0, 17):
            with self.subTest(status=status), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                _ = fixture.connect(consume_permission("consume-completed", assignment, claim)).replies()
                _ = publish_candidate(fixture, request)
                _ = fixture.connect(wire.ExecutionReport("completed", assignment, claim,
                    execution_identity.ExecutionNotStarted(), placement.AttemptCompleted(status),
                    placement.DispatchPathSettled())).replies()
                fixture.restart()
                reply, = fixture.connect(request).replies()
                self.assertIsInstance(reply, wire.DispatchStatus)
                assert isinstance(reply, wire.DispatchStatus)
                self.assertIsInstance(reply.state, placement.ClosedAssignment)
                self.assertIsInstance(reply.permission, placement.PermissionConsumed)
                self.assertEqual(len(fixture.server.assignments), 1)

    def test_a_released_claim_waits_for_its_settlement_instead_of_a_final_status(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            claim = adopt_assignment(fixture, request, assignment)
            authorized, = fixture.connect(consume_permission("consume-declined", assignment, claim)).replies()
            self.assertIsInstance(authorized, wire.FirstAuthorization)
            _ = fixture.connect(wire.ExecutionReport("declined", assignment, claim, execution_identity.ExecutionNotStarted(),
                placement.AttemptDeclined("no_tests"), placement.DispatchPathSettled())).replies()
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            fixture.clock.advance(1)
            # The release evidence arrives after the pass that settles reports.
            with mock.patch.object(fixture.server, "_settle_attempt_reports"):
                _ = publish_candidate(fixture, request)
                waiting, = fixture.server.handle_request(request)
            self.assertIsInstance(waiting, wire.PlacementWait)
            self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
            _ = fixture.run_pass()
            settled = fixture.server.assignments[key]
            self.assertEqual(settled.state, placement.ClosedAssignment("no_tests"))
            self.assertNotEqual(settled.retry, placement.DoNotRetry("attempt is not settled"))
            self.assertNotIn(assignment.episode, fixture.server.current_assignments)

    def test_an_empty_scan_cannot_close_a_pending_dispatch_before_or_after_restart(self) -> None:
        cases: tuple[tuple[str, placement.AttemptOutcome], ...] = tuple(
            (pending, outcome) for pending in ("claim", "start", "cleanup")
                      for outcome in (placement.PrelaunchRefused("disk"),
                                      placement.AttemptOutcomeUnavailable("the remote start is not settled")))
        cases += (("settled-loss-stale-evidence", placement.AttemptDeclined("lost")),)
        for pending, outcome in cases:
            with self.subTest(pending=pending, outcome=type(outcome).__name__), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                authorized, = fixture.connect(consume_permission("consume-pending", assignment, claim)).replies()
                self.assertIsInstance(authorized, wire.FirstAuthorization)
                _ = publish_candidate(fixture, request)
                if pending == "settled-loss-stale-evidence":
                    fixture.clock.advance(1)
                report = wire.ExecutionReport("empty-scan", assignment, claim, execution_identity.ExecutionNotStarted(),
                    outcome, placement.DispatchPathSettled() if pending == "settled-loss-stale-evidence"
                    else placement.DispatchPathPending(pending))
                _ = fixture.connect(report).replies()
                for restart in (False, True):
                    if restart:
                        fixture.restart()
                    waiting, = fixture.connect(request).replies()
                    self.assertIsInstance(waiting, wire.PlacementWait)
                    saved = fixture.server.assignments[execution_identity.attempt_key(assignment.episode, assignment.attempt)]
                    self.assertEqual(saved.assignment, assignment)
                    self.assertIsInstance(saved.state, placement.CurrentAssignment)
                    reply, = fixture.connect(consume_permission(f"repeat-{restart}", assignment, claim)).replies()
                    self.assertIsInstance(reply, wire.DispatchStatus)
                    assert isinstance(reply, wire.DispatchStatus)
                    self.assertIsInstance(reply.permission, placement.PermissionConsumed)
                _ = publish_candidate(fixture, request)
                _ = fixture.connect(replace(report, request_id="path-settled", outcome=placement.PrelaunchRefused("disk"),
                    path=placement.DispatchPathSettled())).replies()
                if pending == "settled-loss-stale-evidence":
                    fixture.clock.advance(180)
                    _ = publish_candidate(fixture, request)
                self.assertNotEqual(assignment_for(fixture, request).attempt, assignment.attempt)

    def test_verify_hands_its_exact_claim_to_a_child_and_keeps_the_coordinator_death_check(self) -> None:
        for ending in ("live", "coordinator-ended", "adapter-ended", "adapter-release-interrupted"):
            coordinator_ends = ending == "coordinator-ended"
            with self.subTest(ending=ending), PlacementFixture() as fixture:
                program = ("import subprocess,sys; child=subprocess.Popen(['sleep','300']); "
                           "print(child.pid,flush=True); sys.stdin.readline(); child.terminate(); child.wait(); "
                           "print('adapter-ended',flush=True); sys.stdin.read()")
                with subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as coordinator:
                    try:
                        if coordinator.stdout is None:
                            self.fail("The private coordinator has no output pipe")
                        output = cast(TextIO, coordinator.stdout)
                        self.assertTrue(select.select([output], [], [], TEST_HANG_GUARD_SECS)[0])
                        holder_pid = int(output.readline())
                        owner_start = records.process_start_time(coordinator.pid)
                        holder_start = records.process_start_time(holder_pid)
                        if owner_start is None or holder_start is None:
                            self.fail("The private process tree has no checked start identity")
                        fixture.proc.register(wire.Owner(coordinator.pid, owner_start))
                        fixture.proc.register(wire.Owner(holder_pid, holder_start))
                        write_process(fixture.proc.path, holder_pid, parent=coordinator.pid, start=holder_start)
                        request, repository = mac_claim_request(fixture)
                        request = replace(request, owner=placement.SubmittingOwner(
                            fixture.server.host, coordinator.pid, owner_start), run=wire.RunIdentity(coordinator.pid, owner_start))
                        _ = publish_candidate(fixture, request)
                        assignment = assignment_for(fixture, request)
                        fixture.enter_context(mock.patch.dict(os.environ, {"MAC_TEST_STATE_DIR": str(fixture.root / "provider")}))
                        paths = mac_test.state_paths("mac")
                        settings = fixture.root / "mac_test.conf"
                        _ = write_config_fixture(settings,
                            "offload=on\nlinux_only.hana=\nmac_skip.hana=\n", template="mac_test")
                        args = mac_test.CliArguments()
                        args.pid, args.worktree, args.what = coordinator.pid, str(repository), "nextest hana"
                        args.repo_root, args.package, args.settings = str(repository), "hana", str(settings)
                        args.target = "mac"
                        with mock.patch.object(mac_test, "start_claim_watcher"), \
                             redirect_stdout(io.StringIO()):
                            self.assertEqual(mac_test.command_claim_wait(args, paths,
                                session=assignment.session, assignment=assignment), 0)
                            reserved, = mac_test.read_claims(paths)
                            identity = claim_state.claim_identity(reserved.record)
                            args.pid, args.from_pid, args.remote = holder_pid, coordinator.pid, assignment.destination.host
                            args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
                            self.assertEqual(mac_test.command_claim(args, paths, watch_claim=False,
                                session=assignment.session, assignment=assignment), 0)
                        transferred, = mac_test.read_claims(paths)
                        self.assertEqual(claim_state.claim_identity(transferred.record), identity)
                        self.assertEqual(transferred.record["pid"], holder_pid)
                        self.assertNotEqual(holder_pid, request.owner.pid)
                        holder = placement.ClaimHolderIdentity(fixture.server.host, holder_pid, holder_start)
                        reference = placement.ProviderClaimReference(assignment.destination.provider,
                            assignment.destination.target, assignment.destination.scope,
                            identity.claim_id, identity.episode, identity.attempt)
                        adopted = placement.AdoptedProviderClaim(reference, holder, assignment.session,
                            assignment.destination.host, "host-generation-a")
                        _ = publish_candidate(fixture, request, claims=(adopted,))
                        before = transferred.path.read_bytes()
                        if coordinator_ends:
                            _ = coordinator.communicate("", timeout=TEST_HANG_GUARD_SECS)
                        _ = fixture.run_pass()
                        reply, = fixture.connect(wire.ConsumePermission("child-consumption", assignment,
                            reference, holder=holder)).replies()
                        self.assertIsInstance(reply, wire.DispatchStatus if coordinator_ends else wire.FirstAuthorization)
                        self.assertEqual(transferred.path.read_bytes(), before)
                        if not coordinator_ends:
                            saved = fixture.server.assignments[execution_identity.attempt_key(assignment.episode, assignment.attempt)]
                            self.assertEqual(saved.holder, holder)
                        if ending in ("adapter-ended", "adapter-release-interrupted"):
                            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                            saved = fixture.server.assignments[key]
                            self.assertIsNone(coordinator.poll())
                            fixture.clock.advance(1)
                            _ = publish_candidate(fixture, request)
                            _ = fixture.run_pass()
                            waiting, = fixture.connect(request).replies()
                            self.assertIsInstance(waiting, wire.PlacementWait)
                            self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                            assert coordinator.stdin is not None
                            _ = coordinator.stdin.write("end adapter\n")
                            coordinator.stdin.flush()
                            self.assertTrue(select.select([output], [], [], TEST_HANG_GUARD_SECS)[0])
                            self.assertEqual(output.readline(), "adapter-ended\n")
                            self.assertIsNone(records.process_start_time(holder_pid))
                            self.assertIsNone(coordinator.poll())
                            self.assertIsInstance(saved.permission, placement.PermissionConsumed)
                            assert isinstance(saved.permission, placement.PermissionConsumed)
                            evidence_key = fixture.server.host_evidence_key(request, assignment.destination)
                            stale = replace(fixture.provider_evidence[evidence_key],
                                            observed_at=saved.permission.consumed_at)
                            fixture.provider_evidence[evidence_key] = stale
                            fixture.server.publish_host_evidence(stale)
                            _ = fixture.run_pass()
                            self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                            fixture.clock.advance(1)
                            _ = publish_candidate(fixture, request, claims=(adopted,))
                            _ = fixture.run_pass()
                            self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                            scope_key = claim_state.claim_fence_scope_key(transferred.path, transferred.record)
                            pending = claim_state.claim_pending_start(paths, transferred.path, transferred.record)
                            pending.parent.mkdir(parents=True, exist_ok=True)
                            _ = pending.write_text("pending startup\n")
                            _ = publish_candidate(fixture, request)
                            _ = fixture.run_pass()
                            self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                            if ending == "adapter-release-interrupted":
                                command = offload.prepare_start_fence_command(identity, "mac", scope_key=scope_key)
                                prepared = subprocess.run(["bash", "-c", command],
                                    capture_output=True, text=True, check=False, timeout=5)
                                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                                release_program = """
import os
from pathlib import Path
from pathlib import Path
import signal
import socket
import subprocess
import sys
from unittest import mock
from scripts.mac_test import mac_test
paths = mac_test.StatePaths(Path(sys.argv[1]))
args = mac_test.CliArguments()
args.pid, args.target, args.settings = int(sys.argv[2]), "mac", sys.argv[3]
args.claim_id, args.episode, args.attempt = sys.argv[4:7]
claim = Path(sys.argv[7])
real_run, real_unlink = subprocess.run, Path.unlink

def private_transport(arguments, **options):
    return real_run(["bash", "-c", arguments[-1]], capture_output=True,
                    text=True, check=False, timeout=5)

def kill_before_claim_unlink(path, missing_ok=False):
    if path == claim:
        os.kill(os.getpid(), signal.SIGKILL)
    real_unlink(path, missing_ok=missing_ok)

with mock.patch.object(subprocess, "run", side_effect=private_transport), \
     mock.patch.object(Path, "unlink", kill_before_claim_unlink):
    raise SystemExit(mac_test.command_release(args, paths))
"""
                                interrupted = subprocess.run([sys.executable, "-c", release_program,
                                    str(paths.directory), str(args.pid), str(settings), identity.claim_id,
                                    identity.episode, identity.attempt, str(transferred.path)],
                                    capture_output=True, text=True, check=False, timeout=5)
                                self.assertEqual(interrupted.returncode, -signal.SIGKILL,
                                                 interrupted.stdout + interrupted.stderr)
                                self.assertFalse(pending.exists())
                                self.assertEqual(transferred.path.read_bytes(), before)
                                fence = fixture.root / offload.start_fence_relative(identity, "mac", scope_key=scope_key).with_suffix(".lock")
                                self.assertFalse(fence.exists())
                                _ = publish_candidate(fixture, request, claims=(adopted,))
                                _ = fixture.run_pass()
                                self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                                with mock.patch.object(subprocess, "run", side_effect=AssertionError(
                                        "record-free cleanup must not start SSH")):
                                    self.assertEqual(mac_test.command_release(args, paths), 0)
                                self.assertIsInstance(mac_test.read_run(transferred.path), mac_test.NoRun)
                                _ = publish_candidate(fixture, request)
                            else:
                                pending.unlink()
                            _ = fixture.run_pass()
                            closed = fixture.server.assignments[key]
                            self.assertIsInstance(closed.state, placement.ClosedAssignment)
                            self.assertEqual(closed.terminal_report, placement.TerminalAttemptReport(
                                placement.AttemptDeclined("lost"), placement.DispatchPathSettled(), fixture.clock.now()))
                            self.assertIsNone(coordinator.poll())
                            self.assertEqual(len(fixture.server.assignments), 1)
                            fixture.clock.advance(180)
                            _ = publish_candidate(fixture, request)
                            replacement = assignment_for(fixture, request)
                            self.assertNotEqual(replacement.attempt, assignment.attempt)
                            self.assertEqual(replacement.owner, assignment.owner)
                            self.assertEqual(replacement.since, assignment.since)
                    finally:
                        if coordinator.poll() is None:
                            _ = coordinator.communicate("", timeout=TEST_HANG_GUARD_SECS)

    def test_reader_assigns_supported_profile_steps_and_refuses_unknown_steps_for_good(self) -> None:
        for step in ("nextest", "clippy", "mend-check", "mend", "check", "doc", "build", "unknown"):
            with self.subTest(step=step), PlacementFixture() as fixture:
                repository = fixture.root / "hana"
                (repository / ".git").mkdir(parents=True)
                _ = (fixture.root / "build_targets.conf").write_text(
                    "targets=box\nkind.box=ssh\nplatform.box=linux\nsettings.box=box.conf\n" + UNREACHABLE_WAIT_FIXTURE)
                _ = write_config_fixture(fixture.root / "box.conf",
                    "offload=on\nhost=fixture-box\nkinds=test,lint,check,doc,build\nslots=2\n", template="mac_test")
                request = placement_request(fixture)
                assert isinstance(request.profile, records.StepProfile)
                destination = placement.PlacementDestination("ssh", "box", "fixture-box", RepositoryScope("hana"))
                request = replace(request, profile=replace(request.profile, step=step), constraints=replace(
                    request.constraints, repository=str(repository), candidates=(placement.PlacementCandidate(destination),)))
                key = fixture.server.host_evidence_key(request, destination)
                fixture.server.host_requests[key] = request

                def ssh_reply(arguments: Sequence[str],
                              deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CommandOutput:
                    self.assertEqual((arguments[0], arguments[-2], arguments[-1]), ("ssh", "fixture-box", "true"))
                    self.assertIsInstance(deadline, offload.CommandTimeLimit)
                    return offload.CommandOutput(0, "", "")

                with mock.patch.object(offload.ProcessController, "capture", side_effect=ssh_reply):
                    evidence = fixture.read_host_evidence(key)
                fixture.server.publish_host_evidence(evidence)
                if step == "unknown":
                    assert isinstance(evidence, placement.FreshHostEvidence)
                    self.assertEqual(evidence.eligibility, placement.FixedExclusion("command"))
                    reply, = fixture.server.handle_request(request)
                    assert isinstance(reply, wire.DispatchStatus)
                    self.assertEqual(reply.retry, placement.DoNotRetry("command"))
                else:
                    self.assertEqual(assignment_for(fixture, request).destination, destination)

    def target_settings_fixture(self, fixture: PlacementFixture, *, platform: str = "linux"
                                ) -> tuple[Path, wire.PlacementRequest, placement.PlacementDestination]:
        _ = (fixture.root / "build_targets.conf").write_text(
            f"targets=fixture,healthy\nkind.fixture=ssh\nplatform.fixture={platform}\nsettings.fixture=target.conf\n"
            + "kind.healthy=ssh\nplatform.healthy=linux\nsettings.healthy=mac_test.conf\n" + UNREACHABLE_WAIT_FIXTURE)
        settings = fixture.root / "target.conf"
        _ = write_config_fixture(settings, "host=fixture\nexecution_transport=ssh\n"
                                 + "probe_timeout_s=10\nslots=1\nkinds=check\n", template="mac_test")
        repository = fixture.root / "hana"
        (repository / ".git").mkdir(parents=True)
        request = placement_request(fixture)
        destination = replace(request.constraints.candidates[0].destination,
                              provider="mac" if platform == "macos" else "ssh", target="fixture", host="fixture")
        request = replace(request, constraints=replace(request.constraints, repository=str(repository),
            command_kind="check", candidates=(placement.PlacementCandidate(destination),)))
        return settings, request, destination

    def commit_settings(self, settings: Path) -> None:
        git = ("git", "-C", str(settings.parent))
        _ = subprocess.run((*git, "init", "-q"), check=True)
        _ = subprocess.run((*git, "add", settings.name), check=True)
        _ = subprocess.run((*git, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                            "commit", "-qm", "Save the usable settings"), check=True)

    def read_target(self, fixture: PlacementFixture, request: wire.PlacementRequest) -> placement.HostEvidence:
        key = fixture.server.host_evidence_key(request, request.constraints.candidates[0].destination)
        fixture.server.host_requests[key] = request
        return fixture.read_host_evidence(key)

    def test_bad_needed_key_uses_the_committed_value_with_one_anomaly(self) -> None:
        with PlacementFixture() as fixture, fixture.loop_turn():
            settings, request, destination = self.target_settings_fixture(fixture)
            self.commit_settings(settings)
            _ = settings.write_text(settings.read_text().replace("execution_transport=ssh", "execution_transport=broken"))
            warnings = io.StringIO()
            with (redirect_stderr(warnings), mock.patch.object(offload.ProcessController, "capture",
                                                               return_value=offload.CommandOutput(0, "", ""))):
                evidence = self.read_target(fixture, request)
                fixture.server.publish_host_evidence(evidence)
                self.assertEqual(assignment_for(fixture, request).destination, destination)
            self.assertEqual(warnings.getvalue().count("execution_transport='broken'"), 1)

    def test_bad_needed_key_only_excludes_its_target(self) -> None:
        with PlacementFixture() as fixture, fixture.loop_turn():
            settings, request, destination = self.target_settings_fixture(fixture)
            _ = settings.write_text(settings.read_text().replace("probe_timeout_s=10", "probe_timeout_s=broken"))
            healthy = replace(destination, target="healthy")
            healthy_only = replace(request, constraints=replace(request.constraints,
                candidates=(placement.PlacementCandidate(healthy),)))
            request = replace(request, constraints=replace(request.constraints,
                candidates=(*request.constraints.candidates, *healthy_only.constraints.candidates)))
            with redirect_stderr(io.StringIO()):
                evidence = self.read_target(fixture, request)
                assert isinstance(evidence, placement.HostEvidenceUnavailable), evidence
                self.assertIn("probe_timeout_s='broken'", evidence.reason)
                _ = publish_candidate(fixture, healthy_only)
                self.assertEqual(assignment_for(fixture, request).destination, healthy)
            unavailable = fixture.server.host_evidence_key(request, destination)
            self.assertNotIn(unavailable, fixture.server.host_readers)

    def test_hana_retry_survives_bad_registered_transport(self) -> None:
        with PlacementFixture() as fixture, fixture.loop_turn():
            registered, request, destination = self.target_settings_fixture(fixture)
            _ = registered.write_text("execution_transport=broken\n")
            settings = fixture.root / "explicit.conf"
            _ = write_config_fixture(settings, "enabled=on\n", template="hana_aws")
            candidate = placement.PlacementCandidate(destination, settings=placement.ExplicitProviderSettings(settings))
            request = replace(request, constraints=replace(request.constraints, candidates=(candidate,)))
            before = fixture.server.host_evidence_key(request, destination)
            _ = (fixture.root / "aws_target.conf").write_text("changed=1\n")
            self.assertNotEqual(fixture.server.host_evidence_key(request, destination), before)
            starting = placement.TemporaryExclusion("starting")
            fixture.server.placement_eligibility[request.episode] = (placement.CandidateEligibility(destination, starting),)
            reply_to = cast(Callable[[wire.PlacementRequest, placement.ResourceHolder], wire.Reply],
                            getattr(fixture.server, "_placement_reply"))

            reply = reply_to(request, placement.ResourceHolderUnknown())

            assert isinstance(reply, wire.PlacementWait)
            self.assertEqual([(notice.target, notice.reason) for notice in reply.retry_notices], [("fixture", "starting")])
            _ = publish_candidate(fixture, request, starting)
            self.assertIsInstance(fixture.server.handle_request(request)[0], wire.PlacementWait)

    def test_bad_probe_timeout_leaves_no_subscription_after_pruning(self) -> None:
        with PlacementFixture() as fixture, mock.patch.object(service, "HostEvidenceWorker", ManualEvidenceWorker):
            settings, request, destination = self.target_settings_fixture(fixture)
            good = settings.read_text()
            bad = fixture.server.host_evidence_key(request, destination)
            _ = settings.write_text(good.replace("probe_timeout_s=10", "probe_timeout_s=broken"))
            reader = cast(Callable[[placement.HostEvidenceKey, float], service.HostEvidenceReader],
                          getattr(fixture.server, "_host_evidence_reader"))
            prune = cast(Callable[[], None], getattr(fixture.server, "_prune_host_evidence"))
            with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
                _ = reader(bad, 1)
            _ = settings.write_text(good.replace("probe_timeout_s=10", "probe_timeout_s=12"))
            repaired = fixture.server.host_evidence_key(request, destination)
            fresh = reader(repaired, 1)
            fixture.server.placement_requests[request.episode] = request
            fixture.server.placement_host_keys[(request.episode, destination)] = repaired
            prune()
            self.assertEqual(set(fresh.worker.subscriptions), {repaired})

    def test_hana_probe_recovers_an_overflowing_timeout(self) -> None:
        with (PlacementFixture() as fixture, fixture.loop_turn(), redirect_stderr(io.StringIO()) as warnings,
              mock.patch.object(service, "HostEvidenceWorker", ManualEvidenceWorker)):
            settings, request, destination = self.target_settings_fixture(fixture)
            good = ("execution_transport=hana-control\nhost=fixture\n"
                    "# whole: 1..\naws_timeout_s=7\n# whole: 1..\nstep_memory_gib=24\n")
            _ = settings.write_text(good)
            self.commit_settings(settings)
            _ = settings.write_text(good.replace("aws_timeout_s=7", "aws_timeout_s=" + "9" * 400))

            reply, = fixture.server.handle_request(request)

            self.assertIsInstance(reply, wire.PlacementWait)
            key = fixture.server.host_evidence_key(request, destination)
            self.assertEqual(fixture.server.host_readers[key].timeout, 7.0)
            self.assertEqual(warnings.getvalue().count("aws_timeout_s="), 1)

    def test_repaired_key_reports_the_same_bad_value_again(self) -> None:
        with PlacementFixture() as fixture:
            settings, request, destination = self.target_settings_fixture(fixture)
            self.commit_settings(settings)
            good = settings.read_text()
            bad = good.replace("probe_timeout_s=10", "probe_timeout_s=broken")
            timeout = cast(Callable[[placement.HostEvidenceKey], float], getattr(fixture.server, "_host_probe_timeout"))
            with redirect_stderr(io.StringIO()) as warnings:
                for contents in (bad, good, bad):
                    _ = settings.write_text(contents)
                    key = fixture.server.host_evidence_key(request, destination)
                    self.assertEqual(timeout(key), 10)
                    self.assertEqual(timeout(key), 10)
            self.assertEqual(warnings.getvalue().count("probe_timeout_s='broken'"), 2)

    def test_mac_evidence_key_ignores_aws_settings(self) -> None:
        from scripts.build_targets import target_config

        with PlacementFixture() as fixture:
            _settings, request, destination = self.target_settings_fixture(fixture, platform="macos")
            before = fixture.server.host_evidence_key(request, destination)
            _ = (fixture.root / "aws_target.conf").write_text("changed=1\n")
            with mock.patch.object(target_config, "read_execution_transport", wraps=target_config.read_execution_transport) as read:
                self.assertEqual(fixture.server.host_evidence_key(request, destination), before)
            read.assert_not_called()

    def idle_hana(self, fixture: PlacementFixture) -> tuple[Path, wire.PlacementRequest]:
        from scripts.aws_target import hana_box
        from scripts.aws_target.test_hana_box import record_lifecycle_image

        settings, request, _destination = self.target_settings_fixture(fixture)
        settings.unlink()
        _ = write_config_fixture(settings, "enabled=on\nrepository_identity=https://github.com/example/hana\n",
                                 template="hana_aws")
        assert isinstance(request.profile, records.StepProfile)
        request = replace(request, profile=replace(request.profile, step="check"),
            constraints=replace(request.constraints, command=("cargo", "check")))
        account = fixture.root / "aws_target.conf"
        _ = write_config_fixture(account, template="aws_target")
        controller = hana_box.HanaController(fixture.root / "hana-state", account, settings,
                                             hana_box.ProviderNotInstalled())
        identity = hana_box.DeploymentIdentity("fixture", "box", "spot", "us-east-2")
        controller.register(identity)
        _ = record_lifecycle_image(controller.directory, settings, identity=identity)
        from scripts.aws_target import hana_placement_adapter
        from scripts.mac_test import eligibility

        _ = fixture.enter_context(mock.patch.object(hana_placement_adapter, "controller_for", return_value=controller))
        _ = fixture.enter_context(mock.patch.object(eligibility, "repository_identity",
            return_value=eligibility.KnownRepository("hana", "https://github.com/example/hana")))
        return settings, request

    def test_hana_probe_keeps_the_workload_used_to_select_transport(self) -> None:
        from scripts.aws_target import hana_spend
        from scripts.build_targets import target_config

        with PlacementFixture() as fixture:
            settings, request = self.idle_hana(fixture)
            destination = request.constraints.candidates[0].destination
            key = fixture.server.host_evidence_key(request, destination)
            fixture.server.host_requests[key] = request
            read_transport = target_config.read_execution_transport

            def edit_workload(target: target_config.SshTarget, *,
                              configuration: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT
                              ) -> hana.ExecutionTransport:
                transport = read_transport(target, configuration=configuration)
                if isinstance(transport, hana.HanaControlTransport):
                    _ = write_config_fixture(settings, "execution_transport=ssh\nguard_grace_seconds=59\n")
                return transport

            with (mock.patch.object(target_config, "read_execution_transport", side_effect=edit_workload) as read,
                  mock.patch.object(hana_spend.HanaSpending, "preview_start", return_value=hana_spend.StartAffordable(0))):
                evidence = fixture.read_host_evidence(key)
            assert isinstance(evidence, placement.FreshHostEvidence), evidence
            self.assertEqual(evidence.eligibility, ALLOWED_HOST)
            read.assert_called_once()
            self.assertEqual(conf.read(settings).text("execution_transport"), "ssh")

    def test_hana_probe_reports_the_path_and_key_for_duplicate_fallback_types(self) -> None:
        with PlacementFixture() as fixture, redirect_stderr(io.StringIO()):
            settings, request = self.idle_hana(fixture)
            duplicate = "c7a.4xlarge,c7a.4xlarge"
            _ = write_config_fixture(settings, f"fallback_instance_types={duplicate}\n")

            evidence = self.read_target(fixture, request)

            assert isinstance(evidence, placement.HostEvidenceUnavailable), evidence
            self.assertIn(f"{settings}: fallback_instance_types={duplicate!r}", evidence.reason)

    def test_idle_hana_preview_uses_the_recovered_permit(self) -> None:
        from scripts.aws_target import hana_box, hana_spend

        with PlacementFixture() as fixture:
            settings, request = self.idle_hana(fixture)
            good = settings.read_text()
            window = conf.read(settings).whole("permit_max_seconds")
            self.commit_settings(settings)
            _ = settings.write_text(good.replace(f"permit_max_seconds={window}", "permit_max_seconds=broken"))
            warnings = io.StringIO()

            def preview(spending: hana_spend.HanaSpending[hana_spend.StartPermitSettings],
                        now: int, expires: int) -> hana_spend.StartAffordable:
                self.assertTrue(spending.settings.enabled)
                self.assertEqual(expires - now, spending.settings.permit_max_seconds)
                return hana_spend.StartAffordable(expires)

            with (redirect_stderr(warnings),
                  mock.patch.object(hana_box.HanaController, "image_is_current", return_value=True),
                  mock.patch.object(hana_spend.HanaSpending, "preview_start",
                                                               autospec=True, side_effect=preview) as start):
                evidence = self.read_target(fixture, request)
                assert isinstance(evidence, placement.FreshHostEvidence), evidence
                self.assertEqual(evidence.eligibility, ALLOWED_HOST)
                start.assert_called_once()
                self.assertEqual(warnings.getvalue().count("permit_max_seconds='broken'"), 1)
                self.assertEqual(hana_spend.read_settings(conf.read(settings)).permit_max_seconds, window)

                _ = settings.write_text(good.replace("mount_timeout_seconds=15", "mount_timeout_seconds=broken"))
                with mock.patch.object(conf, "_committed_files", return_value=()):
                    evidence = self.read_target(fixture, request)
                    assert isinstance(evidence, placement.FreshHostEvidence), evidence
                    self.assertEqual(evidence.eligibility, ALLOWED_HOST)
                    self.assertEqual(start.call_count, 2)
                    with self.assertRaisesRegex(conf.Unusable, "mount_timeout_seconds"):
                        _ = hana_spend.read_settings(conf.read(settings))

                # An unattended reader counts an invalid switch as off; it does not recover the committed value.
                _ = settings.write_text(good.replace("enabled=on", "enabled=broken"))
                evidence = self.read_target(fixture, request)
                assert isinstance(evidence, placement.FreshHostEvidence), evidence
                self.assertEqual(evidence.eligibility, placement.FixedExclusion("off"))
                self.assertEqual(start.call_count, 2)

    def test_linux_kinds_recovers_the_committed_command(self) -> None:
        with PlacementFixture() as fixture:
            settings, request, _destination = self.target_settings_fixture(fixture)
            self.commit_settings(settings)
            _ = settings.write_text(settings.read_text().replace("kinds=check", "kinds=broken"))
            with (redirect_stderr(io.StringIO()) as warnings,
                  mock.patch.object(offload.ProcessController, "capture", return_value=offload.CommandOutput(0, "", ""))):
                evidence = self.read_target(fixture, request)
            assert isinstance(evidence, placement.FreshHostEvidence), evidence
            self.assertEqual(evidence.eligibility, ALLOWED_HOST)
            self.assertEqual(warnings.getvalue().count("kinds='broken'"), 1)

    def test_overlapping_snapshots_report_each_bad_edit_once(self) -> None:
        with PlacementFixture() as fixture, conf.report_key_changes(), redirect_stderr(io.StringIO()) as warnings:
            settings, _request, _destination = self.target_settings_fixture(fixture)
            self.commit_settings(settings)
            good = settings.read_text()
            bad = good.replace("probe_timeout_s=10", "probe_timeout_s=broken")
            old_valid = conf.read(settings)
            _ = settings.write_text(bad)
            old_bad = conf.read(settings)
            self.assertEqual(old_bad.number("probe_timeout_s"), 10)
            self.assertEqual(old_valid.number("probe_timeout_s"), 10)
            self.assertEqual(conf.read(settings).number("probe_timeout_s"), 10)
            self.assertEqual(warnings.getvalue().count("probe_timeout_s='broken'"), 1)

            _ = settings.write_text(good)
            self.assertEqual(conf.read(settings).number("probe_timeout_s"), 10)
            _ = settings.write_text(bad)
            self.assertEqual(conf.read(settings).number("probe_timeout_s"), 10)
            self.assertEqual(old_bad.number("probe_timeout_s"), 10)
            self.assertEqual(warnings.getvalue().count("probe_timeout_s='broken'"), 2)

    def test_older_request_without_a_reader_cannot_hold_a_newer_reading(self) -> None:
        with PlacementFixture() as fixture, fixture.loop_turn(), redirect_stderr(io.StringIO()):
            settings, older, destination = self.target_settings_fixture(fixture)
            _ = settings.write_text(settings.read_text().replace("probe_timeout_s=10", "probe_timeout_s=broken"))
            server = fixture.server
            request_evidence = cast(Callable[[wire.PlacementRequest, placement.PlacementDestination], placement.HostEvidence],
                                    getattr(server, "_request_host_evidence"))
            self.assertIsInstance(request_evidence(older, destination), placement.HostEvidenceUnavailable)
            older_key = server.placement_host_keys[(older.episode, destination)]
            self.assertNotIn(older_key, server.host_readers)
            candidate = placement.CandidateEligibility(destination, placement.EvidenceNeeded("probe unavailable"))
            make_hold = cast(Callable[[wire.PlacementRequest, placement.CandidateEligibility,
                                      dict[tuple[str, placement.PlacementDestination], float]], service.PlacementHold | None],
                             getattr(server, "_placement_hold"))
            hold = make_hold(older, candidate, {})

            fixture.clock.advance(1)
            younger = replace(older, episode="younger",
                constraints=replace(older.constraints, session=OwningSession("younger")))
            evidence = publish_candidate(fixture, younger)
            server.placement_host_keys[(younger.episode, destination)] = evidence.key
            held_for_older = cast(Callable[[wire.PlacementRequest, placement.PlacementDestination, list[service.PlacementHold],
                dict[tuple[str, placement.PlacementDestination], float]], bool], getattr(server, "_held_for_older"))
            self.assertFalse(held_for_older(younger, destination, [hold] if hold is not None else [], {}))
            self.assertIsNone(hold)

    def client_constraints(self, request: wire.PlacementRequest) -> placement.PlacementConstraints:
        from scripts.build_targets import build_targets

        arguments = request.constraints
        assert isinstance(arguments.command_kind, str)
        return build_targets.fixed_placement_constraints(Path(arguments.repository), arguments.package,
            arguments.command_kind, arguments.command, arguments.session, arguments.source_key)

    def test_fixed_candidates_ignore_bad_cleanup_settings(self) -> None:
        with PlacementFixture() as fixture, redirect_stderr(io.StringIO()) as warnings:
            settings, request, _destination = self.target_settings_fixture(fixture)
            before = self.client_constraints(request)
            timeout = conf.read(settings).text("leftover_timeout_s")
            _ = settings.write_text(settings.read_text().replace(
                f"leftover_timeout_s={timeout}", "leftover_timeout_s=broken"))
            self.assertEqual(self.client_constraints(request), before)
            self.assertTrue(any(candidate.destination.target == "fixture" and candidate.eligibility == ALLOWED_HOST
                                for candidate in before.candidates))
            self.assertEqual(warnings.getvalue(), "")

    def test_bad_registry_neighbor_does_not_stop_healthy_placement(self) -> None:
        from scripts.build_targets import build_targets, target_config

        with PlacementFixture() as fixture, redirect_stderr(io.StringIO()):
            settings, request, destination = self.target_settings_fixture(fixture)
            healthy_settings = fixture.root / "mac_test.conf"
            _ = write_config_fixture(healthy_settings, "host=healthy\nkinds=check\n")
            destination = replace(destination, target="healthy", host="healthy")
            request = replace(request, constraints=replace(request.constraints,
                candidates=(placement.PlacementCandidate(destination),)))
            registry = fixture.root / "build_targets.conf"

            def check_healthy() -> None:
                target = records.effective_target("healthy", placement.ConfiguredProviderSettings())
                self.assertEqual(target, target_config.SshTarget("healthy", "linux", healthy_settings))
                self.assertEqual(claim_state.state_paths("healthy").platform, "linux")
                with mock.patch.object(offload.ProcessController, "capture", return_value=offload.CommandOutput(0, "", "")):
                    evidence = self.read_target(fixture, request)
                assert isinstance(evidence, placement.FreshHostEvidence), evidence
                self.assertEqual(evidence.eligibility, ALLOWED_HOST)
                self.assertTrue(any(candidate.destination.target == "healthy" and candidate.eligibility == ALLOWED_HOST
                                    for candidate in self.client_constraints(request).candidates))
                args = build_targets.CliArguments()
                args.target, args.kind, args.repo_root = "healthy", "check", request.constraints.repository
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(build_targets.command_constraints(args), 0)
                self.assertIn("healthy\teligible", output.getvalue())

            settings.unlink()
            with self.subTest(neighbor="missing file"):
                check_healthy()
            _ = registry.write_text(registry.read_text().replace("platform.fixture=linux", "platform.fixture=broken"))
            with self.subTest(neighbor="invalid registry entry"):
                check_healthy()

    def test_client_and_service_recover_the_same_linux_policy_and_capacity(self) -> None:
        with PlacementFixture() as fixture, redirect_stderr(io.StringIO()) as warnings:
            settings, request, _destination = self.target_settings_fixture(fixture)
            _ = settings.write_text(settings.read_text() + "\nslots.hana=2\n")
            self.commit_settings(settings)
            _ = settings.write_text(settings.read_text().replace("slots.hana=2", "slots.hana=broken")
                                    .replace("kinds=check", "kinds=broken"))
            candidates = [candidate for candidate in self.client_constraints(request).candidates
                          if candidate.destination.target == "fixture"]
            self.assertEqual([candidate.destination.slot for candidate in candidates], [1, 2])
            self.assertTrue(all(candidate.eligibility == ALLOWED_HOST for candidate in candidates))
            with mock.patch.object(offload.ProcessController, "capture", return_value=offload.CommandOutput(0, "", "")):
                evidence = self.read_target(fixture, request)
            assert isinstance(evidence, placement.FreshHostEvidence), evidence
            self.assertEqual(evidence.eligibility, ALLOWED_HOST)
            self.assertEqual(warnings.getvalue().count("slots.hana='broken'"), 1)
            self.assertEqual(warnings.getvalue().count("kinds='broken'"), 1)

    def test_hana_preview_recovers_budget_and_ignores_guest_guard(self) -> None:
        from decimal import Decimal
        from scripts.aws_target import hana_box, hana_spend

        with PlacementFixture() as fixture:
            settings, request = self.idle_hana(fixture)
            good = settings.read_text()
            self.commit_settings(settings)

            def preview(spending: hana_spend.HanaSpending[hana_spend.StartPermitSettings],
                        _now: int, expires: int) -> hana_spend.StartAffordable:
                self.assertEqual(spending.settings.daily_budget_usd, Decimal(6))
                return hana_spend.StartAffordable(expires)

            with mock.patch.object(hana_spend.HanaSpending, "preview_start", autospec=True, side_effect=preview) as start:
                _ = settings.write_text(good.replace("daily_budget_usd=6", "daily_budget_usd=broken"))
                with redirect_stderr(io.StringIO()) as warnings:
                    budget = self.read_target(fixture, request)
                assert isinstance(budget, placement.FreshHostEvidence), budget
                self.assertEqual(budget.eligibility, ALLOWED_HOST)
                self.assertEqual(warnings.getvalue().count("daily_budget_usd='broken'"), 1)
                start.assert_called_once()

                _ = settings.write_text(good.replace("guard_grace_seconds=60", "guard_grace_seconds=broken"))
                with redirect_stderr(io.StringIO()):
                    stale = self.read_target(fixture, request)
                assert isinstance(stale, placement.FreshHostEvidence), stale
                self.assertEqual(stale.eligibility, placement.FixedExclusion(placement.HANA_IMAGE_STALE))
                start.assert_called_once()

                with (mock.patch.object(hana_box.HanaController, "image_is_current", return_value=True),
                      redirect_stderr(io.StringIO()) as warnings):
                    guest = self.read_target(fixture, request)
                assert isinstance(guest, placement.FreshHostEvidence), guest
                self.assertEqual(guest.eligibility, ALLOWED_HOST)
                self.assertNotIn("guard_grace_seconds='broken'", warnings.getvalue())
                self.assertEqual(start.call_count, 2)

    def test_hana_client_and_service_recover_the_same_route_limit(self) -> None:
        from scripts.aws_target import hana_spend

        with PlacementFixture() as fixture, redirect_stderr(io.StringIO()) as warnings:
            settings, request = self.idle_hana(fixture)
            self.commit_settings(settings)
            _ = settings.write_text(settings.read_text().replace("# whole: 1..1\nmax_steps=1", "# whole: 1..\nmax_steps=2"))
            candidates = self.client_constraints(request).candidates
            self.assertTrue(any(candidate.destination.target == "fixture" and candidate.eligibility == ALLOWED_HOST
                                for candidate in candidates))
            with mock.patch.object(hana_spend.HanaSpending, "preview_start", return_value=hana_spend.StartAffordable(0)):
                evidence = self.read_target(fixture, request)
            assert isinstance(evidence, placement.FreshHostEvidence), evidence
            self.assertEqual(evidence.eligibility, ALLOWED_HOST)
            self.assertEqual(warnings.getvalue().count("max_steps='2'"), 1)

    def test_mac_probe_recovers_the_committed_mirror_root(self) -> None:
        with PlacementFixture() as fixture, redirect_stderr(io.StringIO()) as warnings:
            settings, request, _destination = self.target_settings_fixture(fixture, platform="macos")
            _ = write_config_fixture(settings, "linux_only.hana=\n")
            mac_settings = fixture.root / "mac_test.conf"
            _ = write_config_fixture(mac_settings, "mirror_root=~/fixture-mirrors\n")
            self.commit_settings(mac_settings)
            _ = mac_settings.write_text(mac_settings.read_text().replace(
                "mirror_root=~/fixture-mirrors", "mirror_root=/outside"))
            reply = offload.CommandOutput(0, "free_gib=1000\nhost=fixture\nrc=0\n", "")
            with mock.patch.object(offload.ProcessController, "capture", return_value=reply) as probe:
                evidence = self.read_target(fixture, request)
            assert isinstance(evidence, placement.FreshHostEvidence), evidence
            self.assertEqual(evidence.eligibility, ALLOWED_HOST)
            self.assertIn("fixture-mirrors/hana", str(probe.call_args))
            self.assertEqual(warnings.getvalue().count("mirror_root='/outside'"), 1)

    def test_edit_during_read_uses_the_stamp_of_the_read_contents(self) -> None:
        with PlacementFixture() as fixture, conf.report_key_changes(), redirect_stderr(io.StringIO()) as warnings:
            settings, _request, _destination = self.target_settings_fixture(fixture)
            self.commit_settings(settings)
            bad = settings.read_text().replace("probe_timeout_s=10", "probe_timeout_s=broken")
            with settings.open(encoding="utf-8") as stream:
                read = stream.read

                def edit_and_read() -> str:
                    with io.open(settings, "w", encoding="utf-8") as changed:
                        _ = changed.write(bad)
                    return read()

                with (mock.patch.object(Path, "open", return_value=stream),
                      mock.patch.object(stream, "read", side_effect=edit_and_read)):
                    first = conf.read(settings)

            self.assertEqual(first.number("probe_timeout_s"), 10)
            self.assertEqual(conf.read(settings).number("probe_timeout_s"), 10)
            self.assertEqual(warnings.getvalue().count("probe_timeout_s='broken'"), 1)

    def test_bad_probe_timeout_leaves_no_request_after_pruning(self) -> None:
        with PlacementFixture() as fixture, fixture.loop_turn(), redirect_stderr(io.StringIO()):
            settings, request, destination = self.target_settings_fixture(fixture)
            _ = settings.write_text(settings.read_text().replace("probe_timeout_s=10", "probe_timeout_s=broken"))
            key = fixture.server.host_evidence_key(request, destination)

            reply, = fixture.server.handle_request(request)

            self.assertIsInstance(reply, wire.PlacementWait)
            self.assertNotIn(key, fixture.server.host_requests)

    def test_capacity_ignores_bad_leftover_timeout(self) -> None:
        with PlacementFixture() as fixture, redirect_stderr(io.StringIO()) as warnings:
            settings, request, destination = self.target_settings_fixture(fixture)
            good = settings.read_text()
            timeout = conf.read(settings).text("leftover_timeout_s")
            _ = settings.write_text(good.replace(f"leftover_timeout_s={timeout}", "leftover_timeout_s=broken"))
            paths = mac_test.state_paths(destination.target)
            with self.subTest(reader="free_now"):
                self.assertEqual(mac_test.free_now(paths, settings, Path(request.constraints.repository)), "free")
            with self.subTest(reader="slot_capacity"):
                self.assertEqual(mac_test.slot_capacity(paths, RepositoryScope("hana")), 1)
            self.assertEqual(warnings.getvalue(), "")

    def test_capacity_recovers_bad_repository_slot_count(self) -> None:
        with PlacementFixture() as fixture:
            settings, _request, destination = self.target_settings_fixture(fixture)
            good = settings.read_text() + "\nslots.hana=2\n"
            _ = settings.write_text(good)
            self.commit_settings(settings)
            _ = settings.write_text(good.replace("slots.hana=2", "slots.hana=broken"))
            paths = mac_test.state_paths(destination.target)
            with redirect_stderr(io.StringIO()) as warnings:
                self.assertEqual(mac_test.slot_capacity(paths, RepositoryScope("hana")), 2)
            self.assertEqual(warnings.getvalue().count("slots.hana='broken'"), 1)

    def test_unreadable_mac_settings_allow_local_placement(self) -> None:
        with PlacementFixture() as fixture, redirect_stderr(io.StringIO()):
            request = placement_request(fixture)
            mac, = request.constraints.candidates
            local = local_candidate(fixture)
            request = replace(request, prerequisite=placement.TokenRequired(), constraints=replace(
                request.constraints, candidates=(mac, local)))
            mac_settings = fixture.root / "mac_test.conf"
            read_bytes = conf.read_bytes

            def read_settings(path: Path) -> conf.ConfigurationBytes:
                if path == mac_settings:
                    raise PermissionError("The Mac settings are unreadable")
                return read_bytes(path)

            with mock.patch.object(conf, "read_bytes", side_effect=read_settings):
                reply, = fixture.connect(request).replies()
                assert isinstance(reply, wire.PlacementWait), reply
                eligibility = {row.destination: row.eligibility for row in reply.eligibility}
                self.assertEqual(eligibility[local.destination], ALLOWED_HOST)
                mac_eligibility = eligibility[mac.destination]
                assert isinstance(mac_eligibility, placement.EvidenceNeeded), mac_eligibility
                self.assertIn("The Mac settings are unreadable", mac_eligibility.reason)

                _ = fixture.connect(wire.PrerequisiteReport(
                    "token-checked", request.episode, request.owner, placement.TokenHeld())).replies()
                granted, = fixture.connect(request).replies()
                self.assertIsInstance(granted, wire.LocalPlacementGranted)

    def test_unreadable_aws_settings_confine_the_refusal_to_hana(self) -> None:
        with PlacementFixture() as fixture:
            _settings, request, _destination = self.target_settings_fixture(fixture, platform="macos")
            mac = request.constraints.candidates[0].destination
            mac_key = fixture.server.host_evidence_key(request, mac)
            account = fixture.root / "aws_target.conf"
            open_path = cast(Callable[..., object], Path.open)

            def unreadable(path: Path, *args: object, **kwargs: object) -> object:
                if path == account:
                    raise PermissionError("The account settings are unreadable")
                return open_path(path, *args, **kwargs)

            with mock.patch.object(Path, "open", autospec=True, side_effect=unreadable):
                self.assertEqual(fixture.server.host_evidence_key(request, mac), mac_key)
        with PlacementFixture() as fixture:
            _settings, hana = self.idle_hana(fixture)
            account = fixture.root / "aws_target.conf"
            with mock.patch.object(Path, "open", autospec=True, side_effect=unreadable):
                evidence = self.read_target(fixture, hana)
                assert isinstance(evidence, placement.FreshHostEvidence), evidence
                assert isinstance(evidence.eligibility, placement.TemporaryExclusion), evidence
                self.assertIn("account settings are unreadable", evidence.eligibility.reason)

    def test_admission_starts_probe_recovery_and_execution_keeps_the_host_override(self) -> None:
        from scripts.build_targets import build_targets
        from scripts.copies import copies
        from scripts.mac_test import eligibility

        with PlacementFixture() as fixture:
            settings, request, destination = self.target_settings_fixture(fixture)
            host = socket.gethostname().split(".", 1)[0]
            good = settings.read_text() + f"\n# range: 1..\nshell_timeout_s=10\n# range: 1..\nshell_timeout_s.{host}=31\n"
            _ = settings.write_text(good.replace("floor_sweep_timeout_s=180", "floor_sweep_timeout_s=broken"))
            repository = eligibility.KnownRepository("hana")
            with (mock.patch.object(eligibility, "repository_identity", return_value=repository),
                  mock.patch.object(eligibility, "read_failed_probe", return_value=eligibility.FailedProbe()),
                  mock.patch.object(build_targets, "start_probe_recovery") as start):
                evidence = self.read_target(fixture, request)
                assert isinstance(evidence, placement.FreshHostEvidence), evidence
                self.assertEqual(evidence.eligibility, ALLOWED_HOST)
                start.assert_called_once()

            _ = settings.write_text(good)
            args = build_targets.CliArguments()
            args.target, args.repo_root, args.kind = destination.target, request.constraints.repository, "check"
            with (mock.patch.object(eligibility, "repository_identity", return_value=repository),
                  mock.patch.object(eligibility, "read_failed_probe", return_value=eligibility.FailedProbe()),
                  mock.patch.object(copies, "remote_lease", return_value=nullcontext()),
                  mock.patch.object(offload, "request_mirror"),
                  mock.patch.object(offload, "claim_for_run", return_value=offload.FreshMacClaim(1)),
                  mock.patch.object(offload, "copy_tracked_scripts", return_value=True),
                  mock.patch.object(offload, "copy_tree_with_key", return_value=offload.CopiedTree(offload.KnownTreeKey("source"))),
                  mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(100, "fixture")) as probe,
                  mock.patch.object(offload, "release_claim")):
                args.recovery_fd = os.open(os.devnull, os.O_RDONLY)
                self.assertEqual(build_targets.command_recover_probe(args), 0)

            probe.assert_called_once()
            config = cast(eligibility.OffloadConfig, probe.call_args.args[1])
            self.assertEqual(offload.runner_values(config, destination.target)["shell_timeout_s"], "31")

    def test_each_mac_run_kind_is_admitted_on_the_mac_and_refused_on_a_linux_target(self) -> None:
        cases: tuple[tuple[str, placement.CommandKind | placement.CommandKindNotSent, str], ...] = (
            *((kind, kind, "macos") for kind in placement.MAC_RUN_KINDS),
            *((kind, kind, "linux") for kind in placement.MAC_RUN_KINDS),
            ("zsh", placement.CommandKindNotSent(), "macos"),
            ("nextest", placement.CommandKindNotSent(), "linux"))
        for step, sent, platform in cases:
            with self.subTest(kind=sent, platform=platform), PlacementFixture() as fixture:
                repository = fixture.root / "hana"
                (repository / ".git").mkdir(parents=True)
                _ = (fixture.root / "build_targets.conf").write_text(
                    f"targets=box\nkind.box=ssh\nplatform.box={platform}\nsettings.box=box.conf\n" + UNREACHABLE_WAIT_FIXTURE)
                _ = write_config_fixture(fixture.root / "box.conf",
                    "offload=on\nhost=fixture-box\nkinds=test,lint,check,doc,build\nslots=2\n", template="mac_test")
                request = placement_request(fixture)
                assert isinstance(request.profile, records.StepProfile)
                destination = placement.PlacementDestination("ssh", "box", "fixture-box", RepositoryScope("hana"))
                request = replace(request, profile=replace(request.profile, step=step), constraints=replace(
                    request.constraints, repository=str(repository), command_kind=sent,
                    candidates=(placement.PlacementCandidate(destination),)))
                key = fixture.server.host_evidence_key(request, destination)
                fixture.server.host_requests[key] = request
                reachable = offload.CommandOutput(0, "", "")
                with (mock.patch.object(offload.ProcessController, "capture", return_value=reachable),
                      mock.patch.object(mac_test, "free_now", return_value="free")):
                    evidence = fixture.read_host_evidence(key)
                fixture.server.publish_host_evidence(evidence)
                admitted = isinstance(sent, placement.CommandKindNotSent) and step == "nextest" or (
                    sent in placement.MAC_RUN_KINDS and platform == "macos")
                if admitted:
                    assert isinstance(evidence, placement.FreshHostEvidence)
                    self.assertEqual(evidence.eligibility, placement.AllowedHost())
                    self.assertEqual(assignment_for(fixture, request).destination, destination)
                else:
                    assert isinstance(evidence, placement.FreshHostEvidence)
                    self.assertEqual(evidence.eligibility, placement.FixedExclusion("command"))
                    reply, = fixture.server.handle_request(request)
                    assert isinstance(reply, wire.DispatchStatus)
                    self.assertEqual(reply.retry, placement.DoNotRetry("command"))

    def test_each_command_kind_reads_its_own_host_evidence(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            destination = request.constraints.candidates[0].destination
            kinds: tuple[placement.CommandKind | placement.CommandKindNotSent, ...] = (
                *placement.MAC_RUN_KINDS, "test", placement.CommandKindNotSent())
            keys = {fixture.server.host_evidence_key(replace(request, constraints=replace(
                request.constraints, command_kind=kind)), destination) for kind in kinds}
            self.assertEqual(len(keys), len(kinds))

    def test_each_mac_run_kind_returns_its_mac_placement_with_a_cancel(self) -> None:
        for kind in placement.MAC_RUN_KINDS:
            with self.subTest(kind=kind), PlacementFixture() as fixture:
                request = placement_request(fixture)
                request = replace(request, constraints=replace(request.constraints, command_kind=kind))
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                # mac_queue_run returns the placement this way after each run.
                reply, = fixture.server.handle_request(wire.CancelPlacement(
                    "return", request.episode, request.owner, "the Mac run ended with status 0"))
                self.assertIsInstance(reply, wire.Ok)
                self.assertNotIn(request.episode, fixture.server.current_assignments)
                self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment("cancelled"))

    def test_a_busy_mac_holds_a_mac_run_kind(self) -> None:
        with PlacementFixture() as fixture:
            repository = fixture.root / "hana"
            (repository / ".git").mkdir(parents=True)
            _ = (fixture.root / "build_targets.conf").write_text(
                "targets=box\nkind.box=ssh\nplatform.box=macos\nsettings.box=box.conf\n")
            _ = write_config_fixture(fixture.root / "box.conf",
                "offload=on\nhost=fixture-box\nkinds=test,lint,check,doc,build\nslots=2\n", template="mac_test")
            request = placement_request(fixture)
            destination = placement.PlacementDestination("ssh", "box", "fixture-box", RepositoryScope("hana"))
            request = replace(request, constraints=replace(request.constraints, repository=str(repository),
                command_kind="install", candidates=(placement.PlacementCandidate(destination),)))
            key = fixture.server.host_evidence_key(request, destination)
            fixture.server.host_requests[key] = request
            with mock.patch.object(mac_test, "free_now", return_value="busy"):
                evidence = fixture.read_host_evidence(key)
            assert isinstance(evidence, placement.FreshHostEvidence)
            self.assertEqual(evidence.eligibility, placement.TemporaryExclusion("busy"))

    def test_an_episode_keeps_its_first_constraints_across_a_land_a_busy_target_and_a_restart(self) -> None:
        for revision in ("", *OLDER_CLIENTS, "429f57a6c"):
            with self.subTest(client=revision or "current"), PlacementFixture() as fixture:
                first = placement_request(fixture)
                if revision:
                    # The first request comes from an older client, in the shape that client writes.
                    with pinned_snapshot_reader(revision):
                        older_wire = sys.modules["_pinned_admission_wire"]
                        older = cast(Callable[[str], object], getattr(older_wire, "parse_request"))(wire.encode_request(first))
                        line = cast(Callable[[object], str], getattr(older_wire, "encode_request"))(older)
                    first = cast(wire.PlacementRequest, wire.parse_request(line))
                destination = first.constraints.candidates[0].destination
                _ = publish_candidate(fixture, first, placement.TemporaryExclusion("busy"))
                reply, = fixture.server.handle_request(first)
                self.assertIsInstance(reply, wire.PlacementWait)
                # A land changes the candidate policy, and the newer client sends its command kind.
                later = replace(first, constraints=replace(first.constraints, command_kind="test", candidates=(
                    placement.PlacementCandidate(destination, placement.FixedExclusion("linux_only")),)))
                reply, = fixture.server.handle_request(later)
                self.assertIsInstance(reply, wire.PlacementWait)
                kept = replace(first, constraints=replace(first.constraints, command_kind="test"))
                self.assertEqual(fixture.server.placement_requests[first.episode].constraints, kept.constraints)
                fixture.restart()
                self.assertEqual(fixture.server.placement_requests[first.episode].constraints, kept.constraints)
                _ = publish_candidate(fixture, kept, placement.TemporaryExclusion("busy"))
                reply, = fixture.server.handle_request(later)
                self.assertIsInstance(reply, wire.PlacementWait)
                _ = publish_candidate(fixture, kept)
                self.assertEqual(assignment_for(fixture, later).destination, destination)

    def test_an_episode_refuses_a_request_with_another_start_or_purpose(self) -> None:
        with PlacementFixture() as fixture:
            first = placement_request(fixture)
            _ = publish_candidate(fixture, first, placement.TemporaryExclusion("busy"))
            reply, = fixture.server.handle_request(first)
            self.assertIsInstance(reply, wire.PlacementWait)
            for changed, later in (("since", replace(first, since=first.since + 1)),
                                   ("purpose", replace(first, purpose=placement.SpeculativeCopy("parent", 1.0)))):
                with self.subTest(changed=changed):
                    reply, = fixture.server.handle_request(later)
                    assert isinstance(reply, wire.Refused)
                    self.assertEqual(reply.reason, "placement episode changed its owner, start, run or purpose")

    def test_a_lost_or_delivered_authorization_never_replays_before_or_after_restart(self) -> None:
        for response in ("lost", "delivered", "delivered_before_next_pass"):
            for restart in (False, True):
                with self.subTest(response=response, restart=restart), PlacementFixture() as fixture:
                    fixture.configure(replay_retention_seconds=1)
                    request = placement_request(fixture)
                    _ = publish_candidate(fixture, request)
                    assignment = assignment_for(fixture, request)
                    self.assertEqual(assignment.destination, request.constraints.candidates[0].destination)
                    self.assertEqual((assignment.episode, assignment.source_key, assignment.owner, assignment.since),
                                     (request.episode, request.constraints.source_key, request.owner, request.since))
                    fixture.restart()
                    self.assertEqual(assignment_for(fixture, request), assignment)
                    claim = adopt_assignment(fixture, request, assignment)
                    consume = replace(consume_permission("consume-first", assignment, claim),
                                      code_version="consume-caller-version")
                    fixture.fault.seen.clear()

                    def durable_authorization(reply: wire.Reply) -> wire.Reply:
                        if isinstance(reply, wire.FirstAuthorization):
                            state = service.read_state(fixture.state_dir)
                            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                            self.assertIsInstance(state.assignments[key].permission, placement.PermissionConsumed)
                            start = records.process_start_time(os.getpid())
                            self.assertIsNotNone(start)
                            self.assertEqual(state.caller_versions[records.run_owner(os.getpid(), str(start))],
                                             consume.code_version)
                            self.assertEqual(fixture.fault.seen.count("after_state_write"), 1)
                        return reply

                    fixture.server.reply_filter = durable_authorization
                    if response == "lost":
                        peer = fixture.connect(consume)
                        for _ in range(4):
                            fixture.server.run_once(0)
                        peer.close()
                    elif response == "delivered":
                        reply, = fixture.connect(consume).replies()
                        self.assertEqual(reply, wire.FirstAuthorization(assignment, claim))
                    else:
                        from scripts.build_queue import admission_client

                        queued, finish = threading.Event(), threading.Event()
                        run_pass = fixture.server.run_pass

                        def hold_next_pass() -> gate.PassResult | gate.UnusableAdmissionConfiguration:
                            if queued.is_set() and not finish.wait(TEST_HANG_GUARD_SECS):
                                raise AssertionError("the test did not release the independent admission pass")
                            return run_pass()

                        def queued_authorization(reply: wire.Reply) -> wire.Reply:
                            reply = durable_authorization(reply)
                            if isinstance(reply, wire.FirstAuthorization):
                                queued.set()
                                fixture.server.next_pass = fixture.clock.monotonic()
                            return reply

                        fixture.server.reply_filter = queued_authorization
                        with mock.patch.object(fixture.server, "run_pass", side_effect=hold_next_pass), \
                             mock.patch.object(admission_client, "LOADED_ADMISSION_CODE_VERSION", consume.code_version), \
                             fixture.serving():
                            try:
                                self.assertEqual(admission_client.consume_permission(consume),
                                                 wire.FirstAuthorization(assignment, claim))
                            finally:
                                finish.set()
                    if restart:
                        fixture.clock.advance(2)
                        _ = fixture.run_pass()
                        fixture.restart()
                    for request_id in (consume.request_id, "another-transport-request"):
                        reply, = fixture.connect(replace(consume, request_id=request_id)).replies()
                        self.assertIsInstance(reply, wire.DispatchStatus)
                        assert isinstance(reply, wire.DispatchStatus)
                        self.assertEqual(reply.assignment, assignment)
                        self.assertIsInstance(reply.permission, placement.PermissionConsumed)
                        self.assertEqual(reply.reason, "launch permission was already consumed")

    def test_closed_unknown_and_changed_assignments_never_receive_authorization(self) -> None:
        for case, reason in (("closed", "the attempt is closed: busy"), ("unknown", "unknown attempt"),
                             ("source", "the request differs from the saved assignment"),
                             ("owner", "the request differs from the saved assignment"), ("claim", "the exact claim is not adopted"),
                             ("stale", "adoption evidence is stale: provider evidence expired"),
                             ("unavailable", "adoption evidence is unavailable: claim read failed")):
            with self.subTest(case=case), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                original = assignment
                if case == "closed":
                    _ = fixture.server.handle_request(wire.ClaimFailure("claim-refused", assignment, "busy"))
                    replacement = assignment_for(fixture, request)
                    self.assertNotEqual(replacement.attempt, assignment.attempt)
                    self.assertEqual(replacement.since, assignment.since)
                elif case == "unknown":
                    assignment = replace(assignment, attempt="unknown-old-attempt")
                    claim = replace(claim, attempt=assignment.attempt)
                elif case == "source":
                    assignment = replace(assignment, source_key=execution_identity.SourceKeyKnown("obsolete-source"))
                elif case == "owner":
                    assignment = replace(assignment, owner=replace(assignment.owner, host="another-coordinator"))
                elif case == "claim":
                    claim = replace(claim, claim_id="another-claim")
                for restart in (False, True):
                    if restart:
                        fixture.restart()
                    if case in ("stale", "unavailable"):
                        key = fixture.server.host_evidence_key(request, assignment.destination)
                        evidence = (placement.StaleHostEvidence(key, fixture.clock.now() - 20, "provider evidence expired")
                            if case == "stale" else placement.HostEvidenceUnavailable(key, fixture.clock.now(), "claim read failed"))
                        fixture.provider_evidence[key] = evidence
                        fixture.server.publish_host_evidence(evidence)
                    reply, = fixture.server.handle_request(consume_permission(f"consume-{restart}", assignment, claim))
                    self.assertIsInstance(reply, wire.DispatchStatus)
                    assert isinstance(reply, wire.DispatchStatus)
                    self.assertEqual(reply.reason, reason)
                if case in ("source", "owner", "claim") or case.startswith("generation-"):
                    claim = (replace(claim, claim_id="claim-a") if case.startswith("generation-")
                             else adopt_assignment(fixture, request, original))
                    reply, = fixture.server.handle_request(consume_permission("valid-consumption", original, claim))
                    self.assertEqual(reply, wire.FirstAuthorization(original, claim))

    def test_session_evidence_cannot_transfer_a_holders_exemption(self) -> None:
        with PlacementFixture() as fixture:
            holder = placement_request(fixture, session=OwningSession("holder"))
            stranger = placement_request(fixture, session=OwningSession("stranger"))
            unknown = placement_request(fixture, session=NoOwningSession())
            _ = publish_candidate(fixture, holder)
            assigned = assignment_for(fixture, holder)
            for request in (stranger, unknown):
                with self.subTest(session=request.constraints.session):
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.PlacementWait)
                    _ = publish_candidate(fixture, request, placement.TemporaryExclusion("blocked"))
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.PlacementWait)
                    assert isinstance(reply, wire.PlacementWait)
                    self.assertEqual(reply.eligibility[0].eligibility, placement.TemporaryExclusion("blocked"))
            self.assertEqual(assignment_for(fixture, holder), assigned)

    def test_execution_hosts_keep_equal_pids_separate_and_never_become_owner_watches(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            with mock.patch.object(fixture.proc, "pidfd", wraps=fixture.proc.pidfd) as watched:
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                reply, = fixture.server.handle_request(consume_permission("consume", assignment, claim))
                self.assertIsInstance(reply, wire.FirstAuthorization)
                executions = tuple(execution_identity.ProcessExecution(host, "generation-a", 700001, "equal-start")
                                   for host in ("fixture-mac", "second-host"))
                self.assertEqual(len(set(executions)), 2)
                for execution in executions:
                    _ = fixture.server.handle_request(wire.ExecutionReport(
                        f"report-{execution.host}", assignment, claim, execution_identity.ObservedExecution(execution)))
                fixture.restart()
            self.assertTrue(watched.call_args_list)
            self.assertEqual({call.args[0] for call in watched.call_args_list}, {request.owner.pid})

    def test_blocked_remote_evidence_keeps_socket_requests_fast_and_retains_the_claim(self) -> None:
        with PlacementFixture() as fixture:
            fixture.configure(waiting_pass_seconds=0.05)
            _ = (fixture.root / "mac_test.conf").write_text("# range: 0.001..60\nprobe_timeout_s=0.05\n")
            _ = fixture.run_pass()
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            claim = adopt_assignment(fixture, request, assignment)
            paths = mac_test.StatePaths(fixture.root / "provider")
            paths.directory.mkdir()
            provider = paths.run
            record = {"claim_id": claim.claim_id, "episode": claim.episode, "attempt": claim.attempt,
                      "submitting_session": "submitting-session", "pid": request.owner.pid,
                      "proc_start": request.owner.start_time, "remote_host": assignment.destination.host,
                      "what": "hana tests", "worktree": str(fixture.root), "since": "2026-11-01T12:00:00+00:00"}
            _ = provider.write_text(json.dumps(record))
            before = provider.read_bytes()
            local = fixture.request()
            granted = only_admitted(fixture.server.handle_request(local))
            entered, finish = threading.Event(), threading.Event()
            read_started: list[float] = []
            evidence_key = fixture.server.host_evidence_key(request, assignment.destination)

            class FirstProbeWorker(service.HostEvidenceWorker):
                @override
                def _probe(self, key: placement.HostEvidenceKey, subscription: object) -> bool:
                    if key == evidence_key and entered.is_set():
                        return False
                    return super()._probe(key, subscription)

            def blocked_probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                read_started.append(fixture.clock.now())
                entered.set()
                _ = finish.wait()
                return placement.FreshHostEvidence(key, fixture.clock.now(), "late-generation", placement.AllowedHost())

            fixture.server.host_probe = blocked_probe
            fixture.clock.advance(0.09)
            self.assertIsInstance(fixture.server.host_evidence(evidence_key), placement.FreshHostEvidence)
            fixture.clock.advance(0.02)
            self.assertIsInstance(fixture.server.host_evidence(evidence_key), placement.StaleHostEvidence)
            waiter = placement_request(fixture)
            with (ThreadPoolExecutor(max_workers=1) as workers,
                  mock.patch.object(service, "HostEvidenceWorker", FirstProbeWorker)):
                try:
                    probe_guard = LauncherHangGuard("Remote evidence probe start")
                    reply, = probe_guard.result(workers.submit(fixture.connect(waiter).replies))
                    self.assertIsInstance(reply, wire.PlacementWait)
                    probe_guard.event(entered)
                    fixture.allow_next_start()
                    for operation in (wire.ViewRequest(), wire.ReleaseRequest("release-local", granted.reservation, 0),
                                      fixture.request()):
                        reply_guard = LauncherHangGuard(f"{type(operation).__name__} while remote evidence is blocked")
                        replies = reply_guard.result(workers.submit(fixture.connect(operation).replies))
                        self.assertTrue(replies)
                        self.assertIsInstance(replies[-1], wire.ViewReply if isinstance(operation, wire.ViewRequest)
                                              else wire.Ok if isinstance(operation, wire.ReleaseRequest) else wire.Admitted)
                    self.assertIsInstance(fixture.server.host_evidence(evidence_key), placement.StaleHostEvidence)
                    self.assertEqual(provider.read_bytes(), before)
                    self.assertEqual([item.record["claim_id"] for item in mac_test.live_claims(paths)], [claim.claim_id])
                    self.assertEqual(assignment_for(fixture, request), assignment)
                finally:
                    fixture.clock.advance(1)
                    finish.set()
                    reader = fixture.server.host_readers.get(evidence_key)
                    if reader is not None:
                        reader.close()
                        LauncherHangGuard("Remote evidence probe cleanup").join(reader.thread)
            assert reader is not None
            published = reader.latest
            assert isinstance(published, placement.FreshHostEvidence)
            self.assertEqual(published.generation, "late-generation")
            observed = fixture.server.host_evidence(evidence_key)
            self.assertIsInstance(observed, placement.StaleHostEvidence)
            self.assertLessEqual(observed.observed_at, read_started[0])


class ManualEvidenceWorker(service.HostEvidenceWorker):
    """Probe only when the test asks, so each reading has a known time."""

    @override
    def _read(self) -> None:
        return

    def probe_next(self) -> placement.HostEvidenceKey:
        with self.lock:
            ready = self._next_ready(float("inf"))
        if not isinstance(ready, tuple):
            raise AssertionError("the worker has no key to probe")
        _ = self._probe(*ready)
        return ready[0]


@dataclass(frozen=True)
class MacOrder:
    """An older request with a busy Mac reading and a younger request with a newer free reading."""

    older: wire.PlacementRequest
    younger: wire.PlacementRequest
    older_key: placement.HostEvidenceKey
    younger_key: placement.HostEvidenceKey
    worker: ManualEvidenceWorker


# The Mac hold lasts one waiting pass plus one probe time limit.
HOLD_SECONDS = 1 + 5


class MacOrderTests(unittest.TestCase):
    """A younger request never takes the Mac from an older request whose busy reading is older."""

    def mac_order(self, fixture: PlacementFixture) -> MacOrder:
        fixture.configure(waiting_pass_seconds=1)
        _ = write_config_fixture(fixture.root / "mac_test.conf", "probe_timeout_s=5\n", template="mac_test")
        _ = fixture.run_pass()
        older = placement_request(fixture, since=fixture.clock.now() - 60)
        older_key = self.provide(fixture, older, placement.TemporaryExclusion("busy"))
        self.assertIsInstance(fixture.connect(older).replies()[-1], wire.PlacementWait)
        _ = fixture.run_pass()
        worker = fixture.server.host_targets[("mac", "mac")]
        assert isinstance(worker, ManualEvidenceWorker)
        self.assertEqual(worker.probe_next(), older_key)
        fixture.clock.advance(1)
        # Another worktree session reads the same Mac scope under its own key.
        younger = placement_request(fixture, session=OwningSession("younger-session"), since=fixture.clock.now() - 10)
        younger_key = self.provide(fixture, younger, ALLOWED_HOST)
        self.assertIsInstance(fixture.connect(younger).replies()[-1], wire.PlacementWait)
        _ = fixture.run_pass()
        # The younger key is due first: the older key was probed one second ago.
        self.assertEqual(worker.probe_next(), younger_key)
        _ = fixture.run_pass()
        self.assertEqual(self.assigned(fixture, older, younger), [])
        return MacOrder(older, younger, older_key, younger_key, worker)

    @staticmethod
    def provide(fixture: PlacementFixture, request: wire.PlacementRequest,
                eligibility: placement.HostEligibility) -> placement.HostEvidenceKey:
        """Set the Mac reading that the next probe of this request returns."""
        key = fixture.server.host_evidence_key(request, request.constraints.candidates[0].destination)
        fixture.provider_evidence[key] = placement.FreshHostEvidence(
            key, fixture.clock.now(), "host-generation-a", eligibility)
        return key

    @staticmethod
    def assigned(fixture: PlacementFixture, *requests: wire.PlacementRequest) -> list[str]:
        return [request.episode for request in requests if request.episode in fixture.server.current_assignments]

    def test_the_older_request_is_probed_next_and_takes_the_mac(self) -> None:
        with PlacementFixture() as fixture, mock.patch.object(service, "HostEvidenceWorker", ManualEvidenceWorker):
            order = self.mac_order(fixture)
            fixture.clock.advance(1)
            _ = self.provide(fixture, order.older, ALLOWED_HOST)
            self.assertEqual(order.worker.probe_next(), order.older_key)
            _ = fixture.run_pass()
            self.assertEqual(self.assigned(fixture, order.older, order.younger), [order.older.episode])

    def test_the_hold_never_lasts_past_one_probe_when_the_probe_fails_or_never_answers(self) -> None:
        for probe in ("fails", "never answers"):
            with (self.subTest(probe=probe), PlacementFixture() as fixture,
                  mock.patch.object(service, "HostEvidenceWorker", ManualEvidenceWorker)):
                order = self.mac_order(fixture)
                if probe == "fails":
                    fixture.clock.advance(1)
                    fixture.provider_evidence[order.older_key] = placement.HostEvidenceUnavailable(
                        order.older_key, fixture.clock.now(), "SSH probe could not start")
                    self.assertEqual(order.worker.probe_next(), order.older_key)
                else:
                    fixture.clock.advance(HOLD_SECONDS)
                    _ = publish_candidate(fixture, order.younger)
                    _ = fixture.run_pass()
                    self.assertEqual(self.assigned(fixture, order.older, order.younger), [])
                    fixture.clock.advance(0.01)
                    _ = publish_candidate(fixture, order.younger)
                _ = fixture.run_pass()
                self.assertEqual(self.assigned(fixture, order.older, order.younger), [order.younger.episode])

    def test_the_hold_ends_when_the_older_request_leaves_the_queue(self) -> None:
        with PlacementFixture() as fixture, mock.patch.object(service, "HostEvidenceWorker", ManualEvidenceWorker):
            order = self.mac_order(fixture)
            reply, = fixture.server.handle_request(wire.CancelPlacement(
                "leave", order.older.episode, order.older.owner, "the step ended"))
            self.assertIsInstance(reply, wire.Ok)
            _ = fixture.run_pass()
            self.assertEqual(self.assigned(fixture, order.older, order.younger), [order.younger.episode])
            self.assertEqual(fixture.server.placement_holds, {})

    def test_older_requests_that_cannot_use_the_mac_never_keep_it_from_a_younger_one(self) -> None:
        """11-15 10:44-11:04: two older cargo-liner steps waited on natedev memory and could not use the Mac.

        The tile nextest read the cargo-liner scope free. Each new tile reading started new holds for the older
        steps, and the tile reading expired before both older probes answered. The tile nextest never got the Mac.
        """
        for refusal in ("busy", "disk"):
            with (self.subTest(refusal=refusal), PlacementFixture() as fixture,
                  mock.patch.object(service, "HostEvidenceWorker", ManualEvidenceWorker)):
                fixture.configure(waiting_pass_seconds=1)
                _ = write_config_fixture(fixture.root / "mac_test.conf", "probe_timeout_s=5\n", template="mac_test")
                fixture.free(0)
                fixture.hold_starts()
                _ = fixture.run_pass()
                readings: dict[placement.HostEvidenceKey, placement.HostEligibility] = {}
                older: list[wire.PlacementRequest] = []
                for name, age in (("handler-mend", 1500), ("handler-clippy", 1400)):
                    request = placement_request(fixture, session=OwningSession(name), since=fixture.clock.now() - age)
                    request = replace(request, constraints=replace(request.constraints,
                        candidates=(*request.constraints.candidates, local_candidate(fixture))))
                    readings[self.provide(fixture, request, placement.TemporaryExclusion(refusal))] = (
                        placement.TemporaryExclusion(refusal))
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    older.append(request)
                tile = placement_request(fixture, session=OwningSession("tile-nextest"),
                                         since=fixture.clock.now() - 1300)
                readings[self.provide(fixture, tile, ALLOWED_HOST)] = ALLOWED_HOST
                self.assertIsInstance(fixture.connect(tile).replies()[-1], wire.PlacementWait)
                worker = fixture.server.host_targets[("mac", "mac")]
                assert isinstance(worker, ManualEvidenceWorker)
                for _probe in range(15):
                    # Each Mac probe takes four seconds, so three probes outlast one reading.
                    fixture.clock.advance(4)
                    for key, eligibility in readings.items():
                        fixture.provider_evidence[key] = placement.FreshHostEvidence(
                            key, fixture.clock.now(), "host-generation-a", eligibility)
                    _ = worker.probe_next()
                    _ = fixture.run_pass()
                self.assertEqual(self.assigned(fixture, *older, tile), [tile.episode])


NatedevCause = records.HeadCause | records.WaitReason | wire.CloudSlotWait | wire.PlacementEvidenceWait | wire.PlacementResourceWait


class NatedevOnlyHoldTests(unittest.TestCase):
    """Rule 9: a natedev guard holds natedev only. A free Mac takes a waiting run that allows the Mac."""

    def mac_takes_the_run_after_a_natedev_wait(
            self, fixture: PlacementFixture,
            hold_natedev: Callable[[wire.PlacementRequest], wire.PlacementRequest] = lambda request: request,
    ) -> NatedevCause:
        """Make the run wait on natedev while the Mac is busy, then free the Mac. Return the natedev cause."""
        mac_only = placement_request(fixture)
        request = hold_natedev(replace(mac_only, constraints=replace(mac_only.constraints,
            candidates=(local_candidate(fixture), *mac_only.constraints.candidates))))
        _ = publish_candidate(fixture, mac_only, placement.TemporaryExclusion("busy"))
        waiting = fixture.connect(request).replies()[-1]
        assert isinstance(waiting, wire.PlacementWait), waiting
        _ = publish_candidate(fixture, mac_only)
        _ = fixture.run_pass()
        mac = mac_only.constraints.candidates[0].destination
        self.assertIn(request.episode, fixture.server.current_assignments, "the free Mac did not take the run")
        self.assertEqual(fixture.server.assignments[fixture.server.current_assignments[request.episode]]
                         .assignment.destination, mac)
        # The next request of the run gets the Mac assignment, not the natedev wait.
        reply, = fixture.server.handle_request(request)
        assert isinstance(reply, wire.Assignment), reply
        self.assertEqual(reply.assignment.destination, mac)
        self.assertNotIn(request.episode, fixture.server.reservations)
        wait = waiting.wait
        if isinstance(wait, wire.Wait):
            return wait.reason.cause if isinstance(wait.reason, records.HeadAdmissionStatus) else wait.reason
        return wait

    def test_a_natedev_build_space_hold_never_keeps_a_free_mac_from_a_run_that_allows_it(self) -> None:
        with mock.patch.object(store, "host_name", return_value="natedev"), PlacementFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", free_floor_gib="150")
            _ = fixture.enter_context(mock.patch.object(fixture.readings, "build_space",
                                                        return_value=records.BuildSpaceLow(125, 150)))
            self.assertEqual(self.mac_takes_the_run_after_a_natedev_wait(fixture), records.BuildSpaceLow(125, 150))

    def test_full_or_pressured_natedev_memory_never_keeps_a_free_mac_from_a_run_that_allows_it(self) -> None:
        for pressure in (False, True):
            with self.subTest(pressure=pressure), PlacementFixture() as fixture:
                _ = only_admitted(fixture.server.handle_request(fixture.request(need=GIB)))
                fixture.allow_next_start()
                if pressure:
                    fixture.hold_starts()
                else:
                    fixture.free(2 * GIB)
                self.assertIsInstance(self.mac_takes_the_run_after_a_natedev_wait(fixture),
                                      records.MemoryPressure if pressure else records.NeedsMemory)

    def test_a_full_natedev_gpu_slot_never_keeps_a_free_mac_from_a_run_that_allows_it(self) -> None:
        with PlacementFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf",
                                gpu_test_slots="1", gpu_test_repositories="fixture")
            _ = only_admitted(fixture.server.handle_request(fixture.request(need=GIB)))
            fixture.allow_next_start()
            self.assertEqual(self.mac_takes_the_run_after_a_natedev_wait(fixture), records.GpuSlotWait(1, 1))

    def test_a_natedev_build_hold_never_keeps_a_free_mac_from_a_run_that_allows_it(self) -> None:
        with PlacementFixture() as fixture:
            def hold_natedev(request: wire.PlacementRequest) -> wire.PlacementRequest:
                # The local candidate is first, so this is the natedev build hold reading.
                _ = publish_candidate(fixture, request, placement.TemporaryExclusion("blocked"))
                return request

            cause = self.mac_takes_the_run_after_a_natedev_wait(fixture, hold_natedev)
            assert isinstance(cause, wire.PlacementResourceWait), cause
            self.assertEqual((cause.resource, cause.reason), ("natedev", "blocked"))

    def test_an_occupied_natedev_cargo_folder_never_keeps_a_free_mac_from_a_run_that_allows_it(self) -> None:
        with PlacementFixture() as fixture:
            folder = fixture.root / "target"
            granted, = fixture.connect(with_cargo_folder(local_cargo_request(fixture), str(folder))).replies()
            assert isinstance(granted, wire.LocalPlacementGranted), granted
            fixture.allow_next_start()

            def same_folder(request: wire.PlacementRequest) -> wire.PlacementRequest:
                return replace(request, constraints=replace(request.constraints,
                                                            cargo_folder=placement.CargoFolder(folder)))

            self.assertIsInstance(self.mac_takes_the_run_after_a_natedev_wait(fixture, same_folder),
                                  placement.CargoFolderWait)


class PassGapTests(unittest.TestCase):
    """Socket requests in one pass gap share the next pass and get their replies from it."""

    def gap_fixture(self, fixture: AdmissionFixture) -> None:
        fixture.configure(min_pass_gap_seconds=0.25)
        _ = fixture.run_pass()

    def test_placement_requests_in_one_gap_share_the_next_pass(self) -> None:
        with PlacementFixture() as fixture:
            self.gap_fixture(fixture)
            requests = [placement_request(fixture) for _ in range(2)]
            for request in requests:
                _ = publish_candidate(fixture, request)
            peers = [fixture.connect(request) for request in requests]
            with mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passes:
                self.assertEqual(peers[0].replies(), [])
                self.assertEqual([item.request.episode for item in fixture.server.parked_placements],
                                 [request.episode for request in requests])
                self.assertEqual(passes.call_count, 0)
                fixture.clock.advance(0.25)
                replies = [peer.replies() for peer in peers]
                self.assertEqual(passes.call_count, 1)
            for reply in replies:
                self.assertEqual(len(reply), 1, reply)
                self.assertIsInstance(reply[0], wire.Assignment | wire.PlacementWait)
            self.assertFalse(fixture.server.parked_placements)

    def test_a_closed_connection_drops_its_parked_placement(self) -> None:
        with PlacementFixture() as fixture:
            self.gap_fixture(fixture)
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            peer = fixture.connect(request)
            self.assertEqual(peer.replies(), [])
            self.assertEqual(len(fixture.server.parked_placements), 1)
            peer.close()
            fixture.server.run_once(0)
            self.assertFalse(fixture.server.parked_placements)
            fixture.clock.advance(0.25)
            fixture.server.run_once(0)
            self.assertFalse(fixture.server.connections)

    def test_an_admission_in_the_gap_gets_its_grant_at_the_next_pass(self) -> None:
        with AdmissionFixture() as fixture:
            self.gap_fixture(fixture)
            peer = fixture.connect(fixture.request())
            self.assertEqual(peer.replies(), [])
            fixture.clock.advance(0.25)
            _ = only_admitted(peer.replies())

    def test_a_request_without_a_connection_gets_its_reply_at_once(self) -> None:
        with PlacementFixture() as fixture:
            self.gap_fixture(fixture)
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            _ = assignment_for(fixture, request)
            self.assertFalse(fixture.server.parked_placements)
