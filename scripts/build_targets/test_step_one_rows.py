"""Check target registry, probe backoff, and eligibility results."""
from __future__ import annotations

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import time
from typing import cast, override
import unittest
from unittest import mock

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import placement
from scripts.build_targets import build_targets, target_config
from scripts.build_targets.test_targets import fixture_mac_mirror
from scripts.lib import conf
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE, write_config_fixture
from scripts.mac_test import claim_state, eligibility, offload


class StepOneRows(unittest.TestCase):
    root: Path = Path()
    registry: Path = Path()
    settings: Path = Path()
    registry_text: str = ""

    @property
    def mac(self) -> target_config.SshTarget:
        return target_config.SshTarget("mac", "macos", self.settings)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = self.root / "build_targets.conf"
        self.settings = self.root / "mac_test.conf"
        self.registry_text = (
            "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n" + UNREACHABLE_WAIT_FIXTURE)
        _ = self.registry.write_text(self.registry_text)
        _ = cast(object, write_config_fixture(self.settings, "host=fixture-mac\noffload=on\nfree_floor_gib=60\nlinux_only.fixture=\n"))
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(self.root),
            "HOME": str(self.root), "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "BUILD_TARGETS_STATE_DIR": str(self.root / "targets")}))
        _ = self.enterContext(mock.patch.object(eligibility, "repository_identity",
                                               return_value=eligibility.KnownRepository("fixture", "key")))
        _ = self.enterContext(mock.patch.object(claim_state, "mac_repository_identity",
            return_value=fixture_mac_mirror(self.root, "fixture", "key")))

    def disk_result(self, request: build_targets.RequestMacDiskRecovery) -> target_config.EligibleTarget | target_config.ExcludedTarget:
        return build_targets.mac_target_eligibility(self.mac, self.root, "fixture", "check",
            target_config.ANY_ELIGIBLE_TARGET, answer="free", session=NoOwningSession(),
            configuration=conf.read(self.settings), request_disk_recovery=request)

    def test_recovery_requests_follow_the_disk_read(self) -> None:
        """macOS target eligibility row 17."""
        requests: list[build_targets.MacDiskRecoveryRequest] = []

        def record(host: str, request: build_targets.MacDiskRecoveryRequest) -> build_targets.MacDiskRecoveryInactive:
            self.assertEqual(host, "fixture-mac")
            requests.append(request)
            return build_targets.MacDiskRecoveryInactive()

        with (mock.patch.object(offload, "probe_mac", side_effect=(
                    offload.ProbeSnapshot(59, "fixture-mac"), offload.ProbeSnapshot(60, "fixture-mac"))),
              mock.patch.object(offload, "mac_disk_floor", wraps=offload.mac_disk_floor) as read):
            self.assertEqual(self.disk_result(record), target_config.ExcludedTarget(self.mac, "disk"))
            self.assertEqual(self.disk_result(record), target_config.EligibleTarget(self.mac))
        self.assertEqual(read.call_count, 2)
        short, ready = requests
        assert isinstance(short, build_targets.MacDiskSweep)
        self.assertEqual(short.disk, offload.MacDiskShort(59, 60))
        self.assertEqual(ready, build_targets.MacDiskRecoveryQuery())

    def test_disk_recovery_maps_active_ready_and_short(self) -> None:
        """macOS target eligibility row 18."""
        shortage = offload.MacDiskShort(59, 60)
        for free, recovery, expected in (
            (60, build_targets.MacDiskRecoveryActive(shortage),
             build_targets.MacDiskRecoveryPending(self.mac, "disk", shortage)),
            (60, build_targets.MacDiskRecoveryInactive(), target_config.EligibleTarget(self.mac)),
            (59, build_targets.MacDiskRecoveryInactive(), target_config.ExcludedTarget(self.mac, "disk")),
        ):
            with (self.subTest(free=free, recovery=recovery),
                  mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(free, "fixture-mac"))):
                self.assertEqual(self.disk_result(mock.Mock(return_value=recovery)), expected)

    def test_mac_probe_maps_interruption_and_read_errors(self) -> None:
        """macOS target eligibility row 21."""
        quiet = mock.Mock(return_value=build_targets.MacDiskRecoveryInactive())
        with (mock.patch.object(offload, "probe_mac", side_effect=offload.OffloadInterrupted(15)),
              self.assertRaises(SystemExit) as stopped):
            _ = self.disk_result(quiet)
        self.assertEqual(stopped.exception.code, 143)
        with mock.patch.object(offload, "probe_mac", side_effect=OSError("fixture read failed")):
            self.assertEqual(self.disk_result(quiet),
                             target_config.ExcludedTarget(self.mac, "state unreadable: fixture read failed"))
        with (mock.patch.object(offload, "probe_mac", side_effect=conf.Unusable("fixture bad key")),
              self.assertRaisesRegex(conf.Unusable, "fixture bad key")):
            _ = self.disk_result(quiet)
        quiet.assert_not_called()
        with (mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(60, "fixture-mac")),
              mock.patch.object(offload, "interrupt_handlers", return_value=nullcontext())):
            result = build_targets.mac_target_eligibility(self.mac, self.root, "fixture", "check",
                target_config.ANY_ELIGIBLE_TARGET, answer="free", session=NoOwningSession(),
                configuration=conf.read(self.settings))
        self.assertEqual(result, target_config.EligibleTarget(self.mac))

    def backoff_files(self) -> tuple[Path, Path]:
        backoff = eligibility.backoff_path("mac")
        backoff.parent.mkdir(parents=True, exist_ok=True)
        mark = backoff.with_name("failed_probe")
        _ = backoff.write_text("9999999999\n")
        _ = mark.write_text("prior failure\n")
        return backoff, mark

    def test_reachable_probe_clears_the_previous_failure(self) -> None:
        """command_probe_backoff row 5."""
        backoff, mark = self.backoff_files()
        with (mock.patch.object(offload.ProcessController, "capture",
                                return_value=offload.CommandOutput(0, "", "")) as ssh,
              redirect_stderr(StringIO()) as output):
            self.assertEqual(build_targets.command_probe_backoff("mac", probe=True), 0)
        ssh.assert_called_once()
        self.assertFalse(backoff.exists())
        self.assertFalse(mark.exists())
        self.assertIn("reachable again", output.getvalue())

    def test_probe_backoff_rechecks_the_timer_after_success(self) -> None:
        """command_probe_backoff row 6."""
        backoff, mark = self.backoff_files()
        for start, expected in ((1, 1), (10**20, 0)):
            with (self.subTest(start=start), mock.patch.object(time, "time_ns", return_value=start),
                  mock.patch.object(offload.ProcessController, "capture", return_value=offload.CommandOutput(0, "", "")),
                  mock.patch.object(eligibility, "read_backoff", wraps=eligibility.read_backoff) as read,
                  redirect_stderr(StringIO())):
                self.assertEqual(build_targets.command_probe_backoff("mac", probe=True), expected)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(backoff.exists(), bool(expected))
            self.assertEqual(mark.exists(), bool(expected))

    def test_fixed_remote_candidate_preserves_slots_and_configuration_faults(self) -> None:
        """Fixed placement candidates row 15."""
        linux = self.root / "linux.conf"
        _ = linux.write_text("host=fixture-linux\nkinds=check\n")
        _ = self.registry.write_text(self.registry_text.replace("targets=mac,natedev", "targets=mac,natedev,linux")
                                     + "kind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n")

        def candidates() -> dict[str, placement.PlacementCandidate]:
            result = build_targets.fixed_placement_constraints(
                self.root, "", "check", ("cargo", "check"), NoOwningSession(), "fixture-source")
            return {row.destination.target: row for row in result.candidates}

        initial = candidates()
        self.assertEqual(initial["linux"].destination.slot, 1)
        self.assertEqual(initial["mac"].destination.slot, 0)
        self.assertEqual(initial["linux"].eligibility, placement.AllowedHost())
        _ = linux.write_text("kinds=check\n")
        unreadable = candidates()["linux"]
        self.assertEqual(unreadable.destination.host, "linux")
        self.assertEqual(unreadable.eligibility, placement.FixedExclusion("state unreadable"))

    def test_basic_eligibility_prints_each_registered_target(self) -> None:
        """Basic eligibility row 7."""
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(build_targets.command_eligible(self.root, "", target_config.LocalRequired("fixture rule")), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            f"mac\texcluded\tfixture rule\t{self.settings}\tmacos", "natedev\teligible\t-\t-\t-"])

    def test_registry_reads_pinned_names_only_for_a_missing_selection(self) -> None:
        """Target registry and settings helpers row 2."""
        _ = self.registry.write_text(self.registry_text +
            "pinned_targets=extra\nkind.extra=local\nplatform.extra=linux\n")
        with mock.patch.object(target_config, "pinned_target_names", wraps=target_config.pinned_target_names) as read:
            self.assertEqual(target_config.read_registered_targets(self.registry, selected="natedev"),
                             (target_config.LocalTarget("natedev", "linux"),))
            read.assert_not_called()
            self.assertEqual(target_config.read_registered_targets(self.registry, selected="extra"),
                             (target_config.LocalTarget("extra", "linux"),))
        read.assert_called_once()

    def test_registry_validates_name_and_reads_local_kind_and_platform(self) -> None:
        """Target registry and settings helpers row 3."""
        configuration = conf.read(self.registry)
        purpose = target_config.RegistryRead.PLACEMENT
        with mock.patch.object(conf.ConfigRead, "value", autospec=True, side_effect=conf.ConfigRead.value) as read:
            self.assertEqual(target_config.read_registered_targets(selected="natedev",
                purpose=purpose, configuration=configuration), (target_config.LocalTarget("natedev", "linux"),))
        self.assertEqual([call.args[1] for call in read.call_args_list],
                         ["targets", "kind.natedev", "platform.natedev"])

        _ = self.registry.write_text("targets=../invalid\n")
        with self.assertRaisesRegex(target_config.ConfigError, "invalid target name"):
            _ = target_config.read_registered_targets(self.registry, selected="../invalid", purpose=purpose)

    def test_full_registry_read_stats_the_relative_settings_path(self) -> None:
        """Target registry and settings helpers row 4."""
        with mock.patch.object(Path, "is_file", autospec=True, side_effect=Path.is_file) as stat:
            self.assertEqual(target_config.read_registered_targets(self.registry, selected="mac"), (self.mac,))
            self.settings.unlink()
            with self.assertRaisesRegex(target_config.ConfigError, "settings file does not exist"):
                _ = target_config.read_registered_targets(self.registry, selected="mac")
        self.assertEqual(stat.call_args_list, [mock.call(self.settings), mock.call(self.settings)])

    def test_registry_skips_removed_aws_only_when_unselected(self) -> None:
        """Target registry and settings helpers row 5."""
        _ = self.registry.write_text("targets=aws,runner,scale\nkind.aws=ssh\nsettings.aws=aws_target.conf\n"
                                    + "kind.runner=ssh\nplatform.runner=linux\nsettings.runner=runner.conf\n"
                                    + "kind.scale=scaler\nsettings.scale=scale.conf\n")
        purpose = target_config.RegistryRead.PLACEMENT
        self.assertEqual(target_config.read_registered_targets(self.registry, purpose=purpose), (
            target_config.SshTarget("runner", "linux", self.root / "runner.conf"),
            target_config.ScalerTarget("scale", self.root / "scale.conf")))
        with self.assertRaisesRegex(target_config.ConfigError, "permanent AWS builder was removed"):
            _ = target_config.read_registered_targets(self.registry, selected="aws", purpose=purpose)
