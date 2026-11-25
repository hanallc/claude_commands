"""Read the nightly timer schedule."""

from datetime import UTC, datetime
import os
import re
import subprocess

from scripts.lib.user_zone import user_zone


def nightly_next_run() -> str:
    """Read the next timer start and show it in the user's time zone."""
    environment = {key: value for key, value in os.environ.items() if key != "TZ"} | {"LC_ALL": "C"}
    try:
        timer = subprocess.run(["systemctl", "--user", "--timestamp=unix", "show",
            "--property=NextElapseUSecRealtime,TimersCalendar", "claude-nightly.timer"],
            capture_output=True, text=True, check=True, timeout=5, env=environment)
        properties = dict(line.split("=", 1) for line in timer.stdout.splitlines() if "=" in line)
        now = datetime.now(UTC)
        timestamp = re.fullmatch(r"@(\d+)", properties.get("NextElapseUSecRealtime", ""))
        scheduled = datetime.fromtimestamp(int(timestamp[1]), UTC) if timestamp else None
        if scheduled is None or scheduled <= now:
            # The timer can still show the elapsed start while the service runs.
            calendars = re.findall(r"OnCalendar=([^;]+?)\s+;\s+next_elapse=", properties.get("TimersCalendar", ""))
            if not calendars:
                return "unavailable"
            next_times = subprocess.run(["systemd-analyze", "calendar", f"--base-time=@{int(now.timestamp())}", *calendars],
                capture_output=True, text=True, check=True, timeout=5, env=environment)
            utc_date = (r"(?m)^\s*(?:Next elapse:|\(in UTC\):)\s+[A-Za-z]{3} "
                        + r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC$")
            dates = [match[1] for match in re.finditer(utc_date, next_times.stdout)]
            if not dates:
                return "unavailable"
            scheduled = datetime.strptime(min(dates), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
        return "unavailable"
    return scheduled.astimezone(user_zone()).strftime("%a %m-%d %H:%M %Z")
