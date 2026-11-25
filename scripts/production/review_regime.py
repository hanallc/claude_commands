#!/usr/bin/env python3
"""Record merged phases and compare their review results.

Usage:
  review_regime.py add --unit <unit> --plan <plan path> --phase <N> --checkpoint <commit>
                       --regime before|trial|after
                       --started <ISO> --merged <ISO> --holds <K> --merge-defects <D>
                       [--ux-findings <N>] [--code-findings <N>] [--review-minutes <M>]
                       [--ux-check-minutes <U>] [--ux-repair-minutes <R>]
                       [--excluded <reason>] [--note <text>]
  review_regime.py report [--since <ISO date>]

The review trial began on 2026-10-01. It added a UX reviewer to each phase
with visible changes and a code-quality reviewer to each phase.
The trial ended on 2026-10-04. The showrunner removed the extra code reviewer.
Phases that began after the trial use the `after` regime.

The showrunner adds one row per merged phase. The report compares holds,
defects, elapsed time, and review time across the three regimes.
From 2026-10-07, the code finding count includes all code findings from the
phase review. Earlier counts include only the code-quality reviewer's findings.

`holds` counts the checkpoints that the showrunner held.
`merge-defects` counts defects that merge design checks found in the phase's
work. It includes moved defects and excludes defects older than the phase.

The report skips rows that have an `excluded` reason. Typical reasons include
no visible change, no merge design check, and no defect count in the log.
`--ux-check-minutes 0` records `no visible change` unless a reason is supplied.
"""

import argparse
import json
import statistics
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

LEDGER = Path.home() / ".claude/data/review_regime.jsonl"
REGIMES = ("before", "trial", "after")
# `add` records this reason for a phase whose screenshot check took no minutes.
NO_VISIBLE_CHANGE = "no visible change"


@dataclass(frozen=True)
class Row:
    unit: str
    # The unit's plan as its production doc names it; None on a row written before 2026-10-12.
    plan: str | None
    phase: str
    regime: str
    started: str
    merged: str
    holds: int
    merge_defects: int
    ux_findings: int | None
    code_findings: int | None
    review_minutes: float | None
    ux_check_minutes: float | None
    ux_repair_minutes: float | None
    note: str | None
    excluded: str | None
    # Older rows can have no checkpoint.
    checkpoint: str | None

    def hours(self) -> float:
        return (datetime.fromisoformat(self.merged) - datetime.fromisoformat(self.started)).total_seconds() / 3600


def optional_int(record: dict[str, object], key: str) -> int | None:
    value = record.get(key)
    return value if isinstance(value, int) else None


def optional_float(record: dict[str, object], key: str) -> float | None:
    value = record.get(key)
    return float(value) if isinstance(value, int | float) else None


def read_rows() -> list[Row]:
    if not LEDGER.exists():
        return []
    rows: list[Row] = []
    for line in LEDGER.read_text().splitlines():
        if not line.strip():
            continue
        record = cast(dict[str, object], json.loads(line))
        note = record.get("note")
        excluded = record.get("excluded")
        plan = record.get("plan")
        checkpoint = record.get("checkpoint")
        rows.append(
            Row(
                unit=str(record["unit"]),
                plan=plan if isinstance(plan, str) else None,
                phase=str(record["phase"]),
                regime=str(record["regime"]),
                started=str(record["started"]),
                merged=str(record["merged"]),
                holds=int(cast(int, record["holds"])),
                merge_defects=int(cast(int, record["merge_defects"])),
                ux_findings=optional_int(record, "ux_findings"),
                code_findings=optional_int(record, "code_findings"),
                review_minutes=optional_float(record, "review_minutes"),
                ux_check_minutes=optional_float(record, "ux_check_minutes"),
                ux_repair_minutes=optional_float(record, "ux_repair_minutes"),
                note=note if isinstance(note, str) else None,
                excluded=excluded if isinstance(excluded, str) else None,
                checkpoint=checkpoint if isinstance(checkpoint, str) else None,
            )
        )
    return rows


def add(arguments: argparse.Namespace) -> None:
    started = cast(str, arguments.started)
    merged = cast(str, arguments.merged)
    for label, moment in (("started", started), ("merged", merged)):
        parsed = datetime.fromisoformat(moment)
        if parsed.tzinfo is None:
            raise SystemExit(f"review_regime: --{label} {moment!r} needs a UTC offset")
    checkpoint_ref = cast(str, arguments.checkpoint)
    resolved = subprocess.run(["git", "rev-parse", "--verify", "--end-of-options", f"{checkpoint_ref}^{{commit}}"],
                              capture_output=True, text=True, check=False)
    if resolved.returncode:
        raise SystemExit(f"review_regime: cannot resolve --checkpoint {checkpoint_ref!r}: {resolved.stderr.strip()}")
    checkpoint = resolved.stdout.strip()
    check_minutes = cast(float | None, arguments.ux_check_minutes)
    excluded = cast(str | None, arguments.excluded)
    if excluded is None and check_minutes == 0:
        excluded = NO_VISIBLE_CHANGE
    row = Row(
        unit=cast(str, arguments.unit),
        plan=cast(str, arguments.plan),
        phase=cast(str, arguments.phase),
        regime=cast(str, arguments.regime),
        started=started,
        merged=merged,
        holds=cast(int, arguments.holds),
        merge_defects=cast(int, arguments.merge_defects),
        ux_findings=cast(int | None, arguments.ux_findings),
        code_findings=cast(int | None, arguments.code_findings),
        review_minutes=cast(float | None, arguments.review_minutes),
        ux_check_minutes=check_minutes,
        ux_repair_minutes=cast(float | None, arguments.ux_repair_minutes),
        note=cast(str | None, arguments.note),
        excluded=excluded,
        checkpoint=checkpoint,
    )
    if row.hours() < 0:
        raise SystemExit("review_regime: --merged is before --started")
    identity = (row.unit, row.plan, row.phase, checkpoint)
    for existing in read_rows():
        if (existing.unit, existing.plan, existing.phase, existing.checkpoint) == identity:
            print(f"kept {row.unit} phase {row.phase} of {row.plan}: "
                  + f"a row exists from the merge at {existing.merged}; nothing added")
            return
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    record = cast(dict[str, object], asdict(row))
    with LEDGER.open("a") as ledger:
        _ = ledger.write(json.dumps(record) + "\n")
    print(f"added {row.unit} phase {row.phase} ({row.regime}): {row.holds} holds, {row.merge_defects} merge defects, {row.hours():.1f} h" + (f"; excluded: {row.excluded}" if row.excluded else ""))


def mean_of(values: list[float]) -> str:
    return f"{statistics.mean(values):.1f}" if values else "-"


def median_of(values: list[float]) -> str:
    return f"{statistics.median(values):.1f}" if values else "-"


def report(since: str | None) -> None:
    rows = [row for row in read_rows() if (since is None or row.merged >= since) and row.excluded is None]
    lines = [f"| | {' | '.join(REGIMES)} |", "| --- " * (len(REGIMES) + 1) + "|"]
    groups = {regime: [row for row in rows if row.regime == regime] for regime in REGIMES}

    def line(name: str, value: Callable[[list[Row]], str]) -> None:
        lines.append(f"| {name} | {' | '.join(value(groups[regime]) for regime in REGIMES)} |")

    line("phases merged", lambda group: str(len(group)))
    line("holds per phase (mean)", lambda group: mean_of([float(row.holds) for row in group]))
    line("merge design-check defects per phase (mean)", lambda group: mean_of([float(row.merge_defects) for row in group]))
    line("hours, start to merge (median)", lambda group: median_of([row.hours() for row in group]))
    line(
        "review-worker minutes per phase (mean)",
        lambda group: mean_of([row.review_minutes for row in group if row.review_minutes is not None]),
    )
    line(
        "screenshot check minutes per phase (mean)",
        lambda group: mean_of([row.ux_check_minutes for row in group if row.ux_check_minutes is not None]),
    )
    line(
        "repair minutes after its findings per phase (mean)",
        lambda group: mean_of([row.ux_repair_minutes for row in group if row.ux_repair_minutes is not None]),
    )
    line("UX reviewer findings per phase (mean)", lambda group: mean_of([float(row.ux_findings) for row in group if row.ux_findings is not None]))
    line("code findings per phase (mean)", lambda group: mean_of([float(row.code_findings) for row in group if row.code_findings is not None]))
    print("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    adding = commands.add_parser("add")
    _ = adding.add_argument("--unit", required=True)
    _ = adding.add_argument("--plan", required=True, help="the unit's plan, as its production doc names it")
    _ = adding.add_argument("--phase", required=True)
    _ = adding.add_argument("--checkpoint", required=True, help="the checkpoint commit in the current repository")
    _ = adding.add_argument("--regime", choices=REGIMES, required=True)
    _ = adding.add_argument("--started", required=True)
    _ = adding.add_argument("--merged", required=True)
    _ = adding.add_argument("--holds", type=int, required=True)
    _ = adding.add_argument("--merge-defects", type=int, required=True)
    _ = adding.add_argument("--ux-findings", type=int)
    _ = adding.add_argument("--code-findings", type=int)
    _ = adding.add_argument("--review-minutes", type=float)
    _ = adding.add_argument("--ux-check-minutes", type=float)
    _ = adding.add_argument("--ux-repair-minutes", type=float)
    _ = adding.add_argument("--excluded", help=f"why the comparison cannot use this phase; --ux-check-minutes 0 records {NO_VISIBLE_CHANGE!r}")
    _ = adding.add_argument("--note")
    reporting = commands.add_parser("report")
    _ = reporting.add_argument("--since", help="only phases merged on or after this ISO date; design checks began 2026-09-28")
    arguments = parser.parse_args()
    command = cast(str, arguments.command)
    if command == "add":
        add(arguments)
    else:
        report(cast(str | None, arguments.since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
