"""Identify suite runner entry points without loading their code."""
from collections.abc import Sequence
from pathlib import Path


SUITE_MODULE = "scripts.production.gate"
SUITE_RUNNERS = frozenset({"gate.py"})


def starts_suite_runner(target: Sequence[str]) -> bool:
    return tuple(target[:2]) == ("-m", SUITE_MODULE) or bool(target) and Path(target[0]).name in SUITE_RUNNERS
