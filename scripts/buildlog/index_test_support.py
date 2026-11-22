"""Private Buildlog index and target settings."""

from scripts.build_targets.target_test_support import mac_target_settings

BUILDLOG_INDEX_SETTINGS = """\
# range: 0..
INDEX_UNUSED_DAYS=2
# range: 0..
INDEX_TOUCH_MINUTES=60
# whole: 1..
INDEX_SPILL_PAGES=65536
# range: 0..
INDEX_READ_WAIT_SECONDS=1
"""

BUILDLOG_TARGET_SETTINGS = mac_target_settings() + """\
sync.hosts.natedev=natedev
sync.hosts.mac=Mac,Nates-MacBook-Pro-2023,mac,macbook
sync.hosts.aws=aws-build
sync.connect_timeout_s=10
sync.reach_timeout_s=30
sync.rsync_timeout_s=600
"""
