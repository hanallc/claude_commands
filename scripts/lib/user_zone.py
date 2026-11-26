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
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from pathlib import Path


def _check_zone_name(name: str) -> str:
    try:
        _ = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        from scripts.lib import conf

        raise conf.Unusable(f"{name!r} is not a zone name") from error
    return name


def configured_zone_name(path: str | Path = "user_zone") -> str:
    """Read the zone from a config file at use."""
    from scripts.lib import conf

    return conf.read(path).value("zone", _check_zone_name)


def user_zone_name() -> str:
    """The user's zone as an IANA name. Raises ValueError naming where an unknown one came from."""
    pinned = os.environ.get("USER_ZONE", "")
    if pinned:
        try:
            return _check_zone_name(pinned)
        except ValueError as error:
            raise ValueError(f"USER_ZONE: {error}") from error
    return configured_zone_name()


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
