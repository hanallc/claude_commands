"""Format recorded queue evidence without reading admission configuration."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from scripts.build_queue.queue_lines import age
from scripts.build_queue.scope_activity import NotServiceSlice, RunningServiceJob, WaitingServiceJob, UnmatchedServiceJob
from scripts.build_queue import admission_records
from scripts.lint import memory_admit as gate
from scripts.message import unit_labels


def step_labels(step: admission_records.WaitingStep | admission_records.RunningStep,
                labels: Mapping[str, unit_labels.UnitLabels]) -> unit_labels.UnitLabels:
    identity = labels.get(step.session_id)
    unit = gate.queue_unit(step.unit, step.session_id, labels)
    if identity is not None:
        return replace(identity, unit=unit) if step.unit else identity
    fallback = " ".join(step.session_name.split()) or step.folder.rstrip("/").rpartition("/")[2] or "unknown"
    return unit_labels.UnitLabels(fallback, unit_labels.OWNER_NOT_RECORDED,
                                 unit or unit_labels.OWNER_NOT_RECORDED)


def gib(value: int) -> str:
    return f"{value / admission_records.GIB:.1f} GiB"


def ci_amount(value: int) -> str:
    """Show CI use under 0.1 GiB in MiB, so idle CI never reads 0.0 GiB."""
    return f"{value * 1024 / admission_records.GIB:.0f} MiB" if value < admission_records.GIB // 10 else gib(value)


def room_text(room: admission_records.MemoryRoom | admission_records.ReadingUnavailable) -> str:
    """Show the calculation from the last admission pass."""
    if isinstance(room, admission_records.ReadingUnavailable):
        return "Cap unavailable: " + room.reason
    if isinstance(room.cap, int):
        ci_use = f" + CI use {ci_amount(room.ci_use)}" if room.ci_use else ""
        calculation = f"Cap {gib(room.cap)}; anonymous use {gib(room.used)}{ci_use} + free {gib(room.available)}"
    else:
        missing = "; ".join(f"{run.owner}: {run.use.reason}" for run in room.cap.runs
                            if isinstance(run.use, admission_records.RunUseUnavailable))
        calculation = "Cap unavailable"
        if missing:
            calculation += f": swap-backed use unavailable for {missing}"
        calculation += f"; free {gib(room.available)}"
    swapped = f" − {gib(room.agent_swap_demand)} swap coming back" if room.agent_swap_demand else ""
    text = (f"{calculation} "
            f"− safety {gib(room.safety)}{swapped}; kept aside {gib(room.kept)}; room {gib(room.room)}")
    if room.shortfall:
        text += f"; shortfall {gib(room.shortfall)}"
    return text


def keep_aside_text(run: admission_records.RunKeepAside) -> str:
    use = (f"anonymous use {gib(run.memory.use.bytes)}" if isinstance(run.memory.use, admission_records.AnonymousRunUse)
           else f"swap-backed use unavailable: {run.memory.use.reason}")
    return (f"need {gib(run.memory.need)}; {use}; held slot {gib(run.memory.held)}; "
            f"kept aside {gib(run.bytes)}")


def service_activity_text(memory: admission_records.CompilerServiceMemory | admission_records.UnownedSliceMemory, now: float) -> str:
    """Describe the recorded service state at the pass time."""
    state = admission_records.recorded_service_state(memory)
    if isinstance(state, NotServiceSlice):
        raise ValueError("a build scope has no service state")
    if isinstance(state, (RunningServiceJob, WaitingServiceJob, UnmatchedServiceJob)):
        elapsed = f"{age(state.started_at, now)} min" if state.started_at else "age unknown"
        activity = ("running job" if isinstance(state, RunningServiceJob)
                    else "waiting job" if isinstance(state, WaitingServiceJob) else "unmatched job")
        return f"{activity}, {elapsed}"
    return "idle"
