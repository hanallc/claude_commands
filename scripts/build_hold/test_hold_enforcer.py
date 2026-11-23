"""Enforcement with temporary process, cgroup, holder and build log roots."""

from __future__ import annotations

import io
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast, override
from unittest import mock

from scripts.build_queue import execution_identity
from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_hold import build_hold, hold_enforcer, machine_hold, override_guard
from scripts.build_queue import admission_protocol, admission_service, admission_records, placement
from scripts.buildlog import store
from scripts.agents import detached_runs
from scripts.build_targets import target_config
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.mac_test import claim_state
from scripts.herdr import worker_panes
from scripts.lib.atomic_file import write_text_atomic
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.production import staff

from scripts.build_hold.override_guard import require_checkout

require_checkout(Path(__file__))


from scripts.direct import step_record


EMPTY_PROCESS_ENVIRONMENT: Mapping[str, str] = MappingProxyType({})
LOCAL_PLACE = admission_protocol.LocalPlace()


class HoldEnforcerTests(unittest.TestCase):
    root: Path = Path()
    proc: Path = Path()
    holders: Path = Path()
    uid: int = 0
    now: float = 0
    claimed: frozenset[build_hold.ProcessIdentity] = frozenset()
    claim_reader: Callable[[Path], build_hold.LiveClaims | build_hold.ClaimsUnreadable] = staticmethod(hold_enforcer.claimed_processes)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.proc, self.holders = self.root / "proc", self.root / "holders"
        self.proc.mkdir()
        self.holders.mkdir()
        self.uid, self.now = os.getuid(), 0
        self.claimed = frozenset()
        self.claim_reader = hold_enforcer.claimed_processes
        _ = self.enterContext(mock.patch.object(hold_enforcer, "claimed_processes", side_effect=self.read_claims))
        boot = self.proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        _ = boot.write_text("test-boot\n")
        configuration = self.root / "config"
        environment = config_environment(configuration)
        _ = write_config_fixture(configuration / "build_targets.conf", "targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
        self.enterContext(mock.patch.dict(os.environ, {
            **environment,
            "HOME": str(self.root), "BUILD_HOLD_DIR": str(self.holders),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "BUILD_HOLD_ENFORCER_DIR": str(self.root / "enforcer"),
            "VERIFY_STEPS_DIR": str(self.root / "steps"),
            "VERIFY_PROC_ROOT": str(self.proc),
            "BUILD_HOLD_CGROUP_ROOT": str(self.root / "cgroup"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "STAFF_FILE": str(self.root / "staff.json"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "", "CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": "",

            "PLAN_DELEGATE_BOARD_DIR": "", "PLAN_DELEGATE_SESSION_DIR": "",
        }))
        self.write_holder()
        # Registering a stopped session must use this temporary cycle.
        with build_hold.release_lock():
            cycle = build_hold.open_cycle(datetime.fromtimestamp(100, timezone.utc))
            cycle["holders"]["holder"] = {"since": datetime.fromtimestamp(100, timezone.utc).isoformat(), "released_at": ""}
            build_hold.save_cycle(cycle)

    def read_claims(self, proc_root: Path) -> build_hold.LiveClaims:
        self.assertEqual(proc_root, self.proc)
        return build_hold.LiveClaims(self.claimed)

    def test_drain_keeps_a_verified_remote_waiter_without_local_admission(self) -> None:
        _ = self.process(700, 'bash', argv=('/bin/bash', '/scripts/direct/verify.sh'), session='other')
        processes = build_hold.read_audit_processes(self.proc)
        identity = processes[700].identity
        claims = build_hold.LiveClaims(frozenset({identity}), remote_waiters=frozenset({identity}))
        runs = hold_enforcer.classify_drain_runs(processes, frozenset(), claims)
        assert isinstance(runs, tuple)
        self.assertEqual(len(runs), 1)
        self.assertIsInstance(runs[0], hold_enforcer.WorkingRun)
        assert isinstance(runs[0], hold_enforcer.WorkingRun)
        self.assertEqual(runs[0].reason, 'remote waiter')
        unassigned = hold_enforcer.classify_drain_runs(processes, frozenset(), build_hold.LiveClaims(frozenset()))
        assert isinstance(unassigned, tuple)
        self.assertIsInstance(unassigned[0], hold_enforcer.WaitingRun)

    def test_drain_classifies_verify_by_pid_namespace(self) -> None:
        own = self.proc / "self/ns/pid"
        own.parent.mkdir(parents=True)
        own.symlink_to("pid:[4026531836]")
        process = self.process(700, "bash", argv=("/bin/bash", "/fixture/scripts/direct/verify.sh", "test"),
                               session="gate-owner")
        namespace = process / "ns/pid"
        namespace.parent.mkdir()
        cases = (
            ("pid:[4026532000]", build_hold.PidNamespaceRelation.DIFFERENT, False),
            ("pid:[4026531836]", build_hold.PidNamespaceRelation.SAME, True),
            (None, build_hold.PidNamespaceRelation.UNREADABLE, True),
        )
        for target, relation, waiting in cases:
            with self.subTest(relation=relation):
                namespace.unlink(missing_ok=True)
                if target is not None:
                    namespace.symlink_to(target)
                table = build_hold.read_audit_processes(self.proc, build_hold.DRAIN_BUILD_AUDIT)
                self.assertIs(table[700].pid_namespace, relation)
                runs = hold_enforcer.classify_drain_runs(table, frozenset(), build_hold.LiveClaims(frozenset()))
                if waiting:
                    self.assertEqual(runs, (hold_enforcer.WaitingRun(table[700], OwningSession("gate-owner")),))
                else:
                    self.assertEqual(runs, ())

    def test_drain_with_unreadable_claims_skips_a_gate_in_another_pid_namespace(self) -> None:
        for pid in (700, 701):
            _ = self.process(pid, "bash", argv=("bash", "verify.sh", "test"), session="gate-owner")
        table = self.table()
        table[700] = replace(table[700], pid_namespace=build_hold.PidNamespaceRelation.DIFFERENT)

        with redirect_stderr(io.StringIO()):
            kept = hold_enforcer.working_gates(table, build_hold.ClaimsUnreadable("private claims are damaged"),
                                               frozenset())
        self.assertEqual(kept, (table[701].identity,))

    def test_regular_enforcement_protects_a_suite_runner_in_another_pid_namespace(self) -> None:
        own = self.proc / "self/ns/pid"
        own.parent.mkdir(parents=True)
        own.symlink_to("pid:[4026531836]")
        runner = self.process(700, "python3", argv=("python3", "/fixture/scripts/production/gate.py", "--tests-only"),
                              session="other")
        namespace = runner / "ns/pid"
        namespace.parent.mkdir()
        namespace.symlink_to("pid:[4026532000]")

        with mock.patch.object(Path, "readlink", autospec=True, side_effect=Path.readlink) as readlink:
            _ = build_hold.read_audit_processes(self.proc)
            decisions = self.decisions()
        self.assertNotIn(mock.call(own), readlink.call_args_list)
        self.assertNotIn(mock.call(namespace), readlink.call_args_list)
        decision, = decisions
        self.assertIsInstance(decision, hold_enforcer.AllowedBuild)

        table = build_hold.read_audit_processes(self.proc, build_hold.DRAIN_BUILD_AUDIT)
        self.assertIs(table[700].pid_namespace, build_hold.PidNamespaceRelation.DIFFERENT)
        holders = build_hold.natedev_holds(self.holders).in_force
        decision, = hold_enforcer.decide_builds(table, holders, self.uid, 999, frozenset())
        self.assertIsInstance(decision, hold_enforcer.AllowedBuild)

    def hana_claim(self) -> tuple[target_config.SshTarget, claim_state.StatePaths]:
        _ = self.process(700, 'bash', argv=('bash', 'verify.sh', 'check'), session='other')
        _ = self.process(701, 'python3', parent=700, argv=('python3', 'offload.py', 'check'))
        settings = self.root / 'hana.conf'
        _ = settings.write_text('execution_transport=hana-control\nhost=fixture-hana\n')
        target = target_config.SshTarget('fixture-hana', 'linux', settings)
        paths = claim_state.StatePaths(self.root / 'hana-claims')
        paths.directory.mkdir()
        record: claim_state.RemoteJobClaim = {
            'claim_id': execution_identity.assigned_claim_id('fixture-episode', 'fixture-attempt'),
            'episode': 'fixture-episode', 'attempt': 'fixture-attempt',
            'submitting_session': 'other', 'pid': 701, 'proc_start': '11000', 'what': 'Hana check',
            'worktree': str(self.root), 'since': '2026-11-16T12:00:00+00:00', 'remote_host': 'fixture-hana',
            'repo_name': 'hana', 'repo_key': 'fixture-repository', 'slot': 1,
        }
        _ = paths.run.write_text(json.dumps(record))
        return target, paths

    def test_live_hana_claim_stays_claimed_during_drain_without_an_assignment_or_admission_state(self) -> None:
        target, paths = self.hana_claim()
        table = self.table()
        for admission in ('no matching assignment', 'missing state'):
            with self.subTest(admission=admission):
                state = admission_service.ServiceState({}, {}, {}, admission_records.ReadyToStart())
                with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                        mock.patch.object(claim_state, 'state_paths', return_value=paths),
                        mock.patch.object(admission_service, 'read_state', return_value=state,
                            side_effect=FileNotFoundError('private admission state') if admission == 'missing state' else None)):
                    claims = self.claim_reader(self.proc)
                assert isinstance(claims, build_hold.LiveClaims)
                self.assertIn(table[701].identity, claims.identities)
                self.assertEqual(claims.remote_waiters, frozenset())
                runs = hold_enforcer.classify_drain_runs(table, frozenset(), claims)
                assert isinstance(runs, tuple)
                self.assertEqual(len(runs), 1)
                assert isinstance(runs[0], hold_enforcer.WorkingRun)
                self.assertEqual(runs[0].reason, 'Mac or AWS claim')
                with (mock.patch.object(hold_enforcer, 'claimed_processes', return_value=claims),
                        mock.patch.object(hold_enforcer, 'admission_state', return_value=self.run_state())):
                    kept = hold_enforcer.drain_identities(self.proc, build_hold.HolderIdentity('holder', 'holder'))
                self.assertIn(table[700].identity, kept)

    def test_only_an_exact_current_hana_assignment_adds_the_claim_and_coordinator_as_remote_waiters(self) -> None:
        target, paths = self.hana_claim()
        table = self.table()
        claim, = claim_state.read_claims(paths)
        assignment = placement.PlacementAssignment('fixture-episode', 'fixture-attempt',
            placement.PlacementDestination('ssh', target.name, 'fixture-hana', claim.scope, 1),
            execution_identity.SourceKeyKnown('fixture-source'),
            placement.SubmittingOwner(store.host_name(), 700, '11000'), OwningSession('other'), 100)
        key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        state = admission_service.ServiceState({}, {}, {}, admission_records.ReadyToStart())
        state.current_assignments[assignment.episode] = key
        for match in (True, False):
            with self.subTest(match=match):
                current = assignment if match else replace(assignment, session=OwningSession('another-session'))
                state.assignments[key] = admission_records.SavedPlacementAssignment(current)
                with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                        mock.patch.object(claim_state, 'state_paths', return_value=paths),
                        mock.patch.object(admission_service, 'read_state', return_value=state)):
                    claims = self.claim_reader(self.proc)
                assert isinstance(claims, build_hold.LiveClaims)
                self.assertIn(table[701].identity, claims.identities)
                expected: frozenset[build_hold.ProcessIdentity] = (
                    frozenset({table[700].identity, table[701].identity}) if match else frozenset())
                self.assertEqual(claims.remote_waiters, expected)

    def test_unreadable_admission_state_has_no_remote_waiters_and_does_not_pause_enforcement(self) -> None:
        target, paths = self.hana_claim()
        _ = self.process(800, 'cargo', argv=('cargo', 'check'), session='other')
        admission = self.root / 'buildlog/admission/state.json'
        admission.parent.mkdir(parents=True)
        _ = admission.write_text('{')
        sent: list[tuple[int, int]] = []

        def release(_seconds: float) -> None:
            (self.holders / 'holder').unlink()

        errors = io.StringIO()
        with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                mock.patch.object(claim_state, 'state_paths', return_value=paths),
                mock.patch.object(hold_enforcer, 'claimed_processes', side_effect=self.claim_reader),
                redirect_stderr(errors)):
            claims = self.claim_reader(self.proc)
            assert isinstance(claims, build_hold.LiveClaims)
            self.assertEqual(claims.remote_waiters, frozenset())
            self.assertIn(build_hold.ProcessIdentity(701, '11000'), claims.identities)
            result = hold_enforcer.run(self.proc, self.holders, self.root / 'enforcer', uid=self.uid,
                enforcer_pid=999, stopper=hold_enforcer.StopBuildsAndTests(self.proc,
                    signal_process=lambda pid, signum: sent.append((pid, signum))),
                sleep=release, cgroup_root=self.root / 'cgroup')
        self.assertEqual(result, 0)
        self.assertEqual(sent, [(800, signal.SIGTERM)])
        self.assertNotIn('enforcement waits', errors.getvalue())

    def test_hana_match_errors_keep_live_claims_and_do_not_pause_enforcement(self) -> None:
        target, paths = self.hana_claim()
        _ = self.process(800, 'cargo', argv=('cargo', 'check'), session='other')
        claim, = claim_state.read_claims(paths)
        assignment = placement.PlacementAssignment('fixture-episode', 'fixture-attempt',
            placement.PlacementDestination('ssh', target.name, 'fixture-hana', claim.scope, 1),
            execution_identity.SourceKeyKnown('fixture-source'),
            placement.SubmittingOwner(store.host_name(), 700, '11000'), OwningSession('other'), 100)
        key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        state = admission_service.ServiceState({}, {}, {}, admission_records.ReadyToStart())
        state.current_assignments[assignment.episode] = key
        state.assignments[key] = admission_records.SavedPlacementAssignment(assignment)

        def release(_seconds: float) -> None:
            (self.holders / 'holder').unlink()

        for module, operation in ((target_config, 'read_execution_transport'),
                (execution_identity, 'attempt_key'), (execution_identity, 'assigned_claim_id')):
            for error_type in (OSError, ValueError):
                with self.subTest(operation=operation, error=error_type.__name__):
                    self.write_holder()
                    sent: list[tuple[int, int]] = []
                    errors = io.StringIO()
                    with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                            mock.patch.object(claim_state, 'state_paths', return_value=paths),
                            mock.patch.object(admission_service, 'read_state', return_value=state),
                            mock.patch.object(module, operation, side_effect=error_type('private Hana match failure')),
                            mock.patch.object(hold_enforcer, 'claimed_processes', side_effect=self.claim_reader),
                            redirect_stderr(errors)):
                        claims = self.claim_reader(self.proc)
                        assert isinstance(claims, build_hold.LiveClaims)
                        self.assertEqual(claims.remote_waiters, frozenset())
                        self.assertIn(build_hold.ProcessIdentity(701, '11000'), claims.identities)
                        result = hold_enforcer.run(self.proc, self.holders, self.root / 'enforcer', uid=self.uid,
                            enforcer_pid=999, stopper=hold_enforcer.StopBuildsAndTests(self.proc,
                                signal_process=lambda pid, signum: sent.append((pid, signum))),
                            sleep=release, cgroup_root=self.root / 'cgroup')
                    self.assertEqual(result, 0)
                    self.assertEqual(sent, [(800, signal.SIGTERM)])
                    self.assertNotIn('enforcement waits', errors.getvalue())

    def test_local_cargo_under_a_remote_coordinator_stays_an_audit_violation(self) -> None:
        _ = self.hana_claim()
        _ = self.process(800, 'cargo', parent=700, argv=('cargo', 'check'))
        _ = self.process(801, 'rustc', parent=800)
        table = self.table()
        waiters = frozenset({table[700].identity, table[701].identity})
        claims = build_hold.LiveClaims(waiters, remote_waiters=waiters)
        with (mock.patch.object(hold_enforcer, 'claimed_processes', return_value=claims),
                mock.patch.object(hold_enforcer, 'admitted_processes', return_value=frozenset())):
            builds = build_hold.audit_hold(self.holders, self.proc)
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0].process.identity, table[800].identity)
        self.assertEqual(builds[0].classification, 'other_session')
        self.assertEqual(builds[0].children, (table[801].identity,))
        self.assertEqual([(row['pid'], row['class']) for row in self.rows()], [(800, 'other_session')])

    def write_holder(self, keep: tuple[build_hold.ProcessIdentity, ...] = ()) -> None:
        _ = (self.holders / "holder").write_text(json.dumps({
            "holder": "builder", "since": datetime.fromtimestamp(100, timezone.utc).isoformat(),
            "for": "timed test", "release_eta": "unknown",
            "keep": [{"pid": item.pid, "start_time": item.start_time} for item in keep],
        }))

    def process(self, pid: int, command: str, *, parent: int = 1, start: int = 11_000,
                argv: tuple[str, ...] = (), session: str = "", tty: int = 0,
                environment: Mapping[str, str] = EMPTY_PROCESS_ENVIRONMENT, executable: str = "") -> Path:
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ["0"] * 20
        fields[0], fields[1], fields[4], fields[19] = "S", str(parent), str(tty), str(start)
        _ = (directory / "stat").write_text(f"{pid} ({command}) " + " ".join(fields))
        _ = (directory / "comm").write_text(command + "\n")
        _ = (directory / "cmdline").write_bytes(b"\0".join(argument.encode() for argument in (argv or (command,))) + b"\0")
        values = dict(environment)
        if session:
            values["CLAUDE_CODE_SESSION_ID"] = session
        _ = (directory / "environ").write_bytes(b"\0".join(f"{key}={value}".encode() for key, value in values.items()))
        for name, target in (("cwd", str(self.root)), ("exe", executable or f"/tools/{command}")):
            path = directory / name
            path.unlink(missing_ok=True)
            path.symlink_to(target)
        (directory / "fd").mkdir(exist_ok=True)
        _ = (directory / "fd/2").write_text("")
        return directory

    def table(self) -> dict[int, build_hold.AuditProcess]:
        return hold_enforcer.read_processes(self.proc, self.uid, self.root / "cgroup")

    def write_detached_run(self, path: Path, *, command_started: bool, command: list[str] | None = None) -> None:
        record: detached_runs.RunRecord = {
            "run_id": path.stem, "status": "running", "session_dir": str(self.root),
            "worker": "gate-owner", "staff_id": "", "label": "fixture gate",
            "command": command if command is not None else ["bash", "verify.sh", "check"],
            "log": str(path.with_suffix(".log")),
            "started_at": 100.0, "pid": 100, "director_dir": "", "done_message_file": "",
        }
        if command_started:
            record.update(command_group=10, command_started="11000")
        _ = path.write_text(json.dumps(record))

    def decisions(self, enforcer_pid: int = 999) -> tuple[hold_enforcer.BuildDecision, ...]:
        return hold_enforcer.decide_builds(self.table(), build_hold.natedev_holds(self.holders).in_force,
                                          self.uid, enforcer_pid, self.claimed)

    def rows(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines()]

    def write_step(self, call_id: str, state: step_record.StepState, pid: int) -> None:
        step_record.write(step_record.StepRecord(
            call_id, pid, step_record.process_start(pid), self.root, "check", step_record.WorkspacePackages(),
            step_record.AnyTarget(("mac", "natedev"), ("mac",)), state, "2026-10-28T12:00:00+00:00",
        ))

    def stopped_worker_lineages(self) -> tuple[tuple[str, dict[str, str], dict[str, str], tuple[str, ...]], ...]:
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        director = fixture_member("showrunner", name="stop-director")
        run = self.root / "director-run"
        _ = staff.set_run(director.id, staff.OpenRun(run))
        owner = director.current_session
        unit = {"UNIT_SESSION_DIR": str(run)}
        return (
            ("Codex command", {**unit, "CODEX_THREAD_ID": "t1", "CLAUDE_CODE_SESSION_ID": owner},
             {"CLAUDE_CODE_SESSION_ID": "farther"}, (owner, "codex:t1")),
            ("Codex ancestor", unit, {"CODEX_THREAD_ID": "t1", "CLAUDE_CODE_SESSION_ID": "farther"},
             (owner, "codex:t1")),
            ("Claude ancestor", unit, {"CLAUDE_CODE_SESSION_ID": "claude-worker"}, (owner, "claude-worker")),
            ("agent is owner", {**unit, "CLAUDE_CODE_SESSION_ID": owner}, {"CODEX_THREAD_ID": "farther"}, (owner,)),
            ("no agent", unit, {}, (owner,)),
            ("lone Codex", {"CODEX_THREAD_ID": "t1"}, {}, ("codex:t1",)),
        )

    def test_drain_command_keeps_working_runs_and_waits_without_stopping(self) -> None:
        from scripts.build_targets import build_targets
        from scripts.build_hold.owning_session import NoOwningSession

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        member = fixture_member("producer", name="holder")
        _ = (self.holders / "holder").rename(self.holders / member.id)
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"))
        _ = self.process(11, "python3", parent=10, argv=("python3", "targeted_test.py"))
        _ = self.process(20, "python3", argv=("python3", "merge_gate.py"))
        _ = self.process(21, "cargo", parent=20, argv=("cargo", "test"))
        _ = self.process(30, "Runner.Worker")
        _ = self.process(40, "custom-build")
        _ = self.process(60, "cargo", argv=("cargo", "build"), session="existing-agent")
        _ = self.process(61, "rustc", parent=60)
        _ = self.process(80, "bash", argv=("bash", "verify.sh", "check"))
        _ = self.process(81, "cargo", parent=80, argv=("cargo", "clippy"))
        statuses: list[str] = []
        original_wait = hold_enforcer.wait_for_kept_runs

        def status() -> None:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(build_hold.main(["status", "--proc-root", str(self.proc)]), 0)
            statuses.append(output.getvalue())

        def advance(seconds: float) -> None:
            self.assertEqual(seconds, 1)
            self.now += 60
            if self.now == 60:
                _ = self.process(50, "bash", start=12_000, argv=("bash", "verify.sh", "test"))
                lineage = build_targets.read_caller_lineage(self.proc, 50, "12000")
                self.assertIsInstance(build_targets.hold_waited_on(build_hold.natedev_holds(self.holders),
                    NoOwningSession(), self.holders, lineage), build_hold.Holder)
                _ = self.process(70, "cargo", argv=("cargo", "build"), session="new-agent")
                decisions = {item.process.identity.pid: item for item in self.decisions()}
                self.assertIsInstance(decisions[60], hold_enforcer.KeptBuild)
                self.assertIsInstance(decisions[61], hold_enforcer.KeptBuild)
                self.assertIsInstance(decisions[70], hold_enforcer.WouldStopBuild)
                for pid in (20, 21, 30, 60, 61):
                    shutil.rmtree(self.proc / str(pid))
            else:
                for pid in (40, 80, 81):
                    shutil.rmtree(self.proc / str(pid))
            status()

        def wait(holder: build_hold.Holder, proc_root: Path) -> int:
            self.assertEqual(set(holder.keep), {build_hold.ProcessIdentity(pid, "11000") for pid in (20, 30, 40, 60, 80)})
            self.assertIsInstance(holder.release, build_hold.KnownReleaseEta)
            record = cast(dict[str, object], json.loads((self.holders / member.id).read_text()))
            self.assertEqual(set(record), {"holder", "since", "for", "release_eta", "keep", "verb"})
            self.assertEqual(record["verb"], "drain")
            for pid, expected in ((10, build_hold.Holder), (11, build_hold.Holder),
                                  *((pid, build_targets.NoBlockingHold) for pid in (20, 21, 30, 60, 61, 80, 81))):
                lineage = build_targets.read_caller_lineage(self.proc, pid, "11000")
                self.assertIsInstance(build_targets.hold_waited_on(build_hold.natedev_holds(self.holders),
                    NoOwningSession(), self.holders, lineage), expected)
            status()
            return original_wait(holder, proc_root, sleep=advance, clock=lambda: self.now)

        with mock.patch.object(hold_enforcer, "wait_for_kept_runs", side_effect=wait), \
             mock.patch.object(hold_enforcer, "admission_state", return_value=self.run_state()), \
             mock.patch.object(hold_enforcer, "start"), \
             mock.patch.object(hold_enforcer, "stop_waiting_run") as stop, \
             mock.patch.object(os, "kill") as kill, \
             mock.patch.object(build_hold, "start_hold_recorder"), \
             mock.patch.object(build_hold, "print_hold_audit"), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(build_hold.main(["drain", "natedev", "--holder", "holder", "--for", "private drain",
                "--keep", "40", "--release-eta", "23:59", "--zone", "Etc/GMT+12",
                "--proc-root", str(self.proc)]), 0)
        stop.assert_not_called()
        kill.assert_not_called()
        self.assertIn("draining (5 runs left)", statuses[0])
        self.assertIn("draining (2 runs left)", statuses[1])
        self.assertIn(" - held", statuses[2])
        self.assertIn("draining: 5 runs remain", output.getvalue())
        self.assertIn("draining: 2 runs remain", output.getvalue())
        self.assertEqual(output.getvalue().splitlines()[-1], "drained; natedev stays held until release")
        self.assertTrue((self.holders / member.id).is_file())
        self.assertTrue((self.proc / "50/stat").is_file())
        self.assertNotIn("pid 10,", statuses[0])
        self.assertIn(": pid 80, start 11000", statuses[0])
        self.assertIn(": pid 80, start 11000", statuses[1])
        self.assertNotIn("kept by", statuses[2])

    def test_drain_keeps_a_gate_started_before_the_hold_is_published(self) -> None:
        from scripts.build_targets import build_targets
        from scripts.build_hold.owning_session import NoOwningSession

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        member = fixture_member("producer", name="holder")
        (self.holders / "holder").unlink()
        original_start = build_hold.start_hold
        discoveries: list[tuple[build_hold.ProcessIdentity, ...]] = []
        original_discover = hold_enforcer.drain_identities

        def discover(proc_root: Path, identity: build_hold.HolderIdentity) -> tuple[build_hold.ProcessIdentity, ...]:
            kept = original_discover(proc_root, identity)
            discoveries.append(kept)
            return kept

        def publish(directory: Path, identity: build_hold.HolderIdentity, purpose: str,
                    request: build_hold.ReleaseRequest, now: datetime,
                    keep: tuple[build_hold.ProcessIdentity, ...] | list[build_hold.ProcessIdentity], *,
                    before_publish: Callable[[], None] | None = None, verb: machine_hold.HoldVerb = "clear") -> str:
            self.assertEqual(discoveries, [()])
            self.assertEqual(verb, "drain")
            self.assertFalse(build_hold.has_holder_file(directory))
            _ = self.process(55, "bash", argv=("bash", "verify.sh", "check"))
            _ = self.process(57, "cargo", parent=55, argv=("cargo", "build"))
            return original_start(directory, identity, purpose, request, now, keep, before_publish=before_publish,
                                  verb=verb)

        def start_enforcer() -> None:
            self.assertEqual(discoveries, [(), (build_hold.ProcessIdentity(55, "11000"),)])
            holder = build_hold.read_holder(self.holders / member.id)
            self.assertEqual(holder.keep, (build_hold.ProcessIdentity(55, "11000"),))
            _ = self.process(56, "bash", argv=("bash", "verify.sh", "check"))
            for pid, expected in ((55, build_targets.NoBlockingHold), (56, build_hold.Holder)):
                lineage = build_targets.read_caller_lineage(self.proc, pid, "11000")
                self.assertIsInstance(build_targets.hold_waited_on(build_hold.natedev_holds(self.holders),
                    NoOwningSession(), self.holders, lineage), expected)

        # The fake gate starts at tick 11000; fix the boot clock after it so a fresh host keeps it.
        with mock.patch.object(hold_enforcer, "drain_identities", side_effect=discover), \
             mock.patch.object(hold_enforcer, "admission_state", return_value=self.run_state()), \
             mock.patch.object(time, "clock_gettime_ns", return_value=120_000_000_000), \
             mock.patch.object(os, "sysconf", return_value=100), \
             mock.patch.object(build_hold, "start_hold", side_effect=publish), \
             mock.patch.object(hold_enforcer, "start", side_effect=start_enforcer), \
             mock.patch.object(hold_enforcer, "wait_for_kept_runs", return_value=0), \
             mock.patch.object(build_hold, "start_hold_recorder"), redirect_stdout(io.StringIO()):
            self.assertEqual(build_hold.main(["drain", "natedev", "--holder", "holder", "--for", "private drain",
                "--release-eta", "23:59", "--zone", "Etc/GMT+12",
                "--proc-root", str(self.proc)]), 0)

    def local_reservation(self, pid: int, run: admission_protocol.RunIdentity | admission_protocol.NoRun, *,
                          place: admission_protocol.LocalPlace | admission_protocol.CloudPlace = LOCAL_PLACE
                          ) -> admission_service.ReservationRecord:
        return admission_service.ReservationRecord(
            f"episode-{pid}", "", admission_protocol.Owner(pid, "11000"), 1, "fixture", "test", str(self.root), "", "",
            admission_records.NoTestWidth(), admission_records.NoCallRecord(), f"reservation-{pid}",
            {"pid": pid, "start_time": "11000", "need": 1, "since": 100.0}, run=run, place=place)

    def run_state(self, *, slot_owner: int = 0, slot_start: str = "11000",
                  reservations: tuple[admission_service.ReservationRecord, ...] = ()) -> admission_service.ServiceState:
        state = admission_service.ServiceState({record.episode: record for record in reservations}, {}, {},
                                               admission_records.ReadyToStart())
        if slot_owner:
            slot = admission_service.VerifyRunSlot("fixture", admission_protocol.Owner(slot_owner, slot_start))
            state.verify_run_slots[slot.key] = slot
        return state

    def run_with_step(self, step_environment: Mapping[str, str] = EMPTY_PROCESS_ENVIRONMENT) -> None:
        """Write run 100 with a working step: gate 101 and its cargo 102."""
        _ = self.process(100, "python3", argv=("python3", "copies.py", "run"), session="runner")
        _ = self.process(101, "bash", parent=100, argv=("bash", "verify.sh", "test"), environment=step_environment)
        _ = self.process(102, "cargo", parent=101, argv=("cargo", "test"))

    def drain_keep(self, state: admission_service.ServiceState) -> tuple[build_hold.ProcessIdentity, ...]:
        with mock.patch.object(admission_service, "read_state", return_value=state):
            return hold_enforcer.drain_identities(self.proc, build_hold.HolderIdentity("holder", "holder"))

    def assert_kept(self, keep: tuple[build_hold.ProcessIdentity, ...], *pids: int) -> None:
        self.assertCountEqual(keep, tuple(build_hold.ProcessIdentity(pid, "11000") for pid in pids))

    def assert_next_step(self, run: int,
                         expected: type[hold_enforcer.KeptBuild | hold_enforcer.WouldStopBuild]) -> None:
        """Start a step under the run in the hold and check the enforcer and the client hold check."""
        _ = self.process(90, "bash", parent=run, start=12_000, argv=("bash", "verify.sh", "test"))
        _ = self.process(91, "cargo", parent=90, start=12_000, argv=("cargo", "test"))
        self.assert_step_kept(90, 91, "12000", expected)

    def assert_step_kept(self, gate: int, build: int, start: str,
                         expected: type[hold_enforcer.KeptBuild | hold_enforcer.WouldStopBuild]) -> None:
        """Check the enforcer decision for the build and the client hold check for its gate."""
        from scripts.build_targets import build_targets

        decisions = {item.process.identity.pid: item for item in self.decisions()}
        self.assertIsInstance(decisions[build], expected)
        lineage = build_targets.read_caller_lineage(self.proc, gate, start)
        self.assertIsInstance(build_targets.hold_waited_on(build_hold.natedev_holds(self.holders),
            NoOwningSession(), self.holders, lineage),
            build_targets.NoBlockingHold if expected is hold_enforcer.KeptBuild else build_hold.Holder)

    def test_drain_keeps_a_run_between_steps(self) -> None:
        _ = self.process(100, "python3", argv=("python3", "copies.py", "run"), session="runner")
        keep = self.drain_keep(self.run_state(slot_owner=100))
        self.assert_kept(keep, 100)
        self.write_holder(keep)
        self.assert_next_step(100, hold_enforcer.KeptBuild)

    def test_drain_keeps_the_run_and_the_working_step(self) -> None:
        self.run_with_step()
        run = admission_protocol.RunIdentity(100, "11000")
        keep = self.drain_keep(self.run_state(slot_owner=100, reservations=(self.local_reservation(101, run),)))
        self.assert_kept(keep, 100, 101)
        self.write_holder(keep)
        for pid in (101, 102):
            shutil.rmtree(self.proc / str(pid))
        self.assert_next_step(100, hold_enforcer.KeptBuild)

    def test_drain_keeps_a_working_run_with_no_slot(self) -> None:
        # The exempt unit gets no verify run slot; its local reservation names the run.
        self.run_with_step()
        run = admission_protocol.RunIdentity(100, "11000")
        keep = self.drain_keep(self.run_state(reservations=(self.local_reservation(101, run),)))
        self.assert_kept(keep, 100, 101)
        self.write_holder(keep)
        for pid in (101, 102):
            shutil.rmtree(self.proc / str(pid))
        self.assert_next_step(100, hold_enforcer.KeptBuild)

    def test_drain_keeps_the_step_when_its_run_controller_is_gone(self) -> None:
        self.run_with_step()
        keep = self.drain_keep(self.run_state(slot_owner=100))
        self.assert_kept(keep, 100, 101)
        self.write_holder(keep)
        shutil.rmtree(self.proc / "100")
        _ = self.process(101, "bash", argv=("bash", "verify.sh", "test"))
        self.assert_step_kept(101, 102, "11000", hold_enforcer.KeptBuild)

    def test_drain_reads_the_run_from_buildlog_run_when_admission_state_is_unreadable(self) -> None:
        self.run_with_step({"BUILDLOG_RUN": "100:11000"})
        _ = self.process(110, "python3", argv=("python3", "copies.py", "run"), session="other-runner")
        _ = self.process(111, "bash", parent=110, argv=("bash", "verify.sh", "test"),
                         environment={"BUILDLOG_RUN": "110:99999"})
        _ = self.process(112, "cargo", parent=111, argv=("cargo", "test"))
        errors = io.StringIO()
        with mock.patch.object(admission_service, "read_state", side_effect=ValueError("damaged")), \
             redirect_stderr(errors):
            keep = hold_enforcer.drain_identities(self.proc, build_hold.HolderIdentity("holder", "holder"))
        self.assert_kept(keep, 100, 101, 111)
        expected = ("admission state unavailable: damaged; "
                    + "the drain keeps every outermost gate and reads runs from BUILDLOG_RUN")
        self.assertEqual(errors.getvalue().splitlines(), [expected])

    def test_drain_does_not_keep_a_run_that_only_waits(self) -> None:
        _ = self.process(100, "python3", argv=("python3", "copies.py", "run"), session="runner")
        _ = self.process(101, "bash", parent=100, argv=("bash", "verify.sh", "test"))
        state = self.run_state()
        run = admission_protocol.RunIdentity(100, "11000")
        state.run_places[admission_records.run_owner(100, "11000")] = admission_protocol.RunPlace(
            run, 100.0, admission_protocol.Owner(101, "11000"))
        keep = self.drain_keep(state)
        self.assertEqual(keep, ())
        self.write_holder(keep)
        self.assert_next_step(100, hold_enforcer.WouldStopBuild)

    def test_drain_does_not_keep_a_slot_owner_with_another_start_time(self) -> None:
        _ = self.process(100, "python3", argv=("python3", "copies.py", "run"), session="runner")
        self.assertEqual(self.drain_keep(self.run_state(slot_owner=100, slot_start="10999")), ())

    def test_drain_keeps_the_owner_of_a_local_reservation_with_no_run(self) -> None:
        self.run_with_step()
        cases = ((admission_protocol.LocalPlace(), (100, 101)), (admission_protocol.CloudPlace("aws"), (101,)))
        for place, kept in cases:
            with self.subTest(place=type(place).__name__):
                reservation = self.local_reservation(100, admission_protocol.NoRun(), place=place)
                state = self.run_state(reservations=(reservation,))
                self.assert_kept(self.drain_keep(state), *kept)

    def test_drain_keeps_work_with_no_run_by_its_own_identity(self) -> None:
        _ = self.process(60, "cargo", argv=("cargo", "build"), session="agent")
        _ = self.process(61, "rustc", parent=60)
        _ = self.process(70, "bash", argv=("bash", "verify.sh", "check"), session="ci")
        _ = self.process(71, "cargo", parent=70, argv=("cargo", "clippy"))
        no_run = self.local_reservation(70, admission_protocol.NoRun())
        for name, state in (("no admission record", self.run_state()),
                            ("local step with no run", self.run_state(reservations=(no_run,)))):
            with self.subTest(name):
                self.assert_kept(self.drain_keep(state), 60, 70)

    def test_drain_command_refuses_unknown_machines_and_hana_aws(self) -> None:
        for target, refusal in (("aws", machine_hold.NAME_THE_MACHINE), ("all", machine_hold.NAME_THE_MACHINE),
                                ("hana-aws", "drain hana-aws is not supported yet")):
            with self.subTest(target=target), \
                 mock.patch.object(build_hold, "start_hold") as start, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(build_hold.main(["drain", target, "--for", "private drain"]), 2)
                self.assertEqual(output.getvalue(), f"{refusal}\n")
                start.assert_not_called()

    def test_kept_drain_interrupt_retains_the_hold(self) -> None:
        _ = self.process(10, "Runner.Worker")
        self.write_holder((build_hold.ProcessIdentity(10, "11000"),))
        holder = build_hold.read_holder(self.holders / "holder")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(hold_enforcer.wait_for_kept_runs(holder, self.proc,
                sleep=mock.Mock(side_effect=KeyboardInterrupt), clock=lambda: 0), 130)
        self.assertEqual(build_hold.read_holder(self.holders / "holder"), holder)

    def test_clear_stops_existing_gates_and_running_children_but_keeps_explicit_runs(self) -> None:
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        member = fixture_member("producer", name="holder")
        _ = (self.holders / "holder").rename(self.holders / member.id)
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="queued")
        _ = self.process(20, "bash", argv=("bash", "verify.sh", "test"), session="running")
        _ = self.process(21, "cargo", parent=20, argv=("cargo", "test"))
        _ = self.process(30, "bash", argv=("bash", "verify.sh", "test"), session="kept")
        signals: list[tuple[int, int]] = []
        original_stop = hold_enforcer.stop_waiting_run

        def stop(run: hold_enforcer.WaitingRun, holder: build_hold.Holder,
                 processes: Mapping[int, build_hold.AuditProcess], proc_root: Path, *,
                 signal_process: Callable[[int, int], None] = os.kill,
                 ) -> hold_enforcer.StoppedRun | hold_enforcer.RunIdentityChanged:
            _ = signal_process
            return original_stop(run, holder, processes, proc_root,
                signal_process=lambda pid, signum: signals.append((pid, signum)))

        with mock.patch.object(hold_enforcer, "stop_waiting_run", side_effect=stop), \
             mock.patch.object(hold_enforcer, "start") as start, \
             mock.patch.object(build_hold, "start_hold_recorder"), redirect_stdout(io.StringIO()):
            self.assertEqual(build_hold.main(["clear", "natedev", "--holder", "holder", "--for", "private clear",
                "--release-eta", "23:59", "--zone", "Etc/GMT+12",
                "--keep", "30", "--proc-root", str(self.proc)]), 0)
        start.assert_called_once_with()
        self.assertCountEqual(signals, [(10, signal.SIGTERM), (20, signal.SIGTERM), (21, signal.SIGTERM)])
        self.assertEqual(build_hold.read_holder(self.holders / member.id).keep,
                         (build_hold.ProcessIdentity(30, "11000"),))
        self.assertEqual({row["session"] for row in self.rows() if row["action"] == "stopped"}, {"queued", "running"})

    def test_clear_keeps_the_gate_above_an_explicitly_kept_cargo_child(self) -> None:
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        member = fixture_member("producer", name="holder")
        _ = (self.holders / "holder").rename(self.holders / member.id)
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "test"))
        _ = self.process(11, "cargo", parent=10, argv=("cargo", "test"))
        _ = self.process(12, "rustc", parent=11)
        with mock.patch.object(hold_enforcer, "start"), \
             mock.patch.object(build_hold, "start_hold_recorder"), \
             mock.patch.object(hold_enforcer, "stop_waiting_run") as stop, redirect_stdout(io.StringIO()):
            self.assertEqual(build_hold.main(["clear", "natedev", "--holder", "holder", "--for", "private clear",
                "--release-eta", "23:59", "--zone", "Etc/GMT+12",
                "--keep", "11", "--proc-root", str(self.proc)]), 0)
        stop.assert_not_called()
        decisions = hold_enforcer.decide_builds(self.table(), build_hold.natedev_holds(self.holders).in_force,
            self.uid, 999, self.claimed, drain=True)
        self.assertTrue(all(isinstance(item, hold_enforcer.KeptBuild) for item in decisions))

    def test_drain_and_stale_stops_protect_kept_children_and_their_parent(self) -> None:
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "test"))
        _ = self.process(11, "cargo", parent=10, argv=("cargo", "test"))
        _ = self.process(12, "rustc", parent=11)
        table = self.table()
        holders = build_hold.natedev_holds(self.holders).in_force
        decisions = hold_enforcer.decide_builds(table, holders, self.uid, 999, self.claimed, drain=True)
        gate = next(item for item in decisions if item.process.identity.pid == 10)
        assert isinstance(gate, hold_enforcer.WouldStopBuild)
        self.write_holder((table[11].identity,))
        holders = build_hold.natedev_holds(self.holders).in_force
        signals: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(self.proc,
            signal_process=lambda pid, signum: signals.append((pid, signum)), clock=lambda: 5)
        stopper.stop(gate, table, holders, 999, self.claimed)
        stopper.terminating = {item.identity: 0 for item in table.values()}
        stopper.kill_survivors(table, holders, 999, self.claimed)
        self.assertEqual(stopper.terminating, {})
        self.assertEqual(signals, [])
        self.assertEqual(self.rows(), [])

    def test_clear_does_not_stop_gates_when_claims_are_unreadable(self) -> None:
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"))
        with mock.patch.object(hold_enforcer, "claimed_processes",
                 return_value=build_hold.ClaimsUnreadable("private claims are damaged")), \
             mock.patch.object(hold_enforcer, "stop_waiting_run") as stop, redirect_stderr(io.StringIO()) as errors:
            hold_enforcer.clear_queued_runs(self.proc)
        stop.assert_not_called()
        self.assertIn("claims unreadable; clear waits", errors.getvalue())

    def test_a_hold_keeps_a_gate_whose_build_waits_for_or_runs_on_the_mac_or_aws(self) -> None:
        # The fps hold on 2026-11-15 stopped a Mac-only lint that waited for the Mac
        # and a lint whose Mac step had passed and whose claim had ended.
        # A drain does not keep such a gate: a later natedev step must wait for the release.
        state = step_record.StepState
        cases = (
            ("Mac only, waiting for placement", ("mac",), state.WAITING_HOLD, True),
            ("waiting for the Mac", ("mac", "natedev"), state.WAITING_MAC, True),
            ("on the Mac, claim ended", ("mac", "natedev"), state.ON_MAC, True),
            ("waiting for AWS", ("aws", "natedev"), state.WAITING_SSH, True),
            ("on AWS", ("aws", "natedev"), state.ON_SSH, True),
            ("waiting for natedev", ("mac", "natedev"), state.WAITING_HOLD, False),
            ("on natedev", ("mac", "natedev"), state.ON_NATEDEV, False),
        )
        holders = build_hold.natedev_holds(self.holders).in_force
        for name, targets, step_state, kept in cases:
            with self.subTest(name):
                _ = self.process(10, "bash", session="gate",
                                 argv=("bash", "verify.sh", "lint", "hana", "--mac", "lint on the Mac"))
                step_record.write(step_record.StepRecord(
                    "gate-call", 10, step_record.process_start(10), self.root, "lint",
                    step_record.NamedPackage("hana"), step_record.AnyTarget(targets, ("mac",)), step_state,
                    "2026-11-15T17:45:00+00:00"))
                table = self.table()
                with mock.patch.object(target_config, "read_targets", return_value=()):
                    claims = self.claim_reader(self.proc)
                assert isinstance(claims, build_hold.LiveClaims)
                self.assertEqual(table[10].identity in claims.identities, kept)
                self.assertEqual(table[10].identity in claims.routed, kept)
                signals: list[tuple[int, int]] = []
                with redirect_stdout(io.StringIO()):
                    hold_enforcer.stop_queued_runs(table, holders, self.uid, 999, claims.identities, self.proc,
                                                   signal_process=lambda pid, signum: signals.append((pid, signum)))
                self.assertEqual(signals, [] if kept else [(10, signal.SIGTERM)])
                runs = hold_enforcer.classify_drain_runs(table, frozenset(), claims)
                assert not isinstance(runs, build_hold.ClaimsUnreadable)
                run, = runs
                self.assertIsInstance(run, hold_enforcer.WaitingRun)
                with mock.patch.object(build_hold, "read_audit_processes", return_value=table), \
                     mock.patch.object(hold_enforcer, "admission_state", return_value=self.run_state()), \
                     mock.patch.object(target_config, "read_targets", return_value=()):
                    kept_runs = hold_enforcer.drain_identities(
                        self.proc, build_hold.HolderIdentity(holders[0].key, holders[0].name))
                self.assertNotIn(table[10].identity, kept_runs)

    def test_an_unreadable_step_record_makes_the_claims_unreadable(self) -> None:
        # A damaged record can name a Mac-only gate; a hold must not stop that gate.
        _ = self.process(10, "bash", session="gate", argv=("bash", "verify.sh", "lint", "hana", "--mac", "lint"))
        directory = step_record.record_directory()
        directory.mkdir(parents=True, exist_ok=True)
        _ = (directory / "gate-call.json").write_text("{damaged")
        with mock.patch.object(target_config, "read_targets", return_value=()):
            claims = self.claim_reader(self.proc)
        assert isinstance(claims, build_hold.ClaimsUnreadable)
        self.assertIn("gate-call.json", claims.reason)

    def test_drain_keeps_first_scan_entries_and_bounds_second_scan_additions(self) -> None:
        from scripts.build_targets import build_targets
        from scripts.build_hold.owning_session import NoOwningSession

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = fixture_member("producer", name="holder")
        (self.holders / "holder").unlink()
        _ = self.process(40, "Runner.Worker", start=12_000)
        _ = self.process(41, "cargo", start=12_000, argv=("cargo", "build"))
        _ = self.process(42, "rustc", start=12_000, parent=41)
        _ = self.process(45, "custom-build", start=12_001)
        original_discover = hold_enforcer.drain_identities
        calls = 0

        def discover(proc_root: Path, identity: build_hold.HolderIdentity) -> tuple[build_hold.ProcessIdentity, ...]:
            nonlocal calls
            calls += 1
            if calls == 2:
                _ = self.process(50, "Runner.Worker", start=11_999)
                _ = self.process(55, "Runner.Worker", start=12_000)
                _ = self.process(60, "Runner.Worker", start=12_001)
                _ = self.process(70, "bash", start=12_001, argv=("bash", "verify.sh", "test"))
                _ = self.process(80, "cargo", start=12_001, argv=("cargo", "build"))
            return original_discover(proc_root, identity)

        def wait(holder: build_hold.Holder, proc_root: Path) -> int:
            self.assertCountEqual(holder.keep, (build_hold.ProcessIdentity(40, "12000"),
                build_hold.ProcessIdentity(41, "12000"), build_hold.ProcessIdentity(45, "12001"),
                build_hold.ProcessIdentity(50, "11999")))
            for pid, start in ((40, "12000"), (41, "12000"), (42, "12000"), (45, "12001")):
                lineage = build_targets.read_caller_lineage(proc_root, pid, start)
                self.assertIsInstance(build_targets.hold_waited_on(build_hold.natedev_holds(self.holders),
                    NoOwningSession(), self.holders, lineage), build_targets.NoBlockingHold)
            for pid, start in ((55, "12000"), (60, "12001"), (70, "12001"), (80, "12001")):
                lineage = build_targets.read_caller_lineage(proc_root, pid, start)
                self.assertIsInstance(build_targets.hold_waited_on(build_hold.natedev_holds(self.holders),
                    NoOwningSession(), self.holders, lineage), build_hold.Holder)
            return 0

        with mock.patch.object(hold_enforcer, "drain_identities", side_effect=discover), \
             mock.patch.object(time, "clock_gettime_ns", return_value=120_000_000_000) as clock, \
             mock.patch.object(os, "sysconf", return_value=100), \
             mock.patch.object(hold_enforcer, "start"), \
             mock.patch.object(hold_enforcer, "wait_for_kept_runs", side_effect=wait), \
             mock.patch.object(build_hold, "start_hold_recorder"), redirect_stdout(io.StringIO()):
            self.assertEqual(build_hold.main(["drain", "natedev", "--holder", "holder", "--for", "private drain",
                "--release-eta", "23:59", "--zone", "Etc/GMT+12",
                "--keep", "45", "--proc-root", str(self.proc)]), 0)
        clock.assert_called_once_with(time.CLOCK_BOOTTIME)

    def test_release_during_second_drain_scan_does_not_restore_the_hold(self) -> None:
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        member = fixture_member("producer", name="holder")
        (self.holders / "holder").unlink()
        original_discover = hold_enforcer.drain_identities
        calls = 0

        def discover(proc_root: Path, identity: build_hold.HolderIdentity) -> tuple[build_hold.ProcessIdentity, ...]:
            nonlocal calls
            calls += 1
            if calls == 2:
                with build_hold.release_lock():
                    (self.holders / member.id).unlink()
            return original_discover(proc_root, identity)

        with mock.patch.object(hold_enforcer, "drain_identities", side_effect=discover), \
             mock.patch.object(hold_enforcer, "start") as start, \
             mock.patch.object(hold_enforcer, "wait_for_kept_runs") as wait, \
             mock.patch.object(build_hold, "start_hold_recorder"), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(build_hold.main(["drain", "natedev", "--holder", "holder", "--for", "private drain",
                "--release-eta", "23:59", "--zone", "Etc/GMT+12",
                "--proc-root", str(self.proc)]), 0)
        self.assertFalse((self.holders / member.id).exists())
        self.assertIn("the hold was released", output.getvalue())
        start.assert_not_called()
        wait.assert_not_called()

    def test_parser_rejects_removed_hold_command_and_drain_option(self) -> None:
        for arguments in (["hold", "--for", "test"], ["clear", "natedev", "--for", "test", "--drain"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as exit_code:
                    _ = build_hold.main(arguments)
                self.assertEqual(exit_code.exception.code, 2)
                self.assertIn("usage:", errors.getvalue())

    def test_drain_classifies_gate_entry_points_and_work_evidence(self) -> None:
        entries = (("bash", "verify.sh", "check"), ("python3", "merge_gate.py", "--tests-only"),
                   ("python3", "targeted_test.py"))
        for argv in entries:
            with self.subTest(entry=argv):
                _ = self.process(10, argv[0], argv=argv, session="gate-owner")
                table = self.table()
                identity = table[10].identity
                errors = io.StringIO()
                with mock.patch.object(hold_enforcer, "claimed_processes",
                                       return_value=build_hold.ClaimsUnreadable("private claims are damaged")), \
                     redirect_stderr(errors):
                    self.assertEqual(hold_enforcer.working_gates(table, hold_enforcer.claimed_processes(self.proc),
                                                                 frozenset()), (identity,))
                self.assertEqual(errors.getvalue().splitlines(), ["claims unreadable: private claims are damaged"])
                for admitted, claimed, expected in (
                    (frozenset[build_hold.ProcessIdentity](), frozenset[build_hold.ProcessIdentity](),
                     hold_enforcer.WaitingRun),
                    (frozenset({identity}), frozenset[build_hold.ProcessIdentity](), hold_enforcer.WorkingRun),
                    (frozenset[build_hold.ProcessIdentity](), frozenset({identity}), hold_enforcer.WorkingRun),
                ):
                    with self.subTest(admitted=admitted, claimed=claimed):
                        classified = hold_enforcer.classify_drain_runs(table, admitted, build_hold.LiveClaims(claimed))
                        assert not isinstance(classified, build_hold.ClaimsUnreadable)
                        run, = classified
                        self.assertIsInstance(run, expected)
                        self.assertEqual(run.owner, OwningSession("gate-owner"))
                _ = self.process(20, argv[0], parent=10, start=11_001, argv=argv)
                _ = self.process(21, "runner", parent=10, start=11_002)
                _ = self.process(30, "cargo", parent=21, start=11_003, argv=("cargo", "test"))
                try:
                    runs = hold_enforcer.classify_drain_runs(self.table(), frozenset(), build_hold.LiveClaims(frozenset()))
                    assert not isinstance(runs, build_hold.ClaimsUnreadable)
                    self.assertEqual([run.process.identity.pid for run in runs], [10])
                    self.assertTrue(all(isinstance(run, hold_enforcer.WorkingRun) for run in runs))
                finally:
                    for pid in (20, 21, 30):
                        shutil.rmtree(self.proc / str(pid))

    def test_drain_records_stop_and_owner_before_signalling_and_cancels_a_changed_identity(self) -> None:
        record = self.root / "detached-run.json"
        _ = self.process(100, "python3", start=10_000,
                         argv=("python3", "detached_runs.py", "supervise", str(record)))
        holder, = build_hold.natedev_holds(self.holders).in_force
        record_build = hold_enforcer.record_build
        for outcome in ("shared holders", "shared releases", "shared run record",
                        "record failure", "identity change", "no command group", "stopped"):
            with self.subTest(outcome=outcome):
                record = (self.root / ".local/state/build-hold-enforcer/run.json"
                          if outcome == "shared run record" else self.root / "detached-run.json")
                record.parent.mkdir(parents=True, exist_ok=True)
                _ = self.process(100, "python3", start=10_000,
                                 argv=("python3", "detached_runs.py", "supervise", str(record)))
                # The command leads its own group; a record without one has not started its command.
                self.write_detached_run(record, command_started=outcome != "no command group")
                _ = self.process(10, "bash", parent=100, argv=("bash", "verify.sh", "check"), session="gate-owner")
                table = self.table()
                classified = hold_enforcer.classify_drain_runs(table, frozenset(), build_hold.LiveClaims(frozenset()))
                assert not isinstance(classified, build_hold.ClaimsUnreadable)
                run, = classified
                assert isinstance(run, hold_enforcer.WaitingRun)
                events: list[str] = []
                signals: list[tuple[int, int]] = []

                def save_stop(build: build_hold.AuditedBuild, holder: build_hold.Holder, proc_root: Path,
                              action: Literal["audit", "stopped"], *,
                              stopped_run: build_hold.ProcessIdentity | hold_enforcer.NoStoppedRun,
                              restart: build_hold.ReleaseEntry | hold_enforcer.NoRunNotice,
                              request_id: str, runner_record: str) -> None:
                    events.append("record")
                    if outcome == "record failure":
                        raise OSError("private stop record is full")
                    record_build(build, holder, proc_root, action, stopped_run=stopped_run, restart=restart,
                                 request_id=request_id, runner_record=runner_record)

                def stage_reason(path: Path, text: str, *, new_file_mode: int | None = None) -> None:
                    write_text_atomic(path, text, new_file_mode=new_file_mode)
                    if "stop_reason" in text and outcome == "identity change":
                        _ = self.process(10, "editor", start=12_000)

                def signal_group(pid: int, signum: int) -> None:
                    signals.append((pid, signum))
                    self.assertEqual(events, ["record"])
                    self.assertEqual([row["stopped_run"] for row in self.rows() if row["action"] == "stopped"],
                                     [{"pid": 10, "proc_start": "11000"}])
                    cycle = build_hold.read_cycle()
                    assert not isinstance(cycle, build_hold.NoCycle)
                    self.assertIn("gate-owner", cycle["recipients"])

                with mock.patch.object(hold_enforcer, "record_build", side_effect=save_stop), \
                     mock.patch.object(detached_runs, "write_text_atomic", side_effect=stage_reason):
                    if outcome.startswith("shared"):
                        before = record.read_bytes()
                        rows = self.rows()
                        environment = ({"BUILD_HOLD_DIR": str(self.root / ".local/state/build-hold")}
                                       if outcome == "shared holders" else
                                       {"BUILD_HOLD_RELEASE_DIR": str(self.root / ".local/state/build-hold-release")}
                                       if outcome == "shared releases" else {})
                        with mock.patch.object(override_guard, "user_home", return_value=self.root), \
                             mock.patch.dict(os.environ, environment), \
                             self.assertRaises(override_guard.SharedStateWriteRefused):
                            _ = hold_enforcer.stop_waiting_run(
                                run, holder, table, self.proc, signal_group=signal_group,
                                signal_process=lambda pid, signum: signals.append((pid, signum)),
                                supervisor_args=lambda pid, proc_root: ("python3", "detached_runs.py", "supervise", str(record)),
                            )
                        self.assertEqual(events, [])
                        self.assertEqual(signals, [])
                        self.assertEqual(record.read_bytes(), before)
                        self.assertEqual(self.rows(), rows)
                        continue
                    elif outcome == "record failure":
                        with self.assertRaisesRegex(OSError, "private stop record is full"):
                            _ = hold_enforcer.stop_waiting_run(
                                run, holder, table, self.proc, signal_group=signal_group,
                                signal_process=lambda pid, signum: self.fail(f"unexpected signal {pid} {signum}"),
                                supervisor_args=lambda pid, proc_root: ("python3", "detached_runs.py", "supervise", str(record)),
                            )
                    else:
                        result = hold_enforcer.stop_waiting_run(
                            run, holder, table, self.proc, signal_group=signal_group,
                            signal_process=lambda pid, signum: self.fail(f"unexpected signal {pid} {signum}"),
                            supervisor_args=lambda pid, proc_root: ("python3", "detached_runs.py", "supervise", str(record)),
                        )
                        expected = hold_enforcer.StoppedRun if outcome == "stopped" else hold_enforcer.RunIdentityChanged
                        self.assertIsInstance(result, expected)
                if outcome == "stopped":
                    self.assertEqual(signals, [(10, signal.SIGTERM)])
                    saved = cast(dict[str, object], json.loads(record.read_text()))
                    self.assertEqual(saved["stop_reason"], "stopped for timed test; run again after the release")
                else:
                    self.assertEqual(signals, [])
                    self.assertEqual([row for row in self.rows() if row["action"] == "stopped"], [])
                    saved = cast(dict[str, object], json.loads(record.read_text()))
                    self.assertNotIn("stop_reason", saved)

        for index, (label, environment, ancestor_environment, recipients) in enumerate(self.stopped_worker_lineages()):
            with self.subTest(agent=label):
                started = 12_000 + index * 10
                cycle = build_hold.read_cycle()
                assert not isinstance(cycle, build_hold.NoCycle)
                cycle["recipients"], cycle["entries"] = {}, []
                build_hold.save_cycle(cycle)
                _ = self.process(40, "worker", start=10_000, environment=ancestor_environment)
                _ = self.process(10, "bash", parent=40, start=started,
                                 argv=("bash", "verify.sh", "check"), environment=environment)
                table = self.table()
                run = hold_enforcer.WaitingRun(table[10], build_hold.audit_owner(
                    table[10], build_hold.audit_ancestors(table[10], table)))
                self.assertEqual(run.owner, OwningSession("t1" if label == "lone Codex" else recipients[0]))
                worker_signals: list[tuple[int, int]] = []
                register = build_hold.register_wait
                with mock.patch.object(build_hold, "register_wait", wraps=register) as registration:
                    def signal_worker(pid: int, signum: int) -> None:
                        saved = build_hold.read_cycle()
                        assert not isinstance(saved, build_hold.NoCycle)
                        self.assertCountEqual(saved["recipients"], recipients)
                        self.assertEqual([call.args[0] for call in registration.call_args_list], list(recipients))
                        worker_signals.append((pid, signum))

                    result = hold_enforcer.stop_waiting_run(
                        run, holder, table, self.proc, signal_process=signal_worker,
                        signal_group=lambda pid, signum: self.fail(f"unexpected group signal {pid} {signum}"),
                    )
                self.assertIsInstance(result, hold_enforcer.StoppedRun)
                self.assertEqual(worker_signals, [(10, signal.SIGTERM)])
                if label == "lone Codex":
                    self.assertEqual(self.rows()[-1]["session"], "t1")

    def test_hold_stop_records_the_exit_for_a_scoped_detached_run(self) -> None:
        runs = self.root / detached_runs.DETACHED_RUNS_DIR
        runs.mkdir()
        holder, = build_hold.natedev_holds(self.holders).in_force
        gate = ["bash", "verify.sh", "check"]
        scoped_command = ["systemd-run", "--user", "--scope", "--", *gate]
        _ = self.process(10, "bash", parent=100, argv=tuple(gate), session="gate-owner")
        for exit_code in (143, 0):
            with self.subTest(exit_code=exit_code):
                record = runs / f"scoped-run-{exit_code}.json"
                self.write_detached_run(record, command_started=True, command=scoped_command)
                _ = self.process(100, "python3", start=10_000,
                                 argv=("python3", "detached_runs.py", "supervise", str(record)))
                table = self.table()

                def run(command: list[str], _environment: dict[str, str], _log: Path) -> int:
                    self.assertEqual(command, scoped_command)
                    signals: list[tuple[int, int]] = []
                    waiting = hold_enforcer.WaitingRun(table[10], OwningSession("gate-owner"))
                    stop = hold_enforcer.stop_waiting_run(
                        waiting, holder, table, self.proc,
                        signal_group=lambda pid, signum: signals.append((pid, signum)),
                        signal_process=lambda pid, signum: self.fail(f"unexpected process signal {pid} {signum}"),
                    )
                    self.assertIsInstance(stop, hold_enforcer.StoppedRun)
                    self.assertEqual(signals, [(10, signal.SIGTERM)])
                    return exit_code

                with (isolated_staff_file(self.root / "staff.json", {}),
                      mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "0"}),
                      mock.patch.object(detached_runs, "_run", side_effect=run),
                      mock.patch.object(detached_runs, "deliver_in_fresh_process", return_value=0)):
                    self.assertEqual(detached_runs.main(["supervise", str(record)]), 0)

                saved = cast(detached_runs.RunRecord, json.loads(record.read_text()))
                self.assertEqual((saved["status"], saved.get("exit_code")), ("finished", exit_code))
                self.assertEqual(saved.get("stop_reason"), "stopped for timed test; run again after the release")
                result = detached_runs.result_text(saved, detached_runs.command_result(saved))
                self.assertIn(f"Result: STOPPED (exit {exit_code})", result)

    def test_release_names_each_stopped_detached_run_and_routes_it_once(self) -> None:
        holder, = build_hold.natedev_holds(self.holders).in_force
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        director = fixture_member("showrunner", name="director")
        live = fixture_member("worker", name="live", parent=director.id)
        ended = fixture_member("worker", name="ended", parent=director.id)
        closed = fixture_member("worker", name="closed", parent=director.id)
        cases = ((live, "live checks", False, True), (ended, "ended checks", False, True),
                 (closed, "closed checks", True, True), (ended, "second ended check", False, True),
                 (ended, "already finished", False, False))
        signals: list[tuple[int, int]] = []
        for index, (worker, label, clear, succeeds) in enumerate(cases):
            with self.subTest(run=label):
                path = self.root / f"run-{index}.json"
                command = ("python3", "-m", "unittest", "test_example")
                self.write_detached_run(path, command_started=True, command=list(command))
                process_start = 11_000 + index
                record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                record.update(worker=worker.name, staff_id=str(worker.id), label=label, command_started=str(process_start))
                _ = path.write_text(json.dumps(record))
                _ = self.process(100, "python3", start=10_000,
                                 argv=("python3", "detached_runs.py", "supervise", str(path)))
                thread_id = str(worker.current_session)
                _ = self.process(10, "python3", parent=100, start=process_start, argv=command,
                                 environment={"CODEX_THREAD_ID": thread_id})
                table = self.table()
                owner = OwningSession(thread_id)

                def send_signal(pid: int, signum: int, succeed: bool = succeeds) -> None:
                    if not succeed:
                        raise ProcessLookupError(pid)
                    signals.append((pid, signum))

                if clear:
                    result = hold_enforcer.stop_waiting_run(
                        hold_enforcer.WaitingRun(table[10], owner), holder, table, self.proc, signal_group=send_signal)
                    self.assertIsInstance(result, hold_enforcer.StoppedRun)
                else:
                    decision = hold_enforcer.WouldStopBuild(table[10], owner, "timed test", holder, table[10])
                    stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=send_signal)
                    stopper.stop(decision, table, (holder,), 999, self.claimed)
        self.assertEqual(signals, [(10, signal.SIGTERM)] * 4)

        attempts: list[str] = []
        sent: list[tuple[str, str]] = []

        def send(_entry: build_hold.ReleaseEntry, to: str, notice: str) -> build_hold.ReleaseNoticeResult:
            attempts.append(to)
            if to == f"codex:{closed.current_session}":
                return build_hold.ReleaseWorkerClosed()
            sent.append((to, notice))
            return build_hold.ReleaseDelivered()

        with (mock.patch.object(worker_panes, "observe", side_effect=AssertionError("Use the sender.")),
              mock.patch.object(build_hold, "send_release_to", side_effect=send)):
            self.assertEqual(build_hold.release_cycle(self.holders, holder.key), "released, builds may resume.")
            self.assertEqual(build_hold.release_cycle(self.holders, holder.key), "hold is already released.")

        expected = (f"codex:{live.current_session}", f"codex:{ended.current_session}", str(director.id),
                    f"codex:{ended.current_session}")
        self.assertEqual([to for to, _ in sent], list(expected))
        self.assertEqual(attempts, [*expected[:2], f"codex:{closed.current_session}", *expected[2:]])
        for index, ((_, notice), (worker, _, _, _)) in enumerate(zip(sent, cases, strict=False)):
            self.assertIn(f"Restart the stopped run python3 unittest for {worker.name}.", notice)
            self.assertIn(f"Run ID run-{index}.", notice)
        cycle = build_hold.read_cycle()
        assert not isinstance(cycle, build_hold.NoCycle)
        self.assertEqual([entry["delivery"] for entry in build_hold.stopped_run_entries(cycle["entries"]).values()],
                         ["Delivered"] * 4)

    def test_release_does_not_forward_a_send_failure(self) -> None:
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        director = fixture_member("showrunner", name="director")
        worker = fixture_member("worker", name="worker", parent=director.id)
        address = f"codex:{worker.current_session}"
        run: build_hold.StoppedRun = {"id": "checks", "label": "checks", "staff_id": str(worker.id), "director_dir": ""}
        entry = build_hold.new_entry(address, worker.name)
        failure = build_hold.ReleaseSendFailed("delivery was not confirmed")
        with mock.patch.object(build_hold, "send_release_to", return_value=failure) as send:
            result = build_hold.send_stopped_run_release(entry, run, "released", staff.initial_snapshot())
        self.assertEqual(result, failure)
        send.assert_called_once_with(entry, address, "released Restart the stopped run checks for worker. Run ID checks.")

    def test_release_does_not_name_a_run_if_enforcer_exits_before_signaling(self) -> None:
        holder, = build_hold.natedev_holds(self.holders).in_force
        record = self.root / "interrupted.json"
        self.write_detached_run(record, command_started=True)
        _ = self.process(100, "python3", start=10_000,
                         argv=("python3", "detached_runs.py", "supervise", str(record)))
        _ = self.process(10, "bash", parent=100, argv=("bash", "verify.sh", "check"),
                         environment={"CODEX_THREAD_ID": "interrupted-worker"})
        table = self.table()
        run = hold_enforcer.WaitingRun(table[10], OwningSession("interrupted-worker"))

        def exit_before_signal(_pid: int, _signum: int) -> None:
            row, = self.rows()
            self.assertEqual(row["stop_status"], "requested")
            entry = cast(build_hold.ReleaseEntry, row["restart"])
            self.assertEqual(entry.get("run"), {"id": "interrupted", "label": "bash verify.sh check",
                                               "staff_id": "", "director_dir": str(self.root)})
            raise SystemExit(1)

        with self.assertRaises(SystemExit):
            _ = hold_enforcer.stop_waiting_run(run, holder, table, self.proc, signal_group=exit_before_signal)
        self.assertEqual(self.rows()[0]["stop_status"], "requested")
        with (isolated_staff_file(self.root / "staff.json", {}),
              mock.patch.object(build_hold, "send_release_to", return_value=build_hold.ReleaseDelivered()) as send):
            self.assertEqual(build_hold.release_cycle(self.holders, holder.key),
                             "released, builds may resume. Pending stop confirmations: 1.")
        send.assert_called_once()
        self.assertNotIn("Restart the stopped run", cast(str, send.call_args.args[2]))

    def test_release_recovers_a_drain_stop_after_an_audit_save_failure(self) -> None:
        self.assert_release_recovers_stop("drain")

    def test_release_recovers_an_active_detached_stop_after_an_audit_save_failure(self) -> None:
        self.assert_release_recovers_stop("active detached")

    def test_release_recovers_a_direct_stop_after_an_audit_save_failure(self) -> None:
        self.assert_release_recovers_stop("direct")

    def test_resume_recovers_an_unreadable_stop_confirmation(self) -> None:
        self.assert_release_recovers_stop("active detached", fault="unreadable confirmation")

    def test_resume_recovers_a_stop_after_the_runner_confirms_it(self) -> None:
        self.assert_release_recovers_stop("drain", fault="after runner confirmation")

    def test_release_recovers_the_confirmation_after_the_runner_record_is_pruned(self) -> None:
        self.assert_release_recovers_stop("drain", fault="after runner confirmation", prune=True)

    def assert_release_recovers_stop(
        self, path: Literal["drain", "active detached", "direct"], *,
        fault: Literal["audit save", "unreadable confirmation", "after runner confirmation"] = "audit save",
        prune: bool = False,
    ) -> None:
        holder, = build_hold.natedev_holds(self.holders).in_force
        runs = self.root / "runs"
        runs.mkdir()
        record = runs / "recover-stop.json"
        secret = "example-private-argument"
        command = ("bash", "verify.sh", "check", secret) if path == "drain" else ("cargo", "test", "--package", secret)
        detached = path != "direct"
        if detached:
            self.write_detached_run(record, command_started=True, command=list(command))
            saved = cast(detached_runs.RunRecord, json.loads(record.read_text()))
            saved["label"] = " ".join(command)
            _ = record.write_text(json.dumps(saved))
            _ = self.process(100, "python3", start=10_000,
                             argv=("python3", "detached_runs.py", "supervise", str(record)))
        _ = self.process(10, command[0], parent=100 if detached else 1, argv=command,
                         environment={"CODEX_THREAD_ID": "recover-worker"})
        table = self.table()
        owner = OwningSession("recover-worker")
        signals: list[tuple[int, int]] = []
        errors = io.StringIO()

        def signal_fixture(pid: int, signum: int) -> None:
            signals.append((pid, signum))

        def stop() -> None:
            if path == "drain":
                result = hold_enforcer.stop_waiting_run(
                    hold_enforcer.WaitingRun(table[10], owner), holder, table, self.proc, signal_group=signal_fixture)
                self.assertIsInstance(result, hold_enforcer.StoppedRun)
            else:
                decision = hold_enforcer.WouldStopBuild(table[10], owner, "timed test", holder, table[10])
                stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=signal_fixture)
                stopper.stop(decision, table, (holder,), 999, self.claimed)

        if fault == "after runner confirmation":
            with (mock.patch.object(build_hold, "save_stop_confirmation", side_effect=SystemExit(1)),
                  self.assertRaises(SystemExit)):
                stop()
        else:
            with (mock.patch.object(hold_enforcer, "set_stop_status", side_effect=OSError("audit save failed")),
                  redirect_stderr(errors)):
                stop()
            self.assertIn("audit save failed", errors.getvalue())
        self.assertEqual(signals, [(10, signal.SIGTERM)])
        row, = self.rows()
        self.assertEqual(row["stop_status"], "requested")
        request_id = cast(str, row["stop_request_id"])
        confirmation = build_hold.stop_confirmation_path(request_id)
        if fault == "after runner confirmation":
            self.assertFalse(confirmation.exists())
            self.assertTrue(detached_runs.confirms_hold_stop(record, record.stem, request_id))
            self.assertFalse(detached_runs.confirms_hold_stop(record, record.stem, "other-request"))
            if prune:
                saved = cast(detached_runs.RunRecord, json.loads(record.read_text()))
                saved.update(status="finished", exit_code=143, started_at=saved["started_at"] - 1)
                write_text_atomic(record, json.dumps(saved))
                self.write_detached_run(runs / "newer.json", command_started=False)
                with mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())):
                    self.assertEqual(detached_runs.prune_finished(runs), [record.stem])
                self.assertFalse(record.exists())
                self.assertEqual(confirmation.read_text(), "stopped\n")
            else:
                confirmation = record
        else:
            self.assertEqual(confirmation.read_text(), "stopped\n")

        saved_confirmation = confirmation.read_bytes()
        resume = fault != "audit save" and not prune
        if resume:
            _ = confirmation.write_bytes(b"\xff")
        complete = "released, builds may resume."
        pending = complete + " Pending stop confirmations: 1."

        with (isolated_staff_file(self.root / "staff.json", {}),
              mock.patch.object(build_hold, "send_release_to", return_value=build_hold.ReleaseDelivered()) as send):
            released = build_hold.release_cycle(self.holders, holder.key)
            if resume:
                self.assertEqual(released, pending)
                self.assertFalse((self.holders / holder.key).exists())
                send.assert_called_once()
                self.assertNotIn("Restart the stopped run", cast(str, send.call_args.args[2]))
                intent = build_hold.read_release_intent(build_hold.release_directory() / "pending-release.json")
                assert not isinstance(intent, build_hold.NoReleaseIntent)
                notices = intent["notices"]
                assert notices["kind"] == "cycle"
                request, = notices.get("stop_requests", [])
                self.assertEqual(request["id"], request_id)
                if fault == "after runner confirmation":
                    self.assertEqual(request.get("runner_record"), str(record))
                _ = confirmation.write_bytes(saved_confirmation)
                send.reset_mock()
                released = build_hold.release_cycle(self.holders, holder.key, resume=True)
            self.assertEqual(released, complete)
            self.assertEqual(build_hold.release_cycle(self.holders, holder.key), "hold is already released.")
        send.assert_called_once()
        self.assertEqual(cast(str, send.call_args.args[1]), "codex:recover-worker")
        notice = cast(str, send.call_args.args[2])
        label = "bash verify.sh check" if path == "drain" else "cargo test"
        self.assertIn(f"Restart the stopped run {label} for ", notice)
        run_id = record.stem if detached else f"process:{row['id']}"
        self.assertIn(f"Run ID {run_id}.", notice)
        self.assertNotIn(secret, notice)

    def test_release_names_a_direct_run_with_only_a_staff_owner(self) -> None:
        holder, = build_hold.natedev_holds(self.holders).in_force
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        director = fixture_member("showrunner", name="director")
        worker = fixture_member("worker", name="worker", parent=director.id)
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "test"),
                         environment={"BUILDLOG_OWNER_STAFF_ID": str(worker.id)})
        table = self.table()
        owner = NoOwningSession()
        self.assertEqual(build_hold.run_agent(table[10], ()), build_hold.NoRunAgent())
        signals: list[tuple[int, int]] = []

        result = hold_enforcer.stop_waiting_run(
            hold_enforcer.WaitingRun(table[10], owner), holder, table, self.proc,
            signal_process=lambda pid, signum: signals.append((pid, signum)))
        self.assertIsInstance(result, hold_enforcer.StoppedRun)
        self.assertEqual(signals, [(10, signal.SIGTERM)])

        with mock.patch.object(build_hold, "send_release_to", return_value=build_hold.ReleaseDelivered()) as send:
            self.assertEqual(build_hold.release_cycle(self.holders, holder.key), "released, builds may resume.")
        send.assert_called_once()
        self.assertEqual(cast(str, send.call_args.args[1]), str(worker.id))
        self.assertIn("Restart the stopped run bash verify.sh test for worker.", cast(str, send.call_args.args[2]))

    def test_release_omits_arguments_from_a_direct_run_notice(self) -> None:
        holder, = build_hold.natedev_holds(self.holders).in_force
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        director = fixture_member("showrunner", name="director")
        worker = fixture_member("worker", name="worker", parent=director.id)
        secret = "example-private-argument"
        argv = ("cargo", "test", "--package", secret)
        _ = self.process(10, "cargo", argv=argv, environment={"CODEX_THREAD_ID": str(worker.current_session)})
        table = self.table()
        owner = OwningSession(str(worker.current_session))
        signals: list[tuple[int, int]] = []
        decision = hold_enforcer.WouldStopBuild(table[10], owner, "timed test", holder, table[10])
        stopper = hold_enforcer.StopBuildsAndTests(
            self.proc, signal_process=lambda pid, signum: signals.append((pid, signum)))
        stopper.stop(decision, table, (holder,), 999, self.claimed)
        self.assertEqual(signals, [(10, signal.SIGTERM)])

        with mock.patch.object(build_hold, "send_release_to", return_value=build_hold.ReleaseDelivered()) as send:
            self.assertEqual(build_hold.release_cycle(self.holders, holder.key), "released, builds may resume.")
        send.assert_called_once()
        notice = cast(str, send.call_args.args[2])
        self.assertIn("Restart the stopped run cargo test for worker.", notice)
        row, = self.rows()
        self.assertIn(f"Run ID process:{row['id']}.", notice)
        self.assertNotIn(secret, notice)

    def test_release_names_two_runs_without_detached_supervisors(self) -> None:
        holder, = build_hold.natedev_holds(self.holders).in_force
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        director = fixture_member("showrunner", name="director")
        worker = fixture_member("worker", name="worker", parent=director.id)
        thread = str(worker.current_session)
        environment = {"CODEX_THREAD_ID": thread}
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), environment=environment)
        _ = self.process(11, "cargo", argv=("cargo", "test"), environment=environment)
        table = self.table()
        owner = OwningSession(thread)
        signals: list[tuple[int, int]] = []
        def signal_process(pid: int, signum: int) -> None:
            signals.append((pid, signum))

        result = hold_enforcer.stop_waiting_run(
            hold_enforcer.WaitingRun(table[10], owner), holder, table, self.proc, signal_process=signal_process)
        self.assertIsInstance(result, hold_enforcer.StoppedRun)
        decision = hold_enforcer.WouldStopBuild(table[11], owner, "timed test", holder, table[11])
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=signal_process)
        stopper.stop(decision, table, (holder,), 999, self.claimed)
        self.assertEqual(signals, [(10, signal.SIGTERM), (11, signal.SIGTERM)])
        self.assertEqual([row["stopped_run"] for row in self.rows()],
                         [{"pid": 10, "proc_start": "11000"}, {"pid": 11, "proc_start": "11000"}])

        with mock.patch.object(build_hold, "send_release_to", return_value=build_hold.ReleaseDelivered()) as send:
            self.assertEqual(build_hold.release_cycle(self.holders, holder.key), "released, builds may resume.")
        self.assertEqual([cast(str, call.args[1]) for call in send.call_args_list], [f"codex:{thread}"] * 2)
        for call, label in zip(send.call_args_list, ("bash verify.sh check", "cargo test"), strict=True):
            self.assertIn(f"Restart the stopped run {label} ", cast(str, call.args[2]))

    def test_drain_skips_a_reused_pid_before_a_stop(self) -> None:
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="gate-owner")
        table = self.table()
        classified = hold_enforcer.classify_drain_runs(table, frozenset(), build_hold.LiveClaims(frozenset()))
        assert not isinstance(classified, build_hold.ClaimsUnreadable)
        run, = classified
        assert isinstance(run, hold_enforcer.WaitingRun)
        _ = self.process(10, "editor", start=12_000)
        holder, = build_hold.natedev_holds(self.holders).in_force
        signals: list[tuple[int, int]] = []
        result = hold_enforcer.stop_waiting_run(
            run, holder, table, self.proc,
            signal_process=lambda pid, signum: signals.append((pid, signum)),
            signal_group=lambda pid, signum: signals.append((pid, signum)),
        )
        self.assertIsInstance(result, hold_enforcer.RunIdentityChanged)
        self.assertEqual(signals, [])
        self.assertEqual(self.rows(), [])

    def test_drain_checks_detached_supervisor_before_group_stop_and_records_reason(self) -> None:
        record = self.root / "detached-run.json"
        _ = self.process(100, "python3", start=10_000,
                         argv=("python3", "detached_runs.py", "supervise", str(record)))
        _ = self.process(10, "bash", parent=100, argv=("bash", "verify.sh", "check"), session="gate-owner")
        table = self.table()
        classified = hold_enforcer.classify_drain_runs(table, frozenset(), build_hold.LiveClaims(frozenset()))
        assert not isinstance(classified, build_hold.ClaimsUnreadable)
        run, = classified
        assert isinstance(run, hold_enforcer.WaitingRun)
        holder, = build_hold.natedev_holds(self.holders).in_force
        read_supervisor_args = hold_enforcer.supervisor_args
        for argument, current, prefix, changes in ((str(record), False, ("python3",), False),
                                                   (str(record), True, ("python3",), False),
                                                   (record.name, True, ("python3",), False),
                                                   (str(record), False, ("python3", "unrelated.py"), False),
                                                   (str(record), True, ("python3",), True)):
            with self.subTest(record_argument=argument, supervisor_current=current, supervisor_prefix=prefix,
                              group_changes_after_check=changes):
                _ = self.process(100, "python3", start=10_000,
                                 argv=("python3", "detached_runs.py", "supervise", argument))
                table = self.table()
                self.write_detached_run(record, command_started=True)
                signals: list[tuple[int, int]] = []
                inspected: list[int] = []

                def supervisor_args(pid: int, proc_root: Path) -> tuple[str, ...]:
                    inspected.append(pid)
                    self.assertEqual(proc_root, self.proc)
                    argv = ((*prefix, "detached_runs.py", "supervise", argument)
                            if current or len(prefix) == 2 else ("editor", "notes.txt"))
                    _ = (proc_root / str(pid) / "cmdline").write_bytes(
                        b"\0".join(value.encode() for value in argv) + b"\0")
                    return read_supervisor_args(pid, proc_root)

                def signal_group(pid: int, signum: int) -> None:
                    saved = cast(dict[str, object], json.loads(record.read_text()))
                    self.assertNotIn("stop_reason", saved)
                    signals.append((pid, signum))

                with mock.patch.object(detached_runs, "command_group", wraps=detached_runs.command_group) as read_group:
                    if changes:
                        group = detached_runs.CommandGroup(10, "11000")
                        read_group.side_effect = [group, group, detached_runs.CommandGroup(20, "12000")]
                    result = hold_enforcer.stop_waiting_run(
                        run, holder, table, self.proc, supervisor_args=supervisor_args,
                        signal_process=lambda pid, signum: self.fail(f"unexpected process signal {pid} {signum}"),
                        signal_group=signal_group,
                    )
                self.assertIn(100, inspected)
                if current:
                    self.assertIsInstance(result, hold_enforcer.StoppedRun)
                    self.assertEqual(signals, [(10, signal.SIGTERM)])
                    saved = cast(dict[str, object], json.loads(record.read_text()))
                    self.assertEqual(saved["stop_reason"], "stopped for timed test; run again after the release")
                else:
                    self.assertIsInstance(result, hold_enforcer.RunIdentityChanged)
                    self.assertEqual(signals, [])

    def test_only_confirmed_hold_stops_are_consumed(self) -> None:
        holder, = build_hold.natedev_holds(self.holders).in_force
        consumed = self.root / "consumed-stops"
        for pid, (status, expected) in enumerate((("requested", False), ("not_stopped", False),
                                                 ("stopped", True), ("legacy", True)), 10):
            with self.subTest(stop_status=status):
                _ = self.process(pid, "cargo", session="gate-owner",
                                 environment={"BUILDLOG_CALL_ID": "drain-call"})
                process = self.table()[pid]
                build = build_hold.AuditedBuild(process, OwningSession("gate-owner"), "other_session", ())
                if status == "legacy":
                    hold_enforcer.record_build(build, holder, self.proc, "stopped")
                else:
                    hold_enforcer.record_build(build, holder, self.proc, "stopped", stopped_run=process.identity)
                    if status == "not_stopped":
                        hold_enforcer.set_stop_status(holder, process.identity, "not_stopped")
                    elif status == "stopped":
                        hold_enforcer.set_stop_status(holder, process.identity, "stopped")
                self.assertEqual(step_record.consume_stopped_build("drain-call", consumed), expected)

    def test_verify_controller_survives_every_live_state_while_local_compile_child_stops(self) -> None:
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="other")
        decision, = self.decisions()
        self.assertIsInstance(decision, hold_enforcer.AllowedBuild)
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        _ = self.process(20, "cargo", parent=10, argv=("cargo", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        _ = self.process(21, "python3", parent=10, start=11_001,
                         argv=("python3", "offload.py", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        for state in step_record.StepState:
            with self.subTest(state=state):
                self.write_step("verify-call", state, 10)
                decisions = {item.process.identity.pid: item for item in self.decisions()}
                self.assertIsInstance(decisions[10], hold_enforcer.AllowedBuild)
                expected = (hold_enforcer.WouldStopBuild if state == step_record.StepState.ON_NATEDEV
                            else hold_enforcer.AllowedBuild)
                self.assertIsInstance(decisions[20], expected)
                self.assertIsInstance(decisions[21], expected)
                if state in (step_record.StepState.WAITING_HOLD, step_record.StepState.WAITING_MAC):
                    sent: list[int] = []
                    stopper = hold_enforcer.StopBuildsAndTests(
                        self.proc, signal_process=lambda pid, _signum: sent.append(pid))
                    for decision in decisions.values():
                        if isinstance(decision, hold_enforcer.WouldStopBuild):
                            stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force,
                                         999, self.claimed)
                    self.assertEqual(sent, [])

    def test_call_record_decides_unknown_commands_and_cargo_without_name_rules(self) -> None:
        _ = self.process(90, "coordinator")
        for pid, (command, argv) in enumerate((("worker", ("worker", "compile")),
                                             ("cargo", ("cargo", "check"))), 10):
            _ = self.process(pid, command, argv=argv, session="other",
                             environment={"BUILDLOG_CALL_ID": f"call-{pid}"})
        for state in step_record.StepState:
            for pid in (10, 11):
                self.write_step(f"call-{pid}", state, 90)
            table = self.table()
            decisions = hold_enforcer.decide_builds(table, build_hold.natedev_holds(self.holders).in_force,
                                                    self.uid, 999, self.claimed)
            self.assertEqual({item.process.identity.pid for item in decisions}, {10, 11})
            sent: list[int] = []
            stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, _signum: sent.append(pid))
            for decision in decisions:
                if isinstance(decision, hold_enforcer.WouldStopBuild):
                    stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
            self.assertEqual(sent, [10, 11] if state == step_record.StepState.ON_NATEDEV else [])
        self.assertEqual({(row["pid"], row["call_id"], row["action"]) for row in self.rows()},
                         {(10, "call-10", "stopped"), (11, "call-11", "stopped")})

    def test_missing_stale_and_unreadable_call_records_stop_a_verify_process(self) -> None:
        _ = self.process(10, "bash", argv=("bash", "verify.sh", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        for condition in ("missing", "stale", "unreadable"):
            with self.subTest(condition=condition):
                if condition == "stale":
                    self.write_step("verify-call", step_record.StepState.ON_MAC, 10)
                    _ = self.process(10, "bash", start=12_000, argv=("bash", "verify.sh", "check"), session="other",
                                     environment={"BUILDLOG_CALL_ID": "verify-call"})
                elif condition == "unreadable":
                    _ = step_record.record_path("verify-call").write_text("{broken")
                table = self.table()
                decision, = self.decisions()
                self.assertIsInstance(decision, hold_enforcer.WouldStopBuild)
                sent: list[int] = []
                if isinstance(decision, hold_enforcer.WouldStopBuild):
                    stopper = hold_enforcer.StopBuildsAndTests(
                        self.proc, signal_process=lambda pid, _signum: sent.append(pid))
                    stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
                self.assertEqual(sent, [10])

    def test_reroute_keeps_kill_for_old_compiler_and_protects_new_mac_children(self) -> None:
        _ = self.process(90, "coordinator")
        _ = self.process(10, "cargo", argv=("cargo", "check"), session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        _ = self.process(11, "worker", parent=10, start=11_001, session="other",
                         environment={"BUILDLOG_CALL_ID": "mac-call"})
        self.write_step("verify-call", step_record.StepState.ON_NATEDEV, 90)
        self.write_step("mac-call", step_record.StepState.ON_MAC, 90)
        table = self.table()
        decision = next(item for item in self.decisions() if item.process.identity.pid == 10)
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        sent: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(
            self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)), clock=lambda: self.now)
        stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.write_step("verify-call", step_record.StepState.WAITING_MAC, 90)
        _ = self.process(12, "worker", parent=90, start=11_002, session="other",
                         environment={"BUILDLOG_CALL_ID": "verify-call"})
        self.now = 3
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent, [(10, signal.SIGTERM), (10, signal.SIGKILL)])
        self.assertEqual(stopper.terminating, {})
        decisions = {item.process.identity.pid: item for item in self.decisions()}
        self.assertIsInstance(decisions[12], hold_enforcer.AllowedBuild)

    def test_sole_holder_keeps_its_on_natedev_record_process(self) -> None:
        _ = self.process(90, "coordinator")
        _ = self.process(10, "worker", session="holder", environment={"BUILDLOG_CALL_ID": "holder-call"})
        self.write_step("holder-call", step_record.StepState.ON_NATEDEV, 90)
        decision, = self.decisions()
        self.assertIsInstance(decision, hold_enforcer.AllowedBuild)
        self.assertEqual(decision.owner, OwningSession("holder"))
        member = staff.StaffRow(staff.StaffId("fixed-holder"), "producer", "renamed-holder", staff.StaffRoot(),
                                current_session=staff.StaffId("holder"))
        _ = (self.holders / "holder").rename(self.holders / member.id)
        with mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((member,))):
            decision, = self.decisions()
        self.assertIsInstance(decision, hold_enforcer.AllowedBuild)
        self.assertEqual(decision.owner, OwningSession("holder"))

    def test_stopped_run_writes_reason_and_log_registers_notice_then_terms_root_first_and_kills_survivor(self) -> None:
        root = self.process(30, "cargo", argv=("cargo", "check", "--workspace"), session="other")
        _ = (root / "fd/2").write_text("TREE ok\nFMT ok\n")
        _ = self.process(20, "rustc", parent=30, start=11_001)
        _ = self.process(10, "linker", parent=20, start=11_002)
        decision = next(item for item in self.decisions() if item.process.identity.pid == 30)
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        sent: list[tuple[int, int]] = []

        def send(pid: int, signum: int) -> None:
            self.assertEqual((root / "fd/2").read_text(),
                             "TREE ok\nFMT ok\nnatedev builds and tests are held by builder for timed test; this run was stopped. "
                             + "You get a notice at the release; run it again then.\n")
            self.assertEqual(self.rows()[0]["action"], "stopped")
            cycle = build_hold.read_cycle()
            assert not isinstance(cycle, build_hold.NoCycle)
            self.assertIn("other", cycle["recipients"])
            sent.append((pid, signum))
            if signum == signal.SIGTERM and pid != 30:
                (self.proc / str(pid) / "stat").unlink()

        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=send, clock=lambda: self.now)
        stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent, [(30, signal.SIGTERM), (20, signal.SIGTERM), (10, signal.SIGTERM)])
        row, = self.rows()
        self.assertEqual((row["pid"], row["proc_start"], row["session"], row["command"], row["hold_key"], row["cwd"]),
                         (30, "11000", "other", "cargo check --workspace", "holder", str(self.root)))
        self.now = 1.99
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(len(sent), 3)
        self.now = 2
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent[-1], (30, signal.SIGKILL))
        self.assertEqual(len(sent), 4)

        for index, (label, environment, ancestor_environment, recipients) in enumerate(self.stopped_worker_lineages()):
            with self.subTest(agent=label):
                started = 12_000 + index * 10
                cycle = build_hold.read_cycle()
                assert not isinstance(cycle, build_hold.NoCycle)
                cycle["recipients"], cycle["entries"] = {}, []
                build_hold.save_cycle(cycle)
                _ = self.process(40, "worker", start=10_000, environment=ancestor_environment)
                _ = self.process(30, "cargo", parent=40, start=started, argv=("cargo", "check"), environment=environment)
                _ = self.process(20, "rustc", parent=30, start=started + 1)
                _ = self.process(10, "linker", parent=20, start=started + 2)
                decision = next(item for item in self.decisions() if item.process.identity.pid == 30)
                assert isinstance(decision, hold_enforcer.WouldStopBuild)
                self.assertEqual(decision.owner, OwningSession("t1" if label == "lone Codex" else recipients[0]))
                signals: list[tuple[int, int]] = []
                register = build_hold.register_wait
                with mock.patch.object(build_hold, "register_wait", wraps=register) as registration:
                    def signal_worker(pid: int, signum: int) -> None:
                        saved = build_hold.read_cycle()
                        assert not isinstance(saved, build_hold.NoCycle)
                        self.assertCountEqual(saved["recipients"], recipients)
                        self.assertEqual([call.args[0] for call in registration.call_args_list], list(recipients))
                        signals.append((pid, signum))

                    stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=signal_worker, register=registration)
                    stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
                self.assertEqual(signals, [(30, signal.SIGTERM), (20, signal.SIGTERM), (10, signal.SIGTERM)])
                if label == "lone Codex":
                    self.assertEqual(self.rows()[-1]["session"], "t1")

    def test_codex_mac_idle_cargo_and_claude_unittest_and_reparented_worker_test_are_stopped(self) -> None:
        _ = self.process(10, "codex", start=100, environment={"CODEX_THREAD_ID": "codex-worker"})
        _ = self.process(20, "python3", parent=10, argv=("python3", "-m", "unittest", "scripts.direct.test_mac_idle"))
        cargo = self.process(21, "cargo", parent=20, start=11_001, argv=("/nix/store/rust/bin/cargo", "check", "--workspace"))
        (cargo / "cwd").unlink()
        (cargo / "cwd").symlink_to("/dev/shm/mac-idle-suite-123/tmp456")
        _ = self.process(30, "claude", start=100, session="claude-session")
        _ = self.process(31, "python3", parent=30, argv=("python3", "-m", "unittest", "scripts.agents.codex_test_support"))
        _ = self.process(40, "python3", argv=("python3", "-m", "unittest", "scripts.production.test_gate"),
                         environment={"CLAUDE_CODE_SESSION_ID": "detached-session"})
        decisions = {item.process.identity.pid: item for item in self.decisions()}
        self.assertEqual(set(decisions), {20, 21, 31, 40})
        self.assertTrue(all(isinstance(item, hold_enforcer.WouldStopBuild) for item in decisions.values()))
        self.assertEqual(decisions[21].owner, OwningSession("codex-worker"))
        self.assertEqual(decisions[21].process.cwd, "/dev/shm/mac-idle-suite-123/tmp456")
        self.assertEqual(decisions[40].owner, OwningSession("detached-session"))

    def test_waiting_merge_gate_and_verify_survive_while_their_build_children_stop(self) -> None:
        _ = self.process(30, "python3", argv=("python3", "merge_gate.py", "--checkout", "/tree", "--tests-only"), session="gate")
        _ = self.process(20, "python3", parent=30, start=11_001, argv=("python3", "-m", "unittest", "suite"))
        _ = self.process(10, "rustc", parent=20, start=11_002)
        _ = self.process(21, "basedpyright", parent=30, start=11_001, argv=("basedpyright", "src"))
        _ = self.process(40, "bash", argv=("bash", "verify.sh", "test"), session="verify")
        _ = self.process(41, "cargo", parent=40, start=11_001,
                         argv=("/home/user/.rustup/toolchains/stable/bin/cargo", "test"))
        sent: list[int] = []
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, _signum: sent.append(pid))
        decisions = self.decisions()
        for item in decisions:
            if isinstance(item, hold_enforcer.WouldStopBuild):
                stopper.stop(item, self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent, [20, 10, 21, 41])
        self.assertIsInstance(next(item for item in decisions if item.process.identity.pid == 30), hold_enforcer.AllowedBuild)
        self.assertIsInstance(next(item for item in decisions if item.process.identity.pid == 40), hold_enforcer.AllowedBuild)
        self.assertEqual({row["pid"] for row in self.rows()}, {20, 21, 41})
        self.assertTrue(all(row["action"] == "stopped" for row in self.rows()))

    def test_merge_gate_entry_points_wait_but_mentions_do_not_protect_tests(self) -> None:
        for name in ("cloud_gate.py", "gate_box.py"):
            with self.subTest(name=name):
                _ = self.process(30, "python3", argv=("python3", f"/tree/{name}"), session="other")
                self.assertEqual(self.decisions(), ())
        for argv in (
            ("/tree/merge_gate.py", "--tests-only"),
            ("/tree/gate.py", "--tests-only"),
            ("python3", "-u", "/tree/gate.py", "--affected"),
            ("python3", "-m", "scripts.production.gate", "--tests-only"),
            ("python3", "-u", "/tree/merge_gate.py", "--promote-only"),
            ("python3", "-m", "scripts.production.merge_gate", "--tests-only"),
        ):
            with self.subTest(argv=argv):
                _ = self.process(30, "python3", argv=argv, session="other")
                decision, = self.decisions()
                self.assertIsInstance(decision, hold_enforcer.AllowedBuild)
        for argv in (
            ("python3", "-m", "unittest", "merge_gate.py"),
            ("python3", "/tree/test_driver.py", "merge_gate.py"),
        ):
            with self.subTest(argv=argv):
                _ = self.process(30, "python3", argv=argv, session="other")
                decision, = self.decisions()
                self.assertIsInstance(decision, hold_enforcer.WouldStopBuild)

    def test_old_nextest_stops_keep_covers_children_and_reused_keep_pid_does_not(self) -> None:
        _ = self.process(10, "cargo-nextest", start=1000, session="other")
        _ = self.process(20, "rustc", parent=10, start=2000)
        self.assertTrue(all(isinstance(item, hold_enforcer.WouldStopBuild) for item in self.decisions()))
        self.write_holder((build_hold.ProcessIdentity(10, "1000"),))
        self.assertTrue(all(isinstance(item, hold_enforcer.KeptBuild) for item in self.decisions()))
        _ = self.process(10, "cargo-nextest", start=1500, session="other")
        self.assertTrue(all(isinstance(item, hold_enforcer.WouldStopBuild) for item in self.decisions()))

    def test_session_owned_headless_rust_analyzer_stops_after_twelve_hour_parent(self) -> None:
        _ = self.process(10, "python3", start=100, argv=("python3", "scripts/fixture.py"), session="rename")
        _ = self.process(20, "rust-analyzer", parent=10)
        decision, = self.decisions()
        self.assertIsInstance(decision, hold_enforcer.WouldStopBuild)
        self.assertEqual(decision.owner, OwningSession("rename"))
        self.assertEqual(decision.process.identity.pid, 20)

    def test_holder_user_terminal_desktop_and_shared_server_pass_but_service_and_marked_terminal_stop(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "test"), session="holder")
        _ = self.process(20, "bash", tty=22)
        _ = self.process(21, "cargo", parent=20, argv=("cargo", "test"))
        _ = self.process(30, "sccache", argv=("sccache", "--server"))
        _ = self.process(31, "rustc", parent=30)
        _ = self.process(40, "sccache", environment={"SCCACHE_START_SERVER": "1"})
        _ = self.process(41, "rustc", parent=40)
        _ = self.process(50, "basedpyright", argv=("/venv/bin/basedpyright", "src"), session="agent")
        _ = self.process(60, "zed", start=100)
        _ = self.process(61, "basedpyright-langserver", parent=60)
        _ = self.process(62, "cargo", parent=60, argv=("cargo", "check"))
        _ = self.process(70, "cargo", argv=("cargo", "build"))
        _ = self.process(80, "cargo", tty=33, argv=("cargo", "build"), environment={"CODEX_THREAD_ID": "detached"})
        app = self.root / f"cgroup/user.slice/user-{self.uid}.slice/user@{self.uid}.service/app.slice"
        desktop = app / "app-zed@one.service"
        desktop.mkdir(parents=True)
        _ = (desktop / "cgroup.procs").write_text("60\n61\n")
        service = app / "run-daemon.service"
        service.mkdir()
        _ = (service / "cgroup.procs").write_text("70\n")
        decisions = {item.process.identity.pid: item for item in self.decisions()}
        for pid in (10, 21, 31, 41, 62):
            self.assertIsInstance(decisions[pid], hold_enforcer.AllowedBuild)
        for pid in (50, 70, 80):
            self.assertIsInstance(decisions[pid], hold_enforcer.WouldStopBuild)
        self.assertNotIn(61, decisions)

    def test_all_test_entry_points_and_executable_path_match_without_treating_lookup_or_language_server_as_builds(self) -> None:
        cases = (
            ("python3", ("python3", "-m", "pytest")), ("py.test", ("/venv/bin/py.test",)),
            ("python3", ("python3", "/tree/test_driver.py")),
            ("bash", ("bash", "-e", "/tree/test_build.sh")), ("clippy-driver", ("clippy-driver",)),
            ("node", ("node", "/tools/basedpyright.js", "src")),
        )
        expected: set[int] = set()
        for pid, (command, argv) in enumerate(cases, 10):
            _ = self.process(pid, command, argv=argv)
            expected.add(pid)
        _ = self.process(30, "hidden", argv=("renamed", "check"), executable="/toolchain/bin/cargo")
        expected.add(30)
        _ = self.process(40, "cargo", argv=("cargo", "+nightly", "--config", "build", "metadata"))
        _ = self.process(41, "python3", argv=("python3", "helper.py", "-m", "unittest"))
        _ = self.process(42, "python3", argv=("python3", "-c", "print('pytest')"))
        _ = self.process(43, "basedpyright-langserver")
        _ = self.process(44, "rust-analyzer")
        _ = self.process(45, "python3", argv=("python3", "/tree/driver_test.py"))
        decisions = self.decisions()
        self.assertEqual({item.process.identity.pid for item in decisions}, expected)
        self.assertTrue(all(isinstance(item, hold_enforcer.WouldStopBuild) for item in decisions))

    def test_hold_stops_natedev_tests_but_spares_mac_coordination(self) -> None:
        for pid, verb in enumerate(("watch-claim", "free-now", "claim", "release"), 10):
            _ = self.process(pid, "python3", session="other", argv=(
                "python3", "-u", "/tree/scripts/mac_test/mac_test.py", verb,
            ))
        _ = self.process(20, "python3", session="other", argv=(
            "python3", "-m", "scripts.mac_test.mac_test", "watch-claim",
        ))
        _ = self.process(21, "mac_test.py", session="other", executable="/tools/python3", argv=(
            "/tree/scripts/mac_test/mac_test.py", "release",
        ))
        _ = self.process(30, "python3", session="other", argv=("python3", "-m", "unittest", "suite"))
        _ = self.process(31, "cargo", session="other", argv=("cargo", "nextest", "run"))
        table = self.table()
        holders = build_hold.natedev_holds(self.holders).in_force
        decisions = hold_enforcer.decide_builds(table, holders, self.uid, 999, self.claimed)
        self.assertEqual({item.process.identity.pid for item in decisions}, {30, 31})
        sent: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(
            self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)),
        )
        for decision in decisions:
            self.assertIsInstance(decision, hold_enforcer.WouldStopBuild)
            if isinstance(decision, hold_enforcer.WouldStopBuild):
                stopper.stop(decision, table, holders, 999, self.claimed)
        self.assertEqual(sent, [(30, signal.SIGTERM), (31, signal.SIGTERM)])
        self.assertEqual([row["pid"] for row in self.rows()], [30, 31])

        for condition in ("live", "stale record", "ended", "unrelated", "reused pid"):
            with self.subTest(claim=condition):
                step_record.record_path("claimed-call").unlink(missing_ok=True)
                _ = self.process(30, "python3", session="other", argv=("python3", "-m", "unittest", "suite"),
                                 environment={"BUILDLOG_CALL_ID": "claimed-call"})
                if condition == "stale record":
                    self.write_step("claimed-call", step_record.StepState.ON_MAC, 30)
                    _ = self.process(30, "python3", start=12_000, session="other",
                                     argv=("python3", "-m", "unittest", "suite"),
                                     environment={"BUILDLOG_CALL_ID": "claimed-call"})
                _ = self.process(32, "python3", parent=30, start=12_001,
                                 argv=("python3", "offload.py", "check"),
                                 environment={"BUILDLOG_CALL_ID": "claimed-call"})
                _ = self.process(33, "ssh", parent=32, start=12_002,
                                 environment={"BUILDLOG_CALL_ID": "claimed-call"})
                table = self.table()
                before = hold_enforcer.decide_builds(table, holders, self.uid, 999, frozenset())
                stopped = next(item for item in before if item.process.identity.pid == 30)
                assert isinstance(stopped, hold_enforcer.WouldStopBuild)
                claimed = table[32].identity
                if condition == "unrelated":
                    claimed = table[31].identity
                elif condition == "reused pid":
                    claimed = build_hold.ProcessIdentity(32, "12000")
                self.claimed = frozenset() if condition == "ended" else frozenset({claimed})
                decisions = {item.process.identity.pid: item for item in
                             hold_enforcer.decide_builds(table, holders, self.uid, 999, self.claimed)}
                allowed = condition in ("live", "stale record")
                for pid in (30, 32, 33):
                    self.assertIsInstance(decisions[pid], hold_enforcer.AllowedBuild if allowed
                                          else hold_enforcer.WouldStopBuild)
                    if allowed:
                        self.assertEqual(decisions[pid].reason, "holds a live Mac or AWS claim")
                sent = []
                stopper = hold_enforcer.StopBuildsAndTests(
                    self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)))
                stopper.stop(stopped, table, holders, 999, self.claimed)
                self.assertEqual(sent, [] if allowed else [(pid, signal.SIGTERM) for pid in (30, 32, 33)])

    def test_verify_version_and_metadata_probes_are_not_builds_during_hold(self) -> None:
        _ = self.process(5, "bash", session="verify", argv=("bash", "verify.sh", "test"))
        probes = (
            ("rustc", "-vV"), ("rustc", "--version"), ("rustc", "-V"),
            ("rustc", "+nightly", "-vV"), ("cargo", "--version"),
            ("cargo", "metadata"), ("cargo", "+nightly", "metadata", "--format-version", "1"),
        )
        for pid, argv in enumerate(probes, 10):
            _ = self.process(pid, argv[0], parent=5, argv=argv)
        _ = self.process(30, "rustc", parent=5, argv=("rustc", "/tree/lib.rs"))
        decisions = {item.process.identity.pid: item for item in self.decisions()}
        self.assertEqual(set(decisions), {5, 30})
        self.assertIsInstance(decisions[5], hold_enforcer.AllowedBuild)
        self.assertIsInstance(decisions[30], hold_enforcer.WouldStopBuild)

    def test_reader_checks_other_environments_but_limits_arguments_to_candidates_and_records(self) -> None:
        _ = self.process(10, "claude", start=1000, session="other")
        _ = self.process(20, "cargo", parent=10, argv=("cargo", "build"))
        _ = self.process(30, "cargo-port")
        read_bytes = Path.read_bytes
        detailed: set[tuple[int, str]] = set()

        def read(path: Path) -> bytes:
            detailed.add((int(path.parent.name), path.name))
            return read_bytes(path)

        with mock.patch.object(Path, "read_bytes", read):
            table = self.table()
        self.assertEqual(detailed, {(10, "cmdline"), (10, "environ"), (20, "cmdline"),
                                    (20, "environ"), (30, "environ")})
        self.assertEqual(table[30].environment, {})
        for command, expected in (("gate.py", True), ("cloud_gate.py", False), ("gate_box.py", False)):
            with self.subTest(command=command):
                _ = self.process(40, command, argv=(command, "--tests-only"))
                detailed.clear()
                with mock.patch.object(Path, "read_bytes", read):
                    _ = self.table()
                self.assertEqual((40, "cmdline") in detailed, expected)


    def test_other_uid_and_enforcer_ancestors_are_never_stopped(self) -> None:
        _ = self.process(10, "python3", argv=("python3", "-m", "unittest"))
        _ = self.process(20, "python3", parent=10, argv=("python3", "hold_enforcer.py", "run"), start=11_001)
        _ = self.process(30, "cargo", argv=("cargo", "build"))
        table = self.table()
        table[30] = replace(table[30], uid=self.uid + 1)
        decisions = hold_enforcer.decide_builds(table, build_hold.natedev_holds(self.holders).in_force, self.uid, 20, self.claimed)
        decision, = decisions
        self.assertIsInstance(decision, hold_enforcer.AllowedBuild)
        self.assertEqual(decision.process.identity.pid, 10)

    def test_pid_reuse_before_term_or_kill_gets_no_signal(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "build"))
        decision, = self.decisions()
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        table = self.table()
        sent: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)),
                                                 clock=lambda: self.now)
        _ = self.process(10, "cargo", start=12_000, argv=("cargo", "build"))
        stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual((sent, self.rows()), ([], []))
        decision, = self.decisions()
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        _ = self.process(10, "cargo", start=13_000, argv=("cargo", "build"))
        self.now = 2
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent, [(10, signal.SIGTERM)])

    def test_keep_added_during_term_grace_prevents_kill_of_root_and_children(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "build"))
        _ = self.process(20, "rustc", parent=10, start=11_001)
        decision = next(item for item in self.decisions() if item.process.identity.pid == 10)
        assert isinstance(decision, hold_enforcer.WouldStopBuild)
        sent: list[tuple[int, int]] = []
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)),
                                                 clock=lambda: self.now)
        stopper.stop(decision, self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.write_holder((decision.process.identity,))
        self.now = 2
        stopper.kill_survivors(self.table(), build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent, [(10, signal.SIGTERM), (20, signal.SIGTERM)])

        self.write_holder()
        _ = self.process(30, "worker", parent=20, start=11_002)
        table = self.table()
        sent.clear()
        stopper = hold_enforcer.StopBuildsAndTests(
            self.proc, signal_process=lambda pid, signum: sent.append((pid, signum)), clock=lambda: self.now)
        stopper.stop(decision, table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.claimed = frozenset({table[20].identity})
        self.now = 5
        stopper.kill_survivors(table, build_hold.natedev_holds(self.holders).in_force, 999, self.claimed)
        self.assertEqual(sent, [(10, signal.SIGTERM), (20, signal.SIGTERM), (30, signal.SIGTERM)])
        self.assertEqual(stopper.terminating, {})

    def test_run_reports_overdue_holds_once_across_restarts_and_retries_failed_sends(self) -> None:
        directory = self.root / "enforcer"
        path = self.holders / "holder"
        release = datetime(2026, 10, 28, 12, 1, tzinfo=timezone.utc)
        current = release - timedelta(seconds=1)
        attempts: list[tuple[datetime, str, str]] = []
        expected_per_pass: tuple[int, ...] = ()
        fail_next = False
        signals: list[int] = []
        _ = self.process(30, "cargo", argv=("cargo", "check"))
        stopper = hold_enforcer.StopBuildsAndTests(
            self.proc, signal_process=lambda pid, _signum: signals.append(pid), clock=lambda: self.now)

        def publish(since: datetime, eta: datetime) -> None:
            _ = path.write_text(json.dumps({
                "holder": "builder", "since": since.isoformat(), "for": "timed test",
                "release_eta": eta.isoformat(), "keep": [],
            }))

        def send(key: str, text: str) -> build_hold.HolderMessageSent | build_hold.HolderMessageFailed:
            nonlocal fail_next
            attempts.append((current, key, text))
            if fail_next:
                fail_next = False
                return build_hold.HolderMessageFailed("sender unavailable")
            return build_hold.HolderMessageSent()

        def passes(instants: tuple[datetime, ...], zones: tuple[str, ...] = ()) -> None:
            nonlocal current
            index = 0
            current = instants[index]
            before = path.read_bytes()
            previous_attempts = list(attempts)
            previous_markers = sorted((directory / "overdue-notices").glob("*"))
            if zones:
                os.environ["USER_ZONE"] = zones[index]

            def advance(seconds: float) -> None:
                nonlocal index, current
                self.assertEqual(seconds, 1)
                self.assertEqual(path.read_bytes(), before)
                if expected_per_pass:
                    self.assertEqual(len(attempts), expected_per_pass[index])
                if os.environ.get("USER_ZONE") == "Invalid/Zone":
                    self.assertEqual(attempts, previous_attempts)
                    self.assertEqual(sorted((directory / "overdue-notices").glob("*")), previous_markers)
                index += 1
                if index == len(instants):
                    raise InterruptedError("restart the fixture enforcer")
                current = instants[index]
                if zones:
                    os.environ["USER_ZONE"] = zones[index]

            with self.assertRaisesRegex(InterruptedError, "restart the fixture enforcer"):
                _ = hold_enforcer.run(self.proc, self.holders, directory, uid=self.uid, enforcer_pid=999,
                    stopper=stopper, sleep=advance, cgroup_root=self.root / "cgroup", send=send, clock=lambda: current)

        publish(release - timedelta(minutes=1), release)
        with mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}):
            passes((release - timedelta(seconds=1),))
            self.assertEqual(attempts, [])
            passes((release, release + timedelta(seconds=1)))
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0][:2], (release, "holder"))
            for words in ("timed test", "12:01 UTC", "stays in force", "/build:hold release natedev",
                          "clear", "drain", "--release-eta"):
                self.assertIn(words, attempts[0][2])
            passes((release + timedelta(seconds=20), release + timedelta(seconds=30)))
            self.assertEqual(len(attempts), 1)

            next_release = release + timedelta(minutes=1)
            publish(release + timedelta(seconds=30), next_release)
            fail_next = True
            errors = io.StringIO()
            with redirect_stderr(errors):
                passes((next_release - timedelta(seconds=1), next_release,
                        next_release + timedelta(seconds=1), next_release + timedelta(seconds=59),
                        next_release + timedelta(seconds=60), next_release + timedelta(seconds=61)))
            self.assertEqual([attempt[0] for attempt in attempts],
                             [release, next_release, next_release + timedelta(seconds=60)])
            self.assertEqual(len(errors.getvalue().splitlines()), 1)
            self.assertIn("sender unavailable", errors.getvalue())
            passes((next_release + timedelta(seconds=62),))
            self.assertEqual(len(attempts), 3)
            self.assertEqual(signals, [30])

            invalid_zone_release = next_release + timedelta(minutes=2)
            publish(next_release + timedelta(minutes=1), invalid_zone_release)
            _ = self.process(40, "cargo", argv=("cargo", "check"))
            errors = io.StringIO()
            with redirect_stderr(errors):
                passes((invalid_zone_release, invalid_zone_release + timedelta(seconds=1),
                        invalid_zone_release + timedelta(seconds=59), invalid_zone_release + timedelta(seconds=60)),
                       ("Invalid/Zone", "UTC", "UTC", "UTC"))
            self.assertEqual(len(attempts), 4)
            self.assertEqual(attempts[-1][:2], (invalid_zone_release + timedelta(seconds=60), "holder"))
            self.assertIn("Invalid/Zone", errors.getvalue())

            for case in ("two holders", "failed first send", "failed marker write"):
                with self.subTest(case=case):
                    directory = self.root / case.replace(" ", "-")
                    attempts.clear()
                    publish(release - timedelta(minutes=1), release)
                    second = self.holders / "second"
                    second.unlink(missing_ok=True)
                    fail_next = False
                    if case == "failed marker write":
                        directory.mkdir()
                        _ = (directory / "overdue-notices").write_text("blocked marker directory\n")
                        instants = (release, release + timedelta(seconds=60), release + timedelta(seconds=120))
                        expected_per_pass = (1, 1, 1)
                    else:
                        _ = second.write_text(json.dumps({
                            "holder": "second", "since": (release - timedelta(seconds=30)).isoformat(),
                            "for": "second test", "release_eta": release.isoformat(), "keep": [],
                        }))
                        fail_next = case == "failed first send"
                        instants = ((release, release + timedelta(seconds=1), release + timedelta(seconds=59),
                                     release + timedelta(seconds=60), release + timedelta(seconds=61))
                                    if fail_next else (release, release + timedelta(seconds=1)))
                        expected_per_pass = (1, 2, 2, 3, 3) if fail_next else (1, 2)
                    errors = io.StringIO()
                    try:
                        with redirect_stderr(errors):
                            passes(instants)
                    finally:
                        second.unlink(missing_ok=True)
                        if case == "failed marker write":
                            (directory / "overdue-notices").unlink()
                    if case == "failed marker write":
                        self.assertEqual([attempt[1] for attempt in attempts], ["holder"])
                        self.assertEqual(len(errors.getvalue().splitlines()), 1)
                        self.assertIn("could not save overdue notice", errors.getvalue())
                    else:
                        self.assertEqual([attempt[1] for attempt in attempts],
                                         ["holder", "second", "holder"] if case == "failed first send"
                                         else ["holder", "second"])

        self.assertEqual(signals, [30, 40])
        path.unlink()
        self.assertEqual(hold_enforcer.run(self.proc, self.holders, directory, uid=self.uid, enforcer_pid=999,
            stopper=stopper, cgroup_root=self.root / "cgroup", send=send, clock=lambda: current), 0)
        self.assertFalse((directory / "overdue-notices").exists())

    def test_run_stops_descendants_once_then_exits_on_file_removal_without_reading_another_table(self) -> None:
        _ = self.process(30, "cargo", argv=("cargo", "check"))
        _ = self.process(20, "rustc", parent=30, start=11_001)
        sent: list[int] = []
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, _signum: sent.append(pid))

        def release(_seconds: float) -> None:
            (self.holders / "holder").unlink()

        def run() -> int:
            return hold_enforcer.run(self.proc, self.holders, self.root / "enforcer", uid=self.uid,
                                     enforcer_pid=999, stopper=stopper, sleep=release, cgroup_root=self.root / "cgroup")

        self.assertEqual(run(), 0)
        self.assertEqual(sent, [30, 20])
        self.assertEqual([row["pid"] for row in self.rows()], [30])
        with mock.patch.object(build_hold, "read_audit_processes", side_effect=AssertionError("empty hold reads no table")):
            self.assertEqual(run(), 0)

        self.write_holder()
        _ = self.process(40, "python3", parent=20, start=11_002, argv=("python3", "offload.py", "check"))
        self.claimed = frozenset({self.table()[40].identity})
        sent.clear()
        stopper = hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, _signum: sent.append(pid))
        passes = 0

        def finish_claim(_seconds: float) -> None:
            nonlocal passes
            passes += 1
            if passes == 1:
                self.assertEqual(sent, [])
                self.claimed = frozenset()
            else:
                self.assertEqual(sent, [30, 20, 40])
                (self.holders / "holder").unlink()

        with mock.patch.object(hold_enforcer, "claimed_processes", side_effect=self.read_claims) as claims:
            self.assertEqual(hold_enforcer.run(
                self.proc, self.holders, self.root / "enforcer", uid=self.uid,
                enforcer_pid=999, stopper=stopper, sleep=finish_claim, cgroup_root=self.root / "cgroup"), 0)
        self.assertEqual(claims.call_args_list, [mock.call(self.proc), mock.call(self.proc)])
        self.assertEqual(passes, 2)

        for failure in ("claim file", "target config", "claim after table"):
            with self.subTest(claim_read=failure):
                self.write_holder()
                self.claimed = frozenset()
                paths = claim_state.StatePaths(self.root / "claims-state")
                paths.directory.mkdir(exist_ok=True)
                _ = paths.run.write_text("{broken")
                events: list[str] = []
                signals: list[tuple[int, int]] = []
                passes = 0
                stopper = hold_enforcer.StopBuildsAndTests(
                    self.proc, signal_process=lambda pid, signum: signals.append((pid, signum)), clock=lambda: 5)
                if failure != "claim after table":
                    stopper.terminating[build_hold.ProcessIdentity(20, "11001")] = 0
                read_table = build_hold.read_audit_processes

                def table_before_claims(
                    proc_root: Path, selection: build_hold.HoldBuildCandidates, cgroup_root: Path,
                ) -> dict[int, build_hold.AuditProcess]:
                    table = read_table(proc_root, selection, cgroup_root)
                    events.append("table")
                    if failure == "claim after table":
                        self.claimed = frozenset({table[40].identity})
                    return table

                def targets() -> tuple[target_config.Target, ...]:
                    if failure == "target config" and passes < 2:
                        raise target_config.ConfigError("private target config is damaged")
                    return (target_config.SshTarget("mac", "macos", self.root / "mac.conf"),)

                def read_after_table(proc_root: Path) -> build_hold.LiveClaims | build_hold.ClaimsUnreadable:
                    events.append("claims")
                    self.assertEqual(events[-2:], ["table", "claims"])
                    if failure == "claim after table":
                        return self.read_claims(proc_root)
                    return self.claim_reader(proc_root)

                def recover(seconds: float) -> None:
                    nonlocal passes
                    self.assertEqual(seconds, 1)
                    passes += 1
                    if failure == "claim after table":
                        self.assertEqual(signals, [])
                        (self.holders / "holder").unlink()
                    elif passes < 3:
                        self.assertEqual(signals, [])
                        if passes == 2:
                            paths.run.unlink()
                    else:
                        self.assertIn((20, signal.SIGKILL), signals)
                        self.assertIn((30, signal.SIGTERM), signals)
                        (self.holders / "holder").unlink()

                errors = io.StringIO()
                with mock.patch.object(build_hold, "read_audit_processes", side_effect=table_before_claims), \
                     mock.patch.object(hold_enforcer, "claimed_processes", side_effect=read_after_table), \
                     mock.patch.object(target_config, "read_targets", side_effect=targets), \
                     mock.patch.object(claim_state, "state_paths", return_value=paths), redirect_stderr(errors):
                    self.assertEqual(hold_enforcer.run(
                        self.proc, self.holders, self.root / "enforcer", uid=self.uid,
                        enforcer_pid=999, stopper=stopper, sleep=recover, cgroup_root=self.root / "cgroup"), 0)
                if failure == "claim after table":
                    self.assertEqual(errors.getvalue(), "")
                else:
                    self.assertEqual(len(errors.getvalue().splitlines()), 2)
                    self.assertIn("enforcement waits", errors.getvalue())
                    self.assertIn("claims readable; enforcement resumes", errors.getvalue())
                    self.assertIn(str(paths.run) if failure == "claim file" else "private target config", errors.getvalue())

    def test_clear_cutoff_stays_fixed_when_claims_recover_after_a_clock_change(self) -> None:
        _ = self.process(10, "bash", start=9_000, argv=("bash", "verify.sh", "check"))
        _ = self.process(20, "bash", start=11_000, argv=("bash", "verify.sh", "check"))
        _ = self.process(30, "bash", start=9_000, argv=("bash", "verify.sh", "check"))
        _ = self.process(31, "cargo", parent=30, start=9_001, argv=("cargo", "test"))
        self.write_holder((build_hold.ProcessIdentity(31, "9001"),))
        signals: list[tuple[int, int]] = []
        passes = 0

        def recover(_seconds: float) -> None:
            nonlocal passes
            passes += 1
            self.assertEqual(signals, [] if passes == 1 else [(10, signal.SIGTERM)])
            if passes == 1:
                wall_clock.return_value = 0
                boot_clock.return_value = 121_000_000_000
                _ = self.process(40, "bash", start=12_001, argv=("bash", "verify.sh", "check"))
            if passes == 3:
                (self.holders / "holder").unlink()

        stopper = hold_enforcer.StopBuildsAndTests(self.proc,
            signal_process=lambda pid, signum: signals.append((pid, signum)))
        with mock.patch.object(hold_enforcer, "claimed_processes", side_effect=[
                build_hold.ClaimsUnreadable("private claims are damaged"),
                build_hold.LiveClaims(frozenset()), build_hold.LiveClaims(frozenset())]), \
             mock.patch.object(time, "clock_gettime_ns", return_value=100_000_000_000) as boot_clock, \
             mock.patch.object(time, "time_ns", return_value=120_000_000_000) as wall_clock, \
             mock.patch.object(os, "sysconf", return_value=100), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            holder = build_hold.read_holder(self.holders / "holder")
            hold_enforcer.record_clear_cutoff(build_hold.HolderIdentity(holder.key, holder.name), holder.since)
            boot_clock.return_value = 120_000_000_000
            self.assertEqual(hold_enforcer.run(self.proc, self.holders, self.root / "enforcer",
                uid=self.uid, enforcer_pid=999, stopper=stopper, sleep=recover, cgroup_root=self.root / "cgroup"), 0)
        self.assertEqual(passes, 3)
        boot_clock.assert_called_once_with(time.CLOCK_BOOTTIME)
        wall_clock.assert_not_called()
        self.assertIn("claims readable; enforcement resumes", errors.getvalue())
        self.assertEqual([row["pid"] for row in self.rows()], [10])

    def test_clear_cutoff_requires_the_same_hold_and_boot(self) -> None:
        holder = build_hold.read_holder(self.holders / "holder")
        self.assertEqual(hold_enforcer.read_clear_cutoff(holder), 0)
        with mock.patch.object(time, "clock_gettime_ns", return_value=100_000_000_000), \
             mock.patch.object(os, "sysconf", return_value=100), \
             mock.patch.object(hold_enforcer, "boot_identity", return_value="boot-one"):
            hold_enforcer.record_clear_cutoff(build_hold.HolderIdentity(holder.key, holder.name), holder.since)
            self.assertEqual(hold_enforcer.read_clear_cutoff(holder), 10_000)
            other = replace(holder, since=datetime.fromtimestamp(101, timezone.utc))
            self.assertEqual(hold_enforcer.read_clear_cutoff(other), 0)
        with mock.patch.object(hold_enforcer, "boot_identity", return_value="boot-two"):
            self.assertEqual(hold_enforcer.read_clear_cutoff(holder), 0)
        path = hold_enforcer.clear_cutoff_path(holder.key, holder.since)
        for contents in ("{broken", '[]', '{"boot_id":"boot-one","before":true}'):
            with self.subTest(contents=contents), \
                 mock.patch.object(hold_enforcer, "boot_identity", return_value="boot-one"):
                _ = path.write_text(contents)
                self.assertEqual(hold_enforcer.read_clear_cutoff(holder), 0)

    def test_rehold_keep_added_after_table_read_is_checked_before_any_signal(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "check"))
        table = self.table()
        sent: list[int] = []

        def changed_hold(_root: Path, _selection: build_hold.HoldBuildCandidates, _cgroups: Path) -> dict[int, build_hold.AuditProcess]:
            self.write_holder((table[10].identity,))
            return table

        def release(_seconds: float) -> None:
            (self.holders / "holder").unlink()

        with mock.patch.object(build_hold, "read_audit_processes", side_effect=changed_hold) as read:
            result = hold_enforcer.run(self.proc, self.holders, self.root / "enforcer", uid=self.uid, enforcer_pid=999,
                stopper=hold_enforcer.StopBuildsAndTests(self.proc, signal_process=lambda pid, _signum: sent.append(pid)),
                sleep=release, cgroup_root=self.root / "cgroup")
        self.assertEqual(result, 0)
        read.assert_called_once()
        self.assertEqual((sent, self.rows()), ([], []))

    def test_scan_reports_stop_allowed_and_kept_without_changes_or_signals(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "check"), start=9000)
        _ = self.process(20, "cargo", argv=("cargo", "build"), session="holder")
        _ = self.process(30, "python3", argv=("python3", "-m", "unittest"))
        self.write_holder((build_hold.ProcessIdentity(10, "9000"),))
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        output = io.StringIO()
        with mock.patch.object(os, "kill", side_effect=AssertionError("scan sends no signals")), redirect_stdout(output):
            hold_enforcer.scan(self.proc, self.holders, self.uid, 999, self.root / "cgroup")
        text = output.getvalue()
        self.assertIn("kept: no session, pid 10", text)
        self.assertIn("allowed: holder, pid 20", text)
        self.assertIn("would stop: no session, pid 30", text)
        self.assertIn("--keep 30", text)
        self.assertRegex(text, r"pass: [0-9.]+ s; 3 processes")
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

        for reader in ("scan", "report"):
            with self.subTest(claim_reader=reader):
                output = io.StringIO()
                with mock.patch.object(hold_enforcer, "claimed_processes",
                                       return_value=build_hold.ClaimsUnreadable("private claims are damaged")), \
                     redirect_stdout(output):
                    if reader == "scan":
                        hold_enforcer.scan(self.proc, self.holders, self.uid, 999, self.root / "cgroup")
                        text = output.getvalue()
                    else:
                        text = "\n".join(hold_enforcer.report(self.proc, self.holders))
                self.assertEqual(text.count("claims unreadable: private claims are damaged"), 1)
                self.assertIn("would stop: no session, pid 30", text)

    def test_audit_and_stopped_rows_share_writer_and_deduplicate_each_action(self) -> None:
        _ = self.process(10, "cargo", argv=("cargo", "build"), session="other")
        decision, = self.decisions()
        build = build_hold.AuditedBuild(decision.process, decision.owner, "other_session", ())
        holder, = build_hold.natedev_holds(self.holders).in_force
        actions: tuple[Literal["audit", "stopped"], ...] = ("audit", "stopped", "audit", "stopped")
        for action in actions:
            hold_enforcer.record_build(build, holder, self.proc, action)
        self.assertEqual([row["action"] for row in self.rows()], ["audit", "stopped"])

    def test_start_keeps_active_unit_and_uses_absolute_paths_or_detached_fallback(self) -> None:
        with mock.patch.object(shutil, "which", return_value="/tools/systemctl"), \
             mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "active\n", "")) as run, \
             mock.patch.object(subprocess, "Popen") as launch:
            hold_enforcer.start()
            self.assertEqual(run.call_count, 1)
            launch.assert_not_called()
        with mock.patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "test-bus"}), \
             mock.patch.object(shutil, "which", return_value="/tools/tool"), \
             mock.patch.object(subprocess, "run", side_effect=[subprocess.CompletedProcess([], 3, "inactive\n", ""),
                                                              subprocess.CompletedProcess([], 0, "", "")]) as run:
            hold_enforcer.start()
            command = cast(list[str], run.call_args.args[0])
            self.assertIn("--unit=build-hold-enforcer", command)
            self.assertEqual(command[-1], "run")
            self.assertTrue(Path(command[-2]).is_absolute())
            self.assertTrue(Path(command[-3]).is_absolute())
        with mock.patch.object(shutil, "which", return_value=None), \
             mock.patch.object(subprocess, "Popen") as launch:
            hold_enforcer.start()
            self.assertTrue(launch.call_args.kwargs["start_new_session"])


    # Machine holds (item 226, part 3): the enforcer acts on the named machine only.

    def machine_hold(self, machine: machine_hold.Machine, verb: machine_hold.HoldVerb,
                     comment: str = "Mac system update") -> machine_hold.MachineHold:
        return machine_hold.MachineHold(machine, verb, machine_hold.NamedOwner("bob-id", "bob"), comment)

    def write_claim(self, machine: machine_hold.Machine, pid: int, *, start: str = "11000") -> None:
        path = hold_enforcer.machine_state_paths(machine).claims / f"repo-{pid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({
            "claim_id": f"claim-{pid}", "episode": f"episode-{pid}", "attempt": "1", "submitting_session": "",
            "pid": pid, "proc_start": start, "what": f"tests {pid}", "worktree": "repo",
            "since": "2026-11-22T12:00:00+00:00"}))

    def write_block(self, machine: machine_hold.Machine, holder: str, reason: str) -> Path:
        from scripts.mac_test import mac_test

        path = hold_enforcer.machine_state_paths(machine).block
        path.parent.mkdir(parents=True, exist_ok=True)
        since = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
        mac_test.write_block(path, mac_test.ActiveMacBlock(
            holder, reason, since, since + timedelta(hours=4), mac_test.HolderQueue(holder), mac_test.NO_SHOWRUNNER,
            "not_configured", mac_test.FREE_MESSAGE_NOT_NEEDED))
        return path

    def enforce(self, holds: tuple[machine_hold.MachineHold, ...], machine: machine_hold.Machine,
                signals: list[tuple[int, int]]) -> tuple[str, ...]:
        return hold_enforcer.enforce_machine(machine_hold.ActiveHolds(holds), machine, self.proc,
                                             signal_process=lambda pid, number: signals.append((pid, number)))

    def test_machine_action_takes_the_strongest_hold_on_the_named_machine_only(self) -> None:
        drain, clear = self.machine_hold("natedev", "drain"), self.machine_hold("natedev", "clear")
        cases: tuple[tuple[tuple[machine_hold.MachineHold, ...], machine_hold.Machine, machine_hold.MachineAction], ...] = (
            ((), "natedev", machine_hold.Free()),
            ((drain,), "natedev", machine_hold.KeepRuns(drain)),
            ((drain, clear), "natedev", machine_hold.EndRuns(clear)),
            ((drain,), "mac", machine_hold.Free()),
            ((self.machine_hold("mac", "drain"),), "mac-linux", machine_hold.KeepRuns(self.machine_hold("mac", "drain"))),
            ((self.machine_hold("mac", "clear"),), "mac-linux", machine_hold.EndRuns(self.machine_hold("mac", "clear"))),
            ((self.machine_hold("mac-linux", "clear"),), "mac", machine_hold.Free()),
            ((self.machine_hold("hana-aws", "clear"),), "hana-aws",
             machine_hold.ClearWaitsForAwsHold(self.machine_hold("hana-aws", "clear"))),
        )
        for holds, machine, expected in cases:
            with self.subTest(holds=holds, machine=machine):
                self.assertEqual(machine_hold.machine_action(machine_hold.ActiveHolds(holds), machine), expected)

    def test_machine_names_refuse_any_other_name(self) -> None:
        for name in machine_hold.MACHINES:
            self.assertEqual(machine_hold.parse_machine(name), name)
        for name in (None, "", "aws", "all", "linux", "Mac"):
            with self.subTest(name=name):
                self.assertEqual(machine_hold.parse_machine(name), machine_hold.UnknownMachine(name or ""))

    def test_natedev_drain_ends_nothing_and_natedev_clear_stops_runs_outside_the_allow_rules(self) -> None:
        signals: list[tuple[int, int]] = []
        with mock.patch.object(hold_enforcer, "clear_queued_runs") as clear:
            self.assertEqual(self.enforce((self.machine_hold("natedev", "drain"),), "natedev", signals), ())
            clear.assert_not_called()
            self.assertEqual(self.enforce((self.machine_hold("natedev", "clear"),), "natedev", signals), ())
            clear.assert_called_once_with(self.proc)
        self.assertEqual(signals, [])

    def test_natedev_holder_files_read_as_machine_holds_with_their_verb(self) -> None:
        identity = build_hold.HolderIdentity("drainer", "drainer")
        _ = build_hold.write_hold(self.holders, identity, "folder switch", build_hold.NoReleaseEta(),
                                  datetime.now(timezone.utc), verb="drain")
        holds = hold_enforcer.active_holds(("natedev",)).on("natedev")
        self.assertCountEqual([(hold.verb, hold.comment) for hold in holds],
                              [("clear", "timed test"), ("drain", "folder switch")])

    def test_mac_clear_ends_each_claim_run_as_refused_with_the_reason(self) -> None:
        _ = self.process(40, "python3", argv=("python3", "mac_queue_run.py"))
        _ = self.process(41, "python3", argv=("python3", "mac_queue_run.py"))
        self.write_claim("mac", 40)
        self.write_claim("mac", 41, start="10000")
        signals: list[tuple[int, int]] = []
        hold = self.machine_hold("mac", "clear")
        lines = self.enforce((hold,), "mac", signals)
        reason = 'ended: mac cleared by bob — "Mac system update". A Mac run cannot go back to its lines yet.'
        self.assertEqual(machine_hold.refused_reason(hold), reason)
        self.assertEqual(signals, [(40, signal.SIGTERM)])
        self.assertEqual((self.proc / "40/fd/2").read_text(), reason + "\n")
        self.assertEqual((self.proc / "41/fd/2").read_text(), "")
        self.assertEqual(lines, (f"mac: pid 40 (tests 40) {reason}",))

    def test_mac_drain_ends_nothing_on_mac_or_mac_linux(self) -> None:
        _ = self.process(40, "python3", argv=("python3", "mac_queue_run.py"))
        self.write_claim("mac", 40)
        self.write_claim("mac-linux", 40)
        signals: list[tuple[int, int]] = []
        for machine in machine_hold.covered_machines("mac"):
            self.assertEqual(self.enforce((self.machine_hold("mac", "drain"),), machine, signals), ())
        self.assertEqual(signals, [])

    def test_mac_clear_leaves_a_wait_reservation_that_no_runner_has_adopted(self) -> None:
        # Section 4: a waiter stays in its lines. Its holder is verify.sh until a runner adopts the reservation.
        _ = self.process(45, "bash", argv=("bash", "/fixture/scripts/direct/verify.sh", "test"))
        _ = self.process(46, "python3", argv=("python3", "mac_queue_run.py"))
        self.write_claim("mac", 45)
        self.write_claim("mac", 46)
        signals: list[tuple[int, int]] = []
        lines = self.enforce((self.machine_hold("mac", "clear"),), "mac", signals)
        self.assertEqual(signals, [(46, signal.SIGTERM)])
        self.assertEqual(len(lines), 1)
        claims = hold_enforcer.machine_state_paths("mac").claims
        self.assertNotIn("ended", cast(dict[str, object], json.loads((claims / "repo-45.json").read_text())))
        self.assertEqual((self.proc / "45/fd/2").read_text(), "")

    def test_mac_clear_leaves_a_wait_reservation_whose_verify_shell_has_an_option_value(self) -> None:
        verify = "/fixture/scripts/direct/verify.sh"
        _ = self.process(45, "bash", argv=("bash", "-o", "pipefail", verify, "test"))
        self.write_claim("mac", 45)
        signals: list[tuple[int, int]] = []
        self.assertEqual(self.enforce((self.machine_hold("mac", "clear"),), "mac", signals), ())
        self.assertEqual(signals, [])
        for options in (("-eo", "pipefail"), ("-O", "extglob"), ("+o", "posix")):
            self.assertEqual(hold_enforcer.verify_arguments(("bash", *options, verify, "test")), (verify, "test"))

    def test_mac_clear_covers_mac_linux_and_a_mac_linux_clear_does_not_cover_mac(self) -> None:
        _ = self.process(40, "python3", argv=("python3", "mac_queue_run.py"))
        _ = self.process(50, "python3", argv=("python3", "mac_linux.py"))
        self.write_claim("mac", 40)
        self.write_claim("mac-linux", 50)
        signals: list[tuple[int, int]] = []
        own = (self.machine_hold("mac-linux", "clear"),)
        self.assertEqual(self.enforce(own, "mac", signals), ())
        self.assertEqual(len(self.enforce(own, "mac-linux", signals)), 1)
        self.assertEqual(signals, [(50, signal.SIGTERM)])
        signals.clear()
        mac = (self.machine_hold("mac", "clear"),)
        for machine in machine_hold.covered_machines("mac"):
            _ = self.enforce(mac, machine, signals)
        self.assertEqual(signals, [(40, signal.SIGTERM), (50, signal.SIGTERM)])
        self.assertIn("ended: mac cleared by bob", (self.proc / "50/fd/2").read_text())

    def test_mac_block_covers_mac_linux_and_the_mac_linux_block_stays_after_the_mac_release(self) -> None:
        mac = self.write_block("mac", "bob-id", "flight home")
        _ = self.write_block("mac-linux", "alice-id", "guest image")
        holds = hold_enforcer.active_holds(machine_hold.covered_machines("mac"))
        self.assertEqual([(hold.machine, hold.verb, hold.comment) for hold in holds.on("mac-linux")],
                         [("mac", "drain", "flight home"), ("mac-linux", "drain", "guest image")])
        self.assertEqual([hold.machine for hold in holds.on("mac")], ["mac"])
        mac.unlink()
        after = hold_enforcer.active_holds(machine_hold.covered_machines("mac"))
        self.assertEqual(machine_hold.machine_action(after, "mac"), machine_hold.Free())
        action = machine_hold.machine_action(after, "mac-linux")
        self.assertIsInstance(action, machine_hold.KeepRuns)
        self.assertEqual(cast(machine_hold.KeepRuns, action).hold.comment, "guest image")

    def test_hana_aws_clear_acts_as_a_drain_and_makes_no_aws_call(self) -> None:
        signals: list[tuple[int, int]] = []
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")), \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")):
            lines = self.enforce((self.machine_hold("hana-aws", "clear"),), "hana-aws", signals)
            drain = self.enforce((self.machine_hold("hana-aws", "drain"),), "hana-aws", signals)
        self.assertEqual(lines, ("hana-aws: clear waits for the AWS hold",))
        self.assertEqual(drain, ())
        self.assertEqual(signals, [])

    def test_a_run_that_named_natedev_and_mac_stays_in_its_natedev_line_after_drain_mac(self) -> None:
        (self.holders / "holder").unlink()
        _ = self.process(50, "bash", argv=("bash", "verify.sh", "test"), session="two-machines")
        _ = self.process(51, "cargo", parent=50, argv=("cargo", "test"))
        _ = self.process(60, "python3", argv=("python3", "mac_queue_run.py"))
        self.write_claim("mac", 60)
        state = self.run_state(reservations=(self.local_reservation(50, admission_protocol.NoRun()),))
        signals: list[tuple[int, int]] = []
        for machine in machine_hold.covered_machines("mac"):
            self.assertEqual(self.enforce((self.machine_hold("mac", "drain"),), machine, signals), ())
        self.assertEqual(signals, [])
        self.assertEqual(hold_enforcer.taken_runs(state, self.proc), frozenset({build_hold.ProcessIdentity(50, "11000")}))
        decisions = {item.process.identity.pid: item for item in self.decisions()}
        self.assertIsInstance(decisions[51], hold_enforcer.AllowedBuild)
        # A clear of the Mac ends only the run with the Mac claim.
        for machine in machine_hold.covered_machines("mac"):
            _ = self.enforce((self.machine_hold("mac", "clear"),), machine, signals)
        self.assertEqual(signals, [(60, signal.SIGTERM)])
        self.assertEqual((self.proc / "50/fd/2").read_text(), "")

    def test_clear_mac_command_blocks_the_mac_then_ends_its_claim_runs(self) -> None:
        _ = self.process(40, "python3", argv=("python3", "mac_queue_run.py"))
        self.write_claim("mac", 40)
        signals: list[tuple[int, int]] = []
        original = hold_enforcer.end_claim_runs

        def end_claim_runs(machine: machine_hold.Machine, hold: machine_hold.MachineHold, proc_root: Path,
                           **_: object) -> tuple[str, ...]:
            return original(machine, hold, proc_root, signal_process=lambda pid, number: signals.append((pid, number)))

        _ = self.write_block("mac", "holder", "Mac system update")
        result = build_hold.MacHoldResult(0, "Mac blocked for holder.\n", "")
        for action, notices in (("drain", ["--showrunner", "producer"]), ("clear", [])):
            with self.subTest(action=action), \
                 mock.patch.object(build_hold, "run_mac_hold", return_value=result) as block, \
                 mock.patch.object(hold_enforcer, "end_claim_runs", side_effect=end_claim_runs), \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(build_hold.main([action, "mac", "--holder", "holder", "--for", "Mac system update",
                                                  "--proc-root", str(self.proc), *notices]), 0)
                block.assert_called_once_with(["block", "--holder", "holder", "--for", "Mac system update",
                                               "--hold-verb", action, *notices], "mac")
                if action == "drain":
                    self.assertEqual(signals, [])
                    self.assertEqual(output.getvalue(), "Mac blocked for holder.\n")
                else:
                    self.assertEqual(signals, [(40, signal.SIGTERM)])
                    self.assertIn('ended: mac cleared by holder — "Mac system update"', output.getvalue())

    def test_clear_mac_ends_the_runs_when_the_block_is_set_but_the_ci_switch_step_fails(self) -> None:
        _ = self.process(40, "python3", argv=("python3", "mac_queue_run.py"))
        self.write_claim("mac", 40)
        _ = self.write_block("mac", "holder", "Mac system update")
        for action in ("drain", "clear"):
            advice = f"CI can still use the Mac: cold agent. Run github-warmup, then /build:hold {action} mac again.\n"
            signals: list[tuple[int, int]] = []
            original = hold_enforcer.end_claim_runs

            def end_claim_runs(machine: machine_hold.Machine, hold: machine_hold.MachineHold, proc_root: Path,
                               **_: object) -> tuple[str, ...]:
                return original(machine, hold, proc_root,
                                signal_process=lambda pid, number: signals.append((pid, number)))

            with self.subTest(action=action), \
                 mock.patch.object(build_hold, "run_mac_hold",
                                   return_value=build_hold.MacHoldResult(2, advice, "")) as block, \
                 mock.patch.object(hold_enforcer, "end_claim_runs", side_effect=end_claim_runs), \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(build_hold.main([action, "mac", "--holder", "holder", "--for", "Mac system update",
                                                  "--proc-root", str(self.proc)]), 2)
                self.assertEqual(cast(list[str], block.call_args.args[0])[-2:], ["--hold-verb", action])
                printed = output.getvalue()
                self.assertTrue(printed.startswith(advice))
                if action == "drain":
                    self.assertEqual((signals, printed), ([], advice))
                else:
                    self.assertEqual(signals, [(40, signal.SIGTERM)])
                    self.assertTrue(printed[len(advice):].startswith(
                        'mac: pid 40 (tests 40) ended: mac cleared by holder'))

    def test_a_damaged_mac_linux_block_does_not_stop_clear_mac(self) -> None:
        _ = self.process(40, "python3", argv=("python3", "mac_queue_run.py"))
        _ = self.process(50, "python3", argv=("python3", "mac_linux.py"))
        self.write_claim("mac", 40)
        self.write_claim("mac-linux", 50)
        _ = self.write_block("mac", "holder", "Mac system update")
        _ = hold_enforcer.machine_state_paths("mac-linux").block.write_text("{damaged")
        signals: list[tuple[int, int]] = []
        original = hold_enforcer.end_claim_runs

        def end_claim_runs(machine: machine_hold.Machine, hold: machine_hold.MachineHold, proc_root: Path,
                           **_: object) -> tuple[str, ...]:
            return original(machine, hold, proc_root, signal_process=lambda pid, number: signals.append((pid, number)))

        with mock.patch.object(build_hold, "run_mac_hold", return_value=build_hold.MacHoldResult(0, "", "")), \
             mock.patch.object(hold_enforcer, "end_claim_runs", side_effect=end_claim_runs), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(build_hold.main(["clear", "mac", "--holder", "holder", "--for", "Mac system update",
                                              "--proc-root", str(self.proc)]), 0)
        # clear mac still ends the mac-linux claim runs through the mac hold.
        self.assertEqual(signals, [(40, signal.SIGTERM), (50, signal.SIGTERM)])

    def test_a_damaged_mac_block_prints_the_mac_test_output_then_the_error(self) -> None:
        _ = self.process(40, "python3", argv=("python3", "mac_queue_run.py"))
        self.write_claim("mac", 40)
        path = hold_enforcer.machine_state_paths("mac").block
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("{damaged")
        state = "Mac blocked for holder.\nIt lifts by itself at Sun 16:00 PST unless you run /build:hold drain mac again.\n"
        with mock.patch.object(build_hold, "run_mac_hold", return_value=build_hold.MacHoldResult(0, state, "")), \
             mock.patch.object(hold_enforcer, "end_claim_runs", side_effect=AssertionError("no run ends")), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(build_hold.main(["clear", "mac", "--holder", "holder", "--for", "Mac system update",
                                              "--proc-root", str(self.proc)]), 1)
        printed = output.getvalue()
        self.assertTrue(printed.startswith(state))
        self.assertTrue(printed[len(state):].startswith("mac: block unreadable; the clear ended no run: "))

    def test_clear_mac_ends_no_run_when_the_block_is_not_set(self) -> None:
        _ = self.process(40, "python3", argv=("python3", "mac_queue_run.py"))
        self.write_claim("mac", 40)
        refused = build_hold.MacHoldResult(1, "already blocked by alice\n", "")
        with mock.patch.object(build_hold, "run_mac_hold", return_value=refused), \
             mock.patch.object(hold_enforcer, "end_claim_runs", side_effect=AssertionError("no run ends")), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(build_hold.main(["clear", "mac", "--holder", "holder", "--for", "Mac system update",
                                              "--proc-root", str(self.proc)]), 1)
        self.assertEqual(output.getvalue(), "already blocked by alice\n")

    def test_mac_clear_signals_no_claim_without_a_positive_pid_and_a_decimal_start(self) -> None:
        self.write_claim("mac", -1, start="")
        self.write_claim("mac", 0, start="")
        self.write_claim("mac", 42, start="not-a-start")
        signals: list[tuple[int, int]] = []
        self.assertEqual(self.enforce((self.machine_hold("mac", "clear"),), "mac", signals), ())
        self.assertEqual(signals, [])

    def test_mac_clear_names_a_run_it_may_not_signal_and_ends_the_next_run(self) -> None:
        for pid in (40, 41):
            _ = self.process(pid, "python3", argv=("python3", "mac_queue_run.py"))
            self.write_claim("mac", pid)

        def signal_process(pid: int, number: int) -> None:
            if pid == 40:
                raise PermissionError(1, "Operation not permitted")
            signals.append((pid, number))

        signals: list[tuple[int, int]] = []
        lines = hold_enforcer.enforce_machine(machine_hold.ActiveHolds((self.machine_hold("mac", "clear"),)), "mac",
                                              self.proc, signal_process=signal_process)
        self.assertEqual(signals, [(41, signal.SIGTERM)])
        self.assertEqual(sorted(line.split(" (")[0] for line in lines), ["mac: pid 40", "mac: pid 41"])
        self.assertIn("mac: pid 40 (tests 40) could not be ended: [Errno 1] Operation not permitted", lines)

    # Defect B: a clear, or any other runner death, leaves no Mac attempt that is not settled.

    def start_runner(self) -> tuple[subprocess.Popen[bytes], str]:
        """Start a stand-in offload runner that this test owns, and give its real start time."""
        from scripts.lib import process_start

        runner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(runner.wait)
        self.addCleanup(runner.kill)
        started = process_start.proc_table_start(runner.pid, Path("/proc"))
        assert isinstance(started, process_start.Started)
        _ = self.process(runner.pid, "python3", start=int(started.start), argv=("python3", "offload.py"))
        return runner, started.start

    def runner_exits(self, runner: subprocess.Popen[bytes]) -> int:
        """Reap the runner, and remove its entry from the test process table as the kernel does."""
        status = runner.wait(timeout=10)
        shutil.rmtree(self.proc / str(runner.pid))
        return status

    def started_offload_run(self, pid: int, start: str) -> tuple[Path, Path, Path]:
        """Write the assignment, the remote claim and the pending start of an offload run on the Mac."""
        from scripts.build_queue import admission_client
        from scripts.mac_test import offload

        _ = write_config_fixture(self.root / "config/build_targets.conf",
                                 "targets=natedev,mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n")
        destination = placement.PlacementDestination("mac", "mac", "mac", claim_state.RepositoryScope("repo"))
        assignment = placement.PlacementAssignment("verify-call", "attempt-1", destination,
            execution_identity.SourceKeyKnown("source"), placement.SubmittingOwner("natedev", os.getpid(), "start"),
            NoOwningSession(), 1.0)
        assignment_file = self.root / "step.assignment.json"
        admission_client.write_assignment(assignment_file, admission_protocol.Assignment(assignment))
        claim = hold_enforcer.machine_state_paths("mac").claims / "repo.json"
        claim.parent.mkdir(parents=True, exist_ok=True)
        record: claim_state.RemoteJobClaim = {
            "claim_id": self.run_identity().claim_id, "episode": "verify-call", "attempt": "attempt-1",
            "submitting_session": "", "pid": pid, "proc_start": start, "what": "tests sample", "worktree": "repo",
            "since": "2026-11-22T12:00:00+00:00", "repo_name": "repo", "repo_key": "", "remote_host": "mac.test"}
        _ = claim.write_text(json.dumps(record))
        pending = offload.pending_start_path("mac", self.run_identity())
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.touch()
        return assignment_file, claim, pending

    def run_identity(self) -> claim_state.ClaimIdentity:
        return claim_state.ClaimIdentity(execution_identity.assigned_claim_id("verify-call", "attempt-1"),
                                         "verify-call", "attempt-1")

    def settle_run(self, assignment_file: Path, *, work_remains: bool = False, report_fails: bool = False
                   ) -> tuple[str, list[admission_protocol.ExecutionReport], list[str]]:
        """Run the caller's run-end settle with the Mac cleanup and the queue as stand-ins.

        Return the runner result file that the settle wrote ("" when it wrote none), the reports and the cleanup scripts.
        """
        from scripts.build_queue import admission_client
        from scripts.build_targets import build_targets
        from scripts.mac_test import eligibility, offload

        result = assignment_file.with_name("runner-result")
        reports: list[admission_protocol.ExecutionReport] = []
        scripts: list[str] = []

        def read_leftovers(_config: object, host: str, script: str, target: str) -> offload.LeftoverObservation:
            self.assertEqual((host, target), ("mac.test", "mac"))
            scripts.append(script)
            if work_remains:
                return offload.LeftoverObservation(offload.LeftoversRemain(1), "leftovers=1\nrc=0\n")
            settled = "dispatch_start_settled=" + offload.pending_start_key(self.run_identity())
            return offload.LeftoverObservation(offload.LeftoversEnded(), f"leftovers=0\n{settled}\nrc=0\n")

        def report_execution(request: admission_protocol.ExecutionReport) -> admission_protocol.DispatchStatus:
            reports.append(request)
            if report_fails:
                raise admission_protocol.AdmissionUnavailable("queue down")
            return cast(admission_protocol.DispatchStatus, mock.sentinel.status)

        with mock.patch.object(eligibility, "read_connection_settings"), \
             mock.patch.object(offload, "read_leftovers", side_effect=read_leftovers), \
             mock.patch.object(admission_client, "report_execution", side_effect=report_execution), \
             redirect_stdout(io.StringIO()) as output:
            build_targets.settle_ended_runner(assignment_file, self.run_identity(), NoOwningSession(), str(result))
        self.assertEqual(output.getvalue(), "")
        return (result.read_text() if result.exists() else ""), reports, scripts

    def test_clear_mac_settles_the_started_offload_run_as_refused_and_its_caller_stops(self) -> None:
        runner, start = self.start_runner()
        assignment_file, claim, pending = self.started_offload_run(runner.pid, start)
        hold = self.machine_hold("mac", "clear")
        reason = machine_hold.refused_reason(hold)
        lines = hold_enforcer.enforce_machine(machine_hold.ActiveHolds((hold,)), "mac", self.proc)
        self.assertEqual(lines, (f"mac: pid {runner.pid} (tests sample) {reason}",))
        self.assertEqual(self.runner_exits(runner), -signal.SIGTERM)
        self.assertEqual(cast(dict[str, object], json.loads(claim.read_text()))["ended"], reason)
        result, reports, scripts = self.settle_run(assignment_file)
        # The refused result gives the 6.1 reason to the caller, and verify.sh stops on it.
        self.assertEqual(result, f"mac=refused\nreason={reason}\nseconds=0.000000\n")
        self.assertEqual(len(scripts), 1)
        self.assertIn("leftovers", scripts[0])
        self.assertEqual([(report.outcome, report.path) for report in reports],
                         [(placement.AttemptDeclined(reason), placement.DispatchPathSettled())])
        self.assertFalse(claim.exists())
        self.assertFalse(pending.exists())

    def test_a_cleared_run_whose_cleanup_fails_names_the_failure_and_its_caller_stops(self) -> None:
        runner, start = self.start_runner()
        assignment_file, claim, _pending = self.started_offload_run(runner.pid, start)
        hold = self.machine_hold("mac", "clear")
        _ = hold_enforcer.enforce_machine(machine_hold.ActiveHolds((hold,)), "mac", self.proc)
        _ = self.runner_exits(runner)
        result, reports, _scripts = self.settle_run(assignment_file, work_remains=True)
        failure = "the remote work of the claim has not ended"
        self.assertEqual(result, f"mac=refused\nreason=the Mac run ended, and its cleanup failed: {failure}\n"
                                 + "seconds=0.000000\n")
        # The queue gets the attempt end with the named failure; the dispatch stays pending while the claim stays.
        self.assertEqual([(report.outcome, report.path) for report in reports],
                         [(placement.AttemptDeclined(machine_hold.refused_reason(hold)), placement.DispatchPathPending(failure))])
        self.assertTrue(claim.exists())

    def test_a_report_failure_after_a_good_release_names_it_and_sends_no_pending_report(self) -> None:
        runner, start = self.start_runner()
        assignment_file, claim, pending = self.started_offload_run(runner.pid, start)
        hold = self.machine_hold("mac", "clear")
        reason = machine_hold.refused_reason(hold)
        _ = hold_enforcer.enforce_machine(machine_hold.ActiveHolds((hold,)), "mac", self.proc)
        _ = self.runner_exits(runner)
        with redirect_stderr(io.StringIO()) as errors:
            result, reports, _scripts = self.settle_run(assignment_file, report_fails=True)
        # The cleanup worked and the claim is gone: one report, no pending path, and no "cleanup failed".
        self.assertEqual([(report.outcome, report.path) for report in reports],
                         [(placement.AttemptDeclined(reason), placement.DispatchPathSettled())])
        self.assertEqual(result, f"mac=refused\nreason={reason}\nseconds=0.000000\n")
        self.assertEqual(errors.getvalue(),
                         "release-wait: the Mac claim is released, and the report of the attempt end failed: queue down\n")
        self.assertFalse(claim.exists())
        self.assertFalse(pending.exists())

    def test_a_runner_that_dies_without_a_clear_settles_its_attempt_as_lost(self) -> None:
        runner, start = self.start_runner()
        assignment_file, claim, pending = self.started_offload_run(runner.pid, start)
        # Out of memory, or a SIGKILL by hand: the runner runs no code of its own.
        runner.kill()
        _ = self.runner_exits(runner)
        result, reports, scripts = self.settle_run(assignment_file)
        # The caller goes on with its "lost" result; the queue closes the attempt, so the next placement can assign.
        self.assertEqual(result, "")
        self.assertEqual(len(scripts), 1)
        self.assertEqual([(report.outcome, report.path) for report in reports],
                         [(placement.AttemptDeclined("lost"), placement.DispatchPathSettled())])
        self.assertFalse(claim.exists())
        self.assertFalse(pending.exists())

    def test_a_lost_run_whose_cleanup_fails_keeps_its_claim_for_the_next_claim(self) -> None:
        runner, start = self.start_runner()
        assignment_file, claim, pending = self.started_offload_run(runner.pid, start)
        runner.kill()
        _ = self.runner_exits(runner)
        # No clear ended the run: its caller goes on, and the next claim of the scope takes the claim over.
        result, reports, _scripts = self.settle_run(assignment_file, work_remains=True)
        self.assertEqual((result, reports), ("", []))
        self.assertTrue(claim.exists())
        self.assertTrue(pending.exists())

    def cleared_kept_claim(self) -> tuple[Path, Path, claim_state.HeldClaim]:
        """A started offload run that a clear marked, whose runner is gone."""
        from scripts.mac_test import mac_test

        runner, start = self.start_runner()
        assignment_file, claim, _pending = self.started_offload_run(runner.pid, start)
        runner.kill()
        _ = self.runner_exits(runner)
        paths = hold_enforcer.machine_state_paths("mac")
        held = claim_state.read_claims(paths)[0]
        self.assertTrue(mac_test.mark_claim_ended(paths, held, "ended: fixture clear"))
        return assignment_file, claim, claim_state.read_claims(paths)[0]

    def test_a_takeover_drops_the_ended_reason_of_the_kept_claim(self) -> None:
        from scripts.mac_test import mac_test

        _assignment_file, claim, held = self.cleared_kept_claim()
        assert "remote_host" in held.record
        _ = self.process(777, "python3", start=12_000, argv=("python3", "offload.py"))
        arguments = mac_test.CliArguments()
        arguments.pid = 777
        with redirect_stdout(io.StringIO()):
            _ = mac_test.take_over_kept_claim(arguments, held, held.record, session=NoOwningSession())
        record = cast(dict[str, object], json.loads(claim.read_text()))
        self.assertEqual((record["pid"], record.get("cleanup_pending")), (777, True))
        self.assertNotIn("ended", record)

    def test_the_release_of_a_cleared_claim_prints_the_clear_reason(self) -> None:
        from scripts.mac_test import mac_test, offload

        _assignment_file, claim, held = self.cleared_kept_claim()
        offload.pending_start_path("mac", self.run_identity()).unlink()
        arguments = mac_test.CliArguments()
        arguments.pid = held.record["pid"]
        arguments.claim_id, arguments.episode, arguments.attempt = (
            held.record["claim_id"], held.record["episode"], held.record["attempt"])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(mac_test.command_release(arguments, hold_enforcer.machine_state_paths("mac")), 0)
        self.assertEqual(output.getvalue(), 'ended="ended: fixture clear"\n')
        self.assertFalse(claim.exists())

    def test_a_run_end_settle_without_a_target_state_folder_makes_no_folder(self) -> None:
        runner, start = self.start_runner()
        assignment_file, _claim, _pending = self.started_offload_run(runner.pid, start)
        state = hold_enforcer.machine_state_paths("mac").directory
        shutil.rmtree(state)
        self.assertEqual(self.settle_run(assignment_file), ("", [], []))
        self.assertFalse(state.exists())

    def test_a_drain_leaves_a_kept_run_between_two_steps_unsettled(self) -> None:
        runner, start = self.start_runner()
        assignment_file, claim, pending = self.started_offload_run(runner.pid, start)
        signals: list[tuple[int, int]] = []
        for machine in machine_hold.covered_machines("mac"):
            self.assertEqual(self.enforce((self.machine_hold("mac", "drain"),), machine, signals), ())
        self.assertEqual(signals, [])
        self.assertNotIn("ended", cast(dict[str, object], json.loads(claim.read_text())))
        # The run still lives, so a run-end settle finds nothing to settle.
        self.assertEqual(self.settle_run(assignment_file), ("", [], []))
        self.assertTrue(claim.exists())
        self.assertTrue(pending.exists())
        self.assertIsNone(runner.poll())

    def test_mac_clear_never_signals_the_caller_or_its_ancestors(self) -> None:
        _ = self.process(40, "python3", argv=("python3", "mac_queue_run.py"))
        _ = self.process(41, "python3", parent=40, argv=("python3", "build_hold.py", "clear", "mac"))
        _ = self.process(50, "python3", argv=("python3", "mac_queue_run.py"))
        for pid in (40, 41, 50):
            self.write_claim("mac", pid)
        signals: list[tuple[int, int]] = []
        with mock.patch.object(os, "getpid", return_value=41):
            _ = self.enforce((self.machine_hold("mac", "clear"),), "mac", signals)
        self.assertEqual(signals, [(50, signal.SIGTERM)])

    def test_release_mac_command_ends_the_mac_block_only(self) -> None:
        result = build_hold.MacHoldResult(0, "", "")
        with mock.patch.object(build_hold, "release_mac_hold", return_value=result) as release, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(build_hold.main(["release", "mac", "--holder", "holder"]), 0)
        release.assert_called_once_with(build_hold.HolderIdentity("holder", "holder"), "mac")
        self.assertTrue((self.holders / "holder").is_file())


if __name__ == "__main__":
    _ = unittest.main()
