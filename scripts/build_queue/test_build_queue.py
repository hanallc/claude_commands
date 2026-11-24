"""Check the durable build priority command with private admission state."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import os
import unittest
from unittest import mock
from typing import cast, override

from scripts.build_queue import admission_client, admission_protocol as wire, admission_records as records
from scripts.build_queue import admission_service, build_queue, head_facts, person_actions, queue_snapshot
from scripts.buildlog import record as buildlog_record
from scripts.production import staff
from scripts.build_queue.admission_test_support import AdmissionFixture, InjectedCrash
from scripts.lint import memory_admit
from scripts.message import unit_labels


class QueueTests(unittest.TestCase):
    fixture: AdmissionFixture = cast(AdmissionFixture, cast(object, None))

    @override
    def setUp(self) -> None:
        self.fixture = self.enterContext(AdmissionFixture())
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": ""}))
        self.fixture.free(0)
        self.fixture.hold_starts()

    def waiter(self, pid: int, unit: str, *, session_id: str | None = None) -> wire.AdmitRequest:
        owner = self.fixture.fixture_owner(pid, str(pid))
        request = self.fixture.request(owner=owner)
        request = replace(request, identity=replace(request.identity, name="recorded-" + unit,
            unit=unit, folder=f"/fixture/tree-{pid}",
            session_id=request.identity.session_id if session_id is None else session_id))
        _ = self.fixture.server.handle_request(request)
        return request

    def save_for(self, request: wire.AdmitRequest, name: str) -> person_actions.Bump:
        key = self.fixture.server.waiters[request.episode].row.get("move_id", "")
        assert key
        bump = person_actions.Bump(name, person_actions.SetBump(key), request.identity.unit,
            "actor", self.fixture.clock.now(), "urgent")
        person_actions.save_bump(self.fixture.state_dir, bump)
        return bump

    def bumps(self) -> list[person_actions.Bump]:
        return [bump for _, bump in person_actions.pending_bumps(self.fixture.state_dir, 0)]

    def priority_events(self) -> list[str]:
        return [line for path in self.fixture.state_dir.parent.glob("*/*.jsonl")
                for line in path.read_text().splitlines() if '"queue_priority"' in line]

    def priority_units(self) -> list[str]:
        priority = self.fixture.server.priority
        return [entry["unit"] for entry in priority.entries] if isinstance(priority, records.QueuePriority) else []

    def bump_output(self, *words: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch("sys.argv", ["build_queue.py", *words]),
              redirect_stdout(output), redirect_stderr(errors)):
            status = build_queue.main()
        return status, output.getvalue(), errors.getvalue()

    def test_bare_bump_prints_usage_and_the_builds_page_link(self) -> None:
        from scripts.build_queue import memory_feed

        for arguments in ((), ("--help",)):
            with (self.subTest(arguments=arguments),
                  mock.patch.object(memory_feed, "page_url", return_value="https://fixture/builds"),
                  mock.patch.object(queue_snapshot, "read_queue_snapshot") as read):
                self.assertEqual(self.bump_output(*arguments),
                    (0, "Usage: /build:bump <position|session|folder> | clear [unit]\nhttps://fixture/builds\n", ""))
                read.assert_not_called()

    def test_bump_with_words_saves_when_the_service_is_stopped_and_applies_on_restart(self) -> None:
        request = self.waiter(200, "berth")
        selected = self.fixture.server.waiters[request.episode].row.get("move_id", "")
        self.fixture.server.close()
        self.assertFalse(self.fixture.socket_path.exists())
        with (mock.patch.object(admission_client, "view", side_effect=wire.AdmissionUnavailable("no answer")) as view,
              mock.patch.object(admission_client, "priority_set",
                                side_effect=wire.AdmissionUnavailable("no answer")) as set_priority,
              mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "actor-id"}),
              mock.patch("time.time", return_value=123.0)):
            status, output, errors = self.bump_output("1", "--words", "/build:bump 1")
        self.assertEqual((status, errors), (0, ""))
        bump, = self.bumps()
        self.assertEqual(output, f"build queue: recorded {bump.id}\n")
        self.assertEqual(bump, person_actions.Bump(bump.id, person_actions.SetBump(selected, 1),
            "berth", "actor-id", 123.0, "/build:bump 1"))
        view.assert_not_called()
        set_priority.assert_not_called()
        self.assertIsInstance(admission_service.read_state(self.fixture.state_dir).priority, records.NoQueuePriority)
        self.fixture.restart()
        priority = self.fixture.server.priority
        assert isinstance(priority, records.QueuePriority)
        entry, = priority.entries
        self.assertEqual((entry.get("kind"), entry["unit"], entry["set_by"], entry["why"]),
                         ("UnitPriorityEntry", "berth", "actor-id", "/build:bump 1"))
        self.assertEqual(person_actions.bump_status(self.fixture.state_dir), ())

    def test_clear_one_unit_records_a_known_priority(self) -> None:
        _ = self.waiter(200, "demo")
        self.assertEqual(self.bump_output("1")[0], 0)
        _ = self.fixture.server.run_pass()
        with (mock.patch.object(records, "queue_command_actor", return_value="actor"),
              mock.patch.object(admission_client, "priority_clear") as socket_clear):
            status, output, errors = self.bump_output("clear", "demo", "--words=clear demo")
            socket_clear.assert_not_called()
        self.assertEqual((status, errors), (0, ""))
        bump = self.bumps()[-1]
        self.assertEqual(output, f"build queue: recorded {bump.id}\n")
        self.assertEqual((bump.action, bump.unit, bump.actor, bump.reason),
            (person_actions.ClearBump(), "demo", "actor", "clear demo"))

    def test_clear_unknown_unit_refuses_without_a_record(self) -> None:
        with mock.patch.object(admission_client, "priority_clear", return_value=wire.Refused("unknown unit missing")):
            self.assertEqual(self.bump_output("clear", "missing"),
                (3, "build queue: unknown unit missing; the queue priority did not change\n", ""))
        self.assertEqual(self.bumps(), [])

    def test_bump_stores_the_words_and_the_askers_staff_name(self) -> None:
        _ = self.waiter(100, "head")
        _ = self.waiter(200, "hana")
        member = staff.StaffRow(staff.StaffId("natemerge-id"), "unit", "natemerge", staff.StaffRoot(),
                                current_session=staff.StaffId("merge-session"))
        cases: tuple[tuple[str, str, str, tuple[staff.StaffRow, ...], records.Asker], ...] = (
            ("merge-session", "codex-session", "natemerge", (member,), records.StaffAsker("natemerge")),
            ("", "lone-session", "lone-session", (), records.UnstaffedSession("lone-session")),
            ("", "", "nate (plain shell)", (), records.PlainShellAsker()),
        )
        words = "natemerge can bump at will"
        for claude, codex, name, members, asker in cases:
            actor = claude or codex or "user"
            with (self.subTest(actor=actor),
                  mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": claude, "CODEX_THREAD_ID": codex}),
                  mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(members)),
                  mock.patch.object(buildlog_record, "queue_priority", wraps=buildlog_record.queue_priority) as logged):
                self.assertEqual(self.bump_output("2", "--words", words)[0], 0)
                _ = self.fixture.server.run_pass()
                self.assertEqual(self.fixture.queue_pids(), [200, 100])
                priority = self.fixture.server.priority
                assert isinstance(priority, records.QueuePriority)
                entry, = priority.entries
                self.assertEqual(entry, {"kind": "UnitPriorityEntry", "unit": "hana", "why": words,
                    "set_at": self.bumps()[-1].written_at, "set_by": actor, "asker": asker})
                self.assertEqual(head_facts.priority_text(priority), f'queue priority: hana ({name}: "{words}")')
                self.assertEqual(logged.call_args.args[2:4], (words, actor))
                self.assertEqual(self.bump_output("clear", "--words", "clear now")[0], 0)
                _ = self.fixture.server.run_pass()
                self.assertEqual(logged.call_args.args[2:4], ("clear now", actor))
                self.assertEqual(self.fixture.queue_pids(), [100, 200])

    def test_words_are_required_except_in_a_plain_shell(self) -> None:
        _ = self.waiter(100, "head")
        _ = self.waiter(200, "hana")
        refusal = "build queue: --words is required: pass the user's words; the queue priority did not change\n"
        for session, arguments in (("lone-session", ("2",)), ("", ("2",)),
                                   ("lone-session", ("clear",)), ("", ("clear",))):
            with (self.subTest(session=session, arguments=arguments),
                  mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session}),
                  mock.patch.object(buildlog_record, "queue_priority", wraps=buildlog_record.queue_priority) as logged):
                before = self.bumps()
                result = self.bump_output(*arguments)
                _ = self.fixture.server.run_pass()
                if session:
                    self.assertEqual(result, (3, refusal, ""))
                    self.assertEqual(self.bumps(), before)
                    logged.assert_not_called()
                else:
                    bump = self.bumps()[-1]
                    self.assertEqual(result, (0, f"build queue: recorded {bump.id}\n", ""))
                    self.assertEqual(logged.call_args.args[2:4], ("typed by nate in a plain shell", "user"))

    def test_bump_pass_applies_records_in_file_order_and_restart_applies_none_twice(self) -> None:
        _ = self.waiter(100, "head")
        tail = self.waiter(200, "tail")
        other = self.waiter(300, "other")
        for name, request in (("first", other), ("second", tail), ("repeat", other)):
            _ = self.save_for(request, name)
        self.assertEqual(self.priority_units(), [])
        _ = self.fixture.server.run_pass()
        self.assertEqual(self.priority_units(), ["other", "tail"])
        self.assertEqual(self.fixture.queue_pids(), [300, 200, 100])
        state = admission_service.read_state(self.fixture.state_dir)
        journal = self.fixture.state_dir / person_actions.JOURNAL_NAME
        self.assertEqual(state.bumps.offset, journal.stat().st_size)
        self.assertEqual({episode: move.from_place for episode, move in state.bumps.moves.items()},
                         {other.episode: 3, tail.episode: 3})
        self.fixture.restart()
        self.assertEqual(self.priority_units(), ["other", "tail"])
        self.assertEqual(admission_service.read_state(self.fixture.state_dir).bumps, state.bumps)
        self.assertEqual(len(self.priority_events()), 2)

    def test_bump_restart_applies_the_tail_after_the_state_write(self) -> None:
        first = self.waiter(200, "first")
        second = self.waiter(300, "second")
        _ = self.save_for(first, "first")
        _ = self.save_for(second, "second")
        self.fixture.fault.stage = "after_state_write"
        with self.assertRaises(InjectedCrash):
            _ = self.fixture.server.run_pass()
        self.assertEqual(self.priority_units(), ["first"])
        self.fixture.restart()
        self.assertEqual(self.priority_units(), ["first", "second"])
        self.assertEqual(person_actions.bump_status(self.fixture.state_dir), ())
        self.assertEqual(len(self.priority_events()), 2)

    def test_bump_set_then_clear_leaves_the_previous_priority(self) -> None:
        _ = self.waiter(100, "head")
        _ = self.waiter(200, "tail")
        self.assertEqual(self.bump_output("1")[0], 0)
        _ = self.fixture.server.run_pass()
        for arguments in (("2",), ("clear", "tail")):
            self.assertEqual(self.bump_output(*arguments)[0], 0)
        self.assertEqual(self.priority_units(), ["head"])
        _ = self.fixture.server.run_pass()
        self.assertEqual(self.priority_units(), ["head"])
        self.assertEqual(person_actions.bump_status(self.fixture.state_dir), ())

    def test_bump_for_a_departed_step_keeps_the_refusal_after_restart(self) -> None:
        request = self.waiter(200, "demo")
        _ = self.save_for(request, "gone")
        self.fixture.end_fixture_owner(200)
        _ = self.fixture.server.run_pass()
        self.assertEqual(self.priority_units(), [])
        expected = (person_actions.BumpNotApplied("demo", "the step left the queue"),)
        self.assertEqual(person_actions.bump_status(self.fixture.state_dir), expected)
        self.fixture.restart()
        self.assertEqual(person_actions.bump_status(self.fixture.state_dir), expected)

    def test_bump_passes_a_dash_selector_as_one_value(self) -> None:
        with (mock.patch.object(records, "queue_command_actor", return_value="showrunner"),
              mock.patch.object(build_queue, "bump", return_value=0) as run):
            self.assertEqual(self.bump_output("-x", "--words", "urgent"), (0, "", ""))
        run.assert_called_once_with("-x", "showrunner", "urgent")

    def test_unmatched_bump_selector_refuses_without_writing_priority(self) -> None:
        with mock.patch.object(unit_labels, "unit_labels_for", return_value={}):
            self.assertEqual(self.bump_output("missing"), (3, "build queue: no waiting step matches missing\n", ""))
        self.assertEqual(self.bumps(), [])

    def test_bump_uses_the_place_numbers_that_include_a_kept_place(self) -> None:
        _ = self.waiter(100, "head")
        worker = self.waiter(200, "worker")
        published = queue_snapshot.read_view_snapshot(self.fixture.state_dir)
        now = self.fixture.clock.now()
        kept = records.HeldPlace("queue", "fixture", "lint", now, now, now + 60, "4242-99",
                                 ("natedev",), 1, "failed", position=2)
        reading = replace(published.reading, held_places=(kept,))
        queue_snapshot.write_view_snapshot(self.fixture.state_dir, published.written_at, published.fresh_until,
                                          wire.encode_reply(wire.ViewReply(reading)).encode())
        with mock.patch.object(admission_client, "priority_set") as priority:
            self.assertEqual(self.bump_output("2"), (3, "build queue: place #2 is kept for queue\n", ""))
            self.assertEqual(self.bumps(), [])
            status, output, errors = self.bump_output("3")
            priority.assert_not_called()
        self.assertEqual((status, errors), (0, ""))
        bump, = self.bumps()
        self.assertEqual(output, f"build queue: recorded {bump.id}\n")
        step_key = self.fixture.server.waiters[worker.episode].row.get("move_id", "")
        self.assertTrue(step_key)
        self.assertEqual((bump.action, bump.unit), (person_actions.SetBump(step_key, 3), "worker"))

    def test_bump_requires_exactly_one_selector_without_running_priority(self) -> None:
        for selectors in (("berth", "other"), ("two words",), ("--run", "1")):
            with self.subTest(selectors=selectors):
                self.assertEqual(self.bump_output(*selectors),
                    (3, "build bump: use /build:bump <position|session|folder>\n", ""))
        self.assertEqual(self.bumps(), [])

    def test_bump_selectors_record_the_selected_step_and_unit(self) -> None:
        _ = self.waiter(100, "head")
        tail = self.waiter(200, "tail")
        labels = {tail.identity.session_id: unit_labels.UnitLabels("worker", unit="roster-tail")}
        selected = self.fixture.server.waiters[tail.episode].row.get("move_id", "")
        with mock.patch.object(unit_labels, "unit_labels_for", return_value=labels) as read_roster:
            self.assertEqual(self.bump_output("2")[0], 0)
            read_roster.assert_not_called()
            for selector in (tail.identity.session_id, "worker", "/fixture/tree-200", "tree-200"):
                with self.subTest(selector=selector):
                    self.assertEqual(self.bump_output(selector)[0], 0)
        self.assertEqual([(bump.action, bump.unit) for bump in self.bumps()],
                         [(person_actions.SetBump(selected, 2), "tail")] * 5)

    def test_bump_uses_the_session_label_when_the_waiter_has_no_unit(self) -> None:
        waiter = self.waiter(100, "")
        session = waiter.identity.session_id
        selected = self.fixture.server.waiters[waiter.episode].row.get("move_id", "")
        with mock.patch.object(memory_admit, "priority_labels",
                               return_value={session: unit_labels.UnitLabels("worker", unit="roster-unit")}) as read:
            self.assertEqual(self.bump_output("1")[0], 0)
        read.assert_called_once_with([session])
        bump, = self.bumps()
        self.assertEqual((bump.action, bump.unit), (person_actions.SetBump(selected, 1), "roster-unit"))

    def test_unitless_bump_refuses_without_writing_priority(self) -> None:
        _ = self.waiter(100, "")
        with mock.patch.object(memory_admit, "priority_labels", return_value={}):
            self.assertEqual(self.bump_output("1"), (3, "build queue: waiting step 1 has no unit\n", ""))
        self.assertEqual(self.bumps(), [])

    def test_waiter_with_no_session_or_unit_cannot_be_bumped(self) -> None:
        _ = self.waiter(100, "", session_id="")
        with mock.patch.object(memory_admit, "priority_labels") as read:
            self.assertEqual(self.bump_output("1"), (3, "build queue: waiting step 1 has no unit\n", ""))
        read.assert_not_called()
        self.assertEqual(self.bumps(), [])

    def test_clear_from_any_session_records_the_actor(self) -> None:
        for claude, codex, actor in (("ordinary-session", "codex-session", "ordinary-session"),
                                    ("", "codex-session", "codex-session"), ("", "", "user")):
            with (self.subTest(actor=actor),
                  mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": claude, "CODEX_THREAD_ID": codex}),
                  mock.patch.object(admission_client, "priority_clear", return_value=wire.Ok())):
                self.assertEqual(self.bump_output("clear", "--words", "clear now")[0], 0)
                self.assertTrue(self.bumps(), "clear must save a record")
                bump = self.bumps()[-1]
                self.assertEqual((bump.action, bump.unit, bump.actor, bump.reason),
                                 (person_actions.ClearBump(), "", actor, "clear now"))

    def test_bump_without_a_published_order_reports_unavailable(self) -> None:
        (self.fixture.state_dir / "queue.json").unlink()
        (self.fixture.state_dir / queue_snapshot.VIEW_NAME).unlink()
        self.assertEqual(self.bump_output("1"),
                         (2, "", "build queue: a numeric selector requires the published queue view\n"))
        self.assertEqual(self.bumps(), [])

    def test_numeric_bump_requires_the_published_view(self) -> None:
        _ = self.waiter(100, "head")
        worker = self.waiter(200, "worker")
        (self.fixture.state_dir / queue_snapshot.VIEW_NAME).unlink()

        self.assertEqual(self.bump_output("2"),
                         (2, "", "build queue: a numeric selector requires the published queue view\n"))
        self.assertEqual(self.bumps(), [])

        with mock.patch.object(unit_labels, "unit_labels_for", return_value={}):
            self.assertEqual(self.bump_output(worker.identity.folder)[0], 0)
        bump, = self.bumps()
        step_key = self.fixture.server.waiters[worker.episode].row.get("move_id", "")
        self.assertEqual((bump.action, bump.unit), (person_actions.SetBump(step_key, 2), "worker"))


class ServiceQueryTests(unittest.TestCase):
    def test_queue_read_returns_the_private_services_exact_reading(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            reply = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reply, wire.ViewReply)
            with fixture.serving():
                self.assertEqual(memory_admit.queue_read(), reply.reading)


if __name__ == "__main__":
    _ = unittest.main()
