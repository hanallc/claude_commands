"""Read the global action count for the status line."""
from __future__ import annotations

import json
import os
import subprocess
from http.client import HTTPConnection
from ipaddress import IPv4Address
from pathlib import Path
from typing import cast

from scripts.lib import conf
from scripts.review_links import for_you


def global_action_count(config: Path) -> str:
    """Return a complete action count or an explicit unavailable result."""
    try:
        count = for_you.read(config, include_running_revision=False).open_count
        return str(count.value) if isinstance(count, for_you.KnownOpenCount) else "unavailable"
    except Exception:
        # Keep the status line available when a count source fails.
        return "unavailable"


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
        if isinstance(payload, dict):
            fields = cast(dict[str, object], payload)
            count = fields.get("count")
            if fields.get("available") is True and type(count) is int and count >= 0:
                return str(count)
    except Exception:
        return "unavailable"
    return "unavailable"
