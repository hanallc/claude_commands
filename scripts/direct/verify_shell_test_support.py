"""Load Verify functions for isolated shell fixtures."""

from pathlib import Path

from scripts.lib.shell_test_support import shell_functions


VERIFY = Path(__file__).with_name("verify.sh")
INVOKE = VERIFY.parents[1] / "lint/invoke.sh"


def verify_shell_functions(*names: str) -> str:
    """Load the shared descriptor selector before the Verify functions."""
    return shell_functions(INVOKE, "find_unused_file_descriptor") + shell_functions(VERIFY, *names)


def verify_cancellation_functions(verify: Path, handler: str) -> str:
    """Load the step and log cleanup functions with the cancellation handler."""
    return shell_functions(verify, "stop_run_step", "close_mac_log", handler)
