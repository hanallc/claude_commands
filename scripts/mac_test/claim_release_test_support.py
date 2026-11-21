"""Run the provider release command inside a fixture."""

from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from scripts.mac_test import mac_test, offload


def run_claim_release(arguments: Sequence[str], *, paths: mac_test.StatePaths | None = None) -> offload.CommandOutput:
    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        args = mac_test.parser().parse_args(arguments[2:], namespace=mac_test.CliArguments())
        status = mac_test.command_release(args, mac_test.state_paths(args.target) if paths is None else paths)
    return offload.CommandOutput(status, stdout.getvalue(), stderr.getvalue())
