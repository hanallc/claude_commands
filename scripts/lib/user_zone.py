#!/usr/bin/env python3
"""The zone every time shown to the user is written in.

One setting, `config/user_zone.conf`, so a change of zone is one line. `USER_ZONE` in the
environment wins over the file: a test that compares against saved output pins its zone with it.

Usage: user_zone.py            print the zone's IANA name, for `TZ="$(…)" date '+%H:%M %Z'`
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import sys
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts.lib import conf_reads

DEFAULT_ZONE = "America/New_York"


def settings_path() -> str:
    """The zone file at each call: `USER_ZONE_CONF`, else `user_zone.conf` in the config folder that `CONF_DIR` moves."""
    if override := os.environ.get("USER_ZONE_CONF"):
        return override
    from scripts.lib import conf

    return str(conf.path("user_zone"))


def stored_zone(path: str) -> str:
    """The file's `zone=` value. Empty when the file or the line is missing."""
    try:
        lines = conf_reads.read_text(path).splitlines()
    except FileNotFoundError:
        return ""
    return next((line.split("=", 1)[1].strip() for line in lines if line.startswith("zone=")), "")


def user_zone_name() -> str:
    """The user's zone as an IANA name. Raises ValueError naming where an unknown one came from."""
    pinned = os.environ.get("USER_ZONE", "")
    path = "USER_ZONE" if pinned else settings_path()
    name = pinned or stored_zone(path) or DEFAULT_ZONE
    try:
        _ = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"{path}: {name!r} is not a zone name; use one like {DEFAULT_ZONE}") from error
    return name


def user_zone() -> ZoneInfo:
    """The user's zone. Read at each call, so an edit to the setting needs no restart."""
    return ZoneInfo(user_zone_name())


def zone_label(moment: datetime | None = None) -> str:
    """The short label beside a time in the user's zone (EDT, EST), as of `moment` or now."""
    at = moment.astimezone(user_zone()) if moment is not None else datetime.now(user_zone())
    return at.tzname() or user_zone_name()


def shown_time(moment: datetime, zone: tzinfo | None = None, *, labeled: bool = False) -> str:
    """A time as the user reads it on a page: Nov 15, 2026, 08:45 (24-hour, the user's zone).

    A caller that already holds a zone passes it. `labeled` adds the zone label: Nov 15, 2026, 08:45 PST.
    """
    return moment.astimezone(zone or user_zone()).strftime("%b %d, %Y, %H:%M" + (" %Z" if labeled else ""))


def main() -> int:
    try:
        print(user_zone_name())
    except ValueError as error:
        print(f"user_zone: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
