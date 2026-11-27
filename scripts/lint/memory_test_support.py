"""Private memory readings for tests that use the admission gate."""
from __future__ import annotations

from decimal import Decimal, DecimalException
from enum import Enum
import math
import os
import re
import shutil
from functools import cache, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.build_hold.override_guard import require_checkout
from scripts.lib import conf, conf_reads
from scripts.lib.helper_tree_test_support import FIXTURE_OWNER

if TYPE_CHECKING:
    from scripts.build_queue.admission_records import AdmissionPolicy, MemoryConfig

require_checkout(Path(__file__))


ADMISSION_CONFIG_FIXTURE = Path(__file__).with_name("fixtures") / "build_admission.conf"


@cache
def admission_configuration_fixture() -> MemoryConfig:
    """Read the explicit fixture settings without using the committed template."""
    from unittest.mock import patch

    from scripts.build_queue.admission_records import read_memory_config

    with patch.dict(os.environ, {"CONF_DIR": str(ADMISSION_CONFIG_FIXTURE.parent)}):
        return read_memory_config(report=False)


def admission_policy_fixture() -> AdmissionPolicy:
    return admission_configuration_fixture().policy


def _template(name: str) -> Path:
    if name == ADMISSION_CONFIG_FIXTURE.name:
        return ADMISSION_CONFIG_FIXTURE
    return Path(__file__).resolve().parents[2] / "config" / name


# Every SSH route reads this build target registry key. 45 seconds gives an SSH keepalive count of 3.
UNREACHABLE_WAIT_FIXTURE_SECONDS = 45
UNREACHABLE_WAIT_FIXTURE = f"# whole: 1..86400\nunreachable_wait_seconds={UNREACHABLE_WAIT_FIXTURE_SECONDS}\n"


# Give fixtures short deadlines and the word values their tests expect.
# Explicit fixture settings take precedence.
TEST_CONFIG_DEFAULTS = {
    "accounts.conf": (
        "quota_relay_timeout_seconds=30\n"
        "quota_edit_timeout_seconds=30\n"
        "account_command_timeout_seconds=15\n"
        "claude_usage_timeout_seconds=15\n"
        "codex_close_timeout_seconds=3\n"
        "codex_version_timeout_seconds=15\n"
        "codex_update_timeout_seconds=30\n"
        "login_probe_timeout_seconds=30\n"
        "role_map_timeout_seconds=20\n"
        "login_peer_read_timeout_seconds=30\n"
    ),
    "aws_target.conf": (
        "gpu_size_trial_preflight_timeout_s=30\n"
        "notification_timeout_s=30\n"
        "aws_timeout_s=30\n"
        "snapshot_notification_timeout_s=30\n"
        "deployment_timeout_s=30\n"
        "stack_wait_timeout_s=30\n"
        "box_setup_timeout_s=60\n"
    ),
    "build_admission.conf": (
        "min_pass_gap_seconds=0\n"
        "running_pass_seconds=5\n"
        "waiting_pass_seconds=1\n"
        "wait_reply_seconds=30\n"
        "query_timeout_seconds=0.5\n"
        "reconnect_seconds=2\n"
        "silence_seconds=30\n"
        "source_check_seconds=1\n"
        "source_settle_seconds=2\n"
    ),
    "build_folders.conf": (
        "slots.wait_seconds=30\n"
        "locks.probe_wait_seconds=0.25\n"
        "locks.probe_retry_seconds=0.005\n"
    ),
    "build_targets.conf": (
        "targets=hana-aws,mac,mac-linux,natedev\n"
        "pinned_targets=hana-gpu\n"
        "enabled.mac-linux=on\n"
        "notices.mac=on\n"
        "hold_sender_timeout_seconds=30\n"
        "sync.connect_timeout_s=10\n"
        "sync.reach_timeout_s=30\n"
        "sync.rsync_timeout_s=60\n"
        "vault.lock_retries=5\n"
        "vault.lock_retry_delay_s=1\n"
        "loopback.command_timeout_s=60\n"
        "loopback.shell_timeout_s=30\n"
    ),
    "buildlog.conf": "index_read_wait_seconds=1\n",
    "cloud_gate.conf": (
        "enabled=on\n"
        "execution_transport=hana-gpu\n"
        "hana_gpu_route_enabled=on\n"
        "gpu_setup_dry_run_retry_timeout_s=30\n"
        "gpu_setup_dry_run_retry_interval_s=5\n"
        "spot_price_live_read_timeout_s=3\n"
        "git_network_timeout_s=30\n"
        "api_timeout_s=30\n"
        "termination_timeout_s=30\n"
        "expiry_timeout_s=30\n"
        "module_time_limit_s=60\n"
        "gpu_size_trial_result_poll_s=10\n"
        "result_poll_s=2\n"
    ),
    "codex_daemon.conf": (
        "timeout_seconds=5.0\n"
        "queue_timeout_seconds=30\n"
    ),
    "copies.conf": (
        "git_timeout_seconds=30\n"
        "guard_timeout_seconds=10\n"
    ),
    "dev_shell.conf": "timeout_s=30\n",
    "direct.conf": (
        "claude_worker_register_wait_secs=30\n"
        "worker_first_turn_seconds=30\n"
        "worker_settle_seconds=30\n"
        "worker_compact_seconds=30\n"
        "pane_agent_start_seconds=30\n"
        "worker_publish_lock_seconds=15\n"
    ),
    "disks.conf": "measure.timeout_seconds=30\n",
    "fast_track.conf": (
        "check_time_limit_seconds=30\n"
        "fetch_lock_wait_seconds=0.5\n"
    ),
    "hana_aws.conf": (
        "enabled=on\n"
        "execution_transport=hana-control\n"
        "guard_grace_seconds=30\n"
        "mount_timeout_seconds=15\n"
        "stop_poll_seconds=10\n"
        "control_poll_seconds=1\n"
        "start_timeout_seconds=30\n"
        "controller_tick_timeout_s=30\n"
        "aws_timeout_s=10\n"
        "stop_wait_timeout_s=30\n"
        "setup_idle_wait_timeout_s=30\n"
        "intent_lookup_timeout_s=30\n"
        "image_upload_timeout_s=60\n"
        "image_wait_interval_s=10\n"
        "source_download_timeout_s=30\n"
        "source_upload_timeout_s=30\n"
        "result_transfer_timeout_s=30\n"
        "source_git_timeout_s=30\n"
    ),
    "hana_instances.conf": "stop_grace_seconds=5\n",
    "hana_shot.conf": (
        "call_timeout_seconds=10.0\n"
        "capture_timeout_seconds=30.0\n"
        "copy_timeout_seconds=30.0\n"
        "git_timeout_seconds=5\n"
        "signal_grace_seconds=2\n"
        "setup_seconds=5.0\n"
        "ready_seconds=30.0\n"
        "warm_seconds=30.0\n"
        "git_status_seconds=3\n"
        "port_probe_seconds=0.5\n"
        "poll_seconds=0.1\n"
        "shutdown_call_seconds=5\n"
        "shutdown_seconds=20\n"
    ),
    "issue_score.conf": "request_timeout=30\n",
    "lint.conf": (
        "sweep_sender_timeout_seconds=30\n"
        "sweep_history_timeout_seconds=30\n"
    ),
    "live_settings.conf": "mac_wake_probe_seconds=5\n",
    "mac_gate.conf": (
        "placement_check_timeout_seconds=10\n"
        "placement_recheck_seconds=15\n"
        "terminate_grace_seconds=3\n"
    ),
    "mac_test.conf": (
        "offload=on\n"
        "linux_only.hana=hana_video,hana_clerestory\n"
        "connect_timeout_s=6\n"
        "probe_timeout_s=15\n"
        "floor_sweep_timeout_s=30\n"
        "gh_timeout_s=30\n"
        "leftover_timeout_s=10\n"
        "send_timeout_s=30\n"
        "claim_poll_seconds=5\n"
    ),
    "merge_checkpoint.conf": "mac_pull_retry_seconds=2\n",
    "nix_env.conf": (
        "command_timeout_seconds=30\n"
        "alert_timeout_seconds=30\n"
    ),
    "notify.conf": "review_timeout_seconds=30\n",
    "postmaster.conf": (
        "wait_seconds=3.0\n"
        "claim_grace_seconds=15.0\n"
        "poll_seconds=0.05\n"
    ),
    "production.conf": (
        "retire_term_wait_seconds=10\n"
        "retire_command_timeout_seconds=30\n"
        "mac_run_example_seconds=20\n"
        "mac_run_status_grace_seconds=30\n"
        "mac_run_poll_seconds=1\n"
        "smoke_ready_seconds=30\n"
        "smoke_exit_seconds=30\n"
        "smoke_connect_seconds=1\n"
        "smoke_call_seconds=5\n"
        "smoke_signal_grace_seconds=2\n"
        "smoke_poll_seconds=1\n"
        "rename_live_wait_seconds=30\n"
        "rename_run_stop_wait_seconds=30\n"
        "rename_poll_seconds=1\n"
        "rename_command_timeout_seconds=30\n"
        "units_census_seconds=10\n"
        "units_table_width_read_milliseconds=50\n"
    ),
    "rca.conf": "send_seconds=30\n",
    "review_links.conf": (
        "page_request_timeout_seconds=15\n"
        "tailscale_timeout_seconds=10\n"
    ),
    "scratch.conf": "removal_remote_timeout_seconds=30\n",
    "send.conf": (
        "waits_review_seconds=30\n"
        "delivery_timeout=30\n"
        "kill_grace=10\n"
        "user_cancel_timeout=30\n"
        "check_timeout=20\n"
        "caller_margin_seconds=15\n"
        "remote_transport_seconds=30\n"
    ),
    "send_repeat_guard.conf": (
        "notice_timeout_seconds=15\n"
        "drain_timeout_seconds=30\n"
    ),
    "shutdown.conf": (
        "stop_grace_seconds=2\n"
        "kill_grace_seconds=5\n"
        "move_stop_margin_seconds=3\n"
        "start_seconds=20\n"
        "opening_prompt_seconds=30\n"
        "stay_seconds=10\n"
        "own_pane_shell_seconds=30\n"
        "resume_check_seconds=30\n"
        "resume_check_interval_seconds=5\n"
    ),
    "stall_watch.conf": (
        "sender_batch_seconds=30\n"
        "sender_cleanup_seconds=2\n"
        "delivery_reserve_seconds=10\n"
    ),
    "statusline.conf": "rebuild.eval_timeout=30\n",
    "test_times.conf": (
        "module_cost_stage_timeout_s=60\n"
        "module_cost_leftover_s=2\n"
    ),
}


def shifted_number(setting: conf.Setting) -> str:
    directive, _, bounds = setting.limit.partition(":")
    low_text, separator, high_text = bounds.strip().partition("..")
    if not separator or directive not in ("# whole", "# range"):
        return setting.value
    try:
        if directive == "# whole":
            if any(re.fullmatch(r"[+-]?[0-9]+", text.strip()) is None
                   for text in (setting.value, low_text, high_text) if text.strip()):
                return setting.value
            whole = int(setting.value)
            low_whole = int(low_text) if low_text.strip() else None
            high_whole = int(high_text) if high_text.strip() else None
            if ((low_whole is not None and whole < low_whole)
                    or (high_whole is not None and whole > high_whole)):
                return setting.value
            if high_whole is None or whole < high_whole:
                return str(whole + 1)
            return str(whole - 1) if low_whole is None or whole > low_whole else setting.value

        number = conf.decimal(setting.value)
        low = conf.decimal(low_text) if low_text.strip() else None
        high = conf.decimal(high_text) if high_text.strip() else None

        def usable(candidate: Decimal) -> bool:
            return (math.isfinite(float(candidate)) and float(candidate) != float(number)
                    and (low is None or candidate >= low) and (high is None or candidate <= high))

        if (low is not None and number < low) or (high is not None and number > high):
            return setting.value
        step = abs(number) / 4 if number else Decimal(1)
        preferred = number * Decimal("1.25") if number else step
        if usable(preferred):
            return str(preferred)
        toward_low = (low + number) / 2 if low is not None else number - step
        if usable(toward_low):
            return str(toward_low)
        if high is not None:
            toward_high = (high + number) / 2
            if usable(toward_high):
                return str(toward_high)
        for endpoint in (low, high):
            if endpoint is not None and usable(endpoint):
                return str(endpoint)
        for direction in (-math.inf, math.inf):
            adjacent = math.nextafter(float(number), direction)
            if not math.isfinite(adjacent):
                continue
            candidate = Decimal.from_float(adjacent)
            if usable(candidate):
                return str(candidate)
        return setting.value
    except (DecimalException, ValueError):
        return setting.value


@lru_cache(maxsize=128)
def shifted_config_lines(source: str) -> tuple[str, ...]:
    """Shift the committed values and mark their original values."""
    lines = source.splitlines()
    marked_lines = {shift.line for shift in conf_reads.shifted_values(source)}
    changes = {setting.line: (setting, shifted_number(setting))
               for setting in conf.settings(source).values() if setting.line not in marked_lines}
    result: list[str] = []
    for index, line in enumerate(lines):
        change = changes.get(index)
        if change is not None:
            setting, value = change
            if value != setting.value:
                result.insert(len(result) - 1, f"{conf_reads.SHIFT_MARK}{setting.value} {value}")
                prefix, separator, suffix = line.partition("=")
                start = len(suffix) - len(suffix.lstrip())
                line = prefix + separator + suffix[:start] + value + suffix[start + len(setting.value):]
        result.append(line)
    return tuple(result)


def fixture_config_lines(source: Path, existing: str, overrides: str) -> list[str]:
    """Shift inherited settings and keep explicit fixture settings."""
    text = source.read_text() if source.exists() else ""
    explicit = source == ADMISSION_CONFIG_FIXTURE
    lines = text.splitlines() if explicit else list(shifted_config_lines(text))
    defaults = "" if explicit else TEST_CONFIG_DEFAULTS.get(source.name, "")
    limit_prefixes = ("# whole:", "# range:", "# one of:")
    for layer, keep_shifts in ((defaults, False), (existing, True), (overrides, False)):
        previous = before_limit = ""
        for raw in layer.splitlines():
            limit = previous if previous.startswith(limit_prefixes) else ""
            mark = before_limit if keep_shifts and limit else ""
            before_limit, previous = previous, raw.strip()
            key, separator, value = raw.partition("=")
            if not separator or key.lstrip().startswith("#"):
                continue
            key = key.strip()
            if mark and not conf_reads.shifted_original(mark, re.split(r"\s+#", value, maxsplit=1)[0].strip()):
                mark = ""
            for index, line in enumerate(lines):
                if line.partition("=")[0].strip() == key:
                    start = index
                    if index and lines[index - 1].strip().startswith(limit_prefixes):
                        start -= 1
                        limit = limit or lines[start]
                        if start and lines[start - 1].startswith(conf_reads.SHIFT_MARK):
                            start -= 1
                    lines[start:index + 1] = [part for part in (mark, limit, f"{key}={value}") if part]
                    break
            else:
                lines.extend(part for part in (mark, limit, raw) if part)
    return lines


def write_config_fixture(path: Path, overrides: str = "", *, template: str | None = None) -> int:
    """Write the merged configuration to the private fixture path."""
    source = _template(f"{template}.conf" if template else path.name)
    existing = path.read_text() if path.exists() else ""
    lines = fixture_config_lines(source, existing, overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def copy_config_file_fixture(source: Path, destination: Path) -> Path:
    """Copy one committed config with its inherited numeric values shifted."""
    if source.name in TEST_CONFIG_DEFAULTS:
        contents = "\n".join(fixture_config_lines(source, "", "")) + "\n"
    else:
        text = source.read_text(encoding="utf-8")
        lines = shifted_config_lines(text)
        contents = text if lines == tuple(text.splitlines()) else "\n".join(lines) + "\n"
    _ = destination.write_text(contents, encoding="utf-8")
    return destination


def copy_config_fixture(source: Path, destination: Path) -> Path:
    """Copy the committed configuration folder into a private fixture."""
    _ = shutil.copytree(source, destination)
    for template in source.glob("*.conf"):
        _ = copy_config_file_fixture(template, destination / template.name)
    return destination


def write_config_fault(path: Path, source: str) -> int:
    """Keep revision limits when a test supplies a partial or damaged config."""
    template = _template(path.name)
    limits: dict[str, str] = {}
    previous = ""
    for line in template.read_text().splitlines():
        key, separator, _ = line.partition("=")
        if separator:
            limits[key.strip()] = previous if previous.startswith(("# whole:", "# range:", "# one of:")) else ""
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


class FixtureConfigValues(Enum):
    """Choose test settings or the exact committed configuration bytes."""

    SHIFTED = "shift inherited values"
    EXACT = "copy committed bytes"


def config_environment(root: Path, *, values: FixtureConfigValues = FixtureConfigValues.SHIFTED) -> dict[str, str]:
    """Give subprocess fixtures a configuration folder with the requested values."""
    folder = root
    names = {source.name for source in _template("build_targets.conf").parent.glob("*.conf")}
    names.add(ADMISSION_CONFIG_FIXTURE.name)
    for name in sorted(names):
        destination = folder / name
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if values is FixtureConfigValues.EXACT:
                _ = shutil.copyfile(_template(name), destination)
            else:
                _ = copy_config_file_fixture(_template(name), destination)
    return {"CONF_DIR": str(folder), "BUILD_TARGETS_CONFIG": str(folder / "build_targets.conf")}


def memory_gate_shell_environment() -> dict[str, str]:
    """Keep the parent reservation out of a memory gate fixture shell."""
    return {name: value for name, value in os.environ.items() if name != "BUILDLOG_RESERVED_STEP"}


def gate_isolation_environment(root: Path) -> dict[str, str]:
    """Keep a gate subprocess outside the caller's holds and session records."""
    return {
        **config_environment(root),
        **private_ci_environment(root),
        "BUILD_HOLD_DIR": str(root / "holders"),
        "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
        "STAFF_FILE": str(root / "staff.json"),
        "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
        "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
        # The fixture names its own owner and drops the caller's, so a test never reads the owner around it.
        "CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": "", "BUILDLOG_OWNER_STAFF_ID": "",
        "CLAUDE_SESSION_NAME": FIXTURE_OWNER,
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
