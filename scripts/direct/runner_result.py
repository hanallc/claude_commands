"""Read a runner result without changing the result file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Literal, cast


ResultWord = Literal["passed", "failed", "lost", "declined"]


@dataclass(frozen=True)
class UsableRunnerResult:
    word: ResultWord
    reason: str = ""
    seconds: str = ""
    holder: str = ""
    fetch: str = "failed"
    fix: str = ""
    build_target_directory: str = ""


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
        elif name in {"reason", "seconds", "holder", "fetch", "fix", "build_target_directory"}:
            fields[name] = value
    if len(words) != 1 or words[0] not in {"passed", "failed", "lost", "declined"}:
        return UnusableRunnerResult()
    word = cast(ResultWord, words[0])
    if word == "passed" and status != 0 or word == "failed" and status == 0:
        return UnusableRunnerResult()
    return UsableRunnerResult(word, fields.get("reason", ""), fields.get("seconds", ""),
                              fields.get("holder", ""), fields.get("fetch", "failed"),
                              fields.get("fix", ""), fields.get("build_target_directory", ""))


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
                  result.fix, result.build_target_directory):
        print(field)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
