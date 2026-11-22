"""Fixed settings for private Mac and SSH target fixtures."""

MAC_CONNECTION_SETTINGS = """\
host=mac
# range: 5e-324..
connect_timeout_s=6
# range: 5e-324..
leftover_timeout_s=10
"""

MAC_OFFLOAD_SETTINGS = MAC_CONNECTION_SETTINGS + """\
# one of: on, off
offload=on
# range: 5e-324..
probe_timeout_s=15
# range: 5e-324..
unreachable_backoff_s=300
# range: 5e-324..
floor_sweep_timeout_s=180
# range: 1..
mirror_busy_backoff_s=120
# whole: 1..
abandoned_session_minutes=60
mirror_root=~/.local/state/mac-test/test-mirror
# range: 5e-324..
free_floor_gib=100
# range: 5e-324..
mac_budget_gib=24
# range: 5e-324..
fetch_limit_mib=512
# range: 5e-324..
fetch_retention_days=14
"""

MAC_IDLE_SETTINGS = """\
# range: 5e-324..
alert_seconds=180
# range: 5e-324..
push_seconds=600
# range: 5e-324..
stale_seconds=300
# range: 5e-324..
repeat_minutes=1440
# range: 5e-324..
send_timeout_s=180
"""

MAC_CLAIM_SETTINGS = """\
ci_repo=
ci_variable=MACOS_CI
ci_workflow=ci.yml
ci_job=macOS: Compile and Test
# range: 5e-324..
gh_timeout_s=30
# range: 5e-324..
ci_jobless_queued_minutes=60
# range: 5e-324..
block_hours=4
# range: 5e-324..
block_max_hours=48
# range: 5e-324..
block_warn_minutes=15
# range: 5e-324..
claim_idle_minutes=15
# range: 5e-324..
claim_poll_seconds=60
"""

MAC_RUN_SETTINGS = MAC_OFFLOAD_SETTINGS + MAC_CLAIM_SETTINGS + MAC_IDLE_SETTINGS

MAC_SWEEP_SETTINGS = """\
# range: 5e-324..
sweep_floor_gib=150
sweep_floor_roots=/tmp ~/rust ~/.local/state/mac-test/test-mirror
"""
