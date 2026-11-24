#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Read live configuration and recover missing or invalid values from Git."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from threading import Lock
from typing import TYPE_CHECKING, TypeVar, override

from scripts.lib import conf_reads


if TYPE_CHECKING:
    from decimal import Decimal


_warning_sink: ContextVar[Callable[[str, bool], None] | None] = ContextVar("configuration_warning_sink", default=None)


def _print_warning(message: str) -> None:
    print(message, file=sys.stderr)


def _warning(message: str, *, report: bool = True) -> None:
    sink = _warning_sink.get()
    if sink is not None:
        sink(message, report)
    elif report:
        _print_warning(message)


@contextmanager
def diagnostics(*, report: bool = True) -> Generator[list[str]]:
    """Collect reader warnings for a reply, with optional local output."""
    messages: list[str] = []

    def collect(message: str, new_warning: bool) -> None:
        messages.append(message)
        if report and new_warning:
            _print_warning(message)

    token = _warning_sink.set(collect)
    try:
        yield messages
    finally:
        _warning_sink.reset(token)


@dataclass(frozen=True)
class ConfigurationStamp:
    """Identify the file and its modification when the reader opens it."""

    device: int
    inode: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> ConfigurationStamp:
        return cls(info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)


class UnstampedConfiguration(Enum):
    UNSTAMPED = "no file stamp"


def file_stamp(source: Path) -> ConfigurationStamp | UnstampedConfiguration:
    """Stamp the file without a read, so a cache keyed by the stamp sees each edit."""
    try:
        return ConfigurationStamp.from_stat(source.stat())
    except OSError:
        return UnstampedConfiguration.UNSTAMPED


_report_changes: ContextVar[bool] = ContextVar("report_configuration_changes", default=False)
_reported_bad_edits: set[tuple[Path, str, str, ConfigurationStamp | UnstampedConfiguration]] = set()
_bad_edits_lock = Lock()


@contextmanager
def report_key_changes() -> Generator[None]:
    """Report each change to an invalid live key once."""
    token = _report_changes.set(True)
    try:
        yield
    finally:
        _report_changes.reset(token)


def _key_warning(source: Path, key: str, reason: str, bad: str,
                 stamp: ConfigurationStamp | UnstampedConfiguration) -> None:
    report = True
    if _report_changes.get():
        identity = (source, key, bad, stamp)
        with _bad_edits_lock:
            report = identity not in _reported_bad_edits
            _reported_bad_edits.add(identity)
    _warning(f"warning: {reason}", report=report)


class Unusable(ValueError):
    """The named configuration key has no usable value."""


@dataclass(frozen=True)
class Setting:
    """The setting holds its stored value, limit, and source line."""
    value: str
    limit: str
    line: int


@dataclass(frozen=True)
class _ShiftedSetting(Setting):
    """A test fixture setting carries its committed value."""

    origin: conf_reads.ShiftedConfigKey


Result = TypeVar("Result")


@dataclass(frozen=True)
class UsableLiveValue[Value]:
    """The live configuration supplies this usable value."""
    value: Value


@dataclass(frozen=True)
class RecoveredValue[Value]:
    """The recovered configuration value includes the reason for recovery."""
    value: Value
    reason: str


@dataclass(frozen=True)
class UnavailableValue:
    """The reason explains why no usable configuration value is available."""
    reason: str

    def unrecovered(self) -> str:
        """Give the reason after the committed history gave no usable value."""
        return f"{self.reason}; no usable committed value"


@dataclass(frozen=True)
class _MissingValue(UnavailableValue):
    """The live file or key is missing and has no usable value."""

    @override
    def unrecovered(self) -> str:
        """Give the repair instruction for the missing key or file."""
        return self.reason


class _MissingKey(ValueError):
    """The file has no effective entry for the requested key."""


def _missing_key(source: Path, key: str) -> str:
    return f"{source}: {key} is missing; add it to {source}"


def _missing_file(source: Path) -> str:
    return f"{source}: the file does not exist; add it with its keys"


@dataclass(frozen=True)
class _UnreadableFile:
    """The file read failure holds its reason and whether the file is missing."""
    reason: str
    missing: bool


class UseConfiguredValue(Enum):
    """Read current configuration when the call starts."""

    CURRENT = "current configuration"


class HostOverride(Enum):
    """Tell whether a `<key>.<host>` key for the reading host replaces the key.

    IGNORED fits a key whose last part names something other than the reading host, such as a watched machine.
    """

    APPLIES = "the reading host key replaces the key"
    IGNORED = "the key is read as written"


@dataclass(frozen=True)
class ConfigRead:
    """Keep one live file read for the settings used in one pass."""

    source: Path
    settings: dict[str, Setting] | _UnreadableFile
    stamp: ConfigurationStamp | UnstampedConfiguration = UnstampedConfiguration.UNSTAMPED

    @property
    def file_missing(self) -> bool:
        """The file was missing when the reader opened it."""
        return isinstance(self.settings, _UnreadableFile) and self.settings.missing

    def _resolve(self, key: str, convert: Callable[[str], Result], *, warn: bool,
                 default: Result | UseConfiguredValue = UseConfiguredValue.CURRENT, allow_blank: bool = False,
                 recover_invalid: bool = True, use_file_limits: bool = True,
                 host_override: HostOverride = HostOverride.APPLIES
                 ) -> UsableLiveValue[Result] | RecoveredValue[Result] | UnavailableValue:
        bad = "<unreadable>"

        def read_key(settings: dict[str, Setting], *, use_default: bool = True) -> tuple[Result, str | None]:
            nonlocal bad
            try:
                setting = _setting(settings, key, host_override)
            except _MissingKey:
                if not use_default or isinstance(default, UseConfiguredValue):
                    raise
                return default, None
            if isinstance(setting, _ShiftedSetting):
                setting.origin.record()
            bad = setting.value
            if not use_file_limits:
                return convert(setting.value), setting.value
            if allow_blank and not setting.value and not setting.limit.startswith(("# whole:", "# range:", "# one of:")):
                return convert(""), setting.value
            return _converted(setting, convert), setting.value

        missing: _MissingValue | None = None
        try:
            if isinstance(self.settings, _UnreadableFile):
                if self.settings.missing:
                    raise _MissingKey(self.settings.reason)
                raise ValueError(self.settings.reason)
            value, _ = read_key(self.settings)
            return UsableLiveValue(value)
        except (OSError, UnicodeError, ValueError, ArithmeticError) as error:
            if isinstance(error, _MissingKey):
                repair = _missing_key(self.source, key)
                if self.file_missing:
                    repair += "; the file does not exist"
                missing = _MissingValue(repair)
                bad = "<missing>"
            reason = f"{self.source}: {key}={bad!r}: {error}"
        if missing is None and not recover_invalid:
            return UnavailableValue(reason)
        if warn and missing is None:
            _key_warning(self.source, key, reason, bad, self.stamp)
        current_limit = ""
        if use_file_limits and isinstance(self.settings, dict):
            current = self.settings.get(self.effective_key(key) if host_override is HostOverride.APPLIES else key)
            if current is not None and current.limit.startswith(("# whole:", "# range:", "# one of:")):
                current_limit = current.limit
        fallback: RecoveredValue[Result] | None = None
        for committed in _committed_files(self.source):
            try:
                value, raw_value = read_key(_read_settings(committed, self.source), use_default=missing is None)
            except (ValueError, ArithmeticError):
                continue
            if not current_limit:
                if warn and missing is not None:
                    _key_warning(self.source, key, reason, "<missing>", self.stamp)
                return RecoveredValue(value, reason)
            if fallback is None:
                fallback = RecoveredValue(value, reason)
            if raw_value is None:
                continue
            try:
                _limits(current_limit, raw_value)
            except (ValueError, ArithmeticError):
                continue
            return RecoveredValue(value, reason)
        if missing is not None:
            return missing
        return fallback if fallback is not None else UnavailableValue(reason)

    def origin(self, key: str, convert: Callable[[str], Result], *, use_file_limits: bool = True
               ) -> UsableLiveValue[Result] | RecoveredValue[Result] | UnavailableValue:
        """Read a live or recovered value. The converter can supply all validation."""
        return self._resolve(key, convert, warn=False, use_file_limits=use_file_limits)

    def value(self, key: str, convert: Callable[[str], Result], *,
              default: Result | UseConfiguredValue = UseConfiguredValue.CURRENT, allow_blank: bool = False,
              recover_invalid: bool = True, host_override: HostOverride = HostOverride.APPLIES) -> Result:
        """Read one key with an optional default. Recover missing settings from Git."""
        result = self._resolve(key, convert, warn=True, default=default, allow_blank=allow_blank,
                               recover_invalid=recover_invalid, host_override=host_override)
        if isinstance(result, UnavailableValue):
            raise Unusable(result.unrecovered() if recover_invalid else result.reason)
        return result.value

    def text(self, key: str, *, recover_invalid: bool = True) -> str:
        """Read text and allow defaults for missing values."""
        return self.value(key, str, recover_invalid=recover_invalid)

    def effective_key(self, key: str) -> str:
        """Return the host override key when the live file has one."""
        if isinstance(self.settings, dict):
            return effective_key(self.settings, key, host_name=_local_host_name())
        return key

    def number(self, key: str) -> float:
        return self.value(key, _number)

    def whole(self, key: str) -> int:
        return self.value(key, _whole)

    def words(self, key: str) -> tuple[str, ...]:
        return self.value(key, lambda value: tuple(value.split()))

    def values(self, *keys: str, host_only_prefix: str = "") -> dict[str, str]:
        """Return all values when no keys are given. Add base keys for host overrides that match the prefix."""
        settings = self.settings
        if isinstance(settings, _UnreadableFile):
            settings = _recover_file(self.source, settings)
        selected = dict.fromkeys(keys or settings)
        if host_only_prefix:
            suffix = f".{_local_host_name()}"
            for key in settings:
                if key.startswith(host_only_prefix) and key.endswith(suffix):
                    selected[key.removesuffix(suffix)] = None
        result: dict[str, str] = {}
        for key in selected:
            try:
                setting = _setting(settings, key)
            except _MissingKey:
                continue
            if isinstance(setting, _ShiftedSetting):
                setting.origin.record()
            if not setting.value and not setting.limit.startswith(("# whole:", "# range:", "# one of:")):
                result[key] = setting.value
                continue
            try:
                _limits(setting.limit, setting.value)
                result[key] = setting.value
            except ValueError:
                result[key] = self.text(key)
        return result


def _recover_file(source: Path, fault: _UnreadableFile) -> dict[str, Setting]:
    _warning(f"warning: {source}: keys: {fault.reason}")
    for contents in _committed_files(source):
        return _read_settings(contents, source)
    if fault.missing:
        raise Unusable(_missing_file(source))
    raise Unusable(f"{source}: keys: {fault.reason}; no usable committed file")


@dataclass(frozen=True)
class ConfigurationBytes:
    """The file bytes and the stamp of the open file."""

    data: bytes
    stamp: ConfigurationStamp


def _file_stamp(descriptor: int) -> ConfigurationStamp:
    return ConfigurationStamp.from_stat(os.fstat(descriptor))


def read_bytes(source: Path) -> ConfigurationBytes:
    """Read bytes for a pass that also needs a configuration digest."""
    with source.open("rb") as stream:
        # An edit during the read must invalidate the stamp.
        stamp = _file_stamp(stream.fileno())
        return ConfigurationBytes(stream.read(), stamp)


def read(file: str | Path, *, load: Callable[[Path], ConfigurationBytes] | None = None) -> ConfigRead:
    """Read and parse the live file once; resolve faults when a setting is used."""
    source = path(file)
    stamp: ConfigurationStamp | UnstampedConfiguration = UnstampedConfiguration.UNSTAMPED
    try:
        if load is not None:
            snapshot = load(source)
            stamp = snapshot.stamp
            contents = snapshot.data.decode("utf-8")
        elif _report_changes.get():
            with source.open(encoding="utf-8") as stream:
                try:
                    contents = stream.read()
                finally:
                    stamp = _file_stamp(stream.fileno())
        else:
            contents = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return ConfigRead(source, _UnreadableFile(str(error), isinstance(error, FileNotFoundError)), stamp)
    return ConfigRead(source, _read_settings(contents, source), stamp)


def read_or_repository(stem: str) -> ConfigRead:
    """Use the test-owned file when the fixture has no copy."""
    if "CONF_DIR" in os.environ and not path(stem).exists():
        return read(test_config_path(stem))
    return read(stem)


_TEST_CONFIG = Path(__file__).resolve().parents[2] / "test_config"


def test_config_path(stem: str) -> Path:
    """Return the test-owned copy of a repository config file. /tune never changes it."""
    return _TEST_CONFIG / f"{stem}.conf"


# Give real fixture commands short deadlines.
# A test that exercises a deadline sets its own value.
_TEST_CONFIG_DEFAULTS = {
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
        "hold_sender_timeout_seconds=30\n"
        "sync.connect_timeout_s=10\n"
        "sync.reach_timeout_s=30\n"
        "sync.rsync_timeout_s=60\n"
        "vault.lock_retries=5\n"
        "vault.lock_retry_delay_s=1\n"
        "loopback.command_timeout_s=60\n"
        "loopback.shell_timeout_s=30\n"
    ),
    "buildlog.conf": "INDEX_READ_WAIT_SECONDS=1\n",
    "cloud_gate.conf": (
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
        "CLAUDE_WORKER_REGISTER_WAIT_SECS=30\n"
        "WORKER_FIRST_TURN_SECONDS=30\n"
        "WORKER_SETTLE_SECONDS=30\n"
        "WORKER_COMPACT_SECONDS=30\n"
        "PANE_AGENT_START_SECONDS=30\n"
        "WORKER_PUBLISH_LOCK_SECONDS=15\n"
    ),
    "disks.conf": "measure.timeout_seconds=30\n",
    "fast_track.conf": (
        "check_time_limit_seconds=30\n"
        "fetch_lock_wait_seconds=0.5\n"
    ),
    "hana_aws.conf": (
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
        "connect_timeout_s=6\n"
        "probe_timeout_s=15\n"
        "floor_sweep_timeout_s=30\n"
        "gh_timeout_s=30\n"
        "leftover_timeout_s=10\n"
        "send_timeout_s=30\n"
        "claim_poll_seconds=5\n"
    ),
    "merge_checkpoint.conf": "MAC_PULL_RETRY_SECONDS=2\n",
    "nix_env.conf": (
        "command_timeout_seconds=30\n"
        "alert_timeout_seconds=30\n"
    ),
    "notify.conf": "REVIEW_TIMEOUT_SECONDS=30\n",
    "postmaster.conf": (
        "wait_seconds=3.0\n"
        "claim_grace_seconds=15.0\n"
        "poll_seconds=0.05\n"
    ),
    "production.conf": (
        "RETIRE_TERM_WAIT_SECONDS=10\n"
        "RETIRE_COMMAND_TIMEOUT_SECONDS=30\n"
        "mac_run_example_seconds=20\n"
        "mac_run_status_grace_seconds=30\n"
        "mac_run_poll_seconds=1\n"
        "smoke_ready_seconds=30\n"
        "smoke_exit_seconds=30\n"
        "smoke_connect_seconds=1\n"
        "smoke_call_seconds=5\n"
        "smoke_signal_grace_seconds=2\n"
        "smoke_poll_seconds=1\n"
        "RENAME_LIVE_WAIT_SECONDS=30\n"
        "RENAME_RUN_STOP_WAIT_SECONDS=30\n"
        "RENAME_POLL_SECONDS=1\n"
        "RENAME_COMMAND_TIMEOUT_SECONDS=30\n"
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
        "STOP_GRACE_SECONDS=2\n"
        "KILL_GRACE_SECONDS=5\n"
        "MOVE_STOP_MARGIN_SECONDS=3\n"
        "START_SECONDS=20\n"
        "OPENING_PROMPT_SECONDS=30\n"
        "STAY_SECONDS=10\n"
        "OWN_PANE_SHELL_SECONDS=30\n"
        "RESUME_CHECK_SECONDS=30\n"
        "RESUME_CHECK_INTERVAL_SECONDS=5\n"
    ),
    "stall_watch.conf": (
        "sender_batch_seconds=30\n"
        "sender_cleanup_seconds=2\n"
        "delivery_reserve_seconds=10\n"
    ),
    "statusline.conf": "rebuild.eval_timeout=30\n",
    "test_times.conf": (
        "MODULE_COST_STAGE_TIMEOUT_S=60\n"
        "MODULE_COST_LEFTOVER_S=2\n"
    ),
}


def shifted_number(setting: Setting) -> str:
    from decimal import Decimal, DecimalException

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

        number = decimal(setting.value)
        low = decimal(low_text) if low_text.strip() else None
        high = decimal(high_text) if high_text.strip() else None

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
               for setting in settings(source).values() if setting.line not in marked_lines}
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


def fixture_config_lines(source: Path, existing: str, overrides: str, *,
                         explicit: bool = False) -> list[str]:
    """Shift inherited settings and keep explicit fixture settings."""
    text = source.read_text() if source.exists() else ""
    lines = text.splitlines() if explicit else list(shifted_config_lines(text))
    defaults = "" if explicit else _TEST_CONFIG_DEFAULTS.get(source.name, "")
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


def copy_config_file_fixture(source: Path, destination: Path, *, explicit: bool = False) -> Path:
    """Copy a config file and shift numeric values from its test-owned twin."""
    if source.parent.name == "config":
        owned = source.parent.with_name("test_config") / source.name
        if not owned.is_file():
            _ = shutil.copyfile(source, destination)
            return destination
        source = owned
    if explicit or source.name in _TEST_CONFIG_DEFAULTS:
        contents = "\n".join(fixture_config_lines(source, "", "", explicit=explicit)) + "\n"
    else:
        text = source.read_text(encoding="utf-8")
        lines = shifted_config_lines(text)
        contents = text if lines == tuple(text.splitlines()) else "\n".join(lines) + "\n"
    _ = destination.write_text(contents, encoding="utf-8")
    return destination


def copy_for_test(source: Path, destination: Path) -> Path:
    """Copy test-owned settings and keep existing fixture files and links."""
    import shutil

    def present(folder: str, names: list[str]) -> set[str]:
        target = destination / Path(folder).relative_to(source)
        return {name for name in names if (target / name).is_symlink()
                or os.path.lexists(target / name) and not (target / name).is_dir()}

    def copy(name: str, target: str) -> str:
        template = Path(name)
        if template.parent == source and template.suffix == ".conf":
            return str(copy_config_file_fixture(template, Path(target)))
        return shutil.copy2(name, target)

    keep = present if destination.exists() else None
    return Path(shutil.copytree(source, destination, symlinks=True, ignore=keep, copy_function=copy,
                                dirs_exist_ok=True))


def directory() -> Path:
    """Return the live configuration folder, or the test fixture override."""
    return Path(os.environ.get("CONF_DIR") or "~/.claude/config").expanduser()


def path(file: str | Path) -> Path:
    conf_reads.record(file if isinstance(file, str) else file.stem)
    if isinstance(file, Path):
        return file.expanduser()
    if not file or Path(file).name != file or file.endswith(".conf"):
        raise Unusable(f"{file}: file: expected a configuration stem")
    return directory() / f"{file}.conf"


def _parse(source: str) -> dict[str, Setting]:
    settings: dict[str, Setting] = {}
    previous = ""
    for index, raw in enumerate(source.splitlines()):
        line = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
        if line and not line.startswith("["):
            key, separator, value = line.partition("=")
            if separator:
                _ = settings.setdefault(key.strip(), Setting(value.strip(), previous, index))
        previous = raw.strip()
    return settings


def _read_settings(contents: str, source: Path) -> dict[str, Setting]:
    """Attach shift marks to actual settings only when a test traces reads."""
    settings = _parse(contents)
    if conf_reads.shift_reads_enabled():
        for shift in conf_reads.shifted_values(contents):
            setting = settings.get(shift.key)
            if setting is not None and setting.line == shift.line:
                origin = conf_reads.ShiftedConfigKey(source.name, shift)
                settings[shift.key] = _ShiftedSetting(setting.value, setting.limit, setting.line, origin)
    return settings


def settings(source: str) -> dict[str, Setting]:
    """Return each stored key of a config text, with its first value and line."""
    return _parse(source)


def _local_host_name() -> str:
    return socket.gethostname().partition(".")[0]


def effective_key(settings: dict[str, Setting], key: str, *, host_name: str) -> str:
    """Select the override for the specified short host name when it exists."""
    candidate = f"{key}.{host_name}"
    return candidate if candidate in settings else key


def _setting(settings: dict[str, Setting], key: str, host_override: HostOverride = HostOverride.APPLIES) -> Setting:
    try:
        if host_override is HostOverride.IGNORED:
            return settings[key]
        return settings[effective_key(settings, key, host_name=_local_host_name())]
    except KeyError:
        raise _MissingKey("key is missing") from None


def _limits(limit: str, value: str) -> None:
    if not value.strip():
        raise ValueError("value is empty")
    if limit.startswith("# one of:"):
        choices = tuple(word.strip() for word in limit.removeprefix("# one of:").split(","))
        if value not in choices:
            raise ValueError(f"expected one of {', '.join(choices)}")
    elif limit.startswith(("# whole:", "# range:")):
        form = "# whole:" if limit.startswith("# whole:") else "# range:"
        bounds = limit.removeprefix(form).strip().split("..")
        if len(bounds) != 2:
            raise ValueError(f"invalid limit {limit}")
        convert = _whole if form == "# whole:" else decimal
        number = convert(value)
        low, high = (bound.strip() for bound in bounds)
        if (low and number < convert(low)) or (high and number > convert(high)):
            raise ValueError(f"outside range {low}..{high}")


def decimal(value: str) -> Decimal:
    from decimal import Decimal, InvalidOperation

    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("expected a finite number") from error
    if not result.is_finite() or not math.isfinite(float(result)):
        raise ValueError("expected a finite number")
    return result


def on_off(value: str) -> bool:
    if value not in ("on", "off"):
        raise ValueError("expected on or off")
    return value == "on"


def _number(value: str) -> float:
    return float(decimal(value))


def _whole(value: str) -> int:
    if not re.fullmatch(r"[+-]?[0-9]+", value):
        raise ValueError("expected a whole number")
    return int(value)


def positive_whole(value: str) -> int:
    result = _whole(value)
    if result <= 0:
        raise ValueError("expected a positive whole number")
    return result


def positive_whole_seconds(value: str) -> float:
    return float(positive_whole(value))


_GIT_REPOSITORY_OVERRIDES = frozenset({
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
})


def _git(folder: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {name: value for name, value in os.environ.items() if name not in _GIT_REPOSITORY_OVERRIDES}
    try:
        return subprocess.run(["git", "-C", str(folder), *arguments], env=environment,
                              capture_output=True, text=True, check=False)
    except (OSError, UnicodeError) as error:
        return subprocess.CompletedProcess(["git", *arguments], 1, "", str(error))


def _committed_files(source: Path) -> Iterator[str]:
    folder = next((parent for parent in source.absolute().parents if parent.is_dir()), source.parent)
    root = _git(folder, "rev-parse", "--show-toplevel")
    if root.returncode:
        return
    checkout = Path(root.stdout.strip())
    try:
        relative = source.resolve().relative_to(checkout.resolve()).as_posix()
    except ValueError:
        return
    history = _git(checkout, "log", "--format=%H", "--", relative)
    for revision in history.stdout.splitlines() if history.returncode == 0 else ():
        committed = _git(checkout, "show", f"{revision}:{relative}")
        if committed.returncode == 0:
            yield committed.stdout


def _converted(setting: Setting, convert: Callable[[str], Result]) -> Result:
    whole = convert is _whole or convert is positive_whole or convert is positive_whole_seconds
    if whole and not setting.limit.startswith("# whole:"):
        raise ValueError("expected a # whole: limit")
    if convert is _number and not setting.limit.startswith("# range:"):
        raise ValueError("expected a # range: limit")
    result = convert(setting.value)
    _limits(setting.limit, setting.value)
    return result


def effective_line(file: str | Path, key: str) -> tuple[int, str]:
    """Return the effective source line index and its value without a write."""
    source = path(file)
    try:
        setting = _setting(_parse(source.read_text(encoding="utf-8")), key)
    except (OSError, UnicodeError, ValueError) as error:
        raise Unusable(f"{source}: {key}: {error}") from error
    return setting.line, setting.value


def known_host_names() -> frozenset[str]:
    """Return the local and configured short host names in lowercase."""
    source = path("machines")
    try:
        machines = _parse(source.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        machines = _recover_file(source, _UnreadableFile(str(error), missing=True))
    aliases = machines.get("aliases")
    hosts = {_local_host_name().casefold()}
    if aliases is not None:
        for alias in aliases.value.split():
            hosts.add(alias.casefold())
            names = machines.get(f"{alias}.hosts")
            if names is not None:
                hosts.update(name.partition(".")[0].casefold() for name in names.value.split())
    return frozenset(hosts)


def host_suffix(key: str, names: frozenset[str]) -> str:
    """Return the known host suffix or an empty string."""
    base, _, suffix = key.rpartition(".")
    return suffix if base and suffix.casefold() in names else ""


def full_model_color_key(file: str | Path, key: str) -> bool:
    """Check for a full model ID in a statusline color key."""
    is_statusline = file.name == "statusline.conf" if isinstance(file, Path) else file == "statusline"
    return is_statusline and re.fullmatch(
        r"model\.(?:gpt-[0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*|claude-[a-z][a-z0-9-]*-[0-9][a-z0-9-]*)", key,
    ) is not None


def host_override(file: str | Path, settings: dict[str, Setting], key: str) -> str | None:
    """Find an override. Require a known host suffix for full model color keys."""
    prefix = f"{key}."
    candidates = [candidate for candidate in settings if candidate.startswith(prefix)]
    if not candidates:
        return None
    if not full_model_color_key(file, key):
        return candidates[0]
    names = known_host_names()
    return next((candidate for candidate in candidates if host_suffix(candidate, names) == candidate[len(prefix):]), None)


def editable_line(file: str | Path, key: str) -> tuple[int, str]:
    """Return an exact stored key's line and value when no host override exists."""
    source = path(file)
    try:
        settings = _parse(source.read_text(encoding="utf-8"))
        if key not in settings:
            raise ValueError("exact stored key is missing")
        setting = settings[key]
        if (candidate := host_override(source, settings, key)) is not None:
            raise ValueError(
                f"{key} at line {setting.line + 1} has host override "
                + f"{candidate} at line {settings[candidate].line + 1}"
            )
    except (OSError, UnicodeError, ValueError) as error:
        raise Unusable(f"{source}: {key}: {error}") from error
    return setting.line, setting.value


def check(file: str | Path, key: str, value: str) -> None:
    source = path(file)
    try:
        setting = _setting(_parse(source.read_text(encoding="utf-8")), key)
        _limits(setting.limit, value)
    except (OSError, UnicodeError, ValueError) as error:
        raise Unusable(f"{source}: {key}={value!r}: {error}") from error


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    if len(args) != 3 or args[0] != "get":
        print("usage: conf.py get FILE KEY", file=sys.stderr)
        return 2
    try:
        print(read(args[1]).text(args[2]))
    except Unusable as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
