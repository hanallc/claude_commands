"""Test producer stops and stray removal with private session files."""

from __future__ import annotations

import fcntl
import io
import json
import math
import os
import signal
import subprocess
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest.mock import call, patch

from scripts.agents import pane_record, remote, shutdown, stop_resume, test_stop_resume
from scripts.herdr import herdr, pane_launch
from scripts.herdr.test_herdr import codex_foreground_cases
from scripts.lib import atomic_file, conf, git_exclude, waiting_rows
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.message import sessions
from scripts.production import production_admission, production_doc, record_store, showrunners, staff
from scripts.production.add_showrunner_test_support import write_guard_proof
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id
from scripts.production.test_production_doc import enrolled_document


NATIVE_PROCESS = herdr.native_process


class EnrolledProductionShutdownTests(StaffTestCase):
    repository: Path = Path()
    integration: Path = Path()
    document: Path = Path()
    owner_id: staff.StaffId = staff.StaffId("")
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.repository = self.root / "main"
        self.integration = self.root / "integration"
        self.environment = {**fixture_git_environment(self.root), "GIT_CONFIG_SYSTEM": os.devnull,
                            "XDG_STATE_HOME": str(self.root / "state"),
                            "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner")}
        self.enterContext(patch.dict(os.environ, self.environment, clear=True))
        _ = subprocess.run(["git", "init", "-qb", "main", str(self.repository)], env=self.environment, check=True)
        _ = commit_repository(self.repository, "Fixture", allow_empty=True, env=self.environment)
        _ = self.git("worktree", "add", "-qb", "integration", str(self.integration))
        self.document = self.repository / "docs/plans/fixture-production-live.md"
        enrolled_document(self.document, self.repository, self.integration, "integration", log="docs/plans/fixture-log.md")
        owner = fixture_member(
            "showrunner", name="fixture", doc=staff.LiveDoc(remote.local_machine(), self.document))
        self.owner_id = owner.id

    @property
    def owner(self) -> staff.StaffRow:
        owner = staff.by_id(self.owner_id)
        assert owner is not None
        return owner

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repository), *arguments], env=self.environment, check=True,
                              text=True, capture_output=True).stdout

    def test_retirement_preserves_integration_and_removes_an_ordinary_staging_worktree(self) -> None:
        origin = self.root / "origin.git"
        _ = subprocess.run(["git", "init", "-q", "--bare", str(origin)], env=self.environment, check=True)
        _ = self.git("remote", "add", "origin", str(origin))
        staging = self.root / "staging"
        _ = self.git("worktree", "add", "-qb", "staging", str(staging))
        _ = self.git("push", "-q", "origin", "main", "integration", "staging")
        unit = fixture_member("unit", name="fixture-merges", parent=self.owner.id,
                              worktree=staff.PresentWorktree(remote.local_machine(), staging))
        production = production_doc.read_production(self.owner)
        settings_path = self.root / "production.conf"
        _ = settings_path.write_text("# range: 0.01..\nRETIRE_COMMAND_TIMEOUT_SECONDS=5\n")
        settings = conf.read(settings_path)
        for target, branch, kept in ((self.integration, "integration", True), (staging, "staging", False)):
            with self.subTest(branch=branch):
                result = shutdown.retirement_remove(shutdown.RetiringUnit(production, unit.name, target, branch, unit),
                                                    settings=settings)
                if kept:
                    self.assertIn("protected production", result[0])
                else:
                    self.assertEqual(result, [])
                self.assertEqual(target.exists(), kept)
                self.assertEqual(bool(self.git("branch", "--list", branch)), kept)
                self.assertEqual(bool(self.git("ls-remote", "--heads", "origin", branch)), kept)
        self.assertTrue(self.integration.is_dir())
        self.assertTrue(self.git("branch", "--list", "integration"))
        self.assertTrue(self.git("ls-remote", "--heads", "origin", "integration"))

    def test_live_rename_resumes_an_interrupted_branch_rename_without_changing_main(self) -> None:
        state = self.root / "showrunner"
        excludes = self.repository / ".git/info/exclude"
        with excludes.open("a", encoding="utf-8") as output:
            _ = output.write(f"\n/{self.document.relative_to(self.repository).as_posix()}\n/docs/plans/fixture-log.md\n")
        write_guard_proof(self.root, state, self.repository)
        with (patch.object(showrunners, "SHOWRUNNER_STATE_DIR", state),
              patch.object(production_doc, "GUARD_REPOSITORY", self.repository)):
            production_admission.require_admission(production_doc.read_production(self.owner))
        tracked = production_doc.legacy_document_path(self.document)
        historical = self.repository / "former-proposals"
        _ = tracked.write_text("- **Proposals:** former-proposals\n")
        _ = commit_repository(self.repository, "Record former proposals", str(tracked), env=self.environment)
        index = self.repository / ".git/index"
        head, before = self.git("rev-parse", "HEAD"), index.read_bytes()
        destination = self.document.with_name("renamed-production-live.md")
        request = shutdown.rename_plan(self.owner.id, "renamed", to_doc=str(destination), to_branch="renamed-integration")
        move = request.move
        assert isinstance(move, shutdown.LiveDocBranchMove)
        with self.subTest(stage="interrupted exclusion removal"):
            def interrupt(checkout: Path, source: str) -> None:
                self.assertEqual(checkout / source, self.document)
                locks = (production_doc.live_paths_lock_file(self.integration),
                         waiting_rows.journal_path().with_suffix(".lock"))
                for path in locks:
                    with path.open("r") as lock, self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise OSError("interrupted exclusion removal")

            with (patch.object(git_exclude, "remove_exact_exclusion", side_effect=interrupt),
                  self.assertRaisesRegex(OSError, "interrupted exclusion removal")):
                _ = shutdown.rename_move_doc(request, move)
            self.assertEqual(self.owner.doc, staff.LiveDoc(remote.local_machine(), self.document))
            self.assertTrue(destination.is_file())
            with self.assertRaises(production_doc.ProductionPathsError):
                _ = production_doc.live_file_conflicts(self.repository, "HEAD")
            retry_doc = shutdown.rename_plan(self.owner.id, "renamed", to_doc=str(destination), to_branch=move.branch)
            assert isinstance(retry_doc.move, shutdown.LiveDocBranchMove)
            self.assertTrue(shutdown.rename_move_doc(retry_doc, retry_doc.move))
        _ = self.git("-C", str(self.integration), "branch", "-m", move.branch)
        retry = shutdown.rename_plan(self.owner.id, "renamed", to_doc=str(destination), to_branch=move.branch)
        assert isinstance(retry.move, shutdown.LiveDocBranchMove)
        self.assertTrue(shutdown.rename_move_branch(retry.move))
        row = staff.by_id(self.owner.id)
        assert row is not None and isinstance(row.doc, staff.LiveDoc)
        self.assertEqual(row.doc.path, destination)
        self.assertFalse(self.document.exists())
        git_exclude.require_untracked_ignored(self.repository, destination.relative_to(self.repository).as_posix())
        self.assertEqual(production_doc.read_production(row).merge_branch, move.branch)
        old_pattern = "/" + self.document.relative_to(self.repository).as_posix()
        self.assertNotIn(old_pattern, (self.repository / ".git/info/exclude").read_text().splitlines())
        self.assertIn(historical, record_store.known_folders(record_store.RecordKind.PROPOSAL, row.id,
                                                            document=destination, strict=True))
        self.assertEqual((self.git("branch", "--show-current"), self.git("rev-parse", "HEAD"), index.read_bytes()),
                         ("main\n", head, before))


class ProducerCommandsTests(unittest.TestCase):
    def world(self) -> test_stop_resume.StopResumeTests:
        case = test_stop_resume.StopResumeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def register(self, case: test_stop_resume.StopResumeTests) -> str:
        identity = case.entry["session_id"]
        directory = case.root / "sessions"
        directory.mkdir(exist_ok=True)
        _ = (directory / "123.json").write_text(json.dumps({
            "pid": 123, "sessionId": identity, "name": "alpha",
            "messagingSocketPath": "", "updatedAt": 1, "procStart": "generation-1"}))
        case.live.return_value = sessions.LiveSessionProcess(123)

        def delivered(pid: int, number: int) -> None:
            if (pid, number) == (123, signal.SIGTERM):
                (directory / "123.json").unlink()
                case.live.return_value = sessions.SessionNotLive()
                case.process.return_value = sessions.SessionNotLive()

        case.kill.side_effect = delivered

        def probe(pid: int, number: int) -> None:
            if number:
                case.kill(pid, number)

        _ = case.enterContext(patch.object(os, "kill", side_effect=probe))
        original = case.client.result

        def process_info(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments == ("pane", "process-info", "--pane", "w1:p9"):
                return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 999,
                    "foreground_processes": [{"pid": 999, "name": "codex", "argv": ["codex"]}]}}
            if arguments == ("pane", "process-info", "--pane", "w1:p2") and (directory / "123.json").is_file():
                return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 123,
                    "foreground_processes": [{"pid": 123, "name": "claude", "argv": ["claude"]}]}}
            return original(*arguments, deadline=deadline)

        _ = case.enterContext(patch.object(case.client, "result", side_effect=process_info))
        return identity

    def test_stop_selects_the_producer_process_with_duplicate_or_unreported_panes(self) -> None:
        with self.subTest(selection="registry-references"):
            case = self.world()
            identity = case.entry["session_id"]
            other = fixture_runtime_id("registry-display-name")
            registered = [{"sessionId": other, "name": "registry-worker"}]
            with patch.object(sessions, "running_sessions", return_value=registered):
                self.assertEqual(stop_resume.stop_target("registry-worker"),
                                 stop_resume.SessionNotFound("registry-worker"))
                self.assertEqual(stop_resume.stop_target(other), other)
                registered[0]["name"] = identity
                self.assertEqual(stop_resume.stop_target(identity), identity)
                registered[0]["name"] = case.entry["name"]
                self.assertEqual(stop_resume.stop_target(case.entry["name"]), identity)
            case.kill.assert_not_called()
            case.doCleanups()

        for selection in ("duplicate-label", "registry-only"):
            with self.subTest(selection=selection):
                case = self.world()
                identity = self.register(case)
                if selection == "duplicate-label":
                    _ = fixture_member("producer", name="alpha", runtime_id=identity)
                    case.client.rows.append({"pane_id": "w1:p8", "terminal_id": "other-terminal",
                        "tab_id": "other-tab", "workspace_id": "w1", "label": "alpha",
                        "agent_session": {"kind": "id", "value": fixture_runtime_id("stray")}})
                else:
                    _ = case.client.rows[0].pop("agent_session")
                    pane_record.save("agents", {})
                    rejected = io.StringIO()
                    with redirect_stdout(rejected):
                        status = shutdown.main(["stop", "alpha"])
                    self.assertEqual((status, rejected.getvalue().strip(), case.kill.call_args_list),
                                     (2, "alpha: no session on this machine", []))
                output = io.StringIO()
                with patch.object(herdr, "native_process", NATIVE_PROCESS), redirect_stdout(output):
                    status = shutdown.main(["stop", identity if selection == "registry-only" else "alpha"])
                self.assertEqual(status, 0, output.getvalue())
                self.assertEqual(case.kill.call_args_list, [call(123, signal.SIGTERM)])
                self.assertIn(f"{identity}: stopped pid 123", output.getvalue())
                self.assertEqual(case.client.closed, [])
                case.doCleanups()

        from scripts.agents.test_shutdown_cli import DIRECTOR, RetireUnitTests
        from scripts.copies import copies
        from scripts.lib import conf

        case = RetireUnitTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        unit = shutdown.retirement_find_unit("alpha", "alpha")
        member = replace(unit.staff_member, id=staff.StaffId(fixture_runtime_id("fixed-retiring-holder")))
        self.assertNotEqual(member.id, member.current_session)
        unit = replace(unit, staff_member=member)
        holds = Path(os.environ["BUILD_HOLD_DIR"])
        fixed_hold = (holds / DIRECTOR).rename(holds / member.id)
        with patch.object(shutdown, "retirement_remove", return_value=[]) as remove, \
             patch.object(copies, "remove_trees") as trees, redirect_stdout(io.StringIO()) as output:
            code = shutdown.retirement_clear_worktree(unit, settings=conf.read("production"),
                record=staff.StaffSnapshot((member,)), pane_workers=[], pane=herdr.NoPane("alpha"), require_cleanup=True)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn(["build_hold.py", "release", "--holder", member.id], case.calls)
        self.assertFalse(fixed_hold.exists())
        remove.assert_called_once()
        trees.assert_called_once_with(str(member.id))

        case.doCleanups()
        from scripts.herdr.test_worker_panes import WorkerPaneTests

        case = WorkerPaneTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        _ = case.enterContext(patch.object(pane_record, "RECORD_DIR", pane_launch.record_directory()))
        owner = case.worker("selected-native")
        foreign = replace(owner, id=staff.StaffId("foreign-native"),
                          current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000199"))
        case.world.add(owner, "w1:p7", label="misleading-current")
        case.world.add(foreign, "w1:p8", label=owner.name)
        case.world.locations["w1:p7"]["agent_session"] = {"value": "stale-report"}
        case.world.locations["w1:p8"]["agent_session"] = {"value": owner.current_session}
        case.world.record(owner, "w1:p8")
        entries = pane_record.read("agents")
        _ = entries.pop("w1:p7", None)
        pane_record.save("agents", entries)

        with self.subTest(record="reused pane"):
            self.assertEqual(shutdown.retirement_pane(owner, staff.snapshot()),
                             shutdown.RetiringPane("agents", "w1:p7", "terminal-w1:p7"))


    def test_stray_cleanup_uses_the_proved_owner_and_keeps_a_codex_parents_pane(self) -> None:
        world = self.world()
        client = world.client
        codex = fixture_runtime_id("stray-codex-parent")
        claude = world.entry["session_id"]
        pane = "w1:p2"
        terminal = "w1:p2-terminal"
        receipt = world.root / "panes/.producer-admission-alpha"
        _ = receipt.write_text(json.dumps({"pane": pane, "terminal": terminal}))
        native = {456: herdr.NativeAgentProcess(herdr.PaneSessionReport(codex), "start-codex", {}),
                  457: herdr.NativeAgentProcess(herdr.EnvironmentThread(claude), "start-claude", {})}
        observed = {codex: herdr.LiveConversation("codex", (456,)),
                    claude: herdr.LiveConversation("claude", (457,))}
        def read_native(pid: int, *_args: object) -> herdr.NativeAgentProcess:
            return native[pid]

        read = client.result
        with (codex_foreground_cases(world.root) as cases,
              patch.object(client, "pane_activity", return_value=herdr.PaneRunning(("codex", "claude"))),
              patch.object(herdr, "read_conversation", side_effect=observed.__getitem__),
              patch.object(herdr, "native_process", side_effect=read_native),
              patch.object(stop_resume, "producer_conflicts", return_value=[(claude, "")]),
              patch.object(shutdown, "save_producer_cleanup")):
            for case in cases:
                def result(*args: str, deadline: float = math.inf) -> dict[str, object]:
                    if args == ("pane", "process-info", "--pane", pane):
                        return {"process_info": case.info}
                    return read(*args, deadline=deadline)

                with (self.subTest(case=case.name), patch.object(client, "result", side_effect=result),
                      patch.object(shutdown, "finish_producer_cleanup", return_value=0) as finish):
                    self.assertEqual(shutdown.stray_producer_panes(client, codex),
                                     [shutdown.StrayProducerPane(pane, terminal)] if case.owner_proved else [])
                    self.assertEqual(shutdown.remove_stray_producer("alpha", claude), 0)
                    cleanup = cast(shutdown.PendingProducerCleanup, finish.call_args.args[1])
                    self.assertEqual(cleanup.panes, ())
                    self.assertFalse(cleanup.admission_matches)
        self.assertEqual(client.closed, [])

    def test_remove_stray_preserves_the_real_producer_and_completes_a_rerun(self) -> None:
        for target, failure_step in (("stray", ""), ("current", ""), ("split", ""), ("pane-admission", ""),
                ("other-admission", ""), ("foreign-session-on-pane", ""), ("no-row", ""), ("unrelated-unproved", ""),
                ("unretained-admission", ""),
                ("transcript-failure", "transcript"), ("pane-failure", "pane"),
                ("stop-progress-failure", "stop-progress"), ("transcripts-only", "")):
            with self.subTest(target=target):
                case = self.world()
                identity = self.register(case)
                current = identity if target == "current" else fixture_runtime_id("real-alpha")
                expected = (() if target == "no-row" else
                            (fixture_member("producer", name="alpha", runtime_id=current),))
                if target == "split":
                    case.client.rows.append({"pane_id": "w1:p8", "terminal_id": "other-terminal",
                        "tab_id": "w1:t9", "workspace_id": "w1", "label": "other"})
                elif target == "unrelated-unproved":
                    case.client.rows.append({"pane_id": "w1:p9", "terminal_id": "unknown-terminal",
                        "tab_id": "unknown-tab", "workspace_id": "w1", "label": "unknown"})
                receipt = case.root / "panes/.producer-admission-alpha"
                if target == "unretained-admission" or failure_step:
                    pane_record.save("agents", {})
                if failure_step in ("pane", "stop-progress"):
                    _ = case.client.rows[0].pop("agent_session")
                saved = ({"pane": "w1:p2", "terminal": "w1:p2-terminal"}
                         if target == "unretained-admission" or failure_step else
                         {"pane": "w1:p2"} if target == "pane-admission" else
                         {"session_id": fixture_runtime_id("other-admission"), "pane": "w1:p8"}
                         if target == "other-admission" else
                         {"session_id": fixture_runtime_id("other-admission"), "pane": "w1:p2",
                          "terminal": "w1:p2-terminal"} if target == "foreign-session-on-pane" else
                         {"session_id": identity, "pane": "w1:p2"})
                _ = receipt.write_text(json.dumps(saved))
                transcripts: list[Path] = []
                for account in (".claude3", ".claude4"):
                    folder = case.root / account / "projects/private"
                    folder.mkdir(parents=True)
                    path = folder / f"{identity}.jsonl"
                    _ = path.write_text("retained transcript\n")
                    transcripts.append(path)
                if target == "transcripts-only":
                    (case.root / "sessions/123.json").unlink()
                    case.live.return_value = sessions.SessionNotLive()
                    case.process.return_value = sessions.SessionNotLive()
                    case.client.rows.clear()
                    pane_record.save("agents", {})
                    receipt.unlink()
                output = io.StringIO()
                with patch.object(herdr, "native_process", NATIVE_PROCESS), redirect_stdout(output):
                    if failure_step:
                        unlink = Path.unlink

                        def fail_transcript(path: Path, missing_ok: bool = False) -> None:
                            if path == transcripts[-1]:
                                raise OSError("fixture transcript deletion failure")
                            unlink(path, missing_ok=missing_ok)

                        def fail_stop_progress(path: Path, text: str, *, new_file_mode: int | None = None) -> None:
                            if path.name == f".producer-cleanup-alpha-{identity}" and json.loads(text)["step"] == "admission":
                                raise OSError("fixture stop progress failure")
                            atomic_file.write_text_atomic(path, text, new_file_mode=new_file_mode)

                        with ExitStack() as failure:
                            if failure_step == "transcript":
                                _ = failure.enter_context(patch.object(Path, "unlink", new=fail_transcript))
                            elif failure_step == "stop-progress":
                                _ = failure.enter_context(patch.object(shutdown, "write_text_atomic", new=fail_stop_progress))
                            else:
                                _ = failure.enter_context(patch.object(case.client, "close_retained_pane", return_value=False))
                            failed = shutdown.main(["remove-stray", "alpha", identity])
                        self.assertNotEqual(failed, 0)
                        pending = herdr.json_object(cast(object, json.loads(
                            (receipt.parent / f".producer-cleanup-alpha-{identity}").read_text())), "pending cleanup")
                        self.assertEqual(pending["session"], identity)
                        self.assertEqual(pending["processes"], [{"pid": 123, "started": "generation-1", "kind": "claude"}])
                        self.assertEqual(pending["panes"], [{"pane": "w1:p2", "tab": "w1:t9", "terminal": "w1:p2-terminal"}])
                        self.assertEqual(pending["admission"], saved)
                        self.assertTrue(pending["admission_matches"])
                    status = shutdown.main(["remove-stray", "alpha", identity])
                    if failure_step:
                        self.assertIn(f"{identity}: removed stray", output.getvalue())
                    if target != "current":
                        repeated = shutdown.main(["remove-stray", "alpha", identity])
                        self.assertEqual(repeated, 0, output.getvalue())
                if target == "current":
                    self.assertNotEqual(status, 0)
                    self.assertEqual(case.kill.call_args_list, [])
                    self.assertEqual(case.client.closed, [])
                    self.assertEqual([path.read_text() for path in transcripts], ["retained transcript\n"] * 2)
                    self.assertEqual(json.loads(receipt.read_text())["session_id"], identity)
                elif target == "transcripts-only":
                    self.assertEqual(status, 0, output.getvalue())
                    self.assertEqual(case.kill.call_args_list, [])
                    self.assertEqual(case.client.closed, [])
                    self.assertEqual(sessions.transcript_locations(identity), [])
                    self.assertIn(f"{identity}: removed stray", output.getvalue())
                    self.assertIn(f"{identity}: unchanged", output.getvalue())
                else:
                    self.assertEqual(status, 0, output.getvalue())
                    self.assertEqual(case.kill.call_args_list, [call(123, signal.SIGTERM)])
                    self.assertEqual(case.client.closed, ["w1:p2"])
                    self.assertEqual(sessions.transcript_locations(identity), [])
                    if target in ("other-admission", "foreign-session-on-pane"):
                        self.assertEqual(json.loads(receipt.read_text()), saved)
                    else:
                        self.assertEqual(list(receipt.parent.glob(receipt.name)), [])
                    self.assertIn(f"{identity}: unchanged", output.getvalue())
                    if target == "split":
                        self.assertEqual([pane["pane_id"] for pane in case.client.panes()], ["w1:p8"])
                    elif target == "unrelated-unproved":
                        self.assertEqual([pane["pane_id"] for pane in case.client.panes()], ["w1:p9"])
                self.assertEqual(staff.initial_snapshot().rows, expected)
                case.doCleanups()
