"""Use the gate's admission policy for measurements."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
import sys

from scripts.build_queue.queue_place import HeldReservation, QueuePlace
from scripts.lib import conf
from scripts.lib.python_startup.sitecustomize import startup_pythonpath
from scripts.production import gate
from scripts.production.gate_modules import GateRefused
from scripts.production.gate_routes import SuiteMode


class MeasurementBlocked(Exception):
    """The queue did not admit the measurement."""


@contextmanager
def measurement_place(repo: Path, temporary: Path) -> Generator[QueuePlace | None]:
    if sys.platform == "darwin":
        # The Mac gate guest uses WholeMachine and takes no local reservation.
        yield None
        return
    with ExitStack() as cleanup:
        try:
            place = cleanup.enter_context(gate.gate_reservation(repo, temporary, gate.BuildHoldWait()))
            if not isinstance(place.reservation, HeldReservation):
                raise GateRefused("the gate has no queue reservation")
        except (GateRefused, conf.Unusable, OSError) as error:
            raise MeasurementBlocked(str(error)) from error
        yield place


def measurement_child(place: QueuePlace | None, environment: dict[str, str], fds: tuple[int, ...] = ()
                      ) -> tuple[dict[str, str], tuple[int, ...]]:
    """Give a measured child the same source proof as a gate module."""
    admitted = {**environment, "BUILDLOG_CALLER": SuiteMode.GATE.value}
    if place is not None:
        admitted = place.child_environment(admitted)
    child, fds = gate.gate_child_environment(admitted, fds)
    child["PYTHONPATH"] = startup_pythonpath(child.get("PYTHONPATH", ""))
    return child, fds
