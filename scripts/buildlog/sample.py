"""Record machine resource counters without opening the SQLite index."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict, cast

from scripts.buildlog import store
from scripts.buildlog import cgroup_capture, filesystem
from scripts.lib.boot_identity import parse_boot_id as parse_boot_id
from scripts.lib.cargo_output import CargoOutputRoots, read_cargo_output_roots
from scripts.buildlog.cgroup_capture import (Unmeasured as Unmeasured, StallTotals as StallTotals,
                            PartialStallTotals as PartialStallTotals, read_pressure as read_pressure,
                            parse_pressure as parse_pressure)

PROC = Path("/proc")
MEMINFO = Path("/proc/meminfo")
PRESSURE = Path("/proc/pressure/memory")
CPU_PRESSURE = Path("/proc/pressure/cpu")
IO_PRESSURE = Path("/proc/pressure/io")
STAT = Path("/proc/stat")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
POWER_PROFILE_STATE = Path("/var/lib/power-profiles-daemon/state.ini")


class MachineMemoryUse(NamedTuple):
    memory_bytes: int
    swap_bytes: int


class PowerProfile(NamedTuple):
    name: str
    since_epoch: float


def read_power_profile(path: Path) -> PowerProfile | Unmeasured:
    """Read the daemon's profile and last write time without contacting it."""
    try:
        with path.open() as handle:
            state = os.fstat(handle.fileno())
            if state.st_size >= 1024:
                return Unmeasured.VALUE
            text = handle.read()
    except (OSError, UnicodeError):
        return Unmeasured.VALUE
    in_state = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_state = line == "[State]"
        elif in_state:
            name, separator, value = line.partition("=")
            if separator and name.strip() == "Profile":
                profile = value.strip()
                return PowerProfile(profile, state.st_mtime) if profile else Unmeasured.VALUE
    return Unmeasured.VALUE


class CpuTicks(NamedTuple):
    idle: int
    iowait: int
    total: int


def parse_cpu_ticks(text: str) -> CpuTicks:
    """Read aggregate CPU ticks, excluding guest ticks already in user and nice."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            fields = line.split()[1:]
            if len(fields) < 8:
                raise ValueError("cpu stat lacks the first eight counters")
            ticks = [int(value) for value in fields[:8]]
            return CpuTicks(ticks[3], ticks[4], sum(ticks))
    raise ValueError("cpu stat lacks aggregate cpu line")


def read_cpu_ticks(path: Path) -> CpuTicks | Unmeasured:
    try:
        return parse_cpu_ticks(path.read_text())
    except (OSError, ValueError):
        return Unmeasured.VALUE


def parse_meminfo(text: str) -> MachineMemoryUse:
    """Return used memory and swap in bytes from Linux meminfo text."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, rest = line.partition(":")
        if separator and name in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            words = rest.split()
            if len(words) != 2 or words[1] != "kB":
                raise ValueError(f"invalid {name} in meminfo")
            values[name] = int(words[0]) * 1024
    if len(values) != 4:
        raise ValueError("meminfo lacks memory or swap totals")
    return MachineMemoryUse(values["MemTotal"] - values["MemAvailable"], values["SwapTotal"] - values["SwapFree"])


def anon_bytes(cgroup: Path) -> int | Unmeasured:
    """Read process memory from a slice, or mark an absent slice unmeasured."""
    reading = cgroup_capture.read_cgroup(cgroup)
    if reading["state"] == "present":
        return reading["stat"].get("anon", Unmeasured.VALUE)
    return Unmeasured.VALUE


def sccache_in_service(builds: Path) -> int | Unmeasured:
    if not builds.is_dir():
        return Unmeasured.VALUE
    try:
        return int(bool((builds / "sccache.service/cgroup.procs").read_text().strip()))
    except OSError:
        return 0


class SccacheStats(TypedDict):
    rust_compile_requests: int
    rust_hits: int
    rust_misses: int
    rust_hit_rate: float | None
    cache_size: int | None
    max_cache_size: int | None


class SccacheSample(SccacheStats):
    kind: Literal["sccache_sample"]
    host: str
    at: str
    boot_id: str
    server_started_us: int


def sccache_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected an sccache JSON object.")
    return cast(dict[str, object], value)


def sccache_counter(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("Expected a nonnegative SQLite integer.")
    return value


def rust_cache_count(stats: dict[str, object], key: str) -> int:
    counts = sccache_object(sccache_object(stats[key])["counts"])
    return sccache_counter(counts.get("Rust", 0))


def parse_sccache_stats(text: str) -> SccacheStats:
    result = sccache_object(cast(object, json.loads(text)))
    stats = sccache_object(result["stats"])
    hits = rust_cache_count(stats, "cache_hits")
    misses = rust_cache_count(stats, "cache_misses")
    requests = sccache_counter(hits + misses)
    size, maximum = result["cache_size"], result["max_cache_size"]
    return {"rust_compile_requests": requests, "rust_hits": hits, "rust_misses": misses,
            "rust_hit_rate": hits / requests if requests else None,
            "cache_size": sccache_counter(size) if size is not None else None,
            "max_cache_size": sccache_counter(maximum) if maximum is not None else None}


def sccache_service() -> tuple[int, int]:
    command = ["systemctl", "--user", "show", "sccache.service",
               "--property=ActiveState", "--property=ExecMainStartTimestampMonotonic", "--property=MainPID"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=2, check=True)
    values = {key: value for key, _, value in (line.partition("=") for line in result.stdout.splitlines())}
    pid = int(values["MainPID"])
    start = sccache_counter(int(values["ExecMainStartTimestampMonotonic"]))
    if values["ActiveState"] != "active" or pid <= 0 or start == 0:
        raise ValueError("The sccache service is inactive.")
    return pid, start


def sccache_sample() -> int:
    """Record one hourly reading from the active server."""
    try:
        boot_id = parse_boot_id(BOOT_ID.read_text())
        pid, start = sccache_service()
        server = PROC / str(pid)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("SCCACHE_")}
        for item in (server / "environ").read_bytes().split(b"\0"):
            name, separator, value = item.partition(b"=")
            if separator and name.startswith(b"SCCACHE_"):
                environment[os.fsdecode(name)] = os.fsdecode(value)
        # The server start flag conflicts with --show-stats.
        _ = environment.pop("SCCACHE_START_SERVER", None)
        environment.update(SCCACHE_LOG="sccache::commands=debug", RUST_LOG_STYLE="never")
        at = time.time()
        result = subprocess.run(["sccache", "--show-stats", "--stats-format", "json"],
                                capture_output=True, text=True, timeout=2, check=True,
                                cwd=server / "cwd", env=environment)
        # sccache v0.15 prints default counters when the connection fails.
        # Only the server request path logs this message.
        if not any(line.endswith(" DEBUG sccache::commands] request_stats") for line in result.stderr.splitlines()):
            return 0
        stats = parse_sccache_stats(result.stdout)
        if sccache_service() != (pid, start):
            return 0
    except (OSError, ValueError, KeyError, RecursionError, subprocess.SubprocessError):
        return 0
    host = store.host_name()
    record: SccacheSample = {**stats, "kind": "sccache_sample", "host": host, "at": store.utc_iso(at),
                             "boot_id": boot_id, "server_started_us": start}
    store.append_line(store.sample_file(host, at), record)
    return 0


class PoolTokensMeasured(TypedDict):
    state: Literal["measured"]
    free_pool_tokens: int


def free_pool_tokens(pool: str) -> PoolTokensMeasured | cgroup_capture.ReadingUnavailable:
    command = ["stevie", "-t"] if pool == "/dev/steve" else ["stevie", "-s", pool, "-t"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=0.5, check=False)
        value = int(result.stdout.strip())
        if result.returncode == 0 and value >= 0:
            return {"state": "measured", "free_pool_tokens": value}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {"state": "unavailable"}


class FilesystemMeasured(TypedDict):
    state: Literal["measured"]
    path: str
    total: int | Literal["unavailable"]
    used: int
    free: int
    device: str
    device_name: str


def filesystem_reading() -> FilesystemMeasured | cgroup_capture.ReadingUnavailable:
    path = os.environ.get("BUILDLOG_TARGET_FILESYSTEM", "")
    if not path:
        roots = read_cargo_output_roots(Path(os.environ.get("BUILDLOG_WORKSPACE", os.getcwd())))
        if not isinstance(roots, CargoOutputRoots):
            return {"state": "unavailable"}
        path = str(roots.build_directory)
    existing = Path(path)
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    path = str(existing)
    try:
        reading = filesystem.filesystem_usage(path)
        return {"state": "measured", "path": path, "total": reading.total, "used": reading.used,
                "free": reading.free, "device": reading.device, "device_name": reading.device_name}
    except OSError:
        return {"state": "unavailable"}


class SampleTaken(NamedTuple):
    at: float
    host: str


def sample() -> SampleTaken:
    """Append the current machine counters to a separate monthly JSONL file."""
    if not all(path.exists() for path in (MEMINFO, PRESSURE, BOOT_ID)):
        raise SystemExit("buildlog sample is Linux only (requires /proc).")
    from scripts.buildlog.memory import read_zram

    at = time.time()
    host = store.host_name()
    boot_id = parse_boot_id(BOOT_ID.read_text())
    memory = parse_meminfo(MEMINFO.read_text())
    stalls = parse_pressure(PRESSURE.read_text())
    cpu_stalls = read_pressure(CPU_PRESSURE)
    io_stalls = read_pressure(IO_PRESSURE)
    ticks = read_cpu_ticks(STAT)
    slices = cgroup_capture.read_slices()
    builds_slice, ci_slice = slices["builds"], slices["ci"]
    builds = builds_slice["stat"].get("anon") if builds_slice["state"] == "present" else None
    ci = ci_slice["stat"].get("anon") if ci_slice["state"] == "present" else None
    sccache = sccache_in_service(cgroup_capture.builds_path())
    profile = read_power_profile(POWER_PROFILE_STATE)
    record: dict[str, object] = {
        "kind": "sample",
        "host": host,
        "at": store.utc_iso(at),
        "boot_id": boot_id,
        "slices": slices,
        "zram": read_zram(),
        "filesystem": filesystem_reading(),
        "jobserver": {"builds": free_pool_tokens("/dev/steve"), "ci": free_pool_tokens("/dev/steve-ci")},
        "mem_used_bytes": memory.memory_bytes,
        "swap_used_bytes": memory.swap_bytes,
        "builds_anon_bytes": builds if isinstance(builds, int) else None,
        "sccache_in_service": sccache if isinstance(sccache, int) else None,
        "ci_anon_bytes": ci if isinstance(ci, int) else None,
        "stall_some_us": stalls.some_us,
        "stall_full_us": stalls.full_us,
        "cpu_stall_some_us": cpu_stalls.some_us if isinstance(cpu_stalls.some_us, int) else None,
        "cpu_stall_full_us": cpu_stalls.full_us if isinstance(cpu_stalls.full_us, int) else None,
        "io_stall_some_us": io_stalls.some_us if isinstance(io_stalls.some_us, int) else None,
        "io_stall_full_us": io_stalls.full_us if isinstance(io_stalls.full_us, int) else None,
        "cpu_idle_ticks": ticks.idle if isinstance(ticks, CpuTicks) else None,
        "cpu_iowait_ticks": ticks.iowait if isinstance(ticks, CpuTicks) else None,
        "cpu_total_ticks": ticks.total if isinstance(ticks, CpuTicks) else None,
        "power_profile": profile.name if isinstance(profile, PowerProfile) else None,
        "power_profile_since": store.utc_iso(profile.since_epoch) if isinstance(profile, PowerProfile) else None,
    }
    store.append_line(store.sample_file(host, at), record)
    return SampleTaken(at, host)
