"""Run the Mac route in its own process during a split gate."""

from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from importlib import import_module
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import BinaryIO, cast

from scripts.aws_target import gate_box, ledger
from scripts.lib import process_group
from scripts.production.gate_routes import GateRun, MacGateCancellation, MacGateRoute, SuiteMode, TreeSelection


MAC_GATE_MODULE = "scripts.mac_test.mac_gate"


def load_mac_gate_route(module: str) -> MacGateRoute:
    return cast(MacGateRoute, cast(object, import_module(module)))


@dataclass(frozen=True)
class MacGateMiss:
    """Keep the reason and elapsed time when the Mac returns no result."""
    reason: str
    seconds: float


@dataclass(frozen=True)
class MacGateProcess:
    """Read the Mac result pipe and reap the child."""
    process: subprocess.Popen[bytes]
    result_reader: BinaryIO
    started_at: float

    def result(self) -> GateRun | MacGateMiss:
        try:
            payload = self.result_reader.read()
            status = self.process.wait()
            if status:
                return MacGateMiss(f"The Mac route process exited with status {status}",
                                   time.monotonic() - self.started_at)
            if not payload:
                return MacGateMiss("The Mac route process returned no result", time.monotonic() - self.started_at)
            row = ledger.mapping(cast(object, json.loads(payload)))
            if "gate" in row:
                return gate_box.decode_run(row["gate"])
            seconds = row["seconds"]
            if (not isinstance(seconds, (float, int)) or isinstance(seconds, bool)
                    or not math.isfinite(seconds) or seconds < 0):
                raise ValueError("invalid elapsed time")
            return MacGateMiss(ledger.text(row, "miss"), seconds)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
            return MacGateMiss(f"The Mac route process returned an invalid result ({error})",
                               time.monotonic() - self.started_at)


@contextmanager
def start_mac_gate(module: str, repo: Path, *, mode: SuiteMode, full: bool,
                   module_time_limit_s: float, selected: TreeSelection) -> Generator[MacGateProcess]:
    """Close the cancellation writer before reaping the child."""
    repo = repo.resolve()
    with ExitStack() as parent_ends:
        cancel_read, cancel_write = os.pipe()
        cancel_writer = parent_ends.enter_context(os.fdopen(cancel_write, "wb"))
        with ExitStack() as child_ends:
            _ = child_ends.enter_context(os.fdopen(cancel_read, "rb"))
            result_read, result_write = os.pipe()
            _ = child_ends.enter_context(os.fdopen(result_write, "wb"))
            result_reader = parent_ends.enter_context(os.fdopen(result_read, "rb"))
            command = [sys.executable, "-m", "scripts.production.mac_gate_process",
                       module, str(repo), str(result_write), str(cancel_read),
                       mode.value, str(int(full)), str(module_time_limit_s), selected.tree, *selected.paths]
            started_at = time.monotonic()
            process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2],
                                       stdin=subprocess.DEVNULL, start_new_session=True,
                                       pass_fds=(cancel_read, result_write), restore_signals=False)
        try:
            yield MacGateProcess(process, result_reader, started_at)
            cancel_writer.close()
            if process.returncode is None:
                _ = result_reader.read()
                _ = process.wait()
        except BaseException:
            cancel_writer.close()
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process_group.send_process_group(process.pid, signal.SIGKILL)
                _ = process.wait()
            raise


def main(arguments: Sequence[str]) -> None:
    module, repo, result_writer, reader, mode, full, time_limit, tree, *paths = arguments
    result_fd = int(result_writer)
    os.set_inheritable(result_fd, False)
    mac = load_mac_gate_route(module)
    started_at = time.monotonic()
    with os.fdopen(int(reader), "rb") as read_end:
        result = mac.route(Path(repo), mode=SuiteMode(mode), full=bool(int(full)),
                           module_time_limit_s=float(time_limit), selected=TreeSelection(tuple(paths), tree),
                           cancellation=MacGateCancellation(read_end.fileno()))
    row = ({"miss": result, "seconds": time.monotonic() - started_at} if isinstance(result, str)
           else {"gate": gate_box.encode_run(result)})
    with suppress(BrokenPipeError), os.fdopen(result_fd, "wb") as write_end:
        _ = write_end.write(json.dumps(row).encode("utf-8"))


if __name__ == "__main__":
    main(sys.argv[1:])
