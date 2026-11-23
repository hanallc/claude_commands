"""Fixed target registries for tests."""
from __future__ import annotations

# Every SSH route reads this build target registry key. 45 seconds gives an SSH keepalive count of 3.
UNREACHABLE_WAIT_FIXTURE_SECONDS = 45
UNREACHABLE_WAIT_FIXTURE = f"# whole: 1..86400\nunreachable_wait_seconds={UNREACHABLE_WAIT_FIXTURE_SECONDS}\n"

LOCAL_TARGET = "kind.natedev=local\nplatform.natedev=linux\n"
LOCAL_TARGET_SETTINGS = "targets=natedev\n" + LOCAL_TARGET
# The Mac Linux guest reads its route from these registry keys. A guest run claims the Mac target.
MAC_LINUX_TARGET_SETTINGS = ("targets=mac,mac-linux,natedev\n" + LOCAL_TARGET
                             + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
                             + "kind.mac-linux=ssh\nplatform.mac-linux=linux\nsettings.mac-linux=mac_linux.conf\n"
                             + UNREACHABLE_WAIT_FIXTURE)
HOLD_TARGET_SETTINGS = LOCAL_TARGET_SETTINGS + """\
default_target=natedev
sync.hosts.mac=mac
sync.hosts.aws=aws-build
# range: 0.01..300
hold_sender_timeout_seconds=30
"""


def mac_target_settings(settings_name: str = "mac_test.conf", *, local: bool = True) -> str:
    """Name a private Mac settings file and select the active targets."""
    active = "mac,natedev" if local else "mac"
    return f"targets={active}\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac={settings_name}\n" + (
        LOCAL_TARGET if local else ""
    ) + UNREACHABLE_WAIT_FIXTURE
