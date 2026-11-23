"""Format step names and text for the Builds page."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from types import MappingProxyType

from scripts.build_queue.snapshot_codec import owner_name, row_text, row_what, wait_line_name

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
    """Name who started the step: a unit, the staff owner, the stored caller, or else the repository."""
    return owner_name(labels.get(step.session_id), step.unit, step.session_name, step.project)


def blocking_step_name(step: WaitingStep, *, labels: Mapping[str, UnitLabels] = NO_UNIT_LABELS,
                       fallback_name: str = "") -> str:
    """Name the step ahead of a waiting row."""
    return wait_line_name(step.unit, step.session_name, step.folder, row_what(step.what, step.step),
                          identity=labels.get(step.session_id), fallback_name=fallback_name)


def step_text(step: WaitingStep | RunningStep, names: StepNames, *,
              labels: Mapping[str, UnitLabels] = NO_UNIT_LABELS) -> str:
    """Show `<owner>: <what>`: the caller's own text for the work, or else the step's shown name."""
    return row_text(step_owner_name(step, labels), row_what(step.what, names.shown(step.step)))


def age(since: float, now: float) -> int:
    return max(0, int((now - since) // 60))


def plain(lines: Sequence[Line]) -> str:
    return "\n".join("".join(span.text for span in line) for line in lines)
