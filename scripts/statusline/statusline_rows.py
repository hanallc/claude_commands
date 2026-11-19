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
from scripts.review_links import for_you
from scripts.statusline import assignment


def global_action_count() -> str:
    """Return a complete action count or an explicit unavailable result."""
    try:
        count = for_you.read().open_count
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


def main() -> None:
    """Pass the action count and shell arguments to the status line renderer."""
    read_count = console_action_count if sys.argv[1] == "--for-you-console" else global_action_count
    try:
        count = read_count()
    except Exception:
        traceback.print_exc()
        count = "unavailable"
    sys.argv[1:2] = ["--arg", "for_you", count]
    assignment.main()


if __name__ == "__main__":
    main()
