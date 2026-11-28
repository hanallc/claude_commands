"""Registration behavior against a private seeded fixture."""

from __future__ import annotations
from scripts.lint.memory_test_support import config_environment, copy_config_fixture

import json
import io
import fcntl
import os
import shutil
import subprocess
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from datetime import datetime
from typing import IO, cast, final, override
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.production import session_staff, staff, showrunners
from scripts.production.production_doc import read_production
from scripts.agents import shutdown
from scripts.agents import remote
from scripts.production.rename_test_support import RenameDisplays
from scripts.production.cli_fixture import production_paths
from scripts.production.staff_test_support import fixture_member, showrunner_relationship
from scripts.production.update_registration_test_support import RegistrationFixture, review, registration
from scripts.console_text import check as console_text
from scripts.console_text.test_support import real_review_question

from scripts.production import footer_counts
from scripts.production import next_item
from scripts.production import tidy_wrapped
from scripts.hooks import showrunner_footer
from scripts.production.cli_fixture import invoke_cli
from scripts.production.staff_test_support import fixture_runtime_id
from scripts.review_links import change_browser, change_page
from scripts.statusline import counts_row
from scripts.lib import urgency
from scripts.lint.memory_test_support import write_config_fixture


class ProducerQuestionTests(RegistrationFixture):
    producer: staff.StaffRow = staff.StaffRow(staff.StaffId(""), "producer", "", staff.StaffRoot(), current_session=staff.StaffId(""))

    @override
    def setUp(self) -> None:
        super().setUp()
        self.producer = fixture_member("producer", name="dwight")
        listing = self.root / "reviews/change-review - example.md"
        _ = self.doc.write_text(self.doc.read_text() + f"- **Change review:** `{listing}`\n")
        config = self.root / "config"
        _ = copy_config_fixture(Path(__file__).parents[2] / 'config', config)
        settings = config / "review_links.conf"
        _ = write_config_fixture(settings, "delivery_pause_seconds=0\ndelivery_tries=3\n")
        self.env.update({"CLAUDE_CODE_SESSION_ID": self.producer.current_session, "CODEX_THREAD_ID": "",
                         "CHANGE_REVIEW_DIR": str(self.root / "reviews"), "REVIEW_LINKS_ROOT": str(self.root / "links"),
                         "CONF_DIR": str(config)})
        self.enterContext(mock.patch.dict(os.environ, self.env))

    def file_question(self, title: str = "Choose the next project") -> None:
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(title + "?")):
            result = invoke_cli(registration, registration.main, ["ask", "--text", title, "--ask", title + "?"],
                                self.env, self.checkout, production_state=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_producers_file_reviewed_questions_without_a_production(self) -> None:
        for name in ("dwight", "morty", "brain"):
            with self.subTest(name=name):
                self.producer = fixture_member("producer", name=name)
                self.env.update({"CLAUDE_CODE_SESSION_ID": "" if name == "brain" else self.producer.current_session,
                                 "CODEX_THREAD_ID": self.producer.current_session if name == "brain" else ""})
                self.file_question(name)
                identities = registration.QuestionStaff.read()
                records = registration.question_records_for_document(self.producer, identities)
                self.assertEqual(len(records), 1)
                question = records[0]
                self.assertEqual((question.owner, question.asker), (self.producer.id, self.producer.id))
                self.assertEqual(question.path.parent, self.state / "showrunner" / self.producer.id / "questions")
                self.assertEqual(question.item.get("reviewed"), name + "?")
                self.assertFalse(list((self.root / "issues").glob("question-*.md")))

    def test_producer_questions_use_the_existing_review_and_caller_rules(self) -> None:
        held = review.QuestionAwaitingFacts("Which project?", ("Give the project names.",))
        with mock.patch.object(review, "review_question", return_value=held):
            result = invoke_cli(registration, registration.main, ["ask", "--ask", "Which project?"],
                                self.env, self.checkout, production_state=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("question held for review", result.stdout)
        self.assertFalse(list((self.state / "showrunner" / self.producer.id).glob("questions/*.md")))
        for kind in ("showrunner", "unit"):
            member = next(row for row in staff.snapshot().rows if row.kind == kind)
            environment = {**self.env, "CLAUDE_CODE_SESSION_ID": member.current_session}
            with mock.patch.object(review, "review_question", side_effect=AssertionError("No review without an owner")):
                result = invoke_cli(registration, registration.main, ["ask", "--ask", "Which project?"],
                                    environment, self.checkout, production_state=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("pass --unit", result.stdout)

    def use_console_verdicts(self, *failed_lines: tuple[str, ...]) -> None:
        replies = iter(failed_lines)

        def judge(*_args: object, prompt: str, **_options: object) -> review.StructuredCall:
            request = cast(dict[str, object], json.loads(prompt.split("\n", 2)[2]))
            pending = cast(dict[str, object], request["parts"])
            parts: list[dict[str, object]] = [
                {"number": int(number), "questions": [], "failed_rules": []} for number in pending
            ]
            return review.StructuredCall("sonnet", review.StructuredAnswer({
                "draft": {"questions": [], "failed_rules": [
                    {"rule": "console-standard-4", "quote": line} for line in next(replies)
                ]},
                "parts": parts,
            }))

        def runner(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            text = "Question rules" if argv[0] == "zsh" else "agent=sonnet effort=high"
            return subprocess.CompletedProcess(argv, 0, text, "")

        def question_review(key: review.QuestionKey, draft: str, *, console_kind: str) -> review.QuestionOutcome:
            return real_review_question(key, draft, runner=runner, console_kind=console_kind)

        self.env["NOTIFY_STATE_DIR"] = str(self.state / "notify")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.env)))
        _ = self.enterContext(mock.patch.object(review, "GUIDE", self.root))
        _ = self.enterContext(mock.patch.object(review, "reviewer_environment", return_value=({}, "claude2")))
        _ = self.enterContext(mock.patch.object(review, "reviewer_binary",
                                                return_value=review.ResolvedReviewerBinary(self.root / "claude")))
        _ = self.enterContext(mock.patch.object(review, "structured_call", side_effect=judge))
        _ = self.enterContext(mock.patch.object(review, "review_question", side_effect=question_review))

    def ask_with_review(self, *args: str) -> subprocess.CompletedProcess[str]:
        return invoke_cli(registration, registration.main, ["ask", *args], self.env, self.checkout,
                          production_state=True)

    def held_review_id(self, result: subprocess.CompletedProcess[str]) -> str:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        _, hint, rest = result.stdout.rpartition("Use --revise ")
        self.assertTrue(hint, result.stdout)
        return rest.split()[0]

    def test_ask_revision_preserves_passed_lines_and_rechecks_the_changed_title(self) -> None:
        initial = "Try again?"
        revised = "Retry the download?"
        fact = "The download stopped."
        self.use_console_verdicts((initial,), (revised, fact))

        first = self.ask_with_review("--ask", initial, "--context", revised, "--item", fact)
        review_id = self.held_review_id(first)
        second = self.ask_with_review("--revise", review_id, "--ask", revised, "--item", fact)
        self.assertEqual(self.held_review_id(second), review_id)
        self.assertIn(f"console-standard-4: {revised}", second.stdout)
        self.assertNotIn(f"console-standard-4: {fact}", second.stdout)

    def test_ask_same_title_keeps_separate_draft_verdicts(self) -> None:
        title = "Try again?"
        shared = "The download stopped."
        self.use_console_verdicts((title,), (shared,))

        first = self.ask_with_review("--ask", title, "--context", shared, "--item", "The first file is new.")
        second = self.ask_with_review("--ask", title, "--context", shared, "--item", "The second file is new.")
        self.assertNotEqual(self.held_review_id(first), self.held_review_id(second))
        self.assertIn(f"console-standard-4: {shared}", second.stdout)
        self.assertEqual(len(list((review.state_path() / "question-verdicts").glob("*.json"))), 2)

    def test_ask_migrates_the_verdict_from_the_previous_title(self) -> None:
        initial = "Try again?"
        revised = "Retry the download?"
        fact = "The download stopped."
        self.use_console_verdicts((initial,), (revised, fact), (fact,))
        legacy = review.QuestionKey(self.producer.name, initial)
        outcome = console_text.check(f"{initial}\n\n1. {fact}", "question", key=legacy, title=initial)
        self.assertIsInstance(outcome, review.QuestionReviewFailed)
        folder = review.state_path() / "question-verdicts"
        legacy_path, = folder.glob("*.json")

        migrated = self.ask_with_review("--revise-title", initial, "--ask", revised, "--item", fact)
        _ = self.held_review_id(migrated)
        self.assertIn(f"console-standard-4: {revised}", migrated.stdout)
        self.assertNotIn(f"console-standard-4: {fact}", migrated.stdout)
        self.assertFalse(legacy_path.exists())
        self.assertEqual(len(list(folder.glob("*.json"))), 1)

        new = self.ask_with_review("--ask", initial, "--item", fact)
        _ = self.held_review_id(new)
        self.assertIn(f"console-standard-4: {fact}", new.stdout)

    def test_the_producer_page_count_and_next_share_the_question_store(self) -> None:
        self.file_question()
        status = change_page.DeliveryStatus(self.root / "links")
        browser = change_page.ReviewBrowser("producer-test", kind="questions")
        prepared = change_browser.prepare(browser, status)
        self.assertEqual([question.tab for question in prepared.questions], ["dwight"])
        body = change_browser.render(browser, status).decode()
        self.assertIn('data-tab="dwight"', body)
        self.assertIn('value="dwight">dwight (1)</option>', body)
        self.assertIn('name="outcome" value="answer"', body)
        self.assertNotIn("No showrunner owns this question.", body)
        instance = self.state / "notifier" / f"showrunner-{self.producer.id}"
        sources: list[Path] = []
        self.assertEqual(counts_row.row(instance, sources, require_complete=True), ("Review: 1 question", ""))
        question = prepared.questions[0].record
        self.assertIn(question.path, sources)
        self.assertIn("Choose the next project?", next_item.show("question", urgency.URGENT, None))
        _ = staff.rename(self.producer.id, "renamed-dwight")
        self.assertEqual([question.tab for question in change_browser.prepare(browser, status).questions], ["renamed-dwight"])

    def test_producer_answers_retry_the_current_session_and_recover_after_restart(self) -> None:
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                title = f"Project choice {recovery}"
                self.file_question(title)
                status = change_page.DeliveryStatus(self.root / "links")
                browser = change_page.ReviewBrowser("producer-test", kind="questions")
                question = next(item for item in change_browser.prepare(browser, status).questions if item.record.item["text"] == title)
                delivery, answer = change_browser.submit_question(question, "answer", "Choose the first project.", status)
                self.assertEqual(answer, "Choose the first project.")
                current = staff.by_id(self.producer.id)
                assert current is not None
                replacement = staff.StaffId(fixture_runtime_id(f"producer-answer-{recovery}"))
                _ = staff.update_current_session(current.current_session, replacement)
                self.env["CLAUDE_CODE_SESSION_ID"] = replacement
                calls: list[tuple[str, str]] = []
                delivery_key = change_page.review_delivery_key(delivery.page.review_list, delivery.row, delivery.attempt)

                def sender(recipient: str, name: str, message: str, *, key: str, recipient_key: str) -> None:
                    del name
                    self.assertEqual((key, recipient_key), (delivery_key, self.producer.id))
                    calls.append((recipient, message))
                    if len(calls) == 1:
                        raise RuntimeError("fixture sender unavailable")

                with mock.patch.object(change_page, "send_message", side_effect=sender):
                    if recovery:
                        change_page.recover_question_answers(change_page.DeliveryStatus(self.root / "links"))
                    else:
                        change_page.deliver(delivery, status)
                self.assertEqual([recipient for recipient, _ in calls], [f"session:{replacement}"] * 2)
                self.assertTrue(all("Choose the first project." in message for _, message in calls))
                self.assertEqual(status.slots(delivery.page, delivery.row).intents(), ())
                self.assertEqual(registration.question_records_for_document(self.producer, registration.QuestionStaff.read())[-1].status,
                                 "closed")

    def test_producer_delivery_exhaustion_alerts_the_configured_showrunner(self) -> None:
        runner = staff.snapshot().by_id(self.owner_id)
        assert runner is not None
        settings = self.root / "config" / "review_links.conf"
        _ = write_config_fixture(settings, f"unrouted_alert_recipient={runner.name}\n")
        self.file_question()
        status = change_page.DeliveryStatus(self.root / "links")
        browser = change_page.ReviewBrowser("producer-test", kind="questions")
        question = change_browser.prepare(browser, status).questions[0]
        delivery, _ = change_browser.submit_question(question, "answer", "Choose the first project.", status)
        calls: list[str] = []
        keys: list[str] = []

        def sender(recipient: str, name: str, message: str, *, key: str, recipient_key: str) -> None:
            del name
            alert = "delivery needs attention" in message
            target = runner if alert else self.producer
            self.assertEqual((recipient, recipient_key), (f"session:{target.current_session}", target.id))
            calls.append(message)
            keys.append(key)
            if not alert:
                raise RuntimeError("fixture sender unavailable")

        with mock.patch.object(change_page, "send_message", side_effect=sender):
            change_page.deliver(delivery, status)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(set(keys[:3])), 1)
        self.assertNotEqual(keys[0], keys[-1])
        self.assertIn("Tries: 3", calls[-1])
        self.assertIn("Choose the first project.", calls[-1])


class RegistrationTests(RegistrationFixture):
    def test_unreadable_timer_reports_without_rewriting_a_legacy_prompt(self) -> None:
        timer = self.state / "notifier" / f"showrunner-{self.owner_id}"
        timer.mkdir(parents=True)
        _ = (timer / "conf").write_bytes(b"TARGET=\xff\n")
        prompt = self.state / "showrunner" / self.owner_id / "prompt.txt"
        prompt.parent.mkdir(parents=True)
        _ = prompt.write_text("old prompt\n")
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            lookup = showrunners.timer_scratch(self.owner_id, record=staff.snapshot())
        self.assertIsInstance(lookup, showrunners.TimerConfigurationUnreadable)

        result = self.run_command("log", "adopt the prompt")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("timer configuration unreadable", result.stderr)
        self.assertIn("decode", result.stderr)
        self.assertEqual(prompt.read_text(), "old prompt\n")
        self.assertNotIn("rewrote", result.stdout)

    def test_start_and_resume_retarget_only_updates_and_retire_old_session(self) -> None:
        doc_bytes = self.doc.read_bytes()
        first = self.run_command("register")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("register: ok", first.stdout)
        initial_calls = self.notifier_calls()
        self.assertEqual(len([call for call in initial_calls if call[:2] == ["new", f"showrunner-{self.fixture_staff_id()}"]]), 1)
        updates = next(call for call in initial_calls if call[:2] == ["new", f"showrunner-{self.fixture_staff_id()}"])
        self.assertEqual(updates[updates.index("--to") + 1], f"session:{self.fixture_staff_id()}")
        self.assertEqual(updates[updates.index("--prompt") + 1], "scheduled")
        self.assertNotIn("--prompt-file", updates)
        self.assertNotIn("--from", updates)
        self.assertFalse((self.state / "showrunner" / self.owner_id / "prompt.txt").exists())
        self.assertIn("automatic reports are off", first.stdout)
        for instance in ("stall-watch",):
            self.assertEqual(len([call for call in initial_calls if call[:2] == ["new", instance]]), 1)

        new_env = {**self.env, "CLAUDE_CODE_SESSION_ID": "resume-session-id"}
        resumed = self.run_command("register", "--session", "resumed-showrunner", env=new_env)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        repeated = self.run_command("register", "--session", "resumed-showrunner", env=new_env)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        # The showrunner's name is written nowhere: no registry, no doc line, no commit.
        self.assertFalse(self.config.exists())
        self.assertEqual(self.doc.read_bytes(), doc_bytes)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
        calls = self.notifier_calls()
        self.assertEqual(len([call for call in calls if call[:2] == ["new", f"showrunner-{self.fixture_staff_id()}"]]), 3)
        updates = [call for call in calls if call[:2] == ["new", f"showrunner-{self.fixture_staff_id()}"]]
        self.assertEqual([call[call.index("--to") + 1] for call in updates],
                         [f"session:{self.fixture_staff_id()}"] * 3)
        for instance in ("stall-watch",):
            self.assertEqual(len([call for call in calls if call[:2] == ["new", instance]]), 1)

    def test_on_demand_keeps_a_stopped_timer_as_the_showrunners_record(self) -> None:
        first = self.run_command("register")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("- **Updates:** every 15 minutes", "- **Updates:** on demand"),
                                encoding="utf-8")
        _ = self.git("commit", "-qam", "reports on demand")
        before = len(self.notifier_calls())
        resumed = self.run_command("register")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn(f"register: ok — showrunner-{self.fixture_staff_id()} registered; automatic reports are off", resumed.stdout)
        calls = self.notifier_calls()[before:]
        self.assertIn(["new", f"showrunner-{self.fixture_staff_id()}"], [call[:2] for call in calls])
        self.assertIn(["stop", f"showrunner-{self.fixture_staff_id()}"], calls)
        self.assertNotIn(["remove", f"showrunner-{self.fixture_staff_id()}"], calls)
        self.assertEqual((self.state / f"instance-showrunner-{self.fixture_staff_id()}").read_text(encoding="utf-8"), "stopped")
        again = self.run_command("register")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        # A re-register retargets the timer and stops it again, so it stays stopped.
        self.assertEqual([call[0] for call in self.notifier_calls()[before + len(calls):]],
                         ["new", "stop", "status"])
        self.assertEqual((self.state / f"instance-showrunner-{self.fixture_staff_id()}").read_text(encoding="utf-8"), "stopped")

    def test_a_new_production_with_no_updates_line_starts_quiet(self) -> None:
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("- **Updates:** every 15 minutes\n", ""), encoding="utf-8")
        _ = self.git("commit", "-qam", "no Updates line")
        first = self.run_command("register")

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn(f"register: ok — showrunner-{self.fixture_staff_id()} registered; automatic reports are off", first.stdout)
        self.assertEqual((self.state / f"instance-showrunner-{self.fixture_staff_id()}").read_text(encoding="utf-8"), "stopped")
        self.assertNotIn("footer", first.stdout)

    def test_a_legacy_interval_keeps_the_registration_stopped(self) -> None:
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("every 15 minutes", "every 20 minutes"), encoding="utf-8")
        _ = self.git("commit", "-qam", "every 20 minutes")

        result = self.run_command("register")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updates = next(call for call in self.notifier_calls() if call[:2] == ["new", f"showrunner-{self.fixture_staff_id()}"])
        self.assertEqual(updates[updates.index("--every") + 1], str(registration.DEFAULT_MINUTES))
        self.assertIn(["stop", f"showrunner-{self.fixture_staff_id()}"], self.notifier_calls())
        self.assertIn("automatic reports are off", result.stdout)

    def test_register_requires_the_environment_session_id(self) -> None:
        without_id = {key: value for key, value in self.env.items() if key != "CLAUDE_CODE_SESSION_ID"}
        missing_id = self.run_command("register", "--session", "first-showrunner", env=without_id)
        self.assertEqual(missing_id.returncode, 2)
        self.assertIn("register: failed", missing_id.stdout + missing_id.stderr)
        self.assertEqual(self.notifier_calls(), [])
        self.assertFalse(self.config.exists())
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_register_refuses_dirty_production_doc_without_side_effects(self) -> None:
        with self.doc.open("a", encoding="utf-8") as output:
            _ = output.write("\nA local note.\n")
        before = self.doc.read_bytes()
        result = self.run_command("register", "--session", "resumed-showrunner")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("register: failed — the production doc has uncommitted edits; commit or discard them first", result.stdout)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.notifier_calls(), [])
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_register_warns_for_a_session_not_named_for_the_production_and_still_registers(self) -> None:
        record = self.state / "sessions" / f"{os.getpid()}.json"
        _ = record.write_text(json.dumps({"pid": os.getpid(), "sessionId": "current-session-id", "name": "other"}),
                              encoding="utf-8")
        result = self.run_command("register")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("register: warn", result.stdout)
        self.assertNotIn("production example has showrunner other", result.stdout)
        self.assertNotIn("/agents rename example other", result.stdout)
        with mock.patch.dict(os.environ, self.env):
            _ = staff.rename(staff.StaffId(self.owner_id), "renamed-example")
        renamed = self.run_command("register")
        self.assertEqual(renamed.returncode, 0, renamed.stdout + renamed.stderr)
        self.assertNotIn("register: warn", renamed.stdout)
        updates = [call for call in self.notifier_calls() if call[:2] == ["new", f"showrunner-{self.owner_id}"]]
        self.assertEqual([call[call.index("--to") + 1] for call in updates], [f"session:{self.owner_id}"] * 2)
        self.assertIn("register: ok", result.stdout)
        self.assertNotEqual(self.notifier_calls(), [])


@final
class QuestionIdentityTests(RegistrationFixture):
    asker_id: staff.StaffId = staff.StaffId("")
    issues: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.env.update(config_environment(self.root / "fixture-config"))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.env)))
        self.issues = self.root / "issues"
        self.issues.mkdir(exist_ok=True)
        self.timer("example")
        parent = showrunner_relationship("example")
        self.asker_id = fixture_member("unit", name="alpha", parent=parent.id,
                                       worktree=staff.PresentWorktree(remote.local_machine(), self.root / "alpha")).id

    def file_question(self, title: str = "Choose the layout", question: str = "Keep the layout?") -> None:
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(question)):
            result = self.run_command("ask", "--text", title, "--ask", question,
                                      "--unit", "alpha")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.question_rows()
        self.assertTrue(rows, "The filed question needs a canonical record")
        self.assertEqual(rows[-1]["asked_by"], self.asker_id)

    def question_rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for path in sorted(self.issues.glob("*.md")):
            lines = path.read_text(encoding="utf-8").splitlines()
            if "record_type: question" not in lines or "status: open" not in lines:
                continue
            saved = next(line.removeprefix("registration: ") for line in lines
                         if line.startswith("registration: "))
            rows.append(cast(dict[str, object], json.loads(saved)))
        return rows

    @contextmanager
    def forbid_legacy_question_access(self) -> Generator[None]:
        legacy = self.outstanding()
        real_open = Path.open

        def open_file(path: Path, mode: str = "r", buffering: int = -1,
                      encoding: str | None = None, errors: str | None = None,
                      newline: str | None = None) -> IO[str] | IO[bytes]:
            if path == legacy:
                raise AssertionError("A migrated question must use its record")
            return cast(IO[str] | IO[bytes], real_open(path, mode, buffering, encoding, errors, newline))

        with (mock.patch.dict(os.environ, self.env), production_paths(self.env),
              mock.patch.object(Path, "open", open_file)):
            yield

    def migrate(self) -> None:
        migrated = self.run_command("migrate-questions")
        self.assertEqual(migrated.returncode, 0, migrated.stdout + migrated.stderr)
        path = self.outstanding()
        if path.exists():
            self.assertEqual(json.loads(path.read_text()), [])
            path.unlink()

    def test_migrated_question_commands_never_access_the_legacy_list(self) -> None:
        self.file_question()
        self.migrate()
        with self.forbid_legacy_question_access():
            for command in (("open",), ("next",), ("questions-table",),
                            ("mark-important", "--item", "Choose the layout"),
                            ("questions", "--state", str(self.question_state(["Choose the layout"]))),
                            ("answer", "--item", "Choose the layout")):
                with self.subTest(command=command):
                    result = self.run_command(*command)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.question_rows(), [])

    def test_next_reader_keeps_the_migrated_question_and_its_queue_key(self) -> None:
        legacy = {"since": "2026-10-06T08:00", "text": "Choose the layout",
                                        "question": "Keep the layout?", "reviewed": "Keep the layout?",
                                        "asked_by": "alpha", "urgency": "user"}
        _ = self.outstanding().write_text(json.dumps([legacy]))
        self.migrate()
        with self.forbid_legacy_question_access():
            owner = registration.QuestionStaff.read().owner(self.doc)
            after = next_item.read_queue("question", self.doc)
        self.assertEqual(after.name, f"question-{owner.id}")
        self.assertEqual(len(after.entries), 1)
        self.assertEqual(after.entries[0].key, f"{owner.id}: Choose the layout")
        self.assertEqual(after.entries[0].item,
                         "example [user], since Tue Oct 06 08:00, asked by alpha: Keep the layout?")

    def test_footer_counts_read_migrated_records_without_the_legacy_list(self) -> None:
        self.file_question()
        self.migrate()
        with self.forbid_legacy_question_access():
            counts = footer_counts.read_counts(self.doc,
                                                questions=len(registration.read_open_items(self.outstanding())))
            sources = footer_counts.sources(self.doc)
        self.assertEqual(counts.questions, 1)
        self.assertEqual(footer_counts.counts_line(counts), "Review: 1 question")
        for record in self.issues.glob("*.md"):
            self.assertIn(record, sources)


    def test_showrunner_footer_reads_migrated_records_without_the_legacy_list(self) -> None:
        self.file_question()
        self.migrate()
        production = showrunner_footer.Production("example", self.doc, "America/Los_Angeles",
                                                  showrunners.owner_for("example"))
        with self.forbid_legacy_question_access():
            self.assertEqual(showrunner_footer.open_questions([production]), 1)

    def test_rename_state_keeps_a_migrated_question_and_its_fixed_asker(self) -> None:
        self.file_question()
        self.migrate()
        before = {path: path.read_bytes() for path in self.issues.glob("*.md")}
        row = staff.by_id(self.asker_id)
        assert row is not None
        removed = self.root / "removed-asker-worktree"
        self.assertFalse(removed.exists())
        row = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), removed),
                                 expected=row.worktree)
        displays = RenameDisplays(row, live=False)
        with (self.forbid_legacy_question_access(),
              displays.installed(), redirect_stdout(io.StringIO())):
            _ = shutdown.rename_apply(str(self.asker_id), "renamed")
            renamed = staff.by_id(self.asker_id)
            assert renamed is not None
            self.assertEqual((renamed.id, renamed.name), (self.asker_id, "renamed"))
            _ = shutdown.rename_apply(str(self.asker_id), "renamed")
        self.assertEqual({path: path.read_bytes() for path in self.issues.glob("*.md")}, before)
        self.assertEqual(self.question_rows()[0]["asked_by"], self.asker_id)

    def test_tidy_removes_only_the_wrapped_productions_open_questions(self) -> None:
        self.file_question()
        self.migrate()
        proposal = self.issues / "Keep this proposal.md"
        _ = proposal.write_text("---\nstatus: open\ntags: [proposal]\n---\nKeep this proposal.\n")
        before = proposal.read_bytes()
        _ = self.doc.write_text(self.doc.read_text() + "\n> **Status: PRODUCTION — wrapped.** Complete.\n")
        shutil.rmtree(self.state / f"notifier/showrunner-{self.fixture_staff_id()}")
        with (self.forbid_legacy_question_access(),
              mock.patch.object(tidy_wrapped, "STATE_DIR", self.state / "showrunner"),
              mock.patch.object(tidy_wrapped, "NOTIFIER_STATE_DIR", self.state / "notifier"),
              redirect_stdout(io.StringIO())):
            result = tidy_wrapped.main([str(self.doc)])
        self.assertEqual(result, 0)
        self.assertEqual(self.question_rows(), [])
        self.assertEqual(proposal.read_bytes(), before)

    def test_migration_rerun_completes_after_the_record_write(self) -> None:
        legacy = {"since": "2026-10-06T08:00", "text": "Choose the layout",
                  "question": "Keep the layout?", "asked_by": "alpha",
                  "review_unavailable": "offline", "after": "2099-01-02T08:00"}
        _ = self.outstanding().write_text(json.dumps([legacy]))
        original = self.outstanding().read_bytes()
        replace = os.replace

        def stop_after_record(source: Path, destination: Path) -> None:
            if Path(destination) == self.outstanding():
                self.assertEqual(len(self.question_rows()), 1)
                raise KeyboardInterrupt("Stop before the legacy list removal")
            replace(source, destination)

        with mock.patch.object(os, "replace", side_effect=stop_after_record), self.assertRaises(KeyboardInterrupt):
            _ = self.run_command("migrate-questions")
        self.assertEqual(self.outstanding().read_bytes(), original)
        self.migrate()
        rows = self.question_rows()
        self.assertEqual(rows, [{**legacy, "asked_by": self.asker_id, "review_id": "Q1"}])
        remaining: object = cast(object, json.loads(self.outstanding().read_text())) if self.outstanding().exists() else []
        self.assertEqual(remaining, [])
        before = self.store_snapshot()
        self.migrate()
        self.assertEqual(self.store_snapshot(), before)

    def test_interrupted_migration_keeps_unwritten_questions_during_ordinary_writes(self) -> None:
        for action in ("answer", "questions"):
            with self.subTest(action=action):
                for path in self.issues.glob("*.md"):
                    path.unlink()
                legacy = [{"since": "2026-10-06T08:00", "text": title,
                           "question": title + "?", "asked_by": str(self.asker_id)}
                          for title in ("Choose the layout", "Choose the compiler")]
                _ = self.outstanding().write_text(json.dumps(legacy))
                replace = os.replace
                written: list[Path] = []

                def stop_after_first_record(source: Path, destination: Path) -> None:
                    target = Path(destination)
                    if target.suffix == ".md" and target.parent == self.issues:
                        if written:
                            raise KeyboardInterrupt("Stop after the first question record")
                        written.append(target)
                    replace(source, destination)

                with (mock.patch.object(os, "replace", side_effect=stop_after_first_record),
                      self.assertRaises(KeyboardInterrupt)):
                    _ = self.run_command("migrate-questions")
                self.assertEqual(len(written), 1)
                if action == "answer":
                    result = self.run_command("answer", "--item", "Choose the layout")
                    expected = ["Choose the compiler"]
                else:
                    state = self.question_state(["Choose the layout"])
                    result = self.run_command("questions", "--state", str(state))
                    expected = ["Choose the layout", "Choose the compiler"]
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.migrate()
                self.assertCountEqual([row["text"] for row in self.question_rows()], expected)

    def test_migration_keeps_identical_question_text_from_distinct_askers(self) -> None:
        owner = staff.by_id(self.asker_id)
        assert owner is not None
        second = fixture_member("unit", name="beta", parent=owner.parent,
                                worktree=staff.PresentWorktree(remote.local_machine(), self.root / "beta"))
        legacy = [{"since": "2026-10-06T08:00", "text": "Choose the layout",
                   "question": "Keep the layout?", "asked_by": str(asker)}
                  for asker in (self.asker_id, second.id)]
        _ = self.outstanding().write_text(json.dumps(legacy))
        self.migrate()
        rows = self.question_rows()
        self.assertEqual(len(rows), 2)
        self.assertCountEqual([row["asked_by"] for row in rows], [self.asker_id, second.id])
        before = self.store_snapshot()
        self.migrate()
        self.assertEqual(self.store_snapshot(), before)

    def test_next_keeps_local_items_when_a_remote_child_has_a_local_reminder(self) -> None:
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "Choose the layout", "question": "Choose the layout?"})
        parent = staff.by_id(self.asker_id)
        assert parent is not None
        child = fixture_member("showrunner", name="remote-child", parent=parent.parent,
                               doc=staff.LiveDoc("macbook", self.doc.with_name("remote-child-production.md")))
        reminder = self.outstanding().with_name("remote-child.json")
        _ = reminder.write_text(json.dumps([{"since": "2026-10-06T08:00", "text": "Choose the compiler"}]))
        before = reminder.read_bytes()
        errors = io.StringIO()
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            identities = registration.QuestionStaff.read()
            self.assertIn(child.name, identities.names("example", below=True))
            self.assertNotIn(child.name, identities.documents())
            owner = identities.owner(self.doc)
            with redirect_stderr(errors):
                queue = next_item.read_queue("question", self.doc, below=True, record=identities.record)
        self.assertEqual([entry.key for entry in queue.entries], [f"{owner.id}: Choose the layout"])
        self.assertEqual(errors.getvalue().splitlines(), ["questions: remote-child: ownership evidence is unavailable"])
        self.assertEqual(reminder.read_bytes(), before)

    def test_descendant_without_a_timer_remains_in_next_and_answer_search(self) -> None:
        child_doc = self.document_for("child")
        parent = staff.by_id(self.asker_id)
        assert parent is not None
        child = fixture_member("showrunner", name="child", parent=parent.parent,
                               doc=staff.LiveDoc(remote.local_machine(), child_doc))
        self.write_open_for("child", {"since": "2026-10-06T08:00", "text": "Choose the compiler",
                                      "question": "Use this compiler?", "asked_by": str(child.id)})
        self.assertFalse((self.state / "notifier/showrunner-child").exists())
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            queue = next_item.read_queue("question", self.doc, below=True)
        self.assertEqual([entry.key for entry in queue.entries], [f"{child.id}: Choose the compiler"])
        answer = self.run_command("answer", "--item", "Choose the compiler")
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        self.assertIn("child: Choose the compiler", answer.stdout)
        self.assertEqual(self.question_rows(), [])

    def test_questions_table_uses_one_staff_snapshot_without_an_explicit_document(self) -> None:
        self.file_question()
        from scripts.production.cli_fixture import invoke_cli
        owner = staff.by_id(self.fixture_staff_id())
        assert owner is not None
        environment = {**self.env, "CLAUDE_CODE_SESSION_ID": owner.current_session, "CODEX_THREAD_ID": ""}
        with (mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshot,
              mock.patch.object(session_staff, "member_sources",
                                wraps=session_staff.member_sources) as sources):
            result = invoke_cli(registration, registration.main, ["questions-table"],
                                environment, self.checkout, production_state=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Choose the layout", result.stdout)
        snapshot.assert_called_once_with()
        self.assertEqual(sources.call_count, 1)

    def test_footer_ignores_questions_when_the_document_has_no_local_staff_owner(self) -> None:
        self.file_question()
        owner = staff.by_id(self.asker_id)
        assert owner is not None and isinstance(owner.parent, staff.ReportsTo)
        parent = owner.parent.id
        record = staff.snapshot()
        rows = tuple(replace(row, doc=staff.LiveDoc("macbook", self.doc)) if row.id == parent else row
                     for row in record.rows)
        with staff.supplied_snapshot(replace(record, rows=rows)):
            with mock.patch.dict(os.environ, self.env), production_paths(self.env):
                production = showrunner_footer.Production("example", self.doc, "America/Los_Angeles",
                                                      showrunners.owner_for("example"))
                errors = io.StringIO()
                with redirect_stderr(errors):
                    self.assertEqual(showrunner_footer.open_questions([production]), 0)
                self.assertIn("questions: unavailable", errors.getvalue())
                self.assertEqual(registration.read_open_items(self.outstanding()), [])
                self.assertIn(self.doc, footer_counts.sources(self.doc))
            refused = self.run_command("ask", "--text", "Choose the compiler", "--ask", "Use it?")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("no showrunner staff row", refused.stdout + refused.stderr)

    def store_snapshot(self) -> dict[str, tuple[bytes, int]]:
        paths = [*self.issues.rglob("*.md"), *self.outstanding().parent.glob("*.json"),
                 *self.outstanding().parent.glob("*.jsonl")]
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    def test_answer_resolves_the_fixed_asker_after_a_rename(self) -> None:
        self.file_question()
        _ = staff.rename(self.asker_id, "renamed")
        references: list[str] = []
        resolve = staff.StaffSnapshot.by_member_reference

        def resolve_reference(record: staff.StaffSnapshot, reference: str) -> staff.StaffRow | None:
            references.append(reference)
            return resolve(record, reference)

        with mock.patch.object(staff.StaffSnapshot, "by_member_reference", resolve_reference):
            result = self.run_command("answer", "--item", "Choose the layout")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(self.asker_id), references)
        self.assertIn("renamed", result.stdout)
        self.assertNotIn("relay: to alpha", result.stdout)

        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            production = read_production("example")
            identities = registration.QuestionStaff.read()
            asker = registration.StaffAsker(self.asker_id)
            relay = registration.answer_relay(production, production.name, asker, identities)
            self.assertEqual(relay.recipient, self.asker_id)
            self.assertEqual(relay.line, registration.relay_line(production, production.name, asker, identities))
            with self.assertRaises(registration.RegistrationFailure):
                _ = registration.answer(production, "Choose the layout", identities=identities, only_open=True)

    def test_answer_resolves_the_fixed_asker_after_a_transfer(self) -> None:
        self.file_question()
        destination = fixture_member("showrunner", name="destination")
        _ = staff.move(self.asker_id, staff.ReportsTo(destination.id))
        references: list[str] = []
        resolve = staff.StaffSnapshot.by_member_reference

        def resolve_reference(record: staff.StaffSnapshot, reference: str) -> staff.StaffRow | None:
            references.append(reference)
            return resolve(record, reference)

        with mock.patch.object(staff.StaffSnapshot, "by_member_reference", resolve_reference):
            result = self.run_command("answer", "--item", "Choose the layout")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(self.asker_id), references)
        self.assertIn("destination", result.stdout)
        self.assertIn("alpha", result.stdout)

    def test_followup_waits_for_the_reply_and_keeps_the_exchange(self) -> None:
        self.file_question()
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            production = read_production("example")
            relay = registration.followup(production, "Choose the layout", "Which file holds it?")
            self.assertEqual(relay.recipient, self.asker_id)
            with self.assertRaises(registration.RegistrationFailure):
                _ = registration.followup(production, "Choose the layout", "A second one")
        waiting = self.run_command("open")
        self.assertEqual(waiting.returncode, 0, waiting.stdout + waiting.stderr)
        self.assertIn("waiting for the asker's reply since", waiting.stdout)
        self.assertIn("0 due, 0 deferred, 1 waiting for a reply", waiting.stdout)
        self.assertNotIn("Keep the layout?", self.run_command("next").stdout)

        with mock.patch.dict(self.env, {"NOTIFY_STATE_DIR": str(self.state / "notify")}):
            with mock.patch.object(review, "review_question") as reviewer:
                held = self.run_command("reply", "--item", "Choose the layout", "--text", "Use /tmp/layout.toml.")
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        self.assertIn("reply held for review: omit-file-path: /tmp/layout.toml", held.stdout)
        reviewer.assert_not_called()
        self.assertIn("waiting for the asker's reply", self.run_command("open").stdout)

        words = "Use the layout settings."
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(words)) as reviewer:
            replied = self.run_command("reply", "--item", "Choose the layout", "--text", words)
        self.assertEqual(reviewer.call_args.kwargs["console_kind"], "reply")
        self.assertEqual(replied.returncode, 0, replied.stdout + replied.stderr)
        self.assertIn("reply: ok", replied.stdout)
        again = self.run_command("reply", "--item", "Choose the layout", "--text", "Again")
        self.assertNotEqual(again.returncode, 0)
        listed = self.run_command("open")
        self.assertIn("1 due, 0 deferred", listed.stdout)
        self.assertIn(f"Follow-up: Which file holds it?\nReply: {words}", listed.stdout)
        self.assertEqual(self.question_rows()[-1].get("followups"),
                         [{"followup": "Which file holds it?", "reply": words}])

    def test_reply_reviews_without_record_locks_and_refuses_a_closed_question(self) -> None:
        self.file_question()
        words = "Use the layout settings."
        question = self.question_record_path("Choose the layout")
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            scope = read_production("example")
            _ = registration.followup(scope, "Choose the layout", "Where is the setting?")
            owner_lock = self.outstanding().with_suffix(".lock")
            number_lock = owner_lock.parent.parent / "review-folders" / scope.owner.id / "question-numbers.lock"

            def read_draft(*_args: object, **_kwargs: object) -> review.QuestionAccepted:
                for path in (owner_lock, number_lock):
                    with path.open("r") as handle:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                registration.withdraw(scope, "Choose the layout")
                return review.QuestionAccepted(words)

            with mock.patch.object(review, "review_question", side_effect=read_draft) as reader:
                with self.assertRaisesRegex(registration.RegistrationFailure, "no longer open"):
                    registration.reply(scope, "Choose the layout", words)
            reader.assert_called_once()
        self.assertEqual(self.question_rows(), [])
        self.assertNotIn(words, question.read_text())

    def test_reply_saves_the_reason_when_the_console_reviewer_times_out(self) -> None:
        self.file_question()
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            _ = registration.followup(read_production("example"), "Choose the layout", "Where is the setting?")
        words = "Use the layout settings."
        with mock.patch.object(review, "review_question",
                               return_value=review.QuestionReviewUnavailable(words, "timeout")):
            replied = self.run_command("reply", "--item", "Choose the layout", "--text", words)

        self.assertEqual(replied.returncode, 0, replied.stdout + replied.stderr)
        row, = self.question_rows()
        self.assertNotIn("review_unavailable", row)
        self.assertEqual(row["followups"], [
            {"followup": "Where is the setting?", "reply": words, "review_unavailable": "timeout"},
        ])
        opened = self.run_command("open")
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        self.assertIn("1 due, 0 deferred", opened.stdout)
        self.assertIn(f"Reply: {words}", opened.stdout)

    def test_reminder_replacement_and_answer_preserve_the_filed_question(self) -> None:
        self.file_question()
        state = self.question_state(["Review the release notes"])
        with mock.patch.object(review, "review_question", side_effect=AssertionError("A reminder needs no review")):
            result = self.run_command("questions", "--state", str(state))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        listed = self.run_command("open")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("Review the release notes", listed.stdout)
        self.assertIn("Keep the layout?", listed.stdout)
        self.assertIn("2 due, 0 deferred", listed.stdout)

        answered = self.run_command("answer", "--item", "Review the release notes")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        self.assertEqual(answered.stdout.splitlines(),
                         ["relay: none; the item belongs to this production",
                          "answer: ok — example: Review the release notes"])
        remaining = self.run_command("next")
        self.assertIn("Keep the layout?", remaining.stdout)
        self.assertNotIn("Review the release notes", remaining.stdout)

    def test_next_keeps_the_shown_question_until_its_answer(self) -> None:
        self.file_question()
        first = self.run_command("next")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("Keep the layout?", first.stdout)
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Ship the release?")):
            filed = self.run_command("ask", "--text", "Choose the release", "--ask", "Ship the release?",
                                     "--unit", "alpha", "--urgency", "user")
        self.assertEqual(filed.returncode, 0, filed.stdout + filed.stderr)
        repeated = self.run_command("next")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(repeated.stdout.splitlines()[0], first.stdout.splitlines()[0])
        self.assertIn("1 more due", repeated.stdout)
        answered = self.run_command("answer", "--item", "Choose the layout")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        following = self.run_command("next")
        self.assertEqual(following.returncode, 0, following.stdout + following.stderr)
        self.assertIn("Ship the release?", following.stdout)
        self.assertNotIn("Keep the layout?", following.stdout)

    def test_question_table_lists_a_filed_question_beside_an_excluded_proposal(self) -> None:
        proposal = self.issues / "A separate proposal.md"
        _ = proposal.write_text("\n".join([
            "---", "status: open", "tags: [issue, proposal]", "proposal_id: P1",
            "proposed_by: example", "date_created: 2026-11-02", "---", "A separate proposal.", "",
        ]), encoding="utf-8")
        proposal_before = proposal.read_bytes()
        self.file_question()
        records = [path for path in self.issues.glob("*.md") if path != proposal]
        self.assertEqual(len(records), 1)
        stored = records[0].read_text(encoding="utf-8")
        self.assertIn(str(self.asker_id), stored)
        self.assertIn("Keep the layout?", stored)
        listed = self.run_command("questions-table")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("|", listed.stdout)
        self.assertIn("Choose the layout", listed.stdout)
        self.assertIn("alpha", listed.stdout)
        self.assertNotIn("A separate proposal", listed.stdout)
        answered = self.run_command("answer", "--item", "Choose the layout")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        closed = self.run_command("questions-table")
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        self.assertNotIn("Choose the layout", closed.stdout)
        self.assertEqual(proposal.read_bytes(), proposal_before)

    def test_question_store_migration_is_idempotent_and_preserves_next(self) -> None:
        _ = self.outstanding().write_text(json.dumps([{"since": "2026-10-06T08:00", "text": "Choose the layout",
                                        "question": "Keep the layout?", "reviewed": "Keep the layout?",
                                        "asked_by": "alpha"}]))
        migrated = self.run_command("migrate-questions")
        self.assertEqual(migrated.returncode, 0, migrated.stdout + migrated.stderr)
        rows = self.question_rows()
        self.assertEqual(rows[0]["asked_by"], self.asker_id)
        remaining: object = cast(object, json.loads(self.outstanding().read_text())) if self.outstanding().exists() else []
        self.assertEqual(remaining, [])
        records = list(self.issues.glob("*.md"))
        self.assertEqual(len(records), 1)
        self.assertIn(str(self.asker_id), records[0].read_text(encoding="utf-8"))
        following = self.run_command("next")
        self.assertEqual(following.returncode, 0, following.stdout + following.stderr)
        self.assertEqual(following.stdout.splitlines(),
                         ["example, since Tue Oct 06 08:00, asked by alpha: Keep the layout?",
                          "next: ok — 0 more due"])
        before = self.store_snapshot()
        repeated = self.run_command("migrate-questions")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.store_snapshot(), before)

    def test_native_next_releases_a_question_in_the_repeated_dst_hour(self) -> None:
        self.file_question()
        state = self.question_state([{"text": "Choose the layout", "after": "2026-11-01T01:45"}])
        deferred = self.run_command("questions", "--state", str(state))
        self.assertEqual(deferred.returncode, 0, deferred.stdout + deferred.stderr)
        zone = ZoneInfo("America/Los_Angeles")
        for fold, next_text, table_state in (
                (0, "no question is open", "deferred until 2026-11-01T01:45"),
                (1, "Keep the layout?", "waiting for your answer")):
            now = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=fold)
            clock = mock.Mock(wraps=datetime, now=mock.Mock(return_value=now))
            with self.subTest(fold=fold), mock.patch.object(registration, "datetime", clock):
                following = self.run_command("next")
                self.assertEqual(following.returncode, 0, following.stdout + following.stderr)
                self.assertIn(next_text, following.stdout)
                table = self.run_command("questions-table")
                self.assertEqual(table.returncode, 0, table.stdout + table.stderr)
                self.assertIn(table_state, table.stdout)

    def test_ask_holds_a_fast_failure_before_the_model_and_publication(self) -> None:
        title = "Keep the layout?"
        context = "Your layout settings are in /tmp/layout.toml."
        notify = self.state / "notify"
        with mock.patch.dict(self.env, {"NOTIFY_STATE_DIR": str(notify)}):
            with mock.patch.object(review, "review_question") as reviewer:
                result = self.run_command("ask", "--ask", title, "--context", context, "--unit", "alpha")
            reviewer.assert_not_called()
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("question held for review: Keep the layout?: omit-file-path: /tmp/layout.toml", result.stdout)
            self.assertEqual(list(self.issues.glob("*.md")), [])
            key = review.QuestionKey(self.fixture_owner_name(), title)
            hold = notify / "questions" / f"{key.digest}.json"
            self.assertEqual(json.loads(hold.read_text()), {
                "slug": key.name, "text": title, "question": title + "\n\n" + context,
                "details": ["omit-file-path: /tmp/layout.toml"],
            })

            revised = "The new layout uses more space on your screen."
            with mock.patch.object(review, "review_question",
                                   return_value=review.QuestionAccepted(title + "\n\n" + revised)) as reviewer:
                filed = self.run_command("ask", "--ask", title, "--context", revised, "--unit", "alpha")
            self.assertEqual(filed.returncode, 0, filed.stdout + filed.stderr)
            reviewer.assert_called_once_with(key, title + "\n\n" + revised, console_kind="question")
            self.assertEqual(len(self.question_rows()), 1)

    def test_question_review_and_defer_keep_the_fixed_asker(self) -> None:
        held = review.QuestionAwaitingFacts("Keep the layout?", ("Which layout?",))
        with mock.patch.object(review, "review_question", return_value=held):
            result = self.run_command("ask", "--text", "Choose the layout", "--ask", "Keep the layout?",
                                      "--unit", "alpha")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Which layout?", result.stdout + result.stderr)
        self.assertEqual(list(self.issues.glob("*.md")), [])
        with mock.patch.object(review, "review_question",
                               return_value=review.QuestionReviewUnavailable("Keep the layout?", "fixture timeout")):
            filed = self.run_command("ask", "--text", "Choose the layout", "--ask", "Keep the layout?",
                                     "--unit", "alpha")
        self.assertEqual(filed.returncode, 0, filed.stdout + filed.stderr)
        state = self.question_state([{"text": "Choose the layout", "after": "2099-01-02T08:00"}])
        deferred = self.run_command("questions", "--state", str(state))
        self.assertEqual(deferred.returncode, 0, deferred.stdout + deferred.stderr)
        listed = self.run_command("open")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("deferred until", listed.stdout)
        self.assertIn("Keep the layout?", listed.stdout)
        self.assertIn("0 due, 1 deferred", listed.stdout)
        rows = self.question_rows()
        self.assertEqual(rows[0]["asked_by"], self.asker_id)
        self.assertEqual(rows[0]["review_unavailable"], "fixture timeout")

    def test_open_and_answer_use_the_document_owner_without_a_timer_after_rename(self) -> None:
        self.file_question()
        owner = staff.snapshot().by_doc(remote.local_machine(), self.doc)
        assert owner is not None
        timer = self.state / f"notifier/showrunner-{self.fixture_staff_id()}"
        shutil.rmtree(timer)
        _ = staff.rename(owner.id, "different-display-name")
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            identities = registration.QuestionStaff.read()
            self.assertEqual(identities.owner(self.doc).name, "different-display-name")
            self.assertEqual(identities.documents()["different-display-name"].doc, owner.doc)
            self.assertEqual(registration.outstanding_file("different-display-name", identities=identities),
                             self.state / "showrunner/outstanding" / f"{owner.id}.json")
        listed = self.run_command("open")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn("Keep the layout?", listed.stdout)
        answered = self.run_command("answer", "--item", "Choose the layout")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        self.assertEqual(self.question_rows(), [])
        self.assertFalse(timer.exists())
        self.assertFalse((self.state / "showrunner/outstanding/example.json").exists())

    def test_reader_preserves_review_timing_and_rank_after_migration(self) -> None:
        legacy = {"since": "2026-10-06T08:00", "text": "Choose the layout",
                                        "question": "Keep the layout?", "review_unavailable": "offline",
                                        "asked_by": str(self.asker_id), "after": "2099-01-02T08:00",
                                        "urgency": "soon", "due": "2099-01-03", "follows": "Earlier choice"}
        _ = self.outstanding().write_text(json.dumps([legacy]))
        self.migrate()
        with self.forbid_legacy_question_access():
            after = registration.read_open_items(self.outstanding())
        self.assertEqual(len(after), 1)
        item = after[0]
        self.assertIsInstance(item, registration.ReviewUnavailableOutstandingQuestion)
        assert isinstance(item, registration.ReviewUnavailableOutstandingQuestion)
        self.assertEqual((item.text, item.question, item.asked_by),
                         ("Choose the layout", "Keep the layout?", self.asker_id))
        self.assertEqual(item.since, datetime(2026, 10, 6, 8))
        self.assertEqual(item.reason, "offline")
        self.assertEqual(item.timing, registration.DeferredUntil(datetime(2099, 1, 2, 8)))
        self.assertEqual(item.rank.follows, "Earlier choice")

        path = self.outstanding()
        for invalid in ("2099-02-29T08:00", "2099-01-02T24:00"):
            with self.subTest(after=invalid):
                with self.assertRaisesRegex(registration.InputError, r"\[0\]\.after: expected a valid YYYY-MM-DDTHH:MM"):
                    _ = registration.parse_outstanding_entries([{**legacy, "after": invalid}], path)


if __name__ == "__main__":
    _ = unittest.main()
