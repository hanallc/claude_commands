"""The start of the GitHub job that runs a CI cargo step."""

from __future__ import annotations

import os
from pathlib import Path
import time

from scripts.build_queue import admission_records as records
from scripts.lib import process_stat


def job_start(pid: int, proc_root: Path) -> float | None:
    """Return the CLOCK_BOOTTIME seconds when the GitHub job of pid started, or None without a readable Runner.Worker ancestor.

    A self-hosted runner starts one Runner.Worker process for each job, so its start is the job start.
    """
    seen: set[int] = set()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        try:
            name, fields = process_stat.name_and_fields(pid, proc_root)
            if name == "Runner.Worker":
                return int(fields[19]) / os.sysconf("SC_CLK_TCK")
            pid = int(fields[1])
        except (OSError, IndexError, ValueError):
            return None
    return None


def queue_job_start(boot_start: float | None, *, now: float, boot_now: float) -> records.CiJobStart:
    """Convert a CLOCK_BOOTTIME job start to the queue clock.

    now and boot_now are readings of the queue clock and of CLOCK_BOOTTIME at the same moment.
    """
    if boot_start is None:
        return records.NO_JOB_START
    return records.JobStart(now - (boot_now - boot_start))


def read_queue_job_start(pid: int, proc_root: Path) -> records.CiJobStart:
    """Read the job start of pid on the system queue clock."""
    return queue_job_start(job_start(pid, proc_root), now=time.time(), boot_now=time.clock_gettime(time.CLOCK_BOOTTIME))
