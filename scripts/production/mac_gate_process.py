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
import tempfile
import time
from typing import cast

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
    """Read a Mac gate result after its process exits."""
    process: subprocess.Popen[bytes]
    result_path: Path
    started_at: float

    def result(self) -> GateRun | MacGateMiss:
        status = self.process.wait()
        if status:
            return MacGateMiss(f"The Mac route process exited with status {status}",
                               time.monotonic() - self.started_at)
        try:
            row = ledger.mapping(cast(object, json.loads(self.result_path.read_text())))
            if "gate" in row:
                return gate_box.decode_run(row["gate"])
            seconds = row["seconds"]
            if (not isinstance(seconds, (float, int)) or isinstance(seconds, bool)
                    or not math.isfinite(seconds) or seconds < 0):
                raise ValueError("invalid elapsed time")
            return MacGateMiss(ledger.text(row, "miss"), seconds)
        except FileNotFoundError:
            reason = "The Mac route process returned no result"
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
            reason = f"The Mac route process returned an invalid result ({error})"
        return MacGateMiss(reason, time.monotonic() - self.started_at)


@contextmanager
def start_mac_gate(module: str, repo: Path, *, mode: SuiteMode, full: bool,
                   module_time_limit_s: float, selected: TreeSelection) -> Generator[MacGateProcess]:
    """Close the cancellation writer before reaping the child."""
    repo = repo.resolve()
    with tempfile.TemporaryDirectory(prefix="mac-gate-result-") as folder, ExitStack() as cancellation:
        result_path = Path(folder) / "result.json"
        reader, writer = os.pipe()
        _ = cancellation.enter_context(os.fdopen(writer, "wb"))
        command = [sys.executable, "-m", "scripts.production.mac_gate_process",
                   module, str(repo), str(result_path), str(reader),
                   mode.value, str(int(full)), str(module_time_limit_s), selected.tree, *selected.paths]
        started_at = time.monotonic()
        with os.fdopen(reader, "rb"):
            process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2],
                                       stdin=subprocess.DEVNULL, start_new_session=True,
                                       pass_fds=(reader,), restore_signals=False)
        try:
            yield MacGateProcess(process, result_path, started_at)
            cancellation.close()
            _ = process.wait()
        except BaseException:
            cancellation.close()
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process_group.send_process_group(process.pid, signal.SIGKILL)
                _ = process.wait()
            raise


def main(arguments: Sequence[str]) -> None:
    module, repo, result_path, reader, mode, full, time_limit, tree, *paths = arguments
    mac = load_mac_gate_route(module)
    started_at = time.monotonic()
    with os.fdopen(int(reader), "rb") as read_end:
        result = mac.route(Path(repo), mode=SuiteMode(mode), full=bool(int(full)),
                           module_time_limit_s=float(time_limit), selected=TreeSelection(tuple(paths), tree),
                           cancellation=MacGateCancellation(read_end.fileno()))
    row = ({"miss": result, "seconds": time.monotonic() - started_at} if isinstance(result, str)
           else {"gate": gate_box.encode_run(result)})
    _ = Path(result_path).write_text(json.dumps(row))


if __name__ == "__main__":
    main(sys.argv[1:])
