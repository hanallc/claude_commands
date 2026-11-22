"""Fixed settings for detached run fixtures."""

DETACHED_RUN_SETTINGS = """\
# whole: 1..
LIVE_RUN_LIMIT=3
# whole: 1..
PROCESS_CHECK_SECONDS=5
# whole: 1..10
RESULT_PUSH_RETRIES=3
# range: 0.1..60
RESULT_PUSH_RETRY_SECONDS=5
# whole: 1..
RESULT_LINE_CHARS=300
# whole: 1..
RESULT_TEXT_CHARS=2000
"""
