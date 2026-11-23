"""The stranded list: approved hashes not on main, old changed unit trees, and the once-only notice."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from scripts.agents import remote
from scripts.build_queue import admission_records as records
from scripts.build_queue.queue_fixture import fixture_reservation
from scripts.build_queue.queue_snapshot import write_queue_snapshot
from scripts.direct import step_record
from scripts.lib import conf
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lint.memory_test_support import admission_policy_fixture, write_config_fixture
from scripts.production import board_reads, session_staff, showrunners, staff, stranded
from scripts.production.staff_test_support import fixture_member, isolated_staff_file

HOUR = 3600.0
ZONE = "America/Los_Angeles"


def running_step(pid: int, folder: str, unit: str) -> records.RunningStep:
    return records.RunningStep("session", "", "check", folder, records.GIB,
                               records.MeasuredMemory(records.GIB // 10, anonymous=records.AnonymousRunUse(records.GIB // 10),
                                                      anonymous_source=records.NoSource()),
                               pid=pid, unit=unit)


class StrandedTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    home: Path = Path()
    main: str = ""
    approved: str = ""
    now: float = 0.0
    unit: staff.StaffRow | None = None
    owner: staff.StaffRow | None = None

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / "config"
        config.mkdir()
        _ = write_config_fixture(config / "production.conf", "stranded_after_hours=4\nunits_anomaly_repeat_minutes=60\n")
        _ = write_config_fixture(config / "build_admission.conf")
        environment = {**fixture_git_environment(self.root), "HOME": str(self.root), "CONF_DIR": str(config),
                       "XDG_STATE_HOME": str(self.root / "state"), "UNIT_HISTORY_DIR": str(self.root / "history"),
                       "GATE_STATE_DIR": str(self.root / "gate"), "VERIFY_STEPS_DIR": str(self.root / "steps"),
                       "WORKTREES_STATE_DIR": str(self.root / "worktrees"), "BUILDLOG_DIR": str(self.root / "buildlog"),
                       "USER_ZONE": ZONE}
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.repo = self.root / "repo"
        self.git(self.root, "init", "-q", "-b", "main", str(self.repo))
        _ = (self.repo / "tracked.txt").write_text("one\n")
        self.main = commit_repository(self.repo, "main work")
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", self.main)
        self.home = self.root / "natest" / "worktree"
        self.git(self.repo, "worktree", "add", "-q", "-b", "natest", str(self.home), self.main)
        _ = (self.home / "tracked.txt").write_text("two\n")
        self.approved = commit_repository(self.home, "approved work")
        doc = self.root / "production.md"
        _ = doc.write_text(f"# Production\n- **Merge branch:** main\n- **Showrunner checkout:** {self.repo}\n"
                           + "- **Log:** log.md\n")
        self.owner = fixture_member("showrunner", name="show", doc=staff.LiveDoc(remote.local_machine(), doc))
        self.unit = fixture_member("unit", name="natest", parent=self.owner.id,
                                   worktree=staff.PresentWorktree(remote.local_machine(), self.home))
        self.now = time.time() + 30.5 * HOUR
        _ = self.enterContext(patch.object(step_record, "live_records", return_value=[]))
        self.write_queue()

    def write_queue(self, *, running: tuple[tuple[str, str], ...] = (), waiting: tuple[tuple[str, str], ...] = (),
                    age: float = 0.0) -> None:
        """Write the build queue snapshot with these running and waiting folders and units, `age` seconds old."""
        steps = tuple(running_step(100 + index, folder, unit) for index, (folder, unit) in enumerate(running))
        waiters: list[tuple[Path, records.Waiter]] = [
            (Path(f"{200 + index}.waiting"), {"pid": 200 + index, "start_time": "1", "need": records.GIB, "since": 1.0,
                                             "folder": folder, "unit": unit})
            for index, (folder, unit) in enumerate(waiting)]
        reservations = tuple(fixture_reservation(cast(int, step.pid), step.promise, folder=step.folder) for step in steps)
        inputs = records.QueueInputs(waiters, steps, reservations, records.HostMemory(25 * records.GIB, 60 * records.GIB),
                                     records.NoPressureHold(), records.ReadyToStart(), self.now - age)
        directory = self.root / "buildlog" / "admission"
        directory.mkdir(parents=True, exist_ok=True)
        write_queue_snapshot(directory, inputs, admission_policy_fixture())

    def git(self, folder: Path, *arguments: str) -> None:
        _ = subprocess.run(["git", "-C", str(folder), *arguments], check=True, capture_output=True, text=True)

    def head(self) -> str:
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True).stdout.strip()

    def review(self, hours_ago: float, **fields: object) -> dict[str, object]:
        """A completed review pass_finished event of phase 421."""
        return {"event_type": "pass_finished", "pass_kind": "review", "status": "completed", "phase_id": "421",
                "phase_instance_id": "i421", "timestamp_epoch": self.now - hours_ago * HOUR, **fields}

    def write_run(self, *events: dict[str, object], owner: staff.StaffRow | None = None) -> None:
        """One run of the unit, or of `owner`, with these events inside one completed phase 421."""
        member = owner or self.unit
        assert member is not None
        runs = self.root / "history" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        started = {"event_type": "run_started", "owner_staff_id": member.id, "run_started_at": 1.0,
                   "timestamp_epoch": 1.0}
        phase = {"event_type": "phase_started", "phase_id": "421", "phase_instance_id": "i421",
                 "timestamp_epoch": self.now - 40 * HOUR}
        finished = {"event_type": "phase_finished", "phase_id": "421", "phase_instance_id": "i421",
                    "status": "completed", "timestamp_epoch": self.now - 32 * HOUR}
        lines = (started, phase, *events, finished)
        _ = (runs / f"run-{member.name}.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))

    def approved_text(self, line: str, commit: str = "") -> str:
        moment = stranded.time_text(self.now - 33 * HOUR, now=self.now, zone=ZoneInfo(ZONE))
        return f"natest 421 approved {(commit or self.approved)[:10]} {moment}, not on main, {line} (33 h)"

    def set_main(self, commit: str) -> None:
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", commit)

    def test_an_approved_hash_not_on_main_shows_with_its_merge_line_state(self) -> None:
        self.write_run(self.review(33, verdict="APPROVE", reviewed_commit=self.approved, reviewed_tree=str(self.home)))
        found = stranded.rows("show", self.now)
        self.assertEqual([row.text for row in found], [self.approved_text("not in the merge line")])
        self.assertEqual(found[0].kind, stranded.StrandedKind.APPROVED_HASH)
        line = self.root / "gate" / "land_queue.json"
        line.parent.mkdir(parents=True, exist_ok=True)
        _ = line.write_text(json.dumps([{"label": "natest 421", "batch": "", "commits": [self.approved], "added": ""}]))
        self.assertEqual([row.text for row in stranded.rows("show", self.now)], [self.approved_text("in the merge line")])

    def test_a_newer_request_changes_in_the_same_run_hides_an_older_approve(self) -> None:
        approve = self.review(35, verdict="APPROVE", reviewed_commit=self.approved, reviewed_tree=str(self.home))
        self.write_run(approve)
        self.assertEqual(len(stranded.rows("show", self.now)), 1)
        self.write_run(approve, self.review(34, verdict="REQUEST CHANGES", reviewed_commit=self.approved,
                                            reviewed_tree=str(self.home)))
        self.assertEqual(stranded.rows("show", self.now), [])

    def test_the_row_names_the_checkpoint_commit_after_an_approved_review(self) -> None:
        checkpoint = commit_repository(self.home, "checkpoint", allow_empty=True)
        self.write_run(self.review(34, verdict="APPROVE", reviewed_commit=self.approved, reviewed_tree=str(self.home)),
                       {"event_type": "checkpoint", "phase_id": "421", "phase_instance_id": "i421",
                        "checkpoint_commit": checkpoint, "timestamp_epoch": self.now - 33 * HOUR})
        assert self.unit is not None
        found = stranded.rows("show", self.now)
        self.assertEqual([row.key for row in found], [f"approved:{self.unit.id}:421:{checkpoint}"])
        self.assertEqual([row.text for row in found], [self.approved_text("not in the merge line", checkpoint)])
        self.set_main(self.approved)
        self.assertEqual([row.key for row in stranded.rows("show", self.now)], [found[0].key])
        self.set_main(checkpoint)
        self.assertEqual(stranded.rows("show", self.now), [])

    def test_a_checkpoint_followed_by_a_fix_pass_names_the_checkpoint_commit(self) -> None:
        checkpoint = commit_repository(self.home, "checkpoint", allow_empty=True)
        reply = self.root / "review_reply_rerun_1.txt"
        _ = reply.write_text("| Example | `012345abcdef` |\n\nAPPROVE\n")
        self.write_run(self.review(34, reply_file=str(reply)),
                       {"event_type": "checkpoint", "phase_id": "421", "phase_instance_id": "i421",
                        "checkpoint_commit": checkpoint, "timestamp_epoch": self.now - 33 * HOUR},
                       {"event_type": "pass_started", "pass_kind": "fix", "phase_id": "421", "phase_instance_id": "i421",
                        "timestamp_epoch": self.now - 32.5 * HOUR},
                       {"event_type": "pass_finished", "pass_kind": "fix", "status": "completed", "phase_id": "421",
                        "phase_instance_id": "i421", "timestamp_epoch": self.now - 32.2 * HOUR})
        assert self.unit is not None
        found = stranded.rows("show", self.now)
        self.assertEqual([row.key for row in found], [f"approved:{self.unit.id}:421:{checkpoint}"])
        self.assertEqual([row.text for row in found], [self.approved_text("not in the merge line", checkpoint)])
        self.set_main(checkpoint)
        self.assertEqual(stranded.rows("show", self.now), [])

    def test_a_review_after_the_last_checkpoint_names_the_checkpoint_commit(self) -> None:
        checkpoint = commit_repository(self.home, "checkpoint", allow_empty=True)
        self.write_run({"event_type": "checkpoint", "phase_id": "421", "phase_instance_id": "i421",
                        "checkpoint_commit": checkpoint, "timestamp_epoch": self.now - 34 * HOUR},
                       self.review(33, verdict="APPROVE", reviewed_commit=self.approved, reviewed_tree=str(self.home)))
        found = stranded.rows("show", self.now)
        self.assertEqual([row.text for row in found], [self.approved_text("not in the merge line", checkpoint)])
        self.set_main(checkpoint)
        self.assertEqual(stranded.rows("show", self.now), [])

    def test_a_review_lens_with_no_verdict_word_does_not_hide_an_approve(self) -> None:
        self.write_run(self.review(33, verdict="APPROVE", reviewed_commit=self.approved, reviewed_tree=str(self.home)),
                       self.review(32.5, verdict="no verdict", reviewed_commit=self.approved,
                                   reviewed_tree=str(self.home)))
        self.assertEqual([row.text for row in stranded.rows("show", self.now)],
                         [self.approved_text("not in the merge line")])

    def test_a_retip_merge_on_main_clears_the_row(self) -> None:
        self.write_run(self.review(33, verdict="APPROVE", reviewed_commit=self.approved, reviewed_tree=str(self.home)))
        self.assertEqual(len(stranded.rows("show", self.now)), 1)
        self.git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge natest re-tip", "natest")
        self.set_main(self.head())
        self.assertEqual(stranded.rows("show", self.now), [])

    def test_a_merge_of_the_same_unit_and_phase_over_another_commit_clears_the_row(self) -> None:
        self.write_run(self.review(33, verdict="APPROVE", reviewed_commit=self.approved, reviewed_tree=str(self.home)))
        self.assertEqual(len(stranded.rows("show", self.now)), 1)
        self.git(self.repo, "checkout", "-q", "-b", "copy", self.main)
        _ = (self.repo / "copy.txt").write_text("copied work\n")
        copy = commit_repository(self.repo, "copied work")
        self.git(self.repo, "checkout", "-q", "main")
        self.git(self.repo, "merge", "-q", "--no-ff", "-m", f"Merge natest phase 421 ({copy[:10]})", "copy")
        self.set_main(self.head())
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", self.approved,
                                         "refs/remotes/origin/main"], check=False).returncode, 1)
        self.assertEqual(stranded.rows("show", self.now), [])

    def test_an_older_event_reads_the_verdict_and_hash_from_the_reply(self) -> None:
        reply = self.root / "review_reply_1.txt"
        _ = reply.write_text(f"Checked `{self.main[:10]}..{self.approved[:10]}`.\n\n**APPROVE**\n\nDONE x\n")
        self.write_run(self.review(33, reply_file=str(reply)))
        assert self.unit is not None
        found = stranded.rows("show", self.now)
        self.assertEqual([row.key for row in found], [f"approved:{self.unit.id}:421:{self.approved[:10]}"])
        _ = reply.write_text(f"**REQUEST CHANGES** at `{self.approved[:10]}`.\n")
        self.assertEqual(stranded.rows("show", self.now), [])

    def test_an_old_changed_tree_shows_a_row(self) -> None:
        _ = (self.home / "tracked.txt").write_text("three\n")
        found = stranded.rows("show", self.now)
        self.assertEqual([row.text for row in found],
                         ["natest tree worktree: 1 changed file, no commit for 30 h, no live run"])
        self.assertEqual(found[0].key, f"tree:{self.home}")
        self.assertEqual(stranded.rows("show", time.time() + HOUR), [])

    def test_a_live_run_in_the_tree_or_of_the_unit_skips_the_tree(self) -> None:
        _ = (self.home / "tracked.txt").write_text("three\n")
        cases: tuple[tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]], ...] = (
            (((str(self.home / "crates"), ""),), ()), ((), ((str(self.home), ""),)),
            ((("/build/claude/natest/1", "natest"),), ()))
        for running, waiting in cases:
            with self.subTest(running=running, waiting=waiting):
                self.write_queue(running=running, waiting=waiting)
                self.assertEqual(stranded.rows("show", self.now), [])
        self.write_queue(running=(("/build/claude/other/1", "other"),))
        self.assertEqual(len(stranded.rows("show", self.now)), 1)

    def test_units_with_homes_in_a_shared_folder_claim_only_their_own_trees(self) -> None:
        assert self.owner is not None
        shared = self.root / "shared"
        trees: dict[str, Path] = {}
        for name in ("queue-home", "resources-home", "unrelated"):
            trees[name] = shared / name
            self.git(self.repo, "worktree", "add", "-q", "--detach", str(trees[name]), self.main)
        trees["natest-side"] = self.root / "natest" / "side"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(trees["natest-side"]), self.main)
        for unit in ("queue", "resources"):
            _ = fixture_member("unit", name=unit, parent=self.owner.id,
                               worktree=staff.PresentWorktree(remote.local_machine(), trees[f"{unit}-home"]))
        for tree in trees.values():
            _ = (tree / "tracked.txt").write_text("changed\n")
        found = stranded.rows("show", self.now)
        self.assertEqual(sorted((row.unit, row.key) for row in found),
                         [("natest", f"tree:{trees['natest-side']}"), ("queue", f"tree:{trees['queue-home']}"),
                          ("resources", f"tree:{trees['resources-home']}")])

    def test_a_missing_or_old_queue_snapshot_shows_the_tree_row_but_does_not_send_it(self) -> None:
        assert self.owner is not None
        _ = (self.home / "tracked.txt").write_text("three\n")
        snapshot = self.root / "buildlog" / "admission" / "queue.json"
        send = Mock(return_value=True)
        freshest = conf.read("build_admission").number("silence_seconds")
        for case, make in (("missing", lambda: snapshot.unlink()), ("old", lambda: self.write_queue(age=freshest + 1))):
            with self.subTest(case=case):
                make()
                found = stranded.rows("show", self.now)
                self.assertEqual([(row.text, row.check) for row in found],
                                 [("natest tree worktree: 1 changed file, no commit for 30 h, live runs unread",
                                   stranded.RowCheck.LIVE_RUNS_UNREAD)])
                self.assertTrue(stranded.notify(self.owner, found, send=send))
        send.assert_not_called()
        self.assertFalse(stranded.sent_path(self.owner).exists())
        self.write_queue(age=freshest - 1)
        found = stranded.rows("show", self.now)
        self.assertEqual([row.check for row in found], [stranded.RowCheck.COMPLETE])
        self.assertTrue(stranded.notify(self.owner, found, send=send))
        send.assert_called_once()
        self.assertEqual(json.loads(stranded.sent_path(self.owner).read_text()), [f"tree:{self.home}"])

    def test_a_removed_phase_tree_reads_the_unit_home_and_an_unreadable_commit_shows_unsent_while_its_run_is_open(
            self) -> None:
        assert self.owner is not None
        other = self.root / "other-repo"
        self.git(self.root, "init", "-q", "-b", "main", str(other))
        _ = (other / "own.txt").write_text("own\n")
        approved = commit_repository(other, "own approved work")
        member = fixture_member("unit", name="separate", parent=self.owner.id,
                                worktree=staff.PresentWorktree(remote.local_machine(), other))
        self.write_run(self.review(33, verdict="APPROVE", reviewed_commit=approved,
                                   reviewed_tree=str(self.root / "removed-phase-tree")), owner=member)
        _ = staff.set_run(member.id, staff.OpenRun(self.root / "unit-runs" / "run-separate"))
        found = stranded.rows("show", self.now)
        self.assertEqual([row.key for row in found], [f"approved:{member.id}:421:{approved}"])
        _ = (other / ".git").rename(self.root / "moved-git")
        with session_staff.anomalies() as lines:
            found = stranded.rows("show", self.now)
        self.assertEqual(lines, [])
        moment = stranded.time_text(self.now - 33 * HOUR, now=self.now, zone=ZoneInfo(ZONE))
        self.assertEqual([(row.text, row.check) for row in found],
                         [(f"separate 421 approved {approved[:10]} {moment}, commit not in a local repository (33 h)",
                           stranded.RowCheck.COMMIT_UNREAD)])
        send = Mock(return_value=True)
        self.assertTrue(stranded.notify(self.owner, found, send=send))
        send.assert_not_called()
        _ = staff.clear_run(member.id, self.root / "unit-runs" / "run-separate")
        self.assertEqual(stranded.rows("show", self.now), [])
        _ = staff.set_run(member.id, staff.OpenRun(self.root / "unit-runs" / "run-separate"))
        self.git(self.repo, "checkout", "-q", "-b", "copy", self.main)
        _ = (self.repo / "copy.txt").write_text("copied work\n")
        copy = commit_repository(self.repo, "copied work")
        self.git(self.repo, "checkout", "-q", "main")
        self.git(self.repo, "merge", "-q", "--no-ff", "-m", f"Merge separate phase 421 ({copy[:10]})", "copy")
        self.set_main(self.head())
        self.assertEqual(stranded.rows("show", self.now), [])

    def test_the_send_part_reads_the_run_history_and_the_live_runs_once_for_all_productions(self) -> None:
        assert self.owner is not None
        doc = self.root / "second.md"
        _ = doc.write_text(f"# Production\n- **Merge branch:** main\n- **Showrunner checkout:** {self.repo}\n"
                           + "- **Log:** log.md\n")
        second = fixture_member("showrunner", name="second", doc=staff.LiveDoc(remote.local_machine(), doc))
        home = self.root / "other" / "worktree"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(home), self.main)
        _ = fixture_member("unit", name="other", parent=second.id,
                           worktree=staff.PresentWorktree(remote.local_machine(), home))
        for tree in (self.home, home):
            _ = (tree / "tracked.txt").write_text("changed\n")
        notifier = self.root / "notifier"
        for owner in (self.owner, second):
            (notifier / f"showrunner-{owner.id}").mkdir(parents=True)
        with (patch.object(showrunners, "NOTIFIER_STATE_DIR", notifier),
              patch.object(board_reads, "unit_run_history", wraps=board_reads.unit_run_history) as history,
              patch.object(stranded, "live_runs", wraps=stranded.live_runs) as live,
              patch.object(stranded, "send_notice", return_value=True) as send):
            self.assertEqual(stranded.notify_all(self.now), [])
        history.assert_called_once()
        live.assert_called_once()
        self.assertEqual(sorted(cast(staff.StaffRow, call.args[0]).name for call in send.call_args_list), ["second", "show"])

    def test_the_sent_record_sends_each_key_once_and_drops_a_gone_key(self) -> None:
        assert self.owner is not None
        first = stranded.StrandedRow("natest", stranded.StrandedKind.TREE, "tree:a", "natest tree a: 1 changed file", 1)
        second = first._replace(key="tree:b", text="natest tree b: 2 changed files")
        send = Mock(return_value=True)
        self.assertTrue(stranded.notify(self.owner, [first], send=send))
        self.assertTrue(stranded.notify(self.owner, [first, second], send=send))
        self.assertTrue(stranded.notify(self.owner, [second], send=send))
        self.assertEqual([cast(str, call.args[1]).splitlines()[1:] for call in send.call_args_list],
                         [["- natest tree a: 1 changed file"], ["- natest tree b: 2 changed files"]])
        sent = stranded.sent_path(self.owner)
        self.assertEqual(json.loads(sent.read_text()), ["tree:b"])
        self.assertFalse(stranded.notify(self.owner, [first], send=Mock(return_value=False)))
        self.assertEqual(json.loads(sent.read_text()), [])
        with patch("scripts.production.stranded.subprocess.run",
                          return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertTrue(stranded.send_notice(self.owner, "Stranded work."))
        argv = cast(list[str], run.call_args.args[0])
        self.assertEqual(argv[argv.index("--repeat-minutes") + 1], "60")


    def test_a_drop_record_removes_the_row_of_its_commit_only(self) -> None:
        self.write_run(self.review(33, verdict="APPROVE", reviewed_commit=self.approved, reviewed_tree=str(self.home)))
        self.assertEqual(len(stranded.rows("show", self.now)), 1)
        record = stranded.drop("natest", self.approved[:10], by="builds", why="replaced by phase 422", now=self.now)
        assert self.unit is not None
        self.assertEqual(record, {"unit_id": self.unit.id, "commit": self.approved, "by": "builds",
                                  "why": "replaced by phase 422", "at": self.now})
        self.assertEqual(stranded.rows("show", self.now), [])
        _ = (self.home / "tracked.txt").write_text("four\n")
        newer = commit_repository(self.home, "newer approved work")
        self.write_run(self.review(33, verdict="APPROVE", reviewed_commit=newer, reviewed_tree=str(self.home)))
        self.assertEqual([row.text for row in stranded.rows("show", self.now)],
                         [self.approved_text("not in the merge line", newer)])

    def test_the_drop_command_refuses_an_unknown_commit_or_an_empty_reason(self) -> None:
        base = ["drop", "--unit", "natest", "--by", "builds"]
        printed, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(printed), redirect_stderr(errors):
            self.assertEqual(stranded.main([*base, "--commit", "0123456789abcdef", "--why", "replaced"]), 2)
            self.assertEqual(stranded.main([*base, "--commit", "HEAD", "--why", "replaced"]), 2)
            self.assertEqual(stranded.main([*base, "--commit=--git-dir", "--why", "replaced"]), 2)
            self.assertEqual(stranded.main([*base, "--commit", self.approved, "--why", " "]), 2)
            self.assertEqual(stranded.main(["drop", "--unit", "nobody", "--commit", self.approved, "--by", "builds",
                                            "--why", "replaced"]), 2)
            self.assertFalse(stranded.dropped_path().exists())
            self.assertEqual(stranded.main([*base, "--commit", self.approved, "--why", "replaced"]), 0)
        self.assertEqual(errors.getvalue().splitlines(), [
            f"stranded drop refused: natest: no commit 0123456789abcdef in {self.home}",
            "stranded drop refused: --commit HEAD is not a hash of 7 to 40 hex characters",
            "stranded drop refused: --commit --git-dir is not a hash of 7 to 40 hex characters",
            "stranded drop refused: --by and --why must not be empty",
            "stranded drop refused: no unit named nobody"])
        self.assertEqual(printed.getvalue(),
                         f"dropped natest {self.approved[:10]} by builds: replaced ({stranded.dropped_path()})\n")
        self.assertEqual([record["commit"] for record in stranded.read_dropped()], [self.approved])

    def test_a_second_drop_keeps_the_earlier_record(self) -> None:
        _ = stranded.drop("natest", self.approved, by="builds", why="first", now=self.now)
        _ = stranded.drop("natest", self.main, by="sdlc", why="second", now=self.now + 1)
        self.assertEqual([(record["commit"], record["by"], record["why"]) for record in stranded.read_dropped()],
                         [(self.approved, "builds", "first"), (self.main, "sdlc", "second")])


    def test_a_drop_refuses_a_record_file_it_cannot_read_and_keeps_it(self) -> None:
        path = stranded.dropped_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        assert self.unit is not None
        good = {"unit_id": self.unit.id, "commit": self.main, "by": "builds", "why": "first", "at": 1.0}
        for text in (json.dumps([good]) + ",", json.dumps([good, {"unit_id": self.unit.id}]), json.dumps(good)):
            _ = path.write_text(text)
            with self.assertRaises(stranded.DropRefused):
                _ = stranded.drop("natest", self.approved, by="builds", why="second", now=self.now)
            self.assertEqual(path.read_text(), text)
        self.assertEqual(stranded.read_dropped(), [])
        _ = path.write_text(json.dumps([good]))
        _ = stranded.drop("natest", self.approved, by="builds", why="second", now=self.now)
        self.assertEqual([record["why"] for record in stranded.read_dropped()], ["first", "second"])


if __name__ == "__main__":
    _ = unittest.main()
