"""Repository capacity waits retain their order without reserving memory."""
from __future__ import annotations

from contextlib import AbstractContextManager, redirect_stderr
from dataclasses import replace
import fcntl
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import unittest
from unittest import mock
from typing import cast

from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_service as service, slot_wait
from scripts.build_queue.admission_test_support import AdmissionFixture, Exchange, ScriptedTransport, reply_bytes
from scripts.lib.build_slots_test_support import SlotTestConfiguration, private_slot_config
from scripts.lib.proc_text import read_proc_text


class VerifyPackageTests(unittest.TestCase):
    def test_failed_parse_retries_and_caches_the_next_package(self) -> None:
        cases = (
            ("timeout-package", subprocess.TimeoutExpired("bash", 2)),
            ("os-error-package", OSError("cannot start bash")),
            ("failed-exit-package", subprocess.CompletedProcess(["bash"], 2, stdout="partial", stderr="")),
            ("failed-extraction-package", AssertionError("cannot extract the parser")),
        )
        for package, failure in cases:
            success = subprocess.CompletedProcess(["bash"], 0, stdout=package, stderr="")
            with (self.subTest(package=package),
                  mock.patch.object(slot_wait, "verify_package_parser", return_value="") as extract,
                  mock.patch.object(subprocess, "run", return_value=success) as run):
                if isinstance(failure, AssertionError):
                    parse = extract
                    parse.side_effect = (failure, "")
                else:
                    parse = run
                    parse.side_effect = (failure, success)
                arguments = ("--filter", "name with spaces", package)
                self.assertEqual(slot_wait.verify_package("test", arguments), "")
                self.assertEqual(slot_wait.verify_package("test", arguments), package)
                self.assertEqual(slot_wait.verify_package("test", arguments), package)
                self.assertEqual(parse.call_count, 2)


class QueueRowNameTests(unittest.TestCase):
    def test_an_unknown_repository_uses_the_folder_name(self) -> None:
        self.assertEqual(slot_wait.QueueRowName.repository("unregistered session", "/private/widget"), "widget build")


class RepositorySlotWaitTests(unittest.TestCase):
    slot_lock_descriptors: tuple[int, ...] = ()

    def pool(self, maximum: int = 1) -> tuple[AdmissionFixture, SlotTestConfiguration]:
        fixture = self.enterContext(AdmissionFixture())
        settings = private_slot_config(fixture.root, repository="widget", maximum=maximum)
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, settings.environment)))
        self.slot_lock_descriptors = ()
        _ = self.enterContext(mock.patch.object(service, "read_proc_text", side_effect=self.read_fixture_locks))
        settings.slots.mkdir()
        return fixture, settings

    def hold(self, settings: SlotTestConfiguration, number: int = 1, repository: str = "widget") -> int:
        stream = self.enterContext((settings.slots / f"{repository}-slot{number}.lock").open("w"))
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        descriptor = stream.fileno()
        self.slot_lock_descriptors += (descriptor,)
        return descriptor

    def read_fixture_locks(self, path: Path) -> str:
        """Read only the fixture locks when the service asks for the lock list."""
        if path != Path("/proc/locks"):
            return read_proc_text(path)
        return "\n".join(
            line.removeprefix("lock:\t")
            for descriptor in self.slot_lock_descriptors
            for line in read_proc_text(Path(f"/proc/self/fdinfo/{descriptor}")).splitlines()
            if line.startswith("lock:\t")
        )

    def request(self, fixture: AdmissionFixture, settings: SlotTestConfiguration,
                repository: str = "widget") -> wire.RepositorySlotRequest:
        original = fixture.request()
        return wire.RepositorySlotRequest(original.episode, original.owner, original.since, repository,
            settings.machine, str(settings.slots), 1, original.since + 30,
            wire.Identity("slot-session", "slot-showrunner", "gate", "proof", "slot-unit"))

    def test_service_names_a_repository_slot_waiter_with_no_name(self) -> None:
        fixture, settings = self.pool()
        _ = self.hold(settings)
        request = self.request(fixture, settings)
        request = replace(request, identity=replace(request.identity, name=""))
        self.assertIsInstance(fixture.connect(request).replies()[-1], wire.RepositorySlotWaiting)
        reply, = fixture.connect(wire.ViewRequest()).replies()
        assert isinstance(reply, wire.ViewReply)
        assert isinstance(reply.reading, wire.CloudQueueRead)
        row, = reply.reading.repository_slots
        self.assertEqual(row.identity.name, "unregistered session")

    def test_capacity_grants_when_a_slot_frees_or_the_pool_can_grow(self) -> None:
        for state in ("released", "below maximum"):
            with self.subTest(state=state):
                fixture, settings = self.pool(maximum=2 if state == "below maximum" else 1)
                descriptor = self.hold(settings)
                request = self.request(fixture, settings)
                peer = fixture.connect(request)
                replies = peer.replies()
                if state == "released":
                    self.assertIsInstance(replies[-1], wire.RepositorySlotWaiting)
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    _ = fixture.server.run_pass()
                    replies = peer.replies()
                self.assertEqual(replies[-1], wire.RepositorySlotReady())
                self.assertFalse(fixture.server.reservations)
                self.assertFalse(fixture.server.waiters)
                self.assertFalse(fixture.server.repository_slot_waiters)
                self.doCleanups()

    def test_lost_race_retains_the_original_place_ahead_of_a_later_waiter(self) -> None:
        fixture, settings = self.pool()
        descriptor = self.hold(settings)
        first = self.request(fixture, settings)
        first_peer = fixture.connect(first)
        self.assertIsInstance(first_peer.replies()[-1], wire.RepositorySlotWaiting)
        fixture.clock.advance(1)
        second = self.request(fixture, settings)
        second_peer = fixture.connect(second)
        self.assertIsInstance(second_peer.replies()[-1], wire.RepositorySlotWaiting)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        _ = fixture.server.run_pass()
        self.assertEqual(first_peer.replies()[-1], wire.RepositorySlotReady())
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        retry_peer = fixture.connect(first)
        self.assertIsInstance(retry_peer.replies()[-1], wire.RepositorySlotWaiting)
        self.assertEqual(fixture.server.repository_slot_waiters[first.episode].request.since, first.since)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        _ = fixture.server.run_pass()
        self.assertEqual(retry_peer.replies()[-1], wire.RepositorySlotReady())
        self.assertIn(second.episode, fixture.server.repository_slot_waiters)

    def test_slot_retry_names_the_package_after_the_first_parse_times_out(self) -> None:
        fixture, settings = self.pool()
        request = self.request(fixture, settings)
        requests: list[wire.RepositorySlotRequest] = []

        def ready(attempt: wire.RepositorySlotRequest) -> wire.RepositorySlotReady:
            requests.append(attempt)
            return wire.RepositorySlotReady()

        command = ("bash", "/fixture/verify.sh", "test", "slot-retry-package")
        success = subprocess.CompletedProcess(["bash"], 0, stdout="slot-retry-package", stderr="")
        with (slot_wait.slot_caller("slot-session", "builds", fixture.root, command, "test"),
              mock.patch.object(client, "repository_slot", side_effect=ready),
              mock.patch.object(subprocess, "run", side_effect=(subprocess.TimeoutExpired("bash", 2), success)) as parse):
            for _attempt in range(2):
                slot_wait.wait_for_slot("widget", settings.machine, settings.slots, 1,
                    episode=request.episode, since=request.since, showrunner="builds", unit="unit",
                    work="test", purpose="verify")

        self.assertEqual([attempt.identity.step for attempt in requests], ["test", "test slot-retry-package"])
        self.assertEqual(parse.call_count, 2)

    def test_finite_wait_names_the_repository_held_slots_and_maximum_key(self) -> None:
        fixture, settings = self.pool()
        _ = self.hold(settings)
        request = self.request(fixture, settings)
        peer = fixture.connect(request)
        self.assertIsInstance(peer.replies()[-1], wire.RepositorySlotWaiting)
        fixture.clock.advance(30)
        _ = fixture.server.run_pass()
        reply = peer.replies()[-1]
        assert isinstance(reply, wire.RepositorySlotExpired)
        self.assertIn("widget", reply.reason)
        self.assertIn("widget-slot1", reply.reason)
        self.assertIn(f"slots.{settings.machine}.widget.max", reply.reason)
        self.assertFalse(fixture.server.repository_slot_waiters)

    def test_restart_discards_the_wait_and_a_retry_retains_its_original_time(self) -> None:
        fixture, settings = self.pool()
        descriptor = self.hold(settings)
        request = self.request(fixture, settings)
        peer = fixture.connect(request)
        self.assertIsInstance(peer.replies()[-1], wire.RepositorySlotWaiting)
        fixture.server.persist()
        saved = cast(object, json.loads((fixture.state_dir / "state.json").read_text()))
        self.assertNotIn(request.episode, json.dumps(saved))
        fixture.restart()
        self.assertFalse(fixture.server.repository_slot_waiters)
        retry = fixture.connect(request)
        self.assertIsInstance(retry.replies()[-1], wire.RepositorySlotWaiting)
        self.assertEqual(fixture.server.repository_slot_waiters[request.episode].request.since, request.since)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        _ = fixture.server.run_pass()
        self.assertEqual(retry.replies()[-1], wire.RepositorySlotReady())

    def test_wait_function_keeps_its_request_across_a_restart_and_bounds_silence(self) -> None:
        for state in ("restart", "silent"):
            with self.subTest(state=state):
                fixture, settings = self.pool()
                waiting = wire.RepositorySlotWaiting("widget", (1,), 1, f"slots.{settings.machine}.widget.max")
                request = self.request(fixture, settings)
                events = ([Exchange(reply_bytes(waiting), b""), Exchange(reply_bytes(wire.RepositorySlotReady()))]
                          if state == "restart" else [Exchange(reply_bytes(waiting), *(TimeoutError() for _index in range(30)))])
                transport = ScriptedTransport(fixture.clock, *events)
                with mock.patch.object(client, "CLOCK", fixture.clock), \
                     mock.patch.object(socket, "socket", side_effect=transport.socket), \
                     mock.patch.object(os, "getpid", return_value=request.owner.pid), \
                     mock.patch("scripts.build_queue.admission_records.process_start_time", return_value=request.owner.start_time), \
                     redirect_stderr(io.StringIO()):
                    def wait() -> None:
                        slot_wait.wait_for_slot("widget", settings.machine, settings.slots, 1,
                            episode=request.episode, since=request.since, showrunner="builds", unit="unit", work="work", purpose="gate")
                    if state == "silent":
                        with self.assertRaisesRegex(slot_wait.SlotWaitExpired, "widget-slot1.*slots.*widget.max"):
                            wait()
                        self.assertEqual(len(transport.attempts), 1)
                    else:
                        wait()
                        self.assertEqual(transport.requests[0], transport.requests[1])
                        self.assertEqual(len(transport.attempts), 2)
                self.doCleanups()

    def test_only_waiting_pools_are_read_and_three_pools_share_one_pass(self) -> None:
        fixture, settings = self.pool()
        with mock.patch.object(slot_wait, "read_pool", wraps=slot_wait.read_pool) as read:
            _ = fixture.server.run_pass()
            read.assert_not_called()
        for repository in ("widget", "second", "third"):
            _ = self.hold(settings, repository=repository)
        for repository in ("widget", "widget", "second", "second", "third"):
            peer = fixture.connect(self.request(fixture, settings, repository))
            self.assertIsInstance(peer.replies()[-1], wire.RepositorySlotWaiting)
        with mock.patch.object(slot_wait, "read_pool", wraps=slot_wait.read_pool) as read:
            started = time.thread_time()
            _ = fixture.server.run_pass()
            elapsed = time.thread_time() - started
            self.assertEqual(read.call_count, 3)
        print(f"slot pass: 3 pools, 5 waiters, CPU {elapsed:.6f} s")


if __name__ == "__main__":
    _ = unittest.main()
