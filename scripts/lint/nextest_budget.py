#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Report a nextest run's test threads and the machine load when it starts."""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from typing import override


@dataclass(frozen=True)
class SpecifiedTestWidth:
    """A known nextest width specification, including symbolic values."""

    value: str

    @override
    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class UnknownTestWidth:
    """The run has no readable width specification or CPU count."""

    @override
    def __str__(self) -> str:
        return "unknown"


TestWidth = SpecifiedTestWidth | UnknownTestWidth


def thread_line(granted: TestWidth, requested: TestWidth) -> str:
    """Report the granted width and the machine load as the tests start."""
    try:
        load = f"{os.getloadavg()[0]:.2f}"
    except OSError:
        load = "unknown"
    cpus = os.cpu_count()
    host = socket.gethostname().split(".", 1)[0]
    return (f"nextest: {granted} of {requested} test threads; load {load} "
            + f"on {cpus if cpus is not None else 'unknown'} CPUs ({host})")


def requested_threads(argv: list[str]) -> TestWidth:
    """Read each nextest width option, then the environment or CPU default."""
    arguments = iter(argv)
    for word in arguments:
        if word == "--":
            break
        if word in ("--test-threads", "--jobs", "-j"):
            value = next(arguments, None)
            return SpecifiedTestWidth(value) if value is not None else UnknownTestWidth()
        if word.startswith(("--test-threads=", "--jobs=")):
            return SpecifiedTestWidth(word.split("=", 1)[1])
        if word.startswith("-j") and len(word) > 2:
            return SpecifiedTestWidth(word[2:].removeprefix("="))
    value = os.environ.get("NEXTEST_TEST_THREADS")
    if value is not None:
        return SpecifiedTestWidth(value)
    cpus = os.cpu_count()
    return SpecifiedTestWidth(str(cpus)) if cpus else UnknownTestWidth()


def main() -> None:
    _, action, *arguments = sys.argv
    if action != "report":
        raise SystemExit(f"unknown action {action}")
    requested = requested_threads(arguments)
    print(thread_line(requested, requested))


if __name__ == "__main__":
    main()
