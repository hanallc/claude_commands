"""Format step names and text for the Builds page."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from types import MappingProxyType

from scripts.build_queue.snapshot_codec import public_queue_text

if TYPE_CHECKING:
    from scripts.build_queue.admission_records import RunningStep, WaitingStep
    from scripts.lib.build_steps import StepNames
    from scripts.message.unit_labels import UnitLabels

NO_UNIT_LABELS: Mapping[str, UnitLabels] = MappingProxyType({})

@dataclass(frozen=True)
class Span:
    text: str


Line = tuple[Span, ...]


def field(text: str) -> str:
    return " ".join(text.split())


def step_owner_name(step: WaitingStep | RunningStep, labels: Mapping[str, UnitLabels]) -> str:
    """Use a recorded unit, a staff unit, or the repository name."""
    identity = labels.get(step.session_id)
    showrunner = public_queue_text(identity.showrunner) if identity is not None else ""
    for candidate in (step.unit, identity.unit if identity is not None else ""):
        unit = public_queue_text(candidate)
        if showrunner:
            unit = public_queue_text(unit.removeprefix(showrunner + "-"))
        if unit:
            return unit
    return public_queue_text(step.project)


def step_text(step: WaitingStep | RunningStep, names: StepNames, *,
              labels: Mapping[str, UnitLabels] = NO_UNIT_LABELS) -> str:
    """Show the owner unit with the step's shown name."""
    return field(f"{step_owner_name(step, labels)} {names.shown(step.step)}")


def age(since: float, now: float) -> int:
    return max(0, int((now - since) // 60))


def plain(lines: Sequence[Line]) -> str:
    return "\n".join("".join(span.text for span in line) for line in lines)
