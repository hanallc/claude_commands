"""Fixed limits for production tests."""

QUESTION_TITLE_SETTINGS = "# whole: 20..\nquestion_title_max_chars=100\n"

UNIT_PORT_SETTINGS = """\
# whole: 1..65535
unit_port_first=15720
# whole: 1..65535
unit_port_last=15789
# whole: 1..65535
smoke_port=15790
# whole: 1..65535
mac_run_port=15710
"""

UNIT_TABLE_SETTINGS = """\
# whole: 40..
units_table_columns=170
# whole: 40..
units_table_floor_columns=120
# whole: 1..4
units_table_cell_lines=4
# whole: 2..
units_table_break_columns=30
# whole: 1..
units_table_width_read_milliseconds=50
# range: 0.1..
units_census_seconds=10
"""

PROMPT_ALERT_SETTINGS = "# range: 0..\nPROMPT_ALERT_SECONDS=60\n"


UNIT_ANOMALY_SETTINGS = """\
UNIT_ANOMALIES_TO=fixture-director
# range: 1..
units_anomaly_repeat_minutes=60
"""

RENAME_SETTINGS = """\
# range: 5e-324..
RENAME_LIVE_WAIT_SECONDS=180
# range: 5e-324..
RENAME_RUN_STOP_WAIT_SECONDS=120
# range: 5e-324..
RENAME_POLL_SECONDS=1
# range: 5e-324..
RENAME_COMMAND_TIMEOUT_SECONDS=900
"""

RETIRE_SETTINGS = """\
# range: 5e-324..
RETIRE_POLL_SECONDS=0.2
# range: 5e-324..
RETIRE_TERM_WAIT_SECONDS=10
# range: 5e-324..
RETIRE_COMMAND_TIMEOUT_SECONDS=900
"""
