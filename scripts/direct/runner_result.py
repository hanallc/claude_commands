"""Read a runner result without changing the result file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Literal, cast, get_args


ResultWord = Literal["passed", "failed", "lost", "declined", "prelaunch_failed", "hana_command_admission_refused"]


@dataclass(frozen=True)
class UsableRunnerResult:
    word: ResultWord
    reason: str = ""
    seconds: str = ""
    holder: str = ""
    fetch: str = "failed"
    fix: str = ""
    build_target_directory: str = ""
    kill_diagnostic_seen: bool = False


@dataclass(frozen=True)
class UnusableRunnerResult:
    """The file or runner status cannot supply a usable result."""


def read_result(path: Path, status: int, selected_is_mac: bool) -> UsableRunnerResult | UnusableRunnerResult:
    """Keep the runner protocol's field and exit status rules."""
    try:
        if not path.is_file() or not os.access(path, os.R_OK) or path.stat().st_size == 0:
            return UnusableRunnerResult()
        with path.open(encoding="utf-8", errors="surrogateescape", newline="") as source:
            text = source.read()
    except OSError:
        return UnusableRunnerResult()

    fields: dict[str, str] = {}
    words: list[str] = []
    # Bash removes NUL bytes and trailing newlines during command substitution.
    for line in text.replace("\0", "").rstrip("\n").split("\n"):
        name, separator, value = line.partition("=")
        if not separator:
            continue
        if name == "mac" or name == "outcome" and not selected_is_mac:
            words.append(value)
        elif name in {"reason", "seconds", "holder", "fetch", "fix", "build_target_directory", "kill_diagnostic"}:
            fields[name] = value
    if len(words) != 1 or words[0] not in get_args(ResultWord):
        return UnusableRunnerResult()
    word = cast(ResultWord, words[0])
    if word == "passed" and status != 0 or word == "failed" and status == 0:
        return UnusableRunnerResult()
    if word == "prelaunch_failed" and (selected_is_mac or status in (0, 75)):
        return UnusableRunnerResult()
    if word == "hana_command_admission_refused" and (selected_is_mac or status != 75 or not fields.get("reason")):
        return UnusableRunnerResult()
    return UsableRunnerResult(word, fields.get("reason", ""), fields.get("seconds", ""),
                              fields.get("holder", ""), fields.get("fetch", "failed"),
                              fields.get("fix", ""), fields.get("build_target_directory", ""),
                              kill_diagnostic_seen=(selected_is_mac and word == "failed"
                                                    and fields.get("kill_diagnostic") == "1"))


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 3 or arguments[2] not in {"0", "1"}:
        return 2
    try:
        status = int(arguments[1])
    except ValueError:
        return 2
    result = read_result(Path(arguments[0]), status, arguments[2] == "1")
    if isinstance(result, UnusableRunnerResult):
        return 1
    for field in (result.word, result.reason, result.seconds, result.holder, result.fetch,
                  result.fix, result.build_target_directory, str(int(result.kill_diagnostic_seen))):
        print(field)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
