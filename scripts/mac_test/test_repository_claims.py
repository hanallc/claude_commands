"""Check Mac claims, Linux claims, and worktree affinity."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import FrameType
from typing import cast, override

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_wire as wire, execution_identity, placement
from scripts.buildlog import repository
from scripts.lib import process_start as process_identity
from scripts.lib.git_test_support import commit_repository
from scripts.lib.process_start_test_support import process_reader
from scripts.copies.copies_test_support import copies_environment

import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]

from scripts.mac_test import eligibility, claim_state, mac_test, offload
from scripts.mac_test.offload_test_support import split_mac_run_command
from scripts.build_hold import owning_session as session_owner
from scripts.build_targets import targets
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE, config_environment, write_config_fixture


class RepositoryKeyTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "project"
        _ = subprocess.run(["git", "init", "--quiet", str(self.root)], check=True, capture_output=True)

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *arguments], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_real_origin_forms_keep_the_normalized_repository_key(self) -> None:
        expected = eligibility.KnownRepository("project", "https://github.com/Nate/Project")
        for remote in ("git@GitHub.com:Nate/Project.git", "https://github.com/Nate/Project.git",
                       "ssh://git@github.com:22/Nate/Project.git"):
            with self.subTest(remote=remote):
                _ = self.git("config", "remote.origin.url", remote)
                self.assertEqual(eligibility.repository_identity(self.root), expected)

    def test_repository_key_keeps_github_path_case(self) -> None:
        _ = self.git("config", "remote.origin.url", "https://github.com/Team/Hana.git")
        self.assertEqual(repository.repository_key(str(self.root)), "https://github.com/Team/Hana")

    def test_other_hosts_keep_repository_case(self) -> None:
        for remote in ("git@fixture.invalid:Team/Project.git", "https://fixture.invalid/Team/Project.git"):
            with self.subTest(remote=remote):
                _ = self.git("config", "remote.origin.url", remote)
                self.assertEqual(eligibility.repository_identity(self.root),
                                 eligibility.KnownRepository("project", "https://fixture.invalid/Team/Project"))

    def test_real_repository_without_origin_keeps_the_root_commit_key(self) -> None:
        root_commit = commit_repository(self.root, "Fixture", allow_empty=True)
        self.assertEqual(eligibility.repository_identity(self.root),
                         eligibility.KnownRepository("project", "git-root:" + root_commit))

    def test_real_repository_without_commits_keeps_the_repository_name(self) -> None:
        self.assertEqual(eligibility.repository_identity(self.root), eligibility.KnownRepository("project"))

    def test_linux_uses_the_same_key_for_a_clone_with_a_quoted_local_origin(self) -> None:
        _ = commit_repository(self.root, "Fixture", allow_empty=True)
        _ = self.git("remote", "add", "origin", "https://github.com/nate/project.git")
        clone = self.root.parent / "clone"
        _ = subprocess.run(["git", "clone", "-q", str(self.root), str(clone)], check=True)
        config = clone / ".git/config"
        _ = config.write_text(config.read_text().replace(f"url = {self.root}", f'url = "{self.root}"'))
        self.assertIn(f'url = "{self.root}"', config.read_text())
        paths = claim_state.StatePaths(self.root.parent / "state", "linux", platform="linux")

        scope = claim_state.target_claim_scope(paths, str(clone))

        self.assertEqual(scope, claim_state.RepositoryScope("project", str(self.root.resolve())))

    def test_attempt_without_commits_or_origin_records_unknown_identity(self) -> None:
        log_root = self.root.parent / "buildlog"
        request = offload.OffloadRequest(self.root, "project", "empty-repository", offload.NO_RESULT_FILE, (), owner="verify",
                                        target="linux", holder_session="fixture-session")
        with mock.patch.dict(os.environ, {
            "BUILDLOG_DIR": str(log_root), "BUILDLOG_MEM_WAIT_SINCE": "", "BUILDLOG_MEM_WAIT_FIRST_FILE": "",
        }):
            offload.persist_attempt(request, offload.DeclinedMacRun("off", 0),
                                    offload.AttemptEvidence(), time.monotonic())
        rows = [cast(dict[str, object], json.loads(line))
                for path in log_root.rglob("*.jsonl") for line in path.read_text().splitlines()]
        self.assertEqual([row["repo_identity"] for row in rows if row["kind"] == "ssh_attempt"], ["unknown"])

    def test_repository_key_lookup_runs_no_memory_admit_code(self) -> None:
        files: set[str] = set()

        def collect_file(frame: FrameType, event: str, argument: object) -> None:
            if event == "call" and argument is None:
                files.add(frame.f_code.co_filename)

        sys.setprofile(collect_file)
        try:
            _ = eligibility.repository_identity(self.root)
        finally:
            sys.setprofile(None)
        self.assertNotIn("memory_admit.py", {Path(file).name for file in files})


class RepositoryClaimTests(unittest.TestCase):
    root: Path = Path()
    origins: dict[str, str] = {}
    common: dict[str, Path] = {}
    dead: set[int] = set()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.origins = {}
        self.common = {}
        self.dead = set()
        settings = (
            "offload=on\nci_repo=\nblock_hours=4\nblock_max_hours=48\n"
            "mirror_root=~/.local/state/mac-test/test-mirror\n"
            "block_warn_minutes=15\ngh_timeout_s=1\nclaim_idle_minutes=15\n"
            "claim_poll_seconds=60\nconnect_timeout_s=1\nprobe_timeout_s=1\n"
            "unreachable_backoff_s=30\n"
            "build_budget_gib=32\ntarget_budget_gib=24\n"
        )
        for name in ("mac", "linux"):
            _ = write_config_fixture(self.root / f"{name}.conf", f"host=fake-{name}\n" + settings,
                                     template="mac_test")
        _ = write_config_fixture(self.root / "mac_test.conf", "ci_repo=\nmirror_root=~/.local/state/mac-test/test-mirror\n")
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(
            "targets=mac,linux,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac.conf\n"
            + "kind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n" + UNREACHABLE_WAIT_FIXTURE
        )
        private = copies_environment(self.root)
        _ = shutil.copy2(Path(private["CONF_DIR"]) / "build_folders.conf", self.root / "build_folders.conf")
        _ = write_config_fixture(self.root / "machines.conf", "mac.hosts=fake-mac\n")
        _ = write_config_fixture(self.root / "build_folders.conf", "names.repository_key_length=8\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **config_environment(self.root),
            **private,
            "CONF_DIR": str(self.root),
            "HOME": str(self.root), "BUILD_TARGETS_CONFIG": str(registry),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch.object(process_identity, "process_start", side_effect=process_reader(self.process_start)))
        _ = self.enterContext(mock.patch.object(mac_test, "utc_now", return_value=datetime(2026, 11, 1, tzinfo=timezone.utc)))
        _ = self.enterContext(mock.patch.object(time, "monotonic", return_value=100.0))
        _ = self.enterContext(mock.patch.object(session_owner, "owning_session", return_value=NoOwningSession()))
        _ = self.enterContext(mock.patch.object(mac_test, "start_claim_watcher"))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=self.git_reply))
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected child process")))

    def process_start(self, pid: int) -> str | None:
        return None if pid in self.dead else f"start-{pid}"

    def repository(self, name: str, origin: str, source: Path | None = None) -> Path:
        root = self.root / name
        root.mkdir(parents=True)
        if source is None:
            common = root / ".git"
            common.mkdir()
            _ = (common / "config").write_text(f'[remote "origin"]\n\turl = {origin}\n')
        else:
            common = self.common[str(source)]
            private = common / "worktrees" / root.name
            private.mkdir(parents=True)
            _ = (private / "commondir").write_text("../..\n")
            _ = (root / ".git").write_text(f"gitdir: {private}\n")
        self.origins[str(root)] = origin
        self.common[str(root)] = common
        return root

    def git_reply(self, argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        self.assertEqual(argv[0], "git", f"Unexpected external command: {argv}")
        directory = argv[argv.index("-C") + 1] if "-C" in argv else str(_options.get("cwd", ""))
        if "config" in argv and "--git-dir" in argv:
            origin = self.origins[str(Path(directory).parent)]
            return subprocess.CompletedProcess(argv, 0, f"local\0remote.origin.url\n{origin}\0", "")
        origin = self.origins.get(directory)
        if origin is None:
            return subprocess.CompletedProcess(argv, 128, "", "not a repository")
        if "remote" in argv or "remote.origin.url" in argv:
            answer = origin
        elif "--git-common-dir" in argv:
            answers = [str(self.common[directory])]
            if "--show-toplevel" in argv:
                answers.append(directory)
            if "--abbrev-ref" in argv:
                answers.append("main")
            answer = "\n".join(answers)
        elif "--show-toplevel" in argv:
            answer = directory
        elif "--git-dir" in argv:
            answer = str(self.common[directory])
        elif "rev-list" in argv or "HEAD" in argv:
            answer = "a" * 40
        else:
            self.fail(f"Unexpected git command: {argv}")
        return subprocess.CompletedProcess(argv, 0, answer + "\n", "")

    def arguments(self, target: str, worktree: Path, pid: int, remote: bool = False) -> mac_test.CliArguments:
        args = mac_test.CliArguments()
        args.target = target
        args.worktree = str(worktree)
        args.pid = pid
        args.what = f"check {worktree.name}"
        if remote:
            args.remote = f"fake-{target}"
        return args

    def claim(self, target: str, worktree: Path, pid: int, remote: bool = False,
              *, identity: bool = False,
              request_identity: claim_state.ClaimIdentity | claim_state.NoClaimIdentity = claim_state.NO_CLAIM_IDENTITY,
              ) -> tuple[int, str]:
        output = io.StringIO()
        args = self.arguments(target, worktree, pid, remote)
        args.identity = identity
        if isinstance(request_identity, claim_state.ClaimIdentity):
            args.claim_id, args.episode, args.attempt = request_identity.claim_id, request_identity.episode, request_identity.attempt
        with redirect_stdout(output):
            status = mac_test.command_claim(args, mac_test.state_paths(target))
        return status, output.getvalue()

    def release(self, target: str, pid: int) -> None:
        args = mac_test.CliArguments()
        args.target, args.pid = target, pid
        claim = self.claims(target)[pid]
        args.claim_id = cast(str, claim["claim_id"])
        args.episode = cast(str, claim["episode"])
        args.attempt = cast(str, claim["attempt"])
        self.assertEqual(mac_test.command_release(args, mac_test.state_paths(target)), 0)

    def claims(self, target: str) -> dict[int, dict[str, object]]:
        return {claim.record["pid"]: cast(dict[str, object], cast(object, claim.record))
                for claim in mac_test.read_claims(mac_test.state_paths(target))}

    def test_origin_forms_and_clone_names_share_repository_capacity(self) -> None:
        main = self.repository("hana", "git@GitHub.com:Nate/Hana.git")
        clone = self.repository("tmp/phase-copy", "https://github.com/nate/hana/")
        linked = self.repository("worktrees/feature", "ssh://git@github.com:22/nate/hana.git", main)

        identities = [eligibility.repository_identity(path) for path in (main, clone, linked)]

        known = [identity for identity in identities if isinstance(identity, eligibility.KnownRepository)]
        self.assertEqual(len(known), 3)
        self.assertEqual([identity.key for identity in known],
                         ["https://github.com/Nate/Hana", "https://github.com/nate/hana", "https://github.com/Nate/Hana"])
        self.assertEqual([identity.name for identity in known], ["hana", "phase-copy", "hana"])
        self.assertIsInstance(identities[0], eligibility.KnownRepository)
        self.assertEqual(cast(eligibility.KnownRepository, identities[0]).name, "hana")

        paths = mac_test.state_paths("mac")
        for worktree, pid, expected in ((main, 101, 0), (clone, 102, 11)):
            args = self.arguments("mac", worktree, pid)
            mirror = claim_state.mac_repository_identity(args.worktree)
            assert isinstance(mirror, claim_state.ResolvedMacMirror)
            saved = mirror.claim_repository()
            assert isinstance(saved, claim_state.MacRepositoryIdentity)
            scope = claim_state.RepositoryScope(saved.mirror_name, saved.key)
            with claim_state.locked(paths.lock):
                result = mac_test.claim_once(args, paths, scope, session=NoOwningSession(),
                                             mac_kind=mac_test.MacClaimKind.GUEST, mac_repository=saved)
            if expected == 0:
                self.assertIsInstance(result, mac_test.ClaimFinished)
            else:
                self.assertIsInstance(result, mac_test.ClaimBusy)

        self.assertEqual(self.claim("mac", linked, 103)[0], 11)
        self.assertNotIn("slot", self.claims("mac")[101])
        self.release("mac", 101)
        self.assertEqual(self.claim("mac", clone, 102), (0, "claimed\n"))

    def test_saved_github_key_in_mixed_case_still_holds_capacity(self) -> None:
        first = self.repository("first/hana", "https://github.com/Nate/Hana.git")
        second = self.repository("second/clone", "https://github.com/nate/hana.git")
        with mock.patch.object(claim_state, "mac_claim_key", side_effect=str):
            self.assertEqual(self.claim("mac", first, 101)[0], 0)
        paths = mac_test.state_paths("mac")
        saved, = mac_test.read_claims(paths)
        original = saved.path.read_bytes()
        self.assertEqual(saved.record.get("repo_key"), "https://github.com/Nate/Hana")
        self.assertEqual(self.claim("mac", second, 102)[0], 11)
        self.assertEqual(saved.path.read_bytes(), original)

    def test_linux_clones_with_different_names_share_origin_capacity(self) -> None:
        main = self.repository("project", "git@github.com:nate/project.git")
        clone = self.repository("phase-copy", "https://github.com/nate/project.git")
        self.assertEqual(self.claim("linux", main, 101)[0], 0)
        self.assertEqual(self.claim("linux", clone, 102)[0], 11)

        self.assertEqual(self.claim("linux", main, 103)[0], 11)
        self.assertEqual(self.claim("linux", clone, 104)[0], 11)

    def test_mac_serializes_different_origins_with_the_same_name(self) -> None:
        first = self.repository("first/project", "https://github.com/first/project.git")
        second = self.repository("second/project", "https://github.com/second/project.git")

        self.assertNotEqual(eligibility.repository_identity(first), eligibility.repository_identity(second))
        self.assertEqual(self.claim("mac", first, 101)[0], 0)
        self.assertEqual(self.claim("mac", second, 102)[0], 11)
        self.assertEqual(set(self.claims("mac")), {101})
        self.release("mac", 101)
        self.assertEqual(self.claim("mac", second, 102)[0], 0)

    def test_linux_keeps_same_name_origins_independent(self) -> None:
        first = self.repository("first/project", "https://github.com/first/project.git")
        second = self.repository("second/project", "https://github.com/second/project.git")
        self.assertEqual(self.claim("linux", first, 101)[0], 0)
        self.assertEqual(self.claim("linux", first, 102)[0], 11)

        self.assertEqual(self.claim("linux", second, 103)[0], 0)
        self.assertEqual(set(self.claims("linux")), {101, 103})

    def test_local_and_shared_clones_keep_the_original_identity(self) -> None:
        main = self.repository("hana", "https://github.com/nate/hana.git")
        local = self.repository("tmp/local-clone", str(main))
        shared = self.repository("tmp/shared-clone", str(local))
        alternates = shared / ".git/objects/info/alternates"
        alternates.parent.mkdir(parents=True)
        _ = alternates.write_text(str(main / ".git/objects") + "\n")

        expected = eligibility.repository_identity(main)

        self.assertEqual(eligibility.repository_identity(local), expected)
        self.assertEqual(eligibility.repository_identity(shared), expected)

    def test_credentials_and_default_ports_do_not_change_identity(self) -> None:
        first = self.repository("one/project", "https://user@example.com:443/team/project.git?view=1#readme")
        second = self.repository("two/project", "ssh://git@example.com:22/team/project.git")
        third = self.repository("three/project", "https://example.com:8443/team/project.git")

        self.assertEqual(eligibility.repository_identity(first), eligibility.repository_identity(second))
        self.assertNotEqual(eligibility.repository_identity(second), eligibility.repository_identity(third))

    def test_concurrent_requests_take_one_repository_claim(self) -> None:
        root = self.repository("project", "https://github.com/nate/project.git")
        barrier = threading.Barrier(6)

        def contend(pid: int) -> int:
            _ = barrier.wait(timeout=5)
            return mac_test.command_claim(self.arguments("linux", root, pid), mac_test.state_paths("linux"))

        with redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=6) as workers:
            statuses = list(workers.map(contend, range(101, 107)))

        self.assertEqual(sorted(statuses), [0, 11, 11, 11, 11, 11])
        self.assertEqual(len(self.claims("linux")), 1)

    def test_remote_claims_remain_when_the_local_owner_ends(self) -> None:
        root = self.repository("project", "https://github.com/nate/project.git")
        self.assertEqual(self.claim("linux", root, 101, remote=True)[0], 0)
        before = self.claims("linux")
        self.dead.add(101)

        self.assertEqual(self.claim("linux", root, 103)[0], 11)
        self.assertEqual(self.claims("linux"), before)
        self.release("linux", 101)
        self.assertEqual(self.claim("linux", root, 103)[0], 0)

    def test_repeated_claim_preserves_its_identity(self) -> None:
        root = self.repository("project", "https://github.com/nate/project.git")
        requested = claim_state.ClaimIdentity("claim-a", "episode-a", "attempt-a")
        self.assertEqual(self.claim("linux", root, 101, request_identity=requested)[0], 0)
        before = self.claims("linux")

        self.assertEqual(self.claim("linux", root, 101, request_identity=requested)[0], 0)

        self.assertEqual(self.claims("linux"), before)
        for other in (claim_state.ClaimIdentity("claim-b", "episode-a", "attempt-a"),
                      claim_state.ClaimIdentity("claim-a", "episode-b", "attempt-a"),
                      claim_state.ClaimIdentity("claim-a", "episode-a", "attempt-b")):
            with self.subTest(identity=other):
                self.assertEqual(self.claim("linux", root, 101, request_identity=other)[0], 11)
                self.assertEqual(self.claims("linux"), before)

    def test_takeover_keeps_the_remote_claim_until_cleanup(self) -> None:
        root = self.repository("project", "https://github.com/nate/project.git")
        self.assertEqual(self.claim("linux", root, 101, remote=True)[0], 0)
        before = self.claims("linux")[101]
        self.dead.add(101)

        status, output = self.claim("linux", root, 102, remote=True, identity=True)

        self.assertEqual(status, 13)
        answer = offload.read_claim_answer(offload.CommandOutput(status, output, ""))
        self.assertIsInstance(answer, offload.TakenOverMacClaim)
        self.assertEqual(set(self.claims("linux")), {102})
        retained = self.claims("linux")[102]
        self.assertEqual(retained.get("remote_host"), before.get("remote_host"))
        if isinstance(answer, offload.TakenOverMacClaim):
            self.assertEqual(answer.slot, 1)

    def test_dead_request_does_not_write_claim_state(self) -> None:
        root = self.repository("project", "https://github.com/nate/project.git")
        self.assertEqual(self.claim("linux", root, 101)[0], 0)
        self.release("linux", 101)
        paths = mac_test.state_paths("linux")
        before = {str(path.relative_to(paths.directory)): path.read_bytes()
                  for path in paths.directory.rglob("*.json")}
        self.dead.add(102)

        self.assertEqual(self.claim("linux", root, 102)[0], 1)
        after = {str(path.relative_to(paths.directory)): path.read_bytes()
                 for path in paths.directory.rglob("*.json")}
        self.assertEqual(after, before)

    def test_linux_mirrors_use_the_build_folder_rule_without_percent_encoding_the_key(self) -> None:
        cases = (
            (eligibility.KnownRepository("checkout", "https://example.com/one/hana"), "hana-33d435bf"),
            (eligibility.KnownRepository("checkout", "https://example.com/two/hana"), "hana-020748b0"),
            (eligibility.KnownRepository("project", "git-root:" + "1" * 40), "project-c8446dca"),
        )
        mirrors: set[str] = set()
        for repository, name in cases:
            with self.subTest(repository=repository):
                mirror = offload.remote_mirror(repository, "linux", 2)
                self.assertEqual(mirror, f".local/state/build-target/mirror/{name}/linux-s2")
                self.assertNotIn("%", mirror)
                self.assertIn("%", repository.path_key)
                mirrors.add(mirror)
        self.assertEqual(len(mirrors), len(cases))
        self.assertEqual(offload.remote_mirror(eligibility.KnownRepository("other-copy", cases[0][0].key), "linux", 2),
                         offload.remote_mirror(cases[0][0], "linux", 2))

    def test_linux_mirrors_use_distinct_paths_for_each_origin_and_number(self) -> None:
        first = self.repository("one/project", "ssh://git@example.com/a/project.git")
        second = self.repository("two/project", "https://example.com/b/project.git")
        first_id = eligibility.repository_identity(first)
        second_id = eligibility.repository_identity(second)
        self.assertIsInstance(first_id, eligibility.KnownRepository)
        self.assertIsInstance(second_id, eligibility.KnownRepository)
        if not isinstance(first_id, eligibility.KnownRepository) or not isinstance(second_id, eligibility.KnownRepository):
            self.fail("Repository identity must resolve before selecting a mirror")
        mirrors = [offload.remote_mirror(first_id, "linux", 1), offload.remote_mirror(first_id, "linux", 2),
                   offload.remote_mirror(second_id, "linux", 1)]

        self.assertEqual(len(set(mirrors)), 3)
        for mirror, slot in zip(mirrors, (1, 2, 1), strict=True):
            path = PurePosixPath(mirror)
            self.assertEqual(path.parts[:4], (".local", "state", "build-target", "mirror"))
            self.assertEqual(len(path.parts), 6)
            self.assertEqual(path.name, f"linux-s{slot}")
            self.assertNotIn("..", path.parts)
        self.assertEqual(offload.remote_mirror(first_id), ".local/state/mac-test/test-mirror/project")

    def test_invalid_legacy_numbers_never_confirm_a_claim(self) -> None:
        for output in ("claimed\nslot: 0\n", "claimed\nslot: -1\n", "claimed\nslot: x\n",
                       "claimed\nslot: 1\nslot: 2\n"):
            with self.subTest(output=output):
                output += 'claim_identity={"claim_id":"claim-a","episode":"episode-a","attempt":"attempt-a"}\n'
                answer = offload.read_claim_answer(offload.CommandOutput(0, output, ""))
                self.assertIsInstance(answer, offload.MacClaimNotConfirmed)

    def test_origin_name_does_not_move_a_legacy_mac_mirror_or_claim(self) -> None:
        root = self.repository(".claude", "https://github.com/nate/claude_commands.git")
        identity = eligibility.repository_identity(root)
        self.assertIsInstance(identity, eligibility.KnownRepository)
        if not isinstance(identity, eligibility.KnownRepository):
            self.fail("The repository must have a known identity")
        self.assertEqual(identity.name, ".claude")
        self.assertEqual(identity.key, "https://github.com/nate/claude_commands")
        self.assertEqual(offload.remote_mirror(identity), ".local/state/mac-test/test-mirror/.claude")
        paths = mac_test.state_paths("mac")
        path = paths.claims / ".claude.json"
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps({
            "claim_id": "claim-a", "episode": "episode-a", "attempt": "attempt-a", "submitting_session": "",
            "pid": 101, "proc_start": "start-101", "worktree": str(root), "what": "legacy run",
            "since": "2026-11-01T00:00:00+00:00",
        }))

        self.assertEqual(self.claim("mac", root, 102)[0], 11)
        self.assertEqual(list(paths.claims.iterdir()), [path])

    def test_takeover_retry_requires_cleanup_before_a_fresh_claim(self) -> None:
        root = self.repository("project", "https://github.com/nate/project.git")
        self.assertEqual(self.claim("linux", root, 101, remote=True)[0], 0)
        self.dead.add(101)
        status, output = self.claim("linux", root, 102, remote=True, identity=True)
        self.assertEqual(status, 13)

        requested = claim_state.reply_claim_identity(output)
        repeated_status, repeated_output = self.claim(
            "linux", root, 102, remote=True, identity=True, request_identity=requested)

        self.assertEqual(repeated_status, 13)
        self.assertEqual(repeated_output, output)
        self.assertIsInstance(offload.read_claim_answer(
            offload.CommandOutput(repeated_status, repeated_output, "")), offload.TakenOverMacClaim)
        self.release("linux", 102)
        self.assertEqual(self.claim("linux", root, 102, remote=True)[0], 0)

    def test_target_platform_selects_the_mirror_and_claim_layout(self) -> None:
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(registry.read_text().replace(
            "targets=mac,linux,natedev", "targets=mac,linux,darwin,natedev")
            + "kind.darwin=ssh\nplatform.darwin=macos\nsettings.darwin=mac.conf\n")
        root = self.repository("project", "https://github.com/nate/project.git")
        identity = eligibility.repository_identity(root)
        if not isinstance(identity, eligibility.KnownRepository):
            self.fail("The repository must have a known identity")
        self.assertEqual(offload.remote_mirror(identity, "darwin"), ".local/state/mac-test/test-mirror/project")
        self.assertEqual(self.claim("darwin", root, 101)[0], 0)
        self.assertEqual(self.claim("darwin", root, 102)[0], 11)
        saved = claim_state.mac_repository_identity(str(root))
        assert isinstance(saved, claim_state.MacMirrorIdentity)
        self.assertTrue((mac_test.state_paths("darwin").claims / saved.claim_file).exists())
        _ = registry.write_text(registry.read_text().replace("platform.mac=macos", "platform.mac=linux"))

        self.assertTrue(offload.remote_mirror(identity, "mac", 2).endswith("/mac-s2"))
        self.assertEqual(self.claim("mac", root, 103)[0], 0)
        self.assertNotIn("slot", self.claims("mac")[103])

    def test_target_state_includes_each_repository_claim(self) -> None:
        registry = self.root / "build_targets.conf"
        _ = registry.write_text(registry.read_text().replace('targets=mac,linux,', 'targets=mac,linux-one,').replace('.linux=', '.linux-one=').replace('=linux.conf', '=linux-one.conf'))
        _ = (self.root / "linux.conf").rename(self.root / "linux-one.conf")
        root = self.repository("project", "https://github.com/nate/project.git")
        other = self.repository("other", "https://github.com/nate/other.git")
        self.assertEqual(self.claim("linux-one", root, 101)[0], 0)
        self.assertEqual(self.claim("linux-one", other, 102)[0], 0)
        configured = next(target for target in targets.read_targets() if target.name == "linux-one")

        state = targets.target_state(configured)

        self.assertIsInstance(state, targets.Running)
        if isinstance(state, targets.Running):
            self.assertEqual(len(state.jobs), 2)

    def test_mac_skip_keeps_legacy_filters_when_linux_filter_support_expands(self) -> None:
        identity = eligibility.KnownRepository("hana", "https://github.com/nate/hana")
        settings = self.root / "mac.conf"
        _ = write_config_fixture(settings, settings.read_text() + "mac_skip.hana=test(gpu)\n",
                                 template="mac_test")
        cases = (
            (("-E", "test(a)"), ("-E", "(test(a)) & not (test(gpu))")),
            (("--workspace",), ("--workspace",)),
            (("-E",), ("-E",)),
            (("--filterset=test(a)",), ("--filterset=test(a)",)),
            (("-E=test(a)",), ("-E=test(a)",)),
        )
        for words, expected in cases:
            with self.subTest(words=words):
                request = offload.OffloadRequest(self.root / "hana", "hana", "call", offload.NO_RESULT_FILE, words, owner="verify", holder_session="fixture-session")

                command = offload.remote_test_command(eligibility.read_config(settings), request, identity)

                invocation, _ = split_mac_run_command(command)
                start = invocation.index("~/.claude/scripts/lint/lint") + 1
                self.assertEqual(invocation[start:], ["nextest", *expected])

    def test_saved_numbered_ssh_assignment_adopts_its_exact_reservation(self) -> None:
        source = self.repository("project", "https://github.com/nate/project.git")
        paths = mac_test.state_paths("linux")
        scope = claim_state.RepositoryScope("project", "https://github.com/nate/project")
        destination = placement.PlacementDestination("ssh", "linux", "fake-linux", scope, 2)
        owner = placement.SubmittingOwner("fixture", 101, "start-101")
        assignment = placement.PlacementAssignment("episode", "attempt", destination,
            execution_identity.SourceKeyKnown("source"), owner, NoOwningSession(), 0)
        identity = claim_state.ClaimIdentity(execution_identity.assigned_claim_id("episode", "attempt"),
                                             "episode", "attempt")
        self.assertEqual(self.claim("linux", source, owner.pid, request_identity=identity)[0], 0)
        claim, = claim_state.read_claims(paths)
        legacy = claim.path.with_name(f"{claim.path.stem}.slot-2.json")
        _ = claim.path.rename(legacy)

        runner = self.arguments("linux", source, 102, remote=True)
        runner.from_pid = owner.pid
        runner.claim_id, runner.episode, runner.attempt = identity.claim_id, identity.episode, identity.attempt
        runner.assignment = wire.encode_reply(wire.Assignment(assignment))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(mac_test.command_claim(runner, paths), 0)

        saved, = claim_state.read_claims(paths)
        self.assertEqual((saved.path, saved.scope), (legacy, scope))
        self.assertEqual(claim_state.claim_identity(saved.record), identity)
        self.assertEqual((saved.record["pid"], saved.record.get("remote_host")), (102, destination.host))
        self.assertEqual(output.getvalue().splitlines(), ["slot: 2", "claimed"])

    def test_legacy_linux_claim_holds_repository_until_its_owner_ends(self) -> None:
        source = self.repository("project", "https://github.com/nate/project.git")
        paths = mac_test.state_paths("linux")
        self.assertEqual(self.claim("linux", source, 101)[0], 0)
        claim, = mac_test.read_claims(paths)
        legacy = claim.path.with_name(claim.path.stem + ".slot-2.json")
        _ = claim.path.rename(legacy)

        self.assertEqual(self.claim("linux", source, 102)[0], 11)
        self.assertTrue(legacy.is_file())
        self.dead.add(101)

        self.assertEqual(self.claim("linux", source, 102)[0], 0)
        self.assertFalse(legacy.exists())
        self.assertTrue(claim.path.is_file())


if __name__ == "__main__":
    _ = unittest.main()
