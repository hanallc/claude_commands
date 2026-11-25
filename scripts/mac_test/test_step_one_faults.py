"""Check that the Mac readers preserve the order of capacity checks."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import tempfile
from typing import Literal, cast, override
import unittest
from unittest import mock

from scripts.build_hold.owning_session import OwningSession
from scripts.build_targets import target_config
from scripts.lib import conf, process_start
from scripts.mac_test import claim_state, eligibility, mac_test


class MacCapacityFaultTests(unittest.TestCase):
    session: OwningSession = OwningSession("fixture-run")
    repository_key: str = "https://fixture.invalid/hana"
    root: Path = Path()
    registry: Path = Path()
    settings: Path = Path()
    repo: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = self.root / "build_targets.conf"
        self.settings = self.root / "mac_test.conf"
        self.repo = self.root / "hana"
        (self.repo / ".git").mkdir(parents=True)
        _ = self.settings.write_text("offload=on\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root),
            "CONF_DIR": str(self.root),
            "BUILD_TARGETS_STATE_DIR": str(self.root / "targets"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac"),
        })))
        _ = self.enterContext(mock.patch.object(conf, "_committed_files", return_value=()))

    def paths(self, target: str, platform: Literal["linux", "macos"]) -> claim_state.StatePaths:
        _ = self.registry.write_text(f"targets={target}\nkind.{target}=ssh\nplatform.{target}={platform}\n"
                                     + f"settings.{target}=mac_test.conf\n")
        paths = claim_state.state_paths(target)
        self.assertTrue(paths.directory.is_relative_to(self.root))
        paths.directory.mkdir(parents=True)
        return paths

    def block(self, paths: claim_state.StatePaths) -> None:
        now = datetime.now(UTC)
        mac_test.write_block(paths.block, mac_test.ActiveMacBlock(
            "fixture", "private block", now, now + timedelta(hours=1),
            mac_test.RecordedSession("fixture-holder", "fixture"), mac_test.NO_SHOWRUNNER,
            "off_before", mac_test.FREE_MESSAGE_NOT_NEEDED))

    def claim(self, paths: claim_state.StatePaths, scope: claim_state.RepositoryScope,
              *, copy: bool = False) -> None:
        record: dict[str, object] = {
            "claim_id": "fixture-claim", "episode": "fixture-episode", "attempt": "fixture-attempt",
            "repo_name": scope.name, "repo_key": scope.key, "submitting_session": "fixture-holder",
            "pid": os.getpid(), "proc_start": "fixture-start", "remote_host": "fixture-host",
            "what": "fixture job", "worktree": str(self.repo), "since": datetime.now(UTC).isoformat(),
        }
        if copy:
            record["copy"] = True
        mac_test.write_json(paths.run, record)

    def capacity(self, paths: claim_state.StatePaths, scope: claim_state.RepositoryScope,
                 names: tuple[str, ...]) -> mac_test.FreeNowAnswer:
        reader: object = getattr(mac_test, "read_free_now_target", None)
        # Main uses the direct reader. The split adds a shared read for the same run.
        if reader is None:
            return mac_test.free_now(paths, self.settings, self.repo, session=self.session, mac_scope=scope)
        read = cast(Callable[[claim_state.StatePaths, Path, Iterable[str]], object], reader)
        check = cast(Callable[[object, claim_state.RepositoryScope, str, OwningSession], mac_test.FreeNowAnswer],
                     getattr(mac_test, "check_free_now_run"))
        return check(read(paths, self.settings, names), scope, scope.name, self.session)

    def test_linux_decline_written_during_process_read_refuses_the_run(self) -> None:
        """free_now row 34. A new Linux decline must refuse the run."""
        paths = self.paths("fixture-linux", "linux")
        scope = claim_state.RepositoryScope(self.repo.name)
        self.claim(paths, scope)
        mark = eligibility.linux_version_decline_path(paths.target, scope.name)

        def owner_ended(pid: int) -> process_start.Gone:
            self.assertEqual(pid, os.getpid())
            _ = mark.write_text("fixture decline\n")
            return process_start.Gone()

        with mock.patch.object(process_start, "process_start", side_effect=owner_ended) as process:
            answer = self.capacity(paths, scope, (scope.name,))
        process.assert_called()
        self.assertTrue(mark.is_file())
        self.assertEqual(answer, "bad")

    def test_valid_block_precedes_an_unreadable_mac_claim(self) -> None:
        """free_now row 15. An unreadable claim must preserve a valid block."""
        paths = self.paths("mac", "macos")
        self.block(paths)
        _ = paths.run.write_text("{")
        scope = claim_state.RepositoryScope(self.repo.name, self.repository_key)
        self.assertEqual(self.capacity(paths, scope, (scope.name,)), "blocked")

    def assert_registry_change_preserves_capacity(self, target: str, platform: Literal["linux", "macos"]) -> None:
        paths = self.paths(target, platform)
        self.block(paths)
        scope = claim_state.RepositoryScope(self.repo.name, self.repository_key)
        resolve = eligibility.configured_ssh_target
        reads: list[str] = []

        def remove_after_read(name: str) -> target_config.SshTarget:
            reads.append(name)
            entry = resolve(name)
            if len(reads) == 1:
                _ = self.registry.write_text("targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
            return entry

        with mock.patch.object(eligibility, "configured_ssh_target", side_effect=remove_after_read):
            try:
                answer = self.capacity(paths, scope, (scope.name, "other-repo"))
            except conf.Unusable as error:
                self.fail(f"The capacity reader lost the accepted {target} registration: {error}")
        self.assertEqual(reads, [target])
        self.assertEqual(answer, "blocked")

    def test_linux_capacity_uses_the_accepted_registry_entry(self) -> None:
        """free_now row 6, 20, 31, 34. A registry change must preserve the Linux reading."""
        self.assert_registry_change_preserves_capacity("fixture-linux", "linux")

    def test_mac_copy_gives_way_after_a_transient_process_read_error(self) -> None:
        """free_now row 24. A later process read can confirm the live copy."""
        paths = self.paths("mac", "macos")
        scope = claim_state.RepositoryScope(self.repo.name, self.repository_key)
        self.claim(paths, scope, copy=True)
        with mock.patch.object(process_start, "process_start", side_effect=[
            process_start.Unreadable("fixture process read failed"), process_start.Started("fixture-start"),
        ]) as process:
            answer = mac_test.free_now(paths, self.settings, self.repo, session=self.session, mac_scope=scope)
        self.assertEqual(answer, "free")
        self.assertEqual(process.call_args_list, [mock.call(os.getpid())] * 2)

    def test_registered_mac_capacity_uses_the_accepted_registry_entry(self) -> None:
        """free_now row 6, 20, 31, 34. A registry change must preserve the macOS reading."""
        self.assert_registry_change_preserves_capacity("mac-lab", "macos")
