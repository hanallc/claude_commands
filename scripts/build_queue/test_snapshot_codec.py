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

from scripts.build_queue import admission_records as records
from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue import admission_protocol as wire, placement
from scripts.build_queue.admission_test_support import GIB, snapshot_with_head_cause, wait_reply
from scripts.build_hold.owning_session import NoOwningSession
from scripts.mac_test.claim_state import RepositoryScope, WholeMacScope
from scripts.build_queue.scope_activity import NotServiceSlice, RunningServiceJob, WaitingServiceJob, UnmatchedServiceJob


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
                execution=placement.ExecutionEvidenceUnavailable("no remote process", 100), assignment_host="fixture-mac")
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
            for source in (placement.SourceKeyKnown("source-a"), placement.SourceKeyUnavailable("no source")):
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
                                         source.key if isinstance(source, placement.SourceKeyKnown) else "")

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
                        self.assertEqual(type(older_cause).__name__,
                                         "BehindHead" if isinstance(cause, (records.GpuSlotWait, records.NatedevHeld))
                                         else type(cause).__name__)

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

    def test_gpu_slot_wait_keeps_its_shape_in_a_field_with_no_wait_behind_the_head(self) -> None:
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
            constraints = placement.PlacementConstraints(placement.SourceKeyKnown("source-a"), ("/bin/zsh", "-c", "true"),
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
