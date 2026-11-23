"""The machine hold record and what the enforcer does on each machine.

The record has the fields of the hold contract (item 226, section 2) that the enforcer reads.
The queue hold store (item 226, part 2) makes these records.
Until then, hold_enforcer.active_holds reads them from the natedev holder files and the Mac blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast, get_args

Machine = Literal["natedev", "mac", "mac-linux", "hana-aws"]
MACHINES = cast(tuple[Machine, ...], get_args(Machine))
HoldVerb = Literal["drain", "clear"]

NAME_THE_MACHINE = "Name the machine: natedev, mac, mac-linux or hana-aws."
CLEAR_WAITS_FOR_AWS_HOLD = "clear waits for the AWS hold"


@dataclass(frozen=True)
class Nate:
    """nate made the hold with a click on the page."""


@dataclass(frozen=True)
class NamedOwner:
    """A staff member made the hold with the command."""
    staff_id: str
    name: str


Owner = Nate | NamedOwner


def owner_name(owner: Owner) -> str:
    return "nate" if isinstance(owner, Nate) else owner.name


@dataclass(frozen=True)
class MachineHold:
    """One active hold: the machine that it stops, the verb, who made it, and why."""
    machine: Machine
    verb: HoldVerb
    owner: Owner
    comment: str


@dataclass(frozen=True)
class UnknownMachine:
    name: str


def parse_machine(name: str | None) -> Machine | UnknownMachine:
    """Accept only the four machine names. No name is an unknown machine."""
    for machine in MACHINES:
        if name == machine:
            return machine
    return UnknownMachine(name or "")


def covered_machines(machine: Machine) -> tuple[Machine, ...]:
    """A mac hold also covers mac-linux. It makes no second hold."""
    return ("mac", "mac-linux") if machine == "mac" else (machine,)


@dataclass(frozen=True)
class ActiveHolds:
    """The active holds of every machine."""
    holds: tuple[MachineHold, ...]

    def on(self, machine: Machine) -> tuple[MachineHold, ...]:
        """Return the holds that cover the machine: its own holds, and a mac hold for mac-linux."""
        return tuple(hold for hold in self.holds if machine in covered_machines(hold.machine))

    def including(self, hold: MachineHold) -> ActiveHolds:
        return self if hold in self.holds else ActiveHolds((*self.holds, hold))


@dataclass(frozen=True)
class Free:
    """No active hold covers the machine."""


@dataclass(frozen=True)
class KeepRuns:
    """A drain: the machine takes no new run, and its running runs finish."""
    hold: MachineHold


@dataclass(frozen=True)
class EndRuns:
    """A clear: the machine takes no new run, and its running runs end."""
    hold: MachineHold


@dataclass(frozen=True)
class ClearWaitsForAwsHold:
    """A clear on hana-aws acts as a drain: ending a box makes AWS calls, and the AWS hold stops them."""
    hold: MachineHold


MachineAction = Free | KeepRuns | EndRuns | ClearWaitsForAwsHold


def machine_action(holds: ActiveHolds, machine: Machine) -> MachineAction:
    """Act by the strongest hold that covers the machine. A clear is stronger than a drain."""
    covering = holds.on(machine)
    for hold in covering:
        if hold.verb == "clear":
            return ClearWaitsForAwsHold(hold) if machine == "hana-aws" else EndRuns(hold)
    return KeepRuns(covering[0]) if covering else Free()


def refused_reason(hold: MachineHold) -> str:
    """The row reason of a Mac or mac-linux run that a clear ends (contract section 6.1)."""
    return (f'ended: {hold.machine} cleared by {owner_name(hold.owner)} — "{hold.comment}". '
            "A Mac run cannot go back to its lines yet.")
