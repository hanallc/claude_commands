"""Read the action count and render the status line in one process."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import subprocess
import sys
import traceback
from http.client import HTTPConnection, HTTPException
from ipaddress import IPv4Address
from typing import cast

from scripts.lib import conf
from scripts.statusline import assignment, rebuild_row

try:
    from scripts.review_links import for_you
except Exception:
    for_you = None


def global_action_count(rebuild: rebuild_row.RebuildAnswer | None = None) -> str:
    """Return a complete action count or an explicit unavailable result."""
    if for_you is None:
        return "unavailable"
    try:
        count = for_you.read(rebuild_row.DEFAULT_CONFIG, rebuild=rebuild).open_count
    except (OSError, ValueError):
        return "unavailable"
    return str(count.value) if isinstance(count, for_you.KnownOpenCount) else "unavailable"


def console_action_count() -> str:
    """Read the global count from the console host."""
    try:
        host = os.environ.get("REVIEW_LINKS_HOST")
        if not host:
            peer = conf.read_or_repository("console").text("for_you.host")
            address = subprocess.check_output(
                ["tailscale", "ip", "-4", peer], text=True, stderr=subprocess.DEVNULL, timeout=2)
            host = str(IPv4Address(address.strip()))
        port = conf.read_or_repository("review_links").whole("server_port")
        connection = HTTPConnection(host, port, timeout=2)
        try:
            connection.request("GET", "/console/for-you.json?view=count")
            response = connection.getresponse()
            if response.status != 200:
                return "unavailable"
            payload = cast(object, json.loads(response.read()))
        finally:
            connection.close()
    except (HTTPException, OSError, ValueError, subprocess.SubprocessError):
        return "unavailable"
    if isinstance(payload, dict):
        fields = cast(dict[str, object], payload)
        count = fields.get("count")
        if fields.get("available") is True and type(count) is int and count >= 0:
            return str(count)
    return "unavailable"


def action_count(source: str, rebuild: rebuild_row.RebuildAnswer | None = None) -> str:
    """Read the action count from its selected source."""
    return console_action_count() if source == "--for-you-console" else global_action_count(rebuild)


def main() -> None:
    """Pass the action count and shell arguments to the status line renderer."""
    if len(sys.argv) == 2:
        print(action_count(sys.argv[1]))
        return
    rebuild = rebuild_row.read(rebuild_row.DEFAULT_CONFIG)
    try:
        count = action_count(sys.argv[1], rebuild)
    except Exception:
        traceback.print_exc()
        count = "unavailable"
    sys.argv[1:2] = ["--arg", "for_you", count]
    assignment.main(rebuild)


if __name__ == "__main__":
    main()
