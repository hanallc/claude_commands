"""Fixed settings for Verify, output scans, and cleanup tests."""

LINT_TOOL_SETTINGS = """\
mend=on
style_review=on
clippy=on
doc=on
fmt=on
sweep=off
# whole: 1..9999
clippy_fix_jobs=8
clippy_fix_pass_limit=3
# whole: -1000..1000
build_oom_score_adj=500
"""

FOLDER_AUDIT_SETTINGS = """\
# whole: 1..
folder_min_age_seconds=86400
# whole: 1..
folder_remove_after_seconds=604800
# whole: 1..
dataclass_code_days=14
# whole: 1..
folder_idle_depth=3
# whole: 1..
folder_idle_entry_limit=50000
# whole: 1..
folder_rg_files_per_call=500
# whole: 1..
folder_start_lines=20
# whole: 1..
folder_start_bytes=1048576
folder_state_dir=~/.local/state/folder-sweep
"""

TMP_SWEEP_SETTINGS = """\
# whole: 0..
tmp_sweep_log_sample=20
# whole: 1..
tmp_sweep_log_max_mib=16
"""

SWEEP_SCAN_SETTINGS = """\
# range: 0..
recent_minutes=60
# range: 0..
sweep_budget_gib=24
# range: 0..
sweep_budget_gib.hana=96
# range: 0..
sweep_doc_index_mib=250
# whole: 1..
sweep_proc_locks_reads=3
# whole: 1..
sweep_dep_info_head_bytes=4096
# whole: 1..
sweep_max_tree_depth=3
# whole: 1..
sweep_floor_search_depth=8
# whole: 1..
sweep_scan_workers=4
# whole: 1..
sweep_scan_workers.natedev=1
# whole: 1..
sweep_discovery_workers=4
sweep_floor_roots=~/rust ~/.local/state ~/.cache
sweep_ci_targets=~/fixture-ci
sweep_floor_skip=.git node_modules
"""

LINT_SENDER_SETTINGS = "# range: 0..\nsweep_sender_timeout_seconds=30\n"

SWEEP_FLOOR_SETTINGS = """\
# range: 0..
sweep_free_floor_gib.natedev=300
# range: 0..
sweep_phone_floor_gib=150
# range: 0..
sweep_phone_rearm_gib=5
# range: 0..
sweep_phone_rearm_minutes=60
# range: 1..
sweep_crossing_repeat_minutes=525600
# range: 0..100
sweep_free_floor_percent=20
# range: 0..
sweep_build_floor_gib.natedev=300
# range: 0..
floor_lock_alert_minutes=15
# range: 0..
sweep_growth_window_seconds=1800
# range: 0..
sweep_snapshot_max_age_seconds=100800
# range: 0..
sweep_alert_interval_seconds=3600
# whole: 1..
sweep_push_text_limit=500
# whole: 1..
sweep_unexplained_fall_bytes=10737418240
# whole: 1..
sweep_walk_request_fall_bytes=10737418240
# range: 0..
sweep_walk_request_interval_seconds=7200
# whole: 1..
sweep_large_removal_bytes=34359738368
# range: 0..
sweep_holder_lookback_seconds=180
# range: 0..
sweep_delete_stamp_grace_seconds=2
# range: 0..
sweep_history_timeout_seconds=120
# whole: 1..
sweep_cold_crates=50
sweep_message_path_widths=256 128 96 72 56 40 24 16
# whole: 1..
sweep_message_limit=1024
# whole: 1..
sweep_measurement_paths=3
# range: 3..5
sweep_alert_folder_count=3
""" + LINT_SENDER_SETTINGS
