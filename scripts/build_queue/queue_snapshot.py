"""Store captured queue evidence for callers that cannot read the live service."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import cast

from scripts.build_queue import admission_protocol as wire, admission_records
from scripts.build_queue import snapshot_codec as codec

VIEW_NAME = "view.json"


@dataclass(frozen=True)
class QueueSnapshot:
    written_at: float
    inputs: admission_records.QueueInputs
    policy: admission_records.AdmissionPolicy


@dataclass(frozen=True)
class NoQueueSnapshot:
    pass


@dataclass(frozen=True)
class PublishedView:
    written_at: float
    fresh_until: float
    reading: admission_records.QueueRead


def _write_snapshot(path: Path, value: dict[str, object]) -> None:
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    temporary = path.with_name(path.name + ".tmp")
    try:
        _ = temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_queue_snapshot(directory: Path, inputs: admission_records.QueueInputs, policy: admission_records.AdmissionPolicy) -> None:
    """Publish one completed pass atomically without additional memory readings."""
    _write_snapshot(directory / "queue.json", {"v": 5, "written_at": inputs.now, "inputs": codec.snapshot_value(inputs),
                                               "policy": codec.snapshot_value(policy)})


def write_view_snapshot(directory: Path, written_at: float, fresh_until: float, reply: bytes) -> None:
    """Publish the same encoded reply that the socket serves."""
    _write_snapshot(directory / VIEW_NAME, {"written_at": written_at, "fresh_until": fresh_until,
                                           "reply": reply.decode("utf-8")})


def read_view_snapshot(directory: Path) -> PublishedView:
    """Read the complete published view or report the queue read error."""
    try:
        raw = cast(object, json.loads((directory / VIEW_NAME).read_bytes()))
        if not isinstance(raw, dict):
            raise ValueError("invalid view file")
        fields = cast(dict[str, object], raw)
        written_at = cast(float, codec.parse_snapshot_value(fields["written_at"], float))
        fresh_until = cast(float, codec.parse_snapshot_value(fields.get("fresh_until", written_at), float))
        encoded = fields["reply"]
        if not isinstance(encoded, str):
            raise ValueError("invalid view reply")
        reply = wire.parse_reply(encoded)
        if not isinstance(reply, wire.ViewReply):
            raise ValueError("invalid view reply")
        return PublishedView(written_at, fresh_until, reply.reading)
    except (OSError, ValueError, KeyError, RecursionError, OverflowError) as error:
        raise wire.AdmissionUnavailable("The queue did not answer.") from error


def read_queue_snapshot(directory: Path) -> QueueSnapshot | NoQueueSnapshot:
    """Refuse missing or incomplete evidence without reading the live queue."""
    try:
        raw = cast(object, json.loads((directory / "queue.json").read_text()))
        if not isinstance(raw, dict):
            return NoQueueSnapshot()
        envelope = cast(dict[str, object], raw)
        if type(envelope.get("v")) is not int or envelope["v"] != 5:
            return NoQueueSnapshot()
        written_at = cast(float, codec.parse_snapshot_value(envelope["written_at"], float))
        evidence = envelope["inputs"]
        if not isinstance(evidence, dict):
            return NoQueueSnapshot()
        fields = cast(dict[str, object], evidence)
        if "limits" in fields:
            return NoQueueSnapshot()
        inputs = cast(admission_records.QueueInputs, codec.parse_snapshot_value(fields, admission_records.QueueInputs))
        if sum(not isinstance(step.measured, (admission_records.CompilerServiceMemory, admission_records.UnownedSliceMemory))
               for step in inputs.running) != len(inputs.reservations):
            return NoQueueSnapshot()
        policy = cast(admission_records.AdmissionPolicy, codec.parse_snapshot_value(envelope["policy"], admission_records.AdmissionPolicy))
        return QueueSnapshot(written_at, inputs, policy)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return NoQueueSnapshot()
