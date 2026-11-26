"""Exercise login sittings with scratch state and injected transports."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import cast, override
from unittest import mock

from scripts.accounts import accounts, login_sittings as sittings
from scripts.agents import remote
from scripts.agents.test_support import write_machine_config
from scripts.lib import atomic_file, conf
from scripts.maintenance import agent_notes_job
from scripts.maintenance.schedule import ScheduleBusy, locked_history
from scripts.maintenance.settings import Part
from scripts.notify import push_texts, review
from scripts.message import send_budget
from scripts.whoami import agent_accounts, usage_store
from scripts.whoami.quota_alert import PushFailed, PushSent


class LoginSittingsTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.config: Path = Path()
        self.now: datetime = datetime(2026, 11, 17, 15, tzinfo=timezone.utc)
        self.local: tuple[sittings.LoginEnd, ...] = ()
        self.peer: tuple[sittings.LoginEnd, ...] | sittings.PeerReadFailed = ()
        self.sent: list[tuple[push_texts.RenderedPush, sittings.PushKind]] = []
        self.peer_calls: list[tuple[str, int]] = []
        self.retire_calls: list[str] = []
        self.outcome: PushSent | PushFailed = PushSent()
        self.retire_outcome: sittings.ReminderRetired | sittings.ReminderRetirementFailed = sittings.ReminderRetired()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "USER_ZONE": "America/New_York"})))
        _ = self.enterContext(mock.patch.object(remote, "this_machine", return_value="natedev"))
        self.config = self.root / "accounts.conf"
        _ = self.config.write_text(
            "login_sitting_machine=natedev\nlogin_sitting_first=2026-11-17T10:00:00-05:00\n"
            + "# whole: 1..\nlogin_sitting_interval_days=5\n"
            + "# whole: 1..\nlogin_safety_margin_days=3\n"
            + "# whole: 1..\nlogin_peer_read_minutes=60\n"
            + "# whole: 1..\nlogin_peer_read_timeout_seconds=30\n", encoding="utf-8")

    def end(self, label: str, days: int, machine: str = "natedev") -> sittings.LoginEnd:
        return sittings.LoginEnd(machine, label, self.now + timedelta(days=days))

    def read_peer(self, machine: str, timeout: int,
                  _copy: sittings.ReminderStateCopy) -> sittings.PeerLoginDates | sittings.PeerReadFailed:
        self.peer_calls.append((machine, timeout))
        return (self.peer if isinstance(self.peer, sittings.PeerReadFailed)
                else sittings.PeerLoginDates(self.peer, sittings.ReminderCopyStatus.SAVED))

    def send(self, batch: sittings.ReminderBatch) -> PushSent | PushFailed:
        self.sent.append((sittings.reminder_push(batch.ends), batch.kind))
        return self.outcome

    def retire(self, key: str) -> sittings.ReminderRetired | sittings.ReminderRetirementFailed:
        self.retire_calls.append(key)
        return self.retire_outcome

    def read_local(self, _machine: str) -> tuple[sittings.LoginEnd, ...]:
        return self.local

    def run_pass(self, at: datetime, machine: str = "natedev") -> list[str]:
        return sittings.run(at, configuration=conf.read(self.config), machine=lambda: machine,
            peer_machine=lambda: "mac", read_local=self.read_local,
            read_peer=self.read_peer, send=self.send, retire=self.retire)

    def state(self) -> sittings.ReminderState:
        return sittings.load_state(self.root / ".local/state/accounts/login_sittings.json")

    def status_reading(self, *ends: sittings.LoginEnd) -> accounts.StatusReading:
        rows = tuple(accounts.StatusRow(label=end.account, tool="claude", folder=f"~/.{end.account}",
            login="fixture", roles=(), weekly_left_percent=accounts.StatusUnavailable(),
            resets_at=accounts.StatusUnavailable(), credits=accounts.StatusUnavailable(), on_credits="no",
            use_credits=False, read_at=accounts.StatusUnavailable(),
            read_outcome=agent_accounts.UsageReadOutcome.OK, attempted_at=accounts.StatusUnavailable(),
            login_ends_at=agent_accounts.LoginEndKnown(end.at)) for end in ends)
        return accounts.StatusReading(rows, (), (), timezone.utc, self.now)

    def test_overlapping_pass_skips_state_peer_read_and_delivery_while_the_lock_is_busy(self) -> None:
        self.local = (self.end("claude1", 5),)
        nested: list[list[str]] = []
        read_peer = self.read_peer
        send = self.send

        def overlap(machine: str, timeout: int,
                    reminder_copy: sittings.ReminderStateCopy) -> sittings.PeerLoginDates | sittings.PeerReadFailed:
            if not nested:
                nested.append([])
                nested[0] = self.run_pass(self.now)
            return read_peer(machine, timeout, reminder_copy)

        def send_claimed(batch: sittings.ReminderBatch) -> PushSent | PushFailed:
            with locked_history(self.root / ".local/state/accounts/login_sittings") as held:
                self.assertIsInstance(held, ScheduleBusy)
            return send(batch)

        with (mock.patch.object(self, "read_peer", side_effect=overlap),
              mock.patch.object(self, "send", side_effect=send_claimed),
              mock.patch.object(sittings, "load_state", wraps=sittings.load_state) as load):
            _ = self.run_pass(self.now)
        self.assertEqual(nested, [[]])
        self.assertEqual(load.call_count, 2)
        self.assertEqual(self.peer_calls, [("mac", 30)])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.state().sitting, self.now)

    def test_saves_drop_named_dates_only_when_the_same_machine_and_account_have_a_different_date(self) -> None:
        at = self.now - timedelta(days=2)
        self.local = (self.end("claude1", -1), self.end("claude2", 0), self.end("claude3", 1))
        old_peer = self.end("claude1", 0, "mac")
        self.peer = (old_peer,)
        _ = self.run_pass(at)
        self.assertEqual(self.state().named, {*self.local, old_peer})
        missing = self.local[1]
        retained = self.local[2]
        self.local = (self.end("claude1", 29), retained)
        self.peer = (self.end("claude1", 30, "mac"),)
        _ = self.run_pass(at + timedelta(minutes=30))
        self.assertEqual(self.state().named, {missing, retained, old_peer})
        _ = self.run_pass(at + timedelta(hours=2))
        self.assertEqual(self.state().named, {missing, retained})
        self.assertEqual(len(self.sent), 1)

    def test_sitting_selects_the_earliest_machine_date_and_names_both_machines_once(self) -> None:
        self.local = (self.end("claude1", 20), self.end("claude2", 11), self.end("claude3", 10))
        self.peer = (self.end("claude1", 4, "mac"), self.end("claude2", 12, "mac"))
        _ = self.run_pass(self.now)
        self.assertEqual(len(self.sent), 1)
        push, kind = self.sent[0]
        self.assertEqual(kind, "sitting")
        self.assertIn("claude1: Mac Sat 11-21, natedev Mon 12-07", push.body)
        self.assertNotIn("claude3", push.body)
        self.assertNotIn("claude2", push.body)
        self.assertEqual(self.state().sitting, self.now)
        self.assertEqual(self.state().named, {self.local[0], self.peer[0]})
        _ = self.run_pass(self.now + timedelta(minutes=1))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.peer_calls, [("mac", 30)])

    def test_sittings_name_one_account_each_in_end_date_order(self) -> None:
        self.local = (self.end("claude1", 22), self.end("claude2", 25))
        self.peer = (self.end("claude2", 12, "mac"), self.end("claude3", 17, "mac"))
        for days in (0, 5, 10, 15):
            _ = self.run_pass(self.now + timedelta(days=days))
        self.assertEqual([kind for _, kind in self.sent], ["sitting", "sitting", "sitting"])
        for (push, _), expected in zip(self.sent, ("claude2", "claude3", "claude1"), strict=True):
            labels = [label for label in ("claude1", "claude2", "claude3") if label in push.body]
            self.assertEqual(labels, [expected])

    def test_next_login_request_rows_share_the_next_sitting_and_account(self) -> None:
        self.local = (self.end("claude1", 12), self.end("claude2", 18))
        self.peer = (self.end("claude2", 20, "mac"),)
        _ = self.run_pass(self.now)
        policy = sittings.SittingPolicy.read(conf.read(self.config))
        state = self.state()
        at = self.now + timedelta(hours=1)
        request = sittings.next_login_request(at, policy, state, (*self.local, *state.peer))
        self.assertEqual(request, sittings.ScheduledLoginRequest(
            self.now + timedelta(days=5), sittings.ReminderBatch("sitting", (self.peer[0], self.local[1]))))

    def test_next_request_matches_safety_and_sitting_delivery(self) -> None:
        policy = sittings.SittingPolicy.read(conf.read(self.config))
        cases: tuple[tuple[int, int, sittings.PushKind], ...] = (
            (2, 0, "safety"), (5, 2, "safety"), (8, 5, "sitting"))
        for ends_in, request_in, kind in cases:
            with self.subTest(ends_in=ends_in):
                self.sent.clear()
                early = self.end("claude2", ends_in)
                same_account = self.end("claude2", 10, "mac")
                simultaneous = self.end("claude3", ends_in, "mac")
                self.local, self.peer = (early,), (same_account, simultaneous)
                dates = (*self.local, *self.peer)
                state = sittings.ReminderState(sitting=self.now, peer=self.peer)
                sittings.save_state(sittings.reminder_state_path(), state)
                request = sittings.next_login_request(self.now, policy, state, dates)
                expected = (early, simultaneous) if kind == "safety" else (same_account, early)
                self.assertEqual(request, sittings.ScheduledLoginRequest(
                    self.now + timedelta(days=request_in), sittings.ReminderBatch(kind, expected)))
                _ = self.run_pass(self.now + timedelta(days=request_in))
                self.assertEqual(self.sent[0], (sittings.reminder_push(expected), kind))

    def test_sitting_without_any_stored_date_records_the_sitting_without_a_push(self) -> None:
        _ = self.run_pass(self.now)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state().sitting, self.now)
        self.local = (self.end("claude1", 5),)
        _ = self.run_pass(self.now + timedelta(minutes=1))
        self.assertEqual(self.sent, [])

    def test_missed_sittings_send_only_for_the_latest_sitting(self) -> None:
        self.local = (self.end("claude1", 30),)
        later = self.now + timedelta(days=23)
        _ = self.run_pass(later)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.state().sitting, self.now + timedelta(days=20))
        _ = self.run_pass(later)
        self.assertEqual(len(self.sent), 1)

    def test_safety_names_multiple_unnamed_accounts_at_the_margin_only_once(self) -> None:
        at = self.now - timedelta(days=1)
        self.local = (self.end("claude1", 2), self.end("claude2", 1), self.end("claude3", 8))
        self.peer = (self.end("claude1", 10, "mac"),)
        _ = self.run_pass(at)
        self.assertEqual([kind for _, kind in self.sent], ["safety"])
        self.assertIn("claude1: natedev Thu 11-19", self.sent[0][0].body)
        self.assertIn("claude2: natedev Wed 11-18", self.sent[0][0].body)
        self.assertNotIn("claude3", self.sent[0][0].body)
        self.assertNotIn("Mac", self.sent[0][0].body)
        _ = self.run_pass(at + timedelta(minutes=1))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.state().named, set(self.local[:2]))

    def test_missing_dates_keep_named_records_and_do_not_repeat_the_safety_push_when_dates_return(self) -> None:
        at = self.now - timedelta(days=1)
        local = self.end("claude1", 1)
        peer = self.end("claude2", 1, "mac")
        self.local, self.peer = (local,), (peer,)
        _ = self.run_pass(at)
        self.assertEqual([kind for _, kind in self.sent], ["safety"])
        self.local, self.peer = (), ()
        _ = self.run_pass(at + timedelta(hours=1))
        self.assertEqual(self.state().named, {local, peer})
        self.local, self.peer = (local,), (peer,)
        _ = self.run_pass(at + timedelta(hours=2))
        self.assertEqual([kind for _, kind in self.sent], ["safety"])
        self.assertEqual(self.state().named, {local, peer})
        self.assertEqual(self.retire_calls, [])

    def test_a_sitting_push_prevents_a_safety_push_for_the_same_end_date(self) -> None:
        self.local = (self.end("claude1", 2),)
        _ = self.run_pass(self.now)
        _ = self.run_pass(self.now + timedelta(days=1))
        self.assertEqual([kind for _, kind in self.sent], ["sitting"])

    def test_sitting_omits_a_machine_date_that_safety_already_reported(self) -> None:
        self.local = (self.end("claude1", 1),)
        mac_end = self.end("claude1", 9, "mac")
        self.peer = (mac_end,)
        _ = self.run_pass(self.now - timedelta(days=1))
        self.assertEqual(self.state().named, set(self.local))

        _ = self.run_pass(self.now)

        self.assertEqual([kind for _, kind in self.sent], ["safety", "sitting"])
        self.assertEqual(self.sent[0][0].values["accounts"], "claude1: natedev Wed 11-18")
        self.assertEqual(self.sent[1][0].values["accounts"], "claude1: Mac Thu 11-26")
        self.assertEqual(self.state().named, {*self.local, mac_end})

    def test_renewed_login_leaves_the_next_request_and_its_new_end_can_get_a_safety_push(self) -> None:
        self.local = (self.end("claude1", 1), self.end("claude2", 8))
        _ = self.run_pass(self.now)
        self.local = (self.end("claude1", 9), self.end("claude2", 8))
        _ = self.run_pass(self.now + timedelta(days=5))
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("claude1", self.sent[1][0].body)
        self.assertIn("claude2", self.sent[1][0].body)
        # Before the next sitting, a new end inside the safety margin is eligible.
        _ = self.run_pass(self.now + timedelta(days=6))
        self.assertEqual(self.sent[-1][1], "safety")
        self.assertIn(self.local[0], self.state().named)

    def test_renewal_records_when_each_machine_end_moves_later(self) -> None:
        local_key, peer_key = ("natedev", "claude1"), ("mac", "claude1")
        self.local = (self.end("claude1", 14),)
        self.peer = (self.end("claude1", 12, "mac"),)
        _ = self.run_pass(self.now)
        self.assertIsInstance(self.state().observed[local_key].renewal, sittings.NoRecordedTime)
        self.assertIsInstance(self.state().observed[peer_key].renewal, sittings.NoRecordedTime)

        self.local = (self.end("claude1", 36),)
        local_at = self.now + timedelta(minutes=30)
        _ = self.run_pass(local_at)
        self.assertEqual(self.state().observed[local_key].renewal, sittings.LoginRenewal(local_at, self.local[0].at))
        self.assertIsInstance(self.state().observed[peer_key].renewal, sittings.NoRecordedTime)

        self.peer = (self.end("claude1", 34, "mac"),)
        peer_at = self.now + timedelta(hours=1)
        _ = self.run_pass(peer_at)
        _ = self.run_pass(peer_at + timedelta(hours=2))
        self.assertEqual(self.state().observed[peer_key].renewal, sittings.LoginRenewal(peer_at, self.peer[0].at))
        self.assertEqual(self.state().observed[local_key].renewal, sittings.LoginRenewal(local_at, self.local[0].at))

    def test_renewal_uses_dates_saved_before_observation_records_existed(self) -> None:
        path = self.root / ".local/state/accounts/login_sittings.json"
        path.parent.mkdir(parents=True)
        old = self.end("claude1", 7)
        row = {"machine": old.machine, "label": old.account, "login_ends_at": old.at.isoformat()}
        _ = path.write_text(json.dumps({"named": [row]}), encoding="utf-8")
        self.local = (self.end("claude1", 30),)
        _ = self.run_pass(self.now)
        self.assertEqual(self.state().observed[(old.machine, old.account)].renewal,
                         sittings.LoginRenewal(self.now, self.local[0].at))

    def test_failed_peer_read_keeps_the_last_good_dates_and_throttles_attempts(self) -> None:
        self.peer = (self.end("claude4", 5, "mac"),)
        _ = self.run_pass(self.now - timedelta(days=2))
        self.peer = sittings.PeerReadFailed("fixture offline")
        lines = self.run_pass(self.now)
        self.assertIn("fixture offline", "\n".join(lines))
        self.assertIn("claude4: Mac Sun 11-22", self.sent[0][0].body)
        self.assertEqual(self.state().peer, (self.end("claude4", 5, "mac"),))
        self.assertEqual(self.state().peer_read, self.now - timedelta(days=2))
        _ = self.run_pass(self.now + timedelta(minutes=59))
        self.assertEqual(len(self.peer_calls), 2)
        _ = self.run_pass(self.now + timedelta(minutes=60))
        self.assertEqual(len(self.peer_calls), 3)

    def test_local_read_failure_keeps_the_peer_attempt_and_result_and_throttles_the_next_pass(self) -> None:
        old_peer = self.end("claude4", 5, "mac")
        new_peer = self.end("claude4", 29, "mac")
        results: tuple[tuple[sittings.LoginEnd, ...] | sittings.PeerReadFailed, ...] = (
            (new_peer,), sittings.PeerReadFailed("fixture offline"))
        for index, result in enumerate(results):
            with (self.subTest(result=result),
                  mock.patch.object(accounts, "accounts_home", return_value=self.root / str(index))):
                self.peer_calls.clear()
                self.sent.clear()
                self.peer = (old_peer,)
                _ = self.run_pass(self.now - timedelta(hours=2))
                self.peer = result
                with (mock.patch.object(self, "read_local", side_effect=RuntimeError("fixture local read failed")),
                      self.assertRaisesRegex(RuntimeError, "fixture local read failed")):
                    _ = self.run_pass(self.now)
                state = sittings.load_state(accounts.accounts_home() / ".local/state/accounts/login_sittings.json")
                self.assertEqual(state.peer_attempt, self.now)
                if isinstance(result, sittings.PeerReadFailed):
                    self.assertEqual(state.peer, (old_peer,))
                    self.assertEqual(state.peer_read, self.now - timedelta(hours=2))
                else:
                    self.assertEqual(state.peer, (new_peer,))
                    self.assertEqual(state.peer_read, self.now)
                _ = self.run_pass(self.now + timedelta(minutes=1))
                self.assertEqual(self.peer_calls, [("mac", 30), ("mac", 30)])

    def test_local_read_failure_retires_all_superseded_batches(self) -> None:
        mac_only = self.end("claude1", 12, "mac")
        mixed_mac = self.end("claude2", 18, "mac")
        mixed_local = self.end("claude2", 20)
        self.peer, self.local = (mac_only, mixed_mac), (mixed_local,)
        _ = self.run_pass(self.now)
        _ = self.run_pass(self.now + timedelta(days=5))
        mac_key = sittings.ReminderBatch("sitting", (mac_only,)).key
        mixed_key = sittings.ReminderBatch("sitting", (mixed_mac, mixed_local)).key

        self.local = (self.end("claude2", 40),)
        _ = self.run_pass(self.now + timedelta(days=6))
        self.assertEqual(set(self.state().accepted_batches), {mac_key, mixed_key})

        self.peer = (self.end("claude1", 39, "mac"), self.end("claude2", 41, "mac"))
        at = self.now + timedelta(days=7)
        with (mock.patch.object(self, "send", wraps=self.send) as send,
              mock.patch.object(self, "retire", wraps=self.retire) as retire,
              mock.patch.object(self, "read_local", side_effect=RuntimeError("fixture local read failed")),
              self.assertRaisesRegex(RuntimeError, "fixture local read failed")):
            _ = self.run_pass(at)

        send.assert_not_called()
        self.assertEqual(retire.call_args_list, [mock.call(mac_key), mock.call(mixed_key)])
        state = self.state()
        self.assertEqual(state.peer, self.peer)
        self.assertEqual(state.accepted_batches, {})

        _ = self.run_pass(at + timedelta(minutes=1))
        self.assertEqual(self.retire_calls, [mac_key, mixed_key])
        self.assertEqual(self.state().accepted_batches, {})
        self.assertEqual(len(self.sent), 2)

    def test_retirement_keeps_a_batch_with_an_unknown_date(self) -> None:
        old_mac = self.end("claude1", 12, "mac")
        old_local = self.end("claude1", 15)
        renewed_mac = self.end("claude1", 40, "mac")
        batch = sittings.ReminderBatch("sitting", (old_mac, old_local))
        state = sittings.ReminderState(
            sitting=self.now,
            observed={(old_mac.machine, old_mac.account): sittings.ObservedLogin(renewed_mac)},
            accepted_batches={batch.key: batch.ends},
        )
        sittings.save_state(self.root / ".local/state/accounts/login_sittings.json", state)

        _ = self.run_pass(self.now + timedelta(hours=1))

        self.assertEqual(self.retire_calls, [])
        self.assertEqual(self.state().accepted_batches, state.accepted_batches)

    def test_failed_retirement_save_still_tries_every_obsolete_batch(self) -> None:
        first, second = self.end("claude1", 12), self.end("claude2", 18)
        batches = {batch.key: batch.ends for batch in (
            sittings.ReminderBatch("sitting", (first,)), sittings.ReminderBatch("sitting", (second,)))}
        self.local = (self.end("claude1", 40), self.end("claude2", 41))
        path = self.root / ".local/state/accounts/login_sittings.json"
        save_state = sittings.save_state
        save_state(path, sittings.ReminderState(
            sitting=self.now, peer_attempt=self.now, accepted_batches=batches.copy()))
        removal_writes: list[set[str]] = []

        def fail_removal(path: Path, state: sittings.ReminderState) -> None:
            if state.accepted_batches != batches:
                removal_writes.append(set(state.accepted_batches))
                raise OSError("fixture retirement save failed")
            save_state(path, state)

        with (mock.patch.object(sittings, "save_state", side_effect=fail_removal),
              self.assertRaisesRegex(OSError, "fixture retirement save failed")):
            _ = self.run_pass(self.now)

        self.assertEqual(self.retire_calls, list(batches))
        self.assertEqual(removal_writes, [set()])
        self.assertEqual(self.state().accepted_batches, batches)

        _ = self.run_pass(self.now + timedelta(minutes=1))
        self.assertEqual(self.retire_calls, list(batches) * 2)
        self.assertEqual(self.state().accepted_batches, {})
        self.assertEqual(self.sent, [])

    def test_failed_send_records_no_sitting_or_named_login_and_retries(self) -> None:
        self.local = (self.end("claude1", 1),)
        self.outcome = PushFailed("fixture send failed")
        _ = self.run_pass(self.now)
        self.assertIsInstance(self.state().sitting, sittings.NoRecordedTime)
        self.assertEqual(self.state().named, set())
        self.outcome = PushSent()
        _ = self.run_pass(self.now + timedelta(minutes=1))
        self.assertEqual([kind for _, kind in self.sent], ["sitting", "sitting"])
        self.assertEqual(self.state().sitting, self.now)

    def test_failed_safety_send_records_no_login_and_retries(self) -> None:
        self.local = (self.end("claude1", 1),)
        self.outcome = PushFailed("fixture send failed")
        _ = self.run_pass(self.now - timedelta(days=1))
        self.assertEqual(self.state().named, set())
        self.outcome = PushSent()
        _ = self.run_pass(self.now - timedelta(hours=12))
        self.assertEqual([kind for _, kind in self.sent], ["safety", "safety"])

    def test_other_machine_timer_skips_state_reads_and_transport(self) -> None:
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("unexpected process")):
            self.assertEqual(self.run_pass(self.now, "mac"), [])
        self.assertEqual(self.peer_calls, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.retire_calls, [])
        self.assertFalse(sittings.reminder_state_path().parent.exists())

    def test_scheduled_leader_exchanges_state_and_dates_in_one_peer_call(self) -> None:
        write_machine_config(self.root)
        leader_end = self.end("claude1", 2)
        mac_end = self.end("claude2", 5, "mac")
        sittings.save_state(sittings.reminder_state_path(), sittings.ReminderState(
            sitting=self.now, named={leader_end, mac_end}, local=(leader_end,)))
        mac_state = self.root / "mac" / "login_sittings.json"
        mac_copy = mac_state.with_name("login_sittings_copy.json")
        command = '"$HOME/.claude/scripts/lib/py" -m scripts.accounts.login_sittings exchange'

        at = self.now

        def scheduled_now(_zone: tzinfo) -> datetime:
            return at

        def exchange(argv: list[str], *, input: str, timeout: int, **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv, ["ssh", "-o", "BatchMode=yes", "mac", command])
            self.assertEqual(timeout, 30)
            minutes = (at - self.now) // timedelta(minutes=1)
            if minutes == 240:
                return subprocess.CompletedProcess(argv, 255, "", "")
            sent = cast(dict[str, object], json.loads(input))
            self.assertEqual(sent["source"], "natedev")
            self.assertEqual(sent["copied_at"], at.isoformat())
            state = cast(dict[str, object], sent["state"])
            self.assertEqual(state["sitting"], self.now.isoformat())
            if minutes == 0:
                self.assertEqual(len(cast(list[object], state["named"])), 2)
            if minutes == 60:
                sent["copied_at"] = "invalid"
            date = self.end("claude2", 5 + minutes // 60, "mac")
            output, errors = io.StringIO(), io.StringIO()
            flushed: list[bytes] = []

            def fail_write(_source: Path, _target: Path) -> None:
                if minutes == 180:
                    raise subprocess.TimeoutExpired(argv, timeout, output=b"".join(flushed))
                raise OSError("fixture write failed")

            writer = mock.patch("os.replace", side_effect=fail_write) if minutes in (120, 180) else nullcontext()
            with (mock.patch.object(remote, "local_machine", return_value="fixture-mac-host"),
                  mock.patch.object(sittings, "reminder_state_path", return_value=mac_state),
                  mock.patch.object(accounts, "read_status", return_value=self.status_reading(date)),
                  mock.patch.object(output, "flush", side_effect=lambda: flushed.append(output.getvalue().encode())),
                  writer):
                code = sittings.main(["exchange"], io.StringIO(json.dumps(sent)), output, errors)
            return subprocess.CompletedProcess(argv, code, output.getvalue(), errors.getvalue())

        with (mock.patch.dict(os.environ, {"CONF_DIR": str(self.root)}),
              mock.patch("socket.gethostname", return_value="natedev"),
              mock.patch.object(accounts, "read_status", return_value=self.status_reading(leader_end)),
              mock.patch.object(agent_notes_job, "datetime", SimpleNamespace(now=scheduled_now)),
              mock.patch.object(subprocess, "run", side_effect=exchange) as transport,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(agent_notes_job._perform(Part.LOGINS), 0)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(self.state().peer, (mac_end,))
            original = mac_copy.read_bytes()
            for minutes, calls, days in ((59, 1, 5), (60, 2, 6), (61, 2, 6), (120, 3, 7), (180, 4, 8), (240, 5, 8)):
                at = self.now + timedelta(minutes=minutes)
                self.assertEqual(agent_notes_job._perform(Part.LOGINS), 0)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(transport.call_count, calls)
                self.assertEqual(self.state().peer, (self.end("claude2", days, "mac"),))
                self.assertEqual(mac_copy.read_bytes(), original)
        self.assertEqual(output.getvalue().splitlines(), [
            "login sittings: mac reminder copy rejected",
            "login sittings: mac reminder copy unsaved",
            "login sittings: mac reminder copy status unavailable",
            "login sittings: peer dates unavailable: mac account read exited 255",
        ])
        self.assertEqual(self.state().peer_read, self.now + timedelta(minutes=180))
        self.assertFalse(mac_state.exists())

    def test_exchange_command_rejects_malformed_timestamps_and_keeps_the_copy(self) -> None:
        leader_end = self.end("claude1", 2)
        renewal = sittings.LoginRenewal(self.now, leader_end.at)
        state = sittings.ReminderState(sitting=self.now, named={leader_end}, peer=(leader_end,),
            peer_read=self.now, peer_attempt=self.now, local=(leader_end,),
            observed={("natedev", "claude1"): sittings.ObservedLogin(leader_end, renewal)},
            accepted_batches={"fixture-batch": (leader_end,)})
        encoded = sittings.ReminderStateCopy("natedev", self.now, state).encode()
        paths: tuple[tuple[str | int, ...], ...] = (
            ("copied_at",), ("state", "sitting"), ("state", "peer_read"), ("state", "peer_attempt"),
            ("state", "named", 0, "login_ends_at"), ("state", "local", 0, "login_ends_at"),
            ("state", "peer", 0, "login_ends_at"), ("state", "observed", 0, "login_ends_at"),
            ("state", "observed", 0, "renewed_at"), ("state", "observed", 0, "renewed_login_end"),
            ("state", "accepted_batches", "fixture-batch", 0, "login_ends_at"),
        )
        copy_path = sittings.reminder_state_path().with_name("login_sittings_copy.json")
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(self.root)}),
              mock.patch.object(remote, "local_machine", return_value="mac"),
              mock.patch.object(accounts, "read_status", return_value=self.status_reading(leader_end)) as read):
            output, errors = io.StringIO(), io.StringIO()
            self.assertEqual(sittings.main(["exchange"], io.StringIO(encoded), output, errors), 0)
            self.assertEqual(errors.getvalue(), "")
            dates = [{"label": "claude1", "login_ends_at": leader_end.at.isoformat()}]
            self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],
                             [{"accounts": dates}, {"copy_status": "saved"}])
            original = copy_path.read_bytes()
            for path in paths:
                with self.subTest(path=path):
                    document = cast(object, json.loads(encoded))
                    parent = document
                    for key in path[:-1]:
                        parent = cast(dict[str, object], parent)[key] if isinstance(key, str) else cast(list[object], parent)[key]
                    final_key = path[-1]
                    assert isinstance(final_key, str)
                    cast(dict[str, object], parent)[final_key] = "2026-11-18T12:00:00"
                    output, errors = io.StringIO(), io.StringIO()
                    self.assertEqual(sittings.main(["exchange"], io.StringIO(json.dumps(document)), output, errors), 0)
                    self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],
                                     [{"accounts": dates}, {"copy_status": "rejected"}])
                    self.assertEqual(errors.getvalue(), "")
                    self.assertEqual(copy_path.read_bytes(), original)
            self.assertEqual(read.call_count, 1 + len(paths))

    def test_exchange_command_rejects_deeply_nested_copy_and_keeps_dates(self) -> None:
        local_end = self.end("claude2", 9, "mac")
        copy_path = sittings.reminder_state_path().with_name("login_sittings_copy.json")
        old = sittings.ReminderStateCopy("natedev", self.now, sittings.ReminderState()).encode()
        atomic_file.write_text_atomic(copy_path, old)
        depth = sys.getrecursionlimit() + 10
        nested = "[" * depth + "]" * depth
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(self.root)}),
              mock.patch.object(remote, "local_machine", return_value="fixture-mac-host"),
              mock.patch.object(accounts, "read_status", return_value=self.status_reading(local_end)),
              mock.patch.object(json, "load", side_effect=RecursionError("fixture JSON nesting"))):
            output, errors = io.StringIO(), io.StringIO()
            self.assertEqual(sittings.main(["exchange"], io.StringIO(nested), output, errors), 0)
        dates = [{"label": "claude2", "login_ends_at": local_end.at.isoformat()}]
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],
                         [{"accounts": dates}, {"copy_status": "rejected"}])
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(copy_path.read_text(), old)

    def test_exchange_command_returns_dates_when_copy_write_fails(self) -> None:
        local_end = self.end("claude2", 9, "mac")
        copy_path = sittings.reminder_state_path().with_name("login_sittings_copy.json")
        old = sittings.ReminderStateCopy("natedev", self.now - timedelta(hours=1), sittings.ReminderState())
        atomic_file.write_text_atomic(copy_path, old.encode())
        new = sittings.ReminderStateCopy("natedev", self.now, sittings.ReminderState())
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(self.root)}),
              mock.patch.object(remote, "local_machine", return_value="fixture-mac-host"),
              mock.patch.object(accounts, "read_status", return_value=self.status_reading(local_end)) as read):
            def reject_replace(_source: Path, _target: Path) -> None:
                read.assert_called_once_with(source=accounts.StoredUsageOnly())
                raise OSError("fixture write failed")

            with mock.patch("os.replace", side_effect=reject_replace):
                output, errors = io.StringIO(), io.StringIO()
                self.assertEqual(sittings.main(["exchange"], io.StringIO(new.encode()), output, errors), 0)
            dates = [{"label": "claude2", "login_ends_at": local_end.at.isoformat()}]
            self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],
                             [{"accounts": dates}, {"copy_status": "unsaved"}])
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(copy_path.read_text(), old.encode())
            with mock.patch.object(atomic_file, "sync_folder", side_effect=OSError("fixture folder sync failed")):
                output, errors = io.StringIO(), io.StringIO()
                self.assertEqual(sittings.main(["exchange"], io.StringIO(new.encode()), output, errors), 0)
            self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],
                             [{"accounts": dates}, {"copy_status": "durability unconfirmed"}])
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(copy_path.read_text(), new.encode())

    def test_local_dates_use_the_same_stored_rows_as_account_status(self) -> None:
        reports = [agent_accounts.Report("Claude", email="fixture-one",
            source=agent_accounts.ClaudeDirectorySource(self.root / ".claude1"),
            login_ends_at=agent_accounts.LoginEndKnown(self.end("claude1", 5).at)),
            agent_accounts.Report("Claude", email="fixture-shared",
            source=agent_accounts.ClaudeDirectorySource(self.root / ".claude"),
            login_ends_at=agent_accounts.LoginEndKnown(self.end("claude2", 6).at)),
            agent_accounts.Report("Claude", email="fixture-unknown",
            source=agent_accounts.ClaudeDirectorySource(self.root / ".claude3")),
            agent_accounts.Report("Codex", email="fixture-codex",
            source=agent_accounts.CodexHomeSource(self.root / ".codex1"))]
        usage_store.save(reports, self.now)
        with mock.patch.object(accounts, "read_status", return_value=self.status_reading(self.end("claude1", 5))) as read:
            self.assertEqual(sittings.local_dates("natedev"), (self.end("claude1", 5),))
        read.assert_called_once_with(source=accounts.StoredUsageOnly())

    def test_deleted_account_has_no_date_even_when_its_stored_directory_link_remains(self) -> None:
        current, removed = self.root / ".claude1", self.root / ".claude2"
        current.mkdir()
        removed.mkdir()
        usage_store.save([agent_accounts.Report("Claude", email=f"fixture-{index}",
            source=agent_accounts.ClaudeDirectorySource(directory),
            login_ends_at=agent_accounts.LoginEndKnown(self.end(f"claude{index}", index).at))
            for index, directory in enumerate((current, removed), start=1)], self.now)
        removed.rmdir()
        self.assertIn(usage_store.directory_key("Claude", str(removed)), usage_store.load().directory_logins)
        with mock.patch.object(accounts, "read_status", return_value=self.status_reading(self.end("claude1", 1))) as read:
            self.assertEqual(sittings.local_dates("natedev"), (self.end("claude1", 1),))
        read.assert_called_once_with(source=accounts.StoredUsageOnly())

    def test_peer_reader_uses_stored_json_and_excludes_unknown_or_non_claude_rows(self) -> None:
        reminder_copy = sittings.ReminderStateCopy("natedev", self.now, sittings.ReminderState())
        rows = [{"label": "claude1", "login_ends_at": self.end("claude1", 4).at.isoformat()},
                {"label": "claude2", "login_ends_at": None},
                {"label": "codex1", "login_ends_at": self.now.isoformat()}]
        date_line = json.dumps({"accounts": rows}) + "\n"
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, date_line + '{"copy_status": "saved"}\n', "")) as read:
            self.assertEqual(sittings.peer_dates("mac", 37, reminder_copy), sittings.PeerLoginDates(
                (self.end("claude1", 4, "mac"),), sittings.ReminderCopyStatus.SAVED))
        self.assertEqual(read.call_args.args[0], ["ssh", "-o", "BatchMode=yes", "mac",
            '"$HOME/.claude/scripts/lib/py" -m scripts.accounts.login_sittings exchange'])
        self.assertEqual(read.call_args.kwargs["timeout"], 37)
        self.assertEqual(read.call_args.kwargs["input"], reminder_copy.encode())
        for status_line in ("", '{"copy_status": "unexpected"}\n', '{"copy_status": "saved"}'):
            with self.subTest(status_line=status_line), mock.patch.object(subprocess, "run", side_effect=
                    subprocess.TimeoutExpired("fixture ssh", 37, output=(date_line + status_line).encode())):
                self.assertEqual(sittings.peer_dates("mac", 37, reminder_copy), sittings.PeerLoginDates(
                    (self.end("claude1", 4, "mac"),), sittings.ReminderCopyStatus.UNREPORTED))
        with mock.patch.object(subprocess, "run", side_effect=
                subprocess.TimeoutExpired("fixture ssh", 37, output=date_line.rstrip("\n").encode())):
            self.assertIsInstance(sittings.peer_dates("mac", 37, reminder_copy), sittings.PeerReadFailed)
        for stdout, code in (("bad json\n", 0), ('{"accounts": null}\n', 0), ("", 255)):
            with self.subTest(stdout=stdout, code=code), mock.patch.object(subprocess, "run",
                    return_value=subprocess.CompletedProcess([], code, stdout, "")):
                self.assertIsInstance(sittings.peer_dates("mac", 37, reminder_copy), sittings.PeerReadFailed)

    def test_sender_uses_the_batch_key_and_keeps_silence_enabled(self) -> None:
        ends = (self.end("claude1", 3), self.end("claude1", 4, "mac"))
        kinds: tuple[sittings.PushKind, ...] = ("sitting", "safety")
        for kind in kinds:
            batch = sittings.ReminderBatch(kind, ends)
            self.assertEqual(batch.key, sittings.ReminderBatch(kind, ends[::-1]).key)
            for status in ("SENT", "KEPT", "REPLACED"):
                with (self.subTest(kind=kind, status=status),
                      mock.patch.object(review, "user_send_seconds", return_value=15),
                      mock.patch.object(send_budget, "user_seconds", return_value=25),
                      mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                          [], 0, f"{status}: fixture\n", "")) as send):
                    self.assertIsInstance(sittings.send_push(batch), PushSent)
                argv = cast(list[str], send.call_args.args[0])
                self.assertEqual(argv[argv.index("--key") + 1], batch.key)
                self.assertTrue(batch.key.startswith(f"login-{kind}-"))
                self.assertEqual(argv[argv.index("--text-id") + 1], sittings.TEXT_ID)
                self.assertEqual(argv[argv.index("--from") + 1], "accounts")
                self.assertEqual(argv[argv.index("--need") + 1], "note")
                self.assertNotIn("--through-silence", argv)
        with (mock.patch.object(review, "user_send_seconds", return_value=15),
              mock.patch.object(send_budget, "user_seconds", return_value=25),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", ""))):
            self.assertIsInstance(sittings.send_push(sittings.ReminderBatch("safety", ends)), PushFailed)

    def test_silence_keeps_two_sittings_and_retry_reuses_its_key(self) -> None:
        self.local = (self.end("claude1", 12), self.end("claude2", 18))
        attempts: list[str] = []
        retained: dict[str, str] = {}

        def submit(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            key = argv[argv.index("--key") + 1]
            attempts.append(key)
            if len(attempts) == 1:
                return subprocess.CompletedProcess(argv, 3, "FAILED: fixture\n", "")
            retained[key] = argv[argv.index("--value") + 1]
            return subprocess.CompletedProcess(argv, 0, "KEPT: fixture\n", "")

        with (mock.patch.object(self, "send", side_effect=sittings.send_push),
              mock.patch.object(review, "user_send_seconds", return_value=15),
              mock.patch.object(send_budget, "user_seconds", return_value=25),
              mock.patch.object(subprocess, "run", side_effect=submit)):
            _ = self.run_pass(self.now)
            _ = self.run_pass(self.now + timedelta(minutes=1))
            _ = self.run_pass(self.now + timedelta(days=5))

        first, retry, second = attempts
        self.assertEqual(first, retry)
        self.assertNotEqual(first, second)
        self.assertEqual(retained, {first: "accounts=claude1: natedev Sun 11-29",
                                    second: "accounts=claude2: natedev Sat 12-05"})
        self.assertEqual(self.state().named, set(self.local))

    def test_renewal_retires_a_held_batch_and_keeps_a_partly_current_batch(self) -> None:
        first = self.end("claude1", 12)
        second = self.end("claude2", 18)
        mac = self.end("claude2", 20, "mac")
        self.local, self.peer = (first, second), (mac,)
        first_key = sittings.ReminderBatch("sitting", (first,)).key
        second_key = sittings.ReminderBatch("sitting", (mac, second)).key
        retained: set[str] = set()

        def transport(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            key = argv[argv.index("--key") + 1]
            if "retire" in argv:
                retained.remove(key)
                return subprocess.CompletedProcess(argv, 0, "dropped 1 kept push\n", "")
            retained.add(key)
            return subprocess.CompletedProcess(argv, 0, "KEPT: fixture\n", "")

        with (mock.patch.object(self, "send", side_effect=sittings.send_push) as send,
              mock.patch.object(self, "retire", side_effect=sittings.retire_push),
              mock.patch.object(review, "user_send_seconds", return_value=15),
              mock.patch.object(send_budget, "user_seconds", return_value=45),
              mock.patch.object(subprocess, "run", side_effect=transport) as submit):
            _ = self.run_pass(self.now)
            _ = self.run_pass(self.now + timedelta(days=5))
            self.assertEqual(retained, {first_key, second_key})
            self.assertEqual(self.state().accepted_batches, {first_key: (first,), second_key: (mac, second)})

            self.local = (self.end("claude1", 40), self.end("claude2", 41))
            _ = self.run_pass(self.now + timedelta(days=6))
            _ = self.run_pass(self.now + timedelta(days=7))

        self.assertEqual(send.call_count, 2)
        self.assertEqual(submit.call_count, 3)
        argv = cast(list[str], submit.call_args.args[0])
        self.assertEqual(argv[-7:], ["retire", "--to", "user", "--from", "accounts", "--key", first_key])
        self.assertEqual(submit.call_args.kwargs["timeout"], 45)
        self.assertEqual(retained, {second_key})
        self.assertEqual(self.state().accepted_batches, {second_key: (mac, second)})

    def test_failed_retirement_retains_the_batch_and_retries(self) -> None:
        original = self.end("claude1", 12)
        self.local = (original,)
        _ = self.run_pass(self.now)
        key = sittings.ReminderBatch("sitting", (original,)).key
        self.local = (self.end("claude1", 40),)
        self.retire_outcome = sittings.ReminderRetirementFailed("fixture error")
        lines = self.run_pass(self.now + timedelta(hours=1))
        self.assertIn("login sittings: retirement failed: fixture error", lines)
        self.assertEqual(self.state().accepted_batches, {key: (original,)})

        self.retire_outcome = sittings.ReminderRetired()
        _ = self.run_pass(self.now + timedelta(hours=2))
        _ = self.run_pass(self.now + timedelta(hours=3))

        self.assertEqual(self.retire_calls, [key, key])
        self.assertEqual(self.state().accepted_batches, {})
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    _ = unittest.main()
