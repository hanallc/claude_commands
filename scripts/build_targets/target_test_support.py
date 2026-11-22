"""Fixed target registries for tests."""
from __future__ import annotations

LOCAL_TARGET = "kind.natedev=local\nplatform.natedev=linux\n"
LOCAL_TARGET_SETTINGS = "targets=natedev\n" + LOCAL_TARGET
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
    )
