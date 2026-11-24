"""Send a simplification report or feedback on a sent report."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
from subprocess import DEVNULL, PIPE, run
import sys
from typing import Literal, TypedDict, cast
from uuid import uuid4

from scripts.lib import conf
from scripts.message import send_result


RECIPIENT_KEY = "simplify_report_recipient"
SEND_SCRIPT = Path(__file__).resolve().parents[1] / "message" / "send.py"


class ReportArguments(TypedDict):
    mode: Literal["report"]
    sender: str
    before: Path
    after: Path
    failure: str


class FeedbackArguments(TypedDict):
    mode: Literal["feedback"]
    sender: str
    report: str
    item: Path
    words: Path


def parse_arguments(argv: list[str]) -> ReportArguments | FeedbackArguments:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--from", dest="sender", required=True)
    commands = parser.add_subparsers(dest="mode", required=True)
    report = commands.add_parser("report")
    _ = report.add_argument("--before", type=Path, required=True)
    _ = report.add_argument("--after", type=Path, required=True)
    _ = report.add_argument("--failure", required=True)
    feedback = commands.add_parser("feedback")
    _ = feedback.add_argument("--report", required=True)
    _ = feedback.add_argument("--item", type=Path, required=True)
    _ = feedback.add_argument("--words", type=Path, required=True)
    parsed = cast(object, vars(parser.parse_args(argv)))
    return cast(ReportArguments | FeedbackArguments, parsed)


def configured_recipient() -> str:
    settings = conf.read(conf.path("adhoc_review"))
    recipient = settings.origin(RECIPIENT_KEY, str)
    if not isinstance(recipient, conf.UsableLiveValue) or not recipient.value:
        raise ValueError(f"{settings.source} requires {RECIPIENT_KEY}")
    return recipient.value


def read_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"{path} is empty")
    return text


def send_message(recipient: str, sender: str, message: str) -> None:
    result = run([sys.executable, str(SEND_SCRIPT), f"--to={recipient}", f"--from={sender}"],
                 input=message, encoding="utf-8", stdout=PIPE, stderr=DEVNULL, check=False)
    outcome = send_result.send_outcome(result.returncode, result.stdout)
    if isinstance(outcome, send_result.UnconfirmedSendOutcome):
        raise ValueError(f"send.py did not confirm delivery or storage (exit {result.returncode})")
    if outcome.header.outcome not in ("sent", "kept", "queued"):
        raise ValueError(f"send.py returned {outcome.header.outcome.upper()}")


def send_report(arguments: ReportArguments, recipient: str) -> None:
    failure = arguments["failure"].strip()
    if not failure or len(failure.splitlines()) != 1:
        raise ValueError("--failure requires one line")
    before = read_text(arguments["before"])
    item = read_text(arguments["after"])
    report_id = uuid4().hex
    message = (f"Ad hoc review simplify report {report_id}\n\n"
               f"Why the first item failed\n{failure}\n\n"
               f"Before\n{before}\n\nAfter\n{item}")
    send_message(recipient, arguments["sender"], message)
    print(report_id)


def send_feedback(arguments: FeedbackArguments, recipient: str) -> None:
    report_id = arguments["report"]
    if not re.fullmatch(r"[0-9a-fA-F]{32}", report_id):
        raise ValueError("--report requires 32 hexadecimal characters")
    item = read_text(arguments["item"])
    words = read_text(arguments["words"])
    message = (f"Ad hoc review simplify feedback for report {report_id}\n\n"
               f"Simplified item\n{item}\n\nUser feedback\n{words}")
    send_message(recipient, arguments["sender"], message)


def main(argv: list[str]) -> int:
    arguments = parse_arguments(argv)
    try:
        recipient = configured_recipient()
        if arguments["mode"] == "report":
            send_report(arguments, recipient)
        else:
            send_feedback(arguments, recipient)
    except (ValueError, OSError) as error:
        print(f"simplify_report.py: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
