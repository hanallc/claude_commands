"""Print the wait-holder flags that verify.sh passes to the phase wait recorder."""

from __future__ import annotations

if __name__ == "__main__":
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})

import json
import os
from pathlib import Path
import sys
from typing import cast
from uuid import UUID


def text(value: object) -> str:
    return value.strip() if isinstance(value, str) and not any(c in value for c in "\r\n\t\0") else ""


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def mapping(value: object) -> dict[str, object]:
    return {str(key): entry for key, entry in cast(dict[object, object], value).items()} if isinstance(value, dict) else {}


def fields(raw: str) -> dict[str, object]:
    try:
        return mapping(cast(object, json.loads(raw)))
    except ValueError:
        return {}


def token_owner(payload: str, board: Path) -> tuple[str, str]:
    """Name the session, or else the unit, that holds the token slot."""
    from scripts.lib import unit_runs
    from scripts.production import staff

    if payload == "director":
        return "--holder-session", unit_runs.director(board)
    if not payload or payload == "unknown":
        return "--holder-session", ""
    matches: list[dict[str, object]] = []
    for path in board.glob("impl_worker_*.json"):
        worker = fields(read(path))
        if text(worker.get("slot")) == payload or path.stem == "impl_worker_" + payload:
            matches.append(worker)
    state = fields(read(board / "progress_history_state.json"))
    if matches:
        reference = text(matches[0].get("staff_id")) if len(matches) == 1 else ""
    else:
        active_pass = mapping(state.get("pass", {}))
        entry = mapping(active_pass.get(payload, {}))
        if payload not in active_pass:
            workers = [worker for worker in map(mapping, active_pass.values())
                       if worker.get("worker_name") == payload]
            entry = workers[0] if len(workers) == 1 else {}
        reference = text(entry.get("worker_id"))
    try:
        record = staff.snapshot()
        member = record.by_id(staff.StaffId(reference)) if reference else None
        if member is not None and member.current_session:
            return "--holder-session", member.current_session
        unit = unit_runs.run_owner(state, record)
        return "--holder-unit", unit.name if isinstance(unit, staff.StaffRow) else ""
    except (OSError, ValueError):
        return "--holder-session", ""


def hold_owner(hold_name: str) -> tuple[str, str]:
    """Name the session, or else the holder name, of the natedev hold that blocks this caller."""
    from scripts.build_hold import build_hold, owning_session
    from scripts.build_targets import build_targets

    directory = Path(hold_name)
    holder = build_targets.hold_waited_on(build_hold.natedev_holds(directory), owning_session.owning_session(),
                                          directory, build_targets.current_caller_lineage())
    if not isinstance(holder, build_hold.Holder):
        return "--holder-session", ""
    session = build_hold.holder_session(holder.key)
    try:
        _ = UUID(session)
    except ValueError:
        return "--holder-unit", text(holder.name)
    return "--holder-session", session


def mac_owner(resource: str, repository: str) -> str:
    """Name another session whose live Mac claim overlaps this repository."""
    from scripts.mac_test import mac_test

    paths = mac_test.state_paths("" if resource == "mac" else resource)
    scope = mac_test.target_claim_scope(paths, repository)
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for claim in mac_test.read_claims(paths):
        if mac_test.scopes_overlap(claim.scope, scope) and mac_test.run_is_live(claim.record):
            candidate = text(claim.record.get("holder_session"))
            if candidate and candidate != session:
                return candidate
    return ""


def main() -> None:
    kind, payload, board_name, hold_name, repository = sys.argv[1:]
    owner, owner_flag = "", "--holder-session"
    resource_kind, resource = "slot", "cargo"
    if kind == "token":
        owner_flag, owner = token_owner(payload, Path(board_name))
    elif kind == "hold":
        resource = "natedev"
        owner_flag, owner = hold_owner(hold_name)
    elif kind in ("mac", "target"):
        resource = payload or "mac"
        owner = mac_owner(resource, repository)
    elif kind == "provider":
        diagnostic = fields(payload)
        print(text(diagnostic.get("target")))
        print(text(diagnostic.get("what")))
    elif kind in ("memory", "resource"):
        holder = fields(payload)
        if kind == "resource":
            resource = text(holder.get("resource"))
            print(resource)
            print(text(holder.get("reason")))
            holder = mapping(holder.get("holder", {}))
        else:
            resource_kind, resource = "memory", "memory"
        if holder.get("kind") == "session":
            owner = text(holder.get("session_id"))
        elif holder.get("kind") == "unit":
            owner_flag, owner = "--holder-unit", text(holder.get("unit"))
    if owner:
        print("\n".join(("--holder-resource-kind", resource_kind, "--holder-resource", resource, owner_flag, owner)))


if __name__ == "__main__":
    main()
