"""Store and deliver the notices for one pass."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Collection, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

from scripts.agents import worker_lifecycle
from scripts.direct import pass_record, progress_history
from scripts.lib import atomic_file, scratch, unit_runs
from scripts.message import send
from scripts.production import staff

REPEAT_MINUTES = 52_596_000
NOTICE_FILES = ".*.pass-notices.jsonseq"
TURN_EVENT = "turn:"


class NoticeKind(StrEnum):
    RESULT = "result"
    STATUS = "status"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class Notice:
    text: str
    sender_native_id: str
    kind: NoticeKind
    transition: str = ""
    source_event_id: str = ""
    settled: bool = False
    legacy_stamp: str = ""
    # A busy or uncertain notice sends only while this dispatch delivery stays unconfirmed.
    unconfirmed_delivery: str = ""

    @property
    def ordered_turn(self) -> bool:
        """Only the latest-turn check of the turn end sends this notice."""
        return self.source_event_id.startswith(TURN_EVENT)


@dataclass(frozen=True)
class LegacyNoticeStamp:
    saved: str
    incoming: str

    @property
    def matches_replay(self) -> bool:
        return self.saved == self.incoming

    def settled_event(self, notice: Notice) -> Notice:
        if self.matches_replay:
            return replace(notice, settled=True, legacy_stamp=self.saved)
        return Notice(f"Legacy notice {self.saved}", notice.sender_native_id,
                      NoticeKind.STATUS, settled=True, legacy_stamp=self.saved)


@dataclass(frozen=True)
class Route:
    instance_id: str
    director_id: staff.StaffId
    worker_name: str


def _path(run: Path, instance: str) -> Path:
    digest = hashlib.sha256(instance.encode()).hexdigest()
    return run / f".{digest}.pass-notices.jsonseq"


@contextmanager
def _locked(path: Path, purpose: str) -> Generator[None]:
    with path.with_suffix(f".{purpose}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@dataclass(frozen=True)
class PassNotices:
    route: Route
    notices: dict[str, Notice]

    @classmethod
    def read(cls, path: Path) -> PassNotices | None:
        try:
            frames = path.read_bytes().split(b"\x1e")
        except FileNotFoundError:
            return None
        record = None
        seen: set[str] = set()
        for frame in frames:
            # A short write cannot complete a frame. The next append starts a new frame.
            if not frame.endswith(b"\n"):
                continue
            fields = pass_record.object_dict(cast(object, json.loads(frame)))
            if fields is None:
                raise ValueError(f"{path}: invalid pass notice frame")
            if isinstance(key := fields.get("settled"), str):
                if record is not None and (notice := record.notices.get(key)) is not None:
                    record.notices[key] = replace(notice, settled=True)
                continue
            route = _read_route(path, fields)
            notices = pass_record.object_dict(fields.get("notices")) or {}
            if record is None:
                record = cls(route, {})
                # Apply the legacy event only to the first complete publication.
                initial = pass_record.object_dict(fields.get("initial")) or {}
                notices = {**initial, **{key: value for key, value in notices.items() if key not in initial}}
            for key, value in notices.items():
                if key in seen:
                    continue
                seen.add(key)
                notice = _read_notice(path, key, value)
                if notice.kind == NoticeKind.RESULT:
                    record = cls(record.route, {name: entry for name, entry in record.notices.items()
                                                if entry.settled or entry.kind == NoticeKind.DIAGNOSTIC})
                record.notices[key] = notice
        return record


def _read_route(path: Path, fields: dict[str, object]) -> Route:
    route = pass_record.object_dict(fields.get("route")) or {}
    instance, director, worker = (pass_record.string(route.get(field))
                                  for field in ("instance_id", "director_id", "worker_name"))
    if not all((instance, director, worker)) or path != _path(path.parent, instance):
        raise ValueError(f"{path}: invalid pass notice route")
    return Route(instance, staff.StaffId(director), worker)


def _read_notice(path: Path, key: str, value: object) -> Notice:
    fields = pass_record.object_dict(value) or {}
    text = pass_record.string(fields.get("text"))
    sender = pass_record.string(fields.get("sender_native_id"))
    if not text or not sender:
        raise ValueError(f"{path}: incomplete pass notice {key}")
    return Notice(text, sender, NoticeKind(pass_record.string(fields.get("kind"))),
                  pass_record.string(fields.get("transition")), pass_record.string(fields.get("source_event_id")),
                  fields.get("settled") is True, pass_record.string(fields.get("legacy_stamp")),
                  pass_record.string(fields.get("unconfirmed_delivery")))


def _append(path: Path, content: dict[str, object]) -> None:
    frame = b"\x1e" + json.dumps(content).encode() + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if os.write(descriptor, frame) != len(frame):
            raise OSError(f"{path}: incomplete pass notice write")
        os.fsync(descriptor)
        atomic_file.sync_folder(path.parent)
    finally:
        os.close(descriptor)


def _publish(path: Path, route: Route, notices: dict[str, Notice], *, initial: dict[str, Notice] | None = None) -> None:
    content: dict[str, object] = {"route": asdict(route),
                                 "notices": {key: asdict(notice) for key, notice in notices.items()}}
    if initial:
        content["initial"] = {key: asdict(notice) for key, notice in initial.items()}
    _append(path, content)


def stage_result(run: Path, route: Route, key: str, notice: Notice, *, legacy: LegacyNoticeStamp) -> None:
    path = _path(run, route.instance_id)
    initial = {key if legacy.matches_replay else "status:1": legacy.settled_event(notice)} if legacy.saved else {}
    with _locked(path, "record"):
        _publish(path, route, {key: notice}, initial=initial)


def stage_status(run: Path, route: Route, notice: Notice, *, legacy: LegacyNoticeStamp) -> str:
    """Save a status transition and return its event ID."""
    path = _path(run, route.instance_id)
    with _locked(path, "record"):
        record = PassNotices.read(path)
        if record is None and legacy.saved:
            imported = {"status:1": legacy.settled_event(notice)}
            if not legacy.matches_replay:
                imported["status:2"] = notice
            _publish(path, route, imported)
            return "status:1" if legacy.matches_replay else "status:2"
        saved = {} if record is None else record.notices
        if any(entry.kind == NoticeKind.RESULT for entry in saved.values()):
            return ""
        statuses = [(key, entry) for key, entry in saved.items() if entry.kind == NoticeKind.STATUS]
        for key, entry in statuses:
            if notice.source_event_id and entry.source_event_id == notice.source_event_id:
                return key
            if entry.legacy_stamp == legacy.incoming and (not entry.source_event_id or not notice.source_event_id):
                return key
        if not notice.source_event_id and statuses and statuses[-1][1].transition == notice.transition:
            return statuses[-1][0]
        key = f"status:{len(statuses) + 1}"
        _publish(path, route, {key: notice})
        return key


def _diagnostics(sender: str, messages: dict[str, bool]) -> dict[str, Notice]:
    return {f"diagnostic:{hashlib.sha256(text.encode()).hexdigest()}":
            Notice(text, sender, NoticeKind.DIAGNOSTIC, settled=settled)
            for text, settled in messages.items()}


def stage_diagnostics(run: Path, route: Route, sender: str, messages: Collection[str]) -> tuple[str, ...]:
    if not messages:
        return ()
    path = _path(run, route.instance_id)
    notices = _diagnostics(sender, dict.fromkeys(messages, False))
    with _locked(path, "record"):
        _publish(path, route, notices)
    return tuple(notices)


def stage_ended_run_result(run: Path, route: Route, sender: str, run_id: str, message: str) -> str:
    """Keep one director notice for a detached run after its pass ends."""
    path = _path(run, route.instance_id)
    key = f"detached-result:{run_id}"
    ending = f"\n\nThe worker pass {route.instance_id} has ended."
    with _locked(path, "record"):
        saved = PassNotices.read(path)
        if saved is not None:
            if saved.route.director_id != route.director_id:
                raise ValueError("the pass notice recipient changed")
            if key in saved.notices:
                return key
            for old_key, notice in saved.notices.items():
                if (old_key.startswith("diagnostic:") and notice.text.startswith(f"Detached run: {run_id}\n")
                        and notice.text.endswith(ending)):
                    return old_key
        _publish(path, route, {key: Notice(message + ending, sender, NoticeKind.DIAGNOSTIC)})
    return key


def import_legacy_diagnostics(run: Path, route: Route, sender: str) -> None:
    path = _path(run, route.instance_id)
    legacy = path.with_name(path.name.replace(".pass-notices.jsonseq", ".completion-diagnostics.json"))
    if not legacy.is_file():
        return
    with _locked(path, "record"):
        notices = _diagnostics(sender, {text: settled is True for text, settled in
                                        worker_lifecycle.read_json_object(legacy).items()})
        if notices:
            _publish(path, route, notices)
            legacy.unlink(missing_ok=True)


def _delivery_key(route: Route, key: str) -> str:
    return f"worker-completion:{route.instance_id}:{key}"


def _received(route: Route, key: str) -> bool:
    message = send.Message(send.staff_queue_key(route.director_id), route.worker_name, "", "",
                           _delivery_key(route, key))
    return send.confirmed_skip(message, timedelta(minutes=REPEAT_MINUTES), send.now()) is not None


def _completion_scratch_path(run: Path, instance: str) -> Path:
    digest = hashlib.sha256(instance.encode()).hexdigest()
    return run / f".{digest}.completion-scratch.json"


def save_completion_scratch(run: Path, instance: str, retained: list[scratch.KeptFolder]) -> None:
    if not retained:
        return
    path = _completion_scratch_path(run, instance)
    saved = worker_lifecycle.read_json_object(path)
    updated = saved | {str(folder.path.parent.resolve() / folder.path.name): folder.reason for folder in retained}
    if updated != saved:
        atomic_file.write_text_atomic(path, json.dumps(updated))


def _completion_text(run: Path, instance: str, saved_text: str) -> str:
    path = _completion_scratch_path(run, instance)
    lines = [line for line in saved_text.split("\n") if not scratch.is_retained_task_folder_notice(line)]
    for name, reason in worker_lifecycle.read_json_object(path).items():
        folder = Path(name)
        if not folder.is_absolute() or not isinstance(reason, str):
            raise worker_lifecycle.WorkerStateUnreadable(path, f"invalid retained folder entry {name!r}")
        try:
            _ = folder.lstat()
        except FileNotFoundError:
            continue
        lines.append(scratch.retained_task_folder_notice(folder, reason))
    return "\n".join(lines)


def _send(run: Path, route: Route, key: str, notice: Notice) -> send.Result:
    director = staff.snapshot().by_id(route.director_id)
    if director is None:
        raise ValueError(f"{route.director_id}: notice recipient is not in staff")
    prefix = "codex:" if isinstance(staff.backend(director), staff.CodexBackend) else "session:"
    body = (_completion_text(run, route.instance_id, notice.text)
            if notice.kind == NoticeKind.RESULT and key != "stopped" else notice.text)
    options = send.parse(["--to", prefix + str(director.current_session), "--from", route.worker_name,
                          "--text", body, "--key", _delivery_key(route, key),
                          "--recipient-key", send.staff_queue_key(route.director_id),
                          "--repeat-minutes", str(REPEAT_MINUTES)])
    return send.send(options._replace(sender_origin=send.ForwardedSender(send.read_sender_stamp(notice.sender_native_id)),
                                      shown_sender=send.ShownSender(route.worker_name)))


def _settle(path: Path, key: str) -> None:
    with _locked(path, "record"):
        _append(path, {"settled": key})


def read(run: Path, instance: str) -> PassNotices | None:
    return PassNotices.read(_path(run, instance))


def is_settled(run: Path, instance: str, key: str) -> bool:
    record = read(run, instance)
    return record is not None and (notice := record.notices.get(key)) is not None and notice.settled


def _deliver(path: Path, keys: Collection[str], *, retry: bool = False) -> None:
    # Keep transport outside the record lock so a new assignment can save its state.
    with _locked(path, "delivery"):
        status_blocked = False
        for key in keys:
            with _locked(path, "record"):
                record = PassNotices.read(path)
            if record is None or (notice := record.notices.get(key)) is None or notice.settled:
                continue
            if status_blocked and notice.kind == NoticeKind.STATUS:
                continue
            try:
                if notice.unconfirmed_delivery and not progress_history.dispatch_delivery_pending(
                        path.parent, record.route.instance_id, notice.unconfirmed_delivery):
                    _settle(path, key)
                    continue
                if not retry or not _received(record.route, key):
                    result = _send(path.parent, record.route, key, notice)
                    if not isinstance(result, send.RepeatGuardHeld) and result.outcome not in ("sent", "skipped"):
                        if result.outcome == "held" and notice.kind == NoticeKind.DIAGNOSTIC:
                            continue
                        raise ValueError(result.detail)
                _settle(path, key)
            except (OSError, ValueError, staff.StaffRefused) as error:
                if not retry:
                    raise
                status_blocked |= notice.kind == NoticeKind.STATUS
                unit_runs.report_anomaly(" ".join(f"anomaly: {path}: pass notice retry failed: {error}".splitlines()))


def deliver(run: Path, instance: str, keys: Collection[str]) -> None:
    if keys:
        _deliver(_path(run, instance), keys)


def deliver_status(run: Path, instance: str, event: str) -> None:
    record = read(run, instance)
    if record is None or event not in record.notices:
        return
    pending: list[str] = []
    for key, notice in record.notices.items():
        if notice.kind == NoticeKind.STATUS and not notice.settled and (key == event or not notice.ordered_turn):
            pending.append(key)
        if key == event:
            break
    deliver(run, instance, pending)


def retry_result(run: Path, instance: str, event: str) -> None:
    _deliver(_path(run, instance), (event,), retry=True)


def deliver_diagnostics(run: Path, instance: str) -> None:
    path = _path(run, instance)
    record = PassNotices.read(path)
    if record is not None:
        pending = tuple(key for key, notice in record.notices.items()
                        if notice.kind == NoticeKind.DIAGNOSTIC and not notice.settled)
        if pending:
            _deliver(path, pending)


def retry(run: Path, worker: str = "", *, exclude_instance: str = "", retry_excluded_diagnostics: bool = False) -> None:
    for path in run.glob(NOTICE_FILES):
        try:
            record = PassNotices.read(path)
            if record is None or (worker and record.route.worker_name != worker):
                continue
            excluded = record.route.instance_id == exclude_instance
            # The pending-DONE retry of the turn end owns each ordered turn notice.
            pending = tuple(key for key, notice in record.notices.items() if not notice.settled and not notice.ordered_turn
                            and (not excluded or retry_excluded_diagnostics and notice.kind == NoticeKind.DIAGNOSTIC))
            if pending:
                _deliver(path, pending, retry=True)
        except (OSError, ValueError, staff.StaffRefused) as error:
            unit_runs.report_anomaly(" ".join(f"anomaly: {path}: pass notice retry failed: {error}".splitlines()))
