"""The start of the GitHub job that runs a CI cargo step."""

from __future__ import annotations

import os
from pathlib import Path
import time

from scripts.build_queue import admission_records as records
from scripts.lib import process_stat


def job_start(pid: int, proc_root: Path, *, start_time: str | None = None) -> float | None:
    """Return the CLOCK_BOOTTIME seconds when the GitHub job of pid started, or None without a readable Runner.Worker ancestor.

    A self-hosted runner starts one Runner.Worker process for each job, so its start is the job start.
    start_time, when given, is the stat start of pid; pid with another start is a reused pid.
    A parent that started after the process below it is a reused pid, so the walk ends without a job start.
    """
    seen: set[int] = set()
    expected = start_time
    below: int | None = None
    while pid > 0 and pid not in seen:
        seen.add(pid)
        try:
            name, fields = process_stat.name_and_fields(pid, proc_root)
            started = int(fields[19])
            if (expected is not None and fields[19] != expected) or (below is not None and started > below):
                return None
            if name == "Runner.Worker":
                return started / os.sysconf("SC_CLK_TCK")
            pid, expected, below = int(fields[1]), None, started
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


def read_queue_job_start(pid: int, start_time: str, proc_root: Path) -> records.CiJobStart:
    """Read the job start of pid, which started at stat start start_time, on the system queue clock."""
    return queue_job_start(job_start(pid, proc_root, start_time=start_time), now=time.time(),
                           boot_now=time.clock_gettime(time.CLOCK_BOOTTIME))
