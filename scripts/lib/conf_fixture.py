"""Apply the shared configuration fixture rules."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal, DecimalException
from functools import lru_cache
import math
import re
from typing import Protocol

from scripts.lib import conf_reads


class FixtureSetting(Protocol):
    @property
    def value(self) -> str: ...

    @property
    def limit(self) -> str: ...

    @property
    def line(self) -> int: ...


type SettingsReader = Callable[[str], Mapping[str, FixtureSetting]]
type NumberReader = Callable[[str], Decimal]


# Give fixtures short deadlines and the word values their tests expect.
# Explicit fixture settings take precedence.
TEST_CONFIG_DEFAULTS = {
    "accounts.conf": (
        "usage_stale_minutes=10\n"
        "notes_directory=~/rust/hanadocs/agents\n"
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
        "roots.system=/tmp\n"
        "roots.build=~/build\n"
        "roots.build.natedev=/build\n"
        "names.suffix_characters=abcdefghijklmnopqrstuvwxyz0123456789_\n"
        "patterns.legacy-unique={purpose}{suffix}\n"
        "slots.wait_seconds=30\n"
        "locks.probe_wait_seconds=0.25\n"
        "locks.probe_retry_seconds=0.005\n"
    ),
    "build_targets.conf": (
        "targets=hana-aws,mac,mac-linux,natedev\n"
        "pinned_targets=hana-gpu\n"
        "kind.hana-gpu=ssh\n"
        "platform.hana-gpu=linux\n"
        "settings.hana-gpu=cloud_gate.conf\n"
        "kind.natedev=local\n"
        "platform.natedev=linux\n"
        "kind.mac=ssh\n"
        "platform.mac=macos\n"
        "settings.mac=mac_test.conf\n"
        "kind.mac-linux=ssh\n"
        "platform.mac-linux=linux\n"
        "settings.mac-linux=mac_linux.conf\n"
        "enabled.mac-linux=on\n"
        "kind.hana-aws=ssh\n"
        "platform.hana-aws=linux\n"
        "settings.hana-aws=hana_aws.conf\n"
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
        "host=aws-hana\n"
        "execution_transport=hana-control\n"
        "kinds=check,lint\n"
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
        "recent_minutes=60\n"
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
        "host=mac\n"
        "ci_repo=hanallc/hana\n"
        "ci_variable=MACOS_CI\n"
        "ci_workflow=ci.yml\n"
        "ci_job=macOS: Compile and Test\n"
        "linux_only.hana=hana_video,hana_clerestory\n"
        "offload_exclude..claude=doc\n"
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


def shifted_number(setting: FixtureSetting, decimal: NumberReader) -> str:
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
def shifted_config_lines(source: str, settings: SettingsReader, decimal: NumberReader) -> tuple[str, ...]:
    """Shift the committed values and mark their original values."""
    lines = source.splitlines()
    marked_lines = {shift.line for shift in conf_reads.shifted_values(source)}
    changes = {setting.line: (setting, shifted_number(setting, decimal))
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


LIMIT_PREFIXES = ("# whole:", "# range:", "# one of:")


@lru_cache(maxsize=128)
def _fixture_template_lines(text: str, fixture_keys: frozenset[str], settings: SettingsReader,
                            decimal: NumberReader) -> tuple[str, ...]:
    # Omit unpinned words so a required read reports a missing key before main.
    # A read with a fallback uses that fallback.
    # Git recovery reads only the fixture path's history, if it has any.
    lines = text.splitlines()
    omitted: set[int] = set()
    replaced: set[str] = set()
    for index, line in enumerate(lines):
        if "=" not in line:
            continue
        for key, setting in settings(line).items():
            if key in fixture_keys and key not in replaced:
                replaced.add(key)
                continue
            try:
                _ = decimal(setting.value)
            except ValueError:
                omitted.add(index)
                if index and lines[index - 1].strip().startswith(LIMIT_PREFIXES):
                    omitted.add(index - 1)
    source = "\n".join(line for index, line in enumerate(lines) if index not in omitted)
    return shifted_config_lines(source, settings, decimal)


def config_lines(filename: str, text: str, existing: str = "", overrides: str = "", *,
                 settings: SettingsReader, decimal: NumberReader, explicit: bool = False) -> list[str]:
    """Shift inherited numbers and keep explicit fixture settings."""
    defaults = "" if explicit else TEST_CONFIG_DEFAULTS.get(filename, "")
    if explicit:
        lines = text.splitlines()
    else:
        fixture_keys = frozenset(settings("\n".join((defaults, existing, overrides))))
        lines = list(_fixture_template_lines(text, fixture_keys, settings, decimal))
    for layer, keep_shifts in ((defaults, False), (existing, True), (overrides, False)):
        previous = before_limit = ""
        for raw in layer.splitlines():
            limit = previous if previous.startswith(LIMIT_PREFIXES) else ""
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
                    if index and lines[index - 1].strip().startswith(LIMIT_PREFIXES):
                        start -= 1
                        limit = limit or lines[start]
                        if start and lines[start - 1].startswith(conf_reads.SHIFT_MARK):
                            start -= 1
                    lines[start:index + 1] = [part for part in (mark, limit, f"{key}={value}") if part]
                    break
            else:
                lines.extend(part for part in (mark, limit, raw) if part)
    return lines
