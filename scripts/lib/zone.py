#!/usr/bin/env python3
"""Show or change the zone every time shown to the user is written in: the /zone command.

  zone.py                   the zone, its label and offset, and the time now; sends a queued Mac change
  zone.py set NAME          change it. NAME is an IANA zone (America/Curacao), the city of one
                            (Curacao, "New York"), or a label (AST) that only one zone uses
  zone.py drain [--pulled PATH]  send a change queued while the Mac was offline

`set` writes config/user_zone.conf live at once on natedev and on the Mac, as /tune does, and
tells every session on both machines with broadcast.py --all. The commit on main, the push and
the Mac pull follow through live_settings; none of them comes before the write. It runs on
natedev, whose ~/.claude main the Mac pulls from. When the Mac cannot be reached, its sessions'
message waits in the queue (<state>/zone/mac.json); a bare `zone.py` and each promotion's Mac
pull (merge_checkpoint.promote) send it. A name that is not a zone, or that names more than one,
is refused with exit 2 and the zones it could mean.

ZONE_SSH, ZONE_BROADCAST and ZONE_STATE replace ssh, broadcast.py and the state folder in tests.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import platform
import re
import shlex
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, available_timezones

from scripts.lib import conf as config, live_settings, mac_wake, main_publish, user_zone  # noqa: E402
from scripts.lib.main_publish import Refusal as Refusal
from scripts.lib.main_publish import checkout_of, first_line as _first_line, queue, read_queue

MAC = "mac"
SENDER = "zone"
SUBJECT = "zone("
STEM = "user_zone"
KEY = "zone"
SHOWN_CANDIDATES = 3


def _words(text: str) -> list[str]:
    """`text` as case-folded words with no accents: "Curaçao", "curacao" and "CURACAO" match."""
    plain = "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))
    return re.sub(r"[_\-\s]+", " ", plain).casefold().split()


def _offset(moment: datetime) -> str:
    """The moment's UTC offset as UTC-04:00."""
    text = moment.strftime("%z")
    return f"UTC{text[:3]}:{text[3:]}"


def _candidates(zones: list[str], now: datetime) -> str:
    """Up to SHOWN_CANDIDATES examples per UTC offset, so a reader sees which places differ."""
    groups: dict[str, list[str]] = {}
    for zone in zones:
        groups.setdefault(_offset(now.astimezone(ZoneInfo(zone))), []).append(zone)
    return "; ".join(f"{len(names)} at {offset} (such as {', '.join(names[:SHOWN_CANDIDATES])})"
                     for offset, names in sorted(groups.items()))


def resolve(name: str, now: datetime | None = None) -> str:
    """The one IANA zone `name` means. Raises Refusal when no zone or more than one matches."""
    wanted = name.strip()
    zones = available_timezones()
    exact = sorted(zone for zone in zones if zone.casefold() == wanted.casefold())
    if exact:
        return exact[0]
    at = now or datetime.now(timezone.utc)
    regional = sorted(zone for zone in zones if "/" in zone and not zone.startswith("Etc/"))
    by_city = [zone for zone in regional if _words(zone.rsplit("/", 1)[1]) == _words(wanted)]
    if len(by_city) == 1:
        return by_city[0]
    if by_city:
        raise Refusal(f"{wanted!r} is the city of {len(by_city)} zones: {_candidates(by_city, at)}; "
                      + "give the IANA zone")
    label = wanted.upper()
    by_label = [zone for zone in regional
                if any((moment.astimezone(ZoneInfo(zone)).tzname() or "") == label
                       for moment in (at, at + timedelta(days=182)))]
    if len(by_label) == 1:
        return by_label[0]
    if by_label:
        raise Refusal(f"{wanted!r} is the label of {len(by_label)} zones: {_candidates(by_label, at)}; "
                      + "name the place or the IANA zone")
    raise Refusal(f"no zone is named {wanted!r}; give the IANA zone of the place, such as America/Los_Angeles")


def describe(zone: str) -> str:
    """America/Curacao (AST, UTC-04:00); now 18:40 AST."""
    now = datetime.now(ZoneInfo(zone))
    label = now.tzname() or zone
    return f"{zone} ({label}, {_offset(now)}); now {now:%H:%M} {label}"


def stored(conf: Path) -> str:
    """The file's zone= value, or the default when the file or the line is missing."""
    return user_zone.stored_zone(str(conf)) or user_zone.DEFAULT_ZONE


def _ssh(remote: str) -> subprocess.CompletedProcess[str] | str:
    return main_publish.ssh(remote, ssh_program=os.environ.get("ZONE_SSH") or "ssh", host=MAC)


def pull_mac(checkout: Path) -> main_publish.MacPulled | main_publish.MacPullFailed:
    return main_publish.pull_mac(checkout, ssh_program=os.environ.get("ZONE_SSH") or "ssh")


def _counted(output: str) -> tuple[bool, str]:
    """broadcast.py's recipient lines as counts, with each line that did not arrive."""
    lines = output.splitlines()
    sent = sum(1 for line in lines if re.search(r" - sent\b", line))
    missed = [line for line in lines if "NOT sent" in line]
    return not missed, f"{sent} sent" + "".join(f"\n  {line}" for line in missed)


def broadcast(text: str, *, here: bool) -> tuple[bool, str]:
    """Send `text` to every session, on this machine only when `here`."""
    program = os.environ.get("ZONE_BROADCAST") or str(Path(__file__).resolve().parents[1] / "production/broadcast.py")
    done = subprocess.run([sys.executable, program, *(["--here"] if here else []), "--from", SENDER, "--all", text],
                          capture_output=True, text=True, check=False)
    delivered, summary = _counted(done.stdout)
    if done.returncode and delivered:
        return False, f"{summary}; broadcast.py: {_first_line(done)}"
    return delivered, summary


def broadcast_on_mac(text: str) -> tuple[bool, str]:
    done = _ssh('"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/broadcast.py" --here '
                + f"--from {SENDER} --all {shlex.quote(text)}")
    if isinstance(done, str):
        return False, f"not sent: {done}"
    delivered, summary = _counted(done.stdout)
    return delivered and done.returncode == 0, summary


def notice(zone: str, before: str) -> str:
    now = datetime.now(ZoneInfo(zone))
    label = now.tzname() or zone
    return (f"From /zone ({now:%H:%M} {label}): the user's time zone is now {zone} ({label}, {_offset(now)}); "
            + f"it was {before}. config/user_zone.conf says so on both machines. Write every time you give the user "
            + f"in {label}, with its label. Read the clock with TZ=$(python3 ~/.claude/scripts/lib/user_zone.py) "
            + "date '+%H:%M %Z', never with bare date, because each machine's clock keeps its own zone. "
            + "Scripts that read user_zone.py change by themselves. Change any note of yours that names the old zone.")


def drain(checkout: Path | None, *, pulled: PurePosixPath | None) -> list[str]:
    """Pull each pending checkout, then send its queued zone notice."""
    change = read_queue()
    if isinstance(change, main_publish.NoPendingMacPull):
        return []
    for pull in change.pulls:
        if pull.mac != pulled:
            source = pull.source if isinstance(pull.source, Path) else checkout
            if source is None:
                return [f"mac: the {change.label} change waits since {change.since}; run /zone on natedev"]
            outcome = main_publish.pull_mac(source, mac=pull.mac, ssh_program=os.environ.get("ZONE_SSH") or "ssh")
            if isinstance(outcome, main_publish.MacPullFailed):
                return [f"mac: the {change.label} change still waits since {change.since}: {outcome.reason}"]
        main_publish.clear_pull_only(pull.mac)
    delivered, summary = broadcast_on_mac(change.text) if change.text else (True, "no message")
    if not delivered:
        return [f"mac: pulled the {change.label} change; its message still waits: {summary}"]
    main_publish.clear_pull_only(notice_delivered=True)
    return [f"mac: took the {change.label} change queued at {change.since}; message {summary}"]


def set_zone(name: str) -> int:
    """Write the zone live on both machines and tell every session; the commit, push and Mac pull follow."""
    if platform.system() == "Darwin":
        raise Refusal("run /zone on natedev: the Mac pulls ~/.claude main from there")
    zone = resolve(name)
    conf = Path(user_zone.CONF)
    if conf.resolve() != config.path(STEM).resolve():
        raise Refusal(f"{conf} is not the live settings file {config.path(STEM)}")
    checkout = checkout_of(conf)
    before = stored(conf)
    print(f"zone: {describe(zone)}")
    delivered = True
    if before == zone:
        print("was: the same; nothing to change")
    else:
        print(f"was: {before}")
        label = datetime.now(ZoneInfo(zone)).tzname() or zone
        message = f"{SUBJECT}{zone}): set with /zone ({label})"
        _ = live_settings.stage(live_settings.WaitingChange(STEM, KEY, zone, message))
        print(f"live now: {KEY}={zone}")
        mac = live_settings.write_mac(STEM, KEY, zone)
        print(mac)
        text = notice(zone, before)
        if mac.startswith("mac: live now"):
            delivered, summary = broadcast(text, here=False)
        else:
            since = datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M %Z")
            queue(main_publish.PendingMacPull(label=zone, text=text, since=since, reason=mac))
            _ = mac_wake.start()
            delivered, summary = broadcast(text, here=True)
        print(f"broadcast: {summary}")
    try:
        lines = live_settings.publish(checkout, wait=True) or []
    except (Refusal, OSError) as error:
        print(f"zone: {' '.join(str(error).split())}", file=sys.stderr)
        print(f"zone: {zone} stays live; main does not have it; the notes timer tries again", file=sys.stderr)
        return 2
    for line in [*lines, *drain(checkout, pulled=None)]:
        print(line)
    return 0 if delivered else 1


def show() -> int:
    print(f"zone: {describe(user_zone.user_zone_name())}")
    checkout = None if platform.system() == "Darwin" else checkout_of(Path(user_zone.CONF))
    for line in drain(checkout, pulled=None):
        print(line)
    return 0


def main(arguments: list[str]) -> int:
    try:
        match arguments:
            case []:
                return show()
            case ["set", name]:
                return set_zone(name)
            case ["drain"] | ["drain", "--pulled", _]:
                checkout = None if platform.system() == "Darwin" else checkout_of(Path(user_zone.CONF))
                pulled = PurePosixPath(arguments[2]) if len(arguments) == 3 else None
                for line in drain(checkout, pulled=pulled):
                    print(line)
                return 0
            case _:
                print("usage: zone.py [set NAME | drain [--pulled PATH]]", file=sys.stderr)
                return 2
    except (Refusal, ValueError) as error:
        print(f"zone: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
