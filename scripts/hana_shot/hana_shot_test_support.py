"""Private endpoint and launch limits for the Hana fixtures."""

SHOT_ENDPOINT_SETTINGS = """\
host=127.0.0.1
# whole: 1..
refused_port=15702
"""

SMOKE_SETTINGS = """\
# whole: 1..65535
smoke_port=15790
# whole: 1..
smoke_ready_seconds=20
# whole: 1..
smoke_exit_seconds=10
# whole: 1..
smoke_connect_seconds=1
# whole: 1..
smoke_call_seconds=5
# whole: 1..
smoke_signal_grace_seconds=2
# whole: 1..
smoke_poll_seconds=1
"""

SHOT_LAUNCH_SETTINGS = SHOT_ENDPOINT_SETTINGS + """\
# range: 0..
call_timeout_seconds=10
# range: 0..
port_probe_seconds=0.2
# range: 0..
git_timeout_seconds=5
# range: 0..
git_status_seconds=3
# whole: 1..
dirty_path_limit=10
# range: 0..
ready_seconds=20
# range: 0..
poll_seconds=0.05
# range: 0..
warm_frame_ms=50
# range: 0..
warm_seconds=2
# range: 0..
shutdown_call_seconds=2
# range: 0..
shutdown_seconds=4
# range: 0..
signal_grace_seconds=1
# whole: 1..
panic_context_lines=3
# whole: 1..
panic_max_lines=40
"""
