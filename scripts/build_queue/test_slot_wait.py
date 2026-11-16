"""Repository capacity waits retain their order without reserving memory."""
from __future__ import annotations

from contextlib import AbstractContextManager, redirect_stderr
import fcntl
import io
import json
import os
import socket
import time
import unittest
from unittest import mock
from typing import cast

from scripts.build_queue import admission_client as client, admission_protocol as wire, slot_wait
from scripts.build_queue.admission_test_support import AdmissionFixture, Exchange, ScriptedTransport, reply_bytes
from scripts.lib.build_slots_test_support import SlotTestConfiguration, private_slot_config


class RepositorySlotWaitTests(unittest.TestCase):
    def pool(self, maximum: int = 1) -> tuple[AdmissionFixture, SlotTestConfiguration]:
        fixture = self.enterContext(AdmissionFixture())
        settings = private_slot_config(fixture.root, repository="widget", maximum=maximum)
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, settings.environment)))
        settings.slots.mkdir()
        return fixture, settings

    def hold(self, settings: SlotTestConfiguration, number: int = 1, repository: str = "widget") -> int:
        stream = self.enterContext((settings.slots / f"{repository}-slot{number}.lock").open("w"))
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream.fileno()

    def request(self, fixture: AdmissionFixture, settings: SlotTestConfiguration,
                repository: str = "widget") -> wire.RepositorySlotRequest:
        original = fixture.request()
        return wire.RepositorySlotRequest(original.episode, original.owner, original.since, repository,
            settings.machine, str(settings.slots), 1, original.since + 30,
            wire.Identity("slot-session", "slot-showrunner", "gate", "proof", "slot-unit"))

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
