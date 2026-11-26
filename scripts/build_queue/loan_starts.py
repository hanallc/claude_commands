"""Show which admissions used the memory loan before a stop."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import cast

from scripts.build_queue import memory_trace
from scripts.buildlog import memory, store
from scripts.lib.user_zone import user_zone


DEFAULT_LOOKBACK_HOURS = 24
KILL_LOOKBACK_DAYS = 7
TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"


def last_kill() -> tuple[datetime, str]:
    end = datetime.now(UTC)
    start = end - timedelta(days=KILL_LOOKBACK_DAYS)
    kernel = memory.kernel_oom_kills(start, end)
    early = memory.earlyoom_kills(start, end)
    if kernel is memory.JournalUnavailable.VALUE and early is memory.JournalUnavailable.VALUE:
        raise ValueError("OOM journals unavailable. Supply --stop.")
    candidates: list[tuple[datetime, str]] = []
    if kernel is not memory.JournalUnavailable.VALUE:
        candidates.extend((at, "kernel OOM kill") for at, _ in kernel)
    if early is not memory.JournalUnavailable.VALUE:
        candidates.extend((kill.at, "earlyoom kill") for kill in early)
    latest = max(candidates, key=lambda candidate: candidate[0], default=None)
    if latest is None:
        raise ValueError(f"No kill in the available OOM journals in the last {KILL_LOOKBACK_DAYS} days. Supply --stop.")
    return latest


def ended_peak(event: dict[str, object]) -> int | None:
    peak = event.get("peak")
    if isinstance(peak, dict):
        fields = cast(dict[str, object], peak)
        size = fields.get("bytes")
        if fields.get("kind") == "PeakSampled" and type(size) is int and size >= 0:
            return size
    return None


def gib(value: object) -> str:
    return f"{value / memory.GIB:.2f}" if type(value) is int else "not recorded"


def starts_table(directory: Path, stop: datetime, zone: tzinfo, *, window_minutes: int | None = None) -> str:
    """Show the steps still running at the stop or all starts in the requested window."""
    window = timedelta(hours=DEFAULT_LOOKBACK_HOURS) if window_minutes is None else timedelta(minutes=window_minutes)
    start = stop - window
    lower, upper = start.timestamp(), stop.timestamp()

    def reached(event: dict[str, object]) -> bool:
        at = event.get("at")
        return not isinstance(at, (int, float)) or at >= lower

    events: list[tuple[float, str, dict[str, object]]] = []
    endings: dict[str, tuple[float, int | None]] = {}
    for event in store.locked_rows_in_order(directory / "queue_times.jsonl", reached,
                                           lambda line: b'"admitted"' in line or b'"ended"' in line):
        value = event.get("reservation_id")
        identifier = value if isinstance(value, str) else ""
        kind = event.get("kind")
        if kind == "admitted":
            at = event.get("admitted_at")
            if isinstance(at, (int, float)) and lower <= at < upper:
                events.append((at, identifier, event))
        elif kind == "ended" and identifier:
            at = event.get("ended_at")
            previous = endings.get(identifier)
            if isinstance(at, (int, float)) and (previous is None or at < previous[0]):
                endings[identifier] = (at, ended_peak(event))
    if window_minutes is None:
        events = [event for event in events if (end := endings.get(event[1])) is None or end[0] > upper]
    events.sort(key=lambda entry: entry[0])
    identifiers = {identifier for _, identifier, _ in events if identifier}
    peaks: dict[str, int] = {}
    if identifiers and isinstance(policy := memory_trace.read_policy(), memory_trace.MemoryTracePolicy):
        peaks = memory_trace.TraceStore(directory / "memory_traces", policy).peaks_for(identifiers)

    def show_time(at: float) -> str:
        return datetime.fromtimestamp(at, zone).strftime(TIME_FORMAT)

    header = ("Time", "Unit or session", "Repo", "Step", "Need (GiB)", "Room (GiB)", "Loan (GiB)", "Fit",
              "Peak (GiB)", "Peak source")
    rows: list[tuple[str, ...]] = [header if window_minutes is None else (*header, "Ended")]
    for at, identifier, event in events:
        owner = event.get("unit") or event.get("session_name") or event.get("session_id")
        ended_at, fallback = endings.get(identifier, (None, None))
        peak = peaks.get(identifier)
        source = "trace" if peak is not None else "ended" if fallback is not None else "none"
        names = (" ".join(str(value).split()) if value else "not recorded"
                 for value in (owner, event.get("repo"), event.get("step")))
        fit = event.get("fit") or "not recorded"
        row = (show_time(at), *names, *(gib(event.get(key)) for key in ("need", "room", "loan")),
               str(fit), gib(peak if peak is not None else fallback), source)
        if window_minutes is not None:
            row = (*row, show_time(ended_at) if ended_at is not None and ended_at <= upper else "running at stop")
        rows.append(row)
    widths = [max(map(len, column)) for column in zip(*rows, strict=True)]
    return "\n".join("  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
                     for row in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--stop", type=memory.parse_instant, help="ISO time with an offset")
    _ = parser.add_argument("--window-minutes", type=int, help="Show all starts in this window before the stop")
    args = parser.parse_args(argv)
    minutes = cast(int | None, args.window_minutes)
    if minutes is not None and minutes <= 0:
        parser.error("--window-minutes must be positive")
    try:
        given_stop = cast(datetime | None, args.stop)
        stop, source = (given_stop, "--stop") if given_stop is not None else last_kill()
        zone = user_zone()
        table = starts_table(store.root() / "admission", stop, zone, window_minutes=minutes)
        print(f"Stop {stop.astimezone(zone).strftime(TIME_FORMAT)} ({source})")
        print(table)
    except (OSError, OverflowError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
