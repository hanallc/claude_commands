"""Read detached-run records and their stop confirmations."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, TypeGuard, cast

from scripts.agents import worker_lifecycle
from scripts.lib import process_stop


CONFIRMED_STOP_SUFFIX = ".stop-confirmed"


class PaneDispatchUnavailable(TypedDict):
    unavailable: str


class StopFields(TypedDict, total=False):
    stop_reason: str
    scope_error: str
    hold_stop_requests: list[str]
    stop_pass_requested: Literal[True]
    self_stop: Literal[True]


class RunRecord(StopFields):
    pending_hold_stop_requests: NotRequired[list[str]]
    run_id: str
    status: Literal["running", "finished"]
    session_dir: str
    worker: str
    staff_id: str
    label: str
    command: list[str]
    # Older records have no kind.
    kind: NotRequired[Literal["detect", "python-tests", "other"]]
    log: str
    started_at: float
    pid: int
    supervisor_started: NotRequired[str]
    command_pid: NotRequired[int]
    # The command leads its own session, so this group is the command's process group.
    command_group: NotRequired[int]
    command_started: NotRequired[str]
    command_slice: NotRequired[process_stop.RunSliceIdentity]
    stop_notice_id: NotRequired[str]
    completion_after_slice_cleanup: NotRequired[Literal[True]]
    slice_cleanup_pending: NotRequired[Literal[True]]
    slice_leftovers: NotRequired[Literal[True]]
    director_dir: str
    done_message_file: str
    # The tree key of cwd at start, "" when unknown; records before 2026-10-21 have none.
    tree_key: NotRequired[str]
    exit_code: NotRequired[int]
    finished_at: NotRequired[str]
    delivery: NotRequired[str]
    delivered_at: NotRequired[float]
    ended_pass_result_released: NotRequired[Literal[True]]
    worker_result_receipt: NotRequired[object]
    blocked_notice_accepted: NotRequired[Literal[True]]
    push_retries: NotRequired[int]
    push_alert_attempted: NotRequired[Literal[True]]
    # Set when the run ended without its supervisor: "supervisor gone".
    completion_pending: NotRequired[bool]
    ended: NotRequired[str]
    # An empty id records a worker run outside a pass. Older records omit the field.
    pane_dispatch_id: NotRequired[str | PaneDispatchUnavailable]
    orphan_result_superseded: NotRequired[Literal[True]]


def _is_hold_stop_requests(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(
        isinstance(item, str) and re.fullmatch(r"[0-9a-f]{32}", item) is not None
        for item in cast(list[object], value)
    )


def is_detached_run_record(raw: dict[str, object]) -> TypeGuard[RunRecord]:
    pane_dispatch = raw.get("pane_dispatch_id")
    match raw:
        case {
            "run_id": str(run_id), "status": "running" | "finished", "session_dir": str(),
            "worker": str(), "staff_id": str(), "label": str(), "command": list(),
            "log": str(), "started_at": int() | float(), "pid": int(),
            "director_dir": str(), "done_message_file": str(),
        }:
            return (bool(run_id) and not isinstance(raw["started_at"], bool)
                    and not isinstance(raw["pid"], bool)
                    and all(isinstance(arg, str) for arg in cast(list[object], raw["command"]))
                    and all(key not in raw or type(raw[key]) is int
                            for key in ("command_pid", "command_group", "exit_code", "push_retries"))
                    and all(key not in raw or isinstance(raw[key], str)
                            for key in ("supervisor_started", "command_started", "tree_key", "finished_at",
                                        "delivery", "ended", "stop_reason", "scope_error", "stop_notice_id"))
                    and ("hold_stop_requests" not in raw or _is_hold_stop_requests(raw["hold_stop_requests"]))
                    and ("pending_hold_stop_requests" not in raw or (
                        raw.get("stop_pass_requested") is True and "command_slice" in raw
                        and _is_hold_stop_requests(raw["pending_hold_stop_requests"])))
                    and ("delivered_at" not in raw or type(raw["delivered_at"]) in (int, float))
                    and ("completion_pending" not in raw or isinstance(raw["completion_pending"], bool))
                    and ("command_slice" not in raw or process_stop.is_slice_identity(raw["command_slice"]))
                    and all(key not in raw or raw[key] is True
                            for key in ("stop_pass_requested", "self_stop", "orphan_result_superseded",
                                        "blocked_notice_accepted", "push_alert_attempted",
                                        "slice_cleanup_pending", "slice_leftovers",
                                        "completion_after_slice_cleanup", "ended_pass_result_released"))
                    and ("pane_dispatch_id" not in raw or isinstance(pane_dispatch, str)
                         or (isinstance(pane_dispatch, dict)
                             and isinstance(cast(dict[str, object], pane_dispatch).get("unavailable"), str))))
        case _:
            return False


def read_detached_stop_fields(path: Path, run_id: object) -> StopFields:
    saved = worker_lifecycle.read_json_object(path)
    if not saved:
        return {}
    reason = saved.get("stop_reason")
    requests = saved.get("hold_stop_requests", [])
    if (not isinstance(run_id, str) or saved.get("run_id") != run_id or not isinstance(reason, str)
            or not _is_hold_stop_requests(requests)
            or any(key in saved and saved[key] is not True for key in ("stop_pass_requested", "self_stop"))):
        raise worker_lifecycle.WorkerStateUnreadable(path, "invalid stop fields")
    fields: StopFields = {"stop_reason": reason}
    if isinstance(error := saved.get("scope_error"), str):
        fields["scope_error"] = error
    if requests:
        fields["hold_stop_requests"] = requests
    if saved.get("stop_pass_requested") is True:
        fields["stop_pass_requested"] = True
    if saved.get("self_stop") is True:
        fields["self_stop"] = True
    return fields


def read_detached_record_if_present(path: Path) -> RunRecord | None:
    raw = worker_lifecycle.read_json_object(path)
    if not raw and not path.exists():
        return None
    if not is_detached_run_record(raw):
        raise SystemExit(f"detached_runs: {path} is not a run record")
    if path.name != f"{raw['run_id']}.json":
        raise SystemExit(f"detached_runs: {path} does not match run ID {raw['run_id']}")
    return with_confirmed_detached_stop(path, raw)


def with_confirmed_detached_stop(path: Path, record: RunRecord) -> RunRecord:
    confirmed = read_detached_stop_fields(path.with_suffix(CONFIRMED_STOP_SUFFIX), record["run_id"])
    requests = confirmed.get("hold_stop_requests")
    if requests:
        confirmed["hold_stop_requests"] = list(dict.fromkeys([*record.get("hold_stop_requests", []), *requests]))
    return {**record, **confirmed} if confirmed else record


def confirms_hold_stop(path: Path, run_id: str, request_id: str) -> bool:
    """Check the published confirmation of this hold request."""
    try:
        record = read_detached_record(path)
    except (SystemExit, worker_lifecycle.WorkerStateUnreadable):
        return False
    return record["run_id"] == run_id and request_id in record.get("hold_stop_requests", [])


def read_detached_record(path: Path) -> RunRecord:
    record = read_detached_record_if_present(path)
    if record is None:
        raise SystemExit(f"detached_runs: {path} is not a run record")
    return record
