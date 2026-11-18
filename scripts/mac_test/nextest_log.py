"""Capture nextest output on the Mac and limit the output sent after a failure."""

import re
from collections.abc import Mapping
from pathlib import Path

from scripts.lib import conf

DIRECTORY = ".local/state/mac-test/logs"
TEMPLATE = "nextest.XXXXXXXX"
LOG_NAME = re.compile(r"nextest\.[A-Za-z0-9]{8}")
RETENTION_DAYS = "nextest_failure_log_days"
TAIL_BYTES = "nextest_failure_tail_bytes"
SUMMARY_BYTES = "nextest_failure_summary_bytes"
DEFAULT_SETTINGS = Path(__file__).resolve().parents[2] / "config/mac_test.conf"


def capture_function(values: Mapping[str, str]) -> str:
    """Define the remote shell function that captures and reports nextest output."""
    if TAIL_BYTES in values and SUMMARY_BYTES in values:
        tail_bytes, summary_bytes = int(values[TAIL_BYTES]), int(values[SUMMARY_BYTES])
    else:
        defaults = conf.read(DEFAULT_SETTINGS)
        tail_bytes = int(values[TAIL_BYTES]) if TAIL_BYTES in values else defaults.whole(TAIL_BYTES)
        summary_bytes = int(values[SUMMARY_BYTES]) if SUMMARY_BYTES in values else defaults.whole(SUMMARY_BYTES)
    if tail_bytes < 1 or summary_bytes < 1:
        raise conf.Unusable("Mac nextest output limits must be positive")
    return rf"""run_nextest_logged() {{
    nextest_log_dir="$HOME"/{DIRECTORY}
    (umask 077; mkdir -p "$nextest_log_dir") && [ ! -L "$nextest_log_dir" ] &&
        nextest_log=$(mktemp "$nextest_log_dir"/{TEMPLATE}) || {{
        printf 'mac_test: cannot create a nextest log in %s\n' "$nextest_log_dir" >&2
        return 125
    }}
    if "$@" >"$nextest_log" 2>&1; then
        nextest_status=0
        cat "$nextest_log"
    else
        nextest_status=$?
        touch -m "$nextest_log"
        printf 'mac_test: full nextest log: %s\n' "$nextest_log"
        if [ "$(wc -c < "$nextest_log")" -gt {tail_bytes} ]; then
            printf 'mac_test: tail fragment: '
        fi
        tail -c {tail_bytes} "$nextest_log"
    fi
    if [ -s "$nextest_log" ] && [ "$(tail -c 1 "$nextest_log" | wc -l)" -eq 0 ]; then
        printf '\n'
    fi
    if [ "$nextest_status" -eq 0 ]; then
        rm -f "$nextest_log"
    else
        LC_ALL=C awk -v max_bytes={summary_bytes} '
            {{line = $0; gsub(/\033\[[0-9;]*m/, "", line)}}
            line ~ /^[[:space:]]*Summary \[[[:space:]]*[0-9.]+s\][[:space:]]+[0-9]+(\/[0-9]+)? tests? run:/ {{
                summary = substr(line, 1, max_bytes)
            }}
            END {{if (summary != "") print summary}}' "$nextest_log"
    fi
    return "$nextest_status"
}}"""
