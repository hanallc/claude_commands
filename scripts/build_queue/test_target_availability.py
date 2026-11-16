from __future__ import annotations

import unittest
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from scripts.build_queue import target_availability
from scripts.build_queue.admission_records import PackageNotRecorded
from scripts.build_targets import build_targets, targets
from scripts.build_targets.target_config import ConfigError, ExcludedTarget, LocalTarget, SshTarget
from scripts.mac_test import claim_state, eligibility, mac_test
from scripts.message import unit_labels

MAC = SshTarget("mac", "macos", Path("/fixture/mac.conf"))
NATEDEV = LocalTarget("natedev", "linux")
SINCE = datetime(2026, 11, 13, 13, 50, tzinfo=UTC)
LATER = datetime(2026, 11, 13, 15, 10, tzinfo=UTC)
LABELS = {
    "mac-session": unit_labels.UnitLabels("research-director", "hana", "research"),
    "first-session": unit_labels.UnitLabels("report-director", "builds", "report"),
}
UNTIL_RELEASED = targets.UntilReleased()


def held(holder: str, session: str, *, since: datetime | targets.StartNotRecorded = SINCE,
         end: targets.HoldEnd = UNTIL_RELEASED, reason: str = "fixture reason") -> targets.Held:
    return targets.Held(holder, reason, since, end, targets.HolderSession(session))


def snapshot(target: SshTarget | LocalTarget, *states: targets.TargetState,
             backoff: eligibility.BackoffState = eligibility.NO_BACKOFF,
             claims: tuple[mac_test.HeldClaim, ...] = ()) -> targets.TargetSnapshot:
    return targets.TargetSnapshot(target, states, claims, backoff)


def claim(scope: claim_state.ClaimScope, what: str) -> mac_test.HeldClaim:
    record: claim_state.ProcessBoundClaim = {
        "claim_id": "c-" + what, "episode": "e", "attempt": "a", "submitting_session": "s", "pid": 4242,
        "proc_start": "1", "what": what, "worktree": "/fixture", "since": SINCE.isoformat()}
    return mac_test.HeldClaim(scope, Path("/fixture/claims") / (what + ".json"), record)


def scope_of(scopes: Mapping[str, claim_state.ClaimScope]) -> Callable[[claim_state.StatePaths, str], claim_state.ClaimScope]:
    def find(_paths: claim_state.StatePaths, folder: str) -> claim_state.ClaimScope:
        return scopes[folder]
    return find


def live_unless(what: str) -> Callable[[mac_test.RunRecord], bool]:
    def live(record: mac_test.RunRecord) -> bool:
        return record["what"] != what
    return live


MAC_BLOCK = held("research", "mac-session", reason="Mac CI restore",
                 end=targets.AwaitingBlockRemoval(LATER))
NATEDEV_HOLDS = (held("report", "first-session", end=targets.ReleaseEstimated(LATER), reason="disk walk"),
                 held("unnamed", "other-session", since=targets.StartNotRecorded(), reason="cargo sweep"))


class AvailabilityRowTests(unittest.TestCase):
    def test_mac_block_and_two_natedev_holds_name_owner_reason_and_times(self) -> None:
        snapshots = (snapshot(MAC, MAC_BLOCK), snapshot(NATEDEV, *NATEDEV_HOLDS))
        rows = target_availability.availability_rows(snapshots, LABELS, UTC)
        self.assertEqual(rows, [
            {"target": "mac", "state": "Blocked", "reason": "Mac CI restore", "holder": "research", "unit": "research",
             "showrunner": "hana", "since": "since 13:50 UTC",
             "until": "awaiting unblock; requested expiry was 15:10 UTC"},
            {"target": "natedev", "state": "Held", "reason": "disk walk", "holder": "report", "unit": "report",
             "showrunner": "builds", "since": "since 13:50 UTC", "until": "expected release 15:10 UTC"},
            {"target": "natedev", "state": "Held", "reason": "cargo sweep", "holder": "unnamed", "unit": "—",
             "showrunner": "—", "since": "start not recorded", "until": "until released"},
        ])
        self.assertEqual(target_availability.holder_sessions(snapshots),
                         ["mac-session", "first-session", "other-session"])

    def test_free_and_busy_targets_have_no_row(self) -> None:
        job = targets.RunningJob("nextest hana", SINCE, targets.NotSeenIdle())
        snapshots = (snapshot(MAC, targets.Running((job,))), snapshot(NATEDEV, targets.Free()))
        self.assertEqual(target_availability.availability_rows(snapshots, LABELS, UTC), [])

    def test_unreachable_and_unreadable_targets_show_as_unavailable(self) -> None:
        snapshots = (snapshot(MAC, targets.Unreachable("retry backoff")),
                     snapshot(NATEDEV, targets.StateUnreadable("holder file: bad json")))
        rows = target_availability.availability_rows(snapshots, LABELS, UTC)
        self.assertEqual([(row["target"], row["state"], row["reason"]) for row in rows],
                         [("mac", "Unavailable", "retry backoff"), ("natedev", "Unavailable", "holder file: bad json")])

    def test_an_unreadable_configuration_shows_one_row_and_no_sessions(self) -> None:
        unreadable = target_availability.SnapshotsUnreadable("targets.conf: missing")
        self.assertEqual(target_availability.availability_rows(unreadable, LABELS, UTC), [
            {"target": "targets", "state": "Unavailable", "reason": "targets.conf: missing", "holder": "", "unit": "",
             "showrunner": "", "since": "", "until": ""}])
        self.assertEqual(target_availability.holder_sessions(unreadable), [])
        self.assertEqual(target_availability.mac_claims(unreadable), ())

    def test_read_snapshots_returns_the_configuration_error(self) -> None:
        with mock.patch.object(targets, "target_snapshots", side_effect=OSError("disk gone")):
            self.assertEqual(target_availability.read_snapshots(), target_availability.SnapshotsUnreadable("disk gone"))
        with mock.patch.object(targets, "target_snapshots", side_effect=ConfigError("bad key")):
            self.assertIsInstance(target_availability.read_snapshots(), target_availability.SnapshotsUnreadable)


class MacRouteTests(unittest.TestCase):
    def routes(self, *mac_states: targets.TargetState) -> target_availability.MacRoutes:
        return target_availability.MacRoutes((snapshot(MAC, *mac_states), snapshot(NATEDEV, targets.Free())), LABELS, UTC)

    def test_holds_names_each_mac_state_and_a_free_mac_gives_nothing(self) -> None:
        cases: tuple[tuple[targets.TargetState, str], ...] = (
            (MAC_BLOCK, "blocked by research (hana) awaiting unblock; requested expiry was 15:10 UTC"),
            (held("solo", "unknown-session", end=targets.ExpiresAt(LATER)), "blocked by solo until 15:10 UTC"),
            (targets.Unreachable("probe failed"), "unreachable, probe failed"),
            (targets.StateUnreadable("block.json: bad"), "state unreadable, block.json: bad"),
            (targets.Free(), ""),
        )
        for state, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.routes(state).holds("/fixture/hana"), expected)
        unreadable = target_availability.MacRoutes(target_availability.SnapshotsUnreadable("targets.conf: missing"), {}, UTC)
        self.assertEqual(unreadable.holds("/r"), "state unreadable, targets.conf: missing")
        self.assertEqual(target_availability.MacRoutes((snapshot(NATEDEV, targets.Free()),), {}, UTC).holds("/r"), "")

    def test_a_busy_mac_holds_a_repository_only_with_its_own_claims(self) -> None:
        cargo_liner = claim_state.RepositoryScope("cargo-liner", "github.com/natepiano/cargo-liner")
        hana = claim_state.RepositoryScope("hana", "github.com/natepiano/hana")
        organon = claim_state.RepositoryScope("organon", "github.com/natepiano/organon")
        claims = (claim(hana, "nextest hana"), claim(cargo_liner, "check cargo-handler"))
        scopes = {"/fixture/cargo-liner-tile": cargo_liner, "/fixture/organon": organon}
        job = targets.RunningJob("nextest hana", SINCE, targets.NotSeenIdle())
        routes = target_availability.MacRoutes(
            (snapshot(MAC, targets.Running((job,)), claims=claims), snapshot(NATEDEV, targets.Free())), LABELS, UTC)
        with mock.patch.object(mac_test, "run_is_live", return_value=True), \
             mock.patch.object(mac_test, "target_claim_scope", side_effect=scope_of(scopes)) as scope:
            self.assertEqual(routes.holds("/fixture/cargo-liner-tile"), "busy with check cargo-handler")
            self.assertEqual(routes.holds("/fixture/cargo-liner-tile"), "busy with check cargo-handler")
            self.assertEqual(routes.holds("/fixture/organon"), "")
        self.assertEqual(scope.call_count, 2)

    def test_a_whole_mac_claim_or_an_unknown_repository_holds_with_every_live_claim(self) -> None:
        cargo_liner = claim_state.RepositoryScope("cargo-liner")
        claims = (claim(claim_state.WHOLE_MAC, "mac setup"), claim(cargo_liner, "check cargo-handler"),
                  claim(claim_state.RepositoryScope("hana"), "nextest hana"))
        job = targets.RunningJob("mac setup", SINCE, targets.NotSeenIdle())
        routes = target_availability.MacRoutes(
            (snapshot(MAC, targets.Running((job,)), claims=claims), snapshot(NATEDEV, targets.Free())), LABELS, UTC)
        scopes = {"/fixture/cargo-liner": cargo_liner, "/fixture/outside": claim_state.WHOLE_MAC}
        with mock.patch.object(mac_test, "run_is_live", side_effect=live_unless("nextest hana")), \
             mock.patch.object(mac_test, "target_claim_scope", side_effect=scope_of(scopes)):
            self.assertEqual(routes.holds("/fixture/cargo-liner"), "busy with mac setup; check cargo-handler")
            self.assertEqual(routes.holds("/fixture/outside"), "busy with mac setup; check cargo-handler")

    def test_each_mac_state_gives_its_fit(self) -> None:
        job = targets.RunningJob("nextest hana", SINCE, targets.NotSeenIdle())
        Allowed, Unreadable = target_availability.Allowed, target_availability.FitUnreadable
        cases: tuple[tuple[targets.TargetState, target_availability.TargetFit], ...] = (
            (MAC_BLOCK, Allowed("blocked by research (hana) awaiting unblock; requested expiry was 15:10 UTC")),
            (held("solo", "unknown-session", end=targets.ExpiresAt(LATER)), Allowed("blocked by solo until 15:10 UTC")),
            (targets.Running((job, job)), Allowed("busy with nextest hana; nextest hana")),
            (targets.Unreachable("probe failed"), Allowed("unreachable, probe failed")),
            (targets.StateUnreadable("block.json: bad"), Unreadable("Mac state unreadable, block.json: bad")),
            (targets.Free(), Allowed("no block or claim recorded")),
        )
        for state, expected in cases:
            with self.subTest(expected=expected), \
                 mock.patch.object(build_targets, "step_policy", return_value=eligibility.KnownRepository("hana", "/r")):
                routes = self.routes(state)
                self.assertEqual(routes.fit("nextest", "/fixture/hana", "hana"), expected)
                self.assertEqual(routes.blocked(), isinstance(state, targets.Held))

    def test_kind_and_routing_rule_come_before_target_state(self) -> None:
        routes = self.routes(MAC_BLOCK)
        with mock.patch.object(build_targets, "step_policy", side_effect=AssertionError("no policy read")):
            self.assertEqual(routes.fit("fmt", "/fixture/hana", "hana"),
                             target_availability.NotAllowed("this step kind does not run on the Mac"))
            self.assertEqual(routes.fit("build", "/fixture/hana", "hana"),
                             target_availability.NotAllowed("makes natedev artifacts"))

    def test_policy_exclusion_names_its_reason_and_reads_once_for_each_key(self) -> None:
        routes = self.routes(targets.Free())
        excluded = ExcludedTarget(MAC, "linux_only")
        linux_only = target_availability.NotAllowed("this package runs only on Linux")
        with mock.patch.object(build_targets, "step_policy", return_value=excluded) as policy:
            self.assertEqual(routes.fit("nextest", "/fixture/hana", "hana"), linux_only)
            self.assertEqual(routes.fit("nextest", "/fixture/hana", "hana"), linux_only)
            self.assertEqual(routes.fit("nextest", "/fixture/hana", PackageNotRecorded()),
                             target_availability.NotAllowed("this package runs only on Linux; package not recorded"))
            self.assertEqual(routes.fit("nextest", "/fixture/hana", ""), linux_only)
        self.assertEqual(policy.call_count, 3)
        with mock.patch.object(build_targets, "step_policy", return_value=ExcludedTarget(MAC, "disk")):
            self.assertEqual(self.routes(targets.Free()).fit("check", "/fixture/hana", "hana"),
                             target_availability.NotAllowed("disk"))

    def test_dynamic_exclusions_leave_the_state(self) -> None:
        for reason in sorted(target_availability.DYNAMIC_REASONS):
            with self.subTest(reason=reason), \
                 mock.patch.object(build_targets, "step_policy", return_value=ExcludedTarget(MAC, reason)):
                self.assertEqual(self.routes(targets.Unreachable("retry backoff")).fit("check", "/fixture/hana", "hana"),
                                 target_availability.Allowed("unreachable, retry backoff"))

    def test_a_policy_read_error_shows_as_unreadable(self) -> None:
        with mock.patch.object(build_targets, "step_policy", side_effect=ConfigError("offload.conf: bad")):
            self.assertEqual(self.routes(targets.Free()).fit("check", "/fixture/hana", "hana"),
                             target_availability.FitUnreadable("Mac state unreadable, offload.conf: bad"))

    def test_unreadable_or_absent_mac_target(self) -> None:
        unreadable = target_availability.MacRoutes(target_availability.SnapshotsUnreadable("targets.conf: missing"), {}, UTC)
        self.assertEqual(unreadable.fit("nextest", "/r", "p"),
                         target_availability.FitUnreadable("Mac state unreadable, targets.conf: missing"))
        self.assertFalse(unreadable.blocked())
        absent = target_availability.MacRoutes((snapshot(NATEDEV, targets.Free()),), {}, UTC)
        self.assertEqual(absent.fit("nextest", "/r", "p"), target_availability.NoMacTarget())
        self.assertFalse(absent.blocked())


if __name__ == "__main__":
    _ = unittest.main()
