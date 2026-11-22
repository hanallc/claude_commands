#!/usr/bin/env python3
"""Apply the showrunner guard to a Codex tool call."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys

from scripts.hooks.showrunner_guard import ConfiguredBudget, Policy, Resolver
from scripts.hooks.showrunner_guard import codex_front, decide, emit, resolve_production_paths


def main(
    *,
    resolver: Resolver = resolve_production_paths,
    policy: Policy = decide,
    policy_timeout_seconds: float | ConfiguredBudget = ConfiguredBudget.READ_CONFIG,
) -> int:
    emit(codex_front(sys.stdin.read(), resolver=resolver, policy=policy,
                     policy_timeout_seconds=policy_timeout_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
