#!/usr/bin/env python3
"""Read the build queue without changing priority."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import sys

from scripts.build_queue import admission_protocol, queue_evidence
from scripts.lint import memory_admit
from scripts.message import unit_labels


def main() -> int:
    words = sys.argv[1:]
    if words == ["--help"]:
        print("Usage: build_queue.py [position|session|folder]")
        return 0
    if len(words) > 1:
        print("build queue: use build_queue.py [position|session|folder]", file=sys.stderr)
        return 3
    try:
        reading = memory_admit.queue_read()
        steps = (*reading.view.running, *reading.view.waiting)
        if words:
            selector = words[0]
            labels = unit_labels.unit_labels_for([step.session_id for step in steps], os.environ)
            if selector.isdecimal():
                matches = memory_admit.match_waiters(selector, reading.view)
                steps = tuple(reading.view.waiting[place - 1] for place in matches)
            else:
                steps = tuple(step for step in steps if memory_admit.step_matches(selector, step)
                              or queue_evidence.step_labels(step, labels).session == selector
                              or queue_evidence.step_labels(step, labels).unit == selector)
        print(admission_protocol.encode_record(steps))
        return 0
    except admission_protocol.AdmissionUnavailable as error:
        print(f"build queue: admission service unavailable: {error.reason}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"build queue: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
