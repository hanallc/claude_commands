"""Format step names and text for the Builds page."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from types import MappingProxyType

from scripts.build_queue.snapshot_codec import public_queue_text, row_text

if TYPE_CHECKING:
    from scripts.build_queue.admission_records import RunningStep, WaitingStep
    from scripts.lib.build_steps import StepNames
    from scripts.message.unit_labels import UnitLabels

NO_UNIT_LABELS: Mapping[str, UnitLabels] = MappingProxyType({})

@dataclass(frozen=True)
class Span:
    text: str


Line = tuple[Span, ...]


def owner_name(identity: UnitLabels | None, unit: str, *later: str) -> str:
    """Name who started the work: a unit, the staff showrunner or session, then each later name in order."""
    showrunner = public_queue_text(identity.showrunner) if identity is not None else ""
    for candidate in (unit, identity.unit if identity is not None else ""):
        shown = public_queue_text(candidate)
        if showrunner:
            shown = public_queue_text(shown.removeprefix(showrunner + "-"))
        if shown:
            return shown
    staff_owner = public_queue_text(identity.owner_text) if identity is not None else ""
    return staff_owner or next((shown for name in later if (shown := public_queue_text(name))), "")


def step_owner_name(step: WaitingStep | RunningStep, labels: Mapping[str, UnitLabels]) -> str:
    """Name who started the step: a unit, the staff owner, the stored caller, or else the repository."""
    return owner_name(labels.get(step.session_id), step.unit, step.session_name, step.project)


def row_what(what: str, step: str) -> str:
    """What the work does: the caller's own usable text, such as the mac_queue_run --what text, or else the step."""
    return public_queue_text(what) or step


def wait_line_name(unit: str, caller: str, folder: str, step: str, *, identity: UnitLabels | None = None,
                   fallback_name: str = "") -> str:
    """Name a waiting step `<owner>: <step>`; owner_name picks the owner, then the folder name, then the fallback."""
    return row_text(owner_name(identity, unit, caller, Path(folder).name, fallback_name), step)


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
