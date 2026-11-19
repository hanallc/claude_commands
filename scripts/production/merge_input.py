"""Parse checkpoint input without reading production state."""
from __future__ import annotations

import argparse
import re
import shlex
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple, Never, cast, override

from scripts.production import review_regime, unit_names


COMMIT_HASH = re.compile(r"[0-9a-f]{7,40}")


class MergeInputRefused(ValueError):
    """The checkpoint input cannot be used."""


class CodeCheckpoint(NamedTuple):
    pass


class ShrinkCommit(NamedTuple):
    pass


class AsBuiltCommit(NamedTuple):
    """A unit's closing documentation commit."""


class ReviewTrial(NamedTuple):
    ux_findings: str
    code_findings: str
    review_minutes: str
    ux_check_minutes: str
    ux_repair_minutes: str

    def record_options(self) -> tuple[tuple[str, str], ...]:
        return (("--ux-findings", self.ux_findings), ("--code-findings", self.code_findings),
                ("--review-minutes", self.review_minutes), ("--ux-check-minutes", self.ux_check_minutes),
                ("--ux-repair-minutes", self.ux_repair_minutes))


class NoReviewTrial(NamedTuple):
    pass


class UnitCheckpointInput(NamedTuple):
    unit: str
    phase: str
    commit: str
    kind: CodeCheckpoint | ShrinkCommit | AsBuiltCommit
    review_trial: ReviewTrial | NoReviewTrial
    delivers: str
    scratch: str
    trailer: tuple[str, ...]
    cancel_prior: bool
    started: str
    regime: str
    holds: int
    merge_defects: int
    excluded: str
    bump: bool


class BatchArgumentParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> Never:
        raise MergeInputRefused(message)


def checkpoint_parser(*, description: str | None = None, batch_item: bool = False) -> argparse.ArgumentParser:
    """Define each flag for both checkpoint commands and batch lines."""
    parser = (BatchArgumentParser(add_help=False, allow_abbrev=False) if batch_item
              else argparse.ArgumentParser(description=description))
    if not batch_item:
        _ = parser.add_argument("--production", required=True)
        _ = parser.add_argument("--batch", type=Path)
    _ = parser.add_argument("unit", nargs="?")
    _ = parser.add_argument("phase", nargs="?")
    _ = parser.add_argument("hash", nargs="?")
    _ = parser.add_argument("--from-child")
    _ = parser.add_argument("--checkpoint")
    _ = parser.add_argument("--root-self", action="store_true")
    _ = parser.add_argument("--shrink", action="store_true")
    _ = parser.add_argument("--as-built", action="store_true")
    _ = parser.add_argument("--trailer", action="append", default=[])
    _ = parser.add_argument("--review-trial")
    _ = parser.add_argument("--delivers")
    _ = parser.add_argument("--cancel-prior", action="store_true")
    _ = parser.add_argument("--bump", action="store_true",
                            help="only a user's bump: push a code checkpoint before its background gate")
    _ = parser.add_argument("--started")
    _ = parser.add_argument("--regime", choices=("after", "trial"), default="after")
    _ = parser.add_argument("--holds", type=int, default=0)
    _ = parser.add_argument("--merge-defects", type=int, default=0)
    _ = parser.add_argument("--excluded")
    _ = parser.add_argument("--scratch")
    return parser


def require_unit_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if cast(bool, args.root_self) or cast(str | None, args.checkpoint):
        parser.error("--root-self and --checkpoint require --from-child")
    if not all((cast(str | None, args.unit), cast(str | None, args.phase), cast(str | None, args.hash))):
        parser.error("unit, phase and hash are required for a unit checkpoint")


def user_bump(args: argparse.Namespace) -> bool:
    bump = cast(bool, getattr(args, "bump", False))
    if bump and any((cast(bool, args.shrink), cast(bool, args.as_built),
                     cast(str | None, getattr(args, "from_child", None)))):
        raise MergeInputRefused("--bump applies only to a code checkpoint requested by the user")
    return bump


def review_numbers(line: str) -> ReviewTrial:
    pattern = (r"review trial: ux (\d+) findings, code (\d+) findings, review-worker minutes ([\d.]+), "
               r"ux check minutes ([\d.]+), ux repair minutes ([\d.]+)")
    match = re.fullmatch(pattern, line.strip())
    if match is None:
        raise MergeInputRefused("--review-trial must read: review trial: ux <N> findings, code <N> findings, "
                                + "review-worker minutes <M>, ux check minutes <U>, ux repair minutes <R>")
    trial = ReviewTrial(match[1], match[2], match[3], match[4], match[5])
    parser = BatchArgumentParser(add_help=False, allow_abbrev=False)
    review_regime.add_measurement_arguments(parser)
    _ = parser.parse_args([f"{option}={value}" for option, value in trial.record_options()])
    return trial


def unit_checkpoint(args: argparse.Namespace) -> UnitCheckpointInput:
    """Check the input fields of a unit checkpoint."""
    bump = user_bump(args)
    unit = unit_names.name(cast(str, args.unit))
    shrink, as_built = cast(bool, args.shrink), cast(bool, args.as_built)
    if shrink and as_built:
        raise MergeInputRefused("--shrink and --as-built are separate checkpoints")
    kind = ShrinkCommit() if shrink else AsBuiltCommit() if as_built else CodeCheckpoint()
    trial = cast(str | None, args.review_trial)
    if isinstance(kind, CodeCheckpoint) and not trial:
        raise MergeInputRefused("code checkpoint requires --review-trial")
    review_trial = review_numbers(trial) if trial else NoReviewTrial()
    delivers = cast(str | None, args.delivers)
    if isinstance(kind, CodeCheckpoint) and (not delivers or not delivers.strip()):
        raise MergeInputRefused("code checkpoint requires --delivers")
    if delivers and ("\n" in delivers or "\r" in delivers):
        raise MergeInputRefused("--delivers must be one line")
    if not isinstance(kind, CodeCheckpoint) and delivers:
        raise MergeInputRefused("--delivers applies only to a code checkpoint")
    started = cast(str, args.started)
    if isinstance(kind, CodeCheckpoint):
        _ = review_regime.review_time("started", started)
    elif not started:
        raise MergeInputRefused("the following arguments are required: --started")
    return UnitCheckpointInput(unit, cast(str, args.phase), cast(str, args.hash), kind,
                               review_trial, delivers or "", cast(str | None, args.scratch) or "",
                               tuple(cast(list[str], args.trailer)), cast(bool, args.cancel_prior),
                               started, cast(str, args.regime), cast(int, args.holds),
                               cast(int, args.merge_defects), cast(str | None, args.excluded) or "", bump)


def batch_lines(path: Path) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            yield number, line


def batch_arguments(parser: argparse.ArgumentParser, line: str) -> argparse.Namespace:
    return parser.parse_args(shlex.split(line))


def read_batch(path: Path) -> tuple[UnitCheckpointInput, ...]:
    """Check each batch line before the caller uses any batch item."""
    parser = checkpoint_parser(batch_item=True)
    items: list[UnitCheckpointInput] = []
    for number, line in batch_lines(path):
        try:
            if "\0" in line:
                raise MergeInputRefused("batch arguments contain a NUL byte")
            args = batch_arguments(parser, line)
            if cast(str | None, args.started) is None:
                parser.error("the following arguments are required: --started")
            if (cast(bool, args.bump) or cast(str | None, args.from_child) is not None
                    or cast(str | None, args.checkpoint) is not None):
                raise MergeInputRefused("--bump, --from-child and --checkpoint are not batch items")
            require_unit_arguments(parser, args)
            commit = cast(str, args.hash)
            if not COMMIT_HASH.fullmatch(commit):
                raise MergeInputRefused(f"not a commit hash: {commit!r}")
            items.append(unit_checkpoint(args))
        except ValueError as error:
            raise MergeInputRefused(f"{path}:{number}: {error}") from error
    if not items:
        raise MergeInputRefused("the batch file has no items")
    return tuple(items)
