"""Validate the tagged decoder with both Python union forms."""
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Literal, NotRequired, TypedDict, cast, get_args
import unittest
from unittest import mock

from scripts.build_queue import execution_identity
from scripts.build_targets import target_config
from scripts.build_queue import admission_records as records
from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue import admission_protocol as wire, placement
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, snapshot_with_head_cause, wait_reply
from scripts.build_queue.queue_fixture import fixture_reservation, head_reading
from scripts.build_hold.owning_session import NoOwningSession
from scripts.mac_test.claim_state import RepositoryScope, WholeMacScope
from scripts.build_queue.scope_activity import LiveBuildRun, NotServiceSlice, RunningServiceJob, WaitingServiceJob, UnmatchedServiceJob
from scripts.production import staff
from scripts.production.staff import StaffId


@dataclass(frozen=True)
class Ready:
    count: int


@dataclass(frozen=True)
class Unavailable:
    reason: str


class Reading(TypedDict):
    state: Literal["ready"] | Literal["waiting"]
    value: Ready | Unavailable
    detail: NotRequired[str]


def service_snapshot() -> records.AdmissionWaitSnapshot:
    memory = records.UnownedSliceMemory(123, pid=42, job_state=RunningServiceJob(100))
    running = records.RunningStep("service", "hana CI", "service", "hana-ci.slice", 0, memory, pid=42)
    return replace(wait_reply(records.NeedsMemory()).snapshot, running=(running,))


# The CI pin, and the last main before older clients read a GPU slot wait as a wait behind the slot.
OLDER_CLIENTS = ("7fdfd9a6a", "6a0923b23")
# The CI cargo front that nixos pins on 2026-11-15. It refuses its step on a wait that it cannot read.
CI_CLIENT = "bbc8803f4"


@contextmanager
def pinned_snapshot_reader(revision: str = "7fdfd9a6a") -> Generator[tuple[Callable[[object, object], object], type, ModuleType]]:
    """Load the schemas and codec of an older client directly from Git."""
    names = ("snapshot_codec", "placement", "admission_records", "admission_wire", "admission_protocol")
    modules = {"_pinned_" + name: ModuleType("_pinned_" + name) for name in names}
    with mock.patch.dict(sys.modules, modules):
        for name, module in modules.items():
            path = "scripts/build_queue/" + name.removeprefix("_pinned_") + ".py"
            source = subprocess.run(["git", "show", revision + ":" + path],
                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, check=True).stdout
            for dependency in ("snapshot_codec", "placement", "admission_records"):
                source = source.replace("from scripts.build_queue import " + dependency + " as ",
                                        "import _pinned_" + dependency + " as ")
                source = source.replace(f"from scripts.build_queue import {dependency}\n",
                                        f"import _pinned_{dependency} as {dependency}\n")
            for dependency in ("admission_wire", "placement"):
                source = source.replace("from scripts.build_queue." + dependency + " import (",
                                        "from _pinned_" + dependency + " import (")
            # The older codec shapes its records by module name, so it must know the pinned module names.
            source = source.replace('__module__ == "scripts.build_queue.', '__module__ == "_pinned_')
            exec(compile(source, revision + ":" + path, "exec"), module.__dict__)
        setattr(modules["_pinned_admission_records"], "placement", modules["_pinned_placement"])
        setattr(modules["_pinned_admission_wire"], "records", modules["_pinned_admission_records"])
        setattr(modules["_pinned_admission_wire"], "placement", modules["_pinned_placement"])
        yield (cast(Callable[[object, object], object], getattr(modules["_pinned_snapshot_codec"], "parse_snapshot_value")),
               cast(type, getattr(modules["_pinned_admission_records"], "AdmissionWaitSnapshot")),
               modules["_pinned_admission_protocol"])


class SnapshotCodecTests(unittest.TestCase):
    def test_snapshot_replies_preserve_the_ended_step_identity(self) -> None:
        reading = head_reading()
        now = reading.explanation.now
        run = LiveBuildRun(42, "start", now - 1)
        identity = records.EndedStepFacts("test", "hana", "hana", "/fixture/hana", StaffId("fixed-director"))
        held = records.BetweenStepsSlot(run, "finished", GIB, now + 10, identity)
        for slot in (held, records.ContinuationSlot(held, "next")):
            inputs = replace(reading.explanation.decided_from, slots=(slot,))
            explanation = replace(reading.explanation, decided_from=inputs)
            current = replace(reading, explanation=explanation)
            for reply in (wire.ViewReply(current), wire.ExplainReply(explanation), wire.MineReply(current)):
                with self.subTest(slot=type(slot).__name__, reply=type(reply).__name__):
                    self.assertEqual(wire.parse_reply(wire.encode_reply(reply)), reply)

            raw = cast(dict[str, object], codec.snapshot_value(inputs))
            older, = cast(list[dict[str, object]], raw["slots"])
            if isinstance(slot, records.ContinuationSlot):
                older = cast(dict[str, object], older["slot"])
            del older["identity"]
            parsed = cast(records.QueueInputs, codec.parse_snapshot_value(raw, records.QueueInputs))
            retained = replace(held, identity=records.HoldPredatesStepFacts())
            expected = replace(slot, slot=retained) if isinstance(slot, records.ContinuationSlot) else retained
            self.assertEqual(parsed, replace(inputs, slots=(expected,)))

            older["identity"] = {"kind": "EndedStepIdentity", "source": "step", "name": "test hana",
                                 "staff_id": "fixed-director"}
            parsed = cast(records.QueueInputs, codec.parse_snapshot_value(raw, records.QueueInputs))
            absent = records.EndedStepFactNotRecorded()
            recovered = records.EndedStepFacts("test hana", records.PackageNotRecorded(), absent, absent,
                                              StaffId("fixed-director"))
            retained = replace(held, identity=recovered)
            expected = replace(slot, slot=retained) if isinstance(slot, records.ContinuationSlot) else retained
            self.assertEqual(parsed, replace(inputs, slots=(expected,)))

    def test_older_hold_snapshot_recovers_the_caller_from_its_reservation(self) -> None:
        inputs = head_reading().explanation.decided_from
        reservation = fixture_reservation(42, GIB)
        reservation.update(reservation_id="finished", queue_waiter={"pid": 42,
            "start_time": "start", "since": inputs.now - 10, "need": GIB, "session_name": "saved caller"})
        run = LiveBuildRun(42, "start", inputs.now - 10)
        facts = records.EndedStepFacts("check", "hana", "hana", "/fixture/hana", records.NoEndedStepStaff())
        held = records.BetweenStepsSlot(run, "finished", GIB, inputs.now + 10, facts)
        for slot in (held, records.ContinuationSlot(held, "successor")):
            with self.subTest(slot=type(slot).__name__):
                current = replace(inputs, reservations=(reservation,), slots=(slot,))
                raw = cast(dict[str, object], codec.snapshot_value(current))
                encoded, = cast(list[dict[str, object]], raw["slots"])
                if isinstance(slot, records.ContinuationSlot):
                    encoded = cast(dict[str, object], encoded["slot"])
                del cast(dict[str, object], encoded["identity"])["caller_name"]
                parsed = cast(records.QueueInputs, codec.parse_snapshot_value(raw, records.QueueInputs))
                decoded, = parsed.slots
                slot_read = decoded.slot if isinstance(decoded, records.ContinuationSlot) else decoded
                self.assertEqual(slot_read.identity, replace(facts, caller_name="saved caller"))

    def test_hold_caller_candidates_use_reservation_and_fixed_staff_in_order(self) -> None:
        inputs = head_reading().explanation.decided_from
        member = staff.StaffRow(StaffId("fixed"), "unit", "staff name", staff.StaffRoot(),
                                current_session=StaffId("current"))
        run = LiveBuildRun(42, "start", inputs.now - 10)
        for stored, reserved, expected in (
            ("recorded name", "reserved name", "recorded name"),
            (records.EndedStepFactNotRecorded(), "reserved name", "reserved name"),
            ("$OWNER", "owner not recorded", "staff name"),
        ):
            with self.subTest(stored=stored, reserved=reserved):
                reservation = fixture_reservation(42, GIB)
                reservation.update(reservation_id="finished", queue_waiter={"pid": 42,
                    "start_time": "start", "since": inputs.now - 10, "need": GIB, "session_name": reserved})
                facts = records.EndedStepFacts("gate", "hana", "hana", "/fixture/hana", member.id,
                                               caller_name=stored)
                held = records.BetweenStepsSlot(run, "finished", GIB, inputs.now + 10, facts)
                current = replace(inputs, reservations=(reservation,), slots=(held,))
                with codec.snapshot_staff(staff.StaffSnapshot((member,))):
                    parsed = cast(records.QueueInputs, codec.parse_snapshot_value(codec.snapshot_value(current),
                                                                                 records.QueueInputs))
                self.assertEqual(parsed.slots, (replace(held, identity=replace(facts, caller_name=expected)),))

    def test_older_services_read_direct_requests_with_a_local_reason(self) -> None:
        reason = "it edits the merged checkout on natedev with mend and fmt, and the push goes from there"
        for revision in (CI_CLIENT, "1be2f8932"):
            with self.subTest(revision=revision), AdmissionFixture() as fixture, \
                    pinned_snapshot_reader(revision) as (parse, schema, pinned):
                parse_request = cast(Callable[[str], object], getattr(pinned, "parse_request"))
                encode_request = cast(Callable[[object], str], getattr(pinned, "encode_request"))
                for base in (fixture.request(), fixture.service_request()):
                    with self.subTest(request=type(base).__name__):
                        request = replace(base, route=target_config.LocalRequired(reason))
                        encoded = wire.encode_request(request)
                        self.assertEqual(wire.parse_request(encoded), request)
                        decoded = parse_request(encoded)
                        self.assertEqual(type(decoded).__name__, type(request).__name__)
                        self.assertEqual(cast(str, getattr(decoded, "episode")), request.episode)
                        restored = wire.parse_request(encode_request(decoded))
                        assert isinstance(restored, (wire.AdmitRequest, wire.ServiceEstimateRequest))
                        self.assertEqual(restored.route, records.DIRECT_CARGO)
                running = records.RunningStep("session", "fixture", "build", "/fixture", 0,
                    records.MeasuredMemory(1024), pid=42, route=target_config.LocalRequired(reason))
                snapshot = replace(wait_reply(records.NeedsMemory()).snapshot, running=(running,))
                parsed = parse(codec.snapshot_value(snapshot), schema)
                rows = cast(tuple[object, ...], getattr(parsed, "running"))
                self.assertEqual(cast(str, getattr(rows[0], "step")), "build")

    def test_pinned_client_reads_current_pressure_policy_and_some_pressure(self) -> None:
        with pinned_snapshot_reader() as (parse, schema, _pinned):
            for pressure in (records.NoPressureHold(), records.MemoryPressure(0, 91, 50, condition="some")):
                with self.subTest(pressure=pressure):
                    snapshot = service_snapshot()
                    snapshot = replace(snapshot, decision=replace(snapshot.decision, pressure=pressure))
                    raw = cast(dict[str, object], codec.snapshot_value(snapshot))
                    self.assertFalse(hasattr(snapshot.policy.pressure, "swap_used_limit"))
                    parsed = parse(raw, schema)
                    policy = cast(object, getattr(parsed, "policy"))
                    legacy_policy = cast(object, getattr(policy, "pressure"))
                    self.assertEqual(cast(object, getattr(legacy_policy, "swap_used_limit")), 0.0)
                    if isinstance(pressure, records.MemoryPressure):
                        decision = cast(object, getattr(parsed, "decision"))
                        legacy_pressure = cast(object, getattr(decision, "pressure"))
                        self.assertEqual(cast(object, getattr(legacy_pressure, "condition")), "swap")
                    restored = cast(records.AdmissionWaitSnapshot,
                        codec.parse_snapshot_value(raw, records.AdmissionWaitSnapshot))
                    self.assertEqual(restored, snapshot)
                    if isinstance(pressure, records.MemoryPressure):
                        assert isinstance(restored.decision.pressure, records.MemoryPressure)
                        self.assertEqual(restored.decision.pressure.condition, "some")

    def test_pinned_client_schema_ignores_new_service_states_in_the_optional_field(self) -> None:
        with pinned_snapshot_reader() as (parse, schema, pinned):
            for current in (records.ServiceJobStateNotRecorded(), WaitingServiceJob(100), UnmatchedServiceJob(100)):
                with self.subTest(state=current):
                    snapshot = service_snapshot()
                    original = snapshot.running[0].measured
                    assert isinstance(original, records.UnownedSliceMemory)
                    snapshot = replace(snapshot, running=(replace(snapshot.running[0],
                        measured=replace(original, service_job_state=current)),))
                    raw = cast(dict[str, object], codec.snapshot_value(snapshot))
                    self.assertEqual(codec.parse_snapshot_value(raw, records.AdmissionWaitSnapshot), snapshot)
                    running = cast(list[dict[str, object]], raw["running"])
                    memory = cast(dict[str, object], running[0]["measured"])
                    parsed = parse(raw, schema)
                    steps = cast(tuple[object, ...], getattr(parsed, "running"))
                    measured = cast(object, getattr(steps[0], "measured"))
                    self.assertEqual(cast(object, getattr(measured, "bytes")), 123)
                    state = cast(object, getattr(measured, "job_state"))
                    self.assertIsInstance(state, RunningServiceJob)
                    self.assertEqual(cast(object, getattr(state, "started_at")), 100)
                    self.assertFalse(hasattr(measured, "service_job_state"))
                    memory["job_state"] = {"kind": "WaitingServiceJob", "started_at": 100}
                    with self.assertRaisesRegex(ValueError, "^invalid snapshot variant$"):
                        _ = parse(raw, schema)

            remote = records.RunningStep("remote-session", "remote build", "test", "/private", 0,
                records.RemoteMemoryUnavailable("the provider has no memory reading"), pid=records.PidNotRecorded(),
                execution=execution_identity.ExecutionEvidenceUnavailable("no remote process", 100), assignment_host="fixture-mac")
            snapshot = replace(service_snapshot(), running=(remote,))
            with self.subTest(message="Phase 44 snapshot"):
                raw = codec.snapshot_value(snapshot)
                self.assertEqual(codec.parse_snapshot_value(raw, records.AdmissionWaitSnapshot), snapshot)
                parsed = parse(raw, schema)
                steps = cast(tuple[object, ...], getattr(parsed, "running"))
                self.assertEqual(steps, ())
                encoded = cast(dict[str, object], raw)
                self.assertEqual(encoded["running"], [])
                self.assertEqual(encoded["running_with_remote"], [codec.snapshot_value(remote)])

            from scripts.build_queue.queue_fixture import head_reading

            reading = head_reading()
            reading = replace(reading, view=replace(reading.view, running=(remote,)),
                              explanation=replace(reading.explanation, running=(records.ExplainedRunningStep(
                                  remote, records.AdmissionTimeNotRecorded(), 0, 0, 0),)))
            parse_reply = cast(Callable[[str], object], getattr(pinned, "parse_reply"))
            for reply in (wire.ViewReply(reading), wire.ExplainReply(reading.explanation), wire.MineReply(reading)):
                with self.subTest(message=type(reply).__name__):
                    text = wire.encode_reply(reply)
                    self.assertEqual(wire.parse_reply(text), reply)
                    self.assertEqual(type(parse_reply(text)).__name__, type(reply).__name__)

            destination = placement.PlacementDestination("mac", "mac", "fixture-mac", WholeMacScope())
            for source in (execution_identity.SourceKeyKnown("source-a"), execution_identity.SourceKeyUnavailable("no source")):
                assignment = placement.PlacementAssignment("episode", "attempt", destination, source,
                    placement.SubmittingOwner("fixture-local", 42, "start"), NoOwningSession(), 100)
                claim = placement.ProviderClaimReference("mac", "mac", destination.scope, "claim", "episode", "attempt")
                for reply in (wire.Assignment(assignment), wire.DispatchStatus(assignment,
                        placement.CurrentAssignment(), placement.PermissionAvailable()), wire.FirstAuthorization(assignment, claim)):
                    with self.subTest(message=type(reply).__name__, source=source):
                        text = wire.encode_reply(reply)
                        self.assertEqual(wire.parse_reply(text), reply)
                        parsed_assignment = cast(object, getattr(parse_reply(text), "assignment"))
                        self.assertEqual(cast(str, getattr(parsed_assignment, "source_key")),
                                         source.key if isinstance(source, execution_identity.SourceKeyKnown) else "")

    def test_every_snapshot_reply_keeps_remote_rows_out_of_the_pinned_memory_union(self) -> None:
        from scripts.build_queue.queue_fixture import head_reading

        local = service_snapshot().running[0]
        remote = replace(local, step="live remote build", assignment_host="fixture-linux",
                         measured=records.RemoteMemoryUnavailable("remote memory is unavailable"))
        reading = head_reading()
        reading = replace(reading, view=replace(reading.view, running=(local, remote)),
            explanation=replace(reading.explanation, running=tuple(
                records.ExplainedRunningStep(step, records.AdmissionTimeNotRecorded(), 0, 0, 0)
                for step in (local, remote))))
        self.assertFalse(hasattr(reading.explanation.decided_from, "override"))
        waiting = wait_reply(records.NeedsMemory())
        waiting = replace(waiting, snapshot=replace(waiting.snapshot, running=(local, remote)))
        replies = (wire.ViewReply(reading), wire.ExplainReply(reading.explanation), wire.MineReply(reading), waiting)
        with pinned_snapshot_reader() as (_parse, _schema, pinned):
            parse_reply = cast(Callable[[str], object], getattr(pinned, "parse_reply"))
            for reply in replies:
                with self.subTest(reply=type(reply).__name__):
                    encoded = wire.encode_reply(reply)
                    self.assertEqual(wire.parse_reply(encoded), reply)
                    decoded = parse_reply(encoded)
                    self.assertEqual(type(decoded).__name__, type(reply).__name__)
                    if isinstance(reply, (wire.ViewReply, wire.MineReply)):
                        old_reading = cast(object, getattr(decoded, "reading"))
                        old_view = cast(object, getattr(old_reading, "view"))
                        old_explanation = cast(object, getattr(old_reading, "explanation"))
                        containers = (old_view, old_explanation)
                    elif isinstance(reply, wire.ExplainReply):
                        containers = (cast(object, getattr(decoded, "explanation")),)
                    else:
                        containers = (cast(object, getattr(decoded, "snapshot")),)
                    for container in containers:
                        rows = cast(tuple[object, ...], getattr(container, "running"))
                        self.assertEqual(len(rows), 1)
                        row = rows[0]
                        step = cast(object, getattr(row, "step")) if type(row).__name__ == "ExplainedRunningStep" else row
                        self.assertEqual(cast(str, getattr(step, "step")), local.step)
                        measured = cast(object, getattr(step, "measured"))
                        self.assertNotEqual(type(measured).__name__, "GoneMemory")

    def test_pinned_client_never_decodes_a_live_remote_step_as_gone(self) -> None:
        snapshot = service_snapshot()
        remote = replace(snapshot.running[0], step="remote test",
                         measured=records.RemoteMemoryUnavailable("no remote memory reader"),
                         assignment_host="fixture-mac")
        snapshot = replace(snapshot, running=(*snapshot.running, remote))
        raw = cast(dict[str, object], codec.snapshot_value(snapshot))
        self.assertEqual(codec.parse_snapshot_value(raw, records.AdmissionWaitSnapshot), snapshot)
        with pinned_snapshot_reader() as (parse, schema, _pinned):
            parsed = parse(raw, schema)
        steps = cast(tuple[object, ...], getattr(parsed, "running"))
        self.assertEqual(len(steps), 1)
        self.assertEqual(cast(str, getattr(steps[0], "step")), "service")
        self.assertTrue(all(type(cast(object, getattr(step, "measured"))).__name__ != "GoneMemory" for step in steps))
        self.assertEqual(raw["running_with_remote"],
                         [codec.snapshot_value(snapshot.running[0]), codec.snapshot_value(remote)])

    def test_pinned_client_reads_each_wait_reason_and_the_current_client_keeps_it(self) -> None:
        causes: dict[type, records.HeadCause] = {
            records.MemoryPressure: records.MemoryPressure(0, 91, 50, condition="some"),
            records.PressureUnavailable: records.PressureUnavailable("no pressure file"),
            records.ReadingUnavailable: records.ReadingUnavailable(),
            records.StartSpacingWait: records.StartSpacingWait(1000.5),
            records.NeedsMemory: records.NeedsMemory(8, 4, 6, 1, 2),
            records.EstimateExpired: records.EstimateExpired(1000.0),
            records.BehindHead: records.BehindHead("organon", 900.0, "session"),
            records.GpuSlotWait: records.GpuSlotWait(1, 1),
            records.RunLimitWait: records.RunLimitWait(2, 2, "claude_commands", "natedev"),
            placement.CargoFolderWait: placement.CargoFolderWait(),
            records.BuildSpaceLow: records.BuildSpaceLow(125, 150),
            records.BuildSpaceUnreadable: records.BuildSpaceUnreadable("fixture disk offline"),
            records.ReadyForAdmission: records.ReadyForAdmission(),
        }
        self.assertEqual(set(causes), set(get_args(records.HeadCause)))
        snapshot = wait_reply(records.NeedsMemory()).snapshot
        reasons: list[records.WaitReason] = [*causes.values(), records.TokenWaitingWaiter(1000.0),
                                             records.NatedevHeld("fps", "Phase 3 timing")]
        reasons += [snapshot_with_head_cause(snapshot, cause).head_reason for cause in causes.values()]
        self.assertEqual({type(reason) for reason in reasons}, set(get_args(records.WaitReason)))
        for revision in OLDER_CLIENTS:
            with pinned_snapshot_reader(revision) as (_parse, _schema, pinned):
                parse_reply = cast(Callable[[str], object], getattr(pinned, "parse_reply"))
                for reason in reasons:
                    cause = reason.cause if isinstance(reason, records.HeadAdmissionStatus) else reason
                    with self.subTest(revision=revision, reason=type(reason).__name__, cause=type(cause).__name__):
                        reply = replace(wait_reply(reason), snapshot=replace(snapshot, head_reason=reason))
                        line = wire.encode_reply(reply)
                        self.assertEqual(wire.parse_reply(line), reply)
                        older_reason = cast(object, getattr(parse_reply(line), "reason"))
                        older_cause = (cast(object, getattr(older_reason, "cause"))
                                       if type(older_reason).__name__ == "HeadAdmissionStatus" else older_reason)
                        legacy_wait = (records.GpuSlotWait | records.RunLimitWait | records.NatedevHeld | placement.CargoFolderWait
                                       | records.BuildSpaceHold)
                        self.assertEqual(type(older_cause).__name__,
                                         "BehindHead" if isinstance(cause, legacy_wait) else type(cause).__name__)
                        if isinstance(cause, records.BuildSpaceHold):
                            self.assertEqual(cast(str, getattr(older_cause, "head_name")), "the build space floor")
                        if isinstance(cause, records.RunLimitWait):
                            self.assertEqual(cast(str, getattr(older_cause, "head_name")),
                                             "2 of 2 claude_commands runs on natedev")

    def test_each_pinned_client_reads_a_machine_choice_row_and_the_current_client_keeps_it(self) -> None:
        reading = head_reading()
        row = replace(reading.view.waiting[0], reason=records.MachineChoiceWait())
        reading = replace(reading, view=replace(reading.view, waiting=(row, *reading.view.waiting[1:])))
        reply = wire.ViewReply(reading)
        line = wire.encode_reply(reply)
        self.assertEqual(wire.parse_reply(line), reply)
        for revision in (*OLDER_CLIENTS, CI_CLIENT):
            with self.subTest(revision=revision), pinned_snapshot_reader(revision) as (_parse, _schema, pinned):
                older = cast(Callable[[str], object], getattr(pinned, "parse_reply"))(line)
                view = cast(object, getattr(cast(object, getattr(older, "reading")), "view"))
                waiting = cast(tuple[object, ...], getattr(view, "waiting"))
                reason = cast(object, getattr(waiting[0], "reason"))
                self.assertEqual(type(reason).__name__, "BehindHead")
                self.assertEqual(cast(str, getattr(reason, "head_name")), "a machine choice")

    def test_the_pinned_ci_client_reads_a_natedev_hold_as_a_wait_and_the_current_client_keeps_it(self) -> None:
        held = records.NatedevHeld("fps", "Phase 3 timing")
        snapshot = wait_reply(records.NeedsMemory()).snapshot
        reply = replace(wait_reply(held, 3, "", records.held_text(held)), snapshot=replace(snapshot, head_reason=held))
        line = wire.encode_reply(reply)
        self.assertEqual(wire.parse_reply(line), reply)
        with pinned_snapshot_reader(CI_CLIENT) as (_parse, _schema, pinned):
            older = cast(Callable[[str], object], getattr(pinned, "parse_reply"))(line)
        for reason in (cast(object, getattr(older, "reason")),
                       cast(object, getattr(cast(object, getattr(older, "snapshot")), "head_reason"))):
            self.assertEqual(type(reason).__name__, "BehindHead")
            self.assertEqual(cast(str, getattr(reason, "head_name")), "the natedev hold by fps for Phase 3 timing")
        self.assertEqual(cast(str, getattr(older, "text")), "natedev is held by fps for Phase 3 timing; waiting for release")

    def test_gpu_capacity_wait_keeps_its_shape_in_a_field_with_no_wait_behind_the_head(self) -> None:
        waiting = wait_reply(records.NeedsMemory()).snapshot
        decision = replace(waiting.decision, gpu=records.GpuSlotWait(1, 1))
        status = snapshot_with_head_cause(waiting, records.GpuSlotWait(1, 1)).head_reason
        assert isinstance(status, records.HeadAdmissionStatus)
        status = replace(status, decision=decision)
        snapshot = replace(waiting, decision=decision, head_reason=status)
        reply = replace(wait_reply(status), snapshot=snapshot)
        line = wire.encode_reply(reply)
        written = cast(dict[str, object], cast(dict[str, object], codec.snapshot_value(snapshot))["decision"])
        self.assertEqual(written["gpu"], {"kind": "GpuSlotWait", "used": 1, "slots": 1})
        self.assertNotIn("gpu" + codec.GPU_SLOT_SUFFIX, written)
        self.assertEqual(wire.parse_reply(line), reply)
        for revision in OLDER_CLIENTS:
            with self.subTest(revision=revision), pinned_snapshot_reader(revision) as (_parse, _schema, pinned):
                parsed = cast(Callable[[str], object], getattr(pinned, "parse_reply"))(line)
                older_snapshot = cast(object, getattr(parsed, "snapshot"))
                gpu = cast(object, getattr(cast(object, getattr(older_snapshot, "decision")), "gpu", None))
                self.assertEqual(type(gpu).__name__, "NoneType" if revision == "7fdfd9a6a" else "GpuSlotWait")

    def test_each_older_service_reads_a_command_kind_request_and_the_new_service_reads_each_older_request(self) -> None:
        destination = placement.PlacementDestination("ssh", "mac", "fixture-mac", RepositoryScope("hana"))
        profile = records.StepProfile("hana", "zsh", "tree", records.ExpectedPeak(GIB, "measured", 5), estimated_at=1000.0)
        for kind in (*placement.MAC_RUN_KINDS, "test", placement.CommandKindNotSent()):
            constraints = placement.PlacementConstraints(execution_identity.SourceKeyKnown("source-a"), ("/bin/zsh", "-c", "true"),
                "/repo/hana", "hana", NoOwningSession(), (placement.PlacementCandidate(destination),), command_kind=kind)
            request = wire.PlacementRequest("episode-a", placement.SubmittingOwner("natedev", 42, "100"), 1000.0,
                                            profile, constraints)
            line = wire.encode_request(request)
            self.assertEqual(wire.parse_request(line), request)
            for revision in (*OLDER_CLIENTS, "429f57a6c"):
                with self.subTest(kind=kind, revision=revision), pinned_snapshot_reader(revision):
                    # The older service parses the new request, and its client writes the request again in its own shape.
                    older_wire = sys.modules["_pinned_admission_wire"]
                    older = cast(Callable[[str], object], getattr(older_wire, "parse_request"))(line)
                    older_line = cast(Callable[[object], str], getattr(older_wire, "encode_request"))(older)
                    self.assertNotIn("command_kind", older_line)
                    self.assertEqual(wire.parse_request(older_line),
                                     replace(request, constraints=replace(constraints, command_kind=placement.CommandKindNotSent())))

    def test_acquisition_defaults_unused_variants_and_restores_strict_reads(self) -> None:
        for field in ("job_state", "service_job_state"):
            with self.subTest(field=field):
                raw = cast(dict[str, object], codec.snapshot_value(service_snapshot()))
                running = cast(list[dict[str, object]], raw["running"])
                memory = cast(dict[str, object], running[0]["measured"])
                memory[field] = {"kind": "FutureServiceJob"}
                with codec.snapshot_read_policy(codec.SnapshotReadPolicy.ACQUISITION_FIELDS):
                    parsed = cast(records.AdmissionWaitSnapshot, codec.parse_snapshot_value(raw, records.AdmissionWaitSnapshot))
                measured = parsed.running[0].measured
                self.assertIsInstance(measured, records.UnownedSliceMemory)
                assert isinstance(measured, records.UnownedSliceMemory)
                self.assertEqual(measured.bytes, 123)
                self.assertEqual(measured.job_state, NotServiceSlice())
                self.assertEqual(measured.service_job_state, records.ServiceJobStateNotRecorded())
                with self.assertRaisesRegex(ValueError, "^invalid snapshot variant$"):
                    _ = codec.parse_snapshot_value(raw, records.AdmissionWaitSnapshot)

    def test_acquisition_refuses_unknown_variants_in_a_used_field(self) -> None:
        raw = cast(dict[str, object], codec.snapshot_value(service_snapshot()))
        raw["head_reason"] = {"kind": "FutureAdmissionReason"}
        with codec.snapshot_read_policy(codec.SnapshotReadPolicy.ACQUISITION_FIELDS), \
             self.assertRaisesRegex(ValueError, "^invalid snapshot variant$"):
            _ = codec.parse_snapshot_value(raw, records.AdmissionWaitSnapshot)

    def test_literal_unions_accept_each_member_and_refuse_unknown_values(self) -> None:
        schema = Literal["ready"] | Literal["waiting"]
        for value in ("ready", "waiting"):
            self.assertEqual(codec.parse_snapshot_value(value, schema), value)
        for value in ("unknown", 0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ = codec.parse_snapshot_value(value, schema)

    def test_mac_outage_reports_preserve_evidence_in_saved_state(self) -> None:
        for evidence in placement.MacOutage:
            report = placement.TerminalAttemptReport(placement.AttemptLost(evidence, 10.0),
                placement.DispatchPathSettled(), 610.0)
            self.assertEqual(codec.snapshot_value(evidence), evidence.name)
            self.assertEqual(wire.parse_record(wire.encode_record(report), placement.TerminalAttemptReport), report)
        for value in ("unknown", 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "invalid Mac outage evidence"):
                _ = codec.parse_snapshot_value(value, placement.MacOutage)

    def test_nested_union_records_keep_tags_and_optional_fields(self) -> None:
        for record in (Ready(3), Unavailable("not read")):
            raw = {"state": "waiting", "value": codec.snapshot_value(record)}
            self.assertEqual(codec.parse_snapshot_value(raw, Reading),
                             {"state": "waiting", "value": record})
            raw["detail"] = "fixture"
            self.assertEqual(codec.parse_snapshot_value(raw, Reading),
                             {"state": "waiting", "value": record, "detail": "fixture"})
        with self.assertRaises(ValueError):
            _ = codec.parse_snapshot_value({"state": "ready", "value": {"kind": "Other"}}, Reading)

    def test_scalar_unions_keep_strict_types_and_finite_numbers(self) -> None:
        schema = list[int | str | float]
        self.assertEqual(codec.parse_snapshot_value([1, "sample", 2.5], schema), [1, "sample", 2.5])
        for value in (True, float("nan"), float("inf"), None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ = codec.parse_snapshot_value([value], schema)


if __name__ == "__main__":
    _ = unittest.main()
