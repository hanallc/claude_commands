#!/usr/bin/env python3
"""SessionStart hook: record the Claude launch of the herdr pane this session runs in.

The entry is what a restore needs to resume the pane on its own account: see
`scripts/agents/pane_record.py`. A session outside herdr gets nothing.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import subprocess
import sys
from typing import TypedDict, cast
from collections.abc import Mapping

from scripts.agents import pane_record  # noqa: E402
from scripts.herdr import herdr, herdr_types, pane_launch
from scripts.production import session_staff, staff  # noqa: E402
from scripts.hooks import staff_session_identity  # noqa: E402


class HookInput(TypedDict, total=False):
    session_id: str
    cwd: str
    source: str


def _check_pane_owner(session: str, pane: str) -> herdr.ProvedClaudeForeground:
    """Check ownership before the hook changes staff or launch records."""
    info = herdr.session_socket_request(session, "pane.process_info", pane_id=pane).get("process_info")
    owner = herdr.foreground_pane_owner(herdr.json_object(info, "Claude hook process info"))
    if isinstance(owner, herdr.UnprovedForegroundOwner):
        raise herdr_types.HerdrFailure(owner.reason)
    if not isinstance(owner, herdr.ProvedClaudeForeground):
        raise herdr_types.HerdrFailure("the pane has no proved Claude owner")
    return owner


def _caller_arguments(owner: herdr.ProvedClaudeForeground) -> list[str]:
    """Read the nearest Claude ancestor and require the proved PID."""
    pid = os.getppid()
    for _ in range(pane_record.ANCESTOR_LIMIT):
        if pid <= 1:
            break
        arguments = pane_record.process_arguments(pid)
        if not arguments:
            return []
        if herdr.is_claude_process(arguments):
            if pid == owner.pid:
                return arguments
            break
        pid = pane_record.process_parent(pid)
    raise herdr_types.HerdrFailure("the hook is outside the proved Claude launch")


def main() -> None:
    environment = dict(os.environ)
    pane_id = environment.get("HERDR_PANE_ID", "")
    if environment.get("HERDR_ENV") != "1" or not pane_id:
        return
    payload = cast(HookInput, json.loads(sys.stdin.read()))
    session = environment.get("HERDR_SESSION") or "default"
    try:
        owner = _check_pane_owner(session, pane_id)
        arguments = _caller_arguments(owner)
    except (herdr_types.HerdrFailure, OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
        print(f"session launch observation: {error}", file=sys.stderr)
        return
    if not arguments:
        print("cannot read Claude arguments; retained launch stays unchanged", file=sys.stderr)
        return
    fork_name = environment.get(pane_launch.CLAUDE_FORK_NAME_ENV)
    if fork_name and "--fork-session" in arguments and payload.get("source") != "clear":
        launch_arguments = pane_launch.parse_claude_arguments(arguments)
        if launch_arguments.value(pane_launch.NAME_FLAGS) == fork_name:
            return
    identity = staff_session_identity.handle(cast(Mapping[str, object], payload), environment)
    if identity is staff_session_identity.ClearIdentityResult.STAFFED:
        return
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    if "-p" in arguments or "--print" in arguments:
        return
    launch = pane_record.launch(pane_id, session_id, payload.get("cwd") or os.getcwd(), environment, arguments)
    if staff.runtime_id(owner.pid) == session_id:
        from scripts.message import sessions
        started = sessions.process_start(owner.pid)
        if started:
            launch["pid"] = owner.pid
            launch["proc_start"] = started
    pane_record.write(session, session_staff.record_membership(launch))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"session launch observation: {error}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)
