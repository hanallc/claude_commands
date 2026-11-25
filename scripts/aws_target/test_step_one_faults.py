"""Check that shared Hana and GPU reads preserve request checks."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from inspect import signature
from typing import cast
import unittest
from unittest import mock

from scripts.aws_target import hana_placement as hana, hana_placement_adapter as adapter, hana_spend
from scripts.aws_target import test_hana_placement as fixtures
from scripts.aws_target.ledger_test_support import text_setting
from scripts.build_queue import admission_protocol as wire, admission_records as records, placement
from scripts.build_targets.execution_transport import HanaGpuNextestTransport
from scripts.lib import conf, process_start
from scripts.mac_test import claim_state, mac_test


class HanaEvidenceFaultTests(unittest.TestCase):
    def fixture(self) -> fixtures.HanaPlacementTests:
        fixture = fixtures.HanaPlacementTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def evidence(self, fixture: fixtures.HanaPlacementTests, request: wire.PlacementRequest
                 ) -> placement.HostEvidence:
        reader: object = getattr(adapter, "read_hana_target", None)
        # Main checks one request before it reads state. The split reads shared state first.
        if reader is None:
            return fixture.evidence(request)
        read = cast(Callable[..., object], reader)
        check = cast(Callable[[object, placement.HostEvidenceKey, wire.PlacementRequest, str],
                              placement.HostEvidence], getattr(adapter, "check_hana_run"))
        destination = fixture.assignment.destination
        key = placement.HostEvidenceKey(destination, request.constraints.session, request.constraints.command,
            "fixture", str(fixture.root), "hana", request.profile.step, command_kind="check")
        workload = conf.read(fixture.workload)
        route = adapter.read_settings(workload)
        assert isinstance(route, hana.EnabledHanaRoute), route
        transport = hana.HanaControlTransport(fixture.workload, destination.host)
        options = {"runs": ((key, request, fixtures.KEY),)} if "runs" in signature(read).parameters else {}
        with mock.patch("time.time", fixture.clock), mock.patch("time.monotonic", fixture.clock):
            capacity = read(transport, workload, route, destination.target, fixture.clock.now, **options)
            return check(capacity, key, request, fixtures.KEY)

    def test_blocked_stopped_hana_skips_the_spending_preview(self) -> None:
        """Hana read_evidence row 43. A blocked request must skip spending."""
        fixture = self.fixture()
        paths = claim_state.state_paths(fixture.assignment.destination.target)
        paths.directory.mkdir(parents=True)
        since = datetime.fromtimestamp(fixture.clock.now, UTC)
        mac_test.write_block(paths.block, mac_test.ActiveMacBlock(
            "fixture", "private block", since, since + timedelta(hours=1),
            mac_test.RecordedSession("other-session", "fixture"), mac_test.NO_SHOWRUNNER,
            "off_before", mac_test.FREE_MESSAGE_NOT_NEEDED))
        with mock.patch.object(hana_spend, "preview_placement_start", wraps=hana_spend.preview_placement_start) as preview:
            result = self.evidence(fixture, fixture.request())
        assert isinstance(result, placement.FreshHostEvidence), result
        self.assertEqual(result.eligibility, placement.TemporaryExclusion("blocked"))
        preview.assert_not_called()

    def test_refused_hana_command_skips_the_held_claim_lock(self) -> None:
        """Hana read_evidence row 4. A command refusal must skip the claim lock."""
        fixture = self.fixture()
        request = fixture.request(command=("cargo", "check", "--fix"))
        paths = claim_state.state_paths(fixture.assignment.destination.target)
        with claim_state.locked(paths.lock), mock.patch.object(
            claim_state, "locked", side_effect=AssertionError("A refused Hana command tried to take the claim lock")
        ) as lock:
            result = self.evidence(fixture, request)
        assert isinstance(result, placement.FreshHostEvidence), result
        self.assertEqual(result.eligibility, placement.FixedExclusion("command"))
        lock.assert_not_called()


class GpuEvidenceFaultTests(unittest.TestCase):
    def fixture(self) -> fixtures.HanaGpuPlacementTests:
        fixture = fixtures.HanaGpuPlacementTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def evidence(self, fixture: fixtures.HanaGpuPlacementTests, *, pinned: bool) -> placement.FreshHostEvidence:
        assignment = fixture.assignment
        destination = assignment.destination
        key = placement.HostEvidenceKey(destination, assignment.session, fixture.command, "fixture",
                                        package=fixture.package, command_kind="test")
        selection = placement.PinnedHost(destination.target) if pinned else placement.ConfiguredOrder()
        constraints = placement.PlacementConstraints(assignment.source_key, fixture.command, str(fixture.root),
            fixture.package, assignment.session, (placement.PlacementCandidate(destination),), selection,
            command_kind="test")
        profile = records.StepProfile(fixture.package, "nextest", "source", records.ExpectedPeak(1024, "fallback", 0))
        request = wire.PlacementRequest(assignment.episode, assignment.owner, 0, profile, constraints)
        transport = HanaGpuNextestTransport(fixture.gpu_settings, destination.host)
        settings = conf.read(fixture.gpu_settings)
        reader: object = getattr(adapter, "read_hana_gpu_target", None)
        if reader is None:
            return adapter.read_gpu_evidence(transport, key, request, configuration=settings)
        read = cast(Callable[..., object], reader)
        check = cast(Callable[[object, placement.HostEvidenceKey, wire.PlacementRequest, str],
                              placement.FreshHostEvidence], getattr(adapter, "check_hana_gpu_run"))
        capacity = read(transport, destination.target, configuration=settings)
        return check(capacity, key, request, fixtures.KEY)

    def test_damaged_gpu_block_preserves_the_unpinned_refusal(self) -> None:
        """GPU read_gpu_evidence row 13. A damaged block must preserve the fixed refusal."""
        fixture = self.fixture()
        paths = claim_state.state_paths(fixture.assignment.destination.target)
        paths.directory.mkdir(parents=True)
        _ = paths.block.write_text("{")
        try:
            result = self.evidence(fixture, pinned=False)
        except claim_state.StateReadError as error:
            self.fail(f"The damaged GPU block masked the fixed refusal: {error}")
        self.assertEqual(result.eligibility, placement.FixedExclusion("This target requires --target hana-gpu"))

    def test_invalid_gpu_selections_preserve_a_dead_owners_claim(self) -> None:
        """GPU gpu_step row 3, 5 and GPU read_gpu_evidence row 1, 5. A settings error must preserve the claim."""
        fixture = self.fixture()
        text_setting(fixture.gpu_settings, "hana_gpu_selections", "")
        assignment = fixture.assignment
        paths = claim_state.state_paths(assignment.destination.target)
        paths.directory.mkdir(parents=True)
        mac_test.write_json(paths.run, {
            "claim_id": "fixture-claim", "episode": assignment.episode, "attempt": assignment.attempt,
            "submitting_session": "", "pid": assignment.owner.pid, "proc_start": assignment.owner.start_time,
            "remote_host": "fixture-gpu", "what": "fixture nextest", "worktree": str(fixture.root),
            "since": datetime.now(UTC).isoformat(),
        })
        before = paths.run.read_bytes()
        with (mock.patch.object(conf, "_committed_files", return_value=()),
              mock.patch.object(process_start, "process_start", return_value=process_start.Gone()),
              self.assertRaisesRegex(conf.Unusable, "hana_gpu_selections")):
            _ = self.evidence(fixture, pinned=True)
        self.assertTrue(paths.run.is_file(), "The GPU settings error deleted the saved claim")
        self.assertEqual(paths.run.read_bytes(), before)
