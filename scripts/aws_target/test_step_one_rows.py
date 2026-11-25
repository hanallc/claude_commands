"""Check Hana and GPU placement rows with private provider state."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from functools import cached_property, partial
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.aws_target import gpu_size_trial as trial, hana_box, hana_execution, hana_image
from scripts.aws_target import hana_placement as hana, hana_placement_adapter as adapter, hana_render, hana_spend
from scripts.aws_target import ledger, remote, test_hana_execution, test_hana_placement
from scripts.aws_target.controller_lock import control_lock
from scripts.build_queue import execution_identity, placement
from scripts.build_targets import target_config
from scripts.lib import conf, file_lock, process_start
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import claim_state
from scripts.production import gate


class HanaClaimRows(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: test_hana_placement.HanaPlacementTests = test_hana_placement.HanaPlacementTests()

    @override
    def setUp(self) -> None:
        _ = self.enterContext(mock.patch.object(process_start, "process_start",
                                                return_value=process_start.Started("fixture-holder")))
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    @property
    def assignment(self) -> placement.PlacementAssignment:
        return self.fixture.assignment

    @property
    def holder(self) -> placement.ClaimHolderIdentity:
        return self.fixture.holder

    @property
    def controller(self) -> hana_box.HanaController:
        return self.fixture.controller

    @property
    def transport(self) -> hana.HanaControlTransport:
        return hana.HanaControlTransport(self.fixture.workload, self.assignment.destination.host)

    def test_assignment_reads_start_only_for_a_valid_local_pid(self) -> None:
        """Hana validate_assignment row 6."""
        host = replace(self.holder, host="remote-host")
        invalid_pid = replace(self.holder, pid=0)
        with mock.patch.object(process_start, "process_start", return_value=process_start.Started("stale")) as start:
            for holder in (host, invalid_pid, self.holder):
                self.assertEqual(adapter.validate_assignment(self.assignment, holder),
                                 hana.HanaClaimRefused("The local claim holder is not current"))
        start.assert_called_once_with(self.holder.pid)

    def test_valid_assignment_returns_the_configured_transport(self) -> None:
        """Hana validate_assignment row 7."""
        self.assertEqual(adapter.validate_assignment(self.assignment, self.holder), self.transport)

    def test_claim_holds_state_lock_when_reading_claims(self) -> None:
        """Hana claim_assigned row 3."""
        path = claim_state.state_paths(self.assignment.destination.target).lock
        read = claim_state.read_claims

        def read_locked(paths: claim_state.StatePaths) -> tuple[claim_state.HeldClaim, ...]:
            with file_lock.exclusive(path, wait=False) as lock:
                self.assertIs(lock, file_lock.LockResult.BUSY)
            return read(paths)

        with mock.patch.object(claim_state, "read_claims", side_effect=read_locked) as claims:
            self.assertIsInstance(adapter.claim_assigned(self.assignment, self.holder, self.controller),
                                  hana.HanaClaimPending)
        claims.assert_called_once()
        with file_lock.exclusive(path, wait=False) as lock:
            self.assertIs(lock, file_lock.LockResult.ACQUIRED)

    def test_adoption_refuses_wrong_holder_and_pending_cleanup(self) -> None:
        """Hana read_assigned_adoption row 5."""
        adopted = self.fixture.ready()
        self.assertEqual(adapter.read_assigned_adoption(self.assignment, self.holder, self.transport),
                         adopted.reserved.claim)
        wrong_holder = replace(self.holder, start_time="another-start")
        refusal = hana.HanaClaimRefused("the exact claim is not adopted")
        self.assertEqual(adapter.read_assigned_adoption(self.assignment, wrong_holder, self.transport), refusal)
        saved, = claim_state.read_claims(claim_state.state_paths(self.assignment.destination.target))
        _ = saved.path.write_text(json.dumps({**saved.record, "cleanup_pending": True}))
        self.assertEqual(adapter.read_assigned_adoption(self.assignment, self.holder, self.transport), refusal)

    def settled_command(self) -> None:
        reference = placement.assigned_claim_reference(self.assignment)
        result = hana_execution.settle_unpublished_claim(self.controller.directory, reference,
            release_claim=hana.HanaClaimReleased, reason="The fixture did not launch a command")
        self.assertIsInstance(result, hana_execution.HanaSettled)

    def test_release_reads_the_claim_under_state_lock(self) -> None:
        """Hana release_assigned row 5."""
        _ = self.fixture.ready()
        self.settled_command()
        path = claim_state.state_paths(self.assignment.destination.target).lock
        read = claim_state.read_claims

        def read_locked(paths: claim_state.StatePaths) -> tuple[claim_state.HeldClaim, ...]:
            with file_lock.exclusive(path, wait=False) as lock:
                self.assertIs(lock, file_lock.LockResult.BUSY)
            return read(paths)

        with mock.patch.object(claim_state, "read_claims", side_effect=read_locked) as claims:
            self.assertEqual(adapter.release_assigned(self.assignment, self.holder, self.controller),
                             hana.HanaClaimReleased())
        claims.assert_called_once()

    def test_release_saves_lifecycle_and_deletes_only_the_matching_claim(self) -> None:
        """Hana release_assigned row 8."""
        _ = self.fixture.ready()
        self.settled_command()
        paths = claim_state.state_paths(self.assignment.destination.target)
        saved, = claim_state.read_claims(paths)
        lifecycle = self.controller.path.read_bytes()
        unrelated = saved.path.with_name("unrelated.txt")
        _ = unrelated.write_text("keep")
        check = hana_execution.execution_command_settled
        with mock.patch.object(hana_execution, "execution_command_settled", wraps=check) as guard:
            self.assertEqual(adapter.release_assigned(self.assignment, self.holder, self.controller),
                             hana.HanaClaimReleased())
        self.assertEqual(guard.call_count, 2)
        self.assertNotEqual(self.controller.path.read_bytes(), lifecycle)
        self.assertFalse(saved.path.exists())
        self.assertEqual(unrelated.read_text(), "keep")
        result = self.controller.read_saved_lifecycle()
        assert isinstance(result, hana_box.HanaLifecycleEvidence)
        self.assertEqual(result.claims, ())

    def test_recovery_reads_target_claims_under_state_lock(self) -> None:
        """Hana recover_assigned_execution row 3."""
        path = claim_state.state_paths(self.assignment.destination.target).lock
        read = claim_state.read_claims

        def read_locked(paths: claim_state.StatePaths) -> tuple[claim_state.HeldClaim, ...]:
            with file_lock.exclusive(path, wait=False) as lock:
                self.assertIs(lock, file_lock.LockResult.BUSY)
            return read(paths)

        with (mock.patch.object(process_start, "process_start", return_value=process_start.Gone()),
              mock.patch.object(claim_state, "read_claims", side_effect=read_locked) as claims):
            result = adapter.recover_assigned_execution(self.assignment, self.holder, self.controller)
        claims.assert_called_once()
        self.assertEqual(result, hana_execution.HanaSettlementPending("The assigned Hana dispatch journal is unavailable"))

    def test_recovery_without_claim_reads_lifecycle_under_controller_lock(self) -> None:
        """Hana recover_assigned_execution row 5."""
        path = self.controller.directory / "controller.lock"
        read = self.controller.read_saved_lifecycle

        def read_locked() -> hana_box.HanaLifecycleEvidence | hana.HanaUnprovisioned:
            with file_lock.exclusive(path, wait=False) as lock:
                self.assertIs(lock, file_lock.LockResult.BUSY)
            return read()

        with (mock.patch.object(process_start, "process_start", return_value=process_start.Gone()),
              mock.patch.object(self.controller, "read_saved_lifecycle", side_effect=read_locked) as lifecycle):
            result = adapter.recover_assigned_execution(self.assignment, self.holder, self.controller)
        lifecycle.assert_called_once()
        self.assertIsInstance(result, hana_execution.HanaSettlementPending)

    def test_recovery_checks_observation_and_the_exact_boot(self) -> None:
        """Hana recover_assigned_execution row 11."""
        dispatch = test_hana_execution.HanaExecutionTests()
        dispatch.root = self.controller.directory
        assignment = replace(self.assignment, episode=dispatch.key.episode, attempt=dispatch.key.attempt,
                             source_key=cast(execution_identity.SourceKeyKnown, dispatch.source_key))
        self.fixture.assignment = assignment
        _ = dispatch.dispatch()
        with mock.patch.object(process_start, "process_start", return_value=process_start.Gone()):
            missing = adapter.recover_assigned_execution(assignment, self.holder, self.controller)
            self.assertEqual(missing, hana_execution.HanaSettlementPending(
                "The Hana journal has no observation for this boot"))
            _ = hana_execution.observe_execution(dispatch.root, dispatch.request, dispatch.objects)
            missing_claim = adapter.recover_assigned_execution(assignment, self.holder, self.controller)
            self.assertEqual(missing_claim, hana_execution.HanaSettlementPending(
                "The Hana claim is missing before command settlement"))

        _ = self.fixture.ready()
        paths = claim_state.state_paths(assignment.destination.target)
        claim, = claim_state.read_claims(paths)
        with mock.patch.object(process_start, "process_start", return_value=process_start.Gone()):
            wrong_boot = adapter.recover_assigned_execution(assignment, self.holder, self.controller)
            self.assertEqual(wrong_boot, hana_execution.HanaSettlementPending("The Hana claim belongs to a different boot"))
            record = {**claim.record, "instance_generation": hana.generation_text(dispatch.key.generation)}
            _ = claim.path.write_text(json.dumps(record))
            dispatch.objects.fenced = True
            dispatch.objects.terminated = True
            dispatch.objects.lease_free = True
            _ = hana_execution.observe_execution(dispatch.root, dispatch.request, dispatch.objects)
            settled = adapter.recover_assigned_execution(assignment, self.holder, self.controller)
            self.assertIsInstance(settled, hana_execution.HanaSettled)
            self.assertEqual(adapter.recover_assigned_execution(assignment, self.holder, self.controller), settled)
        self.assertEqual(claim_state.read_claims(paths), ())

    def test_gpu_claim_pending_reads_the_registered_target_and_exact_claim(self) -> None:
        """GPU gpu_claim_pending row 1."""
        adopted = self.fixture.ready()
        claim_id = adopted.reserved.claim.reference.claim_id
        target = self.assignment.destination.target
        paths = claim_state.state_paths(target)
        with (mock.patch.object(target_config, "configured_ssh_target",
                                wraps=target_config.configured_ssh_target) as registry,
              mock.patch.object(claim_state, "read_run", wraps=claim_state.read_run) as read):
            self.assertTrue(adapter.gpu_claim_pending(target, claim_id))
            self.assertFalse(adapter.gpu_claim_pending(target, "different-claim"))
        self.assertEqual(registry.call_args_list, [mock.call(target), mock.call(target)])
        self.assertTrue(any(cast(Path, call.args[0]).is_relative_to(paths.directory) for call in read.call_args_list))


class HanaImageRows(unittest.TestCase):
    root: Path = Path()
    config: Path = Path()
    targets: Path = Path()
    folders: Path = Path()
    source: Path = Path()
    manifest: Path = Path()
    deployment: Path = Path()
    setup: Path = Path()
    digest: hana_render.GuestDigest = hana_render.GuestDigest("")

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "config/hana_aws.conf"
        self.config.parent.mkdir()
        image_values = {
            "guard_grace_seconds": 60, "watchdog_check_seconds": 1, "permit_max_seconds": 1800,
            "step_cpu_limit": 4, "step_memory_gib": 24, "mount_timeout_seconds": 15, "aws_timeout_s": 10,
            "control_poll_seconds": 1, "result_transfer_timeout_s": 120, "source_max_bytes": 1048576,
            "source_download_timeout_s": 60,
        }
        _ = self.config.write_text("host=fixture.example\nenabled=on\n" + "".join(
            f"# whole: 1..\n{key}={value}\n" for key, value in image_values.items()))
        self.targets = self.config.with_name("build_targets.conf")
        _ = self.targets.write_text("# range: 1..\nloopback.command_timeout_s=30\n"
                                    + "# range: 1..\nloopback.command_timeout_s.fixture=29\n")
        self.folders = self.config.with_name("build_folders.conf")
        _ = self.folders.write_text(
            "roots.build=~/fixture\n# range: 0..\nlocks.probe_wait_seconds=0.25\n"
            + "# range: 0..\nlocks.probe_retry_seconds=0.005\n")
        self.source = self.root / "scripts/fixture.py"
        self.manifest = self.root / hana_render.IMAGE_FILE_LIST
        self.manifest.parent.mkdir(parents=True)
        _ = self.source.write_bytes(b"fixture = True\n")
        _ = self.manifest.write_text("scripts/fixture.py\n")
        self.deployment = self.root / "state"
        self.setup = self.deployment / "hana/setup.json"
        self.setup.parent.mkdir(parents=True)
        render = partial(hana_render.guest_digest, checkout=self.root)
        self.digest = render(self.config)
        _ = self.setup.write_text(json.dumps({"deployment": "deployment", "instance": "instance",
                                              "guest_digest": self.digest.value}))
        _ = self.enterContext(mock.patch.object(hana_render, "guest_digest", wraps=render))

    def current(self, config: Path | conf.ConfigRead) -> bool:
        return hana_image.deployed_image_is_current(self.deployment, "deployment", "instance", config)

    def test_image_reads_workload_only_for_a_path_and_selects_host_keys(self) -> None:
        """Hana image check row 3."""
        loaded = conf.read(self.config)
        with mock.patch.object(conf, "read", wraps=conf.read) as read:
            self.assertTrue(self.current(loaded))
            read.assert_not_called()
            self.assertTrue(self.current(self.config))
            read.assert_called_once_with(self.config)
        with self.config.open("a") as stream:
            _ = stream.write("# whole: 1..\nresult_transfer_timeout_s.other=61\n")
        self.assertTrue(self.current(self.config))
        with self.config.open("a") as stream:
            _ = stream.write("# whole: 1..\nresult_transfer_timeout_s.fixture=61\n")
        self.assertFalse(self.current(self.config))

    def test_image_reads_the_checkout_command_timeout(self) -> None:
        """Hana image check row 4."""
        loaded = conf.read(self.config)
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read:
            self.assertTrue(self.current(loaded))
            self.assertEqual(sum(call.args[0] == self.targets for call in read.call_args_list), 1)
        _ = self.targets.write_text(self.targets.read_text().replace("fixture=29", "fixture=31"))
        self.assertFalse(self.current(loaded))

    def test_image_reads_and_validates_the_build_folder_values(self) -> None:
        """Hana image check row 5."""
        loaded = conf.read(self.config)
        initial = self.folders.read_text()
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read:
            self.assertTrue(self.current(loaded))
            self.assertEqual(sum(call.args[0] == self.folders for call in read.call_args_list), 1)
        for original, invalid in (("~/fixture", "relative"), ("0.25", "-1"), ("0.005", "0")):
            with self.subTest(original=original):
                _ = self.folders.write_text(initial.replace(original, invalid))
                self.assertFalse(self.current(loaded))

    def test_image_renders_guard_and_receiver_settings(self) -> None:
        """Hana image check row 6."""
        from scripts.aws_target import hana_guest, hana_receiver

        with (mock.patch.object(hana_guest, "read_settings", wraps=hana_guest.read_settings) as guard,
              mock.patch.object(hana_receiver, "read_settings", wraps=hana_receiver.read_settings) as receiver):
            self.assertTrue(self.current(self.config))
        guard.assert_called_once()
        receiver.assert_called_once()
        arguments = cast(tuple[conf.ConfigRead], guard.call_args.args)
        self.assertEqual(arguments[0].whole("step_cpu_limit"), 4)
        self.assertEqual(receiver.call_args.kwargs["command_timeout_s"], 29)

    def test_image_reads_each_listed_source_and_rejects_invalid_entries(self) -> None:
        """Hana image check row 7."""
        loaded = conf.read(self.config)
        with (mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as text,
              mock.patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as data):
            self.assertTrue(self.current(loaded))
        self.assertEqual(sum(call.args[0] == self.manifest for call in text.call_args_list), 1)
        data.assert_called_once_with(self.source)
        _ = self.manifest.write_text("../outside.py\n")
        self.assertFalse(self.current(loaded))

    def test_image_digest_comparison_refuses_changed_or_unreadable_inputs(self) -> None:
        """Hana image check row 8."""
        self.assertTrue(self.current(self.config))
        _ = self.setup.write_text(json.dumps({"deployment": "deployment", "instance": "instance",
                                              "guest_digest": "b" * 64}))
        self.assertFalse(self.current(self.config))
        _ = self.setup.write_text(json.dumps({"deployment": "deployment", "instance": "instance",
                                              "guest_digest": self.digest.value}))
        self.targets.unlink()
        self.assertFalse(self.current(self.config))
        _ = self.targets.write_text("loopback.command_timeout_s=invalid\n")
        self.assertFalse(self.current(self.config))


class HanaPricingRows(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: test_hana_placement.HanaPlacementTests = test_hana_placement.HanaPlacementTests()

    @override
    def setUp(self) -> None:
        _ = self.enterContext(mock.patch.object(process_start, "process_start",
                                                return_value=process_start.Started("fixture-holder")))
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    @cached_property
    def book(self) -> ledger.CostLedger:
        return ledger.CostLedger(self.fixture.state)

    @cached_property
    def spending(self) -> hana_spend.HanaSpending[hana_spend.PermitSettings]:
        settings = hana_spend.read_settings(conf.read(self.fixture.workload))
        return hana_spend.HanaSpending(self.book, self.fixture.settings, settings)

    @property
    def now(self) -> int:
        return self.fixture.clock.now

    def test_preview_reads_start_journal_only_for_closed_unobserved_compute(self) -> None:
        """Hana preview pricing helpers row 1."""
        journal = remote.read_hana_start_journal
        lock_path = self.book.directory / "cost-ledger.lock"

        def read_locked(directory: Path) -> remote.HanaStartJournal:
            with file_lock.exclusive(lock_path, wait=False) as lock:
                self.assertIs(lock, file_lock.LockResult.BUSY)
            return journal(directory)

        for observed, count in ((True, 0), (False, 1)):
            with self.book.transaction() as state:
                state.hana_compute = [ledger.HanaComputeExposure(
                    self.now - 60, self.now + 60, 1, Decimal("0.35"), ledger.HanaExposureClosed(self.now),
                    "m7a.4xlarge", region="us-east-2", observed_active=observed)]
            with mock.patch.object(remote, "read_hana_start_journal", side_effect=read_locked) as read:
                self.assertIsInstance(self.spending.preview_start(self.now, self.now + 60), hana_spend.StartAffordable)
            self.assertEqual(read.call_count, count)

    def test_preview_uses_provisioned_type_for_in_memory_exposure(self) -> None:
        """Hana preview pricing helpers row 2."""
        setup = self.book.directory / "hana/setup.json"
        _ = setup.write_text(json.dumps({"instance": "fixture", "instance_type": "m7i.4xlarge"}))
        before = self.book.path.read_bytes()
        assess = self.book.placement_spending_preview
        with (mock.patch.object(remote, "read_record", wraps=remote.read_record) as read,
              mock.patch.object(self.book, "placement_spending_preview", wraps=assess) as preview):
            self.assertIsInstance(self.spending.preview_start(self.now, self.now + 60), hana_spend.StartAffordable)
        self.assertEqual(sum(call.args[0] == setup for call in read.call_args_list), 1)
        preview.assert_called_once()
        state = cast(ledger.LedgerEvidence, preview.call_args.args[0])
        compute, = state.hana_compute
        storage, = state.hana_storage
        self.assertEqual(compute.instance_type, "m7i.4xlarge")
        self.assertEqual(compute.expires, self.now + 60)
        self.assertIsInstance(storage.state, ledger.HanaExposureOpen)
        self.assertEqual(self.book.path.read_bytes(), before)

    def test_pricing_reads_complete_days_before_returning_period_totals(self) -> None:
        """Hana preview pricing helpers row 6."""
        state = self.book.read_snapshot()
        with mock.patch.object(self.book, "read_complete_day_amounts", wraps=self.book.read_complete_day_amounts) as read:
            result = self.book.placement_spending_preview(state, self.fixture.settings, self.now)
        read.assert_called_once()
        self.assertEqual(read.call_args.args[1], self.now)
        self.assertIsInstance(result, ledger.SpendingAllowed)
        assert isinstance(result, ledger.SpendingAllowed)
        costs = result.evidence.costs
        assert isinstance(costs, ledger.CostEstimate)
        self.assertEqual(costs.daily.usd, Decimal(0))
        self.assertEqual(costs.monthly.usd, Decimal(0))
        self.assertEqual(result.evidence.reserved_daily_fixed_usd, Decimal(0))
        self.assertEqual(result.evidence.reserved_monthly_fixed_usd, Decimal(0))

    def test_pricing_preview_reports_unavailable_input(self) -> None:
        """Hana preview pricing helpers row 8."""
        state = self.book.read_snapshot()
        for error in (OSError("fixture read failed"), ValueError("fixture value invalid"),
                      ArithmeticError("fixture calculation failed")):
            with (self.subTest(error=error),
                  mock.patch.object(self.book, "read_complete_day_amounts", side_effect=error)):
                result = self.book.placement_spending_preview(state, self.fixture.settings, self.now)
                self.assertIsInstance(result, ledger.SpendingUnavailable)
                assert isinstance(result, ledger.SpendingUnavailable)
                self.assertEqual(result.reason, str(error))


class GpuHandoffRows(unittest.TestCase):
    root: Path = Path()
    trials: Path = Path()
    trial_id: str = "1" * 32
    folder: Path = Path()
    request: trial.HanaGpuNextestRequest = trial.HanaGpuNextestRequest(("--workspace", "--test", "fixture"), ("g6.2xlarge",))

    @property
    def launch(self) -> trial.AssignedHanaGpuLaunch:
        return trial.AssignedHanaGpuLaunch(self.trial_id, lambda: None)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.trials = self.root / "trials"
        self.folder = self.trials / self.trial_id
        _ = self.enterContext(mock.patch.object(trial, "trial_root", return_value=self.trials))
        _ = self.enterContext(mock.patch.object(trial, "ssh_target_state_directory", return_value=self.root / "aws"))
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(self.root), "HOME": str(self.root)}))
        _ = cast(object, write_config_fixture(self.root / "cloud_gate.conf",
            'hana_gpu_instance_types=g6.2xlarge\nhana_gpu_spot_max_usd_per_hour={"g6.2xlarge":0.75}\n'
            + "gpu_size_trial_limit_usd=2\nhana_gpu_lifetime_minutes=10\ngpu_size_trial_box_extra_usd=0.10\n"))

    def test_assigned_trial_locks_root_and_refuses_an_existing_folder(self) -> None:
        """GPU assigned trial handoff row 2."""
        self.folder.mkdir(parents=True)
        existing = self.folder / "existing"
        _ = existing.write_text("keep")
        with (mock.patch.object(trial, "control_lock", wraps=control_lock) as lock,
              mock.patch.object(trial, "prepare_locked") as prepare,
              self.assertRaisesRegex(FileExistsError, "trial folder already exists")):
            _ = trial.run_nextest(self.root, "HEAD", self.request, launch=self.launch)
        lock.assert_called_once_with(self.trials)
        prepare.assert_not_called()
        self.assertEqual(existing.read_text(), "keep")

    def test_assigned_trial_keeps_its_lease_after_root_unlock(self) -> None:
        """GPU assigned trial handoff row 3."""
        from collections.abc import Generator
        from contextlib import contextmanager

        events = mock.Mock()
        stop = ValueError("stop at preparation")
        trial_lock = trial.trial_lock

        @contextmanager
        def check_acquisition(folder: Path, *, exclusive: bool = False) -> Generator[None]:
            if not exclusive:
                with file_lock.exclusive(self.trials / "controller.lock", wait=False) as lock:
                    self.assertIs(lock, file_lock.LockResult.BUSY, "Take the trial lease before the root unlock")
            with trial_lock(folder, exclusive=exclusive):
                yield

        def prepare(_repo: Path, _commit: str, folder: Path, _request: trial.HanaGpuNextestRequest) -> None:
            self.assertEqual(folder, self.folder)
            with file_lock.exclusive(self.trials / "controller.lock", wait=False) as lock:
                self.assertIs(lock, file_lock.LockResult.ACQUIRED)
            with self.assertRaises(BlockingIOError), trial.trial_lock(folder, exclusive=True):
                self.fail("The trial lease must remain held")
            raise stop

        with (mock.patch.object(trial, "control_lock", wraps=control_lock) as root,
              mock.patch.object(trial, "trial_lock", side_effect=check_acquisition) as lease,
              mock.patch.object(trial, "prepare_locked", side_effect=prepare),
              self.assertRaisesRegex(ValueError, "stop at preparation")):
            events.attach_mock(root, "root")
            events.attach_mock(lease, "lease")
            _ = trial.run_nextest(self.root, "HEAD", self.request, launch=self.launch)
        self.assertEqual([call[0] for call in events.mock_calls], ["root", "lease", "lease"])
        root.assert_called_once_with(self.trials)
        self.assertEqual(lease.call_args_list, [mock.call(self.folder), mock.call(self.folder, exclusive=True)])
        with trial.trial_lock(self.folder, exclusive=True):
            self.assertTrue(self.folder.is_dir())

    def test_assigned_trial_prepares_before_requesting_local_admission(self) -> None:
        """GPU assigned trial handoff row 4."""
        repo = self.root / "repo"
        repo.mkdir()
        _ = (repo / "fixture.txt").write_text("test source\n")
        for words in (("init", "-q"), ("add", "fixture.txt"),
                      ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                       "commit", "-qm", "Private source")):
            _ = subprocess.run(["git", *words], cwd=repo, check=True, capture_output=True,
                               env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

        checked = trial.checked

        def fake_tool(arguments: Sequence[str], cwd: Path, **_options: object) -> str:
            if arguments[0] == "git":
                return checked(arguments, cwd)
            self.assertTrue((self.folder / "prepare.log").is_file())
            self.assertEqual((cwd / "fixture.txt").read_text(), "test source\n")
            if "gpu-trial-preflight" in arguments:
                return f"/fixture/bin/{arguments[-1]}"
            return "fixture tool version"

        _ = (self.root / "aws_target.conf").write_text("# range: 1..\ngpu_size_trial_preflight_timeout_s=5\n")

        def service(folder: Path, _directory: Path, _settings: Path, _execute: object, *, assigned: bool) -> int:
            self.assertTrue(assigned)
            document = trial.read_json(folder / "request.json")
            self.assertEqual(document["commit"], commit)
            self.assertEqual(document["repo"], str(repo))
            tools = trial.mapping(document["tools"])
            self.assertEqual(tools["cargo-nextest"], "/fixture/bin/cargo-nextest")
            self.assertEqual(tools["nix"], "/fixture/bin/nix")
            self.assertNotIn("vulkaninfo", tools)
            self.assertEqual(trial.nextest_request(document), self.request)
            self.assertEqual(trial.mapping(document["limits"])["spot_max_usd_per_hour"], {"g6.2xlarge": "0.75"})
            _ = (folder / "run-error.json").write_text('{"reason":"fixture service stopped"}')
            return 1

        def find_tool(name: str, **_options: object) -> str:
            return f"/fixture/bin/{name}"

        with (mock.patch.object(shutil, "which", side_effect=find_tool),
              mock.patch.object(trial, "checked", side_effect=fake_tool),
              mock.patch.object(trial, "preflight", wraps=trial.preflight) as tools,
              mock.patch.object(gate.BuildHoldWait, "wait_until_free"),
              mock.patch.object(gate, "gate_reservation") as admission,
              mock.patch.object(trial, "start_service", side_effect=service),
              mock.patch.object(trial, "assigned_gpu_cleanup_confirmed", return_value=True),
              self.assertRaisesRegex(ValueError, "fixture service stopped")):
            admitted = cast(mock.MagicMock, admission.return_value)
            enter = cast(mock.MagicMock, admitted.__enter__)
            place = cast(mock.MagicMock, enter.return_value)
            process = cast(mock.MagicMock, cast(mock.MagicMock, place.popen).return_value)
            cast(mock.MagicMock, process.wait).return_value = 0
            _ = trial.run_nextest(repo, "HEAD", self.request, launch=self.launch)
        tools.assert_called_once()
        admission.assert_called_once()
        self.assertTrue((self.folder / "source.tar").exists())

    def test_assigned_trial_reports_saved_error_or_missing_result(self) -> None:
        """GPU assigned trial handoff row 12."""
        def prepare(_repo: Path, _commit: str, folder: Path, request: trial.HanaGpuNextestRequest) -> None:
            source = b"fixture source"
            _ = (folder / "source.tar").write_bytes(source)
            (folder / "cargo-home").mkdir()
            trial.write_json(folder / "manifest.json", {"schema": 2, "toolchain_source": "nix",
                "state": "prepared", "trial": folder.name, "commit": "a" * 40, "tree": "b" * 40,
                "hana_nextest": {"arguments": request.arguments, "instance_types": request.instance_types},
                "sha256": {"source.tar": sha256(source).hexdigest()}})

        results = ((self.trial_id, "worker diagnostic"), ("2" * 32, "exited with status 7 and has no result"))
        for trial_id, reason in results:
            def service(folder: Path, _directory: Path, _settings: Path, _execute: object, *, assigned: bool) -> int:
                self.assertTrue(assigned)
                if trial_id == self.trial_id:
                    _ = (folder / "run-error.json").write_text('{"reason":"worker diagnostic"}')
                receipt = trial.cleanup_receipt(folder)
                receipt.parent.mkdir(parents=True, exist_ok=True)
                _ = receipt.write_text(json.dumps({"trial": trial_id}))
                return 7

            launch = trial.AssignedHanaGpuLaunch(trial_id, lambda: None)
            with (self.subTest(trial=trial_id), mock.patch.object(trial, "prepare_locked", side_effect=prepare),
                  mock.patch.object(trial, "start_service", side_effect=service) as start,
                  self.assertRaisesRegex(ValueError, reason)):
                _ = trial.run_nextest(self.root, "HEAD", self.request, launch=launch)
            start.assert_called_once()
            output = self.trials / trial_id
            self.assertFalse((output / "online-result.json").exists())
            self.assertEqual((output / "source.tar").exists(), trial_id == self.trial_id)
            self.assertEqual((output / "cargo-home").exists(), trial_id == self.trial_id)
            self.assertEqual((output / "inputs-removed.json").exists(), trial_id != self.trial_id)
