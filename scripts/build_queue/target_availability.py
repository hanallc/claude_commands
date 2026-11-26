"""Name each target block and hold, and whether a natedev step may run on the Mac.

Every fact comes from one `targets.target_snapshots()` reading and the
`build_targets.step_policy` reader. This module reads no block, hold, claim or
backoff file itself, and it runs no remote command.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, final

from scripts.build_queue.admission_records import PackageNotRecorded, RunningStep, WaitingStep
from scripts.build_targets import build_targets, targets
from scripts.build_targets.target_config import ConfigError, ExcludedTarget, SshTarget, StepRoute, LocalRequired, PinnedTarget, MacOnly
from scripts.direct import step_record
from scripts.message import unit_labels

if TYPE_CHECKING:
    from scripts.mac_test import mac_test
    from scripts.mac_test.claim_state import ClaimScope


class TargetRow(TypedDict):
    target: str
    state: str
    reason: str
    holder: str
    unit: str
    showrunner: str
    since: str
    until: str


@dataclass(frozen=True)
class SnapshotsUnreadable:
    reason: str


Snapshots = tuple[targets.TargetSnapshot, ...] | SnapshotsUnreadable


def read_snapshots() -> Snapshots:
    """Read every target's coordination state once for one page reading."""
    try:
        return targets.target_snapshots()
    except (ConfigError, OSError, UnicodeError) as error:
        return SnapshotsUnreadable(str(error))


def holder_sessions(snapshots: Snapshots) -> list[str]:
    """Return the sessions that own a hold, a block or a kept claim, for the page's one label pass."""
    if isinstance(snapshots, SnapshotsUnreadable):
        return []
    owners = (owner for snapshot in snapshots for state in snapshot.states
              for owner in ((state, *state.kept_claims) if isinstance(state, targets.Held) else targets.kept_claims(state)))
    return [owner.holder_session.session_id for owner in owners if isinstance(owner.holder_session, targets.HolderSession)]


def holder_labels(state: targets.Held, labels: Mapping[str, unit_labels.UnitLabels]) -> unit_labels.UnitLabels:
    session = state.holder_session
    if isinstance(session, targets.HolderSession) and session.session_id in labels:
        return labels[session.session_id]
    return unit_labels.UnitLabels(state.holder)


def availability_rows(snapshots: Snapshots, labels: Mapping[str, unit_labels.UnitLabels],
                      zone: tzinfo) -> list[TargetRow]:
    """One row for each block, hold, kept claim or unreadable target; a free or busy target has no other row.

    Kept claims follow the Mac block or outage row.
    """
    if isinstance(snapshots, SnapshotsUnreadable):
        return [unavailable_row("targets", snapshots.reason)]
    rows: list[TargetRow] = []
    now = targets.utc_now()
    for snapshot in snapshots:
        name = snapshot.target.name
        for state in snapshot.states:
            if isinstance(state, targets.Held):
                identity = holder_labels(state, labels)
                rows.append({"target": name, "state": "Blocked" if isinstance(snapshot.target, SshTarget) else "Held",
                             "reason": state.reason, "holder": state.holder, "unit": identity.unit,
                             "showrunner": identity.showrunner,
                             "since": targets.start_text(state.since, zone), "until": targets.end_text(state.end, zone)})
            elif isinstance(state, (targets.Unreachable, targets.StateUnreadable)):
                rows.append(unavailable_row(name, state.reason))
            for kept in targets.kept_claims(state):
                session = kept.holder_session
                identity = (labels.get(session.session_id) if isinstance(session, targets.HolderSession) else None)
                # The reason names the repository, so a claim with no known session shows no holder.
                rows.append({"target": name, "state": "Kept", "reason": targets.kept_claim_reason(kept, now),
                             "holder": identity.session if identity else "",
                             "unit": identity.unit if identity else "", "showrunner": identity.showrunner if identity else "",
                             "since": "since " + targets.kept_since_text(kept.since, zone), "until": ""})
    return rows


def unavailable_row(target: str, reason: str) -> TargetRow:
    return {"target": target, "state": "Unavailable", "reason": reason, "holder": "", "unit": "", "showrunner": "",
            "since": "", "until": ""}


DYNAMIC_REASONS = frozenset({"backoff", "state"})
# Plain words for the static Mac exclusions; other reasons show as recorded.
EXCLUSION_TEXT = {"off": "Mac offload is off", "repo": "this repository does not run on the Mac"}


@dataclass(frozen=True)
class NoMacTarget:
    """No configured target runs macOS."""


@dataclass(frozen=True)
class Allowed:
    """The step may run on the target; the note names the target's state."""
    note: str


@dataclass(frozen=True)
class NotAllowed:
    reason: str


@dataclass(frozen=True)
class FitUnreadable:
    reason: str


TargetFit = Allowed | NotAllowed | FitUnreadable


def mac_reason(fit: TargetFit | NoMacTarget) -> str:
    """Format the Mac reason from the same decision as the target list."""
    if isinstance(fit, Allowed):
        return "Mac: " + fit.note
    if isinstance(fit, (NotAllowed, FitUnreadable)):
        return "Mac: no, " + fit.reason
    return "Mac: no, no Mac target is configured"


@final
class MacRoutes:
    """Say, for each natedev step, whether it may run on the Mac, and why not."""

    def __init__(self, snapshots: Snapshots, labels: Mapping[str, unit_labels.UnitLabels], zone: tzinfo) -> None:
        self.mac: targets.TargetSnapshot | NoMacTarget | SnapshotsUnreadable = (
            snapshots if isinstance(snapshots, SnapshotsUnreadable)
            else next((snapshot for snapshot in snapshots if snapshot.target.platform == "macos"), NoMacTarget()))
        self.labels: Mapping[str, unit_labels.UnitLabels] = labels
        self.zone: tzinfo = zone
        self.policies: dict[tuple[str, str | PackageNotRecorded, str], TargetFit | None] = {}
        self.scopes: dict[str, ClaimScope] = {}

    def route(self, step: WaitingStep | RunningStep) -> TargetFit | NoMacTarget:
        """Read the placement route before the live Mac state."""
        return self.route_fit(step.route, step.step, step.folder, step.package)

    def route_fit(self, route: StepRoute, step: str, folder: str,
                  package: str | PackageNotRecorded) -> TargetFit | NoMacTarget:
        """Apply the recorded route before the target policy and state."""
        if isinstance(route, LocalRequired):
            reason = route.reason
            if reason == "step writes the tree":
                reason = f"{step} edits this checkout on natedev"
            elif reason == "linux_only":
                reason = f"{package if isinstance(package, str) and package else 'this workspace'} is Linux only"
            return NotAllowed(reason)
        if isinstance(route, PinnedTarget) and not self.is_mac(route.target):
            return NotAllowed(f"this step is pinned to {route.target}")
        if isinstance(route, (MacOnly, PinnedTarget)):
            mac = self.mac
            return (self.state_fit(mac) if isinstance(mac, targets.TargetSnapshot)
                    else FitUnreadable("Mac state unreadable, " + mac.reason)
                    if isinstance(mac, SnapshotsUnreadable) else mac)
        return self.fit(step, folder, package)

    def line(self, step: WaitingStep | RunningStep) -> str:
        """Format the recorded route reason."""
        return mac_reason(self.route(step))

    def fit(self, step: str, folder: str, package: str | PackageNotRecorded) -> TargetFit | NoMacTarget:
        mac = self.mac
        if isinstance(mac, SnapshotsUnreadable):
            return FitUnreadable("Mac state unreadable, " + mac.reason)
        if isinstance(mac, NoMacTarget):
            return mac
        kind = (step if step in step_record.KINDS
                else build_targets.recorded_step_kind(step))
        if isinstance(kind, build_targets.KindNotOffloaded):
            return NotAllowed("this step kind does not run on the Mac")
        rule = build_targets.step_routing_rule(kind)
        if isinstance(rule, build_targets.NatedevOnly):
            return NotAllowed(rule.reason)
        excluded = self.policy(mac, folder, package, kind)
        return excluded or self.state_fit(mac)

    def policy(self, mac: targets.TargetSnapshot, folder: str, package: str | PackageNotRecorded,
               kind: build_targets.StepKind) -> TargetFit | None:
        """Read each repository, package and kind once in a page reading.

        Backoff and unreadable state are not policy: the Mac state names them.
        """
        key = (folder, package, kind)
        if key not in self.policies:
            name = package if isinstance(package, str) else ""
            try:
                result = build_targets.step_policy(mac.target, Path(folder), name, kind, backoff=mac.backoff)
                fit: TargetFit | None = None
                if isinstance(result, ExcludedTarget) and result.reason not in DYNAMIC_REASONS:
                    reason = (f"{name or 'this workspace'} is Linux only" if result.reason == "linux_only"
                              else EXCLUSION_TEXT.get(result.reason, result.reason))
                    if isinstance(package, PackageNotRecorded) and result.reason == "linux_only":
                        reason += "; package not recorded"
                    fit = NotAllowed(reason)
            except (ConfigError, OSError, UnicodeError, ValueError) as error:
                fit = FitUnreadable(f"Mac state unreadable, {error}")
            self.policies[key] = fit
        return self.policies[key]

    def is_mac(self, target: str) -> bool:
        """Say whether a pinned target name is the Mac target."""
        return isinstance(self.mac, targets.TargetSnapshot) and self.mac.target.name == target

    def blocked(self) -> bool:
        """The Mac card shows the block, so the idle alert omits its copy."""
        return isinstance(self.mac, targets.TargetSnapshot) and isinstance(self.mac.states[0], targets.Held)

    def holds(self, folder: str) -> str:
        """Name only what keeps this folder's repository off the Mac: a block, the Mac state or its own claim.

        A Mac free for this repository gives "".
        """
        mac = self.mac
        if isinstance(mac, SnapshotsUnreadable):
            return "state unreadable, " + mac.reason
        if isinstance(mac, NoMacTarget):
            return ""
        state = mac.states[0]
        if isinstance(state, (targets.Running, targets.Free)):
            # A free Mac state leaves out guest claims, but a guest claim still holds its repository.
            jobs = self.repository_jobs(mac, folder)
            return "busy with " + "; ".join(job.what for job in jobs) if jobs else ""
        if isinstance(state, targets.StateUnreadable):
            return "state unreadable, " + state.reason
        if isinstance(state, targets.Held):
            # The user, 2026-11-15: a block is a build hold that a unit put on the Mac, so say so.
            text = f"build hold by {self.owner(state)} {targets.end_text(state.end, self.zone)}"
            return text + (f" — {state.reason}" if state.reason else "")
        fit = self.state_fit(mac)
        return fit.note if isinstance(fit, Allowed) else ""

    def repository_jobs(self, mac: targets.TargetSnapshot, folder: str) -> tuple[targets.RunningJob, ...]:
        """The live claims that keep this repository off the Mac: its own claim and any whole Mac claim."""
        from scripts.mac_test import mac_test

        # Cost: target_claim_scope runs git once for each Mac-only waiting folder in each page reading,
        # and only while a Mac claim holds work. self.scopes keeps one result for each folder in the reading.
        working = working_claims(mac)
        if not working:
            return ()
        if folder not in self.scopes:
            self.scopes[folder] = mac_test.target_claim_scope(mac_test.state_paths(mac.target.name), folder)
        scope = self.scopes[folder]
        return tuple(targets.running_job(claim) for claim in working if same_repository(claim.scope, scope))

    def owner(self, state: targets.Held) -> str:
        """The unit that holds a target, else its session, with the showrunner when known."""
        identity = holder_labels(state, self.labels)
        owner = identity.unit if identity.unit != "—" else identity.session
        return owner + (f" ({identity.showrunner})" if identity.showrunner != "—" else "")

    def state_fit(self, mac: targets.TargetSnapshot) -> TargetFit:
        state = mac.states[0]
        if isinstance(state, targets.Held):
            return Allowed(f"blocked by {self.owner(state)} {targets.end_text(state.end, self.zone)}")
        if isinstance(state, targets.Unreachable):
            return Allowed("unreachable, " + state.reason)
        if isinstance(state, targets.StateUnreadable):
            return FitUnreadable("Mac state unreadable, " + state.reason)
        # The Mac state leaves out guest claims, so the note reads the claim folder, as repository_jobs does.
        jobs = tuple(targets.running_job(claim) for claim in working_claims(mac))
        return Allowed("busy with " + "; ".join(job.what for job in jobs) if jobs else "no block or claim recorded")


def working_claims(mac: targets.TargetSnapshot) -> tuple[mac_test.HeldClaim, ...]:
    """The Mac claims that hold work, from the claim folder: a guest claim is not in the Mac state."""
    from scripts.mac_test import mac_test

    return tuple(claim for claim in mac.claims if mac_test.run_holds_work(claim.record))


def same_repository(claim: ClaimScope, step: ClaimScope) -> bool:
    """A whole Mac scope matches every repository; a repository matches by key, else by name."""
    from scripts.mac_test import mac_test

    if not isinstance(claim, mac_test.RepositoryScope) or not isinstance(step, mac_test.RepositoryScope):
        return True
    if claim.key and step.key:
        return claim.key == step.key
    return claim.name == step.name


def mac_claims(snapshots: Snapshots) -> Sequence[mac_test.HeldClaim]:
    """The Mac claims from the same reading, for the Mac rows' owner labels."""
    if isinstance(snapshots, SnapshotsUnreadable):
        return ()
    return next((snapshot.claims for snapshot in snapshots if snapshot.target.platform == "macos"), ())
