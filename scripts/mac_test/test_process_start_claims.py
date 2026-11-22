"""Use the same Darwin start identity for claim adoption and PID reuse."""
from __future__ import annotations

from contextlib import chdir, redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import Literal
from unittest import mock

import sys

from scripts.build_queue import execution_identity
from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.mac_test import claim_state, eligibility, mac_test, offload
from scripts.lib import process_start as identity
from scripts.build_queue import placement, admission_protocol as wire
from scripts.build_queue.test_placement import (
    PlacementFixture, placement_request, publish_candidate, assignment_for, coordinator_holder,
)
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE, write_config_fixture


class ClaimProcessStartTests(unittest.TestCase):
    def test_claim_from_a_long_wait_uses_the_same_worktree_as_the_runner(self) -> None:
        with PlacementFixture() as fixture:
            root = fixture.root
            repository = root / "hana"
            repository.mkdir()
            alias = root / "checkout"
            alias.symlink_to(repository, target_is_directory=True)
            _ = write_config_fixture(root / "mac_test.conf", "offload=on\nlinux_only.hana=\n", template="mac_test")
            request = placement_request(fixture, since=fixture.clock.now())
            fixture.clock.advance(3600)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            args = mac_test.CliArguments()
            args.pid, args.repo_root, args.package = assignment.owner.pid, str(alias), "hana"
            args.settings = str(root / "mac_test.conf")
            args.identity = True
            paths = mac_test.StatePaths(root / "provider")
            state_environment = {"MAC_TEST_STATE_DIR": str(root / "mac-state"),
                                 "BUILD_TARGETS_STATE_DIR": str(root / "target-state")}
            reservation = io.StringIO()
            with (mock.patch.dict(os.environ, state_environment),
                  mock.patch.object(eligibility, "repository_identity", return_value=eligibility.KnownRepository("hana")),
                  redirect_stdout(reservation)):
                self.assertEqual(mac_test.command_claim_wait(args, paths, session=assignment.session,
                    assignment=assignment), 0)
            exact = claim_state.reply_claim_identity(reservation.getvalue())
            assert isinstance(exact, claim_state.ClaimIdentity)
            runner_pid = assignment.owner.pid + 1
            runner_start = identity.Started("70000")

            def read_start(pid: int) -> identity.ProcessStart:
                if pid == runner_pid:
                    return runner_start
                self.assertEqual(pid, assignment.owner.pid)
                return identity.Started(assignment.owner.start_time)

            with mock.patch.object(identity, "process_start", side_effect=read_start), redirect_stdout(io.StringIO()):
                parser = offload.parser()
                runner = offload.request_from_arguments(parser.parse_args(
                    ["run", "--repo-root", str(alias), "--call-id", assignment.episode],
                    namespace=offload.CliArguments()))
                adoption = mac_test.CliArguments()
                adoption.pid, adoption.from_pid = runner_pid, assignment.owner.pid
                adoption.remote, adoption.worktree = assignment.destination.host, str(runner.repo_root)
                adoption.claim_id, adoption.episode, adoption.attempt = exact.claim_id, exact.episode, exact.attempt
                status = mac_test.command_claim(adoption, paths, watch_claim=False, assignment=assignment)
            self.assertEqual(status, 0)
            adopted, = mac_test.read_claims(paths)
            self.assertEqual(claim_state.claim_identity(adopted.record), exact)
            self.assertEqual((adopted.record["pid"], adopted.record["proc_start"]), (runner_pid, runner_start.start))

    def test_direct_claim_adopts_the_same_worktree_with_a_raw_path_on_either_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "scripts").mkdir()
            raw = str(root / "scripts" / "..")
            canonical = str(root)
            cases = ((raw, canonical, False), (canonical, raw, False), (canonical, canonical, True))
            parser = mac_test.parser()
            for index, (reserved_path, runner_path, stored_raw_path) in enumerate(cases):
                with self.subTest(reserved_path=reserved_path, runner_path=runner_path, stored_raw_path=stored_raw_path):
                    paths = mac_test.StatePaths(root / f"provider-{index}")
                    words = ["claim", "--what", "nextest hana", "--claim-id", "claim-a",
                             "--episode", "episode-a", "--attempt", "attempt-a", "--session", "fixture"]
                    reserve = parser.parse_args([*words, "--pid", "41", "--worktree", reserved_path],
                                                namespace=mac_test.CliArguments())
                    adopt = parser.parse_args([*words, "--pid", "42", "--from-pid", "41", "--worktree", runner_path,
                                               "--remote", "fixture-mac"], namespace=mac_test.CliArguments())

                    def read_start(pid: int) -> identity.ProcessStart:
                        return identity.Started(str(pid * 1000))

                    errors = io.StringIO()
                    with mock.patch.object(identity, "process_start", side_effect=read_start), \
                            redirect_stdout(io.StringIO()), redirect_stderr(errors):
                        self.assertEqual(mac_test.command_claim(reserve, paths, watch_claim=False, session=reserve.session), 0)
                        reserved, = mac_test.read_claims(paths)
                        if stored_raw_path:
                            mac_test.write_json(reserved.path, {**reserved.record, "worktree": raw})
                        status = mac_test.command_claim(adopt, paths, watch_claim=False, session=adopt.session)
                    self.assertEqual(status, 0, errors.getvalue())
                    self.assertEqual(reserved.record["worktree"], canonical)
                    adopted, = mac_test.read_claims(paths)
                    self.assertEqual(claim_state.claim_identity(adopted.record), claim_state.claim_identity(reserved.record))
                    self.assertEqual((adopted.record["pid"], adopted.record["proc_start"]), (42, "42000"))
                    self.assertEqual(adopted.record["worktree"], canonical)

    def test_saved_relative_worktree_cannot_be_adopted_from_another_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            scope = mac_test.RepositoryScope("hana")
            session = OwningSession("fixture-session")

            def read_start(pid: int) -> identity.ProcessStart:
                return identity.Started(str(pid * 1000))

            with (mock.patch.object(mac_test, "target_claim_scope", return_value=scope),
                  mock.patch.object(identity, "process_start", side_effect=read_start),
                  redirect_stdout(io.StringIO())):
                for reservation_root, runner_root in ((first, second), (second, first)):
                    with self.subTest(reservation_root=reservation_root, runner_root=runner_root):
                        paths = mac_test.StatePaths(root / f"provider-{reservation_root.name}")
                        reserve = mac_test.CliArguments()
                        reserve.pid, reserve.worktree = 41, "."
                        reserve.claim_id, reserve.episode, reserve.attempt = "claim-a", "episode-a", "attempt-a"
                        with chdir(reservation_root):
                            self.assertEqual(mac_test.command_claim(reserve, paths, watch_claim=False, session=session), 0)
                        held, = mac_test.read_claims(paths)
                        self.assertEqual(held.record["worktree"], str(reservation_root))
                        mac_test.write_json(held.path, {**held.record, "worktree": "."})
                        before = held.path.read_bytes()

                        adopt = mac_test.CliArguments()
                        adopt.pid, adopt.from_pid, adopt.worktree, adopt.remote = 42, 41, ".", "fixture-mac"
                        adopt.claim_id, adopt.episode, adopt.attempt = reserve.claim_id, reserve.episode, reserve.attempt
                        errors = io.StringIO()
                        with chdir(runner_root), redirect_stderr(errors):
                            status = mac_test.command_claim(adopt, paths, watch_claim=False, session=session)
                        self.assertEqual(status, 12, errors.getvalue())
                        self.assertEqual(errors.getvalue(), "cannot adopt the Mac claim: worktree\n")
                        self.assertEqual(held.path.read_bytes(), before)

    def test_saved_relative_worktree_allows_retained_claim_cleanup_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = mac_test.StatePaths(root, "box", platform="linux")
            scope = mac_test.RepositoryScope("hana", "repo-key", 2)
            record: mac_test.RemoteJobClaim = {
                "claim_id": "claim-a", "episode": "episode-a", "attempt": "attempt-a",
                "submitting_session": "old-session", "pid": 41, "proc_start": "41000",
                "what": "nextest hana", "worktree": ".", "since": "2026-11-04T00:00:00+00:00",
                "repo_name": scope.name, "repo_key": scope.key, "slot": scope.slot,
                "remote_host": "fixture-box",
            }
            path = mac_test.claim_path(paths, scope)
            path.parent.mkdir()
            mac_test.write_json(path, record)
            held, = mac_test.read_claims(paths)
            args = mac_test.CliArguments()
            args.pid, args.worktree = 42, str(root)
            args.claim_id, args.episode, args.attempt = record["claim_id"], record["episode"], record["attempt"]
            args.leased_slots = [scope.slot]
            session = OwningSession("cleanup-session")

            def read_start(pid: int) -> identity.ProcessStart:
                return identity.Started("42000") if pid == args.pid else identity.Gone()

            output = io.StringIO()
            with (mock.patch.object(identity, "process_start", side_effect=read_start),
                  mock.patch.object(mac_test, "target_claim_scope", return_value=scope),
                  redirect_stdout(output)):
                with mac_test.locked(paths.lock):
                    result = mac_test.take_over_kept_claim(args, held, {**record}, session=session)
                self.assertEqual(result, mac_test.ClaimFinished(13))
                self.assertEqual(mac_test.command_claim(args, paths, watch_claim=False, session=session), 13)
            self.assertEqual(output.getvalue().splitlines(), ["leftovers: fixture-box", "slot: 2"] * 2)
            saved, = mac_test.read_claims(paths)
            self.assertEqual(saved.scope, scope)
            self.assertEqual(claim_state.claim_identity(saved.record), claim_state.claim_identity(record))
            self.assertEqual((saved.record["pid"], saved.record["proc_start"]), (42, "42000"))
            self.assertEqual(saved.record["submitting_session"], session.session_id)
            self.assertIs(saved.record.get("cleanup_pending"), True)
            self.assertEqual(saved.record["worktree"], ".")

    def test_adoption_refusal_names_the_failed_check_without_changing_the_reservation(self) -> None:
        checks = ("runner pid start", "no reservation", "reservation count", "scope", "pid", "identity",
                  "submitting session", "remote host", "proc_start", "worktree", "remote missing", "block state",
                  "leased slot")
        for check in checks:
            with self.subTest(check=check), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = (mac_test.StatePaths(root, "box", platform="linux") if check == "leased slot"
                         else mac_test.StatePaths(root))
                scope = mac_test.RepositoryScope("hana")
                session = OwningSession("fixture-session")
                record: mac_test.ProcessBoundClaim = {
                    "claim_id": "claim-a", "episode": "episode-a", "attempt": "attempt-a",
                    "submitting_session": session.session_id, "pid": 41, "proc_start": "40000",
                    "what": "waiting", "worktree": str(root), "since": "2026-11-04T00:00:00+00:00",
                    "repo_name": "other" if check == "scope" else "hana", "repo_key": "",
                }
                args = mac_test.CliArguments()
                args.pid, args.from_pid = 42, 40 if check == "pid" else 41
                args.claim_id, args.episode = "claim-a", "episode-a"
                args.attempt = "foreign-attempt" if check == "identity" else "attempt-a"
                args.worktree = str(root / "other") if check == "worktree" else str(root)
                if check != "remote missing":
                    args.remote = "fixture-mac"
                if check == "submitting session":
                    session = OwningSession("foreign-session")
                if check == "leased slot":
                    record["slot"] = 2
                    args.leased_slots = [1]
                if check != "no reservation":
                    mac_test.write_json(paths.run, {**record, "remote_host": "fixture-mac"} if check == "remote host"
                                        else record)
                if check == "reservation count":
                    paths.claims.mkdir()
                    mac_test.write_json(paths.claims / "duplicate.json", record)
                if check == "block state":
                    _ = paths.block.write_text("invalid block\n")
                before = mac_test.read_claims(paths)

                def read_start(pid: int) -> identity.ProcessStart:
                    if pid == 42:
                        return identity.Gone() if check == "runner pid start" else identity.Started("42000")
                    return identity.Started("41000" if check == "proc_start" else "40000")

                errors = io.StringIO()
                with mock.patch.object(identity, "process_start", side_effect=read_start), redirect_stderr(errors), \
                        redirect_stdout(io.StringIO()):
                    result = mac_test.adopt_wait_claim(args, paths, scope, session=session)
                self.assertEqual(result, mac_test.ClaimFinished(11 if check == "leased slot" else 12))
                self.assertEqual(errors.getvalue().splitlines(), [f"cannot adopt the Mac claim: {check}"])
                self.assertEqual(mac_test.read_claims(paths), before)

    def test_assigned_claims_keep_provider_overlap_rules_and_never_substitute_a_linux_slot(self) -> None:
        cases: tuple[tuple[Literal["mac", "ssh"], claim_state.ClaimScope, claim_state.ClaimScope, list[int]], ...] = (
            ("mac", mac_test.RepositoryScope("hana"), mac_test.RepositoryScope("hana"), [0, 11]),
            ("mac", mac_test.RepositoryScope("hana"), mac_test.RepositoryScope("other"), [0, 0]),
            ("mac", mac_test.WHOLE_MAC, mac_test.RepositoryScope("hana"), [0, 11]),
            ("ssh", mac_test.RepositoryScope("hana", "repo-key", 1), mac_test.RepositoryScope("hana", "repo-key", 1), [0, 11]),
        )
        for provider, first_scope, second_scope, expected in cases:
            with self.subTest(provider=provider, first=first_scope, second=second_scope), PlacementFixture() as fixture:
                target = "mac" if provider == "mac" else "box"
                _ = (fixture.root / "build_targets.conf").write_text(
                    "targets=mac,box\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
                    + "kind.box=ssh\nplatform.box=linux\nsettings.box=box.conf\n" + UNREACHABLE_WAIT_FIXTURE)
                _ = write_config_fixture(fixture.root / "box.conf", "offload=on\nslots=2\n", template="mac_test")
                paths = mac_test.StatePaths(fixture.root / "provider", target,
                    platform="macos" if provider == "mac" else "linux")
                assignments: list[placement.PlacementAssignment] = []
                requests: list[wire.PlacementRequest] = []
                for scope in (first_scope, second_scope):
                    request = placement_request(fixture)
                    destination = placement.PlacementDestination(provider, target, "fixture-host", scope,
                        scope.slot if provider == "ssh" and isinstance(scope, mac_test.RepositoryScope) else 0)
                    request = replace(request, constraints=replace(request.constraints,
                        candidates=(placement.PlacementCandidate(destination),)))
                    _ = publish_candidate(fixture, request)
                    assignments.append(assignment_for(fixture, request))
                    requests.append(request)

                def acquire(assignment: placement.PlacementAssignment) -> int:
                    args = mac_test.CliArguments()
                    args.pid, args.target, args.worktree = assignment.owner.pid, target, str(fixture.root)
                    args.what = "nextest hana"
                    return mac_test.command_claim(args, paths, watch_claim=False,
                        session=assignment.session, assignment=assignment)

                with redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=2) as workers:
                    statuses = list(workers.map(acquire, assignments))
                self.assertEqual(sorted(statuses), expected)
                held = mac_test.read_claims(paths)
                self.assertEqual(len(held), expected.count(0))
                if provider == "ssh":
                    self.assertEqual([claim.scope.slot for claim in held if isinstance(claim.scope, mac_test.RepositoryScope)], [1])
                authorizations = 0
                for request, assigned, status in zip(requests, assignments, statuses, strict=True):
                    if status != 0:
                        continue
                    claim, = [claim for claim in held if claim.record["pid"] == assigned.owner.pid]
                    exact = claim_state.claim_identity(claim.record)
                    args = mac_test.CliArguments()
                    args.pid, args.from_pid, args.target = assigned.owner.pid, assigned.owner.pid, target
                    args.worktree, args.remote = str(fixture.root), assigned.destination.host
                    args.claim_id, args.episode, args.attempt = exact.claim_id, exact.episode, exact.attempt
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(mac_test.command_claim(args, paths, watch_claim=False,
                            session=assigned.session, assignment=assigned), 0)
                    reference = placement.ProviderClaimReference(provider, target, assigned.destination.scope,
                        exact.claim_id, exact.episode, exact.attempt)
                    holder = coordinator_holder(assigned)
                    adopted = placement.AdoptedProviderClaim(reference, holder, assigned.session,
                        assigned.destination.host, "host-generation-a")
                    _ = publish_candidate(fixture, request, claims=(adopted,))
                    reply, = fixture.connect(wire.ConsumePermission("winner-" + assigned.episode,
                        assigned, reference, holder)).replies()
                    self.assertIsInstance(reply, wire.FirstAuthorization)
                    authorizations += isinstance(reply, wire.FirstAuthorization)
                self.assertEqual(authorizations, expected.count(0))

    def test_darwin_adoption_requires_both_live_start_matches(self) -> None:
        old_start = "Tue Nov  3 23:00:00 2026"
        new_start = "Wed Nov  4 00:00:00 2026"
        matching = claim_state.ClaimIdentity("claim-a", "episode-a", "attempt-a")
        cases = (
            (True, True, matching, NoOwningSession(), False, 0),
            (False, True, matching, NoOwningSession(), False, 12),
            (True, False, matching, NoOwningSession(), False, 12),
            (True, True, claim_state.ClaimIdentity("claim-b", "episode-a", "attempt-a"), NoOwningSession(), False, 12),
            (True, True, claim_state.ClaimIdentity("claim-a", "episode-b", "attempt-a"), NoOwningSession(), False, 12),
            (True, True, claim_state.ClaimIdentity("claim-a", "episode-a", "attempt-b"), NoOwningSession(), False, 12),
            (True, True, matching, OwningSession("another-session"), False, 12),
            (True, True, claim_state.ClaimIdentity("claim-b", "episode-a", "attempt-b"), OwningSession("another-session"), False, 12),
            (True, True, matching, OwningSession("cleanup-session"), True, 13),
            (True, True, claim_state.ClaimIdentity("claim-b", "episode-a", "attempt-a"), OwningSession("cleanup-session"), True, 12),
            (True, False, matching, OwningSession("cleanup-session"), True, 12),
        )
        for owner_matches, runner_live, requested, session, cleanup, expected in cases:
            with self.subTest(owner_matches=owner_matches, runner_live=runner_live,
                              identity=requested, session=session, cleanup=cleanup), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = mac_test.StatePaths(root)
                mac_test.write_json(paths.run, {"claim_id": "claim-a", "episode": "episode-a", "attempt": "attempt-a",
                    "submitting_session": "original-session" if cleanup else "", "pid": 41, "proc_start": old_start, "what": "waiting",
                    "worktree": str(root), "since": "2026-11-04T00:00:00+00:00"})
                if cleanup:
                    retained = mac_test.read_run(paths.run)
                    assert not isinstance(retained, mac_test.NoRun)
                    mac_test.write_json(paths.run, {**retained, "remote_host": "mac"})
                args = mac_test.CliArguments()
                args.pid, args.from_pid, args.worktree, args.remote = 42, 41, str(root), "mac"
                args.claim_id, args.episode, args.attempt = requested.claim_id, requested.episode, requested.attempt

                def ps(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    pid = command[2]
                    if pid == "42" and not runner_live:
                        return subprocess.CompletedProcess(command, 1, "", "")
                    start = old_start if pid == "41" and owner_matches else new_start
                    return subprocess.CompletedProcess(command, 0, start + " S\n", "")

                with mock.patch.object(sys, "platform", "darwin"), \
                        mock.patch.object(subprocess, "run", side_effect=ps), \
                        mock.patch.object(mac_test, "target_claim_scope", return_value=mac_test.WHOLE_MAC), \
                        redirect_stdout(io.StringIO()):
                    if cleanup:
                        held = mac_test.read_claims(paths)[0]
                        assert "remote_host" in held.record
                        takeover = mac_test.CliArguments()
                        takeover.pid = 41
                        transition = mac_test.take_over_kept_claim(
                            takeover, held, held.record, session=OwningSession("cleanup-session"))
                        self.assertIsInstance(transition, mac_test.ClaimFinished)
                        assert isinstance(transition, mac_test.ClaimFinished)
                        self.assertEqual(transition.status, 13)
                        before = paths.run.read_bytes()
                        unrelated = mac_test.adopt_wait_claim(
                            args, paths, mac_test.WHOLE_MAC, session=OwningSession("unrelated-session"))
                        self.assertIsInstance(unrelated, mac_test.ClaimFinished)
                        assert isinstance(unrelated, mac_test.ClaimFinished)
                        self.assertEqual(unrelated.status, 12)
                        self.assertEqual(paths.run.read_bytes(), before)
                    if cleanup and expected == 13:
                        assigned = placement.PlacementAssignment("new-episode", "new-attempt",
                            placement.PlacementDestination("mac", "mac", "mac", mac_test.WHOLE_MAC),
                            execution_identity.SourceKeyUnavailable("the retained cleanup has no source key"),
                            placement.SubmittingOwner("natedev", 42, new_start), session, 0)
                        result_status = mac_test.command_claim(args, paths, watch_claim=False,
                            session=session, assignment=assigned)
                        result = mac_test.ClaimFinished(result_status)
                    else:
                        result = mac_test.adopt_wait_claim(args, paths, mac_test.WHOLE_MAC, session=session)
                self.assertIsInstance(result, mac_test.ClaimFinished)
                assert isinstance(result, mac_test.ClaimFinished)
                self.assertEqual(result.status, expected)
                saved = mac_test.read_run(paths.run)
                self.assertNotIsInstance(saved, mac_test.NoRun)
                assert not isinstance(saved, mac_test.NoRun)
                self.assertEqual(saved["pid"], 42 if expected in (0, 13) else 41)
                self.assertEqual(saved["proc_start"], new_start if expected in (0, 13) else old_start)
                self.assertEqual((saved["claim_id"], saved["episode"], saved["attempt"]),
                                 ("claim-a", "episode-a", "attempt-a"))

    def test_pid_reuse_and_unreadable_start_never_allow_a_signal(self) -> None:
        claim: mac_test.ProcessBoundClaim = {"claim_id": "claim-a", "episode": "episode-a", "attempt": "attempt-a",
            "submitting_session": "", "pid": os.getpid(), "proc_start": "old", "what": "copy",
            "worktree": "/tmp/test", "since": "2026-11-04T00:00:00+00:00", "copy": True}
        for result in (identity.Started("different"), identity.Zombie("old"), identity.Gone(), identity.Unreadable("denied")):
            with self.subTest(result=type(result)), mock.patch.object(identity, "process_start", return_value=result), \
                    mock.patch.object(os, "kill") as send:
                self.assertFalse(mac_test.run_is_live(claim))
                self.assertFalse(mac_test.copy_gives_way(claim))
                mac_test.stop_copy(claim)
                send.assert_not_called()
