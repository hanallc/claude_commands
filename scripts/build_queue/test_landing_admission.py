"""Exercise landing precedence through private admission sockets."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext, redirect_stderr
from dataclasses import replace
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, call, patch

from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_records as records
from scripts.build_queue import first_step, memory_feed, placement, queue_place, queue_view, step_estimator
from scripts.build_queue.admission_test_support import (AdmissionFixture, Exchange, GIB, ScriptedTransport, ScriptSocket,
    admitted_reply, only_admitted, reply_bytes, wait_reply)
from scripts.build_queue.test_placement import (PlacementFixture, adopt_assignment, assignment_for, consume_permission,
    placement_request, publish_candidate)
from scripts.buildlog import repository
from scripts.lib import build_folders
from scripts.lib.process_start import Started
from scripts.lib.test_hang_guard import LauncherHangGuard
from scripts.lint import memory_admit as gate
from scripts.production import land_queue, land_runs, staff
from scripts.production.staff_test_support import fixture_runtime_id


REPOSITORY = records.RepositoryIdentity("https://github.com/natepiano/claude_commands", "claude_commands")
LANDING_REPOSITORY = wire.LandingRepositoryKey(REPOSITORY.key)
COMMIT = "a" * 40
WAIT_TEXT = "waits for the landing queue"


class LandingWaitObserved(Exception):
    """End the client call after the service sends a landing wait."""


def queue_land() -> None:
    _ = land_queue.take_next(os.getpid())
    land_queue.add("builds first", batch="", commits=[COMMIT], first=False, before=None)


def local_test_clone(root: Path, source_name: str = "source", upstream: str = REPOSITORY.key) -> Path:
    source = root / source_name
    clone = root / "clone"
    git("init", "-q", str(source))
    git("-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-qm", "Source")
    git("-C", str(source), "remote", "add", "origin", upstream)
    git("clone", "-q", "--no-local", str(source), str(clone))
    git("-C", str(clone), "remote", "set-url", "origin", f"../{source_name}")
    return clone


def running_gate_request(fixture: AdmissionFixture, *, run: bool = False) -> wire.AdmitRequest:
    request = fixture.request(step="gate", need=GIB)
    profile = records.ProgramProfile("gate", "fixture", request.cwd,
        records.HistoryPeakSizing(GIB, records.FirstProgramPeak(GIB), fixture.clock.now()))
    identity = wire.RunIdentity(request.owner.pid, request.owner.start_time) if run else wire.NO_RUN
    with patch.object(client, "CLOCK", fixture.clock):
        request = client.step_request(["gate"], request.cwd, request.owner, request.since, "",
                                      request.episode, profile, run=identity)
    return replace(request, identity=replace(request.identity, landing_repository=LANDING_REPOSITORY))


def request_behind_gate(fixture: AdmissionFixture, *, cloud: bool = False) -> wire.AdmitRequest:
    waiting = fixture.request(step="nextest", need=GIB)
    return replace(waiting, identity=replace(waiting.identity, unit="queue", landing_repository=LANDING_REPOSITORY),
                   place=wire.CloudPlace("aws") if cloud else wire.LocalPlace())


class LandingAdmissionTests(unittest.TestCase):
    def test_wait_names_the_live_gate_job_from_its_command_and_configuration(self) -> None:
        session = staff.StaffId(fixture_runtime_id("natemerge"))
        owner = staff.StaffRow(session, "unit", "natemerge", staff.StaffRoot(), current_session=session)
        for options, key, step in (((), "integrate", "integrate"), (("--tests-only",), "test-all", "test all")):
            with (self.subTest(options=options), AdmissionFixture(skip_idle_spacing=True) as fixture,
                  patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session, "CODEX_THREAD_ID": "",
                                          records.BUILDLOG_UNIT: "builds"}),
                  patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((owner,))),
                  land_runs.landing([COMMIT], ["builds bb15"])):
                config = fixture.root / "build_steps.conf"
                _ = config.write_text(f"names.{key}={step}\nowned.default={{unit}} {{step}}\n")
                running = running_gate_request(fixture, run=True)
                self.assertEqual(running.run, wire.RunIdentity(running.owner.pid, running.owner.start_time))
                command = ("python3", "/fixture/gate.py", "--unit", "builds", *options)
                _ = (fixture.proc.path / str(running.owner.pid) / "cmdline").write_text("\0".join(command) + "\0")
                grant = only_admitted(fixture.connect(running).replies())
                peer = fixture.connect(request_behind_gate(fixture))
                reply, = peer.replies()
                assert isinstance(reply, wire.Wait), reply
                self.assertEqual(reply.text, f"waits for natemerge {step}")

                _ = config.write_text(f"names.{key}=check the batch\nowned.default={{unit}} {{step}}\n")
                _ = fixture.run_pass()
                view, = fixture.connect(wire.ViewRequest()).replies()
                assert isinstance(view, wire.ViewReply)
                row, = view.reading.view.waiting
                self.assertEqual(records.reason_phrase(row.reason, ()), "waits for natemerge check the batch")
                fixture.release(grant)

    def assert_landing_wait(self, fixture: AdmissionFixture, request: wire.AdmitRequest,
                            check: Callable[[], None] = lambda: None) -> None:
        def wait(reply: wire.Wait) -> None:
            self.assertEqual(reply.reason, records.WaitsForLandingTest())
            check()
            raise LandingWaitObserved()

        with LauncherHangGuard("Landing wait").protect(), fixture.serving(), self.assertRaises(LandingWaitObserved):
            _ = client.admit(request, wait, lambda: request)

    def test_a_copy_first_step_has_the_landing_reference_before_its_child_starts(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture:
            fixture.configure(**{"program_first_peak_gib.gate": 4.0})
            queue_land()

            def admit(refreshed: queue_place.RefreshedRequest, _output: client.WaitOutput) -> wire.Admitted:
                request = refreshed.refresh()
                grant = only_admitted(fixture.connect(request).replies())
                reference = request.identity.landing_test
                assert isinstance(reference, land_runs.LandingTestReference)
                self.assertEqual(os.environ[land_runs.TEST_REFERENCE_ENV], reference.token)
                self.assertIn(reference, land_runs.running_test_references())
                return grant

            command = ("python3", "/fixture/gate.py", "--merge-branch", "merge", "--unit-branch", "topic",
                       "--checkpoint", COMMIT, "--previous-checkpoint", "b" * 40, "--unit", "queue", "--phase", "44eb")
            with (patch.object(client, "CLOCK", fixture.clock),
                  patch.object(repository, "repository_key", return_value=REPOSITORY.key),
                  patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.temporary)),
                  patch.object(queue_place, "request_place", side_effect=admit),
                  patch.object(queue_place, "release_place", side_effect=fixture_release(fixture))):
                with first_step.admitted_run(command, fixture.root) as admission:
                    self.assertIsInstance(admission.grant, wire.Admitted)
                    admission.handoff(fixture.root)
                    fixture.allow_next_start()
                    child = fixture.request(step="gate", need=GIB)
                    sizing = records.HistoryPeakSizing(GIB, records.FirstProgramPeak(GIB), fixture.clock.now())
                    profile = records.ProgramProfile("gate", "fixture", child.cwd, sizing)
                    child = client.step_request(["gate"], child.cwd, child.owner, child.since, "",
                                                child.episode, profile)
                    fixture.release(only_admitted(fixture.connect(child).replies()))
            self.assertFalse(land_runs.running_test_references())
            self.assertEqual(fixture.caller_reservations(), {})

    def test_a_landing_that_enters_mid_pass_holds_local_and_cloud_grants(self) -> None:
        for cloud in (False, True):
            with self.subTest(cloud=cloud), AdmissionFixture(skip_idle_spacing=True) as fixture:
                request = fixture.request(step="test", need=GIB)
                request = replace(request, identity=replace(request.identity, landing_repository=LANDING_REPOSITORY),
                                  place=wire.CloudPlace("aws") if cloud else wire.LocalPlace())
                decide = gate.admission_pass
                queued = False

                def decide_then_queue(inputs: records.QueueInputs, now: float, policy: records.AdmissionPolicy) -> gate.PassResult:
                    nonlocal queued
                    result = decide(inputs, now, policy)
                    if not queued:
                        queue_land()
                        queued = True
                    return result

                with patch.object(gate, "admission_pass", side_effect=decide_then_queue):
                    replies = fixture.connect(request).replies()
                assert isinstance(reply := replies[-1], wire.Wait), replies
                self.assertEqual(reply.reason, records.WaitsForLandingTest())
                self.assertEqual(fixture.caller_reservations(), {})

    def test_a_relative_local_clone_resolves_each_origin_once_on_the_client(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture:
            clone = local_test_clone(fixture.root)
            self.assertFalse((clone / ".git/objects/info/alternates").exists())
            workdir = clone / "nested"
            workdir.mkdir()
            queue_land()
            owner = fixture.request(step="test", need=GIB)
            request = client.step_request(["cargo", "test"], str(workdir), owner.owner, owner.since, "", owner.episode)
            with patch.object(repository, "output", wraps=repository.output) as lookup:
                def check_lookup() -> None:
                    expected = [call(["git", "-C", str(root), "config", "--get-all", "remote.origin.url"], str(root), 5)
                                for root in (clone, fixture.root / "source")]
                    self.assertEqual(lookup.call_args_list, expected)
                    _ = fixture.run_pass()
                    _ = fixture.run_pass()
                    self.assertEqual(lookup.call_args_list, expected)

                self.assert_landing_wait(fixture, request, check_lookup)

    def test_a_relative_origin_can_name_a_checkout_with_a_dot_git_suffix(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture:
            clone = local_test_clone(fixture.root, "source.git")
            self.assertFalse((fixture.root / "source").exists())
            queue_land()
            owner = fixture.request(step="test", need=GIB)
            request = client.step_request(["cargo", "test"], str(clone), owner.owner,
                                          owner.since, "", owner.episode)
            self.assert_landing_wait(fixture, request)

    def test_separate_placement_calls_keep_the_landing_wait_after_the_origin_disappears(self) -> None:
        self.assert_placement_wait_survives_missing_origin(client.request_placement)

    def test_fresh_placement_clients_keep_the_landing_wait_after_the_origin_disappears(self) -> None:
        def submit(request: wire.PlacementRequest) -> wire.Reply:
            code = (
                "import sys\n"
                "from scripts.build_queue import admission_client as client, admission_protocol as wire\n"
                "request = wire.parse_request(sys.stdin.read())\n"
                "assert isinstance(request, wire.PlacementRequest)\n"
                "print(wire.encode_reply(client.request_placement(request)), end='')"
            )
            result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
                input=wire.encode_request(request), capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return wire.parse_reply(result.stdout)

        self.assert_placement_wait_survives_missing_origin(submit)

    def assert_placement_wait_survives_missing_origin(self, submit: Callable[[wire.PlacementRequest], wire.Reply]) -> None:
        with PlacementFixture() as fixture:
            clone = local_test_clone(fixture.root)
            request = placement_request(fixture)
            assert isinstance(request.local, wire.LocalAdmissionContext)
            request = replace(request, local=replace(request.local, cwd=str(clone)))
            _ = publish_candidate(fixture, request)
            queue_land()

            with (LauncherHangGuard("Separate landing placement calls").protect(),
                  patch.object(client, "CLOCK", fixture.clock), fixture.serving()):
                first = submit(request)
                assert isinstance(first, wire.PlacementWait) and isinstance(first.wait, wire.Wait), first
                self.assertEqual(first.wait.reason, records.WaitsForLandingTest())

                _ = (fixture.root / "source").rename(fixture.root / "source-away")
                second = submit(request)
                assert isinstance(second, wire.PlacementWait) and isinstance(second.wait, wire.Wait), second
                self.assertEqual(second.wait.reason, records.WaitsForLandingTest())
                self.assertEqual((second.episode, second.since), (first.episode, first.since))

    def test_origin_headers_with_trailing_comments_resolve(self) -> None:
        for comment in ("#", ";"):
            with self.subTest(comment=comment), AdmissionFixture() as fixture:
                git("init", "-q", str(fixture.root))
                config = fixture.root / ".git/config"
                with config.open("a", encoding="utf-8") as settings:
                    _ = settings.write(f'  [remote "origin"]\t{comment} Source repository\n\turl = {REPOSITORY.key}\n')
                queue_land()
                owner = fixture.request(step="test", need=GIB)
                request = client.step_request(["cargo", "test"], str(fixture.root), owner.owner,
                                              owner.since, "", owner.episode)
                self.assert_landing_wait(fixture, request)

    def test_relative_origin_url_with_a_semicolon_comment_resolves(self) -> None:
        with AdmissionFixture() as fixture:
            clone = local_test_clone(fixture.root)
            config = clone / ".git/config"
            _ = config.write_text(config.read_text().replace("url = ../source", "url = ../source ; Source repository"))
            queue_land()
            owner = fixture.request(step="test", need=GIB)
            request = client.step_request(["cargo", "test"], str(clone), owner.owner, owner.since, "", owner.episode)
            self.assert_landing_wait(fixture, request)

    def test_a_test_waits_when_the_first_of_two_origin_urls_is_landing(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture:
            git("init", "-q", str(fixture.root))
            git("-C", str(fixture.root), "remote", "add", "origin", REPOSITORY.key)
            git("-C", str(fixture.root), "remote", "set-url", "--add", "origin", "https://example.invalid/other")
            queue_land()
            owner = fixture.request(step="test", need=GIB)
            request = client.step_request(["cargo", "test"], str(fixture.root), owner.owner,
                                          owner.since, "", owner.episode)
            self.assert_landing_wait(fixture, request)

    def test_first_step_resends_a_placement_with_the_new_landing_key(self) -> None:
        with AdmissionFixture() as fixture:
            clone = local_test_clone(fixture.root)
            source = fixture.root / "source"
            unavailable = source.rename(fixture.root / "source-away")
            request = placement_request(fixture)
            assert isinstance(request.local, wire.LocalAdmissionContext)
            request = replace(request, local=replace(request.local, cwd=str(clone)))
            assignment = placement.PlacementAssignment(request.episode, "first-step", request.constraints.candidates[0].destination,
                request.constraints.source_key, request.owner, request.constraints.session, request.since)
            expired = wire.PlacementWait(request.episode, request.since, 1, (),
                wait_reply(records.EstimateExpired(1), text="estimate expired"))
            transport = ScriptedTransport(fixture.clock, Exchange(reply_bytes(expired)),
                Exchange(reply_bytes(wire.Assignment(assignment))), Exchange(reply_bytes(wire.Ok())))

            def restore_origin(_reply: wire.Wait) -> None:
                _ = unavailable.rename(source)

            with (patch.object(client, "CLOCK", fixture.clock),
                  patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket),
                  patch.object(client.WaitOutput, "__call__", side_effect=restore_origin),
                  patch.object(first_step, "_verify_request", return_value=request),
                  patch.object(first_step, "local_release_waiter", return_value=nullcontext(Mock())),
                  patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.temporary))):
                with first_step.admitted_run(("bash", "/fixture/verify.sh", "test"), clone) as run:
                    self.assertEqual(run.grant, wire.Assignment(assignment))
            initial, refreshed, _ = transport.requests
            assert isinstance(initial, wire.PlacementRequest) and isinstance(refreshed, wire.PlacementRequest)
            self.assertEqual(initial.identity.landing_repository, wire.LandingRepositoryNotRecorded())
            self.assertEqual(refreshed.identity.landing_repository, LANDING_REPOSITORY)
            self.assertEqual((refreshed.episode, refreshed.owner, refreshed.since),
                             (initial.episode, initial.owner, initial.since))

    def test_an_admission_refresh_keeps_the_landing_key_after_the_origin_disappears(self) -> None:
        with AdmissionFixture() as fixture, patch.object(client, "CLOCK", fixture.clock):
            clone = local_test_clone(fixture.root)
            seed = fixture.request(step="test")

            def refresh() -> wire.AdmitRequest:
                return client.step_request(["cargo", "test"], str(clone), seed.owner, seed.since, "", seed.episode)

            def remove_origin(_reply: wire.Wait) -> None:
                _ = (fixture.root / "source").rename(fixture.root / "source-away")

            grant = admitted_reply()
            expired = wait_reply(records.EstimateExpired(1), text="estimate expired")
            transport = ScriptedTransport(fixture.clock,
                Exchange(reply_bytes(expired), after_send={2: (reply_bytes(grant),)}))
            with (patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket),
                  patch.object(client, "DefaultSelector", side_effect=transport.selector)):
                self.assertEqual(client.admit(refresh(), remove_origin, refresh), grant)
            initial, refreshed = transport.requests
            assert isinstance(initial, wire.AdmitRequest) and isinstance(refreshed, wire.AdmitRequest)
            self.assertEqual(initial.identity.landing_repository, LANDING_REPOSITORY)
            self.assertEqual(refreshed.identity, initial.identity)

    def test_ci_resends_an_estimate_request_with_the_new_landing_key(self) -> None:
        with AdmissionFixture() as fixture, patch.object(client, "CLOCK", fixture.clock):
            clone = local_test_clone(fixture.root)
            source = fixture.root / "source"
            unavailable = source.rename(fixture.root / "source-away")
            request = replace(fixture.service_request(), argv=("cargo", "test"), cwd=str(clone))

            def restore_origin(_reply: wire.Wait) -> None:
                _ = unavailable.rename(source)

            grant = admitted_reply()
            expired = wait_reply(records.EstimateExpired(1), text="estimate expired")
            transport = ScriptedTransport(fixture.clock,
                Exchange(reply_bytes(expired), after_send={2: (reply_bytes(grant),)}))
            with (patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket),
                  patch.object(client, "DefaultSelector", side_effect=transport.selector)):
                self.assertEqual(client.service_admit(request, restore_origin), grant)
            initial, refreshed = transport.requests
            assert isinstance(initial, wire.ServiceEstimateRequest)
            self.assertEqual(initial.landing_repository, wire.LandingRepositoryNotRecorded())
            self.assertEqual(refreshed, replace(initial, landing_repository=LANDING_REPOSITORY))

    def test_ci_reconnect_sends_the_landing_key_from_a_partial_refresh(self) -> None:
        with AdmissionFixture() as fixture, patch.object(client, "CLOCK", fixture.clock):
            timing = wire.AcquisitionTiming(silence_seconds=2, reconnect_seconds=1, unreachable_limit_seconds=9)
            clone = local_test_clone(fixture.root)
            source = fixture.root / "source"
            unavailable = source.rename(fixture.root / "source-away")
            request = replace(fixture.service_request(), argv=("cargo", "test"), cwd=str(clone))
            expired = wait_reply(records.EstimateExpired(1), text="estimate expired")
            grant = admitted_reply()
            original = Exchange(reply_bytes(expired))
            replacement = Exchange(reply_bytes(grant))
            transport = ScriptedTransport(fixture.clock, original, replacement)
            send = ScriptSocket.send
            prefix = b""

            def restore_origin(_reply: wire.Wait) -> None:
                _ = unavailable.rename(source)

            def write(peer: ScriptSocket, data: bytes) -> int:
                nonlocal prefix
                if peer.exchange is not original or peer.send_count == 0:
                    return send(peer, data)
                if not prefix:
                    prefix = data[:-1]
                    original.writable = False
                    _ = source.rename(unavailable)
                    return len(prefix)
                _ = send(peer, prefix + data)
                return len(data)

            def resume_write() -> None:
                original.writable = True

            transport.on_selector_timeout = resume_write
            with (patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket),
                  patch.object(client, "DefaultSelector", side_effect=transport.selector),
                  patch.object(ScriptSocket, "send", write), redirect_stderr(io.StringIO())):
                self.assertEqual(client.service_admit(request, restore_origin, timing), grant)

            self.assertEqual(transport.attempts, [0, 2])
            self.assertEqual([peer.send_count for peer in transport.sockets], [2, 1])
            refreshed = replace(request, landing_repository=LANDING_REPOSITORY)
            self.assertEqual(transport.requests, [request, refreshed, refreshed])

    def test_a_refreshed_test_waits_for_the_landing_when_its_origin_becomes_readable(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture, patch.object(client, "CLOCK", fixture.clock):
            clone = local_test_clone(fixture.root)
            source = fixture.root / "source"
            unavailable = source.rename(fixture.root / "source-away")
            queue_land()
            owner = fixture.request(step="test", need=GIB)
            refresh_seconds = records.read_memory_config().policy.timing.estimate_refresh_seconds

            def request() -> wire.AdmitRequest:
                return client.step_request(["cargo", "test"], str(clone), owner.owner, owner.since, "", owner.episode)

            first = request()
            self.assertEqual(first.identity.landing_repository, wire.LandingRepositoryNotRecorded())
            fixture.clock.advance(refresh_seconds + 1)
            waited_for_landing = False

            def wait(reply: wire.Wait) -> None:
                nonlocal waited_for_landing
                reason = reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason
                if isinstance(reason, records.WaitsForLandingTest):
                    self.assertTrue(source.is_dir())
                    self.assertEqual(fixture.caller_reservations(), {})
                    if not waited_for_landing:
                        self.assertEqual(land_queue.take_next(os.getpid()), "builds first")
                        land_runs.close_claim(os.getpid())
                        waited_for_landing = True
                elif isinstance(reason, records.EstimateExpired):
                    self.assertFalse(source.exists())
                    _ = unavailable.rename(source)

            with LauncherHangGuard("Landing estimate refresh").protect(), fixture.serving():
                result = client.admit(first, wait, request)
            self.assertTrue(source.is_dir(), f"the client did not request a refreshed estimate: {result!r}")
            self.assertTrue(waited_for_landing, "the refreshed request bypassed landing protection")
            assert isinstance(result, wire.Admitted), result
            fixture.release(result)

    def test_a_local_clone_keeps_its_history_key_for_an_unrelated_test(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture:
            clone = local_test_clone(fixture.root, upstream="https://github.com/fixture/other")
            queue_land()
            argv = ["cargo", "nextest", "run"]
            source, _ = step_estimator.git_identity(argv, str(clone))
            assert isinstance(source, records.RepositoryIdentity)
            self.assertEqual(source.key, str(fixture.root / "source"))
            owner = fixture.request(step="nextest", need=GIB)
            request = client.step_request(argv, str(clone), owner.owner, owner.since, "", owner.episode)
            self.assertEqual(request.profile.repository, source)
            with LauncherHangGuard("Unrelated test admission").protect(), fixture.serving():
                grant = client.admit(request, lambda reply: self.fail(str(reply.reason)), lambda: request)
            assert isinstance(grant, wire.Admitted), grant
            self.assertEqual(fixture.server.waiters, {})
            fixture.release(grant)

    def test_service_protection_never_starts_git(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture:
            git("init", "-q", str(fixture.root))
            git("-C", str(fixture.root), "remote", "add", "origin", REPOSITORY.key)
            queue_land()
            known = fixture.request(step="test", need=GIB)
            known = replace(known, cwd=str(fixture.root),
                            identity=replace(known.identity, landing_repository=LANDING_REPOSITORY))
            unknown = fixture.request(step="test", need=GIB)
            unknown = replace(unknown, cwd=str(fixture.root), profile=replace(unknown.profile, repository=REPOSITORY))
            self.assertNotIn("landing_repository", wire.encode_request(unknown))

            with patch("scripts.build_queue.admission_service.subprocess.Popen",
                       side_effect=AssertionError("service started a subprocess while deciding landing protection")):
                wait, = fixture.connect(known).replies()
                assert isinstance(wait, wire.Wait)
                self.assertEqual(wait.reason, records.WaitsForLandingTest())
                fixture.release(only_admitted(fixture.connect(unknown).replies()))

    def test_four_older_tests_wait_and_keep_their_order_after_the_land(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture:
            clone = local_test_clone(fixture.root)
            queue_land()
            requests: list[wire.AdmitRequest] = []
            for index, step in enumerate(("gate", "test-affected", "test", "t")):
                request = fixture.request(need=GIB, step=step, since=fixture.clock.now() - 40 + index)
                requests.append(replace(request, identity=replace(request.identity, landing_repository=LANDING_REPOSITORY)))
            peers = [fixture.connect(request) for request in reversed(requests)]
            for peer in peers:
                reply = peer.replies()[-1]
                assert isinstance(reply, wire.Wait)
                self.assertEqual((reply.reason, reply.text), (records.WaitsForLandingTest(), WAIT_TEXT))
            self.assertEqual(fixture.caller_reservations(), {})
            view, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(view, wire.ViewReply)
            self.assertIsInstance(view.reading.explanation.head, records.NoWaiter)
            self.assertEqual([row.since for row in view.reading.view.waiting], [request.since for request in requests])
            reason = view.reading.view.waiting[0].reason
            self.assertEqual(records.reason_phrase(reason, ()), WAIT_TEXT)
            self.assertEqual(memory_feed.wait_why(reason), "gate job")
            other = only_admitted(fixture.connect(fixture.request(need=GIB, step="test")).replies())
            after_grant, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(after_grant, wire.ViewReply)
            self.assertIsInstance(after_grant.reading.explanation.head, records.NoWaiter)
            self.assertEqual(after_grant.reading.view.waiting, view.reading.view.waiting)
            fixture.release(other)
            fixture.allow_next_start()
            with patch.object(land_runs, "process_start", return_value=Started("10")):
                self.assertEqual(land_queue.take_next(123), "builds first")
                with land_runs.landing([COMMIT]):
                    request = fixture.request(need=GIB, step="gate")
                    sizing = records.HistoryPeakSizing(GIB, records.FirstProgramPeak(GIB), fixture.clock.now())
                    profile = records.ProgramProfile("gate", "fixture", request.cwd, sizing)
                    transport = ScriptedTransport(fixture.clock, Exchange(reply_bytes(admitted_reply())))
                    with (patch.object(client, "CLOCK", fixture.clock),
                          patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket),
                          patch.object(client, "DefaultSelector", side_effect=transport.selector)):
                        landing = client.step_request(["gate"], str(clone), request.owner, request.since,
                                                      "", request.episode, profile)
                        _ = client.admit(landing, lambda reply: self.fail(str(reply.reason)), lambda: landing)
                    sent, = transport.requests
                    assert isinstance(sent, wire.AdmitRequest)
                    self.assertEqual(landing.profile, profile)
                    self.assertEqual(sent.identity.landing_repository, LANDING_REPOSITORY)
                    self.assertEqual(landing.identity.landing_test, land_runs.test_reference())
                    grant = only_admitted(fixture.connect(sent).replies())
                    self.assertEqual(list(fixture.caller_reservations()), [landing.episode])
                    fixture.release(grant)
                    _ = fixture.run_pass()
                    self.assertEqual(fixture.caller_reservations(), {})
            for index, request in enumerate(requests):
                fixture.allow_next_start()
                _ = fixture.run_pass()
                self.assertEqual(list(fixture.caller_reservations()), [request.episode])
                fixture.release(only_admitted(peers[-index - 1].replies()))

    def test_cloud_work_reports_the_landing_wait_and_yields_the_slot(self) -> None:
        with AdmissionFixture() as fixture, patch.object(land_runs, "process_start", return_value=Started("10")):
            queue_land()
            request = fixture.request(step="gate")
            request = replace(request, identity=replace(request.identity, landing_repository=LANDING_REPOSITORY),
                              place=wire.CloudPlace("aws"))
            wait, = fixture.connect(request).replies()
            assert isinstance(wait, wire.Wait)
            self.assertEqual(wait.reason, records.WaitsForLandingTest())
            with redirect_stderr(io.StringIO()) as output:
                client.WaitOutput()(wait)
            self.assertIn(WAIT_TEXT, output.getvalue())
            view, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(view, wire.ViewReply) and isinstance(view.reading, wire.CloudQueueRead)
            row, = view.reading.cloud
            self.assertEqual(row.wait, records.WaitsForLandingTest())
            work = queue_view.cloud_work(row, fixture.clock.now(), 1, queue_view.CloudPushIntervalUnavailable("fixture"))
            self.assertEqual(work.state, WAIT_TEXT)
            _ = land_queue.take_next(123)
            with land_runs.landing([COMMIT]):
                landing = fixture.request(step="gate")
                identity = replace(landing.identity, landing_test=land_runs.test_reference(),
                                   landing_repository=LANDING_REPOSITORY)
                landing = replace(landing, identity=identity, profile=request.profile, place=request.place)
                _ = only_admitted(fixture.connect(landing).replies())
                self.assertEqual(list(fixture.caller_reservations()), [landing.episode])

    def test_routed_tests_wait_before_assignment_and_before_start_permission(self) -> None:
        with PlacementFixture() as fixture:
            earlier = placement_request(fixture)
            earlier = replace(earlier, identity=replace(earlier.identity, landing_repository=LANDING_REPOSITORY))
            _ = publish_candidate(fixture, earlier)
            assignment = assignment_for(fixture, earlier)
            claim = adopt_assignment(fixture, earlier, assignment)
            queue_land()
            reply, = fixture.server.handle_request(consume_permission("unit-start", assignment, claim))
            assert isinstance(reply, wire.DispatchStatus)
            self.assertEqual(reply.state, placement.ClosedAssignment(WAIT_TEXT))
            self.assertIsInstance(reply.permission, placement.PermissionAvailable)
            later = placement_request(fixture)
            later = replace(later, identity=replace(later.identity, landing_repository=LANDING_REPOSITORY))
            _ = publish_candidate(fixture, later)
            fixture.clock.advance(1)
            waiting, = fixture.connect(later).replies()
            assert isinstance(waiting, wire.PlacementWait) and isinstance(waiting.wait, wire.Wait)
            self.assertEqual(waiting.wait.reason, records.WaitsForLandingTest())
            self.assertNotIn(later.episode, fixture.server.current_assignments)


def git(*arguments: str) -> None:
    _ = subprocess.run(["git", *arguments], check=True, capture_output=True, timeout=10)


def fixture_release(fixture: AdmissionFixture):
    def release(reservation: str, status: int, _timing: wire.CleanupTiming) -> wire.Ok:
        response, = fixture.server.handle_request(wire.ReleaseRequest("release-copy", reservation, status))
        assert isinstance(response, wire.Ok)
        return response
    return release
