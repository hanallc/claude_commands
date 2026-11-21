#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Run targeted tests with affected module selection in the shared suite runner."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Sequence
import sys
from typing import cast

from scripts.production import merge_gate
from scripts.lib.build_steps import gate_job_for_command
from scripts.lib.testing_folders import test_run_folder


@merge_gate.gate_failure_boundary()
def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    merge_gate.add_suite_arguments(parser)
    args = cast(merge_gate.GateArguments, parser.parse_args(arguments))
    args.gate_job = gate_job_for_command(["targeted_test.py", *arguments])
    with test_run_folder():
        return merge_gate.run_tests_only(args, merge_gate.SuiteMode.TARGETED_TEST)


if __name__ == "__main__":
    merge_gate.run_gate_cli(main)
