"""Give fixture subprocesses the allowed caller settings and the fixture owner name."""

from __future__ import annotations

import os


VERIFY_TEST_ENVIRONMENT = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "TZ", "TMPDIR", "TERM",
    # Keep the test runner's Python process settings in fixture subprocesses.
    "PYTHON_CPU_COUNT", "PYTHONDONTWRITEBYTECODE",
})
# Every fixture says who started its queue requests, so the queue never refuses a test run.
FIXTURE_OWNER = "test fixture"


def verify_test_environment() -> dict[str, str]:
    """Read the allowed caller settings, set the fixture locale, and name the fixture owner."""
    environment = {name: value for name, value in os.environ.items() if name in VERIFY_TEST_ENVIRONMENT}
    environment["LANG"] = environment["LC_ALL"] = "C"
    environment["CLAUDE_SESSION_NAME"] = FIXTURE_OWNER
    return environment
