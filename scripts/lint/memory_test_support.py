"""Private memory readings for tests that use the admission gate."""
from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
import os
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.build_hold.override_guard import require_checkout
from scripts.build_targets.target_test_support import LOCAL_TARGET_SETTINGS
from scripts.lib.build_folders_test_support import workspace_folder_settings
from scripts.lint.config_test_support import LINT_TOOL_SETTINGS, LINT_SENDER_SETTINGS

if TYPE_CHECKING:
    from scripts.build_queue.admission_records import AdmissionPolicy, MemoryConfig

require_checkout(Path(__file__))


@contextmanager
def fixture_feed_code_settings() -> Generator[None]:
    """Use fixed settings when the imported feed hashes its source files."""
    from unittest.mock import patch

    settings = Path(__file__).resolve().parents[2] / "config/memory_feed.conf"
    read_bytes = Path.read_bytes

    def fixture_bytes(path: Path) -> bytes:
        return b"output_folder=fixture\n" if path == settings else read_bytes(path)

    with patch.object(Path, "read_bytes", fixture_bytes):
        yield


ADMISSION_CONFIG_FIXTURE = Path(__file__).with_name("fixtures") / "build_admission.conf"


@cache
def admission_configuration_fixture() -> MemoryConfig:
    """Read the fixed test settings with the original configuration reader."""
    from unittest.mock import patch

    from scripts.build_queue.admission_records import read_memory_config

    with patch.dict(os.environ, {"CONF_DIR": str(ADMISSION_CONFIG_FIXTURE.parent)}):
        return read_memory_config(report=False)


def admission_policy_fixture() -> AdmissionPolicy:
    return admission_configuration_fixture().policy


# Use a zero pass gap for tests that control the clock.
ADMISSION_SERVICE_SETTINGS = """\
cloud_gate_slots=16
gpu_test_slots=2
agent_swap_share_percent=20
# range: 0.125..64
merge_gate_governor_gib=4
# range: 5e-324..1.6742321987285425e299
program_first_peak_gib.targeted-test=4
# whole: 0..50
peak_history_share_percent=10
# range: 0..
progress_profile_sweep_seconds=0.05
# whole: 1..86400
remote_start_hold_seconds=1200
# range: 1..
head_stall_alert_minutes=30
# whole: 1..
peak_hold_seconds.merge-gate=180
# whole: 1..
peak_hold_seconds.targeted-test=180
# whole: 0..
nested_tokens=3
"""
LIMIT_PREFIXES = ("# whole:", "# range:", "# one of:", "# text:")


def _merge_fixture_settings(defaults: str, *layers: str) -> str:
    lines = defaults.splitlines()
    for layer in layers:
        previous = ""
        for raw in layer.splitlines():
            limit = previous if previous.startswith(LIMIT_PREFIXES) else ""
            previous = raw.strip()
            key, separator, value = raw.partition("=")
            if not separator or key.lstrip().startswith("#"):
                continue
            for index, line in enumerate(lines):
                if line.partition("=")[0].strip() == key.strip():
                    lines[index] = f"{key.strip()}={value}"
                    if limit:
                        if index and lines[index - 1].strip().startswith(LIMIT_PREFIXES):
                            lines[index - 1] = limit
                        else:
                            lines.insert(index, limit)
                    break
            else:
                if limit:
                    lines.append(limit)
                lines.append(raw)
    return "\n".join(lines) + "\n"


def write_config_fixture(path: Path, overrides: str = "", *, defaults: str = "") -> int:
    """Apply explicit settings and preserve the limits on existing values."""
    existing = ""
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        existing = path.read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.write_text(_merge_fixture_settings(defaults, existing, overrides), encoding="utf-8")


@cache
def admission_fixture_settings() -> str:
    """Return the fixed admission settings for the fixture clock."""
    return _merge_fixture_settings(ADMISSION_CONFIG_FIXTURE.read_text(encoding="utf-8"), ADMISSION_SERVICE_SETTINGS)


def write_config_fault(path: Path, source: str) -> int:
    """Keep revision limits when a test supplies a partial or damaged config."""
    limits: dict[str, str] = {}
    previous = ""
    for line in path.read_text().splitlines():
        key, separator, _ = line.partition("=")
        if separator and previous.startswith(LIMIT_PREFIXES):
            limits[key.strip()] = previous
        previous = line.strip()
    lines: list[str] = []
    for line in source.splitlines():
        key = line.partition("=")[0].strip()
        if limit := limits.get(key):
            lines.append(limit)
        lines.append(line)
    return path.write_text("\n".join(lines) + "\n")


class ConfigFixtureRefused(ValueError):
    """The configuration folder is not a private folder that only the test owns."""


def commit_config_fixture(path: Path) -> None:
    """Give the private configuration a known-good git revision for fault tests.

    The folder must be under the temporary root and outside every other git repository.
    No caller GIT_* variable reaches git, so the commit can go only to the folder's own repository.
    """
    import subprocess
    import tempfile
    from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

    folder = path.parent.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if folder == temporary_root or not folder.is_relative_to(temporary_root):
        raise ConfigFixtureRefused(f"{folder} is not a private folder under {temporary_root}")
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment |= {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    enclosing = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"], capture_output=True,
                               text=True, env=environment, timeout=TEST_HANG_GUARD_SECS)
    if enclosing.returncode == 0 and Path(enclosing.stdout.strip()).resolve() != folder:
        raise ConfigFixtureRefused(f"{folder} is inside the git repository {enclosing.stdout.strip()}")
    environment |= {"GIT_DIR": str(folder / ".git"), "GIT_WORK_TREE": str(folder)}
    for arguments in (("init", "-q"), ("config", "user.name", "Config fixture"),
                      ("config", "user.email", "fixture@example.invalid"),
                      ("add", path.name), ("commit", "-qm", "known good configuration")):
        _ = subprocess.run(["git", "-C", str(folder), *arguments], check=True,
                           capture_output=True, env=environment, timeout=TEST_HANG_GUARD_SECS)


def seed_config_fixture_folder(root: Path, fixtures: Mapping[str, str]) -> None:
    """Write private fixtures and preserve existing regular files."""
    if root.is_symlink():
        root.unlink()
    root.mkdir(parents=True, exist_ok=True)
    for name, source in fixtures.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"Expected a relative configuration file: {name!r}")
        parent = root
        for component in relative.parts[:-1]:
            parent /= component
            if parent.is_symlink():
                parent.unlink()
        destination = parent / relative.name
        if destination.is_symlink():
            destination.unlink()
        if not destination.exists():
            _ = write_config_fixture(destination, defaults=source)


def config_environment(root: Path, *, fixtures: Mapping[str, str] | None = None) -> dict[str, str]:
    """Set the private configuration paths for subprocesses."""
    seed_config_fixture_folder(root, fixtures or {})
    return {"CONF_DIR": str(root), "BUILD_TARGETS_CONFIG": str(root / "build_targets.conf")}


def install_admission_fixture(root: Path, *, min_pass_gap_seconds: float = 0) -> None:
    """Write the shared admission fixture if the caller has no settings."""
    source = _merge_fixture_settings(admission_fixture_settings(),
                                     f"min_pass_gap_seconds={min_pass_gap_seconds:g}\n")
    seed_config_fixture_folder(root, {"build_admission.conf": source})


def gate_isolation_environment(root: Path) -> dict[str, str]:
    """Keep a gate subprocess outside the caller's holds and session records."""
    install_admission_fixture(root)
    seed_config_fixture_folder(root, {
        "build_targets.conf": LOCAL_TARGET_SETTINGS,
        "build_folders.conf": workspace_folder_settings(root),
        "lint.conf": LINT_TOOL_SETTINGS + LINT_SENDER_SETTINGS,
    })
    return {
        **config_environment(root),
        **private_ci_environment(root),
        "BUILD_HOLD_DIR": str(root / "holders"),
        "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
        "STAFF_FILE": str(root / "staff.json"),
        "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
        "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
    }


def private_ci_environment(root: Path) -> dict[str, str]:
    """Keep CI slice readings inside the private fixture."""
    ci = root / "ci.slice"
    ci.mkdir(exist_ok=True)
    for name, value in (("memory.current", "0"), ("memory.stat", "anon 0\nshmem 0\ninactive_file 0\n"), ("memory.max", str(40 * 1024**3)),
                        ("memory.swap.current", "0"), ("cgroup.procs", "")):
        path = ci / name
        if not path.exists():
            _ = path.write_text(value)
    return {"BUILDLOG_CI_CGROUP": str(ci)}


def write_pressure_reading(path: Path, *, full_avg10: float = 0.0, some_avg10: float = 0.0) -> None:
    """Write both pressure classes for the real admission reader."""
    _ = path.write_text(
        f"some avg10={some_avg10} avg60=0.00 avg300=0.00 total=0\n"
        + f"full avg10={full_avg10} avg60=0.00 avg300=0.00 total=0\n"
    )


def fake_memory_readings(root: Path, *, available_kib: int = 67108864,
                         total_kib: int = 67108864, full_avg10: float = 0.0,
                         swap_total_kib: int = 0, swap_free_kib: int = 0, some_avg10: float = 0.0) -> dict[str, str]:
    meminfo = root / "meminfo"
    pressure = root / "pressure"
    vmstat = root / "vmstat"
    _ = vmstat.write_text("pswpin 0\n")
    _ = meminfo.write_text(f"MemTotal: {total_kib} kB\nMemAvailable: {available_kib} kB\n"
                          + f"SwapTotal: {swap_total_kib} kB\nSwapFree: {swap_free_kib} kB\n")
    write_pressure_reading(pressure, full_avg10=full_avg10, some_avg10=some_avg10)
    install_admission_fixture(root)
    seed_config_fixture_folder(root, {
        "build_folders.conf": workspace_folder_settings(root),
        "lint.conf": LINT_TOOL_SETTINGS + LINT_SENDER_SETTINGS,
    })
    return {
        **config_environment(root),
        **private_ci_environment(root),
        "BUILDLOG_MEMINFO": str(meminfo),
        "BUILDLOG_VMSTAT": str(vmstat),
        "BUILDLOG_BUILDS_CGROUP": str(root / "builds.slice"),
        "BUILDLOG_MEMORY_PRESSURE": str(pressure),
        "BUILDLOG_LIVE_MEMORY_TRIPWIRE": os.environ.get("BUILDLOG_LIVE_MEMORY_TRIPWIRE",
                                                         str(root / "live-memory-reads.jsonl")),
    }
