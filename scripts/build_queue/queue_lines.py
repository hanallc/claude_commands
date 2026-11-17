"""Format step names and text for the Builds page."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from types import MappingProxyType

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
    """Use the recorded unit before the repository name."""
    identity = labels.get(step.session_id)
    unit = step.unit or (identity.unit if identity is not None else "")
    if identity is not None:
        unit = unit.removeprefix(identity.showrunner + "-")
    return unit if unit not in ("", "—", "owner not recorded") else step.project


def step_text(step: WaitingStep | RunningStep, names: StepNames, *,
              labels: Mapping[str, UnitLabels] = NO_UNIT_LABELS) -> str:
    """Show the owner unit with the step's shown name."""
    return field(f"{step_owner_name(step, labels)} {names.shown(step.step)}")


def age(since: float, now: float) -> int:
    return max(0, int((now - since) // 60))


def plain(lines: Sequence[Line]) -> str:
    return "\n".join("".join(span.text for span in line) for line in lines)
