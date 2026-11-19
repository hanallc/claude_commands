"""Format step names and text for the Builds page."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from types import MappingProxyType

from scripts.build_queue.snapshot_codec import public_queue_text, select_queue_name, wait_line_name

if TYPE_CHECKING:
    from scripts.build_queue.admission_records import RunningStep, WaitingStep
    from scripts.lib.build_steps import StepNames
    from scripts.message.unit_labels import UnitLabels

NO_UNIT_LABELS: Mapping[str, UnitLabels] = MappingProxyType({})

@dataclass(frozen=True)
class Span:
    text: str


Line = tuple[Span, ...]


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


def blocking_step_name(step: WaitingStep, *, fallback_name: str = "") -> str:
    """Name the step ahead of a waiting row."""
    return wait_line_name(step.unit, step.session_name, step.folder, step.step, fallback_name=fallback_name)


def step_text(step: WaitingStep | RunningStep, names: StepNames, *,
              labels: Mapping[str, UnitLabels] = NO_UNIT_LABELS) -> str:
    """Show the owner unit with the step's shown name."""
    owner = step_owner_name(step, labels)
    shown = public_queue_text(names.shown(step.step))
    return select_queue_name(" ".join(part for part in (owner, shown) if part))


def age(since: float, now: float) -> int:
    return max(0, int((now - since) // 60))


def plain(lines: Sequence[Line]) -> str:
    return "\n".join("".join(span.text for span in line) for line in lines)
